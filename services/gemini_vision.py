"""
Gemini Vision image extraction for resource utilization reports.

Extracts server metric screenshots from DOCX/PDF files and sends each
image to Gemini Vision to read CPU%, Memory%, Disk% values that standard
text parsers cannot extract (image-only graphs from Zabbix / Azure Monitor).

Architecture (v2 — based on PE blueprint):
  ┌─ images_from_docx()  ─── zipfile.ZipFile  (reliable, no python-docx dep)
  ├─ images_from_pdf()   ─── PyMuPDF fitz
  ├─ extract_chart_metrics()  ─── Gemini Vision (metric_type aware prompt)
  ├─ parse_resource_file()    ─── master dispatcher
  └─ enrich_servers_with_vision()  ─── merge into existing server list

Public API:
    enrich_servers_with_vision(raw_bytes, filename, existing_servers, api_key)
        → list[dict]
    extract_images_from_docx(raw_bytes) → list[bytes]
    extract_images_from_pdf(raw_bytes)  → list[bytes]
    parse_resource_file(raw_bytes, filename) → list[dict]   (raw metric dicts)
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# ── In-memory Vision result cache (avoids re-processing identical images) ───
# Key: SHA256 hex of raw image bytes.  Cleared on process restart (single-user).
_VISION_CACHE: dict[str, list[dict] | None] = {}

logger = logging.getLogger("pe_dashboard.vision")

# ─── V4 prompt — multi-metric extraction from combined charts ──────────────
_VISION_PROMPT_V2 = """You are reading a server performance monitoring chart (Azure Monitor, Grafana, Zabbix).
The chart may show MULTIPLE metrics on the same graph with different colored lines.
Extract ALL metric lines/series visible in the chart.
Return ONLY raw JSON — no markdown fences, no explanation, no code blocks.

{
  "hostname": "<server name from chart title/legend, or null>",
  "metrics": [
    {
      "metric_type": "CPU" or "MEMORY" or "DISK" or "NETWORK" or "UNKNOWN",
      "raw_value":   <exact legend summary value as float — do NOT modify it>,
      "avg_value":   <average/mean line value as float, or null>,
      "unit":        "%",
      "raw_label":   "<exact metric label text from the legend>"
    }
  ]
}

Critical rules:
- Extract EVERY metric line visible — do NOT skip any, even if value is 0
- Return ALL 3 metrics if 3 lines are visible — never omit the 0% IOPS line
- Read the EXACT summary value from the legend at the bottom (e.g. "| 41%", "| 99.2700%")
- raw_value = the EXACT number shown in the legend — do NOT invert or transform it
- raw_label = copy the full metric label text (e.g. "Available Memory Percentage (Min)")
- metric_type classification by label:
    "Percentage CPU" or any "CPU" label → "CPU"
    "Available Memory" or any "Memory" label → "MEMORY"
    "VM Uncached IOPS" or "IOPS" label → "DISK"
    "Disk Usage" or "Disk" label → "DISK"
    "Network" label → "NETWORK"
- IMPORTANT: Do NOT apply any inversion — return the raw number exactly as shown
    "Available Memory Percentage (Min) | 41%" → raw_value=41.0 (NOT 59)
    "Percentage CPU (Max) | 87.15%" → raw_value=87.15
    "VM Uncached IOPS Consumed Percentage (Max) | 0%" → raw_value=0.0
