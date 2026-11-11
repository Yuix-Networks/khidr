from django.contrib import admin

from .models import Agent, ChatMessage, ChatSummary, GitRemote, IgnoreRule, Project, ProjectMemory, Task


class GitRemoteInline(admin.TabularInline):
    model = GitRemote
    extra = 1


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "repo_path", "is_active", "glitchtip_project", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "repo_path")
    inlines = [GitRemoteInline]
    fieldsets = (
        (None, {"fields": ("name", "slug", "description", "is_active")}),
        ("Git / Deploy", {"fields": ("repo_path", "repo_url", "deploy_command", "glitchtip_project")}),
        ("Monitoring", {"fields": ("uptime_monitors",)}),
        ("Remote server", {"fields": ("server_connection", "server_path")}),
        ("Python", {"fields": ("python_path", "pyenv_path")}),
    )


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ("slug", "type", "role", "model", "context_length", "talent", "priority", "is_active")
    list_filter = ("type", "role", "talent", "is_active")
    search_fields = ("slug", "model", "base_url")
    filter_horizontal = ("projects",)
    fieldsets = (
        (None, {"fields": ("slug", "type", "role", "is_active", "talent", "priority", "projects")}),
        ("OpenAI-compatible", {"fields": ("base_url", "model", "context_length", "api_key_env")}),
    )


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "project",
        "status",
        "source",
        "occurrences",
        "complexity",
        "assigned_agent",
        "current_agent",
        "waiting_for_user",
        "updated_at",
    )
    list_filter = ("status", "source", "project", "complexity", "waiting_for_user", "assigned_agent")
    search_fields = ("title", "description", "branch_name")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("title", "description", "project", "status", "position", "complexity")}),
        ("Assignment", {"fields": ("assigned_agent", "current_agent")}),
        ("Git", {"fields": ("branch_name", "branch_attempt")}),
        ("Alert", {"fields": ("source", "fingerprint", "occurrences", "last_seen_at")}),
        ("Lifecycle", {"fields": ("last_heartbeat", "waiting_for_user", "next_dispatch_after", "created_at", "updated_at")}),
    )


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("task", "role", "agent", "is_streaming", "created_at")
    list_filter = ("role", "is_streaming", "agent")
    search_fields = ("content",)
    readonly_fields = ("created_at",)


@admin.register(ChatSummary)
class ChatSummaryAdmin(admin.ModelAdmin):
    list_display = ("task", "produced_by", "created_at")
    list_filter = ("produced_by",)
    readonly_fields = ("created_at",)


@admin.register(ProjectMemory)
class ProjectMemoryAdmin(admin.ModelAdmin):
    list_display = ("project", "importance", "created_at")
    list_filter = ("importance", "project")
    search_fields = ("content",)
    readonly_fields = ("created_at",)


@admin.register(IgnoreRule)
class IgnoreRuleAdmin(admin.ModelAdmin):
    list_display = ("pattern", "project", "note", "is_active", "hits", "last_hit_at")
    list_filter = ("is_active", "project")
    search_fields = ("pattern", "note")
    readonly_fields = ("hits", "last_hit_at")
