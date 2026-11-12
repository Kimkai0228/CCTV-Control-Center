# CCTV Control Center v3 — User Manual

Complete reference for operators and administrators of the Footprints
International School CCTV console. For a short task guide, see the
[User Guide](USER_GUIDE.md).

---

## 1. Overview

CCTV Control Center v3 is a self-hosted web console that replaces the old
Telegram-only workflow. One container on **port 5555** provides:

- A responsive WebUI (desktop, tablet, phone)
- Manual footage extraction from Hikvision/DVR/ISAPI recorders
- Intel QuickSync compression with automatic software fallback
- Google Drive upload, sharing and email delivery
- Telegram bot and MiniApp
- Scheduled NVR/camera/disk health monitoring with alerts
- Optional remote **campus agents** that download footage on the local network
- Shared dashboard reports with relative or custom date ranges
- Persistent activity and system logs

Configuration lives in SQLite (`data/cctv.db`); only bootstrap values belong in
`.env`.

### Main concepts

| Term | Meaning |
| --- | --- |
| **NVR** | A recorder (Hikvision, DVR, ISAPI/TruVision) |
| **Camera** | A channel on an NVR, with a name, zone and enabled flag |
| **Job** | One footage request: download → compress → upload → share |
| **Agent** | A remote container on a campus that runs jobs locally |
| **Folder** | A Google Drive destination key mapped to a campus |
| **Report** | A saved dashboard chart shared with all users |

---

## 2. Roles and access

| Capability | Super admin | Admin | Operator |
| --- | --- | --- | --- |
| Request footage | ✅ | ✅ | ✅ |
| View own jobs, retry own failed cameras | ✅ | ✅ | ✅ |
| View all users’ jobs | ✅ | ✅ | — |
| Resolve alerts | ✅ | ✅ | ✅ |
| Manage alert exceptions | ✅ | ✅ | — |
| Manage NVRs, cameras, folders | ✅ | ✅ | — |
| Customise dashboard reports | ✅ | ✅ | — |
| Manage users | ✅ | ✅ | — |
| Settings, Integrations, API key | ✅ | — | — |

The super-admin account is defined by `SUPER_ADMIN_USER` in `.env` and is
re-synced on each start; it cannot be deleted.

---

## 3. First-run setup (administrator)

1. Copy `.env.example` to `.env` and set a strong `SUPER_ADMIN_PASSWORD` and
   stable `SECRET_KEY`.
2. Start the stack:
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
3. Sign in and create operator/admin accounts in **Users**.
4. **Integrations → Google Drive** — upload the service-account JSON, set the
   impersonation mailbox and click **Test**.
5. **Integrations → Telegram** — paste the bot token and chat IDs. Saving
   reloads the bot without a restart.
6. **Integrations → SMTP** — set the Gmail address/app password, approver and CC.
7. **NVRs & cameras** — add each recorder and click **Discover cameras**.
8. **Integrations → Drive folders** — map folder keys to campuses.
9. **Settings** — review compression, workers, health thresholds and retention.
10. Optional: register campus agents (section 12).

Hikvision/DVR export requires the proprietary SDK in `./lib`; it is mounted into
the container but never baked into the image.

---

## 4. Navigation

| Page | Purpose |
| --- | --- |
| **Dashboard** | Status tiles, report charts, health, alerts, recent jobs |
| **Footage download** | Create a footage request |
| **Jobs** | All jobs, progress, files, per-camera retry, cancel/delete |
| **Alerts** | Open/resolved alerts and exceptions |
| **NVRs & cameras** | Recorder inventory, discovery, live view, channel enable/disable |
| **Campus agents** | Remote workers, tokens, public URLs, direct download |
| **Integrations** | Google Drive, Telegram, SMTP, Drive folders |
| **Users** | Accounts, roles, passwords |
| **Settings** | Branding, compression, storage, agents/API, health, security, logs |

The sidebar can be collapsed; on phones it opens from the menu button.

---

## 5. Footage requests

Open **Footage download** and complete the form.

### 5.1 Date and time

- **Date** selects the recording day.
- **Time range** uses two 24-hour pickers (hour `00–23`, minute `00–59`).
  There is no AM/PM mode.
- The end time must be after the start time.
- A single request may cover at most **12 hours**.

Examples: `08:00 → 08:30`, `13:30 → 14:15`, `23:50 → 00:05` is **not** valid
because it crosses midnight — use two requests instead.

