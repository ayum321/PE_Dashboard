/* Node test suite for static/interpretation_formatters.js
 * Run: node _test_interpretation_formatters.js
 * Proves every STRICT RULE + edge case. Exit 0 = all pass. */
const P = require("./static/interpretation_formatters.js");

let pass = 0, fail = 0;
function ok(name, cond) {
  if (cond) { pass++; }
  else { fail++; console.error("  FAIL:", name); }
}
function eq(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) { pass++; }
  else { fail++; console.error(`  FAIL: ${name}\n    got : ${g}\n    want: ${w}`); }
}

// ── alert_banner ───────────────────────────────────────────────────────────
(function () {
  const r = P.alertBanner({
    server: "prbc081402002", tags: ["PROD", "DB"], metric: "Available Mem", avail_pct: 18,
    last_seen_utc: "2026-07-08T10:00:00Z", anomaly_count_720h: 4,
    hypothesis: "inspect Oracle SGA growth during 00:00-06:00 batch window",
  });
  eq("alert_banner line1",
    r.line1,
    "prbc081402002 (PROD, DB) — Available Mem 18% avail — last seen 2026-07-08T10:00:00Z, 4 anomalies in last 720h");
  eq("alert_banner line2 from hypothesis",
    r.line2, "Highest priority: inspect Oracle SGA growth during 00:00-06:00 batch window");

  // empty hypothesis → fixed fallback
  const r2 = P.alertBanner({ server: "s1", metric: "CPU", avail_pct: 5, last_seen_utc: "t", anomaly_count_720h: 0 });
  eq("alert_banner empty hypothesis fallback",
    r2.line2, "Highest priority: insufficient signal to recommend action — monitor next cycle.");
  ok("alert_banner no tags → no parens", r2.line1.indexOf("(") === -1);

  // missing fields → insufficient data (not guessed)
  const r3 = P.alertBanner({ server: "s1" });
  ok("alert_banner missing metric", r3.line1.includes("insufficient data"));
  ok("alert_banner does not invent avail number", r3.line1.includes("insufficient data% avail"));
})();

// ── fleet_diagnosis ─────────────────────────────────────────────────────────
(function () {
  const r = P.fleetDiagnosis({
    fleet_grade: "B", fleet_score: 82.5, total_servers: 16,
    servers_approaching_threshold: 3, false_positive_count: 2,
    servers_list: ["s1", "s2", "s3"],
    per_server_reasons: [{ status: "warning" }, { status: "expected" }],
  });
  eq("fleet line1",
    r.line1, "3 server(s) approaching threshold limits. 2 apparent alert(s) were aggregation artifacts (filtered).");
  ok("fleet echoes score verbatim (no recompute)", r.closing.includes("(82.5/100)"));
  ok("fleet grade echoed", r.closing.includes("Fleet Grade B"));
  ok("fleet action lists servers when not all expected", r.closing.includes("Action required: s1, s2, s3"));
  ok("fleet no growth question when no growth_pattern", r.line2 === "");

  // all expected → No action needed
  const r2 = P.fleetDiagnosis({
    fleet_grade: "A", fleet_score: 95, servers_approaching_threshold: 0, false_positive_count: 0,
    servers_list: ["s1"], per_server_reasons: [{ status: "expected" }, { status: "expected" }],
  });
  ok("fleet all-expected → No action needed", r2.closing.includes("No action needed"));

  // growth_pattern present → exactly one diagnostic question
  const r3 = P.fleetDiagnosis({
    fleet_grade: "C", fleet_score: 70, servers_approaching_threshold: 2, false_positive_count: 0,
    servers_list: ["a", "b"],
    per_server_reasons: [{ status: "warning", growth_pattern: "scheduled" }, { status: "warning", growth_pattern: "organic" }],
  });
  ok("fleet growth question present", r3.line2.length > 0 && r3.line2.endsWith("?"));
  ok("fleet growth mentions scheduled+organic", /scheduled/.test(r3.line2) && /organic/.test(r3.line2));
})();

