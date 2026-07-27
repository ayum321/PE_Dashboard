// Minimal headless harness to execute the REAL app.js in a Node vm context
// and call _renderVmDeepDiveCard() directly with synthetic data, to catch any
// runtime exception that would silently kill the "Unified Time-Series" chart
// in a real browser (no console visible to the user).
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const byId = new Map();

function makeEl(tag) {
  let _id = "";
  const el = {
    tagName: tag,
    className: "",
    style: {},
    dataset: {},
    children: [],
    _html: "",
    classList: {
      _set: new Set(),
      add(...c) { c.forEach(x => this._set.add(x)); },
      remove(...c) { c.forEach(x => this._set.delete(x)); },
      toggle(c, force) {
        if (force === undefined) { if (this._set.has(c)) { this._set.delete(c); return false; } this._set.add(c); return true; }
        if (force) this._set.add(c); else this._set.delete(c);
        return force;
      },
      contains(c) { return this._set.has(c); },
    },
    appendChild(child) { this.children.push(child); return child; },
    insertBefore(child) { this.children.push(child); return child; },
    remove() { if (_id) byId.delete(_id); },
    addEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    querySelector(sel) {
      if (sel === "canvas") return makeCanvas();
      return makeEl("div");
    },
    querySelectorAll() { return []; },
    getContext() { return {}; },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    closest() { return null; },
    get id() { return _id; },
    set id(v) { _id = v; if (v) byId.set(v, el); },
  };
  return el;
}
function makeCanvas() {
  const c = makeEl("canvas");
  c.getContext = () => ({});
  return c;
}

const sandbox = {};
sandbox.window = sandbox;
sandbox.console = console;
sandbox.document = {
  createElement: (tag) => makeEl(tag),
  getElementById: (id) => byId.get(id) || null,
  addEventListener: () => {},
  querySelector: () => null,
  querySelectorAll: () => [],
  head: makeEl("head"),
  body: makeEl("body"),
  documentElement: makeEl("html"),
};
sandbox.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
sandbox.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
sandbox.navigator = { userAgent: "node" };
sandbox.fetch = () => Promise.resolve({ ok: true, json: async () => ({}) });
sandbox.requestAnimationFrame = (fn) => setTimeout(fn, 0);
sandbox.URL = URL;
sandbox.URL.createObjectURL = () => "blob://x";
sandbox.URL.revokeObjectURL = () => {};
sandbox.Blob = function () {};
sandbox.Intl = Intl;
sandbox.performance = { now: () => Date.now() };
sandbox.location = { href: "http://localhost/", search: "", pathname: "/", hash: "" };
sandbox.history = { replaceState: () => {}, pushState: () => {} };

// Chart.js stub — deep enough that _chartJsDefaults() and `new Chart()` don't throw.
function ChartStub(ctx, cfg) {
  this.ctx = ctx; this.config = cfg;
  this.data = cfg.data;
  this.resize = () => {};
  this.destroy = () => {};
  this.toBase64Image = () => "";
  this.resetZoom = () => {};
  console.log("  [ChartStub] datasets:", (cfg.data.datasets || []).map(d => `${d.label}(${d.yAxisID})`).join(", "));
  console.log("  [ChartStub] scales:", Object.keys(cfg.options?.scales || {}));
}
ChartStub.defaults = {
  animation: {}, transitions: { active: { animation: {} }, resize: { animation: {} } },
  elements: { point: {} }, plugins: { tooltip: {} },
};
ChartStub.register = () => {};
sandbox.Chart = ChartStub;

sandbox.Plotly = {
  react: () => {}, relayout: () => {}, restyle: () => {}, purge: () => {},
  downloadImage: () => {}, newPlot: () => {},
};

vm.createContext(sandbox);

const code = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
try {
  vm.runInContext(code, sandbox, { filename: "app.js" });
} catch (e) {
  console.error("LOAD-TIME ERROR (script failed to even load):");
  console.error(e.stack || e);
  process.exit(1);
}
console.log("app.js loaded into sandbox OK");
console.log("typeof _renderVmDeepDiveCard =", typeof sandbox._renderVmDeepDiveCard);

