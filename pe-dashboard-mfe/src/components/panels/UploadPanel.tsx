import React, { useEffect, useState } from 'react';
import { Box, Button, CircularProgress, Paper, Typography, makeStyles } from '@material-ui/core';
import { useHistory } from 'react-router-dom';
import {
  connectAzure,
  disconnectAzure,
  getAzureAuthStatus,
  parseSow,
  processBatchMulti,
  refreshBatch,
  ResourceServer,
  uploadBatchSlaXlsx,
  uploadBenchmark,
  uploadDashboardFile,
} from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { BatchIcon, BenchmarkIcon, ResourceIcon, SlaMatrixIcon, SowIcon } from '../../theme/icons';
import { AzureFetchModal } from '../shared/AzureFetchModal';
import { UploadTile } from '../shared/UploadTile';

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
  status: { marginTop: theme.spacing(1) },
  error: { color: theme.palette.error.main },
}));

export function UploadPanel() {
  const classes = useStyles();
  const history = useHistory();
  const { data, setBatch, setResource, setSlaMatrix, setBenchmark, setSowBaseline, setCustomerName } = useAppData();
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [azureAuth, setAzureAuth] = useState<Record<string, unknown> | null>(null);
  const [azureBusy, setAzureBusy] = useState(false);
  const [azureModalOpen, setAzureModalOpen] = useState(false);
  const [progress, setProgress] = useState<Record<string, number | null>>({});

  const trackProgress = (key: string) => (pct: number) => setProgress((prev) => ({ ...prev, [key]: pct }));
  const clearProgress = (key: string) => setProgress((prev) => ({ ...prev, [key]: null }));

  useEffect(() => {
    getAzureAuthStatus().then(setAzureAuth).catch(() => undefined);
  }, []);

  const handleBatchUpload = async (files: File[]) => {
    setBusy(true);
    setError(null);
    try {
      const result = await processBatchMulti(files, trackProgress('batch'));
      setBatch(result);
      // The backend always computes a full per-job SLA matrix alongside batch KPIs
      // (BatchResponse.sla_matrix) — wire it into the SLA Matrix tab immediately so
      // it doesn't sit empty until a separate SLA-matrix-specific file is uploaded.
      const embeddedSlaMatrix = (result as { sla_matrix?: Record<string, unknown> }).sla_matrix;
      if (embeddedSlaMatrix) setSlaMatrix(embeddedSlaMatrix);
      const customer = (result as { customer_name?: string }).customer_name;
      if (customer) setCustomerName(customer);
      setStatus(`Processed ${files.length} Ctrl-M file(s).`);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Batch upload failed.');
    } finally {
      setBusy(false);
      clearProgress('batch');
    }
  };

  const handleResourceDocxUpload = async (files: File[]) => {
    setBusy(true);
    setError(null);
    try {
      const result = await uploadDashboardFile(files[0], trackProgress('resource'));
      setResource({ servers: result.data.servers || [] });
      setStatus(`${result.data.server_count || 0} server(s) parsed from ${result.filename}.`);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Resource upload failed.');
    } finally {
      setBusy(false);
      clearProgress('resource');
    }
  };

  const handleWorkflowSlaUpload = async (files: File[]) => {
    setBusy(true);
    setError(null);
    try {
      // uploadBatchSlaXlsx hits /api/batch-sla/upload — the endpoint that
      // actually parses per-workflow SLA contracts and persists them as the
      // Tier-1 source every batch SLA calculation reads (_batch_sla_xlsx in
      // config_store). It also returns updated_batch_kpis as an immediate
      // partial recompute when Ctrl-M is already loaded.
      const result = await uploadBatchSlaXlsx(files[0], trackProgress('sla'));
      const workflowCount = Number(result.workflow_count) || 0;
      const withSla = Number(result.with_sla_count) || 0;
      // Batch Review's KPIs (SLA source, job/window compliance, buffer gauge) were
      // computed with the OLD ceiling — re-run /api/batch/refresh so they reflect
      // this newly-uploaded SLA file immediately, matching the real dashboard's
      // upload-then-auto-refresh flow (no stale-banner step needed here).
      if (data.batch) {
        try {
          const refreshed = await refreshBatch();
          setBatch(refreshed);
          const embeddedSlaMatrix = (refreshed as { sla_matrix?: Record<string, unknown> }).sla_matrix;
          if (embeddedSlaMatrix) setSlaMatrix(embeddedSlaMatrix);
        } catch {
          // Refresh is best-effort — the SLA matrix upload itself still succeeded.
        }
      }
      setStatus(`Workflow SLA info loaded: ${withSla}/${workflowCount} workflow(s) with an explicit SLA ceiling.`);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Workflow SLA upload failed.');
    } finally {
      setBusy(false);
      clearProgress('sla');
    }
  };

  const handleBenchmarkUpload = async (files: File[]) => {
    setBusy(true);
    setError(null);
    try {
      const result = await uploadBenchmark(files[0], trackProgress('benchmark'));
      setBenchmark(result);
      setStatus(`Benchmark loaded: ${Number(result.total_transactions) || 0} transactions.`);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Benchmark upload failed.');
    } finally {
      setBusy(false);
      clearProgress('benchmark');
    }
  };

  const handleSowUpload = async (files: File[]) => {
    setBusy(true);
    setError(null);
    try {
      const result = await parseSow(files[0], trackProgress('sow'));
      setSowBaseline(result);
      setStatus(`SOW contract parsed from ${files[0].name}.`);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'SOW parse failed.');
    } finally {
      setBusy(false);
      clearProgress('sow');
    }
  };

  const handleAzureConnect = async () => {
    setAzureBusy(true);
    setError(null);
    try {
      const result = await connectAzure();
      setAzureAuth(result);
    } catch (connectError) {
      setError(connectError instanceof Error ? connectError.message : 'Azure sign-in failed.');
    } finally {
      setAzureBusy(false);
    }
  };

  const handleAzureFetched = (servers: ResourceServer[]) => {
    setResource({ servers });
    setStatus(`Live Azure Monitor metrics fetched for ${servers.length} server(s).`);
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
            disabled={busy}
            progress={progress.batch}
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
                <Button component="span" size="small" variant="outlined" style={{ fontSize: 10, borderColor: '#213060', color: '#6b7db3' }} disabled={busy}>
                  {progress.resource != null && progress.resource < 100 ? `Uploading\u2026 ${progress.resource}%` : 'Import supplied report'}
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
            disabled={busy}
            progress={progress.sla}
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
            disabled={busy}
            compact
            progress={progress.benchmark}
            onFiles={handleBenchmarkUpload}
          />
          <UploadTile
            id="bench-ui-input"
            icon={<BenchmarkIcon />}
            accent="#a855f7"
            title="UI Benchmark"
            hint="Transaction load / response times"
            accept=".xlsx,.xls,.csv"
            disabled={busy}
            compact
            progress={progress.benchmark}
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
            disabled={busy}
            progress={progress.sow}
            onFiles={handleSowUpload}
          />
        </Box>
      </Box>

      <Box className={classes.warningBar}>
        <strong style={{ color: '#f59e0b' }}>File picker hanging in Downloads?</strong>{' '}
        Use drag-and-drop into the tile, or move the file to a smaller folder like Desktop before browsing.
      </Box>

      {busy && (
        <Box mt={2}>
          <CircularProgress size={22} aria-label="Uploading" />
        </Box>
      )}
      {status && <Typography className={classes.status} variant="body2">{status}</Typography>}
      {error && (
        <Typography className={`${classes.status} ${classes.error}`} variant="body2" role="alert">
          {error}
        </Typography>
      )}
      <AzureFetchModal open={azureModalOpen} onClose={() => setAzureModalOpen(false)} onFetched={handleAzureFetched} />
    </Paper>
  );
}

