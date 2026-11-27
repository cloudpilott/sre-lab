# Full OSD blocks all writes (wrong CRUSH weight)

Ticket: llm-train checkpoint failed at 06:45: OSError: [Errno 28] No space left on device: '/ckpt/llm-train/step_41000/rank3.pt'. The storage dashboard says the cluster is only ~60% used.

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

osd.13 on ceph-04 hit 95% (full_ratio): OSD_FULL is HEALTH_ERR and Ceph blocks writes to every pool, so CephFS returns ENOSPC even though the cluster is ~60% used. `ceph osd tree` shows its CRUSH weight is 13.97 instead of 6.99 (set wrong when the disk was replaced), so CRUSH gave it twice its share of data (`ceph osd df`: VAR ~1.6, ~2x PGs). Action: `ceph osd crush reweight osd.13 6.98599`. If you need writes back right now, temporarily `ceph osd set-full-ratio 0.96`, and restore 0.95 once data has moved.

</details>
