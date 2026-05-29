// Universal SQL — query console. Vanilla JS, no build step.
// Talks to the same origin: /healthz, /v1/query, /v1/jobs/{id}, /v1/dev/token.

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "usql.token";

const el = {
  token: $("token"),
  mintRole: $("mint-role"),
  mintTenant: $("mint-tenant"),
  mintBtn: $("mint-btn"),
  mintHint: $("mint-hint"),
  sql: $("sql"),
  staleness: $("staleness"),
  asyncOk: $("async-ok"),
  runBtn: $("run-btn"),
  runStatus: $("run-status"),
  results: $("results"),
  resultsPlaceholder: $("results-placeholder"),
  meta: $("meta"),
  resultsWrap: $("results-wrap"),
  healthDot: $("health-dot"),
  healthText: $("health-text"),
  examples: $("examples"),
};

// Example library — keyed by which connectors must be registered for the chip
// to show. Mocks are always present; live chips render only when gh_live /
// jira_live are loaded (env vars present).
const EXAMPLES = [
  { label: "cross-app join", needs: ["github", "jira"],
    sql: "SELECT pr.id, pr.title, issue.id, issue.status\nFROM github.pull_requests pr\nJOIN jira.issues issue ON pr.id = issue.linked_pr_id\nWHERE pr.state = 'open' LIMIT 10" },
  { label: "github ⨝ linear", needs: ["github", "linear"],
    sql: "SELECT pr.id, pr.title, lin.id, lin.team_key, lin.state\nFROM github.pull_requests pr\nJOIN linear.issues lin ON pr.id = lin.linked_pr_id LIMIT 10" },
  { label: "RLS demo (eng)", needs: ["jira"],
    sql: "SELECT i.id, i.project_key FROM jira.issues i LIMIT 50" },
  { label: "CLS block (eng → 403)", needs: ["jira"],
    sql: "SELECT i.internal_notes FROM jira.issues i LIMIT 1" },
  { label: "CLS mask (eng)", needs: ["github"],
    sql: "SELECT pr.author, pr.author_email FROM github.pull_requests pr LIMIT 3" },
  // --- live ---
  { label: "live github PRs", needs: ["gh_live"], live: true, stalenessMs: 0,
    sql: "SELECT pr.id, pr.title, pr.state, pr.author\nFROM gh_live.pull_requests pr\nWHERE pr.state = 'open' LIMIT 10" },
  { label: "live jira issues", needs: ["jira_live"], live: true, stalenessMs: 0,
    sql: "SELECT j.id, j.status, j.assignee, j.title\nFROM jira_live.issues j\nWHERE j.project_key = 'KAFKA' LIMIT 10" },
  { label: "live cross-app (Kafka)", needs: ["gh_live", "jira_live"], live: true, stalenessMs: 0,
    sql: "SELECT pr.id, pr.title, j.id, j.status, j.assignee\nFROM gh_live.pull_requests pr\nJOIN jira_live.issues j ON pr.linked_issue_key = j.id\nWHERE j.project_key = 'KAFKA' LIMIT 10" },
];

// --- bootstrap ------------------------------------------------------------

el.token.value = localStorage.getItem(TOKEN_KEY) || "";
el.token.addEventListener("change", () => localStorage.setItem(TOKEN_KEY, el.token.value.trim()));

el.sql.value = "SELECT pr.id, pr.title, issue.id, issue.status\n"
  + "FROM github.pull_requests pr\n"
  + "JOIN jira.issues issue ON pr.id = issue.linked_pr_id\n"
  + "WHERE pr.state = 'open' LIMIT 10";

// Examples are rendered after /healthz tells us which connectors are loaded.

el.runBtn.addEventListener("click", runQuery);
el.sql.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); runQuery(); }
});
el.mintBtn.addEventListener("click", mintToken);

checkHealth();

// --- health ---------------------------------------------------------------

async function checkHealth() {
  try {
    const r = await fetch("/healthz");
    const j = await r.json();
    const connectors = j.connectors || [];
    el.healthDot.classList.add("ok");
    el.healthText.textContent = `ok · ${connectors.join(", ") || "0 connectors"}`;
    renderExamples(connectors);
  } catch (e) {
    el.healthDot.classList.add("err");
    el.healthText.textContent = "unreachable";
    renderExamples([]);
  }
}

