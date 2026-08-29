import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import * as dashboardApi from '../../api/dashboardApi';
import { AppDataProvider } from '../../context/AppDataContext';
import {
  buildFleetHeatmapView,
  durationMinutesFromBounds,
  fleetHeatmapCellLabel,
  formatRecurringDurations,
  formatSpikeWindow,
  groupCrossServerCorrelations,
  metricFamily,
  normalMetricLabels,
  preferredFleetHeatmapMetric,
  ResourcePanel,
  selectDominantMetric,
  withGapBreaks,
} from './ResourcePanel';
import { useAppData } from '../../context/AppDataContext';

jest.mock('highcharts-react-official', () => () => <div data-testid="highcharts-react" />);
jest.mock('../../api/dashboardApi', () => {
  const actual = jest.requireActual('../../api/dashboardApi');
  return {
    ...actual,
    fetchAzureTimeseries: jest.fn(),
    getAzureAuthStatus: jest.fn(),
    getAzureStatus: jest.fn(),
    getSowState: jest.fn(),
    processResource: jest.fn(),
  };
});

const mockedFetchAzureTimeseries = dashboardApi.fetchAzureTimeseries as jest.MockedFunction<typeof dashboardApi.fetchAzureTimeseries>;
const mockedGetAzureAuthStatus = dashboardApi.getAzureAuthStatus as jest.MockedFunction<typeof dashboardApi.getAzureAuthStatus>;
const mockedGetAzureStatus = dashboardApi.getAzureStatus as jest.MockedFunction<typeof dashboardApi.getAzureStatus>;
const mockedGetSowState = dashboardApi.getSowState as jest.MockedFunction<typeof dashboardApi.getSowState>;
const mockedProcessResource = dashboardApi.processResource as jest.MockedFunction<typeof dashboardApi.processResource>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function buildSeries(values: number[]) {
  return values.map((value, index) => ({
    t: new Date(Date.UTC(2026, 7, 1, index, 0, 0)).toISOString(),
    v: value,
  }));
}

function buildDeepDive({
  vmName = 'vm-critical',
  degraded = false,
  hoursBack = 24,
  resourceId = 'vm-1',
}: {
  vmName?: string;
  degraded?: boolean;
  hoursBack?: number;
  resourceId?: string;
} = {}) {
  return {
    vms: {
      [vmName]: {
        resource_id: resourceId,
        series: {
          'Percentage CPU': buildSeries([24, 30, 45, 82, 95, 68]),
          'Available Memory Percentage': buildSeries([48, 46, 42, 35, 22, 28]),
          'OS Disk Bandwidth Consumed Percentage': buildSeries([8, 10, 14, 18, 21, 12]),
        },
        series_max: {
          'Percentage CPU': buildSeries([28, 35, 52, 88, 99, 75]),
          'Available Memory Percentage': buildSeries([48, 46, 42, 35, 22, 28]),
        },
        spikes: {
          'Percentage CPU': [{
            start: '2026-08-01T03:00:00.000Z',
            end: '2026-08-01T05:00:00.000Z',
            peak: 95,
            peak_time: '2026-08-01T04:00:00.000Z',
            duration_min: 120,
            severity: 'critical',
            detection: 'zscore',
            severity_reason: 'CPU spike above baseline',
            z_score: 4.1,
          }],
        },
        stats: {
          'Percentage CPU': { mean: 57, p95: 82, max: 95, count: 6 },
          'Available Memory Percentage': { mean: 37, min: 22, p5: 24, count: 6 },
          'OS Disk Bandwidth Consumed Percentage': { mean: 14, p95: 21, max: 21, count: 6 },
        },
        baseline_confidence: {
          pulls: degraded ? 1 : 8,
          min_pulls: 5,
          mature_min_pulls: 15,
          degraded,
          retention_days: 5,
          baseline_mean: 62,
          baseline_std: 9,
        },
      },
    },
    heatmap: {
      timestamps: buildSeries([0, 1, 2, 3, 4, 5]).map((point) => point.t),
      grids: {
        cpu: [{ name: vmName, values: [24, 30, 45, 82, 95, 68] }],
        memory: [{ name: vmName, values: [48, 46, 42, 35, 22, 28] }],
        disk: [{ name: vmName, values: [8, 10, 14, 18, 21, 12] }],
      },
    },
    baseline: { days_observed: 5 },
    patterns: [],
    window: { timezone: 'UTC', start_utc: '2026-08-01T00:00:00.000Z', end_utc: '2026-08-01T05:00:00.000Z', grain: '1h' },
    summary: { vm_count: 1, hours_back: hoursBack, total_critical: 1, total_warning: 0, affected_vms: 1 },
  };
}

function buildResource(deepDive?: ReturnType<typeof buildDeepDive>) {
  return {
    servers: [{
      host: 'vm-critical.contoso.local',
      resource_id: 'vm-1',
      source: 'azure_monitor',
      type: 'APP',
      environment: 'PROD',
      cpu_used: 88,
      cpu_avg_pct: 57,
      cpu_max_pct: 95,
      mem_used: 62,
      mem_min_pct: 22,
      mem_max_pct: 78,
      disk_used_max: 21,
      disk_max_pct: 21,
      health_score: 71,
      status: 'Critical',
      cpu_available: true,
      mem_available: true,
      disk_available: true,
    }],
    kpis: {},
    anomalies: [],
    executive_summary: { verdict: 'NO DATA' },
    ...(deepDive ? { deep_dive: deepDive } : {}),
  };
}

