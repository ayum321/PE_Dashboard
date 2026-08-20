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
import { uploadSlaMatrix } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { SectionBanner } from '../shared/SectionBanner';
import { KpiStatCard } from '../shared/KpiStatCard';

interface SlaBreach {
  job_name?: string;
  sub_application?: string;
  run_date?: string;
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
  sla_limit?: number;
  sla_limit_hrs?: number;
  breach_rate?: number;
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
  const compliancePct = Number(slaMatrix.compliance_pct) || 0;
  const windowDayPct = slaMatrix.window_day_compliance_pct != null ? Number(slaMatrix.window_day_compliance_pct) : compliancePct;

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
          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 12, marginTop: 16 }}>
            <KpiStatCard label="Compliance" value={`${compliancePct.toFixed(1)}%`} sub="Runs within SLA ceiling" accent="#10d96e" />
            <KpiStatCard label="Total Runs" value={Number(slaMatrix.total_runs) || 0} sub={`${Number(slaMatrix.total_jobs) || 0} unique jobs`} accent="#3b82f6" />
            <KpiStatCard label="Breaching" value={Number(slaMatrix.breaching_runs) || 0} sub="Over SLA ceiling" accent="#f43f5e" />
            <KpiStatCard label="At Risk" value={Number(slaMatrix.at_risk_runs) || 0} sub="Near SLA ceiling" accent="#f59e0b" />
            <KpiStatCard label="Long Jobs" value={Number(slaMatrix.long_job_runs) || 0} sub="15\u201340% of SLA ceiling" accent="#2dd4bf" />
            <KpiStatCard label="Failed Runs" value={Number(slaMatrix.failed_runs) || 0} sub="Execution failures" accent="#fb923c" />
            {slaMatrix.worst_job != null && (
              <KpiStatCard label="Worst Job" value={`${Number(slaMatrix.worst_hrs || 0).toFixed(2)}h`} sub={String(slaMatrix.worst_job)} accent="#a855f7" />
            )}
          </Box>

          {breaches.length > 0 && (
            <Table size="small" className="pe-table" aria-label="SLA breach table" style={{ marginTop: 16 }}>
              <TableHead>
                <TableRow>
                  <TableCell>Job</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell align="right">Runtime hours</TableCell>
                  <TableCell align="right">Over SLA hours</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {breaches.slice(0, 25).map((breach, index) => (
                  <TableRow key={`${breach.job_name || 'job'}-${index}`}>
                    <TableCell>{breach.job_name || 'Unnamed job'}</TableCell>
                    <TableCell>{breach.status || 'BREACH'}</TableCell>
                    <TableCell align="right">{(breach.run_hrs || 0).toFixed(2)}</TableCell>
                    <TableCell align="right">{(breach.breach_margin_hrs || 0).toFixed(2)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          {(lowBufferJobs.length > 0 || unexplainedBreaches.length > 0) && (
            <Box
              className={classes.section}
              style={{ borderRadius: 12, border: '1px solid rgba(245,158,11,.3)', background: 'rgba(245,158,11,.05)', padding: 16 }}
            >
              <Typography variant="subtitle2" style={{ color: '#f59e0b' }}>SLA Triage — Action Required</Typography>

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
          )}

          {baselineEntries.length > 0 && (
            <Box className={classes.section}>
              <Typography variant="subtitle2">Adaptive Fair Job SLA — learned from this file</Typography>
              <Typography variant="caption" color="textSecondary">Per-job baseline computed from its own run history (needs \u2265 3 runs for a confident baseline).</Typography>
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
                Runs that exceeded a job's own learned baseline (z \u2265 2) while staying under the global SLA ceiling.
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
                  {outliers.length} mild outlier(s) (z 2\u20133, under 25% over baseline) — informational only, no action required.
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
