terraform {
  required_version = ">= 1.6"

  required_providers {
    maas = {
      source  = "canonical/maas"
      version = "~> 2.10"
    }
  }

  # Lab: local state (gitignored, it holds the iDRAC credentials in plain text).
  # Production: a remote backend with locking, e.g.
  #   backend "s3" { bucket = "dc1-tfstate" key = "maas/terraform.tfstate" use_lockfile = true }
}

# Credentials come from the environment (./lab.sh maas tf sets them):
#   MAAS_API_URL = http://<maas-vm>:5240/MAAS    MAAS_API_KEY = admin API key from .maas/api-key
provider "maas" {
  api_version         = "2.0"
  installation_method = "snap"
}
