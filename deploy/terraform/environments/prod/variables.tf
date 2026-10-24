variable "project_id" {
  description = "The prod project. One project per environment."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "create_platform" {
  description = "Build the VPC, Autopilot cluster and Artifact Registry (modules/gke-platform). false when the platform team already runs them."
  type        = bool
  default     = true
}

variable "network_id" {
  description = "Existing VPC (projects/<p>/global/networks/<n>) when create_platform = false."
  type        = string
  default     = null
}

variable "master_authorized_networks" {
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}
