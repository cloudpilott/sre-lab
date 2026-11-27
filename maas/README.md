# Bare-metal provisioning layer: MAAS + Terraform

How the lab's fake hardware would really get built. In a Dell data centre the layers are:

```
Dell server racked + cabled
  -> iDRAC / Redfish: firmware + BIOS baseline         (OpenManage, dellemc.openmanage Ansible)
  -> MAAS: power on, PXE, commission, tag, install OS   <- this layer
       ^ driven by Terraform (machines and network as code, reviewed in PRs)
  -> Ansible: hardening, drivers, tuning, agents
  -> Kubernetes, GPU Operator, cephadm -> GitOps -> Prometheus
```

## What is real and what is simulated

| Piece | In this lab |
|---|---|
| MAAS 3.7 region + rack controller | **Real**, in the `maas` VM (2 vCPU, 4 GB + 2 GB swap) with the test PostgreSQL snap |
| Terraform `canonical/maas` provider | **Real**: settings, DNS domain, zones, pools, fabrics, VLANs, spaces, subnets, IP ranges, tags |
| Brownfield import of cp1/w1/w2 | **Real, partly**: `./lab.sh maas enroll` registers them as *Deployed* and Terraform tags them `k3s`. Their hardware inventory is rejected (see below) |
| 14 Dell servers (8x XE9680, 6x R760xd) | **Modelled**: in `terraform/maas/fleet.tf`, shown with `./lab.sh maas fleet-plan` (plan only) |
| PXE boot / DHCP | **Off**: needs a provisioning VLAN and BMCs; a Mac can't PXE-boot VMs, and MAAS DHCP on the Multipass network would break the lab |

## Use

```bash
brew install hashicorp/tap/terraform
./lab.sh maas up            # VM + MAAS + admin user + API key (.maas/, gitignored)
./lab.sh maas tf init
./lab.sh maas tf plan
./lab.sh maas tf apply      # network plan, zones, pools, tags, settings
./lab.sh maas enroll        # import cp1 w1 w2 as deployed machines
./lab.sh maas tf apply      # now also tags them "k3s"
./lab.sh maas fleet-plan    # what Terraform would do to the Dell fleet
./lab.sh maas status        # API, image import, machine list
./lab.sh maas ui            # web UI URL + where the password is
```

Free RAM: the `maas` VM adds 4 GB to the running lab. If the Mac struggles, `./lab.sh maas stop` when you're done.
The `maas` VM isn't part of `./lab.sh stop|start|snapshot`; use `./lab.sh maas stop|start|snapshot`.

## Known limit: hardware inventory of k3s nodes

`enroll` runs MAAS's commissioning scripts on each node. They succeed, but MAAS fails to *store* the results
(commissioning status **Failed**, event `Subnet 10.42.x.0/24 would overlap with existing subnets`). Every IP on a host
becomes a MAAS subnet with the interface's exact prefix, and on a k3s node flannel.1 has `10.42.x.0/32` and cni0 has
`10.42.x.0/24`: nested prefixes MAAS can't model. Pre-creating `10.42.0.0/16` doesn't help, because MAAS only reuses an
exact CIDR match. In a real DC this doesn't come up: MAAS commissions servers *before* Kubernetes adds CNI
interfaces. Brownfield import works on hosts without nested subnets.

## Files

| File | What it holds |
|---|---|
| `maas/bootstrap.sh` | Runs in the VM: snap installs, `maas init region+rack`, admin user, API key |
| `terraform/maas/versions.tf` | Provider pin; credentials come from `MAAS_API_URL` / `MAAS_API_KEY` |
| `terraform/maas/settings.tf` | Global settings, DNS domain, zones (racks a-c), resource pools |
| `terraform/maas/network.tf` | Fabrics `oob` and `prod`; VLANs 30/40/41/50; spaces; subnets; reserved and PXE ranges; optional DHCP |
| `terraform/maas/tags.tf` | XPath auto-tags (NVIDIA, H100 + IOMMU kernel opts, NVMe, Dell) and manual tags |
| `terraform/maas/fleet.tf` | Dell fleet: Redfish power via iDRAC, pools, racks, deploy with static IPs and cloud-init |
| `terraform/maas/templates/user-data.yaml.tftpl` | cloud-init hand-off to Ansible (ops user, chrony, lldpd, role fact) |

## Network plan

| Network | Fabric / VLAN | CIDR | MTU | Purpose |
|---|---|---|---|---|
| oob | oob / untagged | 172.16.10.0/24 | 1500 | iDRAC |
| pxe | prod / untagged | 172.16.20.0/22 | 1500 | PXE + install (MAAS DHCP in a real DC) |
| k8s | prod / 30 | 172.16.30.0/24 | 1500 | Kubernetes |
| ceph-public | prod / 40 | 172.16.40.0/24 | 9000 | Ceph clients |
| ceph-cluster | prod / 41 | 172.16.41.0/24 | 9000 | Ceph replication (the `mtu-mismatch` drill lives here) |
| gpu-storage | prod / 50 | 172.16.50.0/24 | 9000 | GPU nodes to storage (RoCE) |

## Drills

1. Run `tf plan` after changing one VLAN's MTU in the MAAS UI. Explain the drift Terraform reports and fix it from code.
2. `terraform import` a zone you created by hand, so Terraform manages it.
3. Add an `inference` resource pool and move two GPU nodes into it. Read the `fleet-plan` diff before applying anything.
4. Why does `gpu-h100` set `iommu=pt`? Why is the Ceph CRUSH failure domain *rack* and not *host* in `fleet.tf`?
5. In the UI, find the hardware inventory MAAS collected from `w1` during enroll (CPU, memory, NICs, disks).
6. Move state to a remote backend with locking, and explain what goes wrong when two people `apply` against local state.
