import React, { useEffect } from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppDataProvider, useAppData } from '../../context/AppDataContext';
import { workbookSlaSnapshotFromUpload } from '../../api/dashboardApi';
import { SlaMatrixPanel } from './SlaMatrixPanel';

jest.mock('../../api/dashboardApi', () => {
  const actual = jest.requireActual('../../api/dashboardApi');
  return {
    ...actual,
    uploadBatchSlaXlsx: jest.fn(),
    refreshBatch: jest.fn(),
    generateFindings: jest.fn(),
    getRedFlags: jest.fn(),
    getExecutiveDashboard: jest.fn(),
    getPeNarrative: jest.fn(),
    getFinalJudgment: jest.fn(),
  };
});

const api = require('../../api/dashboardApi') as {
  uploadBatchSlaXlsx: jest.Mock;
  refreshBatch: jest.Mock;
  generateFindings: jest.Mock;
  getRedFlags: jest.Mock;
  getExecutiveDashboard: jest.Mock;
  getPeNarrative: jest.Mock;
  getFinalJudgment: jest.Mock;
};

const RICH_SLA_PAYLOAD = {
  compliance_pct: 91.2,
  window_day_compliance_pct: 88.4,
  total_runs: 300,
  total_jobs: 40,
  breaching_runs: 3,
  at_risk_runs: 4,
  ok_runs: 280,
  long_job_runs: 10,
  failed_runs: 3,
  worst_job: 'JOB_A',
  worst_hrs: 7.2,
  explicit_sla_matrix: false,
  sla_limit_hrs: 6,
  sla_label: 'Daily SLA (6.0h)',
  breaches: [
    { job_name: 'JOB_A', sub_application: 'FIN', run_date: '2026-08-01', run_hrs: 7.2, sla_limit_hrs: 6, breach_margin_hrs: 1.2, status: 'BREACH', sla_source: 'global' },
    { job_name: 'JOB_B', sub_application: 'OPS', run_date: '2026-08-02', run_hrs: 5.9, sla_limit_hrs: 6, breach_margin_hrs: -0.1, status: 'AT_RISK', sla_source: 'sla_matrix' },
  ],
  job_summary: [
    { job_name: 'JOB_A', runs: 12, peak_hrs: 7.2, avg_hrs: 5.1, sla_limit: 6, buffer_pct: -20, breach_runs: 2, breach_rate: 16.6, sla_source: 'global', sla_match_confidence: 'low' },
    { job_name: 'JOB_C', runs: 20, peak_hrs: 2.1, avg_hrs: 1.8, sla_limit: 6, buffer_pct: 65, breach_runs: 0, breach_rate: 0, sla_source: 'sla_matrix', sla_match_confidence: 'high' },
  ],
  job_baselines: {
    JOB_A: { runs: 12, avg_hrs: 5.1, std_hrs: 0.8, p95_hrs: 6.5, max_hrs: 7.2, expected_hrs: 6.6, sample_size_ok: true },
  },
  outliers: [
    { job_name: 'JOB_A', run_date: '2026-08-01', start_time: '01:00', end_time: '07:00', run_hrs: 6.0, expected_hrs: 4.5, expected_margin_hrs: 1.5, outlier_z: 3.4 },
  ],
  resource_linked: [
    { job_name: 'JOB_A', run_date: '2026-08-01', start_hour: 1, run_hrs: 7.2, resource_signal: { verdict: 'RESOURCE_LINK', fleet_cpu: 88, fleet_mem: 79, hot_hour_jobs: 4, critical_hosts: ['db01'] } },
    { job_name: 'JOB_B', run_date: '2026-08-02', start_hour: 2, run_hrs: 5.9, resource_signal: { verdict: 'ISOLATED', fleet_cpu: 30, fleet_mem: 40, hot_hour_jobs: 0, critical_hosts: [] } },
  ],
  workflow_summary: [
    {
      workflow_name: 'FIN_DAILY', batch_type: 'DAILY', runtime_h: 5.4, sla_h: 6,
      buffer_pct: 10, status: 'AT_RISK', sla_source: 'batch_sla_xlsx',
      clock_buffer_mins: 34, start_delay_mins: 48, start_time_status: 'LATE_START', contract_start_time: '01:00',
      workflow_start: '01:48', workflow_end: '07:12', elapsed_duration_h: 5.4,
      workbook_start_time: '01:48', workbook_reported_end: '07:12', workbook_timing_source: 'WORKBOOK_REPORTED_TIMESTAMP_PAIR',
    },
  ],
  batch_sla_mapping_report: {
    schema_version: '1',
    sheets: [{
      field_states: [
        { canonical_field: 'batch_name', state: 'mapped_populated', populated_rows: 1, empty_rows: 0 },
        { canonical_field: 'module', state: 'field_absent_in_source' },
        { canonical_field: 'comments', state: 'mapped_empty_for_all_rows' },
      ],
    }],
  },
};

