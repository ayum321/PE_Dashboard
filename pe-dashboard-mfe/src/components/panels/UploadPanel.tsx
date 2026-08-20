import React, { ChangeEvent, useState } from 'react';
import { Box, Button, CircularProgress, Paper, Typography, makeStyles } from '@material-ui/core';
import { processBatchMulti, uploadDashboardFile } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2), flexWrap: 'wrap' },
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
    <Paper className={classes.panel} elevation={0}>
      <Typography variant="h6">Upload &amp; Intake</Typography>
      <Typography variant="body2" color="textSecondary">
        Start with Ctrl-M execution history and resource evidence. All calculations are performed by the FastAPI backend.
      </Typography>
      <Box className={classes.row}>
        <input
          className={classes.input}
          id="batch-upload-input"
          type="file"
          accept=".csv,.xlsx,.xls"
          multiple
          onChange={handleBatchUpload}
        />
        <label htmlFor="batch-upload-input">
          <Button component="span" variant="contained" color="primary" disabled={busy}>
            Upload Ctrl-M Batch Export
          </Button>
        </label>
        <input
          className={classes.input}
          id="resource-upload-input"
          type="file"
          accept=".csv,.doc,.docx,.pdf,.txt,.xls,.xlsx,.zip"
          onChange={handleResourceUpload}
        />
        <label htmlFor="resource-upload-input">
          <Button component="span" variant="outlined" disabled={busy}>
            Upload Resource / SLA File
          </Button>
        </label>
        {busy && <CircularProgress size={22} aria-label="Uploading" />}
      </Box>
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
