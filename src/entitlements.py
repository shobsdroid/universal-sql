"""Entitlement service: merges role policy into the query plan at plan time.

AuthZ (allow/deny on tables) + RLS (row filters compiled to predicates) + CLS
(column masks and blocks) are injected into each SourcePlan BEFORE the connector
executor runs, so a connector never fetches rows or columns the user may not see.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

from .errors import EntitlementDenied
from .plan import Predicate, QueryPlan

_POLICY_PATH = Path(__file__).parent.parent / "policy.yaml"


# --- Column mask functions (referenced by name from policy.yaml) -----------
def mask_email(value):
    if not isinstance(value, str) or "@" not in value:
        return "***"
    return "***@" + value.split("@", 1)[1]


MASKS = {"mask_email": mask_email}


@lru_cache(maxsize=1)
def load_policy() -> dict:
    with open(_POLICY_PATH) as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=256)
def _compile_filter(expr_str: str) -> tuple[Predicate, ...]:
    """Compile a policy row_filter (SQL boolean expr) into Predicate objects.
    Cached: policy filters are a small fixed set and the result is read-only."""
    from .planner import _flatten_and, _parse_predicate  # reuse parsing logic
    tree = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {expr_str}", read="postgres")
    where = tree.args.get("where")
    preds = []
    for cond in _flatten_and(where.this):
        _alias, pred = _parse_predicate(cond)
        preds.append(pred)
    return tuple(preds)


def apply_entitlements(plan: QueryPlan, role: str) -> None:
    """Mutates plan in place: authz check + RLS/CLS injection."""
    policy = load_policy()
    role_policy = policy.get("roles", {}).get(role)
    if role_policy is None:
        raise EntitlementDenied(f"role '{role}' has no entitlement policy")

    can_access = set(role_policy.get("can_access", []))
    tables_policy = role_policy.get("tables", {}) or {}

    by_alias = {sp.alias: sp for sp in plan.sources}

    for sp in plan.sources:
        fqtn = f"{sp.connector}.{sp.table}"
        if fqtn not in can_access:
            raise EntitlementDenied(
                f"role '{role}' is not entitled to access {fqtn}")
        tp = tables_policy.get(fqtn) or {}

        row_filter = tp.get("row_filter")
        if row_filter:
            sp.rls_predicates.extend(_compile_filter(row_filter))

        masks = tp.get("masks") or {}
        for col, fn in masks.items():
            if fn not in MASKS:
                raise EntitlementDenied(f"unknown mask function '{fn}'")
        sp.masked_columns = dict(masks)

        sp.blocked_columns = list(tp.get("blocked") or [])

    # CLS: explicitly selecting a blocked column is denied (least-privilege).
    for sc in plan.select_columns:
        sp = by_alias[sc.alias]
        if sc.column in sp.blocked_columns:
            raise EntitlementDenied(
                f"role '{role}' may not select blocked column "
                f"'{sp.connector}.{sp.table}.{sc.column}'")
