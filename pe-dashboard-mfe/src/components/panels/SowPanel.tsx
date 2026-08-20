import React, { ChangeEvent, useEffect, useState } from 'react';
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
import { compareSow, getSowBaseline, parseSow, saveSowBaseline } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';

interface SowMetric {
  key: string;
  label: string;
  sow: number;
  actual: number;
  pct: number;
  status: string;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2), flexWrap: 'wrap' },
  input: { display: 'none' },
  fields: { display: 'flex', gap: theme.spacing(2), marginTop: theme.spacing(2), flexWrap: 'wrap' },
}));

export function SowPanel() {
  const classes = useStyles();
  const { data, setSowBaseline, setSowCompare } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dailyDfu, setDailyDfu] = useState('');
  const [dailySku, setDailySku] = useState('');
  const [dailyOrders, setDailyOrders] = useState('');
  const [batchJobs, setBatchJobs] = useState('');

  useEffect(() => {
    getSowBaseline()
      .then((baseline) => {
        setSowBaseline(baseline);
        if (baseline.daily_dfu != null) setDailyDfu(String(baseline.daily_dfu));
        if (baseline.daily_sku != null) setDailySku(String(baseline.daily_sku));
        if (baseline.daily_orders != null) setDailyOrders(String(baseline.daily_orders));
        if (baseline.batch_jobs != null) setBatchJobs(String(baseline.batch_jobs));
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleParse = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await parseSow(file);
      setSowBaseline(result);
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : 'SOW parse failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleSave = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = {
        daily_dfu: dailyDfu ? Number(dailyDfu) : undefined,
        daily_sku: dailySku ? Number(dailySku) : undefined,
        daily_orders: dailyOrders ? Number(dailyOrders) : undefined,
        batch_jobs: batchJobs ? Number(batchJobs) : undefined,
      };
      const result = await saveSowBaseline(payload);
      setSowBaseline(result);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Saving SOW baseline failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleCompare = async () => {
    setBusy(true);
    setError(null);
    try {
      const actuals: Record<string, number> = {};
      if (data.batch?.kpis) {
        const kpis = data.batch.kpis as { total_jobs?: number };
        if (kpis.total_jobs != null) actuals.batch_jobs = kpis.total_jobs;
      }
      const result = await compareSow({ actuals });
      setSowCompare(result);
    } catch (compareError) {
      setError(compareError instanceof Error ? compareError.message : 'SOW comparison failed.');
    } finally {
      setBusy(false);
    }
  };

  const metrics = ((data.sowCompare?.metrics as SowMetric[]) || []);

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">SOW Contract &amp; Volume Compliance</Typography>
      <Box className={classes.row}>
        <input className={classes.input} id="sow-parse-input" type="file" accept=".pdf,.docx" onChange={handleParse} />
        <label htmlFor="sow-parse-input">
          <Button component="span" variant="outlined" disabled={busy}>Parse SOW Document</Button>
        </label>
        {busy && <CircularProgress size={22} aria-label="Processing" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      <Box className={classes.fields}>
        <TextField id="sow-daily-dfu" size="small" label="Daily DFU" value={dailyDfu} onChange={(e) => setDailyDfu(e.target.value)} />
        <TextField id="sow-daily-sku" size="small" label="Daily SKU" value={dailySku} onChange={(e) => setDailySku(e.target.value)} />
        <TextField id="sow-daily-orders" size="small" label="Daily orders" value={dailyOrders} onChange={(e) => setDailyOrders(e.target.value)} />
        <TextField id="sow-batch-jobs" size="small" label="Batch jobs" value={batchJobs} onChange={(e) => setBatchJobs(e.target.value)} />
      </Box>
      <Box className={classes.row}>
        <Button variant="contained" color="primary" onClick={handleSave} disabled={busy}>Save Baseline</Button>
        <Button variant="outlined" onClick={handleCompare} disabled={busy}>Compare Against Actuals</Button>
      </Box>

      {data.sowCompare && (
        <>
          <Typography variant="subtitle2" style={{ marginTop: 16 }}>
            Overall status: {String(data.sowCompare.overall_status)}
          </Typography>
          {metrics.length > 0 && (
            <Table size="small" className="pe-table" aria-label="SOW comparison table" style={{ marginTop: 8 }}>
              <TableHead>
                <TableRow>
                  <TableCell>Metric</TableCell>
                  <TableCell align="right">Contracted</TableCell>
                  <TableCell align="right">Actual</TableCell>
                  <TableCell align="right">% of contract</TableCell>
                  <TableCell>Status</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {metrics.map((metric) => (
                  <TableRow key={metric.key}>
                    <TableCell>{metric.label}</TableCell>
                    <TableCell align="right">{metric.sow}</TableCell>
                    <TableCell align="right">{metric.actual}</TableCell>
                    <TableCell align="right">{metric.pct.toFixed(1)}%</TableCell>
                    <TableCell>{metric.status}</TableCell>
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
