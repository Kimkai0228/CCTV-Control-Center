# CCTV Control Center v3 — User Guide

A short, task-focused guide for everyday operators: sign in, request footage,
track a job, retry a failed camera, read alerts and use the Telegram MiniApp.

> For installation, configuration, campus agents and the API, see the
> [User Manual](USER_MANUAL.md).

---

## 1. Signing in

1. Open the WebUI, e.g. `https://cctv.footprints.work` (or `http://<server>:5555`).
2. Enter your username and password.
3. The left sidebar is the main menu. On a phone, tap the menu icon to open it.

Roles:

| Role | Can do |
| --- | --- |
| **Super admin** | Everything, including Settings, Users, Integrations and API keys |
| **Admin** | Manage NVRs, folders, users, alerts and dashboard reports |
| **Operator** | Request footage, view and retry their own jobs, view alerts |

If a menu item is missing, your role does not have access to it.

---

## 2. Requesting footage (WebUI)

Open **Footage download**.

1. **Date** — pick the recording day.
2. **Time range (24-hour)** — choose the start and end time with the hour and
   minute menus, e.g. `08:00 → 08:30`. The console uses 24-hour time only.
   A single request can cover at most **12 hours**.
3. **Campus / Zone** — narrow the camera list.
4. **Cameras** — tick one or more cameras. Use **Select visible** for the current
   filter, or the search box to find a camera by name/channel.
5. **Folder / Incident name** — optional; this becomes the Google Drive folder
   and file prefix. Leave it blank to use the default name.
6. **Incident description / Requester email** — optional notes and notification
   recipients.
7. **Destination** — choose where files go (or keep the default).
8. **Upload to Google Drive** — leave ticked to upload and share.
9. Click **Submit request**.

The request is queued and appears in **Recent jobs** on the right and in
**Jobs**.

### Time format examples

| You want | Choose |
| --- | --- |
| 8:05 AM | `08` : `05` |
| 1:30 PM | `13` : `30` |
| 11:55 PM | `23` : `55` |
| Midnight | `00` : `00` |

---

## 3. Watching a job

Open **Jobs** (or **Recent jobs** on the dashboard). Each row shows the folder
name, reference, status, file count and age.

| Status | Meaning |
| --- | --- |
| **Queued** | Waiting for a worker or campus agent |
| **Waiting for agent** | Sent to a campus agent, not claimed yet |
| **Running** | Downloading, compressing or uploading |
| **Completed** | Every camera succeeded |
| **Partial** | Some cameras failed; successful files are kept |
| **Failed** | No usable file was produced |
| **Cancelled** | Stopped by an operator |

Click **View** to open the job details. While a job is running you can watch the
progress bar and **Cancel this job**.

When the job finishes, use **Download** next to a file, **Drive** to open the
shared folder, or **⧉** to copy a direct link (agent files only).

### Reading the camera list

For multi-camera jobs the details window lists every camera separately:

- **Green dot** — the camera downloaded successfully. The file name and size are
  shown, with download buttons.
- **Red dot** — the camera failed. The reason is shown (for example
  *No recording found* or *Connection timed out*).

Failed cameras stay next to the successful ones so you can see exactly what is
missing.

---

## 4. Retrying a failed camera

You do **not** need to create a new request or re-download cameras that already
succeeded.

1. Open **Jobs → View** on a **Partial** or **Failed** job.
2. Find the red camera row.
3. Click **↻ Retry** directly next to that camera.
   - Only that camera is downloaded again.
   - Files already collected from the other cameras are kept.
   - If the job was uploaded to Google Drive, new files are added to the **same
     Drive folder**.
4. To retry every failed camera at once, click **↻ Retry all N failed cameras**
   above the list.

The job returns to **Queued** / **Running** while the retry is processed. When
it finishes, the retried camera rows update in place. A camera that succeeds is
green from then on; a camera that still fails keeps its error message.

> The small **↻** next to a file download button means *resume this browser
> download*, not *retry the camera*. Use the button in the camera row for a
> camera retry.

---

## 5. Dashboard and reports

The **Dashboard** shows:

- NVRs online, total cameras, active jobs and open alerts
- **Reports** — shared charts for every user. Each chart has its own range and
  can use a relative window (last 7/14/30/90 days) or a **custom date range**.
- NVR health, open alerts, storage and compression status
- **Recent jobs**

A chart header tells you its range, for example *Distribution · last 30 days* or
*Distribution · Jan 1, 2026 – Aug 10, 2026*.

**Customising reports** (admins only):

1. Click **Customize**.
2. Add a **Pie** or **Area** chart, choose the data, title and width.
3. Set **Time range** to a relative window or **Custom range…**, then choose
   **From** and **To** dates (for example `01/01/2026` to `10/08/2026`).
4. Reorder with ↑/↓, remove with ✕, or use **Reset default**.
5. Click **Save layout**. Every user sees the new layout; non-admin users see
   only their own job data in the charts.

A custom range is inclusive and may cover at most 366 days.

---

## 6. Alerts

Open **Alerts** to see open and resolved problems:

- NVR offline / recovered
- Camera offline / recovered
- Disk status or capacity warnings

Click an alert to resolve it; you can add a short **resolve note**. **Resolve
all** clears the visible open alerts.

Admins can add **Alert exceptions** to silence a camera, NVR, alert type or a
specific recurring alert. Exceptions are listed under **Alerts → Alert
exceptions**.

---

## 7. Telegram and the MiniApp

If your account is linked to Telegram, you can request footage in chat:

| Command | Purpose |
| --- | --- |
| `/tk` | Start a guided footage request |
| `/tk folder_name` | Same, with a custom folder name |
| `/tkbaboy` | Open the MiniApp |
| `/health` | NVR health summary |

A request message needs `Date:`, `Start Time:` and `End Time:`; optional lines
are `Incident:`, `Requester:` and `Campus:`.

The MiniApp shows your recent jobs, progress, download buttons and a **Cancel**
button. Downloads inside Telegram use the same signed links as the WebUI.

Campus trigger words (for example `/ttp`) pin a request to that campus so
footage is handled by the correct local agent.

---

## 8. Quick troubleshooting

| Problem | What to do |
| --- | --- |
| “End time must be after start time” | Use 24-hour times; the end must be later than the start (within 12 hours) |
| A camera is missing from the picker | It may be disabled or offline; ask an admin to check **NVRs & cameras** |
| Job is **Partial** | Open the job and retry the red camera rows |
| Job is **Failed** | Check the error in the job details; retry once the NVR is reachable |
| Download link expired | Main-host files expire after the retention period; ask for a new request or use the Drive folder |
| Drive button missing | Upload was disabled for that request or Drive is not configured |
| Charts show no data | Widen the chart range or clear a custom date range with no jobs |

If something still fails, open **Settings → Logs → Activity/System** (admins) or
send the job reference to your administrator.
