"""GitHub live connector — fetches real PRs from github.com via REST.

Auth: GITHUB_TOKEN (fine-grained or classic PAT).
Scope: GITHUB_REPOS (comma-separated owner/repo; one HTTP call per repo).

Schema mapping (GitHub field -> our column):
  number                            -> id
  title                             -> title
  state                             -> state         (open / closed)
  user.login                        -> author
  (passed in)                       -> repo_name
  base.ref                          -> base_branch
  head.sha                          -> head_sha
  created_at, updated_at            -> created_at, updated_at
  regex on title: r"[A-Z]+-\\d+"   -> linked_issue_key  (Smart-Commits-style)

Pushdown:
  state = X      -> ?state=X (open|closed|all)
  repo_name = X  -> only call that repo's endpoint
  other preds    -> post-filter (executor)

Pagination: follow Link rel="next" up to PAGE_LIMIT pages per repo (cap to
keep blast radius small even if a query is wide open).
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from ..errors import ConnectorAuthFailure
from ..plan import SourcePlan, matches_all
from ..settings import CONNECTORS
from .base import Connector, ConnectorContext, ConnectorManifest, FetchResult

ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+")
PAGE_LIMIT = 5            # safety: 5 * 100 = 500 PRs per repo per query
PAGE_SIZE = 100
TIMEOUT_S = 4.0           # leave headroom under the 5s per-source gateway deadline


def _map_pr(pr: dict, repo_name: str) -> dict:
    title = pr.get("title") or ""
    m = ISSUE_KEY_RE.search(title)
    return {
        "id": pr.get("number"),
        "title": title,
        "state": pr.get("state"),
        "author": (pr.get("user") or {}).get("login"),
        "repo_name": repo_name,
        "base_branch": (pr.get("base") or {}).get("ref"),
        "head_sha": (pr.get("head") or {}).get("sha"),
        "linked_issue_key": m.group(0) if m else None,
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
    }


def _pushdown(plan: SourcePlan) -> tuple[str | None, set[str]]:
    """Return (state_param, repo_filter) pulled out of plan.pushed_predicates.

    A predicate consumed here is still present in pushed_predicates but the
    matches_all() post-filter is idempotent (server side already applied it).
    """
    state = None
    repos: set[str] = set()
    for p in plan.pushed_predicates + plan.rls_predicates:
        if p.column == "state" and p.op == "=" and isinstance(p.value, str):
            state = p.value
        if p.column == "repo_name" and p.op == "=" and isinstance(p.value, str):
            repos.add(p.value)
        if p.column == "repo_name" and p.op == "IN" and isinstance(p.value, (list, tuple)):
            repos.update(v for v in p.value if isinstance(v, str))
    return state, repos


class GitHubLiveConnector(Connector):
    name = "gh_live"

    def __init__(self) -> None:
        token = os.environ.get("GITHUB_TOKEN")
        repos_env = os.environ.get("GITHUB_REPOS", "")
        if not token or not repos_env.strip():
            raise ConnectorAuthFailure(
                "GITHUB_TOKEN and GITHUB_REPOS env vars are required for gh_live")
        self._token = token
        self._repos = tuple(r.strip() for r in repos_env.split(",") if r.strip())
        self._cfg = CONNECTORS["gh_live"]

    def describe(self) -> ConnectorManifest:
        return ConnectorManifest(
            tables=["pull_requests"],
            pushable_filters=list(self._cfg.pushable_filters),
            max_page_size=PAGE_SIZE,
            supports_conditional_requests=self._cfg.freshness.supports_etag,
            version="0.1.0",
        )

    async def execute(self, plan: SourcePlan, ctx: ConnectorContext) -> FetchResult:
        state, repo_filter = _pushdown(plan)
        repos = tuple(r for r in self._repos if not repo_filter or r in repo_filter)

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Stop paging once we have enough rows to satisfy the plan's LIMIT. Without
        # this, a `LIMIT 5` query against a busy repo would fetch all 500 PRs
        # and trip the per-source deadline.
        target = max(plan.limit, 1) if plan.limit else PAGE_LIMIT * PAGE_SIZE

        rows: list[dict] = []
        async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
            for repo in repos:
                url: str | None = (
                    f"https://api.github.com/repos/{repo}/pulls"
                    f"?per_page={PAGE_SIZE}&state={state or 'open'}&sort=updated&direction=desc")
                pages = 0
                while url and pages < PAGE_LIMIT:
                    r = await client.get(url)
                    if r.status_code == 401 or r.status_code == 403:
                        raise ConnectorAuthFailure(
                            f"GitHub returned {r.status_code} for {repo} "
                            f"(check token scope / repo access)")
                    r.raise_for_status()
                    page = r.json()
                    rows.extend(_map_pr(pr, repo) for pr in page)
                    if len(rows) >= target:
                        break
                    url = _next_link(r.headers.get("Link", ""))
                    pages += 1
                if len(rows) >= target:
                    break

        # Post-filter: applies pushed_predicates we didn't translate + non-pushable preds.
        server_filters = plan.pushed_predicates + plan.rls_predicates
        rows = [row for row in rows if matches_all(row, server_filters)]
        return FetchResult(rows=rows, etag=None, fetched_at_ms=int(time.time() * 1000))


def _next_link(link_header: str) -> str | None:
    # Link header format: <url1>; rel="next", <url2>; rel="last"
    for part in link_header.split(","):
        bits = part.strip().split(";")
        if len(bits) < 2:
            continue
        url = bits[0].strip().strip("<>")
        rel = bits[1].strip()
        if rel == 'rel="next"':
            return url
    return None
