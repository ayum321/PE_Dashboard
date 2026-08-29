"""Regression guard for SOW actuals surviving React route changes.

Run from ``backend/PE_Dashboard_API/app``:
``py -3.14 _test_sow_state_persistence.py``.
"""
from __future__ import annotations


def main() -> None:
    from routers import sow

    config: dict[str, object] = {}
    audit_context: dict[str, object] = {}
    original_config_get = sow.config_store.get
    original_config_set = sow.config_store.set
    original_ac_get = sow.session_cache.ac_get
    original_ac_set = sow.session_cache.ac_set
    try:
        sow.config_store.get = lambda key: config.get(key)  # type: ignore[method-assign]
        sow.config_store.set = lambda key, value: config.__setitem__(key, value)  # type: ignore[method-assign]
        sow.session_cache.ac_get = lambda key: audit_context.get(key)  # type: ignore[method-assign]
        sow.session_cache.ac_set = lambda key, value: audit_context.__setitem__(key, value)  # type: ignore[method-assign]

        saved = sow.save_baseline(sow.SowBaseline(daily_dfu=9_000_000, daily_sku=9_000_000))
        assert saved == {"daily_dfu": 9_000_000.0, "daily_sku": 9_000_000.0}, saved

        comparison = sow.compare_sow(sow.SowCompareRequest(actuals={"daily_dfu": 7_968_993}))
        assert len(comparison.metrics) == 1, comparison.model_dump()
        metric = comparison.metrics[0]
        assert metric.key == "daily_dfu" and metric.actual == 7_968_993.0, metric.model_dump()
        assert metric.status != "LOW", metric.model_dump()

        restored = sow.get_sow_state()
        assert restored["actuals"] == {"daily_dfu": 7_968_993.0}, restored
        assert restored["compare"]["metrics"][0]["actual"] == 7_968_993.0, restored
        assert all(m["actual"] != 0 for m in restored["compare"]["metrics"]), restored

        awaiting = sow.compare_sow(sow.SowCompareRequest(actuals={}))
        assert awaiting.overall_status == "AWAITING_ACTUALS", awaiting.model_dump()
        assert awaiting.metrics == [], awaiting.model_dump()
        print("[OK] SOW actuals persist across routes and missing values never become false zero-volume findings")
    finally:
        sow.config_store.get = original_config_get  # type: ignore[method-assign]
        sow.config_store.set = original_config_set  # type: ignore[method-assign]
        sow.session_cache.ac_get = original_ac_get  # type: ignore[method-assign]
        sow.session_cache.ac_set = original_ac_set  # type: ignore[method-assign]


if __name__ == "__main__":
    main()
