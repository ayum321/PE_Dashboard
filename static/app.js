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
  benchmark:     null,   // from /api/benchmark  (Phase 7)
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
// SLA daily limit — kept in sync with the backend config loaded in loadConfig().
// Use `let` so loadConfig() can update it when the user changes settings.
let SLA_DAILY_HRS = 6.0;

// Live Chart.js instances — re-created on every renderBatchReview() / renderResourceReview() call
const charts = { slaBuffer: null, windowTrend: null, topJobs: null, resourceBars: null };

// Latest KPIs for the SLA buffer gauge — kept so the canvas gauge can be
// redrawn crisply on browser resize / zoom (canvas does not auto-reflow).
let _lastBufferKpis = null;
let _bufferResizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(_bufferResizeTimer);
  _bufferResizeTimer = setTimeout(() => {
    if (_lastBufferKpis && document.getElementById("chart-sla-buffer")?.offsetParent) {
      renderSlaBufferChart(_lastBufferKpis);
    }
  }, 120);
});

// Resource Review · table view state
const resourceTableState = { showAll: false, filter: "", sortKey: "cpu_pct", sortDir: -1, filterType: "", filterEnv: "", filterStatus: "" };
const RESOURCE_TABLE_PREVIEW = 25; // initial lazy slice

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
  checkAzureIdentity();    // show logged-in Azure identity
  refreshAiStatus();      // header AI engine badge
  refreshAuditContext().catch(() => {}); // restore session-cache data on reload
  console.info("[pe-dashboard] full shell ready (phases 2-8)");
});


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
    if (view === "overview")     renderOverview();
    if (view === "insights")     triggerGenerateFindings();
    if (view === "redflags")     triggerRedFlags();
  } else if (view === "overview") {
    // Data might be in session cache — try restoring then render
    refreshAuditContext().then(() => {
      if (window.appData.batch || window.appData.resource || window.appData.servers?.length)
        renderOverview();
    }).catch(() => {});
  }

  // Audit context health bar — always refresh when entering PE Findings
  if (view === "insights")     refreshAuditContext();

  // New tab hooks
  if (view === "settings")   loadSettings();
  if (view === "slamatrix") { _renderSlaCommitmentsPanel(); if (window.appData.batch) triggerSlaMatrix(); }
  if (view === "sow")        { initSowTab(); loadSowBaseline(); }

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

  // Header chip
  const chip = document.getElementById("dataset-chip");
  if (chip) {
    chip.textContent = `${payload.filename} · ${payload.server_count} servers`;
    chip.classList.remove("hidden");
  }

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
  setText("batch-dataset-chip", `${payload.filename} · ${payload.kpis.total_runs.toLocaleString()} runs`);

  // Customer name — sourced ONLY from the Ctrl-M filename (server-side extraction).
  const cust = (payload.customer_name || "").trim();
  window.appData.customerName = cust;
  const totalRuns = payload?.kpis?.total_runs || 0;
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
    "Run: py -3.14 -m uvicorn main:app --host 127.0.0.1 --port 8765"
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
        "Start it with: py -3.14 -m uvicorn main:app --host 127.0.0.1 --port 8765");
    } else {
      toast("error", "Network error", msg.split("\n")[0]);
    }
    return { ok: false, status: 0, body: null };
  }
}

/** Show a persistent red banner at the top when the server is down. */
function _showServerDownBanner() {
  if (document.getElementById("server-down-banner")) return;
  const banner = document.createElement("div");
  banner.id = "server-down-banner";
  banner.className = "fixed top-0 left-0 right-0 z-[9999] bg-Cred/90 text-white text-xs font-semibold px-4 py-2 flex items-center justify-between gap-4";
  banner.innerHTML = `
    <span>⚠ Server unreachable — run <code class="bg-black/30 px-1 rounded">py -3.14 -m uvicorn main:app --host 127.0.0.1 --port 8765</code> then reload</span>
    <button onclick="this.parentElement.remove()" class="opacity-70 hover:opacity-100 text-lg leading-none">&times;</button>
  `;
  document.body.prepend(banner);
  // Auto-retry: poll /api/config every 5s and remove banner when server is back
  const poll = setInterval(async () => {
    try {
      const r = await fetch("/api/config", { signal: AbortSignal.timeout(2000) });
      if (r.ok) {
        clearInterval(poll);
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
      "Start it with: py -3.14 -m uvicorn main:app --host 127.0.0.1 --port 8765");
  } else {
    toast("error", "Network error", msg.split("\n")[0].slice(0, 200));
  }
}

function initResetButton() {
  // #reset-btn now calls clearSessionData() directly via onclick in HTML.
  // This function is kept as a no-op so the initResetButton() call at startup
  // doesn't throw a ReferenceError.
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

function toast(kind, title, message, ttlMs = 4500) {
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
    }
    chart.draw();
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
  Chart.defaults.animation = {
    duration: 600,
    easing: "easeOutQuart",
  };
  Chart.defaults.transitions = {
    active: { animation: { duration: 200 } },
    resize: { animation: { duration: 0 } },
  };
  Chart.defaults.elements.point.hitRadius = 6;
  Chart.defaults.elements.point.hoverRadius = 5;
  Chart.defaults.plugins.tooltip.animation = { duration: 150, easing: "easeOutQuart" };
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

  // Click-to-browse handled natively by the <label for="batch-file-input"> in HTML.
  // JS only needed for drag-and-drop.

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
    toast(
      "success",
      "Batch analysis complete",
      `${files.length} file(s) · ${payload.kpis.total_runs.toLocaleString()} runs · ${payload.kpis.total_jobs} jobs · ${_n(payload.kpis.compliance_pct).toFixed(1)}% compliant`
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
// Utility job exclusion state (persists across re-renders)
// ─────────────────────────────────────────────────────────────
let _batchExcludeUtility = true;           // auto-detected utility jobs excluded by default
let _batchManualExclude  = new Set();      // user explicitly excluded
let _batchManualInclude  = new Set();      // user overrode auto-exclude

/** Check if a job should be excluded from the current analysis. */
function _isJobExcluded(job) {
  const name = job.Job_Name || "";
  if (_batchManualExclude.has(name)) return true;   // manual always wins
  if (_batchManualInclude.has(name)) return false;  // manual include overrides auto
  return _batchExcludeUtility && !!job.is_utility;  // auto-detected utility
}

/** Shallow copy of batch payload with excluded jobs filtered out. */
function _filterBatchUtility(data) {
  return {
    ...data,
    top_jobs:     (data.top_jobs     || []).filter(j => !_isJobExcluded(j)),
    top_breaches: (data.top_breaches || []).filter(j => !_isJobExcluded(j)),
  };
}

/** Re-render batch review with current exclusion state (no new fetch needed). */
function _reRenderBatch() {
  if (window.appData?.batch) renderBatchReview(window.appData.batch);
}

// ─────────────────────────────────────────────────────────────
// renderBatchReview(payload) — main entrypoint after upload
// ─────────────────────────────────────────────────────────────
function renderBatchReview(data) {
  if (!data || !data.kpis) {
    console.warn("[pe-dashboard] renderBatchReview called with empty payload");
    return;
  }

  // Apply exclusion filter — filtered is used for charts/tables
  const filtered = _filterBatchUtility(data);

  // Reveal the body, hide empty state
  document.getElementById("batch-empty")?.classList.add("hidden");
  document.getElementById("batch-review-body")?.classList.remove("hidden");

  // Dataset chip
  const chip = document.getElementById("batch-dataset-chip");
  if (chip) {
    chip.textContent = `${data.filename} · ${data.kpis.total_runs.toLocaleString()} runs`;
    chip.classList.remove("hidden");
  }

  // ── Excluded jobs panel (per-job chips) ──────────────────────
  {
    const allJobs       = data.top_jobs || [];
    const autoUtilJobs  = allJobs.filter(j => j.is_utility);
    const excludedJobs  = allJobs.filter(j => _isJobExcluded(j));
    const includedBack  = autoUtilJobs.filter(j => _batchManualInclude.has(j.Job_Name));

    let utilPanel = document.getElementById("batch-utility-panel");
    if (!utilPanel) {
      utilPanel = document.createElement("div");
      utilPanel.id = "batch-utility-panel";
      const _srcWm = document.getElementById("batch-source-watermark");
      const insertTarget = _srcWm?.parentElement || document.getElementById("batch-review-body");
      if (insertTarget) {
        // IMPORTANT: chip (#batch-dataset-chip) is a nested <span> inside
        // #batch-loaded-chip — it is NOT a direct child of insertTarget.
        // Using chip.nextSibling as a reference node causes the
        // "Child to insert before is not a child of this node" crash.
        // Use #batch-review-body instead — it IS a direct child of insertTarget.
        const _batchBody = document.getElementById("batch-review-body");
        if (_batchBody && _batchBody.parentNode === insertTarget) {
          insertTarget.insertBefore(utilPanel, _batchBody);
        } else {
          insertTarget.appendChild(utilPanel);
        }
      }
    }

    if (autoUtilJobs.length > 0 || _batchManualExclude.size > 0) {
      const hasAnyExclusion = excludedJobs.length > 0 || _batchManualExclude.size > 0;

      // Build rows for the detection table — show ALL detected utility + manually excluded
      const manualOnlyJobs = (data.top_jobs || []).filter(
        j => !j.is_utility && _batchManualExclude.has(j.Job_Name)
      );
      const tableJobs = [...autoUtilJobs, ...manualOnlyJobs];

      const detectionRows = tableJobs.map(j => {
        const isExcluded   = _isJobExcluded(j);
        const isAuto       = !!j.is_utility;
        const reason       = j.utility_reason || "";
        const isManualExcl = _batchManualExclude.has(j.Job_Name);
        const isManualIncl = _batchManualInclude.has(j.Job_Name);

        const patternBadge = isAuto
          ? `<span class="inline-flex items-center px-1.5 py-0 rounded font-mono text-[8px]"
                  style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}"
                  title="Matched pattern: '${escapeHtml(reason)}'">${escapeHtml(reason) || "utility"}</span>`
          : `<span class="text-[8px] text-Cmuted font-mono">manual</span>`;

        const statusBadge = isExcluded
          ? `<span class="text-[8px] font-bold" style="color:${THEME.amber}">⊘ excluded</span>`
          : `<span class="text-[8px] font-bold" style="color:${THEME.green}">✓ included</span>`;

        const toggleBtn = isExcluded
          ? `<button class="util-toggle-btn text-[8px] px-2 py-0.5 rounded font-semibold hover:opacity-80 transition"
                    style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.1)};border:1px solid ${hexA(THEME.cyan,0.25)}"
                    data-util-include="${escapeHtml(j.Job_Name)}"
                    title="Include this job back in SLA analysis">Include</button>`
          : `<button class="util-toggle-btn text-[8px] px-2 py-0.5 rounded font-semibold hover:opacity-80 transition"
                    style="color:${THEME.amber};background:${hexA(THEME.amber,0.1)};border:1px solid ${hexA(THEME.amber,0.25)}"
                    data-util-exclude="${escapeHtml(j.Job_Name)}"
                    title="Exclude this job from SLA analysis">Exclude</button>`;

        const peak = typeof j.peak_hrs === "number" ? j.peak_hrs.toFixed(2) + "h" : "—";
        const rowStyle = isExcluded
          ? `opacity:0.55;background:${hexA(THEME.amber,0.04)}`
          : "";

        return `<tr style="${rowStyle}" class="border-t border-Cborder/30">
          <td class="px-2 py-1 font-mono text-[10px] text-Cmuted/90"
              style="${isExcluded ? "text-decoration:line-through" : ""}">
            ${escapeHtml(j.Job_Name)}
          </td>
          <td class="px-2 py-1 text-[9px]">${patternBadge}</td>
          <td class="px-2 py-1 text-right font-mono text-[9px] text-Cmuted">${peak}</td>
          <td class="px-2 py-1 text-center">${statusBadge}</td>
          <td class="px-2 py-1 text-right">${toggleBtn}</td>
        </tr>`;
      }).join("");

      utilPanel.className = "rounded-lg mt-2 overflow-hidden";
      utilPanel.style.cssText = `border:1px solid ${hexA(THEME.amber,0.3)};background:${hexA(THEME.amber,0.03)}`;
      utilPanel.innerHTML = `
        <div class="flex items-center justify-between px-3 py-2" style="background:${hexA(THEME.amber,0.07)}">
          <div class="flex items-center gap-2">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"
                 class="w-3.5 h-3.5" style="color:${THEME.amber}">
              <path stroke-linecap="round" stroke-linejoin="round"
                    d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"/>
            </svg>
            <span class="text-[10px] font-bold uppercase tracking-wider" style="color:${THEME.amber}">
              Detected Utility / Infrastructure Jobs
            </span>
            <span class="text-[9px] font-mono px-1.5 py-0.5 rounded"
                  style="color:${THEME.amber};background:${hexA(THEME.amber,0.18)}">
              ${excludedJobs.length} excluded · ${tableJobs.length - excludedJobs.length} included
            </span>
          </div>
          <div class="flex items-center gap-2">
            <span class="text-[8px] text-Cmuted">Pattern-matched against ${autoUtilJobs.length} job(s) — not SLA targets</span>
            ${hasAnyExclusion ? `<button id="batch-util-reset" class="text-[8px] px-2 py-0.5 rounded hover:opacity-80 transition"
                style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.1)};border:1px solid ${hexA(THEME.cyan,0.2)}">Reset all</button>` : ""}
          </div>
        </div>
        <table class="w-full">
          <thead>
            <tr style="background:${hexA(THEME.amber,0.04)}">
              <th class="px-2 py-1 text-left text-[9px] uppercase tracking-wider text-Cmuted font-semibold w-1/2">Job Name</th>
              <th class="px-2 py-1 text-left text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Detected Pattern</th>
              <th class="px-2 py-1 text-right text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Peak</th>
              <th class="px-2 py-1 text-center text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Status</th>
              <th class="px-2 py-1 text-right text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Action</th>
            </tr>
          </thead>
          <tbody>${detectionRows}</tbody>
        </table>
      `;

      // Wire toggle buttons
      utilPanel.querySelectorAll("[data-util-include]").forEach(el => {
        el.addEventListener("click", () => {
          const name = el.dataset.utilInclude;
          _batchManualExclude.delete(name);
          if (autoUtilJobs.some(j => j.Job_Name === name)) _batchManualInclude.add(name);
          _reRenderBatch();
        });
      });
      utilPanel.querySelectorAll("[data-util-exclude]").forEach(el => {
        el.addEventListener("click", () => {
          _batchManualExclude.add(el.dataset.utilExclude);
          _batchManualInclude.delete(el.dataset.utilExclude);
          _reRenderBatch();
        });
      });
      utilPanel.querySelector("#batch-util-reset")?.addEventListener("click", () => {
        _batchManualInclude.clear();
        _batchManualExclude.clear();
        _reRenderBatch();
      });
    } else {
      utilPanel.className = "hidden";
      utilPanel.innerHTML = "";
    }
  }

  // ── Schedule-based excluded sub_apps panel ───────────────────
  // Shows sub_apps removed from compliance scope because their schedule type
  // (CYCLIC, OUTBOUND, CALENDAR_BASED, MONTHLY, etc.) has no SLA window target.
  {
    const excludedSubs = (data.excluded_sub_apps || []).filter(x => x.reason !== "MANUAL");
    let subPanel = document.getElementById("batch-scope-exclusion-panel");
    if (!subPanel) {
      subPanel = document.createElement("div");
      subPanel.id = "batch-scope-exclusion-panel";
      const utilEl = document.getElementById("batch-utility-panel");
      const insertTarget = utilEl?.parentNode || document.getElementById("batch-review-body")?.parentElement;
      if (insertTarget) {
        const afterEl = document.getElementById("batch-utility-panel") || document.getElementById("batch-review-body");
        if (afterEl && afterEl.parentNode === insertTarget) {
          insertTarget.insertBefore(subPanel, afterEl.nextSibling);
        } else {
          insertTarget.appendChild(subPanel);
        }
      }
    }

    if (excludedSubs.length > 0) {
      // Schedule type → display info
      const schedMeta = {
        CYCLIC:               { icon: "🔄", label: "Cyclic / Polling",          desc: "High-frequency polling — no SLA window target",                color: THEME.purple },
        CYCLIC_INTERVAL:      { icon: "🔄", label: "Cyclic Interval",           desc: "Interval-based polling job",                                   color: THEME.purple },
        OUTBOUND:             { icon: "📤", label: "Outbound / EDI",            desc: "File delivery job — excluded from batch compliance",           color: THEME.cyan   },
        CALENDAR_BASED:       { icon: "📅", label: "Calendar (4-4-5)",          desc: "Retail calendar cycle — no standard SLA ceiling",              color: THEME.teal   },
        MONTHLY:              { icon: "📆", label: "Monthly",                   desc: "Monthly run — not a daily SLA window",                         color: THEME.muted  },
        BIMONTHLY:            { icon: "📆", label: "Bi-Monthly",                desc: "Bi-monthly run",                                               color: THEME.muted  },
        DATE_SPECIFIC_MONTHLY:{ icon: "📆", label: "Date-Specific Monthly",     desc: "Runs on specific date each month",                             color: THEME.muted  },
        QUARTERLY:            { icon: "📆", label: "Quarterly",                 desc: "Quarterly run — not a daily SLA window",                       color: THEME.muted  },
        PIPELINE_STAGE:       { icon: "⚙️", label: "Pipeline Stage",            desc: "Internal pipeline orchestration step",                         color: THEME.blue   },
        ADHOC:                { icon: "⚡", label: "Ad-Hoc",                   desc: "On-demand / manual trigger — no SLA window",                   color: THEME.amber  },
      };

      const rows = excludedSubs.map(x => {
        const m   = schedMeta[x.reason] || { icon: "–", label: x.reason, desc: "", color: THEME.muted };
        const jc  = x.job_count > 0 ? `${x.job_count} job${x.job_count > 1 ? "s" : ""}` : "—";
        const ph  = x.peak_hrs  > 0 ? x.peak_hrs.toFixed(2) + "h" : "—";
        return `<tr class="border-t border-Cborder/25 hover:bg-white/[0.02] transition-colors">
          <td class="px-3 py-1.5 font-mono text-[10px] text-Cwhite/80">${escapeHtml(x.sub_app)}</td>
          <td class="px-3 py-1.5">
            <span class="inline-flex items-center gap-1 px-1.5 py-0 rounded text-[9px] font-semibold"
                  style="color:${m.color};background:${hexA(m.color,0.12)};border:1px solid ${hexA(m.color,0.25)}"
                  title="${escapeHtml(m.desc)}">${m.icon} ${escapeHtml(m.label)}</span>
          </td>
          <td class="px-3 py-1.5 text-[9px] text-Cmuted">${escapeHtml(m.desc)}</td>
          <td class="px-3 py-1.5 text-right font-mono text-[9px] text-Cmuted">${jc}</td>
          <td class="px-3 py-1.5 text-right font-mono text-[9px] text-Cmuted">${ph}</td>
        </tr>`;
      }).join("");

      subPanel.className = "rounded-lg mt-2 overflow-hidden";
      subPanel.style.cssText = `border:1px solid ${hexA(THEME.cyan,0.2)};background:${hexA(THEME.cyan,0.025)}`;
      subPanel.innerHTML = `
        <div class="flex items-center justify-between px-3 py-2" style="background:${hexA(THEME.cyan,0.055)}">
          <div class="flex items-center gap-2">
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2"
                 stroke="currentColor" class="w-3.5 h-3.5 shrink-0" style="color:${THEME.cyan}">
              <path stroke-linecap="round" stroke-linejoin="round"
                    d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 5.25h.008v.008H12v-.008Z"/>
            </svg>
            <span class="text-[10px] font-bold uppercase tracking-wider" style="color:${THEME.cyan}">
              Sub-Apps Excluded from Compliance Scope
            </span>
            <span class="text-[9px] font-mono px-1.5 py-0.5 rounded"
                  style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.18)}">
              ${excludedSubs.length} sub-app${excludedSubs.length > 1 ? "s" : ""}
            </span>
          </div>
          <span class="text-[8px] text-Cmuted">Schedule type has no SLA window target — not counted in compliance %</span>
        </div>
        <table class="w-full">
          <thead>
            <tr style="background:${hexA(THEME.cyan,0.03)}">
              <th class="px-3 py-1 text-left text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Sub-Application</th>
              <th class="px-3 py-1 text-left text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Schedule Type</th>
              <th class="px-3 py-1 text-left text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Reason</th>
              <th class="px-3 py-1 text-right text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Jobs</th>
              <th class="px-3 py-1 text-right text-[9px] uppercase tracking-wider text-Cmuted font-semibold">Peak</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    } else {
      subPanel.className = "hidden";
      subPanel.innerHTML = "";
    }
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
      if (slaType === "sla_matrix") {
        const matchInfo = ms?.total_jobs > 0
          ? ` · ${ms.sla_matrix}/${ms.total_jobs} jobs matched`
          : "";
        slaSrcEl.textContent = `SLA: Customer Matrix (${slaSrc.filename || "uploaded"})${matchInfo}`;
        slaSrcEl.style.borderColor = THEME.green;
        slaSrcEl.style.color = THEME.green;
      } else {
        slaSrcEl.textContent = `SLA: System Default (${data.kpis.daily_limit_hrs || 6}h)`;
        slaSrcEl.style.borderColor = THEME.amber;
        slaSrcEl.style.color = THEME.amber;
      }
    }
  }

  // 1. KPI cards
  // Sync the global SLA_DAILY_HRS to the resolved ceiling from this dataset
  // so every chart/component that reads it reflects the actual customer SLA,
  // not the hardcoded 6h default.
  if (data.kpis?.daily_limit_hrs) {
    SLA_DAILY_HRS = Number(data.kpis.daily_limit_hrs) || SLA_DAILY_HRS;
  }
  renderBatchKpis(data.kpis);
  renderBatchLayerCards(data);
  renderBatchCoverageStrip(data.data_coverage || null);
  renderBatchDataWarnings(data.data_coverage?.warnings || []);
  renderBatchSlaSourceTags(data.sla_source || null, data.kpis);

  // 2. Charts
  renderSlaBufferChart(data.kpis);
  renderWindowTrendChart(data.window || []);
  renderTopJobsChart(filtered.top_jobs || [], filtered.kpis);

  // 3. Top 10 breaching jobs table
  // Pass allJobs (unfiltered) as the excluded context so user can see what was detected
  renderTopBreachesTable(filtered.top_breaches || [], filtered.kpis, data.top_jobs || []);

  // 4. Heatmaps (only shown when data is present)
  renderSlaHeatmap(data.sla_heatmap  || null);
  renderHourHeatmap(data.hour_heatmap || null);
}


