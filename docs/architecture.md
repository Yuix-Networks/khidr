# Architecture

Khidr is a Django app with two processes — the web server and the
orchestrator daemon — sharing one PostgreSQL database. Agents run outside
both, in sandboxes, and talk back to Khidr only over MCP.

## Pieces

| Piece | Where | What it does |
| --- | --- | --- |
| Web (`runserver` / daphne) | host | kanban + chat UI, webhooks, the `/mcp/` endpoint |
| Orchestrator (`run_orchestrator`) | host | dispatch, heartbeats, one thread per running task |
| Git cache + workspaces | host, `KHIDR_DATA_DIR` | `git/<project>.git` (bare cache), `workspaces/task-<id>/` |
| Harness | sandbox | the coding CLI: `claude`, `codex`, `gemini`, `qwen`, `opencode` |
| Sandbox | docker container per run, or the host (`local`) | runs the harness in the task workspace |

Code map: `core/services/dispatcher.py` (who works on what),
`core/services/session.py` (a task's run lifecycle),
`core/services/agents/runner.py` (one run), `harnesses.py` (per-CLI argv,
env, config files and event parsing), `sandbox.py` (docker / local),
`core/mcp.py` (the MCP endpoint), `core/services/tools.py` (the tools),
`core/services/workspace.py` (git), `core/services/server_exec.py`.

## A run

1. **Dispatch.** Each tick, for each project, the dispatcher takes the first
   Todo card (not waiting for the user, not in cooldown) and an agent that is
   assigned to the project, is active, has `talent >= complexity`, has fewer
   than `max_concurrency` tasks in progress, isn't resting after a rate limit
   (`cooldown_until`), and whose sandbox is available. A card pinned to an
   agent (`assigned_agent`) waits for that agent.
2. **Workspace.** Khidr fetches the project's remote into its bare cache with
   the project's credentials, then clones the cache into the task's workspace
   (first run) or fast-forwards it (later runs, if nothing is unpublished).
   The workspace's `origin` is the cache.
3. **Run token.** A `Run` row is created with a random token (only its hash is
   stored). The token authorizes MCP calls for this task until the run ends.
4. **Harness.** The harness writes its config into the run directory — MCP
   server URL and token, system prompt, provider settings — and returns the
   argv, env and the prompt (the card plus its chat history).
5. **Sandbox.** `docker run --rm` with the workspace at `/workspace`, the run
   directory read-only at `/khidr/run`, the agent's home at `/home/khidr`, and
   the git cache read-only at its host path. Or a local process with a scrubbed
   environment.
6. **Events.** The CLI's JSON lines become text, tool calls and context usage
   in the task chat. If the agent calls `ask_user`, the run is stopped right
   after the tool result and the card waits for the user.
7. **End.** The run ends with one of: `finished`, `waiting_for_user`,
   `max_iterations` (requeued; the workspace keeps the work), `rate_limited`
   (the agent rests for `KHIDR_RATE_LIMIT_COOLDOWN_SECONDS`, the card goes back
   to Todo), or `error` (retried with a cooldown, parked after three in a row
   or immediately for auth/setup errors). The token stops working.

## Publishing

Agents can't push: the sandbox has no credentials and `origin` is a
read-only local cache. When the agent calls `set_status("review")` (or
`done`), the tool — running in Khidr, on the host — checks that the
workspace is clean, refreshes the cache, and then:

- `publish_mode = default_branch`: rebases the agent's commits onto the latest
  default branch and pushes it. If the rebase conflicts it aborts and tells the
  agent to `git fetch origin && git rebase origin/<branch>` and resolve.
- `publish_mode = task_branch`: pushes `khidr/task-<id>` (force) for you to
  open a pull request.

Credentials (`Project.git_auth`): `host` uses the orchestrator user's own git
config and SSH agent; `token` answers HTTPS prompts with the value of
`git_token_env`; `ssh_key` uses a dedicated key file (e.g. a deploy key) via
`GIT_SSH_COMMAND`.

## Deploys and servers

- `deploy` runs the project's `deploy_command` on the host, in the task
  workspace, only if `allow_agent_deploy` is on.
- `server_exec(command)` runs a read-only command on `server_connection` over
  the host's SSH. Every piped segment must start with an allowlisted prefix
  (`server_allowed_commands`, or a built-in read-only list); `;`, `&&`, `||`,
  redirects, `$(…)` and secret files are refused. Every call is logged in the
  task chat.

## What the sandbox can see

| | docker | local |
| --- | --- | --- |
| Task workspace | read-write | read-write |
| Git cache | read-only | readable (host user) |
| Agent home (CLI login) | `data/agents/<slug>/home` | your home |
| Git / SSH credentials | no | your user's — no isolation |
| Database, Django settings | no | not in the env, but readable on disk |
| Network | yes | yes |

The agent's own provider credentials (subscription login or API key) are
necessarily visible to the CLI, and therefore to commands the agent runs.
Give each agent its own key where you can, and treat `local` as trusted-only.

## MCP endpoint

`POST /mcp/` implements the request/response half of MCP Streamable HTTP:
`initialize`, `ping`, `tools/list`, `tools/call` (plus empty
`resources/list` / `prompts/list`). No SSE stream (GET → 405). Auth is
`Authorization: Bearer <run token>`. From Docker, `KHIDR_INTERNAL_URL`'s
`localhost` becomes `host.docker.internal`.
