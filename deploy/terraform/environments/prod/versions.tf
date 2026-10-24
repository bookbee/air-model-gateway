terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.0"
    }
  }
  # State in a GCS bucket the platform owns, one prefix per environment:
  #   terraform init -backend-config="bucket=<state-bucket>"
  backend "gcs" {
    prefix = "air-model-gateway/prod"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
