"""Standard error vocabulary shared across the system (design doc §15)."""
from __future__ import annotations


class QueryError(Exception):
    """Base for all query errors that map to a structured HTTP response."""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_body(self, trace_id: str) -> dict:
        return {"error": self.code, "message": self.message, "trace_id": trace_id, **self.extra}


class Unauthenticated(QueryError):
    code = "UNAUTHENTICATED"
    http_status = 401


class InvalidSQL(QueryError):
    code = "INVALID_SQL"
    http_status = 400


class EntitlementDenied(QueryError):
    code = "ENTITLEMENT_DENIED"
    http_status = 403


class RateLimitExhausted(QueryError):
    code = "RATE_LIMIT_EXHAUSTED"
    http_status = 429


class ConnectorAuthFailure(QueryError):
    code = "CONNECTOR_AUTH_FAILURE"
    http_status = 502


class QueryTooComplex(QueryError):
    code = "QUERY_TOO_COMPLEX"
    http_status = 422


class SchemaDrift(QueryError):
    code = "SCHEMA_DRIFT"
    http_status = 422


# Codes that are annotations on an otherwise-200 response rather than failures.
SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
STALE_DATA = "STALE_DATA"
PARTIAL_RESULT = "PARTIAL_RESULT"
