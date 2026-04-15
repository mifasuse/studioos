# StudioOS — Mimari Plan ve İlk Milestone

> **Doküman tipi:** Yaşayan referans (living reference).
> **Son güncelleme:** 2026-04-15.
> **Yazarlar:** Nuri Topçugil + Claude (tasarım oturumu).

---

## 0. Başlangıç Noktası

Mevcut kurulum (2026-04-15 sabahı itibarıyla):
- **OpenClaw** runtime, 17 ajan, 2 studio (AMZ Arbitrage + App Studio)
- **MiniMax M2.7-highspeed** tek LLM provider ($30/ay Plus plan)
- Tek kanal konsolidasyon (`#amz-hq` + `#hq`)
- Scope lock, orchestration docs, per-agent escalation rules
- Session cron cleanup aktif
- **Sorun:** Reaktif sistem (Slack mention bekler), otonom değil

## 1. Vizyon

Nuri'nin hedefi: **Ai Studio İşletim Sistemi (StudioOS)**.

Tek kullanıcılık chatbot platformu değil, **kendi kendine işleyen, öğrenen, multi-tenant ai şirketi platformu**.

### Planlanan studio tipleri
1. **AMZ Arbitrage** (mevcut) — TR→US arbitraj
2. **App Studio** (mevcut) — Mobile app portföyü
3. **Game Studio** (planlanan) — Oyun geliştirme
4. **Freelance Studio** (planlanan) — Fiverr/Upwork iş alıp yapan
5. **SEO/GEO Agency** (planlanan) — Client SEO hizmet
6. **NFT Trading** (planlanan) — Otomatik NFT alım/satım

### Otonomi seviyesi — hedef
- **Pragmatik hedef:** L4-L5 (goal-driven + learning)
- **Uzak vizyon:** L7 (self-organizing)
- **Başlangıç:** L3 (heartbeat-driven proactive) → hızla L4'e evrilir

### Kritik ilke
- Bu chatbot değil: **event-driven distributed operating system**.
- Agent = identity (persistent); Run = execution (ephemeral).
- Deployment, reliability, observability first-class concern.

---

## 2. Mimari Kararlar — Özet

### Platform seviyesi

| # | Konu | Seçim | Gerekçe |
|---|------|-------|---------|
| 1 | Orchestration engine | **LangGraph** | Stateful workflow, checkpoint, time-travel, branching native |
| 2 | Language | **Python 3.12** | AI ekosistemi + LangGraph | 
| 3 | LLM strategy | **Multi-provider router** | MiniMax default, Claude strategic, GPT/OpenCodex coding, fallback chain |
| 4 | Dashboard timing | **Slack-first → hybrid** | Phase 1 minimal, Phase 2 dashboard |
| 5 | Meta-CEO | **HAYIR (şimdilik)** | Nuri = meta-CEO; 3+ studio aktifleştiğinde değerlendirilir |
| 6 | Migration | **Parallel run** | OpenClaw çalışmaya devam, StudioOS paralel kurulur, tek flow taşınır, güven kazanıldıkça genişler |

### Runtime tasarımı

