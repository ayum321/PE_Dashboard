"""
smart_findings — post-processor that turns the rule-engine output of
routers.findings into a Claude-grade audit briefing:

    • Deduplicates by root cause (e.g. "3 possible SLA waivers detected")
    • Re-shapes every finding into the strict 7-field contract
    • Tags evidence quality (MEASURED / INFERRED / ASSUMED)
    • Caps INFO findings at 5, collapses the rest into a single "N more" card
    • Emits a VERDICT BLOCK and a NEXT ACTIONS table (max 3 rows)
    • Splits "data missing" findings into a separate OPEN GAPS section
    • Sorts CRITICAL → WARNING → OK → INFO (no interleaving)

Designed to run in O(N) over the existing findings list — no LLM round-trip
required for the deterministic part. The LLM (Gemma) is only invoked for the
15-word verdict summary, and that call is best-effort (returns a fallback
deterministic summary if the model is unreachable).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any, Optional

log = logging.getLogger("pe_dashboard.smart_findings")

# ── Owner mapping by source pillar ─────────────────────────────────────────
_OWNER_BY_SOURCE = {
    "batch":     "PE Lead",
    "sla":       "PE Lead",
    "resource":  "Infra Owner",
    "benchmark": "PE Lead",
    "sow":       "Customer",
    "issues":    "PE Lead",
}

# ── Evidence-class → display tag ───────────────────────────────────────────
_EVIDENCE_TAG = {
    "measured":    "MEASURED",
    "inferred":    "INFERRED",
    "defaulted":   "ASSUMED",
    "assumed":     "ASSUMED",
    "waived":      "INFERRED",
    "unavailable": "ASSUMED",
}

# ── Severity ordering for sort ─────────────────────────────────────────────
_LEVEL_ORDER = {"critical": 0, "warning": 1, "ok": 2, "info": 3}
_LEVEL_TAG   = {"critical": "CRITICAL", "warning": "WARNING",
                "ok": "OK",            "info": "INFO"}

# ── Phrases that mean a finding is purely "data missing" (→ Open Gaps) ─────
_GAP_PHRASES = (
    "no audit data loaded",
    "data missing",
    "not uploaded",
    "missing — ",
    "unavailable until",
    "no end_time",
    "image-only docx",
    "no resource utilization report",
    "no ui benchmark report",
    "no sow",
    "no issues register uploaded",
)

# ── Pre-go-live checklist phrases (force INFO unless explicitly failing) ──
_CHECKLIST_PHRASES = (
    "30-day", "ui sign-off", "automation", "monitoring gap",
    "open audit gaps",
)

# ── Deterministic owner inference from text ───────────────────────────────
def _infer_owner(level: str, source: str, text: str, root_cause: str) -> str:
    rc = (root_cause or "").upper()
    if rc.startswith("WAIVER"):       return "Customer"
    if rc.startswith("SLA_MISMATCH"): return "PE Lead"
    if "infra" in text.lower() or "server" in text.lower() or "fleet" in text.lower():
        return "Infra Owner"
    if "sow" in text.lower() or "contract" in text.lower():
        return "Customer"
    return _OWNER_BY_SOURCE.get(source or "", "PE Lead")


# ── Title shortening: keep ≤ 70 chars, lead with the metric/number ─────────
_NUM_LEAD = re.compile(r"^([\d.,]+%?|\d+\s+\w+)")

def _trim_title(text: str, max_len: int = 70) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    # Strip leading emoji and decorative whitespace
    t = re.sub(r"^[^\w\d]+", "", t)
    if len(t) <= max_len:
        return t
    # Cut on a sentence boundary if possible
    cut = t[:max_len]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _trim_one_line(text: str, max_len: int = 90) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _trim_action(action: str, max_len: int = 60) -> str:
    a = re.sub(r"\s+", " ", (action or "").strip())
    if not a:
        return ""
    # Force imperative tone — strip soft openers
    a = re.sub(r"^(please|consider|maybe|try to|you should)\s+", "",
               a, flags=re.IGNORECASE)
    return a[:max_len].rstrip(".,;:") + ("…" if len(a) > max_len else "")


# ── Root-cause grouping key ────────────────────────────────────────────────
def _group_key(f: dict) -> str:
    """Findings with the same group_key collapse into one card."""
    rc = (f.get("root_cause") or "").upper().strip()
    if rc:
        return f"rc:{rc}"
    text = (f.get("text") or "").lower()
    # Heuristic groups when no explicit root_cause was attached
    if "waiver" in text:                     return "rc:WAIVER_NOT_APPLIED"
    if "aggregation" in text or "agg trap" in text: return "rc:AGG_TRAP"
    if "dual cpu" in text or "dual pressure" in text: return "rc:DUAL_PRESSURE"
    if "cpu saturation" in text or "cpu warning" in text: return "rc:CPU_PRESSURE"
    if "memory pressure" in text:            return "rc:MEM_PRESSURE"
    if "disk usage" in text or "disk" in text and "critical" in text: return "rc:DISK_PRESSURE"
    if "sla" in text and "breach" in text:   return "rc:SLA_BREACH"
    if "at risk" in text or "at_risk" in text: return "rc:SLA_AT_RISK"
    if "anomal" in text:                     return "rc:ANOMALY"
    return f"text:{(f.get('text') or '')[:40]}"


def _is_open_gap(f: dict) -> bool:
    text = (f.get("text") or "") + " " + (f.get("sub") or "")
    t = text.lower()
    return any(p in t for p in _GAP_PHRASES)


def _is_checklist(f: dict) -> bool:
    text = (f.get("text") or "") + " " + (f.get("sub") or "")
    t = text.lower()
    return any(p in t for p in _CHECKLIST_PHRASES)


# ── Build a structured finding from a group of raw findings ────────────────
def _shape_finding(group: list[dict], group_id: str) -> dict:
    """Collapse N raw findings into one structured card."""
    # Pick the highest-severity item as the primary
    primary = min(group, key=lambda f: _LEVEL_ORDER.get(f.get("level"), 9))
    level   = primary.get("level", "info")
    src     = primary.get("source", "")
    rc      = primary.get("root_cause", "")

    # Title: count-led when grouped, else number-led if metric available
    base_title = primary.get("text") or ""
    if len(group) > 1:
        # "3 possible SLA waivers detected"
        suffix = "detected" if "detected" not in base_title.lower() else ""
        # Strip trailing colon/quote noise
        clean = re.sub(r"^\d+\s+", "", base_title).rstrip(" :")
        title = f"{len(group)} {clean} {suffix}".strip()
    else:
        title = base_title

    one_line = primary.get("sub") or primary.get("impact") or ""
    impact   = primary.get("impact") or ""
    action   = primary.get("recommendation") or ""
    evidence = primary.get("evidence") or ""

    ev_class = primary.get("evidence_class") or "measured"
    if ev_class not in _EVIDENCE_TAG:
        log.warning("Unknown evidence_class '%s' for finding '%s' — treating as INFERRED",
                    ev_class, primary.get("text", "?"))
    ev_tag   = _EVIDENCE_TAG.get(ev_class, "INFERRED")  # default INFERRED not MEASURED

    # If grouped, mention the count in the impact line
    if len(group) > 1 and impact and str(len(group)) not in impact:
        impact = f"Affects {len(group)} item(s). {impact}"

    owner  = _infer_owner(level, src, base_title, rc)

    return {
        "id":          group_id,
        "level":       level,
        "severity":    _LEVEL_TAG.get(level, "INFO"),
        "title":       _trim_title(title),
        "one_line":    _trim_one_line(one_line),
        "evidence":    f"{(src or 'context').upper()} · {ev_tag}",
        "evidence_raw": evidence,
        "impact":      impact,
        "action":      _trim_action(action) or "—",
        "owner":       owner,
        "icon":        primary.get("icon") or "",
        "source":      src,
        "root_cause":  rc,
        "evidence_class": ev_class,
        "group_size":  len(group),
    }


# ── Public entry: run the dedup+structure pass ─────────────────────────────
def smartify(
    findings: list[dict],
    *,
    customer_name: Optional[str] = None,
    kpi_evidence: Optional[dict] = None,
) -> dict:
    """Apply the FINDINGS OUTPUT RULES to a raw findings list.

    Returns a dict with:
        verdict:      {decision, grade, blocker_count, summary, customer}
        next_actions: list[{owner, action, finding_id}] (max 3)
        findings:     list[shaped finding] in CRITICAL→WARNING→OK→INFO order
        open_gaps:    list[shaped finding]   (data-missing only)
        info_collapsed: int   (how many INFO findings hidden behind "more")
        counts:       {critical, warning, ok, info, total}
    """
    raw = list(findings or [])
    open_gaps_raw: list[dict] = []
    actionable:    list[dict] = []

    for f in raw:
        if _is_open_gap(f):
            open_gaps_raw.append(f)
        else:
            # Demote pure checklist items to INFO unless they failed
            if _is_checklist(f) and f.get("level") not in ("critical", "warning"):
                f = {**f, "level": "info"}
            actionable.append(f)

    # ── Group by root cause / heuristic key ───────────────────────────
    buckets: dict[str, list[dict]] = defaultdict(list)
    for f in actionable:
        buckets[_group_key(f)].append(f)

    # Within a group, keep highest-severity ordering for the primary item
    shaped: list[dict] = []
    for key, group in buckets.items():
        group.sort(key=lambda f: _LEVEL_ORDER.get(f.get("level"), 9))
        shaped.append(_shape_finding(group, group_id=key))

    # ── Cap INFO findings at 5, collapse the rest ─────────────────────
    info_items = [f for f in shaped if f["level"] == "info"]
    other      = [f for f in shaped if f["level"] != "info"]
    info_collapsed = 0
    if len(info_items) > 5:
        info_collapsed = len(info_items) - 5
        info_items = info_items[:5]
        info_items.append({
            "id":       "info:_collapsed",
            "level":    "info",
            "severity": "INFO",
            "title":    f"{info_collapsed} additional observations available on request.",
            "one_line": "Collapsed to keep the briefing focused on actionable findings.",
            "evidence": "CONTEXT · MEASURED",
            "impact":   "",
            "action":   "Click to expand if needed",
            "owner":    "PE Lead",
            "icon":     "📎",
            "source":   "",
            "root_cause": "INFO_COLLAPSED",
            "evidence_class": "measured",
            "group_size": info_collapsed,
            "_collapsed": True,
        })

    # ── Sort: critical → warning → ok → info ──────────────────────────
    out = sorted(other + info_items, key=lambda f: _LEVEL_ORDER.get(f["level"], 9))

    # ── Open gaps shaped separately ───────────────────────────────────
    gaps_shaped: list[dict] = []
    for i, f in enumerate(open_gaps_raw):
        gaps_shaped.append(_shape_finding([f], group_id=f"gap:{i}"))

    # ── Severity mismatch correction (KPI evidence check) ───────────
    severity_overrides: list[dict] = []
    if kpi_evidence:
        from services.verdict_reconciler import check_finding_severity_mismatch
        for f in out:
            corrected = check_finding_severity_mismatch(
                f.get("level", ""), f.get("title", "") + " " + f.get("one_line", ""),
                kpi_evidence,
            )
            if corrected and corrected != f.get("level"):
                severity_overrides.append({
                    "finding_id": f.get("id"),
                    "was": f["level"],
                    "now": corrected,
                    "reason": "KPI evidence mismatch",
                })
                f["level"] = corrected
                f["severity"] = corrected.upper()

    # ── Counts ────────────────────────────────────────────────────────
    counts = {
        "critical": sum(1 for f in out if f["level"] == "critical"),
        "warning":  sum(1 for f in out if f["level"] == "warning"),
        "ok":       sum(1 for f in out if f["level"] == "ok"),
        "info":     sum(1 for f in out if f["level"] == "info"),
    }
    counts["total"] = sum(counts.values())

    # ── Verdict + Next Actions ────────────────────────────────────────
    verdict = _build_verdict(out, customer_name=customer_name, counts=counts)
    next_actions = _build_next_actions(out, max_rows=3)

    return {
        "verdict":        verdict,
        "next_actions":   next_actions,
        "findings":       out,
        "open_gaps":      gaps_shaped,
        "info_collapsed": info_collapsed,
        "counts":         counts,
        "severity_overrides": severity_overrides,
    }


# ── Verdict builder ────────────────────────────────────────────────────────
def _build_verdict(
    shaped: list[dict],
    *,
    customer_name: Optional[str],
    counts: dict,
) -> dict:
    crit = counts.get("critical", 0)
    warn = counts.get("warning", 0)
    ok   = counts.get("ok", 0)

    if crit > 0:
        decision = "BLOCKED"
        grade    = "F" if crit >= 3 else "D"
    elif warn > 0:
        decision = "CONDITIONAL"
        grade    = "C" if warn >= 3 else "B"
    elif ok > 0:
        decision = "APPROVED"
        grade    = "A"
    else:
        decision = "PENDING"
        grade    = "—"

    # decision_label from canonical table for consistency
    try:
        from services.pe_config import GRADE_TABLE
        grade_labels = {g[1]: g[2] for g in GRADE_TABLE}
        decision_label = grade_labels.get(grade, decision)
    except Exception:
        decision_label = decision

    # 15-word deterministic summary (LLM may overwrite this later)
    summary = _deterministic_summary(decision, shaped, crit, warn)

    return {
        "decision":     decision,
        "grade":        grade,
        "blocker_count": crit,
        "warning_count": warn,
        "summary":      summary,
        "customer":     customer_name or "Unknown",
    }


def _deterministic_summary(
    decision: str, shaped: list[dict], crit: int, warn: int,
) -> str:
    """A 15-word fallback verdict summary used when Gemma is unavailable."""
    if decision == "BLOCKED":
        top = next((f for f in shaped if f["level"] == "critical"), None)
        cause = (top or {}).get("root_cause", "").replace("_", " ").lower() or "critical findings"
        return f"Sign-off blocked by {crit} critical finding(s); root cause: {cause}; remediation required before customer review."
    if decision == "CONDITIONAL":
        return f"No blockers but {warn} warning(s) require acknowledgement; conditional approval pending owner sign-off on residual risk."
    if decision == "APPROVED":
        return "All reviewed pillars within thresholds; PE audit ready for customer sign-off with no outstanding blockers."
    return "Insufficient evidence loaded to render a verdict; upload Ctrl-M batch and resource files to proceed."


# ── Next Actions (max 3) ───────────────────────────────────────────────────
def _build_next_actions(shaped: list[dict], *, max_rows: int = 3) -> list[dict]:
    rows: list[dict] = []
    seen_owner: set[tuple[str, str]] = set()  # (owner, root_cause) dedup
    # Walk in severity order, take first N distinct (owner × root_cause)
    for f in shaped:
        if len(rows) >= max_rows:
            break
        if f["level"] not in ("critical", "warning"):
            continue
        if not f.get("action") or f["action"] == "—":
            continue
        key = (f["owner"], f.get("root_cause", "")[:20])
        if key in seen_owner:
            continue
        seen_owner.add(key)
        rows.append({
            "owner":      f["owner"],
            "action":     f["action"],
            "links_to":   f["id"],
            "links_text": _trim_title(f["title"], 50),
            "severity":   f["severity"],
        })
    return rows
