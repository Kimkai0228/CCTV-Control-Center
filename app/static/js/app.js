/* CCTV V3 shared front-end helpers */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function toast(message, type) {
    let host = $("#toasts");
    if (!host) {
      host = document.createElement("div");
      host.id = "toasts";
      document.body.appendChild(host);
    }
    const el = document.createElement("div");
    el.className = "toast " + (type || "info");
    el.innerHTML =
      '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">' +
      (type === "error"
        ? '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>'
        : type === "success"
        ? '<path d="M20 6 9 17l-5-5"/>'
        : '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>') +
      "</svg><span>" +
      escapeHtml(message) +
      "</span>";
    host.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateY(6px)";
      el.style.transition = "all .2s";
      setTimeout(() => el.remove(), 220);
    }, type === "error" ? 6500 : 3800);
  }

  async function api(url, options) {
    const opts = Object.assign({ credentials: "same-origin", headers: {} }, options || {});
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    opts.headers["Accept"] = "application/json";
    let response;
    try {
      response = await fetch(url, opts);
    } catch (err) {
      throw new Error("Network error – check the server connection");
    }
    let data = null;
    const text = await response.text();
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = { detail: text }; }
    }
    if (!response.ok) {
      if (response.status === 401 && !location.pathname.startsWith("/login")) {
        location.href = "/login";
      }
      let message = (data && data.detail) || response.statusText || "Request failed";
      if (Array.isArray(message)) message = message.map((m) => m.msg || m).join(", ");
      throw new Error(message);
    }
    return data;
  }

  function modal(id) {
    const el = typeof id === "string" ? document.getElementById(id) : id;
    if (!el) return null;
    return {
      open() {
        el.classList.add("open");
        const body = el.querySelector(".modal-body");
        if (body) body.scrollTop = 0;
        el.scrollTop = 0;
        document.body.style.overflow = "hidden";
      },
      close() { el.classList.remove("open"); document.body.style.overflow = ""; },
      el,
    };
  }

  function confirmDialog(message, opts) {
    opts = opts || {};
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "modal-backdrop open";
      wrap.innerHTML =
        '<div class="modal" style="max-width:430px"><div class="modal-head"><h2>' +
        escapeHtml(opts.title || "Please confirm") +
        '</h2></div><div class="modal-body"><p class="muted" style="font-size:.86rem">' +
        escapeHtml(message) +
        '</p></div><div class="modal-foot"><button class="btn btn-ghost" data-no>Cancel</button><button class="btn ' +
        (opts.danger ? "btn-danger" : "") +
        '" data-yes>' +
        escapeHtml(opts.confirm || "Confirm") +
        "</button></div></div>";
      document.body.appendChild(wrap);
      const done = (value) => {
        wrap.remove();
        document.body.style.overflow = "";
        resolve(value);
      };
      wrap.querySelector("[data-no]").onclick = () => done(false);
      wrap.querySelector("[data-yes]").onclick = () => done(true);
      wrap.addEventListener("click", (e) => { if (e.target === wrap) done(false); });
    });
  }

  function fmtBytes(bytes) {
    const value = Number(bytes || 0);
    if (!value) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
    return (value / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + " " + units[i];
  }

  function parseDate(value) {
    if (!value) return null;
    // The API serializes database timestamps as naive UTC strings. Browsers
    // interpret those as local time, which shifts every "x ago" label by the
    // UTC offset. Treat an offset-less ISO timestamp as UTC.
    if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value) && !/(Z|[+-]\d{2}:?\d{2})$/.test(value)) {
      value = value + "Z";
    }
    const date = new Date(value);
    return isNaN(date.getTime()) ? null : date;
  }

  function fmtDate(value, withTime) {
    if (!value) return "—";
    const date = parseDate(value);
    if (!date) return String(value);
    return date.toLocaleString(undefined, {
      day: "2-digit", month: "short", year: "numeric",
      ...(withTime === false ? {} : { hour: "2-digit", minute: "2-digit" }),
    });
  }

  function timeAgo(value) {
    if (!value) return "never";
    const date = parseDate(value);
    if (!date) return "never";
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 60) return "just now";
    const mins = Math.floor(seconds / 60);
    if (mins < 60) return mins + "m ago";
    const hours = Math.floor(mins / 60);
    if (hours < 24) return hours + "h ago";
    const days = Math.floor(hours / 24);
    if (days < 30) return days + "d ago";
    return fmtDate(value, false);
  }

  function statusBadge(status) {
    const map = {
      completed: ["badge-success", "Completed"],
      partial: ["badge-warning", "Partial"],
      running: ["badge-info", "Running"],
      queued: ["badge-info", "Queued"],
      dispatched: ["badge-warning", "Waiting for agent"],
      busy: ["badge-warning", "Busy"],
      failed: ["badge-danger", "Failed"],
      cancelled: ["badge", "Cancelled"],
      online: ["badge-success", "Online"],
      offline: ["badge-danger", "Offline"],
      error: ["badge-danger", "Error"],
      unknown: ["badge", "Unknown"],
      ready: ["badge-success", "Ready"],
      expired: ["badge", "Expired"],
    };
    const item = map[status] || ["badge", status || "Unknown"];
    return '<span class="badge ' + item[0] + '">' + escapeHtml(item[1]) + "</span>";
  }

  function severityBadge(severity) {
    const map = { critical: "badge-danger", warning: "badge-warning", info: "badge-info" };
    return '<span class="badge ' + (map[severity] || "badge") + '">' + escapeHtml(severity || "info") + "</span>";
  }

  function jobFiles(job) {
    const local = job.files || [];
    if (local.length) return local;
    // Agents copy finished files to the main host (JobFile rows above). Older
    // agents only reported result.files, which stay on the agent host.
    return ((job.result && job.result.files) || []).map((f) => ({
      id: f.file_id,
      filename: f.filename,
      size_bytes: f.size_bytes,
      status: f.status || ((f.compression && f.compression.compressed) ? "compressed" : "processed"),
      channel: f.channel,
      camera: f.camera,
      nvr: f.nvr,
      download_url: f.download_url || null,
      agent_download: !!f.agent_download,
      on_agent: !f.download_url,
    }));
  }

  function downloadFile(url, filename, opts) {
    opts = opts || {};
    if (!url) return;
    const link = document.createElement("a");
    link.href = url;
    link.rel = "noopener";
    if (!opts.agent) link.download = filename || "";
    document.body.appendChild(link);
    link.click();
    link.remove();
    if (opts.agent) {
      toast("Download started from the campus agent. If it stops, click Retry to resume.", "info");
    }
  }

  function progressHtml(percent, label) {
    const p = Math.max(0, Math.min(100, Number(percent || 0)));
    return (
      (label ? '<div class="progress-label"><span>' + escapeHtml(label) + "</span><span>" + p + "%</span></div>" : "") +
      '<div class="progress"><i style="width:' + p + '%"></i></div>'
    );
  }

  function emptyState(text, icon) {
    return (
      '<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
      (icon || '<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/>') +
      "</svg><div>" + escapeHtml(text || "Nothing here yet") + "</div></div>"
    );
  }

  function debounce(fn, wait) {
    let timer;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait || 250);
    };
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied to clipboard", "success");
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      toast("Copied to clipboard", "success");
    }
  }

  function autoRefresh(fn, ms) {
    let timer = null;
    let stopped = false;
    const run = async () => {
      if (stopped || document.hidden) return;
      try { await fn(); } catch (err) { /* keep silent for background refresh */ }
    };
    run();
    timer = setInterval(run, ms || 20000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) run(); });
    return () => { stopped = true; clearInterval(timer); };
  }

  function initTheme() {
    const saved = localStorage.getItem("cctv-theme");
    const preferred = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    const theme = saved || preferred;
    document.documentElement.setAttribute("data-theme", theme);
    $$("[data-theme-toggle]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        localStorage.setItem("cctv-theme", next);
        updateThemeIcon();
      });
    });
    updateThemeIcon();
  }

  function updateThemeIcon() {
    const dark = document.documentElement.getAttribute("data-theme") === "dark";
    $$("[data-theme-toggle]").forEach((btn) => {
      btn.innerHTML = dark
        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>';
    });
  }

  function initShell() {
    // Modals must live on <body>: any ancestor with transform/filter/overflow
    // can clip or offset position:fixed overlays.
    $$(".modal-backdrop").forEach((el) => {
      if (el.parentElement !== document.body) document.body.appendChild(el);
    });

    const sidebar = $("#sidebar");
    const overlay = $("#sidebar-overlay");
    const openBtn = $("#menu-toggle");
    const close = () => { sidebar && sidebar.classList.remove("open"); overlay && overlay.classList.remove("open"); };
    if (openBtn && sidebar) {
      openBtn.addEventListener("click", () => {
        sidebar.classList.toggle("open");
        overlay && overlay.classList.toggle("open", sidebar.classList.contains("open"));
      });
    }
    overlay && overlay.addEventListener("click", close);
    $$(".nav-link").forEach((link) => {
      if (!link.dataset.label) link.dataset.label = link.textContent.trim();
      link.addEventListener("click", close);
    });

    // Desktop sidebar collapse. The mobile hamburger above still opens the
    // off-canvas panel; the collapsed state only applies at desktop widths.
    const sidebarToggle = $("#sidebar-toggle");
    let collapsed = document.documentElement.getAttribute("data-sidebar") === "collapsed";
    const applySidebar = (value) => {
      document.documentElement.setAttribute("data-sidebar", value ? "collapsed" : "open");
      if (sidebarToggle) {
        sidebarToggle.setAttribute("aria-expanded", String(!value));
        sidebarToggle.title = value ? "Expand menu" : "Collapse menu";
        sidebarToggle.setAttribute("aria-label", value ? "Expand menu" : "Collapse menu");
      }
      $$(".nav-link").forEach((link) => {
        link.title = value ? (link.dataset.label || "") : "";
      });
    };
    if (sidebarToggle) {
      sidebarToggle.addEventListener("click", () => {
        collapsed = !collapsed;
        applySidebar(collapsed);
        try { localStorage.setItem("cctv-sidebar", collapsed ? "collapsed" : "open"); } catch (_) {}
      });
    }
    applySidebar(collapsed);

    const logout = $("#logout-btn");
    logout && logout.addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
      location.href = "/login";
    });

    $$("[data-modal-open]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = modal(btn.getAttribute("data-modal-open"));
        m && m.open();
      });
    });
    $$("[data-modal-close]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = btn.closest(".modal-backdrop");
        if (m) { m.classList.remove("open"); document.body.style.overflow = ""; }
      });
    });
    document.addEventListener("click", (e) => {
      if (e.target.classList && e.target.classList.contains("modal-backdrop")) {
        e.target.classList.remove("open");
        document.body.style.overflow = "";
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const open = $(".modal-backdrop.open");
        if (open) { open.classList.remove("open"); document.body.style.overflow = ""; }
      }
    });
    $$(".tabs").forEach((tabs) => {
      const buttons = $$(".tab", tabs);
      buttons.forEach((btn) => {
        btn.addEventListener("click", () => {
          buttons.forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
          const scope = tabs.parentElement;
          Array.from(scope.children).forEach((panel) => {
            if (!panel.classList.contains("tab-panel")) return;
            panel.classList.toggle("active", panel.id === btn.getAttribute("data-tab"));
          });
        });
      });
    });
    $$("[data-copy]").forEach((btn) => {
      btn.addEventListener("click", () => copyText(btn.getAttribute("data-copy")));
    });
    $$("[data-toggle-password]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const input = btn.parentElement.querySelector("input");
        input.type = input.type === "password" ? "text" : "password";
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initShell();
  });

  window.App = {
    $, $$, api, toast, modal, confirm: confirmDialog, fmtBytes, fmtDate, timeAgo,
    statusBadge, severityBadge, progressHtml, emptyState, debounce, copyText, autoRefresh,
    jobFiles, downloadFile, escapeHtml,
  };
})();
