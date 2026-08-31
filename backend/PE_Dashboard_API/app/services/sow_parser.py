"""
SOW (Statement of Work) volume targets parser.
Extracts PE volume metrics from PDF/DOCX using Gemini AI + regex fallback.

Also extracts:
  - sla_windows: batch-type SLA ceilings (e.g. DAILY=6h, WEEKLY=8h)
  - volume_by_year: DFU/SKU item-location ramp per contract year (CY1/CY2/CY3)
  - contract metadata: customer_name, annual_fee, currency, contract_years
  - availability SLA %, disaster recovery RTO/RPO, max_item_locations
"""
from __future__ import annotations
import io
import json
import re
from typing import Any, Dict

_METRIC_LABELS = {
    "daily_dfu":         "Daily DFU (Demand Fulfillment Units)",
    "daily_sku":         "Daily SKU Count",
    "daily_orders":      "Daily Orders / Transactions",
    "batch_jobs":        "Daily Batch Jobs",
    "peak_users":        "Peak Concurrent Users",
    "data_volume_gb":    "Daily Data Volume (GB)",
    "cpu_baseline_pct":  "CPU Utilisation Baseline (%)",
    "mem_baseline_pct":  "Memory Utilisation Baseline (%)",
    "disk_baseline_pct": "Disk Utilisation Baseline (%)",
}

def parse_sow_volumes(raw_bytes: bytes, filename: str, api_key: str = "") -> Dict[str, Any]:
    """Extract volume targets from a SOW document. Returns {key: float} dict."""
    text = _extract_text(raw_bytes, filename)
    if api_key and text:
        try:
            return _gemini_extract(text[:6000], api_key)
        except Exception:
            pass
    return _regex_extract(text)


def _extract_text(raw_bytes: bytes, filename: str) -> str:
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    try:
        if ext == "pdf":
            import fitz
            doc = fitz.open(stream=raw_bytes, filetype="pdf")
            return "\n".join(page.get_text() for page in doc)
        elif ext in ("docx", "doc"):
            from docx import Document
            doc = Document(io.BytesIO(raw_bytes))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        else:
            return raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return raw_bytes.decode("utf-8", errors="replace")[:10000]


def _regex_extract(text: str) -> Dict[str, Any]:
    t = text.lower()
    patterns = {
        "daily_dfu":         r"(?:daily\s+)?dfu[\s:=]+(\d[\d,\.]*)",
        "daily_sku":         r"(?:daily\s+)?sku[\s:=]+(\d[\d,\.]*)",
        "daily_orders":      r"(?:daily\s+)?orders?[\s:=]+(\d[\d,\.]*)",
        "batch_jobs":        r"batch\s+jobs?[\s:=]+(\d[\d,\.]*)",
        "peak_users":        r"(?:peak|concurrent)\s+users?[\s:=]+(\d[\d,\.]*)",
        "data_volume_gb":    r"data\s+volume[\s:=]+(\d[\d,\.]*)\s*gb",
        "cpu_baseline_pct":  r"cpu[\s:=]+(\d[\d,\.]*)\s*%",
        "mem_baseline_pct":  r"mem(?:ory)?[\s:=]+(\d[\d,\.]*)\s*%",
        "disk_baseline_pct": r"disk[\s:=]+(\d[\d,\.]*)\s*%",
    }
    result: Dict[str, Any] = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, t, re.IGNORECASE)
        if m:
            try:
                result[key] = float(m.group(1).replace(",", ""))
            except Exception:
                pass
    return result


def _gemini_extract(text: str, api_key: str) -> Dict[str, Any]:
    prompt = f"""You are a performance engineering consultant. Extract volume targets from this Statement of Work (SOW) document.

Return ONLY valid JSON with these exact keys (use null for missing values):
{{
  "daily_dfu": <number or null>,
  "daily_sku": <number or null>,
  "daily_orders": <number or null>,
  "batch_jobs": <number or null>,
  "peak_users": <number or null>,
  "data_volume_gb": <number or null>,
  "cpu_baseline_pct": <number or null>,
  "mem_baseline_pct": <number or null>,
  "disk_baseline_pct": <number or null>
}}

SOW TEXT:
{text}

JSON only, no explanation:"""

    # Try new SDK first
    try:
        from google import genai as _genai
        client = _genai.Client(api_key=api_key)
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        raw = response.text.strip()
    except ImportError:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash-001")
        raw = model.generate_content(prompt).text.strip()

    # Parse JSON from response
    m = re.search(r'\{[\s\S]*?\}', raw)
    if m:
        data = json.loads(m.group())
    else:
        data = json.loads(raw)

    # Remove null values
    return {k: float(v) for k, v in data.items() if v is not None}


# ── Contract-level extraction (SLA windows + volume ramp + metadata) ──────────

