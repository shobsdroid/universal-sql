# Tenant-scoped KMS key. Cache entries and S3 Parquet are encrypted with this
# key; org off-boarding schedules key deletion -> crypto-shred (design §11.3).
# Vault (connector OAuth tokens / API keys) runs in-cluster and is bootstrapped
# by the Helm chart, not provisioned here.

resource "aws_kms_key" "tenant" {
  description             = "USQL ${var.tenant_scope} data key (cache/Parquet encryption)"
  deletion_window_in_days = 7 # crypto-shred waiting period
  enable_key_rotation     = true
}

resource "aws_kms_alias" "tenant" {
  name          = "alias/${var.name_prefix}-data"
  target_key_id = aws_kms_key.tenant.key_id
}
