# Runbook: PodinfoAvailabilityBurn

**What it means:** podinfo is returning 5xx fast enough to exhaust the 30-day error budget early.
- `critical` (page): fast burn - budget gone in hours. Act now.
- `warning` (ticket): slow burn - budget gone in days. Fix this week.

## Triage (5 minutes)
1. Grafana -> *podinfo - Golden Signals*: is it all pods or one? Started when?
2. Recent change? `helm history podinfo -n demo` / `kubectl rollout history deploy/podinfo -n demo`
3. Pod health: `kubectl get pods -n demo -o wide` (restarts? one node?)
4. Logs: Grafana Explore -> Loki -> `{namespace="demo"} |= "error"`
5. Active chaos? `kubectl get podchaos,networkchaos,stresschaos,schedule -n demo`

## Mitigate
- Bad release -> `helm rollback podinfo -n demo`
- Fault flag on -> `helm upgrade podinfo podinfo/podinfo -n demo --reuse-values --set faults.error=false`
- Bad node -> `kubectl cordon <node> && kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`

## After
Write a postmortem (template in EXERCISES.md). Link this alert, timeline, budget consumed.
