"""Static configuration for the prototype.

In production these values live in the control plane (tenant registry,
rate-limit config, freshness config) per the design doc. Here they are
in-process constants so the prototype runs with zero external dependencies.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# --- Auth (dev only) -------------------------------------------------------
# HS256 shared secret stands in for OIDC/JWKS validation in the prototype.
JWT_SECRET = os.environ.get("USQL_JWT_SECRET", "dev-secret-do-not-use-in-prod")
JWT_ALGORITHM = "HS256"

# --- Gateway ---------------------------------------------------------------
REQUEST_TIMEOUT_MS = int(os.environ.get("USQL_REQUEST_TIMEOUT_MS", "30000"))
# Per-source deadline. A source slower than this is dropped and the query
# returns partial results (design doc §4.3 graceful degradation).
SOURCE_TIMEOUT_MS = int(os.environ.get("USQL_SOURCE_TIMEOUT_MS", "5000"))
MAX_ROWS = int(os.environ.get("USQL_MAX_ROWS", "1000"))

# Above this estimated row count a real system would spill to DuckDB/S3
# materialization. The prototype only documents the boundary (see README).
MATERIALIZATION_THRESHOLD = 50_000


@dataclass
class RateLimitPolicy:
    """Three nested token-bucket scopes, refill per minute."""
    connector_rpm: int
    tenant_rpm: int
    user_rpm: int
    burst_multiplier: float = 1.5
    async_overflow: bool = True


@dataclass
class FreshnessPolicy:
    default_ttl_ms: int        # L2 cache TTL
    min_freshness_ms: int      # floor a client cannot go below
    stale_if_error_ms: int     # serve stale within this window if live fetch fails
    supports_etag: bool = False


@dataclass
class ConnectorConfig:
    name: str
    rate_limit: RateLimitPolicy
    freshness: FreshnessPolicy
    # Simulated source behaviour for the mock connectors.
    sim_latency_ms: int = 120
    pushable_filters: tuple = field(default_factory=tuple)


# Scale all rate-limit budgets for load testing without touching the demo
# defaults. e.g. USQL_RATELIMIT_RPM_MULTIPLIER=10000 to drive a k6 1k-QPS run.
_RPM_MULT = float(os.environ.get("USQL_RATELIMIT_RPM_MULTIPLIER", "1"))


def _rpm(value: int) -> int:
    return int(value * _RPM_MULT)


CONNECTORS: dict[str, ConnectorConfig] = {
    "github": ConnectorConfig(
        name="github",
        rate_limit=RateLimitPolicy(
            connector_rpm=_rpm(4000), tenant_rpm=_rpm(400), user_rpm=_rpm(20)),
        freshness=FreshnessPolicy(
            default_ttl_ms=60_000, min_freshness_ms=0,
            stale_if_error_ms=300_000, supports_etag=True,
        ),
        sim_latency_ms=120,
        pushable_filters=("state", "author", "repo_name", "created_at"),
    ),
    "jira": ConnectorConfig(
        name="jira",
        rate_limit=RateLimitPolicy(
            connector_rpm=_rpm(3000), tenant_rpm=_rpm(300), user_rpm=_rpm(15)),
        freshness=FreshnessPolicy(
            default_ttl_ms=60_000, min_freshness_ms=0,
            stale_if_error_ms=300_000, supports_etag=False,
        ),
        sim_latency_ms=160,
        pushable_filters=("project_key", "status", "assignee"),
    ),
    "linear": ConnectorConfig(
        name="linear",
        rate_limit=RateLimitPolicy(
            connector_rpm=_rpm(3000), tenant_rpm=_rpm(300), user_rpm=_rpm(15)),
        freshness=FreshnessPolicy(
            default_ttl_ms=60_000, min_freshness_ms=0,
            stale_if_error_ms=300_000, supports_etag=False,
        ),
        sim_latency_ms=140,
        pushable_filters=("state", "assignee", "team_key"),
    ),
    # Live connectors — real GitHub / Jira REST APIs.
    # Rate-limit budgets reflect real source quotas (GitHub: 5000/h authenticated;
    # Jira Cloud: variable, conservative default). TTL is longer than mocks to
    # protect outbound quota.
    "gh_live": ConnectorConfig(
        name="gh_live",
        rate_limit=RateLimitPolicy(
            connector_rpm=_rpm(80), tenant_rpm=_rpm(60), user_rpm=_rpm(15)),
        freshness=FreshnessPolicy(
            default_ttl_ms=300_000, min_freshness_ms=0,
            stale_if_error_ms=900_000, supports_etag=False,
        ),
        sim_latency_ms=0,
        pushable_filters=("state", "repo_name"),
    ),
    "jira_live": ConnectorConfig(
        name="jira_live",
        rate_limit=RateLimitPolicy(
            connector_rpm=_rpm(60), tenant_rpm=_rpm(40), user_rpm=_rpm(10)),
        freshness=FreshnessPolicy(
            default_ttl_ms=300_000, min_freshness_ms=0,
            stale_if_error_ms=900_000, supports_etag=False,
        ),
        sim_latency_ms=0,
        pushable_filters=("project_key", "status", "assignee", "id"),
    ),
}

# Tables exposed per connector, with the canonical column set (schema catalog).
SCHEMA_CATALOG: dict[str, dict[str, list[str]]] = {
    "github": {
        "pull_requests": [
            "id", "title", "state", "author", "author_email", "repo_name",
            "base_branch", "head_sha", "created_at", "updated_at",
        ],
    },
    "jira": {
        "issues": [
            "id", "title", "status", "assignee", "project_key",
            "linked_pr_id", "internal_notes", "created_at", "updated_at",
        ],
    },
    "linear": {
        "issues": [
            "id", "title", "state", "assignee", "team_key", "priority",
            "linked_pr_id", "created_at", "updated_at",
        ],
    },
    "gh_live": {
        "pull_requests": [
            "id", "title", "state", "author", "repo_name",
            "base_branch", "head_sha", "linked_issue_key",
            "created_at", "updated_at",
        ],
    },
    "jira_live": {
        "issues": [
            "id", "title", "status", "assignee", "project_key",
            "description", "created_at", "updated_at",
        ],
    },
}
