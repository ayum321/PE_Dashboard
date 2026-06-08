"""
Senior PE Consultant — cross-pillar interconnection engine.

POST /api/pe-consultant
    Takes the outputs of the three operational pillars:
      - SLA Matrix     (/api/sla-matrix     or /api/sla-matrix/json)
      - PE Findings    (/api/generate-findings)
      - Red Flags & RCA (/api/red-flags)
    plus the supporting batch + resource context, and returns:

      1. cross_links — deterministic linkage between pillars
         (same job / sub-app / host appearing in 2+ pillars)
      2. evidence_chain — per-job audit trail across pillars
      3. consultant — LLM-driven Senior PE Consultant verdict
         (Grade, Score, Decision, top risks, predictions, next actions)
      4. accuracy   — self-check signal: confidence + missing inputs

The endpoint is read-only over the three pillar payloads. It does not
re-run the underlying calculators — the upstream results are the
single source of truth.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from services.pe_utils import coerce_float as _f, coerce_int as _i

router = APIRouter()


# ── Request / Response models ───────────────────────────────────

class PeConsultantRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Pillar outputs
    sla_matrix: Optional[Dict[str, Any]] = None   # /api/sla-matrix response
    findings:   Optional[Dict[str, Any]] = None   # /api/generate-findings response
    red_flags:  Optional[Dict[str, Any]] = None   # /api/red-flags response

    # Supporting context (so consultant can reason about scale + infra)
    batch_kpis:       Optional[Dict[str, Any]]       = None
    top_jobs:         Optional[List[Dict[str, Any]]] = None
    top_breaches:     Optional[List[Dict[str, Any]]] = None
    resource_kpis:    Optional[Dict[str, Any]]       = None
    servers:          Optional[List[Dict[str, Any]]] = None
    customer_name:    Optional[str]                  = None
    # Expanded batch + SLA detail
    workflow_summary: Optional[List[Dict[str, Any]]] = None  # per-workflow resolved SLA
    batch_window:     Optional[List[Dict[str, Any]]] = None  # daily window time-series
    batch_anomalies:  Optional[List[Dict[str, Any]]] = None  # detected anomalies
    batch_sub_stats:  Optional[List[Dict[str, Any]]] = None  # per-sub-app breakdown


class CrossLink(BaseModel):
    """A single piece of evidence that appears in two or more pillars."""
    entity:        str               # job name / host / sub-app
    entity_kind:   str               # "job" | "host" | "sub_application"
    pillars:       List[str]         # subset of: ["sla","findings","redflags"]
    sla_evidence:       Optional[Dict[str, Any]] = None  # {peak_hrs, sla_limit_hrs, status, breach_margin}
    findings_evidence:  Optional[List[Dict[str, Any]]] = None  # [{level, text, source}]
    redflags_evidence:  Optional[List[Dict[str, Any]]] = None  # [{id, risk, category, question}]
    severity:      str               # CRITICAL | HIGH | MEDIUM | LOW
    confidence:    int               # 0..100


class EvidenceChain(BaseModel):
    job_name:      str
    sub_application: Optional[str] = None
    chain:         List[Dict[str, Any]]   # ordered narrative steps
    verdict:       str                    # e.g. "Confirmed runtime breach + at-risk infra"


class ConsultantVerdict(BaseModel):
    grade:        str = "—"
    score:        float = 0.0
    decision:     str = "INSUFFICIENT_DATA"   # GO | HOLD | REMEDIATE | INSUFFICIENT_DATA
    headline:     str = ""
    top_risks:    List[str] = []
    predictions:  List[str] = []
    next_actions: List[str] = []
    narrative:    str = ""
    model:        str = ""
    # Agentic upgrade — list of tool calls the LLM made to gather evidence,
    # so the UI can show *why* the verdict reads the way it does.
    agent_trace:  List[Dict[str, Any]] = []
    tool_count:   int = 0


class AccuracySignal(BaseModel):
    pillars_loaded:  Dict[str, bool]
    coverage_pct:    int
    confidence:      int
    missing_inputs:  List[str]
    notes:           List[str]


class PeConsultantResponse(BaseModel):
    cross_links:    List[CrossLink]
    evidence_chain: List[EvidenceChain]
    consultant:     ConsultantVerdict
    accuracy:       AccuracySignal


# ── Helpers ─────────────────────────────────────────────────────

def _norm(name: Any) -> str:
    return str(name or "").strip().upper()


def _index_findings_by_job(findings: List[Dict[str, Any]],
                           job_names: set[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Match findings whose text/sub mentions any job in `job_names`."""
    idx: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in findings:
        blob = (f.get("text", "") + " " + f.get("sub", "")).upper()
        for jn in job_names:
            if jn and jn in blob:
                idx[jn].append({
                    "level":  f.get("level"),
                    "text":   f.get("text"),
                    "source": f.get("source"),
                })
    return idx


def _index_redflags_by_job(flags: List[Dict[str, Any]],
                           job_names: set[str]) -> Dict[str, List[Dict[str, Any]]]:
    idx: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for fl in flags:
        blob = (fl.get("context", "") + " " + fl.get("data_point", "")).upper()
        for jn in job_names:
            if jn and jn in blob:
                idx[jn].append({
                    "id":       fl.get("id"),
                    "risk":     fl.get("risk"),
                    "category": fl.get("category"),
                    "question": fl.get("question"),
                })
    return idx