function renderExamples(connectors) {
  const have = new Set(connectors);
  const chips = EXAMPLES.filter((ex) => ex.needs.every((n) => have.has(n)));
  // Wipe everything after the leading "Examples:" label.
  el.examples.querySelectorAll(".example").forEach((n) => n.remove());
  chips.forEach((ex) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "example" + (ex.live ? " live" : "");
    b.textContent = ex.label;
    b.addEventListener("click", () => {
      el.sql.value = ex.sql;
      if (ex.stalenessMs != null) el.staleness.value = ex.stalenessMs;
    });
    el.examples.appendChild(b);
  });
  if (chips.length === 0) {
    const span = document.createElement("span");
    span.className = "muted small";
    span.textContent = "(no connectors registered)";
    el.examples.appendChild(span);
  }
}

// --- token mint -----------------------------------------------------------

async function mintToken() {
  el.mintHint.textContent = "";
  try {
    const r = await fetch("/v1/dev/token", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ role: el.mintRole.value, tenant_id: el.mintTenant.value || "acme-corp" }),
    });
    if (r.status === 404) {
      el.mintHint.innerHTML = `mint disabled on this server. Set <code>USQL_DEV_TOKEN_ENDPOINT=1</code> and restart, or paste a token from <code>python -m src.auth ${el.mintRole.value} ${el.mintTenant.value}</code>`;
      return;
    }
    if (!r.ok) {
      const body = await r.text();
      el.mintHint.textContent = `mint failed: ${r.status} ${body.slice(0, 120)}`;
      return;
    }
    const j = await r.json();
    el.token.value = j.token;
    localStorage.setItem(TOKEN_KEY, j.token);
    el.mintHint.textContent = `minted ${j.role} for ${j.tenant_id}`;
  } catch (e) {
    el.mintHint.textContent = `mint error: ${e.message}`;
  }
}

// --- run query ------------------------------------------------------------

async function runQuery() {
  const token = el.token.value.trim();
  if (!token) { showBanner("err", "Paste or mint a JWT first."); return; }
  const sql = el.sql.value.trim();
  if (!sql) { showBanner("err", "Empty SQL."); return; }

  const body = { sql };
  const s = el.staleness.value;
  if (s !== "") body.max_staleness_ms = parseInt(s, 10);
  if (el.asyncOk.checked) body.async_ok = true;

  el.runBtn.disabled = true;
  el.runStatus.textContent = "running…";
  clearResults();
  const t0 = performance.now();

  try {
    const r = await fetch("/v1/query", {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    });
    const elapsed = Math.round(performance.now() - t0);
    const j = await r.json().catch(() => ({}));

    if (r.status === 202) { return handleAsync(j, token, elapsed); }
    if (r.status === 429) { return handleRateLimit(j, sql, body, elapsed); }
    if (!r.ok) { return showError(r.status, j, elapsed); }

    renderResult(j, elapsed);
  } catch (e) {
    showBanner("err", `network error: ${e.message}`);
  } finally {
    el.runBtn.disabled = false;
    el.runStatus.textContent = "";
  }
}

async function handleAsync(j, token, elapsed) {
  showBanner("info", `Queued as job <span class="code">${j.job_id}</span>. Polling…`);
  el.runStatus.textContent = "polling job…";
  const t0 = performance.now();
  for (let i = 0; i < 60; i++) {
    await sleep(500);
    const r = await fetch(j.poll_url, { headers: { authorization: `Bearer ${token}` } });
    const body = await r.json().catch(() => ({}));
    if (r.status === 200 && body.columns) {
      const total = Math.round(performance.now() - t0) + elapsed;
      renderResult(body, total);
      return;
    }
    if (r.status === 502) {
      showError(502, body, Math.round(performance.now() - t0) + elapsed);
      return;
    }
  }
  showBanner("warn", "Job poll timed out after 30s.");
}

function handleRateLimit(j, sql, body, elapsed) {
  const reroute = j.async_available
    ? `<button class="reroute" id="reroute-btn">Retry with async_ok</button>`
    : `<span class="reroute muted small">(async not available)</span>`;
  showBanner("warn",
    `<span class="code">RATE_LIMIT_EXHAUSTED</span> — ${escapeHtml(j.connector || "")} (${escapeHtml(j.scope || "")}). `
    + `Retry in ~${j.retry_after_ms || "?"} ms. ${reroute}`);
  const btn = document.getElementById("reroute-btn");
  if (btn) {
    btn.addEventListener("click", () => {
      el.asyncOk.checked = true;
      runQuery();
    });
  }
  renderMetaError(j, elapsed);
}

function showError(status, j, elapsed) {
  const code = j.error || `HTTP_${status}`;
  const msg = j.message || JSON.stringify(j).slice(0, 240);
  showBanner("err", `<span class="code">${escapeHtml(code)}</span> — ${escapeHtml(msg)}`);
  renderMetaError(j, elapsed);
}

// --- rendering ------------------------------------------------------------

