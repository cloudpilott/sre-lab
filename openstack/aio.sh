#!/usr/bin/env bash
# Runs INSIDE the os-aio VM as user ubuntu.  Usage: ./aio.sh <stage>...
# Stages: prep install config deploy init status   (run in that order; each is re-runnable)
set -euo pipefail
RELEASE="${RELEASE:-2026.2}"
VENV=/opt/kolla-venv
WORK="$HOME/kolla"
EXT_NET=10.0.2.0/24 EXT_GW=10.0.2.1
CIRROS_URL=https://download.cirros-cloud.net/0.6.3/cirros-0.6.3-aarch64-disk.img
IFACE=$(ip route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')
IP=$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -1)
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
venv() { # shellcheck disable=SC1091
  . "$VENV/bin/activate"; }

prep() {
  log "Host prep: $IFACE $IP"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3-dev libffi-dev gcc libssl-dev \
       libdbus-glib-1-dev python3-venv iptables >/dev/null

  # RabbitMQ/Kolla need the hostname to resolve to the real IP, not 127.0.1.1.
  # Multipass's cloud-init rewrites /etc/hosts on every boot (its user-data overrides
  # cloud.cfg.d), so fix it with a unit that runs after cloud-init and before Docker.
  sudo rm -f /etc/cloud/cloud.cfg.d/99-kolla.cfg
  sudo tee /usr/local/sbin/kolla-fix-hosts >/dev/null <<'FIX'
#!/bin/sh
dev=$(ip route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')
ip=$(ip -4 -o addr show dev "$dev" | awk '{print $4}' | cut -d/ -f1 | head -1)
h=$(hostname)
sed -i "/[[:space:]]$h\([[:space:]]\|\$\)/d" /etc/hosts
echo "$ip $h" >> /etc/hosts
FIX
  sudo chmod +x /usr/local/sbin/kolla-fix-hosts
  sudo tee /etc/systemd/system/kolla-fix-hosts.service >/dev/null <<'UNIT'
[Unit]
Description=Map hostname to the primary IP for Kolla (RabbitMQ)
After=cloud-init.service network-online.target
Wants=network-online.target
Before=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/kolla-fix-hosts
[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable kolla-fix-hosts.service
  sudo /usr/local/sbin/kolla-fix-hosts

  # External network for Neutron: veth0 = gateway 10.0.2.1, veth1 = handed to br-ex.
  sudo tee /etc/systemd/network/25-veth-ext.netdev >/dev/null <<NET
[NetDev]
Name=veth0
Kind=veth
[Peer]
Name=veth1
NET
  sudo tee /etc/systemd/network/25-veth0.network >/dev/null <<NET
[Match]
Name=veth0
[Network]
Address=$EXT_GW/24
ConfigureWithoutCarrier=yes
NET
  sudo tee /etc/systemd/network/25-veth1.network >/dev/null <<NET
[Match]
Name=veth1
[Network]
LinkLocalAddressing=no
IPv6AcceptRA=no
ConfigureWithoutCarrier=yes
NET
  sudo networkctl reload
  sleep 2
  sudo networkctl reconfigure veth0 veth1 || true

  # Let instances on the external network reach the internet through the VM.
  echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/90-kolla-ext.conf >/dev/null
  sudo sysctl -q --system
  sudo tee /etc/systemd/system/kolla-ext-nat.service >/dev/null <<UNIT
[Unit]
Description=NAT for the OpenStack lab external network ($EXT_NET)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
# -o $IFACE matters: Neutron's iptables_hybrid firewall bridges guest traffic through host
# netfilter, and without it this rule would also SNAT floating-IP traffic to the host IP.
ExecStart=/bin/sh -c 'iptables -t nat -D POSTROUTING -s $EXT_NET ! -d $EXT_NET -j MASQUERADE 2>/dev/null; true'
ExecStart=/bin/sh -c 'iptables -t nat -C POSTROUTING -s $EXT_NET -o $IFACE -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -s $EXT_NET -o $IFACE -j MASQUERADE'
ExecStart=/bin/sh -c 'iptables -C FORWARD -s $EXT_NET -j ACCEPT 2>/dev/null || iptables -I FORWARD -s $EXT_NET -j ACCEPT'
ExecStart=/bin/sh -c 'iptables -C FORWARD -d $EXT_NET -j ACCEPT 2>/dev/null || iptables -I FORWARD -d $EXT_NET -j ACCEPT'
[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now kolla-ext-nat.service
  ip -br addr show veth0; ip -br link show veth1
}

install() {
  log "Installing Kolla-Ansible stable/$RELEASE into $VENV"
  sudo mkdir -p "$VENV" /etc/kolla && sudo chown "$USER:" "$VENV" /etc/kolla
  [[ -x $VENV/bin/python ]] || python3 -m venv "$VENV"
  venv
  pip install -q -U pip
  pip install -q "git+https://opendev.org/openstack/kolla-ansible@stable/$RELEASE"
  # The all-in-one inventory runs modules with this venv's python, so the Docker SDK
  # and dbus bindings must live here too (prechecks: "Checking docker SDK version").
  pip install -q docker dbus-python
  pip install -q python-openstackclient -c "https://releases.openstack.org/constraints/upper/$RELEASE" \
    || pip install -q python-openstackclient
  [[ -f /etc/kolla/passwords.yml ]] || cp -r "$VENV/share/kolla-ansible/etc_examples/kolla/"* /etc/kolla/
  mkdir -p "$WORK"
  cp "$VENV/share/kolla-ansible/ansible/inventory/all-in-one" "$WORK/"
  kolla-ansible install-deps
  kolla-ansible --version
}

config() {
  log "Writing /etc/kolla/globals.yml and passwords"
  venv
  sed -e "s/__IP__/$IP/" -e "s/__IFACE__/$IFACE/" "$HOME/globals.yml.tpl" > /etc/kolla/globals.yml
  kolla-genpwd               # fills only blank entries; safe to re-run
  # Nova defaults ARM guests to cpu_mode=host-passthrough, which needs KVM. Under TCG
  # emulation (no nested virt on M2) libvirt rejects it, so pin an emulated CPU model. "max" = every feature QEMU can emulate; cortex-a72 crashed the CirrOS kernel at boot.
  mkdir -p /etc/kolla/config/nova
  cat > /etc/kolla/config/nova/nova-compute.conf <<CONF
[libvirt]
cpu_mode = custom
cpu_models = max
CONF
  grep -vE '^\s*(#|$)' /etc/kolla/globals.yml
}

deploy() {
  venv; cd "$WORK"
  log "bootstrap-servers";  kolla-ansible bootstrap-servers -i all-in-one
  # quay.io/openstack.kolla images are upstream's CI/test builds: fine for a lab, flagged by prechecks.
  log "prechecks";          kolla-ansible prechecks -i all-in-one --use-test-images
  log "pull images";        kolla-ansible pull -i all-in-one
  log "deploy";             kolla-ansible deploy -i all-in-one
  log "post-deploy";        kolla-ansible post-deploy -i all-in-one
  mkdir -p ~/.config/openstack && cp /etc/kolla/clouds.yaml ~/.config/openstack/
}

init() {
  venv; export OS_CLOUD=kolla-admin
  log "Creating demo resources"
  openstack image show cirros >/dev/null 2>&1 || {
    curl -fsSL -o /tmp/cirros.img "$CIRROS_URL"
    openstack image create cirros --public --disk-format qcow2 --container-format bare --file /tmp/cirros.img
  }
  openstack flavor show m1.tiny >/dev/null 2>&1 || openstack flavor create m1.tiny --vcpus 1 --ram 512 --disk 1   # 256 MB is too small for ARM UEFI + GRUB
  openstack network show public >/dev/null 2>&1 || openstack network create public --external \
      --provider-network-type flat --provider-physical-network physnet1
  openstack subnet show public-subnet >/dev/null 2>&1 || openstack subnet create public-subnet --network public \
      --subnet-range "$EXT_NET" --gateway "$EXT_GW" --no-dhcp --allocation-pool start=10.0.2.100,end=10.0.2.200
  openstack network show demo-net >/dev/null 2>&1 || openstack network create demo-net
  openstack subnet show demo-subnet >/dev/null 2>&1 || openstack subnet create demo-subnet --network demo-net \
      --subnet-range 192.168.100.0/24 --dns-nameserver 1.1.1.1
  openstack router show demo-router >/dev/null 2>&1 || {
    openstack router create demo-router
    openstack router set demo-router --external-gateway public
    openstack router add subnet demo-router demo-subnet
  }
  local sg; sg=$(openstack security group list --project admin -f value -c ID -c Name | awk '$2=="default"{print $1}')
  openstack security group rule create "$sg" --protocol icmp >/dev/null 2>&1 || true
  openstack security group rule create "$sg" --protocol tcp --dst-port 22 >/dev/null 2>&1 || true
  [[ -f ~/.ssh/id_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519
  openstack keypair show lab >/dev/null 2>&1 || openstack keypair create lab --public-key ~/.ssh/id_ed25519.pub
  openstack server show test-vm >/dev/null 2>&1 || {
    log "Booting test-vm (emulated CPU: expect 3-10 minutes)"
    openstack server create test-vm --flavor m1.tiny --image cirros --network demo-net --key-name lab --wait
  }
  local fip; fip=$(openstack floating ip list --port "$(openstack port list --server test-vm -f value -c ID | head -1)" -f value -c 'Floating IP Address')
  if [[ -z $fip ]]; then
    fip=$(openstack floating ip create public -f value -c floating_ip_address)
    openstack server add floating ip test-vm "$fip"
  fi
  echo "test-vm floating IP: $fip"
}

reconfigure() { # apply /etc/kolla changes; e.g. ./aio.sh config reconfigure  (TAGS=nova to limit)
  venv; cd "$WORK"
  log "reconfigure ${TAGS:-all}"
  kolla-ansible reconfigure -i all-in-one ${TAGS:+--tags "$TAGS"}
}

status() {
  venv; export OS_CLOUD=kolla-admin
  openstack compute service list; openstack network agent list
  openstack server list
  sudo docker ps --format '{{.Names}}\t{{.Status}}' | sort | column -t
}

[[ $# -gt 0 ]] || { sed -n '2,3p' "$0"; exit 1; }
for stage in "$@"; do "$stage"; done
