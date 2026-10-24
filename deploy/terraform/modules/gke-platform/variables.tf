variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  description = "Prefix for the network, cluster and router."
  type        = string
  default     = "air"
}

variable "subnet_cidr" {
  type    = string
  default = "10.10.0.0/20"
}

variable "pods_cidr" {
  type    = string
  default = "10.16.0.0/14"
}

variable "services_cidr" {
  type    = string
  default = "10.20.0.0/20"
}

variable "master_authorized_networks" {
  description = "CIDRs allowed to reach the control plane (CI runners, VPN). Empty = Google-internal only."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "release_channel" {
  type    = string
  default = "REGULAR"
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "artifact_registry_repository" {
  description = "Docker repository for AIR images. Set to null when it lives in a shared project."
  type        = string
  default     = "air"
}

variable "labels" {
  type    = map(string)
  default = {}
}
