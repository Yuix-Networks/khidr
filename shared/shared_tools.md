# Shared Tools

You have two classes of tools.

## A — Claude Code's standard dev tools

Agents run on the Claude Agent SDK; Claude Code's built-in tools come
ready to use (parameter descriptions are in the tool schema):

- `Bash` — shell command (runs in the repo directory; default timeout 5 min).
  For long jobs (crawler, build, dev server) pass `run_in_background: true`,
  then read the output / stop the process.
- `Read` / `Write` / `Edit` — read, write, replace a single match in a file
- `Glob` — find files like `**/*.py`
- `Grep` — regex search (ripgrep)
- `WebFetch` — read a URL's content
- `TodoWrite` / `TaskCreate` / `TaskUpdate` / `TaskList` — keep your own
  progress list. These are **not kanban cards**; for cards use
  `create_kanbancard` / `get_kanbancard` / `search_kanbancards` below.

Not available: `AskUserQuestion` (use `ask_user`), `WebSearch` (use `web_search`),
plan mode, worktrees, sub-agents.

## B — Orchestrator-specific tools (the `khidr` MCP server)

They show up in the tool list as `mcp__khidr__<name>` (e.g.
`mcp__khidr__set_status`); below they are described by their short names.
They trigger our side for the kanban card, project memory, .env and
external services:

- Card: `note`, `set_status`, `ask_user`, `reject_task`,
  `get_kanbancard`, `search_kanbancards`, `create_kanbancard`
- Memory: `get_project_memory`, `add_project_memory`, `save_memory`,
  `search_memory`, `list_memories`
- Other: `web_search`, `understand_image`, `get_glitchtip_issue`

### `note(text)`

Leaves a highlighted system note in the chat. For progress summaries,
handoffs and completion reports.

### `set_status(status)`

Changes the column of the current kanban card.
`status ∈ {backlog, todo, in_progress, review, done}`.
When the work is done you call `set_status("review")` — a Telegram
notification for the card goes out automatically.

> `set_status("review")` / `set_status("done")` pass through a repo
> cleanliness guard: they are rejected if there are uncommitted changes,
> you are on a branch other than main, or there are unpushed commits.
> Clean up first, then move to review. For work where you wrote no code
> this check passes automatically.

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
