# NVLink CRC errors -> Xid 74

Ticket: All-reduce bandwidth inside one node looks low. Earlier today someone saw a few NCCL retries.

<details><summary>Answer</summary>

h100-03 GPU 5: NVLink CRC flit errors are climbing on link 7 and NVLink bandwidth is down. About 20 min after the fault starts it escalates to Xid 74 (fatal NVLink error) and the job crashes. Action: drain early. Reset the GPU and NVSwitch fabric (restart fabric manager or reboot). If errors return, the baseboard/NVSwitch tray needs service.

</details>
