"""REST API for ProjectMemory model."""

from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from core.models import Project, ProjectMemory


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _auth(request) -> str | None:
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        return None
    try:
        return Project.objects.get(api_token=token).slug
    except Project.DoesNotExist:
        return None


@require_http_methods(["POST"])
def api_login(request):
    email = request.POST.get("email") or request.GET.get("email")
    password = request.POST.get("password") or request.GET.get("password")
    if not email or not password:
        return _error("email and password are required", 401)

    try:
        project = Project.objects.get(email=email)
    except Project.DoesNotExist:
        return _error("Invalid credentials", 401)

    if not project.check_password(password):
        return _error("Invalid credentials", 401)

    return JsonResponse({"access_token": project.api_token, "project_id": project.pk})


@require_http_methods(["GET"])
def list_memories(request, project_id):
    auth = _auth(request)
    if not auth:
        return _error("Unauthorized", 401)

    try:
        project = Project.objects.get(pk=project_id, api_token=auth)
    except Project.DoesNotExist:
        return _error("Not found", 404)

    limit = min(int(request.GET.get("limit", 50)), 100)
    memories = ProjectMemory.objects.filter(project=project)[:limit]
    return JsonResponse({
        "memories": [
            {
                "id": m.pk,
                "content": m.content,
                "importance": m.importance,
                "created_at": m.created_at.isoformat(),
            }
            for m in memories
        ]
    })


@require_http_methods(["POST"])
def create_memory(request, project_id):
    auth = _auth(request)
    if not auth:
        return _error("Unauthorized", 401)

    try:
        project = Project.objects.get(pk=project_id, api_token=auth)
    except Project.DoesNotExist:
        return _error("Not found", 404)

    import json
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return _error("Invalid JSON", 400)

    content = (body.get("content") or "").strip()
    if not content:
        return _error("content is required", 400)

    importance = body.get("importance", "medium")
    if importance not in ("low", "medium", "high", "critical"):
        importance = "medium"

    memory = ProjectMemory.objects.create(
        project=project,
        content=content,
        importance=importance,
    )
    return JsonResponse({
        "id": memory.pk,
        "content": memory.content,
        "importance": memory.importance,
        "created_at": memory.created_at.isoformat(),
    }, status=201)


@require_http_methods(["DELETE"])
def delete_memory(request, project_id, memory_id):
    auth = _auth(request)
    if not auth:
        return _error("Unauthorized", 401)

    try:
        project = Project.objects.get(pk=project_id, api_token=auth)
    except Project.DoesNotExist:
        return _error("Not found", 404)

    try:
        memory = ProjectMemory.objects.get(pk=memory_id, project=project)
    except ProjectMemory.DoesNotExist:
        return _error("Not found", 404)

    memory.delete()
    return JsonResponse({"deleted": True})


@require_http_methods(["GET"])
def search_memories(request, project_id):
    auth = _auth(request)
    if not auth:
        return _error("Unauthorized", 401)

    try:
        project = Project.objects.get(pk=project_id, api_token=auth)
    except Project.DoesNotExist:
        return _error("Not found", 404)

    query = (request.GET.get("q") or request.GET.get("query") or "").strip()
    if not query:
        return _error("q or query parameter is required", 400)

    limit = min(int(request.GET.get("limit", 10)), 50)
    memories = ProjectMemory.objects.filter(
        project=project,
        content__icontains=query,
    )[:limit]

    return JsonResponse({
        "memories": [
            {
                "id": m.pk,
                "content": m.content,
                "importance": m.importance,
                "created_at": m.created_at.isoformat(),
            }
            for m in memories
        ]
    })