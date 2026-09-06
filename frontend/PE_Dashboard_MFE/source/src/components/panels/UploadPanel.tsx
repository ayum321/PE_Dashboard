import React, { useEffect, useRef, useState } from 'react';
import { Box, Button, Paper, Typography, makeStyles } from '@material-ui/core';
import { useHistory } from 'react-router-dom';
import {
  DashboardPayload,
  disconnectAzure,
  getAzureAuthStatus,
  getExecutiveDashboard,
  getFinalJudgment,
  getPeNarrative,
  getRedFlags,
  generateFindings,
  parseSow,
  processBatchMulti,
  refreshBatch,
  ResourceServer,
  uploadBatchSlaXlsx,
  uploadBenchmark,
  uploadDashboardFile,
  workbookSlaSnapshotFromUpload,
} from '../../api/dashboardApi';
import { AppData, isValidCustomerName, useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload, buildFinalJudgmentPayload, buildPeNarrativePayload } from '../../utils/buildAnalysisPayload';
import { BatchIcon, BenchmarkIcon, ResourceIcon, SlaMatrixIcon, SowIcon } from '../../theme/icons';
import { AzureFetchMeta, AzureFetchModal } from '../shared/AzureFetchModal';
import { UploadTile } from '../shared/UploadTile';

type UploadKey = 'batch' | 'resource' | 'sla' | 'benchmark' | 'sow';
type UploadPhase = 'uploading' | 'processing' | 'complete' | 'error';

interface IntakeUpload {
  key: UploadKey;
  title: string;
  filenames: string[];
  phase: UploadPhase;
  progress: number;
  loaded: number;
  total: number;
  message: string;
  metric?: string;
  updatedAt: number;
}

const intakeAccents: Record<UploadKey, string> = {
  batch: '#10d96e', resource: '#3b82f6', sla: '#2dd4bf', benchmark: '#f59e0b', sow: '#22d3ee',
};