def _index_findings_by_host(findings: List[Dict[str, Any]],
                            hosts: set[str]) -> Dict[str, List[Dict[str, Any]]]:
    idx: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in findings:
        blob = (f.get("text", "") + " " + f.get("sub", "")).upper()
        for h in hosts:
            if h and h in blob:
                idx[h].append({
                    "level":  f.get("level"),
                    "text":   f.get("text"),
                    "source": f.get("source"),
                })
    return idx


def _index_redflags_by_host(flags: List[Dict[str, Any]],
                            hosts: set[str]) -> Dict[str, List[Dict[str, Any]]]:
    idx: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for fl in flags:
        blob = (fl.get("context", "") + " " + fl.get("data_point", "")).upper()
        for h in hosts:
            if h and h in blob:
                idx[h].append({
                    "id":       fl.get("id"),
                    "risk":     fl.get("risk"),
                    "category": fl.get("category"),
                    "question": fl.get("question"),
                })
    return idx


def _severity_from_status(status: str, fl_max: str, fd_max: str) -> str:
    order = {"CRITICAL": 4, "HIGH": 3, "BREACH": 4, "AT_RISK": 3,
             "MEDIUM": 2, "WARNING": 2, "LOW": 1, "INFO": 1, "OK": 0}
    sev = max(order.get(status.upper(), 0),
              order.get(fl_max.upper(), 0),
              order.get(fd_max.upper(), 0))
    if sev >= 4: return "CRITICAL"
    if sev == 3: return "HIGH"
    if sev == 2: return "MEDIUM"
    return "LOW"


