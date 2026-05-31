"""Audit log records executed queries and blocked attempts, and the audit
endpoint is itself admin-only + tenant-scoped."""


def test_executed_query_is_audited(client, eng_headers, admin_headers):
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT i.id, i.project_key FROM jira.issues i LIMIT 5"})
    assert r.status_code == 200

    events = client.get("/v1/audit", headers=admin_headers).json()["events"]
    execs = [e for e in events if e["event_type"] == "query.execute"]
    assert execs, "expected a query.execute audit event"
    e = execs[-1]
    assert e["tables_accessed"] == ["jira.issues"]
    assert e["tenant_id"] == "acme-corp" and e["role"] == "eng"
    assert e["rows_returned"] == 5
    assert any("project_key" in p for p in e["rls_predicates_applied"])
    assert "trace_id" in e and "timestamp" in e and "duration_ms" in e


def test_denied_access_is_audited(client, eng_headers, admin_headers):
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT i.internal_notes FROM jira.issues i LIMIT 1"})
    assert r.status_code == 403

    events = client.get("/v1/audit", headers=admin_headers).json()["events"]
    denials = [e for e in events if e["event_type"] == "query.denied"]
    assert denials and denials[-1]["error_code"] == "ENTITLEMENT_DENIED"


def test_audit_endpoint_requires_auth(client):
    assert client.get("/v1/audit").status_code == 401


def test_audit_endpoint_requires_admin(client, eng_headers):
    r = client.get("/v1/audit", headers=eng_headers)
    assert r.status_code == 403
    assert r.json()["error"] == "ENTITLEMENT_DENIED"


def test_trace_endpoint_requires_admin(client, eng_headers):
    assert client.get("/v1/trace/latest").status_code == 401
    assert client.get("/v1/trace/latest", headers=eng_headers).status_code == 403
