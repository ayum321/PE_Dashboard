"""
job_baselines — adaptive per-job SLA from the uploaded Ctrl-M data itself.

The dashboard always has a *global* SLA (e.g. 6h daily / 8h weekly), but real
batch jobs vary wildly: a 30-minute job that suddenly takes 4 hours is a
serious incident even though it never trips the global ceiling, while a
1-hour job that takes 1.2 hours is fine. To detect that we compute a
*per-job adaptive baseline* directly from the file's own history.

Public API:
    compute_job_baselines(df, *, min_runs=3) -> dict[job_name, dict]
        Returns { job_name: {
            runs, avg_hrs, std_hrs, p95_hrs, max_hrs, expected_hrs,
            sample_size_ok
        } }
        - expected_hrs is the adaptive ceiling for that job:
            max(p95_hrs, avg_hrs + 2*std_hrs)
        - sample_size_ok = runs >= min_runs (otherwise treat the baseline as a
          weak signal and prefer the global SLA).

    enrich_runs_with_baselines(df, baselines, *, sla_hrs) -> list[dict]
        Per-run records enriched with job-specific expected_hrs, buffer,
        and z-score. Used by the SLA matrix and correlation engine to flag
        job-level outliers that the global SLA alone would miss.
"""
from __future__ import annotations

from typing import Any, Dict, List


def compute_job_baselines(df, *, min_runs: int = 3) -> Dict[str, Dict[str, Any]]:
    """Compute per-job statistical baselines from the Ctrl-M dataframe.

    Looks at every successful (or non-failed) run of each job and produces:
        - avg_hrs, std_hrs, p95_hrs, max_hrs   — distribution stats
        - expected_hrs                         — adaptive ceiling
        - sample_size_ok                       — True when runs >= min_runs
    """
    if df is None or df.empty or "Job_Name" not in df.columns:
        return {}

    work = df.copy()
    if "run_time_hrs" not in work.columns:
        if "Run_Sec" in work.columns:
            work["run_time_hrs"] = work["Run_Sec"].astype(float).fillna(0.0) / 3600.0
        else:
            return {}

    # Exclude failed runs so the baseline reflects "normal" behaviour, not
    # zero-second crashes that would skew avg_hrs toward 0.
    if "Status" in work.columns:
        try:
            from services.pe_utils import SUCCESS_STATUSES
            ok_mask = work["Status"].astype(str).str.strip().str.upper().isin(SUCCESS_STATUSES)
            # If literally everything failed, fall back to all rows so we still
            # return something sensible.
            if ok_mask.any():
                work = work[ok_mask]
        except Exception:
            pass

    work = work[work["run_time_hrs"] > 0]
    if work.empty:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for job, grp in work.groupby("Job_Name"):
        hrs = grp["run_time_hrs"].astype(float)
        runs = int(len(hrs))
        avg  = float(hrs.mean())
        std  = float(hrs.std(ddof=0)) if runs > 1 else 0.0
        try:
            p95 = float(hrs.quantile(0.95))
        except Exception:
            p95 = float(hrs.max())
        mx = float(hrs.max())
        # GAP-F2 fix: adaptive ceiling formula depends on sample size.
        # For small n, stddev is an unreliable estimator — reduce the multiplier
        # to avoid inflating the ceiling above any observed value.
        if runs >= 10:
            expected = max(p95, avg + 2.0 * std)
        elif runs >= 5:
            expected = max(p95, avg + 1.5 * std)  # reduced multiplier for small n
        else:
            expected = mx                           # insufficient data: use observed max
        if expected <= 0:
            expected = max(mx, avg)

        out[str(job)] = {
            "runs":            runs,
            "avg_hrs":         round(avg, 4),
            "std_hrs":         round(std, 4),
            "p95_hrs":         round(p95, 4),
            "max_hrs":         round(mx, 4),
            "expected_hrs":    round(expected, 4),
            "sample_size_ok":  runs >= min_runs,
        }
    return out


