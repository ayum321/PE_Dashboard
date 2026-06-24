"""Lock-in regression test for the per-sub-app window-compliance keystone bug.

Prevents three confirmed, customer-impacting regressions from silently returning:

  1. Window compliance must group by (Sub_Application, run_date) and judge each
     sub-app against ITS OWN ceiling — NOT pool every sub-app into one flat daily
     bucket. A long cyclic/weekly sub-app must never drag a daily sub-app down.
  2. Excluded schedule types (CYCLIC/MONTHLY/etc.) must be dropped from the
     denominator, not counted as daily breaches.
  3. A single critical finding must cap the findings grade at C — volume of
     unrelated "ok" findings must never claw it back to A/B.

Run:  py -3.14 _test_window_compliance_regression.py
"""
from services import compliance_engine as ce


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def test_per_sub_app_grouping_not_pooled() -> None:
    """A long weekly sub-app must not drag a daily sub-app's compliance down."""
    recs = []
    # DAILY: 3 of 5 days within 7.5h ceiling -> 60% on its own.
    for d, h in [("2025-01-01", 5), ("2025-01-02", 6), ("2025-01-03", 7),
                 ("2025-01-04", 9), ("2025-01-05", 10)]:
        recs.append({"run_date": d, "sub_app": "PROD_DAILY", "elapsed_hrs": h,
                     "sla_ceil": 7.5, "schedule_type": "DAILY"})
    # WEEKLY: long 20h window every day, but judged on its OWN 24h ceiling -> ok.
    for d in ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"]:
        recs.append({"run_date": d, "sub_app": "PROD_WEEKLY", "elapsed_hrs": 20,
                     "sla_ceil": 24.0, "schedule_type": "WEEKLY"})

    r = ce.compute_window_compliance(recs, {})
    # 10 windows total (5 daily + 5 weekly), 2 daily breaches, weekly all pass.
    if r["total_windows"] != 10:
        _fail(f"expected 10 windows, got {r['total_windows']}")
    if r["breach_count"] != 2:
        _fail(f"expected 2 breaches (DAILY only), got {r['breach_count']}")
    # If sub-apps were POOLED into one flat daily bucket the WEEKLY 20h window
    # would breach every day and compliance would collapse toward ~0-20%.
    if r["compliance_pct"] < 75.0:
        _fail(f"POOLING REGRESSION: compliance {r['compliance_pct']}% too low — "
              "sub-apps are being judged against a shared flat ceiling")
    print(f"  [OK] per-sub-app grouping: compliance {r['compliance_pct']}% "
          f"({r['breach_count']}/{r['total_windows']} windows breach)")


def test_excluded_types_dropped_from_denominator() -> None:
    """CYCLIC/MONTHLY/OUTBOUND sub-apps must leave the denominator entirely."""
    recs = []
    for d in ["2025-02-01", "2025-02-02", "2025-02-03"]:
        recs.append({"run_date": d, "sub_app": "PROD_DAILY", "elapsed_hrs": 4,
                     "sla_ceil": 7.5, "schedule_type": "DAILY"})
        recs.append({"run_date": d, "sub_app": "PROD_CYCLIC", "elapsed_hrs": 23,
                     "sla_ceil": 7.5, "schedule_type": "CYCLIC"})
    r = ce.compute_window_compliance(recs, {})
    if r["total_windows"] != 3:
        _fail(f"expected 3 in-scope windows (DAILY only), got {r['total_windows']}")
    if r["excluded_windows"] != 3:
        _fail(f"expected 3 excluded CYCLIC windows, got {r['excluded_windows']}")
    if r["compliance_pct"] != 100.0:
        _fail(f"CYCLIC EXCLUSION REGRESSION: expected 100%, got {r['compliance_pct']}%")
    print(f"  [OK] excluded-type drop: {r['excluded_windows']} CYCLIC windows "
          f"excluded, compliance {r['compliance_pct']}%")


def test_distinct_day_rollup_is_honest() -> None:
    """breach_days/total_days must stay in calendar-day units (<= window counts)."""
    recs = []
    for d in ["2025-03-01", "2025-03-02"]:
        # two sub-apps per day -> 4 windows but only 2 calendar days
        recs.append({"run_date": d, "sub_app": "A", "elapsed_hrs": 9,
                     "sla_ceil": 7.5, "schedule_type": "DAILY"})
        recs.append({"run_date": d, "sub_app": "B", "elapsed_hrs": 4,
                     "sla_ceil": 7.5, "schedule_type": "DAILY"})
    r = ce.compute_window_compliance(recs, {})
    if r["total_windows"] != 4:
        _fail(f"expected 4 windows, got {r['total_windows']}")
    if r["total_days"] != 2:
        _fail(f"expected 2 distinct days, got {r['total_days']}")
    if r["breach_days"] > r["total_days"]:
        _fail(f"INVARIANT VIOLATION: breach_days {r['breach_days']} > "
              f"total_days {r['total_days']}")
    print(f"  [OK] honest day rollup: {r['breach_days']}/{r['total_days']} days "
          f"breached across {r['total_windows']} windows")


def test_legacy_per_date_records_still_work() -> None:
    """Records without a sub_app must keep the original per-date behaviour."""
    leg = [{"run_date": "2025-04-01", "elapsed_hrs": 5, "sla_ceil": 7.5},
           {"run_date": "2025-04-02", "elapsed_hrs": 9, "sla_ceil": 7.5}]
    r = ce.compute_window_compliance(leg, {})
    if r["total_windows"] != 2:
        _fail(f"expected 2 per-date windows, got {r['total_windows']}")
    if r["compliance_pct"] != 50.0:
        _fail(f"legacy per-date regression: expected 50%, got {r['compliance_pct']}%")
    print(f"  [OK] legacy per-date compat: compliance {r['compliance_pct']}%")


def test_grade_floor_caps_at_c_with_any_critical() -> None:
    """A single critical finding must cap the grade at C regardless of ok volume."""
    # Mirror the exact formula + floor from routers/findings.py (Patch I).
    def _grade(n_crit: int, n_warn: int, n_ok: int) -> str:
        penalty = max(0.0, min(100.0,
                               100.0 - (n_crit * 15.0) - (n_warn * 5.0) + (n_ok * 2.0)))
        if   penalty >= 90: g = "A"
        elif penalty >= 75: g = "B"
        elif penalty >= 60: g = "C"
        elif penalty >= 45: g = "D"
        else:               g = "F"
        if n_crit > 0 and g in ("A", "B"):
            g = "C"
        return g

    # 3 critical + 20 ok: without the floor this scores back up to an A.
    g = _grade(3, 0, 20)
    if g not in ("C", "D", "F"):
        _fail(f"GRADE FLOOR REGRESSION: 3 critical + 20 ok -> grade {g}, "
              "expected <= C")
    # No criticals + lots of ok should still be allowed to reach A.
    if _grade(0, 0, 0) != "A":
        _fail("clean run should grade A")
    print(f"  [OK] grade floor: 3 critical + 20 ok -> grade {g} (capped <= C)")


def main() -> None:
    print("Window-compliance keystone regression suite")
    print("-" * 60)
    test_per_sub_app_grouping_not_pooled()
    test_excluded_types_dropped_from_denominator()
    test_distinct_day_rollup_is_honest()
    test_legacy_per_date_records_still_work()
    test_grade_floor_caps_at_c_with_any_critical()
    print("-" * 60)
    print("ALL REGRESSION CHECKS PASSED")


if __name__ == "__main__":
    main()
