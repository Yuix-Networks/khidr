from django.db import models
from django.utils import timezone


class Project(models.Model):
    """A project with its own git repo and assigned agents."""

    name = models.CharField(max_length=128, unique=True)
    slug = models.SlugField(
        max_length=64,
        unique=True,
        blank=True,
        null=True,
        default=None,
        help_text="URL-friendly identifier. Auto-generated from name if blank."
    )
    description = models.TextField(blank=True, null=True)
    repo_path = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text="Absolute path to local git repo. Required before tasks can run.",
    )
    repo_url = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text=(
            "Git remote URL used to clone this project into "
            'each agent container, e.g. "git@github.com:your-org/your-project.git". '
            "If blank, provisioning falls back to the origin URL of the host's "
            "existing clone at repo_path (and skips the project if there is none)."
        ),
    )
    deploy_command = models.CharField(
        max_length=256,
        blank=True,
        null=True,
        default="",
        help_text='Command agents run after pushing to ship the change, e.g. "fab deploy" '
        'or "make deploy". Leave blank if the project has no deploy step.',
    )

    # Remote server access (for inspection/debug only — no code changes on server)
    server_connection = models.CharField(
        max_length=256,
        blank=True,
        null=True,
        help_text='SSH command to reach the server, e.g. "ssh myserver". '
        "Agent uses this for log inspection / DB shell, never to edit code.",
    )
    server_path = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text='Absolute path to the project on the server, e.g. "/srv/myproject".',
    )

    # Python environment paths for agents
    pyenv_path = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text='Path to pyenv version dir, e.g. "~/.pyenv/versions/khidr". '
        "Leave blank for python-free projects (mobile/desktop apps, static sites); "
        "provisioning will skip venv/pip steps and only git clone.",
    )
    python_path = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text='Full path to Python interpreter, e.g. "~/.pyenv/versions/khidr/bin/python".',
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    api_token = models.CharField(max_length=64, blank=True, null=True, unique=True)
    password = models.CharField(max_length=128, blank=True, null=True)
    glitchtip_project = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        unique=True,
        help_text="GlitchTip project name for webhook matching.",
    )
    uptime_monitors = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Comma-separated Uptime Kuma monitor names (e.g. \"Acme, River\"). "
        "When one goes down, an agent task is opened on this project at the top of Todo.",
    )

    def check_password(self, raw_password: str) -> bool:
        from django.contrib.auth.hashers import check_password
        if not self.password:
            return False
        return check_password(raw_password, self.password)

    def set_password(self, raw_password: str) -> None:
        from django.contrib.auth.hashers import make_password
        self.password = make_password(raw_password)

    def __str__(self):
        return self.name


class GitRemote(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="git_remotes",
    )
    name = models.CharField(
        max_length=64,
        help_text="Remote name, e.g. origin or production",
    )
    url = models.CharField(
        max_length=512,
        help_text="Remote URL, e.g. ssh://myserver/srv/myproject",
    )

    class Meta:
        ordering = ["id"]
        unique_together = [["project", "name"]]

    def __str__(self):
        return f"{self.project.name}/{self.name}"


