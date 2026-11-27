# Runbook: GPU and InfiniBand faults (H100 fleet)

Simulated in this lab by `./lab.sh gpu ...`. The commands below are the real ones. In the lab, run them
through `./lab.sh gpu ssh <node>` (a fake shell that prints what the real tool would show).

## Triage in 5 minutes
1. **Scope.** One GPU, one node, one rail (same `mlx5_N` on many nodes), or the whole fleet?
   Grafana → *GPU Fleet - H100 + InfiniBand*.
2. **Hardware or software?** Hardware: Xid in the hardware class, ECC/remap counters, link counters, a failed `dcgmi diag`.
   Software: Xid 13/31/43 with clean ECC, a crash that follows the code to other nodes.
3. **Protect the job.** Cordon first (`kubectl cordon <node>`) so a restart doesn't land on the bad node again,
   then drain at a checkpoint if the job is still running.
4. **Confirm on a drained node:** `dcgmi diag -r 3` (it refuses to run while the GPUs are busy).
5. **Decide:** reset / reboot / reseat / RMA / send it back to the user. Write down the serial number (`nvidia-smi -q`).

```bash
dmesg -T | grep -E 'Xid|NVRM|AER|mlx5'
nvidia-smi                                   # GPU missing? Uncorr. ECC column?
nvidia-smi -q -i N -d ECC,ROW_REMAPPER       # DRAM correctable/uncorrectable, Pending, Remapping Failure
nvidia-smi -q -i N -d PERFORMANCE,TEMPERATURE,CLOCK   # clock event reasons
nvidia-smi nvlink -e                         # per-link CRC/replay/recovery
lspci | grep -i nvidia                       # (rev ff) = device not answering on PCIe
ibstat; ibdev2netdev                         # State / Physical state / Rate per HCA
perfquery -C mlx5_N -P 1                     # SymbolErrorCounter, LinkDowned, LinkErrorRecovery, PortXmitWait
ibdiagnet                                    # fabric-wide: down links, width/speed, counters over threshold
all_reduce_perf -b 1M -e 8G -f 4 -g 1        # nccl-tests; compare busbw with the acceptance baseline
```

## Xid table
| Xid | Meaning | Usually | Action |
|---|---|---|---|
| 13 | Graphics engine exception | Application (out-of-bounds etc.) | Back to the user. Hardware only if it repeats across codes on one GPU |
| 31 | GPU memory page fault (MMU) | Application (illegal address) | Back to the user with the Xid line. Check that ECC is clean |
| 43 | GPU stopped processing | Application fault/hang | Usually follows 13/31 |
| 45 | Preemptive cleanup | Follows another error | Look at the Xid before it |
| 48 | Double-bit ECC (older GPUs) | Hardware | Drain, reset, RMA if it repeats |
| 63 | Row remap recorded | Hardware, self-healing | Reset the GPU to apply the remap |
| 64 | Row remap failure | Hardware | RMA |
| 74 | NVLink error | Hardware (link/NVSwitch) | Drain, reset GPU + fabric manager. RMA the baseboard if it repeats |
| 79 | Fell off the bus | Hardware/PCIe/power/thermal | Drain, power-cycle, diag, RMA if it repeats |
| 92 | High single-bit ECC rate | Hardware, degrading | Plan a drain, watch the trend |
| 94 | Contained uncorrectable ECC | Hardware | Only that process killed. Reset to apply the remap |
| 95 | Uncontained uncorrectable ECC | Hardware | All processes on the GPU affected. Drain and reset now |

