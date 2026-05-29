"""Audit log records executed queries and blocked access attempts."""


def test_executed_query_is_audited(client, eng_headers):
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT i.id, i.project_key FROM jira.issues i LIMIT 5"})
    assert r.status_code == 200

    events = client.get("/v1/audit").json()["events"]
    execs = [e for e in events if e["event_type"] == "query.execute"]
    assert execs, "expected a query.execute audit event"
    e = execs[-1]
    assert e["tables_accessed"] == ["jira.issues"]
    assert e["tenant_id"] == "acme-corp" and e["role"] == "eng"
    assert e["rows_returned"] == 5
    # eng has an RLS row filter on jira -> it must be recorded.
    assert any("project_key" in p for p in e["rls_predicates_applied"])
    assert "trace_id" in e and "timestamp" in e and "duration_ms" in e


def test_denied_access_is_audited(client, eng_headers):
    # Selecting a blocked column is denied AND recorded.
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT i.internal_notes FROM jira.issues i LIMIT 1"})
    assert r.status_code == 403

    events = client.get("/v1/audit").json()["events"]
    denials = [e for e in events if e["event_type"] == "query.denied"]
    assert denials and denials[-1]["error_code"] == "ENTITLEMENT_DENIED"
