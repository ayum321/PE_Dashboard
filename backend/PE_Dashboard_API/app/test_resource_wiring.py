from services.resource_calculator import build_resource_payload
from routers.azure_resource import _snapshot_observation_window, _ts_cache_key


def test_normalized_azure_resource_payload_is_idempotent():
    raw = {
        "host": "app-01",
        "type": "APP",
        "source": "azure_monitor",
        "cpu_used": 88.0,
        "cpu_avg": 57.0,
        "cpu_max_pct": 95.0,
        "mem_used": 62.0,
        "mem_avg": 62.0,
        "mem_max_pct": 78.0,
        "disk_used_max": 21.0,
    }

    first = build_resource_payload([raw])
    second = build_resource_payload(first["servers"])

    assert first["servers"][0]["cpu_avg_pct"] == 57.0
    assert first["servers"][0]["mem_avg_pct"] == 62.0
    assert second["servers"][0]["cpu_avg_pct"] == 57.0
    assert second["servers"][0]["mem_avg_pct"] == 62.0
    assert second["servers"][0]["health_score"] == first["servers"][0]["health_score"]


def test_snapshot_window_exposes_exact_requested_range_and_math_basis():
    from datetime import datetime, timezone

    end = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    window = _snapshot_observation_window(720, end)

    assert window["start_utc"] == "2026-07-31T12:00:00Z"
    assert window["end_utc"] == "2026-08-30T12:00:00Z"
    assert window["requested_hours"] == 720
    assert window["snapshot_grain_hours"] == 6
    assert "missing buckets are excluded" in window["definitions"]["avg"]


def test_timeseries_cache_separates_environment_scopes():
    args = ("session-1", ["/vm/app-1"], 24, None, None, {"/vm/app-1": "APP"})
    prod = _ts_cache_key(*args, {"/vm/app-1": "PROD"})
    test = _ts_cache_key(*args, {"/vm/app-1": "TEST"})
    assert prod != test
