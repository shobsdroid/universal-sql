"""Audit log — an append-only record of every cross-system access (design §11.4).

The prototype emits a structured JSON line per event and keeps a bounded
in-process ring buffer (inspectable via GET /v1/audit). Production would append
to an immutable store (Postgres + S3 archival) with the same event shape.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from datetime import datetime, timezone

_LOG = logging.getLogger("usql.audit")
_RING: deque[dict] = deque(maxlen=1000)


def fingerprint(text: str) -> str:
    """Stable hash of the (normalized-ish) query for correlation without storing
    the raw SQL/plan in the audit trail."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def record(event: dict) -> None:
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    _RING.append(event)
    _LOG.info(json.dumps(event, default=str))


def recent(limit: int = 50, tenant_id: str | None = None) -> list[dict]:
    events = list(_RING)
    if tenant_id is not None:
        events = [e for e in events if e.get("tenant_id") == tenant_id]
    return events[-limit:]


def clear() -> None:
    _RING.clear()
