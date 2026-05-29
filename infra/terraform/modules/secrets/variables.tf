variable "name_prefix" {
  type = string
}

variable "tenant_scope" {
  description = "Tenant id (single-tenant) or 'shared' (multi-tenant control plane)."
  type        = string
}
