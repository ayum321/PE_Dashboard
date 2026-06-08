"""
Export router — generates a standalone downloadable HTML PE Audit Report.

POST /api/export-report
    body: the full window.appData JSON from the frontend
    response: text/html file download

The Jinja2 template `report_export.html` is rendered server-side so all
dynamic values (batch KPIs, resource stats, issues, approvals) are stamped
directly into the HTML.  The result is 100 % self-contained — no CDN, no
JS framework, no external assets — safe to email or archive.
"""
from __future__ import annotations

import html as html_lib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict

from services import pe_config
from services.pe_utils import coerce_float as _f

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ── Thresholds — sourced from pe_config (user-configurable via Settings) ──────────
CPU_OK  = pe_config.CPU_WARN
CPU_WARN = pe_config.CPU_CRIT
MEM_OK  = pe_config.MEM_WARN
MEM_WARN = pe_config.MEM_CRIT
DISK_OK  = pe_config.DISK_WARN
DISK_WARN = pe_config.DISK_CRIT
DAILY_LIMIT_HRS   = pe_config.SLA_DAILY_HRS
MONTHLY_LIMIT_HRS = pe_config.SLA_MONTHLY_HRS


# ── Pydantic models ────────────────────────────────────────────
class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    upload:    Optional[Dict[str, Any]] = None
    servers:   Optional[List[Dict[str, Any]]] = None
    batch:     Optional[Dict[str, Any]] = None
    resource:  Optional[Dict[str, Any]] = None
    issues:    Optional[List[Dict[str, Any]]] = None
    approvals: Optional[Dict[str, Any]] = None


# ── Helpers ────────────────────────────────────────────────────
# coerce_float is imported from pe_utils — no local _f needed

def _esc(s: Any) -> str:
    return html_lib.escape(str(s or ""))


def _tag(val: float, ok: float, warn: float, fmt: str = "{:.1f}%") -> str:
    v = fmt.format(val)
    if val >= warn:
        return f'<span class="tag tag-red">{v}</span>'
    if val >= ok:
        return f'<span class="tag tag-amber">{v}</span>'
    if val > 0:
        return f'<span class="tag tag-green">{v}</span>'
    return '<span class="tag tag-muted">N/A</span>'


def _srv_rows(servers: List[dict]) -> str:
    if not servers:
        return "<tr><td colspan='8' class='dim' style='text-align:center;padding:20px'>No server data</td></tr>"
    rows = []
    for s in servers:
        cpu  = _f(s.get("cpu_pct",  0) or s.get("cpu_used",  0))
        mem  = _f(s.get("mem_pct",  0) or s.get("mem_used",  0))
        disk = _f(s.get("disk_pct", 0) or s.get("disk_used_max", 0))
        ram  = _f(s.get("mem_gb", 0) or s.get("mem_total_gb", 0))
        host = _esc(s.get("host") or s.get("server") or "?")
        stype = _esc(s.get("type", "APP"))
        img_only = s.get("image_only", False)
        if img_only or (cpu == 0 and mem == 0 and disk == 0):
            status = '<span class="tag tag-muted">IMAGE ONLY</span>'
            cpu_td = mem_td = dsk_td = '<span class="dim">—</span>'
        else:
            worst = max(cpu, mem, disk)
            if worst >= 90:   status = '<span class="tag tag-red">CRITICAL</span>'
            elif worst >= 75: status = '<span class="tag tag-amber">WARNING</span>'
            else:             status = '<span class="tag tag-green">HEALTHY</span>'
            cpu_td = _tag(cpu, CPU_OK, CPU_WARN)
            mem_td = _tag(mem, MEM_OK, MEM_WARN)
            dsk_td = _tag(disk, DISK_OK, DISK_WARN)
        ram_disp = f"{ram:.1f} GB" if ram else "—"
        disks = s.get("disks") or {}
        mounts_html = " ".join(
            f'<span class="mtag">{_esc(k)}: {_f(v):.0f}%</span>'
            for k, v in list(disks.items())[:4]
        ) or '<span class="dim">—</span>'
        rows.append(f"""<tr>
          <td class="host-cell"><b>{host.split(".")[0]}</b><br><span class="dim">{host}</span></td>
          <td><span class="tag tag-blue">{stype}</span></td>
          <td>{cpu_td}</td><td>{mem_td}</td>
          <td class="dim">{ram_disp}</td>
          <td>{dsk_td}</td>
          <td style="font-size:10px">{mounts_html}</td>
          <td>{status}</td>
        </tr>""")
    return "".join(rows)


