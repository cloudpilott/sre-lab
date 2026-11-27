# Global MAAS settings: what you'd otherwise click through in the UI's first-run wizard.
locals {
  maas_settings = {
    maas_name                   = "sre-lab-${var.site}"
    completed_intro             = "true" # skip the UI setup wizard: Terraform owns the config
    default_osystem             = "ubuntu"
    default_distro_series       = var.distro_series
    commissioning_distro_series = var.distro_series
    upstream_dns                = "1.1.1.1 8.8.8.8"
    ntp_servers                 = "ntp.ubuntu.com"
    enable_analytics            = "false"
    release_notifications       = "false"
  }
}

resource "maas_configuration" "this" {
  for_each = local.maas_settings
  key      = each.key
  value    = each.value
}

resource "maas_dns_domain" "site" {
  name          = "${var.site}.lab"
  ttl           = 300
  authoritative = true
}

# Zones = failure domains you can target (rack / power feed). Ceph spreads replicas across them.
resource "maas_zone" "rack" {
  for_each    = toset(["a", "b", "c"])
  name        = "${var.site}-rack-${each.key}"
  description = "${upper(var.site)} row 1, rack ${upper(each.key)} (own PDUs and ToR pair)"
}

# Pools = who may allocate what. Teams get access to a pool, not to individual machines.
resource "maas_resource_pool" "pool" {
  for_each = {
    gpu-compute  = "Dell PowerEdge XE9680, 8x H100 SXM5: training / inference"
    ceph-storage = "Dell PowerEdge R760xd, NVMe: Ceph OSD / MON hosts"
    k8s-control  = "Kubernetes control plane and lab VMs"
  }
  name        = each.key
  description = each.value
}
