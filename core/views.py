import json
import secrets

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Project
from .services.alerts import ingest_glitchtip, ingest_uptime


@csrf_exempt
@require_http_methods(["POST"])
def glitchtip_webhook(request):
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    text = body.get("text", "")
    attachments = body.get("attachments", [])

    if not attachments:
        return JsonResponse({"error": "No attachments"}, status=400)

    attachment = attachments[0]
    title = attachment.get("title", "GlitchTip Alert")
    title_link = attachment.get("title_link", "")
    fields = {f["title"]: f["value"] for f in attachment.get("fields", [])}

    project_name = fields.get("Project")
    if not project_name:
        return JsonResponse({"error": "Project field not found"}, status=400)

    try:
        project = Project.objects.get(glitchtip_project=project_name)
    except Project.DoesNotExist:
        return JsonResponse({"error": f"Unknown project: {project_name}"}, status=404)
    if not project.is_active:
        # an inactive project gets no agent, so its cards would only pile up
        return JsonResponse({"created": False, "result": "inactive", "task_id": None})

    environment = fields.get("Environment", "")
    server_name = fields.get("Server Name", "")
    release = fields.get("Release", "")

    description_parts = [f"[Link]({title_link})"] if title_link else []
    if project_name:
        description_parts.append(f"**Project:** {project_name}")
    if environment:
        description_parts.append(f"**Environment:** {environment}")
    if server_name:
        description_parts.append(f"**Server:** {server_name}")
    if release:
        description_parts.append(f"**Release:** {release}")

    description = "\n".join(description_parts)

    result, task = ingest_glitchtip(project, title, description, issue_url=title_link)
    return JsonResponse(
        {"created": result == "created", "result": result, "task_id": task.pk if task else None},
        status=201 if result == "created" else 200,
    )


@csrf_exempt
@require_http_methods(["POST"])
def uptime_kuma_webhook(request, token):
    """Uptime Kuma "Webhook" notification (JSON body: heartbeat + monitor + msg)."""
    expected = getattr(settings, "WEBHOOK_TOKEN", "") or ""
    if not expected or not secrets.compare_digest(token, expected):
        return JsonResponse({"error": "Forbidden"}, status=403)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    result, task = ingest_uptime(body if isinstance(body, dict) else {})
    return JsonResponse(
        {"result": result, "task_id": task.pk if task else None},
        status=201 if result == "created" else 200,
    )
