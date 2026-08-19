"""
data_reviewer — post-load AI sanity check using OpenAI gpt-oss-120b.

After Ctrl-M batch + resource files are loaded, the dashboard's KPIs are
computed by deterministic Python (services.batch_calculator,
services.resource_calculator, services.sla_engine). Those numbers are
trustworthy by construction — but the **interpretation layer** (which
job is "the worst", which server is "saturated", which finding is
"misleading green") still benefits from a reasoning-model second pass.

This service does exactly that: it bundles a compact digest of every
loaded pillar and asks `openai/gpt-oss-120b` (a true reasoning model
served via NVIDIA NIM) to:

    1. Cross-check the headline numbers for internal contradictions
    2. Flag any metric that looks anomalous given the others
    3. Suggest concrete corrections (e.g. "compliance shown as 96% but
       breach count is 4 — recompute compliance excluding waivered runs")
    4. Return a strict JSON corrections list the UI can apply

The model returns:
    {
      "verdict": "CLEAN" | "CORRECTIONS_REQUIRED",
      "confidence": 0-100,
      "summary": "<one paragraph>",
      "corrections": [
        {"field": "batch.compliance_pct", "current": 96.0,
         "suggested": 88.5, "reason": "...", "severity": "high|med|low"}
      ],
      "anomalies": [...],
      "internal_contradictions": [...]
    }

This is best-effort. If the reasoning model is unreachable, the endpoint
falls back to a deterministic in-process consistency checker so the user
still gets an honest signal that the data passed sanity checks.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("pe_dashboard.data_reviewer")

# ── System role for the reviewer model ─────────────────────────────────────
_SYSTEM = (
    "You are a Senior Performance Engineering reviewer auditing the OUTPUT "
    "of a deterministic Python pipeline. Your job is NOT to recompute the "
    "numbers — you trust the calculations. Your job is to spot:\n"
    "  • internal contradictions (e.g. 0 breaches but 96% compliance and "
    "    4 jobs marked BREACH)\n"
    "  • anomalies that suggest a parser bug or stale data\n"
    "  • interpretive errors in classification (severity, status, RCA)\n"
    "  • misleading-green situations (numbers look fine but elapsed window "
    "    or per-job baselines say otherwise)\n"
    "Be concise and specific. Cite exact numbers. Never invent fields."
)

_PROMPT = (
    "Review the following dashboard data digest and return a STRICT JSON "
    "object with this exact shape:\n"
    "{\n"
    '  "verdict": "CLEAN" or "CORRECTIONS_REQUIRED",\n'
    '  "confidence": 0-100,\n'
    '  "summary": "one paragraph, <= 280 chars",\n'
    '  "corrections": [\n'
    '    {"field": "<dotted path>", "current": <value>, "suggested": <value>,\n'
    '     "reason": "...", "severity": "high"|"med"|"low"}\n'
    '  ],\n'
    '  "anomalies": ["<short string>", ...],\n'
    '  "internal_contradictions": ["<short string>", ...]\n'
    "}\n"
    "If everything is consistent, set verdict=CLEAN with empty arrays. "
    "Do NOT include any prose outside the JSON object. "
    "Do NOT use markdown fences."
)


def _digest(payload: dict) -> dict:
    """Trim the payload down to the headline numbers (keeps prompt < 8KB)."""
    out: dict[str, Any] = {}

    # Batch headline
    batch = (payload or {}).get("batch") or {}
    if batch:
        bk = batch.get("kpis") or {}
        out["batch"] = {
            "compliance_pct":   bk.get("compliance_pct"),
            "total_jobs":       bk.get("total_jobs"),
            "total_runs":       bk.get("total_runs"),
            "jobs_breach":      bk.get("jobs_breach"),
            "jobs_at_risk":     bk.get("jobs_at_risk"),
            "window_breach_days": bk.get("window_breach_days"),
            "window_total_days":  bk.get("window_total_days"),
            "elapsed_window":   bk.get("elapsed_window"),
            "summed_runtime":   bk.get("summed_runtime"),
            "data_coverage":    (bk.get("data_coverage") or {}).get("confidence_label"),
        }
        # Top 5 jobs only — keep payload small
        out["batch"]["top_jobs_brief"] = [
            {"job": (j.get("Job_Name") or j.get("job_name")),
             "peak_hrs": j.get("peak_hrs"),
             "buffer_status": j.get("buffer_status")}
            for j in (batch.get("top_jobs") or [])[:5]
        ]

    # Resource headline
    res = (payload or {}).get("resource") or {}
    if res:
        rk = res.get("kpis") or {}
        out["resource"] = {
            "fleet_grade":  rk.get("fleet_grade"),
            "fleet_score":  rk.get("fleet_score"),
            "total_servers": rk.get("total_servers"),
            "n_critical":   rk.get("n_critical"),
            "n_warning":    rk.get("n_warning"),
            "n_healthy":    rk.get("n_healthy"),
            "n_agg_trap":   rk.get("n_agg_trap"),
            "n_dual_pressure": rk.get("n_dual_pressure"),
            "avg_cpu":      rk.get("avg_cpu"),
            "avg_mem":      rk.get("avg_mem"),
            "avg_disk":     rk.get("avg_disk"),
            "data_quality": rk.get("data_quality"),
        }
        servers = res.get("servers") or []
        out["resource"]["top_hot_servers"] = [
            {"host": (s.get("host") or s.get("server", "")).split(".")[0],
             "type": s.get("type"),
             "cpu":  s.get("cpu_pct"),
             "mem":  s.get("mem_pct"),
             "disk": s.get("disk_pct"),
             "status": s.get("status"),
             "agg_trap": bool(s.get("agg_trap")),
             "dual_pressure": bool(s.get("dual_pressure"))}
            for s in sorted(servers,
                            key=lambda x: (x.get("cpu_pct") or 0)
                                          + (x.get("mem_pct") or 0),
                            reverse=True)[:8]
        ]

    # SLA matrix headline
    sla = (payload or {}).get("sla_matrix") or {}
    if sla:
        out["sla_matrix"] = {
            "sla_label":      sla.get("sla_label"),
            "sla_limit_hrs":  sla.get("sla_limit_hrs"),
            "compliance_pct": sla.get("compliance_pct"),
            "breaching_runs": sla.get("breaching_runs"),
            "at_risk_runs":   sla.get("at_risk_runs"),
            "ok_runs":        sla.get("ok_runs"),
            "worst_job":      sla.get("worst_job"),
            "worst_hrs":      sla.get("worst_hrs"),
            "outliers_count": len(sla.get("outliers") or []),
            "resource_linked_count": len(sla.get("resource_linked") or []),
        }

    # Findings summary (already deduplicated by smart_findings)
    findings = (payload or {}).get("findings") or {}
    if findings:
        out["findings_summary"] = findings.get("summary") or {}
        out["smart_verdict"]    = (findings.get("smart") or {}).get("verdict")

    # Customer + dataset context
    out["customer_name"] = (payload or {}).get("customer_name")
    return out


def _deterministic_review(digest: dict) -> dict:
    """Fallback when the reasoning model is unreachable. Catches the
    obvious internal contradictions without an LLM."""
    contradictions: list[str] = []
    anomalies:      list[str] = []
    corrections:    list[dict] = []

    b = digest.get("batch") or {}
    # 1. compliance vs breach contradiction
    comp = b.get("compliance_pct")
    breaches = b.get("jobs_breach") or 0
    if comp is not None and comp >= 95 and breaches >= 1:
        contradictions.append(
            f"Compliance shown as {comp:.1f}% but {breaches} job(s) marked as BREACH"
        )

    # 2. elapsed vs summed (misleading-green)
    el = (b.get("elapsed_window") or {}).get("worst_day") or {}
    sm = b.get("summed_runtime") or {}
    el_h = (el.get("elapsed_hrs") or 0)
    sm_h = (sm.get("worst_day_hrs") or 0)
    if el_h and sm_h and el_h > sm_h * 1.3:
        anomalies.append(
            f"Elapsed window {el_h:.1f}h is {(el_h/max(sm_h,0.1)-1)*100:.0f}% larger "
            f"than summed runtime {sm_h:.1f}h — orchestration idle time"
        )

    r = digest.get("resource") or {}
    # 3. fleet grade vs critical count
    n_crit = r.get("n_critical") or 0
    grade  = (r.get("fleet_grade") or "").upper()
    if grade in ("A", "B") and n_crit > 0:
        contradictions.append(
            f"Fleet grade {grade} but {n_crit} server(s) marked CRITICAL"
        )

    # 4. agg-trap dilution
    agg = r.get("n_agg_trap") or 0
    if agg > 0 and n_crit > 0 and agg >= n_crit:
        anomalies.append(
            f"All {n_crit} CRITICAL servers may be aggregation-trap false alarms ({agg} flagged)"
        )

    sla = digest.get("sla_matrix") or {}
    # 5. SLA matrix contradictions
    s_breach = sla.get("breaching_runs") or 0
    s_comp   = sla.get("compliance_pct")
    if s_comp is not None and s_comp >= 95 and s_breach > 0:
        contradictions.append(
            f"SLA compliance {s_comp:.1f}% but {s_breach} run(s) breaching"
        )

    verdict = "CORRECTIONS_REQUIRED" if (contradictions or corrections) else "CLEAN"
    return {
        "verdict":     verdict,
        "confidence":  60,
        "summary":     ("Deterministic consistency check ran without the reasoning model. "
                        f"Found {len(contradictions)} contradiction(s), {len(anomalies)} anomaly(ies)."),
        "corrections": corrections,
        "anomalies":   anomalies,
        "internal_contradictions": contradictions,
        "engine":      "deterministic",
    }


def review(payload: dict, *, prefer_model: str = "openai/gpt-oss-120b") -> dict:
    """Run the post-load reasoning review.

    Returns a dict with the strict shape described in the module docstring.
    Always returns something — even when the LLM is unreachable.
    """
    digest = _digest(payload or {})

    try:
        from services.ai_engine import (
            chat as _chat, is_ready, _NIM_DEFAULT_WATERFALL,
        )
        ready = is_ready()
    except Exception as exc:
        log.info("data_reviewer: ai_engine import failed (%s)", exc)
        return _deterministic_review(digest)

    if not ready.get("nvidia_key"):
        return _deterministic_review(digest)

    # Force the reasoning waterfall: gpt-oss-120b first, then -20b
    body = json.dumps(digest, default=str)
    if len(body) > 12000:
        body = body[:12000] + " …<truncated>"
    prompt = f"{_PROMPT}\n\nDATA DIGEST:\n{body}"

    # We rely on chat() to honour ai_text_model — temporarily override via
    # config_store would be invasive, so instead we call the NIM driver
    # directly with prefer_model and fall back to standard waterfall.
    from services.ai_engine import _call_nim
    from services.config_store import get_nvidia_key
    nv = get_nvidia_key()

    candidates = [prefer_model, "openai/gpt-oss-20b"]
    candidates += [m for m in _NIM_DEFAULT_WATERFALL if m not in candidates]

    for model in candidates:
        try:
            out = _call_nim(prompt, _SYSTEM, nv, model,
                            max_tokens=1500, temperature=0.2,
                            json_mode=True, timeout=45)
        except Exception as exc:
            log.info("data_reviewer: %s call failed (%s)", model, exc)
            out = None
        if not out:
            continue
        # Parse JSON
        cleaned = out.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            parsed = json.loads(cleaned)
        except Exception:
            # Try to extract the largest JSON object substring
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", cleaned)
            if not m:
                continue
            try:
                parsed = json.loads(m.group())
            except Exception:
                continue

        # Normalize the shape so the UI never crashes on missing fields
        return {
            "verdict":     str(parsed.get("verdict", "CLEAN")).upper(),
            "confidence":  int(parsed.get("confidence", 75) or 0),
            "summary":     str(parsed.get("summary", ""))[:500],
            "corrections": list(parsed.get("corrections", []))[:20],
            "anomalies":   list(parsed.get("anomalies", []))[:20],
            "internal_contradictions": list(parsed.get("internal_contradictions", []))[:20],
            "engine":      f"nvidia:{model}",
        }

    log.info("data_reviewer: no NIM model returned valid JSON, falling back")
    return _deterministic_review(digest)
