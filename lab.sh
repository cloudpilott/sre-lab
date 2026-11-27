#!/usr/bin/env bash
# SRE lab on macOS (Apple Silicon): Multipass VMs -> k3s -> observability + chaos.
# Usage: ./lab.sh <command>   (run ./lab.sh help)
set -euo pipefail
cd "$(dirname "$0")"

# ---- Sizing (tuned for a 16 GB M2; override via env) ------------------------
NODES=(cp1 w1 w2)
IMAGE="${IMAGE:-24.04}"
CPUS="${CPUS:-2}"
CP_MEM="${CP_MEM:-4G}"            # 3G was too tight: k3s (sqlite) crashed under load with the GPU layer
W_MEM="${W_MEM:-3584M}"
DISK="${DISK:-20G}"
K3S_VERSION="${K3S_VERSION:-}"          # empty = k3s stable channel

KCFG="$PWD/kubeconfig"
PW_FILE="$PWD/.grafana-password"
export KUBECONFIG="$KCFG"

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "missing '$1' - run: ./lab.sh prereqs"; }
ip_of(){ multipass info "$1" | awk '/IPv4/{print $2; exit}'; }
domain(){ echo "$(ip_of cp1).nip.io"; }
render(){ sed "s/__DOMAIN__/$(domain)/g" "$1"; }

# Retry until the VM answers over ssh and cloud-init has finished (up to ~10 min).
wait_vm() {
  local n="$1" i
  for i in $(seq 1 60); do
    # Poll (no --wait): `cloud-init status --wait` over multipass exec can hang indefinitely.
    if multipass exec "$n" -- cloud-init status </dev/null 2>/dev/null | grep -q 'status: done'; then
      echo "$n ready"; return 0
    fi
    sleep 10
  done
  die "$n did not become ready; try: multipass restart $n"
}

prereqs() {
  log "Installing tools with Homebrew"
  brew install --cask multipass
  brew install kubectl helm k9s k6
}

vms() {
  need multipass
  for n in "${NODES[@]}"; do
    if multipass info "$n" >/dev/null 2>&1; then echo "$n already exists"; continue; fi
    mem="$W_MEM"; [[ $n == cp1 ]] && mem="$CP_MEM"
    log "Launching $n ($CPUS vCPU, $mem RAM, $DISK disk)"
    # Long timeout: first boots on a laptop can exceed multipass's 5-minute default.
    if ! multipass launch "$IMAGE" --name "$n" --cpus "$CPUS" --memory "$mem" --disk "$DISK" --timeout 900; then
      multipass info "$n" >/dev/null 2>&1 || die "launch of $n failed and no VM was created"
      echo "launch reported a timeout; waiting for $n to finish booting..."
    fi
    wait_vm "$n"
  done
  multipass list
}

cluster() {
  need multipass; need kubectl
  local cp_ip ver="" token
  cp_ip="$(ip_of cp1)"
  [[ -n $K3S_VERSION ]] && ver="INSTALL_K3S_VERSION=$K3S_VERSION"

  log "Installing k3s server on cp1 ($cp_ip)"
  multipass exec cp1 -- bash -c "curl -sfL https://get.k3s.io | $ver sh -s - server \
      --write-kubeconfig-mode 644 --tls-san $cp_ip"

  token="$(multipass exec cp1 -- sudo cat /var/lib/rancher/k3s/server/node-token)"
  for n in "${NODES[@]:1}"; do
    log "Joining $n"
    multipass exec "$n" -- bash -c "curl -sfL https://get.k3s.io | $ver \
      K3S_URL=https://$cp_ip:6443 K3S_TOKEN=$token sh -"
  done

  log "Writing kubeconfig to $KCFG"
  multipass exec cp1 -- sudo cat /etc/rancher/k3s/k3s.yaml \
    | sed "s/127.0.0.1/$cp_ip/; s/: default$/: sre-lab/" > "$KCFG"
  chmod 600 "$KCFG"

  kubectl wait --for=condition=Ready nodes --all --timeout=300s
  for n in "${NODES[@]:1}"; do kubectl label node "$n" node-role.kubernetes.io/worker=worker --overwrite; done
  kubectl get nodes -o wide
}

repos() {
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
  helm repo add grafana     https://grafana.github.io/helm-charts      >/dev/null
  helm repo add chaos-mesh  https://charts.chaos-mesh.org              >/dev/null
  helm repo add sloth       https://slok.github.io/sloth               >/dev/null
  helm repo add podinfo     https://stefanprodan.github.io/podinfo     >/dev/null
  helm repo update >/dev/null
}

install_mon() {
  log "kube-prometheus-stack (Prometheus, Alertmanager, Grafana)"
  render values/kube-prometheus-stack.yaml | helm upgrade --install mon \
    prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
    -f - --set grafana.adminPassword="$(cat "$PW_FILE")" --wait --timeout 15m
}

platform() {
  need helm; need kubectl; repos
  [[ -f $PW_FILE ]] || { openssl rand -base64 18 > "$PW_FILE"; chmod 600 "$PW_FILE"; }

  install_mon

  log "Loki (logs)"
  helm upgrade --install loki grafana/loki -n monitoring \
    -f values/loki.yaml --wait --timeout 10m

  log "Alloy (log shipper -> Loki)"
  helm upgrade --install alloy grafana/alloy -n monitoring \
    -f values/alloy.yaml --wait --timeout 5m

  log "Sloth (SLO -> Prometheus rules generator)"
  helm upgrade --install sloth sloth/sloth -n monitoring --wait --timeout 5m

  log "Chaos Mesh"
  render values/chaos-mesh.yaml | helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
    -n chaos-mesh --create-namespace -f - --wait --timeout 10m

  status
}

apps() {
  need helm; repos
  log "podinfo demo app"
  render values/podinfo.yaml | helm upgrade --install podinfo podinfo/podinfo \
    -n demo --create-namespace -f - --wait
  kubectl apply -f slo/podinfo-slo.yaml
  kubectl apply -f dashboards/
  status
}

status() {
  local d; d="$(domain)"
  kubectl get nodes
  cat <<EOF

  Grafana       http://grafana.$d        (user: admin, password in .grafana-password)
  Prometheus    http://prometheus.$d
  Alertmanager  http://alertmanager.$d
  Chaos Mesh    http://chaos.$d
  podinfo       http://podinfo.$d

  export KUBECONFIG=$KCFG
EOF
}

load()  { need k6; k6 run -e BASE_URL="http://podinfo.$(domain)" load/podinfo.js; }

chaos() {
  local exp="${1:-}"
  [[ -n $exp ]] || { echo "experiments:"; ls chaos/ | sed 's/\.yaml$//;s/^/  /'; exit 0; }
  kubectl apply -f "chaos/$exp.yaml"
  echo "Running. Stop early with: kubectl delete -f chaos/$exp.yaml"
}

stop()  { multipass stop "${NODES[@]}"; }
start() { multipass start "${NODES[@]}"; kubectl wait --for=condition=Ready nodes --all --timeout=300s; }

snapshot() {
  local name="${1:-snap-$(date +%Y%m%d-%H%M)}"
  log "Snapshotting as '$name' (VMs must stop briefly)"
  multipass stop "${NODES[@]}"
  for n in "${NODES[@]}"; do multipass snapshot "$n" --name "$name"; done
  start
}

restore() {
  local name="${1:?usage: ./lab.sh restore <snapshot-name>  (see: multipass list --snapshots)}"
  multipass stop "${NODES[@]}"
  for n in "${NODES[@]}"; do multipass restore "$n.$name" --destructive; done
  start
}

down() {
  read -r -p "Delete VMs ${NODES[*]} permanently? [y/N] " a
  [[ $a == y || $a == Y ]] || exit 0
  multipass delete --purge "${NODES[@]}"
  rm -f "$KCFG"
}

all() { vms; cluster; platform; apps; snapshot baseline; }

# ---- OpenStack all-in-one (Kolla-Ansible) in its own VM ----------------------
# Needs ~10 GB RAM: stop the k3s lab first (./lab.sh stop).
OS_VM="${OS_VM:-os-aio}"
OS_CPUS="${OS_CPUS:-4}"
OS_MEM="${OS_MEM:-10G}"
OS_DISK="${OS_DISK:-60G}"

os_push() {
  multipass transfer openstack/aio.sh openstack/globals.yml.tpl "$OS_VM:/home/ubuntu/"
  multipass exec "$OS_VM" -- chmod +x /home/ubuntu/aio.sh
}
os_run() { multipass exec "$OS_VM" -- /home/ubuntu/aio.sh "$@"; }