| # | Soru | Seçim | Notlar |
|---|------|-------|--------|
| Q1 | Agent as Process vs Function | **Function (stateless) + Run abstraction** | Stateless function + durable state + resumable execution. Agent = identity, Run = execution. |
| Q2 | Template vs Instance | **Template + Instance + Versioning** | Template = behavior (workflow, role logic, tool contract). Instance = business context (goals, KPIs, tool scope, budget). Templates versioned (v1, v2...) → rollout + rollback. |
| Q3 | Event bus | **Postgres Outbox + Redis Streams** | Baştan doğru kurulur. Outbox transactional, Redis fan-out. |
| Q4 | Event naming | **Domain prefix** | `amz.opportunity.detected`, `app.build.completed`. Routing ve subscription basit olur. |
| Q5 | Schema versioning | **Dual publish + strict deprecation window** | v1 + v2 birlikte yayınlanır, consumer migrate, v1 deprecate. Sonsuz backward compat yok. |
| Q6 | Workflow checkpoint | **Postgres (LangGraph PostgresSaver)** | Run + checkpoint transactional birlik. Workflow state = snapshot + pointer; büyük state için ileride S3 fallback. |
| Q7 | Tool registry | **Hibrit MCP (HTTP + stdio) + Governance Layer** | HTTP: Playwright, GitHub, Slack (long-lived). Stdio: küçük CLI/script. **Kritik:** Tool Governance (rate limit, audit, approval gate) baştan. |
| Q8 | Agent mode | **Hybrid (auto + manual)** | `normal`, `degraded`, `paused`, `emergency`. Mode scheduler behavior değiştirir: `degraded` = sadece HIGH priority; `emergency` = bypass queue. |
| Q9 | Memory decay | **Importance + age + agent-driven GC** | `importance < 0.3 AND age > 30d AND unused` → sil. Ayrıca haftalık "memory pruning agent" + "reflection agent" — GC sadece cron değil, learning loop. |
| Q10 | Deployment topology | **Modular Monolith** | Tek Docker container, logical module separation. Microservices'a erken geçmek solo-dev için ölüm. |

### Kritik ek önlemler (kaçınılmaz)

- **Correlation / Trace sistemi** — her run `correlation_id`, her event carryover. Distributed tracing temeli.
- **Idempotency key** — duplicate event / retry / crash recovery. `UNIQUE` constraint + consumer dedup.
- **Dead Letter Queue (DLQ)** — failed events baştan. Admin UI'da inspect + manuel retry/discard.
- **Priority queue** — emergency (0) → approval (10) → high (20) → normal (50) → low (80) → background (99). Mode ile birleşir.
- **Idempotent event handlers** — her subscriber event_id ile dedup. Zorunlu.

### Logistik kararlar

| # | Konu | Seçim |
|---|------|-------|
| Q-L1 | Repo yeri | **GitHub: `mifasuse/studioos`** (yeni, private veya public) |
| Q-L2 | Çalışma yeri | **Hetzner sunucu** (168.119.15.239), `/srv/projects/studioos/` altında, kendi Postgres container'ı |
| Q-L3 | Deploy akışı | **GitHub Actions → SSH → git pull + docker compose** (pricefinder pattern aynı) |

---

## 3. Sistem Mimarisi — Yüksek Seviye

```
┌─────────────────────────────────────────────────────────────┐
│                      StudioOS Platform                       │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │    Studio    │  │    Studio    │  │    Studio    │ ...  │
│  │     AMZ      │  │  App Studio  │  │     Game     │      │
│  │  Arbitrage   │  │              │  │              │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                  │              │
│         └─────────┬───────┴───────┬──────────┘              │
│                   │               │                         │
│          ┌────────▼──────┐   ┌────▼──────────┐              │
│          │ Core Services │   │ Meta Services │              │
│          │               │   │               │              │
│          │ • Postgres    │   │ • Scheduler   │              │
│          │ • pgvector    │   │ • Dispatcher  │              │
│          │ • Redis       │   │ • Runner      │              │
│          │ • MinIO       │   │ • Outbox pub  │              │
│          │               │   │ • Event bus   │              │
│          │               │   │ • LLM router  │              │
│          │               │   │ • Tool reg.   │              │
│          │               │   │ • Audit log   │              │
│          │               │   │ • Governance  │              │
│          └───────────────┘   └───────────────┘              │
└─────────────────────────────────────────────────────────────┘
           ↕ Slack / Telegram / Dashboard / CLI
┌─────────────────────────────────────────────────────────────┐
│                    Nuri (Meta-Admin)                        │
│  • Studio create/pause/retire                                │
│  • Strategic approval                                        │
│  • Cross-studio budget allocation                            │
└─────────────────────────────────────────────────────────────┘
```

