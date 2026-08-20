import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Box,
  Button,
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
import { refreshBatch } from '../../api/dashboardApi';

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
  fleet_sla_buffer?: FleetSlaBuffer;
  batch_env?: string;
  env_type?: string;
}

interface TopJobRow {
  Job_Name: string;
  Sub_Application?: string;
  peak_hrs: number;
  avg_hrs: number;
  total_hrs: number;
  sla_hrs?: number | null;
  sla_used_pct?: number | null;
  buffer_pct?: number | null;
  buffer_status: string;
  concurrent_overlap_hrs?: number | null;
  concurrent_job_count?: number | null;
  is_utility?: boolean;
  utility_reason?: string;
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
  confidence?: number;
  confidence_label?: string;
  has_synthetic_timestamps?: boolean;
  warnings?: DataWarning[];
  excluded_jobs?: ExcludedJobRaw[];
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

interface FleetSlaBuffer {
  buffer_hrs: number;
  buffer_pct: number;
  status: string;
  sla_source?: string;
}

interface ExcludedJobRaw {
  job_name?: string;
  name?: string;
  reason?: string;
}

interface ConcurrencyBurst {
  run_date: string;
  start_clock: string;
  end_clock: string;
  duration_min: number;
  peak_concurrent: number;
  jobs: string[];
  job_count: number;
}

interface ConcurrencyGroup {
  example_sub_app: string;
  job_count: number;
  distinct_jobs_total: number;
  occurrences: number;
  days_seen: number;
  peak_concurrent: number;
  avg_duration_min: number;
  burst_tightness: number;
  trend: number[];
  severity_level: string;
  bursts: ConcurrencyBurst[];
}

interface Concurrency {
  total_days_with_concurrency: number;
  groups: ConcurrencyGroup[];
}

interface HourHeatmapCell {
  sub_app: string;
  hour: number;
  count: number;
  total_hrs: number;
}

interface HourHeatmapData {
  sub_apps: string[];
  hours: number[];
  cells: HourHeatmapCell[];
}

interface UnifiedExclusionRow {
  name: string;
  category: string;
  why: string;
  scope: 'ALL_METRICS' | 'COMPLIANCE_ONLY';
  isUtil: boolean;
}

/** Derive a display category from a reason string like "purge_(0.051h<0.100h)". */
function _exclusionCategory(reason: string): string {
  const r = String(reason || '').trim();
  if (!r) return 'OTHER';
  const base = r.split('(')[0].trim().replace(/^_+|_+$/g, '');
  return (base || r).toUpperCase() || 'OTHER';
}

function _exclusionCategoryTone(cat: string): string {
  if (cat === 'INSUFFICIENT') return '#6b7db3';
  if (cat === 'SHORT_JOB') return '#2dd4bf';
  if (cat === 'FILE_WATCHER' || cat === 'FW') return '#3b82f6';
  if (cat === 'EXPORT') return '#a855f7';
  if (['PURGE', 'TRUNCATE', 'ARCHIVE_LOG', 'DELETE_TYPE', 'BACKUP', 'DB_BACKUP', 'DB_RESTORE', 'DB_CLEANUP'].includes(cat)) return '#f59e0b';
  return '#6b7db3';
}

/** Quality-based exclusions (too few/short runs) can't be scored back in. */
function _exclusionIsUtility(cat: string): boolean {
  return !['INSUFFICIENT', 'SHORT_JOB'].includes(cat.toUpperCase());
}

/** Human-readable "why excluded" from a raw reason token. */
function _exclusionWhy(reason: string): string {
  const r = String(reason || '').trim();
  const up = r.toUpperCase();
  if (up === 'INSUFFICIENT') return 'Fewer than 3 runs — no reliable SLA baseline to score against.';
  if (up === 'SHORT_JOB') return 'Near-zero duration — no measurable runtime to score.';
  const m = r.match(/^(.*?)\(?\s*([\d.]+)\s*h?\s*<\s*([\d.]+)\s*h?\)?$/i);
  if (m && m[2] && m[3]) {
    const pat = m[1].replace(/[_\s]+$/, '') || r;
    return `Runtime ${Number(m[2]).toFixed(3)}h under the ${Number(m[3]).toFixed(2)}h utility ceiling — matched pattern '${pat}', treated as housekeeping.`;
  }
  return `Matched utility pattern '${r}' — housekeeping job, not real batch work.`;
}

function _concSeverityTheme(level: string): { label: string; color: string } {
  const lv = String(level || '').toLowerCase();
  if (lv === 'high') return { label: 'HIGH', color: '#f43f5e' };
  if (lv === 'medium') return { label: 'MED', color: '#f59e0b' };
  return { label: 'LOW', color: '#2dd4bf' };
}

/** Plain-language one-liner, ported from _concExplainSentence() (app.js). */
function _concExplainSentence(group: ConcurrencyGroup): string {
  const peak = Number(group?.peak_concurrent) || 0;
  const days = Number(group?.days_seen) || 0;
  const avgMin = Number(group?.avg_duration_min) || 0;
  const tight = Number(group?.burst_tightness) || 0;
  if (peak <= 0) return 'No meaningful job overlap detected for this sub-application.';
  const durText = avgMin < 1 ? `${Math.max(1, Math.round(avgMin * 60))}s` : `${avgMin.toFixed(1)}min`;
  const dayText = `${days} day${days === 1 ? '' : 's'}`;
  if (tight >= 3 || peak >= 6) {
    return `${peak} jobs pile into the same ~${durText} window on ${dayText} — a short, sharp burst rather than sustained overlap.`;
  }
  if (peak >= 4) {
    return `Up to ${peak} jobs overlap for ~${durText} at a time across ${dayText} — moderate contention; staggering start times would help.`;
  }
  return `Occasional overlap: up to ${peak} jobs together for ~${durText}, seen on ${dayText} — low risk.`;
}

function _excludedJobsCsv(rows: UnifiedExclusionRow[]): string {
  const cols = ['job_name', 'category', 'why_excluded', 'scope'];
  const out = [cols.join(',')];
  rows.forEach((r) => {
    out.push([r.name, r.category, r.why, r.scope].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','));
  });
  return out.join('\n');
}

