# Practice cheat sheet: Kubernetes + GPU fleet + Ceph

Commands use this lab's names: namespaces `demo` (podinfo), `monitoring`, `ml` (llm-train), `gpu-sim`;
real nodes `cp1 w1 w2`; fake GPU nodes `h100-01`…`h100-08`. Each section ends with **Try**: small tasks to practice.

One-time setup. Add these lines to `~/.zshrc`, then open a new terminal:
```bash
export KUBECONFIG=~/sre-lab/kubeconfig
alias k=kubectl
setopt interactive_comments          # lets you paste lines that end in "# comment"
autoload -Uz compinit && compinit
source <(kubectl completion zsh); compdef k=kubectl
```
**Run commands one line at a time.** The blocks below are reference lists, not scripts. Pasting a whole block runs
every line, including `reveal` and `clear` in the drill workflow.

---

## 1. Look around
```bash
k get nodes -o wide                       # status, IPs, kernel, runtime
k get ns
k get pods -A                             # every pod in every namespace
k get pods -n demo -o wide                # which node each pod runs on
k get all -n demo                         # deployments, replicasets, pods, services, hpa
k api-resources | less                    # every resource type the cluster knows
k explain pod.spec.tolerations            # built-in docs for any field
k get pod <pod> -n demo -o yaml           # the full object, including status
```
**Try:** How many pods run on `w1`? (`k get pods -A -o wide --field-selector spec.nodeName=w1`)
Which namespace has the most pods?

## 2. Debug a pod
```bash
k describe pod <pod> -n demo              # read the Events at the bottom first
k logs <pod> -n demo                      # add -f to follow, --previous for the crashed container
k logs deploy/podinfo -n demo --tail=50
k exec -it <pod> -n demo -- sh            # shell inside the container
k get events -n demo --sort-by=.lastTimestamp
k top pods -n demo; k top nodes           # needs metrics-server (bundled with k3s)
k port-forward svc/podinfo -n demo 9898:9898   # then curl localhost:9898
```
| Pod status | Usual cause | Look at |
|---|---|---|
| `Pending` | No node fits (CPU/mem/GPU, taints, nodeSelector) | `describe` → Events: `0/11 nodes are available: ...` |
| `ImagePullBackOff` | Wrong image/tag, no registry access | `describe` → Events |
| `CrashLoopBackOff` | App exits on start | `logs --previous` |
| `OOMKilled` | Memory limit too low | `describe` → Last State, `k top pod` |
| `Running` but 0/1 Ready | Readiness probe failing | `describe` → probe events, `logs` |

**Try:** `helm upgrade podinfo podinfo/podinfo -n demo --reuse-values --set image.tag=does-not-exist`.
Find the cause from `describe`, then `helm rollback podinfo -n demo`.

## 3. Deploy, scale, roll back
```bash
k scale deploy/podinfo -n demo --replicas=4
k rollout status deploy/podinfo -n demo
k rollout history deploy/podinfo -n demo
k rollout undo deploy/podinfo -n demo
k set resources deploy/podinfo -n demo --requests=cpu=100m,memory=64Mi
k get hpa -n demo -w                      # watch the autoscaler during ./lab.sh load
helm list -A; helm history podinfo -n demo; helm get values podinfo -n demo
k create deploy nginx --image=nginx --dry-run=client -o yaml > nginx.yaml   # generate YAML, then edit it
k diff -f nginx.yaml; k apply -f nginx.yaml; k delete -f nginx.yaml
```
**Try:** Run `./lab.sh load` and watch the HPA scale podinfo. What triggered it, and how long until it scaled back down?

