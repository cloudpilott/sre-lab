# Worn-out NVMe makes one OSD slow

Ticket: Since about 06:45 llm-train checkpoints take 6+ minutes instead of 40 s, and GPU utilization drops to 0 during every checkpoint. No GPU or network alerts.

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

osd.13 (/dev/nvme1n1 on ceph-04) is slow: SLOW_OPS and BLUESTORE_SLOW_OP_ALERT, `ceph osd perf` shows ~1 s commit/apply latency vs 1-3 ms elsewhere, and its journal logs 'slow operation observed'. smartctl shows Percentage Used 104% and the error log growing: the drive is worn out and throttling (dmesg: occasional I/O timeouts; `ceph device ls` shows the wear). Every write to a PG that includes this OSD waits for it, so one slow disk slows the whole checkpoint. Action: `ceph osd out 13` (or `ceph osd primary-affinity osd.13 0` as a stopgap), let data drain, then replace the drive (`ceph orch osd rm 13 --replace`).

</details>
