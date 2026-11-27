# Scrub found an inconsistent PG (bad sector)

Ticket: CephPGsDamaged fired at 06:45: 'Possible data damage: 1 pg inconsistent'. Was data lost, and what should we do?

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

PG 3.e failed deep-scrub: `rados list-inconsistent-obj 3.e` shows a read_error on the shard on osd.13. The other two copies agree, so nothing is lost. dmesg on ceph-04 has 'critical medium error, dev /dev/nvme1n1' and smartctl shows Media and Data Integrity Errors: the disk has bad sectors. Action: `ceph pg repair 3.e` (rewrites the bad copy from the good ones), then watch: more media errors mean replace the disk. Always check which shard is bad before repairing.

</details>
