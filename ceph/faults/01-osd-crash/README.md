# OSD daemon crashed (software assert)

Ticket: CephOSDDown fired on the storage cluster around 06:45. llm-train checkpoints still succeed, but the cluster is HEALTH_WARN. Find out what happened and whether data is at risk.

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

osd.13 on ceph-04 crashed on a software assert: `ceph crash ls` / `ceph crash info` and its journal show 'FAILED ceph_assert(head_obc)' then 'Caught signal (Aborted)'. systemd gave up after 5 restarts, so `ceph orch ps` shows it in error. The disk is healthy: dmesg is clean and smartctl passes. Because it stayed down for more than 10 min (mon_osd_down_out_interval) Ceph marked it out and backfilled its PGs elsewhere. Data was never at risk (2 of 3 copies stayed up; min_size 2). Action: `ceph orch daemon restart osd.13`, `ceph crash archive <id>`. If it hits the same assert again, attach `ceph crash info` to a tracker.ceph.com search and plan the point-release upgrade. Don't replace the disk.

</details>
