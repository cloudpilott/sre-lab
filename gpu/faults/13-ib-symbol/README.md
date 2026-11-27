# InfiniBand symbol errors (bad cable/optic)

Ticket: Multi-node all-reduce is ~10% slower than last week, and only some job placements are affected.

<details><summary>Answer</summary>

h100-03 mlx5_5: SymbolErrorCounter and PortRcvErrors are climbing steadily while the link stays Active. That is physical-layer bit errors (dirty connector, bent fiber, failing transceiver). Action: clean or replace the cable/optic at the next maintenance. Watch for escalation to link flaps.

</details>
