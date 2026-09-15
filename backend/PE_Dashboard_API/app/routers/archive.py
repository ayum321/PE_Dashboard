"""Local Report Archive API.

Read-only surface over services/report_archive.py — list what's been
generated, view a stored report inline, or force-download it. No cloud
involved: everything is served from the local SQLite index + local HTML
files created by routers/export.py's save-on-generate hook.
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from services import evidence_vault, report_archive

router = APIRouter()

# Archived reports contain customer-specific audit material.  Do not allow a
# browser or intermediary cache to retain it after the user leaves this local
# dashboard.  Keep these headers on every success and 404 response.
_ARCHIVE_HEADERS = {
    "Cache-Control": "private, no-store, no-cache, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}


@router.get("/report-archive", summary="List all archived reports (latest per customer)")
async def list_archive():
    return JSONResponse(content={"reports": report_archive.list_reports()}, headers=_ARCHIVE_HEADERS)


@router.get("/report-archive/{slug}", summary="View an archived report inline")
async def view_archive_report(slug: str):
    record = report_archive.get_report(slug)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No archived report for '{slug}'",
            headers=_ARCHIVE_HEADERS,
        )
    return HTMLResponse(content=record["html"], headers=_ARCHIVE_HEADERS)


@router.get("/report-archive/{slug}/download", summary="Download an archived report")
async def download_archive_report(slug: str):
    record = report_archive.get_report(slug)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"No archived report for '{slug}'",
            headers=_ARCHIVE_HEADERS,
        )
    filename = report_archive.download_filename(record["customer_slug"])
    return HTMLResponse(
        content=record["html"],
        headers={
            **_ARCHIVE_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/report-archive/import", summary="Import an exported HTML report into Review Registry")
async def import_archive_report(file: UploadFile = File(...)):
    try:
        content_bytes = await file.read()
        html_text = content_bytes.decode("utf-8", errors="replace")
        result = report_archive.import_html_report(html_text, filename=file.filename or "")
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=result.get("error", "Failed to import report"),
                headers=_ARCHIVE_HEADERS,
            )
        return JSONResponse(content=result, headers=_ARCHIVE_HEADERS)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc), headers=_ARCHIVE_HEADERS)


# ── Evidence Vault Endpoints ──────────────────────────────────────────────────
@router.get("/report-archive/{slug}/documents", summary="List preserved evidence documents for an archived audit")
async def list_archive_documents(slug: str):
    docs = evidence_vault.list_audit_documents(slug)
    pkg = evidence_vault.get_latest_audit_package_zip(slug)
    return JSONResponse(
        content={
            "customer_slug": slug,
            "documents": docs,
            "documents_count": len(docs),
            "package_available": pkg is not None,
            "package_name": pkg[1] if pkg else None,
        },
        headers=_ARCHIVE_HEADERS,
    )


@router.get("/report-archive/{slug}/documents/{doc_id}/download", summary="Download a preserved evidence document")
async def download_evidence_document(slug: str, doc_id: str):
    doc = evidence_vault.get_document(doc_id, customer_slug=slug)
    if not doc or not doc.get("absolute_path"):
        raise HTTPException(
            status_code=404,
            detail=f"Evidence document '{doc_id}' not found for '{slug}'",
            headers=_ARCHIVE_HEADERS,
        )
    return FileResponse(
        path=doc["absolute_path"],
        filename=doc["filename"],
        headers=_ARCHIVE_HEADERS,
    )


@router.get("/report-archive/{slug}/package/download", summary="Download full PE Audit ZIP Package")
async def download_audit_package(slug: str):
    pkg = evidence_vault.get_latest_audit_package_zip(slug)
    if not pkg:
        raise HTTPException(
            status_code=404,
            detail=f"No audit package ZIP available for '{slug}'",
            headers=_ARCHIVE_HEADERS,
        )
    zip_path, zip_name = pkg
    return FileResponse(
        path=str(zip_path),
        filename=zip_name,
        media_type="application/zip",
        headers=_ARCHIVE_HEADERS,
    )


@router.post("/report-archive/{slug}/documents/attach", summary="Attach supplementary proof to an archived audit")
async def attach_archive_document(
    slug: str,
    file: UploadFile = File(...),
    document_type: str = "other",
    label: str = "",
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.", headers=_ARCHIVE_HEADERS)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.", headers=_ARCHIVE_HEADERS)
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.", headers=_ARCHIVE_HEADERS)

    result = evidence_vault.attach_document_to_archive(
        customer=slug,
        filename=file.filename,
        raw_bytes=raw,
        document_type=document_type or "other",
        label=label or None,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=500,
            detail=result.get("error", "Failed to attach document"),
            headers=_ARCHIVE_HEADERS,
        )
    return JSONResponse(content=result, headers=_ARCHIVE_HEADERS)

