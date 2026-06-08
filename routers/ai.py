"""
AI Insight router — unified multi-provider engine (NVIDIA Gemma/Llama → Gemini).

POST /api/ai-insight
    body: { type, api_key, batch_kpis, resource_kpis, servers, issues }
    response: { text: "...", model: "nvidia:google/gemma-3-27b-it" }

GET /api/ai-status
    response: { nvidia_key, gemini_key, provider, text_model, nim_models }

Mirrors `ai_run_deep_analysis()` and `ai_run_batch_analysis()` from
app_v2.py but routes every prompt through services.ai_engine, so the
dashboard automatically uses the fastest available provider (Gemma first,
Llama-3.3 second, Gemini last) without per-router duplication.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from services import pe_config
from services.ai_engine import chat as _ai_chat, is_ready as _ai_status

logger = logging.getLogger("pe_dashboard.routers.ai")

router = APIRouter()

# Thresholds — read from pe_config (user-configurable via Settings)
DAILY_LIMIT_HRS = pe_config.SLA_DAILY_HRS
CPU_OK   = pe_config.CPU_WARN
CPU_WARN = pe_config.CPU_CRIT
MEM_OK   = pe_config.MEM_WARN
MEM_WARN = pe_config.MEM_CRIT
DISK_OK  = pe_config.DISK_WARN
DISK_WARN = pe_config.DISK_CRIT

_SYSTEM = (
    "You are a Senior Performance Engineering consultant. Write tight, "
    "specific, evidence-led prose. Quote exact numbers, hostnames, and job "
    "names from the data. No filler, no apologies, no markdown headers "
    "deeper than '##'."
)


# ── Pydantic models ────────────────────────────────────────────
class AiRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    type:          Literal["batch", "resource", "full"] = "full"
    api_key:       str = ""                # user-supplied (legacy Gemini) key
    batch_kpis:    Dict[str, Any] | None = None
    resource_kpis: Dict[str, Any] | None = None
    servers:       List[Dict[str, Any]] | None = None
    issues:        List[Dict[str, Any]] | None = None
    top_jobs:      List[Dict[str, Any]] | None = None


class AiResponse(BaseModel):
    text:  str
    model: str = ""


# ── Engine wrapper with HTTP-friendly errors ───────────────────
def _run(prompt: str) -> tuple[str, str]:
    try:
        return _ai_chat(prompt, system=_SYSTEM, max_tokens=2048, temperature=0.4)
    except RuntimeError as exc:
        msg = str(exc)
        if "no NVIDIA key" in msg and "no Gemini key" in msg:
            raise HTTPException(
                status_code=400,
                detail="No AI key configured. Add an NVIDIA NIM key (Settings → NVIDIA NIM API Key) or a Gemini key.",
            ) from exc
        raise HTTPException(status_code=502, detail=f"AI engine error: {msg[:200]}") from exc


# ── Prompt builders ────────────────────────────────────────────
def _batch_prompt(bk: dict, top_jobs: list, issues: list) -> str:
    compliance = float(bk.get("compliance_pct", 0))
    breach_jobs = [j for j in top_jobs if float(j.get("peak_hrs", 0)) > DAILY_LIMIT_HRS][:10]
    at_risk = [j for j in top_jobs
               if float(j.get("buffer_pct", 100)) >= 0
               and float(j.get("buffer_pct", 100)) < 15][:10]
    top10 = top_jobs[:10]
    window_stats = {
        "sla_limit_hrs": DAILY_LIMIT_HRS,
        "total_jobs": bk.get("total_jobs", 0),
        "total_runs": bk.get("total_runs", 0),
        "jobs_breach": bk.get("jobs_breach", 0),
        "jobs_at_risk": bk.get("jobs_at_risk", 0),
        "compliance_pct": round(compliance, 2),
        "fleet_sla_buffer": bk.get("fleet_sla_buffer"),
    }
    return f"""You are a Senior Performance Engineering consultant reviewing a Ctrl-M batch workload audit.

