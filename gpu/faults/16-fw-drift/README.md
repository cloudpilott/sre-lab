# HCA firmware below the locked version (RMA replacement card)

Ticket: Remote hands replaced a ConnectX-7 in h100-03 yesterday. Before it goes back to the customer, confirm the node meets the build standard.

<details><summary>Answer</summary>

h100-03 mlx5_5 runs firmware 28.42.1000, but the locked version is 28.45.1020: the replacement card came from the RMA pool with older firmware. Everything else matches. Evidence: ibstat 'Firmware version', flint -d mlx5_5 q, the mlx5_core firmware line in dmesg, ./lab.sh gpu compliance. Action: keep the node out of service, flash the locked firmware (mlxfwreset / the vendor bundle), reset the card, rerun the compliance and IB checks, update the as-built record. Add 'flash to locked FW' to the part-swap runbook.

</details>