def _build_cross_links(req: PeConsultantRequest) -> tuple[list[CrossLink], list[EvidenceChain]]:
    sla = req.sla_matrix or {}
    findings_resp = req.findings or {}
    rf_resp = req.red_flags or {}

    sla_breaches = sla.get("breaches") or []
    sla_jobs     = sla.get("job_summary") or []
    findings_list = findings_resp.get("findings") or []
    rf_list = rf_resp.get("flags") or []

    # Collect candidate jobs from SLA matrix (breach + at-risk)
    job_breach_map: Dict[str, Dict[str, Any]] = {}
    for b in sla_breaches:
        nm = _norm(b.get("job_name"))
        if not nm:
            continue
        prev = job_breach_map.get(nm)
        if not prev or _f(b.get("run_hrs")) > _f(prev.get("run_hrs")):
            job_breach_map[nm] = b

    # Also pull worst peak per job from job_summary if not in breaches
    for js in sla_jobs:
        nm = _norm(js.get("job_name"))
        if not nm or nm in job_breach_map:
            continue
        if _i(js.get("breach_runs")) > 0 or _i(js.get("atrisk_runs")) > 0:
            job_breach_map[nm] = {
                "job_name":          js.get("job_name"),
                "sub_application":   "",
                "run_hrs":           js.get("peak_hrs"),
                "sla_limit_hrs":     sla.get("sla_limit_hrs"),
                "breach_margin_hrs": _f(js.get("peak_hrs")) - _f(sla.get("sla_limit_hrs")),
                "status":            "BREACH" if _i(js.get("breach_runs")) > 0 else "AT_RISK",
            }

    # Also include statistical outliers (z-score > 2) — these are within SLA but
    # running significantly above their own baseline.  A job that ran 5× its
    # baseline while still under the 6h ceiling is invisible to breach/at-risk
    # filters, but if it also appears in Findings or Red Flags it IS cross-pillar
    # evidence and must surface here.
    sla_outliers = sla.get("outliers") or []
    _outlier_jobs: Dict[str, Dict[str, Any]] = {}
    for o in sla_outliers:
        nm = _norm(o.get("job_name") or o.get("job") or "")
        if not nm or nm in job_breach_map:
            continue   # already captured as breach/at-risk — skip
        _outlier_jobs[nm] = {
            "job_name":       o.get("job_name") or o.get("job") or nm,
            "sub_application": o.get("sub_application", ""),
            "run_hrs":        o.get("peak_hrs") or o.get("run_hrs"),
            "sla_limit_hrs":  sla.get("sla_limit_hrs"),
            "breach_margin_hrs": 0.0,   # within SLA — no breach margin
            "status":         "STATISTICAL_OUTLIER",
            "z_score":        o.get("z_score"),
        }

    job_keys = set(job_breach_map.keys())
    fidx_jobs = _index_findings_by_job(findings_list, job_keys)
    ridx_jobs = _index_redflags_by_job(rf_list, job_keys)

    # Outlier candidate keys (separate lookup — only links when multi-pillar)
    _outlier_keys = set(_outlier_jobs.keys())
    fidx_outliers = _index_findings_by_job(findings_list, _outlier_keys)
    ridx_outliers = _index_redflags_by_job(rf_list, _outlier_keys)

    cross: list[CrossLink] = []
    chains: list[EvidenceChain] = []

    for jn, b in job_breach_map.items():
        fevd = fidx_jobs.get(jn) or []
        revd = ridx_jobs.get(jn) or []
        pillars = ["sla"]
        if fevd: pillars.append("findings")
        if revd: pillars.append("redflags")

        # Only include cross-links that actually appear in ≥2 pillars
        if len(pillars) < 2:
            continue

        fl_max = max((x.get("risk", "LOW") for x in revd), default="LOW")
        fd_max = max((x.get("level", "info").upper() for x in fevd), default="INFO")
        # Map finding level → risk-ish word
        if fd_max == "CRITICAL": fd_max = "CRITICAL"
        elif fd_max == "WARNING": fd_max = "MEDIUM"
        else: fd_max = "LOW"

        sev = _severity_from_status(b.get("status", ""), fl_max, fd_max)

        cross.append(CrossLink(
            entity=b.get("job_name") or jn,
            entity_kind="job",
            pillars=pillars,
            sla_evidence={
                "peak_hrs":          _f(b.get("run_hrs")),
                "sla_limit_hrs":     _f(b.get("sla_limit_hrs")),
                "status":            b.get("status"),
                "breach_margin_hrs": _f(b.get("breach_margin_hrs")),
                "sub_application":   b.get("sub_application", ""),
            },
            findings_evidence=fevd or None,
            redflags_evidence=revd or None,
            severity=sev,
            confidence=90 if len(pillars) == 3 else 75,
        ))

        # Evidence chain narrative
        chain_steps = [
            {"pillar": "SLA Matrix",
             "fact":  f"{b.get('status')} — peak {_f(b.get('run_hrs')):.2f}h vs "
                      f"{_f(b.get('sla_limit_hrs')):.1f}h SLA "
                      f"(margin {_f(b.get('breach_margin_hrs')):+.2f}h)"},
        ]
        for fe in fevd[:2]:
            chain_steps.append({"pillar": f"Finding · {fe.get('level','').upper()}",
                                "fact": fe.get("text", "")})
        for re in revd[:2]:
            chain_steps.append({"pillar": f"Red Flag {re.get('id','')} · {re.get('risk','')}",
                                "fact": re.get("question", "")})

        verdict_word = "Confirmed cross-pillar risk" if len(pillars) == 3 else "Two-pillar evidence"
        chains.append(EvidenceChain(
            job_name=b.get("job_name") or jn,
            sub_application=b.get("sub_application") or None,
            chain=chain_steps,
            verdict=f"{verdict_word} — severity {sev}",
        ))

    # ── Statistical-outlier cross-links ─────────────────────────
    # Jobs within SLA but > 2σ above their own baseline.  Only linked when
    # the same job also appears in Findings OR Red Flags (≥2 pillars).
    # Severity capped at MEDIUM — no breach, but warrants monitoring.
    for jn, b in _outlier_jobs.items():
        fevd = fidx_outliers.get(jn) or []
        revd = ridx_outliers.get(jn) or []
        if not fevd and not revd:
            continue   # only in SLA outliers — single pillar, skip
        pillars = ["sla"]
        if fevd: pillars.append("findings")
        if revd: pillars.append("redflags")
        z_str = f" (z={_f(b.get('z_score')):.2f})" if b.get("z_score") else ""
        chain_steps = [
            {"pillar": "SLA Matrix (outlier)",
             "fact": f"STATISTICAL_OUTLIER — peak {_f(b.get('run_hrs')):.2f}h within "
                     f"{_f(b.get('sla_limit_hrs')):.1f}h SLA{z_str}"},
        ]
        for fe in fevd[:2]:
            chain_steps.append({"pillar": f"Finding · {fe.get('level','').upper()}",
                                 "fact": fe.get("text", "")})
        for re in revd[:2]:
            chain_steps.append({"pillar": f"Red Flag {re.get('id','')} · {re.get('risk','')}",
                                 "fact": re.get("question", "")})
        cross.append(CrossLink(
            entity=b.get("job_name") or jn,
            entity_kind="job",
            pillars=pillars,
            sla_evidence={
                "peak_hrs":          _f(b.get("run_hrs")),
                "sla_limit_hrs":     _f(b.get("sla_limit_hrs")),
                "status":            "STATISTICAL_OUTLIER",
                "breach_margin_hrs": 0.0,
                "z_score":           b.get("z_score"),
                "sub_application":   b.get("sub_application", ""),
            },
            findings_evidence=fevd or None,
            redflags_evidence=revd or None,
            severity="MEDIUM",   # within SLA — capped at MEDIUM
            confidence=70,
        ))
        chains.append(EvidenceChain(
            job_name=b.get("job_name") or jn,
            sub_application=b.get("sub_application") or None,
            chain=chain_steps,
            verdict=f"Statistical outlier confirmed in {len(pillars)} pillar(s) — severity MEDIUM",
        ))

    # ── Host-level cross-links ───────────────────────────────────
    hosts = {_norm(s.get("host")) for s in (req.servers or []) if s.get("host")}
    hot_hosts = {
        _norm(s.get("host")) for s in (req.servers or [])
        if _f(s.get("cpu_used")) >= 80 or _f(s.get("mem_used")) >= 85
    }
    if hot_hosts:
        fidx_h = _index_findings_by_host(findings_list, hot_hosts)
        ridx_h = _index_redflags_by_host(rf_list, hot_hosts)
        for h in hot_hosts:
            fevd = fidx_h.get(h) or []
            revd = ridx_h.get(h) or []
            if not fevd and not revd:
                continue
            pillars = []
            if fevd: pillars.append("findings")
            if revd: pillars.append("redflags")
            if len(pillars) < 2:
                continue
            srv = next((s for s in (req.servers or []) if _norm(s.get("host")) == h), {})
            cross.append(CrossLink(
                entity=srv.get("host") or h,
                entity_kind="host",
                pillars=pillars,
                sla_evidence={
                    "cpu_used": _f(srv.get("cpu_used")),
                    "mem_used": _f(srv.get("mem_used")),
                    "role":     srv.get("type") or "",
                },
                findings_evidence=fevd or None,
                redflags_evidence=revd or None,
                severity=_severity_from_status(
                    "CRITICAL" if _f(srv.get("cpu_used")) >= 90 or _f(srv.get("mem_used")) >= 90 else "HIGH",
                    max((x.get("risk", "LOW") for x in revd), default="LOW"),
                    "CRITICAL" if any(x.get("level") == "critical" for x in fevd) else "MEDIUM",
                ),
                confidence=80,
            ))

    # Sort: severity then number of pillars
    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    cross.sort(key=lambda c: (sev_rank.get(c.severity, 9), -len(c.pillars)))
    return cross, chains


