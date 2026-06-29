"""Azure pattern-detection quick-win regression suite.

Locks the three production-grade hardening fixes so they can't silently regress:
  1. recurring_time evidence floor  : distinct days >= PATTERN_MIN_OCCURRENCES
                                       AND ratio  >= PATTERN_MIN_RATIO
  2. ratio surfaced in title        : "(d/N days, P%)" so a PE lead sees the
                                       confidence without reading source
  3. predict_linear time-to-breach  : only emitted when rising AND R2 gate met

Generic only — config-driven thresholds, no customer values.
"""
from datetime import datetime, timedelta
from services import azure_monitor as az, pe_config


def _spikes(days, hr=2, z=3.1):
    return [{"peak_time": f"2025-11-{d:02d}T{hr:02d}:00:00Z", "peak": 90, "z_score": z,
             "severity": "critical", "duration_min": 5} for d in days]


def _rt(patterns):
    return [p for p in patterns if p["type"] == "recurring_time"]


def main():
    print("Azure pattern-detection quick-win suite")
    print("-" * 60)
    occ, ratio = pe_config.PATTERN_MIN_OCCURRENCES, pe_config.PATTERN_MIN_RATIO

    # 2 distinct days on a 15-day window -> 13% -> below ratio gate -> none
    p = az._detect_patterns({"vmA": {"cpu": _spikes([1, 2])}}, hours_back=360)
    assert not _rt(p), "2/15 fluke must be suppressed"
    print("  [OK] 2/15 coincidence suppressed (ratio 13% < gate)")

    # 5 distinct days on 15d -> 33% -> fires, ratio surfaced
    p = _rt(az._detect_patterns({"vmB": {"cpu": _spikes([1, 2, 3, 4, 5])}}, hours_back=360))
    assert p and p[0]["recurrence_days"] == 5, "5/15 must fire"
    assert "33%" in p[0]["title"] and "5/15" in p[0]["title"], f"ratio not surfaced: {p[0]['title']}"
    print(f"  [OK] 5/15 fires, ratio in title: {p[0]['title']!r}")

    # min-occ gate: 2 days never fires regardless of ratio
    p = _rt(az._detect_patterns({"vmC": {"cpu": _spikes([1, 8])}}, hours_back=72))
    assert not p, "2 distinct days < min_occ must be suppressed"
    print(f"  [OK] distinct-day floor enforced (min_occ={occ}, ratio>={ratio})")

    # predict_linear: a clean rising ramp must yield hours_to_warn; flat noise must not
    base = datetime(2025, 11, 1, 0, 0, 0)
    rising = [{"t": (base + timedelta(hours=i)).isoformat() + "Z", "v": 40 + i * 0.5}
              for i in range(48)]
    vm = {"vm-rise": {"series": {"Percentage CPU": rising}}}
    a = az._compute_baseline_analysis(vm, 48)
    rec = a["per_vm"]["vm-rise"]["Percentage CPU"]
    assert rec["trend_direction"] == "rising" and rec["hours_to_warn"] is not None, rec
    assert rec["trend_r2"] >= pe_config.PREDICT_MIN_R2
    print(f"  [OK] rising ramp -> hours_to_warn={rec['hours_to_warn']} (R2={rec['trend_r2']})")

    import random
    random.seed(1)
    flat = [{"t": (base + timedelta(hours=i)).isoformat() + "Z", "v": 50 + random.uniform(-8, 8)}
            for i in range(48)]
    a2 = az._compute_baseline_analysis({"vm-flat": {"series": {"Percentage CPU": flat}}}, 48)
    rec2 = a2["per_vm"]["vm-flat"]["Percentage CPU"]
    assert rec2["hours_to_warn"] is None, "noisy/flat must not project a breach"
    print("  [OK] flat/noisy -> no false time-to-breach (R2 gate holds)")

    print("-" * 60)
    print("ALL PATTERN-DETECTION CHECKS PASSED")


if __name__ == "__main__":
    main()
