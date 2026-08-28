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
import json
import math
import re
from datetime import datetime, timedelta
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
            f'<div class="dim micro">{sub}</div>')


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
                f'<div class="dim micro">{sub}</div>')
    return _metric_cell(mem, MEM_OK, MEM_WARN, f"warn {_g(MEM_OK)}/{_g(MEM_WARN)}")


# ── Disk evidence resolution ──────────────────────────────────────────────
# Nine of eleven hosts previously showed a bare "N/A" disk cell because the
# point-in-time server snapshot carries no disk figure for them.  The captured
# deep-dive window does carry an Azure disk counter for those same hosts, so the
# report resolves the cell from that already-computed statistic instead of
# printing a silent blank.  When neither source emitted the counter the cell now
# says so rather than implying the value is simply missing.
_DISK_METRICS = (
    ("OS Disk Bandwidth Consumed Percentage", "OS disk bandwidth"),
    ("Data Disk Bandwidth Consumed Percentage", "Data disk bandwidth"),
)


def _host_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _host_anchor(value: Any) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in _host_key(value))
    return f"host-{slug.strip('-') or 'unknown'}"


def _deep_dive_vms(resource: dict) -> Dict[str, dict]:
    deep_dive = resource.get("deep_dive") if isinstance(resource.get("deep_dive"), dict) else {}
    vms = deep_dive.get("vms") if isinstance(deep_dive.get("vms"), dict) else {}
    return {_host_key(name): detail for name, detail in vms.items() if isinstance(detail, dict)}


def _series_disk_peak(detail: dict) -> tuple[float | None, str]:
    """Highest already-computed disk peak across the captured disk counters."""
    stats = detail.get("stats") if isinstance(detail.get("stats"), dict) else {}
    best: float | None = None
    label = ""
    for metric, short in _DISK_METRICS:
        entry = stats.get(metric)
        if not isinstance(entry, dict):
            continue
        value = _number_or_none(entry.get("max"))
        if value is None:
            continue
        if best is None or value > best:
            best, label = value, short
    return best, label


def _disk_cell(disk: float, detail: dict | None) -> str:
    """Disk cell with an explicit provenance line — never a silent blank."""
    if disk > 0:
        return _metric_cell(disk, DISK_OK, DISK_WARN, f"warn {_g(DISK_OK)}/{_g(DISK_WARN)}")
    resolved, label = _series_disk_peak(detail or {})
    if resolved is None:
        return ('<span class="tag tag-gray">NOT EMITTED</span>'
                '<div class="dim micro">No disk % counter in the snapshot or the captured window</div>')
    cls = "tag-red" if resolved >= DISK_WARN else ("tag-amber" if resolved >= DISK_OK else "tag-green")
    return (f'<span class="tag {cls}">{resolved:.1f}%</span>'
            f'<div class="dim micro">{_esc(label)} peak &middot; warn {_g(DISK_OK)}/{_g(DISK_WARN)}</div>')


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