Batch Summary: {json.dumps(window_stats)}
Breaching Jobs (>{DAILY_LIMIT_HRS}h SLA): {json.dumps(breach_jobs) if breach_jobs else "None"}
At-Risk Jobs (<15% buffer): {json.dumps(at_risk) if at_risk else "None"}
Top 10 Jobs: {json.dumps(top10)}

Write a batch performance diagnostic covering:
1. **Compliance Summary** — SLA %, jobs in breach vs at-risk vs healthy.
2. **Root Cause Analysis** — For breaching jobs, propose causes (sequential chaining, no parallelism, data volume growth, bottlenecks).
3. **Batch Window Pressure** — Avg/max daily window vs {DAILY_LIMIT_HRS}h SLA, risk of creep.
4. **Remediation Plan** — Numbered concrete fixes (parallelisation, scheduling, tuning).
5. **Go-Live Readiness** — ✅ READY / ⚠️ CONDITIONAL / 🔴 NOT READY with one-line justification.

Use job names. Quote exact hours. No generic padding."""


def _resource_prompt(rk: dict, servers: list) -> str:
    fleet_summary = {
        "fleet_grade":    rk.get("fleet_grade", "?"),
        "fleet_score":    rk.get("fleet_score", 0),
        "n_critical":     rk.get("n_critical",  0),
        "n_warning":      rk.get("n_warning",   0),
        "n_healthy":      rk.get("n_healthy",   0),
        "total_servers":  rk.get("total_servers", len(servers)),
        "avg_cpu":        rk.get("avg_cpu",  0),
        "avg_mem":        rk.get("avg_mem",  0),
        "avg_disk":       rk.get("avg_disk", 0),
        "cpu_warn": CPU_WARN, "cpu_ok": CPU_OK,
        "disk_warn": DISK_WARN, "disk_ok": DISK_OK,
        "mem_warn": MEM_WARN, "mem_ok": MEM_OK,
    }
    server_rows = []
    for s in servers:
        if s.get("image_only"):
            continue
        row = {
            "host":         (s.get("server") or s.get("host","?")).split(".")[0],
            "type":         s.get("type", "APP"),
            "cpu_pct":      round(float(s.get("cpu_pct",  s.get("cpu_used",  0)) or 0), 1),
            "mem_pct":      round(float(s.get("mem_pct",  s.get("mem_used",  0)) or 0), 1),
            "disk_max_pct": round(float(s.get("disk_pct", s.get("disk_used_max", 0)) or 0), 1),
            "score":        s.get("health_score"),
        }
        server_rows.append(row)

    return f"""You are a Senior Performance Engineering consultant reviewing a server infrastructure audit.

Fleet Summary: {json.dumps(fleet_summary)}
Per-Server Metrics: {json.dumps(server_rows)}

Write a diagnostic report covering:
1. **Overall Fleet Health** — Grade and 2-3 sentence summary.
2. **Critical Findings** — Each server at/near thresholds with exact % values and risk description.
3. **Root Causes** — Likely causes for elevated metrics (batch concurrency, log accumulation, memory leaks, under-provisioning).
4. **Prioritised Action Plan** — Numbered concrete remediation steps, most urgent first.
5. **Go-Live Verdict** — ✅ READY / ⚠️ CONDITIONAL / 🔴 NOT READY with one-line justification.

Use hostnames. Quote exact numbers. No generic padding."""


def _full_prompt(bk: dict, rk: dict, servers: list, top_jobs: list, issues: list) -> str:
    open_issues = [i for i in issues if i.get("Status") in ("Open","In Progress")]
    return f"""You are a Senior Performance Engineering consultant writing a full PE Audit sign-off report.

=== BATCH WORKLOAD ===
{json.dumps({k: bk.get(k) for k in ['compliance_pct','jobs_breach','jobs_at_risk','total_jobs','total_runs','fleet_sla_buffer']})}
Top Jobs (first 10): {json.dumps(top_jobs[:10])}

=== INFRASTRUCTURE ===
Fleet: Grade {rk.get('fleet_grade','?')} · Score {rk.get('fleet_score',0)} · {rk.get('n_critical',0)} Critical · {rk.get('n_warning',0)} Warning · {rk.get('n_healthy',0)} Healthy
Servers: {len([s for s in servers if not s.get('image_only')])} with metrics

