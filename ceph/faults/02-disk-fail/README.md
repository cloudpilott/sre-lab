# NVMe drive died under an OSD

Ticket: CephOSDDown and CephDaemonCrash fired on the storage cluster around 06:45. The storage team asks: software or hardware, and what do we do?

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

/dev/nvme1n1 on ceph-04 dropped off the bus: dmesg shows I/O timeouts, 'controller is down; will reset: CSTS=0xffffffff' and 'Removing after probe failure'. smartctl can't open it and lsblk no longer lists it. osd.13 aborted with 'Unexpected IO error. This may suggest a hardware issue' and can't start again. Ceph marked it out after 10 min and is backfilling. Action: hardware. Let recovery finish (`ceph -s`), then `ceph orch osd rm 13 --replace` (keeps the OSD id), swap the drive; cephadm recreates the OSD on the new disk. Restarting the daemon won't help.

</details>
