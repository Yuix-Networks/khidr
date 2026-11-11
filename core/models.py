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
    repo_url = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text=(
            'Git URL Khidr clones and pushes, e.g. "git@github.com:your-org/your-project.git" '
            'or "https://github.com/your-org/your-project.git". Blank = the project has no repo '
            "(agents get an empty working directory)."
        ),
    )
    default_branch = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Branch tasks start from and publish to. Blank = the remote's HEAD branch.",
    )

    # Git credentials stay on the orchestrator host: Khidr fetches and pushes
    # itself, agents never see them.
    GIT_AUTH_HOST = "host"
    GIT_AUTH_TOKEN = "token"
    GIT_AUTH_SSH_KEY = "ssh_key"
    GIT_AUTH_CHOICES = [
        (GIT_AUTH_HOST, "Host git config (your own SSH agent / credential helper)"),
        (GIT_AUTH_TOKEN, "HTTPS token from an env var"),
        (GIT_AUTH_SSH_KEY, "Dedicated SSH key file (e.g. a deploy key)"),
    ]
    git_auth = models.CharField(max_length=16, choices=GIT_AUTH_CHOICES, default=GIT_AUTH_HOST)
    git_token_env = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Env var on the orchestrator host holding the HTTPS token (git_auth=token).",
    )
    git_ssh_key_path = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Private key file on the orchestrator host (git_auth=ssh_key).",
    )

    PUBLISH_DEFAULT_BRANCH = "default_branch"
    PUBLISH_TASK_BRANCH = "task_branch"
    PUBLISH_CHOICES = [
        (PUBLISH_DEFAULT_BRANCH, "Rebase onto the default branch and push it"),
        (PUBLISH_TASK_BRANCH, "Push a khidr/task-<id> branch (open a PR yourself)"),
    ]
    publish_mode = models.CharField(
        max_length=16, choices=PUBLISH_CHOICES, default=PUBLISH_DEFAULT_BRANCH,
        help_text="What Khidr does with the agent's commits when it moves a card to Review.",
    )

    deploy_command = models.CharField(
        max_length=256,
        blank=True,
        null=True,
        default="",
        help_text='Command Khidr runs on the orchestrator host when an agent calls the deploy '
        'tool, e.g. "make deploy". Runs in the task workspace after publishing.',
    )
    allow_agent_deploy = models.BooleanField(
        default=False,
        help_text="Let agents call the deploy tool. Off = only you deploy.",
    )

    # Remote server access for inspection (logs, status) — run by Khidr on the
    # host through an allowlist; agents get no SSH keys.
    server_connection = models.CharField(
        max_length=256,
        blank=True,
        null=True,
        help_text='SSH destination for the server_exec tool, e.g. "deploy@myserver" or a '
        "Host alias from the orchestrator host's ~/.ssh/config.",
    )
    server_path = models.CharField(
        max_length=512,
        blank=True,
        null=True,
        help_text='Absolute path to the project on the server, e.g. "/srv/myproject".',
    )
    server_allowed_commands = models.TextField(
        blank=True,
        default="",
        help_text="One command prefix per line that server_exec may run (blank = a built-in "
        "read-only list: tail, cat, ls, grep, journalctl, systemctl status, docker ps, df, …).",
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


class Agent(models.Model):
    # The harness is the coding-agent CLI Khidr drives. Each runs headless in
    # the task's sandbox and reaches Khidr's tools over HTTP MCP.
    TYPE_CLAUDE = "claude"
    TYPE_CODEX = "codex"
    TYPE_GEMINI = "gemini"
    TYPE_QWEN = "qwen"
    TYPE_OPENCODE = "opencode"
    TYPE_CHOICES = [
        (TYPE_CLAUDE, "Claude Code CLI (subscription or Anthropic-compatible API)"),
        (TYPE_CODEX, "OpenAI Codex CLI (ChatGPT subscription or OpenAI-compatible API)"),
        (TYPE_GEMINI, "Gemini CLI (Google login or API key)"),
        (TYPE_QWEN, "Qwen Code CLI (Qwen login or OpenAI-compatible API)"),
        (TYPE_OPENCODE, "opencode (any provider it supports)"),
    ]

    AUTH_API_KEY = "api_key"
    AUTH_SUBSCRIPTION = "subscription"
    AUTH_CHOICES = [
        (AUTH_API_KEY, "API key (pay per token)"),
        (AUTH_SUBSCRIPTION, "Subscription / CLI login"),
    ]

    SANDBOX_DEFAULT = ""
    SANDBOX_DOCKER = "docker"
    SANDBOX_LOCAL = "local"
    SANDBOX_CHOICES = [
        (SANDBOX_DEFAULT, "Default (KHIDR_SANDBOX)"),
        (SANDBOX_DOCKER, "Docker: a fresh container per run"),
        (SANDBOX_LOCAL, "Local: run on the host, no isolation"),
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

    auth = models.CharField(max_length=16, choices=AUTH_CHOICES, default=AUTH_API_KEY)
    sandbox = models.CharField(max_length=16, choices=SANDBOX_CHOICES, blank=True, default="")
    max_concurrency = models.PositiveSmallIntegerField(
        default=1, help_text="How many tasks this agent may work on at once.",
    )
    cooldown_until = models.DateTimeField(
        null=True, blank=True,
        help_text="Set when the provider reports a rate/usage limit; no dispatch until then.",
    )

    # API endpoint for auth=api_key: Anthropic-compatible for claude (e.g.
    # https://api.minimax.io/anthropic), OpenAI-compatible for the others.
    # Blank = the harness's own default.
    base_url = models.URLField(blank=True)
    model = models.CharField(max_length=128, blank=True)
    context_length = models.IntegerField(
        default=0,
        help_text="Context window size in tokens (e.g. 128000). If 0, uses built-in defaults.",
    )
    api_key_env = models.CharField(
        max_length=64,
        blank=True,
        help_text="Env var on the orchestrator host holding the API key — or, for claude with "
        "auth=subscription, a token from `claude setup-token`. Not the key itself.",
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
    def effective_sandbox(self) -> str:
        from django.conf import settings

        return self.sandbox or settings.KHIDR_SANDBOX

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


class Run(models.Model):
    """One harness process working on a task. Its token authenticates the
    agent's calls to Khidr's MCP endpoint for exactly this task."""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="runs")
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, related_name="runs")
    token_hash = models.CharField(max_length=64, unique=True)
    asked_user = models.BooleanField(default=False)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"run {self.pk} task={self.task_id} agent={self.agent_id}"

    @staticmethod
    def hash_token(token: str) -> str:
        import hashlib

        return hashlib.sha256(token.encode()).hexdigest()

    @classmethod
    def start(cls, task, agent) -> tuple["Run", str]:
        import secrets

        token = secrets.token_urlsafe(32)
        run = cls.objects.create(task=task, agent=agent, token_hash=cls.hash_token(token))
        return run, token

    @classmethod
    def active_for_token(cls, token: str) -> "Run | None":
        if not token:
            return None
        return (
            cls.objects.select_related("task", "task__project", "agent")
            .filter(token_hash=cls.hash_token(token), ended_at__isnull=True)
            .first()
        )

    def end(self, reason: str) -> None:
        Run.objects.filter(pk=self.pk, ended_at__isnull=True).update(
            ended_at=timezone.now(), end_reason=reason[:32]
        )