### 5.2 Camera selection

1. Choose **Campus** and **Zone** to filter.
2. Tick cameras, use **Select visible**, or search by name/channel.
3. Disabled channels never appear; offline cameras may still be selected.
4. The server re-validates every selection; a disabled or missing camera rejects
   the request.

### 5.3 Options

| Field | Purpose |
| --- | --- |
| **Folder / Incident name** | Drive folder and file prefix; defaults when blank |
| **Incident description** | Free text stored with the job |
| **Requester email** | Recipient for the share email |
| **Destination** | Drive folder key / server-only destination |
| **Upload to Google Drive** | Upload, share and email when enabled |
| **Email approver** | CC the configured approver |

### 5.4 What happens next

1. The job is queued and routed to a campus agent when one serves the
   campus/zone; otherwise it runs on the main host.
2. Each camera is downloaded separately.
3. Files are compressed (QuickSync/VAAPI, else `libx264`).
4. Files are uploaded to Drive (or kept locally/on the agent).
5. Recipients are granted access and emailed.
6. The job becomes **Completed**, **Partial** or **Failed**.

---

## 6. Jobs and per-camera retry

### 6.1 Statuses

| Status | Meaning |
| --- | --- |
| `queued` | Waiting for a worker |
| `dispatched` | Assigned to a campus agent, not claimed |
| `running` | Downloading/compressing/uploading |
| `completed` | All cameras succeeded |
| `partial` | Some cameras failed; successful files kept |
| `failed` | No usable file produced |
| `cancelled` | Stopped by an operator |

### 6.2 Job details

Click **View** on a job. The window shows status, source, destination, file
count, finish time, Drive link and progress. Files can be:

- **Downloaded** from the main host (retention-limited)
- **Downloaded directly from a campus agent** (signed link, resumable)
- Opened in **Google Drive**

The **↻** beside a file button resumes an interrupted browser download; the
**⧉** button copies the direct link.

### 6.3 Camera results

New jobs store a result for every selected camera. The details window shows one
row per camera:

- **Success** — green dot, file name, size and download buttons
- **Failure** — red dot, the failure reason and a **↻ Retry** button

Successful and failed cameras are shown together in the original request order.

### 6.4 Retrying

- **Retry one camera** — click **↻ Retry** on its red row.
- **Retry all failed** — click **↻ Retry all N failed cameras**.

Behaviour:

- Only the selected failed cameras are downloaded again.
- Successful files and existing Drive uploads are preserved.
- The new files are written into the **same Drive folder** as the original job.
- The job returns to `queued`/`running` and then updates to `completed`,
  `partial` or `failed` based on the merged camera results.
- A camera that previously succeeded cannot be retried, so completed work is
  never duplicated.
- Older jobs that only stored error text fall back to retrying the original
  camera selection; the button is still available on `partial`/`failed` jobs.

### 6.5 Cancelling and deleting

- **Cancel** stops a queued/dispatched/running job and keeps partial files.
- **Delete** removes the job and its local files (Drive folders are not deleted)
  and is available to admins after the job stops.

---

## 7. Dashboard and reports

### 7.1 Status

Tiles show NVRs online, total cameras, active jobs and open alerts. The page also
shows NVR health, open alerts, storage, compression backend and recent jobs.

### 7.2 Report charts

Reports are saved globally in the `dashboard.reports` setting and shown to every
user. Only super-admins/admins can change the layout.

- **Pie** — distribution by source, campus, status or agent
- **Area** — trend over total requests, source, campus, status or agent
- **Width** — half or full
- **Range** — last 7/14/30/90 days **or a custom date range**

A custom range is inclusive (`start` through `end`), limited to **366 days**, and
validated on the server. The chart footer shows the exact range. Users who are
not admins see only their own jobs in the charts.

### 7.3 Editing

1. Click **Customize**.
2. Add a chart, choose type/data/title/width.
3. Choose **Custom range…** and set **From/To** (e.g. `01/01/2026` to
   `10/08/2026`).
4. Reorder with ↑/↓, remove with ✕, or **Reset default**.
5. **Save layout**.

The default layout is a *Jobs by source* pie (30 days) and a *Requests over time*
area chart (14 days).

---

## 8. Alerts

The scheduler checks every `health.interval_minutes` (default 10 minutes):

