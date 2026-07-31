# Deploying to a Proxmox LXC container

Native install with systemd. Simpler than Docker-in-LXC (no nesting=1
requirement, no cgroup quirks, `journalctl` gives you logs directly).

## LXC provisioning (in Proxmox)

Create an **unprivileged** LXC container:
- Template: Debian 12 or Ubuntu 24.04 (both ship Python 3.12)
- Resources: 1 vCPU, 512MB RAM, 2GB disk — plenty
- Network: static IP on the office LAN, bridge to your LAN vmbr
- Features: nesting off (not needed), keyctl off, fuse off
- Start on boot: yes

## Inside the LXC

```bash
# 1. System packages
apt update
apt install -y python3 python3-venv git curl

# 2. Dedicated system user (no login shell, no home)
useradd --system --shell /usr/sbin/nologin --home-dir /opt/crstl-api crstl

# 3. Clone into /opt/crstl-api (readable by root, owned by crstl for writes)
cd /opt
git clone https://github.com/hddecorating-RC/crstl-api.git
chown -R crstl:crstl /opt/crstl-api
cd crstl-api

# 4. Python virtualenv
sudo -u crstl python3 -m venv .venv
sudo -u crstl .venv/bin/pip install -r requirements.txt

# 5. Populate .env from the template
cp .env.example .env
chmod 600 .env
chown crstl:crstl .env
$EDITOR .env   # fill in CRSTL_API_KEY, GRAPH_*, MAIL_SENDER, MAIL_RECIPIENTS

# 6. Install the systemd unit and start
cp deploy/crstl-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crstl-api

# 7. Verify
systemctl status crstl-api
journalctl -u crstl-api -f            # follow logs; initial Crstl sync takes ~15s
curl http://127.0.0.1:8000/api/health
```

Open `http://<lxc-ip>:8000` from any LAN machine to load the dashboard.

## What persists

Everything the app writes lives under `/opt/crstl-api/.tmp/`:
- `tracking.db` — emailed/exported/netsuite event history (drives digest dedup)
- Daily NetSuite CSVs (`netsuite_export_<date>.csv`)

The LXC's disk holds this — take a Proxmox snapshot before major changes.

## Updating

```bash
cd /opt/crstl-api
sudo -u crstl git pull
sudo -u crstl .venv/bin/pip install -r requirements.txt   # if requirements.txt changed
systemctl restart crstl-api
```

## Backups

The tracking DB is tiny (KB) but losing it means the next digest re-sends
everything. Suggest a nightly cron on the LXC:

```
0 2 * * * cp /opt/crstl-api/.tmp/tracking.db /opt/crstl-api/.tmp/tracking.db.bak.$(date +\%F) && find /opt/crstl-api/.tmp/tracking.db.bak.* -mtime +14 -delete
```

Or rely on Proxmox scheduled snapshots of the LXC.

## Common problems

**`systemctl status` shows failed.** `journalctl -u crstl-api -n 50` — almost
always a bad `.env` (missing `CRSTL_API_KEY`, misspelled `GRAPH_*`).

**Digest emails not sending.** Curl the endpoint from inside the LXC:
```bash
curl -sS -X POST http://127.0.0.1:8000/api/email/send-digest
```
`403 ErrorAccessDenied` = Exchange Application Access Policy blocked the app.

**Dashboard loads but "Never synced".** Initial sync happens at service start
(~15s). If it stays stuck, check logs for auth errors against `api.crstl.so`.

**Time zone drift.** The service file pins `TZ=America/Toronto`. Verify with
`systemctl show crstl-api | grep TimezoneName` — should read `America/Toronto`.

---

## Docker fallback (kept, but not the primary path)

`Dockerfile` and `docker-compose.yml` are in the repo if you ever want to
run this as a container instead — `docker compose up -d --build`. Would
require enabling `nesting=1` on the LXC config, which is why we don't
default to it.
