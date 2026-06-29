"""Spike-to-batch attribution — the cross-source time join no commercial PE tool
does, because nobody else holds both Azure Monitor spikes and Ctrl-M run history.

For each Azure resource spike window the engine finds which Ctrl-M jobs were
executing during that window, ranked by runtime, so a PE lead can say "the 95%
CPU spike at 02:13 lines up with CALCPLAN_Daily (4.2h)". This is time-coincidence,
not host-pinned causation — Ctrl-M exports carry no host column, so a job can only
be linked by overlapping clock time, not by which VM ran it. Callers must surface
that caveat. Timezone: spikes are UTC, batch Start_Time is naive local — both are
compared as naive wall-clock, which assumes the customer's batch clock is the VM's.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger("pe_dashboard.spike_attribution")

_ATTRIBUTABLE = {"critical_sustained", "critical", "warning"}


def _naive(ts) -> Optional[pd.Timestamp]:
    t = pd.to_datetime(ts, errors="coerce")
    if t is None or pd.isna(t):
        return None
    return t.tz_localize(None) if t.tzinfo is not None else t


def _load_runs(job_runs: List[dict]) -> List[dict]:
    """Parse cached job_runs_df rows into start/end/hrs/name, dropping anything
    without a usable run window. Returns naive wall-clock timestamps."""
    runs = []
    for r in job_runs or []:
        s = _naive(r.get("Start_Time"))
        e = _naive(r.get("End_Time"))
        if s is None:
            continue
        if e is None:
            secs = r.get("Run_Sec")
            if secs:
                e = s + pd.Timedelta(seconds=float(secs))
            else:
                continue
        hrs = r.get("run_time_hrs")
        runs.append({
            "job": r.get("Job_Name") or r.get("Sub_Application") or "?",
            "start": s, "end": e,
            "hrs": float(hrs) if hrs is not None else round((e - s).total_seconds() / 3600.0, 2),
        })
    return runs


def attribute_spikes(vms: Dict[str, Any], job_runs: List[dict], top_n: int = 5) -> dict:
    """Join attributable spikes to concurrently-running jobs. Returns rows (one per
    spike, worst severity first) + a summary with the time-coincidence caveat."""
    runs = _load_runs(job_runs)
    rows: List[dict] = []
    spikes_total = 0
    for vm_name, vm in (vms or {}).items():
        for metric, splist in (vm.get("spikes") or {}).items():
            for sp in splist:
                if (sp.get("severity") or "") not in _ATTRIBUTABLE:
                    continue
                spikes_total += 1
                ws, we = _naive(sp.get("start")), _naive(sp.get("end"))
                if ws is None or we is None:
                    continue
                # overlap: run started before window end AND ended after window start
                hits = [r for r in runs if r["start"] < we and r["end"] > ws]
                hits.sort(key=lambda r: r["hrs"], reverse=True)
                top = hits[:top_n]
                rows.append({
                    "vm": vm_name, "metric": metric,
                    "peak": sp.get("peak"), "peak_time": sp.get("peak_time"),
                    "severity": sp.get("severity"), "duration_min": sp.get("duration_min"),
                    "concurrent_jobs": len(hits),
                    "heaviest": top[0]["job"] if top else None,
                    "heaviest_hrs": top[0]["hrs"] if top else None,
                    "jobs": [{"job": r["job"], "hrs": r["hrs"],
                              "start": r["start"].isoformat(), "end": r["end"].isoformat()} for r in top],
                })
    sev_rank = {"critical_sustained": 0, "critical": 1, "warning": 2}
    rows.sort(key=lambda x: (sev_rank.get(x["severity"], 9), -(x["peak"] or 0)))
    attributed = sum(1 for r in rows if r["concurrent_jobs"] > 0)
    return {
        "rows": rows,
        "summary": {
            "spikes_total": spikes_total,
            "spikes_attributed": attributed,
            "attribution_rate": round(attributed / spikes_total * 100, 1) if spikes_total else 0.0,
            "runs_loaded": len(runs),
            "method": "time_coincidence",
            "caveat": ("Jobs are linked by overlapping clock time, not by host — Ctrl-M exports "
                       "carry no server column. Spike times are UTC; batch times assumed same wall-clock."),
        },
    }
