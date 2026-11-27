# GPU fell off the bus (Xid 79)

Ticket: llm-train crashed at step 41,207 with 'CUDA error: unspecified launch failure' on llm-train-2. After the automatic restart one pod is stuck Pending.

<details><summary>Answer</summary>

h100-03 GPU 5 (bus 0000:ab:00.0) fell off the PCIe bus: Xid 79, the GPU is gone from nvidia-smi and DCGM, lspci shows rev ff, and the device plugin now advertises 7 GPUs, so the 8-GPU pod can't schedule anywhere. Action: cordon + drain h100-03, power-cycle (a warm reboot often isn't enough), run dcgmi diag -r 3. If it repeats, RMA the GPU or baseboard. Check the BMC for PCIe/power/thermal events.

</details>