- NVR online/offline and recovery
- Camera offline per channel and recovery
- HDD status and capacity (capacity alerts optional)

Alerts are de-duplicated by fingerprint; repeat notifications are throttled by
`health.alert_cooldown_minutes` (default 60).

- **Resolve** an alert, optionally with a note.
- **Resolve all** clears the visible open alerts.
- **Alert exceptions** silence a camera, NVR, alert type or exact fingerprint.
- **Quiet hours** suppress Telegram delivery only; alerts are still stored.
- **Run check now** triggers an immediate sweep.

NVR disks are usually near full, so capacity alerts are disabled unless
**Settings → Health → also alert when an HDD is nearly full** is enabled.

---

## 9. NVRs and cameras

### 9.1 Adding an NVR

Open **NVRs & cameras → Add NVR** and enter name, code, campus, zone, protocol,
host, ports and credentials. Passwords are encrypted with `SECRET_KEY`.

### 9.2 Discovery

**Discover cameras** fills channel names, online state and metadata. Health
sweeps keep status current.

### 9.3 Live view

**Cameras → Live** opens an MJPEG stream from ISAPI snapshots with Pause/Resume,
Fullscreen and Snapshot controls. It works for Hikvision NVRs and analog/TVI DVR
channels.

### 9.4 Disabled ports

Use **Disable** on an unused DVR port. Disabled channels are ignored by
discovery, health checks, alerting and the footage picker. **Discover** does not
re-enable them; use **Enable** explicitly.

### 9.5 DVR status

Analog channels use `chanStatus` when supported, then fall back to
`videoInputEnabled`/`resDesc`. A disabled channel or `NO VIDEO` is shown as
**Offline**, not **Unknown**.

---

## 10. Integrations

### 10.1 Google Drive

- Upload a service-account JSON and set the impersonation mailbox.
- The account needs domain-wide delegation and Drive scope.
- **Test** verifies delegation.
- Uploads create a folder per job; recipients receive access for
  `google.share_expiry_days` (default 30).

### 10.2 Telegram

- Bot token, group/alert chat IDs and the global trigger word.
- Saving reloads the bot live (no restart).
- `/tk` always works even if the trigger word changes.
- **Quiet hours** pause Telegram delivery between the configured times.
- **MiniApp** URL: `PUBLIC_URL/miniapp`, set in BotFather.
- MiniApp downloads use the `X-Telegram-Init-Data` header.

### 10.3 SMTP

Set the sender address/app password, approver and CC. Used for share emails and
approver notifications.

### 10.4 Drive folders

Map a folder key to a campus so jobs choose the correct destination. Folder keys
appear in the footage destination picker.

---

## 11. Settings

Settings is split into tabs. Each tab has a short contextual aside.

| Tab | Contents |
| --- | --- |
| **Branding** | App name, organization, public URL, theme, campus, logo, palette |
| **Compression** | Backend (auto/qsv/vaapi/software), presets, quality/CRF, parallel encoders, QuickSync probe |
| **Jobs & storage** | Max workers, local retention, agent retention, default folder name |
| **Agents & API** | Agent routing rules/timeouts, direct download, REST API toggle, API key rotation |
| **Health** | Scheduler interval, cooldown, HDD threshold, alert toggles, run check |
| **Security** | Session lifetime |
| **Logs** | Activity and System logs, filtering, search, download |

Only super-admins (and admins where noted) can open Settings and Integrations.

---

## 12. Campus agents

A campus agent downloads and compresses footage on the local network, then either
uploads directly to Drive or keeps files for direct browser download.

### 12.1 On the main host

1. Open **Campus agents → Add agent**.
2. Set campus, zones, trigger word and (optionally) public HTTPS URL.
3. Copy the generated token into `AGENT_TOKEN` on the agent host.
4. Enable **Direct agent download** in **Settings → Agents & API** if browsers
   should fetch files from the agent.

### 12.2 On the campus host

Only these are needed: `docker-compose.agent.yml`, `.env.agent`, and `lib/` for
Hikvision/DVR.

```bash
cp .env.agent.example .env.agent
# edit AGENT_MAIN_URL and AGENT_TOKEN, then:
docker compose -f docker-compose.agent.yml --env-file .env.agent up -d --build
```

Set `AGENT_BIND=0.0.0.0` only when nginx runs on another host; otherwise keep the
default loopback binding and proxy only `/agent-files/`.

