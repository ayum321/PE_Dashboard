"""
ai_narrator — thin convenience layer on top of services.ai_engine.

Every analysis router (batch, resource, correlation, sow, executive,
benchmark, sla_matrix, redflags) calls `narrate(topic, digest)` to attach
a short, evidence-led narrative to its response.  All calls go through
the unified ai_engine waterfall (Gemma → Llama-3.3 → Llama-3.1 → Nemotron
→ Mixtral → Gemini), so adding/removing providers happens in one place.

The narrator is best-effort: any failure (no key, API down, timeout) returns
(None, None) and the caller's response is unaffected.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple

log = logging.getLogger("pe_dashboard.ai_narrator")

# Default system role — every PE narrative shares this voice
_SYSTEM = (
    "You are a Senior Performance Engineering consultant briefing a CTO. "
    "Be tight, specific, and evidence-led: quote exact numbers, hostnames, "
    "job names, and percentages from the data. No filler, no apologies, "
    "no generic advice. If data is empty say so in one line."
)

# Per-topic prompt templates — kept short so token use stays low
_TEMPLATES: dict[str, str] = {
    "resource": (
        "Resource utilisation report ingested. In 5 lines: "
        "(1) fleet health one-liner with grade, "
        "(2) top 3 servers by stress (host + CPU/MEM/DISK %), "
        "(3) any mis-classifications visible, "
        "(4) the single biggest risk, "
        "(5) one immediate action."
    ),
    "batch": (
        "Ctrl-M batch run report ingested. In 4 lines: "
        "(1) SLA compliance + total runs, "
        "(2) top 3 breaching jobs with hours/duration, "
        "(3) failure rate context vs target, "
        "(4) one immediate stabilisation action."
    ),
    "sla_matrix": (
        "Batch SLA matrix computed. In 4 lines: "
        "(1) overall pass/at-risk/breach split, "
        "(2) the worst window (daily/weekly/monthly) and why, "
        "(3) the worst 2-3 jobs that drag the matrix, "
        "(4) one immediate action."
    ),
    "benchmark": (
        "UI / perf benchmark comparison ingested. In 4 lines: "
        "(1) overall verdict (improved / steady / regressed), "
        "(2) top 3 transactions with biggest delta + numbers, "
        "(3) likely root cause pattern from the deltas, "
        "(4) one diagnostic action."
    ),
    "correlation": (
        "Cross-pillar correlation computed. In 4 lines: "
        "(1) the strongest signal linking batch ↔ resource ↔ SLA, "
        "(2) which pillar is the leading indicator, "
        "(3) the single biggest compounded risk, "
        "(4) one cross-team action."
    ),
    "sow": (
        "SOW vs actual volume comparison computed. In 4 lines: "
        "(1) overall variance one-liner, "
        "(2) top 3 contracted lines that are over/under, "
        "(3) commercial / capacity implication, "
        "(4) one renegotiation / planning action."
    ),
    "executive": (
        "Executive dashboard data computed. Output EXACTLY 5 labeled lines — "
        "no preamble, no reasoning, no markdown, no numbering. "
        "Each line must start with its label followed by a colon:\n"
        "COVERAGE: <overall posture grade + score + what was measured>\n"
        "RISK: <top risk with exact numbers — breach days, SRI, RFCS>\n"
        "CAUSE: <root cause — resource pressure or scheduler config — with hostnames/job names>\n"
        "IMPACT: <business effect — fail rate, SLA breaches, delivery risk>\n"
        "ACTION: <single recommended decision with concrete next step>\n"
        "Quote exact numbers. No other text before or after."
    ),
    "redflags": (
        "Performance-engineering red-flag scan complete. In 4 lines: "
        "(1) overall posture, "
        "(2) the single most damaging flag and why, "
        "(3) the cluster of related risks, "
        "(4) one immediate action."
    ),
    "final_judgment": (
        "You have ALL pillars of a Performance Engineering audit: "
        "resource, batch, SLA matrix, benchmark, correlation, SOW, "
        "and red-flags. The score has already been computed deterministically — "
        "your job is to EXPLAIN it, not recompute it. Use ONLY these supplied "
        "fields:\n"
        "- 'evidence_ledger': the authoritative list of facts that moved the "
        "score, each with the exact points it removed. Every risk you name MUST "
        "come from here.\n"
        "- 'pillar_score_detail': each pillar's base score → final score after "
        "severity penalties.\n"
        "- 'cross_pillar_links': PRE-COMPUTED correlations between pillars. Restate "
        "these verbatim in the CROSS-PILLAR LINKS section — do NOT invent new links.\n"
        "- 'evidence_facts' and 'verdict_reason': the named compliance/breach facts.\n"
        "The verdict MUST cite which evidence fact(s) drove the decision. Do not "
        "invent context or numbers not present in the payload. Produce a UNIFIED "
        "VERDICT in this exact structure:\n"
        "VERDICT: <one sentence overall grade citing the top driver fact + its points>\n"
        "TOP RISKS:\n"
        "  1. <risk from evidence_ledger> — <fact with numbers and points removed>\n"
        "  2. <risk from evidence_ledger> — <fact with numbers and points removed>\n"
        "  3. <risk from evidence_ledger> — <fact with numbers and points removed>\n"
        "CROSS-PILLAR LINKS: <restate the supplied cross_pillar_links; if none, say "
        "'no significant cross-pillar correlations computed'>\n"
        "DECISION: <go / hold / remediate-then-go>\n"
        "NEXT 48H: <up to 3 concrete actions targeting the highest-points risks>\n"
    ),
    "smart_verdict_15w": (
        "You are reviewing the deterministic findings of a PE audit. "
        "Produce ONE sentence of EXACTLY 12-18 words explaining the verdict. "
        "Lead with the most damaging measurable fact. No greeting, no hedging, "
        "no bullet points. Plain text only — no markdown, no quotes."
    ),
    "findings_enrich": (
        "You are a Senior Performance Engineering consultant. "
        "You receive a list of PE audit findings that need structured enrichment. "
        "For EACH finding in 'findings_to_enrich', return a JSON array where each element has:\n"
        "  {\"id\": <same integer id>, \"root_cause\": \"<5-10 word technical root cause category>\", "
        "\"impact\": \"<one sentence, max 20 words, quantifying business impact>\", "
        "\"recommendation\": \"<one imperative sentence, max 15 words, specific action>\"}\n\n"
        "RULES:\n"
        "- Use exact numbers from 'kpi_context' when available — never fabricate metrics.\n"
        "- root_cause must be a concise technical label (e.g. 'CPU resource contention', 'batch window scheduling gap').\n"
        "- impact must state a consequence, not repeat the finding title.\n"
        "- recommendation must be a single imperative sentence starting with a verb.\n"
        "- If a finding already has a non-empty root_cause/impact/action, keep it — only fill blanks.\n"
        "- Return ONLY the JSON array, no markdown fences, no explanation."
    ),
    "findings_rca_verdict": (
        "You are a Senior Performance Engineering consultant writing the RCA "
        "verdict for an automated PE audit. You receive structured verdict lines "
        "generated from real data. Your task: rewrite them into exactly 5-7 lines "
        "of clear, technically precise, functionally correct English.\n\n"
        "RULES:\n"
        "- Each line addresses one dimension: scope, compliance, root cause, "
        "impact, evidence quality, decision, primary blocker.\n"
        "- Quote exact numbers from the input (never fabricate).\n"
        "- Use active voice. No promotional language. No filler words.\n"
        "- Write as a PE consultant briefing a CTO — direct, measured, factual.\n"
        "- Do NOT use bullet points, numbering, markdown, or labels like "
        "'Scope:' or 'Impact:'. Write flowing sentences.\n"
        "- Do NOT use em dashes or semicolons excessively.\n"
        "- Do NOT start consecutive sentences with the same word.\n"
        "- Each line should be a single complete sentence, 15-25 words.\n"
        "- The tone should be calm authority, not alarm or hype.\n\n"
        "Output ONLY the 5-7 lines of plain text, nothing else."
    ),
}


def narrate(
    topic: str,
    digest: Any,
    *,
    extra_instructions: str = "",
    max_tokens: int = 480,
    temperature: float = 0.3,
) -> Tuple[Optional[str], Optional[str]]:
    """Generate an AI narrative for a router response.

    Args:
        topic: One of the keys in `_TEMPLATES` (resource, batch, sla_matrix,
            benchmark, correlation, sow, executive, redflags, final_judgment).
            Unknown topics fall back to a generic prompt.
        digest: Any JSON-serialisable object describing the result data.
            Will be truncated to ~16 KB to stay within model limits.
        extra_instructions: Optional caller-specific addendum.
        max_tokens: Generation budget (defaults to 480 — enough for ~6 lines).
        temperature: Sampling temperature (default 0.3 — keeps output factual).

    Returns:
        (narrative_text, model_id) or (None, None) on any failure.
    """
    try:
        from services.ai_engine import chat as _ai_chat, is_ready
        ready = is_ready()
        if not (ready.get("nvidia_key") or ready.get("gemini_key")):
            return None, None
    except Exception as exc:  # noqa: BLE001
        log.info("ai_narrator: engine unavailable (%s)", exc)
        return None, None

    template = _TEMPLATES.get(topic) or (
        f"Briefly summarise this {topic} data in 4 lines: what the data "
        "shows, biggest signal, biggest risk, recommended next step."
    )
    if extra_instructions:
        template = f"{template}\n\nADDITIONAL FOCUS: {extra_instructions}"

    try:
        body = json.dumps(digest, default=str)
    except Exception:
        body = str(digest)
    if len(body) > 16000:
        body = body[:16000] + " …<truncated>"

    prompt = f"{template}\n\nDATA:\n{body}"

    try:
        text, model = _ai_chat(
            prompt, system=_SYSTEM,
            max_tokens=max_tokens, temperature=temperature,
        )
        if not text:
            return None, model
        # Strip chain-of-thought blocks emitted by reasoning models
        # (<think>...</think> or <reasoning>...</reasoning>)
        import re as _re
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
        text = text.strip()
        return (text or None), model
    except Exception as exc:  # noqa: BLE001
        log.info("ai_narrator: narration failed for topic=%s (%s)", topic, exc)
        return None, None
