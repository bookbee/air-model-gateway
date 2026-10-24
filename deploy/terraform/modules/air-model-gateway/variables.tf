variable "project_id" {
  description = "GCP project of this environment. One project per environment: secret names are fixed, so two environments can't share a project."
  type        = string
}

variable "region" {
  description = "Region for Memorystore (same region as the GKE cluster)."
  type        = string
}

variable "environment" {
  description = "staging or prod; used in labels and resource names."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod."
  }
}

variable "network_id" {
  description = "VPC the GKE cluster runs in (projects/<p>/global/networks/<n>); Memorystore peers with it."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace the Helm release runs in."
  type        = string
  default     = "air-model-gateway"
}

variable "kubernetes_service_account" {
  description = "Kubernetes service account of the release (the chart's default is the release name)."
  type        = string
  default     = "air-model-gateway"
}

variable "redis_tier" {
  description = "BASIC (single node) or STANDARD_HA (replica with automatic failover)."
  type        = string
  default     = "STANDARD_HA"
}

variable "redis_memory_size_gb" {
  description = "Cache entries and rate-limit buckets are small; 1 GB is ample to start."
  type        = number
  default     = 1
}

variable "redis_version" {
  type    = string
  default = "REDIS_7_2"
}

variable "redis_ca_mount_path" {
  description = "Where the chart mounts the Redis CA (redis.tls.mountPath in the Helm values). Baked into the Redis URL."
  type        = string
  default     = "/etc/air-model-gateway/redis-ca"
}

variable "provider_secrets" {
  description = "Provider API keys to create secret containers for: setting suffix -> secret id. Values are added out of band (bootstrap-secrets.sh), never through Terraform."
  type        = map(string)
  default = {
    ANTHROPIC_API_KEY = "air-model-gateway-anthropic-api-key"
    GEMINI_API_KEY    = "air-model-gateway-gemini-api-key"
    OPENAI_API_KEY    = "air-model-gateway-openai-api-key"
  }
}

variable "consumers" {
  description = "Services that call the gateway. Each gets a <service>-model-gateway-token secret for its own deployment to mount; grant the consumer's identity access in that service's own Terraform."
  type        = list(string)
  default = [
    "air-classifier-service",
    "air-orchestrator-service",
    "air-web-console",
    "air-tools",
    "air-rag",
  ]
}

variable "labels" {
  type    = map(string)
  default = {}
}
