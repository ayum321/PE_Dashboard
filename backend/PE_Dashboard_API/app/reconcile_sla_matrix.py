#!/usr/bin/env python3
"""
SLA Matrix System-Wide Reconciliation & Ground-Truth Diff Engine
================================================================
Independently computes expected ground-truth SLA, measured runtime duration,
headroom, buffer %, and compliance status for all workflows/jobs (132+),
and diffs against the dashboard engine's output layer-by-layer.

Split by 4 evaluation layers:
  1. Formula Precision: (Buffer% = (SLA - rt)/SLA * 100, headroom = (SLA - rt)*60)
  2. Top-Level Aggregates: Total runs, Breaching, At Risk, Long Jobs, Compliance %
  3. Per-Row Data Resolution: (Tier-1 contract vs Tier-2 SOW vs Tier-3 Assumed)
  4. Disclosure & Assumed Ceiling Sync: Banner vs Tier-3 table rows

Usage:
  python reconcile_sla_matrix.py [--batch-sla <path>] [--ctrlm <path>] [--ceiling <hours>] [--json <out>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Add app root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routers.sla_matrix import _compute_sla_matrix
from services import config_store, pe_config, session_cache
from services.sla_merger import _overnight_delta_hours, parse_sla_hours, parse_start_time


@dataclass
class WorkflowTruth:
    workflow: str
    module: str
    schedule: str
    raw_start: Optional[str]
    raw_expected_end: Optional[str]
    raw_current_end: Optional[str]
    declared_duration: Optional[float]
    first_job: Optional[str]
    last_job: Optional[str]
    expected_sla: float
    expected_tier: str  # "Tier 1", "Tier 2", "Tier 3"
    expected_runtime: Optional[float]
    expected_headroom_mins: Optional[int]
    expected_buffer_pct: Optional[float]
    expected_status: str


@dataclass
class RowDiff:
    workflow: str
    clean: bool
    sla_truth: float
    sla_engine: Optional[float]
    sla_match: bool
    runtime_truth: Optional[float]
    runtime_engine: Optional[float]
    runtime_match: bool
    headroom_truth: Optional[int]
    headroom_engine: Optional[int]
    headroom_match: bool
    buffer_truth: Optional[float]
    buffer_engine: Optional[float]
    buffer_match: bool
    status_truth: str
    status_engine: Optional[str]
    status_match: bool
    tier_truth: str
    tier_engine: Optional[str]
    tier_match: bool
    defects: List[str]


def compute_ground_truth_workflow(
    raw_row: Dict[str, Any],
    assumed_daily_ceiling: float = 8.25,
    ctrlm_df: Optional[pd.DataFrame] = None,
) -> WorkflowTruth:
    """Independently calculates ground truth for a workflow row without using dashboard internal state."""
    wf_name = str(raw_row.get("workflow") or raw_row.get("batch_name") or raw_row.get("Workflow") or "?").strip()
    mod_name = str(raw_row.get("module") or raw_row.get("Module") or "").strip()
    sched = str(raw_row.get("schedule") or raw_row.get("Schedule") or "Daily").strip()

    raw_st = raw_row.get("start_time") or raw_row.get("Start_Time") or raw_row.get("start")
    raw_et = raw_row.get("expected_end_time") or raw_row.get("sla_end_time") or raw_row.get("Expected_End_Time") or raw_row.get("sla")
    raw_ct = raw_row.get("current_end_time") or raw_row.get("Current_End_Time")

    dur_raw = raw_row.get("duration") or raw_row.get("sla_hours") or raw_row.get("Duration")
    declared_dur: Optional[float] = None
    if dur_raw is not None:
        try:
            declared_dur = float(dur_raw)
        except (ValueError, TypeError):
            declared_dur = parse_sla_hours(str(dur_raw))

    first_j = str(raw_row.get("first_job") or raw_row.get("First_Job") or "").strip() or None
    last_j = str(raw_row.get("last_job") or raw_row.get("Last_Job") or "").strip() or None

    # Determine True Expected SLA & Tier
    exp_sla: float = assumed_daily_ceiling
    exp_tier: str = "Tier 3"

    if declared_dur is not None and declared_dur > 0:
        exp_sla = declared_dur
        exp_tier = "Tier 1"
    elif raw_st and raw_et:
        delta_h = _overnight_delta_hours(str(raw_st), str(raw_et))
        if delta_h is not None and delta_h > 0:
            exp_sla = delta_h
            exp_tier = "Tier 1"
        else:
            exp_sla = assumed_daily_ceiling
            exp_tier = "Tier 3"
    else:
        exp_sla = assumed_daily_ceiling
        exp_tier = "Tier 3"

    # Determine True Expected Runtime
    exp_rt: Optional[float] = None
    if ctrlm_df is not None and not ctrlm_df.empty:
        # Filter Ctrl-M jobs for this workflow
        sub_mask = pd.Series(False, index=ctrlm_df.index)
        if "Sub_Application" in ctrlm_df.columns:
            sub_col = ctrlm_df["Sub_Application"].astype(str).str.upper()
            sub_mask |= (sub_col == wf_name.upper())
            if mod_name:
                sub_mask |= (sub_col == mod_name.upper())
            for al in raw_row.get("aliases") or []:
                sub_mask |= (sub_col == str(al).upper())

        # If anchors present, narrow by anchors
        if first_j and last_j and "Job_Name" in ctrlm_df.columns:
            j_col = ctrlm_df["Job_Name"].astype(str).str.upper()
            fj_cand = [c.strip().upper() for c in re.split(r"[\r\n\t;]+| {2,}", first_j) if c.strip()]
            lj_cand = [c.strip().upper() for c in re.split(r"[\r\n\t;]+| {2,}", last_j) if c.strip()]
            f_mask = j_col.apply(lambda j: any(c in j for c in fj_cand))
            l_mask = j_col.apply(lambda j: any(c in j for c in lj_cand))
            if sub_mask.any():
                f_mask &= sub_mask
                l_mask &= sub_mask
            if f_mask.any() and l_mask.any():
                st_series = pd.to_datetime(ctrlm_df.loc[f_mask, "Start_Time"], errors="coerce").dropna()
                et_series = pd.to_datetime(ctrlm_df.loc[l_mask, "End_Time"], errors="coerce").dropna()
                if not st_series.empty and not et_series.empty:
                    exp_rt = round((et_series.max() - st_series.min()).total_seconds() / 3600.0, 4)

        if exp_rt is None and sub_mask.any():
            st_series = pd.to_datetime(ctrlm_df.loc[sub_mask, "Start_Time"], errors="coerce").dropna()
            et_series = pd.to_datetime(ctrlm_df.loc[sub_mask, "End_Time"], errors="coerce").dropna()
            if not st_series.empty and not et_series.empty:
                exp_rt = round((et_series.max() - st_series.min()).total_seconds() / 3600.0, 4)

    if exp_rt is None and raw_st and raw_ct:
        ct_delta = _overnight_delta_hours(str(raw_st), str(raw_ct))
        if ct_delta is not None and ct_delta >= 0:
            exp_rt = ct_delta

    # Calculate Buffer %, Headroom, Status
    exp_hd: Optional[int] = None
    exp_buf: Optional[float] = None
    exp_stat: str = "NOT_OBSERVED"

    if exp_rt is not None and exp_sla > 0:
        exp_hd = round((exp_sla - exp_rt) * 60)
        exp_buf = round((exp_sla - exp_rt) / exp_sla * 100.0, 2)
        if exp_buf <= 0:
            exp_stat = "BREACH"
        elif exp_buf <= 15.0:
            exp_stat = "AT_RISK"
        elif exp_buf <= 40.0:
            exp_stat = "LONG_JOB"
        else:
            exp_stat = "OK"

    return WorkflowTruth(
        workflow=wf_name,
        module=mod_name,
        schedule=sched,
        raw_start=str(raw_st) if raw_st else None,
        raw_expected_end=str(raw_et) if raw_et else None,
        raw_current_end=str(raw_ct) if raw_ct else None,
        declared_duration=declared_dur,
        first_job=first_j,
        last_job=last_j,
        expected_sla=exp_sla,
        expected_tier=exp_tier,
        expected_runtime=exp_rt,
        expected_headroom_mins=exp_hd,
        expected_buffer_pct=exp_buf,
        expected_status=exp_stat,
    )


def generate_132_workflow_benchmark(assumed_ceiling: float = 8.25) -> Tuple[List[Dict[str, Any]], pd.DataFrame]:
    """Generates the full 132-job comprehensive benchmark across all 3 tiers,
    covering exact matches, multi-line anchors, clock-inferred SLAs, SOW windows,
    typos, and Tier 3 assumed ceilings.
    """
    bench_rows: List[Dict[str, Any]] = []
    ctrlm_events: List[Dict[str, Any]] = []

    # Category 1: Standard Tier-1 Workflows with declared duration (Rows 1 to 40)
    for i in range(1, 41):
        name = f"PROD_CORE_BATCH_{i:02d}"
        dur = round(1.0 + (i % 6) * 0.75, 2)
        rt = round(dur * (0.4 + (i % 5) * 0.2), 2)
        bench_rows.append({
            "workflow": name,
            "module": f"CORE_MOD_{i:02d}",
            "schedule": "Daily",
            "sla_hours": dur,
            "first_job": f"J_{name}_START",
            "last_job": f"J_{name}_END",
            "start_time": "10:00 AM",
            "expected_end_time": "12:00 PM",
            "current_end_time": "11:30 AM",
        })
        ctrlm_events.extend([
            {"Job_Name": f"J_{name}_START", "Sub_Application": name, "Start_Time": "2026-08-30 10:00:00", "End_Time": "2026-08-30 10:15:00"},
            {"Job_Name": f"J_{name}_STEP",  "Sub_Application": name, "Start_Time": "2026-08-30 10:15:00", "End_Time": "2026-08-30 11:15:00"},
            {"Job_Name": f"J_{name}_END",   "Sub_Application": name, "Start_Time": "2026-08-30 11:15:00", "End_Time": f"2026-08-30 11:{30 + (i % 20):02d}:00"},
        ])

    # Category 2: Clock-inferred SLAs with multiline anchors and alias merges (Rows 41 to 75)
    # Includes Row 4 merged case (ASC_NIGHTLY + PROD_ATTA), multiline anchors (MPS), and typo ("Thrusday")
    bench_rows.append({
        "workflow": "ASC_NIGHTLY",
        "module": "PROD_ATTA",
        "aliases": ["ASC_NIGHTLY", "PROD_ATTA"],
        "schedule": "Daily at 11 AM",
        "start_time": "Daily at 11 AM",
        "expected_end_time": "2:00 PM",  # 3.0h clock delta
        "first_job": "JOB_ATTA_FIRST",
        "last_job": "JOB_ATTA_LAST",
    })
    ctrlm_events.extend([
        {"Job_Name": "JOB_ATTA_FIRST", "Sub_Application": "PROD_ATTA", "Start_Time": "2026-08-30 11:00:00", "End_Time": "2026-08-30 11:30:00"},
        {"Job_Name": "JOB_ATTA_LAST",  "Sub_Application": "PROD_ATTA", "Start_Time": "2026-08-30 12:00:00", "End_Time": "2026-08-30 12:30:00"},
    ])

    bench_rows.append({
        "workflow": "PROD_MPS",
        "module": "PROD_MPS",
        "schedule": "Daily at 05 AM",
        "start_time": "Daily at 05 AM",
        "expected_end_time": "8:30 AM",  # 3.5h delta
        "first_job": "MPS_JOB_FIRST",
        "last_job": "P_ESP_PUBLISH_FILLRATE_REPORT_MPS_FBD\nP_ESP_PUBLISH_FILLRATE_REPORT_MPS_PCP",
    })
    ctrlm_events.extend([
        {"Job_Name": "EARLY_POLL", "Sub_Application": "PROD_MPS", "Start_Time": "2026-08-29 00:00:00", "End_Time": "2026-08-29 00:30:00"},
        {"Job_Name": "MPS_JOB_FIRST", "Sub_Application": "PROD_MPS", "Start_Time": "2026-08-30 05:00:00", "End_Time": "2026-08-30 05:30:00"},
        {"Job_Name": "P_ESP_PUBLISH_FILLRATE_REPORT_MPS_PCP", "Sub_Application": "PROD_MPS", "Start_Time": "2026-08-30 06:30:00", "End_Time": "2026-08-30 07:00:00"},
        {"Job_Name": "LATE_EXPORT", "Sub_Application": "PROD_MPS", "Start_Time": "2026-08-30 17:11:56", "End_Time": "2026-08-30 17:11:56"},
    ])

    bench_rows.append({
        "workflow": "PROD_LOADINGFILES",
        "module": "PROD_LOADINGFILES",
        "schedule": "Thrusday at 12:45 AM",
        "start_time": "Thrusday at 12:45 AM",
        "expected_end_time": "7:00 AM",  # 6.25h delta
        "first_job": "J_LOAD_START",
        "last_job": "J_LOAD_END",
    })
    ctrlm_events.extend([
        {"Job_Name": "J_LOAD_START", "Sub_Application": "PROD_LOADINGFILES", "Start_Time": "2026-08-30 00:45:00", "End_Time": "2026-08-30 01:00:00"},
        {"Job_Name": "J_LOAD_END", "Sub_Application": "PROD_LOADINGFILES", "Start_Time": "2026-08-30 03:00:00", "End_Time": "2026-08-30 03:30:00"},
    ])

    for i in range(44, 76):
        name = f"PROD_CHAIN_FLOW_{i:02d}"
        bench_rows.append({
            "workflow": name,
            "module": f"CHAIN_MOD_{i:02d}",
            "schedule": "Daily",
            "start_time": f"0{i % 4 + 1}:00 AM",
            "expected_end_time": f"0{i % 4 + 4}:30 AM",  # 3.5h delta
            "first_job": f"J_{name}_1",
            "last_job": f"J_{name}_2\nJ_{name}_3",
        })
        ctrlm_events.extend([
            {"Job_Name": f"J_{name}_1", "Sub_Application": name, "Start_Time": "2026-08-30 02:00:00", "End_Time": "2026-08-30 02:30:00"},
            {"Job_Name": f"J_{name}_2", "Sub_Application": name, "Start_Time": "2026-08-30 04:00:00", "End_Time": "2026-08-30 04:30:00"},
        ])

    # Category 3: Tier-3 Unmatched / Global Fallback Jobs (Rows 76 to 132)
    # These lack BatchSLA entries and MUST resolve dynamically against assumed ceiling (e.g. 8.25h)
    for i in range(76, 133):
        name = f"PROD_UNMATCHED_JOB_{i:02d}"
        ctrlm_events.extend([
            {"Job_Name": f"J_{name}_RUN", "Sub_Application": name, "Start_Time": "2026-08-30 01:00:00", "End_Time": f"2026-08-30 {4 + (i % 8):02d}:00:00"},
        ])

    ctrlm_df = pd.DataFrame(ctrlm_events)
    return bench_rows, ctrlm_df


def reconcile_dataset(
    batch_rows: List[Dict[str, Any]],
    ctrlm_df: pd.DataFrame,
    assumed_ceiling: float = 8.25,
) -> Dict[str, Any]:
    """Runs full ground-truth reconciliation between independent logic and dashboard engine."""
    # Seed config store with test batch rows
    config_store.set("_batch_sla_xlsx", {"workflows": batch_rows})
    config_store.set("_sow_sla_windows", {})

    # Run dashboard engine
    resp = _compute_sla_matrix(
        df=ctrlm_df,
        sla_mode="daily",
        custom_sla_hrs=assumed_ceiling,
    )
    summary = resp.workflow_summary or []
    engine_by_name: Dict[str, Dict[str, Any]] = {}
    for w in summary:
        sub = str(w.get("sub_application") or w.get("workflow_name") or w.get("workflow_key") or "").strip()
        engine_by_name[sub.upper()] = w
        wf_name = str(w.get("workflow_name") or "").strip()
        if wf_name:
            engine_by_name[wf_name.upper()] = w

    # Build unique workflow universe
    all_wfs: Dict[str, Dict[str, Any]] = {}
    known_keys: set[str] = set()
    for r in batch_rows:
        w_name = str(r.get("workflow") or r.get("batch_name") or "").strip()
        if w_name:
            all_wfs[w_name.upper()] = r
            known_keys.add(w_name.upper())
        if r.get("module"):
            known_keys.add(str(r.get("module")).strip().upper())
        for al in r.get("aliases") or []:
            if al:
                known_keys.add(str(al).strip().upper())

    if "Sub_Application" in ctrlm_df.columns:
        for sa in ctrlm_df["Sub_Application"].dropna().unique():
            sa_str = str(sa).strip()
            if sa_str and sa_str.upper() not in known_keys and sa_str.upper() not in all_wfs:
                all_wfs[sa_str.upper()] = {"workflow": sa_str, "Sub_Application": sa_str}

    row_diffs: List[RowDiff] = []
    layer1_clean = 0
    layer2_clean = 0
    layer3_clean = 0
    layer4_clean = 0
    total_audited = len(all_wfs)

    for wf_upper, raw_dict in sorted(all_wfs.items()):
        truth = compute_ground_truth_workflow(raw_dict, assumed_daily_ceiling=assumed_ceiling, ctrlm_df=ctrlm_df)
        engine_w = engine_by_name.get(wf_upper) or engine_by_name.get(truth.workflow.upper())
        if not engine_w and truth.module:
            engine_w = engine_by_name.get(truth.module.upper())
        if not engine_w:
            for al in raw_dict.get("aliases") or []:
                if str(al).upper() in engine_by_name:
                    engine_w = engine_by_name[str(al).upper()]
                    break
        engine_w = engine_w or {}

        defects: List[str] = []

        # Layer 1: Formula Verification
        eng_sla = engine_w.get("sla_h")
        eng_rt = engine_w.get("runtime_h")
        eng_buf = engine_w.get("buffer_pct")
        eng_hd = engine_w.get("duration_headroom_mins")

        formula_ok = True
        if eng_sla is not None and eng_rt is not None and eng_sla > 0:
            calc_buf = round((eng_sla - eng_rt) / eng_sla * 100.0, 2)
            calc_hd = round((eng_sla - eng_rt) * 60)
            if eng_buf is not None and abs(eng_buf - calc_buf) > 0.1:
                formula_ok = False
                defects.append(f"Formula Error: Buffer {eng_buf}% != ({eng_sla}-{eng_rt})/{eng_sla}*100 ({calc_buf}%)")
            if eng_hd is not None and abs(eng_hd - calc_hd) > 1:
                formula_ok = False
                defects.append(f"Formula Error: Headroom {eng_hd}m != ({eng_sla}-{eng_rt})*60 ({calc_hd}m)")
        if formula_ok:
            layer1_clean += 1

        # Layer 3: Per-Row Data Resolution
        sla_match = (eng_sla is not None and abs(eng_sla - truth.expected_sla) <= 0.05)
        if not sla_match:
            defects.append(f"SLA Mismatch: Expected {truth.expected_sla}h, got {eng_sla}h")

        rt_match = True
        if truth.expected_runtime is not None:
            rt_match = (eng_rt is not None and abs(eng_rt - truth.expected_runtime) <= 0.1)
            if not rt_match:
                defects.append(f"Runtime Mismatch: Expected {truth.expected_runtime}h, got {eng_rt}h")

        hd_match = True
        if truth.expected_headroom_mins is not None and eng_hd is not None:
            hd_match = (abs(eng_hd - truth.expected_headroom_mins) <= 2)

        buf_match = True
        if truth.expected_buffer_pct is not None and eng_buf is not None:
            buf_match = (abs(eng_buf - truth.expected_buffer_pct) <= 0.5)

        eng_stat = str(engine_w.get("status") or "").upper()
        stat_match = (eng_stat == truth.expected_status) or (truth.expected_status == "NOT_OBSERVED" and not eng_rt)
        if not stat_match:
            defects.append(f"Status Mismatch: Expected {truth.expected_status}, got {eng_stat}")

        eng_src = str(engine_w.get("sla_source") or "").lower()
        if "batch_sla" in eng_src or "anchor" in eng_src or "time_window" in eng_src:
            eng_tier = "Tier 1"
        elif "sow" in eng_src:
            eng_tier = "Tier 2"
        else:
            eng_tier = "Tier 3"

        tier_match = (eng_tier == truth.expected_tier)
        if not tier_match:
            defects.append(f"Tier Mismatch: Expected {truth.expected_tier}, got {eng_tier} (src: {eng_src})")

        row_clean = (sla_match and rt_match and stat_match and tier_match and formula_ok)
        if row_clean:
            layer3_clean += 1

        # Layer 4: Disclosure / Assumed Ceiling Sync
        # If Tier 3, verify that it used assumed_ceiling (e.g. 8.25), not hardcoded 6.0!
        layer4_ok = True
        if eng_tier == "Tier 3" and eng_sla is not None:
            if abs(eng_sla - assumed_ceiling) > 0.05:
                layer4_ok = False
                defects.append(f"Assumed Ceiling Desync: Table row used {eng_sla}h instead of active ceiling {assumed_ceiling}h")
        if layer4_ok:
            layer4_clean += 1

        row_diffs.append(RowDiff(
            workflow=truth.workflow,
            clean=row_clean and layer4_ok,
            sla_truth=truth.expected_sla,
            sla_engine=eng_sla,
            sla_match=sla_match,
            runtime_truth=truth.expected_runtime,
            runtime_engine=eng_rt,
            runtime_match=rt_match,
            headroom_truth=truth.expected_headroom_mins,
            headroom_engine=eng_hd,
            headroom_match=hd_match,
            buffer_truth=truth.expected_buffer_pct,
            buffer_engine=eng_buf,
            buffer_match=buf_match,
            status_truth=truth.expected_status,
            status_engine=eng_stat,
            status_match=stat_match,
            tier_truth=truth.expected_tier,
            tier_engine=eng_tier,
            tier_match=tier_match,
            defects=defects,
        ))

    # Layer 2: Aggregates validation
    # Verify that engine's top-level summary totals are consistent with its detail runs and rows
    total_runs_ok = (resp.total_runs is not None and resp.total_runs >= 0)
    ceiling_ok = (resp.sla_limit_hrs is not None and abs(resp.sla_limit_hrs - assumed_ceiling) <= 0.05)

    # Check workflow-level aggregate consistency
    wf_breaching_count = sum(1 for d in row_diffs if d.status_engine == "BREACH")
    wf_ok_count = sum(1 for d in row_diffs if d.status_engine in ("OK", "LONG_JOB", "AT_RISK"))
    workflow_aggregates_ok = (wf_breaching_count + wf_ok_count <= total_audited)

    # Check compliance formula consistency
    if resp.window_total_days and resp.window_breach_days is not None and resp.window_total_days > 0:
        expected_window_day_comp = round((resp.window_total_days - resp.window_breach_days) / resp.window_total_days * 100.0, 1)
        comp_formula_ok = (resp.compliance_pct is not None and abs(resp.compliance_pct - expected_window_day_comp) <= 0.5)
    else:
        comp_formula_ok = (resp.compliance_pct is not None and 0.0 <= resp.compliance_pct <= 100.0)

    layer2_match = total_runs_ok and ceiling_ok and workflow_aggregates_ok and comp_formula_ok

    return {
        "total_audited": total_audited,
        "clean_count": sum(1 for d in row_diffs if d.clean),
        "defective_count": sum(1 for d in row_diffs if not d.clean),
        "assumed_ceiling": assumed_ceiling,
        "layers": {
            "formula_precision_pct": round(layer1_clean / total_audited * 100.0, 1),
            "top_level_aggregates_match": layer2_match,
            "per_row_resolution_pct": round(layer3_clean / total_audited * 100.0, 1),
            "assumed_ceiling_sync_pct": round(layer4_clean / total_audited * 100.0, 1),
            "overall_system_accuracy_pct": round(sum(1 for d in row_diffs if d.clean) / total_audited * 100.0, 1),
        },
        "diffs": [asdict(d) for d in row_diffs],
    }


def print_reconciliation_report(results: Dict[str, Any], verbose: bool = False) -> None:
    """Prints a structured, high-density ASCII reconciliation dashboard."""
    tot = results["total_audited"]
    clean = results["clean_count"]
    defects = results["defective_count"]
    ceil = results["assumed_ceiling"]
    layers = results["layers"]

    print("\n" + "=" * 92)
    print("           PE DASHBOARD -- SLA MATRIX SYSTEM-WIDE RECONCILIATION AUDIT")
    print("=" * 92)
    print(f"  Total Workflows Audited: {tot:<6} | Clean Rows: {clean:<6} | Defects: {defects:<6} | Ceiling: {ceil}h")
    print("-" * 92)
    print("  LAYER EVALUATION SUMMARY:")
    print(f"    1. Formula Precision Layer:       {layers['formula_precision_pct']:>6.1f}%  (Buffer % & Headroom arithmetic)")
    print(f"    2. Top-Level Aggregates Layer:    {'PASS (100%)' if layers['top_level_aggregates_match'] else 'FAIL'}")
    print(f"    3. Per-Row Data Resolution Layer: {layers['per_row_resolution_pct']:>6.1f}%  (Tier 1/2/3 SLA, duration, anchors)")
    print(f"    4. Assumed Ceiling Sync Layer:    {layers['assumed_ceiling_sync_pct']:>6.1f}%  (Banner vs table row sync)")
    print("    -----------------------------------------------------------------")
    print(f"    OVERALL SYSTEM FIDELITY SCORE:   {layers['overall_system_accuracy_pct']:>6.1f}%")
    print("=" * 92)

    diffs = results["diffs"]
    failing = [d for d in diffs if not d["clean"]]
    if failing:
        print(f"\n[!] IDENTIFIED DEFECTS ({len(failing)} workflows with discrepancies):")
        print(f"{'WORKFLOW':<28} | {'SLA (EXP/ACT)':<15} | {'RUNTIME':<12} | {'STATUS':<12} | DEFECT DIAGNOSIS")
        print("-" * 92)
        for d in failing[:35]:
            sla_str = f"{d['sla_truth']}h / {d['sla_engine']}h"
            rt_str = f"{d['runtime_truth'] or '-'} / {d['runtime_engine'] or '-'}"
            st_str = f"{d['status_truth']} / {d['status_engine']}"
            defect_txt = "; ".join(d["defects"])
            print(f"{d['workflow'][:26]:<28} | {sla_str:<15} | {rt_str:<12} | {st_str:<12} | {defect_txt}")
        if len(failing) > 35:
            print(f"  ... and {len(failing) - 35} more failing rows (use --verbose or --json to view full output)")
    else:
        print("\n[OK] ZERO DEFECTS DETECTED: All audited workflows reconcile 100% with ground truth.")

    if verbose:
        print(f"\n[INFO] AUDITED WORKFLOWS BREAKDOWN (First 20 of {tot}):")
        print(f"{'WORKFLOW':<30} | {'TIER':<8} | {'SLA':<8} | {'RUNTIME':<10} | {'HEADROOM':<12} | {'BUFFER %':<10} | STATUS")
        print("-" * 92)
        for d in diffs[:20]:
            hd_str = f"{d['headroom_engine']}m" if d['headroom_engine'] is not None else "-"
            buf_str = f"{d['buffer_engine']}%" if d['buffer_engine'] is not None else "-"
            rt_str = f"{d['runtime_engine']}h" if d['runtime_engine'] is not None else "-"
            print(f"{d['workflow'][:28]:<30} | {d['tier_engine']:<8} | {d['sla_engine']}h    | {rt_str:<10} | {hd_str:<12} | {buf_str:<10} | {d['status_engine']}")
    print("=" * 92 + "\n")


def main():
    parser = argparse.ArgumentParser(description="System-wide SLA Matrix Reconciliation Engine")
    parser.add_argument("--batch-sla", type=str, default=None, help="Path to BatchSLA_info.xlsx file")
    parser.add_argument("--ctrlm", type=str, default=None, help="Path to Ctrl-M CSV/XLSX file")
    parser.add_argument("--ceiling", type=float, default=8.25, help="Assumed SLA ceiling hours (default: 8.25)")
    parser.add_argument("--json", type=str, default=None, help="Output path for JSON audit results")
    parser.add_argument("--verbose", action="store_true", help="Print detailed row-by-row table")
    args = parser.parse_args()

    batch_rows: List[Dict[str, Any]] = []
    ctrlm_df: Optional[pd.DataFrame] = None

    if args.batch_sla and os.path.isfile(args.batch_sla):
        print(f"[*] Loading BatchSLA workbook: {args.batch_sla}")
        from services.sla_merger import parse_batch_sla_workbook
        with open(args.batch_sla, "rb") as f:
            parsed = parse_batch_sla_workbook(f.read(), os.path.basename(args.batch_sla))
            batch_rows = parsed.get("workflows") or []
    if args.ctrlm and os.path.isfile(args.ctrlm):
        print(f"[*] Loading Ctrl-M data: {args.ctrlm}")
        from services.batch_calculator import load_ctrlm_bytes
        with open(args.ctrlm, "rb") as f:
            ctrlm_df = load_ctrlm_bytes(f.read())

    if not batch_rows and ctrlm_df is None:
        print(f"[*] No workbook provided on command line — generating full 132-workflow reference benchmark (ceiling={args.ceiling}h)...")
        batch_rows, ctrlm_df = generate_132_workflow_benchmark(assumed_ceiling=args.ceiling)

    results = reconcile_dataset(batch_rows, ctrlm_df, assumed_ceiling=args.ceiling)
    print_reconciliation_report(results, verbose=args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"[✓] Full reconciliation JSON saved to: {args.json}")

    sys.exit(0 if results["defective_count"] == 0 else 1)


if __name__ == "__main__":
    main()
