"""
Cross-source Correlation Analysis router.

POST /api/correlate
Matches CTRL-M batch performance issues against server resource peaks
and derives root cause classifications + ranked insights.
No external dependencies — pure Python logic over the data already
parsed by the batch and resource routers.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from services.pe_utils import coerce_float as _f

router = APIRouter()


# ── Request / Response models ───────────────────────────────────

class CorrelateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_kpis:    Optional[Dict[str, Any]]       = None
    top_jobs:      Optional[List[Dict[str, Any]]] = None
    top_breaches:  Optional[List[Dict[str, Any]]] = None
    resource_kpis: Optional[Dict[str, Any]]       = None
    servers:       Optional[List[Dict[str, Any]]] = None
    anomalies:     Optional[List[Dict[str, Any]]] = None
    sub_stats:     Optional[List[Dict[str, Any]]] = None


class CorrelationRow(BaseModel):
    job:        str
    status:     str    # BREACH | AT_RISK | OK
    peak_hrs:   float
    server:     str
    cpu_pct:    Optional[float] = None   # None when no server data linked
    mem_pct:    Optional[float] = None
    root_cause: str    # RESOURCE_CPU | RESOURCE_MEM | CAPACITY | SCHEDULING | UNDETERMINED
    risk:       str    # CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN


class CorrelateResponse(BaseModel):
    rows:    List[CorrelationRow]
    summary: Dict[str, Any]
    insights: List[str]
    ai_narrative: Optional[str] = None
    ai_model:     Optional[str] = None



# ── Endpoint ────────────────────────────────────────────────────

@router.post("/correlate", response_model=CorrelateResponse)
def correlate(body: CorrelateRequest) -> CorrelateResponse:
    bk           = body.batch_kpis    or {}
    rk           = body.resource_kpis or {}
    servers      = body.servers       or []
    anomalies    = body.anomalies     or []
    top_breaches = body.top_breaches  or []
    top_jobs     = body.top_jobs      or []

    # Tier servers by CPU saturation
    critical_srvs = sorted(
        [s for s in servers if _f(s.get("cpu_used")) >= 90],
        key=lambda s: _f(s.get("cpu_used")), reverse=True,
    )
    high_srvs = sorted(
        [s for s in servers if 80 <= _f(s.get("cpu_used")) < 90],
        key=lambda s: _f(s.get("cpu_used")), reverse=True,
    )
    primary = (
        critical_srvs[0] if critical_srvs else
        high_srvs[0]     if high_srvs     else
        servers[0]        if servers       else None
    )

    rows: List[CorrelationRow] = []

    # ── Map breaching jobs → most likely culprit server ──────────
    breach_names = {j.get("Job_Name") for j in top_breaches}

    for job in top_breaches:
        job_name  = job.get("Job_Name", "Unknown")
        peak_hrs  = _f(job.get("peak_hrs"))

        if critical_srvs:
            srv  = critical_srvs[0]
            cpu  = _f(srv.get("cpu_used"))
            mem  = _f(srv.get("mem_used"))
            root = "RESOURCE_CPU" if cpu >= 90 else ("RESOURCE_MEM" if mem >= 90 else "CAPACITY")
            risk = "CRITICAL"
        elif high_srvs:
            srv  = high_srvs[0]
            cpu  = _f(srv.get("cpu_used"))
            mem  = _f(srv.get("mem_used"))
            root = "RESOURCE_CPU" if cpu >= 85 else ("RESOURCE_MEM" if mem >= 80 else "SCHEDULING")
            risk = "HIGH"
        elif servers:
            # We have server data but no critical/high-risk servers — most
            # likely the breach is scheduling-related, not resource-bound.
            srv  = servers[0]
            cpu  = _f(srv.get("cpu_used"))
            mem  = _f(srv.get("mem_used"))
            root = "SCHEDULING"
            risk = "MEDIUM"
        else:
            # No resource data at all — we cannot attribute a root cause.
            srv  = {}
            cpu  = None
            mem  = None
            root = "UNDETERMINED"
            risk = "UNKNOWN"

        rows.append(CorrelationRow(
            job=job_name, status="BREACH", peak_hrs=peak_hrs,
            server=srv.get("host", "No server data"),
            cpu_pct=cpu, mem_pct=mem,
            root_cause=root, risk=risk,
        ))

    # ── Map at-risk jobs (not in breaches) ───────────────────────
    for job in (top_jobs or [])[:15]:
        if job.get("Job_Name") in breach_names:
            continue
        job_name   = job.get("Job_Name", "Unknown")
        peak_hrs   = _f(job.get("peak_hrs"))
        buffer_pct = _f(job.get("buffer_pct"), 100)

        risk = "HIGH" if buffer_pct < 20 else ("MEDIUM" if buffer_pct < 40 else "LOW")
        if primary:
            root = "CAPACITY" if buffer_pct < 20 else "SCHEDULING"
            srv_name = primary.get("host", "No server data")
            cpu_val  = _f(primary.get("cpu_used"))
            mem_val  = _f(primary.get("mem_used"))
        else:
            # No server data linked to this job — cannot determine cause.
            root = "UNDETERMINED"
            risk = "UNKNOWN"
            srv_name = "No server data"
            cpu_val  = None
            mem_val  = None

        rows.append(CorrelationRow(
            job=job_name, status="AT_RISK", peak_hrs=peak_hrs,
            server=srv_name, cpu_pct=cpu_val, mem_pct=mem_val,
            root_cause=root, risk=risk,
        ))

    # ── Compute summary percentages ──────────────────────────────
    total = len(rows)
    if total:
        res_c  = sum(1 for r in rows if r.root_cause.startswith("RESOURCE"))
        sched  = sum(1 for r in rows if r.root_cause == "SCHEDULING")
        cap    = sum(1 for r in rows if r.root_cause == "CAPACITY")
        unk    = sum(1 for r in rows if r.root_cause in ("UNDETERMINED", "UNKNOWN"))
        pct    = lambda n: round(n / total * 100, 1)
    else:
        res_c = sched = cap = unk = 0
        pct   = lambda _: 0.0

    summary = {
        "total":                total,
        "resource_caused_pct":  pct(res_c),
        "scheduling_pct":       pct(sched),
        "capacity_pct":         pct(cap),
        "undetermined_pct":     pct(unk),
        "unknown_pct":          pct(unk),  # back-compat alias
        "primary_constraint":   primary.get("host", "No resource data") if primary else "No resource data",
        "critical_server_count": len(critical_srvs),
        "high_server_count":     len(high_srvs),
        "breach_count":         int(bk.get("jobs_breach", 0)),
        "fleet_grade":          rk.get("fleet_grade", ""),
    }

    # ── Check resource data quality ───────────────────────────────
    resource_data_available = False
    if servers:
        known_count = sum(
            1 for s in servers
            if _f(s.get("cpu_used")) > 0 or _f(s.get("mem_used")) > 0
        )
        resource_data_available = known_count > 0

    summary["resource_data_available"] = resource_data_available
    if not resource_data_available and servers:
        summary["resource_warning"] = (
            "Resource data not yet available — root cause classification "
            "is based on scheduling analysis only."
        )

    # ── Generate ranked insights ─────────────────────────────────
    insights: List[str] = []

    if not resource_data_available and servers:
        insights.insert(0,
            "Resource metrics are 0.0% for all servers — root cause classification "
            "is based on scheduling analysis only. Upload a valid resource utilisation "
            "report (Zabbix/Azure) to enable infrastructure correlation."
        )

    if total == 0:
        insights.append(
            "No correlation data — upload a Ctrl-M CSV and a resource DOCX/PDF to enable analysis."
        )
    else:
        res_pct = pct(res_c)
        sc_pct  = pct(sched)
        if res_pct >= 50:
            insights.append(
                f"{res_pct:.0f}% of batch issues correlate with resource pressure. "
                "Server scaling or workload redistribution is the highest-ROI fix."
            )
        if critical_srvs:
            top = critical_srvs[0]
            insights.append(
                f"{top.get('host','?')} is at {_f(top.get('cpu_used')):.0f}% CPU — "
                "primary infrastructure bottleneck. Address before production cutover."
            )
        if sc_pct >= 30:
            insights.append(
                f"{sc_pct:.0f}% of issues appear scheduling-related. "
                "Review Ctrl-M dependency chains for serialisation and parallelisation opportunities."
            )
        breach_count = int(bk.get("jobs_breach", 0))
        if breach_count:
            insights.append(
                f"{breach_count} job(s) breached SLA. "
                "Root cause resolution must be completed and signed off before go-live."
            )
        fg = rk.get("fleet_grade", "")
        if fg in ("C", "D", "F"):
            insights.append(
                f"Fleet health grade {fg} — infrastructure remediation required before PE audit sign-off."
            )
        dual = [
            s for s in servers
            if _f(s.get("cpu_used")) >= 80 and _f(s.get("mem_used")) >= 70
        ]
        if dual:
            insights.append(
                f"{len(dual)} server(s) under dual CPU+memory pressure — highest risk of cascade failures."
            )

    return CorrelateResponse(rows=rows, summary=summary, insights=insights)