def parse_sow_contract(raw_bytes: bytes, filename: str, api_key: str = "") -> Dict[str, Any]:
    """
    Extract full contract intelligence from a SOW PDF/DOCX.

    Returns a dict with:
      sla_windows       → {"DAILY": {"limit_hours": 6.0, "source": "SOW_EXTRACTED"}, ...}
      volume_by_year    → {"CY1": {"item_locations": 10000, "uom": "Item-Locations"}, ...}
      max_item_locations, growth_pack_size
      availability_sla_pct
      disaster_recovery → {"rto_hours": 48, "rpo_hours": 4, "level": "..."}
      customer_name, contract_years, annual_fee, currency
      raw_volumes       → the existing daily_dfu/sku dict (backward compat)
    """
    text = _extract_text(raw_bytes, filename)
    result: Dict[str, Any] = {}

    # --- Gemini full extraction (preferred) ---
    if api_key and text:
        try:
            result = _gemini_extract_contract(text[:8000], api_key)
        except Exception:
            pass

    # --- Regex fallback / supplement ---
    _regex_supplement_contract(text, result)

    # Always include raw volume metrics for backward compat
    result["raw_volumes"] = parse_sow_volumes(raw_bytes, filename, api_key)

    return result


def _gemini_extract_contract(text: str, api_key: str) -> Dict[str, Any]:
    """Use Gemini to extract the full contract intelligence block."""
    prompt = f"""You are a Performance Engineering consultant analysing a Statement of Work (SOW).
Extract ALL of the following from the document.  Return ONLY valid JSON — no markdown, no explanation.

{{
  "customer_name": "<full legal entity name or null>",
  "contract_years": <integer or null>,
  "annual_fee": "<numeric string or null>",
  "currency": "<3-letter code or null>",
  "sla_windows": {{
    "DAILY":  {{"limit_hours": <number or null>}},
    "WEEKLY": {{"limit_hours": <number or null>}},
    "MONTHLY":{{"limit_hours": <number or null>}}
  }},
  "volume_by_year": {{
    "CY1": {{"item_locations": <number or null>, "uom": "<unit label>"}},
    "CY2": {{"item_locations": <number or null>, "uom": "<unit label>"}},
    "CY3": {{"item_locations": <number or null>, "uom": "<unit label>"}}
  }},
  "max_item_locations": <number or null>,
  "growth_pack_size": <number or null>,
  "availability_sla_pct": <number or null>,
  "disaster_recovery": {{
    "level": "<tier name or null>",
    "rto_hours": <number or null>,
    "rpo_hours": <number or null>
  }}
}}

Hints for Blue Yonder / JDA SOW documents:
- Batch window appears as: "Batch Window – 6 hr. Daily / 8hr. Weekly" — extract 6 for DAILY, 8 for WEEKLY
- Volume appears as: "Contract Year 1: 10,000 Item-Locations" rows in a table
- Availability appears in a table: "Standard Availability  99.7%"
- Disaster Recovery level appears as: "Disaster Recovery – Standard Plus" on the cover page
  and the RTO/RPO table on a later page: "Standard Plus  Up to 48 Hours  4 hours"
- Annual fee appears as: "Total Annual Subscription Fee (excluding taxes): €207,000"

SOW TEXT:
{text}

JSON only:"""

    raw = ""
    try:
        from google import genai as _genai
        client = _genai.Client(api_key=api_key)
        raw = client.models.generate_content(model="gemini-2.0-flash", contents=prompt).text.strip()
    except ImportError:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        raw = genai.GenerativeModel("gemini-2.0-flash-001").generate_content(prompt).text.strip()

    m = re.search(r'\{[\s\S]*\}', raw)
    data = json.loads(m.group() if m else raw)

    # ── Validate AI-extracted values against reasonable bounds ─────────
    # Prevents Gemini hallucinations from propagating false data.
    _validation_warnings = []

    # SLA windows: must be 0.5h–48h (anything outside is likely hallucinated)
    sla_w = data.get("sla_windows") or {}
    _validated_sla = {}
    for btype, entry in sla_w.items():
        if not isinstance(entry, dict) or entry.get("limit_hours") is None:
            continue
        lh = entry.get("limit_hours")
        try:
            lh = float(lh)
        except (TypeError, ValueError):
            _validation_warnings.append(f"SLA window '{btype}': non-numeric value '{lh}' — skipped")
            continue
        if lh < 0.5 or lh > 48:
            _validation_warnings.append(
                f"SLA window '{btype}': {lh}h outside valid range (0.5–48h) — skipped"
            )
            continue
        _validated_sla[btype] = {**entry, "limit_hours": lh, "source": "SOW_EXTRACTED"}
    data["sla_windows"] = _validated_sla

    # Volume: item_locations must be 1–50,000,000 (50M ceiling)
    vol = data.get("volume_by_year") or {}
    _validated_vol = {}
    for yr, entry in vol.items():
        if not isinstance(entry, dict) or entry.get("item_locations") is None:
            continue
        il = entry.get("item_locations")
        try:
            il = int(float(il))
        except (TypeError, ValueError):
            continue
        if il < 1 or il > 50_000_000:
            _validation_warnings.append(
                f"Volume '{yr}': {il:,} item-locations outside valid range — skipped"
            )
            continue
        _validated_vol[yr] = {**entry, "item_locations": il}
    data["volume_by_year"] = _validated_vol

    # Availability: must be 90–100%
    avail = data.get("availability_sla_pct")
    if avail is not None:
        try:
            avail = float(avail)
            if avail < 90 or avail > 100:
                _validation_warnings.append(
                    f"Availability SLA {avail}% outside valid range (90–100%) — removed"
                )
                data.pop("availability_sla_pct", None)
        except (TypeError, ValueError):
            data.pop("availability_sla_pct", None)

    # Annual fee: must be positive and < 100M
    fee = data.get("annual_fee")
    if fee is not None:
        try:
            fee_num = float(str(fee).replace(",", ""))
            if fee_num <= 0 or fee_num > 100_000_000:
                _validation_warnings.append(
                    f"Annual fee '{fee}' outside valid range — removed"
                )
                data.pop("annual_fee", None)
        except (TypeError, ValueError):
            data.pop("annual_fee", None)

    if _validation_warnings:
        data["_ai_validation_warnings"] = _validation_warnings

    return data


