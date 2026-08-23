import React, { useCallback, useEffect, useMemo, useState } from 'react';
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
  TextField,
  Typography,
  makeStyles,
} from '@material-ui/core';
import { getApiBaseUrl, getReportArchive } from '../../api/dashboardApi';

interface ArchiveRow {
  customer_slug: string;
  customer: string;
  generated_at: string;
  env?: string;
  pe_approved?: boolean;
  cust_approved?: boolean;
  pe_name?: string;
  cust_name?: string;
  checklist_mismatches?: number;
  sla_breach_count?: number;
  sla_at_risk_count?: number;
  sla_total_jobs?: number;
  batch_metrics_captured?: boolean | number;
  batch_compliance_pct?: number;
  batch_total_jobs?: number;
  batch_total_runs?: number;
  batch_total_hrs?: number;
  batch_breach_count?: number;
  batch_at_risk_count?: number;
  batch_ok_count?: number;
  resource_metrics_captured?: boolean | number;
  resource_fleet_grade?: string;
  resource_fleet_score?: number;
  resource_total_servers?: number;
  resource_critical_count?: number;
  resource_warning_count?: number;
  sow_metrics_captured?: boolean | number;
  sow_status?: string;
  sow_metrics_count?: number;
  benchmark_metrics_captured?: boolean | number;
  benchmark_total_transactions?: number;
  benchmark_sla_breach_count?: number;
  benchmark_degraded_count?: number;
  batch_perf_regression_count?: number;
  batch_perf_total_jobs?: number;
  issues_count?: number;
}

type RegistryFilter = 'all' | 'signed' | 'pending' | 'attention';
type RegistrySort = 'recent' | 'attention' | 'customer';
type Tone = 'green' | 'amber' | 'red' | 'blue' | 'gray';
interface SnapshotGroup { tone: Tone; label: string; compact: string; lines: Array<[string, string]>; }

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  toolbar: { display: 'flex', alignItems: 'center', gap: theme.spacing(1), flexWrap: 'wrap' },
  summary: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: theme.spacing(1), marginTop: theme.spacing(2) },
  tableWrap: { marginTop: theme.spacing(1), overflow: 'auto', border: '1px solid rgba(33,48,96,.85)', borderRadius: 8, background: 'rgba(6,9,26,.38)' },
  empty: { padding: theme.spacing(6, 2), textAlign: 'center' },
}));

const TONE: Record<Tone, { color: string; background: string; border: string }> = {
  green: { color: '#10d96e', background: 'rgba(16,217,110,.10)', border: 'rgba(16,217,110,.30)' },
  amber: { color: '#f59e0b', background: 'rgba(245,158,11,.10)', border: 'rgba(245,158,11,.30)' },
  red: { color: '#f43f5e', background: 'rgba(244,63,94,.10)', border: 'rgba(244,63,94,.30)' },
  blue: { color: '#60a5fa', background: 'rgba(96,165,250,.10)', border: 'rgba(96,165,250,.30)' },
  gray: { color: '#94a3b8', background: 'rgba(148,163,184,.07)', border: 'rgba(148,163,184,.24)' },
};

