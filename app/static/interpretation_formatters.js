/* ============================================================================
 * PE Dashboard — Deterministic Interpretation Formatters
 * ----------------------------------------------------------------------------
 * PURE FORMATTERS. No LLM, no DOM, no network, no globals mutated.
 *
 * Design contract (the reason this file exists):
 *   - All severity / classification / ranking MATH happens upstream in the
 *     dashboard's rule engine. These functions ONLY translate a pre-computed
 *     JSON object into a precise, human-readable string.
 *   - A formatter can NEVER invent, estimate, or round a number. Values are
 *     echoed verbatim from the input JSON.
 *   - Adding a new scenario = extend the JSON schema. The formatters do not
 *     change. That is what makes this layer scale to "any scenario".
 *
 * Every function is total: given malformed / missing input it degrades to
 * "insufficient data" (per field) rather than guessing.
 *
 * Works with AI disabled (PE_AI_ENABLED=0) because there is no AI here.
 * ==========================================================================*/
(function (root) {
  "use strict";

  // ── Global STRICT RULES helpers ──────────────────────────────────────────

  const MISSING = "insufficient data";

  // Present = not null/undefined AND (for strings) not empty after trim.
  function present(v) {
    if (v === null || v === undefined) return false;
    if (typeof v === "string") return v.trim().length > 0;
    if (typeof v === "number") return !Number.isNaN(v);
    return true;
  }

  // Echo a value verbatim, or the MISSING sentinel. Never rounds/reformats numbers.
  function val(v) {
    return present(v) ? v : MISSING;
  }

  // Number echo: only accept a real finite number; otherwise MISSING.
  // Does NOT round — returns the number as-is for template interpolation.
  function num(v) {
    return typeof v === "number" && Number.isFinite(v) ? v : MISSING;
  }

  function tagString(tags) {
    if (!Array.isArray(tags)) return "";
    const clean = tags.filter(present).map(String);
    return clean.length ? clean.join(", ") : "";
  }

  // ── 2. alert_banner ──────────────────────────────────────────────────────
  // Single worst offender. Two lines. Mentions NO other server.
  // Input: {server, metric, severity, avail_pct, last_seen_utc,
  //         anomaly_count_720h, correlated_metrics, hypothesis, tags?}
  function alertBanner(input) {
    const j = input || {};
    const server = val(j.server);
    const metric = val(j.metric);
    const value = num(j.avail_pct);
    const lastSeen = val(j.last_seen_utc);
    const anomalies = num(j.anomaly_count_720h);
    const tags = tagString(j.tags);

    const head = tags ? `${server} (${tags})` : `${server}`;
    const line1 =
      `${head} — ${metric} ${value}% avail — last seen ${lastSeen}, ` +
      `${anomalies} anomalies in last 720h`;

    // Line 2 derives ONLY from hypothesis. Empty → fixed fallback string.
    const line2 = present(j.hypothesis)
      ? `Highest priority: ${String(j.hypothesis).trim()}`
      : "Highest priority: insufficient signal to recommend action — monitor next cycle.";

    return { line1, line2, text: `${line1}\n${line2}` };
  }

  // ── 3. fleet_diagnosis ───────────────────────────────────────────────────
  // Aggregate narrative + grade. Never computes/alters fleet_score — echoes it.
  // Input: {fleet_grade, fleet_score, total_servers,
  //         servers_approaching_threshold, false_positive_count,
  //         servers_list[], per_server_reasons[]}
  function fleetDiagnosis(input) {
    const j = input || {};
    const approaching = num(j.servers_approaching_threshold);
    const falsePos = num(j.false_positive_count);
    const reasons = Array.isArray(j.per_server_reasons) ? j.per_server_reasons : [];
    const list = Array.isArray(j.servers_list) ? j.servers_list.filter(present) : [];

    const line1 =
      `${approaching} server(s) approaching threshold limits. ` +
      `${falsePos} apparent alert(s) were aggregation artifacts (filtered).`;

    // Line 2 ONLY if per_server_reasons carries a growth_pattern field.
    let line2 = "";
    const withGrowth = reasons.filter((r) => r && present(r.growth_pattern));
    if (withGrowth.length) {
      const scheduled = withGrowth.filter(
        (r) => String(r.growth_pattern).toLowerCase() === "scheduled"
      );
      const organic = withGrowth.filter(
        (r) => String(r.growth_pattern).toLowerCase() === "organic"
      );
      if (scheduled.length && organic.length) {
        line2 =
          "Is the rising utilisation scheduled batch load or organic growth — " +
          `${scheduled.length} server(s) look scheduled, ${organic.length} look organic?`;
      } else if (scheduled.length) {
        line2 =
          "Is the recurring utilisation a scheduled batch window rather than sustained growth?";
      } else if (organic.length) {
        line2 =
          "Is the rising utilisation organic growth that will need capacity headroom soon?";
      } else {
        const gp = String(withGrowth[0].growth_pattern);
        line2 = `Is the observed growth_pattern "${gp}" expected for this workload?`;
      }
    }

    // Closing: echo grade + score. "No action needed" ONLY if EVERY reason is expected.
    const grade = val(j.fleet_grade);
    const score = num(j.fleet_score); // echoed, never recomputed
    let action;
    const statuses = reasons.map((r) => (r && present(r.status) ? String(r.status).toLowerCase() : null));
    const allExpected = statuses.length > 0 && statuses.every((s) => s === "expected");
    if (allExpected) {
      action = "No action needed";
    } else if (list.length) {
      action = `Action required: ${list.join(", ")}`;
    } else {
      action = "Action required: server list " + MISSING;
    }
    const closing = `Fleet Grade ${grade} (${score}/100). ${action}.`;

    const lines = [line1];
    if (line2) lines.push(line2);
    lines.push(closing);
    return { line1, line2, closing, text: lines.join("\n") };
  }

  // ── 4. monitoring_note ───────────────────────────────────────────────────
  // Per-server expected-vs-anomaly classification with a DATA CONFLICT net.
  // Input: {server, tags[], metric, current_pct, expected_range_low,
  //         expected_range_high, classification, reason_code}
  const VALID_CLASSIFICATION = new Set(["EXPECTED", "WARNING", "ANOMALOUS"]);
  function monitoringNote(input) {
    const j = input || {};
    const metric = val(j.metric);
    const current = num(j.current_pct);
    const low = num(j.expected_range_low);
    const high = num(j.expected_range_high);
    const cls = present(j.classification) ? String(j.classification).trim() : null;
    const reason = val(j.reason_code);

    // Safety net #1 — classification must be a known label, else recompute.
    if (!cls || !VALID_CLASSIFICATION.has(cls)) {
      return { text: "[DATA CONFLICT — recompute]", conflict: true };
    }

    // Safety net #2 — number outside expected band but labelled EXPECTED.
    const rangeKnown = typeof current === "number" && typeof low === "number" && typeof high === "number";
    if (rangeKnown && cls === "EXPECTED" && (current < low || current > high)) {
      return { text: "[DATA CONFLICT — recompute]", conflict: true };
    }

    const text = `${metric} ${current}% — ${cls} for ${reason} (expected ${low}-${high}%)`;
    return { text, conflict: false, classification: cls };
  }

  // ── 5. anomaly_spotlight ─────────────────────────────────────────────────
  // Ranked outliers. Strict deviation_pct desc. Exactly top_n (no more).
  // Noise floor: deviation_pct < 15% does not qualify.
  // Input: {servers:[{server, metric, deviation_pct, direction, baseline_window}], top_n}
  const NOISE_FLOOR = 15;
  function anomalySpotlight(input) {
    const j = input || {};
    const topN = typeof j.top_n === "number" && j.top_n > 0 ? Math.floor(j.top_n) : 0;
    const servers = Array.isArray(j.servers) ? j.servers.slice() : [];

    // Rank strictly by deviation_pct desc. Non-numeric deviation sinks to bottom.
    servers.sort((a, b) => {
      const av = typeof a.deviation_pct === "number" && Number.isFinite(a.deviation_pct) ? a.deviation_pct : -Infinity;
      const bv = typeof b.deviation_pct === "number" && Number.isFinite(b.deviation_pct) ? b.deviation_pct : -Infinity;
      return bv - av;
    });

    const qualifying = servers.filter(
      (s) => typeof s.deviation_pct === "number" && Number.isFinite(s.deviation_pct) && s.deviation_pct >= NOISE_FLOOR
    );

    const shown = qualifying.slice(0, topN);
    const entries = shown.map(
      (s) =>
        `${val(s.server)} — ${val(s.metric)} ${val(s.direction)} ${num(s.deviation_pct)}% ` +
        `vs baseline (${val(s.baseline_window)})`
    );

    // If fewer than top_n qualify, append the noise-threshold note.
    if (qualifying.length < topN) {
      entries.push("No further anomalies above noise threshold.");
    }
    return { entries, count: shown.length, text: entries.join("\n") };
  }

  // ── 6. server_row_status ─────────────────────────────────────────────────
  // Bulk. status_code verbatim. reason_code suffix only when non-null.
  // Single pass — output.length MUST equal input.length.
  // Input: [{server, cpu_pct, mem_used_pct, disk_pct, status_code, reason_code}]
  function serverRowStatus(rows) {
    const arr = Array.isArray(rows) ? rows : [];
    const out = arr.map((r) => {
      const o = r || {};
      const badge = present(o.status_code) ? String(o.status_code) : MISSING;
      const suffix = present(o.reason_code) ? ` (${String(o.reason_code)})` : "";
      return { server: val(o.server), badge: `${badge}${suffix}` };
    });
    // Invariant guard: never drop or merge rows.
    if (out.length !== arr.length) {
      throw new Error(`server_row_status invariant violated: ${out.length} != ${arr.length}`);
    }
    return out;
  }

  // ── Dispatcher ───────────────────────────────────────────────────────────
  const TASKS = {
    alert_banner: alertBanner,
    fleet_diagnosis: fleetDiagnosis,
    monitoring_note: monitoringNote,
    anomaly_spotlight: anomalySpotlight,
    server_row_status: serverRowStatus,
  };
  function format(task, input) {
    const fn = TASKS[task];
    if (!fn) throw new Error(`unknown interpretation task: ${task}`);
    return fn(input);
  }

  const API = {
    MISSING,
    NOISE_FLOOR,
    VALID_CLASSIFICATION,
    alertBanner,
    fleetDiagnosis,
    monitoringNote,
    anomalySpotlight,
    serverRowStatus,
    format,
  };

  // Browser: expose on window. Node: module.exports (for tests).
  if (typeof module !== "undefined" && module.exports) module.exports = API;
  root.PEInterpret = API;
})(typeof window !== "undefined" ? window : globalThis);
