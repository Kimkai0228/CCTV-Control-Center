# CCTV Control Center v3

A self-hosted CCTV operations console for Footprints International School.
Everything — the WebUI, Telegram bot and NVR health scheduler — runs in a single
container on **port 5555**, backed by SQLite.

- Responsive WebUI (desktop, tablet, phone)
- Database-backed configuration (NVRs, cameras, Drive folders, thresholds, users)
- Manual footage download: select campus → zone → NVR → cameras → date/time
- Telegram MiniApp replacing the old `/tkbaboy` flow
- NVR health monitoring: HDD, offline cameras, Telegram alerts
- QuickSync (Intel VA-API) compression with automatic `libx264` fallback
- Public URL: `https://cctv.footprints.work`

> v2 lives in a separate deployment. This directory is the v3 upgrade workspace;
> reference and are not used by the v3 app.

**Documentation**

- [User Guide](docs/USER_GUIDE.md) — short, task-focused steps for everyday operators.
- [User Manual](docs/USER_MANUAL.md) — complete reference: setup, jobs and per-camera retry, alerts, agents, API and troubleshooting.

---

## 1. Quick start (Docker)

```bash
cp .env.example .env
# edit .env: set SUPER_ADMIN_PASSWORD and a strong SECRET_KEY
docker compose up -d --build
docker compose logs -f
```

Open `http://localhost:5555` (or your public URL) and sign in with the
super-admin credentials from `.env`.

State lives in `./data` (mounted at `/app/data`):

| Path | Purpose |
| --- | --- |
| `data/cctv.db` | SQLite database (all settings, NVRs, jobs, alerts, users) |
| `data/uploads/` | Uploaded Google credential JSON |
| `data/work/` | Temporary footage while a job runs |
| `data/.secret_key` | Generated key if `SECRET_KEY` is not set |

### Hikvision SDK (`./lib`)

Hikvision and DVR exports use the proprietary Hikvision SDK from `./lib`
(`libhcnetsdk.so` and `HCNetSDKCom/`). The SDK is **not** baked into the Docker
image, so `docker-compose.yml` mounts `./lib` at `/app/lib`. Keep that directory
next to `docker-compose.yml`, and copy it to every campus agent host as well.
TruVision/ISAPI NVRs do not need it.

### GPU / QuickSync

`docker-compose.yml` maps `/dev/dri` into the container. The image installs
`ffmpeg`, `intel-media-va-driver`, `i965-va-driver` and `va-driver-all`.

The container starts its entrypoint as root only long enough to:

1. align the unprivileged `cctv` user with `PUID`/`PGID`,
2. align the `render`/`video` group GIDs with the actual `/dev/dri` nodes,
3. fix ownership of `./data`,

then drops privileges and runs the app as `cctv`. This means QuickSync works
without running the application itself as root.

Verify inside the container:

```bash
docker compose exec cctv-autonomous-v3 id
docker compose exec cctv-autonomous-v3 ls -l /dev/dri
docker compose exec cctv-autonomous-v3 vainfo
docker compose exec cctv-autonomous-v3 ffmpeg -hide_banner -encoders | grep qsv
```

Set `PUID`/`PGID` in `.env` to your host user (`id -u`, `id -g`) so files in
`./data` stay editable from the host.

If QuickSync is unavailable the compressor automatically falls back to
software `libx264`. On GPUs where the legacy MFX/QSV session is unsupported but
VA-API works (common on newer Intel generations), v3 uses the VAAPI hardware
encoder (`h264_vaapi`) instead — still the QuickSync hardware block, just a
different API. The Settings page and `/api/system/compression` show the
detected backend (`qsv` or `vaapi`) and a reason. You can also force `auto`,
`qsv`, `vaapi` or `software` under **Settings → Compression**.

### Running without Docker

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

Set `RUN_BOT=false RUN_SCHEDULER=false` to run only the WebUI.

---

## 2. Environment variables

Only bootstrap values belong in `.env`; everything else is managed in the UI.

