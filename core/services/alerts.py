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
**Önce triyaj:** Bu kart GlitchTip'te ilk kez görülen bir hatadan otomatik açıldı; gerçek bir iş olmayabilir.
Issue'yu (`get_glitchtip_issue`) ve ilgili kodu/logları incele, sonra karar ver:
- Kodda düzeltilecek gerçek bir hataysa düzelt ve normal akışla gönder.
- Gereksizse (geçici/tek seferlik: deploy anı, ağ/sağlayıcı kesintisi, bot/tarayıcı gürültüsü, zaten düzeltilmiş, \
kodla ilgisi yok) kod değiştirmeden `reject_task` çağır: kısa gerekçe yaz; tekrar gelirse yine gürültü olacaksa \
`ignore_pattern` ile başlığa uyan bir regex ver ki bir daha kart açılmasın."""


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


def _server_description(name: str, alias: str, msg: str, when: str) -> str:
    """A "Server <alias>" push monitor (servers repo `fab kuma-push`): the server checks itself
    every minute and pushes; DOWN = its check failed (msg says why) or no push arrived."""
    return "\n".join([
        f"**Uptime Kuma:** `{name}` DOWN (sunucunun kendi dakikalık kontrolü)",
        f"**Kuma mesajı:** {msg}" if msg else "",
        f"**Zaman:** {when}" if when else "",
        "",
        f"`{alias}` sunucusu kendi kontrolünü geçemedi ya da hiç push gönderemedi "
        "(sunucu/ağ/cron kapalı). HA çiftinde site diğer CT'den açık kalabilir; yine de çöz:",
        f"1. `ssh {alias}` ile bağlan (bağlanamıyorsan sunucu ya da ağ kapalı: bulguları yaz, review'a al).",
        "2. Kontrolü elle çalıştır: `. /etc/kuma-push.env && bash -c \"$KUMA_CHECK\"` — neyin bozuk olduğunu söyler.",
        "3. Servisler (supervisorctl/systemctl), nginx ve uygulama logları, disk/RAM, PostgreSQL "
        "(çiftlerde `fab pair-pg-state`).",
        "4. Neden uygulama koduysa düzelt ve normal akışla gönder (commit → push → fab deploy). "
        "Sunucuda kod düzenleme yok; SSH sadece inceleme için.",
        "Kontrol tekrar geçince Uptime Kuma bunu bu kartın sohbetine yazar.",
    ]).replace("\n\n\n", "\n\n")


def _uptime_description(project: Project, monitor: dict, msg: str, when: str) -> str:
    m = _SERVER_MONITOR.match((monitor.get("name") or "").strip())
    if m and not monitor.get("url"):
        return _server_description(monitor.get("name"), m.group(1), msg, when)
    url = monitor.get("url") or ""
    server = project.server_connection or "(Project.server_connection boş)"
    return "\n".join([
        f"**Uptime Kuma:** `{monitor.get('name')}` DOWN",
        f"**URL:** {url}" if url else "",
        f"**Kuma mesajı:** {msg}" if msg else "",
        f"**Zaman:** {when}" if when else "",
        "",
        "Site erişilemiyor. Hemen teşhis et:",
        f"1. `curl -sSIL --max-time 20 {url or '<url>'}` ile durumu, DNS'i (`dig`) ve TLS'i kontrol et.",
        f"2. Sunucuya bak (`{server}`): servis durumu (supervisorctl/systemctl/docker ps), "
        "nginx ve uygulama logları, disk/RAM.",
        "3. Neden uygulama koduysa düzelt ve normal akışla gönder (commit → push → fab deploy). "
        "Sunucuda kod düzenleme yok; SSH sadece inceleme için.",
        "4. Kod dışı bir nedense (DNS, sağlayıcı, ağ, donanım, sertifika) bulguları yaz ve review'a al.",
        "Site tekrar ayağa kalkınca Uptime Kuma bunu bu kartın sohbetine yazar.",
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
                content=f"[uptime] `{name}` ajan almadan tekrar ayağa kalktı ({msg or 'UP'}); kart kapatıldı.",
            )
            return "resolved", open_task
        ChatMessage.objects.create(
            task=open_task, role=ChatMessage.ROLE_SYSTEM,
            content=f"[uptime] `{name}` tekrar ayakta ({msg or 'UP'}, {when}).",
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
        title=f"[DOWN] {name} erişilemiyor"[:512],
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
