# CCTV Control Center v3 — Complete Instruction Documentation

*Self-hosted CCTV operations console for Footprints International School.*
Everything — the WebUI, Telegram bot and NVR health scheduler — runs in a single
container on **port 5555**, backed by SQLite.

---
## Table of Contents
1. [Quick Start](#1-quick-start)
2. [Environment Variables](#2-environment-variables)
3. [First-Run Configuration](#3-first-run-configuration)
4. [Web UI Usage](#4-web-ui-usage)
5. [Telegram Bot](#5-telegram-bot)
6. [NVR Health Monitoring](#6-nvr-health-monitoring)
7. [Footage Workflow](#7-footage-workflow)
8. [Camera REST API](#8-camera-rest-api)
9. [Campus Agents (TTP/Remote Workers)](#9-campus-agents-ttpremote-workers)
10. [Reverse Proxy (HTTPS)](#10-reverse-proxy-https)
11. [Administration](#11-administration)
12. [Testing](#12-testing)
13. [Migrating from v2](#13-migrating-from-v2)
14. [Troubleshooting](#14-troubleshooting)

---
## 1. Quick Start

### Docker (recommended)

```bash
# 1. Copy and edit the environment file
cp .env.example .env

# 2. Set required values
#    - SUPER_ADMIN_PASSWORD: change from default
#    - SECRET_KEY: either set a strong value or leave blank (auto-generated)
#    - PUBLIC_URL: your HTTPS host (e.g. https://cctv.footprints.work)
#    - PUID/PGID: your host user IDs (run `id -u` and `id -g` on the host)

# 3. Start the stack
docker compose up -d --build

# 4. View logs and wait for startup
docker compose logs -f
```

Open `http://localhost:5555` (or your public URL) and sign in with the
super-admin credentials from `.env`.

### Without Docker

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

Set `RUN_BOT=false RUN_SCHEDULER=false` in `.env` to run only the WebUI.

> **State location:** `./data` (mounted at `/app/data` inside the container)
> - `data/cctv.db` — SQLite database (all settings, NVRs, jobs, alerts, users)
> - `data/uploads/` — Uploaded Google credential JSON
> - `data/work/` — Temporary footage while a job runs
> - `data/.secret_key` — Generated key if `SECRET_KEY` is not set

Set `PUID`/`PGID` in `.env` to your host user so files in `./data` stay editable
from the host.

> **Hikvision SDK:** Hikvision/DVR exports need `./lib/libhcnetsdk.so` and
> `./lib/HCNetSDKCom/` on the host. The proprietary SDK is not baked into the
> public image; `docker-compose.yml` mounts `./lib` at `/app/lib`. Copy the same
> directory to every campus agent host. TruVision/ISAPI NVRs do not need it.

---
## 2. Environment Variables

Only bootstrap values belong in `.env`; everything else is managed in the UI.

| Variable | Default | Notes |
|---|---|---|
| `SUPER_ADMIN_USER` | `superadmin` | Re-synced on every start; cannot be deleted |
| `SUPER_ADMIN_PASSWORD` | `ChangeMe!2026` | **Change this**; re-synced on start |
| `SUPER_ADMIN_EMAIL` | `cctv@example.com` | Used for the super-admin account |
| `SECRET_KEY` | auto-generated | Sessions + Fernet encryption of stored secrets. Keep stable — changing invalidates sessions and makes previously encrypted secrets unreadable. |
| `PUBLIC_URL` | `https://cctv.footprints.work` | Links, MiniApp, emails. Must match BotFather and Caddy/nginx config. |
| `PORT` / `HOST` | `5555` / `0.0.0.0` | App bind address |
| `PUID` / `PGID` | `1000` / `1000` | Runtime app user; set to your host `id -u` / `id -g` |
| `TIMEZONE` | `Asia/Phnom_Penh` | Display timezone |
| `RUN_BOT` | `true` | Start the Telegram bot thread |
| `RUN_SCHEDULER` | `true` | Start the NVR health scheduler |
| `SEED_LEGACY_NVRS` | `false` | Import a private v2 `nvr_data.py` once on first boot (migration only) |
| `DATA_DIR` / `WORK_DIR` / `UPLOAD_DIR` | under `data/` | Storage paths |
| `DEBUG` | `false` | Exposes `/api/docs` when true |

### Bootstrap values (set once, then managed in UI)

- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_GROUP_ID` — seeded from `.env` on first boot; later set via **Integrations → Telegram**
- `GOOGLE_KEYS_FILE` — private `google_keys.json` imported on first boot if no credentials stored; later uploaded via **Integrations → Google Drive**. The file is git-ignored and excluded from Docker.
- `APP_CAMPUS` — physical campus of this host (used by agent routing)

---
## 3. First-Run Configuration

After starting the app, complete these steps in the WebUI:

1. **Users** — change the super-admin password in `.env`; create operator/admin
   accounts in **Users** (create, delete, reset password).

2. **Integrations → Google Drive** — upload the service-account JSON and set the
   impersonation mailbox. Click **Test** to confirm delegation.

3. **Integrations → Telegram** — paste the bot token and group/alert chat IDs.
   Saving a new token or trigger word reloads the bot live; no restart is needed.

4. **Integrations → SMTP** — set the Gmail address/app password, approver and CC
   addresses.

5. **NVRs** — add each recorder, then **Discover cameras**. Discovery fills the
   channel list; health sweeps keep online status current.

6. **Folders** — map Drive folder keys to campuses for upload destinations.
   (Environment variables `DRIVE_TK_ECP`, `DRIVE_TTP_ECP`, etc. can also seed
   folders at startup.)

7. **Settings** — adjust compression, job workers, health thresholds and alert
   cooldown.

### Google Drive delegation

The service account must have domain-wide delegation and impersonate
`google.impersonate_email` (default `cctv@footprintsschool.edu.kh`) with the
Drive scope. Folder access is granted to recipients for
`google.share_expiry_days` (default 30).

### Telegram MiniApp

1. Set `PUBLIC_URL` to the HTTPS host and configure it in **Settings**.
2. In **@BotFather** → `/setmenubutton` (or `/newapp`) point the MiniApp URL at
   `https://cctv.footprints.work/miniapp`.
3. `/tkbaboy` (also `/badboytk`, `/miniapp`) opens the MiniApp inside Telegram.
4. The MiniApp validates Telegram `initData` HMAC; optionally restrict access
   with `telegram.allowed_user_ids`.

MiniApp file download uses the `X-Telegram-Init-Data` header (not cookies), so it
works inside the Telegram webview.

---
## 4. Web UI Usage

### Roles

- **superadmin** — full control (bot token, Google JSON, SMTP password; can
  change super-admin password, create admins)
- **admin** — can reach Integrations, Users and Settings
- **operator** — can create/view own jobs, cannot change global settings
- **viewer** — read-only access

### Main Pages

- **Dashboard** — summary of NVRs, cameras, health, jobs, alerts, compression status
- **NVRs** — list, add, edit, discover cameras, view health per NVR
- **Folders** — manage Google Drive folder mappings (key, name, campus, folder ID)
- **Integrations** — configure Google Drive, Telegram, SMTP
- **Users** — list, create, update, delete users (super-admin only)
- **Settings** — adjust compression backend/preset/quality, job workers, health
  thresholds, alert cooldown, agent settings
- **Alerts** — view open/resolved alerts, acknowledge, resolve, create exceptions
  (suppress recurring alerts per camera/NVR/alert-type/fingerprint)
- **Agents** — manage remote campus agents (create, rotate token, delete)
- **Jobs** — list jobs, view progress, cancel, delete

### Camera REST API (read-only)

Generate a key in **Settings → Campus agents & REST API** (super-admin only). All
endpoints accept either `X-API-Key` header or a signed-in session.

```bash
curl -H "X-API-Key: $CCTV_API_KEY" \
  https://cctv.footprints.work:5555/api/v1/cameras

curl -H "X-API-Key: $CCTV_API_KEY" \
  "https://cctv.footprints.work:5555/api/v1/cameras?campus=tk&zone=ecp&q=gate"

curl -H "X-API-Key: $CCTV_API_KEY" \
  https://cctv.footprints.work:5555/api/v1/cameras/summary
```

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/cameras` | Paginated cameras with name, IP, MAC, online state and NVR/campus/zone |
| `GET /api/v1/cameras/summary` | Total/online/offline counts and per-campus totals |
| `GET /api/v1/cameras/{id}` | One camera |
| `GET /api/v1/nvrs` | NVR inventory with status and HDD usage |
| `GET /api/v1/health` | Same health summary as the dashboard |

Rotate the key from the same Settings card; the old key stops working
immediately.

---
## 5. Telegram Bot

### Commands

| Command | Purpose |
|---|---|
| `/start`, `/help` | Command overview |
| `/tk` | Reply to a CCTV request message; guided camera/date selection |
| `/tk folder_name` | Same, overriding the upload folder name |
| `/tkbaboy`, `/badboytk`, `/miniapp` | Open the MiniApp |
| `/health`, `/status` | NVR health summary |
| `/id`, `/chatid` | Show chat/user IDs (for `group_id` config) |

### Message format

The request message must contain `Date:`, `Start Time:` and `End Time:` (plus
optional `Incident:`, `Requester:`, `Campus:`). Jobs run through the same engine
as the WebUI.

### Trigger words

The global command is configurable in **Integrations → Telegram** (for example
`tk` → `/tk`), and changes reload the bot live. `/tk` always remains accepted so
existing chats never break. An agent's own trigger word (for example `/ttp`)
pins the request to that campus, so a TK operator cannot accidentally pull
footage from a remote campus.

### MiniApp inside Telegram

1. Set `PUBLIC_URL` to HTTPS and configure it in **Settings**.
2. In **@BotFather** → `/setmenubutton` point the MiniApp URL at
   `https://cctv.footprints.work/miniapp`.
3. `/tkbaboy` / `/badboytk` / `/miniapp` opens the MiniApp inside Telegram.

---
## 6. NVR Health Monitoring

The scheduler runs every `health.interval_minutes` (default 10) and records:

- NVR online/offline (with automatic recovery alerts)
- HDD status and usage vs. `health.hdd_threshold_percent` (default 90%)
- Offline cameras (per channel, with recovery)

Alerts are de-duplicated by fingerprint and repeat notifications are throttled by
`health.alert_cooldown_minutes` (default 60). Open alerts appear on **Alerts** and
in `/health`. Use **Run check now** (`POST /api/health/run`) to trigger a sweep.

> SNMP and NVR temperature monitoring were removed from v3. Existing databases
> are migrated automatically: legacy NVR columns are dropped and orphaned
> `health.temp_threshold_c` / `health.snmp_*` settings are deleted.

### Alert exceptions

Suppress matching alerts via **Alerts → Alert exceptions**. An exception can
target an exact fingerprint, or combine `alert_type` / `nvr_id` / `camera_id`.
For example:
- camera + nvr → ignore every alert for a ghost camera
- alert_type=hdd + nvr → ignore HDD capacity on an always-full NVR
- fingerprint → ignore exactly one recurring alert

HDD capacity alerts are off by default — only disk status/corruption alerts fire
unless **Settings → Health → also alert when an HDD is nearly full** is enabled.

### Resolve notes

**Alerts → Resolve** (or **Resolve all**) opens a small dialog with an optional
note. The note is stored with the alert and shown in the Resolved status cell.
Recovery, alert-exception suppression and camera-disable actions fill in an
automatic note.

### Quiet hours (pause Telegram alerts)

**Integrations → Telegram → Pause Telegram alerts during quiet hours** stops
health alerts being sent to Telegram between the start and end time (default
17:00 → 07:00, in `app.timezone`). Alerts are still recorded and shown on
**Alerts** as normal; only Telegram delivery pauses. Problems that are still
open after quiet hours end are sent on the next sweep after the alert cooldown.

### Camera IP / MAC

Discovery and every health sweep read each channel's IP from the NVR
(Hikvision and TruVision). NVRs do not report a camera's MAC, so the app asks
each camera directly using the NVR's username/password. Cameras with a
different password, or on the NVR's internal PoE network, keep a blank MAC
(retried every 6 hours, or immediately with **Discover cameras**) and can be
filled in by hand. Analog DVR channels have no IP/MAC.

### Live view

**NVRs & cameras → Cameras → Live** opens a low-latency MJPEG stream built from
repeated ISAPI JPEG snapshots. The modal can **Pause/Resume**, go **Fullscreen**
and save a **Snapshot**. Works for Hikvision NVRs and analog/TVI DVR channels on
DVR recorders as well.

### DVR channel status

Analog DVR channels are not reported by the IP-camera status endpoint. The app
uses `/ISAPI/System/workingstatus/chanStatus` when the firmware supports it, and
otherwise reads `videoInputEnabled` plus `resDesc` from
`/ISAPI/System/Video/inputs/channels`. A disabled channel, `NO VIDEO` or a lost
signal is shown as **Offline**; valid resolutions are **Online**. This removes
the previous **Unknown** state for DVR cameras.

### Disabling unused ports

DVRs often report every licensed port even when no camera is connected. Open
**NVRs & cameras → Cameras** and click **Disable** on an unused channel. The
port is ignored by health checks, alerting, discovery updates and every footage
picker until you click **Enable**. A disabled port keeps its row in the Cameras
modal (greyed out) so you can re-enable it later; **Discover** never re-enables
it automatically.

---
## 7. Footage Workflow

### Web UI / MiniApp

1. Choose campus → zone → NVR(s) → cameras.
2. Pick a date and start/end time (max 12 hours per request).
3. Optionally set the folder name (`/tk folder_name`, or the MiniApp/WebUI field).
4. The job downloads from the NVR, compresses with QuickSync, uploads to Drive,
   grants recipient access and emails links.
5. Track progress in **Jobs** or the MiniApp; cancel while running.

### Telegram bot

1. Send `/tk` and reply to a CCTV request message.
2. Follow the guided steps: select campus → zone → NVRs → cameras → set date/time.
3. Select destination folder.
4. Receive Google Drive link and email notifications when ready.

### Job controls

- `jobs.max_workers` controls concurrent jobs (default 2, configurable 1-8).
- `compression.max_parallel` controls parallel encoders (default 2, configurable 1-4).
- `jobs.keep_local_days` controls how long main-host copies are kept (default 7).
- `agents.keep_files_days` controls how long files kept on a campus agent are
  kept (default 7, configurable 1-365). It is separate from the main-host
  retention and is sent to the agent on every heartbeat.
- Use **Cancel** in the UI or `POST /api/jobs/{job_id}/cancel` to stop a running job.

### Camera naming

Each camera file is named `{folder_name}_CH{camera_number}.mp4` (or with the
incident name prefix). When multiple NVRs are selected, the NVR code is included
as a prefix to avoid filename clashes.

---
## 8. Campus Agents (TTP / Remote Workers)

The main host can route a job to a small agent container on another campus so
footage is downloaded and compressed on the local network instead of crossing
the WAN.

### Setting up an agent

1. Open **Campus agents → Add agent** and set campus, zones and a trigger word
   (for example `ttp`).

2. On the campus host you only need the image, `docker-compose.agent.yml`,
   `.env.agent`, and (for Hikvision/DVR NVRs) the `lib/` SDK directory:

```bash
cp .env.agent.example .env.agent
# edit AGENT_MAIN_URL and AGENT_TOKEN
docker compose -f docker-compose.agent.yml --env-file .env.agent up -d --build
```

Do **not** copy the main server's `.env`, `data/cctv.db`, `google_keys.json`
or `nvr_data.py` to the agent host. The agent receives NVR and Drive
credentials from the main host in the authenticated job payload, and keeps its
own state in `./agent-data`.

`AGENT_MAIN_URL` must be reachable from the campus network and should use
HTTPS. Set `AGENT_VERIFY_TLS=false` only for a self-signed certificate. The
compose file publishes port 5555 on `127.0.0.1` only, for an optional local
nginx that serves `/agent-files/`; the agent itself only needs outbound access
to the main host. Set `AGENT_BIND=0.0.0.0` in `.env.agent` only if nginx runs on
a different host, and keep its nginx location limited to `/agent-files/`.

If you use a registry instead of a copied source directory, set `CCTV_IMAGE`
in `.env.agent` and remove `build:` from `docker-compose.agent.yml` (or run
`docker compose pull` first). You can also transfer an image offline with
`docker save` / `docker load`.

The `AGENT_CAMPUS` / `AGENT_ZONES` values are labels; job routing uses the
campus and zones stored in the main WebUI record.

3. On the main host, `/ttp` in Telegram routes that request to the TTP agent;
   `/tk` continues to run locally. The agent heartbeats every poll interval and
   posts progress back to **Jobs**. If an agent never claims a job, the main
   host optionally runs it locally after `agents.claim_timeout_minutes` (default 10).

### Direct download for agent jobs

For the fastest large-file downloads, keep the files on the campus agent and let
the browser fetch them over the agent's public HTTPS URL.

1. On **Campus agents**, set **Public download URL** for the agent, for example
   `https://agent-cctv.footprints.work`. Leave **Settings → Campus agents →
   Direct agent download** enabled (the default).
2. Point nginx at the agent's port 5555 and expose `/agent-files/` (see
   [Reverse Proxy](#9-reverse-proxy-https)). No agent login is needed and the
   agent token is never placed in a URL.
3. When a job finishes, the agent moves the final MP4s to
   `./agent-data/agent-files/job_<id>/`. The main host signs a short-lived
   per-file link (HMAC-SHA256 over job id, filename and expiry; default 6 hours)
   and **Jobs** shows ⬇ Download, ↻ Retry/resume and ⧉ Copy link. The MiniApp
   opens the same link with Telegram's `openLink`.
4. The agent serves the file with HTTP range support, so a dropped transfer can
   resume instead of restarting. nginx must disable buffering for that location
   and use a long timeout (sample below).
5. Files are deleted after `agents.keep_files_days` (default 7). The agent
   checks every 10 minutes and refreshes a file's timestamp while it is being
   downloaded, so an active transfer is not removed mid-stream.

If direct download is disabled or the public URL is blank, the agent falls back
to copying MP4s to the main host (`POST /api/agents/jobs/{job_id}/files/chunk`)
in resumable chunks, so agent jobs still get the same **⬇ Download** buttons in
**Jobs** and the MiniApp as local jobs. If a chunk is dropped, the agent retries
it without restarting the whole file. After updating the code, rebuild the agent
container (`docker compose -f docker-compose.agent.yml --env-file .env.agent up
-d --build`). Tune with `AGENT_UPLOAD_CHUNK_MB` (default 8) and
`AGENT_UPLOAD_RETRIES` (default 4). If the main host is behind nginx, allow at
least the chunk size (for example `client_max_body_size 20m`) and a long proxy
timeout. Older agents that do not understand the direct-download job block use
this upload path automatically.

### Agent credentials

Agents receive NVR credentials and Drive credentials only in the authenticated
HTTPS job payload. They keep their own local work directory and clean temporary
work files after upload (`AGENT_KEEP_FILES=true` to retain them for debugging).
Files kept for direct download are cleaned separately after
`agents.keep_files_days`.

### Agent API (used by the agent container)

- `GET /api/agents/jobs/next` — claim the next job (includes the `download`
  block: `direct` and `keep_days`)
- `POST /api/agents/jobs/{job_id}/progress` — report progress and result files
- `POST /api/agents/jobs/{job_id}/files/chunk` — resumable result upload (fallback)
- `POST /api/agents/jobs/{job_id}/files` — single-request result upload (older agents)
- `POST /api/agents/heartbeat` — send heartbeat; response includes the current
  `keep_files_days` and `direct_download` settings
- `GET /agent-files/{job_id}/{filename}?expires=&sig=` — signed direct download
  on the campus agent (main host returns 404)
- Token auth: `X-Agent-Token` or `Authorization: Bearer <token>`

---
## 9. Reverse Proxy (HTTPS)

The app expects HTTPS at `https://cctv.footprints.work`. Example Caddy config:

```caddy
cctv.footprints.work {
    reverse_proxy 127.0.0.1:5555
}
```

Or nginx:

```nginx
server {
    listen 443 ssl;
    server_name cctv.footprints.work;

    location / {
        proxy_pass http://127.0.0.1:5555;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
        client_max_body_size 2g;   # agent file copies
    }
}
```

Campus agent with direct downloads (run on the agent host, proxying port 5555):

```nginx
server {
    listen 443 ssl;
    server_name agent-cctv.footprints.work;

    location /agent-files/ {
        proxy_pass http://127.0.0.1:5555;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        # Large MP4s: stream instead of buffering, and allow a long transfer.
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 1h;
        send_timeout 1h;
    }

    location / {
        return 404;
    }
}
```

Session cookies set `secure` based on the request scheme, so local HTTP testing
still works. Behind the proxy, `X-Forwarded-Proto: https` enables secure cookies.

---
## 10. Administration

### Password hashing

Uses PBKDF2-SHA256 with 260,000 rounds. No external build dependencies.

### Secret storage

Uses Fernet encryption (key derived from `SECRET_KEY`). Stored secrets include:
Telegram bot token, Google service account JSON, SMTP password, API key, agent
token.

### Session cookies

- Name: `cctv_session`
- HttpOnly, SameSite=lax, secure when behind HTTPS
- Max age configurable via `security.session_hours` (default 168 hours / 7 days)

### API key rotation

Rotate the camera REST API key from **Settings → System → Rotate API key**
(super-admin only). A new key is generated immediately; the old key stops working.

### Audit logging

All admin actions (user creation, setting changes, NVR operations, job creation/
cancellation, etc.) are logged to the `audit_logs` table with username, action,
detail and IP.

---
## 11. Testing

```bash
.venv/bin/pytest -q
```

Tests cover:
- Security helpers (password hashing, secret encryption, session validation)
- Telegram `initData` validation
- NVR parsing (ISAPI responses)
- HTTP API (isolated `DATA_DIR`, bot/scheduler disabled, legacy seeding off)

---
## 12. Migrating from v2

- Set `SEED_LEGACY_NVRS=true` (default `false`) to import a private `nvr_data.py`
  once on first run. That file contains NVR credentials, is git-ignored, and is
  excluded from the Docker build context. Never publish it.
- The root-level legacy `mailer.py` used by `cctv_bot.py` reads its credentials
  from `CCTV_SMTP_USER`, `CCTV_SMTP_PASSWORD`, optional `CCTV_SMTP_CC`,
  `CCTV_SMTP_APPROVER`, `CCTV_SMTP_HOST` and `CCTV_SMTP_PORT`. v3 itself uses
  **Integrations → SMTP** and stores those values in the database.
- `DRIVE_*` environment variables seed Drive folder mappings the first time.
- A legacy `GOOGLE_KEYS_FILE` (`google_keys.json`) is imported if no credentials
  are stored yet — prefer uploading via **Integrations**.
- Legacy `TELEGRAM_BOT_TOKEN` / `TELEGRAM_GROUP_ID` seed the Telegram settings.
- Keep the v2 deployment running until v3 is verified, then decommission it.

---
## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| `ffmpeg not installed` / no QuickSync | Confirm `/dev/dri` exists on the host, is mapped in compose, and `docker compose exec cctv-autonomous-v3 ls -l /dev/dri` shows a `renderD*` node; run `vainfo` |
| `render node ... not accessible` | The entrypoint auto-aligns groups; if you overrode `user:`, remove it. Otherwise set `PUID`/`PGID` and rebuild/restart |
| `Error initializing an MFX session: -3` | Normal on newer Intel GPUs without the oneVPL runtime; v3 automatically uses `h264_vaapi`. Confirm with `GET /api/system/compression` |
| `Running pip as the 'root' user` warning | Harmless build-time message; v3 now silences it with `PIP_ROOT_USER_ACTION=ignore` |
| Bot silent after token change | Save **Integrations → Telegram**; the bot reloads live. Confirm `RUN_BOT=true` |
| `/tk` says session expired after camera selection | Fixed in v3; the folder menu accepts both new campus strings and legacy dicts |
| Ghost camera or always-full disk alerts | Add an **Alerts → exception** (camera + type, or HDD + NVR) or press **Ignore** on the alert |
| MiniApp blank / 401 | `PUBLIC_URL` must be HTTPS and match BotFather; `telegram.miniapp_enabled` on |
| Login redirects immediately | Ensure `SECRET_KEY` is stable and cookies aren't blocked |
| NVR offline | Check host/port/credentials; run **Discover cameras** |
| Alerts repeat every sweep | Increase `health.alert_cooldown_minutes` |
| Drive upload fails | Verify Google service account JSON has domain-wide delegation and the impersonation email has Drive access |
| Email not sending | Check SMTP credentials in **Integrations → SMTP**; use **Test SMTP** to verify |
| Agent never claims jobs | Check `AGENT_TOKEN` and `AGENT_MAIN_URL`; ensure the agent container can reach the main host over HTTPS |
| Agent job fails with Hikvision SDK error 7 or ISAPI connect timeout | The agent cannot reach the NVR IPs. Check the agent host firewall/VPN/routes to the camera VLANs |
| ` Camera already exists` during discovery | Channels may already be in the database; use **Discover** to update names/online status, or add cameras manually via the UI |

---
## 14. Directory Overview

```
cctv-autonomous.v3/
├── app/                    # FastAPI application
│   ├── main.py             # App entrypoint, FastAPI setup
│   ├── config.py           # Environment variables, paths, secrets
│   ├── database.py         # SQLAlchemy engine & session management
│   ├── models.py           # DB models (User, NVR, Camera, Job, etc.)
│   ├── security.py         # Password hashing, Fernet encryption, sessions
│   ├── settings_service.py # Defaults, seeding, CRUD for settings
│   ├── routers/            # API routers (auth, users, nvrs, jobs, integrations, etc.)
│   ├── services/           # Business logic (compressor, downloader, health, drive, mailer, telegram, agents)
│   ├── templates/          # Jinja2 templates for Web UI
│   ├── static/             # CSS/JS for Web UI
│   └──...
├── data/                   # Runtime state (SQLite, uploads, work temp files; git-ignored)
├── requirements.txt        # Python dependencies
├── cctv_bot.py             # v2 bot (kept for reference, not used by v3)
├── run.py                  # Entrypoint for non-Docker runs
├── docker-compose.yml      # Docker service definition
├── docker-compose.agent.yml # Remote campus agent compose file
├── Dockerfile              # Container build script
├── .env.example            # Example environment file
└── .gitignore              # Excludes secrets, runtime data and legacy SDK files
```

Private migration-only files (git-ignored and excluded from the Docker image):

- `nvr_data.py` — v2 NVR list with RTSP passwords
- `google_keys.json` — Google service account
- `.env` — bootstrap secrets

Legacy `mailer.py` has no embedded credentials; supply `CCTV_SMTP_*` variables
when running the v2 bot.