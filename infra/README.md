# Infrastructure (design scaffold)

> **This is a reviewable design scaffold, not a deployable stack.** It makes the
> IaC design in `design_doc.md` §12 concrete — module layout, the
> single-↔multi-tenant toggle, per-tenant KMS, autoscaling, canary — without
> wiring real cloud credentials, remote state, or a backend. It is intentionally
> **not** `terraform apply`-ready (no provider auth, no S3/DynamoDB backend, node
> AMIs/instance types are illustrative). It has not been `terraform validate`-d
> here because that requires downloading the ~600 MB AWS provider.

## Layout

```
infra/
├── terraform/                 # IaC (design doc §12.3)
│   ├── versions.tf            # terraform + provider constraints
│   ├── variables.tf           # incl. deployment_mode = multi_tenant | single_tenant
│   ├── main.tf                # wires the modules; per-mode logic in locals
│   ├── outputs.tf
│   ├── terraform.tfvars.example
│   └── modules/
│       ├── networking/        # VPC, subnets, NAT, security groups
│       ├── databases/         # Postgres (control plane) + Redis (cache/rate-limit)
│       ├── secrets/           # per-tenant KMS keys (+ Vault runs in-cluster)
│       └── cluster/           # EKS + node groups + autoscaling
└── helm/universal-sql/        # CD (Helm chart, canary via Argo Rollouts)
    ├── values.yaml            # multi-tenant defaults
    ├── values-single-tenant.yaml
    └── templates/             # gateway/executor deployments + HPA
```

## The deployment-mode toggle (the "no code changes" claim)

Both modes use the **same modules and the same chart** — only variable/values
differ (design doc §12.2):

| | `multi_tenant` (default) | `single_tenant` |
|---|---|---|
| Cluster | one shared EKS cluster | one cluster **per tenant** |
| Isolation | k8s namespace per tenant + NetworkPolicy | whole cluster in the tenant VPC |
| Naming | `usql-shared-*` | `usql-<tenant_id>-*` |
| KMS | a key per tenant in the control plane | the tenant's own key |

```bash
# multi-tenant (shared)
terraform apply -var="deployment_mode=multi_tenant"

# single-tenant (dedicated) — same modules, different vars
terraform apply -var="deployment_mode=single_tenant" -var="tenant_id=acme-corp"

# Helm follows the same split:
helm upgrade --install usql ./helm/universal-sql                       # multi
helm upgrade --install usql ./helm/universal-sql \
  -f ./helm/universal-sql/values-single-tenant.yaml --set tenantId=acme-corp
```

CD (design §12.3): GitHub Actions → `terraform plan` review → `helm upgrade` with
Argo Rollouts canary (10% → 50% → 100%), auto-rollback if P95 > SLO for 5 min.
