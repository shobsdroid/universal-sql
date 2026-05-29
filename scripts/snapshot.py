"""No-Docker observability snapshot — renders a screenshot-ready view of one
cross-app query's trace (per-connector timing + parallel fan-out) plus a compact
metrics summary, using only the running app (no Grafana/Jaeger needed).

Usage:
    # terminal 1
    uvicorn src.main:app --port 8099
    # terminal 2
    python scripts/snapshot.py
    # then screenshot the output (also saved to docs/trace_view.txt)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prometheus_client.parser import text_string_to_metric_families  # noqa: E402

from src.auth import mint_token  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://localhost:8099")
DOCS = Path(__file__).parent.parent / "docs"

JOIN_SQL = ("SELECT pr.id, pr.title, issue.id, issue.status "
            "FROM github.pull_requests pr "
            "JOIN jira.issues issue ON pr.id = issue.linked_pr_id "
            "WHERE pr.state = 'open' LIMIT 10")


def _post(path, body, token):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def _get(path, token=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(BASE + path, headers=headers)
    with urllib.request.urlopen(req) as r:
        return r.read().decode()


def render_gantt(trace: dict) -> str:
    spans = trace["spans"]
    root = next(s for s in spans if s["name"] == "query")
    t0, t1 = root["start_ns"], root["end_ns"]
    total = (t1 - t0) / 1e6 or 1.0
    W = 46
    lines = [
        f"TRACE  {trace['trace_id'][:16]}   root=query   total={total:6.1f} ms",
        f"       {'span':<26}{'dur':>9}   0{'-' * (W - len(str(int(total))) - 4)}{int(total)}ms",
    ]
    for s in sorted(spans, key=lambda x: x["start_ns"]):
        off = (s["start_ns"] - t0) / 1e6
        dur = (s["end_ns"] - s["start_ns"]) / 1e6
        start_col = min(W - 1, int(off / total * W))
        bar_len = max(1, min(W - start_col, int(round(dur / total * W))))
        bar = " " * start_col + "#" * bar_len
        a = s["attributes"]
        if "connector" in a:
            extra = f"  {a['connector']:<7} rows={a.get('rows_fetched')} cache_hit={a.get('cache_hit')}"
        elif s["name"] == "query":
            extra = f"  plan={a.get('plan_type')} rows={a.get('rows_returned')} role={a.get('role')}"
        else:
            extra = ""
        lines.append(f"       {s['name']:<26}{dur:>7.1f}ms  |{bar:<{W}}|{extra}")
    lines.append("")
    lines.append("       (github and jira spans start together and overlap -> parallel fan-out;")
    lines.append("        root total ~= the slower connector, not the sum)")
    return "\n".join(lines)


def render_metrics(text: str) -> str:
    families = {f.name: f for f in text_string_to_metric_families(text)}

    def samples(name):
        f = families.get(name)
        return f.samples if f else []

    # throughput by plan_type
    q_count = {s.labels.get("plan_type", "?"): s.value
               for s in samples("usql_query_duration_seconds")
               if s.name.endswith("_count")}
    total_q = sum(q_count.values())

    # cache hit ratio + per-connector avg latency
    c_count, c_sum = {}, {}
    for s in samples("usql_connector_api_latency_seconds"):
        key = (s.labels.get("connector"), s.labels.get("cache_hit"))
        if s.name.endswith("_count"):
            c_count[key] = s.value
        elif s.name.endswith("_sum"):
            c_sum[key] = s.value
    hits = sum(v for (c, h), v in c_count.items() if h == "true")
    miss = sum(v for (c, h), v in c_count.items() if h == "false")
    hit_ratio = hits / (hits + miss) * 100 if (hits + miss) else 0.0

    per_conn = {}
    for (c, h), n in c_count.items():
        per_conn.setdefault(c, [0.0, 0.0])
        per_conn[c][0] += n
        per_conn[c][1] += c_sum.get((c, h), 0.0)

    errors = sum(s.value for s in samples("usql_query_errors_total"))
    rl = sum(s.value for s in samples("usql_rate_limit_exhausted_total"))

    lines = ["METRICS  (from /metrics)",
             f"       queries total        : {int(total_q)}   " +
             "  ".join(f"{k}={int(v)}" for k, v in q_count.items()),
             f"       connector cache-hit  : {hit_ratio:5.1f}%   (hit={int(hits)} live={int(miss)})"]
    for c, (n, tot) in sorted(per_conn.items()):
        avg = (tot / n * 1000) if n else 0
        lines.append(f"       {c:<7} avg fetch    : {avg:6.1f} ms over {int(n)} fetches")
    lines.append(f"       query errors         : {int(errors)}")
    lines.append(f"       rate-limit rejections: {int(rl)}")
    return "\n".join(lines)


def main() -> None:
    eng = mint_token("eng", "acme-corp")
    admin = mint_token("admin", "acme-corp")

    # Warm a couple of single-source queries + a cache hit so metrics are interesting.
    _post("/v1/query", {"sql": "SELECT i.id FROM jira.issues i WHERE i.project_key='ENG' LIMIT 5"}, admin)
    _post("/v1/query", {"sql": "SELECT i.id FROM jira.issues i WHERE i.project_key='ENG' LIMIT 5"}, admin)  # hit
    _post("/v1/query", {"sql": "SELECT pr.author_email FROM github.pull_requests pr LIMIT 3"}, eng)
    # The trace we render: a forced-live join so connector timing is real.
    resp = _post("/v1/query", {"sql": JOIN_SQL, "max_staleness_ms": 0}, admin)

    trace = json.loads(_get("/v1/trace/latest", admin))   # admin-only endpoint
    metrics_text = _get("/metrics")

    sep = "=" * 78
    out = "\n".join([
        sep,
        "Universal SQL Layer - observability snapshot",
        sep,
        "",
        render_gantt(trace),
        "",
        render_metrics(metrics_text),
        "",
        f"response: rows={len(resp['rows'])} freshness_ms={resp['freshness_ms']} "
        f"partial={resp['partial']} trace_id={resp['trace_id']}",
        sep,
    ]) + "\n"
    print(out)

    DOCS.mkdir(exist_ok=True)
    (DOCS / "trace_view.txt").write_text(out)
    (DOCS / "metrics_sample.txt").write_text(
        "\n".join(l for l in metrics_text.splitlines()
                  if l.startswith("usql_") and "_bucket" not in l and "_created" not in l) + "\n")
    print(f"saved {DOCS / 'trace_view.txt'} and {DOCS / 'metrics_sample.txt'}")


if __name__ == "__main__":
    main()
