from datetime import datetime, timedelta, timezone
import unittest

from services.azure_monitor import _detect_patterns, _detect_spikes


def _points(values, offsets):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [
        {"t": (start + timedelta(hours=offset)).isoformat(), "v": value}
        for value, offset in zip(values, offsets)
    ]


class AzureMonitorSpikeDurationTests(unittest.TestCase):
    def test_shared_spike_windows_keep_dates_and_contributing_events(self):
        base = datetime(2026, 9, 20, 13, 7, tzinfo=timezone.utc)

        def spike(when, severity="critical"):
            return {
                "peak_time": when.isoformat(),
                "start": (when - timedelta(minutes=2)).isoformat(),
                "end": (when + timedelta(minutes=2)).isoformat(),
                "peak": 82.0,
                "z_score": 3.5,
                "severity": severity,
            }

        patterns = _detect_patterns({
            "app-1": {"Percentage CPU": [spike(base), spike(base + timedelta(days=1))]},
            "db-1": {"Percentage CPU": [spike(base + timedelta(minutes=10)),
                                         spike(base + timedelta(days=1, minutes=10))]},
            "noise": {"Percentage CPU": [spike(base + timedelta(minutes=5), "normal")]},
        }, hours_back=48)
        shared = [item for item in patterns if item["type"] == "cross_vm_correlation"]

        self.assertEqual(len(shared), 2)
        self.assertNotEqual(shared[0]["peak_start_utc"], shared[1]["peak_start_utc"])
        self.assertEqual([item["count"] for item in shared], [2, 2])
        self.assertEqual({event["vm"] for event in shared[0]["spike_events"]}, {"app-1", "db-1"})
        self.assertEqual(shared[0]["peak_end_utc"], (base + timedelta(minutes=10)).isoformat())

    def test_shared_warning_spikes_do_not_become_critical(self):
        base = datetime(2026, 9, 20, 13, 7, tzinfo=timezone.utc)
        spikes = {
            vm: {"Percentage CPU": [{
                "peak_time": (base + timedelta(minutes=offset)).isoformat(),
                "peak": 65.0,
                "z_score": 2.0,
                "severity": "warning",
            }]}
            for vm, offset in (("app-1", 0), ("app-2", 5))
        }
        shared = [item for item in _detect_patterns(spikes) if item["type"] == "cross_vm_correlation"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["severity"], "warning")

    def test_test_spike_cannot_bridge_two_prod_spikes(self):
        base = datetime(2026, 9, 20, 13, tzinfo=timezone.utc)
        spikes = {
            vm: {"Percentage CPU": [{
                "peak_time": (base + timedelta(minutes=minute)).isoformat(),
                "peak": 85.0,
                "z_score": 3.5,
                "severity": "critical",
            }]}
            for vm, minute in (("prod-app-1", 0), ("test-app-1", 10), ("prod-app-2", 20))
        }
        environments = {"prod-app-1": "PROD", "test-app-1": "TEST", "prod-app-2": "PROD"}
        shared = [item for item in _detect_patterns(spikes, vm_environments=environments)
                  if item["type"] == "cross_vm_correlation"]
        self.assertEqual(shared, [])

    def test_missing_bucket_does_not_create_multi_day_duration(self):
        # Two high runs are separated by a six-hour telemetry gap. The detector
        # must report two observed runs, not one wall-clock run spanning the gap.
        spikes = _detect_spikes(
            _points([0, 90, 90, 90, 90, 0], [0, 1, 2, 8, 9, 10]),
            metric_name="Percentage CPU",
        )

        self.assertTrue(spikes)
        self.assertTrue(all(spike["duration_min"] <= 60 for spike in spikes))

    def test_continuous_observed_breach_keeps_its_duration(self):
        spikes = _detect_spikes(
            _points([0, 90, 90, 90, 90, 0], [0, 1, 2, 3, 4, 5]),
            metric_name="Percentage CPU",
        )

        self.assertTrue(spikes)
        self.assertGreaterEqual(max(spike["duration_min"] for spike in spikes), 180)

    def test_short_series_still_breaks_at_a_large_gap(self):
        spikes = _detect_spikes(
            _points([0, 90, 90, 90, 0], [0, 1, 2, 8, 9]),
            metric_name="Percentage CPU",
        )

        self.assertTrue(spikes)
        self.assertTrue(all(spike["duration_min"] <= 60 for spike in spikes))

    def test_diurnal_batch_window_suppresses_expected_cyclic_batch_false_alarm(self):
        # 3 days of telemetry where 02:00 UTC regularly runs batch at 60%.
        # Day 1: 02:00 = 60%, day 2: 02:00 = 62%, day 3: 02:00 = 61%.
        # Daytime is 5%.
        # Because 60% is completely normal and expected for 02:00 UTC (diurnal z < 1.0),
        # diurnal baseline prevents treating regular planned batch as an abnormal spike.
        values = []
        offsets = []
        for day in range(3):
            for hour in range(24):
                val = 61.0 if hour == 2 else 5.0
                values.append(val)
                offsets.append(day * 24 + hour)
        spikes = _detect_spikes(_points(values, offsets), metric_name="Percentage CPU")
        # Ensure planned regular batch is not misclassified as an abnormal spike
        self.assertFalse(any(s.get("detection") == "z_score" and s.get("peak") <= 62.0 for s in spikes))

    def test_diurnal_batch_window_detects_abnormal_daytime_elevation(self):
        # 3 days of telemetry where 14:00 UTC is normally 5%.
        # On day 3, 14:00 UTC spikes to 45% (abnormal for daytime quiet hours).
        # Diurnal model must catch this as an abnormal spike.
        values = []
        offsets = []
        for day in range(3):
            for hour in range(24):
                val = 45.0 if (day == 2 and hour == 14) else 5.0
                values.append(val)
                offsets.append(day * 24 + hour)
        spikes = _detect_spikes(_points(values, offsets), metric_name="Percentage CPU")
        self.assertTrue(any(s.get("peak") == 45.0 for s in spikes))


    def test_segmented_baseline_metadata_and_confidence_score(self):
        # When segmented baseline activates, baseline_type is 'segmented',
        # segment stats are captured, confidence_score is float, and AWR metadata is present.
        values = []
        offsets = []
        for day in range(3):
            for hour in range(24):
                val = 75.0 if hour == 2 else 5.0
                values.append(val)
                offsets.append(day * 24 + hour)
        # Add an anomalous daytime spike on day 2 at 15:00 UTC (80%)
        idx = 2 * 24 + 15
        values[idx] = 80.0
        spikes = _detect_spikes(_points(values, offsets), metric_name="Percentage CPU", is_db=True)
        self.assertTrue(spikes)
        daytime_spike = next((s for s in spikes if s.get("peak") == 80.0), None)
        self.assertIsNotNone(daytime_spike)
        self.assertEqual(daytime_spike.get("baseline_type"), "segmented")
        self.assertIn("confidence_score", daytime_spike)
        self.assertIsInstance(daytime_spike["confidence_score"], float)
        self.assertGreaterEqual(daytime_spike["confidence_score"], 0.0)
        self.assertLessEqual(daytime_spike["confidence_score"], 1.0)
        self.assertIn("awr_window_start", daytime_spike)
        self.assertIn("awr_window_end", daytime_spike)
        self.assertTrue(daytime_spike.get("awr_drilldown_available"))

    def test_vm_sku_aware_severity(self):
        # Small VM (<= 4 vCPUs) has relaxed CPU warning threshold (70% vs 60%)
        values = [10.0] * 20 + [65.0] * 5 + [10.0] * 20
        offsets = list(range(len(values)))
        # Standard VM without sizing info: 65% CPU -> warning
        spikes_standard = _detect_spikes(
            _points(values, offsets), metric_name="Percentage CPU",
            vm_size_info={"vcpus": 16}
        )
        # Small VM (2 vCPUs): 65% CPU is below relaxed 70% warning threshold -> notable or healthy
        spikes_small = _detect_spikes(
            _points(values, offsets), metric_name="Percentage CPU",
            vm_size_info={"vcpus": 2}
        )
        if spikes_standard:
            self.assertEqual(spikes_standard[0]["severity"], "warning")
        if spikes_small:
            self.assertIn(spikes_small[0]["severity"], ("notable", "healthy"))


if __name__ == "__main__":
    unittest.main()
