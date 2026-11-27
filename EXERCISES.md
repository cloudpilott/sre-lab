# SRE Exercises

Before each drill: `./lab.sh snapshot pre-<name>`. Keep `./lab.sh load` running in another terminal
so there is traffic. For every drill, write down: **detect → triage → mitigate → postmortem**.

## Level 1 — Observability basics
1. **Find the golden signals.** Open *podinfo - Golden Signals*. Run `./lab.sh load`. Explain each panel's shape.
2. **PromQL.** Write queries for: the pod with the most restarts; node memory % used;
   the top 5 containers by CPU throttling (`container_cpu_cfs_throttled_periods_total`).
3. **LogQL.** In Explore → Loki: `{namespace="demo"} | json | level="error"`. Count errors per pod per minute.
4. **SLOs.** Open the *SLOs* folder (Sloth dashboards). How much error budget is left?
   What is the burn rate during a load test?

## Level 2 — Alerting
5. **Wire up notifications.** Add an Alertmanager receiver (Slack incoming webhook, or a local
   `webhook.site`-style receiver) to `values/kube-prometheus-stack.yaml` under `alertmanager.config`.
   Route `severity=critical` → page, `warning` → ticket channel. Re-run `./lab.sh platform`.
6. **Trigger the page.** `helm upgrade podinfo podinfo/podinfo -n demo --reuse-values --set faults.error=true`
   plus load. How long until `PodinfoAvailabilityBurn` fires? Why that long? (Read the generated
   rule: `kubectl get prometheusrule -n demo -o yaml`.)
7. **Alert hygiene.** Find any alerts that fire on a healthy lab (Alertmanager UI). Silence or fix each one, and justify the choice.

## Level 3 — Failure drills
8. **Pod loss:** `./lab.sh chaos pod-kill`. Did users notice? Add a PodDisruptionBudget and readiness probe tuning.
9. **Latency:** `./lab.sh chaos network-delay`. Which SLO burns? Does the k6 threshold fail?
10. **Saturation:** `./lab.sh chaos cpu-stress` while loading. Watch HPA (`kubectl get hpa -n demo -w`).
11. **Node loss:** `multipass stop w2`. How long until pods reschedule? (Hint: node-monitor-grace-period,
    tolerationSeconds for `node.kubernetes.io/unreachable`.) Bring it back: `multipass start w2`.
12. **Disk pressure:** `multipass exec w1 -- sudo fallocate -l 14G /fill`. Observe evictions and the
    `NodeFilesystemSpaceFillingUp` alert. Clean up: `multipass exec w1 -- sudo rm /fill`.
13. **DNS:** `./lab.sh chaos dns-failure`. What breaks, and what do the logs say?
14. **Control plane:** `multipass stop cp1`. What still works? (Running pods: yes. New scheduling: no.)

## Level 4 — Reliability engineering
15. **Capacity:** raise k6 VUs until the latency SLO breaks. Document the max sustainable req/s per pod.
    Right-size requests/limits from real usage.
16. **Bad deploy:** `helm upgrade podinfo podinfo/podinfo -n demo --reuse-values --set image.tag=does-not-exist`.
    Detect it, roll back, and measure time to recover.
17. **Game day:** have a friend (or a random script) run a hidden chaos experiment. You get
    only alerts and dashboards. Time to detect / time to mitigate.
18. **Toil:** automate the fix for one runbook (e.g. a CronJob or a small controller).

## Level 5 — GPU fleet (simulated 8x H100, `./lab.sh gpu up`)
Runbook: `runbooks/gpu-ib-faults.md`. Dashboard: *GPU Fleet - H100 + InfiniBand*. Fake node shell: `./lab.sh gpu ssh <node>`.
19. **Baseline.** On a healthy fleet, record from Grafana and the shell: GPU temp/clock/power range, `all_reduce_perf` busbw
    at 8 GB (`./lab.sh gpu nccl`), and IB PortXmitWait rate. You will compare every drill against these.
20. **Scheduling meets hardware.** `./lab.sh gpu fault xid79 h100-04 2`. Explain why `llm-train-*` is Pending
    (`kubectl describe pod`), and which alert fired first. Cordon, drain, then run `dcgmi diag -r 3` on the node.