def _iss_rows(issues: List[dict]) -> str:
    if not issues:
        return "<tr><td colspan='8' class='dim' style='text-align:center;padding:20px'>No issues logged</td></tr>"
    sev_map = {"Critical": "tag-red", "High": "tag-amber", "Medium": "tag-amber",
               "Low": "tag-green", "Informational": "tag-blue"}
    rows = []
    for i in issues:
        sc = sev_map.get(i.get("Severity", ""), "tag-muted")
        rows.append(f"""<tr>
          <td><b>{_esc(i.get('ID',''))}</b></td>
          <td><span class="tag {sc}">{_esc(i.get('Severity',''))}</span></td>
          <td>{_esc(i.get('Type',''))}</td>
          <td>{_esc(i.get('Status',''))}</td>
          <td>{_esc(i.get('Description',''))}</td>
          <td class="dim">{_esc(i.get('Mitigation',''))}</td>
          <td class="dim">{_esc(i.get('Owner',''))}</td>
          <td class="dim">{_esc(i.get('ETA',''))}</td>
        </tr>""")
    return "".join(rows)


def _top_rows(top_jobs: List[dict]) -> str:
    if not top_jobs:
        return "<tr><td colspan='5' class='dim' style='text-align:center;padding:20px'>No batch data</td></tr>"
    rows = []
    for r in top_jobs[:20]:
        peak  = _f(r.get("peak_hrs", 0))
        avg   = _f(r.get("avg_hrs",  0))
        buf   = _f(r.get("buffer_pct", (DAILY_LIMIT_HRS - peak) / DAILY_LIMIT_HRS * 100))
        name  = _esc(r.get("Job_Name") or r.get("job_name") or "?")
        if peak > DAILY_LIMIT_HRS:
            status = '<span class="tag tag-red">BREACH</span>'
            peak_style = 'style="color:#ef4444;font-weight:700"'
        elif buf < 15:
            status = '<span class="tag tag-amber">AT RISK</span>'
            peak_style = ""
        else:
            status = '<span class="tag tag-green">OK</span>'
            peak_style = ""
        rows.append(f"""<tr>
          <td><b>{name}</b></td>
          <td {peak_style}>{peak:.3f}h</td>
          <td class="dim">{avg:.3f}h</td>
          <td>{buf:.1f}%</td>
          <td>{status}</td>
        </tr>""")
    return "".join(rows)


def _checklist_rows(checklist: dict) -> str:
    labels = {
        "batch":   "Batch SLA validated (daily/weekly/monthly)",
        "res":     "Resource utilization within thresholds",
        "data":    "Data volume (DFU/SKU) vs SOW verified",
        "issues":  "Issues & waivers acknowledged",
        "perf":    "Batch performance-test report reviewed",
        "ctrlm":   "Ctrl-M 30-day execution history reviewed",
        "ui":      "UI performance benchmarking approved",
        "sow":     "SOW service IDs & scenarios confirmed",
        "res15":   f"Resource utilization (last {pe_config.RESOURCE_CAPTURE_DAYS} days) reviewed",
    }
    rows = []
    for key, label in labels.items():
        checked = bool(checklist.get(key, False))
        icon  = "✅" if checked else "⬜"
        color = "var(--green)" if checked else "var(--text-muted)"
        rows.append(
            f'<div style="padding:5px 0;font-size:12px;color:{color}">'
            f'{icon} &nbsp;{_esc(label)}</div>'
        )
    return "".join(rows)


