# Contained uncorrectable ECC error (Xid 94)

Ticket: llm-train died with 'CUDA error: uncorrectable ECC error encountered' on llm-train-2. It restarted on the same nodes.

<details><summary>Answer</summary>

h100-03 GPU 5: a contained DRAM uncorrectable error (Xid 94). Only the process using that memory was killed. A row remap is pending (Xid 63, 'Pending: Yes'), and it only takes effect after a GPU reset. Action: drain h100-03, reset the GPU (nvidia-smi -r -i 5) or reboot, confirm Pending: No, then return it to service. RMA only if remapping fails or the errors keep coming.

</details>
