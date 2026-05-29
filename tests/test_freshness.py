"""Freshness cache behaviour + graceful partial results on a slow source."""

JIRA_Q = {"sql": "SELECT i.id FROM jira.issues i WHERE i.project_key = 'ENG' LIMIT 5"}


def _source(resp):
    return resp.json()["sources"][0]


def test_cache_miss_then_hit(client, admin_headers):
    r1 = client.post("/v1/query", headers=admin_headers, json=JIRA_Q)
    assert _source(r1)["cache_hit"] is False        # first call is live

    r2 = client.post("/v1/query", headers=admin_headers, json=JIRA_Q)
    assert _source(r2)["cache_hit"] is True          # served from cache


def test_max_staleness_zero_forces_live(client, admin_headers):
    client.post("/v1/query", headers=admin_headers, json=JIRA_Q)   # populate cache
    r = client.post("/v1/query", headers=admin_headers,
                    json={**JIRA_Q, "max_staleness_ms": 0})
    assert _source(r)["cache_hit"] is False          # staleness 0 -> always live


def test_slow_source_yields_partial(client, admin_headers, monkeypatch):
    # Make Jira slower than the per-source deadline -> graceful partial result.
    monkeypatch.setattr("src.executor.SOURCE_TIMEOUT_MS", 100)
    monkeypatch.setenv("USQL_JIRA_LATENCY_MS", "800")
    r = client.post("/v1/query", headers=admin_headers, json=JIRA_Q)
    assert r.status_code == 200                       # degraded, not failed
    body = r.json()
    assert body["partial"] is True
    assert "SOURCE_TIMEOUT" in body["annotations"]


def test_overall_request_timeout_degrades_to_partial(client, admin_headers, monkeypatch):
    # Per-source deadline high so it doesn't trip; overall request ceiling tiny.
    monkeypatch.setattr("src.executor.SOURCE_TIMEOUT_MS", 5000)
    monkeypatch.setattr("src.main.REQUEST_TIMEOUT_MS", 50)        # 50 ms ceiling
    monkeypatch.setenv("USQL_JIRA_LATENCY_MS", "800")            # slower than ceiling
    r = client.post("/v1/query", headers=admin_headers, json=JIRA_Q)
    assert r.status_code == 200
    body = r.json()
    assert body["partial"] is True
    assert "REQUEST_TIMEOUT" in body["annotations"]
