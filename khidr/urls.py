from django.contrib import admin
from django.urls import include, path

from core.mcp import mcp_endpoint
from core.api import api_login, list_memories, create_memory, delete_memory, search_memories
from core.views import glitchtip_webhook, uptime_kuma_webhook

urlpatterns = [
    path("admin/", admin.site.urls),
    path("mcp", mcp_endpoint),
    path("mcp/", mcp_endpoint, name="mcp"),
    path("", include("chat.urls")),
    path("api/login/", api_login, name="api_login"),
    path("api/projects/<int:project_id>/memories/", list_memories, name="list_memories"),
    path("api/projects/<int:project_id>/memories/create/", create_memory, name="create_memory"),
    path("api/projects/<int:project_id>/memories/<int:memory_id>/", delete_memory, name="delete_memory"),
    path("api/projects/<int:project_id>/memories/search/", search_memories, name="search_memories"),
    path("webhooks/glitchtip/", glitchtip_webhook, name="glitchtip_webhook"),
    path("webhooks/uptime-kuma/<str:token>/", uptime_kuma_webhook, name="uptime_kuma_webhook"),
]
