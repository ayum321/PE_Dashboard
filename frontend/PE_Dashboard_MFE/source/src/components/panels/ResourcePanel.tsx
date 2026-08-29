import React, { useMemo, useRef, useState } from 'react';
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
  TableSortLabel,
  TextField,
  Typography,
  makeStyles,
} from '@material-ui/core';
import Highcharts from '../../theme/highchartsSetup';
import HighchartsReact from 'highcharts-react-official';
import {
  fetchAzureTimeseries,
  getAzureAuthStatus,
  getAzureStatus,
  processResource,
  ResourceServer,
} from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { AzureFetchModal } from '../shared/AzureFetchModal';
import { KpiStatCard } from '../shared/KpiStatCard';
import { MiniGauge } from '../shared/MiniGauge';

interface FleetKpis {
  fleet_grade?: string;
  fleet_score?: number;
  avg_cpu?: number;
  avg_mem?: number;
  avg_disk?: number;
  n_critical?: number;
  n_warning?: number;
  n_healthy?: number;
  n_app?: number;
  n_db?: number;
  n_sre?: number;
  image_only?: number;
  known_servers?: number;
  cpu_reporting?: number;
  mem_reporting?: number;
  disk_reporting?: number;
  cpu_coverage?: number;
  mem_coverage?: number;
  disk_coverage?: number;
  total_vcpus?: number | null;
  vcpus_reporting?: number;
  thresholds?: Record<string, number>;
}

interface ResourceAnomaly {
  host: string;
  metric: string;
  value: number;
  z: number;
}

interface ExecutiveBottleneck {
  host: string;
  type: string;
  environment?: string;
  status: string;
  issues: string[];
}

interface ExecutiveFalseAlarm {
  host: string;
  type: string;
  cpu_max: number;
  cpu_avg: number;
  reason: string;
}

interface ExecutiveSummary {
  verdict: 'HEALTHY' | 'WARNING' | 'CRITICAL' | 'NO DATA';
  verdict_detail: string;
  false_alarms: ExecutiveFalseAlarm[];
  bottlenecks: ExecutiveBottleneck[];
  monitoring_notes?: ExecutiveBottleneck[];
  summary_line1: string;
  summary_line2: string;
}

interface ServerRow {
  host: string;
  server?: string;
  type?: string;
  environment?: string;
  product_group?: string;
  vm_size?: string;
  vcpus?: number | null;
  vcpu_source?: string;
  location?: string;
  resource_id?: string;
  source?: string;
  source_env?: string;
  image_only?: boolean;
  cpu_used?: number;
  cpu_avg_pct?: number;
  cpu_max_pct?: number;
  cpu_min_pct?: number;
  effective_cpu?: number;
  mem_used?: number;
  mem_gb?: number;
  mem_max_pct?: number;
  mem_min_pct?: number;
  disk_used_max?: number;
  disk_max_pct?: number;
  disk_min_pct?: number;
  cpu_available?: boolean;
  mem_available?: boolean;
  disk_available?: boolean;
  role_cpu_ok?: number;
  role_cpu_warn?: number;
  health_score?: number;
  status?: string;
  mem_status?: string;
  agg_trap?: boolean;
  dual_pressure?: boolean;
}

interface DeepDiveSpike {
  start?: string;
  end?: string;
  peak?: number;
  peak_time?: string;
  duration_min?: number;
  severity?: string;
  detection?: string;
  severity_reason?: string;
  z_score?: number;
  mean?: number;
  std?: number;
}

interface DeepDiveStat {
  mean?: number;
  std?: number;
  min?: number;
  max?: number;
  p5?: number;
  p95?: number;
  count?: number;
  max_anomalous?: boolean;
  min_anomalous?: boolean;
}

interface DeepDiveVm {
  series?: Record<string, { t: string; v: number }[]>;
  // FastAPI provides the timestamp-aligned Azure MAXIMUM aggregation separately
  // from the AVERAGE series.  Keep it distinct: a whole-window maximum line is
  // not a valid replacement for the peak within each aggregation bucket.
  series_max?: Record<string, { t: string; v: number }[]>;
  spikes?: Record<string, DeepDiveSpike[]>;
  stats?: Record<string, DeepDiveStat>;
  waveforms?: Record<string, { shape?: string; label?: string; icon?: string; risk?: string; meaning?: string; action?: string; confidence?: number; confidence_label?: string }>;
  baseline_confidence?: { pulls: number; min_pulls: number; mature_min_pulls?: number; degraded: boolean; retention_days?: number; baseline_mean?: number | null; baseline_std?: number | null };
  resource_id?: string;
}

interface DeepDivePattern {
  type: string;
  severity?: string;
  title?: string;
  description?: string;
  vms?: string[];
  metrics?: string[];
  metric?: string;
  // FastAPI emits a human-readable UTC time-of-day (for example "01:30")
  // for cross-server coincidence groups, not a numeric timestamp.
  time_utc?: string | number;
  hour?: number;
  recurrence_days?: number;
  recurrence_ratio?: number;
  peak_z?: number;
  peak?: number;
  peak_mean?: number;
  total_duration_min?: number;
  count?: number;
  worst_vm?: string;
  worst_peak?: number;
  mean_prior?: number;
  mean_recent?: number;
  worsening?: boolean;
}

interface DeepDiveHeatmapGrid {
  name: string;
  values: (number | string | null)[];
}

interface DeepDiveResponse {
  vms: Record<string, DeepDiveVm>;
  heatmap?: { timestamps: string[]; grids?: Record<string, DeepDiveHeatmapGrid[]> };
  patterns?: DeepDivePattern[];
  baseline?: { days_observed?: number };
  spike_attribution?: {
    rows: { vm: string; metric: string; start?: string; end?: string; peak?: number; peak_time?: string; duration_min?: number; severity?: string; concurrent_jobs: number; heaviest?: string; heaviest_hrs?: number; jobs?: { job: string; hrs?: number }[] }[];
    summary: { spikes_total: number; spikes_attributed: number; attribution_rate: number; runs_loaded: number; caveat: string };
  };
  window?: { timezone?: string; start_utc?: string; end_utc?: string; grain?: string; data_points?: number; is_custom?: boolean };
  summary?: { vm_count: number; hours_back: number; total_critical: number; total_warning: number; affected_vms: number };
}

type BaselineConfidence = NonNullable<DeepDiveVm['baseline_confidence']>;

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  controls: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2), flexWrap: 'wrap' },
  azureRow: { display: 'flex', gap: theme.spacing(1), alignItems: 'center', marginTop: theme.spacing(2), flexWrap: 'wrap' },
  empty: { marginTop: theme.spacing(2) },
  section: { marginTop: theme.spacing(3) },
}));

type SortKey = 'host' | 'cpu_used' | 'mem_used' | 'disk_used_max' | 'health_score';


/** Short display name for an Azure metric key, ported from _ddShortMetric() (app.js). */
function shortMetric(k: string): string {
  k = k || '';
  if (k.includes('CPU')) return 'CPU';
  if (/Available Memory Bytes/i.test(k)) return 'Memory Bytes';
  if (k.includes('Memory')) return 'Memory';
  if (k.includes('OS Disk')) return 'OS Disk';
  if (k.includes('Data Disk')) return 'Data Disk';
  return k.replace(' Percentage', '').replace(' Consumed', '');
}

type DeepDiveMetricFamily = 'cpu' | 'memory-percent' | 'memory-bytes' | 'os-disk' | 'data-disk' | 'other';

/** Canonical family used for joins between stats, spikes, waveforms and UI text.
 * Azure has emitted both canonical and shortened metric names over time; exact
 * string comparison made an anomalous memory series look normal. */
export function metricFamily(metric: string): DeepDiveMetricFamily {
  const key = (metric || '').toLowerCase();
  if (key.includes('cpu')) return 'cpu';
  if (key.includes('memory') || key.includes('mem')) {
    return key.includes('byte') ? 'memory-bytes' : 'memory-percent';
  }
  if (key.includes('os disk') && key.includes('bandwidth') && key.includes('percentage')) return 'os-disk';
  if (key.includes('data disk') && key.includes('bandwidth') && key.includes('percentage')) return 'data-disk';
  return 'other';
}

const GRADED_METRIC_FAMILIES = new Set<DeepDiveMetricFamily>(['cpu', 'memory-percent', 'os-disk', 'data-disk']);

/** List only graded metric families that have no anomaly event. Chart-only
 * telemetry is deliberately excluded: it has no warning/critical band. */
export function normalMetricLabels(stats: Record<string, DeepDiveStat>, spikes: Record<string, DeepDiveSpike[]>): string[] {
  const spikeFamilies = new Set(Object.keys(spikes || {}).map(metricFamily));
  return Array.from(new Set(
    Object.keys(stats || {})
      .filter((metric) => GRADED_METRIC_FAMILIES.has(metricFamily(metric)))
      .filter((metric) => !spikeFamilies.has(metricFamily(metric)))
      .map(shortMetric),
  ));
}

const SEVERITY_RANK: Record<string, number> = {
  critical_sustained: 4,
  critical: 3,
  warning: 2,
  notable: 1,
};

function highestMetricSeverity(events: DeepDiveSpike[]): { rank: number; label: string } {
  const top = events.reduce<DeepDiveSpike | null>((best, event) => {
    if (!best || (SEVERITY_RANK[event.severity || ''] || 0) > (SEVERITY_RANK[best.severity || ''] || 0)) return event;
    return best;
  }, null);
  const severity = top?.severity || '';
  const label = severity === 'critical_sustained' ? 'CRITICAL SUSTAINED'
    : severity === 'critical' ? 'CRITICAL'
      : severity === 'notable' ? 'ELEVATED' : 'WARNING';
  return { rank: SEVERITY_RANK[severity] ?? -1, label };
}

interface DominantMetric {
  key: string;
  family: DeepDiveMetricFamily;
  label: 'CPU' | 'MEM' | 'DISK';
  value: number;
  pressure: number;
  statType: 'P95' | 'min avail';
  events: DeepDiveSpike[];
  severityRank: number;
  severityLabel: string;
  color: string;
}

/** Select one metric as the card's complete story. Severity evidence is the
 * primary key; pressure is only a tie-breaker. This keeps a CPU critical badge
 * from sitting beside an unrelated memory headline. */
export function selectDominantMetric(
  stats: Record<string, DeepDiveStat>,
  spikes: Record<string, DeepDiveSpike[]>,
): DominantMetric | null {
  const entries = Object.entries(stats || {});
  const eventsFor = (family: DeepDiveMetricFamily) => Object.entries(spikes || {})
    .filter(([metric]) => metricFamily(metric) === family)
    .flatMap(([, values]) => values || []);
  const statFor = (families: DeepDiveMetricFamily[]) => entries
    .filter(([metric]) => families.includes(metricFamily(metric)))
    .sort(([, a], [, b]) => (b.p95 ?? b.max ?? 0) - (a.p95 ?? a.max ?? 0))[0];
  const candidates: DominantMetric[] = [];

  const cpuEvents = eventsFor('cpu');
  const cpuEntry = statFor(['cpu']);
  if (cpuEntry || cpuEvents.length) {
    const stat = cpuEntry?.[1];
    const value = stat?.p95 ?? stat?.max ?? Math.max(...cpuEvents.map((event) => event.peak || 0), 0);
    const sev = highestMetricSeverity(cpuEvents);
    candidates.push({
      key: cpuEntry?.[0] || 'Percentage CPU',
      family: 'cpu',
      label: 'CPU',
      value,
      pressure: value,
      statType: 'P95',
      events: cpuEvents,
      severityRank: sev.rank,
      severityLabel: sev.label,
      color: '#3b82f6',
    });
  }

  const memEvents = eventsFor('memory-percent');
  const memEntry = statFor(['memory-percent']);
  if (memEntry || memEvents.length) {
    const stat = memEntry?.[1];
    const value = stat?.min ?? Math.min(...memEvents.map((event) => event.peak ?? 100), 100);
    const sev = highestMetricSeverity(memEvents);
    candidates.push({
      key: memEntry?.[0] || 'Available Memory Percentage',
      family: 'memory-percent',
      label: 'MEM',
      value,
      pressure: 100 - value,
      statType: 'min avail',
      events: memEvents,
      severityRank: sev.rank,
      severityLabel: sev.label,
      color: '#22d3ee',
    });
  }

  const diskEvents = [...eventsFor('os-disk'), ...eventsFor('data-disk')];
  const diskEntry = statFor(['os-disk', 'data-disk']);
  if (diskEntry || diskEvents.length) {
    const stat = diskEntry?.[1];
    const value = stat?.p95 ?? stat?.max ?? Math.max(...diskEvents.map((event) => event.peak || 0), 0);
    const sev = highestMetricSeverity(diskEvents);
    const diskMetricKey = diskEntry?.[0] || 'OS Disk Bandwidth Consumed Percentage';
    candidates.push({
      key: diskMetricKey,
      family: metricFamily(diskMetricKey),
      label: 'DISK',
      value,
      pressure: value,
      statType: 'P95',
      events: diskEvents,
      severityRank: sev.rank,
      severityLabel: sev.label,
      color: '#f59e0b',
    });
  }

  candidates.sort((a, b) => b.severityRank - a.severityRank || b.events.length - a.events.length || b.pressure - a.pressure);
  return candidates[0] || null;
}

export function formatUtcDateTime(value?: string): string {
  const date = new Date(value || '');
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true });
}

export function durationMinutesFromBounds(start?: string, end?: string): number | null {
  const startMs = new Date(start || '').getTime();
  const endMs = new Date(end || '').getTime();
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) return null;
  return Math.max(1, Math.round((endMs - startMs) / 60000));
}

export function formatSpikeWindow(start?: string, end?: string): string {
  const startText = formatUtcDateTime(start);
  const endText = formatUtcDateTime(end);
  return startText === '—' || endText === '—' ? '—' : `${startText} → ${endText} UTC`;
}

export function formatRecurringDurations(durations: number[]): string {
  const values = durations.filter((value) => Number.isFinite(value) && value >= 0).map((value) => Math.max(1, Math.round(value)));
  if (!values.length) return '—';
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? `${humanizeDurationMin(min)} each` : `${humanizeDurationMin(min)}–${humanizeDurationMin(max)} per event`;
}

/** Green/amber/red band by threshold, higher-is-worse unless inverted. */
function bandColor(v: number, ok: number, warn: number, invert = false): string {
  if (invert) {
    if (v <= warn) return '#f43f5e';
    if (v <= ok) return '#f59e0b';
    return '#10d96e';
  }
  if (v >= warn) return '#f43f5e';
  if (v >= ok) return '#f59e0b';
  return '#10d96e';
}

/** Resolves the CPU/Mem/Disk value shown by the utilization bars for the active
 * Avg/Max/Min aggregation mode, ported from _resourceAggValue() (app.js). A job
 * that spikes briefly during a long observation window averages away to
 * nothing under Avg; Max surfaces the true worst point. */
function resourceAggValue(s: ServerRow, metric: 'cpu' | 'mem' | 'disk', mode: 'avg' | 'max' | 'min'): number | null {
  if (mode === 'max') {
    const v = metric === 'cpu' ? s.cpu_max_pct : metric === 'mem' ? s.mem_max_pct : s.disk_max_pct;
    if (v != null) return v;
  } else if (mode === 'min') {
    const v = metric === 'cpu' ? s.cpu_min_pct : metric === 'mem' ? s.mem_min_pct : s.disk_min_pct;
    if (v != null) return v;
  } else if (metric === 'cpu' && s.cpu_avg_pct != null) {
    return s.cpu_avg_pct;
  }
  return metric === 'cpu' ? s.cpu_used ?? null : metric === 'mem' ? s.mem_used ?? null : s.disk_used_max ?? null;
}

const STATUS_DOT: Record<string, string> = { Critical: 'status-dot-red', Warning: 'status-dot-amber', Healthy: 'status-dot-green', Unknown: 'status-dot-muted' };
const DB_EXPECTED_COLOR = '#a855f7';
const DB_EXPECTED_GRAD = ['#7e22ce', '#c084fc'];
const STATUS_BADGE: Record<string, string> = { Critical: 'metric-badge-red', Warning: 'metric-badge-amber', Healthy: 'metric-badge-green', Unknown: 'metric-badge-blue' };
const SOURCE_BADGE: Record<string, string> = { azure_monitor: 'metric-badge-teal', static: 'metric-badge-blue', dynamic: 'metric-badge-purple' };
// Mirrors services/pe_config.py MEM_WARN/MEM_CRIT (non-DB band only — the DB
// band is read from the backend's own mem_status classification, not re-derived).
const MEM_WARN_PCT = 70;
const MEM_CRIT_PCT = 80;
const VERDICT_COLOR: Record<string, string> = { HEALTHY: '#10d96e', WARNING: '#f59e0b', CRITICAL: '#f43f5e' };

/** Ported from _inferRole()/_inferEnv() (app.js) \u2014 hostname-based fallback used
 * when a deep-dive VM name can't be matched back to an already-known server row. */
function inferRole(vmName: string): string {
  const n = (vmName || '').toLowerCase();
  if (/db|ora|sql|pg|mysql|mongo|redis|cosmos|warehouse|dw/.test(n)) return 'DB';
  if (/sre|batch|sch|job|worker|cron|ctm|ctrl|infra|ops|mgmt|monitor/.test(n)) return 'SRE';
  if (/app|web|ui|api|gateway/.test(n)) return 'APP';
  return 'APP';
}
function inferEnv(vmName: string): string {
  const n = (vmName || '').toLowerCase();
  if (/\b(prod|prd)\b|[-_](prod|prd)/.test(n)) return 'PROD';
  if (/\b(test|tst|uat|qa)\b|[-_](test|tst|uat|qa)/.test(n)) return 'TEST';
  if (/\b(dev|stg|staging)\b|[-_](dev|stg|staging)/.test(n)) return 'DEV';
  if (n[0] === 'p') return 'PROD';
  if (n[0] === 't') return 'TEST';
  if (n[0] === 'd') return 'DEV';
  return '';
}

/** "Available Memory Percentage" reads as available-%, everything else as used-%. */
function formatPeak(metric: string, val: number): string {
  return /Available Memory/i.test(metric) ? `${val.toFixed(1)}% available` : `${val.toFixed(1)}%`;
}

function humanizeDurationMin(min: number): string {
  if (min >= 1440) return `${(min / 1440).toFixed(1)}d`;
  if (min >= 60) return `${(min / 60).toFixed(1)}h`;
  return `${Math.round(min)}m`;
}

type HeatmapMetric = 'cpu' | 'memory' | 'disk';

interface FleetHeatmapView {
  columns: { label: string; title: string }[];
  rows: { name: string; values: (number | null)[] }[];
  bucketSize: number;
}

const FLEET_HEATMAP_MAX_COLUMNS = 48;