21. **ECC triage.** Inject `xid94`, `xid95` and `row-remap-fail` on three different nodes. For each, decide: reset, reboot or RMA.
    Which `nvidia-smi -q -d ROW_REMAPPER` field decides it?
22. **Not the hardware.** `./lab.sh gpu fault xid31 h100-06`. Gather the evidence that you'd send back to the user instead of an RMA.
23. **Stragglers.** Inject `thermal` and, separately, `power-brake`. Both make the job slow with no crash. Find the node from metrics alone,
    then tell the two apart in `nvidia-smi -q -d PERFORMANCE`.
24. **Fabric.** Inject `ib-symbol`, `ib-degraded` and `ib-congestion` (one at a time). Which shows up in `ibdiagnet`, which only in
    `perfquery`, which only in NCCL busbw? Which should never become an RMA?
25. **Catch it early.** `./lab.sh gpu fault nvlink h100-02 5`. You have 20 minutes before it turns into Xid 74 and kills the job.
    Detect, cordon and drain in time.
26. **Blind drills.** `./lab.sh gpu drill 1`, then `2`, then `3`. Work only from the ticket, alerts, kubectl and the node shell.
    Record time to detect, time to the right action, and anything you'd have wrongly RMA'd.
27. **Alert design.** `GPUXidHardware` lists Xid values one by one. Rewrite it with a recording rule and a label so the Xid class
    (app / hardware / degrading) drives routing: app → ticket to the user's team, hardware → page the infra on-call.

## Level 6 — Ceph storage (simulated 24-OSD cluster, `./lab.sh ceph up`)
Runbook: `runbooks/ceph-faults.md`. Dashboard: *Ceph Cluster - storage (sim)*. Admin shell: `./lab.sh ceph ssh` (ceph-01).
28. **Baseline.** On a healthy cluster, record `ceph -s`, `ceph osd df` (min/max %USE, STDDEV), `ceph osd perf` and pool
    MAX AVAIL. Read `ceph/faults/00-healthy/` until the normal output is boring.
29. **Watch recovery.** `./lab.sh ceph fault disk-fail 13`. Run `ceph -s` every few minutes for 30 min. Note when the OSD
    goes from down/in to out, when backfill starts, and when HEALTH_WARN clears. Why does `OSD_DOWN` disappear before recovery ends?
30. **Software or hardware?** Inject `osd-crash` and `disk-fail` on two different OSDs. Collect the evidence that tells
    them apart (crash info, journal, dmesg, smartctl, lsblk) and write the action for each.
31. **Capacity that isn't.** Inject `nearfull`, clear, then `osd-full`. In both, the cluster is ~60% used. Explain MAX AVAIL
    in `ceph df` and find the different root causes (`ceph balancer status` vs `ceph osd tree`).
32. **Planned maintenance.** Write the checklist for rebooting ceph-03 (a mon host) without moving data. Then inject
    `flags-left ceph-03` and explain what your checklist's last step would have prevented.
33. **Quorum.** Inject `mon-disk-full ceph-02`. What happens if ceph-01 now dies? Find the root cause from the host alone.
34. **The slow checkpoint.** `./lab.sh ceph fault slow-ops`. Find the OSD from Grafana alone, then prove it's the disk.
    Why does one slow OSD out of 24 slow down the whole llm-train checkpoint?
35. **Network.** `./lab.sh ceph fault mtu-mismatch ceph-05`. Show why a plain `ping` passes and the cluster still flaps.
36. **Blind drills.** `./lab.sh ceph drill 1`, `2`, `3`. Work only from the ticket, alerts and the shells. Record time to the
    right action, and anything you'd have done that would have made it worse.
37. **Combined.** Run `./lab.sh gpu drill 2` and `./lab.sh ceph drill 2` together. Two tickets arrive at once: triage which
    one blocks training, and in what order you work them.

## Postmortem template
```
Title / date / severity
Impact:            who/what, duration, error budget consumed (%)
Timeline (UTC):    detection, escalation, mitigation, resolution
Root cause:        technical + contributing factors
What went well / what went poorly / where we got lucky
Action items:      owner, due date, prevent | detect | mitigate
```
