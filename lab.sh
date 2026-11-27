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
  local d s=http; d="$(domain)"
  kubectl get nodes
  # https once ./lab.sh tls has installed the trusted wildcard cert (and it still matches cp1's current IP)
  kubectl -n kube-system get secret lab-wildcard-tls >/dev/null 2>&1 && [[ -f .tls/domain && "$(cat .tls/domain)" == "$d" ]] && s=https
  cat <<EOF

  Grafana       $s://grafana.$d        (user: admin, password in .grafana-password)
  Prometheus    $s://prometheus.$d
  Alertmanager  $s://alertmanager.$d
  Chaos Mesh    $s://chaos.$d
  podinfo       $s://podinfo.$d

  export KUBECONFIG=$KCFG
EOF
  [[ $s == https ]] || echo "  (HTTPS with a trusted certificate: ./lab.sh tls)"
}

# ---- HTTPS: one trusted wildcard certificate as Traefik's default certificate --------------------------------
# k3s' Traefik already serves every Ingress on :443, but with its self-signed "TRAEFIK DEFAULT CERT".
# mkcert (a local CA trusted by macOS / browsers) signs *.<cp1-ip>.nip.io; the CA's private key stays on the Mac.
# Rerun after cp1's IP changes (the hostnames change with it).
tls() {
  need mkcert; cluster_up || die "cluster not reachable; run: ./lab.sh start"
  [[ -f "$(mkcert -CAROOT)/rootCA.pem" ]] || die "run 'mkcert -install' once first (adds mkcert's local CA to the macOS trust store; asks for your password)"
  local d dir="$PWD/.tls"; d="$(domain)"
  mkdir -p "$dir"; chmod 700 "$dir"
  log "Wildcard certificate for *.$d"
  mkcert -cert-file "$dir/tls.crt" -key-file "$dir/tls.key" "*.$d" "$d"
  echo "$d" > "$dir/domain"
  kubectl -n kube-system create secret tls lab-wildcard-tls --cert="$dir/tls.crt" --key="$dir/tls.key" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f - <<EOF
apiVersion: traefik.io/v1alpha1
kind: TLSStore
metadata:
  name: default            # Traefik's default certificate for every HTTPS route without its own
  namespace: kube-system
spec:
  defaultCertificate:
    secretName: lab-wildcard-tls
EOF
  log "Checking HTTPS (Traefik reloads the certificate in a few seconds)"
  local n ok; sleep 5
  for n in grafana prometheus alertmanager chaos podinfo; do
    ok="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$n.$d/")" || ok="TLS error"
    printf '  https://%-28s %s\n' "$n.$d" "$ok"
  done
  echo "Safari/Chrome trust it now; restart Firefox once. Expires: $(openssl x509 -enddate -noout -in "$dir/tls.crt" | cut -d= -f2)"
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

# ---- Bare-metal provisioning layer: MAAS controller VM + Terraform -----------------
# A real MAAS (region + rack) in its own VM, configured entirely by Terraform (terraform/maas).
# No PXE-bootable hardware on a Mac, so the Dell fleet is modelled in Terraform (plan-only) and the
# k3s VMs are enrolled as already-deployed machines (MAAS "brownfield" import). See maas/README.md.
MAAS_VM="${MAAS_VM:-maas}"
MAAS_CPUS="${MAAS_CPUS:-2}"
MAAS_MEM="${MAAS_MEM:-4G}"            # 3G ran out of memory (MAAS 3.7 = region + rack + Temporal + PostgreSQL)
MAAS_DISK="${MAAS_DISK:-25G}"
MAAS_DIR="$PWD/.maas"              # admin password + API key (gitignored)
MAAS_TF="terraform/maas"