// ── monitoring_note (DATA CONFLICT safety net) ──────────────────────────────
(function () {
  const ok1 = P.monitoringNote({
    metric: "Mem used", current_pct: 88, expected_range_low: 80, expected_range_high: 92,
    classification: "EXPECTED", reason_code: "DB_SGA_PGA",
  });
  eq("monitoring in-band EXPECTED", ok1.text, "Mem used 88% — EXPECTED for DB_SGA_PGA (expected 80-92%)");
  ok("monitoring no conflict when in band", ok1.conflict === false);

  // out of band but labelled EXPECTED → DATA CONFLICT
  const c1 = P.monitoringNote({
    metric: "Mem used", current_pct: 96, expected_range_low: 80, expected_range_high: 92,
    classification: "EXPECTED", reason_code: "DB_SGA_PGA",
  });
  eq("monitoring DATA CONFLICT (above band)", c1.text, "[DATA CONFLICT — recompute]");
  ok("monitoring conflict flag set", c1.conflict === true);

  const c2 = P.monitoringNote({
    metric: "CPU", current_pct: 10, expected_range_low: 30, expected_range_high: 60,
    classification: "EXPECTED", reason_code: "IDLE",
  });
  eq("monitoring DATA CONFLICT (below band)", c2.text, "[DATA CONFLICT — recompute]");

  // WARNING out of band is allowed (not a conflict — only EXPECTED triggers net)
  const w = P.monitoringNote({
    metric: "CPU", current_pct: 95, expected_range_low: 30, expected_range_high: 60,
    classification: "WARNING", reason_code: "SPIKE",
  });
  eq("monitoring WARNING out-of-band ok", w.text, "CPU 95% — WARNING for SPIKE (expected 30-60%)");

  // invalid classification word → recompute
  const inv = P.monitoringNote({ metric: "CPU", current_pct: 50, expected_range_low: 0, expected_range_high: 100, classification: "CRITICAL", reason_code: "X" });
  eq("monitoring invalid classification → conflict", inv.text, "[DATA CONFLICT — recompute]");
})();

// ── anomaly_spotlight ───────────────────────────────────────────────────────
(function () {
  const r = P.anomalySpotlight({
    top_n: 2,
    servers: [
      { server: "a", metric: "CPU", deviation_pct: 40, direction: "above", baseline_window: "15d" },
      { server: "b", metric: "Mem", deviation_pct: 120, direction: "below", baseline_window: "15d" },
      { server: "c", metric: "Disk", deviation_pct: 22, direction: "above", baseline_window: "15d" },
    ],
  });
  eq("spotlight ranks desc + caps top_n", r.entries, [
    "b — Mem below 120% vs baseline (15d)",
    "a — CPU above 40% vs baseline (15d)",
  ]);
  ok("spotlight count == top_n", r.count === 2);

  // fewer than top_n above noise floor → append note
  const r2 = P.anomalySpotlight({
    top_n: 3,
    servers: [
      { server: "a", metric: "CPU", deviation_pct: 40, direction: "above", baseline_window: "7d" },
      { server: "b", metric: "Mem", deviation_pct: 9, direction: "below", baseline_window: "7d" },  // below noise floor
    ],
  });
  ok("spotlight drops sub-noise entry", r2.entries.length === 2);
  ok("spotlight adds noise note", r2.entries[r2.entries.length - 1] === "No further anomalies above noise threshold.");
  ok("spotlight count excludes noise + note", r2.count === 1);
})();

// ── server_row_status (array-length invariant) ──────────────────────────────
(function () {
  const rows = [
    { server: "s1", cpu_pct: 4, mem_used_pct: 88, disk_pct: 1, status_code: "DB NORMAL", reason_code: "SGA_PGA" },
    { server: "s2", cpu_pct: 90, mem_used_pct: 50, disk_pct: 10, status_code: "CRITICAL", reason_code: null },
    { server: "s3", cpu_pct: 20, mem_used_pct: 30, disk_pct: 5, status_code: "HEALTHY" },
  ];
  const out = P.serverRowStatus(rows);
  ok("server_row output length == input length", out.length === rows.length);
  eq("server_row verbatim + reason suffix", out[0].badge, "DB NORMAL (SGA_PGA)");
  eq("server_row null reason → no suffix", out[1].badge, "CRITICAL");
  eq("server_row missing reason → no suffix", out[2].badge, "HEALTHY");

  // empty + huge arrays keep invariant
  ok("server_row empty array", P.serverRowStatus([]).length === 0);
  const big = Array.from({ length: 1000 }, (_, i) => ({ server: "s" + i, status_code: "HEALTHY" }));
  ok("server_row 1000 rows single pass", P.serverRowStatus(big).length === 1000);
})();

// ── dispatcher ──────────────────────────────────────────────────────────────
(function () {
  ok("dispatcher routes", P.format("server_row_status", []).length === 0);
  let threw = false;
  try { P.format("nope", {}); } catch (e) { threw = true; }
  ok("dispatcher rejects unknown task", threw);
})();

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
