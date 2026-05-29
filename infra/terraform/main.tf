locals {
  is_single = var.deployment_mode == "single_tenant"

  # Single-tenant gets a tenant-scoped name; multi-tenant shares one stack.
  name_prefix = local.is_single ? "usql-${var.tenant_id}" : "usql-shared"

  # Multi-AZ Postgres + larger node ceiling for the shared (multi-tenant) stack;
  # single-tenant stacks are typically smaller and cost-attributed to one tenant.
  multi_az      = !local.is_single
  node_ceiling  = local.is_single ? min(var.node_max, 8) : var.node_max
  tenant_scope  = local.is_single ? var.tenant_id : "shared"

  common_tags = {
    app                   = "universal-sql"
    environment           = var.environment
    deployment_mode       = var.deployment_mode
    tenant_scope          = local.tenant_scope
    data_residency_region = var.data_residency_region
  }
}

module "networking" {
  source      = "./modules/networking"
  name_prefix = local.name_prefix
  vpc_cidr    = var.vpc_cidr
}

module "databases" {
  source             = "./modules/databases"
  name_prefix        = local.name_prefix
  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = [module.networking.data_security_group_id]
  multi_az           = local.multi_az
}

module "secrets" {
  source      = "./modules/secrets"
  name_prefix = local.name_prefix
  # In single-tenant, this is the tenant's own key. In multi-tenant this is the
  # control-plane key; per-tenant keys are provisioned at org onboarding time.
  tenant_scope = local.tenant_scope
}

module "cluster" {
  source             = "./modules/cluster"
  name_prefix        = local.name_prefix
  subnet_ids         = module.networking.private_subnet_ids
  node_min           = var.node_min
  node_max           = local.node_ceiling
  single_tenant_vpc  = local.is_single
}
