#!/usr/bin/env python3
"""atp.py - acceptance testing (ATP) for the simulated H100 GPU nodes.

Runs the commands an engineer would run on each node (nvidia-smi, dcgmi, nvbandwidth, NCCL, HPL, ibstat,
gdsio, flint, racadm, ...), parses their output, and compares it with:
  gpu/locked-versions.json   the build standard (driver, Fabric Manager, firmware, BIOS, iDRAC, kernel, ...)
  gpu/atp-thresholds.json    pass/fail targets (link width, NVLink, bandwidths, HPL TFLOPS, IB, GDS)
Every step is timed. Results go to gpu/reports/: a Markdown report with evidence, JSON, and a Prometheus textfile.

  python3 gpu/atp.py [node ...]       full ATP (default: all 8 nodes, plus the 64-GPU NCCL test)
  python3 gpu/atp.py --compliance     version compliance only (fast)
  options: --out DIR   --no-files
Exit code: 0 = every node passed, 1 = at least one defect.

The nodes under test are treated as pre-handover (drained, no customer jobs). Step durations are modelled on
real run times (DCGM level 3 ~15 min, HPL ~9 min, ...) because the simulator answers instantly.
Standard library only; runs on Python 3.9.
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gpusim as gs  # noqa: E402

NOW = time.time()

# Modelled duration (seconds) of each step on real hardware, in run order.
STEPS = [
    ("versions", 45, "Versions match the locked build standard"),
    ("inventory", 15, "8 GPUs visible to the driver and on the PCIe bus"),
    ("fabric", 20, "Fabric Manager running, NVSwitch fabric trained"),
    ("topology", 10, "NVLink and GPU-NIC topology (nvidia-smi topo -m)"),
    ("pcie", 15, "PCIe Gen5 x16 on every GPU, replays under limit"),
    ("ecc", 10, "No uncorrectable ECC, no pending or failed row remaps"),
    ("ib", 40, "8 InfiniBand ports Active at NDR 400, no symbol errors"),
    ("dcgm", 960, "DCGM diagnostic level 3 (burn-in)"),
    ("nvbandwidth", 180, "Host-to-device and GPU-to-GPU copy bandwidth"),
    ("nccl", 300, "NCCL all_reduce over the node's 8 GPUs (NVLink)"),
    ("hpl", 540, "HPL: FP64 TFLOPS and residual check"),
    ("gds", 420, "GPUDirect Storage read throughput per GPU"),
]
CLUSTER_STEP = ("nccl-cluster", 600, "NCCL all_reduce across all 64 GPUs (InfiniBand)")

DEFECT = {   # check -> (defect type, standard action)
    "versions": ("Config", "Re-flash / re-image to the locked versions, then rerun ATP"),
    "inventory": ("Hardware: RMA candidate", "Reseat and power-cycle via remote hands; RMA the GPU/baseboard if it repeats"),
    "fabric": ("Config", "Match Fabric Manager to the driver, restart nvidia-fabricmanager, confirm Fabric State Completed"),
    "topology": ("Hardware", "Reset GPU + NVSwitch fabric; service the baseboard / NVSwitch tray if NVLinks stay down"),
    "pcie": ("Hardware: rework", "Remote hands reseat the GPU / riser / cable to the PCIe switch, then retest"),
    "ecc": ("Hardware: RMA candidate", "Reset the GPU to apply pending row remaps; RMA if remapping failed"),
    "ib": ("Hardware: rework", "Clean / swap the cable or optic, check the leaf switch port, rerun ibdiagnet"),
    "dcgm": ("Hardware", "Fix the failing DCGM test area (see evidence) and rerun level 3"),
    "nvbandwidth": ("Performance", "Below target: see the link / NVLink / PCIe checks for the cause"),
    "nccl": ("Performance", "Below target: see the NVLink, thermal and power checks for the cause"),
    "hpl": ("Performance", "Below target or failed residual: see thermal, power, ECC and NVLink checks"),
    "gds": ("Config", "Load nvidia_fs for the running kernel (or boot the locked kernel), rerun gdsio"),
    "nccl-cluster": ("Performance", "Fabric below target: check IB link width / errors and congestion on the slow node"),
}


def load(name):
    with open(os.path.join(HERE, name)) as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def sh(st, node, cmd):
    return gs.run_one(st, node, cmd, NOW)


def ev(cmd, text, pick=None, limit=12):
    """Evidence: a command plus the interesting lines of its output."""
    lines = [l for l in text.rstrip("\n").split("\n") if l.strip()]
    if pick:
        rx = re.compile(pick, re.I)
        lines = [l for l in lines if rx.search(l)] or lines[:3]
    return (cmd, lines[:limit])


def result(status, value, evidence, finding=""):
    return dict(status=status, value=value, evidence=evidence, finding=finding)


# ---- checks: each parses real-style tool output -------------------------------------------------
def c_versions(st, node, L, T):
    got, evid = {}, []

    def grab(cmd, rx, key, pick=None):
        out = sh(st, node, cmd)
        m = re.search(rx, out)
        got[key] = m.group(1) if m else "unreadable"
        evid.append(ev(cmd, out, pick or rx.split("(")[0][:20], 3))

    grab("uname -r", r"(\S+)", "kernel")
    grab("cat /proc/driver/nvidia/version", r"x86_64\s+(\S+)", "driver", "NVRM")
    grab("nvidia-smi", r"CUDA Version: (\S+)", "cuda", "Driver Version")
    grab("nv-fabricmanager --version", r"is : (\S+)", "fabricmanager")
    out = sh(st, node, "nvidia-smi -q | grep VBIOS")
    vb = sorted(set(re.findall(r"VBIOS Version\s+: (\S+)", out)))
    got["vbios"] = vb[0] if len(vb) == 1 else ("mixed: " + ",".join(vb) if vb else "unreadable")
    evid.append(ev("nvidia-smi -q | grep VBIOS", out, None, 2))
    grab("ofed_info -s", r"(\S+?):?$", "doca_ofed")
    fws = []
    for p in range(gs.NGPU):
        m = re.search(r"FW Version:\s+(\S+)", sh(st, node, "flint -d mlx5_%d q" % p))
        fws.append(m.group(1) if m else "unreadable")
    got["hca_fw"] = fws
    evid.append(("flint -d mlx5_N q   (N = 0..7)", ["mlx5_%d FW Version: %s" % (p, v) for p, v in enumerate(fws)]))
    out = sh(st, node, "gdscheck -p")
    m1, m2 = re.search(r"GDS release version: (\S+)", out), re.search(r"nvidia_fs version:\s+(\S+)", out)
    got["gds"], got["nvidia_fs"] = (m1.group(1) if m1 else "unreadable"), (m2.group(1) if m2 else "unreadable")
    evid.append(ev("gdscheck -p", out, "release version|nvidia_fs version", 2))
    out = sh(st, node, "racadm getversion")
    for key, label in (("bios", "Bios Version"), ("idrac", "iDRAC Version")):
        m = re.search(label + r"\s+=\s+(\S+)", out)
        got[key] = m.group(1) if m else "unreadable"
    evid.append(ev("racadm getversion", out, "Bios|iDRAC", 2))

    drift = []
    for k, want in L.items():
        if k == "dcgm":
            continue                       # read from the DCGM diag step
        have = got.get(k)
        if k == "hca_fw":
            bad = ["mlx5_%d=%s" % (p, v) for p, v in enumerate(have) if v != want]
            if bad:
                drift.append("hca_fw %s (locked %s)" % (", ".join(bad), want))
        elif have != want:
            drift.append("%s %s (locked %s)" % (k, have, want))
    got["hca_fw"] = sorted(set(fws))[0] if len(set(fws)) == 1 else "mixed"
    if drift:
        return result("FAIL", "%d drift" % len(drift), evid, "; ".join(drift)), got
    return result("PASS", "all %d match" % (len(L) - 1), evid), got


def c_inventory(st, node, L, T):
    out = sh(st, node, "nvidia-smi -L")
    n = len(re.findall(r"^GPU \d+:", out, re.M))
    pci = sh(st, node, "lspci")
    gone = re.findall(r"^(\S+) 3D controller.*rev ff", pci, re.M)
    e = [ev("nvidia-smi -L", out, None, 8), ev("lspci | grep '3D controller'", pci, "3D controller", 8)]
    if n != T["gpus_per_node"] or gone:
        return result("FAIL", "%d/8 GPUs" % n, e, "GPU(s) missing; lspci rev ff at %s" % ", ".join(gone) if gone else "GPU missing")
    return result("PASS", "8/8 GPUs", e)


def c_fabric(st, node, L, T):
    act = sh(st, node, "systemctl is-active nvidia-fabricmanager").strip()
    q = sh(st, node, "nvidia-smi -q -d FABRIC")
    states = re.findall(r"State\s+: (.+)", q)
    statuses = re.findall(r"Status\s+: (.+)", q)
    e = [("systemctl is-active nvidia-fabricmanager", [act]), ev("nvidia-smi -q -d FABRIC", q, r"State|Status", 4)]
    if act != "active" or any(s.strip() != "Completed" for s in states) or any(s.strip() != "Success" for s in statuses):
        st_out = sh(st, node, "systemctl status nvidia-fabricmanager")
        e.append(ev("systemctl status nvidia-fabricmanager", st_out, "Active|don't match|Failed", 4))
        return result("FAIL", "FM %s" % act, e, "Fabric Manager %s, fabric state %s" % (act, ",".join(sorted(set(states)))))
    return result("PASS", "Completed", e)


def c_topology(st, node, L, T):
    out = sh(st, node, "nvidia-smi topo -m")
    rows = [l.split("\t") for l in out.split("\n") if l.startswith("GPU")]
    bad = []
    hdr = out.split("\n")[0].split("\t")[1:]
    for r in rows:
        g = r[0]
        for col, cell in zip(hdr, r[1:]):
            if re.match(r"GPU\d+$", col) and col != g and cell != T["nvlink"]:
                bad.append("%s-%s %s" % (g, col, cell))
            if col == "NIC" + g[3:] and cell != T["gpu_nic_affinity"]:
                bad.append("%s-%s %s" % (g, col, cell))
    e = [ev("nvidia-smi topo -m", out, r"^\s|^GPU", 9)]
    if bad or len(rows) != T["gpus_per_node"]:
        return result("FAIL", "%d bad pairs" % len(bad), e, "expected %s between GPUs: %s" % (T["nvlink"], ", ".join(sorted(set(bad))[:6])))
    return result("PASS", "NV18 all pairs", e)


def c_pcie(st, node, L, T):
    bad, lines = [], []
    for g in range(gs.NGPU):
        out = sh(st, node, "nvidia-smi -q -i %d -d PCI" % g)
        if "Unable to determine" in out:
            bad.append("GPU%d missing" % g)
            continue
        gen = re.search(r"PCIe Generation\s+:\s*\n\s+Max\s+: (\d+)\s*\n\s+Current\s+: (\d+)", out)
        wid = re.search(r"Link Width\s+:\s*\n\s+Max\s+: (\d+)x\s*\n\s+Current\s+: (\d+)x", out)
        rep = re.search(r"Replays Since Reset\s+: (\d+)", out)
        cg, cw, rp = int(gen.group(2)), int(wid.group(2)), int(rep.group(1))
        lines.append("GPU%d Gen%d x%d replays %d" % (g, cg, cw, rp))
        if cg < T["pcie_gen"] or cw < T["pcie_width"]:
            bad.append("GPU%d Gen%d x%d" % (g, cg, cw))
        if rp > T["pcie_replays_max"]:
            bad.append("GPU%d %d replays" % (g, rp))
    e = [("nvidia-smi -q -i N -d PCI   (N = 0..7)", lines)]
    if bad:
        e.append(ev("lspci -vv | grep LnkSta", sh(st, node, "lspci -vv"), "downgraded|LnkSta:.*x8", 4))
        return result("FAIL", ", ".join(bad[:2]), e, "PCIe: " + ", ".join(bad))
    return result("PASS", "Gen5 x16 all", e)


def c_ecc(st, node, L, T):
    bad, lines = [], []
    for g in range(gs.NGPU):
        out = sh(st, node, "nvidia-smi -q -i %d -d ECC,ROW_REMAPPER" % g)
        if "Unable to determine" in out:
            continue                                   # inventory already fails it
        dbe = int(re.search(r"DRAM Uncorrectable\s+: (\d+)", out).group(1))
        rr = out.split("Remapped Rows", 1)[1]
        pend = re.search(r"Pending\s+: (\w+)", rr).group(1)
        fail = re.search(r"Remapping Failure Occurred\s+: (\w+)", rr).group(1)
        if dbe or pend == "Yes" or fail == "Yes":
            bad.append("GPU%d DBE %d, remap pending %s, remap failure %s" % (g, dbe, pend, fail))
        lines.append("GPU%d DRAM uncorrectable %d, remap pending %s, failure %s" % (g, dbe, pend, fail))
    e = [("nvidia-smi -q -i N -d ECC,ROW_REMAPPER   (N = 0..7)", lines)]
    return result("FAIL", "%d GPU(s)" % len(bad), e, "; ".join(bad)) if bad else result("PASS", "clean", e)


def c_ib(st, node, L, T):
    out = sh(st, node, "ibstat")
    cas = re.split(r"^CA '", out, flags=re.M)[1:]
    bad, lines = [], []
    for ca in cas:
        name = ca.split("'")[0]
        state = re.search(r"\tState: (\w+)", ca).group(1)
        rate = int(re.search(r"Rate: (\d+)", ca).group(1))
        p = int(name.split("_")[1])
        pq = sh(st, node, "perfquery -C %s -P 1" % name)
        m = re.search(r"SymbolErrorCounter:\.*(\d+)", pq)
        sym = int(m.group(1)) if m else -1
        lines.append("%s %s %d Gb/s, symbol errors %s" % (name, state, rate, sym if sym >= 0 else "n/a (port down)"))
        if state != "Active" or rate < T["ib_rate_gbps"] or sym > T["ib_symbol_errors_max"] or sym < 0:
            bad.append("%s %s %dG sym %s" % (name, state, rate, sym if sym >= 0 else "n/a"))
    e = [("ibstat + perfquery -C mlx5_N -P 1", lines)]
    if bad or len(cas) != T["ib_ports"]:
        return result("FAIL", ", ".join(bad[:2]), e, "IB: " + ", ".join(bad))
    return result("PASS", "8x Active 400G", e)


def c_dcgm(st, node, L, T):
    out = sh(st, node, "dcgmi diag -r 3")
    e = [ev("dcgmi diag -r 3", out, r"Fail|Warning|Error|DCGM Version|Driver Version", 10)]
    m = re.search(r"DCGM Version\s+\| (\S+)", out)
    if "Error:" in out:
        return result("ERROR", "could not run", e, out.strip().split("\n")[0][:160]), None
    fails = re.findall(r"\| (\S[^|]*?)\s+\| Fail - GPUs: ([\d,]+)", out)
    if fails:
        return result("FAIL", "; ".join("%s GPU %s" % (k.strip(), v) for k, v in fails[:3]), e,
                      "DCGM failed: " + "; ".join("%s on GPU %s" % (k.strip(), v) for k, v in fails)), (m.group(1) if m else None)
    return result("PASS", "Pass - All", e), (m.group(1) if m else None)


def c_nvbandwidth(st, node, L, T):
    out = sh(st, node, "nvbandwidth")
    e = [ev("nvbandwidth", out, r"SUM|CUDA error|^\s+0\s", 6)]
    if "CUDA error" in out:
        return result("ERROR", "could not run", e, "nvbandwidth: CUDA error 802 (fabric not ready)")
    h2d_sec = out.split("Running host_to_device_memcpy_ce.")[1].split("SUM")[0]
    row = [l for l in h2d_sec.split("\n") if l.strip().startswith("0") and "." in l][0].split()[1:]
    cols = [l for l in h2d_sec.split("\n") if l.strip() and l.strip()[0].isdigit() and "." not in l][0].split()
    h2d = dict(zip(cols, map(float, row)))
    d2d_sec = out.split("Running device_to_device_memcpy_read_ce.")[1].split("SUM")[0]
    d2d = [float(x) for l in d2d_sec.split("\n")[2:] for x in l.split()[1:] if x not in ("N/A",) and "." in x]
    bad = ["GPU%s H2D %.1f GB/s" % (g, v) for g, v in h2d.items() if v < T["h2d_gbps_min_per_gpu"]]
    if d2d and min(d2d) < T["d2d_gbps_min_per_pair"]:
        bad.append("GPU-GPU min %.0f GB/s" % min(d2d))
    val = "H2D min %.1f, D2D min %.0f GB/s" % (min(h2d.values()), min(d2d) if d2d else 0)
    return result("FAIL", val, e, "; ".join(bad)) if bad else result("PASS", val, e)


def last_busbw(out):
    rows = [l.split() for l in out.split("\n") if re.match(r"^\s+\d+\s+\d+\s+float", l)]
    return float(rows[-1][7]) if rows else None


def c_nccl(st, node, L, T):
    out = sh(st, node, "all_reduce_perf -b 1M -e 8G -f 4 -g 8")
    bw = last_busbw(out)
    e = [ev("all_reduce_perf -b 1M -e 8G -f 4 -g 8", out, r"8589934592|failure|error|baseline", 4)]
    if bw is None:
        return result("ERROR", "could not run", e, "NCCL failed: " + out.strip().split("\n")[-1][:160])
    val = "%.0f GB/s busbw" % bw
    if bw < T["nccl_node_busbw_gbps_min"]:
        return result("FAIL", val, e, "busbw %.0f < %d GB/s" % (bw, T["nccl_node_busbw_gbps_min"]))
    return result("PASS", val, e)


def c_hpl(st, node, L, T):
    out = sh(st, node, "hpl.sh --dat hpl-8gpu.dat")
    e = [ev("hpl.sh --dat hpl-8gpu.dat", out, r"^WC0|PASSED|FAILED|ERROR|error|aborted", 4)]
    m = re.search(r"^WC0\s+\d+\s+\d+\s+\d+\s+\d+\s+([\d.]+)\s+([\d.e+]+)", out, re.M)
    if not m:
        return result("ERROR", "aborted", e, "HPL did not run: " + [l for l in out.split("\n") if l.strip()][-2][:160])
    tf = float(m.group(2)) / 1e3
    ok = "PASSED" in out
    val = "%.0f TFLOPS, residual %s" % (tf, "PASSED" if ok else "FAILED")
    if tf < T["hpl_tflops_min"] or not ok:
        return result("FAIL", val, e, "HPL %s (target >= %d TFLOPS, residual PASSED)" % (val, T["hpl_tflops_min"]))
    return result("PASS", val, e)


def c_gds(st, node, L, T):
    vals, lines = [], []
    for g in range(gs.NGPU):
        out = sh(st, node, "gdsio -D /mnt/vast/gds -d %d -w 8 -s 8G -i 1M -x 0 -I 0" % g)
        m = re.search(r"Throughput: ([\d.]+) GiB/sec", out)
        if "CUDA error" in out or "failed" in out:
            return result("ERROR", "could not run", [("gdsio -d %d ..." % g, [out.strip()[:160]])], "gdsio: " + out.strip()[:160])
        if m:
            vals.append((g, float(m.group(1))))
            lines.append("GPU%d READ GPUD %.2f GiB/s" % (g, float(m.group(1))))
    e = [("gdsio -D /mnt/vast/gds -d N -w 8 -s 8G -i 1M -x 0 -I 0   (N = 0..7)", lines)]
    bad = [(g, v) for g, v in vals if v < T["gds_read_gibs_min_per_gpu"]]
    total = sum(v for _, v in vals)
    val = "%.1f GiB/s node total" % total
    if bad:
        e.append(ev("gdscheck -p", sh(st, node, "gdscheck -p"), "nvidia_fs version|WARN", 2))
        return result("FAIL", val, e, "GDS below %.1f GiB/s on GPU %s" % (T["gds_read_gibs_min_per_gpu"], ",".join(str(g) for g, _ in bad)))
    return result("PASS", val, e)


CHECKS = {"versions": c_versions, "inventory": c_inventory, "fabric": c_fabric, "topology": c_topology, "pcie": c_pcie,
          "ecc": c_ecc, "ib": c_ib, "dcgm": c_dcgm, "nvbandwidth": c_nvbandwidth, "nccl": c_nccl, "hpl": c_hpl, "gds": c_gds}


def modelled(step, cost, node, status):
    d = cost * (0.9 + 0.25 * (gs.h("dur", step, node) % 1000) / 1000.0)
    return d * 2 if status != "PASS" else d          # a failed step is retried once before it is declared failed


def run_node(st, node, L, T, steps):
    res, timing, dcgm_ver = {}, {}, None
    for step, cost, _ in steps:
        t0 = time.time()
        out = CHECKS[step](st, node, L, T)
        if step == "versions":
            out, _ = out
        if step == "dcgm":
            out, dcgm_ver = out
        res[step] = out
        timing[step] = dict(modelled_s=modelled(step, cost, node, out["status"]), wall_ms=(time.time() - t0) * 1000)
    if dcgm_ver and "dcgm" in L and dcgm_ver != L["dcgm"] and "versions" in res:
        r = res["versions"]
        r["status"], r["finding"] = "FAIL", (r["finding"] + "; " if r["finding"] else "") + "dcgm %s (locked %s)" % (dcgm_ver, L["dcgm"])
    return res, timing


def fmt_dur(s):
    return "%dm%02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


def main(argv):
    compliance = "--compliance" in argv
    nofiles = "--no-files" in argv
    outdir = argv[argv.index("--out") + 1] if "--out" in argv else os.path.join(HERE, "reports")
    nodes = [a for a in argv if a in gs.NODES] or list(gs.NODES)
    unknown = [a for a in argv if not a.startswith("--") and a not in gs.NODES and (argv.index(a) == 0 or argv[argv.index(a) - 1] != "--out")]
    if unknown:
        sys.exit("unknown node(s): %s (nodes: %s)" % (" ".join(unknown), " ".join(gs.NODES)))
    L, T = load("locked-versions.json"), load("atp-thresholds.json")
    s = gs.load_state()
    # Pre-handover: the nodes under test run no jobs (drained); nodes not under test keep their pods.
    st = dict(s, pods={n: gs.pod_of(s, n) for n in gs.NODES if n not in nodes})
    steps = [x for x in STEPS if x[0] == "versions"] if compliance else STEPS
    names = {k: d for k, _, d in STEPS + [CLUSTER_STEP]}

    results, timings = {}, {}
    for n in nodes:
        results[n], timings[n] = run_node(st, n, L, T, steps)

    cluster = None
    if not compliance and len(nodes) == len(gs.NODES):
        cst = dict(s, pods=None)
        out = gs.run_one(cst, nodes[0], "all_reduce_perf -b 1M -e 8G -f 4 -g 1", NOW)
        bw = last_busbw(out)
        e = [ev("all_reduce_perf -b 1M -e 8G -f 4 -g 1   (64 ranks, 8 nodes)", out, r"8589934592|NCCL WARN|failure|Only|baseline", 4)]
        if bw is None:
            why = [l for l in out.split("\n") if re.search(r"failure|WARN|error|Only|no llm-train", l, re.I)]
            cluster = result("ERROR", "could not run", e, (why[0] if why else "all_reduce_perf failed").strip()[:160])
        elif bw < T["nccl_cluster_busbw_gbps_min"]:
            cluster = result("FAIL", "%.0f GB/s busbw" % bw, e, "busbw %.0f < %d GB/s" % (bw, T["nccl_cluster_busbw_gbps_min"]))
        else:
            cluster = result("PASS", "%.0f GB/s busbw" % bw, e)
        cluster["modelled_s"] = modelled(CLUSTER_STEP[0], CLUSTER_STEP[1], "cluster", cluster["status"])

    # ---- defects: first failing check per node = root-cause candidate, the rest are consequences ----
    defects = []
    for n in nodes:
        fails = [k for k, _, _ in steps if results[n][k]["status"] != "PASS"]
        for i, k in enumerate(fails):
            kind, action = DEFECT[k]
            defects.append(dict(id="DEF-%03d" % (len(defects) + 1), node=n, check=k, status=results[n][k]["status"],
                                finding=results[n][k]["finding"], type=kind, action=action,
                                role="root cause" if i == 0 else "consequence of %s" % fails[0]))
    if cluster and cluster["status"] != "PASS":
        kind, action = DEFECT["nccl-cluster"]
        defects.append(dict(id="DEF-%03d" % (len(defects) + 1), node="cluster", check="nccl-cluster", status=cluster["status"],
                            finding=cluster["finding"], type=kind, action=action,
                            role="consequence" if any(d["role"] == "root cause" for d in defects) else "root cause"))

    node_pass = {n: all(results[n][k]["status"] == "PASS" for k, _, _ in steps) for n in nodes}
    node_time = {n: sum(t["modelled_s"] for t in timings[n].values()) for n in nodes}
    wall = max(node_time.values()) + (cluster["modelled_s"] if cluster else 0)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(NOW))
    title = "Version compliance" if compliance else "ATP"

    # ---- console ----
    icon = {"PASS": "ok  ", "FAIL": "FAIL", "ERROR": "ERR "}
    print("%s for %s   (locked: gpu/locked-versions.json, thresholds: gpu/atp-thresholds.json)\n" % (title, " ".join(nodes)))
    print("%-12s" % "check" + "".join("%-9s" % n for n in nodes))
    for k, _, _ in steps:
        print("%-12s" % k + "".join("%-9s" % icon[results[n][k]["status"]].strip() for n in nodes))
    print("%-12s" % "duration" + "".join("%-9s" % fmt_dur(node_time[n]) for n in nodes))
    if cluster:
        print("\ncluster NCCL (64 GPUs): %s  %s" % (cluster["status"], cluster["value"]))
    print("\nResult: %d/%d nodes pass. Modelled wall time %s (nodes in parallel%s)."
          % (sum(node_pass.values()), len(nodes), fmt_dur(wall), " + cluster NCCL" if cluster else ""))
    for d in defects:
        print("  %s %-8s %-11s %-28s %s" % (d["id"], d["node"], d["check"], "[" + d["role"] + "]", d["finding"][:110]))

    if nofiles:
        return 0 if not defects else 1

    # ---- files ----
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, "%s-%s" % ("compliance" if compliance else "atp", stamp))
    with open(base + ".json", "w") as f:
        json.dump(dict(generated=stamp, nodes=nodes, locked=L, thresholds=T, results=results, timings=timings,
                       cluster=cluster, defects=defects, wall_s=wall), f, indent=2)
    with open(base + ".prom", "w") as f:      # node_exporter textfile collector format
        f.write("# HELP atp_check_pass 1 if the ATP check passed\n# TYPE atp_check_pass gauge\n")
        for n in nodes:
            for k, _, _ in steps:
                f.write('atp_check_pass{node="%s",check="%s"} %d\n' % (n, k, results[n][k]["status"] == "PASS"))
        f.write("# HELP atp_step_duration_seconds Modelled duration of each ATP step\n# TYPE atp_step_duration_seconds gauge\n")
        for n in nodes:
            for k, t in timings[n].items():
                f.write('atp_step_duration_seconds{node="%s",step="%s"} %.0f\n' % (n, k, t["modelled_s"]))
        f.write("# TYPE atp_wall_seconds gauge\natp_wall_seconds %.0f\n# TYPE atp_run_timestamp_seconds gauge\natp_run_timestamp_seconds %d\n" % (wall, NOW))

    md = ["# %s report: GPU nodes %s" % (title, ", ".join(nodes)), "",
          "Generated %s by `gpu/atp.py` against **simulated** hardware (gpusim). "
          "Build standard: `gpu/locked-versions.json`. Thresholds: `gpu/atp-thresholds.json`." % time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(NOW)), "",
          "## Summary", "",
          "**%d of %d nodes pass.** %s" % (sum(node_pass.values()), len(nodes),
                                           "Ready for handover." if all(node_pass.values()) and (not cluster or cluster["status"] == "PASS")
                                           else "Not ready for handover: %d defect(s) below." % len(defects)), "",
          "| Node | Result | Root cause | Modelled duration |", "|---|---|---|---|"]
    for n in nodes:
        rc = [d for d in defects if d["node"] == n and d["role"] == "root cause"]
        md.append("| %s | %s | %s | %s |" % (n, "✅ PASS" if node_pass[n] else "❌ FAIL",
                                             ("%s: %s" % (rc[0]["check"], rc[0]["finding"])) if rc else "-", fmt_dur(node_time[n])))
    if cluster:
        md += ["", "Cluster: %s **%s** (%s)." % (names["nccl-cluster"], cluster["status"], cluster["value"])]
    md += ["", "## Check matrix", "", "| Check | " + " | ".join(nodes) + " |", "|---" * (len(nodes) + 1) + "|"]
    for k, _, desc in steps:
        md.append("| %s<br><sub>%s</sub> | " % (k, desc) + " | ".join(
            ("✅ " if results[n][k]["status"] == "PASS" else "❌ ") + results[n][k]["value"] for n in nodes) + " |")
    md += ["", "## Defects", ""]
    if defects:
        md += ["| ID | Node | Check | Role | Finding | Type | Action |", "|---|---|---|---|---|---|---|"]
        md += ["| %s | %s | %s | %s | %s | %s | %s |" % (d["id"], d["node"], d["check"], d["role"], d["finding"].replace("|", "/"),
                                                        d["type"], d["action"]) for d in defects]
    else:
        md.append("None.")
    # timing: where does the time go?
    per_step = {}
    for n in nodes:
        for k, t in timings[n].items():
            per_step.setdefault(k, []).append(t["modelled_s"])
    tot = sum(max(v) for v in per_step.values())
    md += ["", "## Timing (instrumented)", "",
           "Nodes run in parallel, so the slowest node sets the pace. Steps ranked by their share of the critical path:", "",
           "| Step | Median | Slowest node | Share |", "|---|---|---|---|"]
    for k, v in sorted(per_step.items(), key=lambda kv: -max(kv[1])):
        v2 = sorted(v)
        md.append("| %s | %s | %s | %d%% |" % (k, fmt_dur(v2[len(v2) // 2]), fmt_dur(max(v)), round(100 * max(v) / tot)))
    if cluster:
        md.append("| nccl-cluster | %s | - | after nodes |" % fmt_dur(cluster["modelled_s"]))
    top = max(per_step.items(), key=lambda kv: max(kv[1]))[0]
    md += ["", "Wall time: **%s**. Biggest step: **%s**: the next place to look for a faster deployment "
               "(e.g. run it in parallel with other tests that don't load the GPUs, or shorten it where the design allows)."
           % (fmt_dur(wall), top), "", "## Evidence", ""]
    for n in nodes:
        md += ["<details><summary>%s: %s</summary>" % (n, "PASS" if node_pass[n] else "FAIL"), ""]
        for k, _, _ in steps:
            r = results[n][k]
            md += ["**%s: %s** (%s)%s" % (k, r["status"], r["value"], (" — " + r["finding"]) if r["finding"] else ""), "", "```"]
            for cmd, lines in r["evidence"]:
                md.append("$ " + cmd)
                md += ["  " + l for l in lines]
            md += ["```", ""]
        md += ["</details>", ""]
    if cluster:
        md += ["**Cluster NCCL: %s** (%s)" % (cluster["status"], cluster["value"]), "", "```"]
        for cmd, lines in cluster["evidence"]:
            md += ["$ " + cmd] + ["  " + l for l in lines]
        md += ["```", ""]
    with open(base + ".md", "w") as f:
        f.write("\n".join(md) + "\n")
    rel = os.path.relpath(base, os.getcwd())
    print("\nReport: %s.md  (+ .json, .prom)" % (base if rel.startswith("..") else rel))
    return 0 if not defects else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
