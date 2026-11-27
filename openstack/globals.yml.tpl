# Kolla-Ansible globals for a single-VM all-in-one on Apple Silicon (M2, no nested virt).
# __IP__ / __IFACE__ are filled in by aio.sh from the VM's primary interface.
kolla_base_distro: "ubuntu"
openstack_release: "2026.2"
openstack_tag_suffix: "-aarch64"          # ARM images: <release>-ubuntu-noble-aarch64

# Networking: no HAProxy, so the "VIP" is just the VM's own address.
kolla_internal_vip_address: "__IP__"
network_interface: "__IFACE__"
neutron_external_interface: "veth1"       # unaddressed end of a veth pair (aio.sh prep)
neutron_plugin_agent: "openvswitch"

# Keep it small: core services only.
enable_haproxy: "no"
enable_keepalived: "no"
enable_proxysql: "no"
enable_fluentd: "no"
enable_heat: "no"
enable_cinder: "no"
enable_horizon: "yes"

# M2 has no nested virtualization -> software emulation (TCG). Tiny guests only.
nova_compute_virt_type: "qemu"
