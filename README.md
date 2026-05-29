# Universal SQL Layer (prototype)

One `POST /v1/query` that federates SQL across SaaS APIs (GitHub PRs ↔ Jira
issues ↔ Linear issues, plus optional live GitHub/Jira). JWT auth, plan-time
RLS/CLS entitlements, per-connector rate limiting with async overflow, a
freshness cache, in-memory cross-app joins, and Prometheus + OTel
observability.

## Run

```bash
pip install -r requirements.txt
uvicorn src.main:app --port 8099
```

Mint a dev token and hit it:

```bash
TOKEN=$(python -m src.auth admin acme-corp)
curl -s localhost:8099/healthz
curl -s -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT id, state FROM github.pull_requests LIMIT 5"}' \
  localhost:8099/v1/query | jq
```

## Docker

```bash
docker compose up -d --build
```

Brings up the app + Prometheus + Grafana + Jaeger + a loadgen.

## Tests

```bash
pytest -q
```

See `design_doc.md` for architecture and `PLAN_OF_ACTION.md` for the
6-month roadmap.
