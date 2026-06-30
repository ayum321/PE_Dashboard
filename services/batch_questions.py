"""
Dynamic PE batch-review question generator.

Post Ctrl-M upload, the same audit must read very differently across 250+
customers — a fresh go-live, a hyper-care window, a version upgrade, a steady
production account.  This module turns whatever the batch data actually shows
into plain-English, evidence-cited consultative questions a PE lead can put
straight to the customer.

Design rules
------------
* No fabrication.  Every question names a real job / number / date that exists
  in the uploaded data; if the evidence is absent, the question is not emitted.
* Grammatical, conclusive English.  Each item states the observation (with
  numbers) then asks the specific "why / confirm" question and what it implies.
* Reactive, not a fixed checklist.  The set of questions is a function of the
  data — different uploads yield a different (and differently ordered) bank, so
  the practical space of outputs is effectively combinatorial ("N!").

The generator is deterministic (no LLM) so it is fast, auditable and identical
on every run for the same input.  An optional AI pass elsewhere may re-word the
prose, but the facts and the verdict direction come from here.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from services.pe_utils import coerce_float as _f, coerce_int as _i

# Severity tiers, ordered so callers can sort/cap consistently.
SEV_RANK = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}

# How many questions to emit per category before they become noise.
_MAX_PER_CATEGORY = 6

_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")


# ── value humanisers ──────────────────────────────────────────────────────────

def _humanize_secs(secs: float) -> str:
    """9 → '9 seconds', 900 → '15 minutes', 5400 → '1.5 hours'."""
    s = _f(secs)
    if s < 1:
        return f"{s:.1f} seconds"
    if s < 90:
        return f"{int(round(s))} seconds"
    if s < 5400:  # under 90 min → minutes
        m = s / 60.0
        return f"{int(round(m))} minutes" if m >= 10 else f"{m:.1f} minutes"
    h = s / 3600.0
    return f"{h:.1f} hours"


def _humanize_hrs(hrs: float) -> str:
    """0.63 → '38 minutes', 1.0 → '1 hour', 18.33 → '18.3 hours'."""
    h = _f(hrs)
    if h <= 0:
        return "0 minutes"
    if h < 1:
        m = h * 60.0
        return f"{int(round(m))} minutes"
    if h < 2:
        return f"{h:.1f} hours" if abs(h - 1.0) > 0.05 else "1 hour"
    return f"{h:.1f} hours"


def _factor(old: float, new: float) -> str:
    """Human slowdown/speedup factor: 9→900 → '100x slower'."""
    o, n = _f(old), _f(new)
    if o <= 0 or n <= 0:
        return ""
    if n >= o:
        f = n / o
        return f"{f:.0f}x slower" if f >= 2 else f"{(f - 1) * 100:.0f}% slower"
    f = o / n
    return f"{f:.0f}x faster" if f >= 2 else f"{(1 - n / o) * 100:.0f}% faster"


def _fmt_date(d: Any) -> str:
    """'2026-06-13' → 'June 13'.  Falls back to the raw string."""
    s = str(d or "").strip()
    parts = s.split("-")
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        mi = _i(parts[1])
        if 1 <= mi <= 12:
            return f"{_MONTHS[mi - 1]} {_i(parts[2])}"
    return s or "an unknown date"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _base_job(name: str) -> str:
    """Strip an env prefix (PROD_/TEST_/UAT_/…) for module inference only."""
    import re
    return re.sub(r"^(?:PROD|TEST|UAT|DEV|STG|SIT|QA)[_\-]+", "", str(name or ""), flags=re.I)


def _infer_module(job: str) -> str:
    """Best-effort module name from the job's verb/noun, for failure questions."""
    base = _base_job(job).upper()
    for key, label in (
        ("OUTBOUND", "outbound"), ("INBOUND", "inbound"), ("EXTRACT", "extract"),
        ("ARCHIVE", "archive"), ("CALCPLAN", "planning"), ("PLAN", "planning"),
        ("REPLEN", "replenishment"), ("FORECAST", "forecast"), ("ALLOC", "allocation"),
        ("LOAD", "load"), ("REPORT", "reporting"), ("INTERFACE", "interface"),
    ):
        if key in base:
            return label
    return ""


