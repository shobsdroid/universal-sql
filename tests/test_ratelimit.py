"""Rate limiting: friendly 429 + Retry-After, and async overflow reroute."""
import time

SIMPLE = {"sql": "SELECT pr.id FROM github.pull_requests pr LIMIT 1"}


def _deplete(client, headers, n=40):
    last = None
    for _ in range(n):
        last = client.post("/v1/query", headers=headers, json=SIMPLE)
    return last


def test_rate_limit_returns_friendly_429(client, eng_headers):
    last = _deplete(client, eng_headers)
    assert last.status_code == 429
    body = last.json()
    assert body["error"] == "RATE_LIMIT_EXHAUSTED"
    assert body["retry_after_ms"] > 0
    assert body["async_available"] is True
    assert "Retry-After" in last.headers


def test_async_overflow_returns_202_and_completes(client, eng_headers, admin_headers):
    _deplete(client, eng_headers)
    r = client.post("/v1/query", headers=eng_headers,
                    json={**SIMPLE, "async_ok": True})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    # Job poll is owner-scoped: no token -> 401, another user -> 404.
    assert client.get(f"/v1/jobs/{job_id}").status_code == 401
    assert client.get(f"/v1/jobs/{job_id}", headers=admin_headers).status_code == 404

    # Poll (as the owner) until the queued job drains its rate-limit wait.
    deadline = time.time() + 10
    result = None
    while time.time() < deadline:
        jr = client.get(f"/v1/jobs/{job_id}", headers=eng_headers)
        data = jr.json()
        if "rows" in data:
            result = data
            break
        time.sleep(0.3)
    assert result is not None, "async job did not complete in time"
    assert result["columns"] == ["id"]


def test_multi_connector_acquire_is_atomic():
    """A join rejected on one connector must NOT consume the other's budget."""
    from src.ratelimit import LIMITER
    t, u = "acme-corp", "u1"

    # Baseline: how many github tokens are available when untouched.
    LIMITER.reset()
    before = 0
    while LIMITER.try_acquire("github", t, u).ok:
        before += 1

    # Fresh state; drain Jira completely, leave GitHub untouched.
    LIMITER.reset()
    while LIMITER.try_acquire("jira", t, u).ok:
        pass

    # A github+jira acquire must fail on jira and consume nothing from github.
    for _ in range(10):
        d = LIMITER.try_acquire_many(["github", "jira"], t, u)
        assert d.ok is False and d.connector == "jira"

    after = 0
    while LIMITER.try_acquire("github", t, u).ok:
        after += 1
    assert after >= before - 1     # github budget intact despite failed joins