interface LongpoleRow {
  job: string;
  avg_min: number;
  max_min: number;
  runs: number;
  days_present: number;
  days_total: number;
  spike_ratio: number;
  window_share_pct: number;
  is_longpole: boolean;
  stability: string;
}

interface LongpoleCell {
  job: string;
  date: string;
  minutes: number;
}

interface LongpoleMatrix {
  jobs: string[];
  dates: string[];
  cells: LongpoleCell[];
  rows: LongpoleRow[];
  has_data: boolean;
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

const STATUS_COLOR: Record<string, string> = {
  BREACH: '#f43f5e',
  AT_RISK: '#f59e0b',
  LONG_JOB: '#2dd4bf',
  OK: '#10d96e',
  SLA_MISSING: '#6b7db3',
};

// Mirrors services/pe_config.py SLA_ATRISK_PCT / SLA_LONGJOB_PCT defaults.
const SLA_ATRISK_PCT = 15;
const SLA_LONGJOB_PCT = 40;

export function BatchPanel() {
  const classes = useStyles();
  const { data, setBatch } = useAppData();
  const [sortKey, setSortKey] = useState<SortKey>('peak_hrs');
  const [sortDesc, setSortDesc] = useState(true);
  const [manualInclude, setManualInclude] = useState<Set<string>>(new Set());
  const [manualExcludeReasons, setManualExcludeReasons] = useState<Map<string, string>>(new Map());
  const [manualExclude, setManualExclude] = useState<Set<string>>(new Set());
  const [excludedDetailOpen, setExcludedDetailOpen] = useState(false);
  const [excludedFilter, setExcludedFilter] = useState('');
  const [addJobName, setAddJobName] = useState('');
  const [addJobReason, setAddJobReason] = useState('');
  const [exclusionsBusy, setExclusionsBusy] = useState(false);

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
  const fleetSlaBuffer = kpis.fleet_sla_buffer;
  const longpole = data.batch?.longpole_matrix as LongpoleMatrix | undefined;
  const windowCompliance = kpis.window_compliance_pct ?? kpis.batch_window_compliance ?? 0;
  const slaCeilingHrs = kpis.daily_limit_hrs || 6;
  const concurrency = data.batch?.concurrency as Concurrency | undefined;
  const hourHeatmap = data.batch?.hour_heatmap as HourHeatmapData | undefined;
  const sowCompare = data.sowCompare as { metrics?: { sow?: number; actual?: number; pct?: number; label?: string }[] } | null;
  const benchmarkPerf = (data.benchmark as { batch_perf_summary?: Record<string, unknown>; filename?: string } | null)?.batch_perf_summary;

  // ── ENV chip — TEST/UAT vs PROD badge, ported from renderBatchKpis() FIX 6.3 ──
  const envValue = (kpis.batch_env || kpis.env_type || '').toUpperCase();
  const envChip = envValue === 'TEST' || envValue === 'UAT'
    ? { label: envValue, color: '#f59e0b', title: 'This data is from a TEST/UAT environment — not production' }
    : envValue === 'PROD' || envValue === 'PRODUCTION'
      ? { label: 'PROD', color: '#10d96e', title: '' }
      : null;

  // ── Unified exclusion rows — merges auto utility-pattern exclusions (top_jobs
  // is_utility) with backend compliance-only exclusions (data_coverage.excluded_jobs)
  // into one table, ported from _buildUnifiedExclusionRows() (app.js). ──
  const unifiedExclusions = useMemo<UnifiedExclusionRow[]>(() => {
    const rows = new Map<string, UnifiedExclusionRow>();
    const allTopJobs = (data.batch?.top_jobs as TopJobRow[]) || [];
    allTopJobs.filter((j) => j.is_utility).forEach((j) => {
      const name = j.Job_Name;
      if (manualInclude.has(name)) return;
      const customReason = manualExcludeReasons.get(name);
      rows.set(name.toUpperCase(), {
        name,
        category: 'UTILITY',
        why: _exclusionWhy(j.utility_reason || 'utility pattern'),
        scope: 'ALL_METRICS',
        isUtil: true,
      });
      if (customReason) rows.get(name.toUpperCase())!.why = customReason;
    });
    (dataCoverage?.excluded_jobs || []).forEach((j) => {
      const name = j.job_name || j.name || '?';
      const key = name.toUpperCase();
      if (rows.has(key)) return;
      const cat = _exclusionCategory(j.reason || '');
      rows.set(key, {
        name,
        category: cat,
        why: _exclusionWhy(j.reason || ''),
        scope: 'COMPLIANCE_ONLY',
        isUtil: _exclusionIsUtility(cat),
      });
    });
    manualExclude.forEach((name) => {
      const key = name.toUpperCase();
      if (rows.has(key)) return;
      rows.set(key, {
        name,
        category: 'MANUAL',
        why: manualExcludeReasons.get(name) || 'Manually excluded by the reviewer for this session — removed from every metric.',
        scope: 'ALL_METRICS',
        isUtil: true,
      });
    });
    return Array.from(rows.values()).sort((a, b) => {
      if (a.scope !== b.scope) return a.scope === 'ALL_METRICS' ? -1 : 1;
      if (a.category !== b.category) return a.category.localeCompare(b.category);
      return a.name.localeCompare(b.name);
    });
  }, [data.batch, manualInclude, manualExclude, manualExcludeReasons, dataCoverage]);

  const includedBackJobs = ((data.batch?.top_jobs as TopJobRow[]) || []).filter((j) => j.is_utility && manualInclude.has(j.Job_Name));
  const filteredExclusions = unifiedExclusions.filter((row) =>
    !excludedFilter.trim() || row.name.toLowerCase().includes(excludedFilter.trim().toLowerCase()));
  const byCat = new Map<string, number>();
  unifiedExclusions.forEach((row) => byCat.set(row.category, (byCat.get(row.category) || 0) + 1));
  const allMetricsCount = unifiedExclusions.filter((r) => r.scope === 'ALL_METRICS').length;
  const complianceOnlyCount = unifiedExclusions.length - allMetricsCount;
  const manualCount = unifiedExclusions.filter((r) => r.category === 'MANUAL').length;

  const applyExclusions = async (nextInclude: Set<string>, nextExclude: Set<string>, nextReasons: Map<string, string>) => {
    setExclusionsBusy(true);
    try {
      const manualExclusions = Array.from(nextExclude).map((name) => ({ name, reason: nextReasons.get(name) || '' }));
      const refreshed = await refreshBatch(manualExclusions);
      setBatch(refreshed);
    } catch {
      // Refresh failed — local state still updates below so the UI reflects intent.
    } finally {
      setExclusionsBusy(false);
    }
  };

  const handleReinclude = (name: string) => {
    const nextExclude = new Set(manualExclude); nextExclude.delete(name);
    const nextInclude = new Set(manualInclude).add(name);
    const nextReasons = new Map(manualExcludeReasons); nextReasons.delete(name);
    setManualExclude(nextExclude); setManualInclude(nextInclude); setManualExcludeReasons(nextReasons);
    applyExclusions(nextInclude, nextExclude, nextReasons);
  };
  const handleReexclude = (name: string) => {
    const nextInclude = new Set(manualInclude); nextInclude.delete(name);
    setManualInclude(nextInclude);
    applyExclusions(nextInclude, manualExclude, manualExcludeReasons);
  };
  const handleAddManualExclude = () => {
    const val = addJobName.trim();
    if (!val) return;
    const nextExclude = new Set(manualExclude).add(val);
    const nextInclude = new Set(manualInclude); nextInclude.delete(val);
    const nextReasons = new Map(manualExcludeReasons);
    if (addJobReason.trim()) nextReasons.set(val, addJobReason.trim()); else nextReasons.delete(val);
    setManualExclude(nextExclude); setManualInclude(nextInclude); setManualExcludeReasons(nextReasons);
    setAddJobName(''); setAddJobReason('');
    applyExclusions(nextInclude, nextExclude, nextReasons);
  };
  const handleResetAllExclusions = () => {
    setManualInclude(new Set()); setManualExclude(new Set()); setManualExcludeReasons(new Map());
    applyExclusions(new Set(), new Set(), new Map());
  };

  // Real narrative formulas ported from renderBatchStory()/_buildBatchNarrative() (app.js) —
  // derived from the same window[] data, not fabricated wording.
  const narrative = useMemo(() => {
    if (window.length === 0) return null;
    const total = window.length;
    const breachDays = window.filter((point) => point.breach).length;
    const cleanDays = total - breachDays;
    const compliancePct = windowCompliance || (total > 0 ? (cleanDays / total) * 100 : 0);
    const worstBreach = window.filter((point) => point.breach).sort((a, b) => b.total_hrs - a.total_hrs)[0];
    const tone = breachDays === 0 ? 'ok' : breachDays / total > 0.3 ? 'critical' : 'warning';
    return { total, breachDays, cleanDays, compliancePct, worstBreach, tone };
  }, [window, windowCompliance]);

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

  const gaugeSource = fleetSlaBuffer || (worstJob ? { buffer_pct: worstJob.buffer_pct, status: undefined, sla_source: worstJob.sla_source } : null);
  const gaugeNeedle = gaugeSource ? Math.max(0, Math.min(100, gaugeSource.buffer_pct)) : 0;
  const gaugeStatus = gaugeSource?.status;
  const gaugeColor = gaugeStatus ? (STATUS_COLOR[gaugeStatus] || '#6b7db3') : (gaugeSource && gaugeSource.buffer_pct <= 0 ? '#f43f5e' : '#10d96e');
  const gaugeOptions: Highcharts.Options = {
    chart: { type: 'gauge', height: 260 },
    title: { text: undefined },
    pane: {
      startAngle: -90,
      endAngle: 90,
      background: undefined,
      center: ['50%', '85%'],
      size: '140%',
    },
    yAxis: {
      min: 0,
      max: 100,
      lineWidth: 0,
      tickInterval: 20,
      minorTickInterval: null,
      labels: { distance: 12, style: { fontSize: '9px', color: '#6b7db3' } },
      plotBands: [
        { from: 0, to: SLA_ATRISK_PCT, color: '#f43f5e' },
        { from: SLA_ATRISK_PCT, to: SLA_LONGJOB_PCT, color: '#f59e0b' },
        { from: SLA_LONGJOB_PCT, to: 100, color: '#10d96e' },
      ],
    },
    series: [{
      type: 'gauge',
      name: 'SLA buffer',
      data: [gaugeNeedle],
      dial: { backgroundColor: '#f0f4ff', baseWidth: 4, rearLength: '0%' },
      pivot: { backgroundColor: '#f0f4ff', radius: 5 },
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

  // ── Hour-of-day scheduling contention heatmap, ported from renderHourHeatmap() (app.js) ──
  const hourHeatmapOptions: Highcharts.Options | null = hourHeatmap && hourHeatmap.cells.length > 0 ? {
    chart: { type: 'heatmap', height: Math.max(220, hourHeatmap.sub_apps.length * 26) },
    title: { text: undefined },
    xAxis: { categories: hourHeatmap.hours.map((h) => `${String(h).padStart(2, '0')}:00`), opposite: true },
    yAxis: { categories: hourHeatmap.sub_apps, title: { text: undefined }, reversed: true },
    colorAxis: { min: 0, minColor: 'rgba(34,211,238,0.08)', maxColor: '#22d3ee' },
    legend: { enabled: false },
    tooltip: {
      formatter(this: Highcharts.TooltipFormatterContextObject) {
        const cell = hourHeatmap.cells[this.point.index];
        if (!cell || !cell.count) return 'No jobs';
        return `<b>${cell.sub_app}</b> @ ${String(cell.hour).padStart(2, '0')}:00<br/>${cell.count} job(s), ${cell.total_hrs.toFixed(1)}h total`;
      },
    },
    series: [{
      type: 'heatmap',
      data: hourHeatmap.cells.map((cell) => ({
        x: hourHeatmap.hours.indexOf(cell.hour),
        y: hourHeatmap.sub_apps.indexOf(cell.sub_app),
        value: cell.count,
      })),
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
        {envChip && (
          <span
            className="metric-badge"
            title={envChip.title}
            style={{ color: envChip.color, borderColor: `${envChip.color}40`, background: `${envChip.color}1f`, fontWeight: 700, textTransform: 'uppercase', fontSize: 10 }}
          >
            {envChip.label}
          </span>
        )}
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

      {/* ── PE Audit Coverage Strip — ported from renderBatchCoverageStrip() (app.js) ── */}
      {dataCoverage && (() => {
        const span = dataCoverage.date_span_days || 0;
        const evidenceStatus = span >= 15 ? 'loaded' : span >= 7 ? 'partial' : 'missing';
        const slaType = slaSource?.type || '';
        const slaStatus = slaType === 'sla_matrix' || slaType === 'batch_sla_xlsx' ? 'customer'
          : slaType === 'customer_fallback' ? 'partial' : 'default';
        const confidence = dataCoverage.confidence ?? 0;
        const confStatus = dataCoverage.has_synthetic_timestamps ? 'missing'
          : confidence >= 80 ? 'loaded' : confidence >= 60 ? 'partial' : 'missing';
        const sowMetrics = (sowCompare?.metrics || []).filter((m) => Number(m.sow) > 0 && Number(m.actual) > 0);
        const badgeColors: Record<string, string> = { loaded: '#10d96e', partial: '#f59e0b', customer: '#10d96e', default: '#f59e0b', missing: '#6b7db3' };
        const badge = (label: string, status: string) => (
          <span
            className="metric-badge"
            style={{ color: badgeColors[status], borderColor: `${badgeColors[status]}66`, background: `${badgeColors[status]}1a` }}
          >
            {label}: {status.toUpperCase()}
          </span>
        );
        let sowBadge: React.ReactNode;
        if (sowMetrics.length) {
          const worst = sowMetrics.reduce((a, b) => (Math.abs(Number(b.pct) - 100) > Math.abs(Number(a.pct) - 100) ? b : a));
          const pct = Number(worst.pct);
          const isOver = pct > 110;
          const isCrit = pct > 120;
          const isUnder = pct < 70;
          const col = isCrit || isOver ? '#f43f5e' : isUnder ? '#f59e0b' : pct < 100 ? '#f59e0b' : '#10d96e';
          sowBadge = (
            <span className="metric-badge" title={`${worst.label}: ${worst.actual} vs contracted ${worst.sow}`} style={{ color: col, borderColor: `${col}66`, background: `${col}1a` }}>
              Volume vs SOW: {pct.toFixed(0)}% of contract{isCrit ? ' — CRITICAL OVER' : isOver ? ' — OVER' : ''}
            </span>
          );
        } else {
          sowBadge = badge('Volume vs SOW', sowCompare ? 'loaded' : 'missing');
        }
        return (
          <Box display="flex" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
            {badge('15-Day Evidence', evidenceStatus)}
            {badge('SLA Source', slaStatus)}
            {dataCoverage.has_synthetic_timestamps
              ? badge('⛔ SYNTHETIC TIMESTAMPS', 'missing')
              : badge(`Data Quality ${confidence}%`, confStatus)}
            {badge('Waivers', 'missing')}
            {sowBadge}
          </Box>
        );
      })()}

      {/* ── Excluded Jobs manager — ported from renderExcludedJobsPanel() (app.js) ── */}
      {(unifiedExclusions.length > 0 || includedBackJobs.length > 0) && (
        <Box
          style={{ borderRadius: 12, border: '1px solid rgba(45,212,191,.28)', background: 'rgba(45,212,191,.05)', padding: 12, marginBottom: 12 }}
        >
          <Box
            display="flex" alignItems="center" justifyContent="space-between" style={{ gap: 12, flexWrap: 'wrap', cursor: 'pointer' }}
            onClick={() => setExcludedDetailOpen((open) => !open)}
          >
            <Box display="flex" alignItems="center" style={{ gap: 12 }}>
              <span style={{ fontSize: 22, fontWeight: 800, color: '#2dd4bf' }}>⊘ {unifiedExclusions.length}</span>
              <Box>
                <Typography variant="subtitle2">Excluded Jobs</Typography>
                <Typography variant="caption" color="textSecondary">
                  {allMetricsCount} all-metrics · {complianceOnlyCount} compliance-only · {byCat.size} categor{byCat.size === 1 ? 'y' : 'ies'}
                  {manualCount ? ` · ${manualCount} chosen by you` : ''}
                </Typography>
              </Box>
            </Box>
            <Box display="flex" style={{ gap: 6, flexWrap: 'wrap' }}>
              {Array.from(byCat.entries()).sort((a, b) => b[1] - a[1]).map(([cat, count]) => {
                const tone = _exclusionCategoryTone(cat);
                return (
                  <span key={cat} className="metric-badge" style={{ color: tone, borderColor: `${tone}47`, background: `${tone}1f` }}>
                    {cat} · {count}
                  </span>
                );
              })}
              <Typography variant="caption" style={{ color: '#2dd4bf', fontWeight: 700 }}>{excludedDetailOpen ? 'Hide manager ▲' : 'Manage ▾'}</Typography>
            </Box>
          </Box>

          {excludedDetailOpen && (
            <Box style={{ marginTop: 10, paddingTop: 10, borderTop: '1px solid rgba(33,48,96,.3)' }}>
              <Box display="flex" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
                <input
                  type="text" placeholder="Filter excluded jobs…" value={excludedFilter}
                  onChange={(e) => setExcludedFilter(e.target.value)}
                  style={{ fontSize: 11.5, padding: '4px 8px', borderRadius: 6, background: 'rgba(17,29,54,.6)', border: '1px solid #213060', color: '#f0f4ff', minWidth: 160 }}
                />
                <input
                  type="text" placeholder="Exclude a job by name…" value={addJobName}
                  onChange={(e) => setAddJobName(e.target.value)}
                  style={{ fontSize: 11.5, padding: '4px 8px', borderRadius: 6, background: 'rgba(17,29,54,.6)', border: '1px solid #213060', color: '#f0f4ff', minWidth: 180 }}
                />
                <input
                  type="text" placeholder="Why? (optional)" value={addJobReason}
                  onChange={(e) => setAddJobReason(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') handleAddManualExclude(); }}
                  style={{ fontSize: 11.5, padding: '4px 8px', borderRadius: 6, background: 'rgba(17,29,54,.6)', border: '1px solid #213060', color: '#f0f4ff', minWidth: 200 }}
                />
                <Button size="small" variant="outlined" onClick={handleAddManualExclude} disabled={exclusionsBusy}>＋ Exclude</Button>
                <Box display="flex" style={{ marginLeft: 'auto', gap: 8 }}>
                  <Button size="small" onClick={handleResetAllExclusions} disabled={exclusionsBusy} style={{ color: '#f43f5e' }}>Reset all</Button>
                  <Button
                    size="small"
                    onClick={() => {
                      const blob = new Blob([_excludedJobsCsv(unifiedExclusions)], { type: 'text/csv' });
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement('a');
                      a.href = url; a.download = `excluded_jobs_${new Date().toISOString().slice(0, 10)}.csv`; a.click();
                      URL.revokeObjectURL(url);
                    }}
                    style={{ color: '#22d3ee' }}
                  >
                    ⭳ CSV
                  </Button>
                </Box>
              </Box>
              <Table size="small" className="pe-table" aria-label="Excluded jobs table">
                <TableHead>
                  <TableRow>
                    <TableCell>Job</TableCell>
                    <TableCell>Category</TableCell>
                    <TableCell>Why excluded</TableCell>
                    <TableCell>Scope</TableCell>
                    <TableCell align="right">Action</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {filteredExclusions.length === 0 ? (
                    <TableRow><TableCell colSpan={5} align="center">No excluded jobs match the current filter.</TableCell></TableRow>
                  ) : filteredExclusions.map((row) => {
                    const tone = _exclusionCategoryTone(row.category);
                    return (
                      <TableRow key={row.name}>
                        <TableCell style={{ fontFamily: 'monospace', fontWeight: 700 }}>{row.name}</TableCell>
                        <TableCell><span className="metric-badge" style={{ color: tone, borderColor: `${tone}47`, background: `${tone}1a` }}>{row.category}</span></TableCell>
                        <TableCell style={{ fontSize: 11.5 }}>{row.why}</TableCell>
                        <TableCell>
                          <span className="metric-badge" style={row.scope === 'ALL_METRICS'
                            ? { color: '#f59e0b', background: 'rgba(245,158,11,.12)' }
                            : { color: '#22d3ee', background: 'rgba(34,211,238,.1)' }}>
                            {row.scope === 'ALL_METRICS' ? 'ALL METRICS' : 'COMPLIANCE ONLY'}
                          </span>
                        </TableCell>
                        <TableCell align="right">
                          {row.isUtil ? (
                            <Button size="small" disabled={exclusionsBusy} onClick={() => handleReinclude(row.name)} style={{ color: '#10d96e' }}>↩ Re-include</Button>
                          ) : (
                            <Typography variant="caption" style={{ fontStyle: 'italic', color: '#6b7db3' }}>needs ≥3 runs</Typography>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
              {includedBackJobs.length > 0 && (
                <Box display="flex" alignItems="center" style={{ gap: 6, flexWrap: 'wrap', marginTop: 8, paddingTop: 8, borderTop: '1px solid rgba(33,48,96,.2)' }}>
                  <Typography variant="caption" style={{ fontWeight: 700, color: '#6b7db3' }}>↩ Manually re-included ({includedBackJobs.length}):</Typography>
                  {includedBackJobs.map((j) => (
                    <Button key={j.Job_Name} size="small" onClick={() => handleReexclude(j.Job_Name)} style={{ color: '#10d96e', fontFamily: 'monospace', fontSize: 10.5 }}>
                      {j.Job_Name} ✕
                    </Button>
                  ))}
                </Box>
              )}
            </Box>
          )}
        </Box>
      )}

      {/* ── Concurrent Jobs Evidence — ported from renderBatchConcurrencyEvidence() (app.js) ── */}
      {concurrency && concurrency.groups.length > 0 && (
        <Box style={{ borderRadius: 8, border: '1px solid rgba(34,211,238,.28)', background: 'rgba(34,211,238,.04)', padding: 10, marginBottom: 12 }}>
          <Box display="flex" alignItems="center" justifyContent="space-between" style={{ flexWrap: 'wrap' }}>
            <Typography variant="caption" style={{ color: '#22d3ee', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em' }}>Concurrent Jobs</Typography>
            <Typography variant="caption" color="textSecondary">{concurrency.total_days_with_concurrency} affected day(s)</Typography>
          </Box>
          <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>
            Ranked by peak simultaneity — the most jobs running at the same instant. Severity is the tercile of peak across these groups.
          </Typography>
          {concurrency.groups.slice(0, 10).map((group, idx) => {
            const sev = _concSeverityTheme(group.severity_level);
            return (
              <Box key={`${group.example_sub_app}-${idx}`} style={{ borderRadius: 8, border: `1px solid ${sev.color}47`, background: `${sev.color}0d`, marginTop: 6, padding: '6px 10px' }}>
                <Box display="flex" alignItems="center" style={{ gap: 10, flexWrap: 'wrap' }}>
                  <Typography variant="body2" style={{ flex: 1, minWidth: 120, fontWeight: 600 }}>{group.example_sub_app}</Typography>
                  <span style={{ fontSize: 16, fontWeight: 800, color: sev.color }}>{group.peak_concurrent}</span>
                  <Typography variant="caption" style={{ color: sev.color, textTransform: 'uppercase', fontSize: 9 }}>peak</Typography>
                  <span className="metric-badge" style={{ color: sev.color, borderColor: `${sev.color}47`, background: `${sev.color}1a` }}>{sev.label}</span>
                  <Typography variant="caption" color="textSecondary">{group.days_seen}d · {group.occurrences} occ</Typography>
                </Box>
                <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>
                  {_concExplainSentence(group)}
                </Typography>
                {group.bursts.slice(0, 3).map((burst, bIdx) => (
                  <Box key={bIdx} display="flex" style={{ gap: 8, marginTop: 2 }}>
                    <Typography variant="caption" style={{ fontFamily: 'monospace', color: '#6b7db3', width: 70 }}>{burst.run_date}</Typography>
                    <Typography variant="caption" style={{ fontFamily: 'monospace', color: '#f0f4ff', width: 90 }}>{burst.start_clock}–{burst.end_clock}</Typography>
                    <Typography variant="caption" style={{ fontWeight: 700, color: sev.color }}>peak {burst.peak_concurrent}</Typography>
                    <Typography variant="caption" color="textSecondary">· {burst.job_count} in window</Typography>
                  </Box>
                ))}
              </Box>
            );
          })}
        </Box>
      )}

      {/* ── Benchmark cross-reference callout — ported from _renderBatchBenchmarkXref() (app.js) ── */}
      {benchmarkPerf && (() => {
        const bps = benchmarkPerf as Record<string, number>;
        const regr = Number(bps.regressions) || 0;
        const impr = Number(bps.improvements) || 0;
        const comp = Number(bps.comparable) || 0;
        const netSecs = Number(bps.net_delta_secs) || 0;
        const hasReg = regr > 0;
        const netDir = netSecs >= 0 ? 'saved' : 'added';
        const netMin = (Math.abs(netSecs) / 60).toFixed(1);
        const color = hasReg ? '#f59e0b' : '#10d96e';
        return (
          <Box display="flex" alignItems="center" justifyContent="space-between" style={{ gap: 12, flexWrap: 'wrap', borderRadius: 12, border: `1px solid ${color}4d`, background: `${color}0a`, padding: '10px 14px', marginBottom: 12 }}>
            <Box>
              <Typography variant="caption" style={{ color, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' }}>
                {hasReg ? '⚠️' : '✅'} Batch Runtime Comparison loaded
              </Typography>
              <Typography variant="caption" color="textSecondary" style={{ display: 'block' }}>
                {comp} jobs compared · <span style={{ color: hasReg ? '#f43f5e' : '#10d96e' }}>{regr} regression(s)</span> · {impr} improvement(s) · net {netMin} min {netDir}/run
              </Typography>
              <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 9.5 }}>
                This data feeds PE Findings — regression analysis is separate from Ctrl-M SLA compliance above.
              </Typography>
            </Box>
          </Box>
        );
      })()}

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

      {narrative && (
        <Box
          className={classes.chart}
          style={{
            borderRadius: 16,
            border: `1px solid ${narrative.tone === 'ok' ? 'rgba(16,217,110,.35)' : narrative.tone === 'critical' ? 'rgba(244,63,94,.35)' : 'rgba(245,158,11,.35)'}`,
            background: `linear-gradient(135deg, ${narrative.tone === 'ok' ? 'rgba(16,217,110,.07)' : narrative.tone === 'critical' ? 'rgba(244,63,94,.07)' : 'rgba(245,158,11,.07)'}, rgba(17,29,54,.45))`,
            padding: 16,
          }}
        >
          <Typography variant="caption" style={{ textTransform: 'uppercase', letterSpacing: '.1em', color: '#6b7db3', fontWeight: 700 }}>
            Batch SLA — the headline question
          </Typography>
          <Typography variant="subtitle1" style={{ marginTop: 4 }}>Are we meeting batch SLAs?</Typography>
          <Typography
            variant="body2"
            style={{ marginTop: 4, fontWeight: 700, color: narrative.tone === 'ok' ? '#10d96e' : narrative.tone === 'critical' ? '#f43f5e' : '#f59e0b' }}
          >
            {narrative.breachDays === 0
              ? `Yes — every one of the ${narrative.total} day(s) finished inside the window (${narrative.compliancePct.toFixed(0)}% day compliance).`
              : `${narrative.cleanDays}/${narrative.total} day(s) finished inside the window — ${narrative.compliancePct.toFixed(0)}% day compliance, ${narrative.breachDays} breach${narrative.breachDays > 1 ? 'es' : ''}.`}
          </Typography>
          {narrative.worstBreach && (
            <Typography variant="body2" style={{ marginTop: 8, color: '#f0f4ff' }}>
              Worst breach: {narrative.worstBreach.run_date} ran {narrative.worstBreach.total_hrs.toFixed(2)}h
              {narrative.worstBreach.top_job && `; longest job ${narrative.worstBreach.top_job}.`}
            </Typography>
          )}
        </Box>
      )}


      {(window.length > 0 || gaugeSource) && (
        <Box className={classes.chart} style={{ display: 'grid', gridTemplateColumns: gaugeSource ? '1fr 2fr' : '1fr', gap: 16 }}>
          {gaugeSource && (
            <Box className="chart-panel" style={{ padding: 16, position: 'relative' }}>
              <Typography variant="subtitle2">SLA Buffer Gauge</Typography>
              <Typography variant="caption" color="textSecondary">Headroom between worst-job peak and the SLA ceiling</Typography>
              <Box style={{ position: 'relative' }}>
                <HighchartsReact highcharts={Highcharts} options={gaugeOptions} />
                <Box
                  style={{
                    position: 'absolute', left: '50%', bottom: 26, transform: 'translateX(-50%)',
                    textAlign: 'center', pointerEvents: 'none',
                  }}
                >
                  <div style={{ fontSize: 22, fontWeight: 800, color: gaugeColor }}>{gaugeSource.buffer_pct.toFixed(0)}%</div>
                  {gaugeStatus && <div style={{ fontSize: 10, color: '#6b7db3', textTransform: 'uppercase' }}>{gaugeStatus.replace('_', ' ')}</div>}
                </Box>
              </Box>
              {worstJob && (
                <Typography variant="caption" style={{ display: 'block', textAlign: 'center', color: '#6b7db3' }}>
                  {worstJob.job_name} · {worstJob.peak_hrs.toFixed(2)}h vs {worstJob.sla_hrs.toFixed(2)}h ceiling
                  {gaugeSource.sla_source === 'adaptive' && ' · adaptive baseline'}
                </Typography>
              )}
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

      {hourHeatmapOptions && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">Hour-of-Day Scheduling Contention</Typography>
          <Typography variant="caption" color="textSecondary">Sub-application × hour — bright cells are peak scheduling contention windows.</Typography>
          <HighchartsReact highcharts={Highcharts} options={hourHeatmapOptions} />
        </Box>
      )}

      {longpole && longpole.has_data && longpole.rows.length > 0 && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">Long-Pole Job Consistency</Typography>
          <Typography variant="caption" color="textSecondary">
            Longest jobs by average runtime — read across a row to see if the same job is slow every day or only spikes.
          </Typography>
          <Table size="small" className="pe-table" aria-label="Long-pole job consistency table" style={{ marginTop: 8 }}>
            <TableHead>
              <TableRow>
                <TableCell>Job</TableCell>
                <TableCell align="right">Avg (min)</TableCell>
                <TableCell align="right">Max (min)</TableCell>
                <TableCell align="right">Runs</TableCell>
                <TableCell align="right">Days present</TableCell>
                <TableCell align="right">Spike ratio</TableCell>
                <TableCell>Stability</TableCell>
                <TableCell>Long-pole</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {longpole.rows.map((row) => (
                <TableRow key={row.job}>
                  <TableCell>{row.job}</TableCell>
                  <TableCell align="right">{row.avg_min.toFixed(1)}</TableCell>
                  <TableCell align="right">{row.max_min.toFixed(1)}</TableCell>
                  <TableCell align="right">{row.runs}</TableCell>
                  <TableCell align="right">{row.days_present}/{row.days_total}</TableCell>
                  <TableCell align="right" style={{ color: row.spike_ratio > 2.5 ? '#f43f5e' : row.spike_ratio > 1.5 ? '#f59e0b' : '#10d96e' }}>
                    {row.spike_ratio.toFixed(2)}x
                  </TableCell>
                  <TableCell style={{ textTransform: 'capitalize' }}>{row.stability}</TableCell>
                  <TableCell>
                    {row.is_longpole && <span className="metric-badge metric-badge-amber">LONG-POLE</span>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
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
                  <TableCell>
                    <span className="metric-badge" style={{ color: STATUS_COLOR[job.buffer_status] || '#6b7db3', borderColor: `${STATUS_COLOR[job.buffer_status] || '#6b7db3'}40`, background: `${STATUS_COLOR[job.buffer_status] || '#6b7db3'}1f` }}>
                      {job.buffer_status}
                    </span>
                  </TableCell>
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
                <TableCell align="right">SLA hrs</TableCell>
                <TableCell align="right">Buffer %</TableCell>
                <TableCell align="right">SLA used</TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {topBreaches.slice(0, 9).map((job, index) => {
                const buffer = job.buffer_pct;
                const color = STATUS_COLOR[job.buffer_status] || '#6b7db3';
                return (
                  <TableRow key={`${job.Job_Name}-${index}`}>
                    <TableCell>
                      {job.Job_Name}
                      {job.concurrent_job_count != null && job.concurrent_job_count > 0 && (
                        <Typography variant="caption" style={{ display: 'block', color: '#6b7db3' }}>
                          {job.concurrent_job_count} concurrent · {(job.concurrent_overlap_hrs || 0).toFixed(1)}h overlap
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell>{job.Sub_Application || '—'}</TableCell>
                    <TableCell align="right">{job.peak_hrs.toFixed(2)}</TableCell>
                    <TableCell align="right">{job.avg_hrs.toFixed(2)}</TableCell>
                    <TableCell align="right">{job.sla_hrs != null ? job.sla_hrs.toFixed(2) : '—'}</TableCell>
                    <TableCell align="right" style={{ color: buffer != null && buffer < 0 ? '#f43f5e' : '#f59e0b' }}>
                      {buffer != null ? `${buffer.toFixed(1)}%` : '—'}
                    </TableCell>
                    <TableCell align="right">{job.sla_used_pct != null ? `${job.sla_used_pct.toFixed(0)}%` : '—'}</TableCell>
                    <TableCell>
                      <span className="metric-badge" style={{ color, borderColor: `${color}40`, background: `${color}1f` }}>
                        {job.buffer_status}
                      </span>
                    </TableCell>
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