def _srv_rows(servers: List[dict], vms_by_host: Dict[str, dict] | None = None) -> str:
    if not servers:
        return ("<tr><td colspan='6' class='empty'>No server data captured "
                "for this engagement.</td></tr>")
    vms_by_host = vms_by_host or {}
    rows = []
    for s in servers:
        # Prefer effective_cpu (aggregation-trap aware) — the value the live
        # grader actually scored — falling back to raw cpu when absent.
        _eff = s.get("effective_cpu")
        cpu  = _f(_eff if _eff is not None else (s.get("cpu_pct", 0) or s.get("cpu_used", 0)))
        mem  = _f(s.get("mem_pct",  0) or s.get("mem_used",  0))
        disk = _f(s.get("disk_pct", 0) or s.get("disk_used_max", 0))
        ram  = _f(s.get("mem_gb", 0) or s.get("mem_total_gb", 0))
        host_raw = str(s.get("host") or s.get("server") or "?")
        host = _esc(host_raw)
        detail = vms_by_host.get(_host_key(host_raw))
        stype = (s.get("type") or "APP").upper()
        stype_esc = _esc(stype)
        img_only = s.get("image_only", False)
        if img_only or (cpu == 0 and mem == 0 and disk == 0 and not detail):
            status = '<span class="tag tag-gray">IMAGE ONLY</span>'
            cpu_td = mem_td = dsk_td = '<span class="dim">—</span>'
        else:
            status = _health_badge(s, cpu, mem, disk, stype)
            cpu_td = _cpu_cell(cpu, stype)
            mem_td = _mem_cell(mem, stype, s.get("mem_status"))
            dsk_td = _disk_cell(disk, detail)
        sub = host if not ram else f"{host} &middot; {ram:.0f} GB RAM"
        jump = (f'<a class="jump" href="#mx-{_host_anchor(host_raw)}">trend &rsaquo;</a>'
                if detail else "")
        rows.append(f"""<tr id="{_host_anchor(host_raw)}">
          <td class="host-cell"><b>{host.split(".")[0]}</b> {jump}<br><span class="dim">{sub}</span></td>
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


# ── Job relevance, cadence and significance ─────────────────────────────────
# The report is a *product* performance story.  Ctrl-M carries a large tail of
# housekeeping that is real work for the scheduler but noise for this audience:
# file watchers, backup/restore, log archival, stats gathers, and the
# batch-window sentinel jobs that only stamp a timestamp.
#
# `pe_config.STRONG_UTILITY_TOKENS` is the project's canonical "this is not real
# batch work" list and is reused verbatim so there is one source of truth.
# `pe_config.RUNTIME_GATED_UTILITY` is deliberately NOT reused wholesale: its
# keys are runtime-gated (a match only excludes below a threshold) and it
# contains broad stems like ``export_`` that legitimately match product export
# jobs.  The families below are the name-only ones that are never product work
# regardless of how long they ran — a 1.4 h database backup is still a backup.
_NON_PRODUCT_TOKENS: tuple[str, ...] = (
    "file_watcher", "filewatcher", "filewatch", "watcher",
    "backup", "bkup", "_bkp", "restore",
    "archive_log", "log_archive", "logrotate",
    "purge_", "truncate_", "housekeep", "cleanup", "clean_up",
    "heartbeat", "health_check", "healthcheck", "ping_job", "zabbix",
    "gather_db_stats", "update_stats", "rebuild_index", "db_stats",
    "batch_start", "batch_end", "batchstart", "batchend",
    "pre_batch_node", "post_batch_node",
    "disable_login", "enable_login", "disable_users", "enable_users",
    "disable_monitors", "enable_monitors", "disable_triggers", "enable_triggers",
    "dummy", "sentinel",
)

# Human-facing reason shown in the disclosure note, keyed by the matched stem.
_NON_PRODUCT_FAMILY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("File watcher / listener", ("file_watcher", "filewatcher", "filewatch", "watcher")),
    ("Backup / restore", ("backup", "bkup", "_bkp", "restore")),
    ("Log & data housekeeping", ("archive_log", "log_archive", "logrotate", "purge_",
                                 "truncate_", "housekeep", "cleanup", "clean_up")),
    ("Monitoring / heartbeat", ("heartbeat", "health_check", "healthcheck", "ping_job", "zabbix")),
    ("Database maintenance", ("gather_db_stats", "update_stats", "rebuild_index", "db_stats")),
    ("Batch-window sentinel", ("batch_start", "batch_end", "batchstart", "batchend",
                               "pre_batch_node", "post_batch_node", "disable_login",
                               "enable_login", "disable_users", "enable_users",
                               "disable_monitors", "enable_monitors", "disable_triggers",
                               "enable_triggers", "dummy", "sentinel")),
)

_CADENCE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DAILY", ("daily", "dly", "_day", "everyday", "nightly")),
    ("WEEKLY", ("weekly", "wkly", "_week", "_wk")),
    ("MONTHLY", ("monthly", "mthly", "_month", "_mth")),
    ("QUARTERLY", ("quarterly", "qtrly", "_qtr", "quarter")),
)
_CADENCE_SUFFIX: dict[str, str] = {"d": "DAILY", "w": "WEEKLY", "m": "MONTHLY", "q": "QUARTERLY"}
_CADENCE_KEYS: tuple[str, ...] = ("DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "OTHER")


def _job_name_of(row: dict) -> str:
    return str(row.get("Job_Name") or row.get("job_name") or "").strip()


def _norm_job(name: str) -> str:
    """Lowercase, punctuation-collapsed form used by every name-token test."""
    return "_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + "_"


def _job_cadence(row: dict) -> str:
    """Resolve a job's schedule nature.

    `schedule_type` is authoritative when the calculator resolved one.  It very
    often has not — which is why the first cut of this table filed 12 of 20 real
    jobs under "Other", including obvious ``..._WKLY_2`` / ``DB_Backup_M`` names.
    The name is the fallback signal, read after stripping the trailing instance
    number (``JDA_PROCESSING_JOB_WKLY_2`` → ``..._wkly``).
    """
    explicit = str(row.get("schedule_type") or "").strip().upper()
    if explicit in _CADENCE_KEYS and explicit != "OTHER":
        return explicit
    name = _job_name_of(row)
    if not name:
        return "OTHER"
    norm = _norm_job(re.sub(r"[_\-]\d+$", "", name))
    for cadence, tokens in _CADENCE_WORDS:
        if any(token in norm for token in tokens):
            return cadence
    tail = norm.strip("_").rsplit("_", 1)[-1]
    if len(tail) == 1 and tail in _CADENCE_SUFFIX:
        return _CADENCE_SUFFIX[tail]
    return "OTHER"


def _job_exclusion_reason(row: dict) -> str:
    """Family name if this job is scheduler housekeeping, else "" for product work."""
    name = _job_name_of(row)
    if not name:
        return ""
    norm = _norm_job(name)
    for token in getattr(pe_config, "STRONG_UTILITY_TOKENS", ()) or ():
        if str(token).strip().lower() in norm:
            for family, stems in _NON_PRODUCT_FAMILY:
                if any(stem in norm for stem in stems):
                    return family
            return "Utility / sentinel"
    for family, stems in _NON_PRODUCT_FAMILY:
        if any(stem in norm for stem in stems):
            return family
    return ""


def _split_jobs(top_jobs: List[dict]) -> tuple[List[dict], List[dict]]:
    """Partition into (product jobs, set-aside housekeeping jobs).

    Nothing is silently dropped — the excluded set is rendered as a disclosure
    note under the table so the reader can audit the filter.
    """
    product: List[dict] = []
    setaside: List[dict] = []
    for row in top_jobs:
        if not isinstance(row, dict):
            continue
        reason = _job_exclusion_reason(row)
        if reason:
            setaside.append({**row, "_excluded_reason": reason})
        else:
            product.append(row)
    return product, setaside


def _job_attention(row: dict, status_key: str) -> str:
    """Why this job is worth a reader's time, or "" when it is simply healthy.

    Deliberately narrow: a breach/at-risk verdict, or a peak that runs far
    ahead of the job's own average (an unstable job hides inside a green
    buffer until the day its peak lands on a busy window).
    """
    if status_key in ("BREACH", "AT_RISK"):
        return "SLA pressure"
    if status_key == "SLA_MISSING":
        return "No ceiling resolved"
    try:
        peak = float(row.get("peak_hrs") or 0.0)
        avg = float(row.get("avg_hrs") or 0.0)
    except (TypeError, ValueError):
        return ""
    if avg > 0.05 and peak >= avg * 2.5 and peak - avg >= 0.25:
        return "Volatile runtime"
    if status_key == "LONG_JOB":
        return "Long runner"
    return ""


def _top_rows(top_jobs: List[dict]) -> str:
    if not top_jobs:
        return "<tr><td colspan='7' class='dim' style='text-align:center;padding:20px'>No batch data</td></tr>"

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
    schedule_labels = {"DAILY": "Daily", "WEEKLY": "Weekly", "MONTHLY": "Monthly",
                       "QUARTERLY": "Quarterly"}
    cadence_classes = {"DAILY": "tag-green", "WEEKLY": "tag-blue",
                       "MONTHLY": "tag-purple", "QUARTERLY": "tag-cyan"}
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
    for r in _split_jobs(top_jobs)[0][:20]:
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
        cadence_key = _job_cadence(r)
        schedule = schedule_labels.get(cadence_key)
        schedule_tag = (f' <span class="tag {cadence_classes.get(cadence_key, "tag-gray")} tag-sm">{schedule}</span>'
                        if schedule else "")
        sla_cell = (f'{sla:.2f}h' if not missing_sla else '<span class="tag tag-gray">N/A</span>')
        buffer_cell = f'{buf:.1f}%' if buf is not None else '—'
        attention = _job_attention(r, status_key)
        attention_cell = (f'<span class="tag tag-amber tag-sm">{_esc(attention)}</span>'
                          if attention else '<span class="dim micro">—</span>')
        # Buffer is the number a reader acts on; give it the same colour language
        # as the status tag instead of leaving it as undifferentiated body text.
        buffer_colour = {"BREACH": "var(--red)", "AT_RISK": "var(--red)",
                         "LONG_JOB": "var(--amber)"}.get(status_key, "var(--green)")
        rows.append(f"""<tr data-cadence="{cadence_key}" data-attention="{'1' if attention else '0'}">
          <td><b>{name}</b>{schedule_tag}</td>
          <td {peak_style}>{peak:.3f}h</td>
          <td class="dim">{avg:.3f}h</td>
          <td class="dim">{sla_cell} <span class="tag tag-gray tag-sm">{source}</span></td>
          <td style="color:{buffer_colour};font-weight:700;">{buffer_cell}</td>
          <td>{status}</td>
          <td>{attention_cell}</td>
        </tr>""")
    if not rows:
        return ("<tr><td colspan='7' class='dim' style='text-align:center;padding:20px'>"
                "Every job in this window resolved to scheduler housekeeping — see the "
                "set-aside note below.</td></tr>")
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


def _sow_ceiling_notice(metrics: List[dict]) -> str:
    """Warn when two differently-united SOW metrics carry an identical ceiling.

    The report never invents a per-metric commitment. It states the collision so
    the reader checks the contract instead of quoting a ceiling that was very
    likely reused from another line item.
    """
    buckets: dict[float, list[str]] = {}
    for metric in metrics:
        sow_v, _actual, _pct, _status = _sow_resolve(metric)
        if sow_v > 0:
            label = str(metric.get("label") or metric.get("key") or "?")
            buckets.setdefault(round(sow_v, 4), []).append(label)
    collisions = [(value, names) for value, names in buckets.items() if len(names) > 1]
    if not collisions:
        return ""
    parts = [f"{_esc(' and '.join(names))} both carry {value:,.0f}" for value, names in sorted(collisions)]
    return ("&#9888; Commitment provenance — " + "; ".join(parts)
            + ". Distinct units sharing one contracted ceiling usually means a single SOW figure was"
              " reused; confirm the per-metric commitment in the contract before quoting it.")


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


def _latest_registry_metadata(report: dict[str, Any]) -> dict[str, Any]:
    """Copy the frozen export payload into the legacy latest-report index.

    Review Registry remains a convenience view with one current report per
    customer.  Its values must be forwarded from ``audit_report_payload``—it
    must never reconstruct SLA, severity, fleet-grade, or SOW conclusions from
    an old browser session.
    """
    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    batch = report.get("batch_sla") if isinstance(report.get("batch_sla"), dict) else {}
    summary = batch.get("buffer_summary") if isinstance(batch.get("buffer_summary"), dict) else {}
    resource = report.get("resource_review") if isinstance(report.get("resource_review"), dict) else {}
    fleet = resource.get("fleet_summary") if isinstance(resource.get("fleet_summary"), dict) else {}
    sow = report.get("sow_capacity") if isinstance(report.get("sow_capacity"), dict) else {}
    benchmark = report.get("benchmark") if isinstance(report.get("benchmark"), dict) else {}
    sign_off = str(meta.get("sign_off_status") or "draft").strip().lower()

    return {
        "generated_at": meta.get("generated_at"),
        "env": "",
        "pe_approved": sign_off in {"reviewed", "customer_approved"},
        "cust_approved": sign_off == "customer_approved",
        "pe_name": "",
        "cust_name": "",
        "checklist_mismatches": 0,
        # All fields below are read from the same frozen payload rendered into
        # the downloaded HTML.  Missing values stay missing; ``save`` handles
        # the captured flags without fabricating display numbers.
        "sla_breach_count": summary.get("jobs_breach", summary.get("breach_count")),
        "sla_at_risk_count": summary.get("jobs_at_risk", summary.get("at_risk_count")),
        "sla_total_jobs": summary.get("total_jobs"),
        "batch_metrics_captured": bool(summary),
        "batch_compliance_pct": summary.get("compliance_pct", summary.get("window_compliance_pct")),
        "batch_total_jobs": summary.get("total_jobs"),
        "batch_total_runs": summary.get("total_runs"),
        "batch_total_hrs": summary.get("total_hrs"),
        "batch_breach_count": summary.get("jobs_breach", summary.get("breach_count")),
        "batch_at_risk_count": summary.get("jobs_at_risk", summary.get("at_risk_count")),
        "batch_ok_count": summary.get("jobs_ok", summary.get("ok_count")),
        "resource_metrics_captured": bool(fleet),
        "resource_fleet_grade": fleet.get("grade"),
        "resource_fleet_score": fleet.get("score"),
        "resource_total_servers": fleet.get("servers_total"),
        "resource_critical_count": fleet.get("critical"),
        "resource_warning_count": fleet.get("warning"),
        "sow_metrics_captured": bool(sow.get("metrics")),
        "sow_status": sow.get("reported_status"),
        "sow_metrics_count": len(sow.get("metrics") or []),
        "benchmark_metrics_captured": bool(benchmark.get("loaded")),
        "benchmark_total_transactions": benchmark.get("total_transactions"),
        "benchmark_sla_breach_count": benchmark.get("sla_breach_count"),
        "benchmark_degraded_count": benchmark.get("degraded_count"),
        "batch_perf_regression_count": benchmark.get("batch_perf_regression_count"),
        "batch_perf_total_jobs": benchmark.get("batch_perf_total_jobs"),
        "issues_count": meta.get("issues_logged_count"),
    }


# ── Baseline-locked report additions ───────────────────────────────────────
# These helpers deliberately consume the dashboard payload as it already exists.
# They do not calculate an alternative SLA, fleet-grade, or SOW verdict.  The
# original table helpers above remain the sole renderer for the legacy tables.
def _number_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


# ── Fleet verdict reconciliation ───────────────────────────────────────────
# The fleet grade, the severity distribution bar, the executive verdict, and the
# priority action plan previously read three different sources, so the report
# could print "Grade ? · 0.0/100" directly above a bar showing 11/11 hosts
# healthy.  Everything now resolves through ``_reconcile_fleet`` so those panels
# can only ever tell one story.
_SERVER_STATUS_ALIASES = {
    "critical": "critical", "crit": "critical", "severe": "critical",
    "warning": "warning", "warn": "warning", "at_risk": "warning",
    "healthy": "healthy", "ok": "healthy", "normal": "healthy", "good": "healthy",
}
_REAL_GRADES = {"A", "B", "C", "D", "F"}


def _server_status_key(server: dict) -> str:
    raw = str(server.get("status") or server.get("health") or "").strip().lower().replace(" ", "_")
    return _SERVER_STATUS_ALIASES.get(raw, "")


def _server_metrics(server: dict) -> tuple[float, float, float]:
    effective = server.get("effective_cpu")
    cpu = _f(effective if effective is not None else (server.get("cpu_pct", 0) or server.get("cpu_used", 0)))
    mem = _f(server.get("mem_pct", 0) or server.get("mem_used", 0))
    disk = _f(server.get("disk_pct", 0) or server.get("disk_used_max", 0))
    return cpu, mem, disk


def _server_is_image_only(server: dict) -> bool:
    if server.get("image_only"):
        return True
    return not any(value > 0 for value in _server_metrics(server))


def _derive_fleet_health(rows: List[dict]) -> Dict[str, Any]:
    """Re-run the dashboard's own fleet grader over the rendered host rows.

    Used only when the supplied resource KPIs carry no usable grade.  It calls
    ``resource_calculator.calculate_fleet_health`` — the same function the live
    Resource Review tab uses — so the report cannot introduce a second grading
    formula of its own.
    """
    try:
        from services.resource_calculator import calculate_fleet_health
    except Exception:
        return {"grade": "N/A", "score": 0.0, "source": ""}
    mapped = []
    for server in rows:
        cpu, mem, disk = _server_metrics(server)
        mapped.append({
            "cpu_used": cpu, "mem_used": mem, "disk_used_max": disk,
            "type": str(server.get("type") or "APP").upper(),
            "host": server.get("host") or server.get("server") or "",
        })
    try:
        fleet = calculate_fleet_health(mapped)
    except Exception:
        return {"grade": "N/A", "score": 0.0, "source": ""}
    grade = str(fleet.get("grade") or "N/A").strip().upper()
    if grade not in _REAL_GRADES:
        return {"grade": "N/A", "score": 0.0, "source": "",
                "quality": str(fleet.get("data_quality") or "")}
    return {
        "grade": grade,
        "score": _f(fleet.get("fleet_score", 0)),
        "source": "Re-derived in this report from the same host rows and role thresholds the distribution bar counts",
        "quality": str(fleet.get("data_quality") or ""),
    }


def _reconcile_fleet(resource_kpis: dict, servers: List[dict]) -> Dict[str, Any]:
    """One fleet verdict for the KPI gauge, the bar, the verdict and the plan."""
    rows = [server for server in servers if isinstance(server, dict)]
    counts = {"critical": 0, "warning": 0, "healthy": 0, "unknown": 0}
    for server in rows:
        if _server_is_image_only(server):
            counts["unknown"] += 1
            continue
        counts[_server_status_key(server) or "unknown"] += 1

    kpi_grade = str(resource_kpis.get("fleet_grade") or "").strip().upper()
    kpi_score = _f(resource_kpis.get("fleet_score", 0))
    grade, score = kpi_grade, kpi_score
    source = "Dashboard fleet grader (supplied with the resource evidence)"
    if grade not in _REAL_GRADES or score <= 0:
        derived = _derive_fleet_health(rows)
        grade, score = derived["grade"], derived["score"]
        source = derived["source"] or "Fleet grade could not be resolved from the loaded evidence"

    if rows:
        total = len(rows)
        critical, warning, healthy, unknown = (
            counts["critical"], counts["warning"], counts["healthy"], counts["unknown"])
    else:
        total = int(_f(resource_kpis.get("total_servers", 0)))
        critical = int(_f(resource_kpis.get("n_critical", 0)))
        warning = int(_f(resource_kpis.get("n_warning", 0)))
        healthy = max(0, total - critical - warning)
        unknown = 0

    disagreement = ""
    if rows and (kpi_grade or kpi_score):
        kpi_crit = int(_f(resource_kpis.get("n_critical", critical)))
        kpi_warn = int(_f(resource_kpis.get("n_warning", warning)))
        if (kpi_crit, kpi_warn) != (critical, warning):
            disagreement = (f"Supplied KPI counts ({kpi_crit} critical / {kpi_warn} warning) differ from the "
                            f"{total} host rows rendered below ({critical} critical / {warning} warning); "
                            "the rows are shown because they are the evidence a reader can check.")

    resolved = grade in _REAL_GRADES
    return {
        "grade": grade if resolved else "N/A",
        "score": score if resolved else 0.0,
        "resolved": resolved,
        "total": total, "critical": critical, "warning": warning,
        "healthy": healthy, "unknown": unknown,
        "graded": max(0, total - unknown),
        "source": source,
        "disagreement": disagreement,
    }


# ── Metrics Explorer ───────────────────────────────────────────────────────
# Renders the captured deep-dive window per host: one unified chart across every
# metric, the precomputed statistics, the precomputed signal-pattern shape, the
# detected spikes, and the Ctrl-M jobs that overlap those spikes.  Every number
# comes from ``resource.deep_dive``; nothing is recalculated here.
_EXPLORER_METRICS = (
    ("Percentage CPU", "CPU", "var(--cyan)", "cpu"),
    ("Available Memory Percentage", "Memory available", "var(--purple)", "memory"),
    ("OS Disk Bandwidth Consumed Percentage", "OS disk", "var(--green)", "os_disk"),
    ("Data Disk Bandwidth Consumed Percentage", "Data disk", "var(--blue)", "data_disk"),
)
_RANGE_CHOICES = (7, 15, 30)
# The chart is ~960 px wide, so ~200 plotted points is already sub-5px per
# sample.  Anything denser only inflates the self-contained file.
_MAX_PLOT_POINTS = 200
_MAX_STORED_POINTS = 120


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _series_points(detail: dict, metric: str) -> list[tuple[datetime, float]]:
    series = detail.get("series") if isinstance(detail.get("series"), dict) else {}
    raw = next((points for name, points in series.items()
                if str(name).casefold() == metric.casefold()), None)
    if not isinstance(raw, list):
        return []
    points: list[tuple[datetime, float]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        stamp, value = _parse_ts(entry.get("t")), _number_or_none(entry.get("v"))
        if stamp is not None and value is not None:
            points.append((stamp, value))
    points.sort(key=lambda item: item[0])
    return points


def _downsample(points: list[tuple[datetime, float]], limit: int = _MAX_PLOT_POINTS) -> tuple[list[tuple[datetime, float]], bool]:
    """Bucket the series to a drawable width, keeping each bucket's maximum."""
    if len(points) <= limit:
        return points, False
    bucket = len(points) / limit
    reduced: list[tuple[datetime, float]] = []
    for index in range(limit):
        chunk = points[int(index * bucket):max(int((index + 1) * bucket), int(index * bucket) + 1)]
        if chunk:
            reduced.append(max(chunk, key=lambda item: item[1]))
    return reduced, True


