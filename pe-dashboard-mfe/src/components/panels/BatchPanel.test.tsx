import React, { useEffect } from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppDataProvider, useAppData } from '../../context/AppDataContext';
import { BatchPanel } from './BatchPanel';

const RICH_BATCH_PAYLOAD = {
  filename: 'ctrlm_export.csv',
  kpis: {
    compliance_pct: 92.5,
    window_compliance_pct: 88.2,
    total_runs: 420,
    total_jobs: 60,
    jobs_breach: 3,
    jobs_at_risk: 5,
    jobs_ok: 52,
    failed_runs: 2,
    fail_rate_pct: 0.5,
    daily_limit_hrs: 6,
    batch_env: 'PROD',
    fleet_sla_buffer: { buffer_hrs: 1.2, buffer_pct: 18, status: 'AT_RISK', sla_source: 'sla_matrix' },
  },
  top_jobs: [
    { Job_Name: 'JOB_A', Sub_Application: 'FIN', peak_hrs: 5.4, avg_hrs: 4.1, total_hrs: 40, sla_hrs: 6, buffer_pct: 10, buffer_status: 'AT_RISK' },
    { Job_Name: 'PURGE_TMP', Sub_Application: 'UTIL', peak_hrs: 0.05, avg_hrs: 0.04, total_hrs: 1, buffer_status: 'OK', is_utility: true, utility_reason: 'purge_(0.051h<0.100h)' },
  ],
  top_breaches: [
    { Job_Name: 'JOB_A', Sub_Application: 'FIN', peak_hrs: 5.4, avg_hrs: 4.1, total_hrs: 40, sla_hrs: 6, buffer_pct: 10, buffer_status: 'AT_RISK' },
  ],
  window: [{ run_date: '2026-08-01', total_hrs: 5.2, job_count: 12, breach: false, top_job: 'JOB_A' }],
  data_coverage: {
    date_range: ['2026-08-01', '2026-08-15'],
    date_span_days: 15,
    confidence: 82,
    confidence_label: 'HIGH',
    warnings: [],
    excluded_jobs: [{ job_name: 'SHORT_JOB_1', reason: 'SHORT_JOB' }],
  },
  concurrency: {
    total_days_with_concurrency: 4,
    groups: [{
      example_sub_app: 'FIN', job_count: 3, distinct_jobs_total: 3, occurrences: 2, days_seen: 2,
      peak_concurrent: 5, avg_duration_min: 12.5, burst_tightness: 2.1, trend: [1, 2],
      severity_level: 'high',
      bursts: [{ run_date: '2026-08-01', start_clock: '01:00', end_clock: '01:12', duration_min: 12, peak_concurrent: 5, jobs: ['JOB_A', 'JOB_B'], job_count: 2 }],
    }],
  },
  hour_heatmap: {
    sub_apps: ['FIN'], hours: [1, 2], cells: [{ sub_app: 'FIN', hour: 1, count: 3, total_hrs: 4.2 }],
  },
  sla_source: { type: 'sla_matrix', daily_hrs: 6, adaptive_active: false, adaptive_job_count: 0, adaptive_total_jobs: 0 },
};

function BatchDataInjector({ children }: { children: React.ReactNode }) {
  const { setBatch } = useAppData();
  useEffect(() => { setBatch(RICH_BATCH_PAYLOAD as never); }, [setBatch]);
  return <>{children}</>;
}

describe('BatchPanel', () => {
  it('shows the empty state when no batch data has been uploaded', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText } = render(
      <MemoryRouter>
        <AppDataProvider>
          <BatchPanel />
        </AppDataProvider>
      </MemoryRouter>,
    );

    expect(getByText(/No Ctrl-M data loaded yet/i)).toBeDefined();
  });

  it('renders excluded jobs, concurrency evidence and coverage strip without crashing on real-shaped data', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText } = render(
      <MemoryRouter>
        <AppDataProvider>
          <BatchDataInjector>
            <BatchPanel />
          </BatchDataInjector>
        </AppDataProvider>
      </MemoryRouter>,
    );

    expect(getByText(/Excluded Jobs/i)).toBeDefined();
    expect(getByText(/Concurrent Jobs/i)).toBeDefined();
    expect(getByText(/15-Day Evidence/i)).toBeDefined();
  });
});