def _accuracy(req: PeConsultantRequest, cross: list[CrossLink]) -> AccuracySignal:
    pillars_loaded = {
        "sla":          bool((req.sla_matrix or {}).get("total_runs")),
        "findings":     bool((req.findings or {}).get("findings")),
        "redflags":     bool((req.red_flags or {}).get("flags")),
        "batch_review": bool((req.batch_kpis or {}).get("total_runs")),
        "resource":     (req.resource_kpis or {}).get("fleet_grade") not in (None, "N/A", "", 0),
    }
    loaded_count = sum(1 for v in pillars_loaded.values() if v)
    coverage = int(loaded_count / 5 * 100)

    missing: list[str] = []
    if not pillars_loaded["sla"]:          missing.append("SLA Matrix not computed — run from SLA tab")
    if not pillars_loaded["findings"]:     missing.append("PE Findings not generated")
    if not pillars_loaded["redflags"]:     missing.append("Red Flags not generated")
    if not pillars_loaded["batch_review"]: missing.append("Batch Review not uploaded — upload Ctrl-M export")
    if not pillars_loaded["resource"]:     missing.append("Resource data not uploaded — upload server fleet file")

    notes: list[str] = []
    if cross:
        notes.append(f"{len(cross)} cross-pillar link(s) — same evidence appears in ≥2 layers")
    else:
        if loaded_count >= 3:
            notes.append(f"{loaded_count}/5 data sources loaded — no overlapping entities detected")

    wf_breaches = [w for w in (req.workflow_summary or []) if (w.get("status") or "").upper() == "BREACH"]
    if wf_breaches:
        notes.append(f"{len(wf_breaches)} workflow(s) in BREACH across all batch runs")

    anomaly_count = len(req.batch_anomalies or [])
    if anomaly_count:
        notes.append(f"{anomaly_count} batch anomaly(ies) detected")

    # Confidence: weighted — all 5 sources loaded + batch data density
    bk = req.batch_kpis or {}
    batch_conf = _i((bk.get("data_coverage") or {}).get("confidence"), 100)
    confidence = int(coverage * 0.6 + batch_conf * 0.4)

    return AccuracySignal(
        pillars_loaded=pillars_loaded,
        coverage_pct=coverage,
        confidence=confidence,
        missing_inputs=missing,
        notes=notes,
    )


