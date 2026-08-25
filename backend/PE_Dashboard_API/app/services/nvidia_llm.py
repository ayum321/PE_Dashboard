"""
NVIDIA NIM LLM fallback for resource utilization extraction.

Used when the deterministic regex parser in `resource_parser_generic` returns
zero servers or zero metrics from a Zabbix / Azure Monitor / Grafana text
report.  The LLM is asked to read the raw extracted text and return a
structured JSON list of servers with their CPU / Memory / Disk usage so the
Resource Review tab can render correct analysis.

Public API:
    extract_servers_from_text(text, api_key=None, max_chars=60000) -> list[dict]
        Returns server dicts shaped like the regex parser output:
            { host, type, cpu_used, cpu_avg, mem_used, mem_total_gb,
              disk_used_max, disks, _image_only, _llm_extracted }

The endpoint used is NVIDIA NIM's OpenAI-compatible chat completions:
    POST https://integrate.api.nvidia.com/v1/chat/completions
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pe_dashboard.nvidia_llm")

_NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# Model waterfall — first that responds wins.  Instruct/chat models with
# strong JSON-mode behaviour are preferred.
_MODELS = [
    "meta/llama-3.3-70b-instruct",
    "nvidia/llama-3.1-nemotron-70b-instruct",
]

_SYSTEM_PROMPT = (
    "You are a server performance analyst. You read raw text exported from "
    "Zabbix, Azure Monitor, Grafana, or similar infrastructure monitoring "
    "tools and return ONLY a strict JSON object listing every server you can "
    "identify with its CPU%, Memory% and Disk% utilization. "
    "Never invent values. If a metric is missing, return 0."
)

_USER_PROMPT_TEMPLATE = """Extract every server's resource utilization from the report text below.

CRITICAL RULES:
- Output ONLY a JSON object — no prose, no markdown fences, no commentary.
- Schema:
  {{
    "servers": [
      {{
        "host":          "<short hostname, e.g. tsbb191525041>",
        "type":          "DB" or "APP" or "SRE",
        "cpu_used":      <number 0-100, percent USED — invert idle/free if needed>,
        "cpu_avg":       <number 0-100, average CPU used, 0 if unknown>,
        "mem_used":      <number 0-100, percent USED — invert if Available/Free is given>,
        "mem_total_gb":  <number, 0 if unknown>,
        "disk_used_max": <number 0-100, max disk USED across all mounts>,
        "disks":         {{ "<mount>": <percent_used>, ... }}
      }}
    ]
  }}

INVERSION RULES (do this BEFORE writing the value):
- "CPU idle 92%"           -> cpu_used = 8
- "Available memory 30%"   -> mem_used = 70
- "Free disk on / : 45%"   -> disks["/"] = 55
- "Memory utilization 65%" -> mem_used = 65   (already used, no inversion)

CLASSIFICATION:
- type = "DB" if hostname contains db/sql/ora/orcl/rac
- type = "SRE" if it contains batch/ctrlm/ctlm/sched
- type = "APP" otherwise

Hostnames must be real machine names (letters + at least one digit, e.g.
'tsbb191525041', 'prod-db-01').  Do NOT return generic words like
'Server', 'Database', 'CPU' as a hostname.

