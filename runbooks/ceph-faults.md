# Runbook: Ceph storage faults

Simulated in this lab by `./lab.sh ceph ...`. The commands below are the real ones. In the lab, run them through
`./lab.sh ceph ssh [host]` (ceph-01 is the admin host).

## Triage
1. **What does Ceph say?** `ceph -s`, then `ceph health detail`. Every alert maps to a health check code
   (`OSD_DOWN`, `PG_DEGRADED`, `MON_DOWN`, …). Grafana → *Ceph Cluster - storage (sim)*.
2. **Is data at risk or I/O blocked?** HEALTH_ERR, `inactive`/`peered`/`incomplete` PGs, `OSD_FULL`, `PG_DAMAGED`
   = urgent. `undersized+degraded` with 2 of 3 copies up = not lost, but don't lose another host.
3. **Which daemon, which host, which disk?** `ceph osd tree`, `ceph osd metadata <id> | grep -e hostname -e devices`,
   `ceph orch ps`, `ceph device ls`.
4. **Software or hardware?** `ceph crash ls` / `crash info`, `journalctl -u ceph-osd@<id>`, then on the host
   `dmesg -T`, `smartctl -a /dev/<dev>`, `lsblk`.
5. **Don't make it worse.** Never `ceph osd purge`/`destroy` or delete a mon store while PGs are degraded. Don't restart
   OSDs in a loop. Set `noout` before planned work, and **unset it afterwards**.

```bash
ceph -s; ceph health detail
ceph osd tree; ceph osd df tree; ceph osd perf
ceph pg dump_stuck; ceph pg ls degraded
ceph df                        # MAX AVAIL is set by the fullest OSD
ceph osd dump | grep -e flags -e ratio
ceph crash ls; ceph orch ps --daemon-type osd; ceph orch host ls
```

## OSD down
`ceph osd tree` shows which. Then:
- `ceph crash ls` + `journalctl`: an assert and a healthy disk (clean dmesg, smartctl PASSED, in `lsblk`) = **software**.
  `ceph orch daemon restart osd.N`, `ceph crash archive <id>`. Repeats → tracker.ceph.com, upgrade.
- I/O errors, `controller is down`, device missing from `lsblk`, smartctl can't open it = **hardware**.
  Let recovery finish, `ceph orch osd rm N --replace`, swap the drive.
- After 10 min (`mon_osd_down_out_interval`) Ceph marks it out and backfills. That's expected, not a second problem.

## Host down
`ceph orch host ls` (Offline), `ping`. 4 OSDs down plus any mon/MDS/mgr there. Data stays readable while the CRUSH failure
domain is host and min_size 2. Check the BMC: power, SEL, console. If it comes back soon, its OSDs rejoin and catch up.
Planned work: `ceph osd set noout` → work → `ceph osd unset noout`.

## Degraded
`ceph pg dump_stuck`. States: `undersized+degraded` (a copy is missing) · `remapped+backfill_wait/backfilling` (moving data
to a new OSD) · `recovery_wait` (waiting to recover). If the degraded count doesn't fall, check flags (next section) and
`osd_max_backfills`. Inactive PGs (`peered`, `incomplete`) block I/O: page.

## Flags
`ceph osd dump | grep flags`. `noout` (don't mark out), `norebalance`, `norecover`, `nobackfill` (don't move data),
`nodown` (ignore down reports). All are for maintenance and must be unset afterwards: `ceph osd unset <flag>`.

## Full
Ratios: nearfull 0.85 (warning) · backfillfull 0.90 (no backfill onto it) · full 0.95 (**all writes stop**).
`ceph osd df`: compare `%USE`, `VAR`, `PGS`. One hot OSD with a normal cluster average = imbalance, not capacity:
- balancer off → `ceph balancer mode upmap && ceph balancer on`, or `ceph osd reweight-by-utilization`
- wrong CRUSH weight in `ceph osd tree` (it should be the size in TiB) → `ceph osd crush reweight osd.N <TiB>`
- emergency: `ceph osd set-full-ratio 0.96` to get writes back, then restore 0.95.

## Slow ops
`ceph health detail` names the OSD; `ceph osd perf` shows its latency against the others. On its host: journal
`slow operation observed`, dmesg I/O timeouts, smartctl `Percentage Used` (wear) and the error log. One slow OSD slows
every PG it's in. Stopgap: `ceph osd primary-affinity osd.N 0`. Fix: `ceph osd out N`, replace the drive.

## Inconsistent
`ceph health detail` → PG id. `rados list-inconsistent-obj <pg> --format=json-pretty` → which shard has which error.
`read_error` on one shard with the others agreeing = bad sector on that disk (check dmesg `medium error`, smartctl
`Media and Data Integrity Errors`). `ceph pg repair <pg>`. Repeated errors on the same OSD → replace the disk.

## Mon down
`ceph mon stat`, `ceph health detail`. 3 mons tolerate 1 failure; with 2 left, one more failure stops the cluster. On the host:
`journalctl -u ceph-mon` (`reached critical levels of available space` = disk), `df -h`, `du -sh /var/log/ceph/*/*`,
`ceph config dump | grep debug`. Free space (never touch `store.db`), remove debug overrides, `ceph orch daemon restart mon.<host>`.

## Clock skew
`ceph time-sync-status`. On the mon host: `chronyc tracking` (Leap status), `chronyc sources` (Reach 0 = can't reach the
NTP server: firewall, UDP 123). Fix NTP, then `chronyc makestep`.

## Flapping
OSDs going up/down, `OSD_SLOW_PING_TIME_BACK`, journal `wrongly marked me down`, `heartbeat_check: no reply ... since back
<old> front <recent>` = the **cluster (back) network** is broken while the public network works. Check `ip link` MTU on
every host and test with `ping -M do -s 8972 <cluster ip>` both ways (a normal ping passes). Stopgap: `ceph osd set nodown`
(unset it afterwards).

## After
`ceph -s` back to HEALTH_OK (or only the expected warnings), `ceph crash archive-all` once handled, unset every flag you set,
write the postmortem (EXERCISES.md template).
