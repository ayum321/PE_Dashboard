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
  window_day_compliance_pct?: number;
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
  effective_hrs?: number;
  elapsed_hrs?: number;
  min_buffer_pct?: number | null;
  active_busy_hrs?: number;
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
  resolved_ceilings?: number[];
  resolved_ceiling_min?: number;
  resolved_ceiling_max?: number;
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
  job_priority?: Record<string, { priority?: string; score?: number; reason?: string }>;
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

/** teal (short) -> amber (longer) -> red (longest run) by share of matrix max, ported from
 * renderLongpoleHeatmap()'s _cellStyle() (app.js). */
function _longpoleCellStyle(mins: number, maxMin: number): React.CSSProperties {
  if (!mins) return { background: '#0f172a', border: '1px solid rgba(255,255,255,.05)' };
  const t = Math.min(1, mins / maxMin);
  const base = t < 0.5 ? '45,212,191' : t < 0.8 ? '245,158,11' : '244,63,94';
  const intensity = (0.30 + 0.6 * t).toFixed(2);
  return { background: `rgba(${base},${intensity})`, border: `1px solid rgba(${base},.8)` };
}

/** "2026-06-01" -> "06/01", ported from renderLongpoleHeatmap()'s _short() (app.js). */
function _shortDate(d: string): string {
  const m = String(d).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[2]}/${m[3]}` : String(d).slice(-5);
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
  max_minutes?: number;
  busy_ref_hrs?: number;
  share_pct_flag?: number;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  kpiRow: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2) },
  kpi: { padding: theme.spacing(1.5), minWidth: 130 },
  chart: { marginTop: theme.spacing(3) },
  empty: { marginTop: theme.spacing(2) },
}));

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
  const [manualInclude, setManualInclude] = useState<Set<string>>(new Set());
  const [manualExcludeReasons, setManualExcludeReasons] = useState<Map<string, string>>(new Map());
  const [manualExclude, setManualExclude] = useState<Set<string>>(new Set());
  const [excludedDetailOpen, setExcludedDetailOpen] = useState(false);
  const [excludedFilter, setExcludedFilter] = useState('');
  const [addJobName, setAddJobName] = useState('');
  const [addJobReason, setAddJobReason] = useState('');
  const [exclusionsBusy, setExclusionsBusy] = useState(false);

  const kpis = (data.batch?.kpis || {}) as BatchKpis;
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
  // Day-level window compliance (calendar days where every in-scope sub-app finished
  // within its ceiling) is the canonical headline metric — identical to the Executive
  // Dashboard/PE Findings. It must be tried BEFORE the pair-level window_compliance_pct
  // (sub-app × day), which is a different, usually-higher number.
  const windowCompliance = kpis.window_day_compliance_pct ?? kpis.batch_window_compliance ?? kpis.window_compliance_pct ?? 0;
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
      const reason = j.utility_reason || 'utility pattern';
      rows.set(name.toUpperCase(), {
        name,
        // Real category is the uppercased raw reason token (e.g. RUNTIME_GATED:EXPORT,
        // STRONG_UTILITY:FILE_WATCHER) — NOT a generic "UTILITY" label. Verified against
        // the real dashboard's _exclusionCategory() output on live Dawnfoods data.
        category: _exclusionCategory(reason),
        why: customReason || _exclusionWhy(reason),
        scope: 'ALL_METRICS',
        isUtil: true,
      });
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

  // ── Effective Window KPI \u2014 the SLA-binding LONGEST CONTIGUOUS block per day
  // (window[].effective_hrs), NOT the first\u2192last elapsed SPAN (window[].elapsed_hrs),
  // which is mostly idle gaps on spread/sequenced batches. Ported from the effRows
  // logic in renderBatchLayerCards() (app.js) \u2014 falls back to elapsed_window.worst_day
  // only when the per-day window[] array is unavailable. ──
  const effectiveWindowDisplay = useMemo(() => {
    const effRows = window
      .map((w) => ({
        date: w.run_date,
        eff: Number(w.effective_hrs ?? w.elapsed_hrs ?? w.total_hrs ?? 0),
        span: Number(w.elapsed_hrs ?? 0),
        breach: !!w.breach,
      }))
      .filter((r) => r.date && r.eff > 0);
    if (effRows.length) {
      const worst = [...effRows].sort((a, b) => b.eff - a.eff)[0];
      const avgEff = effRows.reduce((sum, r) => sum + r.eff, 0) / effRows.length;
      const spanTxt = worst.span > worst.eff + 0.05 ? ` \u00b7 span ${worst.span.toFixed(1)}h` : '';
      return {
        value: `${worst.eff.toFixed(1)}h`,
        sub: `Worst day: ${worst.date} \u00b7 Avg ${avgEff.toFixed(1)}h${spanTxt}`,
        breach: worst.breach,
      };
    }
    if (elapsedWindow?.available && elapsedWindow.worst_day) {
      return {
        value: `${Number(elapsedWindow.worst_day.elapsed_hrs || 0).toFixed(1)}h`,
        sub: `Worst day: ${elapsedWindow.worst_day.run_date || ''} \u00b7 Avg ${Number(elapsedWindow.avg_elapsed_hrs || 0).toFixed(1)}h`,
        breach: false,
      };
    }
    return null;
  }, [window, elapsedWindow]);

  // ── Pattern Detection — statistical z-score spike detection on window values,
  // ported from the spikeIdxs computation in renderWindowTrendChart() (app.js). A day
  // is anomalous if z > 2.0, or z > 1.5 while also a breach day. ──
  const patternDetection = useMemo(() => {
    const rawEff = window.map((w) => Number(w.effective_hrs) || 0);
    const rawElapsed = window.map((w) => Number(w.elapsed_hrs) || 0);
    const rawSums = window.map((w) => Number(w.total_hrs) || 0);
    const hasEff = rawEff.some((v) => v > 0);
    const hasElapsed = rawElapsed.some((v) => v > 0);
    const values = window.map((_, i) => (hasEff && rawEff[i] > 0 ? rawEff[i] : hasElapsed && rawElapsed[i] > 0 ? rawElapsed[i] : rawSums[i]));
    if (!values.length) return [];
    const mean = values.reduce((s, v) => s + v, 0) / values.length;
    const std = Math.sqrt(values.reduce((s, v) => s + (v - mean) ** 2, 0) / values.length);
    const items: { date: string; val: number; count: number; type: 'breach' | 'spike'; top: string }[] = [];
    window.forEach((w, i) => {
      const z = std > 0 ? (values[i] - mean) / std : 0;
      const isSpike = z > 2.0 || (z > 1.5 && w.breach);
      if (isSpike || w.breach) {
        items.push({ date: w.run_date, val: values[i], count: w.job_count, type: w.breach ? 'breach' : 'spike', top: w.top_job || '' });
      }
    });
    return items;
  }, [window]);

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

  // ── Multiple distinct SLA ceilings in scope (e.g. an SLA matrix resolving
  // several customer windows across different sub-apps) — draw one dashed
  // reference line per distinct ceiling instead of a single line, ported from
  // slaLinePlugin()'s dedup logic (app.js). Falls back to the single dominant
  // ceiling when only one (or no) customer-sourced ceiling is in scope. ──
  const windowChartCeilings = useMemo(() => {
    const raw = (slaSource?.resolved_ceilings || []).filter((v) => Number.isFinite(v) && v > 0);
    if (raw.length <= 1) return [slaCeilingHrs];
    const seen = new Set<string>();
    const list: number[] = [];
    for (const v of raw) {
      const key = v.toFixed(2);
      if (seen.has(key)) continue;
      seen.add(key);
      list.push(v);
    }
    return list.sort((a, b) => a - b);
  }, [slaSource, slaCeilingHrs]);

  const chartOptions: Highcharts.Options = {
    chart: { type: 'column', height: 280 },
    title: { text: undefined },
    xAxis: { categories: window.map((point) => point.run_date) },
    yAxis: {
      title: { text: 'Total hours' },
      plotLines: windowChartCeilings.map((ceil, idx) => ({
        value: ceil,
        color: '#f59e0b',
        dashStyle: 'Dash' as const,
        width: 2,
        zIndex: 4,
        label: {
          text: windowChartCeilings.length > 1 ? `${ceil}h` : `SLA ceiling ${ceil}h`,
          align: (idx % 2 === 0 ? 'left' : 'right') as 'left' | 'right',
          style: { color: '#f59e0b', fontSize: '10px' },
        },
      })),
    },
    tooltip: {
      formatter(this: Highcharts.TooltipFormatterContextObject) {
        const point = window[this.point.index];
        if (!point) return `${this.x}: ${this.y}h`;
        return `<b>${point.run_date}</b><br/>${point.total_hrs.toFixed(2)}h across ${point.job_count} job(s)` +
          (point.top_job ? `<br/>Top job: ${point.top_job}` : '') +
          ((point.active_busy_hrs || 0) > 0 ? `<br/>Active busy time: ${point.active_busy_hrs!.toFixed(2)}h (real compute \u2014 overlaps counted once)` : '') +
          (point.breach ? '<br/><span style="color:#f43f5e">BREACH</span>' : '');
      },
    },
    series: [
      {
        type: 'column',
        name: 'Daily batch hours',
        data: window.map((point) => {
          // Prefer the day's OWN tightest sub-app buffer (min_buffer_pct) when the
          // backend supplies it, so the bar colour reflects the real per-day
          // ceiling instead of always the single global default — mirrors
          // vanilla's _dayBand() (app.js).
          let band: 'red' | 'amber' | 'green';
          if (point.breach) {
            band = 'red';
          } else {
            const bufPct = point.min_buffer_pct != null && Number.isFinite(point.min_buffer_pct)
              ? point.min_buffer_pct
              : (slaCeilingHrs > 0 ? ((slaCeilingHrs - point.total_hrs) / slaCeilingHrs) * 100 : 100);
            band = bufPct <= SLA_ATRISK_PCT ? 'red' : bufPct <= SLA_LONGJOB_PCT ? 'amber' : 'green';
          }
          return { y: point.total_hrs, color: band === 'red' ? '#f43f5e' : band === 'amber' ? '#f59e0b' : '#10d96e' };
        }),
      },
      // Active busy time (interval union) overlaid as a teal line so the gap
      // between an inflated elapsed span and real compute time is visible at
      // a glance — ported from renderWindowTrendChart()'s teal dataset (app.js).
      ...(window.some((p) => (p.active_busy_hrs || 0) > 0) ? [{
        type: 'area' as const,
        name: 'Active busy time (h)',
        data: window.map((p) => p.active_busy_hrs || 0),
        color: 'rgba(45,212,191,0.95)',
        fillOpacity: 0.12,
        lineWidth: 2,
        marker: { radius: 2.5, fillColor: 'rgba(45,212,191,1)' },
        }] : []),
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

  // ── SLA Compliance Heatmap \u2014 ported from renderSlaHeatmap() (app.js) as a
  // plain HTML table with DISCRETE 4-band status coloring, NOT a Highcharts
  // heatmap with a continuous colorAxis. The continuous ratio scale (0\u20131+)
  // is what produced the "hazy"/inconsistent look \u2014 vanilla never used a
  // gradient here, it uses the exact same buffer-band colors as the gauge
  // and daily-window bars (green/amber/red/dark-green-no-run). ──
  const slaHeatmapRows = useMemo(() => {
    if (!slaHeatmap || !slaHeatmap.cells.length) return null;
    const lookup = new Map<string, SlaHeatmapCell>();
    for (const c of slaHeatmap.cells) lookup.set(`${c.job}||${c.date}`, c);
    const jobPriority = slaHeatmap.job_priority || {};
    const priorityRank = (p?: string) => (p === 'critical' ? 2 : p === 'warning' ? 1 : 0);
    const orderedJobs = [...slaHeatmap.jobs].sort((a, b) => {
      const ma = jobPriority[a] || {};
      const mb = jobPriority[b] || {};
      const pa = priorityRank(ma.priority);
      const pb = priorityRank(mb.priority);
      if (pa !== pb) return pb - pa;
      const sa = typeof ma.score === 'number' ? ma.score : 0;
      const sb = typeof mb.score === 'number' ? mb.score : 0;
      if (sb !== sa) return sb - sa;
      return a.localeCompare(b);
    });
    return { lookup, orderedJobs, jobPriority };
  }, [slaHeatmap]);

  const spikeDateSet = useMemo(() => new Set(patternDetection.map((p) => p.date)), [patternDetection]);

  const slaCellColor = (cell?: SlaHeatmapCell): string => {
    if (!cell || cell.hrs == null) return '#0f3d24';
    const ceiling = cell.sla_limit ?? slaHeatmap?.limit ?? 6;
    if (cell.breach) return '#f43f5e';
    if (cell.hrs > ceiling * 0.85) return '#f59e0b';
    return '#10d96e';
  };
  const slaCellTitle = (cell?: SlaHeatmapCell): string => {
    if (!cell || cell.hrs == null) return 'No run';
    const ceiling = cell.sla_limit ?? slaHeatmap?.limit ?? 6;
    return `${cell.hrs.toFixed(2)}h \u2014 ${cell.breach ? 'BREACH' : 'OK'} (SLA: ${ceiling.toFixed(2)}h)`;
  };
  const shortDate = (d: string): string => {
    const parts = String(d).split(/[-/]/);
    return parts.length === 3 ? `${parts[1]}-${parts[2]}` : String(d).slice(-5);
  };

  // ── Ctrl-M Execution Density Heatmap \u2014 ported from renderHourHeatmap()
  // (app.js), also a plain HTML table (continuous cyan intensity IS correct
  // here \u2014 this one measures a count, not a status band). ──
  const hourHeatmapRows = useMemo(() => {
    if (!hourHeatmap || !hourHeatmap.cells.length) return null;
    const lookup = new Map<string, HourHeatmapCell>();
    let maxCount = 1;
    for (const c of hourHeatmap.cells) {
      lookup.set(`${c.sub_app}||${c.hour}`, c);
      if ((c.count || 0) > maxCount) maxCount = c.count;
    }
    const hours = hourHeatmap.hours.length ? hourHeatmap.hours : Array.from({ length: 24 }, (_, i) => i);
    return { lookup, maxCount, hours };
  }, [hourHeatmap]);

  const hourCellBg = (cell: HourHeatmapCell | undefined, maxCount: number): string => {
    if (!cell || !cell.count) return 'rgba(33,48,96,.25)';
    const t = Math.min(cell.count / maxCount, 1);
    return `rgba(34,211,238,${(0.10 + t * 0.80).toFixed(2)})`;
  };
  const hourCellTitle = (cell: HourHeatmapCell | undefined, hour: number): string => {
    if (!cell || !cell.count) return 'No jobs';
    return `${cell.sub_app} @ ${String(hour).padStart(2, '0')}:00: ${cell.count} job(s), ${(cell.total_hrs || 0).toFixed(1)}h total`;
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

      {/* ── Batch Story — the headline narrative, ported from renderBatchStory() (app.js).
          Answers "Are we meeting batch SLAs?" FIRST, before the coverage strip/KPI
          grid, matching vanilla's order. ── */}
      {narrative && (
        <Box
          style={{
            borderRadius: 16,
            border: `1px solid ${narrative.tone === 'ok' ? 'rgba(16,217,110,.35)' : narrative.tone === 'critical' ? 'rgba(244,63,94,.35)' : 'rgba(245,158,11,.35)'}`,
            background: `linear-gradient(135deg, ${narrative.tone === 'ok' ? 'rgba(16,217,110,.07)' : narrative.tone === 'critical' ? 'rgba(244,63,94,.07)' : 'rgba(245,158,11,.07)'}, rgba(17,29,54,.45))`,
            padding: 16,
            marginTop: 8,
            marginBottom: 12,
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

      <Box className={classes.kpiRow} style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12 }}>
        {effectiveWindowDisplay && (
          <KpiStatCard
            label="Effective Window"
            value={effectiveWindowDisplay.value}
            sub={effectiveWindowDisplay.sub}
            valueColor={effectiveWindowDisplay.breach ? '#f43f5e' : '#a855f7'}
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

      {/* ── Excluded Jobs manager — ported from renderExcludedJobsPanel() (app.js). Sits
          directly under the KPI strip, matching vanilla's insertion point exactly. ── */}
      {(unifiedExclusions.length > 0 || includedBackJobs.length > 0) && (
        <Box
          style={{ borderRadius: 12, border: '1px solid rgba(45,212,191,.28)', background: 'rgba(45,212,191,.05)', padding: 12, marginTop: 12, marginBottom: 12 }}
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

      {/* ── Concurrent Jobs Evidence — ported from renderBatchConcurrencyEvidence() (app.js).
          Each group card is a native <details>/<summary> — collapsed by default, click to
          reveal occurrence rows — matching vanilla's show/hide affordance exactly (previously
          always rendered fully expanded with no way to hide it). ── */}
      {concurrency && concurrency.groups.length > 0 && (
        <Box style={{ borderRadius: 8, border: '1px solid rgba(34,211,238,.28)', background: 'rgba(34,211,238,.04)', padding: 10, marginBottom: 12 }}>
          <Box display="flex" alignItems="center" justifyContent="space-between" style={{ flexWrap: 'wrap' }}>
            <Typography variant="caption" style={{ color: '#22d3ee', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em' }}>Concurrent Jobs</Typography>
            <Typography variant="caption" color="textSecondary">{concurrency.total_days_with_concurrency} affected day(s)</Typography>
          </Box>
          <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>
            Ranked by peak simultaneity — the most jobs running at the same instant. Severity is the tercile of peak across these groups. Click a row for occurrence detail.
          </Typography>
          {concurrency.groups.slice(0, 10).map((group, idx) => {
            const sev = _concSeverityTheme(group.severity_level);
            return (
              <details key={`${group.example_sub_app}-${idx}`} style={{ borderRadius: 8, border: `1px solid ${sev.color}47`, background: `${sev.color}0d`, marginTop: 6 }}>
                <summary style={{ cursor: 'pointer', listStyle: 'none', padding: '6px 10px' }}>
                  <Box display="flex" alignItems="center" style={{ gap: 10, flexWrap: 'wrap' }}>
                    <Typography component="span" variant="body2" style={{ flex: 1, minWidth: 120, fontWeight: 600 }}>{group.example_sub_app}</Typography>
                    <span style={{ fontSize: 16, fontWeight: 800, color: sev.color }}>{group.peak_concurrent}</span>
                    <Typography component="span" variant="caption" style={{ color: sev.color, textTransform: 'uppercase', fontSize: 9 }}>peak</Typography>
                    <span className="metric-badge" style={{ color: sev.color, borderColor: `${sev.color}47`, background: `${sev.color}1a` }}>{sev.label}</span>
                    <Typography component="span" variant="caption" color="textSecondary">{group.days_seen}d · {group.occurrences} occ</Typography>
                  </Box>
                </summary>
                <Box style={{ padding: '0 10px 8px' }}>
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
              </details>
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
              <Box display="flex" style={{ gap: 10, flexWrap: 'wrap', justifyContent: 'center', marginTop: 8 }}>
                <Typography variant="caption" style={{ display: 'flex', alignItems: 'center', gap: 4, color: '#10d96e' }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#10d96e', display: 'inline-block' }} /> Healthy · &gt;40% buffer
                </Typography>
                <Typography variant="caption" style={{ display: 'flex', alignItems: 'center', gap: 4, color: '#f59e0b' }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#f59e0b', display: 'inline-block' }} /> {'Tight \u00b7 15\u201340% buffer'}
                </Typography>
                <Typography variant="caption" style={{ display: 'flex', alignItems: 'center', gap: 4, color: '#f43f5e' }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#f43f5e', display: 'inline-block' }} /> {'At risk / breach \u00b7 \u226415%'}
                </Typography>
                <Typography variant="caption" style={{ color: '#f59e0b' }}>{'\u26a1 unusually long day (statistical spike)'}</Typography>
              </Box>
            </Box>
          )}
          {window.length > 0 && (
            <Box className="chart-panel" style={{ padding: 16 }}>
              <Typography variant="subtitle2">Daily Batch Window</Typography>
              <Typography variant="caption" color="textSecondary">
                Bar color = SLA buffer that day{window.some((p) => (p.active_busy_hrs || 0) > 0) ? ' \u00b7 teal line = active busy time (real compute)' : ''}{windowChartCeilings.length > 1 ? ` \u00b7 dashed lines mark ${windowChartCeilings.length} distinct contracted windows (${windowChartCeilings[0]}\u2013${windowChartCeilings[windowChartCeilings.length - 1]}h)` : ''}
              </Typography>
              <HighchartsReact highcharts={Highcharts} options={chartOptions} />
              {patternDetection.length > 0 && (
                <Box style={{ marginTop: 8, borderRadius: 12, border: '1px solid rgba(245,158,11,.3)', background: 'rgba(245,158,11,.04)', padding: '8px 12px' }}>
                  <Box display="flex" alignItems="center" style={{ gap: 8, marginBottom: 6 }}>
                    <Typography variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em', color: '#f59e0b' }}>Pattern Detection</Typography>
                    <span style={{ fontSize: 8, padding: '2px 6px', borderRadius: 4, color: '#f59e0b', background: 'rgba(245,158,11,.12)' }}>
                      {patternDetection.length} anomalous day{patternDetection.length !== 1 ? 's' : ''}
                    </span>
                  </Box>
                  <Box display="flex" style={{ gap: 6, flexWrap: 'wrap' }}>
                    {patternDetection.map((it) => {
                      const color = it.type === 'breach' ? '#f43f5e' : '#f59e0b';
                      return (
                        <span
                          key={it.date}
                          title={`${it.date}: ${it.val.toFixed(2)}h · ${it.count} jobs${it.top ? ' · ' + it.top : ''}`}
                          style={{ fontSize: 8, fontFamily: 'monospace', padding: '2px 6px', borderRadius: 4, color, background: `${color}1a`, border: `1px solid ${color}4d` }}
                        >
                          {it.type === 'breach' ? '⚠' : '⚡'} {it.date} {it.val.toFixed(1)}h
                        </span>
                      );
                    })}
                  </Box>
                </Box>
              )}
            </Box>
          )}
        </Box>
      )}

      {longpole && longpole.has_data && longpole.rows.length > 0 && (() => {
        const dates = longpole.dates || [];
        const rows = longpole.rows || [];
        const cellMap = new Map<string, number>();
        (longpole.cells || []).forEach((c) => cellMap.set(`${c.job}|${c.date}`, c.minutes));
        const maxMin = Math.max(1, Number(longpole.max_minutes) || 1);
        const busyRef = Number(longpole.busy_ref_hrs) || 0;
        const flagPct = Number(longpole.share_pct_flag) || 25;
        const longpoles = rows.filter((r) => r.is_longpole).length;
        const topRow = rows.reduce((a, b) => (Number(b.window_share_pct) || 0) > (Number(a.window_share_pct) || 0) ? b : a, rows[0]);
        const topShare = Number(topRow?.window_share_pct) || 0;
        let critSentence = 'These jobs define your effective batch critical path — any growth here eats directly into the window buffer.';
        if (topShare > 0 && topRow?.job) {
          critSentence += ` Biggest single contributor: ${topRow.job} at ${topShare.toFixed(0)}% of the ${busyRef.toFixed(1)}h busy window`;
          critSentence += longpoles > 0
            ? ` — ${longpoles} job(s) cross the ${flagPct}% long-pole line (▲), so trimming them frees the most headroom.`
            : '; no single job dominates, so the risk is aggregate concurrency rather than one runaway job.';
        }
        return (
          <Box className={classes.chart}>
            <Box display="flex" alignItems="center" justifyContent="space-between" style={{ flexWrap: 'wrap', gap: 8 }}>
              <Typography variant="subtitle2">Long-Pole Job Consistency</Typography>
              <span className={`metric-badge ${longpoles > 0 ? 'metric-badge-amber' : 'metric-badge-green'}`}>
                {longpoles > 0 ? `${longpoles} long-pole (\u2265${flagPct}% of window)` : 'No single dominating job'}
              </span>
            </Box>
            <Typography variant="caption" color="textSecondary" style={{ display: 'block' }}>
              {rows.length} longest jobs · {dates.length} day(s) · typical busy window {busyRef.toFixed(1)}h · cell = longest run that day (min)
            </Typography>
            <Typography variant="caption" style={{ display: 'block', marginTop: 4, color: '#f59e0b' }}>{critSentence}</Typography>
            <Box style={{ overflowX: 'auto', marginTop: 8 }}>
              <table style={{ borderCollapse: 'separate', borderSpacing: 3 }}>
                <thead>
                  <tr>
                    <th style={{ position: 'sticky', left: 0, textAlign: 'left', fontSize: 10, color: '#6b7db3', paddingRight: 12, paddingBottom: 4, background: '#111d36' }}>Job</th>
                    {dates.map((d) => (
                      <th key={d} title={d} style={{ fontSize: 9, fontFamily: 'monospace', color: '#6b7db3', minWidth: 30, textAlign: 'center', paddingBottom: 4 }}>{_shortDate(d)}</th>
                    ))}
                    <th style={{ fontSize: 9, color: '#6b7db3', paddingLeft: 8, textAlign: 'center' }} title="Average single-run minutes">avg</th>
                    <th style={{ fontSize: 9, color: '#6b7db3', paddingLeft: 4, textAlign: 'center' }} title="Longest single run">max</th>
                    <th style={{ fontSize: 9, color: '#6b7db3', paddingLeft: 4, textAlign: 'center' }} title="Average runtime as % of the typical daily busy window">share</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const jobShort = row.job.length > 30 ? `${row.job.slice(0, 29)}\u2026` : row.job;
                    const shareColor = row.is_longpole ? '#f59e0b' : '#8899bb';
                    return (
                      <tr key={row.job}>
                        <td
                          title={`${row.job} — ${row.stability}, ${row.runs} run(s) over ${row.days_present}/${row.days_total} days`}
                          style={{ position: 'sticky', left: 0, fontSize: 11, color: 'rgba(255,255,255,.9)', paddingRight: 12, whiteSpace: 'nowrap', background: '#111d36' }}
                        >
                          {row.is_longpole && <span style={{ color: '#f59e0b' }}>▲ </span>}
                          {jobShort}
                        </td>
                        {dates.map((d) => {
                          const mins = cellMap.get(`${row.job}|${d}`) || 0;
                          return (
                            <td
                              key={d}
                              title={mins ? `${row.job} — ${d}: longest run ${mins.toFixed(0)} min` : `${row.job} — ${d}: did not run`}
                              style={{ textAlign: 'center', width: 30, height: 24, borderRadius: 2, ..._longpoleCellStyle(mins, maxMin) }}
                            >
                              <span style={{ fontSize: 9, fontWeight: 700, color: '#fff' }}>{mins ? Math.round(mins) : ''}</span>
                            </td>
                          );
                        })}
                        <td style={{ textAlign: 'center', fontSize: 10, fontFamily: 'monospace', color: 'rgba(255,255,255,.8)', paddingLeft: 8 }}>{row.avg_min.toFixed(0)}</td>
                        <td style={{ textAlign: 'center', fontSize: 10, fontFamily: 'monospace', color: 'rgba(255,255,255,.8)', paddingLeft: 4 }}>{row.max_min.toFixed(0)}</td>
                        <td style={{ textAlign: 'center', fontSize: 10, fontFamily: 'monospace', fontWeight: 700, color: shareColor, paddingLeft: 4 }}>
                          {row.window_share_pct ? `${row.window_share_pct.toFixed(0)}%` : '\u2014'}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Box>
            <Box display="flex" style={{ gap: 12, flexWrap: 'wrap', marginTop: 8 }}>
              <Typography variant="caption" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ width: 12, height: 12, borderRadius: 2, background: 'rgba(45,212,191,.7)', display: 'inline-block' }} /> shorter run
              </Typography>
              <Typography variant="caption" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ width: 12, height: 12, borderRadius: 2, background: 'rgba(245,158,11,.8)', display: 'inline-block' }} /> longer
              </Typography>
              <Typography variant="caption" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ width: 12, height: 12, borderRadius: 2, background: 'rgba(244,63,94,.85)', display: 'inline-block' }} /> longest run
              </Typography>
              <Typography variant="caption" style={{ color: '#f59e0b' }}>
                ▲ long-pole (avg ≥ {flagPct}% of the {busyRef.toFixed(1)}h busy window)
              </Typography>
              <Typography variant="caption" color="textSecondary">cell = longest single run that day (min); blank = job didn't run</Typography>
            </Box>
          </Box>
        );
      })()}

      {topBreaches.length > 0 && (() => {
        const hasBreach = topBreaches.some((j) => j.buffer_pct != null && j.buffer_pct < 0);
        const breachCount = topBreaches.filter((j) => j.buffer_pct != null && j.buffer_pct < 0).length;
        const isAdaptive = !!slaSource?.adaptive_active;
        const shown = Math.min(hasBreach ? breachCount : 10, topBreaches.length);
        const title = hasBreach
          ? (isAdaptive ? `Top ${shown} Performance Regressions` : `Top ${shown} Breaching Jobs`)
          : 'Top 10 Jobs by Peak Runtime';
        const subtitle = hasBreach
          ? (isAdaptive
            ? `${shown} job(s) exceeded adaptive history baseline · no contract SLA loaded`
            : `${shown} job(s) exceeded their SLA window`)
          : `No SLA breaches — showing ranked jobs by peak runtime · ${window.length} day(s)`;
        return (
        <Box className={classes.chart}>
          <Box display="flex" alignItems="center" justifyContent="space-between" style={{ flexWrap: 'wrap', gap: 8 }}>
            <Typography variant="subtitle2">{title}</Typography>
            {hasBreach && (
              <span className={`metric-badge ${isAdaptive ? 'metric-badge-amber' : 'metric-badge-red'}`}>
                {shown} {isAdaptive ? `regression${shown !== 1 ? 's' : ''}` : `breach${shown !== 1 ? 'es' : ''}`}
              </span>
            )}
          </Box>
          <Typography variant="caption" color="textSecondary">{subtitle}</Typography>
          <Table size="small" className="pe-table" aria-label="Top breaching jobs table" style={{ marginTop: 8 }}>
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
        );
      })()}

      {/* ── SLA Compliance Heatmap + Ctrl-M Execution Density Heatmap — LAST in the
          panel, matching vanilla's exact order (these are separate sections
          rendered after the whole batch-review-body, not before Long-Pole/Top
          Breaching Jobs). Both are plain HTML tables (ported verbatim from
          renderSlaHeatmap()/renderHourHeatmap() in app.js), not Highcharts
          heatmaps — the prior Highcharts port's continuous colorAxis produced
          a hazy, inconsistent look on a metric that is actually a discrete
          4-band status (same bands as the gauge/daily-window bars). ── */}
      {slaHeatmapRows && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">SLA Compliance Heatmap</Typography>
          <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginBottom: 8 }}>
            Job × Date — colour = SLA buffer that run (green = healthy buffer · amber = within 15% of the SLA ceiling · red = breach)
          </Typography>
          <Box display="flex" alignItems="center" style={{ gap: 16, marginBottom: 10, fontSize: 10, flexWrap: 'wrap' }}>
            <Box display="flex" alignItems="center" style={{ gap: 5 }}><span style={{ width: 12, height: 12, borderRadius: 2, background: '#0f3d24', display: 'inline-block' }} /><Typography variant="caption" color="textSecondary">No run</Typography></Box>
            <Box display="flex" alignItems="center" style={{ gap: 5 }}><span style={{ width: 12, height: 12, borderRadius: 2, background: '#10d96e', display: 'inline-block' }} /><Typography variant="caption" color="textSecondary">Healthy (&gt;15% buffer)</Typography></Box>
            <Box display="flex" alignItems="center" style={{ gap: 5 }}><span style={{ width: 12, height: 12, borderRadius: 2, background: '#f59e0b', display: 'inline-block' }} /><Typography variant="caption" color="textSecondary">Within 15% of SLA</Typography></Box>
            <Box display="flex" alignItems="center" style={{ gap: 5 }}><span style={{ width: 12, height: 12, borderRadius: 2, background: '#f43f5e', display: 'inline-block' }} /><Typography variant="caption" color="textSecondary">BREACH</Typography></Box>
          </Box>
          <Box style={{ overflowX: 'auto' }}>
            <table style={{ fontSize: 10, borderCollapse: 'collapse', minWidth: 'max-content' }}>
              <thead>
                <tr>
                  <th style={{ position: 'sticky', left: 0, zIndex: 1, background: '#111d36', textAlign: 'left', paddingRight: 12, paddingBottom: 4, color: '#6b7db3', fontWeight: 700, whiteSpace: 'nowrap', minWidth: 150 }}>Job</th>
                  {slaHeatmap!.dates.map((d) => {
                    const isSpike = spikeDateSet.has(d);
                    return (
                      <th key={d} title={`${d}${isSpike ? ' \u26a1 Spike day' : ''}`} style={{ paddingBottom: 4, paddingLeft: 2, paddingRight: 2, color: '#6b7db3', fontWeight: 400, textAlign: 'center', whiteSpace: 'nowrap', background: isSpike ? 'rgba(245,158,11,.18)' : undefined, borderBottom: isSpike ? '2px solid #f59e0b' : undefined }}>
                        {shortDate(d)}{isSpike && <><br /><span style={{ color: '#f59e0b', fontSize: 8 }}>{'\u26a1'}</span></>}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {slaHeatmapRows.orderedJobs.map((job) => {
                  const meta = slaHeatmapRows.jobPriority[job] || {};
                  const pr = meta.priority || 'normal';
                  const prColor = pr === 'critical' ? '#f43f5e' : pr === 'warning' ? '#f59e0b' : '#10d96e';
                  const prLabel = pr === 'critical' ? 'PRIORITY' : pr === 'warning' ? 'WATCH' : '';
                  return (
                    <tr key={job} style={pr !== 'normal' ? { background: `${prColor}0f`, boxShadow: `inset 2px 0 0 ${prColor}` } : undefined}>
                      <td title={`${job}${meta.reason ? ` \u00b7 ${meta.reason}` : ''}`} style={{ position: 'sticky', left: 0, background: '#111d36', paddingRight: 12, paddingTop: 2, paddingBottom: 2, color: '#f0f4ff', fontFamily: 'monospace', whiteSpace: 'nowrap', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        <Box display="flex" alignItems="center" style={{ gap: 6 }}>
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{job}</span>
                          {prLabel && <span style={{ display: 'inline-flex', alignItems: 'center', padding: '1px 5px', borderRadius: 4, fontSize: 8, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em', color: prColor, background: `${prColor}24`, border: `1px solid ${prColor}59` }}>{prLabel}</span>}
                        </Box>
                      </td>
                      {slaHeatmap!.dates.map((date) => {
                        const cell = slaHeatmapRows.lookup.get(`${job}||${date}`);
                        const isSpike = spikeDateSet.has(date);
                        return (
                          <td key={date} title={`${slaCellTitle(cell)}${isSpike ? ' \u26a1' : ''}`} style={{ padding: '2px 2px', textAlign: 'center', minWidth: 22, background: isSpike ? 'rgba(245,158,11,.06)' : undefined }}>
                            <div style={{ width: 20, height: 15, background: slaCellColor(cell), borderRadius: 2, margin: 'auto', border: isSpike ? '1px solid rgba(245,158,11,.5)' : undefined }} />
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Box>
        </Box>
      )}

      {hourHeatmapRows && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">Ctrl-M Execution Density Heatmap</Typography>
          <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginBottom: 8 }}>Sub-Application × Hour of Day — colour intensity shows job execution count (contention windows)</Typography>
          <Box style={{ overflowX: 'auto' }}>
            <table style={{ fontSize: 10, borderCollapse: 'collapse', minWidth: 'max-content' }}>
              <thead>
                <tr>
                  <th style={{ position: 'sticky', left: 0, zIndex: 1, background: '#111d36', textAlign: 'left', paddingRight: 12, paddingBottom: 4, color: '#6b7db3', fontWeight: 700, whiteSpace: 'nowrap', minWidth: 130 }}>Sub-App</th>
                  {hourHeatmapRows.hours.map((h) => (
                    <th key={h} style={{ paddingBottom: 4, color: '#6b7db3', fontWeight: 400, textAlign: 'center', whiteSpace: 'nowrap', minWidth: 28, fontSize: 9 }}>{String(h).padStart(2, '0')}:00</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {hourHeatmap!.sub_apps.map((app) => (
                  <tr key={app}>
                    <td title={app} style={{ position: 'sticky', left: 0, background: '#111d36', paddingRight: 12, paddingTop: 2, paddingBottom: 2, color: '#f0f4ff', fontFamily: 'monospace', whiteSpace: 'nowrap', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis' }}>{app}</td>
                    {hourHeatmapRows.hours.map((h) => {
                      const cell = hourHeatmapRows.lookup.get(`${app}||${h}`);
                      return (
                        <td key={h} title={hourCellTitle(cell, h)} style={{ padding: '2px 2px' }}>
                          <div style={{ width: 22, height: 16, borderRadius: 2, margin: 'auto', background: hourCellBg(cell, hourHeatmapRows.maxCount) }} />
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </Box>
          <Box display="flex" alignItems="center" style={{ gap: 10, marginTop: 12, fontSize: 10, color: '#6b7db3' }}>
            <span>Low</span>
            <div style={{ height: 8, width: 120, borderRadius: 4, background: 'linear-gradient(to right, rgba(34,211,238,.10), rgba(34,211,238,.26), rgba(34,211,238,.42), rgba(34,211,238,.58), rgba(34,211,238,.74), rgba(34,211,238,.90))' }} />
            <span>High</span>
            <span style={{ marginLeft: 16, fontStyle: 'italic' }}>Bright columns = peak scheduling contention windows</span>
          </Box>
        </Box>
      )}
    </Paper>
  );
}