// ── SLA source annotations on charts ──────────────────────────
function renderBatchSlaSourceTags(sla, kpis) {
  const tag1 = document.getElementById("chart-sla-source-tag");
  const tag2 = document.getElementById("chart-window-source-tag");
  const ceiling = document.getElementById("chart-sla-ceiling-tag");

  if (sla) {
    // Determine source label
    let srcLabel;
    if (sla.type === "sla_matrix") {
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
    const label = `SLA: ${srcLabel}${modelTag}${validTag}${matchTag} · Daily ${sla.daily_hrs?.toFixed(1) || "6.0"}h`;

    if (tag1) { tag1.textContent = label; tag1.classList.remove("hidden"); }
    if (tag2) { tag2.textContent = label; tag2.classList.remove("hidden"); }
    if (ceiling) { ceiling.textContent = `${sla.daily_hrs?.toFixed(1) || "6.0"} h`; }

    // Show blocked warning if SLA is assumed
    if (sla.blocked) {
      if (tag1) tag1.style.color = THEME.red;
      if (tag2) tag2.style.color = THEME.red;
    } else if (sla.type === "sla_matrix") {
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
  } else if (kpis) {
    if (ceiling) { ceiling.textContent = `${kpis.daily_limit_hrs?.toFixed(1) || "6.0"} h`; }
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
  const bwc = k.batch_window_compliance;
  const wbd = k.window_breach_days || 0;
  const wtd = k.window_total_days || 0;
  let compSub = `${k.jobs_ok} OK · ${k.total_jobs} total`;
  if (bwc != null) {
    compSub += ` · Window: ${_n(bwc).toFixed(0)}%`;
    if (wbd > 0) compSub += ` (${wbd}d breached)`;
  }
  setText("bk-compliance-sub", compSub);

  // Window SLA Rate — prominently displayed as its own KPI
  const winCompEl = document.getElementById("bk-window-compliance");
  if (winCompEl && bwc != null) {
    winCompEl.textContent = `${_n(bwc).toFixed(1)}%`;
    winCompEl.style.color =
      bwc >= 95 ? THEME.green :
      bwc >= 80 ? THEME.amber : THEME.red;
  }
  const winSubEl = document.getElementById("bk-window-compliance-sub");
  if (winSubEl) {
    if (bwc != null) {
      const passD = wtd - wbd;
      winSubEl.textContent = `${passD}/${wtd} days pass · ${wbd} breach`;
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

  // ── Elapsed Window ──
  const ewEl = document.getElementById("bk-elapsed");
  if (ewEl) {
    if (ew.available && ew.worst_day) {
      ewEl.textContent = `${_n(ew.worst_day.elapsed_hrs).toFixed(1)}h`;
      ewEl.style.color = ew.worst_day.elapsed_hrs > (data.kpis?.daily_limit_hrs || 6) ? THEME.red : THEME.purple;
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
      setText("bk-sla-source-sub",
        `Daily ${sla.daily_hrs?.toFixed(1) || "?"}h · Weekly ${sla.weekly_hrs?.toFixed(1) || "?"}h · High-confidence`
      );
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
  badge("cov-30day", "30-Day Evidence",
    span >= 30 ? "loaded" : span >= 14 ? "partial" : "missing");

  // SLA source quality from sla_source metadata
  const batchSla = (window.appData.batch || {}).sla_source || {};
  const slaStatus = batchSla.type === "sla_matrix" ? "customer" :
                    batchSla.type === "customer_fallback" ? "partial" : "default";
  badge("cov-sla", "SLA Source", slaStatus);

  badge("cov-confidence", `Confidence ${dc.confidence || 0}%`,
    dc.confidence >= 80 ? "loaded" : dc.confidence >= 60 ? "partial" : "missing");
  badge("cov-waivers", "Waivers", "missing");
  const hasSow = !!(window.appData.sowCompare || window.appData.sow);
  badge("cov-sow", "Volume vs SOW", hasSow ? "loaded" : "missing");
}


// ── Data Warnings ─────────────────────────────────────────────
function renderBatchDataWarnings(warnings) {
  const wrap = document.getElementById("batch-data-warnings");
  if (!wrap) return;
  if (!warnings || !warnings.length) { wrap.classList.add("hidden"); return; }

  wrap.classList.remove("hidden");
  wrap.innerHTML = warnings.map(w => {
    const sev = w.severity === "warning" ? THEME.amber : THEME.cyan;
    return `<div class="rounded-lg border-l-2 px-3 py-2 text-[11px]"
                 style="border-left-color:${sev};background:${hexA(sev, 0.06)}">
      <span style="color:${sev}" class="font-bold">${w.severity === "warning" ? "⚠️" : "ℹ️"}</span>
      <span class="text-Cmuted ml-1">${escapeHtml(w.text)}</span>
    </div>`;
  }).join("");
}


// ─────────────────────────────────────────────────────────────
// Chart 1 — SLA Buffer Gauge  (SVG arc speedometer, no Chart.js)
// ─────────────────────────────────────────────────────────────
function renderSlaBufferChart(k) {
  const canvas = document.getElementById("chart-sla-buffer");
  if (!canvas) return;
  destroyChart("slaBuffer");

  // Remember the latest KPIs so the gauge can be redrawn on resize / zoom.
  _lastBufferKpis = k;

  // Headline metric priority: WINDOW buffer (whole nightly batch window vs SLA)
  // → fleet buffer (worst single job vs SLA). The window buffer is the real
  // customer-facing SLA, so it leads when End_Time data is available.
  const winBuf  = k.window_sla_buffer;
  const buf      = winBuf || k.fleet_sla_buffer;
  const isWindow = !!winBuf;

  // ── Extract values ─────────────────────────────────────────
  // buffer_pct can be negative (BREACH). The gauge arc represents the SIGNED
  // buffer position clamped to the range [-100, +100] for display.
  const rawBuf   = buf ? buf.buffer_pct  : null;   // may be negative
  const status   = buf ? buf.status      : "NO DATA";
  // Sub-line context: window mode shows worst elapsed window; job mode shows
  // the worst single job's peak runtime.
  const peakHrs  = isWindow
                     ? (winBuf.worst_elapsed_hrs ?? null)
                     : (k.worst_job_peak ?? null);
  const slaHrs   = isWindow
                     ? (winBuf.sla_ceiling_hrs ?? SLA_DAILY_HRS)
                     : ((k.fleet_sla_buffer?.buffer_hrs != null && (k.worst_job_peak ?? null) != null)
                         ? +(k.worst_job_peak + k.fleet_sla_buffer.buffer_hrs).toFixed(2)
                         : (k.sla?.daily_limit_hrs ?? SLA_DAILY_HRS));
  const peakLabel = isWindow ? "Window" : "Peak";

  // Update subtitle text to match the active metric
  const _subtitleEl = document.getElementById("chart-sla-subtitle");
  if (_subtitleEl) {
    _subtitleEl.textContent = isWindow
      ? "Headroom between worst daily batch window and the SLA ceiling"
      : "Headroom between worst-job peak and the daily SLA limit";
  }

  // Map buffer_pct → needle angle on a 180° arc.
  // Arc spans -100% (left, 180°) through 0% (middle, 90°) to +100% (right, 0°).
  // Positive buffer = needle swings right (safe). Negative = swings left (breach).
  const clampedBuf   = rawBuf == null ? 0 : Math.max(-100, Math.min(100, rawBuf));
  // 0% = straight up (90°), +100% = full right (0°), -100% = full left (180°)
  const needleAngle  = 180 - ((clampedBuf + 100) / 200) * 180;  // degrees, 0=right

  // Zone colors (matching pe_config thresholds)
  const zoneColor =
    rawBuf == null            ? THEME.muted  :
    rawBuf < 0                ? THEME.red    :   // BREACH
    rawBuf <= 15              ? "#f97316"    :   // AT_RISK  (orange)
    rawBuf <= 40              ? THEME.amber  :   // LONG_JOB (amber)
                                THEME.green;     // OK

  const statusLabel =
    rawBuf == null ? "NO DATA" :
    rawBuf < 0     ? "BREACH"   :
    rawBuf <= 15   ? "AT RISK"  :
    rawBuf <= 40   ? "LONG JOB" : "HEALTHY";

  // Draw via canvas 2D (faster than SVG, same fidelity)
  const ctx   = canvas.getContext("2d");
  const dpr   = window.devicePixelRatio || 1;
  const W     = canvas.parentElement?.clientWidth  || 320;
  const H     = canvas.parentElement?.clientHeight || 256;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width  = W + "px";
  canvas.style.height = H + "px";
  ctx.scale(dpr, dpr);

  const cx    = W / 2;
  const cy    = H * 0.60;   // centre slightly below midpoint (semicircle sits above)
  const R     = Math.min(W * 0.34, H * 0.60);   // smaller gauge, more breathing room
  const thick = R * 0.17;

  // Helper: draw arc segment (degrees, 0=right, CCW)
  const arcDeg = (start, end, color, width, glow = false) => {
    const s = (start - 90) * Math.PI / 180;
    const e = (end   - 90) * Math.PI / 180;
    ctx.save();
    if (glow) {
      ctx.shadowColor = color;
      ctx.shadowBlur  = 14;
    }
    ctx.beginPath();
    ctx.arc(cx, cy, R - thick / 2, s, e);
    ctx.strokeStyle = color;
    ctx.lineWidth   = width;
    ctx.lineCap     = "butt";
    ctx.stroke();
    ctx.restore();
  };

  // ── Background track ────────────────────────────────────────
  arcDeg(180, 360, hexA(THEME.border, 0.45), thick);

  // ── Colored zone arcs (BREACH → AT_RISK → LONG_JOB → OK) ──
  // Degrees: 180° (left) = -100%, 270° (top) = 0%, 360° (right) = +100%
  // BREACH zone: -100% → 0% = 180° → 270°  (quarter left)
  // AT_RISK:      0% → 15% = 270° → 297°
  // LONG_JOB:    15% → 40% = 297° → 342°
  // OK:          40% → 100% = 342° → 360°
  arcDeg(180, 270, hexA(THEME.red,   0.25), thick);   // BREACH zone
  arcDeg(270, 297, hexA("#f97316",   0.25), thick);   // AT_RISK zone
  arcDeg(297, 342, hexA(THEME.amber, 0.25), thick);   // LONG_JOB zone
  arcDeg(342, 360, hexA(THEME.green, 0.25), thick);   // OK zone

  // ── Zone boundary ticks ─────────────────────────────────────
  const drawTick = (pct, label) => {
    const ang  = (180 + (pct + 100) / 200 * 180) - 180;  // map to 180→360
    const rad  = (ang - 90) * Math.PI / 180;
    const rIn  = R - thick - 4;
    const rOut = R + 6;
    ctx.save();
    ctx.strokeStyle = hexA(THEME.border, 0.7);
    ctx.lineWidth   = 1;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(cx + rIn  * Math.cos(rad), cy + rIn  * Math.sin(rad));
    ctx.lineTo(cx + rOut * Math.cos(rad), cy + rOut * Math.sin(rad));
    ctx.stroke();
    ctx.setLineDash([]);
    if (label) {
      const rLbl = R + 18;
      ctx.fillStyle = hexA(THEME.muted, 0.75);
      ctx.font = `500 9px "Sora", sans-serif`;
      ctx.textAlign   = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, cx + rLbl * Math.cos(rad), cy + rLbl * Math.sin(rad));
    }
    ctx.restore();
  };
  drawTick(0,  "0%");
  drawTick(15, "15%");
  drawTick(40, "40%");

  // ── Active fill arc (from 0% baseline toward current value) ─
  if (rawBuf != null) {
    const startDeg = clampedBuf >= 0 ? 270 : 180 + ((clampedBuf + 100) / 100) * 90;
    const endDeg   = clampedBuf >= 0 ? 270 + (clampedBuf / 100) * 90 : 270;
    if (Math.abs(endDeg - startDeg) > 0.5) {
      arcDeg(Math.min(startDeg, endDeg), Math.max(startDeg, endDeg),
             zoneColor, thick, true);
    }
  }

  // ── Zone labels at arc midpoints ────────────────────────────
  const zoneLabels = [
    { mid: 225, text: "BREACH", c: THEME.red },
    { mid: 283, text: "AT RISK", c: "#f97316" },
    { mid: 320, text: "LONG JOB", c: THEME.amber },
    { mid: 351, text: "OK", c: THEME.green },
  ];
  zoneLabels.forEach(({ mid, text, c }) => {
    const rad   = (mid - 90) * Math.PI / 180;
    const rLbl  = R - thick * 0.5;
    ctx.save();
    ctx.fillStyle = hexA(c, 0.65);
    ctx.font = `700 7.5px "Sora", sans-serif`;
    ctx.textAlign    = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, cx + rLbl * Math.cos(rad), cy + rLbl * Math.sin(rad));
    ctx.restore();
  });

  // ── Needle ───────────────────────────────────────────────────
  if (rawBuf != null) {
    const needleRad = (needleAngle - 90) * Math.PI / 180;
    const tipDist   = R - thick - 2;
    const hubR      = thick * 0.38;
    // Needle shadow / glow
    ctx.save();
    ctx.shadowColor = zoneColor;
    ctx.shadowBlur  = 12;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + tipDist * Math.cos(needleRad), cy + tipDist * Math.sin(needleRad));
    ctx.strokeStyle = zoneColor;
    ctx.lineWidth   = 3;
    ctx.lineCap     = "round";
    ctx.stroke();
    ctx.restore();
    // Hub dot
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, hubR, 0, Math.PI * 2);
    ctx.fillStyle = zoneColor;
    ctx.shadowColor = zoneColor;
    ctx.shadowBlur  = 8;
    ctx.fill();
    ctx.restore();
  }

  // ── Centre text ──────────────────────────────────────────────
  ctx.save();
  ctx.textAlign = "center";

  // Big number
  const bigText = rawBuf == null ? "N/A" : (rawBuf > 0 ? "+" : "") + rawBuf.toFixed(1) + "%";
  ctx.fillStyle = rawBuf == null ? THEME.muted : zoneColor;
  ctx.shadowColor = rawBuf == null ? "transparent" : zoneColor;
  ctx.shadowBlur  = rawBuf == null ? 0 : 18;
  ctx.font = `800 ${Math.round(R * 0.28)}px "Sora", sans-serif`;
  ctx.textBaseline = "alphabetic";
  ctx.fillText(bigText, cx, cy - R * 0.05);
  ctx.shadowBlur = 0;

  // Status label
  ctx.fillStyle = hexA(zoneColor, 0.9);
  ctx.font = `700 ${Math.round(R * 0.115)}px "Sora", sans-serif`;
  ctx.textBaseline = "top";
  ctx.fillText(statusLabel, cx, cy - R * 0.01);

  // Window / Peak vs SLA sub-line
  if (peakHrs != null) {
    ctx.fillStyle = hexA(THEME.muted, 0.75);
    ctx.font = `500 ${Math.round(R * 0.105)}px "Sora", sans-serif`;
    ctx.textBaseline = "top";
    ctx.fillText(`${peakLabel} ${peakHrs.toFixed(2)}h  ·  SLA ${(+slaHrs).toFixed(1)}h`, cx, cy + R * 0.15);
  }
  ctx.restore();

  // ── Legend ───────────────────────────────────────────────────
  const legendItems = [
    { c: THEME.green,  l: "OK (>40%)" },
    { c: THEME.amber,  l: "Long Job (15–40%)" },
    { c: "#f97316",    l: "At Risk (0–15%)" },
    { c: THEME.red,    l: "Breach (<0%)" },
  ];
  const lgY   = cy + R * 0.38;
  const lgW   = 8;
  let   lgX   = cx - (legendItems.length * 80) / 2;
  ctx.save();
  legendItems.forEach(({ c, l }) => {
    ctx.fillStyle = hexA(c, 0.9);
    ctx.fillRect(lgX, lgY, lgW, lgW);
    ctx.fillStyle = hexA(THEME.muted, 0.8);
    ctx.font = `500 8.5px "Sora", sans-serif`;
    ctx.textAlign    = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(l, lgX + lgW + 4, lgY + lgW / 2);
    lgX += ctx.measureText(l).width + lgW + 16;
  });
  ctx.restore();
}


// ─────────────────────────────────────────────────────────────
// Chart 2 — Daily Window Trend  (Grafana-style ambient bar chart)
// ─────────────────────────────────────────────────────────────
function renderWindowTrendChart(winData) {
  const canvas = document.getElementById("chart-window-trend");
  if (!canvas) return;
  destroyChart("windowTrend");

  const labels   = winData.map((w) => w.run_date);
  const counts   = winData.map((w) => w.job_count);
  const topJobs  = winData.map((w) => w.top_job || "");
  const rawSums  = winData.map((w) => +(w.total_hrs  || 0));
  const rawElaps = winData.map((w) => +(w.elapsed_hrs || 0));

  // Prefer elapsed_hrs (wall-clock) over summed (parallel-inflated)
  const hasElapsed = rawElaps.some(v => v > 0);
  const values  = winData.map((_, i) => hasElapsed && rawElaps[i] > 0 ? rawElaps[i] : rawSums[i]);

  // Grafana-style: color each bar by its SLA zone
  // OK → cyan-teal, LONG_JOB → amber, AT_RISK → orange, BREACH → red
  const barColors = values.map(v =>
    v > SLA_DAILY_HRS           ? THEME.red    :
    v > SLA_DAILY_HRS * 0.85   ? "#f97316"    :
    v > SLA_DAILY_HRS * 0.60   ? THEME.amber  :
                                  THEME.teal
  );

  // Update subtitle
  const metricTypeEl = document.getElementById("chart-window-metric-type");
  if (metricTypeEl) {
    metricTypeEl.textContent = hasElapsed
      ? "Elapsed window (first start → last end per day)"
      : "Summed runtime (all jobs — may overcount parallel runs)";
    metricTypeEl.className = hasElapsed
      ? "text-[9px] text-Cteal font-semibold mt-0.5"
      : "text-[9px] text-Camber font-semibold mt-0.5";
  }

  // Peak bar index
  let peakIdx = 0, peakVal = 0;
  values.forEach((v, i) => { if (v > peakVal) { peakVal = v; peakIdx = i; } });

  const TOP_N = 5;
  const topNIdx = new Set(
    values.map((v, i) => ({ v, i })).sort((a, b) => b.v - a.v).slice(0, TOP_N).map(x => x.i)
  );
  const breachCount = winData.filter(w => w.breach).length;

  // ── SLA zone band plugin ─────────────────────────────────────
  // Draws translucent colored bands for each SLA zone — like Grafana threshold bands.
  const zoneBandPlugin = {
    id: "zoneBands",
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea, scales } = chart;
      if (!chartArea || !scales.y) return;
      const { left, right } = chartArea;
      const toY = v => scales.y.getPixelForValue(v);
      const maxY = scales.y.max;

      const bands = [
        { from: SLA_DAILY_HRS,            to: maxY,              color: THEME.red,   alpha: 0.06, label: "BREACH" },
        { from: SLA_DAILY_HRS * 0.85,     to: SLA_DAILY_HRS,    color: "#f97316",   alpha: 0.05, label: "AT RISK" },
        { from: SLA_DAILY_HRS * 0.60,     to: SLA_DAILY_HRS * 0.85, color: THEME.amber, alpha: 0.04, label: "LONG JOB" },
        { from: 0,                         to: SLA_DAILY_HRS * 0.60, color: THEME.teal, alpha: 0.03, label: "OK" },
      ];
      ctx.save();
      bands.forEach(({ from, to, color, alpha, label }) => {
        const y0 = toY(Math.min(to,   maxY));
        const y1 = toY(Math.max(from, 0));
        if (y1 <= y0) return;
        ctx.fillStyle = hexA(color, alpha);
        ctx.fillRect(left, y0, right - left, y1 - y0);
        // Zone label on the right edge
        ctx.fillStyle = hexA(color, 0.28);
        ctx.font = `700 8px "Sora", sans-serif`;
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(label, right - 4, (y0 + y1) / 2);
      });
      ctx.restore();
    },
  };

  // ── Glow bar + labels plugin ─────────────────────────────────
  const breachCount = winData.filter(w => w.breach).length;
  const glowLabelPlugin = {
    id: "glowLabel",
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(0);
      if (!meta) return;
      const ctx = chart.ctx;
      ctx.save();

      winData.forEach((w, i) => {
        if (!meta.data[i]) return;
        const bar    = meta.data[i];
        const isTop  = topNIdx.has(i);
        const isPeak = i === peakIdx;
        const v      = values[i];
        const col    = barColors[i];
        const isBreach = w.breach;

        // Ambient glow on every bar
        ctx.save();
        ctx.shadowColor = hexA(col, isBreach ? 0.55 : 0.30);
        ctx.shadowBlur  = isBreach ? 12 : 6;
        ctx.fillStyle   = hexA(col, isBreach ? 0.92 : 0.75);
        const bw  = bar.width;
        const bh  = chart.chartArea.bottom - bar.y;
        ctx.beginPath();
        ctx.roundRect
          ? ctx.roundRect(bar.x - bw/2, bar.y, bw, bh, [3, 3, 0, 0])
          : ctx.rect(bar.x - bw/2, bar.y, bw, bh);
        ctx.fill();
        ctx.restore();

        // Breach column full-width tint
        if (isBreach) {
          ctx.fillStyle = hexA(THEME.red, 0.07);
          ctx.fillRect(bar.x - bar.width/2 - 2, chart.chartArea.top,
                       bar.width + 4, chart.chartArea.bottom - chart.chartArea.top);
        }

        // Value labels
        // Peak → ▲ marker + value + job name (always)
        // Non-peak breach → value only when few breach days (≤5) and differs from peak
        //   (avoids printing "11.0h" on 15 identical breach bars)
        // Non-breach top-N → value only (gives scale context)
        ctx.textAlign = "center";
        if (isPeak) {
          ctx.fillStyle = THEME.amber;
          ctx.font = '700 7px "Sora", sans-serif';
          ctx.fillText("▲ worst", bar.x, bar.y - 18);
          const jobLabel = topJobs[i]
            ? (topJobs[i].length > 13 ? topJobs[i].slice(0, 11) + "…" : topJobs[i])
            : null;
          ctx.fillStyle = isBreach ? THEME.red : hexA(THEME.white, 0.95);
          ctx.font = `bold 12px "Sora", sans-serif`;
          ctx.fillText(v.toFixed(1) + "h", bar.x, bar.y - (jobLabel ? 9 : 4));
          if (jobLabel) {
            ctx.fillStyle = isBreach ? hexA(THEME.red, 0.85) : hexA(THEME.muted, 0.95);
            ctx.font = '600 10px "Sora", sans-serif';
            ctx.fillText(jobLabel, bar.x, bar.y + 5);
          }
        } else if (isBreach && breachCount <= 5 && Math.abs(v - peakVal) > 0.05) {
          // Few distinct breach days — worth labelling individually
          ctx.fillStyle = hexA(THEME.red, 0.85);
          ctx.font = '600 10.5px "Sora", sans-serif';
          ctx.fillText(v.toFixed(1) + "h", bar.x, bar.y - 4);
        } else if (!isBreach && isTop) {
          // OK/AT_RISK top-N bars — show value for scale context
          ctx.fillStyle = hexA(THEME.white, 0.80);
          ctx.font = '600 10px "Sora", sans-serif';
          ctx.fillText(v.toFixed(1) + "h", bar.x, bar.y - 4);
        }

        // Failure ✕ marker
        if (w.has_failures) {
          const failLabel = w.fail_count > 1 ? `✕${w.fail_count}` : "✕";
          ctx.fillStyle = THEME.red;
          ctx.font = 'bold 9px "Sora", sans-serif';
          ctx.fillText(failLabel, bar.x, Math.min(bar.y - 1, chart.chartArea.bottom - 8) - 11);
        }
      });

      // Summary breach count top-right
      if (breachCount > 0) {
        ctx.fillStyle = hexA(THEME.red, 0.9);
        ctx.font = 'bold 10px "Sora", sans-serif';
        ctx.textAlign = "right";
        ctx.fillText(
          `${breachCount}/${winData.length} days breached (${Math.round(breachCount / winData.length * 100)}%)`,
          chart.chartArea.right - 4, chart.chartArea.top + 12
        );
      }
      ctx.restore();
    },
  };

  // ── SLA ceiling line plugin ──────────────────────────────────
  const slaGlowLinePlugin = {
    id: "slaGlowLine",
    afterDatasetsDraw(chart) {
      const yScale = chart.scales.y;
      if (!yScale) return;
      const y = yScale.getPixelForValue(SLA_DAILY_HRS);
      const { left, right } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      // Glow outer
      ctx.shadowColor = THEME.red;
      ctx.shadowBlur  = 8;
      ctx.strokeStyle = hexA(THEME.red, 0.65);
      ctx.lineWidth   = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      // Label
      ctx.shadowBlur  = 0;
      ctx.fillStyle   = THEME.red;
      ctx.font        = '700 11px "Sora", sans-serif';
      ctx.textAlign   = "left";
      ctx.fillText(`SLA ${SLA_DAILY_HRS}h ceiling`, left + 6, y - 5);
      ctx.restore();
    },
  };

  charts.windowTrend = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: hasElapsed ? "Elapsed Window (h)" : "Daily Total (h)",
        data: values,
        // Transparent — glowLabelPlugin redraws bars with glow
        backgroundColor: "transparent",
        borderColor:     "transparent",
        borderWidth: 0,
        borderRadius: 4,
        barPercentage: 0.82,
        categoryPercentage: 0.88,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 28, right: 16 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: hexA(THEME.card, 0.97),
          borderColor:     hexA(THEME.border, 0.8),
          borderWidth: 1,
          titleColor: THEME.white,
          bodyColor:  THEME.muted,
          padding: 10,
          displayColors: false,
          titleFont:  { family: "Sora", size: 11, weight: "700" },
          bodyFont:   { family: "Sora", size: 10 },
          callbacks: {
            title: (items) => labels[items[0].dataIndex],
            label: (ctx) => {
              const i = ctx.dataIndex;
              const lines = [];
              if (hasElapsed && rawElaps[i] > 0) {
                lines.push(`Elapsed window : ${rawElaps[i].toFixed(2)}h`);
                lines.push(`Summed runtime : ${rawSums[i].toFixed(2)}h`);
              } else {
                lines.push(`Summed runtime : ${rawSums[i].toFixed(2)}h`);
              }
              lines.push(`Jobs           : ${counts[i]}`);
              if (topJobs[i]) lines.push(`Top job        : ${topJobs[i]}`);
              lines.push(`SLA ceiling    : ${SLA_DAILY_HRS}h`);
              const buf = SLA_DAILY_HRS - values[i];
              lines.push(`Buffer         : ${buf >= 0 ? "+" : ""}${buf.toFixed(2)}h (${((buf / SLA_DAILY_HRS) * 100).toFixed(0)}%)`);
              if (winData[i].breach) lines.push("⚠ SLA BREACH");
              if (winData[i].has_failures) lines.push(`✕ ${winData[i].fail_count ?? 1} job failure(s)`);
              return lines;
            },
          },
        },
        zoom: _zoomConfig({ mode: "x" }),
      },
      scales: {
        x: {
          ticks: {
            color: hexA(THEME.muted, 0.95),
            font: { family: "Sora", size: 11, weight: "600" },
            maxRotation: winData.length > 12 ? 45 : 0,
            minRotation: winData.length > 12 ? 30 : 0,
            maxTicksLimit: winData.length > 20 ? 15 : undefined,
            autoSkipPadding: 8,
          },
          grid: { color: hexA(THEME.border, 0.25), drawBorder: false },
        },
        y: {
          beginAtZero: true,
          suggestedMax: SLA_DAILY_HRS * 1.35,
          title: {
            display: true,
            text: hasElapsed ? "Elapsed hrs (wall-clock)" : "Hours (summed)",
            color: hexA(THEME.muted, 0.95),
            font: { family: "Sora", size: 11, weight: "600" },
          },
          ticks: {
            color: hexA(THEME.muted, 0.95),
            font: { family: "Sora", size: 11 },
            callback: v => v.toFixed(0) + "h",
          },
          grid: { color: hexA(THEME.border, 0.20), drawBorder: false },
        },
      },
    },
    plugins: [zoneBandPlugin, glowLabelPlugin, slaGlowLinePlugin, crosshairPlugin],
  });

  _addChartToolbar(canvas.parentElement, charts.windowTrend, () => {
    let csv = "Date,Window_Hrs,Summed_Hrs,Job_Count,Breach,Top_Job\n";
    winData.forEach((w, i) => {
      csv += `${w.run_date},${values[i].toFixed(2)},${rawSums[i].toFixed(2)},${counts[i]},${w.breach || false},${topJobs[i]}\n`;
    });
    return csv;
  });
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
      ctx.strokeStyle = THEME.red;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = THEME.red;
      ctx.font = '700 10px "Sora", sans-serif';
      ctx.textAlign = "left";
      ctx.fillText(`${slaHrs}h SLA`, left + 6, y - 4);
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
function renderTopBreachesTable(rows, kpis, allJobs) {
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

  // Update title / subtitle dynamically
  if (title) {
    title.textContent = isFallback
      ? "Top 10 Jobs by Peak Runtime"
      : `Top ${trueBreaches.length} Breaching Jobs`;
  }
  if (subtitle) {
    subtitle.textContent = isFallback
      ? "No SLA breaches — showing ranked jobs by peak runtime"
      : `${trueBreaches.length} job(s) exceeded their SLA window`;
  }
  // Badge
  if (badge) {
    if (!isFallback) {
      badge.textContent  = `${trueBreaches.length} breach${trueBreaches.length !== 1 ? "es" : ""}`;
      badge.className    = "metric-badge metric-badge-red";
      badge.classList.remove("hidden");
    } else {
      badge.classList.add("hidden");
    }
  }

  empty?.classList.add("hidden");
  wrap?.classList.remove("hidden");

  // Add "Show excluded" toggle button into the table header area if there are excluded jobs
  const excludedInAll = (allJobs || []).filter(j => _isJobExcluded(j));
  let showExcludedToggle = wrap?.querySelector("#show-excluded-toggle");
  if (excludedInAll.length > 0) {
    if (!showExcludedToggle) {
      showExcludedToggle = document.createElement("button");
      showExcludedToggle.id = "show-excluded-toggle";
      showExcludedToggle.dataset.showExcluded = "false";
      // Insert near the table title area
      const titleEl = document.getElementById("top-jobs-title");
      titleEl?.parentElement?.parentElement?.insertAdjacentElement("afterend", showExcludedToggle)
        ?? wrap?.insertAdjacentElement("beforebegin", showExcludedToggle);
    }
    const showing = showExcludedToggle.dataset.showExcluded === "true";
    showExcludedToggle.className = "text-[9px] font-semibold px-2 py-1 rounded transition mb-1";
    showExcludedToggle.style.cssText = showing
      ? `color:${THEME.amber};background:${hexA(THEME.amber,0.12)};border:1px solid ${hexA(THEME.amber,0.3)}`
      : `color:${THEME.muted || "#6b7280"};background:transparent;border:1px solid ${hexA(THEME.amber,0.2)}`;
    showExcludedToggle.textContent = showing
      ? `⊘ Hiding ${excludedInAll.length} excluded job(s) — click to show`
      : `⊘ ${excludedInAll.length} excluded job(s) hidden — click to show`;
    showExcludedToggle.onclick = () => {
      showExcludedToggle.dataset.showExcluded =
        showExcludedToggle.dataset.showExcluded === "true" ? "false" : "true";
      renderTopBreachesTable(rows, kpis, allJobs);
    };
  } else if (showExcludedToggle) {
    showExcludedToggle.remove();
    showExcludedToggle = null;
  }

  const showExcluded = showExcludedToggle?.dataset.showExcluded === "true";

  const statusClass = (status) => {
    switch ((status || "").toUpperCase()) {
      case "BREACH":    return "metric-badge metric-badge-red";
      case "AT_RISK":   return "metric-badge metric-badge-orange";
      case "LONG_JOB":  return "metric-badge metric-badge-amber";
      case "OK":        return "metric-badge metric-badge-green";
      case "CRITICAL":  return "metric-badge metric-badge-red";
      case "CAUTION":   return "metric-badge metric-badge-amber";
      case "HEALTHY":   return "metric-badge metric-badge-green";
      case "EXCELLENT": return "metric-badge metric-badge-green";
      default:          return "metric-badge metric-badge-blue";
    }
  };

  const renderRow = (row, isExcluded) => {
    const tr = document.createElement("tr");
    tr.className = isExcluded
      ? "border-t border-Cborder/20 transition-colors"
      : "hover:bg-Cblue/5 transition-colors";
    if (isExcluded) {
      tr.style.cssText = `opacity:0.45;background:${hexA(THEME.amber,0.04)}`;
    }

    const bufPct  = typeof row.buffer_pct   === "number" ? row.buffer_pct   : null;
    const slaUsed = typeof row.sla_used_pct === "number" ? row.sla_used_pct : null;
    const peak    = typeof row.peak_hrs     === "number" ? row.peak_hrs     : 0;
    const avg     = typeof row.avg_hrs      === "number" ? row.avg_hrs      : 0;
    const status  = row.buffer_status || (bufPct < 0 ? "BREACH" : "HEALTHY");
    const jobName = row.Job_Name || row.job_name || "—";
    const reason  = row.utility_reason || "";

    const bufferClass =
      isExcluded    ? "text-Cmuted"  :
      bufPct === null   ? "text-Cmuted"  :
      bufPct < 0        ? "text-Cred font-bold"   :
      bufPct < 10       ? "text-Camber font-bold"  :
      bufPct < 30       ? "text-Camber" : "text-Cgreen";

    const slaBarPct   = Math.min(100, slaUsed ?? (peak / (row.sla_hrs ?? defaultSla) * 100));
    const slaBarColor = isExcluded ? "#6b7280" : slaBarPct >= 100 ? "#f43f5e" : slaBarPct >= 80 ? "#f59e0b" : "#3b82f6";
    const jobSla = row.sla_hrs ?? defaultSla;

    const nameCell = isExcluded
      ? `<div class="truncate max-w-[150px]" title="${escapeHtml(jobName)}" style="text-decoration:line-through">
           ${escapeHtml(jobName)}
         </div>
         <div class="text-[8px] font-mono mt-0.5" style="color:${THEME.amber}">
           ⊘ excluded${reason ? ` · ${escapeHtml(reason)}` : ""}
         </div>`
      : `<div class="truncate max-w-[150px]" title="${escapeHtml(jobName)}">${escapeHtml(jobName)}</div>`;

    const actionBtn = isExcluded
      ? `<button class="batch-include-btn text-[10px] px-1.5 py-0.5 rounded hover:opacity-80 transition"
                 style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.1)};border:1px solid ${hexA(THEME.cyan,0.2)}"
                 data-include-job="${escapeHtml(jobName)}"
                 title="Include '${escapeHtml(jobName)}' back in analysis">↩ Include</button>`
      : `<button class="batch-exclude-btn text-[11px] px-1 py-0.5 rounded opacity-40 hover:opacity-100 transition cursor-pointer"
                 style="color:${THEME.amber};background:${hexA(THEME.amber,0.08)};border:1px solid transparent"
                 data-exclude-job="${escapeHtml(jobName)}"
                 title="Exclude '${escapeHtml(jobName)}' from analysis">⊘</button>`;

    tr.innerHTML = `
      <td class="px-3 py-2 font-mono text-[11px]" style="color:${isExcluded ? '#9ca3af' : ''}">
        ${nameCell}
      </td>
      <td class="px-3 py-2 text-right font-mono font-bold text-[11px]" style="color:${isExcluded ? '#6b7280' : ''}">
        ${peak.toFixed(2)}h
        ${!isExcluded && peak > jobSla ? '<span class="ml-1 text-[9px] text-Cred font-bold">▲SLA</span>' : ""}
      </td>
      <td class="px-3 py-2 text-right font-mono text-Cmuted text-[11px]">${avg.toFixed(2)}h</td>
      <td class="px-3 py-2 text-right font-mono text-[11px] ${bufferClass}">
        ${bufPct !== null ? (bufPct >= 0 ? "+" : "") + bufPct.toFixed(1) + "%" : "—"}
      </td>
      <td class="px-3 py-2 text-right text-[11px]">
        <div class="flex items-center justify-end gap-1.5">
          <div class="pe-progress-track w-14">
            <div class="pe-progress-fill" style="width:${slaBarPct.toFixed(0)}%;background:${slaBarColor}"></div>
          </div>
          <span class="font-mono text-[10px] text-Cmuted">${slaBarPct.toFixed(0)}%</span>
        </div>
      </td>
      <td class="px-3 py-2">
        ${isExcluded ? `<span class="metric-badge" style="color:${THEME.amber};background:${hexA(THEME.amber,0.12)}">excluded</span>` : `<span class="${statusClass(status)}">${escapeHtml(status)}</span>`}
      </td>
      <td class="px-1 py-2 text-center">${actionBtn}</td>
    `;

    tr.querySelector(".batch-exclude-btn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      _batchManualExclude.add(jobName);
      _batchManualInclude.delete(jobName);
      _reRenderBatch();
    });
    tr.querySelector(".batch-include-btn")?.addEventListener("click", (e) => {
      e.stopPropagation();
      _batchManualExclude.delete(jobName);
      if ((allJobs || []).find(j => j.Job_Name === jobName)?.is_utility) {
        _batchManualInclude.add(jobName);
      }
      _reRenderBatch();
    });

    tbody.appendChild(tr);
  };

  for (const row of displayRows) renderRow(row, false);

  // Append excluded rows when toggle is on
  if (showExcluded && excludedInAll.length > 0) {
    const sepRow = document.createElement("tr");
    sepRow.innerHTML = `<td colspan="7" class="px-3 py-1 text-[9px] font-bold uppercase tracking-wider"
                                         style="color:${THEME.amber};background:${hexA(THEME.amber,0.06)}">
                         ⊘ Excluded from SLA analysis — detected as utility / infrastructure jobs
                        </td>`;
    tbody.appendChild(sepRow);
    for (const row of excludedInAll) renderRow(row, true);
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
};

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

  const search = document.getElementById("resource-table-search");
  search?.addEventListener("input", (e) => {
    resourceTableState.filter = (e.target.value || "").trim().toLowerCase();
    _updateClearButton();
    if (window.appData.resource) renderResourceTable(window.appData.resource.servers);
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
    renderResourceReview(payload);
    triggerPeConsultant().catch(() => {});  // re-run with resource data now available

    // PE Narrative: fire directly — do NOT rely solely on the findings cascade
    // because findings may skip early (no batch) or fail, leaving narrative stale.
    triggerPeNarrative().catch(() => {});

    // Pre-AI: auto-generate findings immediately on resource processing
    triggerGenerateFindings().catch(() => {});
    refreshAuditContext().catch(() => {});  // update health bar

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

  renderResourceKpis(data.kpis || {});
  _renderPriorityAction(data);
  renderResourceExecutiveSummary(data.executive_summary || null);
  renderResourceAnomalies(data.anomalies || []);
  renderResourceBarChart(data.servers || []);
  renderResourceHeatmap(data.servers || []);
  renderResourceTable(data.servers || []);

  // Show Metrics Deep Dive card when Azure data is present
  const ddCard = document.getElementById("resource-deepdive-card");
  if (ddCard) {
    const isAzure = (data.servers || []).some(s => s.source === "azure_monitor");
    if (isAzure) ddCard.classList.remove("hidden");
    else ddCard.classList.add("hidden");
  }
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
  let worst = null, worstMetric = "", worstVal = 0, worstIssue = "";
  for (const s of servers) {
    const memUsed = s.mem_pct || 0;
    const cpuUsed = s.cpu_pct || 0;
    const diskUsed = s.disk_pct || 0;
    const candidates = [
      { metric: "MEM", val: memUsed, threshold: 80, action: "investigate SGA/PGA allocation or VM sizing" },
      { metric: "CPU", val: cpuUsed, threshold: 80, action: "check runaway SQL, parallel degree, or vCPU sizing" },
      { metric: "DISK", val: diskUsed, threshold: 85, action: "review I/O-bound queries or storage throughput limits" },
    ];
    for (const c of candidates) {
      if (c.val > worstVal) {
        worst = s; worstMetric = c.metric; worstVal = c.val; worstIssue = c.action;
      }
    }
  }

  if (!worst || worstVal < 70) { banner.classList.add("hidden"); return; }

  const sev = worstVal >= 90 ? THEME.red : worstVal >= 80 ? THEME.amber : THEME.blue;
  const name = (worst.server || "").split(".")[0];
  const status = worst.status || "unknown";
  const wEnv = worst.environment || "";
  const wType = worst.type || "";
  const envTag = wEnv ? ` [${wEnv}]` : "";

  // DB servers with high memory but low CPU → softer action text
  const isDbExpected = wType === "DB" && worstMetric === "MEM" && worstVal <= 92 && (worst.cpu_pct || 0) < 20;
  const actionText = isDbExpected
    ? `DB memory ${worstVal.toFixed(0)}% is within expected SGA/PGA range. Monitor for growth trend above 93%.`
    : `Highest priority: ${worstIssue} before next batch window. This server is the fleet's single biggest risk.`;

  banner.classList.remove("hidden");
  banner.style.borderColor = hexA(sev, 0.5);
  banner.style.background = hexA(sev, 0.06);
  titleEl.style.color = sev;
  titleEl.textContent = `${name} (${wType}${envTag}) — ${worstMetric} ${worstVal.toFixed(0)}% (${status.toUpperCase()})`;
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
    const memUsed = st["Available Memory Percentage"]?.min != null ? 100 - st["Available Memory Percentage"].min : 0;
    const cpuMax = st["Percentage CPU"]?.max ?? 0;
    const score = Math.max(memUsed, cpuMax) + spikeCount * 5;
    if (score > worstScore) {
      worstScore = score;
      const domMetric = memUsed >= cpuMax ? "MEM" : "CPU";
      const domVal = Math.max(memUsed, cpuMax);
      worst = { vmName, domMetric, domVal, spikeCount, memUsed, cpuMax };
    }
  }

  if (!worst) return;

  const sev = worst.domVal >= 90 ? THEME.red : worst.domVal >= 80 ? THEME.amber : THEME.blue;
  const name = worst.vmName.split(".")[0];
  const role = _inferRole(worst.vmName);
  const env = _inferEnv(worst.vmName);
  const envSuffix = env ? ` [${env}]` : "";
  const action = worst.domMetric === "MEM"
    ? "investigate SGA/PGA allocation or VM sizing"
    : "check runaway SQL, parallel degree, or vCPU sizing";

  banner.classList.remove("hidden");
  banner.style.borderColor = hexA(sev, 0.5);
  banner.style.background = hexA(sev, 0.06);
  titleEl.style.color = sev;
  titleEl.textContent = `${name} (${role}${envSuffix}) — ${worst.domMetric} ${worst.domVal.toFixed(0)}%, ${worst.spikeCount} spike${worst.spikeCount > 1 ? "s" : ""} in last ${_deepDiveHoursBack}h`;
  detailEl.textContent = `Highest priority: ${action} before next batch window. This is the fleet's single biggest risk right now.`;
}