function clearResults() {
  el.results.classList.add("hidden");
  el.results.querySelector("thead").innerHTML = "";
  el.results.querySelector("tbody").innerHTML = "";
  el.meta.classList.add("hidden");
  el.meta.innerHTML = "";
  el.resultsPlaceholder.classList.add("hidden");
  // remove banners
  document.querySelectorAll(".banner").forEach((b) => b.remove());
}

function showBanner(kind, html) {
  const b = document.createElement("div");
  b.className = `banner ${kind}`;
  b.innerHTML = html;
  el.resultsWrap.prepend(b);
}

function renderResult(j, elapsed) {
  // Table
  const thead = el.results.querySelector("thead");
  const tbody = el.results.querySelector("tbody");
  const cols = j.columns || [];
  thead.innerHTML = `<tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr>`;
  const rows = j.rows || [];
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${cols.length || 1}" class="muted" style="text-align:center;padding:24px;">(no rows)</td></tr>`;
  } else {
    tbody.innerHTML = rows.map((row) =>
      `<tr>${row.map((v) => `<td>${escapeHtml(formatCell(v))}</td>`).join("")}</tr>`
    ).join("");
  }
  el.results.classList.remove("hidden");

  // Meta panel
  renderMeta(j, elapsed, rows.length);

  if (j.partial) {
    showBanner("warn", `Partial results — annotations: ${(j.annotations || []).join(", ") || "(none)"}`);
  }
}

function renderMeta(j, elapsed, rowCount) {
  const sources = (j.sources || []).map((s) => {
    const status = s.status || "?";
    const klass = status === "ok" ? "ok" : (status === "STALE_DATA" ? "warn" : "err");
    const hit = s.cache_hit === true ? "HIT" : (s.cache_hit === false ? "MISS" : "—");
    return `<div class="source">
      <div><span class="name">${escapeHtml(s.connector)}</span> <span class="v ${klass}">${escapeHtml(status)}</span></div>
      <div class="sub">cache: ${hit} · freshness: ${s.freshness_ms ?? "—"} ms${s.version ? " · v" + escapeHtml(s.version) : ""}</div>
    </div>`;
  }).join("");

  const rls = j.rate_limit_status || {};
  const rlChips = Object.entries(rls).map(([c, st]) =>
    `<span class="v ${st === "ok" ? "ok" : "warn"}">${escapeHtml(c)}:${escapeHtml(st)}</span>`
  ).join(" ");

  el.meta.innerHTML = `
    <h3>Response</h3>
    <div class="row"><span class="k">rows</span><span class="v">${rowCount}</span></div>
    <div class="row"><span class="k">client elapsed</span><span class="v">${elapsed} ms</span></div>
    <div class="row"><span class="k">server freshness</span><span class="v">${j.freshness_ms ?? "—"} ms</span></div>
    <div class="row"><span class="k">partial</span><span class="v ${j.partial ? "warn" : "ok"}">${j.partial ? "true" : "false"}</span></div>
    <div class="row"><span class="k">trace_id</span><span class="v" title="${escapeHtml(j.trace_id || "")}">${shortId(j.trace_id)}</span></div>
    <div class="row"><span class="k">rate limit</span><span class="v">${rlChips || "—"}</span></div>
    <h3 style="margin-top:12px;">Sources</h3>
    ${sources || '<span class="muted small">(no sources)</span>'}
    ${j.annotations && j.annotations.length ? `<h3 style="margin-top:12px;">Annotations</h3>
      <div class="v">${j.annotations.map(escapeHtml).join(", ")}</div>` : ""}
  `;
  el.meta.classList.remove("hidden");
}

function renderMetaError(j, elapsed) {
  el.meta.innerHTML = `
    <h3>Error</h3>
    <div class="row"><span class="k">code</span><span class="v err">${escapeHtml(j.error || "?")}</span></div>
    <div class="row"><span class="k">client elapsed</span><span class="v">${elapsed} ms</span></div>
    ${j.trace_id ? `<div class="row"><span class="k">trace_id</span><span class="v" title="${escapeHtml(j.trace_id)}">${shortId(j.trace_id)}</span></div>` : ""}
    ${j.connector ? `<div class="row"><span class="k">connector</span><span class="v">${escapeHtml(j.connector)}</span></div>` : ""}
    ${j.retry_after_ms != null ? `<div class="row"><span class="k">retry_after_ms</span><span class="v warn">${j.retry_after_ms}</span></div>` : ""}
  `;
  el.meta.classList.remove("hidden");
}

// --- utils ----------------------------------------------------------------

function shortId(s) { return s ? (s.slice(0, 8) + "…") : "—"; }
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function formatCell(v) {
  if (v == null) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return v;
}
