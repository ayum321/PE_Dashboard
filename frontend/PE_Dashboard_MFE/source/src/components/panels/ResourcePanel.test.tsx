import React from 'react';
import { render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { buildFleetHeatmapView, durationMinutesFromBounds, fleetHeatmapCellLabel, formatRecurringDurations, formatSpikeWindow, groupCrossServerCorrelations, metricFamily, normalMetricLabels, preferredFleetHeatmapMetric, ResourcePanel, selectDominantMetric, withGapBreaks } from './ResourcePanel';

describe('ResourcePanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ method: 'none' }),
    } as Response);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows the empty state and an Azure connect control when no servers are loaded', async () => {
    const { getByText, getByRole } = render(
      <AppDataProvider>
        <ResourcePanel />
      </AppDataProvider>,
    );

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(getByText(/Upload a resource report/i)).toBeDefined();
    expect(getByRole('button', { name: /Connect Azure/i })).toBeDefined();
  });

  it('builds visible Fleet Heatmap cells for nine servers and preserves the risk direction', () => {
    const heatmap = {
      timestamps: ['2026-08-01T00:00:00Z', '2026-08-01T01:00:00Z', '2026-08-01T02:00:00Z'],
      grids: {
        cpu: Array.from({ length: 9 }, (_, index) => ({ name: `vm-${index + 1}`, values: [12, 83, 18] })),
        memory: Array.from({ length: 9 }, (_, index) => ({ name: `vm-${index + 1}`, values: [88, 39, 71] })),
        disk: Array.from({ length: 9 }, (_, index) => ({ name: `vm-${index + 1}`, values: [null, null, null] })),
      },
    };

    const cpu = buildFleetHeatmapView(heatmap, 'cpu');
    const memory = buildFleetHeatmapView(heatmap, 'memory');
    const disk = buildFleetHeatmapView(heatmap, 'disk');

    expect(cpu?.rows).toHaveLength(9);
    expect(cpu?.rows[0].values).toEqual([12, 83, 18]);
    expect(memory?.rows[0].values).toEqual([88, 39, 71]);
    expect(disk?.columns).toHaveLength(3);
    expect(disk?.rows).toHaveLength(9);
    expect(disk?.rows.every((row) => row.values.every((value) => value == null))).toBe(true);
  });

  it('keeps every observed healthy bucket visible and preserves only genuine telemetry gaps as null', () => {
    const heatmap = {
      timestamps: ['2026-08-01T00:00:00Z', '2026-08-01T01:00:00Z', '2026-08-01T02:00:00Z', '2026-08-01T03:00:00Z'],
      grids: {
        cpu: [
          { name: 'quiet-vm', values: [11, 12, 10, 13] },
          { name: 'missing-metric-vm', values: [null, null, null, null] },
        ],
      },
    };
    const cpu = buildFleetHeatmapView(heatmap, 'cpu');

    expect(cpu?.rows[0].values).toEqual([11, 12, 10, 13]);
    expect(cpu?.rows[1].values).toEqual([null, null, null, null]);
  });

  it('uses the lowest available-memory sample and labels its inverted risk direction', () => {
    const heatmap = {
      timestamps: ['2026-08-01T00:00:00Z', '2026-08-01T01:00:00Z', '2026-08-01T02:00:00Z'],
      grids: {
        memory: [{ name: 'db-pressure', values: [94, 29, 80] }],
        cpu: [{ name: 'cpu-peak', values: [12, 83, 18] }],
      },
    };

    expect(buildFleetHeatmapView(heatmap, 'memory', 1)?.rows[0].values).toEqual([29]);
    expect(buildFleetHeatmapView(heatmap, 'cpu', 1)?.rows[0].values).toEqual([83]);
    expect(fleetHeatmapCellLabel(29, 'memory')).toMatch(/29\.0% available.*lower availability is higher risk/i);
    expect(fleetHeatmapCellLabel(83, 'cpu')).toMatch(/83\.0% utilized.*higher utilization is higher risk/i);
  });

  it('groups cross-server correlation evidence by time and metric without inventing pairs', () => {
    const groups = groupCrossServerCorrelations([
      { type: 'cross_vm_correlation', time_utc: '01:30', metrics: ['Percentage CPU'], vms: ['app-1', 'db-1'], count: 2, severity: 'critical' },
      { type: 'cross_vm_correlation', time_utc: '01:30', metrics: ['Percentage CPU'], vms: ['DB-1.', 'sre-1'], count: 2, severity: 'critical' },
      { type: 'recurring_time', time_utc: '02:00', metrics: ['Percentage CPU'], vms: ['other'] },
    ]);

    expect(groups).toEqual([{ timeUtc: '01:30', metrics: ['Percentage CPU'], vms: ['app-1', 'db-1', 'sre-1'], eventCount: 4, severity: 'critical' }]);
  });

  it('defaults the heatmap to the metric with the material findings', () => {
    expect(preferredFleetHeatmapMetric({
      vms: {
        vm1: { spikes: { 'Percentage CPU': [], 'Available Memory Percentage': [{ severity: 'warning' }] } },
        vm2: { spikes: { 'Available Memory Percentage': [{ severity: 'critical_sustained' }] } },
      },
    })).toBe('memory');
  });

  it('keeps card severity and headline on the same metric', () => {
    const selected = selectDominantMetric(
      {
        'Percentage CPU': { p95: 59, max: 99 },
        'Available Memory Percentage': { min: 4, p5: 7, max: 18 },
      },
      {
        'Percentage CPU': [{ severity: 'critical_sustained', peak: 99 }],
        'Available Memory Percentage': [{ severity: 'warning', peak: 5.5 }],
      },
    );

    expect(selected?.label).toBe('CPU');
    expect(selected?.severityLabel).toBe('CRITICAL SUSTAINED');
  });

  it('uses the observed memory floor only when the card says MIN AVAIL', () => {
    const selected = selectDominantMetric(
      { 'Available Memory Percentage': { min: 4, p5: 7, min_anomalous: true } },
      { 'Available Memory Percentage': [{ severity: 'warning', peak: 5.5 }] },
    );

    expect(selected?.value).toBe(4);
    expect(metricFamily('Available Memory Bytes')).toBe('memory-bytes');
  });

  it('deduplicates aliased normal metrics and excludes a flagged family', () => {
    const labels = normalMetricLabels(
      {
        'Percentage CPU': {},
        'Available Memory Percentage': {},
        'Available Memory Bytes': {},
        'OS Disk Bandwidth Consumed Percentage': {},
      },
      { Memory: [{ severity: 'warning' }] },
    );

    expect(labels).toEqual(['CPU', 'OS Disk']);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it('renders the same UTC bounds and duration semantics in every spike table', () => {
    expect(durationMinutesFromBounds('2026-08-15T18:40:00Z', '2026-08-15T20:40:00Z')).toBe(120);
    expect(formatSpikeWindow('2026-08-15T18:40:00Z', '2026-08-15T20:40:00Z')).toMatch(/Aug 15, 06:40 PM → Aug 15, 08:40 PM UTC/);
    expect(formatRecurringDurations([60, 240])).toBe('1.0h–4.0h per event');
  });

  // Numeric audit: does the chart actually distinguish a genuine Azure Monitor
  // telemetry gap from a real, gradually-changing value? Fixtures below are
  // built from real timestamps pulled directly out of a captured production
  // snapshot (data/report_snapshots/nfm/aud-20260828t065154z-1c5b1cd5.json,
  // host tsbf141403011, "Disk Write Bytes"), not invented numbers.
  it('does not flag a real, uniformly-sampled hourly series as having a gap', () => {
    // Real cadence: exactly 60-minute buckets, zero missing samples (confirmed
    // 359/359 consecutive deltas == 60min across the full captured series).
    const hourly = Array.from({ length: 6 }, (_, i) => ({
      t: new Date(Date.UTC(2026, 7, 13, 6 + i, 50)).toISOString(),
      v: 10 + i,
    }));
    const result = withGapBreaks(hourly);
    expect(result.sawGap).toBe(false);
    expect(result.data).toHaveLength(hourly.length);
    expect(result.data.every(([, v]) => v !== null)).toBe(true);
  });

  it('inserts a visible break at the exact bucket Azure Monitor genuinely skipped', () => {
    // Real gap: this host's series runs 18:50 -> (missing 19:50) -> 20:50,
    // confirmed via direct inspection of the captured payload (delta_min=120
    // where every neighboring bucket in the same series is 60).
    const points = [
      { t: '2026-08-19T16:50:00.000Z', v: 4.2e9 },
      { t: '2026-08-19T17:50:00.000Z', v: 4.1e9 },
      { t: '2026-08-19T18:50:00.000Z', v: 4.3e9 },
      { t: '2026-08-19T20:50:00.000Z', v: 3.9e9 }, // the real, captured 120-min jump
      { t: '2026-08-19T21:50:00.000Z', v: 4.0e9 },
    ];
    const result = withGapBreaks(points);
    expect(result.sawGap).toBe(true);
    // 5 real points + exactly 1 inserted null break, not one per pre-existing point.
    expect(result.data).toHaveLength(6);
    const nullEntries = result.data.filter(([, v]) => v === null);
    expect(nullEntries).toHaveLength(1);
    // The break sits at the midpoint of the real gap, not at either endpoint,
    // so Highcharts draws it as a break rather than silently connecting
    // 18:50 straight to 20:50 as if the value changed gradually in between.
    const midpoint = (new Date('2026-08-19T18:50:00.000Z').getTime() + new Date('2026-08-19T20:50:00.000Z').getTime()) / 2;
    expect(nullEntries[0][0]).toBe(midpoint);
  });

  it('detects a gap even when only two normal cadence deltas are available', () => {
    const points = [
      { t: '2026-08-19T16:50:00.000Z', v: 1 },
      { t: '2026-08-19T17:50:00.000Z', v: 2 },
      { t: '2026-08-19T18:50:00.000Z', v: 3 },
      { t: '2026-08-20T01:50:00.000Z', v: 4 },
    ];
    const result = withGapBreaks(points);

    expect(result.sawGap).toBe(true);
    expect(result.data.filter(([, value]) => value === null)).toHaveLength(1);
  });
});
