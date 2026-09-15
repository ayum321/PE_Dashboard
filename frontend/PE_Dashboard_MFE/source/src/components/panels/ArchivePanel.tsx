import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  MenuItem,
  Paper,
  Select,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TextField,
  Typography,
  makeStyles,
} from '@material-ui/core';
import {
  EvidenceDocument,
  attachEvidenceToArchive,
  getApiBaseUrl,
  getAuditPackageDownloadUrl,
  getReportArchive,
  getReportDocumentDownloadUrl,
  getReportDocuments,
  importReportArchive,
} from '../../api/dashboardApi';
import { isValidCustomerName, normalizeCustomer, useOptionalAppData } from '../../context/AppDataContext';

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
  documents_count?: number;
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

function docIcon(type: string): string {
  switch (type) {
    case 'batch_sla': return '📊';
    case 'ctrlm_history': return '⏱️';
    case 'sow_contract': return '📜';
    case 'benchmark': return '📈';
    case 'azure_telemetry': return '☁️';
    case 'waiver': return '📝';
    default: return '📁';
  }
}

function formatBytes(bytes?: number): string {
  if (!bytes || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

interface VaultProps {
  slug: string;
  customer: string;
  onRefreshArchive?: () => void;
}

function EvidenceVaultView({ slug, customer, onRefreshArchive }: VaultProps) {
  const [docs, setDocs] = useState<EvidenceDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pkgAvailable, setPkgAvailable] = useState(false);
  const [copiedHash, setCopiedHash] = useState<string | null>(null);
  const [attachOpen, setAttachOpen] = useState(false);
  const [attachBusy, setAttachBusy] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [attachSuccess, setAttachSuccess] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [docType, setDocType] = useState('waiver');
  const [customLabel, setCustomLabel] = useState('');
  const fileRef = React.useRef<HTMLInputElement | null>(null);

  const loadDocs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await getReportDocuments(slug);
      setDocs(res.documents || []);
      setPkgAvailable(Boolean(res.package_available));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load documents');
    } finally {
      setLoading(false);
    }
  }, [slug]);

  useEffect(() => {
    void loadDocs();
  }, [loadDocs]);

  const handleCopyHash = (hash: string) => {
    if (navigator?.clipboard?.writeText) {
      void navigator.clipboard.writeText(hash);
      setCopiedHash(hash);
      setTimeout(() => setCopiedHash(null), 2500);
    }
  };

  const handleAttachSubmit = async () => {
    if (!selectedFile) {
      setAttachError('Please select a file to attach');
      return;
    }
    setAttachBusy(true);
    setAttachError(null);
    setAttachSuccess(null);
    try {
      const res = await attachEvidenceToArchive(slug, selectedFile, docType, customLabel.trim());
      if (res && res.ok) {
        setAttachSuccess(`Attached "${selectedFile.name}" successfully to audit proof.`);
        setSelectedFile(null);
        setCustomLabel('');
        if (fileRef.current) fileRef.current.value = '';
        setTimeout(() => {
          setAttachOpen(false);
          setAttachSuccess(null);
        }, 1200);
        await loadDocs();
        onRefreshArchive?.();
      } else {
        setAttachError(String(res?.error || 'Failed to attach evidence.'));
      }
    } catch (err) {
      setAttachError(err instanceof Error ? err.message : 'Failed to attach evidence.');
    } finally {
      setAttachBusy(false);
    }
  };

  return (
    <Box style={{ marginTop: 14, paddingTop: 14, borderTop: '1px dashed rgba(33,48,96,.85)' }}>
      <Box display="flex" alignItems="center" justifyContent="space-between" flexWrap="wrap" style={{ gap: 10, marginBottom: 12 }}>
        <Box display="flex" alignItems="center" style={{ gap: 8 }}>
          <Typography variant="subtitle2" style={{ fontWeight: 800, color: '#f0f4ff', display: 'flex', alignItems: 'center', gap: 6 }}>
            <span>🔒</span> Audit Proof &amp; Preserved Source Documents
          </Typography>
          <Tag tone={docs.length > 0 ? 'green' : 'gray'}>
            {docs.length > 0 ? `${docs.length} sealed proof doc${docs.length === 1 ? '' : 's'}` : 'Legacy export'}
          </Tag>
        </Box>
        <Box display="flex" alignItems="center" style={{ gap: 8 }}>
          <Button
            size="small"
            variant="outlined"
            style={{ color: '#cbd5e1', borderColor: 'rgba(203,213,225,.25)', textTransform: 'none', fontSize: 11 }}
            onClick={() => setAttachOpen(true)}
          >
            + Attach Supplementary Proof
          </Button>
          {(pkgAvailable || docs.length > 0) && (
            <Button
              size="small"
              variant="contained"
              style={{ background: '#10d96e', color: '#03170c', fontWeight: 750, textTransform: 'none', fontSize: 11 }}
              href={getAuditPackageDownloadUrl(slug)}
              download
            >
              📦 Download Audit Package (ZIP)
            </Button>
          )}
        </Box>
      </Box>

      <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginBottom: 10, fontSize: 11 }}>
        Immutable raw source files preserved at audit sign-off time. Every document carries a SHA-256 fingerprint for post-go-live dispute defense and audit proof.
      </Typography>

      {loading && (
        <Box display="flex" alignItems="center" style={{ gap: 8, padding: '12px 0' }}>
          <CircularProgress size={16} />
          <Typography variant="caption" color="textSecondary">Loading preserved evidence receipts…</Typography>
        </Box>
      )}

      {error && (
        <Typography variant="caption" style={{ color: '#f43f5e' }}>{error}</Typography>
      )}

      {!loading && docs.length === 0 && (
        <Box style={{ padding: 12, borderRadius: 6, background: 'rgba(148,163,184,.05)', border: '1px solid rgba(148,163,184,.2)' }}>
          <Typography variant="caption" style={{ color: '#94a3b8', display: 'block' }}>
            ℹ️ No raw source documents were preserved on this historical export. New audits automatically seal all uploaded Excel, CSV, PDF, and Azure telemetry files. You can attach supplementary proof (waivers, memos, emails) using the button above.
          </Typography>
        </Box>
      )}

      {!loading && docs.length > 0 && (
        <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 10 }}>
          {docs.map((d) => (
            <Box
              key={d.doc_id}
              style={{
                borderRadius: 8,
                border: '1px solid rgba(33,48,96,.95)',
                background: 'linear-gradient(180deg, rgba(17,29,54,.65), rgba(10,18,34,.75))',
                padding: 12,
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
                gap: 8,
              }}
            >
              <Box>
                <Box display="flex" alignItems="center" justifyContent="space-between" style={{ gap: 6, marginBottom: 4 }}>
                  <Typography variant="caption" style={{ fontWeight: 800, color: '#38bdf8', display: 'flex', alignItems: 'center', gap: 4 }}>
                    <span>{docIcon(d.document_type)}</span> {d.document_label || d.document_type}
                  </Typography>
                  <Tag tone={d.is_frozen ? 'green' : 'amber'}>{d.is_frozen ? 'Sealed' : 'Staged'}</Tag>
                </Box>
                <Typography variant="body2" style={{ fontWeight: 700, color: '#f0f4ff', wordBreak: 'break-all', fontSize: 12 }}>
                  {d.filename}
                </Typography>
                <Box display="flex" alignItems="center" style={{ gap: 8, marginTop: 4 }}>
                  <Typography variant="caption" color="textSecondary" style={{ fontSize: 10 }}>
                    Size: {formatBytes(d.file_size_bytes)}
                  </Typography>
                  <Typography variant="caption" color="textSecondary" style={{ fontSize: 10 }}>
                    Uploaded: {formatDate(d.uploaded_at)}
                  </Typography>
                </Box>
              </Box>

              <Box style={{ background: 'rgba(6,9,26,.65)', border: '1px solid rgba(33,48,96,.6)', borderRadius: 5, padding: '4px 6px' }}>
                <Box display="flex" alignItems="center" justifyContent="space-between" style={{ gap: 4 }}>
                  <Typography variant="caption" style={{ fontFamily: 'monospace', fontSize: 9.5, color: '#94a3b8', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    SHA-256: {d.file_hash ? `${d.file_hash.slice(0, 16)}…${d.file_hash.slice(-8)}` : '—'}
                  </Typography>
                  {d.file_hash && (
                    <Button
                      size="small"
                      style={{ minWidth: 'auto', padding: '1px 5px', fontSize: 9, color: copiedHash === d.file_hash ? '#10d96e' : '#60a5fa' }}
                      onClick={() => handleCopyHash(d.file_hash)}
                    >
                      {copiedHash === d.file_hash ? 'Copied' : 'Copy'}
                    </Button>
                  )}
                </Box>
              </Box>

              <Box display="flex" justifyContent="flex-end" style={{ marginTop: 2 }}>
                <Button
                  size="small"
                  variant="outlined"
                  style={{ fontSize: 10, color: '#60a5fa', borderColor: 'rgba(96,165,250,.35)', textTransform: 'none' }}
                  href={getReportDocumentDownloadUrl(slug, d.doc_id)}
                  download
                >
                  Download File
                </Button>
              </Box>
            </Box>
          ))}
        </Box>
      )}

      {/* Attach Supplementary Document Dialog */}
      <Dialog open={attachOpen} onClose={() => !attachBusy && setAttachOpen(false)} maxWidth="sm" fullWidth PaperProps={{ style: { background: '#0d1526', border: '1px solid #213060', color: '#f0f4ff' } }}>
        <DialogTitle style={{ color: '#f0f4ff', fontWeight: 800 }}>
          Attach Supplementary Audit Proof
        </DialogTitle>
        <DialogContent dividers style={{ borderColor: '#213060' }}>
          <Typography variant="body2" color="textSecondary" style={{ marginBottom: 14 }}>
            Attach post-review customer sign-off confirmation emails, risk waivers, architecture memos, or incident follow-ups directly into this customer's sealed Review Registry vault.
          </Typography>

          <Box style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', color: '#6b7db3', marginBottom: 4 }}>
              Document Category
            </label>
            <Select
              value={docType}
              onChange={(e) => setDocType(String(e.target.value))}
              variant="outlined"
              fullWidth
              style={{ background: '#0a0f1e', color: '#f0f4ff', border: '1px solid #213060', borderRadius: 6 }}
            >
              <MenuItem value="waiver">Governance Risk Waiver / Exception Memo</MenuItem>
              <MenuItem value="email_approval">Customer Sign-Off Confirmation Email</MenuItem>
              <MenuItem value="incident_note">Post-Go-Live Incident / Tuning Note</MenuItem>
              <MenuItem value="architecture_doc">Architecture / Topology Reference</MenuItem>
              <MenuItem value="other">Other Supporting Proof Document</MenuItem>
            </Select>
          </Box>

          <Box style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', color: '#6b7db3', marginBottom: 4 }}>
              Custom Label / Note (Optional)
            </label>
            <TextField
              fullWidth
              size="small"
              variant="outlined"
              placeholder="e.g. SRE VP Waiver for Nightly Order Batch Window"
              value={customLabel}
              onChange={(e) => setCustomLabel(e.target.value)}
              InputProps={{ style: { background: '#0a0f1e', color: '#f0f4ff', fontSize: 12 } }}
            />
          </Box>

          <Box style={{ marginBottom: 8 }}>
            <label style={{ display: 'block', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', color: '#6b7db3', marginBottom: 4 }}>
              Select File (.pdf, .docx, .xlsx, .csv, .eml, .txt, .png)
            </label>
            <input
              ref={fileRef}
              type="file"
              style={{ color: '#94a3b8', fontSize: 12 }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) setSelectedFile(f);
              }}
            />
            {selectedFile && (
              <Typography variant="caption" style={{ display: 'block', color: '#10d96e', marginTop: 4 }}>
                Selected: {selectedFile.name} ({formatBytes(selectedFile.size)})
              </Typography>
            )}
          </Box>

          {attachError && (
            <Typography variant="caption" style={{ display: 'block', color: '#f43f5e', marginTop: 8 }}>
              {attachError}
            </Typography>
          )}

          {attachSuccess && (
            <Typography variant="caption" style={{ display: 'block', color: '#10d96e', marginTop: 8 }}>
              {attachSuccess}
            </Typography>
          )}
        </DialogContent>
        <DialogActions style={{ borderColor: '#213060', padding: '12px 16px' }}>
          <Button onClick={() => setAttachOpen(false)} disabled={attachBusy} style={{ color: '#94a3b8' }}>
            Cancel
          </Button>
          <Button
            onClick={handleAttachSubmit}
            variant="contained"
            color="primary"
            disabled={!selectedFile || attachBusy}
            style={{ fontWeight: 750 }}
          >
            {attachBusy ? 'Attaching…' : 'Upload & Seal Evidence'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

export function ArchivePanel() {
  const classes = useStyles();
  const appData = useOptionalAppData();
  const data = appData?.data;
  const [reports, setReports] = useState<ArchiveRow[]>([]);
  const [busy, setBusy] = useState(true);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<RegistryFilter>('all');
  const [sort, setSort] = useState<RegistrySort>('recent');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);

  const CACHE_KEY = 'pe_report_archive_cache';

  const loadReports = useCallback(async () => {
    setBusy(true); setError(null);
    try {
      const result = await getReportArchive();
      const loaded = (result.reports as ArchiveRow[]) || [];
      setReports(loaded);
      if (loaded.length > 0) {
        try { localStorage.setItem(CACHE_KEY, JSON.stringify(loaded)); } catch {}
      }
    } catch (fetchError) {
      try {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) {
          setReports(JSON.parse(cached));
          setError('Operating in cached mode: could not reach backend server.');
          return;
        }
      } catch {}
      setError(fetchError instanceof Error ? fetchError.message : 'Failed to load Review Registry.');
    } finally { setBusy(false); }
  }, []);

  useEffect(() => { void loadReports(); }, [loadReports]);

  const handleFileImport = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setImporting(true);
    setNotice(null);
    try {
      const res = await importReportArchive(file);
      if (res && res.ok) {
        setNotice({
          type: 'success',
          message: `Successfully imported "${res.customer || file.name}" into the Review Registry.`,
        });
        await loadReports();
      } else {
        setNotice({
          type: 'error',
          message: (res as any)?.error || (res as any)?.detail || 'Failed to import report.',
        });
      }
    } catch (importErr) {
      setNotice({
        type: 'error',
        message: importErr instanceof Error ? importErr.message : 'Failed to import report.',
      });
    } finally {
      setImporting(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };
  const visibleReports = useMemo(() => {
    const query = search.trim().toLowerCase();
    const isCurrent = (cust: string) => Boolean(
      data?.customerName && isValidCustomerName(data.customerName) &&
      normalizeCustomer(data.customerName) === normalizeCustomer(cust)
    );
    return reports.filter((report) => {
      const matchesFilter = filter === 'all' || (filter === 'signed' && signedOff(report)) || (filter === 'pending' && !signedOff(report)) || (filter === 'attention' && attentionScore(report) > 0);
      const peName = report.pe_name || (isCurrent(report.customer) ? data?.approvals?.pe?.name : '') || '';
      const custName = report.cust_name || (isCurrent(report.customer) ? data?.approvals?.customer?.name : '') || '';
      const env = report.env || '';
      return matchesFilter && (!query || [report.customer, env, peName, custName].join(' ').toLowerCase().includes(query));
    }).sort((left, right) => {
      if (sort === 'customer') return String(left.customer || '').localeCompare(String(right.customer || ''));
      if (sort === 'attention') { const diff = attentionScore(right) - attentionScore(left); if (diff) return diff; }
      return timestamp(right.generated_at) - timestamp(left.generated_at);
    });
  }, [data?.approvals?.customer?.name, data?.approvals?.pe?.name, data?.customerName, filter, reports, search, sort]);
  const summary = useMemo(() => {
    const latest = [...reports].sort((left, right) => timestamp(right.generated_at) - timestamp(left.generated_at))[0];
    return { signed: reports.filter(signedOff).length, attention: reports.filter((report) => attentionScore(report) > 0).length, latest };
  }, [reports]);
  const toggleExpanded = (slug: string) => setExpanded((previous) => { const next = new Set(previous); if (next.has(slug)) next.delete(slug); else next.add(slug); return next; });

  return <Paper className={`${classes.panel} kpi-card`} elevation={0}>
    <Box display="flex" alignItems="flex-start" justifyContent="space-between" style={{ gap: 16, flexWrap: 'wrap' }}>
      <Box><Typography variant="h6">Review Registry</Typography><Typography variant="body2" color="textSecondary" style={{ marginTop: 4 }}>The latest exported HTML audit per customer. Snapshot metrics are frozen at export time and never re-graded from a later session.</Typography></Box>
      <Box display="flex" alignItems="center" style={{ gap: 8 }}>
        <input
          type="file"
          ref={fileInputRef}
          accept=".html,.htm"
          style={{ display: 'none' }}
          onChange={handleFileImport}
        />
        <Button
          size="small"
          variant="contained"
          color="primary"
          onClick={() => fileInputRef.current?.click()}
          disabled={busy || importing}
        >
          {importing ? 'Importing…' : 'Import report'}
        </Button>
        <Button size="small" variant="outlined" onClick={() => void loadReports()} disabled={busy || importing}>
          Refresh registry
        </Button>
      </Box>
    </Box>
    {notice && (
      <Box
        style={{
          marginTop: 12,
          padding: '8px 12px',
          borderRadius: 6,
          border: `1px solid ${notice.type === 'success' ? '#10d96e' : '#f43f5e'}`,
          background: notice.type === 'success' ? 'rgba(16,217,110,.12)' : 'rgba(244,63,94,.12)',
          color: notice.type === 'success' ? '#10d96e' : '#f43f5e',
          fontSize: 12,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <span>{notice.message}</span>
        <Button size="small" style={{ color: 'inherit', minWidth: 'auto', padding: '0 4px' }} onClick={() => setNotice(null)}>✕</Button>
      </Box>
    )}
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
        const isCurrentCustomer = Boolean(
          data?.customerName && isValidCustomerName(data.customerName) &&
          normalizeCustomer(data.customerName) === normalizeCustomer(report.customer)
        );
        const peFallback = isCurrentCustomer ? data?.approvals?.pe?.name : '';
        const custFallback = isCurrentCustomer ? data?.approvals?.customer?.name : '';
        const envFallback = isCurrentCustomer && typeof (data?.resource as any)?.servers?.[0]?.environment === 'string'
          ? (data?.resource as any).servers[0].environment
          : '';

        const peName = (report.pe_name && report.pe_name !== 'Not recorded' && report.pe_name !== '—')
          ? report.pe_name
          : (peFallback || 'Not recorded');

        const custName = (report.cust_name && report.cust_name !== 'Not recorded' && report.cust_name !== '—')
          ? report.cust_name
          : (custFallback || 'Not recorded');

        const displayEnv = (report.env && report.env !== 'Not detected' && report.env !== 'Not Detected' && report.env !== '—')
          ? report.env
          : (envFallback || 'Not detected');

        const docCount = Number(report.documents_count || 0);

        return <React.Fragment key={report.customer_slug}>
          <TableRow hover style={attentionScore(report) > 0 ? { background: 'rgba(245,158,11,.035)' } : undefined}>
            <TableCell><Typography variant="body2" style={{ fontWeight: 700 }}>{report.customer || 'Unknown customer'}</Typography><Typography variant="caption" color="textSecondary">Environment: {displayEnv}</Typography></TableCell>
            <TableCell><Box display="flex" flexDirection="column" alignItems="flex-start" style={{ gap: 5 }}><Tag tone={state.tone}>{state.label}</Tag><Tag tone={count(report.checklist_mismatches) ? 'amber' : 'gray'}>{count(report.checklist_mismatches)} evidence gap{count(report.checklist_mismatches) === 1 ? '' : 's'}</Tag><Typography variant="caption" color="textSecondary">{state.detail}</Typography></Box></TableCell>
            <TableCell style={{ minWidth: 320 }}>
              <Box display="flex" alignItems="center" style={{ gap: 5, overflowX: 'auto', paddingBottom: 4 }}>
                {docCount > 0 ? (
                  <Tag tone="green">📦 {docCount} proof doc{docCount === 1 ? '' : 's'} · ZIP ready</Tag>
                ) : (
                  <Tag tone="gray">Legacy export</Tag>
                )}
                {detailGroups.map((group) => <Tag key={group.label} tone={group.tone}>{group.compact}</Tag>)}
              </Box>
              <Button size="small" onClick={() => toggleExpanded(report.customer_slug)} style={{ marginTop: 4 }}>
                {detailOpen ? 'Hide breakdown & proof vault' : 'Show breakdown & proof vault'}
              </Button>
            </TableCell>
            <TableCell><Typography variant="caption" color="textSecondary">PE</Typography><Typography variant="body2" style={{ fontSize: 12 }}>{peName}</Typography><Typography variant="caption" color="textSecondary">Customer</Typography><Typography variant="body2" style={{ fontSize: 12 }}>{custName}</Typography></TableCell>
            <TableCell style={{ whiteSpace: 'nowrap', fontFamily: 'monospace', fontSize: 11 }}>{formatDate(report.generated_at)}</TableCell>
            <TableCell style={{ whiteSpace: 'nowrap' }}>
              <Button size="small" color="primary" href={`${getApiBaseUrl()}/api/report-archive/${encodeURIComponent(report.customer_slug)}`} target="_blank" rel="noopener noreferrer">Open full HTML</Button>
              <Button size="small" href={`${getApiBaseUrl()}/api/report-archive/${encodeURIComponent(report.customer_slug)}/download`}>Download</Button>
              {docCount > 0 && (
                <Button
                  size="small"
                  variant="outlined"
                  style={{ color: '#10d96e', borderColor: 'rgba(16,217,110,.4)', marginLeft: 6, fontWeight: 700, fontSize: 11 }}
                  href={getAuditPackageDownloadUrl(report.customer_slug)}
                  download
                >
                  ZIP
                </Button>
              )}
            </TableCell>
          </TableRow>
          {detailOpen && (
            <TableRow style={{ background: 'rgba(6,9,26,.48)' }}>
              <TableCell colSpan={6} style={{ padding: 14 }}>
                <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(205px, 1fr))', gap: 8 }}>
                  {detailGroups.map((group) => {
                    const tone = TONE[group.tone];
                    return (
                      <Box key={group.label} style={{ minWidth: 0, padding: 10, border: `1px solid ${tone.border}`, borderRadius: 6, background: 'rgba(17,29,54,.45)' }}>
                        <Tag tone={group.tone}>{group.label}</Tag>
                        {group.lines.map(([label, value]) => (
                          <Box key={label} display="flex" justifyContent="space-between" style={{ gap: 8, marginTop: 6 }}>
                            <Typography variant="caption" color="textSecondary">{label}</Typography>
                            <Typography variant="caption" style={{ fontFamily: 'monospace', textAlign: 'right' }}>{value}</Typography>
                          </Box>
                        ))}
                      </Box>
                    );
                  })}
                </Box>
                <EvidenceVaultView
                  slug={report.customer_slug}
                  customer={report.customer}
                  onRefreshArchive={() => void loadReports()}
                />
              </TableCell>
            </TableRow>
          )}
        </React.Fragment>;
      })}
    </TableBody></Table></Box>}
  </Paper>;
}
