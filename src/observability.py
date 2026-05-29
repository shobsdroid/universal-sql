"""Observability: Prometheus metrics + OpenTelemetry traces (design doc §13).

Traces use a console exporter so spans print to the server log — enough to show
the per-connector time breakdown the THP asks for. In production this would be
an OTLP exporter to a collector (Tempo/Jaeger) and a real Prometheus scrape.
"""
from __future__ import annotations

import os
from collections import OrderedDict

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from prometheus_client import Counter, Histogram

# --- Prometheus metrics ----------------------------------------------------
QUERY_DURATION = Histogram(
    "usql_query_duration_seconds",
    "End-to-end query duration",
    ["tenant", "plan_type", "status"],
)
CONNECTOR_LATENCY = Histogram(
    "usql_connector_api_latency_seconds",
    "Per-connector fetch latency",
    ["connector", "cache_hit"],
)
RATE_LIMIT_EXHAUSTED = Counter(
    "usql_rate_limit_exhausted_total",
    "Requests rejected by the rate limiter",
    ["connector", "scope"],
)
QUERY_ERRORS = Counter(
    "usql_query_errors_total",
    "Queries that returned an error",
    ["error_code"],
)

# --- In-memory span collector ----------------------------------------------
# Keeps recent finished spans grouped by trace so we can render a trace WITHOUT
# an external backend (Jaeger). Powers GET /v1/trace/latest + scripts/snapshot.py.
class InMemorySpanCollector(SpanProcessor):
    def __init__(self, max_traces: int = 50) -> None:
        self._traces: "OrderedDict[str, list[dict]]" = OrderedDict()
        self._max = max_traces

    def on_start(self, span, parent_context=None):  # noqa: D401
        return

    def on_end(self, span):
        tid = format(span.context.trace_id, "032x")
        self._traces.setdefault(tid, []).append({
            "name": span.name,
            "span_id": format(span.context.span_id, "016x"),
            "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
            "start_ns": span.start_time,
            "end_ns": span.end_time,
            "attributes": {k: v for k, v in (span.attributes or {}).items()},
        })
        self._traces.move_to_end(tid)
        while len(self._traces) > self._max:
            self._traces.popitem(last=False)

    def shutdown(self):
        return

    def force_flush(self, timeout_millis: int = 30000):
        return True

    def latest_query_trace(self) -> dict | None:
        for tid in reversed(self._traces):
            spans = self._traces[tid]
            if any(s["name"] == "query" for s in spans):
                return {"trace_id": tid, "spans": spans}
        return None


# --- Tracing ---------------------------------------------------------------
# Exporter selection:
#   OTEL_EXPORTER_OTLP_ENDPOINT set -> batch-export to an OTLP collector/Jaeger
#   else USQL_TRACE_CONSOLE != 0   -> print spans to stdout (local demo)
#   else                            -> spans created but not exported (load tests)
_provider = TracerProvider(resource=Resource.create({"service.name": "universal-sql"}))
_otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if _otlp_endpoint:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    _provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=_otlp_endpoint.rstrip("/") + "/v1/traces")))
elif os.environ.get("USQL_TRACE_CONSOLE", "1") != "0":
    _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

# Always collect spans in-process for the local trace view (cheap, bounded).
SPAN_COLLECTOR = InMemorySpanCollector()
_provider.add_span_processor(SPAN_COLLECTOR)

trace.set_tracer_provider(_provider)
tracer = trace.get_tracer("universal-sql")
