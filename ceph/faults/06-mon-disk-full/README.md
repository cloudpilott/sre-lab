# Monitor stopped: its disk filled with debug logs

Ticket: CephMonDownQuorumAtRisk fired at 06:45. One more monitor failure and the storage cluster stops serving I/O.

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-02.

<details><summary>Answer</summary>

mon.ceph-02 shut itself down: its journal says 'reached critical levels of available space on local monitor storage -- shutdown!'. `df -h` on ceph-02 shows / at 97%; `du` shows ceph-mon.ceph-02.log at ~350 GB because debug_mon was left at 20/20 (`ceph config dump | grep debug`). Action: `ceph config rm mon.ceph-02 debug_mon`, truncate the log, check free space, `ceph orch daemon restart mon.ceph-02`; quorum goes back to 3/3. Never delete the mon store (store.db).

</details>
