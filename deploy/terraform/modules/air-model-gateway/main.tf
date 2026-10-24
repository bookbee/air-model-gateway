# What the gateway owns in one environment: its Redis, its secrets, and the
# IAM that lets its pods (and only its pods) read them. The cluster and VPC are
# inputs: the platform owns them (modules/gke-platform builds one if needed).

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  labels = merge(var.labels, {
    app         = "air-model-gateway"
    environment = var.environment
    managed-by  = "terraform"
  })

  # Direct Workload Identity Federation: IAM is granted to the Kubernetes
  # service account itself, so there is no Google service account and no key.
  workload_principal = format(
    "principal://iam.googleapis.com/projects/%s/locations/global/workloadIdentityPools/%s.svc.id.goog/subject/ns/%s/sa/%s",
    data.google_project.this.number, var.project_id, var.namespace, var.kubernetes_service_account,
  )

  # Secrets the gateway's pods mount. Values for tokens and provider keys are
  # added out of band so they never land in Terraform state; the Redis ones are
  # written here because only Terraform knows them.
  gateway_secrets = merge(
    {
      service_tokens = "air-model-gateway-service-tokens"
      redis_url      = "air-model-gateway-redis-url"
      redis_ca       = "air-model-gateway-redis-ca"
    },
    { for k, v in var.provider_secrets : lower(k) => v },
  )
}

resource "google_project_service" "apis" {
  for_each           = toset(["redis.googleapis.com", "secretmanager.googleapis.com"])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---- Memorystore ------------------------------------------------------------

resource "google_redis_instance" "gateway" {
  project        = var.project_id
  region         = var.region
  name           = "air-model-gateway-${var.environment}"
  display_name   = "air-model-gateway (${var.environment})"
  tier           = var.redis_tier
  memory_size_gb = var.redis_memory_size_gb
  redis_version  = var.redis_version
  labels         = local.labels

  authorized_network      = var.network_id
  auth_enabled            = true
  transit_encryption_mode = "SERVER_AUTHENTICATION"

  redis_configs = {
    # Evict only keys with a TTL. Every key the gateway writes has one (cache
    # entries by cache_ttl_seconds, rate-limit buckets by refill time), so this
    # bounds memory without ever touching a key someone else put there.
    maxmemory-policy = "volatile-lru"
  }

  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 3
        minutes = 0
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# ---- Secret Manager -----------------------------------------------------------

resource "google_secret_manager_secret" "gateway" {
  for_each  = local.gateway_secrets
  project   = var.project_id
  secret_id = each.value
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "gateway" {
  for_each  = local.gateway_secrets
  project   = var.project_id
  secret_id = google_secret_manager_secret.gateway[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.workload_principal
}

# The URL carries the AUTH string and points the client at the mounted CA.
# Hostname checking is off because the client dials Memorystore by IP; the
# certificate chain is still verified against the instance's own CA.
resource "google_secret_manager_secret_version" "redis_url" {
  secret = google_secret_manager_secret.gateway["redis_url"].id
  secret_data = format(
    "rediss://:%s@%s:%d/0?ssl_cert_reqs=required&ssl_ca_certs=%s/ca.pem&ssl_check_hostname=false",
    google_redis_instance.gateway.auth_string,
    google_redis_instance.gateway.host,
    google_redis_instance.gateway.port,
    var.redis_ca_mount_path,
  )
}

resource "google_secret_manager_secret_version" "redis_ca" {
  secret      = google_secret_manager_secret.gateway["redis_ca"].id
  secret_data = join("\n", [for c in google_redis_instance.gateway.server_ca_certs : c.cert])
}

# One token secret per consumer. bootstrap-secrets.sh fills them and the
# gateway's service-tokens map together, so the two can't drift.
resource "google_secret_manager_secret" "consumer_token" {
  for_each  = toset(var.consumers)
  project   = var.project_id
  secret_id = "${each.value}-model-gateway-token"
  labels    = merge(local.labels, { consumer = each.value })
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}
