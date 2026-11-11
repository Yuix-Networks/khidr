"""
WebSocket consumer for per-task live chat.

Approach: each connected socket runs a small polling loop inside its own
asyncio task. Poll cadence is ~1s. This avoids needing a shared channel
layer (the orchestrator daemon runs in a different OS process than the
runserver, so an InMemoryChannelLayer can't deliver to it).

If we ever want sub-second updates, swap in channels-redis + reconnect
to group_send — the signals module already emits events; it just has
nowhere cross-process to go right now.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.db.models import Max
from django.utils import timezone

from core.models import ChatMessage, Task

from .auth import SESSION_KEY

log = logging.getLogger(__name__)

POLL_INTERVAL = 1.0


class TaskChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.task_id = int(self.scope["url_route"]["kwargs"]["task_id"])

        # Session access pulls the row from the DB — that is a sync ORM call
        # and must not run in the event loop directly.
        if not await self._session_authenticated():
            await self.close(code=4401)
            return

        await self.accept()
        self.last_id = await self._max_message_id()
        self._task_snapshot: dict | None = None
        self._stop = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def disconnect(self, code):
        if hasattr(self, "_stop"):
            self._stop.set()
        if hasattr(self, "_poll_task"):
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass

    async def receive(self, text_data=None, bytes_data=None):
        # Read-only for now. User actions go through HTTP endpoints.
        pass

    # --- polling ------------------------------------------------------------

    async def _poll_loop(self):
        try:
            while not self._stop.is_set():
                try:
                    await self._emit_new_messages()
                    await self._emit_task_state()
                except Exception:  # noqa: BLE001
                    log.exception("poll iteration failed (task %s)", self.task_id)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _emit_new_messages(self):
        rows = await self._fetch_new_messages(self.last_id)
        for r in rows:
            payload = {
                "type": "chat",
                "id": r["id"],
                "role": r["role"],
                "agent": r["agent__slug"],
                "content": r["content"] or "",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            await self.send(text_data=json.dumps(payload, ensure_ascii=False))
            if r["id"] > self.last_id:
                self.last_id = r["id"]

    async def _emit_task_state(self):
        snap = await self._fetch_task_snapshot()
        if snap is None:
            return
        if snap == self._task_snapshot:
            return
        self._task_snapshot = snap
        await self.send(text_data=json.dumps({
            "type": "task",
            "id": self.task_id,
            "status": snap.get("status"),
            "current_agent": snap.get("current_agent__slug"),
            "assigned_agent": snap.get("assigned_agent__slug"),
            "branch_name": snap.get("branch_name") or "",
            "waiting_for_user": snap.get("waiting_for_user"),
            "context_used": snap.get("context_used") or 0,
            "context_limit": snap.get("context_limit") or 0,
            "tokens_per_second": snap.get("tokens_per_second") or 0.0,
            "agent_status": snap.get("agent_status") or "idle",
            "agent_status_detail": snap.get("agent_status_detail") or "",
        }, ensure_ascii=False))

    # --- DB helpers (sync, wrapped) ----------------------------------------

    @database_sync_to_async
    def _session_authenticated(self) -> bool:
        session = self.scope.get("session")
        if session is None:
            return False
        # Reading session keys triggers a DB query — safe here inside
        # database_sync_to_async.
        return bool(session.get(SESSION_KEY))

    @database_sync_to_async
    def _max_message_id(self) -> int:
        mx = ChatMessage.objects.filter(task_id=self.task_id).aggregate(m=Max("id"))["m"]
        return mx or 0

    @database_sync_to_async
    def _fetch_new_messages(self, since_id: int):
        return list(
            ChatMessage.objects.filter(task_id=self.task_id, id__gt=since_id)
            .order_by("id")
            .values("id", "role", "content", "created_at", "agent__slug")
        )

    @database_sync_to_async
    def _fetch_task_snapshot(self):
        return (
            Task.objects.filter(pk=self.task_id)
            .values(
                "status",
                "current_agent__slug",
                "assigned_agent__slug",
                "branch_name",
                "waiting_for_user",
                "context_used",
                "context_limit",
                "tokens_per_second",
                "agent_status",
                "agent_status_detail",
            )
            .first()
        )


class ProjectBoardConsumer(AsyncWebsocketConsumer):
    """
    Reactive kanban for a project — pushes a task-list snapshot on every
    change so the frontend can reconcile the board without a page reload.
    """

    async def connect(self):
        self.project_id = int(self.scope["url_route"]["kwargs"]["project_id"])

        if not await self._session_authenticated():
            await self.close(code=4401)
            return

        await self.accept()
        self._last_snapshot_hash: int | None = None
        self._stop = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def disconnect(self, code):
        if hasattr(self, "_stop"):
            self._stop.set()
        if hasattr(self, "_poll_task"):
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass

    async def receive(self, text_data=None, bytes_data=None):
        pass  # read-only

    @database_sync_to_async
    def _session_authenticated(self) -> bool:
        session = self.scope.get("session")
        if session is None:
            return False
        return bool(session.get(SESSION_KEY))

    @database_sync_to_async
    def _fetch_tasks(self):
        _done_cutoff = timezone.now() - timedelta(days=3)
        rows = list(
            Task.objects.filter(project_id=self.project_id)
            .exclude(status=Task.STATUS_DONE, updated_at__lt=_done_cutoff)
            .values(
                "id",
                "title",
                "status",
                "complexity",
                "position",
                "branch_name",
                "waiting_for_user",
                "current_agent__slug",
                "assigned_agent__slug",
                "created_at",
            )
            .order_by("status", "position", "id")
        )
        return rows

    async def _poll_loop(self):
        try:
            while not self._stop.is_set():
                try:
                    await self._emit_snapshot_if_changed()
                except Exception:  # noqa: BLE001
                    log.exception("project board poll failed (project %s)", self.project_id)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _emit_snapshot_if_changed(self):
        rows = await self._fetch_tasks()
        # Cheap, stable hash: tuple of the fields we care about.
        key = tuple(
            (
                r["id"], r["title"], r["status"], r["complexity"],
                r["position"], r["branch_name"] or "",
                r["waiting_for_user"],
                r["current_agent__slug"] or "",
                r["assigned_agent__slug"] or "",
            )
            for r in rows
        )
        h = hash(key)
        if h == self._last_snapshot_hash:
            return
        self._last_snapshot_hash = h
        payload = [
            {
                "id": r["id"],
                "title": r["title"],
                "status": r["status"],
                "complexity": r["complexity"],
                "position": r["position"],
                "branch_name": r["branch_name"] or "",
                "waiting_for_user": r["waiting_for_user"],
                "current_agent": r["current_agent__slug"],
                "assigned_agent": r["assigned_agent__slug"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
        await self.send(text_data=json.dumps({"type": "board", "tasks": payload}, ensure_ascii=False))
