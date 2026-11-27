# InfiniBand link trained at reduced width

Ticket: NCCL all_reduce_perf bus bandwidth is about half of what it was at acceptance. No errors anywhere.

<details><summary>Answer</summary>

h100-03 mlx5_5 is Active but at 200 Gb/s (2X width) instead of 400 Gb/s (4X NDR). Two lanes failed to train. Ring all-reduce runs at the speed of its slowest link, so the whole job halves. Action: reseat/replace the cable or optic. ibdiagnet / ibportstate show the width.

</details>
