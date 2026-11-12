# Khidr

**A self-hosted orchestrator that runs a team of coding agents against your projects, kanban-style.**

Khidr keeps a kanban board per project. Cards land in *To do* — from you, from
the chat UI, from a GlitchTip error or an Uptime Kuma alert — and a dispatcher
hands each card to an agent from your pool. The agent works in a throw-away
sandbox on the task's own git checkout, commits, and moves the card to
*Review*; Khidr publishes the commits and you approve or send the card back.

> Named after Khidr (Hızır), the figure in Anatolian folklore who shows up
> exactly when help is needed.

## Features

- **Any coding CLI is an agent.** Claude Code, OpenAI Codex, Gemini CLI, Qwen
  Code and opencode, driven headless.
- **Subscriptions, not just API keys.** Run agents on your Claude Pro/Max,
  ChatGPT, Google or Qwen login, or on any Anthropic- or OpenAI-compatible API
  (Anthropic, OpenRouter, MiniMax, DeepSeek, a local llama.cpp server…). When a
  provider reports a rate or usage limit, the agent rests and its task goes to
  whoever is free.
- **Tools over MCP.** Khidr's own tools — `set_status`, `ask_user`, `note`,
  kanban, project memory, `server_exec`, `deploy` — are served from one HTTP
  MCP endpoint, authorized per run with a short-lived token.
- **A fresh sandbox per run.** One Docker image, a new container for every run,
  the task's workspace mounted in. Or `local` mode for development.
- **No keys in the sandbox.** Khidr fetches and pushes with its own git
  credentials; agents commit, Khidr rebases and pushes when the card moves to
  Review. Server inspection goes through an allowlisted `server_exec` tool.
- **Kanban + chat web UI**, live over WebSockets. Approve, request changes, or
  talk to the agent mid-task.
- **Alert → card.** GlitchTip and Uptime Kuma webhooks open de-duplicated cards,
  with ignore rules for known noise.
- **Telegram pings** when an agent asks for help or finishes a task.

## How it works

```
                 ┌──────────────────── Khidr host ─────────────────────┐
  browser ──────▶│ web: UI · webhooks · POST /mcp/ (tools, per-run token)│
                 │ orchestrator: dispatch · heartbeats · runs           │──▶ PostgreSQL
                 │ git: cache, workspaces, publish (your credentials)   │
                 └──────────┬───────────────────────────▲──────────────┘
                 docker run │ --rm, per run              │ MCP over HTTP
                            ▼                            │
             ┌──────── sandbox container ────────┐       │
             │ claude | codex | gemini | qwen |   │──────┘
             │ opencode, headless, in /workspace  │──▶ model provider
             └────────────────────────────────────┘    (subscription or API)
```

1. The dispatcher picks a Todo card and an agent with a free slot
   (`max_concurrency`) that isn't resting after a rate limit.