function count(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : 0;
}
function decimal(value: unknown, digits = 1): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString(undefined, { maximumFractionDigits: digits }) : '—';
}
function captured(value: unknown): boolean { return value === true || value === 1 || value === '1'; }
function formatDate(value?: string): string {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleString(undefined, { year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
    : '—';
}
function timestamp(value?: string): number {
  const parsed = value ? new Date(value).getTime() : 0;
  return Number.isNaN(parsed) ? 0 : parsed;
}
function signedOff(report: ArchiveRow): boolean { return Boolean(report.pe_approved && report.cust_approved); }
function attentionScore(report: ArchiveRow): number {
  const sow = String(report.sow_status || '').toUpperCase();
  return (sow === 'CRITICAL_OVER' ? 10_000_000 : sow === 'OVER' ? 1_000_000 : 0)
    + count(report.sla_breach_count) * 10_000 + count(report.resource_critical_count) * 1_000
    + count(report.benchmark_sla_breach_count) * 100 + count(report.batch_perf_regression_count) * 10
    + count(report.sla_at_risk_count) + count(report.checklist_mismatches);
}
function reviewState(report: ArchiveRow): { tone: Tone; label: string; detail: string } {
  if (signedOff(report)) return { tone: 'green', label: 'Signed off', detail: 'PE and customer approval recorded' };
  if (report.pe_approved) return { tone: 'amber', label: 'Awaiting customer', detail: 'PE approval recorded; customer sign-off pending' };
  if (report.cust_approved) return { tone: 'amber', label: 'Awaiting PE', detail: 'Customer approval recorded; PE sign-off pending' };
  return { tone: 'gray', label: 'Exported — sign-off pending', detail: 'No approval recorded on this frozen export' };
}
function sowPresentation(status?: string): { label: string; tone: Tone } {
  const value = String(status || '').toUpperCase();
  if (value === 'OPTIMAL' || value === 'ACCEPTABLE') return { label: value, tone: 'green' };
  if (value === 'LOW') return { label: 'UNDER-UTILISED', tone: 'blue' };
  if (value === 'OVER') return { label: 'OVER CONTRACT', tone: 'amber' };
  if (value === 'CRITICAL_OVER') return { label: 'CRITICAL OVER', tone: 'red' };
  return { label: 'NOT ASSESSED', tone: 'gray' };
}
function snapshots(report: ArchiveRow): SnapshotGroup[] {
  const rows: SnapshotGroup[] = [];
  if (captured(report.batch_metrics_captured)) {
    const breaches = count(report.batch_breach_count ?? report.sla_breach_count);
    const atRisk = count(report.batch_at_risk_count ?? report.sla_at_risk_count);
    rows.push({ tone: breaches ? 'red' : atRisk ? 'amber' : 'green', label: 'Batch SLA snapshot', compact: `SLA ${decimal(report.batch_compliance_pct)}%`, lines: [
      ['Compliance', `${decimal(report.batch_compliance_pct)}%`], ['Jobs', `${count(report.batch_total_jobs ?? report.sla_total_jobs)} total · ${count(report.batch_ok_count)} within SLA`],
      ['Exceptions', `${atRisk} at risk · ${breaches} breach`], ['Runs / runtime', `${count(report.batch_total_runs)} / ${decimal(report.batch_total_hrs, 2)} h`],
    ] });
  } else rows.push({ tone: 'gray', label: 'Batch SLA not captured', compact: 'SLA N/A', lines: [['Snapshot', 'Not available on this export']] });
  if (captured(report.resource_metrics_captured)) {
    const grade = String(report.resource_fleet_grade || '—').toUpperCase();
    const tone: Tone = grade === 'A' ? 'green' : grade === 'B' ? 'blue' : grade === 'C' ? 'amber' : grade === 'D' || grade === 'F' ? 'red' : 'gray';
    rows.push({ tone, label: `Resource fleet · ${grade}`, compact: `Fleet ${grade} · ${decimal(report.resource_fleet_score)}`, lines: [
      ['Fleet score', decimal(report.resource_fleet_score)], ['Servers', String(count(report.resource_total_servers))],
      ['Exceptions', `${count(report.resource_critical_count)} critical · ${count(report.resource_warning_count)} warning`],
    ] });
  } else rows.push({ tone: 'gray', label: 'Resource not captured', compact: 'Fleet N/A', lines: [['Snapshot', 'Not available on this export']] });
  if (captured(report.sow_metrics_captured)) {
    const sow = sowPresentation(report.sow_status);
    rows.push({ tone: sow.tone, label: `SOW · ${sow.label}`, compact: `SOW ${sow.label}`, lines: [['Contract metrics', String(count(report.sow_metrics_count))]] });
  } else rows.push({ tone: 'gray', label: 'SOW not captured', compact: 'SOW N/A', lines: [['Snapshot', 'Not available on this export']] });
  if (captured(report.benchmark_metrics_captured)) {
    const breaches = count(report.benchmark_sla_breach_count), regressions = count(report.batch_perf_regression_count), degraded = count(report.benchmark_degraded_count);
    rows.push({ tone: breaches || regressions ? 'red' : degraded ? 'amber' : 'green', label: 'Benchmark snapshot', compact: breaches ? `Benchmark ${breaches} breach` : regressions ? `Benchmark ${regressions} regression` : degraded ? 'Benchmark degraded' : 'Benchmark clear', lines: [
      ['Transactions', String(count(report.benchmark_total_transactions))], ['SLA / degraded', `${breaches} breach · ${degraded} degraded`], ['Batch performance', `${count(report.batch_perf_total_jobs)} jobs · ${regressions} regression`],
    ] });
  } else rows.push({ tone: 'gray', label: 'Benchmark not captured', compact: 'Benchmark N/A', lines: [['Snapshot', 'Not available on this export']] });
  const mismatches = count(report.checklist_mismatches);
  rows.push({ tone: count(report.issues_count) ? 'amber' : 'gray', label: `Issues · ${count(report.issues_count)} logged`, compact: `Issues ${count(report.issues_count)}`, lines: [['Recorded issue entries', String(count(report.issues_count))]] });
  rows.push({ tone: mismatches ? 'amber' : 'gray', label: `Checklist · ${mismatches} evidence gap${mismatches === 1 ? '' : 's'}`, compact: mismatches ? `⚠ ${mismatches} gap${mismatches === 1 ? '' : 's'}` : 'Checklist clear', lines: [['Supporting evidence', mismatches ? 'Review required' : 'No recorded gap']] });
  return rows;
}
function Tag({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  const style = TONE[tone];
  return <span style={{ display: 'inline-flex', alignItems: 'center', minHeight: 22, padding: '2px 7px', borderRadius: 4, border: `1px solid ${style.border}`, background: style.background, color: style.color, fontSize: 10, fontWeight: 750, letterSpacing: '.025em', whiteSpace: 'nowrap' }}>{children}</span>;
}
function SummaryCard({ label, value, note, tone = 'blue' }: { label: string; value: string; note: string; tone?: Tone }) {
  const style = TONE[tone];
  return <Box style={{ minWidth: 0, padding: '14px 16px', border: `1px solid ${style.border}`, borderRadius: 8, background: 'rgba(17,29,54,.56)' }}><Typography variant="caption" style={{ display: 'block', color: '#6b7db3', fontWeight: 800, fontSize: 9, letterSpacing: '.1em', textTransform: 'uppercase' }}>{label}</Typography><Typography variant="h6" style={{ marginTop: 4, color: style.color, fontFamily: 'monospace', fontWeight: 800 }}>{value}</Typography><Typography variant="caption" color="textSecondary" style={{ fontSize: 10 }}>{note}</Typography></Box>;
}

export function ArchivePanel() {
  const classes = useStyles();
  const [reports, setReports] = useState<ArchiveRow[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<RegistryFilter>('all');
  const [sort, setSort] = useState<RegistrySort>('recent');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const loadReports = useCallback(async () => {
    setBusy(true); setError(null);
    try { const result = await getReportArchive(); setReports((result.reports as ArchiveRow[]) || []); }
    catch (fetchError) { setError(fetchError instanceof Error ? fetchError.message : 'Failed to load Review Registry.'); }
    finally { setBusy(false); }
  }, []);
  useEffect(() => { void loadReports(); }, [loadReports]);
  const visibleReports = useMemo(() => {
    const query = search.trim().toLowerCase();
    return reports.filter((report) => {
      const matchesFilter = filter === 'all' || (filter === 'signed' && signedOff(report)) || (filter === 'pending' && !signedOff(report)) || (filter === 'attention' && attentionScore(report) > 0);
      return matchesFilter && (!query || [report.customer, report.env, report.pe_name, report.cust_name].join(' ').toLowerCase().includes(query));
    }).sort((left, right) => {
      if (sort === 'customer') return String(left.customer || '').localeCompare(String(right.customer || ''));
      if (sort === 'attention') { const diff = attentionScore(right) - attentionScore(left); if (diff) return diff; }
      return timestamp(right.generated_at) - timestamp(left.generated_at);
    });
  }, [filter, reports, search, sort]);
  const summary = useMemo(() => {
    const latest = [...reports].sort((left, right) => timestamp(right.generated_at) - timestamp(left.generated_at))[0];
    return { signed: reports.filter(signedOff).length, attention: reports.filter((report) => attentionScore(report) > 0).length, latest };
  }, [reports]);
  const toggleExpanded = (slug: string) => setExpanded((previous) => { const next = new Set(previous); if (next.has(slug)) next.delete(slug); else next.add(slug); return next; });

  return <Paper className={`${classes.panel} kpi-card`} elevation={0}>
    <Box display="flex" alignItems="flex-start" justifyContent="space-between" style={{ gap: 16, flexWrap: 'wrap' }}>
      <Box><Typography variant="h6">Review Registry</Typography><Typography variant="body2" color="textSecondary" style={{ marginTop: 4 }}>The latest exported HTML audit per customer. Snapshot metrics are frozen at export time and never re-graded from a later session.</Typography></Box>
      <Button size="small" variant="outlined" onClick={() => void loadReports()} disabled={busy}>Refresh registry</Button>
    </Box>
    <Box className={classes.summary} aria-label="Review Registry summary">
      <SummaryCard label="Customers reviewed" value={String(reports.length)} note="Latest export per customer" />
      <SummaryCard label="Signed off" value={String(summary.signed)} note="PE and customer approval recorded" tone="green" />
      <SummaryCard label="Needs attention" value={String(summary.attention)} note="Exceptions or evidence gaps on export" tone={summary.attention ? 'amber' : 'green'} />
      <SummaryCard label="Latest export" value={summary.latest ? formatDate(summary.latest.generated_at) : '—'} note={summary.latest?.customer || 'No exported audit yet'} tone="gray" />
    </Box>
    <Box style={{ marginTop: 16, border: '1px solid rgba(33,48,96,.85)', borderRadius: 8, background: 'rgba(17,29,54,.45)', padding: 10 }}>
      <Box className={classes.toolbar}>
        <TextField value={search} onChange={(event) => setSearch(event.target.value)} variant="outlined" size="small" placeholder="Search customer, environment, or reviewer" inputProps={{ 'aria-label': 'Search Review Registry' }} style={{ minWidth: 250, flex: '1 1 300px' }} />
        {([['all', 'All exports'], ['signed', 'Signed off'], ['pending', 'Awaiting sign-off'], ['attention', 'Needs attention']] as Array<[RegistryFilter, string]>).map(([value, label]) => <Button key={value} size="small" variant={filter === value ? 'contained' : 'text'} color={filter === value ? 'primary' : 'default'} onClick={() => setFilter(value)}>{label}</Button>)}
        <select aria-label="Sort customer reviews" value={sort} onChange={(event) => setSort(event.target.value as RegistrySort)} style={{ minHeight: 32, color: '#cbd5e1', background: '#0a1222', border: '1px solid #213060', borderRadius: 6, padding: '4px 8px', fontSize: 12 }}><option value="recent">Newest export first</option><option value="attention">Highest attention first</option><option value="customer">Customer name</option></select>
      </Box>
      <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 8 }}>{visibleReports.length} of {reports.length} customer{reports.length === 1 ? '' : 's'} shown</Typography>
    </Box>
    {busy && <Box display="flex" justifyContent="center" style={{ padding: 32 }}><CircularProgress size={24} aria-label="Loading archive" /></Box>}
    {error && <Typography variant="body2" color="error" style={{ marginTop: 16 }}>{error}</Typography>}
    {!busy && !error && reports.length === 0 && <Box className={classes.empty}><Typography variant="subtitle1" style={{ fontWeight: 700 }}>No reports have been generated and archived yet.</Typography><Typography variant="body2" color="textSecondary" style={{ marginTop: 6 }}>Export the HTML report from Governance after supplying a customer name. Its complete evidence snapshot will appear here automatically.</Typography></Box>}
    {!busy && !error && reports.length > 0 && visibleReports.length === 0 && <Box className={classes.empty}><Typography variant="subtitle1" style={{ fontWeight: 700 }}>No customer reviews match these controls.</Typography><Typography variant="body2" color="textSecondary" style={{ marginTop: 6 }}>Change the search, filter, or sort selection to view another exported review.</Typography></Box>}
    {!busy && !error && visibleReports.length > 0 && <Box className={classes.tableWrap}><Table size="small" className="pe-table" aria-label="Review Registry table" style={{ minWidth: 1240 }}><TableHead><TableRow><TableCell>Customer</TableCell><TableCell>Review completion</TableCell><TableCell>Frozen exported evidence</TableCell><TableCell>Review team</TableCell><TableCell>Last exported</TableCell><TableCell>Report access</TableCell></TableRow></TableHead><TableBody>
      {visibleReports.map((report) => {
        const state = reviewState(report), detailOpen = expanded.has(report.customer_slug), detailGroups = snapshots(report);
        return <React.Fragment key={report.customer_slug}>
          <TableRow hover style={attentionScore(report) > 0 ? { background: 'rgba(245,158,11,.035)' } : undefined}>
            <TableCell><Typography variant="body2" style={{ fontWeight: 700 }}>{report.customer || 'Unknown customer'}</Typography><Typography variant="caption" color="textSecondary">Environment: {report.env || 'Not detected'}</Typography></TableCell>
            <TableCell><Box display="flex" flexDirection="column" alignItems="flex-start" style={{ gap: 5 }}><Tag tone={state.tone}>{state.label}</Tag><Tag tone={count(report.checklist_mismatches) ? 'amber' : 'gray'}>{count(report.checklist_mismatches)} evidence gap{count(report.checklist_mismatches) === 1 ? '' : 's'}</Tag><Typography variant="caption" color="textSecondary">{state.detail}</Typography></Box></TableCell>
            <TableCell style={{ minWidth: 300 }}><Box display="flex" style={{ gap: 5, overflowX: 'auto', paddingBottom: 4 }}>{detailGroups.map((group) => <Tag key={group.label} tone={group.tone}>{group.compact}</Tag>)}</Box><Button size="small" onClick={() => toggleExpanded(report.customer_slug)} style={{ marginTop: 4 }}>{detailOpen ? 'Hide breakdown' : 'Show breakdown'}</Button></TableCell>
            <TableCell><Typography variant="caption" color="textSecondary">PE</Typography><Typography variant="body2" style={{ fontSize: 12 }}>{report.pe_name || 'Not recorded'}</Typography><Typography variant="caption" color="textSecondary">Customer</Typography><Typography variant="body2" style={{ fontSize: 12 }}>{report.cust_name || 'Not recorded'}</Typography></TableCell>
            <TableCell style={{ whiteSpace: 'nowrap', fontFamily: 'monospace', fontSize: 11 }}>{formatDate(report.generated_at)}</TableCell>
            <TableCell style={{ whiteSpace: 'nowrap' }}><Button size="small" color="primary" href={`${getApiBaseUrl()}/api/report-archive/${encodeURIComponent(report.customer_slug)}`} target="_blank" rel="noopener noreferrer">Open full HTML</Button><Button size="small" href={`${getApiBaseUrl()}/api/report-archive/${encodeURIComponent(report.customer_slug)}/download`}>Download</Button></TableCell>
          </TableRow>
          {detailOpen && <TableRow style={{ background: 'rgba(6,9,26,.48)' }}><TableCell colSpan={6} style={{ padding: 12 }}><Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(205px, 1fr))', gap: 8 }}>{detailGroups.map((group) => { const tone = TONE[group.tone]; return <Box key={group.label} style={{ minWidth: 0, padding: 10, border: `1px solid ${tone.border}`, borderRadius: 6, background: 'rgba(17,29,54,.45)' }}><Tag tone={group.tone}>{group.label}</Tag>{group.lines.map(([label, value]) => <Box key={label} display="flex" justifyContent="space-between" style={{ gap: 8, marginTop: 6 }}><Typography variant="caption" color="textSecondary">{label}</Typography><Typography variant="caption" style={{ fontFamily: 'monospace', textAlign: 'right' }}>{value}</Typography></Box>)}</Box>; })}</Box></TableCell></TableRow>}
        </React.Fragment>;
      })}
    </TableBody></Table></Box>}
  </Paper>;
}