openstack() {
  local cmd="${1:-help}"; shift || true
  case $cmd in
    up)
      need multipass
      if multipass list --format csv | awk -F, 'NR>1 && $2=="Running"' | grep -qE '^(cp1|w1|w2),'; then
        die "k3s VMs are running; free the RAM first with: ./lab.sh stop"
      fi
      if ! multipass info "$OS_VM" >/dev/null 2>&1; then
        log "Launching $OS_VM ($OS_CPUS vCPU, $OS_MEM RAM, $OS_DISK disk)"
        multipass launch "$IMAGE" --name "$OS_VM" --cpus "$OS_CPUS" --memory "$OS_MEM" --disk "$OS_DISK" --timeout 900 \
          || multipass info "$OS_VM" >/dev/null 2>&1 || die "launch of $OS_VM failed"
      fi
      wait_vm "$OS_VM"
      os_push
      os_run prep install config ;;
    deploy) os_push; os_run config deploy init ;;
    init)   os_run init ;;
    status) os_run status
            echo; echo "  Horizon  http://$(ip_of "$OS_VM")   (user: admin, password: ./lab.sh openstack password)" ;;
    password) multipass exec "$OS_VM" -- awk '/^keystone_admin_password:/{print $2}' /etc/kolla/passwords.yml ;;
    shell)  multipass shell "$OS_VM" ;;
    stop)   multipass stop "$OS_VM" ;;
    start)  multipass start "$OS_VM" ;;
    snapshot) local name="${1:-os-$(date +%Y%m%d-%H%M)}"
              multipass stop "$OS_VM"; multipass snapshot "$OS_VM" --name "$name"; multipass start "$OS_VM" ;;
    down)   read -r -p "Delete VM $OS_VM permanently? [y/N] " a
            [[ $a == y || $a == Y ]] && multipass delete --purge "$OS_VM" ;;
    *) cat <<'EOF'
./lab.sh openstack up         create os-aio VM, prep network, install Kolla-Ansible
./lab.sh openstack deploy     bootstrap, prechecks, pull, deploy, post-deploy, demo resources
./lab.sh openstack init       (re)create demo network, image, flavor and test-vm
./lab.sh openstack status     services, agents, servers, containers, Horizon URL
./lab.sh openstack password   print the Horizon/Keystone admin password
./lab.sh openstack shell | stop | start | snapshot [name] | down
EOF
  esac
}

# ---- Simulated H100 fleet: KWOK fake nodes + GPU/InfiniBand fault injection -----
# 8 fake nodes (h100-01..08, 8 GPUs + 8 NDR HCAs each) cost a few MB: KWOK fakes the kubelet, gpusim fakes
# dcgm-exporter / IB counters and the node-side tools (nvidia-smi, dmesg, ibstat, ...). See gpu/README.md.
KWOK_VERSION="${KWOK_VERSION:-v0.8.0}"
export GPUSIM_STATE="$PWD/gpu/state.json"
gpusim() { python3 gpu/gpusim.py "$@"; }
cluster_up() { [[ -f $KCFG ]] && kubectl --request-timeout=5s get --raw /readyz >/dev/null 2>&1; }
gpu_ready() { cluster_up && kubectl get ns gpu-sim >/dev/null 2>&1; }

# KWOK (fake kubelets) is shared by the GPU and Ceph layers.
kwok_up() {
  log "KWOK $KWOK_VERSION (fake kubelets)"
  kubectl apply -f "https://github.com/kubernetes-sigs/kwok/releases/download/$KWOK_VERSION/kwok.yaml"
  kubectl apply -f "https://github.com/kubernetes-sigs/kwok/releases/download/$KWOK_VERSION/stage-fast.yaml"
  kubectl -n kube-system rollout status deploy/kwok-controller --timeout=180s
}
kwok_down() {   # only once neither fake fleet is left
  [[ -n "$(kubectl get nodes -l type=kwok -o name 2>/dev/null)" ]] && return 0
  kubectl delete -f "https://github.com/kubernetes-sigs/kwok/releases/download/$KWOK_VERSION/stage-fast.yaml" --ignore-not-found
  kubectl delete -f "https://github.com/kubernetes-sigs/kwok/releases/download/$KWOK_VERSION/kwok.yaml" --ignore-not-found
}

