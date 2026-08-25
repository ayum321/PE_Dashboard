"""
Agent router — exposes the tool-using LLM agent over HTTP.

POST /api/agent/deep-dive
    body: { "question": "...", "scope": "general|finding|breach|verdict",
            "context": { ...optional caller-supplied hints... } }
    returns: { answer, model, trace, tool_count, scope }

The frontend Deep-Dive button posts here. The agent reads ALL its evidence
from session_cache (populated on every upload / SLA-matrix run / findings
run), so the only payload the UI has to send is the question.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()
log = logging.getLogger("pe_dashboard.agent")


class DeepDiveRequest(BaseModel):
    question: str                       = Field(..., description="What to investigate")
    scope:    str                       = Field("general",
                                                 description="general | finding | breach | verdict")
    context:  Optional[Dict[str, Any]]  = Field(None,
                                                 description="Optional hints (job_name, host, finding_id, etc.)")


class TraceStep(BaseModel):
    step:    int
    kind:    str                  # "tool_call" | "answer" | "error"
    name:    Optional[str]   = None
    args:    Optional[Any]   = None
    result:  Optional[Any]   = None
    content: Optional[str]   = None
    error:   Optional[str]   = None
    fallback_reason: Optional[str] = None


class DeepDiveResponse(BaseModel):
    answer:     str
    model:      str
    scope:      str
    tool_count: int
    trace:      list[TraceStep]


@router.post(
    "/agent/deep-dive",
    response_model=DeepDiveResponse,
    summary="Run the tool-using LLM agent on a single question. Cites real evidence from session_cache.",
)
def deep_dive(body: DeepDiveRequest) -> DeepDiveResponse:
    from services.ai_agent import run_agent

    # Compose the actual task by attaching context (so the agent doesn't
    # have to guess which job/host the user is asking about).
    ctx_lines: list[str] = []
    if body.context:
        for k, v in body.context.items():
            if v is None or v == "":
                continue
            ctx_lines.append(f"  - {k}: {v}")
    task = body.question
    if ctx_lines:
        task = (
            f"{body.question}\n\n"
            f"CONTEXT (from the UI panel the user clicked from):\n"
            + "\n".join(ctx_lines)
        )

    system_by_scope = {
        "general": (
            "You are a Senior Performance Engineering analyst. Use the tools "
            "to fetch evidence from the uploaded customer data, then deliver "
            "a tight, numeric, no-filler answer. NEVER invent hostnames, job "
            "names or numbers \u2014 every identifier you cite must come from a "
            "tool result. If the data does not support a claim, say so."
        ),
        "finding": (
            "You are confirming or refuting a single PE finding. Use tools "
            "to inspect the underlying rows. NEVER invent identifiers. "
            "Output structure:\n"
            "  STATUS: CONFIRMED | DISPUTED | INCONCLUSIVE\n"
            "  ROOT CAUSE: <one sentence>\n"
            "  EVIDENCE:\n"
            "    - <exact numbers, hostnames, job names from tool output>\n"
            "  NEXT ACTION: <one sentence>"
        ),
        "breach": (
            "You are diagnosing a specific SLA breach. Always call "
            "get_job_history first (use a real job_name from list_jobs or "
            "the inventory), then get_resource_linked_runs and "
            "get_host_metrics for any hosts referenced. NEVER invent hosts "
            "or jobs. Output:\n"
            "  VERDICT: RESOURCE_DRIVEN | JOB_REGRESSION | DATA_VOLUME | UNCLEAR\n"
            "  EVIDENCE:\n"
            "    - <numbers + tool>\n"
            "  CONFIDENCE: LOW | MEDIUM | HIGH\n"
            "  NEXT ACTION: <one sentence>"
        ),
        "verdict": (
            "You are the Senior PE Consultant producing the unified verdict. "
            "Inspect findings, red flags, SLA matrix and resource pressure "
            "via tools. NEVER invent hostnames or job names \u2014 if the "
            "resource report is missing, say so explicitly and answer using "
            "the data that IS available. Output:\n"
            "  VERDICT: <one line, includes grade and decision>\n"
            "  TOP 3 RISKS:\n"
            "    1. <risk> \u2014 evidence: <exact numbers>\n"
            "  PREDICTIONS (with confidence %):\n"
            "    - <prediction> [confidence X%]\n"
            "  NEXT 48H ACTIONS:\n"
            "    1. <action>\n"
            "  ACCURACY NOTE: <one line>"
        ),
    }
    system = system_by_scope.get(body.scope) or system_by_scope["general"]
    # Append universal tool-calling discipline to every scope
    system += (
        "\n\nTOOL-CALLING RULES:\n"
        "  • Call tools by their EXACT registered name only. Never append channel "
        "tokens, angle-bracket tags, or any suffix to a tool name (e.g. use "
        "'list_findings', never 'list_findings<channel>commentary').\n"
        "  • Tool arguments must be plain JSON — no channel or sentinel tokens.\n"
        "  • If you are unsure which tool to call, call list_loaded_data first."
    )

    try:
        text, model, trace = run_agent(task=task, system=system)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent.deep_dive failed: %s", exc)
        return DeepDiveResponse(
            answer=f"Agent failed: {exc}", model="deterministic",
            scope=body.scope, tool_count=0, trace=[],
        )

    return DeepDiveResponse(
        answer=text or "",
        model=model or "",
        scope=body.scope,
        tool_count=sum(1 for t in trace if t.get("kind") == "tool_call"),
        trace=[TraceStep(**t) for t in trace],
    )
