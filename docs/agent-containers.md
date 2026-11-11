# Agent containers

Each agent in `agents.yaml` gets its own long-lived Docker container named
`khidr-agent-<slug>`. The orchestrator never runs agent code on the host; it
`docker exec`s `manage.py agent_worker` inside the container.

The container mirrors the host layout so that paths the orchestrator hands
over resolve identically on both sides:

| Path | Contents |
| --- | --- |
| `/home/khidr/workspace/<project>` | the agent's own read-write clone of each project |
| `/home/khidr/workspace/khidr` | a clone of Khidr itself (the worker code) |
| `/home/khidr/.pyenv/versions/khidr` | the virtualenv the worker runs in |
| `/home/khidr/.ssh` | SSH keys (mounted read-only) |

## 1. Build the image

```bash
docker build -t khidr-agent:latest -f docker/Dockerfile.agent docker/
docker network create khidr-agents
```

## 2. Start the containers and provision them

```bash
python manage.py provision_agents
```

For every active agent this:

1. starts `khidr-agent-<slug>` if it is missing or stopped (creating the
   `khidr-agents` network if needed), mounting
   `/home/khidr/workspace-agents/<slug>` as its workspace and `~/.ssh`
   read-only;
2. clones Khidr into `/home/khidr/workspace/khidr` (or fast-forwards the
   existing clone), creates the `khidr` virtualenv with pyenv, installs
   `requirements.txt` and copies the host's `.env`;
3. for every active project, clones `repo_url` (or the origin of the host's
   clone at `repo_path`) into `repo_path`, adds the project's extra git
   remotes and, when `pyenv_path` is set, creates that virtualenv and
   installs the project's `requirements.txt`.

It is idempotent: re-run it after adding an agent or a project, or after
updating Khidr. Requirements are reinstalled only when `requirements.txt`
changed; the worker also checks that the clone and the virtualenv agree
before every run and reinstalls requirements when they drift.

Useful flags (see `python manage.py provision_agents --help` for all):

| Flag | Effect |
| --- | --- |
| `--agent SLUG` / `--project NAME` | limit to these agents / projects (repeatable) |
| `--dry-run` | print the `docker` commands instead of running them |
| `--no-start` | only provision containers that are already running |
| `--skip-projects` | provision Khidr only |
| `--khidr-repo URL` | where to clone Khidr from (default: this checkout's `origin`) |
| `--khidr-python` / `--python` | pyenv versions for the `khidr` / project virtualenvs (default `3.14.4` / `3.12.10`, both baked into the image) |
| `--image`, `--network`, `--agents-root`, `--ssh-dir`, `--env-file` | override the defaults above |

The `khidr` hostname resolves to the host inside every container, so
`PG_HOST=khidr` reaches PostgreSQL on the host (make sure `pg_hba.conf`
allows the Docker bridge network).

### Doing it by hand

`provision_agents` runs the equivalent of:

```bash
for slug in $(python manage.py dump_agents); do
  mkdir -p /home/khidr/workspace-agents/$slug
  docker run -d --name khidr-agent-$slug \
    --network khidr-agents --restart=unless-stopped \
    --add-host=khidr:host-gateway \
    -v /home/khidr/workspace-agents/$slug:/home/khidr/workspace \
    -v /home/khidr/.ssh:/home/khidr/.ssh:ro \
    khidr-agent:latest
done
```

followed, inside each container, by cloning Khidr and every project from
`python manage.py dump_projects`, creating their virtualenvs and running
`pip install -r requirements.txt`.
