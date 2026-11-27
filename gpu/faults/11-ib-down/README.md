# InfiniBand port down

Ticket: llm-train hangs at start-up, then NCCL times out: 'NET/IB : Got completion from peer ... with status=12'.

<details><summary>Answer</summary>

h100-03 mlx5_5 port 1 is Down / Polling. The physical link isn't up, so there is no peer (cable, transceiver or switch port). rdma/ib allocatable dropped to 7. Action: drain h100-03, check the cable/transceiver and the leaf switch port, and reseat or swap the optic.

</details>