function finiteMetricValue(value: number | string | null | undefined): number | null {
  if (value == null || value === '') return null;
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function formatHeatmapTimestamp(timestamp: string): string {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit' });
}

/**
 * Turns the dense Azure Monitor matrix into a display-safe grid.  CPU/Disk
 * retain the highest observed value in a bucket; available-memory retains the
 * lowest value because that is the operationally worst reading.  This avoids
 * losing a short pressure event while keeping 15-day heatmaps legible.
 */
export function buildFleetHeatmapView(
  heatmap: DeepDiveResponse['heatmap'] | undefined,
  metric: HeatmapMetric,
  maxColumns = FLEET_HEATMAP_MAX_COLUMNS,
): FleetHeatmapView | null {
  const timestamps = heatmap?.timestamps || [];
  const sourceRows = heatmap?.grids?.[metric] || [];
  if (!timestamps.length || !sourceRows.length) return null;

  const bucketSize = Math.max(1, Math.ceil(timestamps.length / maxColumns));
  const columns: FleetHeatmapView['columns'] = [];
  for (let start = 0; start < timestamps.length; start += bucketSize) {
    const end = Math.min(timestamps.length, start + bucketSize);
    const first = formatHeatmapTimestamp(timestamps[start]);
    const last = formatHeatmapTimestamp(timestamps[end - 1]);
    columns.push({ label: bucketSize === 1 ? first : `${first}–${last}`, title: bucketSize === 1 ? first : `${first} to ${last}` });
  }

  const rows = sourceRows.map((row) => {
    const values: (number | null)[] = [];
    for (let start = 0; start < timestamps.length; start += bucketSize) {
      const bucket = row.values.slice(start, start + bucketSize)
        .map(finiteMetricValue)
        .filter((value): value is number => value != null);
      if (!bucket.length) {
        values.push(null);
        continue;
      }
      const value = metric === 'memory' ? Math.min(...bucket) : Math.max(...bucket);
      values.push(value);
    }
    return { name: row.name, values };
  });

  // A completely un-emitted metric is still evidence: render its full grid so
  // operators can distinguish missing Azure telemetry from an absent heatmap.
  return { columns, rows, bucketSize };
}

function fleetHeatmapCellColor(value: number | null, metric: HeatmapMetric): string {
  // Missing telemetry is deliberately patterned and bordered.  It must never
  // disappear into the panel background or be mistaken for a healthy period.
  if (value == null) return 'repeating-linear-gradient(135deg, rgba(100,116,139,.28) 0, rgba(100,116,139,.28) 2px, rgba(15,23,42,.92) 2px, rgba(15,23,42,.92) 5px)';
  // Memory is an *available* percentage, so low values are the risk state.
  if (metric === 'memory') return value <= 15 ? '#f43f5e' : value <= 40 ? '#f59e0b' : '#10b981';
  return value >= 85 ? '#f43f5e' : value >= 65 ? '#f59e0b' : '#10b981';
}

/** Keep the unit and risk direction visible at the point a reviewer hovers a
 * heatmap cell. Memory is deliberately raw Azure "available %", not used %:
 * lower availability is more pressure. */
export function fleetHeatmapCellLabel(value: number | null, metric: HeatmapMetric): string {
  if (value == null) return 'not emitted by Azure Monitor';
  if (metric === 'memory') return `${value.toFixed(1)}% available — lower availability is higher risk`;
  return `${value.toFixed(1)}% utilized — higher utilization is higher risk`;
}

export interface CrossServerCorrelationGroup {
  timeUtc: string;
  metrics: string[];
  vms: string[];
  eventCount: number;
  severity: string;
}

function normalizedVmIdentity(vm: string): string {
  return String(vm || '').trim().toLowerCase().replace(/\.$/, '');
}

function appendUniqueVm(target: string[], candidate: string): void {
  const identity = normalizedVmIdentity(candidate);
  if (identity && !target.some((existing) => normalizedVmIdentity(existing) === identity)) target.push(candidate);
}

/** Group the API's raw coincidence patterns into an operational review unit. */
export function groupCrossServerCorrelations(patterns: DeepDivePattern[]): CrossServerCorrelationGroup[] {
  const groups = new Map<string, CrossServerCorrelationGroup>();
  for (const pattern of patterns) {
    if (pattern.type !== 'cross_vm_correlation' || !pattern.vms?.length) continue;
    const timeUtc = pattern.time_utc == null ? 'time unavailable' : String(pattern.time_utc);
    const metrics = (pattern.metrics || (pattern.metric ? [pattern.metric] : [])).slice().sort();
    const key = `${timeUtc}|${metrics.join('|')}`;
    const existing = groups.get(key);
    if (existing) {
      pattern.vms.forEach((vm) => appendUniqueVm(existing.vms, vm));
      existing.eventCount += pattern.count || pattern.vms.length;
      if (pattern.severity === 'critical') existing.severity = 'critical';
      continue;
    }
    const vms: string[] = [];
    pattern.vms.forEach((vm) => appendUniqueVm(vms, vm));
    groups.set(key, {
      timeUtc,
      metrics,
      vms,
      eventCount: pattern.count || pattern.vms.length,
      severity: pattern.severity || 'warning',
    });
  }
  return Array.from(groups.values()).sort((a, b) => b.vms.length - a.vms.length || b.eventCount - a.eventCount);
}

/** Select the metric with the most material evidence for a freshly loaded
 * fleet. This prevents an all-clear CPU heatmap being the default while the
 * selected window actually contains memory warnings or breaches. */
export function preferredFleetHeatmapMetric(response: Pick<DeepDiveResponse, 'vms'>): HeatmapMetric {
  const score: Record<HeatmapMetric, number> = { cpu: 0, memory: 0, disk: 0 };
  for (const vmData of Object.values(response.vms || {})) {
    for (const [metric, spikes] of Object.entries(vmData.spikes || {})) {
      const target: HeatmapMetric | null = /memory/i.test(metric)
        ? 'memory'
        : /disk/i.test(metric)
          ? 'disk'
          : /cpu/i.test(metric)
            ? 'cpu'
            : null;
      if (!target) continue;
      for (const spike of spikes) {
        score[target] += spike.severity === 'critical_sustained' ? 3 : spike.severity === 'critical' ? 2 : 1;
      }
    }
  }
  return (['memory', 'cpu', 'disk'] as HeatmapMetric[]).reduce(
    (best, metric) => score[metric] > score[best] ? metric : best,
    'cpu',
  );
}

function defaultDeepDiveVm(response: Pick<DeepDiveResponse, 'vms'>): string {
  const vmEntries = Object.entries(response.vms || {});
  const firstWithSpikes = vmEntries.find(([, vmData]) => Object.values(vmData.spikes || {}).some((events) => events.length > 0));
  return (firstWithSpikes || vmEntries[0])?.[0] || '';
}

function baselineConfidenceTitle(confidence: BaselineConfidence): string {
  return `Baseline: ${confidence.pulls} pull${confidence.pulls !== 1 ? 's' : ''} / ${confidence.retention_days ?? confidence.min_pulls}d${confidence.baseline_mean != null ? ` · μ=${confidence.baseline_mean}%` : ''}${confidence.baseline_std != null ? ` σ=${confidence.baseline_std}%` : ''}${confidence.degraded ? ' — session only, not yet baseline-eligible' : ''}`;
}

function lowConfidenceBaselineTitle(confidence: BaselineConfidence): string {
  return `${baselineConfidenceTitle(confidence)} — severity uses a low-confidence baseline; corroborate with the raw time series.`;
}

function stripPersistedDeepDive<T extends Record<string, unknown> | null>(resource: T): T {
  if (!resource || !Object.prototype.hasOwnProperty.call(resource, 'deep_dive')) return resource;
  const { deep_dive: _discarded, ...rest } = resource as T & { deep_dive?: DeepDiveResponse };
  return rest as T;
}

const SEVERITY_COLOR: Record<string, string> = {
  'CRITICAL SUSTAINED': '#a855f7', CRITICAL: '#f43f5e', WARNING: '#f59e0b', NOTABLE: '#6b7db3', ELEVATED: '#f59e0b',
};

const ddSelectStyle: React.CSSProperties = { background: '#0a0f1e', border: '1px solid #213060', borderRadius: 6, padding: '3px 8px', color: '#e2e8f0', fontSize: 11 };
function ddPillStyle(active: boolean): React.CSSProperties {
  return { padding: '2px 8px', borderRadius: 6, fontSize: 10, fontWeight: 700, cursor: 'pointer', border: `1px solid ${active ? '#3b82f680' : '#213060'}`, background: active ? '#3b82f61f' : 'transparent', color: active ? '#93c5fd' : '#6b7db3' };
}

/** Fixed-scale inline sparkline (last-6h slice when available), ported from
 * the SVG polyline built in _renderVmServerCard() (app.js). Fixed 0–100 axis
 * for CPU/Mem keeps amplitudes comparable card-to-card. */
function Sparkline({ points, color, fixed0to100 }: { points: { t: string; v: number }[]; color: string; fixed0to100: boolean }) {
  const now = Date.now();
  const sixHoursMs = 6 * 60 * 60 * 1000;
  const recent = points.filter((p) => now - new Date(p.t).getTime() <= sixHoursMs);
  const source = recent.length > 4 ? recent : points;
  const vals = source.map((p) => p.v);
  const mn = fixed0to100 ? 0 : Math.min(...vals);
  const mx = fixed0to100 ? 100 : Math.max(...vals);
  const rng = mx - mn || 1;
  const w = 120, h = 28;
  const step = w / Math.max(1, vals.length - 1);
  const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(h - ((v - mn) / rng) * h).toFixed(1)}`).join(' ');
  return (
    <svg width={w} height={h} style={{ opacity: 0.75 }}>
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" />
    </svg>
  );
}

/** Investigated: does the Unified Time-Series chart's stepped/plateaued
 * shape indicate forward-filled/interpolated telemetry rather than real
 * utilization? Checked live snapshot payloads directly: Azure returns a
 * complete, evenly-spaced bucket per host/metric (e.g. 360 hourly points for
 * a 15-day window with zero missing buckets in the common case) and the
 * backend never fills a missing bucket \u2014 `_query_single_vm_timeseries` only
 * appends a point when Azure actually returned one. The plateaus are real:
 * idle VMs sit within ~0.5% of their own average for hours, then a real
 * batch/business-hours spike produces the "cliff". However, on the rare host
 * where Azure genuinely drops a bucket (confirmed once across four real
 * snapshots), the chart used to silently bridge the gap with a straight
 * line \u2014 indistinguishable from a real gradual change. `withGapBreaks`
 * detects a delta more than 1.75x the series' own median cadence and inserts
 * a null point so Highcharts renders a break instead of a false ramp. */
export function withGapBreaks(points: { t: string; v: number }[]): { data: (number | null)[][]; sawGap: boolean } {
  if (points.length < 3) return { data: points.map((p) => [new Date(p.t).getTime(), p.v]), sawGap: false };
  const times = points.map((p) => new Date(p.t).getTime());
  const deltas = times.slice(1).map((t, i) => t - times[i]).sort((a, b) => a - b);
  // Lower median keeps a short series such as [1h, 7h] anchored to its
  // observed cadence instead of learning the missing-bucket gap as normal.
  const median = deltas[Math.floor((deltas.length - 1) / 2)] || 0;
  const threshold = median * 1.75;
  const data: (number | null)[][] = [];
  let sawGap = false;
  for (let i = 0; i < points.length; i++) {
    data.push([times[i], points[i].v]);
    if (i < points.length - 1 && median > 0 && (times[i + 1] - times[i]) > threshold) {
      data.push([times[i] + (times[i + 1] - times[i]) / 2, null]);
      sawGap = true;
    }
  }
  return { data, sawGap };
}

/** Fuzzy-matches a server's short host name against loaded deep-dive VM keys
 * and returns its CPU series for the Trend column, ported from
 * _serverSparkline()'s lookup logic (app.js) \u2014 Azure VM names often drop the
 * domain suffix the resource report's host field carries. */
function findDeepDiveSeries(deepDive: DeepDiveResponse | null, host: string): { t: string; v: number }[] | null {
  if (!deepDive?.vms) return null;
  const name = (host || '').toLowerCase().replace(/\..*/, '');
  if (!name) return null;
  let vmData: DeepDiveVm | undefined = deepDive.vms[name];
  if (!vmData) {
    const match = Object.entries(deepDive.vms).find(([k]) => k.includes(name) || name.includes(k));
    vmData = match?.[1];
  }
  const pts = vmData?.series?.['Percentage CPU'];
  return pts && pts.length >= 3 ? pts.slice(-48) : null;
}

export function ResourcePanel() {
  const classes = useStyles();
  const { data, setResource, setCustomerName } = useAppData();
  const resourceRefreshId = useRef(0);
  const deepDiveRefreshId = useRef(0);
  const derivedResourceRefreshId = useRef(0);
  const [filter, setFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [envFilter, setEnvFilter] = useState('');
  const [productGroupFilter, setProductGroupFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('cpu_used');
  const [sortDesc, setSortDesc] = useState(true);
  // Avg/Max/Min aggregation toggle for the utilization bars — mirrors Azure
  // Metrics Explorer's own picker (ported from _resourceAggMode, app.js).
  const [utilAggMode, setUtilAggMode] = useState<'avg' | 'max' | 'min'>('avg');
  const [azureAuth, setAzureAuth] = useState<Record<string, unknown> | null>(null);
  const [azureModalOpen, setAzureModalOpen] = useState(false);
  const [hoursBack, setHoursBack] = useState(24);
  const [fleetKpis, setFleetKpis] = useState<FleetKpis | null>(null);
  const [anomalies, setAnomalies] = useState<ResourceAnomaly[]>([]);
  const [execSummary, setExecSummary] = useState<ExecutiveSummary | null>(null);

  // ── Metrics Deep Dive state ──
  const [deepDive, setDeepDive] = useState<DeepDiveResponse | null>(null);
  const [deepDiveBusy, setDeepDiveBusy] = useState(false);
  const [deepDiveError, setDeepDiveError] = useState<string | null>(null);
  const [deepDiveVm, setDeepDiveVm] = useState('');
  const [heatmapMetric, setHeatmapMetric] = useState<'cpu' | 'memory' | 'disk'>('cpu');
  // ── Requires Investigation grid controls, ported from _renderDeepDiveCharts() (app.js) ──
  const [ddSortBy, setDdSortBy] = useState<'priority' | 'mem' | 'spikes' | 'latest' | 'name'>('priority');
  const [ddMinPct, setDdMinPct] = useState(0);
  const [ddTypeFilter, setDdTypeFilter] = useState<Set<string>>(new Set());
  const [ddShowMaxOverlay, setDdShowMaxOverlay] = useState(true);
  const [ddShowMinOverlay, setDdShowMinOverlay] = useState(false);
  const [correlatedVms, setCorrelatedVms] = useState<Set<string>>(new Set());
  const [correlationSort, setCorrelationSort] = useState<'servers' | 'events' | 'time'>('servers');
  // ── Custom absolute time range, ported from toggleDeepDiveCustomPicker()/
  // setDeepDiveCustomRange() (app.js) — scope the deep dive to one batch
  // night or one incident instead of a rolling preset window. ──
  const [ddCustomPickerOpen, setDdCustomPickerOpen] = useState(false);
  const [ddCustomStart, setDdCustomStart] = useState('');
  const [ddCustomEnd, setDdCustomEnd] = useState('');
  const [ddCustomActive, setDdCustomActive] = useState(false);
  const resourceWithDeepDive = data.resource as (((typeof data.resource) & { deep_dive?: DeepDiveResponse }) | null);
  const persistedDeepDive = resourceWithDeepDive?.deep_dive;

  const clearDeepDiveState = (clearPersisted = false) => {
    deepDiveRefreshId.current += 1;
    setDeepDiveBusy(false);
    setDeepDiveError(null);
    setDeepDive(null);
    setDeepDiveVm('');
    setCorrelatedVms(new Set<string>());
    if (clearPersisted && resourceWithDeepDive?.deep_dive) {
      setResource(stripPersistedDeepDive(resourceWithDeepDive) as Parameters<typeof setResource>[0]);
    }
  };

  React.useEffect(() => {
    getAzureAuthStatus()
      .then(setAzureAuth)
      .catch(() => setAzureAuth(null));
    getAzureStatus().catch(() => undefined);
  }, []);

  React.useEffect(() => {
    return () => {
      resourceRefreshId.current += 1;
      deepDiveRefreshId.current += 1;
      derivedResourceRefreshId.current += 1;
    };
  }, []);

  React.useEffect(() => {
    if (!persistedDeepDive) {
      setDeepDive((current) => current == null ? current : null);
      setDeepDiveVm((current) => current ? '' : current);
      setCorrelatedVms((current) => current.size ? new Set<string>() : current);
      return;
    }
    setDeepDive((current) => current === persistedDeepDive ? current : persistedDeepDive);
    setHeatmapMetric(preferredFleetHeatmapMetric(persistedDeepDive));
    const nextVm = defaultDeepDiveVm(persistedDeepDive);
    setDeepDiveVm((current) => current && persistedDeepDive.vms?.[current] ? current : nextVm);
    setCorrelatedVms((current) => current.size
      ? new Set(Array.from(current).filter((vm) => Boolean(persistedDeepDive.vms?.[vm])))
      : current);
  }, [persistedDeepDive]);

  React.useEffect(() => {
    const rows = data.resource?.servers || [];
    const refreshId = ++derivedResourceRefreshId.current;
    const stillCurrent = () => refreshId === derivedResourceRefreshId.current;
    if (rows.length === 0) {
      setFleetKpis(null);
      setAnomalies([]);
      setExecSummary(null);
      return;
    }
    processResource(rows)
      .then((result) => {
        if (!stillCurrent()) return;
        setFleetKpis((result.kpis as FleetKpis) || null);
        setAnomalies((result.anomalies as ResourceAnomaly[]) || []);
        const exec = result.executive_summary as ExecutiveSummary | undefined;
        setExecSummary(exec && exec.verdict !== 'NO DATA' ? exec : null);
      })
      .catch(() => {
        if (!stillCurrent()) return;
        setFleetKpis(null);
        setAnomalies([]);
        setExecSummary(null);
      });
  }, [data.resource]);

  const servers = useMemo(() => (data.resource?.servers || []) as ServerRow[], [data.resource]);
  const isAzureSource = servers.some((s) => s.source === 'azure_monitor');
  const serverTypeCounts = useMemo(() => servers.reduce<Record<string, number>>((counts, server) => {
    const type = String(server.type || 'APP').trim().toUpperCase() || 'APP';
    counts[type] = (counts[type] || 0) + 1;
    return counts;
  }, {}), [servers]);
  const fleetAvg = useMemo(() => {
    // Matches the backend's own convention (resource_calculator.py
    // build_resource_payload): image-only hosts carry no telemetry and must
    // be excluded from the denominator, not silently averaged in as a 0%
    // reading. This branch only renders while fleetKpis (the backend-computed,
    // correctly-filtered average) hasn't loaded yet — but a wrong number
    // shown briefly is still a wrong number, so it must use the same "known"
    // definition as fleetKpis, overThreshold and memSeverity below.
    const rows = servers.filter((s) => !s.image_only);
    const count = rows.length || 1;
    const sum = (key: 'cpu_used' | 'mem_used' | 'disk_used_max' | 'health_score') =>
      rows.reduce((total, server) => total + (server[key] || 0), 0);
    return {
      cpu: sum('cpu_used') / count,
      mem: sum('mem_used') / count,
      disk: sum('disk_used_max') / count,
      health: sum('health_score') / count,
    };
  }, [servers]);

  // ── DB-expected memory context, single-sourced from the SAME mem_status
  // flag Fleet Diagnosis / the per-server table already use — fixes the top
  // KPI gauge showing red/critical for a fleet whose elevated memory is
  // entirely explained by expected DB SGA/PGA allocation (7a).
  //
  // Color is a straight tally of facts the BACKEND already classified per
  // server (mem_status, and the plain non-DB thresholds) — no separate
  // statistical judgment is computed here, so this can never render a color
  // that disagrees with Fleet Diagnosis or the anomaly engine on the same
  // fact (7f follow-up: the prior composition-weighted-threshold formula was
  // itself still "the gauge's own opinion"; this reads the verdict instead
  // of deriving a new one).
  // A fleet MEAN is the wrong headline for a risk dashboard: ten idle hosts drag
  // "Avg CPU" to 22% on the same screen that flags two boxes pegged at 98-99%.
  // The mean stays (it is the contracted KPI) but every gauge now carries the
  // count that actually communicates risk — how many hosts sit at or above the
  // threshold the gauge is graded against.
  const overThreshold = useMemo(() => {
    const live = servers.filter((s) => !s.image_only);
    const count = (pick: (s: ServerRow) => number | null | undefined, limit: number) =>
      live.filter((s) => {
        const v = pick(s);
        return v != null && v >= limit;
      }).length;
    return {
      cpu: count((s) => s.cpu_used, 80),
      mem: count((s) => ((s.type || 'APP') === 'DB' ? null : s.mem_used), MEM_CRIT_PCT),
      disk: count((s) => s.disk_used_max, 85),
    };
  }, [servers]);

  const memSeverity = useMemo(() => {
    const known = servers.filter((s) => s.mem_used != null);
    if (!known.length) return null;
    const dbCount = known.filter((s) => (s.type || 'APP') === 'DB').length;
    const dbExpectedCount = known.filter((s) => s.mem_status === 'DB_NORMAL').length;
    const dbHighCount = known.filter((s) => s.mem_status === 'DB_HIGH').length;
    const nonDbCritCount = known.filter((s) => (s.type || 'APP') !== 'DB' && (s.mem_used || 0) >= MEM_CRIT_PCT).length;
    const nonDbWarnCount = known.filter((s) => (s.type || 'APP') !== 'DB' && (s.mem_used || 0) >= MEM_WARN_PCT && (s.mem_used || 0) < MEM_CRIT_PCT).length;
    const color = (dbHighCount > 0 || nonDbCritCount > 0) ? '#f43f5e'
      : nonDbWarnCount > 0 ? '#f59e0b'
      : dbExpectedCount > 0 ? DB_EXPECTED_COLOR
      : '#10d96e';
    return { total: known.length, dbCount, dbExpectedCount, dbHighCount, nonDbCritCount, nonDbWarnCount, color };
  }, [servers]);

  const filtered = useMemo(() => {
    return servers.filter((server) => {
      if (filter && !server.host.toLowerCase().includes(filter.toLowerCase())) return false;
      if (typeFilter && (server.type || 'APP').toUpperCase() !== typeFilter) return false;
      if (envFilter && (server.environment || '').toUpperCase() !== envFilter) return false;
      if (productGroupFilter && (server.product_group || '') !== productGroupFilter) return false;
      if (statusFilter && (server.status || 'Unknown') !== statusFilter) return false;
      return true;
    });
  }, [servers, filter, typeFilter, envFilter, productGroupFilter, statusFilter]);
  const productGroups = useMemo(() => Array.from(new Set(servers.map((s) => s.product_group).filter(Boolean))) as string[], [servers]);
  const sorted = useMemo(() => {
    const rows = [...filtered];
    rows.sort((a, b) => {
      const av = a[sortKey] ?? 0;
      const bv = b[sortKey] ?? 0;
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : Number(av) - Number(bv);
      return sortDesc ? -cmp : cmp;
    });
    return rows;
  }, [filtered, sortKey, sortDesc]);

  // ── Server utilization bars — top 20 by peak metric, ported from
  // renderResourceHeatmap()'s per-server bar rows (app.js). ──
  const utilizationRows = useMemo(() => {
    const known = servers.filter((s) => !s.image_only && Math.max(s.cpu_used || 0, s.mem_used || 0, s.disk_used_max || 0) > 0);
    return [...known]
      .sort((a, b) => {
        const maxOf = (s: ServerRow) => Math.max(resourceAggValue(s, 'cpu', utilAggMode) || 0, resourceAggValue(s, 'mem', utilAggMode) || 0, resourceAggValue(s, 'disk', utilAggMode) || 0);
        return maxOf(b) - maxOf(a);
      })
      .slice(0, 20);
  }, [servers, utilAggMode]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDesc(!sortDesc);
    } else {
      setSortKey(key);
      setSortDesc(true);
    }
  };

  const handleFetched = async (fetchedServers: ResourceServer[], meta: { hoursBack: number; customer?: string }) => {
    const refreshId = ++resourceRefreshId.current;
    const stillCurrent = () => refreshId === resourceRefreshId.current;
    clearDeepDiveState(false);
    // Persist the exact resource-engine verdict alongside the raw Azure rows.
    // Export/Findings consume this same object; storing only `servers` caused
    // them to recreate counts independently (and occasionally report a
    // healthy fleet beside warning hosts).
    const resolved = await processResource(fetchedServers);
    if (!stillCurrent()) return;
    const resourcePayload = {
      servers: fetchedServers,
      kpis: resolved.kpis,
      anomalies: resolved.anomalies,
      executive_summary: resolved.executive_summary,
    };
    setResource(resourcePayload);
    setFleetKpis((resolved.kpis as FleetKpis) || null);
    setAnomalies((resolved.anomalies as ResourceAnomaly[]) || []);
    const exec = resolved.executive_summary as ExecutiveSummary | undefined;
    setExecSummary(exec && exec.verdict !== 'NO DATA' ? exec : null);
    setHoursBack(meta.hoursBack);
    setDdCustomActive(false);
    // Only set when not already known \u2014 an engagement that starts from Resource
    // Review (never touching Upload & Intake) previously left the persistent
    // customer header blank on every page, not just this one.
    if (!data.customerName && meta.customer) setCustomerName(meta.customer);
    getAzureAuthStatus().then(setAzureAuth).catch(() => undefined);
    // Capture the time-series in the same motion as the fetch. It used to wait
    // for a manual "Load Metrics Deep Dive" click, so anyone who fetched and
    // exported straight away shipped a report whose Metrics Explorer had a
    // panel per host and a series for none of them ("0 of 12 with series").
    // Failure here is non-fatal: handleLoadDeepDive records its own error and
    // the button stays available for a retry.
    // The rows arrive as ResourceServer from the fetch and are read here as
    // ServerRow — the same widening `servers` already applies at line ~575.
    void handleLoadDeepDive(fetchedServers as unknown as ServerRow[], meta.hoursBack, resourcePayload);
  };

  const handleLoadDeepDive = async (
    overrideServers?: ServerRow[],
    overrideHoursBack?: number,
    overrideResource?: Parameters<typeof setResource>[0],
  ) => {
    // `servers` is derived from React state, which has not settled yet when this
    // runs straight after a fetch — the caller passes the rows it just received.
    const sourceServers = Array.isArray(overrideServers) ? overrideServers : servers;
    const vmIds = sourceServers.filter((s) => s.resource_id).map((s) => s.resource_id as string);
    if (!vmIds.length) return;
    const refreshId = ++deepDiveRefreshId.current;
    const stillCurrent = () => refreshId === deepDiveRefreshId.current;
    setDeepDiveBusy(true);
    setDeepDiveError(null);
    try {
      const payload: Record<string, unknown> = { vm_ids: vmIds };
      // Thread the already-tag-aware role (APP/DB/SRE) computed during the
      // resource fetch, keyed by resource_id, so the spike detector judges
      // each VM against the correct memory band instead of re-guessing from
      // the name alone (7a root cause fix).
      const vmTypes: Record<string, string> = {};
      for (const s of sourceServers) {
        if (s.resource_id && s.type) vmTypes[s.resource_id] = s.type;
      }
      if (Object.keys(vmTypes).length) payload.vm_types = vmTypes;
      if (ddCustomActive && ddCustomStart && ddCustomEnd) {
        payload.start_utc = new Date(ddCustomStart).toISOString();
        payload.end_utc = new Date(ddCustomEnd).toISOString();
      } else {
        payload.hours_back = overrideHoursBack ?? hoursBack;
      }
      const result = await fetchAzureTimeseries(payload);
      if (!stillCurrent()) return;
      const dd = result as unknown as DeepDiveResponse;
      setDeepDive(dd);
      // Deep-dive charts/correlation are already produced by the same Azure
      // response that powers this screen. Keep that evidence in the shared
      // session payload so the standalone report can render it without a
      // second query or a different calculation path.
      const baseResource = overrideResource ?? data.resource;
      if (baseResource) {
        setResource({ ...baseResource, deep_dive: dd });
      }
      setHeatmapMetric(preferredFleetHeatmapMetric(dd));
      // Prefer auto-opening a VM with detected spikes (matches vanilla's
      // "auto-open one card so the highest-priority evidence is visible").
      const firstVm = defaultDeepDiveVm(dd);
      if (firstVm) setDeepDiveVm(firstVm);
    } catch (error) {
      if (!stillCurrent()) return;
      setDeepDiveError(error instanceof Error ? error.message : 'Metrics Deep Dive fetch failed.');
    } finally {
      if (stillCurrent()) setDeepDiveBusy(false);
    }
  };

  const handleApplyCustomRange = () => {
    if (!ddCustomStart || !ddCustomEnd) return;
    if (new Date(ddCustomStart).getTime() >= new Date(ddCustomEnd).getTime()) {
      setDeepDiveError('The end of a custom time range must be later than its start.');
      return;
    }
    setDdCustomActive(true);
    clearDeepDiveState(true);
    setDdCustomPickerOpen(false);
  };
  const handleClearCustomRange = () => {
    setDdCustomActive(false);
    setDdCustomStart('');
    setDdCustomEnd('');
    clearDeepDiveState(true);
  };

  // ── Deep Dive derived views ──
  // Plain grid rather than a Highcharts heatmap: the previous dense series
  // could leave only the colour legend visible when Monitor returned a long
  // window or sparse values.  This renders every available sample explicitly.
  const fleetHeatmapView = useMemo(
    () => buildFleetHeatmapView(deepDive?.heatmap, heatmapMetric),
    [deepDive, heatmapMetric],
  );

  const deepDiveVmChart: Highcharts.Options | null = useMemo(() => {
    const vmData = deepDive?.vms?.[deepDiveVm];
    if (!vmData?.series) return null;
    // Keep the unified chart focused on CPU/Memory/Disk-bandwidth % metrics —
    // the raw Azure timeseries also carries Network/VmAvailability/Disk Read-Write
    // Bytes-Ops counters that are noise for this dashboard's PE-facing view.
    const gradedMetrics = new Set([
      'Percentage CPU',
      'Available Memory Percentage',
      'OS Disk Bandwidth Consumed Percentage',
      'Data Disk Bandwidth Consumed Percentage',
    ]);
    const seriesEntries = Object.entries(vmData.series).filter(([metric]) => gradedMetrics.has(metric));
    if (!seriesEntries.length) return null;
    const colors: Record<string, string> = { 'Percentage CPU': '#3b82f6', 'Available Memory Percentage': '#22d3ee', 'OS Disk Bandwidth Consumed Percentage': '#a855f7', 'Data Disk Bandwidth Consumed Percentage': '#a855f7' };
    let hadGap = false;
    const series: Highcharts.SeriesOptionsType[] = seriesEntries.map(([metric, points]) => {
      const gapped = withGapBreaks(points);
      if (gapped.sawGap) hadGap = true;
      return {
        type: 'line',
        name: shortMetric(metric),
        color: colors[metric] || undefined,
        lineWidth: 2,
        marker: { enabled: false },
        connectNulls: false,
        data: gapped.data,
      };
    });
    // True per-bucket MAXIMUM series from FastAPI.  This is deliberately
    // timestamp-aligned rather than a horizontal whole-window max: the latter
    // falsely implies every average bucket peaked at the same value.
    if (ddShowMaxOverlay) {
      for (const [metric, points] of seriesEntries) {
        const maxima = vmData.series_max?.[metric] || [];
        if (!maxima.length) continue;
        const averageByTime = new Map(points.map((p) => [p.t, p.v]));
        const aligned = maxima.filter((p) => averageByTime.has(p.t));
        const hasMeaningfulGap = aligned.some((p) => p.v - (averageByTime.get(p.t) || 0) >= 2);
        if (!hasMeaningfulGap) continue;
        series.push({
          type: 'line',
          name: `${shortMetric(metric)} peak`,
          color: colors[metric] || '#94a3b8',
          dashStyle: 'ShortDot',
          lineWidth: 1,
          opacity: 0.58,
          marker: { enabled: false },
          data: aligned.map((p) => [new Date(p.t).getTime(), p.v]),
        });
      }
    }
    // The API currently returns per-bucket averages and maxima, not minima.
    // Keep an explicitly labelled low-water mark available, without pretending
    // it is an Azure per-bucket MINIMUM aggregation.
    if (ddShowMinOverlay) {
      for (const [metric, points] of seriesEntries) {
        const observedFloor = vmData.stats?.[metric]?.min;
        if (observedFloor == null || !points.length) continue;
        series.push({
          type: 'line',
          name: `${shortMetric(metric)} observed floor`,
          color: colors[metric] || '#94a3b8',
          dashStyle: 'Dash',
          lineWidth: 1,
          opacity: 0.34,
          marker: { enabled: false },
          data: [[new Date(points[0].t).getTime(), observedFloor], [new Date(points[points.length - 1].t).getTime(), observedFloor]],
        });
      }
    }
    const spikeBands = Object.entries(vmData.spikes || {}).flatMap(([metric, spikes]) => spikes
      .filter((spike) => spike.start && spike.end)
      .map((spike) => ({
        from: new Date(spike.start as string).getTime(),
        to: new Date(spike.end as string).getTime(),
        color: `${SEVERITY_COLOR[(spike.severity || 'CRITICAL').toUpperCase().replace('_', ' ')] || '#f43f5e'}18`,
        borderColor: `${SEVERITY_COLOR[(spike.severity || 'CRITICAL').toUpperCase().replace('_', ' ')] || '#f43f5e'}66`,
        borderWidth: 1,
      })));
    return {
      chart: { type: 'line', height: 320, zooming: { type: 'x' }, backgroundColor: 'transparent' },
      title: { text: undefined },
      xAxis: { type: 'datetime', plotBands: spikeBands, labels: { style: { color: '#94a3b8', fontSize: '10px' } } },
      yAxis: { title: { text: 'Utilization (%)', style: { color: '#94a3b8', fontSize: '10px' } }, min: 0, max: 100, gridLineColor: 'rgba(71,85,105,.34)', gridLineDashStyle: 'Dash' },
      legend: { itemStyle: { color: '#cbd5e1', fontSize: '10px', fontWeight: '500' } },
      tooltip: { shared: true, valueDecimals: 1, backgroundColor: 'rgba(9,14,31,.98)', borderColor: 'rgba(96,165,250,.42)', style: { color: '#e2e8f0' } },
      series,
      _hadGap: hadGap,
    } as Highcharts.Options & { _hadGap: boolean };
  }, [deepDive, deepDiveVm, ddShowMaxOverlay, ddShowMinOverlay]);

  // ── Requires Investigation card grid data, ported from _renderVmServerCard()
  // + renderFilteredGrid() (app.js). Groups deep-dive VMs into "has spikes"
  // (critical, sortable/filterable card grid) vs "clean" (compact table). ──
  // Four-way pattern taxonomy, ported from vanilla's Detected Patterns panel
  // (cross_vm_correlation / recurring_time / sustained_pressure / regime_change)
  // \u2014 was flattened to the single "N Critical Anomalies" banner, which counts
  // raw SPIKE EVENTS, not detected PATTERNS (one pattern can bundle many
  // events). Both numbers are real and independently correct; they are not
  // the same measure (IMPROVE).
  const patternTaxonomy = useMemo(() => {
    const patterns = deepDive?.patterns || [];
    if (!patterns.length) return null;
    // Own-property counters, and an explicit `other` bucket. `p.type in byType`
    // previously walked the prototype chain, so a pattern typed "constructor" or
    // "toString" would increment a function into NaN; and any new backend type
    // would be counted in `total` but shown in no chip, leaving the chips and
    // the "N grouped patterns" caption silently disagreeing.
    const byType: Record<string, number> = Object.create(null);
    byType.cross_vm_correlation = 0;
    byType.recurring_time = 0;
    byType.sustained_pressure = 0;
    byType.regime_change = 0;
    byType.other = 0;
    let critical = 0;
    for (const p of patterns) {
      const key = typeof p.type === 'string' && Object.prototype.hasOwnProperty.call(byType, p.type) && p.type !== 'other'
        ? p.type
        : 'other';
      byType[key]++;
      if (p.severity === 'critical') critical++;
    }
    // Derive the headline from the buckets so the chips can never fail to sum.
    const total = byType.cross_vm_correlation + byType.recurring_time
      + byType.sustained_pressure + byType.regime_change + byType.other;
    return { total, critical, byType };
  }, [deepDive]);

  // Regime-shift patterns (baseline μ/σ step-change vs the prior pull window)
  // were computed by the backend and typed on DeepDivePattern, but were never
  // rendered anywhere — they landed in the taxonomy's "Other" bucket with no
  // way to see *what* changed, silently discarding the direction/magnitude the
  // backend already worked out. Surface them as their own compact list so
  // "Other" only ever means genuinely-unclassified evidence.
  const regimeShifts = useMemo(
    () => (deepDive?.patterns || []).filter((p) => p.type === 'regime_change'),
    [deepDive],
  );

  const correlationGroups = useMemo(
    () => groupCrossServerCorrelations(deepDive?.patterns || []),
    [deepDive],
  );
  const sortedCorrelationGroups = useMemo(() => [...correlationGroups].sort((a, b) => {
    if (correlationSort === 'events') return b.eventCount - a.eventCount || b.vms.length - a.vms.length;
    if (correlationSort === 'time') return a.timeUtc.localeCompare(b.timeUtc);
    return b.vms.length - a.vms.length || b.eventCount - a.eventCount;
  }), [correlationGroups, correlationSort]);

  // Every host in this estate shares a long site/tenant prefix (tsbf1414…), so
  // the only distinguishing characters may sit anywhere in the ID — and
  // tsbf141430011 vs tsbf141403011 differ by a digit transposition in the
  // middle. Dim matching positions and highlight differing positions so the
  // eye lands on the characters that identify the actual host.
  const hostNames = useMemo(() => Object.keys(deepDive?.vms || {}), [deepDive]);

  const renderHostId = (name: string, key?: string | number) => (
    <span key={key} style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
      {Array.from(name).map((character, index) => {
        const differs = hostNames.length < 2 || hostNames.some((other) => other[index] !== character);
        return <span key={`${character}-${index}`} style={{ opacity: differs ? 1 : 0.45, fontWeight: differs ? 800 : 500, color: differs ? '#fbbf24' : '#e6edff' }}>{character}</span>;
      })}
    </span>
  );

  // Surface the detector's own baseline quality at fleet level.  A few low-pull
  // VMs can otherwise look equally trustworthy beside well-observed ones.
  const baselineQuality = useMemo(() => {
    const entries = Object.entries(deepDive?.vms || {})
      .map(([vm, data]) => ({ vm, confidence: data.baseline_confidence }))
      .filter((entry): entry is { vm: string; confidence: NonNullable<DeepDiveVm['baseline_confidence']> } => Boolean(entry.confidence));
    if (!entries.length) return null;
    const degraded = entries.filter((entry) => entry.confidence.degraded);
    const minPulls = Math.min(...entries.map((entry) => entry.confidence.pulls));
    const matureMinPulls = Math.max(...entries.map((entry) => entry.confidence.mature_min_pulls ?? entry.confidence.min_pulls));
    const initial = entries.filter((entry) => entry.confidence.pulls < (entry.confidence.mature_min_pulls ?? entry.confidence.min_pulls));
    return { observed: entries.length, degraded, initial, minPulls, matureMinPulls };
  }, [deepDive]);

  const ddCards = useMemo(() => {
    if (!deepDive?.vms) return { critical: [], clean: [] };
    const critical: Array<{
      vmName: string; vmData: DeepDiveVm; role: string; env: string;
      spikeCount: number; thresholdCrossCount: number; hasSustained: boolean; latestSpike: number;
      memAvail: number; memPressure: number; domLabel: string; domVal: number; domColor: string; domKey: string;
      severityLabel: string; severityColor: string; trendArrow: string; trendDelta: string;
      breachLabel: string; waveformLabel: string; waveformIcon: string; waveformRisk: string;
    }> = [];
    const clean: Array<{ vmName: string; vmData: DeepDiveVm }> = [];

    for (const [vmName, vmData] of Object.entries(deepDive.vms)) {
      const spikes = vmData.spikes || {};
      const hasSpikes = Object.values(spikes).some((arr) => arr.length > 0);
      if (!hasSpikes) { clean.push({ vmName, vmData }); continue; }

      const matchedServer = servers.find((s) => (s.resource_id && s.resource_id === vmData.resource_id) || s.host.split('.')[0] === vmName);
      const role = matchedServer?.type || inferRole(vmName);
      const env = matchedServer?.environment || inferEnv(vmName);

      let spikeCount = 0, thresholdCrossCount = 0, latestSpike = 0;
      for (const arr of Object.values(spikes)) {
        spikeCount += arr.length;
        for (const s of arr) {
          if ((s.severity || '').startsWith('critical') || s.detection === 'absolute_threshold') thresholdCrossCount++;
          if (s.peak_time) latestSpike = Math.max(latestSpike, new Date(s.peak_time).getTime());
        }
      }

      const stats = vmData.stats || {};
      const dominant = selectDominantMetric(stats, spikes);
      if (!dominant) continue;
      const memStat = Object.entries(stats).find(([metric]) => metricFamily(metric) === 'memory-percent')?.[1];
      // MIN AVAIL must be the observed minimum. P5 is a separate statistic.
      const memAvail = memStat?.min ?? (dominant.family === 'memory-percent' ? dominant.value : 100);
      const memPressure = 100 - memAvail;

      const domLabel = dominant.label;
      const domVal = dominant.value;
      const domColor = dominant.color;
      const domKey = dominant.key;
      const hasSustained = dominant.severityRank >= SEVERITY_RANK.critical_sustained;
      // Single-sourced from the SAME mem_status flag Fleet Diagnosis and the
      // Server Detail Table already use (7a) \u2014 a DB server's memory sitting in
      // its expected 80\u201392% SGA/PGA band is not a pressure signal, so it must
      // not out-rank a non-sustained z-score breach into CRITICAL here either.
      const isDbMemExpected = dominant.family === 'memory-percent' && matchedServer?.mem_status === 'DB_NORMAL' && dominant.severityRank < SEVERITY_RANK.critical;
      const severityLabel = isDbMemExpected ? 'EXPECTED DB LOAD' : dominant.severityLabel;
      const severityColor = isDbMemExpected ? DB_EXPECTED_COLOR : (SEVERITY_COLOR[severityLabel] || SEVERITY_COLOR.WARNING);

      const domSeries = vmData.series?.[domKey] || [];
      const recentSeries = domSeries.filter((p) => Date.now() - new Date(p.t).getTime() <= 6 * 60 * 60 * 1000);
      const trendSeries = recentSeries.length > 4 ? recentSeries : domSeries;
      let trendArrow = '', trendDelta = '';
      if (trendSeries.length > 4) {
        const latest = trendSeries[trendSeries.length - 1];
        const ref = trendSeries.filter((p) => new Date(latest.t).getTime() - new Date(p.t).getTime() >= 2 * 60 * 60 * 1000).pop() || trendSeries[0];
        const delta = latest.v - ref.v;
        trendArrow = delta > 2 ? '↑' : delta < -2 ? '↓' : '→';
        trendDelta = delta > 2 ? `+${delta.toFixed(0)}%` : delta < -2 ? `${delta.toFixed(0)}%` : 'flat';
      }
      let breachLabel = '';
      if (trendSeries.length > 4) {
        const latest = trendSeries[trendSeries.length - 1];
        const ref = trendSeries.filter((p) => new Date(latest.t).getTime() - new Date(p.t).getTime() >= 2 * 60 * 60 * 1000).pop() || trendSeries[0];
        const elapsed = new Date(latest.t).getTime() - new Date(ref.t).getTime();
        if (elapsed > 0) {
          const rate = (latest.v - ref.v) / elapsed;
          const target = domLabel === 'MEM' ? 15 : 80;
          const movingToward = domLabel === 'MEM' ? rate < 0 : rate > 0;
          const distance = domLabel === 'MEM' ? latest.v - target : target - latest.v;
          const hours = distance / Math.abs(rate) / 3600000;
          if (movingToward && distance > 0 && hours > 0 && hours < 24) breachLabel = domLabel === 'MEM' ? `< 15% avail in ~${hours.toFixed(0)}h` : `breach in ~${hours.toFixed(0)}h`;
        }
      }
      const waveform = vmData.waveforms?.[domKey];
      const waveformLabel = waveform?.label || '';
      const waveformIcon = waveform?.icon || '';
      const waveformRisk = waveform?.risk || '';
      critical.push({ vmName, vmData, role, env, spikeCount, thresholdCrossCount, hasSustained, latestSpike, memAvail, memPressure, domLabel, domVal, domColor, domKey, severityLabel, severityColor, trendArrow, trendDelta, breachLabel, waveformLabel, waveformIcon, waveformRisk });
    }

    const filtered = critical.filter((c) => {
      if (c.memPressure < ddMinPct) return false;
      if (ddTypeFilter.size > 0) {
        const roleUpper = c.role.toUpperCase();
        if (ddTypeFilter.has('DB') && roleUpper.includes('DB')) return true;
        if (ddTypeFilter.has('APP') && (roleUpper === 'APP' || roleUpper === 'SERVER')) return true;
        if (ddTypeFilter.has('SRE') && roleUpper === 'SRE') return true;
        return false;
      }
      return true;
    });
    filtered.sort((a, b) => {
      const envRank: Record<string, number> = { PROD: 0, TEST: 1, DEV: 2 };
      if (ddSortBy === 'priority') {
        if ((a.hasSustained ? 1 : 0) !== (b.hasSustained ? 1 : 0)) return (b.hasSustained ? 1 : 0) - (a.hasSustained ? 1 : 0);
        if ((envRank[a.env] ?? 9) !== (envRank[b.env] ?? 9)) return (envRank[a.env] ?? 9) - (envRank[b.env] ?? 9);
        if (a.thresholdCrossCount !== b.thresholdCrossCount) return b.thresholdCrossCount - a.thresholdCrossCount;
        if (a.spikeCount !== b.spikeCount) return b.spikeCount - a.spikeCount;
        return a.memAvail - b.memAvail;
      }
      if (ddSortBy === 'mem') return a.memAvail - b.memAvail;
      if (ddSortBy === 'spikes') return b.spikeCount - a.spikeCount;
      if (ddSortBy === 'latest') return b.latestSpike - a.latestSpike;
      return a.vmName.localeCompare(b.vmName);
    });

    return { critical: filtered, clean };
  }, [deepDive, servers, ddMinPct, ddTypeFilter, ddSortBy]);

  const toggleDdType = (t: string) => {
    setDdTypeFilter((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t); else next.add(t);
      return next;
    });
  };

  // ── Selected VM's spike-detail rows + header/insight text, ported from
  // _renderVmDeepDiveCard() (app.js), simplified (no cross-day recurrence grouping). ──
  const ddDetail = useMemo(() => {
    const vmData = deepDive?.vms?.[deepDiveVm];
    if (!vmData) return null;
    const spikes = vmData.spikes || {};
    const rows: Array<{ metric: string } & DeepDiveSpike> = [];
    for (const [metric, arr] of Object.entries(spikes)) {
      for (const s of arr) rows.push({ metric, ...s });
    }
    rows.sort((a, b) => (b.z_score || 0) - (a.z_score || 0));

    // Ctrl-M correlation \u2014 join each spike to the backend's spike_attribution
    // rows (one row per attributable spike, joined by overlapping clock time
    // against the uploaded Ctrl-M job runs). Only populated when a Ctrl-M batch
    // file has been uploaded this session; otherwise spike_attribution is absent
    // and every row's ctrlM is null, so the feature is silently "off" as intended.
    const attributionRows = deepDive?.spike_attribution?.rows || [];
    const findCtrlM = (row: { metric: string } & DeepDiveSpike) =>
      attributionRows.find((a) => a.vm === deepDiveVm && a.metric === row.metric && a.peak === row.peak) as
        | { concurrent_jobs: number; heaviest?: string; heaviest_hrs?: number; jobs?: { job: string; hrs?: number }[] }
        | undefined;

    const groupedRows: Array<{ row: typeof rows[number]; recurring: boolean; count: number; days: number; durations: number[]; maxPeak: number; ctrlM?: { concurrent_jobs: number; heaviest?: string; heaviest_hrs?: number; jobs?: { job: string; hrs?: number }[] } }> = [];
    const used = new Set<number>();
    rows.forEach((row, index) => {
      if (used.has(index)) return;
      used.add(index);
      const related = [row];
      const rowHour = row.peak_time ? new Date(row.peak_time).getUTCHours() : -1;
      const rowDay = row.peak_time ? new Date(row.peak_time).toISOString().slice(0, 10) : '';
      rows.forEach((candidate, candidateIndex) => {
        if (used.has(candidateIndex) || candidate.metric !== row.metric || !candidate.peak_time || rowHour < 0) return;
        const candidateDay = new Date(candidate.peak_time).toISOString().slice(0, 10);
        const candidateHour = new Date(candidate.peak_time).getUTCHours();
        if (candidateDay !== rowDay && Math.abs(candidateHour - rowHour) <= 2) {
          related.push(candidate);
          used.add(candidateIndex);
        }
      });
      groupedRows.push({
        row,
        recurring: related.length > 1,
        count: related.length,
        days: new Set(related.map((item) => item.peak_time ? new Date(item.peak_time).toISOString().slice(0, 10) : '')).size,
        durations: related.map((item) => durationMinutesFromBounds(item.start, item.end) ?? item.duration_min ?? 0),
        maxPeak: Math.max(...related.map((item) => item.peak || 0)),
        ctrlM: findCtrlM(row),
      });
    });

    const firstSeries = Object.values(vmData.series || {}).find((a) => a && a.length > 1);
    let grainLabel = 'auto';
    if (firstSeries && firstSeries.length >= 2) {
      const gapMin = (new Date(firstSeries[1].t).getTime() - new Date(firstSeries[0].t).getTime()) / 60000;
      grainLabel = gapMin < 60 ? `${gapMin.toFixed(0)}min` : `${(gapMin / 60).toFixed(0)}h`;
    }

    let insight = '';
    if (rows.length) {
      const top = rows[0];
      const label = shortMetric(top.metric);
      const topIsCpu = /Percentage CPU/i.test(top.metric);
      const cpuStat = vmData.stats?.['Percentage CPU'];
      const cpuP95 = cpuStat?.p95 ?? cpuStat?.mean ?? null;
      const topPeak = (top.peak ?? 0).toFixed(1);
      const topCtrlM = findCtrlM(top);

      // The window P95 is a BASELINE statistic, not an impact measure. Calling a
      // low P95 "minimal CPU impact" next to a critical CPU spike is a direct
      // self-contradiction — and it used to be appended even when the dominant
      // anomaly WAS CPU, producing "pressure on CPU ...; minimal CPU impact".
      // Peak-vs-baseline is the honest reading: a high peak over a low P95 is an
      // isolated burst; a high peak over a high P95 is sustained load.
      const shape = (peak: number, p95: number | null) => {
        if (p95 == null) return null;
        return peak - p95 >= 40
          ? `an isolated burst against a ${p95.toFixed(0)}% P95 baseline for this window, not sustained load`
          : `consistent with sustained load (P95 baseline ${p95.toFixed(0)}%)`;
      };

      if (topIsCpu) {
        // Same metric — describe the spike against its own baseline instead of
        // restating CPU a second time with an opposing adjective.
        const s = shape(top.peak ?? 0, cpuP95);
        insight = `Dominant signal on ${deepDiveVm}: CPU peaked ${topPeak}%${s ? ` — ${s}` : ''}.`;
      } else {
        // Different metric — CPU is genuine cross-metric context. Word it as a
        // baseline reading so it cannot be misread as a competing root cause.
        const cpuContext = cpuP95 != null
          ? `CPU stayed near its ${cpuP95.toFixed(0)}% P95 baseline over the same window`
          : 'CPU context unavailable for this window';
        insight = /Available Memory/i.test(top.metric)
          ? `Dominant signal on ${deepDiveVm}: available memory fell to about ${topPeak}%; ${cpuContext}.`
          : `Dominant signal on ${deepDiveVm}: ${label} peaked ${topPeak}%; ${cpuContext}.`;
      }
      if (topCtrlM?.heaviest) {
        insight += ` Ctrl-M correlation: ${topCtrlM.heaviest} (${(topCtrlM.heaviest_hrs || 0).toFixed(1)}h) was running concurrently \u2014 ${topCtrlM.concurrent_jobs} job(s) overlapped this window (time overlap only, not proof of cause).`;
      }
    }

    return {
      rows, groupedRows, grainLabel, datapoints: firstSeries?.length || 0, insight,
      // Metrics with stats but NO spike events \u2014 excludes anything already
      // shown in the anomaly table above, so this line is genuinely "the rest",
      // not a duplicate of what's already flagged (7d).
      normalMetrics: normalMetricLabels(vmData.stats || {}, spikes),
      ctrlMActive: attributionRows.length > 0,
    };
  }, [deepDive, deepDiveVm]);
  const selectedBaselineConfidence = deepDive?.vms?.[deepDiveVm]?.baseline_confidence;

  if (!data.resource || servers.length === 0) {
    return (
      <Paper className={`${classes.panel} kpi-card`} elevation={0}>
        <Typography variant="h6">Resource Review</Typography>
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload a resource report in Upload &amp; Intake, or fetch live Azure metrics below, to populate this view.
        </Typography>
        <AzureConnectionCard authInfo={azureAuth} onOpen={() => setAzureModalOpen(true)} />
        <AzureFetchModal open={azureModalOpen} autoStartAuth={azureAuth?.method !== 'browser'} onClose={() => setAzureModalOpen(false)} onFetched={handleFetched} onAuthChanged={setAzureAuth} />
      </Paper>
    );
  }

  // Every coverage denominator anchors on the SAME fleet total the Servers card
  // shows. These used to divide by known_servers (telemetry-bearing hosts only),
  // so a fully-resolved fleet rendered "12/12" directly beside a "13 Servers"
  // headline and read as an arithmetic error rather than a documented exclusion.
  const fleetTotal = servers.length;
  const excludedNote = fleetKpis?.image_only
    ? ` \u00b7 ${fleetKpis.image_only} excluded (no telemetry)`
    : '';

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">Resource Review</Typography>

      <AzureConnectionCard authInfo={azureAuth} onOpen={() => setAzureModalOpen(true)} />
      <AzureFetchModal open={azureModalOpen} autoStartAuth={azureAuth?.method !== 'browser'} onClose={() => setAzureModalOpen(false)} onFetched={handleFetched} onAuthChanged={setAzureAuth} />

      <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 12, marginTop: 16, marginBottom: 16 }}>
        <KpiStatCard
          label="Servers"
          value={servers.length}
          sub={`${serverTypeCounts.APP || 0} APP \u00b7 ${serverTypeCounts.DB || 0} DB \u00b7 ${serverTypeCounts.SRE || 0} SRE${fleetKpis?.image_only ? ` \u00b7 ${fleetKpis.image_only} no telemetry` : ''}`}
          accent="#3b82f6"
        />
        {isAzureSource && <KpiStatCard label="Source" value="Azure Monitor" valueColor="#22d3ee" sub={`Live \u00b7 last ${hoursBack}h`} accent="#22d3ee" />}
        {fleetKpis ? (
          <>
            <KpiStatCard
              label="Fleet vCPUs"
              value={fleetKpis.total_vcpus != null ? fleetKpis.total_vcpus.toLocaleString() : '\u2014'}
              valueColor={fleetKpis.total_vcpus != null ? '#4a9eff' : '#6b7db3'}
              sub={fleetKpis.vcpus_reporting ? `${fleetKpis.vcpus_reporting}/${fleetTotal} VM capacities resolved${excludedNote}` : 'Azure Compute capacity unavailable'}
              accent="#4a9eff"
            />
            <KpiStatCard label="Fleet Grade" value={fleetKpis.fleet_grade || '?'} sub={`Score ${(fleetKpis.fleet_score || 0).toFixed(1)}/100`} accent="#a855f7" />
            {fleetKpis.cpu_reporting === 0 ? (
              <KpiStatCard label="Avg CPU" value="—" valueColor="#6b7db3" sub="0 servers reporting CPU" accent="#6b7db3" />
            ) : (
              <MiniGauge label="Avg CPU" value={fleetKpis.avg_cpu || 0} threshold={80} sub={`${fleetKpis.cpu_reporting}/${fleetTotal} reporting · ${overThreshold.cpu ? `${overThreshold.cpu} host(s) ≥ 80%` : 'none ≥ 80%'}`} />
            )}
            {fleetKpis.mem_reporting === 0 ? (
              <KpiStatCard label="Avg Memory" value="—" valueColor="#6b7db3" sub="0 servers reporting memory" accent="#6b7db3" />
            ) : (
              <MiniGauge
                label="Avg Memory"
                value={fleetKpis.avg_mem || 0}
                threshold={80}
                overrideColor={memSeverity?.color}
                sub={!memSeverity || memSeverity.dbCount === 0 ? `${fleetKpis.mem_reporting}/${fleetTotal} reporting · threshold 70/80%` : memSeverity.dbExpectedCount === memSeverity.dbCount ? `${fleetKpis.mem_reporting}/${fleetTotal} reporting · ${memSeverity.dbExpectedCount}/${memSeverity.dbCount} DB host profile` : `${fleetKpis.mem_reporting}/${fleetTotal} reporting · ${memSeverity.dbExpectedCount}/${memSeverity.dbCount} DB in expected band${memSeverity.dbHighCount ? `, ${memSeverity.dbHighCount} above 92%` : `, ${memSeverity.dbCount - memSeverity.dbExpectedCount} below 80%`}`}
                tooltip={memSeverity ? `${memSeverity.dbCount} DB server(s): ${memSeverity.dbExpectedCount} in the configured 80\u201392% host-memory profile, ${memSeverity.dbHighCount} above it. Azure cannot verify SGA/PGA from this host metric alone. ${memSeverity.total - memSeverity.dbCount} non-DB server(s): ${memSeverity.nonDbCritCount} \u2265 80%, ${memSeverity.nonDbWarnCount} \u2265 70%. Color reflects these tallies directly \u2014 same facts Fleet Diagnosis uses below.` : undefined}
              />
            )}
            {fleetKpis.disk_reporting === 0 ? (
              <KpiStatCard label="Avg Disk" value="—" valueColor="#6b7db3" sub="0 servers reporting disk" accent="#6b7db3" />
            ) : (
              <MiniGauge label="Avg Disk" value={fleetKpis.avg_disk || 0} threshold={85} sub={`${fleetKpis.disk_reporting}/${fleetTotal} reporting · ${overThreshold.disk ? `${overThreshold.disk} host(s) ≥ 85%` : 'none ≥ 85%'}`} />
            )}
            <KpiStatCard label="Health" accent="#f43f5e" value={
              <Box display="flex" alignItems="flex-start" style={{ gap: 10 }}>
                {[
                  { n: fleetKpis.n_critical || 0, label: 'CRIT', color: '#f43f5e' },
                  { n: fleetKpis.n_warning || 0, label: 'WARN', color: '#f59e0b' },
                  { n: fleetKpis.n_healthy || 0, label: 'OK', color: '#10d96e' },
                  ...(fleetKpis.image_only ? [{ n: fleetKpis.image_only, label: 'NO DATA', color: '#6b7db3' }] : []),
                ].map((b) => (
                  <Box key={b.label} display="flex" flexDirection="column" alignItems="center" style={{ gap: 3 }}>
                    <span style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: 32, height: 32, borderRadius: 8, fontSize: 15, fontWeight: 800, color: b.color, background: `${b.color}1a`, border: `1px solid ${b.color}4d` }}>{b.n}</span>
                    <span style={{ fontSize: 8, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em', color: b.color }}>{b.label}</span>
                  </Box>
                ))}
              </Box>
            } />
          </>
        ) : (
          <>
            <MiniGauge label="Avg CPU" value={fleetAvg.cpu} threshold={80} sub="Threshold 80%" />
            <MiniGauge
              label="Avg Memory"
              value={fleetAvg.mem}
              threshold={80}
              overrideColor={memSeverity?.color}
              sub={!memSeverity || memSeverity.dbCount === 0 ? 'Threshold 70/80%' : `${memSeverity.dbExpectedCount}/${memSeverity.dbCount} DB host profile`}
            />
            <MiniGauge label="Avg Disk" value={fleetAvg.disk} threshold={85} sub="Threshold 85%" />
            <KpiStatCard label="Fleet Health" value={fleetAvg.health.toFixed(0)} sub="Score /100" accent="#a855f7" />
          </>
        )}
      </Box>

      {/* \u2500\u2500 Fleet Diagnosis \u2014 ported from renderResourceExecutiveSummary() (app.js).
          True verdict after filtering aggregation-artifact false alarms, with
          root-cause candidates (or monitoring notes when all are expected DB
          memory allocation) and a 2-line executive summary. \u2500\u2500 */}
      {execSummary && (() => {
        const vc = VERDICT_COLOR[execSummary.verdict] || '#6b7db3';
        // New API contract keeps expected DB allocation separate from actual
        // bottlenecks.  The fallback supports a cached response from an older
        // API while the backend is being restarted locally.
        const monitoringNotes = execSummary.monitoring_notes || execSummary.bottlenecks.filter((b) => b.issues.join(' ').includes('expected range for DB'));
        const actualBottlenecks = execSummary.bottlenecks.filter((b) => !b.issues.join(' ').includes('expected range for DB'));
        const allExpected = monitoringNotes.length > 0 && actualBottlenecks.length === 0;
        const diagnosisItems = allExpected ? monitoringNotes : actualBottlenecks;
        const verdictDisplay = execSummary.verdict === 'HEALTHY' && allExpected ? 'HEALTHY \u2014 EXPECTED ALLOCATION' : execSummary.verdict;
        return (
          <Box className={classes.section} style={{ borderRadius: 12, border: `1px solid ${vc}66`, background: `${vc}0d`, padding: 16 }}>
            <Box display="flex" alignItems="center" style={{ gap: 10 }}>
              <Typography variant="caption" style={{ fontWeight: 800, textTransform: 'uppercase', letterSpacing: '.12em', fontSize: 10, color: '#6b7db3' }}>Fleet Diagnosis</Typography>
              <span className="metric-badge" style={{ color: vc, borderColor: `${vc}80`, background: `${vc}1f` }}>{verdictDisplay}</span>
            </Box>
            <Typography variant="body2" style={{ marginTop: 6, lineHeight: 1.5 }}>{execSummary.verdict_detail}</Typography>

            {execSummary.false_alarms.length > 0 && (
              <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
                <Typography variant="caption" style={{ color: '#22d3ee', fontWeight: 700, fontSize: 9, textTransform: 'uppercase' }}>{'\ud83d\udd2c'} Aggregation Artifacts ({execSummary.false_alarms.length})</Typography>
                {execSummary.false_alarms.map((fa, i) => (
                  <span key={i} className="metric-badge metric-badge-teal" style={{ fontFamily: 'monospace' }}>
                    {fa.host.split('.')[0]} <span style={{ opacity: 0.7, textTransform: 'none', fontWeight: 400 }}>Max {fa.cpu_max.toFixed(0)}% {'\u2192'} Avg {fa.cpu_avg.toFixed(0)}%</span>
                  </span>
                ))}
              </Box>
            )}

            {diagnosisItems.length > 0 && (
              <Box style={{ marginTop: 10 }}>
                <Typography variant="caption" style={{ fontWeight: 800, textTransform: 'uppercase', letterSpacing: '.1em', fontSize: 9, color: allExpected ? '#6b7db3' : '#f43f5e' }}>
                  {allExpected ? '\ud83d\udccb Monitoring Notes' : '\ud83d\udd25 Root Cause Candidates'} ({diagnosisItems.length})
                </Typography>
                <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 8, marginTop: 6 }}>
                  {diagnosisItems.map((bn, i) => {
                    const isExpected = bn.issues.join(' ').includes('expected range for DB');
                    const cardColor = isExpected ? '#6b7db3' : '#f43f5e';
                    const statusLabel = isExpected ? 'EXPECTED DB LOAD' : bn.status;
                    return (
                      <Box key={i} style={{ borderRadius: 8, border: `1px solid ${cardColor}40`, background: `${cardColor}0a`, padding: 8 }}>
                        <Box display="flex" alignItems="center" style={{ gap: 6 }}>
                          <Typography component="span" variant="body2" style={{ fontFamily: 'monospace', fontWeight: 700, color: cardColor }}>{bn.host.split('.')[0]}</Typography>
                          <span className="metric-badge" style={{ fontSize: 8, color: isExpected ? '#22d3ee' : (STATUS_BADGE[bn.status] ? undefined : '#6b7db3') }}>{statusLabel}</span>
                          <Typography component="span" variant="caption" color="textSecondary">{bn.type}</Typography>
                          {bn.environment && <span className="metric-badge" style={{ fontSize: 8 }}>{bn.environment}</span>}
                        </Box>
                        <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>{bn.issues.join(' \u00b7 ')}</Typography>
                      </Box>
                    );
                  })}
                </Box>
              </Box>
            )}

            <Box style={{ marginTop: 10, paddingTop: 8, borderTop: '1px solid rgba(107,125,179,.2)' }}>
              <Typography variant="body2" style={{ fontWeight: 700 }}>{execSummary.summary_line1}</Typography>
              <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 2 }}>{execSummary.summary_line2}</Typography>
            </Box>
          </Box>
        );
      })()}

      {anomalies.length > 0 && (
        <Box
          style={{ borderRadius: 12, border: '1px solid rgba(245,158,11,.3)', background: 'rgba(245,158,11,.05)', padding: 16, marginBottom: 16 }}
        >
          <Typography variant="subtitle2" style={{ color: '#f59e0b' }}>{'\ud83c\udfaf'} Anomaly Spotlight</Typography>
          <Typography variant="caption" color="textSecondary">Servers whose metrics deviate significantly (|z| {'\u2265'} 2.0) from the fleet.</Typography>
          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8, marginTop: 8 }}>
            {anomalies.slice(0, 9).map((anomaly, index) => (
              <Box key={`${anomaly.host}-${anomaly.metric}-${index}`} className="insight-card warning" style={{ padding: 10 }}>
                <Typography variant="body2" style={{ fontWeight: 700 }}>{anomaly.host}</Typography>
                <Typography variant="caption" color="textSecondary">
                  {anomaly.metric}: {anomaly.value.toFixed(1)} (z={anomaly.z.toFixed(2)})
                </Typography>
              </Box>
            ))}
          </Box>
        </Box>
      )}

      {/* \u2500\u2500 Server Resource Utilization \u2014 per-server bar rows, ported from
          renderResourceHeatmap() (app.js), incl. Avg/Max/Min aggregation toggle,
          legend, disk-unavailable banner, and full tooltip parity. \u2500\u2500 */}
      {utilizationRows.length > 0 && (
        <Box className={classes.section}>
          <Box display="flex" alignItems="center" justifyContent="space-between" style={{ flexWrap: 'wrap', gap: 8 }}>
            <Box>
              <Typography variant="subtitle2">Server Resource Utilization</Typography>
              <Typography variant="caption" color="textSecondary">
                Top {utilizationRows.length} of {servers.length} server(s) visualised below {'\u00b7'} full searchable detail table further down.
              </Typography>
            </Box>
            <Box display="flex" alignItems="center" style={{ gap: 6 }} title={'How the CPU/Mem/Disk bars are aggregated across the observation window \u2014 same Avg/Max/Min choice Azure Metrics Explorer offers. Avg smooths away a short job-driven spike; Max surfaces the true peak.'}>
              {(['avg', 'max', 'min'] as const).map((m) => (
                <button key={m} onClick={() => setUtilAggMode(m)} style={ddPillStyle(utilAggMode === m)}>{m.charAt(0).toUpperCase() + m.slice(1)}</button>
              ))}
            </Box>
          </Box>

          <Box display="flex" alignItems="center" style={{ gap: 14, flexWrap: 'wrap', marginTop: 8, fontSize: 9 }}>
            <Typography component="span" variant="caption" style={{ fontWeight: 700, color: '#f0f4ff' }}>Legend:</Typography>
            <Box display="flex" alignItems="center" style={{ gap: 5 }}><span style={{ width: 10, height: 10, borderRadius: 2, background: 'linear-gradient(135deg,#059669,#34d399)', display: 'inline-block' }} /><Typography component="span" variant="caption" color="textSecondary">OK</Typography></Box>
            <Box display="flex" alignItems="center" style={{ gap: 5 }}><span style={{ width: 10, height: 10, borderRadius: 2, background: 'linear-gradient(135deg,#d97706,#fbbf24)', display: 'inline-block' }} /><Typography component="span" variant="caption" color="textSecondary">Warning</Typography></Box>
            <Box display="flex" alignItems="center" style={{ gap: 5 }}><span style={{ width: 10, height: 10, borderRadius: 2, background: 'linear-gradient(135deg,#dc2626,#f87171)', display: 'inline-block' }} /><Typography component="span" variant="caption" color="textSecondary">Critical</Typography></Box>
            <Box display="flex" alignItems="center" style={{ gap: 5 }}><span style={{ width: 10, height: 10, borderRadius: 2, background: `linear-gradient(135deg,${DB_EXPECTED_GRAD[0]},${DB_EXPECTED_GRAD[1]})`, display: 'inline-block' }} /><Typography component="span" variant="caption" color="textSecondary">DB expected</Typography></Box>
            <Typography component="span" variant="caption" color="textSecondary" style={{ fontSize: 8 }}>| Memory = Available % (Azure native, lower = more pressure) {'\u00b7'} Disk = I/O BW consumed % (not storage space) {'\u00b7'} DB servers with 8{'\u2013'}20% memory available match the configured host profile (shown purple; SGA/PGA not verified)</Typography>
          </Box>

          {(() => {
            const noDisk = utilizationRows.filter((s) => resourceAggValue(s, 'disk', utilAggMode) == null).length;
            if (!noDisk) return null;
            return (
              <Box display="flex" alignItems="center" style={{ gap: 8, marginTop: 8, borderRadius: 8, border: '1px solid rgba(245,158,11,.25)', background: 'rgba(245,158,11,.08)', padding: '6px 10px' }}>
                <span style={{ color: '#f59e0b' }}>{'\u26a0'}</span>
                <Typography variant="caption" style={{ color: '#f59e0b', fontWeight: 700 }}>Disk I/O unavailable for {noDisk}/{utilizationRows.length} server{noDisk !== 1 ? 's' : ''}</Typography>
                <Typography variant="caption" color="textSecondary">{'\u2014'} metric not emitted by VM SKU/agent. Confirm OS disk monitoring is enabled in Azure.</Typography>
              </Box>
            );
          })()}

          <Box style={{ marginTop: 8 }}>
            {utilizationRows.map((s) => {
              const cpuVal = resourceAggValue(s, 'cpu', utilAggMode);
              const memUsedVal = resourceAggValue(s, 'mem', utilAggMode);
              const memAvailVal = memUsedVal != null ? 100 - memUsedVal : null;
              const diskVal = resourceAggValue(s, 'disk', utilAggMode);
              const dbExpected = s.mem_status === 'DB_NORMAL' || (s.type === 'DB' && memUsedVal != null && memUsedVal >= 80 && memUsedVal <= 92);
              const cpuColor = cpuVal != null ? bandColor(cpuVal, 60, 80) : '#475569';
              const memColor = dbExpected ? DB_EXPECTED_GRAD[1] : memAvailVal != null ? bandColor(memAvailVal, 20, 40, true) : '#475569';
              const diskColor = diskVal != null ? bandColor(diskVal, 60, 80) : '#475569';
              const aggNote = (curVal: number | null, avgVal: number | null | undefined) => {
                if (utilAggMode === 'avg' || curVal == null || avgVal == null || Math.abs(curVal - avgVal) <= 5) return null;
                return `avg ${avgVal.toFixed(0)}%`;
              };
              const cpuNote = aggNote(cpuVal, s.cpu_avg_pct ?? s.cpu_used);
              const bars = [
                { label: 'CPU', val: cpuVal, color: cpuColor, note: cpuNote, title: cpuVal != null ? `${cpuVal.toFixed(1)}% (${utilAggMode}; threshold 80%)` : 'No data' },
                { label: 'Mem avail', val: memAvailVal, color: memColor, note: null, title: dbExpected ? `${(memAvailVal ?? 0).toFixed(1)}% available (${utilAggMode}) \u2014 configured DB host profile (8\u201320% available; Azure does not verify SGA/PGA)` : memAvailVal != null ? `${memAvailVal.toFixed(1)}% available (${utilAggMode}; threshold 20%)` : 'No data' },
                { label: 'Disk I/O', val: diskVal, color: diskColor, note: null, title: diskVal != null ? `${diskVal.toFixed(1)}% (${utilAggMode}; threshold 80%)` : 'Disk I/O metric not emitted by this VM SKU/agent.' },
              ];
              return (
                <Box key={s.host} display="flex" alignItems="center" style={{ gap: 12, padding: '4px 0', borderBottom: '1px solid rgba(33,48,96,.2)' }}>
                  <Box display="flex" alignItems="center" style={{ minWidth: 150, flexShrink: 0, gap: 6 }}>
                    <span className="metric-badge" style={{ fontSize: 9 }}>{(s.type || 'APP').toUpperCase()}</span>
                    <Typography variant="caption" style={{ fontFamily: 'monospace', color: '#f0f4ff' }} title={s.host}>{(s.host || '').split('.')[0]}</Typography>
                  </Box>
                  {bars.map((m) => (
                    <Box key={m.label} style={{ flex: 1 }}>
                      <Box title={m.title} style={{ height: 8, borderRadius: 4, background: 'rgba(255,255,255,.06)', overflow: 'hidden', cursor: 'help' }}>
                        {m.val != null && <Box style={{ width: `${Math.min(100, Math.max(0, m.val))}%`, height: '100%', background: m.color }} />}
                      </Box>
                      <Typography variant="caption" style={{ color: m.color, fontSize: 9 }}>{m.label} ({utilAggMode}) {m.val != null ? `${m.val.toFixed(0)}%` : 'N/A'}</Typography>
                      {m.note && <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 8 }}>{m.note}</Typography>}
                    </Box>
                  ))}
                </Box>
              );
            })}
          </Box>
        </Box>
      )}

      <Box className={classes.controls}>
        <TextField size="small" label="Filter by host" value={filter} onChange={(event) => setFilter(event.target.value)} />
        <TextField size="small" select label="Type" value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }} style={{ minWidth: 100 }}>
          <option value="">All</option>
          <option value="APP">APP</option>
          <option value="DB">DB</option>
          <option value="SRE">SRE</option>
        </TextField>
        <TextField size="small" select label="Environment" value={envFilter} onChange={(event) => setEnvFilter(event.target.value)} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }} style={{ minWidth: 120 }}>
          <option value="">All</option>
          <option value="PROD">PROD</option>
          <option value="TEST">TEST</option>
          <option value="UAT">UAT</option>
          <option value="DEV">DEV</option>
        </TextField>
        {productGroups.length > 0 && (
          <TextField size="small" select label="Product Group" value={productGroupFilter} onChange={(event) => setProductGroupFilter(event.target.value)} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }} style={{ minWidth: 140 }}>
            <option value="">All</option>
            {productGroups.map((pg) => <option key={pg} value={pg}>{pg}</option>)}
          </TextField>
        )}
        <TextField size="small" select label="Status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} SelectProps={{ native: true }} InputLabelProps={{ shrink: true }} style={{ minWidth: 110 }}>
          <option value="">All</option>
          <option value="Critical">Critical</option>
          <option value="Warning">Warning</option>
          <option value="Healthy">Healthy</option>
          <option value="Unknown">Unknown</option>
        </TextField>
        <Typography variant="caption" color="textSecondary">{sorted.length} of {servers.length} servers</Typography>
      </Box>
      <Table size="small" className="pe-table" aria-label="Resource review table">
        <TableHead>
          <TableRow>
            <TableCell>
              <TableSortLabel active={sortKey === 'host'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('host')}>Server</TableSortLabel>
            </TableCell>
            <TableCell>Type</TableCell>
            <TableCell>Env</TableCell>
            <TableCell align="right" title={'Most recent CPU used % (last data point in the window). Role-aware threshold: DB/SRE/APP each have their own OK/Warn band.'}>
              <TableSortLabel active={sortKey === 'cpu_used'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('cpu_used')}>CPU %</TableSortLabel>
            </TableCell>
            <TableCell align="right" title={'Period average CPU used % \u2014 the denominator for aggregation-artifact detection (Max high but Avg low = brief spike, not sustained pressure).'}>CPU Avg</TableCell>
            <TableCell align="center" title="30-day CPU trend from the Metrics Deep Dive timeseries (load it below to populate). Hover the sparkline for avg/peak detail.">Trend</TableCell>
            <TableCell align="right" title={'Memory USED % (100 − Azure Available Memory %). DB servers in the configured 80–92% used host-memory profile are tagged “DB expected”; Azure cannot verify SGA/PGA from this host metric alone.'}>
              <TableSortLabel active={sortKey === 'mem_used'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('mem_used')}>Mem Used %</TableSortLabel>
            </TableCell>
            <TableCell align="right" title={'Total memory capacity (GB) reported by the source \u2014 not always available depending on collection method.'}>Mem GB</TableCell>
            <TableCell align="right" title={'Azure OS/Data Disk Bandwidth Consumed % — NOT storage space used. 0.3% = near-idle I/O. Warns at 80% of the provisioned IOPS/throughput quota.'}>
              <TableSortLabel active={sortKey === 'disk_used_max'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('disk_used_max')}>Disk %</TableSortLabel>
            </TableCell>
            <TableCell>Status</TableCell>
            <TableCell>Size</TableCell>
            <TableCell align="right" title="Usable virtual CPUs from Azure Compute capacity metadata. Constrained VM sizes use vCPUsAvailable where Azure exposes it; regional VM-size metadata is the fallback.">vCPUs</TableCell>
            <TableCell>Source</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {sorted.map((server) => {
            const status = server.status || 'Unknown';
            const cpuOk = server.role_cpu_ok ?? 60;
            const cpuWarn = server.role_cpu_warn ?? 80;
            const cpuAvail = server.cpu_available !== false && server.cpu_used != null;
            const memAvail = server.mem_available !== false && server.mem_used != null;
            const memGbAvail = server.mem_available !== false && server.mem_gb != null;
            const diskAvail = server.disk_used_max != null;
            const dbExpected = server.mem_status === 'DB_NORMAL';
            const memColor = !memAvail ? undefined : dbExpected ? DB_EXPECTED_COLOR : (server.mem_used || 0) >= 80 ? '#f43f5e' : (server.mem_used || 0) >= 60 ? '#f59e0b' : '#10d96e';
            const cpuColor = !cpuAvail ? undefined : bandColor(server.effective_cpu ?? server.cpu_used ?? 0, cpuOk, cpuWarn);
            const statusReasons: string[] = [];
            if (cpuAvail && (server.cpu_used || 0) >= cpuWarn) statusReasons.push(`CPU ${(server.cpu_used || 0).toFixed(1)}% used \u2265 ${cpuWarn}% threshold`);
            if (memAvail && !dbExpected && (server.mem_used || 0) >= 80) statusReasons.push(`Memory ${(100 - (server.mem_used || 0)).toFixed(1)}% available \u2264 20% floor`);
            if (memAvail && dbExpected) statusReasons.push('Memory within configured DB host-memory profile (SGA/PGA not verified)');
            if (diskAvail && (server.disk_used_max || 0) >= 80) statusReasons.push(`Disk ${(server.disk_used_max || 0).toFixed(1)}% used \u2265 80% threshold`);
            if (server.dual_pressure) statusReasons.push('Dual pressure: CPU+Memory both critical');
            if (server.agg_trap) statusReasons.push(`Aggregation trap: peak=${(server.cpu_used || 0).toFixed(1)}%, avg=${(server.cpu_avg_pct || 0).toFixed(1)}%`);
            const statusTooltip = statusReasons.length ? `${status}: ${statusReasons.join('; ')}` : (status === 'Healthy' ? 'Healthy: all metrics within thresholds' : status === 'Unknown' ? 'No metric data available from source' : status);
            const driverLine = (status === 'Warning' || status === 'Critical') && statusReasons.length ? `\u2191 ${statusReasons[0]}` : null;
            const deepDiveVmForServer = findDeepDiveSeries(deepDive, server.host || server.server || '');
            return (
            <TableRow key={`${server.host}-${server.type || 'APP'}`}>
              <TableCell style={{ fontFamily: 'monospace' }} title={server.host}>
                {server.server || server.host}
                {server.dual_pressure && <span className="metric-badge metric-badge-red" style={{ fontSize: 8, marginLeft: 4 }} title={'DUAL PRESSURE: CPU \u226580% + Memory \u226585% \u2014 severe resource exhaustion'}>DUAL</span>}
              </TableCell>
              <TableCell>{server.type || '\u2014'}</TableCell>
              <TableCell>
                {server.environment ? <span className="metric-badge" style={{ fontSize: 9 }}>{server.environment}</span> : '\u2014'}
              </TableCell>
              <TableCell align="right" style={{ color: cpuColor }} title={cpuAvail ? undefined : 'Data unavailable'}>
                {cpuAvail ? `${(server.cpu_used || 0).toFixed(1)}%` : 'N/A'}
                {server.agg_trap && <span className="metric-badge metric-badge-teal" style={{ fontSize: 8, marginLeft: 4 }} title={`Aggregation Artifact: Max CPU ${(server.cpu_used || 0).toFixed(1)}% but Avg only ${(server.cpu_avg_pct || 0).toFixed(1)}% \u2014 brief spike, server is healthy`}>BRIEF SPIKE</span>}
              </TableCell>
              <TableCell align="right" title={server.cpu_avg_pct != null ? undefined : 'Insufficient data for period average'}>
                {server.cpu_avg_pct != null ? `${server.cpu_avg_pct.toFixed(1)}%` : 'N/A'}
              </TableCell>
              <TableCell align="center">
                {deepDiveVmForServer ? (
                  <Sparkline
                    points={deepDiveVmForServer}
                    color={bandColor(deepDiveVmForServer[deepDiveVmForServer.length - 1]?.v ?? 0, cpuOk, cpuWarn)}
                    fixed0to100={false}
                  />
                ) : (
                  <span title="Load 30d metrics below to see this server's CPU trend" style={{ color: '#6b7db3' }}>{'\u2014'}</span>
                )}
              </TableCell>
              <TableCell align="right" style={{ color: memColor }} title={memAvail ? undefined : 'Data unavailable'}>
                {memAvail ? `${(server.mem_used || 0).toFixed(1)}%` : 'N/A'}
                {server.mem_status && (
                  <Typography component="span" variant="caption" title={dbExpected ? 'Configured DB host-memory profile. 8\u201320% available is expected; Azure does not verify SGA/PGA.' : 'DB server below expected available (<8%). Check for memory pressure.'} style={{ display: 'block', fontSize: 8, color: server.mem_status === 'DB_HIGH' ? '#f43f5e' : DB_EXPECTED_COLOR, cursor: 'help' }}>
                    {dbExpected ? 'DB host profile' : 'DB high'}
                  </Typography>
                )}
              </TableCell>
              <TableCell align="right" title={memGbAvail ? undefined : 'Memory capacity not available from source'}>{memGbAvail ? server.mem_gb!.toFixed(1) : 'N/A'}</TableCell>
              <TableCell align="right" title={diskAvail ? undefined : 'Disk data unavailable'}>{diskAvail ? `${(server.disk_used_max || 0).toFixed(1)}%` : 'N/A'}</TableCell>
              <TableCell>
                <span className={`status-dot ${STATUS_DOT[status] || 'status-dot-muted'}`} style={{ marginRight: 6 }} />
                <span className={`metric-badge ${STATUS_BADGE[status] || 'metric-badge-blue'}`} title={statusTooltip} style={{ cursor: 'help' }}>{status}</span>
                {driverLine && <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 8 }}>{driverLine}</Typography>}
                {dbExpected && <Typography variant="caption" style={{ display: 'block', fontSize: 8, color: DB_EXPECTED_COLOR }}>{'Host profile expected \u2014 SGA/PGA not verified'}</Typography>}
              </TableCell>
              <TableCell style={{ fontSize: 11 }}>{server.vm_size || '\u2014'}</TableCell>
              <TableCell align="right" title={server.vcpus != null ? (server.vcpu_source === 'regional_vm_size' ? 'Azure regional VM-size metadata: number_of_cores' : `Azure SKU capability: ${server.vcpu_source || 'vCPUs'}`) : 'Usable vCPU count was not returned by either Azure SKU capabilities or regional VM-size metadata'}>{server.vcpus != null ? server.vcpus : 'N/A'}</TableCell>
              <TableCell>
                {server.source ? <span className={`metric-badge ${SOURCE_BADGE[server.source] || 'metric-badge-blue'}`} style={{ fontSize: 8 }}>{server.source}</span> : '\u2014'}
              </TableCell>
            </TableRow>
            );
          })}
        </TableBody>
      </Table>

      {/* \u2500\u2500 Metrics Deep Dive \u2014 Azure Monitor timeseries, patterns, heatmaps, ported
          from _renderDeepDiveBanner()/_renderDeepDivePatterns()/_renderDeepDiveHeatmap() (app.js).
          Only shown when the loaded servers came from a live Azure Monitor fetch. \u2500\u2500 */}
      {isAzureSource && (
        <Box className={classes.section} style={{ borderRadius: 12, border: '1px solid #213060', background: 'rgba(17,29,54,.5)', padding: 16 }}>
          <Box display="flex" alignItems="center" justifyContent="space-between" style={{ flexWrap: 'wrap', gap: 8 }}>
            <Box>
              <Typography variant="subtitle2">Metrics Deep Dive {'\u2014'} Time-Series &amp; Anomaly Detection</Typography>
              <Typography variant="caption" color="textSecondary" style={{ display: 'block' }}>Critical anomalies only {'\u2014'} normal &amp; moderate filtered out. Pattern detection across fleet (z-score {'\u2265'} 3{'\u03c3'})</Typography>
              <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 9, opacity: 0.8 }}>Metric source: Azure Monitor Average + timestamp-aligned Maximum aggregation, with automatic grain by selected time range.</Typography>
            </Box>
            <Button size="small" variant="contained" color="primary" onClick={() => handleLoadDeepDive()} disabled={deepDiveBusy}>
              {deepDiveBusy ? <CircularProgress size={16} color="inherit" /> : 'Load Time-Series'}
            </Button>
          </Box>

          {/* Time Range Picker \u2014 preset pills + custom absolute window */}
          <Box display="flex" alignItems="center" style={{ gap: 6, flexWrap: 'wrap', marginTop: 10 }}>
            <Typography component="span" variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em', color: '#6b7db3', marginRight: 4 }}>Time Range</Typography>
            {[{ h: 1, l: '1h' }, { h: 6, l: '6h' }, { h: 12, l: '12h' }, { h: 24, l: '24h' }, { h: 48, l: '48h' }, { h: 72, l: '3d' }, { h: 168, l: '7d' }, { h: 360, l: '15d' }, { h: 720, l: '30d' }].map((p) => (
              <button key={p.h} onClick={() => { setHoursBack(p.h); setDdCustomActive(false); clearDeepDiveState(true); }} style={ddPillStyle(!ddCustomActive && hoursBack === p.h)}>{p.l}</button>
            ))}
            <button onClick={() => setDdCustomPickerOpen((v) => !v)} style={ddPillStyle(ddCustomActive)} title={'Pick an exact start and end instead of a rolling window \u2014 for scoping the deep dive to one batch night or one incident.'}>{'\ud83d\udcc5'} Custom{'\u2026'}</button>
          </Box>
          {ddCustomPickerOpen && (
            <Box display="flex" alignItems="flex-end" style={{ gap: 8, flexWrap: 'wrap', marginTop: 8, borderRadius: 8, border: '1px solid #213060', background: 'rgba(10,15,30,.4)', padding: '8px 10px' }}>
              <Box>
                <Typography variant="caption" style={{ display: 'block', fontWeight: 700, fontSize: 8, textTransform: 'uppercase', color: '#6b7db3' }}>From</Typography>
                <input type="datetime-local" step={60} value={ddCustomStart} onChange={(e) => setDdCustomStart(e.target.value)} style={{ background: '#0a0f1e', border: '1px solid #213060', borderRadius: 4, padding: '4px 6px', color: '#e2e8f0', fontSize: 11 }} />
              </Box>
              <Box>
                <Typography variant="caption" style={{ display: 'block', fontWeight: 700, fontSize: 8, textTransform: 'uppercase', color: '#6b7db3' }}>To</Typography>
                <input type="datetime-local" step={60} value={ddCustomEnd} onChange={(e) => setDdCustomEnd(e.target.value)} style={{ background: '#0a0f1e', border: '1px solid #213060', borderRadius: 4, padding: '4px 6px', color: '#e2e8f0', fontSize: 11 }} />
              </Box>
              <Button size="small" variant="outlined" onClick={handleApplyCustomRange} disabled={!ddCustomStart || !ddCustomEnd}>Apply</Button>
              <Button size="small" onClick={handleClearCustomRange} style={{ color: '#6b7db3' }}>Reset to preset</Button>
            </Box>
          )}
          {ddCustomActive && ddCustomStart && ddCustomEnd && (
            <Box display="flex" alignItems="center" style={{ gap: 8, marginTop: 8, borderRadius: 8, border: '1px solid rgba(168,85,247,.3)', background: 'rgba(168,85,247,.08)', padding: '6px 10px' }}>
              <Typography variant="caption" style={{ color: '#a855f7', fontWeight: 700, textTransform: 'uppercase', fontSize: 9 }}>Custom Window</Typography>
              <Typography variant="caption" style={{ fontFamily: 'monospace' }}>{new Date(ddCustomStart).toLocaleString()} {'\u2192'} {new Date(ddCustomEnd).toLocaleString()}</Typography>
              <Button size="small" onClick={handleClearCustomRange} style={{ marginLeft: 'auto', color: '#a855f7', fontSize: 10 }}>{'\u2715'} Clear</Button>
            </Box>
          )}
          {deepDiveError && <Typography variant="caption" color="error" style={{ display: 'block', marginTop: 8 }}>{deepDiveError}</Typography>}

          {deepDive && (
            <>
              {/* Banner */}
              {deepDive.summary && (() => {
                const s = deepDive.summary;
                const hasCritical = s.total_critical > 0;
                const color = hasCritical ? '#f43f5e' : '#10d96e';
                const blDays = deepDive.baseline?.days_observed || 0;
                const blNote = blDays >= 15 ? ` \u00b7 ${blDays.toFixed(0)}-day baseline: pattern analysis active` : blDays >= 2 ? ` \u00b7 ${blDays.toFixed(0)}-day observation (15d recommended for PE baseline)` : '';
                const tzNote = deepDive.window?.timezone ? ` Timezone source: ${deepDive.window.timezone}.` : '';
                return (
                  <Box style={{ marginTop: 12, borderRadius: 10, border: `1px solid ${color}66`, background: `${color}1a`, padding: 12 }}>
                    <Typography variant="body2" style={{ color, fontWeight: 700 }}>
                      {hasCritical ? `${s.total_critical} Critical Anomal${s.total_critical > 1 ? 'ies' : 'y'} \u2014 ${s.affected_vms} VM(s) Affected` : 'Fleet Healthy \u2014 No Critical Anomalies'}
                    </Typography>
                    <Typography variant="caption" color="textSecondary">
                      {s.vm_count} VM(s) analyzed over {s.hours_back}h{blNote}.{tzNote}
                    </Typography>
                  </Box>
                );
              })()}

              {/* Three-way pattern taxonomy \u2014 a DIFFERENT measure from the banner
                  above: the banner counts raw critical SPIKE EVENTS, this counts
                  detected PATTERNS (one pattern groups multiple events) (ADD + IMPROVE). */}
              {baselineQuality && (
                <Box style={{ marginTop: 8, borderRadius: 8, border: `1px solid ${baselineQuality.degraded.length || baselineQuality.initial.length ? 'rgba(245,158,11,.40)' : 'rgba(16,217,110,.30)'}`, background: baselineQuality.degraded.length || baselineQuality.initial.length ? 'rgba(245,158,11,.07)' : 'rgba(16,217,110,.05)', padding: '7px 10px' }}>
                  <Typography variant="caption" style={{ display: 'block', fontWeight: 700, color: baselineQuality.degraded.length || baselineQuality.initial.length ? '#f59e0b' : '#10d96e' }}>
                    {baselineQuality.degraded.length
                      ? `Baseline confidence reduced on ${baselineQuality.degraded.length}/${baselineQuality.observed} VM(s)`
                      : baselineQuality.initial.length
                        ? `Initial baseline only on ${baselineQuality.initial.length}/${baselineQuality.observed} VM(s)`
                        : `Longitudinal baseline established across ${baselineQuality.observed} VM(s)`}
                  </Typography>
                  <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 2, fontSize: 9 }}>
                    {baselineQuality.degraded.length
                      ? `${baselineQuality.degraded.slice(0, 3).map((entry) => entry.vm).join(', ')}${baselineQuality.degraded.length > 3 ? ` +${baselineQuality.degraded.length - 3} more` : ''} have fewer than the minimum stored pulls; anomaly results may miss events or overstate them.`
                      : baselineQuality.initial.length
                        ? `Lowest observed history: ${baselineQuality.minPulls} pull${baselineQuality.minPulls === 1 ? '' : 's'}. Basic calibration is available, but ${baselineQuality.matureMinPulls} pulls are required before longitudinal regime comparison is considered mature.`
                        : `Lowest observed history: ${baselineQuality.minPulls} pulls. Per-VM baseline details remain visible in the investigation cards.`}
                  </Typography>
                </Box>
              )}

              {patternTaxonomy && (
                <Box style={{ marginTop: 10, borderRadius: 10, border: '1px solid #213060', background: 'rgba(17,29,54,.5)', padding: 10 }}>
                  <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap' }}>
                    <Typography variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em', fontSize: 9, color: '#6b7db3' }}>Detected Patterns</Typography>
                    <span className="metric-badge" style={{ fontSize: 8, color: '#3b82f6' }}>{patternTaxonomy.byType.cross_vm_correlation} Correlated Across Servers</span>
                    <span className="metric-badge" style={{ fontSize: 8, color: '#f59e0b' }}>{patternTaxonomy.byType.recurring_time} Recurring at a Fixed Hour</span>
                    <span className="metric-badge" style={{ fontSize: 8, color: '#f43f5e' }}>{patternTaxonomy.byType.sustained_pressure} Sustained Pressure</span>
                    {patternTaxonomy.byType.regime_change > 0 && (
                      <span className="metric-badge" style={{ fontSize: 8, color: '#6366f1' }}>{patternTaxonomy.byType.regime_change} Baseline Regime Shift</span>
                    )}
                    {patternTaxonomy.byType.other > 0 && (
                      <span className="metric-badge" style={{ fontSize: 8, color: '#6b7db3' }}>{patternTaxonomy.byType.other} Other</span>
                    )}
                  </Box>
                  <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4, fontSize: 8 }}>
                    {`${patternTaxonomy.total} grouped pattern${patternTaxonomy.total !== 1 ? 's' : ''} \u2014 each pattern carries exactly one type, so the counts above sum to this total. The critical-event count above remains the raw event total.`}
                  </Typography>
                </Box>
              )}

              {regimeShifts.length > 0 && (
                <Box style={{ marginTop: 10, borderRadius: 10, border: '1px solid rgba(99,102,241,.35)', background: 'rgba(99,102,241,.06)', padding: 10 }}>
                  <Typography variant="subtitle2" style={{ color: '#818cf8' }}>Baseline Regime Shifts</Typography>
                  <Typography variant="caption" color="textSecondary" style={{ fontSize: 9, display: 'block' }}>
                    A statistically significant step-change between this pull's mean and the prior pull's baseline for the same host+metric \u2014 not a spike, a shift in the new normal.
                  </Typography>
                  <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 6, marginTop: 6 }}>
                    {regimeShifts.map((p, index) => {
                      const worsening = p.worsening !== false;
                      const color = worsening ? '#f43f5e' : '#10d96e';
                      const vm = p.vms?.[0];
                      return (
                        <Box key={`${vm}-${p.metric}-${index}`} style={{ borderRadius: 8, border: `1px solid ${color}40`, background: `${color}0d`, padding: 8 }}>
                          <Box display="flex" alignItems="center" style={{ gap: 6 }}>
                            {vm && <Typography component="span" variant="caption" style={{ fontWeight: 700, fontFamily: 'monospace' }}>{renderHostId(vm)}</Typography>}
                            <span className="metric-badge" style={{ fontSize: 7, color }}>{worsening ? '\u2191 worsening' : '\u2193 improving'}</span>
                          </Box>
                          <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 9, marginTop: 2 }}>
                            {p.metric ? shortMetric(p.metric) : 'Metric unavailable'}: {'\u03bc'}={p.mean_prior ?? '\u2014'}% {'\u2192'} {'\u03bc'}={p.mean_recent ?? '\u2014'}%
                          </Typography>
                        </Box>
                      );
                    })}
                  </Box>
                </Box>
              )}

              {correlationGroups.length > 0 && (
                <Box style={{ marginTop: 10, borderRadius: 10, border: '1px solid rgba(34,211,238,.35)', background: 'rgba(34,211,238,.05)', padding: 10 }}>
                  <Box display="flex" alignItems="center" justifyContent="space-between" style={{ gap: 8, flexWrap: 'wrap' }}>
                    <Box>
                      <Typography variant="subtitle2" style={{ color: '#22d3ee' }}>Cross-Server Correlation</Typography>
                      <Typography variant="caption" color="textSecondary" style={{ fontSize: 9 }}>Coincident spikes are evidence for a shared workload or dependency; they are not root-cause proof.</Typography>
                    </Box>
                    {correlatedVms.size > 0 && <Button size="small" variant="outlined" onClick={() => setCorrelatedVms(new Set())}>Clear highlight</Button>}
                  </Box>
                  <Box className="pe-table-shell" style={{ marginTop: 8, overflowX: 'auto' }}>
                    <Table size="small" className="pe-table" aria-label="Cross-server correlation evidence">
                      <TableHead>
                        <TableRow>
                          <TableCell><TableSortLabel active={correlationSort === 'time'} onClick={() => setCorrelationSort('time')}>Window (UTC)</TableSortLabel></TableCell>
                          <TableCell><TableSortLabel active={correlationSort === 'servers'} onClick={() => setCorrelationSort('servers')}>Servers</TableSortLabel></TableCell>
                          <TableCell>Metrics</TableCell>
                          <TableCell align="right"><TableSortLabel active={correlationSort === 'events'} onClick={() => setCorrelationSort('events')}>Events</TableSortLabel></TableCell>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {sortedCorrelationGroups.map((group, index) => {
                          const active = group.vms.some((vm) => correlatedVms.has(vm));
                          return (
                            <TableRow
                              key={`${group.timeUtc}-${group.metrics.join('-')}-${index}`}
                              hover
                              onClick={() => { setCorrelatedVms(new Set(group.vms)); if (group.vms[0]) setDeepDiveVm(group.vms[0]); }}
                              style={{ cursor: 'pointer', background: active ? 'rgba(34,211,238,.09)' : undefined }}
                              title="Highlight related investigation cards and open the first server's evidence."
                            >
                              <TableCell style={{ color: '#22d3ee', fontWeight: 700, whiteSpace: 'nowrap' }}>{group.timeUtc === 'time unavailable' ? 'Selected window' : `${group.timeUtc} UTC`}</TableCell>
                              <TableCell>
                                <Box display="flex" style={{ gap: 5, flexWrap: 'wrap' }}>
                                  {group.vms.map((vm, vmIndex) => <span key={`${vm}-${vmIndex}`} style={{ fontSize: 11, padding: '1px 5px', borderRadius: 5, background: 'rgba(34,211,238,.10)', border: '1px solid rgba(34,211,238,.22)' }}>{renderHostId(vm)}</span>)}
                                </Box>
                              </TableCell>
                              <TableCell>{group.metrics.map(shortMetric).join(' · ') || 'Metric unavailable'}</TableCell>
                              <TableCell align="right">{group.eventCount}</TableCell>
                            </TableRow>
                          );
                        })}
                      </TableBody>
                    </Table>
                  </Box>
                </Box>
              )}

              {/* Heatmap */}
              {deepDive.heatmap && (
                <Box style={{ marginTop: 12 }}>
                  <Box display="flex" alignItems="center" justifyContent="space-between">
                    <Typography variant="subtitle2">Fleet Heatmap</Typography>
                    <Box display="flex" style={{ gap: 4 }}>
                      {(['cpu', 'memory', 'disk'] as HeatmapMetric[]).map((m) => (
                        <Button key={m} size="small" variant={heatmapMetric === m ? 'contained' : 'outlined'} onClick={() => setHeatmapMetric(m)}>{m.toUpperCase()}</Button>
                      ))}
                    </Box>
                  </Box>
                  {fleetHeatmapView ? (
                    <>
                      <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4, fontSize: 9 }}>
                        {heatmapMetric === 'memory'
                          ? 'Memory is Azure available % (not used %). Each cell shows the lowest available-memory value in its time bucket; lower availability is higher risk.'
                          : 'Each cell shows the highest observed value in its time bucket; higher utilization is higher risk.'}
                        {' Outlined hatched cells mean this metric was not emitted by Azure Monitor; they are not healthy samples.'}
                        {fleetHeatmapView.bucketSize > 1 ? ` ${fleetHeatmapView.bucketSize} Monitor samples are combined per visible cell for readability.` : ''}
                      </Typography>
                      <Box className="pe-table-shell pe-heatmap-shell" style={{ marginTop: 8, maxHeight: 360, overflow: 'auto' }}>
                        <table className="pe-heatmap-table" aria-label={`Fleet ${heatmapMetric} heatmap`} style={{ borderCollapse: 'separate', borderSpacing: 2, minWidth: Math.max(680, 170 + fleetHeatmapView.columns.length * 18) }}>
                          <thead>
                            <tr>
                              <th style={{ position: 'sticky', left: 0, zIndex: 2, background: '#111d36', minWidth: 150, textAlign: 'left' }}>Server</th>
                                  {/* Keep every coloured bucket while showing only about a dozen
                                      horizontal labels; the remaining columns stay available by hover. */}
                                  {fleetHeatmapView.columns.map((column, index) => {
                                    const labelStride = Math.max(1, Math.ceil(fleetHeatmapView.columns.length / 12));
                                    const showLabel = index % labelStride === 0 || index === fleetHeatmapView.columns.length - 1;
                                    return (
                                      <th key={`${column.title}-${index}`} title={column.title} style={{ minWidth: showLabel ? 58 : 18, padding: '4px 3px', fontSize: 8, whiteSpace: 'nowrap', color: showLabel ? '#94a3b8' : 'transparent' }}>{showLabel ? column.label : '·'}</th>
                                    );
                                  })}
                            </tr>
                          </thead>
                          <tbody>
                            {fleetHeatmapView.rows.map((row) => (
                              <tr key={row.name}>
                                <td style={{ position: 'sticky', left: 0, zIndex: 1, background: '#111d36', padding: '3px 8px', fontFamily: 'monospace', whiteSpace: 'nowrap' }}>{row.name}</td>
                                {row.values.map((value, index) => {
                                  const state = fleetHeatmapCellLabel(value, heatmapMetric);
                                  return (
                                    <td
                                      key={index}
                                      aria-label={`${row.name}, ${fleetHeatmapView.columns[index].title}: ${state}`}
                                      title={`${row.name}\n${fleetHeatmapView.columns[index].title}\n${state}`}
                                      style={{ width: 18, minWidth: 18, height: 20, padding: 0, background: fleetHeatmapCellColor(value, heatmapMetric), border: value == null ? '1px solid rgba(148,163,184,.72)' : '1px solid rgba(255,255,255,.16)' }}
                                    />
                                  );
                                })}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </Box>
                      <Box display="flex" alignItems="center" style={{ gap: 10, flexWrap: 'wrap', marginTop: 6 }}>
                        <Typography variant="caption" color="textSecondary" style={{ fontSize: 9 }}>Legend:</Typography>
                        {/* Distinct glyph per band, not just a recoloured square:
                            severity was previously encoded by colour alone, which
                            disappears in greyscale print and for red-green CVD. */}
                        <Typography variant="caption" style={{ fontSize: 9, color: '#10d96e' }}>{'\u25cf healthy'}</Typography>
                        <Typography variant="caption" style={{ fontSize: 9, color: '#f59e0b' }}>{'\u25b2 watch'}</Typography>
                        <Typography variant="caption" style={{ fontSize: 9, color: '#f43f5e' }}>{'\u25a0 pressure'}</Typography>
                        <Typography variant="caption" color="textSecondary" style={{ fontSize: 9 }}><span style={{ display: 'inline-block', width: 10, height: 10, marginRight: 3, verticalAlign: '-1px', border: '1px solid rgba(148,163,184,.9)', background: 'repeating-linear-gradient(135deg, rgba(100,116,139,.45) 0, rgba(100,116,139,.45) 2px, rgba(15,23,42,.92) 2px, rgba(15,23,42,.92) 5px)' }} />metric not emitted</Typography>
                      </Box>
                    </>
                  ) : (
                    <Typography variant="caption" color="textSecondary">No {heatmapMetric} metric was emitted by the selected VMs in this window.</Typography>
                  )}
                </Box>
              )}

              {/* Requires Investigation — critical VM card grid, ported from
                  _renderDeepDiveCharts()/_renderVmServerCard() (app.js). */}
              {ddCards.critical.length > 0 && (
                <Box style={{ marginTop: 12 }}>
                  <Box display="flex" alignItems="center" style={{ gap: 8 }}>
                    <span>{'\ud83d\udea8'}</span>
                    <Typography variant="subtitle2" style={{ color: '#f43f5e', textTransform: 'uppercase', letterSpacing: '.08em', fontSize: 11 }}>
                      {'Requires Investigation \u2014 '}{ddCards.critical.length} Server{ddCards.critical.length > 1 ? 's' : ''}
                    </Typography>
                  </Box>
                  <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 9, marginTop: 2 }}>
                    {ddCards.critical.length} of {ddCards.critical.length + ddCards.clean.length} analyzed VM(s) flagged {'\u2014'} the remaining {ddCards.clean.length} reported no spikes in this window.
                  </Typography>

                  <Box display="flex" alignItems="center" style={{ gap: 16, flexWrap: 'wrap', padding: '8px 0' }}>
                    <Box display="flex" alignItems="center" style={{ gap: 6 }}>
                      <Typography variant="caption" style={{ fontWeight: 700, color: '#6b7db3' }}>Sort</Typography>
                      <select value={ddSortBy} onChange={(e) => setDdSortBy(e.target.value as typeof ddSortBy)} style={ddSelectStyle}>
                        <option value="priority">Ops Priority (default)</option>
                        <option value="mem">MEM AVAIL % {'\u2193'}</option>
                        <option value="spikes">Spike Count {'\u2193'}</option>
                        <option value="latest">Latest Spike {'\u2193'}</option>
                        <option value="name">Name A{'\u2192'}Z</option>
                      </select>
                    </Box>
                    <Box display="flex" alignItems="center" style={{ gap: 6 }}>
                      <Typography variant="caption" style={{ fontWeight: 700, color: '#6b7db3' }}>Min %</Typography>
                      <input type="range" min={0} max={100} value={ddMinPct} onChange={(e) => setDdMinPct(Number(e.target.value))} style={{ width: 80, accentColor: '#3b82f6' }} />
                      <Typography variant="caption" style={{ fontFamily: 'monospace', color: '#6b7db3', width: 32 }}>{ddMinPct}%</Typography>
                    </Box>
                    <Box display="flex" alignItems="center" style={{ gap: 4 }}>
                      <Typography variant="caption" style={{ fontWeight: 700, color: '#6b7db3' }}>Type</Typography>
                      {(['DB', 'APP', 'SRE'] as const).map((t) => (
                        <button key={t} onClick={() => toggleDdType(t)} style={ddPillStyle(ddTypeFilter.has(t))}>{t}</button>
                      ))}
                      <button onClick={() => setDdTypeFilter(new Set())} style={ddPillStyle(ddTypeFilter.size === 0)}>All</button>
                    </Box>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 'auto', cursor: 'pointer' }} title="Shows Azure Monitor's timestamp-aligned Maximum aggregation beside the Average series, exposing short peaks that average values can hide.">
                      <input type="checkbox" checked={ddShowMaxOverlay} onChange={(e) => setDdShowMaxOverlay(e.target.checked)} style={{ accentColor: '#3b82f6' }} />
                      <Typography variant="caption" style={{ fontWeight: 700, color: '#6b7db3' }}>Show bucket peaks</Typography>
                    </label>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }} title="Shows the lowest observed Average sample in this window. The API does not currently provide a per-bucket Minimum aggregation.">
                      <input type="checkbox" checked={ddShowMinOverlay} onChange={(e) => setDdShowMinOverlay(e.target.checked)} style={{ accentColor: '#3b82f6' }} />
                      <Typography variant="caption" style={{ fontWeight: 700, color: '#6b7db3' }}>Show low-water mark</Typography>
                    </label>
                  </Box>

                  <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginBottom: 8, fontSize: 9 }}>Tip: click any card to open its anomaly table and time-series panel.</Typography>

                  <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 10 }}>
                    {ddCards.critical.map((c) => {
                      const isSelected = c.vmName === deepDiveVm;
                      const isCorrelated = correlatedVms.has(c.vmName);
                      const points = c.vmData.series?.[c.domKey] || [];
                      return (
                        <Box
                          key={c.vmName}
                          onClick={() => { setDeepDiveVm(c.vmName); setCorrelatedVms(new Set()); }}
                          style={{ borderRadius: 10, border: `1px solid ${isSelected ? '#f43f5e' : isCorrelated ? '#22d3ee' : 'rgba(244,63,94,.3)'}`, background: isCorrelated ? 'rgba(34,211,238,.07)' : 'rgba(244,63,94,.04)', padding: 10, cursor: 'pointer', boxShadow: isSelected ? '0 0 0 2px rgba(244,63,94,.3)' : isCorrelated ? '0 0 0 1px rgba(34,211,238,.35)' : undefined }}
                        >
                          <Box display="flex" alignItems="flex-start" justifyContent="space-between" style={{ gap: 8 }}>
                            <Box style={{ minWidth: 0 }}>
                              <Box display="flex" alignItems="center" style={{ gap: 6, flexWrap: 'wrap' }}>
                                <Typography component="span" variant="body2" style={{ fontWeight: 700, fontFamily: 'monospace' }}>{c.vmName}</Typography>
                                <span className="metric-badge" style={{ fontSize: 8 }}>{c.role}</span>
                                {c.env && <span className="metric-badge" style={{ fontSize: 8 }}>{c.env}</span>}
                              </Box>
                              <Box display="flex" alignItems="center" style={{ gap: 6, marginTop: 4, flexWrap: 'wrap' }}>
                                <span className="metric-badge" style={{ fontSize: 8, color: c.severityColor, borderColor: `${c.severityColor}40`, background: `${c.severityColor}1f` }}>{c.severityLabel}</span>
                                {c.vmData.baseline_confidence?.degraded && (
                                  <span className="metric-badge metric-badge-amber" style={{ fontSize: 8 }} title={lowConfidenceBaselineTitle(c.vmData.baseline_confidence)}>
                                    {'⚠'} low-confidence baseline
                                  </span>
                                )}
                                <Typography component="span" variant="caption" color="textSecondary" style={{ fontSize: 9 }}>
                                  {c.spikeCount} spike event{c.spikeCount !== 1 ? 's' : ''} {'\u00b7'} {c.thresholdCrossCount} threshold crossing{c.thresholdCrossCount !== 1 ? 's' : ''}
                                </Typography>
                                {c.vmData.baseline_confidence && (
                                  <span
                                    className="metric-badge"
                                    title={baselineConfidenceTitle(c.vmData.baseline_confidence)}
                                    style={{ fontSize: 8, color: c.vmData.baseline_confidence.degraded ? '#f59e0b' : '#6b7db3', cursor: 'help' }}
                                  >
                                    {'\u24d8'} baseline{c.vmData.baseline_confidence.degraded ? ' (session-only)' : ''}
                                  </span>
                                )}
                              </Box>
                              {(c.waveformLabel || c.breachLabel) && (
                                <Box display="flex" style={{ gap: 5, flexWrap: 'wrap', marginTop: 4 }}>
                                  {c.waveformLabel && <span className="metric-badge" title="Signal shape classification" style={{ fontSize: 8, color: c.waveformRisk === 'critical' ? '#a855f7' : c.waveformRisk === 'high' ? '#f43f5e' : '#22d3ee' }}>{c.waveformIcon} {c.waveformLabel}</span>}
                                  {c.breachLabel && <span className="metric-badge metric-badge-amber" style={{ fontSize: 8 }}>{'\u23f1'} {c.breachLabel}</span>}
                                </Box>
                              )}
                            </Box>
                            <Box style={{ textAlign: 'right', flexShrink: 0 }}>
                              <Typography component="span" variant="h6" title={c.domLabel === 'MEM' ? 'Observed minimum Available Memory Percentage in this window.' : `${c.domLabel} P95 from this window.`} style={{ color: c.domColor, fontWeight: 800 }}>{c.domVal.toFixed(0)}%</Typography>
                              <Typography variant="caption" style={{ display: 'block', color: c.domColor, fontSize: 8, fontWeight: 700 }}>
                                {c.domLabel} {c.domLabel === 'MEM' ? 'MIN AVAIL' : 'P95'}
                              </Typography>
                              {c.trendArrow && <Typography variant="caption" style={{ display: 'block', color: c.domLabel === 'MEM' ? (c.trendArrow === '↓' ? '#f43f5e' : '#10d96e') : (c.trendArrow === '↑' ? '#f43f5e' : '#10d96e'), fontSize: 10, fontWeight: 800 }}>{c.trendArrow} {c.trendDelta}</Typography>}
                            </Box>
                          </Box>
                          {points.length > 4 && (
                            <Box style={{ marginTop: 6 }}>
                              <Sparkline points={points} color={c.domColor} fixed0to100={c.domLabel !== 'DISK'} />
                            </Box>
                          )}
                          <Typography variant="caption" style={{ color: '#3b82f6', fontSize: 9, fontWeight: 700 }}>{isSelected ? 'Showing detail \u25be' : 'Show detail \u25b8'}</Typography>
                        </Box>
                      );
                    })}
                  </Box>

                  {/* Detail panel for the selected card */}
                  {ddDetail && (
                    <Box style={{ marginTop: 12, borderRadius: 10, border: '1px solid rgba(244,63,94,.25)', background: 'rgba(244,63,94,.04)', padding: 12 }}>
                      <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap' }}>
                        <span>{'\u26a1'}</span>
                        <Typography variant="subtitle2" style={{ color: '#f43f5e' }}>ANOMALY &amp; SPIKE EVENTS</Typography>
                        <Typography variant="caption" color="textSecondary">{ddDetail.rows.length} event{ddDetail.rows.length !== 1 ? 's' : ''} on {deepDiveVm}</Typography>
                        {ddDetail.ctrlMActive ? (
                          <span className="metric-badge metric-badge-teal" style={{ fontSize: 8 }} title="Ctrl-M batch job runs are loaded — each spike below is time-joined against overlapping job windows.">{'\ud83d\udd17'} Ctrl-M correlation active</span>
                        ) : (
                          <span className="metric-badge" style={{ fontSize: 8, color: '#6b7db3' }} title="Upload a Ctrl-M execution history file in Upload & Intake to correlate these spikes with concurrently-running batch jobs.">Ctrl-M correlation off — no batch data loaded</span>
                        )}
                      </Box>
                      <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 9, marginTop: 2 }}>
                        Source: Azure Monitor {'\u00b7'} Aggregation: Average {'\u00b7'} Grain: {ddDetail.grainLabel} {'\u00b7'} Datapoints: {ddDetail.datapoints}
                      </Typography>
                      {ddDetail.ctrlMActive && (
                        <Typography variant="caption" style={{ display: 'block', fontSize: 9, marginTop: 2, color: '#2dd4bf' }}>
                          Ctrl-M matches show time overlap only; they do not prove job-to-host causation.
                        </Typography>
                      )}

                      {ddDetail.insight && (
                        <Box style={{ marginTop: 8, borderRadius: 6, border: '1px solid rgba(34,211,238,.25)', background: 'rgba(34,211,238,.08)', padding: '6px 10px' }}>
                          <Typography variant="caption" style={{ color: '#22d3ee' }}><b style={{ textTransform: 'uppercase', fontSize: 9 }}>Insight</b> {ddDetail.insight}</Typography>
                        </Box>
                      )}

                      {ddDetail.rows.length > 0 ? (
                        <>
                          <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 8, marginTop: 8 }}>
                            Severity rules: <span style={{ color: SEVERITY_COLOR['CRITICAL SUSTAINED'] }}>CRITICAL SUSTAINED</span> {'\u00b7'} <span style={{ color: SEVERITY_COLOR.CRITICAL }}>CRITICAL</span> {'\u00b7'} <span style={{ color: SEVERITY_COLOR.WARNING }}>WARNING</span>
                          </Typography>
                          <Table size="small" className="pe-table" aria-label="Anomaly spike events" style={{ marginTop: 6 }}>
                            <TableHead>
                              <TableRow>
                                <TableCell>Severity</TableCell>
                                <TableCell>Metric</TableCell>
                                <TableCell>Peak</TableCell>
                                <TableCell>Window</TableCell>
                                <TableCell>Duration</TableCell>
                                <TableCell>Pattern</TableCell>
                                <TableCell>Detail</TableCell>
                              </TableRow>
                            </TableHead>
                            <TableBody>
                              {ddDetail.groupedRows.slice(0, 30).map(({ row: s, recurring, count, days, durations, maxPeak, ctrlM }, i) => {
                                const sevLabel = (s.severity || 'critical').toUpperCase().replace('_', ' ');
                                const sevColor = SEVERITY_COLOR[sevLabel] || '#f43f5e';
                                const start = formatUtcDateTime(s.start);
                                const end = formatUtcDateTime(s.end);
                                const durationText = recurring ? formatRecurringDurations(durations) : humanizeDurationMin(durationMinutesFromBounds(s.start, s.end) ?? s.duration_min ?? 0);
                                const detailText = recurring
                                  ? `${count} events · durations ${durations.map(humanizeDurationMin).join(', ')}${s.severity_reason ? ` · representative event: ${s.severity_reason}` : ''}`
                                  : s.severity_reason || '\u2014';
                                const pattern = recurring ? `Likely recurring (${days}d)` : (s.detection === 'absolute_threshold' ? 'Sustained breach' : 'Z-score spike');
                                return (
                                  <TableRow key={i}>
                                    <TableCell>
                                      <span style={{ color: sevColor, fontWeight: 700, fontSize: 10 }}>{sevLabel}</span>
                                      {s.detection === 'absolute_threshold' && <span className="metric-badge metric-badge-teal" style={{ fontSize: 8, marginLeft: 4 }}>ABS</span>}
                                      {selectedBaselineConfidence?.degraded && (
                                        <span className="metric-badge metric-badge-amber" style={{ fontSize: 8, marginLeft: 4 }} title={lowConfidenceBaselineTitle(selectedBaselineConfidence)}>
                                          {'⚠'} low-confidence baseline
                                        </span>
                                      )}
                                    </TableCell>
                                    <TableCell>{shortMetric(s.metric)}</TableCell>
                                    <TableCell style={{ fontFamily: 'monospace' }}>{recurring ? formatPeak(s.metric, maxPeak) : s.peak != null ? formatPeak(s.metric, s.peak) : '\u2014'}{recurring && <span className="metric-badge metric-badge-amber" style={{ fontSize: 8, marginLeft: 4 }}>{count}x</span>}</TableCell>
                                    <TableCell style={{ fontSize: 10 }}>{recurring ? `${days}d pattern` : `${start} → ${end} UTC`}</TableCell>
                                    <TableCell style={{ fontSize: 10 }}>{durationText}</TableCell>
                                    <TableCell style={{ fontSize: 10, color: recurring ? '#f59e0b' : undefined }}>{pattern}</TableCell>
                                    <TableCell style={{ fontSize: 10 }}>
                                      <span title={detailText}>{detailText}</span>
                                      {ctrlM && ctrlM.concurrent_jobs > 0 && (
                                        <Box style={{ marginTop: 2 }} title={`${ctrlM.concurrent_jobs} Ctrl-M job(s) overlapped this spike window (time-coincidence, not host-pinned).`}>
                                          <Typography variant="caption" style={{ display: 'block', color: '#2dd4bf' }}>
                                            {'\ud83d\udd17'} {ctrlM.heaviest} ({(ctrlM.heaviest_hrs || 0).toFixed(1)}h) running · {ctrlM.concurrent_jobs} job(s) overlapped · time overlap only
                                          </Typography>
                                          {ctrlM.jobs && ctrlM.jobs.length > 1 && (
                                            <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 8, marginTop: 2 }}>
                                              Jobs: {ctrlM.jobs.map((job) => `${job.job} (${(job.hrs || 0).toFixed(1)}h)`).join(' · ')}
                                            </Typography>
                                          )}
                                        </Box>
                                      )}
                                    </TableCell>
                                  </TableRow>
                                );
                              })}
                            </TableBody>
                          </Table>
                          {ddDetail.normalMetrics.length > 0 && (
                            <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 9, marginTop: 6 }}>
                              {'\u2713 '}{ddDetail.normalMetrics.length} other graded metric{ddDetail.normalMetrics.length > 1 ? 's' : ''} with no detected anomaly: {ddDetail.normalMetrics.join(', ')}
                            </Typography>
                          )}
                        </>
                      ) : (
                        <Box style={{ marginTop: 8, borderRadius: 6, border: '1px solid rgba(16,217,110,.3)', background: 'rgba(16,217,110,.06)', padding: '8px 10px' }}>
                          <Typography variant="caption" style={{ color: '#10d96e', fontWeight: 700 }}>{'\u2713'} ALL PATTERNS NORMAL</Typography>
                          {ddDetail.normalMetrics.length > 0 && (
                            <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 9 }}>{ddDetail.normalMetrics.length} graded metrics with no detected anomaly: {ddDetail.normalMetrics.join(', ')}</Typography>
                          )}
                        </Box>
                      )}

                      {deepDiveVmChart && (
                        <Box style={{ marginTop: 12 }}>
                          <Typography variant="subtitle2">Unified Time-Series {'\u2014'} All Metrics</Typography>
                          <Typography variant="caption" color="textSecondary" style={{ display: 'block', fontSize: 9, marginBottom: 4 }}>
                            Shaded windows are detected anomaly events. Dotted lines are Azure bucket peaks; average lines remain the primary time series.
                            {(deepDiveVmChart as (Highcharts.Options & { _hadGap?: boolean }) | null)?._hadGap && ' A broken line marks a bucket Azure did not report \u2014 it is a gap, not interpolated data.'}
                          </Typography>
                          {(() => {
                            const waveforms = deepDive?.vms?.[deepDiveVm]?.waveforms;
                            if (!waveforms || !Object.keys(waveforms).length) return null;
                            const riskColor: Record<string, string> = { none: '#10d96e', low: '#22d3ee', medium: '#f59e0b', high: '#f43f5e', critical: '#a855f7' };
                            return (
                              <Box style={{ marginBottom: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
                                <Typography variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', fontSize: 9, color: '#6b7db3' }}>Signal Pattern {'\u2014'} what shape is this metric drawing?</Typography>
                                <Box display="flex" style={{ gap: 6, flexWrap: 'wrap' }}>
                                  {Object.entries(waveforms).map(([metric, wf]) => {
                                    const color = riskColor[wf.risk || 'low'] || '#6b7db3';
                                    return (
                                      <span
                                        key={metric}
                                        className="metric-badge"
                                        style={{ fontSize: 9, color, borderColor: `${color}40`, background: `${color}1a`, cursor: 'help' }}
                                        title={`${wf.meaning || ''}${wf.action ? ` \u2192 ${wf.action}` : ''}${wf.confidence_label ? ` (${wf.confidence_label} signal)` : ''}`}
                                      >
                                        {wf.icon} {shortMetric(metric)}: {wf.label}
                                      </span>
                                    );
                                  })}
                                </Box>
                              </Box>
                            );
                          })()}
                          <HighchartsReact highcharts={Highcharts} options={deepDiveVmChart} />
                        </Box>
                      )}
                    </Box>
                  )}
                </Box>
              )}

              {/* Healthy VMs — compact clickable table, ported from the "clean VMs" branch of _renderDeepDiveCharts() (app.js) */}
              {ddCards.clean.length > 0 && (
                <Box style={{ marginTop: 12, borderRadius: 10, border: '1px solid rgba(16,217,110,.2)', background: 'rgba(16,217,110,.05)', padding: 12 }}>
                  <Box display="flex" alignItems="center" style={{ gap: 8 }}>
                    <span>{'\u2705'}</span>
                    <Typography variant="subtitle2" style={{ color: '#10d96e', textTransform: 'uppercase', letterSpacing: '.08em', fontSize: 11 }}>Healthy {'\u2014'} {ddCards.clean.length} Server{ddCards.clean.length > 1 ? 's' : ''} Normal</Typography>
                  </Box>
                  <Table size="small" className="pe-table" aria-label="Healthy servers" style={{ marginTop: 6 }}>
                    <TableHead>
                      <TableRow>
                        <TableCell>Server</TableCell>
                        <TableCell>CPU</TableCell>
                        <TableCell>Memory</TableCell>
                        <TableCell>Status</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {ddCards.clean.map(({ vmName, vmData }) => {
                        const cpuS = vmData.stats?.['Percentage CPU'];
                        const memS = vmData.stats?.['Available Memory Percentage'];
                        return (
                          <TableRow key={vmName} hover onClick={() => setDeepDiveVm(vmName)} style={{ cursor: 'pointer' }}>
                            <TableCell style={{ fontFamily: 'monospace' }}>{vmName}</TableCell>
                            <TableCell style={{ color: '#3b82f6', fontSize: 11 }}>{cpuS ? `avg ${cpuS.mean}% \u00b7 max ${cpuS.max}%` : '\u2014'}</TableCell>
                            <TableCell style={{ color: '#22d3ee', fontSize: 11 }}>{memS ? `avail ${memS.mean}% \u00b7 min ${memS.min}%` : '\u2014'}</TableCell>
                            <TableCell style={{ color: '#10d96e', fontSize: 11 }}>{'\u2713'} Normal</TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </Box>
              )}

              {/* Spike attribution */}
              {deepDive.spike_attribution && deepDive.spike_attribution.rows.length > 0 && (() => {
                const attrRows = deepDive.spike_attribution.rows;
                const linked = attrRows.filter((r) => r.concurrent_jobs > 0);
                const unlinked = attrRows.filter((r) => !r.concurrent_jobs);
                // Attributed spikes first. An unattributed spike is still a real
                // spike worth seeing, but interleaving it with attributed ones in
                // a table titled "Attribution" implied a job link that the row
                // explicitly does not have.
                const ordered = [...linked, ...unlinked].slice(0, 20);
                const fmtWindow = (row: typeof attrRows[number]) => {
                  const range = formatSpikeWindow(row.start, row.end);
                  if (range !== '—') return range;
                  return row.peak_time ? `${formatUtcDateTime(row.peak_time)} UTC` : '—';
                };
                return (
                <Box style={{ marginTop: 12 }}>
                  <Typography variant="subtitle2">{'Spike \u2192 Batch Job Attribution'}</Typography>
                  <Typography variant="caption" color="textSecondary" style={{ display: 'block' }}>{deepDive.spike_attribution.summary.caveat}</Typography>
                  <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 2, fontSize: 9 }}>
                    {`One row per detected spike, newest evidence first. ${linked.length} of ${attrRows.length} spike(s) had a Ctrl-M job running in the same window; ${unlinked.length} had none and are listed last. A host that saturates its CPU repeats a near-identical peak on every spike \u2014 read the Spike Window column to tell them apart.`}
                  </Typography>
                  <Table size="small" className="pe-table" aria-label="Spike attribution table" style={{ marginTop: 8 }}>
                    <TableHead>
                      <TableRow>
                        <TableCell>VM</TableCell>
                        <TableCell>Metric</TableCell>
                        <TableCell>Spike Window (UTC)</TableCell>
                        <TableCell align="right">Peak</TableCell>
                        <TableCell>Severity</TableCell>
                        <TableCell align="right">Concurrent Jobs</TableCell>
                        <TableCell>Heaviest Job</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {ordered.map((row, index) => {
                        const isLinked = row.concurrent_jobs > 0;
                        return (
                        <TableRow key={`${row.vm}-${row.metric}-${row.peak_time || index}`} style={isLinked ? undefined : { opacity: 0.62 }}>
                          <TableCell>{renderHostId(row.vm)}</TableCell>
                          <TableCell>{shortMetric(row.metric)}</TableCell>
                          <TableCell style={{ fontFamily: 'monospace', fontSize: 11 }}>{fmtWindow(row)}</TableCell>
                          <TableCell align="right">{row.peak != null ? row.peak.toFixed(1) : '\u2014'}</TableCell>
                          <TableCell><span className="metric-badge" style={{ color: (row.severity || '').startsWith('critical') ? '#f43f5e' : '#f59e0b' }}>{row.severity}</span></TableCell>
                          <TableCell align="right">{row.concurrent_jobs}</TableCell>
                          <TableCell>
                            {row.heaviest
                              ? `${row.heaviest} (${(row.heaviest_hrs || 0).toFixed(1)}h)`
                              : <span style={{ color: '#6b7db3', fontStyle: 'italic' }}>no Ctrl-M job overlapped</span>}
                          </TableCell>
                        </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </Box>
                );
              })()}
            </>
          )}
        </Box>
      )}
    </Paper>
  );
}

interface AzureConnectionCardProps {
  authInfo: Record<string, unknown> | null;
  onOpen: () => void;
}

/** Compact status card + trigger for the full AzureFetchModal discovery/select/fetch workflow. */
function AzureConnectionCard({ authInfo, onOpen }: AzureConnectionCardProps) {
  const connected = authInfo?.method === 'browser';
  return (
    <Box
      display="flex" alignItems="center" justifyContent="space-between"
      style={{ borderRadius: 12, border: '1px solid #213060', background: 'rgba(17,29,54,.5)', padding: 16, marginTop: 16, flexWrap: 'wrap', gap: 8 }}
    >
      <Box>
        <Typography variant="subtitle2">Azure Monitor Connection</Typography>
        <Typography variant="caption" color="textSecondary">
          {connected ? `Connected as ${authInfo?.display_name || authInfo?.name}` : 'Not connected'}
        </Typography>
      </Box>
      <Button size="small" variant="contained" color="primary" onClick={onOpen}>
        {connected ? 'Fetch from Azure Monitor' : 'Connect Azure'}
      </Button>
    </Box>
  );
}
