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
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel, ConfigDict

from services import pe_config
from services import report_archive
from services.audit_report_payload import attach_prior_audit, build_audit_report_payload
from services.pe_utils import coerce_float as _f
from services.report_svg import render_report_charts
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


# ── Pydantic models ────────────────────────────────────────────
class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    upload:    Optional[Dict[str, Any]] = None
    servers:   Optional[List[Dict[str, Any]]] = None
    batch:     Optional[Dict[str, Any]] = None
    resource:  Optional[Dict[str, Any]] = None
    issues:    Optional[List[Dict[str, Any]]] = None
    approvals: Optional[Dict[str, Any]] = None
    # SOW volume compliance (window.appData.sowCompare) and performance
    # benchmark (window.appData.benchmark) — the checklist has always claimed
    # "Data volume (DFU/SKU) vs SOW verified" and "UI performance benchmarking
    # approved", but nothing ever passed this data to the report, so those
    # claims had zero supporting evidence in the exported document.
    sow:       Optional[Dict[str, Any]] = None
    benchmark: Optional[Dict[str, Any]] = None


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
        return "<tr><td colspan='6' class='dim' style='text-align:center;padding:20px'>No batch data</td></tr>"

    source_labels = {
        "sla_matrix": "Contracted",
        "batch_sla_xlsx": "Contracted",
        "batch_sla_xlsx_tokens": "Contracted",
        "customer_fallback": "Customer fallback",
        "sow_extracted": "SOW extracted",
        "global": "Global default",
        "adaptive": "Adaptive",
        "assumed": "Assumed",
        "default": "Default",
    }
    schedule_labels = {"DAILY": "Daily", "WEEKLY": "Weekly", "MONTHLY": "Monthly"}
    status_tags = {
        "BREACH": ("tag-red", "BREACH"),
        "AT_RISK": ("tag-amber", "AT RISK"),
        "LONG_JOB": ("tag-amber", "LONG JOB"),
        "OK": ("tag-green", "OK"),
        "SLA_MISSING": ("tag-gray", "SLA MISSING"),
    }

    def _finite(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def _legacy_status(peak: float, sla: float, buffer_pct: float | None) -> str:
        """Compatibility only for payloads predating canonical buffer_status."""
        if sla <= 0:
            return "SLA_MISSING"
        buffer_value = buffer_pct if buffer_pct is not None else (sla - peak) / sla * 100
        if buffer_value < 0:
            return "BREACH"
        if buffer_value <= float(pe_config.SLA_ATRISK_PCT):
            return "AT_RISK"
        if buffer_value <= float(pe_config.SLA_LONGJOB_PCT):
            return "LONG_JOB"
        return "OK"

    rows = []
    for r in top_jobs[:20]:
        peak = _finite(r.get("peak_hrs")) or 0.0
        avg = _finite(r.get("avg_hrs")) or 0.0
        canonical_status = str(r.get("buffer_status") or "").strip().upper()
        # Current payloads always carry both sla_hrs and buffer_status.  The
        # configured daily ceiling is strictly a legacy-payload fallback.
        sla_was_supplied = "sla_hrs" in r
        supplied_sla = _finite(r.get("sla_hrs"))
        # A missing key is a legacy-payload compatibility case.  An explicit
        # null, NaN, infinity, zero, or negative ceiling is not: surface it as
        # missing rather than inventing a configured default in the report.
        missing_sla = ((canonical_status == "SLA_MISSING" and (supplied_sla is None or supplied_sla <= 0))
                       or (sla_was_supplied and (supplied_sla is None or supplied_sla <= 0)))
        sla = supplied_sla
        if not missing_sla and (sla is None or sla <= 0):
            sla = float(pe_config.SLA_DAILY_HRS)
        buf = _finite(r.get("buffer_pct"))
        if buf is None and not missing_sla:
            buf = (sla - peak) / sla * 100 if sla > 0 else 0.0
        status_key = ("SLA_MISSING" if missing_sla else
                      (canonical_status if canonical_status in status_tags else _legacy_status(peak, sla, buf)))
        tag_class, status_text = status_tags[status_key]
        status = f'<span class="tag {tag_class}">{status_text}</span>'
        peak_style = 'style="color:#ef4444;font-weight:700"' if status_key == "BREACH" else ""
        name = _esc(r.get("Job_Name") or r.get("job_name") or "?")
        source_key = str(r.get("sla_source") or "default").strip().lower()
        source = source_labels.get(source_key, _esc(source_key.replace("_", " ").title()))
        schedule = schedule_labels.get(str(r.get("schedule_type") or "").strip().upper())
        schedule_tag = (f' <span class="tag tag-blue" style="font-size:9.5px;padding:1px 6px;">{schedule}</span>'
                        if schedule else "")
        sla_cell = (f'{sla:.2f}h' if not missing_sla else '<span class="tag tag-gray">N/A</span>')
        buffer_cell = f'{buf:.1f}%' if buf is not None else '—'
        rows.append(f"""<tr>
          <td><b>{name}</b>{schedule_tag}</td>
          <td {peak_style}>{peak:.3f}h</td>
          <td class="dim">{avg:.3f}h</td>
          <td class="dim">{sla_cell} <span class="tag tag-gray" style="font-size:9.5px;padding:1px 6px;">{source}</span></td>
          <td>{buffer_cell}</td>
          <td>{status}</td>
        </tr>""")
    return "".join(rows)


def _sow_status(pct: float) -> str:
    """Classify SOW consumption % against the PE standard process window.

    Mirrors routers/sow.py::_status() exactly (same pe_config thresholds) so
    the exported report can NEVER show a different verdict than the live SOW
    Contract tab for the same numbers — and so a stale/incomplete client
    payload (e.g. an old browser tab that never sent status) still gets a
    correct, real classification instead of "N/A".
    """
    if pct > pe_config.SOW_OVER_CRIT_PCT:  return "CRITICAL_OVER"
    if pct > pe_config.SOW_OVER_PCT:       return "OVER"
    if pct < pe_config.SOW_UNDER_PCT:      return "LOW"
    if pct < pe_config.SOW_ACCEPTABLE_PCT: return "ACCEPTABLE"
    return "OPTIMAL"


_SOW_STATUS_TAG = {
    "OPTIMAL":       ('tag-green', 'OPTIMAL'),
    "ACCEPTABLE":    ('tag-green', 'ACCEPTABLE'),
    "LOW":           ('tag-blue',  'UNDER-UTILISED'),
    "OVER":          ('tag-amber', 'OVER CONTRACT'),
    "CRITICAL_OVER": ('tag-red',   'CRITICAL OVER'),
}


def _sow_resolve(m: dict) -> tuple[float, float, str]:
    """Resolve (sow, actual, status) for one SOW metric, recomputing pct/status
    server-side when the client sent them missing or zeroed (stale cached JS,
    a manual-entry object built before both fields existed, etc.) even though
    sow/actual are real numbers. Returns (sow_v, act_v, pct, status_key) —
    the single source both _sow_rows() and the overall-status fallback use,
    so the per-row statuses and the header badge can never disagree.
    """
    sow_v  = _f(m.get("sow", 0))
    act_v  = _f(m.get("actual", 0))
    pct    = _f(m.get("pct", 0))
    status_key = (m.get("status") or "").upper()
    if pct <= 0 and sow_v > 0 and act_v > 0:
        pct = round(act_v / sow_v * 100, 1)
    if status_key not in _SOW_STATUS_TAG:
        status_key = _sow_status(pct) if (sow_v > 0 and act_v > 0) else "LOW"
    return sow_v, act_v, pct, status_key


def _sow_rows(metrics: List[dict]) -> str:
    """SOW volume-compliance rows — mirrors the dashboard's SOW Contract &
    Volume Compliance tab so the exported report shows the SAME evidence the
    checklist's "Data volume (DFU/SKU) vs SOW verified" line claims was reviewed.
    """
    if not metrics:
        return ("<tr><td colspan='5' class='empty'>No SOW contract data captured "
                "for this engagement.</td></tr>")
    rows = []
    for m in metrics:
        label  = _esc(m.get("label") or m.get("key") or "?")
        sow_v, act_v, pct, status_key = _sow_resolve(m)
        cls, label_txt = _SOW_STATUS_TAG.get(status_key, ('tag-gray', status_key or 'N/A'))
        pct_style = 'style="color:#ef4444;font-weight:700"' if status_key == "CRITICAL_OVER" else \
                    ('style="color:#f59e0b;font-weight:700"' if status_key == "OVER" else "")
        rows.append(f"""<tr>
          <td><b>{label}</b></td>
          <td class="num">{sow_v:,.0f}</td>
          <td class="num">{act_v:,.0f}</td>
          <td {pct_style}>{pct:.1f}%</td>
          <td><span class="tag {cls}">{label_txt}</span></td>
        </tr>""")
    return "".join(rows)


def _batch_perf_rows(bps: dict) -> str:
    """Batch-runtime performance rows (top regressions, worst-first) — used
    when the benchmark upload is a Ctrl-M runtime comparison (batch_perf_summary
    present) rather than a UI transaction benchmark (generic `rows`). Without
    this, a real batch-perf upload with genuine regressions rendered as
    "No benchmark data uploaded" because the report only ever looked at
    `benchmark.rows`, which batch-perf responses leave empty by design.
    """
    regressions = (bps.get("top_regressions") or [])[:10]
    if not regressions:
        return "<tr><td colspan='5' class='empty'>No regressions detected.</td></tr>"
    rows = []
    for r in regressions:
        name  = _esc(r.get("job") or "?")
        old_s = _f(r.get("old_secs", 0))
        new_s = _f(r.get("new_secs", 0))
        delta = _f(r.get("delta_pct", 0))
        rows.append(f"""<tr>
          <td><b>{name}</b></td>
          <td class="dim">{old_s:.1f}s</td>
          <td>{new_s:.1f}s</td>
          <td style="color:#ef4444;font-weight:700">+{delta:.1f}%</td>
          <td><span class="tag tag-red">REGRESSION</span></td>
        </tr>""")
    return "".join(rows)


def _bench_rows(rows_in: List[dict]) -> str:
    """Performance benchmark rows — mirrors the Performance Benchmark tab so
    the report backs the checklist's "Batch performance-test report reviewed"
    / "UI performance benchmarking approved" lines with actual numbers,
    sorted worst-regression-first so the most material result is visible
    without opening the full dashboard.
    """
    if not rows_in:
        return ("<tr><td colspan='5' class='empty'>No benchmark data captured "
                "for this engagement.</td></tr>")
    _status_tag = {"OK": "tag-green", "WATCH": "tag-amber", "BREACH": "tag-red", "REFERENCE": "tag-gray"}
    ordered = sorted(rows_in, key=lambda r: _f(r.get("delta_pct", 0)), reverse=True)
    rows = []
    for r in ordered[:20]:
        name   = _esc(r.get("transaction") or "?")
        base_s = _f(r.get("baseline_sec", 0))
        cur_s  = _f(r.get("current_sec", 0))
        delta  = _f(r.get("delta_pct", 0))
        status_key = (r.get("status") or "").upper()
        cls = _status_tag.get(status_key, "tag-gray")
        delta_style = 'style="color:#ef4444;font-weight:700"' if delta > 0 and status_key in ("WATCH", "BREACH") else ""
        delta_sign = "+" if delta > 0 else ""
        rows.append(f"""<tr>
          <td><b>{name}</b></td>
          <td class="dim">{base_s:.2f}s</td>
          <td>{cur_s:.2f}s</td>
          <td {delta_style}>{delta_sign}{delta:.1f}%</td>
          <td><span class="tag {cls}">{status_key or 'N/A'}</span></td>
        </tr>""")
    return "".join(rows)


def _checklist_rows(checklist: dict, evidence: dict) -> tuple[str, int]:
    """Render evidence-backed checklist claims and return their mismatch count."""
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
    mismatch_count = 0
    for key, label in labels.items():
        claimed = bool(checklist.get(key, False))
        backed = bool(evidence.get(key, True))
        checked = claimed and backed
        mismatch = claimed and not backed
        if mismatch:
            mismatch_count += 1
        cls = "check--on" if checked else ("check--mismatch" if mismatch else "check--off")
        mark = "✓" if checked else ("⚠" if mismatch else "")
        suffix = " — no supporting data in this export" if mismatch else ""
        rows.append(
            f'<div class="check {cls}"><span class="check__mark">{mark}</span>'
            f'<span>{_esc(label)}{_esc(suffix)}</span></div>'
        )
    return "".join(rows), mismatch_count


# ── Endpoint ───────────────────────────────────────────────────
@router.post(
    "/export-report",
    response_class=HTMLResponse,
    summary="Render and download the standalone PE Audit HTML report",
)
async def export_report(request: Request, body: ExportRequest) -> HTMLResponse:
    try:
        # Export v2 intentionally freezes the exact output from the dashboard
        # engines before any Jinja rendering.  The renderer has no SLA/severity
        # calculation path, so it cannot silently disagree with the live UI.
        export_body = body.model_dump(exclude_none=True)
        report = build_audit_report_payload(export_body)
        customer = report["meta"]["customer"]
        audit_id = report["meta"]["audit_id"]
        if customer and customer != "Customer not specified":
            attach_prior_audit(report, report_archive.get_previous_payload(customer, audit_id))
        else:
            attach_prior_audit(report, None)

        charts, chart_errors, chart_warnings = render_report_charts(report)
        report["validation"]["errors"].extend(chart_errors)
        report["validation"]["warnings"].extend(chart_warnings)
        if chart_errors:
            raise HTTPException(status_code=422, detail="; ".join(chart_errors))

        archive_status = "skipped"
        if customer and customer != "Customer not specified":
            # Immutable JSON exists before HTML rendering so a revisited audit
            # can always explain the exact evidence behind the document.
            snapshot = report_archive.save_payload_snapshot(report)
            if not snapshot.get("ok"):
                raise HTTPException(status_code=409, detail=snapshot.get("error", "Unable to archive immutable audit payload."))
            archive_status = "payload_saved"

        safe_charts = {name: Markup(svg) for name, svg in charts.items()}
        rendered_html = templates.get_template("report/report_v2.html").render(report=report, charts=safe_charts)
        if archive_status == "payload_saved":
            attached = report_archive.attach_snapshot_html(customer, audit_id, rendered_html)
            archive_status = "saved" if attached.get("ok") else "payload_saved_html_failed"

        filename = report_archive.download_filename(audit_id).replace("_archived", "")
        return HTMLResponse(
            content=rendered_html,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "text/html; charset=utf-8",
                "X-Archive-Status": archive_status,
                "X-Audit-Id": audit_id,
            },
        )

        # Legacy v1 renderer remains below temporarily to keep its historical
        # helper functions available during the migration; it is unreachable.
        # ── Extract sub-trees ──────────────────────────────────
        batch     = body.batch     or {}
        resource  = body.resource  or {}
        issues    = body.issues    or []
        approvals = body.approvals or {}
        servers   = body.servers   or []
        sow       = body.sow       or {}
        benchmark = body.benchmark or {}

        batch_kpis    = batch.get("kpis")    or {}
        resource_kpis = resource.get("kpis") or {}
        top_jobs_data = batch.get("top_jobs") or batch.get("top_breaches") or []
        sow_metrics   = sow.get("metrics") or []
        bench_rows_data = benchmark.get("rows") or []

        checklist    = approvals.get("checklist",  {})
        pe_info      = approvals.get("pe",         {})
        cust_info    = approvals.get("customer",   {})
        notes        = approvals.get("notes",      "")
        pe_approved  = bool(pe_info.get("approved",   False))
        cust_approved= bool(cust_info.get("approved", False))
        both_ok      = pe_approved and cust_approved
        # PE reviewer chose to sign off despite an unresolved checklist/blocker
        # gate (frontend disclaimer override) — surface this on the report
        # itself so the sign-off card never silently looks identical to a
        # clean approval.
        pe_override  = bool(pe_info.get("override_blockers", False))

        # Retain the raw values for local archive metadata. The report body
        # remains escaped below, but archive browsing must not show HTML entity
        # sequences in actual customer/reviewer names.
        raw_customer = str(approvals.get("customer_name", "") or "").strip()
        raw_env = str(approvals.get("env_type", "") or "Not Detected")
        raw_pe_name = str(pe_info.get("name", "") or "—")
        raw_cust_name = str(cust_info.get("name", "") or "—")
        customer   = _esc(raw_customer or "Customer not specified")
        # "Production" used to be a silent fallback here even when nothing had
        # actually been detected — for real customer data that reads as a
        # confident claim the report never verified. "Not Detected" is honest;
        # the frontend now auto-derives the real value before this is ever hit
        # (see exportHtmlReport() in static/app.js), so this fallback should be
        # rare in practice, not the normal path.
        env        = _esc(raw_env)
        pe_name    = _esc(raw_pe_name)
        cust_name  = _esc(raw_cust_name)
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
        n_at_risk = int(batch_kpis.get("jobs_at_risk", 0))
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

        # ── SOW volume compliance ───────────────────────────────────
        sow_status  = (sow.get("overall_status") or "").upper()
        sow_summary = _esc(sow.get("summary") or "")
        n_sow       = len(sow_metrics)
        # Defense-in-depth (mirrors _sow_resolve()): a client payload can omit
        # overall_status even when its metrics are real (stale cached JS, the
        # manual-entry fallback, a restored-but-different session shape) — this
        # is exactly what produced a "NOT ASSESSED" header badge sitting above
        # two fully-populated, real DFU/SKU rows in a live customer report.
        # Derive the worst per-metric status instead of ever defaulting to
        # "not assessed" when metrics are actually present.
        _SOW_SEVERITY = {"CRITICAL_OVER": 4, "OVER": 3, "LOW": 2, "ACCEPTABLE": 1, "OPTIMAL": 0}
        resolved_sow_statuses = [_sow_resolve(m)[3] for m in sow_metrics]
        if sow_status not in ("OPTIMAL", "ACCEPTABLE", "LOW", "OVER", "CRITICAL_OVER") and resolved_sow_statuses:
            sow_status = max(resolved_sow_statuses, key=lambda s: _SOW_SEVERITY.get(s, 0))
        elif "CRITICAL_OVER" in resolved_sow_statuses:
            # A client-side summary can be stale after an upload or browser
            # restore.  Never let a green/amber header conceal a rendered
            # critical over-consumption row.
            sow_status = "CRITICAL_OVER"
            sow_summary = ""
        elif "OVER" in resolved_sow_statuses and sow_status not in ("CRITICAL_OVER", "OVER"):
            # The same rule applies to non-critical contract overages.
            sow_status = "OVER"
            sow_summary = ""
        elif sow_status == "OPTIMAL" and resolved_sow_statuses and "OPTIMAL" not in resolved_sow_statuses:
            # A stale overall badge must not call the report OPTIMAL/HIGH when
            # every displayed metric is only in the lower acceptable band.
            sow_status = "ACCEPTABLE"
            sow_summary = ""
        # Disclaimer text — always shown, so a reader unfamiliar with the
        # audit's internal vocabulary knows exactly what each verdict means
        # before deciding whether it's acceptable, instead of just seeing a
        # colored word with no context.
        sow_disclaimer = {
            "CRITICAL_OVER": f"⚠ CRITICAL — actual volume exceeds {_g(pe_config.SOW_OVER_CRIT_PCT)}% of the contracted SOW ceiling. This is a commercial over-consumption event and must be formally acknowledged before PE sign-off.",
            "OVER":          f"⚠ Actual volume exceeds {_g(pe_config.SOW_OVER_PCT)}% of the contracted SOW ceiling — outside the standard process window. Requires formal review and acknowledgment.",
            "LOW":           f"ℹ Actual volume is below {_g(pe_config.SOW_UNDER_PCT)}% of the contracted SOW ceiling — under-utilised relative to contract. Findings are validated only at the tested volume, not at full contracted scale.",
            "ACCEPTABLE":    f"✓ Within the standard {_g(pe_config.SOW_UNDER_PCT)}%–{_g(pe_config.SOW_OVER_PCT)}% SOW process window (lower range).",
            "OPTIMAL":       f"✓ Within the preferred 90%–{_g(pe_config.SOW_OVER_PCT)}% SOW process window. Go-live confidence high.",
        }.get(sow_status, "SOW volume comparison not yet performed for this engagement — upload a SOW contract and Ctrl-M actuals to assess.")
        sow_badge = {
            "OPTIMAL": ("#22c55e", "OPTIMAL"), "ACCEPTABLE": ("#22c55e", "ACCEPTABLE"),
            "LOW": ("#3b82f6", "UNDER-UTILISED"), "OVER": ("#f59e0b", "OVER CONTRACT"),
            "CRITICAL_OVER": ("#ef4444", "CRITICAL OVER"),
        }.get(sow_status, ("#6b7a99", "NOT ASSESSED"))

        # ── Performance benchmark ────────────────────────────────
        bench_summary = _esc(benchmark.get("summary") or "")
        n_bench_total   = int(benchmark.get("total_transactions", len(bench_rows_data)))
        n_bench_breach  = int(benchmark.get("sla_breaches", 0))
        n_bench_degraded= int(benchmark.get("degraded", 0))
        bench_badge = ("#ef4444", f"{n_bench_breach} BREACH") if n_bench_breach else (
                      ("#f59e0b", f"{n_bench_degraded} DEGRADED") if n_bench_degraded else
                      ("#22c55e", "WITHIN TOLERANCE") if n_bench_total else ("#6b7a99", "NOT ASSESSED"))
        # Batch-runtime-performance uploads (Ctrl-M runtime comparison) store
        # their results in batch_perf_summary, NOT the generic `rows` array —
        # without this, a real upload with genuine regressions rendered as
        # "No benchmark data uploaded" underneath a summary line that plainly
        # described real regression/improvement counts.
        batch_perf = benchmark.get("batch_perf_summary") or {}
        has_batch_perf = bool(batch_perf)
        n_batch_perf_regr = int(batch_perf.get("regressions", 0))
        if has_batch_perf and not n_bench_total:
            bench_badge = ("#ef4444", f"{n_batch_perf_regr} REGRESSION") if n_batch_perf_regr \
                          else ("#22c55e", "WITHIN TOLERANCE")

        # Batch table is capped to the top 20 jobs by peak runtime for a
        # readable page — previously this cap had no caption, so the "{{n_jobs}}
        # jobs" panel badge and the 20-row table silently disagreed.
        n_jobs_shown = min(20, len(top_jobs_data))

        # Products/modules reviewed — engagement-scoped selection persisted by
        # routers/sow.py (config_store key "reviewed_products"), mirrored here so
        # the same badge strip shown on every dashboard tab also appears on the
        # exported HTML report header.
        reviewed_product_labels: List[str] = []
        try:
            from services import config_store as _cfg_store
            from services.product_taxonomy import labels_for as _labels_for
            _reviewed_ids = _cfg_store.get("reviewed_products") or []
            reviewed_product_labels = [_esc(lbl) for lbl in _labels_for(_reviewed_ids)]
        except Exception:
            reviewed_product_labels = []

        checklist_rows, checklist_mismatches = _checklist_rows(checklist, {
            "batch": bool(top_jobs_data),
            "ctrlm": bool(top_jobs_data),
            "res": bool(resource_kpis),
            "res15": bool(resource_kpis),
            "data": bool(sow_metrics),
            "sow": bool(sow_metrics),
            "perf": bool(batch_perf),
            "ui": bool(bench_rows_data),
        })

        ctx = dict(
            customer=customer, env=env, gen_date=gen_date,
            reviewed_product_labels=reviewed_product_labels,
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
            n_jobs_shown=n_jobs_shown,
            iss_rows=_iss_rows(issues),
            checklist_rows=checklist_rows,
            checklist_mismatches=checklist_mismatches,
            daily_limit=DAILY_LIMIT_HRS,
            capture_days=pe_config.RESOURCE_CAPTURE_DAYS,
            cpu_ok_t=cpu_ok_t, cpu_warn_t=cpu_warn_t,
            mem_ok_t=mem_ok_t, mem_warn_t=mem_warn_t,
            disk_ok_t=disk_ok_t, disk_warn_t=disk_warn_t,
            role_cpu_label=role_cpu_label, db_mem_label=db_mem_label,
            sow_rows=_sow_rows(sow_metrics), n_sow=n_sow,
            sow_status=sow_status, sow_summary=sow_summary,
            sow_badge_color=sow_badge[0], sow_badge_text=sow_badge[1],
            sow_disclaimer=_esc(sow_disclaimer),
            sow_under_t=_g(pe_config.SOW_UNDER_PCT), sow_over_t=_g(pe_config.SOW_OVER_PCT),
            sow_over_crit_t=_g(pe_config.SOW_OVER_CRIT_PCT),
            bench_rows=_bench_rows(bench_rows_data), n_bench=len(bench_rows_data),
            n_bench_total=n_bench_total, bench_summary=bench_summary,
            bench_badge_color=bench_badge[0], bench_badge_text=bench_badge[1],
            has_batch_perf=has_batch_perf, n_batch_perf_regr=n_batch_perf_regr,
            n_batch_perf_total=int(batch_perf.get("total_jobs", 0)),
            batch_perf_rows=_batch_perf_rows(batch_perf) if has_batch_perf else "",
            pe_override=pe_override,
        )

        html = templates.get_template("report_export.html").render(**ctx)
        # A registry row is a customer record, so an unnamed download must not
        # create a misleading "Unknown Customer" historical entry.  It remains
        # downloadable; supplying the customer name and exporting again records
        # the complete snapshot under the real customer.
        archive_status = "skipped"
        if raw_customer:
            try:
                archive_result = report_archive.save(raw_customer, html, {
                    "generated_at": datetime.now().astimezone().isoformat(),
                    "env": raw_env,
                    "pe_approved": pe_approved,
                    "cust_approved": cust_approved,
                    "pe_name": raw_pe_name,
                    "cust_name": raw_cust_name,
                    "checklist_mismatches": checklist_mismatches,
                    "sla_breach_count": n_breach,
                    "sla_at_risk_count": n_at_risk,
                    "sla_total_jobs": n_jobs,
                    # Archive only the values already calculated above and rendered
                    # in this exact HTML export.  The registry never recomputes a
                    # dashboard verdict from an old session after the fact.
                    "batch_metrics_captured": bool(batch_kpis),
                    "batch_compliance_pct": comp_pct,
                    "batch_total_jobs": n_jobs,
                    "batch_total_runs": total_runs,
                    "batch_total_hrs": total_hrs,
                    "batch_breach_count": n_breach,
                    "batch_at_risk_count": n_at_risk,
                    "batch_ok_count": n_ok_jobs,
                    "resource_metrics_captured": bool(resource_kpis),
                    "resource_fleet_grade": fleet_grade,
                    "resource_fleet_score": fleet_score,
                    "resource_total_servers": n_srv,
                    "resource_critical_count": n_crit,
                    "resource_warning_count": n_warn_s,
                    "sow_metrics_captured": bool(sow_metrics),
                    "sow_status": sow_status,
                    "sow_metrics_count": n_sow,
                    "benchmark_metrics_captured": bool(bench_rows_data) or has_batch_perf,
                    "benchmark_total_transactions": n_bench_total,
                    "benchmark_sla_breach_count": n_bench_breach,
                    "benchmark_degraded_count": n_bench_degraded,
                    "batch_perf_regression_count": n_batch_perf_regr,
                    "batch_perf_total_jobs": int(batch_perf.get("total_jobs", 0)),
                    "issues_count": len(issues),
                })
                archive_status = "saved" if archive_result.get("ok") else "failed"
            except Exception:
                # The archive is supplementary; it must never block the download.
                archive_status = "failed"
        filename = f"PE_Audit_{customer.replace(' ','_')}_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
        return HTMLResponse(
            content=html,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "text/html; charset=utf-8",
                "X-Archive-Status": archive_status,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc
