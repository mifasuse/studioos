"""Self-contained HTML dashboard, served by /dashboard.

No template engine, no build step, no JS framework — vanilla fetch
against the existing /status endpoint, refreshed every 15s. The whole
file is the page; copy/paste-able into a browser if needed.
"""
from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <title>StudioOS</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #0b0f17;
      --panel: #131b29;
      --panel-2: #1a2538;
      --text: #e6edf3;
      --muted: #7d8ea4;
      --border: #25324a;
      --green: #3fb950;
      --red: #f85149;
      --yellow: #d29922;
      --cyan: #58a6ff;
      --magenta: #bc8cff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      font-size: 14px;
      line-height: 1.4;
    }
    header {
      padding: 14px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 16px;
    }
    header h1 {
      font-size: 18px;
      margin: 0;
      font-weight: 600;
    }
    header .as-of {
      font-size: 12px;
      color: var(--muted);
      margin-left: auto;
    }
    header .pill {
      background: var(--panel);
      padding: 4px 10px;
      border-radius: 12px;
      font-size: 12px;
      border: 1px solid var(--border);
    }
    main {
      padding: 16px 24px;
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(12, 1fr);
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      overflow: hidden;
    }
    .card h2 {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      margin: 0 0 12px 0;
      font-weight: 600;
    }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    @media (max-width: 1100px) {
      .span-3, .span-4, .span-6, .span-8 { grid-column: span 12; }
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      padding: 6px 8px;
      text-align: left;
      border-bottom: 1px solid var(--panel-2);
    }
    th {
      color: var(--muted);
      font-weight: 500;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    tr:last-child td { border-bottom: none; }
    .stat-row {
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
    }
    .stat {
      flex: 1 1 80px;
      min-width: 60px;
    }
    .stat .v { font-size: 22px; font-weight: 600; }
    .stat .l { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
    .state-completed { color: var(--green); }
    .state-failed, .state-budget_exceeded, .state-dead, .state-timed_out { color: var(--red); }
    .state-running { color: var(--cyan); }
    .state-pending { color: var(--yellow); }
    .state-awaiting_approval { color: var(--yellow); }
    .mode-normal { color: var(--green); }
    .mode-degraded { color: var(--yellow); }
    .mode-paused, .mode-emergency { color: var(--red); }
    .agent-id { color: var(--magenta); }
    .due-now { color: var(--green); font-weight: 600; }
    .due-bad { color: var(--red); }
    .muted { color: var(--muted); }
    .truncate {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 320px;
    }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 10px;
      background: var(--panel-2);
      color: var(--muted);
      font-size: 11px;
    }
    .badge.alert {
      background: rgba(248, 81, 73, 0.15);
      color: var(--red);
    }
    .empty { color: var(--muted); font-style: italic; }
    a { color: var(--cyan); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .footer {
      padding: 12px 24px;
      color: var(--muted);
      font-size: 11px;
      text-align: center;
    }
  </style>
</head>
<body>
  <header>
    <h1>StudioOS</h1>
    <span class="pill" id="health">…</span>
    <span class="pill" id="approvals-pill" style="display:none;"></span>
    <span class="as-of" id="asof">loading…</span>
  </header>

  <main>
    <section class="card span-3">
      <h2>Run state</h2>
      <div class="stat-row" id="run-stats"></div>
    </section>

    <section class="card span-3">
      <h2>Events / hour</h2>
      <div class="stat-row" id="event-stats"></div>
    </section>

    <section class="card span-3">
      <h2>Tools / hour</h2>
      <div class="stat-row" id="tool-stats"></div>
    </section>

    <section class="card span-3">
      <h2>Failures / hour</h2>
      <div class="stat-row">
        <div class="stat">
          <div class="v" id="failures">—</div>
          <div class="l">last 60m</div>
        </div>
      </div>
    </section>

    <section class="card span-12">
      <h2>Agents</h2>
      <table>
        <thead>
          <tr>
            <th>Agent</th>
            <th>Studio</th>
            <th>Mode</th>
            <th>Schedule</th>
            <th>Next due</th>
            <th>Tool scope</th>
          </tr>
        </thead>
        <tbody id="agents"></tbody>
      </table>
    </section>

    <section class="card span-8">
      <h2>Recent runs</h2>
      <table>
        <thead>
          <tr>
            <th>When</th>
            <th>Agent</th>
            <th>State</th>
            <th>Trigger</th>
            <th>Summary / error</th>
          </tr>
        </thead>
        <tbody id="recent-runs"></tbody>
      </table>
    </section>

    <section class="card span-4">
      <h2>Tools (last hour)</h2>
      <table>
        <thead>
          <tr><th>Tool</th><th style="text-align:right;">Calls</th></tr>
        </thead>
        <tbody id="tools-table"></tbody>
      </table>
      <div style="margin-top:8px;color:var(--muted);font-size:12px;">
        total spend <span id="tool-spend">0</span>¢
      </div>
    </section>

    <section class="card span-6">
      <h2>Events (last hour)</h2>
      <table>
        <thead>
          <tr><th>Type</th><th style="text-align:right;">Count</th></tr>
        </thead>
        <tbody id="events-table"></tbody>
      </table>
    </section>

    <section class="card span-6">
      <h2>Budgets</h2>
      <table>
        <thead>
          <tr>
            <th>Scope</th>
            <th>Period</th>
            <th style="text-align:right;">Spent / Limit</th>
            <th style="text-align:right;">Remaining</th>
          </tr>
        </thead>
        <tbody id="budgets"></tbody>
      </table>
    </section>
  </main>

  <div class="footer">
    Auto-refresh every 15s · <span id="version"></span>
  </div>

<script>
const fmt = (s) => s == null ? "—" : s;
const human = (sec) => {
  if (sec == null) return "—";
  if (sec <= 0) return '<span class="due-now">now</span>';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
  return `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
};
const tsLocal = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleTimeString(); } catch { return iso; }
};
const escape = (str) => String(str || "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[c]));

async function refresh() {
  try {
    const r = await fetch("/status");
    if (!r.ok) throw new Error(`status ${r.status}`);
    const d = await r.json();

    document.getElementById("health").textContent = "OK";
    document.getElementById("health").style.color = "var(--green)";
    document.getElementById("asof").textContent = "as of " + new Date(d.as_of).toLocaleString();

    if (d.pending_approvals > 0) {
      const p = document.getElementById("approvals-pill");
      p.textContent = `${d.pending_approvals} pending approvals`;
      p.classList.add("alert");
      p.style.display = "inline";
    } else {
      document.getElementById("approvals-pill").style.display = "none";
    }

    // Run state cards
    const runStates = d.runs_by_state || {};
    document.getElementById("run-stats").innerHTML = Object.entries(runStates)
      .sort(([a],[b]) => a.localeCompare(b))
      .map(([k,v]) => `<div class="stat"><div class="v state-${k}">${v}</div><div class="l">${k}</div></div>`).join("") || '<div class="empty">no runs yet</div>';

    // Events stats
    const evCounts = d.event_type_counts_last_hour || {};
    const totalEv = Object.values(evCounts).reduce((a,b)=>a+b,0);
    document.getElementById("event-stats").innerHTML = `<div class="stat"><div class="v">${totalEv}</div><div class="l">total</div></div>`;

    // Tool stats
    const toolCounts = d.tool_call_counts_last_hour || {};
    const totalTools = Object.values(toolCounts).reduce((a,b)=>a+b,0);
    document.getElementById("tool-stats").innerHTML = `<div class="stat"><div class="v">${totalTools}</div><div class="l">calls</div></div><div class="stat"><div class="v">${d.tool_cost_cents_last_hour||0}¢</div><div class="l">cost</div></div>`;

    // Failures
    const f = d.failures_last_hour || 0;
    const fEl = document.getElementById("failures");
    fEl.textContent = f;
    fEl.style.color = f > 0 ? "var(--red)" : "var(--green)";

    // Agents table
    document.getElementById("agents").innerHTML = (d.agents || []).map(a => `
      <tr>
        <td><span class="agent-id">${escape(a.id)}</span></td>
        <td>${escape(a.studio_id)}</td>
        <td><span class="mode-${a.mode}">${escape(a.mode)}</span></td>
        <td>${a.schedule_cron ? `<span class="muted">${escape(a.schedule_cron)}</span>` : '<span class="muted">—</span>'}</td>
        <td>${a.schedule_cron ? (a.next_due_seconds == null ? '<span class="due-bad">bad</span>' : human(a.next_due_seconds)) : '<span class="muted">—</span>'}</td>
        <td><span class="muted truncate" style="display:inline-block">${escape((a.tool_scope || []).join(", ")) || "—"}</span></td>
      </tr>
    `).join("");

    // Recent runs
    document.getElementById("recent-runs").innerHTML = (d.recent_runs || []).map(r => `
      <tr>
        <td class="muted">${tsLocal(r.created_at)}</td>
        <td class="agent-id">${escape(r.agent_id)}</td>
        <td><span class="state-${r.state}">${escape(r.state)}</span></td>
        <td class="muted">${escape(r.trigger_type)}</td>
        <td class="truncate">${escape(r.error || r.summary || "")}</td>
      </tr>
    `).join("") || '<tr><td colspan="5" class="empty">no recent runs</td></tr>';

    // Tools table
    const toolEntries = Object.entries(toolCounts).sort(([,a],[,b]) => b - a);
    document.getElementById("tools-table").innerHTML = toolEntries.map(([k,v]) => `
      <tr><td>${escape(k)}</td><td style="text-align:right;">${v}</td></tr>
    `).join("") || '<tr><td colspan="2" class="empty">no tool calls</td></tr>';
    document.getElementById("tool-spend").textContent = d.tool_cost_cents_last_hour || 0;

    // Events table
    const evEntries = Object.entries(evCounts).sort(([,a],[,b]) => b - a);
    document.getElementById("events-table").innerHTML = evEntries.map(([k,v]) => `
      <tr><td>${escape(k)}</td><td style="text-align:right;">${v}</td></tr>
    `).join("") || '<tr><td colspan="2" class="empty">no events</td></tr>';

    // Budgets
    document.getElementById("budgets").innerHTML = (d.budgets || []).map(b => {
      const pct = b.limit_cents > 0 ? Math.round((b.spent_cents / b.limit_cents) * 100) : 0;
      const over = b.over;
      return `
        <tr>
          <td>${escape(b.scope)}</td>
          <td class="muted">${escape(b.period)}</td>
          <td style="text-align:right;">${b.spent_cents} / ${b.limit_cents} (${pct}%)</td>
          <td style="text-align:right;${over ? "color:var(--red);" : "color:var(--green);"}">${b.remaining_cents}</td>
        </tr>
      `;
    }).join("") || '<tr><td colspan="4" class="empty">no budgets configured</td></tr>';

  } catch (err) {
    document.getElementById("health").textContent = "ERROR";
    document.getElementById("health").style.color = "var(--red)";
    console.error(err);
  }
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""
