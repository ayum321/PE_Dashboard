import React, { ChangeEvent, useMemo, useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
  makeStyles,
} from '@material-ui/core';
import Highcharts from '../../theme/highchartsSetup';
import HighchartsReact from 'highcharts-react-official';
import { uploadSlaMatrix } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { SectionBanner } from '../shared/SectionBanner';
import { KpiStatCard } from '../shared/KpiStatCard';

interface SlaBreach {
  job_name?: string;
  sub_application?: string;
  run_date?: string;
  start_time?: string;
  end_time?: string;
  run_hrs?: number;
  sla_limit_hrs?: number;
  breach_margin_hrs?: number;
  status?: string;
  sla_source?: string;
}

interface JobSummaryRow {
  job_name?: string;
  Job_Name?: string;
  buffer_pct?: number;
  peak_hrs?: number;
  avg_hrs?: number;
  runs?: number;
  sla_limit?: number;
  sla_limit_hrs?: number;
  breach_rate?: number;
  breach_runs?: number;
  sla_source?: string;
  sla_match_confidence?: string;
  sla_match_detail?: string;
  reason_code?: string;
}

interface ResourceSignal {
  verdict?: string;
  fleet_cpu?: number;
  fleet_mem?: number;
  hot_hour_jobs?: number;
  critical_hosts?: string[];
}

interface ResourceLinkedRun {
  job_name?: string;
  run_date?: string;
  start_hour?: number;
  run_hrs?: number;
  resource_signal?: ResourceSignal;
}

interface WorkflowSummaryRow {
  workflow_key?: string;
  workflow_name?: string;
  sub_application?: string;
  batch_type?: string;
  runtime_h?: number;
  sla_h?: number;
  sla_source?: string;
  buffer_pct?: number;
  status?: string;
}

interface JobBaseline {
  runs: number;
  avg_hrs: number;
  std_hrs: number;
  p95_hrs: number;
  max_hrs: number;
  expected_hrs: number;
  sample_size_ok: boolean;
}

interface Outlier {
  job_name: string;
  run_date: string;
  start_time: string;
  end_time: string;
  run_hrs: number;
  expected_hrs: number;
  expected_margin_hrs: number;
  outlier_z: number;
}

const LOW_BUFFER_THRESHOLD = 20;

const SRC_LABEL: Record<string, string> = {
  sla_matrix: 'SLA File', batch_sla_xlsx: 'XLSX T1', sow_extracted: 'SOW T2', assumed: 'Assumed', global: 'Global',
};
const SRC_COLOR: Record<string, string> = {
  sla_matrix: '#10d96e', batch_sla_xlsx: '#2dd4bf', sow_extracted: '#22d3ee', assumed: '#f59e0b', global: '#6b7db3',
};
const CONFIDENCE_LABEL: Record<string, string> = {
  high: 'Exact / high match', medium: 'Medium match — review mapping', low: 'Fallback / low match',
};
const DRILL_LABEL: Record<string, string> = { OK: 'OK', LONG_JOB: 'Long Job', AT_RISK: 'At Risk', BREACH: 'Breach', FAILED: 'Failed' };
const DRILL_COLOR: Record<string, string> = { OK: '#10d96e', LONG_JOB: '#3b82f6', AT_RISK: '#f59e0b', BREACH: '#f43f5e', FAILED: '#6b7db3' };
const WF_STATUS_COLOR: Record<string, string> = { OK: '#10d96e', LONG_JOB: '#2dd4bf', AT_RISK: '#f59e0b', BREACH: '#f43f5e', UNKNOWN: '#6b7db3' };

/** Tier bucket from a workflow row's sla_source, mirrors the real dashboard's
 * "Active SLA Commitments" Tier 1 (BatchSLA XLSX) / Tier 2 (SOW) / Tier 3 (default) split. */
function _workflowTier(src: string): 'Tier 1 \u2014 BatchSLA_info.xlsx Workflow Overrides' | 'Tier 2 \u2014 SOW Contract Batch Window Ceilings' | 'Tier 3 \u2014 Global Defaults' {
  if (src.startsWith('batch_sla_xlsx')) return 'Tier 1 \u2014 BatchSLA_info.xlsx Workflow Overrides';
  if (src === 'sow_extracted') return 'Tier 2 \u2014 SOW Contract Batch Window Ceilings';
  return 'Tier 3 \u2014 Global Defaults';
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  section: { marginTop: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2) },
  kpiRow: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2) },
  kpi: { padding: theme.spacing(1.5), minWidth: 120 },
  input: { display: 'none' },
  empty: { marginTop: theme.spacing(2) },
}));

