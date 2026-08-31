"""
ai_agent — tool-using LLM loop on top of services.ai_engine.

Unlike `ai_narrator` which is single-shot (prompt -> prose), this module
runs an iterative agent that can call tools to fetch real evidence before
answering. Every claim in the final answer is therefore traceable to a
deterministic data lookup, not a hallucination.

Flow:
    1. POST to NIM with `tools=[...]` (OpenAI-compatible schema).
    2. If the model returns `tool_calls`, dispatch them through
       `agent_tools.call_tool`, append results to the message history, loop.
    3. Stop when the model emits a normal `content` reply OR after MAX_TURNS.
    4. Return both the final answer and the tool trace so the UI can show
       *what evidence was inspected*.

Falls back to single-shot `ai_engine.chat` if no tool-capable model
responds, so callers always get something useful.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from services import agent_tools
from services.agent_tools import TOOL_SCHEMAS, call_tool

logger = logging.getLogger("pe_dashboard.ai_agent")

_NIM_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# Models with reliable function-calling on NIM, ordered by reasoning depth.
# Mixtral and llama-3.1 are NOT included — they ignore tools half the time.
_TOOL_CAPABLE = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "meta/llama-3.3-70b-instruct",
]

MAX_TURNS = 6  # safety: cap how many tool round-trips one task can do


# ────────────────────────────────────────────────────────────────
def _nim_post(payload: dict, api_key: str, timeout: int = 60) -> Optional[dict]:
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
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            err = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err = ""
        logger.warning("ai_agent: NIM HTTP %s — %s", exc.code, err[:160])
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai_agent: NIM error — %s", exc)
        return None


def _extract_assistant(choice: dict) -> dict:
    """Normalise the assistant message and rescue content from
    `reasoning_content` (gpt-oss) when `content` is empty."""
    msg = (choice or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = (content or "").strip()
    if not content:
        rc = msg.get("reasoning_content") or ""
        if isinstance(rc, list):
            rc = "".join(p.get("text", "") for p in rc if isinstance(p, dict))
        rc = (rc or "").strip()
        if rc:
            content = rc
    return {
        "role":       "assistant",
        "content":    content,
        "tool_calls": msg.get("tool_calls") or [],
    }


# ────────────────────────────────────────────────────────────────
def run_agent(
    *,
    task: str,
    system: str,
    max_tokens: int = 1100,
    temperature: float = 0.2,
    tool_choice: str = "auto",
) -> Tuple[str, str, List[dict]]:
    """Run the tool-using agent loop.

    Returns:
        (final_text, model_id, trace) where trace is a list of dicts:
            {step, kind: "tool_call"|"answer", name, args, result, content}
        suitable for rendering in the UI.

    Falls back to single-shot ai_engine.chat (no tools) if all
    tool-capable models fail.
    """
    from services.config_store import get_nvidia_key, get as cfg_get
    api_key = get_nvidia_key()
    pref    = cfg_get("ai_text_model") or _TOOL_CAPABLE[0]

    trace: List[dict] = []

    # Auto-call list_loaded_data once up front — this gives the model
    # visibility into the dataset shape without spending a tool turn.
    inventory = call_tool("list_loaded_data", {})
    trace.append({
        "step": 0, "kind": "tool_call",
        "name": "list_loaded_data", "args": {}, "result": inventory,
    })
    bootstrap_msg = (
        "You are about to investigate the user's question with read-only tools "
        "that query the actual uploaded customer data. The dataset inventory has "
        "already been fetched for you below.\n\n"
        "HARD RULES:\n"
        "  1. NEVER invent hostnames, job names, server numbers, or IDs. Every "
        "identifier in your answer must appear in a tool result.\n"
        "  2. Before calling get_host_metrics(host=...) you MUST either pass "
        "host='*' (returns all) or pick a name from list_hosts() / the "
        "inventory's resource_summary.all_hostnames.\n"
        "  3. Before calling get_job_history(job_name=...) you MUST pick a name "
        "from list_jobs() or the inventory's sla_summary.sample_*_jobs.\n"
        "  4. If a tool result is empty or returns 'note' / 'error', read that "
        "text — it tells you what to try next. Do NOT retry the same call.\n"
        "  5. If the data needed for the answer is not in the inventory, say so "
        "plainly in the answer. Do not fabricate.\n"
        "  6. Quote exact numbers (hours, percentages, run counts) verbatim from "
        "tool results. Round only when the number itself was rounded.\n\n"
        f"INVENTORY:\n{json.dumps(inventory, default=str)[:3500]}"
    )

    if not api_key:
        return _single_shot_fallback(task, system, max_tokens, temperature, trace,
                                     reason="no NVIDIA key")

    # Order: preferred model first, then the rest of the tool-capable set.
    ordering = [pref] + [m for m in _TOOL_CAPABLE if m != pref]

    for model in ordering:
        text, used = _run_loop(
            model=model, api_key=api_key,
            task=task, system=system,
            bootstrap=bootstrap_msg, trace=trace,
            max_tokens=max_tokens, temperature=temperature,
            tool_choice=tool_choice,
        )
        if text:
            return text, used, trace
        # If this model failed, drop any partial traces from this attempt
        # so the next model starts clean (but keep step 0 inventory).
        trace[:] = [trace[0]]

    return _single_shot_fallback(task, system, max_tokens, temperature, trace,
                                 reason="all tool-capable models failed")


def _run_loop(
    *,
    model: str,
    api_key: str,
    task: str,
    system: str,
    bootstrap: str,
    trace: List[dict],
    max_tokens: int,
    temperature: float,
    tool_choice: str,
) -> Tuple[Optional[str], Optional[str]]:
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user",   "content": bootstrap},
        {"role": "user",   "content": task},
    ]

    for turn in range(1, MAX_TURNS + 1):
        payload = {
            "model":       model,
            "messages":    messages,
            "tools":       TOOL_SCHEMAS,
            "tool_choice": tool_choice,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "top_p":       0.95,
            "stream":      False,
        }
        data = _nim_post(payload, api_key)
        if not data:
            return None, None

        choices = data.get("choices") or []
        if not choices:
            return None, None
        finish = choices[0].get("finish_reason") or ""
        msg    = _extract_assistant(choices[0])
        tool_calls = msg.get("tool_calls") or []

        # Append the assistant turn (must include tool_calls when present).
        if tool_calls:
            messages.append({
                "role":      "assistant",
                "content":   msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn   = (tc.get("function") or {})
                raw_name = fn.get("name") or "?"
                # Sanitize tool name: strip channel tokens, XML-like tags and
                # any non-alphanumeric/underscore chars the model may inject
                # (e.g. gpt-oss inserts "<|channel|>" mid-name on some turns)
                name = re.sub(r"<[^>]*>", "", raw_name)   # strip <...> tokens
                name = re.sub(r"[^\w]", "", name)          # keep only word chars
                name = name.strip("_") or "?"
                args = fn.get("arguments") or {}
                result = call_tool(name, args)
                trace.append({
                    "step": turn, "kind": "tool_call",
                    "name": name,
                    "args": (json.loads(args) if isinstance(args, str) and args.strip().startswith("{") else args),
                    "result": result,
                })
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.get("id") or f"call_{turn}_{name}",
                    "name":         name,
                    "content":      json.dumps(result, default=str)[:6000],
                })
            continue  # let the model reason on the tool results

        # No tool calls — this is the final answer
        text = msg.get("content") or ""
        if text:
            trace.append({"step": turn, "kind": "answer", "content": text[:120]})
            return text.strip(), f"nvidia:{model}"

        # Model returned no content and no tool calls → stop and let the
        # outer loop try the next model.
        if finish in ("stop", "length"):
            return None, None

    # Hit MAX_TURNS without a final answer — force the model to summarise.
    messages.append({
        "role": "user",
        "content": ("You have used the maximum number of tool turns. "
                    "Stop calling tools now and answer the original question "
                    "using the evidence already gathered."),
    })
    payload = {
        "model":       model,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "stream":      False,
    }
    data = _nim_post(payload, api_key)
    if data and (data.get("choices") or []):
        msg = _extract_assistant(data["choices"][0])
        if msg.get("content"):
            trace.append({"step": MAX_TURNS + 1, "kind": "answer",
                          "content": msg["content"][:120]})
            return msg["content"].strip(), f"nvidia:{model}"
    return None, None


def _single_shot_fallback(
    task: str, system: str, max_tokens: int, temperature: float,
    trace: List[dict], reason: str,
) -> Tuple[str, str, List[dict]]:
    """When no tool-capable model is reachable, hand the auto-gathered
    inventory to plain ai_engine.chat and at least produce *something*."""
    try:
        from services.ai_engine import chat as _chat
        inventory = trace[0]["result"] if trace else {}
        prompt = (
            f"{task}\n\n"
            "INVENTORY (read-only):\n"
            f"{json.dumps(inventory, default=str)[:3500]}\n\n"
            "Use ONLY the inventory above. Quote exact numbers. "
            "Be explicit when data is missing."
        )
        text, model = _chat(prompt, system=system,
                            max_tokens=max_tokens, temperature=temperature)
        trace.append({"step": 1, "kind": "answer", "content": (text or "")[:120],
                      "fallback_reason": reason})
        return (text or "").strip(), model, trace
    except Exception as exc:
        trace.append({"step": 1, "kind": "error", "error": str(exc),
                      "fallback_reason": reason})
        return (
            f"AI agent unavailable ({reason}; fallback also failed: {exc}). "
            "The deterministic dashboard panels still contain the full data."
        ), "deterministic", trace


# Convenience helper for the simplest case.
def deep_dive(question: str, *, scope: str = "general") -> Dict[str, Any]:
    """One-shot helper used by /api/agent/deep-dive."""
    system_by_scope = {
        "general": (
            "You are a Senior Performance Engineering analyst. Answer the user's "
            "question by calling the available tools to gather evidence from the "
            "uploaded customer data, then synthesising a tight verdict. "
            "RULES:\n"
            "  * Quote exact numbers, hostnames, job names from tool results.\n"
            "  * If you cannot verify a claim with a tool, say so explicitly.\n"
            "  * No filler, no apologies, no generic advice.\n"
            "  * Final answer must include: ROOT CAUSE, EVIDENCE (cite tool calls), "
            "and ONE NEXT ACTION."
        ),
        "finding": (
            "You are diagnosing a single PE finding. Use the tools to confirm "
            "(or refute) it with row-level evidence. Output sections:\n"
            "  CONFIRMED / DISPUTED / INCONCLUSIVE — one word\n"
            "  ROOT CAUSE — one sentence\n"
            "  EVIDENCE — bullet list with exact numbers + tool names\n"
            "  NEXT ACTION — one sentence"
        ),
        "breach": (
            "You are diagnosing why a specific job breached SLA. Use tools to "
            "fetch the job's history, baseline, and resource correlation. Output:\n"
            "  VERDICT — RESOURCE_DRIVEN | JOB_REGRESSION | DATA_VOLUME | UNCLEAR\n"
            "  EVIDENCE — exact numbers, hostnames, hours\n"
            "  CONFIDENCE — LOW | MEDIUM | HIGH\n"
            "  NEXT ACTION — one sentence"
        ),
        "verdict": (
            "You are the Senior PE Consultant. Use tools to inspect the data, "
            "then produce: VERDICT (one line) / TOP 3 RISKS (with evidence) / "
            "PREDICTIONS / NEXT 48H ACTIONS / ACCURACY NOTE."
        ),
    }
    system = system_by_scope.get(scope, system_by_scope["general"])
    text, model, trace = run_agent(task=question, system=system)
    return {
        "answer": text,
        "model":  model,
        "trace":  trace,
        "tool_count": sum(1 for t in trace if t.get("kind") == "tool_call"),
    }
