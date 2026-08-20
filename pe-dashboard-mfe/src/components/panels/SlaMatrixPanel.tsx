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
import { SectionBanner } from '../shared/SectionBanner';
import { KpiStatCard } from '../shared/KpiStatCard';

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
  const compliancePct = Number(slaMatrix.compliance_pct) || 0;

  return (
    <Box style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <SectionBanner
        eyebrow="Contract Conformance & Drift"
        title="Is every job measured against the right contract — and which jobs are drifting toward a breach?"
        description="Batch Review answers whether the window was met. This tab answers where each SLA ceiling comes from and which jobs are quietly creeping toward their own limits."
        headline={data.slaMatrix ? `${compliancePct.toFixed(1)}%` : '—'}
        headlineLabel="Window SLA · day-level"
        accent="#a855f7"
      />
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
          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 12, marginTop: 16 }}>
            <KpiStatCard label="Compliance" value={`${compliancePct.toFixed(1)}%`} sub="Runs within SLA ceiling" accent="#10d96e" />
            <KpiStatCard label="Total Runs" value={Number(slaMatrix.total_runs) || 0} sub="Evaluated against SLA" accent="#3b82f6" />
            <KpiStatCard label="Breaching" value={Number(slaMatrix.breaching_runs) || 0} sub="Over SLA ceiling" accent="#f43f5e" />
            <KpiStatCard label="At Risk" value={Number(slaMatrix.at_risk_runs) || 0} sub="Near SLA ceiling" accent="#f59e0b" />
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
    </Box>
  );
}
