# air-model-gateway, prod. Apply order and what to do after are in
# docs/03-deployment.md.

locals {
  labels = { platform = "air-platform", environment = "prod" }
}

module "platform" {
  source = "../../modules/gke-platform"
  count  = var.create_platform ? 1 : 0

  project_id                 = var.project_id
  region                     = var.region
  master_authorized_networks = var.master_authorized_networks
  deletion_protection        = true
  labels                     = local.labels
}

module "gateway" {
  source = "../../modules/air-model-gateway"

  project_id  = var.project_id
  region      = var.region
  environment = "prod"
  network_id  = var.create_platform ? module.platform[0].network_id : var.network_id
  redis_tier  = "STANDARD_HA"
  labels      = local.labels
}
