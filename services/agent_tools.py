"""
agent_tools — read-only data lookup functions exposed to the LLM agent.

The LLM cannot fabricate evidence: every claim it makes must come from one
of these tools. Each tool reads the *real* uploaded data from session_cache
(populated by routers/upload, routers/batch, routers/sla_matrix,
routers/findings, routers/redflags) and returns small, focused JSON blocks.

Design rules:
  * Every tool is read-only — no mutation of cached state.
  * Every tool is fail-soft — missing data returns ``{"error": "..."}``
    rather than raising.
  * Every tool returns at most ~2 KB of JSON so the agent can fit dozens
    of tool calls in its context window.
  * The schema (``TOOL_SCHEMAS``) follows OpenAI's function-calling spec,
    which NVIDIA NIM accepts on every tool-capable model
    (gpt-oss-120b/20b, llama-3.3-70b-instruct, mixtral-8x22b).

The agent only ever sees the JSON output, never the Python object — so the
shapes here are the contract.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from services import session_cache


# ── small helpers ─────────────────────────────────────────────
def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _job_match(name: str, target: str) -> bool:
    if not name or not target:
        return False
    n = str(name).lower()
    t = str(target).lower()
    return t == n or t in n or n in t


def _host_match(host: str, target: str) -> bool:
    if not host or not target:
        return False
    h = str(host).lower().split(".")[0]
    t = str(target).lower().split(".")[0]
    return t == h or t in h or h in t


def _trim(rows: List[dict], n: int) -> List[dict]:
    return list(rows or [])[:max(0, n)]


# ════════════════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS
#  Each function takes only JSON-friendly args and returns a dict
#  containing primitive Python types (json.dumps-able).
# ════════════════════════════════════════════════════════════════

def _host_of(s: dict) -> str:
    return str(s.get("host") or s.get("server") or s.get("name") or "").strip()


def list_loaded_data() -> dict:
    """Inventory of what's in the session — the agent calls this first.

    Returns concrete sample identifiers (real hostnames, job names) so the
    agent never has to guess what to pass to tools like get_host_metrics or
    get_job_history. If a list is empty, the inventory says so explicitly.
    """
    batch    = session_cache.get("last_batch")
    sla      = session_cache.get("last_sla_matrix")
    res      = session_cache.get("last_resource") or {}
    findings = session_cache.get("last_findings")
    flags    = session_cache.get("last_red_flags")

    inv: dict = {
        "batch_loaded":        bool(batch),
        "sla_matrix_loaded":   bool(sla),
        "resource_loaded":     bool(res.get("servers")),
        "findings_loaded":     bool(findings),
        "red_flags_loaded":    bool(flags),
    }

    if batch:
        k = batch.get("kpis") or {}
        inv["batch_summary"] = {
            "filename":       batch.get("filename"),
            "total_runs":     k.get("total_runs"),
            "total_jobs":     k.get("total_jobs"),
            "compliance_pct": k.get("compliance_pct"),
            "jobs_breach":    k.get("jobs_breach"),
            "fail_rate_pct":  k.get("fail_rate_pct"),
        }

    if sla:
        outliers = sla.get("outliers") or []
        breaches = sla.get("breaches") or []
        # Sample real job names so the agent has concrete handles
        sample_outlier_jobs = [r.get("job_name") for r in outliers[:5] if r.get("job_name")]
        sample_breach_jobs  = [r.get("job_name") for r in breaches[:5] if r.get("job_name")]
        inv["sla_summary"] = {
            "compliance_pct":     sla.get("compliance_pct"),
            "breaching_runs":     sla.get("breaching_runs"),
            "at_risk_runs":       sla.get("at_risk_runs"),
            "sla_limit_hrs":      sla.get("sla_limit_hrs"),
            "worst_job":          sla.get("worst_job"),
            "n_outliers":         len(outliers),
            "n_resource_linked":  len(sla.get("resource_linked") or []),
            "sample_outlier_jobs": sample_outlier_jobs,
            "sample_breach_jobs":  sample_breach_jobs,
        }

    if res.get("servers"):
        servs = res["servers"]
        # Real hostnames, in stress order, so the agent can call
        # get_host_metrics with valid arguments
        ranked = sorted(
            servs,
            key=lambda s: (_f(s.get("cpu_pct")) + _f(s.get("mem_pct"))),
            reverse=True,
        )
        inv["resource_summary"] = {
            "n_servers":     len(servs),
            "n_db":          sum(1 for s in servs if (s.get("type") or "").upper() == "DB"),
            "n_app":         sum(1 for s in servs if (s.get("type") or "").upper() == "APP"),
            "all_hostnames": [_host_of(s) for s in servs if _host_of(s)][:30],
            "top_stressed":  [
                {
                    "host":     _host_of(s),
                    "type":     s.get("type"),
                    "status":   s.get("status"),
                    "cpu_pct":  s.get("cpu_pct"),
                    "mem_pct":  s.get("mem_pct"),
                }
                for s in ranked[:5] if _host_of(s)
            ],
        }
    else:
        inv["resource_summary"] = {
            "n_servers": 0,
            "note": (
                "No resource report uploaded. get_host_metrics and "
                "get_critical_servers will return empty. Do not invent hostnames."
            ),
        }

    if findings:
        inv["findings_summary"] = findings.get("summary") or {}
    if flags:
        inv["red_flags_summary"] = {
            "total":   flags.get("total"),
            "by_risk": flags.get("by_risk"),
        }

    # Hint the agent on which tools are most useful given the loaded state.
    suggestions: List[str] = []
    if not res.get("servers"):
        suggestions.append("resource not loaded → skip host-level tools")
    if sla and not (sla.get("breaches") or sla.get("outliers")):
        suggestions.append("SLA is clean (no breaches, no outliers) → focus on "
                           "baselines and findings, not breach tools")
    if suggestions:
        inv["hints"] = suggestions
    return inv


def list_hosts() -> dict:
    """All known hostnames + their type/status. Use this BEFORE get_host_metrics."""
    res = session_cache.get("last_resource") or {}
    servs = res.get("servers") or []
    if not servs:
        return {
            "n_total": 0,
            "rows": [],
            "error": (
                "No resource report uploaded. There are no hosts to query. "
                "Do not invent hostnames in the answer."
            ),
        }
    rows = []
    for s in servs:
        host = _host_of(s)
        if not host:
            continue
        rows.append({
            "host":     host,
            "type":     s.get("type"),
            "status":   s.get("status"),
            "cpu_pct":  s.get("cpu_pct"),
            "mem_pct":  s.get("mem_pct"),
            "disk_pct": s.get("disk_pct"),
        })
    rows.sort(key=lambda r: (_f(r.get("cpu_pct")) + _f(r.get("mem_pct"))), reverse=True)
    return {"n_total": len(rows), "rows": rows[:30]}


def list_jobs(limit: int = 25, kind: Optional[str] = None) -> dict:
    """All known job names from the SLA matrix. Use BEFORE get_job_history.

    kind = breach | outlier | None (default: top jobs by run-count).
    """
    sla = session_cache.get("last_sla_matrix") or {}
    if kind == "breach":
        rows = sla.get("breaches") or []
        names = []
        seen = set()
        for r in rows:
            n = r.get("job_name")
            if n and n not in seen:
                names.append(n); seen.add(n)
        return {"kind": "breach", "n_total": len(names), "job_names": names[:limit]}
    if kind == "outlier":
        rows = sla.get("outliers") or []
        names = []
        seen = set()
        for r in rows:
            n = r.get("job_name")
            if n and n not in seen:
                names.append(n); seen.add(n)
        return {"kind": "outlier", "n_total": len(names), "job_names": names[:limit]}
    # Default: most-run jobs from the per-job summary
    summary = sla.get("job_summary") or []
    rows = sorted(summary, key=lambda r: _f(r.get("runs") or r.get("n")), reverse=True)
    out = []
    for r in rows[:limit]:
        out.append({
            "job_name":     r.get("job_name"),
            "runs":         r.get("runs") or r.get("n"),
            "avg_hrs":      r.get("avg_hrs"),
            "p95_hrs":      r.get("p95_hrs"),
            "expected_hrs": r.get("expected_hrs"),
        })
    if not out:
        return {"kind": "all", "n_total": 0, "rows": [],
                "error": "no job summary loaded — Ctrl-M data not ingested"}
    return {"kind": "all", "n_total": len(rows), "rows": out}


def get_breach_runs(limit: int = 10, job_name: Optional[str] = None) -> dict:
    """Return SLA breach rows. If job_name is provided, filter to that job."""
    sla = session_cache.get("last_sla_matrix")
    if not sla:
        return {"error": "SLA matrix not computed yet — upload a Ctrl-M file first"}
    rows = sla.get("breaches") or []
    if not rows:
        return {
            "sla_limit_hrs":  sla.get("sla_limit_hrs"),
            "compliance_pct": sla.get("compliance_pct"),
            "n_total":        0,
            "rows":           [],
            "note": (
                "No SLA breaches — every run finished within the SLA window. "
                "Try get_outliers (per-job baseline) or list_findings instead."
            ),
        }
    if job_name:
        rows = [r for r in rows if _job_match(r.get("job_name", ""), job_name)]
    return {
        "sla_limit_hrs":  sla.get("sla_limit_hrs"),
        "n_total":        len(rows),
        "rows":           _trim(rows, max(1, min(limit, 25))),
    }


def get_outliers(limit: int = 10, job_name: Optional[str] = None) -> dict:
    """Per-job outliers — runs that exceeded their own baseline (z >= 2)."""
    sla = session_cache.get("last_sla_matrix")
    if not sla:
        return {"error": "SLA matrix not computed yet"}
    rows = sla.get("outliers") or []
    if job_name:
        rows = [r for r in rows if _job_match(r.get("job_name", ""), job_name)]
    return {"n_total": len(rows), "rows": _trim(rows, max(1, min(limit, 25)))}


def get_job_history(job_name: str, limit: int = 20) -> dict:
    """All known runs of a single job, with peak/avg/breach stats from baselines."""
    sla = session_cache.get("last_sla_matrix") or {}
    batch = session_cache.get("last_batch") or {}

    baselines = (sla.get("job_baselines") or {})
    base = None
    for key, val in baselines.items():
        if _job_match(key, job_name):
            base = {"job_name": key, **val}
            break

    summary_row = None
    for j in (sla.get("job_summary") or []):
        if _job_match(j.get("job_name", ""), job_name):
            summary_row = j
            break

    sample_runs: List[dict] = []
    for r in (sla.get("breaches") or []):
        if _job_match(r.get("job_name", ""), job_name):
            sample_runs.append({**r, "kind": "breach"})
    for r in (sla.get("outliers") or []):
        if _job_match(r.get("job_name", ""), job_name):
            sample_runs.append({**r, "kind": "outlier"})
    sample_runs = sample_runs[:max(1, min(limit, 30))]

    if not (base or summary_row or sample_runs):
        # Fall back to the batch-level top_jobs / top_breaches
        for r in (batch.get("top_jobs") or []) + (batch.get("top_breaches") or []):
            if _job_match(r.get("Job_Name") or r.get("job_name") or "", job_name):
                sample_runs.append({**r, "kind": "summary"})
        if not (base or sample_runs):
            return {"error": f"job '{job_name}' not found"}

    return {
        "job_name":     job_name,
        "baseline":     base,
        "summary":      summary_row,
        "sample_runs":  sample_runs,
    }


def get_resource_linked_runs(limit: int = 10, verdict: Optional[str] = None) -> dict:
    """Runs flagged with a resource-correlation verdict from the SLA matrix."""
    sla = session_cache.get("last_sla_matrix")
    if not sla:
        return {"error": "SLA matrix not computed yet"}
    rows = sla.get("resource_linked") or []
    if not rows:
        breaches = len(sla.get("breaches") or [])
        outliers = len(sla.get("outliers") or [])
        return {
            "n_total": 0,
            "rows":    [],
            "note": (
                f"No resource-linked runs (breaches={breaches}, outliers={outliers}). "
                "Either no SLA breaches exist, or no resource report was uploaded "
                "to correlate against. Try list_hosts() and list_findings()."
            ),
        }
    if verdict:
        v = verdict.upper()
        rows = [r for r in rows
                if (r.get("resource_signal") or {}).get("verdict") == v]
    return {"n_total": len(rows), "rows": _trim(rows, max(1, min(limit, 25)))}


def _host_summary(s: dict) -> dict:
    return {
        "host":          _host_of(s),
        "type":          s.get("type"),
        "cpu_pct":       s.get("cpu_pct"),
        "cpu_avg_pct":   s.get("cpu_avg_pct"),
        "mem_pct":       s.get("mem_pct"),
        "mem_gb":        s.get("mem_gb"),
        "disk_pct":      s.get("disk_pct"),
        "status":        s.get("status"),
        "agg_trap":      s.get("agg_trap"),
        "dual_pressure": s.get("dual_pressure"),
        "source_env":    s.get("source_env"),
    }


def get_host_metrics(host: Optional[str] = None) -> dict:
    """CPU / memory / disk for one or all servers.

    * host = a real hostname (substring match) → returns that one host.
    * host = "*" / "all" / empty / missing → returns ALL hosts (top-25),
      ranked by combined CPU + memory pressure. This avoids the agent
      hitting an error when it doesn't yet know any hostnames.
    """
    res = session_cache.get("last_resource") or {}
    servs = res.get("servers") or []
    if not servs:
        return {"error": "no resource report uploaded — no hosts available"}

    target = (host or "").strip()
    if target in ("", "*", "all", "any", "ALL"):
        ranked = sorted(
            servs,
            key=lambda s: (_f(s.get("cpu_pct")) + _f(s.get("mem_pct"))),
            reverse=True,
        )
        return {
            "n_total": len(servs),
            "rows":    [_host_summary(s) for s in ranked[:25] if _host_of(s)],
        }

    for s in servs:
        if _host_match(_host_of(s), target):
            return _host_summary(s)

    # Not found — give the agent the list of valid hostnames so it can retry.
    valid = [_host_of(s) for s in servs if _host_of(s)]
    return {
        "error":          f"host '{host}' not found",
        "valid_hosts":    valid[:30],
        "hint":           "call list_hosts() to enumerate, or pass host='*' for all",
    }


def get_critical_servers(limit: int = 10) -> dict:
    """All servers flagged Critical or Warning, sorted by CPU first."""
    res = session_cache.get("last_resource") or {}
    servs = list(res.get("servers") or [])
    if not servs:
        return {"n_total": 0, "rows": [],
                "note": "no resource report uploaded — nothing to flag"}
    flagged = [s for s in servs
               if (s.get("status") or "").lower() in ("critical", "warning")]
    if not flagged:
        return {
            "n_total": 0,
            "rows":    [],
            "note":    f"all {len(servs)} servers are OK — no Warning/Critical hosts",
        }
    flagged.sort(key=lambda s: (_f(s.get("cpu_pct")) + _f(s.get("mem_pct"))), reverse=True)
    rows = []
    for s in flagged[:max(1, min(limit, 25))]:
        rows.append({
            "host":     s.get("host") or s.get("server"),
            "type":     s.get("type"),
            "status":   s.get("status"),
            "cpu_pct":  s.get("cpu_pct"),
            "mem_pct":  s.get("mem_pct"),
            "disk_pct": s.get("disk_pct"),
            "agg_trap": s.get("agg_trap"),
            "dual_pressure": s.get("dual_pressure"),
        })
    return {"n_total": len(flagged), "rows": rows}


def list_findings(level: Optional[str] = None, limit: int = 20) -> dict:
    """Deterministic PE findings. Filter by level (critical|warning|info)."""
    f = session_cache.get("last_findings")
    if not f:
        return {"error": "findings not generated yet"}
    rows = f.get("findings") or []
    if level:
        rows = [r for r in rows if (r.get("level") or "").lower() == level.lower()]
    keep = []
    for r in rows[:max(1, min(limit, 30))]:
        keep.append({
            "id":             r.get("id"),
            "level":          r.get("level"),
            "text":           r.get("text"),
            "detail":         (r.get("detail") or "")[:300],
            "source":         r.get("source"),
            "evidence":       r.get("evidence"),
            "evidence_class": r.get("evidence_class"),
            "root_cause":     r.get("root_cause"),
            "recommendation": (r.get("recommendation") or "")[:240],
            "confidence":     r.get("confidence"),
        })
    return {"summary": f.get("summary"), "n_total": len(rows), "rows": keep}


def list_red_flags(risk: Optional[str] = None, limit: int = 15) -> dict:
    """Red-flag scan results. Filter by risk (CRITICAL|HIGH|MEDIUM|LOW)."""
    rf = session_cache.get("last_red_flags")
    if not rf:
        return {"error": "red flags not generated yet"}
    rows = rf.get("flags") or []
    if risk:
        rows = [r for r in rows if (r.get("risk") or "").upper() == risk.upper()]
    keep = []
    for r in rows[:max(1, min(limit, 25))]:
        keep.append({
            "id":       r.get("id"),
            "risk":     r.get("risk"),
            "category": r.get("category"),
            "question": r.get("question"),
            "context":  (r.get("context") or "")[:240],
        })
    return {"by_risk": rf.get("by_risk"), "n_total": len(rows), "rows": keep}


def get_hour_heatmap(top_n: int = 5) -> dict:
    """The fleet-load-by-hour heatmap. Returns the top N hottest hours."""
    h = session_cache.get("last_hour_heatmap")
    if not h:
        return {"error": "hour heatmap not available — need a Ctrl-M upload"}
    # Heatmap is typically {hour: count} or list of {hour, jobs}
    rows: List[dict] = []
    if isinstance(h, dict):
        for k, v in h.items():
            try:
                rows.append({"hour": int(k), "jobs": int(v)})
            except Exception:
                continue
    elif isinstance(h, list):
        for item in h:
            if isinstance(item, dict) and "hour" in item:
                rows.append({"hour": item.get("hour"), "jobs": item.get("jobs") or item.get("count")})
    rows.sort(key=lambda r: _f(r.get("jobs")), reverse=True)
    return {"hottest_hours": rows[:max(1, min(top_n, 24))]}


# ════════════════════════════════════════════════════════════════
#  TOOL REGISTRY  (function-calling JSON schema)
# ════════════════════════════════════════════════════════════════
TOOL_REGISTRY: Dict[str, Callable[..., dict]] = {
    "list_loaded_data":         list_loaded_data,
    "list_hosts":               list_hosts,
    "list_jobs":                list_jobs,
    "get_breach_runs":          get_breach_runs,
    "get_outliers":             get_outliers,
    "get_job_history":          get_job_history,
    "get_resource_linked_runs": get_resource_linked_runs,
    "get_host_metrics":         get_host_metrics,
    "get_critical_servers":     get_critical_servers,
    "list_findings":            list_findings,
    "list_red_flags":           list_red_flags,
    "get_hour_heatmap":         get_hour_heatmap,
}


TOOL_SCHEMAS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_loaded_data",
            "description": "Inventory of all uploaded artefacts — call this first to see what evidence is available before deciding which other tools to use.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_breach_runs",
            "description": "Return SLA breach rows from the SLA matrix. Optional job_name filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit":    {"type": "integer", "description": "Max rows (default 10, max 25)"},
                    "job_name": {"type": "string",  "description": "Filter to a single job (substring match allowed)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_outliers",
            "description": "Return runs that exceeded their own per-job baseline (z >= 2) but stayed under the global SLA.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit":    {"type": "integer"},
                    "job_name": {"type": "string"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_history",
            "description": "Detailed history for a single job: baseline (avg, p95, expected), summary stats, plus sample breach + outlier rows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_name": {"type": "string"},
                    "limit":    {"type": "integer", "description": "Max sample runs (default 20)"},
                },
                "required": ["job_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_linked_runs",
            "description": "Runs annotated with a resource-link verdict. Filter by verdict (RESOURCE_LINK | TIMING_PRESSURE | ISOLATED).",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit":   {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["RESOURCE_LINK", "TIMING_PRESSURE", "ISOLATED"]},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_hosts",
            "description": "List ALL real hostnames in the uploaded resource report with their type/status/CPU/MEM. Call this BEFORE get_host_metrics — never invent hostnames.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": "List real job names from the SLA matrix. kind='breach' for breaching jobs, 'outlier' for baseline outliers, omit for top jobs by run-count. Call BEFORE get_job_history — never invent job names.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "kind":  {"type": "string", "enum": ["breach", "outlier"]},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_host_metrics",
            "description": "CPU / memory / disk / status for one host or all hosts. Pass host='*' (or omit) for the full ranked list. For one host, pass a real hostname returned by list_hosts (substring match). On 'not found' the response includes valid_hosts so you can retry.",
            "parameters": {
                "type": "object",
                "properties": {"host": {"type": "string", "description": "Real hostname, '*' for all, or omit for all"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_critical_servers",
            "description": "Servers currently flagged Critical or Warning, sorted by combined CPU + memory pressure.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_findings",
            "description": "List deterministic PE findings. Filter by level (critical | warning | info).",
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["critical", "warning", "info"]},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_red_flags",
            "description": "List PE red flags. Filter by risk (CRITICAL | HIGH | MEDIUM | LOW).",
            "parameters": {
                "type": "object",
                "properties": {
                    "risk":  {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hour_heatmap",
            "description": "Top N busiest hours of the batch window, by simultaneous-job count.",
            "parameters": {
                "type": "object",
                "properties": {"top_n": {"type": "integer"}},
                "required": [],
            },
        },
    },
]


def call_tool(name: str, arguments: Any) -> dict:
    """Dispatch a tool call. ``arguments`` may be a dict or a JSON string."""
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except Exception:
            # Try to recover a JSON object from a noisy string
            m = re.search(r"\{[\s\S]*\}", arguments)
            arguments = json.loads(m.group()) if m else {}
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        return fn(**arguments)
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