// ── KPI cards ─────────────────────────────────────────────────
let _scoreDecomp = null;  // stored for grade drill-down

function renderResourceKpis(k) {
  setText("rk-servers", String(k.total_servers ?? 0));

  // Build server type/environment subtitle
  const typeParts = [`${k.n_app ?? 0} APP`, `${k.n_db ?? 0} DB`];
  if (k.n_sre) typeParts.push(`${k.n_sre} SRE`);
  const envParts = [];
  if (k.n_prod) envParts.push(`${k.n_prod} PROD`);
  if (k.n_test) envParts.push(`${k.n_test} TEST`);
  if (k.n_dev)  envParts.push(`${k.n_dev} DEV`);
  const subtitle = typeParts.join(" · ") + (envParts.length ? ` │ ${envParts.join(" · ")}` : "");
  setText("rk-servers-sub", subtitle);

  const grade = k.fleet_grade || "?";
  const gradeEl = document.getElementById("rk-grade");
  if (gradeEl) {
    if (grade === "N/A") {
      gradeEl.textContent = "N/A";
      gradeEl.style.color = THEME.amber;
    } else {
      gradeEl.textContent = grade;
      gradeEl.style.color = GRADE_COLORS[grade] || THEME.muted;
    }
  }
  if (grade === "N/A") {
    setText("rk-grade-sub", "Resource data required");
  } else {
    setText("rk-grade-sub", `Score ${(k.fleet_score ?? 0).toFixed(1)}/100`);
  }

  // Store decomposition for drill-down
  _scoreDecomp = k.score_decomposition || null;

  const t = k.thresholds || RESOURCE_THRESHOLDS;
  const setMetric = (id, val, ok, warn) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = `${(val ?? 0).toFixed(1)}%`;
    el.style.color = metricColor(val ?? 0, ok, warn);
  };
  setMetric("rk-cpu",  k.avg_cpu,  t.cpu_ok,  t.cpu_warn);
  setMetric("rk-mem",  k.avg_mem,  t.mem_ok,  t.mem_warn);
  setMetric("rk-disk", k.avg_disk, t.disk_ok, t.disk_warn);

  drawDonutRing("rk-cpu-ring",  k.avg_cpu  ?? 0, RESOURCE_THRESHOLDS.cpu_warn);
  drawDonutRing("rk-mem-ring",  k.avg_mem  ?? 0, RESOURCE_THRESHOLDS.mem_warn);
  drawDonutRing("rk-disk-ring", k.avg_disk ?? 0, RESOURCE_THRESHOLDS.disk_warn);

  const healthEl = document.getElementById("rk-health");
  if (healthEl) {
    const c = k.n_critical ?? 0;
    const w = k.n_warning  ?? 0;
    const o = k.n_healthy  ?? 0;
    const a = k.n_agg_trap ?? 0;
    const d = k.n_dual_pressure ?? 0;
    let html =
      `<span style="color:${THEME.red}">${c}C</span> ` +
      `<span style="color:${THEME.amber}">${w}W</span> ` +
      `<span style="color:${THEME.green}">${o}✓</span>`;
    if (a > 0) {
      html += ` <span style="color:${THEME.cyan}" title="${a} server(s) with aggregation trap (false alarm)">${a}🔬</span>`;
    }
    if (d > 0) {
      html += ` <span style="color:${THEME.red}" title="${d} server(s) under dual CPU+Memory pressure">${d}⚡</span>`;
    }
    healthEl.innerHTML = html;
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
        <span class="text-[9px] font-bold uppercase tracking-wider text-Ccyan">🔬 FALSE ALARMS (${exec.false_alarms.length})</span>
        ${faTags}
      </div>`;
  }

  // Bottlenecks — compact 2-line cards (max 48px collapsed)
  let bottlenecksHtml = "";
  if (exec.bottlenecks && exec.bottlenecks.length) {
    const bnRows = exec.bottlenecks.map(bn => {
      const primaryIssue = (bn.issues || []).join(' · ');
      const words = primaryIssue.split(/\s+/);
      const truncated = words.length > 12 ? words.slice(0, 12).join(' ') + '…' : primaryIssue;
      // DB servers with expected memory range → amber (informational), not red (alarm)
      const isExpected = primaryIssue.includes("expected range for DB");
      const cardColor = isExpected ? THEME.amber : THEME.red;
      const statusLabel = isExpected ? "EXPECTED" : bn.status;
      const statusColor = isExpected ? THEME.green : (STATUS_COLORS[bn.status] || THEME.muted);
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
    bottlenecksHtml = `
      <div class="mt-3">
        <div class="text-[9px] font-bold uppercase tracking-wider text-Cred mb-2" style="letter-spacing:0.12em">🔥 Root Cause Candidates (${exec.bottlenecks.length})</div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-2">${bnRows}</div>
      </div>`;
  }

  card.innerHTML = `
    <div class="rounded-xl border p-4" style="border-color:${hexA(vc, 0.4)};background:${hexA(vc, 0.05)}">
      <div class="flex items-center gap-3 mb-2">
        <span class="text-[10px] font-bold uppercase tracking-wider text-Cmuted" style="letter-spacing:0.12em">Fleet Diagnosis</span>
        <span class="text-[10px] font-extrabold uppercase tracking-wider px-2 py-0.5 rounded-md border" style="color:${vc};border-color:${hexA(vc, 0.5)};background:${hexA(vc, 0.12)}">${exec.verdict}</span>
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

  // Group anomalies by host for server-centric cards
  const byHost = {};
  for (const a of anomalies) {
    const host = (a.host || "?").split(".")[0];
    if (!byHost[host]) byHost[host] = { host, items: [], maxZ: 0 };
    byHost[host].items.push(a);
    byHost[host].maxZ = Math.max(byHost[host].maxZ, Math.abs(a.z || 0));
  }
  const hostArr = Object.values(byHost).sort((a, b) => b.maxZ - a.maxZ);

  for (const hg of hostArr) {
    const sev = hg.maxZ >= 3 ? THEME.red : THEME.amber;
    const sevLabel = hg.maxZ >= 3 ? "CRITICAL" : "ELEVATED";

    const metricChips = hg.items.map(a => {
      const mc = a.metric === "cpu" ? THEME.blue : a.metric === "memory" ? THEME.cyan : THEME.purple;
      return `<span class="inline-flex items-center gap-1 text-[10px]"><span class="w-1.5 h-1.5 rounded-full inline-block" style="background:${mc}"></span><span class="font-semibold" style="color:${mc}">${(a.metric || "").toUpperCase()}</span> <span class="font-mono" style="color:${sev}">${_n(a.value).toFixed(0)}%</span> <span class="text-Cmuted font-mono">z${_n(a.z) >= 0 ? "+" : ""}${_n(a.z).toFixed(1)}</span></span>`;
    }).join(" ");

    const item = document.createElement("div");
    item.className = "rounded-lg border p-2 transition";
    item.style.borderColor = hexA(sev, 0.3);
    item.style.background = hexA(sev, 0.05);
    item.style.maxHeight = "48px";
    item.style.overflow = "hidden";
    item.innerHTML = `
      <div class="flex items-center justify-between">
        <span class="text-[13px] font-mono font-semibold" style="color:${sev}">${escapeHtml(hg.host)}</span>
        <span class="text-[8px] font-extrabold uppercase tracking-wider px-1.5 py-0.5 rounded-md" style="color:${sev};background:${hexA(sev,0.15)}">${sevLabel}</span>
      </div>
      <div class="flex flex-wrap gap-2 mt-0.5">${metricChips}</div>
    `;
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
        { label: "CPU %",  data: top.map((s) => s.cpu_pct  || 0), backgroundColor: hexA(THEME.blue,   0.85), borderColor: THEME.blue,   borderWidth: 1, borderRadius: 3 },
        { label: "Mem %",  data: top.map((s) => s.mem_pct  || 0), backgroundColor: hexA(THEME.cyan,   0.85), borderColor: THEME.cyan,   borderWidth: 1, borderRadius: 3 },
        { label: "Disk %", data: top.map((s) => s.disk_pct || 0), backgroundColor: hexA(THEME.purple, 0.85), borderColor: THEME.purple, borderWidth: 1, borderRadius: 3 },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 380 },
      layout: { padding: { right: 12 } },
      plugins: {
        legend: {
          position: "bottom",
          labels: { color: THEME.muted, font: { size: 10 }, boxWidth: 10 },
        },
        tooltip: {
          backgroundColor: THEME.card2,
          borderColor: THEME.border,
          borderWidth: 1,
          titleColor: THEME.white,
          bodyColor: THEME.white,
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.x.toFixed(1)}%`,
          },
        },
        zoom: _zoomConfig({ mode: "y" }),
      },
      scales: {
        x: {
          min: 0, max: 105,
          grid:   { color: hexA(THEME.border, 0.5), drawBorder: false },
          ticks:  { color: THEME.muted, font: { size: 10 }, callback: (v) => `${v}%` },
          title:  { display: true, text: "% Used", color: THEME.muted, font: { size: 10 } },
        },
        y: {
          grid:  { color: hexA(THEME.border, 0.25), drawBorder: false },
          ticks: { color: THEME.muted, font: { size: 10 } },
        },
      },
    },
    plugins: [resourceThresholdLinesPlugin(75, 90), crosshairPlugin],
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
      return `<div class="metric-bar-track" title="No data"><div class="metric-bar-fill" style="width:0%;background:#475569"></div></div>
              <div class="text-[9px] text-Cmuted text-right mt-0.5">N/A</div>`;
    }
    const v = Math.max(0, Math.min(100, Number(val)));
    let color;
    if (v >= warn)      color = "#ef4444";  // red
    else if (v >= ok)   color = "#f59e0b";  // amber
    else                color = "#10b981";  // green
    const threshold = opts.threshold ?? warn;
    const pulse = (opts.pulseAt != null && v >= opts.pulseAt)
      ? `<span class="pulse-red" style="position:absolute;right:-3px;top:-3px;width:8px;height:8px;border-radius:50%;background:#ef4444"></span>`
      : "";
    let bar = `<div class="metric-bar-fill" style="width:${v}%;background:${color}"></div>`;
    if (opts.segmented) {
      // Used-vs-available pattern: solid + striped track
      bar = `<div class="metric-bar-fill" style="width:${v}%;background:repeating-linear-gradient(45deg,${color} 0 6px,${color}cc 6px 12px)"></div>`;
    }
    return `<div class="metric-bar-track" title="${v.toFixed(1)}% (threshold ${threshold}%)">
              ${bar}
              <div class="metric-bar-threshold" style="left:${threshold}%"></div>
              ${pulse}
            </div>
            <div class="text-[9px] font-mono text-right mt-0.5" style="color:${color}">${v.toFixed(0)}%${opts.trendArrow ? ' ' + opts.trendArrow : ''}</div>`;
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

    return `<div class="grid items-center gap-3 py-1.5 border-b border-Cborder/30 cursor-pointer hover:bg-Ccard/60 transition rounded"
                 style="grid-template-columns:minmax(120px,1fr) 1.6fr 1.6fr 1.6fr"
                 onclick="filterServerTable('${serverName.replace(/'/g, "\\'")}')" title="Click to filter table to ${serverName}">
      <div class="min-w-0 flex items-center gap-1.5">
        <span class="text-[8px] font-bold uppercase tracking-wider px-1 py-0.5 rounded shrink-0"
              style="color:${typeColor};background:${hexA(typeColor, 0.12)};border:1px solid ${hexA(typeColor, 0.4)}">${escapeHtml(type)}</span>
        ${sEnvTag}
        <span class="text-[11px] font-mono text-Cwhite truncate" title="${escapeHtml(s.host || s.server || '')}">${escapeHtml(host)}</span>
      </div>
      <div>${barCell(s.cpu_pct,  RESOURCE_THRESHOLDS.cpu_ok, RESOURCE_THRESHOLDS.cpu_warn, { threshold: RESOURCE_THRESHOLDS.cpu_warn, trendArrow: cpuTrend })}</div>
      <div>${barCell(s.mem_pct,  RESOURCE_THRESHOLDS.mem_ok, RESOURCE_THRESHOLDS.mem_warn, { threshold: RESOURCE_THRESHOLDS.mem_warn, pulseAt: 95, trendArrow: memTrend })}</div>
      <div>${barCell(s.disk_pct, RESOURCE_THRESHOLDS.disk_ok, RESOURCE_THRESHOLDS.disk_warn, { threshold: RESOURCE_THRESHOLDS.disk_warn, segmented: true, trendArrow: diskTrend })}</div>
    </div>`;
  }).join("");

  wrap.innerHTML = `
    <div class="grid items-center gap-3 pb-1.5 border-b border-Cborder/60 text-[9px] uppercase tracking-wider text-Cmuted font-bold"
         style="grid-template-columns:minmax(120px,1fr) 1.6fr 1.6fr 1.6fr">
      <div>Server</div>
      <div>CPU (threshold ${RESOURCE_THRESHOLDS.cpu_warn}%)</div>
      <div>Memory (threshold ${RESOURCE_THRESHOLDS.mem_warn}%)</div>
      <div>Disk (threshold ${RESOURCE_THRESHOLDS.disk_warn}%)</div>
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
    tr.style.background = statusRowTint(r.status);

    // Role-specific CPU thresholds (from backend), fallback to global
    const cpuOk   = r.role_cpu_ok   ?? RESOURCE_THRESHOLDS.cpu_ok;
    const cpuWarn = r.role_cpu_warn ?? RESOURCE_THRESHOLDS.cpu_warn;

    // Build CPU cell with aggregation trap badge
    const cpuAvail = r.cpu_available !== false && r.cpu_pct != null;
    const cpuVal = cpuAvail ? r.cpu_pct.toFixed(1) : null;
    const cpuColor = cpuAvail ? metricColor(r.effective_cpu ?? r.cpu_pct ?? 0, cpuOk, cpuWarn) : '';
    let cpuExtra = "";
    if (r.agg_trap) {
      cpuExtra = ` <span class="text-[8px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.15)};border:1px solid ${hexA(THEME.cyan,0.4)}" title="Aggregation Trap: Max CPU ${cpuVal}% but Avg only ${(r.cpu_avg_pct||0).toFixed(1)}% — false alarm, server is healthy">FALSE ALARM</span>`;
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

    // Environment badge
    const env = r.environment || "";
    const envColor = env === "PROD" ? THEME.red : env === "TEST" ? THEME.amber : env === "DEV" ? THEME.cyan : THEME.muted;
    const envBadge = env ? `<span class="text-[8px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded" style="color:${envColor};background:${hexA(envColor,0.12)};border:1px solid ${hexA(envColor,0.3)}">${env}</span>` : '<span class="text-Cmuted">—</span>';

    tr.innerHTML = `
      <td class="py-2 pr-3 font-semibold text-Cwhite truncate max-w-[220px]" title="${escapeHtml(r.host || r.server)}">${escapeHtml(r.server)}${dualBadge}</td>
      <td class="py-2 pr-3 text-Cmuted">${escapeHtml(r.type || "")}</td>
      <td class="py-2 pr-3">${envBadge}</td>
      <td class="py-2 pr-3 text-right font-mono ${!cpuAvail ? 'text-Cmuted' : ''}" style="color:${cpuColor}">${cpuAvail ? cpuVal + '%' + cpuExtra : '<span title="Data unavailable">N/A</span>'}</td>
      <td class="py-2 pr-3 text-right font-mono text-Cmuted">${cpuAvgAvail ? r.cpu_avg_pct.toFixed(1) + '%' : 'N/A'}</td>
      <td class="py-2 pr-3 text-right font-mono ${!memAvail ? 'text-Cmuted' : ''}" style="color:${memAvail ? metricColor(r.mem_pct, RESOURCE_THRESHOLDS.mem_ok, RESOURCE_THRESHOLDS.mem_warn) : ''}">${memAvail ? r.mem_pct.toFixed(1) + '%' : '<span title="Data unavailable">N/A</span>'}</td>
      <td class="py-2 pr-3 text-right font-mono text-Cmuted">${memGbAvail ? r.mem_gb.toFixed(1) : 'N/A'}</td>
      <td class="py-2 pr-3 text-right font-mono ${r.disk_pct == null ? 'text-Cmuted' : ''}" style="color:${r.disk_pct != null ? metricColor(r.disk_pct, RESOURCE_THRESHOLDS.disk_ok, RESOURCE_THRESHOLDS.disk_warn) : ''}">${r.disk_pct != null ? (r.disk_pct).toFixed(1) + '%' : 'N/A'}</td>
      <td class="py-2 pr-3"><span class="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-md border" style="${statusPillStyle(r.status)}">${escapeHtml(r.status || "Unknown")}</span></td>
      <td class="py-2 pr-3 text-Cmuted truncate max-w-[180px]" title="${escapeHtml(r.source_env || "")}">${escapeHtml(truncate(r.source_env || "", 28))}</td>
    `;
    tbody.appendChild(tr);
  }
}

