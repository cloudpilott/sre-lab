#!/usr/bin/env python3
"""cephsim - a fake 6-node Ceph (Squid) cluster with 24 NVMe OSDs, for storage diagnosis drills.

One file, standard library only (runs on macOS python3 and in python:3.12-alpine).
It is the checkpoint store of the simulated GPU fleet: CephFS 'ckpt' is mounted by the 8 H100 nodes.

  serve [--port 9283]          Prometheus exporter with ceph-mgr metric names (ceph_health_detail, ceph_osd_up, ...)
  run <host> <command...>      print what <command> would show on <host> (ceph -s, ceph osd df, smartctl, ...)
  shell <host>                 interactive fake root shell on <host> (ceph-01 is the admin host)
  faults                       list injectable fault types
  add <type> [target]          inject a fault (target = OSD id, host or mon host; default random)
  drill [level]                inject one random hidden fault and print the ticket
  reveal | show | clear        answer key / current faults / remove all faults
  nodes | node-manifests | status-patch <host>    helpers for lab.sh
  fixtures <dir>               write static outputs for every fault type (offline study)
  metrics                      print one metrics scrape to stdout

Fault state lives in a JSON file: $CEPHSIM_STATE (default: state.json next to this script).
In the cluster the exporter reads the same file from a ConfigMap.
"""
import hashlib
import json
import os
import random
import re
import shlex
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("CEPHSIM_STATE", os.path.join(HERE, "state.json"))

HOSTS = ["ceph-%02d" % i for i in range(1, 7)]
OSD_PER_HOST = 4
NOSD = len(HOSTS) * OSD_PER_HOST
MONS = HOSTS[:3]
MGRS = HOSTS[:2]
MDSS = ["ceph-04", "ceph-05"]
ADMIN = ["ceph-01", "ceph-02"]
OSD_BYTES = 7681501126656            # Samsung PM9A3 7.68 TB
OSD_W = 6.98599                      # CRUSH weight = TiB
DISK_MODEL, DISK_FW = "SAMSUNG MZQL27T6HBLA-00A07", "GDC5902Q"
VERSION = "19.2.3"
T0 = 1793491200                      # 2026-11-01 00:00 UTC
BUILT_AT = T0 - 40 * 86400           # cluster deployed
UPGRADED_AT = T0 - 2 * 86400         # last rolling restart (upgrade to 19.2.3)
DOWN_OUT = 600                       # mon_osd_down_out_interval
NEARFULL, BACKFILLFULL, FULL = 0.85, 0.90, 0.95
CLUSTER_IF, PUBLIC_IF = "ens1f1np1", "ens1f0np0"

POOLS = [  # id, name, pg_num, share of stored bytes, application
    (1, ".mgr", 1, 0.0, "mgr"),
    (2, "rbd", 256, 0.16, "rbd"),
    (3, "cephfs.ckpt.data", 512, 0.83, "cephfs"),
    (4, "cephfs.ckpt.meta", 64, 0.01, "cephfs"),
]


def h(*parts):
    return int(hashlib.sha256("/".join(str(p) for p in parts).encode()).hexdigest(), 16)