Do **not** copy the main server’s `.env`, `data/cctv.db`, `google_keys.json` or
`nvr_data.py` to the agent host. The agent receives NVR/Drive credentials in the
signed job payload and keeps state in `./agent-data`.

### 12.3 Direct downloads

Finished files can stay on the agent under `/agent-files`. The main host issues
short-lived signed URLs; the agent token is never sent to the browser. Files are
retained for `agents.keep_files_days`.

---

## 13. Users and security

- **Users** — create, edit roles, deactivate, reset passwords.
- Passwords are hashed; Telegram tokens, Drive JSON and SMTP passwords are
  encrypted with `SECRET_KEY`.
- Keep `SECRET_KEY` stable: changing it invalidates sessions and makes stored
  secrets unreadable.
- Session lifetime is configured under **Settings → Security**.
- API keys are generated/rotated in **Settings → Agents & API** (super-admin).

### Camera REST API

Read-only, authenticated with `X-API-Key` or a signed-in session:

```bash
curl -H "X-API-Key: $CCTV_API_KEY" https://cctv.footprints.work:5555/api/v1/cameras
curl -H "X-API-Key: $CCTV_API_KEY" "https://cctv.footprints.work:5555/api/v1/cameras?campus=tk&zone=ecp&q=gate"
curl -H "X-API-Key: $CCTV_API_KEY" https://cctv.footprints.work:5555/api/v1/cameras/summary
curl -H "X-API-Key: $CCTV_API_KEY" https://cctv.footprints.work:5555/api/v1/nvrs
curl -H "X-API-Key: $CCTV_API_KEY" https://cctv.footprints.work:5555/api/v1/health
```

Rotating the key invalidates the old key immediately.

---

## 14. Logs and diagnostics

**Settings → Logs** has two sub-tabs:

- **Activity** — persisted audit trail: logins, settings changes, job created/
  completed/partial/failed/retried, alert actions. Filter by level or search.
- **System** — warnings/errors from `data/logs/cctv.log` (rotated; tokens
  redacted). Filter, search or **Download log**.

Only super-admins/admins can open Settings. Use the job reference and its error
message when reporting a problem.

---

## 15. Data, retention and backups

| Path | Purpose |
| --- | --- |
| `data/cctv.db` | Settings, NVRs, cameras, jobs, files, alerts, users, audit log |
| `data/logs/cctv.log` | Rotating system log |
| `data/uploads/` | Uploaded Google credential JSON |
| `data/work/` | Temporary files while a job runs |
| `data/.secret_key` | Auto-generated key when `SECRET_KEY` is unset |

Retention:

- Main-host job files: `jobs.keep_local_days` (default 7)
- Agent files: `agents.keep_files_days` (default 7)
- Drive access grants: `google.share_expiry_days` (default 30)
- Activity/system logs: rotated by the log handler

Back up `data/` (especially `cctv.db` and `.env`’s `SECRET_KEY`) before upgrades.
Never publish `.env`, `data/`, `google_keys.json`, `nvr_data.py` or `lib/`.

---

## 16. Upgrading

Main host:

```bash
docker compose up -d --build
docker compose logs -f
```

Campus agent:

```bash
docker compose -f docker-compose.agent.yml --env-file .env.agent up -d --build
```

Database schema changes are additive and applied automatically on start. Keep
`SECRET_KEY` and `data/` in place.

---

## 17. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Login redirects immediately | `SECRET_KEY` changed or cookies blocked |
| “End time must be after start time” | Times are not 24-hour or the range is inverted |
| “A single request cannot exceed 12 hours” | Split into smaller requests |
| Camera missing from picker | Disabled channel or offline/removed NVR |
| Job stays **Dispatched** | Agent offline or not polling; check **Campus agents** heartbeat |
| Job **Partial** | Retry the red camera rows in the job |
| Direct download 502 | Agent public URL/nginx not reachable; check port publishing and `/agent-files/` |
| Drive upload skipped | Drive disabled/not configured or service account invalid |
| No report data | Range has no jobs or custom dates are wrong |
| Alert not in Telegram | Quiet hours or missing chat ID; alerts still appear in the UI |
| Agent sees wrong campus | Check its trigger word/zones and `AGENTS_STRICT_CAMPUS` |

For deeper diagnosis, open **Settings → Logs** and check **Activity** first, then
**System**.