## 4. Nodes and scheduling (core skill for GPU clusters)
```bash
k get nodes -L type,nvidia.com/gpu.product,topology.kubernetes.io/zone
k describe node h100-03                   # Capacity vs Allocatable, Taints, "Allocated resources"
k get node h100-03 -o jsonpath='{.status.allocatable}' | python3 -m json.tool
k get nodes -o custom-columns='NODE:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu,IB:.status.allocatable.rdma/ib'
k cordon h100-03                          # no new pods
k drain h100-03 --ignore-daemonsets --delete-emptydir-data   # evict pods (respects PDBs)
k uncordon h100-03
k taint node w2 maint=true:NoSchedule     # add a taint
k taint node w2 maint=true:NoSchedule-    # remove it (trailing -)
k label node w1 disk=ssd                  # add a label;  k label node w1 disk-  removes it
```
How a pod ends up on a GPU node: **resource request** (`nvidia.com/gpu: 8`) + **nodeSelector/affinity** (labels) +
**tolerations** (the node's taints). Read `gpu/k8s/llm-train.yaml` to see all three.

**Try:**
- Why can't a podinfo pod land on `h100-01`? (Hint: taints.)
- Cordon `h100-02`, then delete `llm-train-1`. Where does it go, and why is it Pending?
- Find which node each `llm-train` pod runs on: `k get pods -n ml -o wide`.

## 5. Config, networking, storage
```bash
k get cm,secret -n monitoring
k get secret mon-grafana -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d; echo
k get svc,ingress -A
k get endpoints podinfo -n demo           # empty = selector doesn't match any Ready pod
k run tmp --rm -it --image=busybox -- sh  # throwaway pod: nslookup podinfo.demo, wget -qO- podinfo.demo:9898
k get pvc,pv -A; k get storageclass
```
**Try:** From a busybox pod, resolve `podinfo.demo.svc.cluster.local`. Then run `./lab.sh chaos dns-failure` and try again.

## 6. RBAC and "who can do what"
```bash
k auth can-i create pods -n demo
k auth can-i --list --as=system:serviceaccount:demo:default -n demo
k get clusterrolebindings | grep -i prometheus
```

## 7. Prometheus queries (Grafana → Explore → Prometheus)
Kubernetes:
```promql
sum by (namespace) (kube_pod_status_phase{phase="Pending"})
increase(kube_pod_container_status_restarts_total[1h]) > 0
kube_node_spec_unschedulable == 1                                   # cordoned nodes
kube_node_status_allocatable{resource="nvidia_com_gpu"}             # GPUs the scheduler sees per node
```
GPU (DCGM metric names):
```promql
count by (Hostname) (DCGM_FI_DEV_GPU_TEMP)                          # visible GPUs per node (expect 8)
DCGM_FI_DEV_XID_ERRORS > 0                                          # last Xid per GPU
topk(5, DCGM_FI_DEV_GPU_TEMP)
DCGM_FI_DEV_SM_CLOCK < 1500 and DCGM_FI_DEV_GPU_UTIL > 50           # busy but slow = straggler
rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[10m]) * 60                       # correctable ECC per minute
DCGM_FI_DEV_ROW_REMAP_PENDING == 1 or DCGM_FI_DEV_ROW_REMAP_FAILURE == 1
(floor(DCGM_FI_DEV_CLOCK_THROTTLE_REASONS / 64) % 2) == 1           # HW thermal slowdown bit
avg by (Hostname) (DCGM_FI_DEV_POWER_USAGE)
```
InfiniBand:
```promql
node_infiniband_state_id != 4                                       # port not Active
node_infiniband_rate_bytes_per_second * 8 / 1e9                     # Gb/s (expect 400)
rate(node_infiniband_symbol_error_total[10m]) * 60 > 0
increase(node_infiniband_link_downed_total[30m]) > 0
sum by (Hostname) (rate(node_infiniband_port_transmit_wait_total[5m]))   # congestion
sum by (Hostname) (rate(node_infiniband_port_data_transmitted_bytes_total[2m]))
```
**Try:** Write a query that returns only nodes where *any* GPU is hotter than 85C. Then turn it into an alert rule.

---

## 8. GPU node commands (`./lab.sh gpu ssh h100-03`)
| Command | What to look for |
|---|---|
| `nvidia-smi` | Missing GPU? Non-zero *Volatile Uncorr. ECC*? Temp, power, memory, util |
| `nvidia-smi -q -i 5` | Everything about one GPU (serial, UUID, bus ID) |
| `nvidia-smi -q -i 5 -d ECC,ROW_REMAPPER` | DRAM correctable/uncorrectable, *Pending*, *Remapping Failure Occurred* |
| `nvidia-smi -q -i 5 -d PERFORMANCE,TEMPERATURE,CLOCK` | *Clocks Event Reasons*: HW thermal slowdown, power brake |
| `nvidia-smi -q -d PCI` | *Replays Since Reset*, link gen/width |
| `nvidia-smi nvlink -s` / `-e` | Link speeds (`<inactive>`?) / CRC, replay, recovery errors |
| `dmesg -T \| grep -E 'Xid\|NVRM'` | Xid number + bus ID = which GPU and what failed |
| `dmesg -T \| grep -E 'AER\|mlx5'` | PCIe errors, IB link events |
| `lspci \| grep -i nvidia` | `(rev ff)` = GPU not answering on PCIe |
| `dcgmi diag -r 3` | Full health check (only on a drained node) |

Map a bus ID to a GPU index: `nvidia-smi -q | grep -E '^GPU |Minor Number'`.

**Xid quick reference:** 13/31/43 → usually the application · 63 → row remapped, reset to apply · 64 → remap failed, RMA ·
74 → NVLink · 79 → fell off the bus · 92 → high correctable-ECC rate · 94 → contained ECC (one process) ·
95 → uncontained ECC (whole GPU). Full table: `runbooks/gpu-ib-faults.md`.

## 9. InfiniBand commands
| Command | What to look for |
|---|---|
| `ibstat` / `ibstat mlx5_3` | `State: Active`, `Physical state: LinkUp`, `Rate: 400` |
| `ibdev2netdev` | Which HCAs are Up/Down at a glance |
| `perfquery -C mlx5_3 -P 1` | SymbolErrorCounter, LinkDownedCounter, LinkErrorRecoveryCounter, PortRcvErrors, PortXmitWait |
| `perfquery -x -C mlx5_3 -P 1` | 64-bit data/packet counters |
| `ibdiagnet` | Fabric-wide: down links, wrong width/speed, counters over threshold |
| `all_reduce_perf -b 1M -e 8G -f 4 -g 1` (or `./lab.sh gpu nccl`) | busbw at 8 GB vs the ~190 GB/s baseline |

State codes: 1 Down · 2 Init · 3 Armed · 4 Active. Physical: 2 Polling (no peer/light) · 3 Disabled · 5 LinkUp.

## 10. Fault workflow (drill order)
A checklist. Type each step yourself, filling in the `<...>` parts. Don't paste it as a block.

1. Start the drill and read the ticket: `./lab.sh gpu drill 1`
2. Anything Pending or restarted? Which node? `kubectl get pods -n ml -o wide`
3. Why is it Pending? `kubectl describe pod <pending-pod> -n ml` (look for `Insufficient nvidia.com/gpu` or `rdma/ib`)
4. Grafana *GPU Fleet* dashboard and Alertmanager: which node, GPU or HCA?
5. Confirm on the node: `./lab.sh gpu ssh <node>`, then `dmesg -T`, `nvidia-smi`, `ibstat`, `perfquery`
6. Stop new work landing there: `kubectl cordon <node>`
7. Drain it: `kubectl drain <node> --ignore-daemonsets`, then on the node: `dcgmi diag -r 3`
8. **Only after you've written down your diagnosis:** `./lab.sh gpu reveal`
9. Heal and uncordon: `./lab.sh gpu clear`
Decide every time: **reset · reboot · reseat cable · RMA · send back to the user** — and write down why.

## 11. Ceph commands (`./lab.sh ceph ssh`, admin host ceph-01)
| Command | What to look for |
|---|---|
| `ceph -s` | Health, mons in quorum, OSDs up/in, PG states, degraded %, recovery rate |
| `ceph health detail` | The health check codes (`OSD_DOWN`, `PG_DEGRADED`, `MON_DOWN`, …) and which daemon/PG |
| `ceph osd tree` | Which OSD/host is down, REWEIGHT 0 = out, CRUSH WEIGHT = size in TiB |
| `ceph osd df tree` | `%USE`, `VAR` (vs average), `PGS` per OSD: imbalance, nearfull |
| `ceph osd perf` | commit/apply latency per OSD: one outlier = slow disk |
| `ceph df` | Pool STORED / MAX AVAIL (set by the fullest OSD) |
| `ceph osd dump \| grep -e flags -e ratio` | Maintenance flags left set; nearfull/full ratios |
| `ceph pg dump_stuck` / `ceph pg ls degraded` | Stuck PGs, their state and acting OSDs |
| `rados list-inconsistent-obj <pg> --format=json-pretty` | Which shard of an inconsistent PG is bad |
| `ceph crash ls` / `ceph crash info <id>` | Daemon crashes: assert message and backtrace |
| `ceph orch ps` / `ceph orch host ls` | Daemons in `error`, hosts `Offline` |
| `ceph device ls` | Disk per OSD and its wear |
| `ceph mon stat` / `ceph time-sync-status` | Quorum, clock skew |
| `ceph config dump \| grep debug` | Debug logging someone forgot to turn off |
| `journalctl -u ceph-osd@<id>` / `-u ceph-mon` | Why the daemon stopped |
| `smartctl -a /dev/nvme0n1` | Percentage Used (wear), Media and Data Integrity Errors, error log |
| `dmesg -T`, `lsblk`, `df -h`, `chronyc tracking`, `ip link` | Disk gone, disk full, NTP, MTU |
| `ping -M do -s 8972 10.20.0.11` | Jumbo frames on the cluster network (a normal ping proves nothing) |

PG states: `active+clean` good · `undersized+degraded` copy missing · `remapped+backfilling` moving data ·
`recovery_wait` queued · `peered`/`inactive`/`incomplete` **I/O blocked** · `inconsistent` scrub found bad data.
PromQL: `(ceph_osd_up == 0) * on(ceph_daemon) group_left(hostname) ceph_osd_metadata` (down OSDs with their host) ·
`ceph_health_detail == 1` (active checks) · `topk(5, ceph_osd_commit_latency_ms)` · `ceph_osd_stat_bytes_used / ceph_osd_stat_bytes`.
Drill flow: same as section 10 with `./lab.sh ceph drill 1` → `ceph -s` → `ceph health detail` → host shell → write the fix → `./lab.sh ceph reveal` → `./lab.sh ceph clear`.

---

## 2-week practice plan
| Day | Do |
|---|---|
| 1 | Sections 1–2. Break podinfo three ways (bad image, `faults.error=true`, tiny memory limit) and fix each one |
| 2 | Section 3. Load test, HPA, rollout + rollback, `helm history` |
| 3 | Section 4 on real nodes: cordon/drain `w2`, taint/untaint, PodDisruptionBudget for podinfo |
| 4 | Section 5. Services, endpoints, DNS, `./lab.sh chaos dns-failure` |
| 5 | Section 7. Write 5 PromQL queries from memory and one alert rule |
| 6 | `./lab.sh gpu up`. Section 4 on GPU nodes: describe them, read allocatable, schedule llm-train by hand |
| 7 | Section 8 on a healthy node; read `gpu/faults/00-healthy/` until you know the normal output |
| 8 | `xid79`, `xid94`, `xid95`, `row-remap-fail`: one fault, one diagnosis, one decision each |
| 9 | `thermal`, `power-brake`, `sbe-storm`, `xid31`: stragglers and "not the hardware" |
| 10 | Section 9: `ib-down`, `ib-flap`, `ib-symbol` |
| 11 | `ib-degraded`, `ib-congestion`, `nvlink` (catch it within 20 min) |
| 12–13 | `./lab.sh gpu drill 2` and `3` blind, time yourself, write a postmortem for one |
| 14 | EXERCISES.md drill 27: redesign the Xid alerts with recording rules and routing |

**Week 3 (Ceph, section 11):**
| Day | Do |
|---|---|
| 15 | `./lab.sh ceph up`. Healthy baseline: `ceph -s`, `osd df`, `osd perf`, `df`; read `ceph/faults/00-healthy/` |
| 16 | `disk-fail` then `osd-crash`: watch down → out → backfill; software or hardware? |
| 17 | `nearfull`, `osd-full`: why a 60%-used cluster stops writes |
| 18 | `host-down`, `flags-left`, `mon-disk-full`: maintenance and quorum |
| 19 | `slow-ops`, `pg-inconsistent`, `mtu-mismatch`: the subtle ones |
| 20–21 | `./lab.sh ceph drill 2` and `3` blind, then EXERCISES.md drill 37 (GPU + Ceph tickets at once) |
