# One OSD nearfull: data imbalance

Ticket: CephOSDNearFull fired, but the storage team says the cluster is only ~58% used. Is the alert real, and do we need to buy disks?

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

osd.13 on ceph-04 is at ~87% while the average is ~58%: `ceph osd df` shows VAR ~1.5 and ~50% more PGs than its peers. The balancer is off (`ceph balancer status`: active false), so PGs were never evened out. The fullest OSD sets MAX AVAIL for every pool (`ceph df`), and at 95% (full_ratio) the whole cluster stops writes. Action: `ceph balancer mode upmap` + `ceph balancer on` (or `ceph osd reweight-by-utilization`) and watch %USE converge. No new disks needed.

</details>
