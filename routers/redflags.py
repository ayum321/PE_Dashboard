"""
Red Flags & RCA auto-generator.

POST /api/red-flags
Scans all available data sources (batch KPIs, resource metrics, issues register)
and generates:
  - PE-style investigation questions with risk levels
  - A risk priority matrix
  - Counts by risk tier

All logic is deterministic — no LLM call; use /api/ai-insight for AI narrative.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from services.pe_utils import coerce_float as _f, coerce_int as _i

router = APIRouter()


# ── Models ───────────────────────────────────────────────────────

class RedFlagsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_kpis:    Optional[Dict[str, Any]]       = None
    resource_kpis: Optional[Dict[str, Any]]       = None
    servers:       Optional[List[Dict[str, Any]]] = None
    anomalies:     Optional[List[Dict[str, Any]]] = None
    issues:        Optional[List[Dict[str, Any]]] = None
    top_breaches:  Optional[List[Dict[str, Any]]] = None
    sub_stats:     Optional[List[Dict[str, Any]]] = None
    # Hardwired interconnection — full SLA Matrix response from /api/sla-matrix
    sla_matrix:    Optional[Dict[str, Any]]       = None


class RedFlag(BaseModel):
    id:         str   # Q1, Q2, …
    category:   str   # CPU | Memory | Batch | DR | Scheduling | Governance | Correlation | Testing
    context:    str   # One-sentence observation (with numbers)
    question:   str   # The PE investigation question
    risk:       str   # CRITICAL | HIGH | MEDIUM | LOW
    data_point: str   # The specific metric driving this flag


class RiskItem(BaseModel):
    area:           str
    risk:           str
    impact:         str
    recommendation: str


class RedFlagsResponse(BaseModel):
    flags:       List[RedFlag]
    risk_matrix: List[RiskItem]
    total:       int
    by_risk:     Dict[str, int]
    ai_narrative: Optional[str] = None
    ai_model:     Optional[str] = None


# ── Optional AI narrative helper ─────────────────────────────────
def _ai_narrative(flags: List[RedFlag],
                  risk_matrix: List[RiskItem],
                  by_risk: Dict[str, int]) -> tuple[Optional[str], Optional[str]]:
    """Best-effort short narrative summarising the deterministic flags."""
    if not flags and not risk_matrix:
        return None, None
    try:
        from services.ai_engine import chat as _ai_chat, is_ready
        if not is_ready().get("nvidia_key") and not is_ready().get("gemini_key"):
            return None, None
        digest = {
            "by_risk":      by_risk,
            "top_flags":    [f.model_dump() for f in flags[:8]],
            "risk_matrix":  [r.model_dump() for r in risk_matrix[:8]],
        }
        prompt = (
            "Summarize the top performance-engineering risks in 4 lines: "
            "(1) overall posture, (2) the single most damaging flag and why, "
            "(3) the cluster of related risks, (4) one immediate action.\n\n"
            f"DIGEST: {digest}"
        )
        text, model = _ai_chat(
            prompt,
            system=("You are a Senior Performance Engineer writing for a CTO. "
                    "Be specific, quote exact counts, no filler."),
            max_tokens=380, temperature=0.3,
        )
        return text.strip() or None, model
    except Exception:
        return None, None



# ── Endpoint ────────────────────────────────────────────────────

@router.post("/red-flags", response_model=RedFlagsResponse)
def red_flags(body: RedFlagsRequest) -> RedFlagsResponse:  # noqa: C901
    bk          = body.batch_kpis    or {}
    rk          = body.resource_kpis or {}
    servers     = body.servers       or []
    anomalies   = body.anomalies     or []
    issues      = body.issues        or []
    top_breaches= body.top_breaches  or []
    sla_mx      = body.sla_matrix    or {}

    flags:       List[RedFlag]   = []
    risk_matrix: List[RiskItem]  = []
    q_num = 0

    def add_flag(cat: str, ctx: str, question: str, risk: str, dp: str) -> None:
        nonlocal q_num
        q_num += 1
        flags.append(RedFlag(
            id=f"Q{q_num}", category=cat, context=ctx,
            question=question, risk=risk, data_point=dp,
        ))

    # ── Batch / CTRL-M flags ─────────────────────────────────────
    compliance    = _f(bk.get("compliance_pct"), 100.0)
    breach_count  = _i(bk.get("jobs_breach"))
    total_jobs    = _i(bk.get("total_jobs"))
    at_risk_count = _i(bk.get("jobs_at_risk"))

    if total_jobs > 0:
        if compliance < 80:
            add_flag(
                "Batch",
                f"Batch SLA compliance is {compliance:.1f}% — critically below the 95% production target.",
                f"What is the root cause of the {breach_count} SLA breach(es) and "
                f"what is the remediation plan before production go-live?",
                "CRITICAL", f"{compliance:.1f}% compliance",
            )
            risk_matrix.append(RiskItem(
                area="Batch SLA Compliance",
                risk="CRITICAL",
                impact=f"{breach_count} jobs breached SLA — {100 - compliance:.1f}% non-compliance rate",
                recommendation="Identify and resolve all breach root causes; obtain customer sign-off on each fix",
            ))
        elif compliance < 95:
            add_flag(
                "Batch",
                f"Batch SLA compliance is {compliance:.1f}% — below the 95% production target.",
                f"Are the {breach_count} breaching jobs one-off or recurring failures? "
                "Is there a fix or waiver in place for each?",
                "HIGH", f"{breach_count} breaches",
            )

        if at_risk_count > 0:
            add_flag(
                "Scheduling",
                f"{at_risk_count} jobs have <40% SLA buffer, placing them at risk under full production load.",
                "What monitoring alerts are configured to catch at-risk jobs before they breach? "
                "What is the escalation path when a breach is detected?",
                "HIGH", f"{at_risk_count} at-risk jobs",
            )

        if top_breaches:
            worst = top_breaches[0]
            add_flag(
                "Batch",
                f"Job '{worst.get('Job_Name','?')}' is the worst SLA offender at "
                f"{_f(worst.get('peak_hrs')):.2f} hrs peak runtime.",
                "Has this job's root cause been formally analysed? "
                "Is a code fix, resource allocation change, or SLA waiver required?",
                "CRITICAL",
                f"{_f(worst.get('peak_hrs')):.2f}h peak / "
                f"{_f(worst.get('buffer_pct')):.0f}% buffer left",
            )

    # Zero-duration / pre-execution terminations
    zero_dur = [
        a for a in anomalies
        if "zero" in str(a.get("type", "")).lower() or
        "zero_duration" in str(a.get("type", "")).lower()
    ]
    if zero_dur:
        add_flag(
            "Batch",
            f"{len(zero_dur)} job(s) completed with zero runtime — "
            "indicating pre-execution termination or dependency abort.",
            "What triggered these zero-duration failures? "
            "Are downstream jobs configured to abort on upstream zero-duration outcomes?",
            "CRITICAL", f"{len(zero_dur)} zero-duration jobs",
        )
        risk_matrix.append(RiskItem(
            area="Zero-Duration Failures",
            risk="CRITICAL",
            impact="Pre-execution job terminations can cascade and silently skip critical data processing",
            recommendation="Review Ctrl-M dependency logic; add explicit failure-handling for zero-duration jobs",
        ))

    # ── SLA Matrix interconnection ───────────────────────────────
    # Hardwired flags from the SLA Matrix pillar so Red Flags & RCA
    # cite the same evidence the SLA tab displays.
    if sla_mx:
        sla_breach   = _i(sla_mx.get("breaching_runs"))
        sla_atrisk   = _i(sla_mx.get("at_risk_runs"))
        sla_total    = _i(sla_mx.get("total_runs"))
        sla_comp     = _f(sla_mx.get("compliance_pct"), 100.0)
        sla_limit    = _f(sla_mx.get("sla_limit_hrs"))
        sla_label    = sla_mx.get("sla_label") or "SLA"
        worst_job    = sla_mx.get("worst_job") or ""
        worst_hrs    = _f(sla_mx.get("worst_hrs"))
        worst_margin = _f(sla_mx.get("worst_margin_hrs"))
        breach_rows  = sla_mx.get("breaches") or []

        if sla_breach > 0 and worst_job:
            top_breaches_listed = ", ".join(
                f"{r.get('job_name','?')} ({_f(r.get('run_hrs')):.2f}h)"
                for r in breach_rows[:3] if r.get("status") == "BREACH"
            )
            add_flag(
                "SLA-Matrix",
                f"SLA Matrix flags {sla_breach} BREACH run(s) against {sla_label} "
                f"({sla_comp:.1f}% compliance over {sla_total} runs). "
                f"Worst: {worst_job} at {worst_hrs:.2f}h (+{worst_margin:.2f}h over).",
                f"For each breaching job ({top_breaches_listed or worst_job}), what is the "
                f"committed remediation — code fix, schedule shift, or formal SLA waiver?",
                "CRITICAL", f"{sla_breach} breach run(s) / {sla_label}",
            )
            risk_matrix.append(RiskItem(
                area=f"SLA Matrix · {sla_label}",
                risk="CRITICAL",
                impact=f"{sla_breach} run(s) over {sla_limit:.1f}h limit; worst margin +{worst_margin:.2f}h",
                recommendation="Trace each breach to its root cause in the SLA tab; "
                               "either fix the job or obtain a customer-signed waiver before sign-off",
            ))

        if sla_atrisk > 0 and sla_breach == 0:
            add_flag(
                "SLA-Matrix",
                f"{sla_atrisk} run(s) within 15% of the {sla_label} ceiling — "
                f"the buffer is too tight for production load growth.",
                "What load-growth headroom has been agreed with the customer, and which "
                "jobs will be optimised first if any AT_RISK run flips to BREACH?",
                "HIGH", f"{sla_atrisk} at-risk run(s) / {sla_label}",
            )

        # Repeated breaches by the same job → not a one-off
        from collections import Counter as _Counter
        repeats = _Counter(
            r.get("job_name") for r in breach_rows
            if r.get("status") == "BREACH" and r.get("job_name")
        )
        repeat_offenders = [(jn, n) for jn, n in repeats.items() if n >= 2]
        if repeat_offenders:
            names = ", ".join(f"{jn}×{n}" for jn, n in repeat_offenders[:3])
            add_flag(
                "SLA-Matrix",
                f"{len(repeat_offenders)} job(s) breach SLA on multiple runs — pattern, not anomaly: {names}.",
                "Why are these specific jobs breaching repeatedly? Has a code or data-volume "
                "trend been investigated, or is the SLA itself unrealistic?",
                "CRITICAL", f"{len(repeat_offenders)} repeat offender(s)",
            )

    # ── Resource / CPU flags ─────────────────────────────────────
    critical_srvs = [s for s in servers if _f(s.get("cpu_used")) >= 90]
    high_srvs     = [s for s in servers if 80 <= _f(s.get("cpu_used")) < 90]
    mem_critical  = [s for s in servers if _f(s.get("mem_used")) >= 90]
    disk_critical = [s for s in servers if _f(s.get("disk_used_max")) >= 90]
    dual_pressure = [
        s for s in servers
        if _f(s.get("cpu_used")) >= 80 and _f(s.get("mem_used")) >= 70
    ]

    if critical_srvs:
        top_cpu = critical_srvs[0]
        add_flag(
            "CPU",
            f"{len(critical_srvs)} server(s) running at ≥90% CPU — critically saturated.",
            "What is the primary workload driver on these servers? "
            "What is the plan to reduce CPU below 80% before production go-live?",
            "CRITICAL",
            f"{top_cpu.get('host','?')} @ {_f(top_cpu.get('cpu_used')):.0f}% CPU",
        )
        risk_matrix.append(RiskItem(
            area="CPU Saturation",
            risk="CRITICAL",
            impact=f"{len(critical_srvs)} server(s) at ≥90% CPU — batch jobs will fail under concurrent load",
            recommendation="Increase vCPU allocation or redistribute workload; target <80% max CPU",
        ))

    if high_srvs:
        add_flag(
            "CPU",
            f"{len(high_srvs)} server(s) at 80–90% CPU — limited headroom under peak load.",
            "Under production peak load concurrent with batch execution, will these servers exceed 90%? "
            "What capacity buffer has been agreed with the customer?",
            "HIGH", f"{len(high_srvs)} server(s) at 80–90% CPU",
        )

    if mem_critical:
        # Separate DB servers in expected band from genuine memory pressure
        from services import pe_config as _pc
        db_mem_expected = [s for s in mem_critical
                          if (s.get("type") or s.get("server_type") or "").upper() == "DB"
                          and _f(s.get("mem_used")) <= _pc.DB_MEM_BAND_HIGH]
        mem_genuine = [s for s in mem_critical if s not in db_mem_expected]

        if db_mem_expected:
            add_flag(
                "Memory",
                f"{len(db_mem_expected)} DB server(s) at ≥90% memory — within expected DB allocation band.",
                f"DB servers pre-allocate SGA/PGA memory; steady usage up to {_pc.DB_MEM_BAND_HIGH:.0f}% is expected. "
                f"Confirm no page/swap activity and no upward trend above {_pc.DB_MEM_WARN:.0f}%.",
                "MEDIUM",
                f"{db_mem_expected[0].get('host','?')} @ {_f(db_mem_expected[0].get('mem_used')):.0f}% memory (DB expected)",
            )

        if mem_genuine:
            top_mem = mem_genuine[0]
            add_flag(
                "Memory",
                f"{len(mem_genuine)} server(s) at ≥90% memory utilisation.",
                "Is memory pressure causing OS swapping or paging? "
                "What is the JVM heap and GC configuration on these hosts?",
                "CRITICAL",
                f"{top_mem.get('host','?')} @ {_f(top_mem.get('mem_used')):.0f}% memory",
            )
            risk_matrix.append(RiskItem(
                area="Memory Pressure",
                risk="CRITICAL",
                impact="High memory utilisation may cause OOM kills and silent batch job failures",
                recommendation="Add RAM or tune JVM heap settings; add OOM alerting to monitoring",
            ))

    if dual_pressure:
        add_flag(
            "Infrastructure",
            f"{len(dual_pressure)} server(s) face DUAL pressure — CPU ≥80% AND Memory ≥70%.",
            "Are these dual-pressure servers hosting the most critical batch workloads? "
            "What is the immediate mitigation plan?",
            "CRITICAL", f"{len(dual_pressure)} dual-pressure server(s)",
        )

    if disk_critical:
        top_disk = disk_critical[0]
        add_flag(
            "Infrastructure",
            f"{len(disk_critical)} server(s) at ≥90% disk utilisation.",
            "What is the disk growth rate and projected full date? "
            "Is a disk cleanup or archive job scheduled before production cutover?",
            "HIGH",
            f"{top_disk.get('host','?')} @ {_f(top_disk.get('disk_used_max')):.0f}% disk",
        )

    fleet_grade = rk.get("fleet_grade", "")
    if fleet_grade in ("C", "D", "F"):
        add_flag(
            "Infrastructure",
            f"Overall fleet health grade is {fleet_grade} — below acceptable production standard.",
            "What remediation actions have been agreed to bring fleet health to grade B or above "
            "before the PE audit sign-off?",
            "HIGH", f"Fleet grade {fleet_grade}",
        )
        risk_matrix.append(RiskItem(
            area="Fleet Health",
            risk="HIGH",
            impact=f"Grade {fleet_grade} fleet health indicates systemic infrastructure risk",
            recommendation=f"Address critical/high servers first; re-assess fleet grade post-remediation",
        ))

    # ── Governance / Issues flags ────────────────────────────────
    open_critical = [
        i for i in issues
        if str(i.get("Severity", "")).upper() in ("CRITICAL", "HIGH")
        and str(i.get("Status", "")).upper() not in ("CLOSED", "WAIVED")
    ]

    if open_critical:
        add_flag(
            "Governance",
            f"{len(open_critical)} open CRITICAL/HIGH issue(s) without resolution in the register.",
            "Does each issue have an assigned owner, concrete ETA, and tested fix? "
            "What is blocking resolution?",
            "CRITICAL", f"{len(open_critical)} open critical/high issues",
        )
        risk_matrix.append(RiskItem(
            area="Open Critical Issues",
            risk="CRITICAL",
            impact=f"{len(open_critical)} unresolved critical/high issues blocking go-live sign-off",
            recommendation="Assign owners with concrete ETAs; escalate blockers to PE lead immediately",
        ))

    if not issues and (breach_count > 0 or critical_srvs):
        add_flag(
            "Governance",
            "No issues have been logged despite observable performance anomalies in the data.",
            "Has a formal issues review been conducted? "
            "Are all anomalies tracked, classified, and assigned in the register?",
            "MEDIUM", "0 issues logged",
        )

    # ── Correlation flag ─────────────────────────────────────────
    if critical_srvs and breach_count > 0:
        add_flag(
            "Correlation",
            f"{breach_count} SLA breach(es) co-exist with {len(critical_srvs)} critically loaded server(s).",
            "Has a formal temporal correlation analysis been run to separate resource-caused "
            "failures from scheduling conflicts?",
            "CRITICAL",
            f"{breach_count} breaches + {len(critical_srvs)} critical servers",
        )

    # ── Standard PE pre-go-live questions ───────────────────────
    add_flag(
        "Testing",
        "Production go-live requires validated performance under full concurrent user and batch load.",
        "Has a performance test been executed simulating production peak load including batch execution? "
        "What were the results and were they accepted?",
        "HIGH", "Pre-go-live requirement",
    )

    add_flag(
        "DR",
        "Production readiness requires a validated Disaster Recovery failover procedure.",
        "Has DR failover been tested end-to-end in this environment? "
        "What is the agreed RTO/RPO target and was it achieved in the test?",
        "HIGH", "Pre-go-live requirement",
    )

    add_flag(
        "Monitoring",
        "Proactive monitoring is required to detect performance degradation before users are impacted.",
        "Are Zabbix/Azure Monitor alerts configured for CPU >80%, Memory >85%, Disk >85%, "
        "and batch SLA breach? Who is the on-call escalation contact for alerts?",
        "MEDIUM", "Pre-go-live requirement",
    )

    # ── Tally by risk level ──────────────────────────────────────
    by_risk: Dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in flags:
        by_risk[f.risk] = by_risk.get(f.risk, 0) + 1

    # Default risk matrix entry if nothing generated
    if not risk_matrix and total_jobs == 0 and not servers:
        risk_matrix.append(RiskItem(
            area="Data Completeness",
            risk="MEDIUM",
            impact="No batch or resource data available — analysis may be incomplete",
            recommendation="Upload Ctrl-M CSV and resource utilisation report for a full risk assessment",
        ))

    resp = RedFlagsResponse(
        flags=flags,
        risk_matrix=risk_matrix,
        total=len(flags),
        by_risk=by_risk,
    )
    try:
        from services import session_cache
        session_cache.set("last_red_flags", resp.model_dump())
    except Exception:
        pass
    return resp
