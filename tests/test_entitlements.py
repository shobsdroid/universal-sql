"""Entitlements: AuthN, RLS row filtering, CLS masking + column blocking."""


def test_missing_token_is_401(client):
    r = client.post("/v1/query", json={"sql": "SELECT pr.id FROM github.pull_requests pr LIMIT 1"})
    assert r.status_code == 401
    assert r.json()["error"] == "UNAUTHENTICATED"


def test_eng_rls_excludes_ops_project(client, eng_headers):
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT i.id, i.project_key FROM jira.issues i LIMIT 50"})
    assert r.status_code == 200
    projects = {row[1] for row in r.json()["rows"]}
    assert projects == {"ENG", "PLATFORM"}        # OPS filtered out at plan time


def test_eng_author_email_is_masked(client, eng_headers):
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT pr.author_email FROM github.pull_requests pr LIMIT 5"})
    assert r.status_code == 200
    emails = [row[0] for row in r.json()["rows"]]
    assert emails, "expected rows"
    assert all(e.startswith("***@") for e in emails)


def test_eng_cannot_select_blocked_column(client, eng_headers):
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT i.id, i.internal_notes FROM jira.issues i LIMIT 5"})
    assert r.status_code == 403
    assert r.json()["error"] == "ENTITLEMENT_DENIED"


def test_admin_sees_ops_and_unmasked(client, admin_headers):
    r = client.post("/v1/query", headers=admin_headers, json={
        "sql": "SELECT i.project_key, i.internal_notes FROM jira.issues i "
               "WHERE i.project_key = 'OPS' LIMIT 5"})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows and all(row[0] == "OPS" for row in rows)
    assert any(row[1] for row in rows)            # internal_notes visible to admin


def test_cross_app_join_enforces_rls(client, eng_headers):
    """RLS must hold across a join: eng never sees OPS issues even via JOIN."""
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT pr.id, issue.project_key FROM github.pull_requests pr "
               "JOIN jira.issues issue ON pr.id = issue.linked_pr_id LIMIT 50"})
    assert r.status_code == 200
    projects = {row[1] for row in r.json()["rows"]}
    assert "OPS" not in projects
