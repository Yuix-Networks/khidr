#!/usr/bin/env bash
# Install and start Khidr with Docker Compose.
#
#   ./install.sh                 first install, or update after `git pull`
#   KHIDR_PASSWORD=… ./install.sh  set the web password without a prompt
#
# Safe to run again: it keeps an existing .env, agents.yaml and database.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "Docker is not installed: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (docker compose …)"
docker info >/dev/null 2>&1 || die "the Docker daemon is not running"

rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
env_get() { { grep -E "^$1=" .env 2>/dev/null || true; } | tail -1 | cut -d= -f2- | sed -e "s/^'//" -e "s/'$//"; }
env_set() {  # env_set KEY VALUE — replace or append, value single-quoted
    local tmp; tmp=$(mktemp)
    grep -vE "^$1=" .env > "$tmp" 2>/dev/null || true
    printf "%s='%s'\n" "$1" "$2" >> "$tmp"
    mv "$tmp" .env
}

# --- configuration ----------------------------------------------------------
if [ ! -f .env ]; then
    say "Creating .env"
    cp .env.example .env
    env_set DJANGO_SECRET_KEY "$(rand 50)"
    env_set DJANGO_DEBUG "false"
    env_set DJANGO_ALLOWED_HOSTS "*"
    env_set PG_NAME "khidr"
    env_set PG_USER "khidr"
    env_set PG_PASS "$(rand 32)"
    env_set CHAT_BASE_URL "http://localhost:${KHIDR_PORT:-8000}"
    env_set DJANGO_CSRF_TRUSTED_ORIGINS "http://localhost:${KHIDR_PORT:-8000},http://127.0.0.1:${KHIDR_PORT:-8000}"
    chmod 600 .env
fi
[ -n "$(env_get KHIDR_DATA_DIR)" ] || env_set KHIDR_DATA_DIR "$PWD/data"
DATA_DIR=$(env_get KHIDR_DATA_DIR)
mkdir -p "$DATA_DIR" secrets/ssh
chmod 700 secrets secrets/ssh
[ -f agents.yaml ] || { say "Creating agents.yaml from the example — edit it"; cp agents.yaml.example agents.yaml; }

# --- images -------------------------------------------------------------------
say "Building the Khidr image"
docker compose build web
say "Building the agent sandbox image (first time takes a few minutes)"
docker compose --profile build build sandbox-image

# --- web password -------------------------------------------------------------
if [ -z "$(env_get WEB_PASSWORD_HASH)" ]; then
    password=${KHIDR_PASSWORD:-}
    if [ -z "$password" ]; then
        if [ -t 0 ]; then
            read -r -s -p "Choose a password for the Khidr web UI: " password; echo
        else
            password=$(rand 20)
            echo "Generated web UI password: $password"
        fi
    fi
    [ -n "$password" ] || die "empty password"
    hash=$(docker compose run --rm --no-deps -T -e KHIDR_TESTS=1 -e P="$password" web \
        python -c 'import os; from chat.auth import hash_password; print(hash_password(os.environ["P"]))' \
        | tail -1)
    env_set WEB_PASSWORD_HASH "$hash"
    admin_password=$password
fi

# --- start --------------------------------------------------------------------
say "Starting Khidr"
docker compose up -d db web orchestrator

port=${KHIDR_PORT:-$(env_get KHIDR_PORT)}; port=${port:-8000}
printf 'Waiting for the web UI'
for _ in $(seq 1 90); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${port}/login/"; then ok=1; break; fi
    printf '.'; sleep 2
done
echo
[ -n "${ok:-}" ] || { docker compose logs --tail 50 web; die "the web UI did not come up"; }

docker compose exec -T web python manage.py load_agents || true
if [ -n "${admin_password:-}" ]; then
    # Django admin login (projects, agents, git settings): user "admin", same password.
    docker compose exec -T -e DJANGO_SUPERUSER_PASSWORD="$admin_password" web \
        python manage.py createsuperuser --noinput --username admin --email admin@localhost \
        >/dev/null 2>&1 || true
fi

cat <<EOF

Khidr is running at http://localhost:${port}

Next:
  1. Create a project with "+ New project" on the board. Git credentials and
     publish mode: http://localhost:${port}/admin/ (user "admin", same password).
     Git credentials for Khidr (never given to agents) go in ./secrets/ssh
     (a deploy key, known_hosts, config), or use an HTTPS token env var.
  2. Describe your agents in agents.yaml, put their keys in .env, then:
       docker compose exec web python manage.py load_agents
       docker compose up -d        # recreate so .env changes apply
  3. Log subscription agents in once:
       docker compose exec -it orchestrator python manage.py agent_login <agent>

Logs:    docker compose logs -f orchestrator
Update:  git pull && ./install.sh
EOF