def _is_file_watcher(job: str) -> bool:
    b = _base_job(job).upper().replace("_", "")
    return "FILEWATCHER" in b or "WATCHER" in b or "FILEWATCH" in b


# ── question container ────────────────────────────────────────────────────────

def _q(category: str, severity: str, observation: str, question: str,
       evidence: str, root_cause: str = "") -> Dict[str, str]:
    return {
        "category":    category,
        "severity":    severity if severity in SEV_RANK else "MEDIUM",
        "observation": observation.strip(),
        "question":    question.strip(),
        "evidence":    evidence.strip(),
        "root_cause":  root_cause,
    }


# ── lifecycle framing ─────────────────────────────────────────────────────────

def _lifecycle_tail(lifecycle: str) -> str:
    """Closing clause tuned to where the customer is in their journey."""
    lc = (lifecycle or "").lower()
    if "hypercare" in lc or "hyper-care" in lc:
        return "before this exits hyper-care"
    if "upgrade" in lc or "migrat" in lc:
        return "before the upgrade is signed off"
    if "golive" in lc or "go-live" in lc or "go live" in lc:
        return "before production go-live"
    return "before PE sign-off"


# ── category 1: runtime & regression ──────────────────────────────────────────

def _runtime_questions(ctx: Dict[str, Any], tail: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    bench = ctx.get("benchmark") or {}
    perf  = bench.get("batch_perf_summary") or bench.get("perf_summary") or bench
    regr  = perf.get("top_regressions") or perf.get("projectable_regressions") or []
    regr  = [r for r in regr if isinstance(r, dict)]

    # Per-job old→new regressions — the headline "9 seconds to 15 minutes" case.
    shown = 0
    for r in regr:
        old = _f(r.get("old_secs") or r.get("baseline_sec"))
        new = _f(r.get("new_secs") or r.get("current_sec"))
        job = r.get("job") or r.get("transaction") or r.get("Job_Name") or "?"
        if old <= 0 or new <= 0 or new <= old:
            continue
        fac = _factor(old, new)
        sev = "CRITICAL" if (new / old) >= 5 else "HIGH"
        out.append(_q(
            "Runtime & Regression", sev,
            f"{job} went from {_humanize_secs(old)} to {_humanize_secs(new)} on the new "
            f"release — a {fac} regression.",
            f"What caused this slowdown, and has a root-cause analysis been completed {tail}?",
            f"{job}: {_humanize_secs(old)} → {_humanize_secs(new)} ({fac})",
            root_cause="BATCH_RUNTIME_REGRESSION",
        ))
        shown += 1
        if shown >= 3:
            break

    # Name the next couple of regressed jobs together (remediation backlog).
    rest = [r for r in regr
            if (r.get("job") or r.get("transaction") or r.get("Job_Name"))
            and _f(r.get("new_secs") or r.get("current_sec")) > _f(r.get("old_secs") or r.get("baseline_sec")) > 0]
    if len(rest) > shown:
        extra = rest[shown:shown + 2]
        names = " and ".join(str(r.get("job") or r.get("transaction") or r.get("Job_Name")) for r in extra)
        if names:
            out.append(_q(
                "Runtime & Regression", "HIGH",
                f"{names} also regressed on the new release.",
                f"Are these on the remediation backlog to be cleared {tail}?",
                f"{names} — secondary regressions",
                root_cause="BATCH_RUNTIME_REGRESSION",
            ))

    # Aggregate "N of M regressed" question.
    n_regr = _i(perf.get("regressions"))
    n_comp = _i(perf.get("comparable") or perf.get("total") or perf.get("compared"))
    if n_regr > 0 and n_comp > 0:
        out.append(_q(
            "Runtime & Regression", "HIGH" if n_regr / max(n_comp, 1) >= 0.1 else "MEDIUM",
            f"{n_regr} of {n_comp} compared jobs ran slower on the new release.",
            "Is there a documented plan to investigate every regression, or only the "
            "critical-path jobs?",
            f"{n_regr}/{n_comp} jobs regressed",
            root_cause="BATCH_RUNTIME_REGRESSION",
        ))

    # When there is no benchmark, fall back to statistical outliers vs own baseline.
    if not regr:
        anomalies = [a for a in (ctx.get("anomalies") or []) if isinstance(a, dict)]
        outliers = sorted(
            (a for a in anomalies if _f(a.get("z_score") or a.get("zscore")) >= 2.0),
            key=lambda a: _f(a.get("z_score") or a.get("zscore")), reverse=True,
        )
        for a in outliers[:3]:
            job  = a.get("job_name") or a.get("Job_Name") or "?"
            peak = _f(a.get("peak_hrs") or a.get("run_hrs"))
            avg  = _f(a.get("avg_hrs") or a.get("mean_hrs"))
            z    = _f(a.get("z_score") or a.get("zscore"))
            if peak <= 0 or avg <= 0:
                continue
            sev = "CRITICAL" if z >= 3 else "HIGH"
            out.append(_q(
                "Runtime & Regression", sev,
                f"{job} peaked at {_humanize_hrs(peak)} against its {_humanize_hrs(avg)} "
                f"average — {z:.1f} standard deviations above normal.",
                "Is this a one-off spike or a developing trend, and has the cause been traced?",
                f"{job}: {peak:.2f}h peak vs {avg:.3f}h avg (z={z:.1f})",
                root_cause="RUNTIME_REGRESSION",
            ))

    return out[:_MAX_PER_CATEGORY]


# ── category 2: execution failures ────────────────────────────────────────────

def _failure_questions(ctx: Dict[str, Any], tail: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    jobs = [j for j in (ctx.get("job_summary") or []) if isinstance(j, dict)]
    # Rank jobs by failure count.
    failing = sorted(
        ((j.get("Job_Name") or j.get("job_name") or "?", _i(j.get("fail_count")))
         for j in jobs),
        key=lambda t: t[1], reverse=True,
    )
    failing = [(jn, n) for jn, n in failing if n > 0]

    # Separate file-watchers — they ask a different (upstream) question.
    watchers = [(jn, n) for jn, n in failing if _is_file_watcher(jn)]
    others   = [(jn, n) for jn, n in failing if not _is_file_watcher(jn)]

    shown = 0
    for jn, n in others[:3]:
        module = _infer_module(jn)
        mod_clause = f"the {module} module" if module else "this job"
        sev = "CRITICAL" if n >= 10 else "HIGH" if n >= 3 else "MEDIUM"
        out.append(_q(
            "Execution Failures", sev,
            f"{jn} failed {n} {_plural(n, 'time', 'times')} in the window.",
            f"Is this a known intermittent issue or a systematic defect in {mod_clause}, "
            f"and is a fix committed {tail}?",
            f"{jn}: {n} {_plural(n, 'failure', 'failures')}",
            root_cause="JOB_FAILURE",
        ))
        shown += 1

    if watchers:
        names = ", ".join(f"{jn} (×{n})" for jn, n in watchers[:3])
        total_w = sum(n for _, n in watchers)
        out.append(_q(
            "Execution Failures", "HIGH",
            f"{len(watchers)} file-watcher {_plural(len(watchers), 'job', 'jobs')} failed "
            f"({names}).",
            "Is the file-arrival SLA from the upstream source system being met, or are the "
            "watchers timing out waiting for files that never arrive?",
            f"{total_w} file-watcher {_plural(total_w, 'failure', 'failures')}",
            root_cause="UPSTREAM_FILE_DELAY",
        ))

    # Zero-duration / pre-execution aborts.
    anomalies = [a for a in (ctx.get("anomalies") or []) if isinstance(a, dict)]
    zero = [a for a in anomalies if a.get("has_zero_sec_failures")]
    if zero:
        names = ", ".join(str(a.get("job_name") or a.get("Job_Name") or "?") for a in zero[:3])
        out.append(_q(
            "Execution Failures", "CRITICAL",
            f"{len(zero)} {_plural(len(zero), 'job', 'jobs')} recorded zero-second failures "
            f"({names}) — pre-execution aborts or dependency timeouts.",
            "What trigger or dependency condition caused these, and do downstream jobs "
            "abort cleanly when they occur?",
            f"{len(zero)} zero-duration {_plural(len(zero), 'failure', 'failures')}",
            root_cause="PRE_EXECUTION_ABORT",
        ))

    return out[:_MAX_PER_CATEGORY]


# ── category 3: SLA & scheduling ──────────────────────────────────────────────

def _sla_questions(ctx: Dict[str, Any], tail: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    window = [w for w in (ctx.get("window") or []) if isinstance(w, dict)]
    kpis   = ctx.get("kpis") or {}

    # ── Headline compliance ladder — a deterministic function of the equation ──
    # The window questions below only fire when there ARE breaches, which left a
    # 100%-compliant customer with a silent strip. This headline always emits ONE
    # question scaled to the actual day-level compliance %, so the same audit reads
    # correctly from "perfect, stress-test it" through to "systemic, fix the SLA".
    import services.pe_config as _pc
    _target = _f(getattr(_pc, "SLA_COMPLIANCE_TARGET_PCT", 95.0), 95.0)
    _crit   = _f(getattr(_pc, "SLA_COMPLIANCE_CRIT_PCT",   80.0), 80.0)
    _atrisk = _f(getattr(_pc, "SLA_ATRISK_PCT", 15.0), 15.0)

    total_days  = _i(kpis.get("window_total_days"))
    breach_ct   = _i(kpis.get("window_breach_days"))
    comp        = kpis.get("window_day_compliance_pct")
    if comp is None:
        comp = kpis.get("window_compliance_pct")
    # Fall back to deriving straight from the per-day window list when KPIs absent.
    if (comp is None or total_days <= 0) and window:
        measured   = [w for w in window if _f(w.get("effective_hrs")) > 0 or ("breach" in w)]
        total_days = len(measured)
        breach_ct  = sum(1 for w in measured if w.get("breach"))
        comp = round((total_days - breach_ct) / total_days * 100.0, 1) if total_days else None
    comp = _f(comp) if comp is not None else None

    # Tightest CLEAN day — the smallest surviving buffer, used to qualify "100%"
    # (100% with 4% buffer is not the same story as 100% with 60% buffer).
    tight_day, tight_buf = None, None
    for w in window:
        if w.get("breach"):
            continue
        _b = w.get("min_buffer_pct")
        if _b is None:
            continue
        _bv = _f(_b)
        if tight_buf is None or _bv < tight_buf:
            tight_buf, tight_day = _bv, w

    if comp is not None and total_days > 0:
        # Grammar helpers so 1-day captures read naturally, not "all 1 day".
        _all_days = "the single measured day" if total_days == 1 else f"all {total_days} measured days"
        _xofy = (lambda x: "the only measured day" if total_days == 1
                 else f"{x} of {total_days} days")
        _tight_date = _fmt_date((tight_day or {}).get("run_date"))
        # Below this many measured days the compliance % is too small a sample to
        # call a trend — a PE reviewer would never label one bad day "systemic".
        _MIN_TREND_DAYS = 3
        _small_sample = total_days < _MIN_TREND_DAYS

        if _small_sample:
            # Too few days to judge steady-state — frame around the sample itself.
            if breach_ct == 0:
                _buf_clause = (f", with {tight_buf:.0f}% buffer to spare on the tightest day"
                               if tight_buf is not None else "")
                _count = "one day" if total_days == 1 else f"{total_days} days"
                out.append(_q(
                    "SLA & Scheduling", "LOW",
                    f"Only {_count} of batch data {'was' if total_days == 1 else 'were'} "
                    f"captured, and {'it' if total_days == 1 else 'every day'} finished inside "
                    f"the SLA window{_buf_clause}.",
                    "A short clean run is encouraging but too small to confirm steady-state "
                    "behaviour. Can a fuller capture — a normal week including the peak day — be "
                    "shared so the SLA can be validated under routine load?",
                    f"100% compliance · {total_days}-day sample",
                    root_cause="SMALL_SAMPLE_CLEAN",
                ))
            else:
                _count = "one day" if total_days == 1 else f"{total_days} days"
                out.append(_q(
                    "SLA & Scheduling", "HIGH",
                    f"Only {_count} of data {'was' if total_days == 1 else 'were'} captured and "
                    f"{_xofy(breach_ct)} breached the window.",
                    "The sample is too small to tell a one-off from a pattern. Is this day "
                    "representative of normal load, and can a fuller capture be shared so the "
                    "real compliance picture is clear?",
                    f"{comp:.0f}% compliance · {total_days}-day sample",
                    root_cause="SMALL_SAMPLE_BREACH",
                ))
        elif breach_ct == 0:
            # 100% compliant over a meaningful sample — confirm + stress-test.
            if tight_buf is not None and tight_buf < _atrisk:
                out.append(_q(
                    "SLA & Scheduling", "MEDIUM",
                    f"Window compliance is 100% across {_all_days}, but the margin is thin: "
                    f"the tightest day ({_tight_date}) finished with only {tight_buf:.0f}% "
                    f"buffer left.",
                    "It passes today but has little room to absorb growth. Has it been validated "
                    "against full production data volume, and what is the data-growth forecast "
                    "before the next peak?",
                    f"100% compliance · tightest {tight_buf:.0f}% buffer · {total_days} days",
                    root_cause="HEALTHY_THIN_MARGIN",
                ))
            else:
                _tclause = (f" The tightest day ({_tight_date}) still kept {tight_buf:.0f}% buffer."
                            if tight_buf is not None else "")
                out.append(_q(
                    "SLA & Scheduling", "LOW",
                    f"Window compliance is 100% — {_all_days} finished inside the SLA window."
                    f"{_tclause}",
                    "Performance is healthy. Has it been validated against full production data "
                    "volume, and is there enough window headroom to absorb forecast growth before "
                    "the next peak?",
                    f"100% window compliance · {total_days} days",
                    root_cause="HEALTHY_HEADROOM",
                ))
        elif comp >= _target:
            out.append(_q(
                "SLA & Scheduling", "MEDIUM",
                f"Window compliance is {comp:.0f}% — {_xofy(breach_ct)} missed the window.",
                "Are these isolated one-offs or an early sign of pressure as volume grows? Is "
                "monitoring catching them before they become a hard breach?",
                f"{comp:.0f}% compliance · {breach_ct}/{total_days} days",
                root_cause="NEAR_MISS",
            ))
        elif comp >= _crit:
            out.append(_q(
                "SLA & Scheduling", "HIGH",
                f"Window compliance is {comp:.0f}% — below the {_target:.0f}% production target, "
                f"with {_xofy(breach_ct)} breaching.",
                f"What is the remediation plan, with named owners and dates, to get back above "
                f"{_target:.0f}% before sign-off?",
                f"{comp:.0f}% compliance · {breach_ct}/{total_days} days",
                root_cause="SUB_TARGET_COMPLIANCE",
            ))
        else:
            out.append(_q(
                "SLA & Scheduling", "CRITICAL",
                f"Window compliance is {comp:.0f}% — well below the {_target:.0f}% target, with "
                f"{_xofy(breach_ct)} breaching the window.",
                "This is a recurring pattern, not a one-off. Is the SLA still correct for current "
                "data volumes, or is this a capacity or code problem? What is the recovery plan, "
                "and by when?",
                f"{comp:.0f}% compliance · {breach_ct}/{total_days} days",
                root_cause="SYSTEMIC_BREACH",
            ))

    breach_days = sorted(
        (w for w in window if w.get("breach")),
        key=lambda w: _f(w.get("breach_overrun_hrs")), reverse=True,
    )

    if breach_days:
        worst = breach_days[0]
        date    = _fmt_date(worst.get("run_date"))
        eff     = _f(worst.get("breach_sub_effective") or worst.get("effective_hrs"))
        ceil    = _f(worst.get("breach_sub_ceil"))
        overrun = _f(worst.get("breach_overrun_hrs"))
        sub     = worst.get("breach_sub_app") or "the daily batch"
        ceil_clause = f" against the {_humanize_hrs(ceil)} ceiling" if ceil > 0 else ""
        out.append(_q(
            "SLA & Scheduling", "CRITICAL",
            f"The worst breach was {date} — {sub} ran {_humanize_hrs(eff)}{ceil_clause}, "
            f"{_humanize_hrs(overrun)} over.",
            "Which jobs drove that day's overrun, and has the incident been documented "
            "with a root cause?",
            f"{date}: +{overrun:.2f}h over on {sub}",
            root_cause="WINDOW_BREACH",
        ))

        # Second / third severe days grouped into one correlation question.
        rest = breach_days[1:3]
        if rest:
            bits = ", ".join(
                f"{_fmt_date(w.get('run_date'))} (+{_f(w.get('breach_overrun_hrs')):.1f}h)"
                for w in rest
            )
            out.append(_q(
                "SLA & Scheduling", "HIGH",
                f"{bits} also breached the batch window.",
                "Do these dates line up with any known infrastructure events, releases, or "
                "data-load spikes?",
                f"{len(rest)} further breach {_plural(len(rest), 'day', 'days')}",
                root_cause="WINDOW_BREACH",
            ))

    # Repeat offenders across days (pattern, not a one-off).
    sla_mx = ctx.get("sla_matrix") or {}
    breach_rows = [r for r in (sla_mx.get("breaches") or []) if isinstance(r, dict)]
    repeats = Counter(
        r.get("job_name") for r in breach_rows
        if r.get("status") == "BREACH" and r.get("job_name")
    )
    repeat_offenders = [(jn, n) for jn, n in repeats.items() if n >= 2]
    if repeat_offenders:
        repeat_offenders.sort(key=lambda t: t[1], reverse=True)
        names = ", ".join(f"{jn} (×{n})" for jn, n in repeat_offenders[:3])
        out.append(_q(
            "SLA & Scheduling", "CRITICAL",
            f"{len(repeat_offenders)} {_plural(len(repeat_offenders), 'job', 'jobs')} breached "
            f"on multiple runs — a pattern, not an anomaly ({names}).",
            "Has the SLA been re-validated against current data volumes, or is a code fix "
            "the right remediation?",
            f"{len(repeat_offenders)} repeat {_plural(len(repeat_offenders), 'offender', 'offenders')}",
            root_cause="REPEAT_BREACH",
        ))

    # Batch start-time consistency.
    start_label = (sla_mx.get("batch_start_label")
                   or (ctx.get("kpis") or {}).get("batch_start_label")
                   or "")
    if start_label and breach_days:
        out.append(_q(
            "SLA & Scheduling", "MEDIUM",
            f"The schedule shows the batch starting at {start_label}.",
            "Is the actual trigger time consistent, or are upstream dependencies delaying "
            "the start and eating into the window?",
            f"scheduled start {start_label}",
            root_cause="LATE_START",
        ))

    return out[:_MAX_PER_CATEGORY]


# ── public entry point ────────────────────────────────────────────────────────

def generate_batch_questions(ctx: Dict[str, Any],
                             lifecycle: str = "") -> List[Dict[str, str]]:
    """Build the dynamic, evidence-cited question bank from batch data.

    ``ctx`` keys (all optional — questions are emitted only where data exists):
      kpis, job_summary, anomalies, window, sla_matrix, benchmark, sub_stats.
    ``lifecycle`` is a free-text hint ("go-live" / "hypercare" / "upgrade") that
    only tunes the closing clause; it never invents facts.

    Returns a list of dicts, severity-sorted, each with:
      category, severity, observation, question, evidence, root_cause.
    """
    if not isinstance(ctx, dict):
        return []
    tail = _lifecycle_tail(lifecycle)

    questions: List[Dict[str, str]] = []
    for builder in (_runtime_questions, _failure_questions, _sla_questions):
        try:
            questions.extend(builder(ctx, tail))
        except Exception:  # one bad category must not sink the bank
            continue

    # Stable, useful ordering: severity desc, then category for readability.
    questions.sort(
        key=lambda q: (-SEV_RANK.get(q["severity"], 0), q["category"]),
    )
    return questions
