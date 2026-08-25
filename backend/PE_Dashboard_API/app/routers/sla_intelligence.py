"""
SLA Intelligence router — generic file analyzer.

POST /api/sla-intelligence
    Accepts one or more uploaded files (multipart, field name 'files').
    Each file may be Type A (SLA contract), Type B (raw execution log),
    or Type C (pre-computed summary). The endpoint auto-detects type
    per sheet and returns the structured 9-phase analysis.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, File, HTTPException, UploadFile

from services.sla_intelligence import analyze_files

router = APIRouter()


@router.post("/sla-intelligence", operation_id="sla_intelligence_multi")
async def sla_intelligence(files: List[UploadFile] = File(...)) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    payload = []
    for f in files:
        raw = await f.read()
        if not raw:
            continue
        payload.append((f.filename or "upload.bin", raw))
    if not payload:
        raise HTTPException(status_code=400, detail="All uploaded files are empty")
    try:
        result = analyze_files(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc
    return result