export function SlaMatrixPanel() {
  const classes = useStyles();
  const { data, setSlaMatrix } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drillStatus, setDrillStatus] = useState<string | null>(null);
  const [breachExpanded, setBreachExpanded] = useState(false);
  const [resLinkExpanded, setResLinkExpanded] = useState(false);

  const handleUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await uploadSlaMatrix(file);
      setSlaMatrix(result);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'SLA matrix upload failed.');
    } finally {
      setBusy(false);
    }
  };

  const slaMatrix = data.slaMatrix || {};
  const breaches = (slaMatrix.breaches as SlaBreach[]) || [];
  const jobSummary = (slaMatrix.job_summary as JobSummaryRow[]) || [];
  const jobBaselines = (slaMatrix.job_baselines as Record<string, JobBaseline>) || {};
  const outliers = (slaMatrix.outliers as Outlier[]) || [];
  const resourceLinked = (slaMatrix.resource_linked as ResourceLinkedRun[]) || [];
  const workflowSummary = (slaMatrix.workflow_summary as WorkflowSummaryRow[]) || [];
  const compliancePct = Number(slaMatrix.compliance_pct) || 0;
  const windowDayPct = slaMatrix.window_day_compliance_pct != null ? Number(slaMatrix.window_day_compliance_pct) : compliancePct;
  const explicitSlaMatrix = slaMatrix.explicit_sla_matrix === true;

  // ── Tightest Buffer — the single job closest to its SLA ceiling, ported from
  // #slak-tightbuf (_renderSlaMatrix(), app.js). ──
  const tightestBuffer = useMemo(() => {
    const scored = jobSummary.filter((j) => j.buffer_pct != null && Number.isFinite(j.buffer_pct));
    if (!scored.length) return null;
    return scored.reduce((a, b) => (Number(b.buffer_pct) < Number(a.buffer_pct) ? b : a));
  }, [data.slaMatrix]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Active SLA Commitments — Tier 1/2/3 workflow-level table, ported from
  // _renderSlaCommitmentsPanel() (app.js). Sourced from the same workflow_summary
  // field the real dashboard writes to session_cache["resolved_workflow_df"]. ──
  const workflowByTier = useMemo(() => {
    const groups = new Map<string, WorkflowSummaryRow[]>();
    workflowSummary.forEach((wf) => {
      const tier = _workflowTier(wf.sla_source || '');
      if (!groups.has(tier)) groups.set(tier, []);
      groups.get(tier)!.push(wf);
    });
    return groups;
  }, [data.slaMatrix]); // eslint-disable-line react-hooks/exhaustive-deps

  const lowBufferJobs = useMemo(
    () =>
      jobSummary
        .filter((job) => Number(job.buffer_pct ?? 999) < LOW_BUFFER_THRESHOLD)
        .sort((a, b) => Number(a.buffer_pct ?? 0) - Number(b.buffer_pct ?? 0)),
    [data.slaMatrix], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const unexplainedBreaches = useMemo(() => {
    const seen = new Set<string>();
    return breaches
      .filter((row) => row.status === 'BREACH')
      .filter((row) => {
        const key = (row.job_name || '').toLowerCase();
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }, [data.slaMatrix]); // eslint-disable-line react-hooks/exhaustive-deps

  const baselineEntries = useMemo(
    () => Object.entries(jobBaselines).sort((a, b) => (b[1].expected_hrs || 0) - (a[1].expected_hrs || 0)).slice(0, 10),
    [data.slaMatrix], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const criticalOutliers = useMemo(
    () =>
      [...outliers]
        .sort((a, b) => (b.outlier_z || 0) - (a.outlier_z || 0))
        .filter((row) => {
          const marginPct = row.expected_hrs > 0 ? (row.expected_margin_hrs / row.expected_hrs) * 100 : 0;
          return row.outlier_z >= 3 || marginPct >= 25;
        })
        .slice(0, 10),
    [data.slaMatrix], // eslint-disable-line react-hooks/exhaustive-deps
  );

  // ── Breach "crux" — worst-offender headline + per-job pills, ported from
  // _renderSlaBreachCrux() (app.js). ──
  const breachCrux = useMemo(() => {
    const byJob = new Map<string, { job: string; sub_app: string; breach: number; atrisk: number; peak: number; peak_margin: number; peak_date: string }>();
    for (const r of breaches) {
      const key = r.job_name || '—';
      const cur = byJob.get(key) || { job: key, sub_app: r.sub_application || '—', breach: 0, atrisk: 0, peak: 0, peak_margin: 0, peak_date: '' };
      if (r.status === 'BREACH') cur.breach += 1;
      else if (r.status === 'AT_RISK') cur.atrisk += 1;
      const hrs = Number(r.run_hrs) || 0;
      if (hrs > cur.peak) { cur.peak = hrs; cur.peak_margin = Number(r.breach_margin_hrs) || 0; cur.peak_date = r.run_date || ''; }
      byJob.set(key, cur);
    }
    const jobs = Array.from(byJob.values()).sort((a, b) => b.breach - a.breach || b.peak_margin - a.peak_margin);
    return { jobs: jobs.slice(0, 5), totalJobs: jobs.length };
  }, [data.slaMatrix]); // eslint-disable-line react-hooks/exhaustive-deps

  const sortedBreachRows = useMemo(() => {
    return [...breaches].sort((a, b) => {
      const sa = a.status === 'BREACH' ? 0 : 1;
      const sb = b.status === 'BREACH' ? 0 : 1;
      if (sa !== sb) return sa - sb;
      return (Number(b.breach_margin_hrs) || 0) - (Number(a.breach_margin_hrs) || 0);
    });
  }, [data.slaMatrix]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Multi-ceiling display — resolved ceiling RANGE when the matrix binds
  // more than one distinct ceiling, ported from _renderSlaMatrix() (app.js). ──
  const ceilingSet = useMemo(() => {
    const values = Array.from(new Set(
      jobSummary
        .map((j) => j.sla_limit)
        .filter((v): v is number => v != null && Number.isFinite(v) && v > 0)
        .map((v) => Math.round(v * 100) / 100),
    )).sort((a, b) => a - b);
    return values;
  }, [data.slaMatrix]); // eslint-disable-line react-hooks/exhaustive-deps

  const drillRows = useMemo(() => {
    if (!drillStatus) return [];
    const st = drillStatus.toUpperCase();
    if (['BREACH', 'AT_RISK', 'LONG_JOB'].includes(st)) {
      return breaches.filter((r) => (r.status || '').toUpperCase() === st);
    }
    return jobSummary.filter((r) => {
      if (st === 'FAILED') return false;
      if (st === 'OK') return r.buffer_pct != null && r.buffer_pct > 40;
      return false;
    });
  }, [drillStatus, data.slaMatrix]); // eslint-disable-line react-hooks/exhaustive-deps

  const eligibleRuns = (Number(slaMatrix.ok_runs) || 0) + (Number(slaMatrix.long_job_runs) || 0)
    + (Number(slaMatrix.at_risk_runs) || 0) + (Number(slaMatrix.breaching_runs) || 0);
  const totalRunsWithFailed = eligibleRuns + (Number(slaMatrix.failed_runs) || 0);
  const breakdownRows = [
    { label: 'OK', n: Number(slaMatrix.ok_runs) || 0, color: '#10d96e', status: 'OK' },
    { label: 'Long Job', n: Number(slaMatrix.long_job_runs) || 0, color: '#3b82f6', status: 'LONG_JOB' },
    { label: 'At Risk', n: Number(slaMatrix.at_risk_runs) || 0, color: '#f59e0b', status: 'AT_RISK' },
    { label: 'Breach', n: Number(slaMatrix.breaching_runs) || 0, color: '#f43f5e', status: 'BREACH' },
    { label: 'Failed', n: Number(slaMatrix.failed_runs) || 0, color: '#6b7db3', status: 'FAILED' },
  ];

  const sortedJobBuffers = useMemo(
    () => [...jobSummary].sort((a, b) => (Number(b.peak_hrs) || 0) - (Number(a.peak_hrs) || 0)).slice(0, 12),
    [data.slaMatrix], // eslint-disable-line react-hooks/exhaustive-deps
  );

  const bufferBarOptions: Highcharts.Options = {
    chart: { type: 'bar', height: Math.max(220, sortedJobBuffers.length * 28) },
    title: { text: undefined },
    xAxis: { categories: sortedJobBuffers.map((j) => (j.job_name || '?').length > 22 ? `${(j.job_name || '').slice(0, 20)}…` : (j.job_name || '?')) },
    yAxis: { title: { text: 'Buffer % (positive = within SLA)' } },
    legend: { enabled: false },
    tooltip: {
      formatter(this: Highcharts.TooltipFormatterContextObject) {
        const j = sortedJobBuffers[this.point.index];
        if (!j) return '';
        return `<b>${j.job_name}</b><br/>Buffer: ${j.buffer_pct != null ? j.buffer_pct.toFixed(2) + '%' : '—'}<br/>Peak: ${(j.peak_hrs || 0).toFixed(2)}h  SLA: ${(j.sla_limit || 0).toFixed(2)}h<br/>Source: ${j.sla_source || 'global'}`;
      },
    },
    series: [{
      type: 'bar',
      name: 'SLA Buffer %',
      data: sortedJobBuffers.map((j) => {
        const b = j.buffer_pct == null ? 0 : j.buffer_pct;
        const color = j.buffer_pct == null ? '#6b7db3' : j.buffer_pct < 0 ? '#f43f5e' : j.buffer_pct < 15 ? '#f59e0b' : j.buffer_pct < 40 ? '#3b82f6' : '#10d96e';
        return { y: b, color };
      }),
    }],
  };

  return (
    <Box style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <SectionBanner
        eyebrow="Contract Conformance & Drift"
        title="Is every job measured against the right contract — and which jobs are drifting toward a breach?"
        description="Batch Review answers whether the window was met. This tab answers where each SLA ceiling comes from and which jobs are quietly creeping toward their own limits."
        headline={data.slaMatrix ? `${windowDayPct.toFixed(1)}%` : '—'}
        headlineLabel="Window SLA · day-level"
        accent="#a855f7"
      />
      <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">SLA Matrix</Typography>
      <Box className={classes.row}>
        <input className={classes.input} id="sla-matrix-input" type="file" accept=".csv,.xlsx,.xls" onChange={handleUpload} />
        <label htmlFor="sla-matrix-input">
          <Button component="span" variant="contained" color="primary" disabled={busy}>
            Upload SLA Matrix
          </Button>
        </label>
        {busy && <CircularProgress size={22} aria-label="Uploading" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      {!data.slaMatrix ? (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload a Ctrl-M file here, or an SLA matrix file in Upload &amp; Intake, to populate this view.
        </Typography>
      ) : (
        <>
          {/* ── SLA Matrix intake/result card, ported from the upload confirmation card (app.js) ── */}
          <Box style={{ borderRadius: 12, border: '1px solid rgba(16,217,110,.3)', background: 'rgba(16,217,110,.05)', padding: 12, marginTop: 12 }}>
            <Typography variant="body2" style={{ color: '#10d96e', fontWeight: 700 }}>
              SLA Matrix loaded — {Number(slaMatrix.total_jobs) || 0} jobs · {compliancePct.toFixed(1)}% compliance
              · {Number(slaMatrix.breaching_runs) || 0} breach(es)
            </Typography>
            <Typography variant="caption" color="textSecondary">
              {explicitSlaMatrix ? 'Per-job contract rows matched from an uploaded SLA file.' : 'No per-job contract file matched — using schedule-type / global ceilings.'}
            </Typography>
          </Box>

          {/* ── Assumed-SLA warning banner, ported from #sla-assumed-banner (app.js) ── */}
          {!explicitSlaMatrix && (
            <Box style={{ borderRadius: 12, border: '1px solid rgba(245,158,11,.35)', background: 'rgba(245,158,11,.06)', padding: 12, marginTop: 12 }}>
              <Typography variant="body2" style={{ color: '#f59e0b', fontWeight: 700 }}>⚠ Assumed SLA ceiling in use</Typography>
              <Typography variant="caption" color="textSecondary">
                Unmatched jobs use the assumed {Number(slaMatrix.sla_limit_hrs || 6).toFixed(2)}h global ceiling ({String(slaMatrix.sla_label || 'global mode')}).
                Upload BatchSLA_info.xlsx (Tier 1) or SOW PDF (Tier 2) to activate contracted SLA ceilings.
              </Typography>
            </Box>
          )}

          {/* ── Active SLA Commitments — Tier 1/2/3 workflow table, ported from
              _renderSlaCommitmentsPanel() (app.js). ── */}
          {workflowSummary.length > 0 && (
            <Box style={{ borderRadius: 12, border: '1px solid #213060', background: 'rgba(17,29,54,.5)', padding: 12, marginTop: 12 }}>
              <Typography variant="subtitle2">Active SLA Commitments</Typography>
              <Typography variant="caption" color="textSecondary">Tier 1 (BatchSLA workflows) {'\u00b7'} Tier 2 (SOW contract ceilings) {'\u00b7'} Tier 3 (global defaults) {'\u2014'} live resolution order</Typography>
              {Array.from(workflowByTier.entries()).map(([tier, rows]) => (
                <Box key={tier} style={{ marginTop: 12 }}>
                  <Typography variant="caption" style={{ fontWeight: 700, color: '#a855f7' }}>{tier}</Typography>
                  <Table size="small" className="pe-table" aria-label={`SLA commitments ${tier}`} style={{ marginTop: 4 }}>
                    <TableHead>
                      <TableRow>
                        <TableCell>Workflow</TableCell>
                        <TableCell>Type</TableCell>
                        <TableCell align="right">SLA</TableCell>
                        <TableCell align="right">Peak Window</TableCell>
                        <TableCell align="right">Buffer %</TableCell>
                        <TableCell>Status</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {rows.slice(0, 20).map((wf, index) => (
                        <TableRow key={`${wf.workflow_name || 'wf'}-${index}`}>
                          <TableCell style={{ fontFamily: 'monospace' }}>{wf.workflow_name || wf.workflow_key || '?'}</TableCell>
                          <TableCell>{wf.batch_type || '\u2014'}</TableCell>
                          <TableCell align="right">{wf.sla_h != null ? `${Number(wf.sla_h).toFixed(1)}h` : '\u2014'}</TableCell>
                          <TableCell align="right">{wf.runtime_h != null ? `${Number(wf.runtime_h).toFixed(3)}h` : '\u2014'}</TableCell>
                          <TableCell align="right" style={{ color: wf.buffer_pct != null ? (wf.buffer_pct < 0 ? '#f43f5e' : wf.buffer_pct < 15 ? '#f59e0b' : '#10d96e') : '#6b7db3' }}>
                            {wf.buffer_pct != null ? `${Number(wf.buffer_pct).toFixed(1)}%` : '\u2014'}
                          </TableCell>
                          <TableCell>
                            <span className="metric-badge" style={{ color: WF_STATUS_COLOR[wf.status || 'UNKNOWN'] || '#6b7db3' }}>{wf.status || 'UNKNOWN'}</span>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </Box>
              ))}
              <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 8, fontSize: 9 }}>
                OK {'>'}40% {'\u00b7'} LONG_JOB 15{'\u2013'}40% {'\u00b7'} AT_RISK 0{'\u2013'}15% {'\u00b7'} BREACH {'<'}0% {'\u00b7'} Buffer=(SLA-rt)÷SLA×100
              </Typography>
            </Box>
          )}

          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 12, marginTop: 16 }}>
            <KpiStatCard
              label="Compliance"
              value={`${compliancePct.toFixed(1)}%`}
              sub="Runs within SLA ceiling"
              accent="#10d96e"
            />
            <KpiStatCard label="Total Runs" value={Number(slaMatrix.total_runs) || 0} sub={`${Number(slaMatrix.total_jobs) || 0} unique jobs`} accent="#3b82f6" />
            <KpiStatCard
              label="Drifting Jobs"
              value={outliers.length}
              sub="vs own baseline"
              accent={outliers.length > 0 ? '#f59e0b' : '#10d96e'}
              valueColor={outliers.length > 0 ? '#f59e0b' : '#10d96e'}
            />
            <KpiStatCard
              label="Tightest Buffer"
              value={tightestBuffer ? `${Number(tightestBuffer.buffer_pct).toFixed(1)}%` : '\u2014'}
              sub={tightestBuffer ? (tightestBuffer.job_name || tightestBuffer.Job_Name || 'job-level headroom') : 'job-level headroom'}
              accent={tightestBuffer ? (Number(tightestBuffer.buffer_pct) >= 40 ? '#10d96e' : Number(tightestBuffer.buffer_pct) >= 15 ? '#f59e0b' : '#f43f5e') : '#6b7db3'}
            />
            <KpiStatCard label="Breaching" value={Number(slaMatrix.breaching_runs) || 0} sub="Over SLA ceiling" accent="#f43f5e" />
            <KpiStatCard label="At Risk" value={Number(slaMatrix.at_risk_runs) || 0} sub="Near SLA ceiling" accent="#f59e0b" />
            <KpiStatCard label="Long Jobs" value={Number(slaMatrix.long_job_runs) || 0} sub={'15\u201340% of SLA ceiling'} accent="#2dd4bf" />
            <KpiStatCard label="Failed Runs" value={Number(slaMatrix.failed_runs) || 0} sub="Execution failures" accent="#fb923c" />
            {slaMatrix.worst_job != null && (
              <KpiStatCard label="Worst Job" value={`${Number(slaMatrix.worst_hrs || 0).toFixed(2)}h`} sub={String(slaMatrix.worst_job)} accent="#a855f7" />
            )}
            {/* ── Multi-ceiling range display, ported from #slak-limit (_renderSlaMatrix, app.js) ── */}
            <KpiStatCard
              label="SLA Limit"
              value={ceilingSet.length > 1
                ? `${ceilingSet[0].toFixed(1)}\u2013${ceilingSet[ceilingSet.length - 1].toFixed(1)}h`
                : ceilingSet.length === 1
                  ? `${ceilingSet[0].toFixed(2)}h`
                  : `${Number(slaMatrix.sla_limit_hrs || 6).toFixed(2)}h`}
              sub={ceilingSet.length > 1 ? `${ceilingSet.length} distinct resolved ceilings` : explicitSlaMatrix ? 'SLA file + mode fallback' : 'Assumed \u2014 no SLA file'}
              valueColor={explicitSlaMatrix ? '#10d96e' : '#f59e0b'}
              accent={explicitSlaMatrix ? '#10d96e' : '#f59e0b'}
            />
          </Box>
          <Typography variant="caption" style={{ display: 'block', marginTop: 8, color: '#6b7db3' }}
            title={'Window compliance (headline) = calendar days ALL sub-apps finished within SLA \u00f7 total days. Pair detail = (sub-app \u00d7 day) windows within SLA. Job-run compliance is a separate metric: (OK+LONG_JOB+AT_RISK) \u00f7 eligible runs.'}>
            <span style={{ color: '#3b82f6' }}>{'\u2139'}</span>{' '}
            {'Day-level window compliance (headline) vs pair-level (sub-app \u00d7 day) vs job-run compliance can diverge \u2014 hover for the exact formulas.'}
          </Typography>

          {/* ── Compliance breakdown bars — clickable drill-through, ported from _renderSlaCharts()/_slaBreakdownDrill() (app.js) ── */}
          <Box className={classes.section}>
            <Typography variant="subtitle2">Compliance Breakdown</Typography>
            <Typography variant="caption" color="textSecondary">
              {totalRunsWithFailed} total runs {'\u00b7'} {Number(slaMatrix.failed_runs) || 0} failed {'\u2014'} click a row to see the underlying jobs.
            </Typography>
            <Box style={{ marginTop: 8 }}>
              {breakdownRows.map((row) => {
                const pct = eligibleRuns > 0 && row.status !== 'FAILED' ? (row.n / eligibleRuns) * 100 : (totalRunsWithFailed > 0 ? (row.n / totalRunsWithFailed) * 100 : 0);
                const barPct = totalRunsWithFailed > 0 ? Math.max(row.n > 0 ? 2 : 0, (row.n / totalRunsWithFailed) * 100) : 0;
                return (
                  <Box
                    key={row.status}
                    display="flex" alignItems="center" style={{ gap: 10, cursor: 'pointer', padding: '4px 4px', borderRadius: 8 }}
                    onClick={() => setDrillStatus(drillStatus === row.status ? null : row.status)}
                    title={`Click to see ${row.label} jobs`}
                  >
                    <span style={{ width: 10, height: 10, borderRadius: '50%', background: row.color, flexShrink: 0 }} />
                    <span style={{ width: 80, fontSize: 11, fontWeight: 700, color: row.color }}>{row.label}</span>
                    <Box style={{ flex: 1, height: 10, borderRadius: 6, background: 'rgba(255,255,255,.06)', overflow: 'hidden' }}>
                      <Box style={{ width: `${barPct}%`, height: '100%', background: row.color, minWidth: row.n > 0 ? 2 : 0 }} />
                    </Box>
                    <span style={{ width: 40, textAlign: 'right', fontSize: 10, fontWeight: 700, color: row.color }}>{row.n}</span>
                    <span style={{ width: 48, textAlign: 'right', fontSize: 10, color: '#6b7db3' }}>{pct.toFixed(1)}%</span>
                  </Box>
                );
              })}
              <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 8, fontSize: 9 }}>
                {'buffer% = (SLA_h − runtime_h) / SLA_h × 100 · OK >40% · LongJob 15–40% · AtRisk 0–15% · Breach ≤0% · Failed = execution error (excluded from %)'}
              </Typography>
            </Box>
          </Box>

          {/* ── Drill-through result table, ported from _slaBreakdownDrill() (app.js) ── */}
          {drillStatus && (
            <Box className={classes.section}>
              <Typography variant="subtitle2" style={{ color: DRILL_COLOR[drillStatus] || '#6b7db3' }}>
                {DRILL_LABEL[drillStatus] || drillStatus} {'\u2014'} {drillRows.length} job(s)
              </Typography>
              <Table size="small" className="pe-table" aria-label="SLA drill-through table" style={{ marginTop: 8 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>Job</TableCell>
                    <TableCell align="right">Runtime</TableCell>
                    <TableCell align="right">SLA</TableCell>
                    <TableCell align="right">Buffer</TableCell>
                    <TableCell>Date</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {drillRows.length === 0 ? (
                    <TableRow><TableCell colSpan={5} align="center">No {DRILL_LABEL[drillStatus] || drillStatus} jobs in this run.</TableCell></TableRow>
                  ) : drillRows.slice(0, 100).map((row, index) => {
                    const r = row as SlaBreach & JobSummaryRow;
                    const runHrs = r.run_hrs != null ? `${Number(r.run_hrs).toFixed(3)}h` : r.peak_hrs != null ? `${Number(r.peak_hrs).toFixed(3)}h (peak)` : '\u2014';
                    const sla = r.sla_limit_hrs != null ? Number(r.sla_limit_hrs).toFixed(2) : r.sla_limit != null ? Number(r.sla_limit).toFixed(2) : '\u2014';
                    const buf = r.buffer_pct != null ? `${Number(r.buffer_pct).toFixed(2)}%` : '\u2014';
                    return (
                      <TableRow key={`${r.job_name || 'job'}-${index}`}>
                        <TableCell style={{ fontFamily: 'monospace' }}>{r.job_name || '?'}</TableCell>
                        <TableCell align="right">{runHrs}</TableCell>
                        <TableCell align="right">{sla}{typeof sla === 'string' && sla !== '\u2014' ? 'h' : ''}</TableCell>
                        <TableCell align="right" style={{ color: DRILL_COLOR[drillStatus] || '#6b7db3' }}>{buf}</TableCell>
                        <TableCell>{r.run_date || '\u2014'}</TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </Box>
          )}

          {breaches.length > 0 && (() => {
            const worst = breachCrux.jobs[0];
            const PREVIEW = 5;
            const rowsToShow = breachExpanded ? sortedBreachRows : sortedBreachRows.slice(0, PREVIEW);
            const hiddenCount = sortedBreachRows.length - PREVIEW;
            return (
              <Box className={classes.section}>
                <Typography variant="subtitle2">SLA Breach Detail</Typography>
                {worst && (
                  <Box style={{ borderRadius: 8, background: 'rgba(244,63,94,.05)', border: '1px solid rgba(244,63,94,.2)', padding: 10, marginTop: 8 }}>
                    <Typography variant="body2" style={{ marginBottom: 6 }}>
                      <span style={{ color: '#f43f5e' }}>{'\u25b2'}</span>{' '}
                      <strong>{worst.job}</strong> is the worst offender {'\u2014'} {worst.breach} breach{worst.breach !== 1 ? 'es' : ''}, peak
                      {' '}<strong style={{ color: '#f43f5e' }}>{worst.peak.toFixed(2)}h</strong> (+{worst.peak_margin.toFixed(2)}h over SLA) on {worst.peak_date}.
                    </Typography>
                    <Box display="flex" style={{ gap: 8, flexWrap: 'wrap' }}>
                      {breachCrux.jobs.map((j) => (
                        <Box key={j.job} display="flex" alignItems="center" style={{ gap: 6, padding: '4px 8px', borderRadius: 6, border: '1px solid #213060', background: 'rgba(17,29,54,.6)' }}>
                          <span style={{ fontFamily: 'monospace' }}>{j.job}</span>
                          <span style={{ color: '#f43f5e', fontWeight: 700, fontSize: 11 }}>{j.breach} breach</span>
                          {j.atrisk > 0 && <span style={{ color: '#f59e0b', fontSize: 11 }}>{j.atrisk} risk</span>}
                          <span style={{ color: '#6b7db3', fontSize: 11 }}>peak {j.peak.toFixed(2)}h</span>
                        </Box>
                      ))}
                    </Box>
                  </Box>
                )}
                <Table size="small" className="pe-table" aria-label="SLA breach table" style={{ marginTop: 12 }}>
                  <TableHead>
                    <TableRow>
                      <TableCell>Job</TableCell>
                      <TableCell>Sub-app</TableCell>
                      <TableCell>Date</TableCell>
                      <TableCell align="right">Runtime hours</TableCell>
                      <TableCell align="right">SLA hours</TableCell>
                      <TableCell align="right">Over SLA hours</TableCell>
                      <TableCell>Status</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {rowsToShow.map((breach, index) => (
                      <TableRow key={`${breach.job_name || 'job'}-${index}`}>
                        <TableCell style={{ fontFamily: 'monospace' }}>{breach.job_name || 'Unnamed job'}</TableCell>
                        <TableCell>{breach.sub_application || '\u2014'}</TableCell>
                        <TableCell>{breach.run_date || '\u2014'}</TableCell>
                        <TableCell align="right">{(breach.run_hrs || 0).toFixed(2)}</TableCell>
                        <TableCell align="right">{(breach.sla_limit_hrs || 0).toFixed(2)}</TableCell>
                        <TableCell align="right" style={{ color: (breach.breach_margin_hrs || 0) > 0 ? '#f43f5e' : '#f59e0b' }}>
                          {(breach.breach_margin_hrs || 0) > 0 ? '+' : ''}{(breach.breach_margin_hrs || 0).toFixed(2)}
                        </TableCell>
                        <TableCell>
                          <span className="metric-badge" style={{ color: breach.status === 'BREACH' ? '#f43f5e' : '#f59e0b' }}>{breach.status || 'BREACH'}</span>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {sortedBreachRows.length > PREVIEW && (
                  <Button size="small" onClick={() => setBreachExpanded((v) => !v)} style={{ marginTop: 6 }}>
                    {breachExpanded ? `Show top ${PREVIEW} only \u25b4` : `Show all ${sortedBreachRows.length} breach/at-risk rows (+${hiddenCount}) \u25be`}
                  </Button>
                )}
              </Box>
            );
          })()}

          {/* ── Job Summary (All Jobs) — SLA source + match confidence, ported from
              _renderSlaMatrix()'s job_summary render block (app.js). ── */}
          {jobSummary.length > 0 && (
            <Box className={classes.section}>
              <Typography variant="subtitle2">{'Job Summary \u2014 All Jobs'}</Typography>
              <Typography variant="caption" color="textSecondary">Every scored job with its resolved SLA source and match confidence.</Typography>
              <Table size="small" className="pe-table" aria-label="Job summary all jobs table" style={{ marginTop: 8 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>Job</TableCell>
                    <TableCell align="right">Runs</TableCell>
                    <TableCell align="right">Peak hrs</TableCell>
                    <TableCell align="right">Avg hrs</TableCell>
                    <TableCell align="right">SLA limit</TableCell>
                    <TableCell align="right">Buffer</TableCell>
                    <TableCell align="right">Breach runs</TableCell>
                    <TableCell align="right">Breach rate</TableCell>
                    <TableCell align="right">SLA source</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {[...jobSummary].sort((a, b) => (Number(b.peak_hrs) || 0) - (Number(a.peak_hrs) || 0)).slice(0, 15).map((row, index) => {
                    const src = row.sla_source || 'global';
                    const confidence = String(row.sla_match_confidence || '').toLowerCase();
                    return (
                      <TableRow key={`${row.job_name || 'job'}-${index}`}>
                        <TableCell style={{ fontFamily: 'monospace' }}>{row.job_name || row.Job_Name || '?'}</TableCell>
                        <TableCell align="right">{row.runs ?? '\u2014'}</TableCell>
                        <TableCell align="right" style={{ color: '#f59e0b' }}>{Number(row.peak_hrs ?? 0).toFixed(2)}</TableCell>
                        <TableCell align="right">{Number(row.avg_hrs ?? 0).toFixed(2)}</TableCell>
                        <TableCell align="right">{Number(row.sla_limit ?? row.sla_limit_hrs ?? 0).toFixed(2)}</TableCell>
                        <TableCell align="right" style={{ color: row.buffer_pct == null ? '#6b7db3' : row.buffer_pct >= 30 ? '#10d96e' : row.buffer_pct >= 0 ? '#f59e0b' : '#f43f5e' }}>
                          {row.buffer_pct != null ? `${Number(row.buffer_pct).toFixed(1)}%` : (row.reason_code || '\u2014')}
                        </TableCell>
                        <TableCell align="right" style={{ color: (row.breach_runs || 0) > 0 ? '#f43f5e' : '#10d96e' }}>{row.breach_runs ?? 0}</TableCell>
                        <TableCell align="right" style={{ color: (row.breach_rate || 0) > 20 ? '#f43f5e' : (row.breach_rate || 0) > 0 ? '#f59e0b' : '#10d96e' }}>
                          {Number(row.breach_rate ?? 0).toFixed(1)}%
                        </TableCell>
                        <TableCell align="right" title={[SRC_LABEL[src] || src, CONFIDENCE_LABEL[confidence], row.sla_match_detail].filter(Boolean).join(' \u00b7 ')}>
                          <span style={{ color: SRC_COLOR[src] || '#6b7db3', fontWeight: 700, fontSize: 10.5 }}>{SRC_LABEL[src] || src}</span>
                          {confidence && <span style={{ marginLeft: 4, fontSize: 9.5, color: confidence === 'high' ? '#10d96e' : confidence === 'medium' ? '#f59e0b' : '#6b7db3' }}>({CONFIDENCE_LABEL[confidence]})</span>}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </Box>
          )}

          {/* ── Resource-Link table, ported from _renderSlaResourceLink() (app.js) ── */}
          {resourceLinked.length > 0 && (() => {
            const verdicts = new Set(resourceLinked.map((r) => r.resource_signal?.verdict || '\u2014'));
            const order: Record<string, number> = { RESOURCE_LINK: 0, TIMING_PRESSURE: 1, ISOLATED: 2 };
            const sorted = [...resourceLinked].sort((a, b) => {
              const va = order[a.resource_signal?.verdict || ''] ?? 99;
              const vb = order[b.resource_signal?.verdict || ''] ?? 99;
              if (va !== vb) return va - vb;
              return (b.run_hrs || 0) - (a.run_hrs || 0);
            });
            const MAX_ROWS = 15;
            const rows = resLinkExpanded ? sorted : sorted.slice(0, MAX_ROWS);
            if (verdicts.size === 1) {
              const v = Array.from(verdicts)[0];
              const tone = v === 'RESOURCE_LINK' ? '#f43f5e' : v === 'TIMING_PRESSURE' ? '#f59e0b' : '#10d96e';
              const msg = v === 'ISOLATED'
                ? `All ${resourceLinked.length} runs are ISOLATED \u2014 no fleet pressure detected, no variation worth tabulating.`
                : `All ${resourceLinked.length} runs share verdict ${v} \u2014 no variation to compare.`;
              return (
                <Box className={classes.section}>
                  <Typography variant="subtitle2">Resource-Linked Breaches</Typography>
                  <Typography variant="body2" style={{ color: tone, fontWeight: 700, marginTop: 8 }}>{msg}</Typography>
                </Box>
              );
            }
            return (
              <Box className={classes.section}>
                <Typography variant="subtitle2">Resource-Linked Breaches</Typography>
                <Typography variant="caption" color="textSecondary">Was the fleet under CPU/memory pressure when this job ran? Correlates SLA breaches/outliers with concurrent infrastructure load.</Typography>
                <Table size="small" className="pe-table" aria-label="Resource-linked breaches table" style={{ marginTop: 8 }}>
                  <TableHead>
                    <TableRow>
                      <TableCell>Job</TableCell>
                      <TableCell>Date</TableCell>
                      <TableCell align="right">Start hour</TableCell>
                      <TableCell align="right">Run hrs</TableCell>
                      <TableCell align="right">Fleet CPU</TableCell>
                      <TableCell align="right">Fleet Mem</TableCell>
                      <TableCell>Critical hosts</TableCell>
                      <TableCell>Verdict</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {rows.map((r, index) => {
                      const s = r.resource_signal || {};
                      const verdictColor = s.verdict === 'RESOURCE_LINK' ? '#f43f5e' : s.verdict === 'TIMING_PRESSURE' ? '#f59e0b' : '#6b7db3';
                      return (
                        <TableRow key={`${r.job_name || 'job'}-${index}`}>
                          <TableCell style={{ fontFamily: 'monospace' }}>{r.job_name || '?'}</TableCell>
                          <TableCell>{r.run_date || '\u2014'}</TableCell>
                          <TableCell align="right">{r.start_hour ?? '\u2014'}h</TableCell>
                          <TableCell align="right">{Number(r.run_hrs ?? 0).toFixed(2)}</TableCell>
                          <TableCell align="right" style={{ color: (s.fleet_cpu || 0) >= 80 ? '#f43f5e' : undefined }}>{Number(s.fleet_cpu ?? 0).toFixed(1)}%</TableCell>
                          <TableCell align="right" style={{ color: (s.fleet_mem || 0) >= 80 ? '#f43f5e' : undefined }}>{Number(s.fleet_mem ?? 0).toFixed(1)}%</TableCell>
                          <TableCell style={{ fontSize: 11 }}>{(s.critical_hosts || []).slice(0, 3).join(', ') || '\u2014'}</TableCell>
                          <TableCell><span style={{ color: verdictColor, fontWeight: 700 }}>{s.verdict || '\u2014'}</span></TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
                {sorted.length > MAX_ROWS && (
                  <Button size="small" onClick={() => setResLinkExpanded((v) => !v)} style={{ marginTop: 6 }}>
                    {resLinkExpanded ? `Collapse to top ${MAX_ROWS} \u25b4` : `View all ${sorted.length} rows \u25be`}
                  </Button>
                )}
              </Box>
            );
          })()}

          {/* ── SLA Buffer bars chart, ported from _renderSlaCharts() (app.js) ── */}
          {sortedJobBuffers.length > 0 && (
            <Box className={classes.section}>
              <Typography variant="subtitle2">{'Job SLA Buffer \u2014 Top 12 by Peak Runtime'}</Typography>
              <HighchartsReact highcharts={Highcharts} options={bufferBarOptions} />
            </Box>
          )}

          <Box
            className={classes.section}
            style={{ borderRadius: 12, border: '1px solid rgba(245,158,11,.3)', background: 'rgba(245,158,11,.05)', padding: 16 }}
          >
            <Typography variant="subtitle2" style={{ color: '#f59e0b' }}>SLA Triage — Action Required</Typography>
            <Typography variant="caption" color="textSecondary" style={{ display: 'block' }}>Low-buffer jobs at risk {'\u00b7'} Unexplained SLA breaches {'\u00b7'} Unresolved cases</Typography>

            {lowBufferJobs.length === 0 && unexplainedBreaches.length === 0 ? (
              <Typography variant="body2" style={{ color: '#10d96e', marginTop: 8 }}>
                {'\u2705'} No low-buffer jobs or unexplained breaches — all SLA triage checks pass.
              </Typography>
            ) : null}

            {lowBufferJobs.length > 0 && (
                <>
                  <Typography variant="caption" style={{ display: 'block', marginTop: 12, color: '#6b7db3' }}>
                    {lowBufferJobs.length} job(s) with under {LOW_BUFFER_THRESHOLD}% buffer to the SLA ceiling
                  </Typography>
                  <Table size="small" className="pe-table" aria-label="Low buffer jobs">
                    <TableHead>
                      <TableRow>
                        <TableCell>Job</TableCell>
                        <TableCell align="right">Buffer %</TableCell>
                        <TableCell align="right">Peak hrs</TableCell>
                        <TableCell align="right">SLA limit</TableCell>
                        <TableCell align="right">Breach rate</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {lowBufferJobs.slice(0, 15).map((job, index) => {
                        const buf = Number(job.buffer_pct ?? 0);
                        return (
                          <TableRow key={`${job.job_name || job.Job_Name || 'job'}-${index}`}>
                            <TableCell>{job.job_name || job.Job_Name || 'Unnamed job'}</TableCell>
                            <TableCell align="right" style={{ color: buf < 10 ? '#f43f5e' : '#f59e0b' }}>{buf.toFixed(1)}%</TableCell>
                            <TableCell align="right">{Number(job.peak_hrs ?? 0).toFixed(2)}</TableCell>
                            <TableCell align="right">{Number(job.sla_limit ?? job.sla_limit_hrs ?? 0).toFixed(2)}</TableCell>
                            <TableCell align="right">{Number(job.breach_rate ?? 0).toFixed(1)}%</TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </>
              )}

              {unexplainedBreaches.length > 0 && (
                <>
                  <Typography variant="caption" style={{ display: 'block', marginTop: 16, color: '#6b7db3' }}>
                    {unexplainedBreaches.length} unexplained breach(es) — no correlated resource pressure detected
                  </Typography>
                  <Table size="small" className="pe-table" aria-label="Unexplained breaches">
                    <TableHead>
                      <TableRow>
                        <TableCell>Job</TableCell>
                        <TableCell>Sub-app</TableCell>
                        <TableCell>Date</TableCell>
                        <TableCell align="right">Run hrs</TableCell>
                        <TableCell align="right">SLA limit</TableCell>
                        <TableCell align="right">Over by</TableCell>
                        <TableCell>Source</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {unexplainedBreaches.slice(0, 15).map((row, index) => (
                        <TableRow key={`${row.job_name || 'job'}-${index}`}>
                          <TableCell>{row.job_name || 'Unnamed job'}</TableCell>
                          <TableCell>{row.sub_application || '—'}</TableCell>
                          <TableCell>{row.run_date || '—'}</TableCell>
                          <TableCell align="right">{Number(row.run_hrs ?? 0).toFixed(2)}</TableCell>
                          <TableCell align="right">{Number(row.sla_limit_hrs ?? 0).toFixed(2)}</TableCell>
                          <TableCell align="right" style={{ color: '#f43f5e' }}>+{Number(row.breach_margin_hrs ?? 0).toFixed(2)}</TableCell>
                          <TableCell>{row.sla_source || 'Unknown'}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </>
              )}
          </Box>

          {baselineEntries.length > 0 && (
            <Box className={classes.section}>
              <Typography variant="subtitle2">Adaptive Fair Job SLA — learned from this file</Typography>
              <Typography variant="caption" color="textSecondary">{'Per-job baseline computed from its own run history (needs \u2265 3 runs for a confident baseline).'}</Typography>
              <Table size="small" className="pe-table" aria-label="Adaptive job baselines" style={{ marginTop: 8 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>Job</TableCell>
                    <TableCell align="right">Runs</TableCell>
                    <TableCell align="right">Avg hrs</TableCell>
                    <TableCell align="right">Std hrs</TableCell>
                    <TableCell align="right">P95 hrs</TableCell>
                    <TableCell align="right">Max hrs</TableCell>
                    <TableCell align="right">Expected hrs</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {baselineEntries.map(([job, baseline]) => (
                    <TableRow key={job}>
                      <TableCell>{job}</TableCell>
                      <TableCell align="right" style={{ color: baseline.sample_size_ok ? '#10d96e' : '#f59e0b' }}>{baseline.runs}</TableCell>
                      <TableCell align="right">{Number(baseline.avg_hrs ?? 0).toFixed(2)}</TableCell>
                      <TableCell align="right">{Number(baseline.std_hrs ?? 0).toFixed(2)}</TableCell>
                      <TableCell align="right">{Number(baseline.p95_hrs ?? 0).toFixed(2)}</TableCell>
                      <TableCell align="right">{Number(baseline.max_hrs ?? 0).toFixed(2)}</TableCell>
                      <TableCell align="right" style={{ color: '#a855f7' }}>{Number(baseline.expected_hrs ?? 0).toFixed(2)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}

          {outliers.length > 0 && (
            <Box className={classes.section}>
              <Typography variant="subtitle2">Job Outliers — within SLA but abnormal timing</Typography>
              <Typography variant="caption" color="textSecondary">
                {'Runs that exceeded a job\'s own learned baseline (z \u2265 2) while staying under the global SLA ceiling.'}
              </Typography>
              {criticalOutliers.length > 0 ? (
                <Table size="small" className="pe-table" aria-label="Job outliers" style={{ marginTop: 8 }}>
                  <TableHead>
                    <TableRow>
                      <TableCell>Job</TableCell>
                      <TableCell>Date</TableCell>
                      <TableCell align="right">Start</TableCell>
                      <TableCell align="right">End</TableCell>
                      <TableCell align="right">Run hrs</TableCell>
                      <TableCell align="right">Expected hrs</TableCell>
                      <TableCell align="right">Over by</TableCell>
                      <TableCell align="right">Z-score</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {criticalOutliers.map((row, index) => (
                      <TableRow key={`${row.job_name}-${index}`}>
                        <TableCell>{row.job_name}</TableCell>
                        <TableCell>{row.run_date}</TableCell>
                        <TableCell align="right">{row.start_time}</TableCell>
                        <TableCell align="right">{row.end_time}</TableCell>
                        <TableCell align="right" style={{ color: '#f59e0b' }}>{Number(row.run_hrs ?? 0).toFixed(2)}</TableCell>
                        <TableCell align="right" style={{ color: '#a855f7' }}>{Number(row.expected_hrs ?? 0).toFixed(2)}</TableCell>
                        <TableCell align="right" style={{ color: '#f59e0b' }}>+{Number(row.expected_margin_hrs ?? 0).toFixed(2)}</TableCell>
                        <TableCell align="right" style={{ color: row.outlier_z >= 3 ? '#f43f5e' : '#f59e0b' }}>{Number(row.outlier_z ?? 0).toFixed(1)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <Typography variant="body2" style={{ color: '#f59e0b', marginTop: 8 }}>
                  {outliers.length} mild outlier(s) (z 2{'\u2013'}3, under 25% over baseline) — informational only, no action required.
                </Typography>
              )}
            </Box>
          )}
        </>
      )}
    </Paper>
    </Box>
  );
}
