"""Regression checks for production React SPA deep-link serving.

Run from ``app``: ``py -3.14 _test_mfe_spa_fallback.py``.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import main


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main_test() -> None:
    temporary_mfe = Path(tempfile.mkdtemp(prefix="pe-mfe-fallback-"))
    original_mfe_dir = main.MFE_DIR
    try:
        (temporary_mfe / "static" / "js").mkdir(parents=True)
        (temporary_mfe / "index.html").write_text("<div id=\"root\">PE React</div>", encoding="utf-8")
        (temporary_mfe / "static" / "js" / "main.js").write_text("console.log('asset')", encoding="utf-8")
        main.MFE_DIR = temporary_mfe

        with TestClient(main.app) as client:
            deep_link = client.get("/batch")
            _assert(deep_link.status_code == 200, f"/batch: {deep_link.status_code}")
            _assert("PE React" in deep_link.text, f"/batch did not return SPA shell: {deep_link.text!r}")
            _assert(deep_link.headers.get("cache-control") == "no-store", "SPA shell must not be cached")

            asset = client.get("/static/js/main.js")
            _assert(asset.status_code == 200, f"asset: {asset.status_code}")
            _assert("console.log('asset')" in asset.text, "physical MFE asset was not served")

            missing_asset = client.get("/static/js/missing.js")
            _assert(missing_asset.status_code == 404, f"missing asset: {missing_asset.status_code}")

            missing_api = client.get("/api/not-a-real-route")
            _assert(missing_api.status_code == 404, f"missing API route: {missing_api.status_code}")
        print("[OK] MFE deep-link fallback serves /batch; physical and missing assets remain distinct")
    finally:
        main.MFE_DIR = original_mfe_dir
        shutil.rmtree(temporary_mfe)


if __name__ == "__main__":
    main_test()
