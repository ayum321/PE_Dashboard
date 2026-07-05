"""
Shared window compliance calculation AND ceiling-map construction.

Both batch_calculator.compute_metrics() and sla_matrix._compute_sla_matrix()
import from here.  No module builds its own ceiling map independently — the
numbers on the Batch Review tab and SLA Matrix tab are always identical for
the same data.

Formula (canonical):
    denominator = in-scope (Sub_Application, run_date) windows
                  (collapses to run_date alone when no Sub_Application is present)
    numerator   = windows where actual_window_hrs <= sla_ceiling_hrs
    compliance% = numerator / denominator × 100

Canonical window rule:
    elapsed window for a (sub_app, day) = max(end_time) - min(start_time)
    grouped by (Sub_Application, run_date), compared against THAT sub-app's
    resolved SLA ceiling. A long cyclic/weekly sub-app never drags a daily
    sub-app's compliance down, and excluded schedule types (CYCLIC, OUTBOUND,
    ADHOC, …) are dropped from the denominator entirely.

Ceiling map resolution (canonical, highest → lowest priority):
    1. XLSX workflow SLA  — fuzzy substring match against _batch_sla_xlsx workflows
    2. Schedule-type      — classify_schedule(sub_app) → DAILY/WEEKLY pe_config hours
    3. DAILY default      — pe_config.SLA_DAILY_HRS
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

# ── Generic name-matching primitives ─────────────────────────────────────────
# Customer SLA workbooks name workflows differently from the Ctrl-M
# Sub_Application that actually runs them.  e.g. the contract row "BY WEEKLY"
# governs the Ctrl-M sub-app "TEST_2025_WEEKLY".  Neither string is a substring
# of the other, but they share the cadence token WEEKLY.  Pure substring
# matching silently misses every such pair and the job is judged against a
# wrong default ceiling — manufacturing false breaches.  We resolve the pairing
# on shared *signal tokens* instead, which is naming-convention agnostic and
# therefore works across the whole customer base.

# Environment / connector tokens that carry no cadence identity.
_NOISE_TOKENS: Set[str] = {
    "TEST", "PROD", "PRD", "UAT", "DEV", "STG", "STAGE", "STAGING", "QA",
    "SIT", "PERF", "PRE", "PREPROD", "NONPROD", "TRAIN", "DR",
    "BY", "THE", "OF", "AND", "FOR", "TO", "ON", "AT", "A", "AN",
}

# Day-of-week variants → canonical 3-letter token so "TUESDAY" matches "TUE".
_DAY_CANON: Dict[str, str] = {
    "MONDAY": "MON", "MON": "MON",
    "TUESDAY": "TUE", "TUES": "TUE", "TUE": "TUE", "TUS": "TUE",
    "WEDNESDAY": "WED", "WED": "WED", "WEDS": "WED",
    "THURSDAY": "THU", "THURS": "THU", "THUR": "THU", "THU": "THU", "THURSDY": "THU",
    "FRIDAY": "FRI", "FRI": "FRI",
    "SATURDAY": "SAT", "SAT": "SAT",
    "SUNDAY": "SUN", "SUN": "SUN",
}

_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")  # 1900-2099 — calendar years, not identity


def _signal_tokens(name: str) -> Set[str]:
    """Tokenise a workflow / sub-application name into cadence-identity tokens.

    Splits on every non-alphanumeric boundary, upper-cases, then drops
    environment prefixes, connector words and calendar years, and canonicalises
    day-of-week names.  What remains is the set of tokens that actually identify
    *which* batch this is (DAILY, WEEKLY, MONTHLY, SEQ, OUTBOUND, TUE, …).
    """
    out: Set[str] = set()
    for tok in re.split(r"[^A-Za-z0-9]+", str(name).upper()):
        if not tok or len(tok) <= 1:
            continue
        if _YEAR_RE.match(tok):
            continue
        if tok in _NOISE_TOKENS:
            continue
        out.add(_DAY_CANON.get(tok, tok))
    return out


def _token_match_score(sa_tok: Set[str], pat_tok: Set[str]) -> float:
    """Score how well an XLSX workflow pattern governs a sub-application.

    Dual-containment: sub-app coverage (how much of the sub-app's identity the
    pattern explains) plus pattern coverage (how fully the pattern is itself
    matched).  Summing both rewards the *most specific* governing contract:
    "BY SEQ WEEKLY" beats "BY WEEKLY" for the sub-app "…_SEQ_WEEKLY" because it
    covers more of the sub-app, while "BY WEEKLY" beats "BY SEQ WEEKLY" for a
    plain "…_WEEKLY" sub-app because the pattern itself is fully matched.
    Day-list noise in the pattern ("BY SEQ DAILY (MON,TUE,…)") cannot drown the
    cadence tokens, unlike a raw Jaccard.  Range 0.0 – 2.0.
    """
    inter = sa_tok & pat_tok
    if not inter:
        return 0.0
    sa_cov  = len(inter) / len(sa_tok)  if sa_tok  else 0.0
    pat_cov = len(inter) / len(pat_tok) if pat_tok else 0.0
    return sa_cov + pat_cov


# Minimum dual-containment score to accept a token match. 0.5 demands at least
# one side meaningfully covered (e.g. one shared token that is the sub-app's
# whole identity, or a strong partial overlap) before overriding the
# schedule-type default — weak incidental overlaps fall through safely.
_TOKEN_MATCH_THRESHOLD = 0.5


def compute_window_compliance(
    window_records: List[Dict[str, Any]],
    ceiling_map: Dict[str, Any],
    excluded_types: Optional[set] = None,
    debug: bool = False,
) -> Dict[str, Any]:
    """Compute canonical daily batch-window compliance.

    Parameters
    ----------
    window_records:
        List of dicts, each representing one day in the file.
        Required keys: ``run_date`` plus either ``elapsed_hrs`` or
        ``total_hrs``. Optional keys: ``schedule_type``, ``sla_ceil``,
        ``breach`` and ``sentinel_source``.
    ceiling_map:
        Dict mapping sub_app name → SLA ceiling in hours (or None = excluded).
        Produced by batch_calculator._build_sla_ceiling_map().
    excluded_types:
        Set of schedule type strings that are NEVER counted in the denominator.
        Defaults to pe_config.COMPLIANCE_EXCLUDED_TYPES when None.
    debug:
        When True, logs every (sub_app, date) decision — INCLUDED (with its
        resolved ceiling + breach verdict) or EXCLUDED (with its schedule type)
        — to the "compliance_engine" logger at INFO level. This is the direct
        answer to "is OUTBOUND/SEQ_DAILY silently dropped": turn it on for one
        request and the log shows every row's fate. Off by default (audit-size
        logs on every batch upload would be noise) — enable via
        pe_config.COMPLIANCE_DEBUG_LOG=True or by passing debug=True directly.
        Regardless of this flag, a compact ``audit_windows`` manifest (one row
        per scored (sub_app, date) with its ceiling/effective/breach) is always
        returned so the same trace is inspectable programmatically without logs.

    Returns
    -------
    dict with keys:
        compliance_pct   float — 0-100
        breach_count     int
        ok_count         int
        at_risk_count    int   — elapsed within 15% of ceiling
        total_windows    int   — denominator
        excluded_windows int   — rows skipped (CYCLIC, ADHOC, etc.)
        warnings         list[str] — data-shape guardrails
    """
    import logging
    _log = logging.getLogger("compliance_engine")
    try:
        from services import pe_config as _pc
        _excluded = excluded_types if excluded_types is not None else _pc.COMPLIANCE_EXCLUDED_TYPES
        _atrisk_pct = _pc.SLA_ATRISK_PCT
        _daily_default = _pc.SLA_DAILY_HRS
        _structural_ratio = getattr(_pc, "SLA_STRUCTURAL_RATIO", 0.60)
        if not debug:
            debug = bool(getattr(_pc, "COMPLIANCE_DEBUG_LOG", False))
    except Exception:
        _excluded = {"CYCLIC", "CYCLIC_INTERVAL", "ADHOC", "CALENDAR_BASED",
                     "OUTBOUND", "PIPELINE_STAGE", "MONTHLY", "BIMONTHLY",
                     "QUARTERLY", "ANNUAL"}
        _atrisk_pct = 15.0
        _daily_default = 6.0
        _structural_ratio = 0.60

    daily: Dict[Any, Dict[str, Any]] = {}
    warnings: List[str] = []
    excluded_windows = 0
    # Exclusion transparency: which sub_apps were dropped from the denominator and
    # why. A structural breacher classified OUTBOUND/CYCLIC/etc. silently leaves the
    # all-pass count, so the consumer must be able to name them (e.g. "<SUB_APP> 23.7h
    # excluded as OUTBOUND") instead of presenting an opaque pass count.
    _excluded_detail: Dict[str, Dict[str, Any]] = {}
    # Full (sub_app, date) audit trail — every row's fate, always populated (cheap:
    # one dict per row) so "why is the headline number X" is answerable without
    # re-running with debug logging on.
    audit_windows: List[Dict[str, Any]] = []

    for rec in window_records:
        date_str = str(rec.get("run_date") or rec.get("date") or "").strip()
        if not date_str:
            warnings.append("Skipped a window record without run_date.")
            continue

        sched = str(rec.get("schedule_type") or "").upper()

        # Per-row effective duration + sub_app (computed up-front so excluded rows
        # can still be attributed by name/hours below, not just counted).
        _eff = rec.get("effective_hrs")
        if _eff is None:
            _eff = rec.get("elapsed_hrs")
        if _eff is None:
            _eff = rec.get("total_hrs")
        elapsed = float(_eff or 0.0)
        sub_app = str(rec.get("sub_app") or rec.get("Sub_Application") or "").strip()

        # Skip CYCLIC/ADHOC/excluded schedule types.
        if sched in _excluded:
            excluded_windows += 1
            _ek = sub_app.upper() or "(unnamed)"
            _ed = _excluded_detail.get(_ek)
            if _ed is None:
                _ed = _excluded_detail[_ek] = {
                    "sub_app": sub_app or _ek, "schedule_type": sched,
                    "windows": 0, "worst_hrs": 0.0,
                }
            _ed["windows"] += 1
            if elapsed > _ed["worst_hrs"]:
                _ed["worst_hrs"] = round(elapsed, 3)
            audit_windows.append({
                "sub_app": sub_app or _ek, "run_date": date_str,
                "schedule_type": sched, "effective_hrs": round(elapsed, 3),
                "included": False, "reason": f"excluded_type:{sched}",
            })
            if debug:
                _log.info(
                    "EXCLUDED sub_app=%s date=%s schedule=%s effective=%.2fh "
                    "(schedule type is in COMPLIANCE_EXCLUDED_TYPES)",
                    sub_app or _ek, date_str, sched, elapsed,
                )
            continue

        # effective duration + sub_app already computed up-front above.
        ceil = rec.get("sla_ceil")
        if ceil is None:
            ceil = ceiling_map.get(sub_app.upper())
        if ceil is None:
            ceil = _daily_default
        try:
            ceil_f = float(ceil)
        except (TypeError, ValueError):
            ceil_f = 0.0

        if ceil_f <= 0:
            excluded_windows += 1
            warnings.append(f"Skipped {date_str}: no usable SLA ceiling.")
            audit_windows.append({
                "sub_app": sub_app, "run_date": date_str, "schedule_type": sched,
                "effective_hrs": round(elapsed, 3), "included": False,
                "reason": "no_usable_ceiling",
            })
            if debug:
                _log.info(
                    "EXCLUDED sub_app=%s date=%s schedule=%s (no usable SLA ceiling resolved)",
                    sub_app, date_str, sched,
                )
            continue

        breach = rec.get("breach")
        if breach is None:
            breach = elapsed > ceil_f
        breach = bool(breach)

        if debug:
            _log.info(
                "INCLUDED sub_app=%s date=%s schedule=%s effective=%.2fh ceiling=%.2fh "
                "breach=%s",
                sub_app, date_str, sched, elapsed, ceil_f, breach,
            )
        audit_windows.append({
            "sub_app": sub_app, "run_date": date_str, "schedule_type": sched,
            "effective_hrs": round(elapsed, 3), "ceiling_hrs": round(ceil_f, 3),
            "breach": breach, "included": True,
        })

        # ── Window unit key ──────────────────────────────────────────────
        # When a Sub_Application is present, each (sub_app, date) is its OWN
        # window judged against its OWN resolved ceiling — a long cyclic/weekly
        # sub-app must never drag a daily sub-app's compliance down (and vice
        # versa). When no sub_app is present (legacy per-date records), the key
        # collapses to date alone, preserving the original per-day behaviour.
        unit_key = (sub_app.upper(), date_str) if sub_app else ("", date_str)

        current = daily.get(unit_key)
        if current is None:
            daily[unit_key] = {
                "run_date": date_str,
                "sub_app": sub_app,
                "elapsed_hrs": elapsed,
                "sla_ceil": ceil_f,
                "breach": breach,
                "schedule_type": sched,
                "source_count": 1,
            }
        else:
            current["source_count"] += 1
            if elapsed >= float(current.get("elapsed_hrs") or 0.0):
                current["elapsed_hrs"] = elapsed
                current["sla_ceil"] = ceil_f
                current["breach"] = breach
                current["schedule_type"] = sched or str(current.get("schedule_type") or "")
            else:
                current["breach"] = bool(current.get("breach")) or breach

    duplicate_days = [
        f"{row.get('sub_app') or '?'}@{row.get('run_date')}"
        for row in daily.values() if int(row.get("source_count", 0)) > 1
    ]
    if duplicate_days:
        warnings.append(
            "Duplicate (sub_app, run_date) records were collapsed into one window: "
            + ", ".join(sorted(duplicate_days)[:8])
        )

    total_windows = len(daily)
    breach_count = sum(1 for row in daily.values() if bool(row.get("breach")))
    ok_count = 0
    at_risk_count = 0
    for row in daily.values():
        if row.get("breach"):
            continue
        ceil_f = float(row.get("sla_ceil") or 0.0)
        elapsed = float(row.get("elapsed_hrs") or 0.0)
        if ceil_f > 0 and ((ceil_f - elapsed) / ceil_f * 100) <= _atrisk_pct:
            at_risk_count += 1
        else:
            ok_count += 1

    compliance_pct = round((ok_count + at_risk_count) / total_windows * 100, 1) if total_windows > 0 else 0.0
    # Zero scorable windows ≠ 0% compliant. Every sub-app excluded (all OUTBOUND/
    # CYCLIC/MONTHLY) is a "nothing to judge" state, not total failure — surface it
    # so the headline can say "no scorable windows" instead of a misleading 0%.
    if total_windows == 0 and excluded_windows > 0:
        warnings.append(
            f"No scorable windows: all {excluded_windows} window(s) are excluded "
            f"schedule types (OUTBOUND/CYCLIC/MONTHLY/etc.) — compliance N/A, not 0%."
        )

    # Distinct-day rollups so the UI can still show an honest "X / Y days"
    # alongside the per-window compliance denominator.
    distinct_days = {row.get("run_date") for row in daily.values()}
    breach_day_set = {row.get("run_date") for row in daily.values() if bool(row.get("breach"))}
    total_days = len(distinct_days)
    breach_days = len(breach_day_set)

    # ── Per-sub-app window rollup ────────────────────────────────────────────
    # The Executive "at-risk" panels must reflect the SAME window reality as the
    # headline compliance — the contracted ceiling governs the daily batch
    # WINDOW (first-start → last-end), NOT a single job's runtime. Comparing a
    # 0.6h job peak against a 6h window ceiling makes a breached batch look "98%
    # within SLA", which directly contradicts the decision gate. This rollup
    # gives every consumer the worst daily window, its ceiling, and the breach
    # days per sub-app so the panels can show the binding verdict.
    _sa_roll: Dict[str, Dict[str, Any]] = {}
    for row in daily.values():
        sa = str(row.get("sub_app") or "").strip()
        key = sa.upper()
        rec = _sa_roll.get(key)
        if rec is None:
            rec = _sa_roll[key] = {
                "sub_app": sa or key, "total_windows": 0, "breach_windows": 0,
                "at_risk_windows": 0, "ok_windows": 0, "worst_window_hrs": 0.0,
                "ceiling": 0.0, "schedule_type": row.get("schedule_type") or "",
                "_breach_days": set(),
            }
        elapsed = float(row.get("elapsed_hrs") or 0.0)
        ceil_f = float(row.get("sla_ceil") or 0.0)
        rec["total_windows"] += 1
        if ceil_f > 0:
            rec["ceiling"] = ceil_f
        if elapsed > rec["worst_window_hrs"]:
            rec["worst_window_hrs"] = elapsed
        if bool(row.get("breach")):
            rec["breach_windows"] += 1
            rec["_breach_days"].add(row.get("run_date"))
        elif ceil_f > 0 and ((ceil_f - elapsed) / ceil_f * 100) <= _atrisk_pct:
            rec["at_risk_windows"] += 1
        else:
            rec["ok_windows"] += 1

    per_sub_app: List[Dict[str, Any]] = []
    for rec in _sa_roll.values():
        tw = rec["total_windows"]
        cl = rec["ceiling"]
        ww = rec["worst_window_hrs"]
        comp = round((rec["ok_windows"] + rec["at_risk_windows"]) / tw * 100, 1) if tw else 0.0
        buf = round((cl - ww) / cl * 100, 1) if cl > 0 else None
        if rec["breach_windows"] > 0:
            status = "BREACH"
        elif buf is not None and buf <= _atrisk_pct:
            status = "AT_RISK"
        else:
            status = "OK"
        # Window severity ratio (worst window ÷ ceiling): >1 = over the window,
        # mirrors the SRI 0–1+ scale so the bubble/treemap colour by real breach.
        severity = round(ww / cl, 3) if cl > 0 else 0.0
        # Breach PATTERN — makes the headline breach count diagnosable without a
        # second screen. STRUCTURAL = breaches on most days it ran (≥ the config
        # ratio): a standing capacity/contract problem. INTERMITTENT = occasional
        # spikes (a performance regression). CLEAN = never breached. These drive
        # completely different remediation paths, so the verdict names the pattern,
        # not just the count. Ratio is config-driven (pe_config.SLA_STRUCTURAL_RATIO),
        # never a per-customer constant.
        bdays = len(rec["_breach_days"])
        if bdays <= 0:
            pattern = "clean"
        elif tw > 0 and (bdays / tw) >= _structural_ratio:
            pattern = "structural"
        else:
            pattern = "intermittent"
        per_sub_app.append({
            "sub_app":          rec["sub_app"],
            "schedule_type":    rec["schedule_type"],
            "total_windows":    tw,
            "breach_windows":   rec["breach_windows"],
            "breach_days":      bdays,
            "ok_windows":       rec["ok_windows"],
            "at_risk_windows":  rec["at_risk_windows"],
            "worst_window_hrs": round(ww, 3),
            "ceiling":          round(cl, 3),
            "buffer_pct":       buf,
            "compliance_pct":   comp,
            "severity":         severity,
            "status":           status,
            "pattern":          pattern,
        })
    per_sub_app.sort(key=lambda r: (-r["breach_windows"], -(r["severity"] or 0)))

    # ── Per-breach-day attribution ───────────────────────────────────────────
    # "11 breach days" is unauditable on its own — a reader can't tell if one
    # structural sub-app failed every day or several spiked once. For each breach
    # DATE, name the breaching sub-apps with their judged hours, own ceiling and
    # overrun, so the count traces straight to a cause. Fully generic: derived
    # from the same per-(sub_app, date) windows the compliance % is built on.
    _bd_attr: Dict[str, List[Dict[str, Any]]] = {}
    for row in daily.values():
        if not bool(row.get("breach")):
            continue
        d = str(row.get("run_date") or "")
        cl = float(row.get("sla_ceil") or 0.0)
        eh = float(row.get("elapsed_hrs") or 0.0)
        _bd_attr.setdefault(d, []).append({
            "sub_app":      str(row.get("sub_app") or ""),
            "effective_hrs": round(eh, 3),
            "ceiling":      round(cl, 3),
            "overrun_hrs":  round(eh - cl, 3) if cl > 0 else None,
            "overrun_pct":  round((eh - cl) / cl * 100, 1) if cl > 0 else None,
        })
    breach_days_detail = [
        {"run_date": d, "breachers": sorted(v, key=lambda b: -(b.get("overrun_hrs") or 0))}
        for d, v in sorted(_bd_attr.items())
    ]

    # Excluded sub-apps (worst-first) so the denominator is never opaque.
    excluded_sub_apps = sorted(
        _excluded_detail.values(),
        key=lambda e: -float(e.get("worst_hrs") or 0.0),
    )

    return {
        "compliance_pct":   compliance_pct,
        "breach_count":     breach_count,
        "ok_count":         ok_count,
        "at_risk_count":    at_risk_count,
        "total_windows":    total_windows,
        "excluded_windows": excluded_windows,
        "total_days":       total_days,
        "breach_days":      breach_days,
        "breach_day_list":  sorted(str(d) for d in breach_day_set if d),
        "breach_days_detail": breach_days_detail,
        "excluded_sub_apps":  excluded_sub_apps,
        "per_sub_app":      per_sub_app,
        # The classification cut-off (config-driven, not customer data) surfaced so
        # the consumer can label "structural (≥N% of run-days)" — a PE lead can then
        # agree/disagree without reading source. Pattern is interpretation, never a
        # silent rule; we publish the rule with it.
        "structural_ratio": _structural_ratio,
        "warnings":         warnings,
        # Full row-level trace: one entry per (sub_app, date) with its verdict —
        # answers "is X silently excluded / what ceiling did Y get judged against"
        # without re-running with debug=True. See docstring.
        "audit_windows":    audit_windows,
    }


def build_ceiling_map_detailed(
    sub_applications: List[str],
    xlsx_config: Optional[Dict[str, Any]] = None,
    pe_config_ref=None,
) -> Dict[str, Dict[str, Any]]:
    """Build a {sub_app_upper: provenance dict} ceiling map.

    Identical resolution to build_ceiling_map() but returns the *provenance* of
    every match so callers can label each sub-app honestly (was the ceiling
    sourced from the customer SLA matrix, or assumed from a schedule default?).
    build_ceiling_map() is a thin projection of this — both share one matcher,
    so the per-job provenance panel and the compliance math never diverge.

    Returns
    -------
    Dict mapping sub_app (UPPER) → {
        "sla_hrs":         float,
        "source":          "sla_matrix" | "default",
        "match_type":      "token" | "substring" | "schedule_default",
        "matched_pattern": Optional[str],   # the customer workflow row that won
        "score":           float,           # dual-containment token score (0 if N/A)
        "schedule_type":   str,             # classify_schedule(sub_app)
    }
    """
    if pe_config_ref is None:
        try:
            from services import pe_config as pe_config_ref  # type: ignore[assignment]
        except Exception:
            pe_config_ref = None  # type: ignore[assignment]

    try:
        from services.sla_engine import classify_schedule as _classify
    except Exception:
        _classify = None  # type: ignore[assignment]

    def _sched_type(sub_app: str) -> str:
        if _classify is None:
            return "DAILY"
        try:
            return _classify(sub_app) or "DAILY"
        except Exception:
            return "DAILY"

    # Safe schedule-type → hours lookup
    def _sched_hrs(stype: str) -> float:
        defaults: Dict[str, float] = {
            "DAILY":         getattr(pe_config_ref, "SLA_DAILY_HRS",   6.0),
            "WEEKLY":        getattr(pe_config_ref, "SLA_WEEKLY_HRS",  8.0),
            "TWICE_DAILY":   getattr(pe_config_ref, "SLA_DAILY_HRS",   6.0),
            "BIWEEKLY":      getattr(pe_config_ref, "SLA_BIWEEKLY_HRS", 8.0),
            "MONTHLY":       getattr(pe_config_ref, "SLA_MONTHLY_HRS", 24.0),
            "SEQUENCING":    getattr(pe_config_ref, "SLA_DAILY_HRS",   3.0),  # shorter window
        }
        return defaults.get(stype, getattr(pe_config_ref, "SLA_DAILY_HRS", 6.0))

    # Step 1 — build XLSX pattern → sla_hrs lookup (with precomputed signal tokens)
    _xlsx_pairs: List[Tuple[str, float, Set[str]]] = []   # [(pattern_upper, sla_hrs, tokens)]
    if xlsx_config:
        for wf in xlsx_config.get("workflows") or []:
            # Accept all known field-name variants from parse_batch_sla_xlsx()
            pat = str(
                wf.get("workflow") or wf.get("sub_app_pattern") or ""
            ).upper().strip()
            sla_h = float(
                wf.get("sla_hours") or wf.get("window_sla_hrs") or wf.get("sla_hrs") or 0
            )
            if pat and sla_h > 0:
                _xlsx_pairs.append((pat, sla_h, _signal_tokens(pat)))

    detail_map: Dict[str, Dict[str, Any]] = {}
    for sa in sub_applications:
        sa_upper = str(sa).upper()
        sa_tok   = _signal_tokens(sa_upper)
        stype    = _sched_type(sa)

        # Priority 1: token-overlap match against XLSX workflow patterns.
        # Picks the most specific governing contract via dual-containment score,
        # so cadence names ("BY WEEKLY") resolve to their Ctrl-M sub-app
        # ("TEST_2025_WEEKLY") even when neither is a substring of the other.
        best_score = 0.0
        best_sla: Optional[float] = None
        best_pat: Optional[str] = None
        for pat, sla_h, pat_tok in _xlsx_pairs:
            score = _token_match_score(sa_tok, pat_tok)
            if score < _TOKEN_MATCH_THRESHOLD:
                continue
            # Higher score wins; tie-break on the tighter (smaller) contract
            # window — the binding SLA when two contracts match equally well.
            if best_sla is None or score > best_score or (score == best_score and sla_h < best_sla):
                best_score = score
                best_sla   = sla_h
                best_pat   = pat

        if best_sla is not None:
            detail_map[sa_upper] = {
                "sla_hrs":         best_sla,
                "source":          "sla_matrix",
                "match_type":      "token",
                "matched_pattern": best_pat,
                "score":           round(best_score, 3),
                "schedule_type":   stype,
            }
            continue

        # Priority 1b: legacy substring match — covers opaque codes with no
        # clean alpha tokens (e.g. "EDI852") where tokenisation can't help.
        matched: Optional[float] = None
        matched_pat: Optional[str] = None
        for pat, sla_h, _pt in _xlsx_pairs:
            if pat in sa_upper or sa_upper in pat:
                matched = sla_h
                matched_pat = pat
                break
        if matched is not None:
            detail_map[sa_upper] = {
                "sla_hrs":         matched,
                "source":          "sla_matrix",
                "match_type":      "substring",
                "matched_pattern": matched_pat,
                "score":           0.0,
                "schedule_type":   stype,
            }
        else:
            # Priority 2 / 3: schedule-type default
            detail_map[sa_upper] = {
                "sla_hrs":         _sched_hrs(stype),
                "source":          "default",
                "match_type":      "schedule_default",
                "matched_pattern": None,
                "score":           0.0,
                "schedule_type":   stype,
            }

    return detail_map


def build_ceiling_map(
    sub_applications: List[str],
    xlsx_config: Optional[Dict[str, Any]] = None,
    pe_config_ref=None,
) -> Dict[str, float]:
    """Build a {sub_app_upper: sla_hrs} ceiling map.

    Single source of truth used by both batch_calculator.compute_metrics()
    and sla_matrix._compute_sla_matrix() so the two tabs never diverge.

    Resolution priority (highest → lowest):
        1. XLSX workflow SLA — fuzzy substring match against _batch_sla_xlsx workflows
        2. Schedule-type default — classify_schedule() → DAILY/WEEKLY pe_config hours
        3. DAILY default from pe_config

    Parameters
    ----------
    sub_applications:
        List of unique Sub_Application values from the Ctrl-M DataFrame.
    xlsx_config:
        Parsed _batch_sla_xlsx dict (from config_store). May be None when no
        BatchSLA XLSX has been uploaded — falls back to schedule-type defaults.
    pe_config_ref:
        Reference to services.pe_config module. If None, it is imported lazily.
        Passed explicitly so callers can inject a reloaded instance.

    Returns
    -------
    Dict mapping sub_app (UPPER) → contracted SLA hours (float).
    """
    detail = build_ceiling_map_detailed(sub_applications, xlsx_config, pe_config_ref)
    return {sa: d["sla_hrs"] for sa, d in detail.items()}
