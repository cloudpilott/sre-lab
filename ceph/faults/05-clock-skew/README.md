# Monitor clock skew

Ticket: The storage dashboard has shown HEALTH_WARN since 06:45. Nothing seems broken. Is it safe to ignore?

Start with `ceph -s` (ceph-01). Host-side files (dmesg, smartctl, journal, ...) were captured on ceph-02.

<details><summary>Answer</summary>

mon.ceph-02's clock is ~0.3 s off (MON_CLOCK_SKEW, max 0.05 s). On ceph-02, `chronyc tracking` says 'Not synchronised' and `chronyc sources` shows the NTP server unreachable (Reach 0): the firewall change blocks UDP 123. Not safe to ignore: skewed monitors can drop out of quorum and cephx tickets fail. Action: restore NTP (open UDP 123), `chronyc makestep`, then check `ceph time-sync-status`.

</details>
