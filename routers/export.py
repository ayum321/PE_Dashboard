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
from services.resource_calculator import (
    role_cpu_thresholds as _role_cpu_thr,
    DB_MEM_EXPECTED_LO as _DB_MEM_LO,
    DB_MEM_EXPECTED_HI as _DB_MEM_HI,
)

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
    return '<span class="tag tag-gray">N/A</span>'


def _g(x: Any) -> str:
    """Format a threshold compactly (drops a trailing .0)."""
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return str(x)


def _metric_cell(val: float, amber_at: float, red_at: float, sub: str) -> str:
    """A coloured metric tag with a dim governing-threshold sub-line so the
    reader can verify the grade against the exact ceiling that governed it
    (instead of trusting a summary sentence). Monotone bands: green < amber_at,
    amber in [amber_at, red_at), red >= red_at."""
    if val <= 0:
        return '<span class="tag tag-gray">N/A</span>'
    cls = "tag-red" if val >= red_at else ("tag-amber" if val >= amber_at else "tag-green")
    return (f'<span class="tag {cls}">{val:.1f}%</span>'
            f'<div class="dim" style="margin-top:3px;font-size:10px">{sub}</div>')


def _cpu_cell(cpu: float, stype: str) -> str:
    """Role-aware CPU cell — APP 60/80, DB 85/95, SRE 90/100 (same ceilings the
    live fleet grader applies). Governing pair shown inline."""
    rt = _role_cpu_thr(stype)
    ok, warn = rt["ok"], rt["warn"]
    return _metric_cell(cpu, ok, warn, f"{stype} thr {_g(ok)}/{_g(warn)}")


def _mem_cell(mem: float, stype: str, mem_status: str | None) -> str:
    """Role-aware memory cell. DB servers pre-allocate the SGA/PGA band
    (DB_MEM_EXPECTED_LO–HI) by design, so memory inside that band is EXPECTED,
    not a warning — matching resource_calculator's grader. Other roles fall back
    to the global MEM warn/crit thresholds."""
    if mem <= 0:
        return '<span class="tag tag-gray">N/A</span>'
    if (stype or "").upper() == "DB":
        live = (mem_status or "").upper()
        if live == "DB_HIGH" or (not live and mem > _DB_MEM_HI):
            cls, sub = "tag-red", f"&gt; {_g(_DB_MEM_HI)}% SGA ceiling"
        else:
            cls, sub = "tag-green", f"SGA band {_g(_DB_MEM_LO)}–{_g(_DB_MEM_HI)}%"
        return (f'<span class="tag {cls}">{mem:.1f}%</span>'
                f'<div class="dim" style="margin-top:3px;font-size:10px">{sub}</div>')
    return _metric_cell(mem, MEM_OK, MEM_WARN, f"warn {_g(MEM_OK)}/{_g(MEM_WARN)}")


def _health_badge(s: dict, cpu: float, mem: float, disk: float, stype: str) -> str:
    """Overall server health. PREFER the live pre-computed role-aware `status`
    (single source of truth — it already folds in the DB SGA band override and
    aggregation-trap handling). Re-derive role-aware only when status is absent
    so a legacy/partial payload still grades correctly (never with a flat number)."""
    _map = {
        "critical": '<span class="tag tag-red">CRITICAL</span>',
        "warning":  '<span class="tag tag-amber">WARNING</span>',
        "healthy":  '<span class="tag tag-green">HEALTHY</span>',
        "unknown":  '<span class="tag tag-gray">UNKNOWN</span>',
    }
    live = (s.get("status") or "").strip().lower()
    if live in _map:
        return _map[live]
    # Fallback: role-aware re-derivation (mirrors resource_calculator bands).
    rt = _role_cpu_thr(stype)
    is_db = (stype or "").upper() == "DB"
    mem_red  = (mem > _DB_MEM_HI) if is_db else (mem >= MEM_WARN)
    mem_amber = False if is_db else (mem >= MEM_OK)
    if cpu >= rt["warn"] or disk >= DISK_WARN or mem_red:
        return _map["critical"]
    if cpu >= rt["ok"] or disk >= DISK_OK or mem_amber:
        return _map["warning"]
    return _map["healthy"]


def _srv_rows(servers: List[dict]) -> str:
    if not servers:
        return ("<tr><td colspan='6' class='empty'>No server data captured "
                "for this engagement.</td></tr>")
    rows = []
    for s in servers:
        # Prefer effective_cpu (aggregation-trap aware) — the value the live
        # grader actually scored — falling back to raw cpu when absent.
        _eff = s.get("effective_cpu")
        cpu  = _f(_eff if _eff is not None else (s.get("cpu_pct", 0) or s.get("cpu_used", 0)))
        mem  = _f(s.get("mem_pct",  0) or s.get("mem_used",  0))
        disk = _f(s.get("disk_pct", 0) or s.get("disk_used_max", 0))
        ram  = _f(s.get("mem_gb", 0) or s.get("mem_total_gb", 0))
        host = _esc(s.get("host") or s.get("server") or "?")
        stype = (s.get("type") or "APP").upper()
        stype_esc = _esc(stype)
        img_only = s.get("image_only", False)
        if img_only or (cpu == 0 and mem == 0 and disk == 0):
            status = '<span class="tag tag-gray">IMAGE ONLY</span>'
            cpu_td = mem_td = dsk_td = '<span class="dim">—</span>'
        else:
            status = _health_badge(s, cpu, mem, disk, stype)
            cpu_td = _cpu_cell(cpu, stype)
            mem_td = _mem_cell(mem, stype, s.get("mem_status"))
            dsk_td = _metric_cell(disk, DISK_OK, DISK_WARN, f"warn {_g(DISK_OK)}/{_g(DISK_WARN)}")
        sub = host if not ram else f"{host} &middot; {ram:.0f} GB RAM"
        rows.append(f"""<tr>
          <td class="host-cell"><b>{host.split(".")[0]}</b><br><span class="dim">{sub}</span></td>
          <td><span class="tag tag-blue">{stype_esc}</span></td>
          <td>{cpu_td}</td><td>{mem_td}</td><td>{dsk_td}</td>
          <td>{status}</td>
        </tr>""")
    return "".join(rows)


