# GPU PCIe link trained at x8 instead of x16

Ticket: On one node, host-to-device copies and checkpoint loads run at about half speed. No errors anywhere, no Xids.

<details><summary>Answer</summary>

h100-03 GPU 5: the PCIe link trained at Gen5 x8 instead of x16. nvidia-smi -q -d PCI shows Link Width Current 8x, lspci -vv shows 'Width x8 (downgraded)', and nvbandwidth host_to_device is about half on that GPU. GDS throughput to it drops too. Nothing logs an error, which is why ATP checks link width explicitly. Action: drain, have remote hands reseat the GPU / baseboard cable / riser, retest. If it persists, service the baseboard.

</details>
