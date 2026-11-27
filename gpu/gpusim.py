#!/usr/bin/env python3
"""gpusim - a fake 8-node x 8 H100 cluster for GPU / InfiniBand diagnosis drills.

One file, standard library only (runs on macOS python3 and in python:3.12-alpine).

  serve [--port 9400]          Prometheus exporter: DCGM-style GPU metrics + node_exporter-style IB metrics
  run <node> <command...>      print what <command> would show on <node> (nvidia-smi, dmesg, ibstat, ...)
  shell <node>                 interactive fake root shell on <node>
  faults                       list injectable fault types
  add <type> <node> [n]        inject a fault (n = GPU index or HCA index, default random)
  drill                        inject one random hidden fault and print the ticket
  reveal | show | clear        answer key / current faults / remove all faults
  nodes | node-manifests | status-patch <node> | pods-json   helpers for lab.sh
  fixtures <dir>               write static outputs for every fault type (offline study)
  metrics                      print one metrics scrape to stdout

Fault state lives in a JSON file: $GPUSIM_STATE (default: state.json next to this script).
In the cluster the exporter reads the same file from a ConfigMap.
"""
import hashlib
import json
import os
import random
import shlex
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("GPUSIM_STATE", os.path.join(HERE, "state.json"))

NODES = ["h100-%02d" % i for i in range(1, 9)]
NGPU = 8
GPU_NAME = "NVIDIA H100 80GB HBM3"
GPU_BUS = ["18", "2a", "3a", "5d", "9a", "ab", "ba", "db"]      # DGX-style SXM5 layout
HCA_BUS = ["1a", "3c", "4d", "5e", "9c", "bc", "cc", "dc"]      # one ConnectX-7 per GPU (rail-optimized)
DRIVER, CUDA, DCGM = "580.95.05", "13.0", "4.4.1"
CX7_FW = "28.45.1020"
FB_TOTAL = 81559
T0 = 1793491200          # 2026-11-01 00:00 UTC: counters start here
NVLINKS = 18

# DCGM clock-event (throttle) reason bits
GPU_IDLE, SW_POWER_CAP, HW_SLOWDOWN, SW_THERMAL, HW_THERMAL, HW_POWER_BRAKE = 0x1, 0x4, 0x8, 0x20, 0x40, 0x80

# ---------------------------------------------------------------------------------------------
# Fault catalogue. kind: gpu (target = GPU index), ib (target = HCA/port index), node (whole node).
# crash: the training job's pod on that node dies (node goes idle; lab.sh restarts the pod).
# ---------------------------------------------------------------------------------------------
FAULTS = {
    "xid79": dict(kind="gpu", crash=True, level=1, title="GPU fell off the bus (Xid 79)",
        ticket="llm-train crashed at step 41,207 with 'CUDA error: unspecified launch failure' on {pod}. "
               "After the automatic restart one pod is stuck Pending.",
        answer="{node} GPU {t} (bus {bus}) fell off the PCIe bus: Xid 79, the GPU is gone from nvidia-smi and "
               "DCGM, lspci shows rev ff, and the device plugin now advertises 7 GPUs, so the 8-GPU pod can't "
               "schedule anywhere. Action: cordon + drain {node}, power-cycle (a warm reboot often isn't enough), "
               "run dcgmi diag -r 3. If it repeats, RMA the GPU or baseboard. Check the BMC for PCIe/power/thermal events."),
    "xid94": dict(kind="gpu", crash=True, level=2, title="Contained uncorrectable ECC error (Xid 94)",
        ticket="llm-train died with 'CUDA error: uncorrectable ECC error encountered' on {pod}. It restarted on the same nodes.",
        answer="{node} GPU {t}: a contained DRAM uncorrectable error (Xid 94). Only the process using that memory was killed. "
               "A row remap is pending (Xid 63, 'Pending: Yes'), and it only takes effect after a GPU reset. Action: drain {node}, "
               "reset the GPU (nvidia-smi -r -i {t}) or reboot, confirm Pending: No, then return it to service. "
               "RMA only if remapping fails or the errors keep coming."),
    "xid95": dict(kind="gpu", crash=True, level=2, title="Uncontained uncorrectable ECC error (Xid 95)",
        ticket="All ranks on {node} died at once. Logs show 'uncorrectable ECC error' on several ranks, not just one.",
        answer="{node} GPU {t}: an uncontained ECC error (Xid 95). The driver couldn't isolate it, so every process "
               "on the GPU was affected. A row remap is pending. Action: drain {node} and reset or reboot (required). "
               "Confirm the remap and run dcgmi diag -r 3 before returning it. Repeat offender: RMA."),
    "row-remap-fail": dict(kind="gpu", crash=True, level=3, title="Row-remapping failure (Xid 64)",
        ticket="{pod} keeps crashing with ECC errors even after the node was rebooted yesterday.",
        answer="{node} GPU {t} has run out of spare rows: Xid 64, 'Remapping Failure Occurred: Yes', and dcgmi diag "
               "fails the memory test. A reset will not fix it. Action: take {node} out of service and RMA the GPU."),
    "sbe-storm": dict(kind="gpu", crash=False, level=2, title="Correctable ECC storm (Xid 92)",
        ticket="No crash yet, but the GPU-health dashboard shows one GPU's ECC counter climbing.",
        answer="{node} GPU {t}: single-bit (correctable) ECC errors are climbing fast and the correctable remap count is rising. "
               "After a while it raises Xid 92 (high SBE rate). Correctable errors aren't fatal, but a storm often comes before an "
               "uncorrectable one. Action: watch it, plan a drain at the next checkpoint, reset to apply remaps. RMA if the rate persists."),
    "thermal": dict(kind="gpu", crash=False, level=1, title="Thermal slowdown",
        ticket="Step time went from 1.9s to 2.6s. Nothing crashed and no one changed the code.",
        answer="{node} GPU {t} is at ~90C with HW Thermal Slowdown active: SM clock ~1100 MHz versus ~1980 MHz on its peers. "
               "One slow GPU throttles the whole synchronous job (a straggler). Action: check the fans/airflow/inlet temperature in the BMC "
               "and any blocked filters, drain at the next checkpoint, then have DC ops inspect the node."),
    "power-brake": dict(kind="node", crash=False, level=2, title="HW power brake (PSU/BMC)",
        ticket="Throughput dropped ~40%. Only one node's power draw looks odd on the dashboard.",
        answer="All 8 GPUs on {node} show HW Power Brake Slowdown active, ~250W draw and ~700 MHz clocks. That is an external "
               "power-brake signal (failed/derated PSU or BMC power capping), not a GPU fault. Action: check the PSUs and SEL in the BMC, "
               "and drain the node until the PSU is replaced."),
    "nvlink": dict(kind="gpu", crash=False, level=3, title="NVLink CRC errors -> Xid 74",
        ticket="All-reduce bandwidth inside one node looks low. Earlier today someone saw a few NCCL retries.",
        answer="{node} GPU {t}: NVLink CRC flit errors are climbing on link 7 and NVLink bandwidth is down. About 20 min after the "
               "fault starts it escalates to Xid 74 (fatal NVLink error) and the job crashes. Action: drain early. Reset the GPU and "
               "NVSwitch fabric (restart fabric manager or reboot). If errors return, the baseboard/NVSwitch tray needs service."),
    "pcie-replay": dict(kind="gpu", crash=False, level=3, title="PCIe replays / AER corrected errors",
        ticket="Data-loader-heavy jobs on one node are slower than on the others. Host-to-device copies look slow.",
        answer="{node} GPU {t}: the PCIe replay counter is climbing and dmesg shows AER 'Corrected ... Physical Layer RxErr' from the "
               "GPU's root port. That points to a marginal PCIe link (riser/retimer/seating). Action: drain, reseat, update the retimer "
               "firmware. If it persists, service the baseboard."),
    "xid31": dict(kind="gpu", crash=True, level=2, title="MMU fault from application (Xid 31)",
        ticket="New experiment branch crashes about 10 min in on {pod} with 'an illegal memory access was encountered'. The user wants the node replaced.",
        answer="Xid 31 (GPU memory page fault) on {node} GPU {t}, but ECC is clean, there are no remaps, and dcgmi diag passes. "
               "This is almost always the application: an out-of-bounds access in a custom kernel or a bad index. "
               "Action: do NOT RMA. Send it back to the user with the Xid line and suggest compute-sanitizer / CUDA_LAUNCH_BLOCKING=1. "
               "If the same code crashes on different nodes, that confirms it."),
    "ib-down": dict(kind="ib", crash=True, level=1, title="InfiniBand port down",
        ticket="llm-train hangs at start-up, then NCCL times out: 'NET/IB : Got completion from peer ... with status=12'.",
        answer="{node} {hca} port 1 is Down / Polling. The physical link isn't up, so there is no peer (cable, transceiver or switch port). "
               "rdma/ib allocatable dropped to 7. Action: drain {node}, check the cable/transceiver and the leaf switch port, "
               "and reseat or swap the optic."),
    "ib-flap": dict(kind="ib", crash=False, level=2, title="InfiniBand link flapping",
        ticket="Jobs fail at random times, every hour or two, with NCCL IB timeouts. Rerunning sometimes works.",
        answer="{node} {hca}: LinkDownedCounter goes up every ~3 min and LinkErrorRecovery climbs, so the link keeps "
               "retraining. Usually a marginal cable/optic or a switch port. Action: drain, swap the optic/cable, and check the switch "
               "side counters (ibdiagnet). A flapping link causes intermittent failures that are hard to reproduce."),
    "ib-symbol": dict(kind="ib", crash=False, level=2, title="InfiniBand symbol errors (bad cable/optic)",
        ticket="Multi-node all-reduce is ~10% slower than last week, and only some job placements are affected.",
        answer="{node} {hca}: SymbolErrorCounter and PortRcvErrors are climbing steadily while the link stays Active. That is "
               "physical-layer bit errors (dirty connector, bent fiber, failing transceiver). Action: clean or replace the cable/optic "
               "at the next maintenance. Watch for escalation to link flaps."),
    "ib-degraded": dict(kind="ib", crash=False, level=3, title="InfiniBand link trained at reduced width",
        ticket="NCCL all_reduce_perf bus bandwidth is about half of what it was at acceptance. No errors anywhere.",
        answer="{node} {hca} is Active but at 200 Gb/s (2X width) instead of 400 Gb/s (4X NDR). Two lanes failed to train. "
               "Ring all-reduce runs at the speed of its slowest link, so the whole job halves. Action: reseat/replace the "
               "cable or optic. ibdiagnet / ibportstate show the width."),
    "ib-congestion": dict(kind="node", crash=False, level=3, title="Fabric congestion (not a node fault)",
        ticket="Job throughput dips at certain times of day. Someone wants to RMA {node} because its NICs 'look busy'.",
        answer="Every HCA on {node} shows a fast-rising PortXmitWait with zero errors and Active links at full rate. That is "
               "congestion: the port has data but no credits, often from another tenant's traffic or a hotspot on the "
               "uplinks. Action: do NOT RMA. Look at fabric-wide counters, routing (adaptive routing / SHARP), job placement and "
               "other tenants' jobs."),
}


