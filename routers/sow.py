"""
SOW Volume Baseline router.

GET  /api/sow/baseline          → stored SOW baseline values
POST /api/sow/baseline          → save SOW baseline values
POST /api/sow/parse             → upload SOW doc and extract values
POST /api/sow/compare           → compare actuals against baseline
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from services import config_store

router = APIRouter()
_SOW_KEY = "sow_baseline"

# ── Models ────────────────────────────────────────────────────────────────────

class SowBaseline(BaseModel):
    model_config = ConfigDict(extra="allow")
    daily_dfu:         Optional[float] = None
    daily_sku:         Optional[float] = None
    daily_orders:      Optional[float] = None
    batch_jobs:        Optional[float] = None
    peak_users:        Optional[float] = None
    data_volume_gb:    Optional[float] = None
    cpu_baseline_pct:  Optional[float] = None
    mem_baseline_pct:  Optional[float] = None
    disk_baseline_pct: Optional[float] = None
    custom: Optional[List[Dict[str, Any]]] = []

class SowMetric(BaseModel):
    key:    str
    label:  str
    sow:    float
    actual: float
    pct:    float   # actual/sow*100
    status: str     # OPTIMAL | MODERATE | LOW | HIGH

class SowCompareRequest(BaseModel):
    actuals: Dict[str, float] = {}

class SowCompareResponse(BaseModel):
    metrics:        List[SowMetric]
    overall_status: str
    summary:        str
    ai_narrative:   Optional[str] = None
    ai_model:       Optional[str] = None

# ── Helpers ───────────────────────────────────────────────────────────────────

_LABELS = {
    "daily_dfu":                 "Daily DFU",
    "item_location_customer":    "Item-Location-Customer (DFU proxy)",  # when DFU is ILC count
    "daily_sku":                 "Daily SKU Count",
    "daily_orders":              "Daily Orders",
    "batch_jobs":                "Batch Jobs/Day",
    "peak_users":                "Peak Concurrent Users",
    "data_volume_gb":            "Data Volume (GB)",
    "cpu_baseline_pct":          "CPU Utilisation %",
    "mem_baseline_pct":          "Memory Utilisation %",
    "disk_baseline_pct":         "Disk Utilisation %",
}

def _status(pct: float) -> str:
    """Classify SOW consumption against the 70%–110% standard process window.

    Standard: consumption at UAT must remain within 70%-110% of approved SOW
    limits. Anything outside this range requires formal review and acknowledgment.

    LOW       < 70%    — below standard floor, formal acknowledgment required
    ACCEPTABLE 70–90%  — within 70-110% acceptable window (lower end)
    OPTIMAL   90–110%  — preferred zone within 70-110% window
    HIGH      > 110%   — above standard ceiling, formal acknowledgment required
    """
    if pct < 70:   return "LOW"        # deviation — below 70% floor
    if pct < 90:   return "ACCEPTABLE" # within standard 70-110% window
    if pct <= 110: return "OPTIMAL"    # preferred zone within standard window
    return "HIGH"                      # deviation — above 110% ceiling

# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/sow/baseline")
def get_baseline() -> dict:
    return config_store.get(_SOW_KEY) or {}

@router.post("/sow/baseline")
def save_baseline(body: SowBaseline) -> dict:
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    config_store.set(_SOW_KEY, data)
    return {"ok": True, "saved": len(data)}

@router.delete("/sow/baseline")
def clear_baseline() -> dict:
    """Wipe the stored SOW baseline — called on new engagement or when user clears the form."""
    config_store.set(_SOW_KEY, {})
    return {"ok": True}

@router.post("/sow/parse")
async def parse_sow(file: UploadFile = File(...)) -> dict:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    api_key = config_store.get_gemini_key() or ""
    try:
        from services.sow_parser import parse_sow_volumes, parse_sow_contract
        # Full contract extraction (SLA windows + volume ramp + metadata)
        contract = parse_sow_contract(raw, file.filename or "", api_key)
        # Store SLA ceilings for the 3-tier SLA resolver (tier 2)
        if contract.get("sla_windows"):
            config_store.set("_sow_sla_windows", contract["sla_windows"])
        # Store volume-by-year for the SOW tab growth chart
        if contract.get("volume_by_year"):
            config_store.set("_sow_volume_by_year", contract["volume_by_year"])
        # Store contract metadata
        meta_keys = ("customer_name", "contract_years", "annual_fee", "currency",
                     "max_item_locations", "growth_pack_size",
                     "availability_sla_pct", "disaster_recovery")
        meta = {k: contract[k] for k in meta_keys if contract.get(k) is not None}
        if meta:
            existing = config_store.get("_sow_contract_meta") or {}
            config_store.set("_sow_contract_meta", {**existing, **meta})
        # ── Audit context: sow_contract + volume_vs_sow (E5) ─────────
        try:
            from services import session_cache
            full_contract = {
                **meta,
                "sla_windows":    contract.get("sla_windows")    or {},
                "volume_by_year": contract.get("volume_by_year") or {},
                "operational_standards": contract.get("operational_standards") or {},
            }
            session_cache.ac_set("sow_contract",  full_contract)
            session_cache.ac_set("volume_vs_sow", {
                "volume_by_year": contract.get("volume_by_year") or {},
                "max_item_locations": contract.get("max_item_locations"),
            })
            if meta.get("customer_name"):
                session_cache.ac_set("customer_name", meta["customer_name"])
            # Auto-update the manual baseline with SOW-extracted DFU/SKU values so
            # the manual override form always reflects the current SOW document.
            vol_by_year = contract.get("volume_by_year") or {}
            raw_volumes = contract.get("raw_volumes") or {}
            if vol_by_year:
                max_vol = max(
                    (v.get("item_locations", 0) if isinstance(v, dict) else float(v or 0)
                     for v in vol_by_year.values()),
                    default=0,
                )
                if max_vol > 0:
                    baseline = {"daily_dfu": max_vol}
                    # Only set SKU if the SOW actually contains SKU data
                    daily_sku = raw_volumes.get("daily_sku")
                    if daily_sku and float(daily_sku) > 0:
                        baseline["daily_sku"] = float(daily_sku)
                    config_store.set(_SOW_KEY, baseline)
        except Exception:
            pass
        # Return flat volume dict (backward compat) merged with contract enrichments
        volumes = contract.get("raw_volumes") or {}
        return {**volumes, "_contract": contract}
    except Exception as exc:
        raise HTTPException(422, f"Cannot parse SOW: {exc}") from exc


@router.get("/sow/sla-windows")
def get_sow_sla_windows() -> dict:
    """Return SOW-extracted batch-type SLA ceilings for use by the SLA resolver."""
    return {
        "sla_windows":    config_store.get("_sow_sla_windows") or {},
        "volume_by_year": config_store.get("_sow_volume_by_year") or {},
        "contract_meta":  config_store.get("_sow_contract_meta") or {},
    }

class ManualSlaWindowsRequest(BaseModel):
    daily_hrs:   Optional[float] = None
    weekly_hrs:  Optional[float] = None
    monthly_hrs: Optional[float] = None

@router.post("/sow/sla-windows/manual")
def set_manual_sla_windows(body: ManualSlaWindowsRequest) -> dict:
    """Accept manually entered SLA ceiling values and store them as SOW windows (Tier 2)."""
    existing = config_store.get("_sow_sla_windows") or {}
    if body.daily_hrs   and body.daily_hrs   > 0:
        existing["DAILY"]   = {"limit_hours": body.daily_hrs,   "source": "MANUAL"}
    if body.weekly_hrs  and body.weekly_hrs  > 0:
        existing["WEEKLY"]  = {"limit_hours": body.weekly_hrs,  "source": "MANUAL"}
    if body.monthly_hrs and body.monthly_hrs > 0:
        existing["MONTHLY"] = {"limit_hours": body.monthly_hrs, "source": "MANUAL"}
    config_store.set("_sow_sla_windows", existing)
    return {"ok": True, "windows": existing}

@router.post("/sow/compare", response_model=SowCompareResponse)
def compare_sow(body: SowCompareRequest) -> SowCompareResponse:
    baseline = config_store.get(_SOW_KEY) or {}
    actuals  = body.actuals or {}
    metrics: List[SowMetric] = []

    # Fix 4a — If the baseline's daily_dfu was derived from Item-Location-Customer
    # counts (ILC), relabel it so the display is clear to the reader.
    # ILC derivation is flagged when max_item_locations is set in the SOW contract
    # (meaning the "DFU" figure came from an ILC count, not an actual DFU file).
    _contract = config_store.get("_sow_contract_meta") or {}
    _dfu_is_ilc = bool(_contract.get("max_item_locations") and not _contract.get("total_dfus"))

    for key, label in _LABELS.items():
        sow = baseline.get(key)
        if sow is None or float(sow) <= 0:
            continue
        sow_f = float(sow)
        act_f = float(actuals.get(key, 0))
        pct   = round((act_f / sow_f) * 100, 1) if sow_f else 0.0
        # Use the ILC proxy label when the DFU figure came from item-location count
        _label = label
        if key == "daily_dfu" and _dfu_is_ilc:
            _label = _LABELS.get("item_location_customer", label)
        metrics.append(SowMetric(key=key, label=_label, sow=sow_f,
                                 actual=act_f, pct=pct, status=_status(pct)))

    # Custom metrics
    for cm in (baseline.get("custom") or []):
        sow_f = float(cm.get("baseline", 0))
        if sow_f <= 0:
            continue
        key   = cm.get("key", "custom")
        label = cm.get("label", key)
        act_f = float(actuals.get(key, 0))
        pct   = round((act_f / sow_f) * 100, 1)
        metrics.append(SowMetric(key=key, label=label, sow=sow_f,
                                 actual=act_f, pct=pct, status=_status(pct)))

    if not metrics:
        return SowCompareResponse(metrics=[], overall_status="N/A",
                                  summary="No SOW baseline values set. Enter targets in the form above.")

    highs       = sum(1 for m in metrics if m.status == "HIGH")
    lows        = sum(1 for m in metrics if m.status == "LOW")
    optimals    = sum(1 for m in metrics if m.status == "OPTIMAL")
    acceptables = sum(1 for m in metrics if m.status == "ACCEPTABLE")
    in_range    = optimals + acceptables  # 70-110% = within standard window

    if highs > 0:
        overall = "HIGH"
        summary = (f"⚠️ {highs} metric(s) above 110% of SOW — outside 70%-110% standard process window. "
                   f"Formal review and acknowledgment required per PE standard process.")
    elif lows > len(metrics) // 2:
        overall = "LOW"
        summary = (f"📉 {lows}/{len(metrics)} metrics below 70% of SOW — outside 70%-110% standard process window. "
                   f"Verify test scenarios are representative. Formal acknowledgment required.")
    elif in_range >= len(metrics) * 0.7:
        overall = "OPTIMAL"
        summary = (f"✅ {in_range}/{len(metrics)} metrics within 70%-110% SOW standard process window "
                   f"({optimals} in preferred 90-110% zone, {acceptables} in 70-90% acceptable range). "
                   f"Go-live confidence HIGH.")
    else:
        overall = "MODERATE"
        summary = (f"🟡 Mixed results — {in_range} within 70-110% window, {lows} below 70%, {highs} above 110%. "
                   f"Deviations require formal review and acknowledgment.")

    resp = SowCompareResponse(metrics=metrics, overall_status=overall, summary=summary)
    try:
        from services.ai_narrator import narrate
        text, model = narrate("sow", {
            "overall_status": overall,
            "summary":        summary,
            "metrics":        [m.model_dump() for m in metrics],
        })
        if text:
            resp.ai_narrative = text
            resp.ai_model     = model
    except Exception:
        pass
    return resp
