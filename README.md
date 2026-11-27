# SRE Lab — MacBook Pro M2 / 16 GB

A 3-node Kubernetes (k3s) cluster in Ubuntu VMs on your Mac, plus a working SRE toolchain:
metrics, logs, dashboards, SLOs with burn-rate alerts, chaos engineering and load testing.

```
macOS (M2, 16 GB)
└── Multipass (QEMU + Apple Hypervisor.framework)
    ├── cp1  2 vCPU / 4 GB    k3s server (control plane + etcd/sqlite)
    ├── w1   2 vCPU / 3.5 GB  k3s agent
    └── w2   2 vCPU / 3.5 GB  k3s agent
         ├── monitoring   Prometheus · Alertmanager · Grafana · Loki · Alloy · Sloth
         ├── chaos-mesh   Chaos Mesh controller + daemons + dashboard
         └── demo         podinfo (2–6 replicas, HPA, ServiceMonitor, SLOs)
```
~11 GB goes to the VMs, ~5 GB stays with macOS. Close heavy apps (Chrome, Docker Desktop) while the lab runs.

## Quick start

```bash
./lab.sh prereqs     # brew: multipass, kubectl, helm, k9s, k6
./lab.sh all         # ~15–25 min: VMs -> k3s -> platform -> demo app -> 'baseline' snapshot
./lab.sh status      # prints URLs
export KUBECONFIG=$PWD/kubeconfig
k9s
```

URLs use `nip.io` (wildcard DNS that maps `grafana.<ip>.nip.io` -> `<ip>`). If your network blocks it,
add the hostnames from `./lab.sh status` to `/etc/hosts`, or `kubectl port-forward` instead.

**macOS 15 note:** the first time, macOS may ask to allow your terminal to find devices on the
local network. Allow it, or `kubectl` cannot reach the VMs (System Settings → Privacy & Security → Local Network).

## Daily use
| Command | What it does |
|---|---|
| `./lab.sh stop` / `start` | Pause the VMs to get RAM and battery back |
| `./lab.sh snapshot before-drill` | Save a known-good state |
| `./lab.sh restore baseline` | Roll back after you broke something |
| `./lab.sh load` | k6 ramp to 50 VUs against podinfo |
| `./lab.sh chaos` | List chaos experiments; `./lab.sh chaos network-delay` runs one |
| `./lab.sh down` | Delete everything |

## Layout
```
lab.sh                 bootstrap + day-2 helper
values/                Helm values (sized for 16 GB)
slo/                   Sloth SLO definitions -> Prometheus burn-rate alerts
dashboards/            Grafana dashboards as ConfigMaps
chaos/                 Chaos Mesh experiments
load/                  k6 scripts
runbooks/              alert runbooks
gpu/                   simulated H100 fleet: gpusim.py, k8s manifests, fault fixtures
ceph/                  simulated Ceph storage cluster: cephsim.py, k8s manifests, fault fixtures
EXERCISES.md           guided SRE drills
```

## Next layers (once the basics feel easy)
- **Argo CD**: move everything in `values/` to GitOps. Rebuild the lab from Git alone.
- **Argo Rollouts**: canary podinfo with an AnalysisTemplate that queries the SLO error ratio.
- **Tempo + OpenTelemetry Collector**: traces. The OpenTelemetry Demo is realistic, but it needs ~4 GB.
  Run it with chaos-mesh uninstalled, or give w1/w2 `W_MEM=4G`.
- **Velero + MinIO**: backup/restore drills.
- **Kyverno**: require limits, block `:latest`.
- **kubeadm rebuild**: redo the cluster with kubeadm + Cilium to practise cert rotation, etcd backup and upgrades.

## Simulated GPU fleet (8x H100 + InfiniBand)
Eight fake 8-GPU nodes join the k3s cluster through KWOK (a few MB of RAM, no real GPUs). A small exporter serves
DCGM and InfiniBand metrics, and a fake node shell prints realistic `nvidia-smi`, `dmesg`, `dcgmi`, `ibstat`, `perfquery`,
`ibdiagnet` and NCCL output. Inject GPU faults (Xid 79/94/95/64/31, ECC storms, thermal, power brake, NVLink, PCIe) and IB faults
(down, flapping, symbol errors, degraded width, congestion), or let `drill` pick a hidden one.

```bash
./lab.sh gpu up            # KWOK + h100-01..08 + exporter + alerts + dashboard + llm-train
./lab.sh gpu drill 1       # hidden fault + ticket; investigate, then: ./lab.sh gpu reveal
./lab.sh gpu ssh h100-03   # fake root shell on a GPU node (works even with the VMs stopped)
```
Details: `gpu/README.md`. Drills: EXERCISES.md level 5. Runbook: `runbooks/gpu-ib-faults.md`.

## Simulated Ceph storage (6 hosts, 24 NVMe OSDs)
Six fake storage hosts join the cluster through KWOK (no real Ceph: that needs 3-4 GB more RAM). An exporter serves
ceph-mgr metrics, and a fake admin shell prints realistic `ceph -s`, `ceph health detail`, `ceph osd df`, `rados`,
`smartctl`, `journalctl` and network output. CephFS `ckpt` is the GPU fleet's checkpoint store. Faults: OSD crash, dead NVMe,
nearfull/full OSD, host down, mon disk full, clock skew, maintenance flags left set, slow ops, inconsistent PG, MTU mismatch.

```bash
./lab.sh ceph up           # 6 storage nodes + exporter + alerts + dashboard
./lab.sh ceph drill 1      # hidden fault + ticket; investigate, then: ./lab.sh ceph reveal
./lab.sh ceph ssh          # fake admin shell on ceph-01 (works even with the VMs stopped)
```
Details: `ceph/README.md`. Drills: EXERCISES.md level 6. Runbook: `runbooks/ceph-faults.md`.

## OpenStack all-in-one (alternative to the k3s lab)
OpenStack 2026.2 via Kolla-Ansible in a 4th VM, `os-aio` (4 vCPU / 10 GB). Run it **instead of** k3s, not beside it.

```bash
./lab.sh stop                 # free the k3s VMs' RAM
./lab.sh openstack up         # create VM, network, install Kolla-Ansible
./lab.sh openstack deploy     # ~45 min: deploy + demo network, CirrOS image and test-vm
./lab.sh openstack status     # services, agents, servers, Horizon URL
```
Compute is emulated (`virt_type=qemu`; the M2 has no nested virtualization), so use tiny CirrOS guests.
Files: `openstack/aio.sh` (runs inside the VM), `openstack/globals.yml.tpl`. Full SOP: section 10 of the lab SOP doc.

---
© 2026 cloudpilott. All rights reserved. Personal portfolio project, not accepting contributions.
