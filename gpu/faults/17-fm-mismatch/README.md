# Fabric Manager / driver version mismatch

Ticket: h100-03 was re-imaged after a motherboard swap. Its pods now fail at start-up with 'CUDA error: system not yet initialized' (802).

<details><summary>Answer</summary>

h100-03 has driver 580.105.08 from a new image, but nvidia-fabricmanager is still 580.95.05. Fabric Manager refuses to start when the versions differ, so the NVSwitch fabric never comes up: systemctl status nvidia-fabricmanager is failed, nvidia-smi -q shows Fabric State 'In Progress', CUDA returns error 802 and dcgmi diag can't run. Action: put the node back on the locked set (driver 580.95.05 with the matching Fabric Manager), restart nvidia-fabricmanager, confirm Fabric State Completed / Success, run dcgmi diag -r 3 and the ATP. Then fix the image pipeline so driver and FM are pinned together.

</details>