def _iss_rows(issues: List[dict]) -> str:
    if not issues:
        return ("<tr><td colspan='6' class='empty'>No open issues recorded.</td></tr>")
    sev_map = {"Critical": "tag-red", "High": "tag-amber", "Medium": "tag-amber",
               "Low": "tag-green", "Informational": "tag-blue"}
    rows = []
    for i in issues:
        sc = sev_map.get(i.get("Severity", ""), "tag-gray")
        rows.append(f"""<tr>
          <td><b>{_esc(i.get('ID',''))}</b></td>
          <td><span class="tag {sc}">{_esc(i.get('Severity',''))}</span></td>
          <td>{_esc(i.get('Type',''))}</td>
          <td>{_esc(i.get('Status',''))}</td>
          <td>{_esc(i.get('Description',''))}</td>
          <td class="dim">{_esc(i.get('Owner','') or '—')}</td>
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
        cls  = "check--on" if checked else "check--off"
        mark = "✓" if checked else ""
        rows.append(
            f'<div class="check {cls}"><span class="check__mark">{mark}</span>'
            f'<span>{_esc(label)}</span></div>'
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
        sign_state = "approved" if both_ok else "pending"
        sign_text  = "APPROVED" if both_ok else "PENDING"
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
        n_healthy = max(0, n_srv - n_crit - n_warn_s)

        # Gauge geometry — sweep angle (deg) for the conic-gradient rings so the
        # ambient Grafana-style dials render server-side with no JS.
        comp_deg  = max(0.0, min(100.0, comp_pct))      * 3.6
        score_deg = max(0.0, min(100.0, fleet_score))   * 3.6
        # Server severity distribution as % widths for the stacked health bar.
        _sv_tot   = max(1, n_srv)
        crit_pct_w = round(n_crit    / _sv_tot * 100, 1)
        warn_pct_w = round(n_warn_s  / _sv_tot * 100, 1)
        ok_pct_w   = round(n_healthy / _sv_tot * 100, 1)
        # Live thresholds for honest labels (read fresh so a Settings change shows).
        cpu_ok_t, cpu_warn_t = _g(pe_config.CPU_WARN), _g(pe_config.CPU_CRIT)
        mem_ok_t, mem_warn_t = _g(pe_config.MEM_WARN), _g(pe_config.MEM_CRIT)
        disk_ok_t, disk_warn_t = _g(pe_config.DISK_WARN), _g(pe_config.DISK_CRIT)
        # Role-aware CPU ceilings + DB SGA band — single-sourced from the live
        # fleet grader so the subtitle states exactly what the table was graded on.
        _appt, _dbt, _sret = _role_cpu_thr("APP"), _role_cpu_thr("DB"), _role_cpu_thr("SRE")
        role_cpu_label = (f"APP {_g(_appt['ok'])}/{_g(_appt['warn'])} · "
                          f"DB {_g(_dbt['ok'])}/{_g(_dbt['warn'])} · "
                          f"SRE {_g(_sret['ok'])}/{_g(_sret['warn'])}")
        db_mem_label = f"{_g(_DB_MEM_LO)}–{_g(_DB_MEM_HI)}%"

        ctx = dict(
            customer=customer, env=env, gen_date=gen_date,
            sign_color=sign_color, sign_label=sign_label,
            sign_state=sign_state, sign_text=sign_text,
            pe_name=pe_name, cust_name=cust_name,
            pe_tick=pe_tick, cu_tick=cu_tick,
            pe_approved=pe_approved, cust_approved=cust_approved,
            pe_date=pe_date, cust_date=cust_date,
            notes=_esc(notes),
            comp_pct=comp_pct, comp_col=comp_col, comp_deg=comp_deg,
            n_breach=n_breach, n_ok_jobs=n_ok_jobs,
            n_jobs=n_jobs, total_hrs=total_hrs, total_runs=total_runs,
            fleet_grade=fleet_grade, fleet_score=fleet_score, score_deg=score_deg,
            grade_color=grade_color,
            n_srv=n_srv, n_crit=n_crit, n_warn_s=n_warn_s, n_healthy=n_healthy,
            crit_pct_w=crit_pct_w, warn_pct_w=warn_pct_w, ok_pct_w=ok_pct_w,
            n_issues=len(issues),
            srv_rows=_srv_rows(servers),
            top_rows=_top_rows(top_jobs_data),
            iss_rows=_iss_rows(issues),
            checklist_rows=_checklist_rows(checklist),
            daily_limit=DAILY_LIMIT_HRS,
            monthly_limit=MONTHLY_LIMIT_HRS,
            capture_days=pe_config.RESOURCE_CAPTURE_DAYS,
            cpu_ok_t=cpu_ok_t, cpu_warn_t=cpu_warn_t,
            mem_ok_t=mem_ok_t, mem_warn_t=mem_warn_t,
            disk_ok_t=disk_ok_t, disk_warn_t=disk_warn_t,
            role_cpu_label=role_cpu_label, db_mem_label=db_mem_label,
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
