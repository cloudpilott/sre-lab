# Maintenance flags left set

Ticket: After last night's maintenance on ceph-04 the storage cluster has been HEALTH_WARN with degraded PGs for hours. All 24 OSDs are up. Recovery isn't moving.

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

The maintenance flags noout, norebalance and norecover were never unset (OSDMAP_FLAGS; `ceph osd dump | grep flags`). With norecover set, the writes ceph-04's OSDs missed while they were down are never recovered, so PGs stay active+recovery_wait+degraded and the degraded count doesn't move. Action: `ceph osd unset norecover`, `ceph osd unset norebalance`, `ceph osd unset noout`, watch `ceph -s` return to HEALTH_OK. Put 'unset flags' on the maintenance checklist.

</details>
