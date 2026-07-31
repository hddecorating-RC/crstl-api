# Deploying to the in-office Docker host

## Prereqs on the host
- Docker Engine ≥ 24 and the Compose plugin (`docker compose version`)
- Git (to clone the repo, or scp the source over)
- Outbound HTTPS to `api.crstl.so`, `graph.microsoft.com`, and `login.microsoftonline.com`

## First deploy

```bash
git clone https://github.com/hddecorating-RC/crstl-api.git
cd crstl-api

# Create .env from the template and fill in real values (do not commit)
cp .env.example .env
$EDITOR .env

# Build the image and start the container in the background
docker compose up -d --build

# Watch the logs — initial Crstl sync takes ~15–20s
docker compose logs -f
```

Open http://<host-ip>:8000 in a browser to verify the dashboard loads.

## What persists across restarts

Everything in `.tmp/` (bind-mounted from the host):
- `tracking.db` — emailed/exported/netsuite event history (drives digest dedup)
- Daily generated NetSuite CSVs

If you rebuild the image (`docker compose up -d --build`), state survives.

## Updating to a new version

```bash
cd /path/to/crstl-api
git pull
docker compose up -d --build
```

Compose rebuilds the image and restarts the container. Zero-downtime is out of scope; a brief interruption during restart is fine for a dashboard.

## Timezone

`docker-compose.yml` sets `TZ=America/Toronto` so APScheduler fires:
- 04:00 — NetSuite CSV export
- 07:00 — Crstl cache refresh
- 07:15 — Daily accounting email digest

## Health

```bash
curl http://localhost:8000/api/health
# {"status":"ok"}

docker compose ps            # STATUS column shows "healthy" once probe passes
docker inspect crstl-api --format='{{.State.Health.Status}}'
```

## Common problems

**Container keeps restarting.** Check logs: `docker compose logs --tail=100`. Usually a bad `.env` — missing `CRSTL_API_KEY` or a Graph value.

**Digest emails not sending.** Curl the endpoint from inside the container to see the full error:
```bash
docker compose exec crstl-api curl -sS -X POST http://127.0.0.1:8000/api/email/send-digest
```
`403 ErrorAccessDenied` = Application Access Policy blocked the sender (see main README).

**Dashboard loads but shows "Never synced".** The initial sync happens at container startup and takes ~15s. Wait, refresh. If it stays "Never synced", check logs for auth errors against `api.crstl.so`.
