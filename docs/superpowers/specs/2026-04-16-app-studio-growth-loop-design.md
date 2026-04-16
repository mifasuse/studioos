# M29: App Studio Growth Loop — 4 Agent Migration

> Port OpenClaw App Studio's Growth Intelligence, Growth Execution, CEO, and Pricing agents to StudioOS. These form the weekly growth loop that drives MRR optimization.

## Kapsam

4 agent + 1 tool module + event schemas + Slack routing.

### Kapsam dışı

- Dev, QA, Game Designer, Marketing, Hub Dev (Grup B + C, sonraki milestone)
- Firebase API direct access (Hub API wraps it)
- RevenueCat direct access (Hub API wraps it)
- Apple Search Ads campaign management (Marketing agent's job)

---

## 1. Hub API Tool Module

**File:** `studioos/tools/hub.py`

**Auth:** `X-API-Key` header from `STUDIOOS_HUB_API_KEY` env var.
**Base URL:** `https://hub.mifasuse.com/api` (internal: `http://hub-backend:8000/api` if on same network).
**Key:** `orzKx9ShTha9PHbPGm6ieuMjCfbZVu33TLvPn1Oz3Y4`

### Tools

**`hub.api.overview`** — Single-app KPI snapshot.
- Input: `app_id` (string, required), `days` (int, default 7)
- Output: `{app_id, period_days, spend, installs, cpa, admob_revenue, rc_revenue, total_revenue, net, roi, mrr, active_subscriptions, arpu, last_updated}`
- Endpoint: `GET /api/overview?app_id={app_id}&days={days}`

**`hub.api.metrics`** — Parametric metrics fetch.
- Input: `app_id` (string, required), `metric` (enum: summary|conversion|countries|cohort|mrr_history|funnel|retention), `days` (int, default 30)
- Output: varies by metric type
- Endpoints:
  - summary: `GET /api/metrics/summary?app_id={app_id}&days={days}`
  - conversion: `GET /api/metrics/conversion?app_id={app_id}&days={days}`
  - countries: `GET /api/metrics/countries?app_id={app_id}&days={days}`
  - cohort: `GET /api/metrics/cohort?app_id={app_id}&days={days}`
  - mrr_history: `GET /api/overview/mrr-history?app_id={app_id}`
  - funnel: `GET /api/firebase/funnel?days={days}`
  - retention: `GET /api/firebase/retention?days={days}`

**`hub.api.campaigns`** — ASA campaign management.
- Input: `action` (enum: list|pause|enable|set_budget), `campaign_id` (int, for mutations), `daily_budget` (float, for set_budget)
- Endpoints:
  - list: `GET /api/campaigns`
  - pause: `PUT /api/campaigns/{id}/status` body `{"status": "PAUSED"}`
  - enable: `PUT /api/campaigns/{id}/status` body `{"status": "ENABLED"}`
  - set_budget: `PUT /api/campaigns/{id}/budget` body `{"daily_budget": X}`

---

## 2. Event Schemas

**File:** `studioos/events/schemas_app.py`

| Event Type | Emitter | Payload |
|-----------|---------|---------|
| `app.growth.weekly_report` | growth-intel | `{app_id, period_days, mrr, active_subs, roi, trial_starts, churn_rate, retention_d7, anomalies[], summary}` |
| `app.growth.anomaly_detected` | growth-intel | `{app_id, anomaly_type, metric_name, current_value, previous_value, delta_pct, severity}` |
| `app.discovery.completed` | growth-intel | `{app_name, competitors_count, mvp_features[], gtm_summary}` |
| `app.experiment.proposed` | growth-exec | `{experiment_id, app_id, hypothesis, variants[], traffic_split, duration_days, lane, metrics[]}` |
| `app.experiment.launched` | growth-exec | `{experiment_id, app_id, lane, launched_at}` |
| `app.ceo.weekly_brief` | ceo | `{decisions[], delegations[], kpi_summary}` |
| `app.pricing.recommendation` | pricing | `{app_id, current_price, recommended_price, rationale, ab_test_plan}` |
| `app.task.growth_intel` | ceo | `{target_agent, title, description, priority}` |
| `app.task.pricing` | ceo | `{target_agent, title, description, priority}` |
| `app.task.growth_exec` | ceo | `{target_agent, title, description, priority}` |

---

## 3. Agent Workflows

### 3a. app-studio-growth-intel

**Schedule:** `0 8 * * 1` (Monday 08:00 — 1h before CEO)
**Event triggers:** `app.task.growth_intel`
**Template:** `app_studio_growth_intel`

**Nodes:**

```
START → collect_metrics → analyze → report → END
```

**collect_metrics:**
- For each `goals.tracked_apps` (["quit_smoking", "sms_forward"]):
  - `hub.api.overview` (days=7)
  - `hub.api.metrics` (metric=conversion, days=7)
  - `hub.api.metrics` (metric=retention, days=7)
  - `hub.api.metrics` (metric=mrr_history)

**analyze:**
- Anomaly detection (deterministic):
  - trial_starts == 0 → critical anomaly
  - ROI < 1.0 → warning
  - MRR WoW change > 20% down → alert
  - churn_rate > 15% → warning
  - retention_d7 < 20% → warning
- For each anomaly, emit `app.growth.anomaly_detected` event.

**report:**
- LLM call: summarize metrics + anomalies into a Turkish brief.
- Post to Slack `#intel` (C0AN9PGJELE) + `#hq` (C0AMWBZN39V).
- Post to Telegram.
- Emit `app.growth.weekly_report` event.
- Memory: store the report for reflector.

**Product Discovery mode** (triggered by `app.task.growth_intel` with `payload.kind == "discovery"`):
- Separate node path: `collect_discovery → analyze_discovery → report_discovery`
- Uses LLM to produce the 6-section Product Discovery doc (competitors, 1-star reviews, feature gap, 10x hypothesis, MVP scope, GTM).
- Emit `app.discovery.completed`.
- Post to Slack `#new-app` (C0ANQNER873).

**Goals:**
```yaml
tracked_apps: [quit_smoking, sms_forward]
anomaly_thresholds:
  min_roi: 1.0
  max_churn_rate: 15.0
  min_retention_d7: 20.0
  max_mrr_drop_pct: 20.0
```

**Tool scope:** `hub.api.overview`, `hub.api.metrics`, `llm.chat`, `slack.notify`, `telegram.notify`, `memory.search`

### 3b. app-studio-growth-exec

**Schedule:** none (event-triggered only)
**Event triggers:** `app.growth.weekly_report`, `app.task.growth_exec`
**Template:** `app_studio_growth_exec`

**Nodes:**

```
START → intake → propose_experiment → gate → END
```

**intake:** Read the weekly report event payload, extract anomalies + metrics.

**propose_experiment:**
- LLM call: given this week's metrics + anomalies, propose 1-3 experiments.
- Each experiment classified as Fast Lane or CEO Lane:
  - **Fast Lane:** reversible, <1 day to implement, ≤20% user impact → no approval needed.
  - **CEO Lane:** pricing change, paywall change, >20% rollout → approval required.

**gate:**
- Fast Lane experiments: emit `app.experiment.launched` directly.
- CEO Lane experiments: create approval row, emit `app.experiment.proposed`.
- Notify Slack `#experiments` (C0ANFD5F32Q).
- Memory: store experiment proposals.

**Tool scope:** `llm.chat`, `slack.notify`, `telegram.notify`, `memory.search`

### 3c. app-studio-ceo

**Schedule:** `0 9 * * 1` (Monday 09:00)
**Event triggers:** `app.task.ceo`
**Template:** `app_studio_ceo`

**Nodes:**

```
START → seed_kpi_targets → collect → brief → publish → END
```

**seed_kpi_targets** (first run only):
- MRR target: $500 (higher_better)
- ROI target: 2.0x (higher_better)
- Churn rate target: 10% (lower_better)
- Active subs target: 200 (higher_better)

**collect:**
- Hub API overview for each tracked app (7d + 30d).
- Read last week's `app.growth.weekly_report` from events table.
- Read pending experiments from events table.
- Read current KPI state.

**brief:**
- LLM prompt (mirrors AMZ CEO pattern):
  - "MRR'ı en çok etkileyecek 3 şey?"
  - Max 2 decisions: pricing + acquisition.
  - Delegate via `app.task.*` events.
- Output: Turkish markdown brief.

**publish:**
- Post to Slack `#hq` + Telegram.
- Emit `app.ceo.weekly_brief`.
- Emit task delegation events.

**Tool scope:** `hub.api.overview`, `hub.api.metrics`, `llm.chat`, `slack.notify`, `telegram.notify`, `memory.search`, `kpi.read`

### 3d. app-studio-pricing

**Schedule:** none (event-triggered only)
**Event triggers:** `app.task.pricing`
**Template:** `app_studio_pricing`

**Nodes:**

```
START → collect → analyze → recommend → END
```

**collect:**
- Hub API: countries (ARPU per country), conversion, mrr_history for the target app.

**analyze:**
- LLM: competitive pricing context + country-level data → WTP segments.
- Deterministic: if ARPU < $1 in a country with >100 installs → flag for country-specific pricing.

**recommend:**
- Output: recommended price point + A/B test plan (variants, split, duration, metrics).
- Approval gate: CEO must approve before experiment launches.
- Emit `app.pricing.recommendation`.
- Post to Slack `#growth-ops` (C0ANFD4APK6).

**Tool scope:** `hub.api.overview`, `hub.api.metrics`, `llm.chat`, `slack.notify`, `telegram.notify`, `memory.search`

---

## 4. Slack Per-Agent Routing

Add to `STUDIOOS_SLACK_AGENT_TOKENS`:
```
app-studio-ceo=xoxb-REDACTED
app-studio-growth-intel=xoxb-REDACTED
app-studio-growth-exec=xoxb-REDACTED
app-studio-pricing=xoxb-REDACTED
```

Add to `STUDIOOS_SLACK_AGENT_CHANNELS`:
```
app-studio-growth-intel=C0AN9PGJELE
app-studio-growth-exec=C0ANFD5F32Q
app-studio-pricing=C0ANFD4APK6
```

---

## 5. studio.yaml Updates

Replace existing 3 agents with 7 (3 existing + 4 new). Add templates, subscriptions, event patterns.

---

## 6. Config

New env vars:
- `STUDIOOS_HUB_API_KEY=orzKx9ShTha9PHbPGm6ieuMjCfbZVu33TLvPn1Oz3Y4`
- `STUDIOOS_HUB_API_URL=https://hub.mifasuse.com/api` (or internal `http://hub-backend:8000/api`)
- Slack agent tokens + channels (appended to existing env vars)

---

## 7. Test Plan

1. **Hub API tools**: mock httpx, verify overview/metrics/campaigns parse correctly
2. **Growth Intel anomaly detection**: deterministic thresholds (trial=0, ROI<1, MRR drop >20%)
3. **Growth Exec lane classification**: Fast Lane vs CEO Lane rules
4. **CEO KPI seed**: verify 4 targets created on first run
5. **Event schema validation**: all 10 event types register correctly
6. **Slack routing**: per-agent token + channel resolution
