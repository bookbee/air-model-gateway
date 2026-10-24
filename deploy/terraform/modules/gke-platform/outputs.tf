output "network_id" {
  value = google_compute_network.this.id
}

output "cluster_name" {
  value = google_container_cluster.this.name
}

output "cluster_location" {
  value = google_container_cluster.this.location
}

output "image_repository" {
  description = "Docker repository path for image.repository (append /air-model-gateway)."
  value       = var.artifact_registry_repository == null ? null : "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repository}"
}
