# Correctable ECC storm (Xid 92)

Ticket: No crash yet, but the GPU-health dashboard shows one GPU's ECC counter climbing.

<details><summary>Answer</summary>

h100-03 GPU 5: single-bit (correctable) ECC errors are climbing fast and the correctable remap count is rising. After a while it raises Xid 92 (high SBE rate). Correctable errors aren't fatal, but a storm often comes before an uncorrectable one. Action: watch it, plan a drain at the next checkpoint, reset to apply remaps. RMA if the rate persists.

</details>
