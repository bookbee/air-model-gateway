output "redis_host" {
  value = google_redis_instance.gateway.host
}

output "redis_port" {
  value = google_redis_instance.gateway.port
}

output "workload_principal" {
  description = "IAM principal of the gateway's Kubernetes service account."
  value       = local.workload_principal
}

output "gateway_secret_ids" {
  description = "Secret Manager ids the chart mounts (secrets.gcp.items / redis.tls.gcpSecret)."
  value       = { for k, s in google_secret_manager_secret.gateway : k => s.secret_id }
}

output "consumer_token_secret_ids" {
  description = "Per-consumer token secrets; each consumer's deployment mounts its own."
  value       = { for k, s in google_secret_manager_secret.consumer_token : k => s.secret_id }
}
