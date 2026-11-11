# General Rules

Every agent reads this at the start of every task. Project-independent, stable rules live here.

## Flow

1. Read the task (title, description, chat history).
2. Plan if needed and post the plan with `note`. Ask the user (`ask_user`) before large or ambiguous work; start right away on small, clear work.
3. Do the work in your working directory (the task's own git checkout).
4. Test it (unit tests, a smoke run, lint or build — whatever fits the work).
5. Commit: `git add -A && git commit -m "…"`. Several small commits are fine.
6. Call `set_status("review")` and leave a short summary with `note`.
7. If the work isn't done or something is broken, keep going; don't move it to Review.

## Git: you commit, Khidr publishes

- You have **no push access and no SSH keys**. Don't try `git push`, and don't
  ask for credentials.
- `set_status("review")` publishes your work: Khidr rebases your commits onto the
  latest default branch and pushes them (or pushes a task branch, depending on
  the project). It refuses — and tells you why — when:
  - the workspace has uncommitted changes → commit or discard them;
  - your commits conflict with upstream → `git fetch origin && git rebase origin/<branch>`,
    resolve, commit, call `set_status` again.
- `git fetch origin` works (read-only) when you need the latest upstream code.
- For work without code changes, `set_status("review")` just moves the card.

## Asking the user

- If you need the user, **call `ask_user(message)`**. Writing plain text and
  stopping is not enough — without the tool call Khidr doesn't know you're waiting.
  `ask_user` ends your run, marks the card as waiting and notifies the user; you'll
  be resumed with their answer.
- If the work can't be done, say why with `ask_user` too.

## Your environment

- You run inside a sandbox: usually a fresh Docker container per run, with your
  working directory mounted at `/workspace`. Anything outside the working
  directory is gone after the run; the working directory itself persists for the
  whole task, so uncommitted work survives between runs.
- Installed: git, Python 3 with `uv` and `pip`, Node.js, ripgrep, build tools.
  Create virtualenvs inside the working directory (e.g. `uv venv .venv`) and keep
  them out of commits (`.gitignore`).
- Servers: you can't SSH anywhere. To look at a project's server (logs, service
  status, disk) use the `server_exec` tool — read-only commands from an allowlist.
- Deploys: only via the `deploy` tool, and only when the project allows it.
  Otherwise the user deploys.

## Shell / path rules

- The shell's working directory persists across calls. Prefer absolute paths, or
  `cd <dir> && <cmd>` when a command needs a specific directory.
- Never edit files on servers directly.

## Testing policy

- Test every piece of work; pick the kind of test that fits.
- New test files aren't mandatory; write them when the work calls for it.
- Don't call `set_status("review")` before the tests pass.
