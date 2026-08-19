"""
judgment_engine — severity-weighted pillar scoring + auditable evidence ledger
+ deterministic cross-pillar correlation for the cross-pillar Final Judgment.

WHY THIS EXISTS
---------------
The legacy per-pillar scores were pure pass-rates: a pillar that is 95%
compliant scored 95 even if the 5% that failed overran their SLA by 10×.
A senior reviewer would never sign that off as "grade A". This engine keeps
the pass-rate as the BASE, then applies BOUNDED, individually-cited severity
penalties so the verdict reflects how bad the failures actually were — and
emits an evidence ledger (fact → threshold → PASS/FAIL → points) so every
point moved is defensible by a number the reviewer can see.

TWO MODES (per-customer switchable via pe_config.FJ_SCORING_MODE)
-----------------------------------------------------------------
  "additive"  (default, SAFE): score = pass_rate − Σ(capped penalties).
              A verdict can only get STRICTER, never falsely looser. A clean
              dataset scores exactly as before; a severe one scores lower.
  "recompute" (aggressive): the binding constraint (window vs job-level) becomes
              the base and per-signal penalties apply with no per-pillar total
              cap — a fuller multi-signal recompute the customer can opt into.

GUARANTEE
---------
In "additive" mode, `final_score <= base_score` for every pillar (enforced and
unit-tested). The Final-Judgment decision matrix's hard-block floors still apply
on top, so catastrophic-but-bounded scores never approve.

This module is pure/deterministic and AI-free: it works with no LLM and its
numbers are the GROUND TRUTH the verdict_reconciler defends against the LLM.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from services import pe_config


# ── small safe coercion helpers ──────────────────────────────────────────────
def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        n = float(v)
        if n != n:  # NaN guard
            return None
        return n
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    n = _f(v)
    return None if n is None else int(n)


def _dys(n: Any) -> str:
    """Grammatical 'day'/'days' for a count — avoids machine-style 'day(s)'."""
    return "day" if _i(n) == 1 else "days"


def _first(*vals: Any) -> Any:
    """Coalesce that treats 0/0.0 as valid (unlike `a or b`)."""
    for v in vals:
        if v is not None:
            return v
    return None


def _dig(d: Optional[Dict[str, Any]], *keys: str) -> Any:
    """Read the first present key from a dict OR its nested `kpis` sub-dict.

    The frontend posts the SLA pillar in two shapes depending on the code path
    (flat `{compliance_pct: ...}` on upload, nested `{kpis: {...}}` on cache
    restore). Reading both makes the engine robust to either.
    """
    if not isinstance(d, dict):
        return None
    sub = d.get("kpis") if isinstance(d.get("kpis"), dict) else {}
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
        if k in sub and sub[k] is not None:
            return sub[k]
    return None


def _ev(pillar: str, signal: str, fact: str, status: str,
        points: float = 0.0, value: Any = None,
        threshold: str = "", citation: str = "") -> Dict[str, Any]:
    """Build one evidence-ledger entry. `points` is the signed score impact."""
    return {
        "pillar":    pillar,
        "signal":    signal,
        "fact":      fact,
        "status":    status,                 # PASS | FAIL | PENALTY | INFO
        "points":    round(float(points), 1),
        "value":     value,
        "threshold": threshold,
        "citation":  citation,
    }


class PillarScore:
    """Result of scoring one pillar: final score + the raw penalties + evidence."""
    __slots__ = ("name", "base", "score", "penalty_raw", "penalty_applied",
                 "evidence", "capped")

    def __init__(self, name: str, base: float):
        self.name: str = name
        self.base: float = round(base, 1)
        self.score: float = round(base, 1)
        self.penalty_raw: float = 0.0       # sum of per-signal penalties (uncapped)
        self.penalty_applied: float = 0.0   # actual points removed (after total cap)
        self.evidence: List[Dict[str, Any]] = []
        self.capped: bool = False


# ── penalty helper: clamp a raw penalty to its per-signal cap ─────────────────
def _capped(raw: float, cap: float) -> float:
    return round(min(max(raw, 0.0), cap), 1)


# ═════════════════════════════════════════════════════════════════════════════
# BATCH
# ═════════════════════════════════════════════════════════════════════════════
def _score_batch(b: Optional[Dict[str, Any]], mode: str) -> Optional[PillarScore]:
    if not b:
        return None
    base = _f(_dig(b, "compliance_pct"))
    if base is None:
        return None

    ps = PillarScore("batch", base)
    ev = ps.evidence
    pen = 0.0

    # window-day compliance is the binding PE headline (the share of calendar
    # days the batch finished inside its SLA window).
    wc = _f(_first(_dig(b, "window_day_compliance_pct"),
                   _dig(b, "window_compliance_pct")))
    wbd = _i(_dig(b, "window_breach_days"))
    wtd = _i(_dig(b, "window_total_days"))
    if wc is not None and wc < base:
        gap = base - wc
        p = _capped(gap, pe_config.FJ_PEN_WINDOW_CAP)
        pen += p
        if wbd is not None and wtd:
            clean = wtd - wbd
            fact = (f"Batch finished within its SLA window on only "
                    f"{clean} of {wtd} {_dys(wtd)} ({wc:.1f}%), below the {base:.1f}% "
                    f"job-level pass rate — window is the binding constraint")
            cite = f"window_day_compliance_pct={wc:.1f}, clean_days={clean}/{wtd}"
        else:
            fact = (f"Window compliance {wc:.1f}% is below the {base:.1f}% "
                    f"job-level pass rate — window is the binding constraint")
            cite = f"window_compliance_pct={wc:.1f}"
        ev.append(_ev("batch", "window_binding", fact, "FAIL", -p,
                      value=wc, threshold=f"job-level {base:.1f}%", citation=cite))
    elif wc is not None:
        ev.append(_ev("batch", "window_binding",
                      f"Window compliance {wc:.1f}% ≥ job-level pass rate — no binding penalty",
                      "PASS", 0.0, value=wc, citation=f"window_compliance_pct={wc:.1f}"))

    # execution failure rate (ENDED NOT OK / ABENDED) — distinct from SLA breach
    fr = _f(_dig(b, "fail_rate_pct"))
    thr = pe_config.BATCH_FAIL_RATE
    if fr is not None and fr > thr:
        p = _capped((fr - thr) * pe_config.FJ_PEN_FAILRATE_PER_PCT,
                    pe_config.FJ_PEN_FAILRATE_CAP)
        pen += p
        failed = _i(_dig(b, "failed_runs"))
        cite = f"fail_rate_pct={fr:.1f}" + (f", failed_runs={failed}" if failed else "")
        ev.append(_ev("batch", "failure_rate",
                      f"Execution failure rate {fr:.1f}% exceeds the {thr:.0f}% tolerance",
                      "FAIL", -p, value=fr, threshold=f"{thr:.0f}%", citation=cite))
    elif fr is not None:
        ev.append(_ev("batch", "failure_rate",
                      f"Execution failure rate {fr:.1f}% within {thr:.0f}% tolerance",
                      "PASS", 0.0, value=fr, threshold=f"{thr:.0f}%"))

    # worst single-job overrun magnitude — how far the worst job ran past ceiling
    over_pct, worst_job, worst_peak = _worst_overrun(b)
    if over_pct is not None and over_pct > 0:
        p = _capped(over_pct * pe_config.FJ_PEN_OVERRUN_PER_PCT,
                    pe_config.FJ_PEN_OVERRUN_CAP)
        pen += p
        jtxt = f" ({worst_job})" if worst_job else ""
        ptxt = f", peak {worst_peak:.2f}h" if worst_peak is not None else ""
        ev.append(_ev("batch", "worst_overrun",
                      f"Worst job{jtxt} ran {over_pct:.0f}% past its SLA ceiling{ptxt}",
                      "FAIL", -p, value=over_pct, threshold="100% of ceiling",
                      citation=f"worst_sla_used_pct={100 + over_pct:.0f}"))

    # runtime regression depth
    rc = _i(_first(_dig(b, "regression_count"), _dig(b, "runtime_regression_count")))
    if rc and rc > 0:
        p = _capped(rc * pe_config.FJ_PEN_REGRESSION_PER_JOB,
                    pe_config.FJ_PEN_REGRESSION_CAP)
        pen += p
        ev.append(_ev("batch", "regression_depth",
                      f"{rc} job(s) regressed in runtime vs baseline",
                      "FAIL", -p, value=rc, threshold="0 regressions",
                      citation=f"regression_count={rc}"))

    return _finalize(ps, pen, mode, binding_base=wc)


def _worst_overrun(b: Dict[str, Any]) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    """Return (worst_pct_over_ceiling, job_name, peak_hrs) from top_breaches."""
    rows = b.get("top_breaches") or b.get("top_jobs") or []
    worst_over: Optional[float] = None
    worst_job: Optional[str] = None
    worst_peak: Optional[float] = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        used = _f(r.get("sla_used_pct"))
        if used is None:
            buf = _f(r.get("buffer_pct"))
            if buf is not None:
                used = 100.0 - buf      # buffer is negative on breach
        if used is None:
            continue
        over = used - 100.0
        if worst_over is None or over > worst_over:
            worst_over = over
            worst_job = r.get("Job_Name") or r.get("job_name")
            worst_peak = _f(r.get("peak_hrs"))
    return worst_over, worst_job, worst_peak


# ═════════════════════════════════════════════════════════════════════════════
# SLA MATRIX
# ═════════════════════════════════════════════════════════════════════════════
def _score_sla(s: Optional[Dict[str, Any]], mode: str) -> Optional[PillarScore]:
    if not s:
        return None
    base = _f(_dig(s, "compliance_pct", "run_sla_compliance_pct"))
    if base is None:
        return None

    ps = PillarScore("sla", base)
    ev = ps.evidence
    pen = 0.0

    wc = _f(_first(_dig(s, "window_day_compliance_pct"),
                   _dig(s, "window_compliance_pct")))
    wbd = _i(_dig(s, "window_breach_days"))
    wtd = _i(_dig(s, "window_total_days"))
    if wc is not None and wc < base:
        gap = base - wc
        p = _capped(gap, pe_config.FJ_PEN_WINDOW_CAP)
        pen += p
        if wbd is not None and wtd:
            clean = wtd - wbd
            fact = (f"SLA window met on only {clean} of {wtd} {_dys(wtd)} ({wc:.1f}%), "
                    f"below the {base:.1f}% run-level pass rate")
            cite = f"window_day_compliance_pct={wc:.1f}, clean_days={clean}/{wtd}"
        else:
            fact = (f"SLA window compliance {wc:.1f}% below the {base:.1f}% "
                    f"run-level pass rate")
            cite = f"window_compliance_pct={wc:.1f}"
        ev.append(_ev("sla", "window_binding", fact, "FAIL", -p,
                      value=wc, threshold=f"run-level {base:.1f}%", citation=cite))
    elif wc is not None:
        ev.append(_ev("sla", "window_binding",
                      f"SLA window compliance {wc:.1f}% ≥ run-level pass rate — no binding penalty",
                      "PASS", 0.0, value=wc, citation=f"window_compliance_pct={wc:.1f}"))

    # breach breadth — share of runs over their SLA ceiling
    br = _i(_first(_dig(s, "breaching_runs"), _dig(s, "jobs_breach")))
    tot = _i(_first(_dig(s, "total_runs"), _dig(s, "total_jobs")))
    if br and tot and tot > 0 and br > 0:
        rate = br / tot * 100.0
        p = _capped(rate * pe_config.FJ_PEN_SLA_PER_PCT, pe_config.FJ_PEN_SLA_MAG_CAP)
        pen += p
        ev.append(_ev("sla", "breach_breadth",
                      f"{br}/{tot} runs ({rate:.1f}%) breached their SLA ceiling",
                      "FAIL", -p, value=rate, threshold="0 breaches",
                      citation=f"breaching_runs={br}, total_runs={tot}"))
    elif tot:
        ev.append(_ev("sla", "breach_breadth",
                      f"0/{tot} runs breached their SLA ceiling",
                      "PASS", 0.0, value=0, citation=f"total_runs={tot}"))

    return _finalize(ps, pen, mode, binding_base=wc)


# ═════════════════════════════════════════════════════════════════════════════
# BENCHMARK
# ═════════════════════════════════════════════════════════════════════════════
def _score_benchmark(bm: Optional[Dict[str, Any]], mode: str) -> Optional[PillarScore]:
    if not bm:
        return None
    total = _i(bm.get("total_transactions")) or 0
    if total == 0:
        return None
    degraded = _i(bm.get("degraded")) or 0
    base = max(0.0, 100.0 * (1.0 - degraded / total))

    ps = PillarScore("benchmark", base)
    ev = ps.evidence
    pen = 0.0

    ev.append(_ev("benchmark", "pass_rate",
                  f"{total - degraded}/{total} transactions within threshold",
                  "INFO", 0.0, value=round(base, 1),
                  citation=f"degraded={degraded}, total={total}"))

    wd = _f(bm.get("worst_delta_pct"))
    thr = pe_config.BENCH_THRESHOLD_PCT
    if wd is not None and wd > thr:
        p = _capped((wd - thr) * pe_config.FJ_PEN_BENCH_PER_PCT,
                    pe_config.FJ_PEN_BENCH_MAG_CAP)
        pen += p
        ev.append(_ev("benchmark", "worst_delta",
                      f"Worst transaction regressed +{wd:.0f}% vs baseline (threshold {thr:.0f}%)",
                      "FAIL", -p, value=wd, threshold=f"{thr:.0f}%",
                      citation=f"worst_delta_pct={wd:.1f}"))
    elif wd is not None:
        ev.append(_ev("benchmark", "worst_delta",
                      f"Worst transaction delta +{wd:.0f}% within {thr:.0f}% threshold",
                      "PASS", 0.0, value=wd, threshold=f"{thr:.0f}%"))

    return _finalize(ps, pen, mode)


# ═════════════════════════════════════════════════════════════════════════════
# RESOURCE
# ═════════════════════════════════════════════════════════════════════════════
def _score_resource(r: Optional[Dict[str, Any]], mode: str) -> Optional[PillarScore]:
    if not r:
        return None
    servers = r.get("servers") or []
    if not servers:
        return None
    healths = [_f(s.get("health_score")) for s in servers]
    healths = [h for h in healths if h is not None and h > 0]
    if not healths:
        return None
    base = sum(healths) / len(healths)

    ps = PillarScore("resource", base)
    ev = ps.evidence
    pen = 0.0

    # critical servers (explicit n_critical, else health < 60)
    nc = _i(_first(r.get("n_critical"), _dig(r, "n_critical")))
    if nc is None:
        nc = sum(1 for h in healths if h < 60)
    if nc and nc > 0:
        p = _capped(nc * pe_config.FJ_PEN_RES_CRIT_PER, pe_config.FJ_PEN_RES_CRIT_CAP)
        pen += p
        ev.append(_ev("resource", "critical_servers",
                      f"{nc} server(s) in CRITICAL health",
                      "FAIL", -p, value=nc, threshold="0 critical",
                      citation=f"n_critical={nc}"))
    else:
        ev.append(_ev("resource", "critical_servers",
                      "No servers in CRITICAL health", "PASS", 0.0, value=0))

    # dual pressure — simultaneous CPU + memory stress on the same host
    cpu_w, mem_w = pe_config.CPU_WARN, pe_config.MEM_WARN
    dual = 0
    dual_hosts: List[str] = []
    for s in servers:
        cpu = _f(s.get("cpu_used") or s.get("cpu_pct") or s.get("cpu"))
        mem = _f(s.get("mem_used") or s.get("mem_pct") or s.get("mem"))
        if cpu is not None and mem is not None and cpu >= cpu_w and mem >= mem_w:
            dual += 1
            h = s.get("host") or s.get("server")
            if h:
                dual_hosts.append(str(h))
    if dual > 0:
        p = _capped(dual * pe_config.FJ_PEN_RES_DUAL_PER, pe_config.FJ_PEN_RES_DUAL_CAP)
        pen += p
        htxt = " (" + ", ".join(dual_hosts[:3]) + ")" if dual_hosts else ""
        ev.append(_ev("resource", "dual_pressure",
                      f"{dual} server(s) under simultaneous CPU≥{cpu_w:.0f}% + "
                      f"memory≥{mem_w:.0f}% pressure{htxt}",
                      "FAIL", -p, value=dual,
                      threshold=f"CPU<{cpu_w:.0f}% or mem<{mem_w:.0f}%",
                      citation=f"dual_pressure_servers={dual}"))

    return _finalize(ps, pen, mode)


# ═════════════════════════════════════════════════════════════════════════════
# SOW / VOLUME
# ═════════════════════════════════════════════════════════════════════════════
def _score_sow(sw: Optional[Dict[str, Any]], mode: str) -> Optional[PillarScore]:
    if not sw:
        return None
    metrics = sw.get("metrics") or []
    if not metrics:
        return None
    optimal = sum(1 for m in metrics if str(m.get("status")).upper() == "OPTIMAL")
    base = 100.0 * optimal / len(metrics)

    ps = PillarScore("sow", base)
    ev = ps.evidence
    pen = 0.0

    over = [m for m in metrics
            if str(m.get("status")).upper() in ("EXCEEDED", "CRITICAL", "OVER", "BREACH")]
    if over:
        p = _capped(len(over) * pe_config.FJ_PEN_SOW_PER, pe_config.FJ_PEN_SOW_MAG_CAP)
        pen += p
        names = ", ".join(str(m.get("metric") or m.get("name") or "metric")
                          for m in over[:4])
        ev.append(_ev("sow", "over_baseline",
                      f"{len(over)} metric(s) over contractual baseline ({names})",
                      "FAIL", -p, value=len(over), threshold="0 over baseline",
                      citation=f"over_baseline={len(over)}/{len(metrics)}"))
    else:
        ev.append(_ev("sow", "over_baseline",
                      f"All {len(metrics)} volume metric(s) within contractual baseline",
                      "PASS", 0.0, value=0))

    return _finalize(ps, pen, mode)


# ═════════════════════════════════════════════════════════════════════════════
# CORRELATION  (already severity-aware — wrap to emit evidence, keep math)
# ═════════════════════════════════════════════════════════════════════════════
def _score_correlation(c: Optional[Dict[str, Any]], mode: str) -> Optional[PillarScore]:
    if not c:
        return None
    rows = c.get("rows") or []
    if not rows:
        return None
    crit = sum(1 for r in rows if str(r.get("risk")).upper() == "CRITICAL")
    high = sum(1 for r in rows if str(r.get("risk")).upper() == "HIGH")
    raw_pen = crit * 12.0 + high * 6.0
    base = max(0.0, 100.0 - raw_pen)

    ps = PillarScore("correlation", 100.0)
    ps.score = round(base, 1)
    ps.penalty_raw = round(raw_pen, 1)
    ps.penalty_applied = round(100.0 - base, 1)
    if crit or high:
        ps.evidence.append(_ev("correlation", "risky_links",
                               f"{crit} CRITICAL + {high} HIGH cross-signal correlation(s)",
                               "FAIL", -(100.0 - base),
                               value=crit + high, threshold="0 risky links",
                               citation=f"critical={crit}, high={high}"))
    else:
        ps.evidence.append(_ev("correlation", "risky_links",
                               "No CRITICAL/HIGH cross-signal correlations",
                               "PASS", 0.0, value=0))
    return ps


# ── shared finalizer: apply mode logic + total cap + score-line evidence ──────
def _finalize(ps: PillarScore, pen_raw: float, mode: str,
              binding_base: Optional[float] = None) -> PillarScore:
    ps.penalty_raw = round(pen_raw, 1)

    if mode == "recompute":
        # Aggressive: binding constraint becomes the base (when one exists),
        # penalties apply with no per-pillar total cap.
        base = ps.base
        if binding_base is not None and binding_base < base:
            base = binding_base
        applied = pen_raw
        final = max(0.0, base - applied)
        ps.penalty_applied = round(applied, 1)
    else:
        # Additive (default, safe): bounded total penalty, can only tighten.
        applied = min(pen_raw, pe_config.FJ_PEN_TOTAL_CAP)
        ps.capped = pen_raw > pe_config.FJ_PEN_TOTAL_CAP + 1e-9
        final = max(0.0, ps.base - applied)
        ps.penalty_applied = round(applied, 1)

    ps.score = round(final, 1)

    if ps.capped:
        ps.evidence.append(_ev(
            ps.name, "penalty_cap",
            f"Penalties capped at {pe_config.FJ_PEN_TOTAL_CAP:.0f} pts "
            f"(raw severity {ps.penalty_raw:.1f}) — bounded scoring",
            "INFO", 0.0, value=ps.penalty_applied,
            citation=f"raw={ps.penalty_raw:.1f}, applied={ps.penalty_applied:.1f}"))

    # Headline score line so the ledger always shows base → final per pillar.
    ps.evidence.insert(0, _ev(
        ps.name, "score",
        f"{ps.name.upper()} base {ps.base:.1f} → final {ps.score:.1f} "
        f"(−{ps.penalty_applied:.1f} severity)",
        "INFO", 0.0, value=ps.score,
        citation=f"base={ps.base:.1f}, applied=-{ps.penalty_applied:.1f}, mode={mode}"))
    return ps


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═════════════════════════════════════════════════════════════════════════════
class ScoringResult:
    __slots__ = ("scores", "details", "evidence_chain", "mode")

    def __init__(self, mode: str):
        self.mode: str = mode
        self.scores: Dict[str, float] = {}
        self.details: Dict[str, PillarScore] = {}
        self.evidence_chain: List[Dict[str, Any]] = []


def score_all_pillars(
    resource: Optional[Dict[str, Any]] = None,
    batch: Optional[Dict[str, Any]] = None,
    sla: Optional[Dict[str, Any]] = None,
    benchmark: Optional[Dict[str, Any]] = None,
    sow: Optional[Dict[str, Any]] = None,
    correlation: Optional[Dict[str, Any]] = None,
    mode: Optional[str] = None,
) -> ScoringResult:
    """Score every present pillar with severity penalties + an evidence ledger.

    Returns a ScoringResult whose `.scores` maps pillar→final score (only pillars
    that produced a score are present, matching the legacy behaviour), `.details`
    holds the per-pillar PillarScore objects, and `.evidence_chain` is the flat
    audit ledger across all pillars.
    """
    mode = (mode or getattr(pe_config, "FJ_SCORING_MODE", "additive") or "additive")
    mode = mode if mode in ("additive", "recompute") else "additive"

    res = ScoringResult(mode)
    builders = (
        _score_batch(batch, mode),
        _score_sla(sla, mode),
        _score_resource(resource, mode),
        _score_correlation(correlation, mode),
        _score_benchmark(benchmark, mode),
        _score_sow(sow, mode),
    )
    for ps in builders:
        if ps is None:
            continue
        res.scores[ps.name] = ps.score
        res.details[ps.name] = ps
        res.evidence_chain.extend(ps.evidence)
    return res


# ═════════════════════════════════════════════════════════════════════════════
# LAYER 3 — deterministic cross-pillar correlation
# ═════════════════════════════════════════════════════════════════════════════
def _batch_breaching(batch: Optional[Dict[str, Any]]) -> bool:
    if not batch:
        return False
    wc = _f(_first(_dig(batch, "window_day_compliance_pct"),
                   _dig(batch, "window_compliance_pct")))
    jb = _i(_dig(batch, "jobs_breach"))
    return (wc is not None and wc < 90.0) or bool(jb and jb > 0)


def compute_cross_pillar_links(
    resource: Optional[Dict[str, Any]] = None,
    batch: Optional[Dict[str, Any]] = None,
    sla: Optional[Dict[str, Any]] = None,
    benchmark: Optional[Dict[str, Any]] = None,
    sow: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Deterministic, CITED cross-pillar correlations.

    These are computed from measured numbers — not LLM-guessed — so they persist
    even when AI is off and the verdict_reconciler can defend them. Each link
    names the pillars it spans, a severity, the conclusion, and the evidence.
    """
    links: List[Dict[str, Any]] = []
    breaching = _batch_breaching(batch)

    # resource posture
    n_crit = _i(_first((resource or {}).get("n_critical"),
                       _dig(resource, "n_critical"))) or 0
    avg_cpu = _f(_first((resource or {}).get("avg_cpu"), _dig(resource, "avg_cpu"),
                        _dig(resource, "avg_cpu_pct")))
    peak_mem = _f(_first((resource or {}).get("peak_mem_pct"),
                         _dig(resource, "peak_mem_pct"), _dig(resource, "max_mem_pct")))
    saturated = (n_crit > 0
                 or (avg_cpu is not None and avg_cpu >= pe_config.CPU_WARN)
                 or (peak_mem is not None and peak_mem >= pe_config.MEM_WARN))

    # 1. batch breach × infrastructure saturation → capacity-bound
    if breaching and saturated:
        bits = []
        if avg_cpu is not None:
            bits.append(f"avg CPU {avg_cpu:.0f}%")
        if peak_mem is not None:
            bits.append(f"peak mem {peak_mem:.0f}%")
        if n_crit:
            bits.append(f"{n_crit} critical server(s)")
        links.append({
            "kind": "batch_x_resource_saturation",
            "severity": "HIGH",
            "pillars": ["batch", "resource"],
            "text": ("Batch SLA breaches coincide with infrastructure pressure ("
                     + ", ".join(bits) + ") — the batch is capacity-bound, "
                     "not purely a scheduling artefact."),
            "citation": "; ".join(bits) or "saturation flags set",
        })
    # 1b. batch breach DESPITE healthy infra → scheduling/dependency root cause
    elif breaching and resource and not saturated and (avg_cpu is not None or peak_mem is not None):
        bits = []
        if avg_cpu is not None:
            bits.append(f"avg CPU {avg_cpu:.0f}%")
        if peak_mem is not None:
            bits.append(f"peak mem {peak_mem:.0f}%")
        links.append({
            "kind": "batch_breach_healthy_infra",
            "severity": "MEDIUM",
            "pillars": ["batch", "resource"],
            "text": ("Batch breaches while infrastructure is healthy ("
                     + ", ".join(bits) + ") — root cause is scheduling/dependency "
                     "ordering, not capacity."),
            "citation": "; ".join(bits),
        })

    # 2. volume over SOW baseline × batch breach → contractual scaling pressure
    if breaching and sow:
        metrics = sow.get("metrics") or []
        over = [m for m in metrics
                if str(m.get("status")).upper() in ("EXCEEDED", "OVER", "CRITICAL", "BREACH")]
        if over:
            names = ", ".join(str(m.get("metric") or m.get("name") or "metric")
                              for m in over[:3])
            links.append({
                "kind": "volume_x_batch_breach",
                "severity": "HIGH",
                "pillars": ["sow", "batch"],
                "text": (f"Processing volume is over the SOW baseline ({names}) while "
                         f"batch is breaching — the scaling pressure is contractual, "
                         f"so capacity must grow with committed volume."),
                "citation": f"over_baseline_metrics={len(over)}",
            })

    # 3. benchmark regression × batch runtime regression → shared subsystem
    if benchmark and batch:
        deg = _i(benchmark.get("degraded")) or 0
        worst = _f(benchmark.get("worst_delta_pct"))
        rc = _i(_first(_dig(batch, "regression_count"),
                       _dig(batch, "runtime_regression_count"))) or 0
        if deg > 0 and rc > 0:
            wtxt = f", worst tx +{worst:.0f}%" if worst is not None else ""
            links.append({
                "kind": "benchmark_x_batch_regression",
                "severity": "MEDIUM",
                "pillars": ["benchmark", "batch"],
                "text": (f"UI/transaction regressions ({deg} degraded{wtxt}) and batch "
                         f"runtime regressions ({rc} job(s)) co-occur — points to a "
                         f"shared subsystem degradation (DB/app tier), not isolated faults."),
                "citation": f"degraded={deg}, batch_regressions={rc}",
            })

    return links


