variable "name_prefix" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "node_min" {
  type = number
}

variable "node_max" {
  type = number
}

variable "single_tenant_vpc" {
  description = "True when this cluster is dedicated to one tenant (single-tenant mode)."
  type        = bool
  default     = false
}