- avg_value = average/mean line if a separate avg line is visible, else null
- hostname = short server name (e.g. "tsbc451502041") — null if not visible
- If a value cannot be read clearly, set it to null — NEVER guess
- Return ONLY the JSON object, nothing else"""

# ─── Model waterfall ─────────────────────────────────────────────────────────
# Ordered by: speed · reliability · vision capability
# Lite / non-thinking models are tried first — they have no hidden thinking
# token budget that can truncate the JSON output.
_MODELS_NEW = [
    "gemini-2.5-flash-lite",   # fastest, no thinking tokens
    "gemini-2.0-flash-lite",   # reliable, cheap
    "gemini-2.5-flash",        # thinking model — thinking_budget=0 enforced
    "gemini-2.0-flash",        # fallback
]

# Thinking models — when used, we must set thinking_budget=0 so output
# tokens are not consumed by reasoning chains (which would truncate JSON).
_THINKING_MODELS = {"gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-pro-preview"}

_MODELS_LEGACY = [
    "gemini-1.5-flash-latest",
    "gemini-1.5-pro-latest",
]


# ── Image extraction — DOCX ──────────────────────────────────────────────────

def extract_images_from_docx(raw_bytes: bytes) -> list[bytes]:
    """
    Extract all PNG/JPG images from a DOCX file using direct ZIP access.

    DOCX is a ZIP archive.  Images live under word/media/*.png|jpg|jpeg|gif|bmp.
    This approach is more reliable than python-docx relationship walking because:
      - It works even when alt-text is "AI-generated content" (no text metadata)
      - Requires only stdlib `zipfile`, no extra dependency
      - Finds ALL images regardless of how they are embedded
    """
    images: list[bytes] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
            for name in z.namelist():
                if "word/media/" in name and name.lower().rsplit(".", 1)[-1] in (
                    "png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp"
                ):
                    try:
                        images.append(z.read(name))
                    except Exception as exc:
                        logger.debug("vision: skip %s — %s", name, exc)
    except Exception as exc:
        logger.warning("vision: DOCX ZIP extraction failed — %s", exc)
    logger.info("vision: extracted %d image(s) from DOCX", len(images))
    return images


def extract_images_from_pdf(raw_bytes: bytes) -> list[bytes]:
    """Extract all embedded images from a PDF via PyMuPDF."""
    images: list[bytes] = []
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    base_image = doc.extract_image(xref)
                    images.append(base_image["image"])
                except Exception as exc:
                    logger.debug("vision: skip PDF xref %s — %s", xref, exc)
        doc.close()
    except ImportError:
        logger.warning("vision: PyMuPDF not installed — PDF image extraction unavailable")
    except Exception as exc:
        logger.warning("vision: PDF image extraction failed — %s", exc)
    logger.info("vision: extracted %d image(s) from PDF", len(images))
    return images


# ── JSON cleanup ─────────────────────────────────────────────────────────────

def _clean_json(text: str) -> str:
    """Strip markdown code fences and stray whitespace."""
    text = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    return text.replace("```", "").strip()


# ── Gemini API call ───────────────────────────────────────────────────────────

def _call_gemini_new_sdk(image_bytes: bytes, api_key: str) -> str | None:
    """Try the new google.genai SDK with model waterfall.

    Key fix: thinking models (gemini-2.5-*) use hidden reasoning tokens that
    consume the output budget and truncate the JSON response.  We disable
    thinking via ``thinking_config={"thinking_budget": 0}`` for those models.
    """
    try:
        from google import genai as _genai
        from google.genai import types as _types

        client = _genai.Client(api_key=api_key)
        for model_name in _MODELS_NEW:
            try:
                cfg: dict[str, Any] = {"max_output_tokens": 512, "temperature": 0.0}
                # Disable thinking tokens on reasoning models — they eat output budget
                base = model_name.split("-preview")[0]
                if any(t in base for t in ("2.5", "3.", "3-")):
                    cfg["thinking_config"] = {"thinking_budget": 0}

                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        _types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                        _VISION_PROMPT_V2,
                    ],
                    config=cfg,
                )
                text = (response.text or "").strip()
                if text and len(text) > 5:   # guard against single-char stub responses
                    logger.debug("vision: model=%s returned %d chars", model_name, len(text))
                    return text
                logger.debug("vision: model=%s — empty/stub response, trying next", model_name)
            except Exception as exc:
                logger.debug("vision: model %s failed — %s", model_name, exc)
                continue
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("vision: new SDK init failed — %s", exc)
    return None


def _call_gemini_legacy_sdk(image_bytes: bytes, api_key: str) -> str | None:
    """Fallback to legacy google.generativeai SDK."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        for model_name in _MODELS_LEGACY:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(
                    [_VISION_PROMPT_V2, {"mime_type": "image/png", "data": image_bytes}],
                    generation_config={"max_output_tokens": 400, "temperature": 0.0},
                )
                text = (response.text or "").strip()
                if text:
                    return text
            except Exception as exc:
                logger.debug("vision: legacy model %s failed — %s", model_name, exc)
                continue
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("vision: legacy SDK failed — %s", exc)
    return None


# ── Parse Gemini JSON response ────────────────────────────────────────────────

def _parse_vision_response(raw_text: str) -> list[dict]:
    """Parse Gemini vision JSON response — handles both multi-metric and legacy single-metric formats.
    Always returns a list of metric dicts (may be empty)."""
    cleaned = _clean_json(raw_text)

    # Strategy 1: direct parse
    parsed = None
    try:
        parsed = json.loads(cleaned)
    except Exception:
        pass

    # Strategy 2: find outermost {...} block (handles nested arrays/objects)
    if parsed is None:
        m = re.search(r'\{(?:[^{}]|\{[^{}]*\}|\[(?:[^\[\]]|\{[^{}]*\})*\])*\}', cleaned, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except Exception:
                pass

    # Strategy 3: relaxed — first {...} block
    if parsed is None:
        m = re.search(r'\{[^{}]*\}', cleaned, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except Exception:
                pass

    if not parsed or not isinstance(parsed, dict):
        logger.debug("vision: could not parse JSON from: %s", raw_text[:200])
        return []

    def _fv(d, *keys):
        for k in keys:
            v = d.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    hostname = str(parsed.get("hostname") or parsed.get("host") or "").strip() or None

    def _needs_inversion(label: str) -> bool:
        """Check if a metric label indicates an 'available/free/idle' value that
        must be inverted to get the 'used' percentage."""
        if not label:
            return False
        ll = label.lower()
        return any(k in ll for k in ("available", "free", "idle"))

    def _apply_inversion(val: float | None, label: str) -> float | None:
        """Invert 'available' metrics: used% = 100 - available%."""
        if val is None:
            return None
        if _needs_inversion(label):
            inverted = round(100.0 - val, 1)
            logger.info("vision: inversion applied — raw=%.1f label=%r → used=%.1f", val, label, inverted)
            return inverted
        return val

    # ── Multi-metric format (V4 prompt) ──────────────────────────────────
    metrics_raw = parsed.get("metrics")
    if isinstance(metrics_raw, list) and metrics_raw:
        results = []
        for m in metrics_raw:
            if not isinstance(m, dict):
                continue
            mt = str(m.get("metric_type") or "UNKNOWN").upper()
            # Accept both "raw_value" (V4) and "max_value" (legacy/fallback)
            mv = _fv(m, "raw_value", "max_value", "value")
            av = _fv(m, "avg_value")
            unit = str(m.get("unit") or "%")
            raw_label = str(m.get("raw_label") or m.get("label") or "")

            # Apply Python-side inversion for available/free/idle metrics
            mv = _apply_inversion(mv, raw_label)
            av = _apply_inversion(av, raw_label)

            if mv is None:
                continue
            result = {
                "metric_type": mt,
                "max_value":   round(mv, 1),
                "avg_value":   round(av, 1) if av is not None else None,
                "unit":        unit,
                "hostname":    hostname,
            }
            logger.info("vision: multi-metric → type=%s max=%s avg=%s host=%s label=%r",
                        mt, mv, av, hostname, raw_label)
            results.append(result)
        if results:
            return results

    # ── Legacy single-metric format (fallback) ───────────────────────────
    metric_type = str(parsed.get("metric_type") or "UNKNOWN").upper()
    max_val     = _fv(parsed, "raw_value", "max_value", "max_cpu", "cpu_max", "max_mem", "mem_max",
                      "max_disk", "disk_max", "value")
    avg_val     = _fv(parsed, "avg_value", "avg_cpu", "cpu_avg", "avg_mem", "mem_avg")
    unit        = str(parsed.get("unit") or "%")
    raw_label   = str(parsed.get("raw_label") or parsed.get("label") or "")
    if not hostname:
        hostname = str(parsed.get("hostname") or parsed.get("host") or "").strip() or None

    # Apply Python-side inversion for legacy format too
    max_val = _apply_inversion(max_val, raw_label)
    avg_val = _apply_inversion(avg_val, raw_label)

    # Convert raw GB → % is impossible without total — skip and log
    if unit in ("GB", "MB") and max_val is not None:
        logger.debug(
            "vision: raw %s value (%.1f %s) cannot be converted to %%  — keeping as-is",
            metric_type, max_val, unit,
        )

    if max_val is None:
        return []

    result = {
        "metric_type": metric_type,
        "max_value":   round(max_val, 1),
        "avg_value":   round(avg_val, 1) if avg_val is not None else None,
        "unit":        unit,
        "hostname":    hostname,
    }
    logger.info(
        "vision: parsed → type=%s max=%s avg=%s host=%s",
        metric_type, max_val, avg_val, hostname,
    )
    return [result]


# ── Single-image orchestrator ─────────────────────────────────────────────────

def extract_chart_metrics(image_bytes: bytes, api_key: str) -> list[dict]:
    """
    Send one chart image to Gemini Vision.
    Returns list of metric dicts (one per metric line in the chart).
    Combined charts (CPU + Memory + IOPS on one graph) return multiple entries.
    Uses an in-process SHA256 cache to skip re-processing identical images.
    """
    from services import pe_config
    if not pe_config.AI_ENABLED:
        return []
    # Cache check
    img_hash = hashlib.sha256(image_bytes).hexdigest()
    if img_hash in _VISION_CACHE:
        logger.debug("vision: cache hit %s", img_hash[:12])
        cached = _VISION_CACHE[img_hash]
        return cached if cached else []

    # Pre-process: resize to ≤1024px (reduces token cost, keeps chart readable)
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(image_bytes))
        img.thumbnail((1024, 1024), PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_data = buf.getvalue()
    except Exception:
        img_data = image_bytes

    raw_text = _call_gemini_new_sdk(img_data, api_key)
    if not raw_text:
        raw_text = _call_gemini_legacy_sdk(img_data, api_key)

    if not raw_text:
        logger.warning("vision: all Gemini models failed for this image")
        _VISION_CACHE[img_hash] = []
        return []

    results = _parse_vision_response(raw_text)
    _VISION_CACHE[img_hash] = results
    return results


# ── Section-aware DOCX image extraction ──────────────────────────────────────

def extract_section_images_from_docx(raw_bytes: bytes) -> list[tuple[str, list[bytes]]]:
    """
    Walk DOCX body XML in document order and return:
        [(section_label, [img_bytes, ...]), ...]

    Section labels are detected as short text paragraphs (≤10 words) that
    appear immediately before one or more image-only paragraphs.  This covers
    both Heading-style and plain Normal-style labels (e.g. "Application Server 1 :").
    """
    try:
        from docx import Document
        from docx.oxml.ns import qn as _qn

        doc = Document(io.BytesIO(raw_bytes))
        body = doc.element.body

        # Build rId → image bytes map via relationship parts (avoids media/
        # name assumptions across different DOCX generators)
        rid_to_blob: dict[str, bytes] = {}
        for rId, rel in doc.part.rels.items():
            try:
                part = doc.part.related_parts.get(rId)
                if part and hasattr(part, "blob"):
                    rid_to_blob[rId] = part.blob
            except Exception:
                pass

        sections: list[tuple[str, list[bytes]]] = []
        cur_label: str = ""
        cur_imgs: list[bytes] = []

        def _flush() -> None:
            if cur_label:
                sections.append((cur_label, cur_imgs[:]))

        for child in body.iterchildren():
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local != "p":
                continue

            from docx.text.paragraph import Paragraph
            para = Paragraph(child, doc)
            text = para.text.strip()

            # Collect ALL embedded image rIds from this paragraph (blip embeds)
            blips = child.findall(".//" + _qn("a:blip"))
            img_rids = [b.get(_qn("r:embed")) for b in blips if b.get(_qn("r:embed"))]
            img_bytes_list = [rid_to_blob[r] for r in img_rids if r in rid_to_blob]

            if img_bytes_list:
                # Image paragraph — accumulate under current section
                cur_imgs.extend(img_bytes_list)
            elif text:
                # Text paragraph — start a new section
                _flush()
                cur_label = text.rstrip(": ").strip()
                cur_imgs = []

        _flush()
        # Keep image-less parent sections (e.g. "Application Server 1") so
        # the enrichment loop can map metric-label sub-sections to their
        # parent server.  Only drop entries that are both empty AND have
        # no useful label.
        sections = [(lbl, imgs) for lbl, imgs in sections if (lbl or imgs)]
        logger.info(
            "extract_section_images_from_docx: %d sections, %d total images",
            len(sections),
            sum(len(i) for _, i in sections),
        )
        return sections

    except Exception as exc:
        logger.warning("extract_section_images_from_docx failed (%s) — falling back", exc)
        # Graceful fallback: return all images under a single unnamed section
        all_imgs = extract_images_from_docx(raw_bytes)
        return [("", all_imgs)] if all_imgs else []


def _is_chart_image(img_bytes: bytes) -> bool:
    """Return True if image passes size + aspect-ratio chart heuristics."""
    if len(img_bytes) < 20_000:
        return False
    try:
        from PIL import Image as _PILCheck
        img = _PILCheck.open(io.BytesIO(img_bytes))
        w, h = img.size
        if h > 0 and not (0.25 <= w / h <= 7.0):
            return False
    except Exception:
        pass
    return True


# ── Master DOCX/PDF parser — parallel processing ──────────────────────────────

def parse_resource_file(raw_bytes: bytes, filename: str, api_key: str,
                        max_images: int = 40, max_workers: int = 8,
                        delay_sec: float = 0.0) -> list[dict]:
    """
    Extract all chart images from a DOCX or PDF and run Gemini Vision on each
    using a ThreadPoolExecutor for parallel API calls.

    Returns a list of metric dicts (one per chart image processed):
        [{"metric_type": "CPU", "max_value": 85.3, "avg_value": 45.0,
          "unit": "%", "hostname": "tsbc1234", "chart_index": 0,
          "section_label": "Application Server 1"}, ...]
    """
    ext = (filename or "").lower().rsplit(".", 1)[-1]

    if ext == "docx":
        images = extract_images_from_docx(raw_bytes)
    elif ext == "pdf":
        images = extract_images_from_pdf(raw_bytes)
    else:
        logger.warning("vision: unsupported file extension '%s'", ext)
        return []

    if not images:
        logger.warning("vision: no images found in %s", filename)
        return []

    # Filter chart images
    valid: list[tuple[int, bytes]] = [
        (i, img) for i, img in enumerate(images[:max_images])
        if _is_chart_image(img)
    ]
    logger.info("vision: %d/%d images passed chart filter", len(valid), len(images))

    results: list[dict] = []

    def _process(i: int, img_bytes: bytes) -> list[dict]:
        metric_list = extract_chart_metrics(img_bytes, api_key)
        for m in metric_list:
            m["chart_index"] = i
        return metric_list

    processed = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(valid) or 1)) as pool:
        future_map = {pool.submit(_process, i, img): i for i, img in valid}
        for future in as_completed(future_map):
            processed += 1
            try:
                m_list = future.result()
                if m_list:
                    results.extend(m_list)
                else:
                    logger.debug("vision: image %d returned no usable data", future_map[future])
            except Exception as exc:
                logger.warning("vision: image %d raised %s", future_map[future], exc)

    # Sort by chart_index so callers get deterministic order
    results.sort(key=lambda x: x.get("chart_index", 0))

    logger.info("vision: processed %d/%d images → %d usable metrics",
                processed, len(images), len(results))
    return results


# ── Server DataFrame builder ──────────────────────────────────────────────────

def build_server_df(raw_metrics: list[dict], server_names: list[str] | None = None):
    """
    Group raw metric dicts into server rows.
    Assumes charts appear in order: CPU → MEMORY → DISK per server (3 per server).
    Falls back to metric_type label if order is wrong.

    Returns a pandas DataFrame with columns:
        server, cpu_max, cpu_avg, mem_max, mem_avg, disk_max, disk_avg
    """
    import pandas as pd

    rows: list[dict] = []
    groups = [raw_metrics[i:i + 3] for i in range(0, len(raw_metrics), 3)]

    for idx, grp in enumerate(groups):
        row: dict = {
            "server":   (server_names[idx] if server_names and idx < len(server_names)
                         else f"Server_{idx + 1}"),
            "cpu_max":  None, "cpu_avg":  None,
            "mem_max":  None, "mem_avg":  None,
            "disk_max": None, "disk_avg": None,
        }
        for m in grp:
            mt = (m.get("metric_type") or "UNKNOWN").upper()
            mv = m.get("max_value")
            av = m.get("avg_value")
            if mt == "CPU":
                row["cpu_max"],  row["cpu_avg"]  = mv, av
            elif mt in ("MEMORY", "MEM"):
                row["mem_max"],  row["mem_avg"]  = mv, av
            elif mt == "DISK":
                row["disk_max"], row["disk_avg"] = mv, av
            else:
                # Fallback: assign by position within the group
                pos = grp.index(m)
                key_pairs = [("cpu_max", "cpu_avg"), ("mem_max", "mem_avg"), ("disk_max", "disk_avg")]
                if pos < 3:
                    row[key_pairs[pos][0]] = mv
                    row[key_pairs[pos][1]] = av

        # Prefer hostname from vision if available and we have no name yet
        for m in grp:
            h = m.get("hostname")
            if h and row["server"].startswith("Server_"):
                row["server"] = h
                break

        rows.append(row)

    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["server", "cpu_max", "cpu_avg", "mem_max", "mem_avg", "disk_max", "disk_avg"]
    )
    return df


# ── Hostname matching ─────────────────────────────────────────────────────────

def _normalize_host(h: str) -> str:
    return h.split(".")[0].lower().strip()


def _match_server(vision_hostname: str, servers: list[dict]) -> int | None:
    """Match a Vision-returned hostname against existing servers.

    Checks both ``host`` (FQDN/short) and ``label`` (human-readable heading
    text from Strategy D) so Leonardo/Distell section labels like
    'Application Server 1' are also matched.
    """
    if not vision_hostname:
        return None
    vh = _normalize_host(vision_hostname)
    for i, s in enumerate(servers):
        # Match against short hostname
        sh = _normalize_host(s.get("host", ""))
        if sh and (sh == vh or sh in vh or vh in sh):
            return i
        # Match against display label (Strategy D section heading)
        lbl = _normalize_host(s.get("label", ""))
        if lbl and (lbl == vh or lbl in vh or vh in lbl):
            return i
    return None


# ── Legacy enrich_servers_with_vision (called by upload.py) ─────────────────

def enrich_servers_with_vision(
    raw_bytes: bytes,
    filename: str,
    existing_servers: list[dict[str, Any]],
    api_key: str,
    max_images: int = 40,
    delay_sec: float = 0.0,   # no delay needed — parallel processing
    max_workers: int = 8,
) -> list[dict[str, Any]]:
    """
    Extract images from DOCX/PDF, run Gemini Vision in parallel, and merge
    metrics into existing_servers.

    For DOCX files the section-aware extractor ``extract_section_images_from_docx``
    is used so each server's charts are grouped by their heading label eliminating
    fragile position-based index arithmetic.
    """
    from services import pe_config
    if not pe_config.AI_ENABLED:
        logger.info("vision: AI disabled (pe_config.AI_ENABLED=False) — skipping enrichment")
        return existing_servers
    if not api_key:
        from services.config_store import get_gemini_key
        api_key = get_gemini_key()

    if not api_key:
        logger.warning("vision: no API key — skipping enrichment")
        return existing_servers

    enriched = [dict(s) for s in existing_servers]
    ext = (filename or "").lower().rsplit(".", 1)[-1]

    # ── DOCX: section-aware path ───────────────────────────────────────────
    if ext == "docx":
        sections = extract_section_images_from_docx(raw_bytes)
        if sections:
            return _enrich_docx_sections(sections, enriched, api_key, max_workers)

    # ── PDF / fallback: flat image list path ──────────────────────────────
    raw_metrics = parse_resource_file(
        raw_bytes, filename, api_key,
        max_images=max_images, max_workers=max_workers,
    )

    if not raw_metrics:
        logger.warning("vision: parse_resource_file returned 0 metrics")
        return existing_servers

    _merge_flat_metrics(raw_metrics, enriched)
    return enriched


def _enrich_docx_sections(
    sections: list[tuple[str, list[bytes]]],
    enriched: list[dict[str, Any]],
    api_key: str,
    max_workers: int,
) -> list[dict[str, Any]]:
    """
    Process each DOCX section in parallel.

    For each (section_label, [img_bytes, ...]) we:
    1. Run Vision on all images concurrently.
    2. Find the matching server by fuzzy label match.
    3. Assign CPU / MEM / DISK values to that server.
    If no existing server matches the label, a new server entry is created.
    """
    try:
        from services.resource_parser import _infer_server_type
    except Exception:
        def _infer_server_type(h, ctx="", hint=""):  # type: ignore[override]
            return "DB" if any(k in h.lower() for k in ["db", "sql", "ora"]) else "APP"

    def _norm(s: str) -> str:
        return re.sub(r"[\s_\-]+", " ", s.strip().rstrip(": ").lower())

    # Block-list of metric/chart labels that must NEVER become a server name.
    # The DOCX section heading is sometimes a chart label (e.g.
    # "Available Memory", "VM Uncached Bandwidth", "CPU Last 15 Days Report")
    # — without this filter the vision pipeline invents phantom servers.
    _LABEL_BLOCK_EXACT = {
        "available memory", "free memory", "memory", "mem", "ram",
        "cpu", "processor", "cpu usage", "cpu utilization",
        "ram utilization", "memory utilization",
        "disk", "disk usage", "disk utilization", "storage",
        "network", "iops", "bandwidth", "throughput",
        "vm uncached bandwidth", "uncached bandwidth", "vm uncached",
        "cpu last 15 days report", "memory last 15 days report",
        "disk last 15 days report", "last 15 days", "last 7 days",
        "summary", "overview", "chart", "graph", "report",
        "dfu", "sku", "orders",
    }
    _LABEL_BLOCK_PREFIX = (
        "available memory", "free memory", "vm uncached", "cpu last",
        "memory last", "disk last", "ram utilization",
        "cpu utilization", "memory utilization", "disk utilization",
    )

    def _looks_like_real_host(name: str) -> bool:
        """Reject pure metric-label sections (exact / prefix match against
        the metric blocklist).  Document titles and 'Application Server N'
        style headings pass through."""
        n = _norm(name or "")
        if not n or len(n) < 3:
            return False
        if n in _LABEL_BLOCK_EXACT:
            return False
        for bad in _LABEL_BLOCK_PREFIX:
            if n.startswith(bad):
                return False
        return True

    def _clamp01(v) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f != f:                       # NaN guard
            return None
        if f < 0 or f > 100:             # Reject impossible percentages
            return None
        return round(f, 1)

    def _find_server_by_label(label: str) -> int | None:
        nl = _norm(label)
        for i, s in enumerate(enriched):
            if _norm(s.get("host", "")) == nl:
                return i
            if _norm(s.get("label", "")) == nl:
                return i
            # Substring match (handles minor whitespace differences)
            sl = _norm(s.get("label", "") or s.get("host", ""))
            if sl and (sl in nl or nl in sl):
                return i
        return None

    def _process_section(label: str, imgs: list[bytes]) -> tuple[str, list[dict]]:
        metrics: list[dict] = []
        valid_imgs = [img for img in imgs if _is_chart_image(img)]
        with ThreadPoolExecutor(max_workers=min(max_workers, len(valid_imgs) or 1)) as pool:
            futures = [pool.submit(extract_chart_metrics, img, api_key) for img in valid_imgs]
            for f in as_completed(futures):
                try:
                    m_list = f.result()
                    if m_list:
                        metrics.extend(m_list)
                except Exception as exc:
                    logger.debug("vision: section %r image failed: %s", label, exc)
        return label, metrics

    # Process all sections in parallel but consume results IN DOCUMENT ORDER
    # so we can attach metric-label sub-sections (e.g. "Available Memory")
    # to the most recent valid parent server section.
    last_parent_idx: int | None = None

    with ThreadPoolExecutor(max_workers=min(max_workers, len(sections))) as outer:
        ordered_futures = [outer.submit(_process_section, lbl, imgs)
                           for lbl, imgs in sections]

        for future in ordered_futures:
            try:
                label, metrics = future.result()
            except Exception as exc:
                logger.warning("vision: section future raised %s", exc)
                continue

            # Always try to match the section label against an existing
            # server so empty parent sections still update last_parent_idx.
            existing_idx = _find_server_by_label(label)

            if not metrics:
                if existing_idx is not None:
                    last_parent_idx = existing_idx
                elif _looks_like_real_host(label):
                    # Empty parent section with a real-looking host — record
                    # it as the parent for any sub-section metrics that follow.
                    new_s: dict[str, Any] = {
                        "host":          re.sub(r"\s+", "_", label),
                        "label":         label,
                        "type":          _infer_server_type(label),
                        "cpu_used":      0.0, "cpu_avg": 0.0,
                        "mem_used":      0.0, "mem_total_gb": 0.0,
                        "disk_used_max": 0.0, "disks": {},
                        "_image_only":   True,
                        "_vision_enriched": False,
                    }
                    enriched.append(new_s)
                    last_parent_idx = len(enriched) - 1
                continue

            idx = existing_idx

            if idx is None:
                if _looks_like_real_host(label):
                    # New server entry for this real-looking section label
                    new_s = {
                        "host":          re.sub(r"\s+", "_", label),
                        "label":         label,
                        "type":          _infer_server_type(label),
                        "cpu_used":      0.0, "cpu_avg": 0.0,
                        "mem_used":      0.0, "mem_total_gb": 0.0,
                        "disk_used_max": 0.0, "disks": {},
                        "_image_only":   True,
                        "_vision_enriched": False,
                    }
                    enriched.append(new_s)
                    idx = len(enriched) - 1
                    last_parent_idx = idx
                elif last_parent_idx is not None:
                    # Phantom metric-label sub-section — attach to last parent
                    idx = last_parent_idx
                    logger.info(
                        "vision: attaching sub-section %r metrics to parent %r",
                        label, enriched[idx].get("host", "?"),
                    )
                else:
                    logger.info(
                        "vision: dropping orphan section %r (no parent server yet)",
                        label,
                    )
                    continue
            else:
                last_parent_idx = idx

            srv = enriched[idx]
            for m in metrics:
                mt = (m.get("metric_type") or "UNKNOWN").upper()
                mv = _clamp01(m.get("max_value"))
                av = _clamp01(m.get("avg_value"))
                if mv is None:
                    continue
                if mt == "CPU":
                    if float(srv.get("cpu_used", 0) or 0) == 0:
                        srv["cpu_used"] = mv
                    if av is not None and float(srv.get("cpu_avg", 0) or 0) == 0:
                        srv["cpu_avg"] = av
                elif mt in ("MEMORY", "MEM"):
                    if float(srv.get("mem_used", 0) or 0) == 0:
                        srv["mem_used"] = mv
                elif mt == "DISK":
                    if float(srv.get("disk_used_max", 0) or 0) == 0:
                        srv["disk_used_max"] = mv
                srv["_image_only"]    = False
                srv["_vision_enriched"] = True
                logger.info("vision: section=%r  %s=%.1f%%", label, mt, mv)

    return enriched


def _is_real_hostname_for_merge(name: str) -> bool:
    """Allow Vision-reported hostnames to seed a new server only when they
    look like real machine names (letters + digits, not metric labels)."""
    if not name:
        return False
    base = name.split(".")[0].strip().lower()
    if not base or len(base) < 3:
        return False
    blocked = {
        "available", "memory", "cpu", "disk", "network", "server", "host",
        "node", "unknown", "none", "n/a", "na", "summary", "chart", "report",
    }
    if base in blocked:
        return False
    if not re.search(r"\d", base):
        return False
    return bool(re.match(r"^[a-z][a-z0-9._-]+$", base))


def _merge_flat_metrics(raw_metrics: list[dict], enriched: list[dict[str, Any]]) -> None:
    """Merge a flat list of Vision metric dicts into enriched servers (PDF / fallback)."""
    def _clamp(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f != f or f < 0 or f > 100:
            return None
        return round(f, 1)

    for m in raw_metrics:
        vh = m.get("hostname") or ""
        mv = _clamp(m.get("max_value"))
        av = _clamp(m.get("avg_value"))
        mt = (m.get("metric_type") or "UNKNOWN").upper()

        if mv is None:
            continue

        idx = _match_server(vh, enriched) if vh else None

        if idx is not None:
            s = enriched[idx]
            if mt == "CPU" and float(s.get("cpu_used", 0) or 0) == 0:
                s["cpu_used"] = mv
                if av is not None and float(s.get("cpu_avg", 0) or 0) == 0:
                    s["cpu_avg"] = av
            elif mt in ("MEMORY", "MEM") and float(s.get("mem_used", 0) or 0) == 0:
                s["mem_used"] = mv
            elif mt == "DISK" and float(s.get("disk_used_max", 0) or 0) == 0:
                s["disk_used_max"] = mv
            s["image_only"]       = False
            s["_vision_enriched"] = True
        elif vh and _is_real_hostname_for_merge(vh):
            try:
                from services.resource_parser import _infer_server_type
                stype = _infer_server_type(vh)
            except Exception:
                stype = "APP"
            enriched.append({
                "host": vh, "type": stype,
                "cpu_used":      mv if mt == "CPU" else 0.0,
                "cpu_avg":       av if (mt == "CPU" and av is not None) else 0.0,
                "mem_used":      mv if mt in ("MEMORY", "MEM") else 0.0,
                "mem_total_gb":  0.0,
                "disk_used_max": mv if mt == "DISK" else 0.0,
                "disks": {}, "health_score": 0.0,
                "image_only": False, "_vision_enriched": True,
            })
        else:
            # Flat position-based fallback: n_metrics / n_servers charts per server
            chart_idx  = m.get("chart_index", 0)
            n_srv      = max(len(enriched), 1)
            n_metrics  = max(len(raw_metrics), 1)
            charts_per = max(1, n_metrics // n_srv)
            pos        = chart_idx // charts_per
            target     = enriched[pos] if pos < len(enriched) else None
            if target is not None:
                fld = {"CPU": "cpu_used", "MEMORY": "mem_used", "MEM": "mem_used",
                       "DISK": "disk_used_max"}.get(mt)
                if fld and float(target.get(fld, 0) or 0) == 0:
                    target[fld] = mv
                    target["_vision_enriched"] = True
