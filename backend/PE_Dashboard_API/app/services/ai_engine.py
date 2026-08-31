"""
Unified AI engine for the PE Audit Dashboard.

Single entry point used by every router that needs an LLM call:
    routers.ai          (insights)
    routers.redflags    (narrative)
    routers.correlation (cross-pillar narrative)
    routers.sow         (volume vs SOW commentary)
    routers.executive   (executive briefing)
    routers.benchmark   (degradation diagnosis)
    routers.batch       (Ctrl-M anomaly explanation)
    routers.upload      (post-upload quick summary)

Provider waterfall (configured via config_store keys ai_text_provider / ai_text_model):
    1. NVIDIA NIM gpt-oss-120b   — reasoning model (default for review + agent)
    2. NVIDIA NIM gpt-oss-20b    — smaller reasoning fallback
    3. NVIDIA NIM Llama-3.3-70B  — instruction-tuned fallback
    4. Google Gemini             — vision-only (text waterfall is empty)

Note: gemma-3-27b-it, llama-3.1-70b-instruct, and mixtral-8x22b were
removed from the waterfall because they consistently time out or return
HTTP 400 "function id" errors on the current NIM tenant.

Public API:
    chat(prompt, *, system=None, json_mode=False, max_tokens=2048,
         temperature=0.4, prefer_provider=None) -> tuple[str, str]
        Returns (text, model_used). Raises only if every provider fails.

    chat_json(prompt, **kw) -> tuple[dict | list, str]
        Same but parses the JSON response and returns the parsed object.

    is_ready() -> dict   # quick status for /api/ai-status

The engine is intentionally stateless and dependency-light: stdlib `urllib`
for NIM, `google.genai` (already a project dep) for Gemini.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple

logger = logging.getLogger("pe_dashboard.ai_engine")

_NIM_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# ── NIM model waterfall ────────────────────────────────────────
# Ordered by reasoning quality × speed. The user-configured
# ai_text_model is always tried first; the rest serve as fallbacks.
#
# OpenAI gpt-oss-120b sits at the head: it's a true reasoning model
# (chain-of-thought via `reasoning_content`) which is exactly what we
# need for the post-load DATA REVIEW pass that sanity-checks every
# metric on the dashboard before the user sees it.
_NIM_DEFAULT_WATERFALL = [
    "openai/gpt-oss-120b",                       # reasoning (default for review)
    "openai/gpt-oss-20b",                        # smaller reasoning fallback
    "meta/llama-3.3-70b-instruct",               # instruction-tuned fallback
]

# Models that emit `reasoning_content` — used for the data-review pass
_REASONING_MODELS = {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}

# ── Gemini fallback waterfall ──────────────────────────────────
# Gemini text models serve as a second-vendor fallback when every NIM
# reasoning model is exhausted. Order: fastest → most capable. Used by
# /api/ai-findings and the executive narrative for true cross-vendor
# resilience (NVIDIA NIM + Google Gemini).
_GEMINI_WATERFALL: list[str] = [
    "gemini-2.5-flash",          # fast, highly capable (confirmed working, May 2026)
    "gemini-2.0-flash",          # previous generation, widely available
    "gemini-2.0-flash-lite",     # lightweight fallback
]

# ── Session-scoped dead-model cache ────────────────────────────
# Once a model fails (HTTP 4xx other than 429, or empty SDK response),
# we remember the reason and skip it for the rest of this process.
# This kills the ~150ms-per-call overhead of pinging dead models repeatedly.
# Cleared by /api/ai-self-test (the user's "Verify" button).
_DEAD_MODELS:    dict[str, str] = {}      # "provider:model" -> reason
_LAST_ERR:       dict[str, str] = {}      # last error per model (for self-test surfacing)
_DEAD_LOCK = threading.Lock()


def _mark_dead(provider: str, model: str, reason: str) -> None:
    key = f"{provider}:{model}"
    with _DEAD_LOCK:
        _DEAD_MODELS[key] = reason
        _LAST_ERR[key]    = reason
    logger.info("ai_engine: marking %s as dead for this session — %s", key, reason)


def _is_dead(provider: str, model: str) -> Optional[str]:
    with _DEAD_LOCK:
        return _DEAD_MODELS.get(f"{provider}:{model}")


def reset_dead_cache() -> None:
    """Clear the dead-model cache (called by /api/ai-self-test)."""
    with _DEAD_LOCK:
        _DEAD_MODELS.clear()


def get_last_error(provider: str, model: str) -> str:
    with _DEAD_LOCK:
        return _LAST_ERR.get(f"{provider}:{model}", "")



# ────────────────────────────────────────────────────────────────
# NIM call
# ────────────────────────────────────────────────────────────────
def _call_nim(
    prompt: str,
    system: Optional[str],
    api_key: str,
    model: str,
    max_tokens: int,
    temperature: float,
    json_mode: bool,
    timeout: int,
) -> Optional[str]:
    """One NIM POST. Returns the assistant message string or None on failure."""
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model":       model,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "top_p":       0.95,
        "stream":      False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _NIM_ENDPOINT, data=body, method="POST",
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
            reason = "empty choices in response"
            logger.warning("ai_engine: %s returned %s", model, reason)
            _mark_dead("nvidia", model, reason)
            return None
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = (content or "").strip()
        # ── Reasoning-model fallback ────────────────────────────
        # OpenAI gpt-oss-120b / gpt-oss-20b often emit nothing on the
        # `content` channel and put the full answer (including JSON)
        # inside `reasoning_content`. Harvest it so the caller still
        # sees the answer.
        if not text:
            rc = msg.get("reasoning_content") or ""
            if isinstance(rc, list):
                rc = "".join(p.get("text", "") for p in rc if isinstance(p, dict))
            rc = (rc or "").strip()
            if rc:
                # When json_mode was requested, try to extract the JSON
                # object from the reasoning trace.
                # Use a non-greedy, balanced-brace-aware approach:
                # try json.loads on each { candidate to find the first valid object.
                if json_mode:
                    import json as _json
                    _extracted = None
                    for _m in re.finditer(r"\{", rc):
                        candidate = rc[_m.start():]
                        # Try to parse a valid JSON object from here
                        try:
                            _extracted = _json.loads(candidate[:candidate.rindex("}") + 1]
                                                     if "}" in candidate else candidate)
                            text = candidate[:candidate.rindex("}") + 1]
                            break
                        except Exception:
                            pass
                    # Also try arrays
                    if not text:
                        for _m in re.finditer(r"\[", rc):
                            candidate = rc[_m.start():]
                            try:
                                _json.loads(candidate[:candidate.rindex("]") + 1]
                                            if "]" in candidate else candidate)
                                text = candidate[:candidate.rindex("]") + 1]
                                break
                            except Exception:
                                pass
                if not text:
                    text = rc
        if not text:
            _mark_dead("nvidia", model, "empty content + empty reasoning_content")
            return None
        return text
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = ""
        reason = f"HTTP {exc.code}: {err_body[:160]}"
        logger.warning("ai_engine: NIM %s — %s", model, reason)
        # 4xx (other than 429 rate-limit) means the model is permanently
        # unavailable for this account → mark dead
        if 400 <= exc.code < 500 and exc.code != 429:
            _mark_dead("nvidia", model, reason)
        else:
            with _DEAD_LOCK:
                _LAST_ERR[f"nvidia:{model}"] = reason
        return None
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("ai_engine: NIM %s failed — %s", model, reason)
        with _DEAD_LOCK:
            _LAST_ERR[f"nvidia:{model}"] = reason
        return None


# ────────────────────────────────────────────────────────────────
# Gemini call
# ────────────────────────────────────────────────────────────────
def _call_gemini(
    prompt: str,
    system: Optional[str],
    api_key: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> Optional[str]:
    """One Gemini call via the google-genai SDK.  Returns text or None."""
    try:
        from google import genai as _genai
        client = _genai.Client(api_key=api_key)
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        cfg: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature":       temperature,
        }
        # Disable thinking on flash models to avoid silent truncation at
        # low token budgets. Flash accepts budget=0; pro models require a
        # minimum budget (>= 128) so we skip the override there.
        if "flash" in model and any(t in model for t in ("2.5", "3.", "3-", "3.1")):
            cfg["thinking_config"] = {"thinking_budget": 0}
        resp = client.models.generate_content(
            model=model, contents=full_prompt, config=cfg,
        )
        text = (resp.text or "").strip()
        if not text:
            _mark_dead("gemini", model, "empty response (model may be deprecated or filtered)")
            return None
        return text
    except ImportError:
        logger.warning("ai_engine: google-genai not installed")
        return None
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.debug("ai_engine: Gemini %s failed — %s", model, reason)
        # Gemini SDK raises a generic ClientError for 404 (model not found),
        # 400, etc. — treat any non-empty exception as a permanent dead model
        # for the session.
        msg = str(exc).lower()
        if any(p in msg for p in ("not found", "404", "deprecated",
                                  "invalid", "permission", "403", "400")):
            _mark_dead("gemini", model, reason)
        else:
            with _DEAD_LOCK:
                _LAST_ERR[f"gemini:{model}"] = reason
        return None


# ────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────
def _build_nim_waterfall(preferred: Optional[str]) -> list[str]:
    """Put the preferred NIM model first, then the standard waterfall."""
    seen: set[str] = set()
    out: list[str] = []
    for m in ([preferred] if preferred else []) + _NIM_DEFAULT_WATERFALL:
        if m and m not in seen:
            out.append(m)
            seen.add(m)
    return out


def chat(
    prompt: str,
    *,
    system: Optional[str] = None,
    json_mode: bool = False,
    max_tokens: int = 2048,
    temperature: float = 0.4,
    prefer_provider: Optional[str] = None,
    timeout: int = 25,
) -> Tuple[str, str]:
    """Run one chat completion through the provider waterfall.

    Returns (text, model_id). Raises RuntimeError only when every provider fails.
    """
    from services import pe_config
    if not pe_config.AI_ENABLED:
        raise RuntimeError("ai_engine: AI disabled (pe_config.AI_ENABLED=False)")
    from services.config_store import (
        get_nvidia_key, get_gemini_key, get as cfg_get,
    )
    nv_key  = get_nvidia_key()
    gm_key  = get_gemini_key()

    cfg_provider = (prefer_provider or cfg_get("ai_text_provider", "nvidia") or "nvidia").lower()
    pref_model   = cfg_get("ai_text_model", _NIM_DEFAULT_WATERFALL[0]) or _NIM_DEFAULT_WATERFALL[0]

    nim_models = _build_nim_waterfall(pref_model)

    last_err = "no providers configured"

    def _try_nim() -> Optional[Tuple[str, str]]:
        nonlocal last_err
        if not nv_key:
            last_err = "no NVIDIA key"
            return None
        for m in nim_models:
            if _is_dead("nvidia", m):
                logger.debug("ai_engine: skipping dead NIM model %s", m)
                continue
            text = _call_nim(prompt, system, nv_key, m,
                             max_tokens, temperature, json_mode, timeout)
            if text:
                return text, f"nvidia:{m}"
            last_err = f"NIM {m} failed"
        return None

    def _try_gemini() -> Optional[Tuple[str, str]]:
        nonlocal last_err
        if not gm_key:
            last_err = "no Gemini key"
            return None
        for m in _GEMINI_WATERFALL:
            if _is_dead("gemini", m):
                logger.debug("ai_engine: skipping dead Gemini model %s", m)
                continue
            text = _call_gemini(prompt, system, gm_key, m, max_tokens, temperature)
            if text:
                return text, f"gemini:{m}"
            last_err = f"Gemini {m} failed"
        return None

    # Honour explicit provider preference; otherwise NIM-first.
    if cfg_provider == "gemini":
        order = (_try_gemini, _try_nim)
    else:
        order = (_try_nim, _try_gemini)

    for fn in order:
        result = fn()
        if result:
            return result

    raise RuntimeError(f"ai_engine: all providers failed ({last_err})")


def chat_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    prefer_provider: Optional[str] = None,
) -> Tuple[Any, str]:
    """Convenience wrapper that requests JSON output and parses it."""
    sys_msg = (system or "") + (
        "\n\nReturn ONLY a strict JSON object — no prose, no markdown fences, "
        "no commentary."
    )
    text, model = chat(
        prompt, system=sys_msg.strip(), json_mode=True,
        max_tokens=max_tokens, temperature=temperature,
        prefer_provider=prefer_provider,
    )
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    try:
        return json.loads(cleaned), model
    except Exception:
        m = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", cleaned)
        if m:
            try:
                return json.loads(m.group()), model
            except Exception:
                pass
    raise RuntimeError(f"ai_engine: model {model} did not return valid JSON")


def is_ready() -> dict:
    """Status snapshot for /api/ai-status (UI 'AI is online' banner)."""
    from services import pe_config
    if not pe_config.AI_ENABLED:
        return {
            "nvidia_key":  False,
            "gemini_key":  False,
            "provider":    "disabled",
            "text_model":  None,
            "nim_models":  [],
            "dead_models": {},
            "ai_enabled":  False,
        }
    from services.config_store import get_nvidia_key, get_gemini_key, get as cfg_get
    with _DEAD_LOCK:
        dead = dict(_DEAD_MODELS)
    return {
        "nvidia_key":  bool(get_nvidia_key()),
        "gemini_key":  bool(get_gemini_key()),
        "provider":    cfg_get("ai_text_provider", "nvidia"),
        "text_model":  cfg_get("ai_text_model", _NIM_DEFAULT_WATERFALL[0]),
        "nim_models":  _NIM_DEFAULT_WATERFALL,
        "dead_models": dead,
    }
