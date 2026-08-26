"""
SOW Volume Baseline router.

GET  /api/sow/baseline          → stored SOW baseline values
GET  /api/sow/state             → current-engagement baseline, actuals and comparison
POST /api/sow/baseline          → save SOW baseline values
POST /api/sow/parse             → upload SOW doc and extract values
POST /api/sow/compare           → compare actuals against baseline
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from services import config_store
from services import session_cache
from services import pe_config as _pc

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
    status: str     # LOW | ACCEPTABLE | OPTIMAL | OVER | CRITICAL_OVER
    # Over-consumption provenance — set only when actual exceeds the contracted
    # ceiling, so every consumer states the same overage without recomputing it.
    over_by:     float = 0.0   # absolute units above contracted volume
    over_by_pct: float = 0.0   # percentage points above 100% of contract
    # Remaining contracted capacity. Negative means the actual exceeded SOW.
    capacity_buffer: float = 0.0
    capacity_buffer_pct: float = 0.0

class SowCompareRequest(BaseModel):
    actuals: Dict[str, float] = {}

class SowCompareResponse(BaseModel):
    metrics:        List[SowMetric]
    overall_status: str
    summary:        str
    # Populated whenever one or more metrics exceed the contracted ceiling.
    # Drives the red over-consumption caution banner and the report wording.
    overconsumption: Optional[Dict[str, Any]] = None
    bands:           Optional[Dict[str, float]] = None
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
    """Classify SOW consumption against the PE standard process window.

    Bands come from services/pe_config.py — never hardcode them here, the
    window is customer-tunable via config_store.

    LOW           < SOW_UNDER_PCT          — below the contracted floor. Findings
                                             are only validated at the tested volume.
    ACCEPTABLE    floor .. SOW_ACCEPTABLE_PCT — inside the standard window, lower end.
    OPTIMAL       SOW_ACCEPTABLE_PCT .. SOW_OVER_PCT — preferred zone.
    OVER          > SOW_OVER_PCT           — over-consumption vs contracted scope.
    CRITICAL_OVER > SOW_OVER_CRIT_PCT      — severe over-consumption; blocks PE
                                             sign-off until commercially resolved.
    """
    if pct > _pc.SOW_OVER_CRIT_PCT:     return "CRITICAL_OVER"
    if pct > _pc.SOW_OVER_PCT:          return "OVER"
    if pct < _pc.SOW_UNDER_PCT:         return "LOW"
    if pct < _pc.SOW_ACCEPTABLE_PCT:    return "ACCEPTABLE"
    return "OPTIMAL"


def _overage(sow: float, actual: float) -> tuple:
    """Absolute + percentage-point overage above the contracted volume.

    Returns (0.0, 0.0) when at or under contract, so callers can branch on a
    plain truthiness check without worrying about negative values.
    """
    if sow <= 0 or actual <= sow:
        return 0.0, 0.0
    return round(actual - sow, 2), round((actual - sow) / sow * 100, 1)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/sow/baseline")
def get_baseline() -> dict:
    return config_store.get(_SOW_KEY) or {}

@router.post("/sow/baseline")
def save_baseline(body: SowBaseline) -> dict:
    data = {
        k: v for k, v in body.model_dump().items()
        if v is not None and not (k == "custom" and not v)
    }
    config_store.set(_SOW_KEY, data)
    # Return the saved canonical baseline.  The MFE uses this response as its
    # shared state, so a status envelope here loses the actual target values.
    return data


@router.get("/sow/state")
def get_sow_state() -> dict:
    """Return the current engagement's SOW evidence without inventing actuals.

    Baseline targets are configuration; actuals and their comparison are
    session evidence.  Keeping both together lets every React route hydrate the
    same saved comparison after navigation or a browser refresh.
    """
    cached = session_cache.ac_get("volume_vs_sow") or {}
    comparison = cached.get("comparison") if isinstance(cached, dict) else None
    actuals = cached.get("actuals") if isinstance(cached, dict) else {}
    return {
        "baseline": config_store.get(_SOW_KEY) or {},
        "actuals": actuals if isinstance(actuals, dict) else {},
        "compare": comparison if isinstance(comparison, dict) else None,
    }

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


# ── Products / Modules Reviewed (manual multi-select LOV) ────────────────────
# Generic catalogue of Blue Yonder planning modules (Demand / ESP / Fulfillment /
# Platform families) — see services/product_taxonomy.py. The PE reviewer picks
# which subset was actually in scope for THIS engagement. The selection is
# engagement-specific (like sow_baseline) — persisted in config_store and
# mirrored into the shared audit context so every screen (Executive, Findings,
# Narrative/Export) can show "which product(s) were reviewed" consistently.
_REVIEWED_PRODUCTS_KEY = "reviewed_products"


class ReviewedProductsRequest(BaseModel):
    products: List[str] = []


@router.get("/sow/product-taxonomy")
def get_product_taxonomy() -> dict:
    from services.product_taxonomy import taxonomy_payload
    return taxonomy_payload()


@router.get("/sow/reviewed-products")
def get_reviewed_products() -> dict:
    values = config_store.get(_REVIEWED_PRODUCTS_KEY) or []
    from services.product_taxonomy import labels_for
    return {"products": values, "labels": labels_for(values)}


@router.post("/sow/reviewed-products")
def set_reviewed_products(body: ReviewedProductsRequest) -> dict:
    # De-dupe, preserve selection order, drop blanks
    seen: set[str] = set()
    values: List[str] = []
    for p in body.products or []:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            values.append(p)
    config_store.set(_REVIEWED_PRODUCTS_KEY, values)
    # Mirror into the shared audit context so Executive/Findings/Narrative
    # screens read the same value without a second round-trip.
    try:
        from services import session_cache
        session_cache.ac_set(_REVIEWED_PRODUCTS_KEY, values)
    except Exception:
        pass
    from services.product_taxonomy import labels_for
    return {"ok": True, "products": values, "labels": labels_for(values)}


class ManualSlaWindowsRequest(BaseModel):
    daily_hrs:   Optional[float] = None
    weekly_hrs:  Optional[float] = None
    monthly_hrs: Optional[float] = None

@router.post("/sow/sla-windows/manual")
def set_manual_sla_windows(body: ManualSlaWindowsRequest) -> dict:
    """Accept manually entered SLA ceiling values and store them as SOW windows (Tier 2).

    Setting a field to null removes any existing MANUAL entry for that type so
    'Clear All' (sends all-nulls) fully wipes manual overrides without touching
    values that came from a SOW PDF upload.
    """
    existing = config_store.get("_sow_sla_windows") or {}
    for field, key in [("daily_hrs", "DAILY"), ("weekly_hrs", "WEEKLY"), ("monthly_hrs", "MONTHLY")]:
        val = getattr(body, field)
        if val is not None and val > 0:
            existing[key] = {"limit_hours": val, "source": "MANUAL"}
        elif val is None and existing.get(key, {}).get("source") == "MANUAL":
            # Null explicitly clears a MANUAL entry — leaves SOW-sourced entries intact
            del existing[key]
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
        # An absent actual is unknown evidence, not a measured value of zero.
        # Do not emit a LOW metric for it: that false finding was the source of
        # the cross-page "0 of SOW" contradiction in PE Findings.
        if key not in actuals:
            continue
        act_f = float(actuals[key])
        pct   = round((act_f / sow_f) * 100, 1) if sow_f else 0.0
        # Use the ILC proxy label when the DFU figure came from item-location count
        _label = label
        if key == "daily_dfu" and _dfu_is_ilc:
            _label = _LABELS.get("item_location_customer", label)
        _ov, _ovp = _overage(sow_f, act_f)
        _capacity = round(sow_f - act_f, 2)
        metrics.append(SowMetric(key=key, label=_label, sow=sow_f,
                                 actual=act_f, pct=pct, status=_status(pct),
                                 over_by=_ov, over_by_pct=_ovp,
                                 capacity_buffer=_capacity,
                                 capacity_buffer_pct=round(_capacity / sow_f * 100, 1)))

    # Custom metrics
    for cm in (baseline.get("custom") or []):
        sow_f = float(cm.get("baseline", 0))
        if sow_f <= 0:
            continue
        key   = cm.get("key", "custom")
        label = cm.get("label", key)
        if key not in actuals:
            continue
        act_f = float(actuals[key])
        pct   = round((act_f / sow_f) * 100, 1)
        _ov, _ovp = _overage(sow_f, act_f)
        _capacity = round(sow_f - act_f, 2)
        metrics.append(SowMetric(key=key, label=label, sow=sow_f,
                                 actual=act_f, pct=pct, status=_status(pct),
                                 over_by=_ov, over_by_pct=_ovp,
                                 capacity_buffer=_capacity,
                                 capacity_buffer_pct=round(_capacity / sow_f * 100, 1)))

    if not metrics:
        baseline_count = sum(1 for key in _LABELS if float(baseline.get(key) or 0) > 0)
        baseline_count += sum(1 for cm in (baseline.get("custom") or []) if float(cm.get("baseline", 0) or 0) > 0)
        if baseline_count:
            response = SowCompareResponse(
                metrics=[], overall_status="AWAITING_ACTUALS",
                summary=f"{baseline_count} SOW target(s) loaded; enter actual values before volume compliance can be assessed.",
                bands={"under": _pc.SOW_UNDER_PCT, "over": _pc.SOW_OVER_PCT, "crit": _pc.SOW_OVER_CRIT_PCT},
            )
        else:
            response = SowCompareResponse(metrics=[], overall_status="N/A",
                                          summary="No SOW baseline values set. Enter targets in the form above.")
        _persist_sow_comparison(actuals, response)
        return response

    crit_over   = [m for m in metrics if m.status == "CRITICAL_OVER"]
    over        = [m for m in metrics if m.status == "OVER"]
    exceeded    = crit_over + over
    lows        = sum(1 for m in metrics if m.status == "LOW")
    optimals    = sum(1 for m in metrics if m.status == "OPTIMAL")
    acceptables = sum(1 for m in metrics if m.status == "ACCEPTABLE")
    in_range    = optimals + acceptables

    _fmt = lambda v: f"{v:,.0f}" if abs(v) >= 1 else f"{v:g}"

    overconsumption = None
    if exceeded:
        worst = max(exceeded, key=lambda m: m.pct)
        overconsumption = {
            "count":          len(exceeded),
            "critical_count": len(crit_over),
            "severity":       "CRITICAL_OVER" if crit_over else "OVER",
            "worst_label":    worst.label,
            "worst_pct":      worst.pct,
            "worst_sow":      worst.sow,
            "worst_actual":   worst.actual,
            "worst_over_by":  worst.over_by,
            "items": [
                {"label": m.label, "sow": m.sow, "actual": m.actual,
                 "pct": m.pct, "over_by": m.over_by, "status": m.status}
                for m in sorted(exceeded, key=lambda m: m.pct, reverse=True)
            ],
        }

    if crit_over:
        overall = "CRITICAL_OVER"
        _names  = ", ".join(m.label for m in crit_over)
        summary = (
            f"\U0001F534 OVERCONSUMPTION \u2014 {len(crit_over)} metric(s) above "
            f"{_pc.SOW_OVER_CRIT_PCT:g}% of contracted scope ({_names}). "
            f"Worst: {worst.label} contracted {_fmt(worst.sow)}, actual "
            f"{_fmt(worst.actual)} \u2014 exceeding the contracted amount by "
            f"{_fmt(worst.over_by)} ({worst.pct:.1f}% of SOW). This must be "
            f"commercially addressed and acknowledged before final PE sign-off."
        )
    elif over:
        overall = "OVER"
        summary = (
            f"\u26A0\uFE0F OVERCONSUMPTION \u2014 {len(over)} metric(s) above "
            f"{_pc.SOW_OVER_PCT:g}% of contracted scope. Worst: {worst.label} "
            f"contracted {_fmt(worst.sow)}, actual {_fmt(worst.actual)} "
            f"\u2014 exceeding the contracted amount by {_fmt(worst.over_by)} "
            f"({worst.pct:.1f}% of SOW). Outside the "
            f"{_pc.SOW_UNDER_PCT:g}%\u2013{_pc.SOW_OVER_PCT:g}% standard process "
            f"window \u2014 formal review and acknowledgment required."
        )
    elif lows > len(metrics) // 2:
        overall = "LOW"
        summary = (
            f"\U0001F4C9 {lows}/{len(metrics)} metrics below {_pc.SOW_UNDER_PCT:g}% "
            f"of SOW \u2014 outside the {_pc.SOW_UNDER_PCT:g}%\u2013"
            f"{_pc.SOW_OVER_PCT:g}% standard process window. Findings are validated "
            f"at the tested volume only, not at full contracted scale."
        )
    elif in_range >= len(metrics) * 0.7:
        # `in_range` includes both OPTIMAL and ACCEPTABLE metrics. Do not
        # badge the engagement OPTIMAL when every in-range metric is only in
        # the lower acceptable band.
        overall = "OPTIMAL" if optimals >= len(metrics) * 0.7 else "ACCEPTABLE"
        confidence = "HIGH" if overall == "OPTIMAL" else "MODERATE-HIGH"
        summary = (
            f"\u2705 {in_range}/{len(metrics)} metrics within the "
            f"{_pc.SOW_UNDER_PCT:g}%\u2013{_pc.SOW_OVER_PCT:g}% SOW standard process "
            f"window ({optimals} in preferred 90\u2013{_pc.SOW_OVER_PCT:g}% zone, "
            f"{acceptables} in the lower acceptable range). Go-live confidence {confidence}."
        )
    else:
        overall = "MODERATE"
        summary = (
            f"\U0001F7E1 Mixed results \u2014 {in_range} within the standard window, "
            f"{lows} below {_pc.SOW_UNDER_PCT:g}%, {len(exceeded)} above "
            f"{_pc.SOW_OVER_PCT:g}%. Deviations require formal review and acknowledgment."
        )

    resp = SowCompareResponse(
        metrics=metrics, overall_status=overall, summary=summary,
        overconsumption=overconsumption,
        bands={
            "under": _pc.SOW_UNDER_PCT,
            "over":  _pc.SOW_OVER_PCT,
            "crit":  _pc.SOW_OVER_CRIT_PCT,
        },
    )
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
    _persist_sow_comparison(actuals, resp)
    return resp


def _persist_sow_comparison(actuals: Dict[str, float], response: SowCompareResponse) -> None:
    """Make a saved SOW comparison available to Findings, Narrative and React.

    Preserve SOW-parser fields already in ``volume_vs_sow`` while adding the
    exact response shape that downstream consumers expect.  This is deliberately
    current-session evidence; New Engagement / Hard Reset clears the cache.
    """
    existing = session_cache.ac_get("volume_vs_sow") or {}
    existing = existing if isinstance(existing, dict) else {}
    comparison = response.model_dump()
    session_cache.ac_set("volume_vs_sow", {
        **existing,
        "actuals": {key: float(value) for key, value in actuals.items()},
        "comparison": comparison,
        # Findings' existing cache fallback reads these top-level fields.
        "metrics": comparison["metrics"],
        "overall_status": comparison["overall_status"],
        "summary": comparison["summary"],
        "bands": comparison.get("bands"),
        "overconsumption": comparison.get("overconsumption"),
    })
