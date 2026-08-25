"""
Environment Detection Engine — auto-classify PROD / TEST / UAT / QA / DEV / DR / PREPROD
from messy enterprise naming conventions across uploaded files.

Uses weighted evidence stacking across:
  - filename, folder name, application name, sub-application name,
    batch/job name, schedule name, server hostname, and other metadata.

Public API:
    detect_environment(filename, rows)  -> EnvDetectionResult
    detect_multi(files)                 -> list[EnvDetectionResult]
    compare_environments(results)       -> EnvComparisonResult

Evidence stacking weights:
    Field match in filename:        weight 2
    Field match in Job_Name:        weight 3
    Field match in Sub_Application: weight 3
    Field match in server hostname: weight 4  (strongest signal)
    Field match in schedule/folder: weight 2

Confidence thresholds:
    >= 80: HIGH   — auto-classify
    >= 50: MEDIUM — classify with note
    <  50: LOW    — flag as AMBIGUOUS, require user confirmation
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ── Environment synonyms ──────────────────────────────────────
# Canonical env → list of regex patterns (case-insensitive)
ENV_PATTERNS: Dict[str, List[str]] = {
    "PROD": [
        r"\bPROD\b", r"\bPRODUCTION\b", r"\bPRD\b",
        r"PROD(?=\d)", r"(?<=\w)PROD\b",
    ],
    "TEST": [
        r"\bTEST\b", r"\bTST\b", r"\bTESTING\b",
        r"TEST(?=\d)", r"(?<=\w)TEST\b",
    ],
    "UAT": [
        r"\bUAT\b", r"\bUSER.?ACCEPT\b",
    ],
    "QA": [
        r"\bQA\b", r"\bQUALITY\b",
    ],
    "DEV": [
        r"\bDEV\b", r"\bDEVELOP\b", r"\bDEVELOPMENT\b",
    ],
    "SIT": [
        r"\bSIT\b", r"\bSYSTEM.?INT\b",
    ],
    "PREPROD": [
        r"\bPREPROD\b", r"\bPRE.?PROD\b", r"\bSTAGE\b", r"\bSTAGING\b",
    ],
    "DR": [
        r"\bDR\b", r"\bDISASTER\b", r"\bDR.?SITE\b",
    ],
}

# Compiled patterns for performance
_COMPILED: Dict[str, List[re.Pattern]] = {
    env: [re.compile(p, re.IGNORECASE) for p in patterns]
    for env, patterns in ENV_PATTERNS.items()
}

# Field weights for evidence stacking
FIELD_WEIGHTS: Dict[str, int] = {
    "filename":        2,
    "folder":          2,
    "Job_Name":        3,
    "Sub_Application": 3,
    "Application":     3,
    "Schedule":        2,
    "Folder":          2,
    "host":            4,
    "server":          4,
    "source_env":      3,
}


@dataclass
class EnvSignal:
    """A single environment detection signal from one field."""
    env: str           # canonical env (PROD, TEST, etc.)
    field: str         # which field produced this signal
    value: str         # the actual value that matched
    weight: int        # evidence weight


@dataclass
class EnvDetectionResult:
    """Result of environment detection for a single file."""
    filename: str
    detected_env: str              # canonical env or "AMBIGUOUS"
    confidence: float              # 0-100
    confidence_label: str          # HIGH, MEDIUM, LOW, AMBIGUOUS
    signals: List[EnvSignal] = field(default_factory=list)
    env_scores: Dict[str, float] = field(default_factory=dict)
    raw_label: str = ""            # the raw detected label before normalization
    normalized_env: str = ""       # normalized canonical env
    needs_confirmation: bool = False
    row_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "detected_env": self.detected_env,
            "confidence": round(self.confidence, 1),
            "confidence_label": self.confidence_label,
            "signals": [
                {"env": s.env, "field": s.field, "value": s.value, "weight": s.weight}
                for s in self.signals
            ],
            "env_scores": {k: round(v, 1) for k, v in self.env_scores.items()},
            "raw_label": self.raw_label,
            "normalized_env": self.normalized_env,
            "needs_confirmation": self.needs_confirmation,
            "row_count": self.row_count,
        }


@dataclass
class EnvComparisonResult:
    """Grouping of files by detected environment + batch family matching."""
    environments: Dict[str, List[EnvDetectionResult]]  # env -> files
    batch_families: List[Dict[str, Any]]               # cross-env batch matches
    ambiguous: List[EnvDetectionResult]                 # unclassified files

    def to_dict(self) -> Dict[str, Any]:
        return {
            "environments": {
                env: [r.to_dict() for r in results]
                for env, results in self.environments.items()
            },
            "batch_families": self.batch_families,
            "ambiguous": [r.to_dict() for r in self.ambiguous],
        }


def _scan_text(text: str) -> List[Tuple[str, str]]:
    """Scan text for environment signals. Returns [(env, matched_text), ...]."""
    hits: List[Tuple[str, str]] = []
    if not text:
        return hits
    for env, patterns in _COMPILED.items():
        for pat in patterns:
            m = pat.search(text)
            if m:
                hits.append((env, m.group()))
                break  # one match per env per text
    return hits


def _scan_rows(rows: List[Dict[str, Any]]) -> List[EnvSignal]:
    """Scan all rows for environment signals across all metadata fields."""
    signals: List[EnvSignal] = []
    # Sample up to 200 rows for performance
    sample = rows[:200] if len(rows) > 200 else rows

    for row in sample:
        for field_name, weight in FIELD_WEIGHTS.items():
            val = row.get(field_name)
            if not val or not isinstance(val, str):
                continue
            hits = _scan_text(val)
            for env, matched in hits:
                signals.append(EnvSignal(
                    env=env, field=field_name,
                    value=f"{val[:60]}…" if len(val) > 60 else val,
                    weight=weight,
                ))
    return signals


def detect_environment(
    filename: str,
    rows: Optional[List[Dict[str, Any]]] = None,
) -> EnvDetectionResult:
    """Detect environment from a single file's name + row metadata.

    Uses weighted evidence stacking: repeated signals across multiple
    fields increase confidence. Single weak signal alone is insufficient.
    """
    signals: List[EnvSignal] = []

    # 1. Scan filename
    fn_hits = _scan_text(filename or "")
    for env, matched in fn_hits:
        signals.append(EnvSignal(
            env=env, field="filename", value=filename or "", weight=FIELD_WEIGHTS["filename"],
        ))

    # 2. Scan row-level metadata
    if rows:
        signals.extend(_scan_rows(rows))

    # 3. Score each environment by weighted signal count
    env_scores: Dict[str, float] = {}
    for sig in signals:
        env_scores[sig.env] = env_scores.get(sig.env, 0) + sig.weight

    total_weight = sum(env_scores.values()) or 1.0
    row_count = len(rows) if rows else 0

    if not env_scores:
        return EnvDetectionResult(
            filename=filename or "unknown",
            detected_env="UNKNOWN",
            confidence=0.0,
            confidence_label="UNKNOWN",
            signals=signals,
            env_scores={},
            raw_label="",
            normalized_env="UNKNOWN",
            needs_confirmation=True,
            row_count=row_count,
        )

    # Sort by score descending
    ranked = sorted(env_scores.items(), key=lambda x: -x[1])
    best_env, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0

    # Confidence: how dominant is the top env vs alternatives?
    dominance = best_score / total_weight * 100
    # Penalize if only filename signal (weak)
    if len(signals) == 1 and signals[0].field == "filename":
        dominance = min(dominance, 45)  # cap at LOW

    # Penalize conflicting signals
    if second_score > 0 and second_score >= best_score * 0.5:
        dominance *= 0.7  # significant competition

    confidence = min(100.0, round(dominance, 1))

    # Label
    if confidence >= 80:
        label = "HIGH"
        needs_conf = False
    elif confidence >= 50:
        label = "MEDIUM"
        needs_conf = False
    else:
        label = "LOW"
        needs_conf = True

    # If signals are mixed/conflicting, mark as ambiguous
    if len(ranked) > 1 and second_score >= best_score * 0.8:
        best_env = "AMBIGUOUS"
        label = "AMBIGUOUS"
        needs_conf = True

    return EnvDetectionResult(
        filename=filename or "unknown",
        detected_env=best_env,
        confidence=confidence,
        confidence_label=label,
        signals=signals,
        env_scores=env_scores,
        raw_label=best_env,
        normalized_env=best_env if best_env != "AMBIGUOUS" else "",
        needs_confirmation=needs_conf,
        row_count=row_count,
    )


def detect_multi(
    files: List[Dict[str, Any]],
) -> List[EnvDetectionResult]:
    """Detect environments for multiple files.

    Each dict must have 'filename' and optionally 'rows'.
    """
    results: List[EnvDetectionResult] = []
    for f in files:
        r = detect_environment(f.get("filename", ""), f.get("rows"))
        results.append(r)
    return results


def compare_environments(
    results: List[EnvDetectionResult],
) -> EnvComparisonResult:
    """Group detection results by environment and find batch family matches.

    A "batch family" = same logical batch exists in two environments
    with slightly different naming (e.g., ESPPROD_DAILY vs ESPTEST_DAILY).
    """
    envs: Dict[str, List[EnvDetectionResult]] = {}
    ambiguous: List[EnvDetectionResult] = []

    for r in results:
        if r.detected_env == "AMBIGUOUS" or r.detected_env == "UNKNOWN":
            ambiguous.append(r)
        else:
            envs.setdefault(r.detected_env, []).append(r)

    # Batch family detection — find filenames that differ only by env token
    families: List[Dict[str, Any]] = []
    env_files: Dict[str, List[str]] = {
        env: [r.filename for r in rs] for env, rs in envs.items()
    }

    if len(env_files) >= 2:
        env_list = list(env_files.keys())
        for i in range(len(env_list)):
            for j in range(i + 1, len(env_list)):
                env_a, env_b = env_list[i], env_list[j]
                for fn_a in env_files[env_a]:
                    # Normalize: strip env tokens to find base name
                    base_a = _strip_env_tokens(fn_a)
                    for fn_b in env_files[env_b]:
                        base_b = _strip_env_tokens(fn_b)
                        if base_a and base_b and base_a == base_b:
                            families.append({
                                "base_name": base_a,
                                "environments": {
                                    env_a: fn_a,
                                    env_b: fn_b,
                                },
                                "comparison_ready": True,
                            })

    return EnvComparisonResult(
        environments=envs,
        batch_families=families,
        ambiguous=ambiguous,
    )


def _strip_env_tokens(filename: str) -> str:
    """Remove environment tokens from filename to get a base name for matching."""
    s = filename.upper()
    # Remove file extension
    s = re.sub(r'\.[^.]+$', '', s)
    # Remove all env tokens
    for patterns in ENV_PATTERNS.values():
        for p in patterns:
            s = re.sub(p, '', s, flags=re.IGNORECASE)
    # Normalize separators and whitespace
    s = re.sub(r'[_\-\s.]+', '_', s).strip('_')
    return s
