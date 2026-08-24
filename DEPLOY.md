# Deploying to a Proxmox LXC container

Native install with systemd. Simpler than Docker-in-LXC (no nesting=1
requirement, no cgroup quirks, `journalctl` gives you logs directly).

## Network model — Tailscale

The app binds `127.0.0.1:8000` only. Tailscale runs here in
`userspace-networking` mode (no `tailscale0` interface), so tailnet traffic
arrives through `tailscale serve`, which terminates HTTPS and proxies to
loopback. Access is over Tailscale (WireGuard-based zero-trust overlay),
which provides:

- Device-level auth via your Tailscale SSO (Google/MS/GitHub)
- End-to-end encryption (WireGuard) — better than most HTTP+basic-auth setups
- MagicDNS + Serve: reach the dashboard at `https://crstl-api.<tailnet>.ts.net`
- Optional ACLs to restrict which users/groups can hit port 8000 (e.g., only
  the `accounting` group)

**Install Tailscale in the LXC** (after step 3 of the install below, before
step 6):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --hostname=crstl-api --ssh
```

Approve the machine in your Tailscale admin console. Note the machine's
100.x.y.z address; MagicDNS `crstl-api` should also resolve.

**Optional ACL** in Tailscale admin (locks down who can reach port 8000):

```json
{
  "acls": [
    {"action": "accept", "src": ["group:accounting", "group:admin"], "dst": ["tag:crstl-api:8000"]}
  ],
  "tagOwners": {"tag:crstl-api": ["autogroup:admin"]}
}
```

Then tag the LXC in the admin console (Machines → crstl-api → Edit tags →
add `tag:crstl-api`).

Without an ACL, every device on your tailnet can reach the dashboard.

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
runuser -u crstl -- python3 -m venv .venv
runuser -u crstl -- .venv/bin/pip install -r requirements.txt

# 5. Populate .env from the template — REQUIRED before starting the service.
#    systemd fails loudly with "Failed to load environment file" if .env
#    is missing. All values below must be set to real production values;
#    empty CRSTL_API_KEY or GRAPH_* results in every scheduled run erroring
#    into the /api/health endpoint.
cp .env.example .env
chmod 600 .env
chown crstl:crstl .env
$EDITOR .env   # fill in CRSTL_API_KEY, GRAPH_*, MAIL_SENDER, MAIL_RECIPIENTS

# 6. Create the writable state directory.
#    The systemd unit hardens the filesystem with ProtectSystem=strict and
#    ReadWritePaths=/opt/crstl-api/.tmp. That bind-mount is set up during
#    namespace init — BEFORE any ExecStartPre — so the directory must exist
#    on disk before `systemctl start`. Otherwise systemd errors with
#    "Failed to set up mount namespacing: .../.tmp: No such file or directory"
#    and the service restart-loops.
mkdir -p .tmp
chown crstl:crstl .tmp

# 7. Install the systemd unit and start
cp deploy/crstl-api.service /etc/systemd/system/
cp deploy/crstl-api-backup.service deploy/crstl-api-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now crstl-api
systemctl enable --now crstl-api-backup.timer

# 8. Verify
systemctl status crstl-api
journalctl -u crstl-api -f            # follow logs; initial Crstl sync takes ~15s
curl http://127.0.0.1:8000/api/health
# {"status":"ok","tracking":{"ok":true,"last_error":null,"last_error_at":null}}
```

The `/api/health` payload includes `tracking.ok`. If it flips to `false`,
the SQLite write path is broken (permission, disk full, corruption). Digest
emails still send but won't be marked → the next digest re-sends them. Fix
this before accounting notices duplicate emails.

Open `https://crstl-api.<tailnet>.ts.net` from any tailnet device to load
the dashboard. It is **not** reachable over the office LAN: the app binds
loopback, and the API has no authentication of its own, so a LAN-wide bind
would expose unauthenticated `POST /api/email/*` to every device on the
subnet.

Note that `sshd` does still listen on `0.0.0.0:22` over the LAN. It is
key-only for root (`PermitRootLogin without-password`) and no
`/root/.ssh/authorized_keys` exists, so nothing can currently authenticate
through it. Day-to-day SSH goes through Tailscale SSH instead. If Tailscale
is unavailable, the way in is the Proxmox console.

## What persists

Everything the app writes lives under `/opt/crstl-api/.tmp/`:
- `tracking.db` — emailed/exported/netsuite event history (drives digest dedup)
- Daily NetSuite CSVs (`netsuite_export_<date>.csv`)

The LXC's disk holds this — take a Proxmox snapshot before major changes.

## Updating

```bash
cd /opt/crstl-api
# NOTE: sudo is NOT installed on this container -- use runuser. Running git
# as root would leave root-owned files in a tree the crstl user must write to.
runuser -u crstl -- git pull --ff-only
runuser -u crstl -- .venv/bin/pip install -r requirements.txt   # if requirements.txt changed
systemctl restart crstl-api

# Confirm what is actually running. A restart alone is NOT a deploy: the unit
# has Restart=on-failure and starts on boot, so a reboot re-runs whatever is
# already checked out. In Aug 2026 the box served a three-week-old commit this
# way, still emailing the weekend digest that had been fixed on main.
git -c safe.directory=/opt/crstl-api log -1 --oneline
```

## Backups

The tracking DB is tiny (KB) but losing it means the next digest re-sends
everything to accounting. It is also the only state not reproducible from
git, so it is what matters if the app is ever rebuilt elsewhere.

`crstl-api-backup.timer` snapshots it daily at 06:00 UTC into
`.tmp/backups/`, keeping 14 days. It uses SQLite's online backup API rather
than `cp` -- the database is live while the API serves, and a plain copy can
capture a torn page mid-write -- then runs `PRAGMA integrity_check` on the
snapshot before pruning anything older.

```bash
systemctl list-timers crstl-api-backup   # confirm it is scheduled
systemctl start crstl-api-backup         # take one now
journalctl -u crstl-api-backup -n 5      # see what it wrote
```

Proxmox scheduled snapshots of the LXC cover disk loss, which same-disk
backups do not.

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