def _explorer_chart(series: dict[str, list[tuple[datetime, float]]], cpu_ok: float, cpu_warn: float) -> str:
    """Unified 0–100 % chart across every captured metric for one host."""
    populated = {metric: points for metric, points in series.items() if len(points) >= 2}
    if not populated:
        return "<div class='empty'>No plottable time-series in this window.</div>"
    starts = [points[0][0] for points in populated.values()]
    ends = [points[-1][0] for points in populated.values()]
    t0, t1 = min(starts), max(ends)
    span = (t1 - t0).total_seconds() or 1.0
    width, height = 960, 218
    # The right gutter is reserved for the CPU threshold labels so they can
    # never sit on top of the plotted line.
    left, right, top, bottom = 44, 88, 16, 34
    plot_w, plot_h = width - left - right, height - top - bottom

    def _x(stamp: datetime) -> float:
        return left + (stamp - t0).total_seconds() / span * plot_w

    def _y(value: float) -> float:
        return top + (100 - max(0.0, min(100.0, value))) * plot_h / 100

    parts = [f"<svg class='mx-svg' viewBox='0 0 {width} {height}' role='img' "
             f"aria-label='Unified CPU, memory and disk utilisation time series'>"]
    for level in (0, 25, 50, 75, 100):
        y = _y(level)
        parts.append(f"<line x1='{left}' x2='{width - right}' y1='{y:.1f}' y2='{y:.1f}' "
                     f"stroke='var(--grid)' stroke-width='1'/>")
        parts.append(f"<text x='{left - 7}' y='{y + 4:.1f}' text-anchor='end' fill='var(--ink-3)' "
                     f"font-size='11'>{level}</text>")
    # Threshold labels live in the reserved right gutter.
    line_end = width - right
    for level, label, colour in ((cpu_ok, "CPU warn", "var(--amber)"), (cpu_warn, "CPU crit", "var(--red)")):
        if 0 < level <= 100:
            y = _y(level)
            parts.append(f"<line x1='{left}' x2='{line_end}' y1='{y:.1f}' y2='{y:.1f}' "
                         f"stroke='{colour}' stroke-width='1' stroke-dasharray='7 5' opacity='.8'/>")
            parts.append(f"<text x='{line_end + 6}' y='{y + 4:.1f}' fill='{colour}' "
                         f"font-size='11' font-weight='600'>{label} {_g(level)}</text>")
    ticks = 5
    for index in range(ticks + 1):
        stamp = t0 + (t1 - t0) * index / ticks
        x = _x(stamp)
        anchor = "start" if index == 0 else ("end" if index == ticks else "middle")
        parts.append(f"<text x='{x:.1f}' y='{height - 12}' text-anchor='{anchor}' fill='var(--ink-3)' "
                     f"font-size='11'>{stamp.strftime('%d %b')}</text>")
    sampled = False
    for metric, label, colour, _key in _EXPLORER_METRICS:
        points = populated.get(metric)
        if not points:
            continue
        drawn, was_sampled = _downsample(points)
        sampled = sampled or was_sampled
        coords = " ".join(f"{_x(stamp):.0f},{_y(value):.1f}" for stamp, value in drawn)
        parts.append(f"<polyline points='{coords}' fill='none' stroke='{colour}' stroke-width='1.5' "
                     f"stroke-linejoin='round' opacity='.95'><title>{_esc(label)}</title></polyline>")
    parts.append("</svg>")
    legend = "".join(
        f"<span class='mx-key'><i style='background:{colour}'></i>{_esc(label)}</span>"
        for metric, label, colour, _key in _EXPLORER_METRICS if populated.get(metric))
    note = (" &middot; sampled to the chart width, each point is its bucket maximum"
            if sampled else "")
    return ("".join(parts)
            + f"<div class='mx-legend'>{legend}<span class='dim micro'>"
              f"{t0.strftime('%d %b %H:%M')} → {t1.strftime('%d %b %H:%M')} UTC{note}</span></div>")


