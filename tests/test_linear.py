"""Linear connector: basic access, RLS for eng, and a federated join with github."""


def test_admin_sees_all_linear_issues(client, admin_headers):
    r = client.post("/v1/query", headers=admin_headers, json={
        "sql": "SELECT i.id, i.team_key FROM linear.issues i LIMIT 50"})
    assert r.status_code == 200
    teams = {row[1] for row in r.json()["rows"]}
    assert teams == {"ENG", "PLATFORM", "OPS"}        # admin sees every team


def test_eng_rls_excludes_ops_team(client, eng_headers):
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT i.id, i.team_key FROM linear.issues i LIMIT 50"})
    assert r.status_code == 200
    teams = {row[1] for row in r.json()["rows"]}
    assert teams == {"ENG", "PLATFORM"}               # OPS filtered out at plan time


def test_federated_join_github_linear(client, admin_headers):
    """Cross-app join: PRs ↔ Linear issues on linked_pr_id."""
    r = client.post("/v1/query", headers=admin_headers, json={
        "sql": "SELECT pr.id, pr.title, issue.id, issue.team_key "
               "FROM github.pull_requests pr "
               "JOIN linear.issues issue ON pr.id = issue.linked_pr_id LIMIT 50"})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) >= 5
    # Both connectors must appear in the per-source detail.
    sources = {s["connector"] for s in r.json()["sources"]}
    assert sources == {"github", "linear"}


def test_eng_join_drops_ops_linear_rows(client, eng_headers):
    """RLS holds across a federated join (linear OPS team must not appear)."""
    r = client.post("/v1/query", headers=eng_headers, json={
        "sql": "SELECT pr.id, issue.team_key FROM github.pull_requests pr "
               "JOIN linear.issues issue ON pr.id = issue.linked_pr_id LIMIT 50"})
    assert r.status_code == 200
    teams = {row[1] for row in r.json()["rows"]}
    assert "OPS" not in teams
