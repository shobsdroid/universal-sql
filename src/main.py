"""FastAPI entry point. Wires gateway -> planner -> entitlements -> rate-limit
-> executor and shapes the response.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from . import audit
from .auth import mint_token, verify_token
from .connectors.base import ConnectorContext
from .connectors.jira_mock import get_registry
from .entitlements import apply_entitlements
from .errors import EntitlementDenied, InvalidSQL, QueryError
from .executor import execute_plan
from .jobs import STORE
from .observability import (
    QUERY_DURATION,
    QUERY_ERRORS,
    RATE_LIMIT_EXHAUSTED,
    SPAN_COLLECTOR,
    tracer,
)
from .planner import plan_from_dict, plan_query
from .ratelimit import LIMITER
from .settings import REQUEST_TIMEOUT_MS

API_DESCRIPTION = """
Federates SQL across SaaS APIs (GitHub PRs ↔ Jira issues in this prototype) behind
a single `POST /v1/query`. Plan-time entitlements (RLS/CLS), per-connector rate
limiting with async overflow, a freshness cache, and cross-app federated joins.

### Auth
All `/v1/*` endpoints require a JWT in `Authorization: Bearer <token>`.
Mint a dev token locally:

```bash
python -m src.auth admin acme-corp     # unrestricted role
python -m src.auth eng   acme-corp     # restricted role (RLS + CLS apply)
```

Then click **Authorize** above and paste the token.

### Headline behaviours
- **RLS** — `eng` cannot see Jira `OPS` rows even via a join.
- **CLS** — `author_email` is masked for `eng`; `internal_notes` selection → 403.
- **Freshness** — `max_staleness_ms:0` forces a live fetch; otherwise served from cache.
- **Rate limit** — token-bucket per connector/tenant/user, friendly 429 + Retry-After,
  or 202 + job_id with `async_ok:true`.

