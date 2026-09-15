"""Evidence Vault — Secure storage, cryptographic hashing, audit packaging,
and lifecycle management for review source documents.

Stores raw evidence files (BatchSLA_info.xlsx, Ctrl-M CSV, SOW PDF, Benchmark XLSX,
and Azure Monitor telemetry snapshots) alongside frozen audit reports and JSON payloads.
Generates evidence_manifest.json with SHA-256 integrity receipts and full ZIP packages
for post-go-live dispute defense and audit proof.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.state_paths import get_state_dir

logger = logging.getLogger("pe_dashboard.evidence_vault")

_ROOT: Path = get_state_dir()
_STAGING_DIR: Path = _ROOT / "data" / "evidence_staging"
_SNAPSHOTS_DIR: Path = _ROOT / "data" / "report_snapshots"
_PACKAGES_DIR: Path = _ROOT / "data" / "report_packages"

DOCUMENT_LABELS: dict[str, str] = {
    "batch_sla": "Batch SLA Matrix",
    "ctrlm_history": "Ctrl-M Execution History",
    "sow_contract": "Statement of Work (SOW)",
    "benchmark": "Benchmark / Performance Test Report",
    "azure_telemetry": "Azure Monitor Telemetry Snapshot",
    "waiver": "Governance Risk Waiver / Memo",
    "other": "Supporting Evidence Document",
}


def _staging_dir() -> Path:
    p = _ROOT / "data" / "evidence_staging"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snapshots_dir() -> Path:
    p = _ROOT / "data" / "report_snapshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _packages_dir() -> Path:
    p = _ROOT / "data" / "report_packages"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slugify(customer: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (customer or "unknown").strip().lower()).strip("-")
    return slug or "unknown"


def _sanitize_filename(name: str) -> str:
    base = Path(name or "evidence.bin").name
    clean = re.sub(r"[^a-zA-Z0-9._-]", "_", base).strip("._-")
    return clean or "evidence_file.dat"


def _get_db():
    """Import and return the shared report_archive database connection and lock."""
    from services import report_archive
    return report_archive._connect(), report_archive._lock


def compute_sha256(raw_bytes: bytes) -> str:
    """Compute cryptographic SHA-256 hex digest for byte payload."""
    return hashlib.sha256(raw_bytes).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Staging Operations (Active Session)
# ─────────────────────────────────────────────────────────────────────────────
def stage_document(
    customer: str,
    document_type: str,
    filename: str,
    raw_bytes: bytes,
    label: Optional[str] = None,
) -> dict[str, Any]:
    """Save an uploaded raw document to the customer's staging vault with SHA-256 hash."""
    if not raw_bytes:
        return {"ok": False, "error": "Cannot stage empty file payload"}

    slug = _slugify(customer)
    safe_name = _sanitize_filename(filename)
    doc_label = label or DOCUMENT_LABELS.get(document_type, "Supporting Evidence Document")
    file_hash = compute_sha256(raw_bytes)
    file_size = len(raw_bytes)
    doc_id = f"doc_{_slugify(document_type)[:10]}_{file_hash[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    cust_staging = _staging_dir() / slug
    cust_staging.mkdir(parents=True, exist_ok=True)

    # Prefix with document type to prevent collision while preserving clean name
    staged_filename = f"{_slugify(document_type)}__{safe_name}"
    target_path = cust_staging / staged_filename

    try:
        # Atomic write
        tmp_path = cust_staging / f".{staged_filename}.tmp"
        tmp_path.write_bytes(raw_bytes)
        os.replace(tmp_path, target_path)
    except Exception as exc:
        logger.warning("evidence_vault: failed to write staged file %s — %s", target_path, exc)
        return {"ok": False, "error": f"Failed writing staged document: {exc}"}

    rel_path = str(target_path.relative_to(_ROOT))

    conn, lock = _get_db()
    with lock:
        try:
            # Replace existing staged document of the same type for this customer
            conn.execute(
                """DELETE FROM report_evidence_documents
                   WHERE customer_slug = ? AND document_type = ? AND is_frozen = 0""",
                (slug, document_type),
            )
            conn.execute(
                """INSERT OR REPLACE INTO report_evidence_documents
                   (doc_id, customer_slug, audit_id, document_type, document_label,
                    filename, file_size_bytes, file_hash, file_path, uploaded_at, is_frozen)
                   VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (doc_id, slug, document_type, doc_label, safe_name, file_size, file_hash, rel_path, now_iso),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("evidence_vault: failed to register staged doc in SQLite: %s", exc)
            return {"ok": False, "error": f"Database registration failed: {exc}"}

    logger.info("evidence_vault: staged %s for %s (%s, %d bytes)", document_type, slug, safe_name, file_size)
    return {
        "ok": True,
        "doc_id": doc_id,
        "customer_slug": slug,
        "document_type": document_type,
        "document_label": doc_label,
        "filename": safe_name,
        "file_size_bytes": file_size,
        "file_hash": file_hash,
        "uploaded_at": now_iso,
    }


def stage_telemetry_snapshot(
    customer: str,
    telemetry_data: dict[str, Any],
    label: str = "Azure Monitor Telemetry Snapshot",
) -> dict[str, Any]:
    """Serialize live Azure Monitor telemetry data to a clean JSON snapshot and stage it."""
    try:
        encoded = json.dumps(telemetry_data, indent=2, ensure_ascii=False, default=str).encode("utf-8")
        filename = f"azure_telemetry_{_slugify(customer)}.json"
        return stage_document(
            customer=customer,
            document_type="azure_telemetry",
            filename=filename,
            raw_bytes=encoded,
            label=label,
        )
    except Exception as exc:
        logger.warning("evidence_vault: failed to stage telemetry snapshot: %s", exc)
        return {"ok": False, "error": str(exc)}


def get_staged_documents(customer: str) -> list[dict[str, Any]]:
    """Return all currently staged evidence files awaiting audit export."""
    slug = _slugify(customer)
    conn, lock = _get_db()
    with lock:
        cur = conn.execute(
            """SELECT doc_id, customer_slug, document_type, document_label,
                      filename, file_size_bytes, file_hash, file_path, uploaded_at
               FROM report_evidence_documents
               WHERE customer_slug = ? AND is_frozen = 0
               ORDER BY uploaded_at ASC""",
            (slug,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Validate that files still exist on disk
    valid_rows = []
    for r in rows:
        p = _ROOT / r["file_path"]
        if p.exists():
            valid_rows.append(r)
    return valid_rows


# ─────────────────────────────────────────────────────────────────────────────
# Audit Freeze & Packaging (On Export)
# ─────────────────────────────────────────────────────────────────────────────
def freeze_evidence_to_audit(
    customer: str,
    audit_id: str,
    meta: Optional[dict[str, Any]] = None,
    html_content: Optional[str] = None,
    payload_content: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Commit all staged documents for customer to the permanent audit folder.
    Generates evidence_manifest.json and builds the complete audit package ZIP.
    """
    slug = _slugify(customer)
    safe_audit_id = _slugify(audit_id)
    meta = meta or {}

    audit_folder = _snapshots_dir() / slug / safe_audit_id
    evidence_folder = audit_folder / "source_documents"
    audit_folder.mkdir(parents=True, exist_ok=True)
    evidence_folder.mkdir(parents=True, exist_ok=True)

    # 1. Fetch staged documents
    staged = get_staged_documents(customer)
    frozen_docs: list[dict[str, Any]] = []

    conn, lock = _get_db()

    for doc in staged:
        src_path = _ROOT / doc["file_path"]
        if not src_path.exists():
            continue

        dest_filename = _sanitize_filename(doc["filename"])
        dest_path = evidence_folder / dest_filename
        # Avoid collision if multiple files had the same original name
        if dest_path.exists() and dest_path != src_path:
            dest_filename = f"{doc['document_type']}_{dest_filename}"
            dest_path = evidence_folder / dest_filename

        try:
            shutil.copy2(src_path, dest_path)
            rel_dest = str(dest_path.relative_to(_ROOT))
            frozen_doc = {**doc, "file_path": rel_dest, "audit_id": audit_id, "is_frozen": 1}
            frozen_docs.append(frozen_doc)

            with lock:
                conn.execute(
                    """UPDATE report_evidence_documents
                       SET audit_id = ?, file_path = ?, is_frozen = 1
                       WHERE doc_id = ?""",
                    (audit_id, rel_dest, doc["doc_id"]),
                )
        except Exception as exc:
            logger.warning("evidence_vault: error freezing file %s: %s", src_path, exc)

    with lock:
        conn.commit()

    # 2. Write HTML and Payload files into audit_folder if provided
    html_rel_path = None
    html_hash = None
    if html_content:
        html_dest = audit_folder / f"PE_Audit_{slug}.html"
        html_dest.write_text(html_content, encoding="utf-8")
        html_rel_path = str(html_dest.relative_to(_ROOT))
        html_hash = compute_sha256(html_content.encode("utf-8"))

    payload_rel_path = None
    payload_hash = None
    if payload_content:
        payload_dest = audit_folder / "audit_payload.json"
        encoded_payload = json.dumps(payload_content, indent=2, ensure_ascii=False, default=str)
        payload_dest.write_text(encoded_payload, encoding="utf-8")
        payload_rel_path = str(payload_dest.relative_to(_ROOT))
        payload_hash = compute_sha256(encoded_payload.encode("utf-8"))

    # 3. Generate evidence manifest
    manifest = {
        "customer": customer,
        "customer_slug": slug,
        "audit_id": audit_id,
        "generated_at": meta.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "environment": meta.get("env") or "Production",
        "review_team": {
            "pe_reviewer": meta.get("pe_name") or "Not recorded",
            "pe_approved": bool(meta.get("pe_approved")),
            "customer_sponsor": meta.get("cust_name") or "Not recorded",
            "customer_approved": bool(meta.get("cust_approved")),
        },
        "report_artifacts": {
            "html_report": {
                "filename": f"PE_Audit_{slug}.html",
                "sha256": html_hash,
                "path": html_rel_path,
            },
            "json_payload": {
                "filename": "audit_payload.json",
                "sha256": payload_hash,
                "path": payload_rel_path,
            },
        },
        "source_documents": [
            {
                "doc_id": d["doc_id"],
                "document_type": d["document_type"],
                "document_label": d.get("document_label") or DOCUMENT_LABELS.get(d["document_type"], "Supporting Document"),
                "filename": d["filename"],
                "file_size_bytes": d["file_size_bytes"],
                "sha256": d["file_hash"],
                "uploaded_at": d.get("uploaded_at"),
            }
            for d in frozen_docs
        ],
        "total_documents": len(frozen_docs),
        "manifest_version": "1.0",
        "verification_statement": (
            "Cryptographic proof receipt generated by PE Dashboard. "
            "Every source document was preserved at audit sign-off time."
        ),
    }

    manifest_path = audit_folder / "evidence_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    # 4. Build Audit Package ZIP
    zip_path = build_audit_zip(slug, audit_id)

    # 5. Update reports table documents_count
    with lock:
        try:
            conn.execute(
                "UPDATE reports SET documents_count = ? WHERE customer_slug = ?",
                (len(frozen_docs), slug),
            )
            conn.commit()
        except Exception as exc:
            logger.debug("evidence_vault: could not update documents_count on reports table: %s", exc)

    logger.info(
        "evidence_vault: froze %d document(s) for customer %s (audit %s)",
        len(frozen_docs), slug, audit_id
    )

    return {
        "ok": True,
        "customer_slug": slug,
        "audit_id": audit_id,
        "documents_count": len(frozen_docs),
        "manifest_path": str(manifest_path.relative_to(_ROOT)),
        "zip_path": str(zip_path.relative_to(_ROOT)) if zip_path else None,
        "documents": frozen_docs,
    }


def build_audit_zip(customer_slug: str, audit_id: str) -> Optional[Path]:
    """Assemble a standalone ZIP package containing report HTML, JSON snapshot, manifest, and source documents."""
    slug = _slugify(customer_slug)
    safe_audit_id = _slugify(audit_id)
    audit_folder = _snapshots_dir() / slug / safe_audit_id
    if not audit_folder.is_dir():
        logger.warning("evidence_vault: cannot build ZIP, folder does not exist: %s", audit_folder)
        return None

    zip_filename = f"PE_Audit_Package_{slug}_{safe_audit_id}.zip"
    cust_pkg_dir = _packages_dir() / slug
    cust_pkg_dir.mkdir(parents=True, exist_ok=True)
    target_zip = cust_pkg_dir / zip_filename
    tmp_zip = cust_pkg_dir / f".{zip_filename}.tmp"

    try:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(audit_folder):
                for f in files:
                    full_p = Path(root) / f
                    # Calculate arcname relative to audit_folder
                    rel_to_audit = full_p.relative_to(audit_folder)
                    arcname = f"PE_Audit_{slug}/{rel_to_audit}"
                    zf.write(full_p, arcname=arcname)
        os.replace(tmp_zip, target_zip)
        logger.info("evidence_vault: built audit ZIP %s (%d bytes)", target_zip.name, target_zip.stat().st_size)
        return target_zip
    except Exception as exc:
        logger.warning("evidence_vault: failed building ZIP for %s: %s", slug, exc)
        if tmp_zip.exists():
            tmp_zip.unlink(missing_ok=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Query & Retrieval Operations
# ─────────────────────────────────────────────────────────────────────────────
def list_audit_documents(customer_slug: str, audit_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Retrieve all preserved evidence documents for an audit (or latest frozen audit)."""
    slug = _slugify(customer_slug)
    conn, lock = _get_db()
    with lock:
        if audit_id:
            cur = conn.execute(
                """SELECT doc_id, customer_slug, audit_id, document_type, document_label,
                          filename, file_size_bytes, file_hash, file_path, uploaded_at, is_frozen
                   FROM report_evidence_documents
                   WHERE customer_slug = ? AND audit_id = ?
                   ORDER BY uploaded_at ASC""",
                (slug, audit_id),
            )
        else:
            cur = conn.execute(
                """SELECT doc_id, customer_slug, audit_id, document_type, document_label,
                          filename, file_size_bytes, file_hash, file_path, uploaded_at, is_frozen
                   FROM report_evidence_documents
                   WHERE customer_slug = ?
                   ORDER BY is_frozen DESC, uploaded_at ASC""",
                (slug,),
            )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    return rows


def get_document(doc_id: str, customer_slug: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Retrieve document metadata and verify its physical file on disk."""
    conn, lock = _get_db()
    with lock:
        if customer_slug:
            cur = conn.execute(
                """SELECT doc_id, customer_slug, audit_id, document_type, document_label,
                          filename, file_size_bytes, file_hash, file_path, uploaded_at, is_frozen
                   FROM report_evidence_documents
                   WHERE doc_id = ? AND customer_slug = ?""",
                (doc_id, _slugify(customer_slug)),
            )
        else:
            cur = conn.execute(
                """SELECT doc_id, customer_slug, audit_id, document_type, document_label,
                          filename, file_size_bytes, file_hash, file_path, uploaded_at, is_frozen
                   FROM report_evidence_documents
                   WHERE doc_id = ?""",
                (doc_id,),
            )
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()

    if not row:
        return None
    record = dict(zip(cols, row))
    file_path = _ROOT / record["file_path"]

    # Security check: prevent path traversal outside _ROOT
    try:
        resolved = file_path.resolve()
        if not resolved.is_relative_to(_ROOT.resolve()):
            logger.warning("evidence_vault: path traversal blocked for doc_id=%s path=%s", doc_id, file_path)
            return None
    except Exception:
        return None

    if not file_path.exists():
        logger.warning("evidence_vault: document record exists but file missing: %s", file_path)
        return None

    record["absolute_path"] = str(file_path)
    return record


def get_latest_audit_package_zip(customer_slug: str) -> Optional[tuple[Path, str]]:
    """Get the audit package ZIP path and safe download filename for a customer."""
    slug = _slugify(customer_slug)
    pkg_dir = _packages_dir() / slug
    if pkg_dir.is_dir():
        zips = sorted(pkg_dir.glob("PE_Audit_Package_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        if zips:
            target = zips[0]
            return target, target.name

    # If no pre-built ZIP in packages, check if snapshots folder has audits we can build on the fly
    snap_cust = _snapshots_dir() / slug
    if snap_cust.is_dir():
        audit_dirs = sorted(
            [d for d in snap_cust.iterdir() if d.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if audit_dirs:
            latest_audit_id = audit_dirs[0].name
            built_zip = build_audit_zip(slug, latest_audit_id)
            if built_zip and built_zip.exists():
                return built_zip, built_zip.name

    return None


def attach_document_to_archive(
    customer: str,
    filename: str,
    raw_bytes: bytes,
    document_type: str = "other",
    label: Optional[str] = None,
    audit_id: Optional[str] = None,
) -> dict[str, Any]:
    """Manually attach a supplementary evidence document (waiver, signed letter, email) to an archived customer."""
    if not raw_bytes:
        return {"ok": False, "error": "Cannot attach empty file"}

    slug = _slugify(customer)
    safe_name = _sanitize_filename(filename)
    doc_label = label or DOCUMENT_LABELS.get(document_type, "Supplementary Review Evidence")
    file_hash = compute_sha256(raw_bytes)
    file_size = len(raw_bytes)
    doc_id = f"doc_supp_{file_hash[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()

    # If audit_id not provided, find latest audit_id for customer
    resolved_audit_id = audit_id
    if not resolved_audit_id:
        conn, lock = _get_db()
        with lock:
            cur = conn.execute(
                "SELECT audit_id FROM report_payload_snapshots WHERE customer_slug = ? ORDER BY generated_at DESC LIMIT 1",
                (slug,),
            )
            r = cur.fetchone()
            if r:
                resolved_audit_id = r[0]

    safe_audit = _slugify(resolved_audit_id or "supplementary")
    audit_folder = _snapshots_dir() / slug / safe_audit
    evidence_folder = audit_folder / "source_documents"
    evidence_folder.mkdir(parents=True, exist_ok=True)

    dest_path = evidence_folder / f"supp__{safe_name}"
    try:
        dest_path.write_bytes(raw_bytes)
        rel_dest = str(dest_path.relative_to(_ROOT))
    except Exception as exc:
        return {"ok": False, "error": f"Failed writing supplementary file: {exc}"}

    conn, lock = _get_db()
    with lock:
        try:
            conn.execute(
                """INSERT OR REPLACE INTO report_evidence_documents
                   (doc_id, customer_slug, audit_id, document_type, document_label,
                    filename, file_size_bytes, file_hash, file_path, uploaded_at, is_frozen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (doc_id, slug, resolved_audit_id, document_type, doc_label, safe_name, file_size, file_hash, rel_dest, now_iso),
            )
            # Update reports documents_count
            cur = conn.execute("SELECT COUNT(*) FROM report_evidence_documents WHERE customer_slug = ?", (slug,))
            cnt = cur.fetchone()[0]
            conn.execute("UPDATE reports SET documents_count = ? WHERE customer_slug = ?", (cnt, slug))
            conn.commit()
        except Exception as exc:
            return {"ok": False, "error": f"Database update failed: {exc}"}

    # Rebuild ZIP package if an audit folder exists
    if resolved_audit_id:
        build_audit_zip(slug, resolved_audit_id)

    return {
        "ok": True,
        "doc_id": doc_id,
        "customer_slug": slug,
        "filename": safe_name,
        "document_type": document_type,
        "document_label": doc_label,
        "file_size_bytes": file_size,
        "file_hash": file_hash,
    }