def _explorer_stats(detail: dict, series: dict[str, list[tuple[datetime, float]]]) -> str:
    """Precomputed avg / p95 / peak per metric — read, never recalculated."""
    stats = detail.get("stats") if isinstance(detail.get("stats"), dict) else {}
    cells = []
    for metric, label, colour, _key in _EXPLORER_METRICS:
        entry = stats.get(metric)
        if not isinstance(entry, dict):
            if not series.get(metric):
                continue
            entry = {}
        mean = _number_or_none(entry.get("mean"))
        p95 = _number_or_none(entry.get("p95"))
        peak = _number_or_none(entry.get("max"))
        if mean is None and p95 is None and peak is None:
            continue
        def _fmt(value: float | None) -> str:
            return f"{value:.1f}%" if value is not None else "—"
        cells.append(
            f"<div class='mx-stat'><div class='mx-stat__k'><i style='background:{colour}'></i>{_esc(label)}</div>"
            f"<div class='mx-stat__v tabnum'>{_fmt(peak)}</div>"
            f"<div class='dim micro'>avg {_fmt(mean)} &middot; p95 {_fmt(p95)}</div></div>")
    if not cells:
        return "<div class='dim'>No summary statistics were captured for this host.</div>"
    return f"<div class='mx-stats'>{''.join(cells)}</div>"


_RISK_TAG = {"critical": "tag-red", "high": "tag-red", "medium": "tag-amber",
             "low": "tag-blue", "none": "tag-green"}


def _explorer_patterns(detail: dict) -> str:
    waveforms = detail.get("waveforms") if isinstance(detail.get("waveforms"), dict) else {}
    chips = []
    for metric, label, _colour, _key in _EXPLORER_METRICS:
        shape = waveforms.get(metric)
        if not isinstance(shape, dict):
            continue
        risk = str(shape.get("risk") or "low").lower()
        chips.append(f"<span class='tag {_RISK_TAG.get(risk, 'tag-gray')}'>"
                     f"{_esc(label)}: {_esc(shape.get('label') or 'unclassified')}</span>")
    if not chips:
        return ""
    return ("<div class='mx-row'><span class='mx-row__k'>Signal pattern</span>"
            f"<span class='mx-chips'>{''.join(chips)}</span></div>")


def _explorer_spikes(detail: dict) -> tuple[str, int]:
    spikes = detail.get("spikes") if isinstance(detail.get("spikes"), dict) else {}
    entries = [(metric, spike) for metric, values in spikes.items()
               if isinstance(values, list) for spike in values if isinstance(spike, dict)]
    graded = [(metric, spike) for metric, spike in entries
              if str(spike.get("severity") or "").lower() in {"critical", "critical_sustained", "warning"}]
    if not graded:
        return ("<div class='mx-row'><span class='mx-row__k'>Spikes</span>"
                "<span class='tag tag-green'>None detected in this window</span></div>", 0)
    order = {"critical_sustained": 0, "critical": 1, "warning": 2}
    graded.sort(key=lambda item: (order.get(str(item[1].get("severity") or "").lower(), 9),
                                  -(_number_or_none(item[1].get("peak")) or 0.0)))
    chips = []
    for metric, spike in graded[:4]:
        severity = str(spike.get("severity") or "").lower()
        cls = "tag-red" if severity.startswith("critical") else "tag-amber"
        short = metric.replace("Percentage ", "").replace(" Consumed Percentage", "").replace(" Percentage", "")
        peak = _number_or_none(spike.get("peak"))
        when = _parse_ts(spike.get("peak_time"))
        duration = _number_or_none(spike.get("duration_min"))
        bits = [f"{_esc(short)} {severity.replace('_', ' ').upper()}"]
        if peak is not None:
            bits.append(f"peak {peak:.1f}%")
        if duration:
            bits.append(f"{duration:.0f} min")
        if when is not None:
            bits.append(when.strftime("%d %b %H:%M"))
        chips.append(f"<span class='tag {cls}'>{' &middot; '.join(bits)}</span>")
    more = f"<span class='dim micro'>+{len(graded) - 4} more</span>" if len(graded) > 4 else ""
    return ("<div class='mx-row'><span class='mx-row__k'>Spikes</span>"
            f"<span class='mx-chips'>{''.join(chips)}{more}</span></div>", len(graded))


