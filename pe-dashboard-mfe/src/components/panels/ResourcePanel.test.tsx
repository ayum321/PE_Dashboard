import React from 'react';
import { render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { buildFleetHeatmapView, groupCrossServerCorrelations, preferredFleetHeatmapMetric, ResourcePanel } from './ResourcePanel';

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
    expect(disk).toBeNull();
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
});
