# Khidr: web (UI, webhooks, MCP endpoint) and orchestrator, one image.
# Agents don't run here: the orchestrator starts a sandbox container per run
# (docker/Dockerfile.sandbox) through the host's Docker socket.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git for the host-side workspaces, ssh for git remotes and server_exec,
# the docker CLI to start sandboxes, libpq client tools for the DB wait.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git openssh-client ca-certificates curl gnupg postgresql-client \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN KHIDR_TESTS=1 DJANGO_SETTINGS_MODULE=khidr.settings python manage.py collectstatic --noinput -v 0 \
    && git config --system --add safe.directory '*'

EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
