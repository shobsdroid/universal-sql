"""Async overflow path: when a rate-limited query opts into async, it is
enqueued here and the client polls GET /v1/jobs/{id} (design doc §8.3).

Prototype uses an in-process asyncio task + dict store. Production would use
Kafka/SQS + a durable job table and optional push-notification webhook.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .connectors.base import ConnectorContext
from .executor import execute_plan
from .plan import QueryPlan
from .ratelimit import LIMITER

_MAX_WAIT_S = 60          # give up if budget never frees within a minute
_POLL_INTERVAL_S = 0.5


@dataclass
class Job:
    job_id: str
    trace_id: str
    tenant_id: str                  # owner — enforced on poll
    user_id: str
    status: str = "queued"          # queued | running | done | failed
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def enqueue(self, plan: QueryPlan, ctx: ConnectorContext, registry: dict,
                connectors: list[str], trace_id: str,
                max_staleness_ms: int | None) -> Job:
        job = Job(job_id=uuid.uuid4().hex, trace_id=trace_id,
                  tenant_id=ctx.tenant_id, user_id=ctx.user_id)
        self._jobs[job.job_id] = job
        asyncio.create_task(
            self._run(job, plan, ctx, registry, connectors, max_staleness_ms))
        return job

    async def _run(self, job: Job, plan, ctx, registry, connectors, max_staleness_ms):
        job.status = "running"
        deadline = time.time() + _MAX_WAIT_S
        try:
            # Wait for rate-limit budget to refill across all touched connectors.
            while True:
                if LIMITER.try_acquire_many(connectors, ctx.tenant_id, ctx.user_id).ok:
                    break
                if time.time() > deadline:
                    job.status = "failed"
                    job.error = "RATE_LIMIT_EXHAUSTED: budget did not free within window"
                    return
                await asyncio.sleep(_POLL_INTERVAL_S)

            result = await execute_plan(plan, ctx, registry, max_staleness_ms)
            job.result = {
                "columns": result.columns,
                "rows": result.rows,
                "freshness_ms": result.freshness_ms,
                "rate_limit_status": {c: "ok" for c in result.source_status},
                "sources": result.source_detail,
                "partial": result.partial,
                "annotations": result.annotations,
                "trace_id": job.trace_id,
            }
            job.status = "done"
        except Exception as e:  # noqa: BLE001 - surface any failure to the poller
            job.status = "failed"
            job.error = str(e)


STORE = JobStore()