| Variable | Default | Notes |
| --- | --- | --- |
| `SUPER_ADMIN_USER` | `superadmin` | Re-synced on every start; cannot be deleted |
| `SUPER_ADMIN_PASSWORD` | `ChangeMe!2026` | **Change this**; re-synced on start |
| `SUPER_ADMIN_EMAIL` | — | Used for the super-admin account |
| `SECRET_KEY` | auto-generated | Sessions + Fernet encryption of stored secrets |
| `PUBLIC_URL` | `https://cctv.footprints.work` | Links, MiniApp, emails |
| `PORT` / `HOST` | `5555` / `0.0.0.0` | App bind address |
| `PUID` / `PGID` | `1000` / `1000` | Runtime app user; set to your host `id -u` / `id -g` |
| `TIMEZONE` | `Asia/Phnom_Penh` | Display timezone |
| `RUN_BOT` | `true` | Start the Telegram bot thread |
| `RUN_SCHEDULER` | `true` | Start the NVR health scheduler |
| `SEED_LEGACY_NVRS` | `false` | Import a private v2 `nvr_data.py` once on first boot (migration only) |
| `DATA_DIR` / `WORK_DIR` / `UPLOAD_DIR` | under `data/` | Storage paths |
| `DEBUG` | `false` | Exposes `/api/docs` when true |

Changing `SECRET_KEY` invalidates sessions and makes previously encrypted
secrets unreadable — keep it stable.

---

## 3. First-run configuration

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

## 4. Telegram bot

| Command | Purpose |
| --- | --- |
| `/start`, `/help` | Command overview |
| `/tk` | Reply to a CCTV request; guided camera/date selection |
| `/tk folder_name` | Same, overriding the upload folder name |
| `/tkbaboy`, `/badboytk`, `/miniapp` | Open the MiniApp |
| `/health`, `/status` | NVR health summary |
| `/id`, `/chatid` | Show chat/user IDs (for `group_id` config) |

The request message must contain `Date:`, `Start Time:` and `End Time:` (plus
optional `Incident:`, `Requester:`, `Campus:`). Jobs run through the same engine
as the WebUI.

---

## 5. NVR health monitoring

The scheduler runs every `health.interval_minutes` (default 10) and records:

- NVR online/offline (with automatic recovery alerts)
- HDD status and usage vs. `health.hdd_threshold_percent` (default 90%)
- Offline cameras (per channel, with recovery)

Alerts are de-duplicated by fingerprint and repeat notifications are throttled by
`health.alert_cooldown_minutes` (default 60). Open alerts appear on **Alerts** and
in `/health`. Use **Run check now** or `POST /api/health/run` to trigger a sweep.

**Live view** — **NVRs & cameras → Cameras → Live** opens an MJPEG stream built
from ISAPI snapshots, with Pause/Resume, Fullscreen and Snapshot controls. It
works for Hikvision NVRs and analog/TVI DVR channels.

**DVR channel status** — analog channels use
`/ISAPI/System/workingstatus/chanStatus` when the firmware supports it, then fall
back to `videoInputEnabled` and `resDesc` from
`/ISAPI/System/Video/inputs/channels`. A disabled channel or `NO VIDEO` signal is
shown as **Offline** instead of **Unknown**.

**Unused DVR ports** — open **Cameras** and click **Disable** on a channel with
no camera connected. Disabled ports are ignored by discovery, health checks and
alerting, and never appear in the footage camera picker. Click **Enable** to
bring one back; **Discover** will not re-enable it.

**Quiet hours** — **Integrations → Telegram → Pause Telegram alerts during quiet
hours** stops Telegram delivery between the start and end time (default 17:00 →
07:00 in `app.timezone`). Alerts are still stored and shown on **Alerts**; only
the Telegram message is suppressed. Open problems are sent on the next sweep
after quiet hours end.

**Resolve notes** — resolving an alert (individually or with **Resolve all**)
accepts an optional note stored with the alert history. Recovery, exception and
camera-disable actions add their own note automatically.

---

## 6. Footage workflow

1. Choose campus → zone → NVR(s) → cameras.
2. Pick a date and start/end time (max 12 hours per request).
3. Optionally set the folder name (`/tk folder_name`, or the MiniApp/WebUI field).
4. The job downloads from the NVR, compresses with QuickSync, uploads to Drive,
   grants recipient access and emails links.
5. Track progress in **Jobs** or the MiniApp; cancel while running.

`jobs.max_workers` controls concurrent jobs and `compression.max_parallel`
controls parallel encoders. Main-host copies are cleaned after
`jobs.keep_local_days` (default 7); files kept on a campus agent use the
separate `agents.keep_files_days` setting (default 7) in **Settings**.

### Telegram trigger words

The global command is configurable in **Integrations → Telegram** (for example
`tk` → `/tk`), and changes reload the bot live. `/tk` always remains accepted so
existing chats never break. An agent's own trigger word (for example `/ttp`)
pins the request to that campus, so a TK operator cannot accidentally pull
footage from a remote campus.

### Alert exceptions