2. Khidr refreshes its git cache of the project and prepares the task's
   workspace (a clone that persists across the task's runs).
3. It starts the agent's CLI in a sandbox with the workspace mounted, a prompt,
   and an MCP config pointing back at Khidr with a token for this run.
4. The CLI's JSON event stream becomes the chat. `ask_user` ends the run and
   waits for your reply; `set_status("review")` makes Khidr rebase the agent's
   commits onto the latest default branch and push them (or push a
   `khidr/task-<id>` branch, per project).

More detail: [docs/architecture.md](docs/architecture.md).

## Install (Docker)

Requirements: Docker with Compose v2. On macOS, Docker Desktop.

```bash
git clone https://github.com/Yuix-Networks/khidr.git && cd khidr
./install.sh
```

`install.sh` creates `.env` with generated secrets, builds the Khidr image and
the agent sandbox image, asks for a web password, and starts PostgreSQL, the
web app and the orchestrator. The UI is at http://localhost:8000; the Django
admin (git credentials, agents) at `/admin/` as user `admin`.

Then:

1. **Agents.** Edit `agents.yaml`, put their API keys or `claude setup-token`
   tokens in `.env`, and load them:
   ```bash
   docker compose exec web python manage.py load_agents
   docker compose up -d        # recreate so .env changes apply
   ```
   Log subscription CLIs (Codex, Gemini, Qwen) in once:
   ```bash
   docker compose exec -it orchestrator python manage.py agent_login <agent>
   ```
2. **Projects.** "+ New project" on the board, with the repository URL. Khidr
   fetches and pushes with the credentials in `./secrets/ssh` (a deploy key,
   `known_hosts`, an ssh `config`) or an HTTPS token env var (set per project in
   the admin). These never reach the agents.
3. **Cards.** Create a card; an agent picks it up within a few seconds.

Update with `git pull && ./install.sh`. Logs: `docker compose logs -f orchestrator`.

The orchestrator container mounts the Docker socket to start sandbox
containers, which is root-equivalent on the host: run Khidr on a machine you
trust it with. Set `KHIDR_PORT` / `KHIDR_BIND` in `.env` to change where the
UI listens (default `127.0.0.1:8000`).

## Development without Docker

Requirements: Python 3.12+, PostgreSQL, git; Docker if you want the `docker`
sandbox.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # DJANGO_SECRET_KEY, PG_*, WEB_PASSWORD_HASH (manage.py set_password)
python manage.py migrate
docker build -t khidr-sandbox:latest -f docker/Dockerfile.sandbox docker/   # or KHIDR_SANDBOX=local
cp agents.yaml.example agents.yaml && python manage.py load_agents
python manage.py runserver           # web UI + MCP endpoint on :8000
python manage.py run_orchestrator    # in another terminal
```

## Agents

| `type` | CLI | Subscription login | API key |
| --- | --- | --- | --- |
| `claude` | Claude Code | `claude setup-token` → env var | Anthropic-compatible `base_url` |
| `codex` | OpenAI Codex | ChatGPT (`agent_login`) | OpenAI-compatible `base_url` |
| `gemini` | Gemini CLI | Google (`agent_login`) | `GEMINI_API_KEY` |
| `qwen` | Qwen Code | Qwen OAuth (`agent_login`) | OpenAI-compatible `base_url` |
| `opencode` | opencode | its own `auth login` | OpenAI-compatible `base_url` |

A docker agent keeps its login in `data/agents/<slug>/home`, mounted as the
sandbox's home on every run. A local agent uses your own home directory.

Using a personal subscription to drive automated agents is subject to each
provider's terms. Use your own account, for your own work.

## Configuration

Environment variables (a `.env` in the repo root is loaded automatically); see
[`.env.example`](.env.example) for the full list.

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Django |
| `PG_HOST`, `PG_PORT`, `PG_NAME`, `PG_USER`, `PG_PASS` | PostgreSQL |
| `WEB_PASSWORD_HASH` | bcrypt hash of the web UI password (`manage.py set_password`) |
| `KHIDR_SANDBOX` | `docker` (default) or `local` |
| `KHIDR_SANDBOX_IMAGE` | sandbox image, default `khidr-sandbox:latest` |
| `KHIDR_INTERNAL_URL` | how sandboxes reach Khidr's `/mcp/` (default `http://127.0.0.1:8000`) |
| `KHIDR_DATA_DIR` | git caches, workspaces, agent homes (default `./data`) |
| `KHIDR_RATE_LIMIT_COOLDOWN_SECONDS` | how long an agent rests after a usage limit |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional notifications |
| `GLITCHTIP_URL`, `GLITCHTIP_API_KEY`, `KHIDR_API_TOKEN` | optional alert integrations |

Per project (admin): `repo_url`, `default_branch`, `publish_mode`, git
credentials (`git_auth`: your host config, a token env var, or a deploy key
file), `deploy_command` + `allow_agent_deploy`, and `server_connection` +
`server_allowed_commands` for `server_exec`.

## Running tests

```bash
python manage.py test
```

Tests use SQLite and need no secrets, services or Docker.

## Status

Khidr grew out of an orchestrator that has run in production at Yuix Networks
for months; this open-source edition reworks how agents run (any CLI, per-run
sandboxes, no keys in the sandbox) and is young. Expect rough edges and
breaking changes. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE)

---

Khidr is built and maintained by [Yuix Networks](https://yuix.org), the team behind
[Nameocean.net](https://nameocean.net).

- [Yuix.org](https://yuix.org)
- [Nameocean.net](https://nameocean.net)
