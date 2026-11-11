# Shared Tools

You have two classes of tools.

## A — Your CLI's own tools

You run inside a coding-agent CLI (Claude Code, Codex, Gemini CLI, Qwen Code or
opencode). Use its built-in tools as usual: shell, file read/write/edit, search,
web fetch, its own todo list. Its todo list is **not** the kanban — for cards use
`create_kanbancard` / `get_kanbancard` / `search_kanbancards` below.

Don't use your CLI's own "ask the user" / plan-mode / sub-agent features: nobody
is watching the terminal. Use `ask_user` instead.

## B — Khidr's tools (the `khidr` MCP server)

Depending on the CLI they appear as `mcp__khidr__<name>`, `khidr__<name>` or just
`<name>`; below they are described by their short names. They run on Khidr's side,
not in your sandbox:

- Card: `note`, `set_status`, `ask_user`, `reject_task`,
  `get_kanbancard`, `search_kanbancards`, `create_kanbancard`
- Memory: `get_project_memory`, `add_project_memory`, `save_memory`,
  `search_memory`, `list_memories`
- Ship & inspect: `deploy`, `server_exec`
- Other: `web_search`, `understand_image`, `get_glitchtip_issue`

### `note(text)`

Leaves a highlighted system note in the chat. For progress summaries,
handoffs and completion reports.

### `set_status(status)`

Changes the column of the current kanban card.
`status ∈ {backlog, todo, in_progress, review, done}`.
When the work is done you call `set_status("review")` — a Telegram
notification for the card goes out automatically.

> `set_status("review")` / `set_status("done")` publish your commits: Khidr
> rebases them onto the latest default branch and pushes them with its own
> credentials. They are refused when there are uncommitted changes or the
> rebase conflicts — the error says what to do. For work without code changes
> they just move the card.

### `ask_user(message)`

Ask the user a question and **stop**. This call:
1. Puts the card in `waiting_for_user=True` mode — the orchestrator does not requeue it.
2. Sends the user an **instant notification over Telegram** (with the chat link).

Writing plain text and stopping is not enough — without the tool call the
orchestrator counts the session as normally "finished" and silently requeues it.

### `get_kanbancard(card_id, recent_messages=5)`

Returns the details of another kanban card: title, description,
project, status, complexity, assigned/current agent, and the last N
messages (max 20). Read-only, cross-project OK (it can look at cards
outside your own project). Use it when the current card references or
depends on another card.

Example output:
```
# Kanban card #18: Redesign the forum page
- Project: Acme
- Status: Done  (complexity: 3)
- Assigned: claude-sonnet
- Currently working: (none)

## Description
...

## Last 5 chat messages
- [claude-sonnet] ...
- [user] ...
```

### `search_kanbancards(query, max_results=20, include_done=true)`

Case-insensitive substring search over the title + description of cards
in the **same project**. Returns one line per card in `updated_at desc`
order, formatted as `#<id> [<status>] <title>`.

- `max_results`: 1–50 (default 20). When exceeded: `... truncated at N`.
- `include_done`: pass false to leave out Done cards (active work only).

Ideal for checking, **before** opening a `create_kanbancard`, whether a
card for the same work was already done or is in progress.

Example output:
```
Found 2 kanban card(s) matching 'admin stats' in Acme:
- #42 [in_progress] Add admin stats counter
- #31 [done] Stats panel cleanup
```

### `get_project_memory()` / `add_project_memory(text)`

Project-level **long-term shared memory**. Reads the `description` field
of your current project (`get_project_memory`) or appends to it
(`add_project_memory`).

Don't confuse it with `note`: a `note` lands in the **current card's**
chat — the trail you leave while finishing that task. `add_project_memory`
is for things **other agents working on this project later** need to know.

Typical use:

- You moved a setting in `settings.py` → other agents will look for its
  new place → `add_project_memory("PG_* is now read from .env, settings.py no longer hardcodes it")`.
- The project uses the same deploy command everywhere and your card did
  too → don't write it, it's already known.
- Discoveries: genuinely useful facts such as "the crawler lives in
  `~/workspace/myproject/crawl.py` and now also pulls standings into the DB".

Style:

- 1-2 sentences. Long write-ups go to the card, not the description.
- Each entry is automatically prefixed with `[2026-04-22 14:30 · <slug>]`; don't write the date yourself.
- Entries accumulate as bullets under an `## Agent memory` heading — the
  description itself stays clean.

Before starting a new card, call `get_project_memory()` and look at the last
10-15 lines: what did earlier agents learn about this environment?

### `create_kanbancard(title, description?, complexity=3, status="todo", assigned_agent?)`

Creates a new kanban card **in the same project** (an agent cannot create
work in another project). For the subtask flow: if the current work is
large, split it; the orchestrator picks up the sub-cards in order. Returns
the new card's id.

- `status` is only `todo` or `backlog` — the orchestrator manages the
  rest of the lifecycle.
- `complexity` 1–5; an agent needs `talent >= complexity` to be
  selectable by the dispatcher.
- `assigned_agent`: optional slug. If given, only that agent takes it;
  if blank, the dispatcher picks a suitable one.
- A `[created by card-<parent>]` system message is posted automatically
  to the new card's chat (provenance).

## How do I finish?

- Code + tests done → summary with `note` + `set_status("review")`.
- You can add a plain-text summary on top if you like; it lands in the chat anyway.

## How do I ask for help?

- `ask_user("this needs clarifying: …")`. You get pinged on Telegram.

## Recommended order for the subtask flow

1. `search_kanbancards("keyword")` — check for duplicates.
2. If there is one, look at it with `get_kanbancard(<id>)`; if continuing there
   is better, don't create new work on top of it.
3. Otherwise open the new subtask with `create_kanbancard(...)`. If you put the
   id in your `note`, the user can jump straight to that card from the UI.

### `deploy()`

Runs the project's deploy command on Khidr's host, after your work is
published. Only works when the project allows agent deploys; otherwise say in
your note that the change is ready to deploy.

### `server_exec(command)`

Runs a read-only command on the project's server over Khidr's own SSH: logs
(`tail -n 200 /var/log/app.log`, `journalctl -u app --since "1 hour ago"`),
status (`systemctl status app`, `docker ps`), disk (`df -h`). Commands must start
with an allowlisted prefix; pipes between allowlisted commands are fine, but no
`;`, `&&`, redirects or secret files (`.env`, SSH keys).