# ---------------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------------
def load_state(path=None):
    path = path or STATE
    try:
        with open(path) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("faults", [])
    s.setdefault("hidden", False)
    s.setdefault("pods", None)       # {node: pod} of running llm-train pods; None = unknown (offline)
    return s


def save_state(s, path=None):
    with open(path or STATE, "w") as f:
        json.dump(s, f, indent=2, sort_keys=True)
        f.write("\n")


def h(*parts):
    return int(hashlib.sha256("/".join(str(p) for p in parts).encode()).hexdigest(), 16)


def jitter(lo, hi, *seed):
    """Deterministic noise that changes every 15s (stable within a scrape)."""
    r = (h(*seed, int(time.time() // 15)) % 10000) / 10000.0
    return lo + (hi - lo) * r


def faults_on(s, node, kind=None, target=None):
    out = []
    for f in s["faults"]:
        if f["node"] != node:
            continue
        k = FAULTS[f["type"]]["kind"]
        if kind and k != kind and k != "node":
            continue
        if target is not None and k != "node" and f.get("target") != target:
            continue
        out.append(f)
    return out


def has(s, node, ftype, target=None):
    for f in faults_on(s, node, target=target):
        if f["type"] == ftype and (target is None or FAULTS[ftype]["kind"] == "node" or f.get("target") == target):
            return f
    return None


def node_crashed(s, node, now):
    """Time the node's training pod stopped (fault crash, or drained by the learner), else None."""
    pods = s.get("pods")
    if pods is not None and node not in pods:
        return now - 1
    for f in faults_on(s, node):
        t = f["type"]
        if FAULTS[t]["crash"]:
            return f["since"]
        if t == "nvlink" and now - f["since"] > 1200:
            return f["since"] + 1200
    return None


def uuid(node, g):
    x = "%032x" % (h("uuid", node, g) % (1 << 128))
    return "GPU-%s-%s-%s-%s-%s" % (x[:8], x[8:12], x[12:16], x[16:20], x[20:])


def serial(node, g):
    return "16523%08d" % (h("sn", node, g) % 10**8)


def guid(node, p):
    return "0x946dae0300%06x" % (h("guid", node, p) % (1 << 24))


def lid(node, p):
    return 0x10 + NODES.index(node) * 8 + p if node in NODES else 0x10 + p


def boot_time(node):
    return T0 + 14 * 86400 + (h("boot", node) % 86400)


def pod_of(s, node):
    return (s.get("pods") or {}).get(node) or "llm-train-%d" % (NODES.index(node) if node in NODES else 0)


# ---------------------------------------------------------------------------------------------
# Simulated device state
# ---------------------------------------------------------------------------------------------
def gpu_state(s, node, g, now):
    crashed = node_crashed(s, node, now)
    busy = crashed is None
    d = dict(present=True, busy=busy, xid=0, sbe=0, dbe=0, remap_corr=0, remap_unc=0, remap_pending=0,
             remap_fail=0, nvlink_crc=0, pcie_replay=int(h("replay", node, g) % 3), throttle=0,
             nvlink_inactive=[], nvlink_bw_factor=1.0, events=[])
    if busy:
        d.update(util=int(jitter(94, 100, node, g, "u")), temp=int(jitter(58, 67, node, g, "t")),
                 power=int(jitter(560, 655, node, g, "p")), sm=1980, fb=int(jitter(70800, 72400, node, g, "fb")),
                 tensor=jitter(0.52, 0.61, node, g, "ta"))
        d["memtemp"] = d["temp"] + int(jitter(4, 8, node, g, "mt"))
    else:
        d.update(util=0, temp=int(jitter(30, 36, node, g, "t")), power=int(jitter(68, 78, node, g, "p")),
                 sm=345, fb=0, tensor=0.0, throttle=GPU_IDLE)
        d["memtemp"] = d["temp"] + 3

    pb = has(s, node, "power-brake")
    if pb:
        d.update(throttle=d["throttle"] | HW_SLOWDOWN | HW_POWER_BRAKE, power=int(jitter(230, 265, node, g, "pb")),
                 sm=705, tensor=d["tensor"] * 0.38, temp=max(30, d["temp"] - 15))
        d["events"].append((pb["since"] + 3 + g, "pb"))

    for f in faults_on(s, node, "gpu", g):
        t, age = f["type"], now - f["since"]
        if t == "xid79":
            d["present"] = False
            d["events"].append((f["since"], "xid79"))
        elif t in ("xid94", "xid95"):
            d.update(xid=int(t[3:]), dbe=1, remap_unc=1, remap_pending=1)
            d["events"] += [(f["since"], t), (f["since"] + 1, "xid63")]
        elif t == "row-remap-fail":
            d.update(xid=64, dbe=3, remap_unc=7, remap_corr=1, remap_fail=1)
            d["events"] += [(f["since"] - 86400, "xid94"), (f["since"] - 86399, "xid63"), (f["since"], "xid94"),
                            (f["since"] + 1, "xid64")]
        elif t == "sbe-storm":
            d["sbe"] = int(40 * age / 60)
            d["remap_corr"] = min(8, int(age // 1200) + 1)
            if age > 600:
                d["xid"] = 92
                d["events"].append((f["since"] + 600, "xid92"))
        elif t == "thermal":
            d.update(temp=int(jitter(89, 93, node, g, "th")), throttle=d["throttle"] | HW_SLOWDOWN | SW_THERMAL | HW_THERMAL,
                     sm=int(jitter(1080, 1140, node, g, "thc")), power=int(jitter(400, 440, node, g, "thp")))
            d["memtemp"] = d["temp"] + 5
            d["tensor"] *= 0.56
        elif t == "nvlink":
            d["nvlink_crc"] = int(300 * min(age, 1200) / 60 + 25 * max(0, age - 1200) / 60)
            d["nvlink_bw_factor"] = 0.55
            if age > 1200:
                d.update(xid=74, nvlink_inactive=[7])
                d["events"].append((f["since"] + 1200, "xid74"))
        elif t == "pcie-replay":
            d["pcie_replay"] += int(200 * age / 60)
            d["events"].append((f["since"], "aer"))
        elif t == "xid31":
            d["xid"] = 31
            d["events"].append((f["since"], "xid31"))
    return d


def port_state(s, node, p, now):
    """IB HCA mlx5_<p>, port 1."""
    d = dict(state=4, phys=5, rate=400, width="4X", sym=0, downed=int(h("dn", node, p) % 2), recov=0, rcv_err=0,
             xmit_wait=0, events=[])
    crashed = node_crashed(s, node, now)
    base = 36e9                                   # bytes/s per direction while training
    since_slow, factor = None, 1.0
    for f in faults_on(s, node, "ib", p):
        t, age = f["type"], now - f["since"]
        if t == "ib-down":
            d.update(state=1, phys=2, rate=10, width="1X", downed=d["downed"] + 1)
            d["events"].append((f["since"], "ib-down"))
        elif t == "ib-flap":
            n = int(age // 180) + 1
            d["downed"] += n
            d["recov"] += 3 * n
            d["sym"] += 17 * n
            if age % 180 < 12:
                d.update(state=1, phys=2)
            for i in range(min(n, 40)):
                d["events"].append((f["since"] + i * 180, "ib-flap-down"))
                d["events"].append((f["since"] + i * 180 + 12, "ib-flap-up"))
        elif t == "ib-symbol":
            d["sym"] += int(120 * age / 60)
            d["rcv_err"] += int(9 * age / 60)
            since_slow, factor = f["since"], 0.9
        elif t == "ib-degraded":
            d.update(rate=200, width="2X")
            d["events"].append((f["since"], "ib-degraded"))
            since_slow, factor = f["since"], 0.5
        elif t == "ib-congestion":
            d["xmit_wait"] += int(4.2e8 * age / 60)
            since_slow, factor = f["since"], 0.7
    if has(s, node, "thermal") or has(s, node, "power-brake"):
        f = has(s, node, "thermal") or has(s, node, "power-brake")
        since_slow, factor = f["since"], 0.6
    d["xmit_wait"] += int(2.1e6 * (now - T0) / 3600)  # small background wait
    if crashed is not None:
        stop = crashed
        d["tx"] = int(base * (stop - T0))
    elif since_slow is not None:
        d["tx"] = int(base * (since_slow - T0) + base * factor * (now - since_slow))
    else:
        d["tx"] = int(base * (now - T0))
    d["rx"] = int(d["tx"] * 0.998)
    return d


# ---------------------------------------------------------------------------------------------
# Prometheus exporter
# ---------------------------------------------------------------------------------------------
GPU_METRICS = [
    ("DCGM_FI_DEV_GPU_UTIL", "gauge", "GPU utilization (in %).", "util"),
    ("DCGM_FI_DEV_GPU_TEMP", "gauge", "GPU temperature (in C).", "temp"),
    ("DCGM_FI_DEV_MEMORY_TEMP", "gauge", "Memory temperature (in C).", "memtemp"),
    ("DCGM_FI_DEV_POWER_USAGE", "gauge", "Power draw (in W).", "power"),
    ("DCGM_FI_DEV_SM_CLOCK", "gauge", "SM clock frequency (in MHz).", "sm"),
    ("DCGM_FI_DEV_FB_USED", "gauge", "Framebuffer memory used (in MiB).", "fb"),
    ("DCGM_FI_DEV_FB_FREE", "gauge", "Framebuffer memory free (in MiB).", "fbfree"),
    ("DCGM_FI_DEV_XID_ERRORS", "gauge", "Value of the last XID error encountered.", "xid"),
    ("DCGM_FI_DEV_ECC_SBE_VOL_TOTAL", "counter", "Total number of single-bit volatile ECC errors.", "sbe"),
    ("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "counter", "Total number of double-bit volatile ECC errors.", "dbe"),
    ("DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS", "counter", "Number of remapped rows for correctable errors.", "remap_corr"),
    ("DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS", "counter", "Number of remapped rows for uncorrectable errors.", "remap_unc"),
    ("DCGM_FI_DEV_ROW_REMAP_PENDING", "gauge", "Whether a row remap is pending (needs GPU reset).", "remap_pending"),
    ("DCGM_FI_DEV_ROW_REMAP_FAILURE", "gauge", "Whether remapping of rows has failed.", "remap_fail"),
    ("DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL", "counter", "Total number of NVLink flow-control CRC errors.", "nvlink_crc"),
    ("DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL", "counter", "Total number of NVLink bandwidth counters for all lanes.", "nvlink_bw"),
    ("DCGM_FI_DEV_PCIE_REPLAY_COUNTER", "counter", "Total number of PCIe retries.", "pcie_replay"),
    ("DCGM_FI_DEV_CLOCK_THROTTLE_REASONS", "gauge", "Current clock throttle reasons (bitmask).", "throttle"),
    ("DCGM_FI_PROF_PIPE_TENSOR_ACTIVE", "gauge", "Ratio of cycles the tensor (HMMA) pipe is active.", "tensor"),
]
IB_METRICS = [
    ("node_infiniband_state_id", "gauge", "State of the InfiniBand port (1 Down, 2 Init, 3 Armed, 4 Active).", "state"),
    ("node_infiniband_physical_state_id", "gauge", "Physical state (2 Polling, 3 Disabled, 5 LinkUp, 6 LinkErrorRecovery).", "phys"),
    ("node_infiniband_rate_bytes_per_second", "gauge", "Maximum signal transfer rate.", "rate_bps"),
    ("node_infiniband_symbol_error_total", "counter", "Number of minor link errors detected on one or more physical lanes.", "sym"),
    ("node_infiniband_link_downed_total", "counter", "Number of times the link failed to recover and went down.", "downed"),
    ("node_infiniband_link_error_recovery_total", "counter", "Number of times the link successfully recovered from an error.", "recov"),
    ("node_infiniband_port_errors_received_total", "counter", "Number of packets containing an error received.", "rcv_err"),
    ("node_infiniband_port_transmit_wait_total", "counter", "Ticks the port had data to send but no flow-control credits.", "xmit_wait"),
    ("node_infiniband_port_data_transmitted_bytes_total", "counter", "Number of data octets transmitted.", "tx"),
    ("node_infiniband_port_data_received_bytes_total", "counter", "Number of data octets received.", "rx"),
]


def nvlink_counter(s, node, g, now):
    rate, end = 3.6e9, node_crashed(s, node, now) or now
    f = has(s, node, "nvlink", g)
    if f and f["since"] < end:
        return int(rate * (f["since"] - T0) + rate * 0.55 * (end - f["since"]))
    return int(rate * (end - T0))


def render_metrics(s, now=None):
    now = now or time.time()
    rows = {m[0]: [] for m in GPU_METRICS + IB_METRICS}
    for node in NODES:
        pod = pod_of(s, node)
        for g in range(NGPU):
            d = gpu_state(s, node, g, now)
            if not d["present"]:
                continue                      # a GPU that fell off the bus disappears from DCGM
            d["fbfree"] = FB_TOTAL - d["fb"]
            d["nvlink_bw"] = nvlink_counter(s, node, g, now)
            lbl = ('gpu="%d",UUID="%s",pci_bus_id="00000000:%s:00.0",device="nvidia%d",modelName="%s",'
                   'Hostname="%s",DCGM_FI_DRIVER_VERSION="%s"' % (g, uuid(node, g), GPU_BUS[g].upper(), g, GPU_NAME, node, DRIVER))
            if d["busy"]:
                lbl += ',namespace="ml",pod="%s",container="trainer"' % pod
            for name, _, _, key in GPU_METRICS:
                v = d[key]
                rows[name].append("%s{%s} %s" % (name, lbl, ("%.3f" % v) if isinstance(v, float) else v))
        for p in range(NGPU):
            d = port_state(s, node, p, now)
            d["rate_bps"] = int(d["rate"] * 1e9 / 8)
            lbl = 'Hostname="%s",device="mlx5_%d",port="1"' % (node, p)
            for name, _, _, key in IB_METRICS:
                rows[name].append("%s{%s} %d" % (name, lbl, d[key]))
    out = []
    for name, typ, hlp, _ in GPU_METRICS + IB_METRICS:
        out.append("# HELP %s %s" % (name, hlp))
        out.append("# TYPE %s %s" % (name, typ))
        out.extend(rows[name])
    out.append("# HELP gpusim_active_faults Number of injected faults (drill bookkeeping, hide it from learners).")
    out.append("# TYPE gpusim_active_faults gauge")
    out.append("gpusim_active_faults %d" % len(s["faults"]))
    return "\n".join(out) + "\n"


def serve(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/metrics"):
                body, ctype = render_metrics(load_state()).encode(), "text/plain; version=0.0.4"
            elif self.path.startswith("/healthz"):
                body, ctype = b"ok\n", "text/plain"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print("gpusim exporter on :%d (state %s)" % (port, STATE), flush=True)
    HTTPServer(("", port), H).serve_forever()


# ---------------------------------------------------------------------------------------------
# Fake node commands
# ---------------------------------------------------------------------------------------------
def ts(t):
    return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(t))


def cmd_nvidia_smi(s, node, now, args):
    if "-q" in args:
        return nvsmi_q(s, node, now, args)
    if "nvlink" in args:
        return nvsmi_nvlink(s, node, now, args)
    if "-r" in args or "--gpu-reset" in args:
        return ("GPU reset is disabled in this simulator. Real hardware: drain first, then\n"
                "  nvidia-smi -r -i <n>   (needs no processes on the GPU and on NVSwitch systems the fabric manager)\n")
    lines, gpus = [], [gpu_state(s, node, g, now) for g in range(NGPU)]
    for g, d in enumerate(gpus):
        if not d["present"]:
            lines.append("Unable to determine the device handle for GPU0000:%s:00.0: Unknown Error" % GPU_BUS[g].upper())
    if lines:
        lines.append("No devices were found" if all(not d["present"] for d in gpus) else "")
    sep = "+-----------------------------------------+------------------------+----------------------+"
    lines += [ts(now) + "       ",
              "+" + "-" * 89 + "+",
              "|" + (" NVIDIA-SMI %s" % DRIVER).ljust(35) + ("Driver Version: %s" % DRIVER).ljust(31) + ("CUDA Version: %s" % CUDA).ljust(23) + "|",
              sep,
              "| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |",
              "| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |",
              "|                                         |                        |               MIG M. |",
              "|=========================================+========================+======================|"]
    for g, d in enumerate(gpus):
        if not d["present"]:
            continue
        perf = "P0"
        r = "%dW /  700W " % d["power"]
        c1 = (" N/A  %3dC    %s" % (d["temp"], perf)).ljust(41 - len(r)) + r
        lines.append("|" + ("%4d  %s" % (g, GPU_NAME)).ljust(37) + "On  " + "|" + ("   00000000:%s:00.0 Off " % GPU_BUS[g].upper()) +
                     "|" + ("%d " % d["dbe"]).rjust(22) + "|")
        lines.append("|" + c1 + "|" + ("%dMiB /  %dMiB " % (d["fb"], FB_TOTAL)).rjust(24) + "|" + ("%d%%      Default " % d["util"]).rjust(22) + "|")
        lines.append("|" + " " * 41 + "|" + " " * 24 + "|" + "Disabled ".rjust(22) + "|")
        lines.append(sep)
    lines += ["", "+" + "-" * 89 + "+",
              "| Processes:".ljust(90) + "|",
              "|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |",
              "|        ID   ID                                                               Usage      |",
              "|" + "=" * 89 + "|"]
    procs = [(g, d) for g, d in enumerate(gpus) if d["present"] and d["busy"]]
    if not procs:
        lines.append("|  No running processes found".ljust(90) + "|")
    for g, d in procs:
        pid = 48000 + (h("pid", node) % 9000) + g
        lines.append("|" + ("  %3d   N/A  N/A  %14d      C   %-36s" % (g, pid, "/usr/bin/python3")).ljust(78) + ("%dMiB " % (d["fb"] - 4)).rjust(11) + "|")
    lines.append("+" + "-" * 89 + "+")
    return "\n".join(lines) + "\n"


THROTTLE_NAMES = [(GPU_IDLE, "Idle"), (0x2, "Applications Clocks Setting"), (SW_POWER_CAP, "SW Power Cap"),
                  (HW_SLOWDOWN, "HW Slowdown"), (HW_THERMAL, "    HW Thermal Slowdown"),
                  (HW_POWER_BRAKE, "    HW Power Brake Slowdown"), (0x10, "Sync Boost"), (SW_THERMAL, "SW Thermal Slowdown"),
                  (0x100, "Display Clock Setting")]


def kv(k, v, ind=4):
    return " " * ind + k.ljust(42 - ind) + ": " + str(v)


def nvsmi_q(s, node, now, args):
    want = None
    if "-i" in args:
        try:
            want = int(args[args.index("-i") + 1])
        except (IndexError, ValueError):
            return "Invalid combination of input arguments. Please run 'nvidia-smi -h' for help.\n"
    dsel = None
    if "-d" in args and args.index("-d") + 1 < len(args):
        dsel = set(args[args.index("-d") + 1].upper().split(","))
    out = ["", "==============NVSMI LOG==============", "", kv("Timestamp", ts(now), 0), kv("Driver Version", DRIVER, 0),
           kv("CUDA Version", CUDA, 0), "", kv("Attached GPUs", sum(gpu_state(s, node, g, now)["present"] for g in range(NGPU)), 0)]
    for g in range(NGPU):
        if want is not None and g != want:
            continue
        d = gpu_state(s, node, g, now)
        if not d["present"]:
            out.append("Unable to determine the device handle for gpu 0000:%s:00.0: Unknown Error" % GPU_BUS[g].upper())
            continue
        sec = []
        if not dsel:
            sec += [kv("Product Name", GPU_NAME), kv("Product Architecture", "Hopper"), kv("Persistence Mode", "Enabled"),
                    kv("Serial Number", serial(node, g)), kv("GPU UUID", uuid(node, g)), kv("Minor Number", g),
                    kv("VBIOS Version", "96.00.99.00.01")]
        if not dsel or "PCI" in dsel or "PCIE" in dsel:
            sec += ["    PCI", kv("Bus Id", "00000000:%s:00.0" % GPU_BUS[g].upper(), 8),
                    kv("Device Id", "0x233010DE", 8),
                    "        GPU Link Info", kv("PCIe Generation", "", 12), kv("Max", 5, 16), kv("Current", 5, 16),
                    kv("Link Width", "", 12), kv("Max", "16x", 16), kv("Current", "16x", 16),
                    kv("Replays Since Reset", d["pcie_replay"], 8), kv("Replay Number Rollovers", 0, 8)]
        if not dsel or "PERFORMANCE" in dsel:
            sec.append("    Clocks Event Reasons")
            for bit, name in THROTTLE_NAMES:
                sec.append(kv(name.strip() if not name.startswith("    ") else name.strip(), "Active" if d["throttle"] & bit else "Not Active",
                              12 if name.startswith("    ") else 8))
        if not dsel or "MEMORY" in dsel:
            sec += ["    FB Memory Usage", kv("Total", "%d MiB" % FB_TOTAL, 8), kv("Reserved", "551 MiB", 8),
                    kv("Used", "%d MiB" % d["fb"], 8), kv("Free", "%d MiB" % (FB_TOTAL - 551 - d["fb"]), 8)]
        if not dsel or "UTILIZATION" in dsel:
            sec += ["    Utilization", kv("Gpu", "%d %%" % d["util"], 8), kv("Memory", "%d %%" % (d["util"] * 0.6), 8)]
        if not dsel or "ECC" in dsel:
            agg_dbe = d["dbe"] + (2 if has(s, node, "row-remap-fail", g) else 0)
            sec += ["    ECC Mode", kv("Current", "Enabled", 8), kv("Pending", "Enabled", 8),
                    "    ECC Errors", "        Volatile",
                    kv("SRAM Correctable", 0, 12), kv("SRAM Uncorrectable Parity", 0, 12), kv("SRAM Uncorrectable SEC-DED", 0, 12),
                    kv("DRAM Correctable", d["sbe"], 12), kv("DRAM Uncorrectable", d["dbe"], 12),
                    "        Aggregate",
                    kv("SRAM Correctable", 0, 12), kv("SRAM Uncorrectable Parity", 0, 12), kv("SRAM Uncorrectable SEC-DED", 0, 12),
                    kv("DRAM Correctable", d["sbe"] + h("aggsbe", node, g) % 4, 12), kv("DRAM Uncorrectable", agg_dbe, 12),
                    kv("SRAM Threshold Exceeded", "No", 12)]
        if not dsel or "ROW_REMAPPER" in dsel:
            sec += ["    Remapped Rows", kv("Correctable Error", d["remap_corr"], 8), kv("Uncorrectable Error", d["remap_unc"], 8),
                    kv("Pending", "Yes" if d["remap_pending"] else "No", 8),
                    kv("Remapping Failure Occurred", "Yes" if d["remap_fail"] else "No", 8),
                    "        Bank Remap Availability Histogram",
                    kv("Max", 0 if d["remap_fail"] else 640 - d["remap_unc"] - d["remap_corr"] + 2 * 0, 12),
                    kv("High", 0, 12), kv("Partial", 1 if d["remap_fail"] else 0, 12), kv("Low", 0, 12),
                    kv("None", 1 if d["remap_fail"] else 0, 12)]
        if not dsel or "TEMPERATURE" in dsel:
            sec += ["    Temperature", kv("GPU Current Temp", "%d C" % d["temp"], 8), kv("GPU T.Limit Temp", "%d C" % (87 - d["temp"]), 8),
                    kv("GPU Shutdown Temp", "92 C", 8), kv("GPU Slowdown Temp", "87 C", 8),
                    kv("GPU Max Operating Temp", "85 C", 8), kv("Memory Current Temp", "%d C" % d["memtemp"], 8),
                    kv("Memory Max Operating Temp", "95 C", 8)]
        if not dsel or "POWER" in dsel:
            sec += ["    GPU Power Readings", kv("Average Power Draw", "%.2f W" % (d["power"] - 3.1), 8),
                    kv("Instantaneous Power Draw", "%.2f W" % d["power"], 8), kv("Current Power Limit", "700.00 W", 8),
                    kv("Default Power Limit", "700.00 W", 8), kv("Max Power Limit", "700.00 W", 8)]
        if not dsel or "CLOCK" in dsel:
            sec += ["    Clocks", kv("Graphics", "%d MHz" % d["sm"], 8), kv("SM", "%d MHz" % d["sm"], 8),
                    kv("Memory", "2619 MHz", 8), kv("Video", "%d MHz" % int(d["sm"] * 0.88), 8),
                    "    Max Clocks", kv("SM", "1980 MHz", 8)]
        out += ["", "GPU 00000000:%s:00.0" % GPU_BUS[g].upper()] + sec
    return "\n".join(out) + "\n"


def nvsmi_nvlink(s, node, now, args):
    out = []
    for g in range(NGPU):
        d = gpu_state(s, node, g, now)
        if not d["present"]:
            out.append("Unable to determine the device handle for GPU0000:%s:00.0: Unknown Error" % GPU_BUS[g].upper())
            continue
        out.append("GPU %d: %s (UUID: %s)" % (g, GPU_NAME, uuid(node, g)))
        for l in range(NVLINKS):
            if "-e" in args:
                crc = d["nvlink_crc"] if l == 7 else 0
                out += ["\t Link %d: Replay Errors: %d" % (l, crc // 9), "\t Link %d: Recovery Errors: %d" % (l, crc // 120),
                        "\t Link %d: CRC Errors: %d" % (l, crc)]
            else:
                out.append("\t Link %d: %s" % (l, "<inactive>" if l in d["nvlink_inactive"] else "26.562 GB/s"))
    return "\n".join(out) + "\n"


def cmd_lspci(s, node, now, args):
    out = []
    for g in range(NGPU):
        d = gpu_state(s, node, g, now)
        out.append("%s:00.0 3D controller: NVIDIA Corporation GH100 [H100 SXM5 80GB] (rev %s)" % (GPU_BUS[g], "a1" if d["present"] else "ff"))
    for p in range(NGPU):
        out.append("%s:00.0 Infiniband controller: Mellanox Technologies MT2910 Family [ConnectX-7]" % HCA_BUS[p])
    return "\n".join(sorted(out)) + "\n"


def dmesg_lines(s, node, now):
    boot = boot_time(node)
    ev = []
    for g in range(NGPU):
        for t, e in gpu_state(s, node, g, now)["events"]:
            ev.append((t, e, g))
    for p in range(NGPU):
        for t, e in port_state(s, node, p, now)["events"]:
            ev.append((t, e, p))
    pid, pname = 48000 + (h("pid", node) % 9000), "python3"
    base = [(boot + 4.1, "NVRM: loading NVIDIA UNIX Open Kernel Module for x86_64  %s  Release Build" % DRIVER),
            (boot + 6.3, "nvidia-nvswitch: Probing device 0000:05:00.0, Vendor Id = 0x10de, Device Id = 0x22a3"),
            (boot + 9.0, "mlx5_core 0000:%s:00.0: firmware version: %s" % (HCA_BUS[0], CX7_FW)),
            (boot + 11.2, "mlx5_core 0000:%s:00.0: Port module event: module 0, Cable plugged" % HCA_BUS[0])]
    out = list(base)
    for t, e, i in sorted(ev):
        if t > now:
            continue
        b, hb = GPU_BUS[i], HCA_BUS[i]
        x = "NVRM: Xid (PCI:0000:%s:00): " % b
        if e == "xid79":
            out += [(t, x + "79, pid='<unknown>', name=<unknown>, GPU has fallen off the bus."),
                    (t + .01, "NVRM: GPU 0000:%s:00.0: GPU has fallen off the bus." % b),
                    (t + .02, "NVRM: GPU 0000:%s:00.0: GPU serial number is %s." % (b, serial(node, i))),
                    (t + .5, "NVRM: A GPU crash dump has been created. If possible, please run"),
                    (t + .51, "NVRM: nvidia-bug-report.sh as root to collect this data before"),
                    (t + .52, "NVRM: the NVIDIA kernel module is unloaded.")]
        elif e == "xid94":
            out.append((t, x + "94, pid=%d, name=%s, Contained: FBHUB (0x0). RST: No, D-RST: No" % (pid + i, pname)))
        elif e == "xid95":
            out.append((t, x + "95, pid=%d, name=%s, Uncontained: FBHUB. RST: Yes, D-RST: No" % (pid + i, pname)))
        elif e == "xid63":
            out.append((t, x + "63, pid='<unknown>', name=<unknown>, Row Remapper: New row (0x%08x) marked for remapping, "
                               "reset gpu to activate." % (h("row", node, i, t) % (1 << 28))))
        elif e == "xid64":
            out.append((t, x + "64, pid='<unknown>', name=<unknown>, Row Remapper Error: Failed to remap row (0x%08x): "
                               "no spare rows left in bank." % (h("row", node, i) % (1 << 28))))
        elif e == "xid92":
            out.append((t, x + "92, pid='<unknown>', name=<unknown>, High single-bit ECC error rate"))
        elif e == "xid74":
            out += [(t, x + "74, pid=%d, name=%s, NVLink: fatal error detected on link 7(0x0, 0x0, 0x10000, 0x0, 0x0, 0x0, 0x0)" % (pid + i, pname)),
                    (t + .2, x + "45, pid=%d, name=%s, Ch 00000008" % (pid + i, pname))]
        elif e == "xid31":
            out += [(t, x + "31, pid=%d, name=%s, Ch 00000018, intr 00000000. MMU Fault: ENGINE GRAPHICS GPCCLIENT_T1_3 "
                            "faulted @ 0x7f3a_5c210000. Fault is of type FAULT_PDE ACCESS_TYPE_VIRT_READ" % (pid + i, pname)),
                    (t + .1, x + "43, pid=%d, name=%s, Ch 00000018" % (pid + i, pname))]
        elif e == "aer":
            rp = "%02x" % (int(b, 16) - 3)
            for k in range(3):
                tt = t + 7 + k * 41.3
                if tt > now:
                    break
                out += [(tt, "pcieport 0000:%s:01.0: AER: Corrected error message received from 0000:%s:00.0" % (rp, b)),
                        (tt + .001, "nvidia 0000:%s:00.0: PCIe Bus Error: severity=Corrected, type=Physical Layer, (Receiver ID)" % b),
                        (tt + .002, "nvidia 0000:%s:00.0:   device [10de:2330] error status/mask=00000001/0000e000" % b),
                        (tt + .003, "nvidia 0000:%s:00.0:    [ 0] RxErr                  (First)" % b)]
        elif e == "pb":
            if i == 0:
                out.append((t, "NVRM: GPU at PCI:0000:%s:00: HW power brake slowdown asserted by external signal" % b))
        elif e == "ib-down":
            out += [(t, "mlx5_core 0000:%s:00.0: Port module event: module %d, Cable unplugged" % (hb, i)),
                    (t + .4, "mlx5_core 0000:%s:00.0: mlx5_ib_event: port 1 state DOWN" % hb)]
        elif e == "ib-flap-down":
            out.append((t, "mlx5_core 0000:%s:00.0: mlx5_ib_event: port 1 state DOWN" % hb))
        elif e == "ib-flap-up":
            out.append((t, "mlx5_core 0000:%s:00.0: mlx5_ib_event: port 1 state ACTIVE" % hb))
        elif e == "ib-degraded":
            out.append((t, "mlx5_core 0000:%s:00.0: Port module event: module %d, link width degraded to 2X" % (hb, i)))
    out.sort()
    return [(t - boot, m) for t, m in out]


def cmd_dmesg(s, node, now, args):
    human = "-T" in args
    lines = []
    for rel, m in dmesg_lines(s, node, now):
        if human:
            lines.append("[%s] %s" % (ts(boot_time(node) + rel), m))
        else:
            lines.append("[%12.6f] %s" % (rel, m))
    return "\n".join(lines) + "\n"


def cmd_dcgmi(s, node, now, args):
    if "diag" not in args:
        return "Usage: dcgmi diag -r <1|2|3>   (other dcgmi subcommands are not simulated)\n"
    lvl = "1"
    if "-r" in args and args.index("-r") + 1 < len(args):
        lvl = args[args.index("-r") + 1]
    gs = [gpu_state(s, node, g, now) for g in range(NGPU)]
    if any(d["busy"] for d in gs):
        return ("Error: Unable to complete diagnostic for group 0. Return: The GPU is busy with other processes (-31).\n"
                "Hint: drain the node first (kubectl drain), then rerun.\n")
    present = [g for g in range(NGPU) if gs[g]["present"]]

    def row(k, v):
        return "| %-26s| %-47s|" % (k, v)

    def fails(cond, msg):
        bad = [g for g in present if cond(gs[g])]
        if not bad:
            return ["Pass - All"]
        out = ["Fail - GPUs: %s" % ",".join(map(str, bad))]
        for g in bad:
            out.append("Warning: GPU %d: %s" % (g, msg))
        return out

    lines = ["Successfully ran diagnostic for group." if len(present) == NGPU else
             "Warning: %d of 8 GPUs are not visible to DCGM; diagnostic ran on GPUs %s" % (NGPU - len(present), ",".join(map(str, present))),
             "+---------------------------+------------------------------------------------+",
             "| Diagnostic                | Result                                         |",
             "+===========================+================================================+",
             "|-----  Metadata  ----------+------------------------------------------------|",
             row("DCGM Version", DCGM), row("Driver Version Detected", DRIVER),
             row("GPU Device IDs Detected", ",".join("2330" for _ in present)),
             "|-----  Deployment  --------+------------------------------------------------|"]
    rr = fails(lambda d: d["remap_pending"] or d["remap_fail"], "row remap pending or failed; reset or RMA")
    for k, v in [("Denylist", "Pass"), ("NVML Library", "Pass"), ("CUDA Main Library", "Pass"),
                 ("Permissions and OS Blocks", "Pass"), ("Persistence Mode", "Pass"), ("Environment Variables", "Pass"),
                 ("Page Retirement/Row Remap", rr[0]), ("Graphics Processes", "Pass"), ("Inforom", "Pass")]:
        lines.append(row(k, v))
    if lvl in ("2", "3", "4"):
        lines.append("+-----  Integration  -------+------------------------------------------------+")
        lines.append(row("PCIe", fails(lambda d: d["pcie_replay"] > 1000, "PCIe replay count exceeded threshold")[0]))
        lines.append("+-----  Hardware  ----------+------------------------------------------------+")
        mem = fails(lambda d: d["remap_fail"] or d["dbe"] > 0, "uncorrectable memory errors detected")
        lines.append(row("GPU Memory", mem[0]))
        lines.append(row("NVLink", fails(lambda d: d["nvlink_crc"] > 100, "NVLink CRC error threshold exceeded on link 7")[0]))
    if lvl in ("3", "4"):
        lines.append("+-----  Stress  ------------+------------------------------------------------+")
        lines.append(row("Targeted Stress", fails(lambda d: d["throttle"] & (HW_THERMAL | HW_POWER_BRAKE), "clocks throttled during stress (thermal / power brake)")[0]))
        lines.append(row("Targeted Power", fails(lambda d: d["throttle"] & (HW_THERMAL | HW_POWER_BRAKE), "could not reach target power")[0]))
        lines.append(row("Memory Bandwidth", "Pass - All"))
        lines.append(row("Diagnostic", fails(lambda d: d["dbe"] > 0 or d["remap_fail"], "ECC errors during stress")[0]))
    lines.append("+---------------------------+------------------------------------------------+")
    warns = []
    for msgs in (rr,):
        warns += msgs[1:]
    if lvl in ("2", "3", "4"):
        warns += mem[1:]
    if warns:
        lines.append("")
        lines += warns
    return "\n".join(lines) + "\n"


IB_STATE = {1: "Down", 2: "Initializing", 3: "Armed", 4: "Active"}
IB_PHYS = {2: "Polling", 3: "Disabled", 5: "LinkUp", 6: "LinkErrorRecovery"}


def cmd_ibstat(s, node, now, args):
    devs = [a for a in args if a.startswith("mlx5_")]
    out = []
    for p in range(NGPU):
        if devs and "mlx5_%d" % p not in devs:
            continue
        d = port_state(s, node, p, now)
        out += ["CA 'mlx5_%d'" % p, "\tCA type: MT4129", "\tNumber of ports: 1", "\tFirmware version: %s" % CX7_FW,
                "\tHardware version: 0", "\tNode GUID: %s" % guid(node, p), "\tSystem image GUID: %s" % guid(node, p),
                "\tPort 1:", "\t\tState: %s" % IB_STATE[d["state"]], "\t\tPhysical state: %s" % IB_PHYS[d["phys"]],
                "\t\tRate: %d" % d["rate"], "\t\tBase lid: %d" % (lid(node, p) if d["state"] == 4 else 65535),
                "\t\tLMC: 0", "\t\tSM lid: 1", "\t\tCapability mask: 0xa751e848", "\t\tPort GUID: %s" % guid(node, p),
                "\t\tLink layer: InfiniBand"]
    return "\n".join(out) + "\n" if out else "ibpanic: [%d] main: '%s' IB device can't be found\n" % (os.getpid(), devs[0])


def cmd_ibdev2netdev(s, node, now, args):
    return "".join("mlx5_%d port 1 ==> ibp%ds0 (%s)\n" % (p, int(HCA_BUS[p], 16), "Up" if port_state(s, node, p, now)["state"] == 4 else "Down")
                   for p in range(NGPU))


def dots(k, v):
    return (k + ":").ljust(33, ".") + str(v)


def cmd_perfquery(s, node, now, args):
    dev = "mlx5_0"
    if "-C" in args and args.index("-C") + 1 < len(args):
        dev = args[args.index("-C") + 1]
    try:
        p = int(dev.split("_")[1])
        assert 0 <= p < NGPU
    except (IndexError, ValueError, AssertionError):
        return "ibwarn: [%d] mad_rpc_open_port: can't open UMAD port (%s:1)\n" % (os.getpid(), dev)
    d = port_state(s, node, p, now)
    if d["state"] != 4:
        return "ibwarn: [%d] _do_madrpc: send failed; Invalid argument\nibpanic: [%d] main: can't resolve self port %s\n" % (os.getpid(), os.getpid(), dev)
    cap = "0x5A00"
    if "-x" in args:
        return "\n".join(["# Port extended counters: Lid %d port 1 (CapMask: %s)" % (lid(node, p), cap), dots("PortSelect", 1),
                          dots("CounterSelect", "0x0000"), dots("PortXmitData", d["tx"] // 4), dots("PortRcvData", d["rx"] // 4),
                          dots("PortXmitPkts", d["tx"] // 4096), dots("PortRcvPkts", d["rx"] // 4096),
                          dots("PortUnicastXmitPkts", d["tx"] // 4096), dots("PortUnicastRcvPkts", d["rx"] // 4096),
                          dots("PortMulticastXmitPkts", 0), dots("PortMulticastRcvPkts", 0)]) + "\n"
    c16 = lambda v: min(v, 65535)
    c8 = lambda v: min(v, 255)
    return "\n".join(["# Port counters: Lid %d port 1 (CapMask: %s)" % (lid(node, p), cap), dots("PortSelect", 1),
                      dots("CounterSelect", "0x0000"), dots("SymbolErrorCounter", c16(d["sym"])),
                      dots("LinkErrorRecoveryCounter", c8(d["recov"])), dots("LinkDownedCounter", c8(d["downed"])),
                      dots("PortRcvErrors", c16(d["rcv_err"])), dots("PortRcvRemotePhysicalErrors", 0),
                      dots("PortRcvSwitchRelayErrors", 0), dots("PortXmitDiscards", 0), dots("PortXmitConstraintErrors", 0),
                      dots("PortRcvConstraintErrors", 0), dots("CounterSelect2", "0x00"),
                      dots("LocalLinkIntegrityErrors", 0), dots("ExcessiveBufferOverrunErrors", 0), dots("VL15Dropped", 0),
                      dots("PortXmitData", min(d["tx"] // 4, 4294967295)), dots("PortRcvData", min(d["rx"] // 4, 4294967295)),
                      dots("PortXmitPkts", 4294967295), dots("PortRcvPkts", 4294967295),
                      dots("PortXmitWait", min(d["xmit_wait"], 4294967295))]) + "\n"


def cmd_ibdiagnet(s, node, now, args):
    errs, warns, width = [], [], []
    for n in NODES:
        for p in range(NGPU):
            d = port_state(s, n, p, now)
            name = '"%s mlx5_%d"' % (n, p)
            leaf = "leaf-%02d" % (p + 1 + (0 if n in NODES[:4] else 8))
            if d["state"] != 4:
                errs.append("-E- Link is down: %s port 1 <-> %s/P%d (phys state %s)" % (name, leaf, NODES.index(n) + 1, IB_PHYS[d["phys"]]))
            if d["width"] != "4X" and d["state"] == 4:
                width.append("-W- Link width is not optimal: %s port 1 <-> %s/P%d  Active=%s Supported=4X (speed NDR)"
                             % (name, leaf, NODES.index(n) + 1, d["width"]))
            if d["sym"] > 200:
                warns.append("-W- lid=0x%04x guid=%s dev=4129 Port=1 %s  Performance counter: symbol_error_counter Value=%d (threshold=200)"
                             % (lid(n, p), guid(n, p), name, min(d["sym"], 65535)))
            if d["downed"] > 3:
                warns.append("-W- lid=0x%04x guid=%s dev=4129 Port=1 %s  Performance counter: link_downed_counter Value=%d (threshold=3)"
                             % (lid(n, p), guid(n, p), name, min(d["downed"], 255)))
            if d["rcv_err"] > 50:
                warns.append("-W- lid=0x%04x guid=%s dev=4129 Port=1 %s  Performance counter: port_rcv_errors Value=%d (threshold=50)"
                             % (lid(n, p), guid(n, p), name, d["rcv_err"]))
    n_ports = len(NODES) * NGPU
    up = n_ports - len(errs)
    out = ["Loading IBDIAG from: /usr/lib/x86_64-linux-gnu/ibdiag2.1", "-I- Using port 1 as the local port",
           "-I- Discovering ... %d nodes (16 Switches & %d CAs) discovered." % (16 + n_ports, n_ports), "",
           "-I- Fabric Discover finished", "-I- Duplicated GUIDs detection finished successfully", "",
           "-I- Links Check"] + (errs or ["-I- Links Check finished successfully"]) + [""] + \
          ["-I- Links Speed/Width Check"] + (width or ["-I- Links Speed/Width Check finished successfully"]) + [""] + \
          ["-I- PM Counters Check (thresholds from ibdiag config)"] + (warns or ["-I- PM Counters Check finished successfully"]) + [""] + \
          ["-I- Stage                     Warnings   Errors     Comment",
           "-I- Discovery                 0          0",
           "-I- Lids Check                0          0",
           "-I- Links Check               0          %-10d" % len(errs),
           "-I- Links Speed/Width Check   %-10d 0" % len(width),
           "-I- PM Counters               %-10d 0" % len(warns),
           "-I- Routing                   0          0", "",
           "-I- CA ports: %d/%d Active" % (up, n_ports),
           "-I- Note: PortXmitWait (congestion) is not an error and is not checked here; read it with perfquery."]
    return "\n".join(out) + "\n"


def busbw(s, now):
    """Estimate cluster all-reduce bus bandwidth (GB/s) from the current faults."""
    for n in NODES:
        if node_crashed(s, n, now) is not None:
            return None, n
    bw = 192.0
    for n in NODES:
        for p in range(NGPU):
            d = port_state(s, n, p, now)
            if d["width"] == "2X":
                bw = min(bw, 96.0)
            if d["sym"] > 0 and d["state"] == 4 and not has(s, n, "ib-flap"):
                bw = min(bw, 172.0)
        if has(s, n, "ib-congestion"):
            bw = min(bw, 131.0)
        if has(s, n, "power-brake"):
            bw = min(bw, 118.0)
        for g in range(NGPU):
            d = gpu_state(s, n, g, now)
            if d["throttle"] & HW_THERMAL:
                bw = min(bw, 141.0)
            if d["nvlink_crc"]:
                bw = min(bw, 104.0)
    return bw, None


def cmd_nccl(s, node, now, args):
    bw, bad = busbw(s, now)
    head = ["# nThread 1 nGpus 1 minBytes 8 maxBytes 8589934592 step: 2(factor) warmup iters: 5 iters: 20 agg iters: 1 validation: 1 graph: 0",
            "#", "# Using devices"]
    for i, n in enumerate(NODES):
        for g in range(NGPU):
            head.append("#  Rank %2d Group  0 Pid %6d on %8s device  %d [0x%s] %s" % (i * 8 + g, 48000 + g, n, g, GPU_BUS[g], GPU_NAME))
    if bad:
        culprits = [f for f in faults_on(s, bad) if FAULTS[f["type"]]["crash"] or f["type"] == "nvlink"]
        if not culprits:
            return "\n".join(head[:2] + ["%s: no llm-train/nccl rank can start: node has no GPUs allocated (cordoned or drained?)" % bad,
                                         "Only 56 of 64 ranks joined; mpirun aborted."]) + "\n"
        crashed_by = culprits[0]
        t = crashed_by["type"]
        r = NODES.index(bad) * 8 + crashed_by.get("target", 0)
        if t.startswith("ib"):
            msg = ["%s:%d:%d [%d] transport/net_ib.cc:1696 NCCL WARN NET/IB : Got completion from peer 10.0.%d.%d<46321> with "
                   "status=12 opcode=0 len=0 vendor err 129 (Recv) localGid ::ffff:10.0.%d.%d hca mlx5_%d"
                   % (bad, 48000, 48000 + crashed_by["target"], crashed_by["target"], NODES.index(bad) + 1, 20 + crashed_by["target"],
                      NODES.index(bad) + 1, 10 + crashed_by["target"], crashed_by["target"]),
                   "%s: Test NCCL failure common.cu:1005 'remote process exited or there was a network error / '" % bad,
                   " .. %s pid %d: Test failure common.cu:891" % (bad, 48000)]
        elif t == "xid79":
            msg = ["%s: Test CUDA failure common.cu:1041 'unspecified launch failure'" % bad,
                   "%s: CUDA driver error: device %d not found (only 7 visible devices; rank %d cannot start)" % (bad, crashed_by["target"], r)]
        else:
            msg = ["%s: Test CUDA failure common.cu:1041 '%s'" % (bad, {"xid94": "uncorrectable ECC error encountered",
                   "xid95": "uncorrectable ECC error encountered", "row-remap-fail": "uncorrectable ECC error encountered",
                   "xid31": "an illegal memory access was encountered", "nvlink": "unspecified launch failure"}.get(t, "unknown error")),
                   " .. %s pid %d: Test failure common.cu:891" % (bad, 48000)]
        return "\n".join(head + ["#"] + msg + ["-------------------------------------------------------",
                                              "Primary job terminated normally, but 1 process returned a non-zero exit code."]) + "\n"
    rows = ["#", "#                                                              out-of-place                       in-place",
            "#       size         count      type   redop    root     time   algbw   busbw #wrong     time   algbw   busbw #wrong",
            "#        (B)    (elements)                               (us)  (GB/s)  (GB/s)            (us)  (GB/s)  (GB/s)"]
    size = 1 << 20
    while size <= 8 << 30:
        frac = min(1.0, (size / float(256 << 20)) ** 0.55)
        b = max(0.4, bw * frac * jitter(0.97, 1.0, "nccl", size))
        alg = b / (2 * 63 / 64.0)
        t_us = size / (alg * 1e3)
        b2 = b * jitter(0.99, 1.01, "nccl2", size)
        rows.append("%12d  %12d     float     sum      -1  %7.1f  %6.2f  %6.2f      0  %7.1f  %6.2f  %6.2f      0"
                    % (size, size // 4, t_us, alg, b, t_us / (b2 / b), alg * b2 / b, b2))
        size *= 4
    rows += ["# Out of bounds values : 0 OK", "# Avg bus bandwidth    : %.3f " % (bw * 0.71), "#",
             "# (acceptance baseline for this cluster at 8 GB: ~190 GB/s busbw)"]
    return "\n".join(head + rows) + "\n"


HELP = """Simulated commands on {node}:
  nvidia-smi [-q [-i N] [-d ECC|ROW_REMAPPER|TEMPERATURE|PERFORMANCE|POWER|CLOCK|PCI|MEMORY]]
  nvidia-smi nvlink -s | -e
  dcgmi diag -r 1|2|3          (only on a drained node, as on real hardware)
  dmesg [-T]                   lspci
  ibstat [mlx5_N]              ibdev2netdev
  perfquery [-x] -C mlx5_N -P 1
  ibdiagnet                    (fabric-wide)
  all_reduce_perf              (nccl-tests across all 64 GPUs)
  hostname | uptime | help | exit       pipes: | grep [-i] [-v] [-E] PAT | head [-n N] | tail [-n N] | wc -l
"""


def run_one(s, node, line, now=None):
    now = now or time.time()
    lex = shlex.shlex(line, posix=True, punctuation_chars="|")
    lex.whitespace_split = True
    stages, cur = [], []
    for tok in lex:
        if tok == "|":
            stages.append(cur)
            cur = []
        else:
            cur.append(tok)
    stages.append(cur)
    if not stages[0]:
        return ""
    cmd, args = stages[0][0], stages[0][1:]
    if cmd in ("sudo",) and args:
        cmd, args = args[0], args[1:]
    if "all_reduce_perf" in line:
        out = cmd_nccl(s, node, now, args)
    elif cmd == "nvidia-smi":
        out = cmd_nvidia_smi(s, node, now, args)
    elif cmd == "dmesg":
        out = cmd_dmesg(s, node, now, args)
    elif cmd == "lspci":
        out = cmd_lspci(s, node, now, args)
    elif cmd == "dcgmi":
        out = cmd_dcgmi(s, node, now, args)
    elif cmd == "ibstat":
        out = cmd_ibstat(s, node, now, args)
    elif cmd == "ibdev2netdev":
        out = cmd_ibdev2netdev(s, node, now, args)
    elif cmd == "perfquery":
        out = cmd_perfquery(s, node, now, args)
    elif cmd == "ibdiagnet":
        out = cmd_ibdiagnet(s, node, now, args)
    elif cmd == "hostname":
        out = node + "\n"
    elif cmd == "uptime":
        up = now - boot_time(node)
        out = " %s up %d days, %d:%02d,  1 user,  load average: 42.17, 41.80, 41.02\n" % (
            time.strftime("%H:%M:%S"), up // 86400, (up % 86400) // 3600, (up % 3600) // 60)
    elif cmd == "help":
        out = HELP.format(node=node)
    else:
        return "%s: command not simulated here (try 'help')\n" % cmd
    for st in stages[1:]:
        out = pipe(out, st)
    return out


def pipe(text, st):
    import re
    if not st:
        return text
    lines = text.rstrip("\n").split("\n")
    c, a = st[0], st[1:]
    if c == "grep":
        flags = [x for x in a if x.startswith("-")]
        pats = [x for x in a if not x.startswith("-")]
        if not pats:
            return text
        rx = re.compile(pats[0].replace("\\|", "|"), re.I if "-i" in flags else 0)
        keep = [l for l in lines if bool(rx.search(l)) != ("-v" in flags)]
        if "-c" in flags:
            return "%d\n" % len(keep)
        lines = keep
    elif c in ("head", "tail"):
        n = 10
        for i, x in enumerate(a):
            if x == "-n" and i + 1 < len(a):
                n = int(a[i + 1])
            elif x.startswith("-") and x[1:].isdigit():
                n = int(x[1:])
        lines = lines[:n] if c == "head" else lines[-n:]
    elif c == "wc" and "-l" in a:
        return "%d\n" % len(lines)
    else:
        return "%s: not supported in this simulated shell\n" % c
    return "\n".join(lines) + ("\n" if lines else "")


def shell(node):
    if node not in NODES:
        sys.exit("unknown node %s (nodes: %s)" % (node, " ".join(NODES)))
    try:
        import readline  # noqa: F401  (history / line editing)
    except ImportError:
        pass
    print("Simulated shell on %s (8x %s, 8x ConnectX-7 NDR). Type 'help'. Ctrl-D to leave." % (node, GPU_NAME))
    while True:
        try:
            line = input("root@%s:~# " % node).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line in ("exit", "logout"):
            return
        if line:
            try:
                sys.stdout.write(run_one(load_state(), node, line))
            except Exception as e:  # keep the shell alive on parse errors
                print("error: %s" % e)


# ---------------------------------------------------------------------------------------------
# Fault management + lab.sh helpers
# ---------------------------------------------------------------------------------------------
def fmt(f, text):
    t = f.get("target", 0)
    return text.format(node=f["node"], t=t, bus="0000:%s:00.0" % GPU_BUS[t], hca="mlx5_%d" % t,
                       pod=pod_of(load_state(), f["node"]))


def add_fault(s, ftype, node, target=None, since=None):
    if ftype not in FAULTS:
        sys.exit("unknown fault '%s'. Run: faults" % ftype)
    if node not in NODES:
        sys.exit("unknown node '%s' (nodes: %s)" % (node, " ".join(NODES)))
    f = dict(type=ftype, node=node, since=int(since or time.time()))
    if FAULTS[ftype]["kind"] != "node":
        f["target"] = int(target) if target is not None else random.randrange(NGPU)
    s["faults"].append(f)
    return f


def status_patch(s, node, now=None):
    now = now or time.time()
    gpus = sum(1 for g in range(NGPU) if gpu_state(s, node, g, now)["present"])
    ibs = sum(1 for p in range(NGPU) if port_state(s, node, p, now)["state"] == 4 or has(s, node, "ib-flap", p))
    cap = {"cpu": "224", "memory": "2113412748Ki", "ephemeral-storage": "30Ti", "pods": "110",
           "nvidia.com/gpu": "8", "rdma/ib": "8", "hugepages-1Gi": "0", "hugepages-2Mi": "0"}
    alloc = dict(cap, **{"cpu": "220", "memory": "2097152000Ki", "nvidia.com/gpu": str(gpus), "rdma/ib": str(ibs)})
    return {"status": {"capacity": cap, "allocatable": alloc,
                       "nodeInfo": {"architecture": "amd64", "operatingSystem": "linux", "osImage": "Ubuntu 24.04.3 LTS",
                                    "kernelVersion": "6.8.0-87-generic", "containerRuntimeVersion": "containerd://1.7.28",
                                    "kubeletVersion": "v1.33.5+k3s1", "kubeProxyVersion": "", "bootID": "", "machineID": "",
                                    "systemUUID": "%08x-0000-4000-8000-%012x" % (h("sys", node) % (1 << 32), h("sys2", node) % (1 << 48))}}}


def node_manifests():
    items = []
    for i, n in enumerate(NODES):
        items.append({"apiVersion": "v1", "kind": "Node",
                      "metadata": {"name": n,
                                   "annotations": {"kwok.x-k8s.io/node": "fake", "node.alpha.kubernetes.io/ttl": "0"},
                                   "labels": {"type": "kwok", "kubernetes.io/hostname": n, "kubernetes.io/os": "linux",
                                              "kubernetes.io/arch": "amd64", "node-role.kubernetes.io/gpu": "",
                                              "node.kubernetes.io/instance-type": "h100-8x-sxm5",
                                              "topology.kubernetes.io/zone": "rack-a" if i < 4 else "rack-b",
                                              "nvidia.com/gpu.present": "true", "nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3",
                                              "nvidia.com/gpu.count": "8", "nvidia.com/gpu.memory": str(FB_TOTAL),
                                              "nvidia.com/cuda.driver.major": DRIVER.split(".")[0],
                                              "feature.node.kubernetes.io/rdma.capable": "true"}},
                      "spec": {"taints": [{"key": "kwok.x-k8s.io/node", "value": "fake", "effect": "NoSchedule"},
                                          {"key": "nvidia.com/gpu", "value": "present", "effect": "NoSchedule"}]}})
    return {"apiVersion": "v1", "kind": "List", "items": items}


def fixtures(outdir):
    """Write every fault's outputs as plain text, for study without a cluster."""
    now = time.time()
    cases = [("00-healthy", None)] + [("%02d-%s" % (i + 1, k), k) for i, k in enumerate(FAULTS)]
    for name, ftype in cases:
        s = {"faults": [], "hidden": False, "pods": None}
        node, target = "h100-03", 5
        if ftype:
            add_fault(s, ftype, node, target, since=now - 1500)     # 25 min in (past the nvlink escalation)
        d = os.path.join(outdir, name)
        os.makedirs(d, exist_ok=True)
        cmds = [("nvidia-smi.txt", "nvidia-smi"), ("nvidia-smi-q-gpu%d.txt" % target, "nvidia-smi -q -i %d" % target),
                ("nvidia-smi-nvlink-e.txt", "nvidia-smi nvlink -e -i %d" % target), ("dmesg.txt", "dmesg -T"), ("lspci.txt", "lspci"),
                ("ibstat.txt", "ibstat"), ("perfquery-mlx5_%d.txt" % target, "perfquery -C mlx5_%d -P 1" % target),
                ("ibdiagnet.txt", "ibdiagnet"), ("nccl-all_reduce_perf.txt", "all_reduce_perf -b 1M -e 8G -f 4 -g 1")]
        for fn, c in cmds:
            with open(os.path.join(d, fn), "w") as fh:
                fh.write("# root@%s:~# %s\n" % (node, c) + run_one(s, node, c, now))
        drained = dict(s, pods={n: pod_of(s, n) for n in NODES if n != node})   # dcgmi needs a drained node
        with open(os.path.join(d, "dcgmi-diag-r3.txt"), "w") as fh:
            fh.write("# root@%s:~# dcgmi diag -r 3   (after kubectl drain)\n" % node)
            fh.write(run_one(drained, node, "dcgmi diag -r 3", now))
        with open(os.path.join(d, "README.md"), "w") as fh:
            if ftype:
                f = s["faults"][0]
                fh.write("# %s\n\nTicket: %s\n\n<details><summary>Answer</summary>\n\n%s\n\n</details>\n"
                         % (FAULTS[ftype]["title"], fmt(f, FAULTS[ftype]["ticket"]), fmt(f, FAULTS[ftype]["answer"])))
            else:
                fh.write("# Healthy baseline\n\nAll 8 nodes healthy, llm-train running. Diff the other cases against these files.\n")
    print("wrote %d cases to %s" % (len(cases), outdir))


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, a = argv[0], argv[1:]
    s = load_state()
    if cmd == "serve":
        serve(int(a[a.index("--port") + 1]) if "--port" in a else 9400)
    elif cmd == "metrics":
        sys.stdout.write(render_metrics(s))
    elif cmd == "run":
        if len(a) < 2:
            sys.exit("usage: run <node> <command...>")
        if a[0] not in NODES:
            sys.exit("unknown node %s (nodes: %s)" % (a[0], " ".join(NODES)))
        sys.stdout.write(run_one(s, a[0], " ".join(a[1:])))
    elif cmd == "shell":
        shell(a[0] if a else NODES[0])
    elif cmd == "faults":
        for k, v in FAULTS.items():
            if not k.startswith("_"):
                print("  %-15s %-5s L%d  %s" % (k, v["kind"], v["level"], v["title"]))
    elif cmd == "add":
        if len(a) < 2:
            sys.exit("usage: add <type> <node> [gpu|hca index]")
        f = add_fault(s, a[0], a[1], a[2] if len(a) > 2 else None)
        s["hidden"] = False
        save_state(s)
        print("injected %s on %s%s" % (f["type"], f["node"], (" #%d" % f["target"]) if "target" in f else ""))
    elif cmd == "drill":
        lvl = int(a[0]) if a else 3
        pool = [k for k, v in FAULTS.items() if v["level"] <= lvl]
        s["faults"] = []
        f = add_fault(s, random.choice(pool), random.choice(NODES))
        s["hidden"] = True
        save_state(s)
        print("TICKET (%s): %s" % (time.strftime("%H:%M"), fmt(f, FAULTS[f["type"]]["ticket"])))
    elif cmd == "reveal":
        if not s["faults"]:
            print("no active faults")
        for f in s["faults"]:
            print("%s  [%s]\n  %s\n" % (FAULTS[f["type"]]["title"], f["type"], fmt(f, FAULTS[f["type"]]["answer"])))
    elif cmd == "show":
        if s["hidden"]:
            print("%d hidden drill fault(s) active. ./lab.sh gpu reveal shows the answer." % len(s["faults"]))
        for f in ([] if s["hidden"] else s["faults"]):
            print("  %-15s %s%s  since %s" % (f["type"], f["node"], (" #%d" % f["target"]) if "target" in f else "",
                                             time.strftime("%H:%M:%S", time.localtime(f["since"]))))
        if not s["faults"]:
            print("no active faults (cluster healthy)")
    elif cmd == "clear":
        s["faults"], s["hidden"] = [], False
        save_state(s)
        print("all faults cleared")
    elif cmd == "nodes":
        print(" ".join(NODES))
    elif cmd == "node-manifests":
        print(json.dumps(node_manifests(), indent=1))
    elif cmd == "status-patch":
        print(json.dumps(status_patch(s, a[0])))
    elif cmd == "crashed":
        now = time.time()
        print(" ".join(n for n in NODES if node_crashed(s, n, now) is not None))
    elif cmd == "set-pods":           # stdin: lines "node=pod" of Running llm-train pods
        s["pods"] = dict(l.strip().split("=", 1) for l in sys.stdin if "=" in l)
        save_state(s)
    elif cmd == "pending-restarts":   # nodes whose training pod has crashed but not yet been restarted
        now, out = time.time(), []
        for f in s["faults"]:
            t = f["type"]
            crash_at = f["since"] if FAULTS[t]["crash"] else (f["since"] + 1200 if t == "nvlink" else None)
            if crash_at is not None and crash_at <= now and not f.get("restarted"):
                f["restarted"] = True
                out.append(f["node"])
        save_state(s)
        print(" ".join(sorted(set(out))))
    elif cmd == "fixtures":
        fixtures(a[0] if a else os.path.join(HERE, "faults"))
    else:
        sys.exit("unknown command %s\n%s" % (cmd, __doc__))


if __name__ == "__main__":
    main(sys.argv[1:])
