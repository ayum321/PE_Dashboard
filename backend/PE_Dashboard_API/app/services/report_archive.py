"""Local report archive — SQLite-WAL index + on-disk HTML files.

Mirrors services/baseline_store.py's connection pattern (WAL mode, single
module-level connection, threading.Lock around writes, single-worker uvicorn
only) so this follows an established, already-reviewed idiom rather than
introducing a second local-persistence convention.

Storage model — two intentionally separate stores:

* ``reports`` remains the latest-only Review Registry index used by legacy UI.
* ``report_payload_snapshots`` is immutable and versioned by customer + audit
  ID.  It stores the frozen JSON payload and rendered HTML required for export
  evidence and prior-audit comparisons.

Writes are atomic (temp file + os.replace) so a crash mid-write can never
leave a half-written report, payload, or archive index entry.

Nothing in this module re-derives compliance numbers (breach counts,
checklist mismatches, etc.) — callers must pass in values already computed
by the same code path that rendered the HTML. Re-deriving them here would
create a second, independently-maintained copy of grading logic that could
drift from what the report itself displays — exactly the class of bug this
archive exists to help catch, not reintroduce.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.state_paths import get_state_dir, get_state_file

logger = logging.getLogger("pe_dashboard.report_archive")


_ROOT: Path = get_state_dir()
_DB_PATH: Path = get_state_file(".pe_report_archive.db")
_FILES_DIR: Path = _ROOT / "data" / "report_archive"


def _db_path() -> Path:
    return _DB_PATH


def _files_dir() -> Path:
    return _FILES_DIR


def _snapshots_dir() -> Path:
    return _ROOT / "data" / "report_snapshots"


_lock: threading.Lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_META_COLS = (
    "generated_at", "env", "pe_approved", "cust_approved", "pe_name",
    "cust_name", "checklist_mismatches", "sla_breach_count",
    "sla_at_risk_count", "sla_total_jobs", "batch_metrics_captured",
    "batch_compliance_pct", "batch_total_jobs", "batch_total_runs",
    "batch_total_hrs", "batch_breach_count", "batch_at_risk_count",
    "batch_ok_count", "resource_metrics_captured", "resource_fleet_grade",
    "resource_fleet_score", "resource_total_servers",
    "resource_critical_count", "resource_warning_count",
    "sow_metrics_captured", "sow_status", "sow_metrics_count",
    "benchmark_metrics_captured", "benchmark_total_transactions",
    "benchmark_sla_breach_count", "benchmark_degraded_count",
    "batch_perf_regression_count", "batch_perf_total_jobs", "issues_count",
)

# Additive migration map for local SQLite files created before export snapshots
# were introduced.  These names and definitions are module constants, never
# customer-controlled SQL.
_SNAPSHOT_COLUMN_DEFINITIONS = (
    ("batch_metrics_captured", "INTEGER"),
    ("batch_compliance_pct", "REAL"),
    ("batch_total_jobs", "INTEGER"),
    ("batch_total_runs", "INTEGER"),
    ("batch_total_hrs", "REAL"),
    ("batch_breach_count", "INTEGER"),
    ("batch_at_risk_count", "INTEGER"),
    ("batch_ok_count", "INTEGER"),
    ("resource_metrics_captured", "INTEGER"),
    ("resource_fleet_grade", "TEXT"),
    ("resource_fleet_score", "REAL"),
    ("resource_total_servers", "INTEGER"),
    ("resource_critical_count", "INTEGER"),
    ("resource_warning_count", "INTEGER"),
    ("sow_metrics_captured", "INTEGER"),
    ("sow_status", "TEXT"),
    ("sow_metrics_count", "INTEGER"),
    ("benchmark_metrics_captured", "INTEGER"),
    ("benchmark_total_transactions", "INTEGER"),
    ("benchmark_sla_breach_count", "INTEGER"),
    ("benchmark_degraded_count", "INTEGER"),
    ("batch_perf_regression_count", "INTEGER"),
    ("batch_perf_total_jobs", "INTEGER"),
    ("issues_count", "INTEGER"),
)

_LIST_COLS = (
    "customer_slug", "customer", "generated_at", "env", "pe_approved",
    "cust_approved", "pe_name", "cust_name", "checklist_mismatches",
    "sla_breach_count", "sla_at_risk_count", "sla_total_jobs",
    *_META_COLS[10:], "file_size_bytes",
)


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    db_path = _db_path()
    try:
        _files_dir().mkdir(parents=True, exist_ok=True)
        _snapshots_dir().mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as exc:
        logger.warning("report_archive: could not open %s (%s) — falling back to :memory:", db_path, exc)
        _conn = sqlite3.connect(":memory:", check_same_thread=False)
    _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS reports (
        customer_slug         TEXT PRIMARY KEY,
        customer              TEXT NOT NULL,
        generated_at          TEXT NOT NULL,
        env                   TEXT,
        pe_approved           INTEGER,
        cust_approved         INTEGER,
        pe_name               TEXT,
        cust_name             TEXT,
        checklist_mismatches  INTEGER,
        sla_breach_count      INTEGER,
        sla_at_risk_count     INTEGER,
        sla_total_jobs        INTEGER,
        batch_metrics_captured INTEGER,
        batch_compliance_pct   REAL,
        batch_total_jobs       INTEGER,
        batch_total_runs       INTEGER,
        batch_total_hrs        REAL,
        batch_breach_count     INTEGER,
        batch_at_risk_count    INTEGER,
        batch_ok_count         INTEGER,
        resource_metrics_captured INTEGER,
        resource_fleet_grade   TEXT,
        resource_fleet_score   REAL,
        resource_total_servers INTEGER,
        resource_critical_count INTEGER,
        resource_warning_count INTEGER,
        sow_metrics_captured   INTEGER,
        sow_status             TEXT,
        sow_metrics_count      INTEGER,
        benchmark_metrics_captured INTEGER,
        benchmark_total_transactions INTEGER,
        benchmark_sla_breach_count INTEGER,
        benchmark_degraded_count INTEGER,
        batch_perf_regression_count INTEGER,
        batch_perf_total_jobs  INTEGER,
        issues_count           INTEGER,
        file_path             TEXT NOT NULL,
        file_hash             TEXT NOT NULL,
        file_size_bytes       INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_reports_generated_at ON reports(generated_at);

    CREATE TABLE IF NOT EXISTS report_payload_snapshots (
        customer_slug      TEXT NOT NULL,
        audit_id           TEXT NOT NULL,
        customer           TEXT NOT NULL,
        audit_window_start TEXT,
        audit_window_end   TEXT,
        generated_at       TEXT NOT NULL,
        payload_path       TEXT NOT NULL,
        html_path          TEXT,
        PRIMARY KEY (customer_slug, audit_id)
    );
    CREATE INDEX IF NOT EXISTS ix_report_payload_snapshots_customer_generated
        ON report_payload_snapshots(customer_slug, generated_at DESC);
    """)
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(reports)")}
    for name, definition in _SNAPSHOT_COLUMN_DEFINITIONS:
        if name not in existing_columns:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {name} {definition}")
    conn.commit()