maas_url() { echo "http://$(ip_of "$MAAS_VM"):5240/MAAS"; }
# multipass exec sometimes never returns after the remote command has finished: cap every call.
mpx() { local t="$1"; shift; perl -e 'alarm shift; exec @ARGV' "$t" multipass exec "$@"; }
maas_cli() { mpx 120 "$MAAS_VM" -- maas admin "$@"; }
maas_env() {
  [[ -s $MAAS_DIR/api-key ]] || die "no MAAS API key yet; run: ./lab.sh maas up"
  export MAAS_API_URL; MAAS_API_URL="$(maas_url)"
  export MAAS_API_KEY; MAAS_API_KEY="$(cat "$MAAS_DIR/api-key")"
  export TF_VAR_rack_controller_hostname="$MAAS_VM"
  # After ./lab.sh maas enroll, let Terraform tag the imported k3s machines.
  [[ -f $MAAS_DIR/enrolled ]] && export TF_VAR_tag_lab_nodes=true || true
}
maas_machines() {
  maas_cli machines read | python3 -c '
import json, sys
ms = json.load(sys.stdin)
print("%-10s %-12s %-14s %-10s %s" % ("HOSTNAME", "STATUS", "POOL", "ZONE", "TAGS"))
for m in sorted(ms, key=lambda m: m["hostname"]):
    print("%-10s %-12s %-14s %-10s %s" % (m["hostname"], m["status_name"], m["pool"]["name"],
          m["zone"]["name"], ",".join(m["tag_names"]) or "-"))
print("(%d machines)" % len(ms))'
}

maas() {
  local cmd="${1:-help}"; shift || true
  case $cmd in
    up)
      need multipass
      if ! multipass info "$MAAS_VM" >/dev/null 2>&1; then
        log "Launching $MAAS_VM ($MAAS_CPUS vCPU, $MAAS_MEM RAM, $MAAS_DISK disk)"
        multipass launch "$IMAGE" --name "$MAAS_VM" --cpus "$MAAS_CPUS" --memory "$MAAS_MEM" --disk "$MAAS_DISK" --timeout 900 \
          || multipass info "$MAAS_VM" >/dev/null 2>&1 || die "launch of $MAAS_VM failed"
      fi
      wait_vm "$MAAS_VM"
      mkdir -p "$MAAS_DIR"; chmod 700 "$MAAS_DIR"
      [[ -s $MAAS_DIR/admin-password ]] || { openssl rand -base64 18 | tr -d '/+=' > "$MAAS_DIR/admin-password"; chmod 600 "$MAAS_DIR/admin-password"; }
      log "Installing MAAS in $MAAS_VM (first run downloads ~500 MB of snaps)"
      multipass transfer maas/bootstrap.sh "$MAAS_VM:/home/ubuntu/bootstrap.sh"
      multipass exec "$MAAS_VM" -- env MAAS_URL="$(maas_url)" MAAS_ADMIN_PASSWORD="$(cat "$MAAS_DIR/admin-password")" \
        bash /home/ubuntu/bootstrap.sh > "$MAAS_DIR/api-key.tmp"
      mv "$MAAS_DIR/api-key.tmp" "$MAAS_DIR/api-key"; chmod 600 "$MAAS_DIR/api-key"
      maas ui
      echo; echo "Next: ./lab.sh maas tf init && ./lab.sh maas tf plan" ;;
    status)
      multipass info "$MAAS_VM" | awk '/^(Name|State|IPv4|Memory usage)/'
      echo; printf 'API: '; curl -s -o /dev/null -w '%{http_code}\n' "$(maas_url)/api/2.0/version/" || true
      printf 'Boot images importing: '; maas_cli boot-resources is-importing 2>/dev/null || echo "?"
      echo; maas_machines ;;
    machines) maas_machines ;;
    ui)  echo "  MAAS UI   $(maas_url)/   user: admin   password: cat .maas/admin-password" ;;
    cli) maas_cli "$@" ;;                         # e.g. ./lab.sh maas cli fabrics read
    enroll)
      # Brownfield import: register running VMs as Deployed machines and upload their hardware inventory.
      local url key n; url="$(maas_url)"; key="$(cat "$MAAS_DIR/api-key")"
      for n in "${@:-${NODES[@]}}"; do
        log "Enrolling $n as a deployed machine"
        mpx 600 "$n" -- bash -c '
          set -e; cd /tmp
          curl -fsSO "$1/maas-run-scripts" && chmod +x maas-run-scripts
          [ -f "$2-creds.yaml" ] || sudo ./maas-run-scripts register-machine --hostname "$2" "$1" "$3"
          sudo ./maas-run-scripts report-results --config "$2-creds.yaml"' _ "$url" "$n" "$key"
      done
      touch "$MAAS_DIR/enrolled"
      maas_machines
      echo; echo "Known limit: on k3s nodes MAAS can't store the hardware data (commissioning shows Failed)."
      echo "flannel.1 (10.42.x.0/32) and cni0 (10.42.x.0/24) are nested subnets on one host, which MAAS rejects."
      echo "Details: ./lab.sh maas cli events query hostname=w1 limit=2"
      echo; echo "Tag them from Terraform: ./lab.sh maas tf apply   (tag_lab_nodes is now on)" ;;
    tf)
      need terraform; maas_env
      terraform -chdir="$MAAS_TF" "${@:-plan}" ;;
    fleet-plan)                                   # show what Terraform would do to a real Dell fleet
      need terraform; maas_env
      terraform -chdir="$MAAS_TF" plan -var enroll_dell_fleet=true -var deploy_dell_fleet=true -var idrac_password=plan-only ;;
    shell) multipass shell "$MAAS_VM" ;;
    stop)  multipass stop "$MAAS_VM" ;;
    start) multipass start "$MAAS_VM" ;;
    snapshot) local name="${1:-maas-$(date +%Y%m%d-%H%M)}"
              multipass stop "$MAAS_VM"; multipass snapshot "$MAAS_VM" --name "$name"; multipass start "$MAAS_VM" ;;
    down)
      read -r -p "Delete VM $MAAS_VM and its Terraform state permanently? [y/N] " a
      [[ $a == y || $a == Y ]] || exit 0
      multipass delete --purge "$MAAS_VM"
      rm -rf "$MAAS_DIR" "$MAAS_TF"/terraform.tfstate* "$MAAS_TF"/.terraform ;;
    *) cat <<'EOF'
