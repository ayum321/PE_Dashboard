import React, { ChangeEvent, useState } from 'react';
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
  Typography,
  makeStyles,
} from '@material-ui/core';
import { uploadSlaMatrix } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';

interface SlaBreach {
  job_name?: string;
  status?: string;
  run_hrs?: number;
  breach_margin_hrs?: number;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2) },
  kpiRow: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2) },
  kpi: { padding: theme.spacing(1.5), minWidth: 120 },
  input: { display: 'none' },
  empty: { marginTop: theme.spacing(2) },
}));

export function SlaMatrixPanel() {
  const classes = useStyles();
  const { data, setSlaMatrix } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await uploadSlaMatrix(file);
      setSlaMatrix(result);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'SLA matrix upload failed.');
    } finally {
      setBusy(false);
    }
  };

  const slaMatrix = data.slaMatrix || {};
  const breaches = ((slaMatrix.breaches as SlaBreach[]) || []).slice(0, 25);

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">SLA Matrix</Typography>
      <Box className={classes.row}>
        <input className={classes.input} id="sla-matrix-input" type="file" accept=".csv,.xlsx,.xls" onChange={handleUpload} />
        <label htmlFor="sla-matrix-input">
          <Button component="span" variant="contained" color="primary" disabled={busy}>
            Upload SLA Matrix
          </Button>
        </label>
        {busy && <CircularProgress size={22} aria-label="Uploading" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      {!data.slaMatrix ? (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload a Ctrl-M file here, or an SLA matrix file in Upload &amp; Intake, to populate this view.
        </Typography>
      ) : (
        <>
          <Box className={classes.kpiRow}>
            <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
              <Typography variant="caption">Compliance</Typography>
              <Typography variant="h6">{(Number(slaMatrix.compliance_pct) || 0).toFixed(1)}%</Typography>
            </Paper>
            <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
              <Typography variant="caption">Total runs</Typography>
              <Typography variant="h6">{Number(slaMatrix.total_runs) || 0}</Typography>
            </Paper>
            <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
              <Typography variant="caption">Breaching</Typography>
              <Typography variant="h6" style={{ color: '#f43f5e' }}>{Number(slaMatrix.breaching_runs) || 0}</Typography>
            </Paper>
            <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
              <Typography variant="caption">At risk</Typography>
              <Typography variant="h6" style={{ color: '#f59e0b' }}>{Number(slaMatrix.at_risk_runs) || 0}</Typography>
            </Paper>
          </Box>
          {breaches.length > 0 && (
            <Table size="small" className="pe-table" aria-label="SLA breach table" style={{ marginTop: 16 }}>
              <TableHead>
                <TableRow>
                  <TableCell>Job</TableCell>
                  <TableCell>Status</TableCell>
                  <TableCell align="right">Runtime hours</TableCell>
                  <TableCell align="right">Over SLA hours</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {breaches.map((breach, index) => (
                  <TableRow key={`${breach.job_name || 'job'}-${index}`}>
                    <TableCell>{breach.job_name || 'Unnamed job'}</TableCell>
                    <TableCell>{breach.status || 'BREACH'}</TableCell>
                    <TableCell align="right">{(breach.run_hrs || 0).toFixed(2)}</TableCell>
                    <TableCell align="right">{(breach.breach_margin_hrs || 0).toFixed(2)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}
    </Paper>
  );
}
