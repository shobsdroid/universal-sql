output "deployment_mode" {
  value = var.deployment_mode
}

output "name_prefix" {
  value = local.name_prefix
}

output "vpc_id" {
  value = module.networking.vpc_id
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "postgres_endpoint" {
  description = "Control-plane Postgres (catalog/policy/tenants)."
  value       = module.databases.postgres_endpoint
}

output "redis_endpoint" {
  description = "Redis (freshness L2 cache + rate-limit token buckets)."
  value       = module.databases.redis_endpoint
}

output "kms_key_arn" {
  description = "Tenant-scoped KMS key for cache/Parquet encryption + crypto-shred."
  value       = module.secrets.kms_key_arn
}
