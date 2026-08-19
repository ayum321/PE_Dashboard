"""
Cross-pillar Final Judgment endpoint.

POST /api/final-judgment
    body: { resource, batch, sla_matrix, benchmark, correlation, sow,
            redflags, executive }   (any subset; all optional)
    response: {
        verdict: str,
        decision: "GO" | "HOLD" | "REMEDIATE",
        grade: "A" | "B" | "C" | "D" | "F",
        score: float,         # 0..100
        pillars: { resource, batch, sla, benchmark, sow, correlation } scores,
        narrative: str,       # AI-generated unified verdict
        next_actions: List[str],
        ai_model: str,
    }

This is the single 'judge' that ties every dashboard pillar together
into one decision.  Every uploaded report contributes; the LLM produces
the human-readable verdict; deterministic scoring guarantees a numeric
output even when AI is unavailable.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

router = APIRouter()


class FinalJudgmentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    resource:    Optional[Dict[str, Any]] = None   # /api/upload payload
    batch:       Optional[Dict[str, Any]] = None   # /api/process-batch payload
    sla_matrix:  Optional[Dict[str, Any]] = None   # /api/sla-matrix payload
    benchmark:   Optional[Dict[str, Any]] = None   # /api/benchmark payload
    correlation: Optional[Dict[str, Any]] = None   # /api/correlate payload
    sow:          Optional[Dict[str, Any]] = None   # /api/sow/compare payload
    sow_contract: Optional[Dict[str, Any]] = None   # contract intelligence from Zone F upload
    redflags:     Optional[Dict[str, Any]] = None   # /api/red-flags payload
    executive:   Optional[Dict[str, Any]] = None   # /api/executive-dashboard payload


class FinalJudgmentResponse(BaseModel):
    verdict:      str
    decision:     str
    grade:        str
    score:        float
    pillars:      Dict[str, float]
    pillar_weights: Dict[str, float] = {}
    pillar_contributions: Dict[str, float] = {}
    pillar_details: Dict[str, Any] = {}        # per-pillar base/final/penalty breakdown
    evidence_chain: List[Dict[str, Any]] = []  # auditable fact→threshold→points ledger
    cross_pillar_links: List[Dict[str, Any]] = []  # computed (not LLM-guessed) correlations
    scoring_mode: str = "additive"
    verdict_reason: str = ""
    narrative:    Optional[str] = None
    next_actions: List[str]
    pillars_present: List[str]
    ai_model:     Optional[str] = None


# ── Per-pillar scoring ────────────────────────────────────────────
# The deterministic, severity-weighted pillar scores + evidence ledger +
# cross-pillar correlations now live in services/judgment_engine.py so they are
# unit-testable in isolation and shared with any future surface. This router
# orchestrates: it asks the engine for scores, builds the composite, runs the
# decision matrix, then has the LLM narrate FROM the computed evidence.
from services import judgment_engine


# ── Decision matrix ────────────────────────────────────────────────
_GRADE_LABELS = {
    "A": "APPROVED",
    "B": "APPROVED WITH NOTES",
    "C": "CONDITIONAL HOLD",
    "D": "BLOCKED — MINOR",
    "F": "BLOCKED — MAJOR",
}

def _grade(score: float) -> str:
    from services.pe_config import score_to_grade
    letter, _ = score_to_grade(score)
    return letter


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _first_not_none(*vals: Any) -> Any:
    """Coalesce that treats 0/0.0 as valid (unlike `a or b`)."""
    for v in vals:
        if v is not None:
            return v
    return None


def _fmt_pct(v: Any) -> str:
    n = _safe_float(v)
    return "NA" if n is None else f"{n:.1f}%"


def _fmt_count(v: Any) -> str:
    n = _safe_int(v)
    return "NA" if n is None else str(n)


def _build_kpi_evidence(body: FinalJudgmentRequest) -> Dict[str, Any]:
    """Extract named evidence facts used by deterministic verdict reconciliation."""
    ev: Dict[str, Any] = {}
    if body.batch:
        kpis = body.batch.get("kpis") if isinstance(body.batch.get("kpis"), dict) else {}
        dc = body.batch.get("deadline_compliance") or kpis.get("deadline_compliance") or {}
        # Day-level window compliance DERIVED from breach/total days so the verdict
        # reason's % always reconciles with its "{breach}/{total}" fraction.
        _wbd_ev = _safe_int(kpis.get("window_breach_days"))
        _wtd_ev = _safe_int(kpis.get("window_total_days"))
        _day_comp_ev = (
            round((_wtd_ev - _wbd_ev) / _wtd_ev * 100, 1)
            if _wtd_ev and _wtd_ev > 0 and _wbd_ev is not None else None
        )
        ev["batch"] = {
            "compliance_pct": kpis.get("compliance_pct"),
            "job_sla_compliance_pct": (
                kpis.get("job_sla_compliance_pct")
                or kpis.get("job_sla_compliance")
                or kpis.get("compliance_pct")
            ),
            "window_compliance_pct": _first_not_none(
                _day_comp_ev,
                kpis.get("window_day_compliance_pct"),
                kpis.get("window_compliance_pct"),
                kpis.get("batch_window_compliance"),
            ),
            "window_pair_compliance_pct": _first_not_none(
                kpis.get("window_compliance_pct"),
                kpis.get("batch_window_compliance"),
            ),
            "jobs_breach": kpis.get("jobs_breach") or kpis.get("breaching_runs"),
            "window_breach_days": kpis.get("window_breach_days"),
            "window_total_days": kpis.get("window_total_days"),
            "deadline_compliance": dc if isinstance(dc, dict) else {},
            "regression_count": kpis.get("regression_count") or kpis.get("runtime_regression_count"),
            "net_runtime_delta": kpis.get("net_runtime_delta") or kpis.get("net_runtime_delta_pct"),
        }
    if body.sla_matrix:
        ev["sla"] = {
            "compliance_pct": body.sla_matrix.get("compliance_pct"),
            "breaching_runs": body.sla_matrix.get("breaching_runs"),
            "window_breach_days": body.sla_matrix.get("window_breach_days"),
            "window_total_days": body.sla_matrix.get("window_total_days"),
        }
    if body.resource:
        rk = body.resource.get("kpis") if isinstance(body.resource.get("kpis"), dict) else {}
        ev["resource"] = {
            "n_critical": body.resource.get("n_critical") or rk.get("n_critical"),
            "avg_cpu": body.resource.get("avg_cpu") or rk.get("avg_cpu") or rk.get("avg_cpu_pct"),
            "peak_mem_pct": (
                body.resource.get("peak_mem_pct") or rk.get("peak_mem_pct")
                or rk.get("max_mem_pct") or rk.get("mem_peak_pct")
            ),
            "fleet_grade": body.resource.get("fleet_grade") or rk.get("fleet_grade"),
            "fleet_score": body.resource.get("fleet_score") or rk.get("fleet_score"),
        }
    if body.redflags:
        ev["redflags"] = body.redflags.get("by_risk") or {}
    return ev


def _deadline_fact(kpi_evidence: Dict[str, Any]) -> Optional[str]:
    dc = ((kpi_evidence.get("batch") or {}).get("deadline_compliance") or {})
    if not isinstance(dc, dict) or not dc.get("has_deadlines"):
        return None
    comp = _fmt_pct(dc.get("compliance_pct"))
    breach_days = _fmt_count(dc.get("breach_days") or dc.get("breach_windows"))
    worst = _safe_float(dc.get("worst_overrun_hrs"))
    worst_txt = "NA" if worst is None else f"{worst:.2f}h"
    return (
        f"wall-clock deadline compliance {comp} "
        f"({breach_days} breach days, worst overrun {worst_txt})"
    )


def _resource_despite(kpi_evidence: Dict[str, Any]) -> Optional[str]:
    res = kpi_evidence.get("resource") or {}
    n_critical = _safe_int(res.get("n_critical")) or 0
    grade = res.get("fleet_grade")
    score = _safe_float(res.get("fleet_score"))
    peak_mem = _safe_float(res.get("peak_mem_pct"))
    avg_cpu = _safe_float(res.get("avg_cpu"))
    if n_critical > 0 or (score is not None and score < 70):
        return None
    if not any(v is not None for v in (score, peak_mem, avg_cpu)) and not grade:
        return None
    parts = []
    if peak_mem is not None:
        parts.append(f"DB mem peak {peak_mem:.1f}%")
    if avg_cpu is not None:
        parts.append(f"avg CPU {avg_cpu:.1f}%")
    if grade:
        parts.append(f"fleet grade {grade}" + (f" ({score:.1f})" if score is not None else ""))
    return "healthy resource metrics (" + ", ".join(parts) + ")"


def _decision_with_reason(
    score: float,
    pillars: Dict[str, float],
    redflag_critical: int,
    loaded_count: int,
    kpi_evidence: Optional[Dict[str, Any]] = None,
) -> tuple:
    """Return (decision, reason) using algorithmic thresholds."""
    kpi = kpi_evidence or {}
    batch = kpi.get("batch") or {}
    window_comp = _safe_float(batch.get("window_compliance_pct"))
    window_breach_days = _safe_int(batch.get("window_breach_days"))
    window_total_days = _safe_int(batch.get("window_total_days"))
    deadline = _deadline_fact(kpi)

    def _with_support(primary: str) -> str:
        facts = [primary]
        if deadline:
            facts.append(deadline)
        res = _resource_despite(kpi)
        suffix = f", despite {res}" if res and primary.upper().startswith(("SLA", "BATCH")) else ""
        return "; ".join(facts) + suffix

    # Hard blocks
    for name, val in pillars.items():
        if val < 40:
            return ("BLOCKED",
                    _with_support(f"{name.upper()} score {val:.1f}% < 40% hard-block floor"))
    if redflag_critical >= 3:
        return ("BLOCKED",
                f"{redflag_critical} CRITICAL red-flags >= 3 hard-block floor")

    # SLA-specific hard block
    sla_score = pillars.get("sla")
    if sla_score is not None and sla_score < 50:
        if window_comp is not None:
            # window_comp is the DAY-LEVEL figure, so the clean/total day fraction
            # reconciles with it (e.g. 2/28 days == 7.1%).
            if window_breach_days is not None and window_total_days:
                clean_days = window_total_days - window_breach_days
                fact = (
                    f"batch finished within its SLA window on only "
                    f"{clean_days}/{window_total_days} day(s) ({window_comp:.1f}%, < 50% floor)"
                )
            else:
                fact = f"batch-window SLA compliance {window_comp:.1f}% < 50% floor"
        else:
            fact = f"SLA compliance {sla_score:.1f}% < 50% floor"
        return ("BLOCKED",
                _with_support(fact))

    # Resource-specific hard block
    resource_score = pillars.get("resource")
    if resource_score is not None and resource_score < 60:
        return ("BLOCKED",
                f"Resource health {resource_score:.1f}% < 60% hard-block floor")

    # Conditional hold checks
    for name, val in pillars.items():
        if val < 60:
            return ("HOLD",
                    _with_support(f"{name.upper()} score {val:.1f}% < 60% hold threshold"))

    if loaded_count < 3:
        return ("HOLD",
                f"Only {loaded_count}/6 pillars loaded — insufficient evidence for approval")

    if score < 60:
        return ("HOLD", f"Composite score {score:.1f}% < 60% minimum")

    # Approval zone
    if score >= 80:
        reason = "all loaded pillars above approval thresholds"
        return ("GO", reason)
    if score >= 70:
        return ("GO_WITH_NOTES",
                f"all loaded pillars passing; composite score {score:.1f}% < 80% observation threshold")

    return ("HOLD", f"Composite score {score:.1f}% between 60-70% review band")


def _build_digest(body: FinalJudgmentRequest, pillars: Dict[str, float]) -> Dict[str, Any]:
    """Compress every pillar into a token-light digest for the AI."""
    d: Dict[str, Any] = {"pillar_scores": pillars}

    if body.resource:
        servers = body.resource.get("servers") or []
        d["resource"] = {
            "filename":  body.resource.get("filename"),
            "count":     len(servers),
            "top3":      sorted(
                [{"host": s.get("host"),
                  "cpu":  float(s.get("cpu_used") or 0),
                  "mem":  float(s.get("mem_used") or 0),
                  "disk": float(s.get("disk_used_max") or 0)}
                 for s in servers],
                key=lambda x: x["cpu"] + x["mem"] + x["disk"], reverse=True,
            )[:3],
        }

    if body.batch:
        d["batch"] = {
            "filename":     body.batch.get("filename"),
            "kpis":         body.batch.get("kpis"),
            "top_breaches": (body.batch.get("top_breaches") or [])[:5],
        }

    if body.sla_matrix:
        d["sla_matrix"] = {
            "compliance_pct": body.sla_matrix.get("compliance_pct"),
            "breaching_runs": body.sla_matrix.get("breaching_runs"),
            "worst_job":      body.sla_matrix.get("worst_job"),
            "worst_hrs":      body.sla_matrix.get("worst_hrs"),
        }

    if body.benchmark:
        d["benchmark"] = {
            "total":      body.benchmark.get("total_transactions"),
            "degraded":   body.benchmark.get("degraded"),
            "avg_delta":  body.benchmark.get("avg_delta_pct"),
        }

    if body.correlation:
        d["correlation"] = {
            "summary":  body.correlation.get("summary"),
            "insights": (body.correlation.get("insights") or [])[:6],
        }

    if body.sow:
        d["sow"] = {
            "overall_status": body.sow.get("overall_status"),
            "summary":        body.sow.get("summary"),
        }

    if body.sow_contract:
        sc = body.sow_contract
        d["sow_contract"] = {
            "customer":          sc.get("customer_name"),
            "annual_fee":        sc.get("annual_fee"),
            "currency":          sc.get("currency", "€"),
            "contract_years":    sc.get("contract_years"),
            "availability_sla":  sc.get("availability_sla_pct"),
            "sla_windows":       sc.get("sla_windows"),
            "volume_by_year":    sc.get("volume_by_year"),
            "disaster_recovery": sc.get("disaster_recovery"),
        }

    if body.redflags:
        d["redflags"] = {
            "by_risk":      body.redflags.get("by_risk"),
            "top_flags":    (body.redflags.get("flags") or [])[:6],
        }

    if body.executive:
        d["executive_kpis"] = body.executive.get("kpis")

    return d


def _parse_actions(text: str) -> List[str]:
    """Pull bulleted / numbered next-action items out of the AI verdict."""
    if not text:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    actions: List[str] = []
    capture = False
    for ln in lines:
        if re.search(r"NEXT\s*48|NEXT\s*ACTIONS|IMMEDIATE\s*ACTION", ln, re.I):
            capture = True
            continue
        if capture:
            m = re.match(r"^(?:[\-\*\u2022]|\d+\.)\s*(.+)", ln)
            if m:
                actions.append(m.group(1).strip())
            elif ln.upper().startswith(("VERDICT", "DECISION", "TOP RISK", "CROSS")):
                break
    return actions[:5]


@router.post("/final-judgment", response_model=FinalJudgmentResponse)
def final_judgment(body: FinalJudgmentRequest) -> FinalJudgmentResponse:
    # 1. Per-pillar scoring — pass-rate base + bounded severity penalties, with a
    #    fully-cited evidence ledger. Mode (additive|recompute) is per-customer.
    scoring = judgment_engine.score_all_pillars(
        resource=body.resource,
        batch=body.batch,
        sla=body.sla_matrix,
        benchmark=body.benchmark,
        sow=body.sow,
        correlation=body.correlation,
    )
    pillars: Dict[str, float] = dict(scoring.scores)
    pillars_present = list(pillars.keys())
    evidence_chain = scoring.evidence_chain
    scoring_mode = scoring.mode
    pillar_details: Dict[str, Any] = {
        name: {
            "base":            ps.base,
            "final":           ps.score,
            "penalty_applied": ps.penalty_applied,
            "penalty_raw":     ps.penalty_raw,
            "capped":          ps.capped,
        }
        for name, ps in scoring.details.items()
    }

    # 1b. Computed (not LLM-guessed) cross-pillar correlations.
    cross_pillar_links = judgment_engine.compute_cross_pillar_links(
        resource=body.resource,
        batch=body.batch,
        sla=body.sla_matrix,
        benchmark=body.benchmark,
        sow=body.sow,
    )

    # 2. Composite score — weighted, only over pillars present
    #    Pillars with no data get neutral 50 in the numerator
    #    but their weight still counts in the denominator
    weights = {"batch": 0.30, "sla": 0.25, "resource": 0.22,
               "correlation": 0.10, "benchmark": 0.08, "sow": 0.05}
    
    pillar_contributions: Dict[str, float] = {}
    if pillars:
        w_total = sum(weights.values())
        weighted_sum = 0.0
        for k, w in weights.items():
            val = pillars.get(k, 50.0)  # neutral 50 for missing pillars
            contrib = val * w / w_total
            pillar_contributions[k] = round(contrib, 2)
            weighted_sum += val * w
        score = round(weighted_sum / w_total, 1)
    else:
        score = 0.0

    # 3. Red-flag critical count drives the decision matrix
    rf_critical = 0
    if body.redflags:
        rf_critical = int((body.redflags.get("by_risk") or {}).get("CRITICAL") or 0)

    # Build named KPI evidence once, before both deterministic decisioning and
    # LLM reconciliation, so every downstream surface cites the same facts.
    kpi_ev = _build_kpi_evidence(body)

    grade    = _grade(score) if pillars else "N/A"
    decision, verdict_reason = _decision_with_reason(
        score, pillars, rf_critical, len(pillars), kpi_ev
    ) if pillars else ("INSUFFICIENT_DATA", "No pillar data loaded")

    # 4. AI verdict via narrator (best-effort) — narrate FROM the computed ledger
    digest = _build_digest(body, pillars)
    digest["composite_score"] = score
    digest["composite_grade"] = grade
    digest["proposed_decision"] = decision
    digest["redflag_critical"]  = rf_critical
    digest["evidence_facts"] = kpi_ev
    digest["verdict_reason"] = verdict_reason
    digest["scoring_mode"] = scoring_mode
    # Only the FAIL/PENALTY lines drive the narrative — keep the prompt focused on
    # what actually moved the score, with the points each fact removed.
    digest["evidence_ledger"] = [
        {"pillar": e["pillar"], "fact": e["fact"], "points": e["points"]}
        for e in evidence_chain
        if e.get("status") in ("FAIL", "PENALTY") and e.get("points")
    ][:12]
    digest["pillar_score_detail"] = pillar_details
    digest["cross_pillar_links"] = [
        {"severity": l["severity"], "text": l["text"]} for l in cross_pillar_links
    ]

    narrative, ai_model = None, None
    try:
        from services.ai_narrator import narrate
        narrative, ai_model = narrate("final_judgment", digest, max_tokens=720,
                                      temperature=0.35)
    except Exception:
        pass

    next_actions = _parse_actions(narrative or "")

    # ── Reconcile LLM verdict against deterministic ──────────────
    from services.verdict_reconciler import reconcile_verdict

    # Parse LLM decision/grade out of narrative text
    llm_decision_parsed, llm_grade_parsed = None, None
    if narrative:
        vline = re.search(r"(?:VERDICT|DECISION)[:\s]+(.+)", narrative, re.I)
        if vline:
            vtext = vline.group(1).upper()
            for d in ("REMEDIATE", "HOLD", "GO"):
                if d in vtext:
                    llm_decision_parsed = d
                    break
            gm = re.search(r"GRADE\s+([A-F])", vtext)
            if gm:
                llm_grade_parsed = gm.group(1)

    det_actions: List[str] = []
    if rf_critical >= 1:
        det_actions.append(f"Resolve {rf_critical} CRITICAL red-flag(s) before next release")
    if pillars.get("batch", 100) < 90:
        det_actions.append("Stabilise batch SLA — investigate top breaching jobs")
    if pillars.get("resource", 100) < 80:
        det_actions.append("Right-size or rebalance the most stressed servers")
    if pillars.get("benchmark", 100) < 80:
        det_actions.append("Diagnose UI/perf regressions vs baseline")
    if not det_actions:
        det_actions.append("Maintain current posture; keep monitoring")

    recon = reconcile_verdict(
        det_score=score,
        det_grade=grade,
        det_decision=decision,
        det_risks=[],
        det_actions=det_actions,
        llm_grade=llm_grade_parsed,
        llm_decision=llm_decision_parsed,
        llm_actions=next_actions or None,
        llm_narrative=narrative,
        kpi_evidence=kpi_ev,
        det_verdict_reason=verdict_reason,
    )

    # Use reconciled values
    final_decision = recon.final_decision
    final_actions = recon.final_next_actions or det_actions
    final_verdict_reason = recon.verdict_reason or verdict_reason
    if narrative and final_verdict_reason not in narrative:
        narrative = f"{narrative.rstrip()}\nVERDICT_REASON: {final_verdict_reason}"

    _reason_body = final_verdict_reason
    if " — driven by " in _reason_body:
        _reason_body = _reason_body.split(" — driven by ", 1)[1]
    elif " — " in _reason_body:
        _reason_body = _reason_body.split(" — ", 1)[1]
    verdict_line = (
        f"Composite score {score}/100 (grade {grade}) — decision: {final_decision} "
        f"— driven by: {_reason_body}."
    )
    if recon.mismatches:
        verdict_line += f" [{len(recon.mismatches)} mismatch(es) reconciled]"

    return FinalJudgmentResponse(
        verdict=verdict_line,
        decision=final_decision,
        grade=grade,
        score=score,
        pillars=pillars,
        pillar_weights=weights,
        pillar_contributions=pillar_contributions,
        pillar_details=pillar_details,
        evidence_chain=evidence_chain,
        cross_pillar_links=cross_pillar_links,
        scoring_mode=scoring_mode,
        verdict_reason=final_verdict_reason,
        narrative=narrative,
        next_actions=final_actions,
        pillars_present=pillars_present,
        ai_model=ai_model,
    )
