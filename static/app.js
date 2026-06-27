// ════════════════════════════════════════════════════════════
//  PE Audit Dashboard — Phase 2 frontend (Vanilla JS, ES2020+)
//
//  Replaces st.session_state with `window.appData`.
//  Responsibilities:
//    1. Sidebar nav (show/hide views, page-title binding)
//    2. HTML5 Drag & Drop upload zone
//    3. Multipart POST to /api/upload via fetch()
//    4. Toast notifications + KPI strip rendering
//    5. Reset / clear flow
//
//  No build step. No framework. No dependencies beyond Tailwind v3
//  Play CDN + Chart.js (loaded in index.html, used in Phase 3+).
// ════════════════════════════════════════════════════════════
"use strict";

// ── Global state ────────────────────────────────────────────
//   Mirrors the role of st.session_state in the old Streamlit app.
//   Frontend code reads/writes via window.appData.* exclusively.
window.appData = {
  upload:        null,   // last resource UploadResponse JSON
  servers:       [],     // alias to upload.servers, convenience
  batch:         null,   // last BatchResponse JSON
  resource:      null,   // last ResourceResponse JSON (Phase 4)
  issues:        [],     // Issues & Waivers register (Phase 6)
  customerName:  "",     // extracted from uploaded resource document heading
  slaMatrix:     null,   // from /api/sla-matrix (Phase 7)
  slaCeilings:   null,   // from /api/sla-ceilings — customer SLA window dict
  benchmark:     null,   // merged benchmark (from /api/benchmark) (Phase 7)
  benchmarkBatch: null,  // batch-runtime-performance source slot
  benchmarkUI:    null,  // UI-benchmark source slot
  sowCompare:    null,   // from /api/sow/compare (Phase 8)
  config:        null,   // loaded from /api/config on startup
  approvals: {           // Governance sign-off state (Phase 6)
    customer_name: "",
    env_type: "",
    checklist: { batch:false, res:false, data:false, issues:false,
                 perf:false, ctrlm:false, ui:false, sow:false, res15:false },
    pe:       { name:"", approved:false, date:"" },
    customer: { name:"", approved:false, date:"" },
    notes: "",
  },
  view:     "upload",
  loadedAt: null,
};

// Theme palette — kept in sync with tailwind.config in index.html
const THEME = {
  bg:     "#060914",
  card:   "#0d1526",
  card2:  "#111d36",
  border: "#213060",
  green:  "#10d96e",
  amber:  "#f59e0b",
  red:    "#f43f5e",
  blue:   "#3b82f6",
  purple: "#a855f7",
  cyan:   "#22d3ee",
  teal:   "#2dd4bf",
  muted:  "#6b7db3",
  white:  "#f0f4ff",
};

// ── Centralised level → visual token map ─────────────────────
// Single source of truth — no more hardcoded #f43f5e scattered across 3+ places.
const LEVEL_COLOR = {
  critical: THEME.red,
  warning:  THEME.amber,
  ok:       THEME.green,
  info:     THEME.muted,
};
const LEVEL_BG = {
  critical: "rgba(244,63,94,0.08)",
  warning:  "rgba(245,158,11,0.08)",
  ok:       "rgba(16,217,110,0.08)",
  info:     "rgba(107,125,179,0.06)",
};
/** Return signal color for a finding level string. */
function _levelColor(level) { return LEVEL_COLOR[level] || THEME.muted; }
/** Return background tint for a finding level string. */
function _levelBg(level)    { return LEVEL_BG[level]    || "rgba(107,125,179,0.06)"; }

// ── Grade helpers — canonical boundaries matching findings.py ─
// A≥90, B≥75, C≥60, D≥45, F<45. Hard floor: any critical → max C.
const GRADE_LABELS = { A: "APPROVED", B: "APPROVED WITH NOTES", C: "CONDITIONAL HOLD", D: "BLOCKED — MINOR", F: "BLOCKED — MAJOR" };
const GRADE_COLORS_FINDINGS = { A: THEME.green, B: THEME.green, C: THEME.amber, D: THEME.red, F: THEME.red };
function _computeGrade(nCrit, nWarn, nOk) {
  const score = Math.max(0, Math.min(100, 100 - nCrit * 15 - nWarn * 5 + nOk * 2));
  let g = score >= 90 ? "A" : score >= 75 ? "B" : score >= 60 ? "C" : score >= 45 ? "D" : "F";
  // Hard floor: any critical caps at C (mirrors findings.py hard floor fix)
  if (nCrit > 0 && (g === "A" || g === "B")) g = "C";
  return { score, grade: g, label: GRADE_LABELS[g], color: GRADE_COLORS_FINDINGS[g] };
}

// SLA daily limit — kept in sync with the backend config loaded in loadConfig().
// Use `let` so loadConfig() can update it when the user changes settings.
let SLA_DAILY_HRS = 6.0;

// Canonical buffer-band thresholds (single source: services/pe_config.py).
// Synced from /api/config in loadConfig(). Used by the SLA buffer gauge, the
// daily-window bar colours, the shared legends and the batch narrative so every
// panel reads green/amber/red against the SAME numbers (no hardcoded 15/40).
//   buffer% > LONGJOB  → green (healthy headroom)
//   ATRISK < buffer% ≤ LONGJOB → amber (tightening)
//   buffer% ≤ ATRISK   → red (at risk / breach)
let SLA_ATRISK_PCT  = 15.0;
let SLA_LONGJOB_PCT = 40.0;

/** Map a buffer % to a shared {tone, color, label} band token. */
function _bufferBand(bufPct) {
  if (bufPct == null || isNaN(bufPct)) return { tone: "muted",   color: THEME.muted, label: "—" };
  if (bufPct <= 0)                     return { tone: "critical", color: THEME.red,   label: "Breach" };
  if (bufPct <= SLA_ATRISK_PCT)        return { tone: "critical", color: THEME.red,   label: "At risk" };
  if (bufPct <= SLA_LONGJOB_PCT)       return { tone: "warning",  color: THEME.amber, label: "Tight" };
  return { tone: "ok", color: THEME.green, label: "Healthy" };
}

// Utility job exclusion — file watchers, exports, DB backups excluded by default.
// Toggled by the user via the batch review panel toggle.
let _batchExcludeUtility = true;
// Per-job overrides: user can re-include auto-detected utility jobs or manually exclude any job.
const _batchManualInclude = new Set();  // jobs auto-detected as utility but user wants included
const _batchManualExclude = new Set();  // jobs NOT auto-detected but user manually excluded

// Live Chart.js instances — re-created on every renderBatchReview() / renderResourceReview() call
const charts = { slaBuffer: null, windowTrend: null, topJobs: null, resourceBars: null };

// ── Session boundary tracking ──────────────────────────────────────────
// Uses sessionStorage to distinguish "same-tab refresh" (restore data) from
// "new tab / new browser" (clean dashboard).  sessionStorage is scoped to
// the browser tab — closing the tab clears it automatically.
const _PE_SESSION_KEY = 'pe_active_session';

/** Mark this tab as having an active upload session. */
function _markSessionActive() {
  try { sessionStorage.setItem(_PE_SESSION_KEY, Date.now().toString()); } catch {}
}

/** True if this tab already has an active session (user uploaded files here). */
function _isSessionActive() {
  try { return !!sessionStorage.getItem(_PE_SESSION_KEY); } catch { return false; }
}

/** Clear the session marker (called on New Engagement / clear session). */
function _clearSessionMarker() {
  try { sessionStorage.removeItem(_PE_SESSION_KEY); } catch {}
}

// ── Dev-mode reset helpers ──────────────────────────────────────────────
// Two ways to get a clean slate without opening a new tab:
//   1. Add ?reset to the URL:  http://127.0.0.1:8765/?reset  → clears + reloads clean
//   2. Press Ctrl+Shift+D in the app → same effect (for mid-session dev testing)
// Normal F5 refresh preserves session data so you don't need to re-upload files.
(function _handleResetParam() {
  const url = new URL(window.location.href);
  if (url.searchParams.has("reset")) {
    // Remove the param from URL first so the clean reload doesn't loop
    url.searchParams.delete("reset");
    // Clear server session + local marker, then reload without ?reset
    fetch("/api/clear-session", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}) }).catch(() => {}).finally(() => {
        try { sessionStorage.clear(); } catch {}
        window.location.replace(url.toString());
    });
  }
})();

document.addEventListener("keydown", (e) => {
  // Ctrl+Shift+D = Dev Reset: clear session cache and reload with clean slate
  if (e.ctrlKey && e.shiftKey && e.key === "D") {
    e.preventDefault();
    devReset();
  }
});

function devReset() {
  if (!confirm("Dev Reset: clear all session data and reload?\nYour uploaded files will need to be re-uploaded.")) return;
  const btn = document.getElementById("btn-dev-reset");
  if (btn) btn.textContent = "resetting…";
  fetch("/api/clear-session", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}) }).catch(() => {}).finally(() => {
      try { sessionStorage.clear(); } catch {}
      window.location.reload();
  });
}

/** Fast 32-bit hash (djb2) for cache-key comparison — NOT cryptographic. */
function _simpleHash(str) {
  let h = 5381;
  for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
  return h >>> 0;  // unsigned
}

// Resource Review · table view state
const resourceTableState = { showAll: false, filter: "", sortKey: "cpu_pct", sortDir: -1, filterType: "", filterEnv: "", filterStatus: "" };
const RESOURCE_TABLE_PREVIEW = 25; // initial lazy slice
const AZURE_REQUIRE_FRESH_LOGIN = false; // persist sign-in across reloads/restarts (fast). Use the Sign out button to switch accounts.

// ─────────────────────────────────────────────────────────────
// Bootstrap
// ─────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initNav();
  initDropZone();
  initResetButton();
  initJsonToggle();
  initBatchUploader();
  initResourceView();
  initGovernanceTab();
  initFindingsTab();
  initSlaIntakeUploader();
  initBenchIntakeUploader();
  initBatchSlaInfoUploader();
  initSowUploadZone();
  initSlaModeSelect();
  initSowTab();
  setActiveView("upload");
  loadConfig();           // pull stored settings from backend
  _loadAzureStatusBadge(); // check Azure config status
  // Run Azure auth checks after initial shell paint for faster first render.
  setTimeout(() => { _initAzureAuthFast().catch(() => {}); }, 0);
  refreshAiStatus();      // header AI engine badge
  // Only restore cached data if this tab already had an active session
  // (same-tab refresh).  New tab / new browser → clean dashboard.
  if (_isSessionActive()) {
    refreshAuditContext().catch(() => {}); // restore session-cache data on reload
  } else {
    // Wipe any stale server-side cache from a previous engagement
    fetch("/api/clear-session", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}) }).catch(() => {});
  }
  console.info("[pe-dashboard] full shell ready (phases 2-8)");
});

async function _initAzureAuthFast() {
  // Persistent sign-in: only force a logout on a brand-new session if the
  // "require fresh login" policy is explicitly enabled. By default we keep the
  // cached credential so the dashboard is instant and never re-prompts.
  if (AZURE_REQUIRE_FRESH_LOGIN && !_isSessionActive()) {
    try { await fetch("/api/azure/browser-logout", { method: "POST" }); } catch {}
  }
  await checkAzureIdentity({ loadSubscriptions: false, timeoutMs: 2500 });
}


// ─────────────────────────────────────────────────────────────
// 1.  Navigation — show/hide view panels
// ─────────────────────────────────────────────────────────────
const VIEW_META = {
  upload:      { title: "Upload & Intake",        sub: "Drop any PE file — auto-classified and routed" },
  overview:    { title: "Executive Dashboard",    sub: "OSHS · RFCS · SRI · CRS — Batch × Resource × SLA correlation intelligence" },
  batch:       { title: "Batch Review",           sub: "Ctrl-M execution KPIs and SLA buffers" },
  resource:    { title: "Resource Review",        sub: "Server CPU, memory and disk health" },
  correlation: { title: "Correlation Analysis",   sub: "CTRL-M batch issues cross-referenced with server resource peaks" },
  insights:    { title: "PE Findings",            sub: "Automated audit intelligence + Gemini AI analysis" },
  redflags:    { title: "Red Flags & RCA",        sub: "PE investigation questions and risk priority matrix" },
  findings:    { title: "Governance",             sub: "Issues register, approval checklist and sign-off" },
  slamatrix:   { title: "SLA Matrix",             sub: "Detailed SLA compliance per job — daily · weekly · custom" },
  benchmark:   { title: "UI Benchmark",           sub: "Baseline vs Current response time comparison matrix" },
  sow:         { title: "DFU / SKU vs SOW",       sub: "Volume achievement against Statement of Work baseline — DFU · SKU · Orders · Capacity" },
  settings:    { title: "Settings",               sub: "Gemini API key, SLA defaults and configuration" },
};

function initNav() {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => setActiveView(btn.dataset.view));
  });
  // In-panel jump targets (e.g. empty-state buttons)
  document.querySelectorAll("[data-view-target]").forEach((btn) => {
    btn.addEventListener("click", () => setActiveView(btn.dataset.viewTarget));
  });
}

function setActiveView(view) {
  if (!VIEW_META[view]) return;
  window.appData.view = view;

  // Auto-close sidebar on mobile after nav click
  const sb = document.getElementById("sidebar");
  const ov = document.getElementById("sidebar-overlay");
  if (sb && !sb.classList.contains("-translate-x-full") && window.innerWidth < 1024) {
    sb.classList.add("-translate-x-full");
    if (ov) ov.classList.add("hidden");
  }

  // Toggle sidebar button styling
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    const active = btn.dataset.view === view;
    btn.classList.toggle("bg-CnavActiveBg", active);
    btn.classList.toggle("text-Cwhite",     active);
    btn.classList.toggle("border-l-2",      active);
    btn.classList.toggle("border-CnavActiveBorder", active);
    btn.classList.toggle("text-Cmuted",    !active);
    btn.classList.toggle("hover:text-Cwhite", !active);
    btn.classList.toggle("hover:bg-Ccard/40", !active);
  });

  // Toggle main panels
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.viewPanel !== view);
  });

  // Update page header
  const meta = VIEW_META[view];
  document.getElementById("page-title").textContent = meta.title;
  document.getElementById("page-sub").textContent   = meta.sub;

  // Re-evaluate the global customer-name chip — chip is permanently hidden
  // (removed from header per UX cleanup; customer still shown in Exec banner).
  const hdrChip = document.getElementById("header-customer-chip");
  if (hdrChip) {
    hdrChip.classList.add("hidden");
    hdrChip.classList.remove("inline-flex");
  }

  // Auto-refresh each intelligence tab when data is available
  const hasData = !!(window.appData.batch || window.appData.resource || window.appData.servers?.length);
  if (hasData) {
    try {
      if (view === "overview")     renderOverview();
      if (view === "insights")     triggerGenerateFindings();
      if (view === "redflags")     triggerRedFlags();
    } catch(e) { console.error("[pe-dashboard] view render error:", e); }
  } else if (view === "overview" && _isSessionActive()) {
    // Data might be in session cache — try restoring then render
    // Only attempt restore if this tab has an active session (same-tab refresh)
    refreshAuditContext().then(() => {
      if (window.appData.batch || window.appData.resource || window.appData.servers?.length)
        renderOverview();
    }).catch(() => {});
  }

  // Audit context health bar — always refresh when entering PE Findings
  if (view === "insights")     refreshAuditContext();

  // New tab hooks
  try {
    if (view === "settings")   loadSettings();
    if (view === "slamatrix") { _renderSlaCommitmentsPanel(); if (window.appData.batch) triggerSlaMatrix(); }
    if (view === "sow")        { initSowTab(); loadSowBaseline(); }
  } catch(e) { console.error("[pe-dashboard] tab init error:", e); }

  // Always refresh overview if data is present (sidebar data-status too)
  refreshDataStatus();
}


// ─────────────────────────────────────────────────────────────
// 2.  Drag & Drop upload zone
// ─────────────────────────────────────────────────────────────
function initDropZone() {
  const dropZone  = document.getElementById("azure-res-card") || document.getElementById("drop-zone");
  const fileInput = document.getElementById("file-input");
  if (!dropZone || !fileInput) return;

  // Click → open file picker (only for legacy drop-zone, not the azure card)
  if (dropZone.id === "drop-zone") {
    dropZone.addEventListener("click", () => fileInput.click());
  }

  // File picker → upload (multi-file supported)
  fileInput.addEventListener("change", (e) => {
    const picked = [...(e.target.files || [])];
    const files = picked.filter(f => isAllowed(f));
    const skipped = picked.length - files.length;
    if (skipped > 0) {
      toast("warning", "Unsupported file(s)", "Resource Report accepts only .pdf and .docx files. Use the other upload tiles for Ctrl-M, SLA Matrix, or Benchmark files.");
    }
    if (files.length) _uploadResourceFiles(files);
    fileInput.value = ""; // allow re-selecting the same file
  });

  // Drag-over visual feedback
  ["dragenter", "dragover"].forEach((ev) =>
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.classList.add("dropzone-active");
    })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    dropZone.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropZone.classList.remove("dropzone-active");
    })
  );

  // Drop → validate + upload (multi-file)
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropZone.classList.remove("dropzone-active");

    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    const valid = files.filter(f => isAllowed(f));
    const skipped = files.length - valid.length;
    if (skipped > 0) {
      toast("warning", `${skipped} file(s) skipped`, "Resource Report accepts only .pdf and .docx files. Use the other upload tiles for Ctrl-M, SLA Matrix, or Benchmark files.");
    }
    if (valid.length) _uploadResourceFiles(valid);
  });

  // Block accidental drops on the rest of the page
  ["dragover", "drop"].forEach((ev) =>
    window.addEventListener(ev, (e) => e.preventDefault())
  );
}

function isAllowed(file) {
  const name = (file.name || "").toLowerCase();
  return name.endsWith(".pdf") || name.endsWith(".docx");
}


// ─────────────────────────────────────────────────────────────
// 3.  Upload pipeline
// ─────────────────────────────────────────────────────────────
async function uploadFile(file) {
  _renderIntakeProgress("upload-status", {
    filename: file.name,
    message:  formatBytes(file.size),
    percent:  0,
    color:    "blue",
    phase:    "uploading",
  });

  const fd = new FormData();
  fd.append("file", file, file.name);

  try {
    const { ok, status, body } = await _uploadWithProgress("/api/upload", fd, (pct, loaded, total, finished) => {
      _renderIntakeProgress("upload-status", {
        filename: file.name,
        message:  finished ? `${formatBytes(file.size)} — server-side parse…`
                           : `${formatBytes(loaded)} / ${formatBytes(total)}`,
        percent:  finished ? null : pct,
        color:    "blue",
        phase:    finished ? "processing" : "uploading",
      });
    });
    const payload = body;

    if (!ok) {
      hideUploadStatus();
      const detail = payload?.detail || `HTTP ${status}`;
      toast("error", "Upload failed", detail);
      console.error("[pe-dashboard] upload failed", payload);
      return;
    }

    // ── Persist to global state (replaces st.session_state) ──
    window.appData.upload   = payload;
    window.appData.servers  = payload.servers || [];
    window.appData.loadedAt = new Date().toISOString();
    window._execCache = null; // invalidate exec dashboard cache



    hideUploadStatus();
    renderUploadResult(payload);
    toast(
      "success",
      "Upload complete",
      `${payload.server_count} server${payload.server_count === 1 ? "" : "s"} parsed from ${payload.filename}`
    );
    console.info("[pe-dashboard] window.appData updated", window.appData);

    // ── Phase 4 · auto-run Fleet Intelligence on the parsed servers
    if (Array.isArray(payload.servers) && payload.servers.length > 0) {
      processResourceServers(payload.servers).catch((err) => {
        console.error("[pe-dashboard] resource processing failed", err);
      });
    }
    refreshDataStatus();
  } catch (err) {
    hideUploadStatus();
    _handleFetchError(err, "upload");
  }
}


/** Upload multiple resource files sequentially — merges server lists. */
async function _uploadResourceFiles(files) {
  const MAX = 8;
  const batch = files.slice(0, MAX);
  if (files.length > MAX) toast("info", `Processing first ${MAX} files`, `${files.length - MAX} file(s) queued — re-upload remaining after this batch.`);

  let allServers = [...(window.appData.servers || [])];
  let lastPayload = null;
  let totalNew = 0;

  for (let i = 0; i < batch.length; i++) {
    const f = batch[i];
    const prefix = `${i + 1}/${batch.length}`;
    _renderIntakeProgress("upload-status", {
      filename: f.name,
      message:  `File ${prefix} · ${formatBytes(f.size)}`,
      percent:  0,
      color:    "blue",
      phase:    "uploading",
    });
    const fd = new FormData();
    fd.append("file", f, f.name);
    try {
      const { ok, status, body } = await _uploadWithProgress("/api/upload", fd, (pct, loaded, total, finished) => {
        _renderIntakeProgress("upload-status", {
          filename: f.name,
          message:  finished ? `${prefix} · ${formatBytes(f.size)} — parsing…`
                             : `${prefix} · ${formatBytes(loaded)} / ${formatBytes(total)}`,
          percent:  finished ? null : pct,
          color:    "blue",
          phase:    finished ? "processing" : "uploading",
        });
      });
      const payload = body;
      if (!ok) {
        toast("error", `${f.name} failed`, (payload?.detail || `HTTP ${status}`).slice(0, 120));
        continue;
      }
      // Merge servers — avoid duplicates by hostname
      const newServers = (payload.servers || []).filter(s => {
        const h = (s.host || s.server || "").toLowerCase();
        return !allServers.some(e => (e.host || e.server || "").toLowerCase() === h);
      });
      allServers = [...allServers, ...newServers];
      totalNew += newServers.length;
      lastPayload = payload;
    } catch (err) {
      toast("error", `${f.name} error`, String(err?.message || err));
    }
  }

  hideUploadStatus();
  if (!lastPayload) return;



  // Update global state with merged server list
  window.appData.upload  = { ...lastPayload, servers: allServers, server_count: allServers.length };
  window.appData.servers = allServers;
  window.appData.loadedAt = new Date().toISOString();
  renderUploadResult(window.appData.upload);
  toast("success", `${batch.length} file(s) processed`, `${totalNew} new server(s) merged · ${allServers.length} total`);
  if (allServers.length > 0) processResourceServers(allServers).catch(() => {});
  refreshDataStatus();
}




// ─────────────────────────────────────────────────────────────
// 4.  Result rendering (KPI strip + raw JSON dump)
// ─────────────────────────────────────────────────────────────
function renderUploadResult(payload) {
  const card = document.getElementById("upload-result");
  if (!card) return;
  card.classList.remove("hidden");

  setText("kpi-filename",     payload.filename);
  setText("kpi-filetype",     (payload.file_type || "").toUpperCase());
  setText("kpi-server-count", String(payload.server_count ?? 0));
  setText("kpi-mode",         payload.image_only ? "Image-only" : "Text-extracted");

  // Header chip + reset button
  const chip = document.getElementById("dataset-chip");
  if (chip) {
    chip.textContent = `${payload.filename} · ${payload.server_count} servers`;
    chip.classList.remove("hidden");
  }
  document.getElementById("reset-btn")?.classList.remove("hidden");

  // Pre-stage the JSON dump (collapsed by default)
  const dump = document.getElementById("json-dump");
  if (dump) dump.textContent = JSON.stringify(payload, null, 2);

  // Keep the intake card concise; do not surface the post-upload AI
  // briefing on the first page even if the API returns it.
  const aiCard  = document.getElementById("upload-ai-card");
  const aiText  = document.getElementById("upload-ai-text");
  const aiModel = document.getElementById("upload-ai-model");
  if (aiCard && aiText) {
    aiText.textContent = "";
    if (aiModel) aiModel.textContent = "—";
    aiCard.classList.add("hidden");
  }

  // ── Unified intake page: show status card + resource dot ──
  document.getElementById("intake-status-row")?.classList.remove("hidden");
  document.getElementById("upload-next-prompt")?.classList.remove("hidden");
  const dot = document.getElementById("res-status-dot");
  if (dot) { dot.classList.remove("bg-Cmuted/40"); dot.classList.add("bg-Cblue", "animate-pulse"); }
}

function _renderBatchIntakeCard(payload) {
  // Show the batch result card in the Upload & Intake page
  const card = document.getElementById("batch-result-card");
  if (card) card.classList.remove("hidden");
  setText("batch-filename-kpi",   payload.filename);
  setText("batch-runs-kpi",       (payload.kpis.total_runs || 0).toLocaleString());
  setText("batch-compliance-kpi", (payload.kpis.compliance_pct ?? 0).toFixed(1) + "%");
  const breachEl = document.getElementById("batch-breach-kpi");
  if (breachEl) {
    breachEl.textContent = payload.kpis.jobs_breach ?? 0;
    breachEl.className   = `text-xl font-extrabold mt-0.5 ${payload.kpis.jobs_breach > 0 ? "text-Cred" : "text-Cgreen"}`;
  }
  document.getElementById("intake-status-row")?.classList.remove("hidden");
  document.getElementById("upload-next-prompt")?.classList.remove("hidden");
  const dot = document.getElementById("batch-status-dot");
  if (dot) { dot.classList.remove("bg-Cmuted/40"); dot.classList.add("bg-Cgreen", "animate-pulse"); }
  // Batch tab: hide no-data prompt, show loaded chip
  document.getElementById("batch-no-data-prompt")?.classList.add("hidden");
  const chip = document.getElementById("batch-loaded-chip");
  if (chip) chip.classList.remove("hidden");
  // Show refresh button so user can manually re-sync charts with SLA changes
  document.getElementById("batch-refresh-btn")?.classList.remove("hidden");
  const _kpis = payload.kpis || {};
  setText("batch-dataset-chip", `${payload.filename} · ${(_kpis.total_runs || 0).toLocaleString()} runs`);

  // Customer name — sourced ONLY from the Ctrl-M filename (server-side extraction).
  const cust = (payload.customer_name || "").trim();
  window.appData.customerName = cust;
  const totalRuns = _kpis.total_runs || 0;
  applyCustomerName(cust, { runs: totalRuns, filename: payload.filename });
}

/**
 * Single source of truth for customer-name UI updates.
 * Updates: in-page Batch Review chip, global header chip, and Executive Dashboard banner.
 * Pass an empty string to hide everything.
 */
function applyCustomerName(name, opts = {}) {
  const cust = (name || "").trim();
  window.appData.customerName = cust;

  // 1. Batch Review chip (under the "loaded" chip)
  const cnChip = document.getElementById("customer-name-chip");
  const cnText = document.getElementById("customer-name-text");
  if (cnChip && cnText) {
    if (cust) {
      cnText.textContent = cust;
      cnChip.classList.remove("hidden");
      cnChip.classList.add("flex");
    } else {
      cnChip.classList.add("hidden");
      cnChip.classList.remove("flex");
    }
  }

  // 2. Global header chip — permanently hidden (removed per UX cleanup;
  //    customer name still appears in the Executive view banner).
  const hdrChip = document.getElementById("header-customer-chip");
  if (hdrChip) {
    hdrChip.classList.add("hidden");
    hdrChip.classList.remove("inline-flex");
  }

  // 3. Executive Dashboard banner (large) + Audit Pulse strip
  const exBanner = document.getElementById("exec-customer-banner");
  const exName   = document.getElementById("exec-customer-name");
  if (exBanner && exName) {
    if (cust) {
      exName.textContent = cust;
      const runs = opts.runs ?? (window.appData?.batch?.kpis?.total_runs || 0);
      const fn   = opts.filename || window.appData?.batch?.filename || "";
      _renderAuditPulse({ customer: cust, runs, filename: fn });
      exBanner.classList.remove("hidden");
      exBanner.classList.add("flex");
    } else {
      exBanner.classList.add("hidden");
      exBanner.classList.remove("flex");
    }
  }
}

// ── Audit Pulse ── Grafana-style multi-tile strip with sparkline + audit id
function _renderAuditPulse({ customer, runs, filename }) {
  const wrap = document.getElementById("exec-audit-pulse");
  if (!wrap) return;
  wrap.classList.remove("hidden");
  wrap.classList.add("flex");

  // ── Compute date span from window data (server day buckets) ──
  const winRows = window.appData?.batch?.window || [];
  const dates = winRows
    .map(w => w?.run_date)
    .filter(Boolean)
    .map(d => new Date(d))
    .filter(d => !isNaN(d))
    .sort((a, b) => a - b);
  const fmtShort = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  let rangeStr = "—", spanStr = "—";
  if (dates.length) {
    const dMin = dates[0], dMax = dates[dates.length - 1];
    rangeStr = `${fmtShort(dMin)} → ${fmtShort(dMax)}`;
    const days = Math.round((dMax - dMin) / 86400000) + 1;
    spanStr = `${days} day${days !== 1 ? "s" : ""} · ${(runs || 0).toLocaleString()} runs`;
  } else {
    rangeStr = (runs || 0).toLocaleString() + " runs";
    spanStr  = filename ? _truncMid(filename, 28) : "—";
  }
  setText("exec-pulse-range", rangeStr);
  setText("exec-pulse-span",  spanStr);

  // ── Sparkline of daily run counts (with breach overlay) ──
  const svg = document.getElementById("exec-pulse-sparkline");
  const sla = window.appData?.batch?.kpis?.sla_daily_hrs || 6;
  if (svg && winRows.length) {
    const vals    = winRows.map(w => Number(w.runs || w.total_runs || 0));
    const breach  = winRows.map(w => (Number(w.total_hrs || 0) > sla) ? 1 : 0);
    const W = 160, H = 30, P = 2;
    const max = Math.max(1, ...vals);
    const step = vals.length > 1 ? (W - 2 * P) / (vals.length - 1) : 0;
    const points = vals.map((v, i) => {
      const x = P + i * step;
      const y = H - P - (v / max) * (H - 2 * P);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    const areaPath =
      `M ${P},${H - P} L ${points.split(" ").join(" L ")} L ${(W - P).toFixed(1)},${H - P} Z`;
    const dots = vals.map((v, i) => {
      const x = P + i * step;
      const y = H - P - (v / max) * (H - 2 * P);
      const c = breach[i] ? "#f43f5e" : "#22d3ee";
      return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.6" fill="${c}"/>`;
    }).join("");
    svg.innerHTML = `
      <defs>
        <linearGradient id="pulseGrad" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="#22d3ee" stop-opacity="0.55"/>
          <stop offset="100%" stop-color="#22d3ee" stop-opacity="0.05"/>
        </linearGradient>
      </defs>
      <path d="${areaPath}" fill="url(#pulseGrad)" stroke="none"/>
      <polyline points="${points}" fill="none" stroke="#22d3ee" stroke-width="1.4"
                stroke-linecap="round" stroke-linejoin="round"/>
      ${dots}
    `;
    const breachDays = breach.reduce((a, b) => a + b, 0);
    setText("exec-pulse-total", `↑ peak ${max}`);
    const legend = breachDays > 0
      ? `${breachDays} breach day${breachDays !== 1 ? "s" : ""} · ${vals.length} pts`
      : `✓ ${vals.length} clean days`;
    const legEl = document.getElementById("exec-pulse-legend");
    if (legEl) {
      legEl.textContent = legend;
      legEl.className = `text-[9px] mt-0.5 ${breachDays > 0 ? "text-Cred" : "text-Cgreen"}`;
    }
  } else if (svg) {
    svg.innerHTML = `<text x="80" y="18" text-anchor="middle" fill="#475569" font-size="9" font-family="Sora">No daily data yet</text>`;
    setText("exec-pulse-total", (runs || 0).toLocaleString());
    setText("exec-pulse-legend", "awaiting batch processing");
  }

  // ── Audit ID + freshness ──
  const seed   = `${customer}|${filename || ""}|${runs || 0}|${(dates[0] || new Date()).toISOString().slice(0, 10)}`;
  const idHash = _shortHash(seed);
  setText("exec-pulse-id", `#${idHash}`);
  const now = new Date();
  setText("exec-pulse-time",
    `live · ${now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}`);
  const dot = document.getElementById("exec-pulse-dot");
  if (dot) {
    const compliance = window.appData?.batch?.kpis?.compliance_pct ?? 100;
    dot.style.background = compliance >= 95 ? "#10d96e" : compliance >= 80 ? "#f59e0b" : "#f43f5e";
  }
}

function _shortHash(s) {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(36).toUpperCase().slice(0, 6).padStart(6, "0");
}

function _truncMid(s, n) {
  if (!s || s.length <= n) return s || "";
  const half = Math.floor((n - 1) / 2);
  return s.slice(0, half) + "…" + s.slice(-half);
}

function showUploadStatus(filename, message, busy = false) {
  _renderIntakeProgress("upload-status", {
    filename, message,
    percent: busy ? null : 100,
    color:   "blue",
    phase:   busy ? "uploading" : "done",
  });
}

function hideUploadStatus() {
  document.getElementById("upload-status")?.classList.add("hidden");
}

// ── Beautiful progress card (shared by all 4 intake zones) ─────────────────
// opts: { filename, message, percent (0..100|null=indeterminate), color, phase }
const _INTAKE_PALETTE = {
  blue:   { hex: "#3b82f6", g1: "#3b82f6", g2: "#22d3ee", g3: "#a855f7", glow: "59,130,246"  },
  green:  { hex: "#10b981", g1: "#10b981", g2: "#22d3ee", g3: "#3b82f6", glow: "16,185,129"  },
  amber:  { hex: "#f59e0b", g1: "#f59e0b", g2: "#fb923c", g3: "#ef4444", glow: "245,158,11"  },
  purple: { hex: "#a855f7", g1: "#a855f7", g2: "#6366f1", g3: "#3b82f6", glow: "168,85,247"  },
  teal:   { hex: "#14b8a6", g1: "#14b8a6", g2: "#06b6d4", g3: "#3b82f6", glow: "20,184,166"  },
  cyan:   { hex: "#22d3ee", g1: "#22d3ee", g2: "#38bdf8", g3: "#818cf8", glow: "34,211,238"  },
};

function _renderIntakeProgress(containerId, opts = {}) {
  const wrap = document.getElementById(containerId);
  if (!wrap) return;
  const colorKey = opts.color || wrap.dataset.color || "blue";
  const pal      = _INTAKE_PALETTE[colorKey] || _INTAKE_PALETTE.blue;
  const filename = opts.filename || "—";
  const message  = opts.message  || "";
  const phase    = (opts.phase   || "uploading").toLowerCase();
  const pct      = (opts.percent === null || opts.percent === undefined)
                   ? null : Math.max(0, Math.min(100, Math.round(opts.percent)));
  const indeterminate = pct === null;
  const pctText  = indeterminate ? "···" : `${pct}%`;

  const PHASE = {
    uploading:  { label: "Uploading",  icon: "↑" },
    processing: { label: "Processing", icon: "⚙" },
    parsing:    { label: "Parsing",    icon: "⌬" },
    analysing:  { label: "Analysing",  icon: "✦" },
    done:       { label: "Done",       icon: "✓" },
    error:      { label: "Error",      icon: "✕" },
  };
  const ph = PHASE[phase] || PHASE.uploading;
  const active = phase !== "done" && phase !== "error";

  const grad = `linear-gradient(90deg, ${pal.g1} 0%, ${pal.g2} 50%, ${pal.g3} 100%)`;
  const cardBg = `linear-gradient(135deg, rgba(${pal.glow},0.08) 0%, rgba(20,25,45,0.4) 60%, rgba(10,12,24,0.4) 100%)`;

  wrap.classList.remove("hidden");
  wrap.style.cssText = `
    border:1px solid rgba(${pal.glow},0.35);
    background:${cardBg};
    border-radius:0.75rem;
    padding:0.75rem;
    box-shadow:0 0 24px -6px rgba(${pal.glow},0.35);
    transition:all 0.2s ease;
  `;
  wrap.innerHTML = `
    <div class="flex items-center gap-2.5 mb-2">
      <div class="relative w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
           style="background:rgba(${pal.glow},0.15);border:1px solid rgba(${pal.glow},0.45)">
        <span style="color:${pal.hex};font-size:14px;font-weight:700">${ph.icon}</span>
        ${active
          ? `<span class="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full animate-ping" style="background:${pal.hex}"></span>
             <span class="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full" style="background:${pal.hex}"></span>`
          : ""}
      </div>
      <div class="flex-1 min-w-0">
        <div class="text-[11px] font-semibold text-Cwhite truncate" title="${_esc(filename)}">${_esc(filename)}</div>
        <div class="text-[9px] text-Cmuted truncate flex items-center gap-1.5">
          <span class="font-bold uppercase tracking-wider" style="color:${pal.hex}">${ph.label}</span>
          ${message ? `<span class="text-Cborder">·</span><span class="truncate">${_esc(message)}</span>` : ""}
        </div>
      </div>
      <div class="text-[13px] font-bold font-mono tabular-nums shrink-0"
           style="color:${pal.hex}">${pctText}</div>
    </div>
    <div class="relative h-1.5 rounded-full overflow-hidden"
         style="background:rgba(8,11,22,0.8);border:1px solid rgba(${pal.glow},0.2)">
      ${indeterminate
        ? `<div style="position:absolute;top:0;bottom:0;width:33%;border-radius:9999px;background:${grad};box-shadow:0 0 12px rgba(${pal.glow},0.7);animation:intake-shimmer 1.4s ease-in-out infinite"></div>`
        : `<div style="height:100%;width:${pct}%;border-radius:9999px;background:${grad};box-shadow:0 0 12px rgba(${pal.glow},0.6);transition:width 0.2s ease-out"></div>`}
    </div>`;
}

// One-time keyframes for the indeterminate shimmer
(function _injectIntakeProgressCSS() {
  if (typeof document === "undefined") return;
  if (document.getElementById("intake-progress-css")) return;
  const s = document.createElement("style");
  s.id = "intake-progress-css";
  s.textContent = `@keyframes intake-shimmer { 0% { left:-33%; } 100% { left:100%; } }`;
  document.head.appendChild(s);
})();

// XHR uploader that emits real upload-percent for the progress card.
// Falls back to indeterminate after upload completes (server-side processing).
function _uploadWithProgress(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && typeof onProgress === "function") {
        onProgress(Math.round((e.loaded / e.total) * 100), e.loaded, e.total);
      }
    };
    xhr.upload.onload = () => {
      // Bytes finished sending — flip to indeterminate "processing"
      if (typeof onProgress === "function") onProgress(100, 0, 0, true);
    };
    xhr.onload = () => {
      const text = xhr.responseText || "";
      let body;
      try { body = JSON.parse(text); } catch { body = { detail: text }; }
      resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, body, text });
    };
    xhr.onerror = () => reject(_makeNetworkError(url));
    xhr.ontimeout = () => reject(new Error(`Request timed out after ${Math.round(xhr.timeout/1000)}s — server may be overloaded`));
    xhr.send(formData);
  });
}

/**
 * Returns a human-readable Error for a failed fetch/XHR.
 * Distinguishes connection-refused (server down) from other issues.
 */
function _makeNetworkError(url) {
  return new Error(
    "Cannot reach the server — make sure it is running.\n" +
    `Expected: ${location.origin} → ${url}\n` +
    "Run: py -3.14 main.py"
  );
}

/**
 * Wrap a fetch() call with a structured error.
 * Returns {ok, status, body} like _uploadWithProgress, never throws.
 * On network error, shows a toast and returns {ok:false, status:0, body:null}.
 */
async function _apiFetch(url, options = {}) {
  try {
    const res = await fetch(url, options);
    let body = null;
    try { body = await res.json(); } catch { /* non-JSON body */ }
    return { ok: res.ok, status: res.status, body };
  } catch (err) {
    const msg = String(err?.message || err);
    const isConnRefused = msg.toLowerCase().includes("failed to fetch") ||
                          msg.toLowerCase().includes("networkerror") ||
                          msg.toLowerCase().includes("load failed");
    if (isConnRefused) {
      _showServerDownBanner();
      toast("error", "Server unreachable",
        "The PE Dashboard server is not responding. " +
        "Start it with: py -3.14 main.py (auto-finds a free port)");
    } else {
      toast("error", "Network error", msg.split("\n")[0]);
    }
    return { ok: false, status: 0, body: null };
  }
}

/** Show a persistent red banner at the top when the server is down. */
let _serverDownPollId = null;  // dedup: only one poll interval at a time
function _showServerDownBanner() {
  if (document.getElementById("server-down-banner")) return;
  const banner = document.createElement("div");
  banner.id = "server-down-banner";
  banner.className = "fixed top-0 left-0 right-0 z-[9999] bg-Cred/90 text-white text-xs font-semibold px-4 py-2 flex items-center justify-between gap-4";
  banner.innerHTML = `
    <span>⚠ Server unreachable — start the server and reload the page</span>
    <button onclick="this.parentElement.remove()" class="opacity-70 hover:opacity-100 text-lg leading-none">&times;</button>
  `;
  document.body.prepend(banner);
  // Auto-retry: poll /api/config every 5s — dedup to avoid multiple intervals
  if (_serverDownPollId) clearInterval(_serverDownPollId);
  _serverDownPollId = setInterval(async () => {
    try {
      const r = await fetch("/api/config", { signal: AbortSignal.timeout(2000) });
      if (r.ok) {
        clearInterval(_serverDownPollId);
        _serverDownPollId = null;
        document.getElementById("server-down-banner")?.remove();
        toast("success", "Server reconnected", "The PE Dashboard server is back online.");
      }
    } catch { /* still down */ }
  }, 5000);
}

/**
 * Central handler for all fetch/XHR catch blocks.
 * Distinguishes "server not running" from other errors and shows
 * an actionable message + persistent banner for connection-refused.
 */
function _handleFetchError(err, label) {
  const msg = String(err?.message || err);
  const isConnRefused = msg.toLowerCase().includes("failed to fetch") ||
                        msg.toLowerCase().includes("networkerror") ||
                        msg.toLowerCase().includes("load failed") ||
                        msg.toLowerCase().includes("cannot reach") ||
                        msg.toLowerCase().includes("connection refused");
  console.error(`[pe-dashboard] ${label || "fetch"} error`, err);
  if (isConnRefused) {
    _showServerDownBanner();
    toast("error", "Server unreachable",
      "The PE Dashboard server is not responding. " +
      "Start it with: py -3.14 main.py (auto-finds a free port)");
  } else {
    toast("error", "Network error", msg.split("\n")[0].slice(0, 200));
  }
}

function initResetButton() {
  // #reset-btn wiring is handled via onclick="clearSessionData()" in the HTML.
  // This function is kept as a no-op so the DOMContentLoaded call sequence is unchanged.
  // The old partial-reset logic was superseded by clearSessionData() (full hard reset).
}

function initJsonToggle() {
  const btn  = document.getElementById("toggle-json");
  const dump = document.getElementById("json-dump");
  if (!btn || !dump) return;
  btn.addEventListener("click", () => {
    const hidden = dump.classList.toggle("hidden");
    btn.textContent = hidden ? "Show raw JSON" : "Hide raw JSON";
  });
}


// ─────────────────────────────────────────────────────────────
// 5.  Toast notifications
// ─────────────────────────────────────────────────────────────
const TOAST_STYLES = {
  success: {
    bar:  "bg-Cgreen",
    icon: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor" class="w-5 h-5 text-Cgreen"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5"/></svg>',
  },
  error: {
    bar:  "bg-Cred",
    icon: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor" class="w-5 h-5 text-Cred"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z"/></svg>',
  },
  info: {
    bar:  "bg-Cblue",
    icon: '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor" class="w-5 h-5 text-Cblue"><path stroke-linecap="round" stroke-linejoin="round" d="m11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z"/></svg>',
  },
};

function toast(kind, title, message, ttlMs) {
  // Default TTL: errors stay 12s (user needs time to read), others 4.5s
  if (ttlMs === undefined) ttlMs = kind === "error" ? 12000 : 4500;
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const style = TOAST_STYLES[kind] || TOAST_STYLES.info;

  const el = document.createElement("div");
  el.className =
    "toast pointer-events-auto rounded-xl border border-Cborder bg-Ccard/95 backdrop-blur " +
    "shadow-panel flex items-start gap-3 p-3 pr-4 overflow-hidden relative";
  el.innerHTML = `
    <span class="absolute left-0 top-0 bottom-0 w-1 ${style.bar}"></span>
    <div class="pl-2">${style.icon}</div>
    <div class="flex-1 min-w-0">
      <div class="text-[13px] font-bold text-Cwhite leading-tight">${escapeHtml(title)}</div>
      <div class="text-[11px] text-Cmuted mt-0.5 leading-snug">${escapeHtml(message)}</div>
    </div>
    <button class="text-Cmuted hover:text-Cwhite transition-colors -mt-0.5 text-lg leading-none"
            aria-label="Dismiss">&times;</button>
  `;
  el.querySelector("button").addEventListener("click", () => removeToast(el));
  stack.appendChild(el);

  if (ttlMs > 0) setTimeout(() => removeToast(el), ttlMs);
}

function removeToast(el) {
  if (!el || !el.parentNode) return;
  el.style.transition = "opacity .18s ease, transform .18s ease";
  el.style.opacity = "0";
  el.style.transform = "translateY(8px)";
  setTimeout(() => el.remove(), 200);
}


// ─────────────────────────────────────────────────────────────
// 6.  Tiny helpers
// ─────────────────────────────────────────────────────────────
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value ?? "—";
}

function formatBytes(n) {
  if (!n && n !== 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${_n(v).toFixed(_n(v) >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function hexA(hex, alpha) {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

// ════════════════════════════════════════════════════════════
//  ENTERPRISE CHART INFRASTRUCTURE
//  Grafana/Datadog-level utilities: zoom, export, crosshair,
//  threshold bands, time sync, and chart toolbar.
// ════════════════════════════════════════════════════════════

// ── Chart.js zoom config factory ──────────────────────────
// Returns a zoom plugin config block for any Chart.js chart.
// Usage: plugins: { zoom: _zoomConfig() }
function _zoomConfig(opts = {}) {
  const { mode = "x", onZoomComplete } = opts;
  return {
    pan: {
      enabled: true,
      mode,
      modifierKey: null,
    },
    zoom: {
      wheel:  { enabled: true, speed: 0.05 },
      pinch:  { enabled: true },
      drag:   { enabled: true, backgroundColor: hexA(THEME.blue, 0.12),
                borderColor: hexA(THEME.blue, 0.4), borderWidth: 1 },
      mode,
      onZoomComplete: onZoomComplete || undefined,
    },
    limits: {
      x: { minRange: 3 },  // prevent zooming to <3 data points
      y: { minRange: 5 },
    },
  };
}

// ── Chart.js crosshair plugin ─────────────────────────────
// Vertical guide line that follows the mouse — standard in Grafana/Datadog.
const crosshairPlugin = {
  id: "crosshairGuide",
  afterEvent(chart, args) {
    const evt = args.event;
    if (evt.type === "mousemove" && chart.chartArea) {
      chart._crosshairX = (evt.x >= chart.chartArea.left && evt.x <= chart.chartArea.right) ? evt.x : null;
    } else if (evt.type === "mouseout") {
      chart._crosshairX = null;
    } else {
      return; // only redraw for mousemove / mouseout
    }
    // Throttle to one redraw per animation frame — prevents chart.draw() being
    // called hundreds of times per second on fast mouse movement (the primary
    // cause of the Firefox "page slowing down" warning on Resource Review).
    if (!chart._crosshairRaf) {
      chart._crosshairRaf = requestAnimationFrame(() => {
        chart._crosshairRaf = null;
        chart.draw();
      });
    }
  },
  afterDraw(chart) {
    if (!chart._crosshairX || !chart.chartArea) return;
    const ctx = chart.ctx;
    const { top, bottom } = chart.chartArea;
    ctx.save();
    ctx.beginPath();
    ctx.setLineDash([4, 3]);
    ctx.lineWidth = 1;
    ctx.strokeStyle = hexA(THEME.muted, 0.45);
    ctx.moveTo(chart._crosshairX, top);
    ctx.lineTo(chart._crosshairX, bottom);
    ctx.stroke();
    ctx.restore();
  },
};

// ── Chart export toolbar ──────────────────────────────────
// Adds a floating [📷 PNG] [📊 CSV] [↻ Reset Zoom] toolbar to any chart container.
// Usage: _addChartToolbar(wrapperEl, chart, csvFn)
function _addChartToolbar(wrapperEl, chartOrPlotlyEl, csvDataFn) {
  if (!wrapperEl) return;
  // Remove existing toolbar if re-rendering
  const old = wrapperEl.querySelector(".chart-toolbar");
  if (old) old.remove();

  const bar = document.createElement("div");
  bar.className = "chart-toolbar";
  bar.style.cssText = `position:absolute;top:4px;right:4px;display:flex;gap:4px;z-index:20;opacity:0;transition:opacity .2s`;
  wrapperEl.style.position = "relative";
  wrapperEl.addEventListener("mouseenter", () => bar.style.opacity = "1");
  wrapperEl.addEventListener("mouseleave", () => bar.style.opacity = "0");

  const btnStyle = `font-size:9px;padding:2px 6px;border-radius:4px;cursor:pointer;border:1px solid ${hexA(THEME.border,0.5)};background:${hexA(THEME.card2,0.9)};color:${THEME.muted};transition:all .15s`;
  const btnHover = (el) => {
    el.addEventListener("mouseenter", () => { el.style.color = THEME.white; el.style.borderColor = hexA(THEME.blue, 0.6); });
    el.addEventListener("mouseleave", () => { el.style.color = THEME.muted; el.style.borderColor = hexA(THEME.border, 0.5); });
  };

  // PNG export
  const pngBtn = document.createElement("button");
  pngBtn.innerHTML = "📷 PNG";
  pngBtn.style.cssText = btnStyle;
  btnHover(pngBtn);
  pngBtn.onclick = () => {
    if (chartOrPlotlyEl instanceof Chart) {
      const a = document.createElement("a");
      a.href = chartOrPlotlyEl.toBase64Image("image/png", 1);
      a.download = "chart.png";
      a.click();
    } else if (typeof Plotly !== "undefined" && chartOrPlotlyEl.nodeType) {
      Plotly.downloadImage(chartOrPlotlyEl, { format: "png", width: 1600, height: 800, filename: "chart" });
    }
  };
  bar.appendChild(pngBtn);

  // CSV export (when data function provided)
  if (csvDataFn) {
    const csvBtn = document.createElement("button");
    csvBtn.innerHTML = "📊 CSV";
    csvBtn.style.cssText = btnStyle;
    btnHover(csvBtn);
    csvBtn.onclick = () => {
      const csv = csvDataFn();
      if (!csv) return;
      const blob = new Blob([csv], { type: "text/csv" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "chart_data.csv";
      a.click();
      URL.revokeObjectURL(a.href);
    };
    bar.appendChild(csvBtn);
  }

  // Reset zoom (Chart.js only)
  if (chartOrPlotlyEl instanceof Chart) {
    const resetBtn = document.createElement("button");
    resetBtn.innerHTML = "↻ Reset";
    resetBtn.style.cssText = btnStyle;
    btnHover(resetBtn);
    resetBtn.onclick = () => chartOrPlotlyEl.resetZoom();
    bar.appendChild(resetBtn);
  }

  wrapperEl.appendChild(bar);
}

// ── Plotly threshold shapes factory ───────────────────────
// Generates horizontal threshold bands for Plotly heatmaps/charts.
// Returns shapes array for layout.shapes.
function _plotlyThresholdShapes(chartArea, thresholds) {
  // thresholds: [{ y0, y1, color, label }]  (for horizontal bands)
  // For heatmaps, thresholds are on the z-axis, so we use annotations instead
  return thresholds.map(t => ({
    type: "line",
    x0: 0, x1: 1, xref: "paper",
    y0: t.value, y1: t.value,
    line: { color: t.color, width: 1.5, dash: t.dash || "dash" },
  }));
}

// ── Plotly standard layout defaults ───────────────────────
// Consistent Plotly layout settings across all charts.
function _plotlyBaseLayout(overrides = {}) {
  return {
    paper_bgcolor: "transparent",
    plot_bgcolor: "transparent",
    font: { family: "Sora, Inter, system-ui, sans-serif", color: THEME.muted },
    margin: { l: 50, r: 30, t: 10, b: 40, ...overrides.margin },
    hoverlabel: {
      bgcolor: THEME.card2,
      bordercolor: THEME.border,
      font: { family: "JetBrains Mono, monospace", size: 11, color: THEME.white },
    },
    modebar: { bgcolor: "transparent", color: THEME.muted, activecolor: THEME.blue },
    ...overrides,
  };
}

// ── Plotly standard config ────────────────────────────────
function _plotlyConfig(opts = {}) {
  return {
    responsive: true,
    displayModeBar: true,
    displaylogo: false,
    modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d", "toggleSpikelines"],
    modeBarButtonsToAdd: opts.extraButtons || [],
    scrollZoom: true,
    ...opts,
  };
}

// ── Cross-chart time sync registry ────────────────────────
// When user zooms/pans a Plotly chart, propagate the time range to all others.
const _syncedPlotlyCharts = new Set();

/** Safely purge a Plotly chart before re-rendering (prevents GPU/memory leak). */
function _plotlyPurge(el) {
  if (!el) return;
  try {
    if (typeof Plotly !== "undefined" && el._fullLayout) Plotly.purge(el);
  } catch(e) { /* noop */ }
  _syncedPlotlyCharts.delete(el);
}

function _registerPlotlySync(plotlyEl) {
  _syncedPlotlyCharts.add(plotlyEl);
  plotlyEl.on("plotly_relayout", (evtData) => {
    const x0 = evtData["xaxis.range[0]"];
    const x1 = evtData["xaxis.range[1]"];
    if (!x0 || !x1) return;
    for (const other of _syncedPlotlyCharts) {
      if (other === plotlyEl) continue;
      try {
        Plotly.relayout(other, { "xaxis.range[0]": x0, "xaxis.range[1]": x1 });
      } catch (e) { /* ignore if chart destroyed */ }
    }
  });
}

// ── Chart.js enterprise defaults ──────────────────────────
// Consistent defaults for all Chart.js instances.
function _chartJsDefaults() {
  if (typeof Chart === "undefined") return;
  // Reduced animation durations — Firefox canvas rendering is much slower
  // than Chrome. 600ms with thousands of data points causes the
  // "slowing down your browser" warning.
  Chart.defaults.animation = {
    duration: 0,
    easing: "easeOutQuart",
  };
  Chart.defaults.transitions = {
    active: { animation: { duration: 0 } },
    resize: { animation: { duration: 0 } },
  };
  Chart.defaults.elements.point.hitRadius = 6;
  Chart.defaults.elements.point.hoverRadius = 5;
  Chart.defaults.plugins.tooltip.animation = { duration: 0 };
}
// Apply on load
if (typeof Chart !== "undefined") _chartJsDefaults();
else document.addEventListener("DOMContentLoaded", () => setTimeout(_chartJsDefaults, 500));


// ════════════════════════════════════════════════════════════
//  PHASE 3 · BATCH REVIEW
//  - Ctrl-M file upload zone
//  - POST /api/process-batch
//  - renderBatchReview(data) → KPIs + 3 Chart.js charts + table
// ════════════════════════════════════════════════════════════

const BATCH_ALLOWED = [".csv", ".xlsx", ".xls"];

function initBatchUploader() {
  const dz    = document.getElementById("batch-drop-zone");
  const input = document.getElementById("batch-file-input");
  if (!dz || !input) return;

  dz.addEventListener("click", () => input.click());

  input.addEventListener("change", (e) => {
    const files = Array.from(e.target.files || []);
    if (files.length) processBatchFiles(files);
    input.value = "";
  });

  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.add("dropzone-active");
    })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.remove("dropzone-active");
    })
  );
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    dz.classList.remove("dropzone-active");
    const dropped = Array.from(e.dataTransfer?.files || []);
    if (!dropped.length) return;
    const valid = dropped.filter(f => isBatchAllowed(f));
    const invalid = dropped.length - valid.length;
    if (invalid > 0) {
      toast("warning", "Skipped files", `${invalid} file(s) ignored — only .csv, .xlsx and .xls are accepted.`);
    }
    if (valid.length > 8) {
      toast("error", "Too many files", "Maximum 8 Ctrl-M files at a time.");
      return;
    }
    if (valid.length) processBatchFiles(valid);
  });
}

function isBatchAllowed(file) {
  const name = (file.name || "").toLowerCase();
  return BATCH_ALLOWED.some((ext) => name.endsWith(ext));
}

async function processBatchFiles(files) {
  if (!files.length) return;

  // Validate all files first
  const invalid = files.filter(f => !isBatchAllowed(f));
  if (invalid.length) {
    toast("error", "Unsupported file(s)", `${invalid.map(f=>f.name).join(", ")} — only .csv, .xlsx, .xls accepted.`);
    return;
  }
  if (files.length > 8) {
    toast("error", "Too many files", "Maximum 8 Ctrl-M files at a time.");
    return;
  }

  const names = files.map(f => f.name).join(", ");
  const totalSize = files.reduce((s, f) => s + f.size, 0);
  _renderIntakeProgress("batch-status", {
    filename: files.length === 1 ? files[0].name : `${files.length} files`,
    message:  `${formatBytes(totalSize)} \u00b7 ${names}`.slice(0, 80),
    percent:  0,
    color:    "green",
    phase:    "uploading",
  });

  const fd = new FormData();
  if (files.length === 1) {
    // Single file → use existing endpoint for backward compat
    fd.append("file", files[0], files[0].name);
  } else {
    // Multiple files → use multi endpoint
    for (const f of files) {
      fd.append("files", f, f.name);
    }
  }

  const endpoint = files.length === 1 ? "/api/process-batch" : "/api/process-batch/multi";

  try {
    const { ok, status, body } = await _uploadWithProgress(endpoint, fd, (pct, loaded, total, finished) => {
      _renderIntakeProgress("batch-status", {
        filename: files.length === 1 ? files[0].name : `${files.length} files`,
        message:  finished ? `${formatBytes(totalSize)} \u2014 parsing Ctrl-M runs\u2026`
                           : `${formatBytes(loaded)} / ${formatBytes(total)}`,
        percent:  finished ? null : pct,
        color:    "green",
        phase:    finished ? "parsing" : "uploading",
      });
    });
    const payload = body;

    if (!ok) {
      hideBatchStatus();
      toast("error", "Batch processing failed", payload?.detail || `HTTP ${status}`);
      console.error("[pe-dashboard] batch failed", payload);
      return;
    }

    window.appData.batch    = payload;
    window.appData.loadedAt = new Date().toISOString();
    window._execCache = null; // invalidate exec dashboard cache
    _markSessionActive();  // track session boundary
    // Hardwired interconnection: batch response now embeds full-dataset SLA Matrix.
    // Capture it so PE Findings + Red Flags + PE Consultant all see ALL runs.
    if (payload.sla_matrix) {
      window.appData.slaMatrix = payload.sla_matrix;
    }
    hideBatchStatus();
    renderBatchReview(payload);
    _renderBatchIntakeCard(payload);
    refreshDataStatus();

    // Trigger environment detection
    triggerEnvDetection(payload);

    // PE Narrative: fire directly now that batch + slaMatrix are set.
    // Also fires at end of triggerGenerateFindings() below — direct call
    // here ensures it runs even when findings cascade skips/fails.
    triggerPeNarrative().catch(() => {});

    // Pre-AI: auto-generate findings immediately on batch upload
    triggerGenerateFindings().catch(() => {});
    triggerRedFlags().catch(() => {});
    refreshAuditContext().catch(() => {});  // update health bar
    const _bk = payload.kpis || {};
    toast(
      "success",
      "Batch analysis complete",
      `${files.length} file(s) · ${(_bk.total_runs || 0).toLocaleString()} runs · ${_bk.total_jobs || 0} jobs · ${_n(_bk.compliance_pct).toFixed(1)}% compliant`
    );
    console.info("[pe-dashboard] window.appData.batch updated", payload);
  } catch (err) {
    hideBatchStatus();
    _handleFetchError(err, "batch");
  }
}

function showBatchStatus(msg) {
  _renderIntakeProgress("batch-status", {
    filename: "Ctrl-M batch",
    message:  msg,
    percent:  null,
    color:    "green",
    phase:    "processing",
  });
}

// ── Environment Detection ─────────────────────────────────────
async function triggerEnvDetection(batchPayload) {
  const panel = document.getElementById("env-detection-panel");
  if (!panel) return;

  // Build rows sample from batch top_jobs for env detection
  const rows = (batchPayload?.top_jobs || []).map(j => ({
    Job_Name: j.Job_Name || j.job_name || "",
    Sub_Application: j.Sub_Application || j.sub_application || "",
  }));

  // Also include filename
  const filename = batchPayload?.filename || "unknown";

  const files = [{ filename, rows }];

  // Add resource data if available
  if (window.appData.servers?.length) {
    files.push({
      filename: window.appData.resource?.filename || "resource",
      rows: window.appData.servers.slice(0, 50).map(s => ({
        host: s.host || "", server: s.server || "", source_env: s.source_env || "",
      })),
    });
  }

  try {
    const res = await fetch("/api/detect-environment", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ files }),
    });
    if (!res.ok) return;
    const data = await res.json();
    window.appData.envDetection = data;
    renderEnvDetection(data);
  } catch (e) {
    console.warn("[env-detect]", e);
  }
}

function renderEnvDetection(data) {
  const panel = document.getElementById("env-detection-panel");
  const results = document.getElementById("env-detect-results");
  const status = document.getElementById("env-detect-status");
  const summaryText = document.getElementById("env-detect-summary-text");
  if (!panel || !results) return;

  panel.classList.remove("hidden");

  const items = data.results || [];
  if (!items.length) {
    results.innerHTML = '<span class="text-Cmuted text-[10px]">No files analyzed.</span>';
    return;
  }

  const confColor = c => c >= 80 ? THEME.green : c >= 50 ? THEME.amber : THEME.red;

  // Store env data for confirmation overrides
  window._envDetectionData = data;

  // Compact chips — just env label + filename + confidence + confirm buttons
  results.innerHTML = items.map((r, i) => {
    const confirmUI = r.needs_confirmation
      ? `<span class="flex gap-0.5 ml-1">
           <button onclick="confirmEnvOverride(${i},'PROD')" class="text-[8px] font-bold px-1.5 py-0.5 rounded bg-Cgreen/15 border border-Cgreen/40 text-Cgreen hover:bg-Cgreen/25">P</button>
           <button onclick="confirmEnvOverride(${i},'TEST')" class="text-[8px] font-bold px-1.5 py-0.5 rounded bg-Cblue/15 border border-Cblue/40 text-Cblue hover:bg-Cblue/25">T</button>
           <button onclick="confirmEnvOverride(${i},'DR')" class="text-[8px] font-bold px-1.5 py-0.5 rounded bg-Cpurple/15 border border-Cpurple/40 text-Cpurple hover:bg-Cpurple/25">D</button>
         </span>`
      : "";

    return `<span class="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg border border-Cborder bg-Cbg/40" id="env-row-${i}">
      <span class="font-bold text-[10px]" style="color:${confColor(r.confidence)}">${escapeHtml(r.detected_env)}</span>
      <span class="text-[9px] text-Cmuted font-mono">${escapeHtml((r.filename || "").substring(0, 25))}</span>
      <span class="text-[9px] font-semibold" style="color:${confColor(r.confidence)}">${r.confidence}%</span>
      ${r.needs_confirmation ? '<span class="text-[8px] text-Cred font-bold">⚠</span>' : ""}
      ${confirmUI}
    </span>`;
  }).join("");

  // Summary text
  const envNames = [...new Set(items.map(r => r.detected_env))];
  const needsConfirm = items.filter(r => r.needs_confirmation).length;
  if (summaryText) {
    summaryText.textContent = `${items.length} file(s) → ${envNames.join(", ")}` +
      (needsConfirm > 0 ? ` · ${needsConfirm} need confirmation` : "");
  }

  // Show batch families
  const comp = data.comparison || {};
  const families = comp.batch_families || [];
  const famPanel = document.getElementById("env-families");
  const famList = document.getElementById("env-families-list");
  if (famPanel && families.length > 0) {
    famPanel.classList.remove("hidden");
    famList.innerHTML = families.map(f => {
      const envEntries = Object.entries(f.environments || {});
      const hasProd = envEntries.some(([env]) => env.includes("PROD"));
      const hasTest = envEntries.some(([env]) => env.includes("TEST") || env.includes("UAT"));
      const compLabel = (hasProd && hasTest)
        ? ' <span class="text-[8px] font-bold text-Cpurple bg-Cpurple/10 border border-Cpurple/30 px-1.5 py-0.5 rounded">PROD vs TEST</span>'
        : "";
      return `<span class="text-[10px]"><strong>${escapeHtml(f.base_name)}</strong>${compLabel} ` +
        envEntries.map(([env, fn]) =>
          `<span class="text-Cpurple font-semibold">${env}</span>`
        ).join(" · ") + "</span>";
    }).join(" · ");
  }

  // Update status badge
  const ambigCount = (comp.ambiguous || []).length;
  if (status) {
    if (ambigCount > 0) {
      status.textContent = `${ambigCount} AMBIGUOUS`;
      status.style.color = THEME.amber;
      status.style.borderColor = THEME.amber;
    } else {
      const envCount = Object.keys(comp.environments || {}).length;
      status.textContent = `${envCount} env(s)`;
      status.style.color = THEME.green;
      status.style.borderColor = THEME.green;
    }
  }
}

// ── Interactive environment confirmation/override ──────────────
function confirmEnvOverride(index, overrideEnv) {
  const data = window._envDetectionData;
  if (!data || !data.results || !data.results[index]) return;

  // Update the result
  data.results[index].detected_env = overrideEnv;
  data.results[index].confidence = 100;
  data.results[index].needs_confirmation = false;
  data.results[index].override_by_user = true;

  // Re-render
  renderEnvDetection(data);
  toast("success", "Environment Override", `Set to ${overrideEnv}`);
}

function hideBatchStatus() {
  document.getElementById("batch-status")?.classList.add("hidden");
}


// ─────────────────────────────────────────────────────────────
// renderBatchReview(payload) — main entrypoint after upload
// ─────────────────────────────────────────────────────────────

/** Check if a job should be excluded from the current analysis. */
function _isJobExcluded(job) {
  const name = job.Job_Name || "";
  // Manual exclude always wins
  if (_batchManualExclude.has(name)) return true;
  // Manual include overrides auto-detection
  if (_batchManualInclude.has(name)) return false;
  // Auto-detected utility excluded when master toggle is on
  return _batchExcludeUtility && !!job.is_utility;
}

/** Filter excluded jobs from batch payload.
 *  Returns a shallow copy with filtered arrays. Also masks excluded job names
 *  from the window top_job field so the Daily Batch Window tooltip is clean. */
function _filterBatchUtility(data) {
  const filteredJobs    = new Set((data.top_jobs || []).filter(j => !_isJobExcluded(j)).map(j => j.Job_Name));
  const filteredBreaches = new Set((data.top_breaches || []).filter(j => !_isJobExcluded(j)).map(j => j.Job_Name));
  // For window entries: if the top_job for that day is excluded, find the next
  // visible top job, or blank it out — so tooltips don't reference removed jobs.
  const window2 = (data.window || []).map(w => {
    if (!w.top_job || filteredJobs.has(w.top_job)) return w;  // still visible
    // top_job was excluded — try to find another job that day from top_jobs
    const dayJobs = (data.top_jobs || []).filter(j => !_isJobExcluded(j));
    return { ...w, top_job: dayJobs.length ? dayJobs[0].Job_Name : "" };
  });
  return {
    ...data,
    top_jobs:     (data.top_jobs || []).filter(j => !_isJobExcluded(j)),
    top_breaches: (data.top_breaches || []).filter(j => !_isJobExcluded(j)),
    window:       window2,
  };
}

/** Re-render batch review with current exclusion state.
 *  Also queues a findings refresh so the PE Findings page stays in sync.
 *  Busts the executive dashboard cache so next visit gets fresh analysis. */
function _reRenderBatch() {
  if (window.appData.batch) renderBatchReview(window.appData.batch);
  // Bust exec cache so excluded jobs don't bleed into executive dashboard
  window._execCache     = null;
  window._execCacheHash = null;
  // Propagate exclusion changes to PE Findings (debounced — avoids storm)
  setTimeout(() => triggerGenerateFindings().catch(() => {}), 600);
}

/**
 * Show an amber banner when BatchSLA XLSX is loaded but Batch Review KPIs
 * are still on global defaults (i.e. /api/batch/refresh not yet called).
 * Banner auto-removes when refresh succeeds.
 */
function _renderSlaStaleWarningBanner() {
  const existing = document.getElementById("sla-stale-banner");

  const batchSlaLoaded = (window.appData?.batchSlaInfo?.workflows?.length || 0) > 0;
  // Banner hides when the KPIs were actually recomputed with the XLSX contracts.
  // Accepts both "batch_sla_xlsx" (from BatchSLA upload) and "sla_matrix" (from
  // full SLA intelligence). Previously only "batch_sla_xlsx" was checked, so the
  // banner always stayed visible after a successful refresh.
  const slaAppliedType = window.appData?.batch?.sla_source?.type || "";
  const batchUsesXlsx  = slaAppliedType === "batch_sla_xlsx" || slaAppliedType === "sla_matrix";
  const refreshing     = window._batchRefreshing === true;

  // Hide if XLSX not loaded, already applied, or refresh in progress
  if (!batchSlaLoaded || batchUsesXlsx || refreshing) {
    existing?.remove();
    return;
  }
  if (existing) return; // already shown

  const banner = document.createElement("div");
  banner.id = "sla-stale-banner";
  banner.className = [
    "flex items-start gap-3 px-4 py-3 rounded-xl border",
    "border-amber-500/40 bg-amber-950/30",
    "text-amber-200 text-sm leading-snug",
    "mb-4 mt-1",
  ].join(" ");

  const srcFile = window.appData.batchSlaInfo?.source_file || "BatchSLA XLSX";
  banner.innerHTML = `
    <span class="text-amber-400 text-lg leading-none shrink-0 mt-0.5">⚠</span>
    <div class="flex-1 min-w-0">
      <p class="font-semibold text-amber-100 text-xs">
        BatchSLA XLSX loaded — Batch Review KPIs use system defaults until refreshed.
      </p>
      <p class="text-amber-300/80 text-[11px] mt-0.5">
        Window SLA compliance and gauge reflect <span class="font-bold">6h default</span>,
        not the contracted SLA from <span class="font-bold">${srcFile}</span>.
      </p>
    </div>
    <button id="sla-stale-apply-btn"
      onclick="_refreshBatchReview()"
      class="ml-2 px-3 py-1.5 rounded-lg bg-amber-500/20 hover:bg-amber-500/35
             border border-amber-500/50 text-amber-200 font-semibold
             transition-colors cursor-pointer text-xs whitespace-nowrap shrink-0">
      Apply XLSX SLA now
    </button>
    <button onclick="document.getElementById('sla-stale-banner')?.remove()"
      class="ml-1 px-2 py-1 rounded text-amber-400/60 hover:text-amber-300
             text-base leading-none shrink-0 cursor-pointer">×</button>
  `;

  // Insert before the KPI grid — first child of batch-review-body after watermark
  const batchBody = document.getElementById("batch-review-body");
  const datawarn  = document.getElementById("batch-data-warnings");
  if (datawarn?.parentNode) {
    datawarn.parentNode.insertBefore(banner, datawarn);
  } else if (batchBody) {
    batchBody.prepend(banner);
  }
}

/**
 * One-click refresh: re-runs batch metrics with current XLSX SLA ceilings.
 * Called from the amber stale banner "Apply XLSX SLA now" button.
 */
async function _refreshBatchReview() {
  if (!window.appData?.batch) return;
  window._batchRefreshing = true;

  const btn = document.getElementById("sla-stale-apply-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Refreshing…"; }

  try {
    const res = await fetch("/api/batch/refresh", { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    if (payload.error) throw new Error(payload.message || "refresh error");

    window.appData.batch = payload;
    renderBatchReview(payload);
    document.getElementById("sla-stale-banner")?.remove();
    toast("success", "Batch Review updated",
      "Window SLA compliance and gauge now reflect BatchSLA XLSX contracts.");
  } catch (e) {
    toast("warning", "Refresh failed", "Batch Review still shows default SLA values.");
    if (btn) { btn.disabled = false; btn.textContent = "Apply XLSX SLA now"; }
  } finally {
    window._batchRefreshing = false;
  }
}

/**
 * Re-fetch batch analysis from the server using cached raw data + current SLA.
 * Called after SLA matrix upload/removal so charts reflect new SLA ceilings.
 * @param {string} [toastMsg] - optional toast message on success
 */
async function _refreshBatchFromServer(toastMsg) {
  if (!window.appData.batch) return;  // nothing to refresh

  // Show a subtle "refreshing" badge on the batch tab
  const chip = document.getElementById("batch-dataset-chip");
  const _origText = chip?.textContent || "";
  if (chip) chip.textContent = "⟳ Refreshing…";

  try {
    const res = await fetch("/api/batch/refresh", { method: "POST" });
    if (!res.ok) {
      // Server restarted (session cache wiped) — batch CSV needs re-uploading.
      // Still apply SLA banner from batchSlaInfo so the user sees SLA is loaded.
      _applyBatchSlaInfoToBanner();
      const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
      const detail = err.detail || "No cached data";
      toast("warning", "Batch refresh skipped",
        detail + (detail.includes("re-upload") ? "" : " — re-upload the Ctrl-M CSV to apply SLA ceilings."));
      return;
    }
    const data = await res.json();
    window.appData.batch = data;
    renderBatchReview(data);
    // renderBatchReview calls _renderSlaStaleWarningBanner internally; that now
    // correctly hides the banner when sla_source.type is "batch_sla_xlsx" or
    // "sla_matrix". Explicit removal here as a safety net for any race.
    if (["batch_sla_xlsx", "sla_matrix"].includes(data?.sla_source?.type || "")) {
      document.getElementById("sla-stale-banner")?.remove();
    }
    // Cascade: fresh batch data invalidates exec cache and findings
    window._execCache     = null;
    window._execCacheHash = null;
    setTimeout(() => triggerGenerateFindings().catch(() => {}), 400);
    if (toastMsg) toast("success", "Charts updated", toastMsg);
  } catch (err) {
    console.warn("[pe-dashboard] batch refresh failed:", err);
  } finally {
    if (chip) chip.textContent = _origText;
  }
}

// When batch/refresh returns 404 (server restarted, session cache wiped),
// still update the SLA baseline banner from batchSlaInfo so the user can
// see the SLA XLSX IS loaded — they just need to re-upload the Ctrl-M CSV
// to get the full per-job compliance metrics recomputed.
function _applyBatchSlaInfoToBanner() {
  const bsi = window.appData?.batchSlaInfo;
  if (!bsi?.workflows?.length) return;

  // Derive the tightest daily/weekly SLA from workflows
  let dailyHrs = 6.0, weeklyHrs = 8.0;
  for (const wf of bsi.workflows) {
    const bt = (wf.batch_type || "DAILY").toUpperCase();
    const sh = parseFloat(wf.sla_hours || 0);
    if (sh > 0) {
      if (bt === "DAILY"  && sh < dailyHrs)  dailyHrs  = sh;
      if (bt === "WEEKLY" && sh < weeklyHrs) weeklyHrs = sh;
    }
  }

  // Update the baseline banner directly — show "loaded but not applied" state.
  // Avoids a falsely-green banner when KPIs haven't been recomputed yet.
  const banner      = document.getElementById("sla-baseline-banner");
  const bannerIcon  = document.getElementById("sla-baseline-icon");
  const bannerTitle = document.getElementById("sla-baseline-title");
  const bannerDetail = document.getElementById("sla-baseline-detail");
  if (banner && bannerTitle && bannerDetail && bannerIcon) {
    bannerIcon.textContent = "📋";
    bannerTitle.textContent =
      `SLA Baseline: XLSX loaded — Daily ${dailyHrs.toFixed(1)}h · Weekly ${weeklyHrs.toFixed(1)}h`;
    bannerDetail.textContent =
      `${bsi.filename || "BatchSLA_info.xlsx"} loaded (${bsi.with_sla_count || bsi.workflow_count || 0} workflows). ` +
      `Re-upload Ctrl-M CSV to apply per-job targets to compliance metrics.`;
    banner.className =
      "mt-2 px-3 py-2 rounded-lg border border-Cblue/40 bg-Cblue/5 text-Cblue text-[11px] flex items-start gap-2";
    banner.classList.remove("hidden");
  }
  // Also populate slaCeilings so findings immediately picks up the SLA context
  if (!window.appData.slaCeilings) {
    const ceilMap = {};
    for (const wf of bsi.workflows) {
      const bt = (wf.batch_type || "DAILY").toUpperCase();
      const sh = parseFloat(wf.sla_hours || 0);
      if (sh > 0 && (!ceilMap[bt] || sh > ceilMap[bt])) ceilMap[bt] = sh;
    }
    if (Object.keys(ceilMap).length > 0) window.appData.slaCeilings = ceilMap;
  }
}

function renderBatchReview(data) {
  if (!data || !data.kpis) {
    console.warn("[pe-dashboard] renderBatchReview called with empty payload");
    return;
  }

  // Apply utility job exclusion filter
  const filtered = _filterBatchUtility(data);

  // Reveal the body, hide empty state
  document.getElementById("batch-empty")?.classList.add("hidden");
  document.getElementById("batch-review-body")?.classList.remove("hidden");

  // Dataset chip
  const chip = document.getElementById("batch-dataset-chip");
  if (chip) {
    chip.textContent = `${data.filename} · ${(data.kpis?.total_runs ?? 0).toLocaleString()} runs`;
    chip.classList.remove("hidden");
  }

  // ── Utility job exclusion panel (per-job chips) ──
  const allJobs = data.top_jobs || [];
  const autoUtilJobs = allJobs.filter(j => j.is_utility);
  const excludedJobs = allJobs.filter(j => _isJobExcluded(j));
  const includedBackJobs = autoUtilJobs.filter(j => _batchManualInclude.has(j.Job_Name));

  let utilPanel = document.getElementById("batch-utility-panel");
  if (!utilPanel) {
    utilPanel = document.createElement("div");
    utilPanel.id = "batch-utility-panel";
    const _srcWm = document.getElementById("batch-source-watermark");
    const insertTarget = _srcWm?.parentElement || document.getElementById("batch-review-body");
    if (insertTarget) {
      const afterEl = _srcWm || chip;
      if (afterEl?.nextSibling) insertTarget.insertBefore(utilPanel, afterEl.nextSibling);
      else insertTarget.appendChild(utilPanel);
    }
  }

  if (excludedJobs.length > 0 || includedBackJobs.length > 0 || autoUtilJobs.length > 0) {
    // Excluded job chips — click ✕ to include back
    const exChips = excludedJobs.map(j => {
      const isAuto = !!j.is_utility && !_batchManualExclude.has(j.Job_Name);
      return `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[9px] font-mono cursor-pointer group/chip hover:opacity-80 transition"
                    style="color:${THEME.amber};background:${hexA(THEME.amber,0.12)};border:1px solid ${hexA(THEME.amber,0.25)}"
                    data-util-include="${escapeHtml(j.Job_Name)}"
                    title="Click to include this job back in the analysis">
                ${escapeHtml(j.Job_Name)}
                <span class="text-[7px] text-Cmuted">${isAuto ? "auto" : "manual"}</span>
                <span class="text-[11px] opacity-50 group-hover/chip:opacity-100">✕</span>
              </span>`;
    }).join("");

    // Included-back chips — click ↩ to re-exclude
    const inChips = includedBackJobs.map(j => {
      return `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[9px] font-mono cursor-pointer group/chip hover:opacity-80 transition"
                    style="color:${THEME.green};background:${hexA(THEME.green,0.12)};border:1px solid ${hexA(THEME.green,0.25)}"
                    data-util-reexclude="${escapeHtml(j.Job_Name)}"
                    title="Click to exclude this job again">
                ${escapeHtml(j.Job_Name)}
                <span class="text-[7px] text-Cmuted">included</span>
                <span class="text-[11px] opacity-50 group-hover/chip:opacity-100">↩</span>
              </span>`;
    }).join("");

    utilPanel.className = "rounded-lg px-3 py-2 space-y-1.5 mt-1";
    utilPanel.style.cssText = `border:1px solid ${hexA(THEME.amber, 0.25)};background:${hexA(THEME.amber, 0.04)}`;
    utilPanel.innerHTML = `
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <div class="flex items-center gap-2">
          <span class="text-[10px] font-bold uppercase tracking-wider text-Cmuted">Excluded Jobs</span>
          <span class="text-[9px] font-mono px-1.5 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.12)}"
                title="Removed from ALL batch metrics — utility/cyclic patterns (e.g. _export, housekeeping) plus any jobs you toggled off here. Distinct from 'excluded from compliance' below, where jobs stay in the data but aren't scored.">${excludedJobs.length} excluded from analysis <span class="opacity-70">· all metrics</span></span>
        </div>
        <div class="flex items-center gap-2">
          <span class="text-[8px] text-Cmuted">Click job to include/exclude · Use ⊘ in table below to exclude any job</span>
          ${(excludedJobs.length || _batchManualInclude.size || _batchManualExclude.size) ? `<button id="batch-util-reset" class="text-[8px] px-1.5 py-0.5 rounded hover:opacity-80 transition" style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.1)};border:1px solid ${hexA(THEME.cyan,0.2)}">Reset</button>` : ""}
        </div>
      </div>
      <div class="flex flex-wrap gap-1">${exChips}${inChips}</div>
    `;

    // Wire: include back
    utilPanel.querySelectorAll("[data-util-include]").forEach(el => {
      el.addEventListener("click", () => {
        const name = el.dataset.utilInclude;
        _batchManualExclude.delete(name);
        if (autoUtilJobs.some(j => j.Job_Name === name)) _batchManualInclude.add(name);
        _reRenderBatch();
      });
    });
    // Wire: re-exclude
    utilPanel.querySelectorAll("[data-util-reexclude]").forEach(el => {
      el.addEventListener("click", () => {
        _batchManualInclude.delete(el.dataset.utilReexclude);
        _reRenderBatch();
      });
    });
    // Wire: reset all
    utilPanel.querySelector("#batch-util-reset")?.addEventListener("click", () => {
      _batchManualInclude.clear();
      _batchManualExclude.clear();
      _reRenderBatch();
    });
  } else {
    utilPanel.className = "hidden";
    utilPanel.innerHTML = "";
  }

  // Data source watermark
  const srcWm = document.getElementById("batch-source-watermark");
  if (srcWm) {
    srcWm.classList.remove("hidden");
    srcWm.classList.add("flex");
    setText("batch-source-label", `Ctrl-M CSV: ${data.filename || "unknown"}`);
    const slaSrc = data.sla_source;
    const slaSrcEl = document.getElementById("batch-sla-source-label");
    if (slaSrcEl && slaSrc) {
      const slaType = slaSrc.type || "default";
      const ms = slaSrc.match_stats;
      if (_isCustomerSlaType(slaType)) {
        const matchInfo = ms?.total_jobs > 0
          ? ` · ${ms.sla_matrix}/${ms.total_jobs} jobs matched`
          : "";
        slaSrcEl.textContent = `SLA: Customer Matrix (${slaSrc.filename || "uploaded"})${matchInfo}`;
        slaSrcEl.style.borderColor = THEME.green;
        slaSrcEl.style.color = THEME.green;
      } else {
        const defHrs = (data.pe_defaults?.daily_hrs || 6).toFixed(1);
        slaSrcEl.textContent = `SLA: System Default (Daily ${defHrs}h)`;
        slaSrcEl.style.borderColor = THEME.amber;
        slaSrcEl.style.color = THEME.amber;
      }
    }
  }

  // 1. KPI cards
  // Sync the global SLA_DAILY_HRS to the pe_defaults (fresh from server) if
  // available, else from kpis. pe_defaults is always current pe_config values
  // so it never carries stale cached ceiling values.
  if (data.pe_defaults?.daily_hrs) {
    SLA_DAILY_HRS = Number(data.pe_defaults.daily_hrs) || SLA_DAILY_HRS;
  } else if (data.kpis?.sla_ceiling || data.kpis?.daily_limit_hrs) {
    // FIX 6.1: sla_ceiling is the resolved per-sub-app ceiling (more accurate than global default)
    SLA_DAILY_HRS = Number(data.kpis.sla_ceiling || data.kpis.daily_limit_hrs) || SLA_DAILY_HRS;
  }

  // ── Client-side KPI recompute from filtered job list ──────────
  // When jobs are excluded/included, recompute the fleet SLA buffer from the
  // current visible set so the doughnut gauge updates instantly — no server
  // round-trip needed.
  const _filteredJobs = filtered.top_jobs || [];
  const _displayKpis = (() => {
    if (!_filteredJobs.length) return data.kpis;
    const validBuffers = _filteredJobs.map(j => j.buffer_pct ?? 0);
    const avgBuf = validBuffers.reduce((s, v) => s + v, 0) / validBuffers.length;
    const avgBufRounded = Math.round(avgBuf * 10) / 10;
    const status = avgBuf >= 50 ? "EXCELLENT" : avgBuf >= 30 ? "HEALTHY" : avgBuf >= 15 ? "CAUTION" : "CRITICAL";
    const breachCount = validBuffers.filter(b => b < 0).length;
    const critCount = validBuffers.filter(b => b >= 0 && b < 10).length;
    return {
      ...data.kpis,
      fleet_sla_buffer: { ...((data.kpis?.fleet_sla_buffer) || {}), buffer_pct: avgBufRounded, status },
      total_breach_jobs: breachCount,
      total_critical_jobs: critCount,
    };
  })();

  renderBatchKpis(_displayKpis);
  // Story header + computed micro-narrative — reads the SAME window records the
  // charts use (filtered set) so the plain-language verdict always reconciles.
  try { renderBatchStory(filtered, _displayKpis); } catch (_) {}
  renderBatchLayerCards(data);
  renderBatchCoverageStrip(data.data_coverage || null);
  // FIX 6.2: merge data quality warnings from both coverage and kpis.dq_warnings
  const _batchWarnings = [
    ...(data.data_coverage?.warnings || []),
    ...(data.kpis?.dq_warnings      || []),
  ];
  window._lastBatchWarnings = _batchWarnings;
  renderBatchDataWarnings(_batchWarnings);
  renderExcludedJobsPanel(data.data_coverage || null);
  renderBatchSlaSourceTags(data.sla_source || null, _displayKpis);

  // 2. Charts — use filtered data for job-level views
  renderSlaBufferChart(_displayKpis);
  // Pass full unfiltered top_jobs so the chart can build the correct excludedNameSet
  // (filtered.top_jobs already has excluded jobs removed, so excludedNameSet would be
  // empty if we pass filtered — the chart needs ALL jobs to know which ones are excluded)
  renderWindowTrendChart(filtered.window || [], data.top_jobs || []);
  renderTopJobsChart(_filteredJobs, _displayKpis);
  // Long-pole consistency heatmap (top jobs × day) — complements the window chart
  try { renderLongpoleHeatmap(data.longpole_matrix || null); } catch (_) {}

  // 3. Top 10 breaching jobs table
  renderTopBreachesTable(filtered.top_breaches || [], data.kpis);

  // 4. Heatmaps (only shown when data is present)
  renderSlaHeatmap(data.sla_heatmap  || null);
  renderHourHeatmap(data.hour_heatmap || null);

  // 5. Show amber banner if BatchSLA XLSX is loaded but not yet applied to KPIs
  _renderSlaStaleWarningBanner();

  // 6. Benchmark cross-reference: show a slim callout when benchmark runtime data
  // is loaded, so the analyst knows it feeds the PE Findings and what it contains.
  _renderBatchBenchmarkXref();
}


// ── Benchmark cross-reference callout in Batch Review ─────────
/**
 * Shows a slim informational callout in the Batch Review view when
 * Batch Runtime Performance data is loaded. Makes the link between
 * the Ctrl-M operational data and the benchmark regression comparison
 * explicit — user should know the benchmark data feeds the Findings.
 *
 * Injected lazily into the batch-review-body container (idempotent).
 */
function _renderBatchBenchmarkXref() {
  const host = document.getElementById("batch-review-body");
  if (!host) return;

  const b = window.appData?.benchmarkBatch;
  const bps = b?.batch_perf_summary;

  // Remove any existing callout first (re-render on each call)
  document.getElementById("batch-bench-xref")?.remove();

  if (!bps) return;   // no benchmark data → nothing to show

  const totalJobs = bps.total_jobs || 0;
  const comp      = bps.comparable || 0;
  const regr      = bps.regressions || 0;
  const impr      = bps.improvements || 0;
  const netSecs   = bps.net_delta_secs || 0;
  const filename  = b.filename || "Batch Runtime Performance";

  const hasReg = regr > 0;
  const netDir = netSecs >= 0 ? "saved" : "added";
  const netMin = (Math.abs(netSecs) / 60).toFixed(1);

  const xref = document.createElement("div");
  xref.id = "batch-bench-xref";
  xref.className = "rounded-xl border px-4 py-3 flex items-center justify-between gap-3 flex-wrap";
  xref.style.cssText = hasReg
    ? `border-color:${hexA(THEME.amber,0.3)};background:${hexA(THEME.amber,0.04)}`
    : `border-color:${hexA(THEME.green,0.25)};background:${hexA(THEME.green,0.03)}`;

  xref.innerHTML = `
    <div class="flex items-center gap-3 min-w-0">
      <span class="shrink-0 text-base">${hasReg ? "⚠️" : "✅"}</span>
      <div class="min-w-0">
        <div class="text-[11px] font-bold uppercase tracking-wider mb-0.5"
             style="color:${hasReg ? THEME.amber : THEME.green}">
          Batch Runtime Comparison loaded
        </div>
        <div class="text-[10px] text-Cmuted font-mono truncate" title="${_esc(filename)}">
          ${_esc(filename)} ·
          ${comp} jobs compared ·
          <span style="color:${hasReg ? THEME.red : THEME.green}">${regr} regression(s)</span> ·
          ${impr} improvement(s) ·
          net ${netMin} min ${netDir}/run
        </div>
        <div class="text-[9px] text-Cmuted mt-0.5">
          This data feeds <strong class="text-Cwhite">PE Findings</strong> —
          regression analysis is separate from Ctrl-M SLA compliance above.
        </div>
      </div>
    </div>
    <button onclick="setActiveView('benchmark')"
            class="shrink-0 px-3 py-1.5 rounded-lg text-[10px] font-semibold transition-all"
            style="background:${hexA(THEME.purple,0.15)};border:1px solid ${hexA(THEME.purple,0.3)};color:${THEME.purple}">
      View Benchmark →
    </button>`;

  // Insert at the very top of the body so it's visible immediately
  host.insertBefore(xref, host.firstChild);
}


// ── SLA source annotations on charts ──────────────────────────
// A customer-sourced SLA type is any tier resolved from an uploaded customer
// file — both the BatchSLA XLSX zone ("batch_sla_xlsx") and the full SLA
// intelligence run ("sla_matrix"). Treating only "sla_matrix" as real caused
// the false "no customer SLA matrix — indicative only" banner (and amber
// "System Default" chips) to persist after a BatchSLA XLSX upload.
function _isCustomerSlaType(t) {
  return t === "sla_matrix" || t === "batch_sla_xlsx";
}

function renderBatchSlaSourceTags(sla, kpis) {
  const tag1 = document.getElementById("chart-sla-source-tag");
  const tag2 = document.getElementById("chart-window-source-tag");
  const ceiling = document.getElementById("chart-sla-ceiling-tag");

  // ── SLA Baseline Banner ──────────────────────────────────────
  const banner    = document.getElementById("sla-baseline-banner");
  const bannerIcon  = document.getElementById("sla-baseline-icon");
  const bannerTitle = document.getElementById("sla-baseline-title");
  const bannerDetail = document.getElementById("sla-baseline-detail");

  if (sla) {
    const isMatrix = _isCustomerSlaType(sla.type);
    // For the banner, always use live pe_defaults (fresh from server) in default
    // mode so a stale cached sla.daily_hrs never shows wrong values.
    const pd = window.appData?.batch?.pe_defaults || {};
    const dailyHrs  = isMatrix ? (sla.daily_hrs  || pd.daily_hrs  || 6.0) : (pd.daily_hrs  || 6.0);
    const weeklyHrs = isMatrix ? (sla.weekly_hrs || pd.weekly_hrs || 8.0) : (pd.weekly_hrs || 8.0);

    // Determine source label for small chip
    let srcLabel;
    if (isMatrix) {
      srcLabel = `From SLA Matrix (${sla.filename || "uploaded"})`;
    } else if (sla.type === "customer_fallback") {
      srcLabel = "From Customer Fallback";
    } else {
      srcLabel = "Assumed (no SLA file)";
    }

    // Build rich label with model info
    const modelTag = sla.schema_type ? ` · ${sla.schema_type.toUpperCase()} model` : "";
    const validTag = sla.valid_rows > 0 ? ` · ${sla.valid_rows} rules` : "";
    const matchStats = sla.match_stats;
    const matchTag = matchStats?.total_jobs > 0
      ? ` · ${matchStats.sla_matrix}/${matchStats.total_jobs} matched`
        + (matchStats.assumed > 0 ? ` (${matchStats.assumed} assumed)` : "")
      : "";
    const label = `SLA: ${srcLabel}${modelTag}${validTag}${matchTag} · Daily ${Number(dailyHrs).toFixed(1)}h`;

    if (tag1) { tag1.textContent = label; tag1.classList.remove("hidden"); }
    if (tag2) { tag2.textContent = label; tag2.classList.remove("hidden"); }
    if (ceiling) { ceiling.textContent = `${Number(dailyHrs).toFixed(1)} h`; }

    if (sla.blocked) {
      if (tag1) tag1.style.color = THEME.red;
      if (tag2) tag2.style.color = THEME.red;
    } else if (isMatrix) {
      if (tag1) tag1.style.color = THEME.green;
      if (tag2) tag2.style.color = THEME.green;
    }

    // Surface SLA warnings inline
    const warnEl = document.getElementById("chart-sla-warnings");
    if (warnEl && sla.warnings?.length) {
      warnEl.innerHTML = sla.warnings.slice(0, 3).map(w =>
        `<span class="text-[9px] px-1.5 py-0.5 rounded border ${w.severity === "critical" ? "text-Cred border-Cred/30" : "text-Camber border-Camber/30"}">${escapeHtml(w.text || "").substring(0, 80)}</span>`
      ).join(" ");
      warnEl.classList.remove("hidden");
    }

    // ── Render SLA Baseline Banner ───────────────────────────
    if (banner && bannerTitle && bannerDetail && bannerIcon) {
      banner.classList.remove("hidden");
      if (isMatrix) {
        // Green: real matrix loaded
        const ms = matchStats || {};
        const matchLine = ms.total_jobs > 0
          ? `${ms.sla_matrix || 0}/${ms.total_jobs} jobs matched to contract · ${ms.assumed || 0} using schedule defaults`
          : "";
        const schemaLine = sla.schema_type ? `${sla.schema_type.toUpperCase()} model` : "Job-specific model";
        const details = [schemaLine, matchLine].filter(Boolean).join("  ·  ");
        // Honest window summary: a customer matrix usually has several distinct
        // contracted windows. Show the actual resolved range so the banner never
        // asserts a single "Daily Xh" that contradicts the per-job table.
        const rc = sla.resolved_ceilings;
        let windowLine;
        if (Array.isArray(rc) && rc.length > 1) {
          windowLine = `${rc.length} distinct contract windows ${Number(sla.resolved_ceiling_min).toFixed(1)}–${Number(sla.resolved_ceiling_max).toFixed(1)}h across ${sla.resolved_workflow_count || rc.length} workflows`;
        } else if (Array.isArray(rc) && rc.length === 1) {
          windowLine = `Contract window ${Number(rc[0]).toFixed(1)}h`;
        } else {
          windowLine = `Daily ${Number(dailyHrs).toFixed(1)}h · Weekly ${Number(weeklyHrs).toFixed(1)}h`;
        }
        bannerIcon.textContent = "✅";
        bannerTitle.textContent = `SLA Baseline: Customer Matrix — ${sla.filename || "uploaded"}`;
        bannerDetail.textContent = `Per-job SLA contracts active. ${windowLine}${details ? "  ·  " + details : ""}`;
        banner.className = "mt-2 px-3 py-2 rounded-lg border border-Cgreen/40 bg-Cgreen/5 text-Cgreen text-[11px] flex items-start gap-2";
      } else {
        // Amber: defaults only
        bannerIcon.textContent = "📐";
        bannerTitle.textContent = `SLA Baseline: System Defaults (Daily ${Number(dailyHrs).toFixed(1)}h · Weekly ${Number(weeklyHrs).toFixed(1)}h)`;
        bannerDetail.textContent = "No customer SLA matrix uploaded. Compliance findings use PE assumed defaults — indicative only. Upload BatchSLA_info.xlsx to load per-job contract targets.";
        banner.className = "mt-2 px-3 py-2 rounded-lg border border-Camber/40 bg-Camber/5 text-Camber text-[11px] flex items-start gap-2";
      }
    }
  } else {
    // No sla object — show defaults from pe_defaults or kpis
    const pd = window.appData?.batch?.pe_defaults || {};
    const dailyHrs = pd.daily_hrs || kpis?.daily_limit_hrs || 6.0;
    const weeklyHrs = pd.weekly_hrs || 8.0;
    if (ceiling) { ceiling.textContent = `${Number(dailyHrs).toFixed(1)} h`; }
    if (banner && bannerTitle && bannerDetail && bannerIcon) {
      banner.classList.remove("hidden");
      bannerIcon.textContent = "📐";
      bannerTitle.textContent = `SLA Baseline: System Defaults (Daily ${Number(dailyHrs).toFixed(1)}h · Weekly ${Number(weeklyHrs).toFixed(1)}h)`;
      bannerDetail.textContent = "No customer SLA matrix uploaded. Upload BatchSLA_info.xlsx to activate per-job SLA contracts.";
      banner.className = "mt-2 px-3 py-2 rounded-lg border border-Camber/40 bg-Camber/5 text-Camber text-[11px] flex items-start gap-2";
    }
  }
}


// ─────────────────────────────────────────────────────────────
// KPI cards
// ─────────────────────────────────────────────────────────────
// ── KPI Drill-Through panel toggle ───────────────────────────
function _toggleKpiDrill(id) {
  const panel = document.getElementById(id);
  if (!panel) return;
  const wasHidden = panel.classList.contains("hidden");
  // Close all drills first
  ["window-sla-drill", "failed-runs-drill"].forEach(pid => {
    const el = document.getElementById(pid);
    if (el) el.classList.add("hidden");
  });
  if (wasHidden) {
    panel.classList.remove("hidden");
    if (id === "window-sla-drill") _populateWindowSlaDrill();
    if (id === "failed-runs-drill") _populateFailedRunsDrill();
  }
}

function _populateWindowSlaDrill() {
  const body = document.getElementById("window-sla-drill-body");
  if (!body) return;
  const windowData = (window.appData?.batch?.window || []);
  const breachDays = windowData.filter(w => w.breach);
  const allDays    = windowData;

  if (!allDays.length) {
    body.innerHTML = `<p class="text-[11px] text-Cmuted italic">No window data — requires Start_Time and End_Time columns in batch CSV.</p>`;
    return;
  }

  const rows = allDays.map(w => {
    const breach = w.breach;
    const diff   = w.total_hrs - (w.sla_limit || 0);
    return `<tr class="border-b border-Cborder/20 ${breach ? "bg-Cred/5" : ""}">
      <td class="py-1.5 px-3 text-[11px] font-mono ${breach ? "text-Cred font-semibold" : "text-Cwhite"}">${_esc(w.run_date || "?")}</td>
      <td class="py-1.5 px-3 text-right font-mono text-[11px] ${breach ? "text-Cred font-bold" : "text-Camber"}">${_n(w.total_hrs).toFixed(2)}h</td>
      <td class="py-1.5 px-3 text-right font-mono text-[11px] text-Cmuted">${_n(w.sla_limit || 0).toFixed(2)}h</td>
      <td class="py-1.5 px-3 text-right font-mono text-[11px] ${breach ? "text-Cred" : "text-Cgreen"}">${breach ? "+" + diff.toFixed(2) + "h" : "within SLA"}</td>
      <td class="py-1.5 px-3 text-[10px] text-Cmuted">${_n(w.job_count || 0)} jobs</td>
      <td class="py-1.5 px-3 text-[10px] text-Cteal">${_esc(w.top_job || "—")}</td>
      <td class="py-1.5 px-3 text-center text-[10px]">${breach ? '<span class="px-1.5 py-0.5 rounded bg-Cred/20 text-Cred font-bold text-[9px]">BREACH</span>' : '<span class="px-1.5 py-0.5 rounded bg-Cgreen/20 text-Cgreen font-bold text-[9px]">PASS</span>'}</td>
    </tr>`;
  }).join("");

  body.innerHTML = `
    <div class="text-[10px] text-Camber mb-2 font-semibold">
      ${breachDays.length} / ${allDays.length} days breached the batch window SLA.
      ${breachDays.length > 0 ? "Window compliance failure ≠ job-level SLA breach — a day can breach the window without any single job exceeding its SLA (cumulative load effect)." : "All days passed the batch window SLA."}
    </div>
    <table class="w-full text-left border-collapse text-[11px]">
      <thead><tr class="border-b border-Cborder/40 bg-Cbg/60">
        <th class="py-1.5 px-3 text-Camber font-semibold text-[10px]">Date</th>
        <th class="py-1.5 px-3 text-Camber font-semibold text-[10px] text-right">Elapsed</th>
        <th class="py-1.5 px-3 text-Camber font-semibold text-[10px] text-right">SLA</th>
        <th class="py-1.5 px-3 text-Camber font-semibold text-[10px] text-right">Over/Under</th>
        <th class="py-1.5 px-3 text-Camber font-semibold text-[10px]">Jobs</th>
        <th class="py-1.5 px-3 text-Camber font-semibold text-[10px]">Top Job</th>
        <th class="py-1.5 px-3 text-Camber font-semibold text-[10px] text-center">Status</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function _populateFailedRunsDrill() {
  const body = document.getElementById("failed-runs-drill-body");
  if (!body) return;
  const topJobs  = window.appData?.batch?.top_jobs  || [];
  const failJobs = topJobs.filter(j => (j.fail_count || 0) > 0)
                          .sort((a, b) => (b.fail_count || 0) - (a.fail_count || 0));

  if (!failJobs.length) {
    // Check if we have fail_runs count in KPIs at all
    const failKpi = Number(window.appData?.batch?.kpis?.fail_runs || 0);
    if (failKpi > 0) {
      body.innerHTML = `<p class="text-[11px] text-Camber italic">${failKpi} execution failure(s) recorded but per-job breakdown not available. Check your batch CSV includes a Status column with ENDED OK / ENDED NOT OK values.</p>`;
    } else {
      body.innerHTML = `<p class="text-[11px] text-Cgreen italic">✅ No execution failures recorded — all runs ENDED OK.</p>`;
    }
    return;
  }

  const rows = failJobs.map(j => {
    const jn = j.Job_Name || j.job_name || "?";
    const fc = j.fail_count || 0;
    const peak = _n(j.peak_hrs || 0);
    const sub  = j.Sub_Application || j.sub_application || "—";
    return `<tr class="border-b border-Cborder/20 hover:bg-Ccard/30">
      <td class="py-1.5 px-3 text-[11px] font-mono text-Cwhite font-semibold">${_esc(jn)}</td>
      <td class="py-1.5 px-3 text-[10px] text-Cmuted">${_esc(sub)}</td>
      <td class="py-1.5 px-3 text-right text-[13px] font-bold text-Corange">${fc}</td>
      <td class="py-1.5 px-3 text-right text-[11px] text-Camber font-mono">${peak.toFixed(3)}h</td>
      <td class="py-1.5 px-3 text-center">
        <span class="px-1.5 py-0.5 rounded bg-Corange/20 text-Corange font-bold text-[9px]">FAILED ×${fc}</span>
      </td>
    </tr>`;
  }).join("");

  const totalFails = failJobs.reduce((s, j) => s + (j.fail_count || 0), 0);
  body.innerHTML = `
    <div class="text-[10px] text-Corange mb-2 font-semibold">${totalFails} failure(s) across ${failJobs.length} job(s). Execution failures are separate from SLA breaches — investigate Ctrl-M job logs for root cause.</div>
    <table class="w-full text-left border-collapse text-[11px]">
      <thead><tr class="border-b border-Cborder/40 bg-Cbg/60">
        <th class="py-1.5 px-3 text-Corange font-semibold text-[10px]">Job Name</th>
        <th class="py-1.5 px-3 text-Corange font-semibold text-[10px]">Sub-App</th>
        <th class="py-1.5 px-3 text-Corange font-semibold text-[10px] text-right">Failures</th>
        <th class="py-1.5 px-3 text-Corange font-semibold text-[10px] text-right">Peak (h)</th>
        <th class="py-1.5 px-3 text-Corange font-semibold text-[10px] text-center">Status</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderBatchKpis(k) {
  // ── FIX 6.3: ENV chip — show TEST or PROD badge ──────────────────────────
  const _env     = (k?.batch_env || k?.env_type || "").toUpperCase();
  const _envChip = document.getElementById("batch-env-chip");
  if (_envChip) {
    if (_env === "TEST" || _env === "UAT") {
      _envChip.textContent = _env;
      _envChip.className   = "px-2 py-0.5 rounded-full text-[10px] font-bold uppercase "
                           + "tracking-wider bg-amber-500/20 border border-amber-500/40 "
                           + "text-amber-300 ml-2";
      _envChip.title       = "This data is from a TEST/UAT environment — not production";
      _envChip.classList.remove("hidden");
    } else if (_env === "PROD" || _env === "PRODUCTION") {
      _envChip.textContent = "PROD";
      _envChip.className   = "px-2 py-0.5 rounded-full text-[10px] font-bold uppercase "
                           + "tracking-wider bg-green-500/20 border border-green-500/40 "
                           + "text-green-300 ml-2";
      _envChip.title       = "";
      _envChip.classList.remove("hidden");
    } else {
      _envChip.classList.add("hidden");
    }
  }
  // ── end ENV chip ──────────────────────────────────────────────────────────

  // Job SLA Compliance (individual job peaks vs SLA)
  const compEl = document.getElementById("bk-compliance");
  if (compEl) {
    const jsc = _n(k.job_sla_compliance ?? k.compliance_pct);
    compEl.textContent = `${jsc.toFixed(1)}%`;
    compEl.style.color =
      jsc >= 95 ? THEME.green :
      jsc >= 80 ? THEME.amber : THEME.red;
  }

  // Show both compliance types in subtitle
  // Day-level window compliance is the canonical headline (matches Executive
  // Dashboard, PE Findings and the SLA Matrix tab) and reconciles exactly with
  // the breach-day count below. Falls back to pair-level batch_window_compliance.
  const bwc = (k.window_day_compliance_pct != null)
    ? k.window_day_compliance_pct
    : k.batch_window_compliance;
  const wbd = k.window_breach_days || 0;
  const wtd = k.window_total_days || 0;
  let compSub = `${k.jobs_ok} OK · ${k.total_jobs} total`;
  if (bwc != null) {
    compSub += ` · Window: ${_n(bwc).toFixed(0)}%`;
    if (wbd > 0) compSub += ` (${wbd}/${wtd}d breached)`;
  }
  setText("bk-compliance-sub", compSub);

  // Window SLA Rate — prominently displayed as its own KPI
  const winCompEl = document.getElementById("bk-window-compliance");
  if (winCompEl && bwc != null) {
    winCompEl.textContent = `${_n(bwc).toFixed(1)}%`;
    winCompEl.style.color =
      bwc >= 95 ? THEME.green :
      bwc >= 80 ? THEME.amber : THEME.red;
    // Tooltip: day-level headline formula, with pair-level as a secondary line
    const wPairs  = k.window_total_pairs;
    const wBPairs = k.window_breach_pairs || 0;
    const passDt  = wtd - wbd;
    let _wtip = `${passDt}/${wtd} calendar days ALL sub-apps finished within SLA · formula: clean_days ÷ total_days × 100`;
    if (wPairs) {
      _wtip += `\nPair detail: ${wPairs - wBPairs}/${wPairs} (sub-app × day) windows within SLA`;
    }
    winCompEl.title = _wtip;
  }
  const winSubEl = document.getElementById("bk-window-compliance-sub");
  if (winSubEl) {
    if (bwc != null) {
      const wPairs  = k.window_total_pairs;
      const wBPairs = k.window_breach_pairs || 0;
      const passD   = wtd - wbd;
      if (wPairs) {
        const okPairs = wPairs - wBPairs;
        winSubEl.textContent = `${passD}/${wtd} days all-pass · ${okPairs}/${wPairs} windows`;
      } else {
        winSubEl.textContent = `${passD}/${wtd} days pass · ${wbd} breach`;
      }
    } else {
      winSubEl.textContent = "Needs End_Time column";
    }
  }

  setText("bk-breach", String(k.jobs_breach));
  setText("bk-atrisk", String(k.jobs_at_risk));
  setText("bk-ok",     String(k.jobs_ok));
  setText(
    "bk-breach-sub",
    `SLA ${_n(k.daily_limit_hrs, 6).toFixed(1)}h`
  );

  // Failed Runs (execution failures — ENDED NOT OK / ABENDED / TERMINATED).
  // Separate signal from SLA breach: a run can be FAILED without breaching
  // SLA, and a run can breach SLA while ending OK.
  const failed   = Number(k.failed_runs || 0);
  const failRate = Number(k.fail_rate_pct || 0);
  const totalRuns= Number(k.total_runs   || 0);
  const okRuns   = Number(k.ok_runs      || Math.max(0, totalRuns - failed));
  setText("bk-failed", String(failed));
  setText(
    "bk-failed-sub",
    totalRuns
      ? `${failRate.toFixed(2)}% of ${totalRuns} runs · ${okRuns} OK`
      : "no run data",
  );
}


// ── Separated analysis layer cards ────────────────────────────
function renderBatchLayerCards(data) {
  const ew  = data.elapsed_window || {};
  const sr  = data.summed_runtime || {};
  const wj  = data.worst_job || {};
  const sla = data.sla_source || {};

  // ── Effective Window (SLA-binding: longest contiguous block) ──
  // The headline batch-window number is the EFFECTIVE window (longest contiguous
  // run), NOT the first→last elapsed span (mostly idle gaps on spread batches).
  // Breach is judged on this measure, so the KPI must show it to reconcile with
  // the Window SLA tile + Breach Calendar. Elapsed span is kept as context.
  const ewEl = document.getElementById("bk-elapsed");
  if (ewEl) {
    const winArr = Array.isArray(data.window) ? data.window : [];
    const effRows = winArr.map(w => ({
      d: w.run_date,
      eff: Number(w.effective_hrs ?? w.elapsed_hrs ?? w.total_hrs ?? 0),
      span: Number(w.elapsed_hrs ?? 0),
      breach: !!w.breach,
    })).filter(r => r.d && r.eff > 0);
    if (effRows.length) {
      const worst  = effRows.slice().sort((a, b) => b.eff - a.eff)[0];
      const avgEff = effRows.reduce((s, r) => s + r.eff, 0) / effRows.length;
      ewEl.textContent = `${worst.eff.toFixed(1)}h`;
      // Colour by the canonical breach flag of the worst-effective day — NOT a
      // raw hrs>ceiling compare (which would ignore each sub-app's own ceiling).
      ewEl.style.color = worst.breach ? THEME.red : THEME.purple;
      const spanTxt = worst.span > worst.eff + 0.05 ? ` · span ${worst.span.toFixed(1)}h` : "";
      setText("bk-elapsed-sub", `Worst day: ${worst.d} · Avg ${avgEff.toFixed(1)}h${spanTxt}`);
    } else if (ew.available && ew.worst_day) {
      ewEl.textContent = `${_n(ew.worst_day.elapsed_hrs).toFixed(1)}h`;
      ewEl.style.color = THEME.purple;
      setText("bk-elapsed-sub", `Worst day: ${ew.worst_day.run_date} · Avg ${(ew.avg_elapsed_hrs || 0).toFixed(1)}h`);
    } else {
      ewEl.textContent = "N/A";
      ewEl.style.color = THEME.muted;
      setText("bk-elapsed-sub", "End_Time missing — cannot compute");
    }
  }

  // ── Summed Runtime ──
  const srEl = document.getElementById("bk-summed");
  if (srEl) {
    srEl.textContent = `${sr.total_hrs?.toFixed(1) || "0"}h`;
    setText("bk-summed-sub", `Worst day ${sr.worst_day_hrs?.toFixed(1) || "0"}h · Avg ${sr.avg_day_hrs?.toFixed(1) || "0"}h/day`);
  }

  // ── Worst-Job Peak ──
  const wjEl = document.getElementById("bk-worst");
  if (wjEl) {
    if (wj.peak_hrs > 0) {
      wjEl.textContent = `${_n(wj.peak_hrs).toFixed(2)}h`;
      wjEl.style.color = _n(wj.buffer_pct) < 0 ? THEME.red : _n(wj.buffer_pct) < 15 ? THEME.amber : THEME.green;
      const jobName = (wj.job_name || "?").length > 25 ? wj.job_name.substring(0, 22) + "…" : wj.job_name;
      setText("bk-worst-sub", `${jobName} · ${_n(wj.buffer_pct).toFixed(0)}% buffer`);
    } else {
      wjEl.textContent = "—";
      setText("bk-worst-sub", "No runtime data");
    }
  }

  // ── SLA Source ──
  const slaEl = document.getElementById("bk-sla-source");
  if (slaEl) {
    const isMatrix    = sla.type === "sla_matrix";
    const isBatchXlsx = sla.type === "batch_sla_xlsx";
    const isSow       = sla.type === "sow_extracted";
    const isAssumed   = !sla.type || sla.type === "default" || sla.type === "assumed";
    const isFallback  = sla.type === "customer_fallback";

    // Also check appData for active Tier 1/2 to show the correct tier
    const hasTier1 = (window.appData?.batchSlaInfo?.workflows?.length || 0) > 0;
    const hasTier2 = Object.keys(window.appData?.sowContract?.sla_windows || {}).length > 0;

    if (isMatrix || isBatchXlsx) {
      slaEl.textContent = "Tier 1 — BatchSLA XLSX";
      slaEl.style.color = THEME.green;
      const _rc = sla.resolved_ceilings;
      const _win = (Array.isArray(_rc) && _rc.length > 1)
        ? `${_rc.length} windows ${Number(sla.resolved_ceiling_min).toFixed(1)}–${Number(sla.resolved_ceiling_max).toFixed(1)}h`
        : (Array.isArray(_rc) && _rc.length === 1)
          ? `Window ${Number(_rc[0]).toFixed(1)}h`
          : `Daily ${sla.daily_hrs?.toFixed(1) || "?"}h · Weekly ${sla.weekly_hrs?.toFixed(1) || "?"}h`;
      setText("bk-sla-source-sub", `${_win} · High-confidence`);
    } else if (isSow) {
      slaEl.textContent = "Tier 2 — SOW Contract";
      slaEl.style.color = THEME.cyan;
      setText("bk-sla-source-sub",
        `Daily ${sla.daily_hrs?.toFixed(1) || "?"}h · Weekly ${sla.weekly_hrs?.toFixed(1) || "?"}h · From SOW PDF`
      );
    } else if (isFallback) {
      // customer_fallback = schedule-type ceiling from old SLA file
      // After the wiring fix this should only win when no Tier 1/2 loaded
      const tierHint = hasTier1 ? "Tier 1 active (rerun SLA matrix)" :
                       hasTier2 ? "Tier 2 active (rerun SLA matrix)" :
                       "Upload BatchSLA XLSX to activate Tier 1";
      slaEl.textContent = "Tier 3 — SLA File Ceiling";
      slaEl.style.color = THEME.amber;
      setText("bk-sla-source-sub",
        `Daily ${sla.daily_hrs?.toFixed(1) || "5.0"}h · Schedule-type fallback · ${tierHint}`
      );
    } else if (isAssumed) {
      const tierHint = hasTier1 ? "Tier 1 loaded — rerun SLA Matrix to activate" :
                       hasTier2 ? "Tier 2 loaded — rerun SLA Matrix to activate" :
                       "Upload BatchSLA XLSX or SOW to improve accuracy";
      slaEl.textContent = sla.blocked ? "BLOCKED" : "Tier 3 — Global Default";
      slaEl.style.color = sla.blocked ? THEME.red : THEME.amber;
      setText("bk-sla-source-sub",
        sla.blocked
          ? "Cannot produce green compliance — upload BatchSLA XLSX"
          : `Daily ${sla.daily_hrs?.toFixed(1) || "6.0"}h · ${tierHint}`
      );
    } else {
      slaEl.textContent = "From SLA Matrix";
      slaEl.style.color = THEME.green;
      const model = sla.detected_model || sla.schema_type || "";
      setText("bk-sla-source-sub",
        `${model ? model + " · " : ""}Daily ${sla.daily_hrs?.toFixed(1) || "6.0"}h · Weekly ${sla.weekly_hrs?.toFixed(1) || "8.0"}h`
      );
    }
  }
}


// ── PE Audit Coverage Strip ───────────────────────────────────
function renderBatchCoverageStrip(dc) {
  const strip = document.getElementById("batch-coverage-strip");
  if (!strip) return;
  if (!dc) { strip.classList.add("hidden"); return; }

  strip.classList.remove("hidden");

  const badge = (id, label, status) => {
    const el = document.getElementById(id);
    if (!el) return;
    const colors = {
      loaded:  { bg: THEME.green, border: THEME.green, text: THEME.green },
      partial: { bg: THEME.amber, border: THEME.amber, text: THEME.amber },
      customer:{ bg: THEME.green, border: THEME.green, text: THEME.green },
      default: { bg: THEME.amber, border: THEME.amber, text: THEME.amber },
      missing: { bg: THEME.muted, border: THEME.muted, text: THEME.muted },
    };
    const c = colors[status] || colors.missing;
    el.textContent = `${label}: ${status.toUpperCase()}`;
    el.style.color = c.text;
    el.style.borderColor = hexA(c.border, 0.4);
    el.style.background = hexA(c.bg, 0.1);
  };

  const span = dc.date_span_days || 0;
  badge("cov-30day", "15-Day Evidence",
    span >= 15 ? "loaded" : span >= 7 ? "partial" : "missing");

  // SLA source quality from sla_source metadata
  const batchSla = (window.appData.batch || {}).sla_source || {};
  const slaStatus = _isCustomerSlaType(batchSla.type) ? "customer" :
                    batchSla.type === "customer_fallback" ? "partial" : "default";
  badge("cov-sla", "SLA Source", slaStatus);

  badge("cov-confidence", `Confidence ${dc.confidence || 0}%`,
    dc.confidence >= 80 ? "loaded" : dc.confidence >= 60 ? "partial" : "missing");

  // Data integrity: flag synthetic timestamps prominently
  if (dc.has_synthetic_timestamps) {
    badge("cov-confidence", "⛔ SYNTHETIC TIMESTAMPS", "missing");
  }

  badge("cov-waivers", "Waivers", "missing");
  const hasSow = !!(window.appData.sowCompare || window.appData.sow);
  badge("cov-sow", "Volume vs SOW", hasSow ? "loaded" : "missing");
}


// ── Data Warnings ─────────────────────────────────────────────
// Warnings with identical `code` (e.g. UTILITY_PATTERN_NOT_EXCLUDED) are
// collapsed into a single summary row with expand/collapse — prevents 40+
// near-identical info rows from flooding the Batch Review panel.
function renderBatchDataWarnings(warnings) {
  const wrap = document.getElementById("batch-data-warnings");
  if (!wrap) return;
  if (!warnings || !warnings.length) { wrap.classList.add("hidden"); return; }

  wrap.classList.remove("hidden");

  const sevOrder = { critical: 0, warning: 1, info: 2 };
  const sorted = [...warnings].sort((a, b) =>
    (sevOrder[a.severity] ?? 3) - (sevOrder[b.severity] ?? 3)
  );

  // Group by code — critical/warning codes always render individually;
  // info codes with >3 members collapse into a summary row.
  const GROUP_THRESHOLD = 3;
  const groups = new Map(); // code → [warnings]
  sorted.forEach(w => {
    const key = w.code || w.severity || "other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(w);
  });

  // Expand state — persists across re-renders
  if (!window._batchWarnExpanded) window._batchWarnExpanded = {};

  const rows = [];
  groups.forEach((members, code) => {
    const first = members[0];
    const isCritical = first.severity === "critical";
    const isWarning  = first.severity === "warning";
    const sev  = isCritical ? THEME.red : isWarning ? THEME.amber : THEME.cyan;
    const icon = isCritical ? "🚨" : isWarning ? "⚠️" : "ℹ️";
    const bgOp = isCritical ? 0.10 : 0.06;

    // Always render individually for critical/warning, or small groups
    if (isCritical || isWarning || members.length <= GROUP_THRESHOLD) {
      members.forEach(w => {
        rows.push(`<div class="rounded-lg px-3 py-2 text-[11px]${isCritical ? " animate-pulse font-semibold" : ""}"
          style="border-left:${isCritical ? 3 : 2}px solid ${sev};background:${hexA(sev, bgOp)}">
          <span style="color:${sev}" class="font-bold">${icon}</span>
          <span class="${isCritical ? "text-red-300" : "text-Cmuted"} ml-1">${escapeHtml(w.text)}</span>
        </div>`);
      });
      return;
    }

    // Collapsed group for repetitive info codes (UTILITY_PATTERN_NOT_EXCLUDED etc.)
    const expanded = !!window._batchWarnExpanded[code];
    // Derive a clean group label: strip job-specific parts, keep the pattern
    const groupLabel = (() => {
      // Most utility warnings say "Job 'X' matches utility pattern '_export'..."
      // Extract the common part after "matches utility pattern"
      const m = first.text.match(/matches utility pattern '([^']+)'/);
      if (m) return `${members.length} jobs match utility pattern '${m[1]}' — runtime exceeds threshold, treated as real batch`;
      // Fallback: common prefix
      let prefix = first.text;
      for (const w of members) {
        while (prefix && !w.text.startsWith(prefix)) prefix = prefix.slice(0, -1);
      }
      return (prefix.trim().replace(/[:·,\-]+$/, "") || code) + ` (${members.length} jobs)`;
    })();

    rows.push(`<div class="rounded-lg text-[11px]"
        style="border-left:2px solid ${sev};background:${hexA(sev, bgOp)}">
      <div class="flex items-center justify-between gap-2 px-3 py-2 cursor-pointer hover:opacity-90 transition"
           onclick="window._toggleBatchWarnGroup('${escapeHtml(code)}')">
        <div class="flex items-center gap-2 min-w-0">
          <span style="color:${sev}" class="font-bold shrink-0">${icon}</span>
          <span class="text-Cmuted truncate">${escapeHtml(groupLabel)}</span>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <span class="text-[9px] font-mono px-1.5 py-0.5 rounded"
                style="color:${sev};background:${hexA(sev,0.15)};border:1px solid ${hexA(sev,0.3)}">${members.length} jobs</span>
          <span class="text-[10px] text-Cmuted">${expanded ? "▲" : "▼"}</span>
        </div>
      </div>
      ${expanded ? `<div class="px-3 pb-2 space-y-1 border-t" style="border-color:${hexA(sev,0.15)}">
        ${members.map(w => `<div class="text-[10px] text-Cmuted py-0.5 font-mono">${escapeHtml(w.text)}</div>`).join("")}
      </div>` : ""}
    </div>`);
  });

  wrap.innerHTML = `<div class="space-y-1">${rows.join("")}</div>`;
}

/** Toggle expand/collapse of a batch warning group. */
window._toggleBatchWarnGroup = function(code) {
  if (!window._batchWarnExpanded) window._batchWarnExpanded = {};
  window._batchWarnExpanded[code] = !window._batchWarnExpanded[code];
  // Re-render using the cached warnings
  const cached = window._lastBatchWarnings;
  if (cached) renderBatchDataWarnings(cached);
};


// ── Excluded Jobs Panel (SHORT_JOB / INSUFFICIENT / manual excludes) ──────
function renderExcludedJobsPanel(dataCoverage) {
  // Inject panel adjacent to batch-data-warnings if not already present
  const refEl = document.getElementById("batch-data-warnings");
  if (!refEl) return;

  let panel = document.getElementById("batch-excluded-jobs-panel");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "batch-excluded-jobs-panel";
    refEl.insertAdjacentElement("afterend", panel);
  }

  const excluded = dataCoverage?.excluded_jobs || [];
  if (!excluded.length) {
    panel.innerHTML = "";
    panel.classList.add("hidden");
    return;
  }

  panel.classList.remove("hidden");
  const rows = excluded.slice(0, 20).map(j => {
    const reason  = (j.reason || "EXCLUDED").toUpperCase();
    const badgeColor = reason === "SHORT_JOB"
      ? THEME.cyan  : reason === "INSUFFICIENT"
      ? THEME.muted : reason === "CYCLIC"
      ? THEME.amber : THEME.blue;
    return `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono mr-1 mb-1"
                  style="background:${hexA(badgeColor,0.10)};border:1px solid ${hexA(badgeColor,0.25)};color:${badgeColor}">
      ${escapeHtml(j.job_name || j.name || "?")}
      <span class="text-[9px] opacity-70">${reason}</span>
    </span>`;
  }).join("");

  panel.innerHTML = `<div class="rounded-lg border border-Cborder/20 px-3 py-2 mt-1 text-[11px]"
                         style="background:${hexA(THEME.cyan,0.04)}">
    <span class="text-Cmuted font-semibold mr-2"
          title="Kept in the dataset and the run tables, but NOT scored in the SLA compliance % — too few runs (INSUFFICIENT, <3), zero/near-zero duration (SHORT_JOB), or cyclic with no SLA baseline. Distinct from 'excluded from analysis', which removes jobs from every metric.">⊘ ${excluded.length} job(s) excluded from compliance <span class="font-normal opacity-70">· kept in data, not scored</span>:</span>
    ${rows}
    ${excluded.length > 20 ? `<span class="text-Cmuted text-[10px]">…and ${excluded.length - 20} more</span>` : ""}
  </div>`;
}



// ─────────────────────────────────────────────────────────────
// Batch story header + computed micro-narrative
// Answers "Are we meeting batch SLAs?" in plain language from the SAME
// per-day window data the charts read — so the narrative can never disagree
// with the bars/gauge. Buffer bands use the shared SLA_ATRISK_PCT/LONGJOB_PCT.
// ─────────────────────────────────────────────────────────────

/** Format hours compactly: 6 → "6h", 6.25 → "6.2h". */
function _fmtHrs(h) {
  const n = Number(h) || 0;
  return (Math.abs(n % 1) < 0.05 ? n.toFixed(0) : n.toFixed(1)) + "h";
}

/**
 * Compute a plain-language batch SLA story from the per-day window records.
 * Pure function (no DOM). Every number is derived from the in-scope window df,
 * the same source the charts use, so it always reconciles with the visuals.
 */
function _buildBatchNarrative(winData, k) {
  k = k || {};
  const ceiling = _n(k.daily_limit_hrs, SLA_DAILY_HRS) || SLA_DAILY_HRS;
  const days = (Array.isArray(winData) ? winData : []).filter(w => w && w.run_date);
  if (!days.length) return null;

  const rec = days.map(w => {
    // effective_hrs = longest contiguous batch run (the SLA-binding wall-clock),
    // NOT first-start→last-end span (mostly idle gap on spread/sequenced batches).
    const elapsed = +(w.effective_hrs ?? w.elapsed_hrs ?? w.total_hrs ?? 0);
    // Buffer % relative to the BINDING (tightest) sub-app's OWN ceiling when the
    // backend supplies it (DAILY=6h, TUESDAY=6h, WEEKLY=9h …); else global daily.
    const bufPct = (w.min_buffer_pct != null && isFinite(+w.min_buffer_pct))
      ? +w.min_buffer_pct
      : (ceiling > 0 ? ((ceiling - elapsed) / ceiling) * 100 : 0);
    const overrun = (w.breach_overrun_hrs != null && isFinite(+w.breach_overrun_hrs))
      ? +w.breach_overrun_hrs : null;
    return {
      date: w.run_date, elapsed, bufPct, breach: !!w.breach, top: w.top_job || "",
      overrun,
      breachSub:  w.breach_sub_app || "",
      breachEff:  (w.breach_sub_effective != null) ? +w.breach_sub_effective : elapsed,
      breachCeil: (w.breach_sub_ceil != null) ? +w.breach_sub_ceil : ceiling,
      tightSub:   w.tight_sub_app || (w.top_job || ""),
      tightEff:   (w.tight_effective != null) ? +w.tight_effective : elapsed,
      tightCeil:  (w.tight_ceil != null) ? +w.tight_ceil : ceiling,
    };
  });

  const total      = rec.length;
  const breachDays = rec.filter(r => r.breach).length;
  const cleanDays  = total - breachDays;
  const compliance = (k.window_day_compliance_pct != null)
    ? _n(k.window_day_compliance_pct)
    : (total ? (cleanDays / total) * 100 : 0);

  // "Tight" = not a breach but buffer within the at-risk band (≤ ATRISK%).
  const tight     = rec.filter(r => !r.breach && r.bufPct <= SLA_ATRISK_PCT)
                       .sort((a, b) => a.bufPct - b.bufPct);
  const nonBreach = rec.filter(r => !r.breach).sort((a, b) => a.bufPct - b.bufPct);
  const tightest  = nonBreach.length ? nonBreach[0] : null;
  // Worst breach = largest overrun vs the BINDING sub-app's own ceiling (not the
  // longest span, which would just pick the day with the most idle gap).
  const breaches  = rec.filter(r => r.breach)
                       .sort((a, b) => (b.overrun ?? (b.elapsed - ceiling)) - (a.overrun ?? (a.elapsed - ceiling)));
  const worstBreach = breaches.length ? breaches[0] : null;

  // Minutes-of-buffer equivalent of the at-risk band (same %, in minutes).
  const atRiskMins = Math.round(ceiling * 60 * (SLA_ATRISK_PCT / 100));

  // Within-window trend: is the batch window GROWING or SHRINKING over the
  // period? Measured on mean elapsed hours per half (needs ≥6 days). A single
  // upload has no prior 30-day period, so we report the honest within-window
  // direction rather than fabricating a baseline. Hours read cleaner than the
  // buffer %, which goes deeply negative on heavy-breach days.
  let trend = null;
  if (total >= 6) {
    const mid  = Math.floor(total / 2);
    const mean = (arr) => arr.reduce((s, r) => s + r.elapsed, 0) / (arr.length || 1);
    const firstH = mean(rec.slice(0, mid));
    const lastH  = mean(rec.slice(mid));
    const deltaH = lastH - firstH;                 // + = window getting longer = worse
    trend = { firstH, lastH, deltaH, dir: deltaH > 0.25 ? "down" : deltaH < -0.25 ? "up" : "flat" };
  }

  const tone = (breachDays === 0 && tight.length === 0)
    ? "ok"
    : (compliance >= 95 ? "ok" : compliance >= 80 ? "warning" : "critical");

  return { ceiling, total, breachDays, cleanDays, compliance,
           tight, tightest, worstBreach, atRiskMins, trend, tone };
}

/** Render the batch story banner + computed micro-narrative callouts. */
function renderBatchStory(data, k) {
  const host = document.getElementById("batch-story");
  if (!host) return;
  const winData = (data && Array.isArray(data.window)) ? data.window : [];
  const nar = _buildBatchNarrative(winData, k);
  if (!nar) { host.classList.add("hidden"); host.innerHTML = ""; return; }

  // Window-size note shown above the charts row ("over the last N days").
  const noteEl = document.getElementById("batch-buffer-window-note");
  if (noteEl) noteEl.textContent = `over the last ${nar.total} day(s) · ${_fmtHrs(nar.ceiling)} SLA ceiling`;

  const customer = (window.appData && window.appData.customerName) || (k && k.customer_name) || "";
  const env  = ((k && (k.batch_env || k.env_type)) || "").toUpperCase();
  const who  = [customer, env].filter(Boolean).join(" · ");
  const toneHex = nar.tone === "ok" ? THEME.green : nar.tone === "warning" ? THEME.amber : THEME.red;

  const ans = nar.breachDays === 0
    ? `Yes — every one of the ${nar.total} day(s) finished inside the ${_fmtHrs(nar.ceiling)} window (${nar.compliance.toFixed(0)}% day compliance).`
    : `${nar.cleanDays}/${nar.total} day(s) finished inside the ${_fmtHrs(nar.ceiling)} window — ${nar.compliance.toFixed(0)}% day compliance, ${nar.breachDays} breach${nar.breachDays > 1 ? "es" : ""}.`;

  let trendChip = "";
  if (nar.trend) {
    const t = nar.trend;
    // dir "down" = window getting LONGER (worse, red ▼); "up" = shorter (better, green ▲)
    const arrow = t.dir === "down" ? "▼" : t.dir === "up" ? "▲" : "▬";
    const col   = t.dir === "down" ? THEME.red : t.dir === "up" ? THEME.green : THEME.muted;
    const word  = t.dir === "down" ? `+${_fmtHrs(Math.abs(t.deltaH))} longer`
                : t.dir === "up"   ? `${_fmtHrs(Math.abs(t.deltaH))} shorter`
                : "steady";
    trendChip = `<span class="inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded-md"
        style="color:${col};background:${hexA(col,0.12)};border:1px solid ${hexA(col,0.3)}"
        title="First half averaged a ${_fmtHrs(t.firstH)} window → second half ${_fmtHrs(t.lastH)} (vs ${_fmtHrs(nar.ceiling)} ceiling)">
        ${arrow} Window ${word}</span>`;
  }

  // Callouts, prioritised: when there are breaches, lead with the worst (most
  // actionable) and skip the marginal "tight" note; when there are none, surface
  // how close it got (early-warning) or confirm comfortable headroom.
  const callouts = [];
  if (nar.worstBreach) {
    const wb = nar.worstBreach;
    const over = (wb.overrun != null) ? wb.overrun : (wb.elapsed - nar.ceiling);
    let s;
    if (wb.breachSub) {
      s = `Worst breach: ${wb.date} — ${_esc(wb.breachSub)} ran ${_fmtHrs(wb.breachEff)}, +${_fmtHrs(over)} over its ${_fmtHrs(wb.breachCeil)} ceiling`;
    } else {
      s = `Worst breach: ${wb.date} ran ${_fmtHrs(wb.elapsed)} (+${_fmtHrs(over)} over the ceiling)`;
    }
    s += wb.top ? `; longest job ${_esc(wb.top)}.` : ".";
    callouts.push({ tone: "critical", text: s });
  }
  if (nar.breachDays === 0 && nar.tight.length) {
    let s = `${nar.tight.length} of ${nar.total} day(s) ran within ${SLA_ATRISK_PCT}% of their SLA ceiling`;
    if (nar.tightest) {
      const tc   = nar.tightest.tightCeil || nar.ceiling;
      const mins = Math.round((tc - nar.tightest.tightEff) * 60);
      s += ` — tightest was ${nar.tightest.date} at ${_fmtHrs(nar.tightest.tightEff)} vs its ${_fmtHrs(tc)} ceiling (${mins} min to spare`;
      s += nar.tightest.tightSub ? `, ${_esc(nar.tightest.tightSub)})` : ")";
    }
    callouts.push({ tone: "warning", text: s });
  }
  if (nar.breachDays === 0 && !nar.tight.length) {
    callouts.push({ tone: "ok",
      text: `Every day cleared the window comfortably — no day came within ${nar.atRiskMins} min of the ${_fmtHrs(nar.ceiling)} ceiling, so there is real headroom if a job slows down.` });
  }

  const calloutHtml = callouts.map(c => {
    const col = c.tone === "ok" ? THEME.green : c.tone === "warning" ? THEME.amber : THEME.red;
    return `<div class="flex items-start gap-2 text-[11px] leading-snug" style="color:${THEME.white}">
        <span class="mt-[3px] w-1.5 h-1.5 rounded-full shrink-0" style="background:${col}"></span>
        <span>${c.text}</span></div>`;
  }).join("");

  host.classList.remove("hidden");
  host.className = "rounded-2xl border px-5 py-4 shadow-panel";
  host.style.cssText = `border-color:${hexA(toneHex,0.35)};background:linear-gradient(135deg, ${hexA(toneHex,0.07)}, ${hexA(THEME.card2,0.45)})`;
  host.innerHTML = `
    <div class="flex items-start justify-between gap-3 flex-wrap">
      <div>
        <div class="text-[10px] font-bold uppercase tracking-widest text-Cmuted">Batch SLA — the headline question</div>
        <div class="text-base md:text-lg font-extrabold text-Cwhite mt-0.5">Are we meeting batch SLAs${who ? ` for ${_esc(who)}` : ""}?</div>
        <div class="text-[12px] md:text-[13px] font-semibold mt-1" style="color:${toneHex}">${ans}</div>
      </div>
      <div class="flex items-center gap-2 shrink-0">${trendChip}</div>
    </div>
    ${calloutHtml ? `<div class="mt-3 pt-3 border-t space-y-1.5" style="border-color:${hexA(THEME.border,0.6)}">${calloutHtml}</div>` : ""}
  `;
}


function renderSlaBufferChart(k) {
  const canvas = document.getElementById("chart-sla-buffer");
  if (!canvas) return;
  destroyChart("slaBuffer");
  const buf = k.fleet_sla_buffer;
  const bufferPct = buf ? Math.max(0, Math.min(100, buf.buffer_pct)) : 0;
  const bufferColor =
    !buf                       ? THEME.muted :
    buf.status === "EXCELLENT" ? THEME.green :
    buf.status === "HEALTHY"   ? THEME.green :
    buf.status === "CAUTION"   ? THEME.amber : THEME.red;

  // Speedometer zones from the shared pe_config buffer thresholds (single source —
  // identical bands drive the daily bars + legends + narrative).
  const atRisk = SLA_ATRISK_PCT, longJob = SLA_LONGJOB_PCT;
  charts.slaBuffer = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: [`At risk (≤${atRisk}%)`, `Caution (${atRisk}–${longJob}%)`, `Healthy (>${longJob}%)`],
      datasets: [{
        data: [atRisk, longJob - atRisk, 100 - longJob],
        backgroundColor: [hexA(THEME.red, 0.75), hexA(THEME.amber, 0.75), hexA(THEME.green, 0.75)],
        borderColor: [THEME.card2, THEME.card2, THEME.card2],
        borderWidth: 2,
        hoverOffset: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "72%",
      rotation: -90,
      circumference: 180,
      layout: { padding: { bottom: 20 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: THEME.card,
          borderColor: THEME.border,
          borderWidth: 1,
          titleColor: THEME.white,
          bodyColor: THEME.muted,
          callbacks: {
            label: (ctx) => ctx.label,
          },
        },
      },
    },
    plugins: [
      gaugeNeedlePlugin(bufferPct, bufferColor),
      centerTextPlugin(buf ? `${_n(buf.buffer_pct).toFixed(0)}%` : "N/A", buf?.status || ""),
    ],
  });

  // ── Interpretive subtitle: say what the % MEANS, not just the number ──
  const subEl = document.getElementById("chart-sla-subtitle");
  if (subEl) {
    if (buf) {
      const band = _bufferBand(buf.buffer_pct);
      subEl.innerHTML = `Worst-job headroom before its SLA ceiling — <b style="color:${band.color}">`
        + `${_n(buf.buffer_pct).toFixed(0)}% buffer (${band.label})</b>. `
        + `How much a job's runtime can grow before it breaches.`;
    } else {
      subEl.textContent = "Headroom between worst-job peak and the daily SLA limit";
    }
  }

  // ── Shared buffer-band legend (same green/amber/red as the daily bars) ──
  const legEl = document.getElementById("chart-sla-buffer-legend");
  if (legEl) {
    legEl.className = "flex flex-wrap items-center gap-x-3 gap-y-1 mt-2 px-1 text-[10px] text-Cmuted";
    legEl.innerHTML = _bufferBandLegendHtml(k.daily_limit_hrs);
  }
}

/**
 * Shared buffer-band legend HTML — green/amber/red keyed to the SAME
 * SLA_ATRISK_PCT/SLA_LONGJOB_PCT thresholds across the gauge and the daily bars,
 * so identical colours mean identical things on every batch panel.
 */
function _bufferBandLegendHtml(ceiling) {
  const C = Number(ceiling) || SLA_DAILY_HRS;
  const atRiskMins = Math.round(C * 60 * (SLA_ATRISK_PCT / 100));
  const chip = (col, label) =>
    `<span class="inline-flex items-center gap-1"><span class="inline-block w-3 h-3 rounded-sm" style="background:${col}"></span> ${label}</span>`;
  return [
    chip(THEME.green, `Healthy &middot; &gt;${SLA_LONGJOB_PCT}% buffer`),
    chip(THEME.amber, `Tight &middot; ${SLA_ATRISK_PCT}–${SLA_LONGJOB_PCT}% buffer`),
    chip(THEME.red,   `At risk / breach &middot; ≤${SLA_ATRISK_PCT}% (within ${atRiskMins} min of the ${_fmtHrs(C)} ceiling)`),
    `<span class="inline-flex items-center gap-1" style="color:${THEME.amber}">⚡ unusually long day (statistical spike)</span>`,
  ].join("");
}

// Chart.js plugin: speedometer needle over the half-doughnut
function gaugeNeedlePlugin(valuePct, color) {
  return {
    id: "gaugeNeedle",
    afterDatasetsDraw(chart) {
      const { ctx, chartArea } = chart;
      const meta = chart.getDatasetMeta(0);
      if (!chartArea || !meta?.data?.length) return;
      const arc = meta.data[0];
      const cx = arc.x;
      const cy = arc.y;
      const outerR = arc.outerRadius;
      const innerR = arc.innerRadius;
      // -90° rotation, 180° sweep: 0% → pointing left (π), 100% → pointing right (0)
      const angle = Math.PI + (Math.max(0, Math.min(100, valuePct)) / 100) * Math.PI;
      const needleLen = (innerR + outerR) / 2;

      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(angle);
      // Needle body
      ctx.beginPath();
      ctx.moveTo(0, -3.5);
      ctx.lineTo(needleLen, 0);
      ctx.lineTo(0, 3.5);
      ctx.closePath();
      ctx.fillStyle = color || THEME.white;
      ctx.shadowColor = hexA(color || "#ffffff", 0.5);
      ctx.shadowBlur = 6;
      ctx.fill();
      ctx.restore();

      // Hub
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, 6, 0, Math.PI * 2);
      ctx.fillStyle = color || THEME.white;
      ctx.fill();
      ctx.beginPath();
      ctx.arc(cx, cy, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = THEME.card2;
      ctx.fill();
      ctx.restore();
    },
  };
}

// Chart.js plugin: center text inside the doughnut hole
function centerTextPlugin(big, sub) {
  return {
    id: "centerText",
    afterDraw(chart) {
      const { ctx, chartArea } = chart;
      if (!chartArea) return;
      const cx = (chartArea.left + chartArea.right) / 2;
      const cy = chartArea.bottom - 8;
      ctx.save();
      ctx.textAlign = "center";
      ctx.textBaseline = "alphabetic";
      ctx.fillStyle = THEME.white;
      ctx.font = '800 30px "Sora", sans-serif';
      ctx.fillText(big, cx, cy - 22);
      ctx.fillStyle = THEME.muted;
      ctx.font = '700 10px "Sora", sans-serif';
      ctx.fillText(sub, cx, cy - 4);
      ctx.restore();
    },
  };
}


// ─────────────────────────────────────────────────────────────
// Chart 2 — Daily Batch Window (premium bar chart)
// ─────────────────────────────────────────────────────────────
function renderWindowTrendChart(winData, topJobsData) {
  const canvas = document.getElementById("chart-window-trend");
  if (!canvas) return;
  destroyChart("windowTrend");

  if (!winData || winData.length === 0) return;

  // Build excluded set from ALL top_jobs (unfiltered) — caller must pass full list
  const excludedNameSet = new Set((topJobsData || []).filter(j => _isJobExcluded(j)).map(j => j.Job_Name));
  const labels   = winData.map((w) => w.run_date);
  const counts   = winData.map((w) => {
    const rawNames = Array.isArray(w.raw_job_names) ? w.raw_job_names : [];
    if (rawNames.length) return rawNames.filter(n => !excludedNameSet.has(n)).length;
    // No per-day name list — fall back to server count (exclusions unknown per day)
    return w.job_count || 0;
  });
  const rawCounts = winData.map((w) => {
    const rawNames = Array.isArray(w.raw_job_names) ? w.raw_job_names : [];
    // Prefer raw_job_names.length — server raw_job_count may include extras not in name list
    return rawNames.length || w.raw_job_count || w.job_count || 0;
  });
  // Excluded count per day = actual intersection of raw_job_names with excludedNameSet
  const excludedCounts = winData.map((w) => {
    const rawNames = Array.isArray(w.raw_job_names) ? w.raw_job_names : [];
    if (rawNames.length) return rawNames.filter(n => excludedNameSet.has(n)).length;
    return 0; // can't tell without per-day names
  });
  const topJobs  = winData.map((w) => w.top_job || "");
  const rawSums  = winData.map((w) => +(w.total_hrs  || 0));
  const rawElaps = winData.map((w) => +(w.elapsed_hrs || 0));
  const rawEff   = winData.map((w) => +(w.effective_hrs || 0));

  // Point-2: the BAR is the SLA-binding effective window — the longest
  // CONTIGUOUS batch run (largest_block) — NOT the first→last elapsed span.
  // The span is mostly idle gap for spread / sequenced batches (a 2-phase
  // daily reads as ~20h) and plotting it made almost every bar look red against
  // the ceiling even when no batch actually ran long. effective_hrs reconciles
  // the bar height + colour with the (block-based) breach verdict. Falls back to
  // elapsed span, then summed runtime, for legacy payloads without block data.
  const hasEff     = rawEff.some(v => v > 0);
  const hasElapsed = rawElaps.some(v => v > 0);
  const values  = winData.map((_, i) =>
    hasEff && rawEff[i] > 0 ? rawEff[i]
    : hasElapsed && rawElaps[i] > 0 ? rawElaps[i]
    : rawSums[i]);

  // ── Spike detection: statistical z-score on window values ───
  const vMean = values.reduce((s, v) => s + v, 0) / values.length;
  const vStd  = Math.sqrt(values.reduce((s, v) => s + (v - vMean) ** 2, 0) / values.length);
  // A day is a "spike" if its value is >1.5 std above mean AND above SLA, or >2std
  const spikeIdxs = new Set(values.map((v, i) => {
    const z = vStd > 0 ? (v - vMean) / vStd : 0;
    return (z > 2.0 || (z > 1.5 && winData[i]?.breach)) ? i : -1;
  }).filter(i => i >= 0));

  // Gradient fills
  const ctxCanvas = canvas.getContext("2d");
  const h = canvas.parentElement?.clientHeight || 420;
  const breachGrad = ctxCanvas.createLinearGradient(0, 0, 0, h);
  breachGrad.addColorStop(0, "rgba(244,63,94,0.95)");
  breachGrad.addColorStop(1, "rgba(244,63,94,0.40)");
  const amberGrad = ctxCanvas.createLinearGradient(0, 0, 0, h);
  amberGrad.addColorStop(0, "rgba(245,158,11,0.92)");   // tight (15–40% buffer)
  amberGrad.addColorStop(1, "rgba(245,158,11,0.34)");
  const greenGrad = ctxCanvas.createLinearGradient(0, 0, 0, h);
  greenGrad.addColorStop(0, "rgba(16,217,110,0.88)");   // healthy (>40% buffer)
  greenGrad.addColorStop(1, "rgba(16,217,110,0.30)");

  // ── Bars coloured by SLA BUFFER BAND — the SAME green/amber/red thresholds
  // as the gauge (single source: SLA_ATRISK_PCT / SLA_LONGJOB_PCT). A bar's
  // colour now means exactly what the gauge zone means, so the two panels agree.
  // Statistical spikes are kept as a ⚡ overlay annotation (drawn separately),
  // not as a base colour, so anomaly detection isn't lost.
  const _dayBand = (w, i) => {
    if (w.breach) return "red";
    const bufPct = SLA_DAILY_HRS > 0 ? ((SLA_DAILY_HRS - values[i]) / SLA_DAILY_HRS) * 100 : 100;
    return bufPct <= SLA_ATRISK_PCT ? "red" : bufPct <= SLA_LONGJOB_PCT ? "amber" : "green";
  };
  const bgColors  = winData.map((w, i) => {
    const b = _dayBand(w, i);
    return b === "red" ? breachGrad : b === "amber" ? amberGrad : greenGrad;
  });
  const bdrColors = winData.map((w, i) => {
    const b = _dayBand(w, i);
    return b === "red" ? "rgba(244,63,94,0.9)" : b === "amber" ? "rgba(245,158,11,0.9)" : "rgba(16,217,110,0.8)";
  });

  // Update subtitle
  const metricTypeEl = document.getElementById("chart-window-metric-type");
  if (metricTypeEl) {
    metricTypeEl.textContent = hasEff
      ? "Bar = effective batch window (longest contiguous run — the SLA-binding duration) · bar colour = SLA buffer band · teal line = active busy time (real compute) · elapsed span shown on hover"
      : hasElapsed
      ? "Bar height = elapsed span (first start → last end) · bar colour = SLA buffer band · teal line = active busy time (real compute)"
      : "Summed runtime (all jobs — may overcount parallel runs)";
    metricTypeEl.className = hasElapsed
      ? "text-[9px] text-Cteal font-semibold mt-0.5"
      : "text-[9px] text-Camber font-semibold mt-0.5";
  }
  const countNoteEl = document.getElementById("chart-window-count-note");
  if (countNoteEl) {
    countNoteEl.textContent = "Bar labels = unique jobs in scope. Hover for total runs (executions), busy vs idle time, and batch blocks.";
  }

  // Peak bar index
  let peakIdx = 0, peakVal = 0;
  values.forEach((v, i) => { if (v > peakVal) { peakVal = v; peakIdx = i; } });

  const breachCount  = winData.filter(w => w.breach).length;
  const spikeCount   = spikeIdxs.size;
  const maxCount     = Math.max(...counts, 1);

  // ── Enhanced canvas plugin: job counts + spike labels ──────
  const enrichPlugin = {
    id: "enrichLabels",
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(0);
      if (!meta) return;
      const ctx = chart.ctx;
      ctx.save();

      // Adaptive font size: shrink for many bars
      const nBars = winData.length;
      const barFontPx = nBars > 25 ? 7 : nBars > 15 ? 8 : 9;

      winData.forEach((w, i) => {
        if (!meta.data[i]) return;
        const bar = meta.data[i];
        const yVal = values[i];
        const cnt  = counts[i];
        const isSpike = spikeIdxs.has(i);
        const isBreak = w.breach;

        // --- Job count label INSIDE bar (bottom) ---
        if (cnt > 0) {
          const barHeight = Math.abs(bar.base - bar.y);
          if (barHeight > 18) {
            ctx.save();
            ctx.textAlign = "center";
            ctx.font = `600 ${barFontPx}px "Sora", monospace`;
            ctx.fillStyle = hexA(THEME.white, 0.55);
            ctx.fillText(`${cnt}`, bar.x, bar.base - 5);
            ctx.restore();
          }
        }

        // --- Hours label above breach / spike bars ---
        if (isBreak || isSpike) {
          ctx.textAlign = "center";
          ctx.font = `bold ${barFontPx + 1}px "Sora", sans-serif`;
          ctx.fillStyle = isBreak ? THEME.red : THEME.amber;
          ctx.fillText(yVal.toFixed(1) + "h", bar.x, bar.y - 6);
        }

        // --- Spike bolt icon above spike bars ---
        if (isSpike && !isBreak) {
          ctx.textAlign = "center";
          ctx.font = `bold ${barFontPx + 2}px "Sora", sans-serif`;
          ctx.fillStyle = THEME.amber;
          ctx.fillText("⚡", bar.x, bar.y - 18);
        }
      });

      // --- Peak marker ---
      if (peakVal > 0 && meta.data[peakIdx]) {
        const bar = meta.data[peakIdx];
        ctx.textAlign = "center";
        ctx.fillStyle = THEME.amber;
        ctx.font = `bold 8px "Sora", sans-serif`;
        ctx.fillText("▲ WORST", bar.x, bar.y - (winData[peakIdx]?.breach ? 20 : 6));
      }

      // --- Summary badge top-right ---
      const badgeParts = [];
      if (breachCount > 0) badgeParts.push(`${breachCount} breach${breachCount > 1 ? "es" : ""}`);
      if (spikeCount > 0)  badgeParts.push(`${spikeCount} spike${spikeCount > 1 ? "s" : ""}`);
      if (badgeParts.length > 0) {
        const txt = badgeParts.join("  ·  ");
        ctx.font = 'bold 9px "Sora", sans-serif';
        ctx.textAlign = "right";
        const tw = ctx.measureText(txt).width;
        const bx = chart.chartArea.right - tw - 18;
        const by = chart.chartArea.top + 6;
        ctx.fillStyle = hexA(THEME.red, 0.14);
        _roundRect(ctx, bx, by, tw + 14, 18, 4);
        ctx.fill();
        ctx.strokeStyle = hexA(THEME.red, 0.35);
        ctx.lineWidth = 1;
        _roundRect(ctx, bx, by, tw + 14, 18, 4);
        ctx.stroke();
        ctx.fillStyle = THEME.red;
        ctx.fillText(txt, chart.chartArea.right - 11, by + 13);
      }
      ctx.restore();
    },
  };

  // ── Spike pattern annotations passed to heatmap ─────────────
  window._batchWindowSpikes = [...spikeIdxs].map(i => ({ date: labels[i], value: values[i], count: counts[i] }));

  // Bar width auto-sizing: when ≤16 bars use wider bars
  const nBars = winData.length;
  const barPct = nBars <= 10 ? 0.85 : nBars <= 20 ? 0.78 : 0.70;

  const busyVals = winData.map((w) => +(w.active_busy_hrs || 0));
  const hasBusy = hasElapsed && busyVals.some(v => v > 0);

  charts.windowTrend = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: hasEff ? "Effective batch window (h)" : hasElapsed ? "Elapsed Window (h)" : "Daily Total (h)",
        data: values,
        backgroundColor: bgColors,
        borderColor: bdrColors,
        borderWidth: 1.5,
        borderRadius: 5,
        borderSkipped: false,
        barPercentage: barPct,
        categoryPercentage: 0.85,
        order: 2,
      },
      ...(hasBusy ? [{
        // Active busy time (interval union) overlaid as a line so the gap
        // between the inflated elapsed span and real compute time is visible
        // at a glance — the bar can be ~20h while busy time is only ~5h.
        type: "line",
        label: "Active busy time (h)",
        data: busyVals,
        borderColor: "rgba(45,212,191,0.95)",   // teal
        backgroundColor: "rgba(45,212,191,0.12)",
        borderWidth: 2,
        pointRadius: 2.5,
        pointBackgroundColor: "rgba(45,212,191,1)",
        pointHoverRadius: 4,
        fill: true,
        tension: 0.25,
        order: 1,
      }] : []),
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 38, right: 12, left: 4, bottom: 4 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "rgba(11,18,32,0.97)",
          borderColor: THEME.border,
          borderWidth: 1,
          titleFont: { family: "Sora", size: 12, weight: "bold" },
          bodyFont: { family: "Sora", size: 10 },
          titleColor: THEME.white,
          bodyColor: "#94a3b8",
          padding: { x: 14, y: 10 },
          cornerRadius: 8,
          displayColors: false,
          callbacks: {
            title: (items) => {
              const i = items[0]?.dataIndex;
              if (i == null) return "";
              const isSpike = spikeIdxs.has(i);
              const spike = isSpike ? "  ⚡ SPIKE" : "";
              const shown = counts[i] || 0;
              const raw = rawCounts[i] || shown;
              const excluded = excludedCounts[i] || Math.max(raw - shown, 0);
              return `${labels[i]}  ·  ${shown} shown / ${raw} raw / ${excluded} excluded${spike}`;
            },
            label: (ctx) => {
              const i = ctx.dataIndex;
              // The teal "active busy time" line is dataset 1 — give it a one-line
              // label and let the bar (dataset 0) carry the full breakdown.
              if (ctx.datasetIndex && ctx.datasetIndex > 0) {
                return `Active busy time: ${(+(ctx.parsed?.y || 0)).toFixed(2)}h`;
              }
              const lines = [];
              const w = winData[i] || {};
              const shown = counts[i] || 0;
              const raw = rawCounts[i] || shown;
              const excluded = excludedCounts[i] || Math.max(raw - shown, 0);
              const rawNames = Array.isArray(winData[i]?.raw_job_names) ? winData[i].raw_job_names : [];
              const excludedNames = rawNames.filter(n => excludedNameSet.has(n));
              if (hasElapsed) {
                const effH = +(w.effective_hrs || 0);
                const ceilH = +(w.breach_sub_ceil || w.sla_hrs || w.sla_ceil || SLA_DAILY_HRS || 0);
                if (effH > 0) {
                  const verdict = w.breach
                    ? `BREACH — ${w.breach_sub_app || "a sub-app"} +${(+(w.breach_overrun_hrs ?? (effH - ceilH))).toFixed(1)}h over its ${ceilH ? ceilH.toFixed(1) : "?"}h ceiling`
                    : "within window";
                  lines.push(`Effective batch window: ${effH.toFixed(2)}h  (longest contiguous run — judged vs SLA → ${verdict})`);
                }
                lines.push(`Elapsed span: ${rawElaps[i].toFixed(2)}h  (first start → last end — includes idle gaps)`);
                // Busy-time decomposition: the elapsed span overstates real work
                // when jobs run in separated clusters. Show the active compute time
                // (interval union) + idle gap so buffer is read against real load.
                const busy = +(w.active_busy_hrs || 0);
                const idle = +(w.idle_gap_hrs || 0);
                const idlePct = +(w.idle_pct || 0);
                if (busy > 0) {
                  lines.push(`Active busy time: ${busy.toFixed(2)}h  (real compute — overlaps counted once)`);
                  lines.push(`Idle inside span: ${idle.toFixed(2)}h  (${idlePct.toFixed(0)}% of the span is gaps)`);
                }
                if (rawSums[i] > 0) lines.push(`Summed runtime: ${rawSums[i].toFixed(2)}h  (all runs added up)`);
                // Batch blocks: morning/evening clusters split by idle gaps.
                const blocks = Array.isArray(w.batch_blocks) ? w.batch_blocks : [];
                if (blocks.length > 1) {
                  lines.push(`Batch blocks: ${blocks.length} clusters separated by idle gaps —`);
                  blocks.slice(0, 4).forEach(b => {
                    lines.push(`   • ${b.start}–${b.end}  ·  ${(+b.span_hrs).toFixed(2)}h  ·  ${b.runs} runs`);
                  });
                  if (blocks.length > 4) lines.push(`   • … +${blocks.length - 4} more block(s)`);
                } else if (blocks.length === 1) {
                  lines.push(`Batch block: ${blocks[0].start}–${blocks[0].end} (single continuous cluster)`);
                }
              } else {
                lines.push(`Total (summed): ${rawSums[i].toFixed(2)}h`);
              }
              lines.push(`Unique jobs shown here: ${shown}`);
              lines.push(`Raw unique jobs in file: ${raw}`);
              // Total executions (runs) — distinct from unique-job count. This is the
              // number an analyst gets when counting CSV rows for the day (a job that
              // ran 3× = 3 runs but 1 unique job). Surfacing it kills the runs-vs-jobs
              // confusion (e.g. 218 runs on a day with 207 unique jobs).
              const totRuns = +(w.raw_run_count || 0);
              const scopeRuns = +(w.scope_run_count || 0);
              if (totRuns > 0) {
                const repeats = Math.max(totRuns - raw, 0);
                lines.push(`Total executions (runs): ${totRuns}${repeats > 0 ? `  (${repeats} are repeat runs)` : ""}`);
              }
              lines.push(`Unique jobs excluded from chart scope: ${excluded}`);
              if (excludedNames.length > 0) {
                const preview = excludedNames.slice(0, 6).join(", ");
                lines.push(`Excl. jobs: ${preview}${excludedNames.length > 6 ? `, … +${excludedNames.length - 6} more` : ""}`);
              } else if (excludedNameSet.size > 0 && rawNames.length === 0) {
                lines.push(`Excluded jobs: ${excludedNameSet.size} global exclusions (no per-day name data)`);
              } else {
                lines.push("Excluded jobs: none in this day's data");
              }
              if (topJobs[i]) lines.push(`Longest job: ${topJobs[i]}`);
              if (winData[i]?.breach) {
                const ov  = (w.breach_overrun_hrs != null) ? +w.breach_overrun_hrs : (values[i] - SLA_DAILY_HRS);
                const sub = w.breach_sub_app ? `${w.breach_sub_app} ` : "";
                const cl  = (w.breach_sub_ceil != null) ? ` ${(+w.breach_sub_ceil).toFixed(1)}h ceiling` : " limit";
                lines.push(`⚠ SLA BREACH  ${sub}+${ov.toFixed(1)}h over${cl}`);
              }
              return lines;
            },
          },
        },
        annotation: undefined,
        zoom: _zoomConfig({ mode: "x" }),
      },
      scales: {
        x: {
          ticks: {
            color: THEME.muted,
            font: { family: "Sora", size: nBars > 25 ? 7 : 9 },
            maxRotation: 45,
            minRotation: 30,
            autoSkip: nBars > 30,
            autoSkipPadding: 6,
          },
          grid: { color: hexA(THEME.border, 0.2), drawBorder: false },
        },
        y: {
          beginAtZero: true,
          title: {
            display: true,
            text: hasEff ? "Effective window hrs (contiguous run)" : hasElapsed ? "Elapsed hrs (wall-clock)" : "Hours (summed)",
            color: THEME.muted,
            font: { family: "Sora", size: 10 },
          },
          ticks: {
            color: THEME.muted,
            font: { family: "Sora", size: 10 },
            stepSize: vMean > 20 ? 10 : vMean > 10 ? 5 : 2,
          },
          grid: { color: hexA(THEME.border, 0.18), drawBorder: false },
        },
      },
    },
    plugins: [slaLinePlugin(SLA_DAILY_HRS), enrichPlugin, crosshairPlugin],
  });

  // ── Shared buffer-band legend (identical semantics to the SLA Buffer gauge) ──
  const winLeg = document.getElementById("chart-window-legend");
  if (winLeg) winLeg.innerHTML = _bufferBandLegendHtml(SLA_DAILY_HRS)
    + `<span class="inline-flex items-center gap-1"><span class="inline-block w-4 h-0 border-t-2" style="border-color:rgba(45,212,191,0.95)"></span> active busy time</span>`;

  // ── Render spike legend below chart ─────────────────────────
  _renderWindowSpikePanel(winData, spikeIdxs, values, counts);

  // Export toolbar
  _addChartToolbar(canvas.parentElement, charts.windowTrend, () => {
    let csv = "Date,Window_Hrs,Job_Count,Breach,Spike,Top_Job\n";
    winData.forEach((w, i) => {
      csv += `${w.run_date},${values[i].toFixed(2)},${counts[i]},${w.breach || false},${spikeIdxs.has(i)},${topJobs[i]}\n`;
    });
    return csv;
  });
}

/** Render a compact spike summary panel below the window chart. */
function _renderWindowSpikePanel(winData, spikeIdxs, values, counts) {
  let panel = document.getElementById("window-spike-panel");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "window-spike-panel";
    document.getElementById("chart-window-trend")?.parentElement?.after(panel);
  }

  if (spikeIdxs.size === 0 && !winData.some(w => w.breach)) {
    panel.innerHTML = "";
    return;
  }

  const items = [];
  winData.forEach((w, i) => {
    if (spikeIdxs.has(i) || w.breach) {
      const type = w.breach ? "breach" : "spike";
      items.push({ date: w.run_date, val: values[i], count: counts[i], type, top: w.top_job || "" });
    }
  });

  panel.className = "mt-2 rounded-xl border px-3 py-2";
  panel.style.cssText = `border-color:${hexA(THEME.amber,0.3)};background:${hexA(THEME.amber,0.04)}`;
  panel.innerHTML = `
    <div class="flex items-center gap-2 mb-1.5">
      <span class="text-[9px] font-bold uppercase tracking-widest text-Camber">Pattern Detection</span>
      <span class="text-[8px] px-1.5 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.12)}">${items.length} anomalous day${items.length !== 1 ? "s" : ""}</span>
    </div>
    <div class="flex flex-wrap gap-1.5">
      ${items.map(it => `
        <span class="text-[8px] font-mono px-1.5 py-0.5 rounded cursor-default"
              style="color:${it.type === "breach" ? THEME.red : THEME.amber};background:${hexA(it.type === "breach" ? THEME.red : THEME.amber, 0.1)};border:1px solid ${hexA(it.type === "breach" ? THEME.red : THEME.amber, 0.3)}"
              title="${it.date}: ${it.val.toFixed(2)}h · ${it.count} jobs${it.top ? " · " + it.top : ""}">
          ${it.type === "breach" ? "⚠" : "⚡"} ${it.date} ${it.val.toFixed(1)}h
        </span>`).join("")}
    </div>
  `;
}


// Canvas helper: rounded rectangle path (used by chart plugins)
function _roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

// Chart.js plugin: dashed horizontal SLA line on the y-axis
function slaLinePlugin(slaHrs) {
  return {
    id: "slaLine",
    afterDatasetsDraw(chart) {
      const yScale = chart.scales.y;
      if (!yScale) return;
      const y = yScale.getPixelForValue(slaHrs);
      const { left, right } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      // Dashed line
      ctx.strokeStyle = hexA(THEME.red, 0.7);
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      // Label badge
      const label = `${slaHrs}h SLA`;
      ctx.font = 'bold 9px "Sora", sans-serif';
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = hexA(THEME.red, 0.12);
      _roundRect(ctx, left + 4, y - 16, tw + 10, 14, 3);
      ctx.fill();
      ctx.fillStyle = THEME.red;
      ctx.textAlign = "left";
      ctx.fillText(label, left + 9, y - 6);
      ctx.restore();
    },
  };
}


// ─────────────────────────────────────────────────────────────
// Chart 3 — Top 15 jobs horizontal bar (Plotly grouped → Chart.js bar)
// ─────────────────────────────────────────────────────────────
function renderTopJobsChart(topJobs, kpis) {
  const canvas = document.getElementById("chart-top-jobs");
  if (!canvas) return;
  destroyChart("topJobs");

  // Use the resolved SLA ceiling from this dataset (per compute_metrics),
  // falls back to the global SLA_DAILY_HRS which is already synced above.
  const chartSla = kpis?.daily_limit_hrs ?? SLA_DAILY_HRS;

  // Sort ascending so the largest peak appears at the top of a horizontal bar
  const sorted = [...topJobs].sort((a, b) => a.peak_hrs - b.peak_hrs);
  const labels = sorted.map((j) => truncate(j.Job_Name, 32));
  const peak   = sorted.map((j) => j.peak_hrs);
  const avg    = sorted.map((j) => j.avg_hrs);

  // Colour each bar against its own per-job SLA ceiling (sla_hrs from payload)
  // Falls back to chartSla (global ceiling) when sla_hrs not present.
  const peakColors = sorted.map((j, i) => {
    const jobSla = j.sla_hrs ?? chartSla;
    return peak[i] > jobSla           ? THEME.red  :
           peak[i] > jobSla * 0.8    ? THEME.amber : THEME.blue;
  });

  charts.topJobs = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Avg (h)",
          data: avg,
          backgroundColor: hexA(THEME.blue, 0.45),
          borderColor: THEME.blue,
          borderWidth: 1,
          borderRadius: 3,
          barPercentage: 0.95,
          categoryPercentage: 0.85,
          stack: "runtime",
        },
        {
          label: "Peak (h)",
          data: peak,
          backgroundColor: peakColors.map((c) => hexA(c, 0.85)),
          borderColor: peakColors,
          borderWidth: 1,
          borderRadius: 3,
          barPercentage: 0.95,
          categoryPercentage: 0.85,
          stack: "peak",
        },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: THEME.muted,
            font: { family: "Sora", size: 10, weight: "600" },
            boxWidth: 10,
            boxHeight: 10,
          },
        },
        tooltip: {
          backgroundColor: THEME.card,
          borderColor: THEME.border,
          borderWidth: 1,
          titleColor: THEME.white,
          bodyColor: THEME.muted,
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.x.toFixed(3)} h`,
          },
        },
        zoom: _zoomConfig({ mode: "y" }),
      },
      scales: {
        x: {
          beginAtZero: true,
          title: { display: true, text: "Runtime (hrs)", color: THEME.muted, font: { size: 10 } },
          ticks: { color: THEME.muted, font: { family: "Sora", size: 9 } },
          grid: { color: hexA(THEME.border, 0.4), drawBorder: false },
        },
        y: {
          ticks: { color: THEME.muted, font: { family: "Sora", size: 9 }, autoSkip: false },
          grid: { display: false },
        },
      },
    },
    plugins: [verticalSlaLinePlugin(chartSla), crosshairPlugin],
  });

  // Enterprise: export toolbar
  _addChartToolbar(canvas.parentElement, charts.topJobs, () => {
    let csv = "Job_Name,Avg_Hrs,Peak_Hrs,SLA_Hrs\n";
    labels.forEach((l, i) => { csv += `${l},${avg[i].toFixed(3)},${peak[i].toFixed(3)},${chartSla}\n`; });
    return csv;
  });
}

function verticalSlaLinePlugin(slaHrs) {
  return {
    id: "verticalSlaLine",
    afterDatasetsDraw(chart) {
      const xScale = chart.scales.x;
      if (!xScale) return;
      const x = xScale.getPixelForValue(slaHrs);
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = THEME.amber;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = THEME.amber;
      ctx.font = '700 10px "Sora", sans-serif';
      ctx.textAlign = "left";
      ctx.fillText(`SLA ${slaHrs}h`, x + 4, top + 12);
      ctx.restore();
    },
  };
}


// ─────────────────────────────────────────────────────────────
// Top breaches HTML table
// Shows true breaches (buffer<0) when present, otherwise shows
// top 10 jobs by peak hours as a ranked heat-map fallback.
// ─────────────────────────────────────────────────────────────
function renderTopBreachesTable(rows, kpis) {
  const tbody    = document.getElementById("top-jobs-tbody");
  const wrap     = document.getElementById("top-jobs-wrap");
  const empty    = document.getElementById("top-jobs-empty");
  const title    = document.getElementById("top-jobs-title");
  const subtitle = document.getElementById("top-jobs-subtitle");
  const badge    = document.getElementById("top-jobs-badge");
  if (!tbody) return;

  tbody.innerHTML = "";

  const all = rows || [];
  if (all.length === 0) {
    empty?.classList.remove("hidden");
    wrap?.classList.add("hidden");
    badge?.classList.add("hidden");
    return;
  }

  // Separate true breaches vs at-risk/healthy
  const trueBreaches = all.filter((r) => (r.buffer_pct ?? 100) < 0);
  const displayRows  = trueBreaches.length > 0 ? trueBreaches : all.slice(0, 10);
  const isFallback   = trueBreaches.length === 0;
  const defaultSla   = kpis?.daily_limit_hrs ?? SLA_DAILY_HRS;

  // Adaptive mode: PATH C (no XLSX loaded) — breaches are regressions vs history baseline
  const batchSlaPath  = window.appData?.batch?.kpis?.sla_path
    || (rows.some(r => r.sla_source === "adaptive") ? "C" : null);
  const isAdaptiveMode = batchSlaPath === "C"
    || rows.some(r => r.sla_source === "adaptive");

  // Observation period from batch data
  const batchKpis = window.appData?.batch?.kpis || window.appData?.batch || {};
  const dateRange = batchKpis.date_range || [];
  const dataSpanDays = batchKpis.date_span_days || batchKpis.window_total_days || 0;
  const dateRangeLabel = dateRange.length === 2
    ? `${dateRange[0]} → ${dateRange[1]}`
    : (dataSpanDays > 0 ? `${dataSpanDays} day(s)` : "");
  const slaCeiling = batchKpis.sla_ceiling || batchKpis.sla_daily_hrs || defaultSla;

  // Update title / subtitle dynamically
  if (title) {
    if (isAdaptiveMode && !isFallback) {
      title.textContent = `Top ${trueBreaches.length} Performance Regressions`;
    } else {
      title.textContent = isFallback
        ? "Top 10 Jobs by Peak Runtime"
        : `Top ${trueBreaches.length} Breaching Jobs`;
    }
  }
  if (subtitle) {
    if (isFallback) {
      const parts = ["No SLA breaches — showing ranked jobs by peak runtime"];
      if (dateRangeLabel) parts.push(dateRangeLabel);
      subtitle.textContent = parts.join(" · ");
    } else if (isAdaptiveMode) {
      subtitle.textContent = `${trueBreaches.length} job(s) exceeded adaptive history baseline`
        + (dateRangeLabel ? ` · ${dateRangeLabel}` : "")
        + "  ·  No contract SLA loaded";
    } else {
      subtitle.textContent = `${trueBreaches.length} job(s) exceeded their SLA window`
        + (dateRangeLabel ? ` · ${dateRangeLabel}` : "");
    }
  }
  // Badge
  if (badge) {
    if (!isFallback) {
      badge.textContent = isAdaptiveMode
        ? `${trueBreaches.length} regression${trueBreaches.length !== 1 ? "s" : ""}`
        : `${trueBreaches.length} breach${trueBreaches.length !== 1 ? "es" : ""}`;
      badge.className = isAdaptiveMode
        ? "metric-badge metric-badge-amber"
        : "metric-badge metric-badge-red";
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }

  // Adaptive mode warning banner
  const existingAdaptBanner = document.getElementById("top-jobs-adaptive-banner");
  if (existingAdaptBanner) existingAdaptBanner.remove();
  if (isAdaptiveMode && !isFallback) {
    const wrapEl = document.getElementById("top-jobs-wrap");
    if (wrapEl) {
      const ab = document.createElement("div");
      ab.id = "top-jobs-adaptive-banner";
      ab.className = "mb-2 px-3 py-1.5 rounded border border-Camber/30 bg-Camber/5 text-Camber text-[10px]";
      ab.textContent = "⚠ Adaptive baseline mode — regressions are vs. historical run patterns, not contracted SLA. Upload BatchSLA_info.xlsx to activate contract targets.";
      wrapEl.insertAdjacentElement("beforebegin", ab);
    }
  }

  empty?.classList.add("hidden");
  wrap?.classList.remove("hidden");

  // Detect if any job has a real SLA matrix contract — drives dynamic column
  const hasSlaMatrix = _isCustomerSlaType(window.appData?.batch?.sla_source?.type)
    || rows.some(r => r.sla_source === "sla_matrix" || r.sla_contract_type === "JOB_SPECIFIC");

  // Dynamically add/remove SLA / BASELINE column header
  // — In adaptive mode: show "BASELINE" column with per-job adaptive ceiling
  // — In SLA matrix mode: show "SLA" column with contracted ceiling
  const slaColTh = document.getElementById("top-jobs-th-sla");
  const showBaselineCol = isAdaptiveMode;  // always show in adaptive mode so the comparison is visible
  const showSlaCol      = hasSlaMatrix && !isAdaptiveMode;
  const tableHead = tbody?.closest("table")?.querySelector("thead tr");
  if (tableHead) {
    if ((showSlaCol || showBaselineCol) && !slaColTh) {
      const avgTh = tableHead.querySelectorAll("th")[2];
      if (avgTh) {
        const th = document.createElement("th");
        th.id = "top-jobs-th-sla";
        th.className = "px-2 py-2 font-semibold uppercase tracking-wider text-[10px] text-center";
        if (showBaselineCol) {
          th.title = "Adaptive baseline — computed from this job's own 28-day run history (p95/p90/peak·0.9 by sample count)";
          th.textContent = "BASELINE";
        } else {
          th.title = "SLA ceiling per job — 📋 = from SLA matrix contract · ⚙ = schedule default";
          th.textContent = "SLA";
        }
        avgTh.insertAdjacentElement("afterend", th);
      }
    } else if (!showSlaCol && !showBaselineCol && slaColTh) {
      slaColTh.remove();
    }
  }

  const statusClass = (status) => {
    switch ((status || "").toUpperCase()) {
      case "BREACH":               return "metric-badge metric-badge-red";
      case "ADAPTIVE_REGRESSION":  return "metric-badge metric-badge-amber";
      case "AT_RISK":              return "metric-badge metric-badge-amber";
      case "CRITICAL":             return "metric-badge metric-badge-amber";
      case "CAUTION":              return "metric-badge metric-badge-amber";
      case "LONG_JOB":             return "metric-badge metric-badge-blue";
      case "HEALTHY":              return "metric-badge metric-badge-green";
      case "EXCELLENT":            return "metric-badge metric-badge-green";
      case "OK":                   return "metric-badge metric-badge-green";
      default:                     return "metric-badge metric-badge-blue";
    }
  };

  for (const row of displayRows) {
    const tr = document.createElement("tr");
    tr.className = "hover:bg-Cblue/5 transition-colors";

    const bufPct  = typeof row.buffer_pct   === "number" ? row.buffer_pct   : null;
    const slaUsed = typeof row.sla_used_pct === "number" ? row.sla_used_pct : null;
    const peak    = typeof row.peak_hrs     === "number" ? row.peak_hrs     : 0;
    const avg     = typeof row.avg_hrs      === "number" ? row.avg_hrs      : 0;
    const status  = row.buffer_status || (bufPct < 0 ? "BREACH" : "HEALTHY");
    const jobName = row.Job_Name || row.job_name || "—";

    const bufferClass =
      bufPct === null   ? "text-Cmuted"  :
      bufPct < 0        ? "text-Cred font-bold"   :
      bufPct < 10       ? "text-Camber font-bold"  :
      bufPct < 30       ? "text-Camber" : "text-Cgreen";

    const jobSla = row.sla_hrs ?? slaCeiling;
    // In adaptive mode: show actual overflow % (e.g., "112%") not capped 100%
    // so severity is visible (a job at 130% of baseline is worse than one at 101%)
    const rawSlaBarPct = slaUsed ?? (peak / jobSla * 100);
    const slaBarPct    = isAdaptiveMode ? rawSlaBarPct : Math.min(100, rawSlaBarPct);
    const slaBarColor  = rawSlaBarPct >= 100 ? "#f43f5e" : rawSlaBarPct >= 80 ? "#f59e0b" : "#3b82f6";

    // Buffer display: show buffer% when available, else compute from peak/SLA
    let bufferDisplay = "—";
    if (bufPct !== null) {
      bufferDisplay = (bufPct >= 0 ? "+" : "") + bufPct.toFixed(1) + "%";
    } else if (peak > 0 && jobSla > 0) {
      const computedBuf = ((jobSla - peak) / jobSla) * 100;
      bufferDisplay = (computedBuf >= 0 ? "+" : "") + computedBuf.toFixed(1) + "%";
    }

    // BASELINE column (adaptive mode) — shows per-job adaptive ceiling with quality badge
    // SLA column (matrix mode) — shows contracted ceiling with source icon
    const rowSlaSource = row.sla_source || (hasSlaMatrix ? "default" : null);
    const isJobMatrix  = rowSlaSource === "sla_matrix" || row.sla_contract_type === "JOB_SPECIFIC";
    const slaCtx = `title="Adaptive baseline: ${jobSla.toFixed(2)}h — peak compared against this"`;

    // Baseline quality badge (STRONG/MODERATE/WEAK) from PATH C computation
    const bq = (row.baseline_quality || "").toUpperCase();
    const bqColor = bq === "STRONG" ? "text-Cgreen" : bq === "MODERATE" ? "text-Cblue" : bq === "WEAK" ? "text-Camber" : "text-Cmuted";
    const bqTitle = {
      STRONG: "STRONG — ≥14 historical runs, p95 baseline",
      MODERATE: "MODERATE — 7–13 runs, p90 baseline",
      WEAK: "WEAK — 3–6 runs, conservative estimate",
      CONTRACTED: "CONTRACTED — from SLA matrix",
      INSUFFICIENT: "INSUFFICIENT — <3 runs, excluded from compliance"
    }[bq] || bq;

    let slaCeilCell = "";
    if (showBaselineCol) {
      // Adaptive baseline column: show per-job ceiling and quality confidence
      slaCeilCell = `<td class="px-2 py-2 text-center text-[10px]" title="${bqTitle}">
           <span class="font-mono text-Cwhite">${jobSla.toFixed(2)}h</span>
           ${bq && bq !== "CONTRACTED" ? `<span class="ml-1 ${bqColor} opacity-70">${bq.slice(0,3)}</span>` : ""}
         </td>`;
    } else if (showSlaCol) {
      const slaSourceIcon = isJobMatrix ? "📋" : "⚙";
      const slaSourceTitle = isJobMatrix
        ? `Contract SLA: ${jobSla.toFixed(1)}h (from SLA matrix — job-specific)`
        : `Default SLA: ${jobSla.toFixed(1)}h (schedule default)`;
      slaCeilCell = `<td class="px-2 py-2 text-center text-[10px]" title="${slaSourceTitle}">
           <span class="${isJobMatrix ? "text-Cgreen" : "text-Camber opacity-60"}">${slaSourceIcon}</span>
           <span class="ml-1 font-mono text-Cmuted">${jobSla.toFixed(1)}h</span>
         </td>`;
    }

    // SLA USED bar: in adaptive mode cap bar display at 200% max width for very large overflows
    const barWidth = isAdaptiveMode ? Math.min(100, (rawSlaBarPct / 2)).toFixed(0) : Math.min(100, rawSlaBarPct).toFixed(0);
    const barTitle = isAdaptiveMode ? `${rawSlaBarPct.toFixed(0)}% of adaptive baseline` : `${Math.min(100, rawSlaBarPct).toFixed(0)}% of SLA`;

    tr.innerHTML = `
      <td class="px-3 py-2 font-mono text-Cwhite text-[11px]">
        <div class="truncate max-w-[150px]" title="${escapeHtml(jobName)}">${escapeHtml(jobName)}</div>
      </td>
      <td class="px-3 py-2 text-right font-mono font-bold text-Cwhite text-[11px]" ${isAdaptiveMode ? slaCtx : ""}>
        ${peak.toFixed(2)}h
        ${peak > jobSla ? `<span class="ml-1 text-[9px] font-bold" style="color:${isAdaptiveMode ? THEME.amber : THEME.red}">${isAdaptiveMode ? "▲BASE" : "▲SLA"}</span>` : ""}
      </td>
      <td class="px-3 py-2 text-right font-mono text-Cmuted text-[11px]">${avg.toFixed(2)}h</td>
      ${slaCeilCell}
      <td class="px-3 py-2 text-right font-mono text-[11px] ${bufferClass}">
        ${bufferDisplay}
      </td>
      <td class="px-3 py-2 text-right text-[11px]">
        <div class="flex items-center justify-end gap-1.5" title="${barTitle}">
          <div class="pe-progress-track w-14">
            <div class="pe-progress-fill" style="width:${barWidth}%;background:${slaBarColor}"></div>
          </div>
          <span class="font-mono text-[10px] text-Cmuted">${rawSlaBarPct.toFixed(0)}%</span>
        </div>
      </td>
      <td class="px-3 py-2">
        <span class="${statusClass(isAdaptiveMode && status === 'BREACH' ? 'ADAPTIVE_REGRESSION' : status)}">${escapeHtml(isAdaptiveMode && status === "BREACH" ? "REGRESSION" : status)}</span>
      </td>
      <td class="px-1 py-2 text-center">
        <button class="batch-exclude-btn text-[11px] px-1 py-0.5 rounded opacity-40 hover:opacity-100 transition cursor-pointer"
                style="color:${THEME.amber};background:${hexA(THEME.amber,0.08)};border:1px solid transparent"
                data-exclude-job="${escapeHtml(jobName)}"
                title="Exclude '${escapeHtml(jobName)}' from analysis">⊘</button>
      </td>
    `;
    // Wire exclude button
    tr.querySelector(".batch-exclude-btn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      _batchManualExclude.add(jobName);
      _batchManualInclude.delete(jobName);
      _reRenderBatch();
    });
    tbody.appendChild(tr);
  }

  // ── Contextual SLA summary line under the table ──
  const existingSummary = document.getElementById("top-jobs-sla-summary");
  if (existingSummary) existingSummary.remove();
  const wrapEl = document.getElementById("top-jobs-wrap");
  if (wrapEl && displayRows.length > 0) {
    const summaryDiv = document.createElement("div");
    summaryDiv.id = "top-jobs-sla-summary";
    summaryDiv.className = "flex items-center gap-3 flex-wrap text-[9px] text-Cmuted mt-2 px-1";
    const parts = [];
    if (isAdaptiveMode) {
      // In adaptive mode the per-job baseline is what matters — showing global cap
      // as "SLA ceiling" confuses users who see e.g. 0.63h peak vs 6.0h cap
      parts.push(`Mode: <span class="font-bold text-Camber">adaptive per-job baselines</span>`);
      parts.push(`Global cap: <span class="font-bold text-Cwhite">${slaCeiling.toFixed(1)}h</span>`);
    } else {
      parts.push(`SLA ceiling: <span class="font-bold text-Cwhite">${slaCeiling.toFixed(1)}h</span>`);
    }
    if (dataSpanDays > 0) parts.push(`Observation: <span class="font-bold text-Cwhite">${dataSpanDays}d</span>`);
    if (isFallback && displayRows.length > 0) {
      const avgBuf = displayRows.reduce((s, r) => {
        const b = typeof r.buffer_pct === "number" ? r.buffer_pct : ((slaCeiling - (r.peak_hrs || 0)) / slaCeiling * 100);
        return s + b;
      }, 0) / displayRows.length;
      parts.push(`Avg buffer (top ${displayRows.length}): <span class="font-bold text-Cgreen">${avgBuf.toFixed(1)}%</span>`);
    }
    const slaSource = batchKpis.sla_source || "";
    if (slaSource) parts.push(`Source: ${slaSource}`);
    summaryDiv.innerHTML = parts.join(' <span class="text-Cborder">·</span> ');
    wrapEl.insertAdjacentElement("afterend", summaryDiv);
  }
}


// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────
function destroyChart(key) {
  if (charts[key]) {
    try { charts[key].destroy(); } catch { /* noop */ }
    charts[key] = null;
  }
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}


// ════════════════════════════════════════════════════════════
//  PHASE 4 · RESOURCE REVIEW
//  - POST /api/process-resource (auto-fired after upload)
//  - 6 KPI cards + Z-score anomaly strip
//  - Chart.js horizontal grouped bar (CPU/Mem/Disk top servers)
//  - HTML/CSS metric heatmap (no chartjs-chart-matrix dep)
//  - Lazy server detail table with filter + show-all toggle
// ════════════════════════════════════════════════════════════

// Thresholds aligned with services/resource_calculator.py constants
const RESOURCE_THRESHOLDS = {
  cpu_ok:   75,  cpu_warn:  90,   // Warning: 75%, Critical: 90%
  mem_ok:   75,  mem_warn:  90,   // Warning: 75%, Critical: 90%
  disk_ok:  75,  disk_warn: 90,   // Warning: 75%, Critical: 90%
  db_mem_band_low:  80,  db_mem_band_high: 92,  // DB expected band
  db_mem_warn: 93, db_mem_crit: 95,
};

// ── DB memory awareness ──────────────────────────────────────
// Oracle/SQL DB servers pre-allocate SGA/PGA memory — steady 80-92% used
// is normal and should not alarm users. These helpers rewrite labels.
function _isDbRole(role) {
  return /\bDB\b/i.test(role || "");
}
function _isDbMemExpected(role, memPct, thresholds) {
  const t = thresholds || RESOURCE_THRESHOLDS;
  return _isDbRole(role)
    && memPct >= (t.db_mem_band_low || 80)
    && memPct <= (t.db_mem_band_high || 92);
}
function _dbMemLabel(memPct, thresholds) {
  const t = thresholds || RESOURCE_THRESHOLDS;
  const hi = t.db_mem_band_high || 92;
  return `${memPct.toFixed(1)}% used — within DB expected range (${t.db_mem_band_low || 80}–${hi}%)`;
}
function _rewriteWaveformForDb(wf, role, memUsed, thresholds) {
  // Rewrite alarming waveform labels for DB servers in expected band
  if (!_isDbMemExpected(role, memUsed, thresholds) || !wf) return wf;
  const shape = wf.shape || "";
  const alarmingShapes = ["plateau", "flat_high"];
  if (!alarmingShapes.includes(shape)) return wf;
  const t = thresholds || RESOURCE_THRESHOLDS;
  return {
    ...wf,
    label: "Expected DB Load",
    icon: "🗄️",
    risk: "low",
    meaning: `Memory at ${memUsed.toFixed(0)}% — normal for DB workload. Oracle SGA/PGA keeps memory intentionally high.`,
    action: `No action needed. Monitor for growth above ${t.db_mem_warn || 93}%.`,
  };
}

// ── DB-expected memory: one shared cross-panel semantic ──────────
// A DB server pre-allocating 80–92% RAM to SGA/PGA (8–20% available) is working
// as designed — not under pressure. We render that state in DB-identity purple
// (the same hue as the DB role badges) so the status badge, heatmap memory bar,
// table memory column, and anomaly cards all agree, instead of one panel saying
// "expected" while another screams amber/red.
const DB_EXPECTED_COLOR = THEME.purple;            // #a855f7 — matches DB role badges
const DB_EXPECTED_GRAD  = ["#7c3aed", "#a855f7"];  // deep→bright violet (bar/gauge fill)

// Authoritative display status for a resource row. The backend is the source of
// truth for Warning/Critical — it already flips a DB server's memory-only
// Warning to Healthy when CPU and disk are fine. So we only PROMOTE an
// already-cleared (Healthy) DB server in the expected band to a distinct
// "DB Normal" badge; we never downgrade a real Warning/Critical, which would
// hide a genuine CPU or disk problem.
function _resourceDisplayStatus(s) {
  if (!s) return "Unknown";
  const dbNormal = (s.mem_status === "DB_NORMAL") || _isDbMemExpected(s.type, s.mem_pct);
  return (dbNormal && s.status === "Healthy") ? "DB Normal" : (s.status || "Unknown");
}

const GRADE_COLORS = {
  A: THEME.green,
  B: THEME.cyan,
  C: THEME.amber,
  D: "#fb923c",
  F: THEME.red,
};

const STATUS_COLORS = {
  Critical: THEME.red,
  Warning:  THEME.amber,
  Healthy:  THEME.green,
  "DB Normal": DB_EXPECTED_COLOR,   // DB in expected SGA/PGA band — cleared by backend
  Unknown:  THEME.muted,
};

function metricColor(val, ok, warn) {
  if (val >= warn) return THEME.red;
  if (val >= ok)   return THEME.amber;
  return THEME.green;
}

// ── Wiring ────────────────────────────────────────────────────
function initResourceView() {
  const toggle = document.getElementById("resource-table-toggle");
  toggle?.addEventListener("click", () => {
    resourceTableState.showAll = !resourceTableState.showAll;
    toggle.textContent = resourceTableState.showAll ? "Show preview" : "Show all";
    if (window.appData.resource) {
      renderResourceTable(window.appData.resource.servers);
    }
  });

  let _resourceSearchDebounce = null;
  const search = document.getElementById("resource-table-search");
  search?.addEventListener("input", (e) => {
    resourceTableState.filter = (e.target.value || "").trim().toLowerCase();
    _updateClearButton();
    if (_resourceSearchDebounce) clearTimeout(_resourceSearchDebounce);
    _resourceSearchDebounce = setTimeout(() => {
      if (window.appData.resource) renderResourceTable(window.appData.resource.servers);
    }, 150);
  });
  search?.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      search.value = "";
      resourceTableState.filter = "";
      _updateClearButton();
      if (window.appData.resource) renderResourceTable(window.appData.resource.servers);
    }
  });

  // Dropdown filters: Type, Env, Status
  for (const [id, key] of [["resource-filter-type", "filterType"], ["resource-filter-env", "filterEnv"], ["resource-filter-status", "filterStatus"]]) {
    const sel = document.getElementById(id);
    sel?.addEventListener("change", () => {
      resourceTableState[key] = sel.value;
      _updateClearButton();
      if (window.appData.resource) renderResourceTable(window.appData.resource.servers);
    });
  }

  // Clear all filters
  const clearBtn = document.getElementById("resource-clear-filters");
  clearBtn?.addEventListener("click", () => {
    resourceTableState.filter = "";
    resourceTableState.filterType = "";
    resourceTableState.filterEnv = "";
    resourceTableState.filterStatus = "";
    if (search) search.value = "";
    document.getElementById("resource-filter-type").value = "";
    document.getElementById("resource-filter-env").value = "";
    document.getElementById("resource-filter-status").value = "";
    _updateClearButton();
    if (window.appData.resource) renderResourceTable(window.appData.resource.servers);
  });

  // Sortable column headers
  const thead = document.getElementById("resource-thead-row");
  thead?.addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (!th) return;
    const key = th.dataset.sort;
    if (resourceTableState.sortKey === key) {
      resourceTableState.sortDir *= -1;
    } else {
      resourceTableState.sortKey = key;
      resourceTableState.sortDir = -1; // desc by default for metrics
    }
    _updateSortArrows();
    if (window.appData.resource) renderResourceTable(window.appData.resource.servers);
  });
}

function _updateClearButton() {
  const btn = document.getElementById("resource-clear-filters");
  if (!btn) return;
  const hasFilters = resourceTableState.filter || resourceTableState.filterType || resourceTableState.filterEnv || resourceTableState.filterStatus;
  btn.classList.toggle("hidden", !hasFilters);
}

function _updateSortArrows() {
  const thead = document.getElementById("resource-thead-row");
  if (!thead) return;
  thead.querySelectorAll("th[data-sort]").forEach(th => {
    const arrow = th.querySelector(".sort-arrow");
    if (!arrow) return;
    if (th.dataset.sort === resourceTableState.sortKey) {
      arrow.textContent = resourceTableState.sortDir > 0 ? "▲" : "▼";
      arrow.style.color = THEME.blue;
    } else {
      arrow.textContent = "";
    }
  });
}

// ── GAP 2: Heatmap → table filter bridge ──────────────────────
function filterServerTable(serverName) {
  const search = document.getElementById("resource-table-search");
  if (search) {
    search.value = serverName;
    search.dispatchEvent(new Event("input"));
    // Scroll table into view
    const table = document.getElementById("resource-detail-table");
    if (table) table.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

// ── Pipeline ──────────────────────────────────────────────────
async function processResourceServers(servers) {
  try {
    const res = await fetch("/api/process-resource", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ servers }),
    });
    const text = await res.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text || "Server returned a non-JSON response" };
    }
    if (!res.ok) {
      const detail = payload?.detail || `HTTP ${res.status}`;
      toast("error", "Resource processing failed", detail);
      console.error("[pe-dashboard] /api/process-resource failed", payload);
      return;
    }

    window.appData.resource = payload;
    window._execCache = null; // invalidate exec dashboard cache
    _markSessionActive();  // track session boundary
    renderResourceReview(payload);

    // Defer cascade API calls — let the browser paint resource KPIs first
    // before firing network requests that trigger more rendering.
    // triggerPeConsultant and triggerPeNarrative already have debounces
    // (600ms / 500ms) and are also fired from triggerGenerateFindings
    // completion, so a small delay here avoids main-thread contention.
    setTimeout(() => {
      triggerPeConsultant().catch(() => {});
      triggerPeNarrative().catch(() => {});
      triggerGenerateFindings().catch(() => {});
      refreshAuditContext().catch(() => {});
    }, 100);

    const k = payload.kpis || {};

    // Fleet grade N/A warning
    const gradeLabel = k.fleet_grade === "N/A"
      ? `Grade N/A — Resource data insufficient (${k.known_pct || 0}% known)`
      : `Grade ${k.fleet_grade || "?"} · ${k.known_servers}/${k.total_servers} servers scored`;

    toast(
      k.fleet_grade === "N/A" ? "warning" : "info",
      "Fleet Intelligence ready",
      gradeLabel,
      3500
    );
    console.info("[pe-dashboard] window.appData.resource updated", payload);
  } catch (err) {
    _handleFetchError(err, "resource");
  }
}

// ── Top-level renderer ────────────────────────────────────────
function renderResourceReview(data) {
  if (!data) return;

  document.getElementById("resource-empty")?.classList.add("hidden");
  document.getElementById("resource-review-body")?.classList.remove("hidden");

  // Show duration picker when data is from Azure Monitor
  const durPicker = document.getElementById("resource-duration-picker");
  if (durPicker) {
    const isAzure = (data.servers || []).some(s => s.source === "azure_monitor");
    if (isAzure) {
      durPicker.classList.remove("hidden");
      // Restore _lastFetchedVmIds if empty (page refresh scenario)
      if (!_lastFetchedVmIds.length) {
        _lastFetchedVmIds = (data.servers || [])
          .filter(s => s.resource_id)
          .map(s => s.resource_id);
      }
    }
  }

  // Vision AI confidence indicator — show when data came from image parsing
  const visionBanner = document.getElementById("resource-vision-banner");
  if (visionBanner) {
    const imageOnlyServers = (data.servers || []).filter(s => s.image_only);
    if (imageOnlyServers.length > 0) {
      visionBanner.classList.remove("hidden");
      const label = visionBanner.querySelector("[data-vision-label]");
      if (label) label.textContent =
        `${imageOnlyServers.length}/${data.servers.length} server(s) parsed via Vision AI from embedded images — values carry ±5-10% accuracy. Verify critical readings manually.`;
    } else {
      visionBanner.classList.add("hidden");
    }
  }

  // ── Phase 1: instant — lightweight KPI DOM writes (fast first paint)
  renderResourceKpis(data.kpis || {});
  _renderPriorityAction(data);

  // ── Phase 2: deferred — heavy renders staggered across frames
  //    Prevents Firefox "this page is slowing down" warning by yielding
  //    to the browser between Chart.js creation, heatmap, and table builds.
  const _deferredResourceRenders = [
    () => renderResourceExecutiveSummary(data.executive_summary || null),
    () => renderResourceAnomalies(data.anomalies || []),
    () => renderResourceBarChart(data.servers || []),
    () => renderResourceHeatmap(data.servers || []),
    () => renderResourceTable(data.servers || []),
    () => {
      // Show Metrics Deep Dive card when Azure data is present
      const ddCard = document.getElementById("resource-deepdive-card");
      if (ddCard) {
        const isAzure = (data.servers || []).some(s => s.source === "azure_monitor");
        if (isAzure) ddCard.classList.remove("hidden");
        else ddCard.classList.add("hidden");
      }
    },
  ];
  let _ri = 0;
  function _nextResourceRender() {
    if (_ri < _deferredResourceRenders.length) {
      _deferredResourceRenders[_ri++]();
      requestAnimationFrame(_nextResourceRender);
    }
  }
  requestAnimationFrame(_nextResourceRender);
}

// ── E1: Highest Priority Action — single "start here" signal ──
function _renderPriorityAction(data) {
  const banner = document.getElementById("priority-action-banner");
  const titleEl = document.getElementById("priority-action-title");
  const detailEl = document.getElementById("priority-action-detail");
  if (!banner || !titleEl || !detailEl) return;

  const servers = (data.servers || []).filter(s => !s.image_only && (s.cpu_pct > 0 || s.mem_pct > 0));
  if (!servers.length) { banner.classList.add("hidden"); return; }

  // Find the worst server — highest single metric breach
  // Memory: convert from used% (backend) to pressure score for comparison
  let worst = null, worstMetric = "", worstVal = 0, worstIssue = "", worstMemAvail = 0;
  for (const s of servers) {
    const memUsed = s.mem_pct || 0;
    const memAvailPct = 100 - memUsed;
    const cpuUsed = s.cpu_pct || 0;
    const diskUsed = s.disk_pct || 0;
    const candidates = [
      { metric: "MEM", val: memUsed, displayVal: memAvailPct, threshold: 80, action: "investigate SGA/PGA allocation or VM sizing" },
      { metric: "CPU", val: cpuUsed, displayVal: cpuUsed, threshold: 80, action: "check runaway SQL, parallel degree, or vCPU sizing" },
      { metric: "DISK", val: diskUsed, displayVal: diskUsed, threshold: 85, action: "review I/O-bound queries or storage throughput limits" },
    ];
    for (const c of candidates) {
      if (c.val > worstVal) {
        worst = s; worstMetric = c.metric; worstVal = c.val; worstIssue = c.action; worstMemAvail = memAvailPct;
      }
    }
  }

  if (!worst || worstVal < 70) { banner.classList.add("hidden"); return; }

  const name = (worst.server || "").split(".")[0];
  const status = worst.status || "unknown";
  const wEnv = worst.environment || "";
  const wType = worst.type || "";
  const envTag = wEnv ? ` [${wEnv}]` : "";
  const t = window.appData?.resource?.kpis?.thresholds || RESOURCE_THRESHOLDS;

  // DB servers with high memory in expected band → informational, not alarming
  const isDbExp = _isDbMemExpected(wType, worstVal, t) && worstMetric === "MEM" && (worst.cpu_pct || 0) < 20;
  const sev = isDbExp ? THEME.cyan : (worstVal >= 90 ? THEME.red : worstVal >= 80 ? THEME.amber : THEME.blue);
  const displayVal = worstMetric === "MEM" ? worstMemAvail : worstVal;
  const displaySuffix = worstMetric === "MEM" ? "avail" : "";
  const actionText = isDbExp
    ? `Memory ${worstMemAvail.toFixed(0)}% available — expected for DB workload (SGA/PGA allocation). Monitor for drops below ${100 - (t.db_mem_warn || 93)}%.`
    : `Highest priority: ${worstIssue} before next batch window. This server is the fleet's single biggest risk.`;
  const titleSuffix = isDbExp ? "EXPECTED DB LOAD" : status.toUpperCase();

  banner.classList.remove("hidden");
  banner.style.borderColor = hexA(sev, 0.5);
  banner.style.background = hexA(sev, 0.06);
  banner.style.setProperty("--alert-color", sev);
  titleEl.style.color = sev;
  titleEl.textContent = `${name} (${wType}${envTag}) — ${worstMetric} ${displayVal.toFixed(0)}% ${displaySuffix} (${titleSuffix})`;
  detailEl.textContent = actionText;
}

// ── E1 upgrade: refine priority action with deep dive spike data ──
function _updatePriorityFromDeepDive(vms) {
  const banner = document.getElementById("priority-action-banner");
  const titleEl = document.getElementById("priority-action-title");
  const detailEl = document.getElementById("priority-action-detail");
  if (!banner || !titleEl || !detailEl || !vms) return;

  let worst = null, worstScore = 0;
  for (const [vmName, vmData] of Object.entries(vms)) {
    const sp = vmData.spikes || {};
    const st = vmData.stats || {};
    let spikeCount = 0;
    for (const arr of Object.values(sp)) spikeCount += arr.length;
    if (!spikeCount) continue;
    const memAvailMin = st["Available Memory Percentage"]?.min ?? 100;
    const memPressure = 100 - memAvailMin;  // convert to pressure score for comparison
    const cpuMax = st["Percentage CPU"]?.max ?? 0;
    const score = Math.max(memPressure, cpuMax) + spikeCount * 5;
    if (score > worstScore) {
      worstScore = score;
      const domMetric = memPressure >= cpuMax ? "MEM" : "CPU";
      const domVal = domMetric === "MEM" ? memAvailMin : cpuMax;
      worst = { vmName, domMetric, domVal, spikeCount, memAvailMin, memPressure, cpuMax };
    }
  }

  if (!worst) return;

  const name = worst.vmName.split(".")[0];
  const role = _inferRole(worst.vmName);
  const env = _inferEnv(worst.vmName);
  const envSuffix = env ? ` [${env}]` : "";
  const t = window.appData?.resource?.kpis?.thresholds || RESOURCE_THRESHOLDS;

  // DB-aware: if worst metric is MEM and server is DB in expected band, soften
  // _isDbMemExpected expects used%, so pass 100 - available
  const isDbExp = worst.domMetric === "MEM" && _isDbMemExpected(role, worst.memPressure, t) && worst.cpuMax < 20;
  const sev = isDbExp ? THEME.cyan : (worst.domMetric === "MEM" ? (worst.domVal <= 10 ? THEME.red : worst.domVal <= 25 ? THEME.amber : THEME.blue) : (worst.domVal >= 90 ? THEME.red : worst.domVal >= 80 ? THEME.amber : THEME.blue));
  const action = isDbExp
    ? `Memory ${worst.domVal.toFixed(0)}% available — expected DB allocation (SGA/PGA). Monitor for drops below ${100 - (t.db_mem_warn || 93)}%.`
    : worst.domMetric === "MEM"
    ? `Highest priority: investigate SGA/PGA allocation or VM sizing before next batch window. This is the fleet's single biggest risk right now.`
    : `Highest priority: check runaway SQL, parallel degree, or vCPU sizing before next batch window. This is the fleet's single biggest risk right now.`;
  const titleSuffix = isDbExp ? "EXPECTED DB LOAD" : `${worst.spikeCount} anomal${worst.spikeCount > 1 ? "ies" : "y"} in last ${_deepDiveHoursBack}h`;

  banner.classList.remove("hidden");
  banner.style.borderColor = hexA(sev, 0.5);
  banner.style.background = hexA(sev, 0.06);
  banner.style.setProperty("--alert-color", sev);
  titleEl.style.color = sev;
  // CPU domVal = peak used %; MEM domVal = available %; DISK domVal = I/O consumed %
  const _metricSfx = worst.domMetric === "MEM" ? "% avail" : worst.domMetric === "CPU" ? "% peak" : "% I/O";
  // "as of" — find the most recent spike for the dominant metric so the banner
  // reflects when that peak actually occurred, not just that it was in the window.
  const _domMetricKey = worst.domMetric === "CPU" ? "Percentage CPU"
    : worst.domMetric === "MEM" ? "Available Memory Percentage"
    : "OS Disk Bandwidth Consumed Percentage";
  const _domSpikes = (vms[worst.vmName]?.spikes || {})[_domMetricKey] || [];
  let _peakAsOf = "";
  if (_domSpikes.length) {
    const _latestSpike = _domSpikes.reduce((a, b) =>
      new Date(b.peak_time) > new Date(a.peak_time) ? b : a
    );
    _peakAsOf = ` · last seen ${new Date(_latestSpike.peak_time).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
    })} UTC`;
  } else {
    // No flagged spike — value is a period stat, not a pinpoint event
    _peakAsOf = ` · ${_deepDiveHoursBack}h window max`;
  }
  titleEl.textContent = `${name} (${role}${envSuffix}) — ${worst.domMetric} ${worst.domVal.toFixed(0)}${_metricSfx}${_peakAsOf}, ${titleSuffix}`;
  detailEl.textContent = action;
}

// ── KPI cards ─────────────────────────────────────────────────
let _scoreDecomp = null;  // stored for grade drill-down

function renderResourceKpis(k) {
  setText("rk-servers", String(k.total_servers ?? 0));

  // Build server type/environment pills
  const subEl = document.getElementById("rk-servers-sub");
  if (subEl) {
    const pills = [];
    const pill = (label, color) =>
      `<span class="text-[8px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded-md" style="color:${color};background:${hexA(color,.12)};border:1px solid ${hexA(color,.25)}">${label}</span>`;
    if (k.n_app)  pills.push(pill(`${k.n_app} APP`, THEME.blue));
    if (k.n_db)   pills.push(pill(`${k.n_db} DB`, '#a855f7'));
    if (k.n_sre)  pills.push(pill(`${k.n_sre} SRE`, THEME.cyan));
    if (k.n_prod) pills.push(pill(`${k.n_prod} PROD`, THEME.red));
    if (k.n_test) pills.push(pill(`${k.n_test} TEST`, THEME.amber));
    if (k.n_dev)  pills.push(pill(`${k.n_dev} DEV`, THEME.cyan));
    subEl.innerHTML = pills.length ? pills.join('') : `<span class="text-[9px] text-Cmuted">No servers</span>`;
  }

  // Live badge for Azure Monitor
  const liveBadge = document.getElementById("rk-live-badge");
  if (liveBadge) {
    const isAzure = (window.appData?.resource?.servers || []).some(s => s.source === "azure_monitor");
    liveBadge.classList.toggle("hidden", !isAzure);
  }

  const grade = k.fleet_grade || "?";
  const gradeEl = document.getElementById("rk-grade");
  if (gradeEl) {
    if (grade === "N/A") {
      gradeEl.textContent = "N/A";
      gradeEl.style.color = THEME.amber;
      gradeEl.classList.remove("rk-grade-glow");
    } else {
      gradeEl.textContent = grade;
      // Strip asterisk for color lookup (B* → B)
      const baseGrade = grade.replace("*", "");
      gradeEl.style.color = GRADE_COLORS[baseGrade] || THEME.muted;
      gradeEl.classList.add("rk-grade-glow");
    }
  }
  if (grade === "N/A") {
    setText("rk-grade-sub", "Resource data required");
  } else {
    const scoreText = `Score ${(k.fleet_score ?? 0).toFixed(1)}/100`;
    const caveat = k.small_sample ? " (limited sample)" : "";
    setText("rk-grade-sub", scoreText + caveat);
  }

  // Grade change indicator (from previous session)
  const changeEl = document.getElementById("rk-grade-change");
  if (changeEl && k._prev_grade && k._prev_grade !== grade && grade !== "N/A") {
    const dir = (GRADE_COLORS[grade] === THEME.green || grade < k._prev_grade) ? "↑" : "↓";
    const chColor = dir === "↑" ? THEME.green : THEME.red;
    changeEl.innerHTML = `<span style="color:${chColor}">${dir} FROM ${k._prev_grade}</span>`;
    changeEl.classList.remove("hidden");
  }

  // Store decomposition for drill-down
  _scoreDecomp = k.score_decomposition || null;

  const t = k.thresholds || RESOURCE_THRESHOLDS;
  // No server reported parseable metrics (e.g. image-only DOCX). Rings must show
  // "—/No data" rather than a misleading 0% "OK" green that reads as measured-idle.
  const noResData = (k.known_servers ?? 0) === 0;

  // Animated SVG ring gauges (r=29, circ=2πr=182.21)
  const animateRing = (svgId, val, ok, warn, statusId, ringLabelId, invertColor = false) => {
    const svg = document.getElementById(svgId);
    if (!svg) return;
    if (noResData) {
      const ring = svg.querySelector(".rk-ring");
      if (ring) { ring.setAttribute("stroke", THEME.muted); ring.style.strokeDashoffset = (2 * Math.PI * 29).toFixed(2); }
      const rlEl = document.getElementById(ringLabelId);
      if (rlEl) { rlEl.textContent = "—"; rlEl.style.color = THEME.muted; }
      const statusEl = document.getElementById(statusId);
      if (statusEl) {
        statusEl.innerHTML = `<span style="color:${THEME.muted};background:${hexA(THEME.muted,.12)};border:1px solid ${hexA(THEME.muted,.3)}" class="rk-status-pill">NO DATA</span>`;
        statusEl.classList.remove("hidden");
      }
      return;
    }
    const v = Math.max(0, Math.min(100, Number(val) || 0));
    const ring = svg.querySelector(".rk-ring");
    const tick = svg.querySelector(".rk-tick");
    const circ = 2 * Math.PI * 29; // r=29

    // Color by zone
    let color;
    if (invertColor) {
      // For Available % — lower = worse
      if (v <= warn)        color = "#ef4444";
      else if (v <= ok)     color = "#f59e0b";
      else                  color = "#10b981";
    } else {
      if (v >= warn)        color = "#ef4444";
      else if (v >= ok)     color = "#f59e0b";
      else                  color = "#10b981";
    }
    ring.setAttribute("stroke", color);
    ring.setAttribute("stroke-dasharray", circ.toFixed(2));

    // Animate fill — set dashoffset to represent value
    const offset = circ * (1 - v / 100);
    requestAnimationFrame(() => { ring.style.strokeDashoffset = offset; });

    // Threshold tick
    if (tick) {
      const threshVal = invertColor ? warn : warn;
      const angle = (threshVal / 100) * 360;
      tick.setAttribute("transform", `rotate(${angle} 36 36)`);
      tick.setAttribute("opacity", "1");
    }

    // Ring center label (percentage inside ring)
    const rlEl = document.getElementById(ringLabelId);
    if (rlEl) {
      rlEl.textContent = `${v.toFixed(0)}%`;
      rlEl.style.color = color;
    }

    // Status pill below value
    const statusEl = document.getElementById(statusId);
    if (statusEl) {
      let label, pillColor;
      if (invertColor) {
        if (v <= warn)       { label = "WARN"; pillColor = "#ef4444"; }
        else if (v <= ok)    { label = "OK"; pillColor = "#f59e0b"; }
        else                 { label = "OK"; pillColor = "#10b981"; }
      } else {
        if (v >= warn)       { label = "WARN"; pillColor = "#ef4444"; }
        else if (v >= ok)    { label = "OK"; pillColor = "#f59e0b"; }
        else                 { label = "OK"; pillColor = "#10b981"; }
      }
      statusEl.innerHTML = `<span style="color:${pillColor};background:${hexA(pillColor,.12)};border:1px solid ${hexA(pillColor,.3)}" class="rk-status-pill">${label}</span>`;
      statusEl.classList.remove("hidden");
    }
  };

  const setMetric = (id, val, ok, warn) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (noResData) { el.textContent = "—"; el.style.color = THEME.muted; return; }
    el.textContent = `${(val ?? 0).toFixed(1)}%`;
    el.style.color = metricColor(val ?? 0, ok, warn);
  };
  setMetric("rk-cpu",  k.avg_cpu,  t.cpu_ok,  t.cpu_warn);

  // CPU fleet detail line — peak, p95, count above warn
  const cpuDetailEl = document.getElementById("rk-cpu-fleet-detail");
  if (cpuDetailEl) {
    const parts = [];
    if (k.cpu_peak_p95 != null) parts.push(`p95 peak: ${k.cpu_peak_p95.toFixed(1)}%`);
    if (k.cpu_above_warn > 0)  parts.push(`${k.cpu_above_warn} server(s) ≥${t.cpu_warn}%`);
    cpuDetailEl.textContent = parts.length ? parts.join(" · ") : `Warn ≥${t.cpu_warn}%`;
  }
  // Memory: show Available % (Azure native) — convert from backend's used %
  const avgMemAvail = (!noResData && k.avg_mem != null) ? 100 - k.avg_mem : null;
  const memAvailEl = document.getElementById("rk-mem");
  if (memAvailEl) {
    if (noResData) {
      memAvailEl.textContent = "—"; memAvailEl.style.color = THEME.muted;
    } else if (avgMemAvail != null) {
      memAvailEl.textContent = `${avgMemAvail.toFixed(1)}%`;
      // Invert color: low available = red
      memAvailEl.style.color = avgMemAvail <= (100 - t.mem_warn) ? THEME.red
        : avgMemAvail <= (100 - t.mem_ok) ? THEME.amber : THEME.green;
    }
  }

  // Memory fleet detail line: Avg · Min · N/M below floor
  const memFleetDetailEl = document.getElementById("rk-mem-fleet-detail");
  if (memFleetDetailEl && avgMemAvail != null) {
    const minAvail  = k.mem_avail_min;
    const p95Avail  = k.mem_avail_p95;
    const belowFloor = k.mem_below_floor ?? 0;
    const floor      = k.mem_avail_floor ?? (100 - t.mem_warn);
    const nKnown     = k.known_servers || 1;
    const parts = [`Avg ${avgMemAvail.toFixed(1)}%`];
    if (minAvail != null)  parts.push(`Min ${minAvail.toFixed(1)}%`);
    if (p95Avail != null)  parts.push(`p5 ${p95Avail.toFixed(1)}%`);
    if (belowFloor > 0)    parts.push(`${belowFloor}/${nKnown} below ${floor}%`);
    memFleetDetailEl.textContent = parts.join(" · ");
    memFleetDetailEl.style.color = belowFloor > 0 ? THEME.red : (minAvail != null && minAvail < floor ? THEME.amber : THEME.muted);
  }
  setMetric("rk-disk", k.avg_disk, t.disk_ok, t.disk_warn);

  animateRing("rk-cpu-svg",  k.avg_cpu  ?? 0, t.cpu_ok,  t.cpu_warn,  "rk-cpu-status",  "rk-cpu-ring-label");
  // Memory ring: show available % with inverted fill (full ring = 100% available = healthy)
  animateRing("rk-mem-svg",  avgMemAvail ?? 0, 100 - t.mem_warn, 100 - t.mem_ok,  "rk-mem-status",  "rk-mem-ring-label", true);
  animateRing("rk-disk-svg", k.avg_disk ?? 0, t.disk_ok, t.disk_warn, "rk-disk-status", "rk-disk-ring-label");

  // DB memory band helper — show conversion + DB expected band when DB servers present
  const memSubEl = document.getElementById("rk-mem-sub");
  const memFormulaEl = document.getElementById("rk-mem-formula");
  const avgMem = k.avg_mem ?? 0;
  const availPctDisplay = avgMemAvail != null ? avgMemAvail.toFixed(1) : null;
  // Show the Available % with Azure context
  if (memFormulaEl && availPctDisplay !== null) {
    memFormulaEl.innerHTML = `Azure Available ≈ <span style="color:${THEME.white}">${availPctDisplay}%</span>`;
  }
  if (memSubEl && (k.n_db || 0) > 0) {
    const loAvail = 100 - (t.db_mem_band_high || 92);  // 8% available
    const hiAvail = 100 - (t.db_mem_band_low || 80);    // 20% available
    if (avgMemAvail >= loAvail && avgMemAvail <= hiAvail) {
      memSubEl.innerHTML = `<span style="color:${DB_EXPECTED_COLOR}">DB expected: ${loAvail}–${hiAvail}% avail</span> · <span class="text-Cmuted">SGA/PGA steady</span>`;
    } else if (avgMemAvail < loAvail) {
      memSubEl.innerHTML = `<span style="color:${THEME.amber}">Below DB band (<${loAvail}% avail)</span>`;
    } else {
      memSubEl.textContent = `${avgMemAvail.toFixed(0)}% available`;
    }
  }

  // Health card — colored number badges (not plain text)
  const badgesEl = document.getElementById("rk-health-badges");
  if (badgesEl) {
    const c = k.n_critical ?? 0;
    const w = k.n_warning  ?? 0;
    const o = k.n_healthy  ?? 0;
    const u = k.image_only ?? 0;   // servers with no parseable metrics (image-only)
    const a = k.n_agg_trap ?? 0;
    const d = k.n_dual_pressure ?? 0;
    const badge = (n, label, color, pulse) =>
      `<div class="flex flex-col items-center gap-1">
        <span class="inline-flex items-center justify-center rounded-lg font-extrabold ${pulse ? 'animate-pulse' : ''}" style="width:36px;height:36px;font-size:1.3rem;color:${color};background:${hexA(color,.1)};border:1px solid ${hexA(color,.3)}">${n}</span>
        <span class="text-[7px] font-bold uppercase tracking-wider" style="color:${color}">${label}</span>
      </div>`;
    let html = badge(c, 'Crit', THEME.red, c > 0) + badge(w, 'Warn', THEME.amber, false) + badge(o, 'OK', THEME.green, false);
    if (u > 0) html += badge(u, 'No Data', THEME.muted, false);   // image-only — status not assessable
    if (a > 0) html += `<div class="flex flex-col items-center gap-1"><span class="inline-flex items-center justify-center rounded-lg font-extrabold" style="width:28px;height:28px;font-size:.9rem;color:${THEME.cyan};background:${hexA(THEME.cyan,.1)};border:1px solid ${hexA(THEME.cyan,.3)}" title="${a} aggregation artifact(s)">${a}</span><span class="text-[7px] font-bold uppercase tracking-wider" style="color:${THEME.cyan}">🔬</span></div>`;
    if (d > 0) html += `<div class="flex flex-col items-center gap-1"><span class="inline-flex items-center justify-center rounded-lg font-extrabold" style="width:28px;height:28px;font-size:.9rem;color:${THEME.red};background:${hexA(THEME.red,.1)};border:1px solid ${hexA(THEME.red,.3)}" title="${d} dual pressure">${d}</span><span class="text-[7px] font-bold uppercase tracking-wider" style="color:${THEME.red}">⚡</span></div>`;
    badgesEl.innerHTML = html;
  }

  // Glowing health dots
  const dotsEl = document.getElementById("rk-health-dots");
  if (dotsEl) {
    const c = k.n_critical ?? 0, w = k.n_warning ?? 0, o = k.n_healthy ?? 0;
    const scored = c + w + o;
    let dots = '';
    if (c > 0) dots += `<span class="w-2 h-2 rounded-full status-dot-red animate-pulse"></span>`;
    if (w > 0) dots += `<span class="w-2 h-2 rounded-full status-dot-amber"></span>`;
    // Only show the green "all clear" dot when at least one server was actually scored.
    if (scored === 0) dots += `<span class="w-2 h-2 rounded-full" style="background:${THEME.muted}"></span>`;
    else              dots += `<span class="w-2 h-2 rounded-full status-dot-green"></span>`;
    dotsEl.innerHTML = dots;
  }

  // Health verdict pill
  const verdictEl = document.getElementById("rk-health-verdict");
  if (verdictEl) {
    const c = k.n_critical ?? 0, w = k.n_warning ?? 0, o = k.n_healthy ?? 0;
    const scored = c + w + o;
    let label, color;
    if (scored === 0) { label = "NO DATA"; color = THEME.muted; }   // no server scored — never claim HEALTHY
    else if (c > 0)   { label = "CRITICAL"; color = THEME.red; }
    else if (w > 0)   { label = "WARNING"; color = THEME.amber; }
    else              { label = "HEALTHY"; color = THEME.green; }
    verdictEl.innerHTML = `<span class="text-[8px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-md" style="color:${color};background:${hexA(color,.12)};border:1px solid ${hexA(color,.3)}">${label}</span>`;
    verdictEl.classList.remove("hidden");
  }
}

// ── GAP 7: Grade Score Decomposition toggle ───────────────────
function toggleGradeDecomp() {
  const card = document.getElementById("grade-decomp-card");
  const body = document.getElementById("grade-decomp-body");
  if (!card || !body) return;

  if (!card.classList.contains("hidden")) {
    card.classList.add("hidden");
    return;
  }

  if (!_scoreDecomp) {
    body.innerHTML = `<div class="text-xs text-Cmuted">Score decomposition unavailable.</div>`;
    card.classList.remove("hidden");
    return;
  }

  const d = _scoreDecomp;
  if (!d.components || !Array.isArray(d.components)) {
    body.innerHTML = `<div class="text-xs text-Cmuted">Score components unavailable.</div>`;
    card.classList.remove("hidden");
    return;
  }
  const colorMap = { blue: THEME.blue, cyan: THEME.cyan, purple: THEME.purple, red: THEME.red, amber: THEME.amber };

  // Waterfall bars
  let barsHtml = d.components.map(c => {
    const cl = colorMap[c.color] || colorMap["amber"] || THEME.muted;
    const pct = d.perfect > 0 ? (c.points_lost / d.perfect * 100).toFixed(0) : "0";
    return `
      <div class="flex items-center gap-3">
        <div class="w-28 text-[10px] font-semibold text-right" style="color:${cl}">${c.label}</div>
        <div class="flex-1 h-5 rounded-full overflow-hidden" style="background:${hexA(THEME.border, 0.3)}">
          <div class="h-full rounded-full transition-all" style="width:${Math.min(pct * 2, 100)}%;background:${hexA(cl, 0.7)}"></div>
        </div>
        <div class="w-20 text-[10px] font-mono text-right" style="color:${cl}">−${c.points_lost} pts</div>
        <div class="w-16 text-[9px] text-Cmuted text-right">${c.avg != null ? c.avg.toFixed(1) + '% avg' : ''}</div>
      </div>
    `;
  }).join("");

  body.innerHTML = `
    <div class="flex items-center gap-4 mb-3">
      <div class="text-2xl font-extrabold" style="color:${GRADE_COLORS[document.getElementById('rk-grade')?.textContent] || THEME.muted}">${d.score.toFixed(0)}<span class="text-sm text-Cmuted font-normal">/100</span></div>
      <div class="text-[10px] text-Cmuted leading-relaxed">
        ${d.dominant} is the primary cost driver at <span class="font-bold text-Cwhite">−${d.dominant_lost} pts</span> (weight ${d.components.find(c => c.label === d.dominant)?.weight || ''}).<br>
        Total points lost: ${d.total_lost} across all dimensions.
      </div>
    </div>
    <div class="space-y-1.5">${barsHtml}</div>
  `;

  card.classList.remove("hidden");
}

// ── Executive Summary — premium RCA language ──────────────────
function renderResourceExecutiveSummary(exec) {
  const card = document.getElementById("resource-exec-summary");
  if (!card) {
    const kpiParent = document.getElementById("resource-review-body");
    if (!kpiParent) return;
    let existing = document.getElementById("resource-exec-summary");
    if (!existing) {
      existing = document.createElement("div");
      existing.id = "resource-exec-summary";
      existing.className = "mt-4";
      const anomCard = document.getElementById("resource-anomalies-card");
      if (anomCard) {
        anomCard.parentNode.insertBefore(existing, anomCard);
      } else {
        kpiParent.appendChild(existing);
      }
    }
    return renderResourceExecutiveSummary(exec);
  }

  if (!exec || exec.verdict === "NO DATA") {
    card.classList.add("hidden");
    return;
  }

  card.classList.remove("hidden");

  const verdictColors = {
    HEALTHY:  THEME.green,
    WARNING:  THEME.amber,
    CRITICAL: THEME.red,
  };
  const vc = verdictColors[exec.verdict] || THEME.muted;

  // False alarms — compact
  let falseAlarmsHtml = "";
  if (exec.false_alarms && exec.false_alarms.length) {
    const faTags = exec.false_alarms.map(fa =>
      `<span class="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-mono border" style="color:${THEME.cyan};border-color:${hexA(THEME.cyan,0.3)};background:${hexA(THEME.cyan,0.08)}">${escapeHtml(fa.host.split('.')[0])} <span class="font-normal text-Cmuted">Max ${fa.cpu_max.toFixed(0)}%→Avg ${fa.cpu_avg.toFixed(0)}%</span></span>`
    ).join(" ");
    falseAlarmsHtml = `
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <span class="text-[9px] font-bold uppercase tracking-wider text-Ccyan">🔬 Aggregation Artifacts (${exec.false_alarms.length})</span>
        ${faTags}
      </div>`;
  }

  // Bottlenecks — compact 2-line cards (max 48px collapsed)
  let bottlenecksHtml = "";
  if (exec.bottlenecks && exec.bottlenecks.length) {
    // Separate expected (DB within operating range) from actionable items
    const allExpected = exec.bottlenecks.every(bn => (bn.issues || []).join(' ').includes("expected range for DB"));
    const bnRows = exec.bottlenecks.map(bn => {
      const primaryIssue = (bn.issues || []).join(' · ');
      const words = primaryIssue.split(/\s+/);
      const truncated = words.length > 12 ? words.slice(0, 12).join(' ') + '…' : primaryIssue;
      // DB servers with expected memory range → muted (informational), not red (alarm)
      const isExpected = primaryIssue.includes("expected range for DB");
      const cardColor = isExpected ? THEME.muted : THEME.red;
      const statusLabel = isExpected ? "EXPECTED DB LOAD" : bn.status;
      const statusColor = isExpected ? THEME.cyan : (STATUS_COLORS[bn.status] || THEME.muted);
      const bnEnv = bn.environment || "";
      const bnEnvColor = bnEnv === "PROD" ? THEME.red : bnEnv === "TEST" ? THEME.amber : bnEnv === "DEV" ? THEME.cyan : "";
      const bnEnvBadge = bnEnv ? ` <span class="text-[8px] font-bold uppercase px-1 py-0.5 rounded" style="color:${bnEnvColor};background:${hexA(bnEnvColor,0.12)}">${bnEnv}</span>` : "";
      return `<div class="rounded-lg border p-2" style="border-color:${hexA(cardColor,0.25)};background:${hexA(cardColor,0.04)};max-height:48px;overflow:hidden">
        <div class="flex items-center gap-2">
          <span class="font-mono text-[13px] font-semibold" style="color:${cardColor}">${escapeHtml(bn.host.split('.')[0])}</span>
          <span class="text-[9px] px-1.5 py-0.5 rounded font-bold uppercase" style="color:${statusColor};background:${hexA(statusColor,0.12)}">${statusLabel}</span>
          <span class="text-[9px] text-Cmuted">${escapeHtml(bn.type)}</span>${bnEnvBadge}
        </div>
        <div class="text-[11px] text-Cmuted truncate mt-0.5">${escapeHtml(truncated)}</div>
      </div>`;
    }).join("");
    // When all items are expected DB memory, show as monitoring note instead of root cause
    const sectionIcon = allExpected ? "📋" : "🔥";
    const sectionLabel = allExpected ? "Monitoring Notes" : "Root Cause Candidates";
    const sectionColor = allExpected ? "text-Cmuted" : "text-Cred";
    bottlenecksHtml = `
      <div class="mt-3">
        <div class="text-[9px] font-bold uppercase tracking-wider ${sectionColor} mb-2" style="letter-spacing:0.12em">${sectionIcon} ${sectionLabel} (${exec.bottlenecks.length})</div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-2">${bnRows}</div>
      </div>`;
  }

  // When HEALTHY but has monitoring notes, show "HEALTHY — EXPECTED ALLOCATION"
  const hasExpectedOnly = exec.bottlenecks?.length > 0 &&
    exec.bottlenecks.every(bn => (bn.issues || []).join(' ').includes("expected range for DB"));
  const verdictDisplay = (exec.verdict === "HEALTHY" && hasExpectedOnly)
    ? "HEALTHY — EXPECTED ALLOCATION"
    : exec.verdict;

  card.innerHTML = `
    <div class="rounded-xl border p-4" style="border-color:${hexA(vc, 0.4)};background:${hexA(vc, 0.05)}">
      <div class="flex items-center gap-3 mb-2">
        <span class="text-[10px] font-bold uppercase tracking-wider text-Cmuted" style="letter-spacing:0.12em">Fleet Diagnosis</span>
        <span class="text-[10px] font-extrabold uppercase tracking-wider px-2 py-0.5 rounded-md border" style="color:${vc};border-color:${hexA(vc, 0.5)};background:${hexA(vc, 0.12)}">${verdictDisplay}</span>
      </div>
      <div class="text-[12px] text-Cwhite leading-relaxed">${escapeHtml(exec.verdict_detail)}</div>
      ${falseAlarmsHtml}
      ${bottlenecksHtml}
      <div class="mt-3 pt-2.5 border-t" style="border-color:${hexA(THEME.border, 0.4)}">
        <div class="text-[11px] text-Cwhite font-semibold">${escapeHtml(exec.summary_line1)}</div>
        <div class="text-[11px] text-Cmuted mt-0.5 leading-relaxed">${escapeHtml(exec.summary_line2)}</div>
      </div>
    </div>
  `;
}

// ── GAP 4: Anomaly Spotlight — elevated from thin strip ───────
function renderResourceAnomalies(anomalies) {
  const card = document.getElementById("resource-anomalies-card");
  const list = document.getElementById("resource-anomalies-list");
  if (!card || !list) return;

  if (!anomalies || !anomalies.length) {
    card.classList.add("hidden");
    list.innerHTML = "";
    return;
  }

  card.classList.remove("hidden");
  list.innerHTML = "";

  // Server lookup (by short host) so a memory anomaly on a DB server in its
  // expected SGA/PGA band reads as "DB Normal", not WARNING.
  const _srvByHost = {};
  for (const s of (window.appData?.resource?.servers || [])) {
    const h = (s.host || s.server || "").split(".")[0];
    if (h && !_srvByHost[h]) _srvByHost[h] = s;
  }
  const _anomalyDbExpected = (a) => {
    if ((a.metric || "").toLowerCase() !== "memory") return false;
    const srv = _srvByHost[(a.host || "?").split(".")[0]];
    if (!srv || (srv.type || "").toUpperCase() !== "DB") return false;
    // anomaly Memory value is USED % — same convention _isDbMemExpected expects
    return srv.mem_status === "DB_NORMAL" || _isDbMemExpected(srv.type, a.value ?? srv.mem_pct);
  };

  // Group anomalies by host for server-centric cards
  const byHost = {};
  for (const a of anomalies) {
    const host = (a.host || "?").split(".")[0];
    const srv = _srvByHost[host];
    if (srv && !a.role) a.role = (srv.type || "").toUpperCase();
    a._dbExpected = _anomalyDbExpected(a);
    if (!byHost[host]) byHost[host] = { host, items: [], maxZ: 0 };
    byHost[host].items.push(a);
    byHost[host].maxZ = Math.max(byHost[host].maxZ, Math.abs(a.z || 0));
  }
  const hostArr = Object.values(byHost).sort((a, b) => b.maxZ - a.maxZ);

  // ── Canonical severity vocabulary ──
  const _sevFromZ = (z) => {
    const az = Math.abs(z);
    if (az >= 4) return { label: "CRITICAL", color: THEME.red };
    if (az >= 3) return { label: "WARNING",  color: THEME.amber };
    return              { label: "ELEVATED", color: THEME.amber };
  };

  for (const hg of hostArr) {
    // If EVERY flagged metric on this host is DB-expected memory, the host is
    // not under pressure → render "DB NORMAL" (purple), not WARNING. A real
    // CPU/disk anomaly on the same host keeps severity styling so it's never masked.
    const allDbExpected = hg.items.length > 0 && hg.items.every(i => i._dbExpected);
    let sevLabel, sev;
    if (allDbExpected) {
      sevLabel = "DB NORMAL"; sev = DB_EXPECTED_COLOR;
    } else {
      const _s = _sevFromZ(hg.maxZ); sevLabel = _s.label; sev = _s.color;
    }
    const borderColor = allDbExpected ? hexA(DB_EXPECTED_COLOR, 0.45) : hexA(sev, 0.3);
    const bgColor     = allDbExpected ? hexA(DB_EXPECTED_COLOR, 0.07) : hexA(sev, 0.05);

    const metricChips = hg.items.map(a => {
      const mKey = (a.metric || "").toLowerCase();
      const mc = a._dbExpected ? DB_EXPECTED_COLOR : (mKey === "cpu" ? THEME.blue : mKey === "memory" ? THEME.cyan : THEME.purple);
      const zVal = _n(a.z);
      const zLabel = a._dbExpected ? "expected" : (Math.abs(zVal) >= 4 ? "extreme" : Math.abs(zVal) >= 3 ? "significant" : "elevated");
      const roleTag = a.role && a.role !== "SERVER" ? ` <span class="text-[7px] opacity-60">[${a.role}]</span>` : "";
      const valColor = a._dbExpected ? DB_EXPECTED_COLOR : sev;
      return `<span class="inline-flex items-center gap-1 text-[10px]"><span class="w-1.5 h-1.5 rounded-full inline-block" style="background:${mc}"></span><span class="font-semibold" style="color:${mc}">${(a.metric || "").toUpperCase()}</span>${roleTag} <span class="font-mono" style="color:${valColor}">${_n(a.value).toFixed(0)}%</span> <span class="text-Cmuted text-[8px]" title="Statistical deviation: z=${zVal >= 0 ? '+' : ''}${zVal.toFixed(1)}">${zLabel}</span></span>`;
    }).join(" ");

    // "Why flagged?" drilldown formula
    const drillRows = hg.items.map(a => {
      const zVal = _n(a.z);
      const floorVal = a.floor || "—";
      const expNote = a._dbExpected ? ` · <span style="color:${DB_EXPECTED_COLOR}">DB SGA/PGA expected band — not pressure</span>` : "";
      return `<div class="text-[9px] text-Cmuted"><span class="font-semibold text-Cwhite">${(a.metric||"").toUpperCase()}</span>: value=${_n(a.value).toFixed(1)}% · z-score=${zVal >= 0 ? '+' : ''}${zVal.toFixed(2)} (≥2.0 threshold) · floor=${floorVal}%${a.role ? ' · role=' + a.role : ''}${expNote}</div>`;
    }).join("");

    const item = document.createElement("div");
    item.className = "rounded-lg border p-2 transition cursor-pointer";
    item.style.borderColor = borderColor;
    item.style.background = bgColor;
    item.innerHTML = `
      <div class="flex items-center justify-between">
        <span class="text-[13px] font-mono font-semibold" style="color:${sev}">${escapeHtml(hg.host)}</span>
        <div class="flex items-center gap-2">
          <span class="text-[8px] font-extrabold uppercase tracking-wider px-1.5 py-0.5 rounded-md" style="color:${sev};background:${hexA(sev,0.15)}">${sevLabel}</span>
          <span class="text-[7px] text-Cmuted opacity-60 hover:opacity-100">▸ why?</span>
        </div>
      </div>
      <div class="flex flex-wrap gap-2 mt-0.5">${metricChips}</div>
      ${allDbExpected ? `<div class="text-[9px] mt-1" style="color:${DB_EXPECTED_COLOR}">Memory above fleet mean is expected for DB (SGA/PGA pre-allocation) — not a pressure anomaly.</div>` : ""}
      <div class="anomaly-drill hidden mt-1.5 pt-1.5 border-t border-Cborder/30">${drillRows}</div>
    `;
    item.addEventListener("click", () => {
      const drill = item.querySelector(".anomaly-drill");
      if (drill) drill.classList.toggle("hidden");
    });
    list.appendChild(item);
  }
}

// ── Horizontal grouped bar (CPU / Mem / Disk × top 12) ────────
function renderResourceBarChart(servers) {
  const canvas = document.getElementById("chart-resource-bars");
  if (!canvas) return;

  const known = (servers || []).filter(
    (s) => !s.image_only && Math.max(s.cpu_pct || 0, s.mem_pct || 0, s.disk_pct || 0) > 0
  );
  destroyChart("resourceBars");
  if (!known.length) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = THEME.muted;
    ctx.font = "12px Sora, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("No metric data — upload a Zabbix file with CPU/Mem/Disk values.", canvas.width / 2, 40);
    return;
  }

  const top = [...known].sort((a, b) => (b.cpu_pct || 0) - (a.cpu_pct || 0)).slice(0, 12);
  const labels = top.map((s) => {
    const env = s.environment || "";
    return env ? `${truncate(s.server, 18)} [${env}]` : truncate(s.server, 22);
  });

  charts.resourceBars = new Chart(canvas.getContext("2d"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "CPU %",       data: top.map((s) => s.cpu_pct  || 0), backgroundColor: "rgba(59,130,246,0.8)",  borderColor: "#3b82f6",  borderWidth: 1, borderRadius: 4, barPercentage: 0.82, categoryPercentage: 0.78 },
        { label: "Mem Used %", data: top.map((s) => s.mem_pct  || 0), backgroundColor: "rgba(245,158,11,0.7)",  borderColor: "#f59e0b",  borderWidth: 1, borderRadius: 4, barPercentage: 0.82, categoryPercentage: 0.78 },
        { label: "Disk %",     data: top.map((s) => s.disk_pct || 0), backgroundColor: "rgba(168,85,247,0.8)",  borderColor: "#a855f7",  borderWidth: 1, borderRadius: 4, barPercentage: 0.82, categoryPercentage: 0.78 },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      animation: false, // no initial animation — eliminates 400ms canvas paint-loop on load
      layout: { padding: { right: 16, left: 4, top: 4, bottom: 4 } },
      plugins: {
        legend: {
          position: "bottom",
          labels: { color: THEME.muted, font: { size: 11, family: "Sora, sans-serif" }, boxWidth: 12, padding: 16, usePointStyle: true, pointStyle: "rectRounded" },
        },
        tooltip: {
          backgroundColor: "rgba(9,14,31,0.95)",
          borderColor: THEME.border,
          borderWidth: 1,
          titleColor: THEME.white,
          titleFont: { size: 12, weight: "bold" },
          bodyColor: THEME.white,
          bodyFont: { size: 11 },
          padding: 10,
          cornerRadius: 6,
          callbacks: {
            label: (ctx) => {
              const srv = top[ctx.dataIndex];
              let lbl = ` ${ctx.dataset.label}: ${ctx.parsed.x.toFixed(1)}%`;
              // Annotate DB servers in expected SGA/PGA memory band
              if (ctx.datasetIndex === 1 && srv?.type === "DB"
                  && (srv.mem_pct || 0) >= 80 && (srv.mem_pct || 0) <= 92) {
                lbl += " (DB expected — SGA/PGA)";
              }
              return lbl;
            },
          },
        },
        zoom: _zoomConfig({ mode: "y" }),
      },
      scales: {
        x: {
          min: 0, max: 105,
          grid:   { color: "rgba(255,255,255,0.04)", drawBorder: false },
          ticks:  { color: THEME.muted, font: { size: 11, family: "Sora, sans-serif" }, callback: (v) => `${v}%`, stepSize: 25 },
          title:  { display: true, text: "Utilisation % (longer bar = more pressure)", color: THEME.muted, font: { size: 11, family: "Sora, sans-serif" } },
        },
        y: {
          grid:  { display: false },
          ticks: { color: THEME.white, font: { size: 11, family: "'JetBrains Mono', monospace" }, padding: 8 },
        },
      },
    },
    plugins: [resourceThresholdLinesPlugin(75, 90)],
  });

  // Enterprise: export toolbar
  _addChartToolbar(canvas.parentElement, charts.resourceBars, () => {
    let csv = "Server,CPU_Pct,Mem_Pct,Disk_Pct,Env\n";
    top.forEach(s => { csv += `${s.server},${s.cpu_pct||0},${s.mem_pct||0},${s.disk_pct||0},${s.env||""}\n`; });
    return csv;
  });
}

// Plugin: dashed amber line at 75% and dashed red line at 90%
function resourceThresholdLinesPlugin(okT, warnT) {
  return {
    id: "resourceThresholds",
    afterDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart;
      if (!chartArea || !scales?.x) return;
      const drawLine = (val, color) => {
        const x = scales.x.getPixelForValue(val);
        ctx.save();
        ctx.beginPath();
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1.2;
        ctx.strokeStyle = color;
        ctx.moveTo(x, chartArea.top);
        ctx.lineTo(x, chartArea.bottom);
        ctx.stroke();
        ctx.restore();
      };
      drawLine(okT,   THEME.amber);
      drawLine(warnT, THEME.red);
    },
  };
}

// ── Per-server metric bars (replaces the old heatmap) ─────────
//
// Each row shows: hostname + role, then horizontal bars for CPU/MEM/DISK
// with threshold lines. Memory >90% gets a pulsing red dot. Disk uses a
// segmented used-vs-available look. Trend arrow placeholder for future
// time-series. The table below is still the primary view — these bars
// are an embedded per-cell visualization, not a duplicate chart.
function renderResourceHeatmap(servers) {
  const wrap = document.getElementById("resource-heatmap");
  if (!wrap) return;

  const known = (servers || []).filter(
    (s) => !s.image_only && Math.max(s.cpu_pct || 0, s.mem_pct || 0, s.disk_pct || 0) > 0
  );
  if (!known.length) {
    wrap.innerHTML = `<div class="text-center text-Cmuted text-[11px] py-8">No metric data available — upload a Zabbix file with CPU/Mem/Disk values.</div>`;
    return;
  }
  const top = [...known].sort((a, b) =>
    Math.max(b.cpu_pct||0, b.mem_pct||0, b.disk_pct||0)
    - Math.max(a.cpu_pct||0, a.mem_pct||0, a.disk_pct||0)
  ).slice(0, 20);

  const trendArrow = (t) => {
    if (t == null) return `<span class="text-Cmuted text-[10px]" title="No trend — snapshot only">→</span>`;
    if (t > 5)  return `<span class="text-Cred text-[11px]" title="Up vs 7-day avg">↑</span>`;
    if (t < -5) return `<span class="text-Cgreen text-[11px]" title="Down vs 7-day avg">↓</span>`;
    return `<span class="text-Cmuted text-[10px]" title="Flat vs 7-day avg">→</span>`;
  };

  const barCell = (val, ok = 60, warn = 80, opts = {}) => {
    if (val == null || isNaN(Number(val)) || val < 0) {
      if (opts.showGapWarning) {
        return `<div class="metric-bar-track" title="No disk I/O telemetry collected for this server — monitoring gap, not idle"><div class="metric-bar-fill" style="width:0%;background:${hexA(THEME.amber,0.25)}"></div></div>
              <div class="text-[9px] text-right mt-0.5 font-semibold whitespace-nowrap" style="color:${THEME.amber};min-width:3rem" title="No disk I/O telemetry collected — monitoring gap, not confirmed idle">⚠ monitoring gap</div>`;
      }
      return `<div class="metric-bar-track" title="No data"><div class="metric-bar-fill" style="width:0%;background:#475569"></div></div>
              <div class="text-[10px] text-Cmuted text-right mt-0.5 font-mono" style="min-width:3rem">N/A</div>`;
    }
    const v = Math.max(0, Math.min(100, Number(val)));
    let color, gradStart, gradEnd;
    if (opts.invertColor) {
      // For metrics where lower = worse (e.g., Available Memory %)
      if (v <= warn)      { color = "#ef4444"; gradStart = "#dc2626"; gradEnd = "#f87171"; }
      else if (v <= ok)   { color = "#f59e0b"; gradStart = "#d97706"; gradEnd = "#fbbf24"; }
      else                { color = "#10b981"; gradStart = "#059669"; gradEnd = "#34d399"; }
    } else {
      if (v >= warn)      { color = "#ef4444"; gradStart = "#dc2626"; gradEnd = "#f87171"; }
      else if (v >= ok)   { color = "#f59e0b"; gradStart = "#d97706"; gradEnd = "#fbbf24"; }
      else                { color = "#10b981"; gradStart = "#059669"; gradEnd = "#34d399"; }
    }
    const threshold = opts.threshold ?? warn;
    // DB SGA/PGA expected band (80–92% used / 8–20% available) → purple.
    // Intentional pre-allocation, not pressure: overrides the red/amber that the
    // raw available% would otherwise produce, and silences the pressure pulse.
    const _dbExpectedBand = _isDbMemExpected(opts.serverRole, opts.memUsedPct);
    if (_dbExpectedBand) {
      color = DB_EXPECTED_COLOR; gradStart = DB_EXPECTED_GRAD[0]; gradEnd = DB_EXPECTED_GRAD[1];
    }
    const pulseActive = !_dbExpectedBand && opts.pulseAt != null && (opts.invertColor ? v <= opts.pulseAt : v >= opts.pulseAt);
    const pulse = pulseActive
      ? `<span class="pulse-red" style="position:absolute;right:-3px;top:-3px;width:8px;height:8px;border-radius:50%;background:#ef4444"></span>`
      : "";
    const grad = `linear-gradient(90deg, ${gradStart} 0%, ${gradEnd} 100%)`;
    let bar = `<div class="metric-bar-fill" style="width:${v}%;background:${grad}"></div>`;
    if (opts.segmented) {
      bar = `<div class="metric-bar-fill" style="width:${v}%;background:repeating-linear-gradient(90deg,${gradStart} 0 8px,${gradEnd} 8px 16px)"></div>`;
    }
    const _barTitle = _dbExpectedBand
      ? `${v.toFixed(1)}% available — DB expected SGA/PGA band (≈${100 - (RESOURCE_THRESHOLDS.db_mem_band_high ?? 92)}–${100 - (RESOURCE_THRESHOLDS.db_mem_band_low ?? 80)}% available is normal for DB)`
      : `${v.toFixed(1)}% (threshold ${threshold}%)`;
    return `<div class="metric-bar-track" title="${_barTitle}">
              ${bar}
              <div class="metric-bar-threshold" style="left:${threshold}%"></div>
              ${pulse}
            </div>
            <div class="text-[10px] font-mono text-right mt-0.5 tabular-nums" style="color:${color};min-width:3rem">${v.toFixed(0)}%${opts.trendArrow ? ' ' + opts.trendArrow : ''}</div>`;
  };

  const rows = top.map((s) => {
    const host = (s.host || s.server || "?").split(".")[0];
    const type = (s.type || "?").toUpperCase();
    const typeColor = type === "DB" ? "#a855f7" : (type === "SRE" ? "#06b6d4" : "#3b82f6");
    const sEnv = s.environment || "";
    const sEnvColor = sEnv === "PROD" ? "#ef4444" : sEnv === "TEST" ? "#f59e0b" : sEnv === "DEV" ? "#06b6d4" : "";
    const sEnvTag = sEnv ? `<span class="text-[7px] font-bold uppercase tracking-wider px-0.5 py-0.5 rounded shrink-0" style="color:${sEnvColor};background:${hexA(sEnvColor,0.12)}">${sEnv}</span>` : "";
    const cpuTrend  = trendArrow(s.cpu_trend_pct);
    const memTrend  = trendArrow(s.mem_trend_pct);
    const diskTrend = trendArrow(s.disk_trend_pct);
    const serverName = escapeHtml(s.server || s.host || "");

    return `<div class="grid items-center gap-4 py-2 border-b border-Cborder/20 cursor-pointer hover:bg-white/[0.03] transition rounded-md px-1"
                 style="grid-template-columns:minmax(140px,1.2fr) 1.5fr 1.5fr 1.5fr"
                 onclick="filterServerTable('${serverName.replace(/'/g, "\\'")}')" title="Click to filter table to ${serverName}">
      <div class="min-w-0 flex items-center gap-1.5">
        <span class="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0"
              style="color:${typeColor};background:${hexA(typeColor, 0.12)};border:1px solid ${hexA(typeColor, 0.35)}">${escapeHtml(type)}</span>
        ${sEnvTag}
        <span class="text-[11px] font-mono text-Cwhite truncate font-medium" title="${escapeHtml(s.host || s.server || '')}">${escapeHtml(host)}</span>
      </div>
      <div>${barCell(s.cpu_pct,  RESOURCE_THRESHOLDS.cpu_ok, RESOURCE_THRESHOLDS.cpu_warn, { threshold: RESOURCE_THRESHOLDS.cpu_warn, trendArrow: cpuTrend })}${s.cpu_avg_pct != null && Math.abs((s.cpu_pct || 0) - s.cpu_avg_pct) > 5 ? `<div class="text-[7px] text-Cmuted mt-0.5" title="Recent snapshot: ${(s.cpu_pct||0).toFixed(1)}% · Period avg: ${s.cpu_avg_pct.toFixed(1)}%">avg ${s.cpu_avg_pct.toFixed(0)}%</div>` : ""}</div>
      <div>${barCell(s.mem_pct != null ? 100 - s.mem_pct : null,  100 - RESOURCE_THRESHOLDS.mem_ok, 100 - RESOURCE_THRESHOLDS.mem_warn, { threshold: 100 - RESOURCE_THRESHOLDS.mem_warn, invertColor: true, pulseAt: 5, trendArrow: memTrend, serverRole: s.type, memUsedPct: s.mem_pct })}</div>
      <div>${barCell(s.disk_pct, RESOURCE_THRESHOLDS.disk_ok, RESOURCE_THRESHOLDS.disk_warn, { threshold: RESOURCE_THRESHOLDS.disk_warn, segmented: true, trendArrow: diskTrend, showGapWarning: true })}</div>
    </div>`;
  }).join("");

  wrap.innerHTML = `
    <div class="flex items-center gap-4 pb-2 mb-2 text-[9px] text-Cmuted">
      <span class="font-bold uppercase tracking-wider text-Cwhite/70">Legend:</span>
      <span class="inline-flex items-center gap-1.5"><span class="w-3 h-3 rounded" style="background:linear-gradient(135deg,#059669,#34d399)"></span> OK</span>
      <span class="inline-flex items-center gap-1.5"><span class="w-3 h-3 rounded" style="background:linear-gradient(135deg,#d97706,#fbbf24)"></span> Warning</span>
      <span class="inline-flex items-center gap-1.5"><span class="w-3 h-3 rounded" style="background:linear-gradient(135deg,#dc2626,#f87171)"></span> Critical</span>
      <span class="inline-flex items-center gap-1.5"><span class="w-3 h-3 rounded" style="background:linear-gradient(135deg,${DB_EXPECTED_GRAD[0]},${DB_EXPECTED_GRAD[1]})"></span> DB expected</span>
      <span class="text-Cmuted/60 text-[8px]">| Memory = Available % (Azure native, lower = more pressure) · Disk = I/O BW consumed % (not storage space) · DB servers 8–20% mem available is expected SGA/PGA (shown purple)</span>
    </div>
    <div class="grid items-center gap-4 pb-2 border-b border-Cborder/40 text-[10px] uppercase tracking-wider text-Cmuted font-bold px-1"
         style="grid-template-columns:minmax(140px,1.2fr) 1.5fr 1.5fr 1.5fr">
      <div>Server</div>
      <div class="flex items-center gap-1">
        <span class="w-2 h-2 rounded-sm" style="background:${THEME.blue}"></span> CPU (threshold ${RESOURCE_THRESHOLDS.cpu_warn}%)
      </div>
      <div class="flex items-center gap-1 cursor-help" title="Available Memory % (Azure native). Shorter bar = less free RAM = more memory pressure. DB servers normally show 8–20% available (SGA/PGA pre-allocation) and render in purple as expected.">
        <span class="w-2 h-2 rounded-sm" style="background:${THEME.cyan}"></span> Mem avail % <span class="text-[8px] lowercase tracking-normal text-Cmuted/70">· shorter = more pressure</span> <span class="text-[7px] opacity-50">ℹ</span>
      </div>
      <div class="flex items-center gap-1" title="Azure OS/Data Disk Bandwidth Consumed % — NOT storage space. 0.3% = near-idle I/O. Warn at ${RESOURCE_THRESHOLDS.disk_warn}% of provisioned IOPS/BW quota.">
        <span class="w-2 h-2 rounded-sm" style="background:${THEME.purple}"></span> Disk I/O (threshold ${RESOURCE_THRESHOLDS.disk_warn}%) <span class="text-[7px] opacity-50">ℹ</span>
      </div>
    </div>
    ${rows}
  `;
}

// ── Donut ring helper (used for fleet AVG CPU/MEM/DISK tiles) ─
// Draws a 64×64 ring on a canvas where:
//   - the fill arc is the metric value (green/amber/red by threshold)
//   - a thin tick marks the threshold position
function drawDonutRing(canvasId, value, threshold = 80) {
  const c = document.getElementById(canvasId);
  if (!c || !c.getContext) return;
  const v = Math.max(0, Math.min(100, Number(value) || 0));
  const ctx = c.getContext("2d");
  const W = c.width, H = c.height;
  ctx.clearRect(0, 0, W, H);
  const cx = W / 2, cy = H / 2;
  const r = Math.min(W, H) / 2 - 6;
  const lw = 8;

  // Track
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.lineWidth = lw;
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.stroke();

  // Color by zone
  let color;
  if (v >= threshold)        color = "#ef4444";
  else if (v >= threshold-20) color = "#f59e0b";
  else                        color = "#10b981";

  // Fill arc — start at top (-π/2)
  const start = -Math.PI / 2;
  const end   = start + (v / 100) * Math.PI * 2;
  ctx.beginPath();
  ctx.arc(cx, cy, r, start, end);
  ctx.lineWidth = lw;
  ctx.strokeStyle = color;
  ctx.lineCap = "round";
  ctx.stroke();

  // Threshold tick — outer notch
  const tA = start + (threshold / 100) * Math.PI * 2;
  const tx1 = cx + Math.cos(tA) * (r - lw / 2 - 1);
  const ty1 = cy + Math.sin(tA) * (r - lw / 2 - 1);
  const tx2 = cx + Math.cos(tA) * (r + lw / 2 + 2);
  const ty2 = cy + Math.sin(tA) * (r + lw / 2 + 2);
  ctx.beginPath();
  ctx.moveTo(tx1, ty1); ctx.lineTo(tx2, ty2);
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = "rgba(255,255,255,0.6)";
  ctx.lineCap = "butt";
  ctx.stroke();
}

// ── Lazy server detail table ──────────────────────────────────
function renderResourceTable(servers) {
  const tbody = document.getElementById("resource-tbody");
  const empty = document.getElementById("resource-table-empty");
  const toggle = document.getElementById("resource-table-toggle");
  const countEl = document.getElementById("resource-table-count");
  if (!tbody) return;

  let rows = [...(servers || [])];

  // ── Sort ─────────────────────────────────────────────────
  const sk = resourceTableState.sortKey;
  const sd = resourceTableState.sortDir;
  const statusOrder = { Critical: 0, Warning: 1, Healthy: 2, Unknown: 3 };
  rows.sort((a, b) => {
    let va = a[sk], vb = b[sk];
    if (sk === "status") {
      va = statusOrder[va] ?? 9;
      vb = statusOrder[vb] ?? 9;
    } else if (typeof va === "string") {
      va = (va || "").toLowerCase();
      vb = (vb || "").toLowerCase();
      return sd * va.localeCompare(vb);
    }
    va = va ?? -Infinity;
    vb = vb ?? -Infinity;
    return sd * (va > vb ? 1 : va < vb ? -1 : 0);
  });

  // ── Filters ──────────────────────────────────────────────
  const f = resourceTableState.filter;
  if (f) {
    rows = rows.filter(s =>
      (s.server || "").toLowerCase().includes(f) ||
      (s.host   || "").toLowerCase().includes(f)
    );
  }
  if (resourceTableState.filterType) {
    rows = rows.filter(s => (s.type || "").toUpperCase() === resourceTableState.filterType);
  }
  if (resourceTableState.filterEnv) {
    rows = rows.filter(s => (s.environment || "") === resourceTableState.filterEnv);
  }
  if (resourceTableState.filterStatus) {
    rows = rows.filter(s => s.status === resourceTableState.filterStatus);
  }

  // ── Show count ───────────────────────────────────────────
  const totalAll = (servers || []).length;
  if (countEl) {
    if (rows.length < totalAll) {
      countEl.textContent = `(${rows.length} of ${totalAll})`;
    } else {
      countEl.textContent = `(${totalAll})`;
    }
  }

  // Show all vs preview
  const total = rows.length;
  const showAll = resourceTableState.showAll;
  const slice = showAll ? rows : rows.slice(0, RESOURCE_TABLE_PREVIEW);

  // Toggle button label/visibility
  if (toggle) {
    if (total <= RESOURCE_TABLE_PREVIEW) {
      toggle.classList.add("hidden");
    } else {
      toggle.classList.remove("hidden");
      toggle.textContent = showAll
        ? `Show preview (${RESOURCE_TABLE_PREVIEW})`
        : `Show all (${total})`;
    }
  }

  if (!slice.length) {
    tbody.innerHTML = "";
    empty?.classList.remove("hidden");
    return;
  }
  empty?.classList.add("hidden");

  tbody.innerHTML = "";
  for (const r of slice) {
    const tr = document.createElement("tr");
    tr.className = "transition-colors";
    const _dispStatus = _resourceDisplayStatus(r);
    tr.style.background = statusRowTint(_dispStatus);

    // Role-specific CPU thresholds (from backend), fallback to global
    const cpuOk   = r.role_cpu_ok   ?? RESOURCE_THRESHOLDS.cpu_ok;
    const cpuWarn = r.role_cpu_warn ?? RESOURCE_THRESHOLDS.cpu_warn;

    // Build CPU cell with aggregation trap badge
    const cpuAvail = r.cpu_available !== false && r.cpu_pct != null;
    const cpuVal = cpuAvail ? r.cpu_pct.toFixed(1) : null;
    const cpuColor = cpuAvail ? metricColor(r.effective_cpu ?? r.cpu_pct ?? 0, cpuOk, cpuWarn) : '';
    let cpuExtra = "";
    if (r.agg_trap) {
      cpuExtra = ` <span class="text-[8px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.15)};border:1px solid ${hexA(THEME.cyan,0.4)}" title="Aggregation Artifact: Max CPU ${cpuVal}% but Avg only ${(r.cpu_avg_pct||0).toFixed(1)}% — brief spike, server is healthy">BRIEF SPIKE</span>`;
    }

    // Build dual pressure badge
    let dualBadge = "";
    if (r.dual_pressure) {
      dualBadge = ` <span class="text-[8px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${THEME.red};background:${hexA(THEME.red,0.15)};border:1px solid ${hexA(THEME.red,0.4)}" title="DUAL PRESSURE: CPU ≥80% + Memory ≥85% — severe resource exhaustion">DUAL</span>`;
    }

    // Null-safe metric display
    const memAvail = r.mem_available !== false && r.mem_pct != null;
    const memGbAvail = r.mem_available !== false && r.mem_gb != null;
    const cpuAvgAvail = r.cpu_available !== false && r.cpu_avg_pct != null;

    // Role-aware memory context tag (logic uses mem_pct = used%, display shows available%)
    let memContextTag = "";
    const rType = (r.type || "").toUpperCase();
    const rEnv = (r.environment || "").toUpperCase();
    const memAvailPct = memAvail ? 100 - r.mem_pct : 0;
    if (memAvail) {
      if (_isDbMemExpected(rType, r.mem_pct)) {
        memContextTag = ` <span class="text-[8px] cursor-help px-1 py-0.5 rounded" style="color:${DB_EXPECTED_COLOR};background:${hexA(DB_EXPECTED_COLOR,0.12)}" title="DB expected allocation (SGA/PGA). ${(100 - RESOURCE_THRESHOLDS.db_mem_band_high)}–${(100 - RESOURCE_THRESHOLDS.db_mem_band_low)}% available is normal for DB servers.">DB expected</span>`;
      } else if (rType === "DB" && r.mem_pct > (RESOURCE_THRESHOLDS.db_mem_band_high || 92)) {
        memContextTag = ` <span class="text-[8px] cursor-help px-1 py-0.5 rounded" style="color:${THEME.red};background:${hexA(THEME.red,0.1)}" title="DB server below expected available (<${(100 - (RESOURCE_THRESHOLDS.db_mem_band_high || 92))}%). Check for memory pressure.">DB high</span>`;
      } else if (rEnv === "TEST" && r.mem_pct >= 70) {
        memContextTag = ` <span class="text-[8px] cursor-help px-1 py-0.5 rounded" style="color:${THEME.muted};background:${hexA(THEME.muted,0.1)}" title="TEST environment — low available memory may be tolerated.">TEST tolerated</span>`;
      } else if (rType === "APP" && r.mem_pct >= RESOURCE_THRESHOLDS.mem_warn) {
        memContextTag = ` <span class="text-[8px] cursor-help px-1 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.1)}" title="APP server critically low available memory. Check for memory leak or under-provisioning.">APP elevated</span>`;
      }
    }

    // Role-aware memory column colour: DB expected band → purple (expected, DB identity), not red/amber
    const _dbMemNormal = r.mem_status === "DB_NORMAL" || _isDbMemExpected(rType, r.mem_pct);
    const memColor = !memAvail ? ""
      : _dbMemNormal                                                     ? DB_EXPECTED_COLOR
      : memAvailPct <= (100 - RESOURCE_THRESHOLDS.mem_warn)             ? THEME.red
      : memAvailPct <= (100 - RESOURCE_THRESHOLDS.mem_ok)               ? THEME.amber
      : THEME.green;

    // Environment badge
    const env = r.environment || "";
    const envColor = env === "PROD" ? THEME.red : env === "TEST" ? THEME.amber : env === "DEV" ? THEME.cyan : THEME.muted;
    const envBadge = env ? `<span class="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded" style="color:${envColor};background:${hexA(envColor,0.12)};border:1px solid ${hexA(envColor,0.3)}">${env}</span>` : '<span class="text-Cmuted">—</span>';

    tr.innerHTML = `
      <td class="py-2.5 pr-3 font-semibold text-Cwhite truncate max-w-[220px]" title="${escapeHtml(r.host || r.server)}">${escapeHtml(r.server)}${dualBadge}</td>
      <td class="py-2.5 pr-3 text-Cmuted">${escapeHtml(r.type || "")}</td>
      <td class="py-2.5 pr-3">${envBadge}</td>
      <td class="py-2.5 pr-3 text-right font-mono tabular-nums ${!cpuAvail ? 'text-Cmuted' : ''}" style="color:${cpuColor}">${cpuAvail ? cpuVal + '%' + cpuExtra : '<span title="Data unavailable">N/A</span>'}</td>
      <td class="py-2.5 pr-3 text-right font-mono tabular-nums text-Cmuted">${cpuAvgAvail ? r.cpu_avg_pct.toFixed(1) + '%' : '<span title="Insufficient data for period average">N/A</span>'}</td>
      <td class="py-2.5 pr-3 text-right font-mono tabular-nums ${!memAvail ? 'text-Cmuted' : ''}" style="color:${memColor}">${memAvail ? memAvailPct.toFixed(1) + '%' + memContextTag : '<span title="Data unavailable">N/A</span>'}</td>
      <td class="py-2.5 pr-3 text-right font-mono tabular-nums text-Cmuted">${memGbAvail ? r.mem_gb.toFixed(1) : '<span title="Memory capacity not available from source">N/A</span>'}</td>
      <td class="py-2.5 pr-3 text-right font-mono tabular-nums ${r.disk_pct == null ? 'text-Cmuted' : ''}" style="color:${r.disk_pct != null ? metricColor(r.disk_pct, RESOURCE_THRESHOLDS.disk_ok, RESOURCE_THRESHOLDS.disk_warn) : ''}">${r.disk_pct != null ? (r.disk_pct).toFixed(1) + '%' : '<span title="Disk data unavailable">N/A</span>'}</td>
      <td class="py-2.5 pr-3">
        <span class="text-[10px] font-bold uppercase tracking-wider px-2 py-1 rounded-md border cursor-help" title="${_buildStatusTooltip(r)}" style="${statusPillStyle(_dispStatus)}">${escapeHtml(_dispStatus)}</span>
        ${(r.status === "Warning" || r.status === "Critical") ? `<div class="text-[8px] text-Cmuted mt-0.5 leading-tight">${_statusDrivenBy(r)}</div>` : ""}
        ${_dispStatus === "DB Normal" ? `<div class="text-[8px] mt-0.5 leading-tight" style="color:${DB_EXPECTED_COLOR}">SGA/PGA steady — high memory by design</div>` : ""}
      </td>
      <td class="py-2.5 pr-3 text-Cmuted truncate max-w-[180px]" title="${escapeHtml(r.source_env || "")}">${escapeHtml(truncate(r.source_env || "", 28))}</td>
    `;
    tbody.appendChild(tr);
  }
}

// Returns a compact "↑ Mem 16% avail · CPU 81%" line showing which metric(s) drove
// the server into Warning/Critical — eliminates ambiguity when e.g. disk is 0.3%
// but the row shows Warning due to memory.
function _statusDrivenBy(r) {
  const t = RESOURCE_THRESHOLDS;
  const cpu  = r.effective_cpu ?? r.cpu_pct ?? 0;
  const mem  = r.mem_pct;
  const disk = r.disk_pct;
  const memAvail = mem != null ? +(100 - mem).toFixed(1) : null;
  // Score each metric by how far it exceeds its warn threshold (0–1 scale).
  // Sort descending so the worst metric always leads the sub-line.
  const candidates = [];
  if (cpu >= t.cpu_ok) {
    candidates.push({ label: `CPU ${cpu.toFixed(0)}%`, score: (cpu - t.cpu_ok) / Math.max(1, 100 - t.cpu_ok) });
  }
  if (memAvail != null && memAvail <= (100 - t.mem_ok)) {
    // Skip DB servers whose memory is within the expected SGA/PGA band —
    // low available% is normal for DB workloads and should not appear as a driver.
    if (!_isDbMemExpected(r.type, r.mem_pct)) {
      const floor = 100 - t.mem_ok;
      candidates.push({ label: `Mem ${memAvail.toFixed(0)}% avail`, score: floor > 0 ? (floor - memAvail) / floor : 1 });
    }
  }
  if (disk != null && disk >= t.disk_ok) {
    candidates.push({ label: `Disk I/O ${disk.toFixed(0)}%`, score: (disk - t.disk_ok) / Math.max(1, 100 - t.disk_ok) });
  }
  candidates.sort((a, b) => b.score - a.score);
  return candidates.length ? `↑ ${candidates.map(c => c.label).join(" · ")}` : "";
}

function statusRowTint(status) {
  switch (status) {
    case "Critical": return hexA(THEME.red,    0.10);
    case "Warning":  return hexA(THEME.amber,  0.08);
    case "Healthy":  return hexA(THEME.green,  0.06);
    case "DB Normal": return hexA(DB_EXPECTED_COLOR, 0.06);
    default:         return "transparent";
  }
}

function statusPillStyle(status) {
  const c = STATUS_COLORS[status] || THEME.muted;
  return `color:${c};border-color:${hexA(c, 0.5)};background:${hexA(c, 0.12)}`;
}

function _buildStatusTooltip(r) {
  // Build a human-readable explanation of WHY the server has this status.
  const cpuOk   = r.role_cpu_ok   ?? RESOURCE_THRESHOLDS.cpu_ok;
  const cpuWarn = r.role_cpu_warn ?? RESOURCE_THRESHOLDS.cpu_warn;
  const memWarn = RESOURCE_THRESHOLDS.mem_warn;
  const diskWarn = RESOURCE_THRESHOLDS.disk_warn;
  const cpu  = r.effective_cpu ?? r.cpu_pct;
  const mem  = r.mem_pct;          // used %
  const disk = r.disk_pct;
  const memAvailPct = mem != null ? +(100 - mem).toFixed(1) : null;

  const reasons = [];
  if (cpu != null && cpu >= cpuWarn)      reasons.push(`CPU ${cpu.toFixed(1)}% used ≥ ${cpuWarn}% threshold`);
  else if (cpu != null && cpu >= cpuOk)   reasons.push(`CPU ${cpu.toFixed(1)}% used ≥ ${cpuOk}% warn`);
  if (memAvailPct != null && memAvailPct <= (100 - memWarn)) {
    if (!_isDbMemExpected(mem != null ? (r.type || "") : "", mem ?? 0))
      reasons.push(`Memory ${memAvailPct.toFixed(1)}% available ≤ ${(100 - memWarn).toFixed(0)}% floor`);
    else
      reasons.push(`Memory ${memAvailPct.toFixed(1)}% available — within DB expected band (SGA/PGA)`);
  } else if (memAvailPct != null && memAvailPct <= (100 - RESOURCE_THRESHOLDS.mem_ok)) {
    if (!_isDbMemExpected(mem != null ? (r.type || "") : "", mem ?? 0))
      reasons.push(`Memory ${memAvailPct.toFixed(1)}% available ≤ ${(100 - RESOURCE_THRESHOLDS.mem_ok).toFixed(0)}% warn`);
    else
      reasons.push(`Memory ${memAvailPct.toFixed(1)}% available — within DB expected band (SGA/PGA)`);
  }
  if (disk != null && disk >= diskWarn)   reasons.push(`Disk ${disk.toFixed(1)}% used ≥ ${diskWarn}% threshold`);
  if (r.dual_pressure)                    reasons.push(`Dual pressure: CPU+Memory both critical`);
  if (r.agg_trap)                         reasons.push(`Aggregation trap: peak=${r.cpu_pct?.toFixed(1)}%, avg=${r.cpu_avg_pct?.toFixed(1)}%`);

  if (!reasons.length) {
    if (r.status === "Healthy") return `Healthy: all metrics within thresholds`;
    if (r.status === "Unknown") return `No metric data available from source`;
    return r.status || "Unknown";
  }
  return `${r.status}: ${reasons.join("; ")}`;
}

// ════════════════════════════════════════════════════════════════
//  METRICS DEEP DIVE — Critical-only + pattern detection
//  Filters out normal/moderate — shows only actionable PE findings
// ════════════════════════════════════════════════════════════════

let _deepDiveCharts = [];   // track Chart.js instances for cleanup
let _deepDiveData = null;   // last fetched timeseries payload

// ── Deep Dive metric visibility ──
// By default only core metrics (CPU, Memory, Disk BW) are shown.
// Extended metrics (IOPS, latency, cached/uncached) are behind a toggle.
let _ddShowExtendedMetrics = false;

// ── Deep Dive time range picker ──
let _deepDiveHoursBack = 24;
let _deepDiveCustomWindow = null; // {start_utc, end_utc} in ISO UTC

// ── Spike row drill-down ──────────────────────────────────────
// Called via onclick from spike table rows. Pads the spike window by 1h each
// side so the chart shows context around the anomaly. This is the key
// architectural decision: the API call uses the anomaly's timestamp, not now().
function openSpikeWindow(spikeStart, spikeEnd) {
  const PAD_MS = 60 * 60 * 1000; // 1h padding each side
  const t0 = new Date(spikeStart).getTime() - PAD_MS;
  const t1 = new Date(spikeEnd).getTime()   + PAD_MS;
  _deepDiveCustomWindow = {
    start_utc: new Date(t0).toISOString(),
    end_utc:   new Date(t1).toISOString(),
  };
  // Clear time-pill active state — we're now in custom window mode
  document.querySelectorAll(".dd-time-pill").forEach(p => p.classList.remove("dd-time-active"));
  if (typeof loadMetricsDeepDive === "function") loadMetricsDeepDive();
}

function setDeepDiveHours(el) {
  const hours = parseInt(el.dataset.ddHours) || 24;
  _deepDiveHoursBack = hours;
  _deepDiveCustomWindow = null;
  // Update active pill styling
  document.querySelectorAll(".dd-time-pill").forEach(p => p.classList.remove("dd-time-active"));
  el.classList.add("dd-time-active");
  // Auto-reload if data was already fetched
  if (_deepDiveData) loadMetricsDeepDive();
}

function setDeepDiveCustomRange() {
  const sEl = document.getElementById("dd-start-utc");
  const eEl = document.getElementById("dd-end-utc");
  if (!sEl || !eEl || !sEl.value || !eEl.value) {
    toast("warn", "Custom window", "Pick both Start and End UTC values.");
    return;
  }
  const s = new Date(sEl.value);
  const e = new Date(eEl.value);
  if (!(s instanceof Date) || isNaN(s) || !(e instanceof Date) || isNaN(e) || e <= s) {
    toast("error", "Custom window", "Invalid UTC range. End must be after Start.");
    return;
  }
  _deepDiveCustomWindow = { start_utc: s.toISOString(), end_utc: e.toISOString() };
  document.querySelectorAll(".dd-time-pill").forEach(p => p.classList.remove("dd-time-active"));
  loadMetricsDeepDive();
}

function clearDeepDiveCustomRange() {
  _deepDiveCustomWindow = null;
  const active = document.querySelector('.dd-time-pill[data-dd-hours="24"]');
  if (active) setDeepDiveHours(active);
}

function loadMetricsDeepDive() {
  if (!_lastFetchedVmIds || !_lastFetchedVmIds.length) {
    toast("warn", "No VMs", "Fetch Azure resource data first.");
    return;
  }

  const btn = document.getElementById("deepdive-refresh-btn");
  const loading = document.getElementById("deepdive-loading");
  const loadingText = document.getElementById("deepdive-loading-text");
  const chartsDiv = document.getElementById("deepdive-charts");
  const heatmapWrap = document.getElementById("deepdive-heatmap-wrap");
  const banner = document.getElementById("deepdive-spike-banner");

  // Read from deep dive time picker pills
  const hoursBack = _deepDiveHoursBack || 24;

  btn.disabled = true;
  btn.textContent = "Loading…";
  loading.classList.remove("hidden");
  const baselineNote = hoursBack >= 360 ? " (15-day baseline analysis)" : hoursBack >= 168 ? " (7-day pattern analysis)" : "";
  const customNote = _deepDiveCustomWindow ? " (custom UTC window)" : "";
  loadingText.textContent = `Fetching time-series for ${_lastFetchedVmIds.length} VM(s)${baselineNote}${customNote}…`;
  chartsDiv.innerHTML = "";
  heatmapWrap?.classList.add("hidden");
  banner?.classList.add("hidden");

  // Cleanup old Chart.js instances
  _deepDiveCharts.forEach(c => { try { c.destroy(); } catch(e){} });
  _deepDiveCharts = [];

  const t0 = performance.now();

  // 4-minute timeout — prevents infinite hang on slow Azure responses
  const _ddController = new AbortController();
  const _ddTimeout = setTimeout(() => _ddController.abort(), 240_000);

  const payload = { vm_ids: _lastFetchedVmIds, hours_back: hoursBack };
  if (_deepDiveCustomWindow?.start_utc && _deepDiveCustomWindow?.end_utc) {
    payload.start_utc = _deepDiveCustomWindow.start_utc;
    payload.end_utc = _deepDiveCustomWindow.end_utc;
  }

  fetch("/api/azure/timeseries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: _ddController.signal,
  })
  .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
  .then(data => {
    _deepDiveData = data;
    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    loading.classList.add("hidden");
    btn.disabled = false;
    const blLabel = data.baseline?.days_observed >= 15 ? ` · ${data.baseline.days_observed.toFixed(0)}d baseline ✓` : data.baseline?.days_observed >= 2 ? ` · ${data.baseline.days_observed.toFixed(0)}d` : "";
    const win = data.window || {};
    const grain = win.grain ? ` · ${win.grain}` : "";
    const tz = win.timezone ? ` · ${win.timezone}` : "";
    btn.textContent = `Refresh (${elapsed}s${blLabel}${grain}${tz})`;

    // Show/update custom window banner if a custom time range was applied
    _renderDeepDiveWindowBadge(data.window);

    // Phase 1: instant — lightweight banner + patterns
    _renderDeepDiveBanner(data.summary);
    _renderDeepDivePatterns(data.patterns || []);

    // Phase 2: deferred — stagger heavy Plotly heatmaps + Chart.js cards
    const _ddDeferred = [
      () => _renderDeepDiveHeatmap(data.heatmap),
      () => _renderDeepDiveMemoryHeatmap(data.vms),
      () => _renderDeepDiveCharts(data.vms, data.summary),
      () => {
        _updatePriorityFromDeepDive(data.vms);
        window.appData.deepDive = _buildDeepDiveSummary();
        _refreshExecResourceHealth();
        triggerGenerateFindings().catch(() => {});
      },
    ];
    let _ddi = 0;
    function _nextDD() {
      if (_ddi < _ddDeferred.length) {
        _ddDeferred[_ddi++]();
        requestAnimationFrame(_nextDD);
      }
    }
    requestAnimationFrame(_nextDD);
  })
  .catch(err => {
    loading.classList.add("hidden");
    btn.disabled = false;
    btn.textContent = "Load Time-Series";
    const msg = err.name === "AbortError" ? "Timeout — Azure took too long (4 min limit). Try fewer VMs." : err.message;
    toast("error", "Deep Dive Error", msg);
  })
  .finally(() => clearTimeout(_ddTimeout));
}

// ── Custom window badge: shows the active time range above charts ─────────
// Renders a visible pill when a custom UTC window is set, or shows the
// preset time range label so the user always knows what period the graphs cover.
function _renderDeepDiveWindowBadge(win) {
  // Find or create the badge element — insert it before the spike banner
  const banner = document.getElementById("deepdive-spike-banner");
  if (!banner) return;
  let badge = document.getElementById("deepdive-window-badge");
  if (!badge) {
    badge = document.createElement("div");
    badge.id = "deepdive-window-badge";
    badge.className = "flex items-center gap-2 flex-wrap rounded-lg px-3 py-2 mb-1";
    banner.parentNode.insertBefore(badge, banner);
  }

  const w = win || {};
  const startStr = w.start_utc ? new Date(w.start_utc).toLocaleString([], {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  }) : null;
  const endStr = w.end_utc ? new Date(w.end_utc).toLocaleString([], {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  }) : null;

  const isCustom = !!(_deepDiveCustomWindow?.start_utc);
  const grain = w.grain || "auto";
  const pts = w.data_points != null ? ` · ${w.data_points} data points` : "";

  if (isCustom && startStr && endStr) {
    badge.style.cssText = `background:rgba(139,92,246,0.08);border:1px solid rgba(139,92,246,0.3)`;
    badge.innerHTML =
      `<span class="text-sm">🗓</span>` +
      `<span class="text-[10px] font-bold text-purple-400 uppercase tracking-wider">Custom Window</span>` +
      `<span class="text-[10px] text-Cwhite font-mono">${_esc(startStr)}</span>` +
      `<span class="text-[9px] text-Cmuted">→</span>` +
      `<span class="text-[10px] text-Cwhite font-mono">${_esc(endStr)}</span>` +
      `<span class="text-[9px] text-Cmuted">· Grain: ${_esc(grain)}${pts} · Source: Azure Monitor UTC</span>` +
      `<button onclick="clearDeepDiveCustomRange()" class="ml-auto text-[9px] px-2 py-0.5 rounded border border-purple-400/40 text-purple-300 hover:bg-purple-500/10 transition">✕ Clear</button>`;
  } else {
    // Preset window — show which preset is active with data provenance
    const hours = _deepDiveHoursBack || 24;
    const label = hours >= 720 ? "30 days" : hours >= 360 ? "15 days" : hours >= 168 ? "7 days" :
                  hours >= 72 ? "3 days" : hours >= 48 ? "48 hours" : hours >= 24 ? "24 hours" :
                  hours >= 12 ? "12 hours" : hours >= 6 ? "6 hours" : `${hours} hour${hours > 1 ? "s" : ""}`;
    badge.style.cssText = `background:rgba(59,130,246,0.06);border:1px solid rgba(59,130,246,0.2)`;
    badge.innerHTML =
      `<span class="text-sm">📊</span>` +
      `<span class="text-[10px] font-semibold text-Cblue">Window: Last ${_esc(label)}</span>` +
      (endStr ? `<span class="text-[9px] text-Cmuted">up to ${_esc(endStr)}</span>` : "") +
      `<span class="text-[9px] text-Cmuted">· Grain: ${_esc(grain)}${pts} · Source: Azure Monitor UTC</span>`;
  }
}

// ── Banner: critical-only focus ───────────────────────────────
function _renderDeepDiveBanner(summary) {
  const banner = document.getElementById("deepdive-spike-banner");
  const icon = document.getElementById("deepdive-spike-icon");
  const title = document.getElementById("deepdive-spike-title");
  const detail = document.getElementById("deepdive-spike-detail");
  if (!banner) return;

  banner.classList.remove("hidden");

  if (summary.total_critical > 0) {
    banner.style.background = hexA(THEME.red, 0.12);
    banner.style.border = `1px solid ${hexA(THEME.red, 0.4)}`;
    icon.textContent = "🔴";
    title.textContent = `${summary.total_critical} Critical Anomal${summary.total_critical > 1 ? "ies" : "y"} — ${summary.affected_vms} VM${summary.affected_vms > 1 ? "s" : ""} Affected`;
    title.style.color = THEME.red;
    const blDays = _deepDiveData?.baseline?.days_observed || 0;
    const blNote = blDays >= 15 ? ` · ${blDays.toFixed(0)}-day baseline: pattern analysis active` : blDays >= 2 ? ` · ${blDays.toFixed(0)}-day observation (15d recommended for PE baseline)` : "";
    const w = _deepDiveData?.window || {};
    const tzNote = w.timezone ? ` Timezone source: ${w.timezone}.` : "";
    detail.textContent = `${summary.vm_count} VM(s) analyzed over ${summary.hours_back}h — only statistically significant deviations shown${blNote}.${tzNote}`;
  } else {
    banner.style.background = hexA(THEME.green, 0.08);
    banner.style.border = `1px solid ${hexA(THEME.green, 0.3)}`;
    icon.textContent = "✅";
    title.textContent = "Fleet Healthy — No Critical Anomalies";
    title.style.color = THEME.green;
    const blDays = _deepDiveData?.baseline?.days_observed || 0;
    const blNote = blDays >= 15 ? ` ${blDays.toFixed(0)}-day baseline confirms stability.` : blDays >= 2 ? ` ${blDays.toFixed(0)}-day observation — extend to 15d for full PE confidence.` : "";
    const w = _deepDiveData?.window || {};
    const tzNote = w.timezone ? ` Timezone source: ${w.timezone}.` : "";
    detail.textContent = `${summary.vm_count} VM(s) analyzed over ${summary.hours_back}h — all metrics within normal operating range.${blNote}${tzNote}`;
  }
}

// ── Pattern Detection — silent engine (no visible UI) ─────────
// Pattern computation feeds Fleet Diagnosis cards and export only.
// The user sees conclusions, never raw computation.
let _deepDivePatterns = [];   // stored for export

function _renderDeepDivePatterns(patterns) {
  _deepDivePatterns = patterns || [];
  // No visible rendering — patterns feed into export report only.
}

// ── Heatmap column binning ────────────────────────────────────
// Reduces a time-axis heatmap to at most 120 display columns by averaging
// adjacent bins and using the midpoint timestamp. For a 360h/1h-grain window
// this cuts Plotly's rendering work from 360 columns to 90 (4h bins) — a 4×
// reduction in SVG/canvas operations with no visible loss of shape.
const _HEATMAP_MAX_COLS = 120;
function _binHeatmap(tDates, zMatrix) {
  if (tDates.length <= _HEATMAP_MAX_COLS) return { tDates, zMatrix };
  const binSize = Math.ceil(tDates.length / _HEATMAP_MAX_COLS);
  const bT = [], bZ = zMatrix.map(() => []);
  for (let i = 0; i < tDates.length; i += binSize) {
    const hi = Math.min(i + binSize, tDates.length);
    bT.push(tDates[Math.floor((i + hi - 1) / 2)]);
    for (let r = 0; r < zMatrix.length; r++) {
      const vals = zMatrix[r].slice(i, hi).filter(v => v != null && !isNaN(v));
      bZ[r].push(vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : null);
    }
  }
  return { tDates: bT, zMatrix: bZ };
}

// ── Fleet CPU Heatmap (Plotly) ────────────────────────────────
function _renderDeepDiveHeatmap(heatmap) {
  const wrap = document.getElementById("deepdive-heatmap-wrap");
  const container = document.getElementById("deepdive-heatmap");
  if (!wrap || !container || !heatmap || !heatmap.vms.length) return;

  wrap.classList.remove("hidden");

  const vmNames = heatmap.vms.map(v => v.name);
  const zRaw = heatmap.vms.map(v => v.values.map(x => x ?? 0));

  // Bin to ≤120 display columns — reduces Plotly rendering work 3-6× on long windows
  const { tDates, zMatrix: z } = _binHeatmap(
    heatmap.timestamps.map(t => new Date(t)),
    zRaw
  );

  const trace = {
    z: z,
    x: tDates,
    y: vmNames,
    type: "heatmap",
    colorscale: [
      [0,    "#0d1526"],
      [0.15, "#1e3a5f"],
      [0.35, "#3b82f6"],
      [0.55, "#f59e0b"],
      [0.75, "#ef4444"],
      [1.0,  "#dc2626"],
    ],
    zmin: 0,
    zmax: 100,
    colorbar: {
      title: { text: "CPU %", font: { color: THEME.muted, size: 10 } },
      tickfont: { color: THEME.muted, size: 9 },
      thickness: 12,
      tickvals: [0, 25, 50, 75, 90, 100],
    },
    hoverongaps: false,
    hovertemplate: "<b>%{y}</b><br>%{x|%b %d %I:%M %p}<br>CPU: %{z:.1f}%<extra></extra>",
  };

  // Threshold reference lines at 75% (warning) and 90% (critical)
  const thresholdShapes = [
    { type: "line", x0: 0, x1: 1, xref: "paper", y0: -0.5, y1: -0.5, yref: "y",
      line: { color: "transparent", width: 0 } }, // anchor
  ];

  const thresholdAnnotations = [
    { x: 1.01, xref: "paper", y: 0, yref: "y", text: "⚠ 75%",
      showarrow: false, font: { color: THEME.amber, size: 8 }, xanchor: "left" },
    { x: 1.01, xref: "paper", y: vmNames.length - 1, yref: "y", text: "🔴 90%",
      showarrow: false, font: { color: THEME.red, size: 8 }, xanchor: "left" },
  ];

  const layout = _plotlyBaseLayout({
    margin: { l: 140, r: 60, t: 28, b: 40 },
    height: Math.max(160, vmNames.length * 30 + 70),
    title: {
      text: `Fleet CPU Utilisation · Grain: ${_deepDiveData?.window?.grain || "1h avg"} · Source: Azure Monitor UTC`,
      font: { size: 9, color: THEME.muted },
      x: 0.01, xanchor: "left",
    },
    xaxis: {
      type: "date",
      tickfont: { color: THEME.muted, size: 9 },
      tickangle: -45,
      nticks: 20,
      tickformat: "%b %d %I%p",
      hoverformat: "%b %d %I:%M %p",
      rangeslider: { visible: false },
    },
    yaxis: {
      tickfont: { color: THEME.muted, size: 10 },
      autorange: "reversed",
    },
    shapes: thresholdShapes,
  });

  // Plotly.react() diffs the existing plot on re-renders (refresh, drill-down return)
  // instead of purge+rebuild — much faster on second+ renders.
  Plotly.react(container, [trace], layout, _plotlyConfig());

  // Only wire click/toolbar/sync once — guard prevents duplicate listeners on re-render
  if (!container.dataset.ddHeatInit) {
    container.dataset.ddHeatInit = "1";
    container.on("plotly_click", (data) => {
      if (!data.points || !data.points.length) return;
      const vmName = data.points[0].y;
      const detailCard = document.querySelector(`[data-vm-detail="${vmName}"]`);
      if (detailCard) {
        detailCard.scrollIntoView({ behavior: "smooth", block: "center" });
        detailCard.style.boxShadow = `0 0 20px ${hexA(THEME.blue, 0.4)}`;
        setTimeout(() => { detailCard.style.boxShadow = ""; }, 2000);
      }
    });
    _addChartToolbar(wrap, container, () => {
      let csv = "VM,Timestamp,CPU_Pct\n";
      heatmap.vms.forEach(vm => {
        heatmap.timestamps.forEach((t, ti) => { csv += `${vm.name},${t},${vm.values[ti] ?? ""}\n`; });
      });
      return csv;
    });
    _registerPlotlySync(container);
  }
}

// ── GAP 6: Memory Concurrency Heatmap ─────────────────────────
function _renderDeepDiveMemoryHeatmap(vms) {
  const wrap = document.getElementById("deepdive-mem-heatmap-wrap");
  const container = document.getElementById("deepdive-mem-heatmap");
  if (!wrap || !container || !vms || typeof Plotly === "undefined") return;

  // Extract "Available Memory Percentage" series from all VMs
  const vmEntries = Object.entries(vms);
  const vmNames = [];
  const allSeries = [];

  for (const [vmName, vmData] of vmEntries) {
    const series = (vmData.series || {})["Available Memory Percentage"];
    if (!series || !series.length) continue;
    // Annotate DB servers so memory 80-92% is clearly expected
    const role = _inferRole(vmName);
    const roleTag = _isDbRole(role) ? " [DB]" : "";
    vmNames.push(vmName + roleTag);
    // Show as Azure does: Available Memory % (higher = more headroom)
    allSeries.push(series.map(p => ({ t: p.t, v: p.v || 0 })));
  }

  if (!vmNames.length) return;

  wrap.classList.remove("hidden");

  // Build time buckets (use first VM's timestamps as reference)
  const refTimes = allSeries[0].map(p => p.t);

  // Build z-matrix (VMs × time) — O(N+M) Map lookup, not O(N×M) nested scan.
  // All series share Azure-issued timestamps so direct key match is reliable.
  const z = allSeries.map(series => {
    const tsMap = new Map(series.map(p => [new Date(p.t).getTime(), p.v]));
    return refTimes.map(rt => tsMap.get(new Date(rt).getTime()) ?? 0);
  });

  // Bin to ≤120 display columns for Plotly (full-res z kept for analysis + CSV)
  const { tDates, zMatrix: zPlotly } = _binHeatmap(
    refTimes.map(t => new Date(t)),
    z.map(row => [...row])
  );

  // Detect shared batch patterns: time slots where >50% VMs have high memory
  const nVms = vmNames.length;
  const batchFindings = [];
  for (const th of [{pct:80,label:"≥80%"},{pct:50,label:"≥50%"}]) {
    const count = refTimes.filter((_, ti) => {
      const highCount = z.filter(row => row[ti] >= th.pct).length;
      return highCount >= nVms * 0.5;
    }).length;
    if (count > 2) batchFindings.push({...th, count});
  }

  const trace = {
    z: zPlotly,
    x: tDates,
    y: vmNames,
    type: "heatmap",
    zmin: 0,
    zmax: 100,
    // DB-aware color scale: Oracle SGA/PGA keeps 8-20% available — expected, not alarming.
    // Red (<8%) = critical below DB floor | Orange (8%) = at DB floor
    // Teal (12-20%) = Oracle normal operating band | Green (30%+) = healthy
    colorscale: [
      [0,    "#dc2626"],   // 0% avail → critical (>95% used)
      [0.05, "#ef4444"],   // 5% avail → critical
      [0.08, "#f97316"],   // 8% avail → at DB floor (warn)
      [0.12, "#06b6d4"],   // 12% avail → Oracle expected band start (teal)
      [0.20, "#0e7490"],   // 20% avail → Oracle expected band end (dark teal)
      [0.30, "#10b981"],   // 30% avail → healthy
      [1.0,  "#0d1526"],   // 100% avail → near-idle (dark)
    ],
    colorbar: {
      title: { text: "Avail Memory %", font: { color: THEME.muted, size: 10 } },
      tickfont: { color: THEME.muted, size: 9 },
      thickness: 12,
      tickvals: [0, 8, 12, 20, 30, 100],
      ticktext: ["0%", "8% (crit)", "12% DB↓", "20% DB↑", "30% ok", "100%"],
    },
    hoverongaps: false,
    hovertemplate: "<b>%{y}</b><br>%{x|%b %d %I:%M %p}<br>Available Memory: %{z:.1f}%<extra></extra>",
  };

  const layout = _plotlyBaseLayout({
    margin: { l: 140, r: 60, t: 28, b: 40 },
    height: Math.max(160, vmNames.length * 30 + 70),
    title: {
      text: `Fleet Avail Memory % · Teal = DB expected (8–20%) · Red = critical (<8%) · Grain: ${_deepDiveData?.window?.grain || "1h avg"}`,
      font: { size: 9, color: THEME.muted },
      x: 0.01, xanchor: "left",
    },
    xaxis: {
      type: "date",
      tickfont: { color: THEME.muted, size: 9 },
      tickangle: -45,
      nticks: 20,
      tickformat: "%b %d %I%p",
      hoverformat: "%b %d %I:%M %p",
    },
    yaxis: {
      tickfont: { color: THEME.muted, size: 10 },
      autorange: "reversed",
    },
  });

  // Plotly.react() diffs existing plot on re-renders — no full SVG teardown
  Plotly.react(container, [trace], layout, _plotlyConfig());

  // V2: Single merged batch pattern banner — distinguish chronic from periodic
  if (batchFindings.length) {
    const worst = batchFindings[0];
    const totalSlots = refTimes.length;
    const indicator = document.createElement("div");
    indicator.className = "text-[10px] mt-2 px-3 py-1.5 rounded-lg border";

    // When ≥95% of time slots breach the highest threshold → chronic condition, not batch
    const isChronic = worst.count >= totalSlots * 0.95;

    // Count DB servers — their high memory is expected, not a batch pattern
    const dbCount = vmNames.filter(n => n.includes("[DB]")).length;
    const dbNote = dbCount > 0 ? ` (${dbCount} DB server${dbCount > 1 ? 's' : ''} — high memory is expected SGA/PGA allocation)` : "";

    if (isChronic) {
      indicator.style.cssText = `color:${THEME.red};border-color:${hexA(THEME.red,0.4)};background:${hexA(THEME.red,0.08)}`;
      indicator.textContent = `🔴 Persistent fleet-wide memory saturation — not batch-driven, chronic condition. All ${worst.count} observed time slots exceed ${worst.label} threshold on ≥50% of servers.${dbNote} ${dbCount < nVms ? 'This requires capacity expansion, not schedule adjustment.' : 'Verify non-DB servers before concluding capacity issue.'}`;
    } else {
      indicator.style.cssText = `color:${THEME.amber};border-color:${hexA(THEME.amber,0.3)};background:${hexA(THEME.amber,0.06)}`;
      const subtext = batchFindings.map(f => `${f.count} slots at ${f.label}`).join(" · ");
      indicator.textContent = `⚡ Shared batch pattern detected: ${worst.count} time slots where ≥50% of servers show memory ${worst.label} — persistent concurrent batch overlap across observation window.${dbNote} (${subtext})`;
    }
    // Remove any existing indicator from a previous render before appending
    wrap.querySelector(".dd-mem-batch-indicator")?.remove();
    indicator.classList.add("dd-mem-batch-indicator");
    wrap.appendChild(indicator);
  }

  // Only wire click/toolbar/sync once — guard prevents duplicate listeners on re-render
  if (!container.dataset.ddMemHeatInit) {
    container.dataset.ddMemHeatInit = "1";
    container.on("plotly_click", (data) => {
      if (!data.points || !data.points.length) return;
      const vmName = data.points[0].y;
      const detailCard = document.querySelector(`[data-vm-detail="${vmName}"]`);
      if (detailCard) {
        detailCard.scrollIntoView({ behavior: "smooth", block: "center" });
        detailCard.style.boxShadow = `0 0 20px ${hexA(THEME.cyan, 0.4)}`;
        setTimeout(() => { detailCard.style.boxShadow = ""; }, 2000);
      }
    });
    _addChartToolbar(wrap, container, () => {
      let csv = "VM,Timestamp,Mem_Avail_Pct\n";
      vmNames.forEach((vm, vi) => {
        refTimes.forEach((t, ti) => { csv += `${vm},${t},${z[vi][ti]?.toFixed(1) ?? ""}\n`; });
      });
      return csv;
    });
    _registerPlotlySync(container);
  }
}

// Unit-aware peak formatter — converts raw bytes to GB, others to %
function _formatPeak(metricKey, value) {
  const mk = metricKey || "";
  if (metricKey === "Available Memory Bytes" || (typeof value === 'number' && value > 1e6)) {
    return (value / 1073741824).toFixed(1) + " GB";
  }
  if (mk.includes("Latency")) {
    return `${Number(value).toFixed(1)} ms`;
  }
  if (mk.includes("Operations/Sec")) {
    return `${Number(value).toFixed(1)} ops/s`;
  }
  // Show memory as Available % — matches Azure Portal convention
  const isMemAvail = (metricKey || "").includes("Memory") && !(metricKey || "").includes("Bytes");
  if (isMemAvail) {
    return value.toFixed(1) + "% available";
  }
  return value + "%";
}

// Format deviation text with correct sign/direction for the metric
function _formatDeviation(spike) {
  const mn = (spike.metric || "").toLowerCase();
  const isMemory = mn.includes("memory") && !mn.includes("bytes");
  const isLatency = mn.includes("latency");
  const isOps = mn.includes("operations/sec") || mn.includes("iops");
  const zAbs = Math.abs(spike.z_score).toFixed(1);
  const sevWord = zAbs >= 4 ? "extreme" : zAbs >= 3 ? "significant" : "notable";
  if (isMemory) {
    // Memory available dropped — show as "below mean" and convert mean to used
    const usedMean = (100 - spike.mean).toFixed(1);
    return `${sevWord} deviation below avail baseline (avg ${spike.mean}% avail, ${usedMean}% used)`;
  }
  if (isLatency) {
    return `${sevWord} latency elevation (avg ${Number(spike.mean).toFixed(2)} ms)`;
  }
  if (isOps) {
    return `${sevWord} I/O rate deviation (avg ${Number(spike.mean).toFixed(2)} ops/s)`;
  }
  return `${sevWord} deviation above baseline (avg ${spike.mean}%)`;
}

// Confidence tier badge
function _confBadge(conf) {
  if (!conf) return "";
  const colors = {
    "observed":    { c: THEME.green,  bg: THEME.green },
    "inferred":    { c: THEME.amber,  bg: THEME.amber },
    "weak-signal": { c: THEME.red,    bg: THEME.red },
  };
  const cc = colors[conf] || colors["weak-signal"];
  return `<span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded ml-1" style="color:${cc.c};background:${hexA(cc.bg,0.12)}">${conf}</span>`;
}

// ── Per-VM Time-Series Charts — grouped server cards (GAP 1) ──
function _renderDeepDiveCharts(vms, summary) {
  const chartsDiv = document.getElementById("deepdive-charts");
  if (!chartsDiv) return;

  const metricConfig = [
    { key: "Percentage CPU",                    label: "CPU %",            color: THEME.blue,   warn: 80, core: true },
    { key: "Available Memory Percentage",       label: "Available Mem %",  color: THEME.cyan,   warn: 20, core: true },
    { key: "Available Memory Bytes",            label: "Available Memory Bytes", color: THEME.cyan, warn: 0, unit: "bytes" },
    { key: "OS Disk Bandwidth Consumed Percentage",   label: "OS Disk BW %",   color: THEME.amber,  warn: 80, core: true },
    { key: "Data Disk Bandwidth Consumed Percentage", label: "Data Disk BW %", color: THEME.purple, warn: 80, core: true },
    { key: "OS Disk IOPS Consumed Percentage",  label: "OS Disk IOPS %", color: "#f97316", warn: 80 },
    { key: "Data Disk IOPS Consumed Percentage",label: "Data Disk IOPS %", color: "#a855f7", warn: 80 },
    { key: "VM Cached IOPS Consumed Percentage", label: "VM Cached IOPS %", color: "#0ea5e9", warn: 80 },
    { key: "VM Uncached IOPS Consumed Percentage", label: "VM Uncached IOPS %", color: "#14b8a6", warn: 80 },
    { key: "VM Cached Bandwidth Consumed Percentage", label: "VM Cached BW %", color: "#22c55e", warn: 80 },
    { key: "VM Uncached Bandwidth Consumed Percentage", label: "VM Uncached BW %", color: "#eab308", warn: 80 },
    { key: "OS Disk Latency",                   label: "OS Disk Latency (ms)", color: "#f43f5e", warn: 15, unit: "ms" },
    { key: "Data Disk Latency",                 label: "Data Disk Latency (ms)", color: "#ec4899", warn: 15, unit: "ms" },
    { key: "OS Disk Read Operations/Sec",       label: "OS Read IOPS", color: "#60a5fa", warn: 0, unit: "ops/s" },
    { key: "OS Disk Write Operations/Sec",      label: "OS Write IOPS", color: "#93c5fd", warn: 0, unit: "ops/s" },
    { key: "Data Disk Read Operations/Sec",     label: "Data Read IOPS", color: "#c084fc", warn: 0, unit: "ops/s" },
    { key: "Data Disk Write Operations/Sec",    label: "Data Write IOPS", color: "#d8b4fe", warn: 0, unit: "ops/s" },
  ];

  // Separate VMs with critical spikes from clean VMs
  const criticalVms = [];
  const cleanVms = [];
  for (const [vmName, vmData] of Object.entries(vms)) {
    const spikes = vmData.spikes || {};
    const hasCritical = Object.values(spikes).some(arr => arr.length > 0);
    if (hasCritical) criticalVms.push([vmName, vmData]);
    else cleanVms.push([vmName, vmData]);
  }

  // ── Render critical VMs as grouped server cards ──
  if (criticalVms.length) {
    const critHeader = document.createElement("div");
    critHeader.className = "flex items-center gap-2 mt-2";
    const _critWindowLabel = _deepDiveCustomWindow?.start_utc
      ? (() => {
          const s = new Date(_deepDiveCustomWindow.start_utc);
          const e = new Date(_deepDiveCustomWindow.end_utc);
          const hrs = ((e - s) / 3600000).toFixed(0);
          return `${hrs}h custom window`;
        })()
      : `${_deepDiveHoursBack}h window`;
    critHeader.innerHTML = `<span class="text-sm">🚨</span><h4 class="text-[10px] font-bold uppercase tracking-widest text-red-400" style="letter-spacing:0.15em">Requires Investigation — ${criticalVms.length} Server${criticalVms.length > 1 ? "s" : ""}</h4><span class="text-[9px] text-Cmuted ml-auto">${_esc(_critWindowLabel)}</span>`;
    chartsDiv.appendChild(critHeader);

    // E2: Sort/filter controls
    const controls = document.createElement("div");
    controls.className = "flex items-center gap-3 flex-wrap py-2";
    controls.innerHTML = `
      <div class="flex items-center gap-1.5">
        <span class="text-[9px] text-Cmuted font-semibold">Sort</span>
        <select id="dd-sort-select" class="bg-Cbg border border-Cborder rounded-md px-2 py-0.5 text-[10px] text-Cwhite focus:outline-none focus:border-Cblue cursor-pointer">
          <option value="mem">MEM AVAIL % ↓</option>
          <option value="spikes">Spike Count ↓</option>
          <option value="latest">Latest Spike ↓</option>
          <option value="name">Name A→Z</option>
        </select>
      </div>
      <div class="flex items-center gap-1.5">
        <span class="text-[9px] text-Cmuted font-semibold">Min %</span>
        <input id="dd-threshold-input" type="range" min="0" max="100" value="0" class="w-20 h-1 accent-blue-500 cursor-pointer">
        <span id="dd-threshold-label" class="text-[9px] text-Cmuted font-mono w-8">0%</span>
      </div>
      <div class="flex items-center gap-1">
        <span class="text-[9px] text-Cmuted font-semibold">Type</span>
        <button data-dd-type="all" class="dd-type-pill dd-type-active px-1.5 py-0.5 rounded text-[9px] font-semibold border border-Cborder/50 text-Cmuted">All</button>
        <button data-dd-type="db" class="dd-type-pill px-1.5 py-0.5 rounded text-[9px] font-semibold border border-Cborder/50 text-Cmuted">DB</button>
        <button data-dd-type="app" class="dd-type-pill px-1.5 py-0.5 rounded text-[9px] font-semibold border border-Cborder/50 text-Cmuted">APP</button>
        <button data-dd-type="sre" class="dd-type-pill px-1.5 py-0.5 rounded text-[9px] font-semibold border border-Cborder/50 text-Cmuted">SRE</button>
      </div>
      <div class="flex items-center gap-1.5 ml-auto">
        <label class="flex items-center gap-1.5 cursor-pointer select-none" title="Show IOPS, latency, cached/uncached bandwidth metrics in charts and tables">
          <input type="checkbox" id="dd-extended-metrics-toggle" class="accent-blue-500 w-3 h-3 cursor-pointer"${_ddShowExtendedMetrics ? " checked" : ""}>
          <span class="text-[9px] text-Cmuted font-semibold">Extended Metrics</span>
        </label>
      </div>
    `;
    chartsDiv.appendChild(controls);

    // Grid of summary cards
    const grid = document.createElement("div");
    grid.id = "dd-server-grid";
    grid.className = "grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3";
    chartsDiv.appendChild(grid);

    // Store card data for sort/filter
    const cardDataArr = criticalVms.map(([vmName, vmData]) => {
      const sp = vmData.spikes || {};
      const st = vmData.stats || {};
      let spikeCount = 0, latestSpike = 0;
      for (const arr of Object.values(sp)) {
        spikeCount += arr.length;
        for (const s of arr) { latestSpike = Math.max(latestSpike, new Date(s.peak_time).getTime()); }
      }
      const _memSt = st["Available Memory Percentage"];
      const memAvail = _memSt
        ? (_memSt.min_anomalous && _memSt.p5 != null ? _memSt.p5 : _memSt.min ?? 100)
        : 100;
      const memPressure = 100 - memAvail;  // for sort/filter comparison
      const role = _inferRole(vmName);
      return { vmName, vmData, memAvail, memPressure, spikeCount, latestSpike, role };
    });

    function renderFilteredGrid() {
      const sortBy = document.getElementById("dd-sort-select")?.value || "mem";
      const threshold = parseInt(document.getElementById("dd-threshold-input")?.value || "0");
      const activePills = [...controls.querySelectorAll(".dd-type-pill.dd-type-active")].map(b => b.dataset.ddType);
      const showAll = activePills.includes("all") || activePills.length === 0;

      let filtered = cardDataArr.filter(d => d.memPressure >= threshold);
      if (!showAll) {
        filtered = filtered.filter(d => {
          if (activePills.includes("db") && d.role.includes("DB")) return true;
          if (activePills.includes("app") && (d.role === "APP" || d.role === "SERVER")) return true;
          if (activePills.includes("sre") && d.role === "SRE") return true;
          return false;
        });
      }

      filtered.sort((a, b) => {
        if (sortBy === "mem") return a.memAvail - b.memAvail;  // lowest available first (most pressure)
        if (sortBy === "spikes") return b.spikeCount - a.spikeCount;
        if (sortBy === "latest") return b.latestSpike - a.latestSpike;
        return a.vmName.localeCompare(b.vmName);
      });

      // Filter metricConfig based on extended metrics toggle
      const activeMetricConfig = _ddShowExtendedMetrics ? metricConfig : metricConfig.filter(m => m.core);

      // Destroy existing Chart.js instances before clearing DOM
      _deepDiveCharts.forEach(c => { try { c.destroy(); } catch(e){} });
      _deepDiveCharts = [];
      grid.innerHTML = "";
      // Stagger card rendering across frames to avoid Firefox slowdown
      if (!filtered.length) {
        grid.innerHTML = `<div class="col-span-3 text-center text-Cmuted text-xs py-4">No servers match current filters.</div>`;
        return;
      }
      let _ci = 0;
      const BATCH_SIZE = 3; // render 3 cards per frame
      function _nextBatch() {
        const end = Math.min(_ci + BATCH_SIZE, filtered.length);
        for (; _ci < end; _ci++) {
          _renderVmServerCard(filtered[_ci].vmName, filtered[_ci].vmData, activeMetricConfig, grid);
        }
        if (_ci < filtered.length) requestAnimationFrame(_nextBatch);
      }
      requestAnimationFrame(_nextBatch);
    }

    renderFilteredGrid();

    // Wire controls
    document.getElementById("dd-sort-select")?.addEventListener("change", renderFilteredGrid);
    const thresholdInput = document.getElementById("dd-threshold-input");
    let _thresholdDebounce = null;
    thresholdInput?.addEventListener("input", () => {
      document.getElementById("dd-threshold-label").textContent = thresholdInput.value + "%";
      if (_thresholdDebounce) clearTimeout(_thresholdDebounce);
      _thresholdDebounce = setTimeout(renderFilteredGrid, 200);
    });
    controls.querySelectorAll(".dd-type-pill").forEach(btn => {
      btn.addEventListener("click", () => {
        const isAll = btn.dataset.ddType === "all";
        if (isAll) {
          // All resets everything
          controls.querySelectorAll(".dd-type-pill").forEach(b => b.classList.remove("dd-type-active"));
          btn.classList.add("dd-type-active");
        } else {
          // Toggle this pill on/off
          btn.classList.toggle("dd-type-active");
          // Remove All when specific types selected
          controls.querySelector('.dd-type-pill[data-dd-type="all"]')?.classList.remove("dd-type-active");
          // If nothing selected, re-activate All
          const anyActive = controls.querySelector('.dd-type-pill.dd-type-active');
          if (!anyActive) {
            controls.querySelector('.dd-type-pill[data-dd-type="all"]')?.classList.add("dd-type-active");
          }
        }
        renderFilteredGrid();
      });
    });

    // Extended Metrics toggle — re-renders grid + detail when toggled
    const extToggle = document.getElementById("dd-extended-metrics-toggle");
    extToggle?.addEventListener("change", () => {
      _ddShowExtendedMetrics = extToggle.checked;
      renderFilteredGrid();
      // Destroy any Chart.js instances in the detail card before clearing DOM,
      // otherwise orphaned canvas contexts accumulate and stall Firefox GC.
      _deepDiveCharts.forEach(c => { try { c.destroy(); } catch(e){} });
      _deepDiveCharts = [];
      const detailArea = document.getElementById("deepdive-detail-area");
      if (detailArea) detailArea.innerHTML = "";
    });

    // Expanded detail area (empty until card clicked)
    const detailArea = document.createElement("div");
    detailArea.id = "deepdive-detail-area";
    detailArea.className = "space-y-4";
    chartsDiv.appendChild(detailArea);
  }

  // ── Clean VMs: compact summary ──
  if (cleanVms.length) {
    const cleanCard = document.createElement("div");
    cleanCard.className = "rounded-xl border border-green-500/20 bg-green-500/5 p-4 space-y-2";

    let cleanRows = cleanVms.map(([vmName, vmData]) => {
      const stats = vmData.stats || {};
      const cpuS = stats["Percentage CPU"];
      const memS = stats["Available Memory Percentage"];
      const cpuText = cpuS ? `avg ${cpuS.mean}% · max ${cpuS.max}%` : "—";
      const memText = memS ? `avail ${memS.mean}% · min ${memS.min}%` : "—";
      return `<tr class="border-t border-Cborder/20">
        <td class="py-1.5 pr-3 text-[10px] font-semibold text-Cwhite">${escapeHtml(vmName)}</td>
        <td class="py-1.5 pr-3 text-[10px] text-Cblue">${cpuText}</td>
        <td class="py-1.5 pr-3 text-[10px] text-Ccyan">${memText}</td>
        <td class="py-1.5 text-[10px] text-green-400">✓ Normal</td>
      </tr>`;
    }).join("");

    cleanCard.innerHTML = `
      <div class="flex items-center gap-2">
        <span class="text-sm">✅</span>
        <h4 class="text-[10px] font-bold uppercase tracking-widest text-green-400" style="letter-spacing:0.15em">Healthy — ${cleanVms.length} Server${cleanVms.length > 1 ? "s" : ""} Normal</h4>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left"><thead>
          <tr class="text-[9px] text-Cmuted uppercase tracking-wider">
            <th class="pb-1 pr-3">Server</th><th class="pb-1 pr-3">CPU</th>
            <th class="pb-1 pr-3">Memory</th><th class="pb-1">Status</th>
          </tr>
        </thead><tbody>${cleanRows}</tbody></table>
      </div>
    `;
    chartsDiv.appendChild(cleanCard);
  }

  if (!chartsDiv.children.length) {
    chartsDiv.innerHTML = `<div class="text-center text-Cmuted text-xs py-6">No time-series data returned. Ensure metrics are enabled on the selected VMs.</div>`;
  }
}

// ── I2: Role inference — prefer backend type from _discoveredVMs,
//    fall back to hostname regex when backend type unavailable ──
function _inferRole(vmName) {
  // 1. Check backend-assigned type (authoritative — uses Azure tags + hostname)
  const n = (vmName || "").toLowerCase();
  if (typeof _discoveredVMs !== "undefined" && _discoveredVMs.length) {
    const match = _discoveredVMs.find(v =>
      (v.name || "").toLowerCase() === n || (v.resource_id || "").toLowerCase().endsWith("/" + n)
    );
    if (match?.type) return match.type;
  }
  // 2. Fallback: hostname substring checks (aligned with backend _infer_server_type)
  //    Check specific keywords — do NOT match broad prefixes like "prbe" which
  //    catch app servers (prbe471502001) alongside DB servers (prbe471503001).
  if (/db|ora|sql|pg|mysql|mongo|redis|cosmos|warehouse|dw/.test(n)) return "DB";
  if (/sre|batch|sch|job|worker|cron|ctm|ctrl|infra|ops|mgmt|monitor/.test(n)) return "SRE";
  if (/app|web|ui|api|gateway/.test(n)) return "APP";
  return "SERVER";
}

function _inferEnv(vmName) {
  const n = (vmName || "").toLowerCase();
  if (/\b(prod|prd)\b|[-_](prod|prd)/.test(n)) return "PROD";
  if (/\b(test|tst|uat|qa)\b|[-_](test|tst|uat|qa)/.test(n)) return "TEST";
  if (/\b(dev|stg|staging)\b|[-_](dev|stg|staging)/.test(n)) return "DEV";
  if (n[0] === 'p') return "PROD";
  if (n[0] === 't') return "TEST";
  if (n[0] === 'd') return "DEV";
  return "";
}

// ── Server card (collapsed) — trend arrow, role tag, projected breach ──
function _renderVmServerCard(vmName, vmData, metricConfig, container) {
  const spikes = vmData.spikes || {};
  const stats = vmData.stats || {};
  const metrics = vmData.series || {};

  // Count critical events + severity
  let criticalCount = 0;
  let highestSev = 0;
  let hasSustained = false;
  let lastBreach = null;
  for (const [mk, arr] of Object.entries(spikes)) {
    criticalCount += arr.length;
    for (const s of arr) {
      highestSev = Math.max(highestSev, s.z_score || 0);
      if ((s.severity || "").includes("sustained")) hasSustained = true;
      const bt = new Date(s.peak_time);
      if (!lastBreach || bt > lastBreach) lastBreach = bt;
    }
  }

  // Determine dominant metric — single large callout
  // Use P95 for headline (representative peak), not max (single-point outlier)
  const cpuStats = stats["Percentage CPU"];
  const cpuP95 = cpuStats?.p95 ?? cpuStats?.max ?? 0;
  const cpuMax = cpuStats?.max ?? 0;
  // Memory: show lowest available % (P5 or min) — lower = more pressure
  const memAvailStats = stats["Available Memory Percentage"];
  const memLowest = memAvailStats
    ? (memAvailStats.min_anomalous && memAvailStats.p5 != null
      ? memAvailStats.p5
      : memAvailStats.min ?? 100)
    : 100;
  const diskStats = stats["OS Disk Bandwidth Consumed Percentage"];
  const diskP95 = diskStats?.p95 ?? diskStats?.max ?? 0;
  let domLabel, domVal, domColor, domStatType;
  // Pick the metric under most pressure:
  // CPU/Disk: highest % = worst; Memory: lowest available % = worst
  // Convert to a common "pressure" score for comparison
  const memPressure = 100 - memLowest;  // high pressure = low available
  if (memPressure >= cpuP95 && memPressure >= diskP95) {
    domLabel = "MEM"; domVal = memLowest; domColor = THEME.cyan; domStatType = "min avail";
  } else if (cpuP95 >= diskP95) {
    domLabel = "CPU"; domVal = cpuP95; domColor = THEME.blue; domStatType = "P95";
  } else {
    domLabel = "DISK"; domVal = diskP95; domColor = THEME.amber; domStatType = "P95";
  }
  // If P95 < max by a large margin, note the outlier
  const domMaxVal = domLabel === "CPU" ? cpuMax : domLabel === "DISK" ? (diskStats?.max ?? 0) : 0;
  const domHasOutlier = domMaxVal > domVal * 1.3 && domMaxVal > domVal + 10;

  const card = document.createElement("div");
  card.className = "rounded-xl border p-3 cursor-pointer transition hover:scale-[1.01] group";
  card.style.borderColor = hexA(THEME.red, 0.3);
  card.style.background = hexA(THEME.red, 0.04);

  // 100% saturation → pulsing red border
  // For CPU/Disk: domVal >= 100; For MEM (available): domVal <= 0
  if (domLabel === "MEM" ? domVal <= 0 : domVal >= 100) {
    card.classList.add("saturated-pulse");
  }

  // Sparkline from dominant metric series — last 6 hours only
  const domKey = domLabel === "MEM" ? "Available Memory Percentage" : (domLabel === "DISK" ? "OS Disk Bandwidth Consumed Percentage" : "Percentage CPU");
  const fullSeries = metrics[domKey] || metrics["Percentage CPU"] || [];
  const now = Date.now();
  const sixHoursMs = 6 * 60 * 60 * 1000;
  const recentSeries = fullSeries.filter(p => (now - new Date(p.t).getTime()) <= sixHoursMs);
  const sparkSource = recentSeries.length > 4 ? recentSeries : fullSeries;

  let sparkSvg = "";
  if (sparkSource.length > 4) {
    let vals = sparkSource.map(p => p.v);
    // Memory stays as Available % — no inversion needed (matches Azure Portal)
    const mn = Math.min(...vals), mx = Math.max(...vals);
    const rng = mx - mn || 1;
    const w = 80, h = 24;
    const step = w / (vals.length - 1);
    const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - mn) / rng) * h).toFixed(1)}`).join(" ");
    sparkSvg = `<svg width="${w}" height="${h}" class="opacity-60 group-hover:opacity-100 transition"><polyline points="${pts}" fill="none" stroke="${domColor}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
  }

  // Two-stage severity: z-score + absolute threshold gate
  // For CPU/Disk: high value = pressure. For MEM (available %): low value = pressure.
  const _absFloor = { CPU: 50, MEM: 30, DISK: 50 };
  const domFloor = _absFloor[domLabel] || 50;
  // For memory, "meaningful" pressure means available is LOW (≤ 30%)
  const absIsMeaningful = domLabel === "MEM" ? domVal <= domFloor : domVal >= domFloor;
  // DB memory leniency: 8-20% available is expected (SGA/PGA uses 80-92%)
  const isDbMem = _isDbRole(_inferRole(vmName)) && domLabel === "MEM" && domVal >= 8;

  let sevLabel, sevColor;
  if (isDbMem && !hasSustained) {
    sevLabel = domVal < 8 ? "WARNING" : "HEALTHY";
    sevColor = domVal < 8 ? THEME.amber : THEME.green;
  } else if (hasSustained && absIsMeaningful) {
    sevLabel = "CRITICAL SUSTAINED"; sevColor = THEME.purple;
  } else if (highestSev >= 3 && absIsMeaningful) {
    sevLabel = "CRITICAL"; sevColor = THEME.red;
  } else if (highestSev >= 3 && !absIsMeaningful) {
    sevLabel = "ELEVATED"; sevColor = THEME.amber;  // downgraded — z high but abs low
  } else {
    sevLabel = "WARNING"; sevColor = THEME.amber;
  }

  // E3: Trend direction — current vs 2h ago
  const twoHoursMs = 2 * 60 * 60 * 1000;
  let trendArrow = "", trendDelta = "";
  if (sparkSource.length > 4) {
    let tVals = sparkSource.map(p => ({ t: new Date(p.t).getTime(), v: p.v }));
    const latest = tVals[tVals.length - 1];
    const twoHAgo = tVals.filter(p => (latest.t - p.t) >= twoHoursMs);
    const ref = twoHAgo.length ? twoHAgo[twoHAgo.length - 1] : tVals[0];
    const delta = latest.v - ref.v;
    if (delta > 2) { trendArrow = "↑"; trendDelta = `+${delta.toFixed(0)}%`; }
    else if (delta < -2) { trendArrow = "↓"; trendDelta = `${delta.toFixed(0)}%`; }
    else { trendArrow = "→"; trendDelta = "flat"; }
  }
  // For memory (Available %), rising = good (more free), falling = bad (pressure)
  // For CPU/Disk, rising = bad, falling = good
  const trendColor = domLabel === "MEM"
    ? (trendArrow === "↓" ? THEME.red : trendArrow === "↑" ? THEME.green : THEME.muted)
    : (trendArrow === "↑" ? THEME.red : trendArrow === "↓" ? THEME.green : THEME.muted);

  // I2: Role tag
  const role = _inferRole(vmName);
  const vmEnv = _inferEnv(vmName);
  const vmEnvColor = vmEnv === "PROD" ? THEME.red : vmEnv === "TEST" ? THEME.amber : vmEnv === "DEV" ? THEME.cyan : "";
  const vmEnvBadge = vmEnv ? `<span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${vmEnvColor};background:${hexA(vmEnvColor,0.12)}">${vmEnv}</span>` : "";

  // P1: Projected breach
  // CPU/Disk: breach when rising toward 80%. Memory: breach when available drops below 20%.
  let breachLabel = "";
  if (domLabel === "MEM") {
    // Memory: low available trending downward
    if (domVal <= 30 && domVal > 15 && trendArrow === "↓" && sparkSource.length > 4) {
      let tVals = sparkSource.map(p => ({ t: new Date(p.t).getTime(), v: p.v }));
      const latest = tVals[tVals.length - 1];
      const ref2h = tVals.filter(p => (latest.t - p.t) >= twoHoursMs);
      const ref = ref2h.length ? ref2h[ref2h.length - 1] : tVals[0];
      const ratePerMs = (latest.v - ref.v) / (latest.t - ref.t);
      if (ratePerMs < 0) {
        const msToBreak = (latest.v - 15) / Math.abs(ratePerMs);
        const hoursToBreak = msToBreak / (60 * 60 * 1000);
        if (hoursToBreak > 0 && hoursToBreak < 24) {
          breachLabel = `< 15% avail in ~${hoursToBreak.toFixed(0)}h`;
        }
      }
    }
  } else if (domVal >= 70 && domVal < 80 && trendArrow === "↑" && sparkSource.length > 4) {
    let tVals = sparkSource.map(p => ({ t: new Date(p.t).getTime(), v: p.v }));
    const latest = tVals[tVals.length - 1];
    const ref2h = tVals.filter(p => (latest.t - p.t) >= twoHoursMs);
    const ref = ref2h.length ? ref2h[ref2h.length - 1] : tVals[0];
    const ratePerMs = (latest.v - ref.v) / (latest.t - ref.t);
    if (ratePerMs > 0) {
      const msToBreak = (80 - latest.v) / ratePerMs;
      const hoursToBreak = msToBreak / (60 * 60 * 1000);
      if (hoursToBreak > 0 && hoursToBreak < 24) {
        breachLabel = `breach in ~${hoursToBreak.toFixed(0)}h`;
      }
    }
  }

  // Waveform shape for the dominant metric (compact badge on card)
  const waveforms = vmData.waveforms || {};
  const domWaveKey = domLabel === "MEM" ? "Available Memory Percentage"
    : domLabel === "DISK" ? "OS Disk Bandwidth Consumed Percentage"
    : "Percentage CPU";
  let domWave = waveforms[domWaveKey];
  // DB-aware: rewrite alarming memory waveform labels for DB servers in expected band
  // _rewriteWaveformForDb expects "used %" — convert from available
  if (domLabel === "MEM" && domWave) {
    const _thr = window.appData?.resource?.kpis?.thresholds || RESOURCE_THRESHOLDS;
    domWave = _rewriteWaveformForDb(domWave, role, 100 - domVal, _thr);
  }
  const waveRiskColors = { none: THEME.green, low: THEME.cyan, medium: THEME.amber, high: THEME.red, critical: THEME.purple };
  const waveBadge = domWave
    ? `<span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded cursor-help" style="color:${waveRiskColors[domWave.risk] || THEME.muted};background:${hexA(waveRiskColors[domWave.risk] || THEME.muted, 0.12)}" title="${domWave.meaning}">${domWave.icon} ${domWave.label}</span>`
    : "";

  card.innerHTML = `
    <div class="flex items-start justify-between gap-2 mb-1.5">
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5">
          <span class="text-xs font-bold text-Cwhite truncate">${escapeHtml(vmName)}</span>
          <span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${THEME.muted};background:${hexA(THEME.border,0.4)}">${role}</span>
          ${vmEnvBadge}
          ${waveBadge}
        </div>
        <div class="flex items-center gap-2 mt-1">
          <span class="px-1.5 py-0.5 rounded text-[8px] font-extrabold uppercase" style="color:${sevColor};background:${hexA(sevColor,0.15)}">${sevLabel}</span>
          <span class="text-[9px] text-Cmuted">${criticalCount} anomal${criticalCount > 1 ? "ies" : "y"}</span>
          ${breachLabel ? `<span class="text-[8px] font-bold px-1 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.12)}">⏱ ${breachLabel}</span>` : ""}
        </div>
      </div>
      <div class="text-right shrink-0">
        <div class="flex items-baseline gap-1 justify-end">
          <span class="text-lg font-extrabold leading-none" style="color:${domColor}">${domVal.toFixed(0)}%</span>
          <span class="text-xs font-bold" style="color:${trendColor}">${trendArrow}</span>
        </div>
        <div class="flex items-center gap-1 justify-end">
          <span class="text-[8px] font-bold uppercase tracking-wider" style="color:${domColor}">${domLabel} ${domStatType}</span>
          ${trendDelta ? `<span class="text-[8px] font-mono" style="color:${trendColor}">${trendDelta}</span>` : ""}
        </div>
        ${domHasOutlier ? `<div class="text-[7px] text-Cmuted mt-0.5" title="Single-point spike to ${domMaxVal.toFixed(0)}% — P95 shown instead">peak ${domMaxVal.toFixed(0)}% ⚠</div>` : ""}
      </div>
    </div>
    <div class="flex items-center justify-between">
      ${sparkSvg}
      <span class="text-[8px] text-Cmuted">${recentSeries.length > 4 ? "last 6h" : "full window"}</span>
    </div>
  `;

  // Click → expand drilldown and smooth-scroll to it
  card.addEventListener("click", () => {
    const detailArea = document.getElementById("deepdive-detail-area");
    if (!detailArea) return;
    // Destroy existing Chart.js instances before clearing DOM — prevents orphaned
    // canvas contexts that pile up and trigger Firefox's slow-script warning.
    _deepDiveCharts.forEach(c => { try { c.destroy(); } catch(e){} });
    _deepDiveCharts = [];
    detailArea.innerHTML = "";
    // Highlight selected card
    container.querySelectorAll("[data-vm-selected]").forEach(c => {
      c.removeAttribute("data-vm-selected");
      c.style.borderColor = hexA(THEME.red, 0.3);
      c.style.boxShadow = "none";
    });
    card.setAttribute("data-vm-selected", "1");
    card.style.borderColor = THEME.red;
    card.style.boxShadow = `0 0 0 2px ${hexA(THEME.red, 0.3)}`;
    _renderVmDeepDiveCard(vmName, vmData, _ddShowExtendedMetrics ? metricConfig : metricConfig.filter(m => m.core), detailArea, true);
    // Smooth scroll directly to the drilldown
    requestAnimationFrame(() => {
      detailArea.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  container.appendChild(card);
}

// ── Single VM expanded card with charts + spike table ─────────
function _renderVmDeepDiveCard(vmName, vmData, metricConfig, container, showCharts) {
  const metrics = vmData.series || {};
  const spikes = vmData.spikes || {};
  const stats = vmData.stats || {};
  const availableMetrics = metricConfig.filter(mc => metrics[mc.key] && metrics[mc.key].length > 0);

  if (!availableMetrics.length) return;

  const card = document.createElement("div");
  card.className = "rounded-xl border border-red-500/25 bg-Ccard p-4 space-y-3";

  // Check if any metric hits 100% saturation — deserves pulsing red border
  const hasSaturation = Object.values(stats).some(st => st.max >= 100);
  if (hasSaturation) {
    card.classList.add("saturated-pulse");
  }

  // VM header with stats
  const cpuStatsH = stats["Percentage CPU"];
  const memStats = stats["Available Memory Percentage"];
  const grainTag = vmData.grain ? ` <span class="text-[7px] px-0.5 rounded" style="color:${THEME.muted};background:${hexA(THEME.border,0.3)}" title="All stats computed from ${vmData.grain}-average samples. Azure Portal may use coarser grain (e.g. 6h for 30d view) which smooths peaks.">${vmData.grain} grain</span>` : "";
  let headerStats = "";
  if (cpuStatsH) {
    const maxTag = cpuStatsH.max_anomalous
      ? `<span class="text-[8px] px-1 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}" title="Max ${cpuStatsH.max}% may be a single-point spike — P95 is ${cpuStatsH.p95}%">single-point</span>`
      : "";
    const maxSrc = cpuStatsH.max_source === "azure_max_agg" ? "" : "";
    const maxSrcTitle = cpuStatsH.max_source === "azure_max_agg" ? " title=\"Max from Azure Maximum aggregation (true peak, not average)\"" : "";
    headerStats += `<span class="text-Cblue" title="${cpuStatsH.p95_note || ''}">CPU avg ${cpuStatsH.mean}% · <span${maxSrcTitle}>max ${cpuStatsH.max}%</span> ${maxTag}· P95 ${cpuStatsH.p95}%</span>${grainTag}`;
  }
  if (memStats) {
    const memMinTag = memStats.min_anomalous
      ? `<span class="text-[8px] px-1 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}" title="Min ${memStats.min}% may be a single-point dip — P5 is ${memStats.p5 ?? 'N/A'}%">single-point</span>`
      : "";
    headerStats += `${cpuStatsH ? " · " : ""}<span class="text-Ccyan">Mem avail ${memStats.mean}% · min ${memStats.min}% ${memMinTag}</span> <span class="text-[7px] px-0.5 rounded cursor-help" style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.08)}" title="Source: Available Memory Percentage (Azure Monitor Average). Higher = more free memory.">ℹ</span>`;
  }

  let criticalCount = 0;
  for (const ms of Object.values(spikes)) criticalCount += ms.length;

  const ddRole = _inferRole(vmName);
  const ddEnv = _inferEnv(vmName);
  const ddEnvColor = ddEnv === "PROD" ? THEME.red : ddEnv === "TEST" ? THEME.amber : ddEnv === "DEV" ? THEME.cyan : "";
  const ddEnvTag = ddEnv ? `<span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${ddEnvColor};background:${hexA(ddEnvColor,0.12)}">${ddEnv}</span>` : "";

  card.innerHTML = `
    <div class="flex items-center justify-between gap-2 flex-wrap">
      <div class="flex items-center gap-2">
        <span class="text-xs font-bold text-Cwhite">${escapeHtml(vmName)}</span>
        <span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${THEME.muted};background:${hexA(THEME.border,0.4)}">${ddRole}</span>
        ${ddEnvTag}
        <span class="px-2 py-0.5 rounded-full text-[9px] font-bold bg-red-500/20 text-red-400 border border-red-500/30">${criticalCount} critical</span>
      </div>
      <div class="text-[10px] text-Cmuted">${headerStats}</div>
    </div>
  `;

  // Always show disk IO telemetry summary (avg/max), even when no spikes.
  // Hidden by default — only visible when "Extended Metrics" toggle is on.
  if (_ddShowExtendedMetrics) {
  const ioMetrics = [
    ["OS Disk Latency", "OS Latency", "ms"],
    ["Data Disk Latency", "Data Latency", "ms"],
    ["OS Disk IOPS Consumed Percentage", "OS IOPS %", "%"],
    ["Data Disk IOPS Consumed Percentage", "Data IOPS %", "%"],
    ["VM Cached IOPS Consumed Percentage", "Cached IOPS %", "%"],
    ["VM Uncached IOPS Consumed Percentage", "Uncached IOPS %", "%"],
    ["OS Disk Read Operations/Sec", "OS Read IOPS", "ops/s"],
    ["OS Disk Write Operations/Sec", "OS Write IOPS", "ops/s"],
    ["Data Disk Read Operations/Sec", "Data Read IOPS", "ops/s"],
    ["Data Disk Write Operations/Sec", "Data Write IOPS", "ops/s"],
  ];
  const ioRows = ioMetrics.map(([k, lbl, unit]) => {
    const s = stats[k];
    if (!s) {
      return `<tr class="border-t border-Cborder/20"><td class="py-1 pr-3 text-[9px] text-Cmuted">${lbl}</td><td class="py-1 pr-3 text-[9px] text-Cmuted">N/A</td><td class="py-1 text-[9px] text-Cmuted">N/A</td></tr>`;
    }
    const avg = Number(s.mean ?? 0).toFixed(2);
    const mx = Number(s.max ?? 0).toFixed(2);
    return `<tr class="border-t border-Cborder/20"><td class="py-1 pr-3 text-[9px] text-Cwhite">${lbl}</td><td class="py-1 pr-3 text-[9px] text-Cmuted font-mono">${avg} ${unit}</td><td class="py-1 text-[9px] text-Cmuted font-mono">${mx} ${unit}</td></tr>`;
  }).join("");
  const ioSection = document.createElement("div");
  ioSection.className = "rounded-lg border border-Cborder/40 bg-Cbg/50 p-3";
  ioSection.innerHTML = `
    <div class="text-[10px] font-bold uppercase tracking-widest text-Cmuted mb-1">Disk IO Telemetry (Azure Monitor)</div>
    <div class="text-[8px] text-Cmuted mb-1">Source timezone: UTC (display may follow browser locale)</div>
    <table class="w-full text-left"><thead><tr class="text-[8px] text-Cmuted uppercase tracking-wider"><th class="pb-1 pr-3">Metric</th><th class="pb-1 pr-3">Average</th><th class="pb-1">Maximum</th></tr></thead><tbody>${ioRows}</tbody></table>
  `;
  card.appendChild(ioSection);
  } // end _ddShowExtendedMetrics guard

  // (waveform badges removed — Signal Pattern Analysis section below covers all patterns)

  // Critical spike detail table — P2: group recurring events
  // Filter spikes to only show metrics matching the current visibility toggle
  const _coreMetricKeys = new Set(metricConfig.map(m => m.key));
  const allCriticalSpikes = [];
  for (const [metricName, spikeList] of Object.entries(spikes)) {
    if (!_coreMetricKeys.has(metricName)) continue; // skip metrics not in active config
    for (const s of spikeList) allCriticalSpikes.push({ ...s, metric: metricName });
  }
  if (allCriticalSpikes.length) {
    allCriticalSpikes.sort((a, b) => b.z_score - a.z_score);

    // P2: Group by metric + similar time-of-day (within 2h window) across different days
    const groups = [];
    const used = new Set();
    for (let i = 0; i < allCriticalSpikes.length; i++) {
      if (used.has(i)) continue;
      const s = allCriticalSpikes[i];
      const group = [s];
      used.add(i);
      const sHour = new Date(s.peak_time).getHours();
      for (let j = i + 1; j < allCriticalSpikes.length; j++) {
        if (used.has(j)) continue;
        const t = allCriticalSpikes[j];
        if (t.metric !== s.metric) continue;
        const tHour = new Date(t.peak_time).getHours();
        const sDay = new Date(s.peak_time).toDateString();
        const tDay = new Date(t.peak_time).toDateString();
        if (sDay !== tDay && Math.abs(tHour - sHour) <= 2) {
          group.push(t);
          used.add(j);
        }
      }
      groups.push(group);
    }

    const spikeTable = document.createElement("div");
    spikeTable.className = "rounded-lg border border-red-500/20 bg-red-500/5 p-3 overflow-x-auto";

    let rows = groups.map(group => {
      if (group.length === 1) {
        const s = group[0];
        const metricLabel = metricConfig.find(m => m.key === s.metric)?.label || s.metric;
        const start = new Date(s.start).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
        // Show full date+time for end when duration > 24h (multi-day window)
        const end = s.duration_min > 1440
          ? new Date(s.end).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
          : new Date(s.end).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        const peakTime = new Date(s.peak_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

        // Bug 3+4: severity-aware labels (sustained, absolute threshold)
        const sev = (s.severity || "critical").toUpperCase().replace("_", " ");
        const sevColor = sev.includes("SUSTAINED") ? THEME.purple : sev === "WARNING" ? THEME.amber : THEME.red;
        const detectionTag = s.detection === "absolute_threshold"
          ? `<span class="ml-1 px-1 py-0.5 rounded text-[8px] font-bold" style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.15)}">ABS</span>`
          : "";

        // Source lineage tooltip
        const srcMetric = s.source_metric || s.metric;
        const srcAgg = s.aggregation || "Average";
        const srcGrain = s.grain || "";
        const srcFormula = s.formula ? ` | Derived: ${s.formula}` : "";
        const lineageTitle = `Source: ${srcMetric} (${srcAgg}, ${srcGrain})${srcFormula}`;
        const sevReason = s.severity_reason || "";

        return `<tr class="border-t border-red-500/15 cursor-pointer hover:bg-white/[0.04] group" title="Click to reload deep dive for this exact time window" onclick="openSpikeWindow(${JSON.stringify(s.start)},${JSON.stringify(s.end)})">          <td class="py-1.5 pr-3 text-[10px] font-semibold" style="color:${sevColor}" title="${sevReason}">${sev}${detectionTag}${_confBadge(s.confidence)}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cwhite" title="${lineageTitle}">${escapeHtml(metricLabel)}${s.is_derived ? ' <span class="text-[7px] px-0.5 rounded" style="color:'+THEME.cyan+';background:'+hexA(THEME.cyan,0.12)+'">derived</span>' : ''}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cwhite font-mono font-bold">${_formatPeak(s.metric, s.peak)}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">${start} → ${end}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">${s.duration_min}min</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">peak @ ${peakTime}</td>
          <td class="py-1.5 text-[10px] text-Cmuted">${_formatDeviation(s)}</td>
          <td class="py-1.5 pl-1 text-[9px] opacity-0 group-hover:opacity-100 transition" style="color:${THEME.blue}">→ drill</td>
        </tr>`;

      } else {
        // Recurring pattern — collapsed row
        const s0 = group[0];
        const metricLabel = metricConfig.find(m => m.key === s0.metric)?.label || s0.metric;
        const maxPeak = Math.max(...group.map(g => g.peak));
        const avgDur = (group.reduce((a, g) => a + g.duration_min, 0) / group.length).toFixed(0);
        const days = group.map(g => new Date(g.peak_time).toLocaleDateString([], { weekday: "short" }));
        const uniqueDays = [...new Set(days)];
        // Bug 2: "Daily" when ≥6 unique days, otherwise list them
        const dayLabel = uniqueDays.length >= 6 ? "Daily" : uniqueDays.join("/");

        // Bug 5: Show BOTH severity + recurring tag, not just "RECURRING"
        const worstSev = group.some(g => (g.severity || "").includes("sustained")) ? "CRITICAL SUSTAINED"
          : group.some(g => g.severity === "critical") ? "CRITICAL" : "WARNING";
        const sevColor = worstSev.includes("SUSTAINED") ? THEME.purple : worstSev === "WARNING" ? THEME.amber : THEME.red;

        // Evidence-backed recurring label
        const confLabel = group[0].confidence;
        const distinctDays = [...new Set(group.map(g => new Date(g.peak_time).toDateString()))].length;
        const patternLabel = distinctDays >= 5 ? "scheduled job pattern" : distinctDays >= 3 ? "likely recurring" : "possible pattern";
        const patternColor = distinctDays >= 5 ? THEME.amber : distinctDays >= 3 ? THEME.cyan : THEME.muted;

        const minStart = group.reduce((a, g) => g.start < a ? g.start : a, group[0].start);
        const maxEnd   = group.reduce((a, g) => g.end   > a ? g.end   : a, group[0].end);
        return `<tr class="border-t border-red-500/15 cursor-pointer hover:bg-white/[0.04] group" style="background:${hexA(THEME.amber, 0.04)}" title="Click to reload deep dive for this recurring pattern window" onclick="openSpikeWindow(${JSON.stringify(minStart)},${JSON.stringify(maxEnd)})">
          <td class="py-1.5 pr-3 text-[10px] font-semibold" style="color:${sevColor}">${worstSev} <span class="px-1 py-0.5 rounded text-[8px] font-bold" style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}">RECURRING</span>${_confBadge(confLabel)}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cwhite">${escapeHtml(metricLabel)} <span class="px-1 py-0.5 rounded text-[8px] font-bold" style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}">${group.length}×</span></td>
          <td class="py-1.5 pr-3 text-[10px] text-Cwhite font-mono font-bold">${_formatPeak(s0.metric, maxPeak)}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">${dayLabel} pattern</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">~${avgDur}min each</td>
          <td class="py-1.5 pr-3 text-[10px] font-semibold" style="color:${patternColor}">${patternLabel} <span class="text-[8px] text-Cmuted">(${distinctDays}d)</span></td>
          <td class="py-1.5 text-[10px] text-Cmuted">peak ${_formatPeak(s0.metric, maxPeak)}, avg duration ${avgDur}min</td>
          <td class="py-1.5 pl-1 text-[9px] opacity-0 group-hover:opacity-100 transition" style="color:${THEME.blue}">→ drill</td>
        </tr>`;
      }
    }).join("");

    // Sparse-data warning: if any metric has <24 datapoints, warn
    const sparseMetrics = availableMetrics.filter(mc => {
      const pts = metrics[mc.key];
      return pts && pts.length > 0 && pts.length < 24;
    });
    const sparseWarn = sparseMetrics.length
      ? `<div class="text-[9px] px-2 py-1 rounded mb-1.5" style="color:${THEME.amber};background:${hexA(THEME.amber,0.08)};border:1px solid ${hexA(THEME.amber,0.2)}">⚠ Low data density for ${sparseMetrics.map(m => m.label).join(", ")} — interpretations may be less reliable</div>`
      : "";

    // Provenance line
    const firstPts = Object.values(metrics).find(a => a && a.length > 1);
    let grainLabel = "";
    if (firstPts && firstPts.length >= 2) {
      const gap = (new Date(firstPts[1].t) - new Date(firstPts[0].t)) / 60000;
      grainLabel = gap < 60 ? `${gap.toFixed(0)}min` : `${(gap/60).toFixed(0)}h`;
    }
    const provLine = `<div class="text-[8px] text-Cmuted mb-1">Source: Azure Monitor · Aggregation: Average · Grain: ${grainLabel || "auto"} · Datapoints: ${firstPts?.length || "?"}</div>`;

    spikeTable.innerHTML = `
      <div class="text-[10px] font-bold text-red-400 uppercase tracking-widest mb-1">⚡ Critical Spike Events</div>
      ${provLine}${sparseWarn}
      <table class="w-full text-left"><thead>
        <tr class="text-[9px] text-Cmuted uppercase tracking-wider">
          <th class="pb-1 pr-3">Severity</th><th class="pb-1 pr-3">Metric</th>
          <th class="pb-1 pr-3">Peak</th><th class="pb-1 pr-3">Window</th>
          <th class="pb-1 pr-3">Duration</th><th class="pb-1 pr-3">Pattern</th>
          <th class="pb-1">Detail</th>
        </tr>
      </thead><tbody>${rows}</tbody></table>
    `;
    card.appendChild(spikeTable);
  }

  // ── Signal Pattern Analysis (waveform shapes) ──────────────
  // Only show actionable patterns (critical/high) in detail.
  // Healthy metrics collapsed into a single summary line.
  const wfData = vmData.waveforms || {};
  // Filter to core metrics only when extended toggle is off
  const _coreKeys = new Set(metricConfig.map(m => m.key));
  const wfEntries = Object.entries(wfData).filter(([k]) => _coreKeys.has(k));
  if (wfEntries.length) {
    const wfSection = document.createElement("div");
    wfSection.className = "rounded-lg border border-Cborder/40 bg-Cbg/50 p-3 space-y-1";
    const riskOrder = { critical: 0, high: 1, medium: 2, low: 3, none: 4 };
    const riskColorsD = { none: THEME.green, low: THEME.cyan, medium: THEME.amber, high: THEME.red, critical: THEME.purple };
    wfEntries.sort((a, b) => (riskOrder[a[1].risk] ?? 5) - (riskOrder[b[1].risk] ?? 5));
    const metricShort = k =>
      k.includes("CPU") ? "CPU" :
      k.includes("Memory") ? "Memory" :
      k.includes("OS Disk") && k.includes("IOPS") ? "OS IOPS" :
      k.includes("Data Disk") && k.includes("IOPS") ? "Data IOPS" :
      k.includes("OS Disk") && k.includes("Bandwidth") ? "OS Disk BW" :
      k.includes("Data Disk") && k.includes("Bandwidth") ? "Data Disk BW" :
      k.includes("Cached") ? "Cached BW" :
      k.includes("Uncached") ? "Uncached BW" :
      k.includes("OS Disk") ? "OS Disk" :
      k.includes("Data Disk") ? "Data Disk" : k;

    // Separate actionable vs healthy
    const actionable = []; // critical, high, medium
    const healthy = [];    // low, none
    for (const [metric, wf] of wfEntries) {
      let dWf = wf;
      if (metric.includes("Memory")) {
        const memStat = vmData.stats?.["Available Memory Percentage"];
        const memUsed = memStat?.min != null ? 100 - memStat.min : (memStat?.mean != null ? 100 - memStat.mean : 0);
        const _thr = window.appData?.resource?.kpis?.thresholds || RESOURCE_THRESHOLDS;
        dWf = _rewriteWaveformForDb(wf, ddRole, memUsed, _thr);
      }
      const entry = { metric, wf, dWf };
      if (dWf.risk === "critical" || dWf.risk === "high" || dWf.risk === "medium") actionable.push(entry);
      else healthy.push(entry);
    }

    let wfRows = "";
    // ── Actionable patterns: full detail with new intelligence fields ──
    for (const { metric, wf, dWf } of actionable) {
      const rc = riskColorsD[dWf.risk] || THEME.muted;
      const det = wf.details || {};

      // Peak tag
      const peakTag = det.peak_used_pct != null
        ? `<span class="text-[8px] font-mono px-1 py-0.5 rounded" style="color:${det.peak_used_pct >= 85 ? THEME.red : det.peak_used_pct >= 65 ? THEME.amber : THEME.green};background:${hexA(det.peak_used_pct >= 85 ? THEME.red : det.peak_used_pct >= 65 ? THEME.amber : THEME.green, 0.1)}">peak ${det.peak_used_pct.toFixed(0)}%</span>`
        : "";

      // Headroom tag
      const hdTag = det.headroom_pct != null
        ? `<span class="text-[8px] font-mono px-1 py-0.5 rounded" style="color:${det.headroom_pct <= 15 ? THEME.red : det.headroom_pct <= 30 ? THEME.amber : THEME.green};background:${hexA(det.headroom_pct <= 15 ? THEME.red : det.headroom_pct <= 30 ? THEME.amber : THEME.green, 0.1)}">${det.headroom_pct.toFixed(0)}% free</span>`
        : "";

      // DB expected band rewrite badge
      const dbBandTag = (metric.includes("Memory") && dWf !== wf)
        ? `<span class="text-[7px] font-bold px-1 py-0.5 rounded" style="color:${DB_EXPECTED_COLOR};background:${hexA(DB_EXPECTED_COLOR,0.1)}">DB EXPECTED</span>`
        : "";

      // Confidence label with color coding
      const confLabel = dWf.confidence_label || (dWf.confidence >= 0.75 ? "observed" : dWf.confidence >= 0.55 ? "inferred" : "weak-signal");
      const confColor = confLabel === "observed" ? THEME.green : confLabel === "inferred" ? THEME.amber : THEME.muted;
      const confPct = dWf.confidence != null ? `${(dWf.confidence * 100).toFixed(0)}%` : "";
      const confTag = `<span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded cursor-help"
        style="color:${confColor};background:${hexA(confColor,0.1)}"
        title="Confidence: ${confPct} — ${confLabel}. Based on ${det.peak_count || 0} peaks, ${wf.recurrence_days || 0} breach days, ${(det.cv || 0).toFixed(2)} CV."
        >${confLabel} ${confPct}</span>`;

      // Recurrence days tag
      const recurDays = wf.recurrence_days || 0;
      const recurTag = recurDays > 0
        ? `<span class="text-[7px] font-mono px-1 py-0.5 rounded" style="color:${THEME.muted};background:${hexA(THEME.muted,0.08)}" title="${recurDays} distinct days with threshold breach">↺ ${recurDays}d</span>`
        : "";

      // Duration above threshold tag
      const durHrs = wf.duration_above_threshold_hrs || 0;
      const durTag = durHrs >= 1
        ? `<span class="text-[7px] font-mono px-1 py-0.5 rounded" style="color:${durHrs >= 8 ? THEME.red : THEME.amber};background:${hexA(durHrs >= 8 ? THEME.red : THEME.amber, 0.1)}" title="${durHrs.toFixed(1)}h above threshold">⏱ ${durHrs.toFixed(0)}h above</span>`
        : "";

      // Change point badge
      const cpTag = (dWf.shape === "change_point" || det.change_point_idx != null)
        ? `<span class="text-[7px] font-bold px-1 py-0.5 rounded" style="color:#f472b6;background:${hexA('#f472b6',0.1)}" title="Regime shift detected: level changed from ${det.before_mean || '?'}% to ${det.after_mean || '?'}%">REGIME SHIFT</span>`
        : "";

      // Concurrent pressure badge
      const concTag = dWf.concurrent_pressure
        ? `<span class="text-[7px] font-bold px-1 py-0.5 rounded animate-pulse" style="color:${THEME.red};background:${hexA(THEME.red,0.15)}" title="Concurrent pressure: ${(dWf.concurrent_metrics||[]).join(' + ')} all under load simultaneously">🔥 CONCURRENT</span>`
        : "";

      // Secondary pattern
      const secShape = dWf.secondary_shape;
      const secCatalog = secShape ? { sawtooth: {label:"Cyclic",icon:"⚡"}, diurnal: {label:"Diurnal",icon:"🌓"}, trending_up:{label:"Trending ↑",icon:"📈"}, random_spikes:{label:"Spikes",icon:"🎯"}, plateau:{label:"Sustained",icon:"▬"}, change_point:{label:"Shift",icon:"⚠️"}, weekend_dip:{label:"Wknd Dip",icon:"📅"}, flat_low:{label:"Flat Low",icon:"✅"} }[secShape] : null;
      const secTag = secCatalog
        ? `<span class="text-[7px] font-mono px-1 py-0.5 rounded" style="color:${THEME.muted};background:${hexA(THEME.muted,0.08)}" title="Secondary pattern: ${secCatalog.label}">also: ${secCatalog.icon} ${secCatalog.label}</span>`
        : "";

      wfRows += `
        <div class="flex items-start gap-3 py-2 border-b border-Cborder/20 last:border-0 hover:bg-white/[0.015] rounded transition">
          <span class="text-lg shrink-0 mt-0.5">${dWf.icon}</span>
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5 flex-wrap mb-0.5">
              <span class="text-[11px] font-bold text-Cwhite">${metricShort(metric)}</span>
              <span class="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded" style="color:${rc};background:${hexA(rc,0.14)};border:1px solid ${hexA(rc,0.3)}">${dWf.label}</span>
              <span class="text-[8px] font-bold uppercase px-1 py-0.5 rounded" style="color:${rc};background:${hexA(rc,0.08)}">${dWf.risk}</span>
              ${confTag}${peakTag}${hdTag}${durTag}${recurTag}${cpTag}${concTag}${secTag}${dbBandTag}
            </div>
            <div class="text-[9px] text-Cwhite/80 leading-relaxed">${dWf.meaning || ""}</div>
            <div class="text-[9px] mt-0.5 font-medium" style="color:${THEME.cyan}">→ ${dWf.action || ""}</div>
          </div>
        </div>`;
    }

    // ── Healthy patterns: collapsed single line ──
    let healthySummary = "";
    if (healthy.length) {
      const hNames = healthy.map(h => metricShort(h.metric)).join(", ");
      healthySummary = `
        <div class="flex items-center gap-2 pt-1.5 mt-1 border-t border-Cborder/20">
          <span class="text-sm">✓</span>
          <span class="text-[9px] text-Cmuted">${healthy.length} metric${healthy.length > 1 ? "s" : ""} within normal range</span>
          <span class="text-[8px] text-Cmuted/60">${hNames}</span>
        </div>`;
    }

    const headerLabel = actionable.length
      ? `⚠ ${actionable.length} Pattern${actionable.length > 1 ? "s" : ""} Requiring Attention`
      : `✓ All Patterns Normal`;
    const headerColor = actionable.length ? "text-amber-400" : "text-green-400";
    wfSection.innerHTML = `
      <div class="text-[10px] font-bold ${headerColor} uppercase tracking-widest mb-1.5">${headerLabel}</div>
      ${wfRows}${healthySummary}
    `;
    card.appendChild(wfSection);
  }

  // ── Unified multi-metric chart (dual Y-axes) ──────────────
  // CPU + Disk on left axis, Memory (inverted to "used") on right axis
  // Core metrics always shown; extended (IOPS/BW) only when toggle is on
  const _allUnifiedMetrics = [
    { key: "Percentage CPU",                          label: "CPU %",                  color: THEME.blue,   axis: "y", dash: [], core: true },
    { key: "Available Memory Percentage",              label: "Available Mem %",        color: THEME.cyan,   axis: "y1", dash: [], core: true },
    { key: "OS Disk Bandwidth Consumed Percentage",    label: "OS Disk %",              color: THEME.amber,  axis: "y", dash: [4, 2], core: true },
    { key: "Data Disk Bandwidth Consumed Percentage",  label: "Data Disk %",            color: THEME.purple, axis: "y", dash: [2, 2], core: true },
    { key: "OS Disk IOPS Consumed Percentage",         label: "OS IOPS %",              color: "#f97316",  axis: "y", dash: [6, 3] },
    { key: "Data Disk IOPS Consumed Percentage",       label: "Data IOPS %",            color: "#a855f7",  axis: "y", dash: [3, 3] },
    { key: "VM Cached IOPS Consumed Percentage",       label: "Cached IOPS %",          color: "#0ea5e9",  axis: "y", dash: [1, 2] },
    { key: "VM Uncached IOPS Consumed Percentage",     label: "Uncached IOPS %",        color: "#14b8a6",  axis: "y", dash: [8, 2] },
  ];
  const unifiedMetrics = _ddShowExtendedMetrics ? _allUnifiedMetrics : _allUnifiedMetrics.filter(m => m.core);

  const datasetsForChart = [];
  let unifiedLabels = null;
  const allAnnotations = {};
  const _pendingAnnotations = [];

  // Downsample large time-series to prevent Firefox canvas overload.
  // Uses largest-triangle-three-bucket (LTTB-lite): keeps visual shape.
  const MAX_PTS = 500;
  function _downsample(arr, n) {
    if (arr.length <= n) return arr;
    const step = (arr.length - 2) / (n - 2);
    const out = [arr[0]];
    for (let i = 1; i < n - 1; i++) {
      const lo = Math.floor(1 + (i - 1) * step);
      const hi = Math.min(Math.floor(1 + i * step), arr.length - 1);
      let best = lo, bestA = -1;
      const prev = out[out.length - 1];
      const nextAvg = arr.slice(hi, Math.min(hi + Math.ceil(step), arr.length))
        .reduce((s, v) => s + (typeof v === 'number' ? v : 0), 0) / Math.ceil(step);
      for (let j = lo; j <= hi; j++) {
        const a = Math.abs((j - (out.length - 1)) * (nextAvg - (typeof prev === 'number' ? prev : 0))
          - ((out.length - 1) - (out.length - 1)) * ((typeof arr[j] === 'number' ? arr[j] : 0) - (typeof prev === 'number' ? prev : 0)));
        if (a > bestA) { bestA = a; best = j; }
      }
      out.push(arr[best]);
    }
    out.push(arr[arr.length - 1]);
    return out;
  }

  for (const um of unifiedMetrics) {
    const pts = metrics[um.key];
    if (!pts || !pts.length) continue;
    // Downsample both labels and values together
    const rawLabels = pts.map(p => new Date(p.t));
    let vals = pts.map(p => p.v);
    // Memory now shown as Azure-native Available % — no inversion needed
    if (!unifiedLabels) {
      unifiedLabels = pts.length > MAX_PTS ? _downsample(rawLabels, MAX_PTS) : rawLabels;
    }
    const dsVals = pts.length > MAX_PTS ? _downsample(vals, MAX_PTS) : vals;

    datasetsForChart.push({
      label: um.label,
      data: dsVals,
      borderColor: um.color,
      backgroundColor: "transparent",
      borderWidth: 1.5,
      borderDash: um.dash,
      pointRadius: 0,
      pointHitRadius: 6,
      fill: false,
      tension: 0.3,
      yAxisID: um.axis,
    });

    // Add spike annotations for this metric — collision-aware layout
    const metricSpikes = spikes[um.key] || [];
    for (const spike of metricSpikes) {
      _pendingAnnotations.push({
        metric: um.key,
        label: um.label,
        color: um.color,
        peak: spike.peak,
        start: new Date(spike.start),
        end: new Date(spike.end),
        duration_min: spike.duration_min,
        severity: spike.severity || "critical",
        detection: spike.detection || "z_score",
      });
    }
  }

  // ── Build collision-aware annotations from collected spikes ──
  // Group annotations that overlap within a 24h window
  const _annots = _pendingAnnotations.sort((a, b) => a.start - b.start);
  const WINDOW_MS = 24 * 60 * 60 * 1000; // 24h clustering window

  const clusters = [];
  const used = new Set();
  for (let i = 0; i < _annots.length; i++) {
    if (used.has(i)) continue;
    const cluster = [_annots[i]];
    used.add(i);
    for (let j = i + 1; j < _annots.length; j++) {
      if (used.has(j)) continue;
      // Check if annotation j overlaps with any in the cluster within WINDOW_MS
      const clusterEnd = Math.max(...cluster.map(c => c.end.getTime()));
      const clusterStart = Math.min(...cluster.map(c => c.start.getTime()));
      if (_annots[j].start.getTime() <= clusterEnd + WINDOW_MS &&
          _annots[j].end.getTime() >= clusterStart - WINDOW_MS) {
        cluster.push(_annots[j]);
        used.add(j);
      }
    }
    clusters.push(cluster);
  }

  for (const cluster of clusters) {
    if (cluster.length <= 2) {
      // Render individually with vertical stagger + colored left border
      cluster.forEach((ann, slotIdx) => {
        const durLabel = ann.duration_min > 30 ? `${ann.duration_min}min ⚠` : `${ann.duration_min}min`;
        const sevTag = ann.severity.includes("sustained") ? " SUSTAINED" : "";
        const detTag = ann.detection === "absolute_threshold" ? " [ABS]" : "";
        const bgColor = ann.severity.includes("sustained") ? hexA(THEME.purple, 0.85)
          : ann.color === THEME.red ? hexA(THEME.red, 0.8)
          : hexA(ann.color, 0.8);

        const labelContent = `${ann.label} ${ann.peak}% / ${durLabel}${sevTag}${detTag}`;
        const yOffset = slotIdx * 22; // stagger by 22px per slot

        allAnnotations[`spike_${ann.metric}_${ann.start.getTime()}`] = {
          type: "box",
          xMin: ann.start,
          xMax: ann.end,
          backgroundColor: hexA(ann.color, 0.08),
          borderColor: hexA(ann.color, 0.4),
          borderWidth: 1,
          label: {
            display: true,
            content: labelContent,
            position: { x: "start", y: "start" },
            yAdjust: yOffset,
            font: { size: 9, weight: "bold" },
            color: THEME.white,
            backgroundColor: bgColor,
            padding: { top: 2, bottom: 2, left: 6, right: 4 },
            borderRadius: 3,
          },
        };
      });
    } else {
      // >2 annotations in cluster → collapse to count badge + individual boxes without labels
      // Render the highlight boxes for each event (no labels)
      cluster.forEach((ann) => {
        allAnnotations[`spike_box_${ann.metric}_${ann.start.getTime()}`] = {
          type: "box",
          xMin: ann.start,
          xMax: ann.end,
          backgroundColor: hexA(ann.color, 0.06),
          borderColor: hexA(ann.color, 0.3),
          borderWidth: 1,
          label: { display: false },
        };
      });

      // Single summary badge at cluster midpoint
      const clusterStart = new Date(Math.min(...cluster.map(c => c.start.getTime())));
      const clusterEnd = new Date(Math.max(...cluster.map(c => c.end.getTime())));
      const clusterMid = new Date((clusterStart.getTime() + clusterEnd.getTime()) / 2);

      // Build hover tooltip content
      const tooltipLines = cluster.map(a => {
        const sev = a.severity.includes("sustained") ? "SUST" : a.severity === "warning" ? "WARN" : "CRIT";
        return `${a.label} ${a.peak}% ${a.duration_min}min ${sev}`;
      });

      // Unique metric count
      const metricSet = new Set(cluster.map(c => c.label));
      const worstPeak = Math.max(...cluster.map(c => c.peak));

      allAnnotations[`spike_cluster_${clusterStart.getTime()}`] = {
        type: "label",
        xValue: clusterMid,
        yValue: worstPeak,
        yAdjust: -12,
        content: `⚠ ${cluster.length} events · ${metricSet.size} metrics · peak ${worstPeak}%`,
        font: { size: 9, weight: "bold" },
        color: THEME.white,
        backgroundColor: hexA(THEME.red, 0.85),
        padding: { top: 3, bottom: 3, left: 6, right: 6 },
        borderRadius: 4,
        // Store detail lines for tooltip plugin
        _tooltipLines: tooltipLines,
      };
    }
  }

  // Reset for next VM card
  const _pendingAnnotations2 = [];

  if (datasetsForChart.length && unifiedLabels) {
    const chartWrap = document.createElement("div");
    chartWrap.className = "rounded-lg border border-Cborder/50 bg-Cbg/50 p-3";

    // Legend chips
    const legendHtml = datasetsForChart.map(ds =>
      `<span class="inline-flex items-center gap-1 text-[9px]"><span class="w-3 h-0.5 inline-block rounded" style="background:${ds.borderColor}"></span>${ds.label}</span>`
    ).join(" ");

    chartWrap.innerHTML = `
      <div class="flex items-center justify-between mb-2">
        <span class="text-[10px] font-semibold text-Cwhite">Unified Time-Series — All Metrics</span>
        <div class="flex items-center gap-3">
          ${legendHtml}
          <button class="dd-expand-btn px-1.5 py-0.5 rounded border border-Cborder/50 text-[9px] text-Cmuted hover:text-Cblue hover:border-Cblue/40 transition" title="Expand / Collapse chart">⤢ Expand</button>
        </div>
      </div>
      <div class="deepdive-chart-container">
        <canvas></canvas>
      </div>
    `;
    card.appendChild(chartWrap);

    // Wire expand toggle
    const expandBtn = chartWrap.querySelector(".dd-expand-btn");
    const chartContainer = chartWrap.querySelector(".deepdive-chart-container");
    expandBtn.addEventListener("click", () => {
      const isExpanded = chartContainer.classList.toggle("expanded");
      expandBtn.textContent = isExpanded ? "⤡ Collapse" : "⤢ Expand";
      // Trigger Chart.js resize after CSS transition
      setTimeout(() => chart.resize(), 350);
    });

    const canvas = chartWrap.querySelector("canvas");
    const ctx = canvas.getContext("2d");

    const chart = new Chart(ctx, {
      type: "line",
      data: { labels: unifiedLabels, datasets: datasetsForChart },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: THEME.card2,
            borderColor: THEME.border,
            borderWidth: 1,
            titleFont: { size: 10 },
            bodyFont: { size: 10 },
            callbacks: {
              title: items => items.length ? new Date(items[0].parsed.x).toLocaleString() : "",
              label: item => `${item.dataset.label}: ${item.parsed.y.toFixed(1)}%`,
            },
          },
          annotation: {
            annotations: {
              // Threshold bands: warning (75-90%) and critical (90-100%)
              warnBand: {
                type: "box",
                yMin: 75, yMax: 90, yScaleID: "y",
                backgroundColor: hexA(THEME.amber, 0.04),
                borderWidth: 0,
                drawTime: "beforeDatasetsDraw",
              },
              critBand: {
                type: "box",
                yMin: 90, yMax: 100, yScaleID: "y",
                backgroundColor: hexA(THEME.red, 0.06),
                borderWidth: 0,
                drawTime: "beforeDatasetsDraw",
              },
              warnLine: {
                type: "line",
                yMin: 75, yMax: 75, yScaleID: "y",
                borderColor: hexA(THEME.amber, 0.35),
                borderWidth: 1, borderDash: [4, 3],
                drawTime: "beforeDatasetsDraw",
                label: { display: true, content: "75%", position: "end",
                         font: { size: 8 }, color: hexA(THEME.amber, 0.6),
                         backgroundColor: "transparent" },
              },
              critLine: {
                type: "line",
                yMin: 90, yMax: 90, yScaleID: "y",
                borderColor: hexA(THEME.red, 0.4),
                borderWidth: 1, borderDash: [4, 3],
                drawTime: "beforeDatasetsDraw",
                label: { display: true, content: "90%", position: "end",
                         font: { size: 8 }, color: hexA(THEME.red, 0.6),
                         backgroundColor: "transparent" },
              },
              ...allAnnotations,
            },
          },
          zoom: _zoomConfig({ mode: "x" }),
        },
        scales: {
          x: {
            type: "time",
            time: {
              tooltipFormat: "MMM d, HH:mm",
              displayFormats: { minute: "HH:mm", hour: "MMM d HH:mm", day: "MMM d HH:mm" },
            },
            ticks: { color: THEME.muted, font: { size: 9 }, maxTicksLimit: 16, maxRotation: 45, minRotation: 0 },
            grid: { color: hexA(THEME.border, 0.25) },
          },
          y: {
            position: "left",
            beginAtZero: true,
            title: { display: true, text: "CPU / Disk %", color: THEME.muted, font: { size: 9 } },
            ticks: { color: THEME.muted, font: { size: 9 }, callback: v => v + "%" },
            grid: { color: hexA(THEME.border, 0.2) },
          },
          y1: {
            position: "right",
            beginAtZero: true,
            suggestedMax: 100,
            title: { display: true, text: "Available Memory %", color: THEME.cyan, font: { size: 9 } },
            ticks: { color: hexA(THEME.cyan, 0.6), font: { size: 9 }, callback: v => v + "%" },
            grid: { drawOnChartArea: false },
          },
        },
      },
      plugins: [{
        // Inline plugin: draw expanded tooltip when hovering cluster badge
        id: "clusterTooltip",
        _hoveredCluster: null,
        afterEvent(chart, args) {
          const evt = args.event;
          if (evt.type !== "mousemove") return;
          const anns = chart.options.plugins.annotation?.annotations || {};
          let found = null;
          for (const [key, ann] of Object.entries(anns)) {
            if (!key.startsWith("spike_cluster_") || !ann._tooltipLines) continue;
            // Hit-test: check if mouse is near the label position
            const xScale = chart.scales.x;
            const yScale = chart.scales.y;
            const xPx = xScale.getPixelForValue(ann.xValue?.getTime?.() ?? ann.xValue);
            const yPx = yScale.getPixelForValue(ann.yValue) + (ann.yAdjust || 0);
            const dx = Math.abs(evt.x - xPx);
            const dy = Math.abs(evt.y - yPx);
            if (dx < 100 && dy < 20) { found = { key, ann, x: xPx, y: yPx }; break; }
          }
          if (found !== this._hoveredCluster) {
            this._hoveredCluster = found;
            chart.draw();
          }
        },
        afterDraw(chart) {
          const hov = this._hoveredCluster;
          if (!hov || !hov.ann._tooltipLines) return;
          const ctx = chart.ctx;
          const lines = hov.ann._tooltipLines;
          const lineH = 14;
          const pad = 6;
          const w = Math.max(...lines.map(l => ctx.measureText(l).width)) + pad * 2 + 8;
          const h = lines.length * lineH + pad * 2;
          let tx = hov.x + 8;
          let ty = hov.y + 20;
          // Keep within chart area
          if (tx + w > chart.chartArea.right) tx = hov.x - w - 8;
          if (ty + h > chart.chartArea.bottom) ty = hov.y - h - 8;

          ctx.save();
          ctx.fillStyle = THEME.card2;
          ctx.strokeStyle = THEME.border;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.roundRect(tx, ty, w, h, 4);
          ctx.fill();
          ctx.stroke();
          ctx.font = "bold 9px system-ui, sans-serif";
          ctx.textBaseline = "top";
          lines.forEach((line, i) => {
            // Color-code by metric prefix
            if (line.startsWith("CPU")) ctx.fillStyle = THEME.blue;
            else if (line.startsWith("Mem")) ctx.fillStyle = THEME.red;
            else if (line.startsWith("OS")) ctx.fillStyle = THEME.amber;
            else if (line.startsWith("Data")) ctx.fillStyle = THEME.purple;
            else ctx.fillStyle = THEME.white;
            ctx.fillText(line, tx + pad + 4, ty + pad + i * lineH);
          });
          ctx.restore();
        },
      },
      crosshairPlugin,
      ],
    });
    _deepDiveCharts.push(chart);

    // Enterprise: export toolbar on expanded chart
    _addChartToolbar(chartWrap, chart, () => {
      let csv = "Timestamp," + datasetsForChart.map(d => d.label).join(",") + "\n";
      unifiedLabels.forEach((t, ti) => {
        csv += t + "," + datasetsForChart.map(d => d.data[ti]?.y?.toFixed(2) ?? "").join(",") + "\n";
      });
      return csv;
    });
  }

  // Tag card for click-to-drill from heatmap
  card.setAttribute("data-vm-detail", vmName);
  container.appendChild(card);
}


// ════════════════════════════════════════════════════════════
//  PHASE 6 · GOVERNANCE & APPROVAL
//  - Issues / Waivers register (pure client-side state)
//  - PE Validation Checklist (9 toggles, progress bar)
//  - Dual sign-off (PE + Customer)
//  - Export HTML Report → POST /api/export-report → blob download
// ════════════════════════════════════════════════════════════

const SEV_COLORS = {
  Critical:      THEME.red,
  High:          "#fb923c",
  Medium:        THEME.amber,
  Low:           THEME.green,
  Informational: THEME.blue,
};
const STAT_COLORS = {
  Open:         THEME.red,
  "In Progress":THEME.amber,
  Waived:       THEME.amber,
  Resolved:     THEME.green,
  Deferred:     THEME.muted,
};

// ── Bootstrap ─────────────────────────────────────────────────
function initGovernanceTab() {
  // Issue form accordion toggle
  const formToggle  = document.getElementById("issue-form-toggle");
  const formBody    = document.getElementById("issue-form-body");
  const chevron     = document.getElementById("issue-form-chevron");
  formToggle?.addEventListener("click", () => {
    const open = !formBody.classList.contains("hidden");
    formBody.classList.toggle("hidden", open);
    chevron?.classList.toggle("rotate-180", !open);
  });

  // Add Issue button
  document.getElementById("add-issue-btn")?.addEventListener("click", addIssue);

  // Checklist checkboxes
  document.querySelectorAll(".chk-item").forEach((cb) => {
    cb.addEventListener("change", onChecklistChange);
  });

  // PE sign-off checkbox
  document.getElementById("pe-approve-chk")?.addEventListener("change", onPeApprove);

  // Customer sign-off checkbox
  document.getElementById("cust-approve-chk")?.addEventListener("change", onCustApprove);

  // Live name sync
  document.getElementById("pe-name")?.addEventListener("input", (e) => {
    window.appData.approvals.pe.name = e.target.value.trim();
    refreshGoLiveBanner();
  });
  document.getElementById("cust-name")?.addEventListener("input", (e) => {
    window.appData.approvals.customer.name = e.target.value.trim();
    refreshGoLiveBanner();
  });

  // Notes sync
  document.getElementById("approval-notes")?.addEventListener("input", (e) => {
    window.appData.approvals.notes = e.target.value;
  });

  // Export CSV button
  document.getElementById("export-issues-csv-btn")?.addEventListener("click", exportIssuesCsv);

  // Export HTML Report button
  document.getElementById("export-report-btn")?.addEventListener("click", exportHtmlReport);
}

// ── Add Issue ─────────────────────────────────────────────────
function addIssue() {
  const desc = document.getElementById("iss-desc")?.value.trim();
  const errEl = document.getElementById("issue-form-error");
  if (!desc) {
    errEl?.classList.remove("hidden");
    return;
  }
  errEl?.classList.add("hidden");

  const issues = window.appData.issues;
  const autoId = `ISS-${String(issues.length + 1).padStart(3, "0")}`;
  issues.push({
    ID:          document.getElementById("iss-id")?.value.trim() || autoId,
    Type:        document.getElementById("iss-type")?.value || "Bug",
    Severity:    document.getElementById("iss-sev")?.value  || "Medium",
    Status:      document.getElementById("iss-stat")?.value || "Open",
    Owner:       document.getElementById("iss-owner")?.value.trim() || "",
    ETA:         document.getElementById("iss-eta")?.value.trim()   || "N/A",
    Description: desc,
    Mitigation:  document.getElementById("iss-mit")?.value.trim()   || "",
    Logged:      new Date().toISOString().slice(0, 10),
  });

  // Clear form fields
  ["iss-id","iss-owner","iss-eta","iss-desc","iss-mit"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });

  renderIssuesRegister();
  toast("success", "Issue added", `${issues[issues.length - 1].ID} added to register.`, 3000);
}

// ── Render Issues Register ────────────────────────────────────
function renderIssuesRegister() {
  const issues  = window.appData.issues;
  const wrap    = document.getElementById("issues-register-wrap");
  const emptyEl = document.getElementById("issues-empty-note");
  const cards   = document.getElementById("issues-cards");
  if (!wrap || !cards) return;

  if (!issues.length) {
    wrap.classList.add("hidden");
    emptyEl?.classList.remove("hidden");
    return;
  }
  wrap.classList.remove("hidden");
  emptyEl?.classList.add("hidden");

  // KPI counts
  const nOpen    = issues.filter((i) => ["Open","In Progress"].includes(i.Status)).length;
  const nWaived  = issues.filter((i) => i.Status === "Waived").length;
  const nResolved= issues.filter((i) => i.Status === "Resolved").length;
  setText("iss-kpi-total",   String(issues.length));
  setText("iss-kpi-open",    String(nOpen));
  setText("iss-kpi-waived",  String(nWaived));
  setText("iss-kpi-resolved",String(nResolved));

  // Update open KPI color dynamically
  const openEl = document.getElementById("iss-kpi-open");
  if (openEl) openEl.style.color = nOpen > 0 ? THEME.red : THEME.green;

  // Cards
  cards.innerHTML = "";
  issues.forEach((iss, idx) => {
    const sevColor  = SEV_COLORS[iss.Severity]  || THEME.muted;
    const statColor = STAT_COLORS[iss.Status]   || THEME.muted;
    const card = document.createElement("div");
    card.className = "rounded-xl border-l-4 bg-Ccard2 border border-Cborder px-4 py-3";
    card.style.borderLeftColor = sevColor;
    card.innerHTML = `
      <div class="flex items-start justify-between flex-wrap gap-2 mb-2">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-sm font-bold" style="color:${sevColor}">${escapeHtml(iss.ID)}</span>
          <span class="text-[10px] text-Cmuted">${escapeHtml(iss.Type)} · ${escapeHtml(iss.Severity)}</span>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-[10px] font-bold px-2 py-0.5 rounded-full border"
                style="color:${statColor};border-color:${hexA(statColor,.4)};background:${hexA(statColor,.1)}">
            ${escapeHtml(iss.Status)}
          </span>
          <span class="text-[10px] text-Cmuted">Owner: ${escapeHtml(iss.Owner||"—")} · ETA: ${escapeHtml(iss.ETA||"N/A")}</span>
          <button data-remove-idx="${idx}" class="text-[10px] text-Cmuted hover:text-Cred transition ml-1">✕ Remove</button>
        </div>
      </div>
      <p class="text-xs text-Cwhite leading-relaxed mb-1">${escapeHtml(iss.Description)}</p>
      ${iss.Mitigation ? `<p class="text-[11px] text-Cmuted">🛡 ${escapeHtml(iss.Mitigation)}</p>` : ""}
      <p class="text-[10px] text-Cmuted mt-1">Logged: ${escapeHtml(iss.Logged)}</p>
    `;
    card.querySelector("[data-remove-idx]")?.addEventListener("click", (e) => {
      const i = parseInt(e.target.dataset.removeIdx, 10);
      window.appData.issues.splice(i, 1);
      renderIssuesRegister();
    });
    cards.appendChild(card);
  });
}

// ── Checklist ─────────────────────────────────────────────────
function onChecklistChange(e) {
  const key = e.target.dataset.chk;
  window.appData.approvals.checklist[key] = e.target.checked;
  refreshChecklistProgress();
}

function refreshChecklistProgress() {
  const chk   = window.appData.approvals.checklist;
  const done  = Object.values(chk).filter(Boolean).length;
  const total = Object.keys(chk).length;
  const pct   = Math.round((done / total) * 100);
  const bar   = document.getElementById("chk-progress-bar");
  const label = document.getElementById("chk-progress-label");
  if (bar) {
    bar.style.width = `${pct}%`;
    bar.style.background = pct === 100 ? THEME.green : pct >= 67 ? THEME.amber : THEME.red;
  }
  if (label) label.textContent = `Checklist ${done}/${total} complete`;

  const allDone = done === total;
  const peLabel = document.getElementById("pe-approve-label");
  const peChk   = document.getElementById("pe-approve-chk");
  const hint    = document.getElementById("pe-checklist-hint");
  if (peLabel) peLabel.classList.toggle("opacity-50",       !allDone);
  if (peLabel) peLabel.classList.toggle("pointer-events-none", !allDone);
  if (peChk)  peChk.disabled = !allDone;
  if (hint)   hint.classList.toggle("hidden", allDone);
}

// ── PE sign-off ───────────────────────────────────────────────
function onPeApprove(e) {
  const approved = e.target.checked;
  window.appData.approvals.pe.approved = approved;
  if (approved && !window.appData.approvals.pe.date) {
    window.appData.approvals.pe.date = new Date().toISOString().slice(0, 10);
  }
  const badge = document.getElementById("pe-status-badge");
  if (badge) {
    badge.textContent = approved
      ? `✅ PE Approved — ${window.appData.approvals.pe.date}`
      : "⏳ PE Approval Pending";
    badge.style.color = approved ? THEME.green : THEME.amber;
  }
  refreshGoLiveBanner();
}

// ── Customer sign-off ─────────────────────────────────────────
function onCustApprove(e) {
  const approved = e.target.checked;
  window.appData.approvals.customer.approved = approved;
  if (approved && !window.appData.approvals.customer.date) {
    window.appData.approvals.customer.date = new Date().toISOString().slice(0, 10);
  }
  const badge = document.getElementById("cust-status-badge");
  if (badge) {
    badge.textContent = approved
      ? `✅ Customer Approved — ${window.appData.approvals.customer.date}`
      : "⏳ Customer Approval Pending";
    badge.style.color = approved ? THEME.green : THEME.amber;
  }
  refreshGoLiveBanner();
}

// ── Go-Live banner ────────────────────────────────────────────
function refreshGoLiveBanner() {
  const a       = window.appData.approvals;
  const bothOk  = a.pe.approved && a.customer.approved;
  const banner  = document.getElementById("golive-banner");
  const label   = document.getElementById("golive-label");
  const meta    = document.getElementById("golive-meta");
  const color   = bothOk ? THEME.green : THEME.amber;
  if (banner) {
    banner.style.borderColor  = hexA(color, 0.5);
    banner.style.background   = hexA(color, 0.1);
  }
  if (label) {
    label.textContent = `Go-Live Sign-Off Status: ${bothOk ? "APPROVED ✅" : "PENDING ⏳"}`;
    label.style.color = color;
  }
  if (meta) {
    const peName   = a.pe.name       || "—";
    const custName = a.customer.name || "—";
    meta.textContent = `PE: ${peName}  |  Customer: ${custName}`;
  }
}

// ── Export Issues CSV ─────────────────────────────────────────
function exportIssuesCsv() {
  const issues = window.appData.issues;
  if (!issues.length) {
    toast("info", "Nothing to export", "Add issues to the register first.");
    return;
  }
  const cols = ["ID","Type","Severity","Status","Owner","ETA","Description","Mitigation","Logged"];
  const csvRows = [cols.join(",")];
  for (const iss of issues) {
    csvRows.push(cols.map((c) => `"${String(iss[c] || "").replace(/"/g,'""')}"`).join(","));
  }
  const blob = new Blob([csvRows.join("\n")], { type: "text/csv" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = `issues_register_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Export HTML Report ────────────────────────────────────────
async function exportHtmlReport() {
  const btn = document.getElementById("export-report-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Generating…"; }

  // Sync text inputs into appData right before sending
  const a = window.appData.approvals;
  a.pe.name       = document.getElementById("pe-name")?.value.trim()   || a.pe.name;
  a.customer.name = document.getElementById("cust-name")?.value.trim() || a.customer.name;
  a.notes         = document.getElementById("approval-notes")?.value   || a.notes;

  // Build payload — entire appData snapshot
  const payload = {
    upload:    window.appData.upload,
    servers:   window.appData.servers,
    batch:     window.appData.batch,
    resource:  window.appData.resource,
    issues:    window.appData.issues,
    approvals: window.appData.approvals,
  };

  try {
    const res = await fetch("/api/export-report", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.text();
      toast("error", "Export failed", err.slice(0, 200));
      return;
    }
    const blob     = await res.blob();
    const url      = URL.createObjectURL(blob);
    const anchor   = document.createElement("a");
    const dateStr  = new Date().toISOString().slice(0, 10);
    anchor.href    = url;
    anchor.download= `PE_Audit_Report_${dateStr}.html`;
    anchor.click();
    URL.revokeObjectURL(url);
    toast("success", "Report downloaded", "Standalone HTML report saved to your Downloads folder.", 5000);
  } catch (err) {
    _handleFetchError(err);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg> Export HTML Report`; }
  }
}


// ════════════════════════════════════════════════════════════
//  PHASE 5 — PE Findings Engine + Gemini AI Insight
// ════════════════════════════════════════════════════════════

// ── Constants ────────────────────────────────────────────────
const FINDING_STYLES = {
  critical: { border: "border-Cred",   bg: "bg-Cred/5",   badge: "bg-Cred/20 text-Cred",   dot: "bg-Cred"   },
  warning:  { border: "border-Camber", bg: "bg-Camber/5", badge: "bg-Camber/20 text-Camber",dot: "bg-Camber"  },
  info:     { border: "border-Cblue",  bg: "bg-Cblue/5",  badge: "bg-Cblue/20 text-Cblue",  dot: "bg-Cblue"  },
  ok:       { border: "border-Cgreen", bg: "bg-Cgreen/5", badge: "bg-Cgreen/20 text-Cgreen", dot: "bg-Cgreen" },
};

// ── Init ──────────────────────────────────────────────────────
function initFindingsTab() {
  // Wire the "Refresh narrative" button in the 4-pillar accordion header
  const refreshBtn = document.getElementById("btn-refresh-narrative");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      refreshBtn.disabled = true;
      const orig = refreshBtn.textContent;
      refreshBtn.textContent = "Refreshing…";
      try {
        // Step 1: re-sync all appData from backend session cache
        await refreshAuditContext();
        // Step 2: force re-run findings with fresh payload (all 4 pillars)
        await triggerGenerateFindings({ force: true });
        // Step 3: realign executive dashboard grade
        if (typeof renderOverview === "function") {
          try { await renderOverview(); } catch (_) {}
        }
        toast("success", "PE Review refreshed",
          "All 4 pillars reloaded — Batch, Infrastructure, SOW, UI Benchmark");
      } catch (err) {
        toast("error", "Refresh failed", String(err?.message || err));
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.textContent = orig;
      }
    });
  }
}

// ── Trigger AI-driven findings (Gemini / NIM cross-pillar synthesis) ─────
// ── Build SLA triage digest for findings payload ──────────────────────────
// ── SLA Baseline vs Matrix comparison payload ─────────────────
// Cross-checks:
//   (A) SLA matrix expected-completion time (sla_hours) vs pe_config defaults
//   (B) SLA matrix expected-completion time vs actual Ctrl-M batch runtime
// Produces structured delta objects for the findings engine to consume.
function _buildSlaComparison() {
  const batchData    = window.appData?.batch;
  const slaIntel     = window.appData?.slaIntelligence;
  const peDefaults   = batchData?.pe_defaults || {};
  const topJobs      = batchData?.top_jobs || [];
  const wfSummary    = (window.appData?.slaMatrix?.workflow_summary) || [];
  const batchSlaWfs  = (window.appData?.batchSlaInfo?.workflows) || [];

  if (!batchData && !slaIntel) return null;

  const defaultDaily  = Number(peDefaults.daily_hrs  || 6.0);
  const defaultWeekly = Number(peDefaults.weekly_hrs || 8.0);

  // ── A: SLA matrix expected-completion vs pe_config defaults ──────────────
  // Find workflows where the contracted SLA is significantly different from
  // the PE default for that schedule type.
  const VARIANCE_THRESHOLD_PCT = 25; // flag when >25% difference
  const defaultDeltas = [];
  const wfSource = wfSummary.length > 0 ? wfSummary : batchSlaWfs;
  for (const wf of wfSource) {
    const slaHrs  = Number(wf.sla_hours || wf.sla_h || 0);
    if (slaHrs <= 0) continue;
    const btype   = (wf.batch_type || "DAILY").toUpperCase();
    const defHrs  = btype === "WEEKLY" ? defaultWeekly : defaultDaily;
    const diffPct = ((slaHrs - defHrs) / defHrs) * 100;
    if (Math.abs(diffPct) >= VARIANCE_THRESHOLD_PCT) {
      defaultDeltas.push({
        workflow:   wf.workflow || wf.sub_application || "?",
        batch_type: btype,
        matrix_sla_hrs: slaHrs,
        default_hrs:    defHrs,
        diff_pct:       Math.round(diffPct * 10) / 10,
        tighter:        slaHrs < defHrs,
      });
    }
  }

  // ── B: SLA matrix expected-completion vs actual batch runtime ────────────
  // Match top_jobs from batch against SLA matrix workflows and compute gap.
  const BREACH_VARIANCE_PCT = 15; // flag when runtime > 85% of contracted SLA
  const runtimeDeltas = [];
  const wfByName = {};
  for (const wf of wfSource) {
    const name = (wf.workflow || wf.sub_application || "").toUpperCase();
    if (name) wfByName[name] = wf;
  }
  for (const job of topJobs) {
    const jobName = (job.Job_Name || job.job_name || "").toUpperCase();
    const subApp  = (job.Sub_Application || job.sub_application || "").toUpperCase();
    // Try to match by sub_application first, then job name
    const wf = wfByName[subApp] || wfByName[jobName];
    const slaHrs   = Number(job.sla_hrs || wf?.sla_hours || wf?.sla_h || 0);
    const peakHrs  = Number(job.peak_hrs || 0);
    if (slaHrs <= 0 || peakHrs <= 0) continue;
    const bufPct   = ((slaHrs - peakHrs) / slaHrs) * 100;
    const usedPct  = 100 - bufPct;
    if (usedPct >= (100 - BREACH_VARIANCE_PCT)) {
      runtimeDeltas.push({
        job_name:       job.Job_Name || job.job_name || "?",
        sub_app:        job.Sub_Application || job.sub_application || "",
        matrix_sla_hrs: slaHrs,
        peak_runtime_hrs: peakHrs,
        buffer_pct:     Math.round(bufPct * 10) / 10,
        used_pct:       Math.round(usedPct * 10) / 10,
        sla_source:     job.sla_source || "default",
        is_breach:      bufPct < 0,
      });
    }
  }
  runtimeDeltas.sort((a, b) => a.buffer_pct - b.buffer_pct);

  // ── Summary ──────────────────────────────────────────────────────────────
  const hasSlaMatrix = !!(_isCustomerSlaType(window.appData?.batch?.sla_source?.type)
    || slaIntel?.intelligence?.valid_rows > 0);

  return {
    has_sla_matrix:     hasSlaMatrix,
    default_daily_hrs:  defaultDaily,
    default_weekly_hrs: defaultWeekly,
    default_deltas:     defaultDeltas.slice(0, 20),
    runtime_deltas:     runtimeDeltas.slice(0, 20),
    tighter_than_default: defaultDeltas.filter(d => d.tighter).length,
    looser_than_default:  defaultDeltas.filter(d => !d.tighter).length,
    near_breach_count:  runtimeDeltas.filter(d => !d.is_breach && d.used_pct >= 85).length,
    breach_count:       runtimeDeltas.filter(d => d.is_breach).length,
  };
}

// Returns structured arrays of: low-buffer jobs, breaching jobs with no
// resource evidence (unexplained), and workflow-level triage from BatchSLA.
function _buildSlaTriage() {
  const slaMatrix  = window.appData?.slaMatrix  || {};
  const batchSla   = window.appData?.batchSlaInfo || {};
  const jobSummary = slaMatrix.job_summary || [];
  const breachRows = slaMatrix.breaches    || [];
  const resLinked  = slaMatrix.resource_linked || [];

  // Jobs with buffer < 20% (at risk under any production load spike)
  const LOW_BUF_THRESH = 20;
  const lowBufJobs = jobSummary
    .filter(j => {
      const buf = parseFloat(j.buffer_pct ?? 999);
      return buf < LOW_BUF_THRESH;
    })
    .sort((a, b) => parseFloat(a.buffer_pct ?? 0) - parseFloat(b.buffer_pct ?? 0))
    .slice(0, 10)
    .map(j => ({
      job_name:    j.job_name || j.Job_Name || "?",
      buffer_pct:  parseFloat(j.buffer_pct ?? 0),
      peak_hrs:    parseFloat(j.peak_hrs    ?? 0),
      sla_hrs:     parseFloat(j.sla_limit   || j.sla_limit_hrs || 0),
      breach_rate: parseFloat(j.breach_rate ?? 0),
    }));

  // Breaching jobs where no resource link exists → unexplained breach
  const resLinkedJobNames = new Set(
    resLinked.map(r => (r.job_name || r.Job_Name || "").toLowerCase())
  );
  const breachJobsSeen = new Set();
  const unexplainedBreaches = breachRows
    .filter(r => r.status === "BREACH")
    .filter(r => {
      const jn = (r.job_name || "").toLowerCase();
      if (breachJobsSeen.has(jn)) return false;
      breachJobsSeen.add(jn);
      return !resLinkedJobNames.has(jn);
    })
    .slice(0, 10)
    .map(r => ({
      job_name:      r.job_name || "?",
      sub_app:       r.sub_application || "—",
      run_date:      r.run_date || "",
      run_hrs:       parseFloat(r.run_hrs  ?? 0),
      sla_limit_hrs: parseFloat(r.sla_limit_hrs ?? 0),
      margin_hrs:    parseFloat(r.breach_margin_hrs ?? 0),
      sla_source:    r.sla_source || "global",
    }));

  // Workflow-level triage — use canonical workflow_summary from SLA Matrix (Ctrl-M run)
  // or fall back to XLSX batchSlaInfo workflows. This is the correct level:
  // workflow_summary has buffer_pct and status already computed by the backend.
  const canonicalWfs = slaMatrix.workflow_summary || [];
  const xlsxWfs      = batchSla.workflows || [];

  // Prefer canonical (Ctrl-M worst-case), fall back to XLSX snapshot
  const wfSource = canonicalWfs.length > 0 ? canonicalWfs : xlsxWfs;
  const isCanonical = canonicalWfs.length > 0;

  const wfBreaches = wfSource.filter(w => {
    const status = isCanonical ? w.status : w.compliance;
    return status === "BREACH";
  });
  const wfLowBuf = wfSource.filter(w => {
    const status = isCanonical ? w.status : w.compliance;
    if (status === "BREACH") return false; // already in breaches
    const buf = isCanonical
      ? parseFloat(w.buffer_pct ?? 999)
      : (() => {
          const rt  = parseFloat(w.last_run_hours_xlsx || 0);
          const sla = parseFloat(w.sla_hours || 0);
          return sla > 0 ? (sla - rt) / sla * 100 : 999;
        })();
    return buf < LOW_BUF_THRESH;
  });

  return {
    low_buffer_jobs:      lowBufJobs,
    unexplained_breaches: unexplainedBreaches,
    wf_breaching:  wfBreaches.map(w => ({
      workflow:    w.workflow_name || w.workflow || w.sub_application || "?",
      batch_type:  w.batch_type   || "?",
      sla_hours:   isCanonical ? w.sla_h : w.sla_hours,
      runtime_h:   isCanonical ? w.runtime_h : w.last_run_hours_xlsx,
      buffer_pct:  isCanonical ? w.buffer_pct : null,
      sla_source:  isCanonical ? w.sla_source : "batch_sla_xlsx",
      data_src:    isCanonical ? "ctrl_m_canonical" : "xlsx_snapshot",
    })),
    wf_low_buffer: wfLowBuf.map(w => ({
      workflow:    w.workflow_name || w.workflow || w.sub_application || "?",
      batch_type:  w.batch_type   || "?",
      sla_hours:   isCanonical ? w.sla_h : w.sla_hours,
      runtime_h:   isCanonical ? w.runtime_h : w.last_run_hours_xlsx,
      buffer_pct:  isCanonical
        ? w.buffer_pct
        : (() => { const rt = parseFloat(w.last_run_hours_xlsx||0); const s = parseFloat(w.sla_hours||0); return s>0 ? Math.round((s-rt)/s*100*100)/100 : null; })(),
      status:      isCanonical ? w.status : w.compliance,
      sla_source:  isCanonical ? w.sla_source : "batch_sla_xlsx",
      data_src:    isCanonical ? "ctrl_m_canonical" : "xlsx_snapshot",
    })),
    total_jobs_analysed:  jobSummary.length,
    total_wfs_analysed:   wfSource.length,
    source_active: {
      batch_sla_xlsx: xlsxWfs.length > 0,
      ctrl_m_canonical: canonicalWfs.length > 0,
      sow_ceilings:   Object.keys(window.appData?.sowContract?.sla_windows || {}).length > 0,
    },
  };
}

// ── Build the rule-engine payload from whatever is currently in appData ─
// Compact waveform summary for findings/narrative payloads
function _summarizeWaveforms(waveforms) {
  if (!waveforms || !Object.keys(waveforms).length) return null;
  const out = {};
  for (const [metric, wf] of Object.entries(waveforms)) {
    out[metric] = { shape: wf.shape, label: wf.label, risk: wf.risk, confidence: wf.confidence };
  }
  return out;
}

// ── Deep Dive Summary builder — distills time-series evidence for findings/narrative ──
function _buildDeepDiveSummary() {
  if (!_deepDiveData) return null;
  const vms = _deepDiveData.vms || {};
  const summary = _deepDiveData.summary || {};
  const patterns = _deepDivePatterns || [];

  const perVm = [];
  for (const [vmName, vmData] of Object.entries(vms)) {
    const sp = vmData.spikes || {};
    const st = vmData.stats || {};
    let spikeCount = 0;
    const spikeDetails = [];

    for (const [metric, arr] of Object.entries(sp)) {
      spikeCount += arr.length;
      for (const s of arr) {
        spikeDetails.push({
          metric,
          peak: s.peak,
          z_score: s.z_score,
          duration_min: s.duration_min,
          severity: s.severity || "critical",
          detection: s.detection || "z_score",
          peak_time: s.peak_time,
          start: s.start,
          end: s.end,
        });
      }
    }

    if (spikeCount === 0) continue;

    // Compute trend
    const memSeries = (vmData.series || {})["Available Memory Percentage"] || [];
    let trend = "flat";
    if (memSeries.length > 4) {
      const twoH = 2 * 60 * 60 * 1000;
      const latest = memSeries[memSeries.length - 1];
      const lt = new Date(latest.t).getTime();
      const refs = memSeries.filter(p => (lt - new Date(p.t).getTime()) >= twoH);
      if (refs.length) {
        const refV = refs[refs.length - 1].v;
        const delta = refV - latest.v; // available decreasing = memory rising
        if (delta > 2) trend = "rising";
        else if (delta < -2) trend = "recovering";
      }
    }

    const memAvailMin = st["Available Memory Percentage"]?.min ?? null;
    const memUsed = memAvailMin != null ? 100 - memAvailMin : null;
    const cpuMax = st["Percentage CPU"]?.max ?? null;
    const diskIopsMax = Math.max(
      st["OS Disk IOPS Consumed Percentage"]?.max ?? 0,
      st["Data Disk IOPS Consumed Percentage"]?.max ?? 0,
      st["VM Cached IOPS Consumed Percentage"]?.max ?? 0,
      st["VM Uncached IOPS Consumed Percentage"]?.max ?? 0,
    ) || null;
    const diskLatencyMax = Math.max(
      st["OS Disk Latency"]?.max ?? 0,
      st["Data Disk Latency"]?.max ?? 0,
    ) || null;

    perVm.push({
      vm: vmName,
      role: _inferRole(vmName),
      spike_count: spikeCount,
      mem_used_max: memUsed,
      cpu_max: cpuMax,
      disk_iops_max: diskIopsMax,
      disk_latency_max_ms: diskLatencyMax,
      trend,
      spikes: spikeDetails.slice(0, 5), // top 5 per VM
      waveforms: _summarizeWaveforms(vmData.waveforms || {}),
    });
  }

  // Sort by spike count descending
  perVm.sort((a, b) => b.spike_count - a.spike_count);

  // ── Baseline analysis from 15-day+ time-series ──
  const baseline = _deepDiveData.baseline || {};
  const baselineSummary = {};
  if (baseline.per_vm) {
    for (const [vmName, metrics] of Object.entries(baseline.per_vm)) {
      const vmBase = {};
      for (const [metric, data] of Object.entries(metrics)) {
        vmBase[metric] = {
          hot_hours: data.hot_hours || [],
          weekday_avg: data.weekday_avg,
          weekend_avg: data.weekend_avg,
          divergence: data.weekday_weekend_divergence,
          trend_direction: data.trend_direction,
          trend_delta: data.trend_delta,
          trend_pct: data.trend_pct,
          chronic_pressure_days: (data.chronic_pressure_days || []).length,
          total_days: (data.daily_stats || []).length,
          recurring_spikes: data.recurring_spikes || [],
          overall_mean: data.overall_mean,
          overall_p95: data.overall_p95,
          overall_max: data.overall_max,
        };
      }
      baselineSummary[vmName] = vmBase;
    }
  }

  return {
    hours_back: _deepDiveHoursBack || 24,
    total_critical: summary.total_critical || 0,
    affected_vms: summary.affected_vms || 0,
    vm_count: summary.vm_count || 0,
    patterns: patterns.slice(0, 5),
    per_vm: perVm.slice(0, 10), // top 10 worst VMs
    baseline: {
      days_observed: baseline.days_observed || 0,
      sufficient_baseline: baseline.sufficient_baseline || false,
      per_vm: baselineSummary,
      fleet: baseline.fleet || {},
    },
  };
}

function _buildFindingsPayload() {
  const ad = window.appData || {};
  const _filtered = ad.batch ? _filterBatchUtility(ad.batch) : null;
  const bk = ad.batch?.kpis || null;

  // data_coverage lives at batch ROOT (not inside kpis) — enrich batch_kpis explicitly
  // Gap 2 fix: backend reads (req.batch_kpis or {}).get("data_coverage")
  const dataCoverage = ad.batch?.data_coverage || ad.batch?.kpis?.data_coverage || null;
  // sla_source is the AUTHORITATIVE provenance signal (set by batch_calculator only when
  // the batch was re-solved against a customer SLA file). It also lives at the batch ROOT,
  // not inside kpis — so we must nest it explicitly or findings can't tell a customer SLA
  // upload from system defaults (causing "SLA source: Assumed" / "matrix not uploaded" to
  // contradict the SLA panel that correctly shows the customer ceilings).
  const slaSource = ad.batch?.sla_source || ad.batch?.kpis?.sla_source || null;
  const batchKpisEnriched = bk
    ? { ...bk, data_coverage: dataCoverage, ...(slaSource ? { sla_source: slaSource } : {}) }
    : null;

  // Pillar 3 — SOW compare: prefer live comparison, fall back to manual entry
  const sowCompare = ad.sowCompare || _buildSowCompareFromManual() || null;
  const sowDfu     = sowCompare?.dfu_actual  || sowCompare?.dfu     || 0;
  const sowDfuBase = sowCompare?.dfu_target  || sowCompare?.dfu_sow || 0;

  return {
    // Pillar 1 — Batch + SLA
    batch_kpis:     batchKpisEnriched,
    top_jobs:       _filtered?.top_jobs     || null,
    top_breaches:   _filtered?.top_breaches || null,
    window:         ad.batch?.window        || null,
    anomalies:      ad.batch?.anomalies     || null,
    sub_stats:      ad.batch?.sub_stats     || null,
    sla_triage:     _buildSlaTriage(),
    sla_comparison: _buildSlaComparison(),

    // Pillar 2 — Infrastructure
    resource_kpis: ad.resource?.kpis    || null,
    servers:       ad.resource?.servers || ad.servers || null,

    // Pillar 3 — SOW / DFU volume
    sow_compare:  sowCompare,
    sow_dfu:      sowDfu,
    sow_dfu_base: sowDfuBase,

    // Pillar 4 — UI Benchmark
    benchmark: ad.benchmark || null,

    // Supporting
    sla_matrix:      ad.slaMatrix       || null,
    // Filter sla_ceilings to only valid numeric values — Pydantic rejects nulls/strings
    // and would return HTTP 422 if any ceiling value is null (e.g. when SLA matrix has
    // batch types with missing SLA hours).
    sla_ceilings: (() => {
      const raw = ad.slaCeilings;
      if (!raw || typeof raw !== 'object') return null;
      const clean = Object.fromEntries(
        Object.entries(raw).filter(([, v]) => typeof v === 'number' && isFinite(v) && v > 0)
      );
      return Object.keys(clean).length ? clean : null;
    })(),
    issues:          ad.issues          || null,
    customer_name:   ad.customerName    || null,
    sla_intel:       ad.slaIntelligence || null,
    volume_analysis: _buildVolumeAnalysis(),
    deep_dive:       _buildDeepDiveSummary(),
  };
}

// ── Debounced findings trigger — prevents cascade storm ──────
// Multiple callers (batch, resource, deep-dive, exec) all fire
// triggerGenerateFindings in rapid succession. Without debounce,
// this causes 4-6 concurrent API calls + DOM re-renders that
// freeze Firefox. 400ms debounce collapses the burst into one call.
let _findingsDebounceTimer = null;
let _findingsInFlight = false;
let _findingsErrCount = 0;      // consecutive error count — stops cascade toasts
let _findingsLastErrMsg = "";   // dedup repeated identical error messages

async function triggerGenerateFindings({ force = false } = {}) {
  // If a request is already in flight, just schedule a re-run after it finishes
  if (_findingsInFlight) {
    if (!_findingsDebounceTimer) {
      _findingsDebounceTimer = setTimeout(() => {
        _findingsDebounceTimer = null;
        triggerGenerateFindings().catch(() => {});
      }, 400);
    }
    return;
  }

  const loading = document.getElementById("findings-loading");
  if (loading) loading.classList.remove("hidden");

  // Call the deterministic rule engine — fast, always works, no LLM needed.
  // The LLM runs in the background via triggerSmartFindings afterwards.
  const payload = _buildFindingsPayload();

  // Skip if there's nothing to analyse — force=true bypasses this for manual refresh
  const ad = window.appData || {};
  const hasData = !!(ad.batch || ad.resource) ||
                  !!(payload.batch_kpis || payload.resource_kpis ||
                     payload.benchmark || payload.sow_compare ||
                     (payload.top_jobs && payload.top_jobs.length));
  if (!hasData && !force) {
    if (loading) loading.classList.add("hidden");
    return;
  }

  _findingsInFlight = true;

  try {
    const res = await fetch("/api/generate-findings", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });
    if (!res.ok) {
      const msg = await res.text();
      _findingsErrCount++;
      // Show at most ONE toast per unique error to prevent cascade storm
      if (_findingsErrCount <= 1 || msg !== _findingsLastErrMsg) {
        _findingsLastErrMsg = msg;
        const detail = res.status === 422 ? "Payload validation error — check SLA ceilings format" : msg.slice(0, 200);
        toast("error", "Findings error", detail);
      }
      return;
    }
    _findingsErrCount = 0;
    _findingsLastErrMsg = "";
    const data = await res.json();

    // /api/generate-findings already returns the Finding shape directly —
    // no field remapping needed (text, recommendation, evidence_class, etc.)
    const findings = Array.isArray(data.findings) ? data.findings : [];

    window.appData.findings = { findings, summary: data.summary };
    window._lastFailureGrid = data.failure_grid
      || (window.appData.batch && window.appData.batch.failure_grid) || null;
    // Invalidate downstream caches so exec dashboard re-reads fresh data
    window._execCache = null;
    window._findingsLastHash = null;
    renderFindings(findings);
    renderFindingsSummary(data.summary || {});
    renderFindingsDonut(data.summary || {});
    renderPeReviewSections(data, window.appData.smartFindings || null);

    // Background: LLM smart analysis (non-blocking — never delays the table)
    triggerSmartFindings(payload).catch(() => {});

    // Cross-pillar cascade
    triggerPeConsultant().catch(() => {});
    triggerPeNarrative().catch(() => {});
  } catch (err) {
    _handleFetchError(err);
  } finally {
    _findingsInFlight = false;
    const loading = document.getElementById("findings-loading");
    if (loading) loading.classList.add("hidden");
  }
}



// ════════════════════════════════════════════════════════════════
// SMART FINDINGS — Gemma-powered verdict block + Next Actions table
// Calls /api/smart-findings asynchronously (never blocks the UI thread).
// ════════════════════════════════════════════════════════════════
async function triggerSmartFindings(payload) {
  // Reuse the same payload as /api/generate-findings — the endpoint runs
  // the rule engine first, then dedupes, structures, and asks Gemma for
  // a 15-word verdict (with a hard 12s timeout).
  let data;
  try {
    const res = await fetch("/api/smart-findings", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await res.text());
    data = await res.json();
  } catch (err) {
    console.warn("smart-findings failed:", err);
    return;
  }
  window.appData.smartFindings = data;
  renderSmartVerdict(data.verdict || null);
  renderSmartNextActions(data.next_actions || []);
  renderSmartOpenGaps(data.open_gaps || []);
  // Sync the 4-pillar review sections with smart verdict data
  renderPeReviewSections(
    { findings: (window.appData.findings?.findings) || [],
      summary:   window.appData.findings?.summary   || {},
      data_coverage:  data.data_coverage  || {},
      audit_coverage: data.audit_coverage || {} },
    data
  );
}

// ════════════════════════════════════════════════════════════════
// POST-LOAD DATA REVIEW (OpenAI gpt-oss-120b reasoning model)
// Cross-checks every dashboard number for internal contradictions and
// suggests corrections. Runs in parallel with the rule engine — UI never
// waits for it.
// ════════════════════════════════════════════════════════════════
async function triggerReviewData() {
  // Panel removed — bail early so we don't waste an API call.
  if (!document.getElementById("review-data-panel")) return;

  const ad = window.appData || {};
  const body = {
    batch:         ad.batch       || null,
    resource:      ad.resource    || (ad.upload ? { kpis: ad.upload?.kpis, servers: ad.servers } : null),
    sla_matrix:    ad.slaMatrix   || ad.sla_matrix || null,
    findings:      ad.findings    || null,
    customer_name: ad.customerName || null,
  };

  let result;
  try {
    const res = await fetch("/api/review-data", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text());
    result = await res.json();
  } catch (err) {
    console.warn("review-data failed:", err);
    return;
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23-.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5"/></svg> Review with gpt-oss`;
    }
  }
  window.appData.dataReview = result;
  renderDataReview(result);
}

function renderDataReview(r) {
  const panel = document.getElementById("review-data-panel");
  if (!panel || !r) return;
  panel.classList.remove("hidden");

  const verdict = r.verdict || "CLEAN";
  const verdictEl = document.getElementById("review-data-verdict");
  if (verdictEl) {
    const isClean = verdict === "CLEAN";
    const c = isClean ? THEME.green : THEME.red;
    verdictEl.textContent = verdict;
    verdictEl.style.color = c;
    verdictEl.style.borderColor = hexA(c, 0.5);
    verdictEl.style.background = hexA(c, 0.12);
  }

  setText("review-data-confidence", `confidence ${r.confidence ?? 0}%`);
  setText("review-data-engine",     r.engine || "");
  setText("review-data-summary",    r.summary || "—");

  const cWrap = document.getElementById("review-data-contradictions");
  const cList = document.getElementById("review-data-contradictions-list");
  const contradictions = r.internal_contradictions || [];
  if (cWrap && cList) {
    if (contradictions.length) {
      cWrap.classList.remove("hidden");
      cList.innerHTML = contradictions.map((s) =>
        `<li>${escapeHtml(String(s))}</li>`).join("");
    } else cWrap.classList.add("hidden");
  }

  const aWrap = document.getElementById("review-data-anomalies");
  const aList = document.getElementById("review-data-anomalies-list");
  const anomalies = r.anomalies || [];
  if (aWrap && aList) {
    if (anomalies.length) {
      aWrap.classList.remove("hidden");
      aList.innerHTML = anomalies.map((s) =>
        `<li>${escapeHtml(String(s))}</li>`).join("");
    } else aWrap.classList.add("hidden");
  }

  const corrWrap  = document.getElementById("review-data-corrections");
  const corrTbody = document.getElementById("review-data-corrections-tbody");
  const corrections = r.corrections || [];
  if (corrWrap && corrTbody) {
    if (corrections.length) {
      corrWrap.classList.remove("hidden");
      const sevColor = (s) => ({
        high: THEME.red, med: THEME.amber, low: THEME.blue,
      }[String(s || "").toLowerCase()] || THEME.muted);
      corrTbody.innerHTML = corrections.map((c) => {
        const sc = sevColor(c.severity);
        return `<tr class="hover:bg-Ccard/40">
          <td class="py-1 pr-2 font-mono text-Cwhite text-[10px]">${escapeHtml(String(c.field || "—"))}</td>
          <td class="py-1 pr-2 text-right text-Cmuted font-mono">${escapeHtml(String(c.current ?? "—"))}</td>
          <td class="py-1 pr-2 text-right text-Cgreen font-mono">${escapeHtml(String(c.suggested ?? "—"))}</td>
          <td class="py-1 pr-2 text-Cmuted">${escapeHtml(String(c.reason || "").slice(0, 200))}</td>
          <td class="py-1">
            <span class="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded"
                  style="color:${sc};background:${hexA(sc, 0.15)}">
              ${escapeHtml(String(c.severity || "—"))}
            </span>
          </td>
        </tr>`;
      }).join("");
    } else corrWrap.classList.add("hidden");
  }
}

function renderSmartVerdict(v) {
  // Smart verdict now merged into findings verdict hero — no separate panel
}

function renderSmartNextActions(actions) {
  // Next actions now rendered by smart findings panel (if present)
}

function renderSmartOpenGaps(gaps) {
  // Open gaps now rendered by smart findings panel (if present)
}

// ── Findings audit coverage strip ─────────────────────────────
function renderFindingsAuditCoverage(ac) {
  const wrap = document.getElementById("findings-audit-coverage");
  if (!wrap) return;
  if (!ac) { wrap.classList.add("hidden"); return; }

  wrap.classList.remove("hidden");

  const setBadge = (id, label, status) => {
    const el = document.getElementById(id);
    if (!el) return;
    const c = status === "loaded" || status === "customer" ? THEME.green
            : status === "partial" || status === "default" ? THEME.amber
            : THEME.muted;
    el.textContent = `${label}: ${(status || "missing").toUpperCase()}`;
    el.style.color = c;
    el.style.borderColor = hexA(c, 0.4);
    el.style.background = hexA(c, 0.1);
  };

  setBadge("fac-evidence", "Evidence", ac.evidence_30day || "missing");
  setBadge("fac-sla", "SLA Source", ac.sla_source || "missing");
  setBadge("fac-confidence", `Confidence ${ac.confidence || 0}%`,
    (ac.confidence || 0) >= 80 ? "loaded" : (ac.confidence || 0) >= 60 ? "partial" : "missing");
}

// ── Findings severity donut chart ─────────────────────────────
let _findingsDonutChart = null;
function renderFindingsDonut(summary) {
  // Donut removed — severity counts are now in the verdict hero
}

// ── Render findings verdict hero + table ──────────────────────
// ── Findings table helpers ───────────────────────────────────────────────────
function _short(text, max = 72) {
  if (!text) return "";
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

let _findingsViewMode = "finding"; // "finding" | "root"

function _setFindingsMode(mode) {
  _findingsViewMode = mode;
  const btnF = document.getElementById("btn-mode-finding");
  const btnR = document.getElementById("btn-mode-rootcause");
  if (btnF) {
    btnF.className = `px-2.5 py-1 font-semibold transition-colors border-r border-Cborder/50 ${mode === "finding" ? "bg-Cpurple/30 text-Cwhite" : "bg-transparent text-Cmuted"}`;
  }
  if (btnR) {
    btnR.className = `px-2.5 py-1 font-semibold transition-colors ${mode === "root" ? "bg-Cpurple/30 text-Cwhite" : "bg-transparent text-Cmuted"}`;
  }
  _applyFindingsSort();
}

function groupFindingsByRootCause(findings) {
  const map = new Map();
  for (const f of findings) {
    const key = (f.root_cause || "UNCLASSIFIED").replace(/_/g, " ").trim().toUpperCase() || "UNCLASSIFIED";
    if (!map.has(key)) map.set(key, { root: key, crit: 0, warn: 0, info: 0, ok: 0, examples: [], sources: new Set() });
    const g = map.get(key);
    if (f.level === "critical") g.crit++;
    else if (f.level === "warning") g.warn++;
    else if (f.level === "ok") g.ok++;
    else g.info++;
    if (g.examples.length < 5) g.examples.push(f);
    if (f.source) g.sources.add((f.source || "").toUpperCase());
  }
  return [...map.values()].sort((a, b) => b.crit - a.crit || b.warn - a.warn);
}

// Drawer open/close
window.peOpenFinding = function(idx) {
  const arr = window._lastRealFindings || [];
  const f = arr[idx];
  if (!f) return;
  const d     = document.getElementById("pe-finding-drawer");
  const body  = document.getElementById("pe-finding-drawer-body");
  const title = document.getElementById("pe-finding-drawer-title");
  const pill  = document.getElementById("pe-finding-drawer-pillar");
  if (!d || !body || !title) return;
  if (pill) {
    const SRC_LBL = { batch:"BATCH", sla:"SLA", resource:"INFRA", benchmark:"UI BENCH", sow:"SOW", issues:"ISSUES" };
    const sev = (f.level || "info").toUpperCase();
    const sevCol = sev === "CRITICAL" ? "#f43f5e" : sev === "WARNING" ? "#f59e0b" : sev === "OK" ? "#10d96e" : "#6b7db3";
    pill.innerHTML = `<span style="color:${sevCol}">${sev}</span>${f.source ? ` · ${SRC_LBL[f.source] || f.source.toUpperCase()}` : ""}`;
  }
  title.textContent = f.text || "Finding detail";
  const impactText = _findingImpactText(f) || "—";
  const _row = (label, val, col) => val ? `<div class="rounded-lg p-3" style="background:#0d1526;border:1px solid #213060">
    <div class="text-[8px] uppercase tracking-widest font-bold mb-1" style="color:${col || "#6b7db3"}">${label}</div>
    <div class="text-[11px] leading-relaxed" style="color:#e2e8f0">${_esc(val)}</div>
  </div>` : "";
  body.innerHTML = [
    _row("⚡ Root Cause",         (f.root_cause || "").replace(/_/g, " ") || "—",  "#f59e0b"),
    _row("📋 Business Impact",    impactText,                                       "#3b82f6"),
    _row("→ Recommended Action",  f.recommendation || "—",                          "#10d96e"),
    f.sub    ? _row("Context",  f.sub,    "#8899bb") : "",
    f.evidence ? _row("Evidence", f.evidence, "#6b7db3") : "",
    _row("Evidence Class", (f.evidence_class || "N/A").toUpperCase(), "#6b7db3"),
    _row("Confidence", f.confidence != null ? `${f.confidence}%` : "N/A", "#6b7db3"),
  ].join("");
  d.style.transform = "translateX(0)";
  const bd = document.getElementById("pe-finding-backdrop");
  if (bd) bd.classList.remove("hidden");
};

window.peCloseDrawer = function() {
  const d = document.getElementById("pe-finding-drawer");
  if (d) d.style.transform = "translateX(100%)";
  const bd = document.getElementById("pe-finding-backdrop");
  if (bd) bd.classList.add("hidden");
};

function _findingImpactText(f) {
  const impact = (f?.impact || "").trim();
  if (impact) return impact;
  switch ((f?.root_cause || "").trim().toUpperCase()) {
    case "RELEASE_SLA_IMPACT":
      return "New-release runtime regression will directly impact production SLA compliance if deployed as-is";
    default:
      return "";
  }
}

function renderFindingsAsRootCause(findings) {
  const tbody = document.getElementById("findings-tbody");
  const thead = document.getElementById("findings-thead");
  if (!tbody) return;
  const groups = groupFindingsByRootCause(findings);
  if (thead) {
    thead.innerHTML = `<tr class="border-b border-Cborder/60 text-[10px] uppercase tracking-wider text-Cmuted bg-Ccard2/20">
      <th class="px-4 py-2.5 min-w-[220px]">Root Cause</th>
      <th class="px-3 py-2.5 w-28">Severity</th>
      <th class="px-3 py-2.5 w-28">Breakdown</th>
      <th class="px-3 py-2.5">Top Recommended Action</th>
      <th class="px-3 py-2.5 w-20">Pillars</th>
    </tr>`;
  }
  tbody.innerHTML = groups.map(g => {
    const sev = g.crit > 0 ? "CRITICAL" : g.warn > 0 ? "WARNING" : g.ok > 0 ? "OK" : "INFO";
    const col = sev === "CRITICAL" ? "#f43f5e" : sev === "WARNING" ? "#f59e0b" : sev === "OK" ? "#10d96e" : "#6b7db3";
    const sample = g.examples[0] || {};
    const action = _short(sample.recommendation || _findingImpactText(sample) || "—", 70);
    const pills  = [...g.sources].map(s =>
      `<span class="text-[8px] px-1.5 py-0.5 rounded" style="background:${hexA(THEME.muted,.1)};color:${THEME.muted}">${s}</span>`
    ).join(" ");
    const detId = `rcdet-${g.root.replace(/\W/g,"_")}`;
    return `<tr class="border-b border-white/5 hover:bg-white/[0.03] cursor-pointer"
        style="border-left:3px solid ${hexA(col,.5)}"
        onclick="const d=document.getElementById('${detId}');d.classList.toggle('hidden');this.querySelector('.fchev').textContent=d.classList.contains('hidden')?'▸':'▾'">
      <td class="px-4 py-2.5 font-semibold text-[11px]" style="color:${col}">${_esc(g.root)}<span class="fchev ml-2 text-[11px] text-Cmuted/50">▸</span></td>
      <td class="px-3 py-2.5"><span class="text-[9px] font-bold px-2 py-0.5 rounded-full" style="background:${hexA(col,.15)};color:${col}">${sev}</span></td>
      <td class="px-3 py-2.5 text-[10px]" style="color:#6b7db3">${g.crit}C · ${g.warn}W · ${g.info}I</td>
      <td class="px-3 py-2.5 text-[10px]" style="color:#e2e8f0">${_esc(action)}</td>
      <td class="px-3 py-2.5 text-[8px]">${pills}</td>
    </tr>
    <tr id="${detId}" class="hidden border-b border-white/5" style="background:${hexA(col,.03)};border-left:3px solid ${hexA(col,.5)}">
      <td colspan="5" class="px-5 py-3">
        <div class="text-[10px] font-semibold text-Cmuted mb-2">${g.examples.length} finding(s) under this root cause:</div>
        ${g.examples.map((f) => {
          const fc = f.level==='critical'?'#f43f5e':f.level==='warning'?'#f59e0b':f.level==='ok'?'#10d96e':'#6b7db3';
          const fIdx = (window._lastRealFindings||[]).indexOf(f);
          return `<div class="rounded p-2 mb-1.5 text-[10px] cursor-pointer hover:bg-white/5"
               style="background:#0d1526;border:1px solid #213060"
               onclick="event.stopPropagation();window.peOpenFinding(${fIdx})">
            <span class="text-[8px] font-bold px-1.5 py-0.5 rounded mr-1.5" style="background:${hexA(fc,.15)};color:${fc}">${(f.level||"info").toUpperCase()}</span>
            ${_esc(_short(f.text, 100))}
          </div>`;
        }).join("")}
      </td>
    </tr>`;
  }).join("");
}

function renderFindings(findings) {
  window._lastFindings = findings;
  try {
    if (document.getElementById("exec-evidence-ribbon")) {
      _renderExecEvidenceRibbon(findings || []);
    }
  } catch (_) {}

  const tbody   = document.getElementById("findings-tbody");
  const emptyEl = document.getElementById("findings-empty");
  const loading = document.getElementById("findings-loading");
  if (loading) loading.classList.add("hidden");
  if (!tbody) return;

  // Filter out narrative/gaps meta-findings — they go into the hero
  const narrativeFinding = findings.find(f => f.text === "Audit Narrative" && f.icon === "📝");
  const gapsFinding      = findings.find(f => (f.text || "").startsWith("Open Audit Gaps"));
  const realFindings     = findings.filter(f => f !== narrativeFinding && f !== gapsFinding);
  window._lastRealFindings = realFindings;

  if (!realFindings.length) {
    if (emptyEl) emptyEl.classList.remove("hidden");
    tbody.innerHTML = "";
    return;
  }
  if (emptyEl) emptyEl.classList.add("hidden");

  // ── Verdict hero ────────────────────────────────────────────
  const critFindings = realFindings.filter(f => f.level === "critical");
  const warnFindings = realFindings.filter(f => f.level === "warning");
  const critCount = critFindings.length;
  const warnCount = warnFindings.length;
  const okCount   = realFindings.filter(f => f.level === "ok").length;
  const infoCount = realFindings.filter(f => f.level === "info").length;

  // Decision logic: only MEASURED criticals are hard blockers.
  // Inferred/unavailable criticals (e.g., Vision AI parsed resource data)
  // result in REMEDIATE (action required) rather than outright BLOCKED.
  const hardBlockers = critFindings.filter(f =>
    f.evidence_class === "measured" || f.evidence_class === "defaulted");
  const softCriticals = critFindings.filter(f =>
    f.evidence_class === "inferred" || f.evidence_class === "unavailable" || !f.evidence_class);

  let decision, glowColor;
  if (hardBlockers.length > 0) {
    decision = "BLOCKED"; glowColor = THEME.red;
  } else if (softCriticals.length > 0) {
    decision = "BLOCKED"; glowColor = THEME.red;
  } else if (warnCount > 0) {
    decision = "CONDITIONAL"; glowColor = THEME.amber;
  } else {
    decision = "APPROVED"; glowColor = THEME.green;
  }

  // Grade — use shared helper matching Python thresholds + hard floor
  const { grade, label: gradeLabel, color: gc } = _computeGrade(critCount, warnCount, okCount);
  const _GRADE_LABELS = GRADE_LABELS;

  // Animate verdict hero
  const pill = document.getElementById("findings-decision-pill");
  if (pill) {
    pill.textContent = decision;
    pill.style.cssText = `color:${glowColor};border-color:${glowColor};background:${hexA(glowColor,.15)};text-shadow:0 0 20px ${hexA(glowColor,.4)}`;
  }
  const gradePill = document.getElementById("findings-grade-pill");
  if (gradePill) {
    gradePill.textContent = `Grade ${grade} — ${gradeLabel || ""}`;
    gradePill.style.cssText = `color:${gc};border-color:${hexA(gc,.5)};background:${hexA(gc,.12)}`;
  }

  // Customer tag
  const custTag = document.getElementById("findings-customer-tag");
  if (custTag) custTag.textContent = window.appData.customerName ? `Customer: ${window.appData.customerName}` : "";

  // Severity counts in hero
  setText("hero-crit", critCount);
  setText("hero-warn", warnCount);
  setText("hero-ok", okCount);
  setText("hero-info", infoCount);

  // Ambient glow
  const glow = document.getElementById("findings-glow");
  if (glow) {
    glow.style.setProperty("--glow-color", hexA(glowColor, .15));
    glow.style.opacity = "1";
  }

  // Verdict border glow
  const hero = document.getElementById("findings-verdict-hero");
  if (hero) hero.style.borderColor = hexA(glowColor, .5);

  // Narrative text — render as structured lines, not a single blob
  const verdictText = document.getElementById("findings-verdict-text");
  if (verdictText && narrativeFinding && narrativeFinding.sub) {
    const raw = narrativeFinding.sub;
    const lines = raw.split("\n").map(l => l.trim()).filter(Boolean);
    verdictText.innerHTML = lines.map(l => `<span style="display:block;margin-bottom:4px">${escapeHtml(l)}</span>`).join('');
  }

  // Evidence confidence bar
  const measured = realFindings.filter(f => f.evidence_class === "measured").length;
  const total = realFindings.length;
  const confPct = total > 0 ? Math.round((measured / total) * 100) : 0;
  const confBar = document.getElementById("findings-confidence-bar");
  const confLabel = document.getElementById("findings-confidence-pct");
  if (confBar) confBar.style.width = confPct + "%";
  if (confLabel) confLabel.textContent = confPct + "%";

  // Count badge
  const countBadge = document.getElementById("findings-count-badge");
  if (countBadge) countBadge.textContent = realFindings.length;

  // ── Findings table ──────────────────────────────────────────
  if (!window._findingsSort) window._findingsSort = { col: "severity", dir: 1 };
  if (!window._findingsCols) window._findingsCols = {};
  // Smart default: show the highest severity present; fall back to "all"
  // so the table is never empty when the user first opens findings.
  if (!window._findingsFilter) {
    const _hasCrit = realFindings.some(f => f.level === "critical");
    const _hasWarn = realFindings.some(f => f.level === "warning");
    window._findingsFilter = _hasCrit ? "critical" : _hasWarn ? "lp" : "all";
  }
  _renderFindingsColPicker();
  _updateFindingsFilterCounts();
  _applyFindingsSort();

  // Gap A: Sub-App × Day execution-failure heatmap (above the findings table).
  try { renderFailureGrid(window._lastFailureGrid); } catch (_) {}
}

// ── Gap A: Sub-Application × Day execution-failure density heatmap ──────────
// Dynamic columns (one per run_date present), worst-offender rows first.
// Green (no fail) → amber (1 job) → red (2+ jobs), intensity scaled by count.
function renderFailureGrid(grid) {
  const card = document.getElementById("findings-failure-grid-card");
  const host = document.getElementById("findings-failure-grid");
  const subt = document.getElementById("ffg-subtitle");
  if (!card || !host) return;

  // Fallback to the batch payload when the findings response didn't carry it.
  if (!grid && window.appData && window.appData.batch) grid = window.appData.batch.failure_grid;

  if (!grid || !grid.has_data) { card.classList.add("hidden"); host.innerHTML = ""; return; }

  const dates   = Array.isArray(grid.dates)    ? grid.dates    : [];
  const subApps = Array.isArray(grid.sub_apps) ? grid.sub_apps : [];
  const cells   = Array.isArray(grid.cells)    ? grid.cells    : [];
  const maxFail = Math.max(1, Number(grid.max_fail) || 1);
  const rowTot  = grid.row_totals || {};

  card.classList.remove("hidden");

  // All-clear: the failure axis exists but nothing failed in-window.
  if (!subApps.length) {
    if (subt) subt.textContent = `0 failures · ${dates.length} day(s)`;
    host.innerHTML = `<div class="flex items-center gap-2 text-xs py-2" style="color:${THEME.green}">
        <span class="inline-block w-2 h-2 rounded-full" style="background:${THEME.green}"></span>
        No execution failures recorded across ${dates.length} day(s) — every sub-application ended OK.
      </div>`;
    return;
  }

  if (subt) subt.textContent =
    `${grid.total_failed_jobs} failed job-run(s) · ${subApps.length} sub-app(s) · ${dates.length} day(s)`;

  const cmap = {};
  for (const c of cells) cmap[`${c.sub_app}|${c.date}`] = c;

  const _short = (d) => {
    const m = String(d).match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? `${m[2]}/${m[3]}` : String(d).slice(-5);
  };
  const _cellStyle = (c) => {
    if (!c || !c.fail_count) return "background:#0f172a;border:1px solid rgba(255,255,255,.05)";
    const intensity = 0.35 + 0.6 * Math.min(1, c.fail_count / maxFail);
    const base = c.severity === "crit" ? "244,63,94" : "245,158,11";
    return `background:rgba(${base},${intensity.toFixed(2)});border:1px solid rgba(${base},.9)`;
  };

  let html = `<table class="border-separate" style="border-spacing:3px"><thead><tr>`;
  html += `<th class="sticky left-0 z-10 text-left text-[10px] font-semibold text-Cmuted pr-3 pb-1" style="background:${THEME.card}">Sub-Application</th>`;
  for (const d of dates) {
    html += `<th class="text-[9px] font-mono text-Cmuted/80 pb-1 text-center" style="min-width:30px" title="${escapeHtml(String(d))}">${_short(d)}</th>`;
  }
  html += `<th class="text-[9px] font-semibold text-Cmuted pl-2 pb-1 text-center">&Sigma;</th></tr></thead><tbody>`;

  for (const sa of subApps) {
    const saShort = sa.length > 28 ? sa.slice(0, 27) + "\u2026" : sa;
    html += `<tr><td class="sticky left-0 z-10 text-[11px] font-medium text-Cwhite/90 pr-3 whitespace-nowrap" style="background:${THEME.card}" title="${escapeHtml(sa)}">${escapeHtml(saShort)}</td>`;
    for (const d of dates) {
      const c = cmap[`${sa}|${d}`];
      const label = c && c.fail_count ? c.fail_count : "";
      const tip = c && c.fail_count
        ? `${sa} — ${d}: ${c.fail_count} failed job(s)`
        : `${sa} — ${d}: no failures`;
      html += `<td class="text-center align-middle rounded-sm" style="width:30px;height:24px;${_cellStyle(c)}" title="${escapeHtml(tip)}"><span class="text-[10px] font-bold" style="color:#fff">${label}</span></td>`;
    }
    html += `<td class="text-center text-[10px] font-mono font-bold text-Cwhite/80 pl-2">${rowTot[sa] || 0}</td></tr>`;
  }
  html += `</tbody></table>`;
  host.innerHTML = html;
}

/**
 * renderLongpoleHeatmap()
 * Top-N longest jobs × run_date runtime matrix. Answers the analyst's
 * question "which specific jobs eat the batch window, are they consistent
 * day-to-day, and on which days do they spike?". Cell = longest single run
 * (minutes) that day; row stats give avg/max and the share of the typical
 * busy window the job consumes (▲ flag when it crosses the long-pole threshold).
 */
function renderLongpoleHeatmap(lp) {
  const card   = document.getElementById("batch-longpole-card");
  const host   = document.getElementById("batch-longpole-host");
  const subt   = document.getElementById("batch-longpole-subtitle");
  const badge  = document.getElementById("batch-longpole-badge");
  const legend = document.getElementById("batch-longpole-legend");
  if (!card || !host) return;

  if (!lp && window.appData && window.appData.batch) lp = window.appData.batch.longpole_matrix;
  if (!lp || !lp.has_data || !Array.isArray(lp.rows) || !lp.rows.length) {
    card.classList.add("hidden"); host.innerHTML = ""; return;
  }
  card.classList.remove("hidden");

  const dates   = Array.isArray(lp.dates) ? lp.dates : [];
  const rows    = Array.isArray(lp.rows)  ? lp.rows  : [];
  const cells   = Array.isArray(lp.cells) ? lp.cells : [];
  const maxMin  = Math.max(1, Number(lp.max_minutes) || 1);
  const busyRef = Number(lp.busy_ref_hrs) || 0;
  const flagPct = Number(lp.share_pct_flag) || 25;
  const longpoles = rows.filter(r => r.is_longpole).length;

  if (subt) subt.textContent =
    `${rows.length} longest jobs · ${dates.length} day(s) · typical busy window ${busyRef.toFixed(1)}h · cell = longest run that day (min)`;

  // ── Critical-path sentence: name WHY this panel matters + the top contributor.
  const critEl = document.getElementById("batch-longpole-critpath");
  if (critEl) {
    const topRow = rows.reduce((a, b) =>
      (Number(b.window_share_pct) || 0) > (Number(a.window_share_pct) || 0) ? b : a, rows[0] || {});
    const topShare = Number(topRow.window_share_pct) || 0;
    let lead = "These jobs define your effective batch critical path — any growth here eats directly into the window buffer.";
    if (topShare > 0 && topRow.job) {
      lead += ` Biggest single contributor: ${_esc(String(topRow.job))} at ${topShare.toFixed(0)}% of the ${busyRef.toFixed(1)}h busy window`;
      lead += longpoles > 0
        ? ` — ${longpoles} job(s) cross the ${flagPct}% long-pole line (▲), so trimming them frees the most headroom.`
        : `; no single job dominates, so the risk is aggregate concurrency rather than one runaway job.`;
    }
    critEl.textContent = lead;
  }
  if (badge) {
    badge.classList.remove("hidden");
    if (longpoles > 0) {
      badge.className = "metric-badge metric-badge-amber";
      badge.textContent = `${longpoles} long-pole (≥${flagPct}% of window)`;
    } else {
      badge.className = "metric-badge metric-badge-green";
      badge.textContent = "No single dominating job";
    }
  }

  const cmap = {};
  for (const c of cells) cmap[`${c.job}|${c.date}`] = c;

  const _short = (d) => {
    const m = String(d).match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? `${m[2]}/${m[3]}` : String(d).slice(-5);
  };
  // teal (short) → amber (longer) → red (longest run) by share of matrix max.
  const _cellStyle = (mins) => {
    if (!mins) return "background:#0f172a;border:1px solid rgba(255,255,255,.05)";
    const t = Math.min(1, mins / maxMin);
    const base = t < 0.5 ? "45,212,191" : t < 0.8 ? "245,158,11" : "244,63,94";
    const intensity = 0.30 + 0.6 * t;
    return `background:rgba(${base},${intensity.toFixed(2)});border:1px solid rgba(${base},.8)`;
  };

  let html = `<table class="border-separate" style="border-spacing:3px"><thead><tr>`;
  html += `<th class="sticky left-0 z-10 text-left text-[10px] font-semibold text-Cmuted pr-3 pb-1" style="background:${THEME.card}">Job</th>`;
  for (const d of dates) {
    html += `<th class="text-[9px] font-mono text-Cmuted/80 pb-1 text-center" style="min-width:30px" title="${escapeHtml(String(d))}">${_short(d)}</th>`;
  }
  html += `<th class="text-[9px] font-semibold text-Cmuted pl-2 pb-1 text-center" title="Average single-run minutes">avg</th>`;
  html += `<th class="text-[9px] font-semibold text-Cmuted pl-1 pb-1 text-center" title="Longest single run">max</th>`;
  html += `<th class="text-[9px] font-semibold text-Cmuted pl-1 pb-1 text-center" title="Average runtime as % of the typical daily busy window">share</th></tr></thead><tbody>`;

  for (const r of rows) {
    const j = String(r.job || "");
    const jShort = j.length > 30 ? j.slice(0, 29) + "\u2026" : j;
    const flag = r.is_longpole ? `<span style="color:#f59e0b">\u25B2 </span>` : "";
    const rowTip = `${j} — ${r.stability}, ${r.runs} run(s) over ${r.days_present}/${r.days_total} days`;
    html += `<tr><td class="sticky left-0 z-10 text-[11px] font-medium text-Cwhite/90 pr-3 whitespace-nowrap" style="background:${THEME.card}" title="${escapeHtml(rowTip)}">${flag}${escapeHtml(jShort)}</td>`;
    for (const d of dates) {
      const c = cmap[`${j}|${d}`];
      const mins = c ? Number(c.minutes) : 0;
      const tip = c ? `${j} — ${d}: longest run ${mins.toFixed(0)} min` : `${j} — ${d}: did not run`;
      const label = mins ? Math.round(mins) : "";
      html += `<td class="text-center align-middle rounded-sm" style="width:30px;height:24px;${_cellStyle(mins)}" title="${escapeHtml(tip)}"><span class="text-[9px] font-bold" style="color:#fff">${label}</span></td>`;
    }
    const shareCol = r.is_longpole ? "#f59e0b" : "#8899bb";
    const shareTxt = r.window_share_pct ? r.window_share_pct.toFixed(0) + "%" : "\u2014";
    html += `<td class="text-center text-[10px] font-mono text-Cwhite/80 pl-2">${Number(r.avg_min).toFixed(0)}</td>`;
    html += `<td class="text-center text-[10px] font-mono text-Cwhite/80 pl-1">${Number(r.max_min).toFixed(0)}</td>`;
    html += `<td class="text-center text-[10px] font-mono font-bold pl-1" style="color:${shareCol}">${shareTxt}</td></tr>`;
  }
  html += `</tbody></table>`;
  host.innerHTML = html;

  if (legend) {
    legend.innerHTML = `
      <span class="inline-flex items-center gap-1"><span class="inline-block w-3 h-3 rounded-sm" style="background:rgba(45,212,191,.7)"></span> shorter run</span>
      <span class="inline-flex items-center gap-1"><span class="inline-block w-3 h-3 rounded-sm" style="background:rgba(245,158,11,.8)"></span> longer</span>
      <span class="inline-flex items-center gap-1"><span class="inline-block w-3 h-3 rounded-sm" style="background:rgba(244,63,94,.85)"></span> longest run</span>
      <span class="inline-flex items-center gap-1" style="color:#f59e0b">\u25B2 long-pole (avg \u2265 ${flagPct}% of the ${busyRef.toFixed(1)}h busy window)</span>
      <span>cell = longest single run that day (min); blank = job didn't run</span>`;
  }
}

/**
 * renderPeReviewSections()
 * Drives the 4 live accordion subsections of "Completed the PE Review" from
 * the FindingsResponse. Called every time findings are re-generated or smart
 * verdict returns. Each section pulls from the corresponding appData pillar.
 */
function renderPeReviewSections(findingsResp, smartVerdictData) {
  const findings = findingsResp?.findings || [];
  const coverage = findingsResp?.data_coverage || {};
  const summary  = findingsResp?.summary || {};
  const ad       = window.appData || {};

  // The card is hidden by default — unhide as soon as we have any findings data
  const card = document.getElementById("pe-narrative-card");
  if (card) card.classList.remove("hidden");

  // Filter findings by source → styled HTML cards
  const _cards = (source) => findings
    .filter(f => f.source === source)
    .map(f => {
      const col = _levelColor(f.level);
      const bg  = _levelBg(f.level);
      const rec  = f.recommendation
        ? `<div class="mt-1 text-xs" style="color:#6b7db3">→ ${_esc(f.recommendation)}</div>` : "";
      const evid = f.evidence
        ? `<div class="mt-0.5 font-mono" style="font-size:9px;color:#4b5e8a">Evidence: ${_esc(f.evidence)}</div>` : "";
      return `<div class="rounded-lg p-3 mb-2 border" style="background:${bg};border-color:${col}30">
        <div class="flex items-start gap-2">
          <span class="text-sm">${f.icon || "•"}</span>
          <div class="flex-1 min-w-0">
            <div class="text-sm font-semibold" style="color:${col}">${_esc(f.text)}</div>
            ${f.sub ? `<div class="text-xs mt-0.5" style="color:#8899bb">${_esc(f.sub)}</div>` : ""}
            ${rec}${evid}
          </div>
          <span class="font-bold uppercase px-1.5 py-0.5 rounded"
            style="font-size:9px;background:${col}22;color:${col};white-space:nowrap">${f.level}</span>
        </div>
      </div>`;
    }).join("") || `<div class="text-xs py-2" style="color:#6b7db3">No findings for this pillar.</div>`;

  const _badge = (ok) => ok
    ? `<span class="font-bold px-1.5 py-0.5 rounded" style="font-size:9px;background:#10d96e22;color:#10d96e">LOADED</span>`
    : `<span class="font-bold px-1.5 py-0.5 rounded" style="font-size:9px;background:#f43f5e22;color:#f43f5e">MISSING</span>`;

  // ── Section 1: Data Volume Analysis (SOW / DFU / SKU) ────────────────────
  const sec1 = document.getElementById("pe-review-data-volume");
  if (sec1) {
    // Read from PDF-parsed SOW first, then fall back to manual form fields
    const sow = ad.sowCompare || (() => {
      const m = _buildSowCompareFromManual();
      if (!m?.metrics?.length) return null;
      const dfu = m.metrics.find(x => x.key === "daily_dfu");
      const sku = m.metrics.find(x => x.key === "daily_sku");
      if (!dfu && !sku) return null;
      return {
        dfu_actual: dfu?.actual || null, dfu_target: dfu?.sow || null,
        sku_actual: sku?.actual || null, sku_target: sku?.sow || null,
        metrics: m.metrics, _manual: true,
      };
    })() || {};
    const dfuActual = sow.dfu_actual != null ? sow.dfu_actual : "—";
    const dfuTarget = sow.dfu_target != null ? sow.dfu_target : "—";
    const dfuPct    = (sow.dfu_achievement_pct != null)
      ? sow.dfu_achievement_pct.toFixed(1) + "%" 
      : (sow.dfu_actual != null && sow.dfu_target > 0 
        ? (sow.dfu_actual / sow.dfu_target * 100).toFixed(1) + "%" 
        : "—");
    const skuActual = sow.sku_actual != null ? sow.sku_actual : "—";
    const skuTarget = sow.sku_target != null ? sow.sku_target : "—";
    // Coverage is true when backend confirmed it OR when manual data exists
    const sowDataLoaded = coverage.sow || !!(sow.dfu_actual || sow.sku_actual);
    // Build metrics rows for manual entry
    const extraRows = (sow.metrics || []).filter(m => m.key !== "daily_dfu" && m.key !== "daily_sku")
      .map(m => {
        const pct = m.pct != null ? m.pct : (m.sow > 0 && m.actual != null ? +(m.actual / m.sow * 100).toFixed(1) : null);
        const col = pct == null ? "#6b7db3" : pct > 110 ? "#f43f5e" : pct >= 70 ? "#10d96e" : "#f59e0b";
        return `<div class="rounded p-2 text-center col-span-1" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">${_esc(m.label || m.key)}</div>
          <div class="text-sm font-bold" style="color:${col}">${m.actual != null ? m.actual.toLocaleString() : "—"}</div>
          ${m.sow != null ? `<div class="text-[8px]" style="color:#4b5e8a">SOW: ${m.sow.toLocaleString()}</div>` : ""}
        </div>`;
      }).join("");
    sec1.innerHTML = `
      <div class="flex items-center gap-2 mb-3">
        <span class="text-xs font-semibold text-Cwhite">Data Source</span>
        ${_badge(sowDataLoaded)}
        <span class="text-xs" style="color:#6b7db3">${sow._manual ? "Manual entry" : "DFU / SKU vs SOW Contract Details"}</span>
      </div>
      ${sowDataLoaded ? `<div class="grid grid-cols-3 gap-2 mb-3">
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">DFU Actual</div>
          <div class="text-sm font-bold text-Cwhite">${_esc(String(dfuActual))}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">DFU SOW Target</div>
          <div class="text-sm font-bold text-Cwhite">${_esc(String(dfuTarget))}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Achievement</div>
          <div class="text-sm font-bold" style="color:${dfuPct === '—' ? '#6b7db3' : parseFloat(dfuPct) >= 80 ? '#10d96e' : '#f43f5e'}">${dfuPct}</div>
        </div>
        ${skuActual !== "—" ? `
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">SKU Actual</div>
          <div class="text-sm font-bold text-Cwhite">${_esc(String(skuActual))}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">SKU Target</div>
          <div class="text-sm font-bold text-Cwhite">${_esc(String(skuTarget))}</div>
        </div>` : ""}
        ${extraRows}
      </div>` : `<div class="text-xs mb-3" style="color:#6b7db3">Upload SOW PDF or enter DFU/SKU in the DFU/SKU tab to populate volume analysis.</div>`}
      ${_cards("sow")}`;
  }

  // ── Section 2: Batch Execution & SLA Compliance ───────────────────────────
  const sec2 = document.getElementById("pe-review-batch-sla");
  if (sec2) {
    const bk    = ad.batch?.kpis || {};
    const comp  = bk.compliance_pct          != null ? bk.compliance_pct.toFixed(1)          + "%" : "—";
    const _wcRaw = (bk.window_day_compliance_pct != null) ? bk.window_day_compliance_pct : bk.batch_window_compliance;
    const wComp = _wcRaw != null ? Number(_wcRaw).toFixed(1) + "%" : "—";
    const breach   = bk.jobs_breach  ?? "—";
    const atRisk   = bk.jobs_at_risk ?? "—";
    const slaSource = ad.slaCeilings ? "Customer XLSX" : "PE defaults";
    // Gap 4: wall-clock deadline compliance (single canonical source)
    const dl       = bk.deadline_compliance || {};
    const dlHas    = !!dl.has_deadlines;
    const dlPct    = dl.compliance_pct;
    const dlComp   = (dlHas && dlPct != null) ? Number(dlPct).toFixed(1) + "%" : "—";
    const dlBreach = dl.breach_days || 0;
    const dlColor  = !dlHas ? "#6b7db3" : (dlPct >= 95 ? "#10d96e" : dlPct >= 75 ? "#f59e0b" : "#f43f5e");
    const dlSub    = !dlHas ? "no clock SLA" : (dlBreach > 0 ? `${dlBreach} day(s) late` : "on time");
    const dlSubColor = !dlHas ? "#6b7db3" : (dlBreach > 0 ? "#f43f5e" : "#10d96e");
    sec2.innerHTML = `
      <div class="flex items-center gap-2 mb-3">
        <span class="text-xs font-semibold text-Cwhite">Data Source</span>
        ${_badge(coverage.batch)}
        <span class="text-xs" style="color:#6b7db3">Batch Review + SLA Matrix</span>
        <span class="font-semibold px-1.5 py-0.5 rounded" style="font-size:9px;background:#3b82f622;color:#3b82f6">${_esc(slaSource)}</span>
      </div>
      ${coverage.batch ? `<div class="grid grid-cols-5 gap-2 mb-3">
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060" title="Job-level SLA compliance: each job's peak runtime vs its own SLA ceiling.">
          <div class="text-xs" style="color:#6b7db3">Job Compliance</div>
          <div class="text-sm font-bold" style="color:${comp === '—' ? '#6b7db3' : parseFloat(comp) === 100 ? '#10d96e' : parseFloat(comp) >= 90 ? '#f59e0b' : '#f43f5e'}">${comp}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060" title="Window (day-level) compliance: % of calendar days on which every in-scope sub-app finished within its window. Canonical sign-off metric, consistent with the Executive Dashboard and PE Findings.">
          <div class="text-xs" style="color:#6b7db3">Window Compliance</div>
          <div class="text-sm font-bold" style="color:${wComp === '—' ? '#6b7db3' : parseFloat(wComp) >= 95 ? '#10d96e' : '#f43f5e'}">${wComp}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060" title="Wall-clock deadline compliance: did batches FINISH before their contracted clock ceiling (e.g. 06:00 EST)? Distinct from duration — a batch within its hour budget can still miss the absolute deadline.">
          <div class="text-xs" style="color:#6b7db3">Deadline Compliance</div>
          <div class="text-sm font-bold" style="color:${dlColor}">${dlComp}</div>
          <div class="text-[9px]" style="color:${dlSubColor}">${dlSub}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Breaches</div>
          <div class="text-sm font-bold" style="color:${breach === '—' ? '#6b7db3' : Number(breach) === 0 ? '#10d96e' : '#f43f5e'}">${breach}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">At Risk</div>
          <div class="text-sm font-bold" style="color:${atRisk === '—' ? '#6b7db3' : Number(atRisk) === 0 ? '#10d96e' : '#f59e0b'}">${atRisk}</div>
        </div>
      </div>` : `<div class="text-xs mb-3" style="color:#6b7db3">Upload Ctrl-M CSV to populate batch analysis.</div>`}
      ${_cards("batch")}${_cards("sla")}`;
  }

  // ── Section 3: Infrastructure Utilization & Resource Health ──────────────
  const sec3 = document.getElementById("pe-review-infra");
  if (sec3) {
    const rk     = ad.resource?.kpis || {};
    const grade  = rk.fleet_grade  || "—";
    const avgCpu = rk.avg_cpu != null ? rk.avg_cpu.toFixed(1) + "%" : "—";
    const avgMem = rk.avg_mem != null ? rk.avg_mem.toFixed(1) + "%" : "—";
    const nCrit  = rk.n_critical ?? "—";
    const nSrv   = rk.total_servers ?? (ad.servers?.length ?? "—");
    const gradeCol = grade === "A" ? "#10d96e" : grade === "B" ? "#3b82f6" : grade === "C" ? "#f59e0b" : "#f43f5e";
    sec3.innerHTML = `
      <div class="flex items-center gap-2 mb-3">
        <span class="text-xs font-semibold text-Cwhite">Data Source</span>
        ${_badge(coverage.resource)}
        <span class="text-xs" style="color:#6b7db3">Resource Review</span>
      </div>
      ${coverage.resource ? `<div class="grid grid-cols-4 gap-2 mb-3">
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Fleet Grade</div>
          <div class="text-sm font-bold" style="color:${gradeCol}">${grade}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Avg CPU</div>
          <div class="text-sm font-bold text-Cwhite">${avgCpu}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Avg Memory</div>
          <div class="text-sm font-bold text-Cwhite">${avgMem}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Critical Hosts</div>
          <div class="text-sm font-bold" style="color:${nCrit === '—' ? '#6b7db3' : Number(nCrit) === 0 ? '#10d96e' : '#f43f5e'}">${nCrit} / ${nSrv}</div>
        </div>
      </div>` : `<div class="text-xs mb-3" style="color:#6b7db3">Upload resource report to populate infra analysis.</div>`}
      ${_cards("resource")}`;
  }

  // ── Section 4: UAT / UI Benchmark Validation ─────────────────────────────
  const sec4 = document.getElementById("pe-review-ui-benchmark");
  if (sec4) {
    const bench     = ad.benchmark || {};
    const totalTx   = bench.total_transactions ?? (bench.rows?.length ?? "—");
    const degraded  = bench.degraded ?? "—";
    const passRate  = (typeof totalTx === "number" && totalTx > 0)
      ? ((totalTx - (bench.degraded || 0)) / totalTx * 100).toFixed(1) + "%" : "—";
    const worstDelta = bench.worst_delta_pct != null
      ? "+" + bench.worst_delta_pct.toFixed(0) + "%" : "—";
    sec4.innerHTML = `
      <div class="flex items-center gap-2 mb-3">
        <span class="text-xs font-semibold text-Cwhite">Data Source</span>
        ${_badge(coverage.benchmark)}
        <span class="text-xs" style="color:#6b7db3">UI Benchmark XLSX</span>
      </div>
      ${coverage.benchmark ? `<div class="grid grid-cols-4 gap-2 mb-3">
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Transactions</div>
          <div class="text-sm font-bold text-Cwhite">${totalTx}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Degraded</div>
          <div class="text-sm font-bold" style="color:${degraded === '—' ? '#6b7db3' : Number(degraded) === 0 ? '#10d96e' : '#f43f5e'}">${degraded}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Pass Rate</div>
          <div class="text-sm font-bold" style="color:${passRate === '—' ? '#6b7db3' : parseFloat(passRate) >= 90 ? '#10d96e' : '#f59e0b'}">${passRate}</div>
        </div>
        <div class="rounded p-2 text-center" style="background:#0d1526;border:1px solid #213060">
          <div class="text-xs" style="color:#6b7db3">Worst Δ</div>
          <div class="text-sm font-bold" style="color:${worstDelta === '—' ? '#6b7db3' : parseFloat(worstDelta) > 20 ? '#f43f5e' : '#f59e0b'}">${worstDelta}</div>
        </div>
      </div>` : `<div class="text-xs mb-3" style="color:#6b7db3">Upload UI Benchmark XLSX to populate UAT analysis.</div>`}
      ${_cards("benchmark")}`;
  }

  // ── Update accordion summary labels with live finding counts ─────────────
  // Each <details> has a <summary> with a subtitle <span> — patch it with
  // live counts so the user can see state without expanding.
  const _patchSummary = (containerId, label, pillarSources) => {
    const container = document.getElementById(containerId);
    if (!container) return;
    const details = container.closest("details");
    if (!details) return;
    const subtitle = details.querySelector("summary span.text-xs");
    if (!subtitle) return;
    const pFindings = findings.filter(f => pillarSources.includes(f.source));
    const nCrit = pFindings.filter(f => f.level === "critical").length;
    const nWarn = pFindings.filter(f => f.level === "warning").length;
    const nOk   = pFindings.filter(f => f.level === "ok").length;
    if (nCrit > 0) {
      subtitle.textContent = `${nCrit} critical · ${nWarn} warning`;
      subtitle.style.color = "#f43f5e";
    } else if (nWarn > 0) {
      subtitle.textContent = `${nWarn} warning`;
      subtitle.style.color = "#f59e0b";
    } else if (nOk > 0 || (pFindings.length === 0 && (
      (containerId === "pe-review-batch-sla"   && coverage.batch)    ||
      (containerId === "pe-review-infra"       && coverage.resource) ||
      (containerId === "pe-review-data-volume" && coverage.sow)      ||
      (containerId === "pe-review-ui-benchmark"&& coverage.benchmark)
    ))) {
      subtitle.textContent = "✓ No issues detected";
      subtitle.style.color = "#10d96e";
    } else {
      subtitle.textContent = label;
      subtitle.style.color = "#6b7db3";
    }
  };
  _patchSummary("pe-review-data-volume",    "DFU / SKU vs SOW Contract Details", ["sow"]);
  _patchSummary("pe-review-batch-sla",      "Batch Review + SLA Matrix",         ["batch", "sla"]);
  _patchSummary("pe-review-infra",          "Resource Review",                   ["resource"]);
  _patchSummary("pe-review-ui-benchmark",   "UI Benchmark XLSX",                 ["benchmark"]);

  // Auto-expand the section with the most critical findings
  const sectionSources = [
    { id: "pe-review-batch-sla",      sources: ["batch","sla"] },
    { id: "pe-review-infra",          sources: ["resource"]    },
    { id: "pe-review-data-volume",    sources: ["sow"]         },
    { id: "pe-review-ui-benchmark",   sources: ["benchmark"]   },
  ];
  let maxCrit = -1, maxId = null;
  for (const { id, sources } of sectionSources) {
    const n = findings.filter(f => sources.includes(f.source) && f.level === "critical").length;
    if (n > maxCrit) { maxCrit = n; maxId = id; }
  }
  if (maxCrit > 0 && maxId) {
    const el = document.getElementById(maxId)?.closest("details");
    if (el) el.open = true;
  }

  // ── Verdict badge + reason ────────────────────────────────────────────────
  const badge  = document.getElementById("pe-review-verdict-badge");
  const reason = document.getElementById("pe-review-verdict-reason");
  const verdict = smartVerdictData?.verdict || {};
  if (badge) {
    const dec = verdict.decision
      || (summary.critical > 0 ? "BLOCKED" : summary.warning > 0 ? "CONDITIONAL" : "APPROVED");
    const col = dec === "APPROVED" ? "#10d96e" : dec === "CONDITIONAL" ? "#f59e0b" : "#f43f5e";
    badge.textContent   = dec;
    badge.style.color   = col;
    badge.style.borderColor = col + "55";
    badge.style.background  = col + "18";
  }
  if (reason) {
    reason.textContent = verdict.summary || verdict.headline
      || (summary.critical > 0
        ? `${summary.critical} critical issue(s) must be resolved before sign-off`
        : summary.warning > 0
          ? `${summary.warning} warning(s) require acknowledgement`
          : "All pillars reviewed — ready for sign-off");
  }
}


const _FCOL_DEFS = [
  { id: "severity",   label: "Severity",       w: "w-24" },
  { id: "root_cause", label: "Root Cause",      w: "w-36" },
  { id: "impact",     label: "Impact",          w: "w-44" },
  { id: "action",     label: "Action Required", w: "w-44" },
  { id: "evidence",   label: "Evidence",        w: "w-20", center: true },
];

function _filterFindings(fid) {
  window._findingsFilter = fid;
  _updateFindingsFilterCounts();
  _applyFindingsSort();
}

function _updateFindingsFilterCounts() {
  const all = window._lastRealFindings || [];
  const crit = all.filter(f => f.level === "critical").length;
  const lp   = all.filter(f => f.level === "critical" || f.level === "warning").length;
  const ok   = all.filter(f => f.level === "ok" || f.level === "info").length;

  const setN = (id, n) => { const el = document.getElementById(id); if (el) el.textContent = n ? `(${n})` : ""; };
  setN("ffil-all-n",     all.length);
  setN("ffil-crit-n",    crit);
  setN("ffil-lp-n",      lp);
  setN("ffil-improved-n",ok);

  // Active state on filter pills
  const cur = window._findingsFilter || "critical";
  document.querySelectorAll(".findings-filter-btn").forEach(b => {
    const fid = b.getAttribute("onclick")?.match(/'([^']+)'/)?.[1];
    b.classList.toggle("active-filter", fid === cur);
    b.setAttribute("data-fid", fid || "");
  });

  // Ambient card glow + alert bar
  const card  = document.getElementById("findings-table-card");
  const bar   = document.getElementById("findings-alert-bar");
  const msg   = document.getElementById("findings-alert-msg");
  const showCrit = (cur === "critical" || cur === "lp") && crit > 0;
  if (card) card.classList.toggle("findings-card-alert", showCrit);
  if (bar)  bar.classList.toggle("hidden", !showCrit);
  if (msg && showCrit) {
    msg.textContent = `${crit} critical finding${crit !== 1 ? "s" : ""} require immediate attention before PE sign-off`;
  }
}

function _updateFindingsSortBtns() {
  const col = (window._findingsSort || {}).col || "severity";
  document.querySelectorAll(".findings-sort-btn").forEach(b => {
    const sc = b.getAttribute("onclick")?.match(/'([^']+)'/)?.[1];
    b.classList.toggle("active-sort", sc === col);
  });
}

function _sortFindings(colId) {
  if (!window._findingsSort) window._findingsSort = { col: "severity", dir: 1 };
  if (window._findingsSort.col === colId) {
    window._findingsSort.dir *= -1;
  } else {
    window._findingsSort = { col: colId, dir: 1 };
  }
  _updateFindingsSortBtns();
  _applyFindingsSort();
}

function _toggleFindingsCol(colId) {
  if (!window._findingsCols) window._findingsCols = {};
  window._findingsCols[colId] = window._findingsCols[colId] === false ? true : false;
  _renderFindingsColPicker();
  _applyFindingsSort();
}

function _renderFindingsColPicker() {
  const picker = document.getElementById("findings-col-picker");
  if (!picker) return;
  if (!window._findingsCols) window._findingsCols = {};
  picker.innerHTML =
    `<div class="px-3 py-1 mb-1 text-[8px] uppercase tracking-widest text-Cmuted/70 font-bold border-b border-Cborder/30">Toggle Columns</div>` +
    _FCOL_DEFS.map(c => {
      const on = window._findingsCols[c.id] !== false;
      return `<label class="flex items-center gap-2.5 px-3 py-1.5 cursor-pointer hover:bg-white/5 rounded-lg select-none">
        <span class="relative inline-flex w-7 h-4 shrink-0">
          <input type="checkbox" ${on ? "checked" : ""} onchange="_toggleFindingsCol('${c.id}')" class="sr-only peer">
          <span class="absolute inset-0 rounded-full transition-colors peer-checked:bg-Cpurple/70 bg-white/10 border border-white/20 pointer-events-none"></span>
          <span class="absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white shadow transition-transform peer-checked:translate-x-3 pointer-events-none"></span>
        </span>
        <span class="text-[11px] text-Cwhite/80">${c.label}</span>
      </label>`;
    }).join("");

  // Close picker when clicking outside (attach once)
  if (!window._findingsPickerListenerAdded) {
    window._findingsPickerListenerAdded = true;
    document.addEventListener("click", (e) => {
      const p = document.getElementById("findings-col-picker");
      const t = document.getElementById("findings-col-toggle");
      if (p && t && !p.contains(e.target) && !t.contains(e.target)) {
        p.classList.add("hidden");
      }
    });
  }
}

function _applyFindingsSort() {
  const allFindings = window._lastRealFindings;
  if (!allFindings || !allFindings.length) return;
  if (!window._findingsSort)   window._findingsSort   = { col: "severity", dir: 1 };
  if (!window._findingsCols)   window._findingsCols   = {};
  if (!window._findingsFilter) window._findingsFilter = "critical";
  const { col, dir } = window._findingsSort;
  const vis = window._findingsCols;
  const flt = window._findingsFilter;

  // Check view mode FIRST — root cause groups bypass normal sort/render
  if (_findingsViewMode === "root") {
    renderFindingsAsRootCause(allFindings);
    return;
  }

  // Apply filter
  let findings;
  switch (flt) {
    case "critical":  findings = allFindings.filter(f => f.level === "critical"); break;
    case "lp":        findings = allFindings.filter(f => f.level === "critical" || f.level === "warning"); break;
    case "improved":  findings = allFindings.filter(f => f.level === "ok" || f.level === "info"); break;
    default:          findings = allFindings; // "all"
  }

  // Update sort button states
  _updateFindingsSortBtns();

  const SEV = { critical: 0, warning: 1, info: 2, ok: 3 };
  const EC  = { measured: 0, inferred: 1, defaulted: 2, waived: 3, unavailable: 4 };
  const SRC = { batch: 0, sla: 1, resource: 2, benchmark: 3, sow: 4, issues: 5 };

  const sorted = [...findings].sort((a, b) => {
    let d = 0;
    switch (col) {
      case "severity":
        d = (SEV[a.level] ?? 9) - (SEV[b.level] ?? 9);
        if (!d) d = (EC[a.evidence_class] ?? 9) - (EC[b.evidence_class] ?? 9);
        if (!d) d = (SRC[a.source] ?? 9) - (SRC[b.source] ?? 9);
        break;
      case "root_cause":
        d = (a.root_cause || "zzz").replace(/_/g, " ").localeCompare((b.root_cause || "zzz").replace(/_/g, " "));
        break;
      case "impact":     d = (a.impact || "").localeCompare(b.impact || ""); break;
      case "action":     d = (a.recommendation || "").localeCompare(b.recommendation || ""); break;
      case "evidence":   d = (EC[a.evidence_class] ?? 9) - (EC[b.evidence_class] ?? 9); break;
      case "finding":    d = (a.text || "").localeCompare(b.text || ""); break;
      default:           d = (SEV[a.level] ?? 9) - (SEV[b.level] ?? 9);
    }
    return d * dir;
  });

  // ── thead ────────────────────────────────────────────────────
  const thead = document.getElementById("findings-thead");
  if (thead) {
    const si = (id) => col === id
      ? `<span class="ml-0.5 text-[8px]" style="color:${THEME.purple}">${dir === 1 ? "▲" : "▼"}</span>`
      : `<span class="ml-0.5 text-[8px] opacity-25">⇅</span>`;
    const th = `cursor-pointer select-none hover:text-Cwhite/80 transition-colors font-semibold px-3 py-2.5`;
    const visCols = _FCOL_DEFS.filter(c => vis[c.id] !== false);
    thead.innerHTML = `<tr class="border-b border-Cborder/60 text-[10px] uppercase tracking-wider text-Cmuted bg-Ccard2/20">
      <th class="pl-4 pr-2 py-2.5 w-8"></th>
      <th class="${th} min-w-[220px]" onclick="_sortFindings('finding')">Finding ${si("finding")}</th>
      ${visCols.map(c => `<th class="${th} ${c.w || ""} ${c.center ? "text-center" : ""}" onclick="_sortFindings('${c.id}')">${c.label} ${si(c.id)}</th>`).join("")}
      <th class="px-3 py-2.5 w-8"></th>
    </tr>`;
  }

  // ── tbody ────────────────────────────────────────────────────
  const tbody = document.getElementById("findings-tbody");
  if (!tbody) return;
  const visSet  = new Set(_FCOL_DEFS.filter(c => vis[c.id] !== false).map(c => c.id));
  const colspan = 3 + visSet.size;

  // Empty filter state
  if (!sorted.length) {
    const emptyMsgs = {
      critical: { icon: "✅", title: "No Critical Findings", sub: "All critical issues resolved — good to proceed." },
      lp:       { icon: "✅", title: "No Critical / Warning Findings", sub: "No LP blockers found." },
      improved: { icon: "ℹ", title: "No OK / Info Findings", sub: "No informational findings in this dataset." },
      all:      { icon: "📭", title: "No Findings", sub: "No audit findings generated yet." },
    };
    const em = emptyMsgs[flt] || emptyMsgs.all;
    tbody.innerHTML = `<tr><td colspan="${colspan}" class="py-10 text-center">
      <div class="text-2xl mb-2">${em.icon}</div>
      <div class="text-sm font-semibold text-Cwhite">${em.title}</div>
      <div class="text-[11px] text-Cmuted mt-1">${em.sub}</div>
    </td></tr>`;
    return;
  }

  const SV_ST = {
    critical: { dot: THEME.red,   bg: "bg-Cred/10",   bd: "border-Cred/40",   tx: "text-Cred"   },
    warning:  { dot: THEME.amber, bg: "bg-Camber/10", bd: "border-Camber/40", tx: "text-Camber" },
    info:     { dot: THEME.blue,  bg: "bg-Cblue/10",  bd: "border-Cblue/40",  tx: "text-Cblue"  },
    ok:       { dot: THEME.green, bg: "bg-Cgreen/10", bd: "border-Cgreen/40", tx: "text-Cgreen" },
  };
  const EC_CLR  = { measured: THEME.green, inferred: THEME.cyan, defaulted: THEME.amber, waived: THEME.purple, unavailable: THEME.muted };
  const EC_LBL  = { measured: "MEASURED", inferred: "INFERRED", defaulted: "ASSUMED",   waived: "WAIVED",     unavailable: "N/A" };
  const SRC_LBL = { batch: "BATCH", sla: "SLA", resource: "INFRA", benchmark: "BENCH", sow: "SOW", issues: "ISSUES" };
  const SRC_CLR = { batch: THEME.blue, sla: THEME.amber, resource: THEME.purple, benchmark: THEME.cyan, sow: THEME.muted, issues: THEME.red };
  const SEV_BDR = { critical: THEME.red, warning: THEME.amber, info: THEME.blue, ok: THEME.green };

  const _col = (v, max = 45) => {
    const s = (v || "—").trim();
    return s.length > max
      ? `<span title="${_esc(s)}" class="cursor-help">${_esc(s.slice(0, max))}…</span>`
      : `<span title="${_esc(s)}">${_esc(s)}</span>`;
  };

  // ── Smart grouping: collapse repetitive same-pattern findings ──────────────
  // Findings with identical (root_cause + level + source) and >3 members get
  // collapsed into a single summary row with expand/collapse. Critical findings
  // are never grouped — they always render individually for full visibility.
  const _groupKey = (f) => {
    if (f.level === "critical") return null; // never group criticals
    return `${f.root_cause || ""}|${f.level}|${f.source || ""}`;
  };
  const _groupMap = new Map();
  sorted.forEach((f, origIdx) => {
    const key = _groupKey(f);
    if (!key) return;
    if (!_groupMap.has(key)) _groupMap.set(key, []);
    _groupMap.get(key).push({ f, origIdx });
  });
  // Build display list: replace groups of >3 with a summary entry
  const GROUP_THRESHOLD = 3;
  const _groupedIds = new Set();
  _groupMap.forEach((members, key) => {
    if (members.length > GROUP_THRESHOLD) members.forEach(m => _groupedIds.add(m.origIdx));
  });
  // Build render list: individual rows + group header rows
  const _renderList = []; // { type: "row"|"group", f?, members?, key? }
  const _seenGroupKeys = new Set();
  sorted.forEach((f, idx) => {
    const key = _groupKey(f);
    if (key && _groupedIds.has(idx)) {
      if (!_seenGroupKeys.has(key)) {
        _seenGroupKeys.add(key);
        const members = _groupMap.get(key);
        _renderList.push({ type: "group", key, members, f: members[0].f });
      }
    } else {
      _renderList.push({ type: "row", f, origIdx: idx });
    }
  });

  // Track expand state per group key
  if (!window._findingsGroupExpanded) window._findingsGroupExpanded = {};

  const _renderGroupHeader = (entry, gidx) => {
    const { members, f, key } = entry;
    const sv      = SV_ST[f.level] || SV_ST.info;
    const srcLbl  = SRC_LBL[f.source] || (f.source || "").toUpperCase() || "—";
    const srcClr  = SRC_CLR[f.source] || THEME.muted;
    const bdrClr  = SEV_BDR[f.level] || THEME.muted;
    const rc      = (f.root_cause || "").replace(/_/g, " ").trim() || "—";
    // Derive group label: strip job-specific suffix, keep the pattern description
    const groupLabel = (() => {
      const texts = members.map(m => m.f.text || "");
      // Find longest common prefix
      let prefix = texts[0] || "";
      for (const t of texts) {
        while (prefix && !t.startsWith(prefix)) prefix = prefix.slice(0, -1);
      }
      prefix = prefix.trim().replace(/[:·—\-,]+$/, "").trim();
      return prefix || rc;
    })();
    const expanded = !!window._findingsGroupExpanded[key];
    const expandId = `fg-${gidx}`;
    return `<tr data-group-header="${_esc(key)}" data-expand-id="${expandId}"
      class="cursor-pointer border-b border-white/5 hover:bg-white/[0.04] transition-colors group"
      style="border-left:3px solid ${hexA(bdrClr, 0.35)}"
      onclick="window._toggleFindingGroup(this, '${_esc(key)}')">
      <td class="pl-3 pr-2 py-2 w-8">
        <span class="inline-block w-2.5 h-2.5 rounded-full shrink-0"
              style="background:${sv.dot};box-shadow:0 0 8px ${hexA(sv.dot,.4)}"></span>
      </td>
      <td class="px-2 py-2" style="min-width:220px;max-width:320px">
        <div class="flex items-center gap-2">
          <span class="text-[10px] text-Cmuted/70 transition-transform inline-block ${expanded ? "rotate-90" : ""}" style="font-size:9px">▶</span>
          <div>
            <div class="text-[11px] font-semibold text-Cwhite/90 leading-snug">${_esc(_short(groupLabel, 72))}</div>
            <div class="flex items-center gap-1.5 mt-0.5 flex-wrap">
              <span class="text-[8px] font-bold px-1.5 py-0.5 rounded"
                    style="background:${hexA(srcClr,.15)};color:${srcClr};border:1px solid ${hexA(srcClr,.3)}">${srcLbl}</span>
              <span class="text-[8px] px-1.5 py-0.5 rounded font-mono"
                    style="background:${hexA(sv.dot,.12)};color:${sv.dot};border:1px solid ${hexA(sv.dot,.25)}">${members.length} findings</span>
              <span class="text-[8px] text-Cmuted/60">${expanded ? "click to collapse" : "click to expand"}</span>
            </div>
          </div>
        </div>
      </td>
      ${visSet.has("severity") ? `<td class="px-3 py-2 w-24">
        <span class="inline-flex items-center text-[9px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-md ${sv.bg} ${sv.tx} ${sv.bd} border">${f.level}</span>
      </td>` : ""}
      ${visSet.has("root_cause") ? `<td class="px-3 py-2 text-[10px] font-medium w-36" style="color:${THEME.amber}">${_col(rc, 30)}</td>` : ""}
      ${visSet.has("impact")     ? `<td class="px-3 py-2 text-[10px] text-Cwhite/50 w-44">—</td>` : ""}
      ${visSet.has("action")     ? `<td class="px-3 py-2 text-[10px] text-Cwhite/50 w-44">—</td>` : ""}
      ${visSet.has("evidence")   ? `<td class="px-3 py-2 w-20 text-center text-[10px] text-Cmuted">—</td>` : ""}
      <td class="px-2 py-2 w-8 text-center">
        <span class="text-[11px] text-Cmuted/50 group-hover:text-Cmuted transition-colors">${expanded ? "▲" : "▼"}</span>
      </td>
    </tr>
    ${expanded ? members.map((m, mi) => _renderSingleRow(m.f, m.origIdx, true)).join("") : ""}`;
  };

  const _renderSingleRow = (f, idx, isChild = false) => {
    const sv      = SV_ST[f.level] || SV_ST.info;
    const rc      = (f.root_cause || "").replace(/_/g, " ").trim() || "—";
    const impact  = _findingImpactText(f) || "—";
    const action  = _short((f.recommendation || "").trim() || "—", 50);
    const ecColor = EC_CLR[f.evidence_class] || THEME.muted;
    const ecLbl   = EC_LBL[f.evidence_class] || "—";
    const srcLbl  = SRC_LBL[f.source] || (f.source || "").toUpperCase() || "—";
    const srcClr  = SRC_CLR[f.source] || THEME.muted;
    const bdrClr  = SEV_BDR[f.level] || THEME.muted;
    const bdrAlpha = f.level === "critical" ? 0.7 : f.level === "warning" ? 0.45 : 0.2;
    const rcColor = f.level === "critical" ? THEME.amber
                  : f.level === "warning"  ? hexA(THEME.amber, .8)
                  : "rgba(240,244,255,.75)";
    const showEcBadge = !visSet.has("evidence");
    const isCrit = f.level === "critical";
    const hoverTip = [f.sub, _findingImpactText(f)].filter(Boolean).join(" · ");
    const gIdx = (window._lastRealFindings || []).indexOf(f);
    const childIndent = isChild ? "pl-8" : "pl-3";

    return `<tr data-idx="${idx}"
  class="hover:bg-white/[0.04] transition-colors cursor-pointer border-b border-white/5 group${isCrit ? " findings-crit-row" : ""}${isChild ? " findings-child-row" : ""}"
  style="border-left:3px solid ${hexA(bdrClr, bdrAlpha)};${isCrit ? "background:rgba(244,63,94,.025)" : ""}${isChild ? ";background:rgba(255,255,255,.012)" : ""}"
  onclick="window.peOpenFinding(${gIdx >= 0 ? gIdx : idx})">
  <td class="${childIndent} pr-2 py-2.5 w-8">
    <span class="inline-block w-2.5 h-2.5 rounded-full shrink-0${isCrit ? " findings-crit-blink" : ""}"
          style="background:${sv.dot};box-shadow:0 0 ${isCrit ? "12" : "8"}px ${hexA(sv.dot, isCrit ? .75 : .45)}"></span>
  </td>
  <td class="px-2 py-2.5" style="min-width:220px;max-width:320px" title="${_esc(hoverTip)}">
    <div class="text-[11px] font-semibold text-Cwhite leading-snug">${_esc(_short(f.text, 72))}</div>
    <div class="flex items-center gap-1.5 mt-1 flex-wrap">
      <span class="text-[8px] font-bold px-1.5 py-0.5 rounded"
            style="background:${hexA(srcClr,.15)};color:${srcClr};border:1px solid ${hexA(srcClr,.3)}">${srcLbl}</span>
      ${showEcBadge ? `<span class="text-[8px] px-1.5 py-0.5 rounded"
            style="background:${hexA(ecColor,.1)};color:${ecColor};border:1px solid ${hexA(ecColor,.25)}">${ecLbl}</span>` : ""}
    </div>
  </td>
  ${visSet.has("severity") ? `<td class="px-3 py-2.5 w-24">
    <span class="inline-flex items-center text-[9px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-md ${sv.bg} ${sv.tx} ${sv.bd} border">${f.level}</span>
  </td>` : ""}
  ${visSet.has("root_cause") ? `<td class="px-3 py-2.5 text-[10px] font-medium w-36" style="color:${rcColor}">${_col(rc, 30)}</td>` : ""}
  ${visSet.has("impact")     ? `<td class="px-3 py-2.5 text-[10px] text-Cwhite/70 w-44">${_col(impact, 50)}</td>` : ""}
  ${visSet.has("action")     ? `<td class="px-3 py-2.5 text-[10px] text-Cwhite/70 w-44" title="${_esc(f.recommendation||'')}">
    ${_esc(action)}
  </td>` : ""}
  ${visSet.has("evidence")   ? `<td class="px-3 py-2.5 w-20 text-center">
    <span class="inline-flex text-[8px] px-1.5 py-0.5 rounded"
          style="background:${hexA(ecColor,.1)};color:${ecColor};border:1px solid ${hexA(ecColor,.25)}"
          title="${_esc(f.evidence || ecLbl)}">${ecLbl}</span>
  </td>` : ""}
  <td class="px-2 py-2.5 w-8 text-center">
    <span class="text-[11px] text-Cmuted/50 group-hover:text-Cmuted transition-colors">→</span>
  </td>
</tr>`;
  };

  tbody.innerHTML = _renderList.map((entry, gidx) => {
    if (entry.type === "group") return _renderGroupHeader(entry, gidx);
    return _renderSingleRow(entry.f, entry.origIdx);
  }).join("");

  // Show count of grouped vs total
  const groupedCount = [..._seenGroupKeys].reduce((n, k) => n + (_groupMap.get(k)?.length || 0), 0);
  const singleCount  = _renderList.filter(e => e.type === "row").length;
  const groupCount   = _renderList.filter(e => e.type === "group").length;
  const countBadge2  = document.getElementById("findings-count-badge");
  if (countBadge2 && groupCount > 0) {
    countBadge2.textContent = `${sorted.length} (${groupCount} group${groupCount > 1 ? "s" : ""} + ${singleCount} single)`;
  }

  // Unused legacy path kept for non-grouped path below — groups already rendered above
  if (false) sorted.map((f, idx) => {
    void f; void idx; // no-op — entire block dead
  });
}

/** Toggle expand/collapse of a grouped findings row. */
window._toggleFindingGroup = function(headerRow, key) {
  if (!window._findingsGroupExpanded) window._findingsGroupExpanded = {};
  window._findingsGroupExpanded[key] = !window._findingsGroupExpanded[key];
  _applyFindingsSort(); // re-render with updated expand state
};


// ── Audit Context Health Bar ──────────────────────────────────
// Polls /api/audit-context and renders the pillar status pills.
// Called when the PE Findings tab is opened and after each upload.
async function refreshAuditContext() {
  try {
    const res = await fetch("/api/audit-context");
    if (!res.ok) return;
    const data = await res.json();
    const status = data.status || {};
    const pct    = data.completeness_pct ?? 0;

    // Completeness badge
    const compEl = document.getElementById("ac-completeness");
    if (compEl) {
      compEl.textContent = `${pct}% complete`;
      compEl.style.color = pct >= 80 ? THEME.green : pct >= 40 ? THEME.amber : THEME.red;
    }

    // Pillar pills — use inline styles so Tailwind CDN scanning is not required
    const pillDefs = [
      { id: "ac-pill-batch",    key: "batch",    label: "Batch" },
      { id: "ac-pill-sla",      key: "sla",      label: "SLA Matrix" },
      { id: "ac-pill-resource", key: "resource", label: "Resource" },
      { id: "ac-pill-sow",      key: "sow",      label: "SOW Contract" },
      { id: "ac-pill-uat",      key: "uat",      label: "UAT" },
    ];
    pillDefs.forEach(({ id, key, label }) => {
      const el = document.getElementById(id);
      if (!el) return;
      const loaded = status[key] === "loaded";
      const dot = el.querySelector("span");
      const c = loaded ? THEME.green : THEME.muted;
      el.style.color       = c;
      el.style.borderColor = hexA(c, loaded ? 0.45 : 0.3);
      el.style.background  = hexA(c, loaded ? 0.12 : 0.04);
      if (dot) {
        dot.style.background = c;
        dot.style.boxShadow  = loaded ? `0 0 6px ${hexA(c, 0.7)}` : "none";
      }
      el.title = loaded
        ? `${label}: data loaded in audit context`
        : `${label}: not yet loaded — upload to populate`;
    });

    // Store for use in narrative/findings
    window.appData = window.appData || {};
    window.appData.auditContext = data;

    // ── Restore appData from session-cache slots when browser state is empty ──
    // Only restore if this tab has an active session (user uploaded files
    // in this tab).  Prevents stale data from a previous engagement bleeding
    // into a fresh page load in a new tab/window.
    //
    // IMPORTANT: We also re-render panels when restoring so the user sees
    // their data immediately after a same-tab reload (F5 / browser refresh).
    const slots = data.slots || {};
    const ad = window.appData;
    if (_isSessionActive()) {
      let _restored = false;
      if (!ad.batch && slots.batch_kpis) {
        const extra = data.extra || {};
        // Prefer full last_batch payload (has filename, top_breaches,
        // sla_heatmap, hour_heatmap, data_coverage, sla_source, etc.)
        if (extra.last_batch && extra.last_batch.kpis) {
          ad.batch = extra.last_batch;
        } else {
          ad.batch = {
            kpis:          slots.batch_kpis           || null,
            top_jobs:      slots.batch_top_jobs        || null,
            window:        slots.daily_window_series   || null,
            anomalies:     slots.regression_df         || null,
            sub_stats:     slots.sub_stats             || null,
            daily_jobs:    extra.daily_jobs             || null,
            hourly_counts: extra.hourly_counts          || null,
          };
        }
        // Restore customer name from batch payload
        if (ad.batch.customer_name) {
          ad.customerName = ad.batch.customer_name;
        }
        // Restore embedded SLA matrix if present
        if (ad.batch.sla_matrix && !ad.slaMatrix) {
          ad.slaMatrix = ad.batch.sla_matrix;
        }
        _restored = true;
      }
      if (!ad.slaMatrix && slots.sla_matrix_kpis) {
        ad.slaMatrix = {
          kpis:             slots.sla_matrix_kpis       || null,
          job_summary:      slots.sla_job_summary       || slots.job_summary || null,
          workflow_summary: slots.workflow_sla_summary   || null,
        };
      }
      if (!ad.sowCompare && slots.sow_contract) {
        ad.sowCompare = {
          _contract:    slots.sow_contract   || null,
          volume_vs_sow: slots.volume_vs_sow || null,
        };
      }
      // Restore benchmark (UAT evidence) from server cache after reload
      if (!ad.benchmark && data.extra?.last_benchmark) {
        const lb = data.extra.last_benchmark;
        if (lb.rows?.length || lb.batch_perf_summary) {
          const slot = (lb.kind === "batch" || lb.batch_perf_summary) ? "batch" : "ui";
          if (slot === "batch") ad.benchmarkBatch = lb;
          else                  ad.benchmarkUI    = lb;
          try {
            _mergeBenchmarkSources();
            _benchUpdateZoneBadges();
            _renderBenchmark(ad.benchmark);
          } catch (e) { console.warn("[pe-dashboard] restore benchmark:", e); }
        }
      }
      if (!ad.slaCeilings && slots.batch_kpis) {
        // Reconstruct SLA ceilings from config (already set by upload)
        try {
          const cfgRes = await fetch("/api/config");
          if (cfgRes.ok) {
            const cfg = await cfgRes.json();
            const ceil = {};
            if (cfg.daily_sla_hrs)   ceil.DAILY   = cfg.daily_sla_hrs;
            if (cfg.weekly_sla_hrs)  ceil.WEEKLY  = cfg.weekly_sla_hrs;
            if (cfg.monthly_sla_hrs) ceil.MONTHLY = cfg.monthly_sla_hrs;
            if (cfg.custom_sla_hrs)  ceil.CUSTOM  = cfg.custom_sla_hrs;
            if (Object.keys(ceil).length) ad.slaCeilings = ceil;
          }
        } catch {}
      }
      // ── Re-render panels with restored data so user sees results on reload ──
      // Stagger across animation frames so the browser can paint between phases
      // and avoid triggering Firefox's "page is slowing down" warning.
      // Phase order: customer name → batch charts → resource table → findings
      if (_restored) {
        const _restorePhases = [];
        if (ad.batch) {
          _restorePhases.push(() => {
            try {
              renderBatchReview(ad.batch);
              _renderBatchIntakeCard(ad.batch);
              if (ad.customerName) {
                applyCustomerName(ad.customerName, { runs: ad.batch.kpis?.total_runs, filename: ad.batch.filename });
              }
            } catch(e) { console.warn("[pe-dashboard] restore batch:", e); }
          });
        }
        if (ad.resource?.servers) {
          _restorePhases.push(() => {
            try { renderResourceTable(ad.resource.servers); }
            catch(e) { console.warn("[pe-dashboard] restore resource:", e); }
          });
        }
        // Findings last — after all panels have painted
        _restorePhases.push(() => { triggerGenerateFindings().catch(() => {}); });

        let _rpi = 0;
        function _nextRestorePhase() {
          if (_rpi < _restorePhases.length) {
            _restorePhases[_rpi++]();
            requestAnimationFrame(_nextRestorePhase);
          }
        }
        requestAnimationFrame(_nextRestorePhase);
      }
    } // end _isSessionActive guard
  } catch {
    // Non-fatal — health bar is cosmetic
  }
}

// ── PE Review Narrative (structured 4-section report) ────────
let _narrativeDebounce = null;

// ── Full PE Review refresh — "browser refresh" semantics ─────────────────
// Re-syncs the audit context from the server session cache, then re-runs the
// findings rule engine, which cascades to the narrative + consultant.
// Wired to the "Refresh Narrative" button so any data changed/uploaded in
// this session is reflected across the entire PE Review in one click.
let _peReviewRefreshing = false;
async function refreshPeReview() {
  if (_peReviewRefreshing) return;
  _peReviewRefreshing = true;
  const btn = document.getElementById("pe-narr-refresh-btn");
  const origHtml = btn ? btn.innerHTML : null;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<svg class="animate-spin h-4 w-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path></svg> Refreshing…`;
  }
  try {
    // 1. Pull latest session-cache state from server (restores any slot
    //    this tab doesn't have yet — batch, SLA, SOW, benchmark)
    await refreshAuditContext();
    // 2. Re-run the deterministic findings rule engine with the fresh
    //    payload. This cascades to PE Narrative + PE Consultant internally.
    await triggerGenerateFindings();
    toast("success", "PE Review refreshed", "Findings, narrative and verdict regenerated from latest session data.");
  } catch (err) {
    _handleFetchError(err);
  } finally {
    _peReviewRefreshing = false;
    if (btn && origHtml != null) { btn.disabled = false; btn.innerHTML = origHtml; }
  }
}

async function triggerPeNarrative() {
  // Debounce: collapse rapid-fire calls into one (500ms)
  if (_narrativeDebounce) clearTimeout(_narrativeDebounce);
  return new Promise(resolve => {
    _narrativeDebounce = setTimeout(() => { _triggerPeNarrativeImpl().then(resolve).catch(resolve); }, 500);
  });
}
async function _triggerPeNarrativeImpl() {
  const card    = document.getElementById("pe-narrative-card");
  const loading = document.getElementById("pe-narr-loading");
  const btn     = document.getElementById("pe-narr-refresh-btn");
  if (!card) return;
  card.classList.remove("hidden");
  if (loading) loading.classList.remove("hidden");
  if (btn) btn.disabled = true;

  const ad = window.appData || {};

  // ── Build sow_compare: SOW PDF response takes priority; fall back to
  // manual DFU/SKU inputs the user typed in the SOW Contract tab. ────────
  let sow_compare = ad.sowCompare || _buildSowCompareFromManual() || null;

  const payload = {
    batch:         ad.batch        || null,
    resource:      ad.resource     || null,
    sla_matrix:    ad.slaMatrix    || null,
    sla_intel:     ad.slaIntelligence || null,
    sla_triage:    _buildSlaTriage(),
    sow_compare,
    benchmark:     ad.benchmark    || null,
    red_flags:     ad.redFlags     || null,
    findings:      ad.findings     || null,
    customer_name: ad.customerName || null,
    deep_dive:     ad.deepDive     || _buildDeepDiveSummary(),
  };

  try {
    const res = await fetch("/api/pe-narrative", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      toast("error", "PE Narrative error", (await res.text()).slice(0, 200));
      return;
    }
    const data = await res.json();
    window.appData.peNarrative = data;
    renderPeNarrative(data);
  } catch (err) {
    _handleFetchError(err);
  } finally {
    if (loading) loading.classList.add("hidden");
    if (btn) btn.disabled = false;
  }
}

// crit → red, warn → amber, ok → green (matches build_batch_panel tones)
function _toneHex(t) {
  return t === "crit" ? THEME.red
       : t === "warn" ? THEME.amber
       : t === "ok"   ? THEME.green
       : THEME.cyan;
}

// Conclusive batch verdict panel: verdict banner + KPI strip + two-clocks
// explainer + root-cause direction. Sourced from judgment_engine.build_batch_panel
// (same numbers as the Final Judgment) so it can never contradict the verdict.
function _batchVerdictPanelHtml(panel) {
  if (!panel || typeof panel !== "object") return "";
  const v      = panel.verdict || {};
  const tone   = v.tone || "warn";
  const tHex   = _toneHex(tone);
  const status = v.status || "";
  const head   = v.headline || "";

  const bannerHtml = `
    <div class="px-4 py-4 border-b border-Cborder/30"
         style="background:linear-gradient(135deg, ${hexA(tHex,0.16)}, ${hexA(tHex,0.04)})">
      <div class="flex items-start gap-3">
        <span class="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-extrabold uppercase tracking-wider border"
              style="color:${tHex};border-color:${hexA(tHex,0.5)};background:${hexA(tHex,0.12)}">
          <span class="inline-block w-2 h-2 rounded-full" style="background:${tHex};box-shadow:0 0 8px ${tHex}"></span>
          ${_esc(status)}
        </span>
        <p class="text-[15px] leading-snug font-bold text-Cwhite">${_esc(head)}</p>
      </div>
    </div>`;

  const kpis = Array.isArray(panel.kpis) ? panel.kpis : [];
  const kpiHtml = kpis.length ? `
    <div class="grid gap-2 px-4 py-3 border-b border-Cborder/30"
         style="grid-template-columns:repeat(auto-fit,minmax(148px,1fr))">
      ${kpis.map(k => {
        const kHex = _toneHex(k.tone);
        const ring = k.binding ? `box-shadow:0 0 0 1px ${hexA(kHex,0.55)}, 0 0 14px ${hexA(kHex,0.18)};` : "";
        return `
        <div class="rounded-lg border border-Cborder/50 bg-Ccard/60 px-3 py-2.5" style="${ring}">
          <div class="flex items-center justify-between gap-1 mb-1.5">
            <span class="text-[10px] uppercase tracking-wider text-Cmuted font-semibold leading-tight">${_esc(k.label || "")}</span>
            ${k.binding ? `<span class="shrink-0 text-[8px] uppercase tracking-wider font-bold px-1.5 py-0.5 rounded" style="color:${kHex};background:${hexA(kHex,0.16)}">BINDING</span>` : ""}
          </div>
          <div class="text-2xl font-extrabold leading-none" style="color:${kHex}">${_esc(String(k.value ?? "—"))}</div>
          ${k.sub ? `<div class="text-[10px] text-Cmuted/80 mt-1.5 leading-tight">${_esc(k.sub)}</div>` : ""}
        </div>`;
      }).join("")}
    </div>` : "";

  const explainerHtml = panel.explainer ? `
    <div class="px-4 py-3 border-b border-Cborder/30" style="background:${hexA(THEME.cyan,0.04)}">
      <span class="inline-block text-[9px] uppercase tracking-wider font-bold px-1.5 py-0.5 rounded mr-2 align-middle"
            style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.14)}">RECONCILED</span>
      <span class="text-[13px] leading-relaxed text-Cwhite/85">${_esc(panel.explainer)}</span>
    </div>` : "";

  const directionHtml = panel.direction ? `
    <div class="px-4 py-3 border-b border-Cborder/30" style="background:${hexA(tHex,0.05)}">
      <span class="inline-block text-[9px] uppercase tracking-wider font-bold px-1.5 py-0.5 rounded mr-2 align-middle"
            style="color:${tHex};background:${hexA(tHex,0.14)}">DIRECTION</span>
      <span class="text-[13px] leading-relaxed font-medium text-Cwhite/90">${_esc(panel.direction)}</span>
    </div>` : "";

  return bannerHtml + kpiHtml + explainerHtml + directionHtml;
}

// Provenance badge — tells the reviewer at a glance whether a section's numbers
// are contract-derived (parsed from an uploaded SOW PDF, higher trust) or manual
// (typed in, unverified). Surfaces the deterministic `provenance` field the
// narrative attaches to the Data Volume section so manual figures are visibly
// flagged before sign-off instead of sitting in the same styling as measured data.
function _provenanceBadgeHtml(prov) {
  if (!prov || !prov.source) return "";
  const tone = prov.tone || "muted";
  const hex  = tone === "ok"   ? THEME.green
             : tone === "warn" ? THEME.amber
             : THEME.muted;
  const icon = tone === "ok" ? "✓" : tone === "warn" ? "⚠" : "•";
  return `
    <div class="px-4 py-2 flex items-start gap-2.5 border-b border-Cborder/30"
         style="background:${hexA(hex, 0.07)}">
      <span class="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full border whitespace-nowrap mt-0.5"
            style="color:${hex};border-color:${hexA(hex, 0.45)};background:${hexA(hex, 0.12)}">
        ${icon} ${_esc(prov.label || "")}
      </span>
      ${prov.note
        ? `<span class="text-[12px] text-Cwhite/65 leading-snug">${_esc(prov.note)}</span>`
        : ""}
    </div>`;
}

function renderPeNarrative(data) {
  if (!data) return;
  const card = document.getElementById("pe-narrative-card");
  if (!card) return;
  card.classList.remove("hidden");

  // Verdict pill
  const verdictColors = {
    APPROVED:    { bg: "bg-Cgreen/20",  fg: "text-Cgreen",  bd: "border-Cgreen/40"  },
    CONDITIONAL: { bg: "bg-Camber/20",  fg: "text-Camber",  bd: "border-Camber/40"  },
    BLOCKED:     { bg: "bg-Cred/20",    fg: "text-Cred",    bd: "border-Cred/40"    },
  };
  const v = (data.verdict || "CONDITIONAL").toUpperCase();
  const vc = verdictColors[v] || verdictColors.CONDITIONAL;
  const vEl = document.getElementById("pe-narr-verdict");
  if (vEl) {
    vEl.textContent = v;
    vEl.className = `text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full border ${vc.bg} ${vc.fg} ${vc.bd}`;
  }

  // Model badge
  setText("pe-narr-model", (data.model || "deterministic").replace("models/", ""));

  // Cited verdict reason — same evidence facts as Final Judgment & Findings table,
  // so the BLOCKED/CONDITIONAL/APPROVED call always traces to specific numbers.
  const vrEl = document.getElementById("pe-narr-verdict-reason");
  if (vrEl) {
    if (data.verdict_reason) {
      vrEl.textContent = data.verdict_reason;
      vrEl.classList.remove("hidden");
    } else {
      vrEl.classList.add("hidden");
    }
  }

  // Summary paragraph
  setText("pe-narr-summary", data.summary || "—");

  // Sections + tables
  const wrap = document.getElementById("pe-narr-sections");
  if (!wrap) return;
  wrap.innerHTML = "";

  const sectionAccent = {
    data_volume:    THEME.cyan,
    batch_sla:      THEME.amber,
    infrastructure: THEME.purple,
    uat:            THEME.green,
  };

  (data.sections || []).forEach((sec, idx) => {
    const accent = sectionAccent[sec.id] || THEME.cyan;
    const block = document.createElement("div");
    block.className = "rounded-xl border border-Cborder/60 bg-Ccard/40 overflow-hidden";

    // Header
    const headHtml = `
      <div class="px-4 py-3 flex items-center gap-3 border-b border-Cborder/40"
           style="background:${hexA(accent, 0.06)}">
        <span class="text-xs font-mono px-2 py-0.5 rounded border"
              style="color:${accent};border-color:${hexA(accent,0.4)};background:${hexA(accent,0.1)}">${idx + 1}</span>
        <h3 class="text-sm font-bold text-Cwhite">${_esc(sec.title || "")}</h3>
      </div>`;

    const proseHtml = sec.prose
      ? `<p class="px-4 py-3 text-[13px] text-Cwhite/80 leading-relaxed border-b border-Cborder/30">${_esc(sec.prose)}</p>`
      : "";

    // Conclusive verdict panel (batch_sla) — rendered above the evidence table.
    const panelHtml = _batchVerdictPanelHtml(sec.panel);

    // Provenance badge (data_volume) — manual-vs-contractual data source flag.
    const provHtml = _provenanceBadgeHtml(sec.provenance);

    // Caption that tells the reader WHAT the table below is (e.g. "Breach days,
    // worst overrun first" vs "Longest jobs — reference only, all within SLA").
    const captionHtml = sec.table_caption
      ? `<p class="px-4 pt-3 pb-1 text-[11px] uppercase tracking-wider font-semibold" style="color:${hexA(accent,0.9)}">${_esc(sec.table_caption)}</p>`
      : "";

    // Table
    const tbl = sec.table || {};
    const headers = tbl.headers || [];
    const rows    = tbl.rows    || [];
    const isAllNa = rows.length === 0
      || rows.every(r => (r || []).every(c => String(c).trim().toUpperCase() === "NA"
                                            || String(c).trim().toUpperCase().startsWith("NA")));
    const tableHtml = headers.length
      ? `<div class="overflow-x-auto">
           <table class="w-full text-left border-collapse text-[13px]">
             <thead>
               <tr style="background:${hexA(accent, 0.08)}">
                 ${headers.map(h => `<th class="px-3 py-2 font-semibold uppercase tracking-wider text-[11px]"
                                          style="color:${accent}">${_esc(String(h))}</th>`).join("")}
               </tr>
             </thead>
             <tbody>
               ${rows.map(r => `<tr class="border-t border-Cborder/30 ${isAllNa ? 'opacity-60' : ''}">
                 ${(r || []).map(c => `<td class="px-3 py-2 text-Cwhite/85">${_esc(String(c))}</td>`).join("")}
               </tr>`).join("")}
             </tbody>
           </table>
         </div>`
      : "";

    block.innerHTML = headHtml + provHtml + panelHtml + proseHtml + captionHtml + tableHtml;
    wrap.appendChild(block);
  });
}

// ── Update summary badge counts ───────────────────────────────
function renderFindingsSummary(summary) {
  // Summary counts now rendered inline in the verdict hero by renderFindings
}

// ── Escape HTML to prevent XSS in dynamic content ─────────────
function _esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Safe number coerce — never returns null/undefined/NaN */
function _n(v, fallback) { const n = Number(v); return Number.isFinite(n) ? n : (fallback ?? 0); }

// ── Toggle API key visibility ──────────────────────────────────
function toggleAiKeyVisibility() {
  const inp = document.getElementById("ai-api-key");
  const eye = document.getElementById("ai-key-eye");
  if (!inp) return;
  if (inp.type === "password") {
    inp.type = "text";
    if (eye) eye.innerHTML = `<path stroke-linecap="round" stroke-linejoin="round" d="M3.98 8.223A10.477 10.477 0 0 0 1.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.451 10.451 0 0 1 12 4.5c4.756 0 8.773 3.162 10.065 7.498a10.522 10.522 0 0 1-4.293 5.774M6.228 6.228 3 3m3.228 3.228 3.65 3.65m7.894 7.894L21 21m-3.228-3.228-3.65-3.65m0 0a3 3 0 1 0-4.243-4.243m4.242 4.242L9.88 9.88"/>`;
  } else {
    inp.type = "password";
    if (eye) eye.innerHTML = `<path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 0 1 0-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.641 0-8.574-3.007-9.964-7.178Z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"/>`;
  }
}

// ── Copy AI result to clipboard ───────────────────────────────
async function copyAiResult() {
  const el = document.getElementById("ai-result-text");
  if (!el?.textContent) return;
  try {
    await navigator.clipboard.writeText(el.textContent);
    toast("success", "Copied", "AI analysis copied to clipboard.");
  } catch {
    toast("info", "Copy failed", "Select and copy the text manually.");
  }
}

// ── Trigger Gemini AI Insight ─────────────────────────────────
async function triggerAiInsight() {
  // Panel removed — bail early.
  if (!document.getElementById("ai-result-area")) return;
  // Key is stored in Settings — read from config cache (no visible input field)
  const apiKey    = window.appData.config?.gemini_api_key
                 || window.appData.geminiKey
                 || "";
  const typeEl    = document.getElementById("ai-type");
  const analysisType = typeEl?.value || "full";

  // UI refs
  const genBtn    = document.getElementById("ai-generate-btn");
  const loadingEl = document.getElementById("ai-loading");
  const resultArea= document.getElementById("ai-result-area");
  const resultEl  = document.getElementById("ai-result-text");
  const errorEl   = document.getElementById("ai-error");
  const badgeEl   = document.getElementById("ai-model-badge");

  // Reset state
  if (errorEl)   { errorEl.classList.add("hidden");    errorEl.textContent = ""; }
  if (resultArea){ resultArea.classList.add("hidden"); }
  if (loadingEl) { loadingEl.classList.remove("hidden"); }
  if (genBtn)    { genBtn.disabled = true; }

  // Build KPI context — handle both nested (.kpis) and flat shapes
  const bk = window.appData.batch?.kpis    || window.appData.batch    || {};
  const rk = window.appData.resource?.kpis || window.appData.resource || {};
  const topJobs = window.appData.batch?.top_jobs
               || window.appData.batch?.jobs
               || [];

  const payload = {
    type:          analysisType,
    api_key:       apiKey,
    batch_kpis:    Object.keys(bk).length ? bk : null,
    resource_kpis: Object.keys(rk).length ? rk : null,
    servers:       window.appData.servers || [],
    issues:        window.appData.issues  || [],
    top_jobs:      Array.isArray(topJobs) ? topJobs.slice(0, 30) : [],
  };

  try {
    const res = await fetch("/api/ai-insight", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });

    const data = await res.json();

    if (!res.ok) {
      const detail = data?.detail || JSON.stringify(data);
      if (errorEl) {
        errorEl.textContent = detail;
        errorEl.classList.remove("hidden");
      } else {
        toast("error", "AI error", detail.slice(0, 200));
      }
      return;
    }

    // Display result
    if (resultEl)  resultEl.textContent = data.text || "(empty response)";
    if (resultArea) resultArea.classList.remove("hidden");

    // Show model badge
    if (badgeEl && data.model) {
      badgeEl.textContent = data.model.replace("models/", "");
      badgeEl.classList.remove("hidden");
    }

    toast("success", "AI Insight ready", `Generated with ${data.model || "Gemini"}.`, 4000);

  } catch (err) {
    const msg = String(err?.message || err);
    if (errorEl) {
      errorEl.textContent = `Network error: ${msg}`;
      errorEl.classList.remove("hidden");
    } else {
      toast("error", "Network error", msg);
    }
  } finally {
    if (loadingEl) loadingEl.classList.add("hidden");
    if (genBtn) {
      genBtn.disabled = false;
      genBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4 text-Cpurple"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09Z"/></svg> Generate AI Insight`;
    }
  }
}


// ════════════════════════════════════════════════════════════
//  PHASE 7 — Overview, Correlation, Red Flags, Data Status
// ════════════════════════════════════════════════════════════

// ── Sidebar data-status dots (Grafana-style color-coded) ─────
function refreshDataStatus() {
  // Clear all classes then set the right status-dot class
  const setDot = (id, active, label = "") => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove("status-dot-green", "status-dot-amber", "status-dot-red",
                        "status-dot-blue",  "status-dot-muted", "animate-pulse");
    if (active) {
      el.classList.add("status-dot-green", "animate-pulse");
    } else {
      el.classList.add("status-dot-muted");
    }
    // Update companion label if it exists
    const labelEl = document.getElementById(id + "-label");
    if (labelEl) labelEl.textContent = label;
  };

  const hasResource  = !!(window.appData.servers?.length || window.appData.resource);
  const hasBatch     = !!(window.appData.batch);
  const hasIssues    = !!(window.appData.issues?.length);
  const hasBenchmark = !!(window.appData.benchmark);
  const hasGemini    = !!(window.appData.geminiKey || window.appData.config?.gemini_api_key);
  const hasSla       = !!(window.appData.slaMatrix || window.appData.batchSlaInfo);
  const hasSow       = !!(window.appData.sowCompare || window.appData.sow);

  const resourceCount = window.appData.servers?.length || 0;
  const batchJobs     = window.appData.batch?.kpis?.total_jobs || 0;
  const slaJobs       = window.appData.slaMatrix?.job_summary?.length || 0;

  setDot("ds-resource",  hasResource,  resourceCount ? `${resourceCount} srv` : "");
  setDot("ds-batch",     hasBatch,     batchJobs ? `${batchJobs} jobs` : "");
  setDot("ds-issues",    hasIssues);
  setDot("ds-benchmark", hasBenchmark);
  setDot("ds-sla",       hasSla,       slaJobs ? `${slaJobs} jobs` : "");
  setDot("ds-sow",       hasSow);
  setDot("ds-gemini",    hasGemini);

  // Show/hide vision-status chip in header
  const visionChip = document.getElementById("vision-status-chip");
  if (visionChip) {
    if (hasGemini) visionChip.classList.remove("hidden"), visionChip.classList.add("flex");
    else visionChip.classList.add("hidden"), visionChip.classList.remove("flex");
  }
}

// ── Shared chart palette ──────────────────────────────────────
const OV_CHARTS = { window: null, subapp: null, corDonut: null };

// ════════════════════════════════════════════════════════════════
//  EXECUTIVE DASHBOARD — Plotly Correlation Charts + KPI Strip
// ════════════════════════════════════════════════════════════════
const _EXEC_LAYOUT_BASE = {
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor:  "rgba(13,21,38,0.6)",
  font:          { family: "Sora, sans-serif", color: "#6b7db3", size: 11 },
  margin:        { l: 50, r: 20, t: 30, b: 40 },
  xaxis:         { gridcolor: "rgba(33,48,96,0.5)", zerolinecolor: "rgba(33,48,96,0.5)" },
  yaxis:         { gridcolor: "rgba(33,48,96,0.5)", zerolinecolor: "rgba(33,48,96,0.5)" },
};
const _EXEC_CFG = _plotlyConfig({ scrollZoom: false });

function _execColor(val) {
  if (val >= 90) return "#f43f5e";
  if (val >= 75) return "#f59e0b";
  if (val >= 50) return "#3b82f6";
  return "#10d96e";
}

async function renderOverview() {
  const hasData = !!(window.appData.batch || window.appData.resource || window.appData.servers?.length);
  const noData  = document.getElementById("exec-no-data");
  const content = document.getElementById("exec-content");
  const loading = document.getElementById("exec-loading");
  if (!hasData) {
    if (noData)  noData.classList.remove("hidden");
    if (content) content.classList.add("hidden");
    if (loading) loading.classList.add("hidden");
    return;
  }
  // Show loading spinner, hide content until API returns
  if (loading) loading.classList.remove("hidden");
  if (noData)  noData.classList.add("hidden");
  if (content) content.classList.add("hidden");

  // Gather payload
  try {
    // BUG-W2 fix: await findings BEFORE building the payload so the exec API
    // receives real findings for the decision gate. If findings fail, proceed
    // with empty array — the exec API handles that gracefully.
    if (!window._lastFindings || !window._lastFindings.length) {
      await triggerGenerateFindings().catch(() => {});
    }

    // Build payload AFTER findings are ready so the findings array is populated
    const bk = window.appData.batch?.kpis || window.appData.batch || {};
    const rk = window.appData.resource?.kpis || window.appData.resource || {};
    // Apply same exclusion filter as findings so excluded utility/export jobs
    // don't skew the executive dashboard grade, SRI scores, or risk count.
    const _execFiltered = window.appData.batch ? _filterBatchUtility(window.appData.batch) : null;
    const payload = {
      batch_kpis:    bk,
      top_jobs:      _execFiltered?.top_jobs     || [],
      top_breaches:  _execFiltered?.top_breaches || [],
      resource_kpis: rk,
      servers:       window.appData.servers?.length ? window.appData.servers : (window.appData.resource?.servers || []),
      sla_data:      window.appData.slaMatrix || {},
      sub_stats:     window.appData.batch?.sub_stats    || [],
      window:        window.appData.batch?.window       || [],
      window_sub_app: window.appData.batch?.window_sub_app || [],
      hourly_counts: window.appData.batch?.hourly_counts || {},
      benchmark:     window.appData.benchmark || null,
      sow_compare:   window.appData.sowCompare || _buildSowCompareFromManual() || null,
      findings:      window._lastFindings || [],   // now populated from await above
      customer_name: window.appData.customerName || null,
      deep_dive:     window.appData.deepDive || _buildDeepDiveSummary(),
    };

    // ── Fast-path: if cached exec data exists and source data unchanged, reuse ──
    const _payloadStr = JSON.stringify(payload);
    const payloadHash = `${_payloadStr.length}:${_simpleHash(_payloadStr)}`;
    if (window._execCache && window._execCacheHash === payloadHash) {
      if (loading) loading.classList.add("hidden");
      if (content) content.classList.remove("hidden");
      const data = window._execCache;
      _renderExecDecisionStrip(data);
      _renderExecKPIs(data.kpis);
      _renderExecBenchmarkSummary(window.appData.benchmark);
      _renderExecResourceHealth(data.server_heatmap, data.kpis);
      _renderExecTopRiskJobs(data.window_risk?.length ? data.window_risk : data.job_sla_bars);
      _renderExecSowPanel(data.sow_panel);
      requestAnimationFrame(() => {
        _renderExecSLABars(data.window_risk?.length ? data.window_risk : data.job_sla_bars);
        _renderExecTemporal(data.temporal, data.kpis);
        _renderExecBreachCalendar(data.breach_calendar);
        _renderExecForecast(window.appData?.batch?.window || [], data.kpis?.sla_daily_hrs || 6);
        _renderSignoffChecklistV2(data.decision);
        _renderExecHotSpots(data);
        _renderExecNarrative(data.narrative ?? window._lastFindings ?? []);
        _renderExecScopeReconcile(data);
      });
      return;
    }

    const res = await fetch("/api/executive-dashboard", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) { toast("error", "Executive dashboard error", (await res.text()).slice(0, 200)); return; }
    const data = await res.json();

    // Cache for fast tab re-entry
    window._execCache = data;
    window._execCacheHash = payloadHash;

    // Data arrived — swap spinner for content
    if (loading) loading.classList.add("hidden");
    if (content) content.classList.remove("hidden");

    // ── Phase 1: instant — lightweight DOM writes (KPIs, strips)
    _renderExecDecisionStrip(data);
    _renderExecKPIs(data.kpis);
    _renderExecBenchmarkSummary(window.appData.benchmark);
    _renderExecResourceHealth(data.server_heatmap, data.kpis);
    _renderExecTopRiskJobs(data.window_risk?.length ? data.window_risk : data.job_sla_bars);
    _renderExecSowPanel(data.sow_panel);

    // ── Phase 2: deferred — heavy Plotly charts staggered across frames
    const deferredCharts = [
      () => _renderExecSLABars(data.window_risk?.length ? data.window_risk : data.job_sla_bars),
      () => _renderExecTemporal(data.temporal, data.kpis),
      () => _renderExecBreachCalendar(data.breach_calendar),
      () => _renderExecForecast(
             window.appData?.batch?.window || [],
             data.kpis?.sla_daily_hrs || 6),
      () => _renderSignoffChecklistV2(data.decision),
      () => _renderExecHotSpots(data),
      () => _renderExecNarrative(data.narrative ?? window._lastFindings ?? []),
      () => _renderExecScopeReconcile(data),
    ];
    // Render one chart per animation frame so KPIs paint immediately
    let ci = 0;
    function _nextChart() {
      if (ci < deferredCharts.length) {
        deferredCharts[ci++]();
        requestAnimationFrame(_nextChart);
      }
    }
    requestAnimationFrame(_nextChart);

    // Auto-trigger Final Judgment in the background (no manual button)
    if (typeof runFinalJudgment === "function") {
      runFinalJudgment().catch(() => {});
    }
  } catch (err) {
    if (loading) loading.classList.add("hidden");
    if (content) content.classList.remove("hidden");
    // Never leave the narrative panel stuck on its loading placeholder —
    // surface an explicit unavailable state instead of an infinite spinner.
    const _narrEl = document.getElementById("exec-narrative");
    if (_narrEl) {
      _narrEl.innerHTML = `<div class="rounded-lg border border-red-500/30 bg-red-900/20 p-3 text-[11px] text-Cred">
        Executive narrative unavailable — the dashboard service did not respond. Retry the Executive tab or re-upload the batch data.
      </div>`;
    }
    _handleFetchError(err);
  }
}

// ── KPI Strip ────────────────────────────────────────────────
// ── Grade advisory — cross-references deep-dive DB server memory with fleet grade ──
function _buildGradeAdvisory(kpis) {
  const dd = window.appData?.deepDive;
  if (!dd || !kpis) return null;
  const servers = window.appData?.resource?.servers || [];
  const dbServers = servers.filter(s => (s.type || "").toUpperCase().includes("DB"));
  if (!dbServers.length) return null;
  let chronicDb = 0;
  for (const srv of dbServers) {
    const vmKey = Object.keys(dd).find(k => k.toLowerCase() === (srv.server || srv.host || "").toLowerCase());
    if (!vmKey) continue;
    const memWf = dd[vmKey]?.waveforms?.["Available Memory Percentage"];
    if (memWf && (memWf.label === "flat_high" || memWf.label === "plateau" || memWf.risk === "critical" || memWf.risk === "high")) {
      chronicDb++;
    }
  }
  if (chronicDb > 0) {
    return `${kpis.total_servers || 0} servers · DB expected allocation (${chronicDb} within SGA/PGA band)`;
  }
  return null;
}

function _renderExecKPIs(kpis) {
  if (!kpis) return;

  const colorFor = (v, good, warn) =>
    v >= good ? "#10d96e" : (v >= warn ? "#f59e0b" : "#f43f5e");

  // Helper: animate a ring (circumference 2πr ≈ 201 for r=32)
  const setRing = (ringId, pct, color) => {
    const ring = document.getElementById(ringId);
    if (!ring) return;
    const C = 2 * Math.PI * 32;
    const p = Math.max(0, Math.min(100, pct));
    ring.setAttribute("stroke-dasharray", C.toFixed(1));
    ring.setAttribute("stroke-dashoffset", (C * (1 - p / 100)).toFixed(1));
    ring.setAttribute("stroke", color);
  };

  // ── OSHS ring ──
  const oshs = _n(kpis.oshs_score);
  const oshsCol = colorFor(oshs, 75, 60);
  const oshsEl = document.getElementById("exec-oshs");
  if (oshsEl) { oshsEl.textContent = oshs.toFixed(1); oshsEl.style.color = oshsCol; }
  setRing("exec-oshs-ring", oshs, oshsCol);
  const gradeEl = document.getElementById("exec-oshs-grade");
  if (gradeEl) {
    gradeEl.textContent = `Grade ${kpis.oshs_grade}`;
    gradeEl.style.color = oshsCol;
  }
  setText("exec-oshs-delta", kpis.oshs_label || "—");

  // ── Job SLA Rate ring + sparkline ──
  const br = _n(kpis.batch_rate);
  const brCol = colorFor(br, 95, 80);
  const brEl = document.getElementById("exec-batch-rate");
  if (brEl) { brEl.textContent = _fmtPctCompact(br); brEl.style.color = brCol; }
  setRing("exec-batch-ring", br, brCol);
  setText("exec-batch-sub", `${kpis.total_jobs || 0} jobs`);
  _drawMiniSparkline("exec-batch-spark",
    (window.appData?.batch?.window || []).map(w =>
      Number(w.success_rate ?? w.sla_rate ?? w.compliance ?? br)
    ), brCol);

  // ── Window Rate ring + sparkline ──
  const wr = _n(kpis.window_compliance ?? kpis.batch_rate);
  const wrCol = colorFor(wr, 95, 80);
  const wrEl = document.getElementById("exec-window-rate");
  if (wrEl) { wrEl.textContent = _fmtPctCompact(wr); wrEl.style.color = wrCol; }
  setRing("exec-window-ring", wr, wrCol);
  const wbd = kpis.window_breach_days ?? 0;
  setText("exec-window-sub", wbd > 0 ? `${wbd} day(s) breached` : "All days OK");
  _drawMiniSparkline("exec-window-spark",
    (window.appData?.batch?.window || []).map(w => {
      const sla = kpis.sla_daily_hrs || 6;
      const hrs = Number(w.total_hrs || 0);
      return hrs > 0 ? Math.max(0, 100 - ((hrs - sla) / sla) * 100) : 100;
    }), wrCol);

  // ── Grade advisory — uses top-level _buildGradeAdvisory() ──
  // ── Fleet Grade — letter + gradient bullet bar ──
  const flEl = document.getElementById("exec-fleet");
  const gradePct = { "A": 95, "B": 80, "C": 65, "D": 50, "F": 25, "N/A": 0 };
  const gc = { "A": "#10d96e", "B": "#10d96e", "C": "#f59e0b", "D": "#f43f5e", "F": "#f43f5e", "N/A": "#6b7db3" };
  if (flEl) {
    flEl.textContent = kpis.fleet_grade || "—";
    flEl.style.color = gc[kpis.fleet_grade] || "#f0f4ff";
  }
  const gradeAdvisory = _buildGradeAdvisory(kpis);
  setText("exec-fleet-sub", gradeAdvisory || `${kpis.total_servers || 0} servers`);
  const flBar = document.getElementById("exec-fleet-bar");
  if (flBar) flBar.style.width = (gradePct[kpis.fleet_grade] || 0) + "%";

  // ── RFCS — number + bullet marker on gradient ──
  const rfEl = document.getElementById("exec-rfcs");
  const rfcs = _n(kpis.rfcs);
  if (rfEl) {
    rfEl.textContent = rfcs.toFixed(0);
    rfEl.style.color = rfcs >= 60 ? THEME.red : (rfcs >= 30 ? THEME.amber : THEME.green);
  }
  const rfMarker = document.getElementById("exec-rfcs-marker");
  if (rfMarker) {
    const rfPct = Math.max(0, Math.min(100, rfcs));
    rfMarker.style.marginLeft = `calc(${rfPct}% - 1px)`;
  }
  setText("exec-rfcs-sub",
    rfcs >= 60 ? "High risk · escalate" : rfcs >= 30 ? "Moderate risk" : "Low risk");
}

// ── Compact percentage formatter (fits inside small KPI rings) ──
function _fmtPctCompact(v) {
  const n = _n(v);
  if (n >= 99.95) return "100%";
  if (n >= 10)    return n.toFixed(0) + "%";
  return n.toFixed(1) + "%";
}

// ── Mini SVG sparkline drawer (used inside KPI cards) ────────
function _drawMiniSparkline(svgId, values, color) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const vals = (values || []).filter(v => Number.isFinite(v));
  if (!vals.length) {
    svg.innerHTML = `<text x="40" y="14" text-anchor="middle" fill="#475569" font-size="7" font-family="Sora">no trend</text>`;
    return;
  }
  const W = 80, H = 22, P = 1.5;
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const step = vals.length > 1 ? (W - 2 * P) / (vals.length - 1) : 0;
  const pts = vals.map((v, i) => {
    const x = P + i * step;
    const y = H - P - ((v - min) / span) * (H - 2 * P);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const last = pts.split(" ").slice(-1)[0].split(",");
  const gradId = svgId + "-grad";
  svg.innerHTML = `
    <defs>
      <linearGradient id="${gradId}" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.5"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0.02"/>
      </linearGradient>
    </defs>
    <path d="M ${P},${H - P} L ${pts.split(" ").join(" L ")} L ${(W - P).toFixed(1)},${H - P} Z" fill="url(#${gradId})"/>
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${last[0]}" cy="${last[1]}" r="1.6" fill="${color}"/>
  `;
}


// ═════════════════════════════════════════════════════════════
//  V2 RENDERERS — Decision Strip · Sub-App Table · Breach Cal ·
//                 Concurrency · SOW · Auto-FJ
// ═════════════════════════════════════════════════════════════

// ── Decision Strip + Next Actions ─────────────────────────────
function _renderExecDecisionStrip(data) {
  const dec   = data?.decision || {};
  const strip = document.getElementById("exec-decision-strip");
  if (!strip) return;

  const status = (dec.status || "").toUpperCase();
  const isReady       = status === "APPROVED";
  const isBlocked     = status === "BLOCKED";
  const isConditional = status === "CONDITIONAL_HOLD";
  const isIncomplete  = status === "INCOMPLETE";
  const tone =
    isReady       ? { fg: "#10d96e", bd: "#10d96e", bg: "rgba(16,217,110,0.06)", lbl: "APPROVED — READY FOR SIGN-OFF" } :
    isConditional ? { fg: "#f59e0b", bd: "#f59e0b", bg: "rgba(245,158,11,0.06)",  lbl: "CONDITIONAL HOLD" } :
    isBlocked     ? { fg: "#f43f5e", bd: "#f43f5e", bg: "rgba(244,63,94,0.06)",  lbl: "BLOCKED" } :
    isIncomplete  ? { fg: "#6b7db3", bd: "#475569", bg: "rgba(107,125,179,0.06)", lbl: "INCOMPLETE — AWAITING DATA" } :
                    { fg: "#94a3b8", bd: "#475569", bg: "rgba(148,163,184,0.06)", lbl: "PENDING" };

  strip.style.borderLeftColor = tone.bd;
  strip.style.background      = tone.bg;

  setText("exec-dec-customer", dec.customer || window.appData.customerName || "—");
  const statusEl = document.getElementById("exec-dec-status");
  if (statusEl) {
    statusEl.textContent = tone.lbl;
    statusEl.style.color = tone.fg;
    statusEl.style.borderColor = tone.bd;
    statusEl.style.background  = hexA(tone.fg, 0.12);
  }
  setText("exec-dec-grade",    dec.grade || "—");
  // Problem 4: grade sub-label + tooltip
  const _GRADE_LABELS = { A: "APPROVED", B: "APPROVED WITH NOTES", C: "CONDITIONAL HOLD", D: "BLOCKED — MINOR", F: "BLOCKED — MAJOR" };
  const grLabelEl = document.getElementById("exec-dec-grade-label");
  const grWrapEl  = document.getElementById("exec-grade-wrap");
  if (grLabelEl) grLabelEl.textContent = _GRADE_LABELS[dec.grade] || "";
  if (grWrapEl)  grWrapEl.title = `Grade ${dec.grade || "—"} = ${_GRADE_LABELS[dec.grade] || "—"}  ·  Score ${dec.score ?? "—"}/100\nA ≥ 90  B ≥ 75  C ≥ 60  D ≥ 45  F < 45`;
  setText("exec-dec-blockers", String(dec.blockers_count ?? 0));
  setText("exec-dec-reason",   dec.reason || "—");
  setText("exec-dec-days",     String(dec.days_covered ?? "—"));
  const now = new Date();
  setText("exec-dec-time",     now.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}));

  // Next Actions (3 owner-tagged flat cards)
  const wrap = document.getElementById("exec-next-actions");
  if (wrap) {
    const actions = dec.next_actions || [];
    wrap.innerHTML = actions.map(a => `
      <div class="rounded-xl border border-Cborder/60 bg-Ccard/40 px-4 py-3">
        <div class="text-[9px] uppercase tracking-[0.18em] text-Cmuted font-bold mb-1">${_esc(a.owner || "")}</div>
        <div class="text-[12px] text-Cwhite/90 leading-snug">${_esc(a.action || "—")}</div>
      </div>
    `).join("");
  }
}

// ── 5-Condition Checklist (V2 — replaces _renderSignoffChecklist) ──
function _renderSignoffChecklistV2(decision) {
  const wrap = document.getElementById("exec-signoff-checklist");
  if (!wrap) return;
  const conds = decision?.conditions || [];
  wrap.innerHTML = conds.map(c => {
    const tone = c.pass ? "#10d96e" : "#f43f5e";
    const icon = c.pass ? "✓" : "✗";
    return `
      <div class="flex items-center gap-3 text-[11px]">
        <span class="w-5 text-center font-bold" style="color:${tone}">${icon}</span>
        <span class="text-Cwhite/90 font-semibold w-40">${_esc(c.label)}</span>
        <span class="text-Cmuted">actual: <span class="text-Cwhite font-mono">${_esc(c.actual)}</span></span>
        <span class="text-Cmuted">required: <span class="text-Cwhite font-mono">${_esc(c.required)}</span></span>
        ${c.blocker ? `<span class="text-[10px] ml-auto" style="color:${tone}">${_esc(c.blocker)}</span>` : ""}
      </div>`;
  }).join("");

  // ── Gate PE sign-off checkbox based on server state machine ──
  const serverStatus = (decision?.status || "").toUpperCase();
  const canSignOff = serverStatus === "APPROVED" || serverStatus === "CONDITIONAL_HOLD";
  const peChk   = document.getElementById("pe-approve-chk");
  const peLabel  = document.getElementById("pe-approve-label");
  const hint     = document.getElementById("pe-checklist-hint");
  if (peChk) {
    peChk.disabled = !canSignOff;
    if (!canSignOff) peChk.checked = false;
  }
  if (peLabel) peLabel.classList.toggle("pointer-events-none", !canSignOff);
  if (hint) {
    hint.classList.toggle("hidden", canSignOff);
    if (!canSignOff) {
      hint.textContent = serverStatus === "INCOMPLETE"
        ? "Upload data to begin the review before sign-off is available."
        : `Sign-off gated — resolve ${decision?.blockers_count || 0} blocker(s) first.`;
    }
  }
}

// ── Sub-App Summary Table ─────────────────────────────────────
function _renderExecSubAppTable(summary) {
  const wrap  = document.getElementById("exec-subapp-table");
  const tipEl = document.getElementById("exec-subapp-tip");
  if (!wrap) return;
  const rows = summary?.rows || [];
  if (!rows.length) {
    wrap.innerHTML = `<p class="text-[11px] text-Cmuted py-6 text-center">No sub-app data available.</p>`;
    if (tipEl) tipEl.textContent = "";
    return;
  }
  const statusTone = (s) => s === "BREACH" ? "#f43f5e" : s === "AT_RISK" ? "#f59e0b" : "#10d96e";
  const head = `
    <thead>
      <tr class="text-[9px] uppercase tracking-wider text-Cmuted">
        <th class="text-left py-2 px-2">Sub-app</th>
        <th class="text-right py-2 px-2">Jobs</th>
        <th class="text-right py-2 px-2">Peak (h)</th>
        <th class="text-right py-2 px-2">Ceiling (h)</th>
        <th class="text-right py-2 px-2">Buffer %</th>
        <th class="text-right py-2 px-2">Status</th>
      </tr>
    </thead>`;
  const body = rows.map(r => `
    <tr class="border-t border-Cborder/30">
      <td class="py-2 px-2 text-Cwhite/90 font-semibold truncate max-w-[140px]">${_esc(r.sub_app)}</td>
      <td class="py-2 px-2 text-right text-Cwhite/80">${r.job_count}</td>
      <td class="py-2 px-2 text-right text-Cwhite/80 font-mono">${r.peak_hrs}</td>
      <td class="py-2 px-2 text-right text-Cmuted font-mono">${r.ceiling}</td>
      <td class="py-2 px-2 text-right font-mono ${r.buffer_pct < 0 ? 'text-Cred' : 'text-Cwhite/80'}">${r.buffer_pct}%</td>
      <td class="py-2 px-2 text-right">
        <span class="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded"
              style="color:${statusTone(r.status)};background:${hexA(statusTone(r.status),0.12)}">${_esc(r.status)}</span>
      </td>
    </tr>`).join("");
  wrap.innerHTML = `<table class="w-full">${head}<tbody>${body}</tbody></table>`;
  if (tipEl) tipEl.textContent = summary?.tip || "";
}

// ── Enhanced Breach Calendar (with SLA ceiling overlay) ───────
function _renderExecBreachCalendar(payload) {
  const el = document.getElementById("exec-chart-breach-cal");
  const sumEl = document.getElementById("exec-breach-cal-summary");
  if (!el || typeof Plotly === "undefined") return;
  // Backwards-compat: legacy callers passed (window, ceiling)
  let days, ceiling, summary;
  if (Array.isArray(payload)) {
    days    = payload;
    ceiling = arguments[1] || 6;
    summary = "";
  } else {
    days    = payload?.days    || [];
    ceiling = payload?.ceiling || 6;
    summary = payload?.summary || "";
  }
  if (!days.length) {
    el.innerHTML = `<p class="text-[11px] text-Cmuted py-12 text-center">No breach calendar data.</p>`;
    if (sumEl) sumEl.textContent = "";
    return;
  }
  const x = days.map(d => d.date || d.run_date || "");
  const y = days.map(d => Number(d.hours ?? d.total_hrs ?? 0));
  const colors = days.map(d => {
    const s = d.status || (d.is_breach ? "breach" : "ok");
    return s === "breach" ? "#f43f5e" : s === "near" ? "#f59e0b" : "#10d96e";
  });
  const hover = days.map(d => {
    const dow = d.day_of_week ? `${d.day_of_week} ` : "";
    const over = (d.over_by ?? 0) >= 0 ? `+${d.over_by}h` : `${d.over_by}h`;
    const tj = (d.top_jobs || []).filter(Boolean).join(", ") || "—";
    // A "sub_app" breach means the daily total is within the daily ceiling but a
    // sub-application exceeded its OWN tighter contracted ceiling — explain it so
    // a red bar sitting below the ceiling line is never read as a contradiction.
    if (d.breach_basis === "sub_app") {
      return `${dow}${d.date}<br>Window: ${(d.hours ?? 0).toFixed?.(2) ?? d.hours}h (within ${d.ceiling ?? ceiling}h daily)<br>Breach: a sub-application exceeded its contracted SLA ceiling<br>Top jobs: ${tj}`;
    }
    return `${dow}${d.date}<br>Window: ${(d.hours ?? 0).toFixed?.(2) ?? d.hours}h<br>Ceiling: ${d.ceiling ?? ceiling}h<br>Δ: ${over}<br>Top jobs: ${tj}`;
  });

  const trace = {
    type: "bar", x, y,
    marker: { color: colors, line: { color: "#0f172a", width: 1 } },
    text: y.map(v => v.toFixed ? v.toFixed(1) : v),
    textposition: "outside",
    textfont: { size: 9, color: "#94a3b8" },
    hovertemplate: hover.map(h => h + "<extra></extra>"),
  };

  const layout = {
    paper_bgcolor: "transparent",
    plot_bgcolor:  "transparent",
    margin: { l: 36, r: 60, t: 12, b: 40 },
    xaxis: { tickfont: { size: 9, color: "#94a3b8" }, gridcolor: "rgba(148,163,184,0.08)" },
    yaxis: {
      title: { text: "Hours", font: { size: 10, color: "#94a3b8" } },
      tickfont: { size: 9, color: "#94a3b8" },
      gridcolor: "rgba(148,163,184,0.08)",
    },
    shapes: [{
      type: "line",
      xref: "paper", x0: 0, x1: 1,
      y0: ceiling, y1: ceiling,
      line: { color: "#f43f5e", width: 2, dash: "dash" },
    }],
    annotations: [
      {
        // Problem 3: anchor SLA label to LEFT (Y-axis) side, not floating right edge
        xref: "paper", yref: "y",
        x: 0.01, y: ceiling, xanchor: "left", yanchor: "bottom",
        text: `SLA: ${ceiling}h`,
        font: { size: 10, color: "#f43f5e", family: "monospace" },
        showarrow: false,
        bgcolor: "rgba(13,21,38,0.7)",
        borderpad: 2,
      },
    ],
    showlegend: false,
  };
  Plotly.react(el, [trace], layout, _plotlyConfig({ scrollZoom: false }));
  if (sumEl) sumEl.textContent = summary || "";

  // Enterprise: export toolbar
  _addChartToolbar(el.parentElement, el, () => {
    let csv = "Date,Window_Hrs,SLA_Ceiling,Status\n";
    days.forEach(d => { csv += `${d.date || d.run_date},${d.hours ?? d.total_hrs ?? 0},${d.ceiling ?? ceiling},${d.status || ""}\n`; });
    return csv;
  });
}

// ── SOW vs Actual panel (3-column) ────────────────────────────
function _renderExecSowPanel(panel) {
  const card  = document.getElementById("exec-sow-card");
  const grid  = document.getElementById("exec-sow-grid");
  const empty = document.getElementById("exec-sow-empty");
  const stat  = document.getElementById("exec-sow-status");
  if (!card) return;

  if (!panel?.available) {
    // Collapse entirely — don't waste executive dashboard space
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");
  if (grid)  grid.classList.remove("hidden");
  if (empty) empty.classList.add("hidden");

  const overall = (panel.overall_status || "").toUpperCase();
  const tone =
    overall === "OPTIMAL"  ? "#10d96e" :
    overall === "MODERATE" ? "#22d3ee" :
    overall === "HIGH"     ? "#f43f5e" :
    overall === "LOW"      ? "#f59e0b" : "#94a3b8";
  if (stat) {
    stat.textContent = overall || "OK";
    stat.style.color = tone;
    stat.style.borderColor = hexA(tone, 0.5);
    stat.style.background  = hexA(tone, 0.12);
  }

  // Col 1: Volume bars (Plotly grouped bar)
  const volumeRows = panel.volume || [];
  // Fallback: use volume_by_year from session-cache enrichment when no sow_compare metrics
  const volByYear = panel.volume_by_year || {};
  const yearEntries = Object.entries(volByYear).sort(([a], [b]) => a.localeCompare(b));
  const volEl = document.getElementById("exec-sow-volume");
  const deltaEl = document.getElementById("exec-sow-volume-delta");
  if (volEl && typeof Plotly !== "undefined") {
    if (volumeRows.length) {
      const labels = volumeRows.map(r => r.label || r.key);
      const sow    = volumeRows.map(r => Number(r.sow    || 0));
      const actual = volumeRows.map(r => Number(r.actual || 0));
      const traces = [
        { type: "bar", name: "SOW",    x: labels, y: sow,
          marker: { color: "#475569" } },
        { type: "bar", name: "Actual", x: labels, y: actual,
          marker: { color: "#22d3ee" } },
      ];
      Plotly.react(volEl, traces, {
        paper_bgcolor: "transparent", plot_bgcolor: "transparent",
        margin: { l: 40, r: 12, t: 12, b: 36 },
        barmode: "group",
        legend: { font: { size: 9, color: "#94a3b8" }, orientation: "h", y: 1.15 },
        xaxis: { tickfont: { size: 8, color: "#94a3b8" } },
        yaxis: { tickfont: { size: 8, color: "#94a3b8" }, gridcolor: "rgba(148,163,184,0.08)" },
      }, _plotlyConfig({ scrollZoom: false }));
      // Delta label — biggest deviation
      const deltas = volumeRows.map(r => ({
        label: r.label || r.key,
        delta: Number(r.actual || 0) - Number(r.sow || 0),
        // Problem 2: compute pct from actual/sow when backend returns 0
        pct:   Number(r.pct || 0) || (Number(r.sow || 0) > 0
          ? Math.round(Number(r.actual || 0) / Number(r.sow || 0) * 100)
          : (Number(r.actual || 0) > 0 ? 999 : 0)),
      })).sort((a,b) => Math.abs(b.delta) - Math.abs(a.delta));
      const top = deltas[0];
      if (deltaEl && top) {
        const sign = top.delta >= 0 ? "+" : "";
        const tone2 = top.pct > 110 ? "#f43f5e" : top.pct < 70 ? "#f59e0b" : "#10d96e";
        deltaEl.textContent = `${top.label}: ${sign}${top.pct.toFixed(1)}% vs SOW`;
        deltaEl.style.color = tone2;
      }
    } else if (yearEntries.length) {
      // Render contracted volume by year (from SOW PDF — no actuals yet)
      const labels = yearEntries.map(([yr]) => yr);
      const vals   = yearEntries.map(([, v]) => Number(v) || 0);
      const traces = [{
        type: "bar", name: "Contracted Volume",
        x: labels, y: vals,
        marker: { color: "#475569" },
        text: vals.map(v => v >= 1e6 ? (v/1e6).toFixed(1)+"M" : v >= 1e3 ? (v/1e3).toFixed(0)+"K" : String(v)),
        textposition: "outside",
        textfont: { size: 9, color: "#94a3b8" },
      }];
      Plotly.react(volEl, traces, {
        paper_bgcolor: "transparent", plot_bgcolor: "transparent",
        margin: { l: 40, r: 12, t: 20, b: 36 },
        legend: { font: { size: 9, color: "#94a3b8" }, orientation: "h", y: 1.15 },
        xaxis: { tickfont: { size: 9, color: "#94a3b8" }, title: { text: "Contract Year", font: { size: 9 } } },
        yaxis: { tickfont: { size: 8, color: "#94a3b8" }, gridcolor: "rgba(148,163,184,0.08)" },
      }, _plotlyConfig({ scrollZoom: false }));
      if (deltaEl) {
        deltaEl.textContent = `SOW volume profile from ${labels[0]} to ${labels[labels.length - 1]}`;
        deltaEl.style.color = "#94a3b8";
      }
    } else {
      volEl.innerHTML = `<div class="text-center py-6">
        <p class="text-[11px] text-Cmuted mb-2">No SOW volume actuals loaded yet.</p>
        <p class="text-[10px] text-Cmuted/70 mb-3">Upload a SOW PDF via the Upload tab, then run SOW Compare to see achievement % against contracted thresholds (DFUs, SKUs, Orders, Batch Jobs).</p>
        <button onclick="document.querySelector('[data-tab=upload]')?.click()" class="px-3 py-1 text-[10px] rounded border border-Cblue/40 text-Cblue hover:bg-Cblue/10 transition">Go to Upload</button>
      </div>`;
      if (deltaEl) deltaEl.textContent = "";
    }
  }

  // Col 2: SLA reference — breach detail lives in Breach Calendar, not here (Problem 2)
  const slaSum = document.getElementById("exec-sow-sla-summary");
  if (slaSum) {
    const ceiling = panel.sla_hrs ?? window._execCache?.kpis?.sla_daily_hrs ?? "—";
    slaSum.innerHTML = ceiling !== "—"
      ? `<span class="font-semibold text-Cwhite/90">Contracted SLA ceiling: ${ceiling}h</span> per batch window.`
      : `<span class="text-Cmuted">SLA ceiling not specified — upload SOW document to set the contracted target.</span>`;
  }

  // Col 3: Capacity — hide entire column when no data (Problem 2)
  const capEl    = document.getElementById("exec-sow-capacity");
  const capColEl = document.getElementById("exec-sow-cap-col");
  const sowGrid  = document.getElementById("exec-sow-grid");
  if (capEl) {
    const cap = panel.capacity || [];
    if (!cap.length) {
      if (capColEl) capColEl.classList.add("hidden");
      if (sowGrid) {
        sowGrid.classList.remove("lg:grid-cols-3");
        sowGrid.classList.add("lg:grid-cols-2");
      }
    } else {
      if (capColEl) capColEl.classList.remove("hidden");
      if (sowGrid) {
        sowGrid.classList.remove("lg:grid-cols-2");
        sowGrid.classList.add("lg:grid-cols-3");
      }
      capEl.innerHTML = cap.map(c => {
        const tone3 = c.status === "Within baseline" ? "#10d96e"
                    : c.status === "Approaching limit" ? "#f59e0b" : "#f43f5e";
        const pct = Math.min(100, Math.max(0, c.actual / Math.max(c.sow, 1) * 100));
        return `
          <div>
            <div class="flex items-center justify-between text-[10px] text-Cmuted mb-1">
              <span class="font-semibold text-Cwhite/90">${_esc(c.label)}</span>
              <span style="color:${tone3}" class="font-mono">${c.actual}% / ${c.sow}%</span>
            </div>
            <div class="h-1.5 rounded-full bg-Cborder/40 overflow-hidden relative">
              <div class="h-full" style="width:${pct.toFixed(1)}%;background:${tone3}"></div>
              <div class="absolute top-0 bottom-0 border-r border-dashed" style="left:80%;border-color:${hexA(tone3,0.6)}"></div>
            </div>
            <div class="text-[10px] mt-0.5" style="color:${tone3}">${_esc(c.status)}</div>
          </div>`;
      }).join("");
    }
  }
}

// Stubs for legacy renderers — actual implementations below have null
// guards for missing DOM elements, so duplicates are harmless. We removed
// any conflicting redeclarations above.


// ── Sign-off Readiness Strip + Audit Readiness Cards ─────────
function _renderExecSignoffStrip(data) {
  // Sign-off verdict strip
  const verdictEl = document.getElementById("exec-signoff-verdict");
  const detailEl  = document.getElementById("exec-signoff-detail");
  if (!verdictEl) return;

  // Audit readiness cards — declare sla FIRST to avoid TDZ
  const dc  = (window.appData.batch || {}).data_coverage || {};
  const sla = (window.appData.batch || {}).sla_source || {};

  const findings  = window._lastFindings || [];
  const critCount = findings.filter(f => f.level === "critical").length;
  const warnCount = findings.filter(f => f.level === "warning").length;

  const blockers = findings.filter(f => f.level === "critical").map(f => f.text);
  const slaBlocked = sla.blocked;
  const causes = [];
  if (blockers.some(b => /batch window/i.test(b))) causes.push("batch-window overruns");
  if (blockers.some(b => /cpu|critical state/i.test(b))) causes.push("critical CPU concentration");
  if (blockers.some(b => /sla.*breach/i.test(b))) causes.push("SLA breaches");
  if (blockers.some(b => /evidence|insufficient/i.test(b))) causes.push("incomplete evidence");
  if (slaBlocked) causes.push("SLA from assumed defaults");

  // Use server-side decision state if available from executive dashboard
  const serverDecision = window._execCache?.decision;
  const serverStatus = (serverDecision?.status || "").toUpperCase();

  if (serverStatus === "APPROVED") {
    verdictEl.textContent = "APPROVED — READY FOR SIGN-OFF";
    verdictEl.style.color = THEME.green;
    verdictEl.style.borderColor = THEME.green;
    verdictEl.style.background = hexA(THEME.green, 0.1);
    detailEl.textContent = serverDecision?.reason || "All reviewed areas within thresholds.";
  } else if (serverStatus === "BLOCKED") {
    verdictEl.textContent = "SIGN-OFF BLOCKED";
    verdictEl.style.color = THEME.red;
    verdictEl.style.borderColor = THEME.red;
    verdictEl.style.background = hexA(THEME.red, 0.1);
    detailEl.textContent = serverDecision?.reason || `${critCount} critical, ${warnCount} warnings · Blockers: ${causes.join(", ") || "see findings"}`;
  } else if (serverStatus === "CONDITIONAL_HOLD") {
    verdictEl.textContent = "CONDITIONAL HOLD";
    verdictEl.style.color = THEME.amber;
    verdictEl.style.borderColor = THEME.amber;
    verdictEl.style.background = hexA(THEME.amber, 0.1);
    detailEl.textContent = serverDecision?.reason || `${warnCount} warning(s) require review before sign-off.`;
  } else if (serverStatus === "INCOMPLETE") {
    verdictEl.textContent = "INCOMPLETE — AWAITING DATA";
    verdictEl.style.color = THEME.muted;
    verdictEl.style.borderColor = THEME.muted;
    verdictEl.style.background = hexA(THEME.muted, 0.1);
    detailEl.textContent = serverDecision?.reason || "Upload batch and resource data to begin the PE review.";
  } else {
    // Fallback: derive from local findings if executive not yet loaded
    if (critCount === 0 && warnCount === 0 && !slaBlocked) {
      verdictEl.textContent = "READY FOR SIGN-OFF";
      verdictEl.style.color = THEME.green;
      verdictEl.style.borderColor = THEME.green;
      verdictEl.style.background = hexA(THEME.green, 0.1);
      detailEl.textContent = "No critical or warning findings. All reviewed areas within thresholds.";
    } else if (critCount > 0 || slaBlocked) {
      verdictEl.textContent = "SIGN-OFF BLOCKED";
      verdictEl.style.color = THEME.red;
      verdictEl.style.borderColor = THEME.red;
      verdictEl.style.background = hexA(THEME.red, 0.1);
      detailEl.textContent = `${critCount} critical, ${warnCount} warnings · Blockers: ${causes.join(", ") || "see findings"}`;
    } else {
      verdictEl.textContent = "CONDITIONAL";
      verdictEl.style.color = THEME.amber;
      verdictEl.style.borderColor = THEME.amber;
      verdictEl.style.background = hexA(THEME.amber, 0.1);
      detailEl.textContent = `${warnCount} warning(s) require review before sign-off.`;
    }
  }

  const span = dc.date_span_days || 0;
  const evEl = document.getElementById("exec-evidence");
  if (evEl) {
    evEl.textContent = span >= 30 ? "30d+" : span >= 14 ? `${span}d` : span > 0 ? `${span}d` : "NONE";
    evEl.style.color = span >= 30 ? THEME.green : span >= 14 ? THEME.amber : THEME.red;
    setText("exec-evidence-sub", span >= 30 ? "Full coverage" : span > 0 ? "Partial — need 30d" : "No batch data");
  }

  const conf = dc.confidence || 0;
  const confEl = document.getElementById("exec-confidence");
  if (confEl) {
    confEl.textContent = `${conf}%`;
    confEl.style.color = conf >= 80 ? THEME.green : conf >= 60 ? THEME.amber : THEME.red;
    setText("exec-confidence-sub", dc.confidence_label || "—");
  }

  const sqEl = document.getElementById("exec-sla-quality");
  if (sqEl) {
    const isMatrix = _isCustomerSlaType(sla.type);
    const isAssumed = !sla.type || sla.type === "default" || sla.type === "assumed";
    const slaBlocked = sla.blocked;
    if (isMatrix) {
      sqEl.textContent = "MATRIX";
      sqEl.style.color = THEME.green;
      setText("exec-sla-quality-sub",
        `${sla.detected_model || sla.schema_type || "SLA file"} · ${sla.valid_rows || "?"} rules`);
    } else if (isAssumed) {
      sqEl.textContent = slaBlocked ? "BLOCKED" : "DEFAULT";
      sqEl.style.color = slaBlocked ? THEME.red : THEME.amber;
      setText("exec-sla-quality-sub",
        slaBlocked ? "Assumed defaults — green blocked" : "Using system defaults");
    } else {
      sqEl.textContent = "FALLBACK";
      sqEl.style.color = THEME.amber;
      setText("exec-sla-quality-sub", "Customer fallback — partial confidence");
    }
  }

  const srEl = document.getElementById("exec-signoff-ready");
  if (srEl) {
    srEl.textContent = critCount === 0 ? (warnCount === 0 ? "YES" : "COND") : "NO";
    srEl.style.color = critCount === 0 ? (warnCount === 0 ? THEME.green : THEME.amber) : THEME.red;
    setText("exec-signoff-ready-sub",
      critCount > 0 ? `${critCount} blockers` : warnCount > 0 ? `${warnCount} warnings` : "All clear");
  }

  const vsEl = document.getElementById("exec-vol-sow");
  if (vsEl) {
    const hasSow = !!(window.appData.sowCompare);
    vsEl.textContent = hasSow ? "LOADED" : "N/A";
    vsEl.style.color = hasSow ? THEME.green : THEME.muted;
    setText("exec-vol-sow-sub", hasSow ? "SOW comparison active" : "No SOW data");
  }
}


// ── Breach Calendar (legacy v1 removed; v2 implementation lives above) ──


// ── Evidence-Confidence Ribbon ───────────────────────────────
function _renderExecEvidenceRibbon(findings) {
  const el = document.getElementById("exec-evidence-ribbon");
  if (!el) return;

  const classes = { measured: 0, inferred: 0, defaulted: 0, waived: 0, unavailable: 0 };
  for (const f of (findings || [])) {
    const ec = f.evidence_class || "measured";
    if (ec in classes) classes[ec]++;
  }
  const total = Object.values(classes).reduce((a, b) => a + b, 0);
  if (!total) {
    el.innerHTML = `<div class="flex flex-col items-center justify-center text-center py-8 px-4">
      <div class="text-3xl opacity-40 mb-2">🔍</div>
      <div class="text-[12px] text-Cmuted leading-snug">Findings still computing — ribbon will appear once PE Findings finishes.</div>
    </div>`;
    return;
  }

  const colorMap = {
    measured:    THEME.green,
    inferred:    THEME.cyan,
    defaulted:   THEME.amber,
    waived:      THEME.purple,
    unavailable: THEME.muted,
  };
  const labelMap = {
    measured:    "Source-backed",
    inferred:    "Document-derived",
    defaulted:   "Default SLA",
    waived:      "Waived",
    unavailable: "Unavailable",
  };

  // Horizontal stacked bar
  const barHtml = Object.entries(classes).filter(([, v]) => v > 0).map(([cls, v]) => {
    const pct = (v / total * 100).toFixed(0);
    return `<div style="width:${pct}%;background:${hexA(colorMap[cls], 0.6)};min-width:20px"
                 class="h-6 flex items-center justify-center text-[9px] font-bold text-Cwhite"
                 title="${labelMap[cls]}: ${v} finding(s) (${pct}%)">${pct}%</div>`;
  }).join("");

  const legendHtml = Object.entries(classes).filter(([, v]) => v > 0).map(([cls, v]) =>
    `<span class="inline-flex items-center gap-1 text-[10px]">
      <span class="w-2 h-2 rounded-full inline-block" style="background:${colorMap[cls]}"></span>
      ${labelMap[cls]} (${v})
    </span>`
  ).join(" ");

  el.innerHTML = `
    <div class="flex rounded-lg overflow-hidden">${barHtml}</div>
    <div class="flex flex-wrap gap-3 mt-2">${legendHtml}</div>
    <p class="text-[10px] text-Cmuted mt-1">PE audit decisions depend on evidence trust level. Defaulted and inferred findings require additional validation.</p>
  `;
}

function _renderExecBenchmarkSummary(bench) {
  const el = document.getElementById("exec-benchmark-summary");
  if (!el) return;
  if (!bench) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");

  // Slot-aware: a UI section only renders when a UI benchmark source exists.
  const hasUI    = !!window.appData.benchmarkUI;
  const hasBatch = !!(bench.batch_perf_summary);
  const cats = hasUI ? (bench.categories || []) : [];
  const fr = bench.fill_rate || [];
  const obs = bench.observations || [];
  const totalTx = hasUI ? (window.appData.benchmarkUI.total_transactions || 0) : 0;
  const degraded = hasUI ? (window.appData.benchmarkUI.degraded || 0) : 0;
  const referenceOnly = hasUI && window.appData.benchmarkUI.reference_only === true;
  const passRate = totalTx > 0 ? Math.round((totalTx - degraded) / totalTx * 100) : 0;
  const corr = bench.correlation;
  const color = (corr && corr.verdict === "NO-GO") ? "border-Cred/40 bg-Cred/5"
    : referenceOnly ? "border-Ccyan/40 bg-Ccyan/5"
    : (degraded > 0 || (hasBatch && bench.batch_perf_summary.regressions > 0))
      ? "border-Camber/40 bg-Camber/5" : "border-Cgreen/40 bg-Cgreen/5";

  let catCards = "";
  cats.forEach(c => {
    const pct = c.total > 0 ? Math.round(c.passed / c.total * 100) : 0;
    const badge = referenceOnly ? "text-Ccyan" : (c.degraded > 0 ? "text-Cred" : "text-Cgreen");
    const meta = referenceOnly
      ? `${c.total} raw capture row${c.total !== 1 ? "s" : ""}`
      : `${c.passed}/${c.total} pass${c.degraded > 0 ? ` · ${c.degraded} red` : ""}`;
    catCards += `<div class="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-Ccard/50 border border-Cborder/30">
      <span class="text-lg font-bold ${badge}">${referenceOnly ? "REF" : `${pct}%`}</span>
      <div class="text-[10px]"><div class="text-Cwhite font-semibold">${_esc(c.name)}</div>
        <div class="text-Cmuted">${meta}</div>
      </div></div>`;
  });

  const uiBlock = hasUI ? `
    <div class="flex items-center gap-3 mb-3">
      <span class="text-lg">⚡</span>
      <div>
        <div class="text-sm font-bold text-Cwhite">UI Performance Benchmark</div>
        <div class="text-[10px] text-Cmuted">${totalTx} transactions${referenceOnly ? " · reference only" : ` · ${passRate}% pass rate`}${fr.length ? ` · ${fr.length} fill rate entries` : ""}${obs.length ? ` · ${obs.length} SIT obs` : ""}</div>
      </div>
    </div>
    ${catCards ? `<div class="flex flex-wrap gap-2 mt-2">${catCards}</div>` : ""}` : "";

  el.innerHTML = `<div class="rounded-xl border ${color} p-4">
    ${uiBlock}
    ${hasUI && hasBatch ? "" : ""}
    ${_execBatchPerfBlock(hasBatch ? bench.batch_perf_summary : null, !hasUI)}
    ${_execCorrelationBlock(corr)}
  </div>`;
}

/** Compact batch-runtime block for the executive benchmark summary card. */
function _execBatchPerfBlock(bp, isFirst) {
  if (!bp) return "";
  const net = _n(bp.net_delta_secs);
  const netMin = Math.abs(net / 60).toFixed(1);
  const dir = net >= 0 ? "saved" : "added";
  const dirCol = net >= 0 ? "text-Cgreen" : "text-Cred";
  const regrCol = _n(bp.regressions) > 0 ? "text-Cred" : "text-Cgreen";
  const sep = isFirst ? "" : "mt-3 pt-3 border-t border-Cborder/40";
  return `<div class="${sep}">
    <div class="flex items-center gap-3 mb-1">
      <span class="text-lg">⏱️</span>
      <div class="text-sm font-bold text-Cwhite">Batch Runtime Performance</div>
    </div>
    <div class="flex flex-wrap gap-2 mt-1">
      <div class="px-3 py-1.5 rounded-lg bg-Ccard/50 border border-Cborder/30 text-[10px]">
        <span class="text-lg font-bold ${regrCol}">${_n(bp.regressions)}</span>
        <span class="text-Cmuted"> regressions / ${_n(bp.comparable)} comparable</span>
      </div>
      <div class="px-3 py-1.5 rounded-lg bg-Ccard/50 border border-Cborder/30 text-[10px]">
        <span class="text-lg font-bold text-Cgreen">${_n(bp.improvements)}</span>
        <span class="text-Cmuted"> improved</span>
      </div>
      ${_n(bp.suspect) > 0 ? `<div class="px-3 py-1.5 rounded-lg bg-Camber/10 border border-Camber/30 text-[10px]">
        <span class="text-lg font-bold text-Camber">${_n(bp.suspect)}</span>
        <span class="text-Cmuted"> suspect (no-data?)</span>
      </div>` : ""}
      <div class="px-3 py-1.5 rounded-lg bg-Ccard/50 border border-Cborder/30 text-[10px]">
        <span class="text-lg font-bold ${dirCol}">${net >= 0 ? "−" : "+"}${netMin}m</span>
        <span class="text-Cmuted"> net ${dir}/run</span>
      </div>
    </div>
  </div>`;
}

/** Release-readiness verdict block for the executive benchmark summary card. */
function _execCorrelationBlock(corr) {
  if (!corr) return "";
  const V = {
    "GO":          { col: "Cgreen", tag: "RELEASE READY" },
    "CONDITIONAL": { col: "Camber", tag: "CONDITIONAL" },
    "NO-GO":       { col: "Cred",   tag: "NOT READY" },
  }[corr.verdict] || { col: "Cmuted", tag: "" };
  const sys = corr.systemic
    ? `<span class="text-[10px] text-Cred font-mono ml-2">⚠ ${corr.shared_subsystems.length} systemic subsystem(s)</span>`
    : (corr.layers.length === 2 ? `<span class="text-[10px] text-Cgreen font-mono ml-2">✓ isolated</span>` : "");
  return `<div class="mt-3 pt-3 border-t border-Cborder/40 flex items-center gap-3">
    <div class="text-2xl font-mono font-extrabold text-${V.col} tabular-nums">${corr.score}<span class="text-[10px] text-Cmuted">/100</span></div>
    <div>
      <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-${V.col}/15 text-${V.col} border border-${V.col}/30">${V.tag}</span>
      <span class="text-[11px] text-Cwhite font-semibold ml-1">Release Readiness</span>
      ${sys}
    </div>
  </div>`;
}

// ── Panel A: Batch Runtime vs SLA Ceiling (Horizontal Bar) ───
function _renderExecSLABars(jobs) {
  const el = document.getElementById("exec-chart-sla-bars");
  if (!el || !jobs?.length) return;

  // Window mode: bars are the worst daily batch WINDOW per sub-app, judged
  // against each sub-app's OWN contracted ceiling (markers), so a breached
  // window crosses its ceiling instead of looking like huge headroom.
  const isWin  = !!jobs[0]?.is_window;
  const labels = jobs.map(j => { const s = j.job_name || j.sub_app || ""; return s.length > 25 ? s.slice(0, 22) + "…" : s; });

  // Relabel the panel badge + subtitle so the scope (window vs job) is honest.
  const badgeEl = document.getElementById("exec-slabars-badge");
  const subEl   = document.getElementById("exec-slabars-subtitle");
  if (badgeEl) {
    badgeEl.textContent = isWin ? "Window-level" : "Job-level";
    badgeEl.title = isWin
      ? "Scope: each sub-app's worst daily batch window (first-start → last-end) vs its contracted ceiling. This is the BINDING SLA view — it matches the Breach Calendar and decision gate."
      : "Scope: each job's peak runtime vs its own SLA ceiling. Green here ≠ the daily batch window passing — see the Breach Calendar for window-level results.";
  }
  if (subEl) {
    subEl.textContent = isWin
      ? "Each bar = one sub-app's worst daily batch window vs its ceiling (markers) · sorted by risk (worst at top)"
      : "Each bar = one job's peak runtime vs its own ceiling · sorted by SRI (worst at top)";
  }
  const vals   = jobs.map(j => _n(j.peak_hrs));
  const colors = jobs.map(j => j.status === "BREACH" ? "#f43f5e" : (j.status === "AT_RISK" ? "#f59e0b" : "#10d96e"));

  const traces = [
    {
      type: "bar", orientation: "h",
      y: labels, x: vals,
      marker: { color: colors, line: { width: 0 } },
      text: jobs.map(j => isWin ? `${j.breach_days || 0}d` : `SRI: ${_n(j.sri).toFixed(2)}`),
      textposition: "outside",
      textfont: { size: 9, color: "#6b7db3" },
      hovertemplate: isWin
        ? "%{y}<br>Worst window: %{x:.2f}h<br>Ceiling: %{customdata[0]:.1f}h<br>Buffer: %{customdata[1]:.0f}%<br>%{customdata[2]} day(s) breached<extra></extra>"
        : "%{y}<br>Peak: %{x:.2f}h<br>Buffer: %{customdata:.0f}%<extra></extra>",
      customdata: isWin
        ? jobs.map(j => [_n(j.sla_ceiling), (j.buffer_pct ?? 0), (j.breach_days || 0)])
        : jobs.map(j => j.buffer_pct),
      name: isWin ? "Worst Window" : "Peak Runtime",
    },
  ];

  // Per-sub-app ceiling markers (window mode) — each sub-app is judged against
  // its OWN contracted ceiling, so there is no single misleading global line.
  if (isWin) {
    traces.push({
      type: "scatter", mode: "markers",
      y: labels, x: jobs.map(j => _n(j.sla_ceiling)),
      marker: { symbol: "line-ns-open", size: 18, color: "#f43f5e", line: { width: 2.5 } },
      hovertemplate: "Ceiling: %{x:.1f}h<extra></extra>",
      name: "SLA ceiling",
    });
  }

  const sla = jobs.length ? _n(jobs[0].sla_ceiling) : 6;
  const layout = {
    ..._EXEC_LAYOUT_BASE,
    margin: { l: 140, r: 60, t: 10, b: 35 },
    xaxis: { ..._EXEC_LAYOUT_BASE.xaxis, title: { text: "Hours", font: { size: 10 } } },
    yaxis: { ..._EXEC_LAYOUT_BASE.yaxis, autorange: "reversed" },
    showlegend: false,
    bargap: 0.15,
  };
  // Single dashed SLA line only in per-job mode (one global ceiling). Window
  // mode uses the per-bar ceiling markers above because ceilings differ by
  // schedule type (e.g. 6h DAILY vs 9h WEEKLY).
  if (!isWin) {
    layout.shapes = [{
      type: "line", x0: sla, x1: sla, y0: -0.5, y1: labels.length - 0.5,
      line: { color: "#f43f5e", width: 2, dash: "dash" },
    }];
    layout.annotations = [{
      xref: "x", yref: "paper",
      x: sla, y: 1.02, xanchor: "center", yanchor: "bottom",
      text: `SLA: ${sla}h`, showarrow: false,
      font: { size: 9, color: "#f43f5e", family: "monospace" },
      bgcolor: "rgba(13,21,38,0.7)", borderpad: 2,
    }];
  }

  _plotlyPurge(el);
  Plotly.newPlot(el, traces, layout, _EXEC_CFG);
}

// ── Panel B: Server Resource Heatmap ─────────────────────────
function _renderExecHeatmap(servers) {
  const el = document.getElementById("exec-chart-heatmap");
  if (!el || !servers?.length) return;

  const hosts = servers.map(s => s.host.length > 20 ? s.host.slice(0, 17) + "…" : s.host);
  const metrics = ["CPU %", "Mem %", "Disk %"];
  const z = servers.map(s => [s.cpu, s.mem, s.disk]);

  // Annotation text: actual values in cells
  const annotations = [];
  for (let i = 0; i < hosts.length; i++) {
    for (let j = 0; j < 3; j++) {
      annotations.push({
        x: metrics[j], y: hosts[i],
        text: z[i][j].toFixed(1) + "%",
        showarrow: false,
        font: { size: 10, color: z[i][j] >= 75 ? "#fff" : "#c0c0c0" },
      });
    }
  }

  const traces = [{
    type: "heatmap",
    z: z, x: metrics, y: hosts,
    colorscale: [
      [0.0,  "#0d4429"], [0.50, "#1a6d1a"],
      [0.55, "#7a6d0a"], [0.75, "#b5700a"],
      [0.80, "#c4430a"], [1.0,  "#f43f5e"],
    ],
    zmin: 0, zmax: 100,
    showscale: true,
    colorbar: { len: 0.5, thickness: 12, tickfont: { size: 9, color: "#6b7db3" }, title: { text: "%", font: { size: 9 } } },
    hovertemplate: "%{y}<br>%{x}: %{z:.1f}%<extra></extra>",
  }];

  const layout = {
    ..._EXEC_LAYOUT_BASE,
    margin: { l: 130, r: 80, t: 10, b: 35 },
    annotations: annotations,
    yaxis: { ..._EXEC_LAYOUT_BASE.yaxis, autorange: "reversed" },
  };

  _plotlyPurge(el);
  Plotly.newPlot(el, traces, layout, _EXEC_CFG);
}

// ── Re-render Resource Health with latest deep dive data ──
function _refreshExecResourceHealth() {
  const cache = window._execCache;
  if (!cache?.server_heatmap?.length) return;
  _renderExecResourceHealth(cache.server_heatmap, cache.kpis);
}

// ── Resource Health Summary — Grafana/Prometheus-style panel ───────────────
// Shows fleet grade, per-metric arc gauges with role sub-breakdown (APP/DB/SRE),
// environment segmentation (PROD/TEST/DEV), donut fleet health chart, and a
// per-server risk table with inline bars. All data from session — no hardcoding.
function _renderExecResourceHealth(servers, kpis) {
  const el = document.getElementById("exec-resource-health");
  if (!el) return;

  if (!servers?.length) {
    el.innerHTML = `<div class="py-6 text-center">
      <div class="text-2xl opacity-30 mb-2">🖥️</div>
      <p class="text-[11px] text-Cmuted mb-2">Resource data not loaded — go to Resource Review<br>to connect Azure or upload server CSV.</p>
      <button onclick="setActiveView('resource')"
              class="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-[11px] font-semibold bg-Cpurple/20 border border-Cpurple/40 text-Cpurple hover:bg-Cpurple/30 transition-colors cursor-pointer">
        Go to Resource Review →
      </button>
    </div>`;
    return;
  }

  const T = RESOURCE_THRESHOLDS;
  const fleetGrade  = kpis?.fleet_grade || "—";
  const gradeColor  = { A: THEME.green, B: THEME.blue, C: THEME.amber, D: "#f97316", F: THEME.red }[fleetGrade] || THEME.muted;
  const total       = servers.length;

  // ── Observation window ────────────────────────────────────────────────────
  const resourceServers = window.appData?.resource?.servers || [];
  const isAzure   = resourceServers.some(s => s.source === "azure_monitor");
  const ddVms     = _deepDiveData?.vms || {};
  const hasDeepDive = Object.keys(ddVms).length > 0;
  const ddDaysObs = _deepDiveData?.baseline?.days_observed || window.appData?.deepDive?.baseline?.days_observed || 0;
  const ddHours   = _deepDiveHoursBack || window.appData?.deepDive?.hours_back || 0;
  const windowDays = ddDaysObs || (ddHours > 0 ? Math.round(ddHours / 24) : 0);
  const windowLabel = isAzure && hasDeepDive
    ? (windowDays >= 15 ? `✓ ${windowDays}d baseline` : windowDays > 0 ? `⚠ ${windowDays}d (15d ideal)` : "⚠ Azure snapshot")
    : isAzure ? "⊘ Azure snapshot — load time-series"
    : "⊘ Doc upload · point-in-time";
  const windowColor = (isAzure && hasDeepDive && windowDays >= 15) ? THEME.green
    : (isAzure && hasDeepDive && windowDays > 0) ? THEME.amber : THEME.muted;

  // ── Enrich: merge exec heatmap with deep-dive stats + resource record ─────
  const enriched = servers.map(s => {
    const host  = s.host || "";
    const base  = host.split(".")[0].toLowerCase();
    const ddKey = Object.keys(ddVms).find(k => k.toLowerCase() === host.toLowerCase() || k.toLowerCase().startsWith(base));
    const vmSt  = ddKey ? (ddVms[ddKey].stats || {}) : null;
    const res   = resourceServers.find(r => (r.host || r.label || "").toLowerCase().split(".")[0] === base) || {};

    if (vmSt) {
      const cpuSt  = vmSt["Percentage CPU"]                    || {};
      const memSt  = vmSt["Available Memory Percentage"]       || {};
      const diskSt = vmSt["OS Disk Bandwidth Consumed Percentage"] || {};
      return {
        host,
        cpu:  cpuSt.p95 ?? cpuSt.mean ?? s.cpu ?? 0,   cpuPeak: cpuSt.max  ?? s.cpu ?? 0,
        mem:  memSt.p95 != null ? memSt.p95 : (s.mem ?? 0),
        memPeak: memSt.max != null ? memSt.max : (s.mem ?? 0),
        disk: diskSt.p95 ?? diskSt.mean ?? s.disk ?? 0, diskPeak: diskSt.max ?? s.disk ?? 0,
        type: res.type || "APP", env: res.environment || "?", status: res.status || "Healthy",
        _src: "ts",
      };
    }
    return {
      host,
      cpu: s.cpu ?? 0, cpuPeak: s.cpu ?? 0,
      mem: s.mem ?? 0, memPeak: s.mem ?? 0,
      disk: s.disk ?? 0, diskPeak: s.disk ?? 0,
      type: res.type || "APP", env: res.environment || "?", status: res.status || "Healthy",
      _src: "snap",
    };
  });

  const tsCount = enriched.filter(e => e._src === "ts").length;
  const metricLbl = hasDeepDive ? "P95" : "Avg";

  // ── Fleet-level aggregates ────────────────────────────────────────────────
  const _avg  = arr => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
  const _peak = arr => arr.length ? Math.max(...arr) : 0;
  const avgCpu  = _avg(enriched.map(s => s.cpu)),   peakCpu  = _peak(enriched.map(s => s.cpuPeak));
  const avgMem  = _avg(enriched.map(s => s.mem)),   peakMem  = _peak(enriched.map(s => s.memPeak));
  const avgDisk = _avg(enriched.map(s => s.disk)),  peakDisk = _peak(enriched.map(s => s.diskPeak));

  // ── Role-adjusted memory alarm ────────────────────────────────────────────
  // DB servers in the expected SGA/PGA band (80–92% used / 8–20% available)
  // should not trigger a fleet-level CRITICAL colour. Compute an adjusted peak
  // that excludes DB servers in the expected band from the colour decision.
  // The actual avgMem number is still shown truthfully in the gauge.
  const _inDbBand = s => s.type?.toUpperCase() === "DB" && s.memPeak >= 80 && s.memPeak <= 92;
  const dbBandSvrs   = enriched.filter(_inDbBand);
  const nonBandSvrs  = enriched.filter(s => !_inDbBand(s));
  const adjPeakMem   = nonBandSvrs.length > 0 ? _peak(nonBandSvrs.map(s => s.memPeak)) : peakMem;
  const dbBandNote   = dbBandSvrs.length > 0
    ? `${dbBandSvrs.length} DB svr${dbBandSvrs.length > 1 ? "s" : ""} in SGA/PGA band — excluded from alarm`
    : "";

  // ── Role-grouped averages for sub-breakdown chips ─────────────────────────
  const byRole = {};
  enriched.forEach(s => {
    const r = s.type || "APP";
    if (!byRole[r]) byRole[r] = { cpu: [], mem: [], disk: [] };
    byRole[r].cpu.push(s.cpu);
    byRole[r].mem.push(s.mem);
    byRole[r].disk.push(s.disk);
  });
  const roleAvg = (role, metric) => {
    const arr = byRole[role]?.[metric];
    return arr?.length ? _avg(arr) : null;
  };

  // ── Environment groups ────────────────────────────────────────────────────
  const byEnv = {};
  enriched.forEach(s => {
    const e = s.env || "?";
    if (!byEnv[e]) byEnv[e] = { total: 0, crit: 0, warn: 0, ok: 0 };
    byEnv[e].total++;
    if      (s.status === "Critical") byEnv[e].crit++;
    else if (s.status === "Warning")  byEnv[e].warn++;
    else                              byEnv[e].ok++;
  });

  // ── Fleet risk bands ──────────────────────────────────────────────────────
  const bands = { crit: 0, warn: 0, ok: 0 };
  enriched.forEach(s => {
    const p = Math.max(s.cpuPeak, s.memPeak, s.diskPeak);
    if      (p >= T.cpu_warn) bands.crit++;
    else if (p >= T.cpu_ok)   bands.warn++;
    else                      bands.ok++;
  });

  // ── Colour helpers ────────────────────────────────────────────────────────
  const _col = (v, ok, warn) => v >= warn ? THEME.red : v >= ok ? THEME.amber : THEME.green;
  const _lbl = (v, ok, warn) => v >= warn ? "CRITICAL" : v >= ok ? "WARNING" : "OK";

  // ── Grafana-style radial ring gauge (SVG, no external lib) ───────────────
  // Full-circle progress arc; the value label is an HTML overlay so the
  // typography stays crisp and is controlled by the .rh-card__gauge-c CSS.
  const _ring = (pct, col, size = 64) => {
    const sw   = size <= 56 ? 6 : 7;
    const r    = (size - sw) / 2 - 1;
    const c    = size / 2;
    const circ = 2 * Math.PI * r;
    const p    = Math.min(Math.max(pct, 0), 100);
    const filled = (p / 100) * circ;
    const glow = p >= 75 ? `filter:drop-shadow(0 0 5px ${col}77)` : "";
    return `<svg viewBox="0 0 ${size} ${size}" style="transform:rotate(-90deg);${glow}">
      <circle cx="${c}" cy="${c}" r="${r.toFixed(1)}" fill="none" stroke="rgba(255,255,255,0.07)" stroke-width="${sw}"/>
      <circle cx="${c}" cy="${c}" r="${r.toFixed(1)}" fill="none" stroke="${col}" stroke-width="${sw}"
              stroke-linecap="round" stroke-dasharray="${filled.toFixed(1)} ${circ.toFixed(1)}"/>
    </svg>`;
  };

  // ── Horizontal severity bar ───────────────────────────────────────────────
  const _bar = (v, col, h = "h-2.5") =>
    `<div class="${h} rounded-full bg-white/[0.06] overflow-hidden">
       <div class="${h} rounded-full transition-all" style="width:${Math.min(Math.max(v, 0), 100).toFixed(1)}%;background:${col}"></div>
     </div>`;

  // ── Role sub-breakdown chips ──────────────────────────────────────────────
  const ROLE_INFO = { APP: { col: THEME.blue,   bg: "rgba(59,130,246,0.12)" },
                      DB:  { col: THEME.amber,  bg: "rgba(245,158,11,0.12)" },
                      SRE: { col: THEME.purple, bg: "rgba(168,85,247,0.12)" } };
  const _roleChips = (metric) =>
    ["APP", "DB", "SRE"].map(role => {
      const v = roleAvg(role, metric);
      if (v === null) return "";
      const ri = ROLE_INFO[role] || { col: THEME.muted, bg: "rgba(107,125,179,0.1)" };
      return `<span class="rh-chip" style="color:${ri.col};background:${ri.bg};border:1px solid ${ri.col}44">${role} ${v.toFixed(0)}%</span>`;
    }).join("");

  // ── Grafana-style metric card — stable 4-zone contract ───────────────────
  // Zones: head (icon+label+badge) · body (ring gauge + large readout) · bar
  //        (peak severity) · chips (role breakdown). Compact & content-driven.
  // alarmV: optional colour/label override (e.g. role-adjusted DB-band pressure)
  // note:   optional cyan sub-text below chips (e.g. "3 DB svrs in SGA/PGA band")
  const _metricCard = (label, icon, avgV, peakV, metric, okT, warnT, alarmV, note) => {
    const _alarmPeak = alarmV !== undefined ? alarmV : peakV;
    const col   = _col(_alarmPeak, okT, warnT);
    const lbl   = _lbl(_alarmPeak, okT, warnT);
    const sev   = lbl === "CRITICAL" ? "is-crit" : lbl === "WARNING" ? "is-warn" : "is-ok";
    const chips = _roleChips(metric);
    const peakW = Math.min(Math.max(peakV, 0), 100).toFixed(1);
    const cardStyle =
      `--rh-accent:${col};--rh-border:${col}33;` +
      `--rh-bg:linear-gradient(160deg,${col}10 0%,rgba(13,21,38,0.97) 70%);--rh-glow:${col}66`;
    const badgeGlow = lbl === "CRITICAL" ? `text-shadow:0 0 8px ${col}99` : "";
    return `
    <div class="rh-card ${sev}" style="${cardStyle}">
      <div class="rh-card__head">
        <div class="rh-card__head-l">
          <span class="rh-card__icon">${icon}</span>
          <span class="rh-card__label">${label}</span>
        </div>
        <span class="rh-card__badge" style="color:${col};background:${col}1f;border:1px solid ${col}55;${badgeGlow}">${lbl}</span>
      </div>
      <div class="rh-card__body">
        <div class="rh-card__gauge">
          ${_ring(avgV, col, 64)}
          <div class="rh-card__gauge-c">
            <b style="color:${col}">${Math.round(avgV)}%</b>
            <span>${metricLbl}</span>
          </div>
        </div>
        <div class="rh-card__readout">
          <div class="rh-card__value" style="color:${col}">${avgV.toFixed(1)}<u>%</u></div>
          <div class="rh-card__sub">peak <b style="color:${col}">${peakV.toFixed(0)}%</b> · ${total} svr</div>
        </div>
      </div>
      <div class="rh-card__bar"><i style="width:${peakW}%;background:${col}"></i></div>
      ${chips ? `<div class="rh-card__chips">${chips}</div>` : ""}
      ${note ? `<div class="rh-card__note"><span>ℹ</span><span>${note}</span></div>` : ""}
    </div>`;
  };

  // ── Environment breakdown rows ────────────────────────────────────────────
  const ENV_COL = { PROD: THEME.purple, TEST: THEME.blue, DEV: THEME.cyan };
  const envRows = Object.entries(byEnv)
    .sort(([a], [b]) => (a === "PROD" ? -1 : b === "PROD" ? 1 : a.localeCompare(b)))
    .map(([env, cnt]) => {
      const ec   = ENV_COL[env] || THEME.muted;
      const hPct = cnt.total ? (cnt.ok / cnt.total * 100) : 0;
      const tag  = cnt.crit > 0
        ? `<span class="text-[11px] font-bold" style="color:${THEME.red}">${cnt.crit} CRIT</span>`
        : cnt.warn > 0
        ? `<span class="text-[11px] font-bold" style="color:${THEME.amber}">${cnt.warn} WARN</span>`
        : `<span class="text-[11px] font-bold" style="color:${THEME.green}">All OK</span>`;
      return `<div class="flex items-center gap-3 py-2 border-b border-white/[0.05] last:border-0">
        <span class="flex-shrink-0 w-2 h-2 rounded-full" style="background:${ec};box-shadow:0 0 4px ${ec}99"></span>
        <span class="text-[12px] font-bold w-11 flex-shrink-0" style="color:${ec}">${env}</span>
        <div class="flex-1">${_bar(hPct, THEME.green, "h-3")}</div>
        <span class="text-[11px] text-Cmuted flex-shrink-0">${cnt.total}svr</span>
        <div class="flex-shrink-0 w-16 text-right">${tag}</div>
      </div>`;
    }).join("");

  // ── Worst servers ranked table (up to 5) ─────────────────────────────────
  const worst5 = enriched
    .map(s => ({ ...s, worst: Math.max(s.cpuPeak, s.memPeak, s.diskPeak) }))
    .sort((a, b) => b.worst - a.worst)
    .slice(0, 5);

  const serverRows = worst5.map(s => {
    const dom  = s.memPeak >= s.cpuPeak && s.memPeak >= s.diskPeak ? ["MEM", s.memPeak]
               : s.cpuPeak >= s.diskPeak                           ? ["CPU", s.cpuPeak]
               :                                                     ["DISK", s.diskPeak];
    const [mKey, mVal] = dom;
    const col  = _col(mVal, T.cpu_ok, T.cpu_warn);
    const ri   = ROLE_INFO[s.type] || { col: THEME.muted, bg: "rgba(107,125,179,0.1)" };
    const name = s.host.length > 26 ? s.host.slice(0, 23) + "…" : s.host;
    return `<div class="flex items-center gap-3 py-2 border-b border-white/[0.05] last:border-0">
      <span class="flex-shrink-0 w-2 h-2 rounded-full"
            style="background:${col};box-shadow:0 0 5px ${col}99"></span>
      <span class="flex-1 text-[12px] font-medium text-Cwhite truncate" title="${_esc(s.host)}">${_esc(name)}</span>
      <span class="flex-shrink-0 text-[9px] px-2 py-0.5 rounded font-bold"
            style="color:${ri.col};background:${ri.bg};border:1px solid ${ri.col}44">${s.type || "APP"}</span>
      <span class="flex-shrink-0 text-[10px] font-bold text-Cmuted w-9 text-right">${mKey}</span>
      <div class="flex-shrink-0 w-24">${_bar(mVal, col, "h-2.5")}</div>
      <span class="flex-shrink-0 text-[13px] font-extrabold font-mono tabular-nums w-10 text-right"
            style="color:${col}">${mVal.toFixed(0)}%</span>
    </div>`;
  }).join("");

  // ── Canvas donut ID (unique per render to avoid stale canvas) ─────────────
  const cid = `rh-donut-${Date.now()}`;

  // ── Final HTML ────────────────────────────────────────────────────────────
  el.innerHTML = `
  <!-- Header: fleet grade badge + server count + observation window -->
  <div class="flex items-center justify-between pb-4 mb-1 border-b border-white/[0.06]">
    <div class="flex items-center gap-3">
      <div class="w-12 h-12 rounded-xl flex items-center justify-center font-extrabold text-2xl flex-shrink-0"
           style="background:${gradeColor}1c;border:2px solid ${gradeColor}55;color:${gradeColor};
                  box-shadow:0 0 14px ${gradeColor}33">${fleetGrade}</div>
      <div>
        <div class="text-[15px] font-bold text-Cwhite leading-tight">
          ${total} server${total !== 1 ? "s" : ""}
        </div>
        <div class="text-[11px] text-Cmuted mt-0.5">
          ${tsCount > 0 ? `${tsCount} with time-series · ` : ""}${isAzure ? "Azure Monitor" : "Document upload"}
        </div>
      </div>
    </div>
    <div>
      <div class="inline-flex items-center gap-1.5 text-[11px] font-semibold px-3 py-1.5 rounded-full border"
           style="color:${windowColor};border-color:${windowColor}55;background:${windowColor}12">${windowLabel}</div>
    </div>
  </div>

  <!-- Metric cards: CPU / Memory / Disk I/O (Grafana radial gauges) -->
  <div class="rh-metric-grid mb-4">
    ${_metricCard("CPU",      "⚡", avgCpu,  peakCpu,  "cpu",  T.cpu_ok,  T.cpu_warn)}
    ${_metricCard("MEMORY",   "🧠", avgMem,  peakMem,  "mem",  T.mem_ok,  T.mem_warn, adjPeakMem, dbBandNote)}
    ${_metricCard("DISK I/O", "💿", avgDisk, peakDisk, "disk", T.disk_ok, T.disk_warn)}
  </div>

  <!-- Fleet health donut + Environment breakdown -->
  <div class="grid grid-cols-5 gap-3 mb-4">
    <!-- Fleet health donut -->
    <div class="col-span-2 rounded-xl border border-white/[0.07] bg-white/[0.018] p-4
                flex flex-col items-center justify-center gap-3">
      <div class="text-[10px] uppercase tracking-widest font-bold text-Cmuted">Fleet Health</div>
      <div class="relative w-[100px] h-[100px]">
        <canvas id="${cid}" width="100" height="100" style="display:block"></canvas>
        <div class="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
          <div class="text-[22px] font-extrabold leading-none text-Cwhite">${total}</div>
          <div class="text-[10px] text-Cmuted mt-0.5">servers</div>
        </div>
      </div>
      <div class="flex items-center gap-3 text-[10px]">
        <span class="flex items-center gap-1">
          <span class="w-2 h-2 rounded-full" style="background:${THEME.red}"></span>
          <span class="text-Cmuted">${bands.crit} crit</span>
        </span>
        <span class="flex items-center gap-1">
          <span class="w-2 h-2 rounded-full" style="background:${THEME.amber}"></span>
          <span class="text-Cmuted">${bands.warn} warn</span>
        </span>
        <span class="flex items-center gap-1">
          <span class="w-2 h-2 rounded-full" style="background:${THEME.green}"></span>
          <span class="text-Cmuted">${bands.ok} ok</span>
        </span>
      </div>
    </div>
    <!-- Environment breakdown -->
    <div class="col-span-3 rounded-xl border border-white/[0.07] bg-white/[0.018] p-4">
      <div class="text-[10px] uppercase tracking-widest font-bold text-Cmuted mb-3">By Environment</div>
      ${envRows || '<div class="text-[11px] text-Cmuted py-2">No environment data</div>'}
    </div>
  </div>

  <!-- Highest-risk servers ranked table -->
  <div class="rounded-xl border border-white/[0.07] bg-white/[0.018] p-4">
    <div class="flex items-center justify-between mb-3">
      <div class="text-[10px] uppercase tracking-widest font-bold text-Cmuted">Highest-Risk Servers</div>
      <div class="text-[10px] text-Cmuted">worst metric · peak over window</div>
    </div>
    ${serverRows || '<div class="text-[11px] text-Cmuted py-2">No server data</div>'}
  </div>`;

  // ── Draw fleet health donut via Canvas 2D (no external lib) ──────────────
  requestAnimationFrame(() => {
    const canvas = document.getElementById(cid);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const cx = 50, cy = 50, r = 34, sw = 11;
    // Background ring
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255,255,255,0.05)"; ctx.lineWidth = sw; ctx.stroke();
    // Coloured slices
    if (total > 0) {
      const slices = [
        { v: bands.crit, c: THEME.red },
        { v: bands.warn, c: THEME.amber },
        { v: bands.ok,   c: THEME.green },
      ].filter(s => s.v > 0);
      let start = -Math.PI / 2;
      slices.forEach(sl => {
        const sweep = (sl.v / total) * Math.PI * 2;
        ctx.beginPath(); ctx.arc(cx, cy, r, start, start + sweep);
        ctx.strokeStyle = sl.c; ctx.lineWidth = sw - 2; ctx.lineCap = "round"; ctx.stroke();
        start += sweep;
      });
    }
  });
}

// ── NEW: Top 3 At-Risk Jobs (replaces sub-app table in Row 2) ──
function _renderExecTopRiskJobs(jobs) {
  const el = document.getElementById("exec-top-risk-jobs");
  if (!el) return;
  const titleEl = document.getElementById("exec-risk-title");
  const subEl   = document.getElementById("exec-risk-subtitle");
  if (!jobs?.length) {
    el.innerHTML = '<p class="text-Cmuted text-[11px] py-4 text-center">No SLA window data loaded</p>';
    return;
  }

  // Window mode = the BINDING SLA view (per-sub-app daily batch window vs the
  // contracted ceiling). Falls back to per-job peak only when no window data.
  const isWin = !!jobs[0]?.is_window;
  if (titleEl) titleEl.textContent = isWin ? "Top At-Risk Sub-Apps" : "Top At-Risk Jobs";
  if (subEl)   subEl.textContent   = isWin
    ? "Worst daily batch window vs contracted SLA ceiling"
    : "Highest per-job runtime vs its SLA ceiling";

  const statusCol = (s, sri) =>
    s === "BREACH"  ? "#f43f5e" :
    s === "AT_RISK" ? "#f59e0b" :
    s === "OK"      ? "#10d96e" :
    (sri > 1 ? "#f43f5e" : sri > 0.85 ? "#f59e0b" : "#10d96e");

  // Sort by SRI/severity descending (worst first), dedupe by label, take top 3
  const _seen = new Set();
  const top3 = jobs.slice().sort((a, b) => (b.sri||0) - (a.sri||0))
    .filter(j => { const k = j.job_name || j.sub_app || ''; if (_seen.has(k)) return false; _seen.add(k); return true; })
    .slice(0, 3);

  el.innerHTML = top3.map((j, i) => {
    const sri = j.sri || 0;
    const col = statusCol(j.status, sri);
    const ceil = _n(j.sla_ceiling);
    const val  = _n(j.peak_hrs);
    const pct  = ceil > 0 ? ((val / ceil) * 100).toFixed(0) : "—";
    const buffer = j.buffer_pct != null ? _n(j.buffer_pct).toFixed(1) : "—";
    const label = (j.job_name || j.sub_app || "Unknown");
    const name = label.length > 30 ? label.slice(0,27) + "…" : label;
    const valLbl = isWin ? "Worst Window" : "Peak";
    // Window context: how many days this sub-app's window breached + its window
    // compliance — so a red card always explains WHY (never just a bare status).
    const ctx = isWin
      ? `${j.breach_days || 0} day${(j.breach_days===1)?"":"s"} breached · ${j.compliance_pct != null ? _n(j.compliance_pct).toFixed(0) : "—"}% windows within SLA`
      : `SRI ${sri.toFixed(2)} · ${pct}% of ceiling`;
    return `
      <div class="rounded-lg border border-Cborder/40 bg-Ccard/40 p-3" style="border-left:3px solid ${col};">
        <div class="flex items-center justify-between gap-2 mb-1">
          <span class="text-[12px] font-bold text-Cwhite truncate" title="${_esc(label)}">#${i+1} ${_esc(name)}</span>
          <span class="text-[10px] font-bold px-1.5 py-0.5 rounded-full" style="color:${col};background:${col}22;">${_esc(j.status || (sri>1?'BREACH':'OK'))}</span>
        </div>
        <div class="grid grid-cols-3 gap-1 text-center">
          <div>
            <div class="text-[9px] text-Cmuted">${valLbl}</div>
            <div class="text-[12px] font-bold text-Cwhite">${val.toFixed(1)}h</div>
          </div>
          <div>
            <div class="text-[9px] text-Cmuted">Ceiling</div>
            <div class="text-[12px] font-bold text-Cwhite">${ceil.toFixed(1)}h</div>
          </div>
          <div>
            <div class="text-[9px] text-Cmuted">Buffer</div>
            <div class="text-[12px] font-bold" style="color:${col}">${buffer}%</div>
          </div>
        </div>
        <div class="mt-1.5 h-1.5 rounded bg-Cbg/80 overflow-hidden">
          <div class="h-full rounded" style="width:${Math.min(Number(pct)||0, 100)}%;background:${col}"></div>
        </div>
        <div class="text-[9px] text-Cmuted mt-0.5 text-right">${_esc(ctx)}</div>
      </div>`;
  }).join("");
}

// ── Panel C: Sub-App Risk Distribution (Bar) ─────────────────
function _renderExecSubAppBars(subs) {
  const el = document.getElementById("exec-chart-subapp-bars");
  if (!el || !subs?.length) return;

  // Treemap — tile size by job count, color by SRI severity
  const colorFor = (sri) => sri > 1.0 ? "#f43f5e" : sri > 0.85 ? "#f59e0b" : "#10d96e";

  const labels = subs.map(s => s.sub_app);
  const parents = subs.map(() => "");
  const values = subs.map(s => Math.max(1, s.job_count || 1));
  const colors = subs.map(s => colorFor(s.sri));
  const text = subs.map(s =>
    `<b>${s.sub_app}</b><br>SRI ${_n(s.sri).toFixed(2)} · ${s.job_count} jobs`);

  const traces = [{
    type: "treemap",
    labels, parents, values,
    text, textinfo: "text",
    textfont: { size: 11, color: "#ffffff", family: "Sora" },
    marker: {
      colors,
      line: { color: "#0b1530", width: 2 },
      pad: { t: 2, l: 2, r: 2, b: 2 },
    },
    hovertemplate: "<b>%{label}</b><br>SRI: %{customdata:.3f}<br>Jobs: %{value}<extra></extra>",
    customdata: subs.map(s => s.sri),
    tiling: { packing: "squarify", pad: 2 },
  }];

  const layout = {
    ..._EXEC_LAYOUT_BASE,
    margin: { l: 4, r: 4, t: 4, b: 4 },
    showlegend: false,
  };

  _plotlyPurge(el);
  Plotly.newPlot(el, traces, layout, _EXEC_CFG);
}

// ── Panel D: 3-Way Risk Matrix (Bubble) ──────────────────────
function _renderExecBubble(subs) {
  const el = document.getElementById("exec-chart-bubble");
  if (!el || !subs?.length) return;

  const rfcsCols = { red: "#f43f5e", amber: "#f59e0b", green: "#10d96e" };

  const traces = [{
    type: "scatter", mode: "markers",
    x: subs.map(s => s.resource_pressure),
    y: subs.map(s => s.sri),
    text: subs.map(s => s.sub_app),
    marker: {
      size: subs.map(s => Math.max(12, s.crs * 60)),
      color: subs.map(s => rfcsCols[s.rfcs_band] || "#3b82f6"),
      opacity: 0.85,
      line: { width: 1.5, color: "rgba(255,255,255,0.3)" },
    },
    hovertemplate: "<b>%{text}</b><br>Resource: %{x:.1f}%<br>SRI: %{y:.3f}<br>CRS: %{customdata:.3f}<extra></extra>",
    customdata: subs.map(s => s.crs),
  }];

  const layout = {
    ..._EXEC_LAYOUT_BASE,
    xaxis: {
      ..._EXEC_LAYOUT_BASE.xaxis,
      title: { text: "Resource Pressure (%)", font: { size: 10 } },
      range: [0, 110],
    },
    yaxis: {
      ..._EXEC_LAYOUT_BASE.yaxis,
      title: { text: "SLA Risk Index (SRI)", font: { size: 10 } },
    },
    shapes: [
      { type: "line", x0: 75, x1: 75, y0: 0, y1: 2, line: { color: "rgba(244,63,94,0.3)", width: 1, dash: "dot" } },
      { type: "line", y0: 0.85, y1: 0.85, x0: 0, x1: 110, line: { color: "rgba(244,63,94,0.3)", width: 1, dash: "dot" } },
    ],
    annotations: [
      { x: 92, y: 1.5, text: "CRITICAL", showarrow: false, font: { size: 9, color: "#f43f5e" } },
      { x: 20, y: 1.5, text: "SLA RISK", showarrow: false, font: { size: 9, color: "#f59e0b" } },
      { x: 92, y: 0.3, text: "RESOURCE RISK", showarrow: false, font: { size: 8, color: "#f59e0b" } },
      { x: 20, y: 0.3, text: "SAFE", showarrow: false, font: { size: 9, color: "#10d96e" } },
    ],
    showlegend: false,
  };

  _plotlyPurge(el);
  Plotly.newPlot(el, traces, layout, _EXEC_CFG);
}

// ── Panel E: Temporal Correlation (Dual-Axis) ────────────────
function _renderExecTemporal(temporal, kpis) {
  const el = document.getElementById("exec-chart-temporal");
  if (!el || !temporal?.length) return;

  const hours  = temporal.map(t => t.hour);
  const jobs   = temporal.map(t => t.jobs);
  const fails  = temporal.map(t => t.failures);
  const barCol = temporal.map(t => t.fail_rate > 20 ? "#f43f5e" : (t.fail_rate > 5 ? "#f59e0b" : "#3b82f6"));

  // Create CPU line (constant at avg_cpu to show overlap)
  const cpuLine = temporal.map(() => kpis?.avg_cpu ?? 0);

  const traces = [
    {
      type: "bar", name: "Jobs / hour",
      x: hours, y: jobs,
      marker: { color: barCol, opacity: 0.8 },
      yaxis: "y",
      hovertemplate: "Hour %{x}<br>Jobs: %{y}<br>Failures: %{customdata}<extra></extra>",
      customdata: fails,
    },
    {
      type: "scatter", mode: "lines+markers", name: "Avg CPU %",
      x: hours, y: cpuLine,
      line: { color: "#f43f5e", width: 2, dash: "dash" },
      marker: { size: 0 },
      yaxis: "y2",
      hovertemplate: "CPU: %{y:.1f}%<extra></extra>",
    },
  ];

  const layout = {
    ..._EXEC_LAYOUT_BASE,
    xaxis: { ..._EXEC_LAYOUT_BASE.xaxis, title: { text: "Hour of Day", font: { size: 10 } }, dtick: 1 },
    yaxis: { ..._EXEC_LAYOUT_BASE.yaxis, title: { text: "Job Count", font: { size: 10 } }, side: "left" },
    yaxis2: {
      title: { text: "CPU %", font: { size: 10, color: "#f43f5e" } },
      overlaying: "y", side: "right", range: [0, 100],
      gridcolor: "rgba(0,0,0,0)",
      tickfont: { color: "#f43f5e" },
    },
    legend: { font: { size: 9 }, x: 0, y: 1.12, orientation: "h" },
    // Batch window shading (20:00-04:00)
    shapes: [{
      type: "rect", x0: 20, x1: 23, y0: 0, y1: 1, yref: "paper",
      fillcolor: "rgba(168,85,247,0.08)", line: { width: 0 },
    }, {
      type: "rect", x0: 0, x1: 4, y0: 0, y1: 1, yref: "paper",
      fillcolor: "rgba(168,85,247,0.08)", line: { width: 0 },
    }],
    annotations: [
      { x: 22, y: 1.03, yref: "paper", text: "Batch Window", showarrow: false, font: { size: 8, color: "#a855f7" } },
    ],
    bargap: 0.05,
  };

  _plotlyPurge(el);
  Plotly.newPlot(el, traces, layout, _EXEC_CFG);
}

// ── Panel F: OSHS Waterfall ──────────────────────────────────
function _renderExecWaterfall(wf, oshs) {
  const el = document.getElementById("exec-chart-waterfall");
  if (!el || !wf) return;
  if (typeof Plotly === "undefined") return;

  // Multi-arc radial gauge: each pillar is its own concentric ring.
  // Ring fill = contribution / target_pts, where target_pts is the pillar's
  // weight × 100 (read from the backend so the rings track the real scoring
  // math). When the resource pillar is dropped (no measured utilization), its
  // weight is 0 and the batch/SLA targets widen to their re-normalised values
  // — so the maxes MUST come from wf.*_target, never hardcoded 40/35/25.
  const _resAvail = wf.resource_available !== false;
  const pillars = [
    { name: "Batch",    val: _n(wf.batch_contribution),    max: _n(wf.batch_target)    || 40, color: "#22d3ee" },
    { name: "Resource", val: _n(wf.resource_contribution), max: _n(wf.resource_target) || 35, color: "#a78bfa", excluded: !_resAvail },
    { name: "SLA",      val: _n(wf.sla_contribution),      max: _n(wf.sla_target)      || 25, color: "#fbbf24" },
  ].filter(p => !p.excluded);
  const total = _n(wf.total);
  const totalCol = total >= 75 ? "#10d96e" : total >= 60 ? "#f59e0b" : "#f43f5e";
  const grade = oshs?.grade || "?";

  // Reconcile the headline: pillar contributions sum to the *base* OSHS, but the
  // final score is further reduced by the findings penalty (and any release floor).
  // Surface that deduction explicitly so the three rings always add up to the
  // number in the centre — no silent ~17-point gap the viewer can't explain.
  const compSum = pillars.reduce((s, p) => s + _n(p.val), 0);
  const adjust  = Math.round((compSum - total) * 10) / 10;   // >0 → score was reduced
  const _floorTxt = oshs?.floor_applied ? ` · release floor: ${oshs.floor_applied}` : "";
  const _adjTitle = `Base pillar score ${compSum.toFixed(1)} − findings/penalty ${Math.abs(adjust).toFixed(1)} = ${total.toFixed(1)}${_floorTxt}`;

  // Build SVG: concentric arcs, semicircle from -180° → 0°
  const W = 320, H = 240, CX = 160, CY = 200;
  const radii = [110, 85, 60];
  const stroke = 14;

  // Path generator for an arc from angle a0 → a1 (degrees, 180 = left, 0 = right)
  const arcPath = (cx, cy, r, a0, a1) => {
    const rad = a => (a * Math.PI) / 180;
    const x0 = cx + r * Math.cos(rad(a0)), y0 = cy + r * Math.sin(rad(a0));
    const x1 = cx + r * Math.cos(rad(a1)), y1 = cy + r * Math.sin(rad(a1));
    const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
    const sweep = a1 > a0 ? 1 : 0;
    return `M ${x0.toFixed(1)},${y0.toFixed(1)} A ${r},${r} 0 ${large} ${sweep} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  };

  let arcs = "";
  pillars.forEach((p, i) => {
    const r = radii[i];
    arcs += `<path d="${arcPath(CX, CY, r, 180, 360)}" fill="none" stroke="#1e2a52" stroke-width="${stroke}" stroke-linecap="round"/>`;
    const pct = Math.max(0, Math.min(1, p.val / p.max));
    if (pct > 0.001) {
      const a1 = 180 + 180 * pct;
      arcs += `<path d="${arcPath(CX, CY, r, 180, a1)}" fill="none" stroke="${p.color}" stroke-width="${stroke}" stroke-linecap="round" style="filter:drop-shadow(0 0 4px ${p.color}aa);"/>`;
    }
  });

  el.innerHTML = `
    <div class="relative h-full w-full flex flex-col items-center justify-center">
      <svg viewBox="0 0 ${W} ${H}" class="w-full max-w-[340px]" preserveAspectRatio="xMidYMid meet">
        ${arcs}
        <text x="${CX}" y="${CY - 38}" text-anchor="middle" fill="${totalCol}" font-size="44" font-weight="900" font-family="Sora">${total.toFixed(1)}</text>
        <text x="${CX}" y="${CY - 18}" text-anchor="middle" fill="#6b7db3" font-size="9" font-family="Sora" letter-spacing="1.5">/ 100 · GRADE ${grade}</text>
      </svg>
      <div class="flex flex-wrap justify-center gap-x-4 gap-y-1 text-[10px] mt-2 px-2">
        ${pillars.map(p => `
          <span class="flex items-center gap-1.5">
            <span class="inline-block w-2 h-2 rounded-full" style="background:${p.color};box-shadow:0 0 6px ${p.color};"></span>
            <span class="text-Cmuted">${p.name}</span>
            <span class="font-bold" style="color:${p.color};">${_n(p.val).toFixed(1)}<span class="text-Cmuted/60 font-normal">/${_n(p.max).toFixed(1)}</span></span>
            <span class="text-[9px] text-Cmuted">(${p.max > 0 ? Math.round((p.val / p.max) * 100) : 0}%)</span>
          </span>`).join("")}
        ${!_resAvail ? `
          <span class="flex items-center gap-1.5" title="No measured resource utilization (image-only or no upload) — the resource pillar is excluded and its weight re-normalised over Batch + SLA.">
            <span class="inline-block w-2 h-2 rounded-full" style="background:#475569;"></span>
            <span class="text-Cmuted">Resource excluded (no data)</span>
          </span>` : ""}
        ${Math.abs(adjust) > 0.1 ? `
          <span class="flex items-center gap-1.5" title="${_adjTitle}">
            <span class="inline-block w-2 h-2 rounded-full" style="background:#f43f5e;box-shadow:0 0 6px #f43f5e;"></span>
            <span class="text-Cmuted">Findings adj</span>
            <span class="font-bold" style="color:#f43f5e;">${adjust > 0 ? "−" : "+"}${Math.abs(adjust).toFixed(1)}</span>
          </span>` : ""}
      </div>
    </div>
  `;
}

// ── Narrative Panel — 3-section color coded ──────────────────
function _renderExecNarrative(findings) {
  const el = document.getElementById("exec-narrative");
  if (!el) return;

  // ── Case 1: backend deterministic narrative string → render as prose ──
  // The /api/executive-dashboard "narrative" field is a sentence-level string
  // (Scope · Compliance · Root cause · Impact). Render it as readable prose
  // with per-sentence severity accent, NOT dumped into a single green bucket.
  if (typeof findings === "string") {
    const txt = findings.trim();
    if (!txt) {
      el.innerHTML = '<p class="text-Cmuted text-[12px] py-4 text-center">No narrative data — upload batch + resource files to generate insights.</p>';
      return;
    }
    // Split into sentences/lines, keeping bullet evidence lines intact.
    const parts = txt
      .split(/\n+|(?<=[.!])\s+(?=[A-Z0-9])/)
      .map(s => s.trim())
      .filter(Boolean);
    const sevOf = (s) => {
      const l = s.toLowerCase();
      if (/breach|blocked|critical|exceed|fail|overrun|violat|miss/.test(l)) return { c: "#f43f5e", b: "rgba(244,63,94,0.5)" };
      if (/risk|watch|near|approach|caution|degrad|elevated|warning/.test(l)) return { c: "#f59e0b", b: "rgba(245,158,11,0.5)" };
      if (/within|compliant|approved|pass|healthy|ok\b|good|stable/.test(l)) return { c: "#10d96e", b: "rgba(16,217,110,0.5)" };
      return { c: "#6b7db3", b: "rgba(107,125,179,0.35)" };
    };
    el.innerHTML = `<div class="space-y-1.5">
      ${parts.map(p => {
        const sv = sevOf(p);
        return `<div class="flex gap-2 items-start text-[12px] leading-relaxed text-Cwhite/90 pl-2"
                     style="border-left:2px solid ${sv.b}">
          <span class="mt-1 w-1.5 h-1.5 rounded-full flex-shrink-0" style="background:${sv.c}"></span>
          <span>${_esc(p)}</span>
        </div>`;
      }).join("")}
    </div>`;
    return;
  }

  // ── Case 2: findings array → 3-bucket BLOCKERS / WATCH / PASSING ──
  if (!findings) findings = [];
  else if (!Array.isArray(findings)) {
    findings = (typeof findings === "object") ? [findings] : [];
  }
  if (!findings.length) {
    el.innerHTML = '<p class="text-Cmuted text-[12px] py-4 text-center">No narrative data — upload batch + resource files to generate insights.</p>';
    return;
  }

  // Classify findings into 3 buckets
  const blockers = [];
  const watches  = [];
  const passing  = [];

  findings.forEach(f => {
    const level = (f.level || "info").toLowerCase();
    const key = (f.key || "").toLowerCase();
    if (level === "critical" || key === "impact") {
      blockers.push(f);
    } else if (level === "warning" || key === "risk" || key === "cause") {
      watches.push(f);
    } else {
      passing.push(f);
    }
  });

  const sections = [
    { label: "BLOCKERS",  items: blockers, color: "#f43f5e", border: "border-red-500/30",    bg: "bg-red-900/20",    icon: "🔴", empty: "No blocking issues detected" },
    { label: "WATCH",     items: watches,  color: "#f59e0b", border: "border-amber-500/30",  bg: "bg-amber-900/20",  icon: "🟠", empty: "No watch items" },
    { label: "PASSING",   items: passing,  color: "#10d96e", border: "border-green-500/30",  bg: "bg-green-900/20",  icon: "🟢", empty: "No explicit passing items" },
  ];

  el.innerHTML = `<div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
    ${sections.map(s => `
      <div class="rounded-lg border ${s.border} ${s.bg} p-3">
        <div class="flex items-center gap-1.5 mb-2">
          <span>${s.icon}</span>
          <span class="text-[10px] font-bold uppercase tracking-widest" style="color:${s.color}">${s.label}</span>
          <span class="text-[9px] text-Cmuted">(${s.items.length})</span>
        </div>
        ${s.items.length ? s.items.map(f => `
          <div class="mb-2 last:mb-0">
            <div class="text-[11px] text-Cwhite leading-relaxed">${_esc(f.text || '—')}</div>
          </div>
        `).join('') : `<div class="text-[10px] text-Cmuted italic">${s.empty}</div>`}
      </div>
    `).join('')}
  </div>`;
}

// ── Scope Reconciliation Banner ──────────────────────────────
// Resolves the single biggest source of confusion on this page:
// "job-level SLA = 100%" vs "window-level SLA = X%" look contradictory
// but measure two different things. This banner states the relationship
// explicitly so a PE reviewer never has to read backend code to know it.
function _renderExecScopeReconcile(data) {
  const el = document.getElementById("exec-scope-reconcile");
  if (!el) return;

  const kpis = data?.kpis || {};
  const cal  = data?.breach_calendar || {};

  // Job-level: every job vs its own SLA ceiling (peak runtime).
  const jobRate = _n(kpis.batch_rate);
  // Window-level: each day's total elapsed batch window vs the daily ceiling.
  const winRate = _n(kpis.window_compliance ?? kpis.batch_rate);

  // Authoritative single source for breach-day counts (same numbers the
  // Breach Calendar bars are drawn from — guarantees the two never disagree).
  const totalDays  = cal.total_days  ?? kpis.window_total_days ?? null;
  let   breachDays = cal.breach_count ?? kpis.window_breach_days ?? null;
  // Hard guard: a breach-day count can never exceed total days. If the data
  // ever violates this, clamp and flag rather than render an impossible ratio.
  let clampNote = "";
  if (totalDays != null && breachDays != null && breachDays > totalDays) {
    clampNote = " (count reconciled)";
    breachDays = totalDays;
  }

  // Only render the banner when the two scopes actually diverge — when they
  // agree there's nothing to reconcile and the banner would be noise.
  const diverges = Math.abs(jobRate - winRate) >= 1.0 || (breachDays != null && breachDays > 0);
  if (!diverges) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");

  const jobCol = jobRate >= 95 ? "#10d96e" : jobRate >= 80 ? "#f59e0b" : "#f43f5e";
  const winCol = winRate >= 95 ? "#10d96e" : winRate >= 80 ? "#f59e0b" : "#f43f5e";
  const dayTxt = (totalDays != null && breachDays != null)
    ? `${breachDays}/${totalDays} day${totalDays === 1 ? "" : "s"} breached${clampNote}`
    : "—";

  el.innerHTML = `
    <div class="flex items-center gap-1.5 mb-2">
      <span class="text-Cpurple text-sm">⇄</span>
      <span class="text-[10px] font-bold uppercase tracking-widest text-Cwhite/80">Two SLA scopes — both true, measuring different things</span>
    </div>
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
      <div class="rounded-lg border border-Cborder/60 bg-Ccard/40 p-3">
        <div class="flex items-baseline justify-between">
          <span class="text-[10px] uppercase tracking-wider text-Cmuted font-bold">Job-level SLA</span>
          <span class="text-lg font-bold font-mono" style="color:${jobCol}">${_fmtPctCompact(jobRate)}</span>
        </div>
        <p class="text-[11px] text-Cwhite/70 leading-snug mt-1">
          Each job's <strong>peak runtime</strong> vs its own SLA ceiling. Drives the
          <strong>Batch&nbsp;Runtime&nbsp;vs&nbsp;SLA&nbsp;Ceiling</strong> bars above.
        </p>
      </div>
      <div class="rounded-lg border border-Cborder/60 bg-Ccard/40 p-3">
        <div class="flex items-baseline justify-between">
          <span class="text-[10px] uppercase tracking-wider text-Cmuted font-bold">Window-level SLA</span>
          <span class="text-lg font-bold font-mono" style="color:${winCol}">${_fmtPctCompact(winRate)}</span>
        </div>
        <p class="text-[11px] text-Cwhite/70 leading-snug mt-1">
          Each day's <strong>total elapsed batch window</strong> vs the daily ceiling
          (<span class="font-mono" style="color:${winCol}">${dayTxt}</span>). Drives the
          <strong>Breach&nbsp;Calendar</strong> below.
        </p>
      </div>
    </div>
    <p class="text-[10px] text-Cmuted leading-snug mt-2">
      A day can breach the <em>window</em> (batch finished late overall) even when <em>every individual job</em> stayed inside its own SLA —
      caused by late starts, queue delays, or too many jobs running back-to-back without overlap. That is why these two numbers differ and both are correct.
    </p>`;
}

// ── Hot Spots — crux strip above the narrative ───────────────
// Pulls the worst job, worst server, peak-load hour, total breach hours,
// and trend direction from the cross-pillar data already on the page.
function _renderExecHotSpots(data) {
  const el = document.getElementById("exec-hotspots");
  if (!el) return;

  const tiles = [];

  // 1. Worst Sub-App (highest SRI — now window-aware via executive.py overlay)
  const subs = (data?.sub_app_metrics || []).slice().sort((a, b) => (b.sri || 0) - (a.sri || 0));
  if (subs.length) {
    const w = subs[0];
    const col = w.sri > 1 ? "#f43f5e" : w.sri > 0.85 ? "#f59e0b" : "#10d96e";
    // When the per-sub-app WINDOW overlay is present, explain the risk in window
    // terms (days the daily batch window breached) instead of a bare SRI number,
    // so a red tile always matches the Breach Calendar / decision gate.
    const wbd = Number(w.window_breach_days || 0);
    const subTxt = wbd > 0
      ? `SRI ${_n(w.sri).toFixed(2)} · ${wbd} day${wbd !== 1 ? "s" : ""} window breach`
      : `SRI ${_n(w.sri).toFixed(2)} · ${w.job_count} jobs`;
    tiles.push({
      label: "Worst Sub-App",
      value: w.sub_app,
      sub: subTxt,
      color: col,
      icon: "🎯",
    });
  }

  // 2. Hottest Server (highest CPU or memory pressure)
  const heat = data?.server_heatmap;
  if (heat?.servers?.length && heat?.values?.length) {
    let worstIdx = 0, worstVal = -1;
    heat.values.forEach((row, i) => {
      const peak = Math.max(...row.map(Number).filter(Number.isFinite));
      if (peak > worstVal) { worstVal = peak; worstIdx = i; }
    });
    const sname = heat.servers[worstIdx] || "—";
    const col = worstVal >= 80 ? "#f43f5e" : worstVal >= 60 ? "#f59e0b" : "#10d96e";
    tiles.push({
      label: "Hottest Server",
      value: sname.length > 18 ? sname.slice(0, 15) + "…" : sname,
      sub: `peak ${worstVal.toFixed(0)}%`,
      color: col,
      icon: "🔥",
    });
  }

  // 3. Peak Load Hour (from temporal correlation)
  const temp = data?.temporal;
  if (temp?.hours?.length && temp?.job_counts?.length) {
    let peakHour = -1, peakJobs = -1;
    temp.job_counts.forEach((c, i) => {
      const n = Number(c);
      if (n > peakJobs) { peakJobs = n; peakHour = temp.hours[i]; }
    });
    if (peakHour >= 0) {
      tiles.push({
        label: "Peak Load Hour",
        value: `${String(peakHour).padStart(2, "0")}:00`,
        sub: `${peakJobs} jobs running`,
        color: "#22d3ee",
        icon: "⏰",
      });
    }
  }

  // 4. Total Breach Hours (from breach calendar data)
  //    Prefer elapsed_hrs (wall-clock) over total_hrs (summed parallel jobs).
  //    Gate BOTH the day count and the overrun sum by the canonical per-day
  //    breach flag (re-stamped from the window-compliance breach-day set) so
  //    this tile can never disagree with the Breach Calendar / decision gate.
  //    A naive `elapsed > 6h` recompute would falsely flag an 8h WEEKLY window
  //    (OK vs its own 9h ceiling) as a breach — the canonical flag already
  //    accounts for each sub-app's own ceiling.
  const win = window.appData?.batch?.window || [];
  const sla = data?.kpis?.sla_daily_hrs || 6;
  // Effective (longest contiguous block) is the SLA-binding wall-clock; the
  // elapsed span includes idle gaps and overstates the overrun. Prefer the
  // backend's per-day binding overrun (vs each sub-app's OWN ceiling) when present.
  const _whrs = (w) => { const e = Number(w.effective_hrs ?? 0); if (e > 0) return e; const el = Number(w.elapsed_hrs ?? 0); return el > 0 ? el : Number(w.total_hrs ?? 0); };
  const totalOverrun = win.reduce((s, w) => {
    if (!w.breach) return s;
    const ov = (w.breach_overrun_hrs != null && isFinite(+w.breach_overrun_hrs))
      ? +w.breach_overrun_hrs : Math.max(0, _whrs(w) - sla);
    return s + ov;
  }, 0);
  const breachDays = win.filter(w => w.breach).length;
  if (win.length) {
    const col = totalOverrun > 10 ? "#f43f5e" : totalOverrun > 0 ? "#f59e0b" : "#10d96e";
    tiles.push({
      label: "Total Overrun",
      value: totalOverrun > 0 ? `+${totalOverrun.toFixed(1)}h` : "0h",
      sub: breachDays > 0 ? `${breachDays} breach day${breachDays !== 1 ? "s" : ""}` : "All within SLA",
      color: col,
      icon: "⏱️",
    });
  }

  if (!tiles.length) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = tiles.map(t => `
    <div class="rounded-lg bg-Ccard/40 border border-Cborder/40 p-2.5 flex items-center gap-2.5"
         style="border-left:3px solid ${t.color};">
      <div class="text-lg flex-shrink-0">${t.icon}</div>
      <div class="min-w-0 flex-1">
        <div class="text-[8px] uppercase tracking-widest text-Cmuted font-semibold">${_esc(t.label)}</div>
        <div class="text-[12px] font-bold truncate" style="color:${t.color};" title="${_esc(t.value)}">${_esc(t.value)}</div>
        <div class="text-[9px] text-Cmuted truncate">${_esc(t.sub)}</div>
      </div>
    </div>
  `).join("");
}

// ── Sign-Off Readiness Checklist ─────────────────────────────
// Replaces the flat YES/NO/COND tile with an itemized blocker list.
// Each rule: criterion · target · actual · pass/warn/fail status.
function _renderSignoffChecklist(data) {
  const wrap = document.getElementById("exec-signoff-checklist");
  const toggle = document.getElementById("exec-signoff-toggle");
  if (!wrap || !toggle) return;

  const kpis = data?.kpis || {};
  const dc   = (window.appData.batch || {}).data_coverage || {};
  const findings = window._lastFindings || [];
  const critCount = findings.filter(f => f.level === "critical").length;
  const warnCount = findings.filter(f => f.level === "warning").length;
  const sowLoaded = !!(window.appData.sowCompare);
  const evidenceDays = dc.date_span_days || 0;
  const confidence = _n(kpis.confidence_pct ?? 100);
  const winRate = _n(kpis.window_compliance ?? kpis.batch_rate ?? 100);
  const slaQuality = (window.appData.batch || {}).sla_source?.quality || "—";

  // Each rule: { label, target, actual, pass, severity }
  // severity: "blocker" → fails block sign-off, "warning" → conditional only
  const rules = [
    {
      label: "Evidence coverage ≥ 15 days",
      actual: `${evidenceDays}d`,
      pass: evidenceDays >= 15,
      severity: "blocker",
      hint: evidenceDays < 15 ? `Only ${evidenceDays} day(s) on file — need ${15 - evidenceDays} more for full audit confidence.` : "Sufficient history.",
    },
    {
      label: "Confidence ≥ 90%",
      actual: confidence.toFixed(0) + "%",
      pass: confidence >= 90,
      severity: confidence >= 75 ? "warning" : "blocker",
      hint: confidence < 90 ? "Low evidence quality — recheck source data." : "High confidence in measurements.",
    },
    {
      label: "No critical findings",
      actual: `${critCount} critical`,
      pass: critCount === 0,
      severity: "blocker",
      hint: critCount > 0 ? `${critCount} blocker finding(s) require resolution.` : "Clean.",
    },
    {
      label: "Window breach rate < 20%",
      actual: `${(100 - winRate).toFixed(1)}% breached`,
      pass: (100 - winRate) < 20,
      severity: (100 - winRate) > 30 ? "blocker" : "warning",
      hint: (100 - winRate) >= 20 ? "Batch windows missing SLA on too many days." : "Within tolerance.",
    },
    {
      label: "SLA source verified",
      actual: slaQuality,
      pass: !["assumed", "default", "—"].includes(String(slaQuality).toLowerCase()),
      severity: "warning",
      hint: ["assumed", "default", "—"].includes(String(slaQuality).toLowerCase())
        ? "SLA values inferred — confirm with customer contract." : "Source-backed SLA.",
    },
    {
      label: "SOW volume validated",
      actual: sowLoaded ? "loaded" : "N/A",
      pass: sowLoaded,
      severity: "warning",
      hint: sowLoaded ? "SOW comparison active." : "Upload SOW for commercial accountability.",
    },
    {
      label: "No warnings outstanding",
      actual: `${warnCount} warning(s)`,
      pass: warnCount === 0,
      severity: "warning",
      hint: warnCount > 0 ? "Review warnings before final approval." : "All clear.",
    },
  ];

  const blockers = rules.filter(r => !r.pass && r.severity === "blocker").length;
  const warnings = rules.filter(r => !r.pass && r.severity === "warning").length;
  const passed   = rules.filter(r => r.pass).length;

  // Render rows
  wrap.innerHTML = `
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
      ${rules.map(r => {
        const icon = r.pass ? "✓" : (r.severity === "blocker" ? "✗" : "⚠");
        const col  = r.pass ? "#10d96e" : (r.severity === "blocker" ? "#f43f5e" : "#f59e0b");
        return `
          <div class="flex items-start gap-2 px-2.5 py-1.5 rounded bg-Ccard/40 border border-Cborder/30 text-[11px]">
            <div class="font-mono font-bold text-sm leading-none mt-0.5" style="color:${col};">${icon}</div>
            <div class="flex-1 min-w-0">
              <div class="flex items-baseline justify-between gap-2">
                <span class="text-Cwhite font-medium">${_esc(r.label)}</span>
                <span class="font-bold text-[10px] flex-shrink-0" style="color:${col};">${_esc(r.actual)}</span>
              </div>
              ${!r.pass ? `<div class="text-[9px] text-Cmuted mt-0.5 leading-tight">${_esc(r.hint)}</div>` : ""}
            </div>
          </div>`;
      }).join("")}
    </div>
    <div class="flex items-center gap-3 mt-3 pt-2 border-t border-Cborder/40 text-[10px] text-Cmuted">
      <span class="text-Cgreen font-bold">${passed}/${rules.length} passed</span>
      ${blockers > 0 ? `<span class="text-Cred font-bold">${blockers} blocker${blockers !== 1 ? "s" : ""}</span>` : ""}
      ${warnings > 0 ? `<span class="text-Camber font-bold">${warnings} warning${warnings !== 1 ? "s" : ""}</span>` : ""}
    </div>
  `;

  // Toggle behavior — install once
  if (!toggle._wired) {
    toggle.addEventListener("click", () => {
      const isHidden = wrap.classList.contains("hidden");
      wrap.classList.toggle("hidden");
      toggle.textContent = isHidden ? "Hide checklist ▴" : "Show checklist ▾";
    });
    toggle._wired = true;
  }
}

// ── Predictive SLA Breach Forecaster ─────────────────────────
// Linear regression over the existing daily window data.
// Projects the next 14 days; flags the day it crosses SLA.
function _renderExecForecast(windowData, slaHrs) {
  const el = document.getElementById("exec-forecast");
  if (!el) return;
  windowData = (Array.isArray(windowData) ? windowData : [])
    .filter(w => w && w.run_date && Number.isFinite(Number(w.effective_hrs ?? w.elapsed_hrs ?? w.total_hrs)));

  if (windowData.length < 2) {
    el.innerHTML = `<div class="h-full flex flex-col items-center justify-center text-center px-6">
      <div class="text-3xl opacity-40 mb-2">📈</div>
      <div class="text-[12px] text-Cmuted leading-snug">Need 2+ days of run data to forecast — current: ${windowData.length} day(s).</div>
    </div>`;
    return;
  }

  // Sort by date
  windowData = windowData.slice().sort((a, b) => new Date(a.run_date) - new Date(b.run_date));
  const dates = windowData.map(w => new Date(w.run_date));
  // Forecast the SLA-binding effective window (longest contiguous block), not the
  // elapsed span (idle gaps), so the projected crossing of slaHrs is meaningful.
  const ys = windowData.map(w => Number(w.effective_hrs > 0 ? w.effective_hrs : (w.elapsed_hrs > 0 ? w.elapsed_hrs : w.total_hrs)));
  const xs = dates.map((_, i) => i);

  // Simple linear regression: y = a + b*x
  const n = xs.length;
  const sumX = xs.reduce((a, b) => a + b, 0);
  const sumY = ys.reduce((a, b) => a + b, 0);
  const meanX = sumX / n, meanY = sumY / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (xs[i] - meanX) * (ys[i] - meanY);
    den += (xs[i] - meanX) ** 2;
  }
  const slope = den === 0 ? 0 : num / den;
  const intercept = meanY - slope * meanX;

  // Residual std-dev for confidence band
  let ssRes = 0;
  for (let i = 0; i < n; i++) {
    const yhat = intercept + slope * xs[i];
    ssRes += (ys[i] - yhat) ** 2;
  }
  const sigma = n > 2 ? Math.sqrt(ssRes / (n - 2)) : 0;
  const ci = 1.96 * sigma; // 95% band

  // Forecast next 14 days
  const horizon = 14;
  const fxs = [], fys = [], fLow = [], fHigh = [], fDates = [];
  const lastDate = dates[dates.length - 1];
  for (let i = 1; i <= horizon; i++) {
    const x = (n - 1) + i;
    const yhat = intercept + slope * x;
    fxs.push(x);
    fys.push(yhat);
    fLow.push(yhat - ci);
    fHigh.push(yhat + ci);
    const d = new Date(lastDate);
    d.setDate(d.getDate() + i);
    fDates.push(d);
  }

  // Find first day forecast crosses SLA
  let breachIdx = -1;
  for (let i = 0; i < fys.length; i++) {
    if (fys[i] > slaHrs) { breachIdx = i; break; }
  }

  // Plot via Plotly
  if (typeof Plotly === "undefined") return;
  const fmtDate = d => d.toISOString().slice(0, 10);

  const traces = [
    // Confidence band (high)
    {
      x: fDates.map(fmtDate), y: fHigh, type: "scatter", mode: "lines",
      line: { width: 0 }, showlegend: false, hoverinfo: "skip", name: "Upper",
    },
    // Confidence band (low) — fill to previous (high)
    {
      x: fDates.map(fmtDate), y: fLow, type: "scatter", mode: "lines",
      line: { width: 0 }, fill: "tonexty", fillcolor: "rgba(167,139,250,0.18)",
      showlegend: false, hoverinfo: "skip", name: "Lower",
    },
    // Historical line
    {
      x: dates.map(fmtDate), y: ys, type: "scatter", mode: "lines+markers",
      line: { color: "#22d3ee", width: 2 },
      marker: { size: 5, color: "#22d3ee" },
      name: "Actual",
      hovertemplate: "%{x}<br>%{y:.1f}h<extra></extra>",
    },
    // Forecast line
    {
      x: fDates.map(fmtDate), y: fys, type: "scatter", mode: "lines",
      line: { color: "#a78bfa", width: 2, dash: "dot" },
      name: "Forecast",
      hovertemplate: "%{x}<br>≈ %{y:.1f}h<extra></extra>",
    },
  ];

  // Mark predicted breach day
  if (breachIdx >= 0) {
    traces.push({
      x: [fmtDate(fDates[breachIdx])], y: [fys[breachIdx]],
      type: "scatter", mode: "markers+text",
      marker: { size: 12, color: "#f43f5e", symbol: "diamond", line: { color: "#fff", width: 1 } },
      text: ["⚠ Breach"], textposition: "top center",
      textfont: { color: "#f43f5e", size: 10, family: "Sora" },
      showlegend: false, hoverinfo: "skip",
    });
  }

  const layout = {
    margin: { l: 35, r: 10, t: 10, b: 35 },
    plot_bgcolor: "rgba(0,0,0,0)", paper_bgcolor: "rgba(0,0,0,0)",
    font: { family: "Sora", color: "#a8b3d9", size: 9 },
    xaxis: { gridcolor: "rgba(33,48,96,0.4)", color: "#6b7db3", showspikes: false, tickangle: -30, tickfont: { size: 8 } },
    yaxis: { gridcolor: "rgba(33,48,96,0.4)", color: "#6b7db3", title: { text: "Runtime (hrs)", font: { size: 9 } } },
    showlegend: true,
    legend: { orientation: "h", y: -0.22, font: { size: 9, color: "#a8b3d9" } },
    shapes: [{
      type: "line", xref: "paper", x0: 0, x1: 1, y0: slaHrs, y1: slaHrs,
      line: { color: "#f43f5e", width: 1.5, dash: "dash" },
    }],
    annotations: [{
      xref: "paper", x: 0.99, y: slaHrs, xanchor: "right", yanchor: "bottom",
      text: `SLA ${slaHrs}h`, showarrow: false,
      font: { color: "#f43f5e", size: 9 },
    }],
  };
  if (breachIdx >= 0) {
    const daysOut = breachIdx + 1;
    layout.annotations.push({
      xref: "paper", yref: "paper", x: 0.02, y: 0.97, xanchor: "left", yanchor: "top",
      text: `<b>Predicted breach in ~${daysOut} day${daysOut !== 1 ? "s" : ""}</b><br>` +
            `<span style="font-size:9px;">${fmtDate(fDates[breachIdx])} · ${fys[breachIdx].toFixed(1)}h</span>`,
      showarrow: false, align: "left",
      font: { color: "#f43f5e", size: 11, family: "Sora" },
      bgcolor: "rgba(244,63,94,0.12)", bordercolor: "#f43f5e", borderwidth: 1, borderpad: 4,
    });
  } else {
    const trendDir = slope > 0 ? "↑ rising" : slope < 0 ? "↓ improving" : "→ flat";
    const trendCol = slope > 0 ? "#f59e0b" : slope < 0 ? "#10d96e" : "#22d3ee";
    layout.annotations.push({
      xref: "paper", yref: "paper", x: 0.02, y: 0.97, xanchor: "left", yanchor: "top",
      text: `<b style="color:${trendCol};">${trendDir}</b> · No breach in 14d horizon<br>` +
            `<span style="font-size:9px;color:#a8b3d9;">Δ ${(slope * 7).toFixed(2)}h / week</span>`,
      showarrow: false, align: "left",
      font: { color: trendCol, size: 11, family: "Sora" },
      bgcolor: `${trendCol}1a`, bordercolor: trendCol, borderwidth: 1, borderpad: 4,
    });
  }

  _plotlyPurge(el);
  Plotly.newPlot(el, traces, layout, _plotlyConfig());

  // Enterprise: export toolbar for forecast data
  _addChartToolbar(el.parentElement, el, () => {
    let csv = "Date,Window_Hrs,Type\n";
    xs.forEach((d, i) => { csv += `${d.toISOString().slice(0,10)},${ys[i].toFixed(2)},historical\n`; });
    fDates.forEach((d, i) => { csv += `${d.toISOString().slice(0,10)},${fys[i].toFixed(2)},forecast\n`; });
    return csv;
  });
}


// ── Red Flags & RCA ───────────────────────────────────────────
let _redFlagsDebounce = null;
async function triggerRedFlags() {
  return new Promise(resolve => {
    clearTimeout(_redFlagsDebounce);
    _redFlagsDebounce = setTimeout(() => { _triggerRedFlagsImpl().then(resolve).catch(resolve); }, 400);
  });
}
async function _triggerRedFlagsImpl() {
  const btn = document.getElementById("rf-refresh-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Generating…"; }

  const payload = {
    batch_kpis:    window.appData.batch?.kpis    || window.appData.batch    || null,
    resource_kpis: window.appData.resource?.kpis || window.appData.resource || null,
    servers:       window.appData.servers  || [],
    anomalies:     window.appData.batch?.anomalies    || [],
    issues:        window.appData.issues   || [],
    top_breaches:  window.appData.batch?.top_breaches || [],
    sub_stats:     window.appData.batch?.sub_stats    || [],
    // Hardwired interconnection — SLA Matrix output drives matched red flags
    sla_matrix:    window.appData.slaMatrix || null,
  };

  try {
    const res  = await fetch("/api/red-flags", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    if (!res.ok) { toast("error", "Red flags error", (await res.text()).slice(0, 200)); return; }
    const data = await res.json();
    window.appData.redFlags = data;
    _renderRedFlagsResults(data);
    // Cross-pillar cascade — if findings + sla matrix are also loaded, run consultant
    triggerPeConsultant().catch(() => {});
  } catch (err) {
    // Distinguish fetch failures (TypeError) from render-time exceptions
    const isNet = err instanceof TypeError || String(err?.message || "").toLowerCase().includes("fetch");
    if (isNet) {
      _handleFetchError(err, "red-flags");
    } else {
      toast("error", "Red flags render error", String(err?.message || err).slice(0, 200));
      console.error("[red-flags]", err);
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" d="M3 3v1.5M3 21v-6m0 0 2.77-.693a9 9 0 0 1 6.208.682l.108.054a9 9 0 0 0 6.086.71l3.114-.732a48.524 48.524 0 0 1-.005-10.499l-3.11.732a9 9 0 0 1-6.085-.711l-.108-.054a9 9 0 0 0-6.208-.682L3 4.5M3 15V4.5"/></svg> Refresh Flags`;
    }
  }
}

function _renderRedFlagsResults(data) {
  // The Red Flags & RCA UI panel was removed (consolidated into PE Findings).
  // Bail early if the DOM is missing so we don't crash on null .innerHTML.
  // Data is still cached on window.appData.redFlags for Final Judgment etc.
  if (!document.getElementById("rf-questions-list")) return;

  const flags  = data.flags       || [];
  const matrix = data.risk_matrix || [];
  const byRisk = data.by_risk     || {};

  // Counts
  setText("rf-critical", byRisk.CRITICAL ?? 0);
  setText("rf-high",     byRisk.HIGH     ?? 0);
  setText("rf-medium",   byRisk.MEDIUM   ?? 0);
  setText("rf-low",      byRisk.LOW      ?? 0);

  const emptyEl = document.getElementById("rf-empty");
  const listEl  = document.getElementById("rf-questions-list");

  if (!flags.length) {
    if (emptyEl) emptyEl.classList.remove("hidden");
    if (listEl)  listEl.classList.add("hidden");
  } else {
    if (emptyEl) emptyEl.classList.add("hidden");
    if (listEl)  listEl.classList.remove("hidden");

    const RISK_COLORS = {
      CRITICAL: { border: "border-Cred",   bg: "bg-Cred/5",   badge: "bg-Cred/20 text-Cred" },
      HIGH:     { border: "border-Camber", bg: "bg-Camber/5", badge: "bg-Camber/20 text-Camber" },
      MEDIUM:   { border: "border-Cblue",  bg: "bg-Cblue/5",  badge: "bg-Cblue/20 text-Cblue" },
      LOW:      { border: "border-Cgreen", bg: "bg-Cgreen/5", badge: "bg-Cgreen/20 text-Cgreen" },
    };

    listEl.innerHTML = flags.map((f) => {
      const s = RISK_COLORS[f.risk] || RISK_COLORS.MEDIUM;
      return `<div class="rounded-xl border-l-4 ${s.border} ${s.bg} border border-Cborder/60 px-4 py-3">
        <div class="flex items-start gap-2.5">
          <span class="text-xs font-bold text-Cmuted mt-0.5 shrink-0 font-mono">${_esc(f.id)}</span>
          <div class="flex-1">
            <div class="flex items-center gap-2 flex-wrap mb-1">
              <span class="text-[10px] font-bold uppercase tracking-wider text-Cmuted">${_esc(f.category)}</span>
              <span class="px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase ${s.badge}">${f.risk}</span>
              <span class="text-[10px] text-Cmuted font-mono">${_esc(f.data_point)}</span>
            </div>
            <p class="text-xs text-Cmuted mb-1.5 leading-relaxed">${_esc(f.context)}</p>
            <p class="text-xs text-Cwhite font-semibold leading-relaxed">❓ ${_esc(f.question)}</p>
          </div>
        </div>
      </div>`;
    }).join("");
  }

  // Risk matrix
  const matEmptyEl = document.getElementById("rf-matrix-empty");
  const matWrapEl  = document.getElementById("rf-matrix-wrap");
  const matTbody   = document.getElementById("rf-matrix-tbody");
  if (!matrix.length) {
    if (matEmptyEl) matEmptyEl.classList.remove("hidden");
    if (matWrapEl)  matWrapEl.classList.add("hidden");
  } else {
    if (matEmptyEl) matEmptyEl.classList.add("hidden");
    if (matWrapEl)  matWrapEl.classList.remove("hidden");

    const RISK_COL = { CRITICAL: "text-Cred", HIGH: "text-Camber", MEDIUM: "text-Cblue", LOW: "text-Cgreen" };
    if (matTbody) {
      matTbody.innerHTML = matrix.map((m) =>
        `<tr class="hover:bg-Ccard/40 transition-colors">
          <td class="py-2 pr-4 font-semibold text-Cwhite text-xs">${_esc(m.area)}</td>
          <td class="py-2 pr-4 font-bold text-xs uppercase ${RISK_COL[m.risk] || "text-Cmuted"}">${m.risk}</td>
          <td class="py-2 pr-4 text-Cmuted text-xs leading-relaxed">${_esc(m.impact)}</td>
          <td class="py-2 text-Cmuted text-xs leading-relaxed">${_esc(m.recommendation)}</td>
        </tr>`
      ).join("");
    }
  }
}


// ─────────────────────────────────────────────────────────────
// SENIOR PE CONSULTANT — cross-pillar interconnection
// Hardwires SLA Matrix + PE Findings + Red Flags into a single
// consultant verdict. Renders into #pe-consultant-panel.
// ─────────────────────────────────────────────────────────────
let _consultantDebounce = null;
async function triggerPeConsultant() {
  // Debounce: collapse rapid-fire calls into one (600ms)
  if (_consultantDebounce) clearTimeout(_consultantDebounce);
  return new Promise(resolve => {
    _consultantDebounce = setTimeout(() => { _triggerPeConsultantImpl().then(resolve).catch(resolve); }, 600);
  });
}
async function _triggerPeConsultantImpl() {
  // Run when at least 2 of 5 data sources are loaded
  const sm  = window.appData.slaMatrix;
  const fd  = window.appData.findings;
  const rf  = window.appData.redFlags;
  const bat = window.appData.batch;
  const res = window.appData.resource;
  const loaded = [sm, fd, rf, bat, res].filter(Boolean).length;
  if (loaded < 2) return;

  const payload = {
    // Core pillars
    sla_matrix:       sm || null,
    findings:         fd || null,
    red_flags:        rf || null,
    // Batch Review — KPIs + detail
    batch_kpis:       bat?.kpis || null,
    top_jobs:         bat?.top_jobs || [],
    top_breaches:     bat?.top_breaches || [],
    batch_window:     bat?.window || [],
    batch_sub_stats:  bat?.sub_stats || [],
    batch_anomalies:  bat?.anomalies || [],
    // SLA Matrix workflow-level resolved data
    workflow_summary: sm?.workflow_summary || [],
    // Resource fleet
    resource_kpis:    res?.kpis || null,
    servers:          window.appData.servers || [],
    customer_name:    window.appData.customerName || null,
  };

  try {
    const res = await fetch("/api/pe-consultant", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) return;
    const data = await res.json();
    window.appData.peConsultant = data;
    _renderPeConsultant(data);
  } catch (err) {
    /* silent — consultant is best-effort */
  }
}

function _renderPeConsultant(data) {
  const panels = document.querySelectorAll("[data-pe-consultant-panel]");
  if (!panels.length) return;

  const v = data.consultant || {};
  const links = data.cross_links || [];
  const chains = data.evidence_chain || [];
  const acc = data.accuracy || {};

  const decColor = {
    GO:                "text-Cgreen border-Cgreen/50 bg-Cgreen/10",
    HOLD:              "text-Camber border-Camber/50 bg-Camber/10",
    REMEDIATE:         "text-Cred   border-Cred/50   bg-Cred/10",
    INSUFFICIENT_DATA: "text-Cmuted border-Cborder   bg-Ccard/40",
  }[v.decision] || "text-Cmuted border-Cborder bg-Ccard/40";

  const gradeColor = ({ A: "text-Cgreen", B: "text-Cgreen",
                        C: "text-Camber", D: "text-Cred", F: "text-Cred" })[v.grade] || "text-Cmuted";

  const sevColor = {
    CRITICAL: "border-Cred bg-Cred/10 text-Cred",
    HIGH:     "border-Camber bg-Camber/10 text-Camber",
    MEDIUM:   "border-Cblue bg-Cblue/10 text-Cblue",
    LOW:      "border-Cgreen bg-Cgreen/10 text-Cgreen",
  };

  const linksHtml = links.length ? links.slice(0, 8).map((c) => {
    const sev = sevColor[c.severity] || sevColor.MEDIUM;
    const pill = c.pillars.map((p) =>
      `<span class="px-1.5 py-0.5 rounded bg-Ccard text-[9px] uppercase tracking-wider text-Cmuted">${_esc(p)}</span>`
    ).join(" ");
    const slaBits = c.sla_evidence ? Object.entries(c.sla_evidence)
      .filter(([_, vv]) => vv !== "" && vv !== null && vv !== undefined)
      .map(([k, vv]) => `<span class="text-[10px] text-Cmuted">${_esc(k)}: <span class="text-Cwhite font-mono">${_esc(typeof vv === "number" ? Number(vv).toFixed(2) : vv)}</span></span>`)
      .join(" · ") : "";
    const fEvd = (c.findings_evidence || []).slice(0, 2).map((f) =>
      `<li class="text-[11px] text-Cmuted">⚑ <span class="text-Cwhite">${_esc(f.text)}</span></li>`).join("");
    const rEvd = (c.redflags_evidence || []).slice(0, 2).map((r) =>
      `<li class="text-[11px] text-Cmuted">${_esc(r.id)} · <span class="font-bold ${(sevColor[r.risk]||"").split(" ")[2]||""}">${_esc(r.risk)}</span> — <span class="text-Cwhite">${_esc(r.question)}</span></li>`).join("");
    return `<div class="rounded-xl border ${sev} px-3 py-2.5">
      <div class="flex items-center gap-2 flex-wrap mb-1.5">
        <span class="font-mono font-bold text-xs text-Cwhite">${_esc(c.entity)}</span>
        <span class="px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider bg-Ccard text-Cmuted">${_esc(c.entity_kind)}</span>
        <span class="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase ${sev}">${_esc(c.severity)}</span>
        ${pill}
        <span class="ml-auto text-[10px] text-Cmuted">conf ${c.confidence}%</span>
      </div>
      ${slaBits ? `<div class="text-[10px] mb-1">${slaBits}</div>` : ""}
      ${fEvd || rEvd ? `<ul class="space-y-0.5 mt-1">${fEvd}${rEvd}</ul>` : ""}
    </div>`;
  }).join("") : `<p class="text-xs text-Cmuted italic">No cross-pillar overlap detected — pillars are mutually consistent.</p>`;

  const list = (arr, color) => (arr || []).slice(0, 5).map((x) =>
    `<li class="text-[11px] text-Cwhite leading-relaxed before:content-['•'] before:mr-2 before:${color}">${_esc(x)}</li>`).join("");

  const chainHtml = chains.length ? chains.slice(0, 4).map((ch) => {
    const steps = (ch.chain || []).map((s) =>
      `<div class="flex gap-2 text-[11px]">
        <span class="text-Cmuted shrink-0 w-32">${_esc(s.pillar)}</span>
        <span class="text-Cwhite">${_esc(s.fact)}</span>
      </div>`).join("");
    return `<div class="rounded-lg border border-Cborder/60 bg-Ccard/40 px-3 py-2">
      <div class="flex items-center justify-between mb-1.5">
        <span class="font-mono font-bold text-xs text-Cwhite">${_esc(ch.job_name)}</span>
        <span class="text-[10px] text-Camber">${_esc(ch.verdict)}</span>
      </div>
      <div class="space-y-0.5">${steps}</div>
    </div>`;
  }).join("") : "";

  const html = `
    <div class="rounded-2xl border-2 border-Cblue/40 bg-gradient-to-br from-Ccard to-Ccard2 shadow-panel p-6">
      <div class="flex items-start justify-between mb-4 gap-3 flex-wrap">
        <div>
          <div class="flex items-center gap-2 mb-1">
            <span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider bg-Cblue/20 text-Cblue border border-Cblue/40">Senior PE Consultant</span>
            <span class="px-2 py-0.5 rounded-full text-[9px] uppercase tracking-wider bg-Ccard text-Cmuted">${_esc(v.model || "")}</span>
          </div>
          <h2 class="text-base font-bold text-Cwhite">Cross-Pillar Interconnected Verdict</h2>
          <p class="text-[11px] text-Cmuted mt-0.5">SLA Matrix · Batch Review · Resource Fleet · PE Findings · Red Flags — all 5 sources wired into verdict</p>
        </div>
        <div class="flex items-center gap-3">
          <div class="text-center">
            <div class="text-[9px] uppercase tracking-wider text-Cmuted">Grade</div>
            <div class="text-3xl font-black ${gradeColor}">${_esc(v.grade)}</div>
          </div>
          <div class="text-center">
            <div class="text-[9px] uppercase tracking-wider text-Cmuted">Score</div>
            <div class="text-3xl font-black text-Cwhite">${(v.score || 0).toFixed(1)}</div>
          </div>
          <div class="text-center">
            <div class="text-[9px] uppercase tracking-wider text-Cmuted">Decision</div>
            <div class="px-2.5 py-1 rounded-lg border font-bold text-sm ${decColor}">${_esc(v.decision)}</div>
          </div>
        </div>
      </div>

      <p class="text-xs text-Cwhite leading-relaxed mb-3 px-3 py-2 rounded-lg bg-Ccard/40 border border-Cborder/40">${_esc(v.headline)}</p>

      <!-- Data source coverage pills -->
      <div class="flex flex-wrap gap-2 mb-4">
        ${Object.entries(acc.pillars_loaded || {}).map(([src, ok]) => {
          const label = { sla: "SLA Matrix", findings: "PE Findings", redflags: "Red Flags",
                          batch_review: "Batch Review", resource: "Resource Fleet" }[src] || src;
          return ok
            ? `<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-Cgreen/10 border border-Cgreen/30 text-Cgreen">&#10003; ${_esc(label)}</span>`
            : `<span class="flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] bg-Ccard border border-Cborder text-Cmuted">&#9679; ${_esc(label)}</span>`;
        }).join("")}
        <span class="ml-auto text-[10px] text-Cmuted self-center">${acc.coverage_pct || 0}% coverage · ${acc.confidence || 0}% confidence</span>
      </div>

      <div class="grid lg:grid-cols-3 gap-4 mb-4">
        <div>
          <h3 class="text-[10px] uppercase tracking-widest text-Cred font-bold mb-2">Top Risks</h3>
          <ul class="space-y-1.5">${list(v.top_risks, "text-Cred")}</ul>
        </div>
        <div>
          <h3 class="text-[10px] uppercase tracking-widest text-Camber font-bold mb-2">Predictions</h3>
          <ul class="space-y-1.5">${list(v.predictions, "text-Camber")}</ul>
        </div>
        <div>
          <h3 class="text-[10px] uppercase tracking-widest text-Cgreen font-bold mb-2">Next 48h Actions</h3>
          <ul class="space-y-1.5">${list(v.next_actions, "text-Cgreen")}</ul>
        </div>
      </div>

      <div class="mb-4">
        <h3 class="text-[10px] uppercase tracking-widest text-Cblue font-bold mb-2">
          Cross-Pillar Links (${links.length})
          <span class="ml-2 text-Cmuted font-normal normal-case">— same evidence appearing in 2+ pillars</span>
        </h3>
        <div class="grid md:grid-cols-2 gap-2">${linksHtml}</div>
      </div>

      ${chainHtml ? `<div class="mb-4">
        <h3 class="text-[10px] uppercase tracking-widest text-Cmuted font-bold mb-2">Evidence Chains (per job)</h3>
        <div class="grid md:grid-cols-2 gap-2">${chainHtml}</div>
      </div>` : ""}

      <details class="rounded-lg border border-Cborder/40 bg-Ccard/30 px-3 py-2">
        <summary class="text-[11px] text-Cmuted cursor-pointer hover:text-Cwhite">Full consultant narrative + accuracy signal</summary>
        <pre class="mt-2 text-[11px] text-Cwhite whitespace-pre-wrap font-mono leading-relaxed">${_esc(v.narrative || "")}</pre>
        <div class="mt-2 pt-2 border-t border-Cborder/40 text-[10px] text-Cmuted">
          Coverage: <span class="text-Cwhite font-bold">${acc.coverage_pct || 0}%</span> ·
          Confidence: <span class="text-Cwhite font-bold">${acc.confidence || 0}%</span>
          ${(acc.missing_inputs || []).length ? ` · Missing: <span class="text-Camber">${_esc((acc.missing_inputs || []).join("; "))}</span>` : ""}
          ${(acc.notes || []).length ? `<div class="mt-1">${_esc((acc.notes || []).join(" · "))}</div>` : ""}
        </div>
      </details>

      ${(v.tool_count || 0) > 0 ? `
      <details class="mt-2 rounded-lg border border-Cpurple/30 bg-Cpurple/5 px-3 py-2">
        <summary class="text-[11px] text-Cpurple cursor-pointer hover:text-Cwhite">
          🔍 Agent inspected <span class="font-bold">${v.tool_count}</span> piece${v.tool_count === 1 ? "" : "s"} of evidence — show trace
        </summary>
        <div class="mt-2 space-y-1.5">
          ${(v.agent_trace || []).filter((t) => t.kind === "tool_call").map((t) => {
            const args = t.args && typeof t.args === "object" && Object.keys(t.args).length
              ? Object.entries(t.args).map(([k, vv]) => `${_esc(k)}=${_esc(String(vv))}`).join(" · ")
              : "";
            const result = t.result || {};
            const rowCount = result.n_total != null ? result.n_total
                            : Array.isArray(result.rows) ? result.rows.length : null;
            const errMsg = result.error || "";
            const preview = errMsg
              ? `<span class="text-Cred">${_esc(errMsg)}</span>`
              : (rowCount != null
                   ? `<span class="text-Cgreen">${rowCount} row${rowCount === 1 ? "" : "s"}</span>`
                   : `<span class="text-Cgreen">ok</span>`);
            return `<div class="flex items-start gap-2 text-[11px]">
              <span class="font-mono text-Cpurple shrink-0">${t.step}.</span>
              <div class="flex-1">
                <span class="font-mono text-Cwhite">${_esc(t.name || "?")}</span><span class="text-Cmuted">(${args})</span>
                <span class="ml-2">${preview}</span>
              </div>
            </div>`;
          }).join("")}
        </div>
      </details>` : ""}
    </div>
  `;
  panels.forEach((p) => {
    p.classList.remove("hidden");
    p.innerHTML = html;
  });
}


// ── _lastFindings cached in renderFindings() above ──────────


// ─────────────────────────────────────────────────────────────
// Heatmap render functions
// ─────────────────────────────────────────────────────────────

/**
 * renderSlaHeatmap(data)
 *
 * Renders a Job × Date SLA compliance grid into #sla-heatmap-container.
 *
 * data = {
 *   jobs:  string[],                          // top 40 job names
 *   dates: string[],                          // last 21 run dates (YYYY-MM-DD)
 *   cells: [{job, date, hrs, breach}, ...],   // one entry per job×date
 *   limit: number                             // daily SLA hrs (default 6.0)
 * }
 *
 * Color key:
 *   #0f3d24  → no run (dark green)
 *   #10d96e  → OK (< 85 % of limit)
 *   #f59e0b  → near SLA (85–100 % of limit)
 *   #f43f5e  → BREACH (> limit)
 */
function renderSlaHeatmap(data) {
  const section   = document.getElementById("sla-heatmap-section");
  const container = document.getElementById("sla-heatmap-container");
  if (!container) return;
  // Clear stale content whenever data is absent — prevents old heatmap persisting
  // after a file is removed or replaced with a file that has no heatmap data.
  if (!data) {
    container.innerHTML = "";
    if (section) section.classList.add("hidden");
    return;
  }

  const { jobs = [], dates = [], cells = [], limit = 6.0 } = data;
  if (!jobs.length || !dates.length) {
    container.innerHTML = "";
    if (section) section.classList.add("hidden");
    return;
  }

  const jobPriority = data.job_priority || {};
  const priorityRank = (p) => p === "critical" ? 2 : p === "warning" ? 1 : 0;
  const orderedJobs = [...jobs].sort((a, b) => {
    const ma = jobPriority[a] || {};
    const mb = jobPriority[b] || {};
    const pa = priorityRank(ma.priority || "normal");
    const pb = priorityRank(mb.priority || "normal");
    if (pa !== pb) return pb - pa;
    const sa = typeof ma.score === "number" ? ma.score : 0;
    const sb = typeof mb.score === "number" ? mb.score : 0;
    if (sb !== sa) return sb - sa;
    return String(a).localeCompare(String(b));
  });
  const priorityCount = Object.values(jobPriority).filter((meta) => {
    const pr = meta?.priority || "normal";
    return pr !== "normal";
  }).length;

  // Build O(1) lookup: "job||date" → cell object
  const lookup = {};
  for (const c of cells) {
    lookup[`${c.job}||${c.date}`] = c;
  }

  // Shorten date strings: "2024-01-15" → "01-15"
  const fmtDate = (d) => {
    const s = String(d);
    const parts = s.split(/[-/]/);
    if (parts.length === 3) return `${parts[1]}-${parts[2]}`;
    return s.slice(-5);
  };

  // Cell background colour — uses per-cell sla_limit when available (set by
  // _build_sla_heatmap using per-job SLA from BatchSLA XLSX), else global limit.
  const cellColor = (c) => {
    if (!c || c.hrs === null || c.hrs === undefined) return "#0f3d24";
    const cel = c.sla_limit ?? limit;
    if (c.breach)               return "#f43f5e";
    if (c.hrs > cel * 0.85)    return "#f59e0b";
    return "#10d96e";
  };

  // Tooltip text — show the per-job SLA ceiling so analysts see what was used
  const cellTitle = (c) => {
    if (!c || c.hrs === null || c.hrs === undefined) return "No run";
    const cel = c.sla_limit ?? limit;
    return `${c.hrs.toFixed(2)} h — ${c.breach ? "BREACH" : "OK"} (SLA: ${cel.toFixed(2)}h)`;
  };

  const shortDates = dates.map(fmtDate);

  // ── Highlight dates that were detected as spikes in the window chart ──
  const spikeSet = new Set((window._batchWindowSpikes || []).map(s => s.date));

  let html = `
    <table class="text-[10px] border-collapse min-w-max">
      <thead>
        <tr>
          <th class="sticky left-0 z-10 bg-Ccard text-left pr-3 pb-1 text-Cmuted font-semibold
                      whitespace-nowrap" style="min-width:150px">Job</th>
          ${shortDates.map((d, i) => {
            const isSpike = spikeSet.has(dates[i]);
            const spikeStyle = isSpike ? `background:${hexA(THEME.amber,0.18)};border-bottom:2px solid ${THEME.amber}` : "";
            const spikeTitle = isSpike ? ` ⚡ Spike day` : "";
            return `<th class="pb-1 px-0.5 text-Cmuted font-normal text-center whitespace-nowrap"
                 style="${spikeStyle}"
                 title="${_esc(dates[i])}${spikeTitle}">${_esc(d)}${isSpike ? '<br><span style="color:' + THEME.amber + ';font-size:8px">⚡</span>' : ""}</th>`;
          }).join("")}
        </tr>
      </thead>
      <tbody>`;

  for (const job of orderedJobs) {
    const meta = jobPriority[job] || {};
    const pr   = meta.priority || "normal";
    const prCol = pr === "critical" ? THEME.red : pr === "warning" ? THEME.amber : THEME.green;
    const prLabel = pr === "critical" ? "PRIORITY" : pr === "warning" ? "WATCH" : "";
    const rowStyle = pr !== "normal"
      ? `background:${hexA(prCol,0.06)};box-shadow:inset 2px 0 0 ${prCol};`
      : "";
    const jobTitle = `${job}${meta.reason ? ` · ${meta.reason}` : ""}`;
    html += `<tr class="hover:brightness-125 transition-[filter]" style="${rowStyle}">
      <td class="sticky left-0 z-10 bg-Ccard pr-3 py-0.5 text-Cwhite font-mono
                 whitespace-nowrap max-w-[200px] truncate" title="${_esc(jobTitle)}">
        <div class="flex items-center gap-1.5">
          <span class="truncate">${_esc(job)}</span>
          ${prLabel ? `<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[8px] font-bold uppercase tracking-wider"
                style="color:${prCol};background:${hexA(prCol,0.14)};border:1px solid ${hexA(prCol,0.35)}">${prLabel}</span>` : ""}
        </div>
      </td>
      ${dates.map((date) => {
        const c  = lookup[`${job}||${date}`];
        const bg = cellColor(c);
        const tt = cellTitle(c);
        const isSpike = spikeSet.has(date);
        const spikeBorder = isSpike ? `border:1px solid ${hexA(THEME.amber,0.5)}` : "";
        return `<td class="px-0.5 py-0.5 text-center" title="${tt}${isSpike ? " ⚡" : ""}" style="min-width:22px${isSpike ? ";background:" + hexA(THEME.amber,0.06) : ""}">
          <div style="width:20px;height:15px;background:${bg};border-radius:2px;margin:auto;${spikeBorder}"></div>
        </td>`;
      }).join("")}
    </tr>`;
  }

  html += `</tbody></table>`;
  const priorityNote = priorityCount
    ? `<div class="mb-2 text-[10px] text-Cmuted">
        ★ Priority rows are sorted first and flagged when the job has breach or near-SLA days.
      </div>`
    : "";
  container.innerHTML = priorityNote + html;
  if (section) section.classList.remove("hidden");
}


/**
 * renderHourHeatmap(data)
 *
 * Renders a Sub-App × Hour-of-Day execution density grid into
 * #hour-heatmap-container.  Cell intensity is proportional to
 * the job count for that sub-app × hour combination.
 *
 * data = {
 *   sub_apps: string[],                              // top 10 sub-apps
 *   hours:    number[],                              // [0..23]
 *   cells:    [{sub_app, hour, count, total_hrs}…]   // sparse (missing = no jobs)
 * }
 */
function renderHourHeatmap(data) {
  const section   = document.getElementById("hour-heatmap-section");
  const container = document.getElementById("hour-heatmap-container");
  if (!container) return;
  // Clear stale content when data is absent
  if (!data) {
    container.innerHTML = "";
    if (section) section.classList.add("hidden");
    return;
  }

  const { sub_apps = [], hours = [], cells = [] } = data;
  if (!sub_apps.length || !cells.length) {
    container.innerHTML = "";
    if (section) section.classList.add("hidden");
    return;
  }

  // Build lookup: "sub_app||hour" → cell; track global max for normalisation
  const lookup = {};
  let maxCount = 1;
  for (const c of cells) {
    lookup[`${c.sub_app}||${c.hour}`] = c;
    if ((c.count || 0) > maxCount) maxCount = c.count;
  }

  // Hour label "8" → "08:00"
  const fmtHr = (h) => String(h).padStart(2, "0") + ":00";

  // Cell background — low: near-transparent navy, high: vivid cyan
  const cellBg = (c) => {
    if (!c || !c.count) return "rgba(33,48,96,0.25)";
    const t = Math.min(c.count / maxCount, 1);           // 0..1
    const a = (0.10 + t * 0.80).toFixed(2);              // alpha 0.10→0.90
    return `rgba(34,211,238,${a})`;
  };

  const cellTitle = (c) => {
    if (!c || !c.count) return "No jobs";
    return `${c.sub_app} @ ${fmtHr(c.hour)}: ${c.count} job(s), ${(c.total_hrs || 0).toFixed(1)} h total`;
  };

  // Columns: show every other hour label on narrow screens
  const useHours = hours.length ? hours : Array.from({ length: 24 }, (_, i) => i);

  let html = `
    <table class="text-[10px] border-collapse min-w-max">
      <thead>
        <tr>
          <th class="sticky left-0 z-10 bg-Ccard text-left pr-3 pb-1 text-Cmuted font-semibold
                      whitespace-nowrap" style="min-width:130px">Sub-App</th>
          ${useHours.map((h) =>
            `<th class="pb-1 text-Cmuted font-normal text-center whitespace-nowrap"
                 style="min-width:28px;font-size:9px">${fmtHr(h)}</th>`
          ).join("")}
        </tr>
      </thead>
      <tbody>`;

  for (const app of sub_apps) {
    html += `<tr class="hover:brightness-125 transition-[filter]">
      <td class="sticky left-0 z-10 bg-Ccard pr-3 py-0.5 text-Cwhite font-mono
                 whitespace-nowrap max-w-[180px] truncate" title="${_esc(app)}">${_esc(app)}</td>
      ${useHours.map((h) => {
        const c  = lookup[`${app}||${h}`];
        const bg = cellBg(c);
        const tt = cellTitle(c);
        return `<td class="px-0.5 py-0.5" title="${tt}">
          <div style="width:22px;height:16px;border-radius:2px;margin:auto;background:${bg}"></div>
        </td>`;
      }).join("")}
    </tr>`;
  }

  html += `</tbody></table>`;

  // Gradient legend
  const stops = [0.10, 0.26, 0.42, 0.58, 0.74, 0.90]
    .map((a) => `rgba(34,211,238,${a})`)
    .join(",");
  html += `
    <div class="mt-3 flex items-center gap-2.5 text-[10px] text-Cmuted">
      <span>Low</span>
      <div style="height:8px;width:120px;border-radius:4px;
                  background:linear-gradient(to right,${stops})"></div>
      <span>High</span>
      <span class="ml-4 italic">Bright columns = peak scheduling contention windows</span>
    </div>`;

  container.innerHTML = html;
  if (section) section.classList.remove("hidden");
}


// ═══════════════════════════════════════════════════════════════
//  SETTINGS — persistent config + Gemini API key
// ═══════════════════════════════════════════════════════════════

async function loadConfig() {
  try {
    const res  = await fetch("/api/config");
    if (!res.ok) return;
    const cfg  = await res.json();
    window.appData.geminiKey = cfg.gemini_api_key || "";
    window.appData.config    = cfg;               // cache full config for threshold lookups

    // Keep the chart SLA line in sync with the user-configured threshold
    if (cfg.daily_sla_hrs) SLA_DAILY_HRS = Number(cfg.daily_sla_hrs) || 6.0;
    // Keep the buffer-band thresholds (gauge / daily bars / legends / narrative)
    // in sync with pe_config so all panels share one green/amber/red rule.
    if (cfg.sla_atrisk_pct  != null) SLA_ATRISK_PCT  = Number(cfg.sla_atrisk_pct)  || SLA_ATRISK_PCT;
    if (cfg.sla_longjob_pct != null) SLA_LONGJOB_PCT = Number(cfg.sla_longjob_pct) || SLA_LONGJOB_PCT;

    const keyEl = document.getElementById("settings-api-key");
    if (keyEl && cfg.gemini_api_key) keyEl.value = cfg.gemini_api_key;

    const maskedEl = document.getElementById("settings-key-masked");
    if (maskedEl) maskedEl.textContent = cfg.gemini_api_key_masked || "(not set)";

    const nvKeyEl = document.getElementById("settings-nvidia-key");
    if (nvKeyEl && cfg.nvidia_api_key) nvKeyEl.value = cfg.nvidia_api_key;
    const nvMaskedEl = document.getElementById("settings-nvidia-masked");
    if (nvMaskedEl) nvMaskedEl.textContent = cfg.nvidia_api_key_masked || "(not set)";

    const daily    = document.getElementById("cfg-daily-sla");
    const weekly   = document.getElementById("cfg-weekly-sla");
    const biweekly = document.getElementById("cfg-biweekly-sla");
    const monthly  = document.getElementById("cfg-monthly-sla");
    const bench    = document.getElementById("cfg-bench-thresh");
    if (daily    && cfg.daily_sla_hrs)        daily.value    = cfg.daily_sla_hrs;
    if (weekly   && cfg.weekly_sla_hrs)       weekly.value   = cfg.weekly_sla_hrs;
    if (biweekly && cfg.biweekly_sla_hrs)     biweekly.value = cfg.biweekly_sla_hrs;
    if (monthly  && cfg.monthly_sla_hrs)      monthly.value  = cfg.monthly_sla_hrs;
    if (bench    && cfg.benchmark_threshold)  bench.value    = cfg.benchmark_threshold;

    // Restore customer chip only if this tab has an active session
    // (prevents old customer name from bleeding into a new engagement)
    if (cfg.customer_name && _isSessionActive()) {
      applyCustomerName(cfg.customer_name);
    }

    refreshDataStatus();
  } catch (err) {
    // loadConfig runs at page startup — show banner if server is unreachable
    _handleFetchError(err, "loadConfig");
  }
}

/** Refresh the AI engine badge in the header (provider + model). */
async function refreshAiStatus() {
  try {
    const res = await fetch("/api/ai-status");
    if (!res.ok) return;
    const s    = await res.json();
    const chip = document.getElementById("ai-status-chip");
    const lbl  = document.getElementById("ai-status-label");
    // AI engine chip is permanently hidden per UX cleanup; only the
    // Verify button remains so users can still live-test the engine.
    if (chip) { chip.classList.add("hidden"); chip.classList.remove("flex"); }
    if (lbl)  { lbl.textContent = ""; }
    const ready = !!(s.nvidia_key || s.gemini_key);
    const verifyBtn = document.getElementById("ai-verify-btn");
    if (verifyBtn) {
      verifyBtn.classList.toggle("hidden", !ready);
    }
  } catch (err) {
    // Silent on AI status — don't show banner here; loadConfig() will catch it
    const msg = String(err?.message || "");
    if (msg.toLowerCase().includes("failed to fetch") || msg.toLowerCase().includes("networkerror")) {
      _showServerDownBanner();
    }
  }
}


/**
 * Live-probe every text model + Vision and render the result in a modal.
 * Confirms the LLM is actually answering — not just that the keys exist.
 */
async function runAiSelfTest() {
  const btn = document.getElementById("ai-verify-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Probing…"; }
  let modal = document.getElementById("ai-selftest-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "ai-selftest-modal";
    modal.className = "fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm";
    modal.innerHTML = `
      <div class="bg-Ccard border border-Cborder rounded-2xl shadow-panel p-5 w-[640px] max-h-[80vh] overflow-auto">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-sm font-bold text-Cwhite">AI Engine Live Self-Test</h3>
          <button onclick="document.getElementById('ai-selftest-modal').remove()"
                  class="text-Cmuted hover:text-Cwhite text-lg leading-none">×</button>
        </div>
        <div id="ai-selftest-body" class="text-xs text-Cmuted">Running probes…</div>
      </div>`;
    document.body.appendChild(modal);
  }
  const body = document.getElementById("ai-selftest-body");
  body.innerHTML = '<div class="text-Cblue">⏳ Pinging every model — this can take 10-30 seconds…</div>';

  try {
    const res = await fetch("/api/ai-self-test");
    const data = await res.json();
    const rowFor = (r) => {
      const dot = r.status === "ok"   ? '<span class="w-2 h-2 rounded-full bg-Cgreen inline-block"></span>'
              : r.status === "no_key" ? '<span class="w-2 h-2 rounded-full bg-Cmuted inline-block"></span>'
              :                         '<span class="w-2 h-2 rounded-full bg-Cred inline-block"></span>';
      const stat = r.status === "ok" ? "OK" : r.status === "no_key" ? "no key" : "fail";
      const sample = (r.sample || "").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
      const reasonText = (r.reason || "").replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
      const detailLine = r.status === "fail" && reasonText
        ? `<div class="text-[10px] text-Cred/80 mt-0.5 truncate" title="${reasonText}">${reasonText}</div>`
        : "";
      return `<tr class="border-b border-Cborder/40 align-top">
        <td class="py-1 pr-2">${dot}</td>
        <td class="py-1 pr-2 font-mono text-[11px] text-Cwhite">
          ${r.provider}:${r.model}
          ${detailLine}
        </td>
        <td class="py-1 pr-2 text-right ${r.status==='ok'?'text-Cgreen':r.status==='fail'?'text-Cred':'text-Cmuted'}">${stat}</td>
        <td class="py-1 pr-2 text-right text-Cmuted">${r.ms||0}ms</td>
        <td class="py-1 text-Cmuted truncate max-w-[180px]" title="${sample}">${sample}</td>
      </tr>`;
    };

    const v = data.vision || {};
    const visionDot = v.status === "ok" ? "🟢" : v.status === "fail" ? "🔴" : "⚪";
    const visionMetrics = (v.metrics || []).map(m =>
      `<span class="inline-block px-2 py-0.5 mr-1 mb-1 rounded bg-Cpurple/10 border border-Cpurple/30 text-Cpurple text-[10px]">
        ${m.metric_type || "?"}: ${m.max_value ?? m.raw_value ?? "—"}${m.unit||""}
       </span>`).join("");

    body.innerHTML = `
      <div class="mb-3 p-3 rounded-lg bg-Ccard/60 border border-Cborder/40">
        <div class="text-[11px] text-Cmuted mb-1">Summary</div>
        <div class="text-Cwhite font-semibold">
          Text models: ${data.summary.text_ok}/${data.summary.text_total} responding ·
          Vision: ${data.summary.vision_ok ? '<span class="text-Cgreen">OK</span>' : '<span class="text-Cred">not ready</span>'}
        </div>
        <div class="text-[11px] text-Cmuted mt-1">Active model: <span class="font-mono text-Cwhite">${data.summary.active_text_model || '—'}</span></div>
        ${data.summary.promoted_to ? `
          <div class="mt-2 text-[10px] text-Cgreen bg-Cgreen/10 border border-Cgreen/30 rounded px-2 py-1">
            ⚡ Auto-promoted active model to
            <span class="font-mono font-bold">${data.summary.promoted_to}</span>
            — your previous default was unreachable on this account.
            All future calls will use this model first.
          </div>` : ""}
      </div>

      <div class="mb-2 text-[11px] font-bold text-Cwhite uppercase tracking-wider">Text generation (per model probe)</div>
      <table class="w-full text-[11px] mb-4">
        <thead class="text-Cmuted text-[10px]"><tr>
          <th class="py-1"></th><th class="py-1 text-left">Model</th>
          <th class="py-1 text-right">Status</th><th class="py-1 text-right">Latency</th>
          <th class="py-1 text-left">Sample</th>
        </tr></thead>
        <tbody>${(data.text || []).map(rowFor).join("")}</tbody>
      </table>

      <div class="mb-2 text-[11px] font-bold text-Cwhite uppercase tracking-wider">Image → text (Gemini Vision)</div>
      <div class="p-3 rounded-lg bg-Ccard/60 border border-Cborder/40">
        <div>${visionDot} Status: <span class="text-Cwhite">${v.status||'skipped'}</span> · ${v.ms||0}ms</div>
        ${v.error ? `<div class="text-Cred mt-1">${v.error}</div>` : ""}
        ${visionMetrics ? `<div class="mt-2">${visionMetrics}</div>` : ""}
      </div>
    `;
  } catch (err) {
    body.innerHTML = `<div class="text-Cred">Self-test request failed: ${String(err?.message || err)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Verify"; }
  }
}


/**
 * Run the unified cross-pillar AI judgment. Pulls every cached payload
 * from window.appData (resource, batch, SLA, benchmark, correlation,
 * SOW, red-flags, executive) and POSTs to /api/final-judgment.
 */
async function runFinalJudgment() {
  const btn = document.getElementById("fj-run-btn"); // legacy — may be null
  if (btn) { btn.disabled = true; btn.textContent = "⏳ Computing…"; }
  const stamp = document.getElementById("fj-last-run");
  if (stamp) stamp.textContent = "Last run: computing…";

  const ad = window.appData || {};
  const body = {
    resource:    ad.upload      || null,
    batch:       ad.batch       || null,
    sla_matrix:  ad.slaMatrix   || ad.sla_matrix || null,
    benchmark:   ad.benchmark   || null,
    correlation: ad.correlation || null,
    sow:          ad.sowCompare  || ad.sow         || null,
    sow_contract: ad.sowContract                  || null,
    redflags:    ad.redflags    || ad.redFlags    || null,
    executive:   ad.executive   || null,
  };

  try {
    const res = await fetch("/api/final-judgment", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(body),
    });
    if (!res.ok) {
      const txt = await res.text();
      toast("error", "Judgment failed", txt.slice(0, 160));
      return;
    }
    const r = await res.json();

    // Reveal result, hide empty state
    document.getElementById("fj-empty")?.classList.add("hidden");
    document.getElementById("fj-result")?.classList.remove("hidden");

    // Grade label map
    const gradeLabels = {
      A: "APPROVED", B: "APPROVED WITH NOTES", C: "CONDITIONAL HOLD",
      D: "BLOCKED — MINOR", F: "BLOCKED — MAJOR",
    };

    // KPI tiles
    setText("fj-grade",          r.grade || "—");
    setText("fj-grade-label",    gradeLabels[r.grade] || "");
    setText("fj-score",          (r.score ?? 0).toFixed(1));
    setText("fj-pillars-count",  String((r.pillars_present || []).length));
    const decEl = document.getElementById("fj-decision");
    if (decEl) {
      const decLabels = {
        GO: "GO", GO_WITH_NOTES: "GO ★", HOLD: "HOLD",
        BLOCKED: "BLOCKED", REMEDIATE: "BLOCKED", INSUFFICIENT_DATA: "NO DATA",
      };
      decEl.textContent = decLabels[r.decision] || r.decision || "—";
      decEl.className   = "text-base font-extrabold mt-0.5 " + (
        r.decision === "GO"             ? "text-Cgreen" :
        r.decision === "GO_WITH_NOTES"  ? "text-Cblue"  :
        r.decision === "HOLD"           ? "text-Camber" :
                                          "text-Cred"
      );
    }
    setText("fj-model", r.ai_model || "deterministic");

    // Verdict reasoning one-liner
    const vrEl = document.getElementById("fj-verdict-reason");
    if (vrEl && r.verdict_reason) {
      vrEl.textContent = r.verdict_reason;
      vrEl.classList.remove("hidden");
    } else if (vrEl) {
      vrEl.classList.add("hidden");
    }

    // Score Decomposition — weighted contribution bars
    const decomp = document.getElementById("fj-decomposition");
    if (decomp && r.pillar_weights && r.pillar_contributions) {
      decomp.innerHTML = "";
      const maxContrib = Math.max(...Object.values(r.pillar_contributions), 1);
      const pillarNames = {
        batch: "Batch SLA", sla: "SLA Compliance", resource: "Resource Health",
        correlation: "Correlation", benchmark: "Benchmark", sow: "SOW/Volume",
      };
      for (const [k, w] of Object.entries(r.pillar_weights)) {
        const contrib = r.pillar_contributions[k] || 0;
        const raw = r.pillars?.[k];
        const pct = (contrib / (r.score || 1)) * 100;
        const color = raw == null ? "#4b5563"
                    : raw >= 80 ? "#10d96e"
                    : raw >= 60 ? "#f59e0b"
                    : "#f43f5e";
        const row = document.createElement("div");
        row.className = "flex items-center gap-2";
        row.innerHTML = `
          <span class="w-24 text-[10px] text-Cmuted truncate">${pillarNames[k] || k}</span>
          <span class="text-[9px] text-Cmuted/60 w-8 text-right">${(w*100).toFixed(0)}%</span>
          <div class="flex-1 h-3 rounded bg-Cbg/80 overflow-hidden">
            <div class="h-full rounded transition-all duration-500"
                 style="width:${Math.max(pct, 2)}%;background:${color}"></div>
          </div>
          <span class="text-[10px] font-bold w-10 text-right" style="color:${color}">
            ${raw != null ? raw.toFixed(0) : '—'}
          </span>
          <span class="text-[9px] text-Cmuted/50 w-10 text-right">+${contrib.toFixed(1)}</span>`;
        decomp.appendChild(row);
      }
    }

    // Pillar score chips
    const pwrap = document.getElementById("fj-pillars");
    if (pwrap) {
      pwrap.innerHTML = "";
      Object.entries(r.pillars || {}).forEach(([k, v]) => {
        const tone = v >= 90 ? "text-Cgreen border-Cgreen/40" :
                     v >= 75 ? "text-Cblue  border-Cblue/40"  :
                     v >= 60 ? "text-Camber border-Camber/40" :
                               "text-Cred   border-Cred/40";
        const div = document.createElement("div");
        div.className = `rounded-md border bg-Cbg/40 px-2 py-1.5 ${tone}`;
        div.innerHTML = `<div class="text-[9px] uppercase tracking-widest opacity-70">${k}</div>
                         <div class="text-sm font-bold">${Number(v).toFixed(1)}</div>`;
        pwrap.appendChild(div);
      });
    }

    // Evidence Ledger — every fact that moved the score (show-your-work audit trail)
    const ledgerWrap  = document.getElementById("fj-ledger-wrap");
    const ledgerEl    = document.getElementById("fj-ledger");
    const ledgerCount = document.getElementById("fj-ledger-count");
    if (ledgerWrap && ledgerEl) {
      const chain = Array.isArray(r.evidence_chain) ? r.evidence_chain : [];
      // Lead with the penalties that actually moved the score, then the PASS
      // checks so the reviewer sees what was evaluated AND cleared.
      const moved  = chain.filter(e => (e.status === "FAIL" || e.status === "PENALTY") && e.points);
      const passed = chain.filter(e => e.status === "PASS");
      const scoreLn = chain.filter(e => e.signal === "score");
      if (chain.length) {
        ledgerWrap.classList.remove("hidden");
        ledgerEl.innerHTML = "";
        const modeTag = r.scoring_mode === "recompute" ? " · recompute mode" : "";
        if (ledgerCount) ledgerCount.textContent =
          `· ${moved.length} penalty${moved.length === 1 ? "" : "ies"}${modeTag}`;

        const pillarColor = { batch:"#60a5fa", sla:"#c084fc", resource:"#34d399",
                              benchmark:"#fbbf24", sow:"#f472b6", correlation:"#22d3ee" };
        // Per-pillar base→final headline lines first
        scoreLn.forEach(e => {
          const row = document.createElement("div");
          row.className = "flex items-center gap-2 text-[10px]";
          row.innerHTML =
            `<span class="font-bold uppercase tracking-wide w-20 truncate" style="color:${pillarColor[e.pillar]||'#94a3b8'}">${_esc(e.pillar)}</span>
             <span class="text-Cwhite/70">${_esc(e.fact)}</span>`;
          ledgerEl.appendChild(row);
        });
        // Then the penalty lines with their points
        moved.forEach(e => {
          const row = document.createElement("div");
          row.className = "flex items-start gap-2 rounded border border-Cred/25 bg-Cred/5 px-2 py-1";
          row.innerHTML =
            `<span class="text-[10px] font-mono font-bold text-Cred w-12 text-right shrink-0">${e.points.toFixed(1)}</span>
             <span class="text-[10px] uppercase tracking-wide font-bold w-16 truncate shrink-0" style="color:${pillarColor[e.pillar]||'#94a3b8'}">${_esc(e.pillar)}</span>
             <span class="text-[11px] text-Cwhite/90 leading-snug">${_esc(e.fact)}</span>`;
          ledgerEl.appendChild(row);
        });
        // Then a compact line of what was checked and passed
        if (passed.length) {
          const ok = document.createElement("div");
          ok.className = "text-[10px] text-Cgreen/80 pt-0.5";
          ok.innerHTML = `✓ Checked &amp; cleared: ${passed.map(e => _esc(e.signal.replace(/_/g," "))).join(", ")}`;
          ledgerEl.appendChild(ok);
        }
      } else {
        ledgerWrap.classList.add("hidden");
      }
    }

    // Cross-Pillar Links — computed (not LLM-guessed) correlations
    const linksWrap = document.getElementById("fj-links-wrap");
    const linksEl   = document.getElementById("fj-links");
    if (linksWrap && linksEl) {
      const links = Array.isArray(r.cross_pillar_links) ? r.cross_pillar_links : [];
      if (links.length) {
        linksWrap.classList.remove("hidden");
        linksEl.innerHTML = "";
        const sevTone = {
          HIGH:   "border-Cred/40 bg-Cred/5 text-Cred",
          MEDIUM: "border-Camber/40 bg-Camber/5 text-Camber",
          INFO:   "border-Cblue/40 bg-Cblue/5 text-Cblue",
        };
        links.forEach(l => {
          const tone = sevTone[l.severity] || sevTone.INFO;
          const div = document.createElement("div");
          div.className = `rounded-lg border px-2.5 py-1.5 ${tone.split(" ").slice(0,2).join(" ")}`;
          div.innerHTML =
            `<div class="flex items-center gap-1.5 mb-0.5">
               <span class="text-[9px] font-extrabold uppercase tracking-widest ${tone.split(" ")[2]}">${_esc(l.severity || "INFO")}</span>
               <span class="text-[9px] text-Cmuted/70">${(l.pillars||[]).map(_esc).join(" × ")}</span>
             </div>
             <div class="text-[11px] text-Cwhite/90 leading-snug">${_esc(l.text)}</div>`;
          linksEl.appendChild(div);
        });
      } else {
        linksWrap.classList.add("hidden");
      }
    }

    // Narrative + actions
    const narEl = document.getElementById("fj-narrative");
    if (narEl) narEl.textContent = r.narrative || r.verdict || "";
    const actEl = document.getElementById("fj-actions");
    if (actEl) {
      actEl.innerHTML = "";
      (r.next_actions || []).forEach(a => {
        const li = document.createElement("li");
        li.textContent = a;
        actEl.appendChild(li);
      });
    }

    toast("success", "Final judgment ready",
          `${r.grade || "?"} · ${r.decision || "?"} · ${(r.pillars_present || []).length} pillars`);
    if (stamp) {
      const t = new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
      stamp.textContent = `Last run: ${t} · ${r.grade || "?"} · ${r.decision || "?"}`;
    }
  } catch (err) {
    _handleFetchError(err);
    if (stamp) stamp.textContent = "Last run: failed";
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "⚡ Run Final Judgment"; }
  }
}

function loadSettings() { loadConfig(); _loadAzureStatusBadge(); checkAzureIdentity({ loadSubscriptions: false }); }

function toggleSettingsKey() {
  const el = document.getElementById("settings-api-key");
  if (!el) return;
  el.type = el.type === "password" ? "text" : "password";
}

async function saveApiKey() {
  const keyEl    = document.getElementById("settings-api-key");
  const statusEl = document.getElementById("settings-key-status");
  if (!keyEl || !statusEl) return;

  const key = keyEl.value.trim();
  if (!key) { statusEl.textContent = "⚠ Key is empty"; statusEl.className = "text-xs text-Camber"; return; }

  statusEl.textContent = "Validating…";
  statusEl.className   = "text-xs text-Cmuted";

  try {
    const vRes  = await fetch("/api/config/test-key", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const vData = await vRes.json();

    if (!vData.valid) {
      statusEl.textContent = `❌ Invalid: ${vData.error || "unknown"}`;
      statusEl.className   = "text-xs text-Cred";
      return;
    }

    await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gemini_api_key: key }),
    });

    window.appData.geminiKey = key;
    refreshDataStatus();

    const maskedEl = document.getElementById("settings-key-masked");
    if (maskedEl) maskedEl.textContent = key.slice(0, 6) + "••••" + key.slice(-4);

    statusEl.textContent = `✅ Valid — ${vData.recommended || "model ready"}`;
    statusEl.className   = "text-xs text-Cgreen";
    toast("success", "API key saved", "Gemini Vision + AI Insights are now active");
  } catch (err) {
    statusEl.textContent = "❌ Network error";
    statusEl.className   = "text-xs text-Cred";
  }
}

function toggleNvidiaKey() {
  const el = document.getElementById("settings-nvidia-key");
  if (!el) return;
  el.type = el.type === "password" ? "text" : "password";
}

async function saveNvidiaKey() {
  const keyEl    = document.getElementById("settings-nvidia-key");
  const statusEl = document.getElementById("settings-nvidia-status");
  if (!keyEl || !statusEl) return;

  const key = keyEl.value.trim();
  if (!key) { statusEl.textContent = "⚠ Key is empty"; statusEl.className = "text-xs text-Camber"; return; }

  statusEl.textContent = "Validating…";
  statusEl.className   = "text-xs text-Cmuted";

  try {
    const vRes  = await fetch("/api/config/test-nvidia-key", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const vData = await vRes.json();

    if (!vData.valid) {
      statusEl.textContent = `❌ Invalid: ${vData.error || "unknown"}`;
      statusEl.className   = "text-xs text-Cred";
      return;
    }

    await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nvidia_api_key: key }),
    });

    const maskedEl = document.getElementById("settings-nvidia-masked");
    if (maskedEl) maskedEl.textContent = key.slice(0, 8) + "••••" + key.slice(-4);

    statusEl.textContent = `✅ Valid — ${vData.model || "model ready"}`;
    statusEl.className   = "text-xs text-Cgreen";
    toast("success", "NVIDIA key saved", "LLM fallback for resource reports is now active");
  } catch (err) {
    statusEl.textContent = "❌ Network error";
    statusEl.className   = "text-xs text-Cred";
  }
}

async function saveConfig() {
  const daily    = parseFloat(document.getElementById("cfg-daily-sla")?.value    || 6);
  const weekly   = parseFloat(document.getElementById("cfg-weekly-sla")?.value   || 17);
  const biweekly = parseFloat(document.getElementById("cfg-biweekly-sla")?.value || 17);
  const monthly  = parseFloat(document.getElementById("cfg-monthly-sla")?.value  || 17);
  const bench    = parseFloat(document.getElementById("cfg-bench-thresh")?.value || 10);
  const statusEl = document.getElementById("settings-save-status");

  try {
    await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        daily_sla_hrs:    daily,
        weekly_sla_hrs:   weekly,
        biweekly_sla_hrs: biweekly,
        monthly_sla_hrs:  monthly,
        benchmark_threshold: bench,
      }),
    });
    if (statusEl) { statusEl.textContent = "✅ Saved"; statusEl.className = "text-xs text-Cgreen"; }
    toast("success", "Settings saved", "SLA defaults and benchmark threshold updated");
  } catch (_) {
    if (statusEl) { statusEl.textContent = "❌ Failed to save"; statusEl.className = "text-xs text-Cred"; }
  }
}

// ═══════════════════════════════════════════════════════════════
//  AZURE MONITOR — live-fetch integration (personal identity via az login)
// ═══════════════════════════════════════════════════════════════

async function checkAzureIdentity(opts = {}) {
  const loadSubscriptions = !!opts.loadSubscriptions;
  const timeoutMs = Number(opts.timeoutMs || 3000);
  const el = document.getElementById("az-identity-status");
  if (!el) return;
  el.innerHTML = '<span class="text-Cmuted">Checking…</span>';
  try {
    const ctl = new AbortController();
    const tm = setTimeout(() => ctl.abort(), timeoutMs);
    const res = await fetch("/api/azure/auth-status", { signal: ctl.signal });
    clearTimeout(tm);
    const d = await res.json();
    const loginBtn  = document.getElementById("az-browser-login-btn");
    const logoutBtn = document.getElementById("az-browser-logout-btn");
    const statusEl2 = document.getElementById("az-browser-login-status");
    if (d.method && d.method !== "none" && d.name) {
      const displayName = d.display_name || "";
      const userId = d.name || "";
      el.innerHTML =
        `<div class="flex items-center gap-2">` +
        `<span class="w-2 h-2 rounded-full bg-Cgreen inline-block"></span>` +
        `<span class="text-Cgreen font-semibold">Signed in</span>` +
        `</div>` +
        (displayName ? `<div class="text-Cwhite text-[11px] mt-1">${_esc(displayName)}</div>` : '') +
        `<div class="text-Cwhite font-mono text-[10px]">${_esc(userId)}</div>` +
        (d.tenant_id ? `<div class="text-Cmuted text-[10px]">Tenant: ${_esc(d.tenant_id)}</div>` : '');
      if (loginBtn)  loginBtn.classList.add("hidden");
      if (logoutBtn) logoutBtn.classList.remove("hidden");
      if (statusEl2) { statusEl2.textContent = `✅ Signed in as ${displayName || userId}`; statusEl2.className = "text-xs text-Cgreen"; }
      if (loadSubscriptions) loadAzureSubscriptions("");
      _updateUploadAzureStatus(true, displayName || userId, { tenant_id: d.tenant_id || "", method: d.method || "" });
      // Start polling VM cache readiness on dashboard load — if cache is
      // already hot (from a prior session) this completes in one tick.
      _pollVmCacheReady(statusEl2);
      _startAzureAutoSync();
    } else {
      el.innerHTML =
        `<div class="flex items-center gap-2">` +
        `<span class="w-2 h-2 rounded-full bg-Camber inline-block"></span>` +
        `<span class="text-Camber font-semibold">Not signed in</span>` +
        `</div>` +
        `<div class="text-Cmuted text-[10px] mt-1">Click "Sign in with Browser" to connect your Azure identity</div>`;
      if (loginBtn)  loginBtn.classList.remove("hidden");
      if (logoutBtn) logoutBtn.classList.add("hidden");
      if (statusEl2) { statusEl2.textContent = ""; statusEl2.className = "text-xs text-Cmuted"; }
      _updateUploadAzureStatus(false);
      const subSel = document.getElementById("az-subscription-id");
      if (subSel) { subSel.innerHTML = '<option value="">Sign in first</option>'; }
    }
  } catch (_) {
    el.innerHTML = '<span class="text-Cred">Failed to check identity</span>';
  }
}
function _esc(s) { const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }

// ── Browser-based Azure login ────────────────────────────────────────────────
async function azureBrowserLogin() {
  const btn      = document.getElementById("az-browser-login-btn");
  const statusEl = document.getElementById("az-browser-login-status");

  // Quick server reachability check before blocking the button
  try {
    const ping = await fetch("/api/health", { method: "GET",
      signal: AbortSignal.timeout(2000) });
    if (!ping.ok) throw new Error("server unhealthy");
  } catch {
    if (statusEl) {
      statusEl.textContent = "❌ Server not reachable — start the server with start.bat then refresh this page.";
      statusEl.className = "text-xs text-Cred";
    }
    toast("error", "Server offline", "Run start.bat to start the PE Dashboard server, then refresh.");
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = "Opening browser…"; }
  if (statusEl) {
    statusEl.textContent = "Opening Microsoft sign-in page in your browser…";
    statusEl.className = "text-xs text-Cmuted";
  }

  // Progressive status hints so the user knows what to do at each stage
  // First hint fires early: the server may be loading the Azure SDK (first login after
  // server start can take 60-120s on machines with AV scanning crypto libraries).
  const sdkHintTimer = setTimeout(() => {
    if (statusEl) {
      statusEl.textContent = "Loading Azure SDK… this can take up to 2 minutes on first sign-in. Please wait.";
      statusEl.className = "text-xs text-Camber";
    }
  }, 5000);
  const progressHintTimer = setTimeout(() => {
    if (statusEl) {
      statusEl.textContent = "Still loading — check your default browser for the Microsoft login tab.";
      statusEl.className = "text-xs text-Camber";
    }
  }, 30000);
  const mfaHintTimer = setTimeout(() => {
    if (statusEl) {
      statusEl.innerHTML =
        `<span>Complete MFA in your browser, then return here.</span>` +
        `<br><span class="text-Cmuted">If the page says "connection refused" after sign-in, ` +
        `click <strong>Try Again</strong> below — a different port will be used.</span>`;
      statusEl.className = "text-xs text-Camber";
    }
  }, 60000);

  try {
    const ctl = new AbortController();
    const timeoutMs = 300000; // 5 minutes — allows for slow AV-scanned SDK import + MFA
    const tm = setTimeout(() => ctl.abort(), timeoutMs);
    const res  = await fetch("/api/azure/browser-login", { method: "POST", signal: ctl.signal });
    clearTimeout(tm);
    clearTimeout(sdkHintTimer);
    const data = await res.json();
    if (!res.ok) {
      const msg = data?.detail || `HTTP ${res.status}`;
      if (statusEl) { statusEl.textContent = `❌ ${msg}`; statusEl.className = "text-xs text-Cred"; }
      toast("error", "Browser login failed", msg);
      return;
    }

    // Login succeeded — update identity card
    const el = document.getElementById("az-identity-status");
    if (el) {
      el.innerHTML =
        `<div class="flex items-center gap-2">` +
        `<span class="w-2 h-2 rounded-full bg-Cgreen inline-block"></span>` +
        `<span class="text-Cgreen font-semibold">Signed in via browser</span>` +
        `</div>` +
        (data.display_name ? `<div class="text-Cwhite text-[11px] mt-1">${_esc(data.display_name)}</div>` : '') +
        `<div class="text-Cwhite font-mono text-[10px]">${_esc(data.name || "")}</div>` +
        (data.tenant_id ? `<div class="text-Cmuted text-[10px]">Tenant: ${_esc(data.tenant_id)}</div>` : '') +
        `<div class="text-Cmuted text-[10px]">Auth: interactive browser</div>`;
    }

    // Toggle sign-in / sign-out buttons
    if (btn) btn.classList.add("hidden");
    const logoutBtn = document.getElementById("az-browser-logout-btn");
    if (logoutBtn) logoutBtn.classList.remove("hidden");

    // Load subscriptions from browser credential
    if (data.subscriptions && data.subscriptions.length > 0) {
      const sel = document.getElementById("az-subscription-id");
      if (sel) {
        sel.innerHTML = '';
        data.subscriptions.forEach(s => {
          const opt = document.createElement("option");
          opt.value = s.id;
          opt.textContent = `${s.name} (${s.id.slice(0,8)}…)`;
          sel.appendChild(opt);
        });
        if (sel.options.length > 0) sel.selectedIndex = 0;
        _autoSaveAzureConfig();
        loadAzureResourceGroups(sel.value, "");
      }
    } else {
      // Fall back to standard subscription load
      loadAzureSubscriptions("");
    }

    if (statusEl) { statusEl.textContent = `✅ Signed in as ${data.name || data.display_name || "?"}`; statusEl.className = "text-xs text-Cgreen"; }
    toast("success", "Azure browser login", `Signed in as ${data.display_name || data.name || "?"}`);

    // ── Post-login: start polling VM cache readiness so first search is instant ──
    _pollVmCacheReady(statusEl);
    _startAzureAutoSync();

    // Update the fetch modal if it's open
    const notConf = document.getElementById("azure-modal-not-configured");
    const form    = document.getElementById("azure-modal-form");
    if (notConf) notConf.classList.add("hidden");
    if (form)    form.classList.remove("hidden");

  } catch (err) {
    const isNetworkErr = !err?.name || err.name === "TypeError" ||
      (err?.message || "").toLowerCase().includes("networkerror") ||
      (err?.message || "").toLowerCase().includes("failed to fetch");
    const msg = (err && err.name === "AbortError")
      ? "Timed out waiting for sign-in. Please try again and complete login in your browser."
      : isNetworkErr
        ? "Cannot reach server. Start the server with start.bat, then refresh the page and try again."
        : (err?.message || String(err));
    if (statusEl) {
      statusEl.textContent = `❌ ${msg}`;
      statusEl.className = "text-xs text-Cred";
    }
    toast("error", "Browser login error", msg);
  } finally {
    clearTimeout(sdkHintTimer);
    clearTimeout(progressHintTimer);
    clearTimeout(mfaHintTimer);
    if (btn && !btn.classList.contains("hidden")) {
      btn.disabled = false;
      btn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"/></svg> Sign in with Browser';
    }
  }
}

// ── VM cache pre-warm poll ────────────────────────────────────────────────────────────
// After login, polls /api/azure/vm-cache-status every 3s until ready.
// Updates the login status element with a progress indicator so the user
// knows the VM inventory is loading in the background.
let _vmCachePollTimer = null;
async function _pollVmCacheReady(statusEl, maxWaitMs = 90000) {
  if (_vmCachePollTimer) clearInterval(_vmCachePollTimer);
  const t0 = Date.now();
  _vmCachePollTimer = setInterval(async () => {
    try {
      if (Date.now() - t0 > maxWaitMs) {
        clearInterval(_vmCachePollTimer);
        return;
      }
      const res = await fetch("/api/azure/vm-cache-status", { signal: AbortSignal.timeout(3000) });
      if (!res.ok) return;
      const st = await res.json();
      if (st.status === "warming") {
        if (statusEl) {
          statusEl.textContent = "⏳ Loading VM inventory in background…";
          statusEl.className = "text-xs text-Camber";
        }
      } else if (st.status === "ready") {
        clearInterval(_vmCachePollTimer);
        const vmWord = st.vm_count === 1 ? "VM" : "VMs";
        if (statusEl) {
          statusEl.textContent = `✅ VM inventory ready — ${st.vm_count} ${vmWord} cached. Search is now instant.`;
          statusEl.className = "text-xs text-Cgreen";
        }
        // Also update the Azure search placeholder to indicate cache is hot
        const searchInput = document.getElementById("az-vm-search-input");
        if (searchInput && searchInput.placeholder.includes("Loading")) {
          searchInput.placeholder = `Search VMs (${st.vm_count} cached)…`;
        }
      } else if (st.status === "error") {
        clearInterval(_vmCachePollTimer);
        // Don't show an error — search will still work, just slower
      }
    } catch (_) { /* ignore poll errors */ }
  }, 3000);
}

async function azureBrowserLogout() {
  try {
    await fetch("/api/azure/browser-logout", { method: "POST" });
    // Show sign-in, hide sign-out
    const loginBtn  = document.getElementById("az-browser-login-btn");
    const logoutBtn = document.getElementById("az-browser-logout-btn");
    if (loginBtn) {
      loginBtn.classList.remove("hidden");
      loginBtn.disabled = false;   // MUST reset — successful login hides button while still disabled
      loginBtn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"/></svg> Sign in with Browser';
    }
    if (logoutBtn) logoutBtn.classList.add("hidden");
    const statusEl = document.getElementById("az-browser-login-status");
    if (statusEl) { statusEl.textContent = "Browser session cleared."; statusEl.className = "text-xs text-Cmuted"; }
    // Stop all background polling on logout
    if (_vmCachePollTimer) { clearInterval(_vmCachePollTimer); _vmCachePollTimer = null; }
    _stopAzureAutoSync();
    // Refresh identity
    checkAzureIdentity({ loadSubscriptions: false });
    toast("info", "Signed out", "Browser credential cleared. Will fall back to az login.");
  } catch (_) {}
}

// ── Azure auto-sync: refreshes VM inventory cache every 15 minutes ────────
// Runs in the background once the user is signed in. When deep-dive charts
// are visible (data already loaded), it silently pre-warms the VM cache so
// that the next manual refresh hits a warm backend and responds faster.
let _azureAutoSyncTimer = null;
function _startAzureAutoSync() {
  _stopAzureAutoSync();
  _azureAutoSyncTimer = setInterval(async () => {
    try {
      const res = await fetch("/api/azure/vm-cache-status", { signal: AbortSignal.timeout(3000) });
      if (!res.ok) return;
      const st = await res.json();
      // If cache has gone stale, nudge the server to re-warm it
      if (st.status === "idle" || st.status === "error") {
        // A lightweight POST to browser-login endpoint would be intrusive,
        // so just log that cache expired — user will get fresh data on next load
        return;
      }
      // If deep dive data is stale (loaded > 15 min ago), auto-reload silently
      if (_deepDiveData && _lastFetchedVmIds?.length && st.status === "ready") {
        const chartsDiv = document.getElementById("deepdive-charts");
        const hasCharts = chartsDiv && chartsDiv.children.length > 0;
        if (hasCharts) {
          // Auto-reload — show toast so user knows data was refreshed
          await loadMetricsDeepDive();
          toast("info", "Azure data refreshed", "Deep-dive metrics refreshed automatically.");
        }
      }
    } catch (_) { /* ignore auto-sync errors */ }
  }, 15 * 60 * 1000); // every 15 minutes
}
function _stopAzureAutoSync() {
  if (_azureAutoSyncTimer) { clearInterval(_azureAutoSyncTimer); _azureAutoSyncTimer = null; }
}

async function loadAzureSubscriptions(defaultSubId) {
  const sel = document.getElementById("az-subscription-id");
  if (!sel) return;

  // Prefill saved subscription immediately (no spinner) so the modal is
  // usable before the slow subscriptions API call completes.
  let savedSubId = "", savedRg = "";
  try {
    const cfgFast = await fetch("/api/azure/status");
    const cfgFastData = await cfgFast.json();
    savedSubId = cfgFastData.azure_subscription_id_set ? cfgFastData.azure_subscription_id_value : "";
    savedRg    = cfgFastData.azure_resource_group_set  ? cfgFastData.azure_resource_group_value  : "";
    if (savedSubId) {
      sel.innerHTML = `<option value="${_esc(savedSubId)}">${_esc(savedSubId)}</option>`;
      sel.value = savedSubId;
      _autoSaveAzureConfig();
      loadAzureResourceGroups(savedSubId, savedRg);
    } else {
      sel.innerHTML = '<option value="">Loading subscriptions…</option>';
    }
  } catch (_) { /* continue */ }

  // Fetch subscription list — may be instant (from cache) or fast (config
  // fallback with _cache_warming=true meaning background fetch is running).
  async function _fetchAndPopulate(isRetry) {
    try {
      const res = await fetch("/api/azure/subscriptions");
      const d = await res.json();

      if (d.ok && d.subscriptions.length > 0 && !d._cache_warming) {
        // Full list ready — populate dropdown
        sel.innerHTML = '';
        let selected = false;
        d.subscriptions.forEach(s => {
          const opt = document.createElement("option");
          opt.value = s.id;
          opt.textContent = s.name !== s.id ? `${s.name} (${s.id.slice(0,8)}…)` : s.id;
          if (savedSubId && s.id === savedSubId) { opt.selected = true; selected = true; }
          else if (!savedSubId && defaultSubId && s.id === defaultSubId) { opt.selected = true; selected = true; }
          else if (!savedSubId && !defaultSubId && s.is_default) { opt.selected = true; selected = true; }
          sel.appendChild(opt);
        });
        if (!selected && sel.options.length > 0) sel.selectedIndex = 0;
        _autoSaveAzureConfig();
        loadAzureResourceGroups(sel.value, savedRg);

      } else if (d._cache_warming) {
        // Background fetch still in progress — poll every 2s (SDK path is fast)
        const _pollSubs = (attemptsLeft) => {
          if (attemptsLeft <= 0) return; // give up after ~30s
          setTimeout(async () => {
            try {
              const r2 = await fetch("/api/azure/subscriptions");
              const d2 = await r2.json();
              if (d2.ok && d2.subscriptions.length > 0 && !d2._cache_warming) {
                sel.innerHTML = '';
                let selected = false;
                d2.subscriptions.forEach(s => {
                  const opt = document.createElement("option");
                  opt.value = s.id;
                  opt.textContent = s.name !== s.id ? `${s.name} (${s.id.slice(0,8)}\u2026)` : s.id;
                  if (savedSubId && s.id === savedSubId) { opt.selected = true; selected = true; }
                  else if (!savedSubId && defaultSubId && s.id === defaultSubId) { opt.selected = true; selected = true; }
                  else if (!savedSubId && !defaultSubId && s.is_default) { opt.selected = true; selected = true; }
                  sel.appendChild(opt);
                });
                if (!selected && sel.options.length > 0) sel.selectedIndex = 0;
                _autoSaveAzureConfig();
                loadAzureResourceGroups(sel.value, savedRg);
              } else if (d2._cache_warming) {
                _pollSubs(attemptsLeft - 1);
              }
            } catch (_) { _pollSubs(attemptsLeft - 1); }
          }, 2000);
        };
        _pollSubs(15); // up to 15 × 2s = 30s

      } else if (!savedSubId) {
        const msg = d.error ? `No subscriptions — ${d.error}` : "No subscriptions — check az login";
        sel.innerHTML = `<option value="">${_esc(msg)}</option>`;
      }
      // If savedSubId was already shown and list unavailable, keep it.
    } catch (_) {
      if (!sel.value) sel.innerHTML = '<option value="">Failed to load subscriptions</option>';
    }
  }
  _fetchAndPopulate(false);
}

async function loadAzureResourceGroups(subscriptionId, preSelectRg) {
  const sel = document.getElementById("az-resource-group");
  if (!sel) return;
  sel.innerHTML = '<option value="">Loading resource groups…</option>';
  if (!subscriptionId) {
    sel.innerHTML = '<option value="">Select a subscription first</option>';
    return;
  }
  try {
    const res = await fetch(`/api/azure/resource-groups?subscription_id=${encodeURIComponent(subscriptionId)}`);
    const d = await res.json();
    sel.innerHTML = '<option value="">All (entire subscription)</option>';
    if (d.ok && d.resource_groups.length > 0) {
      d.resource_groups.forEach(rg => {
        const opt = document.createElement("option");
        opt.value = rg.name;
        opt.textContent = `${rg.name} (${rg.location})`;
        if (preSelectRg && rg.name === preSelectRg) { opt.selected = true; }
        sel.appendChild(opt);
      });
    }
  } catch (_) {
    sel.innerHTML = '<option value="">All (entire subscription)</option>';
  }
}

function onAzureSubscriptionChange() {
  const subId = document.getElementById("az-subscription-id")?.value || "";
  _autoSaveAzureConfig();
  loadAzureResourceGroups(subId);
}

function onAzureResourceGroupChange() {
  _autoSaveAzureConfig();
}

async function _autoSaveAzureConfig() {
  const payload = {
    azure_subscription_id: (document.getElementById("az-subscription-id")?.value || "").trim(),
    azure_resource_group:  (document.getElementById("az-resource-group")?.value  || "").trim(),
  };
  if (!payload.azure_subscription_id) return;
  try {
    await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    _loadAzureStatusBadge();
  } catch (_) { /* silent */ }
}

async function validateAzure() {
  const statusEl = document.getElementById("azure-config-test-status");
  const subscription_id = (document.getElementById("az-subscription-id")?.value || "").trim();

  if (!subscription_id) {
    if (statusEl) { statusEl.textContent = "⚠ Select a subscription first"; statusEl.className = "text-xs text-Camber"; }
    return;
  }

  if (statusEl) { statusEl.textContent = "Testing (using your az login identity)…"; statusEl.className = "text-xs text-Cmuted"; }
  try {
    const res  = await fetch("/api/azure/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription_id }),
    });
    const data = await res.json();
    if (data.valid) {
      if (statusEl) { statusEl.textContent = `✅ Authenticated — ${data.vm_count_sample || 0}+ VMs found`; statusEl.className = "text-xs text-Cgreen"; }
      toast("success", "Azure connection valid", data.message || "Connection successful");
    } else {
      const hint = data.hint ? ` — ${data.hint}` : "";
      if (statusEl) { statusEl.textContent = `❌ ${(data.error || "auth failed").slice(0, 80)}${hint}`; statusEl.className = "text-xs text-Cred"; }
      toast("error", "Azure auth failed", data.hint || data.error || "Run 'az login' and try again");
    }
  } catch (err) {
    if (statusEl) { statusEl.textContent = `❌ Network error: ${err?.message || err}`; statusEl.className = "text-xs text-Cred"; }
  }
}

async function _loadAzureStatusBadge() {
  try {
    const res  = await fetch("/api/azure/status");
    const data = await res.json();
    const badge = document.getElementById("azure-config-status-badge");
    if (!badge) return;
    if (data.configured) {
      badge.textContent = "✅ Configured";
      badge.className   = "text-[10px] px-2 py-0.5 rounded-full border border-Cgreen/40 text-Cgreen";
    } else {
      badge.textContent = "Not configured";
      badge.className   = "text-[10px] px-2 py-0.5 rounded-full border border-Cmuted/40 text-Cmuted";
    }
    return data.configured;
  } catch (_) { return false; }
}

function openAzureModal() {
  const modal     = document.getElementById("azure-fetch-modal");
  const step1     = document.getElementById("azure-step1");
  const step2     = document.getElementById("azure-step2");
  const statusDiv = document.getElementById("azure-fetch-status");
  if (modal) modal.classList.remove("hidden");
  if (step1) step1.classList.remove("hidden");
  if (step2) step2.classList.add("hidden");
  if (statusDiv) { statusDiv.textContent = ""; statusDiv.classList.add("hidden"); }
  // Reset both filters to ALL and clear search
  _activeVmFilters = { type: "ALL", env: "ALL" };
  document.querySelectorAll('.azure-type-filter').forEach(b => b.classList.remove("az-active"));
  document.querySelector('.azure-type-filter[data-type="ALL"]')?.classList.add("az-active");
  document.querySelectorAll('.azure-env-filter').forEach(b => b.classList.remove("az-active"));
  document.querySelector('.azure-env-filter[data-env="ALL"]')?.classList.add("az-active");
  const srch = document.getElementById("azure-vm-search"); if (srch) srch.value = "";
  // Refresh auth bar and load subscriptions
  _refreshModalAuthBar();
}

function closeAzureModal() {
  const modal = document.getElementById("azure-fetch-modal");
  if (modal) modal.classList.add("hidden");
}

/* ── Modal auth bar: check status and update inline sign-in state ── */
async function _refreshModalAuthBar() {
  const dot      = document.getElementById("azure-modal-auth-dot");
  const label    = document.getElementById("azure-modal-auth-label");
  const bar      = document.getElementById("azure-modal-auth-bar");
  const signIn   = document.getElementById("azure-modal-signin-btn");
  const signOut  = document.getElementById("azure-modal-signout-btn");
  try {
    const res  = await fetch("/api/azure/auth-status");
    const data = await res.json();
    if (data.method && data.method !== "none" && data.name) {
      // Authenticated
      if (dot) { dot.className = "w-2 h-2 rounded-full bg-emerald-400 inline-block"; }
      if (label) { label.className = "text-emerald-400 text-xs"; label.textContent = `Signed in as ${data.display_name || data.name}`; }
      if (bar) { bar.className = "rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-4 py-2.5 flex items-center justify-between gap-3"; }
      if (signIn) signIn.classList.add("hidden");
      if (signOut) signOut.classList.remove("hidden");
      _loadModalSubscriptions();
      _updateUploadAzureStatus(true, data.display_name || data.name, { tenant_id: data.tenant_id || "", method: data.method || "" });
    } else {
      // Not authenticated
      if (dot) { dot.className = "w-2 h-2 rounded-full bg-amber-400 inline-block"; }
      if (label) { label.className = "text-amber-400 text-xs"; label.textContent = "Not signed in — sign in to search & browse Azure VMs"; }
      if (bar) { bar.className = "rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-2.5 flex items-center justify-between gap-3"; }
      if (signIn) signIn.classList.remove("hidden");
      if (signOut) signOut.classList.add("hidden");
      _updateUploadAzureStatus(false);
    }
  } catch (_) {
    if (dot) { dot.className = "w-2 h-2 rounded-full bg-red-400 inline-block"; }
    if (label) { label.className = "text-red-400 text-xs"; label.textContent = "Cannot reach server"; }
    if (bar) { bar.className = "rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-2.5 flex items-center justify-between gap-3"; }
    if (signIn) signIn.classList.add("hidden");
    if (signOut) signOut.classList.add("hidden");
    _updateUploadAzureStatus(false);
  }
}

/* ── Resource Report card — Azure Monitor hero ── */
function _updateUploadAzureStatus(connected, userName, extra) {
  const dot        = document.getElementById("azure-res-dot");
  const statusText = document.getElementById("azure-res-status-text");
  const userEl     = document.getElementById("azure-res-user");
  const tenantEl   = document.getElementById("azure-res-tenant");
  const syncRow    = document.getElementById("azure-res-sync-row");
  const syncTime   = document.getElementById("azure-res-sync-time");
  const vmText     = document.getElementById("azure-res-vm-text");
  const connectBtn = document.getElementById("azure-res-connect-btn");
  const connectTxt = document.getElementById("azure-res-connect-text");
  const card       = document.getElementById("azure-res-card");
  if (!dot || !statusText) return;

  const method = extra?.method || "";

  if (connected) {
    // Identity
    dot.className = "w-2 h-2 rounded-full bg-emerald-400 animate-pulse shrink-0";
    statusText.textContent = "Connected";
    statusText.className = "text-[11px] font-semibold text-emerald-400";
    if (userEl) { userEl.textContent = userName || "Authenticated"; userEl.className = "text-[10px] text-Cwhite mt-0.5 truncate"; }
    if (tenantEl && extra?.tenant_id) { tenantEl.textContent = `Tenant: ${extra.tenant_id.substring(0, 8)}…`; tenantEl.className = "text-[10px] text-Cmuted/60 truncate"; }
    else if (tenantEl) { tenantEl.textContent = ""; }
    // Card border glow
    if (card) { card.style.borderColor = "rgba(16,217,110,0.25)"; }
    // Connect button → Open Azure
    if (connectTxt) connectTxt.textContent = "Open Azure";
    if (connectBtn) {
      connectBtn.className = connectBtn.className
        .replace(/border-Cblue\/40/g, "border-emerald-400/30")
        .replace(/bg-Cblue\/10/g, "bg-emerald-400/10")
        .replace(/text-Cblue/g, "text-emerald-400")
        .replace(/hover:bg-Cblue\/20/g, "hover:bg-emerald-400/20");
    }
    // Sync details
    if (syncRow) syncRow.classList.remove("hidden");
    const azureServers = (window.appData?.resource?.servers || []).filter(s => s.source === "azure_monitor");
    if (syncTime) syncTime.textContent = azureServers.length ? "Synced" : "Not fetched yet";
    if (vmText) vmText.textContent = azureServers.length ? `${azureServers.length} VMs` : "—";
  } else {
    dot.className = "w-2 h-2 rounded-full bg-Cmuted/40 shrink-0";
    statusText.textContent = "Not connected";
    statusText.className = "text-[11px] font-semibold text-Cmuted";
    if (userEl) { userEl.textContent = "Sign in to fetch live Azure VM metrics"; userEl.className = "text-[10px] text-Cmuted/60 mt-0.5 truncate"; }
    if (tenantEl) { tenantEl.textContent = ""; }
    if (card) { card.style.borderColor = ""; }
    if (connectTxt) connectTxt.textContent = "Connect Azure";
    if (connectBtn) {
      connectBtn.className = "flex-1 text-[10px] font-semibold py-2 rounded-lg border transition-all inline-flex items-center justify-center gap-1.5 " +
        "border-Cblue/40 bg-Cblue/10 text-Cblue hover:bg-Cblue/20";
    }
    if (syncRow) syncRow.classList.add("hidden");
  }
}

/* ── Sign in directly from the modal ── */
async function azureModalSignIn() {
  const btn   = document.getElementById("azure-modal-signin-btn");
  const label = document.getElementById("azure-modal-auth-label");
  const dot   = document.getElementById("azure-modal-auth-dot");

  // Fail fast if the server isn't reachable — avoids a frozen "Waiting…" state.
  try {
    const ping = await fetch("/api/health", { method: "GET", signal: AbortSignal.timeout(2000) });
    if (!ping.ok) throw new Error("unhealthy");
  } catch {
    if (label) { label.textContent = "❌ Server not reachable — start the server, then retry."; label.className = "text-red-400 text-xs"; }
    toast("error", "Server offline", "Start the PE Dashboard server, then try again.");
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = "Opening browser…"; }
  if (label) { label.textContent = "Waiting for browser sign-in…"; label.className = "text-Cmuted text-xs"; }
  if (dot) { dot.className = "w-2 h-2 rounded-full bg-Cmuted animate-pulse inline-block"; }

  // Progressive hints so a stalled loopback redirect doesn't look like a freeze.
  const hint1 = setTimeout(() => { if (label) { label.textContent = "Still waiting — complete sign-in in the Microsoft tab that opened."; label.className = "text-amber-400 text-xs"; } }, 30000);
  const hint2 = setTimeout(() => { if (label) { label.textContent = 'If the tab says "connection refused" after sign-in, click Sign in again (a new port is used).'; label.className = "text-amber-400 text-xs"; } }, 90000);

  // Hard ceiling so the modal can NEVER hang forever (mirrors the Settings login).
  const ctl = new AbortController();
  const tm  = setTimeout(() => ctl.abort(), 300000);
  try {
    const res = await fetch("/api/azure/browser-login", { method: "POST", signal: ctl.signal });
    const data = await res.json();
    if (!res.ok) {
      if (label) { label.textContent = `❌ ${data.detail || "Login failed"}`; label.className = "text-red-400 text-xs"; }
      toast("error", "Browser login failed", data.detail || "Unknown error");
      return;
    }
    toast("success", "Azure signed in", `Signed in as ${data.display_name || data.name || "?"}`);
    // Also update the Settings page identity card if present
    const el = document.getElementById("az-identity-status");
    if (el) {
      el.innerHTML =
        `<div class="flex items-center gap-2">` +
        `<span class="w-2 h-2 rounded-full bg-Cgreen inline-block"></span>` +
        `<span class="text-Cgreen font-semibold">Signed in via browser</span>` +
        `</div>` +
        (data.display_name ? `<div class="text-Cwhite text-[11px] mt-1">${_escHtml(data.display_name)}</div>` : '') +
        `<div class="text-Cwhite font-mono text-[10px]">${_escHtml(data.name || "")}</div>` +
        (data.tenant_id ? `<div class="text-Cmuted text-[10px]">Tenant: ${_escHtml(data.tenant_id)}</div>` : '') +
        `<div class="text-Cmuted text-[10px]">Auth: interactive browser</div>`;
    }
    const logoutBtn = document.getElementById("az-browser-logout-btn");
    if (logoutBtn) logoutBtn.classList.remove("hidden");
    // Refresh the modal auth bar — flips to "Signed in as…" and loads subscriptions
    _refreshModalAuthBar();
  } catch (err) {
    const msg = (err && err.name === "AbortError")
      ? "Timed out waiting for sign-in. Click Sign in again and complete login in your browser."
      : (err?.message || String(err));
    if (label) { label.textContent = `❌ ${msg}`; label.className = "text-red-400 text-xs"; }
    toast("error", "Browser login error", msg);
  } finally {
    clearTimeout(tm); clearTimeout(hint1); clearTimeout(hint2);
    if (btn) { btn.disabled = false; btn.innerHTML = '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"/></svg> Sign in with Browser'; }
  }
}

/* ── Sign out from the modal ── */
async function azureModalSignOut() {
  try {
    await fetch("/api/azure/browser-logout", { method: "POST" });
    toast("info", "Signed out", "Browser credential cleared.");
    _refreshModalAuthBar();
    // Also refresh Settings page if present
    checkAzureIdentity({ loadSubscriptions: true });
  } catch (_) {}
}

/* ── Load subscriptions into the modal dropdown ── */
async function _loadModalSubscriptions() {
  const sel = document.getElementById("azure-modal-sub");
  if (!sel) return;

  // Resolve the configured subscription once so we can pre-select it.
  let cfgSub = "";
  try { const c = await fetch("/api/azure/status"); const cs = await c.json(); cfgSub = cs.azure_subscription_id_value || ""; } catch {}

  const renderSubs = (subs) => {
    sel.innerHTML = "";
    for (const s of subs) {
      const opt = document.createElement("option");
      opt.value = s.id;
      const nm = (s.name && s.name !== s.id) ? s.name : s.id;
      opt.textContent = `${nm} (${String(s.id).slice(0, 8)}…)`;
      if (s.id === cfgSub || s.is_default) opt.selected = true;
      sel.appendChild(opt);
    }
    azureLoadRGs();
  };

  // The subscription list is filled by a background worker AFTER sign-in, so the
  // first call almost always returns `_cache_warming`. Poll briefly instead of
  // giving up — otherwise the dropdown sticks on its placeholder or wrongly
  // reports "No subscriptions found".
  const MAX_TRIES = 12, DELAY_MS = 2000;
  for (let attempt = 0; attempt < MAX_TRIES; attempt++) {
    let d;
    try {
      const r = await fetch("/api/azure/subscriptions");
      d = await r.json();
    } catch {
      sel.innerHTML = '<option value="">Failed to load — reopen to retry</option>';
      return;
    }
    const subs = d.subscriptions || [];
    if (d.ok === false && !subs.length) {        // genuinely not signed in
      sel.innerHTML = '<option value="">Sign in to load subscriptions</option>';
      return;
    }
    if (subs.length && !d._cache_warming) {      // full list ready
      renderSubs(subs);
      return;
    }
    sel.innerHTML = '<option value="">Loading subscriptions…</option>';
    await new Promise(res => setTimeout(res, DELAY_MS));
  }

  // Warmed too slowly — render whatever we have (e.g. the saved sub) or say so.
  try {
    const r = await fetch("/api/azure/subscriptions");
    const d = await r.json();
    const subs = d.subscriptions || [];
    if (subs.length) { renderSubs(subs); return; }
  } catch {}
  sel.innerHTML = '<option value="">No subscriptions found — check your Azure RBAC access</option>';
}

/* ── Load resource groups when subscription changes ── */
async function azureLoadRGs() {
  const subSel = document.getElementById("azure-modal-sub");
  const rgSel  = document.getElementById("azure-modal-rg-select");
  if (!subSel || !rgSel) return;
  const subId = subSel.value;
  rgSel.innerHTML = '<option value="">All (entire subscription)</option>';
  if (!subId) return;
  try {
    const r = await fetch(`/api/azure/resource-groups?subscription_id=${encodeURIComponent(subId)}`);
    const d = await r.json();
    for (const g of (d.resource_groups || [])) {
      const opt = document.createElement("option");
      opt.value = g.name;
      opt.textContent = `${g.name} (${g.location})`;
      rgSel.appendChild(opt);
    }
  } catch {}
}

/* ── Cached discovered VMs ── */
let _discoveredVMs = [];
let _selectedVmIds = new Set();   // Persistent selection — survives filter switches
let _activeVmFilters = { type: "ALL", env: "ALL" };

/* ── Detect VM environment from Azure tags or name prefix ── */
function _getVmEnv(vm) {
  const tags = vm.tags || {};
  const tagEnv = (tags.Environment || tags.environment || tags.Env || tags.env || tags.Tier || "").toUpperCase();
  for (const [env, rx] of [["PROD",/PROD/],["TEST",/TEST|QA/],["UAT",/UAT/],["STG",/STG|STAGE/],["DEV",/DEV/]]) {
    if (rx.test(tagEnv)) return env;
  }
  const n = (vm.name || "").toUpperCase();
  if (/^PR[A-Z]{2}\d|PROD[_\-]/.test(n))         return "PROD";
  if (/^TS[A-Z]{2}\d|^TST|TEST[_\-]/.test(n))    return "TEST";
  if (/^UA[A-Z]{2}\d|UAT[_\-]/.test(n))           return "UAT";
  if (/^ST[A-Z]{2}\d|STG[_\-]|STAGE[_\-]/.test(n)) return "STG";
  if (/^DV[A-Z]{2}\d|DEV[_\-]/.test(n))           return "DEV";
  return "PROD"; // default — assume production
}

/* ── Helper: show discovered VMs in step 2 ── */
function _showDiscoveredVMs(data, statusEl, statusMsg) {
  _discoveredVMs = data.vms || [];
  _selectedVmIds = new Set();
  if (!_discoveredVMs.length) {
    if (statusEl) { statusEl.textContent = "No VMs found."; statusEl.className = "text-xs text-amber-400"; }
    return;
  }
  const step1 = document.getElementById("azure-step1");
  if (step1) step1.classList.add("hidden");
  const step2 = document.getElementById("azure-step2");
  if (step2) step2.classList.remove("hidden");

  const counts = data.counts || {};
  document.getElementById("azure-vm-total").textContent = `${data.total} VMs`;
  document.getElementById("azure-vm-app-badge").textContent = `APP ${counts.APP || 0}`;
  document.getElementById("azure-vm-db-badge").textContent  = `DB ${counts.DB || 0}`;
  document.getElementById("azure-vm-sre-badge").textContent = `SRE ${counts.SRE || 0}`;

  _updateFilterCounts();
  _renderVMTable(_discoveredVMs);
  _updateSelectedCount();
  if (statusEl) { statusEl.textContent = statusMsg; statusEl.className = "text-xs text-emerald-400"; }
}

/* ── Inject live counts into filter buttons + env summary badges ── */
function _updateFilterCounts() {
  const typeCounts = {APP:0, DB:0, SRE:0};
  const envCounts  = {};
  _discoveredVMs.forEach(v => {
    typeCounts[v.type] = (typeCounts[v.type]||0) + 1;
    const e = _getVmEnv(v); envCounts[e] = (envCounts[e]||0) + 1;
  });
  // Type chips
  const tc = {"APP":"az-cnt-app","DB":"az-cnt-db","SRE":"az-cnt-sre"};
  Object.entries(tc).forEach(([t,id]) => { const el = document.getElementById(id); if (el) el.textContent = typeCounts[t] || ""; });
  // Env chips
  const ec = {"PROD":"az-cnt-prod","TEST":"az-cnt-test","UAT":"az-cnt-uat","STG":"az-cnt-stg","DEV":"az-cnt-dev"};
  Object.entries(ec).forEach(([e,id]) => { const el = document.getElementById(id); if (el) el.textContent = envCounts[e] || ""; });
  // Env summary badges
  const envBadgeContainer = document.getElementById("azure-vm-env-badges");
  if (envBadgeContainer) {
    const envStyles = {PROD:"env-PROD",TEST:"env-TEST",UAT:"env-UAT",STG:"env-STG",DEV:"env-DEV"};
    envBadgeContainer.innerHTML = Object.entries(envCounts)
      .sort(([a],[b]) => a.localeCompare(b))
      .map(([e,c]) => `<span class="az-env-badge ${envStyles[e]||''}">${e} ${c}</span>`)
      .join("");
  }
}

/* ── Search VMs across all subscriptions (Resource Graph) ── */
async function azureSearchVMs() {
  const btn    = document.getElementById("azure-search-btn");
  const status = document.getElementById("azure-discover-status");
  const query  = (document.getElementById("azure-search-input")?.value || "").trim();

  if (!query) { if (status) { status.textContent = "Enter a search term (customer name, server name, tag…)."; status.className = "text-xs text-amber-400"; } return; }

  if (btn) { btn.disabled = true; btn.textContent = "Searching…"; }
  if (status) { status.textContent = `Searching across all subscriptions for "${query}"…`; status.className = "text-xs text-Cmuted"; }

  try {
    const res = await fetch("/api/azure/search-vms", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ query })
    });
    const data = await res.json();
    if (!res.ok) {
      if (status) { status.textContent = `❌ ${data.detail || "Search failed"}`; status.className = "text-xs text-red-400"; }
      return;
    }
    _showDiscoveredVMs(data, status, `✅ Found ${data.total} VMs matching "${query}"`);
  } catch (err) {
    if (status) { status.textContent = `❌ ${err.message}`; status.className = "text-xs text-red-400"; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Search"; }
  }
}

/* ── Step 1: Discover VMs (by subscription) ── */
async function azureDiscoverVMs() {
  const btn    = document.getElementById("azure-discover-btn");
  const status = document.getElementById("azure-discover-status");
  const subId  = document.getElementById("azure-modal-sub")?.value || "";
  const rg     = document.getElementById("azure-modal-rg-select")?.value || "";

  if (!subId) { if (status) status.textContent = "Select a subscription first."; return; }

  if (btn) { btn.disabled = true; btn.textContent = "Scanning…"; }
  if (status) { status.textContent = "Listing VMs in subscription…"; status.className = "text-xs text-Cmuted"; }

  try {
    // Save subscription to config so fetch uses it
    await fetch("/api/config", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ azure_subscription_id: subId, azure_resource_group: rg || "" })
    });

    const res = await fetch("/api/azure/discover-vms", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ subscription_id: subId, resource_group: rg || null })
    });
    const data = await res.json();
    if (!res.ok) {
      if (status) { status.textContent = `❌ ${data.detail || "Discovery failed"}`; status.className = "text-xs text-red-400"; }
      return;
    }

    _discoveredVMs = data.vms || [];
    if (!_discoveredVMs.length) {
      if (status) { status.textContent = "No VMs found in this subscription/resource group."; status.className = "text-xs text-amber-400"; }
      return;
    }

    _showDiscoveredVMs(data, status, `✅ Found ${data.total} VMs`);

  } catch (err) {
    if (status) { status.textContent = `❌ ${err.message}`; status.className = "text-xs text-red-400"; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Browse"; }
  }
}

/* ── Render VM table rows — grouped by customer ── */
function _renderVMTable(vms) {
  const tbody = document.getElementById("azure-vm-table-body");
  if (!tbody) return;

  const typeColors = {
    APP: "text-emerald-400 bg-emerald-500/10 border-emerald-500/30",
    DB:  "text-blue-400 bg-blue-500/10 border-blue-500/30",
    SRE: "text-amber-400 bg-amber-500/10 border-amber-500/30",
  };

  // Group by customer
  const grouped = {};
  vms.forEach((vm, i) => {
    const cust = vm.customer || (vm.tags||{}).CustomerName || (vm.tags||{}).customerName || "Untagged";
    if (!grouped[cust]) grouped[cust] = [];
    grouped[cust].push({ vm, idx: i });
  });

  const customers = Object.keys(grouped);
  const multiCustomer = customers.length > 1 || (customers.length === 1 && customers[0] !== "Untagged");

  let html = "";
  for (const cust of customers) {
    // Customer group header (only if multiple customers or tagged)
    if (multiCustomer) {
      const count = grouped[cust].length;
      const typeBreakdown = {};
      grouped[cust].forEach(({vm}) => { typeBreakdown[vm.type] = (typeBreakdown[vm.type]||0) + 1; });
      const badges = Object.entries(typeBreakdown).map(([t,c]) => `${t}:${c}`).join(" · ");
      const allSelected = grouped[cust].every(({vm}) => _selectedVmIds.has(vm.resource_id));
      const someSelected = grouped[cust].some(({vm}) => _selectedVmIds.has(vm.resource_id));
      html += `<tr class="bg-Cbg/60 border-t border-Cborder">
        <td class="px-2 py-1.5"><input type="checkbox" ${allSelected ? 'checked' : ''} ${!allSelected && someSelected ? 'indeterminate' : ''} class="azure-cust-check rounded border-Cborder"
            onchange="azureToggleCustomer(this, '${_escHtml(cust)}')" title="Select/deselect all ${_escHtml(cust)} VMs" /></td>
        <td colspan="6" class="px-2 py-1.5">
          <span class="text-[10px] font-bold text-Cwhite">${_escHtml(cust)}</span>
          <span class="text-[10px] text-Cmuted ml-2">${count} VM${count>1?'s':''} · ${badges}</span>
        </td>
      </tr>`;
    }

    for (const { vm, idx } of grouped[cust]) {
      const colors = typeColors[vm.type] || typeColors.APP;
      const app = vm.application || (vm.tags||{}).Application || (vm.tags||{}).application || "";
      const isChecked = _selectedVmIds.has(vm.resource_id);
      const env = _getVmEnv(vm);
      const envClr = {PROD:"text-red-400",TEST:"text-sky-400",UAT:"text-violet-400",STG:"text-orange-400",DEV:"text-teal-400"}[env] || "text-Cmuted";
      html += `<tr class="hover:bg-Cbg/40 azure-vm-row" data-type="${vm.type}" data-env="${env}" data-customer="${_escHtml(cust)}" data-idx="${idx}">
        <td class="px-2 py-1.5"><input type="checkbox" ${isChecked ? 'checked' : ''} class="azure-vm-check rounded border-Cborder" data-rid="${_escHtml(vm.resource_id)}" data-customer="${_escHtml(cust)}" onchange="_onVmCheckChange(this)" /></td>
        <td class="px-2 py-1.5 text-Cwhite font-mono text-[11px] font-medium">${_escHtml(vm.name)}</td>
        <td class="px-2 py-1.5">
          <select class="azure-type-select bg-transparent border rounded px-1 py-0.5 text-[10px] font-bold ${colors}" data-rid="${_escHtml(vm.resource_id)}"
                  onchange="azureChangeVMType('${_escHtml(vm.resource_id)}', this.value)">
            <option value="APP" ${vm.type==='APP'?'selected':''} class="bg-Cbg text-Cwhite">APP</option>
            <option value="DB" ${vm.type==='DB'?'selected':''} class="bg-Cbg text-Cwhite">DB</option>
            <option value="SRE" ${vm.type==='SRE'?'selected':''} class="bg-Cbg text-Cwhite">SRE</option>
          </select>
        </td>
        <td class="px-2 py-1.5 text-[10px] font-bold"><span class="az-env-badge env-${env}">${env}</span></td>
        <td class="px-2 py-1.5 text-Cmuted text-[10px]">${_escHtml(app)}</td>
        <td class="px-2 py-1.5 text-Cmuted text-[10px] max-w-[140px] truncate" title="${_escHtml(cust)}">${_escHtml(cust)}</td>
        <td class="px-2 py-1.5 text-Cmuted text-[10px] hidden sm:table-cell">${_escHtml(vm.location)}</td>
      </tr>`;
    }
  }
  tbody.innerHTML = html;

  // Set indeterminate state on customer group checkboxes (must be done via JS, not HTML attribute)
  document.querySelectorAll('.azure-cust-check').forEach(cb => {
    const custRow = cb.closest('tr');
    if (!custRow) return;
    // Find customer name from sibling cell
    const custName = custRow.querySelector('td[colspan] .text-\\[10px\\]')?.textContent?.trim() ||
                     custRow.querySelectorAll('td')[1]?.querySelector('span')?.textContent?.trim() || "";
    if (!custName) return;
    const custVms = _discoveredVMs.filter(v =>
      (v.customer || (v.tags||{}).CustomerName || (v.tags||{}).customerName || "Untagged") === custName);
    const allSel = custVms.every(v => _selectedVmIds.has(v.resource_id));
    const anySel = custVms.some(v => _selectedVmIds.has(v.resource_id));
    cb.checked       = allSel;
    cb.indeterminate = !allSel && anySel;
  });
}

/* Toggle all VMs for a specific customer */
function azureToggleCustomer(headerCb, customer) {
  document.querySelectorAll(`.azure-vm-check[data-customer="${customer}"]`).forEach(cb => {
    cb.checked = headerCb.checked;
    _syncVmSelection(cb);
  });
  _updateSelectedCount();
}

function _escHtml(s) { const d=document.createElement("div"); d.textContent=s||""; return d.innerHTML; }

/* ── Track checkbox change → persist in _selectedVmIds ── */
function _onVmCheckChange(cb) {
  _syncVmSelection(cb);
  _updateSelectedCount();
}
function _syncVmSelection(cb) {
  const rid = cb.dataset.rid;
  if (!rid) return;
  if (cb.checked) _selectedVmIds.add(rid);
  else _selectedVmIds.delete(rid);
}

/* ── Type override by user — uses resource_id, not filter-array index ── */
function azureChangeVMType(resourceId, newType) {
  const vm = _discoveredVMs.find(v => v.resource_id === resourceId);
  if (vm) vm.type = newType;
  const counts = {APP:0,DB:0,SRE:0};
  _discoveredVMs.forEach(v => counts[v.type] = (counts[v.type]||0) + 1);
  document.getElementById("azure-vm-app-badge").textContent = `APP ${counts.APP}`;
  document.getElementById("azure-vm-db-badge").textContent  = `DB ${counts.DB}`;
  document.getElementById("azure-vm-sre-badge").textContent = `SRE ${counts.SRE}`;
  _updateFilterCounts();
  _applyVmFilters();
}

/* ── Select / deselect all (works across ALL VMs, not just visible) ── */
function azureSelectAll(checked) {
  if (checked) {
    _discoveredVMs.forEach(v => _selectedVmIds.add(v.resource_id));
  } else {
    _selectedVmIds.clear();
  }
  // Update visible checkboxes to match
  document.querySelectorAll(".azure-vm-check").forEach(cb => cb.checked = checked);
  const allCb = document.getElementById("azure-vm-checkall");
  if (allCb) allCb.checked = checked;
  _updateSelectedCount();
}
function azureToggleAll(checked) {
  // Toggle only the currently visible filtered VMs
  document.querySelectorAll(".azure-vm-check").forEach(cb => {
    cb.checked = checked;
    _syncVmSelection(cb);
  });
  _updateSelectedCount();
}

function _updateSelectedCount() {
  const el = document.getElementById("azure-selected-count");
  if (el) el.textContent = `${_selectedVmIds.size} of ${_discoveredVMs.length} selected`;
}

/* ── Combined filter: respects type + env + name search ── */
function _applyVmFilters() {
  const activeTypes = [...document.querySelectorAll('.azure-type-filter.az-active')].map(b => b.dataset.type);
  const showAllTypes = activeTypes.includes("ALL") || activeTypes.length === 0;
  const searchQ = (document.getElementById("azure-vm-search")?.value || "").toLowerCase().trim();
  let filtered = showAllTypes ? _discoveredVMs : _discoveredVMs.filter(v => activeTypes.includes(v.type));
  if (_activeVmFilters.env !== "ALL") {
    filtered = filtered.filter(v => _getVmEnv(v) === _activeVmFilters.env);
  }
  if (searchQ) {
    filtered = filtered.filter(v =>
      (v.name || "").toLowerCase().includes(searchQ) ||
      (v.application || "").toLowerCase().includes(searchQ) ||
      (v.customer || "").toLowerCase().includes(searchQ)
    );
  }
  _renderVMTable(filtered);
  _updateSelectedCount();
  // Sync header checkbox state to visible rows
  const allCb = document.getElementById("azure-vm-checkall");
  if (allCb && filtered.length > 0) {
    const allVisible = filtered.every(v => _selectedVmIds.has(v.resource_id));
    const anyVisible = filtered.some(v => _selectedVmIds.has(v.resource_id));
    allCb.checked       = allVisible;
    allCb.indeterminate = !allVisible && anyVisible;
  }
}

/* ── Env filter — single-select radio style ── */
function azureFilterEnv(env) {
  const allBtn  = document.querySelector('.azure-env-filter[data-env="ALL"]');
  const clicked = document.querySelector(`.azure-env-filter[data-env="${env}"]`);
  const wasActive = clicked?.classList.contains("az-active") && env !== "ALL";
  document.querySelectorAll('.azure-env-filter').forEach(b => b.classList.remove("az-active"));
  if (env === "ALL" || wasActive) {
    allBtn?.classList.add("az-active");
    _activeVmFilters.env = "ALL";
  } else {
    clicked?.classList.add("az-active");
    _activeVmFilters.env = env;
  }
  _applyVmFilters();
}

/* ── Type filter — multi-select ── */
function azureFilterType(type) {
  const allBtn = document.querySelector('.azure-type-filter[data-type="ALL"]');
  const typeBtns = document.querySelectorAll('.azure-type-filter:not([data-type="ALL"])');

  if (type === "ALL") {
    typeBtns.forEach(b => b.classList.remove("az-active"));
    allBtn?.classList.add("az-active");
  } else {
    const btn = document.querySelector(`.azure-type-filter[data-type="${type}"]`);
    btn?.classList.toggle("az-active");
    allBtn?.classList.remove("az-active");
    const anyActive = document.querySelector('.azure-type-filter:not([data-type="ALL"]).az-active');
    if (!anyActive) allBtn?.classList.add("az-active");
  }
  _applyVmFilters();
}

/* ── Step 2: Fetch metrics for selected VMs ── */
let _lastFetchedVmIds = [];  // Track for re-fetch with different duration

async function runAzureFetch() {
  const btn      = document.getElementById("azure-fetch-btn");
  const statusEl = document.getElementById("azure-fetch-status");
  const hours    = parseInt(document.getElementById("azure-modal-hours")?.value || "24");

  // Collect selected VM metadata from persistent selection set
  const selectedIds = [..._selectedVmIds];
  const selectedVms = _discoveredVMs.filter(v => _selectedVmIds.has(v.resource_id));

  if (!selectedIds.length) {
    if (statusEl) { statusEl.textContent = "Select at least one VM to fetch metrics for."; statusEl.classList.remove("hidden"); statusEl.className = "text-xs text-amber-400"; }
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = `Fetching ${selectedIds.length} VMs…`; }
  if (statusEl) { statusEl.textContent = `Pulling last ${hours}h of CPU / Memory / Disk metrics for ${selectedIds.length} VMs…`; statusEl.classList.remove("hidden"); statusEl.className = "text-xs text-Cmuted"; }

  try {
    const body = { hours_back: hours, vm_ids: selectedIds, vm_meta: selectedVms.length === selectedIds.length ? selectedVms : undefined };
    const payload = await _fetchAzureWithProgress(body, btn, statusEl);
    if (!payload) return; // error already handled

    // Feed into the same resource review rendering pipeline
    _lastFetchedVmIds = selectedIds;
    window.appData.resource = payload;
    window.appData.servers  = payload.servers || [];
    window.appData.upload   = payload;
    window._execCache = null;
    _markSessionActive();  // track session boundary
    closeAzureModal();
    setActiveView("resource");

    const rBody = document.getElementById("resource-review-body");
    const rEmpty = document.getElementById("resource-empty");
    if (rBody)  rBody.classList.remove("hidden");
    if (rEmpty) rEmpty.classList.add("hidden");

    // Show the duration picker on the Resource Review page
    const durPicker = document.getElementById("resource-duration-picker");
    if (durPicker) { durPicker.classList.remove("hidden"); durPicker.value = String(hours); }

    renderResourceReview(payload);
    // Defer findings — let browser paint resource view first
    setTimeout(() => { triggerGenerateFindings().catch(() => {}); }, 100);

    const k = payload.kpis || {};
    const elapsed = payload.fetch_time_seconds ? ` (${payload.fetch_time_seconds}s)` : "";
    toast("success", `Azure: ${payload.vm_count || k.total_servers || "?"} VMs fetched`,
          `Grade ${k.fleet_grade || "?"} · Last ${hours}h average${elapsed}`);

  } catch (err) {
    if (statusEl) { statusEl.textContent = `❌ Network error: ${err?.message || err}`; statusEl.className = "text-xs text-red-400"; }
    _handleFetchError(err);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Fetch Metrics"; }
  }
}

/**
 * Shared SSE streaming helper — sends POST to /api/azure/fetch-resources-stream,
 * reads progress events, updates UI, returns final payload or null on error.
 */
async function _fetchAzureWithProgress(body, btn, statusEl) {
  // 5-minute timeout for the entire SSE stream — prevents infinite hang
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 300_000);

  try {
  const res = await fetch("/api/azure/fetch-resources-stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: controller.signal,
  });

  if (!res.ok) {
    const text = await res.text();
    let detail;
    try { detail = JSON.parse(text).detail; } catch { detail = text; }
    if (statusEl) { statusEl.textContent = `❌ ${detail || `HTTP ${res.status}`}`; statusEl.className = "text-xs text-red-400"; }
    toast("error", "Azure fetch failed", detail || `HTTP ${res.status}`);
    return null;
  }

  // Read SSE stream
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let payload = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Parse SSE events from buffer
    const lines = buffer.split("\n");
    buffer = lines.pop() || ""; // keep incomplete line

    let eventType = "";
    let dataStr = "";
    for (const line of lines) {
      if (line.startsWith("event: ")) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith("data: ")) {
        dataStr = line.slice(6);
        try {
          const data = JSON.parse(dataStr);
          if (eventType === "progress") {
            const pct = data.total > 0 ? Math.round((data.done / data.total) * 100) : 0;
            const msg = `${data.phase}… ${data.done}/${data.total} (${pct}%)`;
            if (statusEl) { statusEl.textContent = msg; }
            if (btn && data.total > 0) { btn.textContent = `Fetching… ${pct}%`; }
          } else if (eventType === "result") {
            payload = data;
          } else if (eventType === "error") {
            if (statusEl) { statusEl.textContent = `❌ ${data.detail}`; statusEl.className = "text-xs text-red-400"; }
            toast("error", "Azure fetch failed", data.detail);
            return null;
          }
        } catch { /* skip malformed */ }
        eventType = "";
        dataStr = "";
      }
    }
  }

  return payload;
  } catch (err) {
    if (err.name === "AbortError") {
      if (statusEl) { statusEl.textContent = "❌ Timeout — Azure took too long (5 min limit)"; statusEl.className = "text-xs text-red-400"; }
      toast("error", "Azure timeout", "The request took longer than 5 minutes. Try fewer VMs or a shorter time range.");
      return null;
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

/* ── Re-fetch with different duration from Resource Review page ── */
async function azureRefetchDuration(hours) {
  if (!_lastFetchedVmIds.length) {
    toast("info", "No VMs cached", "Open Azure Monitor modal and fetch VMs first.");
    return;
  }
  const h = parseInt(hours) || 24;
  toast("info", `Re-fetching ${_lastFetchedVmIds.length} VMs`, `Pulling last ${h}h metrics…`);

  try {
    const payload = await _fetchAzureWithProgress(
      { hours_back: h, vm_ids: _lastFetchedVmIds },
      null, null
    );
    if (!payload) { toast("error", "Re-fetch failed", "See console for details"); return; }

    window.appData.resource = payload;
    window.appData.servers  = payload.servers || [];
    window.appData.upload   = payload;
    window._execCache = null;
    _markSessionActive();  // track session boundary
    renderResourceReview(payload);
    // Defer findings — let browser paint resource view first
    setTimeout(() => { triggerGenerateFindings().catch(() => {}); }, 100);

    const k = payload.kpis || {};
    const elapsed = payload.fetch_time_seconds ? ` (${payload.fetch_time_seconds}s)` : "";
    toast("success", `Azure: ${payload.vm_count || k.total_servers || "?"} VMs refreshed`,
          `Grade ${k.fleet_grade || "?"} · Last ${h}h average${elapsed}`);
  } catch (err) {
    toast("error", "Re-fetch error", err?.message || String(err));
  }
}


// ═══════════════════════════════════════════════════════════════
//  SLA MATRIX
// ═══════════════════════════════════════════════════════════════

function initSlaModeSelect() {
  const sel = document.getElementById("sla-mode-select");
  const customField = document.getElementById("sla-custom-field");
  if (!sel || !customField) return;
  sel.addEventListener("change", () => {
    customField.classList.toggle("hidden", sel.value !== "custom");
  });
}

// ══════════════════════════════════════════════════════════════════════════════
//  SLA COMMITMENTS PANEL — shows Tier 1 (BatchSLA) + Tier 2 (SOW) in SLA tab
// ══════════════════════════════════════════════════════════════════════════════

function _renderSlaCommitmentsPanel() {
  const panel = document.getElementById("sla-commitments-panel");
  if (!panel) return;

  const sowContract  = window.appData?.sowContract || {};
  const batchSlaInfo = window.appData?.batchSlaInfo  || {};
  const slaW         = sowContract.sla_windows || {};
  const workflows    = batchSlaInfo.workflows  || [];

  const hasSOW    = Object.keys(slaW).length > 0;
  const hasBatch  = workflows.length > 0;
  const hasManual = Object.values(slaW).some(e => e?.source === "MANUAL");

  // Restore manual override ACTIVE badge if manual values are stored in appData
  if (hasManual) {
    const badge = document.getElementById("sla-manual-active-badge");
    if (badge) badge.classList.remove("hidden");
    // Auto-open the manual panel body so user sees what's active
    const body    = document.getElementById("sla-manual-body");
    const chevron = document.getElementById("sla-manual-chevron");
    if (body?.classList.contains("hidden")) {
      body.classList.remove("hidden");
      if (chevron) chevron.style.transform = "rotate(180deg)";
    }
    // Restore field values + SET tags
    const fieldMap = { DAILY: "daily", WEEKLY: "weekly", MONTHLY: "monthly" };
    Object.entries(fieldMap).forEach(([k, t]) => {
      if (slaW[k]?.source === "MANUAL") {
        const inp = document.getElementById(`sla-manual-${t}`);
        const tag = document.getElementById(`sla-manual-${t}-tag`);
        if (inp && !inp.value) inp.value = slaW[k].limit_hours;
        if (tag) tag.classList.remove("hidden");
      }
    });
  }

  if (!hasSOW && !hasBatch) return; // nothing to show in commitments panel
  panel.classList.remove("hidden");

  // ── Tier 2: SOW windows ──────────────────────────────────────────────
  const sowEl = document.getElementById("sla-sow-windows-rows");
  if (sowEl) {
    if (hasSOW) {
      const slaColors = { DAILY: "Ccyan", WEEKLY: "Cblue", MONTHLY: "Cpurple", BIWEEKLY: "Cteal" };
      sowEl.innerHTML = Object.entries(slaW).map(([btype, entry]) => {
        const hrs    = entry.limit_hours ?? entry;
        const col    = slaColors[btype] || "Ccyan";
        const isMan  = entry?.source === "MANUAL";
        const badgeCls = isMan
          ? "bg-Camber/15 border-Camber/40 text-Camber"
          : `bg-${col}/15 border-${col}/30 text-${col}`;
        const badgeTxt = isMan ? "⚡ MANUAL" : "Tier 2 · SOW";
        return `<div class="flex items-center justify-between rounded-lg border border-${col}/20 bg-${col}/5 px-3 py-2">
          <div class="flex items-center gap-2">
            <span class="text-[10px] font-bold uppercase text-${col}">${_esc(btype)}</span>
            <span class="text-[10px] text-Cmuted">window ceiling</span>
          </div>
          <div class="flex items-center gap-3">
            <span class="text-sm font-extrabold font-mono text-${col}">${hrs}h</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full border font-semibold ${badgeCls}">${badgeTxt}</span>
          </div>
        </div>`;
      }).join("");
      // Also pre-fill manual override fields if empty
      const manD = document.getElementById("sla-manual-daily");
      const manW = document.getElementById("sla-manual-weekly");
      const manM = document.getElementById("sla-manual-monthly");
      if (manD && !manD.value && slaW.DAILY?.limit_hours)   manD.value = slaW.DAILY.limit_hours;
      if (manW && !manW.value && slaW.WEEKLY?.limit_hours)  manW.value = slaW.WEEKLY.limit_hours;
      if (manM && !manM.value && slaW.MONTHLY?.limit_hours) manM.value = slaW.MONTHLY.limit_hours;
    } else {
      sowEl.innerHTML = `<div class="text-[10px] text-Cmuted italic">No SOW contract uploaded yet.
        <button onclick="setActiveView('upload')" class="text-Ccyan hover:underline font-semibold ml-1">Upload SOW →</button>
      </div>`;
    }
  }

  // ── Tier 1: BatchSLA workflows ────────────────────────────────────────
  const batchEl = document.getElementById("sla-batchwf-rows");
  if (batchEl) {
    if (hasBatch) {
      const topWfs  = workflows.slice(0, 10);
      const more    = workflows.length - topWfs.length;
      // ── Use canonical workflow_summary from the backend (single source of truth) ──
      // The backend computes elapsed wall-clock time (max(End_Time) - min(Start_Time))
      // and resolves SLA with Tier 1→2→3 priority, storing results per sub_application.
      // We cross-reference each XLSX workflow row against workflow_summary by normalized name.
      const _normWf = (n) => {
        const pfxs = ["prod_", "test_", "uat_", "dev_", "stg_"];
        let s = (n || "").toLowerCase().trim();
        for (const p of pfxs) { if (s.startsWith(p)) { s = s.slice(p.length); break; } }
        return s;
      };

      // Build canonical lookup: normalized_workflow_key → workflow_summary entry
      // Keys stored lowercase so substring matching against _normWf() output is case-insensitive.
      const canonicalMap = {};
      for (const wfRow of (window.appData?.slaMatrix?.workflow_summary || [])) {
        const k = (wfRow.workflow_key || _normWf(wfRow.sub_application || "")).toLowerCase().trim();
        if (k) canonicalMap[k] = wfRow;
      }

      // entryOf: return full canonical entry for an XLSX workflow row.
      // Resolution order (generic — works for any customer naming convention):
      //   1. Exact match (after _normWf normalisation)
      //   2. Progressive prefix-strip: try each right-side suffix after splitting on '_'
      //      e.g. "petbarn_daily_db_bckup" → "daily_db_bckup" → "db_bckup"
      //      This handles any CUSTOMER_WORKFLOWNAME or CUSTOMER_ENV_WORKFLOWNAME pattern
      //   3. Substring fallback (canonical keys sorted longest-first to avoid short-key false positives)
      const entryOf = (wf) => {
        const wfNorm = _normWf(wf.workflow || "");
        // 1. Exact
        if (canonicalMap[wfNorm]) return canonicalMap[wfNorm];
        // 2. Progressive prefix-strip: try each suffix starting from longest
        const parts = wfNorm.split("_");
        for (let i = 0; i < parts.length - 1; i++) {
          const suffix = parts.slice(i + 1).join("_");
          if (suffix && canonicalMap[suffix]) return canonicalMap[suffix];
        }
        // 3. Substring fallback — longest canonical key wins (most specific match first)
        const sorted = Object.entries(canonicalMap).sort((a, b) => b[0].length - a[0].length);
        for (const [k, e] of sorted) {
          if (wfNorm && (wfNorm.includes(k) || k.includes(wfNorm))) return e;
        }
        return null;
      };

      // bufOf: canonical first, XLSX last_run_hours_xlsx as provenance-labeled fallback
      // Returns { val: number|null, src: "canonical"|"xlsx_last_run"|"none" }
      const bufOf = (wf) => {
        const entry = entryOf(wf);
        if (entry) {
          return { val: typeof entry.buffer_pct === "number" ? entry.buffer_pct : null,
                   src: "canonical" };
        }
        // Fallback: compute from XLSX last-run data (Start Time → Current end time)
        const rt  = typeof wf.last_run_hours_xlsx === "number" ? wf.last_run_hours_xlsx : null;
        const sla = typeof wf.sla_hours            === "number" ? wf.sla_hours            : null;
        if (rt != null && sla != null && sla > 0) {
          return { val: parseFloat(((sla - rt) / sla * 100).toFixed(2)), src: "xlsx_last_run" };
        }
        return { val: null, src: "none" };
      };

      // statusOf: canonical first, XLSX compliance label as fallback
      // compliance_label() in sla_merger.py uses identical thresholds (60/85/100%)
      const statusOf = (wf) => {
        const entry = entryOf(wf);
        if (entry) return entry.status || "UNKNOWN";
        return wf.compliance || "UNKNOWN";   // pre-computed in sla_merger.py
      };

      // runtimeOf: canonical first, XLSX last_run_hours_xlsx as fallback
      // Returns { val: number|null, src: "canonical"|"xlsx_last_run"|"none" }
      const runtimeOf = (wf) => {
        const entry = entryOf(wf);
        if (entry && typeof entry.runtime_h === "number")
          return { val: entry.runtime_h, src: "canonical" };
        const rt = typeof wf.last_run_hours_xlsx === "number" ? wf.last_run_hours_xlsx : null;
        return { val: rt, src: rt != null ? "xlsx_last_run" : "none" };
      };

      batchEl.innerHTML = `
        <div class="text-[10px] text-Cmuted mb-1.5">
          ${workflows.length} workflow(s) loaded ·
          ${batchSlaInfo.with_explicit_sla || batchSlaInfo.with_sla_count || 0} with explicit SLA
          ${(batchSlaInfo.with_fallback_sla || 0) > 0
            ? ` · <span class="text-Camber font-semibold" title="These workflows have no SLA column in the XLSX — using SOW ceiling or global defaults">${batchSlaInfo.with_fallback_sla} using fallback SLA</span>`
            : ""} ·
          types: <span class="text-Cteal font-semibold">${(batchSlaInfo.batch_types || []).join(", ") || "—"}</span>
          ${Object.keys(canonicalMap).length > 0
            ? `· <span class="text-Cgreen font-semibold">✓ Ctrl-M runtime matched</span>`
            : `· <span class="text-Camber" title="Showing XLSX last-run data. Run SLA Matrix with Ctrl-M upload for live runtime.">⚠ using XLSX last-run — upload Ctrl-M for live data</span>`}
        </div>
        <div class="overflow-x-auto rounded-lg border border-Cborder/40">
          ${(() => {
            // ── SLA Standard vs Matrix comparison notice ──────────────────
            // Shows inline when both batch and SLA matrix are loaded.
            // Compares pe_config defaults with matrix SLA values per workflow.
            const cmp = _buildSlaComparison();
            if (!cmp) return "";
            const pd = window.appData?.batch?.pe_defaults || {};
            const defD = Number(pd.daily_hrs || 6.0).toFixed(1);
            const defW = Number(pd.weekly_hrs || 8.0).toFixed(1);
            const parts = [];
            if (cmp.tighter_than_default > 0) {
              parts.push(`<span class="text-Cred font-semibold">${cmp.tighter_than_default} workflow(s) have tighter-than-default SLA</span>`);
            }
            if (cmp.looser_than_default > 0) {
              parts.push(`<span class="text-Cmuted">${cmp.looser_than_default} workflow(s) have extended SLA window</span>`);
            }
            if (cmp.breach_count > 0) {
              parts.push(`<span class="text-Cred font-bold">${cmp.breach_count} job(s) already breaching contracted SLA</span>`);
            }
            if (cmp.near_breach_count > 0) {
              parts.push(`<span class="text-Camber font-semibold">${cmp.near_breach_count} job(s) consuming &ge;85% of contracted SLA</span>`);
            }
            if (!parts.length && !cmp.has_sla_matrix) {
              return `<div class="text-[9px] text-Camber/80 px-3 py-2 border-b border-Camber/20 bg-Camber/5 flex items-center gap-2">
                <span>&#x1F4D0;</span>
                <span>PE Standard Baseline active &mdash; Daily <strong>${defD}h</strong> &middot; Weekly <strong>${defW}h</strong>. Upload BatchSLA_info.xlsx to compare against per-job contracted targets.</span>
              </div>`;
            }
            const alertClass = (cmp.breach_count > 0 || cmp.tighter_than_default > 0)
              ? "text-Camber/90 bg-Camber/5 border-Camber/20"
              : "text-Cmuted/80 bg-Cbg/40 border-Cborder/20";
            return parts.length > 0 ? `<div class="text-[9px] px-3 py-2 border-b ${alertClass} flex flex-wrap items-center gap-x-3 gap-y-0.5">
              <span class="font-bold text-[8px] uppercase tracking-wider opacity-70">SLA vs Baseline</span>
              <span class="opacity-50">PE default: Daily ${defD}h &middot; Weekly ${defW}h</span>
              <span class="opacity-30">&bull;</span>
              ${parts.join(' <span class="opacity-30">&middot;</span> ')}
              <button onclick="triggerGenerateFindings().catch(()=>{})" class="ml-auto text-[8px] font-semibold text-Cgreen hover:underline cursor-pointer">Push to PE Findings &rarr;</button>
            </div>` : "";
          })()}
          <table class="w-full text-[10px]">
            <thead><tr class="border-b border-Cborder/40 bg-Cbg/60">
              <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">Workflow</th>
              <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">Type</th>
              <th class="text-right py-1.5 px-2 text-Cmuted font-semibold" title="SLA (Expected Completion) — the agreed time window by which this workflow must finish. Source tiers: 1 = XLSX contract · 2 = SOW ceiling · 3 = PE system default. Badge shows which tier was used.">SLA <span class="text-[8px] font-normal opacity-60">(Expected Completion)</span></th>
              <th class="text-right py-1.5 px-2 text-Cmuted font-semibold" title="Ctrl-M peak runtime: worst-case elapsed time per run across the observation period. XLSX tag = last-known run snapshot (not live Ctrl-M data).">Runtime</th>
              <th class="text-right py-1.5 px-2 text-Cmuted font-semibold" title="Buffer % = (SLA − Runtime) ÷ SLA × 100. Negative = breach. Formula shown on hover per row.">Buffer %</th>
              <th class="text-center py-1.5 px-2 text-Cmuted font-semibold" title="OK (>40% buffer) · LONG_JOB (15–40%) · AT_RISK (0–15%) · BREACH (<0%) · SLA_MISSING · RUNTIME_MISSING">Status</th>
            </tr></thead>
            <tbody>
              ${topWfs.map(w => {
                // SLA displayed must match the value actually used for buffer/status.
                // entryOf(w) → canonical (Ctrl-M resolved) entry has entry.sla_h = the tier-resolved SLA.
                // w.sla_hours = XLSX-derived SLA (may differ if backend fell back to SOW before re-upload).
                // Always prefer the canonical sla_h so displayed SLA, buffer, and status are consistent.
                const entry     = entryOf(w);
                const _slaVal   = entry?.sla_h ?? w.sla_hours;
                const _slaSrc   = entry?.sla_source ?? w.sla_source ?? (w.sla_hours != null ? "xlsx" : "none");
                // Source badge: shows WHERE this SLA came from — critical for trustworthiness.
                // batch_sla_xlsx* = BatchSLA_info.xlsx per-workflow   → BATCH (teal)
                // sow_extracted   = SOW contract batch-type ceiling   → SOW   (purple)
                // sla_matrix      = uploaded customer SLA matrix file → CONTRACT (green)
                // global*/assumed = no contract found, system default → DEFAULT (amber warning)
                const _slaBadge = (() => {
                  const s = (_slaSrc || "").toLowerCase();
                  // batch_sla_xlsx* and sla_intelligence_anchor = SLA came from the customer file
                  if (s.startsWith("batch_sla_xlsx") || s === "xlsx" || s === "sla_intelligence_anchor")
                    return `<span class="ml-1 text-[7px] font-bold text-Cgreen bg-Cgreen/10 px-0.5 rounded" title="Source: Customer SLA Matrix file — per-contract window">CONTRACT</span>`;
                  if (s === "sow_extracted")
                    return `<span class="ml-1 text-[7px] font-bold text-Cpurple bg-Cpurple/10 px-0.5 rounded" title="Source: SOW contract batch-type ceiling (no per-workflow SLA in XLSX)">SOW</span>`;
                  if (s === "sla_matrix" || s === "contract")
                    return `<span class="ml-1 text-[7px] font-bold text-Cgreen bg-Cgreen/10 px-0.5 rounded" title="Source: Uploaded customer SLA matrix file">CONTRACT</span>`;
                  if (s === "global_default" || s.startsWith("global") || s === "assumed")
                    return `<span class="ml-1 text-[7px] font-bold text-Camber bg-Camber/10 px-0.5 rounded" title="No contract SLA found — system default used. Upload SLA Matrix to override.">DEFAULT</span>`;
                  if (s && s !== "none")
                    return `<span class="ml-1 text-[7px] font-bold text-Cmuted bg-Cmuted/10 px-0.5 rounded" title="SLA source: ${_esc(_slaSrc)}">${s.replace(/_/g," ").toUpperCase().slice(0,8)}</span>`;
                  return "";
                })();
                const sla       = _slaVal != null
                  ? `<span title="SLA source: ${_esc(_slaSrc)}">${_slaVal}h</span>${_slaBadge}`
                  : `—${_slaBadge}`;
                const status    = statusOf(w);
                const cCol      = status === "BREACH" ? "text-Cred font-bold"
                                : status === "AT_RISK" ? "text-Camber"
                                : status === "LONG_JOB" ? "text-Corange"
                                : status === "OK" ? "text-Cteal"
                                : status === "RUNTIME_MISSING" ? "text-Cmuted"
                                : status === "SLA_MISSING" ? "text-Cpurple"
                                : "text-Cmuted";
                const bufResult = bufOf(w);       // { val, src }
                const rtResult  = runtimeOf(w);   // { val, src }
                const buf       = bufResult.val;
                const rt        = rtResult.val;
                const rtSrc     = rtResult.src;   // "canonical" | "xlsx_last_run" | "none"
                // Runtime cell: tag XLSX-sourced values so user knows provenance (Standard 2)
                const rtStr     = rt != null
                  ? (rtSrc === "xlsx_last_run"
                      ? `<span class="text-Cwhite/50" title="From XLSX BatchSLA_info Current end time − Start Time (last known run). Upload Ctrl-M for live data.">${rt.toFixed(3)}h <span class="text-[8px] text-Camber/80">XLSX</span></span>`
                      : `${rt.toFixed(3)}h`)
                  : "—";
                // Buffer cell: typed failure state, never bare "—" (Standard 5)
                const bufStr    = buf != null
                  ? (buf < 0 ? `${buf.toFixed(1)}%` : `+${buf.toFixed(1)}%`)
                  : (status === "SLA_MISSING"     ? "SLA_MISSING"
                  :  status === "RUNTIME_MISSING" ? "RT_MISSING"
                  :  status === "FAILED"           ? "FAILED"
                  :  "—");
                // Formula tooltip (Standard 8)
                const _rtDisp = rt != null ? rt.toFixed(3) : "?";
                const bufTitle = buf != null
                  ? `(${_slaVal ?? "?"}h SLA − ${_rtDisp}h runtime) ÷ SLA × 100 = ${buf.toFixed(1)}%`
                    + (bufResult.src === "xlsx_last_run" ? " [source: XLSX last-run, not live Ctrl-M]" : "")
                  : (entry?.debug_buffer_reason || status || "no runtime data");
                const bCol    = buf == null ? "text-Cmuted italic text-[9px]"
                              : buf < 0    ? "text-Cred font-bold"
                              : buf < 15   ? "text-Cred font-bold"
                              : buf < 40   ? "text-Camber font-semibold"
                              : "text-Cteal";
                const lowFlag = buf != null && buf < 15
                  ? `<span class="ml-0.5 text-[8px] font-bold text-Cred bg-Cred/10 px-1 rounded">${buf < 0 ? "BREACH" : "LOW"}</span>` : "";
                const name  = w.workflow || w.sub_application || "—";
                const btype = w.batch_type || "—";
                return `<tr class="border-b border-Cborder/20 hover:bg-Ccard/30 ${status === 'BREACH' ? 'bg-Cred/5' : ''}">
                  <td class="py-1.5 px-2 text-Cwhite/80 font-mono truncate max-w-[160px]" title="${_esc(name)}">${_esc(name)}</td>
                  <td class="py-1.5 px-2 text-Cmuted">${_esc(btype)}</td>
                  <td class="py-1.5 px-2 text-right font-mono font-bold text-Cteal">${sla}</td>
                  <td class="py-1.5 px-2 text-right font-mono text-Cwhite/70">${rtStr}</td>
                  <td class="py-1.5 px-2 text-right ${bCol}" title="${_esc(bufTitle)}">${bufStr}${lowFlag}</td>
                  <td class="py-1.5 px-2 text-center font-semibold text-[9px] ${cCol}">${status}</td>
                </tr>`;
              }).join("")}
              ${more > 0 ? `<tr><td colspan="6" class="py-1.5 px-2 text-[10px] text-Cmuted italic text-center">+ ${more} more workflows not shown</td></tr>` : ""}
            </tbody>
          </table>
        </div>
        ${(() => {
          const _at = parseFloat(window.appData?.config?.sla_atrisk_pct  ?? 15);
          const _lj = parseFloat(window.appData?.config?.sla_longjob_pct ?? 40);
          const _bc = topWfs.filter(w => statusOf(w) === 'BREACH').length;
          const _ac = topWfs.filter(w => ['AT_RISK', 'LONG_JOB'].includes(statusOf(w))).length;
          let findingsLine = "";
          if (_bc + _ac > 0) {
            let parts = [];
            if (_bc > 0) parts.push('<span class="text-Cred font-bold">' + _bc + ' BREACH \u2192 critical</span>');
            if (_ac > 0) parts.push('<span class="text-Camber">' + _ac + ' AT_RISK/LONG_JOB \u2192 warning</span>');
            findingsLine = '<div class="mt-1 flex flex-wrap items-center gap-2 text-[9px] text-Cmuted">'
              + '<span>PE Findings impact:</span> ' + parts.join(' \u00b7 ')
              + ' <button onclick="triggerGenerateFindings().catch(()=>{})" class="text-Cgreen font-semibold hover:underline ml-2">Re-push to PE Findings \u2192</button></div>';
          }
          return `
        <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[9px] border-t border-Cborder/20 pt-2">
          <span class="text-Cteal font-bold">OK</span><span class="text-Cmuted">&gt;${_lj}%</span>
          <span class="text-Cmuted/40">\u00b7</span>
          <span class="text-Corange font-bold">LONG_JOB</span><span class="text-Cmuted">${_at}\u2013${_lj}%</span>
          <span class="text-Cmuted/40">\u00b7</span>
          <span class="text-Camber font-bold">AT_RISK</span><span class="text-Cmuted">0\u2013${_at}%</span>
          <span class="text-Cmuted/40">\u00b7</span>
          <span class="text-Cred font-bold">BREACH</span><span class="text-Cmuted">&lt;0%</span>
          <span class="text-Cmuted/30 mx-1">|</span>
          <span class="text-Cmuted">Buffer=(SLA\u2212rt)\u00f7SLA\u00d7100</span>
          <span class="text-Cmuted/30 mx-1">|</span>
          <span class="text-Camber/70 font-semibold">XLSX</span><span class="text-Cmuted">=snapshot only</span>
          <button id="sla-interpret-btn" onclick="_triggerSlaInterpret()" class="ml-auto flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-Cpurple/20 border border-Cpurple/40 text-Cpurple text-[9px] font-semibold hover:bg-Cpurple/30 transition-colors">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-3 h-3"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09Z"/></svg>
            Interpret with AI
          </button>
        </div>
        ${findingsLine}
        <div id="sla-interpret-result" class="hidden mt-2 border-t border-Cpurple/20 pt-2">
          <div class="flex items-center justify-between mb-1">
            <span class="text-[9px] font-bold text-Cpurple uppercase tracking-wider">PE Analysis</span>
            <span id="sla-interpret-model" class="text-[8px] text-Cmuted"></span>
          </div>
          <div id="sla-interpret-text" class="text-[10px] text-Cwhite/70 leading-relaxed whitespace-pre-wrap"></div>
        </div>`;
        })()}` ;

      // ── Developer Debug Expander ─────────────────────────────────────────────
      // Shows exactly why each workflow got the SLA/buffer/status it did.
      // Includes debug columns: join_hit, runtime_source, sla_source, buffer_reason.
      const debugWfRows = Object.values(canonicalMap);
      if (debugWfRows.length > 0) {
        const misses = debugWfRows.filter(r => !r.debug_join_hit);
        const breaches = debugWfRows.filter(r => r.status === "BREACH");
        const debugTableRows = debugWfRows.map(r => {
          const hit = r.debug_join_hit
            ? `<span class="text-Cteal">✓ T1</span>`
            : `<span class="text-Camber" title="${_esc(r.debug_buffer_reason || '')}">✗ ${r.sla_source || '?'}</span>`;
          const buf = typeof r.buffer_pct === "number"
            ? (r.buffer_pct < 0 ? `<span class="text-Cred font-bold">${r.buffer_pct.toFixed(1)}%</span>`
               : r.buffer_pct < 15 ? `<span class="text-Camber">${r.buffer_pct.toFixed(1)}%</span>`
               : `<span class="text-Cteal">${r.buffer_pct.toFixed(1)}%</span>`)
            : `<span class="text-Cmuted">—</span>`;
          const st = r.status === "BREACH" ? `<span class="text-Cred font-bold">BREACH</span>`
                   : r.status === "AT_RISK" ? `<span class="text-Camber">AT_RISK</span>`
                   : r.status === "OK" ? `<span class="text-Cteal">OK</span>`
                   : `<span class="text-Cmuted">${r.status || "?"}</span>`;
          return `<tr class="border-b border-Cborder/10 text-[9px]">
            <td class="py-1 px-1.5 font-mono text-Cwhite/60 max-w-[120px] truncate" title="${_esc(r.sub_application || '')}">${_esc(r.workflow_key || r.sub_application || "—")}</td>
            <td class="py-1 px-1.5 text-Cmuted">${r.debug_normalized_subapp || "—"}</td>
            <td class="py-1 px-1.5 text-center">${hit}</td>
            <td class="py-1 px-1.5 text-right font-mono text-Cwhite/60">${r.runtime_h != null ? r.runtime_h.toFixed(3)+"h" : "—"}</td>
            <td class="py-1 px-1.5 text-right font-mono text-Cteal">${r.sla_h != null ? r.sla_h+"h" : "—"}</td>
            <td class="py-1 px-1.5 text-right">${buf}</td>
            <td class="py-1 px-1.5 text-center">${st}</td>
            <td class="py-1 px-1.5 text-Cmuted max-w-[160px] truncate text-[8px]" title="${_esc(r.debug_buffer_reason || '')}">${_esc(r.debug_runtime_source || "—")}</td>
          </tr>`;
        }).join("");

        const debugEl = document.createElement("details");
        debugEl.className = "mt-2";
        debugEl.innerHTML = `
          <summary class="text-[9px] text-Cmuted cursor-pointer hover:text-Cwhite/60 select-none py-1">
            🔬 SLA Debug — ${debugWfRows.length} workflow(s) resolved
            ${breaches.length > 0 ? `· <span class="text-Cred font-bold">${breaches.length} BREACH</span>` : ""}
            ${misses.length > 0 ? `· <span class="text-Camber">${misses.length} prefix-miss (Tier 2/3 fallback)</span>` : ""}
          </summary>
          <div class="mt-1.5 overflow-x-auto rounded border border-Cborder/30 bg-Cbg/60">
            <table class="w-full text-[9px]">
              <thead><tr class="border-b border-Cborder/40 bg-Cbg">
                <th class="text-left py-1 px-1.5 text-Cmuted">workflow_key</th>
                <th class="text-left py-1 px-1.5 text-Cmuted">normalized</th>
                <th class="text-center py-1 px-1.5 text-Cmuted">Tier</th>
                <th class="text-right py-1 px-1.5 text-Cmuted">runtime</th>
                <th class="text-right py-1 px-1.5 text-Cmuted">SLA</th>
                <th class="text-right py-1 px-1.5 text-Cmuted">buffer</th>
                <th class="text-center py-1 px-1.5 text-Cmuted">status</th>
                <th class="text-left py-1 px-1.5 text-Cmuted">runtime_src</th>
              </tr></thead>
              <tbody>${debugTableRows}</tbody>
            </table>
          </div>`;
        batchEl.appendChild(debugEl);
      }
    } else {
      batchEl.innerHTML = `<div class="text-[10px] text-Cmuted italic">No BatchSLA_info.xlsx uploaded yet.
        <button onclick="setActiveView('upload')" class="text-Cteal hover:underline font-semibold ml-1">Upload BatchSLA →</button>
      </div>`;
    }
  }
}

// ── Manual SLA Override toggle ────────────────────────────────────────────────
window._toggleSlaManual = function() {
  const body    = document.getElementById("sla-manual-body");
  const chevron = document.getElementById("sla-manual-chevron");
  if (!body) return;
  const isOpen = !body.classList.contains("hidden");
  body.classList.toggle("hidden", isOpen);
  if (chevron) chevron.style.transform = isOpen ? "rotate(0deg)" : "rotate(180deg)";
};

// Per-field SET badge on input change
window._slaManualInputChange = function(type, val) {
  const tag = document.getElementById(`sla-manual-${type}-tag`);
  const card = document.getElementById(`sla-manual-${type}-card`);
  const filled = val !== "" && !isNaN(parseFloat(val));
  if (tag)  tag.classList.toggle("hidden", !filled);
  // subtle glow on card when filled
  if (card) {
    const glowMap = { daily: "ring-Cblue/20", weekly: "ring-Cpurple/20", monthly: "ring-Cteal/20" };
    if (filled) card.style.boxShadow = "";
    else card.style.boxShadow = "";
  }
};

// Clear all manual fields + badges + ACTIVE badge
window._clearSlaManual = function() {
  ["daily", "weekly", "monthly"].forEach(t => {
    const inp = document.getElementById(`sla-manual-${t}`);
    const tag = document.getElementById(`sla-manual-${t}-tag`);
    if (inp) inp.value = "";
    if (tag) tag.classList.add("hidden");
  });
  const badge = document.getElementById("sla-manual-active-badge");
  if (badge) badge.classList.add("hidden");
  const msg = document.getElementById("sla-manual-msg");
  if (msg) msg.classList.add("hidden");

  // Wipe MANUAL entries from appData (keep XLSX/SOW sourced ones)
  const wins = window.appData?.sowContract?.sla_windows;
  if (wins) {
    ["DAILY","WEEKLY","MONTHLY"].forEach(k => {
      if (wins[k]?.source === "MANUAL") delete wins[k];
    });
  }
  fetch("/api/sow/sla-windows/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ daily_hrs: null, weekly_hrs: null, monthly_hrs: null }),
  }).catch(() => {});

  _renderSlaCommitmentsPanel();
  toast("info", "Manual overrides cleared", "SLA ceilings reverted to uploaded contract values.");
};

// Apply manual SLA ceiling overrides directly to SOW windows and re-run
function _applySlaManualOverride() {
  const daily   = parseFloat(document.getElementById("sla-manual-daily")?.value   || "");
  const weekly  = parseFloat(document.getElementById("sla-manual-weekly")?.value  || "");
  const monthly = parseFloat(document.getElementById("sla-manual-monthly")?.value || "");

  if (isNaN(daily) && isNaN(weekly) && isNaN(monthly)) {
    toast("warning", "No values entered", "Enter at least one SLA ceiling before applying.");
    return;
  }

  // Patch appData.sowContract.sla_windows with the manual values
  window.appData = window.appData || {};
  window.appData.sowContract = window.appData.sowContract || {};
  window.appData.sowContract.sla_windows = window.appData.sowContract.sla_windows || {};
  if (!isNaN(daily))   window.appData.sowContract.sla_windows.DAILY   = { limit_hours: daily,   source: "MANUAL" };
  if (!isNaN(weekly))  window.appData.sowContract.sla_windows.WEEKLY  = { limit_hours: weekly,  source: "MANUAL" };
  if (!isNaN(monthly)) window.appData.sowContract.sla_windows.MONTHLY = { limit_hours: monthly, source: "MANUAL" };

  // Show ACTIVE badge in header + SET tags per field
  const badge = document.getElementById("sla-manual-active-badge");
  if (badge) badge.classList.remove("hidden");
  ["daily","weekly","monthly"].forEach(t => {
    const v = t === "daily" ? daily : t === "weekly" ? weekly : monthly;
    const tag = document.getElementById(`sla-manual-${t}-tag`);
    if (tag) tag.classList.toggle("hidden", isNaN(v));
  });

  // Persist to backend so SLA matrix resolver picks it up
  fetch("/api/sow/sla-windows/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      daily_hrs:   isNaN(daily)   ? null : daily,
      weekly_hrs:  isNaN(weekly)  ? null : weekly,
      monthly_hrs: isNaN(monthly) ? null : monthly,
    }),
  }).catch(() => {}); // best-effort

  // Re-render commitments panel, then re-run both SLA Matrix + Batch Review
  _renderSlaCommitmentsPanel();
  if (window.appData.batch) {
    (async () => {
      await triggerSlaMatrix();
      await _refreshBatchFromServer("Batch Review updated with manual SLA windows");
    })().catch(() => {});
  }

  const parts = [
    !isNaN(daily)   ? `DAILY = ${daily}h`   : "",
    !isNaN(weekly)  ? `WEEKLY = ${weekly}h`  : "",
    !isNaN(monthly) ? `MONTHLY = ${monthly}h` : "",
  ].filter(Boolean);

  const msg = document.getElementById("sla-manual-msg");
  if (msg) {
    msg.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.5" stroke="currentColor" class="w-4 h-4 shrink-0"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5" /></svg> Applied: ${parts.join(" · ")}`;
    msg.classList.remove("hidden");
    setTimeout(() => msg.classList.add("hidden"), 5000);
  }

  toast("success", "Manual SLA override applied", parts.join(" · ") + " — SLA Matrix recalculating…");
}

// ── SLA Commitments AI Interpretation ────────────────────────────────────────
// Calls POST /api/sla-commitments/interpret which builds a precision PE prompt
// from live workflow_summary (or XLSX fallback) and returns LLM analysis.
async function _triggerSlaInterpret() {
  const btn     = document.getElementById("sla-interpret-btn");
  const result  = document.getElementById("sla-interpret-result");
  const textEl  = document.getElementById("sla-interpret-text");
  const modelEl = document.getElementById("sla-interpret-model");
  if (!btn || !result || !textEl) return;

  btn.disabled = true;
  btn.innerHTML = `<svg class="w-3 h-3 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"/></svg> Analysing…`;

  try {
    const resp = await fetch("/api/sla-commitments/interpret", { method: "POST" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    textEl.textContent  = data.text  || "(no response)";
    if (modelEl) modelEl.textContent = data.model ? `model: ${data.model}` : "";
    result.classList.remove("hidden");
  } catch (err) {
    _handleFetchError(err, "sla-interpret");
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-3 h-3"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09Z"/></svg> Interpret with AI`;
  }
}

async function triggerSlaMatrix() {
  const btn = document.getElementById("sla-run-btn");
  if (btn) { btn.disabled = true; btn.textContent = "Calculating…"; }

  const batchData = window.appData.batch;
  if (!batchData) {
    document.getElementById("sla-empty")?.classList.remove("hidden");
    if (btn) { btn.disabled = false; btn.textContent = "Calculate SLA Matrix"; }
    toast("warning", "No batch data", "Upload a Ctrl-M file on the Upload page first.");
    return;
  }

  // Render commitments panel before calculating (shows Tier 1/2 sources to user)
  _renderSlaCommitmentsPanel();

  // Always call the API — it reads the full job_runs_df from session_cache and
  // uses the latest XLSX/SOW config from config_store.
  // The old short-circuit that returned early when batchData.sla_matrix.job_baselines
  // was present is intentionally removed: it caused customer-specific SLAs from
  // BatchSLA_info.xlsx and SOW to be silently ignored whenever Ctrl-M was uploaded first.

  const mode      = document.getElementById("sla-mode-select")?.value || "daily";
  const customHrs = parseFloat(document.getElementById("sla-custom-hrs")?.value || 6);

  // Use all available job data
  const rowMap = new Map();
  for (const r of [...(batchData.top_jobs || []), ...(batchData.top_breaches || [])]) {
    const k = r.Job_Name || r.job_name || "";
    if (k && !rowMap.has(k)) rowMap.set(k, r);
  }
  const rows = [...rowMap.values()];

  const payload = { rows, sla_mode: mode, sla_hrs: mode === "custom" ? customHrs : 0 };

  try {
    const res  = await fetch("/api/sla-matrix/json", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) { toast("error", "SLA Matrix error", (await res.text()).slice(0, 200)); return; }
    const data = await res.json();
    _renderSlaMatrix(data);
  } catch (err) {
    _handleFetchError(err);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Calculate SLA Matrix"; }
  }
}

function _renderSlaMatrix(data) {
  document.getElementById("sla-empty")?.classList.add("hidden");
  document.getElementById("sla-kpi-row")?.classList.remove("hidden");

  // Use DAY-LEVEL window compliance as the headline — the canonical PE sign-off
  // metric, identical to the Executive Dashboard and PE Findings. It is the % of
  // calendar days on which every in-scope sub-app finished within its window.
  const headlineComp = (data.window_day_compliance_pct != null)
    ? data.window_day_compliance_pct
    : data.compliance_pct;
  const compColor = headlineComp >= 95 ? "text-Cgreen" :
                    headlineComp >= 80 ? "text-Camber" : "text-Cred";
  const compEl = document.getElementById("slak-compliance");
  if (compEl) {
    compEl.textContent = _n(headlineComp).toFixed(1) + "%";
    compEl.className = `text-2xl font-bold ${compColor}`;

  // ── Tooltip: explain exactly what the headline percentage measures ──
  // The headline IS DAY-LEVEL window compliance: calendar days on which EVERY
  // in-scope sub-app finished within its window ÷ total days. Pair-level
  // ((sub_app × day) windows) is shown as a secondary detail line. Job-run
  // compliance is a different metric entirely and is labelled as such.
  const wPairs  = data.window_total_pairs;   // total (sub_app × day) pairs
  const wBPairs = data.window_breach_pairs;  // breaching pairs
  const wDays   = data.window_total_days;    // calendar days
  const wBDays  = data.window_breach_days || 0;

  const tipLines = [];
  if (wDays != null) {
    const passDays = wDays - wBDays;
    tipLines.push(
      `Window compliance (headline): ${passDays}/${wDays} calendar days ALL sub-apps finished within SLA`
        + (wBDays > 0 ? ` · ${wBDays} day(s) had ≥1 sub-app breach` : "")
    );
  }
  if (wPairs != null) {
    const okPairs = wPairs - (wBPairs || 0);
    tipLines.push(
      `Pair detail (secondary): ${okPairs}/${wPairs} (sub-app × day) batch windows within SLA`
    );
  }
  // Job-run compliance is a separate, distinct metric — show it clearly labelled
  const _eligible = (data.total_runs || 0) - (data.failed_runs || 0);
  const _passing  = (data.ok_runs || 0) + (data.long_job_runs || 0) + (data.at_risk_runs || 0);
  const runSuffix = data.failed_runs
    ? `, ${data.failed_runs} FAILED excluded from denominator`
    : "";
  tipLines.push(
    `Job-run compliance (separate metric): ${_passing}/${_eligible} eligible runs pass`
      + ` · formula: (OK+LONG_JOB+AT_RISK) ÷ eligible × 100${runSuffix}`
  );
  compEl.title = tipLines.join("\n");
  }

  // Sub-label: lead with the DAY-LEVEL fraction (matches the headline %), then a
  // clearly-labelled pair-level secondary. If pairs unavailable, show days only.
  const compSubEl = document.getElementById("slak-compliance-sub");
  if (compSubEl && data.window_total_days != null) {
  const wPairs  = data.window_total_pairs;
  const wBPairs = data.window_breach_pairs || 0;
  const wDays   = data.window_total_days;
  const wBDays  = data.window_breach_days || 0;
  const passDays = wDays - wBDays;
  let subText;
  if (wPairs != null) {
    const okPairs = wPairs - wBPairs;
    subText = `${passDays}/${wDays} days all-pass · ${okPairs}/${wPairs} windows`;
  } else {
    subText = `${passDays}/${wDays} days pass · Window`;
  }
  compSubEl.textContent = subText;
  compSubEl.className = `text-[10px] ${wBDays > 0 ? "text-Cred" : "text-Cmuted"}`;
  }

  // Explain the day-level vs job-run distinction when they diverge — reconcile,
  // don't contradict. The window breach basis is the longest CONTIGUOUS batch
  // block (idle gaps excluded), the same SLA-binding window Batch Review uses.
  const compNote = document.getElementById("slak-compliance-note");
  if (compNote) {
    const breachCount = data.breaching_runs || 0;
    const wbDays      = data.window_breach_days || 0;
    const wTot        = data.window_total_days || 0;
    const windowWarnings = data.window_warnings || [];
    let noteText = "";
    if (wbDays > 0 && breachCount === 0) {
      // Job-run pass rate is ~100% yet some days breach the window: on those days
      // the binding sub-app's longest contiguous run exceeded its OWN ceiling,
      // even though every individual job ENDED OK. Same story as Batch Review.
      noteText = `ℹ Every job individually ENDED OK, yet ${wbDays} of ${wTot} day(s) breached the window: `
        + `on those days the binding sub-app's longest CONTIGUOUS run (idle gaps excluded) exceeded its own SLA ceiling. `
        + `This is the same day-level story as the Batch Review tab — open it for the per-day breakdown of which sub-app drove each breach.`;
    }
    if (windowWarnings.length) {
      noteText = noteText ? `${noteText} · ${windowWarnings[0]}` : windowWarnings[0];
    }
    if (noteText) {
      compNote.classList.remove("hidden");
      compNote.textContent = noteText;
    } else {
      compNote.classList.add("hidden");
    }
  }
  setText("slak-total",  String(data.total_runs));

  // ── Story banner headline — reconciled day-level Window SLA (matches Batch Review) ──
  const _bannerEl = document.getElementById("sla-banner-headline");
  if (_bannerEl) {
    _bannerEl.textContent = _n(headlineComp).toFixed(1) + "%";
    _bannerEl.className = `text-4xl font-bold leading-none mt-1 ${compColor}`;
  }
  const _bannerSub = document.getElementById("sla-banner-sub");
  if (_bannerSub) {
    const _wd = data.window_total_days || 0;
    const _wbd = data.window_breach_days || 0;
    _bannerSub.textContent = _wd
      ? `${_wd - _wbd}/${_wd} days all sub-apps within window · ${_wbd} breach day${_wbd === 1 ? "" : "s"}`
      : "";
  }

  // ── Drifting Jobs — runs over their OWN learned baseline (z ≥ 2), still under global SLA ──
  const driftN = (data.outliers || []).length;
  const drEl = document.getElementById("slak-drift");
  if (drEl) { drEl.textContent = String(driftN); drEl.className = `text-2xl font-bold ${driftN > 0 ? "text-Camber" : "text-Cgreen"}`; }

  // ── Tightest Buffer — the single job closest to its SLA ceiling (job-level headroom) ──
  const _bufs = (data.job_summary || [])
    .map((j) => j.buffer_pct)
    .filter((v) => v != null && !Number.isNaN(v));
  const tbEl = document.getElementById("slak-tightbuf");
  if (tbEl) {
    if (_bufs.length) {
      const _minBuf = Math.min(..._bufs);
      tbEl.textContent = _minBuf.toFixed(1) + "%";
      tbEl.className = `text-2xl font-bold ${_minBuf >= 40 ? "text-Cgreen" : _minBuf >= 15 ? "text-Camber" : "text-Cred"}`;
    } else {
      tbEl.textContent = "—";
      tbEl.className = "text-2xl font-bold text-Cmuted";
    }
  }
  setText("slak-limit", _n(data.sla_limit_hrs).toFixed(2) + "h");
  // explicit_sla_matrix = true ONLY when per-job contract rows from an uploaded
  // SLA file were matched. Schedule-type ceilings alone do NOT qualify.
  const isPerJob = data.explicit_sla_matrix === true;
  const limitSubEl = document.getElementById("slak-limit-sub");
  if (limitSubEl) {
    if (isPerJob) {
      limitSubEl.textContent = "SLA file + mode fallback";
      limitSubEl.className   = "text-[10px] mt-0.5 font-semibold text-Cgreen";
    } else {
      limitSubEl.textContent = "Assumed — no SLA file";
      limitSubEl.className   = "text-[10px] mt-0.5 font-semibold text-Camber";
    }
  }

  // ── Determine if BatchSLA or SOW ceilings are active ─────────────────
  const hasBatchSla  = (window.appData?.batchSlaInfo?.workflows?.length || 0) > 0;
  const hasSowWin    = Object.keys(window.appData?.sowContract?.sla_windows || {}).length > 0;
  const hasTierSource = hasBatchSla || hasSowWin;

  // Update "SLA Limit" sub-label to reflect active tier
  if (limitSubEl && !isPerJob) {
    if (hasBatchSla && hasSowWin) {
      limitSubEl.textContent = "BatchSLA (T1) + SOW ceilings (T2)";
      limitSubEl.className   = "text-[10px] mt-0.5 font-semibold text-Ccyan";
    } else if (hasBatchSla) {
      limitSubEl.textContent = "BatchSLA Tier 1 active";
      limitSubEl.className   = "text-[10px] mt-0.5 font-semibold text-Cteal";
    } else if (hasSowWin) {
      limitSubEl.textContent = "SOW Tier 2 ceilings active";
      limitSubEl.className   = "text-[10px] mt-0.5 font-semibold text-Ccyan";
    }
  }

  // Show assumed-SLA warning banner — suppressed when BatchSLA or SOW ceilings are active
  const banner    = document.getElementById("sla-assumed-banner");
  const bannerDtl = document.getElementById("sla-assumed-detail");
  if (banner) {
    if (isPerJob || hasTierSource) {
      banner.classList.add("hidden");
    } else {
      banner.classList.remove("hidden");
      if (bannerDtl) {
        const hasCeilNote = (data.sla_label || "").includes("Schedule");
        const ceilNote = hasCeilNote
          ? "Schedule-type ceilings from the SLA intelligence file are available, but no per-job contract rows were matched for these jobs. "
          : "";
        bannerDtl.textContent =
          `${ceilNote}Unmatched jobs use the assumed ${_n(data.sla_limit_hrs).toFixed(2)}h global ceiling ` +
          `(${data.sla_label || "global mode"}). ` +
          `Upload BatchSLA_info.xlsx (Tier 1) or SOW PDF (Tier 2) to activate contracted SLA ceilings.`;
      }
    }
  }

  const wEl = document.getElementById("slak-worst");
  if (wEl) {
    wEl.textContent = data.worst_job ? `${data.worst_job} (${_n(data.worst_hrs).toFixed(2)}h)` : "—";
    wEl.title = data.worst_job ? `+${_n(data.worst_margin_hrs).toFixed(2)}h over SLA` : "";
  }

  // Breach & At-Risk detail table was removed in the SLA-tab refocus (it duplicated
  // Batch Review's top-breaching table). data.breaches still flows to PE Findings/export.

  // Store for findings engine
  window.appData.slaMatrix = data;
  _markSessionActive();  // track session boundary

  // ── Sync SLA-enriched window compliance back to batch KPIs ──
  // When SLA XLSX is uploaded after batch CSV, the batch_window_compliance
  // in appData.batch.kpis is stale (computed with default SLA ceiling).
  // Update it with the SLA Matrix's authoritative window compliance so
  // Executive Dashboard gauges reflect the correct values.
  if (window.appData.batch?.kpis && data.window_compliance_pct != null) {
    window.appData.batch.kpis.window_compliance_pct = data.window_compliance_pct;
    window.appData.batch.kpis.batch_window_compliance = data.window_compliance_pct;
    window.appData.batch.kpis.window_breach_days = data.window_breach_days || 0;
    window.appData.batch.kpis.window_total_days  = data.window_total_days || 0;
    // Day-level window compliance is the canonical headline — keep it in sync with
    // the SLA Matrix so it reconciles exactly with the breach/total days written above.
    if (data.window_day_compliance_pct != null) {
      window.appData.batch.kpis.window_day_compliance_pct = data.window_day_compliance_pct;
    }
    // Also update SLA ceiling if the matrix provides it; sync global SLA_DAILY_HRS (FIX 6.1)
    if (data.sla_hrs > 0) {
      window.appData.batch.kpis.sla_ceiling = data.sla_hrs;
      window.appData.batch.kpis.daily_limit_hrs = data.sla_hrs;
      SLA_DAILY_HRS = Number(data.sla_hrs) || SLA_DAILY_HRS;
    }
  }
  // Invalidate exec dashboard cache so it recomputes with fresh SLA data
  window._execCache = null;
  window._execCacheHash = null;

  refreshDataStatus();
  refreshAuditContext().catch(() => {});  // update health bar

  // Hardwired cross-pillar cascade — SLA Matrix is now the freshest signal,
  // so refresh PE Findings and Red Flags so they cite the same evidence,
  // then run the Senior PE Consultant for the unified verdict.
  // PE Narrative is triggered inside triggerGenerateFindings(); fire it
  // explicitly too so it refreshes even when findings skip (no batch/resource).
  (async () => {
    try { await triggerGenerateFindings(); } catch (e) {}
    try { await triggerRedFlags();         } catch (e) {}
    try { await triggerPeConsultant();     } catch (e) {}
    try { await triggerPeNarrative();      } catch (e) {}
  })();

  // ── SLA Triage panel ──
  _renderSlaTriage(data);

  // Job Summary (All Jobs) table was removed in the SLA-tab refocus — it echoed
  // Batch Review's per-job rollup. The block below is retained but inert: when
  // #sla-job-wrap is absent (the common case now) the guard skips it entirely.
  // data.job_summary is still stored on window.appData.slaMatrix for Findings/export.
  const jobWrap  = document.getElementById("sla-job-wrap");
  const jobTbody = document.getElementById("sla-job-tbody");
  // Clean any prior summary/toggle blocks so re-renders don't stack
  if (jobWrap) {
    jobWrap.querySelectorAll("[data-job-summary],[data-job-more]").forEach((n) => n.remove());
  }
  const summary100 =
    _n(data.compliance_pct) >= 100 &&
    (data.breaching_runs || 0) === 0 &&
    (data.at_risk_runs   || 0) === 0;

  if (data.job_summary?.length && jobWrap) {
    jobWrap.classList.remove("hidden");
    const tableEl = jobTbody?.closest("table");

    // Show SLA source column header only when per-job SLA file was used
    // ── Standard 2: SLA source always visible regardless of tier ──
    const srcHdr = document.getElementById("sla-src-col-hdr");
    if (srcHdr) srcHdr.classList.remove("hidden");

    if (summary100) {
      // Suppress the table — one-line summary is enough
      if (jobTbody) jobTbody.innerHTML = "";
      if (tableEl) tableEl.classList.add("hidden");
      const line = document.createElement("div");
      line.dataset.jobSummary = "1";
      line.className = "text-[12px] text-Cgreen font-semibold py-3";
      line.textContent =
        `${data.total_runs} runs across ${data.job_summary.length} jobs · 0 breaches · 0 at-risk — all within SLA ceiling.`;
      jobWrap.appendChild(line);
    } else if (jobTbody) {
      if (tableEl) tableEl.classList.remove("hidden");
      const sorted = [...data.job_summary].sort((a, b) => (_n(b.peak_hrs) || 0) - (_n(a.peak_hrs) || 0));
      const MAX_ROWS = 10;
      const visible = sorted.slice(0, MAX_ROWS);
      const overflow = sorted.length - visible.length;

      const rowHtml = (j) => {
        // ── Standard 5: typed failure states — never render NaN% for null buffer ──
        const bufPct = j.buffer_pct;          // null when SLA_MISSING or FAILED
        const bufRsn = j.reason_code;         // "SLA_MISSING" | "FAILED" | null
        const _sl    = _n(j.sla_limit || j.sla_limit_hrs || data.sla_limit_hrs);
        const _pk    = _n(j.peak_hrs);
        // ── Standard 8: every buffer cell must carry source + formula tooltip ──
        const bufTitle = bufPct != null
          ? `formula: (${_sl.toFixed(2)}h SLA − ${_pk.toFixed(2)}h peak) ÷ ${_sl.toFixed(2)}h × 100 = ${bufPct.toFixed(1)}%`
          : (bufRsn === "SLA_MISSING" ? `SLA_MISSING — no SLA resolved for this job (source: ${j.sla_source || "none"})`
            : bufRsn === "FAILED"      ? `FAILED — job did not complete; no SLA classification possible`
            : `No buffer computable — check sla_source and runtime`);
        const bCol = bufPct == null   ? "text-Cmuted italic"
                   : bufPct >= 30     ? "text-Cgreen font-semibold"
                   : bufPct >= 0      ? "text-Camber font-semibold"
                   : "text-Cred font-bold";
        const bufCell = bufPct != null
          ? `<td class="py-2 pr-4 text-right ${bCol}" title="${_esc(bufTitle)}">${bufPct.toFixed(1)}%</td>`
          : `<td class="py-2 pr-4 text-right text-[10px] ${bCol}" title="${_esc(bufTitle)}">${bufRsn || "—"}</td>`;
        const rCol = j.breach_rate > 20 ? "text-Cred"   : j.breach_rate > 0 ? "text-Camber" : "text-Cgreen";
        const slaLimitCell = `<td class="py-2 pr-4 text-right text-Cblue" title="SLA ceiling for this job">${_sl.toFixed(2)}</td>`;
        // ── Standard 2: sla_source always visible — distinguishes Tier 1 / fallback ──
        const src      = j.sla_source || "global";
        const srcColor = src === "sla_matrix"     ? "text-Cgreen"
                       : src === "batch_sla_xlsx" ? "text-Cteal"
                       : src === "sow_extracted"  ? "text-Ccyan"
                       : src === "assumed"        ? "text-Camber"
                       : "text-Cmuted";
        const _srcMap  = { sla_matrix: "SLA File", batch_sla_xlsx: "XLSX T1", sow_extracted: "SOW T2",
                           assumed: "Assumed", global: "Global" };
        const srcCell  = `<td class="py-2 text-right ${srcColor} font-semibold text-[10px]" title="${_esc(src)}">${_srcMap[src] || src}</td>`;
        return `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
          <td class="py-2 pr-4 font-mono text-Cwhite text-[11px]">${_esc(j.job_name)}</td>
          <td class="py-2 pr-4 text-right text-Cmuted" title="${j.runs} runs in dataset">${j.runs}</td>
          <td class="py-2 pr-4 text-right text-Camber font-semibold" title="worst single run">${_pk.toFixed(2)}</td>
          <td class="py-2 pr-4 text-right text-Cmuted" title="mean of ${j.runs} runs">${_n(j.avg_hrs).toFixed(2)}</td>
          ${slaLimitCell}
          ${bufCell}
          <td class="py-2 pr-4 text-right ${j.breach_runs > 0 ? "text-Cred font-bold" : "text-Cgreen"}" title="${j.breach_runs}/${j.runs} runs exceeded SLA ceiling">${j.breach_runs}</td>
          <td class="py-2 pr-4 text-right ${rCol}" title="${j.breach_runs}/${j.runs} × 100">${_n(j.breach_rate).toFixed(1)}%</td>
          ${srcCell}
        </tr>`;
      };
      jobTbody.innerHTML = visible.map(rowHtml).join("");

      if (overflow > 0) {
        const more = document.createElement("div");
        more.dataset.jobMore = "1";
        more.className = "mt-2 flex items-center justify-end";
        more.innerHTML = `<button type="button" class="text-[11px] font-semibold text-Cblue hover:underline">View all ${sorted.length} jobs ▾</button>`;
        const btn = more.querySelector("button");
        let expanded = false;
        btn.addEventListener("click", () => {
          expanded = !expanded;
          jobTbody.innerHTML = (expanded ? sorted : visible).map(rowHtml).join("");
          btn.textContent = expanded ? `Collapse to top ${MAX_ROWS} ▴` : `View all ${sorted.length} jobs ▾`;
        });
        jobWrap.appendChild(more);
      }
    }
  } else {
    if (jobWrap) jobWrap.classList.add("hidden");
  }

  // ── Adaptive per-job baselines (computed from this very file) ──
  _renderSlaBaselines(data);
  _renderSlaOutliers(data);
  _renderSlaResourceLink(data);
}

/** Per-job adaptive baseline table — shows what the dashboard learnt.
 *  Rule 2: cap default render at 5 most-demanding jobs; collapse the rest. */
function _renderSlaBaselines(data) {
  const wrap  = document.getElementById("sla-baseline-wrap");
  const tbody = document.getElementById("sla-baseline-tbody");
  const cnt   = document.getElementById("sla-baseline-count");
  const bls   = data?.job_baselines || null;
  if (!wrap || !tbody) return;
  // Clean prior toggle blocks so re-renders don't stack
  wrap.querySelectorAll("[data-baseline-more]").forEach((n) => n.remove());
  const entries = bls ? Object.entries(bls) : [];
  if (!entries.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  if (cnt) cnt.textContent = `${entries.length} job${entries.length === 1 ? "" : "s"}`;
  // Sort by expected_hrs descending — most demanding jobs first
  entries.sort((a, b) => (b[1].expected_hrs || 0) - (a[1].expected_hrs || 0));

  const MAX_ROWS = 5;
  const visible = entries.slice(0, MAX_ROWS);
  const overflow = entries.length - visible.length;

  const rowHtml = ([job, b]) => {
    const sample = b.sample_size_ok
      ? `<span class="text-Cgreen font-semibold">${b.runs}</span>`
      : `<span class="text-Camber" title="Need ≥ 3 runs for a confident baseline">${b.runs}</span>`;
    return `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
      <td class="py-2 pr-4 font-mono text-Cwhite text-[11px]">${_esc(job)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${b.runs}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_n(b.avg_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_n(b.std_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_n(b.p95_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_n(b.max_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right text-Cpurple font-semibold">${_n(b.expected_hrs).toFixed(2)}</td>
      <td class="py-2 text-center">${sample}</td>
    </tr>`;
  };
  tbody.innerHTML = visible.map(rowHtml).join("");

  if (overflow > 0) {
    const more = document.createElement("div");
    more.dataset.baselineMore = "1";
    more.className = "mt-2 flex items-center justify-end";
    more.innerHTML = `<button type="button" class="text-[11px] font-semibold text-Cblue hover:underline">View all ${entries.length} jobs ▾</button>`;
    const btn = more.querySelector("button");
    let expanded = false;
    btn.addEventListener("click", () => {
      expanded = !expanded;
      tbody.innerHTML = (expanded ? entries : visible).map(rowHtml).join("");
      btn.textContent = expanded ? `Collapse to top ${MAX_ROWS} ▴` : `View all ${entries.length} jobs ▾`;
    });
    wrap.appendChild(more);
  }
}

/** Runs that exceeded the per-job baseline (z ≥ 2) but stayed under global SLA.
 *  Suppression: only render the *critical* outliers by default (z ≥ 3 OR
 *  margin ≥ 25% over expected), then offer a toggle to expand the rest.
 *  Hard cap at top 10 even when expanded was not requested — show all on demand. */
function _renderSlaOutliers(data) {
  const wrap  = document.getElementById("sla-outliers-wrap");
  const tbody = document.getElementById("sla-outliers-tbody");
  const cnt   = document.getElementById("sla-outliers-count");
  const out   = data?.outliers || [];
  if (!wrap || !tbody) return;
  // Clean any prior toggle/summary blocks so re-renders don't stack
  wrap.querySelectorAll("[data-outliers-more],[data-outliers-summary]").forEach((n) => n.remove());
  if (!out.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");

  // Sort by severity (z desc, then margin desc) so the worst is always on top
  const sorted = [...out].sort((a, b) => {
    const za = _n(a.outlier_z), zb = _n(b.outlier_z);
    if (zb !== za) return zb - za;
    return _n(b.expected_margin_hrs) - _n(a.expected_margin_hrs);
  });

  // Critical filter: z ≥ 3 (3-sigma) OR margin ≥ 25% over expected baseline.
  const isCritical = (r) => {
    const z = _n(r.outlier_z);
    const marginPct = _n(r.expected_hrs) > 0
      ? (_n(r.expected_margin_hrs) / _n(r.expected_hrs)) * 100
      : 0;
    return z >= 3 || marginPct >= 25;
  };
  const critical = sorted.filter(isCritical);
  const moderate = sorted.filter((r) => !isCritical(r));

  if (cnt) {
    cnt.textContent = critical.length
      ? `${critical.length} critical · ${out.length} total`
      : `${out.length} run${out.length === 1 ? "" : "s"}`;
  }

  const rowHtml = (r) => {
    const z = _n(r.outlier_z);
    const zCol = z >= 3 ? "text-Cred font-bold" : "text-Camber font-semibold";
    return `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
      <td class="py-2 pr-4 font-mono text-Cwhite text-[11px]">${_esc(r.job_name)}</td>
      <td class="py-2 pr-4 text-Cmuted">${_esc(r.run_date)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_esc(r.start_time)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_esc(r.end_time)}</td>
      <td class="py-2 pr-4 text-right text-Camber font-semibold">${_n(r.run_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right text-Cpurple">${_n(r.expected_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right text-Camber font-semibold">+${_n(r.expected_margin_hrs).toFixed(2)}</td>
      <td class="py-2 text-right ${zCol}">${z.toFixed(1)}</td>
    </tr>`;
  };

  const tableEl = tbody.closest("table");

  if (!critical.length) {
    // Nothing severe enough to act on — collapse the table to one sentence.
    tbody.innerHTML = "";
    if (tableEl) tableEl.classList.add("hidden");
    const line = document.createElement("div");
    line.dataset.outliersSummary = "1";
    line.className = "text-[12px] text-Camber font-semibold py-3";
    line.textContent =
      `${out.length} mild outlier${out.length === 1 ? "" : "s"} (z 2–3, < 25% over baseline) — informational only, no action required.`;
    (tableEl?.parentNode || wrap).insertBefore(line, tableEl);
    return;
  }

  if (tableEl) tableEl.classList.remove("hidden");
  tbody.innerHTML = critical.map(rowHtml).join("");

  if (moderate.length > 0) {
    const more = document.createElement("div");
    more.dataset.outliersMore = "1";
    more.className = "mt-2 flex items-center justify-end";
    more.innerHTML = `<button type="button" class="text-[11px] font-semibold text-Cblue hover:underline">View ${moderate.length} mild outlier${moderate.length === 1 ? "" : "s"} ▾</button>`;
    const btn = more.querySelector("button");
    let expanded = false;
    btn.addEventListener("click", () => {
      expanded = !expanded;
      tbody.innerHTML = (expanded ? sorted : critical).map(rowHtml).join("");
      btn.textContent = expanded
        ? `Hide mild outliers ▴`
        : `View ${moderate.length} mild outlier${moderate.length === 1 ? "" : "s"} ▾`;
    });
    wrap.appendChild(more);
  }
}

/** Resource correlation for breaches + outliers (was the fleet hot at that hour?).
 *  Suppression rules:
 *   - Rule 1: if all rows share one verdict, replace the table with a one-line summary.
 *   - Rule 2: if 2+ verdicts but >15 rows, show top 15 and collapse the rest.
 */
function _renderSlaResourceLink(data) {
  const wrap  = document.getElementById("sla-reslink-wrap");
  const tbody = document.getElementById("sla-reslink-tbody");
  const cnt   = document.getElementById("sla-reslink-count");
  const linked = data?.resource_linked || [];
  if (!wrap || !tbody) return;
  if (!linked.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  if (cnt) cnt.textContent = `${linked.length} run${linked.length === 1 ? "" : "s"}`;

  // Rule 1 — uniform-verdict suppression
  const verdicts = linked.map((r) => r.resource_signal?.verdict || "—");
  const uniqueVerdicts = [...new Set(verdicts)];
  const tableEl = tbody.closest("table");

  // Remove any prior summary block we injected so we don't duplicate
  const priorSummary = wrap.querySelector("[data-reslink-summary]");
  if (priorSummary) priorSummary.remove();
  const priorMore = wrap.querySelector("[data-reslink-more]");
  if (priorMore) priorMore.remove();

  if (uniqueVerdicts.length === 1) {
    const v = uniqueVerdicts[0];
    const tone = v === "RESOURCE_LINK" ? "text-Cred"
              : v === "TIMING_PRESSURE" ? "text-Camber"
              : "text-Cgreen";
    const msg = v === "ISOLATED"
      ? `All ${linked.length} runs are ISOLATED — no fleet pressure detected, no variation worth tabulating.`
      : `All ${linked.length} runs share verdict ${v} — no variation to compare.`;
    tbody.innerHTML = "";
    if (tableEl) tableEl.classList.add("hidden");
    const summary = document.createElement("div");
    summary.dataset.reslinkSummary = "1";
    summary.className = `text-[12px] ${tone} font-semibold py-3`;
    summary.textContent = msg;
    (tableEl?.parentNode || wrap).insertBefore(summary, tableEl);
    return;
  }
  if (tableEl) tableEl.classList.remove("hidden");

  // Sort by verdict severity, then by run_hrs
  const order = { RESOURCE_LINK: 0, TIMING_PRESSURE: 1, ISOLATED: 2 };
  linked.sort((a, b) => {
    const va = order[a.resource_signal?.verdict] ?? 99;
    const vb = order[b.resource_signal?.verdict] ?? 99;
    if (va !== vb) return va - vb;
    return (b.run_hrs || 0) - (a.run_hrs || 0);
  });

  // Rule 2 — row count cap
  const MAX_ROWS = 15;
  const visible = linked.slice(0, MAX_ROWS);
  const overflow = linked.length - visible.length;

  const rowHtml = (r) => {
    const s = r.resource_signal || {};
    const verdictClass = s.verdict === "RESOURCE_LINK" ? "text-Cred font-bold"
                       : s.verdict === "TIMING_PRESSURE" ? "text-Camber font-semibold"
                       : "text-Cmuted";
    const cpuCls = (s.fleet_cpu || 0) >= 80 ? "text-Cred font-bold" : "text-Cmuted";
    const memCls = (s.fleet_mem || 0) >= 80 ? "text-Cred font-bold" : "text-Cmuted";
    const hosts = (s.critical_hosts || []).slice(0, 3).map(_esc).join(", ") || "—";
    return `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
      <td class="py-2 pr-4 font-mono text-Cwhite text-[11px]">${_esc(r.job_name)}</td>
      <td class="py-2 pr-4 text-Cmuted">${_esc(r.run_date)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${r.start_hour ?? "—"}h</td>
      <td class="py-2 pr-4 text-right text-Cwhite font-semibold">${_n(r.run_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${s.hot_hour_jobs ?? 0}</td>
      <td class="py-2 pr-4 text-right ${cpuCls}">${_n(s.fleet_cpu).toFixed(1)}%</td>
      <td class="py-2 pr-4 text-right ${memCls}">${_n(s.fleet_mem).toFixed(1)}%</td>
      <td class="py-2 pr-4 text-Cmuted text-[11px]">${hosts}</td>
      <td class="py-2 text-center"><span class="${verdictClass}">${s.verdict || "—"}</span></td>
    </tr>`;
  };

  tbody.innerHTML = visible.map(rowHtml).join("");

  if (overflow > 0) {
    const more = document.createElement("div");
    more.dataset.reslinkMore = "1";
    more.className = "mt-2 flex items-center justify-end";
    more.innerHTML = `
      <button type="button" class="text-[11px] font-semibold text-Cblue hover:underline">
        View all ${linked.length} rows ▾
      </button>`;
    const btn = more.querySelector("button");
    let expanded = false;
    btn.addEventListener("click", () => {
      expanded = !expanded;
      tbody.innerHTML = (expanded ? linked : visible).map(rowHtml).join("");
      btn.textContent = expanded
        ? `Collapse to top ${MAX_ROWS} ▴`
        : `View all ${linked.length} rows ▾`;
    });
    wrap.appendChild(more);
  }
}

// ── SLA breach "crux" renderer ────────────────────────────────
function _renderSlaBreachCrux(data, detailWrap, tbody, countLabel) {
  if (!detailWrap) return;
  detailWrap.classList.remove("hidden");

  const breachRows = data.breaches.filter((r) => r.status === "BREACH");
  const atRiskRows = data.breaches.filter((r) => r.status === "AT_RISK");

  // Aggregate per job: count breaches/at-risks, find peak runtime + worst date
  const byJob = new Map();
  for (const r of data.breaches) {
    const k = r.job_name || "\u2014";
    const cur = byJob.get(k) || { job: k, sub_app: r.sub_application || "\u2014",
      breach: 0, atrisk: 0, peak: 0, peak_margin: 0, peak_date: "" };
    if (r.status === "BREACH") cur.breach++;
    else if (r.status === "AT_RISK") cur.atrisk++;
    const hrs = _n(r.run_hrs) || 0;
    if (hrs > cur.peak) {
      cur.peak = hrs;
      cur.peak_margin = _n(r.breach_margin_hrs) || 0;
      cur.peak_date = r.run_date || "";
    }
    byJob.set(k, cur);
  }
  const jobs = [...byJob.values()].sort((a, b) =>
    (b.breach - a.breach) || (b.peak_margin - a.peak_margin));
  const topJobs = jobs.slice(0, 5);

  if (countLabel) {
    countLabel.textContent =
      `${breachRows.length} breach · ${atRiskRows.length} at-risk · ${jobs.length} job${jobs.length !== 1 ? "s" : ""} affected`;
  }

  // Crux: one-line headline + per-job pills
  const crux = document.createElement("div");
  crux.dataset.breachCrux = "1";
  crux.className = "mb-3 p-3 rounded-lg bg-Cred/5 border border-Cred/20";
  const worst = topJobs[0];
  const headline = worst
    ? `<span class="text-Cred">▲</span> <span class="font-bold text-Cwhite">${_esc(worst.job)}</span> is the worst offender — ${worst.breach} breach${worst.breach !== 1 ? "es" : ""}, peak <span class="text-Cred font-bold">${worst.peak.toFixed(2)}h</span> (+${worst.peak_margin.toFixed(2)}h over SLA) on ${_esc(worst.peak_date)}.`
    : "";
  const pillsHtml = topJobs.map((j) => {
    const riskPart = j.atrisk ? `<span class="text-Camber">${j.atrisk} risk</span>` : "";
    return `<div class="px-2.5 py-1.5 rounded-md bg-Ccard border border-Cborder text-[11px] flex items-center gap-2">
      <span class="font-mono text-Cwhite">${_esc(j.job)}</span>
      <span class="text-Cmuted">·</span>
      <span class="text-Cred font-bold">${j.breach} breach</span>
      ${riskPart}
      <span class="text-Cmuted">peak ${j.peak.toFixed(2)}h</span>
    </div>`;
  }).join("");
  crux.innerHTML =
    `<div class="text-[12px] text-Cmuted leading-snug mb-2">${headline}</div>` +
    `<div class="flex flex-wrap gap-2">${pillsHtml}</div>`;

  const hdrRow = detailWrap.querySelector("h3")?.parentElement;
  if (hdrRow && hdrRow.parentElement) {
    hdrRow.parentElement.insertBefore(crux, hdrRow.nextSibling);
  } else {
    detailWrap.prepend(crux);
  }

  // Render only the 5 worst rows by default; toggle to expand to full list.
  const tableEl = tbody?.closest("table");
  const tableWrap = tableEl?.parentElement;

  const sortedRows = [...data.breaches].sort((a, b) => {
    const sa = a.status === "BREACH" ? 0 : 1;
    const sb = b.status === "BREACH" ? 0 : 1;
    if (sa !== sb) return sa - sb;
    return (_n(b.breach_margin_hrs) || 0) - (_n(a.breach_margin_hrs) || 0);
  });
  const PREVIEW = 5;
  const previewRows = sortedRows.slice(0, PREVIEW);
  const hiddenCount = sortedRows.length - previewRows.length;

  const rowHtml = (r) => {
    const sColor = r.status === "BREACH" ? "text-Cred font-bold" : "text-Camber font-semibold";
    const mColor = r.breach_margin_hrs > 0 ? "text-Cred font-bold" : "text-Camber";
    const ddBtn = window.deepDiveBtn ? window.deepDiveBtn({
      title:    `Breach: ${r.job_name}`,
      question: `Why did job '${r.job_name}' breach SLA on ${r.run_date}? It ran ${_n(r.run_hrs).toFixed(2)}h vs the ${_n(r.sla_limit_hrs).toFixed(1)}h limit (margin +${_n(r.breach_margin_hrs).toFixed(2)}h). Use get_job_history, get_resource_linked_runs and get_host_metrics to determine the root cause.`,
      scope:    "breach",
      context:  { job_name: r.job_name, run_date: r.run_date, run_hrs: r.run_hrs,
                  sla_limit_hrs: r.sla_limit_hrs, status: r.status },
      label:    "Why",
    }) : "";
    return `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
      <td class="py-2 pr-4 font-mono text-Cwhite text-[11px]">${_esc(r.job_name)}</td>
      <td class="py-2 pr-4 text-Cmuted text-[11px]">${_esc(r.sub_application)}</td>
      <td class="py-2 pr-4 text-Cmuted">${_esc(r.run_date)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_esc(r.start_time)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_esc(r.end_time)}</td>
      <td class="py-2 pr-4 text-right font-semibold text-Camber">${_n(r.run_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right text-Cmuted">${_n(r.sla_limit_hrs).toFixed(2)}</td>
      <td class="py-2 pr-4 text-right ${mColor}">${r.breach_margin_hrs > 0 ? "+" : ""}${_n(r.breach_margin_hrs).toFixed(2)}</td>
      <td class="py-2 text-center"><span class="${sColor}">${r.status}</span> ${ddBtn}</td>
    </tr>`;
  };
  if (tbody) tbody.innerHTML = previewRows.map(rowHtml).join("");

  if (hiddenCount > 0 && tbody) {
    const toggle = document.createElement("div");
    toggle.dataset.breachToggle = "1";
    toggle.className = "mt-2 text-center";
    const btn = document.createElement("button");
    btn.className = "text-[11px] text-Cblue hover:text-Cwhite hover:underline";
    btn.textContent = `Show all ${sortedRows.length} breach/at-risk rows ▾`;
    let expanded = false;
    btn.addEventListener("click", () => {
      expanded = !expanded;
      if (expanded) {
        tbody.innerHTML = sortedRows.map(rowHtml).join("");
        btn.textContent = `Show top ${PREVIEW} only ▴`;
      } else {
        tbody.innerHTML = previewRows.map(rowHtml).join("");
        btn.textContent = `Show all ${sortedRows.length} breach/at-risk rows ▾`;
      }
    });
    toggle.appendChild(btn);
    (tableWrap?.parentElement || detailWrap).appendChild(toggle);
  }
}

// ── SLA Triage — identify low-buffer jobs + unexplained breaches ─────────
// ── SLA breakdown drill-through: show job list for a given status tier ──
function _slaBreakdownDrill(status) {
  const drill = document.getElementById("sla-breakdown-drill");
  const title = document.getElementById("sla-drill-title");
  const tbody = document.getElementById("sla-drill-tbody");
  if (!drill || !tbody) return;

  const data = window._slaData || {};
  // Source: merge breaches list + job_summary (job_summary has rolled-up peak data)
  const labels = { OK: "OK", LONG_JOB: "Long Job", AT_RISK: "At Risk", BREACH: "Breach", FAILED: "Failed" };
  const colors = { OK: THEME.green, LONG_JOB: THEME.blue, AT_RISK: THEME.amber, BREACH: THEME.red, FAILED: THEME.muted };

  // Get per-run rows from breaches (BREACH+AT_RISK+LONG_JOB), for OK/FAILED use job_summary
  let rows = [];
  const st = status.toUpperCase();
  if (["BREACH", "AT_RISK", "LONG_JOB"].includes(st)) {
    rows = (data.breaches || []).filter(r => (r.status || "").toUpperCase() === st);
  } else {
    // For OK / FAILED — show job-level summary rows
    rows = (data.job_summary || []).filter(r => {
      const buf = r.buffer_pct;
      if (st === "FAILED") return (r.fail_count || 0) > 0;
      if (st === "OK")     return buf != null && buf > 40;
      return false;
    });
  }

  const c = colors[st] || THEME.muted;
  if (title) {
    title.textContent = `${labels[st] || st} — ${rows.length} ${rows.length === 1 ? "job" : "jobs"}`;
    title.style.color = c;
  }

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="py-3 px-3 text-center text-Cmuted text-[10px]">No ${labels[st] || st} jobs in this run.</td></tr>`;
  } else {
    tbody.innerHTML = rows.slice(0, 100).map(r => {
      const buf = r.buffer_pct != null ? (+r.buffer_pct).toFixed(2) + "%" : "—";
      const rc  = r.reason_code ? `<span class="text-Camber">${_esc(r.reason_code)}</span>` : "";
      const src = r.sla_source || r.sla_source_note || "—";
      const run = r.run_hrs != null ? (+r.run_hrs).toFixed(3) + "h" : (r.peak_hrs != null ? (+r.peak_hrs).toFixed(3) + "h (peak)" : "—");
      const sla = r.sla_limit_hrs != null ? (+r.sla_limit_hrs).toFixed(2) + "h" : (r.sla_limit != null ? (+r.sla_limit).toFixed(2) + "h" : "—");
      return `<tr class="border-b border-Cborder/20 hover:bg-white/5">
        <td class="py-1 px-2 font-mono text-[10px]" style="color:${c}">${_esc(r.job_name || "?")}</td>
        <td class="py-1 px-2 text-right text-Cmuted text-[10px]">${run}</td>
        <td class="py-1 px-2 text-right text-Cmuted text-[10px]">${sla}</td>
        <td class="py-1 px-2 text-right text-[10px]" style="color:${c}">${buf}</td>
        <td class="py-1 px-2 text-[10px] text-Cmuted">${_esc(r.run_date || "—")}</td>
        <td class="py-1 px-2 text-[10px]">${rc || _esc(src)}</td>
      </tr>`;
    }).join("");
  }

  drill.classList.remove("hidden");
  drill.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function _renderSlaTriage(data) {
  const panel = document.getElementById("sla-triage-panel");
  if (!panel) return;

  const jobSummary    = data.job_summary    || [];
  const breachRows    = data.breaches       || [];
  const resLinked     = data.resource_linked || [];
  const LOW_BUF_THRESH = 20;

  // 1. Low buffer jobs
  const lowBufJobs = jobSummary
    .filter(j => parseFloat(j.buffer_pct ?? 999) < LOW_BUF_THRESH)
    .sort((a, b) => parseFloat(a.buffer_pct ?? 0) - parseFloat(b.buffer_pct ?? 0));

  // 2. Unexplained breaches — breached SLA but no resource link
  const resLinkedSet = new Set(resLinked.map(r => (r.job_name || r.Job_Name || "").toLowerCase()));
  const seenBreach   = new Set();
  const unexplained  = breachRows
    .filter(r => r.status === "BREACH")
    .filter(r => {
      const jn = (r.job_name || "").toLowerCase();
      if (seenBreach.has(jn)) return false;
      seenBreach.add(jn);
      return !resLinkedSet.has(jn);
    });

  const hasLowBuf     = lowBufJobs.length > 0;
  const hasUnexplained = unexplained.length > 0;

  if (!hasLowBuf && !hasUnexplained) {
    panel.classList.remove("hidden");
    document.getElementById("sla-triage-lowbuf")?.classList.add("hidden");
    document.getElementById("sla-triage-unexplained")?.classList.add("hidden");
    document.getElementById("sla-triage-clean")?.classList.remove("hidden");
    return;
  }

  panel.classList.remove("hidden");
  document.getElementById("sla-triage-clean")?.classList.add("hidden");

  // ── Low buffer table ─────────────────────────────────────────────
  const lowBufSection = document.getElementById("sla-triage-lowbuf");
  const lowBufTbody   = document.getElementById("sla-triage-lowbuf-tbody");
  const lowBufCount   = document.getElementById("sla-triage-lowbuf-count");
  if (hasLowBuf && lowBufSection && lowBufTbody) {
    lowBufSection.classList.remove("hidden");
    if (lowBufCount) lowBufCount.textContent = lowBufJobs.length;
    lowBufTbody.innerHTML = lowBufJobs.map(j => {
      const buf    = parseFloat(j.buffer_pct ?? 0);
      const peak   = parseFloat(j.peak_hrs   ?? 0);
      const sla    = parseFloat(j.sla_limit  || j.sla_limit_hrs || 0);
      const brate  = parseFloat(j.breach_rate ?? 0);
      const bufCol = buf < 5  ? "text-Cred font-bold" :
                     buf < 10 ? "text-Cred font-semibold" :
                     buf < 20 ? "text-Camber font-semibold" : "text-Cgreen";
      const risk   = buf < 5  ? "PRIORITY — immediate optimisation needed" :
                     buf < 10 ? "PRIORITY — any production data spike will breach" :
                     "WATCH — prioritise in heat map and PE review";
      const riskCol = buf < 10 ? "text-Cred" : "text-Camber";
      return `<tr class="border-b border-Cborder/20 hover:bg-Ccard/30">
        <td class="py-1.5 px-3 font-mono text-Cwhite text-[10px]">${_esc(j.job_name || j.Job_Name || "?")}</td>
        <td class="py-1.5 px-3 text-right ${bufCol}">${buf.toFixed(1)}%</td>
        <td class="py-1.5 px-3 text-right text-Camber font-mono">${peak.toFixed(2)}</td>
        <td class="py-1.5 px-3 text-right text-Cmuted font-mono">${sla.toFixed(2)}</td>
        <td class="py-1.5 px-3 text-right ${brate > 0 ? "text-Cred" : "text-Cgreen"}">${brate.toFixed(1)}%</td>
        <td class="py-1.5 px-3 text-[10px] ${riskCol} italic">${risk}</td>
      </tr>`;
    }).join("");
  }

  // ── Unexplained breaches table ───────────────────────────────────
  const unexplainedSection = document.getElementById("sla-triage-unexplained");
  const unexplainedTbody   = document.getElementById("sla-triage-unexplained-tbody");
  const unexplainedCount   = document.getElementById("sla-triage-unexplained-count");
  if (hasUnexplained && unexplainedSection && unexplainedTbody) {
    unexplainedSection.classList.remove("hidden");
    if (unexplainedCount) unexplainedCount.textContent = unexplained.length;
    unexplainedTbody.innerHTML = unexplained.map(r => {
      const margin  = parseFloat(r.breach_margin_hrs ?? 0);
      const srcBadge = r.sla_source === "batch_sla_xlsx" ? "BatchSLA" :
                       r.sla_source === "sow_extracted"   ? "SOW Tier2" :
                       r.sla_source === "global"          ? "Global Default" : r.sla_source || "Unknown";
      const srcCol   = r.sla_source === "batch_sla_xlsx" ? "text-Cteal" :
                       r.sla_source === "sow_extracted"   ? "text-Ccyan" :
                       r.sla_source === "global"          ? "text-Camber" : "text-Cmuted";
      const ddBtn = window.deepDiveBtn ? window.deepDiveBtn({
        title:    `Unexplained Breach: ${r.job_name}`,
        question: `Job '${r.job_name}' breached its ${_n(r.sla_limit_hrs).toFixed(1)}h SLA on ${r.run_date} (ran ${_n(r.run_hrs).toFixed(2)}h, +${margin.toFixed(2)}h over). NO correlated infrastructure pressure was detected. Investigate the root cause — check: (1) application logs for that run, (2) data volume changes, (3) lock/wait events in the DB layer, (4) whether the job ran concurrently with another job sharing the same resources. Use all available tools.`,
        scope:    "breach",
        context:  { job_name: r.job_name, run_date: r.run_date, run_hrs: r.run_hrs, sla_limit_hrs: r.sla_limit_hrs },
        label:    "Investigate",
      }) : "";
      return `<tr class="border-b border-Cborder/20 hover:bg-Ccard/30">
        <td class="py-1.5 px-3 font-mono text-Cwhite text-[10px]">${_esc(r.job_name || "?")}</td>
        <td class="py-1.5 px-3 text-Cmuted text-[10px]">${_esc(r.sub_application || "—")}</td>
        <td class="py-1.5 px-3 text-Cmuted text-[10px]">${_esc(r.run_date || "—")}</td>
        <td class="py-1.5 px-3 text-right text-Camber font-mono font-bold">${_n(r.run_hrs).toFixed(2)}</td>
        <td class="py-1.5 px-3 text-right text-Cmuted font-mono">${_n(r.sla_limit_hrs).toFixed(2)}</td>
        <td class="py-1.5 px-3 text-right text-Cred font-bold">+${margin.toFixed(2)}</td>
        <td class="py-1.5 px-3 text-[10px] ${srcCol}">${srcBadge}</td>
        <td class="py-1.5 px-3 text-center">${ddBtn}</td>
      </tr>`;
    }).join("");
  }
}

// ── SLA Matrix graphical charts ──────────────────────────────
let _slaBufferBars = null;  // (compliance donut replaced with breakdown bars)

function _renderSlaCharts(data) {
  const chartWrap = document.getElementById("sla-charts-wrap");
  if (!chartWrap || typeof Chart === "undefined") return;
  chartWrap.classList.remove("hidden");

  // Store all runs for drill-through
  window._slaAllRuns = (data.breaches || []).concat(
    (data.job_summary || []).flatMap ? [] : []
  );
  window._slaData = data;

  // ── 1) Compliance Breakdown — horizontal bars (replaces donut) ────────
  // Formula (generic, customer-agnostic):
  //   buffer_pct = (SLA_h − runtime_h) / SLA_h × 100
  //   OK        → buffer_pct > 40%
  //   LONG_JOB  → 15% < buffer_pct ≤ 40%
  //   AT_RISK   → 0% < buffer_pct ≤ 15%
  //   BREACH    → buffer_pct ≤ 0%
  //   FAILED    → execution failure (not SLA-classified)
  const br        = data.breaching_runs  || 0;
  const ar        = data.at_risk_runs    || 0;
  const lj        = data.long_job_runs   || 0;
  const failed    = data.failed_runs     || 0;
  const ok        = data.ok_runs         || 0;
  const eligible  = br + ar + lj + ok;   // FAILED excluded from compliance denominator
  const total     = eligible + failed;

  const brEl = document.getElementById("sla-breakdown-bars");
  const totEl = document.getElementById("sla-breakdown-total");
  if (totEl) totEl.textContent = `${total} total runs · ${failed} failed`;

  if (brEl) {
    const pct = (n) => eligible > 0 ? (n / eligible * 100).toFixed(1) : "0.0";
    const bar = (n, color, max) => {
      const w = total > 0 ? Math.max(2, n / total * 100) : 0;
      return `<div class="h-2.5 rounded-full" style="width:${w.toFixed(1)}%;background:${color};min-width:${n > 0 ? '2px' : '0'}"></div>`;
    };

    const rows = [
      { label: "OK",        n: ok,     color: THEME.green,  pct: pct(ok),     status: "OK"       },
      { label: "Long Job",  n: lj,     color: THEME.blue,   pct: pct(lj),     status: "LONG_JOB" },
      { label: "At Risk",   n: ar,     color: THEME.amber,  pct: pct(ar),     status: "AT_RISK"  },
      { label: "Breach",    n: br,     color: THEME.red,    pct: pct(br),     status: "BREACH"   },
      { label: "Failed",    n: failed, color: THEME.muted,  pct: failed > 0 ? (failed / total * 100).toFixed(1) : "0.0", status: "FAILED" },
    ];

    brEl.innerHTML = rows.map(r => `
      <div class="flex items-center gap-3 group cursor-pointer hover:bg-white/5 rounded-lg px-1 py-1 -mx-1 transition"
           onclick="_slaBreakdownDrill('${r.status}')"
           title="Click to see ${r.label} jobs">
        <span class="w-2.5 h-2.5 rounded-full shrink-0" style="background:${r.color}"></span>
        <span class="text-[11px] font-semibold w-20 shrink-0" style="color:${r.color}">${r.label}</span>
        <div class="flex-1 h-2.5 rounded-full overflow-hidden" style="background:rgba(255,255,255,0.06)">
          ${bar(r.n, r.color)}
        </div>
        <span class="text-[10px] font-bold tabular-nums w-10 text-right" style="color:${r.color}">${r.n}</span>
        <span class="text-[10px] text-Cmuted tabular-nums w-12 text-right">${r.pct}%</span>
        <span class="text-[9px] text-Cmuted/50 group-hover:text-Cmuted transition">›</span>
      </div>`).join("");

    // Formula footnote
    brEl.insertAdjacentHTML("beforeend", `
      <div class="mt-3 pt-2 border-t border-Cborder/30 text-[9px] text-Cmuted/60 leading-relaxed">
        buffer% = (SLA<sub>h</sub> − runtime<sub>h</sub>) / SLA<sub>h</sub> × 100
        &nbsp;·&nbsp; OK >40% · LongJob 15–40% · AtRisk 0–15% · Breach ≤0%
        &nbsp;·&nbsp; Failed = execution error (excluded from %)</div>`);
  }

  // ── 2) Job SLA Buffer horizontal bars ────────────────────────────────
  const barCanvas = document.getElementById("sla-buffer-bars");
  if (barCanvas && data.job_summary?.length) {
    const top12 = data.job_summary.slice(0, 12);
    const names   = top12.map((j) => j.job_name?.length > 22 ? j.job_name.slice(0, 20) + "…" : (j.job_name || "?"));
    const buffers = top12.map((j) => j.buffer_pct != null ? +j.buffer_pct : null);
    const slaSrcs = top12.map((j) => j.sla_source || "global");
    const slaVals = top12.map((j) => j.sla_limit  != null ? +j.sla_limit  : null);
    const peaks   = top12.map((j) => j.peak_hrs   != null ? +j.peak_hrs   : null);

    // Color by status; grey when SLA_MISSING (buffer_pct = null)
    const bColors = buffers.map((b) =>
      b === null ? THEME.muted :
      b < 0   ? THEME.red   :
      b < 15  ? THEME.amber :
      b < 40  ? THEME.blue  : THEME.green
    );
    const displayBuffers = buffers.map((b) => b === null ? 0 : b);

    const bd = {
      labels: names,
      datasets: [{
        label: "SLA Buffer %",
        data: displayBuffers,
        backgroundColor: bColors.map((c) => hexA(c, 0.75)),
        borderColor: bColors, borderWidth: 1,
        borderRadius: 3, barPercentage: 0.75,
      }],
    };

    if (_slaBufferBars) { _slaBufferBars.data = bd; _slaBufferBars.update(); }
    else {
      _slaBufferBars = new Chart(barCanvas, {
        type: "bar", data: bd,
        options: {
          indexAxis: "y",
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: THEME.card,
              borderColor: THEME.border, borderWidth: 1,
              titleColor: THEME.white, bodyColor: THEME.muted,
              callbacks: {
                label: (c) => {
                  const j = top12[c.dataIndex];
                  const buf = j.buffer_pct;
                  const rc  = j.reason_code;
                  if (rc === "SLA_MISSING") return ` Buffer: — (SLA_MISSING — upload BatchSLA XLSX)`;
                  if (buf === null || buf === undefined) return ` Buffer: — (${rc || "unknown"})`;
                  const src = slaSrcs[c.dataIndex];
                  const sla = slaVals[c.dataIndex];
                  const pk  = peaks[c.dataIndex];
                  return [
                    ` Buffer: ${(+buf).toFixed(2)}%`,
                    ` Peak: ${pk != null ? pk.toFixed(3) + "h" : "—"}  SLA: ${sla != null ? sla.toFixed(2) + "h" : "—"}`,
                    ` Source: ${src}`,
                  ];
                },
              },
            },
          },
          scales: {
            x: {
              title: { display: true, text: "Buffer % (positive = within SLA)", color: THEME.muted, font: { size: 10 } },
              ticks: { color: THEME.muted, font: { size: 9 }, callback: (v) => v + "%" },
              grid: { color: hexA(THEME.border, 0.3) },
            },
            y: {
              ticks: { color: THEME.muted, font: { family: '"JetBrains Mono"', size: 9 } },
              grid: { display: false },
            },
          },
        },
      });
    }
  }
}


// ═══════════════════════════════════════════════════════════════
//  UI BENCHMARK COMPARISON
// ═══════════════════════════════════════════════════════════════

function initBenchmarkUploader() {
  const dz    = document.getElementById("bench-drop-zone");
  const input = document.getElementById("bench-file-input");
  if (!dz || !input) return;

  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) uploadBenchmarkFile(f);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dz.classList.add("border-Cpurple","bg-Cpurple/5"); })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dz.classList.remove("border-Cpurple","bg-Cpurple/5"); })
  );
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); e.stopPropagation();
    dz.classList.remove("border-Cpurple", "bg-Cpurple/5");
    const f = e.dataTransfer?.files?.[0];
    if (f) uploadBenchmarkFile(f);
  });
}

async function uploadBenchmarkFile(file) {
  const statusEl = document.getElementById("bench-status");
  const textEl   = document.getElementById("bench-status-text");
  if (statusEl) statusEl.classList.remove("hidden");
  if (textEl)   textEl.textContent = `Processing ${file.name}…`;

  const threshold = parseFloat(
    document.getElementById("cfg-bench-thresh")?.value ||
    window.appData.config?.benchmark_threshold ||
    10
  );
  const fd = new FormData();
  fd.append("file", file);
  fd.append("threshold", String(threshold));

  try {
    const res  = await fetch("/api/benchmark", { method: "POST", body: fd });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      toast("error", "Benchmark failed", (err.detail || "").slice(0, 200));
      return;
    }
    const data = await res.json();
    const slot = (data.kind === "batch" || data.batch_perf_summary) ? "batch" : "ui";
    if (slot === "batch") window.appData.benchmarkBatch = data;
    else                  window.appData.benchmarkUI    = data;
    _markSessionActive();  // track session boundary
    _mergeBenchmarkSources();
    _benchUpdateZoneBadges();
    refreshDataStatus();
    _renderBenchmark(window.appData.benchmark);
    triggerGenerateFindings().catch(() => {});
    toast("success", "Benchmark loaded", `${data.total_transactions} transactions compared`);
  } catch (err) {
    _handleFetchError(err);
  } finally {
    if (statusEl) statusEl.classList.add("hidden");
  }
}

// ── Benchmark state ───────────────────────────────────────────
let _benchData     = null;
let _benchShowAll  = false;
let _benchRegressionChart = null;

function _benchFmtSec(s) {
  s = Number(s) || 0;
  if (s < 60) return s.toFixed(1) + "s";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return sec ? `${m}m ${sec}s` : `${m}m`;
}

function _benchVolumeBand(r) {
  const recs = r.records;
  if (!recs) return "";
  if (recs < 10000)  return "small";
  if (recs < 100000) return "medium";
  return "large";
}

function _benchResetFilters() {
  const fa = document.getElementById("bench-filter-action");
  const fv = document.getElementById("bench-filter-volume");
  if (fa) fa.value = "";
  if (fv) fv.value = "";
  _benchShowAll = false;
  _benchUpdateShowAllBtn();
  _benchApplyFilter();
}

function _benchToggleShowAll() {
  _benchShowAll = !_benchShowAll;
  _benchUpdateShowAllBtn();
  _benchApplyFilter();
}

// ── Mode toggle: "batch" | "ui" ───────────────────────────────────────────────
// Industrial active state: 2px border-bottom on active tab, signal color badge.
// Switches between batch runtime panel and UI benchmark panel.
function _benchSetMode(mode) {
  const batchPanel = document.getElementById("bench-batch-panel");
  const uiPanel    = document.getElementById("bench-ui-panel");
  const batchTab   = document.getElementById("bench-tab-batch");
  const uiTab      = document.getElementById("bench-tab-ui");

  const activeTabCls   = ["text-Cwhite", "border-Cpurple"];
  const inactiveTabCls = ["text-Cmuted",  "border-transparent"];

  if (mode === "batch") {
    batchPanel?.classList.remove("hidden");
    uiPanel?.classList.add("hidden");
    // Active indicator
    batchTab?.classList.remove(...inactiveTabCls);
    batchTab?.classList.add(...activeTabCls);
    uiTab?.classList.remove(...activeTabCls);
    uiTab?.classList.add(...inactiveTabCls);
  } else {
    uiPanel?.classList.remove("hidden");
    batchPanel?.classList.add("hidden");
    uiTab?.classList.remove(...inactiveTabCls);
    uiTab?.classList.add(...activeTabCls);
    batchTab?.classList.remove(...activeTabCls);
    batchTab?.classList.add(...inactiveTabCls);
  }
}

function _benchUpdateShowAllBtn() {
  const btn = document.getElementById("bench-show-all-btn");
  if (!btn) return;
  if (_benchShowAll) {
    btn.textContent = "Show Critical Only";
    btn.classList.add("border-Cpurple/60", "text-Cwhite");
    btn.classList.remove("text-Cmuted");
  } else {
    btn.textContent = "Show All";
    btn.classList.remove("border-Cpurple/60", "text-Cwhite");
    btn.classList.add("text-Cmuted");
  }
}

function _benchApplyFilter() {
  if (!_benchData) return;
  const actionFilter  = (document.getElementById("bench-filter-action")?.value  || "").toLowerCase();
  const volumeFilter  =  document.getElementById("bench-filter-volume")?.value  || "";
  const allRows = (_benchData.rows || []);

  const filtered = allRows.filter(r => {
    if (actionFilter && (r.action || "").toLowerCase() !== actionFilter) return false;
    if (volumeFilter && _benchVolumeBand(r) !== volumeFilter)             return false;
    if (!_benchShowAll && (r.status === "OK" || r.status === "N/A"))      return false;
    return true;
  });

  _benchRenderTable(filtered, _benchData.threshold_pct, _benchData.batch_perf_summary);
  _benchRenderMiniChart(filtered);
}

function _renderBenchmark(data) {
  _benchData    = data;
  _benchShowAll = false;
  document.getElementById("bench-empty")?.classList.add("hidden");
  document.getElementById("bench-no-data-prompt")?.classList.add("hidden");
  document.getElementById("bench-loaded-chip")?.classList.remove("hidden");
  document.getElementById("bench-kpi-row")?.classList.remove("hidden");

  const isBatchPerf = !!(data.batch_perf_summary);
  const rows = data.rows || [];

  // Loaded label
  const cats = data.categories || [];
  const catLabel = isBatchPerf ? " · batch runtime" : (cats.length > 0 ? ` · ${cats.length} categories` : "");
  const lbl = document.getElementById("bench-loaded-label");
  if (lbl) lbl.textContent = `${data.filename || "Benchmark"} · ${data.total_transactions} ${isBatchPerf ? "jobs" : "transactions"}${catLabel}`;

  // Adapt KPI strip labels by mode
  const totalLbl = document.getElementById("bk-total-label");
  const fourthLbl = document.getElementById("bk-fourth-label");
  if (totalLbl) totalLbl.textContent = isBatchPerf ? "Total Jobs" : "Transactions";
  if (fourthLbl) fourthLbl.textContent = isBatchPerf ? "Net Runtime Δ" : "Concurrent Users";

  // ── BAND A: KPI strip ────────────────────────────────────────
  const withBase = rows.filter(r => r.baseline_sec > 0);
  const okCount  = rows.filter(r => r.status === "OK").length;
  const breachCount = rows.filter(r => r.status === "BREACH").length;
  const watchCount  = rows.filter(r => r.status === "WATCH").length;
  const total    = rows.length;
  const passRate = withBase.length > 0 ? Math.round(okCount / withBase.length * 100) : null;

  const passEl = document.getElementById("bk-pass-rate");
  const passSub = document.getElementById("bk-pass-rate-sub");
  if (passEl) {
    if (passRate !== null) {
      passEl.textContent = passRate + "%";
      passEl.className = `text-2xl font-bold ${passRate >= 90 ? "text-Cgreen" : passRate >= 70 ? "text-Camber" : "text-Cred"}`;
      if (passSub) passSub.textContent = `${okCount}/${withBase.length} pass`;
    } else {
      passEl.textContent = "N/A";
      passEl.className = "text-2xl font-bold text-Cmuted";
      if (passSub) passSub.textContent = "no prior baseline";
    }
  }

  // Worst regression
  const worstRow = [...withBase].sort((a, b) => b.delta_pct - a.delta_pct)[0];
  const worstEl  = document.getElementById("bk-worst-reg");
  const worstName= document.getElementById("bk-worst-reg-name");
  if (worstEl) {
    if (worstRow && worstRow.delta_pct > 0) {
      worstEl.textContent = "+" + worstRow.delta_pct.toFixed(1) + "%";
      worstEl.className = `text-2xl font-bold ${worstRow.status === "BREACH" ? "text-Cred" : "text-Camber"}`;
      if (worstName) { worstName.textContent = (worstRow.action ? worstRow.action + ": " : "") + worstRow.transaction; worstName.title = worstRow.transaction; }
    } else {
      worstEl.textContent = "—";
      worstEl.className   = "text-2xl font-bold text-Cgreen";
      if (worstName) worstName.textContent = "no regressions";
    }
  }

  // Total jobs / transactions
  setText("bk-total", String(total));

  // 4th KPI: Net Runtime for batch mode, Max Concurrent for UI mode
  const concurEl = document.getElementById("bk-max-concurrent");
  if (isBatchPerf && data.batch_perf_summary) {
    const bps = data.batch_perf_summary;
    const netSecs = _n(bps.net_delta_secs);
    const netMin  = Math.abs(netSecs / 60).toFixed(1);
    if (concurEl) {
      concurEl.textContent = (netSecs >= 0 ? "−" : "+") + netMin + " min";
      concurEl.className   = `text-2xl font-bold ${netSecs >= 0 ? "text-Cgreen" : "text-Cred"}`;
    }
    const covSub = document.getElementById("bk-coverage-sub");
    if (covSub) covSub.textContent = netSecs >= 0 ? "saved per run" : "added per run";
  } else {
    const concurs = rows.map(r => r.concurrent_users || 0).filter(v => v > 0);
    const maxConcur = concurs.length > 0 ? Math.max(...concurs) : null;
    if (concurEl) {
      concurEl.textContent = maxConcur != null ? String(maxConcur) : "—";
      concurEl.className   = "text-2xl font-bold text-Cblue";
    }
  }

  // Summary banner
  const bannerEl  = document.getElementById("bench-summary-banner");
  const summaryEl = document.getElementById("bench-summary-text");
  if (bannerEl && summaryEl && data.summary) {
    bannerEl.classList.remove("hidden");
    summaryEl.textContent = data.summary;
    bannerEl.className = `rounded-xl border px-5 py-3 ${breachCount > 0 ? "border-Cred/40 bg-Cred/10" : watchCount > 0 ? "border-Camber/40 bg-Camber/10" : "border-Cgreen/40 bg-Cgreen/10"}`;
  }

  // ── Mode tabs — show both, set badge counts, auto-activate correct mode ──
  // Only show the tab bar when at least one side has real data.
  // Hide the secondary tab when only one source is loaded to avoid the
  // confusing "no data" badge sitting next to live data.
  const hasBatchSlot = isBatchPerf;
  const hasUISlot    = !isBatchPerf && total > 0;
  const tabWrap = document.getElementById("bench-mode-tabs");
  if (tabWrap) tabWrap.classList.remove("hidden");

  // Batch tab badge
  const batchBadge = document.getElementById("bench-tab-batch-badge");
  const batchTab   = document.getElementById("bench-tab-batch");
  if (batchBadge && batchTab) {
    if (hasBatchSlot) {
      batchBadge.textContent = `${total} jobs`;
      const hasReg = (data.batch_perf_summary?.regressions || 0) > 0;
      batchBadge.className = `ml-2 px-1.5 py-0.5 rounded text-[9px] font-extrabold transition-colors duration-150 ${hasReg ? "bg-Cred/20 text-Cred" : "bg-Cgreen/20 text-Cgreen"}`;
      batchTab.classList.remove("hidden");
    } else {
      // No batch data — hide tab entirely; user can upload via Zone D
      batchBadge.textContent = "";
      batchTab.classList.add("hidden");
    }
  }

  // UI tab badge
  const uiBadge = document.getElementById("bench-tab-ui-badge");
  const uiTab   = document.getElementById("bench-tab-ui");
  if (uiBadge && uiTab) {
    if (hasUISlot) {
      uiBadge.textContent = `${total} txns`;
      const hasBreaches = breachCount > 0;
      uiBadge.className = `ml-2 px-1.5 py-0.5 rounded text-[9px] font-extrabold transition-colors duration-150 ${hasBreaches ? "bg-Cred/20 text-Cred" : "bg-Cpurple/20 text-Cpurple"}`;
      uiTab.classList.remove("hidden");
    } else if (hasBatchSlot) {
      // Batch is loaded; UI is not. Show a small "add" hint instead of "no data"
      uiBadge.textContent = "+ add UI data";
      uiBadge.className   = "ml-2 px-1.5 py-0.5 rounded text-[9px] font-extrabold bg-Cpurple/10 text-Cpurple/60 border border-Cpurple/20 transition-colors duration-150";
      uiTab.classList.remove("hidden");
    } else {
      uiBadge.textContent = "";
      uiTab.classList.add("hidden");
    }
  }

  // ── BAND B: render sub-panels then set mode ──────────────────
  _benchUpdateShowAllBtn();
  _benchApplyFilter();

  // ── BAND C: Coverage + Evidence ──────────────────────────────
  _benchRenderCoverage(data);
  _benchRenderEvidence(data.evidence_sentences || []);

  // Sub-panels
  _renderBatchPerfSummary(data);   // was dead code — now wired
  _renderBenchCategories(data);
  _renderBenchFillRate(data);
  _renderBenchObservations(data);
  _renderBenchCorrelation(data.correlation);

  // Auto-activate the mode that has data
  _benchSetMode(isBatchPerf ? "batch" : "ui");
}

/**
 * Render the cross-layer correlation verdict in the benchmark view.
 * Industrial: 2px border, mono score, signal colors. Shows release-readiness
 * score, GO/CONDITIONAL/NO-GO verdict, and systemic-regression callout.
 */
function _renderBenchCorrelation(corr) {
  const host = document.getElementById("bench-correlation-panel");
  if (!host) return;
  if (!corr) { host.classList.add("hidden"); host.innerHTML = ""; return; }

  const V = {
    "GO":          { col: "Cgreen", bd: "border-Cgreen/40", bg: "bg-Cgreen/5",  tag: "RELEASE READY" },
    "CONDITIONAL": { col: "Camber", bd: "border-Camber/40", bg: "bg-Camber/5",  tag: "CONDITIONAL" },
    "NO-GO":       { col: "Cred",   bd: "border-Cred/40",   bg: "bg-Cred/5",    tag: "NOT READY" },
  }[corr.verdict] || { col: "Cmuted", bd: "border-Cborder", bg: "", tag: "" };

  const layerTxt = corr.layers.length === 2
    ? "batch runtime + UI benchmark"
    : corr.layers[0] === "batch" ? "batch runtime only" : "UI benchmark only";

  let systemicHtml = "";
  if (corr.systemic && corr.shared_subsystems.length) {
    const items = corr.shared_subsystems.map(s =>
      `<li class="flex items-start gap-2">
        <span class="text-Cred font-mono text-[10px] mt-0.5">▣</span>
        <span class="text-[11px] text-Cwhite">
          <span class="font-mono font-bold text-Cred">${_esc(s.token)}</span>
          <span class="text-Cmuted"> — batch:</span> ${_esc(s.batch)}
          <span class="text-Cmuted"> · UI:</span> ${_esc(s.ui)}
        </span>
      </li>`).join("");
    systemicHtml = `
      <div class="mt-3 pt-3 border-t border-Cred/30">
        <div class="flex items-center gap-2 mb-2">
          <span class="text-[10px] font-mono font-bold text-Cred uppercase tracking-wider">⚠ Systemic regression</span>
          <span class="text-[10px] text-Cmuted">same subsystem slow in both layers → shared DB/infra root cause</span>
        </div>
        <ul class="space-y-1">${items}</ul>
      </div>`;
  } else if (corr.layers.length === 2) {
    systemicHtml = `
      <div class="mt-3 pt-3 border-t border-Cborder">
        <span class="text-[11px] text-Cgreen font-mono">✓ No systemic overlap</span>
        <span class="text-[11px] text-Cmuted"> — batch and UI regressions are isolated, not a shared root cause.</span>
      </div>`;
  }

  host.classList.remove("hidden");
  host.innerHTML = `
    <div class="rounded-xl border-2 ${V.bd} ${V.bg} px-5 py-4">
      <div class="flex items-center gap-4">
        <div class="shrink-0 text-center">
          <div class="text-3xl font-mono font-extrabold text-${V.col} tabular-nums">${corr.score}</div>
          <div class="text-[9px] font-mono text-Cmuted uppercase tracking-wider">/100</div>
        </div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-${V.col}/15 text-${V.col} border border-${V.col}/30">${V.tag}</span>
            <span class="text-xs font-bold text-Cwhite">Performance Release Readiness</span>
          </div>
          <div class="text-[11px] text-Cmuted font-mono mt-1">
            ${_esc(layerTxt)}
            ${corr.layers.includes("batch") ? ` · batch regr ${corr.batch_regression_rate}%` : ""}
            ${corr.layers.includes("ui") ? ` · UI breach ${corr.ui_breach_rate}%` : ""}
          </div>
        </div>
      </div>
      ${systemicHtml}
    </div>`;
}

function _benchRenderTable(rows, threshold, batchPerfSummary) {
  const tbody = document.getElementById("bench-tbody");
  if (!tbody) return;
  let html = "";

  if (batchPerfSummary && rows.length === 0) {
    // Batch perf mode — show summary line only
    const bp = batchPerfSummary;
    html = `<tr><td colspan="8" class="py-6 text-center text-xs text-Cmuted">
      ${bp.regressions} regression(s) · ${bp.improvements} improvement(s) across ${bp.total_jobs} jobs.
      Use "Show All" to view individual job rows.
    </td></tr>`;
    tbody.innerHTML = html; return;
  }

  if (!rows.length) {
    const msg = _benchShowAll ? "No transactions found." : "No BREACH or WATCH findings. Toggle 'Show All' to view all rows.";
    tbody.innerHTML = `<tr><td colspan="8" class="py-8 text-center">
      <div class="text-xl mb-2">${_benchShowAll ? "📭" : "✅"}</div>
      <div class="text-sm font-semibold text-Cwhite">${_benchShowAll ? "No data" : "All transactions within tolerance"}</div>
      <div class="text-xs text-Cmuted mt-1">${msg}</div>
    </td></tr>`;
    return;
  }

  // Group by category if multiple categories
  const cats = [...new Set(rows.map(r => r.category || "General"))];
  const showCats = cats.length > 1;

  const ST = {
    BREACH: { bg: "bg-Cred/15",   bd: "border-l-2 border-Cred",   badge: "bg-Cred/20 text-Cred",   dot: "🔴" },
    WATCH:  { bg: "bg-Camber/10", bd: "border-l-2 border-Camber", badge: "bg-Camber/20 text-Camber",dot: "🟡" },
    OK:     { bg: "",              bd: "",                          badge: "bg-Cgreen/20 text-Cgreen",dot: "🟢" },
    REFERENCE: { bg: "",           bd: "",                          badge: "bg-Ccyan/20 text-Ccyan",   dot: "🧭" },
    "N/A":  { bg: "",              bd: "",                          badge: "bg-Cmuted/20 text-Cmuted", dot: "⚪" },
  };
  const dCol = (d) => d > (threshold * 2) ? "text-Cred font-bold" : d > threshold ? "text-Camber font-bold" : d < -5 ? "text-Cgreen" : "text-Cmuted";

  cats.forEach(cat => {
    const catRows = rows.filter(r => (r.category || "General") === cat);
    if (showCats) {
      html += `<tr class="bg-Cbg/80"><td colspan="8" class="py-2 px-2">
        <span class="text-[10px] font-bold text-Cpurple uppercase tracking-wider">${_esc(cat)}</span>
        <span class="ml-2 text-[10px] text-Cmuted">${catRows.length} items</span>
      </td></tr>`;
    }
    catRows.forEach(r => {
      const sv  = ST[r.status] || ST["N/A"];
      const d   = _n(r.delta_pct);
      const hasBase = r.baseline_sec > 0;
      const slaFmt = r.sla_sec ? _benchFmtSec(r.sla_sec) : "—";
      const recFmt = r.records ? _n(r.records).toLocaleString() : "—";
      const curSec = _n(r.current_sec);
      const baseDisp = hasBase ? _benchFmtSec(r.baseline_sec) : "—";
      const slaOver  = r.sla_sec && curSec > r.sla_sec;
      html += `<tr class="border-b border-Cborder/30 hover:bg-Ccard/40 ${sv.bg} ${sv.bd}"
               title="${_esc(r.comments || '')}">
        <td class="py-2 pr-3 text-Cwhite font-semibold text-xs">${_esc(r.transaction)}</td>
        <td class="py-2 pr-3 text-center">
          ${r.action ? `<span class="px-1.5 py-0.5 rounded text-[9px] font-bold bg-Cpurple/20 text-Cpurple">${_esc(r.action)}</span>` : '<span class="text-Cmuted">—</span>'}
        </td>
        <td class="py-2 pr-3 text-right font-mono text-xs text-Cmuted">${baseDisp}</td>
        <td class="py-2 pr-3 text-right font-mono text-xs ${slaOver ? "text-Cred font-bold" : "text-Cwhite"}">${_benchFmtSec(curSec)}</td>
        <td class="py-2 pr-3 text-right text-xs ${hasBase ? dCol(d) : "text-Cmuted"}">${hasBase ? (d > 0 ? "+" : "") + d.toFixed(1) + "%" : "—"}</td>
        <td class="py-2 pr-3 text-right font-mono text-xs text-Cmuted">${recFmt}</td>
        <td class="py-2 pr-3 text-right font-mono text-xs ${slaOver ? "text-Cred" : "text-Cmuted"}">${slaFmt}</td>
        <td class="py-2 text-center">
          <span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase ${sv.badge}">${sv.dot} ${r.status}</span>
        </td>
      </tr>`;
    });
  });

  tbody.innerHTML = html;
}

function _benchRenderMiniChart(rows) {
  const panel = document.getElementById("bench-chart-panel");
  const canvas = document.getElementById("bench-regression-chart");
  if (!panel || !canvas) return;

  const withDelta = rows.filter(r => r.baseline_sec > 0 && r.delta_pct > 0)
                        .sort((a, b) => b.delta_pct - a.delta_pct)
                        .slice(0, 5);

  if (!withDelta.length) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");

  const labels = withDelta.map(r => {
    const name = (r.action ? r.action + ": " : "") + r.transaction;
    return name.length > 22 ? name.slice(0, 22) + "…" : name;
  });
  const deltas = withDelta.map(r => r.delta_pct);
  const colors = withDelta.map(r => r.status === "BREACH" ? "rgba(239,68,68,0.75)" : "rgba(245,158,11,0.75)");

  if (_benchRegressionChart) { _benchRegressionChart.destroy(); _benchRegressionChart = null; }
  _benchRegressionChart = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        data: deltas,
        backgroundColor: colors,
        borderRadius: 4,
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label(ctx) {
              const r = withDelta[ctx.dataIndex];
              const parts = [`Δ ${r.delta_pct.toFixed(1)}%`, `${_benchFmtSec(r.baseline_sec)} → ${_benchFmtSec(r.current_sec)}`];
              if (r.records) parts.push(`${r.records.toLocaleString()} records`);
              if (r.concurrent_users) parts.push(`${r.concurrent_users} user(s)`);
              return parts;
            }
          }
        }
      },
      scales: {
        x: {
          ticks: { color: "#6b7280", font: { size: 9 }, callback: v => "+" + v + "%" },
          grid:  { color: "rgba(255,255,255,0.05)" },
        },
        y: { ticks: { color: "#9ca3af", font: { size: 9 } }, grid: { display: false } },
      },
    },
  });
}

function _benchRenderCoverage(data) {
  const panel = document.getElementById("bench-coverage-panel");
  if (!panel) return;
  const cov = data.coverage_summary;
  if (!cov) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");

  // Flows
  const flowsEl = document.getElementById("bench-flows-badges");
  if (flowsEl) {
    flowsEl.innerHTML = (cov.flows || []).map(f =>
      `<span class="px-2 py-0.5 rounded-full text-[9px] font-semibold bg-Cpurple/20 text-Cpurple border border-Cpurple/30">${_esc(f)}</span>`
    ).join("") || '<span class="text-Cmuted text-xs">—</span>';
  }

  // Actions
  const actEl = document.getElementById("bench-actions-badges");
  if (actEl) {
    actEl.innerHTML = (cov.actions || []).map(a =>
      `<span class="px-2 py-0.5 rounded-full text-[9px] font-semibold bg-Cblue/20 text-Cblue border border-Cblue/30">${_esc(a)}</span>`
    ).join("") || '<span class="text-Cmuted text-xs">—</span>';
  }

  // Record volumes
  const recEl = document.getElementById("bench-record-vol");
  if (recEl) {
    if (cov.record_min != null) {
      recEl.innerHTML = `min: ${_n(cov.record_min).toLocaleString()}<br>med: ${_n(cov.record_median).toLocaleString()}<br>max: ${_n(cov.record_max).toLocaleString()}`;
    } else {
      recEl.textContent = "not captured";
    }
  }

  // Concurrency
  const concEl = document.getElementById("bench-concurrency");
  if (concEl) {
    if (cov.concurrent_max != null) {
      concEl.innerHTML = `median: ${cov.concurrent_median}<br>max: ${cov.concurrent_max}`;
    } else {
      concEl.textContent = "not captured";
    }
  }
}

function _benchRenderEvidence(sentences) {
  const el = document.getElementById("bench-evidence-list");
  if (!el) return;
  if (!sentences.length) {
    el.innerHTML = '<div class="text-xs text-Cmuted">No evidence sentences — upload file with Action + Records columns.</div>';
    return;
  }
  el.innerHTML = sentences.map(s => {
    const isBreach = s.includes("SLA BREACH") || s.includes("BREACH threshold");
    const isWatch  = s.includes("watch band");
    const dot = isBreach ? "bg-Cred"   : isWatch ? "bg-Camber" : "bg-Cgreen";
    const tx  = isBreach ? "text-Cred/90" : isWatch ? "text-Camber/90" : "text-Cwhite/70";
    return `<div class="flex items-start gap-2 text-[11px] leading-relaxed">
      <span class="mt-1.5 w-1.5 h-1.5 rounded-full ${dot} shrink-0"></span>
      <span class="${tx}">${_esc(s.replace(/^"|"$/g, ""))}</span>
    </div>`;
  }).join("");
}

function _renderBenchCategories(data) {
  const wrap = document.getElementById("bench-category-cards");
  if (!wrap) return;
  const cats = data.categories || [];
  if (!cats.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  wrap.innerHTML = cats.map((c) => {
    const pct = c.total > 0 ? Math.round(c.passed / c.total * 100) : 0;
    const color = c.degraded > 0 ? "border-Cred/40" : c.failed > 0 ? "border-Camber/40" : "border-Cgreen/40";
    const badge = c.degraded > 0 ? "text-Cred" : "text-Cgreen";
    return `<div class="rounded-xl border ${color} bg-Ccard shadow-kpi p-3 min-w-[180px]">
      <div class="text-[10px] text-Cmuted uppercase tracking-widest font-semibold mb-1 truncate">${_esc(c.name)}</div>
      <div class="flex items-end gap-2">
        <span class="text-lg font-extrabold ${badge}">${pct}%</span>
        <span class="text-[10px] text-Cmuted mb-0.5">${c.passed}/${c.total} passed</span>
      </div>
      ${c.degraded > 0 ? `<div class="text-[10px] text-Cred mt-1">${c.degraded} regression(s)</div>` : ""}
      <div class="text-[10px] text-Cmuted mt-0.5">Avg Δ ${_n(c.avg_delta).toFixed(1)}%</div>
    </div>`;
  }).join("");
}

function _renderBenchFillRate(data) {
  const wrap = document.getElementById("bench-fill-rate");
  if (!wrap) return;
  const fr = data.fill_rate || [];
  if (!fr.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");

  // Group by date
  const dateMap = {};
  const dateOrder = [];
  fr.forEach((e) => {
    const d = e.date || "Unknown";
    if (!dateMap[d]) { dateMap[d] = []; dateOrder.push(d); }
    dateMap[d].push(e);
  });

  let html = `<h3 class="text-sm font-bold text-Cwhite mb-3">Fill Rate Comparison (PROD vs TEST)</h3>
    <div class="overflow-x-auto"><table class="w-full text-xs">
    <thead><tr class="border-b border-Cborder">
      <th class="text-left py-2 pr-4 text-Cmuted font-semibold">Date</th>
      <th class="text-left py-2 pr-4 text-Cmuted font-semibold">Type</th>
      <th class="text-right py-2 pr-4 text-Cmuted font-semibold">PROD %</th>
      <th class="text-right py-2 pr-4 text-Cmuted font-semibold">TEST %</th>
      <th class="text-right py-2 pr-4 text-Cmuted font-semibold">Diff</th>
      <th class="text-center py-2 text-Cmuted font-semibold">Status</th>
    </tr></thead><tbody>`;

  dateOrder.forEach((d) => {
    dateMap[d].forEach((e, i) => {
      const diffColor = Math.abs(e.diff) < 0.5 ? "text-Cgreen" : Math.abs(e.diff) < 1.0 ? "text-Camber" : "text-Cred";
      const stColor = (e.status || "").toLowerCase() === "pass" ? "bg-Cgreen/20 text-Cgreen" : "bg-Cred/20 text-Cred";
      html += `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
        <td class="py-2 pr-4 text-Cmuted">${i === 0 ? _esc(d) : ""}</td>
        <td class="py-2 pr-4 text-Cwhite font-semibold">${_esc(e.type)}</td>
        <td class="py-2 pr-4 text-right font-mono text-Cmuted">${_n(e.prod).toFixed(3)}%</td>
        <td class="py-2 pr-4 text-right font-mono text-Cwhite">${_n(e.test).toFixed(3)}%</td>
        <td class="py-2 pr-4 text-right font-mono ${diffColor}">${_n(e.diff) > 0 ? "+" : ""}${_n(e.diff).toFixed(4)}</td>
        <td class="py-2 text-center"><span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase ${stColor}">${_esc(e.status||"")}</span></td>
      </tr>`;
    });
  });
  html += "</tbody></table></div>";
  wrap.innerHTML = html;
}

function _renderBatchPerfSummary(data) {
  const wrap = document.getElementById("bench-batch-perf");
  if (!wrap) return;
  const bps = data.batch_perf_summary;
  if (!bps) { wrap.classList.add("hidden"); return; }

  const netSecs = _n(bps.net_delta_secs);
  const netMin  = Math.abs(netSecs / 60).toFixed(1);
  const netDir  = netSecs >= 0 ? "saved" : "added";
  const netCol  = netSecs >= 0 ? "text-Cgreen" : "text-Cred";
  const dropped = _n(bps.dropped);
  const newOnly = _n(bps.new_only);
  const suspect = _n(bps.suspect);

  const statCard = (label, value, color, sub) =>
    `<div class="rounded-xl border border-Cborder bg-Cbg/40 p-3 text-center min-w-[130px]">
      <div class="text-[10px] text-Cmuted uppercase tracking-widest font-semibold mb-1">${label}</div>
      <div class="text-xl font-extrabold ${color}">${value}</div>
      ${sub ? `<div class="text-[10px] text-Cmuted mt-0.5">${sub}</div>` : ""}
    </div>`;

  const bpRow = (e, isReg) => {
    const pct = _n(e.delta_pct);
    const pctCol = isReg ? "text-Cred" : "text-Cgreen";
    const sav = _n(e.delta_secs);
    return `<tr class="border-b border-Cborder/30 hover:bg-Ccard/40">
      <td class="py-1.5 pr-3 text-Cwhite text-xs font-semibold truncate max-w-[200px]" title="${_esc(e.job)}">${_esc(e.job)}</td>
      <td class="py-1.5 pr-3 text-right text-Cmuted font-mono text-xs tabular-nums">${_n(e.old_secs).toFixed(0)}s</td>
      <td class="py-1.5 pr-3 text-right font-mono text-xs tabular-nums ${isReg ? "text-Camber" : "text-Cgreen"}">${_n(e.new_secs).toFixed(0)}s</td>
      <td class="py-1.5 text-right font-mono text-xs font-bold tabular-nums ${pctCol}">${pct > 0 ? "+" : ""}${pct.toFixed(0)}%</td>
      <td class="py-1.5 text-right font-mono text-xs tabular-nums ${sav >= 0 ? "text-Cgreen" : "text-Cred"}">${sav >= 0 ? "+" : ""}${sav.toFixed(0)}s</td>
    </tr>`;
  };

  const reg  = bps.top_regressions  || [];
  const impr = bps.top_improvements || [];

  const colHead = `<thead><tr class="border-b border-Cborder">
    <th class="text-left py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">Job</th>
    <th class="text-right py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">Before (s)</th>
    <th class="text-right py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">After (s)</th>
    <th class="text-right py-1.5 text-Cmuted text-[10px] font-semibold">Δ %</th>
    <th class="text-right py-1.5 text-Cmuted text-[10px] font-semibold">Δ sec</th>
  </tr></thead>`;

  let html = `<h3 class="text-sm font-bold text-Cwhite mb-4">Batch Runtime Comparison</h3>
    <div class="flex flex-wrap gap-3 mb-5">
      ${statCard("Total Jobs",   bps.total_jobs,    "text-Cwhite",  `${bps.comparable} comparable`)}
      ${statCard("Regressions",  bps.regressions,   bps.regressions > 0 ? "text-Cred"   : "text-Cgreen", "worse than before")}
      ${statCard("Improvements", bps.improvements,  bps.improvements > 0 ? "text-Cgreen" : "text-Cmuted", "faster than before")}
      ${statCard("No Change",    bps.no_change,     "text-Cmuted",  "within ±threshold")}
      ${suspect > 0 ? statCard("Suspect",  suspect, "text-Camber", "near-instant · no-data?") : ""}
      ${dropped > 0 ? statCard("Not Run",  dropped, "text-Camber", "no data in new env") : ""}
      ${newOnly > 0 ? statCard("New Only",  newOnly, "text-Cmuted", "no prior baseline") : ""}
      ${statCard("Net Runtime",  `${netSecs >= 0 ? "−" : "+"}${netMin} min`, netCol, `${netDir} per run · comparable only`)}
    </div>`;

  // Top regressions / improvements side-by-side
  html += `<div class="grid grid-cols-1 xl:grid-cols-2 gap-4 mb-5">`;
  if (reg.length) {
    html += `<div>
      <div class="text-xs font-bold text-Cred uppercase tracking-wider mb-2">Top Regressions <span class="font-normal text-Cmuted normal-case">(${bps.regressions} total)</span></div>
      <div class="overflow-x-auto"><table class="w-full text-xs">${colHead}<tbody>
        ${reg.map(e => bpRow(e, true)).join("")}
      </tbody></table></div>
    </div>`;
  }
  if (impr.length) {
    html += `<div>
      <div class="text-xs font-bold text-Cgreen uppercase tracking-wider mb-2">Top Improvements <span class="font-normal text-Cmuted normal-case">(${bps.improvements} total)</span></div>
      <div class="overflow-x-auto"><table class="w-full text-xs">${colHead}<tbody>
        ${impr.map(e => bpRow(e, false)).join("")}
      </tbody></table></div>
    </div>`;
  }
  if (!reg.length && !impr.length) {
    html += `<div class="col-span-2 py-6 text-center text-Cmuted text-xs">
      All ${bps.total_jobs} jobs ran within tolerance — no significant regressions or improvements.
    </div>`;
  }
  html += `</div>`;

  // Suspect near-instant collapses — surfaced explicitly so they're never silently
  // banked as "improvements". These need PE review (likely no-data / early-exit).
  // Gap 1: each collapse now carries a CAUSE classification so a PE reviewer can
  // triage at a glance instead of re-opening the raw data for every row.
  const susp = bps.top_suspect || [];
  if (susp.length) {
    const reasonBadge = (e) => {
      const rc = String(e.reason || "").toUpperCase();
      if (rc === "DATA_VOLUME_SUSPECT")
        return `<span class="px-1.5 py-0.5 rounded text-[9px] font-bold bg-Cred/15 text-Cred border border-Cred/40" title="Data-heavy load/extract job (HIST_TRANSFER, EXTRACT_SKU…, IO_SRE) collapsing to seconds — almost certainly a TEST data-volume artifact (empty / stub data), not a tuning win.">DATA-VOLUME</span>`;
      if (rc === "NEEDS_VERIFICATION")
        return `<span class="px-1.5 py-0.5 rounded text-[9px] font-bold bg-Camber/15 text-Camber border border-Camber/40" title="Near-total (≥99%) runtime drop on a non-data job — suspicious; verify before crediting as a genuine improvement.">VERIFY</span>`;
      return `<span class="px-1.5 py-0.5 rounded text-[9px] font-bold bg-Cmuted/10 text-Cmuted border border-Cborder" title="Tripped the collapse gate but is not a known data-heavy class — treat cautiously, may be genuine.">REVIEW</span>`;
    };
    const suspHead = `<thead><tr class="border-b border-Cborder">
      <th class="text-left py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">Job</th>
      <th class="text-right py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">Before (s)</th>
      <th class="text-right py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">After (s)</th>
      <th class="text-right py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">Δ %</th>
      <th class="text-right py-1.5 text-Cmuted text-[10px] font-semibold">Likely cause</th>
    </tr></thead>`;
    const suspRow = (e) => {
      const pct = _n(e.delta_pct);
      return `<tr class="border-b border-Cborder/30 hover:bg-Ccard/40">
        <td class="py-1.5 pr-3 text-Cwhite text-xs font-semibold truncate max-w-[180px]" title="${_esc(e.job)}">${_esc(e.job)}</td>
        <td class="py-1.5 pr-3 text-right text-Cmuted font-mono text-xs tabular-nums">${_n(e.old_secs).toFixed(0)}s</td>
        <td class="py-1.5 pr-3 text-right font-mono text-xs tabular-nums text-Cgreen">${_n(e.new_secs).toFixed(0)}s</td>
        <td class="py-1.5 pr-3 text-right font-mono text-xs font-bold tabular-nums text-Cgreen">${pct > 0 ? "+" : ""}${pct.toFixed(0)}%</td>
        <td class="py-1.5 text-right">${reasonBadge(e)}</td>
      </tr>`;
    };
    const sc = susp.reduce((a, e) => { const k = String(e.reason || "POSSIBLE_GENUINE"); a[k] = (a[k] || 0) + 1; return a; }, {});
    const chip = (lbl, n, cls) => n > 0 ? `<span class="px-1.5 py-0.5 rounded ${cls} text-[9px] font-bold">${n} ${lbl}</span>` : "";
    const rollup = [
      chip("data-volume", sc["DATA_VOLUME_SUSPECT"], "bg-Cred/15 text-Cred border border-Cred/40"),
      chip("verify",      sc["NEEDS_VERIFICATION"],  "bg-Camber/15 text-Camber border border-Camber/40"),
      chip("review",      sc["POSSIBLE_GENUINE"],    "bg-Cmuted/10 text-Cmuted border border-Cborder"),
    ].filter(Boolean).join(" ");
    html += `<div class="mb-5 rounded-xl border border-Camber/40 bg-Camber/5 p-3">
      <div class="flex items-center justify-between flex-wrap gap-2 mb-1">
        <div class="text-xs font-bold text-Camber uppercase tracking-wider">⚠ Suspect Collapses <span class="font-normal text-Cmuted normal-case">(${suspect} total — excluded from improvements & net savings)</span></div>
        ${rollup ? `<div class="flex items-center gap-1.5">${rollup}</div>` : ""}
      </div>
      <div class="text-[10px] text-Cmuted mb-2">Multi-minute jobs that now finish in seconds. <span class="text-Cred font-semibold">DATA-VOLUME</span> = data-heavy load/extract job that almost certainly ran on empty/stub TEST data (not a real win). <span class="text-Camber font-semibold">VERIFY</span> = near-total drop on another job class. <span class="text-Cmuted font-semibold">REVIEW</span> = collapsed but may be genuine. Verify before crediting as improvement.</div>
      <div class="overflow-x-auto"><table class="w-full text-xs">${suspHead}<tbody>
        ${susp.map(suspRow).join("")}
      </tbody></table></div>
    </div>`;
  }

  // ── Gap 6: per-batch-type breakdown (Monthly / Daily / SEQ Daily / Weekly …) ──
  // A single flat regression count hides that a Monthly-batch regression carries
  // far more PE weight than a SEQ-optimizer one. Sourced from
  // batch_perf_summary.by_batch_type so every per-type count reconciles exactly
  // with the headline buckets above (same bucketing logic, server-side).
  const byType = bps.by_batch_type || [];
  if (byType.length > 1) {
    html += `<div class="border-t border-Cborder pt-5">
      <div class="text-xs font-bold text-Cwhite uppercase tracking-wider mb-3">By Batch Type <span class="font-normal text-Cmuted normal-case">— per-window regression breakdown (highest PE impact first)</span></div>
      <div class="overflow-x-auto">
        <table class="w-full text-xs">
          <thead><tr class="border-b border-Cborder">
            <th class="text-left py-2 pr-4 text-Cmuted font-semibold">Batch Type</th>
            <th class="text-right py-2 pr-4 text-Cmuted font-semibold">Jobs</th>
            <th class="text-right py-2 pr-4 text-Cmuted font-semibold">Regressions</th>
            <th class="text-right py-2 pr-4 text-Cmuted font-semibold">Improvements</th>
            <th class="text-right py-2 pr-4 text-Cmuted font-semibold">Suspect</th>
            <th class="text-right py-2 pr-4 text-Cmuted font-semibold">Regr Rate</th>
            <th class="text-right py-2 text-Cmuted font-semibold">Net Runtime</th>
          </tr></thead>
          <tbody>
            ${byType.map(c => {
              const reg = _n(c.regressions);
              const regCol = reg > 0 ? "text-Cred font-bold" : "text-Cmuted";
              const imprCol = _n(c.improvements) > 0 ? "text-Cgreen" : "text-Cmuted";
              const suspCol = _n(c.suspect) > 0 ? "text-Camber" : "text-Cmuted";
              const rate = _n(c.regression_rate);
              const rateCol = rate >= 25 ? "text-Cred" : rate > 0 ? "text-Camber" : "text-Cgreen";
              const net = _n(c.net_delta_secs);
              const netMinT = Math.abs(net / 60).toFixed(1);
              const netCol = net >= 0 ? "text-Cgreen" : "text-Cred";
              return `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
                <td class="py-2 pr-4 text-Cwhite font-semibold">${_esc(c.batch_type)}</td>
                <td class="py-2 pr-4 text-right font-mono tabular-nums text-Cmuted">${_n(c.total_jobs)}</td>
                <td class="py-2 pr-4 text-right font-mono tabular-nums ${regCol}">${reg}</td>
                <td class="py-2 pr-4 text-right font-mono tabular-nums ${imprCol}">${_n(c.improvements)}</td>
                <td class="py-2 pr-4 text-right font-mono tabular-nums ${suspCol}">${_n(c.suspect)}</td>
                <td class="py-2 pr-4 text-right font-mono tabular-nums ${rateCol}">${rate.toFixed(0)}%</td>
                <td class="py-2 text-right font-mono tabular-nums ${netCol}">${net >= 0 ? "−" : "+"}${netMinT}m</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
    </div>`;
  }

  wrap.innerHTML = html;
}

function _renderBenchObservations(data) {
  const wrap = document.getElementById("bench-observations");
  if (!wrap) return;
  const obs = data.observations || [];
  if (!obs.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");

  let html = `<h3 class="text-sm font-bold text-Cwhite mb-3">SIT Observations</h3>
    <div class="overflow-x-auto"><table class="w-full text-xs">
    <thead><tr class="border-b border-Cborder">
      <th class="text-left py-2 pr-4 text-Cmuted font-semibold">Problem</th>
      <th class="text-left py-2 pr-4 text-Cmuted font-semibold">Date</th>
      <th class="text-left py-2 pr-4 text-Cmuted font-semibold">Root Cause</th>
      <th class="text-left py-2 pr-4 text-Cmuted font-semibold">Status</th>
      <th class="text-left py-2 pr-4 text-Cmuted font-semibold">Action</th>
      <th class="text-left py-2 text-Cmuted font-semibold">Owner</th>
    </tr></thead><tbody>`;
  obs.forEach((o) => {
    const stColor = (o.status || "").toLowerCase() === "closed" ? "text-Cgreen" : "text-Camber";
    html += `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
      <td class="py-2 pr-4 text-Cwhite font-semibold">${_esc(o.problem || "")}</td>
      <td class="py-2 pr-4 text-Cmuted">${_esc(o.date || "")}</td>
      <td class="py-2 pr-4 text-Cmuted">${_esc(o["why_(root_cause)"] || o.root_cause || "")}</td>
      <td class="py-2 pr-4 ${stColor} font-semibold">${_esc(o.status || "")}</td>
      <td class="py-2 pr-4 text-Cmuted text-[11px]">${_esc(o.corrective_action_taken || "")}</td>
      <td class="py-2 text-Cmuted">${_esc(o.owner || "")}</td>
    </tr>`;
  });
  html += "</tbody></table></div>";
  wrap.innerHTML = html;
}


// ═══════════════════════════════════════════════════════════════
//  INTAKE UPLOAD ZONES — Zone C (SLA Matrix) & Zone D (Benchmark)
// ═══════════════════════════════════════════════════════════════

/** Zone C — SLA Matrix file upload on the Intake page */
function initSlaIntakeUploader() {
  const dz    = document.getElementById("sla-intake-drop-zone");
  const input = document.getElementById("sla-intake-file-input");
  if (!dz || !input) return;

  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) _uploadSlaIntakeFile(f);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dz.classList.add("border-Camber","bg-Camber/5"); })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dz.classList.remove("border-Camber","bg-Camber/5"); })
  );
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); e.stopPropagation();
    dz.classList.remove("border-Camber", "bg-Camber/5");
    const f = e.dataTransfer?.files?.[0];
    if (f) _uploadSlaIntakeFile(f);
  });
}

async function _uploadSlaIntakeFile(file) {
  const dot = document.getElementById("sla-status-dot");
  _renderIntakeProgress("sla-intake-status", {
    filename: file.name,
    message:  formatBytes(file.size),
    percent:  0,
    color:    "amber",
    phase:    "uploading",
  });

  const fd = new FormData();
  fd.append("file", file);
  fd.append("sla_mode", document.getElementById("sla-mode-select")?.value || "daily");
  fd.append("sla_hrs", "0");

  try {
    const { ok, status, body } = await _uploadWithProgress("/api/sla-matrix", fd, (pct, loaded, total, finished) => {
      _renderIntakeProgress("sla-intake-status", {
        filename: file.name,
        message:  finished ? `${formatBytes(file.size)} \u2014 resolving SLA contracts\u2026`
                           : `${formatBytes(loaded)} / ${formatBytes(total)}`,
        percent:  finished ? null : pct,
        color:    "amber",
        phase:    finished ? "parsing" : "uploading",
      });
    });
    if (!ok) {
      toast("error", "SLA Matrix upload failed", ((body?.detail) || `HTTP ${status}`).slice(0, 200));
      return;
    }
    const data = body;
    // Store for the SLA Matrix tab
    window.appData.slaMatrix = data;
    window.appData.slaMatrixFilename = file.name;
    _markSessionActive();  // track session boundary

    // Run SLA intelligence engine (rich analysis with schema detection + traceability)
    if (file.name.toLowerCase().match(/\.(xlsx|xls|csv)$/)) {
      try {
        const intelFd = new FormData();
        intelFd.append("file", file);
        const intelRes = await fetch("/api/sla-intelligence", { method: "POST", body: intelFd });
        if (intelRes.ok) {
          const intel = await intelRes.json();
          window.appData.slaCeilings = intel.ceilings || null;
          window.appData.slaIntelligence = intel.intelligence || null;
          window.appData.slaTraceability = intel.traceability || null;
          _renderSlaIntelligencePanel(intel);
        } else {
          // Fallback to legacy ceilings endpoint
          const ceilFd = new FormData();
          ceilFd.append("file", file);
          const ceilRes = await fetch("/api/sla-ceilings", { method: "POST", body: ceilFd });
          if (ceilRes.ok) {
            window.appData.slaCeilings = await ceilRes.json();
          }
        }
      } catch (_e) { /* non-critical */ }
    }

    // Update dot
    if (dot) { dot.className = "w-2 h-2 rounded-full bg-Camber animate-pulse shrink-0"; }

    // Show result card on intake page
    _renderSlaIntakeCard(data, file.name);

    // Auto-populate the SLA Matrix tab
    _renderSlaMatrix(data);

    // Show intake status row + next prompt
    document.getElementById("intake-status-row")?.classList.remove("hidden");
    document.getElementById("upload-next-prompt")?.classList.remove("hidden");

    toast("success", "SLA Matrix loaded",
      `${data.total_runs} runs · ${data.compliance_pct.toFixed(1)}% compliance`);

    // ── Dynamic batch refresh: propagate new SLA ceilings to batch charts ──
    // Wait briefly for /api/sla-intelligence to finish persisting (it runs
    // concurrently above) then re-run the batch analysis with the new SLA data.
    if (window.appData.batch) {
      setTimeout(() => _refreshBatchFromServer("SLA Matrix applied to batch analysis"), 400);
    }
  } catch (err) {
    _handleFetchError(err);
  } finally {
    document.getElementById("sla-intake-status")?.classList.add("hidden");
  }
}

function _renderSlaIntakeCard(data, filename) {
  const card = document.getElementById("sla-result-card");
  if (!card) return;
  card.classList.remove("hidden");

  setText("sla-result-filename", filename || "—");
  const compEl = document.getElementById("sla-result-compliance");
  if (compEl) {
    compEl.textContent = data.compliance_pct.toFixed(1) + "%";
    compEl.className   = `text-lg font-extrabold mt-0.5 ${data.compliance_pct >= 95 ? "text-Cgreen" : data.compliance_pct >= 80 ? "text-Camber" : "text-Cred"}`;
  }
  setText("sla-result-mode", data.sla_label || data.sla_mode || "—");
  const brEl = document.getElementById("sla-result-breach");
  if (brEl) {
    brEl.textContent = String(data.breaching_runs);
    brEl.className   = `text-lg font-extrabold mt-0.5 ${data.breaching_runs > 0 ? "text-Cred" : "text-Cgreen"}`;
  }

  // ── Remove SLA Matrix button ──────────────────────────────────
  // Wires up (or re-wires) the remove button each time the card renders.
  const removeBtn = document.getElementById("sla-result-remove");
  if (removeBtn) {
    // Clone to remove any previous listener
    const fresh = removeBtn.cloneNode(true);
    removeBtn.parentNode.replaceChild(fresh, removeBtn);
    fresh.addEventListener("click", async () => {
      fresh.textContent = "Removing…";
      fresh.disabled = true;
      try {
        await fetch("/api/sla-matrix/clear", { method: "POST" });
        window.appData.slaMatrix = null;
        window.appData.slaMatrixFilename = null;
        window.appData.slaCeilings = null;
        window.appData.slaIntelligence = null;
        // Bust exec cache — grade must be recalculated with default ceilings
        window._execCache     = null;
        window._execCacheHash = null;
        // Hide the result card
        document.getElementById("sla-result-card")?.classList.add("hidden");
        // Reset the SLA Matrix tab
        _renderSlaMatrix(null);
        // Reset the dot
        const dot = document.getElementById("sla-status-dot");
        if (dot) dot.className = "w-2 h-2 rounded-full bg-Cborder shrink-0";
        toast("info", "SLA Matrix removed", "Batch analysis will revert to default SLA ceilings");
        // Refresh batch charts with default ceilings
        if (window.appData.batch) {
          await _refreshBatchFromServer("Reverted to default SLA ceilings");
        }
      } catch (e) {
        toast("error", "Remove failed", String(e));
        fresh.textContent = "Remove";
        fresh.disabled = false;
      }
    });
  }
}


// ── SLA Intelligence Panel ────────────────────────────────────
function _renderSlaIntelligencePanel(intel) {
  // ── Compact summary on upload page ──────────────────────────
  const panel = document.getElementById("sla-intelligence-panel");
  if (panel) {
    const info = intel.intelligence || {};
    const trace = intel.traceability || {};
    const ceil = intel.ceilings || {};

    panel.classList.remove("hidden");

    // Summary text
    const summaryEl = document.getElementById("sla-intel-summary-text");
    if (summaryEl) {
      const model = (info.schema_type || "unknown").toUpperCase();
      const valid = info.valid_rows || 0;
      const partial = info.partial_rows || 0;
      const total = info.total_rows || 0;
      const blocked = trace.blocked ? " · ⚠ BLOCKED" : "";
      summaryEl.textContent = `${model} model · ${valid} valid · ${partial} partial · ${total} total${blocked}`;
      if (trace.blocked) summaryEl.style.color = THEME.red;
    }

    // Mini ceiling chips — file-sourced values only
    const ceilEl = document.getElementById("sla-intel-ceilings-mini");
    if (ceilEl) {
      const missingMini = (intel.intelligence || {}).missing_ceilings || [];
      const fromFile = Object.entries(ceil).map(([sched, hrs]) =>
        `<span class="text-[9px] font-bold px-2 py-0.5 rounded border border-Cborder bg-Cbg/40 text-Cwhite">
          <span class="text-Cmuted">${_esc(sched)}</span> ${Number(hrs).toFixed(1)}h
        </span>`
      );
      const notInFile = missingMini.map(sched =>
        `<span class="text-[9px] font-bold px-2 py-0.5 rounded" style="border:1px solid rgba(245,158,11,.4);color:#f59e0b;background:rgba(245,158,11,.07)">
          <span style="opacity:.7">${_esc(sched)}</span> N/A
        </span>`
      );
      ceilEl.innerHTML = [...fromFile, ...notInFile].join("");
    }
  }

  // ── Full detail in SLA Matrix tab ───────────────────────────
  _renderSlaIntelligenceDetail(intel);
}

function _renderSlaIntelligenceDetail(intel) {
  const panel = document.getElementById("sla-intelligence-detail");
  if (!panel) return;

  const info = intel.intelligence || {};
  const trace = intel.traceability || {};
  const warnings = info.warnings || [];
  const contracts = (info.contracts || []).slice(0, 30);

  panel.classList.remove("hidden");

  // Schema detection header
  const schemaColor = info.valid_rows > 0 ? THEME.green : THEME.red;
  const schemaLabel = info.detected_model || info.schema_type || "Unknown";
  const blocked = trace.blocked;

  let html = `
    <div class="flex items-center gap-3 mb-3">
      <span class="text-[11px] font-bold px-2 py-0.5 rounded"
            style="color:${schemaColor};background:${hexA(schemaColor, 0.1)};border:1px solid ${hexA(schemaColor, 0.3)}">
        ${escapeHtml(info.schema_type?.toUpperCase() || "?")} MODEL
      </span>
      <span class="text-[11px] text-Cmuted">${escapeHtml(schemaLabel)}</span>
      <span class="text-[10px] text-Cmuted">${info.valid_rows || 0} valid · ${info.partial_rows || 0} partial · ${info.total_rows || 0} total rows</span>
      ${blocked ? '<span class="text-[10px] text-Cred font-bold">⚠ COMPLIANCE BLOCKED</span>' : ""}
    </div>`;

  // Warnings
  if (warnings.length) {
    html += '<div class="space-y-1 mb-3">';
    for (const w of warnings) {
      const wColor = w.severity === "critical" ? THEME.red : w.severity === "warning" ? THEME.amber : THEME.blue;
      html += `<div class="text-[10px] px-2 py-1 rounded border" style="color:${wColor};border-color:${hexA(wColor, 0.3)};background:${hexA(wColor, 0.05)}">
        <span class="font-bold">${escapeHtml(w.code || "")}</span> ${escapeHtml(w.text || "")}
      </div>`;
    }
    html += '</div>';
  }

  // Ceilings summary — file-sourced only, explicitly flag missing schedule types
  const ceil = intel.ceilings || {};
  const missingCeil = (intel.intelligence || {}).missing_ceilings || [];
  const allSchedTypes = ["DAILY", "WEEKLY", "MONTHLY"];
  html += '<div class="flex flex-wrap gap-3 mb-3">';
  // Show file-sourced ceilings
  for (const [sched, hrs] of Object.entries(ceil)) {
    const fromFile = _isCustomerSlaType(trace.type);
    const srcLabel = fromFile ? "From SLA File" : "Config Assumed";
    const srcColor = fromFile ? "text-Cgreen" : "text-Camber";
    html += `<div class="rounded-lg border border-Cborder bg-Cbg/40 px-3 py-1.5 text-center min-w-[72px]">
      <div class="text-[9px] text-Cmuted font-bold uppercase tracking-wider">${_esc(sched)}</div>
      <div class="text-sm font-extrabold text-Cwhite">${Number(hrs).toFixed(1)}h</div>
      <div class="text-[8px] ${srcColor}">${srcLabel}</div>
    </div>`;
  }
  // Show missing schedule types as explicit 'Not in file' tiles
  for (const sched of missingCeil) {
    html += `<div class="rounded-lg border px-3 py-1.5 text-center min-w-[72px]" style="border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.06)">
      <div class="text-[9px] font-bold uppercase tracking-wider" style="color:#f59e0b">${_esc(sched)}</div>
      <div class="text-sm font-extrabold" style="color:#f59e0b">N/A</div>
      <div class="text-[8px]" style="color:#f59e0b">Not in file</div>
    </div>`;
  }
  html += '</div>';

  // SLA contracts table
  if (contracts.length) {
    html += `<div class="text-[10px] font-bold text-Cmuted uppercase tracking-wider mb-1">Resolved SLA Rules (${contracts.length})</div>`;
    html += '<div class="overflow-x-auto"><table class="w-full text-[10px]">';
    html += '<thead><tr class="text-Cmuted border-b border-Cborder">';
    html += '<th class="text-left px-2 py-1">Batch</th>';
    html += '<th class="text-left px-2 py-1">Schedule</th>';
    html += '<th class="text-right px-2 py-1">Window</th>';
    html += '<th class="text-right px-2 py-1">Actual</th>';
    html += '<th class="text-right px-2 py-1">Buffer</th>';
    html += '<th class="text-left px-2 py-1">Health</th>';
    html += '<th class="text-left px-2 py-1">Reason</th>';
    html += '</tr></thead><tbody>';
    const _healthColor = (s) => s === "OK" ? THEME.green
      : s === "ACK"     ? THEME.green
      : s === "AT_RISK" ? THEME.amber
      : s === "BREACH"  ? THEME.red
      : s === "CYCLIC"  ? THEME.blue
      : THEME.muted;
    const _fmtHrs = (h) => (h === null || h === undefined) ? "—" : Number(h).toFixed(1) + "h";
    const _fmtBuf = (c) => {
      if (c.buffer_hrs === null || c.buffer_hrs === undefined) return "—";
      const sign = c.buffer_hrs < 0 ? "" : "+";
      const pct  = (c.buffer_pct === null || c.buffer_pct === undefined) ? "" : ` (${sign}${c.buffer_pct.toFixed(0)}%)`;
      return `${sign}${c.buffer_hrs.toFixed(1)}h${pct}`;
    };
    for (const c of contracts) {
      const hColor   = _healthColor(c.health_status || "NO_DATA");
      const winTxt   = c.sla_window_hrs ? c.sla_window_hrs.toFixed(1) + "h"
                       : c.sla_duration_hrs ? c.sla_duration_hrs.toFixed(1) + "h" : "—";
      const bufTxt   = _fmtBuf(c);
      const bufColor = (c.buffer_hrs !== null && c.buffer_hrs !== undefined && c.buffer_hrs < 0)
                       ? THEME.red : THEME.muted;
      html += `<tr class="border-b border-Cborder/30 hover:bg-Cblue/5">
        <td class="px-2 py-1 font-semibold text-Cwhite truncate max-w-[180px]">${escapeHtml(c.batch_name || "")}</td>
        <td class="px-2 py-1 text-Cmuted truncate max-w-[160px]">${escapeHtml((c.schedule_raw || c.schedule_type || "").substring(0, 32))}</td>
        <td class="px-2 py-1 text-right font-mono">${winTxt}</td>
        <td class="px-2 py-1 text-right font-mono">${_fmtHrs(c.actual_window_hrs)}</td>
        <td class="px-2 py-1 text-right font-mono" style="color:${bufColor}">${bufTxt}</td>
        <td class="px-2 py-1"><span class="px-1.5 py-0.5 rounded text-[9px] font-bold" style="color:${hColor};background:${hexA(hColor, 0.12)};border:1px solid ${hexA(hColor, 0.4)}">${escapeHtml(c.health_status || "—")}</span></td>
        <td class="px-2 py-1 text-Cmuted truncate max-w-[260px]" title="${escapeHtml(c.health_reason || "")}">${escapeHtml(c.health_reason || "")}</td>
      </tr>`;
    }
    html += '</tbody></table></div>';
  }

  panel.innerHTML = html;
}


// ── New Engagement — wipe all server-side session data ────────────────────
async function clearSessionData() {
  if (!confirm(
    "Clear all session data?\n\n" +
    "This removes the current customer's SOW, findings, resource data and " +
    "batch results so the next engagement starts clean.\n\n" +
    "The server will NOT restart — only the in-memory session is cleared."
  )) return;

  try {
    const res = await fetch("/api/clear-session", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}) });
    if (!res.ok) throw new Error(await res.text());

    // ── 1. Destroy ALL chart instances ────────────────────────────────────
    for (const key of Object.keys(charts)) {
      try { charts[key]?.destroy(); } catch(e) {}
      charts[key] = null;
    }
    _deepDiveCharts.forEach(c => { try { c.destroy(); } catch(e) {} });
    _deepDiveCharts = [];
    for (const el of _syncedPlotlyCharts) {
      try { Plotly.purge(el); } catch(e) {}
    }
    _syncedPlotlyCharts.clear();
    ["exec-chart-breach","exec-chart-heatmap","exec-chart-resource-health",
     "exec-chart-risk","exec-chart-temporal","exec-chart-waterfall",
     "exec-chart-forecast","resourceHeatmapContainer","batchHeatmapContainer"
    ].forEach(id => {
      const el = document.getElementById(id);
      if (el && el._fullLayout) { try { Plotly.purge(el); } catch(e) {} }
    });

    // ── 2. Wipe browser-side app state ───────────────────────────────────
    if (window.appData && typeof window.appData === "object") {
      window.appData.benchmarkBatch = null;
      window.appData.benchmarkUI = null;
    }
    window.appData        = {};
    window._lastFindings  = [];
    window._execCache     = null;
    window._execCacheHash = null;
    window._slaData       = null;
    window._deepDiveData  = null;
    // Clear findings state — stale filter/sort survives a page-session and causes
    // wrong rendering when new findings come in after a hard reset
    window._lastRealFindings = [];
    window._findingsFilter   = null;
    window._findingsSort     = null;
    window._findingsCols     = null;
    // Abort any in-flight findings request — prevents a race where the response
    // arrives AFTER the DOM is cleared and re-populates findings with stale data
    _findingsInFlight = false;
    if (_findingsDebounceTimer) { clearTimeout(_findingsDebounceTimer); _findingsDebounceTimer = null; }
    _clearSessionMarker();

    // ── 3. Upload / Intake tab ────────────────────────────────────────────
    document.getElementById("upload-result")?.classList.add("hidden");
    document.getElementById("dataset-chip")?.classList.add("hidden");
    document.getElementById("batch-result-card")?.classList.add("hidden");
    document.getElementById("bench-result-card")?.classList.add("hidden");
    document.getElementById("sla-intelligence-panel")?.classList.add("hidden");
    document.getElementById("sla-intelligence-detail")?.classList.add("hidden");
    // Batch SLA upload result strip
    document.getElementById("batch-sla-source-banner")?.classList.add("hidden");

    // ── 4. Batch Review tab ───────────────────────────────────────────────
    applyCustomerName("");
    _batchManualExclude.clear();
    _batchManualInclude.clear();
    window._lastBatchWarnings = null;
    window._batchWarnExpanded = {};
    window._findingsGroupExpanded = {};

    // Hide the whole review body, show the no-data state
    document.getElementById("batch-review-body")?.classList.add("hidden");
    document.getElementById("batch-empty")?.classList.remove("hidden");
    document.getElementById("batch-no-data-prompt")?.classList.remove("hidden");
    document.getElementById("batch-loaded-chip")?.classList.add("hidden");
    document.getElementById("batch-source-watermark")?.classList.add("hidden");
    document.getElementById("batch-data-warnings")?.classList.add("hidden");
    document.getElementById("sla-heatmap-section")?.classList.add("hidden");
    document.getElementById("window-sla-drill")?.classList.add("hidden");
    document.getElementById("failed-runs-drill")?.classList.add("hidden");

    // Clear dataset chip and table body (correct ID: top-jobs-tbody)
    document.getElementById("batch-dataset-chip") && (document.getElementById("batch-dataset-chip").textContent = "");
    const topJobsTbody = document.getElementById("top-jobs-tbody");
    if (topJobsTbody) topJobsTbody.innerHTML = "";
    document.getElementById("sla-heatmap-container") && (document.getElementById("sla-heatmap-container").innerHTML = "");

    // Remove dynamically-injected utility panel
    document.getElementById("batch-utility-panel")?.remove();

    // Reset all KPI card values to "—"
    ["bk-elapsed","bk-elapsed-sub","bk-summed","bk-summed-sub","bk-worst","bk-worst-sub",
     "bk-compliance","bk-compliance-sub","bk-window-compliance","bk-window-compliance-sub",
     "bk-breach-sub","bk-failed","bk-failed-sub","bk-sla-source","bk-sla-source-sub"
    ].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = "—"; });
    ["bk-breach","bk-atrisk","bk-ok"].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = "0"; });
    document.getElementById("bk-buffer-badge") && (document.getElementById("bk-buffer-badge").textContent = "—");

    // Reset intake cards (BatchSLA XLSX result + intake dot)
    document.getElementById("batch-sla-info-result")?.remove();
    const bsla_dot = document.getElementById("batch-sla-info-dot");
    if (bsla_dot) bsla_dot.className = "w-2 h-2 rounded-full bg-Cmuted/40 shrink-0";

    // ── 5. Resource Review tab ────────────────────────────────────────────
    document.getElementById("resource-review-body")?.classList.add("hidden");
    document.getElementById("resource-empty")?.classList.remove("hidden");
    const resTbody = document.getElementById("resource-tbody");
    if (resTbody) resTbody.innerHTML = "";
    document.getElementById("resource-heatmap") && (document.getElementById("resource-heatmap").innerHTML = "");

    // ── 6. SLA Matrix tab ─────────────────────────────────────────────────
    document.getElementById("sla-empty")?.classList.remove("hidden");
    ["sla-triage-section","sla-compliance-section","sla-jobs-section",
     "sla-breach-section","sla-intelligence-detail"
    ].forEach(id => document.getElementById(id)?.classList.add("hidden"));
    const slaTbody = document.getElementById("sla-job-tbody");
    if (slaTbody) slaTbody.innerHTML = "";

    // Also wipe manual override panel state
    document.getElementById("sla-commitments-panel")?.classList.add("hidden");
    document.getElementById("sla-manual-body")?.classList.add("hidden");
    document.getElementById("sla-manual-active-badge")?.classList.add("hidden");
    const manChevron = document.getElementById("sla-manual-chevron");
    if (manChevron) manChevron.style.transform = "rotate(0deg)";
    ["daily","weekly","monthly"].forEach(t => {
      const inp = document.getElementById(`sla-manual-${t}`);
      const tag = document.getElementById(`sla-manual-${t}-tag`);
      if (inp) inp.value = "";
      if (tag) tag.classList.add("hidden");
    });
    const manMsg = document.getElementById("sla-manual-msg");
    if (manMsg) manMsg.classList.add("hidden");

    // ── 7. Findings tab ───────────────────────────────────────────────────
    // (window._lastRealFindings and _findingsFilter already cleared in step 2)
    document.getElementById("findings-empty")?.classList.remove("hidden");
    document.getElementById("findings-loading")?.classList.add("hidden");
    ["findings-list","findings-summary-strip","findings-filter-bar"
    ].forEach(id => document.getElementById(id)?.classList.add("hidden"));
    const findingsTbody = document.getElementById("findings-tbody");
    if (findingsTbody) findingsTbody.innerHTML = "";
    document.getElementById("findings-count-badge") && (document.getElementById("findings-count-badge").textContent = "");
    // Clear verdict hero and audit coverage panels (dynamically injected)
    document.getElementById("findings-verdict-hero")?.remove();
    document.getElementById("findings-audit-coverage")?.remove();
    // Hide + wipe PE narrative card — it stays visible otherwise because it's
    // unhidden by renderPeReviewSections() and never re-hidden on reset
    const peNarrCard = document.getElementById("pe-narrative-card");
    if (peNarrCard) {
      peNarrCard.classList.add("hidden");
      const peNarrSections = document.getElementById("pe-narr-sections");
      if (peNarrSections) peNarrSections.innerHTML = "";
      const peNarrSummary = document.getElementById("pe-narr-summary");
      if (peNarrSummary) peNarrSummary.textContent = "—";
      const peNarrVerdict = document.getElementById("pe-narr-verdict");
      if (peNarrVerdict) peNarrVerdict.textContent = "—";
      const peNarrReason = document.getElementById("pe-narr-verdict-reason");
      if (peNarrReason) { peNarrReason.textContent = ""; peNarrReason.classList.add("hidden"); }
    }
    // Reset filter buttons back to default state
    document.querySelectorAll(".findings-filter-btn").forEach(btn => btn.classList.remove("active-filter"));
    const ffilAll = document.getElementById("ffil-all");
    if (ffilAll) ffilAll.classList.add("active-filter");
    // Reset hero pill + counts
    const decisionPill = document.getElementById("findings-decision-pill");
    if (decisionPill) { decisionPill.textContent = "—"; decisionPill.removeAttribute("style"); }
    ["hero-crit","hero-warn","hero-ok","hero-info"].forEach(id => {
      const el = document.getElementById(id); if (el) el.textContent = "0";
    });

    // ── 8. Executive Dashboard tab ────────────────────────────────────────
    // exec-content holds all rendered panels; exec-no-data is the empty state
    document.getElementById("exec-no-data")?.classList.remove("hidden");
    document.getElementById("exec-content")?.classList.add("hidden");
    document.getElementById("exec-loading")?.classList.add("hidden");
    // Also hide legacy ID variants in case template uses them
    ["exec-customer-banner","exec-audit-pulse","exec-kpi-strip","exec-chart-section"
    ].forEach(id => document.getElementById(id)?.classList.add("hidden"));

    // ── 9. Benchmark tab ─────────────────────────────────────────────────
    // Gap 3: restore the GUIDED empty state (what to upload + Go-to-Upload),
    // never a blank panel. Hide the loaded KPI strip / chip so a stale "N/A —
    // no baseline" Pass-Rate tile can't linger and read like an error after reset.
    document.getElementById("bench-no-data-prompt")?.classList.remove("hidden");
    document.getElementById("bench-empty")?.classList.add("hidden");
    document.getElementById("bench-kpi-row")?.classList.add("hidden");
    document.getElementById("bench-loaded-chip")?.classList.add("hidden");
    ["bench-result-section","bench-loaded"
    ].forEach(id => document.getElementById(id)?.classList.add("hidden"));
    ["bench-batch-badge","bench-ui-badge","bench-batch-status","bench-ui-status"
    ].forEach(id => document.getElementById(id)?.classList.add("hidden"));
    const benchDot = document.getElementById("bench-intake-dot");
    if (benchDot) benchDot.className = "w-2 h-2 rounded-full bg-Cmuted/40 shrink-0";
    const benchCorr = document.getElementById("bench-correlation-panel");
    if (benchCorr) benchCorr.innerHTML = "";
    const benchBanner = document.getElementById("bench-summary-banner");
    if (benchBanner) benchBanner.className = "hidden rounded-xl border border-Cborder bg-Ccard2/60 px-5 py-3";
    const benchSummary = document.getElementById("bench-summary-text");
    if (benchSummary) benchSummary.textContent = "";

    // ── 10. SOW tab ───────────────────────────────────────────────────────
    ["sow-dfu-baseline","sow-dfu-actual","sow-sku-baseline","sow-sku-actual",
     "sow-orders-baseline","sow-orders-actual","sow-batchjobs-baseline","sow-batchjobs-actual",
     "sow-users-baseline","sow-users-actual"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = "";
    });
    document.getElementById("sow-manual-panel")?.classList.add("hidden");
    document.getElementById("sow-volume-comparison")?.classList.add("hidden");
    fetch("/api/sow/baseline", { method: "DELETE" }).catch(() => {});
    document.getElementById("sow-empty")?.classList.remove("hidden");
    document.getElementById("sow-contract-grid")?.classList.add("hidden");

    // ── 11. Status dots ───────────────────────────────────────────────────
    ["ds-batch","ds-resource","ds-issues","ds-sow","ds-gemini"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.className = "w-2 h-2 rounded-full status-dot-muted shrink-0 transition-all duration-300";
    });
    ["ds-batch-label","ds-resource-label"].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.textContent = "";
    });

    // ── 12. Navigate back to Upload tab ──────────────────────────────────
    setActiveView("upload");

    toast("success", "Session cleared", "Ready for a new engagement. Re-upload all files.");
  } catch (err) {
    toast("error", "Clear failed", String(err).slice(0, 120));
  }
}


/** Zone D — Performance Benchmark: two source sub-zones (batch runtime + UI). */
function initBenchIntakeUploader() {
  _initBenchZone("batch", "amber");
  _initBenchZone("ui", "purple");
}

function _initBenchZone(kind, color) {
  const dz    = document.getElementById(`bench-${kind}-drop-zone`);
  const input = document.getElementById(`bench-${kind}-file-input`);
  if (!dz || !input) return;
  const onCls = [`border-C${color}`, `bg-C${color}/5`];

  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) _uploadBenchFile(f, kind);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dz.classList.add(...onCls); })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dz.classList.remove(...onCls); })
  );
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); e.stopPropagation();
    dz.classList.remove(...onCls);
    const f = e.dataTransfer?.files?.[0];
    if (f) _uploadBenchFile(f, kind);
  });
}

/**
 * Upload one benchmark source into its slot, then merge both slots into the
 * unified window.appData.benchmark consumed by findings / exec / benchmark view.
 * kind: "batch" (runtime comparison) | "ui" (transaction benchmark).
 */
async function _uploadBenchFile(file, kind) {
  const color  = kind === "batch" ? "amber" : "purple";
  const dot    = document.getElementById("bench-intake-dot");
  const statId = `bench-${kind}-status`;
  _renderIntakeProgress(statId, {
    filename: file.name,
    message:  formatBytes(file.size),
    percent:  0,
    color,
    phase:    "uploading",
  });

  const threshold = parseFloat(
    document.getElementById("cfg-bench-thresh")?.value ||
    window.appData.config?.benchmark_threshold ||
    10
  );
  const fd = new FormData();
  fd.append("file", file);
  fd.append("threshold", String(threshold));

  try {
    const { ok, status, body } = await _uploadWithProgress("/api/benchmark", fd, (pct, loaded, total, finished) => {
      _renderIntakeProgress(statId, {
        filename: file.name,
        message:  finished ? `${formatBytes(file.size)} \u2014 analysing\u2026`
                           : `${formatBytes(loaded)} / ${formatBytes(total)}`,
        percent:  finished ? null : pct,
        color,
        phase:    finished ? "analysing" : "uploading",
      });
    });
    if (!ok) {
      toast("error", "Benchmark upload failed", ((body?.detail) || `HTTP ${status}`).slice(0, 200));
      return;
    }
    const data = body;

    // Auto-route: trust backend's `kind`, but warn on slot mismatch.
    const detected = data.kind || (data.batch_perf_summary ? "batch" : "ui");
    if (detected !== kind) {
      toast("info", "Re-routed by content",
        `File looks like a ${detected === "batch" ? "Batch Runtime" : "UI Benchmark"} source — stored there.`);
    }
    const slot = detected;  // store where the data actually belongs

    if (slot === "batch") window.appData.benchmarkBatch = data;
    else                  window.appData.benchmarkUI    = data;

    _markSessionActive();
    _mergeBenchmarkSources();

    // Slot badges + dot
    _benchUpdateZoneBadges();
    if (dot) dot.className = "w-2 h-2 rounded-full bg-Cpurple animate-pulse shrink-0";

    _renderBenchIntakeCard(window.appData.benchmark, file.name);
    _renderBenchmark(window.appData.benchmark);
    refreshDataStatus();
    triggerGenerateFindings().catch(() => {});

    document.getElementById("intake-status-row")?.classList.remove("hidden");
    document.getElementById("upload-next-prompt")?.classList.remove("hidden");

    const bp = data.batch_perf_summary;
    toast("success",
      slot === "batch" ? "Batch runtime loaded" : "UI benchmark loaded",
      bp ? `${bp.total_jobs} jobs · ${bp.regressions} regression(s)`
         : `${data.total_transactions} transactions · ${data.degraded} regression(s)`);
  } catch (err) {
    _handleFetchError(err);
  } finally {
    document.getElementById(statId)?.classList.add("hidden");
  }
}

/** Update the per-zone count badges from the two slots. */
function _benchUpdateZoneBadges() {
  const b  = window.appData.benchmarkBatch;
  const u  = window.appData.benchmarkUI;
  const bb = document.getElementById("bench-batch-badge");
  const ub = document.getElementById("bench-ui-badge");
  if (bb) {
    if (b?.batch_perf_summary) {
      bb.textContent = `${b.batch_perf_summary.regressions}▲ ${b.batch_perf_summary.total_jobs} jobs`;
      bb.classList.remove("hidden");
    } else bb.classList.add("hidden");
  }
  if (ub) {
    if (u) {
      ub.textContent = `${_n(u.degraded)}▲ ${_n(u.total_transactions)} tx`;
      ub.classList.remove("hidden");
    } else ub.classList.add("hidden");
  }
}

/**
 * Compose the unified window.appData.benchmark from the two source slots.
 * UI source supplies rows / categories / fill_rate / observations; batch source
 * supplies batch_perf_summary. Both compose into one object for downstream
 * findings + exec consumers. Also computes the cross-layer correlation verdict.
 */
function _mergeBenchmarkSources() {
  const b = window.appData.benchmarkBatch;
  const u = window.appData.benchmarkUI;

  if (!b && !u) { window.appData.benchmark = null; return; }

  // ── ISOLATION WALL ──────────────────────────────────────────────────────
  // Batch-runtime data (b) and UI-perf data (u) are fundamentally different
  // measurement types and must NEVER be mixed in the same rows array or
  // computed together. Rules:
  //   - merged.rows              = UI rows only (from u)
  //   - merged.batch_perf_summary = batch summary only (from b)
  //   - batch job names must never appear in the Transaction Comparison Matrix
  //   - UI transaction metrics must never influence batch regression analysis
  // ──────────────────────────────────────────────────────────────────────────
  let merged;
  if (u && b) {
    // Both loaded: UI is the base object; graft batch_perf_summary on top.
    // merged.rows = UI rows only (b.rows is empty for batch files by design).
    merged = { ...u };
    merged.batch_perf_summary = b.batch_perf_summary || null;
    merged.batch_filename     = b.filename || "";
    merged.ui_filename        = u.filename || "";
    merged.filename           = `${u.filename || "UI"} + ${b.filename || "Batch"}`;
  } else if (b && !u) {
    // Batch only: start from b but explicitly zero out rows so batch job names
    // never appear in the UI Transaction Comparison Matrix or UI findings rules.
    merged = { ...b };
    merged.rows               = [];          // UI table must stay empty
    merged.total_transactions = 0;           // batch job count is in batch_perf_summary
    merged.categories         = [];          // batch window categories ≠ UI categories
    merged.evidence_sentences = [];
    merged.coverage_summary   = null;
    merged.ui_filename        = "";
    merged.batch_filename     = b.filename || "";
  } else {
    // UI only
    merged = { ...u };
    merged.batch_perf_summary = null;
    merged.batch_filename     = "";
    merged.ui_filename        = u.filename || "";
  }

  merged.correlation = _computeBenchCorrelation(b, u);
  window.appData.benchmark = merged;
  if (window._execCache !== undefined) window._execCache = null;  // invalidate exec cache
  // Update the cross-reference callout in the Batch Review tab immediately.
  if (window.appData.batch?.kpis) _renderBatchBenchmarkXref();
}

/**
 * Smart correlation bridge between batch-runtime and UI-benchmark sources.
 * Produces a release-readiness score + verdict and detects SYSTEMIC regressions:
 * subsystem name-tokens that regress in BOTH layers (shared DB/infra root cause)
 * rather than isolated tuning. O(n+m) via hash sets.
 * Returns null when fewer than the inputs needed are present.
 */
function _computeBenchCorrelation(batchObj, uiObj) {
  const bp = batchObj?.batch_perf_summary || null;

  // ── Component rates ──
  const bComp = bp ? _n(bp.comparable) : 0;
  const bRegr = bp ? _n(bp.regressions) : 0;
  const batchRegrRate = bComp > 0 ? bRegr / bComp : 0;

  const uRows   = (uiObj?.rows || []).filter(r => _n(r.baseline_sec) > 0);
  const uBreach = uRows.filter(r => r.status === "BREACH").length;
  const uiBreachRate = uRows.length > 0 ? uBreach / uRows.length : 0;

  // Release-readiness score: weight each present layer.
  let score, parts = [];
  if (bp && uRows.length) {
    score = 100 - batchRegrRate * 50 - uiBreachRate * 50;
    parts = ["batch", "ui"];
  } else if (bp) {
    score = 100 - batchRegrRate * 100;
    parts = ["batch"];
  } else if (uRows.length) {
    score = 100 - uiBreachRate * 100;
    parts = ["ui"];
  } else {
    return null;
  }
  score = Math.max(0, Math.min(100, Math.round(score)));
  const verdict = score >= 85 ? "GO" : score >= 65 ? "CONDITIONAL" : "NO-GO";

  // ── Systemic detection: token-set intersection across both layers ──
  const STOP = new Set(["the","and","for","run","job","load","time","test","prod",
    "uat","new","old","sec","secs","daily","weekly","monthly","batch","data",
    "report","process","step","main","seq"]);
  const tokset = (name) => {
    const out = new Set();
    String(name || "").toUpperCase().split(/[^A-Z0-9]+/).forEach(t => {
      if (t.length >= 3 && !STOP.has(t.toLowerCase()) && !/^\d+$/.test(t)) out.add(t);
    });
    return out;
  };

  const shared = [];
  if (bp && uBreach > 0) {
    const batchTok = new Map();
    (bp.top_regressions || []).forEach(r => tokset(r.job).forEach(t => {
      if (!batchTok.has(t)) batchTok.set(t, r.job);
    }));
    const uiTok = new Map();
    uRows.filter(r => r.status === "BREACH").forEach(r => tokset(r.transaction).forEach(t => {
      if (!uiTok.has(t)) uiTok.set(t, r.transaction);
    }));
    batchTok.forEach((batchName, t) => {
      if (uiTok.has(t)) shared.push({ token: t, batch: batchName, ui: uiTok.get(t) });
    });
  }

  return {
    score,
    verdict,
    layers: parts,
    batch_regression_rate: Math.round(batchRegrRate * 100),
    ui_breach_rate: Math.round(uiBreachRate * 100),
    systemic: shared.length > 0,
    shared_subsystems: shared.slice(0, 5),
  };
}

// ═══════════════════════════════════════════════════════════════
//  ZONE E — Workflow SLA Info (BatchSLA_info.xlsx) — Tier-1 SLA source
// ═══════════════════════════════════════════════════════════════

/** Zone E — BatchSLA_info.xlsx uploader (workflow-level SLA contracts). */
function initBatchSlaInfoUploader() {
  const dz    = document.getElementById("batch-sla-info-drop-zone");
  const input = document.getElementById("batch-sla-info-file-input");
  if (!dz || !input) return;

  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) _uploadBatchSlaInfoFile(f);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      dz.classList.add("border-Cteal", "bg-Cteal/5");
    })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      dz.classList.remove("border-Cteal", "bg-Cteal/5");
    })
  );
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); e.stopPropagation();
    dz.classList.remove("border-Cteal", "bg-Cteal/5");
    const f = e.dataTransfer?.files?.[0];
    if (f) _uploadBatchSlaInfoFile(f);
  });
}

async function _uploadBatchSlaInfoFile(file) {
  const dot = document.getElementById("batch-sla-info-dot");
  _renderIntakeProgress("batch-sla-info-status", {
    filename: file.name,
    message:  formatBytes(file.size),
    percent:  0,
    color:    "teal",
    phase:    "uploading",
  });

  const fd = new FormData();
  fd.append("file", file);

  try {
    const { ok, status, body } = await _uploadWithProgress(
      "/api/batch-sla/upload", fd,
      (pct, loaded, total, finished) => {
        _renderIntakeProgress("batch-sla-info-status", {
          filename: file.name,
          message:  finished ? `${formatBytes(file.size)} — parsing workflow SLAs…`
                             : `${formatBytes(loaded)} / ${formatBytes(total)}`,
          percent:  finished ? null : pct,
          color:    "teal",
          phase:    finished ? "parsing" : "uploading",
        });
      }
    );

    if (!ok) {
      toast("error", "Workflow SLA upload failed", ((body?.detail) || `HTTP ${status}`).slice(0, 200));
      if (dot) dot.className = "w-2 h-2 rounded-full bg-Cred";
      return;
    }

    window.appData = window.appData || {};
    window.appData.batchSlaInfo = body;

    // Build slaCeilings from workflow SLA data if not already set from SLA Matrix upload
    // This ensures PE Findings has SLA context even when only BatchSLA is uploaded
    if (!window.appData.slaCeilings && body.workflows?.length > 0) {
      const ceilMap = {};
      for (const wf of body.workflows) {
        const bt = wf.batch_type || "DAILY";
        const sh = wf.sla_hours;
        if (sh != null && sh > 0) {
          // Use the max SLA per batch type as the ceiling
          if (!ceilMap[bt] || sh > ceilMap[bt]) ceilMap[bt] = sh;
        }
      }
      if (Object.keys(ceilMap).length > 0) {
        window.appData.slaCeilings = ceilMap;
      }
    }

    refreshDataStatus();
    document.getElementById("intake-status-row")?.classList.remove("hidden");
    _renderBatchSlaInfoCard(body);

    // Refresh SLA Commitments panel immediately so SLA Matrix tab shows the new data
    _renderSlaCommitmentsPanel();

    // Re-render Batch Review with updated per-job SLAs — recomputes gauge, compliance,
    // and top-breaches table using the XLSX-resolved SLA ceilings, not global defaults.
    // Then re-run SLA Matrix sequentially so it uses the refreshed window.appData.batch.
    // If no batch data yet, the stale banner will auto-show when batch is uploaded later.
    if (window.appData?.batch) {
      (async () => {
        await _refreshBatchFromServer("Batch Review updated with customer SLA contracts");
        await triggerSlaMatrix();
        // After successful refresh, sla_source.type will be "sla_matrix" or "batch_sla_xlsx".
        // Only show the amber stale banner if NEITHER type is set (refresh failed or
        // server restarted — in that case _refreshBatchFromServer already updated
        // the banner via _applyBatchSlaInfoToBanner, so avoid double-showing).
        const slaSrcType = window.appData?.batch?.sla_source?.type;
        if (slaSrcType !== "batch_sla_xlsx" && slaSrcType !== "sla_matrix") {
          _renderSlaStaleWarningBanner();
        }
      })().catch(() => { _renderSlaStaleWarningBanner(); });
    }

    const wfCount  = body.workflow_count || 0;
    const slaCount = body.with_sla_count || 0;
    const explicitCount = body.with_explicit_sla || 0;
    const fallbackCount = body.with_fallback_sla || 0;
    const types    = (body.batch_types  || []).join(", ") || "—";
    toast("success", "Workflow SLA loaded",
      `${wfCount} workflow(s) · ${explicitCount > 0 ? explicitCount + ' with explicit SLA' : slaCount + ' with SLA'}`
      + (fallbackCount > 0 ? ` · ${fallbackCount} using default SLA` : "")
      + ` · types: ${types}`);

    const missing = wfCount - slaCount;
    if (missing > 0) {
      toast("warning", `${missing} workflow(s) without SLA`,
        "These rows fall back to SOW ceiling or global defaults.");
    }
  } catch (err) {
    _handleFetchError(err);
    if (dot) dot.className = "w-2 h-2 rounded-full bg-Cred";
  } finally {
    document.getElementById("batch-sla-info-status")?.classList.add("hidden");
  }
}

function _renderBatchSlaInfoCard(data) {
  let card = document.getElementById("batch-sla-info-result");
  if (!card) {
    card = document.createElement("div");
    card.id = "batch-sla-info-result";
    card.className = "rounded-2xl border border-Cteal/30 bg-gradient-to-br from-Ccard to-Ccard2 shadow-panel p-4";
    document.getElementById("intake-status-row")?.appendChild(card);
  }
  const wfs = data.workflows || [];

  // Cross-reference with actual job runtimes from SLA Matrix / batch data
  const jobSummary = window.appData?.slaMatrix?.job_summary || [];
  const jobByName  = new Map(jobSummary.map(j => [
    (j.job_name || j.Job_Name || "").toLowerCase(), j
  ]));

  // Helper: compute buffer pct from actual peak vs SLA
  const calcBuffer = (wf) => {
    const matchedJob = jobByName.get((wf.workflow || "").toLowerCase());
    if (!matchedJob) return null;
    const peakHrs = parseFloat(matchedJob.peak_hrs || 0);
    const slaHrs  = parseFloat(wf.sla_hours || matchedJob.sla_limit || matchedJob.sla_limit_hrs || 0);
    if (!slaHrs || !peakHrs) return null;
    return ((slaHrs - peakHrs) / slaHrs * 100);
  };

  const rows = wfs.slice(0, 10).map(w => {
    const sla    = w.sla_hours != null ? `${w.sla_hours}h` : "—";
    const comp   = w.compliance || "UNKNOWN";
    const cColor = comp === "BREACH" ? "text-Cred" : comp === "AT_RISK" ? "text-Camber" : comp === "OK" ? "text-Cteal" : "text-Cmuted";
    const buf    = calcBuffer(w);
    const bufStr = buf != null ? `${buf.toFixed(1)}%` : "—";
    const bufColor = buf == null ? "text-Cmuted" :
                     buf < 10   ? "text-Cred font-bold" :
                     buf < 20   ? "text-Camber font-semibold" : "text-Cteal";
    const lowBufFlag = buf != null && buf < 10
      ? `<span class="ml-1 text-[9px] font-bold text-Cred bg-Cred/15 px-1 rounded">⚠ LOW</span>` : "";
    return `<tr class="border-b border-Cborder/20 last:border-0 hover:bg-Ccard/30">
      <td class="py-1 pr-2 text-Cwhite/80 font-mono text-[10px] truncate max-w-[140px]" title="${_esc(w.workflow || '')}">${_esc(w.workflow || '—')}</td>
      <td class="py-1 pr-2 text-[10px] text-Cmuted">${_esc(w.batch_type || '—')}</td>
      <td class="py-1 pr-2 text-[10px] font-mono font-bold text-Cteal">${sla}</td>
      <td class="py-1 pr-2 text-[10px] ${bufColor}">${bufStr}${lowBufFlag}</td>
      <td class="py-1 text-[10px] font-semibold ${cColor}">${comp}</td>
    </tr>`;
  }).join("");

  const more = wfs.length > 10 ? `<p class="text-[10px] text-Cmuted italic mt-1">+ ${wfs.length - 10} more workflows not shown</p>` : "";

  // Triage summary: count low-buffer and breach jobs
  const lowBufJobs   = wfs.filter(w => { const b = calcBuffer(w); return b != null && b < 20; });
  const breachJobs   = wfs.filter(w => w.compliance === "BREACH");
  const triageHtml   = (lowBufJobs.length || breachJobs.length) ? `
    <div class="mt-3 rounded-lg border border-Camber/30 bg-Camber/5 px-3 py-2 text-[10px] space-y-1">
      <div class="font-bold text-Camber uppercase tracking-wider mb-1">⚡ SLA Triage Alerts</div>
      ${breachJobs.length ? `<div class="text-Cred">🔴 ${breachJobs.length} workflow(s) BREACHING SLA: ${breachJobs.slice(0,3).map(w=>_esc(w.workflow||'')).join(', ')}${breachJobs.length>3?'…':''}</div>` : ''}
      ${lowBufJobs.length ? `<div class="text-Camber">⚠️ ${lowBufJobs.length} workflow(s) with &lt;20% SLA buffer — at risk of breach under load</div>` : ''}
    </div>` : "";

  card.innerHTML = `
    <div class="flex items-center gap-2 mb-3">
      <span class="w-2 h-2 rounded-full bg-Cteal animate-pulse"></span>
      <h3 class="text-xs font-bold text-Cwhite">Workflow SLA Loaded</h3>
      <span class="ml-auto text-[10px] text-Cmuted">${_esc(data.filename || "")}</span>
    </div>
    <div class="text-[10px] text-Cmuted mb-2">
      ${data.workflow_count} workflow(s) · ${data.with_sla_count} with SLA ·
      types: <span class="text-Cteal font-semibold">${(data.batch_types || []).join(", ") || "—"}</span>
    </div>
    ${wfs.length ? `<div class="overflow-x-auto rounded border border-Cborder/30">
      <table class="w-full text-[10px]">
        <thead><tr class="border-b border-Cborder/40 bg-Cbg/40">
          <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">Workflow</th>
          <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">Type</th>
          <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">SLA</th>
          <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">Buffer %</th>
          <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">Status</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>${more}` : '<p class="text-[10px] text-Cmuted">No workflow rows parsed.</p>'}
    ${triageHtml}
    <p class="text-[10px] text-Cmuted/70 mt-2 italic">
      Buffer % = headroom between peak actual runtime and SLA ceiling. Requires Ctrl-M data loaded for calculation.
    </p>`;
}

// ═══════════════════════════════════════════════════════════════
//  ZONE F — SOW Contract PDF (Tier 2 SLA ceilings + volume baseline)
// ═══════════════════════════════════════════════════════════════
function initSowUploadZone() {
  const dz    = document.getElementById("sow-intake-drop-zone");
  const input = document.getElementById("sow-intake-file-input");
  if (!dz || !input) return;

  dz.addEventListener("click", () => input.click());
  input.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) _uploadSowFile(f);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      dz.classList.add("border-Ccyan", "bg-Ccyan/5");
    })
  );
  ["dragleave", "dragend"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault(); e.stopPropagation();
      dz.classList.remove("border-Ccyan", "bg-Ccyan/5");
    })
  );
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); e.stopPropagation();
    dz.classList.remove("border-Ccyan", "bg-Ccyan/5");
    const f = e.dataTransfer?.files?.[0];
    if (f) _uploadSowFile(f);
  });
}

async function _uploadSowFile(file) {
  const dot = document.getElementById("sow-intake-dot");
  _renderIntakeProgress("sow-intake-status", {
    filename: file.name,
    message:  formatBytes(file.size),
    percent:  0,
    color:    "cyan",
    phase:    "uploading",
  });

  const fd = new FormData();
  fd.append("file", file);

  try {
    const { ok, status, body } = await _uploadWithProgress(
      "/api/sow/parse", fd,
      (pct, loaded, total, finished) => {
        _renderIntakeProgress("sow-intake-status", {
          filename: file.name,
          message:  finished ? `${formatBytes(file.size)} — extracting SLA windows…`
                             : `${formatBytes(loaded)} / ${formatBytes(total)}`,
          percent:  finished ? null : pct,
          color:    "cyan",
          phase:    finished ? "parsing" : "uploading",
        });
      }
    );

    if (!ok) {
      toast("error", "SOW parse failed", ((body?.detail) || `HTTP ${status}`).slice(0, 200));
      if (dot) dot.className = "w-2 h-2 rounded-full bg-Cred";
      return;
    }

    window.appData = window.appData || {};
    window.appData.sowContract = body._contract || {};

    // Also populate the SOW contract panel if it's already mounted
    _renderSowContractPanel(window.appData.sowContract);
    refreshAuditContext().catch(() => {});  // update health bar

    // Re-run SLA Matrix then Batch Review sequentially so SOW Tier 2 ceilings
    // are reflected in both tabs with consistent compliance numbers.
    if (window.appData?.batch) {
      (async () => {
        await triggerSlaMatrix();
        await _refreshBatchFromServer("Batch Review updated with SOW Tier 2 SLA ceilings");
      })().catch(() => {});
    }

    if (dot) dot.className = "w-2 h-2 rounded-full bg-Ccyan animate-pulse";
    document.getElementById("intake-status-row")?.classList.remove("hidden");

    // Populate SOW baseline form fields in the SOW settings tab
    const contract = body._contract || {};
    let filled = 0;
    _SOW_FIELDS.forEach(({ key, baseId }) => {
      if (body[key] != null) {
        const el = document.getElementById(`${baseId}-baseline`);
        if (el) { el.value = body[key]; filled++; }
      }
    });

    _renderSowIntakeCard(body, file.name);

    const slaW = contract.sla_windows || {};
    const slaSummary = Object.entries(slaW).map(([t, v]) => `${t}=${v.limit_hours}h`).join(", ");
    const volSummary = Object.entries(contract.volume_by_year || {}).map(
      ([yr, v]) => `${yr}:${(v.item_locations || 0).toLocaleString()}`
    ).join(" → ");
    toast("success", "SOW parsed",
      `${filled} metric(s)${slaSummary ? " · SLA: " + slaSummary : ""}${volSummary ? " · Vol: " + volSummary : ""} — Tier 2 ceilings active`);

    // Surface AI validation warnings if any values were rejected
    const aiWarnings = contract._ai_validation_warnings || [];
    if (aiWarnings.length) {
      toast("warning", "SOW data validation",
        `${aiWarnings.length} value(s) rejected as out-of-range: ${aiWarnings.slice(0, 2).join("; ")}${aiWarnings.length > 2 ? "..." : ""}`);
    }

  } catch (err) {
    _handleFetchError(err);
    if (dot) dot.className = "w-2 h-2 rounded-full bg-Cred";
  } finally {
    document.getElementById("sow-intake-status")?.classList.add("hidden");
  }
}

function _renderSowIntakeCard(data, filename) {
  let card = document.getElementById("sow-intake-result");
  if (!card) {
    card = document.createElement("div");
    card.id = "sow-intake-result";
    card.className = "rounded-2xl border border-Ccyan/30 bg-gradient-to-br from-Ccard to-Ccard2 shadow-panel p-4";
    document.getElementById("intake-status-row")?.appendChild(card);
  }
  const contract = data._contract || {};
  const slaW  = contract.sla_windows   || {};
  const volY  = contract.volume_by_year || {};
  const dr    = contract.disaster_recovery || {};

  const slaRows = Object.entries(slaW).map(([t, v]) =>
    `<tr><td class="py-0.5 pr-3 text-Cwhite/80">${t}</td><td class="py-0.5 font-mono text-Ccyan">${v.limit_hours}h SLA ceiling</td></tr>`
  ).join("");
  const volRows = Object.entries(volY).map(([yr, v]) =>
    `<tr><td class="py-0.5 pr-3 text-Cwhite/80">${yr}</td><td class="py-0.5 font-mono text-Cblue">${(v.item_locations || 0).toLocaleString()} ${v.uom || "Item-Locations"}</td></tr>`
  ).join("");
  const drStr = dr.level ? `${dr.level}${dr.rto_hours ? ` (RTO ${dr.rto_hours}h / RPO ${dr.rpo_hours}h)` : ""}` : "";

  card.innerHTML = `
    <div class="flex items-center gap-2 mb-3">
      <span class="w-2 h-2 rounded-full bg-Ccyan animate-pulse"></span>
      <h3 class="text-xs font-bold text-Cwhite">SOW Contract Loaded</h3>
      <span class="ml-auto text-[10px] text-Cmuted">${_esc(filename || "")}</span>
    </div>
    ${contract.customer_name ? `<p class="text-[10px] text-Cmuted mb-2">Customer: <span class="text-Cwhite">${_esc(contract.customer_name)}</span>${contract.annual_fee ? ` &nbsp;·&nbsp; ${contract.currency || "€"}${Number(contract.annual_fee).toLocaleString()} / ${contract.contract_years || 3} yr` : ""}</p>` : ""}
    ${slaRows ? `<div class="text-[10px] font-semibold text-Cmuted uppercase tracking-wider mb-1">Batch Window SLA Ceilings</div><table class="w-full text-[10px] mb-3"><tbody>${slaRows}</tbody></table>` : ""}
    ${volRows ? `<div class="text-[10px] font-semibold text-Cmuted uppercase tracking-wider mb-1">Volume Ramp</div><table class="w-full text-[10px] mb-2"><tbody>${volRows}</tbody></table>` : ""}
    ${drStr ? `<p class="text-[10px] text-Cmuted">DR: <span class="text-Cwhite">${_esc(drStr)}</span></p>` : ""}
    <p class="text-[10px] text-Cmuted/70 mt-2 italic">SLA ceilings are now active as Tier 2 fallback in the SLA matrix.</p>`;
}

// ═══════════════════════════════════════════════════════════════
//  SOW CONTRACT INTELLIGENCE TAB
// ═══════════════════════════════════════════════════════════════
const _SOW_FIELDS = [
  { key: "daily_dfu",    baseId: "sow-dfu",       label: "Daily DFU" },
  { key: "daily_sku",    baseId: "sow-sku",       label: "Daily SKU Count" },
  { key: "daily_orders", baseId: "sow-orders",    label: "Daily Orders" },
  { key: "batch_jobs",   baseId: "sow-batchjobs", label: "Batch Jobs / Day" },
  { key: "peak_users",   baseId: "sow-users",     label: "Peak Concurrent Users" },
];

// ── Open the standalone manual SOW entry panel ───────────────────────────
function openSowManualEntry() {
  document.getElementById("sow-empty")?.classList.add("hidden");
  document.getElementById("sow-manual-panel")?.classList.remove("hidden");
  // Re-bind all inputs (only binds once due to flag)
  _bindSowManualInputs();
  // Restore any previously stored values
  loadSowBaseline();
}

// ── Clear all manual SOW fields and backend baseline ─────────────────────
async function clearSowManual() {
  ["sow-dfu-baseline","sow-dfu-actual","sow-sku-baseline","sow-sku-actual",
   "sow-orders-baseline","sow-orders-actual","sow-batchjobs-baseline","sow-batchjobs-actual",
   "sow-users-baseline","sow-users-actual"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
  ["dfu","sku","orders","batchjobs","users"].forEach(m => _updateSowMetricCard(m));
  _updateSowTargetCount();
  try { await fetch("/api/sow/baseline", { method: "DELETE" }); } catch (_) {}
  window.appData = window.appData || {};
  window.appData.sowCompare = null;
  window._execCacheHash = null;
  document.getElementById("sow-chart-wrap")?.classList.add("hidden");
  document.getElementById("sow-table-wrap")?.classList.add("hidden");
  document.getElementById("sow-save-msg")?.classList.add("hidden");
  toast("info", "SOW data cleared", "All manual targets and comparison results removed.");
}

// ── Called on nav to SOW tab — restore from appData if already loaded ──
function initSowTab() {
  if (window.appData?.sowContract && Object.keys(window.appData.sowContract).length) {
    _renderSowContractPanel(window.appData.sowContract);
  }
  if (window.appData?.sowCompare?.metrics?.length) {
    _renderSowComparison(window.appData.sowCompare);
  }
  loadSowBaseline();
}

// ── Render the 4-section contract intelligence grid ───────────────────────
function _renderSowContractPanel(contract) {
  if (!contract || !Object.keys(contract).length) return;

  // Show grid, hide empty state
  document.getElementById("sow-empty")?.classList.add("hidden");
  document.getElementById("sow-contract-grid")?.classList.remove("hidden");

  const c = contract;

  // ── 1A: Contract Identity ────────────────────────────────────
  const idRows = [
    ["Customer",       c.customer_name],
    ["Contract Term",  c.contract_years ? `${c.contract_years} Contract Years` : null],
    ["Annual Fee",     c.annual_fee     ? `${c.currency || "€"}${Number(c.annual_fee).toLocaleString()} / year` : null],
    ["Availability",   c.availability_sla_pct ? `${c.availability_sla_pct}% Standard Availability` : null],
  ].filter(([,v]) => v);

  const idEl = document.getElementById("sow-identity-rows");
  if (idEl) {
    idEl.innerHTML = idRows.length ? idRows.map(([label, value]) => `
      <div class="flex items-start justify-between gap-4 py-1.5 border-b border-Cborder/30 last:border-0">
        <span class="text-[10px] font-semibold text-Cmuted uppercase tracking-wider shrink-0">${_esc(label)}</span>
        <span class="text-[11px] font-semibold text-Cwhite text-right">${_esc(String(value))}</span>
      </div>`).join("")
    : '<div class="text-[10px] text-Cmuted italic">No identity data extracted</div>';
  }

  // ── 1B: SLA Commitments ──────────────────────────────────────
  const slaW = c.sla_windows || {};
  const slaColors = { DAILY: "Ccyan", WEEKLY: "Cblue", MONTHLY: "Cpurple", BIWEEKLY: "Cteal" };
  const slaEl = document.getElementById("sow-sla-rows");
  if (slaEl) {
    const slaEntries = Object.entries(slaW);
    slaEl.innerHTML = slaEntries.length ? slaEntries.map(([btype, entry]) => {
      const hrs = entry.limit_hours ?? entry;
      const col = slaColors[btype] || "Ccyan";
      return `<div class="flex items-center justify-between rounded-lg border border-${col}/20 bg-${col}/5 px-3 py-2">
        <div>
          <span class="text-[10px] font-bold text-${col} uppercase tracking-wider">${_esc(btype)}</span>
          <span class="text-[10px] text-Cmuted ml-1">Batch Window</span>
        </div>
        <div class="text-right">
          <span class="text-sm font-extrabold text-${col}">${hrs}h</span>
          <span class="text-[10px] text-Cmuted ml-1">SLA ceiling</span>
        </div>
      </div>`;
    }).join("")
    : '<div class="text-[10px] text-Cmuted italic">No SLA windows extracted</div>';
    // Show Tier 2 active badge
    if (slaEntries.length) {
      document.getElementById("sow-sla-tier-badge")?.classList.remove("hidden");
    }
  }

  // ── 2A: Volume Ramp ──────────────────────────────────────────
  const volY = c.volume_by_year || {};
  const volEl = document.getElementById("sow-volume-rows");
  if (volEl) {
    const volEntries = Object.entries(volY);
    const maxVol = Math.max(...volEntries.map(([,v]) => v.item_locations || 0), 1);
    volEl.innerHTML = volEntries.length ? volEntries.map(([yr, v]) => {
      const n   = v.item_locations || 0;
      const pct = (n / maxVol * 100).toFixed(0);
      return `<div class="space-y-1">
        <div class="flex items-center justify-between text-[11px]">
          <span class="font-bold text-Cwhite">${_esc(yr)}</span>
          <span class="font-mono text-Cblue font-semibold">${n.toLocaleString()} <span class="text-Cmuted font-normal">${_esc(v.uom || "Item-Locations")}</span></span>
        </div>
        <div class="h-2 rounded-full bg-Cbg border border-Cborder overflow-hidden">
          <div class="h-full rounded-full bg-gradient-to-r from-Cblue to-Ccyan transition-all duration-700" style="width:${pct}%"></div>
        </div>
      </div>`;
    }).join("")
    : '<div class="text-[10px] text-Cmuted italic">No volume ramp data extracted</div>';
  }

  // ── 2B: Operational Standards ────────────────────────────────
  const dr = c.disaster_recovery || {};
  const opsEl = document.getElementById("sow-ops-rows");
  if (opsEl) {
    const rows = [];
    if (dr.level) {
      rows.push(["Disaster Recovery Level", dr.level, "Cpurple"]);
    }
    if (dr.rto_hours != null) {
      rows.push(["Recovery Time Objective (RTO)", `Up to ${dr.rto_hours} hours`, "Camber"]);
    }
    if (dr.rpo_hours != null) {
      rows.push(["Recovery Point Objective (RPO)", `${dr.rpo_hours} hour${dr.rpo_hours !== 1 ? "s" : ""} max data loss`, "Camber"]);
    }
    if (c.availability_sla_pct != null) {
      rows.push(["Availability Commitment", `${c.availability_sla_pct}% uptime per calendar month`, "Cgreen"]);
    }
    opsEl.innerHTML = rows.length ? rows.map(([label, value, col]) => `
      <div class="flex items-start gap-3 py-2 border-b border-Cborder/30 last:border-0">
        <span class="w-1.5 h-1.5 rounded-full bg-${col} mt-1.5 shrink-0"></span>
        <div class="flex-1 flex items-start justify-between gap-2">
          <span class="text-[10px] text-Cmuted">${_esc(label)}</span>
          <span class="text-[11px] font-semibold text-${col} text-right">${_esc(value)}</span>
        </div>
      </div>`).join("")
    : '<div class="text-[10px] text-Cmuted italic">No operational standards extracted</div>';
  }

  // ── Auto-fill DFU/SKU baseline inputs from volume ramp if empty ──────
  const volY2 = c.volume_by_year || {};
  const volEntries2 = Object.entries(volY2);
  if (volEntries2.length) {
    // Use the largest year's Item-Locations as the primary volume baseline
    const maxEntry = volEntries2.reduce((best, cur) =>
      (cur[1].item_locations || 0) > (best[1].item_locations || 0) ? cur : best
    , volEntries2[0]);
    const maxVol = maxEntry[1].item_locations || 0;
    if (maxVol > 0) {
      const dfuBaseline = document.getElementById("sow-dfu-baseline");
      const skuBaseline = document.getElementById("sow-sku-baseline");
      // Only auto-fill if field is empty (don't overwrite user input)
      if (dfuBaseline && !dfuBaseline.value) dfuBaseline.value = maxVol;
      // Only auto-fill SKU if the SOW contract actually mentions SKU data
      const hasSku = c.daily_sku || c.manual_sku_baseline || Object.values(volY2).some(v => v?.sku_count > 0);
      if (skuBaseline && !skuBaseline.value && hasSku) skuBaseline.value = c.daily_sku || Math.round(maxVol * 0.1);
    }
  }

  // ── Wire manual input changes back to appData.sowContract ────────────
  _bindSowManualInputs();

  // Render volume comparison bars if actuals already saved
  _renderSowVolumeComparison();

  // Store contract in appData so final judgment can use it
  window.appData = window.appData || {};
  window.appData.sowContract = contract;

  // Also refresh the SLA commitments panel in the SLA Matrix tab if already rendered
  _renderSlaCommitmentsPanel();
}

// ── Bind manual inputs → live appData + full pipeline propagation ─────────
let _sowManualBound = false;
let _sowSaveDebounce = null;
function _bindSowManualInputs() {
  if (_sowManualBound) return;
  _sowManualBound = true;

  const bindNum = (id, key, metricId) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("input", () => {
      const val = parseFloat(el.value);
      window.appData = window.appData || {};
      window.appData.sowContract = window.appData.sowContract || {};
      if (!isNaN(val) && val > 0) {
        window.appData.sowContract[key] = val;
      } else {
        delete window.appData.sowContract[key];
      }
      if (metricId) _updateSowMetricCard(metricId);
      _updateSowTargetCount();
      _renderSowVolumeComparison();
      _syncSowCompareFromManual();
      window._execCacheHash = null;
      if (_sowSaveDebounce) clearTimeout(_sowSaveDebounce);
      _sowSaveDebounce = setTimeout(() => {
        _persistSowBaseline();
        triggerGenerateFindings().catch(() => {});
        triggerPeNarrative().catch(() => {});
      }, 600);
    });
  };

  // Volume targets only
  bindNum("sow-dfu-baseline",      "manual_dfu_baseline",      "dfu");
  bindNum("sow-dfu-actual",        "manual_dfu_actual",        "dfu");
  bindNum("sow-sku-baseline",      "manual_sku_baseline",      "sku");
  bindNum("sow-sku-actual",        "manual_sku_actual",        "sku");
  bindNum("sow-orders-baseline",   "manual_orders_baseline",   "orders");
  bindNum("sow-orders-actual",     "manual_orders_actual",     "orders");
  bindNum("sow-batchjobs-baseline","manual_batchjobs_baseline","batchjobs");
  bindNum("sow-batchjobs-actual",  "manual_batchjobs_actual",  "batchjobs");
  bindNum("sow-users-baseline",    "manual_users_baseline",    "users");
  bindNum("sow-users-actual",      "manual_users_actual",      "users");
}

// ── Sync sowCompare from all manual inputs ────────────────────────────────
function _syncSowCompareFromManual() {
  const _n = (id) => parseFloat(document.getElementById(id)?.value) || 0;
  const sc = window.appData?.sowContract || {};

  const fields = [
    { key: "daily_dfu",    label: "Daily DFU",              base: _n("sow-dfu-baseline")      || sc.manual_dfu_baseline      || 0, act: _n("sow-dfu-actual")       || sc.manual_dfu_actual       || 0 },
    { key: "daily_sku",    label: "Daily SKU Count",        base: _n("sow-sku-baseline")      || sc.manual_sku_baseline      || 0, act: _n("sow-sku-actual")       || sc.manual_sku_actual       || 0 },
    { key: "daily_orders", label: "Daily Orders",           base: _n("sow-orders-baseline")   || sc.manual_orders_baseline   || 0, act: _n("sow-orders-actual")    || sc.manual_orders_actual    || 0 },
    { key: "batch_jobs",   label: "Batch Jobs / Day",       base: _n("sow-batchjobs-baseline")|| sc.manual_batchjobs_baseline|| 0, act: _n("sow-batchjobs-actual") || sc.manual_batchjobs_actual || 0 },
    { key: "peak_users",   label: "Peak Concurrent Users",  base: _n("sow-users-baseline")    || sc.manual_users_baseline    || 0, act: _n("sow-users-actual")     || sc.manual_users_actual     || 0 },
  ];
  const metrics = fields
    .filter(f => f.base > 0)
    .map(f => ({ key: f.key, label: f.label, sow: f.base, actual: f.act > 0 ? f.act : null }));

  if (!metrics.length) {
    window.appData = window.appData || {};
    window.appData.sowCompare = null;
    return;
  }
  window.appData = window.appData || {};
  window.appData.sowCompare = window.appData.sowCompare || {};
  window.appData.sowCompare.metrics = metrics;
  if (window.appData.sowContract) {
    window.appData.sowCompare._contract = window.appData.sowContract;
  }
}

// ── Persist baseline to backend (fire-and-forget) ────────────────────────
async function _persistSowBaseline() {
  const _n = (id) => { const v = parseFloat(document.getElementById(id)?.value); return isNaN(v) ? null : v; };
  const baseline = {};

  // Volume targets
  const nf = [
    ["sow-dfu-baseline",       "daily_dfu"],
    ["sow-sku-baseline",       "daily_sku"],
    ["sow-orders-baseline",    "daily_orders"],
    ["sow-batchjobs-baseline", "batch_jobs"],
    ["sow-users-baseline",     "peak_users"],
    ["sow-cpu-baseline",       "cpu_baseline_pct"],
    ["sow-mem-baseline",       "mem_baseline_pct"],
    ["sow-disk-baseline",      "disk_baseline_pct"],
  ];
  nf.forEach(([id, key]) => { const v = _n(id); if (v != null && v > 0) baseline[key] = v; });

  if (!Object.keys(baseline).length) return;
  try {
    await fetch("/api/sow/baseline", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(baseline),
    });
  } catch (_) {}

  // Also persist SLA windows if entered
  const daily   = _n("sow-sla-daily");
  const weekly  = _n("sow-sla-weekly");
  const monthly = _n("sow-sla-monthly");
  if (daily != null || weekly != null || monthly != null) {
    const winPayload = {};
    if (daily   != null && daily   > 0) winPayload.daily_hrs   = daily;
    if (weekly  != null && weekly  > 0) winPayload.weekly_hrs  = weekly;
    if (monthly != null && monthly > 0) winPayload.monthly_hrs = monthly;
    if (Object.keys(winPayload).length) {
      try {
        await fetch("/api/sow/sla-windows/manual", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(winPayload),
        });
      } catch (_) {}
    }
  }
}

// ── Build sow_compare from manual inputs (used by exec + findings payloads) ──
function _buildSowCompareFromManual() {
  const _n = (id) => parseFloat(document.getElementById(id)?.value) || 0;
  const sc = window.appData?.sowContract || {};

  const fields = [
    { key: "daily_dfu",    label: "Daily DFU",             base: _n("sow-dfu-baseline")       || sc.manual_dfu_baseline       || 0, act: _n("sow-dfu-actual")        || sc.manual_dfu_actual        || 0 },
    { key: "daily_sku",    label: "Daily SKU Count",       base: _n("sow-sku-baseline")       || sc.manual_sku_baseline       || 0, act: _n("sow-sku-actual")        || sc.manual_sku_actual        || 0 },
    { key: "daily_orders", label: "Daily Orders",          base: _n("sow-orders-baseline")    || sc.manual_orders_baseline    || 0, act: _n("sow-orders-actual")     || sc.manual_orders_actual     || 0 },
    { key: "batch_jobs",   label: "Batch Jobs / Day",      base: _n("sow-batchjobs-baseline") || sc.manual_batchjobs_baseline || 0, act: _n("sow-batchjobs-actual")  || sc.manual_batchjobs_actual  || 0 },
    { key: "peak_users",   label: "Peak Concurrent Users", base: _n("sow-users-baseline")     || sc.manual_users_baseline     || 0, act: _n("sow-users-actual")      || sc.manual_users_actual      || 0 },
  ];
  const metrics = fields.filter(f => f.base > 0).map(f => ({
    key: f.key, label: f.label, sow: f.base, actual: f.act > 0 ? f.act : null,
  }));
  return metrics.length ? { metrics } : null;
}

// ── Volume Analysis: synthesizes SOW contract + manual inputs + batch stats
// into a single normalized payload for the PE Findings data-volume section.
// Called from _buildFindingsPayload() and re-evaluated on every SOW change.
function _buildVolumeAnalysis() {
  const ad    = window.appData || {};
  const sc    = ad.sowContract   || {};
  const sowCmp = ad.sowCompare   || {};
  const bk    = ad.batch?.kpis   || {};
  const batchCov = ad.batch?.data_coverage || {};
  const _n = (id) => parseFloat(document.getElementById(id)?.value) || 0;

  // All manual baseline/actual fields
  const manualFields = [
    { key: "daily_dfu",    label: "Daily DFU",             base: _n("sow-dfu-baseline")       || sc.manual_dfu_baseline       || 0, act: _n("sow-dfu-actual")        || sc.manual_dfu_actual        || 0 },
    { key: "daily_sku",    label: "Daily SKU Count",       base: _n("sow-sku-baseline")       || sc.manual_sku_baseline       || 0, act: _n("sow-sku-actual")        || sc.manual_sku_actual        || 0 },
    { key: "daily_orders", label: "Daily Orders",          base: _n("sow-orders-baseline")    || sc.manual_orders_baseline    || 0, act: _n("sow-orders-actual")     || sc.manual_orders_actual     || 0 },
    { key: "batch_jobs",   label: "Batch Jobs / Day",      base: _n("sow-batchjobs-baseline") || sc.manual_batchjobs_baseline || 0, act: _n("sow-batchjobs-actual")  || sc.manual_batchjobs_actual  || 0 },
    { key: "peak_users",   label: "Peak Concurrent Users", base: _n("sow-users-baseline")     || sc.manual_users_baseline     || 0, act: _n("sow-users-actual")      || sc.manual_users_actual      || 0 },
  ];

  const hasPdfItems = !!(sowCmp.items?.length);
  const hasManual   = manualFields.some(f => f.base > 0);
  const hasContract = !!(sc.customer_name || sc.annual_fee || sc.sla_windows);

  if (!hasPdfItems && !hasManual && !hasContract) return null;

  const source = hasPdfItems ? "sow_pdf" : hasManual ? "manual" : "session_cache";
  const metrics = [];

  // Priority: SOW PDF compare items
  if (hasPdfItems) {
    for (const item of sowCmp.items) {
      const pct = item.pct || (item.actual && item.sow ? +(item.actual / item.sow * 100).toFixed(1) : null);
      const zone = item.zone || (pct != null
        ? (pct > 110 ? "EXCEEDS" : pct >= 70 ? "ACCEPTABLE" : "UNDER")
        : null);
      metrics.push({
        key:        item.metric?.toLowerCase().replace(/\s+/g, '_') || item.key || "metric",
        label:      item.metric || item.key || "Metric",
        contracted: item.sow    || null,
        actual:     item.actual || null,
        pct, zone,
        status:     item.status || null,
      });
    }
  }

  // Add manual fields not already covered by PDF
  for (const f of manualFields) {
    if (f.base <= 0) continue;
    if (metrics.some(m => m.key === f.key)) continue;  // already from PDF
    const pct = f.act > 0 ? +(f.act / f.base * 100).toFixed(1) : null;
    metrics.push({
      key: f.key, label: f.label,
      contracted: f.base, actual: f.act > 0 ? f.act : null,
      pct,
      zone: pct ? (pct > 110 ? "EXCEEDS" : pct >= 70 ? "ACCEPTABLE" : "UNDER") : null,
      source: "manual",
    });
  }

  // Batch stats for throughput ratio
  const totalRuns  = parseInt(bk.total_runs  || 0) || 0;
  const totalJobs  = parseInt(bk.total_jobs  || 0) || 0;
  const dateSpan   = parseInt(batchCov.date_span_days || bk.date_span_days || 0) || 1;
  const uniqueDates = ad.batch?.window?.length || dateSpan;

  // Contract summary fields from SOW PDF
  const contractStart  = sc.start_date    || sc.contract_start || null;
  const contractEnd    = sc.end_date      || sc.contract_end   || null;
  const annualFee      = sc.annual_fee    || null;
  const maxItemLoc     = sowCmp.max_item_locations || sc.max_item_locations || null;
  const volumeByYear   = sowCmp.volume_by_year    || sc.volume_by_year    || null;

  if (!metrics.length && !hasContract) return null;

  const dfuMetric = metrics.find(m => m.key?.includes("dfu"));
  const skuMetric = metrics.find(m => m.key?.includes("sku"));

  return {
    metrics,
    contracted_daily_dfu: dfuMetric?.contracted || null,
    actual_daily_dfu:     dfuMetric?.actual     || null,
    contracted_daily_sku: skuMetric?.contracted || null,
    actual_daily_sku:     skuMetric?.actual     || null,
    total_runs:           totalRuns,
    total_jobs:           totalJobs,
    date_span_days:       dateSpan,
    unique_run_dates:     uniqueDates,
    max_item_locations:   maxItemLoc,
    volume_by_year:       volumeByYear,
    annual_fee:           annualFee,
    contract_start:       contractStart,
    contract_end:         contractEnd,
    source,
    has_actuals:          metrics.some(m => m.actual != null),
    has_contracted:       metrics.some(m => m.contracted != null),
  };
}

// ── SOW Volume Comparison: visual red/green % achievement bars ──────────
function _renderSowVolumeComparison() {
  const panel = document.getElementById("sow-volume-comparison");
  if (!panel) return;

  const _n = (id) => parseFloat(document.getElementById(id)?.value) || 0;
  const sc  = window.appData?.sowContract || {};
  const metrics = [
    { label: "Daily DFU",             unit: "items",   baseline: _n("sow-dfu-baseline")       || sc.manual_dfu_baseline       || 0, actual: _n("sow-dfu-actual")        || sc.manual_dfu_actual        || 0 },
    { label: "Daily SKU Count",       unit: "SKUs",    baseline: _n("sow-sku-baseline")       || sc.manual_sku_baseline       || 0, actual: _n("sow-sku-actual")        || sc.manual_sku_actual        || 0 },
    { label: "Daily Orders",          unit: "orders",  baseline: _n("sow-orders-baseline")    || sc.manual_orders_baseline    || 0, actual: _n("sow-orders-actual")     || sc.manual_orders_actual     || 0 },
    { label: "Batch Jobs / Day",      unit: "jobs",    baseline: _n("sow-batchjobs-baseline") || sc.manual_batchjobs_baseline || 0, actual: _n("sow-batchjobs-actual")  || sc.manual_batchjobs_actual  || 0 },
    { label: "Peak Concurrent Users", unit: "users",   baseline: _n("sow-users-baseline")     || sc.manual_users_baseline     || 0, actual: _n("sow-users-actual")      || sc.manual_users_actual      || 0 },
  ];

  const rows = metrics.filter(m => m.baseline > 0 || m.actual > 0);
  if (!rows.length) { panel.classList.add("hidden"); return; }

  panel.classList.remove("hidden");
  panel.innerHTML = rows.map(m => {
    if (!m.baseline) return "";
    const pct     = m.actual > 0 ? Math.min((m.actual / m.baseline) * 100, 200) : 0;
    const achPct  = m.actual > 0 ? ((m.actual / m.baseline) * 100).toFixed(1) : "—";
    const over    = m.actual > m.baseline;
    const ok      = m.actual >= m.baseline * 0.9;
    const barCol  = over ? "bg-Camber" : ok ? "bg-Cgreen" : "bg-Cred";
    const textCol = over ? "text-Camber" : ok ? "text-Cgreen" : "text-Cred";
    const label   = over ? `+${(((m.actual/m.baseline)-1)*100).toFixed(1)}% over SOW` :
                    ok   ? `✅ ${achPct}% of SOW target` :
                           `⚠ ${achPct}% — below target`;
    const barW = Math.min(pct, 100);
    return `
      <div class="mb-3">
        <div class="flex items-center justify-between mb-1">
          <span class="text-[11px] font-semibold text-Cwhite">${_esc(m.label)}</span>
          <span class="text-[11px] font-bold ${textCol}">${m.actual > 0 ? m.actual.toLocaleString() : "—"} / ${m.baseline.toLocaleString()} ${m.unit}</span>
        </div>
        <div class="h-2 rounded-full bg-Cborder/30 overflow-hidden">
          <div class="h-2 rounded-full transition-all duration-300 ${barCol}" style="width:${barW}%"></div>
        </div>
        <div class="text-[10px] ${textCol} mt-0.5 font-medium">${label}</div>
        ${over ? `<div class="text-[9px] text-Camber/70 italic mt-0.5">Exceeding SOW commitment — verify capacity headroom</div>` : ""}
      </div>`;
  }).join("");
}

// ── Restore from API on page load ─────────────────────────────────────────
async function loadSowBaseline() {
  try {
    // Restore stored contract meta from backend
    const r = await fetch("/api/sow/sla-windows");
    if (r.ok) {
      const d = await r.json();
      const meta = d.contract_meta || {};
      const synth = {
        ...meta,
        sla_windows:    d.sla_windows    || {},
        volume_by_year: d.volume_by_year || {},
      };
      if (Object.keys(synth.sla_windows).length || meta.customer_name) {
        window.appData = window.appData || {};
        if (!window.appData.sowContract || !Object.keys(window.appData.sowContract).length) {
          window.appData.sowContract = synth;
        }
        _renderSowContractPanel(window.appData.sowContract);
      }
    }
    // Restore baseline form values — always override so stored values are always visible
    const rb = await fetch("/api/sow/baseline");
    if (rb.ok) {
      const data = await rb.json();
      let restored = 0;
      _SOW_FIELDS.forEach(({ key, baseId }) => {
        if (data[key] != null) {
          const el = document.getElementById(`${baseId}-baseline`);
          if (el) { el.value = data[key]; restored++; }
        }
      });
      if (restored > 0) {
        // Refresh all metric cards after restoring stored values
        ["dfu","sku","orders","batchjobs","users"].forEach(m => _updateSowMetricCard(m));
        _updateSowTargetCount();
        // If no PDF is loaded, surface the manual panel
        if (!window.appData?.sowContract?.customer_name) {
          document.getElementById("sow-empty")?.classList.add("hidden");
          document.getElementById("sow-manual-panel")?.classList.remove("hidden");
          _bindSowManualInputs();
        }
      }
    }
    _autoFillSowActuals();
    // Sync sowCompare from restored baseline so all consumers see SOW data
    _syncSowCompareFromManual();
  } catch (_) {}
}

function _autoFillSowActuals() {
  const batch = window.appData.batch;
  if (batch) {
    const jobsEl = document.getElementById("sow-batchjobs-actual");
    if (jobsEl && !jobsEl.value && batch.kpis?.total_jobs) {
      jobsEl.value = batch.kpis.total_jobs;
      _updateSowMetricCard("batchjobs");
    }
  }
}

// ── Update a single volume metric card's progress bar, % label, and badge ──
function _updateSowMetricCard(metricId) {
  const base = parseFloat(document.getElementById(`sow-${metricId}-baseline`)?.value) || 0;
  const act  = parseFloat(document.getElementById(`sow-${metricId}-actual`)?.value)   || 0;
  const barEl   = document.getElementById(`sow-bar-${metricId}`);
  const pctEl   = document.getElementById(`sow-pct-${metricId}`);
  const badgeEl = document.getElementById(`sow-badge-${metricId}`);
  const cardEl  = document.getElementById(`sow-card-${metricId}`);

  if (!base) {
    if (barEl)   { barEl.style.width = "0%"; barEl.style.background = "rgba(71,85,105,0.4)"; }
    if (pctEl)   { pctEl.textContent = "—"; pctEl.style.color = "#475569"; }
    if (badgeEl) { badgeEl.classList.add("hidden"); }
    if (cardEl)  { cardEl.style.borderColor = "rgba(59,130,246,0.16)"; }
    return;
  }
  if (!act) {
    if (barEl)   { barEl.style.width = "0%"; barEl.style.background = "rgba(71,85,105,0.4)"; }
    if (pctEl)   { pctEl.textContent = "—"; pctEl.style.color = "#475569"; }
    if (badgeEl) {
      badgeEl.textContent = "AWAITING";
      badgeEl.style.color = "#60a5fa";
      badgeEl.style.background = "rgba(59,130,246,0.1)";
      badgeEl.style.borderColor = "rgba(59,130,246,0.22)";
      badgeEl.classList.remove("hidden");
    }
    if (cardEl) { cardEl.style.borderColor = "rgba(59,130,246,0.28)"; }
    return;
  }

  const pct = (act / base) * 100;
  let color, gradStart, gradEnd, label;
  if (pct >= 100) {
    color = "#10b981"; gradStart = "rgba(16,185,129,0.55)"; gradEnd = "rgba(16,185,129,0.9)";
    label = "ON TARGET";
    if (cardEl) { cardEl.style.borderColor = "rgba(16,185,129,0.32)"; }
  } else if (pct >= 80) {
    color = "#f59e0b"; gradStart = "rgba(245,158,11,0.55)"; gradEnd = "rgba(245,158,11,0.9)";
    label = "NEAR TARGET";
    if (cardEl) { cardEl.style.borderColor = "rgba(245,158,11,0.32)"; }
  } else {
    color = "#f87171"; gradStart = "rgba(248,113,113,0.55)"; gradEnd = "rgba(248,113,113,0.9)";
    label = "UNDER";
    if (cardEl) { cardEl.style.borderColor = "rgba(248,113,113,0.32)"; }
  }

  if (barEl) {
    barEl.style.width = `${Math.min(pct, 100).toFixed(1)}%`;
    barEl.style.background = `linear-gradient(90deg,${gradStart},${gradEnd})`;
  }
  if (pctEl) {
    pctEl.textContent = `${pct.toFixed(1)}%`;
    pctEl.style.color = color;
  }
  if (badgeEl) {
    badgeEl.textContent = label;
    badgeEl.style.color = color;
    badgeEl.style.background = `${color}18`;
    badgeEl.style.borderColor = `${color}40`;
    badgeEl.classList.remove("hidden");
  }
}

// ── Update the "N of 5 set" count badge in the SOW panel header ─────────────
function _updateSowTargetCount() {
  const set = ["dfu","sku","orders","batchjobs","users"].filter(m => {
    const v = parseFloat(document.getElementById(`sow-${m}-baseline`)?.value);
    return !isNaN(v) && v > 0;
  }).length;
  const el = document.getElementById("sow-target-count");
  if (!el) return;
  if (set === 0) { el.classList.add("hidden"); return; }
  el.textContent = `${set} of 5 set`;
  el.classList.remove("hidden");
}

async function saveSowBaseline() {
  const baseline = {};
  const actuals  = {};
  _SOW_FIELDS.forEach(({ key, baseId }) => {
    const bVal = parseFloat(document.getElementById(`${baseId}-baseline`)?.value || "");
    const aVal = parseFloat(document.getElementById(`${baseId}-actual`)?.value   || "");
    if (!isNaN(bVal) && bVal > 0) baseline[key] = bVal;
    if (!isNaN(aVal) && aVal > 0) actuals[key]  = aVal;
  });

  if (Object.keys(baseline).length === 0) {
    toast("error", "No targets set", "Enter at least one SOW target value before comparing.");
    return;
  }
  const msgEl = document.getElementById("sow-save-msg");
  try {
    await fetch("/api/sow/baseline", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(baseline),
    });
    const res  = await fetch("/api/sow/compare", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actuals }),
    });
    const data = await res.json();
    window.appData = window.appData || {};
    window.appData.sowCompare = data;
    window._execCacheHash = null;
    _markSessionActive();
    refreshDataStatus();
    _renderSowComparison(data);
    triggerGenerateFindings().catch(() => {});
    triggerPeNarrative().catch(() => {});
    if (msgEl) {
      msgEl.textContent = "✅ Saved and compared";
      msgEl.className   = "text-[11px] font-semibold";
      msgEl.style.color = "#10b981";
      msgEl.classList.remove("hidden");
      setTimeout(() => msgEl.classList.add("hidden"), 3000);
    }
  } catch (err) {
    toast("error", "Error", String(err?.message || err));
  }
}

function _renderSowComparison(data) {
  // Always show grid (contains chart-wrap + table-wrap) and hide empty state
  document.getElementById("sow-empty")?.classList.add("hidden");
  document.getElementById("sow-contract-grid")?.classList.remove("hidden");
  document.getElementById("sow-chart-wrap")?.classList.remove("hidden");
  document.getElementById("sow-table-wrap")?.classList.remove("hidden");
  document.getElementById("sow-summary-banner")?.classList.remove("hidden");

  setText("sow-summary-text", data.summary || "");

  // Overall badge
  const badge = document.getElementById("sow-overall-badge");
  if (badge) {
    const cfg = {
      OPTIMAL:  { bg: "bg-Cgreen/20 border-Cgreen/40 text-Cgreen",  icon: "✅" },
      MODERATE: { bg: "bg-Camber/20 border-Camber/40 text-Camber",  icon: "⚠️" },
      LOW:      { bg: "bg-Cred/20 border-Cred/40 text-Cred",        icon: "🔴" },
      HIGH:     { bg: "bg-Cred/20 border-Cred/40 text-Cred",        icon: "🔴" },
      "N/A":    { bg: "bg-Cmuted/20 border-Cborder text-Cmuted",     icon: "—"  },
    }[data.overall_status] || { bg: "bg-Cmuted/20 border-Cborder text-Cmuted", icon: "?" };
    badge.innerHTML = `<span class="text-[11px] font-bold px-3 py-1 rounded-full border ${cfg.bg}">${cfg.icon} ${data.overall_status}</span>`;
  }

  // Bar chart
  const barsEl = document.getElementById("sow-bars-container");
  if (barsEl && data.metrics?.length) {
    barsEl.innerHTML = data.metrics.map((m) => {
      const pct    = Math.min(m.pct, 150);
      const barPct = (pct / 150 * 100).toFixed(1);
      // Standard: 70-110% = acceptable (green). <70% = amber, >110% = red.
      const color  = m.pct > 110                     ? "#f43f5e"  // exceeds
                   : m.pct >= 70                     ? "#22d3ee"  // within 70-110% window
                   : "#f59e0b";                                   // below 70% floor
      const statusBg = m.pct > 110                   ? "bg-Cred/20 text-Cred"
                     : m.pct >= 70                   ? "bg-Ccyan/20 text-Ccyan"
                     : "bg-Camber/20 text-Camber";
      return `<div class="space-y-1.5">
        <div class="flex items-center justify-between text-[11px]">
          <span class="font-semibold text-Cwhite">${_esc(m.label)}</span>
          <div class="flex items-center gap-2">
            <span class="text-Cmuted font-mono text-[10px]">${_fmt(m.actual)} / ${_fmt(m.sow)}</span>
            <span class="font-bold" style="color:${color}">${m.pct.toFixed(1)}%</span>
            <span class="text-[10px] font-bold px-1.5 py-0.5 rounded-full uppercase ${statusBg}">${m.status}</span>
          </div>
        </div>
        <div class="relative h-4 rounded-lg overflow-hidden bg-Cbg border border-Cborder">
          <!-- Zone bands: <70% amber, 70-110% green (full standard window), >110% red -->
          <div class="absolute inset-y-0 left-0 bg-Camber/20" style="width:${(70/150*100).toFixed(1)}%"></div>
          <div class="absolute inset-y-0 bg-Cgreen/15" style="left:${(70/150*100).toFixed(1)}%;width:${((110-70)/150*100).toFixed(1)}%"></div>
          <div class="absolute inset-y-0 bg-Cred/15" style="left:${(110/150*100).toFixed(1)}%;right:0"></div>
          <div class="absolute inset-y-0 w-px bg-white/30" style="left:${(100/150*100).toFixed(1)}%"></div>
          <div class="absolute inset-y-0 left-0 rounded-lg transition-all duration-700" style="width:${barPct}%;background:${color};opacity:0.8"></div>
        </div>
        <div class="flex justify-between text-[9px] text-Cmuted font-mono">
          <span>0</span><span class="text-Camber">70%</span><span>90%</span><span>100%</span><span class="text-Cred">110%</span><span>150%+</span>
        </div>
        ${m.pct > 110 || m.pct < 70 ? `<div class="text-[9px] text-Camber font-semibold">⚠ Outside 70%–110% standard process window — formal review &amp; acknowledgment required</div>` : ""}
      </div>`;
    }).join("");
  }

  // Table
  const tbody = document.getElementById("sow-table-tbody");
  if (tbody && data.metrics?.length) {
    tbody.innerHTML = data.metrics.map((m) => {
      const stBg = (m.status === "OPTIMAL" || m.status === "ACCEPTABLE") ? "bg-Ccyan/15 text-Ccyan" :
                   m.status === "LOW"                                     ? "bg-Camber/20 text-Camber" :
                   "bg-Cred/20 text-Cred";
      // Standard: 70-110% = green/cyan. <70% = amber, >110% = red.
      const pctColor = m.pct >= 70 && m.pct <= 110 ? "text-Ccyan font-bold" :
                       m.pct >= 70                  ? "text-Cred font-bold" : "text-Camber font-semibold";
      return `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
        <td class="py-2.5 pr-4 font-semibold text-Cwhite">${_esc(m.label)}</td>
        <td class="py-2.5 pr-4 text-right text-Cmuted font-mono">${_fmt(m.sow)}</td>
        <td class="py-2.5 pr-4 text-right font-mono text-Cwhite">${_fmt(m.actual)}</td>
        <td class="py-2.5 pr-4 text-right ${pctColor}">${m.pct.toFixed(1)}%</td>
        <td class="py-2.5 text-center"><span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase ${stBg}">${m.status}</span></td>
      </tr>`;
    }).join("");
  }
}

// ── Also wire Zone F uploads into the contract panel ─────────────────────
// (Zone F calls _renderSowIntakeCard which stores to appData.sowContract;
//  _renderSowContractPanel is called from there too — patch it here so both
//  paths lead to the same structured panel render)
const _orig_renderSowIntakeCard = typeof _renderSowIntakeCard === "function" ? _renderSowIntakeCard : null;

/** Format large numbers cleanly */
function _fmt(n) {
  if (n == null || isNaN(n)) return "—";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + "K";
  return Number(n).toFixed(n % 1 === 0 ? 0 : 1);
}


function _renderBenchIntakeCard(data, filename) {
  const card = document.getElementById("bench-result-card");
  if (!card) return;
  card.classList.remove("hidden");

  setText("bench-result-filename", filename || data.filename || "—");
  setText("bench-result-total", String(data.total_transactions));
  const degEl = document.getElementById("bench-result-degraded");
  if (degEl) {
    degEl.textContent = String(data.degraded);
    degEl.className   = `text-lg font-extrabold mt-0.5 ${data.degraded > 0 ? "text-Cred" : "text-Cgreen"}`;
  }
  const dEl = document.getElementById("bench-result-delta");
  if (dEl) {
    const v = data.avg_delta_pct || 0;
    dEl.textContent = (v > 0 ? "+" : "") + v.toFixed(1) + "%";
    dEl.className   = `text-lg font-extrabold mt-0.5 ${v > data.threshold_pct ? "text-Cred" : v > 0 ? "text-Camber" : "text-Cgreen"}`;
  }
}


// ---------------------------------------------------------------
//  AGENT � DEEP DIVE  (tool-using LLM panel)
// ---------------------------------------------------------------
