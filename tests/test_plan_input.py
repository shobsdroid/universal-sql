"""POST /v1/query accepts a pre-built `plan` JSON as an alternative to `sql`,
and entitlements still apply to it."""


def test_plan_json_single_source(client, admin_headers):
    r = client.post("/v1/query", headers=admin_headers, json={"plan": {
        "sources": [{
            "connector": "github", "table": "pull_requests", "alias": "pr",
            "select": ["id", "state"],
            "where": [{"column": "state", "op": "=", "value": "open"}],
        }],
        "limit": 50,
    }})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["id", "state"]
    assert body["rows"] and all(row[1] == "open" for row in body["rows"])


def test_plan_json_respects_entitlements(client, eng_headers):
    # Plan path must still apply RLS: eng never sees OPS Jira issues.
    r = client.post("/v1/query", headers=eng_headers, json={"plan": {
        "sources": [{"connector": "jira", "table": "issues", "alias": "i",
                     "select": ["id", "project_key"]}],
        "limit": 50,
    }})
    assert r.status_code == 200
    projects = {row[1] for row in r.json()["rows"]}
    assert "OPS" not in projects


def test_missing_sql_and_plan_is_400(client, admin_headers):
    r = client.post("/v1/query", headers=admin_headers, json={"max_staleness_ms": 1000})
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_SQL"