=== OPEN ISSUES ===
{json.dumps(open_issues[:5]) if open_issues else "None"}

Write a concise PE Audit executive summary covering:
1. **Overall Fleet Health** — Grade and 2-3 sentence infrastructure summary.
2. **Batch SLA Status** — Compliance %, breaches, risk level.
3. **Critical Findings** — Top 3 items requiring immediate action (quote exact values).
4. **Root Causes** — For each critical finding, 1-sentence probable cause.
5. **Go-Live Verdict** — ✅ READY / ⚠️ CONDITIONAL / 🔴 NOT READY with 1-2 sentence justification.

Be specific. Quote exact percentages, job names, and host names. No filler."""


# ── Endpoint ───────────────────────────────────────────────────
@router.post(
    "/ai-insight",
    response_model=AiResponse,
    summary="Generate Gemini AI insight from batch + resource audit data",
)
async def ai_insight(body: AiRequest) -> AiResponse:
    bk       = body.batch_kpis    or {}
    rk       = body.resource_kpis or {}
    servers  = body.servers        or []
    issues   = body.issues         or []
    top_jobs = body.top_jobs       or []

    if body.type == "batch":
        prompt = _batch_prompt(bk, top_jobs, issues)
    elif body.type == "resource":
        prompt = _resource_prompt(rk, servers)
    else:
        prompt = _full_prompt(bk, rk, servers, top_jobs, issues)

    text, model_name = _run(prompt)
    return AiResponse(text=text, model=model_name)


@router.get("/ai-status")
def ai_status() -> dict[str, Any]:
    """Return AI engine readiness for the UI banner."""
    return _ai_status()


@router.get("/ai-self-test")
def ai_self_test() -> dict[str, Any]:
    """Live round-trip every configured model.

    Posts a tiny prompt to each NIM model and each Gemini model, plus a
    synthetic Vision call, and reports per-model status / latency / sample
    output / failure reason. Used by the UI's "Verify AI" button so users
    can confirm the LLM is actually answering — not just that the keys are
    present.

    Side-effects:
      - resets the session dead-model cache so we re-probe every model
      - if the user's configured `ai_text_model` is dead but another NIM
        model works, auto-promotes that working model in config_store so
        subsequent calls hit the working one first
    """
    import time
    import io
    import base64
    from services.ai_engine import (
        _NIM_DEFAULT_WATERFALL, _GEMINI_WATERFALL,
        _call_nim, _call_gemini, get_last_error, reset_dead_cache,
    )
    from services.config_store import (
        get_nvidia_key, get_gemini_key, get as cfg_get, set as cfg_set,
    )

    # Re-probe every model — clear the dead cache built up by prior calls
    reset_dead_cache()

    nv = get_nvidia_key()
    gm = get_gemini_key()
    probe = "Reply with exactly: PE_OK"
    probe_max = 12
    probe_temp = 0.0

    text_results: list[dict[str, Any]] = []
    first_working_nim: str | None = None

    for m in _NIM_DEFAULT_WATERFALL:
        if not nv:
            text_results.append({"provider": "nvidia", "model": m,
                                 "status": "no_key", "ms": 0,
                                 "sample": "", "reason": "NVIDIA key not configured"})
            continue
        t0 = time.time()
        out = _call_nim(probe, None, nv, m, probe_max, probe_temp, False, 20)
        ok = bool(out)
        if ok and first_working_nim is None:
            first_working_nim = m
        text_results.append({
            "provider": "nvidia", "model": m,
            "status":   "ok" if ok else "fail",
            "ms":       int((time.time() - t0) * 1000),
            "sample":   (out or "")[:80],
            "reason":   "" if ok else (get_last_error("nvidia", m) or "no response"),
        })
    for m in _GEMINI_WATERFALL:
        if not gm:
            text_results.append({"provider": "gemini", "model": m,
                                 "status": "no_key", "ms": 0,
                                 "sample": "", "reason": "Gemini key not configured"})
            continue
        t0 = time.time()
        out = _call_gemini(probe, None, gm, m, probe_max, probe_temp)
        ok = bool(out)
        text_results.append({
            "provider": "gemini", "model": m,
            "status":   "ok" if ok else "fail",
            "ms":       int((time.time() - t0) * 1000),
            "sample":   (out or "")[:80],
            "reason":   "" if ok else (get_last_error("gemini", m) or "no response"),
        })

    # ── Auto-promote: if the configured NIM model is dead but another
    # NIM model worked, switch the active model so the user gets fast
    # first-try success on subsequent calls.
    promoted: str | None = None
    if first_working_nim:
        configured = cfg_get("ai_text_model", "") or ""
        configured_dead = any(
            r["provider"] == "nvidia" and r["model"] == configured
            and r["status"] == "fail"
            for r in text_results
        )
        if configured_dead or not configured:
            cfg_set("ai_text_model", first_working_nim)
            promoted = first_working_nim
            logger.info("ai_engine: auto-promoted active model to %s "
                        "(configured %s was dead)", first_working_nim, configured)


    # Vision probe — best-effort synthetic chart
    vision: dict[str, Any] = {"status": "skipped", "ms": 0, "metrics": []}
    if gm:
        try:
            try:
                from PIL import Image, ImageDraw
                img = Image.new("RGB", (320, 120), "white")
                d = ImageDraw.Draw(img)
                d.text((10, 10), "selftest-host", fill="black")
                d.text((10, 40), "Percentage CPU (Max) | 75.5%", fill="red")
                d.text((10, 60), "Available Memory Percentage (Min) | 50%", fill="blue")
                buf = io.BytesIO(); img.save(buf, format="PNG")
                png = buf.getvalue()
            except ImportError:
                png = base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
                    "IQAAAABJRU5ErkJggg=="
                )
            from services.gemini_vision import extract_chart_metrics
            t0 = time.time()
            metrics = extract_chart_metrics(png, api_key=gm) or []
            vision = {
                "status":  "ok" if metrics else "empty",
                "ms":      int((time.time() - t0) * 1000),
                "metrics": metrics[:5],
            }
        except Exception as exc:
            vision = {"status": "fail", "ms": 0, "metrics": [], "error": str(exc)[:160]}

    summary = {
        "text_ok":  sum(1 for r in text_results if r["status"] == "ok"),
        "text_total": len(text_results),
        "vision_ok":  vision["status"] == "ok",
        "active_text_model": _ai_status().get("text_model"),
        "promoted_to": promoted,
    }
    return {"summary": summary, "text": text_results, "vision": vision}


# ── Generic narrative endpoint (used by batch / correlation / sow /
#    executive / benchmark / sla-matrix UI panels) ─────────────────
class NarrativeRequest(BaseModel):
    """Free-form narrative request — caller supplies the topic + raw data."""
    model_config = ConfigDict(extra="allow")

    topic:        str
    instructions: str = ""
    data:         Dict[str, Any] | List[Any] | None = None
    max_tokens:   int = 900
    temperature:  float = 0.35


@router.post("/ai-narrative", response_model=AiResponse,
             summary="Generic AI narrative for any dashboard panel")
async def ai_narrative(body: NarrativeRequest) -> AiResponse:
    topic = body.topic.strip() or "performance engineering data"
    instr = body.instructions.strip() or (
        "Write a concise, evidence-led 4-6 line analysis. Quote exact "
        "numbers from the data. Identify the single biggest risk and one "
        "concrete next step."
    )
    prompt = (
        f"TOPIC: {topic}\n"
        f"INSTRUCTIONS: {instr}\n\n"
        f"DATA:\n{json.dumps(body.data, default=str)[:18000]}"
    )
    try:
        text, model = _ai_chat(
            prompt, system=_SYSTEM,
            max_tokens=body.max_tokens, temperature=body.temperature,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from exc
    return AiResponse(text=text, model=model)


# ── Post-load data review (OpenAI gpt-oss-120b reasoning) ──────────────────
class ReviewDataRequest(BaseModel):
    """Caller bundles every dashboard payload they have. Missing pillars
    are simply skipped — the reviewer adapts to whatever is loaded."""
    model_config = ConfigDict(extra="allow")

    batch:         Dict[str, Any] | None = None
    resource:      Dict[str, Any] | None = None
    sla_matrix:    Dict[str, Any] | None = None
    findings:      Dict[str, Any] | None = None
    customer_name: str | None = None


@router.post(
    "/review-data",
    summary="Reasoning-model review of the loaded dashboard data",
)
async def review_data(body: ReviewDataRequest) -> Dict[str, Any]:
    """Run `services.data_reviewer.review()` off the event loop.

    The reviewer asks `openai/gpt-oss-120b` (NVIDIA NIM) to cross-check
    the headline numbers across all loaded pillars and return a strict
    JSON corrections list. Hard timeout 35s; falls back to a deterministic
    in-process consistency check if the LLM is unreachable.
    """
    import asyncio
    from services.data_reviewer import review as _review

    payload = body.model_dump(exclude_none=True)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_review, payload),
            timeout=35.0,
        )
    except asyncio.TimeoutError:
        from services.data_reviewer import _deterministic_review, _digest
        logger.warning("review-data: LLM timed out, returning deterministic check")
        result = _deterministic_review(_digest(payload))
        result["timeout"] = True
    return result


# ── AI-driven PE Findings (replaces verbose rule-engine cards) ─────────────
class AiFindingsRequest(BaseModel):
    """Request for LLM-synthesised PE findings.

    Caller may pass any pillar payload; whatever is missing is pulled from
    `services.session_cache` so the reasoning model always sees the full
    picture (SLA matrix + Ctrl-M + resource utilisation + benchmark + SOW).
    """
    model_config = ConfigDict(extra="allow")

    batch:         Dict[str, Any] | None = None
    resource:      Dict[str, Any] | None = None
    sla_matrix:    Dict[str, Any] | None = None
    sla_intel:     Dict[str, Any] | None = None
    benchmark:     Dict[str, Any] | None = None
    sow_compare:   Dict[str, Any] | None = None
    red_flags:     Dict[str, Any] | None = None
    customer_name: str | None = None


_FINDINGS_SYSTEM = (
    "You are a Principal Performance Engineering consultant producing a "
    "single, audit-grade findings report from multi-source telemetry. "
    "You receive: SLA matrix contracts (with derived buffer/health), "
    "Ctrl-M batch run history, server resource utilisation, UI/perf "
    "benchmarking, SOW commitments, and red flags. "
    "Cross-correlate the pillars. Quote exact batch names, hostnames, "
    "numbers, percentages, and dates from the data. "
    "Never invent metrics that are not in the payload. "
    "Output ONLY a strict JSON object — no markdown, no prose outside JSON."
)


def _digest_for_findings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compress raw pillar payloads to ~12 KB of high-signal context."""
    out: Dict[str, Any] = {}
    b = payload.get("batch") or {}
    if b:
        out["batch"] = {
            "kpis":          b.get("kpis"),
            "data_coverage": b.get("data_coverage"),
            "worst_job":     b.get("worst_job"),
            "summed_runtime":b.get("summed_runtime"),
            "elapsed_window":b.get("elapsed_window"),
            "top_jobs":      (b.get("top_jobs") or [])[:15],
            "top_breaches":  (b.get("top_breaches") or [])[:15],
            "anomalies":     (b.get("anomalies") or [])[:10],
            "sub_stats":     (b.get("sub_stats") or [])[:10],
            "sla_source":    b.get("sla_source"),
        }
    r = payload.get("resource") or {}
    if r:
        srv = r.get("servers") or []
        out["resource"] = {
            "kpis":    r.get("kpis"),
            "servers": [
                {k: s.get(k) for k in (
                    "host","role","cpu_avg_pct","cpu_max_pct",
                    "mem_avg_pct","mem_max_pct","disk_pct","status","notes",
                ) if s.get(k) is not None}
                for s in srv[:12]
            ],
        }
    si = payload.get("sla_intel") or {}
    if si:
        out["sla_intel"] = {
            "schema_type":      si.get("schema_type"),
            "ceilings":         si.get("ceilings"),
            "valid_rows":       si.get("valid_rows"),
            "partial_rows":     si.get("partial_rows"),
            "warnings":         (si.get("warnings") or [])[:6],
            "contracts": [
                {k: c.get(k) for k in (
                    "batch_name","schedule_type","schedule_raw","sla_window_hrs",
                    "actual_window_hrs","buffer_hrs","buffer_pct",
                    "health_status","health_reason","is_cyclic",
                    "business_acknowledged","comments",
                ) if c.get(k) is not None}
                for c in (si.get("contracts") or [])[:25]
            ],
        }
    sm = payload.get("sla_matrix") or {}
    if sm:
        out["sla_matrix"] = {
            "kpis":          sm.get("kpis"),
            "outliers":      (sm.get("outliers") or [])[:10],
            "resource_link": (sm.get("resource_linked") or [])[:8],
        }
    bm = payload.get("benchmark") or {}
    if bm:
        out["benchmark"] = {
            "summary":    bm.get("summary"),
            "categories": (bm.get("categories") or [])[:8],
            "rows":       (bm.get("rows") or [])[:15],
        }
    sw = payload.get("sow_compare") or {}
    if sw:
        out["sow_compare"] = sw
    rf = payload.get("red_flags") or {}
    if rf:
        out["red_flags"] = {
            "summary": rf.get("summary"),
            "items":   (rf.get("items") or rf.get("findings") or [])[:10],
        }
    return out


