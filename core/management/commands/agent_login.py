"""
Log an agent's CLI in to its subscription, interactively.

    python manage.py agent_login <agent-slug>

For a docker agent this opens the CLI inside a throw-away sandbox container
whose home is the agent's own home directory (data/agents/<slug>/home), so
the login is stored there and every later run of that agent reuses it. For a
local agent it runs the CLI on this machine with your normal home — logging
in once in your terminal is the same thing.

Claude Code is the exception: `claude setup-token` prints a long-lived token;
put it in the env var named by the agent's api_key_env (for example
CLAUDE_CODE_OAUTH_TOKEN in .env).
"""

from __future__ import annotations

import os
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import Agent
from core.services.agents.sandbox import DockerSandbox, sandbox_for, sandbox_user

LOGIN_COMMANDS = {
    Agent.TYPE_CLAUDE: ["claude", "setup-token"],
    Agent.TYPE_CODEX: ["codex", "login", "--device-auth"],
    Agent.TYPE_GEMINI: ["gemini"],
    Agent.TYPE_QWEN: ["qwen"],
    Agent.TYPE_OPENCODE: ["opencode", "auth", "login"],
}

HINTS = {
    Agent.TYPE_CLAUDE: "Copy the printed token into the env var named by api_key_env "
                       "(e.g. CLAUDE_CODE_OAUTH_TOKEN=… in .env), then restart Khidr.",
    Agent.TYPE_CODEX: "Open the printed URL, enter the code, sign in with your ChatGPT account.",
    Agent.TYPE_GEMINI: "Pick 'Login with Google', open the printed URL, then /quit.",
    Agent.TYPE_QWEN: "Pick 'Qwen OAuth', open the printed URL, then /quit.",
    Agent.TYPE_OPENCODE: "Pick your provider and paste its key or sign in.",
}


class Command(BaseCommand):
    help = "Log an agent's CLI in to its subscription (interactive)."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--shell", action="store_true",
                            help="Open a shell in the agent's sandbox home instead.")

    def handle(self, *args, slug, shell, **options):
        agent = Agent.objects.filter(slug=slug).first()
        if agent is None:
            raise CommandError(f"no agent {slug!r}")
        command = ["bash"] if shell else LOGIN_COMMANDS[agent.type]
        sandbox = sandbox_for(agent)
        self.stdout.write(HINTS.get(agent.type, "") if not shell else "")

        if isinstance(sandbox, DockerSandbox):
            ok, why = sandbox.available()
            if not ok:
                raise CommandError(why)
            home = sandbox.agent_home(agent)
            argv = [
                "docker", "run", "--rm", "-it",
                "-v", f"{home}:{DockerSandbox.HOME}",
                "-e", f"HOME={DockerSandbox.HOME}",
                "-e", "NO_BROWSER=true",
            ]
            user = sandbox_user()
            if user:
                if os.getuid() == 0:
                    import subprocess

                    subprocess.run(["chown", "-R", user, str(home)], capture_output=True)
                argv += ["--user", user]
            argv += [settings.KHIDR_SANDBOX_IMAGE, *command]
            self.stdout.write(f"login home: {home}")
        else:
            if not shutil.which(command[0]):
                raise CommandError(f"{command[0]} is not installed on this machine")
            argv = command
        os.execvp(argv[0], argv)
