variable "site" {
  description = "Site name, used in the MAAS name and DNS domain."
  type        = string
  default     = "dc1"
}

variable "rack_controller_hostname" {
  description = "Hostname of the MAAS rack controller that serves DHCP/PXE (the maas VM in the lab)."
  type        = string
  default     = "maas"
}

# ---- Network plan (what a Dell row would be cabled to) ----------------------------
variable "networks" {
  description = "Data-centre networks: fabric, VLAN, CIDR, MTU and MAAS space."
  type = map(object({
    fabric  = string
    vid     = number # 0 = untagged
    cidr    = string
    mtu     = number
    space   = string
    comment = string
  }))
  default = {
    oob = {
      fabric  = "oob", vid = 0, cidr = "172.16.10.0/24", mtu = 1500, space = "oob",
      comment = "iDRAC / BMC out-of-band management"
    }
    pxe = {
      fabric  = "prod", vid = 0, cidr = "172.16.20.0/22", mtu = 1500, space = "provisioning",
      comment = "PXE boot + OS install; MAAS serves DHCP here in a real DC"
    }
    k8s = {
      fabric  = "prod", vid = 30, cidr = "172.16.30.0/24", mtu = 1500, space = "k8s",
      comment = "Kubernetes node / API traffic"
    }
    ceph_public = {
      fabric  = "prod", vid = 40, cidr = "172.16.40.0/24", mtu = 9000, space = "storage-public",
      comment = "Ceph public network (clients <-> MON/OSD)"
    }
    ceph_cluster = {
      fabric  = "prod", vid = 41, cidr = "172.16.41.0/24", mtu = 9000, space = "storage-cluster",
      comment = "Ceph cluster network (OSD replication + heartbeats)"
    }
    gpu_storage = {
      fabric  = "prod", vid = 50, cidr = "172.16.50.0/24", mtu = 9000, space = "gpu-storage",
      comment = "GPU nodes <-> storage (RoCE); IB compute fabric is outside MAAS"
    }
  }
}

variable "enable_pxe_dhcp" {
  description = <<-EOT
    Turn on MAAS-managed DHCP on the PXE VLAN. Only works when the rack controller has a NIC on that
    VLAN (true in a real DC). Leave false in the lab: there the rack controller only sits on the
    Multipass network, and MAAS DHCP there would fight macOS's DHCP and break the k3s VMs.
  EOT
  type        = bool
  default     = false
}

# ---- Brownfield lab machines ---------------------------------------------------
variable "lab_nodes" {
  description = "k3s VMs enrolled by ./lab.sh maas enroll."
  type        = list(string)
  default     = ["cp1", "w1", "w2"]
}

variable "tag_lab_nodes" {
  description = "Tag the enrolled k3s VMs (set automatically by ./lab.sh maas tf after enroll)."
  type        = bool
  default     = false
}

# ---- Dell fleet (greenfield) ---------------------------------------------------
variable "enroll_dell_fleet" {
  description = "Add the Dell servers to MAAS (Redfish power via iDRAC). Needs reachable iDRACs: plan-only in the lab."
  type        = bool
  default     = false
}

variable "deploy_dell_fleet" {
  description = "Allocate and deploy Ubuntu on the enrolled Dell servers."
  type        = bool
  default     = false
}

variable "idrac_user" {
  description = "iDRAC user MAAS uses for Redfish power control."
  type        = string
  default     = "root"
}

variable "idrac_password" {
  description = "iDRAC password. In production read it from Vault (vault provider / TF_VAR_ from CI secrets)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "distro_series" {
  description = "Ubuntu release deployed to the fleet."
  type        = string
  default     = "noble"
}

variable "ssh_public_key" {
  description = "Extra SSH key for the ops user on deployed servers (MAAS also injects the admin user's keys)."
  type        = string
  default     = ""
}
