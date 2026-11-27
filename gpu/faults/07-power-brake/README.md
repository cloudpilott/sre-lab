# HW power brake (PSU/BMC)

Ticket: Throughput dropped ~40%. Only one node's power draw looks odd on the dashboard.

<details><summary>Answer</summary>

All 8 GPUs on h100-03 show HW Power Brake Slowdown active, ~250W draw and ~700 MHz clocks. That is an external power-brake signal (failed/derated PSU or BMC power capping), not a GPU fault. Action: check the PSUs and SEL in the BMC, and drain the node until the PSU is replaced.

</details>
