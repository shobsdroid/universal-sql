"""Steady mixed traffic so the Grafana dashboard + Jaeger traces populate on
their own (used by the `loadgen` service in docker-compose).

Sends a rotating mix of single-source queries, a cross-app join, and repeated
queries (which become cache hits) as both the restricted `eng` role and `admin`.
Mints its own tokens, so it must share USQL_JWT_SECRET with the app.
"""
from __future__ import annotations

import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.auth import mint_token  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8099")
ENG = "Bearer " + mint_token("eng", "acme-corp")
ADMIN = "Bearer " + mint_token("admin", "acme-corp")

# (auth, body) — repeated bodies exercise the freshness cache (hits).
WORKLOAD = [
    (ENG, {"sql": "SELECT pr.id, pr.state FROM github.pull_requests pr "
                  "WHERE pr.state = 'open' LIMIT 10"}),
    (ADMIN, {"sql": "SELECT i.id, i.status FROM jira.issues i "
                    "WHERE i.project_key = 'ENG' LIMIT 10"}),
    (ADMIN, {"sql": "SELECT pr.id, issue.id FROM github.pull_requests pr "
                    "JOIN jira.issues issue ON pr.id = issue.linked_pr_id "
                    "WHERE pr.state = 'open' LIMIT 10"}),
    (ENG, {"sql": "SELECT pr.author, pr.author_email FROM github.pull_requests pr LIMIT 5"}),
    # force a live fetch occasionally so cache-hit vs live is visible
    (ADMIN, {"sql": "SELECT i.id FROM jira.issues i LIMIT 5", "max_staleness_ms": 0}),
]


def _wait_for_app(client: httpx.Client) -> None:
    for _ in range(120):
        try:
            if client.get(f"{BASE}/healthz").status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)


def main() -> None:
    with httpx.Client(timeout=10) as client:
        _wait_for_app(client)
        i = 0
        while True:
            auth, body = WORKLOAD[i % len(WORKLOAD)]
            try:
                client.post(f"{BASE}/v1/query", json=body, headers={"authorization": auth})
            except Exception:
                pass
            i += 1
            time.sleep(0.1)   # ~10 req/s, plenty to populate dashboards


if __name__ == "__main__":
    main()