### Katmanlar

**Katman 1 — Platform Services (tüm studio'ların paylaştığı çekirdek)**
1. Studio Manager — studio lifecycle (CRUD, isolation)
2. Agent Runtime — scheduler + tool executor + session state
3. LLM Router — provider seçimi + budget + fallback
4. Tool Registry — MCP servers + scoping + rate limit
5. Event Bus — Redis Streams pub/sub
6. Memory Store — Postgres + pgvector
7. Audit Log — append-only, queryable, rollback-ready
8. Observability — dashboard + metrics + alerts
9. Governance — playbook + approvals + budget enforcement

**Katman 2 — Studio (self-contained iş birimi)**
- Mission statement
- KPIs (goal + current)
- Agent roster
- World state (structured DB)
- Memory (4 tip: short, episodic, semantic, procedural)
- Tools (studio'ya özel)
- Budget (LLM + iş)
- Slack/comm channels
- Playbook (learned patterns)

**Katman 3 — Agent (individual worker)**
- Role
- Heartbeat config
- Goals (studio KPI slice)
- Memory
- Tool access scope
- Budget
- Escalation rules
- Mode (normal / degraded / paused / emergency)

---

## 4. Database Schema (v1)

### Platform tables

```sql
-- Studio registry
CREATE TABLE studios (
  id              TEXT PRIMARY KEY,
  display_name    TEXT NOT NULL,
  mission         TEXT,
  status          TEXT CHECK (status IN ('active','paused','retired')),
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  retired_at      TIMESTAMPTZ,
  config          JSONB DEFAULT '{}',
  metadata        JSONB DEFAULT '{}'
);

-- Agent templates (reusable, versioned)
CREATE TABLE agent_templates (
  id              TEXT NOT NULL,
  version         INTEGER NOT NULL,
  display_name    TEXT NOT NULL,
  description     TEXT,
  workflow_ref    TEXT NOT NULL,
  input_schema    JSONB,
  output_schema   JSONB,
  required_tools  TEXT[],
  default_config  JSONB DEFAULT '{}',
  deprecated_at   TIMESTAMPTZ,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (id, version)
);

-- Agent instances
CREATE TABLE agents (
  id                  TEXT PRIMARY KEY,
  studio_id           TEXT REFERENCES studios(id),
  template_id         TEXT NOT NULL,
  template_version    INTEGER NOT NULL,
  display_name        TEXT,
  slack_handle        TEXT,
  mode                TEXT DEFAULT 'normal'
                        CHECK (mode IN ('normal','degraded','paused','emergency')),
  heartbeat_config    JSONB,
  goals               JSONB,
  tool_scope          TEXT[],
  budget_tier         TEXT,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  retired_at          TIMESTAMPTZ,
  FOREIGN KEY (template_id, template_version)
    REFERENCES agent_templates(id, version)
);

-- Agent persistent state
CREATE TABLE agent_state (
  agent_id            TEXT PRIMARY KEY REFERENCES agents(id),
  state               JSONB DEFAULT '{}',
  state_version       INTEGER DEFAULT 1,
  last_run_id         UUID,
  last_run_at         TIMESTAMPTZ,
  updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Runs (execution history)
CREATE TABLE agent_runs (
  id                  UUID PRIMARY KEY,
  agent_id            TEXT REFERENCES agents(id),
  studio_id           TEXT REFERENCES studios(id),
  correlation_id      UUID NOT NULL,
  trigger_type        TEXT,
  trigger_ref         TEXT,
  state               TEXT CHECK (state IN (
                        'pending','running','completed',
                        'failed','timed_out','cancelled','dead'
                      )),
  priority            INTEGER DEFAULT 50,
  workflow_version    TEXT,
  input_snapshot      JSONB,
  output_snapshot     JSONB,
  workflow_state      JSONB,
  error               JSONB,
  retry_count         INTEGER DEFAULT 0,
  parent_run_id       UUID REFERENCES agent_runs(id),
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  started_at          TIMESTAMPTZ,
  ended_at            TIMESTAMPTZ,
  duration_ms         INTEGER GENERATED ALWAYS AS (
                        EXTRACT(EPOCH FROM (ended_at - started_at))*1000
                      ) STORED,
  tokens_used         INTEGER DEFAULT 0,
  cost_usd            NUMERIC(10,4) DEFAULT 0
);

CREATE INDEX idx_runs_agent_time ON agent_runs(agent_id, created_at DESC);
CREATE INDEX idx_runs_correlation ON agent_runs(correlation_id);
CREATE INDEX idx_runs_state ON agent_runs(state) WHERE state IN ('pending','running');
```

### Event system

```sql
CREATE TABLE events (
  id                  UUID PRIMARY KEY,
  event_type          TEXT NOT NULL,
  event_version       INTEGER NOT NULL,
  studio_id           TEXT,
  correlation_id      UUID NOT NULL,
  causation_id        UUID,
  source_type         TEXT,
  source_id           TEXT,
  source_run_id       UUID REFERENCES agent_runs(id),
  idempotency_key     TEXT,
  payload             JSONB NOT NULL,
  metadata            JSONB DEFAULT '{}',
  occurred_at         TIMESTAMPTZ NOT NULL,
  recorded_at         TIMESTAMPTZ DEFAULT NOW(),
  published_at        TIMESTAMPTZ,
  publish_attempts    INTEGER DEFAULT 0,
  UNIQUE (idempotency_key)
);

CREATE INDEX idx_events_unpublished ON events(recorded_at)
  WHERE published_at IS NULL;
CREATE INDEX idx_events_correlation ON events(correlation_id);
CREATE INDEX idx_events_type_time ON events(event_type, occurred_at DESC);

CREATE TABLE subscriptions (
  id                  SERIAL PRIMARY KEY,
  subscriber_type     TEXT,
  subscriber_id       TEXT,
  event_pattern       TEXT,
  filter              JSONB,
  action              TEXT,
  priority            INTEGER DEFAULT 50,
  created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE dead_events (
  event_id            UUID PRIMARY KEY REFERENCES events(id),
  consumer_id         TEXT,
  error               JSONB,
  retry_count         INTEGER,
  first_failed_at     TIMESTAMPTZ,
  last_failed_at      TIMESTAMPTZ,
  resolved_at         TIMESTAMPTZ,
  resolution          TEXT
);
```

### Governance

```sql
CREATE TABLE budget_usage (
  agent_id            TEXT REFERENCES agents(id),
  period              DATE,
  tokens_used         BIGINT DEFAULT 0,
  llm_cost_usd        NUMERIC(10,4) DEFAULT 0,
  tool_calls          INTEGER DEFAULT 0,
  runs_total          INTEGER DEFAULT 0,
  PRIMARY KEY (agent_id, period)
);

CREATE TABLE budget_limits (
  agent_id            TEXT REFERENCES agents(id),
  period              TEXT,
  max_tokens          BIGINT,
  max_cost_usd        NUMERIC(10,4),
  max_runs            INTEGER,
  PRIMARY KEY (agent_id, period)
);

CREATE TABLE approvals (
  id                  UUID PRIMARY KEY,
  correlation_id      UUID NOT NULL,
  requesting_agent    TEXT REFERENCES agents(id),
  requesting_run      UUID REFERENCES agent_runs(id),
  category            TEXT,
  title               TEXT,
  description         TEXT,
  proposed_action     JSONB,
  state               TEXT CHECK (state IN (
                        'pending','granted','denied','expired','cancelled'
                      )),
  requested_at        TIMESTAMPTZ DEFAULT NOW(),
  expires_at          TIMESTAMPTZ,
  decided_at          TIMESTAMPTZ,
  decided_by          TEXT,
  decision_reason     TEXT
);
```

### Memory

```sql
CREATE EXTENSION vector;

CREATE TABLE memory_semantic (
  id                  UUID PRIMARY KEY,
  agent_id            TEXT REFERENCES agents(id),
  studio_id           TEXT REFERENCES studios(id),
  content             TEXT NOT NULL,
  embedding           vector(1536),
  tags                TEXT[],
  importance          REAL DEFAULT 0.5,
  source_run_id       UUID REFERENCES agent_runs(id),
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  accessed_at         TIMESTAMPTZ,
  decay_after         TIMESTAMPTZ
);

CREATE INDEX idx_memory_embedding ON memory_semantic
  USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE memory_episodic (
  id                  UUID PRIMARY KEY,
  agent_id            TEXT REFERENCES agents(id),
  date                DATE NOT NULL,
  content             TEXT,
  summary             TEXT,
  events_count        INTEGER,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (agent_id, date)
);

CREATE TABLE memory_procedural (
  id                  TEXT,
  studio_id           TEXT,
  version             INTEGER,
  content             TEXT,
  author              TEXT,
  change_summary      TEXT,
  created_at          TIMESTAMPTZ DEFAULT NOW(),
  active              BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (id, version)
);
```

---

## 5. Event Flow — Outbox Pattern

```
Agent Run (LangGraph executing)
  ├─ decides to publish event
  ↓
BEGIN TRANSACTION
  UPDATE agent_state
  INSERT INTO events (published_at=NULL)   ← outbox row
COMMIT
  ↓
Outbox Publisher (background daemon, polls WHERE published_at IS NULL)
  ├─ validate against schema registry
  ├─ XADD to Redis Stream
  ├─ UPDATE events SET published_at=NOW()
  ↓
Consumer Groups (scheduler, audit sink, metrics aggregator)
  ├─ read from Redis Stream
  ├─ dedup check (SISMEMBER processed:{type} {event_id})
  ├─ process
  ├─ SADD processed:{type} {event_id} EX 86400
  ├─ XACK
  │
  ├─→ success
  └─→ failure: NACK + retry 3x exponential backoff → DLQ
```

### Idempotency (3 katmanlı)
1. **Publisher:** `idempotency_key` UNIQUE constraint
2. **Stream:** Redis Streams native dedupe via message ID
3. **Consumer:** Redis Set dedup with TTL

### Event envelope (invariant)

```python
class Event:
    event_id: UUID
    event_type: str            # "amz.opportunity.detected"
    event_version: int
    timestamp: datetime         # event time (UTC)
    source: EventSource
    correlation_id: UUID
    causation_id: UUID | None
    studio_id: str
    payload: dict               # validated
    metadata: dict

class EventSource:
    type: Literal["agent","system","human","external"]
    identifier: str
    run_id: UUID | None
```

### Naming convention
`domain.entity.action_past_tense`
- `amz.opportunity.detected`
- `amz.price.changed`
- `app.build.completed`
- `app.qa.passed`
- `agent.lifecycle.started`
- `system.budget.exceeded`
- `governance.approval.requested`

---

## 6. Agent Run Lifecycle

### State machine

```
┌─────────┐
│ PENDING │ ← trigger (heartbeat / event / manual / retry)
└────┬────┘
     ↓
┌─────────┐         ┌───────────┐
│ RUNNING │ ──────→ │ CANCELLED │
└────┬────┘         └───────────┘
     │
     ├──→ COMPLETED
     │
     ├──→ FAILED ──→ PENDING (retry) or DEAD
     │
     └──→ TIMED_OUT ──→ DEAD
```

### Retry policy
- Exponential backoff: 1min → 5min → 15min
- Max 3 retries
- Fatal errors (schema mismatch, config invalid) no retry
- Transitions emit events: `run.triggered`, `run.started`, `run.completed`, `run.failed`, `run.dead`

### Workflow state vs Agent state

Kritik ayrım:
- **Agent state** = persistent across runs (KPIs, memory refs, goals, budget)
- **Workflow state** = ephemeral within run (LangGraph internal, checkpoint, decisions)

LangGraph checkpoint ≠ agent state. İkisini karıştırma.

### Run execution flow

```python
# Agent Runner (Python)
1. agent_state = pg.load(agent_id)
2. memory_snippets = vector.search(agent_id, context)
3. event = event_bus.pop() if triggered
4. input = { agent_state, memory_snippets, event, config }
5. langgraph.run(workflow, input)   # LangGraph takes over
        ↓
    [workflow runs, checkpoints inside]
        ↓
6. output = langgraph result
7. deltas = extract_deltas(output) = {
       agent_state_updates,
       memories_to_save,
       events_to_publish,
       actions_executed
   }
8. BEGIN TX
     UPDATE agent_state
     INSERT memory_*
     INSERT events (outbox)
     UPDATE agent_runs (completed)
   COMMIT
```

### Correlation & tracing

Her run `correlation_id` (UUID). Event chain:
```
Run A (correlation_X)
  → Event E1 (correlation_X, causation=null)
    → wakes Agent B, Run B (correlation_X)
      → Event E2 (correlation_X, causation=E1)
        → wakes Agent C, Run C (correlation_X)
          → Event E3 (correlation_X, causation=E2)
```

Query: `SELECT * FROM events WHERE correlation_id=? ORDER BY occurred_at` → tam pipeline.

### Agent modes

| Mode | Anlamı | Scheduler etkisi |
|------|--------|------------------|
| `normal` | Default | Heartbeat aktif, tüm workflows |
| `degraded` | Tool failure / rate limited | Sadece priority ≤ 20 runs |
| `paused` | Explicit pause (incident, maintenance) | Yeni run kabul etmiyor |
| `emergency` | Elevated (incident response) | Queue bypass, priority 0 |

Mode transitions:
- `normal → degraded`: **auto** (error rate threshold)
- `degraded → normal`: **auto** (health check recovery)
- `any → paused`: **manual** (Nuri veya orchestrator)
- `any → emergency`: **auto + manual** (alert pattern match veya explicit)

Her mode change `agent.mode_changed` event emit eder.

### Priority queue

```python
class Priority:
    EMERGENCY   = 0      # prod down, destructive alert
    APPROVAL    = 10     # human approval gate waiting
    HIGH        = 20     # real-time business event
    NORMAL      = 50     # heartbeat, routine
    LOW         = 80     # reflection, cleanup
    BACKGROUND  = 99     # memory compaction, log rotation
```

Dispatcher: priority ASC, created_at ASC.
Mode ile birleşir: `degraded` mode'daki agent sadece `<= HIGH` işler.

---

## 7. Milestone 1 — Vertical Slice

**Tek amaç:** Core loop çalışıyor mu doğrula.

### Başarı kriteri

Tek komutla zincir çalışır:
```
trigger → pending run → dispatcher → langgraph workflow
  → mock fetch → detect → emit event → outbox
  → consumer → subscription match → wake second agent
  → second run → read event → log → completed

Assert: 2 runs, 1 event, same correlation_id
```

### Scope — dahil

**Tablolar:**
- `studios`
- `agent_templates`
- `agents`
- `agent_state`
- `agent_runs`
- `events`
- `subscriptions`

**Componentler:**
- Alembic migration 0001 (yukarıdaki 7 tablo)
- SQLAlchemy models
- Structlog + correlation_id propagation
- Config (Pydantic Settings, env + yaml)
- 2 minimal LangGraph workflows:
  - `scout_test`: START → fetch_data (mock) → detect_opportunity → emit → END
  - `analyst_test`: START → read event → log → END
- 1 event type: `test.opportunity.detected` v1
- Scheduler (async loop)
- Dispatcher (picks pending run)
- Runner (loads state, invokes workflow, persists output)
- Outbox publisher (polls events, simulates publish — v1 in-process)
- Subscription matcher
- FastAPI routes: `/health`, `/runs/{id}`, `/events`
- CLI (Typer): `init`, `trigger`, `inspect`
- End-to-end pytest

### Scope — hariç (v1'de yok)

- memory tables (semantic, episodic, procedural)
- approvals
- budget tracking
- DLQ
- Redis (in-process polling yeter)
- MCP tool registry (direct function calls)
- Dashboard UI
- Slack/Telegram integration
- Governance layer
- Metrics / Prometheus

### 12 adım (execution plan)

| # | Adım | Tahmin |
|---|------|--------|
| 1 | GitHub repo `mifasuse/studioos` setup + README + MIT | 5 dk |
| 2 | `pyproject.toml` + Python deps + Dockerfile + docker-compose + `.env.example` | 15 dk |
| 3 | Alembic migration 0001 — 7 tablo | 30 dk |
| 4 | SQLAlchemy models + config + db + structlog | 20 dk |
| 5 | Pydantic event schemas — `test.opportunity.detected` v1 | 30 dk |
| 6 | Runtime (scheduler + dispatcher + runner + outbox + subscriptions iskeleti) | 45 dk |
| 7 | LangGraph workflows — `scout_test`, `analyst_test` | 30 dk |
| 8 | Studio config loader — YAML okur, DB'ye seed | 20 dk |
| 9 | FastAPI routes + CLI | 20 dk |
| 10 | End-to-end pytest — chain assert | 30 dk |
| 11 | Docker compose up, gerçek Postgres'te run | 20 dk |
| 12 | GitHub Actions deploy pipeline, sunucuya ilk push | 20 dk |

**Toplam:** ~4-5 saat kesintisiz iş.

### Başlangıç agent çifti

**A (seçilen): `test-scout` → `test-analyst`** (generic, no external deps)

Avantajı: dış bağımlılık yok (real DB, Slack yok). Core loop saf haliyle doğrulanır. Sonra `scout_test`'in mock'u gerçek PriceFinder sorgusuna değişir → **C (AMZ real)** otomatik çıkar.

---

## 8. Milestones Outlook

| Faz | Süre (tahmini) | İçerik |
|-----|----------------|--------|
| **M1** — Vertical slice (şu an) | 1 gün | Scout → event → Analyst, single studio (test) |
| **M2** — Memory + state | 2-3 gün | pg schema completion, memory tables, vector store, KPI ingestion |
| **M3** — Redis event bus | 1-2 gün | Outbox publisher → Redis Streams, consumer groups, DLQ skeleton |
| **M4** — MCP tool registry | 2-3 gün | Tool Governance Layer, MCP HTTP + stdio hybrid, rate limit, audit |
| **M5** — Budget + governance | 2-3 gün | Budget enforcer, approvals table + flow, human-in-loop UI iskeleti |
| **M6** — AMZ Studio migration | 2-3 gün | Scout/Analyst/Pricer/CEO to LangGraph, real PriceFinder data, parallel run with OpenClaw |
| **M7** — Observability + dashboard | 3-5 gün | Next.js dashboard (studio/agent/run/event inspectors), structured metrics |
| **M8** — App Studio migration | 1-2 gün | Same pattern, port 9 agents |
| **M9** — Learning loop | 2-3 gün | Daily reflection, weekly CEO review, playbook evolution |
| **M10** — OpenClaw retire | 1 gün | Cutover, cleanup |
| **M11** — Game Studio template | 2-3 gün | New studio type (game domain), reuse template+instance pattern |
| **M12** — Multi-provider LLM router | 2 gün | MiniMax + Claude + GPT routing |

**v1 tamamlama (AMZ + App Studio + dashboard):** ~3-4 hafta focused work.

---

## 9. Tech Stack (lock)

| Katman | Seçim | Versiyon | Gerekçe |
|--------|-------|----------|---------|
| Language | Python | 3.12 | AI ecosystem, async mature |
| Package mgr | uv | latest | Hızlı, reproducible, modern |
| Web framework | FastAPI | latest | Async, OpenAPI, Pydantic native |
| ORM | SQLAlchemy 2.0 async | latest | Proven, type-safe |
| Migrations | Alembic | latest | Standard |
| Orchestration | LangGraph | latest | Core workflow engine |
| Checkpoint | PostgresSaver (langgraph) | latest | Transactional unity |
| DB | PostgreSQL | 16 | pgvector ready |
| Vector DB | pgvector | latest | Same DB, no separate service |
| Cache/Stream | Redis | 7 | Later — v1 no Redis |
| LLM (default) | MiniMax M2.7-highspeed | - | $30/mo Plus plan, existing |
| LLM (strategic) | Claude Sonnet 4.6 | - | Opus reasoning when needed |
| LLM (coding) | GPT-5 / OpenCodex | - | Code generation niche |
| CLI | Typer | latest | Type-safe, fast |
| Testing | pytest + pytest-asyncio | latest | Standard |
| Logging | structlog | latest | JSON, correlation-aware |
| Config | Pydantic Settings | latest | env + yaml hybrid |
| Dashboard | Next.js + Tailwind | 14+ | Later — matches Hub stack |
| Deployment | Docker Compose | latest | Solo-dev friendly |
| CI/CD | GitHub Actions | - | Pricefinder pattern |

### Explicitly NOT used (bilinçli)
- Kubernetes (overkill for solo dev)
- Kafka (Redis Streams yeter)
- LangChain (sadece ihtiyaç duyulursa, minimum footprint)
- Celery (LangGraph scheduler + async Python kendi çözüyor)

---

## 10. Açık Sorular ve Gelecek Kararlar

- **Q-F1** (future): Workflow definition format — pure Python DSL vs YAML declarative vs hybrid. Şimdilik: pure Python.
- **Q-F2** (future): Template inheritance — Scout template → AMZ-Scout vs Game-Scout. Multi-level override var mı?
- **Q-F3** (future): Cross-studio event routing — studio A'daki event studio B'yi tetikler mi?
- **Q-F4** (future): Agent spawning — bir agent başka bir agent instance yaratabilir mi? (sub-agent pattern)
- **Q-F5** (future): Multi-tenant isolation level — logical DB schemas mı, tamamen ayrı DB'ler mi?
- **Q-F6** (future): Human approval UI — Slack button interaction mu, dashboard mı, email mi?
- **Q-F7** (future): Playbook versioning — agent kendi playbook'unu update edebilir mi, yoksa sadece human approves mu?

---

## 11. Kararların Oturum Referansı

Bu planın oluşturulduğu oturum boyunca alınan kararların özet listesi:

1. OpenClaw'da yapılan ince ayarlar (scope lock, persona, escalation) **devam ediyor** ama **geçici** — StudioOS production'a geçene kadar yaşar.
2. MiniMax Plus $30/ay plan aktif, tüm agent'lar bunu kullanıyor.
3. Claude proxy **tamamen silindi** (container, config, env vars).
4. amz-tool ve app-studio-tool **decommission edildi**; her agent kendi tool'unu direkt çalıştırıyor.
5. AMZ ve App Studio için ORCHESTRATION.md + per-agent escalation rules yazıldı, git commit atıldı.
6. Session auto-archive cron aktif (daily 03:00).
7. Host filesystem mount `/srv/projects:/srv/projects:ro` → container git log erişebilir.
8. OpenClaw ve StudioOS **paralel** çalışacak. StudioOS gradual migration.

---

## 12. Sıradaki Aksiyon

Plan dosyası kaydedildi. Şimdi **Milestone 1 execution**. İlk adım: GitHub repo setup.

Onay komutu: **"go"** → 12 adımı sırayla çalıştırılmaya başlar.
