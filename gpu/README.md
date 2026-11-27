# Simulated H100 fleet

8 fake nodes (`h100-01`…`h100-08`), each with 8 H100 80GB SXM5 GPUs and 8 ConnectX-7 NDR (400 Gb/s) HCAs, rail-optimized
(GPU *n* ↔ `mlx5_n`). Nothing is emulated at the hardware level. Three cheap layers make it look real:

| Layer | What | Cost |
|---|---|---|
| **KWOK** fake kubelets | Real Node objects with `nvidia.com/gpu: 8`, `rdma/ib: 8`, GPU labels and taints. Pods schedule, go Pending, get evicted, drain. | ~50 MB |
| **gpusim exporter** | DCGM-exporter metric names (`DCGM_FI_DEV_*`) and node_exporter IB names (`node_infiniband_*`) for all 64 GPUs / 64 ports | ~30 MB |
| **gpusim shell** | `nvidia-smi` (`-q`, `-L`, `topo -m`, `nvlink -e`), `dmesg`, `lspci -vv`, `dcgmi diag`, `ibstat`, `ibdev2netdev`, `perfquery`, `ibdiagnet`, `all_reduce_perf`, plus build/acceptance tools: `nvbandwidth`, `hpl.sh`, `gdsio`, `gdscheck`, `flint`, `racadm getversion`, `ofed_info`, `nv-fabricmanager`, `systemctl status nvidia-fabricmanager`, `lsmod` | runs on the Mac |
| **ATP runner** | `gpu/atp.py`: runs those tools on every node, parses the output against `gpu/locked-versions.json` and `gpu/atp-thresholds.json`, times each step, writes a report | runs on the Mac |

Everything reads one fault state (`gpu/state.json`), so metrics, kubectl, and the node tools all agree.

```bash
./lab.sh start && ./lab.sh gpu up      # once: KWOK, nodes, exporter, alerts, dashboard, llm-train StatefulSet
./lab.sh gpu drill 1                   # random hidden fault + ticket (levels 1-3)
./lab.sh gpu ssh h100-03               # poke around a node
./lab.sh gpu reveal && ./lab.sh gpu clear
```

## Faults
`./lab.sh gpu faults` lists them. `./lab.sh gpu fault <type> <node> [gpu|hca]` injects one where you choose.

| Type | Looks like | Lesson |
|---|---|---|
| `xid79` | GPU gone from DCGM/nvidia-smi, `rev ff`, allocatable 7, pod Pending | Device-plugin capacity ties hardware to scheduling |
| `xid94` / `xid95` | Uncorrectable ECC, row remap **pending** | Contained vs uncontained. A reset applies the remap |
| `row-remap-fail` | Xid 64, `Remapping Failure Occurred: Yes`, diag fails | When a reset won't help: RMA |
| `sbe-storm` | Correctable ECC climbing, Xid 92 later | Trend alerts and planned drains |
| `thermal` | 90C, HW thermal slowdown, half SM clock | Stragglers slow synchronous jobs |
| `power-brake` | All 8 GPUs on a node at ~250W / 700 MHz | External cause (PSU/BMC), not the GPUs |
| `nvlink` | CRC errors on link 7 → Xid 74 after 20 min | Catch it before it crashes |
| `pcie-replay` | Replays + AER RxErr in dmesg | Marginal PCIe link |
| `xid31` | MMU fault, but ECC clean and diag passes | Application bug: don't RMA |
| `ib-down` | Port Down/Polling, NCCL `status=12`, `rdma/ib` 7 | Physical link |
| `ib-flap` | LinkDowned every 3 min | Intermittent failures |
| `ib-symbol` | Symbol/rcv errors, link Active | Dirty/failing optic |
| `ib-degraded` | Active at 200 Gb/s (2X) | Slowest link sets ring speed |
| `ib-congestion` | PortXmitWait soaring, zero errors | Congestion is not a hardware fault |
| `fw-drift` | One HCA on older firmware after a card swap; no symptoms | Only a compliance check catches it |
| `fm-mismatch` | Driver ≠ Fabric Manager, FM failed, CUDA error 802 | Driver and FM must be pinned together |
| `pcie-downtrain` | GPU link at x8, half H2D bandwidth, no errors | ATP must check link width explicitly |
| `gds-compat` | New kernel, `nvidia_fs` not loaded, GDS ~1/3 speed | Silent fallbacks; pin the kernel |