def enrich_runs_with_baselines(
    df,
    baselines: Dict[str, Dict[str, Any]],
    *,
    sla_hrs: float,
) -> List[Dict[str, Any]]:
    """Return per-run records combining global SLA + per-job baseline.

    Each record contains:
        job_name, run_date, start_time, end_time, run_hrs,
        sla_limit_hrs        — the global SLA ceiling
        expected_hrs         — the job's own adaptive ceiling
        sla_margin_hrs       — run_hrs - sla_limit_hrs   (positive = SLA breach)
        expected_margin_hrs  — run_hrs - expected_hrs    (positive = job outlier)
        outlier_z            — z-score vs job's own avg/std
        is_sla_breach, is_job_outlier, is_failure
    """
    import pandas as pd

    if df is None or df.empty:
        return []

    rows: List[Dict[str, Any]] = []
    try:
        from services.pe_utils import SUCCESS_STATUSES
    except Exception:
        SUCCESS_STATUSES = {"ENDED OK"}

    for _, row in df.iterrows():
        job = str(row.get("Job_Name", "?"))
        hrs = float(row.get("run_time_hrs", 0) or 0)
        if hrs <= 0 and "Run_Sec" in row.index:
            try:
                hrs = float(row.get("Run_Sec", 0) or 0) / 3600.0
            except Exception:
                hrs = 0.0
        raw_status = str(row.get("Status", "")).strip().upper()
        is_failure = bool(raw_status) and raw_status not in SUCCESS_STATUSES

        run_date = ""; start_str = ""; end_str = ""; start_hour = None
        try:
            st = pd.Timestamp(row.get("Start_Time", pd.NaT))
            if pd.notna(st):
                run_date  = st.strftime("%Y-%m-%d")
                start_str = st.strftime("%H:%M")
                start_hour = int(st.hour)
        except Exception:
            pass
        try:
            et = pd.Timestamp(row.get("End_Time", pd.NaT))
            if pd.notna(et):
                end_str = et.strftime("%H:%M")
        except Exception:
            pass

        bl = baselines.get(job) or {}
        expected = float(bl.get("expected_hrs", 0) or 0)
        avg      = float(bl.get("avg_hrs", 0) or 0)
        std      = float(bl.get("std_hrs", 0) or 0)
        z = round((hrs - avg) / std, 2) if std > 0 else 0.0

        sla_margin = round(hrs - sla_hrs, 4)
        exp_margin = round(hrs - expected, 4) if expected > 0 else 0.0

        # SLA breach = global ceiling exceeded
        is_sla_breach = (not is_failure) and (hrs > sla_hrs)
        # Job outlier = exceeded its own adaptive ceiling AND has enough
        # samples. Magnitude floors prevent sub-minute "regressions" of a
        # second or two from being flagged as critical incidents — those
        # are clock-jitter noise, not performance issues. A real outlier
        # must (a) actually run for at least ~6 minutes, (b) have a
        # meaningful baseline (≥ 6 min), and (c) exceed it by ≥ 6 min.
        _MIN_RUNTIME_HRS    = 0.10   # 6 min  — ignore sub-minute jobs
        _MIN_EXPECTED_HRS   = 0.10   # 6 min  — baseline must be material
        _MIN_DELTA_HRS      = 0.10   # 6 min  — regression must be visible
        is_outlier = (
            (not is_failure)
            and bl.get("sample_size_ok", False)
            and expected >= _MIN_EXPECTED_HRS
            and hrs      >= _MIN_RUNTIME_HRS
            and (hrs - expected) >= _MIN_DELTA_HRS
            and z >= 2.0
        )

        rows.append({
            "job_name":            job,
            "sub_application":     str(row.get("Sub_Application", "—")),
            "run_date":            run_date,
            "start_time":          start_str,
            "end_time":            end_str,
            "start_hour":          start_hour,
            "run_hrs":             round(hrs, 4),
            "sla_limit_hrs":       round(sla_hrs, 4),
            "expected_hrs":        round(expected, 4),
            "sla_margin_hrs":      sla_margin,
            "expected_margin_hrs": exp_margin,
            "outlier_z":           z,
            "is_sla_breach":       bool(is_sla_breach),
            "is_job_outlier":      bool(is_outlier),
            "is_failure":          bool(is_failure),
            "raw_status":          raw_status,
        })

    return rows


