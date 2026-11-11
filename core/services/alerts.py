"""
Turn GlitchTip / Uptime Kuma webhooks into Kanban cards without flooding the agents.

GlitchTip: one card per distinct error. GlitchTip opens a fresh issue for the
same error whenever the stack or the interpolated values differ (21 cards for
"TemplateDoesNotExist: terms.html" in 2026-05), so the dedup key is the
project plus the title with values masked, not the issue id. A repeat only
bumps `occurrences` on the first card, whatever its status. Titles matching
an active IgnoreRule never become a card. Only issues GlitchTip saw for the
first time recently open a card: an alert for an issue whose firstSeen is
older than GLITCHTIP_NEW_ISSUE_MAX_AGE_HOURS (a chronic error crossing the
alert threshold again) is dropped. The card asks the agent to triage first;
noise is closed with `reject_task`, which can also add an IgnoreRule.

Uptime Kuma: a DOWN on a monitor listed in some Project.uptime_monitors opens
one card at the top of Todo. It waits UPTIME_DOWN_GRACE_SECONDS before an
agent may take it; an UP inside that window closes it (a blip). Further DOWNs
while the card is open only bump `occurrences`; an UP after pickup is posted
into the card's chat.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta

import httpx
from django.conf import settings
from django.db.models import F, Min
from django.utils import timezone

from core.models import ChatMessage, IgnoreRule, Project, Task

log = logging.getLogger(__name__)

_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`")
# A quoted identifier names the bug ('NoneType', 'user_id', terms.html), keep it;
# anything else in quotes is a runtime value (user input, SQL probes), mask it.
_IDENTIFIER = re.compile(r"[a-z_][\w.\-/]{0,79}")


def _mask_quoted(m: re.Match) -> str:
    inner = next(g for g in m.groups() if g is not None)
    return m.group(0) if _IDENTIFIER.fullmatch(inner) else "<str>"


_MASKS = [
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (_QUOTED, _mask_quoted),
    (re.compile(r"\b0x[0-9a-f]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\d+"), "0"),
]


def normalize_title(title: str) -> str:
    """Error title with the variable parts masked, so repeats compare equal."""
    s = (title or "").lower().strip().rstrip("…").rstrip(".")
    for rx, repl in _MASKS:
        s = rx.sub(repl, s)
    # GlitchTip cuts long titles; an unterminated quote at the end is cut-off value
    s = re.sub(r"['\"`][^'\"`]*$", "<str>", s)
    s = re.sub(r"(<str>\s*)+", "<str>", s)
    return re.sub(r"\s+", " ", s).strip()


def error_fingerprint(project: Project, title: str) -> str:
    return hashlib.sha1(f"{project.pk}:{normalize_title(title)}".encode()).hexdigest()


def uptime_fingerprint(monitor_name: str) -> str:
    return hashlib.sha1(f"uptime:{monitor_name.strip().lower()}".encode()).hexdigest()


def matching_ignore_rule(project: Project, title: str) -> IgnoreRule | None:
    rules = IgnoreRule.objects.filter(is_active=True).filter(project__isnull=True) | \
        IgnoreRule.objects.filter(is_active=True, project=project)
    for rule in rules:
        try:
            if re.search(rule.pattern, title or "", re.IGNORECASE):
                return rule
        except re.error:
            log.warning("ignore rule %s has an invalid regex: %r", rule.pk, rule.pattern)
    return None


def _bump(task: Task) -> None:
    Task.objects.filter(pk=task.pk).update(
        occurrences=F("occurrences") + 1, last_seen_at=timezone.now(),
    )


def _top_position(project: Project) -> float:
    lowest = Task.objects.filter(project=project, status=Task.STATUS_TODO).aggregate(m=Min("position"))["m"]
    return (lowest or 0) - 1


_ISSUE_ID = re.compile(r"/issues/(\d+)")


def glitchtip_issue_first_seen(issue_url: str) -> datetime | None:
    """firstSeen of the GlitchTip issue, or None when it can't be looked up."""
    m = _ISSUE_ID.search(issue_url or "")
    api_key = getattr(settings, "GLITCHTIP_API_KEY", "")
    if not m or not api_key:
        return None
    base = getattr(settings, "GLITCHTIP_URL", "https://glitchtip.example.com").rstrip("/")
    try:
        resp = httpx.get(
            f"{base}/api/0/issues/{m.group(1)}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        return datetime.fromisoformat(resp.json()["firstSeen"].replace("Z", "+00:00"))
    except Exception as exc:  # noqa: BLE001
        log.warning("glitchtip firstSeen lookup failed for %s: %s", issue_url, exc)
        return None


_TRIAGE = """
---
**Triage first:** This card was opened automatically from an error GlitchTip saw for the first time; it may not be real work.
Look at the issue (`get_glitchtip_issue`) and the related code/logs, then decide:
- If it is a real bug to fix in the code, fix it and ship it through the normal flow.
- If it is noise (transient/one-off: mid-deploy, network/provider outage, bot/browser noise, already fixed, \
unrelated to the code), call `reject_task` without changing code: give a short reason; if it would be noise again \
when it recurs, pass an `ignore_pattern` regex matching the title so no card is opened for it again."""


def ingest_glitchtip(
    project: Project, title: str, description: str, issue_url: str = "",
) -> tuple[str, Task | None]:
    """Returns ("created" | "duplicate" | "ignored" | "not_new", task)."""
    rule = matching_ignore_rule(project, title)
    if rule:
        IgnoreRule.objects.filter(pk=rule.pk).update(hits=F("hits") + 1, last_hit_at=timezone.now())
        return "ignored", None

    fp = error_fingerprint(project, title)
    existing = Task.objects.filter(project=project, fingerprint=fp).order_by("created_at").first()
    if existing:
        _bump(existing)
        return "duplicate", existing

    first_seen = glitchtip_issue_first_seen(issue_url)
    max_age = timedelta(hours=float(getattr(settings, "GLITCHTIP_NEW_ISSUE_MAX_AGE_HOURS", 24)))
    if first_seen is not None and timezone.now() - first_seen > max_age:
        return "not_new", None

    task = Task.objects.create(
        project=project,
        title=title[:512],
        description=description + "\n" + _TRIAGE,
        status=Task.STATUS_TODO,
        complexity=3,
        source=Task.SOURCE_GLITCHTIP,
        fingerprint=fp,
        last_seen_at=timezone.now(),
    )
    return "created", task


def project_for_monitor(monitor_name: str) -> Project | None:
    wanted = monitor_name.strip().lower()
    for p in Project.objects.filter(is_active=True).exclude(uptime_monitors=""):
        if wanted in {n.strip().lower() for n in p.uptime_monitors.split(",") if n.strip()}:
            return p
    return None


_OPEN = [Task.STATUS_BACKLOG, Task.STATUS_TODO, Task.STATUS_IN_PROGRESS]


_SERVER_MONITOR = re.compile(r"^Server (\S+)$")


def _ship_line(project: Project) -> str:
    """How a code fix reaches the server, phrased from the project's deploy_command."""
    deploy = (project.deploy_command or "").strip()
    flow = f"commit → push → `{deploy}`" if deploy else "commit → push → the project's deploy step"
    return (
        f"If the cause is application code, fix it and ship it through the normal flow ({flow}). "
        "No code edits on the server; SSH is for inspection only."
    )


def _server_description(project: Project, name: str, alias: str, msg: str, when: str) -> str:
    """A "Server <alias>" push monitor: the server checks itself every minute and
    pushes; DOWN = its check failed (msg says why) or no push arrived."""
    return "\n".join([
        f"**Uptime Kuma:** `{name}` DOWN (the server's own once-a-minute check)",
        f"**Kuma message:** {msg}" if msg else "",
        f"**Time:** {when}" if when else "",
        "",
        f"Server `{alias}` failed its own check or could not push at all "
        "(server/network/cron down). In a redundant pair the site may stay up from the other node; fix it anyway:",
        f"1. Connect with `ssh {alias}` (if you can't, the server or network is down: write up your findings, move to review).",
        "2. Run the check by hand — whatever the push job runs — to see what is broken.",
        "3. Services (supervisorctl/systemctl), nginx and application logs, disk/RAM, PostgreSQL.",
        f"4. {_ship_line(project)}",
        "Once the check passes again, Uptime Kuma posts it to this card's chat.",
    ]).replace("\n\n\n", "\n\n")


def _uptime_description(project: Project, monitor: dict, msg: str, when: str) -> str:
    m = _SERVER_MONITOR.match((monitor.get("name") or "").strip())
    if m and not monitor.get("url"):
        return _server_description(project, monitor.get("name"), m.group(1), msg, when)
    url = monitor.get("url") or ""
    server = project.server_connection or "(Project.server_connection is empty)"
    return "\n".join([
        f"**Uptime Kuma:** `{monitor.get('name')}` DOWN",
        f"**URL:** {url}" if url else "",
        f"**Kuma message:** {msg}" if msg else "",
        f"**Time:** {when}" if when else "",
        "",
        "The site is unreachable. Diagnose it now:",
        f"1. Check the status with `curl -sSIL --max-time 20 {url or '<url>'}`, plus DNS (`dig`) and TLS.",
        f"2. Look at the server (`{server}`): service state (supervisorctl/systemctl/docker ps), "
        "nginx and application logs, disk/RAM.",
        f"3. {_ship_line(project)}",
        "4. If the cause is outside the code (DNS, provider, network, hardware, certificate), write up your findings and move to review.",
        "Once the site is back up, Uptime Kuma posts it to this card's chat.",
    ]).replace("\n\n\n", "\n\n")


def ingest_uptime(payload: dict) -> tuple[str, Task | None]:
    """Returns ("created" | "duplicate" | "recovered" | "resolved" | "ignored", task)."""
    monitor = payload.get("monitor") or {}
    heartbeat = payload.get("heartbeat") or {}
    name = (monitor.get("name") or "").strip()
    if not name or not heartbeat:
        # Kuma's "Test" button sends only {"msg": ...}
        return "ignored", None
    status = heartbeat.get("status")
    msg = (heartbeat.get("msg") or "").strip()
    when = heartbeat.get("localDateTime") or heartbeat.get("time") or ""

    project = project_for_monitor(name)
    if project is None:
        return "ignored", None

    fp = uptime_fingerprint(name)
    open_task = (
        Task.objects.filter(project=project, fingerprint=fp, status__in=_OPEN)
        .order_by("-created_at").first()
    )

    if status == 1:  # UP
        if open_task is None:
            return "ignored", None
        if open_task.status == Task.STATUS_TODO and open_task.current_agent_id is None:
            Task.objects.filter(pk=open_task.pk).update(status=Task.STATUS_DONE)
            ChatMessage.objects.create(
                task=open_task, role=ChatMessage.ROLE_SYSTEM,
                content=f"[uptime] `{name}` came back up before an agent took it ({msg or 'UP'}); card closed.",
            )
            return "resolved", open_task
        ChatMessage.objects.create(
            task=open_task, role=ChatMessage.ROLE_SYSTEM,
            content=f"[uptime] `{name}` is back up ({msg or 'UP'}, {when}).",
        )
        return "recovered", open_task

    if status != 0:  # 2 = pending (retrying), 3 = maintenance
        return "ignored", None

    if open_task:
        _bump(open_task)
        return "duplicate", open_task

    grace = int(getattr(settings, "UPTIME_DOWN_GRACE_SECONDS", 60))
    task = Task.objects.create(
        project=project,
        title=f"[DOWN] {name} unreachable"[:512],
        description=_uptime_description(project, monitor, msg, when),
        status=Task.STATUS_TODO,
        position=_top_position(project),
        complexity=1,
        source=Task.SOURCE_UPTIME,
        fingerprint=fp,
        last_seen_at=timezone.now(),
        next_dispatch_after=timezone.now() + timedelta(seconds=grace),
    )
    return "created", task