def _explorer_overlap(matches: list[dict]) -> str:
    """Ctrl-M jobs running while the host spiked — time coincidence only."""
    if not matches:
        return ("<div class='mx-row'><span class='mx-row__k'>Ctrl-M overlap</span>"
                "<span class='dim'>No Ctrl-M overlap was recorded for the loaded spike windows.</span></div>")
    match = matches[0]
    jobs = match.get("jobs")
    if not isinstance(jobs, list):
        # Older payloads put the job list on ``concurrent_jobs``; current ones
        # put the overlap COUNT there, which must never be printed as a name.
        legacy = match.get("concurrent_jobs")
        jobs = legacy if isinstance(legacy, list) else []
    labels: list[str] = []
    for job in jobs[:3]:
        if isinstance(job, dict):
            name = str(job.get("job") or job.get("name") or "").strip()
            hours = _number_or_none(job.get("hrs"))
            if name:
                labels.append(f"{name} ({hours:.2f}h)" if hours is not None else name)
        elif str(job).strip():
            labels.append(str(job).strip())
    if not labels and match.get("heaviest"):
        labels = [str(match["heaviest"])]
    overlap_count = match.get("concurrent_jobs")
    count_text = (f" {int(overlap_count)} job(s) overlapped the spike window."
                  if isinstance(overlap_count, (int, float)) else "")
    job_text = _esc(", ".join(labels)) if labels else "no named job"
    return ("<div class='mx-row'><span class='mx-row__k'>Ctrl-M overlap</span>"
            f"<span>Ctrl-M overlap: {job_text} — time overlap only, not proof of cause."
            f"{_esc(count_text)}</span></div>")


def _metrics_explorer(servers: List[dict], resource: dict) -> Dict[str, Any]:
    """Build the tabbed per-host Metrics Explorer from the captured deep dive."""
    deep_dive = resource.get("deep_dive") if isinstance(resource.get("deep_dive"), dict) else {}
    vms_by_host = _deep_dive_vms(resource)
    attribution = deep_dive.get("spike_attribution") if isinstance(deep_dive.get("spike_attribution"), dict) else {}
    attr_rows = attribution.get("rows") if isinstance(attribution.get("rows"), list) else []
    attrs_by_host: dict[str, list[dict]] = {}
    for row in attr_rows:
        if isinstance(row, dict):
            attrs_by_host.setdefault(_host_key(row.get("vm") or row.get("host")), []).append(row)

    role_order = {"DB": 0, "APP": 1, "SRE": 2}
    ordered = sorted(
        (server for server in servers if isinstance(server, dict)),
        key=lambda server: (role_order.get(str(server.get("type") or "APP").upper(), 9),
                            _host_key(server.get("host") or server.get("server"))),
    )
    tabs_by_role: dict[str, list[str]] = {}
    panels: list[str] = []
    dataset: dict[str, Any] = {}
    hosts_with_series = 0
    disk_missing = 0
    first_panel = True

    for server in ordered:
        host_raw = str(server.get("host") or server.get("server") or "?")
        host = _esc(host_raw)
        short = _esc(host_raw.split(".")[0])
        role = str(server.get("type") or "APP").upper()
        anchor = _host_anchor(host_raw)
        detail = vms_by_host.get(_host_key(host_raw))
        thresholds = _role_cpu_thr(role)
        series = {metric: _series_points(detail or {}, metric)
                  for metric, _label, _colour, _key in _EXPLORER_METRICS}
        has_series = any(len(points) >= 2 for points in series.values())
        if has_series:
            hosts_with_series += 1
        if _series_disk_peak(detail or {})[0] is None:
            _cpu, _mem, snapshot_disk = _server_metrics(server)
            if snapshot_disk <= 0:
                disk_missing += 1

        status_key = _server_status_key(server)
        status_tag = {"critical": "tag-red", "warning": "tag-amber", "healthy": "tag-green"}.get(status_key, "tag-gray")
        status_text = (status_key or "unknown").upper()
        tabs_by_role.setdefault(role, []).append(
            f"<button type='button' class='mx-tab{' is-on' if first_panel else ''}' "
            f"data-mx-target='mx-{anchor}' onclick=\"mxShow('mx-{anchor}',this)\">{short}"
            f"<i class='mx-dot mx-dot--{status_key or 'unknown'}'></i></button>")

        latest = max((points[-1][0] for points in series.values() if points), default=None)
        earliest = min((points[0][0] for points in series.values() if points), default=None)
        span_days = ((latest - earliest).total_seconds() / 86400.0) if (latest and earliest) else 0.0
        ranges = [days for days in _RANGE_CHOICES if days <= span_days + 0.5]
        if not ranges and span_days > 0:
            ranges = [max(1, round(span_days))]

        charts: list[str] = []
        range_buttons: list[str] = []
        if has_series and latest is not None:
            for index, days in enumerate(reversed(ranges) if ranges else []):
                cutoff = latest - timedelta(days=days)
                windowed = {metric: [point for point in points if point[0] >= cutoff]
                            for metric, points in series.items()}
                chart = _explorer_chart(windowed, thresholds["ok"], thresholds["warn"])
                charts.append(f"<div class='mx-chart' data-mx-range='{days}'"
                              f"{'' if index == 0 else ' hidden'}>{chart}</div>")
                range_buttons.append(
                    f"<button type='button' class='mx-range{' is-on' if index == 0 else ''}' "
                    f"onclick=\"mxRange('mx-{anchor}',{days},this)\">{days} d</button>")
            if span_days > 0 and max(ranges or [0]) < max(_RANGE_CHOICES):
                range_buttons.append(f"<span class='dim micro'>window captured: {span_days:.1f} d</span>")
        body = "".join(charts) if charts else (
            "<div class='empty'>No Azure time-series was captured for this host in this export.</div>")

        spike_html, spike_count = _explorer_spikes(detail or {})
        panels.append(
            f"<div class='mx-panel' id='mx-{anchor}'{'' if first_panel else ' hidden'}>"
            f"<div class='mx-head'><div><div class='mx-head__t'>{short}"
            f"<span class='tag tag-blue'>{_esc(role)}</span>"
            f"<span class='tag {status_tag}'>{_esc(status_text)}</span></div>"
            f"<div class='dim micro'>{host} &middot; CPU ceilings {_g(thresholds['ok'])}/{_g(thresholds['warn'])}"
            f" &middot; <a class='jump' href='#{anchor}'>&lsaquo; utilisation row</a></div></div>"
            f"<div class='mx-ranges'>{''.join(range_buttons)}</div></div>"
            f"{body}"
            f"{_explorer_stats(detail or {}, series)}"
            f"{_explorer_patterns(detail or {})}"
            f"{spike_html}"
            f"{_explorer_overlap(attrs_by_host.get(_host_key(host_raw), []))}"
            "</div>")
        first_panel = False

        dataset[host_raw] = {
            "role": role,
            "status": status_text,
            "captured_days": round(span_days, 2),
            "spikes_graded": spike_count,
            # Evenly spaced bucket maxima between ``start`` and ``end``: the same
            # reduction the chart draws, stored so the captured window can be
            # pulled back out of the archived report later.
            "series": {},
        }
        for metric, _label, _colour, key in _EXPLORER_METRICS:
            points = series[metric]
            if not points:
                continue
            stored, _was = _downsample(points, _MAX_STORED_POINTS)
            dataset[host_raw]["series"][key] = {
                "metric": metric,
                "unit": "percent",
                "start": stored[0][0].isoformat(),
                "end": stored[-1][0].isoformat(),
                "aggregation": "bucket_max",
                "values": [round(value, 1) for _stamp, value in stored],
            }

    if not ordered:
        return {"available": False, "tabs": "", "panels": "", "data_json": "{}",
                "hosts_with_series": 0, "disk_missing": 0, "summary": ""}

    tabs = "".join(
        f"<div class='mx-group'><span class='mx-group__k'>{_esc(role)}</span>{''.join(buttons)}</div>"
        for role, buttons in sorted(tabs_by_role.items(), key=lambda item: role_order.get(item[0], 9)))
    payload = json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                          "hosts": dataset}, separators=(",", ":"))
    return {
        "available": True,
        "tabs": tabs,
        "panels": "".join(panels),
        "data_json": payload.replace("</", "<\\/"),
        "hosts_with_series": hosts_with_series,
        "disk_missing": disk_missing,
        "summary": (f"{hosts_with_series} of {len(ordered)} host(s) carry a captured time-series window. "
                    "Ctrl-M overlap is time coincidence only; it is not proof of cause."),
    }


