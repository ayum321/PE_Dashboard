"use strict";

// Direct DOM-contract runner for the Resource Review trend block.  It evaluates
// the real functions from static/app.js against a tiny deterministic DOM and a
// 17-day measured Azure payload; no browser, network, or fabricated production
// data is involved.
const fs = require("fs");
const vm = require("vm");

const app = fs.readFileSync("static/app.js", "utf8");
const start = app.indexOf("// ── Multi-day resource trend + Day × Server heatmap");
const end = app.indexOf("// ── Horizontal grouped bar", start);
if (start < 0 || end < 0) throw new Error("Resource trend block not found");

const ids = [
  "resource-trend-empty", "resource-trend-content", "resource-trend-status",
  "resource-trend-source", "resource-trend-server", "resource-trend-metric",
  "resource-trend-chart", "resource-trend-attribution",
  "resource-day-server-heatmap", "resource-heatmap-subtext",
];
const elements = Object.fromEntries(ids.map(id => [id, {
  id, innerHTML: "", textContent: "", value: "", disabled: false,
  classList: { add() {}, remove() {}, toggle() {} },
}]));

const THEME = {
  blue: "#3b82f6", cyan: "#06b6d4", purple: "#a855f7", amber: "#f59e0b",
  red: "#ef4444", green: "#10b981", border: "#223052", muted: "#8190b8",
  white: "#ffffff",
};
const context = {
  console, Date, Math, Number, Promise, Map, Set, Object, Array, String,
  THEME,
  DB_EXPECTED_COLOR: "#a855f7",
  RESOURCE_THRESHOLDS: {
    cpu_ok: 60, cpu_warn: 80, mem_ok: 70, mem_warn: 80,
    disk_ok: 70, disk_warn: 85, db_mem_band_low: 80, db_mem_band_high: 92,
  },
  document: { getElementById: id => elements[id] || null },
  window: { appData: { resource: { kpis: { thresholds: {
    cpu_ok: 60, cpu_warn: 80, mem_ok: 70, mem_warn: 80,
    disk_ok: 70, disk_warn: 85, db_mem_band_low: 80, db_mem_band_high: 92,
  } } } } },
  escapeHtml: value => String(value ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]),
  hexA: (hex, alpha) => `${hex}${Math.round(alpha * 255).toString(16).padStart(2, "0")}`,
  setTimeout, fetch: () => Promise.reject(new Error("network disabled in direct runner")),
  _deepDiveData: null, _deepDiveHoursBack: 0, _deepDiveAttribution: null,
};
vm.createContext(context);
vm.runInContext(app.slice(start, end), context);

const startDate = Date.parse("2026-07-01T00:00:00Z");
const avg = [], max = [];
for (let day = 0; day < 17; day += 1) {
  const t = new Date(startDate + day * 86400000).toISOString();
  avg.push({ t, v: 30 + day });
  max.push({ t, v: day === 10 ? 94 : 42 + day });
}
const payload = {
  window: { hours_back: 720, grain: "1h avg", timezone: "UTC", data_points: 17,
    start_utc: avg[0].t, end_utc: avg[avg.length - 1].t },
  vms: { app01: { role: "APP", series: { "Percentage CPU": avg },
    series_max: { "Percentage CPU": max } } },
  spike_attribution: { rows: [{ vm: "app01", jobs: [{
    job: "Demand Monthly", start: "2026-07-11T00:00:00Z",
    end: "2026-07-11T05:00:00Z", hrs: 5,
  }] }] },
};
context._deepDiveData = payload;
vm.runInContext("renderResourceTrendPanel(_deepDiveData)", context);

const chart = elements["resource-trend-chart"].innerHTML;
const heatmap = elements["resource-day-server-heatmap"].innerHTML;
if (!chart.includes("Demand Monthly") || !chart.includes("<rect") || !chart.includes("<polyline")) {
  throw new Error("trend chart did not render measured line plus batch-window overlay");
}
if (!chart.includes("Average + Maximum") || !chart.includes("94.0%")) {
  throw new Error("Azure Maximum series was not used for the visible critical peak");
}
if (!heatmap.includes("app01") || !heatmap.includes("2026-07-11") || !heatmap.includes("94.0%")) {
  throw new Error("17-day server heatmap did not render the daily critical peak");
}
console.log("PASS: 17-day measured trend, MAX overlay, batch window, and day × server heatmap");