@router.post(
    "/ai-findings",
    summary="LLM-synthesised PE findings across all data pillars",
)
async def ai_findings(body: AiFindingsRequest) -> Dict[str, Any]:
    """Cross-pillar findings synthesis.

    Hydrates missing pillars from `session_cache`, builds a compact digest,
    asks the AI engine (NIM → Gemini waterfall) for a structured findings
    list, and returns it. Always returns a `findings` list — empty when
    no data is loaded yet, deterministic fallback when the LLM is down.
    """
    import asyncio
    from services import session_cache
    from services.ai_engine import chat_json

    payload = body.model_dump(exclude_none=True)

    # Hydrate missing pillars from session_cache so the report is complete
    # even when the caller only sends what's currently in the active tab.
    payload.setdefault("batch",      session_cache.get("last_batch")      or {})
    payload.setdefault("resource",   session_cache.get("last_resource")   or {})
    payload.setdefault("sla_matrix", session_cache.get("last_sla_matrix") or {})
    payload.setdefault("red_flags",  session_cache.get("last_red_flags")  or {})
    if not payload.get("sla_intel"):
        from services import config_store
        payload["sla_intel"] = config_store.get("_sla_intelligence") or {}

    digest = _digest_for_findings(payload)
    customer = payload.get("customer_name") or ""

    if not any(digest.values()):
        return {
            "findings": [],
            "summary": {"critical": 0, "warning": 0, "info": 0, "ok": 0, "total": 0},
            "model": "",
            "note": "No data loaded yet — upload Ctrl-M / SLA / resource files first.",
        }

    # ── Run the deterministic rule engine first (ground truth) ────────
    # This catches misleading-green, agg traps, waivers, cross-source
    # correlations — things the LLM often misses or invents.
    rule_engine_findings: List[Dict[str, Any]] = []
    rule_engine_summary: Dict[str, int] = {}
    try:
        from routers.findings import _generate, FindingsRequest as _FR
        _fr_body = _FR(
            batch_kpis=payload.get("batch", {}).get("kpis"),
            top_jobs=payload.get("batch", {}).get("top_jobs"),
            top_breaches=payload.get("batch", {}).get("top_breaches"),
            window=payload.get("batch", {}).get("window"),
            anomalies=payload.get("batch", {}).get("anomalies"),
            sub_stats=payload.get("batch", {}).get("sub_stats"),
            resource_kpis=payload.get("resource", {}).get("kpis"),
            servers=payload.get("resource", {}).get("servers"),
            sla_matrix=payload.get("sla_matrix"),
            benchmark=payload.get("benchmark"),
            sow_compare=payload.get("sow_compare"),
            customer_name=customer,
        )
        _rule_findings, _ = _generate(_fr_body)
        rule_engine_findings = [
            {"level": f.level, "text": f.text, "source": f.source,
             "evidence_class": f.evidence_class, "root_cause": f.root_cause}
            for f in _rule_findings
            if f.level in ("critical", "warning")
        ]
        rule_engine_summary = {
            "critical": sum(1 for f in _rule_findings if f.level == "critical"),
            "warning":  sum(1 for f in _rule_findings if f.level == "warning"),
        }
    except Exception as exc:
        logger.info("ai-findings: rule engine pre-pass failed (%s)", exc)

    schema_hint = (
        '{"findings":[{'
        '"level":"critical|warning|info|ok",'
        '"title":"<one-line headline, <=120 chars>",'
        '"evidence":"<quoted numbers / batches / hosts from the data>",'
        '"root_cause":"<one short cause OR empty>",'
        '"impact":"<business impact in one line>",'
        '"action":"<single concrete next step>",'
        '"confidence":<0-100 integer>,'
        '"sources":["sla_intel","batch","resource","benchmark","sow","red_flags"]'
        '}],'
        '"verdict":"<<=25 word executive summary>",'
        '"top_risk":"<<=20 word single biggest risk>"'
        "}"
    )

    # Include rule engine findings as ground truth in the prompt
    rule_block = ""
    if rule_engine_findings:
        rule_block = (
            "\n\nGROUND TRUTH — Rule Engine Findings (verified, deterministic):\n"
            "These findings were computed from the actual data by the PE rule engine. "
            "You MUST NOT contradict them. You MAY add cross-pillar insights the "
            "rule engine cannot detect, but you MUST include all CRITICAL findings "
            "below in your output (you may reword them).\n"
            f"{json.dumps(rule_engine_findings[:12], default=str)}\n"
            f"Rule engine totals: {rule_engine_summary}\n"
        )

    prompt = (
        f"CUSTOMER: {customer or '(unspecified)'}\n\n"
        "TASK: Produce 6-10 cross-correlated PE findings. Order: "
        "critical → warning → info → ok. Cite the pillar(s) each "
        "finding draws from in `sources`. Suppress trivial / "
        "duplicate observations. If a SLA contract has "
        "`business_acknowledged=true`, do NOT flag it as a breach. "
        "If `is_cyclic=true`, do NOT measure against the SLA window.\n\n"
        f"OUTPUT JSON SHAPE:\n{schema_hint}\n\n"
        f"DATA:\n{json.dumps(digest, default=str)[:16000]}"
        f"{rule_block}"
    )

    try:
        result, model = await asyncio.wait_for(
            asyncio.to_thread(
                chat_json, prompt,
                system=_FINDINGS_SYSTEM,
                max_tokens=2400, temperature=0.25,
            ),
            timeout=40.0,
        )
    except (asyncio.TimeoutError, RuntimeError) as exc:
        logger.warning("ai-findings: LLM unavailable (%s) — returning empty", exc)
        return {
            "findings": [],
            "summary": {"critical": 0, "warning": 0, "info": 0, "ok": 0, "total": 0},
            "model": "",
            "error": f"AI engine unavailable: {str(exc)[:160]}",
        }

    if isinstance(result, list):
        findings = result
        verdict, top_risk = "", ""
    elif isinstance(result, dict):
        raw = result.get("findings")
        if isinstance(raw, list):
            findings = raw
        elif isinstance(raw, dict):
            findings = [raw]                       # single object → wrap
        else:
            findings = []
        verdict  = str(result.get("verdict")  or "")
        top_risk = str(result.get("top_risk") or "")
    else:
        findings, verdict, top_risk = [], "", ""

    # Normalise + clamp
    norm: List[Dict[str, Any]] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        lvl = (f.get("level") or "info").lower()
        if lvl not in ("critical", "warning", "info", "ok"):
            lvl = "info"
        norm.append({
            "level":      lvl,
            "title":      str(f.get("title") or "").strip()[:200],
            "evidence":   str(f.get("evidence") or "").strip()[:600],
            "root_cause": str(f.get("root_cause") or "").strip()[:200],
            "impact":     str(f.get("impact") or "").strip()[:300],
            "action":     str(f.get("action") or "").strip()[:300],
            "confidence": max(0, min(100, int(f.get("confidence") or 0))),
            "sources":    [str(s)[:30] for s in (f.get("sources") or [])][:6],
        })

    order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    norm.sort(key=lambda x: order.get(x["level"], 9))

    # ── Post-LLM validation: ensure rule-engine criticals aren't dropped ──
    # If the rule engine found CRITICAL findings that the LLM omitted,
    # inject them so no real blocker is ever hidden.
    if rule_engine_findings:
        llm_crit_count = sum(1 for f in norm if f["level"] == "critical")
        rule_crit_count = rule_engine_summary.get("critical", 0)
        if llm_crit_count < rule_crit_count:
            # Find which rule-engine criticals the LLM missed
            llm_titles_blob = " ".join(f.get("title", "").lower() for f in norm)
            for rf in rule_engine_findings:
                if rf["level"] != "critical":
                    continue
                # Check if any LLM finding covers this rule finding
                rc_key = (rf.get("root_cause") or "").lower()
                rf_text = (rf.get("text") or "").lower()
                # Match by root cause or key terms in the text
                covered = False
                if rc_key and rc_key in llm_titles_blob:
                    covered = True
                # Check key terms: "breach", "misleading", "cpu saturation", etc.
                key_terms = [w for w in rf_text.split() if len(w) > 5][:4]
                if not covered and key_terms:
                    matches = sum(1 for t in key_terms if t in llm_titles_blob)
                    if matches >= 2:
                        covered = True
                if not covered:
                    # Inject the rule-engine finding the LLM missed
                    norm.insert(0, {
                        "level":      "critical",
                        "title":      rf.get("text", "Rule engine critical finding")[:200],
                        "evidence":   f"Source: {rf.get('source', 'rule-engine')} · "
                                      f"Evidence class: {rf.get('evidence_class', 'measured')}",
                        "root_cause": rf.get("root_cause", ""),
                        "impact":     "Critical finding from deterministic rule engine — "
                                      "LLM analysis omitted this; re-injected for accuracy",
                        "action":     "Investigate and resolve before PE sign-off",
                        "confidence": 100,
                        "sources":    [rf.get("source", "rule-engine")],
                    })

    # ── Severity validation against actual KPIs ──────────────────────
    # Check for "ok" or "info" findings that claim compliance is fine but
    # actual data shows breaches
    batch_kpis = payload.get("batch", {}).get("kpis") or {}
    actual_breaches = int(batch_kpis.get("jobs_breach") or 0)
    actual_compliance = batch_kpis.get("compliance_pct")
    for f in norm:
        title_lower = (f.get("title") or "").lower()
        # Catch LLM saying "no breaches" or "100% compliance" when data says otherwise
        if f["level"] in ("ok", "info") and ("complian" in title_lower or "sla" in title_lower):
            if actual_breaches > 0 and ("no breach" in title_lower or "all within" in title_lower):
                f["level"] = "warning"
                f["title"] = f["title"] + f" [CORRECTED: {actual_breaches} actual breach(es)]"
            if actual_compliance is not None and actual_compliance < 90:
                if "100%" in f.get("title", "") or "all within" in title_lower:
                    f["level"] = "warning"
                    f["title"] = f["title"] + f" [CORRECTED: actual compliance {actual_compliance:.1f}%]"

    # Re-sort after potential injections/corrections
    norm.sort(key=lambda x: order.get(x["level"], 9))

    summary = {
        "critical": sum(1 for f in norm if f["level"] == "critical"),
        "warning":  sum(1 for f in norm if f["level"] == "warning"),
        "info":     sum(1 for f in norm if f["level"] == "info"),
        "ok":       sum(1 for f in norm if f["level"] == "ok"),
        "total":    len(norm),
    }

    resp = {
        "findings": norm,
        "summary":  summary,
        "verdict":  verdict.strip()[:400],
        "top_risk": top_risk.strip()[:300],
        "model":    model,
    }
    try:
        session_cache.set("last_findings", resp)
    except Exception:
        pass
    return resp