### Error vocabulary
| Code | HTTP | Meaning |
|---|---|---|
| `UNAUTHENTICATED` | 401 | Missing/invalid JWT |
| `ENTITLEMENT_DENIED` | 403 | Table not allowed, or a blocked column was selected |
| `INVALID_SQL` | 400 | Parse error / unsupported SQL |
| `QUERY_TOO_COMPLEX` | 422 | e.g. more than one JOIN |
| `RATE_LIMIT_EXHAUSTED` | 429 | Token bucket empty; `Retry-After` + `async_available` |
"""

TAGS_METADATA = [
    {"name": "Query", "description": "Run SQL or a pre-built plan across federated connectors."},
    {"name": "Jobs", "description": "Poll async jobs created by the rate-limit async reroute."},
    {"name": "Audit", "description": "Append-only access trail. **Admin-only, tenant-scoped.**"},
    {"name": "Observability", "description": "Trace + Prometheus metrics exposure."},
    {"name": "Health", "description": "Liveness/readiness."},
]

app = FastAPI(
    title="Universal SQL Layer (prototype)",
    version="0.1.0",
    description=API_DESCRIPTION,
    openapi_tags=TAGS_METADATA,
    contact={"name": "Universal SQL prototype", "url": "https://github.com/"},
    license_info={"name": "Prototype — see repo"},
)

REGISTRY = get_registry()


SQL_EXAMPLE_JOIN = (
    "SELECT pr.id, pr.title, issue.id, issue.status "
    "FROM github.pull_requests pr "
    "JOIN jira.issues issue ON pr.id = issue.linked_pr_id "
    "WHERE pr.state = 'open' LIMIT 10"
)


class QueryRequest(BaseModel):
    """Provide exactly one of `sql` or `plan`."""

    sql: str | None = Field(
        default=None,
        description="A `SELECT` query. Supports columns or `*`, AND-only `WHERE` "
                    "(`= != < > <= >= IN`), one `INNER JOIN ... ON a.x = b.y`, "
                    "`ORDER BY`, `LIMIT`, `OFFSET`. `OR` / multi-JOIN rejected.",
        examples=[SQL_EXAMPLE_JOIN],
    )
    plan: dict | None = Field(
        default=None,
        description="Pre-built plan JSON (same entitlement + rate-limit + freshness "
                    "path as SQL). See examples in the README.",
    )
    max_staleness_ms: int | None = Field(
        default=None,
        description="Cap on how stale cached rows may be. `0` forces a live fetch.",
        examples=[0],
    )
    async_ok: bool = Field(
        default=False,
        description="On rate-limit, reroute to an async job (`202 + job_id`) "
                    "instead of returning `429`.",
    )


class SourceDetail(BaseModel):
    connector: str
    version: str | None = None
    cache_hit: bool | None = None
    freshness_ms: int | None = None
    status: str


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list]
    freshness_ms: int
    rate_limit_status: dict[str, str]
    sources: list[SourceDetail]
    partial: bool
    annotations: list[str]
    trace_id: str


class JobQueued(BaseModel):
    status: str = Field(..., examples=["queued"])
    job_id: str
    poll_url: str = Field(..., examples=["/v1/jobs/abc123"])
    retry_after_ms: int
    connector: str
    trace_id: str


class JobStatus(BaseModel):
    status: str = Field(..., examples=["pending", "done", "failed"])
    job_id: str
    trace_id: str


class ErrorBody(BaseModel):
    error: str = Field(..., examples=["ENTITLEMENT_DENIED"])
    message: str
    trace_id: str


class AuditEnvelope(BaseModel):
    events: list[dict]


class HealthzResponse(BaseModel):
    status: str = Field(..., examples=["ok"])
    connectors: list[str]


COMMON_ERROR_RESPONSES: dict = {
    401: {"model": ErrorBody, "description": "Missing/invalid/expired JWT."},
    403: {"model": ErrorBody, "description": "Table not allowed, or a blocked column was selected."},
}

QUERY_ERROR_RESPONSES: dict = {
    **COMMON_ERROR_RESPONSES,
    400: {"model": ErrorBody, "description": "Parse error or unsupported SQL."},
    422: {"model": ErrorBody, "description": "Query rejected as too complex (e.g. multi-JOIN)."},
    429: {"model": ErrorBody, "description": "Rate limit exhausted. Includes `Retry-After` header."},
    202: {"model": JobQueued, "description": "Async reroute accepted (only when `async_ok:true` and async is available)."},
}


@app.get(
    "/healthz",
    tags=["Health"],
    response_model=HealthzResponse,
    summary="Liveness probe",
    description="Returns `{status: ok, connectors: [...]}`. No auth required.",
)
async def healthz():
    return {"status": "ok", "connectors": list(REGISTRY.keys())}


# --- Dev token mint (opt-in) ----------------------------------------------
# Gated by USQL_DEV_TOKEN_ENDPOINT=1 so production deploys keep the dev JWT
# minter off the network. Used by the bundled query console (/ui).
class DevTokenRequest(BaseModel):
    role: str = Field(default="eng", examples=["eng", "admin"])
    tenant_id: str = Field(default="acme-corp")
    ttl_s: int = Field(default=3600, ge=60, le=24 * 3600)


@app.post(
    "/v1/dev/token",
    tags=["Health"],
    summary="Mint a dev JWT (gated by USQL_DEV_TOKEN_ENDPOINT)",
    description="Convenience endpoint for the bundled UI. Off by default; "
                "set `USQL_DEV_TOKEN_ENDPOINT=1` to enable. Returns 404 otherwise.",
)
async def dev_token(req: DevTokenRequest):
    if os.environ.get("USQL_DEV_TOKEN_ENDPOINT", "").lower() not in ("1", "true", "yes"):
        return JSONResponse(status_code=404,
                            content={"error": "DEV_TOKEN_DISABLED",
                                     "message": "Set USQL_DEV_TOKEN_ENDPOINT=1 to enable."})
    if req.role not in ("admin", "eng"):
        return JSONResponse(status_code=400,
                            content={"error": "INVALID_ROLE",
                                     "message": "role must be 'admin' or 'eng'"})
    token = mint_token(req.role, req.tenant_id, ttl_s=req.ttl_s)
    return {"token": token, "role": req.role, "tenant_id": req.tenant_id, "ttl_s": req.ttl_s}


@app.get(
    "/metrics",
    tags=["Observability"],
    summary="Prometheus metrics (text)",
    description="Prometheus text-format exposition: query duration histogram, "
                "per-connector latency, cache hit/miss, rate-limit rejections, "
                "query errors. No auth (network-restricted in prod).",
    response_class=Response,
)
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get(
    "/v1/audit",
    tags=["Audit"],
    response_model=AuditEnvelope,
    summary="Recent audit events",
    description="Append-only access trail: every query (including denials) records "
                "user/tenant/tables/columns/RLS predicates/rows/duration. "
                "**Admin-only** and **scoped to the caller's tenant.**",
    responses=COMMON_ERROR_RESPONSES,
)
async def audit_trail(limit: int = 50, authorization: str = Header(default=None)):
    # Audit/access trails are admin-only and scoped to the caller's tenant —
    # an audit endpoint must not itself leak other tenants' access events.
    identity = verify_token(authorization)
    if identity.role != "admin":
        raise EntitlementDenied("audit trail requires an admin role")
    return {"events": audit.recent(limit, tenant_id=identity.tenant_id)}


@app.get(
    "/v1/trace/latest",
    tags=["Observability"],
    summary="Latest in-process trace snapshot",
    description="Returns the most recent `query` span tree (Gantt-ready). Used by "
                "`scripts/snapshot.py`. **Admin-only** and **scoped to the caller's tenant.**",
    responses={**COMMON_ERROR_RESPONSES,
               404: {"model": ErrorBody, "description": "No recent trace for the caller's tenant."}},
)
async def latest_trace(authorization: str = Header(default=None)):
    identity = verify_token(authorization)
    if identity.role != "admin":
        raise EntitlementDenied("trace inspection requires an admin role")
    t = SPAN_COLLECTOR.latest_query_trace()
    if t is not None:
        root = next((s for s in t["spans"] if s["name"] == "query"), None)
        if root and root["attributes"].get("tenant_id") == identity.tenant_id:
            return t
    return JSONResponse(status_code=404,
                        content={"error": "NO_TRACE",
                                 "message": "no recent trace for your tenant"})


@app.get(
    "/v1/connectors",
    tags=["Observability"],
    summary="List registered connectors with versions and capabilities",
    description="Returns each registered connector's name, semver, declared tables, "
                "pushable filter columns, and whether it supports conditional "
                "requests (ETag/304). Used for admin onboarding visibility and "
                "for reproducing a query against a known connector version.",
)
async def list_connectors():
    out = []
    for name, conn in REGISTRY.items():
        m = conn.describe()
        out.append({
            "name": name,
            "version": m.version,
            "tables": m.tables,
            "pushable_filters": m.pushable_filters,
            "max_page_size": m.max_page_size,
            "supports_conditional_requests": m.supports_conditional_requests,
        })
    return {"connectors": out}


@app.exception_handler(QueryError)
async def query_error_handler(request: Request, exc: QueryError):
    trace_id = getattr(request.state, "trace_id", "")
    QUERY_ERRORS.labels(error_code=exc.code).inc()
    ident = getattr(request.state, "identity", None)
    started = getattr(request.state, "started", time.monotonic())
    # Audit blocked/failed access attempts too (design §11.4).
    audit.record({
        "event_type": "query.denied",
        "trace_id": trace_id,
        "error_code": exc.code,
        "user_id": getattr(ident, "user_id", None),
        "tenant_id": getattr(ident, "tenant_id", None),
        "role": getattr(ident, "role", None),
        "sql_fingerprint": getattr(request.state, "query_fp", None),
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    })
    return JSONResponse(status_code=exc.http_status, content=exc.to_body(trace_id))


@app.post(
    "/v1/query",
    tags=["Query"],
    response_model=QueryResponse,
    summary="Run a federated SQL query (or pre-built plan)",
    description="Plan → entitlements (RLS/CLS injected pre-fetch) → rate limit "
                "(per connector/tenant/user) → parallel connector execution → "
                "in-memory hash join (if applicable) → response with freshness, "
                "per-source detail, and `trace_id`. Every call is audited.",
    responses=QUERY_ERROR_RESPONSES,
)
async def query(req: QueryRequest, request: Request, authorization: str = Header(default=None)):
    with tracer.start_as_current_span("query") as root:
        # Use the real OTel trace ID so a client can look it up in Jaeger directly
        # at /trace/{trace_id} (instead of needing a tag search).
        trace_id = format(root.get_span_context().trace_id, "032x")
        started = time.monotonic()
        request.state.trace_id = trace_id
        request.state.started = started
        request.state.query_fp = audit.fingerprint(
            req.sql or json.dumps(req.plan or {}, sort_keys=True))
        root.set_attribute("trace_id", trace_id)

        # AuthN: verify JWT -> identity.
        identity = verify_token(authorization)
        request.state.identity = identity
        ctx = ConnectorContext(tenant_id=identity.tenant_id, user_id=identity.user_id)
        root.set_attribute("tenant_id", identity.tenant_id)
        root.set_attribute("role", identity.role)

        # Plan (no connector calls), then AuthZ + RLS/CLS injection at plan time.
        # Accept either a SQL string or a pre-built plan JSON.
        with tracer.start_as_current_span("plan_and_entitle"):
            if req.plan is not None:
                plan = plan_from_dict(req.plan)
            elif req.sql:
                plan = plan_query(req.sql)
            else:
                raise InvalidSQL("provide either 'sql' or 'plan'")
            apply_entitlements(plan, identity.role)
        root.set_attribute("plan_type", plan.plan_type)

        # Rate limit: atomically acquire across every connector the plan touches.
        # A rejected query consumes no budget from any connector.
        connectors = sorted({sp.connector for sp in plan.sources})
        decision = LIMITER.try_acquire_many(connectors, identity.tenant_id, identity.user_id)
        if not decision.ok:
            c = decision.connector
            RATE_LIMIT_EXHAUSTED.labels(connector=c, scope=decision.scope).inc()
            QUERY_DURATION.labels(
                tenant=identity.tenant_id, plan_type=plan.plan_type,
                status="rate_limited").observe(time.monotonic() - started)
            audit.record({
                "event_type": "query.rate_limited",
                "trace_id": trace_id, "user_id": identity.user_id,
                "tenant_id": identity.tenant_id, "role": identity.role,
                "sql_fingerprint": request.state.query_fp,
                "connector": c, "scope": decision.scope,
                "rerouted_async": bool(req.async_ok and decision.async_available),
            })
            if req.async_ok and decision.async_available:
                job = STORE.enqueue(plan, ctx, REGISTRY, connectors, trace_id,
                                    req.max_staleness_ms)
                return JSONResponse(status_code=202, content={
                    "status": "queued",
                    "job_id": job.job_id,
                    "poll_url": f"/v1/jobs/{job.job_id}",
                    "retry_after_ms": decision.retry_after_ms,
                    "connector": c,
                    "trace_id": trace_id,
                })
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(max(1, decision.retry_after_ms // 1000))},
                content={
                    "error": "RATE_LIMIT_EXHAUSTED",
                    "message": (f"{c} connector budget exhausted at {decision.scope} scope "
                                f"for tenant {identity.tenant_id}. Retry in "
                                f"~{decision.retry_after_ms} ms, or resubmit with "
                                f'"async_ok": true to queue it.'),
                    "connector": c,
                    "scope": decision.scope,
                    "retry_after_ms": decision.retry_after_ms,
                    "async_available": decision.async_available,
                    "trace_id": trace_id,
                })
        rl_status = {c: "ok" for c in connectors}

        # Enforce the overall request deadline as a backstop (per-source
        # deadlines normally trip first); a hard timeout degrades to partial.
        try:
            result = await asyncio.wait_for(
                execute_plan(plan, ctx, REGISTRY, req.max_staleness_ms),
                timeout=REQUEST_TIMEOUT_MS / 1000)
        except asyncio.TimeoutError:
            QUERY_DURATION.labels(
                tenant=identity.tenant_id, plan_type=plan.plan_type,
                status="request_timeout").observe(time.monotonic() - started)
            root.set_attribute("partial", True)
            return {
                "columns": [sc.label for sc in plan.select_columns],
                "rows": [],
                "freshness_ms": 0,
                "rate_limit_status": rl_status,
                "sources": [{"connector": c, "status": "SOURCE_TIMEOUT"} for c in connectors],
                "partial": True,
                "annotations": ["REQUEST_TIMEOUT"],
                "trace_id": trace_id,
            }

        root.set_attribute("rows_returned", len(result.rows))
        root.set_attribute("partial", result.partial)

        QUERY_DURATION.labels(
            tenant=identity.tenant_id, plan_type=plan.plan_type,
            status="partial" if result.partial else "ok").observe(time.monotonic() - started)

        audit.record({
            "event_type": "query.execute",
            "trace_id": trace_id,
            "user_id": identity.user_id,
            "tenant_id": identity.tenant_id,
            "role": identity.role,
            "sql_fingerprint": request.state.query_fp,
            "connectors_accessed": connectors,
            "connector_versions": {c: REGISTRY[c].describe().version for c in connectors},
            "tables_accessed": [f"{sp.connector}.{sp.table}" for sp in plan.sources],
            "columns_accessed": sorted({sc.column for sc in plan.select_columns}),
            "rows_returned": len(result.rows),
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "partial": result.partial,
            "rls_predicates_applied": [
                f"{p.column} {p.op} {p.value}"
                for sp in plan.sources for p in sp.rls_predicates],
            "masked_columns": {
                sp.table: list(sp.masked_columns)
                for sp in plan.sources if sp.masked_columns},
        })

        return {
            "columns": result.columns,
            "rows": result.rows,
            "freshness_ms": result.freshness_ms,
            "rate_limit_status": rl_status,
            "sources": result.source_detail,
            "partial": result.partial,
            "annotations": result.annotations,
            "trace_id": trace_id,
        }


@app.get(
    "/v1/jobs/{job_id}",
    tags=["Jobs"],
    summary="Poll an async query job",
    description="Owner-scoped: only the tenant+user that created the job can read it. "
                "Returns `404 JOB_NOT_FOUND` rather than `403` on mismatch so existence "
                "isn't leaked across tenants. Completed jobs return the full query response shape.",
    responses={**COMMON_ERROR_RESPONSES,
               200: {"model": JobStatus, "description": "Job status, or the completed `QueryResponse` once `status:done`."},
               404: {"model": ErrorBody, "description": "Job not found (or not owned by caller)."},
               502: {"model": ErrorBody, "description": "Job failed during execution."}},
)
async def get_job(job_id: str, authorization: str = Header(default=None)):
    identity = verify_token(authorization)
    job = STORE.get(job_id)
    # Owner-scoped: a job is only visible to the tenant+user that created it.
    # Return 404 (not 403) on mismatch so job existence isn't leaked cross-tenant.
    if job is None or job.tenant_id != identity.tenant_id or job.user_id != identity.user_id:
        return JSONResponse(status_code=404, content={"error": "JOB_NOT_FOUND", "job_id": job_id})
    if job.status == "done":
        return job.result
    if job.status == "failed":
        return JSONResponse(status_code=502, content={
            "status": "failed", "job_id": job_id, "error": job.error, "trace_id": job.trace_id})
    return {"status": job.status, "job_id": job_id, "trace_id": job.trace_id}


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=TAGS_METADATA,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "HS256 dev JWT. Mint with `python -m src.auth <role> <tenant>`.",
        }
    }
    # Apply Bearer auth to every authenticated route. /healthz, /metrics, and
    # the docs themselves are intentionally unauthenticated.
    public_paths = {"/healthz", "/metrics", "/docs", "/redoc", "/openapi.json",
                    "/v1/dev/token", "/"}
    for path, ops in schema["paths"].items():
        if path in public_paths:
            continue
        for op in ops.values():
            op["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


# --- Static UI ------------------------------------------------------------
# Bundled query console. Looks for ./web next to the repo root; absent in
# environments that don't ship the UI (mount is skipped silently).
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if _WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def _root_to_ui():
        return RedirectResponse(url="/ui/")