// ── Synthetic single-VM payload, matching the real backend contract ──
const t0 = Date.parse("2026-03-02T00:00:00Z");
function series(fn, n = 48) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({ t: new Date(t0 + i * 30 * 60000).toISOString(), v: fn(i) });
  }
  return out;
}
const vmData = {
  series: {
    "Percentage CPU": series(i => 30 + 20 * Math.sin(i)),
    "Available Memory Percentage": series(i => 60 + 10 * Math.sin(i)),
    "OS Disk Bandwidth Consumed Percentage": series(i => 20 + 5 * Math.sin(i)),
    "Data Disk Bandwidth Consumed Percentage": series(i => 15 + 5 * Math.sin(i)),
  },
  series_max: {
    "Percentage CPU": series(i => 50 + 30 * Math.sin(i)),
    "Available Memory Percentage": series(i => 70 + 10 * Math.sin(i)),
    "OS Disk Bandwidth Consumed Percentage": series(i => 30 + 5 * Math.sin(i)),
    "Data Disk Bandwidth Consumed Percentage": series(i => 25 + 5 * Math.sin(i)),
  },
  spikes: {
    "Percentage CPU": [
      { start: new Date(t0 + 3600000).toISOString(), end: new Date(t0 + 7200000).toISOString(),
        peak: 92.3, peak_time: new Date(t0 + 5400000).toISOString(), duration_min: 60,
        severity: "critical", z_score: 3.4, mean: 30.1, std: 12.0, detection: "z_score" },
    ],
    "Available Memory Percentage": [
      { start: new Date(t0 + 10800000).toISOString(), end: new Date(t0 + 12600000).toISOString(),
        peak: 15.2, peak_time: new Date(t0 + 11700000).toISOString(), duration_min: 30,
        severity: "warning", z_score: 2.7, mean: 60.0, std: 8.0, detection: "z_score" },
    ],
  },
  stats: {
    "Percentage CPU": { mean: 30, max: 92.3, min: 10, p95: 55, max_anomalous: false },
    "Available Memory Percentage": { mean: 60, max: 75, min: 15.2, p5: 20, min_anomalous: false },
  },
  waveforms: {
    "Percentage CPU": {
      shape: "sawtooth", secondary_shape: null, label: "Cyclic Load", icon: "⚡",
      meaning: "Regular rise-and-fall cycles.", action: "Map cycle vs schedule.",
      risk: "medium", confidence: 0.7, confidence_label: "inferred",
      recurrence_days: 3, duration_above_threshold_hrs: 2,
      details: { peak_used_pct: 92.3, mean_used_pct: 30, headroom_pct: 7.7, peak_count: 4, cv: 0.3 },
    },
    "Available Memory Percentage": {
      shape: "plateau", secondary_shape: null, label: "Sustained Load", icon: "▬",
      meaning: "Consistently elevated.", action: "Sizing review.",
      risk: "high", confidence: 0.8, confidence_label: "observed",
      recurrence_days: 5, duration_above_threshold_hrs: 4,
      details: { peak_used_pct: 84.8, mean_used_pct: 40, headroom_pct: 15.2, peak_count: 2, cv: 0.1 },
    },
  },
  role: "APP",
  baseline_confidence: { tier: "degraded", pulls: 2, min_pulls: 3 },
};

const metricConfig = [
  { key: "Percentage CPU", label: "CPU %", color: "#3b82f6", warn: 80, core: true },
  { key: "Available Memory Percentage", label: "Available Mem %", color: "#06b6d4", warn: 20, core: true },
  { key: "OS Disk Bandwidth Consumed Percentage", label: "OS Disk BW %", color: "#f59e0b", warn: 80, core: true },
  { key: "Data Disk Bandwidth Consumed Percentage", label: "Data Disk BW %", color: "#a855f7", warn: 80, core: true },
  { key: "Available Memory Bytes", label: "Available Memory", color: "#67e8f9", unit: "bytes", chartOnly: true },
  { key: "Disk Read Bytes", label: "Disk Read", color: "#60a5fa", unit: "bytes", chartOnly: true },
  { key: "Disk Write Bytes", label: "Disk Write", color: "#c084fc", unit: "bytes", chartOnly: true },
  { key: "Disk Read Operations/Sec", label: "Disk Read Ops/s", color: "#93c5fd", unit: "ops/s", chartOnly: true },
  { key: "Disk Write Operations/Sec", label: "Disk Write Ops/s", color: "#d8b4fe", unit: "ops/s", chartOnly: true },
  { key: "Network In Total", label: "Network In", color: "#22c55e", unit: "bytes", chartOnly: true },
  { key: "Network Out Total", label: "Network Out", color: "#eab308", unit: "bytes", chartOnly: true },
  { key: "VmAvailabilityMetric", label: "Availability", color: "#10b981", unit: "avail", chartOnly: true },
];
vmData.series["Available Memory Bytes"] = series(i => 4e9 + 1e8 * Math.sin(i));
vmData.series["Disk Read Bytes"] = series(i => 5e6 + 1e6 * Math.sin(i));
vmData.series["Disk Write Bytes"] = series(i => 3e6 + 1e6 * Math.sin(i));
vmData.series["Disk Read Operations/Sec"] = series(i => 40 + 10 * Math.sin(i));
vmData.series["Disk Write Operations/Sec"] = series(i => 20 + 5 * Math.sin(i));
vmData.series["Network In Total"] = series(i => 2e6 + 5e5 * Math.sin(i));
vmData.series["Network Out Total"] = series(i => 1e6 + 3e5 * Math.sin(i));
vmData.series["VmAvailabilityMetric"] = series(i => (i === 10 ? 0 : 1));
vmData.stats["Disk Read Operations/Sec"] = { mean: 40, max: 50, min: 30, chart_only: true };
vmData.stats["Disk Write Operations/Sec"] = { mean: 20, max: 25, min: 15, chart_only: true };

