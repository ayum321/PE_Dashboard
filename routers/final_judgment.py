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
    verdict_reason: str = ""
    narrative:    Optional[str] = None
    next_actions: List[str]
    pillars_present: List[str]
    ai_model:     Optional[str] = None


# ── Deterministic scoring (always works, even without AI) ─────────
def _score_resource(r: Optional[Dict[str, Any]]) -> Optional[float]:
    if not r:
        return None
    servers = r.get("servers") or []
    if not servers:
        return None
    healths = [float(s.get("health_score") or 0) for s in servers
               if float(s.get("health_score") or 0) > 0]
    if not healths:
        return None
    return round(sum(healths) / len(healths), 1)


def _score_batch(b: Optional[Dict[str, Any]]) -> Optional[float]:
    if not b:
        return None
    kpis = b.get("kpis") or {}
    comp = kpis.get("compliance_pct")
    if comp is None:
        return None
    return round(float(comp), 1)


def _score_sla(s: Optional[Dict[str, Any]]) -> Optional[float]:
    if not s:
        return None
    comp = s.get("compliance_pct")
    if comp is None:
        return None
    return round(float(comp), 1)


def _score_benchmark(bm: Optional[Dict[str, Any]]) -> Optional[float]:
    if not bm:
        return None
    total = int(bm.get("total_transactions") or 0)
    if total == 0:
        return None
    degraded = int(bm.get("degraded") or 0)
    return round(max(0.0, 100.0 * (1.0 - degraded / total)), 1)


def _score_sow(sw: Optional[Dict[str, Any]]) -> Optional[float]:
    if not sw:
        return None
    metrics = sw.get("metrics") or []
    if not metrics:
        return None
    optimal = sum(1 for m in metrics if m.get("status") == "OPTIMAL")
    return round(100.0 * optimal / len(metrics), 1)


def _score_correlation(c: Optional[Dict[str, Any]]) -> Optional[float]:
    if not c:
        return None
    rows = c.get("rows") or []
    if not rows:
        return None
    crit = sum(1 for r in rows if r.get("risk") == "CRITICAL")
    high = sum(1 for r in rows if r.get("risk") == "HIGH")
    # Higher = better; subtract penalty per risky correlation
    return round(max(0.0, 100.0 - (crit * 12.0 + high * 6.0)), 1)


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


def _decision_with_reason(
    score: float,
    pillars: Dict[str, float],
    redflag_critical: int,
    loaded_count: int,
) -> tuple:
    """Return (decision, reason) using algorithmic thresholds."""
    # Hard blocks
    for name, val in pillars.items():
        if val < 40:
            return ("BLOCKED",
                    f"{name.upper()} score {val:.0f}% — below 40% hard-block threshold")
    if redflag_critical >= 3:
        return ("BLOCKED",
                f"{redflag_critical} CRITICAL red-flags — remediation required before sign-off")

    # SLA-specific hard block
    sla_score = pillars.get("sla")
    if sla_score is not None and sla_score < 50:
        return ("BLOCKED",
                f"SLA compliance {sla_score:.0f}% — more than half the period in breach")

    # Resource-specific hard block
    resource_score = pillars.get("resource")
    if resource_score is not None and resource_score < 60:
        return ("BLOCKED",
                f"Resource health {resource_score:.0f}% — critical server pressure unresolved")

    # Conditional hold checks
    for name, val in pillars.items():
        if val < 60:
            return ("HOLD",
                    f"{name.upper()} score {val:.0f}% — below 60% threshold. Resolve before sign-off")

    if loaded_count < 3:
        return ("HOLD",
                f"Only {loaded_count}/6 pillars loaded — insufficient evidence for approval")

    if score < 60:
        return ("HOLD", f"Composite score {score:.0f}% — below 60% minimum")

    # Approval zone
    if score >= 80:
        reason = "All pillars above threshold. Ready for sign-off"
        return ("GO", reason)
    if score >= 70:
        return ("GO_WITH_NOTES",
                "All pillars passing but composite below 80% — approved with observations")

    return ("HOLD", f"Composite score {score:.0f}% — between 70-80%, review recommended")


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
    # 1. Per-pillar scoring (deterministic)
    pillars_raw: Dict[str, Optional[float]] = {
        "resource":    _score_resource(body.resource),
        "batch":       _score_batch(body.batch),
        "sla":         _score_sla(body.sla_matrix),
        "benchmark":   _score_benchmark(body.benchmark),
        "sow":         _score_sow(body.sow),
        "correlation": _score_correlation(body.correlation),
    }
    pillars: Dict[str, float] = {k: v for k, v in pillars_raw.items() if v is not None}
    pillars_present = list(pillars.keys())

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

    grade    = _grade(score) if pillars else "N/A"
    decision, verdict_reason = _decision_with_reason(
        score, pillars, rf_critical, len(pillars)
    ) if pillars else ("INSUFFICIENT_DATA", "No pillar data loaded")

    # 4. AI verdict via narrator (best-effort)
    digest = _build_digest(body, pillars)
    digest["composite_score"] = score
    digest["composite_grade"] = grade
    digest["proposed_decision"] = decision
    digest["redflag_critical"]  = rf_critical

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

    # Build KPI evidence from the body for validation
    kpi_ev: Dict[str, Any] = {}
    if body.batch:
        kpi_ev["batch"] = {
            "compliance_pct": body.batch.get("kpis", {}).get("compliance_pct")
                              if isinstance(body.batch.get("kpis"), dict)
                              else None,
            "jobs_breach": body.batch.get("kpis", {}).get("jobs_breach")
                           if isinstance(body.batch.get("kpis"), dict)
                           else None,
        }
    if body.resource:
        kpi_ev["resource"] = {
            "n_critical": body.resource.get("n_critical"),
            "avg_cpu": body.resource.get("avg_cpu"),
        }
    if body.redflags:
        kpi_ev["redflags"] = body.redflags.get("by_risk") or {}

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
    )

    # Use reconciled values
    final_decision = recon.final_decision
    final_actions = recon.final_next_actions or det_actions

    verdict_line = (
        f"Composite score {score}/100 (grade {grade}) — decision: {final_decision}."
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
        verdict_reason=verdict_reason,
        narrative=narrative,
        next_actions=final_actions,
        pillars_present=pillars_present,
        ai_model=ai_model,
    )
