# Automatic tags: an XPath over the lshw hardware inventory MAAS collects while commissioning.
# Every machine that matches is tagged, so allocation can ask for "gpu-h100" instead of a hostname.
# Use double quotes inside the XPath: MAAS 3.7 fails tag evaluation (and with it commissioning) on single quotes.
resource "maas_tag" "auto" {
  for_each = {
    nvidia-gpu = {
      definition  = "//node[@class=\"display\"]/vendor[contains(., \"NVIDIA\")]"
      comment     = "Has an NVIDIA GPU"
      kernel_opts = ""
    }
    gpu-h100 = {
      definition  = "//node[@class=\"display\"]/product[contains(., \"H100\")]"
      comment     = "Has H100 GPUs: IOMMU passthrough for GPUDirect / NCCL"
      kernel_opts = "intel_iommu=on iommu=pt"
    }
    nvme = {
      definition  = "//node[@class=\"storage\"][contains(description, \"NVMe\")]"
      comment     = "Has NVMe storage"
      kernel_opts = ""
    }
    dell = {
      definition  = "//node[@class=\"system\"]/vendor[contains(., \"Dell\")]"
      comment     = "Dell hardware: firmware updates via iDRAC / OpenManage"
      kernel_opts = ""
    }
  }
  name        = each.key
  definition  = each.value.definition
  comment     = each.value.comment
  kernel_opts = each.value.kernel_opts == "" ? null : each.value.kernel_opts
}

# Manual tags: applied to an explicit list of machines.
resource "maas_tag" "k3s" {
  name     = "k3s"
  comment  = "Brownfield k3s lab nodes (./lab.sh maas enroll)"
  machines = var.tag_lab_nodes ? var.lab_nodes : []
}

resource "maas_tag" "role" {
  for_each = toset(["gpu", "storage"])
  name     = "role-${each.key}"
  comment  = "Fleet role: ${each.key}"
  machines = [for k, m in maas_machine.fleet : m.id if local.fleet[k].role == each.key]
}
