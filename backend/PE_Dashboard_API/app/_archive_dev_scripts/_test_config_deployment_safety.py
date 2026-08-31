"""Focused deployment-safety checks for configuration and persisted state.

Run from ``app``: ``py -3.14 _test_config_deployment_safety.py``.
"""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import HTTPException


_ENV_KEYS = (
    "PE_STATE_DIR",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "NVIDIA_API_KEY",
    "NIM_API_KEY",
    "PE_ALLOW_UI_SECRET_CONFIG",
)


def _restore_environment(original: dict[str, str | None]) -> None:
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def main_test() -> None:
    original = {key: os.environ.get(key) for key in _ENV_KEYS}
    state_dir = Path(tempfile.mkdtemp(prefix="pe-state-safety-")) / "nested" / "state"
    try:
        os.environ["PE_STATE_DIR"] = str(state_dir)
        os.environ["GOOGLE_API_KEY"] = "runtime-gemini-key"
        os.environ["NVIDIA_API_KEY"] = "runtime-nvidia-key"
        os.environ.pop("PE_ALLOW_UI_SECRET_CONFIG", None)

        from services import config_store, session_cache
        from routers import config

        importlib.reload(config_store)
        importlib.reload(session_cache)
        importlib.reload(config)

        config_store.set("benchmark_threshold", 12.5)
        assert config_store._CONFIG_PATH == state_dir / ".pe_config.json"
        assert config_store._CONFIG_PATH.exists(), "config must be written under PE_STATE_DIR"

        session_cache.set("last_batch", {"runs": 1})
        assert session_cache._CACHE_FILE == state_dir / ".pe_cache.json"
        assert session_cache._CACHE_FILE.exists(), "cache must be written under PE_STATE_DIR"

        config_store.set("gemini_api_key", "persisted-gemini-key")
        config_store.set("nvidia_api_key", "persisted-nvidia-key")
        assert config_store.get_gemini_key() == "runtime-gemini-key"
        assert config_store.get_nvidia_key() == "runtime-nvidia-key"

        response = config.get_config()
        assert "gemini_api_key" not in response
        assert "nvidia_api_key" not in response
        assert response["benchmark_threshold"] == 12.5
        assert "runtime-gemini-key" not in repr(response)
        assert "runtime-nvidia-key" not in repr(response)
        assert config_store.get("gemini_api_key") == "persisted-gemini-key"
        assert config_store.get("nvidia_api_key") == "persisted-nvidia-key"

        try:
            config.update_config(config.ConfigPayload(gemini_api_key="browser-gemini-key"))
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("UI secret persistence must be blocked by default")

        os.environ["PE_ALLOW_UI_SECRET_CONFIG"] = "true"
        result = config.update_config(config.ConfigPayload(ai_post_upload=False, nvidia_api_key="local-nvidia-key"))
        assert result["updated"] == ["nvidia_api_key", "ai_post_upload"]
        assert config_store.get("nvidia_api_key") == "local-nvidia-key"
        assert config_store.get("ai_post_upload") is False

        print("[OK] deployment state directory, environment secret precedence, and UI secret policy")
    finally:
        _restore_environment(original)
        shutil.rmtree(state_dir.parents[1], ignore_errors=True)


if __name__ == "__main__":
    main_test()
