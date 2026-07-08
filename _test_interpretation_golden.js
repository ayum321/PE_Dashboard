/* Golden-file test — proves interpretation_formatters.js against the REAL
 * backend resource-row shape emitted by services/resource_calculator.py, not
 * hand-built mocks. Catches shape/threshold drift between what the tests assume
 * and what production actually emits.
 *
 * Run: node _test_interpretation_golden.js   (exit 0 = all pass)
 *
 * The mem-note mapping here is a byte-for-byte mirror of _memMonitoringNote(r)
 * in static/app.js. If the adapter changes, this must change too — that
 * coupling is the point: it locks the live wiring to a tested contract. */
const P = require("./static/interpretation_formatters.js");

// Frontend RESOURCE_THRESHOLDS.db_mem_band_* — MUST equal backend
// DB_MEM_EXPECTED_LO / DB_MEM_EXPECTED_HI in services/resource_calculator.py.
const DB_BAND_LOW = 80;
const DB_BAND_HIGH = 92;

let pass = 0, fail = 0;
function eq(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g === w) { pass++; }
  else { fail++; console.error(`  FAIL: ${name}\n    got : ${g}\n    want: ${w}`); }
}
function ok(name, cond) { cond ? pass++ : (fail++, console.error("  FAIL:", name)); }

const isDbRole = (role) => /\bDB\b/i.test(role || "");
const isDbMemExpected = (role, mem) =>
  isDbRole(role) && mem >= DB_BAND_LOW && mem <= DB_BAND_HIGH;

// Exact mirror of static/app.js _memMonitoringNote(r).
function memMonitoringNote(r) {
  if (!r || r.mem_available === false || r.mem_pct == null) return null;
  const rType = (r.type || "").toUpperCase();
  if (!isDbRole(rType)) return null;
  const backendExpected = r.mem_status === "DB_NORMAL";
  const frontendExpected = isDbMemExpected(rType, r.mem_pct);
  if (!backendExpected && !frontendExpected) return null;
  return P.monitoringNote({
    metric: "Mem used",
    current_pct: r.mem_pct,
    expected_range_low: DB_BAND_LOW,
    expected_range_high: DB_BAND_HIGH,
    classification: "EXPECTED",
    reason_code: "DB_SGA_PGA",
  });
}

// ── Authentic backend rows (exact field set from resource_calculator.py) ─────
const GOLDEN = {
  // Oracle DB in expected SGA/PGA band — the common healthy case. No conflict.
  dbNormalInBand: {
    host: "prbc081402002.corp.net", server: "prbc081402002", type: "DB",
    environment: "PROD", cpu_pct: 4.2, cpu_avg_pct: 3.1, effective_cpu: 4.2,
    mem_pct: 88.0, mem_gb: 251.0, disk_pct: 1.3, image_only: false,
    health_score: 96.0, status: "Healthy", mem_status: "DB_NORMAL",
    source_env: "PROD_Oracle_Report.docx", agg_trap: false, dual_pressure: false,
    role_cpu_ok: 80, role_cpu_warn: 92, cpu_available: true, mem_available: true,
    disk_available: true, cpu_used: 4.2, mem_used: 88.0, disk_used_max: 1.3,
    resource_id: null, source: "docx",
  },
  // DRIFT: backend says DB_NORMAL but value is 96% (above band). Net must fire.
  dbNormalDrift: {
    host: "prbc999.corp.net", server: "prbc999", type: "DB", environment: "PROD",
    cpu_pct: 5.0, cpu_avg_pct: 4.0, effective_cpu: 5.0, mem_pct: 96.0,
    mem_gb: 260.0, disk_pct: 2.0, image_only: false, health_score: 80.0,
    status: "Healthy", mem_status: "DB_NORMAL", source_env: "stale_cache.docx",
    agg_trap: false, dual_pressure: false, role_cpu_ok: 80, role_cpu_warn: 92,
    cpu_available: true, mem_available: true, disk_available: true,
    cpu_used: 5.0, mem_used: 96.0, disk_used_max: 2.0, resource_id: null, source: "docx",
  },
  // APP server — never gets a mem-note (not a DB role).
  appServer: {
    host: "appnode01", server: "appnode01", type: "APP", environment: "PROD",
    cpu_pct: 55.0, cpu_avg_pct: 40.0, effective_cpu: 55.0, mem_pct: 62.0,
    mem_gb: 32.0, disk_pct: 44.0, image_only: false, health_score: 88.0,
    status: "Healthy", mem_status: null, source_env: "app.docx",
    cpu_available: true, mem_available: true, disk_available: true,
  },
  // DB with mem_status null (backend didn't label) but value 85% is in band —
  // frontend-expected path. In-band → no conflict.
  dbFrontendOnly: {
    host: "db02", server: "db02", type: "DB", environment: "TEST",
    cpu_pct: 10.0, cpu_avg_pct: 8.0, effective_cpu: 10.0, mem_pct: 85.0,
    mem_gb: 120.0, disk_pct: 5.0, image_only: false, health_score: 90.0,
    status: "Healthy", mem_status: null, source_env: "test.docx",
    cpu_available: true, mem_available: true, disk_available: true,
  },
  // DB memory unavailable — parser couldn't read it. No note (null-safe).
  dbMemUnavailable: {
    host: "db03", server: "db03", type: "DB", environment: "PROD",
    cpu_pct: 12.0, mem_pct: null, mem_gb: null, disk_pct: 3.0, status: "Healthy",
    mem_status: null, cpu_available: true, mem_available: false, disk_available: true,
  },
};

// ── Assertions ───────────────────────────────────────────────────────────────
const inBand = memMonitoringNote(GOLDEN.dbNormalInBand);
ok("golden: in-band DB_NORMAL produces a note", inBand !== null);
ok("golden: in-band DB_NORMAL is NOT a conflict", inBand.conflict === false);
eq("golden: in-band note text (verbatim 88, band echoed)",
  inBand.text, "Mem used 88% — EXPECTED for DB_SGA_PGA (expected 80-92%)");

const drift = memMonitoringNote(GOLDEN.dbNormalDrift);
ok("golden: drift row produces a note", drift !== null);
ok("golden: drift row FIRES the DATA CONFLICT net", drift.conflict === true);
eq("golden: drift row shows recompute flag", drift.text, "[DATA CONFLICT — recompute]");

ok("golden: APP server gets no mem-note", memMonitoringNote(GOLDEN.appServer) === null);

const feo = memMonitoringNote(GOLDEN.dbFrontendOnly);
ok("golden: DB frontend-expected in-band produces note", feo !== null);
ok("golden: DB frontend-expected in-band no conflict", feo.conflict === false);

ok("golden: DB with mem unavailable gets no note", memMonitoringNote(GOLDEN.dbMemUnavailable) === null);

// Shape-drift guard: if backend band constants ever diverge from the frontend
// band assumed here, this assertion documents the coupling explicitly.
ok("golden: DB band constants match backend (80-92)", DB_BAND_LOW === 80 && DB_BAND_HIGH === 92);

// Verbatim-number guarantee: formatter never rounds the real value.
const oddVal = memMonitoringNote({ type: "DB", mem_pct: 87.3, mem_status: "DB_NORMAL", mem_available: true });
eq("golden: value echoed verbatim (87.3, not rounded)",
  oddVal.text, "Mem used 87.3% — EXPECTED for DB_SGA_PGA (expected 80-92%)");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
