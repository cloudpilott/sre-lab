output "maas_name" {
  value = local.maas_settings.maas_name
}

output "networks" {
  description = "Subnets MAAS now knows about."
  value       = { for k, s in maas_subnet.net : k => "${s.cidr}  vlan ${var.networks[k].vid}  mtu ${var.networks[k].mtu}  space ${var.networks[k].space}" }
}

output "zones" {
  value = [for z in maas_zone.rack : z.name]
}

output "pools" {
  value = [for p in maas_resource_pool.pool : p.name]
}

output "fleet" {
  description = "Planned Dell servers (enrolled only when enroll_dell_fleet = true)."
  value = {
    for k, m in local.fleet : k => "${m.model}  ${m.pool}  rack-${m.rack}  idrac ${m.idrac}  ${m.ip}"
  }
}

output "fleet_enrolled" {
  value = [for k, m in maas_machine.fleet : k]
}
