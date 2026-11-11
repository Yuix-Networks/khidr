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

## 2. Start one container per agent

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

The `khidr` hostname resolves to the host inside every container, so
`PG_HOST=khidr` reaches PostgreSQL on the host (make sure `pg_hba.conf`
allows the Docker bridge network).

## 3. Provision Khidr and the projects inside each container

For every container:

1. Clone Khidr to `/home/khidr/workspace/khidr`, create the `khidr` virtualenv
   with pyenv and `pip install -r requirements.txt`, and copy the `.env`.
2. For every project from `python manage.py dump_projects`, clone `repo_url`
   into `repo_path` and, when `pyenv_path` is set, create that virtualenv and
   install the project's requirements.

Re-run step 3 after updating Khidr. The worker also checks that the clone and
the virtualenv agree before every run and reinstalls requirements when they
drift.

> A `manage.py` command that automates steps 2–3 is planned; contributions
> welcome.
