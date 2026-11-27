# Storage host down

Ticket: CephOSDHostDown paged the storage on-call at 06:45: one of the storage servers stopped answering.

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

ceph-04 is down: ping and ssh fail, `ceph orch host ls` shows it Offline, its 4 OSDs are down (plus its mon / MDS if it runs one). After 10 min Ceph marked them out and backfills about 1/6 of the data onto the other 5 hosts. Data stays available because the CRUSH failure domain is host (size 3, min_size 2). Action: check the BMC (power, SEL, console) and bring it back; its OSDs rejoin and catch up. For planned maintenance, `ceph osd set noout` first so data doesn't move, and unset it afterwards.

</details>