# Pull the llm-train pod->node map (the fake shell treats a node without a running pod as drained/idle).
gpu_pods() {
  gpu_ready || return 0
  local map   # only overwrite the saved map when kubectl succeeded (an API blip must not look like "all drained")
  map="$(kubectl get pods -n ml -l app=llm-train --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{.spec.nodeName}={.metadata.name}{"\n"}{end}')" || { echo "(could not read llm-train pods; keeping last known map)" >&2; return 0; }
  printf '%s\n' "$map" | gpusim set-pods
}

# Push fault state to the cluster: exporter ConfigMap, device-plugin allocatable, crashed training pods.
gpu_sync() {
  if ! gpu_ready; then echo "(cluster/gpu-sim not reachable: state saved in gpu/state.json; 'gpu ssh' works offline)"; return 0; fi
  kubectl -n gpu-sim create configmap gpusim-state --from-file=state.json="$GPUSIM_STATE" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  local n
  for n in $(gpusim nodes); do
    kubectl patch node "$n" --subresource=status --type=merge -p "$(gpusim status-patch "$n")" >/dev/null
  done
  for n in $(gpusim pending-restarts); do      # the job crashed on $n: torchrun restarts it
    kubectl delete pod -n ml -l app=llm-train --field-selector="spec.nodeName=$n" --wait=false >/dev/null 2>&1 || true
  done
}

gpu() {
  local cmd="${1:-help}"; shift || true
  case $cmd in
    up)
      need kubectl; need helm; cluster_up || die "cluster not reachable; run: ./lab.sh start"
      repos; install_mon      # values drop kubelet scrapes and node-exporter on the fake nodes
      kwok_up
      log "8 fake H100 nodes"
      gpusim node-manifests | kubectl apply -f -
      kubectl wait --for=condition=Ready node -l node-role.kubernetes.io/gpu --timeout=180s
      [[ -f $GPUSIM_STATE ]] || gpusim clear >/dev/null
      log "gpusim exporter, alerts, dashboard, llm-train"
      kubectl apply -f gpu/k8s/exporter.yaml
      kubectl -n gpu-sim create configmap gpusim-code --from-file=gpu/gpusim.py --dry-run=client -o yaml | kubectl apply -f -
      gpu_sync
      kubectl apply -f gpu/k8s/alerts.yaml -f gpu/k8s/dashboard.yaml -f gpu/k8s/llm-train.yaml
      kubectl -n gpu-sim rollout restart deploy/gpusim-exporter >/dev/null
      kubectl -n gpu-sim rollout status deploy/gpusim-exporter --timeout=180s
      gpu status ;;
    status)
      gpu_pods
      kubectl get nodes -l node-role.kubernetes.io/gpu -o custom-columns='NODE:.metadata.name,STATUS:.status.conditions[?(@.type=="Ready")].status,SCHED:.spec.unschedulable,GPU:.status.allocatable.nvidia\.com/gpu,IB:.status.allocatable.rdma/ib,ZONE:.metadata.labels.topology\.kubernetes\.io/zone'
      echo; kubectl get pods -n ml -o wide
      echo; gpusim show ;;
    faults) gpusim faults ;;
    fault)  [[ $# -ge 2 ]] || die "usage: ./lab.sh gpu fault <type> <node> [gpu|hca index]   (types: ./lab.sh gpu faults)"
            gpu_pods; gpusim add "$@"; gpu_sync
            echo "Metrics reach Prometheus in ~1-2 min (ConfigMap sync + scrape)." ;;
    drill)  gpu_pods; gpusim drill "$@"; gpu_sync
            echo; echo "Investigate with Grafana (GPU Fleet), alerts, kubectl and ./lab.sh gpu ssh <node>."
            echo "Answer: ./lab.sh gpu reveal    Reset: ./lab.sh gpu clear" ;;
    reveal) gpusim reveal ;;
    show)   gpusim show ;;
    clear)  gpusim clear; gpu_sync
            if gpu_ready; then kubectl uncordon $(gpusim nodes) >/dev/null; fi ;;
    ssh)    [[ $# -ge 1 ]] || die "usage: ./lab.sh gpu ssh <node>   (nodes: $(gpusim nodes))"
            gpu_pods; gpu_sync >/dev/null; gpusim shell "$1" ;;
    run)    [[ $# -ge 2 ]] || die "usage: ./lab.sh gpu run <node> '<command>'   e.g. ./lab.sh gpu run h100-03 'dmesg -T | grep Xid'"
            gpu_pods; gpu_sync >/dev/null; gpusim run "$@" ;;
    nccl)   gpu_pods; gpusim run h100-01 all_reduce_perf -b 1M -e 8G -f 4 -g 1 ;;
    fixtures) gpusim fixtures "${1:-gpu/faults}" ;;
    down)
      kubectl delete -f gpu/k8s/llm-train.yaml -f gpu/k8s/alerts.yaml -f gpu/k8s/dashboard.yaml --ignore-not-found
      kubectl delete -f gpu/k8s/exporter.yaml --ignore-not-found
      kubectl delete node $(gpusim nodes) --ignore-not-found
      kwok_down ;;
    *) cat <<'EOF'
