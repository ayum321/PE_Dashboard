"""
Customer Identity Service
=========================

Single source of truth for "which customer is this file for?".

The dashboard runs against one customer per session. If a user
uploads a file for a different customer (different SOW, different
SLAs, different infra fleet), every downstream pillar — batch
KPIs, resource grade, SLA Matrix, PE Findings, Red Flags, the
Senior PE Consultant verdict — would be polluted by mixed evidence
and silently produce wrong conclusions.

This service prevents that by:

  1. Extracting candidate customer names from any uploaded file:
       * filename           (e.g. "<CUSTOMER>_15days.csv")
       * CSV/XLSX columns   (e.g. Sub_Application = "<CUSTOMER>_SCPO_DNF_2022_TEST")
       * DOCX / PDF / TXT   (header lines, "Customer:", "Client:")
       * pre-extracted server records (resource_parser tags _customer_name)
       * SOW parser output

  2. Normalising each candidate to a canonical token (uppercase,
     stripped of generic vendor / environment / module suffixes
     like SCPO, DNF, TEST, PROD, UAT, _2022, …).

  3. Comparing the new candidate to the **active customer** held in
     `config_store["active_customer"]`. Returns one of:
        - "first_upload"    no active customer yet → adopt the new one
        - "match"           same customer
        - "mismatch"        different customer → caller MUST surface a
                            blocking warning to the user

The normalisation rules are deterministic and only operate on
strings the user already supplied — no external lookup. The active
customer is stored in `config_store` so it survives across server
restarts.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services import config_store

# Tokens that are *never* a customer name.  Keep this list SMALL — only
# universally-generic terms belong here.  Scheduler / batch vocabulary
# (INTRADAY, CYCLIC, ETL, …) is intentionally ABSENT: the cross-source
# boost in best_candidate() handles those automatically — a token that
# only appears in Sub_Application but NOT in the filename loses to one
# that appears in both.
_NOISE_TOKENS = {
    # environment / lifecycle
    "PROD", "PRODUCTION", "UAT", "DEV", "TEST", "SIT", "STAGE", "STAGING",
    "QA", "PERF", "PERFORMANCE", "TRAIN", "TRAINING", "SANDBOX", "NONPROD",
    "PREPROD", "DR", "BCP",
    # ERP / module / vendor product names & scheduler products
    "SCPO", "DNF", "DEMAND", "FLOW", "DMD", "SUPPLY", "PLAN", "PLANNING",
    "FORECAST", "INVENTORY", "REPLEN", "REPLENISHMENT", "PROMO", "PROMOTION",
    "SAP", "ECC", "S4", "S4HANA", "HANA", "ORACLE", "EBS", "PEOPLESOFT",
    "MANHATTAN", "BLUEYONDER", "BY", "JDA",
    "CTRL", "CTRLM", "BMC", "ESP", "AUTOSYS", "TWS",
    # cloud / infra / tech
    "AZURE", "AWS", "GCP", "OCI", "SQL", "DB", "APP", "WEB", "API",
    "ZABBIX", "NAGIOS", "GRAFANA", "PROMETHEUS", "SPLUNK", "NEWRELIC",
    "SLA", "PE", "RCA", "SRE", "INFRA", "NETWORK", "STORAGE",
    # period / report words
    "DAILY", "WEEKLY", "MONTHLY", "BIWEEKLY", "YEARLY", "QUARTERLY",
    "REPORT", "REPORTS", "REVIEW", "AUDIT", "SUMMARY", "EXTRACT", "EXPORT",
    "LAST", "DAYS", "DAY", "WEEK", "MONTH", "YEAR",
    "RUN", "RUNS", "LOG", "LOGS", "HISTORY",
    "UTILIZATION", "UTILISATION", "UTIL", "USAGE", "METRICS",
    # document type
    "CSV", "XLSX", "XLS", "PDF", "DOCX", "DOC", "TXT", "JSON", "XML",
    # generic English
    "OF", "AND", "FOR", "WITH", "THE", "FROM", "TO", "ON", "IN",
    "FILE", "FILES", "DATA", "PAYLOAD", "RAW", "OUTPUT", "INPUT",
    "INFO", "DETAILS", "STATUS", "RESULT", "RESULTS",
    # short two-letter contract / region prefixes
    "CS", "PS", "MS", "AP", "NA", "EU", "EMEA", "APAC", "LATAM", "ANZ",
}

# Header keys (case-insensitive) that often point at customer name
_HEADER_HINTS = (
    "customer", "client", "company", "account", "tenant", "organization", "organisation",
)

# Regex shortcuts
_WORD_RE = re.compile(r"[A-Z][A-Z0-9]{2,}")        # uppercase tokens, 3+ chars
_LINE_RE = re.compile(r"(?im)^\s*(customer|client|company|account|tenant)\s*[:\-]\s*(.+?)\s*$")
_NONALNUM = re.compile(r"[^A-Z0-9 ]+")


@dataclass
class CustomerCandidate:
    """A single guess at the customer name with provenance & confidence."""
    name:       str          # canonical (UPPERCASE, words separated by space)
    raw:        str          # original string before normalisation
    source:     str          # filename | content | sub_application | sow | resource | header | config
    confidence: int          # 0..100


@dataclass
class CustomerVerdict:
    """Result of identifying & comparing one upload against the active customer."""
    name:        Optional[str]            # canonical chosen customer name
    display:     Optional[str]            # human-friendly Title Case version
    raw:         Optional[str]            # original raw name we picked
    source:      str                      # provenance of the chosen name
    confidence:  int                      # 0..100
    candidates:  List[CustomerCandidate]  # all candidates considered
    status:      str                      # first_upload | match | mismatch | unknown
    active:      Optional[str]            # the previously-active customer (canonical)
    message:     str                      # human-readable explanation
    corroborated_by: List[str] = field(default_factory=list)  # sources confirming the pick
    conflicts:       List[Dict[str, str]] = field(default_factory=list)  # sources disagreeing
    cross_check:     str = "unknown"      # confirmed | partial | conflict | single_source


# ─────────────────────────────────────────────────────────────────
#  Normalisation
# ─────────────────────────────────────────────────────────────────

def _strip_ext(name: str) -> str:
    return os.path.splitext(name or "")[0]


def normalise(raw: str) -> str:
    """Return canonical UPPERCASE customer name, noise tokens removed."""
    if not raw:
        return ""
    s = str(raw).upper().strip()
    s = _NONALNUM.sub(" ", s)
    tokens = [t for t in s.split() if t and not t.isdigit()]
    cleaned: list[str] = []
    for t in tokens:
        if t in _NOISE_TOKENS:
            continue
        # Strip year suffixes & length-1 noise
        if len(t) <= 1:
            continue
        cleaned.append(t)
    return " ".join(cleaned)


def display_name(canonical: str) -> str:
    """Return a friendly Title Case display version."""
    if not canonical:
        return ""
    return " ".join(p.capitalize() for p in canonical.split())


# ─────────────────────────────────────────────────────────────────
#  Candidate extraction
# ─────────────────────────────────────────────────────────────────

def _from_filename(filename: str) -> List[CustomerCandidate]:
    """Emit candidate tokens from the filename stem.

    A filename like 'Last_15_Days_Report_of_CS_<CUSTOMER>_SCPO_DNF_2022_TEST.csv'
    contains BOTH 'CS' (a contract/account prefix) and the real customer token
    as valid uppercase substrings; the customer
    is the longer one. We emit every survivor so the scorer can weigh them
    against evidence from CSV columns / SOW headers.
    """
    if not filename:
        return []
    stem = _strip_ext(os.path.basename(filename))
    raw = re.sub(r"[_\-\.]+", " ", stem).upper()
    norm = normalise(raw)
    parts = [p for p in norm.split() if len(p) >= 3]
    if not parts:
        return []
    # Longer tokens are more distinctive customer markers; rank them so the
    # most distinctive wins when no other evidence is available.
    parts.sort(key=lambda p: (-len(p), p))
    out: list[CustomerCandidate] = []
    for i, p in enumerate(parts[:4]):
        # Confidence: 75 for the longest, 60 / 50 / 40 for shorter alternatives
        conf = max(40, 75 - i * 12)
        # Bonus for very distinctive tokens (>= 6 chars)
        if len(p) >= 6:
            conf = min(85, conf + 10)
        out.append(CustomerCandidate(
            name=p, raw=stem, source="filename", confidence=conf,
        ))
    return out


def _from_text(text: str, source: str = "content") -> List[CustomerCandidate]:
    """Scan free text for explicit Customer/Client lines, then heading words."""
    out: list[CustomerCandidate] = []
    if not text:
        return out

    # 1. Explicit "Customer: NAME" / "Client - NAME"
    for i, m in enumerate(_LINE_RE.finditer(text)):
        if i >= 5:
            break
        raw = m.group(2).strip()
        norm = normalise(raw)
        parts = norm.split()
        if parts:
            out.append(CustomerCandidate(
                name=parts[0],
                raw=raw, source="header", confidence=95,
            ))

    # 2. First non-empty header line (title page heuristic)
    head = "\n".join(text.splitlines()[:20])
    matches = _WORD_RE.findall(head)
    counts: Dict[str, int] = {}
    for w in matches:
        if w in _NOISE_TOKENS:
            continue
        counts[w] = counts.get(w, 0) + 1
    if counts:
        # Pick the most repeated meaningful uppercase word in the heading
        top = max(counts.items(), key=lambda kv: kv[1])
        if top[1] >= 1:
            out.append(CustomerCandidate(
                name=top[0], raw=top[0],
                source=source, confidence=60 if top[1] >= 2 else 45,
            ))
    return out


def _from_sub_application(values: Iterable[Any]) -> List[CustomerCandidate]:
    """Sub_Application column values — return ALL plausible tokens.

    Instead of walking left-to-right and picking the first non-noise
    token (which fails whenever an unlisted scheduler term sits before
    the customer name), we collect EVERY non-noise token across all
    rows and return the top candidates by frequency.  The cross-source
    boost in best_candidate() then picks the token that also appears
    in the filename — no scheduler blocklist needed.
    """
    counts: Dict[str, int] = {}
    sample_raw: Dict[str, str] = {}
    for v in values:
        if v is None:
            continue
        s = str(v).strip().upper()
        if not s or s in {"UNKNOWN", "?"}:
            continue
        seen_in_row: set[str] = set()
        for tok in re.split(r"[_\-\s]+", s):
            if (tok
                    and tok not in _NOISE_TOKENS
                    and len(tok) >= 3
                    and not tok.isdigit()
                    and tok not in seen_in_row):
                counts[tok] = counts.get(tok, 0) + 1
                sample_raw.setdefault(tok, s)
                seen_in_row.add(tok)
    if not counts:
        return []
    total = sum(counts.values())
    # Return top 3 candidates sorted by frequency
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[CustomerCandidate] = []
    for tok, n in ranked[:3]:
        share = n / total
        conf = int(75 + min(14, share * 14))
        out.append(CustomerCandidate(
            name=tok, raw=sample_raw.get(tok, tok),
            source="sub_application", confidence=conf,
        ))
    return out


def _from_servers(servers: List[Dict[str, Any]]) -> List[CustomerCandidate]:
    out: list[CustomerCandidate] = []
    for s in servers or []:
        cand = s.get("_customer_name") or s.get("customer") or s.get("Customer")
        if cand:
            norm = normalise(str(cand))
            parts = norm.split()
            if parts:
                out.append(CustomerCandidate(
                    name=parts[0], raw=str(cand),
                    source="resource", confidence=90,
                ))
                break  # one server is enough — they all share the same tag
    return out


def _from_sow(sow_payload: Dict[str, Any] | None) -> List[CustomerCandidate]:
    if not sow_payload:
        return []
    name = sow_payload.get("customer_name") or sow_payload.get("customer")
    if not name:
        return []
    norm = normalise(str(name))
    parts = norm.split()
    if not parts:
        return []
    return [CustomerCandidate(
        name=parts[0], raw=str(name),
        source="sow", confidence=95,
    )]


# ─────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────

def extract_candidates(
    *,
    filename:    Optional[str]                 = None,
    text:        Optional[str]                 = None,
    df_sub_app:  Optional[Iterable[Any]]       = None,
    servers:     Optional[List[Dict[str, Any]]] = None,
    sow_payload: Optional[Dict[str, Any]]      = None,
) -> List[CustomerCandidate]:
    """Run every available extractor and return the unranked candidate list."""
    cands: list[CustomerCandidate] = []
    if filename:
        cands.extend(_from_filename(filename))
    if text:
        cands.extend(_from_text(text))
    if df_sub_app is not None:
        cands.extend(_from_sub_application(df_sub_app))
    if servers:
        cands.extend(_from_servers(servers))
    if sow_payload:
        cands.extend(_from_sow(sow_payload))
    return cands


def best_candidate(cands: List[CustomerCandidate]) -> Optional[CustomerCandidate]:
    """Pick the best candidate using cross-source corroboration.

    A token that appears from TWO different sources (e.g. both filename
    and sub_application) is almost certainly the real customer name.
    Tokens that only appear from one source (like INTRADAY from sub_app)
    don't get the boost and naturally lose.
    """
    if not cands:
        return None
    cands = [c for c in cands if c.name]
    if not cands:
        return None

    # Build a map: name → set of distinct sources it was seen in
    name_sources: Dict[str, set] = {}
    for c in cands:
        name_sources.setdefault(c.name, set()).add(c.source)

    src_rank = {"sub_application": 0, "sow": 1, "header": 2, "resource": 3,
                "filename": 4, "content": 5, "config": 6}

    def _sort_key(c: CustomerCandidate):
        # Cross-source boost: +15 per additional source that confirms
        n_sources = len(name_sources.get(c.name, set()))
        boosted = min(99, c.confidence + max(0, n_sources - 1) * 15)
        return (-boosted, src_rank.get(c.source, 9), c.name)

    return sorted(cands, key=_sort_key)[0]


def get_active() -> Optional[str]:
    """Return canonical active customer from persistent store, if any."""
    val = config_store.get("active_customer", "") or ""
    return val.strip().upper() or None


def set_active(canonical: str, raw: Optional[str] = None) -> None:
    """Persist the active customer (canonical UPPERCASE)."""
    if not canonical:
        return
    config_store.set("active_customer", canonical.upper())
    if raw:
        config_store.set("active_customer_raw", raw)


def clear_active() -> None:
    config_store.set("active_customer", "")
    config_store.set("active_customer_raw", "")


def identify(
    *,
    filename:    Optional[str]                 = None,
    text:        Optional[str]                 = None,
    df_sub_app:  Optional[Iterable[Any]]       = None,
    servers:     Optional[List[Dict[str, Any]]] = None,
    sow_payload: Optional[Dict[str, Any]]      = None,
    auto_adopt:  bool                          = True,
) -> CustomerVerdict:
    """
    Identify the customer for an upload and compare against the active one.

    If `auto_adopt` is True (default) and no active customer is set yet,
    the best candidate is adopted as the active customer.
    """
    cands = extract_candidates(
        filename=filename, text=text, df_sub_app=df_sub_app,
        servers=servers, sow_payload=sow_payload,
    )
    best = best_candidate(cands)
    active = get_active()

    # Cross-source corroboration: which other sources confirm or contradict
    # the chosen customer? This is the trust signal — Ctrl-M alone is reliable
    # but consistency across SOW + filename + resource raises confidence to
    # "verified", whereas conflicting sources should put the user on alert.
    corroborated_by: list[str] = []
    conflicts: list[dict] = []
    cross_check = "single_source"
    if best:
        # Group candidates by source — keep the highest-confidence pick per source
        by_source: Dict[str, CustomerCandidate] = {}
        for c in cands:
            cur = by_source.get(c.source)
            if not cur or c.confidence > cur.confidence:
                by_source[c.source] = c
        other_sources = [s for s in by_source if s != best.source]
        for s in other_sources:
            cand = by_source[s]
            if cand.name == best.name:
                corroborated_by.append(s)
            else:
                conflicts.append({
                    "source":     s,
                    "name":       cand.name,
                    "display":    display_name(cand.name),
                    "confidence": str(cand.confidence),
                })
        if conflicts and corroborated_by:
            cross_check = "partial"
        elif conflicts:
            cross_check = "conflict"
        elif corroborated_by:
            cross_check = "confirmed"
        else:
            cross_check = "single_source"

    def _verdict(**kw) -> CustomerVerdict:
        kw.setdefault("corroborated_by", corroborated_by)
        kw.setdefault("conflicts", conflicts)
        kw.setdefault("cross_check", cross_check)
        return CustomerVerdict(**kw)

    if not best:
        return _verdict(
            name=active, display=display_name(active) if active else None,
            raw=None, source="config" if active else "none",
            confidence=0, candidates=cands,
            status="unknown" if active else "first_upload",
            active=active,
            message=("No customer identifier found in this file. "
                     "Continuing under active customer "
                     f"'{display_name(active)}'." if active else
                     "No customer identifier found in this file."),
        )

    # Build a corroboration suffix once and reuse across status branches
    if cross_check == "confirmed":
        corr_msg = f" Cross-checked across {len(corroborated_by) + 1} source(s): " \
                   f"{', '.join([best.source] + corroborated_by)}."
    elif cross_check == "partial":
        bad = "; ".join(f"{c['source']}={c['display']}" for c in conflicts)
        corr_msg = (f" Partial corroboration ({', '.join(corroborated_by)}) "
                    f"but conflicts: {bad}.")
    elif cross_check == "conflict":
        bad = "; ".join(f"{c['source']}={c['display']}" for c in conflicts)
        corr_msg = f" ⚠ Cross-source conflict — other files say: {bad}."
    else:
        corr_msg = ""

    if not active:
        if auto_adopt:
            set_active(best.name, best.raw)
        return _verdict(
            name=best.name, display=display_name(best.name),
            raw=best.raw, source=best.source, confidence=best.confidence,
            candidates=cands, status="first_upload", active=best.name,
            message=f"Customer set to '{display_name(best.name)}' from {best.source}.{corr_msg}",
        )

    if best.name == active:
        return _verdict(
            name=best.name, display=display_name(best.name),
            raw=best.raw, source=best.source, confidence=best.confidence,
            candidates=cands, status="match", active=active,
            message=f"Confirmed customer '{display_name(active)}'.{corr_msg}",
        )

    return _verdict(
        name=best.name, display=display_name(best.name),
        raw=best.raw, source=best.source, confidence=best.confidence,
        candidates=cands, status="mismatch", active=active,
        message=(f"Customer mismatch — this file looks like "
                 f"'{display_name(best.name)}' but the active session is "
                 f"'{display_name(active)}'. Verify the upload before "
                 f"trusting the analysis.{corr_msg}"),
    )


def verdict_to_dict(verdict: CustomerVerdict, *, max_candidates: int = 8) -> Dict[str, Any]:
    """Serialise a verdict for JSON responses (used by every upload route)."""
    return {
        "name":            verdict.name,
        "display":         verdict.display,
        "raw":             verdict.raw,
        "source":          verdict.source,
        "confidence":      verdict.confidence,
        "status":          verdict.status,
        "active":          verdict.active,
        "message":         verdict.message,
        "cross_check":     verdict.cross_check,
        "corroborated_by": list(verdict.corroborated_by),
        "conflicts":       list(verdict.conflicts),
        "candidates": [
            {"name": c.name, "raw": c.raw, "source": c.source,
             "confidence": c.confidence}
            for c in verdict.candidates[:max_candidates]
        ],
    }


def sniff_text_from_bytes(raw: bytes, filename: str, max_chars: int = 8000) -> str:
    """Best-effort plain-text extraction for customer-name scanning only."""
    if not raw:
        return ""
    ext = os.path.splitext(filename or "")[1].lower()
    try:
        if ext in (".txt", ".csv"):
            return raw[: max_chars * 2].decode("utf-8", errors="replace")[:max_chars]
        if ext in (".json",):
            return raw[: max_chars * 2].decode("utf-8", errors="replace")[:max_chars]
        if ext == ".docx":
            try:
                from docx import Document
                doc = Document(io.BytesIO(raw))
                parts = [p.text for p in doc.paragraphs[:80] if p.text.strip()]
                return "\n".join(parts)[:max_chars]
            except Exception:
                return ""
        if ext == ".pdf":
            try:
                import pdfplumber
                buf = io.BytesIO(raw)
                with pdfplumber.open(buf) as pdf:
                    pages = pdf.pages[:2]
                    return "\n".join((p.extract_text() or "") for p in pages)[:max_chars]
            except Exception:
                return ""
        if ext in (".xlsx", ".xls"):
            try:
                import pandas as pd
                df = pd.read_excel(io.BytesIO(raw), nrows=5)
                return df.to_csv(index=False)[:max_chars]
            except Exception:
                return ""
    except Exception:
        return ""
    return ""
