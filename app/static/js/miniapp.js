/* CCTV V3 Telegram MiniApp */
(function () {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const initData = tg && tg.initData ? tg.initData : "";

  const state = {
    boot: null,
    tree: { campuses: [], nvrs: [] },
    folders: [],
    settings: {},
    jobs: [],
    selected: new Map(),
    campus: "",
    zone: "",
    panel: "panel-new",
    busy: false,
    timer: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const esc = (value) => App.escapeHtml(value);

  /* ------------------------------------------------------------------ */
  /* Telegram + theme                                                    */
  /* ------------------------------------------------------------------ */

  function setupTelegram() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      tg.disableVerticalSwipes && tg.disableVerticalSwipes();
      tg.setHeaderColor && tg.setHeaderColor("#424874");
      tg.setBackgroundColor && tg.setBackgroundColor("#F4EEFF");
      tg.onEvent && tg.onEvent("themeChanged", applyTelegramTheme);
    } catch (_) {}
  }

  function applyTelegramTheme() {
    if (!tg) return;
    let saved = null;
    try { saved = localStorage.getItem("cctv-theme"); } catch (_) {}
    if (saved) return;
    const theme = tg.colorScheme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme);
    const toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) toggle.dispatchEvent(new Event("click"));
    document.documentElement.setAttribute("data-theme", theme);
  }

  /* ------------------------------------------------------------------ */
  /* API                                                                 */
  /* ------------------------------------------------------------------ */

  async function miniApi(url, options) {
    const opts = Object.assign({ credentials: "same-origin", headers: {} }, options || {});
    if (initData) opts.headers["X-Telegram-Init-Data"] = initData;
    opts.headers["Accept"] = "application/json";
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    let response;
    try {
      response = await fetch(url, opts);
    } catch (_) {
      throw new Error("Cannot reach the CCTV server. Check your connection.");
    }
    let data = null;
    const text = await response.text();
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = { detail: text }; }
    }
    if (!response.ok) {
      let message = (data && data.detail) || response.statusText || "Request failed";
      if (Array.isArray(message)) message = message.map((item) => item.msg || item).join(", ");
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  /* ------------------------------------------------------------------ */
  /* Bootstrap                                                           */
  /* ------------------------------------------------------------------ */

  function showError(message) {
    $("#mini-loading").classList.add("hide");
    $("#mini-app").classList.add("hide");
    $("#mini-error").classList.remove("hide");
    if (message) $("#mini-error-text").textContent = message;
  }

  function showApp() {
    $("#mini-loading").classList.add("hide");
    $("#mini-error").classList.add("hide");
    $("#mini-app").classList.remove("hide");
  }

  async function loadBootstrap(first) {
    const data = await miniApi("/api/miniapp/bootstrap");
    state.boot = data;
    state.tree = data.tree || { campuses: [], nvrs: [] };
    state.folders = data.folders || [];
    state.settings = data.settings || {};
    state.jobs = data.jobs || [];
    if (first) {
      renderIdentity(data.identity || {});
      fillCampus();
    }
    applySettings();
    renderJobs();
    showApp();
  }

  function renderIdentity(identity) {
    const name = [identity.first_name, identity.last_name].filter(Boolean).join(" ") ||
      identity.username || "Operator";
    const role = identity.role && identity.role !== "telegram" ? " · " + identity.role : "";
    $("#mini-user").textContent = name + role;
    if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
      try { tg.setHeaderColor("#424874"); } catch (_) {}
    }
  }

  function applySettings() {
    const uploadEnabled = state.settings.upload_enabled !== false;
    const uploadToggle = $("#m-destination");
    if (!uploadEnabled) {
      uploadToggle.innerHTML = '<option value="LOCAL">🖥 Server only (upload disabled)</option>';
      uploadToggle.value = "LOCAL";
      uploadToggle.disabled = true;
    }
    const defaultFolder = state.settings.default_folder || "CCTV_Request";
    const input = $("#m-folder");
    if (!input.value) input.placeholder = `${defaultFolder} ${apiDate(todayValue())}`;
  }

  /* ------------------------------------------------------------------ */
  /* Form helpers                                                        */
  /* ------------------------------------------------------------------ */

  function todayValue() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  function timeValue(offsetMinutes) {
    const d = new Date(Date.now() + (offsetMinutes || 0) * 60000);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  function apiDate(value) {
    const parts = String(value || "").split("-");
    return parts.length === 3 ? `${parts[2]}-${parts[1]}-${parts[0]}` : value;
  }

  function campusEntry() {
    return state.tree.campuses.find((campus) => campus.key === state.campus) || null;
  }

  function campusFolders() {
    const key = state.campus;
    return state.folders.filter((folder) => !folder.campus || folder.campus === key);
  }

  /* ------------------------------------------------------------------ */
  /* Campus / zone / cameras                                             */
  /* ------------------------------------------------------------------ */

  function fillCampus() {
    const campuses = state.tree.campuses || [];
    const select = $("#m-campus");
    if (!campuses.length) {
      select.innerHTML = '<option value="">No campuses</option>';
      $("#m-zone").innerHTML = '<option value="">—</option>';
      state.campus = "";
      renderCameras();
      return;
    }
    select.innerHTML = campuses
      .map((campus) => `<option value="${esc(campus.key)}">${esc(campus.name)}</option>`)
      .join("");
    state.campus = campuses[0].key;
    select.value = state.campus;
    fillZones();
  }

  function fillZones() {
    const campus = campusEntry();
    const select = $("#m-zone");
    const zones = campus ? campus.zones || [] : [];
    select.innerHTML = '<option value="">All zones</option>' +
      zones.map((zone) => `<option value="${esc(zone.key)}">${esc(zone.name)}</option>`).join("");
    state.zone = "";
    select.value = "";
    fillDestinations();
    renderCameras();
    updateSummary();
  }

  function fillDestinations() {
    const select = $("#m-destination");
    if (!select || select.disabled) return;
    const folders = campusFolders();
    select.innerHTML = folders
      .map((folder) => `<option value="${esc(folder.key)}">${esc(folder.name)}</option>`)
      .join("") + '<option value="LOCAL">🖥 Server only (no upload)</option>';
    select.value = folders[0] ? folders[0].key : "LOCAL";
  }

  function visibleNvrs() {
    const campus = campusEntry();
    if (!campus) return [];
    const zones = state.zone
      ? (campus.zones || []).filter((zone) => zone.key === state.zone)
      : campus.zones || [];
    const rows = [];
    zones.forEach((zone) => {
      (zone.nvrs || []).forEach((nvr) => {
        const cameras = (nvr.cameras || []).filter((camera) => camera.enabled !== false);
        if (cameras.length) rows.push({ ...nvr, cameras, zone_name: zone.name });
      });
    });
    return rows;
  }

  function cameraKey(nvrId, channel) {
    return `${nvrId}:${channel}`;
  }

  function renderCameras() {
    const host = $("#m-cameras");
    const nvrs = visibleNvrs();
    if (!nvrs.length) {
      host.innerHTML = App.emptyState(
        state.campus
          ? "No cameras discovered in this campus yet. Run Discover in the WebUI."
          : "No NVRs configured yet. Add one in the WebUI.",
        ""
      );
      updateSelectedLabel();
      return;
    }
    host.innerHTML = nvrs.map((nvr) => {
      const selectedInNvr = nvr.cameras.filter((camera) => state.selected.has(cameraKey(nvr.id, camera.channel))).length;
      return `<div class="mini-cam-group">
        <div class="mini-cam-head">
          <span class="mini-cam-head-dot ${nvr.status === "online" ? "on" : ""}"></span>
          <b class="truncate">${esc(nvr.name)}</b>
          <span class="tiny faint">${selectedInNvr}/${nvr.cameras.length}</span>
          <button type="button" class="btn btn-ghost btn-xs" data-select-nvr="${nvr.id}">
            ${selectedInNvr === nvr.cameras.length ? "Clear" : "All"}
          </button>
        </div>
        <div class="mini-cams">
          ${nvr.cameras.map((camera) => {
            const key = cameraKey(nvr.id, camera.channel);
            const on = state.selected.has(key);
            return `<button type="button" class="mini-cam ${on ? "on" : ""}" data-cam="${key}"
              data-nvr="${nvr.id}" data-name="${esc(camera.name)}" data-nvrname="${esc(nvr.name)}"
              data-channel="${camera.channel}" data-online="${camera.online}">
              <span class="ch">${camera.channel}</span>
              <span class="nm truncate">${esc(camera.name)}</span>
              ${camera.online === false ? '<span class="off-dot" title="Offline"></span>' : ""}
            </button>`;
          }).join("")}
        </div>
      </div>`;
    }).join("");

    host.querySelectorAll("[data-cam]").forEach((button) => {
      button.addEventListener("click", () => toggleCamera(button));
    });
    host.querySelectorAll("[data-select-nvr]").forEach((button) => {
      button.addEventListener("click", () => {
        const nvrId = Number(button.getAttribute("data-select-nvr"));
        const nvr = nvrs.find((item) => item.id === nvrId);
        if (!nvr) return;
        const keys = nvr.cameras.map((camera) => cameraKey(nvr.id, camera.channel));
        const allOn = keys.every((key) => state.selected.has(key));
        keys.forEach((key) => {
          if (allOn) state.selected.delete(key);
          else {
            const camera = nvr.cameras.find((item) => cameraKey(nvr.id, item.channel) === key);
            state.selected.set(key, {
              nvr_id: nvr.id,
              channel: camera.channel,
              name: camera.name,
              nvr: nvr.name,
            });
          }
        });
        renderCameras();
        updateSummary();
      });
    });
    updateSelectedLabel();
  }

  function toggleCamera(button) {
    const key = button.getAttribute("data-cam");
    if (state.selected.has(key)) {
      state.selected.delete(key);
    } else {
      state.selected.set(key, {
        nvr_id: Number(button.getAttribute("data-nvr")),
        channel: Number(button.getAttribute("data-channel")),
        name: button.getAttribute("data-name"),
        nvr: button.getAttribute("data-nvrname"),
      });
    }
    button.classList.toggle("on", state.selected.has(key));
    const group = button.closest(".mini-cam-group");
    if (group) {
      const total = group.querySelectorAll("[data-cam]").length;
      const on = group.querySelectorAll("[data-cam].on").length;
      const count = group.querySelector(".mini-cam-head .tiny");
      if (count) count.textContent = `${on}/${total}`;
      const allButton = group.querySelector("[data-select-nvr]");
      if (allButton) allButton.textContent = on === total ? "Clear" : "All";
    }
    updateSummary();
  }

  function updateSelectedLabel() {
    $("#m-selected-label").textContent = `${state.selected.size} selected`;
  }

  function updateSummary() {
    const count = state.selected.size;
    const date = $("#m-date").value;
    const start = $("#m-start").value;
    const end = $("#m-end").value;
    if (!count) {
      $("#m-summary").textContent = "Select cameras to begin";
      return;
    }
    const range = date && start && end ? ` · ${apiDate(date)} ${start}–${end}` : "";
    $("#m-summary").textContent = `${count} camera${count === 1 ? "" : "s"}${range}`;
  }

  function selectAllVisible(select) {
    visibleNvrs().forEach((nvr) => {
      nvr.cameras.forEach((camera) => {
        const key = cameraKey(nvr.id, camera.channel);
        if (select) {
          state.selected.set(key, {
            nvr_id: nvr.id,
            channel: camera.channel,
            name: camera.name,
            nvr: nvr.name,
          });
        } else {
          state.selected.delete(key);
        }
      });
    });
    renderCameras();
    updateSummary();
  }

  /* ------------------------------------------------------------------ */
  /* Submit                                                              */
  /* ------------------------------------------------------------------ */

  function splitEmails(value) {
    return String(value || "")
      .replace(/;/g, ",")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }

  async function submitJob() {
    if (state.busy) return;
    if (!state.selected.size) {
      App.toast("Select at least one camera", "error");
      return;
    }
    const date = $("#m-date").value;
    const start = $("#m-start").value;
    const end = $("#m-end").value;
    if (!date || !start || !end) {
      App.toast("Choose a date and time range", "error");
      return;
    }
    if (end <= start) {
      App.toast("End time must be after start time", "error");
      return;
    }
    const folderInput = $("#m-folder").value.trim();
    const defaultFolder = state.settings.default_folder || "CCTV_Request";
    const folderName = folderInput || `${defaultFolder} ${apiDate(date)}`;
    const destination = $("#m-destination").value;
    const upload = destination !== "LOCAL" && state.settings.upload_enabled !== false;
    const email = $("#m-email").value.trim();
    const emails = splitEmails(email);

    const button = $("#m-submit");
    state.busy = true;
    button.disabled = true;
    const original = button.innerHTML;
    button.innerHTML = '<span class="spinner"></span> Starting…';

    try {
      const payload = {
        cameras: Array.from(state.selected.values()).map((camera) => ({
          nvr_id: camera.nvr_id,
          channel: camera.channel,
        })),
        date: apiDate(date),
        start_time: start,
        end_time: end,
        folder_name: folderName,
        incident: folderInput || folderName,
        campus: state.campus,
        requester_email: email || null,
        drive_folder_key: upload ? destination : null,
        upload: upload,
        notify_emails: emails,
        notify_telegram: false,
      };
      const result = await miniApi("/api/miniapp/jobs", { method: "POST", body: payload });
      App.toast(`Job started · ${result.job.ref}`, "success");
      state.selected.clear();
      $("#m-folder").value = "";
      renderCameras();
      updateSummary();
      switchPanel("panel-jobs");
      await refreshJobs();
    } catch (err) {
      if (err.status === 401 || err.status === 403) showError(err.message);
      else App.toast(err.message, "error");
    } finally {
      state.busy = false;
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Jobs                                                                */
  /* ------------------------------------------------------------------ */

  function renderJobs() {
    const host = $("#m-jobs");
    const jobs = state.jobs || [];
    const active = jobs.filter((job) => ["running", "queued", "dispatched"].includes(job.status)).length;
    $("#job-count").textContent = active || jobs.length;
    if (!jobs.length) {
      host.innerHTML = App.emptyState("No requests yet. Start your first extraction.", "");
      return;
    }
    host.innerHTML = jobs.map((job) => {
      const running = ["running", "queued", "dispatched"].includes(job.status);
      const files = App.jobFiles(job);
      const result = job.result || {};
      const errors = result.errors || [];
      return `<div class="card mini-job">
        <div class="top">
          <div style="min-width:0">
            <div class="strong truncate" style="font-size:.86rem">${esc(job.folder_name || job.ref)}</div>
            <div class="tiny faint">${job.ref} · ${App.timeAgo(job.created_at)}</div>
          </div>
          ${App.statusBadge(job.status)}
        </div>
        ${running ? `<div class="mt-2">${App.progressHtml(job.progress, job.message || "Working")}</div>` : ""}
        ${!running && job.message ? `<div class="tiny faint mt-1">${esc(job.message)}</div>` : ""}
        ${job.error ? `<div class="tiny mt-2" style="color:var(--danger)">${esc(job.error)}</div>` : ""}
        ${!running && errors.length ? `<div class="tiny mt-1" style="color:var(--warning)">${esc(errors[0])}${errors.length > 1 ? ` (+${errors.length - 1})` : ""}</div>` : ""}
        <div class="row mt-2" style="gap:6px">
          ${job.drive_link ? `<a class="btn btn-soft btn-xs" href="${esc(job.drive_link)}" target="_blank" rel="noopener">🔗 Drive</a>` : ""}
          ${files.map((file) => file.download_url ? `<button type="button" class="btn btn-ghost btn-xs btn-file" data-download="${job.id}:${file.id || ''}" data-url="${esc(file.download_url)}" data-agent="${file.agent_download ? 'true' : 'false'}" data-filename="${esc(file.filename)}" title="${esc(file.filename)}">⬇ <span class="truncate">${esc(file.filename)}</span></button>` : "").join("")}
          ${running ? `<button type="button" class="btn btn-danger btn-xs" data-cancel="${job.id}">Cancel</button>` : ""}
        </div>
        ${files.length ? `<div class="tiny faint mt-2">${files.length} file(s) · ${files.map((file) => App.fmtBytes(file.size_bytes)).join(" · ")}</div>` : ""}
      </div>`;
    }).join("");

    host.querySelectorAll("[data-cancel]").forEach((button) => {
      button.addEventListener("click", () => cancelJob(Number(button.getAttribute("data-cancel"))));
    });
    host.querySelectorAll("[data-download]").forEach((button) => {
      button.addEventListener("click", () => {
        const filename = button.getAttribute("data-filename");
        const url = button.getAttribute("data-url");
        if (button.getAttribute("data-agent") === "true") {
          openDirect(url);
          return;
        }
        const parts = button.getAttribute("data-download").split(":");
        downloadFile(Number(parts[0]), Number(parts[1]), filename);
      });
    });
  }

  async function refreshJobs() {
    const data = await miniApi("/api/miniapp/bootstrap");
    state.jobs = data.jobs || [];
    renderJobs();
  }

  async function cancelJob(id) {
    if (!(await App.confirm("Cancel this extraction? Files already downloaded are kept.", {
      danger: true,
      confirm: "Cancel job",
    }))) return;
    try {
      await miniApi(`/api/miniapp/jobs/${id}/cancel`, { method: "POST" });
      App.toast("Cancellation requested", "success");
      await refreshJobs();
    } catch (err) {
      App.toast(err.message, "error");
    }
  }

  function openDirect(url) {
    if (!url) return;
    try {
      if (tg && tg.openLink) {
        tg.openLink(url, { try_instant_view: false });
        App.toast("Opening the agent download in your browser", "info");
        return;
      }
    } catch (_) {}
    window.open(url, "_blank", "noopener");
    App.toast("Download started. If it stops, open the job again to retry.", "info");
  }

  async function downloadFile(jobId, fileId, filename) {
    const url = `/api/miniapp/jobs/${jobId}/files/${fileId}`;
    try {
      const headers = initData ? { "X-Telegram-Init-Data": initData } : {};
      const response = await fetch(url, { credentials: "same-origin", headers: headers });
      if (!response.ok) {
        let detail = "Download failed";
        try {
          const data = await response.json();
          detail = data.detail || detail;
        } catch (_) {}
        throw new Error(detail);
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = filename || "footage.mp4";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 15000);
      App.toast("Download started", "success");
    } catch (err) {
      App.toast(err.message, "error");
    }
  }

  /* ------------------------------------------------------------------ */
  /* Panels / events                                                     */
  /* ------------------------------------------------------------------ */

  function switchPanel(panel) {
    state.panel = panel;
    document.querySelectorAll(".mini-tab").forEach((button) => {
      button.classList.toggle("active", button.getAttribute("data-panel") === panel);
    });
    document.querySelectorAll(".mini-panel").forEach((element) => {
      element.classList.toggle("active", element.id === panel);
    });
    if (panel === "panel-jobs") refreshJobs().catch(() => {});
  }

  function wire() {
    $("#m-date").value = todayValue();
    $("#m-start").value = timeValue(-60);
    $("#m-end").value = timeValue(0);
    $("#m-date").addEventListener("change", updateSummary);
    $("#m-start").addEventListener("change", updateSummary);
    $("#m-end").addEventListener("change", updateSummary);
    $("#m-campus").addEventListener("change", (event) => {
      state.campus = event.target.value;
      fillZones();
    });
    $("#m-zone").addEventListener("change", (event) => {
      state.zone = event.target.value;
      fillDestinations();
      renderCameras();
      updateSummary();
    });
    $("#m-select-all").addEventListener("click", () => selectAllVisible(true));
    $("#m-clear").addEventListener("click", () => selectAllVisible(false));
    $("#m-submit").addEventListener("click", submitJob);
    document.querySelectorAll(".mini-tab").forEach((button) => {
      button.addEventListener("click", () => switchPanel(button.getAttribute("data-panel")));
    });
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.addEventListener("click", () => updateThemeColor());
    });
    updateSummary();
  }

  function updateThemeColor() {
    setTimeout(() => {
      const dark = document.documentElement.getAttribute("data-theme") === "dark";
      try {
        tg && tg.setHeaderColor && tg.setHeaderColor(dark ? "#1e1f2e" : "#424874");
        tg && tg.setBackgroundColor && tg.setBackgroundColor(dark ? "#14151f" : "#F4EEFF");
      } catch (_) {}
    }, 0);
  }

  async function start() {
    setupTelegram();
    applyTelegramTheme();
    if (tg && tg.colorScheme) {
      let saved = null;
      try { saved = localStorage.getItem("cctv-theme"); } catch (_) {}
      if (!saved) document.documentElement.setAttribute("data-theme", tg.colorScheme === "dark" ? "dark" : "light");
    }
    wire();
    try {
      await loadBootstrap(true);
    } catch (err) {
      showError(err.status === 401 || err.status === 403 ? err.message : "Could not load the CCTV MiniApp. " + err.message);
      return;
    }
    state.timer = setInterval(async () => {
      if (document.hidden) return;
      try {
        const active = (state.jobs || []).some((job) => ["running", "queued", "dispatched"].includes(job.status));
        if (state.panel === "panel-jobs" || active) await refreshJobs();
      } catch (_) {}
    }, 5000);
  }

  window.addEventListener("beforeunload", () => {
    if (state.timer) clearInterval(state.timer);
  });

  document.addEventListener("DOMContentLoaded", start);
})();