./lab.sh gpu up                    KWOK + 8 fake H100 nodes + exporter + alerts + dashboard + llm-train
./lab.sh gpu status                nodes (allocatable GPUs / IB HCAs), training pods, active faults
./lab.sh gpu faults                list fault types (GPU: Xid 79/94/95/64/31, ECC, thermal, NVLink, PCIe; IB: down/flap/errors/width/congestion)
./lab.sh gpu fault <type> <node> [n]   inject a fault (n = GPU or HCA index)
./lab.sh gpu drill [level 1-3]     inject a random HIDDEN fault and print the ticket
./lab.sh gpu reveal | show | clear answer key / active faults / heal everything (also uncordons)
./lab.sh gpu ssh <node>            fake root shell: nvidia-smi, dmesg, dcgmi, ibstat, perfquery, ibdiagnet, ...
./lab.sh gpu run <node> '<cmd>'    run one fake command, e.g. 'nvidia-smi -q -i 5 -d ECC'
./lab.sh gpu nccl                  nccl-tests all_reduce_perf across all 64 GPUs
./lab.sh gpu fixtures [dir]        write every fault's outputs as text files (default gpu/faults)
./lab.sh gpu down                  remove the GPU layer
The fake shell, faults and fixtures work without the cluster (state in gpu/state.json).
EOF
  esac
}

# ---- Simulated Ceph storage cluster: 6 KWOK storage nodes + cephsim (ceph-mgr metrics, fake ceph CLI) ------------
# ceph-01..06, 4x 7.68 TB NVMe OSDs each (24 OSDs), 3 mons, CephFS 'ckpt' = llm-train's checkpoint store. See ceph/README.md.
export CEPHSIM_STATE="$PWD/ceph/state.json"
cephsim() { python3 ceph/cephsim.py "$@"; }
ceph_ready() { cluster_up && kubectl get ns ceph-sim >/dev/null 2>&1; }

