import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Box,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TableSortLabel,
  Typography,
  makeStyles,
} from '@material-ui/core';
import Highcharts from '../../theme/highchartsSetup';
import HighchartsReact from 'highcharts-react-official';
import { useAppData } from '../../context/AppDataContext';
import { KpiStatCard } from '../shared/KpiStatCard';

interface BatchKpis {
  compliance_pct?: number;
  window_compliance_pct?: number;
  batch_window_compliance?: number;
  total_runs?: number;
  total_jobs?: number;
  total_hrs?: number;
  jobs_breach?: number;
  jobs_at_risk?: number;
  jobs_ok?: number;
  failed_runs?: number;
  fail_rate_pct?: number;
  daily_limit_hrs?: number;
}

interface TopJobRow {
  Job_Name: string;
  Sub_Application?: string;
  peak_hrs: number;
  avg_hrs: number;
  total_hrs: number;
  buffer_pct?: number | null;
  buffer_status: string;
}

interface WindowPoint {
  run_date: string;
  total_hrs: number;
  job_count: number;
  breach: boolean;
  top_job?: string | null;
}

interface ElapsedWindow {
  available: boolean;
  worst_day?: { run_date?: string; elapsed_hrs?: number };
  avg_elapsed_hrs?: number | null;
}

interface SummedRuntime {
  total_hrs: number;
  worst_day_hrs: number;
  avg_day_hrs: number;
}

interface WorstJob {
  job_name: string;
  peak_hrs: number;
  sla_hrs: number;
  buffer_pct: number;
  sla_source: string;
}

interface SlaSourceInfo {
  type: string;
  daily_hrs: number;
  adaptive_active: boolean;
  adaptive_job_count: number;
  adaptive_total_jobs: number;
}

interface DataWarning {
  code: string;
  text: string;
  severity: string;
}

interface DataCoverage {
  date_range?: [string, string] | string[];
  date_span_days?: number;
  confidence_label?: string;
  warnings?: DataWarning[];
}

interface SlaHeatmapCell {
  job: string;
  date: string;
  hrs: number | null;
  breach: boolean;
  sla_limit?: number;
}

interface SlaHeatmap {
  jobs: string[];
  dates: string[];
  cells: SlaHeatmapCell[];
  limit: number;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  kpiRow: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2) },
  kpi: { padding: theme.spacing(1.5), minWidth: 130 },
  chart: { marginTop: theme.spacing(3) },
  empty: { marginTop: theme.spacing(2) },
}));

type SortKey = 'Job_Name' | 'peak_hrs' | 'avg_hrs' | 'total_hrs';

const CONFIDENCE_COLOR: Record<string, string> = {
  HIGH: '#10d96e',
  MEDIUM: '#3b82f6',
  LOW: '#f59e0b',
  INSUFFICIENT: '#f43f5e',
};

