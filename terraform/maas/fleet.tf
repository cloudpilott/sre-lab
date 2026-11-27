# The Dell fleet the simulators stand in for: 8x PowerEdge XE9680 (8x H100 each) and 6x R760xd (Ceph).
# Greenfield flow: enroll (MAAS powers the box on over iDRAC Redfish, PXE-boots it, commissions it,
# runs the hardware tests) -> Ready -> allocate by pool/tags -> deploy Ubuntu -> cloud-init -> Ansible.
# The MACs and iDRAC IPs come from the rack/cabling sheet; here they are generated placeholders.
locals {
  racks = ["a", "b", "c"]

  gpu_nodes = {
    for i in range(1, 9) : format("h100-%02d", i) => {
      role  = "gpu"
      model = "PowerEdge XE9680"
      pool  = "gpu-compute"
      rack  = i <= 4 ? "a" : "b" # 4 per rack: power density (~10 kW each)
      idrac = cidrhost(var.networks.oob.cidr, 10 + i)
      mac   = format("02:5e:10:01:00:%02x", i)
      ip    = cidrhost(var.networks.k8s.cidr, 100 + i)
    }
  }
  storage_nodes = {
    for i in range(1, 7) : format("ceph-%02d", i) => {
      role  = "storage"
      model = "PowerEdge R760xd"
      pool  = "ceph-storage"
      rack  = local.racks[(i - 1) % 3] # 2 per rack: CRUSH failure domain = rack
      idrac = cidrhost(var.networks.oob.cidr, 40 + i)
      mac   = format("02:5e:10:02:00:%02x", i)
      ip    = cidrhost(var.networks.ceph_public.cidr, 100 + i)
    }
  }
  fleet = merge(local.gpu_nodes, local.storage_nodes)
}

resource "maas_machine" "fleet" {
  for_each = var.enroll_dell_fleet ? local.fleet : {}

  hostname        = each.key
  architecture    = "amd64/generic"
  domain          = maas_dns_domain.site.name
  pool            = maas_resource_pool.pool[each.value.pool].name
  zone            = maas_zone.rack[each.value.rack].name
  pxe_mac_address = each.value.mac
  power_type      = "redfish"
  power_parameters = jsonencode({
    power_address = each.value.idrac
    power_user    = var.idrac_user
    power_pass    = var.idrac_password
  })
  # Built-in scripts that run during commissioning on matching hardware.
  commissioning_scripts = each.value.role == "storage" ? ["update_firmware", "configure_hba"] : ["update_firmware"]

  timeouts {
    create = "45m" # power on + PXE + commissioning + hardware tests
  }
}

resource "maas_instance" "fleet" {
  for_each = var.enroll_dell_fleet && var.deploy_dell_fleet ? local.fleet : {}

  allocate_params {
    hostname = maas_machine.fleet[each.key].hostname
    pool     = maas_resource_pool.pool[each.value.pool].name
    tags     = [maas_tag.role[each.value.role].name]
  }

  deploy_params {
    distro_series = var.distro_series
    user_data = templatefile("${path.module}/templates/user-data.yaml.tftpl", {
      hostname = each.key
      role     = each.value.role
      model    = each.value.model
      ssh_key  = var.ssh_public_key
    })
  }

  # Static address on the role's main network (k8s for GPU nodes, Ceph public for storage nodes).
  network_interfaces {
    name        = "eno1"
    subnet_cidr = each.value.role == "gpu" ? var.networks.k8s.cidr : var.networks.ceph_public.cidr
    ip_address  = each.value.ip
  }

  release_params {
    erase   = true # wipe disks when a machine goes back to the pool
    comment = "released by terraform"
  }

  timeouts {
    create = "60m"
  }
}
