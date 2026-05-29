"""Execution-plan data model + predicate evaluation.

A QueryPlan is what the planner produces and the executor consumes. RLS/CLS
from the entitlement service is baked into each SourcePlan *before* any
connector is called, so a connector worker never fetches rows it may not return.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_OPS = {"=", "!=", ">", "<", ">=", "<=", "IN"}


@dataclass
class Predicate:
    column: str
    op: str
    value: Any

    def matches(self, row: dict) -> bool:
        if self.column not in row:
            return False
        cell = row[self.column]
        op = self.op
        if op == "IN":
            return cell in self.value
        if op == "=":
            return cell == self.value
        if op == "!=":
            return cell != self.value
        # Ordered comparisons; fall back to string compare for ISO timestamps.
        try:
            if op == ">":
                return cell > self.value
            if op == "<":
                return cell < self.value
            if op == ">=":
                return cell >= self.value
            if op == "<=":
                return cell <= self.value
        except TypeError:
            return False
        return False


def matches_all(row: dict, predicates: list[Predicate]) -> bool:
    return all(p.matches(row) for p in predicates)


@dataclass
class SourcePlan:
    connector: str
    table: str
    alias: str
    # Predicates the connector can satisfy server-side (API query params).
    pushed_predicates: list[Predicate] = field(default_factory=list)
    # Predicates the executor applies after fetch.
    post_predicates: list[Predicate] = field(default_factory=list)
    # Columns to return to the client (after CLS blocking).
    projected_columns: list[str] = field(default_factory=list)
    # RLS row filter injected at plan time (entitlement service).
    rls_predicates: list[Predicate] = field(default_factory=list)
    # CLS: columns to drop entirely / columns to mask {col: mask_fn_name}.
    blocked_columns: list[str] = field(default_factory=list)
    masked_columns: dict[str, str] = field(default_factory=dict)
    limit: int = 1000

    @property
    def fingerprint(self) -> str:
        """Cache key component: identifies the source-level fetch shape."""
        parts = [self.connector, self.table]
        for p in sorted(self.pushed_predicates + self.rls_predicates,
                        key=lambda x: (x.column, x.op, str(x.value))):
            parts.append(f"{p.column}{p.op}{p.value}")
        return "|".join(parts)


@dataclass
class SelectCol:
    alias: str        # table alias the column belongs to
    column: str       # source column name
    label: str        # output column label (e.g. "pr.title" or "title")


@dataclass
class JoinSpec:
    left_alias: str
    left_column: str
    right_alias: str
    right_column: str


@dataclass
class QueryPlan:
    sources: list[SourcePlan]
    select_columns: list[SelectCol]      # output columns in order
    join: JoinSpec | None = None
    order_by: tuple[str, bool] | None = None   # (column, descending)
    limit: int = 1000
    offset: int = 0
    freshness_hint_ms: int | None = None

    @property
    def plan_type(self) -> str:
        if self.join:
            return "federated_join"
        return "single_source"
