"""Broadcast DB changes to the WebSocket consumer group for the affected task."""

from __future__ import annotations

import logging
from threading import Thread

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models import ChatMessage, Task

log = logging.getLogger(__name__)


def _broadcast(group: str, msg_type: str, payload: dict) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(
        group,
        {"type": msg_type, "payload": payload},
    )


@receiver(post_save, sender=ChatMessage)
def on_chat_message_saved(sender, instance: ChatMessage, created: bool, **kwargs):
    if not created:
        return
    _broadcast(
        f"task_{instance.task_id}",
        "chat.message",
        {
            "id": instance.id,
            "role": instance.role,
            "agent": instance.agent.slug if instance.agent_id else None,
            "content": instance.content,
            "created_at": instance.created_at.isoformat(),
        },
    )


@receiver(post_save, sender=Task)
def on_task_saved(sender, instance: Task, created: bool, **kwargs):
    _broadcast(
        f"task_{instance.id}",
        "task.update",
        {
            "id": instance.id,
            "status": instance.status,
            "current_agent": instance.current_agent.slug if instance.current_agent_id else None,
            "assigned_agent": instance.assigned_agent.slug if instance.assigned_agent_id else None,
            "branch_name": instance.branch_name or "",
            "waiting_for_user": instance.waiting_for_user,
        },
    )


def _notify_new_task(task_id: int) -> None:
    from core.services.telegram import notify_task

    task = Task.objects.select_related("project").filter(pk=task_id).first()
    if task is None:
        return
    headline = f"🆕 New task · {task.project.name}"
    if task.source != Task.SOURCE_MANUAL:
        headline += f" ({task.get_source_display()})"
    body = task.description[:300]

    def send():
        try:
            notify_task(task, headline, body=body)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram notify (new task) failed: %s", exc)

    # In a thread so the Telegram round-trip never holds up the request or
    # the agent that opened the card.
    Thread(target=send, daemon=True).start()


@receiver(post_save, sender=Task)
def on_task_created(sender, instance: Task, created: bool, **kwargs):
    """Ping Telegram for every new card, whoever opened it (UI, agent, webhook)."""
    if not created:
        return
    # After commit, so a rolled-back card never pings.
    transaction.on_commit(lambda: _notify_new_task(instance.pk))
