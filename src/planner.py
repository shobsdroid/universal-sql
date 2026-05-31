"""Query planner: SQL -> execution plan.

Supports the documented subset: SELECT (projection or *), FROM with one or two
sources, optional INNER JOIN ... ON, WHERE (AND of simple comparisons + IN),
ORDER BY, LIMIT. Splits predicates into connector-pushable vs executor post-filter
using each connector's capability manifest.
"""
from __future__ import annotations

from functools import lru_cache

import sqlglot
from sqlglot import exp

from .errors import InvalidSQL, QueryTooComplex
from .plan import JoinSpec, Predicate, QueryPlan, SelectCol, SourcePlan
from .settings import CONNECTORS, MAX_ROWS, SCHEMA_CATALOG


@lru_cache(maxsize=1024)
def _parse_cached(sql: str) -> exp.Expression:
    """Parse once per distinct SQL string. The AST is read-only; plan objects
    are rebuilt per call, so sharing the cached AST across requests is safe."""
    return sqlglot.parse_one(sql, read="postgres")

_OP_MAP = {
    exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">",
    exp.LT: "<", exp.GTE: ">=", exp.LTE: "<=",
}


def _literal_value(node: exp.Expression):
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        # numeric literal
        text = node.this
        return int(text) if text.isdigit() else float(text)
    if isinstance(node, exp.Boolean):
        return node.this
    if isinstance(node, exp.Null):
        return None
    raise InvalidSQL(f"unsupported literal: {node.sql()}")


def _resolve_table(table: exp.Table) -> tuple[str, str, str]:
    """Return (connector, table_name, alias)."""
    connector = table.db
    name = table.name
    if not connector:
        raise InvalidSQL(
            f"table '{name}' must be qualified as <connector>.<table> (e.g. github.pull_requests)")
    if connector not in CONNECTORS:
        raise InvalidSQL(f"unknown connector '{connector}'")
    if name not in SCHEMA_CATALOG.get(connector, {}):
        raise InvalidSQL(f"unknown table '{connector}.{name}'")
    alias = table.alias or name
    return connector, name, alias


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _flatten_and(node.left) + _flatten_and(node.right)
    if isinstance(node, exp.Paren):
        return _flatten_and(node.this)
    return [node]


def _parse_predicate(cond: exp.Expression) -> tuple[str, Predicate]:
    """Return (table_alias, Predicate) for a single comparison or IN."""
    if isinstance(cond, exp.In):
        col = cond.this
        if not isinstance(col, exp.Column):
            raise InvalidSQL(f"unsupported IN predicate: {cond.sql()}")
        values = [_literal_value(e) for e in cond.expressions]
        return col.table, Predicate(col.name, "IN", values)

    op = _OP_MAP.get(type(cond))
    if op is None:
        raise InvalidSQL(f"unsupported WHERE expression: {cond.sql()}")
    left, right = cond.left, cond.right
    if isinstance(left, exp.Column) and not isinstance(right, exp.Column):
        return left.table, Predicate(left.name, op, _literal_value(right))
    raise InvalidSQL(f"unsupported comparison (expected column OP literal): {cond.sql()}")


def plan_query(sql: str) -> QueryPlan:
    try:
        tree = _parse_cached(sql)
    except Exception as e:  # sqlglot raises various parse errors
        raise InvalidSQL(f"could not parse SQL: {e}")

    if not isinstance(tree, exp.Select):
        raise InvalidSQL("only SELECT statements are supported")

    # --- FROM + JOIN -> sources keyed by alias --------------------------
    from_expr = tree.args.get("from")
    if not from_expr:
        raise InvalidSQL("missing FROM clause")
    base_table = from_expr.this
    if not isinstance(base_table, exp.Table):
        raise InvalidSQL("FROM must reference a table")

    sources: dict[str, SourcePlan] = {}
    alias_order: list[str] = []

    def add_source(table: exp.Table) -> str:
        connector, name, alias = _resolve_table(table)
        sources[alias] = SourcePlan(
            connector=connector, table=name, alias=alias, limit=MAX_ROWS)
        alias_order.append(alias)
        return alias

    add_source(base_table)

    joins = tree.args.get("joins") or []
    if len(joins) > 1:
        raise QueryTooComplex("at most one JOIN is supported")

    join_spec = None
    for j in joins:
        jtable = j.this
        if not isinstance(jtable, exp.Table):
            raise InvalidSQL("JOIN must reference a table")
        right_alias = add_source(jtable)
        on = j.args.get("on")
        if not isinstance(on, exp.EQ) or not isinstance(on.left, exp.Column) \
                or not isinstance(on.right, exp.Column):
            raise InvalidSQL("JOIN ON must be: <alias>.<col> = <alias>.<col>")
        join_spec = JoinSpec(
            left_alias=on.left.table, left_column=on.left.name,
            right_alias=on.right.table, right_column=on.right.name)
        # Ensure the right alias is the joined table.
        _ = right_alias

    # --- WHERE -> per-source pushable / post predicates -----------------
    where = tree.args.get("where")
    if where:
        for cond in _flatten_and(where.this):
            alias, pred = _parse_predicate(cond)
            if alias and alias not in sources:
                raise InvalidSQL(f"unknown table alias '{alias}' in WHERE")
            target_alias = alias or alias_order[0]
            sp = sources[target_alias]
            pushable = CONNECTORS[sp.connector].pushable_filters
            if pred.column in pushable:
                sp.pushed_predicates.append(pred)
            else:
                sp.post_predicates.append(pred)

    # --- SELECT list ----------------------------------------------------
    select_cols: list[SelectCol] = []
    for proj in tree.expressions:
        target = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(target, exp.Star):
            if join_spec:
                raise InvalidSQL("SELECT * is not supported with JOIN; list columns explicitly")
            a = alias_order[0]
            sp = sources[a]
            for col in SCHEMA_CATALOG[sp.connector][sp.table]:
                select_cols.append(SelectCol(alias=a, column=col, label=col))
            continue
        if not isinstance(target, exp.Column):
            raise InvalidSQL(f"unsupported SELECT expression: {proj.sql()}")
        alias = target.table or alias_order[0]
        if alias not in sources:
            raise InvalidSQL(f"unknown table alias '{alias}' in SELECT")
        label = proj.alias_or_name if isinstance(proj, exp.Alias) else (
            f"{target.table}.{target.name}" if target.table and join_spec else target.name)
        select_cols.append(SelectCol(alias=alias, column=target.name, label=label))

    if not select_cols:
        raise InvalidSQL("no projectable columns in SELECT")

    # Record projected columns per source (used for CLS + connector hint).
    for sc in select_cols:
        sp = sources[sc.alias]
        if sc.column not in sp.projected_columns:
            sp.projected_columns.append(sc.column)

    # --- ORDER BY / LIMIT ----------------------------------------------
    order_by = None
    order = tree.args.get("order")
    if order:
        if len(order.expressions) != 1:
            raise QueryTooComplex("only single-column ORDER BY is supported")
        ordered = order.expressions[0]
        ocol = ordered.this
        if not isinstance(ocol, exp.Column):
            raise InvalidSQL("ORDER BY must reference a column")
        order_by = (f"{ocol.table}.{ocol.name}" if ocol.table and join_spec else ocol.name,
                    bool(ordered.args.get("desc")))

    limit = MAX_ROWS
    limit_node = tree.args.get("limit")
    if limit_node is not None:
        limit = int(_literal_value(limit_node.expression))
        limit = min(limit, MAX_ROWS)

    offset = 0
    offset_node = tree.args.get("offset")
    if offset_node is not None:
        offset = int(_literal_value(offset_node.expression))
        if offset < 0:
            raise InvalidSQL("OFFSET must be non-negative")

    # Sources must fetch enough rows to satisfy offset+limit before slicing.
    for sp in sources.values():
        sp.limit = min(MAX_ROWS, offset + limit)

    return QueryPlan(
        sources=list(sources.values()),
        select_columns=select_cols,
        join=join_spec,
        order_by=order_by,
        limit=limit,
        offset=offset,
    )