def _infra_coverage(servers: List[dict], resource: dict, mx: Dict[str, Any]) -> str:
    """Say plainly what the utilisation table covers and how it ties to Ctrl-M.

    The table used to render 11 rows with no statement of provenance, so a
    reader could not tell whether that was the whole estate or whatever Azure
    happened to answer for, nor which of those hosts actually carried batch
    work during the window.
    """
    total = len([s for s in servers if isinstance(s, dict)])
    if not total:
        return ""
    with_series = int(mx.get("hosts_with_series") or 0)
    deep_dive = resource.get("deep_dive") if isinstance(resource.get("deep_dive"), dict) else {}
    attribution = deep_dive.get("spike_attribution") if isinstance(deep_dive.get("spike_attribution"), dict) else {}
    attr_rows = attribution.get("rows") if isinstance(attribution.get("rows"), list) else []
    correlated = {_host_key(r.get("vm") or r.get("host")) for r in attr_rows if isinstance(r, dict)}
    correlated.discard("")
    by_role: dict[str, int] = {}
    image_only = 0
    for server in servers:
        if not isinstance(server, dict):
            continue
        by_role[str(server.get("type") or "APP").upper()] = by_role.get(
            str(server.get("type") or "APP").upper(), 0) + 1
        if _server_is_image_only(server):
            image_only += 1
    roles = " · ".join(f"{count} {role}" for role, count in sorted(by_role.items()))

    if with_series:
        series_txt = (f"<b>{with_series}</b> of them carry a captured Azure time-series window, "
                      f"charted per host in the Metrics Explorer below")
    else:
        series_txt = ("<b>none</b> carry a captured time-series window — run "
                      "<b>Resource Review → Load Metrics Deep Dive</b> before exporting to "
                      "chart these hosts")
    ctrlm_txt = (f"<b>{len(correlated)}</b> host(s) show Ctrl-M batch activity coincident with a "
                 f"detected resource spike" if correlated else
                 "no host shows a resource spike coincident with Ctrl-M batch activity in this window")
    extra = (f" {image_only} row(s) are image/metadata-only with no live counters." if image_only else "")
    return (f"<div class='setaside'><b>{total} host(s)</b> were picked up from Azure Monitor "
            f"({roles}). {series_txt}. Cross-referenced against Ctrl-M: {ctrlm_txt}."
            f"{extra} Overlap is time coincidence, not proof of cause.</div>")


def _batch_cadence_svg(top_jobs: List[dict]) -> str:
    """Exactly one cadence/runtime chart, sourced from the table's job rows."""
    usable = _split_jobs(top_jobs)[0][:12]
    if not usable:
        return "<div class='empty'>No product batch job evidence was supplied for a cadence profile.</div>"
    cadence_color = {"DAILY": "#10d96e", "WEEKLY": "#3b82f6", "MONTHLY": "#c084fc",
                     "QUARTERLY": "#22d3ee", "OTHER": "#6b7a9c"}
    maxima = max((_number_or_none(row.get("peak_hrs")) or 0.0 for row in usable), default=0.0) or 1.0
    height = 34 + len(usable) * 19
    pieces = [f"<svg viewBox='0 0 760 {height}' width='100%' height='{height}' role='img' aria-label='Job cadence and peak runtime profile'>"]
    pieces.append("<text x='8' y='14' fill='var(--ink-3)' font-size='11'>Job cadence and peak runtime (hours)</text>")
    for index, row in enumerate(usable):
        y = 28 + index * 19
        name = _esc(row.get("Job_Name") or row.get("job_name") or "?")[:34]
        cadence = _job_cadence(row)
        peak = _number_or_none(row.get("peak_hrs")) or 0.0
        color = cadence_color.get(cadence, "#6b7a9c")
        bar_width = max(2, peak / maxima * 360)
        pieces.append(f"<text x='8' y='{y + 11}' fill='var(--ink-2)' font-size='11'>{name}</text>")
        pieces.append(f"<rect x='260' y='{y}' width='360' height='12' rx='3' fill='var(--track)'/>")
        pieces.append(f"<rect x='260' y='{y}' width='{bar_width:.1f}' height='12' rx='3' fill='{color}' opacity='.88'/>")
        pieces.append(f"<text x='630' y='{y + 10}' fill='{color}' font-size='11' font-weight='700'>{_esc(cadence.title())} · {peak:.2f}h</text>")
    pieces.append("</svg>")
    return "".join(pieces)


def _batch_cadence_tabs(top_jobs: List[dict]) -> str:
    """Cadence filter strip for the job table — the same tab language as the
    Metrics Explorer below it, so a reader learns the interaction once.

    Server-rendered and additive only: every row still ships in the DOM, the
    buttons just toggle which ones are visible, and print forces all of them
    back on regardless of which tab was last clicked on-screen.
    """
    order = ["ALL", "ATTENTION", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "OTHER"]
    labels = {"ALL": "All jobs", "ATTENTION": "Needs attention", "DAILY": "Daily",
              "WEEKLY": "Weekly", "MONTHLY": "Monthly", "QUARTERLY": "Quarterly",
              "OTHER": "Unclassified"}
    counts = dict.fromkeys(order, 0)
    product = _split_jobs(top_jobs)[0][:20]
    for row in product:
        counts["ALL"] += 1
        counts[_job_cadence(row)] += 1
    if counts["ALL"] == 0:
        return ""
    buttons = []
    for key in order:
        if key not in ("ALL", "ATTENTION") and counts[key] == 0:
            continue
        if key == "ATTENTION":
            # Filled in by the client from data-attention so the count can never
            # drift from the badges the rows actually rendered.
            buttons.append(
                "<button type='button' class='mx-tab' data-job-cadence='ATTENTION' "
                "onclick=\"jobFilter('ATTENTION',this)\">Needs attention "
                "<b class='tabnum' id='job-attn-n'>0</b></button>")
            continue
        on = " is-on" if key == "ALL" else ""
        buttons.append(
            f"<button type='button' class='mx-tab{on}' data-job-cadence='{key}' "
            f"onclick=\"jobFilter('{key}',this)\">{labels[key]} <b class='tabnum'>{counts[key]}</b></button>"
        )
    return f"<div class='mx-group'><span class='mx-group__k'>View</span>{''.join(buttons)}</div>"


def _job_legend() -> str:
    """Written-down colour key.

    The table leans on colour for status, cadence and buffer.  Without this a
    reader has to reverse-engineer the bands from the numbers, and the amber
    used for LONG JOB and for AT RISK reads as one undifferentiated 'warning'.
    """
    status = [
        ("tag-green", "OK", f"buffer &gt; {_g(pe_config.SLA_LONGJOB_PCT)}%"),
        ("tag-amber", "LONG JOB", f"{_g(pe_config.SLA_ATRISK_PCT)}–{_g(pe_config.SLA_LONGJOB_PCT)}% buffer"),
        ("tag-amber", "AT RISK", f"0–{_g(pe_config.SLA_ATRISK_PCT)}% buffer"),
        ("tag-red", "BREACH", "runtime ≥ ceiling"),
        ("tag-gray", "SLA MISSING", "no ceiling resolved"),
    ]
    cadence = [("tag-green", "Daily"), ("tag-blue", "Weekly"),
               ("tag-purple", "Monthly"), ("tag-cyan", "Quarterly")]
    status_html = "".join(
        f"<span class='legend__i'><span class='tag {cls} tag-sm'>{text}</span>"
        f"<span class='dim'>{note}</span></span>" for cls, text, note in status)
    cadence_html = "".join(
        f"<span class='legend__i'><span class='tag {cls} tag-sm'>{text}</span></span>"
        for cls, text in cadence)
    return (f"<div class='legend'>"
            f"<div class='legend__g'><span class='legend__k'>Status</span>{status_html}</div>"
            f"<div class='legend__g'><span class='legend__k'>Cadence</span>{cadence_html}</div>"
            f"<div class='legend__g'><span class='legend__k'>Flag</span>"
            f"<span class='legend__i'><span class='tag tag-amber tag-sm'>Volatile runtime</span>"
            f"<span class='dim'>peak ≥ 2.5× its own average</span></span></div>"
            f"</div>")


def _setaside_note(setaside: List[dict]) -> str:
    """Disclosure for the housekeeping jobs kept out of the product table.

    Filtering without saying so would make the job count unreconcilable against
    Ctrl-M.  Naming every set-aside job and its family keeps the filter
    auditable by the customer.
    """
    if not setaside:
        return ""
    by_family: dict[str, list[str]] = {}
    for row in setaside:
        by_family.setdefault(str(row.get("_excluded_reason") or "Utility"), []).append(
            _job_name_of(row) or "?")
    parts = []
    for family in sorted(by_family):
        names = ", ".join(_esc(n) for n in sorted(set(by_family[family])))
        parts.append(f"<div><span class='setaside__l'>{_esc(family)}:</span> {names}</div>")
    return (f"<div class='setaside'><b>{len(setaside)} scheduler-housekeeping job(s) "
            f"set aside</b> so the table reads as product work only. They were measured "
            f"and remain in the Ctrl-M totals — they are excluded from this view, not "
            f"from the analysis.{''.join(parts)}</div>")