class Agent(models.Model):
    TYPE_CLAUDE = "claude-agent-sdk"
    TYPE_CHOICES = [
        (TYPE_CLAUDE, "Claude Agent SDK (Anthropic-compatible API)"),
    ]

    ROLE_WORKER = "worker"
    ROLE_SUMMARIZER = "summarizer"
    ROLE_BOTH = "both"
    ROLE_CHOICES = [
        (ROLE_WORKER, "Worker"),
        (ROLE_SUMMARIZER, "Summarizer"),
        (ROLE_BOTH, "Both"),
    ]

    slug = models.SlugField(max_length=64, unique=True)
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_WORKER)

    # Anthropic Messages API-compatible endpoint (ANTHROPIC_BASE_URL), e.g.
    # https://api.minimax.io/anthropic. Blank = api.anthropic.com.
    base_url = models.URLField(blank=True)
    model = models.CharField(max_length=128, blank=True)
    context_length = models.IntegerField(
        default=0,
        help_text="Context window size in tokens (e.g. 128000). If 0, uses built-in defaults.",
    )
    api_key_env = models.CharField(
        max_length=64,
        blank=True,
        help_text="Name of env var that holds the API key (not the key itself).",
    )

    # Summarizer fallback order: lower value = higher priority
    priority = models.IntegerField(default=100)

    # Skill rating 1-5. Agent can only pick up a task whose complexity <= talent.
    talent = models.PositiveSmallIntegerField(
        default=3,
        help_text="Skill tier 1-5. Must be >= the task's complexity to be eligible.",
    )

    projects = models.ManyToManyField(Project, related_name="agents", blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.slug

    @property
    def is_worker(self) -> bool:
        return self.role in (self.ROLE_WORKER, self.ROLE_BOTH)

    @property
    def is_summarizer(self) -> bool:
        return self.role in (self.ROLE_SUMMARIZER, self.ROLE_BOTH)


class Task(models.Model):
    """A unit of work on a project, with agent-execution state."""

    STATUS_BACKLOG = "backlog"
    STATUS_TODO = "todo"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_REVIEW = "review"
    STATUS_DONE = "done"
    STATUS_CHOICES = [
        (STATUS_BACKLOG, "Backlog"),
        (STATUS_TODO, "Todo"),
        (STATUS_IN_PROGRESS, "In Progress"),
        (STATUS_REVIEW, "Review"),
        (STATUS_DONE, "Done"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=512)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_TODO)
    position = models.FloatField(
        default=0,
        help_text="Within-column ordering; smaller values sit at the top.",
    )
    complexity = models.PositiveSmallIntegerField(
        default=3,
        help_text="Task difficulty 1-5. Agents need talent >= complexity to be eligible.",
    )

    # Assignment
    assigned_agent = models.ForeignKey(
        Agent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tasks",
        help_text="Explicit assignment — if set, only this agent may take the task.",
    )
    current_agent = models.ForeignKey(
        Agent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_tasks",
        help_text="Agent currently working on this task (null when idle).",
    )

    # Git
    branch_name = models.CharField(max_length=256, blank=True)
    branch_attempt = models.PositiveIntegerField(
        default=0,
        help_text="Incremented each 'request changes' cycle — used for -v2, -v3 branch suffixes.",
    )

    # Lifecycle
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    waiting_for_user = models.BooleanField(
        default=False,
        help_text="Set when agent calls ask_user — heartbeat timeout pauses.",
    )
    next_dispatch_after = models.DateTimeField(
        null=True, blank=True,
        help_text="Dispatcher skips this task until after this timestamp. "
                  "Set on requeue to throttle agents that keep ending sessions "
                  "with no tool-call — prevents rapid-fire retries.",
    )

    context_used = models.IntegerField(
        default=0,
        help_text="Running total of tokens used in this task's current session.",
    )
    context_limit = models.IntegerField(
        default=0,
        help_text="Context window limit for the model assigned to this task.",
    )
    tokens_per_second = models.FloatField(
        default=0.0,
        help_text="Last measured streaming speed (tokens/sec).",
    )
    agent_status = models.CharField(
        max_length=16,
        default="idle",
        help_text="Current agent activity: idle, thinking, tool_call, writing, done.",
    )
    agent_status_detail = models.CharField(
        max_length=128,
        default="",
        help_text="Free-text detail for the current agent status (e.g. tool name).",
    )

    # Where the card came from, and the dedup key for webhook-born cards:
    # a GlitchTip error or an Uptime Kuma monitor opens one card, repeats
    # only bump `occurrences`.
    SOURCE_MANUAL = "manual"
    SOURCE_GLITCHTIP = "glitchtip"
    SOURCE_UPTIME = "uptime"
    SOURCE_CHOICES = [
        (SOURCE_MANUAL, "Manual"),
        (SOURCE_GLITCHTIP, "GlitchTip"),
        (SOURCE_UPTIME, "Uptime Kuma"),
    ]
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)
    occurrences = models.PositiveIntegerField(default=1)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "status", "position"]

    def __str__(self):
        return f"{self.project.name} / {self.title[:60]}"

    @property
    def chat_messages(self):
        """Alias for messages — supports both task.messages and task.chat_messages."""
        return self.messages.all()


class ChatMessage(models.Model):
    ROLE_USER = "user"
    ROLE_AGENT = "agent"
    ROLE_SYSTEM = "system"
    ROLE_CHOICES = [
        (ROLE_USER, "User"),
        (ROLE_AGENT, "Agent"),
        (ROLE_SYSTEM, "System"),
    ]

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True)
    content = models.TextField()
    is_streaming = models.BooleanField(
        default=False, help_text="True while tokens are still arriving."
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"[{self.task_id}/{self.role}] {self.content[:40]}"


class ChatSummary(models.Model):
    """Compressed summary of older chat messages to fit agent context window."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="summaries")
    summary_text = models.TextField()
    up_to_message = models.ForeignKey(
        ChatMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Summary covers messages up to and including this one.",
    )
    produced_by = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class ProjectMemory(models.Model):
    """Project-specific memory entries for AI agents."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memories")
    content = models.TextField()
    importance = models.CharField(
        max_length=16,
        choices=[
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("critical", "Critical"),
        ],
        default="medium"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.project.slug} / {self.content[:40]}"


class IgnoreRule(models.Model):
    """GlitchTip alerts whose title matches `pattern` never become a task."""

    pattern = models.CharField(
        max_length=512,
        help_text="Python regex, matched case-insensitively against the alert title (re.search).",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ignore_rules",
        help_text="Blank = applies to every project.",
    )
    note = models.CharField(max_length=256, blank=True)
    is_active = models.BooleanField(default=True)
    hits = models.PositiveIntegerField(default=0)
    last_hit_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.pattern
