import React, { useState } from 'react';
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
import { getExecutiveDashboard } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload } from '../../utils/buildAnalysisPayload';

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2) },
  kpiGrid: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2) },
  kpi: { padding: theme.spacing(1.5), minWidth: 140 },
  narrative: { marginTop: theme.spacing(2), padding: theme.spacing(2) },
  empty: { marginTop: theme.spacing(2) },
}));

const isPrimitive = (value: unknown): value is string | number =>
  typeof value === 'number' || typeof value === 'string';

export function ExecutivePanel() {
  const classes = useStyles();
  const { data, setExecutive } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const basePayload = buildAnalysisPayload(data);
      const payload = {
        ...basePayload,
        sla_data: data.slaMatrix,
        findings: data.findings?.findings,
      };
      const result = await getExecutiveDashboard(payload);
      setExecutive(result);
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : 'Generating executive view failed.');
    } finally {
      setBusy(false);
    }
  };

  const kpis = (data.executive?.kpis as Record<string, unknown>) || {};
  const kpiEntries = Object.entries(kpis).filter(([, value]) => isPrimitive(value));
  const subAppMetrics = (data.executive?.sub_app_metrics as Record<string, unknown>[]) || [];
  const subAppColumns = subAppMetrics.length > 0 ? Object.keys(subAppMetrics[0]).filter((key) => isPrimitive(subAppMetrics[0][key])) : [];

  return (
    <Paper className={classes.panel} elevation={0}>
      <Typography variant="h6">Executive Dashboard</Typography>
      <Box className={classes.row}>
        <Button variant="contained" color="primary" onClick={handleGenerate} disabled={busy}>
          Generate Executive Summary
        </Button>
        {busy && <CircularProgress size={22} aria-label="Generating executive summary" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      {!data.executive ? (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload batch and resource data first, then generate the executive correlation summary.
        </Typography>
      ) : (
        <>
          <Box className={classes.kpiGrid}>
            {kpiEntries.map(([key, value]) => (
              <Paper key={key} className={classes.kpi} elevation={0} variant="outlined">
                <Typography variant="caption">{key.replace(/_/g, ' ')}</Typography>
                <Typography variant="h6">{String(value)}</Typography>
              </Paper>
            ))}
          </Box>
          {typeof data.executive.narrative === 'string' && data.executive.narrative && (
            <Paper className={classes.narrative} elevation={0} variant="outlined">
              <Typography variant="body2">{data.executive.narrative}</Typography>
            </Paper>
          )}
          {subAppMetrics.length > 0 && (
            <Table size="small" aria-label="Sub-application metrics" style={{ marginTop: 16 }}>
              <TableHead>
                <TableRow>
                  {subAppColumns.map((column) => (
                    <TableCell key={column}>{column.replace(/_/g, ' ')}</TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {subAppMetrics.slice(0, 25).map((row, index) => (
                  <TableRow key={index}>
                    {subAppColumns.map((column) => (
                      <TableCell key={column}>{String(row[column])}</TableCell>
                    ))}
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
