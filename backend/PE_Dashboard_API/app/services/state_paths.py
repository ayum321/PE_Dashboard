from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("pe_dashboard.state_paths")

_RESOLVED_STATE_DIR: Path | None = None
_LAST_RAW_ENV: str | None = None

def get_state_dir() -> Path:
    """Return a verified writable state directory, falling back to /tmp if needed."""
    global _RESOLVED_STATE_DIR, _LAST_RAW_ENV
    raw_env = os.environ.get("PE_STATE_DIR", "").strip()
    if _RESOLVED_STATE_DIR is not None and _LAST_RAW_ENV == raw_env:
        return _RESOLVED_STATE_DIR

    candidates: list[Path] = []
    if raw_env:
        candidates.append(Path(raw_env).expanduser().resolve())

    # Fallback candidates
    if os.name == "nt":
        candidates.append(Path(tempfile.gettempdir()) / "pe_dashboard_state")
    else:
        candidates.append(Path("/tmp/pe_dashboard_state"))
        candidates.append(Path(tempfile.gettempdir()) / "pe_dashboard_state")

    # Local package directory fallback
    candidates.append(Path(__file__).resolve().parent.parent)

    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            test_file = cand / ".pe_write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            _RESOLVED_STATE_DIR = cand
            _LAST_RAW_ENV = raw_env
            if raw_env and cand != Path(raw_env).expanduser().resolve():
                logger.warning(
                    "Configured PE_STATE_DIR '%s' is read-only. Auto-fallback to writable directory: %s",
                    raw_env, cand
                )
            return _RESOLVED_STATE_DIR
        except (OSError, PermissionError) as err:
            logger.info("State directory candidate '%s' unwritable (%s), trying next fallback...", cand, err)
            continue

    _RESOLVED_STATE_DIR = Path(tempfile.gettempdir())
    _LAST_RAW_ENV = raw_env
    return _RESOLVED_STATE_DIR


def get_state_file(filename: str) -> Path:
    """Return a path for a state file in the active writable state dir."""
    return get_state_dir() / filename
