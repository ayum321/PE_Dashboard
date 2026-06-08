"""
verdict_reconciler — smart mismatch detection between deterministic rules
and LLM-generated verdicts.

When both engines produce a verdict (grade, decision, risk level, scores),
this module compares them and forces the more evidence-backed answer.

Core principle: deterministic numbers are GROUND TRUTH.
If LLM contradicts a measured number, the LLM is wrong.
If LLM agrees but adds insight the rules missed, that's additive value.

Three reconciliation modes:
  1. SCORE reconciliation — LLM grade vs deterministic grade
  2. DECISION reconciliation — LLM GO/HOLD/REMEDIATE vs deterministic
  3. RISK reconciliation — LLM risk claims vs actual KPI evidence

Public API:
    reconcile_verdict(det_score, det_grade, det_decision, det_risks,
                      llm_score, llm_grade, llm_decision, llm_risks,
                      kpi_evidence) -> ReconciliationResult
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("pe_dashboard.verdict_reconciler")

# Grade severity ranking (higher = more severe)
_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4, "N/A": -1, "—": -1}
_DECISION_RANK = {"GO": 0, "HOLD": 1, "REMEDIATE": 2, "INSUFFICIENT_DATA": -1}
_DECISION_FROM_RANK = {0: "GO", 1: "HOLD", 2: "REMEDIATE"}


class ReconciliationResult:
    """Outcome of reconciling deterministic vs LLM verdicts."""
    __slots__ = (
        "final_grade", "final_score", "final_decision",
        "final_risks", "final_next_actions",
        "source", "mismatches", "overrides",
    )

    def __init__(self):
        self.final_grade:       str = "—"
        self.final_score:       float = 0.0
        self.final_decision:    str = "INSUFFICIENT_DATA"
        self.final_risks:       list[str] = []
        self.final_next_actions: list[str] = []
        self.source:            str = "deterministic"   # deterministic | llm | reconciled
        self.mismatches:        list[dict[str, Any]] = []
        self.overrides:         list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_grade":       self.final_grade,
            "final_score":       self.final_score,
            "final_decision":    self.final_decision,
            "final_risks":       self.final_risks,
            "final_next_actions": self.final_next_actions,
            "source":            self.source,
            "mismatches":        self.mismatches,
            "overrides":         self.overrides,
        }


def reconcile_verdict(
    *,
    det_score:    float,
    det_grade:    str,
    det_decision: str,
    det_risks:    list[str],
    det_actions:  list[str],
    llm_grade:    Optional[str] = None,
    llm_decision: Optional[str] = None,
    llm_risks:    Optional[list[str]] = None,
    llm_actions:  Optional[list[str]] = None,
    llm_narrative: Optional[str] = None,
    kpi_evidence: Optional[dict[str, Any]] = None,
) -> ReconciliationResult:
    """Compare deterministic and LLM verdicts, force the more accurate one.

    Rules:
    1. Score is ALWAYS deterministic (it's computed from real numbers).
    2. Grade is ALWAYS deterministic (derived from score).
    3. Decision: take the STRICTER of the two.
       - If LLM says GO but det says HOLD → HOLD wins.
       - If LLM says REMEDIATE but det says HOLD → REMEDIATE wins.
       - If either is INSUFFICIENT_DATA → that one is ignored.
    4. Risks: merge, but validate LLM risk claims against kpi_evidence.
       - If LLM claims "0 breaches" but kpi says breach_count > 0, discard.
       - If LLM adds a new risk not in det list AND it references real
         entities from kpi_evidence, accept it.
    5. Actions: take LLM actions if they reference real entities, else det.
    """
    r = ReconciliationResult()
    r.final_score = det_score
    r.final_grade = det_grade

    # ── Decision reconciliation (take stricter) ──────────────────
    det_rank = _DECISION_RANK.get(det_decision.upper(), -1)
    llm_rank = _DECISION_RANK.get((llm_decision or "").upper(), -1)

    if llm_rank >= 0 and det_rank >= 0:
        if llm_rank != det_rank:
            r.mismatches.append({
                "field": "decision",
                "deterministic": det_decision,
                "llm": llm_decision,
                "resolution": "stricter wins",
            })
            winner_rank = max(det_rank, llm_rank)
            r.final_decision = _DECISION_FROM_RANK.get(winner_rank, det_decision)
            r.overrides.append(
                f"Decision conflict: det={det_decision}, LLM={llm_decision} "
                f"→ forced {r.final_decision} (stricter)"
            )
            r.source = "reconciled"
        else:
            r.final_decision = det_decision
    else:
        r.final_decision = det_decision

    # ── Grade sanity check ───────────────────────────────────────
    if llm_grade and llm_grade.upper() in _GRADE_RANK:
        det_r = _GRADE_RANK.get(det_grade.upper(), -1)
        llm_r = _GRADE_RANK.get(llm_grade.upper(), -1)
        if det_r >= 0 and llm_r >= 0 and abs(det_r - llm_r) >= 2:
            r.mismatches.append({
                "field": "grade",
                "deterministic": det_grade,
                "llm": llm_grade,
                "resolution": "deterministic wins (computed from real data)",
            })
            r.overrides.append(
                f"Grade mismatch: det={det_grade}, LLM={llm_grade} → "
                f"forced {det_grade} (data-backed)"
            )
            r.source = "reconciled"

    # ── Risk list reconciliation ─────────────────────────────────
    kpi = kpi_evidence or {}
    det_risk_set = set(det_risks)
    validated_llm_risks: list[str] = []

    if llm_risks:
        for risk_text in llm_risks:
            # Check if LLM risk contradicts KPI evidence
            contradiction = _check_risk_contradiction(risk_text, kpi)
            if contradiction:
                r.mismatches.append({
                    "field": "risk",
                    "llm_claim": risk_text,
                    "contradiction": contradiction,
                    "resolution": "LLM risk discarded — contradicts data",
                })
                r.overrides.append(f"Discarded LLM risk: '{risk_text[:60]}…' — {contradiction}")
                r.source = "reconciled"
            else:
                # Check if LLM risk references real entities from KPIs
                if _risk_references_real_data(risk_text, kpi):
                    validated_llm_risks.append(risk_text)
                # else: generic fluff — skip silently

    # Merge: deterministic risks first, then validated LLM additions
    merged_risks = list(det_risks)
    for vr in validated_llm_risks:
        if not any(_similar_risk(vr, existing) for existing in merged_risks):
            merged_risks.append(vr)
    r.final_risks = merged_risks[:6]

    # ── Action list reconciliation ───────────────────────────────
    if llm_actions and _actions_reference_real_data(llm_actions, kpi):
        r.final_next_actions = llm_actions[:5]
        if r.source == "deterministic":
            r.source = "llm"  # LLM actions accepted
    else:
        r.final_next_actions = det_actions[:5]

    if r.mismatches:
        log.info(
            "verdict_reconciler: %d mismatch(es), %d override(s), source=%s",
            len(r.mismatches), len(r.overrides), r.source,
        )

    return r


# ── Evidence validation helpers ──────────────────────────────────────────────

def _check_risk_contradiction(risk_text: str, kpi: dict) -> Optional[str]:
    """If the LLM risk text makes a numerical claim that contradicts
    actual KPI data, return the contradiction description. Else None."""
    rt = risk_text.lower()

    # Check "0 breaches" / "no breaches" claims
    if ("0 breach" in rt or "no breach" in rt or "zero breach" in rt):
        actual_breaches = (
            _safe_int(kpi.get("batch", {}).get("jobs_breach"))
            or _safe_int(kpi.get("sla", {}).get("breaching_runs"))
            or 0
        )
        if actual_breaches > 0:
            return f"Claims 0 breaches but actual breach count = {actual_breaches}"

    # Check "100% compliance" claims
    if "100%" in rt and "complian" in rt:
        actual_comp = _safe_float(
            kpi.get("batch", {}).get("compliance_pct")
            or kpi.get("sla", {}).get("compliance_pct")
        )
        if actual_comp is not None and actual_comp < 99.5:
            return f"Claims 100% compliance but actual = {actual_comp:.1f}%"

    # Check "no critical" / "0 critical" claims
    if ("no critical" in rt or "0 critical" in rt or "zero critical" in rt):
        n_crit_servers = _safe_int(kpi.get("resource", {}).get("n_critical"))
        n_crit_findings = _safe_int(kpi.get("findings", {}).get("critical"))
        n_crit_rf = _safe_int(kpi.get("redflags", {}).get("CRITICAL"))
        total_crit = (n_crit_servers or 0) + (n_crit_findings or 0) + (n_crit_rf or 0)
        if total_crit > 0:
            return f"Claims no criticals but found {total_crit} across pillars"

    # Check fabricated server names
    real_hosts = set()
    for s in kpi.get("servers", []):
        h = (s.get("host") or "").split(".")[0].lower()
        if h:
            real_hosts.add(h)
    if real_hosts:
        # Look for hostname-like patterns in the risk text
        host_pattern = re.findall(r"\b[a-z][a-z0-9_-]{4,30}\b", rt)
        for hp in host_pattern:
            if hp in ("server", "breach", "critical", "warning", "resource",
                      "batch", "metric", "running", "failure", "compliance",
                      "should", "could", "would", "might", "likely"):
                continue
            if hp not in real_hosts and any(c.isdigit() for c in hp):
                # Looks like a fabricated hostname
                return f"References '{hp}' which doesn't match any known server"

    return None


def _risk_references_real_data(risk_text: str, kpi: dict) -> bool:
    """True if the risk text references at least one real entity from KPIs."""
    rt = risk_text.lower()

    # Check for real job names
    for j in kpi.get("top_jobs", []):
        jn = (j.get("Job_Name") or j.get("job_name") or "").lower()
        if jn and len(jn) > 3 and jn in rt:
            return True

    # Check for real hostnames
    for s in kpi.get("servers", []):
        h = (s.get("host") or "").split(".")[0].lower()
        if h and len(h) > 3 and h in rt:
            return True

    # Check for real sub-apps
    for sa in kpi.get("sub_apps", []):
        san = (sa if isinstance(sa, str) else "").lower()
        if san and len(san) > 3 and san in rt:
            return True

    # Check for real numbers from KPIs (compliance %, breach counts, etc.)
    comp = kpi.get("batch", {}).get("compliance_pct")
    if comp is not None:
        comp_str = f"{comp:.1f}"
        if comp_str in risk_text or f"{comp:.0f}" in risk_text:
            return True

    breach = kpi.get("batch", {}).get("jobs_breach")
    if breach is not None and str(breach) in risk_text:
        return True

    # Generic: if it contains numbers at all, it's probably data-backed
    if re.search(r"\d+\.?\d*%|\d+\s*(?:job|server|breach|run|day|hour)", rt):
        return True

    return False


def _similar_risk(a: str, b: str) -> bool:
    """True if two risk strings are essentially the same risk."""
    a_words = set(a.lower().split())
    b_words = set(b.lower().split())
    if not a_words or not b_words:
        return False
    overlap = len(a_words & b_words) / min(len(a_words), len(b_words))
    return overlap > 0.6


def _actions_reference_real_data(actions: list[str], kpi: dict) -> bool:
    """True if at least one action references a real entity."""
    blob = " ".join(actions).lower()
    # Check for real job names
    for j in kpi.get("top_jobs", []):
        jn = (j.get("Job_Name") or j.get("job_name") or "").lower()
        if jn and len(jn) > 3 and jn in blob:
            return True
    # Check for real hostnames
    for s in kpi.get("servers", []):
        h = (s.get("host") or "").split(".")[0].lower()
        if h and len(h) > 3 and h in blob:
            return True
    # Numbers referencing real metrics
    if re.search(r"\d+\.?\d*%|\d+\s*(?:job|server|breach|run|day|hour)", blob):
        return True
    return False


def _safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ── Smart findings mismatch detection ────────────────────────────────────────

def check_finding_severity_mismatch(
    finding_level: str,
    finding_text: str,
    kpi_evidence: dict,
) -> Optional[str]:
    """If a finding's severity doesn't match what KPIs say, return the
    correct severity. Else None (severity is correct).

    Examples:
    - Finding says "OK" but actual breach count > 0 → should be WARNING
    - Finding says "CRITICAL" for a metric within threshold → should be INFO
    """
    level = (finding_level or "").lower()
    text = (finding_text or "").lower()

    batch = kpi_evidence.get("batch", {})
    resource = kpi_evidence.get("resource", {})

    # Rule 1: "OK" / "info" finding that mentions compliance BUT actual compliance < 90%
    if level in ("ok", "info") and "complian" in text:
        comp = _safe_float(batch.get("compliance_pct"))
        if comp is not None and comp < 90:
            return "warning"

    # Rule 2: "OK" finding about breaches but actual breaches > 0
    if level in ("ok", "info") and "breach" in text:
        breaches = _safe_int(batch.get("jobs_breach")) or 0
        if breaches > 0:
            return "warning"

    # Rule 3: "CRITICAL" finding about CPU but actual avg CPU < 60%
    if level == "critical" and ("cpu" in text or "server" in text):
        avg_cpu = _safe_float(resource.get("avg_cpu"))
        if avg_cpu is not None and avg_cpu < 60:
            return "info"

    # Rule 4: "OK" finding about fleet health but critical servers exist
    if level in ("ok", "info") and ("fleet" in text or "health" in text):
        n_crit = _safe_int(resource.get("n_critical")) or 0
        if n_crit > 0:
            return "warning"

    return None  # severity is correct
