"""
Resource utilization router.

POST /api/process-resource
    body: { "servers": [<parsed server record>, ...] }
    response: { "kpis": {...}, "anomalies": [...], "servers": [...] }

The pipeline expects servers already produced by /api/upload — i.e.
records carrying cpu_used / mem_used / disk_used_max / host / type.
Image-only DOCX servers (all-zero metrics) are accepted and surfaced
in the response with `image_only=True`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from services.resource_calculator import build_resource_payload

router = APIRouter()


# ── Pydantic models ────────────────────────────────────────────
class ResourceRequest(BaseModel):
    """Pre-parsed server records as JSON."""
    model_config = ConfigDict(extra="allow")
    servers: List[Dict[str, Any]]


class ResourceKpis(BaseModel):
    model_config = ConfigDict(extra="allow")

    total_servers: int = 0
    known_servers: int = 0
    image_only:    int = 0
    fleet_grade:   str = "?"
    fleet_score:   float = 0.0
    avg_cpu:       float = 0.0
    avg_mem:       float = 0.0
    avg_disk:      float = 0.0
    n_critical:    int = 0
    n_warning:     int = 0
    n_healthy:     int = 0
    n_app:         int = 0
    n_db:          int = 0
    n_sre:         int = 0
    n_agg_trap:    int = 0
    n_dual_pressure: int = 0
    thresholds:    Optional[Dict[str, float]] = None


class ResourceServer(BaseModel):
    model_config = ConfigDict(extra="allow")

    host: str
    server: str
    type: str = "APP"
    cpu_pct: Optional[float] = 0.0
    cpu_avg_pct: Optional[float] = 0.0
    effective_cpu: Optional[float] = 0.0
    mem_pct: Optional[float] = 0.0
    mem_gb: Optional[float] = 0.0
    disk_pct: Optional[float] = 0.0
    image_only: bool = False
    status: str = "Unknown"
    health_score: Optional[float] = None
    source_env: Optional[str] = ""
    agg_trap: bool = False
    dual_pressure: bool = False
    role_cpu_ok: float = 60.0
    role_cpu_warn: float = 80.0
    cpu_available: bool = True
    mem_available: bool = True
    disk_available: bool = True


class ResourceAnomaly(BaseModel):
    model_config = ConfigDict(extra="allow")

    host: str
    metric: str
    value: float
    z: float


class ResourceResponse(BaseModel):
    kpis: ResourceKpis
    anomalies: List[ResourceAnomaly]
    servers: List[ResourceServer]
    executive_summary: Optional[Dict[str, Any]] = None


# ── Endpoint ───────────────────────────────────────────────────
@router.post(
    "/process-resource",
    response_model=ResourceResponse,
    status_code=status.HTTP_200_OK,
    summary="Run Fleet Intelligence on parsed server records and return KPIs + chart data",
)
async def process_resource(body: ResourceRequest) -> ResourceResponse:
    if body.servers is None:
        raise HTTPException(status_code=400, detail="`servers` is required.")

    try:
        payload = build_resource_payload(body.servers)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Resource fleet computation failed: {exc}",
        ) from exc

    # Persist the fully-processed result (kpis + normalized servers with cpu_pct/mem_pct)
    # so pe_narrative and pe_consultant can hydrate from session_cache even when the
    # frontend sends a stale or null resource payload.
    try:
        from services import session_cache
        session_cache.set("last_resource", payload)
        session_cache.ac_set("resource_summary", payload)
    except Exception as cache_exc:
        import logging as _logging
        _logging.getLogger(__name__).warning("resource cache write failed: %s", cache_exc)

    return ResourceResponse(
        kpis=ResourceKpis(**payload["kpis"]),
        anomalies=payload["anomalies"],
        servers=payload["servers"],
    )