const formatBytes = (bytes: number) => {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

function IntakeActivityCard({ upload }: { upload: IntakeUpload }) {
  const accent = intakeAccents[upload.key];
  const inFlight = upload.phase === 'uploading' || upload.phase === 'processing';
  const phaseLabel = upload.phase === 'uploading'
    ? 'UPLOADING'
    : upload.phase === 'processing'
      ? 'PROCESSING EVIDENCE'
      : upload.phase === 'complete'
        ? 'READY'
        : 'NEEDS ATTENTION';
  const transferDetail = upload.phase === 'uploading'
    ? `${formatBytes(upload.loaded)} / ${formatBytes(upload.total)}`
    : upload.phase === 'processing'
      ? 'Upload received — rebuilding shared audit evidence'
      : upload.message;

  return (
    <Box
      role={upload.phase === 'error' ? 'alert' : 'status'}
      aria-live="polite"
      style={{
        position: 'relative', overflow: 'hidden', minWidth: 0, borderRadius: 14,
        padding: '14px 16px', border: `1px solid ${accent}66`,
        background: `linear-gradient(135deg, ${accent}18 0%, rgba(12,22,44,.96) 42%, rgba(9,16,34,.98) 100%)`,
        boxShadow: `0 12px 28px ${accent}14, inset 0 1px 0 rgba(255,255,255,.05)`,
      }}
    >
      <Box style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <span className={`status-dot ${upload.phase === 'error' ? 'status-dot-red' : inFlight ? 'status-dot-blue' : 'status-dot-green'}`} style={{ marginTop: 5 }} />
        <Box style={{ minWidth: 0, flex: 1 }}>
          <Box style={{ display: 'flex', alignItems: 'baseline', gap: 8, justifyContent: 'space-between' }}>
            <Typography variant="subtitle2" style={{ color: '#f4f7ff', fontWeight: 800, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {upload.filenames.join(', ')}
            </Typography>
            <Typography variant="caption" style={{ flexShrink: 0, color: accent, fontWeight: 800, letterSpacing: '.08em', fontFamily: "'JetBrains Mono', monospace" }}>
              {inFlight ? `${upload.progress}%` : upload.metric || phaseLabel}
            </Typography>
          </Box>
          <Typography variant="caption" style={{ display: 'block', marginTop: 2, color: accent, fontWeight: 800, letterSpacing: '.08em', fontFamily: "'JetBrains Mono', monospace" }}>
            {phaseLabel} <span style={{ color: '#6b7db3', fontWeight: 500 }}>· {upload.title}</span>
          </Typography>
          <Typography variant="caption" style={{ display: 'block', marginTop: 5, color: upload.phase === 'error' ? '#fb7185' : '#9badcf' }}>
            {transferDetail}
          </Typography>
        </Box>
      </Box>
      {inFlight && (
        <Box style={{ height: 5, overflow: 'hidden', borderRadius: 999, background: 'rgba(255,255,255,.08)', marginTop: 11 }}>
          <Box style={{ height: '100%', width: `${Math.max(4, upload.progress)}%`, borderRadius: 999, background: `linear-gradient(90deg, ${accent}, #38bdf8)`, boxShadow: `0 0 14px ${accent}`, transition: 'width .2s ease' }} />
        </Box>
      )}
    </Box>
  );
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  headerRow: { display: 'flex', alignItems: 'center', gap: theme.spacing(2), marginBottom: theme.spacing(2) },
  headerIcon: {
    width: 44, height: 44, borderRadius: 12, flexShrink: 0,
    background: 'linear-gradient(135deg, rgba(59,130,246,.3), rgba(168,85,247,.3))',
    border: '1px solid rgba(59,130,246,.3)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#3b82f6',
  },
  stepRow: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: theme.spacing(1), marginBottom: theme.spacing(2) },
  step: { borderRadius: 8, padding: '8px 12px', fontSize: 12 },
  zoneRow: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: theme.spacing(2) },
  sectionDivider: { marginTop: theme.spacing(3), paddingTop: theme.spacing(2), borderTop: '1px solid #213060' },
  warningBar: {
    marginTop: theme.spacing(2), borderRadius: 12, border: '1px solid rgba(245,158,11,.3)',
    background: 'rgba(245,158,11,.05)', padding: '12px 16px', fontSize: 11, color: 'rgba(240,244,255,.9)',
  },
  azureCard: {
    borderRadius: 12, border: '1px solid #213060',
    background: 'linear-gradient(135deg, #0a1628 0%, #0d1a30 100%)', minHeight: 130,
    display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
  },
  input: { display: 'none' },
}));