export function BatchPanel() {
  const classes = useStyles();
  const { data } = useAppData();
  const [sortKey, setSortKey] = useState<SortKey>('peak_hrs');
  const [sortDesc, setSortDesc] = useState(true);

  const kpis = (data.batch?.kpis || {}) as BatchKpis;
  const topJobs = ((data.batch?.top_jobs as TopJobRow[]) || []).slice();
  const topBreaches = ((data.batch?.top_breaches as TopJobRow[]) || []).slice();
  const window = ((data.batch?.window as WindowPoint[]) || []).slice();
  const elapsedWindow = data.batch?.elapsed_window as ElapsedWindow | undefined;
  const summedRuntime = data.batch?.summed_runtime as SummedRuntime | undefined;
  const worstJob = data.batch?.worst_job as WorstJob | undefined;
  const slaSource = data.batch?.sla_source as SlaSourceInfo | undefined;
  const dataCoverage = data.batch?.data_coverage as DataCoverage | undefined;
  const slaHeatmap = data.batch?.sla_heatmap as SlaHeatmap | undefined;
  const windowCompliance = kpis.window_compliance_pct ?? kpis.batch_window_compliance ?? 0;
  const slaCeilingHrs = kpis.daily_limit_hrs || 6;

  const sortedJobs = useMemo(() => {
    const rows = [...topJobs];
    rows.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : Number(av) - Number(bv);
      return sortDesc ? -cmp : cmp;
    });
    return rows;
  }, [topJobs, sortKey, sortDesc]);

  const chartOptions: Highcharts.Options = {
    chart: { type: 'column', height: 280 },
    title: { text: undefined },
    xAxis: { categories: window.map((point) => point.run_date) },
    yAxis: {
      title: { text: 'Total hours' },
      plotLines: [{
        value: slaCeilingHrs,
        color: '#f59e0b',
        dashStyle: 'Dash',
        width: 2,
        label: { text: `SLA ceiling ${slaCeilingHrs}h`, style: { color: '#f59e0b', fontSize: '10px' } },
      }],
    },
    tooltip: {
      formatter(this: Highcharts.TooltipFormatterContextObject) {
        const point = window[this.point.index];
        if (!point) return `${this.x}: ${this.y}h`;
        return `<b>${point.run_date}</b><br/>${point.total_hrs.toFixed(2)}h across ${point.job_count} job(s)` +
          (point.top_job ? `<br/>Top job: ${point.top_job}` : '') +
          (point.breach ? '<br/><span style="color:#f43f5e">BREACH</span>' : '');
      },
    },
    series: [
      {
        type: 'column',
        name: 'Daily batch hours',
        data: window.map((point) => ({
          y: point.total_hrs,
          color: point.breach ? '#f43f5e' : point.total_hrs >= slaCeilingHrs * 0.85 ? '#f59e0b' : '#10d96e',
        })),
      },
    ],
  };

  const gaugeValue = worstJob ? Math.max(-100, Math.min(100, worstJob.buffer_pct)) : 0;
  const gaugeOptions: Highcharts.Options = {
    chart: { type: 'solidgauge', height: 220 },
    title: { text: undefined },
    pane: {
      center: ['50%', '85%'],
      size: '140%',
      startAngle: -90,
      endAngle: 90,
      background: [{ backgroundColor: 'rgba(255,255,255,.05)', innerRadius: '60%', outerRadius: '100%', shape: 'arc' }],
    },
    yAxis: {
      min: -100,
      max: 100,
      stops: [
        [0.0, '#f43f5e'],
        [0.5, '#f59e0b'],
        [1.0, '#10d96e'],
      ],
      lineWidth: 0,
      tickWidth: 0,
      minorTickInterval: undefined,
      labels: { enabled: false },
    },
    series: [{
      type: 'solidgauge',
      data: [gaugeValue],
      dataLabels: {
        format: `<div style="text-align:center"><span style="font-size:22px;font-weight:800;color:${worstJob && worstJob.buffer_pct < 0 ? '#f43f5e' : '#10d96e'}">{y:.1f}%</span></div>`,
        useHTML: true,
      },
    }],
  };

  const heatmapOptions: Highcharts.Options | null = slaHeatmap && slaHeatmap.cells.length > 0 ? {
    chart: { type: 'heatmap', height: Math.max(240, slaHeatmap.jobs.length * 24) },
    title: { text: undefined },
    xAxis: { categories: slaHeatmap.dates, labels: { rotation: -45 } },
    yAxis: { categories: slaHeatmap.jobs, title: { text: undefined }, reversed: true },
    colorAxis: {
      min: 0,
      stops: [
        [0, '#10d96e'],
        [0.7, '#f59e0b'],
        [1, '#f43f5e'],
      ],
    },
    tooltip: {
      formatter(this: Highcharts.TooltipFormatterContextObject) {
        const cell = slaHeatmap.cells[this.point.index];
        if (!cell || cell.hrs == null) return 'No run';
        return `<b>${cell.job}</b><br/>${cell.date}: ${cell.hrs.toFixed(2)}h` + (cell.breach ? ' <span style="color:#f43f5e">BREACH</span>' : '');
      },
    },
    series: [{
      type: 'heatmap',
      data: slaHeatmap.cells.map((cell) => {
        const jobIndex = slaHeatmap.jobs.indexOf(cell.job);
        const dateIndex = slaHeatmap.dates.indexOf(cell.date);
        const ceiling = cell.sla_limit || slaHeatmap.limit || 6;
        const ratio = cell.hrs != null ? cell.hrs / ceiling : null;
        return { x: dateIndex, y: jobIndex, value: ratio };
      }),
    }],
  } : null;

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDesc(!sortDesc);
    } else {
      setSortKey(key);
      setSortDesc(true);
    }
  };

  if (!data.batch) {
    return (
      <Paper
        className={classes.panel}
        elevation={0}
        style={{ border: '1px solid #213060', borderRadius: 12, background: 'rgba(17,29,54,.6)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}
      >
        <Box>
          <Typography variant="subtitle2">No Ctrl-M data loaded yet</Typography>
          <Typography className={classes.empty} variant="body2" color="textSecondary">
            Upload your Ctrl-M CSV/XLSX from Upload &amp; Intake.
          </Typography>
        </Box>
        <Link to="/upload" className="metric-badge metric-badge-green" style={{ textDecoration: 'none' }}>
          Go to Upload →
        </Link>
      </Paper>
    );
  }

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Box display="flex" alignItems="center" style={{ gap: 10, marginBottom: 4 }}>
        <span className="status-dot status-dot-green" />
        <Typography variant="h6">Batch Review</Typography>
      </Box>

      {dataCoverage && (
        <Box display="flex" style={{ gap: 8, flexWrap: 'wrap', marginTop: 8, marginBottom: 4 }}>
          {dataCoverage.date_range && dataCoverage.date_range.length === 2 && (
            <span className="metric-badge metric-badge-blue">{dataCoverage.date_range[0]} to {dataCoverage.date_range[1]}</span>
          )}
          {dataCoverage.confidence_label && (
            <span
              className="metric-badge"
              style={{
                background: `${CONFIDENCE_COLOR[dataCoverage.confidence_label] || '#6b7db3'}1f`,
                color: CONFIDENCE_COLOR[dataCoverage.confidence_label] || '#6b7db3',
                border: `1px solid ${CONFIDENCE_COLOR[dataCoverage.confidence_label] || '#6b7db3'}40`,
              }}
            >
              {dataCoverage.confidence_label} confidence
            </span>
          )}
          {(dataCoverage.warnings || []).slice(0, 3).map((warning, index) => (
            <span
              key={index}
              className={`metric-badge ${warning.severity === 'warning' ? 'metric-badge-amber' : 'metric-badge-blue'}`}
              title={warning.text}
            >
              {warning.text.length > 60 ? `${warning.text.slice(0, 60)}…` : warning.text}
            </span>
          ))}
        </Box>
      )}

      <Box className={classes.kpiRow} style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12 }}>
        {elapsedWindow?.available && (
          <KpiStatCard
            label="Effective Window"
            value={`${Number(elapsedWindow.worst_day?.elapsed_hrs || 0).toFixed(1)}h`}
            sub={`avg ${Number(elapsedWindow.avg_elapsed_hrs || 0).toFixed(1)}h · ${elapsedWindow.worst_day?.run_date || ''}`}
            accent="#a855f7"
          />
        )}
        {summedRuntime && (
          <KpiStatCard
            label="Summed Runtime"
            value={`${summedRuntime.total_hrs.toFixed(1)}h`}
            sub={`worst day ${summedRuntime.worst_day_hrs.toFixed(1)}h · avg ${summedRuntime.avg_day_hrs.toFixed(1)}h`}
            accent="#3b82f6"
          />
        )}
        {worstJob && (
          <KpiStatCard
            label="Worst-Job Peak"
            value={`${worstJob.peak_hrs.toFixed(2)}h`}
            sub={`${worstJob.job_name} · ${worstJob.buffer_pct.toFixed(1)}% buffer`}
            accent="#f59e0b"
          />
        )}
        <KpiStatCard label="Job SLA" value={`${(kpis.compliance_pct || 0).toFixed(1)}%`} sub="Peak job runtime vs SLA ceiling" accent="#10d96e" />
        <KpiStatCard label="Window SLA" value={`${windowCompliance.toFixed(1)}%`} sub="Day-level batch window vs ceiling" accent="#f59e0b" />
        <KpiStatCard label="Peak · Risk · OK" accent="#f43f5e" value={
          <span>
            <span style={{ color: '#f43f5e' }}>{kpis.jobs_breach || 0}</span>
            <span style={{ color: '#6b7db3', margin: '0 4px', fontSize: 16 }}>·</span>
            <span style={{ color: '#f59e0b' }}>{kpis.jobs_at_risk || 0}</span>
            <span style={{ color: '#6b7db3', margin: '0 4px', fontSize: 16 }}>·</span>
            <span style={{ color: '#10d96e' }}>{kpis.jobs_ok || 0}</span>
          </span>
        } sub="Job-level SLA status by peak runtime" />
        <KpiStatCard label="Failed Runs" value={kpis.failed_runs || 0} sub={`${(kpis.fail_rate_pct || 0).toFixed(1)}% of all runs`} accent="#fb923c" />
        {slaSource && (
          <KpiStatCard
            label="SLA Source"
            value={slaSource.adaptive_active ? 'Adaptive' : slaSource.type}
            valueColor="#2dd4bf"
            sub={slaSource.adaptive_active ? `${slaSource.adaptive_job_count}/${slaSource.adaptive_total_jobs} jobs on adaptive baseline` : `${slaSource.daily_hrs}h daily ceiling`}
            accent="#2dd4bf"
          />
        )}
      </Box>
      <Typography variant="caption" style={{ display: 'block', marginTop: 12, color: '#6b7db3' }}>
        <span style={{ color: '#3b82f6' }}>ℹ</span>{' '}
        <strong style={{ color: '#f0f4ff' }}>Job SLA</strong> = % of jobs whose own peak runtime beat its SLA ceiling.
        <strong style={{ color: '#f0f4ff' }}> Window SLA</strong> = % of days the full batch window beat the same ceiling.
      </Typography>



      {(window.length > 0 || worstJob) && (
        <Box className={classes.chart} style={{ display: 'grid', gridTemplateColumns: worstJob ? '1fr 2fr' : '1fr', gap: 16 }}>
          {worstJob && (
            <Box className="chart-panel" style={{ padding: 16 }}>
              <Typography variant="subtitle2">SLA Buffer Gauge</Typography>
              <Typography variant="caption" color="textSecondary">Headroom between worst-job peak and the SLA ceiling</Typography>
              <HighchartsReact highcharts={Highcharts} options={gaugeOptions} />
              <Typography variant="caption" style={{ display: 'block', textAlign: 'center', color: '#6b7db3' }}>
                {worstJob.job_name} · {worstJob.peak_hrs.toFixed(2)}h vs {worstJob.sla_hrs.toFixed(2)}h ceiling
              </Typography>
            </Box>
          )}
          {window.length > 0 && (
            <Box className="chart-panel" style={{ padding: 16 }}>
              <Typography variant="subtitle2">Daily Batch Window</Typography>
              <Typography variant="caption" color="textSecondary">Bar color = SLA buffer that day</Typography>
              <HighchartsReact highcharts={Highcharts} options={chartOptions} />
            </Box>
          )}
        </Box>
      )}

      {slaHeatmap && slaHeatmap.cells.length > 0 && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">SLA Compliance Heatmap</Typography>
          <Typography variant="caption" color="textSecondary">Job × Date — green = healthy buffer, amber = near SLA, red = breach</Typography>
          <HighchartsReact highcharts={Highcharts} options={heatmapOptions as Highcharts.Options} />
        </Box>
      )}

      {sortedJobs.length > 0 && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">Top jobs</Typography>
          <Table size="small" className="pe-table" aria-label="Top jobs table">
            <TableHead>
              <TableRow>
                <TableCell>
                  <TableSortLabel active={sortKey === 'Job_Name'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('Job_Name')}>
                    Job
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'peak_hrs'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('peak_hrs')}>
                    Peak hours
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'avg_hrs'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('avg_hrs')}>
                    Avg hours
                  </TableSortLabel>
                </TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sortedJobs.slice(0, 25).map((job) => (
                <TableRow key={job.Job_Name}>
                  <TableCell>{job.Job_Name}</TableCell>
                  <TableCell align="right">{job.peak_hrs.toFixed(2)}</TableCell>
                  <TableCell align="right">{job.avg_hrs.toFixed(2)}</TableCell>
                  <TableCell>{job.buffer_status}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      )}

      {topBreaches.length > 0 && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">Top Performance Regressions</Typography>
          <Typography variant="caption" color="textSecondary">Jobs with the least SLA buffer remaining — highest breach risk first.</Typography>
          <Table size="small" className="pe-table" aria-label="Top performance regressions table" style={{ marginTop: 8 }}>
            <TableHead>
              <TableRow>
                <TableCell>Job</TableCell>
                <TableCell>Sub-app</TableCell>
                <TableCell align="right">Peak hrs</TableCell>
                <TableCell align="right">Avg hrs</TableCell>
                <TableCell align="right">Buffer %</TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {topBreaches.slice(0, 9).map((job, index) => {
                const buffer = job.buffer_pct;
                return (
                  <TableRow key={`${job.Job_Name}-${index}`}>
                    <TableCell>{job.Job_Name}</TableCell>
                    <TableCell>{job.Sub_Application || '—'}</TableCell>
                    <TableCell align="right">{job.peak_hrs.toFixed(2)}</TableCell>
                    <TableCell align="right">{job.avg_hrs.toFixed(2)}</TableCell>
                    <TableCell align="right" style={{ color: buffer != null && buffer < 0 ? '#f43f5e' : '#f59e0b' }}>
                      {buffer != null ? `${buffer.toFixed(1)}%` : '—'}
                    </TableCell>
                    <TableCell>{job.buffer_status}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Box>
      )}
    </Paper>
  );
}
