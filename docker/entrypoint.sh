#!/bin/sh
# Khidr container entrypoint.
#   web           migrate, then serve the UI / webhooks / MCP endpoint on :8000
#   orchestrator  dispatch tasks and run agents in sandbox containers
#   <anything>    run it (e.g. `python manage.py load_agents`)
set -e

wait_for_db() {
    i=0
    until pg_isready -q -h "${PG_HOST:-db}" -p "${PG_PORT:-5432}" -U "${PG_USER:-khidr}"; do
        i=$((i + 1))
        [ "$i" -ge 60 ] && { echo "database not reachable at ${PG_HOST:-db}" >&2; exit 1; }
        sleep 1
    done
}

case "$1" in
    web)
        wait_for_db
        python manage.py migrate --noinput
        exec daphne -b 0.0.0.0 -p 8000 --access-log - khidr.asgi:application
        ;;
    orchestrator)
        wait_for_db
        # Let the web container finish its migrations first.
        until python manage.py migrate --check >/dev/null 2>&1; do sleep 2; done
        exec python manage.py run_orchestrator --tick "${KHIDR_TICK:-5}"
        ;;
    *)
        exec "$@"
        ;;
esac
