---
name: add-connector
description: Add a new federated SaaS connector (e.g. Linear, Slack, Salesforce, Stripe) to the Universal SQL Layer prototype. Use when the user asks to "add a connector", "wire up a new source", "support <SaaS product> in the SQL layer", or extend the federation beyond github/jira. Generates the connector class, registers it in settings + registry, declares the schema catalog, updates policy.yaml, drops in fixture data, and adds a smoke test.
---

# Add a new connector to the Universal SQL Layer

The architecture is connector-agnostic on the hot path — rate limiting, freshness, entitlements, audit, parallel fan-out, /docs all key off the connector's string name. To plug in a new source you touch exactly **6 places**. This skill drives that end-to-end.

## When to use

The user wants to extend the prototype past `github`/`jira` — typically phrased as:
- "Add a Linear / Slack / Salesforce / PagerDuty connector"
- "Wire up <SaaS> as a third source"
- "Let me query <product> alongside github and jira"

If they only want a real-API rewrite of an existing connector (replacing the mock with live HTTP), this skill is the wrong fit — point them at `src/connectors/<name>_mock.py` and the design doc's connector SDK section instead.

## Step 1 — Gather inputs

Use `AskUserQuestion` to collect (one question per field, allow custom via "Other"):

1. **Connector name** — lowercase, single word, used as the SQL namespace (e.g. `linear`). Validate it's not already in `src/settings.py` `CONNECTORS`.
2. **Table name(s)** — one or more, e.g. `issues`, `teams`. Most demos want one; multi-table is well-supported (see Step 2 multi-table skeleton). Suggest the most idiomatic name for the product.
3. **Columns — per table.** Ask one question *per table* if there are more than one. Always include `id` and (if a join target makes sense) a foreign-key column. Recommend including the columns needed to demo a join with `github.pull_requests` or `jira.issues`. **Track which table each column belongs to** — they must be wired separately into `SCHEMA_CATALOG`, `policy.yaml`, fixtures, and tests.
4. **Pushable filter columns** — *union* across tables is fine here (it's a per-connector capability, not per-table). Subset of columns the source's API can filter server-side. For mocks this is mostly cosmetic but it documents intent.
5. **Mock or real?** — default to mock (deterministic JSON fixture). Real API support is out of scope for this skill; if they want real OAuth/HTTP, scope that as a separate task and stop here.
6. **Demo RLS row filter — per table** (optional) — for the `eng` role. E.g. `team_key IN ('ENG', 'PLATFORM')`. Different tables in the same connector can have different filters or none. Skip if not needed.
7. **Demo CLS — per table** (optional) — masked or blocked columns for the `eng` role.

If the user has already provided all of this in their message, skip the questions and proceed.

## Step 2 — Generate the connector class

Create `src/connectors/<name>_mock.py` by mirroring `src/connectors/jira_mock.py` (the simpler of the two — no ETag). Use these substitutions, no others:

- Class name: `<Name>Connector` (PascalCase, e.g. `LinearConnector`).
- `name = "<name>"`.
- Manifest: `tables=[...]` lists *every* table this connector serves, `pushable_filters=list(self._cfg.pushable_filters)`, `max_page_size=100`, `supports_conditional_requests=self._cfg.supports_etag`.
- `execute()` filter logic identical to `jira_mock.py` — `server_filters = plan.pushed_predicates + plan.rls_predicates`, then `matches_all(r, server_filters)`. Do not add extra logic; the executor handles post_predicates, projection, join, masking.
- Env-var hooks for demo: `USQL_<NAME>_LATENCY_MS` and `USQL_<NAME>_FAIL`. Match the pattern exactly so existing partial-result / stale-if-error demos work.

### Single-table skeleton (most cases)

```python
_FIXTURE = Path(__file__).parent.parent / "fixtures" / "<name>_<table>.json"

class <Name>Connector(Connector):
    name = "<name>"

    def __init__(self) -> None:
        self._rows = json.loads(_FIXTURE.read_text())
        self._cfg = CONNECTORS["<name>"]

    def describe(self) -> ConnectorManifest:
        return ConnectorManifest(
            tables=["<table>"],
            pushable_filters=list(self._cfg.pushable_filters),
            max_page_size=100,
            supports_conditional_requests=self._cfg.supports_etag,
        )

    async def execute(self, plan, ctx):
        latency = int(os.environ.get("USQL_<NAME>_LATENCY_MS", self._cfg.sim_latency_ms))
        await asyncio.sleep(latency / 1000)
        if os.environ.get("USQL_<NAME>_FAIL") == "1":
            raise ConnectionError("simulated <name> outage")
        server_filters = plan.pushed_predicates + plan.rls_predicates
        rows = [r for r in self._rows if matches_all(r, server_filters)]
        return FetchResult(rows=rows, etag=None, fetched_at_ms=int(time.time() * 1000))
```

### Multi-table skeleton

Load one fixture per table into a dict and dispatch on `plan.table`. Raise `KeyError` for an unknown table — the planner should never let one through, so a hard failure here is a bug signal, not a recoverable case.

```python
_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
_TABLES = ("<table1>", "<table2>")   # ← keep in sync with describe() and SCHEMA_CATALOG

class <Name>Connector(Connector):
    name = "<name>"

    def __init__(self) -> None:
        self._rows = {
            t: json.loads((_FIXTURE_DIR / f"<name>_{t}.json").read_text())
            for t in _TABLES
        }
        self._cfg = CONNECTORS["<name>"]

    def describe(self) -> ConnectorManifest:
        return ConnectorManifest(
            tables=list(_TABLES),
            pushable_filters=list(self._cfg.pushable_filters),
            max_page_size=100,
            supports_conditional_requests=self._cfg.supports_etag,
        )

    async def execute(self, plan, ctx):
        latency = int(os.environ.get("USQL_<NAME>_LATENCY_MS", self._cfg.sim_latency_ms))
        await asyncio.sleep(latency / 1000)
        if os.environ.get("USQL_<NAME>_FAIL") == "1":
            raise ConnectionError("simulated <name> outage")
        table_rows = self._rows[plan.table]   # KeyError on unknown table = planner bug
        server_filters = plan.pushed_predicates + plan.rls_predicates
        rows = [r for r in table_rows if matches_all(r, server_filters)]
        return FetchResult(rows=rows, etag=None, fetched_at_ms=int(time.time() * 1000))
```

Three invariants the multi-table skeleton enforces — keep them in sync or you'll get silent drift:
1. `_TABLES` tuple, `describe().tables`, and `SCHEMA_CATALOG[<name>]` keys must list the same tables.
2. Every table in `_TABLES` must have a `<name>_<table>.json` fixture.
3. Every table must have a `policy.yaml` entry under each role (or accept that omitting it = `403` for that role).

## Step 3 — Register the connector

Edit `src/connectors/jira_mock.py`'s `get_registry()` at the bottom — this is the **only** module that wires the registry today. Add the import and the dict entry:

```python
def get_registry() -> dict:
    from .github_mock import GitHubConnector
    from .<name>_mock import <Name>Connector
    return {
        "github": GitHubConnector(),
        "jira": JiraConnector(),
        "<name>": <Name>Connector(),
    }
```

Do NOT refactor `get_registry()` into a discovery pattern — that's a separate cleanup, out of scope here.

## Step 4 — Declare config + schema in settings.py

Edit `src/settings.py`. Two dicts to extend, in this order:

**4a. `CONNECTORS`** — add a `ConnectorConfig` entry. Copy the `jira` block as the template:

```python
"<name>": ConnectorConfig(
    name="<name>",
    rate_limit=RateLimitPolicy(
        connector_rpm=_rpm(3000), tenant_rpm=_rpm(300), user_rpm=_rpm(15)),
    freshness=FreshnessPolicy(
        default_ttl_ms=60_000, min_freshness_ms=0,
        stale_if_error_ms=300_000, supports_etag=False,
    ),
    sim_latency_ms=140,
    pushable_filters=(<comma-separated quoted column names from step 1.4>),
),
```

Use the jira numbers unless the user has a reason to deviate. Real per-source tuning is a follow-up.

**4b. `SCHEMA_CATALOG`** — declare every table + every column:

```python
"<name>": {
    "<table>": [<comma-separated quoted column names>],
    ...
},
```

The planner validates against this catalog — any column not listed here will be rejected at plan time with `INVALID_SQL`. Get this exhaustive.

## Step 5 — Update policy.yaml

Edit `policy.yaml`. For **every** role (`eng`, `admin`, plus any others present):

- Add `<name>.<table>` to the role's `can_access` list.
- Under `tables:` add a `<name>.<table>: {}` entry (admin) or with `row_filter` / `masks` / `blocked` from step 1.6/1.7 (eng).

A missing role entry means `403 ENTITLEMENT_DENIED` for that role on the new table — usually the right default for unknown roles, but make sure admin always sees the new table unrestricted.

## Step 6 — Drop in fixture data

Create **one fixture file per table** at `src/fixtures/<name>_<table>.json` — an array of objects. **Every column declared in `SCHEMA_CATALOG[<name>][<table>]` must appear in every row of that table's fixture.** A few rules:

- At least 5 rows per table so RLS filters have something to drop.
- If this table joins to an existing one, pick FK values that exist in the other fixture. E.g. if you added `linked_pr_id`, the values should match `id`s in `src/fixtures/github_pull_requests.json`.
- For multi-table connectors, FK columns *between this connector's own tables* (e.g. `messages.channel_id` → `channels.id`) should also align so a same-connector self-join demos cleanly.
- Include rows that the demo RLS filter would drop, so the user can prove RLS works.
- Use realistic-looking IDs and titles; the prototype's value is partly in the readable demo output.

## Step 7 — Smoke test

Create `tests/test_<name>.py` — copy the style of `tests/test_entitlements.py`. **Minimum coverage per table**:

1. **Admin can SELECT from the table** and gets N rows (the full fixture).
2. **Eng sees the RLS-filtered subset** (if RLS was configured for that table) OR also gets N rows (if not).

Plus, **at least one test per connector**:

3. **Federated join** with `github.pull_requests` or `jira.issues` — that's the headline behaviour and the most valuable smoke check. Assert `sources` includes both connector names.
4. **RLS holds across the join** — pick one table that has an RLS filter and prove the dropped rows don't reappear via a join with a less-restricted source.

For multi-table connectors, add **one same-connector self-join test** (e.g. `messages JOIN channels ON messages.channel_id = channels.id`) so dispatch on `plan.table` is exercised for both tables in one query.

Tests use the existing `client`, `eng_headers`, `admin_headers` fixtures from `tests/conftest.py`. Don't introduce new fixtures.

## Step 8 — Verify

Run, in order:

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

All existing tests must still pass (they don't reference the new connector by name). The new tests must pass. If pytest fails:
- `INVALID_SQL` on an unknown column → `SCHEMA_CATALOG` is missing it.
- `ENTITLEMENT_DENIED` for admin → `policy.yaml` is missing the table from admin's `can_access`.
- `KeyError` on the connector name → it's not in the registry (step 3).

Then probe the live API to confirm it shows up in `/docs` and `/healthz`:

```bash
# If a server is already running:
curl -s http://localhost:8099/healthz | python3 -m json.tool   # should list the new connector
```

If the user already has the Docker stack up, rebuild only the `app` service:

```bash
PATH="$HOME/.orbstack/bin:$PATH" docker compose up -d --build app
```

## What NOT to do

- **Don't touch `src/main.py`, `src/executor.py`, `src/planner.py`, `src/entitlements.py`, `src/ratelimit.py`, `src/observability.py`.** A correctly-registered connector flows through all of them automatically. If you find yourself editing these, something is wrong with steps 1-6.
- **Don't add the connector to `src/connectors/__init__.py`.** Imports happen lazily inside `get_registry()`.
- **Don't add real HTTP / OAuth in this skill's scope.** That's `authenticate()` / `refreshToken()` + token storage + pagination, all real work the prototype doesn't ship. Scope it as a follow-up.
- **Don't change rate-limit defaults globally.** Per-source tuning is fine; touching `_RPM_MULT` or any other connector's budget is not.
- **Don't introduce `SELECT *`-special-casing.** The existing behaviour (blocked column → 403 even via `*`) is intentional and documented in the README.

## Output to the user

After everything passes, summarize:
1. Files created + edited (with paths).
2. The first query they can run against the new connector (an actual `curl`, with the token-mint command), plus the federated join with `github` or `jira` if one is set up.
3. Where to look in `/docs` (Swagger) for the new connector's endpoints.
4. Honest note on what's NOT in this connector vs. a real one: no OAuth, no pagination, no live API. Pointer to `design_doc.md` §6 if they want the full SDK.
