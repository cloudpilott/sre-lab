# Fabric congestion (not a node fault)

Ticket: Job throughput dips at certain times of day. Someone wants to RMA h100-03 because its NICs 'look busy'.

<details><summary>Answer</summary>

Every HCA on h100-03 shows a fast-rising PortXmitWait with zero errors and Active links at full rate. That is congestion: the port has data but no credits, often from another tenant's traffic or a hotspot on the uplinks. Action: do NOT RMA. Look at fabric-wide counters, routing (adaptive routing / SHARP), job placement and other tenants' jobs.

</details>
