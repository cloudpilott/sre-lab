# Simulated H100 fleet

8 fake nodes (`h100-01`…`h100-08`), each with 8 H100 80GB SXM5 GPUs and 8 ConnectX-7 NDR (400 Gb/s) HCAs, rail-optimized
(GPU *n* ↔ `mlx5_n`). Nothing is emulated at the hardware level. Three cheap layers make it look real:

| Layer | What | Cost |
|---|---|---|
| **KWOK** fake kubelets | Real Node objects with `nvidia.com/gpu: 8`, `rdma/ib: 8`, GPU labels and taints. Pods schedule, go Pending, get evicted, drain. | ~50 MB |
| **gpusim exporter** | DCGM-exporter metric names (`DCGM_FI_DEV_*`) and node_exporter IB names (`node_infiniband_*`) for all 64 GPUs / 64 ports | ~30 MB |
| **gpusim shell** | `nvidia-smi`, `nvidia-smi -q`, `nvlink -e`, `dmesg`, `lspci`, `dcgmi diag`, `ibstat`, `ibdev2netdev`, `perfquery`, `ibdiagnet`, `all_reduce_perf` | runs on the Mac |

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
