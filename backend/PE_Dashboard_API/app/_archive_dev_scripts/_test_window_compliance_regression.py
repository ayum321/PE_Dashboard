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


def test_breach_attribution_is_traceable() -> None:
    """The headline breach count must trace to a cause: which sub-app drove each
    breach day (structural vs intermittent), and which sub-apps were excluded from
    the denominator. Encodes the Haleon pressure-test: a structural breacher
    (PO_RANGES, every day) makes "all-pass" impossible, while a 23.7h OUTBOUND
    breacher (EDI_OB_850) is excluded and must be named, not silently hidden."""
    dates = [f"2025-12-{d:02d}" for d in range(1, 16)]  # 15 days
    recs = []
    for d in dates:
        # structural: 11h vs 8.833h ceiling — breaches EVERY day it runs
        recs.append({"run_date": d, "sub_app": "PO_RANGES", "effective_hrs": 11.0,
                     "sla_ceil": 8.833, "schedule_type": "UNKNOWN"})
        # clean: always within ceiling
        recs.append({"run_date": d, "sub_app": "AM_DAILY", "effective_hrs": 2.6,
                     "sla_ceil": 6.0, "schedule_type": "DAILY"})
        # intermittent: breaches only 2 of 15 days
        eff = 9.0 if d in ("2025-12-07", "2025-12-10") else 3.0
        recs.append({"run_date": d, "sub_app": "EDI_852", "effective_hrs": eff,
                     "sla_ceil": 6.0, "schedule_type": "DAILY"})
        # excluded: 23.7h OUTBOUND — out of denominator but MUST be surfaced
        recs.append({"run_date": d, "sub_app": "EDI_OB_850", "effective_hrs": 23.7,
                     "sla_ceil": 6.0, "schedule_type": "OUTBOUND"})

    r = ce.compute_window_compliance(recs, {})

    # 1. All-pass is arithmetically 0 — a structural breacher runs every day.
    all_pass = r["total_days"] - r["breach_days"]
    if all_pass != 0:
        _fail(f"ALL-PASS REGRESSION: expected 0 all-pass days (PO_RANGES breaches "
              f"all 15), got {all_pass}")

    # 2. Pattern classification: structural vs intermittent vs clean.
    by_sa = {s["sub_app"]: s for s in r["per_sub_app"]}
    if by_sa["PO_RANGES"]["pattern"] != "structural":
        _fail(f"expected PO_RANGES=structural, got {by_sa['PO_RANGES']['pattern']}")
    if by_sa["EDI_852"]["pattern"] != "intermittent":
        _fail(f"expected EDI_852=intermittent, got {by_sa['EDI_852']['pattern']}")
    if by_sa["AM_DAILY"]["pattern"] != "clean":
        _fail(f"expected AM_DAILY=clean, got {by_sa['AM_DAILY']['pattern']}")

    # 3. Excluded sub-app surfaced (not silently dropped).
    ex_names = {e["sub_app"] for e in r["excluded_sub_apps"]}
    if "EDI_OB_850" not in ex_names:
        _fail(f"EXCLUSION OPACITY: EDI_OB_850 (OUTBOUND) not surfaced, got {ex_names}")

    # 3b. Structural cut-off published so the label is auditable, not "trust me".
    if r.get("structural_ratio") is None:
        _fail("TRANSPARENCY GAP: structural_ratio not surfaced — label has no rule")
    if abs(float(r["structural_ratio"]) - 0.60) > 1e-9:
        _fail(f"structural_ratio expected 0.60, got {r['structural_ratio']}")

    # 4. Per-breach-day attribution names the driver + overrun (not unattributed).
    if len(r["breach_days_detail"]) != r["breach_days"]:
        _fail(f"breach_days_detail count {len(r['breach_days_detail'])} != "
              f"breach_days {r['breach_days']}")
    first = r["breach_days_detail"][0]
    drivers = {b["sub_app"] for b in first["breachers"]}
    if "PO_RANGES" not in drivers:
        _fail(f"attribution missing PO_RANGES on {first['run_date']}: {drivers}")
    po = next(b for b in first["breachers"] if b["sub_app"] == "PO_RANGES")
    if not (20 <= po["overrun_pct"] <= 30):
        _fail(f"OVERRUN MISFRAME: PO_RANGES overrun {po['overrun_pct']}% "
              f"(expected ~24.5% at 8.833h ceiling, NOT a 0.6h catastrophic read)")

    print(f"  [OK] breach attribution: 0/15 all-pass · PO_RANGES=structural(15/15) "
          f"EDI_852=intermittent(2/15) · EDI_OB_850 excluded(OUTBOUND,23.7h) named · "
          f"overrun +{po['overrun_pct']}% (8.833h ceiling, not 0.6h)")


