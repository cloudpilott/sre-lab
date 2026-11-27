#!/usr/bin/env python3
"""Generate docs/architecture.svg (the lab architecture diagram). Every arrow is placed by hand.

    python3 docs/make_architecture.py
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
        --force-device-scale-factor=1.5 --window-size=1600,1040 --screenshot=docs/architecture.png "file://$PWD/docs/architecture.svg"
"""
import os
from xml.sax.saxutils import escape

W, H = 1600, 1040
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"
MONO = "Menlo, SF Mono, Consolas, monospace"

C = dict(
    ink="#1f2937", muted="#4b5563", faint="#6b7280",
    real="#2563eb", real_bg="#eff6ff", real_box="#dbeafe", layer_bg="#f8fafc",
    gpu="#7c3aed", gpu_bg="#f5f3ff", gpu_box="#ede9fe",
    ceph="#d97706", ceph_bg="#fffbeb", ceph_box="#fef3c7",
    stop="#9ca3af", stop_bg="#f3f4f6",
    fault="#dc2626", data="#ea580c", read="#6b7280",
)

out = []


def rect(x, y, w, h, fill, stroke, dash=False, r=8, sw=1.5):
    d = ' stroke-dasharray="6 4"' if dash else ""
    out.append('<rect x="%g" y="%g" width="%g" height="%g" rx="%g" fill="%s" stroke="%s" stroke-width="%g"%s/>'
               % (x, y, w, h, r, fill, stroke, sw, d))


def text(x, y, s, size=13, weight="normal", color=None, anchor="start", mono=False, italic=False):
    out.append('<text x="%g" y="%g" font-family="%s" font-size="%g" font-weight="%s" fill="%s" text-anchor="%s"%s>%s</text>'
               % (x, y, MONO if mono else FONT, size, weight, color or C["ink"], anchor,
                  ' font-style="italic"' if italic else "", escape(s)))


def box(x, y, w, h, title, lines=(), fill=None, stroke=None, dash=False, size=12, tsize=13, mono_lines=False, lh=16):
    rect(x, y, w, h, fill or C["real_box"], stroke or C["real"], dash)
    text(x + 10, y + 19, title, tsize, "bold")
    for i, l in enumerate(lines):
        text(x + 10, y + 19 + lh * (i + 1) + 2, l, size, color=C["muted"], mono=mono_lines)


def path(points, color, dash=False, width=2, head=True, label=None, lx=None, ly=None, lanchor="start", lcolor=None):
    d = "M " + " L ".join("%g %g" % p for p in points)
    marker = ' marker-end="url(#h-%s)"' % color.strip("#") if head else ""
    da = ' stroke-dasharray="7 5"' if dash else ""
    out.append('<path d="%s" fill="none" stroke="%s" stroke-width="%g"%s%s/>' % (d, color, width, da, marker))
    if label:
        lines = label.split("\n")
        for i, l in enumerate(lines):
            # white halo so labels stay readable where they cross lines
            out.append('<text x="%g" y="%g" font-family="%s" font-size="11.5" fill="%s" text-anchor="%s" '
                       'stroke="#ffffff" stroke-width="4" paint-order="stroke">%s</text>'
                       % (lx, ly + 14 * i, FONT, lcolor or color, lanchor, escape(l)))


TITLES = []


def layer(y, h, title):
    rect(20, y, 1220, h, C["layer_bg"], "#94a3b8", r=10, sw=1.2)
    TITLES.append((36, y + 24, title))


# --------------------------------------------------------------------------------------------------------------
out.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (W, H, W, H))
out.append("<defs>")
for col in (C["real"], C["fault"], C["data"], C["read"], C["gpu"]):
    out.append('<marker id="h-%s" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
               '<path d="M0 0 L10 5 L0 10 z" fill="%s"/></marker>' % (col.strip("#"), col))
out.append("</defs>")
out.append('<rect width="100%" height="100%" fill="#ffffff"/>')
text(20, 38, "Home SRE Lab", 24, "bold")
text(205, 38, "k3s on a 16 GB Mac, with a simulated 8x H100 GPU fleet and a simulated 24-OSD Ceph cluster", 14, color=C["muted"])

