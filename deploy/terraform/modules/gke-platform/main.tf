# A minimal GKE platform for a project that has none: VPC, Cloud NAT, an
# Autopilot cluster with the Secret Manager add-on, and an Artifact Registry
# repository. Every AIR service would share it, so it belongs to the platform
# team. If they already run a cluster, skip this module (create_platform =
# false in the environment root) and pass their network id instead.

resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "this" {
  project                 = var.project_id
  name                    = "${var.name}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "this" {
  project       = var.project_id
  region        = var.region
  name          = "${var.name}-${var.region}"
  network       = google_compute_network.this.id
  ip_cidr_range = var.subnet_cidr
  # Nodes are private; Google APIs (Secret Manager, Artifact Registry) are
  # reached without public IPs.
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }
}

# Private nodes need NAT to reach the model providers' public APIs
# (Anthropic, OpenAI, Gemini). NAT also gives egress a stable, allow-listable
# set of addresses.
resource "google_compute_router" "this" {
  project = var.project_id
  region  = var.region
  name    = "${var.name}-router"
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  project                            = var.project_id
  region                             = var.region
  name                               = "${var.name}-nat"
  router                             = google_compute_router.this.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

resource "google_container_cluster" "this" {
  project  = var.project_id
  location = var.region
  name     = "${var.name}-gke"

  # Autopilot: Google runs the nodes; Workload Identity, Dataplane V2
  # (NetworkPolicy enforcement) and Managed Prometheus are on by default.
  enable_autopilot    = true
  deletion_protection = var.deletion_protection

  network    = google_compute_network.this.id
  subnetwork = google_compute_subnetwork.this.id
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_networks) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_networks
        content {
          cidr_block   = cidr_blocks.value.cidr_block
          display_name = cidr_blocks.value.display_name
        }
      }
    }
  }

  release_channel {
    channel = var.release_channel
  }

  # Mounts Secret Manager secrets as files (the chart's SecretProviderClass).
  secret_manager_config {
    enabled = true
  }

  resource_labels = var.labels
  depends_on      = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "this" {
  count         = var.artifact_registry_repository == null ? 0 : 1
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_registry_repository
  format        = "DOCKER"
  labels        = var.labels

  docker_config {
    # A pushed tag can't be moved, so what was reviewed is what runs.
    immutable_tags = true
  }

  depends_on = [google_project_service.apis]
}
