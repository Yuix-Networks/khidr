# General Rules

Every agent reads this file at the start of every task. Project-independent, stable rules of behavior live here.
**Do not open branches.** Agents always work on `main`.

1. Read the task (title, description, chat history)
2. Plan if needed and write the plan to the chat — ask the user for approval on large or ambiguous work, start right away on small, clear work
3. Always update the repo first, because several AI agents work on this project: `git pull origin main`
4. Do the work — **directly on the main branch**:
   - edit → test → `git add -A && git commit -m "…"` → `git push` → run the project's deploy command (see `Deploy` under Environment; if it is empty, the project has no deploy step)
5. Test your own work; write tests if needed
6. When the work is done, move the task to Review with `set_status("review")` and leave a short summary with `note`
7. If the work is not done or something is broken, keep fixing it; do not move it to Review

> **Note:** `set_status("review")` checks that the repo is clean. The call
> is rejected if there are uncommitted changes, you are on a branch other
> than main, or there are unpushed commits. Clean up and push first, then
> move to review. For work where you wrote no code this check passes,
> because the repo is already clean.

## Testing policy

- Test every piece of work. Choose how to test based on the kind of work (unit tests, manual smoke check, lint, build)
- Writing new test files is not mandatory; do not write them unless the work calls for it
- Do not `git push` before the tests pass

## Getting stuck / asking for help

- If you need help from the user, **call the `ask_user(message)` tool**. Writing
  plain text and stopping is not enough — without the tool call the orchestrator
  does not know you are waiting; it quietly ends the session and starts
  another round. When `ask_user` is called the orchestrator:
  1. puts the task in `waiting_for_user=True` and does not dispatch it
     again (until the user answers),
  2. sends the user a Telegram notification (with the chat URL).
- If the work cannot be done, say why with `ask_user` as well — writing a
  note to the chat and stopping is not enough on its own.
- After 30 minutes without an answer the orchestrator triggers you again — if you are stuck, say so honestly.

## Working environment: Docker container

Your code runs inside a Docker container named `khidr-agent-<slug>`
(your slug), on the host that runs the Khidr orchestrator.

- Home, user and path layout match the host: user `khidr` (uid=1000),
  projects under `/home/khidr/workspace/`, virtualenvs under
  `/home/khidr/.pyenv/versions/<venv>/`. Don't worry about the
  container/host split when writing commands — paths are the same on both sides.
- Mounts:
  - `/home/khidr/workspace/` — your own read-write area. Other agents cannot
    touch it (each agent gets its own `workspace-agents/<slug>/` folder mounted).
  - `/home/khidr/.ssh/` — read-only access to the host's SSH keys.
    `ssh <server>`, `git clone git@...` and the deploy command all work.
  - `/home/khidr/workspace/khidr/` — the orchestrator's own clone that your
    worker runs from. Do not change anything there; if something needs to
    change, open a Khidr card (`create_kanbancard`).
- The `khidr` hostname resolves to the host (the Docker gateway) from inside the container.
  `ssh khidr` reaches the host machine.

## Shell / path rules

- The shell starts in the `Local` project path from Environment. File tools (`Read`, `Write`, `Edit`, `Glob`, `Grep`) need absolute paths — use the project path from Environment.
- ⚠️ `Bash` state (cwd) **persists** across calls. If you ran `cd subdir/` in an earlier command, the next `./manage.py` looks there, not at the project root.
  - Prefer **absolute paths** for scripts you use often: `<python> <local>/manage.py check`.
  - If a command needs a specific directory, wrap it: `cd <local> && <cmd>`.
- Reach the server with the `ssh <connection>` command from Environment. Never `scp` or edit files directly on the server — code goes local → commit → push → deploy command.

## Server / deploy rules

- Do not edit files directly on the server. No `scp`. No manual service restarts.
- Always: local commit → push → the project's deploy command.
- SSH to the server is for inspection/debugging only (reading logs, DB shell). **Never** for code changes.