## Acceptance testing (ATP)
How a new or repaired node is proven before handover. `./lab.sh gpu atp` runs, on each node:

| Step | Tool | Pass when | Real run time |
|---|---|---|---|
| versions | `uname`, `/proc/driver/nvidia/version`, `nv-fabricmanager`, `ofed_info`, `flint`, `gdscheck`, `racadm` | everything equals `locked-versions.json` | ~1 min |
| inventory | `nvidia-smi -L`, `lspci` | 8 GPUs, none `rev ff` | seconds |
| fabric | `systemctl`, `nvidia-smi -q -d FABRIC` | FM active, fabric Completed / Success | seconds |
| topology | `nvidia-smi topo -m` | NV18 between every GPU pair, GPU *n* ↔ NIC *n* = PIX | seconds |
| pcie | `nvidia-smi -q -d PCI` | Gen5 x16, replays under limit | seconds |
| ecc | `nvidia-smi -q -d ECC,ROW_REMAPPER` | no DBE, no pending or failed remap | seconds |
| ib | `ibstat`, `perfquery` | 8 ports Active at 400 Gb/s, 0 symbol errors | ~1 min |
| dcgm | `dcgmi diag -r 3` | Pass - All | ~15 min |
| nvbandwidth | `nvbandwidth` | H2D ≥ 50 GB/s per GPU, GPU↔GPU ≥ 350 GB/s | ~3 min |
| nccl | `all_reduce_perf -g 8` | ≥ 450 GB/s busbw over NVLink | ~5 min |
| hpl | `hpl.sh` | ≥ 390 TFLOPS and residual PASSED | ~9 min |
| gds | `gdsio` per GPU | ≥ 5.0 GiB/s read per GPU | ~7 min |
| cluster | `all_reduce_perf` over 64 GPUs | ≥ 180 GB/s busbw over IB | ~10 min |

The report (`gpu/reports/atp-<time>.md`, plus `.json` and a Prometheus `.prom` textfile) has a check matrix, a defect
list with the **root cause** per node (the first failing check; later failures are marked as consequences), the timing
of every step with its share of the critical path, and the evidence (command + key output lines) for every check.
The simulator answers instantly, so step durations are modelled on real run times. Tested nodes are treated as drained.

```bash
./lab.sh gpu fault fm-mismatch h100-04 && ./lab.sh gpu fault pcie-downtrain h100-02 3
./lab.sh gpu compliance                # 1-2 min: versions only
./lab.sh gpu atp                       # full ATP, exit code 1 on any defect
./lab.sh gpu atp h100-02 h100-04       # just the reworked nodes
```

## Offline study
`gpu/faults/<NN-type>/` holds every tool's output for each fault on `h100-03` GPU/HCA 5, 25 min after onset, plus
a `README.md` with the ticket and a hidden answer. Diff any of them against `00-healthy/`. Regenerate with `./lab.sh gpu fixtures`.

## How it behaves
- Fault changes reach Prometheus in about 1-2 min (ConfigMap volume sync + 30s scrape).
- When a fault crashes the job, `lab.sh` deletes that node's `llm-train` pod (the "torchrun restart"). It reschedules,
  or stays Pending when the node lost a GPU or HCA.
- A node with no running `llm-train` pod (drained) counts as idle, so `dcgmi diag` only runs after you
  `kubectl drain <node> --ignore-daemonsets`, as on real hardware.
- KWOK nodes can't run containers. The exporter runs on w1/w2 and serves all 8 fake nodes (label `Hostname`).
- The output formats follow the real tools closely, but they aren't byte-exact. The kernel `mlx5` lines in particular are approximate.
