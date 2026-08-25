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
    window_dominant_ceiling_hrs: 6,
    batch_env: 'PROD',
    fleet_sla_buffer: { buffer_hrs: 1.2, buffer_pct: 18, status: 'AT_RISK', sla_source: 'sla_matrix' },
  },
  top_jobs: [
    { Job_Name: 'JOB_A', Sub_Application: 'FIN', peak_hrs: 5.4, avg_hrs: 4.1, total_hrs: 40, sla_hrs: 6, buffer_pct: 10, buffer_status: 'AT_RISK' },
    { Job_Name: 'PURGE_TMP', Sub_Application: 'UTIL', peak_hrs: 0.05, avg_hrs: 0.04, total_hrs: 1, buffer_status: 'OK', is_utility: true, utility_reason: 'purge_(0.051h<0.100h)' },
  ],
  top_breaches: [
    { Job_Name: 'JOB_A', Sub_Application: 'FIN', peak_hrs: 5.4, avg_hrs: 4.1, total_hrs: 40, sla_hrs: 6, buffer_pct: 10, buffer_status: 'AT_RISK', concurrent_job_count: 5, concurrent_overlap_hrs: 0.3 },
  ],
  window: [{
    run_date: '2026-08-01', total_hrs: 5.2, job_count: 12, breach: false, top_job: 'JOB_A',
    effective_hrs: 4.8, elapsed_hrs: 7.1, active_busy_hrs: 4.2, idle_gap_hrs: 2.9, idle_pct: 40.8,
    raw_job_count: 13, raw_run_count: 15, scope_run_count: 14, excluded_job_count: 1,
    raw_total_hrs: 5.3, excluded_hrs: 0.1, min_buffer_pct: 20,
    spike: { is_spike: true, z_score: 2.8, threshold_z: 2.0, trigger_reason: 'runtime_above_statistical_baseline' },
    batch_blocks: [{ start: '01:00', end: '05:48', span_hrs: 4.8, runs: 14 }],
  }],
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
  sla_heatmap: {
    jobs: ['JOB_A'], dates: ['2026-08-01'], limit: 6,
    cells: [{ job: 'JOB_A', date: '2026-08-01', hrs: 5.4, breach: false, sla_limit: 6 }],
    job_priority: { JOB_A: { priority: 'warning', score: 1, reason: 'Near SLA ceiling' } },
  },
  longpole_matrix: {
    has_data: true, jobs: ['JOB_A'], dates: ['2026-08-01'], max_minutes: 60, busy_ref_hrs: 2.4, share_pct_flag: 25,
    cells: [{ job: 'JOB_A', date: '2026-08-01', minutes: 57 }],
    rows: [{ job: 'JOB_A', avg_min: 57, max_min: 57, runs: 1, days_present: 1, days_total: 1, spike_ratio: 1, window_share_pct: 40, is_longpole: true, stability: 'consistent' }],
  },
  sla_source: { type: 'sla_matrix', daily_hrs: 6, adaptive_active: false, adaptive_job_count: 0, adaptive_total_jobs: 0, resolved_ceilings: [6, 8] },
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

  it('renders bordered evidence tables with semantic risk rows on real-shaped data', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { container, getByText } = render(
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
    expect(getByText(/Bar labels show executions/i)).toBeDefined();
    expect(getByText(/Verdict basis:/i)).toBeDefined();
    expect(getByText(/Elapsed span:/i)).toBeDefined();
    expect(getByText(/server baseline/i)).toBeDefined();
    expect(getByText(/2 contracted ceilings: 6–8h/i)).toBeDefined();
    expect(container.querySelector('[aria-label="Top breaching jobs table"]')).not.toBeNull();
    expect(container.querySelector('.pe-table-shell')).not.toBeNull();
    expect(container.querySelector('.pe-table-row-warning')).not.toBeNull();
    expect(container.querySelector('.batch-overlap-signal--critical')).not.toBeNull();
    expect(container.querySelector('.batch-sla-cell--watch')).not.toBeNull();
    expect(container.querySelector('.batch-longpole-cell--longest')).not.toBeNull();
    expect(container.querySelector('.batch-longpole-row--attention')).not.toBeNull();
  });
});