def _build_consultant_digest(req: PeConsultantRequest, cross: list[CrossLink]) -> Dict[str, Any]:
    """Build a ConsultantDigest dict from the request + cross-links.
    Shape: services.digest_schemas.ConsultantDigest
    Never pass this to pe_narrative functions — different key layout.
    """
    sla = req.sla_matrix or {}
    findings_resp = req.findings or {}
    rf_resp = req.red_flags or {}
    bk = req.batch_kpis or {}
    rk = req.resource_kpis or {}

    # Workflow-level breach detail from SLA Matrix (per-workflow resolved SLA)
    wf_summary = req.workflow_summary or []
    wf_breaches = [
        {"workflow":          w.get("workflow"),
         "sub_app":           w.get("sub_app"),
         "elapsed_hrs":       w.get("avg_elapsed_hrs") or w.get("peak_elapsed_hrs"),
         "sla_limit_hrs":     w.get("sla_limit_hrs"),
         "buffer_pct":        w.get("buffer_pct"),
         "breach_run_count":  w.get("breach_run_count"),
         "breach_run_dates":  (w.get("breach_run_dates") or [])[:3],
         "failed_job_count":  w.get("failed_job_count"),
         "sow_buffer_hrs":    w.get("sow_buffer_hrs"),
         "clock_sla_status":  w.get("clock_sla_status"),
         "anchor_used":       w.get("anchor_used"),
        }
        for w in wf_summary
        if (w.get("status") or "").upper() == "BREACH"
    ][:8]

    # Batch window: count days exceeding SLA window
    window_rows = req.batch_window or []
    window_breach_days = [
        {"date": r.get("run_date"), "elapsed_hrs": r.get("elapsed_hrs") or r.get("total_hrs")}
        for r in window_rows
        if (r.get("status") or "").upper() == "BREACH"
    ][:5]

    # Resource: top 3 most stressed servers
    top_stressed = sorted(
        [s for s in (req.servers or []) if s.get("host")],
        key=lambda s: max(_f(s.get("cpu_used")), _f(s.get("mem_used"))),
        reverse=True,
    )[:3]

    # Anomalies summary
    anomalies = (req.batch_anomalies or [])[:5]

    return {
        "customer": req.customer_name or "Unknown",
        "sla": {
            "mode":              sla.get("sla_mode"),
            "limit_hrs":         sla.get("sla_limit_hrs"),
            "compliance_pct":    sla.get("compliance_pct"),
            "run_sla_compliance_pct":  sla.get("compliance_pct"),
            "window_compliance_pct":   bk.get("window_compliance_pct") or bk.get("batch_window_compliance"),
            "batch_window_compliance": bk.get("batch_window_compliance"),
            "breach":            sla.get("breaching_runs"),
            "at_risk":           sla.get("at_risk_runs"),
            "ok":                sla.get("ok_runs"),
            "worst_job":         sla.get("worst_job"),
            "worst_hrs":         sla.get("worst_hrs"),
            "worst_margin":      sla.get("worst_margin_hrs"),
            "top_breaches":      (sla.get("breaches") or [])[:5],
            "workflow_breaches": wf_breaches,
            "workflow_count":    len(wf_summary),
        },
        "findings_summary": findings_resp.get("summary") or {},
        "top_findings": [
            {"level": f.get("level"), "text": f.get("text"),
             "source": f.get("source"), "recommendation": f.get("recommendation")}
            for f in (findings_resp.get("findings") or [])[:8]
            if f.get("level") in ("critical", "warning")
        ],
        "red_flags_by_risk": rf_resp.get("by_risk") or {},
        "top_red_flags": [
            {"id": fl.get("id"), "risk": fl.get("risk"),
             "category": fl.get("category"), "question": fl.get("question"),
             "context": fl.get("context")}
            for fl in (rf_resp.get("flags") or [])[:8]
            if fl.get("risk") in ("CRITICAL", "HIGH")
        ],
        "cross_links": [c.model_dump() for c in cross[:8]],
        "batch_kpis": {
            "compliance_pct":        bk.get("compliance_pct"),
            "total_runs":            bk.get("total_runs"),
            "total_jobs":            bk.get("total_jobs"),
            "jobs_breach":           bk.get("jobs_breach"),
            "jobs_at_risk":          bk.get("jobs_at_risk"),
            "failed_runs":           bk.get("failed_runs"),
            "fail_rate_pct":         bk.get("fail_rate_pct"),
            "batch_window_compliance": bk.get("batch_window_compliance"),
            "window_breach_days":    bk.get("window_breach_days"),
        },
        "batch_window": {
            "breach_days":     window_breach_days,
            "breach_day_count": len(window_breach_days),
        },
        "anomalies": {
            "count":    len(anomalies),
            "top":      anomalies,
        },
        "fleet": {
            "grade":           rk.get("fleet_grade"),
            "score":           rk.get("fleet_score"),
            "n_critical":      rk.get("n_critical"),
            "n_warning":       rk.get("n_warning"),
            "total":           rk.get("total_servers"),
            "known_pct":       rk.get("known_pct"),
            "top_stressed":    [
                {"host": s.get("host"), "cpu": _f(s.get("cpu_used")),
                 "mem": _f(s.get("mem_used")), "role": s.get("type")}
                for s in top_stressed
            ],
        },
    }


