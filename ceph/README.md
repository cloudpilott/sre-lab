# Simulated Ceph storage cluster

Six fake storage hosts (`ceph-01`…`ceph-06`), each with 4× 7.68 TB NVMe OSDs: **24 OSDs, ~168 TiB raw**, Ceph Squid
(19.2) deployed by cephadm. 3 monitors (ceph-01..03), 2 managers (ceph-01 active, ceph-02 standby), and a CephFS
filesystem `ckpt` with MDS on ceph-04 (active) and ceph-05 (standby). In the story, `ckpt` is where `llm-train` on the
8 H100 nodes writes its checkpoints, so storage faults show up as training tickets.

Nothing runs Ceph for real (a real cluster needs 3–4 GB more RAM than the Mac has to spare). Three cheap layers make it
look real, the same way the GPU fleet does:

| Layer | What | Cost |
|---|---|---|
| **KWOK** fake nodes | Real Node objects `ceph-01..06` (label `node-role.kubernetes.io/storage`, zone rack-c / rack-d) | shared with the GPU layer |
| **cephsim exporter** | The ceph-mgr `prometheus` module's metric names (`ceph_health_detail`, `ceph_osd_up`, `ceph_pg_*`, `ceph_pool_*`, …) on :9283, `job="ceph"` | ~30 MB |
| **cephsim shell** | `ceph` (status, health, osd tree/df/perf/dump, pg, df, crash, orch, device, fs, config, balancer, time-sync), `rados list-inconsistent-obj`, `journalctl`, `dmesg`, `smartctl`, `nvme list`, `lsblk`, `df`, `du`, `chronyc`, `ip`, `ping` | runs on the Mac |

Everything reads one fault state (`ceph/state.json`), so `ceph -s`, metrics, alerts and the node tools agree.
Placement is a deterministic stand-in for CRUSH (833 PGs, size 3 / min_size 2, failure domain host), so the same PG
ids and acting sets show up everywhere.

```bash
./lab.sh start && ./lab.sh ceph up     # once: storage nodes, exporter, alerts, dashboard (KWOK if not there yet)
./lab.sh ceph drill 1                  # random hidden fault + ticket (levels 1-3)
./lab.sh ceph ssh                      # admin shell on ceph-01; 'ceph -s' first
./lab.sh ceph reveal && ./lab.sh ceph clear
```

## Faults
`./lab.sh ceph faults` lists them. `./lab.sh ceph fault <type> [target]` injects one (target = OSD id, host or mon host).

| Type | Looks like | Lesson |
|---|---|---|
| `osd-crash` | OSD down, RECENT_CRASH, assert in `ceph crash info`, disk healthy | Software crash: restart, don't swap disks |
| `disk-fail` | OSD down, NVMe gone from `lsblk`, `CSTS=0xffffffff` in dmesg | Hardware: `orch osd rm --replace` |
| `nearfull` | One OSD at 87%, cluster at 58%, balancer off | The fullest OSD sets MAX AVAIL; balance, don't buy |
| `host-down` | 4 OSDs down, host Offline, no ping/ssh, MDS standby lost | Failure domain host; down → out after 10 min → backfill |
| `clock-skew` | MON_CLOCK_SKEW, chrony not synchronised | NTP matters to quorum and cephx |
| `mon-disk-full` | MON_DOWN, quorum 2/3, mon log at 350 GB | Debug logging left on; mon self-protects at 5% free |
| `flags-left` | All OSDs up but PGs degraded forever | `noout,norebalance,norecover` left after maintenance |
| `osd-full` | HEALTH_ERR, ENOSPC on CephFS at 60% used | One full OSD blocks every write; wrong CRUSH weight |
| `slow-ops` | SLOW_OPS, 1 s latency on one OSD, NVMe 104% worn | One slow disk sets the speed of every write |
| `pg-inconsistent` | PG_DAMAGED, read_error on one shard | Find the bad shard, then `pg repair`; watch the disk |
| `mtu-mismatch` | 4 OSDs flapping, slow back-network heartbeats | MTU 1500 vs 9000; small pings lie |

## How it behaves
- The cluster is "live": a down OSD is marked **out after 10 min** (`mon_osd_down_out_interval`) unless `noout` is set,
  then its PGs go `backfill_wait` → `backfilling` → `active+clean` over ~25 min (longer for a whole host). `ceph -s` a few
  times during a drill and watch the degraded count fall.
- `OSD_DOWN` only counts OSDs that are down **and in**, as in real Ceph, so the alert clears once the OSD is out.
- Commands that change the cluster (`ceph osd set/unset/out`, `ceph pg repair`, `ceph orch daemon restart`, …) are
  read-only here: write down the fix you'd run, then `reveal`. `clear` stands in for doing the repair.
- `kubectl` doesn't show `host-down`: KWOK keeps heartbeating the fake node. Use `ceph orch host ls` and `ping`.
- Output formats follow Squid closely but aren't byte-exact.

## Offline study
`./lab.sh ceph fixtures` writes `ceph/faults/<NN-type>/`: every command's output for each fault (OSD 13 / ceph-04 /
mon ceph-02, 25 min after onset) plus a `README.md` with the ticket and a hidden answer. Diff against `00-healthy/`.