export function UploadPanel() {
  const classes = useStyles();
  const history = useHistory();
  const {
    data,
    setBatch,
    setResource,
    setSlaMatrix,
    setBenchmark,
    setSowBaseline,
    setCustomerName,
    setFindings,
    setRedFlags,
    setPeNarrative,
    setExecutive,
    setFinalJudgment,
  } = useAppData();
  const [azureAuth, setAzureAuth] = useState<Record<string, unknown> | null>(null);
  const [azureBusy, setAzureBusy] = useState(false);
  const [azureModalOpen, setAzureModalOpen] = useState(false);
  const [azureMessage, setAzureMessage] = useState<string | null>(null);
  const [uploads, setUploads] = useState<Partial<Record<UploadKey, IntakeUpload>>>({});
  const derivedRefreshId = useRef(0);

  const beginUpload = (key: UploadKey, files: File[], title: string) => {
    const total = files.reduce((sum, file) => sum + file.size, 0);
    setUploads((previous) => ({
      ...previous,
      [key]: { key, title, filenames: files.map((file) => file.name), phase: 'uploading', progress: 0, loaded: 0, total, message: '', updatedAt: Date.now() },
    }));
  };

  const trackProgress = (key: UploadKey) => (pct: number, loaded?: number, total?: number) => {
    setUploads((previous) => {
      const current = previous[key];
      if (!current) return previous;
      return { ...previous, [key]: { ...current, progress: pct, loaded: loaded ?? current.loaded, total: total ?? current.total, updatedAt: Date.now() } };
    });
  };

  const markProcessing = (key: UploadKey, message = 'Upload received — rebuilding shared audit evidence') => {
    setUploads((previous) => {
      const current = previous[key];
      return current ? { ...previous, [key]: { ...current, phase: 'processing', progress: 100, message, updatedAt: Date.now() } } : previous;
    });
  };

  const completeUpload = (key: UploadKey, message: string, metric?: string) => {
    setUploads((previous) => {
      const current = previous[key];
      return current ? { ...previous, [key]: { ...current, phase: 'complete', progress: 100, loaded: current.total || current.loaded, message, metric, updatedAt: Date.now() } } : previous;
    });
  };

  const failUpload = (key: UploadKey, message: string) => {
    setUploads((previous) => {
      const current = previous[key];
      return current ? { ...previous, [key]: { ...current, phase: 'error', message, updatedAt: Date.now() } } : previous;
    });
  };

  const isUploading = (key: UploadKey) => {
    const phase = uploads[key]?.phase;
    return phase === 'uploading' || phase === 'processing';
  };

  useEffect(() => {
    getAzureAuthStatus().then(setAzureAuth).catch(() => undefined);
  }, []);

  /**
   * Mirrors the local dashboard's evidence cascade.  Each API receives the
   * just-uploaded in-memory value rather than waiting for React state to flush,
   * so navigating to any analysis screen cannot show the previous engagement.
   */
  const refreshDerivedEvidence = async (nextData: AppData): Promise<string> => {
    const refreshId = ++derivedRefreshId.current;
    const stillCurrent = () => refreshId === derivedRefreshId.current;
    const payload = buildAnalysisPayload(nextData);
    let findings: Record<string, unknown> | null = null;
    let redFlags: Record<string, unknown> | null = null;
    let executive: Record<string, unknown> | null = null;
    const unavailable: string[] = [];

    try {
      findings = await generateFindings(payload);
      if (stillCurrent()) setFindings(findings);
    } catch {
      unavailable.push('findings');
    }

    try {
      redFlags = await getRedFlags(payload);
      if (stillCurrent()) setRedFlags(redFlags);
    } catch {
      unavailable.push('questions');
    }

    try {
      executive = await getExecutiveDashboard({
        ...payload,
        sla_data: nextData.slaMatrix,
        findings: findings?.findings,
      });
      if (stillCurrent()) setExecutive(executive);
    } catch {
      unavailable.push('executive view');
    }

    try {
      const narrative = await getPeNarrative(buildPeNarrativePayload(nextData, { findings, redFlags }));
      if (stillCurrent()) setPeNarrative(narrative);
    } catch {
      unavailable.push('PE review summary');
    }

    try {
      const judgment = await getFinalJudgment(buildFinalJudgmentPayload(nextData, { findings, redFlags, executive }));
      if (stillCurrent()) setFinalJudgment(judgment);
    } catch {
      unavailable.push('final judgment');
    }

    if (!stillCurrent()) return 'A newer intake upload is reconciling the shared evidence.';

    return unavailable.length
      ? `Batch evidence is ready; ${unavailable.join(', ')} can be refreshed from PE Findings.`
      : 'PE Findings, review summary, executive dashboard, and final judgment refreshed.';
  };

  const clearDerivedEvidence = () => {
    setFindings(null);
    setRedFlags(null);
    setPeNarrative(null);
    setExecutive(null);
    setFinalJudgment(null);
  };

  const handleBatchUpload = async (files: File[]) => {
    beginUpload('batch', files, 'Ctrl-M execution history');
    try {
      const result = await processBatchMulti(files, trackProgress('batch'));
      markProcessing('batch');
      let resolvedBatch = result;
      // Ctrl-M is Batch Review evidence. It may use the workbook's ceilings for
      // its own KPI calculation, but it must never populate or replace the
      // workbook-only SLA Matrix tab.
      try {
        resolvedBatch = await refreshBatch();
      } catch {
        // The original process response is already a valid full batch result.
      }
      setBatch(resolvedBatch);
      const customer = (resolvedBatch as { customer_name?: string }).customer_name;
      if (customer) setCustomerName(customer);
      // A new Ctrl-M extract creates a new shared evidence set.  Clear every
      // derived screen first, then recalculate it from the returned payload.
      clearDerivedEvidence();
      const nextData: AppData = {
        ...data,
        batch: resolvedBatch,
        slaMatrix: data.slaMatrix,
        customerName: customer || data.customerName,
        findings: null,
        redFlags: null,
        peNarrative: null,
        executive: null,
        finalJudgment: null,
      };
      const refreshStatus = await refreshDerivedEvidence(nextData);
      const totalRuns = Number((resolvedBatch as { kpis?: { total_runs?: number }; total_runs?: number }).kpis?.total_runs
        || (resolvedBatch as { total_runs?: number }).total_runs) || 0;
      completeUpload('batch', `Processed ${files.length} Ctrl-M file(s). ${refreshStatus}`, totalRuns ? `${totalRuns.toLocaleString()} runs` : undefined);
    } catch (uploadError) {
      failUpload('batch', uploadError instanceof Error ? uploadError.message : 'Batch upload failed.');
    }
  };

  const handleResourceDocxUpload = async (files: File[]) => {
    beginUpload('resource', files, 'Resource report');
    try {
      const result = await uploadDashboardFile(files[0], trackProgress('resource'));
      markProcessing('resource');
      const resource = { ...result.data, servers: result.data.servers || [] };
      setResource(resource);
      // Resource reports often name the customer in a title/heading even when
      // Ctrl-M's filename doesn't match the expected pattern. Adopt it only
      // when no customer is already known — same rule as the Azure live-fetch
      // path (handleFetched in ResourcePanel.tsx) — so it never silently
      // overrides an engagement already established from Ctrl-M/SOW.
      const resourceCustomer = (result.data as { customer_name?: string }).customer_name;
      const validResourceCustomer = isValidCustomerName(resourceCustomer) ? resourceCustomer : null;
      if (validResourceCustomer) setCustomerName(validResourceCustomer);
      clearDerivedEvidence();
      const refreshStatus = await refreshDerivedEvidence({
        ...data,
        resource,
        customerName: validResourceCustomer || (isValidCustomerName(data.customerName) ? data.customerName : null),
        findings: null,
        redFlags: null,
        peNarrative: null,
        executive: null,
        finalJudgment: null,
      });
      const serverCount = Number(result.data.server_count) || 0;
      completeUpload('resource', `${serverCount} server(s) parsed from ${result.filename}. ${refreshStatus}`, serverCount ? `${serverCount} servers` : undefined);
    } catch (uploadError) {
      failUpload('resource', uploadError instanceof Error ? uploadError.message : 'Resource upload failed.');
    }
  };

  const handleWorkflowSlaUpload = async (files: File[]) => {
    beginUpload('sla', files, 'Workflow SLA contract');
    try {
      // The workbook response includes the standalone SLA Matrix snapshot.
      // Ctrl-M can use the same ceilings for Batch Review, but cannot replace
      // this contract/observation view.
      const result = await uploadBatchSlaXlsx(files[0], trackProgress('sla'));
      markProcessing('sla');
      if (result.file_role === 'batch_execution_history') {
        const batchPayload = result.batch_payload;
        if (!batchPayload || typeof batchPayload !== 'object' || Array.isArray(batchPayload)) {
          throw new Error('Execution-history handoff returned no Batch Review payload.');
        }
        setBatch(batchPayload as AppData['batch']);
        clearDerivedEvidence();
        completeUpload('sla', 'Execution-history workbook detected and loaded into Batch Review. No SLA contract was inferred because the source has no SLA target.', 'Batch Review');
        history.push('/batch');
        return;
      }
      const workflowCount = Number(result.workflow_count) || 0;
      const withSla = Number(result.with_sla_count) || 0;
      const mappingReport = result.mapping_report as { sheets?: Array<{ mapped?: unknown[]; absent_optional?: unknown[] }> } | undefined;
      const mappingSheet = mappingReport?.sheets?.find((sheet) => (sheet.mapped || []).length > 0);
      const mappedFields = mappingSheet?.mapped?.length || 0;
      const absentOptional = mappingSheet?.absent_optional?.length || 0;
      const mappingSummary = `Schema mapping: ${mappedFields} field(s) mapped${absentOptional ? ` · ${absentOptional} optional field(s) not present in source` : ''}.`;
      let refreshedBatch: AppData['batch'] = null;
      const workbookSlaMatrix = workbookSlaSnapshotFromUpload(result);
      setSlaMatrix(workbookSlaMatrix);
      // Recompute Batch Review only when its Ctrl-M evidence is already cached;
      // this has no effect on the workbook SLA Matrix snapshot above.
      try {
        const refreshed = await refreshBatch();
        setBatch(refreshed);
        refreshedBatch = refreshed;
      } catch {
        // Batch evidence may still be uploading in parallel. The workbook SLA
        // Matrix is already complete and remains independent.
      }
      if (refreshedBatch) {
        clearDerivedEvidence();
        const refreshStatus = await refreshDerivedEvidence({
          ...data,
          batch: refreshedBatch,
          slaMatrix: workbookSlaMatrix || data.slaMatrix,
          findings: null,
          redFlags: null,
          peNarrative: null,
          executive: null,
          finalJudgment: null,
        });
        completeUpload('sla', `Workflow SLA info loaded: ${withSla}/${workflowCount} workflow(s) with an explicit SLA ceiling. ${mappingSummary} ${refreshStatus}`, `${withSla}/${workflowCount} SLA`);
      } else {
        completeUpload('sla', `Workbook SLA Matrix loaded: ${withSla}/${workflowCount} workflow(s) with an SLA ceiling. ${mappingSummary} Batch Review can be uploaded separately when Ctrl-M evidence is available.`, `${withSla}/${workflowCount} SLA`);
      }
    } catch (uploadError) {
      failUpload('sla', uploadError instanceof Error ? uploadError.message : 'Workflow SLA upload failed.');
    }
  };

  const handleBenchmarkUpload = async (files: File[]) => {
    beginUpload('benchmark', files, 'Performance benchmark');
    try {
      const result = await uploadBenchmark(files[0], trackProgress('benchmark'));
      markProcessing('benchmark');
      setBenchmark(result);
      clearDerivedEvidence();
      const refreshStatus = await refreshDerivedEvidence({
        ...data,
        benchmark: result,
        findings: null,
        redFlags: null,
        peNarrative: null,
        executive: null,
        finalJudgment: null,
      });
      const transactions = Number(result.total_transactions) || 0;
      completeUpload('benchmark', `Benchmark loaded: ${transactions.toLocaleString()} transactions. ${refreshStatus}`, transactions ? `${transactions.toLocaleString()} txns` : undefined);
    } catch (uploadError) {
      failUpload('benchmark', uploadError instanceof Error ? uploadError.message : 'Benchmark upload failed.');
    }
  };

  const handleSowUpload = async (files: File[]) => {
    beginUpload('sow', files, 'SOW contract');
    try {
      const result = await parseSow(files[0], trackProgress('sow'));
      markProcessing('sow');
      setSowBaseline(result);
      const sowCustomer = (result as { customer_name?: string }).customer_name;
      const validSowCustomer = isValidCustomerName(sowCustomer) ? sowCustomer : null;
      if (validSowCustomer) setCustomerName(validSowCustomer);
      clearDerivedEvidence();
      const refreshStatus = await refreshDerivedEvidence({
        ...data,
        sowBaseline: result,
        customerName: validSowCustomer || (isValidCustomerName(data.customerName) ? data.customerName : null),
        findings: null,
        redFlags: null,
        peNarrative: null,
        executive: null,
        finalJudgment: null,
      });
      completeUpload('sow', `SOW contract parsed from ${files[0].name}. ${refreshStatus}`);
    } catch (uploadError) {
      failUpload('sow', uploadError instanceof Error ? uploadError.message : 'SOW parse failed.');
    }
  };

  const handleAzureConnect = () => {
    setAzureMessage(null);
    setAzureModalOpen(true);
  };

  const handleAzureFetched = (servers: ResourceServer[], meta: AzureFetchMeta, resolved: DashboardPayload) => {
    const validCustomer = isValidCustomerName(meta.customer) ? meta.customer : undefined;
    const customerServers = servers.map((s) => ({
      ...s,
      customer: validCustomer || (isValidCustomerName(s.customer) ? s.customer : undefined),
    }));
    const resource = {
      ...resolved,
      servers: customerServers,
      hours_back: resolved.hours_back ?? meta.hoursBack,
      customer_name: validCustomer,
      customer_status: validCustomer ? meta.customerStatus : 'untagged',
      customer_message: meta.customerMessage,
      customer_source: validCustomer ? 'azure_vm_tags' : undefined,
    };
    setResource(resource);
    if (validCustomer) setCustomerName(validCustomer);
    setAzureMessage(`Live Azure Monitor metrics fetched for ${servers.length} server(s).`);
    // Matches vanilla's runAzureFetch(): jump straight to Resource Review so
    // the fetch result is immediately visible instead of sitting on Upload.
    history.push('/resource');
  };

  const handleAzureDisconnect = async () => {
    setAzureBusy(true);
    try {
      await disconnectAzure();
      setAzureAuth({ method: 'none' });
    } finally {
      setAzureBusy(false);
    }
  };

  const connected = azureAuth?.method === 'browser';
  const activityUploads = Object.values(uploads)
    .filter((upload): upload is IntakeUpload => Boolean(upload))
    .sort((left, right) => right.updatedAt - left.updatedAt);

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Box className={classes.headerRow}>
        <div className={classes.headerIcon}><BatchIcon /></div>
        <Box>
          <Typography variant="h6">Build the audit evidence set</Typography>
          <Typography variant="body2" color="textSecondary">
            Start with Ctrl-M execution history and resource evidence. Add workflow SLA information for an auditable
            compliance verdict; SOW and benchmark files provide supporting context.
          </Typography>
        </Box>
      </Box>

      <Box className={classes.stepRow}>
        <Box className={classes.step} style={{ border: '1px solid rgba(16,217,110,.3)', background: 'rgba(16,217,110,.05)' }}>
          <div style={{ fontWeight: 700, color: '#10d96e' }}>1. Ctrl-M history</div>
          <div style={{ color: '#6b7db3', marginTop: 2 }}>Required for batch runtime and failure analysis</div>
        </Box>
        <Box className={classes.step} style={{ border: '1px solid rgba(59,130,246,.3)', background: 'rgba(59,130,246,.05)' }}>
          <div style={{ fontWeight: 700, color: '#3b82f6' }}>2. Resource evidence</div>
          <div style={{ color: '#6b7db3', marginTop: 2 }}>Required for infrastructure health and correlation</div>
        </Box>
        <Box className={classes.step} style={{ border: '1px solid rgba(45,212,191,.3)', background: 'rgba(45,212,191,.05)' }}>
          <div style={{ fontWeight: 700, color: '#2dd4bf' }}>3. Workflow SLA</div>
          <div style={{ color: '#6b7db3', marginTop: 2 }}>Recommended for customer-specific compliance</div>
        </Box>
      </Box>

      <Box className={classes.zoneRow}>
        <Box>
          <Typography variant="caption" style={{ display: 'block', fontWeight: 700, color: '#f0f4ff', textTransform: 'uppercase', letterSpacing: '.08em', marginBottom: 6 }}>
            Ctrl-M Batch Export
          </Typography>
          <UploadTile
            id="batch-upload-input"
            icon={<BatchIcon />}
            accent="#10d96e"
            title="Ctrl-M Execution History"
            hint=".csv · .xlsx · .xls · up to 8 files"
            accept=".csv,.xlsx,.xls"
            multiple
            disabled={isUploading('batch')}
            progress={uploads.batch?.phase === 'uploading' ? uploads.batch.progress : null}
            onFiles={handleBatchUpload}
          />
        </Box>

        <Box>
          <Typography variant="caption" style={{ display: 'block', fontWeight: 700, color: '#f0f4ff', textTransform: 'uppercase', letterSpacing: '.08em', marginBottom: 6 }}>
            Resource Report
          </Typography>
          <Box className={classes.azureCard}>
            <Box style={{ padding: '14px 16px 8px', display: 'flex', gap: 12 }}>
              <div className="upload-tile-icon" style={{ background: 'rgba(59,130,246,.15)', color: '#3b82f6', flexShrink: 0 }}>
                <ResourceIcon />
              </div>
              <Box style={{ minWidth: 0 }}>
                <Box style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span className={`status-dot ${connected ? 'status-dot-green' : 'status-dot-muted'}`} />
                  <Typography variant="caption" style={{ fontWeight: 600, color: connected ? '#10d96e' : '#6b7db3' }}>
                    {connected ? 'Connected' : 'Not connected'}
                  </Typography>
                </Box>
                {connected && (
                  <Typography variant="caption" style={{ display: 'block', color: 'rgba(240,244,255,.5)', marginTop: 2 }}>
                    {String(azureAuth?.display_name || azureAuth?.name || '')}
                  </Typography>
                )}
              </Box>
            </Box>
            <Box style={{ padding: '0 12px 12px', display: 'flex', gap: 8 }}>
              <Button
                size="small"
                variant="outlined"
                style={{ flex: 1, fontSize: 10, borderColor: 'rgba(59,130,246,.4)', color: '#3b82f6' }}
                onClick={connected ? () => setAzureModalOpen(true) : handleAzureConnect}
                disabled={azureBusy}
              >
                {azureBusy ? '...' : connected ? 'Fetch live metrics' : 'Connect Azure'}
              </Button>
              <label htmlFor="resource-docx-input">
                <input
                  className={classes.input}
                  id="resource-docx-input"
                  type="file"
                  accept=".docx"
                  onChange={(event) => {
                    const files = Array.from(event.target.files || []);
                    event.target.value = '';
                    if (files.length) handleResourceDocxUpload(files);
                  }}
                />
                <Button component="span" size="small" variant="outlined" style={{ fontSize: 10, borderColor: '#213060', color: '#6b7db3' }} disabled={isUploading('resource')}>
                  {uploads.resource?.phase === 'uploading' ? `Uploading\u2026 ${uploads.resource.progress}%` : uploads.resource?.phase === 'processing' ? 'Processing evidence…' : 'Import supplied report'}
                </Button>
              </label>
            </Box>
            {connected && (
              <Box style={{ padding: '0 12px 10px' }}>
                <Button size="small" onClick={handleAzureDisconnect} disabled={azureBusy} style={{ fontSize: 9, color: '#6b7db3' }}>
                  Disconnect
                </Button>
              </Box>
            )}
            {azureMessage && (
              <Typography variant="caption" style={{ display: 'block', padding: '0 12px 10px', color: azureMessage.includes('failed') ? '#fb7185' : '#6b7db3' }}>
                {azureMessage}
              </Typography>
            )}
          </Box>
        </Box>

        <Box>
          <Typography variant="caption" style={{ display: 'block', fontWeight: 700, color: '#f0f4ff', textTransform: 'uppercase', letterSpacing: '.08em', marginBottom: 6 }}>
            Workflow SLA Info
          </Typography>
          <UploadTile
            id="workflow-sla-input"
            icon={<SlaMatrixIcon />}
            accent="#2dd4bf"
            title="BatchSLA_info.xlsx"
            hint=".xlsx · .csv"
            browseLabel="browse"
            accept=".xlsx,.xls,.csv"
            disabled={isUploading('sla')}
            progress={uploads.sla?.phase === 'uploading' ? uploads.sla.progress : null}
            onFiles={handleWorkflowSlaUpload}
          />
        </Box>
      </Box>

      <Box className={classes.sectionDivider}>
        <Typography variant="subtitle2">Add supporting evidence</Typography>
        <Typography variant="caption" color="textSecondary">
          Benchmarks and contract context strengthen regression, volume, and scope findings.
        </Typography>
      </Box>

      <Box className={classes.zoneRow} style={{ marginTop: 12 }}>
        <Box style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <Typography variant="caption" style={{ fontWeight: 700, color: '#f0f4ff', textTransform: 'uppercase', letterSpacing: '.08em' }}>
            Performance Benchmark
          </Typography>
          <UploadTile
            id="bench-batch-input"
            icon={<BenchmarkIcon />}
            accent="#f59e0b"
            title="Batch Runtime Performance"
            hint="Job runtimes — new vs old release"
            accept=".xlsx,.xls,.csv"
            disabled={isUploading('benchmark')}
            compact
            progress={uploads.benchmark?.phase === 'uploading' ? uploads.benchmark.progress : null}
            onFiles={handleBenchmarkUpload}
          />
          <UploadTile
            id="bench-ui-input"
            icon={<BenchmarkIcon />}
            accent="#a855f7"
            title="UI Benchmark"
            hint="Transaction load / response times"
            accept=".xlsx,.xls,.csv"
            disabled={isUploading('benchmark')}
            compact
            progress={uploads.benchmark?.phase === 'uploading' ? uploads.benchmark.progress : null}
            onFiles={handleBenchmarkUpload}
          />
        </Box>

        <Box>
          <Typography variant="caption" style={{ display: 'block', fontWeight: 700, color: '#f0f4ff', textTransform: 'uppercase', letterSpacing: '.08em', marginBottom: 6 }}>
            SOW Contract
          </Typography>
          <UploadTile
            id="sow-upload-input"
            icon={<SowIcon />}
            accent="#22d3ee"
            title="SOW / Schedule 1-A PDF"
            hint=".pdf · .docx"
            browseLabel="browse"
            accept=".pdf,.docx,.txt"
            disabled={isUploading('sow')}
            progress={uploads.sow?.phase === 'uploading' ? uploads.sow.progress : null}
            onFiles={handleSowUpload}
          />
        </Box>
      </Box>

      <Box className={classes.warningBar}>
        <strong style={{ color: '#f59e0b' }}>File picker hanging in Downloads?</strong>{' '}
        Use drag-and-drop into the tile, or move the file to a smaller folder like Desktop before browsing.
      </Box>

      {activityUploads.length > 0 && (
        <Box mt={3}>
          <Box style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 9 }}>
            <Typography variant="subtitle2" style={{ color: '#f0f4ff', fontWeight: 800 }}>Intake activity</Typography>
            <Typography variant="caption" style={{ color: '#6b7db3' }}>Live upload, evidence processing, and confirmed availability</Typography>
          </Box>
          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 12 }}>
            {activityUploads.map((upload) => <IntakeActivityCard key={upload.key} upload={upload} />)}
          </Box>
        </Box>
      )}
      <AzureFetchModal
        open={azureModalOpen}
        autoStartAuth={!connected}
        onClose={() => setAzureModalOpen(false)}
        onFetched={handleAzureFetched}
        onAuthChanged={setAzureAuth}
      />
    </Paper>
  );
}