# ---- Layer 1: host ------------------------------------------------------------------------------------------
layer(55, 175, "LAYER 1 — Host: MacBook Pro M2, 16 GB RAM (~11 GB to VMs, ~5 GB for macOS)")
box(40, 95, 240, 110, "Control & CLI", ["lab.sh (control script)", "kubectl · helm · k9s · k6"])
box(350, 95, 300, 110, "Fault state (JSON files)", ["gpu/state.json", "ceph/state.json", "one source of truth for metrics + shells"],
    mono_lines=False)
box(690, 95, 290, 110, "Fake node shells (run on the Mac)",
    ["gpusim: nvidia-smi, dmesg, dcgmi, ibstat,", "  perfquery, ibdiagnet, all_reduce_perf",
     "cephsim: ceph, rados, smartctl, journalctl,", "  chronyc, ip, ping"], size=11.5, lh=15)
box(1010, 95, 210, 110, "Browser", ["Grafana · Prometheus", "Alertmanager", "via nip.io URLs"])

path([(280, 150), (348, 150)], C["fault"], dash=True, label="drill / fault\n/ clear", lx=314, ly=128, lanchor="middle")
path([(650, 150), (688, 150)], C["read"], label="read", lx=669, ly=143, lanchor="middle")

# ---- Layer 2: VMs ------------------------------------------------------------------------------------------
layer(250, 100, "LAYER 2 — Virtualization: Multipass VMs on Apple Hypervisor.framework")
# lab.sh -> Kubernetes API (drawn before cp1 so it disappears into it: the API server runs on cp1)
path([(200, 205), (200, 403)], C["real"])
path([(200, 366), (200, 366)], C["real"], head=False, label="kubectl → API: node status patches, cordon, pod restarts", lx=208, ly=368)
box(40, 282, 300, 55, "cp1 · 2 vCPU / 4 GB", ["k3s server (API, control plane, sqlite)"])
box(360, 282, 260, 55, "w1 · 2 vCPU / 3.5 GB", ["k3s agent"])
box(660, 282, 240, 55, "w2 · 2 vCPU / 3.5 GB", ["k3s agent"])
box(920, 282, 300, 55, "os-aio · 4 vCPU / 10 GB", ["OpenStack AIO · stopped, runs instead of k3s"],
    fill=C["stop_bg"], stroke=C["stop"], dash=True)

# ---- Layer 3: Kubernetes -----------------------------------------------------------------------------------
layer(375, 255, "LAYER 3 — Kubernetes (k3s) workloads on cp1 / w1 / w2")
# kube-system, chaos-mesh, demo column
box(40, 405, 200, 92, "kube-system", ["API server (on cp1)", "KWOK controller:", "fakes 14 kubelets"])
box(40, 505, 200, 50, "chaos-mesh", ["controller + daemons"])
box(40, 563, 200, 55, "demo", ["podinfo 2–6 replicas, HPA,", "SLOs · k6 load from the Mac"], size=11.5, lh=15)
# monitoring
rect(260, 405, 330, 213, C["real_box"], C["real"])
text(270, 424, "monitoring", 13, "bold")
box(275, 438, 150, 48, "Alertmanager", ["routes alerts"], fill="#ffffff")
box(440, 438, 135, 48, "Prometheus", ["rules: gpu, ceph"], fill="#ffffff")
box(440, 500, 135, 48, "Grafana", ["GPU · Ceph · SLO"], fill="#ffffff")
box(275, 500, 150, 48, "Loki · Alloy · Sloth", ["logs, SLO rules"], fill="#ffffff", tsize=12.5)
path([(440, 462), (427, 462)], C["real"])
path([(507, 486), (507, 498)], C["real"])
# gpu-sim
rect(610, 405, 280, 213, C["gpu_bg"], C["gpu"])
text(655, 424, "gpu-sim", 13, "bold")
box(625, 438, 250, 45, "ConfigMap gpusim-state", [], fill="#ffffff", stroke=C["gpu"])
box(625, 505, 250, 62, "gpusim-exporter :9400", ["DCGM + InfiniBand metrics", "for h100-01..08 (runs on a worker)"],
    fill="#ffffff", stroke=C["gpu"], size=11.5, lh=15)