def _regex_supplement_contract(text: str, result: Dict[str, Any]) -> None:
    """
    Fill missing keys in `result` using regex patterns on raw SOW text.
    Modifies result in-place.
    """
    t = text or ""
    t_lower = t.lower()

    # SLA windows — real Blue Yonder SOW format:
    # "Batch Window – 6 hr. Daily / 8hr. Weekly"  (en-dash, hr. with period, slash separator)
    if "sla_windows" not in result:
        result["sla_windows"] = {}

    # Priority 1: the Blue Yonder "Batch Window – Xhr. Daily / Xhr. Weekly" line
    # Handle all dash variants: en-dash (–), em-dash (—), hyphen (-), colon (:)
    bw = re.search(
        r'batch\s+window\s*[–—\-:]\s*(\d+(?:\.\d+)?)\s*hr?\.?\s+daily\s*[/|]\s*(\d+(?:\.\d+)?)\s*hr?\.?\s+weekly',
        t_lower, re.DOTALL
    )
    if bw:
        if "DAILY" not in result["sla_windows"]:
            result["sla_windows"]["DAILY"]  = {"limit_hours": float(bw.group(1)), "source": "SOW_EXTRACTED"}
        if "WEEKLY" not in result["sla_windows"]:
            result["sla_windows"]["WEEKLY"] = {"limit_hours": float(bw.group(2)), "source": "SOW_EXTRACTED"}

    # Priority 2: standalone patterns
    _sla_patterns = [
        (r'(\d+(?:\.\d+)?)\s*hr?\.?\s+daily',   "DAILY"),
        (r'daily\s+(?:batch\s+)?(?:window|sla)[\s\-–:=]+(\d+(?:\.\d+)?)\s*h', "DAILY"),
        (r'(\d+(?:\.\d+)?)\s*hr?\.?\s+weekly',  "WEEKLY"),
        (r'weekly\s+(?:batch\s+)?(?:window|sla)[\s\-–:=]+(\d+(?:\.\d+)?)\s*h', "WEEKLY"),
        (r'(\d+(?:\.\d+)?)\s*hr?\.?\s+monthly', "MONTHLY"),
    ]
    for pattern, btype in _sla_patterns:
        if btype not in result["sla_windows"]:
            m = re.search(pattern, t_lower)
            if m:
                result["sla_windows"][btype] = {
                    "limit_hours": float(m.group(1)),
                    "source": "SOW_EXTRACTED",
                }

    # Volume ramp — Blue Yonder SOW format:
    # "Contract Year 1: 10,000 Item-Locations"
    # "Contract Year 2: 35,000 Item-Locations"
    if not result.get("volume_by_year"):
        # Pattern 1: explicit "Contract Year N: XX,XXX Item-Locations" rows
        cy_matches = re.findall(
            r'contract\s+year\s+(\d+)[:\s]+(\d[\d,]*)\s*item.?locations?',
            t_lower
        )
        if cy_matches:
            result["volume_by_year"] = {
                f"CY{yr}": {"item_locations": int(cnt.replace(",", "")), "uom": "Item-Locations"}
                for yr, cnt in cy_matches
            }
        else:
            # Pattern 2: any sequence of item-location numbers
            vol_match = re.findall(r'([\d,]+)\s*item.?locations?', t_lower)
            if len(vol_match) >= 2:
                nums = [int(v.replace(",", "")) for v in vol_match[:3]]
                result["volume_by_year"] = {
                    f"CY{i+1}": {"item_locations": n, "uom": "Item-Locations"}
                    for i, n in enumerate(nums)
                }

    # Max item locations
    if not result.get("max_item_locations"):
        m = re.search(r"max(?:imum)?\s+(?:item.?locations?|sku|dfu)[\s:=]+([\d,]+)", t_lower)
        if m:
            result["max_item_locations"] = int(m.group(1).replace(",", ""))

    # Availability SLA % — Blue Yonder table format: "Standard Availability  99.7%"
    if not result.get("availability_sla_pct"):
        # Match "Standard Availability  99.7%" table row (two spaces between)
        m = re.search(r'standard\s+availability\s+([\d.]+)\s*%', t_lower)
        if not m:
            m = re.search(r'availability\s+percentage\s+([\d.]+)\s*%', t_lower)
        if not m:
            m = re.search(r'availability[\s:=]+([\d.]+)\s*%', t_lower)
        if m:
            result["availability_sla_pct"] = float(m.group(1))

    # RTO / RPO — Blue Yonder table format:
    # "Standard Plus  Up to 48 Hours  4 hours"  (from DR table on page 13)
    # Also check cover page: "Disaster Recovery – Standard Plus"
    if not result.get("disaster_recovery"):
        dr_level = None
        rto_h = None
        rpo_h = None

        # Detect DR level from cover page label
        lm = re.search(r'disaster\s+recovery\s*[–\-]\s*([\w\s+]+?)(?:\n|\r|\Z)', t, re.IGNORECASE)
        if lm:
            dr_level = lm.group(1).strip()

        # RTO from "Up to XX Hours" pattern (Blue Yonder DR table)
        rto_m = re.search(r'up\s+to\s+(\d+)\s*hours?', t_lower)
        if rto_m:
            rto_h = int(rto_m.group(1))

        # RPO from "N hours" following the RTO column in the DR table
        # Table row: "Standard Plus  Up to 48 Hours  4 hours"
        rpo_m = re.search(
            r'standard\s+plus\s+up\s+to\s+\d+\s*hours?\s+(\d+)\s*hours?',
            t_lower
        )
        if not rpo_m:
            # Generic RPO pattern
            rpo_m = re.search(r'rpo[\s:=]+(\d+)\s*h(?:ours?)?', t_lower)
        if rpo_m:
            rpo_h = int(rpo_m.group(1))

        # Simple RTO/RPO keyword fallback
        if not rto_h:
            rto_kw = re.search(r'rto[\s:=]+(\d+)\s*h(?:ours?)?', t_lower)
            if rto_kw:
                rto_h = int(rto_kw.group(1))

        if dr_level or rto_h or rpo_h:
            result["disaster_recovery"] = {
                "level":     dr_level,
                "rto_hours": rto_h,
                "rpo_hours": rpo_h,
            }

    # Annual fee / currency — Blue Yonder format:
    # "Total Annual Subscription Fee (excluding taxes): €207,000"
    if not result.get("annual_fee"):
        # Match currency symbol + number (with optional space after symbol)
        m = re.search(r'[€$£]\s*(\d[\d,]+)', t)
        if m:
            result["annual_fee"] = m.group(1).replace(",", "")
            sym = m.group(0)[0]
            result.setdefault("currency", {"€": "EUR", "$": "USD", "£": "GBP"}.get(sym, "EUR"))
        else:
            # Currency code format: "AUD 207,000" / "USD 1,500,000" / "INR 50,00,000"
            m2 = re.search(
                r'(?:AUD|USD|EUR|GBP|INR|CAD|SGD|JPY|CHF|NZD)\s+(\d[\d,]+)',
                t, re.IGNORECASE
            )
            if m2:
                result["annual_fee"] = m2.group(1).replace(",", "")
                _code = m2.group(0).split()[0].upper()
                result.setdefault("currency", _code)
            else:
                # Fallback: keyword-adjacent number
                m3 = re.search(r'annual\s+subscription\s+fee[^\d]*(\d[\d,]+)', t_lower)
                if m3:
                    result["annual_fee"] = m3.group(1).replace(",", "")

    # Contract years from cover page "Initial Subscription Term: 3 Contract Years"
    if not result.get("contract_years"):
        cy_m = re.search(r'(\d+)\s+contract\s+years?', t_lower)
        if cy_m:
            result["contract_years"] = int(cy_m.group(1))

    # Customer name from cover page
    if not result.get("customer_name"):
        cn_m = re.search(r'customer\s*:\s*([^\n\r]+(?:michelin|pneumatiques|manufacture)[^\n\r]*)', t_lower)
        if cn_m:
            result["customer_name"] = cn_m.group(1).strip().title()