def plan_from_dict(spec: dict) -> QueryPlan:
    """Build a QueryPlan directly from a JSON plan (the `plan` alternative to
    `sql` on POST /v1/query). Same validation + pushdown split as the SQL path.

    Shape:
      {"sources": [{"connector","table","alias?","select":[...],
                    "where":[{"column","op","value"}]}],
       "join": {"left_alias","left_column","right_alias","right_column"}?,
       "order_by": ["col", desc_bool]?, "limit": int?, "offset": int?}
    """
    raw_sources = spec.get("sources") or []
    if not raw_sources:
        raise InvalidSQL("plan must include at least one source")
    if len(raw_sources) > 2:
        raise QueryTooComplex("plan supports at most two sources")

    sources: dict[str, SourcePlan] = {}
    alias_order: list[str] = []
    for s in raw_sources:
        connector = s.get("connector")
        table = s.get("table")
        if connector not in CONNECTORS:
            raise InvalidSQL(f"unknown connector '{connector}'")
        if table not in SCHEMA_CATALOG.get(connector, {}):
            raise InvalidSQL(f"unknown table '{connector}.{table}'")
        alias = s.get("alias") or table
        sp = SourcePlan(connector=connector, table=table, alias=alias)
        pushable = CONNECTORS[connector].pushable_filters
        for w in s.get("where", []):
            try:
                column, op, value = w["column"], w["op"], w["value"]
            except (KeyError, TypeError):
                raise InvalidSQL("plan where entries require column/op/value")
            if op not in _OP_MAP.values() and op != "IN":
                raise InvalidSQL(f"unsupported op '{op}' in plan")
            pred = Predicate(column, op, value)
            (sp.pushed_predicates if pred.column in pushable
             else sp.post_predicates).append(pred)
        sources[alias] = sp
        alias_order.append(alias)

    join_spec = None
    j = spec.get("join")
    if j:
        try:
            join_spec = JoinSpec(j["left_alias"], j["left_column"],
                                 j["right_alias"], j["right_column"])
        except (KeyError, TypeError):
            raise InvalidSQL("plan join requires left_alias/left_column/right_alias/right_column")

    select_cols: list[SelectCol] = []
    for s in raw_sources:
        alias = s.get("alias") or s["table"]
        cols = s.get("select") or SCHEMA_CATALOG[s["connector"]][s["table"]]
        for col in cols:
            label = f"{alias}.{col}" if join_spec else col
            select_cols.append(SelectCol(alias=alias, column=col, label=label))
    if not select_cols:
        raise InvalidSQL("plan selects no columns")
    for sc in select_cols:
        sp = sources[sc.alias]
        if sc.column not in sp.projected_columns:
            sp.projected_columns.append(sc.column)

    limit = min(int(spec.get("limit", MAX_ROWS)), MAX_ROWS)
    offset = int(spec.get("offset", 0))
    if offset < 0:
        raise InvalidSQL("offset must be non-negative")
    for sp in sources.values():
        sp.limit = min(MAX_ROWS, offset + limit)

    order_by = None
    if spec.get("order_by"):
        col, desc = spec["order_by"][0], bool(spec["order_by"][1])
        order_by = (col, desc)

    return QueryPlan(sources=list(sources.values()), select_columns=select_cols,
                     join=join_spec, order_by=order_by, limit=limit, offset=offset)