**Alerts → Alert exceptions** suppresses a camera, NVR, alert type or exact
fingerprint. NVR disks are usually near-full, so capacity alerts are off by
default — only disk status/corruption alerts fire unless **Settings → Health →
also alert when an HDD is nearly full** is enabled.

> SNMP and NVR temperature monitoring were removed from v3. Existing databases
> are migrated automatically: the legacy NVR columns are dropped and orphaned
> `health.temp_threshold_c` / `health.snmp_*` settings are deleted.

---

## 7. Camera REST API

Generate a key in **Settings → Campus agents & REST API** (super-admin). All
endpoints are read-only and accept either `X-API-Key` or a signed-in session.

```bash
curl -H "X-API-Key: $CCTV_API_KEY" \
  https://cctv.footprints.work:5555/api/v1/cameras

curl -H "X-API-Key: $CCTV_API_KEY" \
  "https://cctv.footprints.work:5555/api/v1/cameras?campus=tk&zone=ecp&q=gate"

curl -H "X-API-Key: $CCTV_API_KEY" \
  https://cctv.footprints.work:5555/api/v1/cameras/summary
```

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/cameras` | Paginated cameras with name, IP, MAC, online state and NVR/campus/zone |
| `GET /api/v1/cameras/summary` | Total/online/offline counts and per-campus totals |
| `GET /api/v1/cameras/{id}` | One camera |
| `GET /api/v1/nvrs` | NVR inventory with status and HDD usage |
| `GET /api/v1/health` | Same health summary as the dashboard |

Rotate the key from the same Settings card; the old key stops working
immediately.

---

## 8. Campus agents (TK / TTP)

The main host can route a job to a small agent container on another campus so
footage is downloaded and compressed on the local network instead of crossing
the WAN.

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
or `nvr_data.py` to the agent host. The agent gets NVR and Drive credentials
from the main host in the authenticated job payload, and keeps its own state in
`./agent-data`.

`AGENT_MAIN_URL` must be reachable from the campus network and should use
HTTPS. Set `AGENT_VERIFY_TLS=false` only for a self-signed certificate. The
compose file publishes port 5555 on `127.0.0.1` only, for an optional local
nginx that serves `/agent-files/`; the agent itself only needs outbound access
to the main host. Set `AGENT_BIND=0.0.0.0` in `.env.agent` only if nginx runs on
a different host, and keep its nginx location limited to `/agent-files/`
(see **Direct download from the agent**).

If you use a registry instead of a copied source directory, set
`CCTV_IMAGE` in `.env.agent` and remove `build:` from
`docker-compose.agent.yml` (or run `docker compose pull` first). You can also
transfer an image offline with `docker save` / `docker load`.

The `AGENT_CAMPUS` / `AGENT_ZONES` values are labels; job routing uses the
campus and zones stored in the main WebUI record.

3. On the main host, `/ttp` in Telegram routes that request to the TTP agent;
   `/tk` continues to run locally. The agent heartbeats every poll interval and
   posts progress back to **Jobs**. If an agent never claims a job, the main
   host optionally runs it locally after `agents.claim_timeout_minutes`.

Agents receive NVR credentials and Drive credentials only in the authenticated
HTTPS job payload. They keep their own local work directory and clean it after
upload (`AGENT_KEEP_FILES=true` to retain temporary work files for debugging).

#### Direct download from the agent (recommended for large files)

Set the agent's **Public download URL** on the **Campus agents** page (for
example `https://agent-cctv.footprints.work`) and leave
**Settings → Campus agents → Direct agent download** enabled. Finished files
then stay in `AGENT_FILES_DIR/job_<id>/` on the agent (under
`./agent-data/agent-files` in the compose file) and the browser downloads them
directly from the agent over HTTPS. The main host only signs short-lived
per-file URLs with the agent token (default 6 hours); the token is never put in
a URL and no extra secret or agent login is required. **Jobs** shows
⬇ Download, ↻ Retry/resume and ⧉ Copy link for these files, and the MiniApp
opens the same direct link.

Downloads use HTTP range requests, so a browser or download manager can resume
a very large MP4 instead of restarting it. Point nginx at the agent's port 5555
and expose `/agent-files/`, disable response buffering and use a long read
timeout:

```nginx
location /agent-files/ {
    proxy_pass http://127.0.0.1:5555;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 1h;
    send_timeout 1h;
}
```

Agent files are removed after `agents.keep_files_days` (default 7); the agent
checks every 10 minutes using the value sent in its heartbeat. A file's
retention clock is refreshed while it is being downloaded.

