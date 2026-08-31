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


if __name__ == "__main__":
    unittest.main()
