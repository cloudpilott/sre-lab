# MMU fault from application (Xid 31)

Ticket: New experiment branch crashes about 10 min in on llm-train-2 with 'an illegal memory access was encountered'. The user wants the node replaced.

<details><summary>Answer</summary>

Xid 31 (GPU memory page fault) on h100-03 GPU 5, but ECC is clean, there are no remaps, and dcgmi diag passes. This is almost always the application: an out-of-bounds access in a custom kernel or a bad index. Action: do NOT RMA. Send it back to the user with the Xid line and suggest compute-sanitizer / CUDA_LAUNCH_BLOCKING=1. If the same code crashes on different nodes, that confirms it.

</details>
