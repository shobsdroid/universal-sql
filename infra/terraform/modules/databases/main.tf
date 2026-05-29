# Control-plane Postgres (schemas/policies/tenants) + Redis (freshness L2 cache
# and rate-limit token buckets). Both private-subnet only.

resource "aws_db_subnet_group" "pg" {
  name       = "${var.name_prefix}-pg"
  subnet_ids = var.subnet_ids
}

resource "aws_db_instance" "postgres" {
  identifier             = "${var.name_prefix}-pg"
  engine                 = "postgres"
  engine_version         = "16"
  instance_class         = "db.r6g.large" # illustrative
  allocated_storage      = 100
  multi_az               = var.multi_az
  db_subnet_group_name   = aws_db_subnet_group.pg.name
  vpc_security_group_ids = var.security_group_ids
  storage_encrypted      = true
  skip_final_snapshot    = true # scaffold only; prod takes a final snapshot
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.name_prefix}-redis"
  subnet_ids = var.subnet_ids
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${var.name_prefix}-redis"
  description                = "Freshness L2 cache + rate-limit token buckets"
  engine                     = "redis"
  node_type                  = "cache.r6g.large" # illustrative
  num_cache_clusters         = var.multi_az ? 2 : 1
  automatic_failover_enabled = var.multi_az
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = var.security_group_ids
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
}
