# Uncontained uncorrectable ECC error (Xid 95)

Ticket: All ranks on h100-03 died at once. Logs show 'uncorrectable ECC error' on several ranks, not just one.

<details><summary>Answer</summary>

h100-03 GPU 5: an uncontained ECC error (Xid 95). The driver couldn't isolate it, so every process on the GPU was affected. A row remap is pending. Action: drain h100-03 and reset or reboot (required). Confirm the remap and run dcgmi diag -r 3 before returning it. Repeat offender: RMA.

</details>