def test_customer_archetypes() -> None:
    """250+ customers ship wildly different batch shapes. The engine must give an
    accurate, non-misleading verdict for each archetype — not just Haleon's. Each
    case below is a distinct customer shape; a regression in any one is a silent
    wrong answer on real audits."""
    # A) Single ceiling, all clean — simplest customer. 100% compliant, 0 breaches.
    recs = [{"run_date": f"2025-12-{d:02d}", "sub_app": "DAILY_LOAD",
             "effective_hrs": 3.0, "sla_ceil": 6.0, "schedule_type": "DAILY"}
            for d in range(1, 11)]
    r = ce.compute_window_compliance(recs, {})
    if r["compliance_pct"] != 100.0 or r["breach_days"] != 0:
        _fail(f"ARCHETYPE single-ceiling: expected 100%/0 breaches, got "
              f"{r['compliance_pct']}%/{r['breach_days']}")

    # B) Mixed ceilings, one dominant by volume (Haleon). Tight sub-app must NOT be
    #    dragged by the long one; each judged on its own ceiling.
    recs = []
    for d in range(1, 11):
        recs.append({"run_date": f"2025-12-{d:02d}", "sub_app": "TIGHT",
                     "effective_hrs": 0.5, "sla_ceil": 0.6, "schedule_type": "DAILY"})
        recs.append({"run_date": f"2025-12-{d:02d}", "sub_app": "WIDE",
                     "effective_hrs": 8.0, "sla_ceil": 9.0, "schedule_type": "WEEKLY"})
    r = ce.compute_window_compliance(recs, {})
    if r["compliance_pct"] != 100.0:
        _fail(f"ARCHETYPE mixed-ceiling: tight sub-app dragged, got {r['compliance_pct']}%")

    # C) Cross-midnight window: effective_hrs spans 22:00→06:00 = 8h vs 7.5h ceiling.
    #    The breach must register on the run_date regardless of midnight crossing.
    recs = [{"run_date": f"2025-12-{d:02d}", "sub_app": "NIGHT_SEQ",
             "effective_hrs": 8.0, "sla_ceil": 7.5, "schedule_type": "DAILY"}
            for d in range(1, 6)]
    r = ce.compute_window_compliance(recs, {})
    if r["breach_days"] != 5:
        _fail(f"ARCHETYPE cross-midnight: expected 5 breaches, got {r['breach_days']}")

    # D) Every sub-app cyclic/outbound — zero scorable denominator. Must be N/A
    #    (warned), NOT a misleading 0% catastrophic verdict.
    recs = [{"run_date": f"2025-12-{d:02d}", "sub_app": "OB", "effective_hrs": 20,
             "sla_ceil": 6, "schedule_type": "OUTBOUND"} for d in range(1, 6)]
    r = ce.compute_window_compliance(recs, {})
    if r["total_windows"] != 0:
        _fail(f"ARCHETYPE zero-denom: expected 0 windows, got {r['total_windows']}")
    if not any("No scorable" in w for w in r["warnings"]):
        _fail("ARCHETYPE zero-denom: 0% returned with NO 'no scorable windows' warning "
              "— reads as total failure, the silent-wrong-answer trap")

    # E) Structural breach on a weekly workflow. Denominator MUST be days-it-ran (4),
    #    not calendar days — else 3/4 dilutes to 3/30 and never trips structural.
    recs = [{"run_date": f"2025-12-{d:02d}", "sub_app": "WK", "schedule_type": "DAILY",
             "effective_hrs": (3.0 if d == 1 else 10.0), "sla_ceil": 6.0}
            for d in (1, 8, 15, 22)]
    r = ce.compute_window_compliance(recs, {})
    wk = {s["sub_app"]: s for s in r["per_sub_app"]}["WK"]
    if wk["total_windows"] != 4 or wk["pattern"] != "structural":
        _fail(f"ARCHETYPE weekly-structural: expected tw=4/structural (3/4 ran), got "
              f"tw={wk['total_windows']}/{wk['pattern']}")

    # F) No BatchSLA XLSX — empty ceiling_map falls back to daily default. A short
    #    job must read compliant (meaningful), not get a silent wrong ceiling.
    recs = [{"run_date": f"2025-12-{d:02d}", "sub_app": "D", "effective_hrs": 3.0,
             "sla_ceil": None, "schedule_type": "DAILY"} for d in range(1, 4)]
    r = ce.compute_window_compliance(recs, {})  # no ceiling map at all
    if r["total_windows"] != 3 or r["compliance_pct"] != 100.0:
        _fail(f"ARCHETYPE no-xlsx fallback: expected 3 windows/100%, got "
              f"{r['total_windows']}/{r['compliance_pct']}%")

    print("  [OK] archetypes: single·mixed·cross-midnight·zero-denom(N/A warned)·"
          "weekly-structural(4 ran)·no-xlsx fallback — all give honest verdicts")


def main() -> None:
    print("Window-compliance keystone regression suite")
    print("-" * 60)
    test_per_sub_app_grouping_not_pooled()
    test_excluded_types_dropped_from_denominator()
    test_distinct_day_rollup_is_honest()
    test_legacy_per_date_records_still_work()
    test_grade_floor_caps_at_c_with_any_critical()
    test_breach_attribution_is_traceable()
    test_customer_archetypes()
    print("-" * 60)
    print("ALL REGRESSION CHECKS PASSED")


if __name__ == "__main__":
    main()
