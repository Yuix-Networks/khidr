# Khidr

**A self-hosted orchestrator that runs a team of LLM coding agents against your projects, kanban-style.**

Khidr keeps a kanban board per project. Cards land in *To do* — from you, from
the chat UI, from a GlitchTip error or an Uptime Kuma alert — and a dispatcher
hands each card to an agent from your pool. Every agent works in its own Docker
container (its "personal computer"), clones the project, does the work, commits,
and moves the card to *Review* for you to approve or send back.

> Named after Khidr (Hızır), the figure in Anatolian folklore who shows up
> exactly when help is needed.

## Features

- **Agent pool, any model.** Agents run on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
  against any Anthropic Messages API–compatible endpoint: Anthropic, OpenRouter,
  MiniMax, or a local `llama.cpp` server. Configure them in `agents.yaml`.
- **Kanban + chat web UI.** Live board and per-task chat over WebSockets
  (Django Channels). Approve, request changes, or talk to the agent mid-task.
- **Orchestrator tools.** Agents get an in-process MCP server with tools like
  `set_status`, `ask_user`, `note`, `create_kanbancard`, project memory, and web
  search/fetch.
- **Alert → card.** GlitchTip and Uptime Kuma webhooks open de-duplicated cards,
  with ignore rules for known noise.
- **Guard rails.** Heartbeat timeouts, requeue cooldowns, a clean-git-tree check
  before review, secret scrubbing from the agent's shell environment, context
  compaction for small-context models.
- **Telegram pings** when an agent asks for help or finishes a task.

## Architecture

```
            ┌──────────────── Khidr host ────────────────┐
 browser ──▶│ daphne (web UI, API, webhooks)             │
            │ run_orchestrator (dispatch, heartbeats)    │──▶ PostgreSQL
            └───────────────┬────────────────────────────┘
                            │ docker exec
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
   khidr-agent-a     khidr-agent-b     khidr-agent-c      (one container per agent)
   agent_worker ──▶ Claude Agent SDK ──▶ your model endpoint
```

## Quick start (development)

Requirements: Python 3.12+, PostgreSQL, Docker (for agent containers).

```bash
git clone https://github.com/Yuix-Networks/khidr.git && cd khidr
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then fill it in (see below)
cp agents.yaml.example agents.yaml
python manage.py migrate
python manage.py load_agents
python manage.py runserver       # web UI on http://localhost:8000
python manage.py run_orchestrator --tick 5   # in another terminal
```

Set the web UI password:

```bash
python -c "from chat.auth import hash_password; print(hash_password('choose-a-password'))"
```

and put the output in `WEB_PASSWORD_HASH` in `.env`.

Build the agent image, then start and provision one container per agent with
`python manage.py provision_agents` — see
[docs/agent-containers.md](docs/agent-containers.md).

## Configuration

All configuration is environment variables (a `.env` file in the repo root is
loaded automatically). See [`.env.example`](.env.example) for the full list.
The important ones:

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key (required in production) |
| `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Comma-separated hosts / origins |
| `PG_HOST`, `PG_PORT`, `PG_NAME`, `PG_USER`, `PG_PASS` | PostgreSQL connection |
| `WEB_PASSWORD_HASH` | bcrypt hash of the web UI password |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optional notifications |
| `GLITCHTIP_URL`, `GLITCHTIP_API_KEY` | Optional GlitchTip integration |
| `KHIDR_API_TOKEN` | Secret path segment for the Uptime Kuma webhook |
| `SENTRY_DSN` | Optional error reporting for Khidr itself |

## Running tests

```bash
python manage.py test
```

Tests use SQLite and need no secrets or running services.

## Status

Khidr has run in production at Yuix Networks for months, but it is young as an
open-source project: parts of the UI and the agent prompts (`shared/*.md`) are
still in Turkish, and container provisioning is manual. Contributions welcome —
see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE)

---

Khidr is built and maintained by [Yuix Networks](https://yuix.org), the team behind
[Nameocean.net](https://nameocean.net).

- [Yuix.org](https://yuix.org)
- [Nameocean.net](https://nameocean.net)