function SeededResourcePanel({ resource, toggleable = false }: { resource: ReturnType<typeof buildResource>; toggleable?: boolean }) {
  const { setResource } = useAppData();
  const [visible, setVisible] = React.useState(true);

  React.useEffect(() => {
    setResource(resource as never);
  }, [resource, setResource]);

  return (
    <>
      {toggleable && <button onClick={() => setVisible((current) => !current)}>toggle panel</button>}
      {visible && <ResourcePanel />}
    </>
  );
}

describe('ResourcePanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    mockedGetAzureAuthStatus.mockResolvedValue({ method: 'browser' } as never);
    mockedGetAzureStatus.mockResolvedValue({ ok: true } as never);
    mockedGetSowState.mockResolvedValue({} as never);
    mockedProcessResource.mockResolvedValue({
      kpis: {},
      anomalies: [],
      executive_summary: { verdict: 'NO DATA' },
    } as never);
    mockedFetchAzureTimeseries.mockReset();
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  it('shows the empty state and an Azure connect control when no servers are loaded', async () => {
    render(
      <AppDataProvider>
        <ResourcePanel />
      </AppDataProvider>,
    );

    await waitFor(() => expect(mockedGetAzureAuthStatus).toHaveBeenCalled());
    expect(screen.getByText(/Upload a resource report/i)).toBeDefined();
    expect(screen.getByRole('button', { name: /Fetch from Azure Monitor/i })).toBeDefined();
  });

  it('restores persisted deep-dive charts and selection after a tab remount', async () => {
    render(
      <AppDataProvider>
        <SeededResourcePanel resource={buildResource(buildDeepDive())} toggleable />
      </AppDataProvider>,
    );

    await waitFor(() => expect(screen.getByText(/1 event on vm-critical/i)).toBeDefined());
    fireEvent.click(screen.getByRole('button', { name: /toggle panel/i }));
    fireEvent.click(screen.getByRole('button', { name: /toggle panel/i }));

    await waitFor(() => expect(screen.getByText(/1 event on vm-critical/i)).toBeDefined());
    expect(screen.getByText(/Unified Time-Series/i)).toBeDefined();
  });

  it('ignores stale deep-dive responses after a remount starts a newer request', async () => {
    const first = deferred<ReturnType<typeof buildDeepDive>>();
    const second = deferred<ReturnType<typeof buildDeepDive>>();
    mockedFetchAzureTimeseries
      .mockImplementationOnce(() => first.promise as never)
      .mockImplementationOnce(() => second.promise as never);

    render(
      <AppDataProvider>
        <SeededResourcePanel resource={buildResource()} toggleable />
      </AppDataProvider>,
    );

    await waitFor(() => expect(screen.getByRole('button', { name: /Load Time-Series/i })).toBeDefined());
    fireEvent.click(screen.getByRole('button', { name: /Load Time-Series/i }));
    expect(mockedFetchAzureTimeseries).toHaveBeenNthCalledWith(1, expect.objectContaining({ hours_back: 24 }));

    fireEvent.click(screen.getByRole('button', { name: /toggle panel/i }));
    fireEvent.click(screen.getByRole('button', { name: /toggle panel/i }));
    fireEvent.click(screen.getByText('48h'));
    fireEvent.click(screen.getByRole('button', { name: /Load Time-Series/i }));
    expect(mockedFetchAzureTimeseries).toHaveBeenNthCalledWith(2, expect.objectContaining({ hours_back: 48 }));

    await act(async () => {
      second.resolve(buildDeepDive({ vmName: 'fresh-vm', hoursBack: 48 }));
      await second.promise;
    });
    await waitFor(() => expect(screen.getByText(/1 event on fresh-vm/i)).toBeDefined());

    await act(async () => {
      first.resolve(buildDeepDive({ vmName: 'stale-vm', hoursBack: 24 }));
      await first.promise;
    });

    await waitFor(() => expect(screen.getByText(/1 event on fresh-vm/i)).toBeDefined());
    expect(screen.queryByText(/1 event on stale-vm/i)).toBeNull();
  });

  it('renders a low-confidence baseline caveat next to degraded severity labels', async () => {
    render(
      <AppDataProvider>
        <SeededResourcePanel resource={buildResource(buildDeepDive({ degraded: true }))} />
      </AppDataProvider>,
    );

    await waitFor(() => expect(screen.getByText(/1 event on vm-critical/i)).toBeDefined());
    expect(screen.getAllByText(/low-confidence baseline/i).length).toBeGreaterThanOrEqual(2);
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

  it('does not flag a real, uniformly-sampled hourly series as having a gap', () => {
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
    const points = [
      { t: '2026-08-19T16:50:00.000Z', v: 4.2e9 },
      { t: '2026-08-19T17:50:00.000Z', v: 4.1e9 },
      { t: '2026-08-19T18:50:00.000Z', v: 4.3e9 },
      { t: '2026-08-19T20:50:00.000Z', v: 3.9e9 },
      { t: '2026-08-19T21:50:00.000Z', v: 4.0e9 },
    ];
    const result = withGapBreaks(points);
    expect(result.sawGap).toBe(true);
    expect(result.data).toHaveLength(6);
    const nullEntries = result.data.filter(([, v]) => v === null);
    expect(nullEntries).toHaveLength(1);
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