function SlaDataInjector({ children, payload = RICH_SLA_PAYLOAD }: { children: React.ReactNode; payload?: Record<string, unknown> }) {
  const { setSlaMatrix } = useAppData();
  useEffect(() => { setSlaMatrix(payload as never); }, [payload, setSlaMatrix]);
  return <>{children}</>;
}

describe('SlaMatrixPanel', () => {
  beforeEach(() => {
    api.uploadBatchSlaXlsx.mockReset();
    api.refreshBatch.mockReset();
    api.generateFindings.mockReset();
    api.getRedFlags.mockReset();
    api.getExecutiveDashboard.mockReset();
    api.getPeNarrative.mockReset();
    api.getFinalJudgment.mockReset();
  });

  it('builds a workbook-only matrix when a stale local API returns the legacy upload response', () => {
    const snapshot = workbookSlaSnapshotFromUpload({
      filename: 'BatchSLA_info.xlsx',
      workflows: [{ workflow: 'DMD_DAILY', batch_type: 'DAILY', sla_source: 'BATCH_SLA_XLSX', sla_hours: 8, last_run_hours_xlsx: 6.5, compliance: 'LONG_JOB' }],
      mapping_report: { schema_version: '1', sheets: [] },
    });
    expect(snapshot.workbook_only).toBe(true);
    expect(snapshot.total_jobs).toBe(1);
    expect(snapshot.observed_workflow_count).toBe(1);
    expect((snapshot.workflow_summary as Array<{ runtime_h: number; duration_headroom_mins: number }>)[0].runtime_h).toBe(6.5);
    expect((snapshot.workflow_summary as Array<{ runtime_h: number; duration_headroom_mins: number }>)[0].duration_headroom_mins).toBe(90);
  });

  it('does not invent an SLA when workbook clock-window and Duration contract values conflict', () => {
    const snapshot = workbookSlaSnapshotFromUpload({
      filename: 'any_customer.xlsx',
      workflows: [{
        workflow: 'GENERIC_MONTHLY', batch_type: 'MONTHLY', sla_source: 'CONTRACT_CONFLICT',
        contract_conflict: true, workbook_clock_window_hours: 1, workbook_contract_duration_hours: 2,
        contract_conflict_detail: 'End Time implies 1.000h from Start Time, but Duration declares 2.000h.',
      }],
    });
    const row = (snapshot.workflow_summary as Array<{ status: string; sla_h: number | null; sla_source: string; measurement_reason_code: string }>)[0];
    expect(row.status).toBe('SLA_CONTRACT_CONFLICT');
    expect(row.sla_h).toBeNull();
    expect(row.sla_source).toBe('batch_sla_xlsx_conflict');
    expect(row.measurement_reason_code).toBe('CLOCK_DURATION_CONFLICT');
  });

  it('shows the upload control and empty state before any file is uploaded', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText, getByRole } = render(
      <MemoryRouter>
        <AppDataProvider>
          <SlaMatrixPanel />
        </AppDataProvider>
      </MemoryRouter>,
    );

    expect(getByRole('button', { name: /Upload SLA Matrix/i })).toBeDefined();
    expect(getByText(/Upload a BatchSLA workbook here/i)).toBeDefined();
  });

  it('renders breach crux, job summary, resource-link table and buffer chart without crashing on real-shaped data', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText, getAllByText } = render(
      <MemoryRouter>
        <AppDataProvider>
          <SlaDataInjector>
            <SlaMatrixPanel />
          </SlaDataInjector>
        </AppDataProvider>
      </MemoryRouter>,
    );

    expect(getByText(/is the worst offender/i)).toBeDefined();
    expect(getByText(/Job Summary/i)).toBeDefined();
    expect(getByText(/Resource-Linked Breaches/i)).toBeDefined();
    expect(getByText(/Duration headroom/i)).toBeDefined();
    expect(getByText(/Contract window \/ source timing/i)).toBeDefined();
    expect(getAllByText(/Measured duration/i).length).toBeGreaterThan(0);
    expect(getByText('01:48 → 07:12 · reported')).toBeDefined();
    expect(getByText('5.400h')).toBeDefined();
    expect(getByText('36m left')).toBeDefined();
    expect(getByText(/BatchSLA source mapping/i)).toBeDefined();
    expect(getByText(/Not present in source/i)).toBeDefined();
    expect(getByText(/empty in every source row/i)).toBeDefined();
    expect(getAllByText(/JOB_A/i).length).toBeGreaterThan(0);
  });

  it('scores a workbook-reported current end even when it equals the contract target', () => {
    const payload = {
      ...RICH_SLA_PAYLOAD,
      workbook_only: true,
      workflow_summary: [{
        workflow_name: 'USF_DAILY', batch_type: 'DAILY', sla_h: 8.917,
        runtime_h: 8.917, buffer_pct: 0, duration_headroom_mins: 0, status: 'NO_BUFFER',
        workbook_timing_source: 'WORKBOOK_REPORTED_CURRENT_END_EQUALS_TARGET',
        workbook_start_time: '21:05:00',
        workbook_reported_end: '06:00:00', runtime_source_caveat: 'REPORTED_END_EQUALS_TARGET',
        measurement_reason_code: 'REPORTED_END_EQUALS_TARGET',
        measurement_reason_detail: 'Current end equals expected end; verify the source completion value.',
      }],
    };
    const { getByText, getAllByText, queryByText } = render(
      <MemoryRouter><AppDataProvider><SlaDataInjector payload={payload}><SlaMatrixPanel /></SlaDataInjector></AppDataProvider></MemoryRouter>,
    );
    expect(getByText(/reported completion \(equals target — verify source\)/i)).toBeDefined();
    expect(getByText('8.917h')).toBeDefined();
    expect(getByText('0m left')).toBeDefined();
    expect(getAllByText(/Source-reported completion equals the contract target/i).length).toBeGreaterThan(0);
    expect(queryByText(/Not observed — target copied/i)).toBeNull();
  });

  it('recomputes Batch Review and all derived evidence after a direct SLA Matrix upload', async () => {
    api.uploadBatchSlaXlsx.mockResolvedValue({
      filename: 'BatchSLA_info.xlsx',
      workflow_count: 1,
      workflows: [{ workflow: 'FIN_DAILY', batch_type: 'DAILY', sla_source: 'BATCH_SLA_XLSX', sla_hours: 8 }],
      mapping_report: { schema_version: '2', sheets: [] },
    });
    api.refreshBatch.mockResolvedValue({ filename: 'ctrlm.csv', kpis: { total_runs: 12, total_jobs: 2 } });
    api.generateFindings.mockResolvedValue({ findings: [] });
    api.getRedFlags.mockResolvedValue({ questions: [] });
    api.getExecutiveDashboard.mockResolvedValue({});
    api.getPeNarrative.mockResolvedValue({});
    api.getFinalJudgment.mockResolvedValue({});

    const { container, getByText } = render(
      <MemoryRouter><AppDataProvider><SlaMatrixPanel /></AppDataProvider></MemoryRouter>,
    );
    const input = container.querySelector('#sla-matrix-input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(['contract'], 'BatchSLA_info.xlsx')] } });

    await waitFor(() => expect(api.refreshBatch).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.generateFindings).toHaveBeenCalledTimes(1));
    expect(api.getRedFlags).toHaveBeenCalledTimes(1);
    expect(api.getExecutiveDashboard).toHaveBeenCalledTimes(1);
    expect(api.getPeNarrative).toHaveBeenCalledTimes(1);
    expect(api.getFinalJudgment).toHaveBeenCalledTimes(1);
    expect(getByText(/Batch Review, PE Findings, executive dashboard, and final judgment refreshed/i)).toBeDefined();
  });

  it('keeps the SLA contract view when Ctrl-M evidence is not yet available', async () => {
    api.uploadBatchSlaXlsx.mockResolvedValue({
      filename: 'BatchSLA_info.xlsx',
      workflow_count: 1,
      workflows: [{ workflow: 'FIN_DAILY', batch_type: 'DAILY', sla_source: 'BATCH_SLA_XLSX', sla_hours: 8 }],
      mapping_report: { schema_version: '2', sheets: [] },
    });
    api.refreshBatch.mockRejectedValue(new Error('No Ctrl-M data loaded'));

    const { container, getByText } = render(
      <MemoryRouter><AppDataProvider><SlaMatrixPanel /></AppDataProvider></MemoryRouter>,
    );
    const input = container.querySelector('#sla-matrix-input') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(['contract'], 'BatchSLA_info.xlsx')] } });

    await waitFor(() => expect(api.refreshBatch).toHaveBeenCalledTimes(1));
    expect(getByText(/SLA contract loaded\. Upload Ctrl-M evidence when available/i)).toBeDefined();
    expect(getByText(/Workbook SLA Matrix loaded/i)).toBeDefined();
    expect(api.generateFindings).not.toHaveBeenCalled();
  });
});
