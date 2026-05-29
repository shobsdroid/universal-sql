variable "environment" {
  description = "Deployment environment (dev/staging/prod)."
  type        = string
  default     = "dev"
}

variable "region" {
  description = "Cloud region. Should match data_residency_region for the tenant(s)."
  type        = string
  default     = "us-east-1"
}

variable "deployment_mode" {
  description = "multi_tenant (shared cluster) or single_tenant (dedicated cluster per tenant)."
  type        = string
  default     = "multi_tenant"

  validation {
    condition     = contains(["multi_tenant", "single_tenant"], var.deployment_mode)
    error_message = "deployment_mode must be 'multi_tenant' or 'single_tenant'."
  }
}

variable "tenant_id" {
  description = "Tenant identifier. Required when deployment_mode = single_tenant."
  type        = string
  default     = null

  validation {
    condition     = var.deployment_mode == "multi_tenant" || var.tenant_id != null
    error_message = "tenant_id is required when deployment_mode = single_tenant."
  }
}

variable "data_residency_region" {
  description = "Region that storage and job placement must stay within (compliance)."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "node_min" {
  description = "Minimum worker nodes (autoscaling floor)."
  type        = number
  default     = 3
}

variable "node_max" {
  description = "Maximum worker nodes (autoscaling ceiling / cost guardrail)."
  type        = number
  default     = 20
}