./lab.sh maas up              create the maas VM, install MAAS 3.7 (region+rack), admin user, API key
./lab.sh maas tf <args>       run terraform in terraform/maas with MAAS creds set (init|plan|apply|destroy|...)
./lab.sh maas fleet-plan      terraform plan with the 14-server Dell fleet enrolled + deployed (plan only)
./lab.sh maas enroll [vm...]  import running VMs (default cp1 w1 w2) as Deployed machines with hardware inventory
./lab.sh maas status          VM, API, image import and machine list
./lab.sh maas machines | ui | cli <args> | shell | stop | start | snapshot [name] | down
The maas VM is not part of ./lab.sh stop|start|snapshot (k3s only): manage it with the commands above.
EOF
  esac
}

help() {
  cat <<'EOF'
./lab.sh prereqs          install multipass, kubectl, helm, k9s, k6 via brew
./lab.sh all              vms + cluster + platform + apps + 'baseline' snapshot
./lab.sh vms | cluster | platform | apps    run a single stage
./lab.sh status           show URLs
./lab.sh tls              HTTPS: trusted wildcard cert (mkcert) for Grafana, Prometheus, ...
./lab.sh load             run k6 load test against podinfo
./lab.sh chaos [name]     list or run a Chaos Mesh experiment
./lab.sh snapshot [name] | restore <name>
./lab.sh stop | start     pause/resume VMs (saves battery)
./lab.sh down             delete everything
./lab.sh openstack help   OpenStack all-in-one (Kolla-Ansible) commands
./lab.sh gpu help         simulated 8x H100 fleet: GPU / InfiniBand fault drills
./lab.sh ceph help        simulated Ceph storage cluster (24 OSDs): storage fault drills
./lab.sh maas help        bare-metal provisioning layer: MAAS controller VM driven by Terraform
EOF
}

"${1:-help}" "${@:2}"
