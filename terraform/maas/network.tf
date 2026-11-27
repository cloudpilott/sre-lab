# Fabrics (L2 domains), VLANs, spaces and subnets: the DC network plan as code.
locals {
  fabrics       = toset([for n in values(var.networks) : n.fabric])
  tagged_vlans  = { for k, n in var.networks : k => n if n.vid != 0 }
  untagged_nets = { for k, n in var.networks : k => n if n.vid == 0 }
}

resource "maas_fabric" "f" {
  for_each = local.fabrics
  name     = "${var.site}-${each.key}"
}

resource "maas_space" "s" {
  for_each = toset([for n in values(var.networks) : n.space])
  name     = each.key
}

# Every fabric gets an untagged VLAN 0 automatically; look it up rather than create it.
data "maas_vlan" "untagged" {
  for_each = local.untagged_nets
  fabric   = maas_fabric.f[each.value.fabric].id
  vlan     = 0
}

resource "maas_vlan" "tagged" {
  for_each = local.tagged_vlans
  fabric   = maas_fabric.f[each.value.fabric].id
  vid      = each.value.vid
  name     = replace(each.key, "_", "-")
  mtu      = each.value.mtu
  space    = maas_space.s[each.value.space].name
}

resource "maas_subnet" "net" {
  for_each    = var.networks
  name        = replace(each.key, "_", "-")
  cidr        = each.value.cidr
  fabric      = maas_fabric.f[each.value.fabric].id
  vlan        = each.value.vid == 0 ? data.maas_vlan.untagged[each.key].vlan : maas_vlan.tagged[each.key].vid
  gateway_ip  = cidrhost(each.value.cidr, 1)
  dns_servers = []
  allow_proxy = false
}

# Keep the low addresses for gateways, switches and VIPs on every subnet.
resource "maas_subnet_ip_range" "infra" {
  for_each = var.networks
  subnet   = maas_subnet.net[each.key].id
  type     = "reserved"
  start_ip = cidrhost(each.value.cidr, 1)
  end_ip   = cidrhost(each.value.cidr, 9)
  comment  = "gateway, ToR switches, VIPs"
}

# PXE: a dynamic range for machines while they commission/boot the installer.
resource "maas_subnet_ip_range" "pxe_dynamic" {
  subnet   = maas_subnet.net["pxe"].id
  type     = "dynamic"
  start_ip = cidrhost(var.networks.pxe.cidr, 512)
  end_ip   = cidrhost(var.networks.pxe.cidr, 1000)
  comment  = "PXE / commissioning leases"
}

data "maas_rack_controller" "primary" {
  count    = var.enable_pxe_dhcp ? 1 : 0
  hostname = var.rack_controller_hostname
}

resource "maas_vlan_dhcp" "pxe" {
  count                   = var.enable_pxe_dhcp ? 1 : 0
  fabric                  = maas_fabric.f[var.networks.pxe.fabric].id
  vlan                    = 0
  primary_rack_controller = data.maas_rack_controller.primary[0].id
  ip_ranges               = [maas_subnet_ip_range.pxe_dynamic.id]
}