function statusRowTint(status) {
  switch (status) {
    case "Critical": return hexA(THEME.red,    0.10);
    case "Warning":  return hexA(THEME.amber,  0.08);
    case "Healthy":  return hexA(THEME.green,  0.06);
    default:         return "transparent";
  }
}

function statusPillStyle(status) {
  const c = STATUS_COLORS[status] || THEME.muted;
  return `color:${c};border-color:${hexA(c, 0.5)};background:${hexA(c, 0.12)}`;
}

// ════════════════════════════════════════════════════════════════
//  METRICS DEEP DIVE — Critical-only + pattern detection
//  Filters out normal/moderate — shows only actionable PE findings
// ════════════════════════════════════════════════════════════════

let _deepDiveCharts = [];   // track Chart.js instances for cleanup
let _deepDiveData = null;   // last fetched timeseries payload

// ── Deep Dive time range picker ──
let _deepDiveHoursBack = 24;

function setDeepDiveHours(el) {
  const hours = parseInt(el.dataset.ddHours) || 24;
  _deepDiveHoursBack = hours;
  // Update active pill styling
  document.querySelectorAll(".dd-time-pill").forEach(p => p.classList.remove("dd-time-active"));
  el.classList.add("dd-time-active");
  // Auto-reload if data was already fetched
  if (_deepDiveData) loadMetricsDeepDive();
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
  loadingText.textContent = `Fetching time-series for ${_lastFetchedVmIds.length} VM(s)${baselineNote}…`;
  chartsDiv.innerHTML = "";
  heatmapWrap?.classList.add("hidden");
  banner?.classList.add("hidden");

  // Cleanup old Chart.js instances
  _deepDiveCharts.forEach(c => { try { c.destroy(); } catch(e){} });
  _deepDiveCharts = [];

  const t0 = performance.now();

  fetch("/api/azure/timeseries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vm_ids: _lastFetchedVmIds, hours_back: hoursBack }),
  })
  .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
  .then(data => {
    _deepDiveData = data;
    const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
    loading.classList.add("hidden");
    btn.disabled = false;
    const blLabel = data.baseline?.days_observed >= 15 ? ` · ${data.baseline.days_observed.toFixed(0)}d baseline ✓` : data.baseline?.days_observed >= 2 ? ` · ${data.baseline.days_observed.toFixed(0)}d` : "";
    btn.textContent = `Refresh (${elapsed}s${blLabel})`;

    _renderDeepDiveBanner(data.summary);
    _renderDeepDivePatterns(data.patterns || []);
    _renderDeepDiveHeatmap(data.heatmap);
    _renderDeepDiveMemoryHeatmap(data.vms);
    _renderDeepDiveCharts(data.vms, data.summary);
    _updatePriorityFromDeepDive(data.vms);
    // Persist deep dive data in appData so findings/narrative/exec can use it
    window.appData.deepDive = _buildDeepDiveSummary();
    // Re-generate findings with deep dive evidence now available
    triggerGenerateFindings().catch(() => {});
  })
  .catch(err => {
    loading.classList.add("hidden");
    btn.disabled = false;
    btn.textContent = "Load Time-Series";
    toast("error", "Deep Dive Error", err.message);
  });
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
    detail.textContent = `${summary.vm_count} VM(s) analyzed over ${summary.hours_back}h — only critical spikes shown (z-score ≥ 3σ)${blNote}.`;
  } else {
    banner.style.background = hexA(THEME.green, 0.08);
    banner.style.border = `1px solid ${hexA(THEME.green, 0.3)}`;
    icon.textContent = "✅";
    title.textContent = "Fleet Healthy — No Critical Anomalies";
    title.style.color = THEME.green;
    const blDays = _deepDiveData?.baseline?.days_observed || 0;
    const blNote = blDays >= 15 ? ` ${blDays.toFixed(0)}-day baseline confirms stability.` : blDays >= 2 ? ` ${blDays.toFixed(0)}-day observation — extend to 15d for full PE confidence.` : "";
    detail.textContent = `${summary.vm_count} VM(s) analyzed over ${summary.hours_back}h — all metrics within normal operating range.${blNote}`;
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

// ── Fleet CPU Heatmap (Plotly) ────────────────────────────────
function _renderDeepDiveHeatmap(heatmap) {
  const wrap = document.getElementById("deepdive-heatmap-wrap");
  const container = document.getElementById("deepdive-heatmap");
  if (!wrap || !container || !heatmap || !heatmap.vms.length) return;

  wrap.classList.remove("hidden");
  container.innerHTML = "";

  const vmNames = heatmap.vms.map(v => v.name);
  const z = heatmap.vms.map(v => v.values.map(x => x ?? 0));

  // Use actual Date objects for Plotly date axis — avoids duplicate tick labels
  const tDates = heatmap.timestamps.map(t => new Date(t));

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
    margin: { l: 140, r: 60, t: 10, b: 40 },
    height: Math.max(160, vmNames.length * 30 + 60),
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

  Plotly.newPlot(container, [trace], layout, _plotlyConfig());

  // Enterprise: click-to-drill — click a VM row to expand its detail card
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

  // Enterprise: export toolbar + time sync
  _addChartToolbar(wrap, container, () => {
    let csv = "VM,Timestamp,CPU_Pct\n";
    heatmap.vms.forEach(vm => {
      heatmap.timestamps.forEach((t, ti) => { csv += `${vm.name},${t},${vm.values[ti] ?? ""}\n`; });
    });
    return csv;
  });
  _registerPlotlySync(container);
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
    vmNames.push(vmName);
    // Invert: available → used = 100 - available
    allSeries.push(series.map(p => ({ t: p.t, v: 100 - (p.v || 0) })));
  }

  if (!vmNames.length) return;

  wrap.classList.remove("hidden");
  container.innerHTML = "";

  // Build time buckets (use first VM's timestamps as reference)
  const refTimes = allSeries[0].map(p => p.t);
  // Use actual Date objects for Plotly date axis — avoids duplicate tick labels
  const tDates = refTimes.map(t => new Date(t));

  // Build z-matrix (VMs × time)
  const z = allSeries.map(series => {
    // Align to ref timestamps (closest match)
    return refTimes.map(rt => {
      const refT = new Date(rt).getTime();
      let best = null, bestDist = Infinity;
      for (const p of series) {
        const d = Math.abs(new Date(p.t).getTime() - refT);
        if (d < bestDist) { bestDist = d; best = p.v; }
      }
      return best ?? 0;
    });
  });

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
    z: z,
    x: tDates,
    y: vmNames,
    type: "heatmap",
    zmin: 0,
    zmax: 100,
    colorscale: [
      [0,    "#0d1526"],
      [0.3,  "#065f46"],
      [0.5,  "#10b981"],
      [0.7,  "#facc15"],
      [0.8,  "#f59e0b"],
      [0.9,  "#ef4444"],
      [1.0,  "#dc2626"],
    ],
    colorbar: {
      title: { text: "Mem Used %", font: { color: THEME.muted, size: 10 } },
      tickfont: { color: THEME.muted, size: 9 },
      thickness: 12,
      tickvals: [0, 25, 50, 75, 85, 100],
    },
    hoverongaps: false,
    hovertemplate: "<b>%{y}</b><br>%{x|%b %d %I:%M %p}<br>Mem Used: %{z:.1f}%<extra></extra>",
  };

  const layout = _plotlyBaseLayout({
    margin: { l: 140, r: 60, t: 10, b: 40 },
    height: Math.max(160, vmNames.length * 30 + 60),
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

  Plotly.newPlot(container, [trace], layout, _plotlyConfig());

  // V2: Single merged batch pattern banner — distinguish chronic from periodic
  if (batchFindings.length) {
    const worst = batchFindings[0];
    const totalSlots = refTimes.length;
    const indicator = document.createElement("div");
    indicator.className = "text-[10px] mt-2 px-3 py-1.5 rounded-lg border";

    // When ≥95% of time slots breach the highest threshold → chronic condition, not batch
    const isChronic = worst.count >= totalSlots * 0.95;

    if (isChronic) {
      indicator.style.cssText = `color:${THEME.red};border-color:${hexA(THEME.red,0.4)};background:${hexA(THEME.red,0.08)}`;
      indicator.textContent = `🔴 Persistent fleet-wide memory saturation — not batch-driven, chronic condition. All ${worst.count} observed time slots exceed ${worst.label} threshold on ≥50% of servers. This requires capacity expansion, not schedule adjustment.`;
    } else {
      indicator.style.cssText = `color:${THEME.amber};border-color:${hexA(THEME.amber,0.3)};background:${hexA(THEME.amber,0.06)}`;
      const subtext = batchFindings.map(f => `${f.count} slots at ${f.label}`).join(" · ");
      indicator.textContent = `⚡ Shared batch pattern detected: ${worst.count} time slots where ≥50% of servers show memory ${worst.label} — persistent concurrent batch overlap across observation window. (${subtext})`;
    }
    wrap.appendChild(indicator);
  }

  // Enterprise: click-to-drill — click a VM row to expand its detail card
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

  // Enterprise: export toolbar + time sync
  _addChartToolbar(wrap, container, () => {
    let csv = "VM,Timestamp,Mem_Used_Pct\n";
    vmNames.forEach((vm, vi) => {
      refTimes.forEach((t, ti) => { csv += `${vm},${t},${z[vi][ti]?.toFixed(1) ?? ""}\n`; });
    });
    return csv;
  });
  _registerPlotlySync(container);
}

// Unit-aware peak formatter — converts raw bytes to GB, others to %
function _formatPeak(metricKey, value) {
  if (metricKey === "Available Memory Bytes" || (typeof value === 'number' && value > 1e6)) {
    return (value / 1073741824).toFixed(1) + " GB";
  }
  return value + "%";
}

// ── Per-VM Time-Series Charts — grouped server cards (GAP 1) ──
function _renderDeepDiveCharts(vms, summary) {
  const chartsDiv = document.getElementById("deepdive-charts");
  if (!chartsDiv) return;

  const metricConfig = [
    { key: "Percentage CPU",                    label: "CPU %",            color: THEME.blue,   warn: 80 },
    { key: "Available Memory Percentage",       label: "Available Mem %",  color: THEME.cyan,   warn: 20, invert: true },
    { key: "Available Memory Bytes",            label: "Available Memory Bytes", color: THEME.cyan, warn: 0, unit: "bytes" },
    { key: "OS Disk Bandwidth Consumed Percentage",   label: "OS Disk BW %",   color: THEME.amber,  warn: 80 },
    { key: "Data Disk Bandwidth Consumed Percentage", label: "Data Disk BW %", color: THEME.purple, warn: 80 },
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
    critHeader.innerHTML = `<span class="text-sm">🚨</span><h4 class="text-[10px] font-bold uppercase tracking-widest text-red-400" style="letter-spacing:0.15em">Requires Investigation — ${criticalVms.length} Server${criticalVms.length > 1 ? "s" : ""}</h4><span class="text-[9px] text-Cmuted ml-auto">${_deepDiveHoursBack}h window</span>`;
    chartsDiv.appendChild(critHeader);

    // E2: Sort/filter controls
    const controls = document.createElement("div");
    controls.className = "flex items-center gap-3 flex-wrap py-2";
    controls.innerHTML = `
      <div class="flex items-center gap-1.5">
        <span class="text-[9px] text-Cmuted font-semibold">Sort</span>
        <select id="dd-sort-select" class="bg-Cbg border border-Cborder rounded-md px-2 py-0.5 text-[10px] text-Cwhite focus:outline-none focus:border-Cblue cursor-pointer">
          <option value="mem">MEM % ↓</option>
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
      const memUsed = _memSt
        ? (_memSt.min_anomalous && _memSt.p5 != null ? 100 - _memSt.p5 : 100 - _memSt.min)
        : 0;
      const role = _inferRole(vmName);
      return { vmName, vmData, memUsed, spikeCount, latestSpike, role };
    });

    function renderFilteredGrid() {
      const sortBy = document.getElementById("dd-sort-select")?.value || "mem";
      const threshold = parseInt(document.getElementById("dd-threshold-input")?.value || "0");
      const typeFilter = document.querySelector(".dd-type-pill.dd-type-active")?.dataset.ddType || "all";

      let filtered = cardDataArr.filter(d => d.memUsed >= threshold);
      if (typeFilter === "db") filtered = filtered.filter(d => d.role.includes("DB"));
      else if (typeFilter === "app") filtered = filtered.filter(d => d.role.includes("APP"));

      filtered.sort((a, b) => {
        if (sortBy === "mem") return b.memUsed - a.memUsed;
        if (sortBy === "spikes") return b.spikeCount - a.spikeCount;
        if (sortBy === "latest") return b.latestSpike - a.latestSpike;
        return a.vmName.localeCompare(b.vmName);
      });

      grid.innerHTML = "";
      for (const d of filtered) {
        _renderVmServerCard(d.vmName, d.vmData, metricConfig, grid);
      }
      if (!filtered.length) {
        grid.innerHTML = `<div class="col-span-3 text-center text-Cmuted text-xs py-4">No servers match current filters.</div>`;
      }
    }

    renderFilteredGrid();

    // Wire controls
    document.getElementById("dd-sort-select")?.addEventListener("change", renderFilteredGrid);
    const thresholdInput = document.getElementById("dd-threshold-input");
    thresholdInput?.addEventListener("input", () => {
      document.getElementById("dd-threshold-label").textContent = thresholdInput.value + "%";
      renderFilteredGrid();
    });
    controls.querySelectorAll(".dd-type-pill").forEach(btn => {
      btn.addEventListener("click", () => {
        controls.querySelectorAll(".dd-type-pill").forEach(b => b.classList.remove("dd-type-active"));
        btn.classList.add("dd-type-active");
        renderFilteredGrid();
      });
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

// ── I2: Role inference from VM naming convention ──────────────
function _inferRole(vmName) {
  const n = (vmName || "").toLowerCase();
  if (/^prbe|prod.*db|scpo.*db/.test(n)) return "PROD-DB";
  if (/^drbe|dr.*db/.test(n)) return "DR-DB";
  if (/^tabe|test.*db|uat.*db/.test(n)) return "TEST-DB";
  if (/^prb[^e]|prod.*app|batch.*app/.test(n)) return "BATCH-APP";
  if (/^dabe|^dsbe|da.*db/.test(n)) return "DA-DB";
  if (/app|web|ui/.test(n)) return "APP";
  if (/db|ora|sql/.test(n)) return "DB";
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
  const cpuMax = stats["Percentage CPU"]?.max ?? 0;
  // Use P5 (100 - P95 of available) for card display to ignore single-point dips.
  // Fall back to inverted min only when P5 is not available.
  const memAvailStats = stats["Available Memory Percentage"];
  const memMax = memAvailStats
    ? (memAvailStats.min_anomalous && memAvailStats.p5 != null
      ? 100 - memAvailStats.p5
      : 100 - memAvailStats.min)
    : 0;
  const diskMax = stats["OS Disk Bandwidth Consumed Percentage"]?.max ?? 0;
  let domLabel, domVal, domColor;
  if (memMax >= cpuMax && memMax >= diskMax) {
    domLabel = "MEM"; domVal = memMax; domColor = THEME.cyan;
  } else if (cpuMax >= diskMax) {
    domLabel = "CPU"; domVal = cpuMax; domColor = THEME.blue;
  } else {
    domLabel = "DISK"; domVal = diskMax; domColor = THEME.amber;
  }

  const card = document.createElement("div");
  card.className = "rounded-xl border p-3 cursor-pointer transition hover:scale-[1.01] group";
  card.style.borderColor = hexA(THEME.red, 0.3);
  card.style.background = hexA(THEME.red, 0.04);

  // 100% saturation → pulsing red border
  if (domVal >= 100) {
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
    if (domLabel === "MEM") vals = vals.map(v => 100 - v); // invert available → used
    const mn = Math.min(...vals), mx = Math.max(...vals);
    const rng = mx - mn || 1;
    const w = 80, h = 24;
    const step = w / (vals.length - 1);
    const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - mn) / rng) * h).toFixed(1)}`).join(" ");
    sparkSvg = `<svg width="${w}" height="${h}" class="opacity-60 group-hover:opacity-100 transition"><polyline points="${pts}" fill="none" stroke="${domColor}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
  }

  const sevLabel = hasSustained ? "CRITICAL SUSTAINED" : highestSev >= 3 ? "CRITICAL" : "WARNING";
  const sevColor = hasSustained ? THEME.purple : highestSev >= 3 ? THEME.red : THEME.amber;

  // E3: Trend direction — current vs 2h ago
  const twoHoursMs = 2 * 60 * 60 * 1000;
  let trendArrow = "", trendDelta = "";
  if (sparkSource.length > 4) {
    let tVals = sparkSource.map(p => ({ t: new Date(p.t).getTime(), v: domLabel === "MEM" ? 100 - p.v : p.v }));
    const latest = tVals[tVals.length - 1];
    const twoHAgo = tVals.filter(p => (latest.t - p.t) >= twoHoursMs);
    const ref = twoHAgo.length ? twoHAgo[twoHAgo.length - 1] : tVals[0];
    const delta = latest.v - ref.v;
    if (delta > 2) { trendArrow = "↑"; trendDelta = `+${delta.toFixed(0)}%`; }
    else if (delta < -2) { trendArrow = "↓"; trendDelta = `${delta.toFixed(0)}%`; }
    else { trendArrow = "→"; trendDelta = "flat"; }
  }
  const trendColor = trendArrow === "↑" ? THEME.red : trendArrow === "↓" ? THEME.green : THEME.muted;

  // I2: Role tag
  const role = _inferRole(vmName);
  const vmEnv = _inferEnv(vmName);
  const vmEnvColor = vmEnv === "PROD" ? THEME.red : vmEnv === "TEST" ? THEME.amber : vmEnv === "DEV" ? THEME.cyan : "";
  const vmEnvBadge = vmEnv ? `<span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${vmEnvColor};background:${hexA(vmEnvColor,0.12)}">${vmEnv}</span>` : "";

  // P1: Projected breach (for 70-79% range)
  let breachLabel = "";
  if (domVal >= 70 && domVal < 80 && trendArrow === "↑" && sparkSource.length > 4) {
    let tVals = sparkSource.map(p => ({ t: new Date(p.t).getTime(), v: domLabel === "MEM" ? 100 - p.v : p.v }));
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

  card.innerHTML = `
    <div class="flex items-start justify-between gap-2 mb-1.5">
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-1.5">
          <span class="text-xs font-bold text-Cwhite truncate">${escapeHtml(vmName)}</span>
          <span class="text-[7px] font-bold uppercase tracking-wider px-1 py-0.5 rounded" style="color:${THEME.muted};background:${hexA(THEME.border,0.4)}">${role}</span>
          ${vmEnvBadge}
        </div>
        <div class="flex items-center gap-2 mt-1">
          <span class="px-1.5 py-0.5 rounded text-[8px] font-extrabold uppercase" style="color:${sevColor};background:${hexA(sevColor,0.15)}">${sevLabel}</span>
          <span class="text-[9px] text-Cmuted">${criticalCount} spike${criticalCount > 1 ? "s" : ""}</span>
          ${breachLabel ? `<span class="text-[8px] font-bold px-1 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.12)}">⏱ ${breachLabel}</span>` : ""}
        </div>
      </div>
      <div class="text-right shrink-0">
        <div class="flex items-baseline gap-1 justify-end">
          <span class="text-lg font-extrabold leading-none" style="color:${domColor}">${domVal.toFixed(0)}%</span>
          <span class="text-xs font-bold" style="color:${trendColor}">${trendArrow}</span>
        </div>
        <div class="flex items-center gap-1 justify-end">
          <span class="text-[8px] font-bold uppercase tracking-wider" style="color:${domColor}">${domLabel}</span>
          ${trendDelta ? `<span class="text-[8px] font-mono" style="color:${trendColor}">${trendDelta}</span>` : ""}
        </div>
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
    _renderVmDeepDiveCard(vmName, vmData, metricConfig, detailArea, true);
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
  const cpuStats = stats["Percentage CPU"];
  const memStats = stats["Available Memory Percentage"];
  let headerStats = "";
  if (cpuStats) {
    const maxTag = cpuStats.max_anomalous
      ? `<span class="text-[8px] px-1 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}" title="Max ${cpuStats.max}% may be a single-point spike — P95 is ${cpuStats.p95}%">single-point</span>`
      : "";
    headerStats += `<span class="text-Cblue">CPU avg ${cpuStats.mean}% · max ${cpuStats.max}% ${maxTag}· P95 ${cpuStats.p95}%</span>`;
  }
  if (memStats) {
    const memMinTag = memStats.min_anomalous
      ? `<span class="text-[8px] px-1 py-0.5 rounded" style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}" title="Min ${memStats.min}% may be a single-point dip — P5 is ${memStats.p5 ?? 'N/A'}%">single-point</span>`
      : "";
    headerStats += `${cpuStats ? " · " : ""}<span class="text-Ccyan">Mem avail ${memStats.mean}% · min ${memStats.min}% ${memMinTag}</span>`;
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

  // Critical spike detail table — P2: group recurring events
  const allCriticalSpikes = [];
  for (const [metricName, spikeList] of Object.entries(spikes)) {
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
        const deviation = ((s.peak - s.mean) / s.std).toFixed(1);

        // Bug 3+4: severity-aware labels (sustained, absolute threshold)
        const sev = (s.severity || "critical").toUpperCase().replace("_", " ");
        const sevColor = sev.includes("SUSTAINED") ? THEME.purple : sev === "WARNING" ? THEME.amber : THEME.red;
        const detectionTag = s.detection === "absolute_threshold"
          ? `<span class="ml-1 px-1 py-0.5 rounded text-[8px] font-bold" style="color:${THEME.cyan};background:${hexA(THEME.cyan,0.15)}">ABS</span>`
          : "";

        return `<tr class="border-t border-red-500/15">
          <td class="py-1.5 pr-3 text-[10px] font-semibold" style="color:${sevColor}">${sev}${detectionTag}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cwhite">${escapeHtml(metricLabel)}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cwhite font-mono font-bold">${_formatPeak(s.metric, s.peak)}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">${start} → ${end}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">${s.duration_min}min</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">peak @ ${peakTime}</td>
          <td class="py-1.5 text-[10px] text-Cmuted">${deviation}σ above mean (μ=${s.mean}%)</td>
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

        return `<tr class="border-t border-red-500/15" style="background:${hexA(THEME.amber, 0.04)}">
          <td class="py-1.5 pr-3 text-[10px] font-semibold" style="color:${sevColor}">${worstSev} <span class="px-1 py-0.5 rounded text-[8px] font-bold" style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}">RECURRING</span></td>
          <td class="py-1.5 pr-3 text-[10px] text-Cwhite">${escapeHtml(metricLabel)} <span class="px-1 py-0.5 rounded text-[8px] font-bold" style="color:${THEME.amber};background:${hexA(THEME.amber,0.15)}">${group.length}×</span></td>
          <td class="py-1.5 pr-3 text-[10px] text-Cwhite font-mono font-bold">${_formatPeak(s0.metric, maxPeak)}</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">${dayLabel} pattern</td>
          <td class="py-1.5 pr-3 text-[10px] text-Cmuted">~${avgDur}min each</td>
          <td class="py-1.5 pr-3 text-[10px] text-amber-400 font-semibold">scheduled job pattern</td>
          <td class="py-1.5 text-[10px] text-Cmuted">peak ${_formatPeak(s0.metric, maxPeak)}, avg duration ${avgDur}min</td>
        </tr>`;
      }
    }).join("");

    spikeTable.innerHTML = `
      <div class="text-[10px] font-bold text-red-400 uppercase tracking-widest mb-1">⚡ Critical Spike Events</div>
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

  // ── Unified multi-metric chart (dual Y-axes) ──────────────
  // CPU + Disk on left axis, Memory (inverted to "used") on right axis
  const unifiedMetrics = [
    { key: "Percentage CPU",                          label: "CPU %",       color: THEME.blue,   axis: "y", dash: [] },
    { key: "Available Memory Percentage",              label: "Mem Used %",  color: THEME.red,    axis: "y1", dash: [], invert: true },
    { key: "OS Disk Bandwidth Consumed Percentage",    label: "OS Disk %",   color: THEME.amber,  axis: "y", dash: [4, 2] },
    { key: "Data Disk Bandwidth Consumed Percentage",  label: "Data Disk %", color: THEME.purple, axis: "y", dash: [2, 2] },
  ];

  const datasetsForChart = [];
  let unifiedLabels = null;
  const allAnnotations = {};
  const _pendingAnnotations = [];

  for (const um of unifiedMetrics) {
    const pts = metrics[um.key];
    if (!pts || !pts.length) continue;
    if (!unifiedLabels) unifiedLabels = pts.map(p => new Date(p.t));
    let vals = pts.map(p => p.v);
    if (um.invert) vals = vals.map(v => 100 - v); // available → used

    datasetsForChart.push({
      label: um.label,
      data: vals,
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
            title: { display: true, text: "Mem Used %", color: THEME.red, font: { size: 9 } },
            ticks: { color: hexA(THEME.red, 0.6), font: { size: 9 }, callback: v => v + "%" },
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
  // Nothing to wire at boot — buttons use inline onclick handlers.
  // Findings auto-fire when the insights tab becomes active (setActiveView).
}

// ── Trigger AI-driven findings (Gemini / NIM cross-pillar synthesis) ─────
// ── Build SLA triage digest for findings payload ──────────────────────────
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

    const memUsed = st["Available Memory Percentage"]?.min != null
      ? 100 - st["Available Memory Percentage"].min : null;
    const cpuMax = st["Percentage CPU"]?.max ?? null;

    perVm.push({
      vm: vmName,
      role: _inferRole(vmName),
      spike_count: spikeCount,
      mem_used_max: memUsed,
      cpu_max: cpuMax,
      trend,
      spikes: spikeDetails.slice(0, 5), // top 5 per VM
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
  return {
    batch_kpis:    ad.batch?.kpis        || null,
    top_jobs:      ad.batch?.top_jobs    || null,
    top_breaches:  ad.batch?.top_breaches|| null,
    window:        ad.batch?.window      || null,
    anomalies:     ad.batch?.anomalies   || null,
    sub_stats:     ad.batch?.sub_stats   || null,
    resource_kpis: ad.resource?.kpis     || null,
    servers:       ad.resource?.servers  || ad.servers || null,
    sla_matrix:    ad.slaMatrix          || null,
    sla_ceilings:  ad.slaCeilings        || null,
    benchmark:     ad.benchmark          || null,
    sow_compare:   ad.sowCompare         || null,
    issues:        ad.issues             || null,
    customer_name: ad.customerName       || null,
    sla_intel:     ad.slaIntelligence    || null,
    // SLA triage: low-buffer jobs + unexplained breaches for targeted findings
    sla_triage:    _buildSlaTriage(),
    // Deep dive time-series evidence (spikes, patterns) — when available
    deep_dive:     _buildDeepDiveSummary(),
  };
}

async function triggerGenerateFindings() {
  const loading = document.getElementById("findings-loading");
  if (loading) loading.classList.remove("hidden");

  // Call the deterministic rule engine — fast, always works, no LLM needed.
  // The LLM runs in the background via triggerSmartFindings afterwards.
  const payload = _buildFindingsPayload();

  // Skip if there's nothing to analyse
  const ad = window.appData || {};
  const hasData = !!(ad.batch || ad.resource) ||
                  !!(payload.batch_kpis || payload.resource_kpis ||
                     (payload.top_jobs && payload.top_jobs.length));
  if (!hasData) {
    if (loading) loading.classList.add("hidden");
    return;
  }

  try {
    const res = await fetch("/api/generate-findings", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });
    if (!res.ok) {
      const msg = await res.text();
      toast("error", "Findings error", msg.slice(0, 200));
      return;
    }
    const data = await res.json();

    // /api/generate-findings already returns the Finding shape directly —
    // no field remapping needed (text, recommendation, evidence_class, etc.)
    const findings = Array.isArray(data.findings) ? data.findings : [];

    window.appData.findings = { findings, summary: data.summary };
    renderFindings(findings);
    renderFindingsSummary(data.summary || {});
    renderFindingsDonut(data.summary || {});

    // Background: LLM smart analysis (non-blocking — never delays the table)
    triggerSmartFindings(payload).catch(() => {});

    // Cross-pillar cascade
    triggerPeConsultant().catch(() => {});
    triggerPeNarrative().catch(() => {});
  } catch (err) {
    _handleFetchError(err);
  } finally {
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

  // Grade — canonical boundaries matching pe_config.GRADE_TABLE:
  // ≥90=A, ≥80=B, ≥70=C, ≥60=D, <60=F
  // For finding-count-based grade: map counts to a synthetic score
  const penaltyScore = Math.max(0, 100 - hardBlockers.length * 20 - softCriticals.length * 10 - warnCount * 5);
  const grade = penaltyScore >= 90 ? "A"
    : penaltyScore >= 80 ? "B"
    : penaltyScore >= 70 ? "C"
    : penaltyScore >= 60 ? "D"
    : "F";
  const _GRADE_LABELS = { A: "APPROVED", B: "APPROVED WITH NOTES", C: "CONDITIONAL HOLD", D: "BLOCKED — MINOR", F: "BLOCKED — MAJOR" };
  const gradeColors = { F: THEME.red, D: THEME.red, C: THEME.amber, B: THEME.amber, A: THEME.green };
  const gc = gradeColors[grade] || THEME.muted;

  // Animate verdict hero
  const pill = document.getElementById("findings-decision-pill");
  if (pill) {
    pill.textContent = decision;
    pill.style.cssText = `color:${glowColor};border-color:${glowColor};background:${hexA(glowColor,.15)};text-shadow:0 0 20px ${hexA(glowColor,.4)}`;
  }
  const gradePill = document.getElementById("findings-grade-pill");
  if (gradePill) {
    gradePill.textContent = `Grade ${grade} — ${_GRADE_LABELS[grade] || ""}`;
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

  // Narrative text
  const verdictText = document.getElementById("findings-verdict-text");
  if (verdictText && narrativeFinding && narrativeFinding.sub) {
    const raw = narrativeFinding.sub;
    const lines = raw.split("\n").map(l => l.trim()).filter(Boolean);
    // Strip labels (Scope:, Impact:, etc.) and join as flowing prose
    const labelPattern = /^(Scope|Compliance|Root causes? identified|Impact|Evidence|Decision|Primary blocker):\s*/i;
    const cleanLines = lines.map(l => l.replace(labelPattern, ""));
    verdictText.textContent = cleanLines.join(" ");
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
  if (!window._findingsSort)   window._findingsSort   = { col: "severity", dir: 1 };
  if (!window._findingsCols)   window._findingsCols   = {};
  if (!window._findingsFilter) window._findingsFilter = "critical"; // default: show only critical
  _renderFindingsColPicker();
  _updateFindingsFilterCounts();
  _applyFindingsSort();
}

// ── Findings: column defs + sort / column-picker helpers ─────
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

  tbody.innerHTML = sorted.map((f, idx) => {
    const sv      = SV_ST[f.level] || SV_ST.info;
    const rc      = (f.root_cause || "").replace(/_/g, " ").trim() || "—";
    const impact  = (f.impact || "").trim() || "—";
    const action  = (f.recommendation || "").trim() || "—";
    const ecColor = EC_CLR[f.evidence_class] || THEME.muted;
    const ecLbl   = EC_LBL[f.evidence_class] || "—";
    const srcLbl  = SRC_LBL[f.source] || (f.source || "").toUpperCase() || "—";
    const srcClr  = SRC_CLR[f.source] || THEME.muted;
    const detId   = `finding-det-${idx}`;
    const bdrClr  = SEV_BDR[f.level] || THEME.muted;
    const bdrAlpha = f.level === "critical" ? 0.7 : f.level === "warning" ? 0.45 : 0.2;
    // Root cause cell color: amber for critical, amber/70 for warning, default for others
    const rcColor = f.level === "critical" ? THEME.amber
                  : f.level === "warning"  ? hexA(THEME.amber, .8)
                  : "rgba(240,244,255,.75)";
    // Only show ec badge in finding cell when Evidence column is hidden
    const showEcBadge = !visSet.has("evidence");
    const isCrit = f.level === "critical";

    return `<tr data-idx="${idx}"
  class="hover:bg-white/[0.04] transition-colors cursor-pointer border-b border-white/5 group${isCrit ? " findings-crit-row" : ""}"
  style="border-left:3px solid ${hexA(bdrClr, bdrAlpha)};${isCrit ? `background:rgba(244,63,94,.025)` : ""}"
  onclick="const d=document.getElementById('${detId}');d.classList.toggle('hidden');this.querySelector('.fchev').textContent=d.classList.contains('hidden')?'▸':'▾'">
  <td class="pl-3 pr-2 py-2.5 w-8">
    <span class="inline-block w-2.5 h-2.5 rounded-full shrink-0${isCrit ? " findings-crit-blink" : ""}"
          style="background:${sv.dot};box-shadow:0 0 ${isCrit ? "12" : "8"}px ${hexA(sv.dot, isCrit ? .75 : .45)}"></span>
  </td>
  <td class="px-2 py-2.5" style="min-width:220px;max-width:340px">
    <div class="text-[11px] font-semibold text-Cwhite leading-snug">${_esc(f.text)}</div>
    ${f.sub ? `<div class="text-[9px] text-Cmuted mt-0.5 leading-relaxed line-clamp-2" title="${_esc(f.sub)}">${_esc(f.sub)}</div>` : ""}
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
  ${visSet.has("action")     ? `<td class="px-3 py-2.5 text-[10px] text-Cwhite/70 w-44">${_col(action, 50)}</td>` : ""}
  ${visSet.has("evidence")   ? `<td class="px-3 py-2.5 w-20 text-center">
    <span class="inline-flex text-[8px] px-1.5 py-0.5 rounded"
          style="background:${hexA(ecColor,.1)};color:${ecColor};border:1px solid ${hexA(ecColor,.25)}"
          title="${_esc(f.evidence || ecLbl)}">${ecLbl}</span>
  </td>` : ""}
  <td class="px-2 py-2.5 w-8 text-center">
    <span class="fchev text-[11px] text-Cmuted/50 group-hover:text-Cmuted transition-colors">▸</span>
  </td>
</tr>
<tr id="${detId}" class="hidden border-b border-white/5"
    style="background:${hexA(bdrClr, .04)};border-left:3px solid ${hexA(bdrClr, bdrAlpha)}">
  <td colspan="${colspan}" class="pl-5 pr-4 py-3.5">
    <div class="grid grid-cols-3 gap-6 text-[10px]">
      <div>
        <div class="text-[8px] uppercase tracking-widest font-bold mb-1.5" style="color:${THEME.amber}">⚡ Root Cause</div>
        <div class="text-Cwhite/85 leading-relaxed">${_esc(rc)}</div>
      </div>
      <div>
        <div class="text-[8px] uppercase tracking-widest font-bold mb-1.5" style="color:${THEME.blue}">📋 Business Impact</div>
        <div class="text-Cwhite/85 leading-relaxed">${_esc(impact)}</div>
      </div>
      <div>
        <div class="text-[8px] uppercase tracking-widest font-bold mb-1.5" style="color:${THEME.green}">→ Recommended Action</div>
        <div class="text-Cwhite/85 leading-relaxed">${_esc(action)}</div>
        ${f.evidence ? `<div class="text-Cmuted mt-2 text-[8px] italic">${_esc(f.evidence)}</div>` : ""}
      </div>
    </div>
  </td>
</tr>`;
  }).join("");
}

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
    // This covers page-reload / tab-switch scenarios where data was uploaded
    // in a previous page load or via API.
    const slots = data.slots || {};
    const ad = window.appData;
    if (!ad.batch && slots.batch_kpis) {
      const extra = data.extra || {};
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
  } catch {
    // Non-fatal — health bar is cosmetic
  }
}

// ── PE Review Narrative (structured 4-section report) ────────
async function triggerPeNarrative() {
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
  let sow_compare = ad.sowCompare || null;
  if (!sow_compare) {
    const sc      = ad.sowContract || {};
    const dfuBase = parseFloat(document.getElementById("sow-dfu-baseline")?.value) || sc.manual_dfu_baseline || 0;
    const dfuAct  = parseFloat(document.getElementById("sow-dfu-actual")?.value)   || sc.manual_dfu_actual   || 0;
    const skuBase = parseFloat(document.getElementById("sow-sku-baseline")?.value) || sc.manual_sku_baseline || 0;
    const skuAct  = parseFloat(document.getElementById("sow-sku-actual")?.value)   || sc.manual_sku_actual   || 0;
    if (dfuBase > 0 || skuBase > 0) {
      sow_compare = {};
      if (dfuBase > 0) sow_compare["Daily DFU"] = { sow: dfuBase, actual: dfuAct > 0 ? dfuAct : null };
      if (skuBase > 0) sow_compare["Daily SKU"] = { sow: skuBase, actual: skuAct > 0 ? skuAct : null };
    }
  }

  const payload = {
    batch:         ad.batch        || null,
    resource:      ad.resource     || null,
    sla_matrix:    ad.slaMatrix    || null,
    sla_intel:     ad.slaIntelligence || null,
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
      ? `<p class="px-4 py-3 text-xs text-Cwhite/80 leading-relaxed border-b border-Cborder/30">${_esc(sec.prose)}</p>`
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
           <table class="w-full text-left border-collapse text-xs">
             <thead>
               <tr style="background:${hexA(accent, 0.08)}">
                 ${headers.map(h => `<th class="px-3 py-2 font-semibold uppercase tracking-wider text-[10px]"
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

    block.innerHTML = headHtml + proseHtml + tableHtml;
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
  const bk = window.appData.batch?.kpis || window.appData.batch || {};
  const rk = window.appData.resource?.kpis || window.appData.resource || {};
  const payload = {
    batch_kpis:    bk,
    top_jobs:      window.appData.batch?.top_jobs     || [],
    top_breaches:  window.appData.batch?.top_breaches || [],
    resource_kpis: rk,
    servers:       window.appData.servers?.length ? window.appData.servers : (window.appData.resource?.servers || []),
    sla_data:      window.appData.slaMatrix || {},
    sub_stats:     window.appData.batch?.sub_stats    || [],
    window:        window.appData.batch?.window       || [],
    hourly_counts: window.appData.batch?.hourly_counts || {},
    benchmark:     window.appData.benchmark || null,
    sow_compare:   window.appData.sowCompare || null,
    findings:      window._lastFindings || [],
    daily_jobs:    window.appData.batch?.daily_jobs   || {},
    customer_name: window.appData.customerName || null,
    deep_dive:     window.appData.deepDive || _buildDeepDiveSummary(),
  };

  // ── Fast-path: if cached exec data exists and source data unchanged, reuse
  const payloadHash = JSON.stringify(payload).length; // cheap proxy
  if (window._execCache && window._execCacheHash === payloadHash) {
    if (loading) loading.classList.add("hidden");
    if (content) content.classList.remove("hidden");
    const data = window._execCache;
    _renderExecDecisionStrip(data);
    _renderExecKPIs(data.kpis);
    _renderExecNarrative(data.narrative);
    _renderExecBenchmarkSummary(window.appData.benchmark);
    _renderExecHotSpots(data);
    _renderExecResourceHealth(data.server_heatmap, data.kpis);
    _renderExecTopRiskJobs(data.job_sla_bars);
    _renderExecSowPanel(data.sow_panel);
    requestAnimationFrame(() => {
      _renderExecSLABars(data.job_sla_bars);
      _renderExecTemporal(data.temporal, data.kpis);
      _renderExecBreachCalendar(data.breach_calendar);
      _renderExecConcurrency(data.concurrency);
      _renderExecForecast(window.appData?.batch?.window || [], data.kpis?.sla_daily_hrs || 6);
      _renderSignoffChecklistV2(data.decision);
    });
    return;
  }

  try {
    // Fire findings + exec-dashboard in parallel — don't block exec on findings
    const findingsReady = (!window._lastFindings || !window._lastFindings.length)
      ? triggerGenerateFindings().catch(() => {})
      : Promise.resolve();

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

    // ── Phase 1: instant — lightweight DOM writes (KPIs, strips, narrative)
    _renderExecDecisionStrip(data);
    _renderExecKPIs(data.kpis);
    _renderExecNarrative(data.narrative);
    _renderExecBenchmarkSummary(window.appData.benchmark);
    _renderExecHotSpots(data);
    _renderExecResourceHealth(data.server_heatmap, data.kpis);
    _renderExecTopRiskJobs(data.job_sla_bars);
    _renderExecSowPanel(data.sow_panel);

    // ── Phase 2: deferred — heavy Plotly charts staggered across frames
    const deferredCharts = [
      () => _renderExecSLABars(data.job_sla_bars),
      () => _renderExecTemporal(data.temporal, data.kpis),
      () => _renderExecBreachCalendar(data.breach_calendar),
      () => _renderExecConcurrency(data.concurrency),
      () => _renderExecForecast(
             window.appData?.batch?.window || [],
             data.kpis?.sla_daily_hrs || 6),
      () => _renderSignoffChecklistV2(data.decision),
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
    _handleFetchError(err);
  }
}

// ── KPI Strip ────────────────────────────────────────────────
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

  // ── Fleet Grade — letter + gradient bullet bar ──
  const flEl = document.getElementById("exec-fleet");
  const gradePct = { "A": 95, "B": 80, "C": 65, "D": 50, "F": 25, "N/A": 0 };
  const gc = { "A": "#10d96e", "B": "#10d96e", "C": "#f59e0b", "D": "#f43f5e", "F": "#f43f5e", "N/A": "#6b7db3" };
  if (flEl) {
    flEl.textContent = kpis.fleet_grade || "—";
    flEl.style.color = gc[kpis.fleet_grade] || "#f0f4ff";
  }
  setText("exec-fleet-sub", `${kpis.total_servers || 0} servers`);
  const flBar = document.getElementById("exec-fleet-bar");
  if (flBar) flBar.style.width = (gradePct[kpis.fleet_grade] || 0) + "%";

  // ── RFCS — number + bullet marker on gradient ──
  const rfEl = document.getElementById("exec-rfcs");
  const rfcs = _n(kpis.rfcs);
  if (rfEl) {
    rfEl.textContent = rfcs.toFixed(0);
    rfEl.style.color = rfcs >= 60 ? "#f43f5e" : (rfcs >= 30 ? "#f59e0b" : "#10d96e");
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
        xref: "paper", yref: "y",
        x: 1, y: ceiling, xanchor: "left", yanchor: "middle",
        text: `SLA: ${ceiling}h`,
        font: { size: 10, color: "#f43f5e", family: "monospace" },
        showarrow: false,
      },
      {
        xref: "paper", yref: "y",
        x: 1, y: ceiling * 0.9, xanchor: "left", yanchor: "top",
        text: `Buffer: ${(ceiling*0.1).toFixed(1)}h`,
        font: { size: 8, color: "#94a3b8" },
        showarrow: false,
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

// ── Job Concurrency Timeline (worst-day Gantt) ────────────────
function _renderExecConcurrency(payload) {
  const el       = document.getElementById("exec-chart-concurrency");
  const empty    = document.getElementById("exec-concurrency-empty");
  const sumEl    = document.getElementById("exec-concurrency-summary");
  const dateSel  = document.getElementById("exec-concurrency-date");
  if (!el) return;

  if (!payload?.available || !payload.bars?.length) {
    el.innerHTML = "";
    if (empty) empty.classList.remove("hidden");
    if (sumEl) sumEl.textContent = payload?.reason || "";
    if (dateSel) dateSel.innerHTML = "";
    return;
  }
  if (empty) empty.classList.add("hidden");

  // Date dropdown — populated once per render of fresh payload
  if (dateSel) {
    const dates = payload.available_dates || [payload.selected_date];
    dateSel.innerHTML = dates.map(d =>
      `<option value="${_esc(d)}" ${d === payload.selected_date ? "selected" : ""}>${_esc(d)}</option>`
    ).join("");
    dateSel.onchange = () => _renderExecConcurrencyForDate(dateSel.value);
  }

  _drawConcurrencyChart(payload, payload.bars);
  if (sumEl) sumEl.textContent = payload.summary || "";
}

function _renderExecConcurrencyForDate(date) {
  const dailyJobs = window.appData?.batch?.daily_jobs || {};
  const ceiling   = window._execCache?.kpis?.sla_daily_hrs || 6;
  const raw = dailyJobs[date] || [];
  if (!raw.length) return;
  // Compute window boundaries from capped daily_jobs
  const allStarts = raw.map(j => Number(j.start_hr || 0)).filter(h => h > 0);
  const allEnds   = raw.map(j => Number(j.end_hr   || 0)).filter(h => h > 0);
  let wStart = allStarts.length ? Math.min(...allStarts) : 0;
  let wEnd   = allEnds.length   ? Math.max(...allEnds)   : 0;
  let windowLen = wEnd - wStart;

  // Prefer authoritative elapsed_hrs from window data (covers ALL jobs,
  // not just the capped subset in daily_jobs).
  const winRec = (window.appData?.batch?.window || []).find(
    w => (w.run_date || w.date) === date
  );
  if (winRec && Number(winRec.elapsed_hrs) > 0 && wEnd > 0) {
    windowLen = Number(winRec.elapsed_hrs);
    wStart    = wEnd - windowLen;
  }

  const slaDeadline = wStart + ceiling;
  // Sort by end_hr desc, top 15
  const bars = raw.slice().sort((a,b) => (b.end_hr||0) - (a.end_hr||0)).slice(0, 15)
    .map(j => ({
      job:       j.job,
      start_hr:  Number(j.start_hr || 0),
      end_hr:    Number(j.end_hr   || 0),
      duration:  Number((j.end_hr || 0) - (j.start_hr || 0)),
      exceeds_sla: Number(j.end_hr || 0) > slaDeadline,
    }));
  const top3 = bars.slice(0, 3).map(b => b.job);
  const summary = `On ${date}, window elapsed ${windowLen.toFixed(2)}h (SLA: ${ceiling}h). Top 3: ${top3.join(", ")}.`;
  _drawConcurrencyChart({ ceiling, selected_date: date, summary, window_start: wStart }, bars);
  setText("exec-concurrency-summary", summary);
}

function _drawConcurrencyChart(meta, bars) {
  const el = document.getElementById("exec-chart-concurrency");
  if (!el || typeof Plotly === "undefined") return;
  const ceiling = meta.ceiling || 6;
  const wStart  = meta.window_start ?? Math.min(...bars.map(b => b.start_hr));
  const slaDeadline = wStart + ceiling;  // SLA ceiling as hour-of-day
  // Sort: latest-ending at top → earliest-ending at bottom (Plotly flips, so push descending)
  const sorted = bars.slice().sort((a,b) => a.end_hr - b.end_hr);
  const yLabels = sorted.map(b => b.job);
  // Use a stacked bar: invisible "spacer" up to start_hr, then duration bar.
  const spacer = {
    type: "bar", orientation: "h",
    y: yLabels, x: sorted.map(b => b.start_hr),
    marker: { color: "rgba(0,0,0,0)" },
    hoverinfo: "skip",
    showlegend: false,
  };
  const main = {
    type: "bar", orientation: "h",
    y: yLabels, x: sorted.map(b => b.duration),
    marker: {
      color: sorted.map(b => b.exceeds_sla ? "#f43f5e" : "#10d96e"),
      line:  { color: "#0f172a", width: 0.5 },
    },
    hovertemplate: sorted.map(b =>
      `${b.job}<br>Start: ${b.start_hr}h<br>End: ${b.end_hr}h<br>Duration: ${b.duration}h<extra></extra>`),
    showlegend: false,
  };

  const traces = [spacer, main];

  // Memory overlay — fleet average memory % as a red line on secondary Y axis
  const serverData = window.appData?.servers || [];
  const avgMem = serverData.length
    ? serverData.reduce((s, sv) => s + (parseFloat(sv.mem_used || sv.mem || 0)), 0) / serverData.length
    : 0;
  if (avgMem > 0) {
    // Show as a horizontal annotation line at the fleet avg memory level
    // Since this is a horizontal bar chart, we overlay memory info as text
    traces.push({
      type: "scatter", mode: "lines", name: `Fleet Avg Mem ${avgMem.toFixed(0)}%`,
      x: [0, ceiling + 2], y: [yLabels[yLabels.length-1], yLabels[yLabels.length-1]],
      line: { color: "#f43f5e", width: 0 },
      showlegend: true,
      hoverinfo: "skip",
    });
  }

  const layout = {
    paper_bgcolor: "transparent",
    plot_bgcolor:  "transparent",
    barmode: "stack",
    margin: { l: 130, r: 24, t: 10, b: 36 },
    xaxis: {
      title: { text: "Hour of day", font: { size: 10, color: "#94a3b8" } },
      tickfont: { size: 9, color: "#94a3b8" },
      gridcolor: "rgba(148,163,184,0.08)",
    },
    yaxis: {
      tickfont: { size: 9, color: "#cbd5e1" },
      automargin: true,
    },
    shapes: [{
      type: "line",
      x0: slaDeadline, x1: slaDeadline,
      yref: "paper", y0: 0, y1: 1,
      line: { color: "#f43f5e", width: 1.6, dash: "dash" },
    }],
    annotations: [
      {
        x: slaDeadline, xanchor: "left",
        yref: "paper", y: 1.02,
        text: `SLA ceiling ${ceiling}h`,
        font: { size: 9, color: "#f43f5e", family: "monospace" },
        showarrow: false,
      },
      ...(avgMem > 0 ? [{
        xref: "paper", x: 0.99, yref: "paper", y: 0.97,
        xanchor: "right", yanchor: "top",
        text: `Fleet Avg Mem: ${avgMem.toFixed(1)}%`,
        font: { size: 10, color: avgMem >= 80 ? "#f43f5e" : avgMem >= 60 ? "#f59e0b" : "#10d96e", family: "monospace" },
        showarrow: false,
        bgcolor: "rgba(13,21,38,0.8)", borderpad: 3,
      }] : []),
    ],
    legend: avgMem > 0 ? { orientation: "h", y: -0.15, font: { size: 9, color: "#a8b3d9" } } : undefined,
  };
  Plotly.react(el, traces, layout, _plotlyConfig({ scrollZoom: false }));
}

// ── SOW vs Actual panel (3-column) ────────────────────────────
function _renderExecSowPanel(panel) {
  const card  = document.getElementById("exec-sow-card");
  const grid  = document.getElementById("exec-sow-grid");
  const empty = document.getElementById("exec-sow-empty");
  const stat  = document.getElementById("exec-sow-status");
  if (!card) return;

  if (!panel?.available) {
    if (grid)  grid.classList.add("hidden");
    if (empty) empty.classList.remove("hidden");
    if (stat)  {
      stat.textContent = "NOT LOADED";
      stat.style.color = "#94a3b8";
      stat.style.borderColor = "#475569";
      stat.style.background  = "rgba(148,163,184,0.12)";
    }
    return;
  }
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
        pct:   Number(r.pct || 0),
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

  // Col 2: SLA narrative
  const slaSum = document.getElementById("exec-sow-sla-summary");
  if (slaSum) {
    const ceiling = window._execCache?.kpis?.sla_daily_hrs ?? "—";
    const breachCount = window._execCache?.decision?.breach_days ?? 0;
    const totalDays   = window._execCache?.decision?.total_days  ?? 0;
    slaSum.textContent = totalDays
      ? `Contracted ceiling: ${ceiling}h. ${breachCount} of ${totalDays} days breached.`
      : `Contracted ceiling: ${ceiling}h.`;
  }

  // Col 3: Capacity
  const capEl = document.getElementById("exec-sow-capacity");
  if (capEl) {
    const cap = panel.capacity || [];
    if (!cap.length) {
      capEl.innerHTML = `<p class="text-[11px] text-Cmuted">No capacity baselines in SOW.</p>`;
    } else {
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
    const isMatrix = sla.type === "sla_matrix";
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
  const cats = bench.categories || [];
  const fr = bench.fill_rate || [];
  const obs = bench.observations || [];
  const totalTx = bench.total_transactions || 0;
  const degraded = bench.degraded || 0;
  const passRate = totalTx > 0 ? Math.round((totalTx - degraded) / totalTx * 100) : 0;
  const color = degraded > 0 ? "border-Camber/40 bg-Camber/5" : "border-Cgreen/40 bg-Cgreen/5";

  let catCards = "";
  cats.forEach(c => {
    const pct = c.total > 0 ? Math.round(c.passed / c.total * 100) : 0;
    const badge = c.degraded > 0 ? "text-Cred" : "text-Cgreen";
    catCards += `<div class="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-Ccard/50 border border-Cborder/30">
      <span class="text-lg font-bold ${badge}">${pct}%</span>
      <div class="text-[10px]"><div class="text-Cwhite font-semibold">${_esc(c.name)}</div>
        <div class="text-Cmuted">${c.passed}/${c.total} pass${c.degraded > 0 ? ` · ${c.degraded} red` : ""}</div>
      </div></div>`;
  });

  el.innerHTML = `<div class="rounded-xl border ${color} p-4">
    <div class="flex items-center gap-3 mb-3">
      <span class="text-lg">⚡</span>
      <div>
        <div class="text-sm font-bold text-Cwhite">UI Performance Benchmark</div>
        <div class="text-[10px] text-Cmuted">${totalTx} transactions · ${passRate}% pass rate${fr.length ? ` · ${fr.length} fill rate entries` : ""}${obs.length ? ` · ${obs.length} SIT obs` : ""}</div>
      </div>
    </div>
    ${catCards ? `<div class="flex flex-wrap gap-2 mt-2">${catCards}</div>` : ""}
  </div>`;
}

// ── Panel A: Batch Runtime vs SLA Ceiling (Horizontal Bar) ───
function _renderExecSLABars(jobs) {
  const el = document.getElementById("exec-chart-sla-bars");
  if (!el || !jobs?.length) return;

  const labels = jobs.map(j => j.job_name.length > 25 ? j.job_name.slice(0, 22) + "…" : j.job_name);
  const peaks  = jobs.map(j => j.peak_hrs);
  const colors = jobs.map(j => j.status === "BREACH" ? "#f43f5e" : (j.status === "AT_RISK" ? "#f59e0b" : "#10d96e"));
  const sla    = jobs.length ? jobs[0].sla_ceiling : 6;

  const traces = [
    {
      type: "bar", orientation: "h",
      y: labels, x: peaks,
      marker: { color: colors, line: { width: 0 } },
      text: jobs.map(j => `SRI: ${_n(j.sri).toFixed(2)}`),
      textposition: "outside",
      textfont: { size: 9, color: "#6b7db3" },
      hovertemplate: "%{y}<br>Peak: %{x:.2f}h<br>Buffer: %{customdata:.0f}%<extra></extra>",
      customdata: jobs.map(j => j.buffer_pct),
      name: "Peak Runtime",
    },
  ];

  const layout = {
    ..._EXEC_LAYOUT_BASE,
    margin: { l: 140, r: 60, t: 10, b: 35 },
    xaxis: { ..._EXEC_LAYOUT_BASE.xaxis, title: { text: "Hours", font: { size: 10 } } },
    yaxis: { ..._EXEC_LAYOUT_BASE.yaxis, autorange: "reversed" },
    shapes: [{
      type: "line", x0: sla, x1: sla, y0: -0.5, y1: labels.length - 0.5,
      line: { color: "#f43f5e", width: 2, dash: "dash" },
    }],
    annotations: [{
      x: sla, y: -0.3, text: `SLA ${sla}h`, showarrow: false,
      font: { size: 9, color: "#f43f5e" }, xanchor: "left",
    }],
    showlegend: false,
    bargap: 0.15,
  };

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

  Plotly.newPlot(el, traces, layout, _EXEC_CFG);
}

// ── NEW: Resource Health Summary (replaces raw heatmap in Row 2) ─
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

  const fleetGrade = kpis?.fleet_grade || "—";
  const gradeColor = fleetGrade === "A" ? "#10d96e" : fleetGrade === "B" ? "#3b82f6"
                   : fleetGrade === "C" ? "#f59e0b" : "#f43f5e";
  const total = servers.length;

  // Compute per-metric aggregates
  const cpuVals  = servers.map(s => s.cpu || 0).filter(v => v > 0);
  const memVals  = servers.map(s => s.mem || 0).filter(v => v > 0);
  const diskVals = servers.map(s => s.disk || 0);
  const avg = arr => arr.length ? arr.reduce((a,b) => a+b, 0) / arr.length : 0;
  const peak = arr => arr.length ? Math.max(...arr) : 0;

  const avgCpu = avg(cpuVals), peakCpu = peak(cpuVals);
  const avgMem = avg(memVals), peakMem = peak(memVals);
  const avgDisk = avg(diskVals), peakDisk = peak(diskVals);

  const statusFor = (avgV, peakV) => {
    if (peakV >= RESOURCE_THRESHOLDS.cpu_warn) return { icon: "🔴", label: "CRITICAL", color: "#f43f5e" };
    if (peakV >= RESOURCE_THRESHOLDS.cpu_ok || avgV >= 60) return { icon: "⚠️", label: "WARNING", color: "#f59e0b" };
    return { icon: "✅", label: "OK", color: "#10d96e" };
  };
  const cpuStatus = statusFor(avgCpu, peakCpu);
  const memStatus = statusFor(avgMem, peakMem);
  const diskStatus = statusFor(avgDisk, peakDisk);

  // Risk distribution
  const bands = { critical: 0, warning: 0, healthy: 0 };
  servers.forEach(s => {
    const p = Math.max(s.cpu || 0, s.mem || 0, s.disk || 0);
    if (p >= RESOURCE_THRESHOLDS.cpu_warn) bands.critical++;
    else if (p >= RESOURCE_THRESHOLDS.cpu_ok) bands.warning++;
    else bands.healthy++;
  });

  // 3 worst servers by peak metric
  const worst3 = servers
    .map(s => ({ host: s.host, peak: Math.max(s.cpu||0, s.mem||0, s.disk||0), cpu: s.cpu||0, mem: s.mem||0, disk: s.disk||0 }))
    .sort((a, b) => b.peak - a.peak)
    .slice(0, 3);
  const worstMetric = s => s.mem >= s.cpu && s.mem >= s.disk ? "MEM" : s.cpu >= s.disk ? "CPU" : "DISK";
  const worstVal = s => s.mem >= s.cpu && s.mem >= s.disk ? s.mem : s.cpu >= s.disk ? s.cpu : s.disk;

  el.innerHTML = `
    <!-- Fleet grade + 3-metric columns -->
    <div class="flex items-center gap-3 mb-2">
      <div class="text-3xl font-extrabold" style="color:${gradeColor}">${fleetGrade}</div>
      <div>
        <div class="text-[9px] uppercase tracking-widest text-Cmuted">Fleet Grade</div>
        <div class="text-[11px] text-Cwhite">${total} server${total!==1?'s':''}</div>
      </div>
    </div>
    <!-- CPU / Memory / Disk strip -->
    <div class="grid grid-cols-3 gap-1.5 text-center mb-2">
      <div class="rounded-lg border border-Cborder/30 bg-Cbg/40 p-1.5">
        <div class="text-[8px] uppercase tracking-widest text-Cmuted font-bold">CPU</div>
        <div class="text-[13px] font-bold text-Cwhite">${avgCpu.toFixed(1)}%</div>
        <div class="text-[9px] text-Cmuted">Peak ${peakCpu.toFixed(1)}%</div>
        <div class="text-[9px] font-bold" style="color:${cpuStatus.color}">${cpuStatus.icon} ${cpuStatus.label}</div>
      </div>
      <div class="rounded-lg border border-Cborder/30 bg-Cbg/40 p-1.5">
        <div class="text-[8px] uppercase tracking-widest text-Cmuted font-bold">MEMORY</div>
        <div class="text-[13px] font-bold text-Cwhite">${avgMem.toFixed(1)}%</div>
        <div class="text-[9px] text-Cmuted">Peak ${peakMem.toFixed(1)}%</div>
        <div class="text-[9px] font-bold" style="color:${memStatus.color}">${memStatus.icon} ${memStatus.label}</div>
      </div>
      <div class="rounded-lg border border-Cborder/30 bg-Cbg/40 p-1.5">
        <div class="text-[8px] uppercase tracking-widest text-Cmuted font-bold">DISK</div>
        <div class="text-[13px] font-bold text-Cwhite">${avgDisk.toFixed(1)}%</div>
        <div class="text-[9px] text-Cmuted">Peak ${peakDisk.toFixed(1)}%</div>
        <div class="text-[9px] font-bold" style="color:${diskStatus.color}">${diskStatus.icon} ${diskStatus.label}</div>
      </div>
    </div>
    <!-- Risk distribution bar -->
    <div class="mb-1">
      <div class="flex h-2.5 rounded overflow-hidden mb-1">
        ${bands.critical ? `<div class="bg-[#f43f5e]" style="width:${(bands.critical/total*100).toFixed(1)}%"></div>` : ''}
        ${bands.warning  ? `<div class="bg-[#f59e0b]" style="width:${(bands.warning/total*100).toFixed(1)}%"></div>` : ''}
        ${bands.healthy  ? `<div class="bg-[#10d96e]" style="width:${(bands.healthy/total*100).toFixed(1)}%"></div>` : ''}
      </div>
      <div class="flex gap-2 text-[8px] text-Cmuted">
        <span><span class="inline-block w-1.5 h-1.5 rounded-sm bg-[#f43f5e] mr-0.5"></span>${bands.critical} crit</span>
        <span><span class="inline-block w-1.5 h-1.5 rounded-sm bg-[#f59e0b] mr-0.5"></span>${bands.warning} warn</span>
        <span><span class="inline-block w-1.5 h-1.5 rounded-sm bg-[#10d96e] mr-0.5"></span>${bands.healthy} ok</span>
      </div>
    </div>
    <!-- Worst 3 servers -->
    <div>
      <div class="text-[8px] uppercase tracking-widest text-Cmuted font-bold mb-0.5">Highest Risk</div>
      ${worst3.map(s => {
        const col = s.peak >= 80 ? "#f43f5e" : s.peak >= 60 ? "#f59e0b" : "#10d96e";
        const name = s.host.length > 18 ? s.host.slice(0,15)+"…" : s.host;
        const wm = worstMetric(s);
        const wv = worstVal(s);
        return `<div class="flex items-center gap-1.5 py-0.5 text-[10px]">
          <span class="w-1.5 h-1.5 rounded-full flex-shrink-0" style="background:${col}"></span>
          <span class="text-Cwhite truncate flex-1" title="${_esc(s.host)}">${_esc(name)}</span>
          <span class="font-mono font-bold" style="color:${col}">${wm} ${wv.toFixed(0)}%</span>
        </div>`;
      }).join("")}
    </div>
  `;
}

// ── NEW: Top 3 At-Risk Jobs (replaces sub-app table in Row 2) ──
function _renderExecTopRiskJobs(jobs) {
  const el = document.getElementById("exec-top-risk-jobs");
  if (!el) return;
  if (!jobs?.length) {
    el.innerHTML = '<p class="text-Cmuted text-[11px] py-4 text-center">No SLA job data loaded</p>';
    return;
  }

  // Sort by SRI descending (worst first), deduplicate by job name, take top 3
  const _seen = new Set();
  const top3 = jobs.slice().sort((a, b) => (b.sri||0) - (a.sri||0))
    .filter(j => { const k = j.job_name || ''; if (_seen.has(k)) return false; _seen.add(k); return true; })
    .slice(0, 3);

  el.innerHTML = top3.map((j, i) => {
    const sri = j.sri || 0;
    const col = sri > 1 ? "#f43f5e" : sri > 0.85 ? "#f59e0b" : "#10d96e";
    const pct = j.sla_ceiling > 0 ? ((j.peak_hrs / j.sla_ceiling) * 100).toFixed(0) : "—";
    const buffer = j.buffer_pct != null ? j.buffer_pct.toFixed(1) : "—";
    const name = (j.job_name || "Unknown").length > 30
               ? (j.job_name || "Unknown").slice(0,27) + "…"
               : (j.job_name || "Unknown");
    return `
      <div class="rounded-lg border border-Cborder/40 bg-Ccard/40 p-3" style="border-left:3px solid ${col};">
        <div class="flex items-center justify-between gap-2 mb-1">
          <span class="text-[12px] font-bold text-Cwhite truncate" title="${_esc(j.job_name||'')}">#${i+1} ${_esc(name)}</span>
          <span class="text-[10px] font-bold px-1.5 py-0.5 rounded-full" style="color:${col};background:${col}22;">${j.status || (sri>1?'BREACH':'OK')}</span>
        </div>
        <div class="grid grid-cols-3 gap-1 text-center">
          <div>
            <div class="text-[9px] text-Cmuted">Peak</div>
            <div class="text-[12px] font-bold text-Cwhite">${_n(j.peak_hrs).toFixed(1)}h</div>
          </div>
          <div>
            <div class="text-[9px] text-Cmuted">Ceiling</div>
            <div class="text-[12px] font-bold text-Cwhite">${_n(j.sla_ceiling).toFixed(1)}h</div>
          </div>
          <div>
            <div class="text-[9px] text-Cmuted">Buffer</div>
            <div class="text-[12px] font-bold" style="color:${col}">${buffer}%</div>
          </div>
        </div>
        <div class="mt-1.5 h-1.5 rounded bg-Cbg/80 overflow-hidden">
          <div class="h-full rounded" style="width:${Math.min(Number(pct)||0, 100)}%;background:${col}"></div>
        </div>
        <div class="text-[9px] text-Cmuted mt-0.5 text-right">SRI ${sri.toFixed(2)} · ${pct}% utilization</div>
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

  Plotly.newPlot(el, traces, layout, _EXEC_CFG);
}

// ── Panel F: OSHS Waterfall ──────────────────────────────────
function _renderExecWaterfall(wf, oshs) {
  const el = document.getElementById("exec-chart-waterfall");
  if (!el || !wf) return;
  if (typeof Plotly === "undefined") return;

  // Multi-arc radial gauge: each pillar is its own concentric ring.
  // Score = (contribution / weight_pts) * 100 (compliance %).
  const pillars = [
    { name: "Batch",    val: wf.batch_contribution,    max: 40, color: "#22d3ee" },
    { name: "Resource", val: wf.resource_contribution, max: 35, color: "#a78bfa" },
    { name: "SLA",      val: wf.sla_contribution,      max: 25, color: "#fbbf24" },
  ];
  const total = _n(wf.total);
  const totalCol = total >= 75 ? "#10d96e" : total >= 60 ? "#f59e0b" : "#f43f5e";
  const grade = oshs?.grade || "?";

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
            <span class="font-bold" style="color:${p.color};">${_n(p.val).toFixed(1)}<span class="text-Cmuted/60 font-normal">/${p.max}</span></span>
            <span class="text-[9px] text-Cmuted">(${Math.round((p.val / p.max) * 100)}%)</span>
          </span>`).join("")}
      </div>
    </div>
  `;
}

// ── Narrative Panel — 3-section color coded ──────────────────
function _renderExecNarrative(findings) {
  const el = document.getElementById("exec-narrative");
  if (!el) return;

  // Normalise input
  if (!findings) findings = [];
  else if (typeof findings === "string") {
    findings = findings.trim() ? [{ key: "coverage", icon: "🛡️", level: "info", text: findings }] : [];
  } else if (!Array.isArray(findings)) {
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

// ── Hot Spots — crux strip above the narrative ───────────────
// Pulls the worst job, worst server, peak-load hour, total breach hours,
// and trend direction from the cross-pillar data already on the page.
function _renderExecHotSpots(data) {
  const el = document.getElementById("exec-hotspots");
  if (!el) return;

  const tiles = [];

  // 1. Worst Sub-App (highest SRI)
  const subs = (data?.sub_app_metrics || []).slice().sort((a, b) => (b.sri || 0) - (a.sri || 0));
  if (subs.length) {
    const w = subs[0];
    const col = w.sri > 1 ? "#f43f5e" : w.sri > 0.85 ? "#f59e0b" : "#10d96e";
    tiles.push({
      label: "Worst Sub-App",
      value: w.sub_app,
      sub: `SRI ${_n(w.sri).toFixed(2)} · ${w.job_count} jobs`,
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
  const win = window.appData?.batch?.window || [];
  const sla = data?.kpis?.sla_daily_hrs || 6;
  const _whrs = (w) => { const e = Number(w.elapsed_hrs ?? 0); return e > 0 ? e : Number(w.total_hrs ?? 0); };
  const totalOverrun = win.reduce((s, w) => s + Math.max(0, _whrs(w) - sla), 0);
  const breachDays = win.filter(w => _whrs(w) > sla).length;
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
      label: "Evidence coverage ≥ 30 days",
      actual: `${evidenceDays}d`,
      pass: evidenceDays >= 30,
      severity: "blocker",
      hint: evidenceDays < 30 ? `Only ${evidenceDays} day(s) on file — need ${30 - evidenceDays} more for full audit confidence.` : "Sufficient history.",
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
    .filter(w => w && w.run_date && Number.isFinite(Number(w.elapsed_hrs ?? w.total_hrs)));

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
  const ys = windowData.map(w => Number(w.elapsed_hrs > 0 ? w.elapsed_hrs : w.total_hrs));
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
async function triggerRedFlags() {
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
async function triggerPeConsultant() {
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
  if (!container || !data) return;

  const { jobs = [], dates = [], cells = [], limit = 6.0 } = data;
  if (!jobs.length || !dates.length) {
    if (section) section.classList.add("hidden");
    return;
  }

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

  // Cell background colour
  const cellColor = (c) => {
    if (!c || c.hrs === null || c.hrs === undefined) return "#0f3d24";
    if (c.breach)                return "#f43f5e";
    if (c.hrs > limit * 0.85)   return "#f59e0b";
    return "#10d96e";
  };

  // Tooltip text
  const cellTitle = (c) => {
    if (!c || c.hrs === null || c.hrs === undefined) return "No run";
    return `${c.hrs.toFixed(2)} h — ${c.breach ? "BREACH" : "OK"}`;
  };

  const shortDates = dates.map(fmtDate);

  let html = `
    <table class="text-[10px] border-collapse min-w-max">
      <thead>
        <tr>
          <th class="sticky left-0 z-10 bg-Ccard text-left pr-3 pb-1 text-Cmuted font-semibold
                      whitespace-nowrap" style="min-width:150px">Job</th>
          ${shortDates.map((d, i) =>
            `<th class="pb-1 px-0.5 text-Cmuted font-normal text-center whitespace-nowrap"
                 title="${_esc(dates[i])}">${_esc(d)}</th>`
          ).join("")}
        </tr>
      </thead>
      <tbody>`;

  for (const job of jobs) {
    html += `<tr class="hover:brightness-125 transition-[filter]">
      <td class="sticky left-0 z-10 bg-Ccard pr-3 py-0.5 text-Cwhite font-mono
                 whitespace-nowrap max-w-[200px] truncate" title="${_esc(job)}">${_esc(job)}</td>
      ${dates.map((date) => {
        const c  = lookup[`${job}||${date}`];
        const bg = cellColor(c);
        const tt = cellTitle(c);
        return `<td class="px-0.5 py-0.5 text-center" title="${tt}" style="min-width:22px">
          <div style="width:20px;height:15px;background:${bg};border-radius:2px;margin:auto"></div>
        </td>`;
      }).join("")}
    </tr>`;
  }

  html += `</tbody></table>`;
  container.innerHTML = html;
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
  if (!container || !data) return;

  const { sub_apps = [], hours = [], cells = [] } = data;
  if (!sub_apps.length || !cells.length) {
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

    // Restore customer chip from persisted config (set when last Ctrl-M file was uploaded)
    if (cfg.customer_name) {
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

function loadSettings() { loadConfig(); _loadAzureStatusBadge(); checkAzureIdentity(); }

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

async function checkAzureIdentity() {
  const el = document.getElementById("az-identity-status");
  if (!el) return;
  el.innerHTML = '<span class="text-Cmuted">Checking…</span>';
  try {
    const res = await fetch("/api/azure/auth-status");
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
      loadAzureSubscriptions("");
      _updateUploadAzureStatus(true, displayName || userId, { tenant_id: d.tenant_id || "", method: d.method || "" });
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
  if (btn) { btn.disabled = true; btn.textContent = "Opening browser…"; }
  if (statusEl) { statusEl.textContent = "Waiting for browser sign-in…"; statusEl.className = "text-xs text-Cmuted"; }

  try {
    const res  = await fetch("/api/azure/browser-login", { method: "POST" });
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

    // Update the fetch modal if it's open
    const notConf = document.getElementById("azure-modal-not-configured");
    const form    = document.getElementById("azure-modal-form");
    if (notConf) notConf.classList.add("hidden");
    if (form)    form.classList.remove("hidden");

  } catch (err) {
    if (statusEl) { statusEl.textContent = `❌ ${err?.message || err}`; statusEl.className = "text-xs text-Cred"; }
    toast("error", "Browser login error", err?.message || String(err));
  } finally {
    if (btn && !btn.classList.contains("hidden")) {
      btn.disabled = false;
      btn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"/></svg> Sign in with Browser';
    }
  }
}

async function azureBrowserLogout() {
  try {
    await fetch("/api/azure/browser-logout", { method: "POST" });
    // Show sign-in, hide sign-out
    const loginBtn  = document.getElementById("az-browser-login-btn");
    const logoutBtn = document.getElementById("az-browser-logout-btn");
    if (loginBtn)  loginBtn.classList.remove("hidden");
    if (logoutBtn) logoutBtn.classList.add("hidden");
    const statusEl = document.getElementById("az-browser-login-status");
    if (statusEl) { statusEl.textContent = "Browser session cleared."; statusEl.className = "text-xs text-Cmuted"; }
    // Refresh identity
    checkAzureIdentity();
    toast("info", "Signed out", "Browser credential cleared. Will fall back to az login.");
  } catch (_) {}
}

async function loadAzureSubscriptions(defaultSubId) {
  const sel = document.getElementById("az-subscription-id");
  if (!sel) return;
  sel.innerHTML = '<option value="">Loading subscriptions…</option>';
  try {
    const cfgRes = await fetch("/api/azure/status");
    const cfgData = await cfgRes.json();
    const savedSubId = cfgData.azure_subscription_id_set ? cfgData.azure_subscription_id_value : "";
    const savedRg = cfgData.azure_resource_group_set ? cfgData.azure_resource_group_value : "";

    const res = await fetch("/api/azure/subscriptions");
    const d = await res.json();
    if (d.ok && d.subscriptions.length > 0) {
      sel.innerHTML = '';
      let selected = false;
      d.subscriptions.forEach(s => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = `${s.name} (${s.id.slice(0,8)}…)`;
        if (savedSubId && s.id === savedSubId) {
          opt.selected = true;
          selected = true;
        } else if (!savedSubId && defaultSubId && s.id === defaultSubId) {
          opt.selected = true;
          selected = true;
        } else if (!savedSubId && !defaultSubId && s.is_default) {
          opt.selected = true;
          selected = true;
        }
        sel.appendChild(opt);
      });
      // Ensure one option is selected even if metadata does not mark default.
      if (!selected && sel.options.length > 0) {
        sel.selectedIndex = 0;
      }
      _autoSaveAzureConfig();
      loadAzureResourceGroups(sel.value, savedRg);
    } else {
      const msg = d.error ? `No subscriptions found — ${d.error}` : "No subscriptions found — check az login";
      sel.innerHTML = `<option value="">${_esc(msg)}</option>`;
    }
  } catch (_) {
    sel.innerHTML = '<option value="">Failed to load subscriptions</option>';
  }
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
  if (btn) { btn.disabled = true; btn.textContent = "Opening browser…"; }
  if (label) { label.textContent = "Waiting for browser sign-in…"; label.className = "text-Cmuted text-xs"; }
  if (dot) { dot.className = "w-2 h-2 rounded-full bg-Cmuted animate-pulse inline-block"; }
  try {
    const res = await fetch("/api/azure/browser-login", { method: "POST" });
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
    // Refresh the modal auth bar
    _refreshModalAuthBar();
  } catch (err) {
    if (label) { label.textContent = `❌ ${err.message}`; label.className = "text-red-400 text-xs"; }
    toast("error", "Browser login error", err.message);
  } finally {
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
    checkAzureIdentity();
  } catch (_) {}
}

/* ── Load subscriptions into the modal dropdown ── */
async function _loadModalSubscriptions() {
  const sel = document.getElementById("azure-modal-sub");
  if (!sel) return;
  try {
    // Try browser login subscriptions first (from auth status)
    const r = await fetch("/api/azure/subscriptions");
    const d = await r.json();
    const subs = d.subscriptions || [];
    sel.innerHTML = "";
    if (!subs.length) {
      sel.innerHTML = '<option value="">No subscriptions found</option>';
      return;
    }
    // Check if there's a configured subscription to pre-select
    let cfgSub = "";
    try { const c = await fetch("/api/azure/status"); const cs = await c.json(); cfgSub = cs.azure_subscription_id_value || ""; } catch {}

    for (const s of subs) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = `${s.name} (${s.id.slice(0,8)}…)`;
      if (s.id === cfgSub || s.is_default) opt.selected = true;
      sel.appendChild(opt);
    }
    // Auto-load RGs for selected subscription
    azureLoadRGs();
  } catch (e) {
    sel.innerHTML = '<option value="">Failed to load</option>';
  }
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

/* ── Helper: show discovered VMs in step 2 ── */
function _showDiscoveredVMs(data, statusEl, statusMsg) {
  _discoveredVMs = data.vms || [];
  if (!_discoveredVMs.length) {
    if (statusEl) { statusEl.textContent = "No VMs found."; statusEl.className = "text-xs text-amber-400"; }
    return;
  }
  const step2 = document.getElementById("azure-step2");
  if (step2) step2.classList.remove("hidden");
  const counts = data.counts || {};
  document.getElementById("azure-vm-total").textContent = `${data.total} VMs discovered`;
  document.getElementById("azure-vm-app-badge").textContent = `APP ${counts.APP || 0}`;
  document.getElementById("azure-vm-db-badge").textContent  = `DB ${counts.DB || 0}`;
  document.getElementById("azure-vm-sre-badge").textContent = `SRE ${counts.SRE || 0}`;
  _renderVMTable(_discoveredVMs);
  _updateSelectedCount();
  if (statusEl) { statusEl.textContent = statusMsg; statusEl.className = "text-xs text-emerald-400"; }
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
      html += `<tr class="bg-Cbg/60 border-t border-Cborder">
        <td class="px-2 py-1.5"><input type="checkbox" checked class="azure-cust-check rounded border-Cborder"
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
      const sub = vm.subscription_id ? vm.subscription_id.slice(0,8) + "…" : "";
      html += `<tr class="hover:bg-Cbg/40 azure-vm-row" data-type="${vm.type}" data-customer="${_escHtml(cust)}" data-idx="${idx}">
        <td class="px-2 py-1.5"><input type="checkbox" checked class="azure-vm-check rounded border-Cborder" data-rid="${_escHtml(vm.resource_id)}" data-customer="${_escHtml(cust)}" onchange="_updateSelectedCount()" /></td>
        <td class="px-2 py-1.5 text-Cwhite font-mono text-[11px]">${_escHtml(vm.name)}</td>
        <td class="px-2 py-1.5">
          <select class="azure-type-select bg-transparent border rounded px-1 py-0.5 text-[10px] font-bold ${colors}" data-idx="${idx}"
                  onchange="azureChangeVMType(${idx}, this.value)">
            <option value="APP" ${vm.type==='APP'?'selected':''} class="bg-Cbg text-Cwhite">APP</option>
            <option value="DB" ${vm.type==='DB'?'selected':''} class="bg-Cbg text-Cwhite">DB</option>
            <option value="SRE" ${vm.type==='SRE'?'selected':''} class="bg-Cbg text-Cwhite">SRE</option>
          </select>
        </td>
        <td class="px-2 py-1.5 text-Cmuted text-[10px]">${_escHtml(app)}</td>
        <td class="px-2 py-1.5 text-Cmuted text-[10px] max-w-[140px] truncate" title="${_escHtml(cust)}">${_escHtml(cust)}</td>
        <td class="px-2 py-1.5 text-Cmuted text-[10px]" title="${_escHtml(vm.subscription_id||'')}">${sub}</td>
        <td class="px-2 py-1.5 text-Cmuted text-[10px]">${_escHtml(vm.location)}</td>
      </tr>`;
    }
  }
  tbody.innerHTML = html;
}

/* Toggle all VMs for a specific customer */
function azureToggleCustomer(headerCb, customer) {
  document.querySelectorAll(`.azure-vm-check[data-customer="${customer}"]`).forEach(cb => {
    cb.checked = headerCb.checked;
  });
  _updateSelectedCount();
}

function _escHtml(s) { const d=document.createElement("div"); d.textContent=s||""; return d.innerHTML; }

/* ── Type override by user ── */
function azureChangeVMType(idx, newType) {
  if (_discoveredVMs[idx]) _discoveredVMs[idx].type = newType;
  // Update the select styling
  _renderVMTable(_discoveredVMs.filter(vm => {
    const activeFilter = document.querySelector(".azure-type-filter.bg-Cblue\\/20")?.dataset?.type || "ALL";
    return activeFilter === "ALL" || vm.type === activeFilter;
  }));
  // Update badges
  const counts = {APP:0,DB:0,SRE:0};
  _discoveredVMs.forEach(v => counts[v.type] = (counts[v.type]||0) + 1);
  document.getElementById("azure-vm-app-badge").textContent = `APP ${counts.APP}`;
  document.getElementById("azure-vm-db-badge").textContent  = `DB ${counts.DB}`;
  document.getElementById("azure-vm-sre-badge").textContent = `SRE ${counts.SRE}`;
}

/* ── Select / deselect all ── */
function azureSelectAll(checked) {
  document.querySelectorAll(".azure-vm-check").forEach(cb => cb.checked = checked);
  document.getElementById("azure-vm-checkall").checked = checked;
  _updateSelectedCount();
}
function azureToggleAll(checked) {
  document.querySelectorAll(".azure-vm-check").forEach(cb => cb.checked = checked);
  _updateSelectedCount();
}

function _updateSelectedCount() {
  const checks = document.querySelectorAll(".azure-vm-check:checked");
  const el = document.getElementById("azure-selected-count");
  if (el) el.textContent = `${checks.length} of ${_discoveredVMs.length} selected`;
}

/* ── Type filter ── */
function azureFilterType(type) {
  // Update button styles
  document.querySelectorAll(".azure-type-filter").forEach(b => {
    b.classList.toggle("bg-Cblue/20", b.dataset.type === type);
  });
  // Filter rows
  const filtered = type === "ALL" ? _discoveredVMs : _discoveredVMs.filter(v => v.type === type);
  _renderVMTable(filtered);
  _updateSelectedCount();
}

/* ── Step 2: Fetch metrics for selected VMs ── */
let _lastFetchedVmIds = [];  // Track for re-fetch with different duration

async function runAzureFetch() {
  const btn      = document.getElementById("azure-fetch-btn");
  const statusEl = document.getElementById("azure-fetch-status");
  const hours    = parseInt(document.getElementById("azure-modal-hours")?.value || "24");

  // Collect selected VM metadata (not just IDs — avoids redundant API calls)
  const selectedIds = [];
  const selectedVms = [];
  document.querySelectorAll(".azure-vm-check:checked").forEach(cb => {
    if (!cb.dataset.rid) return;
    selectedIds.push(cb.dataset.rid);
    // Find the full VM record from discovered VMs
    const vm = _discoveredVMs.find(v => v.resource_id === cb.dataset.rid);
    if (vm) selectedVms.push(vm);
  });

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
    triggerGenerateFindings().catch(() => {});

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
  const res = await fetch("/api/azure/fetch-resources-stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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
    renderResourceReview(payload);
    triggerGenerateFindings().catch(() => {});

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

  const hasSOW   = Object.keys(slaW).length > 0;
  const hasBatch = workflows.length > 0;

  if (!hasSOW && !hasBatch) return; // nothing to show yet
  panel.classList.remove("hidden");

  // ── Tier 2: SOW windows ──────────────────────────────────────────────
  const sowEl = document.getElementById("sla-sow-windows-rows");
  if (sowEl) {
    if (hasSOW) {
      const slaColors = { DAILY: "Ccyan", WEEKLY: "Cblue", MONTHLY: "Cpurple", BIWEEKLY: "Cteal" };
      sowEl.innerHTML = Object.entries(slaW).map(([btype, entry]) => {
        const hrs = entry.limit_hours ?? entry;
        const col = slaColors[btype] || "Ccyan";
        return `<div class="flex items-center justify-between rounded-lg border border-${col}/20 bg-${col}/5 px-3 py-2">
          <div class="flex items-center gap-2">
            <span class="text-[10px] font-bold uppercase text-${col}">${_esc(btype)}</span>
            <span class="text-[10px] text-Cmuted">window ceiling</span>
          </div>
          <div class="flex items-center gap-3">
            <span class="text-sm font-extrabold font-mono text-${col}">${hrs}h</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-${col}/15 border border-${col}/30 text-${col} font-semibold">Tier 2 · SOW</span>
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
          <table class="w-full text-[10px]">
            <thead><tr class="border-b border-Cborder/40 bg-Cbg/60">
              <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">Workflow</th>
              <th class="text-left py-1.5 px-2 text-Cmuted font-semibold">Type</th>
              <th class="text-right py-1.5 px-2 text-Cmuted font-semibold" title="Contracted SLA window from Tier 1 (XLSX) · Tier 2 (SOW) · Tier 3 (default)">SLA</th>
              <th class="text-right py-1.5 px-2 text-Cmuted font-semibold" title="Ctrl-M: worst-case per-run elapsed. XLSX: last known run from BatchSLA_info Current end time − Start Time">Runtime</th>
              <th class="text-right py-1.5 px-2 text-Cmuted font-semibold" title="(SLA_h − runtime_h) / SLA_h × 100 · negative = BREACH · blank = SLA_MISSING or RT_MISSING">Buffer %</th>
              <th class="text-center py-1.5 px-2 text-Cmuted font-semibold" title="OK · LONG_JOB · AT_RISK · BREACH · SLA_MISSING · RUNTIME_MISSING · FAILED">Status</th>
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
                  if (s.startsWith("batch_sla_xlsx") || s === "xlsx")
                    return `<span class="ml-1 text-[7px] font-bold text-Cteal bg-Cteal/10 px-0.5 rounded" title="Source: BatchSLA_info.xlsx workflow SLA">BATCH</span>`;
                  if (s === "sow_extracted")
                    return `<span class="ml-1 text-[7px] font-bold text-Cpurple bg-Cpurple/10 px-0.5 rounded" title="Source: SOW contract batch-type ceiling (no per-workflow SLA in XLSX)">SOW</span>`;
                  if (s === "sla_matrix" || s === "contract")
                    return `<span class="ml-1 text-[7px] font-bold text-Cgreen bg-Cgreen/10 px-0.5 rounded" title="Source: Uploaded customer SLA matrix file">CONTRACT</span>`;
                  if (s === "global_default" || s.startsWith("global") || s === "assumed")
                    return `<span class="ml-1 text-[7px] font-bold text-Camber bg-Camber/10 px-0.5 rounded" title="No contract SLA found — system default used. Upload BatchSLA_info.xlsx with SLA column or SOW to override.">DEFAULT</span>`;
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

  // Re-render the commitments panel and re-run the SLA matrix if batch data exists
  _renderSlaCommitmentsPanel();
  if (window.appData.batch) triggerSlaMatrix();

  const msg = document.getElementById("sla-manual-msg");
  if (msg) {
    msg.textContent = `✅ Applied: ${[
      !isNaN(daily)   ? `DAILY=${daily}h`   : "",
      !isNaN(weekly)  ? `WEEKLY=${weekly}h`  : "",
      !isNaN(monthly) ? `MONTHLY=${monthly}h` : "",
    ].filter(Boolean).join(" · ")}`;
    msg.classList.remove("hidden");
    setTimeout(() => msg.classList.add("hidden"), 4000);
  }
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

  // Use window compliance as headline when available (matches Executive Dashboard)
  const headlineComp = data.compliance_pct;
  const compColor = headlineComp >= 95 ? "text-Cgreen" :
                    headlineComp >= 80 ? "text-Camber" : "text-Cred";
  const compEl = document.getElementById("slak-compliance");
  if (compEl) {
    compEl.textContent = _n(headlineComp).toFixed(1) + "%";
    compEl.className = `text-2xl font-bold ${compColor}`;
    // Denominator + formula trace — strongest audit rule: KPI must expose source + formula
    const _eligible = (data.total_runs || 0) - (data.failed_runs || 0);
    const _passing  = (data.ok_runs || 0) + (data.long_job_runs || 0) + (data.at_risk_runs || 0);
    compEl.title = `${_passing}/${_eligible} eligible runs pass`
      + ` · formula: (OK+LONG_JOB+AT_RISK) ÷ eligible × 100`
      + (data.failed_runs ? ` · ${data.failed_runs} FAILED excluded from denominator` : ``)
      + (data.window_total_days != null
           ? ` · window: ${(data.window_total_days||0)-(data.window_breach_days||0)}/${data.window_total_days} windows OK`
           : ``);
  }
  // Show window context if available (X of Y sub-app windows breached)
  const compSubEl = document.getElementById("slak-compliance-sub");
  if (compSubEl && data.window_total_days != null && data.window_breach_days != null) {
    const pass = data.window_total_days - data.window_breach_days;
    compSubEl.textContent = `${pass}/${data.window_total_days} windows pass · Window`;
    compSubEl.className = `text-[10px] ${data.window_breach_days > 0 ? "text-Cred" : "text-Cmuted"}`;
  }

  // Explain the compliance vs breach distinction when they diverge
  const compNote = document.getElementById("slak-compliance-note");
  if (compNote) {
    const breachCount = data.breaching_runs || 0;
    const wbDays      = data.window_breach_days || 0;
    if (wbDays > 0 && breachCount === 0) {
      // Classic "window failed but no single job breached" — explain why
      compNote.classList.remove("hidden");
      compNote.textContent = `ℹ Window = total elapsed time (first job start → last job end). `
        + `${wbDays} sub-app window(s) where the batch collectively ran late, even though no individual job `
        + `exceeded its own SLA ceiling. Possible causes: late job start, queue delays, or too many `
        + `jobs running sequentially without overlap.`;
    } else if (wbDays === 0 && breachCount === 0) {
      compNote.classList.add("hidden");
    } else {
      compNote.classList.add("hidden");
    }
  }
  setText("slak-total",  String(data.total_runs));

  const brEl = document.getElementById("slak-breach");
  if (brEl) { brEl.textContent = String(data.breaching_runs); brEl.className = `text-2xl font-bold ${data.breaching_runs > 0 ? "text-Cred" : "text-Cgreen"}`; }
  const arEl = document.getElementById("slak-atrisk");
  if (arEl) { arEl.textContent = String(data.at_risk_runs); arEl.className = `text-2xl font-bold ${data.at_risk_runs > 0 ? "text-Camber" : "text-Cgreen"}`; }
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

  // Breach detail — show a CRUX, not 200 rows.
  const detailWrap = document.getElementById("sla-detail-wrap");
  const tbody      = document.getElementById("sla-breach-tbody");
  const countLabel = document.getElementById("sla-breach-count-label");
  if (detailWrap) {
    detailWrap.querySelectorAll("[data-breach-crux],[data-breach-toggle]").forEach((n) => n.remove());
  }
  if (data.breaches?.length) {
    _renderSlaBreachCrux(data, detailWrap, tbody, countLabel);
  } else {
    if (detailWrap) detailWrap.classList.add("hidden");
  }

  // Store for findings engine
  window.appData.slaMatrix = data;
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

  // ── SLA compliance donut + job buffer bars (graphical) ──
  _renderSlaCharts(data);

  // ── SLA Triage panel ──
  _renderSlaTriage(data);

  // Job summary
  //   Rule 3: if 100% compliance with no breaches and no at-risk runs, the
  //           per-job table is just noise — replace it with a single sentence.
  //   Rule 2: otherwise show top 10 by peak runtime, with a "view all" toggle.
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

  if (data.job_summary?.length) {
    // Aggregated format (no per-job breakdown) — show placeholder instead of table
    if (data.data_format === "aggregated") {
      if (jobWrap) jobWrap.classList.add("hidden");
      // Show informational placeholder
      const existingPlaceholder = document.getElementById("sla-aggregated-placeholder");
      if (!existingPlaceholder) {
        const phDiv = document.createElement("div");
        phDiv.id = "sla-aggregated-placeholder";
        phDiv.className = "rounded-xl border border-Camber/30 bg-Camber/5 px-4 py-3 text-[11px] text-Cwhite/90 flex items-start gap-2";
        phDiv.innerHTML = `
          <span class="text-Camber text-base mt-0.5">&#x26A0;</span>
          <div>
            <div class="font-semibold text-Camber mb-0.5">Job-level data not available</div>
            <div class="text-Cmuted">This Ctrl-M file contains only sub-application-level totals — individual job names are not present. SLA compliance is computed at workflow level only. Upload a file with per-job rows to enable job-level analysis.</div>
          </div>`;
        const jobWrapParent = jobWrap?.parentElement;
        if (jobWrapParent) jobWrapParent.appendChild(phDiv);
      }
    } else {
    if (jobWrap) jobWrap.classList.remove("hidden");
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
    } // end else (per_job)
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
      const risk   = buf < 5  ? "CRITICAL — immediate optimisation needed" :
                     buf < 10 ? "HIGH — any production data spike will breach" :
                     "MODERATE — monitor closely under load";
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
    window.appData.benchmark = data;
    refreshDataStatus();
    _renderBenchmark(data);
    toast("success", "Benchmark loaded", `${data.total_transactions} transactions compared`);
  } catch (err) {
    _handleFetchError(err);
  } finally {
    if (statusEl) statusEl.classList.add("hidden");
  }
}

function _renderBenchmark(data) {
  document.getElementById("bench-empty")?.classList.add("hidden");
  document.getElementById("bench-no-data-prompt")?.classList.add("hidden");
  document.getElementById("bench-loaded-chip")?.classList.remove("hidden");
  document.getElementById("bench-kpi-row")?.classList.remove("hidden");
  document.getElementById("bench-table-wrap")?.classList.remove("hidden");
  // Update loaded label
  const isBatchPerf = !!(data.batch_perf_summary);
  const cats = data.categories || [];
  const catLabel = isBatchPerf ? ` · batch runtime comparison` : (cats.length > 0 ? ` · ${cats.length} categories` : "");
  const lbl = document.getElementById("bench-loaded-label");
  if (lbl) lbl.textContent = `${data.filename || "Benchmark"} · ${data.total_transactions} ${isBatchPerf ? "jobs" : "transactions"}${catLabel}`;

  // Relabel comparison table headers for batch perf mode
  if (isBatchPerf) {
    const ths = document.querySelectorAll("#bench-tbody")?.closest("table")?.querySelectorAll("thead th") || [];
    const labels = ["Job", "Before (s)", "After (s)", "Delta %", "Δ sec", "Status"];
    ths.forEach((th, i) => { if (labels[i]) th.textContent = labels[i]; });
  }

  // KPI strip — relabel for batch perf mode
  const totalLbl  = document.querySelector("#bench-kpi-row [id='bk-total']")?.closest(".rounded-xl")?.querySelector(".text-\\[10px\\]");
  const slaLbl    = document.querySelector("#bench-kpi-row [id='bk-sla-breach']")?.closest(".rounded-xl")?.querySelector(".text-\\[10px\\]");
  const avgLbl    = document.querySelector("#bench-kpi-row [id='bk-avg-delta']")?.closest(".rounded-xl")?.querySelector(".text-\\[10px\\]");
  if (isBatchPerf) {
    if (totalLbl)  totalLbl.textContent  = "Total Jobs";
    if (slaLbl)    slaLbl.textContent    = "New Jobs";
    if (avgLbl)    avgLbl.textContent    = "Net Time Saved";
  }

  setText("bk-total", String(data.total_transactions));
  const degEl = document.getElementById("bk-degraded");
  if (degEl) { degEl.textContent = String(data.degraded); degEl.className = `text-2xl font-bold ${data.degraded > 0 ? "text-Cred" : "text-Cgreen"}`; }
  const slaEl = document.getElementById("bk-sla-breach");
  if (slaEl) {
    if (isBatchPerf) {
      const bp = data.batch_perf_summary;
      slaEl.textContent = String(bp.new_only || 0);
      slaEl.className = "text-2xl font-bold text-Cblue";
    } else {
      slaEl.textContent = String(data.sla_breaches);
      slaEl.className = `text-2xl font-bold ${data.sla_breaches > 0 ? "text-Camber" : "text-Cgreen"}`;
    }
  }
  const avgEl = document.getElementById("bk-avg-delta");
  if (avgEl) {
    if (isBatchPerf) {
      const net = _n(data.batch_perf_summary.net_delta_secs);
      const netMin = Math.abs(net / 60).toFixed(1);
      avgEl.textContent = (net >= 0 ? "+" : "-") + netMin + " min";
      avgEl.className = `text-2xl font-bold ${net >= 0 ? "text-Cgreen" : "text-Cred"}`;
    } else {
      const v = _n(data.avg_delta_pct);
      avgEl.textContent = (v > 0 ? "+" : "") + v.toFixed(1) + "%";
      avgEl.className = `text-2xl font-bold ${v > _n(data.threshold_pct) ? "text-Cred" : v > 0 ? "text-Camber" : "text-Cgreen"}`;
    }
  }

  const bannerEl = document.getElementById("bench-summary-banner");
  const summaryEl= document.getElementById("bench-summary-text");
  if (bannerEl && summaryEl && data.summary) {
    bannerEl.classList.remove("hidden");
    summaryEl.textContent = data.summary;
    bannerEl.className = `rounded-xl border px-5 py-3 ${data.degraded > 0 ? "border-Camber/40 bg-Camber/10" : "border-Cgreen/40 bg-Cgreen/10"}`;
  }

  // ── Category summary cards ──────────────────────────────────
  _renderBenchCategories(data);

  // ── Fill Rate panel ─────────────────────────────────────────
  _renderBenchFillRate(data);

  // ── Observations panel ──────────────────────────────────────
  _renderBenchObservations(data);

  // ── Main comparison table (grouped by category) ─────────────
  const tbody = document.getElementById("bench-tbody");
  if (tbody) {
    const bps = data.batch_perf_summary;
    let html = "";

    if (bps) {
      // Batch perf mode: render top regressions and improvements only
      const reg  = bps.top_regressions  || [];
      const impr = bps.top_improvements || [];
      const thresh = _n(data.threshold_pct);

      const renderBpRow = (e) => {
        const dSecs = _n(e.delta_secs);   // positive = improvement
        const dPct  = _n(e.delta_pct);    // positive = regression
        const dColor = dPct > thresh ? "text-Cred font-bold" : dPct > 0 ? "text-Camber" : "text-Cgreen";
        const stBg  = dPct > thresh ? "bg-Cred/20 text-Cred" : dPct < -5 ? "bg-Cgreen/20 text-Cgreen" : "bg-Cmuted/20 text-Cmuted";
        const status = dPct > thresh ? "REGRESSED" : dPct < -5 ? "IMPROVED" : "STABLE";
        return `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
          <td class="py-2 pr-4 text-Cwhite font-semibold text-xs">${_esc(e.job)}</td>
          <td class="py-2 pr-4 text-right text-Cmuted font-mono text-xs">${_n(e.old_secs).toFixed(1)}s</td>
          <td class="py-2 pr-4 text-right font-mono text-xs ${dPct > 0 ? "text-Camber" : "text-Cgreen"}">${_n(e.new_secs).toFixed(1)}s</td>
          <td class="py-2 pr-4 text-right text-xs ${dColor}">${dPct > 0 ? "+" : ""}${dPct.toFixed(1)}%</td>
          <td class="py-2 pr-4 text-right font-mono text-xs ${dSecs >= 0 ? "text-Cgreen" : "text-Cred"}">${dSecs >= 0 ? "+" : ""}${dSecs.toFixed(0)}s</td>
          <td class="py-2 text-center"><span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase ${stBg}">${status}</span></td>
        </tr>`;
      };

      if (reg.length) {
        html += `<tr class="bg-Cbg/80"><td colspan="6" class="py-2.5 px-2">
          <span class="text-xs font-bold text-Cred uppercase tracking-wider">Top Regressions</span>
          <span class="ml-2 text-[10px] text-Cmuted">${bps.regressions} total · showing worst ${reg.length}</span>
        </td></tr>`;
        reg.forEach(e => { html += renderBpRow(e); });
      }
      if (impr.length) {
        html += `<tr class="bg-Cbg/80"><td colspan="6" class="py-2.5 px-2">
          <span class="text-xs font-bold text-Cgreen uppercase tracking-wider">Top Improvements</span>
          <span class="ml-2 text-[10px] text-Cmuted">${bps.improvements} total · showing best ${impr.length}</span>
        </td></tr>`;
        impr.forEach(e => { html += renderBpRow(e); });
      }
      if (!reg.length && !impr.length) {
        html = `<tr><td colspan="6" class="py-8 text-center text-Cmuted text-xs">All ${bps.total_jobs} jobs within tolerance — no regressions or significant improvements.</td></tr>`;
      }
      tbody.innerHTML = html;
    } else {
    // Normal mode: group by category
    const rows = data.rows || [];
    const catOrder = [];
    const catMap = {};
    rows.forEach((r) => {
      const c = r.category || "General";
      if (!catMap[c]) { catMap[c] = []; catOrder.push(c); }
      catMap[c].push(r);
    });

    catOrder.forEach((cat) => {
      const catRows = catMap[cat];
      const passed = catRows.filter(r => r.status === "GREEN" || (r.pass_fail && r.pass_fail.toLowerCase() === "pass")).length;
      html += `<tr class="bg-Cbg/80 sticky top-0 z-10">
        <td colspan="7" class="py-2.5 px-2">
          <span class="text-xs font-bold text-Cpurple uppercase tracking-wider">${_esc(cat)}</span>
          <span class="ml-2 text-[10px] text-Cmuted">${catRows.length} items · ${passed} passed</span>
        </td></tr>`;
      catRows.forEach((r) => {
        const stBg = r.status === "RED"   ? "bg-Cred/20 text-Cred"     :
                     r.status === "AMBER" ? "bg-Camber/20 text-Camber" :
                     r.status === "GREEN" ? "bg-Cgreen/20 text-Cgreen" : "bg-Cmuted/20 text-Cmuted";
        const dCol = r.delta_pct > data.threshold_pct ? "text-Cred font-bold" :
                     r.delta_pct > 0                  ? "text-Camber" :
                     r.delta_pct < -5                 ? "text-Cgreen" : "text-Cmuted";
        const pfBadge = r.pass_fail
          ? `<span class="ml-1 px-1.5 py-0.5 rounded text-[9px] font-bold ${r.pass_fail.toLowerCase()==='pass' ? 'bg-Cgreen/20 text-Cgreen' : 'bg-Cred/20 text-Cred'}">${_esc(r.pass_fail)}</span>`
          : "";
        html += `<tr class="border-b border-Cborder/40 hover:bg-Ccard/40">
          <td class="py-2 pr-4 text-Cwhite font-semibold text-xs">${_esc(r.transaction)}${pfBadge}</td>
          <td class="py-2 pr-4 text-right text-Cmuted font-mono text-xs">${_n(r.baseline_sec).toFixed(1)}</td>
          <td class="py-2 pr-4 text-right font-mono text-xs ${_n(r.current_sec) > _n(r.baseline_sec) ? "text-Camber" : "text-Cgreen"}">${_n(r.current_sec).toFixed(1)}</td>
          <td class="py-2 pr-4 text-right text-xs ${dCol}">${_n(r.delta_pct) > 0 ? "+" : ""}${_n(r.delta_pct).toFixed(1)}%</td>
          <td class="py-2 pr-4 text-right text-Cmuted text-xs">${r.sla_sec != null ? _n(r.sla_sec).toFixed(1) : "—"}</td>
          <td class="py-2 text-center"><span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase ${stBg}">${r.status}</span></td>
        </tr>`;
      });    });
    tbody.innerHTML = html;
    }   // end else (normal mode)
  }     // end if (tbody)
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
  wrap.classList.remove("hidden");

  const netSecs = _n(bps.net_delta_secs);
  const netMin  = Math.abs(netSecs / 60).toFixed(1);
  const netDir  = netSecs >= 0 ? "saved" : "added";
  const netCol  = netSecs >= 0 ? "text-Cgreen" : "text-Cred";

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
      <td class="py-1.5 pr-3 text-Cwhite text-xs font-semibold">${_esc(e.job)}</td>
      <td class="py-1.5 pr-3 text-right text-Cmuted font-mono text-xs">${_n(e.old_secs).toFixed(0)}s</td>
      <td class="py-1.5 pr-3 text-right font-mono text-xs ${isReg ? "text-Camber" : "text-Cgreen"}">${_n(e.new_secs).toFixed(0)}s</td>
      <td class="py-1.5 text-right font-mono text-xs font-bold ${pctCol}">${pct > 0 ? "+" : ""}${pct.toFixed(0)}%</td>
      <td class="py-1.5 text-right font-mono text-xs ${sav >= 0 ? "text-Cgreen" : "text-Cred"}">${sav >= 0 ? "+" : ""}${sav.toFixed(0)}s</td>
    </tr>`;
  };

  const reg  = bps.top_regressions  || [];
  const impr = bps.top_improvements || [];

  let html = `<h3 class="text-sm font-bold text-Cwhite mb-3">Batch Runtime Comparison</h3>
    <div class="flex flex-wrap gap-3 mb-5">
      ${statCard("Total Jobs",    bps.total_jobs,    "text-Cwhite",  `${bps.comparable} with baseline`)}
      ${statCard("Regressions",   bps.regressions,   bps.regressions > 0 ? "text-Cred" : "text-Cgreen", "worse than before")}
      ${statCard("Improvements",  bps.improvements,  bps.improvements > 0 ? "text-Cgreen" : "text-Cmuted", "faster than before")}
      ${statCard("No Change",     bps.no_change,     "text-Cmuted",  "within ±threshold")}
      ${statCard("Net Runtime",   `${netSecs >= 0 ? "−" : "+"}${netMin} min`, netCol, `${netDir} per run`)}
    </div>`;

  const colHead = `<thead><tr class="border-b border-Cborder">
    <th class="text-left py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">Job</th>
    <th class="text-right py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">Before (s)</th>
    <th class="text-right py-1.5 pr-3 text-Cmuted text-[10px] font-semibold">After (s)</th>
    <th class="text-right py-1.5 text-Cmuted text-[10px] font-semibold">Δ %</th>
    <th class="text-right py-1.5 text-Cmuted text-[10px] font-semibold">Δ sec</th>
  </tr></thead>`;

  html += `<div class="grid grid-cols-1 xl:grid-cols-2 gap-4">`;

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

  // SLA intake zone is a <div> (rendered dynamically), keep JS click handler
  // but guard against double-trigger from label-like bubbling.
  dz.addEventListener("click", (e) => {
    if (e.target === input || e.target.closest("input")) return;
    input.click();
  });
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

// ── New Engagement — wipe all server-side session data ────────────────────
async function clearSessionData() {
  if (!confirm(
    "Hard Reset — clear everything?\n\n" +
    "This wipes the current customer's batch data, resource data, SOW, " +
    "findings, and all session state on both server and browser.\n\n" +
    "The page will reload automatically."
  )) return;

  try {
    const res = await fetch("/api/clear-session", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}) });
    if (!res.ok) throw new Error(await res.text());

    // Delete persisted SOW baseline from backend config store
    await fetch("/api/sow/baseline", { method: "DELETE" }).catch(() => {});

    // Full page reload — this is the only reliable way to wipe ALL state:
    // charts, panels, exclusion sets, file inputs, app globals, DOM nodes.
    // Trying to individually reset every element always misses something.
    window.location.reload();
  } catch (err) {
    toast("error", "Hard reset failed", String(err).slice(0, 120));
  }
}
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
    const fromFile = trace.type === "sla_matrix";
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


/** Zone D — Benchmark file upload on the Intake page */
function initBenchIntakeUploader() {
  const dz    = document.getElementById("bench-intake-drop-zone");
  const input = document.getElementById("bench-intake-file-input");
  if (!dz || !input) return;

  // Click-to-browse handled natively by the <label for="bench-intake-file-input"> in HTML.
  input.addEventListener("change", (e) => {
    const f = e.target.files?.[0];
    if (f) _uploadBenchIntakeFile(f);
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
    if (f) _uploadBenchIntakeFile(f);
  });
}

async function _uploadBenchIntakeFile(file) {
  const dot = document.getElementById("bench-intake-dot");
  _renderIntakeProgress("bench-intake-status", {
    filename: file.name,
    message:  formatBytes(file.size),
    percent:  0,
    color:    "purple",
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
      _renderIntakeProgress("bench-intake-status", {
        filename: file.name,
        message:  finished ? `${formatBytes(file.size)} \u2014 comparing PROD vs TEST\u2026`
                           : `${formatBytes(loaded)} / ${formatBytes(total)}`,
        percent:  finished ? null : pct,
        color:    "purple",
        phase:    finished ? "analysing" : "uploading",
      });
    });
    if (!ok) {
      toast("error", "Benchmark upload failed", ((body?.detail) || `HTTP ${status}`).slice(0, 200));
      return;
    }
    const data = body;
    window.appData.benchmark = data;

    // Update dot
    if (dot) { dot.className = "w-2 h-2 rounded-full bg-Cpurple animate-pulse shrink-0"; }

    // Show result card
    _renderBenchIntakeCard(data, file.name);

    // Auto-populate the Benchmark tab
    _renderBenchmark(data);
    refreshDataStatus();
    // Re-run findings with benchmark data included
    triggerGenerateFindings().catch(() => {});

    // Show intake status row + next prompt
    document.getElementById("intake-status-row")?.classList.remove("hidden");
    document.getElementById("upload-next-prompt")?.classList.remove("hidden");

    toast("success", "Benchmark loaded",
      `${data.total_transactions} transactions · ${data.degraded} regression(s)`);
  } catch (err) {
    _handleFetchError(err);
  } finally {
    document.getElementById("bench-intake-status")?.classList.add("hidden");
  }
}

// ═══════════════════════════════════════════════════════════════
//  ZONE E — Workflow SLA Info (BatchSLA_info.xlsx) — Tier-1 SLA source
// ═══════════════════════════════════════════════════════════════

/** Zone E — BatchSLA_info.xlsx uploader (workflow-level SLA contracts). */
function initBatchSlaInfoUploader() {
  const dz    = document.getElementById("batch-sla-info-drop-zone");
  const input = document.getElementById("batch-sla-info-file-input");
  if (!dz || !input) return;

  // Click-to-browse handled natively by the <label for="batch-sla-info-file-input"> in HTML.
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

    // Re-run SLA Matrix with the new XLSX SLAs applied (uses full job_runs_df from server)
    if (window.appData?.batch) {
      triggerSlaMatrix().catch(() => {});
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

  // Click-to-browse handled natively by the <label for="sow-intake-file-input"> in HTML.
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

    // Re-run SLA Matrix so SOW Tier 2 ceilings are applied to compliance numbers
    if (window.appData?.batch) {
      triggerSlaMatrix().catch(() => {});
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
  { key: "daily_dfu", baseId: "sow-dfu", label: "Daily DFU" },
  { key: "daily_sku", baseId: "sow-sku", label: "Daily SKU Count" },
];

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
      if (skuBaseline && !skuBaseline.value) skuBaseline.value = Math.round(maxVol * 0.1); // ~10% typical SKU ratio
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

// ── Bind manual inputs → live appData + SOW Contract panel refresh ────────
let _sowManualBound = false;
function _bindSowManualInputs() {
  if (_sowManualBound) return;
  _sowManualBound = true;

  const bind = (id, key) => {
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
      _renderSowVolumeComparison();
      // When both baseline fields are cleared, reset sowCompare so narrative shows no stale SOW data
      const dfuCleared = !(parseFloat(document.getElementById("sow-dfu-baseline")?.value) > 0);
      const skuCleared = !(parseFloat(document.getElementById("sow-sku-baseline")?.value) > 0);
      if (dfuCleared && skuCleared) window.appData.sowCompare = null;
      // Re-trigger PE Narrative so Data Volume section picks up the new DFU/SKU values
      triggerPeNarrative().catch(() => {});
    });
  };
  bind("sow-dfu-baseline", "manual_dfu_baseline");
  bind("sow-dfu-actual",   "manual_dfu_actual");
  bind("sow-sku-baseline", "manual_sku_baseline");
  bind("sow-sku-actual",   "manual_sku_actual");
}

// ── SOW Volume Comparison: visual red/green % achievement bars ──────────
function _renderSowVolumeComparison() {
  const panel = document.getElementById("sow-volume-comparison");
  if (!panel) return;

  const sc  = window.appData?.sowContract || {};
  const metrics = [
    {
      label:    "Daily DFU",
      baseline: parseFloat(document.getElementById("sow-dfu-baseline")?.value) || sc.manual_dfu_baseline || 0,
      actual:   parseFloat(document.getElementById("sow-dfu-actual")?.value)   || sc.manual_dfu_actual   || 0,
      unit:     "items",
    },
    {
      label:    "Daily SKU",
      baseline: parseFloat(document.getElementById("sow-sku-baseline")?.value) || sc.manual_sku_baseline || 0,
      actual:   parseFloat(document.getElementById("sow-sku-actual")?.value)   || sc.manual_sku_actual   || 0,
      unit:     "SKUs",
    },
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
      _SOW_FIELDS.forEach(({ key, baseId }) => {
        if (data[key] != null) {
          const el = document.getElementById(`${baseId}-baseline`);
          if (el) el.value = data[key];   // always restore, not just when empty
        }
      });
    }
    _autoFillSowActuals();
  } catch (_) {}
}

function _autoFillSowActuals() {
  const servers = window.appData.servers || [];
  if (servers.length > 0) {
    const avgCpu = servers.reduce((s, v) => s + (parseFloat(v.cpu_used || v.cpu_pct || 0)), 0) / servers.length;
    const avgMem = servers.reduce((s, v) => s + (parseFloat(v.mem_used || v.mem_pct || 0)), 0) / servers.length;
    const cpuEl = document.getElementById("sow-cpu-actual");
    const memEl = document.getElementById("sow-mem-actual");
    if (cpuEl && !cpuEl.value) cpuEl.value = avgCpu.toFixed(1);
    if (memEl && !memEl.value) memEl.value = avgMem.toFixed(1);
  }
  const batch = window.appData.batch;
  if (batch) {
    const jobsEl = document.getElementById("sow-batchjobs-actual");
    if (jobsEl && !jobsEl.value && batch.kpis?.total_jobs) {
      jobsEl.value = batch.kpis.total_jobs;
    }
  }
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
    refreshDataStatus();
    _renderSowComparison(data);
    triggerGenerateFindings().catch(() => {});
    triggerPeNarrative().catch(() => {});   // keep Data Volume in sync
    if (msgEl) {
      msgEl.textContent = "✅ Saved and compared";
      msgEl.className   = "text-[11px] text-Cgreen";
      msgEl.classList.remove("hidden");
      setTimeout(() => msgEl.classList.add("hidden"), 3000);
    }
  } catch (err) {
    toast("error", "Error", String(err?.message || err));
  }
}

function _renderSowComparison(data) {
  // Ensure grid is visible
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
      const color  = m.status === "OPTIMAL"  ? "#22d3ee" :
                     m.status === "MODERATE" ? "#f59e0b" : "#f43f5e";
      const statusBg = m.status === "OPTIMAL"  ? "bg-Ccyan/20 text-Ccyan" :
                       m.status === "MODERATE" ? "bg-Camber/20 text-Camber" :
                       "bg-Cred/20 text-Cred";
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
          <div class="absolute inset-y-0 left-0 bg-Cred/20" style="width:${(70/150*100).toFixed(1)}%"></div>
          <div class="absolute inset-y-0 bg-Camber/20" style="left:${(70/150*100).toFixed(1)}%;width:${((90-70)/150*100).toFixed(1)}%"></div>
          <div class="absolute inset-y-0 bg-Cgreen/20" style="left:${(90/150*100).toFixed(1)}%;width:${((110-90)/150*100).toFixed(1)}%"></div>
          <div class="absolute inset-y-0 bg-Cred/15" style="left:${(110/150*100).toFixed(1)}%;right:0"></div>
          <div class="absolute inset-y-0 w-px bg-white/30" style="left:${(100/150*100).toFixed(1)}%"></div>
          <div class="absolute inset-y-0 left-0 rounded-lg transition-all duration-700" style="width:${barPct}%;background:${color};opacity:0.8"></div>
        </div>
        <div class="flex justify-between text-[9px] text-Cmuted font-mono">
          <span>0</span><span>70%</span><span>90%</span><span>100%</span><span>110%</span><span>150%+</span>
        </div>
      </div>`;
    }).join("");
  }

  // Table
  const tbody = document.getElementById("sow-table-tbody");
  if (tbody && data.metrics?.length) {
    tbody.innerHTML = data.metrics.map((m) => {
      const stBg = m.status === "OPTIMAL"  ? "bg-Ccyan/15 text-Ccyan" :
                   m.status === "MODERATE" ? "bg-Camber/20 text-Camber" :
                   "bg-Cred/20 text-Cred";
      const pctColor = m.pct >= 90 && m.pct <= 110 ? "text-Ccyan font-bold" :
                       m.pct >= 70                  ? "text-Camber font-semibold" : "text-Cred font-bold";
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
