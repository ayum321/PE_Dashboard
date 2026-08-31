"""Local Report Archive API.

Read-only surface over services/report_archive.py — list what's been
generated, view a stored report inline, or force-download it. No cloud
involved: everything is served from the local SQLite index + local HTML
files created by routers/export.py's save-on-generate hook.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from services import report_archive

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
