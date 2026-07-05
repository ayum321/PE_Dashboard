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
    if abs(h - round(h)) < 0.05:  # whole hours read cleaner without the .0
        return f"{int(round(h))} hours"
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


# Day-of-week / schedule-variant tokens a job name is commonly suffixed with —
# e.g. W_IS_FILE_WATCHER_INBOUND_Daily and W_IS_FILE_WATCHER_INBOUND_TUE are the
# SAME job run on two different schedules, not two different jobs. Stripping
# these lets the question generator recognise siblings and merge them into one
# technical question instead of repeating the identical template per variant.
_SCHEDULE_SUFFIX_RE = None


def _job_root(name: str) -> str:
    """Collapse a job name to its schedule-agnostic root for de-duplication.

    'W_IS_FILE_WATCHER_INBOUND_Daily' and 'W_IS_FILE_WATCHER_INBOUND_TUE' both
    collapse to 'W_IS_FILE_WATCHER_INBOUND' — siblings on different run-day
    schedules, so they should be raised as ONE question, not one each.
    """
    import re
    global _SCHEDULE_SUFFIX_RE
    if _SCHEDULE_SUFFIX_RE is None:
        _SCHEDULE_SUFFIX_RE = re.compile(
            r"[_\-](DAILY|WEEKLY|MONTHLY|TUE|TUESDAY|MON|MONDAY|WED|WEDNESDAY|"
            r"THU|THURSDAY|FRI|FRIDAY|SAT|SATURDAY|SUN|SUNDAY|SEQ)+$",
            re.I,
        )
    base = _base_job(name)
    prev = None
    while prev != base:
        prev = base
        base = _SCHEDULE_SUFFIX_RE.sub("", base)
    return base.upper().strip("_-") or _base_job(name).upper()


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
            f"What caused this step-up, has a root-cause analysis been completed, and is it "
            f"resolved now — confirmed in a clean re-run at the same data volume {tail}?",
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
        # Group by schedule-agnostic root so a Daily/TUE (or Mon/Wed/Fri…) pair of
        # the SAME job is raised as ONE technical question, not the identical
        # template repeated once per schedule variant (e.g. two near-identical
        # "W_IS_FILE_WATCHER_INBOUND_Daily" / "..._TUE" entries).
        groups: Dict[str, List[Dict[str, Any]]] = {}
        order: List[str] = []
        for a in outliers:
            job = a.get("job_name") or a.get("Job_Name") or "?"
            peak = _f(a.get("peak_hrs") or a.get("run_hrs"))
            avg  = _f(a.get("avg_hrs") or a.get("mean_hrs"))
            if peak <= 0 or avg <= 0:
                continue
            root = _job_root(job)
            if root not in groups:
                groups[root] = []
                order.append(root)
            groups[root].append(a)

        for root in order[:3]:
            members = groups[root]
            worst = max(members, key=lambda a: _f(a.get("z_score") or a.get("zscore")))
            job  = worst.get("job_name") or worst.get("Job_Name") or "?"
            peak = _f(worst.get("peak_hrs") or worst.get("run_hrs"))
            avg  = _f(worst.get("avg_hrs") or worst.get("mean_hrs"))
            z    = _f(worst.get("z_score") or worst.get("zscore"))
            sev = "CRITICAL" if z >= 3 else "HIGH"

            sibling_names = [
                (m.get("job_name") or m.get("Job_Name") or "?") for m in members if m is not worst
            ]
            names_txt = job if not sibling_names else f"{job} and its {_plural(len(sibling_names), 'schedule sibling', 'schedule siblings')} ({', '.join(sibling_names)})"

            # A file-watcher/listener job's peak is upstream-file arrival latency,
            # not CPU/compute time — ask about the file source, not "code fix".
            if _is_file_watcher(job):
                question = (
                    "Did the upstream file arrive late that run, or is the watcher's "
                    "poll/timeout interval mis-tuned — has this been checked against the "
                    "source system's file-delivery SLA?"
                )
            else:
                question = (
                    "Was another job or batch window running in parallel at that time, "
                    "or is this job dependent on an upstream step that ran long — has the "
                    "specific run been traced to a root cause?"
                )

            if len(members) > 1:
                observation = (
                    f"{names_txt} both peaked at {_humanize_hrs(peak)} against a "
                    f"{_humanize_hrs(avg)} average — {z:.1f} standard deviations above normal, "
                    "on both their run-day schedules."
                )
            else:
                observation = (
                    f"{job} peaked at {_humanize_hrs(peak)} against its {_humanize_hrs(avg)} "
                    f"average — {z:.1f} standard deviations above normal."
                )

            out.append(_q(
                "Runtime & Regression", sev,
                observation,
                question,
                f"{job}: {peak:.2f}h peak vs {avg:.3f}h avg (z={z:.1f})"
                + (f" · {len(sibling_names)} sibling schedule(s) also affected" if sibling_names else ""),
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

    # Driving job + facts for a given breach day, so questions can name the
    # specific job that pushed the day over — not just "the daily batch".
    def _driver_for(day: Dict[str, Any]) -> str:
        return (str(day.get("top_job") or "").strip()
                or str(day.get("breach_sub_app") or "").strip())

    def _day_facts(day: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "date":    _fmt_date(day.get("run_date")),
            "eff":     _f(day.get("breach_sub_effective") or day.get("effective_hrs")),
            "ceil":    _f(day.get("breach_sub_ceil")),
            "overrun": _f(day.get("breach_overrun_hrs")),
            "sub":     day.get("breach_sub_app") or "the daily batch",
            "driver":  _driver_for(day),
        }

    breach_days = sorted(
        (w for w in window if w.get("breach")),
        key=lambda w: _f(w.get("breach_overrun_hrs")), reverse=True,
    )

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
        elif breach_ct == 1 and breach_days:
            # A single bad day on an otherwise clean run — this is the one-off case.
            # Ask what ran in PARALLEL that specific day, not for a systemic plan.
            f = _day_facts(breach_days[0])
            _ceil_clause = f" against its {_humanize_hrs(f['ceil'])} ceiling" if f["ceil"] > 0 else ""
            _driver_clause = f", led by {f['driver']}," if f["driver"] else ""
            _sev = "CRITICAL" if f["overrun"] >= 1.0 else "HIGH"
            out.append(_q(
                "SLA & Scheduling", _sev,
                f"{f['sub']} breached the window only on {f['date']} — it ran "
                f"{_humanize_hrs(f['eff'])}{_ceil_clause}, {_humanize_hrs(f['overrun'])} over"
                f"{_driver_clause} while the other {total_days - 1} measured days stayed inside "
                f"the window.",
                f"A single overrun on an otherwise clean run usually points to a one-off event "
                f"rather than a capacity limit. Was anything unusual running alongside the batch "
                f"on {f['date']} — an ad-hoc data reload, a release or deployment, a manual "
                f"reprocess, or a parallel backup? Was it logged as an incident with a root cause?",
                f"{f['date']}: +{f['overrun']:.2f}h over on {f['sub']}",
                root_cause="ONEOFF_BREACH",
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

    # ── Worst breach day (named) — only when there are 2+ breach days, since a
    #    single breach is already the one-off headline above. Names the driving
    #    job and asks what specifically pushed that day over.
    if breach_days and len(breach_days) >= 2:
        f = _day_facts(breach_days[0])
        _ceil_clause = f" against the {_humanize_hrs(f['ceil'])} ceiling" if f["ceil"] > 0 else ""
        _driver_clause = (f" The longest-running job that day was {f['driver']}."
                          if f["driver"] else "")
        out.append(_q(
            "SLA & Scheduling", "CRITICAL",
            f"The worst breach was {f['date']} — {f['sub']} ran {_humanize_hrs(f['eff'])}"
            f"{_ceil_clause}, {_humanize_hrs(f['overrun'])} over.{_driver_clause}",
            f"What pushed {f['date']} over — a data-volume spike, a parallel activity, or a slow "
            f"dependency? Has the day been logged as an incident with a documented root cause?",
            f"{f['date']}: +{f['overrun']:.2f}h over on {f['sub']}",
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
                "data-load spikes, or is the same job slow on every one of them?",
                f"{len(rest)} further breach {_plural(len(rest), 'day', 'days')}",
                root_cause="WINDOW_BREACH",
            ))

    # ── Job-level driver — a specific job running long against its OWN ceiling.
    #    This is the "one job is taking too much time" angle: name the job, its
    #    actual peak runtime vs its SLA, and ask what it is doing / can it be tuned.
    jobs = [j for j in (ctx.get("job_summary") or []) if isinstance(j, dict)]

    def _jget(j: Dict[str, Any], *keys):
        for k in keys:
            v = j.get(k)
            if v is not None:
                return v
        return None

    job_cands: List[tuple] = []
    for j in jobs:
        if j.get("is_utility"):
            continue
        name = _jget(j, "Job_Name", "job_name")
        buf  = _jget(j, "buffer_pct")
        peak = _f(_jget(j, "peak_hrs", "peak_run_hrs", "peak"))
        sla  = _f(_jget(j, "sla_hrs"))
        if not name or buf is None or peak <= 0 or sla <= 0:
            continue
        job_cands.append((
            str(name), _f(buf), peak, _f(_jget(j, "avg_hrs", "mean_hrs")),
            sla, _f(_jget(j, "sla_used_pct")),
        ))

    if job_cands:
        # Worst job = lowest buffer = most over (or closest to) its own ceiling.
        job_cands.sort(key=lambda t: t[1])
        # Jobs eating deep into their own ceiling (at-risk band or breaching).
        deep      = [c for c in job_cands if c[1] <= _atrisk]
        breaching = [c for c in deep if c[1] < 0]

        # ── Multiple long jobs in the same window — the concurrency / contention
        #    angle. When several jobs run deep into the SLA, the real question is
        #    what is running longest and whether they overlap and starve each
        #    other of CPU / memory / I/O / DB, rather than each running cleanly.
        if len(deep) >= 2:
            top = deep[:3]
            named = ", ".join(f"{n} ({_humanize_hrs(pk)})" for n, bf, pk, av, sl, us in top)
            n_more = len(deep) - len(top)
            more_clause = f", plus {n_more} more" if n_more > 0 else ""
            longest = max(deep, key=lambda c: c[2])
            # The list is ordered by tightest buffer; the longest-by-runtime job
            # may differ, so only call it out when it is not already leading.
            _biggest_clause = (
                f", with {longest[0]} the single biggest at {_humanize_hrs(longest[2])}"
                if longest[0] != top[0][0] else ""
            )
            sev = "CRITICAL" if breaching else "HIGH"
            if not breaching:
                _breach_clause = " — all still inside the ceiling but with little headroom"
            elif len(breaching) == len(deep):
                _breach_clause = (" — both already over the ceiling" if len(deep) == 2
                                  else f" — all {len(deep)} already over the ceiling")
            else:
                _breach_clause = f" — {len(breaching)} of them already over the ceiling"
            out.append(_q(
                "SLA & Scheduling", sev,
                f"{len(deep)} jobs are running deep into the SLA window{_breach_clause}. "
                f"The longest are {named}{more_clause}{_biggest_clause}.",
                "When several long jobs land in the same window, the issue is often how they "
                "are scheduled, not any one job. Are these running one after another or all at "
                "once? If they overlap, are they competing for the same CPU, memory, I/O or "
                f"database — so each runs slower than it would alone? Is {longest[0]} on the "
                "critical path, and would resequencing or staggering the jobs (or moving "
                "non-critical work outside the window) recover time?",
                f"{len(deep)} jobs \u2264{_atrisk:.0f}% buffer \u00b7 {len(breaching)} breaching",
                root_cause="WINDOW_CONCURRENCY",
            ))

        name, buf, peak, avg, sla, used = job_cands[0]
        if used <= 0:
            used = (peak / sla * 100.0) if sla > 0 else 0.0
        if buf < 0:
            _avg_clause = (f", well above its usual {_humanize_hrs(avg)}"
                           if avg > 0 and peak >= avg * 1.25 else "")
            out.append(_q(
                "SLA & Scheduling", "CRITICAL",
                f"{name} is the single job most over its own ceiling — it peaked at "
                f"{_humanize_hrs(peak)} against a {_humanize_hrs(sla)} SLA{_avg_clause}.",
                f"Is {_humanize_hrs(peak)} the expected runtime for this job, or is it running "
                f"long? What is it processing in that step, and can the work be split, indexed, "
                f"or run in parallel to bring it back under {_humanize_hrs(sla)}?",
                f"{name}: {peak:.2f}h peak vs {sla:.2f}h SLA ({buf:.0f}% buffer)",
                root_cause="JOB_LEVEL_BREACH",
            ))
        elif buf <= _atrisk:
            out.append(_q(
                "SLA & Scheduling", "HIGH",
                f"{name} is the tightest job against its ceiling — it peaked at "
                f"{_humanize_hrs(peak)}, using {used:.0f}% of its {_humanize_hrs(sla)} SLA "
                f"({buf:.0f}% buffer left).",
                f"It clears today but has very little headroom. Is {_humanize_hrs(peak)} the "
                f"expected runtime, and what is the data-growth forecast before this job tips "
                f"over its ceiling?",
                f"{name}: {used:.0f}% of SLA · {buf:.0f}% buffer",
                root_cause="JOB_LEVEL_TIGHT",
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
            "Since the same job is slow repeatedly, this is unlikely to be a one-off. Has its SLA "
            "been re-validated against current data volumes, or is a code or indexing fix the "
            "right remediation?",
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