def _priority_actions(batch_kpis: dict, servers: List[dict], sow_metrics: List[dict],
                      sow_status: str, fleet: Dict[str, Any], coverage: Dict[str, int]) -> str:
    """Actions tied to rendered batch/resource/SOW facts, reading the SAME
    reconciled fleet verdict the gauge and the distribution bar display."""
    actions: list[tuple[int, str, str, str]] = []
    breach_count = int(_f(batch_kpis.get("jobs_breach", 0)))
    at_risk_count = int(_f(batch_kpis.get("jobs_at_risk", 0)))
    if breach_count:
        actions.append((1, "P1", "tag-red",
                        f"Resolve {breach_count} batch SLA breach(es) evidenced in the Batch Execution table."))
    if at_risk_count:
        actions.append((2, "P2", "tag-amber",
                        f"Protect {at_risk_count} job(s) rendered AT RISK — buffer is inside the "
                        f"{_g(pe_config.SLA_ATRISK_PCT)}% band."))
    if not fleet.get("resolved"):
        actions.append((2, "P2", "tag-amber",
                        "Fleet grade could not be resolved from the loaded evidence — the severity "
                        f"distribution is rendered from {fleet.get('graded', 0)} graded host row(s), "
                        "but no fleet score accompanied them. Re-run the Resource Review capture."))
    else:
        grade = str(fleet.get("grade"))
        score = _f(fleet.get("score"))
        if grade in {"D", "F"}:
            actions.append((1, "P1", "tag-red",
                            f"Remediate fleet health — reconciled grade {grade} ({score:.1f}/100)."))
        elif grade == "C":
            actions.append((2, "P2", "tag-amber",
                            f"Improve fleet health — reconciled grade C ({score:.1f}/100)."))
    if fleet.get("disagreement"):
        actions.append((2, "P2", "tag-amber",
                        f"Reconcile the resource evidence — {_esc(fleet['disagreement'])}"))
    for server in servers:
        status = _server_status_key(server)
        if status in {"critical", "warning"}:
            rank, priority, cls = (1, "P1", "tag-red") if status == "critical" else (2, "P2", "tag-amber")
            actions.append((rank, priority, cls,
                            f"Review {_esc(server.get('host') or server.get('server') or 'host')} — "
                            f"rendered resource health is {status.upper()}."))
    if sow_metrics and sow_status in {"LOW", "OVER", "CRITICAL_OVER"}:
        rank, priority, cls = (1, "P1", "tag-red") if sow_status == "CRITICAL_OVER" else (3, "P3", "tag-amber")
        actions.append((rank, priority, cls,
                        f"Review SOW volume status: {_esc(sow_status.replace('_', ' '))}."))
    disk_missing = int(coverage.get("disk_missing", 0))
    if int(coverage.get("n_srv", 0)) and not int(coverage.get("hosts_with_series", 0)):
        actions.append((2, "P2", "tag-amber",
                        "No Azure time-series was captured for any host, so the Metrics Explorer "
                        "has trends to show for none of them. Run Resource Review → Load Metrics "
                        "Deep Dive before exporting to archive the charted window with this report."))
    if disk_missing:
        actions.append((3, "P3", "tag-blue",
                        f"Close the disk telemetry gap — {disk_missing} host(s) emitted no disk % counter "
                        "in either the snapshot or the captured window, so their disk cell is graded on "
                        "no evidence."))
    if not actions:
        return "<span class='tag tag-green'>No priority action is generated from the rendered evidence.</span>"
    actions.sort(key=lambda item: item[0])
    return "".join(f"<div class='action'><span class='tag {cls}'>{priority}</span><span>{text}</span></div>"
                   for _rank, priority, cls, text in actions[:8])