ceph_sync() {
  if ! ceph_ready; then echo "(cluster/ceph-sim not reachable: state saved in ceph/state.json; 'ceph ssh' works offline)"; return 0; fi
  kubectl -n ceph-sim create configmap cephsim-state --from-file=state.json="$CEPHSIM_STATE" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

ceph() {
  local cmd="${1:-help}"; shift || true
  case $cmd in
    up)
      need kubectl; need helm; cluster_up || die "cluster not reachable; run: ./lab.sh start"
      repos; install_mon      # values drop kubelet scrapes on the fake nodes (h100-*, ceph-0*)
      kubectl -n kube-system get deploy kwok-controller >/dev/null 2>&1 || kwok_up
      log "6 fake Ceph storage nodes"
      cephsim node-manifests | kubectl apply -f -
      kubectl wait --for=condition=Ready node -l node-role.kubernetes.io/storage --timeout=180s
      local n; for n in $(cephsim nodes); do
        kubectl patch node "$n" --subresource=status --type=merge -p "$(cephsim status-patch "$n")" >/dev/null
      done
      [[ -f $CEPHSIM_STATE ]] || cephsim clear >/dev/null
      log "cephsim exporter, alerts, dashboard"
      kubectl apply -f ceph/k8s/exporter.yaml
      kubectl -n ceph-sim create configmap cephsim-code --from-file=ceph/cephsim.py --dry-run=client -o yaml | kubectl apply -f -
      ceph_sync
      kubectl apply -f ceph/k8s/alerts.yaml -f ceph/k8s/dashboard.yaml
      kubectl -n ceph-sim rollout restart deploy/cephsim-exporter >/dev/null
      kubectl -n ceph-sim rollout status deploy/cephsim-exporter --timeout=180s
      ceph status ;;
    status)
      if cluster_up; then
        kubectl get nodes -l node-role.kubernetes.io/storage -o custom-columns='NODE:.metadata.name,STATUS:.status.conditions[?(@.type=="Ready")].status,ZONE:.metadata.labels.topology\.kubernetes\.io/zone,MON:.metadata.labels.ceph\.io/mon'
        echo; kubectl get pods -n ceph-sim -o wide 2>/dev/null; echo
      fi
      cephsim run ceph-02 ceph -s; cephsim show ;;
    faults) cephsim faults ;;
    fault)  [[ $# -ge 1 ]] || die "usage: ./lab.sh ceph fault <type> [osd id | host]   (types: ./lab.sh ceph faults)"
            cephsim add "$@"; ceph_sync
            echo "Metrics reach Prometheus in ~1-2 min (ConfigMap sync + scrape)." ;;
    drill)  cephsim drill "$@"; ceph_sync
            echo; echo "Investigate with Grafana (Ceph Cluster), alerts and ./lab.sh ceph ssh ceph-01 (admin host)."
            echo "Answer: ./lab.sh ceph reveal    Reset: ./lab.sh ceph clear" ;;
    reveal) cephsim reveal ;;
    show)   cephsim show ;;
    clear)  cephsim clear; ceph_sync ;;
    ssh)    cephsim shell "${1:-ceph-01}" ;;
    run)    [[ $# -ge 2 ]] || die "usage: ./lab.sh ceph run <host> '<command>'   e.g. ./lab.sh ceph run ceph-01 'ceph health detail'"
            cephsim run "$@" ;;
    fixtures) cephsim fixtures "${1:-ceph/faults}" ;;
    down)
      kubectl delete -f ceph/k8s/alerts.yaml -f ceph/k8s/dashboard.yaml -f ceph/k8s/exporter.yaml --ignore-not-found
      kubectl delete node $(cephsim nodes) --ignore-not-found
      kwok_down ;;
    *) cat <<'EOH'
./lab.sh ceph up                   KWOK + 6 fake Ceph storage nodes + exporter (ceph-mgr metrics) + alerts + dashboard
./lab.sh ceph status               storage nodes, exporter pod, 'ceph -s', active faults
./lab.sh ceph faults               list fault types (OSD crash/disk/full/slow, host down, mon, flags, scrub, MTU)
./lab.sh ceph fault <type> [target]   inject a fault (target = OSD id, host, or mon host)
./lab.sh ceph drill [level 1-3]    inject a random HIDDEN fault and print the ticket
./lab.sh ceph reveal | show | clear   answer key / active faults / heal everything
./lab.sh ceph ssh [host]           fake root shell (default ceph-01, the admin host): ceph, rados, smartctl, dmesg, ...
./lab.sh ceph run <host> '<cmd>'   run one fake command, e.g. 'ceph osd df tree'
./lab.sh ceph fixtures [dir]       write every fault's outputs as text files (default ceph/faults)
./lab.sh ceph down                 remove the Ceph layer
The fake shell, faults and fixtures work without the cluster (state in ceph/state.json).
EOH
  esac
}

help() {
  cat <<'EOF'
./lab.sh prereqs          install multipass, kubectl, helm, k9s, k6 via brew
./lab.sh all              vms + cluster + platform + apps + 'baseline' snapshot
./lab.sh vms | cluster | platform | apps    run a single stage
./lab.sh status           show URLs
./lab.sh load             run k6 load test against podinfo
./lab.sh chaos [name]     list or run a Chaos Mesh experiment
./lab.sh snapshot [name] | restore <name>
./lab.sh stop | start     pause/resume VMs (saves battery)
./lab.sh down             delete everything
./lab.sh openstack help   OpenStack all-in-one (Kolla-Ansible) commands
./lab.sh gpu help         simulated 8x H100 fleet: GPU / InfiniBand fault drills
./lab.sh ceph help        simulated Ceph storage cluster (24 OSDs): storage fault drills
EOF
}

"${1:-help}" "${@:2}"
