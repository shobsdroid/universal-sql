variable "name_prefix" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

variable "multi_az" {
  description = "Multi-AZ Postgres + Redis (HA). Enabled for the shared multi-tenant stack."
  type        = bool
  default     = true
}
