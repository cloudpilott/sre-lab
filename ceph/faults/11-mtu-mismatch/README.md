# MTU mismatch on the cluster network

Ticket: CephOSDFlapping and CephOSDTimeoutsClusterNetwork since this morning's network change on ceph-04. Checkpoint writes stall for ~30 s every few minutes.

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-04.

<details><summary>Answer</summary>

ceph-04's cluster-network NIC ens1f1np1 has MTU 1500 while every other host uses 9000 (`ip link`). Small packets work, so a plain ping succeeds, but OSD heartbeats are padded to 2000 bytes (osd_heartbeat_min_size) and replication uses jumbo frames. Peers stop hearing ceph-04's OSDs on the back network ('heartbeat_check: no reply ... since back <old> front <recent>') and report them down; the OSDs say 'wrongly marked me down' and come back: flapping. `ping -M do -s 8972 <cluster ip>` fails. Action: set MTU 9000 on ens1f1np1 (netplan), verify jumbo pings both ways. A brief `ceph osd set nodown` stops the flapping while you fix it (unset it afterwards).

</details>
