# StudioOS

> Multi-studio autonomous agent platform — event-driven distributed OS for AI workers.

## Vision

StudioOS is a platform for running multiple autonomous "studios" — each a self-contained business unit operated by a team of AI agents. Studios have missions, KPIs, playbooks, and budgets. Agents have roles, memory, goals, and escalation rules. The platform handles scheduling, coordination, observability, governance, and learning.

**Key principles:**
- Agent = identity (persistent); Run = execution (ephemeral)
- Event-driven: agents communicate via typed events, not prompts
- Transactional: state updates and event publish are atomic (outbox pattern)
- Observable: every action traced, every decision logged
- Governed: explicit budgets, approval gates, human-in-loop

## Architecture

- **Orchestration engine:** LangGraph (Python)
- **State:** PostgreSQL 16 + pgvector (semantic memory)
- **Event bus:** Postgres outbox + Redis Streams (v2)
- **LLM router:** MiniMax M2.7 default, Claude Sonnet strategic
- **Runtime:** Modular monolith, async Python
- **Deployment:** Docker Compose

See [`docs/PLAN.md`](docs/PLAN.md) for full architecture details.

## Status

**Milestone 1 (v0.1.0):** Vertical slice — single studio, 2 agents, core loop end-to-end.

- [x] Repository setup
- [ ] Core schema (7 tables)
- [ ] Runtime skeleton (scheduler, dispatcher, runner, outbox)
- [ ] LangGraph workflows (scout_test, analyst_test)
- [ ] End-to-end test
- [ ] Docker compose deploy

## Quickstart (dev)

```bash
# Prerequisites: Python 3.12+, Docker, uv (https://github.com/astral-sh/uv)
uv sync
cp .env.example .env
docker compose up -d postgres
uv run alembic upgrade head
uv run studioos init
uv run studioos trigger test-scout
uv run studioos inspect --correlation <id>
```

## License

MIT