## GPUMissing
The GPU is gone from DCGM and `nvidia-smi` ("Unable to determine the device handle"), `lspci` shows `rev ff`,
and the device plugin advertises 7 GPUs, so 8-GPU pods stay Pending with `Insufficient nvidia.com/gpu`.
Drain → cold power-cycle (warm reboot often isn't enough) → `dcgmi diag -r 3` → back to service, or RMA if it repeats.
Check the BMC SEL for PCIe, power or thermal events around the Xid timestamp.

## ECC
- **Correctable (SBE)**: not fatal. A rising rate (`GPUCorrectableECCStorm`, Xid 92) warns of trouble. Remaps of correctable rows happen on reset.
- **Uncorrectable (DBE / Xid 94/95)**: the process (94) or every process on the GPU (95) is killed. The row is marked for
  remapping (`Pending: Yes`), and it only takes effect after a GPU reset. After the reset, `Pending: No` → return to service.
- **Remapping Failure Occurred: Yes / Xid 64**: no spare rows left in that bank. A reset won't help. **RMA.**

## Throttling
`nvidia-smi -q -d PERFORMANCE` → *Clocks Event Reasons* (DCGM bitmask: 0x8 HW slowdown, 0x20 SW thermal,
0x40 HW thermal, 0x80 HW power brake).
- **Thermal on one GPU**: fan, heatsink or airflow. One slow GPU drags a synchronous job (straggler): step time rises with no errors.
- **Power brake on all GPUs of one node**: an external signal (PSU failure, BMC power capping). Not the GPUs. Check the PSUs in the BMC.

## NVLink
CRC/replay counters rising on one link (`nvidia-smi nvlink -e`) and lower intra-node bandwidth usually come before
a fatal Xid 74. Drain early. Reset the GPU and restart `nvidia-fabricmanager` (or reboot). If it persists → baseboard/NVSwitch service.

## PCIe
AER *Corrected / Physical Layer / RxErr* from the GPU's root port plus a rising `Replays Since Reset` means a marginal link
(riser, retimer, seating). Host↔device copies slow down. Drain, reseat, update retimer firmware, then service.

## IB down
`ibstat`: `State: Down`, `Physical state: Polling` = no light/peer (cable, optic, switch port). `Disabled` = someone
or something disabled the port. NCCL fails with `status=12` (retry exceeded) or hangs at init. Check the switch side, then reseat/swap the optic.

## IB flap
`LinkDownedCounter` keeps rising (it saturates at 255) along with `LinkErrorRecoveryCounter`. Jobs fail now and then and
reruns sometimes pass. Swap the cable/optic, check the switch port, and clear the counters afterwards (`perfquery -R`) to confirm.

## IB errors
`SymbolErrorCounter` (saturates at 65535) and `PortRcvErrors` rising while the link stays Active = physical bit errors
(dirty connector, damaged fiber, failing transceiver). Throughput drops a little. Clean or replace at the next maintenance and watch for flapping.

## IB degraded
Active, but `Rate: 200` (or lower) instead of 400 on NDR: some lanes didn't train (`ibdiagnet` → *Links Speed/Width Check*).
No errors anywhere, yet ring all-reduce runs at the slowest link, so bus bandwidth can halve. Reseat/replace the cable.

## IB congestion
`PortXmitWait` rising quickly with zero errors and full-rate Active links = the port has data but no credits. Congestion
downstream (another tenant, hotspot uplink, routing). **Not a node fault. Don't RMA.** Look fabric-wide, at adaptive
routing/SHARP, and at job placement.

## Build standard (locked versions)
Every node must match `gpu/locked-versions.json` before it takes customer work: kernel, driver, Fabric Manager, CUDA,
DCGM, VBIOS, DOCA-OFED, HCA firmware, nvidia_fs/GDS, BIOS, iDRAC. Check with `./lab.sh gpu compliance`, by hand with
`cat /proc/driver/nvidia/version`, `nv-fabricmanager --version`, `flint -d mlx5_N q`, `racadm getversion`, `uname -r`, `gdscheck -p`.
- **After any part swap or re-image, rerun compliance.** RMA replacement cards often ship with older firmware.
- **Driver and Fabric Manager are one unit.** If they differ, FM won't start (`systemctl status nvidia-fabricmanager`:
  *don't match with Fabric Manager version*), `nvidia-smi -q` shows Fabric State *In Progress*, and CUDA fails with error 802.
- **Pin the kernel.** A newer kernel without a rebuilt `nvidia_fs` silently drops GPUDirect Storage to compatibility mode
  (`lsmod` has no nvidia_fs, `gdscheck -p` shows N/A, `gdsio` ~1/3 speed).
- **Link width is a silent failure.** A GPU at PCIe x8 logs no error: check `nvidia-smi -q -d PCI` *Link Width Current*
  and `lspci -vv | grep LnkSta` (*downgraded*). Reseat via remote hands.

## Acceptance (ATP) before handover
`./lab.sh gpu atp` (see `gpu/README.md`). Treat the first failing check on a node as the root cause and the rest as
consequences. Record results against thresholds, raise one defect per root cause (Config / Hardware rework / RMA),
rerun the ATP on the reworked node, and attach the report to the handover.

## After
Postmortem template in EXERCISES.md. For hardware faults, record the node, GPU/HCA, serial/GUID, Xid/counters, action, and RMA ticket.
