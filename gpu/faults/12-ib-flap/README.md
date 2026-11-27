# InfiniBand link flapping

Ticket: Jobs fail at random times, every hour or two, with NCCL IB timeouts. Rerunning sometimes works.

<details><summary>Answer</summary>

h100-03 mlx5_5: LinkDownedCounter goes up every ~3 min and LinkErrorRecovery climbs, so the link keeps retraining. Usually a marginal cable/optic or a switch port. Action: drain, swap the optic/cable, and check the switch side counters (ibdiagnet). A flapping link causes intermittent failures that are hard to reproduce.

</details>