# ═════════════════════════════════════════════════════════════════════════════
# BATCH PANEL — one conclusive, contradiction-free verdict for the narrative
# ═════════════════════════════════════════════════════════════════════════════
def _tone_for_compliance(pct: Optional[float]) -> str:
    """Map a compliance % to a severity tone using the canonical grade floor."""
    if pct is None:
        return "warn"
    if pct >= 90.0:      # GRADE_TABLE A floor == APPROVED
        return "ok"
    if pct >= 70.0:      # C floor == CONDITIONAL HOLD
        return "warn"
    return "crit"


def build_batch_panel(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Build a conclusive, contradiction-free batch verdict panel from CANONICAL
    batch numbers (already resolved by the caller — single source of truth).

    The BINDING metric is window-day compliance: the share of calendar days the
    whole batch finished inside its SLA window. This is the SAME rule
    `_score_batch` uses, so this panel can never contradict the Final Judgment.
    Job-level compliance (each job under its own ceiling) is shown as a clearly
    labelled secondary — the two are reconciled in plain English so "100% job /
    7% window" reads as a story, not a contradiction.

    Input dict `m` (all keys optional / None-safe):
      window_pct, window_estimated, job_pct, total_days, breach_days,
      sla_breaches, exec_failures, fail_rate_pct, total_runs, total_jobs,
      reg_count, reg_jobs, reg_comparable, reg_improved, critical_findings,
      sla_limit_hrs

    Returns {verdict:{status,tone,headline}, kpis:[...], explainer, direction,
    binding_metric} — or None when there is not enough data to render a verdict.
    """
    window = _f(m.get("window_pct"))
    job    = _f(m.get("job_pct"))
    if window is None and job is None:
        return None

    est       = bool(m.get("window_estimated"))
    wtd       = _i(m.get("total_days"))
    wbd       = _i(m.get("breach_days"))
    clean     = (wtd - wbd) if (wtd is not None and wbd is not None) else None
    sla_brc   = _i(m.get("sla_breaches")) or 0
    failures  = _i(m.get("exec_failures")) or 0
    fail_rate = _f(m.get("fail_rate_pct"))
    crit_n    = _i(m.get("critical_findings")) or 0
    reg_count = _i(m.get("reg_count")) or 0
    reg_jobs  = _i(m.get("reg_jobs"))
    reg_comp  = _i(m.get("reg_comparable"))
    reg_impr  = _i(m.get("reg_improved"))

    # Binding metric: prefer the real window; fall back to job-day estimate.
    if window is not None and not est:
        binding = "window"
        prime   = window
    elif window is not None and est:
        binding = "job_estimate"
        prime   = window
    else:
        binding = "job_level"
        prime   = job

    job_healthy = job is not None and job >= 90.0

    # ── tone / status (window binds; critical findings always block) ──────────
    if (prime is not None and prime < 70.0) or crit_n > 0:
        tone, status = "crit", "NOT READY"
    elif (prime is not None and prime < 90.0) or sla_brc > 0 or reg_jobs or reg_count \
            or (fail_rate is not None and fail_rate > pe_config.BATCH_FAIL_RATE):
        tone, status = "warn", "AT RISK"
    else:
        tone, status = "ok", "READY"

    # ── headline (plain language, leads with the binding metric) ──────────────
    if binding == "window":
        if tone == "crit":
            if wtd:
                headline = (f"The batch missed its delivery window on {wbd} of {wtd} {_dys(wtd)} "
                            f"({window:.1f}%) and is not ready for PE sign-off.")
            else:
                headline = (f"Batch window compliance is {window:.1f}%, "
                            f"so the batch is not ready for PE sign-off.")
            if crit_n > 0 and (window is None or window >= 90.0):
                headline = (f"{crit_n} critical {('finding' if crit_n == 1 else 'findings')} block PE sign-off "
                            f"despite {window:.1f}% window compliance.")
        elif tone == "warn":
            _cl = clean if clean is not None else "—"
            _wt = wtd if wtd else "—"
            headline = (f"The batch made its delivery window on {_cl} of {_wt} {_dys(wtd)} "
                        f"({window:.1f}%); review the flagged risks before sign-off.")
        else:
            _wt = wtd if wtd else "every"
            headline = (f"The batch met its delivery window on all {_wt} {_dys(wtd)} "
                        f"({window:.1f}%) and is clear for PE sign-off.")
    elif binding == "job_estimate":
        _verdict = {"crit": "Not ready for PE sign-off.",
                    "warn": "Review the flagged risks before sign-off.",
                    "ok": "Clear for PE sign-off."}[tone]
        headline = (f"Batch SLA compliance (job-day estimate — no wall-clock window "
                    f"data) is {window:.1f}%. {_verdict}")
    else:
        _verdict = {"crit": "Not ready for PE sign-off.",
                    "warn": "Review the flagged risks before sign-off.",
                    "ok": "Clear for PE sign-off."}[tone]
        _jp = f"{job:.1f}%" if job is not None else "n/a"
        headline = f"Job-level SLA compliance is {_jp}. {_verdict}"

    # ── KPI strip (only tiles we actually have data for) ──────────────────────
    kpis: List[Dict[str, Any]] = []
    if window is not None:
        sub = (f"{clean} of {wtd} {_dys(wtd)} clean" if (clean is not None and wtd) else
               "wall-clock batch deadline")
        kpis.append({
            "label": "Window Compliance" + (" (est.)" if est else ""),
            "value": f"{window:.1f}%",
            "tone":  _tone_for_compliance(window),
            "sub":   sub,
            "binding": binding in ("window", "job_estimate"),
        })
    if job is not None:
        kpis.append({
            "label": "Job-Level Pass",
            "value": f"{job:.1f}%",
            "tone":  _tone_for_compliance(job),
            "sub":   "each job under its own ceiling",
        })
    kpis.append({
        "label": "SLA Breaches",
        "value": f"{sla_brc:,}",
        "tone":  "ok" if sla_brc == 0 else "crit",
        "sub":   "job-ceiling breaches",
    })
    if m.get("exec_failures") is not None:
        ftone = "ok" if failures == 0 else ("crit" if (fail_rate or 0) > pe_config.BATCH_FAIL_RATE else "warn")
        fsub  = (f"ENDED NOT OK ({fail_rate:.1f}%)" if fail_rate is not None
                 else "ENDED NOT OK runs")
        kpis.append({
            "label": "Execution Failures",
            "value": f"{failures:,}",
            "tone":  ftone,
            "sub":   fsub,
        })
    if reg_jobs is not None and reg_comp:
        kpis.append({
            "label": "Runtime Regressions",
            "value": f"{reg_jobs:,} / {reg_comp:,}",
            "tone":  "warn" if reg_jobs > 0 else "ok",
            "sub":   (f"{reg_impr:,} improved" if reg_impr is not None else "vs baseline"),
        })
    elif reg_count > 0:
        kpis.append({
            "label": "Runtime Regressions",
            "value": f"{reg_count:,}",
            "tone":  "warn",
            "sub":   "severe (>3σ vs baseline)",
        })
    kpis.append({
        "label": "Critical Findings",
        "value": f"{crit_n:,}",
        "tone":  "ok" if crit_n == 0 else "crit",
        "sub":   "block sign-off" if crit_n else "none open",
    })

    # ── explainer: reconcile window vs job-level so they read as one story ─────
    explainer = ""
    if est:
        explainer = (f"No wall-clock window data was available, so the figure above is a "
                     f"job-day estimate ({window:.1f}%). Upload batch data with End_Time "
                     f"to get the authoritative window-compliance number.")
    elif window is not None and job is not None and (job - window) > 1.0:
        _days = (f"on {wbd} of {wtd} {_dys(wtd)}" if (wbd and wtd) else "")
        explainer = (f"Window {window:.1f}% vs Job-level {job:.1f}%: every job finished under "
                     f"its own ceiling, but the batch's end-to-end run was longer than its "
                     f"SLA window {_days}. Window is the binding SLA for sign-off.")

    # ── direction: root-cause-aware next step ─────────────────────────────────
    if tone == "ok":
        direction = ("No action required — the batch is within both its delivery window "
                     "and every job ceiling. Cleared for sign-off.")
    else:
        if binding == "window" and (window is not None and window < 90.0) and job_healthy:
            root = ("Job ceilings are healthy, so the fix is batch ordering / start-time "
                    "scheduling, not per-job tuning.")
        elif job is not None and job < 90.0:
            root = ("Both individual job runtimes and the batch window are failing — tune "
                    "the slowest jobs and the batch schedule together.")
        else:
            root = "Address the flagged risks below."
        acts: List[str] = []
        if crit_n > 0:
            acts.append(f"resolve the {crit_n} critical {('finding' if crit_n == 1 else 'findings')}")
        _rn = reg_jobs if reg_jobs else reg_count
        if _rn:
            acts.append(f"investigate the {_rn} regressed job(s)")
        if wbd:
            acts.append(f"clear the sub-apps driving the {wbd} window miss(es)")
        elif failures:
            acts.append(f"triage the {failures} execution failure(s)")
        tail = ""
        if acts:
            if len(acts) == 1:
                tail = f" Next: {acts[0]} before sign-off."
            else:
                tail = f" Next: {', '.join(acts[:-1])}, and {acts[-1]} before sign-off."
        direction = root + tail

    return {
        "verdict": {"status": status, "tone": tone, "headline": headline},
        "kpis": kpis,
        "explainer": explainer,
        "direction": direction,
        "binding_metric": binding,
    }