def jitter(lo, hi, *seed):
    """Deterministic noise that changes every 15s (stable within a scrape)."""
    r = (h(*seed, int(time.time() // 15)) % 10000) / 10000.0
    return lo + (hi - lo) * r


def uid(*seed):
    x = "%032x" % (h("uuid", *seed) % (1 << 128))
    return "%s-%s-4%s-a%s-%s" % (x[:8], x[8:12], x[13:16], x[17:20], x[20:])


FSID = uid("fsid")
VHASH = "%040x" % (h("version", VERSION) % (1 << 160))
VSTRING = "ceph version %s (%s) squid (stable)" % (VERSION, VHASH)
IMAGE_ID = "%012x" % (h("image") % (1 << 48))


def osd_host(o):
    return HOSTS[o // OSD_PER_HOST]


def osd_dev(o):
    return "nvme%dn1" % (o % OSD_PER_HOST)


def host_osds(host):
    i = HOSTS.index(host)
    return list(range(i * OSD_PER_HOST, (i + 1) * OSD_PER_HOST))


def pub_ip(host):
    return "10.10.0.%d" % (11 + HOSTS.index(host))


def clu_ip(host):
    return "10.20.0.%d" % (11 + HOSTS.index(host))


def mgr_name(host):
    return "%s.%s" % (host, "".join("abcdefghijklmnopqrstuvwxyz"[h("mgr", host, i) % 26] for i in range(6)))


def mds_name(host):
    return "ckpt.%s.%s" % (host, "".join("abcdefghijklmnopqrstuvwxyz"[h("mds", host, i) % 26] for i in range(6)))


def base_used(o):
    return 0.548 + (h("use", o) % 700) / 10000.0


def fmt_bytes(n):
    """Ceph-style sizes: '7.0 TiB', '12 MiB', '0 B'."""
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    x, i = float(n), 0
    while x >= 1024 and i < 5:
        x, i = x / 1024, i + 1
    if i == 0:
        return "%d B" % n
    return ("%.1f %s" if x < 10 else "%.0f %s") % (x, units[i])


def fmt_age(sec):
    sec = max(0, int(sec))
    if sec < 90:
        return "%ds" % sec
    if sec < 90 * 60:
        return "%dm" % (sec // 60)
    if sec < 48 * 3600:
        return "%dh" % (sec // 3600)
    if sec < 14 * 86400:
        return "%dd" % (sec // 86400)
    return "%dw" % (sec // (7 * 86400))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%06dZ" % (h("us", int(t)) % 10**6)


def ts(t):
    return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(t))


def jts(t):
    """journalctl short timestamp."""
    return time.strftime("%b %d %H:%M:%S", time.localtime(t))


# ---------------------------------------------------------------------------------------------
# Fault catalogue. kind: osd (target = OSD id), host (target = host), mon (target = mon host).
# ---------------------------------------------------------------------------------------------
FAULTS = {
    "osd-crash": dict(kind="osd", level=1, title="OSD daemon crashed (software assert)",
        ticket="CephOSDDown fired on the storage cluster around {time}. llm-train checkpoints still succeed, "
               "but the cluster is HEALTH_WARN. Find out what happened and whether data is at risk.",
        answer="osd.{t} on {host} crashed on a software assert: `ceph crash ls` / `ceph crash info` and its journal show "
               "'FAILED ceph_assert(head_obc)' then 'Caught signal (Aborted)'. systemd gave up after 5 restarts, so "
               "`ceph orch ps` shows it in error. The disk is healthy: dmesg is clean and smartctl passes. Because it stayed "
               "down for more than 10 min (mon_osd_down_out_interval) Ceph marked it out and backfilled its PGs elsewhere. "
               "Data was never at risk (2 of 3 copies stayed up; min_size 2). Action: `ceph orch daemon restart osd.{t}`, "
               "`ceph crash archive <id>`. If it hits the same assert again, attach `ceph crash info` to a tracker.ceph.com "
               "search and plan the point-release upgrade. Don't replace the disk."),
    "disk-fail": dict(kind="osd", level=1, title="NVMe drive died under an OSD",
        ticket="CephOSDDown and CephDaemonCrash fired on the storage cluster around {time}. "
               "The storage team asks: software or hardware, and what do we do?",
        answer="{dev} on {host} dropped off the bus: dmesg shows I/O timeouts, 'controller is down; will reset: "
               "CSTS=0xffffffff' and 'Removing after probe failure'. smartctl can't open it and lsblk no longer lists it. "
               "osd.{t} aborted with 'Unexpected IO error. This may suggest a hardware issue' and can't start again. Ceph "
               "marked it out after 10 min and is backfilling. Action: hardware. Let recovery finish (`ceph -s`), then "
               "`ceph orch osd rm {t} --replace` (keeps the OSD id), swap the drive; cephadm recreates the OSD on the new "
               "disk. Restarting the daemon won't help."),
    "nearfull": dict(kind="osd", level=1, title="One OSD nearfull: data imbalance",
        ticket="CephOSDNearFull fired, but the storage team says the cluster is only ~58% used. "
               "Is the alert real, and do we need to buy disks?",
        answer="osd.{t} on {host} is at ~87% while the average is ~58%: `ceph osd df` shows VAR ~1.5 and ~50% more PGs "
               "than its peers. The balancer is off (`ceph balancer status`: active false), so PGs were never evened "
               "out. The fullest OSD sets MAX AVAIL for every pool (`ceph df`), and at 95% (full_ratio) the whole cluster "
               "stops writes. Action: `ceph balancer mode upmap` + `ceph balancer on` (or `ceph osd reweight-by-utilization`) "
               "and watch %USE converge. No new disks needed."),
    "host-down": dict(kind="host", level=1, title="Storage host down",
        ticket="CephOSDHostDown paged the storage on-call at {time}: one of the storage servers stopped answering.",
        answer="{host} is down: ping and ssh fail, `ceph orch host ls` shows it Offline, its 4 OSDs are down (plus its "
               "mon / MDS if it runs one). After 10 min Ceph marked them out and backfills about 1/6 of the data onto the "
               "other 5 hosts. Data stays available because the CRUSH failure domain is host (size 3, min_size 2). "
               "Action: check the BMC (power, SEL, console) and bring it back; its OSDs rejoin and catch up. "
               "For planned maintenance, `ceph osd set noout` first so data doesn't move, and unset it afterwards."),
    "clock-skew": dict(kind="mon", level=2, title="Monitor clock skew",
        ticket="The storage dashboard has shown HEALTH_WARN since {time}. Nothing seems broken. Is it safe to ignore?",
        answer="mon.{host}'s clock is ~0.3 s off (MON_CLOCK_SKEW, max 0.05 s). On {host}, `chronyc tracking` says "
               "'Not synchronised' and `chronyc sources` shows the NTP server unreachable (Reach 0): the firewall change "
               "blocks UDP 123. Not safe to ignore: skewed monitors can drop out of quorum and cephx tickets fail. Action: "
               "restore NTP (open UDP 123), `chronyc makestep`, then check `ceph time-sync-status`."),
    "mon-disk-full": dict(kind="mon", level=2, title="Monitor stopped: its disk filled with debug logs",
        ticket="CephMonDownQuorumAtRisk fired at {time}. One more monitor failure and the storage cluster stops serving I/O.",
        answer="mon.{host} shut itself down: its journal says 'reached critical levels of available space on local monitor "
               "storage -- shutdown!'. `df -h` on {host} shows / at 97%; `du` shows ceph-mon.{host}.log at ~350 GB because "
               "debug_mon was left at 20/20 (`ceph config dump | grep debug`). Action: `ceph config rm mon.{host} debug_mon`, "
               "truncate the log, check free space, `ceph orch daemon restart mon.{host}`; quorum goes back to 3/3. "
               "Never delete the mon store (store.db)."),
    "flags-left": dict(kind="host", level=2, title="Maintenance flags left set",
        ticket="After last night's maintenance on {host} the storage cluster has been HEALTH_WARN with degraded PGs for "
               "hours. All 24 OSDs are up. Recovery isn't moving.",
        answer="The maintenance flags noout, norebalance and norecover were never unset (OSDMAP_FLAGS; `ceph osd dump | grep "
               "flags`). With norecover set, the writes {host}'s OSDs missed while they were down are never recovered, so "
               "PGs stay active+recovery_wait+degraded and the degraded count doesn't move. Action: `ceph osd unset "
               "norecover`, `ceph osd unset norebalance`, `ceph osd unset noout`, watch `ceph -s` return to HEALTH_OK. "
               "Put 'unset flags' on the maintenance checklist."),
    "osd-full": dict(kind="osd", level=2, title="Full OSD blocks all writes (wrong CRUSH weight)",
        ticket="llm-train checkpoint failed at {time}: OSError: [Errno 28] No space left on device: "
               "'/ckpt/llm-train/step_41000/rank3.pt'. The storage dashboard says the cluster is only ~60% used.",
        answer="osd.{t} on {host} hit 95% (full_ratio): OSD_FULL is HEALTH_ERR and Ceph blocks writes to every pool, so "
               "CephFS returns ENOSPC even though the cluster is ~60% used. `ceph osd tree` shows its CRUSH weight is "
               "13.97 instead of 6.99 (set wrong when the disk was replaced), so CRUSH gave it twice its share of data "
               "(`ceph osd df`: VAR ~1.6, ~2x PGs). Action: `ceph osd crush reweight osd.{t} 6.98599`. If you need writes "
               "back right now, temporarily `ceph osd set-full-ratio 0.96`, and restore 0.95 once data has moved."),
    "slow-ops": dict(kind="osd", level=3, title="Worn-out NVMe makes one OSD slow",
        ticket="Since about {time} llm-train checkpoints take 6+ minutes instead of 40 s, and GPU utilization drops to 0 "
               "during every checkpoint. No GPU or network alerts.",
        answer="osd.{t} ({dev} on {host}) is slow: SLOW_OPS and BLUESTORE_SLOW_OP_ALERT, `ceph osd perf` shows ~1 s "
               "commit/apply latency vs 1-3 ms elsewhere, and its journal logs 'slow operation observed'. smartctl shows "
               "Percentage Used 104% and the error log growing: the drive is worn out and throttling (dmesg: occasional "
               "I/O timeouts; `ceph device ls` shows the wear). Every write to a PG that includes this OSD waits for it, so "
               "one slow disk slows the whole checkpoint. Action: `ceph osd out {t}` (or `ceph osd primary-affinity "
               "osd.{t} 0` as a stopgap), let data drain, then replace the drive (`ceph orch osd rm {t} --replace`)."),
    "pg-inconsistent": dict(kind="osd", level=3, title="Scrub found an inconsistent PG (bad sector)",
        ticket="CephPGsDamaged fired at {time}: 'Possible data damage: 1 pg inconsistent'. "
               "Was data lost, and what should we do?",
        answer="PG {pg} failed deep-scrub: `rados list-inconsistent-obj {pg}` shows a read_error on the shard on osd.{t}. "
               "The other two copies agree, so nothing is lost. dmesg on {host} has 'critical medium error, dev {dev}' and "
               "smartctl shows Media and Data Integrity Errors: the disk has bad sectors. Action: `ceph pg repair {pg}` "
               "(rewrites the bad copy from the good ones), then watch: more media errors mean replace the disk. "
               "Always check which shard is bad before repairing."),
    "mtu-mismatch": dict(kind="host", level=3, title="MTU mismatch on the cluster network",
        ticket="CephOSDFlapping and CephOSDTimeoutsClusterNetwork since this morning's network change on {host}. "
               "Checkpoint writes stall for ~30 s every few minutes.",
        answer="{host}'s cluster-network NIC " + CLUSTER_IF + " has MTU 1500 while every other host uses 9000 "
               "(`ip link`). Small packets work, so a plain ping succeeds, but OSD heartbeats are padded to 2000 bytes "
               "(osd_heartbeat_min_size) and replication uses jumbo frames. Peers stop hearing {host}'s OSDs on the back "
               "network ('heartbeat_check: no reply ... since back <old> front <recent>') and report them down; the OSDs "
               "say 'wrongly marked me down' and come back: flapping. `ping -M do -s 8972 <cluster ip>` fails. Action: "
               "set MTU 9000 on " + CLUSTER_IF + " (netplan), verify jumbo pings both ways. A brief `ceph osd set nodown` "
               "stops the flapping while you fix it (unset it afterwards)."),
}


# ---------------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------------
def load_state(path=None):
    try:
        with open(path or STATE) as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("faults", [])
    s.setdefault("hidden", False)
    return s


def save_state(s, path=None):
    with open(path or STATE, "w") as f:
        json.dump(s, f, indent=2, sort_keys=True)
        f.write("\n")


def faults(s, ftype=None):
    return [f for f in s["faults"] if ftype is None or f["type"] == ftype]


def first(s, ftype):
    fs = faults(s, ftype)
    return fs[0] if fs else None


# ---------------------------------------------------------------------------------------------
# Placement: a deterministic stand-in for CRUSH (3 replicas, failure domain = host)
# ---------------------------------------------------------------------------------------------
_PGS = None


def all_pgs():
    global _PGS
    if _PGS is None:
        _PGS = []
        for pid, _, n, _, _ in POOLS:
            for i in range(n):
                hs = sorted(range(len(HOSTS)), key=lambda x: h("crush", pid, i, x))[:3]
                _PGS.append(("%d.%x" % (pid, i), pid, [x * OSD_PER_HOST + h("osd", pid, i, x) % OSD_PER_HOST for x in hs]))
    return _PGS


def pgs_with(o):
    return [p for p in all_pgs() if o in p[2]]


def inconsistent_pg(o):
    for pgid, pid, acting in all_pgs():
        if pid == 3 and o in acting[1:]:
            return pgid, acting
    return next((p[0], p[2]) for p in all_pgs() if o in p[2])


RAW_USED = sum(base_used(o) for o in range(NOSD)) * OSD_BYTES
STORED = RAW_USED / 3 * 0.985


def pool_stored(pid):
    if pid == 1:
        return 4.1 * 2**20
    return STORED * [p[3] for p in POOLS if p[0] == pid][0]


def pool_objects(pid):
    if pid == 1:
        return 2
    if pid == 4:
        return 1630221
    return int(pool_stored(pid) / (4 * 2**20))


def pg_objs(pid):
    n = [p[2] for p in POOLS if p[0] == pid][0]
    return pool_objects(pid) // n


# ---------------------------------------------------------------------------------------------
# The cluster model: every command and the exporter read from this, so they always agree.
# ---------------------------------------------------------------------------------------------
def model(s, now=None):
    now = now or time.time()
    m = dict(now=now)
    osds = [dict(id=o, host=osd_host(o), dev=osd_dev(o), up=True, inn=True, used=base_used(o), weight=OSD_W,
                 pgmul=1.0, down_since=None, out_since=None, daemon="running", slow=False) for o in range(NOSD)]
    mons = {x: dict(up=True, skew=0.0) for x in MONS}
    hosts = {x: dict(up=True, mtu=9000) for x in HOSTS}
    flags, crashes, balancer = [], [], True
    for f in s["faults"]:
        t, age, tg = f["type"], now - f["since"], f.get("target")
        if t in ("osd-crash", "disk-fail"):
            osds[tg].update(up=False, down_since=f["since"], daemon="error")
            crashes.append(dict(id=iso(f["since"]) + "_" + uid("crash", f["since"], tg), entity="osd.%d" % tg,
                                host=osd_host(tg), t=f["since"], type=t))
        elif t == "host-down":
            hosts[tg]["up"] = False
            for o in host_osds(tg):
                osds[o].update(up=False, down_since=f["since"], daemon="unknown")
            if tg in mons:
                mons[tg]["up"] = False
        elif t == "nearfull":
            osds[tg].update(used=0.868 + min(age, 3600) / 3600 * 0.008, pgmul=1.5)
            balancer = False
        elif t == "osd-full":
            osds[tg].update(used=0.9512, pgmul=1.64, weight=OSD_W * 2)
        elif t == "slow-ops":
            osds[tg]["slow"] = True
        elif t == "mon-disk-full":
            mons[tg]["up"] = False
        elif t == "clock-skew":
            mons[tg]["skew"] = jitter(0.28, 0.34, "skew")
        elif t == "flags-left":
            flags += ["noout", "norebalance", "norecover"]
        elif t == "mtu-mismatch":
            hosts[tg]["mtu"] = 1500
            if age > 60 and (age - 60) % 240 < 45:          # down ~45 s out of every 4 min
                for o in host_osds(tg):
                    osds[o].update(up=False, down_since=now - (age - 60) % 240)
    for o in osds:
        if not o["up"] and "noout" not in flags and now - o["down_since"] >= DOWN_OUT:
            o["inn"], o["out_since"] = False, o["down_since"] + DOWN_OUT
        o["commit"] = o["apply"] = 0 if not o["up"] else (
            int(jitter(780, 1450, "lat", o["id"])) if o["slow"] else int(jitter(0, 3.99, "lat", o["id"])))
    m.update(osds=osds, mons=mons, hosts=hosts, flags=flags, crashes=crashes, balancer=balancer)

    # ---- placement groups
    down = {o["id"] for o in osds if not o["up"]}
    out = {o["id"] for o in osds if not o["inn"]}
    out_start = min([o["out_since"] for o in osds if o["out_since"]] or [now])
    dur = 1500 + 700 * (len(out) - 1)
    progress = (now - out_start) / dur if out else 0
    fl = first(s, "flags-left")
    inc = first(s, "pg-inconsistent")
    inc_pg = inconsistent_pg(inc["target"])[0] if inc else None
    pgs = []
    for pgid, pid, acting in all_pgs():
        objs = pg_objs(pid)
        st, deg, up, act, since = "active+clean", 0, list(acting), list(acting), None
        missing = [x for x in acting if x in down]
        if missing:
            alive = [x for x in acting if x not in down]
            if all(x in out for x in missing):
                used_hosts = {osd_host(x) for x in alive}
                repl = []
                for x in missing:
                    cands = [y for y in range(NOSD) if y not in down and osd_host(y) not in used_hosts]
                    c = cands[h("repl", pgid, x) % len(cands)]
                    used_hosts.add(osd_host(c))
                    repl.append(c)
                up = alive + repl
                r = (h("bf", pgid) % 10000) / 10000.0
                if r < progress:
                    act = up
                else:
                    act = alive
                    st = "active+undersized+degraded+remapped+" + ("backfilling" if r < progress + 0.025 else "backfill_wait")
                    deg, since = objs * len(missing), out_start - DOWN_OUT
            else:
                up = act = alive
                st = "active+undersized+degraded" if len(alive) >= 2 else "undersized+degraded+peered"
                deg, since = objs * len(missing), max(osds[x]["down_since"] for x in missing)
        elif fl and any(osd_host(x) == fl["target"] for x in acting):
            st, deg, since = "active+recovery_wait+degraded", int(objs * 0.04) + 1, fl["since"]
        if pgid == inc_pg and st == "active+clean":
            st = "active+clean+inconsistent"
        pgs.append(dict(pgid=pgid, pool=pid, up=up, acting=act, state=st, degraded=deg, objects=objs, since=since))
    m["pgs"] = pgs
    m["objects"] = sum(pool_objects(p[0]) for p in POOLS)
    m["degraded"] = sum(p["degraded"] for p in pgs)
    m["backfilling"] = any("backfilling" in p["state"] for p in pgs)

    # ---- capacity
    live = [o for o in osds if o["up"] and o["inn"]]
    m["total"] = len([o for o in osds if o["up"]]) * OSD_BYTES
    m["used"] = sum(o["used"] * OSD_BYTES for o in osds if o["up"])
    tw = sum(o["weight"] for o in live) or 1
    m["max_avail"] = min([max(0.0, (FULL - o["used"]) * OSD_BYTES) / (o["weight"] / tw) for o in live] or [0]) / 3

    # ---- client io (bytes/s, ops/s)
    rd, wr = jitter(0.9, 1.4, "rd") * 2**30, jitter(1.8, 2.6, "wr") * 2**30
    if first(s, "slow-ops"):
        wr *= 0.3
    if any(o["used"] >= FULL and o["up"] for o in osds):
        wr = 0
    m["io"] = dict(rd=rd, wr=wr, rd_ops=rd / 2**17, wr_ops=wr / 2**18)
    m["checks"] = health_checks(s, m)
    m["status"] = ("HEALTH_ERR" if any(c[1] == "ERR" for c in m["checks"])
                   else "HEALTH_WARN" if m["checks"] else "HEALTH_OK")
    return m


def health_checks(s, m):
    now, osds, c = m["now"], m["osds"], []
    if m["flags"]:
        c.append(("OSDMAP_FLAGS", "WRN", "%s flag(s) set" % ",".join(m["flags"]), []))
    din = [o for o in osds if not o["up"] and o["inn"]]
    dhosts = [x for x in HOSTS if host_osds(x) and all(not osds[o]["up"] and osds[o]["inn"] for o in host_osds(x))]
    if dhosts:
        c.append(("OSD_HOST_DOWN", "WRN", "%d host%s (%d osds) down" % (len(dhosts), "s" if len(dhosts) > 1 else "",
                  OSD_PER_HOST * len(dhosts)), ["host %s (root=default) (%d osds) is down" % (x, OSD_PER_HOST) for x in dhosts]))
    if din:
        c.append(("OSD_DOWN", "WRN", "%d osds down" % len(din),
                  ["osd.%d (root=default,host=%s) is down" % (o["id"], o["host"]) for o in din]))
    full = [o for o in osds if o["up"] and o["used"] >= FULL]
    near = [o for o in osds if o["up"] and NEARFULL <= o["used"] < BACKFILLFULL]
    if full:
        c.append(("OSD_FULL", "ERR", "%d full osd(s)" % len(full), ["osd.%d is full" % o["id"] for o in full]))
        c.append(("POOL_FULL", "WRN", "%d pool(s) full" % len(POOLS), ["pool '%s' is full (no space)" % p[1] for p in POOLS]))
    if near:
        c.append(("OSD_NEARFULL", "WRN", "%d nearfull osd(s)" % len(near), ["osd.%d is near full" % o["id"] for o in near]))
        c.append(("POOL_NEARFULL", "WRN", "%d pool(s) nearfull" % len(POOLS), ["pool '%s' is nearfull" % p[1] for p in POOLS]))
    mdown = [x for x in MONS if not m["mons"][x]["up"]]
    if mdown:
        c.append(("MON_DOWN", "WRN", "%d/%d mons down, quorum %s" % (len(mdown), len(MONS), ",".join(x for x in MONS if x not in mdown)),
                  ["mon.%s (rank %d) addr [v2:%s:3300/0,v1:%s:6789/0] is down (out of quorum)" % (x, MONS.index(x), pub_ip(x), pub_ip(x))
                   for x in mdown]))
    skew = [x for x in MONS if m["mons"][x]["skew"] > 0.05 and m["mons"][x]["up"]]
    if skew:
        c.append(("MON_CLOCK_SKEW", "WRN", "clock skew detected on %s" % ", ".join("mon." + x for x in skew),
                  ["mon.%s clock skew %.5gs > max 0.05s (latency 0.00%ds)" % (x, m["mons"][x]["skew"], 100 + h("l", x) % 50)
                   for x in skew]))
    degpgs = [p for p in m["pgs"] if "degraded" in p["state"]]
    if degpgs:
        und = [p for p in degpgs if "undersized" in p["state"]]
        summ = "Degraded data redundancy: %d/%d objects degraded (%.3f%%), %d pgs degraded" % (
            m["degraded"], m["objects"] * 3, 100.0 * m["degraded"] / (m["objects"] * 3), len(degpgs))
        if und:
            summ += ", %d pgs undersized" % len(und)
        det = []
        for p in degpgs[:50]:
            age = now - p["since"] if p["since"] else 0
            if "undersized" in p["state"] and age > 60:
                det.append("pg %s is stuck undersized for %s, current state %s, last acting [%s]"
                           % (p["pgid"], fmt_age(age), p["state"], ",".join(map(str, p["acting"]))))
            else:
                det.append("pg %s is %s, acting [%s]" % (p["pgid"], p["state"], ",".join(map(str, p["acting"]))))
        if len(degpgs) > 50:
            det.append("...")
        c.append(("PG_DEGRADED", "WRN", summ, det))
    so = first(s, "slow-ops")
    if so:
        n, old = int(jitter(18, 64, "nslow")), int(jitter(31, 97, "oldslow"))
        c.append(("SLOW_OPS", "WRN", "%d slow ops, oldest one blocked for %d sec, osd.%d has slow ops" % (n, old, so["target"]), []))
        c.append(("BLUESTORE_SLOW_OP_ALERT", "WRN", "1 OSD(s) experiencing slow operations in BlueStore",
                  ["osd.%d observed slow operation indications in BlueStore" % so["target"]]))
    inc = [p for p in m["pgs"] if "inconsistent" in p["state"]]
    if inc:
        c.append(("OSD_SCRUB_ERRORS", "ERR", "2 scrub errors", []))
        c.append(("PG_DAMAGED", "ERR", "Possible data damage: 1 pg inconsistent",
                  ["pg %s is %s, acting [%s]" % (p["pgid"], p["state"], ",".join(map(str, p["acting"]))) for p in inc]))
    mtu = first(s, "mtu-mismatch")
    if mtu and m["now"] - mtu["since"] > 60:
        bad = host_osds(mtu["target"])
        peers = [o for o in range(NOSD) if o not in bad]
        lines = []
        for i, o in enumerate(bad + bad[:2]):
            p = peers[h("peer", o, i) % len(peers)]
            lines.append((jitter(9000, 21000, "ping", o, i), o, p))
        lines.sort(reverse=True)
        c.append(("OSD_SLOW_PING_TIME_BACK", "WRN", "Slow OSD heartbeats on back (longest %.3fms)" % lines[0][0],
                  ["Slow OSD heartbeats on back from osd.%d [%s] to osd.%d [%s] %.3f msec" % (o, osd_host(o), p, osd_host(p), ms)
                   for ms, o, p in lines]))
    if m["crashes"]:
        c.append(("RECENT_CRASH", "WRN", "%d daemons have recently crashed" % len(m["crashes"]),
                  ["%s crashed on host %s at %s" % (x["entity"], x["host"], iso(x["t"])) for x in m["crashes"]]))
    if any(not m["hosts"][x]["up"] for x in MDSS):
        c.append(("MDS_INSUFFICIENT_STANDBY", "WRN", "insufficient standby MDS daemons available",
                  ["have 0; want 1 more"]))
    return sorted(c)


# ---------------------------------------------------------------------------------------------
# `ceph` CLI
# ---------------------------------------------------------------------------------------------
def osd_pgcount(m, o):
    return int(sum(1 for p in m["pgs"] if o["id"] in p["up"]) * o["pgmul"])


def quorum(m):
    return [x for x in MONS if m["mons"][x]["up"]]


def active_mgr(m):
    live = [x for x in MGRS if m["hosts"][x]["up"]]
    return live[0] if live else None


def pg_state_counts(m):
    cnt = {}
    for p in m["pgs"]:
        cnt[p["state"]] = cnt.get(p["state"], 0) + 1
    return sorted(cnt.items(), key=lambda kv: -kv[1])


def io_line(m):
    io = m["io"]
    return "%s/s rd, %s/s wr, %.2fk op/s rd, %.2fk op/s wr" % (
        fmt_bytes(io["rd"]), fmt_bytes(io["wr"]), io["rd_ops"] / 1000, io["wr_ops"] / 1000)


def ceph_status(s, m):
    now, osds = m["now"], m["osds"]
    L = ["  cluster:", "    id:     %s" % FSID, "    health: %s" % m["status"]]
    L += ["            %s" % c[2] for c in m["checks"]]
    q = quorum(m)
    mon_since = max([UPGRADED_AT] + [f["since"] for f in s["faults"] if f["type"] in ("host-down", "mon-disk-full")
                                     and f["target"] in MONS])
    monl = "    mon: %d daemons, quorum %s (age %s)" % (len(MONS), ",".join(q), fmt_age(now - mon_since))
    if len(q) < len(MONS):
        monl += ", out of quorum: %s" % ",".join(x for x in MONS if x not in q)
    am = active_mgr(m)
    standbys = [mgr_name(x) for x in MGRS if x != am and m["hosts"][x]["up"]]
    mgr_since = UPGRADED_AT if am == MGRS[0] else first(s, "host-down")["since"]
    mgrl = "    mgr: %s(active, since %s)" % (mgr_name(am), fmt_age(now - mgr_since))
    if standbys:
        mgrl += ", standbys: %s" % ", ".join(standbys)
    nstandby = sum(1 for x in MDSS if m["hosts"][x]["up"]) - 1
    up = [o for o in osds if o["up"]]
    inn = [o for o in osds if o["inn"]]
    up_since = max([UPGRADED_AT] + [o["down_since"] for o in osds if o["down_since"]] +
                   [f["since"] + 60 + ((now - f["since"] - 60) // 240) * 240 + 45 for f in faults(s, "mtu-mismatch")
                    if now - f["since"] > 60 and (now - f["since"] - 60) % 240 >= 45])
    in_since = max([BUILT_AT] + [o["out_since"] for o in osds if o["out_since"]])
    L += ["", "  services:", monl, mgrl,
          "    mds: 1/1 daemons up%s" % (", 1 standby" if nstandby > 0 else ""),
          "    osd: %d osds: %d up (since %s), %d in (since %s)%s" % (
              NOSD, len(up), fmt_age(now - up_since), len(inn), fmt_age(now - in_since),
              "; %d remapped pgs" % sum(1 for p in m["pgs"] if "remapped" in p["state"])
              if any("remapped" in p["state"] for p in m["pgs"]) else ""),
          "", "  data:", "    volumes: 1/1 healthy",
          "    pools:   %d pools, %d pgs" % (len(POOLS), len(m["pgs"])),
          "    objects: %.2fM objects, %s" % (m["objects"] / 1e6, fmt_bytes(STORED)),
          "    usage:   %s used, %s / %s avail" % (fmt_bytes(m["used"]), fmt_bytes(m["total"] - m["used"]), fmt_bytes(m["total"]))]
    counts = pg_state_counts(m)
    first_line = True
    if m["degraded"]:
        L.append("    pgs:     %d/%d objects degraded (%.3f%%)" % (m["degraded"], m["objects"] * 3,
                                                               100.0 * m["degraded"] / (m["objects"] * 3)))
        first_line = False
    for st, n in counts:
        L.append(("    pgs:     " if first_line else "             ") + "%d %s" % (n, st))
        first_line = False
    L += ["", "  io:", "    client:   " + io_line(m)]
    if m["backfilling"]:
        L.append("    recovery: %s/s, %d objects/s" % (fmt_bytes(jitter(1.4, 2.1, "rec") * 2**30), int(jitter(380, 520, "reco"))))
    return "\n".join(L) + "\n\n"


def ceph_health(m, detail):
    if not m["checks"]:
        return "HEALTH_OK\n"
    out = ["%s %s" % (m["status"], "; ".join(c[2] for c in m["checks"]))]
    if detail:
        for code, sev, summ, det in m["checks"]:
            out.append("[%s] %s: %s" % (sev, code, summ))
            out += ["    " + d for d in det]
    return "\n".join(out) + "\n"


def osd_tree(m):
    L = ["ID   CLASS  WEIGHT     TYPE NAME         STATUS  REWEIGHT  PRI-AFF"]
    tot = sum(o["weight"] for o in m["osds"])
    L.append(" -1         %9.5f  root default                               " % tot)
    for i, x in enumerate(HOSTS):
        os_ = [m["osds"][o] for o in host_osds(x)]
        L.append("%3d         %9.5f      host %s                           " % (-3 - 2 * i, sum(o["weight"] for o in os_), x))
        for o in os_:
            L.append("%3d   nvme  %9.5f          osd.%-2d      %4s   %7.5f  1.00000" % (
                o["id"], o["weight"], o["id"], "up" if o["up"] else "down", 1.0 if o["inn"] else 0.0))
    return "\n".join(L) + "\n"


def osd_df(m, tree=False):
    hdr = "ID  CLASS  WEIGHT   REWEIGHT  SIZE     RAW USE  DATA     OMAP     META     AVAIL    %USE   VAR   PGS  STATUS"
    rows, used, size = [], 0, 0
    live = [o for o in m["osds"] if o["up"]]
    avg = sum(o["used"] for o in live) / max(1, len(live))
    vars_ = []
    for o in m["osds"]:
        if not o["up"]:
            rows.append((o, "%2d   nvme  %7.5f   %7.5f      0 B      0 B      0 B      0 B      0 B      0 B      0     0    0    down"
                         % (o["id"], o["weight"], 1.0 if o["inn"] else 0.0)))
            continue
        u = o["used"] * OSD_BYTES
        meta = (9.5 + h("meta", o["id"]) % 30 / 10.0) * 2**30
        omap = (8 + h("omap", o["id"]) % 9) * 2**20
        used += u
        size += OSD_BYTES
        var = o["used"] / avg
        vars_.append(var)
        rows.append((o, "%2d   nvme  %7.5f   1.00000  %7s  %7s  %7s  %7s  %7s  %7s  %5.2f  %4.2f  %3d      up" % (
            o["id"], o["weight"], fmt_bytes(OSD_BYTES), fmt_bytes(u), fmt_bytes(u - meta), fmt_bytes(omap), fmt_bytes(meta),
            fmt_bytes(OSD_BYTES - u), o["used"] * 100, var, osd_pgcount(m, o))))
    L = [hdr]
    if tree:
        for x in HOSTS:
            L.append("%-90s host %s" % ("", x))
            L += [r for o, r in rows if o["host"] == x]
    else:
        L += [r for o, r in rows]
    mean = sum(o["used"] for o in live) / max(1, len(live)) * 100
    sd = (sum((o["used"] * 100 - mean) ** 2 for o in live) / max(1, len(live))) ** 0.5
    L.append("                       TOTAL  %7s  %7s  %7s  %7s  %7s  %7s  %5.2f" % (
        fmt_bytes(size), fmt_bytes(used), fmt_bytes(used * 0.997), fmt_bytes(290 * 2**20), fmt_bytes(used * 0.003),
        fmt_bytes(size - used), 100.0 * used / max(1, size)))
    L.append("MIN/MAX VAR: %.2f/%.2f  STDDEV: %.2f" % (min(vars_), max(vars_), sd))
    return "\n".join(L) + "\n"


def osd_perf(m):
    L = ["osd  commit_latency(ms)  apply_latency(ms)"]
    for o in reversed(m["osds"]):
        L.append("%3d  %18d  %17d" % (o["id"], o["commit"], o["apply"]))
    return "\n".join(L) + "\n"


def ceph_df(m):
    tot, used = m["total"], m["used"]
    L = ["--- RAW STORAGE ---", "CLASS     SIZE    AVAIL    USED  RAW USED  %RAW USED"]
    for c in ("nvme", "TOTAL"):
        L.append("%-5s  %7s  %7s  %6s  %8s  %9.2f" % (c, fmt_bytes(tot), fmt_bytes(tot - used), fmt_bytes(used), fmt_bytes(used),
                                                   100.0 * used / tot))
    L += ["", "--- POOLS ---", "POOL              ID  PGS   STORED  OBJECTS     USED  %USED  MAX AVAIL"]
    for pid, name, n, _, _ in POOLS:
        st = pool_stored(pid)
        pct = 100.0 * st / (st + m["max_avail"]) if st + m["max_avail"] else 0
        L.append("%-16s %3d  %3d  %7s  %7s  %7s  %5.2f  %9s" % (
            name, pid, n, fmt_bytes(st), ("%.2fM" % (pool_objects(pid) / 1e6)) if pool_objects(pid) > 1e6 else pool_objects(pid),
            fmt_bytes(st * 3), pct, fmt_bytes(m["max_avail"])))
    return "\n".join(L) + "\n"


def pool_lines():
    L = []
    for pid, name, n, _, app in POOLS:
        L.append("pool %d '%s' replicated size 3 min_size 2 crush_rule 0 object_hash rjenkins pg_num %d pgp_num %d "
                 "autoscale_mode %s last_change %d flags hashpspool%s stripe_width 0 application %s"
                 % (pid, name, n, n, "on" if pid != 3 else "warn", 3100 + pid * 7,
                    ",bulk" if pid == 3 else "", app))
    return L


def osd_dump(m):
    fl = ["sortbitwise", "recovery_deletes", "purged_snapdirs", "pglog_hardlimit"] + m["flags"]
    L = ["epoch %d" % osdmap_epoch(m), "fsid %s" % FSID, "created %s" % iso(BUILT_AT).replace("Z", "+0000"),
         "modified %s" % iso(m["now"] - 30).replace("Z", "+0000"), "flags %s" % ",".join(fl),
         "crush_version 87", "full_ratio %g" % FULL, "backfillfull_ratio %g" % BACKFILLFULL, "nearfull_ratio %g" % NEARFULL,
         "require_min_compat_client luminous", "min_compat_client jewel", "require_osd_release squid", "stretch_mode_enabled false"]
    L += pool_lines() + ["max_osd %d" % NOSD]
    for o in m["osds"]:
        x = o["host"]
        L.append("osd.%d %s %s  weight %s up_from %d up_thru %d down_at %d last_clean_interval [%d,%d) "
                 "[v2:%s:%d/%d,v1:%s:%d/%d] [v2:%s:%d/%d,v1:%s:%d/%d] exists%s %s" % (
                     o["id"], "up" if o["up"] else "down", "in" if o["inn"] else "out", "1" if o["inn"] else "0",
                     3990, 4120, 3988, 3101, 3987, pub_ip(x), 6800 + o["id"] % 4 * 2, 1000 + o["id"], pub_ip(x),
                     6801 + o["id"] % 4 * 2, 1000 + o["id"], clu_ip(x), 6804 + o["id"] % 4 * 2, 1000 + o["id"], clu_ip(x),
                     6805 + o["id"] % 4 * 2, 1000 + o["id"], ",up" if o["up"] else "", uid("osd", o["id"])))
    return "\n".join(L) + "\n"


def osdmap_epoch(m):
    return 4127 + sum(3 for o in m["osds"] if not o["up"]) + sum(2 for o in m["osds"] if not o["inn"])


def pg_stat(m):
    st = ", ".join("%d %s" % (n, k) for k, n in pg_state_counts(m))
    io = m["io"]
    out = "%d pgs: %s; %s data, %s used, %s / %s avail; %s/s rd, %s/s wr, %.1fk op/s" % (
        len(m["pgs"]), st, fmt_bytes(STORED), fmt_bytes(m["used"]), fmt_bytes(m["total"] - m["used"]), fmt_bytes(m["total"]),
        fmt_bytes(io["rd"]), fmt_bytes(io["wr"]), (io["rd_ops"] + io["wr_ops"]) / 1000)
    if m["degraded"]:
        out += "; %d/%d objects degraded (%.3f%%)" % (m["degraded"], m["objects"] * 3, 100.0 * m["degraded"] / (m["objects"] * 3))
    return out + "\n"


def pg_dump_stuck(m, kind):
    kind = kind or "unclean"
    keep = []
    for p in m["pgs"]:
        if not p["since"] or m["now"] - p["since"] < 60:
            continue
        st = p["state"]
        if (kind == "unclean" and "clean" not in st) or (kind == "inactive" and "active" not in st) or kind in st.split("+"):
            keep.append(p)
    if not keep:
        return "ok\n"
    L = ["PG_STAT  STATE%s  UP          UP_PRIMARY  ACTING      ACTING_PRIMARY" % (" " * 47)]
    for p in keep:
        L.append("%-7s  %-52s  %-10s  %10d  %-10s  %14d" % (p["pgid"], p["state"], "[%s]" % ",".join(map(str, p["up"])), p["up"][0],
                                                          "[%s]" % ",".join(map(str, p["acting"])), p["acting"][0]))
    return "\n".join(L) + "\nok\n"


def pg_ls(m, states):
    L = ["PG       OBJECTS  DEGRADED  MISPLACED  UNFOUND  BYTES       STATE%s  UP          ACTING" % (" " * 47)]
    for p in m["pgs"]:
        if states and not all(x in p["state"].split("+") for x in states):
            continue
        b = int(pool_stored(p["pool"]) / [x[2] for x in POOLS if x[0] == p["pool"]][0])
        L.append("%-7s  %7d  %8d  %9d  %7d  %-10s  %-52s  %-10s  %s" % (
            p["pgid"], p["objects"], p["degraded"], 0, 0, b, p["state"], "[%s]p%d" % (",".join(map(str, p["up"])), p["up"][0]),
            "[%s]p%d" % (",".join(map(str, p["acting"])), p["acting"][0])))
    return "\n".join(L) + "\n"


def list_inconsistent(s, m, pgid):
    inc = first(s, "pg-inconsistent")
    if not inc or inconsistent_pg(inc["target"])[0] != pgid:
        return '{"epoch":%d,"inconsistents":[]}\n' % osdmap_epoch(m)
    _, acting = inconsistent_pg(inc["target"])
    obj = "1000000b2c1.%08x" % (h("obj", pgid) % 0x10000)
    shards = []
    for o in acting:
        sh = {"osd": o, "primary": o == acting[0], "errors": [], "size": 4194304}
        if o == inc["target"]:
            sh["errors"] = ["read_error"]
        else:
            sh["omap_digest"] = "0xffffffff"
            sh["data_digest"] = "0x%08x" % (h("dd", pgid) % (1 << 32))
        shards.append(sh)
    doc = {"epoch": osdmap_epoch(m), "inconsistents": [{
        "object": {"name": obj, "nspace": "", "locator": "", "snap": "head", "version": 88213},
        "errors": [], "union_shard_errors": ["read_error"],
        "selected_object_info": {"oid": {"oid": obj, "key": "", "snapid": -2, "hash": h("hash", obj) % (1 << 32),
                                         "max": 0, "pool": 3, "namespace": ""}, "size": 4194304},
        "shards": shards}]}
    return json.dumps(doc, indent=4) + "\n"


def mon_stat(m):
    q = quorum(m)
    addrs = ",".join("%s=[v2:%s:3300/0,v1:%s:6789/0]" % (x, pub_ip(x), pub_ip(x)) for x in MONS)
    return "e3: 3 mons at {%s} removed_ranks: {} disallowed_leaders: {}, election epoch %d, leader %d %s, quorum %s %s\n" % (
        addrs, 42 + 2 * (3 - len(q)), MONS.index(q[0]), q[0], ",".join(str(MONS.index(x)) for x in q), ",".join(q))


def time_sync(m):
    skew = {}
    for x in MONS:
        if m["mons"][x]["up"]:
            sk = m["mons"][x]["skew"] or jitter(0, 0.0009, "sk", x)
            skew[x] = {"skew": round(sk, 6), "latency": 0.001 + h("l", x) % 50 / 100000.0,
                       "health": "HEALTH_WARN" if sk > 0.05 else "HEALTH_OK"}
            if sk > 0.05:
                skew[x]["details"] = "clock skew %.5gs > max 0.05s" % sk
    skew[quorum(m)[0]]["skew"] = 0.0
    return json.dumps({"time_skew_status": skew, "timechecks": {"epoch": 42, "round": 1288, "round_status": "finished"}},
                      indent=4) + "\n"


def crash_ls(m, new_only=False):
    L = ["ID                                                                ENTITY  NEW"]
    for c in m["crashes"]:
        L.append("%-64s  %-6s   *" % (c["id"], c["entity"]))
    return "\n".join(L) + "\n" if m["crashes"] or not new_only else "\n"


def crash_info(m, cid):
    for c in m["crashes"]:
        if c["id"] == cid or c["id"].startswith(cid):
            o = int(c["entity"].split(".")[1])
            if c["type"] == "disk-fail":
                func, line, msg = ("void KernelDevice::_aio_thread()", "./src/blk/kernel/KernelDevice.cc",
                                   "ceph_assert(r == 0 && \"Unexpected IO error. This may suggest a hardware issue. "
                                   "Please check your kernel log!\")")
                bt = ["(KernelDevice::_aio_thread()+0x1258) [0x55d1c2a1e8b8]", "(KernelDevice::AioCompletionThread::entry()+0x11)"]
            else:
                func, line, msg = ("int PrimaryLogPG::recover_missing(const hobject_t&, eversion_t, int, PGBackend::RecoveryHandle*)",
                                   "./src/osd/PrimaryLogPG.cc", "ceph_assert(head_obc)")
                bt = ["(PrimaryLogPG::recover_missing(hobject_t const&, eversion_t, int, PGBackend::RecoveryHandle*)+0x7c1)",
                      "(PrimaryLogPG::maybe_kick_recovery(hobject_t const&)+0x1a2)", "(PrimaryLogPG::do_op(boost::intrusive_ptr<OpRequest>&)+0x2b54)"]
            doc = {"assert_condition": msg.replace("ceph_assert(", "")[:-1], "assert_file": line, "assert_func": func,
                   "assert_line": 12034 if "Primary" in line else 604,
                   "assert_msg": "%s: In function '%s' thread 7f2a1b7fe640 time %s\n%s: FAILED %s\n" % (
                       line, func, iso(c["t"]).replace("Z", "+0000"), line, msg),
                   "assert_thread_name": "tp_osd_tp" if "Primary" in line else "bstore_aio",
                   "backtrace": ["/lib/x86_64-linux-gnu/libc.so.6(+0x42520) [0x7f2a3c842520]", "pthread_kill()",
                                 "raise()", "abort()", "(ceph::__ceph_assert_fail(char const*, char const*, int, char const*)+0x18a)"] + bt,
                   "ceph_version": VERSION, "crash_id": c["id"], "entity_name": c["entity"], "os_id": "centos",
                   "os_name": "CentOS Stream", "os_version": "9", "process_name": "ceph-osd", "stack_sig": "%064x" % (h("sig", c["type"]) % (1 << 256)),
                   "timestamp": iso(c["t"]), "utsname_hostname": c["host"], "utsname_release": "6.8.0-87-generic",
                   "utsname_machine": "x86_64", "utsname_sysname": "Linux", "utsname_version": "#88-Ubuntu SMP PREEMPT_DYNAMIC"}
            return json.dumps(doc, indent=4) + "\n"
    return "Error ENOENT: crash %s not found\n" % cid


def orch_ps(m, args):
    dtype = args[args.index("--daemon-type") + 1] if "--daemon-type" in args else None
    hostf = next((a for a in args if a in HOSTS), None)
    L = ["NAME                       HOST     PORTS             STATUS         REFRESHED  AGE  MEM USE  MEM LIM  VERSION    IMAGE ID      CONTAINER ID"]
    rows = []
    for x in MONS:
        rows.append(("mon", "mon.%s" % x, x, "", m["mons"][x]["up"], 1.1 + h("mm", x) % 5 / 10.0, "2048M"))
    for x in MGRS:
        rows.append(("mgr", "mgr.%s" % mgr_name(x), x, "*:9283,8765", True, 0.6 + h("mg", x) % 4 / 10.0, "-"))
    for x in MDSS:
        rows.append(("mds", "mds.%s" % mds_name(x), x, "", True, 2.4, "-"))
    for o in m["osds"]:
        rows.append(("osd", "osd.%d" % o["id"], o["host"], "", o["daemon"] == "running", 3.1 + h("om", o["id"]) % 8 / 10.0, "4096M"))
    for x in HOSTS:
        rows.append(("node-exporter", "node-exporter.%s" % x, x, "*:9100", True, 0.02, "-"))
        rows.append(("crash", "crash.%s" % x, x, "", True, 0.01, "-"))
    for t, name, x, ports, ok, mem, lim in rows:
        if (dtype and t != dtype) or (hostf and x != hostf):
            continue
        hostup = m["hosts"][x]["up"]
        if not hostup:
            status, ref, memu, ver, cid = "unknown", fmt_age(m["now"] - first_host_down(m)) + " ago", "-", VERSION, "%012x" % (h("cid", name) % (1 << 48))
        elif not ok:
            status, ref, memu, ver, cid = "error", "%dm ago" % (1 + h("r", name) % 9), "-", "<unknown>", "<unknown>"
        else:
            status, ref, memu, ver, cid = "running (%s)" % fmt_age(m["now"] - UPGRADED_AT), "%dm ago" % (1 + h("r", name) % 9), \
                ("%.1fG" % mem) if mem >= 1 else ("%dM" % (mem * 1000)), VERSION, "%012x" % (h("cid", name) % (1 << 48))
        L.append("%-26s %-8s %-17s %-14s %-10s %-4s %7s  %7s  %-9s  %-12s  %s" % (
            name, x, ports, status, ref, fmt_age(m["now"] - BUILT_AT), memu, lim, ver,
            IMAGE_ID if ver != "<unknown>" else "<unknown>", cid))
    return "\n".join(L) + "\n"


def first_host_down(m):
    return min((o["down_since"] for o in m["osds"] if o["daemon"] == "unknown"), default=m["now"])


def orch_host_ls(m):
    labels = {"ceph-01": "_admin,mon,mgr,osd", "ceph-02": "_admin,mon,mgr,osd", "ceph-03": "mon,osd",
              "ceph-04": "mds,osd", "ceph-05": "mds,osd", "ceph-06": "osd"}
    L = ["HOST     ADDR        LABELS              STATUS"]
    for x in HOSTS:
        L.append("%-8s %-11s %-19s %s" % (x, pub_ip(x), labels[x], "" if m["hosts"][x]["up"] else "Offline"))
    return "\n".join(L).rstrip() + "\n%d hosts in cluster\n" % len(HOSTS)


def device_ls(s, m):
    L = ["DEVICE                                    HOST:DEV          DAEMONS  WEAR  LIFE EXPECTANCY"]
    for o in m["osds"]:
        if first(s, "disk-fail") and first(s, "disk-fail")["target"] == o["id"]:
            continue
        devid = "%s_S6CRNG0W%06d" % (DISK_MODEL.replace(" ", "_"), h("sn", o["id"]) % 10**6)
        wear = wear_pct(s, o["id"])
        L.append("%-41s %-17s %-8s %3d%%  %s" % (devid, "%s:%s" % (o["host"], o["dev"]), "osd.%d" % o["id"], wear,
                                                  ">%s" % time.strftime("%Y-%m-%d", time.gmtime(m["now"] + 180 * 86400)) if wear < 90 else ""))
    return "\n".join(L) + "\n"


def wear_pct(s, o):
    so = first(s, "slow-ops")
    if so and so["target"] == o:
        return 104
    return 4 + h("wear", o) % 6


def fs_status(m):
    act = MDSS[0] if m["hosts"][MDSS[0]]["up"] else MDSS[1]
    stby = [x for x in MDSS if x != act and m["hosts"][x]["up"]]
    L = ["ckpt - 8 clients", "====",
         "RANK  STATE            MDS               ACTIVITY     DNS    INOS   DIRS   CAPS",
         " 0    active  %s  Reqs: %4d /s  1214k  1208k  88.1k   402k" % (mds_name(act), int(jitter(250, 600, "mdsreq"))),
         "       POOL           TYPE     USED  AVAIL",
         "cephfs.ckpt.meta  metadata  %6s  %6s" % (fmt_bytes(pool_stored(4) * 3), fmt_bytes(m["max_avail"])),
         "cephfs.ckpt.data    data    %6s  %6s" % (fmt_bytes(pool_stored(3) * 3), fmt_bytes(m["max_avail"]))]
    if stby:
        L += ["    STANDBY MDS", "%s" % mds_name(stby[0])]
    L.append("MDS version: %s" % VSTRING)
    return "\n".join(L) + "\n"


def config_dump(s):
    rows = [("global", "basic", "container_image", "quay.io/ceph/ceph@sha256:%s" % ("%064x" % (h("img") % (1 << 256))), "*"),
            ("global", "advanced", "mon_osd_down_out_interval", "600", ""),
            ("global", "advanced", "osd_pool_default_size", "3", ""),
            ("global", "advanced", "public_network", "10.10.0.0/24", "*"),
            ("global", "advanced", "cluster_network", "10.20.0.0/24", "*"),
            ("mon", "advanced", "auth_allow_insecure_global_id_reclaim", "false", ""),
            ("mgr", "advanced", "mgr/cephadm/container_init", "True", "*"),
            ("mgr", "advanced", "mgr/dashboard/ssl_server_port", "8443", "*"),
            ("osd", "advanced", "osd_memory_target_autotune", "true", ""),
            ("osd", "advanced", "osd_mclock_profile", "balanced", ""),
            ("mds", "advanced", "mds_cache_memory_limit", "17179869184", "")]
    md = first(s, "mon-disk-full")
    if md:
        rows.append(("mon.%s" % md["target"], "advanced", "debug_mon", "20/20", ""))
        rows.append(("mon.%s" % md["target"], "advanced", "debug_paxos", "20/20", ""))
    L = ["WHO           MASK  LEVEL     OPTION                                 VALUE                                                                    RO"]
    for who, lvl, opt, val, ro in rows:
        L.append("%-13s       %-9s %-38s %-72s %s" % (who, lvl, opt, val, ro))
    return "\n".join(L) + "\n"


def balancer_status(m):
    return json.dumps({"active": m["balancer"], "last_optimize_duration": "0:00:00.004211" if m["balancer"] else "",
                       "last_optimize_started": ts(m["now"] - 33) if m["balancer"] else "",
                       "mode": "upmap" if m["balancer"] else "none",
                       "no_optimization_needed": m["balancer"], "optimize_result":
                       "Unable to find further optimization, or pool(s) pg_num is decreasing, or distribution is already perfect"
                       if m["balancer"] else "", "plans": []}, indent=4) + "\n"


def osd_metadata(m, o):
    x = osd_host(o)
    d = {"id": o, "arch": "x86_64", "back_addr": "[v2:%s:%d/%d,v1:%s:%d/%d]" % (clu_ip(x), 6804 + o % 4 * 2, 1000 + o, clu_ip(x), 6805 + o % 4 * 2, 1000 + o),
         "back_iface": CLUSTER_IF, "bluestore_bdev_dev_node": "/dev/dm-%d" % (o % 4), "bluestore_bdev_devices": osd_dev(o),
         "bluestore_bdev_size": str(OSD_BYTES), "bluestore_bdev_type": "ssd", "ceph_version": VSTRING,
         "ceph_version_short": VERSION, "container_hostname": x, "container_image": "quay.io/ceph/ceph:v%s" % VERSION,
         "default_device_class": "ssd", "device_ids": "%s=%s_S6CRNG0W%06d" % (osd_dev(o), DISK_MODEL.replace(" ", "_"), h("sn", o) % 10**6),
         "devices": osd_dev(o), "front_addr": "[v2:%s:%d/%d,v1:%s:%d/%d]" % (pub_ip(x), 6800 + o % 4 * 2, 1000 + o, pub_ip(x), 6801 + o % 4 * 2, 1000 + o),
         "front_iface": PUBLIC_IF, "hostname": x, "kernel_version": "6.8.0-87-generic", "mem_total_kb": "263597468",
         "objectstore": "bluestore", "osd_objectstore": "bluestore", "rotational": "0"}
    return json.dumps(d, indent=4) + "\n"


MUTATING = {"set", "unset", "out", "in", "reweight", "reweight-by-utilization", "repair", "restart", "archive", "archive-all",
            "rm", "primary-affinity", "set-full-ratio", "set-nearfull-ratio", "on", "off", "mode", "deep-scrub",
            "scrub", "redeploy", "start", "stop", "purge", "destroy", "enter", "exit", "apply", "add", "drain"}


def cmd_ceph(s, node, now, args):
    m = model(s, now)
    a = [x for x in args if x not in ("--cluster", "ceph", "-f", "plain", "--format")]
    j = " ".join(a)
    if a and any(x in MUTATING for x in a[1:3]) and not j.startswith("osd dump") and j not in ("balancer status",):
        if not (a[0] == "orch" and a[1:2] == ["ps"]):
            return ("[sim] This simulator is read-only, so '%s' changes nothing here.\n"
                    "      Write down your fix and the command you'd run, then: ./lab.sh ceph reveal\n" % ("ceph " + j))
    if not a or a[0] in ("-s", "status"):
        return ceph_status(s, m)
    if a[0] == "health":
        return ceph_health(m, "detail" in a)
    if a[0] == "-v" or a[0] == "--version" or a[0] == "version":
        return VSTRING + "\n"
    if a[0] == "versions":
        return json.dumps({k: {VSTRING: n} for k, n in [("mon", len(quorum(m))), ("mgr", len([x for x in MGRS if m["hosts"][x]["up"]])),
                                                       ("osd", sum(1 for o in m["osds"] if o["up"])), ("mds", 2)]}, indent=4) + "\n"
    if a[0] == "osd":
        sub = a[1] if len(a) > 1 else ""
        if sub == "tree":
            return osd_tree(m)
        if sub == "df":
            return osd_df(m, "tree" in a)
        if sub == "perf":
            return osd_perf(m)
        if sub == "stat":
            up, inn = sum(o["up"] for o in m["osds"]), sum(o["inn"] for o in m["osds"])
            return "%d osds: %d up (since %s), %d in (since %s); epoch: e%d%s\n" % (
                NOSD, up, "4m" if up < NOSD else fmt_age(now - UPGRADED_AT), inn, fmt_age(now - BUILT_AT), osdmap_epoch(m),
                ("; flags " + ",".join(m["flags"])) if m["flags"] else "")
        if sub == "dump":
            return osd_dump(m)
        if sub == "pool" and "ls" in a:
            return "\n".join(pool_lines() if "detail" in a else [p[1] for p in POOLS]) + "\n"
        if sub in ("metadata", "find") and len(a) > 2:
            o = int(a[2].replace("osd.", ""))
            if sub == "find":
                return json.dumps({"osd": o, "addrs": {}, "osd_fsid": uid("osd", o), "host": osd_host(o),
                                   "crush_location": {"host": osd_host(o), "root": "default"}}, indent=4) + "\n"
            return osd_metadata(m, o)
        if sub == "crush" and "rule" in a:
            return "replicated_rule\n"
    if a[0] == "df":
        return ceph_df(m)
    if a[0] == "pg":
        sub = a[1] if len(a) > 1 else ""
        if sub == "stat":
            return pg_stat(m)
        if sub == "dump_stuck":
            return pg_dump_stuck(m, a[2] if len(a) > 2 else None)
        if sub == "ls":
            return pg_ls(m, a[2:])
        if sub == "map" and len(a) > 2:
            p = next((p for p in m["pgs"] if p["pgid"] == a[2]), None)
            if not p:
                return "Error ENOENT: pg %s does not exist\n" % a[2]
            return "osdmap e%d pg %s (%s) -> up [%s] acting [%s]\n" % (osdmap_epoch(m), p["pgid"], p["pgid"],
                                                                      ",".join(map(str, p["up"])), ",".join(map(str, p["acting"])))
    if a[0] == "mon" and a[1:2] == ["stat"] or a[0] == "quorum_status":
        return mon_stat(m)
    if a[0] == "time-sync-status":
        return time_sync(m)
    if a[0] == "crash":
        if a[1:2] in (["ls"], ["ls-new"]):
            return crash_ls(m, a[1] == "ls-new")
        if a[1:2] == ["info"] and len(a) > 2:
            return crash_info(m, a[2])
    if a[0] == "orch":
        if a[1:2] == ["ps"]:
            return orch_ps(m, a[2:])
        if a[1:3] == ["host", "ls"]:
            return orch_host_ls(m)
    if a[0] == "device" and a[1:2] == ["ls"]:
        return device_ls(s, m)
    if a[0] == "fs" and a[1:2] == ["status"]:
        return fs_status(m)
    if a[0] == "config" and a[1:2] == ["dump"]:
        return config_dump(s)
    if a[0] == "balancer" and a[1:2] == ["status"]:
        return balancer_status(m)
    return "ceph %s: not simulated (try 'help')\n" % j


# ---------------------------------------------------------------------------------------------
# Node-side commands
# ---------------------------------------------------------------------------------------------
def boot_time(host):
    return UPGRADED_AT - 3600 + h("boot", host) % 3000


def target_on(s, ftype, host):
    """The fault of this type that lives on host (OSD faults: an OSD of host; host/mon faults: host itself)."""
    for f in faults(s, ftype):
        if FAULTS[ftype]["kind"] == "osd" and osd_host(f["target"]) == host:
            return f
        if FAULTS[ftype]["kind"] != "osd" and f["target"] == host:
            return f
    return None


def dmesg_lines(s, host, now):
    b = boot_time(host)
    L = [(b + 0.0, "Linux version 6.8.0-87-generic (buildd@lcy02-amd64-090) (x86_64-linux-gnu-gcc-13) #88-Ubuntu SMP PREEMPT_DYNAMIC"),
         (b + 0.0, "Command line: BOOT_IMAGE=/vmlinuz-6.8.0-87-generic root=/dev/sda2 ro"),
         (b + 1.9, "nvme nvme0: pci function 0000:c1:00.0"), (b + 1.9, "nvme nvme1: pci function 0000:c2:00.0"),
         (b + 1.9, "nvme nvme2: pci function 0000:c3:00.0"), (b + 1.9, "nvme nvme3: pci function 0000:c4:00.0")]
    for d in range(4):
        L.append((b + 2.4, "nvme nvme%d: 128/0/0 default/read/poll queues" % d))
    L += [(b + 6.2, "mlx5_core 0000:41:00.0 %s: Link up" % PUBLIC_IF), (b + 6.3, "mlx5_core 0000:41:00.1 %s: Link up" % CLUSTER_IF),
          (b + 9.8, "Bridge firewalling registered"), (b + 12.0, "systemd[1]: Started ceph-%s.target." % FSID)]
    df = target_on(s, "disk-fail", host)
    if df:
        d, t = df["target"] % 4, df["since"]
        L += [(t - 41, "nvme nvme%d: I/O tag 718 (02ce) opcode 0x1 (I/O Cmd) QID 12 timeout, aborting req_op:WRITE(1) size:131072" % d),
              (t - 31, "nvme nvme%d: Abort status: 0x371" % d),
              (t - 11, "nvme nvme%d: I/O tag 718 (02ce) opcode 0x1 (I/O Cmd) QID 12 timeout, reset controller" % d),
              (t - 6, "nvme nvme%d: controller is down; will reset: CSTS=0xffffffff, PCI_STATUS=0xffff" % d),
              (t - 6, "nvme nvme%d: Does your device have a faulty power saving mode enabled?" % d),
              (t - 5, "nvme 0000:c%d:00.0: Unable to change power state from D3cold to D0, device inaccessible" % (d + 1)),
              (t - 4, "nvme nvme%d: Disabling device after reset failure: -19" % d),
              (t - 3, "nvme%dn1: detected capacity change from 15002931888 to 0" % d),
              (t - 2, "I/O error, dev nvme%dn1, sector 9311616528 op 0x1:(WRITE) flags 0x8800 phys_seg 32 prio class 2" % d),
              (t - 1, "Buffer I/O error on dev dm-%d, logical block 0, async page read" % d)]
    so = target_on(s, "slow-ops", host)
    if so:
        d = so["target"] % 4
        t = so["since"] - 1800
        while t < now:
            L.append((t, "nvme nvme%d: I/O tag %d QID %d timeout, completion polled" % (d, 100 + int(t) % 800, 1 + int(t) % 31)))
            t += 290 + h("so", int(t)) % 220
    pi = target_on(s, "pg-inconsistent", host)
    if pi:
        d = pi["target"] % 4
        L += [(pi["since"] - 95, "nvme%dn1: I/O Cmd(0x2) @ LBA 4410023936, 8 blocks, I/O Error (sct 0x2 / sc 0x81) MORE" % d),
              (pi["since"] - 95, "critical medium error, dev nvme%dn1, sector 4410023936 op 0x0:(READ) flags 0x0 phys_seg 1 prio class 2" % d)]
    mt = target_on(s, "mtu-mismatch", host)
    if mt:
        L += [(mt["since"] - 12, "mlx5_core 0000:41:00.1 %s: Link down" % CLUSTER_IF),
              (mt["since"] - 9, "mlx5_core 0000:41:00.1 %s: Link up" % CLUSTER_IF)]
    return sorted(x for x in L if x[0] <= now)


def cmd_dmesg(s, host, now, args):
    b = boot_time(host)
    out = []
    for t, msg in dmesg_lines(s, host, now):
        if "-T" in args:
            out.append("[%s] %s" % (ts(t), msg))
        else:
            out.append("[%12.6f] %s" % (t - b, msg))
    return "\n".join(out) + "\n"


def journal(s, host, now, unit):
    """journalctl / cephadm logs for osd.N or mon.<host>."""
    pfx = "%s ceph-%s-%%s[%d]: " % (host, FSID, 2000 + h("pid", unit) % 30000)
    mo = re.search(r"osd[.@-]?(\d+)", unit)
    L = []
    if mo:
        o = int(mo.group(1))
        if osd_host(o) != host:
            return "-- No entries --\n"
        p = pfx % ("osd-%d" % o)
        L.append((UPGRADED_AT + 40, "%s Started Ceph osd.%d for %s." % (host, o, FSID)))
        L.append((UPGRADED_AT + 52, p + "osd.%d 3990 load_pgs opened %d pgs" % (o, len(pgs_with(o)))))
        L.append((UPGRADED_AT + 53, p + "osd.%d 3990 done with init, starting boot process" % o))
        fs = [f for f in s["faults"] if FAULTS[f["type"]]["kind"] == "osd" and f["target"] == o]
        fs += [f for f in s["faults"] if f["type"] == "mtu-mismatch" and f["target"] == host]
        inc = first(s, "pg-inconsistent")
        if inc and inconsistent_pg(inc["target"])[1][0] == o:
            fs.append(dict(inc, type="_inc_primary"))
        for f in fs:
            t, ty = f["since"], f["type"]
            if ty in ("osd-crash", "disk-fail"):
                if ty == "osd-crash":
                    L += [(t - 1, p + "./src/osd/PrimaryLogPG.cc: In function 'int PrimaryLogPG::recover_missing(const hobject_t&, "
                                      "eversion_t, int, PGBackend::RecoveryHandle*)' thread 7f2a1b7fe640 time %s" % iso(t)),
                          (t - 1, p + "./src/osd/PrimaryLogPG.cc: 12034: FAILED ceph_assert(head_obc)")]
                else:
                    L += [(t - 8, p + "bdev(0x55d1c4f2a000 /var/lib/ceph/osd/ceph-%d/block) _aio_thread got r=-5 ((5) Input/output error)" % o),
                          (t - 1, p + "./src/blk/kernel/KernelDevice.cc: 604: FAILED ceph_assert(r == 0 && \"Unexpected IO error. "
                                      "This may suggest a hardware issue. Please check your kernel log!\")")]
                L += [(t, p + " %s" % VSTRING), (t, p + "*** Caught signal (Aborted) **"),
                      (t, p + " in thread 7f2a1b7fe640 thread_name:%s" % ("tp_osd_tp" if ty == "osd-crash" else "bstore_aio")),
                      (t + 1, "%s systemd[1]: ceph-%s@osd.%d.service: Main process exited, code=exited, status=134/n/a" % (host, FSID, o)),
                      (t + 1, "%s systemd[1]: ceph-%s@osd.%d.service: Failed with result 'exit-code'." % (host, FSID, o))]
                for r in range(1, 6):
                    tr = t + r * 12
                    if ty == "disk-fail":
                        L.append((tr, p + "bluestore(/var/lib/ceph/osd/ceph-%d) _open_bdev: unable to open /var/lib/ceph/osd/ceph-%d/block: "
                                          "(6) No such device or address" % (o, o)))
                    else:
                        L.append((tr, p + "./src/osd/PrimaryLogPG.cc: 12034: FAILED ceph_assert(head_obc)"))
                    L.append((tr + 1, "%s systemd[1]: ceph-%s@osd.%d.service: Scheduled restart job, restart counter is at %d." % (host, FSID, o, r)))
                L.append((t + 75, "%s systemd[1]: ceph-%s@osd.%d.service: Start request repeated too quickly." % (host, FSID, o)))
                L.append((t + 75, "%s systemd[1]: Failed to start Ceph osd.%d for %s." % (host, o, FSID)))
            elif ty == "slow-ops":
                tt = t
                while tt < now:
                    L.append((tt, p + "bluestore(/var/lib/ceph/osd/ceph-%d) log_latency_fn slow operation observed for "
                                      "_txc_committed_kv, latency = %.6fs, txc = 0x55d1c%07x" % (o, 3 + h("lat", int(tt)) % 600 / 100.0, int(tt) % 10**7)))
                    L.append((tt + 2, p + "osd.%d 4127 get_health_metrics reporting %d slow ops, oldest is osd_op(client.48213.0:%d "
                                          "3.%x 3:%08x:::1000000a1f3.%08x:head [write 0~4194304] snapc 0=[] ondisk+write+known_if_redirected e4127)"
                              % (o, 20 + int(tt) % 40, 9000 + int(tt) % 900, h("pg", int(tt)) % 512, h("x", int(tt)) % (1 << 32), int(tt) % 99999)))
                    tt += 47 + h("st", int(tt)) % 40
            elif ty == "_inc_primary":
                pgid, acting = inconsistent_pg(f["target"])
                obj = "1000000b2c1.%08x" % (h("obj", pgid) % 0x10000)
                L += [(t - 90, p + "log_channel(cluster) log [DBG] : %s deep-scrub starts" % pgid),
                      (t - 2, p + "log_channel(cluster) log [ERR] : %s shard %d soid 3:%08x:::%s:head : candidate had a read error"
                       % (pgid, f["target"], h("hash", obj) % (1 << 32), obj)),
                      (t - 1, p + "log_channel(cluster) log [ERR] : %s deep-scrub 0 missing, 1 inconsistent objects" % pgid),
                      (t, p + "log_channel(cluster) log [ERR] : %s deep-scrub 2 errors" % pgid)]
            elif ty == "mtu-mismatch":
                tt = t + 60
                peers = [x for x in range(NOSD) if osd_host(x) != host]
                while tt < now:
                    pr = peers[h("pp", o, int(tt)) % len(peers)]
                    L.append((tt + 22, p + "osd.%d 4127 heartbeat_check: no reply from %s:%d osd.%d since back %s front %s (oldest deadline %s)" % (
                        o, clu_ip(osd_host(pr)), 6804 + pr % 4 * 2, pr, iso(tt).replace("Z", "+0000"),
                        iso(tt + 21).replace("Z", "+0000"), iso(tt + 20).replace("Z", "+0000"))))
                    if tt + 45 < now:
                        L.append((tt + 44, p + "log_channel(cluster) log [WRN] : Monitor daemon marked osd.%d down, but it is still running" % o))
                        L.append((tt + 45, p + "log_channel(cluster) log [DBG] : map e%d wrongly marked me down at e%d" % (4130 + int(tt - t) // 240 * 2, 4129 + int(tt - t) // 240 * 2)))
                    tt += 240
        return "\n".join("%s %s" % (jts(t), msg) for t, msg in sorted(x for x in L if x[0] <= now)) + "\n"
    if "mon" in unit:
        if host not in MONS:
            return "-- No entries --\n"
        p = pfx % ("mon-%s" % host)
        L.append((UPGRADED_AT + 20, "%s Started Ceph mon.%s for %s." % (host, host, FSID)))
        L.append((UPGRADED_AT + 25, p + "mon.%s@%d(peon) e3 handle_conf_change mon_allow_pool_delete" % (host, MONS.index(host))))
        md = target_on(s, "mon-disk-full", host)
        if md:
            t, r = md["since"], MONS.index(host)
            for k, av in enumerate([29, 21, 14, 9, 6]):
                L.append((t - 3600 * (5 - k), p + "mon.%s@%d(peon).data_health(42) update_stats avail %d%% total 439 GiB, used %d GiB, avail %d GiB"
                          % (host, r, av, int(439 * (1 - av / 100.0)), int(439 * av / 100.0))))
            L += [(t - 1, p + "log_channel(cluster) log [ERR] : reached critical levels of available space on local monitor storage -- shutdown!"),
                  (t, p + "mon.%s@%d(peon) e3 *** Got Signal Interrupt ***" % (host, r)),
                  (t, "%s systemd[1]: ceph-%s@mon.%s.service: Deactivated successfully." % (host, FSID, host)),
                  (t + 10, "%s systemd[1]: ceph-%s@mon.%s.service: Main process exited, code=exited, status=28/n/a" % (host, FSID, host)),
                  (t + 70, "%s systemd[1]: ceph-%s@mon.%s.service: Start request repeated too quickly." % (host, FSID, host))]
        cs = target_on(s, "clock-skew", host)
        if cs:
            L.append((cs["since"] + 300, p + "log_channel(cluster) log [WRN] : message from mon.0 was stamped %.6fs in the future, "
                                             "clocks not synchronized" % 0.31))
        return "\n".join("%s %s" % (jts(t), msg) for t, msg in sorted(x for x in L if x[0] <= now)) + "\n"
    return "-- No entries --\n"


def cmd_smartctl(s, host, now, args):
    dev = next((a for a in args if a.startswith("/dev/")), None)
    if not dev:
        return "smartctl: missing device, e.g. smartctl -a /dev/nvme0n1\n"
    mo = re.match(r"/dev/nvme(\d)(n1)?$", dev)
    if dev.startswith("/dev/sda"):
        return ("smartctl 7.4 2023-08-01 r5530 [x86_64-linux-6.8.0-87-generic] (local build)\n\n"
                "Device Model:     Micron_5400_MTFDDAK480TGA\nSMART overall-health self-assessment test result: PASSED\n")
    if not mo:
        return "Smartctl open device: %s failed: No such device\n" % dev
    o = HOSTS.index(host) * 4 + int(mo.group(1))
    df = first(s, "disk-fail")
    if df and df["target"] == o:
        return ("smartctl 7.4 2023-08-01 r5530 [x86_64-linux-6.8.0-87-generic] (local build)\n"
                "Copyright (C) 2002-23, Bruce Allen, Christian Franke, www.smartmontools.org\n\n"
                "Smartctl open device: %s failed: No such device\n" % dev)
    so, pi = first(s, "slow-ops"), first(s, "pg-inconsistent")
    worn, media = so and so["target"] == o, pi and pi["target"] == o
    hours = int((now - BUILT_AT) / 3600) + 1800 + h("poh", o) % 500
    errlog = (1712 + int((now - so["since"]) / 30)) if worn else (3 if media else 0)
    L = ["smartctl 7.4 2023-08-01 r5530 [x86_64-linux-6.8.0-87-generic] (local build)",
         "Copyright (C) 2002-23, Bruce Allen, Christian Franke, www.smartmontools.org", "",
         "=== START OF INFORMATION SECTION ===",
         "Model Number:                       %s" % DISK_MODEL,
         "Serial Number:                      S6CRNG0W%06d" % (h("sn", o) % 10**6),
         "Firmware Version:                   %s" % DISK_FW,
         "PCI Vendor/Subsystem ID:            0x144d",
         "Total NVM Capacity:                 7,681,501,126,656 [7.68 TB]",
         "Namespace 1 Size/Capacity:          7,681,501,126,656 [7.68 TB]",
         "Local Time is:                      %s" % ts(now), "",
         "=== START OF SMART DATA SECTION ===",
         "SMART overall-health self-assessment test result: PASSED", "",
         "SMART/Health Information (NVMe Log 0x02)",
         "Critical Warning:                   0x00",
         "Temperature:                        %d Celsius" % (44 if worn else 36 + h("t", o) % 6),
         "Available Spare:                    %d%%" % (12 if worn else 100),
         "Available Spare Threshold:          10%",
         "Percentage Used:                    %d%%" % wear_pct(s, o),
         "Data Units Read:                    %s [%s TB]" % ("{:,}".format(int(hours * 913000)), int(hours * 0.467)),
         "Data Units Written:                 %s [%s PB]" % ("{:,}".format(int(hours * (41e6 if worn else 2.1e6))),
                                                            "%.2f" % (hours * (21 if worn else 1.1) / 1000)),
         "Power Cycles:                       %d" % (11 + h("pc", o) % 4),
         "Power On Hours:                     {:,}".format(hours),
         "Unsafe Shutdowns:                   %d" % (2 + h("us", o) % 3),
         "Media and Data Integrity Errors:    %d" % (3 if media else 0),
         "Error Information Log Entries:      %d" % errlog,
         "Warning  Comp. Temperature Time:    0",
         "Critical Comp. Temperature Time:    0", ""]
    if "-H" in args and "-a" not in args and "-x" not in args:
        return "\n".join(L[:3] + L[12:14]) + "\n"
    return "\n".join(L) + "\n"


def cmd_nvme(s, host, now, args):
    df = first(s, "disk-fail")
    if args[:1] == ["list"]:
        L = ["Node                  Generic               SN                   Model                                    Namespace  Usage                      Format           FW Rev",
             "--------------------- --------------------- -------------------- ---------------------------------------- ---------- -------------------------- ---------------- --------"]
        for o in host_osds(host):
            if df and df["target"] == o:
                continue
            L.append("/dev/%-16s /dev/ng%dn1            S6CRNG0W%06d       %-40s 0x1          7.68  TB /   7.68  TB    512   B +  0 B   %s"
                     % (osd_dev(o), o % 4, h("sn", o) % 10**6, DISK_MODEL, DISK_FW))
        return "\n".join(L) + "\n"
    return "nvme %s: try 'nvme list' or smartctl -a /dev/nvmeXn1\n" % " ".join(args)


def cmd_lsblk(s, host, now, args):
    df = first(s, "disk-fail")
    L = ["NAME                                                                    MAJ:MIN RM   SIZE RO TYPE MOUNTPOINTS",
         "sda                                                                       8:0    0 447.1G  0 disk ",
         "├─sda1                                                                    8:1    0     1G  0 part /boot/efi",
         "└─sda2                                                                    8:2    0 446.1G  0 part /"]
    for o in host_osds(host):
        if df and df["target"] == o:
            continue
        L.append("%-73s 259:%d    0   7T  0 disk " % (osd_dev(o), o % 4 * 2))
        L.append("└─ceph--%s-osd--block--%s 252:%d    0   7T  0 lvm  " % (uid("vg", o)[:8], uid("lv", o)[:8], o % 4))
    return "\n".join(L) + "\n"


def cmd_df(s, host, now, args):
    md = target_on(s, "mon-disk-full", host)
    used, avail, pct = (425, 14, 97) if md else (58 + h("du", host) % 9, 381 - h("du", host) % 9, 14)
    return ("Filesystem      Size  Used Avail Use%% Mounted on\n"
            "tmpfs            26G  3.1M   26G   1%% /run\n"
            "/dev/sda2       439G  %3dG  %3dG  %2d%% /\n"
            "tmpfs           126G     0  126G   0%% /dev/shm\n"
            "/dev/sda1       1.1G  6.2M  1.1G   1%% /boot/efi\n"
            "tmpfs            26G  4.0K   26G   1%% /run/user/0\n") % (used, avail, pct)


def cmd_du(s, host, now, args):
    md = target_on(s, "mon-disk-full", host)
    paths = [a for a in args if not a.startswith("-")] or ["."]
    p = paths[0]
    d = "/var/log/ceph/%s" % FSID
    if p.startswith("/var/log"):
        if p in ("/var/log", "/var/log/", "/var/log/*", "/var/log/ceph", "/var/log/ceph/", "/var/log/ceph/*"):
            return "%s\t%s\n" % ("362G" if md else "1.9G", d)
        L = [("351G" if md else "1.1G", "%s/ceph-mon.%s.log" % (d, host))] if host in MONS else []
        L += [("12M", "%s/ceph-volume.log" % d), ("%dM" % (180 + h("dl", host) % 90), "%s/ceph-osd.%d.log" % (d, host_osds(host)[0])),
              ("9.8G" if md else "410M", "%s/ceph.audit.log" % d)]
        return "\n".join("%s\t%s" % x for x in L) + "\n"
    if p.startswith("/var/lib/ceph"):
        return ("%s\t/var/lib/ceph/%s/mon.%s\n" % ("1.4G", FSID, host)) if host in MONS else "24K\t/var/lib/ceph/%s/crash\n" % FSID
    return "%s\t%s\n" % ("4.0K", p)


def cmd_chronyc(s, host, now, args):
    cs = target_on(s, "clock-skew", host)
    if args[:1] == ["sources"]:
        L = ["MS Name/IP address         Stratum Poll Reach LastRx Last sample",
             "==============================================================================="]
        for i, srv in enumerate(["10.10.0.1", "10.10.0.2"]):
            if cs:
                L.append("^? %-26s  2  10     0   %3dm     +0ns[   +0ns] +/-    0ns" % (srv, int((now - cs["since"]) / 60)))
            else:
                L.append("^%s %-26s  2  10   377   %3d   %+5dus[%+5dus] +/- %3dus" % ("*" if i == 0 else "-", srv, 300 + i * 41,
                                                                                  -12 + i * 30, -11 + i * 30, 180 + i * 40))
        return "\n".join(L) + "\n"
    if args[:1] in (["tracking"], []):
        if cs:
            off = model(s, now)["mons"][host]["skew"]
            return ("Reference ID    : 0A0A0001 (10.10.0.1)\nStratum         : 3\nRef time (UTC)  : %s\n"
                    "System time     : %.9f seconds fast of NTP time\nLast offset     : +0.000000000 seconds\n"
                    "RMS offset      : 0.000021337 seconds\nFrequency       : 41.873 ppm fast\nResidual freq   : +0.000 ppm\n"
                    "Skew            : 0.012 ppm\nRoot delay      : 0.000412113 seconds\nRoot dispersion : %.6f seconds\n"
                    "Update interval : 1031.2 seconds\nLeap status     : Not synchronised\n") % (
                time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(cs["since"])), off, (now - cs["since"]) * 0.000015)
        return ("Reference ID    : 0A0A0001 (10.10.0.1)\nStratum         : 3\nRef time (UTC)  : %s\n"
                "System time     : 0.000012%03d seconds slow of NTP time\nLast offset     : -0.000004113 seconds\n"
                "RMS offset      : 0.000018242 seconds\nFrequency       : 12.104 ppm slow\nResidual freq   : -0.000 ppm\n"
                "Skew            : 0.009 ppm\nRoot delay      : 0.000398221 seconds\nRoot dispersion : 0.000231004 seconds\n"
                "Update interval : 1030.8 seconds\nLeap status     : Normal\n") % (
            time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(now - 410)), h("cr", host) % 1000)
    return "chronyc: try 'chronyc tracking' or 'chronyc sources'\n"


def mac(host, i):
    x = h("mac", host, i)
    return "b8:3f:d2:%02x:%02x:%02x" % (x % 256, (x >> 8) % 256, (x >> 16) % 256)


def cmd_ip(s, host, now, args):
    mtu = model(s, now)["hosts"][host]["mtu"]
    a = [x for x in args if x != "-br"]
    brief = "-br" in args
    ifs = [("lo", 65536, "00:00:00:00:00:00", "127.0.0.1/8", "UNKNOWN"), (PUBLIC_IF, 9000, mac(host, 0), pub_ip(host) + "/24", "UP"),
           (CLUSTER_IF, mtu, mac(host, 1), clu_ip(host) + "/24", "UP")]
    if a[:1] in (["a"], ["addr"], ["address"]):
        if brief:
            return "\n".join("%-16s %-14s %s" % (n, st, ip) for n, _, _, ip, st in ifs) + "\n"
        return "\n".join("%d: %s: <%s> mtu %d qdisc mq state %s group default qlen 1000\n    link/ether %s\n    inet %s scope global %s"
                         % (i + 1, n, "LOOPBACK,UP,LOWER_UP" if n == "lo" else "BROADCAST,MULTICAST,UP,LOWER_UP", m_, st, mc, ip, n)
                         for i, (n, m_, mc, ip, st) in enumerate(ifs)) + "\n"
    if a[:1] in (["l"], ["link"], []):
        want = a[a.index("dev") + 1] if "dev" in a else (a[2] if len(a) > 2 and a[1] == "show" else None)
        rows = [x for x in ifs if not want or x[0] == want]
        if brief:
            return "\n".join("%-16s %-14s %s <%s>" % (n, st, mc, "LOOPBACK,UP,LOWER_UP" if n == "lo" else "BROADCAST,MULTICAST,UP,LOWER_UP")
                             for n, _, mc, _, st in rows) + "\n"
        return "\n".join("%d: %s: <%s> mtu %d qdisc %s state %s mode DEFAULT group default qlen 1000\n    link/%s %s brd ff:ff:ff:ff:ff:ff"
                         % (ifs.index(x) + 1, x[0], "LOOPBACK,UP,LOWER_UP" if x[0] == "lo" else "BROADCAST,MULTICAST,UP,LOWER_UP",
                            x[1], "noqueue" if x[0] == "lo" else "mq", x[4], "loopback" if x[0] == "lo" else "ether", x[2]) for x in rows) + "\n"
    return "ip: try 'ip link', 'ip -br addr'\n"


def cmd_ping(s, host, now, args):
    m = model(s, now)
    dst = next((a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] not in ("-c", "-s", "-M", "-I", "-W"))), None)
    if not dst:
        return "ping: usage error: Destination address required\n"
    size = int(args[args.index("-s") + 1]) if "-s" in args else 56
    count = int(args[args.index("-c") + 1]) if "-c" in args else 4
    dfbit = "-M" in args and args[args.index("-M") + 1] == "do"
    peer, net = None, "public"
    for x in HOSTS:
        if dst in (x, pub_ip(x)):
            peer = x
        elif dst == clu_ip(x):
            peer, net = x, "cluster"
    if not peer:
        return "ping: %s: Name or service not known\n" % dst if not dst[0].isdigit() else \
            "PING %s (%s) %d(%d) bytes of data.\n\n--- %s ping statistics ---\n%d packets transmitted, 0 received, 100%% packet loss\n" % (
                dst, dst, size, size + 28, dst, count)
    ip = clu_ip(peer) if net == "cluster" else pub_ip(peer)
    L = ["PING %s (%s) %d(%d) bytes of data." % (dst, ip, size, size + 28)]
    src_mtu = m["hosts"][host]["mtu"] if net == "cluster" else 9000
    dst_mtu = m["hosts"][peer]["mtu"] if net == "cluster" else 9000
    if not m["hosts"][peer]["up"]:
        L += ["From %s icmp_seq=%d Destination Host Unreachable" % (clu_ip(host) if net == "cluster" else pub_ip(host), i + 1) for i in range(count)]
        L += ["", "--- %s ping statistics ---" % dst, "%d packets transmitted, 0 received, +%d errors, 100%% packet loss, time %dms" % (count, count, count * 1000 + 40)]
    elif size + 28 > src_mtu and dfbit:
        L += ["ping: local error: message too long, mtu=%d" % src_mtu for _ in range(count)]
        L += ["", "--- %s ping statistics ---" % dst, "%d packets transmitted, 0 received, +%d errors, 100%% packet loss, time %dms" % (count, count, count * 1000 + 30)]
    elif size + 28 > min(src_mtu, dst_mtu):
        L += ["", "--- %s ping statistics ---" % dst, "%d packets transmitted, 0 received, 100%% packet loss, time %dms" % (count, count * 1000 + 50)]
    else:
        rtts = [0.08 + h("rtt", host, peer, i) % 60 / 1000.0 for i in range(count)]
        L += ["%d bytes from %s: icmp_seq=%d ttl=64 time=%.3f ms" % (size + 8, ip, i + 1, r) for i, r in enumerate(rtts)]
        L += ["", "--- %s ping statistics ---" % dst, "%d packets transmitted, %d received, 0%% packet loss, time %dms" % (count, count, (count - 1) * 1000 + 3),
              "rtt min/avg/max/mdev = %.3f/%.3f/%.3f/0.012 ms" % (min(rtts), sum(rtts) / count, max(rtts))]
    return "\n".join(L) + "\n"


def cmd_systemctl(s, host, now, args):
    unit = next((a for a in args if a not in ("status", "--no-pager", "-l")), "")
    if args[:1] != ["status"]:
        return "[sim] read-only: '%s' changes nothing here. Note your fix, then ./lab.sh ceph reveal\n" % " ".join(["systemctl"] + args)
    mo = re.search(r"osd[.@-]?(\d+)", unit)
    m = model(s, now)
    if mo and osd_host(int(mo.group(1))) == host:
        o = m["osds"][int(mo.group(1))]
        name = "ceph-%s@osd.%d.service" % (FSID, o["id"])
        if o["daemon"] == "error":
            return ("× %s - Ceph osd.%d for %s\n     Loaded: loaded (/etc/systemd/system/ceph-%s@.service; enabled; preset: enabled)\n"
                    "     Active: failed (Result: exit-code) since %s; %s ago\n" % (name, o["id"], FSID, FSID, ts(o["down_since"] + 75),
                                                                                   fmt_age(now - o["down_since"] - 75)))
        return ("● %s - Ceph osd.%d for %s\n     Loaded: loaded (/etc/systemd/system/ceph-%s@.service; enabled; preset: enabled)\n"
                "     Active: active (running) since %s; %s ago\n" % (name, o["id"], FSID, FSID, ts(UPGRADED_AT + 40), fmt_age(now - UPGRADED_AT)))
    if "mon" in unit and host in MONS:
        name = "ceph-%s@mon.%s.service" % (FSID, host)
        if not m["mons"][host]["up"]:
            return "× %s - Ceph mon.%s for %s\n     Active: failed (Result: exit-code)\n" % (name, host, FSID)
        return "● %s - Ceph mon.%s for %s\n     Active: active (running) since %s\n" % (name, host, FSID, ts(UPGRADED_AT + 20))
    if "chrony" in unit:
        return "● chrony.service - chrony, an NTP client/server\n     Active: active (running) since %s\n" % ts(boot_time(host) + 9)
    return "Unit %s could not be found.\n" % unit


HELP = """Simulated storage host {node} (Ceph {v}, 4x 7.68 TB NVMe OSDs). Pipes to grep/head/tail/wc -l work.
  ceph -s | health [detail] | versions | df
  ceph osd tree | df [tree] | perf | stat | dump | pool ls [detail] | metadata <id> | find <id>
  ceph pg stat | dump_stuck [unclean|inactive|undersized|degraded] | ls [state] | map <pgid>
  rados list-inconsistent-obj <pgid> --format=json-pretty
  ceph mon stat | time-sync-status      ceph crash ls | info <id>
  ceph orch ps [--daemon-type osd] [host] | orch host ls      ceph device ls
  ceph fs status | config dump | balancer status
  journalctl -u ceph-osd@<id> | -u ceph-mon     (also: cephadm logs --name osd.<id>)
  dmesg [-T]   smartctl -a /dev/nvme<N>n1   nvme list   lsblk   df -h   du -sh <path>
  systemctl status ceph-osd@<id>   chronyc tracking | sources   ip link | ip -br addr
  ping [-c N] [-M do] [-s SIZE] <host or ip>    (cluster network: 10.20.0.11-16)
Commands that change things (ceph osd set/out, pg repair, orch daemon restart, ...) are read-only here:
write down the fix you'd apply, then ./lab.sh ceph reveal.
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
    if not model(s, now)["hosts"][node]["up"]:
        return "ssh: connect to host %s port 22: No route to host\n" % node
    cmd, args = stages[0][0], stages[0][1:]
    if cmd == "sudo" and args:
        cmd, args = args[0], args[1:]
    if cmd == "cephadm" and args[:1] == ["shell"]:
        cmd, args = (args[2], args[3:]) if args[1:2] == ["--"] and len(args) > 2 else ("help", [])
    if cmd == "ceph":
        out = cmd_ceph(s, node, now, args)
    elif cmd == "rados" and args[:1] == ["list-inconsistent-obj"] and len(args) > 1:
        out = list_inconsistent(s, model(s, now), args[1])
    elif cmd == "rados" and args[:1] == ["df"]:
        out = ceph_df(model(s, now)).split("--- POOLS ---")[1].lstrip("\n")
    elif cmd == "journalctl":
        unit = args[args.index("-u") + 1] if "-u" in args else ""
        out = journal(s, node, now, unit)
        if "-n" in args:
            out = "\n".join(out.rstrip("\n").split("\n")[-int(args[args.index("-n") + 1]):]) + "\n"
    elif cmd == "cephadm" and args[:1] == ["logs"]:
        name = args[args.index("--name") + 1] if "--name" in args else ""
        out = journal(s, node, now, name)
    elif cmd == "dmesg":
        out = cmd_dmesg(s, node, now, args)
    elif cmd == "smartctl":
        out = cmd_smartctl(s, node, now, args)
    elif cmd == "nvme":
        out = cmd_nvme(s, node, now, args)
    elif cmd == "lsblk":
        out = cmd_lsblk(s, node, now, args)
    elif cmd == "df":
        out = cmd_df(s, node, now, args)
    elif cmd == "du":
        out = cmd_du(s, node, now, args)
    elif cmd == "chronyc":
        out = cmd_chronyc(s, node, now, args)
    elif cmd == "ip":
        out = cmd_ip(s, node, now, args)
    elif cmd == "ping":
        out = cmd_ping(s, node, now, args)
    elif cmd == "systemctl":
        out = cmd_systemctl(s, node, now, args)
    elif cmd in ("ssh",) and args:
        tgt = args[-1].split("@")[-1]
        if tgt in HOSTS and not model(s, now)["hosts"][tgt]["up"]:
            return "ssh: connect to host %s port 22: No route to host\n" % tgt
        return "[sim] use ./lab.sh ceph ssh %s to open a shell on another host\n" % tgt
    elif cmd == "hostname":
        out = node + "\n"
    elif cmd == "uptime":
        up = now - boot_time(node)
        out = " %s up %d days, %d:%02d,  1 user,  load average: 3.12, 2.98, 3.05\n" % (
            time.strftime("%H:%M:%S"), up // 86400, (up % 86400) // 3600, (up % 3600) // 60)
    elif cmd == "help":
        out = HELP.format(node=node, v=VERSION)
    else:
        return "%s: command not simulated here (try 'help')\n" % cmd
    for st in stages[1:]:
        out = pipe(out, st)
    return out


def pipe(text, st):
    if not st:
        return text
    lines = text.rstrip("\n").split("\n")
    c, a = st[0], st[1:]
    if c in ("grep", "egrep"):
        flags = [x for x in a if x.startswith("-") and x != "-e"]
        pats = [a[i + 1] for i, x in enumerate(a) if x == "-e" and i + 1 < len(a)] or [x for x in a if not x.startswith("-")][:1]
        if not pats:
            return text
        rx = re.compile("|".join("(?:%s)" % p.replace("\\|", "|") for p in pats), re.I if any("i" in f for f in flags) else 0)
        keep = [l for l in lines if bool(rx.search(l)) != any("v" in f for f in flags)]
        if any("c" in f for f in flags):
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
    elif c == "sort":
        lines = sorted(lines, reverse="-r" in a)
    elif c == "less" or c == "more":
        pass
    else:
        return "%s: not supported in this simulated shell\n" % c
    return "\n".join(lines) + ("\n" if lines else "")


def shell(node):
    if node not in HOSTS:
        sys.exit("unknown host %s (hosts: %s)" % (node, " ".join(HOSTS)))
    s = load_state()
    if not model(s)["hosts"][node]["up"]:
        print("ssh: connect to host %s port 22: No route to host" % node)
        alt = [x for x in ADMIN if x != node]
        print("(try another admin host: ./lab.sh ceph ssh %s)" % alt[0])
        return
    try:
        import readline  # noqa: F401
    except ImportError:
        pass
    print("Simulated shell on %s (Ceph %s storage host, 4x NVMe OSDs). Type 'help'. Ctrl-D to leave." % (node, VERSION))
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
            except Exception as e:
                print("error: %s" % e)


# ---------------------------------------------------------------------------------------------
# Prometheus exporter (ceph-mgr 'prometheus' module metric names)
# ---------------------------------------------------------------------------------------------
PG_STATES = ["active", "clean", "degraded", "undersized", "remapped", "backfilling", "backfill_wait", "recovering",
             "recovery_wait", "peering", "peered", "inconsistent", "incomplete", "stale", "down"]
OSD_FLAGS = ["noup", "nodown", "noout", "noin", "nobackfill", "norebalance", "norecover", "noscrub", "nodeep_scrub"]


def wr_bytes_counter(s, pid, now):
    """Monotonic client write counter per pool: writes stop while an OSD is full and slow down with slow ops."""
    rate = {1: 1e3, 2: 2.6e8, 3: 1.9e9, 4: 4e7}[pid]
    total = rate * (now - T0)
    for f in s["faults"]:
        if f["type"] in ("osd-full", "slow-ops") and f["since"] < now:
            total -= rate * (now - f["since"]) * (1.0 if f["type"] == "osd-full" else 0.7)
    return int(max(0, total))


def render_metrics(s, now=None):
    now = now or time.time()
    m = model(s, now)
    out = []

    def metric(name, typ, hlp, rows):
        out.append("# HELP %s %s" % (name, hlp))
        out.append("# TYPE %s %s" % (name, typ))
        for lbl, v in rows:
            out.append("%s{%s} %s" % (name, lbl, ("%.6g" % v) if isinstance(v, float) else v) if lbl else "%s %s" % (
                name, ("%.6g" % v) if isinstance(v, float) else v))

    metric("ceph_health_status", "untyped", "Cluster health status",
           [("", {"HEALTH_OK": 0, "HEALTH_WARN": 1, "HEALTH_ERR": 2}[m["status"]])])
    metric("ceph_health_detail", "untyped", "healthcheck status by type (0=inactive, 1=active)",
           [('name="%s",severity="%s"' % (c[0], "HEALTH_ERR" if c[1] == "ERR" else "HEALTH_WARNING"), 1) for c in m["checks"]])
    metric("ceph_mon_quorum_status", "gauge", "Monitors in quorum",
           [('ceph_daemon="mon.%s"' % x, int(m["mons"][x]["up"])) for x in MONS])
    metric("ceph_mon_metadata", "untyped", "MON Metadata",
           [('ceph_daemon="mon.%s",hostname="%s",public_addr="%s",rank="%d",ceph_version="%s"' % (x, x, pub_ip(x), i, VSTRING), 1)
            for i, x in enumerate(MONS)])
    metric("ceph_mgr_status", "gauge", "MGR status (0=standby, 1=active)",
           [('ceph_daemon="mgr.%s"' % mgr_name(x), int(x == active_mgr(m))) for x in MGRS])
    metric("ceph_osd_metadata", "untyped", "OSD Metadata",
           [('ceph_daemon="osd.%d",hostname="%s",device_class="nvme",devices="%s",objectstore="bluestore",cluster_addr="%s",'
             'public_addr="%s",back_iface="%s",front_iface="%s",ceph_version="%s"' % (
                 o["id"], o["host"], o["dev"], clu_ip(o["host"]), pub_ip(o["host"]), CLUSTER_IF, PUBLIC_IF, VSTRING), 1) for o in m["osds"]])
    for name, key, hlp in [("ceph_osd_up", "up", "OSD status up"), ("ceph_osd_in", "inn", "OSD status in")]:
        metric(name, "untyped", hlp, [('ceph_daemon="osd.%d"' % o["id"], int(o[key])) for o in m["osds"]])
    metric("ceph_osd_weight", "untyped", "OSD status weight", [('ceph_daemon="osd.%d"' % o["id"], 1.0 if o["inn"] else 0.0) for o in m["osds"]])
    metric("ceph_osd_crush_weight", "gauge", "OSD CRUSH weight (TiB)", [('ceph_daemon="osd.%d"' % o["id"], round(o["weight"], 5)) for o in m["osds"]])
    metric("ceph_osd_stat_bytes", "gauge", "OSD size", [('ceph_daemon="osd.%d"' % o["id"], OSD_BYTES if o["up"] else 0) for o in m["osds"]])
    metric("ceph_osd_stat_bytes_used", "gauge", "Used space",
           [('ceph_daemon="osd.%d"' % o["id"], int(o["used"] * OSD_BYTES) if o["up"] else 0) for o in m["osds"]])
    metric("ceph_osd_numpg", "gauge", "Placement groups", [('ceph_daemon="osd.%d"' % o["id"], osd_pgcount(m, o)) for o in m["osds"]])
    metric("ceph_osd_apply_latency_ms", "gauge", "OSD stat apply_latency_ms", [('ceph_daemon="osd.%d"' % o["id"], o["apply"]) for o in m["osds"]])
    metric("ceph_osd_commit_latency_ms", "gauge", "OSD stat commit_latency_ms", [('ceph_daemon="osd.%d"' % o["id"], o["commit"]) for o in m["osds"]])
    so = first(s, "slow-ops")
    nslow = int(jitter(18, 64, "nslow")) if so else 0
    metric("ceph_daemon_health_metrics", "gauge", "Health metrics for Ceph daemons",
           [('ceph_daemon="osd.%d",type="SLOW_OPS"' % o["id"], nslow if so and so["target"] == o["id"] else 0) for o in m["osds"]])
    metric("ceph_healthcheck_slow_ops", "gauge", "OSD or Monitor requests taking a long time to process", [("", nslow)])
    for fl in OSD_FLAGS:
        metric("ceph_osd_flag_%s" % fl, "untyped", "OSD Flag %s" % fl, [("", int(fl in m["flags"]))])
    metric("ceph_cluster_total_bytes", "gauge", "DF total_bytes", [("", int(m["total"]))])
    metric("ceph_cluster_total_used_bytes", "gauge", "DF total_used_bytes", [("", int(m["used"]))])
    metric("ceph_pool_metadata", "untyped", "POOL Metadata",
           [('pool_id="%d",name="%s",type="replicated",description="replica:3",compression_mode="none"' % (p[0], p[1]), 1) for p in POOLS])
    metric("ceph_pool_stored", "gauge", "DF pool stored", [('pool_id="%d"' % p[0], int(pool_stored(p[0]))) for p in POOLS])
    metric("ceph_pool_objects", "gauge", "DF pool objects", [('pool_id="%d"' % p[0], pool_objects(p[0])) for p in POOLS])
    metric("ceph_pool_max_avail", "gauge", "DF pool max_avail", [('pool_id="%d"' % p[0], int(m["max_avail"])) for p in POOLS])
    metric("ceph_pool_percent_used", "gauge", "DF pool percent_used",
           [('pool_id="%d"' % p[0], pool_stored(p[0]) / (pool_stored(p[0]) + m["max_avail"]) if pool_stored(p[0]) + m["max_avail"] else 1.0)
            for p in POOLS])
    metric("ceph_pool_wr_bytes", "counter", "DF pool wr_bytes", [('pool_id="%d"' % p[0], wr_bytes_counter(s, p[0], now)) for p in POOLS])
    metric("ceph_pool_rd_bytes", "counter", "DF pool rd_bytes",
           [('pool_id="%d"' % p[0], int({1: 1e3, 2: 3.1e8, 3: 9e8, 4: 6e7}[p[0]] * (now - T0))) for p in POOLS])
    for st in PG_STATES:
        rows = []
        for pid, _, _, _, _ in POOLS:
            rows.append(('pool_id="%d"' % pid, sum(1 for p in m["pgs"] if p["pool"] == pid and st in p["state"].split("+"))))
        metric("ceph_pg_%s" % st, "gauge", "PG %s per pool" % st, rows)
    metric("ceph_pg_total", "gauge", "PG Total Count per Pool", [('pool_id="%d"' % p[0], p[2]) for p in POOLS])
    metric("ceph_num_objects_degraded", "gauge", "Number of degraded objects", [("", m["degraded"])])
    metric("ceph_num_objects_misplaced", "gauge", "Number of misplaced objects", [("", 0)])
    metric("ceph_num_objects_unfound", "gauge", "Number of unfound objects", [("", 0)])
    metric("ceph_mds_metadata", "untyped", "MDS Metadata",
           [('ceph_daemon="mds.%s",fs_id="1",hostname="%s",rank="%s"' % (mds_name(x), x, "0" if x == (MDSS[0] if m["hosts"][MDSS[0]]["up"] else MDSS[1]) else "-1"),
             int(m["hosts"][x]["up"])) for x in MDSS])
    out.append("# HELP cephsim_active_faults Number of injected faults (drill bookkeeping, hide it from learners).")
    out.append("# TYPE cephsim_active_faults gauge")
    out.append("cephsim_active_faults %d" % len(s["faults"]))
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

    print("cephsim exporter on :%d (state %s)" % (port, STATE), flush=True)
    HTTPServer(("", port), H).serve_forever()


# ---------------------------------------------------------------------------------------------
# Fault management + lab.sh helpers
# ---------------------------------------------------------------------------------------------
def fmt(f, text):
    t = f.get("target")
    host = osd_host(t) if isinstance(t, int) else t
    pg = inconsistent_pg(t)[0] if f["type"] == "pg-inconsistent" else ""
    return text.format(t=t, host=host, dev=("/dev/" + osd_dev(t)) if isinstance(t, int) else "", pg=pg,
                       time=time.strftime("%H:%M", time.localtime(f["since"])))


def add_fault(s, ftype, target=None, since=None):
    if ftype not in FAULTS:
        sys.exit("unknown fault '%s'. Run: ./lab.sh ceph faults" % ftype)
    kind = FAULTS[ftype]["kind"]
    if kind == "osd":
        target = int(str(target).replace("osd.", "")) if target is not None else random.randrange(NOSD)
        if not 0 <= target < NOSD:
            sys.exit("OSD id must be 0-%d" % (NOSD - 1))
    elif kind == "mon":
        target = target or random.choice(MONS)
        if target not in MONS:
            sys.exit("%s needs a monitor host: %s" % (ftype, " ".join(MONS)))
    else:
        target = target or random.choice(HOSTS)
        if target not in HOSTS:
            sys.exit("unknown host '%s' (hosts: %s)" % (target, " ".join(HOSTS)))
    f = dict(type=ftype, target=target, since=int(since or time.time()))
    s["faults"].append(f)
    return f


def status_patch(s, host):
    cap = {"cpu": "64", "memory": "263597468Ki", "ephemeral-storage": "439Gi", "pods": "110"}
    alloc = dict(cap, cpu="62", memory="255000000Ki")
    return {"status": {"capacity": cap, "allocatable": alloc,
                       "nodeInfo": {"architecture": "amd64", "operatingSystem": "linux", "osImage": "Ubuntu 24.04.3 LTS",
                                    "kernelVersion": "6.8.0-87-generic", "containerRuntimeVersion": "containerd://1.7.28",
                                    "kubeletVersion": "v1.33.5+k3s1", "kubeProxyVersion": "", "bootID": "", "machineID": "",
                                    "systemUUID": uid("sys", host)}}}


def node_manifests():
    items = []
    for i, x in enumerate(HOSTS):
        items.append({"apiVersion": "v1", "kind": "Node",
                      "metadata": {"name": x, "annotations": {"kwok.x-k8s.io/node": "fake", "node.alpha.kubernetes.io/ttl": "0"},
                                   "labels": {"type": "kwok", "kubernetes.io/hostname": x, "kubernetes.io/os": "linux",
                                              "kubernetes.io/arch": "amd64", "node-role.kubernetes.io/storage": "",
                                              "node.kubernetes.io/instance-type": "storage-4x-nvme",
                                              "topology.kubernetes.io/zone": "rack-c" if i < 3 else "rack-d",
                                              "ceph.io/osd": "true", "ceph.io/mon": "true" if x in MONS else "false"}},
                      "spec": {"taints": [{"key": "kwok.x-k8s.io/node", "value": "fake", "effect": "NoSchedule"},
                                          {"key": "node-role.kubernetes.io/storage", "value": "ceph", "effect": "NoSchedule"}]}})
    return {"apiVersion": "v1", "kind": "List", "items": items}


def fixtures(outdir):
    """Write every fault's outputs as plain text, for study without a cluster."""
    now = time.time()
    cases = [("00-healthy", None)] + [("%02d-%s" % (i + 1, k), k) for i, k in enumerate(FAULTS)]
    for name, ftype in cases:
        s = {"faults": [], "hidden": False}
        if ftype:
            kind = FAULTS[ftype]["kind"]
            add_fault(s, ftype, 13 if kind == "osd" else ("ceph-02" if kind == "mon" else "ceph-04"), since=now - 1500)
        f = s["faults"][0] if ftype else None
        host = (osd_host(f["target"]) if isinstance(f["target"], int) else f["target"]) if f else "ceph-04"
        admin = "ceph-01"
        osd = f["target"] if f and isinstance(f["target"], int) else host_osds(host)[0] if host in HOSTS else 13
        d = os.path.join(outdir, name)
        os.makedirs(d, exist_ok=True)
        cmds = [(admin, "ceph-s.txt", "ceph -s"), (admin, "ceph-health-detail.txt", "ceph health detail"),
                (admin, "ceph-osd-tree.txt", "ceph osd tree"), (admin, "ceph-osd-df.txt", "ceph osd df"),
                (admin, "ceph-osd-perf.txt", "ceph osd perf"), (admin, "ceph-df.txt", "ceph df"),
                (admin, "ceph-osd-dump-flags.txt", "ceph osd dump | grep -e flags -e ratio"),
                (admin, "ceph-pg-dump_stuck.txt", "ceph pg dump_stuck"), (admin, "ceph-crash-ls.txt", "ceph crash ls"),
                (admin, "ceph-orch-ps.txt", "ceph orch ps"), (admin, "ceph-orch-host-ls.txt", "ceph orch host ls"),
                (admin, "ceph-time-sync-status.txt", "ceph time-sync-status"), (admin, "ceph-config-dump.txt", "ceph config dump"),
                (admin, "ceph-balancer-status.txt", "ceph balancer status"), (admin, "ceph-device-ls.txt", "ceph device ls"),
                (host, "dmesg.txt", "dmesg -T"), (host, "journal-osd.%d.txt" % osd, "journalctl -u ceph-osd@%d" % osd),
                (host, "smartctl-%s.txt" % osd_dev(osd), "smartctl -a /dev/%s" % osd_dev(osd)), (host, "lsblk.txt", "lsblk"),
                (host, "ip-link.txt", "ip link"), (host, "ping-jumbo-cluster.txt", "ping -c 3 -M do -s 8972 10.20.0.11"),
                (host, "df-h.txt", "df -h"), (host, "chronyc-tracking.txt", "chronyc tracking")]
        if host in MONS:
            cmds.append((host, "journal-mon.txt", "journalctl -u ceph-mon"))
        if ftype == "pg-inconsistent":
            pg = inconsistent_pg(osd)[0]
            cmds.append((admin, "rados-list-inconsistent-obj.txt", "rados list-inconsistent-obj %s --format=json-pretty" % pg))
            cmds.append((osd_host(inconsistent_pg(osd)[1][0]), "journal-primary-osd.%d.txt" % inconsistent_pg(osd)[1][0],
                         "journalctl -u ceph-osd@%d" % inconsistent_pg(osd)[1][0]))
        for node, fn, c in cmds:
            with open(os.path.join(d, fn), "w") as fh:
                fh.write("# root@%s:~# %s\n" % (node, c) + run_one(s, node, c, now))
        with open(os.path.join(d, "README.md"), "w") as fh:
            if ftype:
                fh.write("# %s\n\nTicket: %s\n\nStart with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on %s.\n\n"
                         "<details><summary>Answer</summary>\n\n%s\n\n</details>\n"
                         % (FAULTS[ftype]["title"], fmt(f, FAULTS[ftype]["ticket"]), host, fmt(f, FAULTS[ftype]["answer"])))
            else:
                fh.write("# Healthy baseline\n\nAll 24 OSDs up/in, HEALTH_OK. Diff the other cases against these files.\n")
    print("wrote %d cases to %s" % (len(cases), outdir))


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, a = argv[0], argv[1:]
    s = load_state()
    if cmd == "serve":
        serve(int(a[a.index("--port") + 1]) if "--port" in a else 9283)
    elif cmd == "metrics":
        sys.stdout.write(render_metrics(s))
    elif cmd == "run":
        if len(a) < 2:
            sys.exit("usage: run <host> <command...>")
        if a[0] not in HOSTS:
            sys.exit("unknown host %s (hosts: %s)" % (a[0], " ".join(HOSTS)))
        sys.stdout.write(run_one(s, a[0], " ".join(a[1:])))
    elif cmd == "shell":
        shell(a[0] if a else HOSTS[0])
    elif cmd == "faults":
        for k, v in FAULTS.items():
            tgt = {"osd": "<osd id>", "host": "<host>", "mon": "<mon host>"}[v["kind"]]
            print("  %-16s %-11s L%d  %s" % (k, tgt, v["level"], v["title"]))
    elif cmd == "add":
        if not a:
            sys.exit("usage: add <type> [target]")
        f = add_fault(s, a[0], a[1] if len(a) > 1 else None)
        s["hidden"] = False
        save_state(s)
        print("injected %s on %s" % (f["type"], ("osd.%d" % f["target"]) if isinstance(f["target"], int) else f["target"]))
    elif cmd == "drill":
        lvl = int(a[0]) if a else 3
        pool = [k for k, v in FAULTS.items() if v["level"] <= lvl]
        s["faults"] = []
        f = add_fault(s, random.choice(pool))
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
            print("%d hidden drill fault(s) active. ./lab.sh ceph reveal shows the answer." % len(s["faults"]))
        for f in ([] if s["hidden"] else s["faults"]):
            print("  %-16s %-8s since %s" % (f["type"], ("osd.%d" % f["target"]) if isinstance(f["target"], int) else f["target"],
                                            time.strftime("%H:%M:%S", time.localtime(f["since"]))))
        if not s["faults"]:
            print("no active faults (HEALTH_OK)")
    elif cmd == "clear":
        s["faults"], s["hidden"] = [], False
        save_state(s)
        print("all Ceph faults cleared")
    elif cmd == "nodes":
        print(" ".join(HOSTS))
    elif cmd == "node-manifests":
        print(json.dumps(node_manifests(), indent=1))
    elif cmd == "status-patch":
        print(json.dumps(status_patch(s, a[0])))
    elif cmd == "fixtures":
        fixtures(a[0] if a else os.path.join(HERE, "faults"))
    else:
        sys.exit("unknown command %s\n%s" % (cmd, __doc__))


if __name__ == "__main__":
    main(sys.argv[1:])
