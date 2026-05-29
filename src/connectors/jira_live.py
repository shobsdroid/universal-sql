"""Jira live connector — fetches real issues via REST + JQL.

Auth (one of):
  - Jira Cloud:  JIRA_BASE_URL + JIRA_EMAIL + JIRA_API_TOKEN  (HTTP Basic). Uses API v3.
  - Public Jira: JIRA_BASE_URL only (no auth). Set JIRA_API_VERSION=2 for Server.
Scope: JIRA_PROJECTS (comma-separated; default project filter applied to JQL).

Public instances known to work anonymously:
  https://issues.apache.org/jira       (projects: KAFKA, SPARK, FLINK, ...)
  https://jira.atlassian.com           (projects: JRACLOUD, CONFCLOUD, ...)
Both are API v2 — set JIRA_API_VERSION=2.

Schema mapping (Jira field -> our column):
  key                       -> id           (e.g. "ENG-123")
  fields.summary            -> title
  fields.status.name        -> status
  fields.assignee.displayName-> assignee
  fields.project.key        -> project_key
  fields.description (text) -> description  (truncated; markdown ADF flattened)
  fields.created, updated   -> created_at, updated_at

Pushdown to JQL:
  project_key, status, assignee, id  -> JQL clauses
  IN supported; '=' supported
  Falls back to post-filter for anything not translatable.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Any

import httpx

from ..errors import ConnectorAuthFailure
from ..plan import SourcePlan, matches_all
from ..settings import CONNECTORS
from .base import Connector, ConnectorContext, ConnectorManifest, FetchResult

PAGE_LIMIT = 5
PAGE_SIZE = 100
TIMEOUT_S = 4.0           # leave headroom under the 5s per-source gateway deadline

# columns -> JQL field name (some are renamed; most are 1:1)
JQL_FIELD = {
    "id": "key",
    "project_key": "project",
    "status": "status",
    "assignee": "assignee",
}


def _quote(v: Any) -> str:
    if isinstance(v, str):
        return '"' + v.replace('"', '\\"') + '"'
    return str(v)


def _build_jql(plan: SourcePlan, default_projects: tuple[str, ...]) -> str:
    clauses: list[str] = []
    seen_project = False
    for p in plan.pushed_predicates + plan.rls_predicates:
        jql_field = JQL_FIELD.get(p.column)
        if not jql_field:
            continue   # post-filter only
        if p.column == "project_key":
            seen_project = True
        if p.op == "=":
            clauses.append(f"{jql_field} = {_quote(p.value)}")
        elif p.op == "IN" and isinstance(p.value, (list, tuple)):
            quoted = ", ".join(_quote(v) for v in p.value)
            clauses.append(f"{jql_field} in ({quoted})")
        # other ops not translated; executor post-filters
    if not seen_project and default_projects:
        quoted = ", ".join(_quote(p) for p in default_projects)
        clauses.append(f"project in ({quoted})")
    return " AND ".join(clauses) if clauses else "ORDER BY updated DESC"


def _flatten_description(adf: Any) -> str | None:
    """Jira Cloud returns description as Atlassian Document Format (ADF) JSON.

    For demo purposes pull the plain text out; truncate to keep responses small.
    """
    if adf is None:
        return None
    if isinstance(adf, str):
        return adf[:500]
    out: list[str] = []
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and node.get("text"):
                out.append(node["text"])
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(adf)
    return (" ".join(out))[:500] or None


def _map_issue(issue: dict) -> dict:
    fields = issue.get("fields") or {}
    assignee = fields.get("assignee") or {}
    project = fields.get("project") or {}
    status = fields.get("status") or {}
    return {
        "id": issue.get("key"),
        "title": fields.get("summary"),
        "status": status.get("name"),
        "assignee": assignee.get("displayName"),
        "project_key": project.get("key"),
        "description": _flatten_description(fields.get("description")),
        "created_at": fields.get("created"),
        "updated_at": fields.get("updated"),
    }


class JiraLiveConnector(Connector):
    name = "jira_live"

    def __init__(self) -> None:
        base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        if not base:
            raise ConnectorAuthFailure("JIRA_BASE_URL is required for jira_live")
        self._base = base
        # API v3 = Jira Cloud (auth required). v2 = Jira Server / public instances.
        self._api_version = os.environ.get("JIRA_API_VERSION", "3")
        email = os.environ.get("JIRA_EMAIL")
        token = os.environ.get("JIRA_API_TOKEN")
        self._auth: str | None = None
        if email and token:
            self._auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        elif self._api_version == "3":
            # Cloud (v3) won't work without auth — fail loudly so the misconfig is obvious.
            raise ConnectorAuthFailure(
                "Jira Cloud (API v3) requires JIRA_EMAIL + JIRA_API_TOKEN. "
                "For public Server instances set JIRA_API_VERSION=2 and omit auth.")
        self._projects = tuple(
            p.strip() for p in os.environ.get("JIRA_PROJECTS", "").split(",") if p.strip())
        self._cfg = CONNECTORS["jira_live"]

    def describe(self) -> ConnectorManifest:
        return ConnectorManifest(
            tables=["issues"],
            pushable_filters=list(self._cfg.pushable_filters),
            max_page_size=PAGE_SIZE,
            supports_conditional_requests=self._cfg.freshness.supports_etag,
            version="0.1.0",
        )

    async def execute(self, plan: SourcePlan, ctx: ConnectorContext) -> FetchResult:
        jql = _build_jql(plan, self._projects)
        # Stop paging once we have enough rows for the plan's LIMIT — protects the
        # per-source deadline on heavy projects (KAFKA has 19k+ issues).
        target = max(plan.limit, 1) if plan.limit else PAGE_LIMIT * PAGE_SIZE
        headers = {"Accept": "application/json"}
        if self._auth:
            headers["Authorization"] = self._auth
        rows: list[dict] = []
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
            start_at = 0
            pages = 0
            while pages < PAGE_LIMIT:
                params = {
                    "jql": jql,
                    "maxResults": min(PAGE_SIZE, max(target - len(rows), 1)),
                    "startAt": start_at,
                    "fields": "summary,status,assignee,project,description,created,updated",
                }
                r = await client.get(
                    f"{self._base}/rest/api/{self._api_version}/search", params=params)
                if r.status_code in (401, 403):
                    raise ConnectorAuthFailure(
                        f"Jira returned {r.status_code} (check email/token + scope)")
                r.raise_for_status()
                payload = r.json()
                issues = payload.get("issues") or []
                rows.extend(_map_issue(i) for i in issues)
                total = payload.get("total", 0)
                start_at += len(issues)
                pages += 1
                if start_at >= total or not issues or len(rows) >= target:
                    break

        server_filters = plan.pushed_predicates + plan.rls_predicates
        rows = [row for row in rows if matches_all(row, server_filters)]
        return FetchResult(rows=rows, etag=None, fetched_at_ms=int(time.time() * 1000))
