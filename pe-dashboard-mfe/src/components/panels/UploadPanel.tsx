import React, { ChangeEvent, useState } from 'react';
import { Box, Button, CircularProgress, Paper, Typography, makeStyles } from '@material-ui/core';
import { processBatchMulti, uploadDashboardFile } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { BatchIcon, ResourceIcon } from '../../theme/icons';

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  tileRow: { display: 'flex', gap: theme.spacing(2), marginTop: theme.spacing(3), flexWrap: 'wrap' },
  tile: { flex: '1 1 260px', padding: theme.spacing(3), textAlign: 'center' },
  input: { display: 'none' },
  status: { marginTop: theme.spacing(1) },
  error: { color: theme.palette.error.main },
}));

export function UploadPanel() {
  const classes = useStyles();
  const { setBatch, setResource, setSlaMatrix, setCustomerName } = useAppData();
  const [batchStatus, setBatchStatus] = useState<string | null>(null);
  const [resourceStatus, setResourceStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const handleBatchUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (!files.length) return;
    setBusy(true);
    setError(null);
    try {
      const result = await processBatchMulti(files);
      setBatch(result);
      const customer = (result as { customer_name?: string }).customer_name;
      if (customer) {
        setCustomerName(customer);
      }
      setBatchStatus(`Processed ${files.length} Ctrl-M file(s).`);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Batch upload failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleResourceUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await uploadDashboardFile(file);
      if (result.classification.type === 'resource') {
        setResource({ servers: result.data.servers || [] });
        setResourceStatus(`${result.data.server_count || 0} server(s) parsed.`);
      } else if (result.classification.type === 'sla_matrix') {
        setSlaMatrix(result.data);
        setResourceStatus(`SLA matrix classified: ${result.data.compliance_pct ?? 0}% compliance.`);
      } else {
        setResourceStatus(`File classified as ${result.classification.type}.`);
      }
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Resource upload failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">Build the audit evidence set</Typography>
      <Typography variant="body2" color="textSecondary">
        Start with Ctrl-M execution history and resource evidence. All calculations are performed by the FastAPI backend.
      </Typography>

      <Box className={classes.tileRow}>
        <Paper className={`${classes.tile} upload-tile`} elevation={0}>
          <div className="upload-tile-icon" style={{ background: 'rgba(59,130,246,.12)', color: '#3b82f6', margin: '0 auto 12px' }}>
            <BatchIcon />
          </div>
          <Typography variant="subtitle2">Ctrl-M Execution History</Typography>
          <Typography variant="caption" color="textSecondary">.csv · .xlsx · .xls — up to 8 files</Typography>
          <Box mt={2}>
            <input
              className={classes.input}
              id="batch-upload-input"
              type="file"
              accept=".csv,.xlsx,.xls"
              multiple
              onChange={handleBatchUpload}
            />
            <label htmlFor="batch-upload-input">
              <Button component="span" variant="contained" color="primary" disabled={busy} fullWidth>
                Upload Ctrl-M Batch Export
              </Button>
            </label>
          </Box>
        </Paper>

        <Paper className={`${classes.tile} upload-tile`} elevation={0}>
          <div className="upload-tile-icon" style={{ background: 'rgba(45,212,191,.12)', color: '#2dd4bf', margin: '0 auto 12px' }}>
            <ResourceIcon />
          </div>
          <Typography variant="subtitle2">Resource / SLA Evidence</Typography>
          <Typography variant="caption" color="textSecondary">Resource report, SLA matrix, benchmark, or SOW file</Typography>
          <Box mt={2}>
            <input
              className={classes.input}
              id="resource-upload-input"
              type="file"
              accept=".csv,.doc,.docx,.pdf,.txt,.xls,.xlsx,.zip"
              onChange={handleResourceUpload}
            />
            <label htmlFor="resource-upload-input">
              <Button component="span" variant="outlined" disabled={busy} fullWidth>
                Upload Resource / SLA File
              </Button>
            </label>
          </Box>
        </Paper>
      </Box>

      {busy && (
        <Box mt={2}>
          <CircularProgress size={22} aria-label="Uploading" />
        </Box>
      )}
      {batchStatus && <Typography className={classes.status} variant="body2">{batchStatus}</Typography>}
      {resourceStatus && <Typography className={classes.status} variant="body2">{resourceStatus}</Typography>}
      {error && (
        <Typography className={`${classes.status} ${classes.error}`} variant="body2" role="alert">
          {error}
        </Typography>
      )}
    </Paper>
  );
}