def _consultant_llm(digest: Dict[str, Any], accuracy: AccuracySignal) -> ConsultantVerdict:
    """Run Senior PE Consultant LLM. Falls back to deterministic verdict if AI unavailable."""
    try:
        from services.ai_engine import chat as _chat, is_ready
        ready = is_ready()
        ai_available = bool(ready.get("nvidia_key") or ready.get("gemini_key"))
    except Exception:
        ai_available = False

    # ── Deterministic baseline verdict (always computed) ──
    # Score: weighted compliance + finding criticals + red flag criticals
    # + resource fleet + batch window + fail rate + anomalies
    #
    # Use window-level compliance as the baseline.  If the SLA matrix
    # supplied a run-level metric, keep it for reference but don't seed the
    # score with it — the window metric is the contractual measure.
    sla_d = digest.get("sla") or {}
    _raw_comp = (sla_d.get("window_compliance_pct")
                 or sla_d.get("batch_window_compliance")
                 or sla_d.get("compliance_pct"))
    sla_comp = _f(_raw_comp, 100.0)
    fc  = _i((digest.get("findings_summary") or {}).get("critical"))
    fw  = _i((digest.get("findings_summary") or {}).get("warning"))
    rfc = _i((digest.get("red_flags_by_risk") or {}).get("CRITICAL"))
    rfh = _i((digest.get("red_flags_by_risk") or {}).get("HIGH"))

    # Batch review signals
    batch_d = digest.get("batch_kpis") or {}
    fail_rate_pct    = _f(batch_d.get("fail_rate_pct"), 0.0)
    jobs_breach      = _i(batch_d.get("jobs_breach"))
    win_breach_days  = _i(batch_d.get("window_breach_days") or
                          digest.get("batch_window", {}).get("breach_day_count"))
    wf_breach_count  = len(digest.get("sla", {}).get("workflow_breaches") or [])

    # Resource fleet signals
    fleet_d          = digest.get("fleet") or {}
    n_crit_servers   = _i(fleet_d.get("n_critical"))
    fleet_score      = _f(fleet_d.get("score"), 100.0)

    # Anomaly signal
    anomaly_count    = _i((digest.get("anomalies") or {}).get("count"))

    # Score 0..100
    score = sla_comp
    score -= fc   * 6.0
    score -= fw   * 1.5
    score -= rfc  * 4.0
    score -= rfh  * 1.5
    # Batch Review deductions
    score -= fail_rate_pct   * 0.5   # each 1% fail rate = -0.5pt
    score -= jobs_breach     * 0.3   # each breaching job = -0.3pt
    score -= win_breach_days * 1.0   # each window-breach day = -1pt
    score -= wf_breach_count * 1.5   # each breaching workflow = -1.5pt
    score -= anomaly_count   * 1.0   # each anomaly = -1pt
    # Resource deductions
    score -= n_crit_servers  * 3.0   # each CRITICAL server = -3pt
    if fleet_score < 70:
        score -= (70 - fleet_score) * 0.2  # weak fleet drags score

    score = max(0.0, min(100.0, score))
    if   score >= 95: grade = "A"
    elif score >= 85: grade = "B"
    elif score >= 70: grade = "C"
    elif score >= 55: grade = "D"
    else:             grade = "F"

    if   score >= 90 and fc == 0 and rfc == 0: decision = "GO"
    elif score >= 75:                           decision = "HOLD"
    elif accuracy.coverage_pct < 40:            decision = "INSUFFICIENT_DATA"
    else:                                       decision = "REMEDIATE"

    # Top deterministic risks — draw from all 5 data sources
    risks: list[str] = []
    # 1. Cross-pillar links (highest confidence)
    for c in digest.get("cross_links", [])[:3]:
        risks.append(
            f"{c['entity']} ({c['entity_kind']}) — {c['severity']} across "
            f"{', '.join(c['pillars'])}"
        )
    # 2. Workflow-level breaches from SLA Matrix
    for wf in (digest.get("sla", {}).get("workflow_breaches") or [])[:2]:
        wf_name = wf.get("workflow") or wf.get("sub_app") or "Unknown workflow"
        br_runs = wf.get("breach_run_count") or 0
        br_dates = ", ".join(wf.get("breach_run_dates") or [])
        risks.append(
            f"Workflow '{wf_name}' — {br_runs} breach run(s)"
            + (f" on {br_dates}" if br_dates else "")
            + (f"; {wf.get('failed_job_count')} failed job(s)" if wf.get("failed_job_count") else "")
        )
    # 3. Resource: critical servers
    for srv in (fleet_d.get("top_stressed") or [])[:1]:
        if srv.get("cpu", 0) >= 80 or srv.get("mem", 0) >= 80:
            risks.append(
                f"Server {srv.get('host')} ({srv.get('role') or 'unknown'}) — "
                f"CPU {srv.get('cpu'):.0f}% / MEM {srv.get('mem'):.0f}%"
            )
    # 4. Batch window breaches
    if win_breach_days:
        risks.append(f"Batch window exceeded SLA ceiling on {win_breach_days} day(s)")
    # 5. Fallbacks when no cross-links
    if not risks:
        if rfc:
            risks.append(f"{rfc} CRITICAL red flag(s) without cross-pillar overlap")
        if fc:
            risks.append(f"{fc} CRITICAL finding(s) require remediation")
        if n_crit_servers:
            risks.append(f"{n_crit_servers} CRITICAL server(s) in fleet — fleet grade {fleet_d.get('grade', '?')}")
        if not risks:
            risks.append("No CRITICAL cross-pillar risks detected across all 5 data sources")

    headline = (
        f"Grade {grade} · Score {score:.1f} · {decision} — "
        f"{len(digest.get('cross_links', []))} cross-pillar link(s), "
        f"{fc} critical finding(s), {rfc} critical red flag(s), "
        f"fleet {fleet_d.get('grade') or 'N/A'}"
    )

    # Data-driven next actions
    next_actions_default: list[str] = []
    if fc:
        next_actions_default.append(f"Remediate {fc} CRITICAL finding(s) before any production change")
    if rfc:
        next_actions_default.append(f"Assign owners to {rfc} CRITICAL red flag(s) with 48h ETAs")
    if n_crit_servers:
        next_actions_default.append(f"Investigate {n_crit_servers} CRITICAL server(s) in fleet — grade {fleet_d.get('grade','?')}")
    if win_breach_days:
        next_actions_default.append(f"Analyse {win_breach_days} batch window breach day(s) — identify peak drivers")
    if wf_breach_count:
        next_actions_default.append(f"Prioritise {wf_breach_count} breaching workflow(s) by breach run frequency")
    if not next_actions_default:
        next_actions_default.append("No CRITICAL actions required — monitor compliance trend")
    next_actions_default.append("Re-run SLA Matrix after remediation to confirm compliance improvement")
    next_actions_default = next_actions_default[:5]

    # Data-driven predictions
    predictions_default: list[str] = []
    if jobs_breach:
        predictions_default.append(f"{jobs_breach} job(s) currently breaching — likely to recur without intervention")
    if fail_rate_pct > 2:
        predictions_default.append(f"{fail_rate_pct:.1f}% job fail rate — continued failures expected in next cycle")
    if n_crit_servers:
        predictions_default.append(f"Fleet pressure from {n_crit_servers} CRITICAL server(s) will amplify during peak batch")
    if wf_breach_count and win_breach_days:
        predictions_default.append(f"{wf_breach_count} breaching workflow(s) + {win_breach_days} window day(s) — cumulative SLA risk")
    if not predictions_default:
        predictions_default.append("Current data signals acceptable — continue monitoring SLA compliance trend")

    if not ai_available:
        return ConsultantVerdict(
            grade=grade, score=round(score, 1), decision=decision,
            headline=headline,
            top_risks=risks,
            predictions=predictions_default,
            next_actions=next_actions_default,
            narrative=("AI provider not configured — deterministic Senior PE Consultant "
                       "scoring used. Configure NVIDIA NIM or Gemini key in Settings to "
                       "enable narrative analysis."),
            model="deterministic",
        )

    # ── LLM-driven Senior PE Consultant verdict ──
    system = (
        "You are a Senior Performance Engineering Consultant with 20+ years of "
        "experience auditing batch + infrastructure for Tier-1 enterprise customers. "
        "You write for a CTO and a PE lead. You quote exact numbers from the digest "
        "(no fabrication). You explicitly link findings, red flags, and SLA breaches "
        "by job/host name when possible. You state predictions with confidence levels. "
        "You separate measured facts from inference. You do not use filler language."
    )
    prompt = (
        "Three operational pillars (SLA Matrix, PE Findings, Red Flags) have been "
        "computed independently from real customer data. A deterministic cross-pillar "
        "linker has already identified entities that appear in 2+ pillars. "
        "Produce a Senior PE Consultant verdict in this exact structure:\n\n"
        "VERDICT: <one line, include grade and decision>\n"
        "TOP RISKS:\n"
        "  1. <risk> — evidence: <pillar references>\n"
        "  2. ...\n"
        "  3. ...\n"
        "PREDICTIONS (next 1-2 batch cycles, with confidence %):\n"
        "  - <prediction> [confidence X%]\n"
        "NEXT 48H ACTIONS (named owner role + concrete metric):\n"
        "  1. <action>\n"
        "  2. ...\n"
        "  3. ...\n"
        "ACCURACY NOTE: <1 line on data confidence and any missing inputs>\n\n"
        f"DIGEST:\n{digest}\n\n"
        f"ACCURACY SIGNAL: coverage={accuracy.coverage_pct}%, "
        f"confidence={accuracy.confidence}%, missing={accuracy.missing_inputs}"
    )

    try:
        # Agentic path — let the LLM call tools to fetch evidence directly
        # from session_cache (job history, host metrics, hottest hours, etc.)
        # so the verdict is grounded in row-level data instead of just the
        # pre-computed digest. Falls back to single-shot inside run_agent.
        from services.ai_agent import run_agent
        agent_trace: list = []
        tool_count = 0
        text, model, trace = run_agent(
            task=prompt,
            system=system + (
                "\n\nIMPORTANT: Before writing the verdict, call list_loaded_data, "
                "then drill into at least ONE concrete piece of evidence using "
                "get_breach_runs or get_resource_linked_runs or get_critical_servers, "
                "and quote those exact numbers in TOP RISKS."
            ),
            max_tokens=900, temperature=0.25,
        )
        agent_trace = trace
        tool_count  = sum(1 for t in trace if t.get("kind") == "tool_call")
    except Exception as exc:
        return ConsultantVerdict(
            grade=grade, score=round(score, 1), decision=decision,
            headline=headline, top_risks=risks,
            predictions=predictions_default, next_actions=next_actions_default,
            narrative=f"LLM unavailable ({exc}); deterministic scoring used.",
            model="deterministic",
        )

    # Parse the structured response into list fields (best effort)
    parsed_risks: list[str] = []
    parsed_preds: list[str] = []
    parsed_acts:  list[str] = []
    section = None
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        u = s.upper()
        if u.startswith("TOP RISKS"):     section = "risks"; continue
        if u.startswith("PREDICTIONS"):   section = "preds"; continue
        if u.startswith("NEXT 48H") or u.startswith("NEXT ACTIONS"): section = "acts"; continue
        if u.startswith("ACCURACY NOTE") or u.startswith("VERDICT"): section = None; continue
        if section is None:
            continue
        # Strip bullet/number prefix
        clean = s.lstrip("0123456789.) -•").strip()
        if not clean:
            continue
        if section == "risks" and len(parsed_risks) < 5: parsed_risks.append(clean)
        elif section == "preds" and len(parsed_preds) < 5: parsed_preds.append(clean)
        elif section == "acts"  and len(parsed_acts)  < 5: parsed_acts.append(clean)

    # ── Reconcile LLM verdict against deterministic baseline ────
    # Deterministic numbers are ground truth. If LLM contradicts data, force.
    from services.verdict_reconciler import reconcile_verdict

    # Build KPI evidence dict for validation
    kpi_ev = {
        "batch":    digest.get("batch_kpis", {}),
        "sla":      digest.get("sla", {}),
        "resource": digest.get("fleet", {}),
        "redflags": digest.get("red_flags_by_risk", {}),
        "findings": digest.get("findings_summary", {}),
        "top_jobs": [
            {"Job_Name": c.get("entity")} for c in digest.get("cross_links", [])
            if c.get("entity_kind") == "job"
        ],
        "servers":  [
            {"host": c.get("entity")} for c in digest.get("cross_links", [])
            if c.get("entity_kind") == "host"
        ],
    }

    # Parse LLM decision from the text
    llm_decision_parsed = None
    llm_grade_parsed = None
    if text:
        import re as _re
        vline = _re.search(r"VERDICT:\s*(.+)", text, _re.IGNORECASE)
        if vline:
            vtext = vline.group(1).upper()
            for d in ("REMEDIATE", "HOLD", "GO"):
                if d in vtext:
                    llm_decision_parsed = d
                    break
            gm = _re.search(r"GRADE\s+([A-F])", vtext)
            if gm:
                llm_grade_parsed = gm.group(1)

    recon = reconcile_verdict(
        det_score=round(score, 1),
        det_grade=grade,
        det_decision=decision,
        det_risks=risks,
        det_actions=next_actions_default,
        llm_grade=llm_grade_parsed,
        llm_decision=llm_decision_parsed,
        llm_risks=parsed_risks or None,
        llm_actions=parsed_acts or None,
        llm_narrative=text,
        kpi_evidence=kpi_ev,
    )

    # Build the reconciliation note for the headline
    recon_note = ""
    if recon.mismatches:
        recon_note = f" [{len(recon.mismatches)} mismatch(es) reconciled]"

    return ConsultantVerdict(
        grade=recon.final_grade,
        score=recon.final_score,
        decision=recon.final_decision,
        headline=headline + recon_note,
        top_risks=recon.final_risks,
        predictions=parsed_preds or predictions_default,
        next_actions=recon.final_next_actions,
        narrative=text or "",
        model=model or "",
        agent_trace=agent_trace,
        tool_count=tool_count,
    )