# ── Endpoint ───────────────────────────────────────────────────
@router.post(
    "/export-report",
    response_class=HTMLResponse,
    summary="Render and download the standalone PE Audit HTML report",
)
async def export_report(request: Request, body: ExportRequest) -> HTMLResponse:
    try:
        # ── Extract sub-trees ──────────────────────────────────
        batch     = body.batch     or {}
        resource  = body.resource  or {}
        issues    = body.issues    or []
        approvals = body.approvals or {}
        servers   = body.servers   or []

        batch_kpis    = batch.get("kpis")    or {}
        resource_kpis = resource.get("kpis") or {}
        top_jobs_data = batch.get("top_jobs") or batch.get("top_breaches") or []

        checklist    = approvals.get("checklist",  {})
        pe_info      = approvals.get("pe",         {})
        cust_info    = approvals.get("customer",   {})
        notes        = approvals.get("notes",      "")
        pe_approved  = bool(pe_info.get("approved",   False))
        cust_approved= bool(cust_info.get("approved", False))
        both_ok      = pe_approved and cust_approved

        customer   = _esc(approvals.get("customer_name", "") or "Unknown Customer")
        env        = _esc(approvals.get("env_type",       "") or "Production")
        pe_name    = _esc(pe_info.get("name",   "") or "—")
        cust_name  = _esc(cust_info.get("name", "") or "—")
        pe_date    = _esc(pe_info.get("date",   ""))
        cust_date  = _esc(cust_info.get("date", ""))

        gen_date   = datetime.now().strftime("%d %b %Y, %I:%M %p")
        sign_color = "#22c55e" if both_ok else "#f59e0b"
        sign_label = "✅ APPROVED" if both_ok else "⏳ PENDING"
        pe_tick    = "✅" if pe_approved   else "⏳"
        cu_tick    = "✅" if cust_approved else "⏳"

        # ── KPIs ───────────────────────────────────────────────
        comp_pct  = _f(batch_kpis.get("compliance_pct", 0))
        comp_col  = "#22c55e" if comp_pct >= 99 else ("#f59e0b" if comp_pct >= 85 else "#ef4444")
        n_breach  = int(batch_kpis.get("jobs_breach", 0))
        n_ok_jobs = int(batch_kpis.get("jobs_ok", 0))
        n_jobs    = int(batch_kpis.get("total_jobs", 0))
        total_hrs = _f(batch_kpis.get("total_hrs", 0))
        total_runs= int(batch_kpis.get("total_runs", 0))

        fleet_grade = resource_kpis.get("fleet_grade", "?")
        fleet_score = _f(resource_kpis.get("fleet_score", 0))
        grade_color = {"A": "#22c55e", "B": "#06b6d4", "C": "#f59e0b",
                       "D": "#fb923c", "F": "#ef4444"}.get(fleet_grade, "#6b7a99")
        n_srv    = int(resource_kpis.get("total_servers", len(servers)))
        n_crit   = int(resource_kpis.get("n_critical", 0))
        n_warn_s = int(resource_kpis.get("n_warning",  0))

        ctx = dict(
            customer=customer, env=env, gen_date=gen_date,
            sign_color=sign_color, sign_label=sign_label,
            pe_name=pe_name, cust_name=cust_name,
            pe_tick=pe_tick, cu_tick=cu_tick,
            pe_approved=pe_approved, cust_approved=cust_approved,
            pe_date=pe_date, cust_date=cust_date,
            notes=_esc(notes),
            comp_pct=comp_pct, comp_col=comp_col,
            n_breach=n_breach, n_ok_jobs=n_ok_jobs,
            n_jobs=n_jobs, total_hrs=total_hrs, total_runs=total_runs,
            fleet_grade=fleet_grade, fleet_score=fleet_score,
            grade_color=grade_color,
            n_srv=n_srv, n_crit=n_crit, n_warn_s=n_warn_s,
            n_issues=len(issues),
            srv_rows=_srv_rows(servers),
            top_rows=_top_rows(top_jobs_data),
            iss_rows=_iss_rows(issues),
            checklist_rows=_checklist_rows(checklist),
            daily_limit=DAILY_LIMIT_HRS,
            monthly_limit=MONTHLY_LIMIT_HRS,
        )

        html = templates.get_template("report_export.html").render(**ctx)
        filename = f"PE_Audit_{customer.replace(' ','_')}_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
        return HTMLResponse(
            content=html,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "text/html; charset=utf-8",
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc
