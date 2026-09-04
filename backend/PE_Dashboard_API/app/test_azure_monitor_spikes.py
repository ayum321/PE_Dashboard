from datetime import datetime, timedelta, timezone
import unittest

from services.azure_monitor import _detect_spikes


def _points(values, offsets):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [
        {"t": (start + timedelta(hours=offset)).isoformat(), "v": value}
        for value, offset in zip(values, offsets)
    ]


class AzureMonitorSpikeDurationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

