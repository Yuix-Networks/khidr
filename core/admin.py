from django.contrib import admin

from .models import Agent, ChatMessage, ChatSummary, IgnoreRule, Project, ProjectMemory, Run, Task


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "repo_url", "publish_mode", "is_active", "created_at")
    list_filter = ("is_active", "publish_mode", "git_auth")
    search_fields = ("name", "slug", "repo_url")
    fieldsets = (
        (None, {"fields": ("name", "slug", "description", "is_active")}),
        ("Git", {"fields": ("repo_url", "default_branch", "publish_mode",
                            "git_auth", "git_token_env", "git_ssh_key_path")}),
        ("Deploy", {"fields": ("deploy_command", "allow_agent_deploy")}),
        ("Server inspection", {"fields": ("server_connection", "server_path", "server_allowed_commands")}),
        ("Monitoring", {"fields": ("glitchtip_project", "uptime_monitors")}),
    )


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ("slug", "type", "auth", "model", "sandbox", "max_concurrency",
                    "talent", "priority", "cooldown_until", "is_active")
    list_filter = ("type", "auth", "sandbox", "role", "talent", "is_active")
    search_fields = ("slug", "model", "base_url")
    filter_horizontal = ("projects",)
    fieldsets = (
        (None, {"fields": ("slug", "type", "role", "is_active", "talent", "priority", "projects")}),
        ("Model & credentials", {"fields": ("auth", "model", "base_url", "api_key_env", "context_length")}),
        ("Execution", {"fields": ("sandbox", "max_concurrency", "cooldown_until")}),
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


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "agent", "started_at", "ended_at", "end_reason", "asked_user")
    list_filter = ("end_reason", "agent")
    readonly_fields = ("token_hash", "started_at", "ended_at")
