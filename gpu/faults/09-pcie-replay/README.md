# PCIe replays / AER corrected errors

Ticket: Data-loader-heavy jobs on one node are slower than on the others. Host-to-device copies look slow.

<details><summary>Answer</summary>

h100-03 GPU 5: the PCIe replay counter is climbing and dmesg shows AER 'Corrected ... Physical Layer RxErr' from the GPU's root port. That points to a marginal PCIe link (riser/retimer/seating). Action: drain, reseat, update the retimer firmware. If it persists, service the baseboard.

</details>