path([(750, 483), (750, 503)], C["fault"], dash=True, label="mounted", lx=758, ly=497)
# ceph-sim
rect(910, 405, 310, 213, C["ceph_bg"], C["ceph"])
text(920, 424, "ceph-sim", 13, "bold")
box(925, 438, 280, 45, "ConfigMap cephsim-state", [], fill="#ffffff", stroke=C["ceph"])
box(925, 505, 280, 62, "cephsim-exporter :9283 (job=\"ceph\")", ["ceph-mgr metrics for 24 OSDs,", "3 mons, pools, PGs (runs on a worker)"],
    fill="#ffffff", stroke=C["ceph"], size=11.5, lh=15, tsize=12.5)
path([(1065, 483), (1065, 503)], C["fault"], dash=True, label="mounted", lx=1073, ly=497)

# state.json -> ConfigMaps (lab.sh syncs them): through the gap between w1 and w2
path([(640, 205), (640, 436)], C["fault"], dash=True)
path([(640, 358), (1000, 358), (1000, 436)], C["fault"], dash=True, head=True,
     label="lab.sh syncs state.json → ConfigMaps", lx=820, ly=352, lanchor="middle")
# exporters -> Prometheus (scrape)
path([(625, 536), (600, 536), (600, 462), (577, 462)], C["real"])
path([(1065, 567), (1065, 600), (600, 600), (600, 536)], C["real"], head=False,
     label="ServiceMonitor scrape every 30s", lx=835, ly=594, lanchor="middle")
# Grafana / Prometheus -> browser
path([(345, 405), (345, 240), (1180, 240), (1180, 207)], C["real"],
     label="dashboards, PromQL, alerts", lx=900, ly=235, lanchor="middle")

# ---- Layer 4: simulated fleets ----------------------------------------------------------------------------
layer(650, 370, "LAYER 4 — Simulated fleets: 14 KWOK fake nodes (real Node objects, no real hardware)")
# KWOK -> fake nodes
path([(40, 450), (30, 450), (30, 700), (38, 700)], C["read"], label="KWOK fakes the kubelets", lx=36, ly=644)

# GPU fleet
rect(40, 685, 570, 320, C["gpu_bg"], C["gpu"], dash=True)
text(52, 707, "GPU fleet · 8 nodes (simulated)", 14, "bold", color=C["gpu"])
for row, (rack, first) in enumerate((("rack-a", 1), ("rack-b", 5))):
    y = 722 + row * 44
    text(52, y + 23, rack, 12, "bold", color=C["muted"])
    for i in range(4):
        rect(110 + i * 72, y, 64, 34, "#ffffff", C["gpu"], r=5)
        text(142 + i * 72, y + 22, "h100-%02d" % (first + i), 11.5, anchor="middle", mono=True)
text(410, 735, "per node:", 12, "bold", color=C["muted"])
text(410, 752, "8x H100 80GB SXM5", 12, color=C["muted"])
text(410, 768, "8x ConnectX-7 NDR 400 Gb/s", 12, color=C["muted"])
text(410, 784, "rail-optimized: GPU n ↔ mlx5_n", 12, color=C["muted"])
text(410, 800, "zone rack-a / rack-b", 12, color=C["muted"])
box(52, 822, 330, 72, "ns ml · StatefulSet llm-train", ["8 pods, one per node", "each requests 8 nvidia.com/gpu + 8 rdma/ib"],
    fill="#ffffff", stroke=C["gpu"])
rect(52, 940, 546, 40, C["gpu_box"], C["gpu"], r=6)
text(325, 965, "InfiniBand NDR 400G fabric · 8 rails · GPU-to-GPU (NCCL) traffic only", 12.5, "bold", anchor="middle")
for x in (142, 214, 286, 358):
    pass
path([(217, 894), (217, 938)], C["gpu"], head=False, width=3)
text(225, 922, "all-reduce", 11.5, color=C["gpu"])

# Ceph cluster
rect(650, 685, 570, 320, C["ceph_bg"], C["ceph"], dash=True)
text(662, 707, "Ceph Squid 19.2 · 6 nodes (simulated)", 14, "bold", color=C["ceph"])
for row, (rack, first) in enumerate((("rack-c", 1), ("rack-d", 4))):
    y = 722 + row * 44
    text(662, y + 23, rack, 12, "bold", color=C["muted"])
    for i in range(3):
        rect(720 + i * 76, y, 68, 34, "#ffffff", C["ceph"], r=5)
        text(754 + i * 76, y + 22, "ceph-%02d" % (first + i), 11.5, anchor="middle", mono=True)