# ── Endpoint ────────────────────────────────────────────────────

@router.post("/pe-consultant", response_model=PeConsultantResponse,
             summary="Senior PE Consultant — cross-pillar interconnection + LLM verdict")
def pe_consultant(body: PeConsultantRequest) -> PeConsultantResponse:
    # ── Auto-hydrate from session_cache so the consultant works even when
    #    the frontend sends an empty/partial request body. ──────────────
    try:
        from services import session_cache
        ac = session_cache.ac_snapshot()

        if not body.sla_matrix:
            sc_sla = session_cache.get("last_sla_matrix")
            if sc_sla:
                body.sla_matrix = sc_sla
        if not body.findings:
            sc_f = session_cache.get("last_smart_findings") or session_cache.get("last_findings")
            if sc_f:
                body.findings = sc_f
        if not body.red_flags:
            sc_rf = session_cache.get("last_red_flags")
            if sc_rf:
                body.red_flags = sc_rf
        if not body.batch_kpis:
            bk = ac.get("batch_kpis")
            if bk:
                body.batch_kpis = bk
        if not body.top_jobs:
            tj = ac.get("batch_top_jobs") or ac.get("job_summary")
            if tj:
                body.top_jobs = tj
        if not body.resource_kpis:
            rs = ac.get("resource_summary") or {}
            rk = rs.get("kpis")
            if rk:
                body.resource_kpis = rk
        if not body.servers:
            rs = ac.get("resource_summary") or {}
            srv = rs.get("servers")
            if srv:
                body.servers = srv
        if not body.workflow_summary:
            wf = ac.get("workflow_sla_summary")
            if wf:
                body.workflow_summary = wf
        if not body.batch_window:
            wr = ac.get("daily_window_series")
            if wr:
                body.batch_window = wr
        if not body.batch_anomalies:
            an = ac.get("regression_df")
            if an:
                body.batch_anomalies = an
        if not body.customer_name:
            cn = ac.get("customer_name")
            if cn:
                body.customer_name = cn
    except Exception:
        pass

    cross, chains = _build_cross_links(body)
    accuracy      = _accuracy(body, cross)
    verdict       = _consultant_llm(_build_consultant_digest(body, cross), accuracy)
    return PeConsultantResponse(
        cross_links=cross,
        evidence_chain=chains,
        consultant=verdict,
        accuracy=accuracy,
    )