REPORT TEXT (truncated if very long):
{text}
"""


def _build_payload(model: str, text: str) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": _USER_PROMPT_TEMPLATE.format(text=text)},
        ],
        "temperature": 0.0,
        "top_p": 0.9,
        "max_tokens": 4096,
        "stream": False,
        # Many NIM models accept this hint and constrain output to JSON.
        "response_format": {"type": "json_object"},
    }


def _post(model: str, text: str, api_key: str, timeout: int = 60) -> Optional[str]:
    """Single POST to NVIDIA NIM. Returns the assistant message content or None."""
    body = json.dumps(_build_payload(model, text)).encode("utf-8")
    req = urllib.request.Request(
        _NVIDIA_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        choices = data.get("choices") or []
        if not choices:
            logger.warning("nvidia_llm: %s returned empty choices", model)
            return None
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            # Some NIM responses pack content as a list of parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return (content or "").strip() or None
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = ""
        logger.warning("nvidia_llm: HTTP %s for %s — %s", exc.code, model, err_body)
        return None
    except Exception as exc:
        logger.warning("nvidia_llm: %s call failed — %s", model, exc)
        return None


def _strip_fences(s: str) -> str:
    s = re.sub(r"```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    return s.replace("```", "").strip()


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # Fall back: find outermost { ... }
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            return None
    return None


def _clamp_pct(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:        # NaN
        return 0.0
    return max(0.0, min(100.0, round(f, 2)))


def _infer_type(host: str) -> str:
    h = (host or "").lower()
    if any(k in h for k in ("db", "sql", "ora", "orcl", "rac")):
        return "DB"
    if any(k in h for k in ("batch", "ctrlm", "ctlm", "sched")):
        return "SRE"
    return "APP"


_HOSTNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{1,}$", re.IGNORECASE)
_BLOCKED_HOSTS = {
    "server", "servers", "database", "host", "node", "cluster", "cpu", "memory",
    "disk", "system", "report", "summary", "unknown", "n/a", "na",
}


def _is_real_host(name: str) -> bool:
    if not name:
        return False
    base = name.split(".")[0].strip().lower()
    if not base or len(base) < 3:
        return False
    if base in _BLOCKED_HOSTS:
        return False
    if not re.search(r"\d", base):
        return False
    return bool(_HOSTNAME_RE.match(base))


def _normalize_server(rec: dict) -> Optional[Dict[str, Any]]:
    host = str(rec.get("host") or rec.get("hostname") or "").strip()
    if not _is_real_host(host):
        return None

    cpu_used      = _clamp_pct(rec.get("cpu_used", rec.get("cpu_pct", 0)))
    cpu_avg       = _clamp_pct(rec.get("cpu_avg", 0))
    mem_used      = _clamp_pct(rec.get("mem_used", rec.get("mem_pct", 0)))
    disk_used_max = _clamp_pct(rec.get("disk_used_max", rec.get("disk_pct", 0)))

    try:
        mem_total_gb = float(rec.get("mem_total_gb") or 0)
    except (TypeError, ValueError):
        mem_total_gb = 0.0

    disks_in = rec.get("disks") or {}
    disks: Dict[str, float] = {}
    if isinstance(disks_in, dict):
        for mnt, v in disks_in.items():
            key = str(mnt).strip() or "/"
            disks[key] = _clamp_pct(v)
    if disks and disk_used_max == 0.0:
        disk_used_max = max(disks.values())

    stype = (rec.get("type") or "").strip().upper()
    if stype not in ("APP", "DB", "SRE"):
        stype = _infer_type(host)

    has_data = cpu_used > 0 or mem_used > 0 or disk_used_max > 0

    return {
        "host":           host,
        "type":           stype,
        "cpu_used":       cpu_used,
        "cpu_avg":        cpu_avg,
        "mem_used":       mem_used,
        "mem_total_gb":   round(mem_total_gb, 2),
        "disk_used_max":  disk_used_max,
        "disks":          disks,
        "_image_only":    not has_data,
        "_llm_extracted": True,
    }


def extract_servers_from_text(
    text: str,
    api_key: Optional[str] = None,
    max_chars: int = 60_000,
) -> List[Dict[str, Any]]:
    """Ask NVIDIA NIM to extract a structured server list from raw report text.

    Returns an empty list if no API key is configured, the call fails, or
    nothing usable is parsed.  Never raises.
    """
    if not text or not text.strip():
        return []

    if not api_key:
        try:
            from services.config_store import get_nvidia_key
            api_key = get_nvidia_key()
        except Exception:
            api_key = ""
    if not api_key:
        logger.debug("nvidia_llm: no API key configured — skipping LLM fallback")
        return []

    snippet = text if len(text) <= max_chars else (
        text[: max_chars // 2] + "\n...[truncated]...\n" + text[-max_chars // 2 :]
    )

    # Delegate to the unified ai_engine so we automatically pick up Gemma /
    # Llama / Gemini fallbacks rather than duplicating the waterfall here.
    user_prompt = _USER_PROMPT_TEMPLATE.format(text=snippet)
    try:
        from services.ai_engine import chat_json
        parsed, used_model = chat_json(
            user_prompt,
            system=_SYSTEM_PROMPT,
            max_tokens=4096,
            temperature=0.0,
            prefer_provider="nvidia",
        )
    except Exception as exc:
        logger.warning("nvidia_llm: ai_engine extraction failed — %s", exc)
        return []

    if isinstance(parsed, list):
        server_list = parsed
    elif isinstance(parsed, dict):
        server_list = parsed.get("servers") or parsed.get("data") or []
        if isinstance(server_list, dict):
            server_list = [server_list]
    else:
        server_list = []
    if not isinstance(server_list, list):
        return []

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for rec in server_list:
        if not isinstance(rec, dict):
            continue
        norm = _normalize_server(rec)
        if not norm:
            continue
        key = norm["host"].split(".")[0].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)

    logger.info(
        "nvidia_llm: extracted %d server(s) via %s",
        len(out), used_model,
    )
    return out


def test_api_key(api_key: str, timeout: int = 15) -> Dict[str, Any]:
    """Lightweight liveness check for an NVIDIA NIM key."""
    key = (api_key or "").strip()
    if not key:
        return {"valid": False, "error": "Empty key"}
    body = json.dumps({
        "model": _MODELS[0],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 4,
        "temperature": 0.0,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        _NVIDIA_ENDPOINT, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return {"valid": True, "model": _MODELS[0]}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {"valid": False, "error": "Invalid API key"}
        return {"valid": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"valid": False, "error": str(exc)[:120]}
