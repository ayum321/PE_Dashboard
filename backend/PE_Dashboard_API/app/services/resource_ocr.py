"""
resource_ocr.py — OCR-based chart metric extraction for image-only reports.

Handles customer and SRE reports where Azure Monitor, Grafana, CloudWatch,
or Zabbix chart screenshots are pasted into Word (.docx) or PDF files
instead of data tables.
"""

import io
import os
import re
import shutil
import logging
from typing import List, Dict, Any, Optional
from PIL import Image

logger = logging.getLogger("pe_dashboard.resource_ocr")

_TESSERACT_CMD: Optional[str] = None

def _get_tesseract_cmd() -> Optional[str]:
    global _TESSERACT_CMD
    if _TESSERACT_CMD is not None:
        return _TESSERACT_CMD

    # Check PATH
    tess = shutil.which("tesseract")
    if tess:
        _TESSERACT_CMD = tess
        return _TESSERACT_CMD

    # Common Windows locations
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            _TESSERACT_CMD = c
            return _TESSERACT_CMD

    _TESSERACT_CMD = ""
    return None


def is_ocr_available() -> bool:
    """Returns True if pytesseract and tesseract binary are both available."""
    try:
        import pytesseract  # noqa: F401
        cmd = _get_tesseract_cmd()
        return bool(cmd)
    except ImportError:
        return False


def extract_servers_from_docx_images(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    """
    Extracts server names and peak metrics from chart screenshots embedded in DOCX files.
    """
    if not is_ocr_available():
        logger.info("OCR chart parser skipped: tesseract not available")
        return []

    import pytesseract
    import zipfile

    tess_cmd = _get_tesseract_cmd()
    if tess_cmd:
        pytesseract.pytesseract.tesseract_cmd = tess_cmd

    servers: Dict[str, Dict[str, Any]] = {}

    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            media_names = [
                f for f in z.namelist()
                if "word/media/" in f and f.lower().endswith((".png", ".jpg", ".jpeg"))
            ]
            if not media_names:
                return []

            logger.info("OCR scanning %d images from DOCX: %s", len(media_names), filename)

            for mname in media_names:
                img_data = z.read(mname)
                try:
                    img = Image.open(io.BytesIO(img_data))
                    text = pytesseract.image_to_string(img)
                except Exception as img_err:
                    logger.debug("Failed to OCR image %s: %s", mname, img_err)
                    continue

                # Generic host extraction from chart header or legend line
                raw_host = None

                # 1. Legend format: (<Agg>), <Host> [|:] <Value>%
                m = re.search(r"\((?:Max|Min|Avg|Average|Total)\)[,\s]+([a-z][a-z0-9_.-]{2,50})\s+[|:\s]*\d", text, re.I)
                if m and re.search(r"[a-z]", m.group(1), re.I):
                    raw_host = m.group(1).lower()

                # 2. Top header pill format: <Host>, <Metric>
                if not raw_host:
                    m = re.search(r"\b([a-z][a-z0-9_.-]{2,50}),\s*(?:Available|Percentage|VM|OS|Data|CPU|Memory|Disk|Network)", text, re.I)
                    if m and re.search(r"[a-z]", m.group(1), re.I):
                        raw_host = m.group(1).lower()

                # 3. Standard host/FQDN token fallback (ensuring it has letters and digits like hostnames do)
                if not raw_host:
                    for token in re.findall(r"\b([a-z][a-z0-9_-]{2,40}(?:\.[a-z0-9_-]+)*)\b", text, re.I):
                        t_lower = token.lower()
                        if re.search(r"[a-z]", t_lower) and re.search(r"\d", t_lower) and t_lower not in (
                            "percentage", "available", "bandwidth", "consumed", "memory",
                            "storage", "metric", "average", "maximum", "minimum",
                            "uncached", "cached", "bytes", "disk", "cpu", "total",
                        ):
                            raw_host = t_lower
                            break

                if not raw_host or not re.search(r"[a-z]", raw_host, re.I):
                    continue

                fqdn = raw_host

                if fqdn not in servers:
                    from services.resource_parser import _infer_server_type
                    stype = _infer_server_type(raw_host, context=text, doc_section_hint=filename)

                    servers[fqdn] = {
                        "host": fqdn,
                        "label": raw_host,
                        "type": stype,
                        "cpu_used": 0.0,
                        "cpu_avg": 0.0,
                        "mem_used": 0.0,
                        "mem_total_gb": 0.0,
                        "disk_used_max": 0.0,
                        "disks": {},
                        "_image_only": False,
                        "_ocr_extracted": True,
                    }

                # Parse metrics from image text
                for line in text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue

                    # CPU Max
                    if "cpu" in line.lower() and ("max" in line.lower() or "%" in line):
                        m = re.search(
                            r"Percentage\s*CPU.*?[,\s]+(?:[a-z0-9_]+)?[|:\s]+(\d+(?:[.,]\d+)?)\s*%",
                            line,
                            re.I,
                        )
                        if not m:
                            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", line)
                        if m:
                            try:
                                val = float(m.group(1).replace(",", "."))
                                if 0 < val <= 100:
                                    servers[fqdn]["cpu_used"] = max(servers[fqdn]["cpu_used"], round(val, 2))
                            except ValueError:
                                pass

                    # Available Memory Min -> Used Memory = 100 - Available
                    if "available memory" in line.lower() or ("memory" in line.lower() and "min" in line.lower()):
                        m = re.search(
                            r"Available\s*Memory.*?[,\s]+(?:[a-z0-9_]+)?[|:\s]+(\d+(?:[.,]\d+)?)\s*%",
                            line,
                            re.I,
                        )
                        if not m:
                            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", line)
                        if m:
                            try:
                                avail = float(m.group(1).replace(",", "."))
                                used = round(max(0.0, min(100.0, 100.0 - avail)), 2)
                                servers[fqdn]["mem_used"] = max(servers[fqdn]["mem_used"], used)
                            except ValueError:
                                pass

                    # Bandwidth Consumed / Disk IO %
                    if "bandwidth" in line.lower() or "disk" in line.lower():
                        m = re.search(
                            r"(?:Bandwidth|Disk).*?[,\s]+(?:[a-z0-9_]+)?[|:\s]+(\d+(?:[.,]\d+)?)\s*%",
                            line,
                            re.I,
                        )
                        if not m:
                            m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", line)
                        if m:
                            try:
                                bw = float(m.group(1).replace(",", "."))
                                if 0 < bw <= 100:
                                    servers[fqdn]["disk_used_max"] = max(
                                        servers[fqdn]["disk_used_max"], round(bw, 2)
                                    )
                            except ValueError:
                                pass

    except Exception as exc:
        logger.warning("OCR docx extraction failed for %s: %s", filename, exc)

    result = list(servers.values())
    if result:
        logger.info("OCR extracted %d servers with live metrics from %s", len(result), filename)
    return result