text(960, 735, "per node: 4x 7.68 TB NVMe OSDs", 12, "bold", color=C["muted"])
text(960, 752, "24 OSDs · ~168 TiB raw · 833 PGs", 12, color=C["muted"])
text(960, 768, "replica 3 · failure domain host", 12, color=C["muted"])
text(960, 784, "MON ceph-01/02/03 · MGR 01 (+02)", 12, color=C["muted"])
text(960, 800, "MDS ceph-04 (+ standby 05)", 12, color=C["muted"])
box(780, 822, 425, 72, "Pools & CephFS", ["rbd · cephfs.ckpt.data · cephfs.ckpt.meta", "CephFS \"ckpt\" = llm-train's checkpoint store"],
    fill="#ffffff", stroke=C["ceph"])
rect(662, 940, 546, 40, C["ceph_box"], C["ceph"], r=6)
text(935, 958, "public net 10.10.0.0/24 · cluster (back) net 10.20.0.0/24", 12.5, "bold", anchor="middle")
text(935, 974, "MTU 9000 on both", 11.5, color=C["muted"], anchor="middle")

# checkpoints: llm-train -> CephFS over the public network (not over InfiniBand)
path([(382, 858), (778, 858)], C["data"], width=2.5,
     label="checkpoints (CephFS over public net)", lx=580, ly=850, lanchor="middle")

# ---- side panel --------------------------------------------------------------------------------------------
rect(1260, 55, 320, 965, "#ffffff", "#cbd5e1", r=10, sw=1.2)
text(1278, 82, "Legend", 15, "bold")
items = [("box", C["real_box"], C["real"], False, "Real infrastructure & workloads"),
         ("box", C["gpu_bg"], C["gpu"], True, "Simulated GPU fleet"),
         ("box", C["ceph_bg"], C["ceph"], True, "Simulated Ceph cluster"),
         ("box", C["stop_bg"], C["stop"], True, "Stopped / alternative VM"),
         ("arrow", C["fault"], None, True, "Fault injection path"),
         ("arrow", C["real"], None, False, "Telemetry & control"),
         ("arrow", C["data"], None, False, "Data (checkpoints)"),
         ("arrow", C["read"], None, False, "Reads / fakes")]
for i, (kind, a, b, dash, label) in enumerate(items):
    y = 100 + i * 26
    if kind == "box":
        rect(1280, y, 30, 18, a, b, dash, r=3)
    else:
        path([(1280, y + 9), (1310, y + 9)], a, dash=dash)
    text(1322, y + 14, label, 12.5)

text(1278, 330, "Fault drills", 15, "bold")
blocks = [("GPU (15 with IB)", ["Xid 79 / 94 / 95 / 64 / 31", "ECC storm · thermal · power brake", "NVLink → Xid 74 · PCIe replays"]),
          ("InfiniBand", ["port down · flapping · symbol errors", "degraded width · congestion"]),
          ("Ceph (11)", ["OSD crash · dead NVMe · nearfull", "full OSD · host down · slow ops", "mon disk full · clock skew",
                         "flags left set · inconsistent PG", "MTU mismatch"])]
y = 356
for title, lines in blocks:
    text(1278, y, title, 13, "bold", color=C["muted"])
    for l in lines:
        y += 17
        text(1278, y, l, 12)
    y += 28

text(1278, y + 4, "Workflow", 15, "bold")
y += 18
rect(1278, y, 284, 104, "#f8fafc", "#cbd5e1", r=6)
for i, l in enumerate(["./lab.sh gpu drill 2", "./lab.sh ceph drill 2", "investigate: Grafana,", "  alerts, kubectl, fake shells", "→ reveal → clear"]):
    text(1290, y + 20 + i * 18, l, 12, mono=True)
y += 130
text(1278, y, "github.com/cloudpilott/sre-lab", 12, color=C["faint"])

for x, y, s in TITLES:
    rect(x - 6, y - 16, len(s) * 7.9 + 10, 20, C["layer_bg"], C["layer_bg"], r=4, sw=0)
    out.append('<text x="%g" y="%g" font-family="%s" font-size="15" font-weight="bold" fill="%s" '
               'stroke="%s" stroke-width="6" paint-order="stroke">%s</text>' % (x, y, FONT, C["ink"], C["layer_bg"], escape(s)))
out.append("</svg>")
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "architecture.svg"), "w") as f:
    f.write("\n".join(out) + "\n")
print("wrote docs/architecture.svg")
