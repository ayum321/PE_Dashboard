"""Baseline persistence — SQLite-WAL store for spike + baseline history.

See ADR-001 in services/spike_schema.py for the SQLite-vs-Redis decision and
the rollback signal. This module is the only writer/reader of the store. Keys
are customer-namespaced (customer_id, vm_id, metric) so 250+ customers stay
isolated in one file. Rows mirror the canonical spike-record contract.

Build order (each independently testable): schema DDL → prune-on-write →
baseline snapshot writer → cold-start gate. Single-worker uvicorn only.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from services import pe_config
from services.spike_schema import make_spike_record

logger = logging.getLogger("pe_dashboard.baseline_store")

_DB_PATH = Path(__file__).resolve().parent.parent / ".pe_baseline.db"
_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# Guaranteed spike fields persisted (mirror of spike_schema canonical contract).
_SPIKE_COLS = ("start", "end", "peak", "peak_time", "duration_min", "severity",
               "reason_code", "severity_reason", "confidence", "detection",
               "z_score", "mean", "std", "threshold", "peak_pct")


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS spikes (
        customer_id TEXT NOT NULL, vm_id TEXT NOT NULL, metric TEXT NOT NULL,
        pull_ts TEXT NOT NULL,
        start TEXT, end TEXT, peak REAL, peak_time TEXT, duration_min INTEGER,
        severity TEXT, reason_code TEXT, severity_reason TEXT, confidence TEXT,
        detection TEXT, z_score REAL, mean REAL, std REAL, threshold REAL, peak_pct REAL
    );
    CREATE INDEX IF NOT EXISTS ix_spikes_key ON spikes(customer_id, vm_id, metric, pull_ts);
    CREATE TABLE IF NOT EXISTS baseline_snapshots (
        customer_id TEXT NOT NULL, vm_id TEXT NOT NULL, metric TEXT NOT NULL,
        pull_ts TEXT NOT NULL, mean REAL, std REAL, n INTEGER,
        PRIMARY KEY (customer_id, vm_id, metric, pull_ts)
    );
    """)
    conn.commit()


def _prune(conn: sqlite3.Connection, cutoff_iso: str) -> None:
    conn.execute("DELETE FROM spikes WHERE pull_ts < ?", (cutoff_iso,))
    conn.execute("DELETE FROM baseline_snapshots WHERE pull_ts < ?", (cutoff_iso,))


def record_pull(customer_id: str, vm_id: str, metric: str, spikes: list[dict],
                mean: float, std: float, n: int, pull_ts: str | None = None) -> None:
    """Persist one pull: raw spikes + a μ/σ baseline snapshot. Prunes on write to
    the BASELINE_RETENTION_DAYS rolling window so growth is bounded per ADR-001."""
    ts = pull_ts or datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=pe_config.BASELINE_RETENTION_DAYS)).isoformat()
    with _lock:
        conn = _connect()
        for sp in spikes:
            conn.execute(
                f"INSERT INTO spikes (customer_id, vm_id, metric, pull_ts, {','.join(_SPIKE_COLS)}) "
                f"VALUES (?,?,?,?,{','.join(['?'] * len(_SPIKE_COLS))})",
                (customer_id, vm_id, metric, ts, *[sp.get(c) for c in _SPIKE_COLS]))
        conn.execute(
            "INSERT OR REPLACE INTO baseline_snapshots VALUES (?,?,?,?,?,?,?)",
            (customer_id, vm_id, metric, ts, mean, std, n))
        _prune(conn, cutoff)
        conn.commit()


def baseline_confidence(customer_id: str, vm_id: str, metric: str) -> dict:
    """Cold-start gate: how many stored pulls back this VM:metric. degraded=True
    until MIN_BASELINE_PULLS, so the VM card can show 'Baseline: N / 90 days'.
    Includes pooled mean/std so the card can explain z-score sensitivity
    (μ=11.2% σ=1.8% → a 40% spike is 16σ here vs 2σ on a busier server)."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT mean, std, n FROM baseline_snapshots WHERE customer_id=? AND vm_id=? AND metric=?",
            (customer_id, vm_id, metric)).fetchall()
    n = len(rows)
    mean = std = None
    if n:
        tot = sum(r[2] for r in rows) or 1
        mean = round(sum(r[0] * r[2] for r in rows) / tot, 1)
        std = round(sum(r[1] * r[2] for r in rows) / tot, 1)
    return {"pulls": n, "retention_days": pe_config.BASELINE_RETENTION_DAYS,
            "min_pulls": pe_config.MIN_BASELINE_PULLS, "degraded": n < pe_config.MIN_BASELINE_PULLS,
            "baseline_mean": mean, "baseline_std": std}


def historical_baseline(customer_id: str, vm_id: str, metric: str) -> Optional[dict]:
    """Pooled μ/σ across stored snapshots; None when below cold-start gate (caller
    falls back to session-only baseline). Surfaces drift over the retention window."""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT mean, std, n FROM baseline_snapshots WHERE customer_id=? AND vm_id=? AND metric=?",
            (customer_id, vm_id, metric)).fetchall()
    if len(rows) < pe_config.MIN_BASELINE_PULLS:
        return None
    tot = sum(r[2] for r in rows) or 1
    mean = sum(r[0] * r[2] for r in rows) / tot
    std = sum(r[1] * r[2] for r in rows) / tot
    return {"mean": mean, "std": std, "pulls": len(rows), "n": tot}