def correlate_with_resource_hours(
    runs: List[Dict[str, Any]],
    hour_heatmap: Dict[str, Any] | None,
    servers: List[Dict[str, Any]] | None,
    *,
    cpu_hot: float = 80.0,
    mem_hot: float = 80.0,
) -> List[Dict[str, Any]]:
    """Annotate breach/outlier runs with resource-pressure context.

    We don't have per-server time series, but we DO know:
        - hour_heatmap: which hours of the day are the busiest for batch
        - servers:      current snapshot of CPU/MEM per server (single point)

    For each flagged run we produce a `resource_signal` dict:
        {
            in_hot_window: bool,        # run started during a top-quartile hour
            hot_hour_jobs: int,         # job count in that hour
            fleet_cpu: float,           # current avg CPU across servers
            fleet_mem: float,           # current avg MEM across servers
            critical_hosts: list[str],  # hosts at >= cpu_hot or >= mem_hot
            verdict: "RESOURCE_LINK" | "TIMING_PRESSURE" | "ISOLATED",
        }
    """
    # Build hourly job counts from heatmap if provided.
    hour_counts: Dict[int, int] = {}
    if hour_heatmap:
        try:
            for entry in hour_heatmap.get("data", []) or []:
                h = int(entry.get("hour", -1))
                if 0 <= h <= 23:
                    hour_counts[h] = int(entry.get("jobs", 0) or 0)
        except Exception:
            pass
    if not hour_counts and isinstance(hour_heatmap, list):
        # Tolerate the alternative shape used elsewhere
        try:
            for entry in hour_heatmap:
                h = int(entry.get("hour", -1))
                if 0 <= h <= 23:
                    hour_counts[h] = int(entry.get("jobs", 0) or 0)
        except Exception:
            pass

    sorted_hours = sorted(hour_counts.values())
    hot_threshold = sorted_hours[int(len(sorted_hours) * 0.75)] if sorted_hours else 0

    fleet_cpu = 0.0; fleet_mem = 0.0
    crit_hosts: List[str] = []
    if servers:
        cpus = [float(s.get("cpu_used", 0) or 0) for s in servers]
        mems = [float(s.get("mem_used", 0) or 0) for s in servers]
        fleet_cpu = round(sum(cpus) / len(cpus), 1) if cpus else 0.0
        fleet_mem = round(sum(mems) / len(mems), 1) if mems else 0.0
        for s in servers:
            cpu_v = float(s.get("cpu_used", 0) or 0)
            mem_v = float(s.get("mem_used", 0) or 0)
            if cpu_v >= cpu_hot or mem_v >= mem_hot:
                crit_hosts.append(str(s.get("host", "?")))

    enriched: List[Dict[str, Any]] = []
    for r in runs:
        if not (r.get("is_sla_breach") or r.get("is_job_outlier")):
            continue
        h = r.get("start_hour")
        in_hot = bool(
            h is not None and hot_threshold > 0
            and hour_counts.get(int(h), 0) >= hot_threshold
        )
        hot_jobs = int(hour_counts.get(int(h), 0)) if h is not None else 0

        if crit_hosts and (in_hot or fleet_cpu >= cpu_hot or fleet_mem >= mem_hot):
            verdict = "RESOURCE_LINK"
        elif in_hot:
            verdict = "TIMING_PRESSURE"
        else:
            verdict = "ISOLATED"

        enriched.append({
            **r,
            "resource_signal": {
                "in_hot_window":  in_hot,
                "hot_hour_jobs":  hot_jobs,
                "fleet_cpu":      fleet_cpu,
                "fleet_mem":      fleet_mem,
                "critical_hosts": crit_hosts[:5],
                "verdict":        verdict,
            },
        })
    return enriched