def _locked_legacy_context(body: ExportRequest, report: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Build the original report context plus additive evidence.

    The original source fields feed the old table helpers unchanged.  ``report``
    supplies only its established audit identifier/window/source metadata.
    """
    batch, resource = body.batch or {}, body.resource or {}
    issues, approvals = body.issues or [], body.approvals or {}
    servers = body.servers or resource.get("servers") or []
    sow, benchmark = body.sow or {}, body.benchmark or {}
    batch_kpis, resource_kpis = batch.get("kpis") or {}, resource.get("kpis") or {}
    top_jobs_data = batch.get("top_jobs") or batch.get("top_breaches") or []
    sow_metrics, bench_rows_data = sow.get("metrics") or [], benchmark.get("rows") or []
    checklist, pe_info, cust_info = approvals.get("checklist", {}), approvals.get("pe", {}), approvals.get("customer", {})
    pe_approved, cust_approved = bool(pe_info.get("approved")), bool(cust_info.get("approved"))
    both_ok, pe_override = pe_approved and cust_approved, bool(pe_info.get("override_blockers", False))
    raw_customer = str(approvals.get("customer_name", "") or "").strip()
    raw_env = str(approvals.get("env_type", "") or "Not Detected")
    customer, env = _esc(raw_customer or "Customer not specified"), _esc(raw_env)
    comp_pct = _f(batch_kpis.get("compliance_pct", 0))
    comp_col = "#22c55e" if comp_pct >= 99 else ("#f59e0b" if comp_pct >= 85 else "#ef4444")
    n_breach, n_at_risk, n_ok_jobs = int(_f(batch_kpis.get("jobs_breach", 0))), int(_f(batch_kpis.get("jobs_at_risk", 0))), int(_f(batch_kpis.get("jobs_ok", 0)))
    n_jobs, total_hrs, total_runs = int(_f(batch_kpis.get("total_jobs", 0))), _f(batch_kpis.get("total_hrs", 0)), int(_f(batch_kpis.get("total_runs", 0)))
    fleet = _reconcile_fleet(resource_kpis, servers)
    fleet_grade, fleet_score = fleet["grade"], fleet["score"]
    grade_color = {"A": "#22c55e", "B": "#06b6d4", "C": "#f59e0b", "D": "#fb923c", "F": "#ef4444"}.get(fleet_grade, "#6b7a99")
    n_srv, n_crit, n_warn_s = fleet["total"], fleet["critical"], fleet["warning"]
    n_healthy, n_unknown = fleet["healthy"], fleet["unknown"]
    total_servers = max(1, n_srv)
    vms_by_host = _deep_dive_vms(resource)
    explorer = _metrics_explorer(servers, resource)
    cpu_ok_t, cpu_warn_t = _g(pe_config.CPU_WARN), _g(pe_config.CPU_CRIT)
    mem_ok_t, mem_warn_t = _g(pe_config.MEM_WARN), _g(pe_config.MEM_CRIT)
    disk_ok_t, disk_warn_t = _g(pe_config.DISK_WARN), _g(pe_config.DISK_CRIT)
    appt, dbt, sret = _role_cpu_thr("APP"), _role_cpu_thr("DB"), _role_cpu_thr("SRE")
    role_cpu_label = f"APP {_g(appt['ok'])}/{_g(appt['warn'])} · DB {_g(dbt['ok'])}/{_g(dbt['warn'])} · SRE {_g(sret['ok'])}/{_g(sret['warn'])}"
    db_mem_label = f"{_g(_DB_MEM_LO)}–{_g(_DB_MEM_HI)}%"
    resolved_sow_statuses = [_sow_resolve(metric)[3] for metric in sow_metrics]
    severity = {"CRITICAL_OVER": 4, "OVER": 3, "LOW": 2, "ACCEPTABLE": 1, "OPTIMAL": 0}
    sow_status = str(sow.get("overall_status") or "").upper()
    if resolved_sow_statuses:
        sow_status = max(resolved_sow_statuses, key=lambda value: severity.get(value, 0))
    sow_badge = {"OPTIMAL": ("#22c55e", "OPTIMAL"), "ACCEPTABLE": ("#22c55e", "ACCEPTABLE"), "LOW": ("#3b82f6", "UNDER-UTILISED"), "OVER": ("#f59e0b", "OVER CONTRACT"), "CRITICAL_OVER": ("#ef4444", "CRITICAL OVER")}.get(sow_status, ("#6b7a99", "NOT ASSESSED"))
    sow_disclaimer = {"LOW": f"ℹ Actual volume is below {_g(pe_config.SOW_UNDER_PCT)}% of the contracted SOW ceiling — under-utilised relative to contract.", "OVER": "⚠ Actual volume exceeds the contracted SOW process window — formal review is required.", "CRITICAL_OVER": "⚠ Actual volume is over the critical SOW ceiling — formal acknowledgement is required.", "ACCEPTABLE": "✓ Within the standard SOW process window.", "OPTIMAL": "✓ Within the preferred SOW process window."}.get(sow_status, "SOW volume comparison not yet performed for this engagement.")
    batch_perf = benchmark.get("batch_perf_summary") or {}
    has_batch_perf, n_batch_perf_regr = bool(batch_perf), int(_f(batch_perf.get("regressions", 0)))
    n_bench_total, n_bench_breach, n_bench_degraded = int(_f(benchmark.get("total_transactions", len(bench_rows_data)))), int(_f(benchmark.get("sla_breaches", 0))), int(_f(benchmark.get("degraded", 0)))
    bench_badge = ("#ef4444", f"{n_bench_breach} BREACH") if n_bench_breach else (("#f59e0b", f"{n_bench_degraded} DEGRADED") if n_bench_degraded else (("#22c55e", "WITHIN TOLERANCE") if n_bench_total else ("#6b7a99", "NOT ASSESSED")))
    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    audit_window = meta.get("audit_window") if isinstance(meta.get("audit_window"), dict) else {}
    source_badges = [str(source.get("name")) for source in (meta.get("sources") or []) if isinstance(source, dict) and source.get("loaded")]
    sign_status = str(meta.get("sign_off_status") or ("customer_approved" if both_ok else "draft")).replace("_", " ").title()
    # Preserve the existing engagement-scoped product chips in the locked
    # header. This is intentionally the same config-store path as the legacy
    # renderer, not a new report-side interpretation of product scope.
    reviewed_product_labels: List[str] = []
    try:
        from services import config_store as _cfg_store
        from services.product_taxonomy import labels_for as _labels_for
        reviewed_product_labels = [_esc(label) for label in _labels_for(_cfg_store.get("reviewed_products") or [])]
    except Exception:
        reviewed_product_labels = []
    evidence = {"batch": bool(top_jobs_data), "ctrlm": bool(top_jobs_data), "res": bool(resource_kpis), "res15": bool(resource_kpis), "data": bool(sow_metrics), "sow": bool(sow_metrics), "perf": bool(batch_perf), "ui": bool(bench_rows_data)}
    checklist_rows, checklist_mismatches = _checklist_rows(checklist, evidence)
    ctx = dict(
        customer=customer, env=env, gen_date=datetime.now().strftime("%d %b %Y, %I:%M %p"),
        reviewed_product_labels=reviewed_product_labels, sign_color="#22c55e" if both_ok else "#f59e0b", sign_label="✅ APPROVED" if both_ok else "⏳ PENDING", sign_state="approved" if both_ok else "pending", sign_text="APPROVED" if both_ok else "PENDING",
        pe_name=_esc(pe_info.get("name") or "—"), cust_name=_esc(cust_info.get("name") or "—"), pe_tick="✅" if pe_approved else "⏳", cu_tick="✅" if cust_approved else "⏳", pe_approved=pe_approved, cust_approved=cust_approved, pe_date=_esc(pe_info.get("date") or ""), cust_date=_esc(cust_info.get("date") or ""), notes=_esc(approvals.get("notes") or ""), pe_override=pe_override,
        comp_pct=comp_pct, comp_col=comp_col, comp_deg=max(0.0, min(100.0, comp_pct)) * 3.6, n_breach=n_breach, n_ok_jobs=n_ok_jobs, n_jobs=n_jobs, total_hrs=total_hrs, total_runs=total_runs,
        fleet_grade=fleet_grade, fleet_score=fleet_score, score_deg=max(0.0, min(100.0, fleet_score)) * 3.6, grade_color=grade_color, n_srv=n_srv, n_crit=n_crit, n_warn_s=n_warn_s, n_healthy=n_healthy, n_unknown=n_unknown, crit_pct_w=round(n_crit / total_servers * 100, 1), warn_pct_w=round(n_warn_s / total_servers * 100, 1), ok_pct_w=round(n_healthy / total_servers * 100, 1), unknown_pct_w=round(n_unknown / total_servers * 100, 1), n_issues=len(issues),
        fleet_resolved=fleet["resolved"], fleet_source=_esc(fleet["source"]), fleet_disagreement=_esc(fleet["disagreement"]), fleet_graded=fleet["graded"],
        srv_rows=_srv_rows(servers, vms_by_host), top_rows=_top_rows(top_jobs_data), n_jobs_shown=min(20, len(_split_jobs(top_jobs_data)[0])), n_jobs_product=len(_split_jobs(top_jobs_data)[0]), iss_rows=_iss_rows(issues), checklist_rows=checklist_rows, checklist_mismatches=checklist_mismatches,
        daily_limit=DAILY_LIMIT_HRS, capture_days=pe_config.RESOURCE_CAPTURE_DAYS, cpu_ok_t=cpu_ok_t, cpu_warn_t=cpu_warn_t, mem_ok_t=mem_ok_t, mem_warn_t=mem_warn_t, disk_ok_t=disk_ok_t, disk_warn_t=disk_warn_t, role_cpu_label=role_cpu_label, db_mem_label=db_mem_label,
        sow_rows=_sow_rows(sow_metrics), n_sow=len(sow_metrics), sow_status=sow_status, sow_summary=_esc(sow.get("summary") or ""), sow_badge_color=sow_badge[0], sow_badge_text=sow_badge[1], sow_disclaimer=_esc(sow_disclaimer), sow_ceiling_notice=_sow_ceiling_notice(sow_metrics), sow_under_t=_g(pe_config.SOW_UNDER_PCT), sow_over_t=_g(pe_config.SOW_OVER_PCT), sow_over_crit_t=_g(pe_config.SOW_OVER_CRIT_PCT),
        bench_rows=_bench_rows(bench_rows_data), n_bench=len(bench_rows_data), n_bench_total=n_bench_total, bench_summary=_esc(benchmark.get("summary") or ""), bench_badge_color=bench_badge[0], bench_badge_text=bench_badge[1], has_batch_perf=has_batch_perf, n_batch_perf_regr=n_batch_perf_regr, n_batch_perf_total=int(_f(batch_perf.get("total_jobs", 0))), batch_perf_rows=_batch_perf_rows(batch_perf) if has_batch_perf else "",
        audit_id=_esc(meta.get("audit_id") or ""), audit_window_start=_esc(audit_window.get("start") or "Not available"), audit_window_end=_esc(audit_window.get("end") or "Not available"), sign_off_status=_esc(sign_status), source_badges=[_esc(source) for source in source_badges],
        executive_verdict=_esc(f"Batch SLA {comp_pct:.1f}% ({n_breach} breach(es)); resource fleet Grade {fleet_grade}"
                               f"{f' ({fleet_score:.1f}/100)' if fleet['resolved'] else ' — not resolved from the loaded evidence'}"
                               f"; {n_crit} critical / {n_warn_s} warning of {n_srv} host(s); SOW {sow_badge[1]}."),
        priority_actions=_priority_actions(batch_kpis, servers, sow_metrics, sow_status, fleet,
                                           {"disk_missing": explorer["disk_missing"],
                                            "hosts_with_series": explorer["hosts_with_series"],
                                            "n_srv": n_srv}),
        mx_available=explorer["available"], mx_tabs=explorer["tabs"], mx_panels=explorer["panels"],
        mx_summary=_esc(explorer["summary"]), mx_data=explorer["data_json"], mx_hosts=explorer["hosts_with_series"],
        cadence_chart=_batch_cadence_svg(top_jobs_data),
        job_cadence_tabs=_batch_cadence_tabs(top_jobs_data),
        job_legend=_job_legend(),
        job_setaside=_setaside_note(_split_jobs(top_jobs_data)[1]),
        infra_coverage=_infra_coverage(servers, resource, explorer),
        methodology_buffer="buffer_pct = (SLA hours - runtime hours) / SLA hours * 100",
    )
    return ctx, raw_customer


# ── Endpoint ───────────────────────────────────────────────────
@router.post(
    "/export-report",
    response_class=HTMLResponse,
    summary="Render and download the standalone PE Audit HTML report",
)
async def export_report(request: Request, body: ExportRequest) -> HTMLResponse:
    try:
        # Freeze the established audit identity and archive payload first.  The
        # customer-facing HTML remains the locked legacy Grafana report; it is
        # deliberately not the abandoned replacement v2 layout.
        export_body = body.model_dump(exclude_none=True)
        report = build_audit_report_payload(export_body)
        customer = report["meta"]["customer"]
        audit_id = report["meta"]["audit_id"]
        if customer and customer != "Customer not specified":
            # Archival history enriches the immutable payload, but a damaged
            # archive location must never prevent a reviewer downloading the
            # current, already-rendered evidence report.
            try:
                attach_prior_audit(
                    report,
                    report_archive.get_previous_payload(customer, audit_id),
                )
            except Exception:
                attach_prior_audit(report, None)
        else:
            attach_prior_audit(report, None)
        archive_status = "skipped"
        if customer and customer != "Customer not specified":
            snapshot = report_archive.save_payload_snapshot(report)
            archive_status = "payload_saved" if snapshot.get("ok") else "failed"

        legacy_ctx, raw_customer = _locked_legacy_context(body, report)
        rendered_html = templates.get_template("report_export.html").render(**legacy_ctx)
        if archive_status == "payload_saved":
            attached = report_archive.attach_snapshot_html(customer, audit_id, rendered_html)
            if not attached.get("ok"):
                archive_status = "payload_saved_html_failed"
            else:
                latest = report_archive.save(customer, rendered_html, _latest_registry_metadata(report))
                archive_status = "saved" if latest.get("ok") else "failed"

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
