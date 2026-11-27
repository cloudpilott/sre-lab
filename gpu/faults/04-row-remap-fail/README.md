# Row-remapping failure (Xid 64)

Ticket: llm-train-2 keeps crashing with ECC errors even after the node was rebooted yesterday.

<details><summary>Answer</summary>

h100-03 GPU 5 has run out of spare rows: Xid 64, 'Remapping Failure Occurred: Yes', and dcgmi diag fails the memory test. A reset will not fix it. Action: take h100-03 out of service and RMA the GPU.

</details>