sandbox._ddShowExtendedMetrics = true;
sandbox._ddShowMaxOverlay = true;

const container = makeEl("div");
try {
  sandbox._renderVmDeepDiveCard("testvm01", vmData, metricConfig, container, true);
  console.log("_renderVmDeepDiveCard returned without throwing.");
  console.log("container children appended:", container.children.length);
  const card = container.children[0];
  console.log("card children count:", card ? card.children.length : "(no card)");
  if (card) {
    card.children.forEach((c, i) => {
      const htmlSnippet = (c._html || "").slice(0, 80).replace(/\s+/g, " ");
      console.log(`  child[${i}] tag=${c.tagName} className="${c.className}" html="${htmlSnippet}"`);
    });
    const hasUnifiedChart = card.children.some(c => (c._html || "").includes("Unified Time-Series"));
    console.log("Unified Time-Series chart wrap present:", hasUnifiedChart);
  }
} catch (e) {
  console.error("RUNTIME ERROR inside _renderVmDeepDiveCard:");
  console.error(e.stack || e);
  process.exit(2);
}

// ── Also test the COMPACT grid card + its click-to-expand wiring, since the
// Unified Time-Series chart only renders inside the detail view opened by
// clicking a grid card. If the compact card itself throws, its click handler
// never attaches and the chart would never appear no matter what.
console.log("\n--- Testing _renderVmServerCard (compact grid card) ---");
const gridContainer = makeEl("div");
try {
  sandbox._renderVmServerCard("testvm01", vmData, metricConfig, gridContainer);
  console.log("_renderVmServerCard returned without throwing.");
  console.log("grid container children:", gridContainer.children.length);
} catch (e) {
  console.error("RUNTIME ERROR inside _renderVmServerCard:");
  console.error(e.stack || e);
  process.exit(3);
}

// ── THE REPORTED BUG: an all-clean fleet (zero VMs with any detected spike).
// Before the fix, #dd-server-grid / #deepdive-detail-area were only created
// inside `if (criticalVms.length)`, so with zero critical VMs there was no
// element in the whole DOM that could ever open a detail view — meaning the
// Unified Time-Series chart was structurally unreachable, not just hidden.
console.log("\n--- Testing _renderDeepDiveCharts with an ALL-CLEAN fleet ---");
const chartsDivEl = makeEl("div");
chartsDivEl.id = "deepdive-charts"; // registers in byId via the id setter
const cleanVmData = {
  series: vmData.series,
  series_max: vmData.series_max,
  spikes: {},          // <-- zero spikes on every metric = "clean" VM
  stats: vmData.stats,
  waveforms: {},
  role: "APP",
};
try {
  sandbox._renderDeepDiveCharts({ healthyvm01: cleanVmData, healthyvm02: cleanVmData }, {});
  console.log("_renderDeepDiveCharts returned without throwing.");
  const detailArea = byId.get("deepdive-detail-area");
  console.log("#deepdive-detail-area exists after all-clean render:", !!detailArea);
  const cleanCardHtml = chartsDivEl.children.map(c => c._html || "").join("\n");
  console.log("clean rows carry data-vm-name (clickable):", /data-vm-name="healthyvm0[12]"/.test(cleanCardHtml));
  console.log("clean-section toggles present (dd-max-overlay-toggle):", cleanCardHtml.includes("dd-max-overlay-toggle"));
  if (!detailArea) {
    console.error("BUG REPRODUCED: no #deepdive-detail-area in the DOM for an all-clean fleet — the Unified Time-Series chart has nowhere to render and no click path reaches it.");
    process.exit(4);
  }
} catch (e) {
  console.error("RUNTIME ERROR inside _renderDeepDiveCharts (all-clean fleet):");
  console.error(e.stack || e);
  process.exit(5);
}
console.log("\nALL DEBUG CHECKS PASSED");