If **Direct agent download** is off or the public URL is blank, the agent falls
back to copying MP4s to the main host
(`POST /api/agents/jobs/{job_id}/files/chunk`) in resumable chunks, so a dropped
WAN link retries only the last chunk instead of restarting a large file. Tune
chunk size with `AGENT_UPLOAD_CHUNK_MB` (default 8) and retries with
`AGENT_UPLOAD_RETRIES` (default 4). Reverse proxies must allow each chunk body
and a generous timeout (`client_max_body_size 20m` is enough for the default
chunk size). Older agents that do not understand the direct-download job block
also use this upload path automatically.

---

## 9. Reverse proxy (HTTPS)

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
    }
}
```

Session cookies set `secure` based on the request scheme, so local HTTP testing
still works. Behind the proxy, `X-Forwarded-Proto: https` enables secure cookies.

---

## 10. Administration

- **Roles:** `superadmin`, `admin`, `operator`, `viewer`.
- Only super-admins can change the bot token, Google JSON and SMTP password;
  only admins can reach Integrations, Users and Settings.
- The super-admin account is re-synced from `.env` on every startup and cannot
  be deleted.
- `/api/docs` is available when `DEBUG=true`.
- Telegram bot tokens, Google JSON and SMTP passwords are encrypted with
  `SECRET_KEY`. Log output redacts Telegram bot tokens, and noisy third-party
  HTTP loggers stay at `WARNING` even with `DEBUG=true` so credentials in request
  URLs never reach the container logs.

### Settings layout

Settings is organized into tabs — **Branding**, **Compression**, **Jobs &
storage**, **Agents & API**, **Health**, **Security** and **Logs** — so each
section opens on its own instead of one long scrolling page. Every tab keeps a
short contextual aside with related links and environment details.

### Logs & diagnostics

The **Logs** tab has two sub-tabs:

- **Activity** — the persisted audit trail (logins, settings changes, job
  lifecycle results). Each entry has a level (`success`, `info`, `warning`,
  `error`) and can be filtered by level or searched.
- **System** — warnings/errors written to `data/logs/cctv.log` (rotated, with
  Telegram tokens redacted). Filter by severity, search, or download the file.

Only super-admins and admins can open Settings. The system log is the quickest
place to look when a job or integration fails.

### Dashboard reports

The dashboard includes customizable report charts, saved globally for every
user. Choose a **Pie** chart (distribution by source, campus, status or agent) or
an **Area** chart (trends over total requests, source, campus, status or agent),
with half or full width. Each chart has its own time range: a relative window
(7/14/30/90 days) or a **custom date range** such as `01/01/2026` to `10/08/2026`.
Custom ranges are inclusive, capped at 366 days, and validated on the server.
Only super-admins and admins can save the layout; other users see the shared
view, scoped to their own job history where applicable. Use **Reset default** to
return to the built-in *Jobs by source* pie and *Requests over time* area charts.

### Before publishing to GitHub or Docker Hub

- `.gitignore` and `.dockerignore` exclude `.env`, `data/`, `temp/`,
  `google_keys.json`, `nvr_data.py`, the Hikvision `lib/` binaries, logs,
  local screenshots, `opencode.json`, `test-synce.txt` and the legacy v2
  scripts.
- **Change the real super-admin password before publishing.** `.env.example`
  ships with the default `ChangeMe!2026`; if production still uses it, anyone
  reading the repository can sign in. Update `.env`, replace the example value
  with a non-production placeholder, and restart.
- Never commit or image `nvr_data.py` — it contains NVR RTSP passwords.
- Root-level `mailer.py` no longer contains credentials; the v3 app stores SMTP
  settings in the database.
- Build and push only after a clean build (`docker compose build --no-cache`);
  an image built before these ignore rules could still contain secrets.
- If a credential was ever committed, pushed, or baked into a public image,
  rotate it. Removing it from the latest commit or image is not enough.

---

## 11. Testing

```bash
.venv/bin/pytest -q
```

Tests cover security helpers, Telegram `initData` validation, NVR parsing and the
HTTP API (isolated `DATA_DIR`, the bot/scheduler disabled, legacy seeding off).

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
| --- | --- |
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
| A job, login or setting change failed | Open **Settings → Logs & diagnostics**; use Activity for the audit trail and System for warnings/errors |
| Agent job fails with Hikvision SDK error 7 or ISAPI connect timeout | The agent cannot reach the NVR IPs. Check the agent host firewall/VPN/routes to the camera VLANs |
| Alerts repeat every sweep | Increase `health.alert_cooldown_minutes` |
