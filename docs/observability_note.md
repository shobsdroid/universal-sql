# Observability artifact — what it proves

Artifacts captured in this folder from a live run of the prototype:

- `trace_view.txt` — rendered Gantt of one cross-app query's trace + a metrics
  summary, produced by `python scripts/snapshot.py` (no Docker needed). **This is
  the screenshot artifact.**
- `trace_sample.json` — raw OpenTelemetry spans for one cross-app join query.
- `metrics_sample.txt` — a `/metrics` scrape after a handful of queries.

> A Grafana dashboard + Jaeger trace are also available via `docker compose up`
> (see README) if you prefer those images.

## Trace (`trace_sample.json`) — per-connector time & parallel fan-out

One `POST /v1/query` for `github.pull_requests JOIN jira.issues` produced this
span tree (times from the captured run):

```
query (federated_join)                 177 ms   rows_returned=7
├─ plan_and_entitle                     12 ms
├─ connector.execute:github            122 ms   rows_fetched=7   cache_hit=false
└─ connector.execute:jira              161 ms   rows_fetched=10  cache_hit=false
```

What it proves:
- **Per-connector latency is attributable** — the trace shows GitHub at 122 ms
  and Jira at 161 ms separately (the THP's "trace that shows connector time").
- **Fan-out is parallel, not serial** — both connector spans start within ~0.2 ms
  of each other, and the root span (177 ms) ≈ the *slower* connector, not the
  *sum* (283 ms). The connector executor fans out concurrently.
- Span attributes carry `connector`, `cache_hit`, `rows_fetched`, `plan_type`,
  so a backend like Tempo/Jaeger can slice latency by connector and cache state.

## Metrics (`metrics_sample.txt`) — cache payoff & error visibility

```
usql_connector_api_latency_seconds{connector="jira",cache_hit="false"}  ~160 ms avg
usql_connector_api_latency_seconds{connector="jira",cache_hit="true"}   ~0.027 ms
usql_query_errors_total{error_code="ENTITLEMENT_DENIED"}                1
```

What it proves:
- **The freshness cache earns its keep** — a Jira cache hit (~27 µs) is ~5000×
  faster than a live fetch (~160 ms). `cache_hit` is a metric label, so hit-ratio
  per connector is directly graphable.
- **Errors are first-class** — the blocked-column attempt incremented
  `usql_query_errors_total{error_code="ENTITLEMENT_DENIED"}`, giving an alertable
  signal for entitlement denials.
- `usql_query_duration_seconds` is labelled by `plan_type`, so single-source vs
  federated-join latency can be tracked against the P95 SLO independently.