def _slugify(customer: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (customer or "unknown").strip().lower()).strip("-")
    return slug or "unknown"


def _atomic_write_text(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    except Exception as exc:
        logger.warning("report_archive: failed to write text to %s — %s", path, exc)


def _snapshot_paths(customer_slug: str, audit_id: str) -> tuple[Path, Path]:
    safe_audit_id = _slugify(audit_id)
    folder = _snapshots_dir() / customer_slug
    return folder / f"{safe_audit_id}.json", folder / f"{safe_audit_id}.html"


def save_payload_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist an immutable, versioned report payload before HTML rendering.

    A snapshot is keyed by customer + audit ID.  The former latest-only archive
    remains for the Review Registry; it is not used as the source for report
    change history.  Duplicate audit IDs are rejected rather than overwritten.
    """
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    customer = str(meta.get("customer") or "").strip()
    audit_id = str(meta.get("audit_id") or "").strip()
    if not customer or customer.lower() == "customer not specified":
        return {"ok": False, "error": "A customer is required to archive an immutable audit payload."}
    if not audit_id:
        return {"ok": False, "error": "An audit ID is required to archive an immutable audit payload."}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"Report payload is not JSON serializable: {exc}"}

    slug = _slugify(customer)
    payload_path, _ = _snapshot_paths(slug, audit_id)
    audit_window = meta.get("audit_window") if isinstance(meta.get("audit_window"), dict) else {}
    row = (slug, audit_id, customer, audit_window.get("start"), audit_window.get("end"),
           str(meta.get("generated_at") or datetime.now(timezone.utc).isoformat()),
           str(payload_path.relative_to(_ROOT)))
    try:
        with _lock:
            conn = _connect()
            existing = conn.execute(
                "SELECT 1 FROM report_payload_snapshots WHERE customer_slug = ? AND audit_id = ?",
                (slug, audit_id),
            ).fetchone()
            if existing or payload_path.exists():
                return {"ok": False, "error": f"Audit ID {audit_id!r} already exists for {customer}; immutable snapshots cannot be overwritten."}
            _atomic_write_text(payload_path, encoded)
            try:
                conn.execute(
                    """INSERT INTO report_payload_snapshots
                       (customer_slug, audit_id, customer, audit_window_start, audit_window_end, generated_at, payload_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
                conn.commit()
            except Exception:
                payload_path.unlink(missing_ok=True)
                raise
        return {"ok": True, "customer_slug": slug, "audit_id": audit_id, "payload_path": str(payload_path)}
    except Exception as exc:
        logger.warning("report_archive.save_payload_snapshot failed for customer=%r audit=%r: %s", customer, audit_id, exc)
        return {"ok": False, "error": str(exc)}


def attach_snapshot_html(customer: str, audit_id: str, html: str) -> dict[str, Any]:
    """Attach a rendered report to an existing immutable JSON payload."""
    slug = _slugify(customer)
    _, html_path = _snapshot_paths(slug, audit_id)
    try:
        with _lock:
            conn = _connect()
            row = conn.execute(
                "SELECT 1 FROM report_payload_snapshots WHERE customer_slug = ? AND audit_id = ?",
                (slug, audit_id),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "Payload snapshot must be stored before attaching HTML."}
            _atomic_write_text(html_path, html)
            conn.execute(
                "UPDATE report_payload_snapshots SET html_path = ? WHERE customer_slug = ? AND audit_id = ?",
                (str(html_path.relative_to(_ROOT)), slug, audit_id),
            )
            conn.commit()
        return {"ok": True, "html_path": str(html_path)}
    except Exception as exc:
        logger.warning("report_archive.attach_snapshot_html failed for customer=%r audit=%r: %s", customer, audit_id, exc)
        return {"ok": False, "error": str(exc)}


def get_previous_payload(customer: str, audit_id: str) -> Optional[dict[str, Any]]:
    """Get the preceding immutable audit for this customer by generated time."""
    slug = _slugify(customer)
    with _lock:
        conn = _connect()
        row = conn.execute(
            """SELECT payload_path FROM report_payload_snapshots
               WHERE customer_slug = ? AND audit_id != ?
               ORDER BY generated_at DESC LIMIT 1""",
            (slug, audit_id),
        ).fetchone()
    if not row:
        return None
    path = _ROOT / row[0]
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("report_archive: cannot load prior payload at %s: %s", path, exc)
        return None


def download_filename(customer_slug: str) -> str:
    """Return a browser-safe archive filename derived only from the slug.

    Customer text can contain quotes or control characters, so it must never
    be interpolated into a Content-Disposition header.
    """
    return f"PE_Audit_{_slugify(customer_slug)}_archived.html"


def _finite_float(value: Any) -> Optional[float]:
    """Return a persisted finite value, never NaN/Infinity or a fake zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _snapshot_float(meta: dict[str, Any], captured_key: str, value_key: str) -> Optional[float]:
    return _finite_float(meta.get(value_key)) if bool(meta.get(captured_key)) else None


def _snapshot_count(meta: dict[str, Any], captured_key: str, value_key: str) -> Optional[int]:
    value = _snapshot_float(meta, captured_key, value_key)
    return max(0, int(value)) if value is not None else None


def _finite_count(value: Any) -> Optional[int]:
    number = _finite_float(value)
    return max(0, int(number)) if number is not None else None


def save(customer: str, html: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Write/replace the archived report for this customer. Never raises —
    archive failures must not break the live export the user is waiting on;
    callers should still wrap this in try/except and log, per defense in
    depth, but this function itself catches and returns an error dict rather
    than propagating.
    """
    try:
        slug = _slugify(customer)
        files_dir = _files_dir()
        files_dir.mkdir(parents=True, exist_ok=True)
        file_path = files_dir / f"{slug}.html"
        tmp_path  = files_dir / f".{slug}.html.tmp"

        html_bytes = html.encode("utf-8")
        file_hash  = hashlib.sha256(html_bytes).hexdigest()

        # Atomic write: full temp file, then rename over the target. On
        # POSIX, os.replace is atomic within the same filesystem, so a
        # process crash mid-write can never leave a truncated report at
        # file_path — worst case the .tmp file is orphaned, never the
        # customer-visible file.
        tmp_path.write_bytes(html_bytes)
        os.replace(tmp_path, file_path)

        row = {
            "customer_slug":        slug,
            "customer":             customer,
            "generated_at":         meta.get("generated_at") or datetime.now(timezone.utc).isoformat(),
            "env":                  meta.get("env", ""),
            "pe_approved":          int(bool(meta.get("pe_approved"))),
            "cust_approved":        int(bool(meta.get("cust_approved"))),
            "pe_name":              meta.get("pe_name", ""),
            "cust_name":            meta.get("cust_name", ""),
            "checklist_mismatches": int(meta.get("checklist_mismatches") or 0),
            "sla_breach_count":     int(meta.get("sla_breach_count") or 0),
            "sla_at_risk_count":    int(meta.get("sla_at_risk_count") or 0),
            "sla_total_jobs":       int(meta.get("sla_total_jobs") or 0),
            "file_path":            str(file_path.relative_to(_ROOT)),
            "file_hash":            file_hash,
            "file_size_bytes":      len(html_bytes),
        }
        batch_captured = bool(meta.get("batch_metrics_captured"))
        resource_captured = bool(meta.get("resource_metrics_captured"))
        sow_captured = bool(meta.get("sow_metrics_captured"))
        benchmark_captured = bool(meta.get("benchmark_metrics_captured"))
        row.update({
            "batch_metrics_captured": int(batch_captured),
            "batch_compliance_pct": _snapshot_float(meta, "batch_metrics_captured", "batch_compliance_pct"),
            "batch_total_jobs": _snapshot_count(meta, "batch_metrics_captured", "batch_total_jobs"),
            "batch_total_runs": _snapshot_count(meta, "batch_metrics_captured", "batch_total_runs"),
            "batch_total_hrs": _snapshot_float(meta, "batch_metrics_captured", "batch_total_hrs"),
            "batch_breach_count": _snapshot_count(meta, "batch_metrics_captured", "batch_breach_count"),
            "batch_at_risk_count": _snapshot_count(meta, "batch_metrics_captured", "batch_at_risk_count"),
            "batch_ok_count": _snapshot_count(meta, "batch_metrics_captured", "batch_ok_count"),
            "resource_metrics_captured": int(resource_captured),
            "resource_fleet_grade": str(meta.get("resource_fleet_grade") or "") if resource_captured else None,
            "resource_fleet_score": _snapshot_float(meta, "resource_metrics_captured", "resource_fleet_score"),
            "resource_total_servers": _snapshot_count(meta, "resource_metrics_captured", "resource_total_servers"),
            "resource_critical_count": _snapshot_count(meta, "resource_metrics_captured", "resource_critical_count"),
            "resource_warning_count": _snapshot_count(meta, "resource_metrics_captured", "resource_warning_count"),
            "sow_metrics_captured": int(sow_captured),
            "sow_status": str(meta.get("sow_status") or "") if sow_captured else None,
            "sow_metrics_count": _snapshot_count(meta, "sow_metrics_captured", "sow_metrics_count"),
            "benchmark_metrics_captured": int(benchmark_captured),
            "benchmark_total_transactions": _snapshot_count(meta, "benchmark_metrics_captured", "benchmark_total_transactions"),
            "benchmark_sla_breach_count": _snapshot_count(meta, "benchmark_metrics_captured", "benchmark_sla_breach_count"),
            "benchmark_degraded_count": _snapshot_count(meta, "benchmark_metrics_captured", "benchmark_degraded_count"),
            "batch_perf_regression_count": _snapshot_count(meta, "benchmark_metrics_captured", "batch_perf_regression_count"),
            "batch_perf_total_jobs": _snapshot_count(meta, "benchmark_metrics_captured", "batch_perf_total_jobs"),
            "issues_count": _finite_count(meta.get("issues_count")),
        })

        with _lock:
            conn = _connect()
            conn.execute("""
                INSERT INTO reports (customer_slug, customer, generated_at, env,
                    pe_approved, cust_approved, pe_name, cust_name,
                    checklist_mismatches, sla_breach_count, sla_at_risk_count,
                    sla_total_jobs, batch_metrics_captured, batch_compliance_pct,
                    batch_total_jobs, batch_total_runs, batch_total_hrs,
                    batch_breach_count, batch_at_risk_count, batch_ok_count,
                    resource_metrics_captured, resource_fleet_grade,
                    resource_fleet_score, resource_total_servers,
                    resource_critical_count, resource_warning_count,
                    sow_metrics_captured, sow_status, sow_metrics_count,
                    benchmark_metrics_captured, benchmark_total_transactions,
                    benchmark_sla_breach_count, benchmark_degraded_count,
                    batch_perf_regression_count, batch_perf_total_jobs, issues_count,
                    file_path, file_hash, file_size_bytes)
                VALUES (:customer_slug, :customer, :generated_at, :env,
                    :pe_approved, :cust_approved, :pe_name, :cust_name,
                    :checklist_mismatches, :sla_breach_count, :sla_at_risk_count,
                    :sla_total_jobs, :batch_metrics_captured, :batch_compliance_pct,
                    :batch_total_jobs, :batch_total_runs, :batch_total_hrs,
                    :batch_breach_count, :batch_at_risk_count, :batch_ok_count,
                    :resource_metrics_captured, :resource_fleet_grade,
                    :resource_fleet_score, :resource_total_servers,
                    :resource_critical_count, :resource_warning_count,
                    :sow_metrics_captured, :sow_status, :sow_metrics_count,
                    :benchmark_metrics_captured, :benchmark_total_transactions,
                    :benchmark_sla_breach_count, :benchmark_degraded_count,
                    :batch_perf_regression_count, :batch_perf_total_jobs, :issues_count,
                    :file_path, :file_hash, :file_size_bytes)
                ON CONFLICT(customer_slug) DO UPDATE SET
                    customer=excluded.customer, generated_at=excluded.generated_at,
                    env=excluded.env, pe_approved=excluded.pe_approved,
                    cust_approved=excluded.cust_approved, pe_name=excluded.pe_name,
                    cust_name=excluded.cust_name,
                    checklist_mismatches=excluded.checklist_mismatches,
                    sla_breach_count=excluded.sla_breach_count,
                    sla_at_risk_count=excluded.sla_at_risk_count,
                    sla_total_jobs=excluded.sla_total_jobs,
                    batch_metrics_captured=excluded.batch_metrics_captured,
                    batch_compliance_pct=excluded.batch_compliance_pct,
                    batch_total_jobs=excluded.batch_total_jobs,
                    batch_total_runs=excluded.batch_total_runs,
                    batch_total_hrs=excluded.batch_total_hrs,
                    batch_breach_count=excluded.batch_breach_count,
                    batch_at_risk_count=excluded.batch_at_risk_count,
                    batch_ok_count=excluded.batch_ok_count,
                    resource_metrics_captured=excluded.resource_metrics_captured,
                    resource_fleet_grade=excluded.resource_fleet_grade,
                    resource_fleet_score=excluded.resource_fleet_score,
                    resource_total_servers=excluded.resource_total_servers,
                    resource_critical_count=excluded.resource_critical_count,
                    resource_warning_count=excluded.resource_warning_count,
                    sow_metrics_captured=excluded.sow_metrics_captured,
                    sow_status=excluded.sow_status,
                    sow_metrics_count=excluded.sow_metrics_count,
                    benchmark_metrics_captured=excluded.benchmark_metrics_captured,
                    benchmark_total_transactions=excluded.benchmark_total_transactions,
                    benchmark_sla_breach_count=excluded.benchmark_sla_breach_count,
                    benchmark_degraded_count=excluded.benchmark_degraded_count,
                    batch_perf_regression_count=excluded.batch_perf_regression_count,
                    batch_perf_total_jobs=excluded.batch_perf_total_jobs,
                    issues_count=excluded.issues_count,
                    file_path=excluded.file_path, file_hash=excluded.file_hash,
                    file_size_bytes=excluded.file_size_bytes
            """, row)
            conn.commit()

        logger.info("report_archive: saved %s (%s, %d bytes)", slug, file_hash[:12], len(html_bytes))
        return {"ok": True, "customer_slug": slug}
    except Exception as exc:
        logger.warning("report_archive.save failed for customer=%r: %s", customer, exc)
        return {"ok": False, "error": str(exc)}


def list_reports() -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        cur = conn.execute(f"SELECT {', '.join(_LIST_COLS)} FROM reports ORDER BY generated_at DESC")
        return [dict(zip(_LIST_COLS, row)) for row in cur.fetchall()]


def get_report(slug: str) -> Optional[dict[str, Any]]:
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT * FROM reports WHERE customer_slug = ?", (slug,))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    if not row:
        return None
    record = dict(zip(cols, row))
    fpath = _ROOT / record["file_path"]
    if not fpath.exists():
        logger.warning("report_archive: DB row for %s exists but file missing at %s", slug, fpath)
        return None
    record["html"] = fpath.read_text(encoding="utf-8")
    return record
