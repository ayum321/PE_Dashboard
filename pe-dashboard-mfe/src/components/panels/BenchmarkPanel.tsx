import React, { ChangeEvent, useMemo, useState } from 'react';
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
  TableSortLabel,
  TextField,
  Typography,
  makeStyles,
} from '@material-ui/core';
import { uploadBenchmark } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';

interface BenchmarkRow {
  transaction: string;
  baseline_sec: number;
  current_sec: number;
  delta_pct: number;
  status: string;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2), flexWrap: 'wrap' },
  kpiRow: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2) },
  kpi: { padding: theme.spacing(1.5), minWidth: 120 },
  input: { display: 'none' },
  empty: { marginTop: theme.spacing(2) },
}));

type SortKey = 'transaction' | 'delta_pct';

export function BenchmarkPanel() {
  const classes = useStyles();
  const { data, setBenchmark } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('delta_pct');
  const [sortDesc, setSortDesc] = useState(true);

  const handleUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await uploadBenchmark(file);
      setBenchmark(result);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Benchmark upload failed.');
    } finally {
      setBusy(false);
    }
  };

  const filtered = useMemo(() => {
    const rows = (data.benchmark?.rows as BenchmarkRow[]) || [];
    return rows.filter((row) => row.transaction.toLowerCase().includes(filter.toLowerCase()));
  }, [data.benchmark, filter]);
  const sorted = useMemo(() => {
    const copy = [...filtered];
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : Number(av) - Number(bv);
      return sortDesc ? -cmp : cmp;
    });
    return copy;
  }, [filtered, sortKey, sortDesc]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDesc(!sortDesc);
    } else {
      setSortKey(key);
      setSortDesc(true);
    }
  };

  return (
    <Paper className={classes.panel} elevation={0}>
      <Typography variant="h6">Performance Benchmark</Typography>
      <Box className={classes.row}>
        <input className={classes.input} id="benchmark-input" type="file" accept=".csv,.xlsx,.xls" onChange={handleUpload} />
        <label htmlFor="benchmark-input">
          <Button component="span" variant="contained" color="primary" disabled={busy}>
            Upload Benchmark File
          </Button>
        </label>
        {busy && <CircularProgress size={22} aria-label="Uploading" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      {!data.benchmark ? (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload a benchmark comparison file to populate this view.
        </Typography>
      ) : (
        <>
          <Box className={classes.kpiRow}>
            <Paper className={classes.kpi} elevation={0} variant="outlined">
              <Typography variant="caption">Transactions</Typography>
              <Typography variant="h6">{Number(data.benchmark.total_transactions) || 0}</Typography>
            </Paper>
            <Paper className={classes.kpi} elevation={0} variant="outlined">
              <Typography variant="caption">Degraded</Typography>
              <Typography variant="h6">{Number(data.benchmark.degraded) || 0}</Typography>
            </Paper>
            <Paper className={classes.kpi} elevation={0} variant="outlined">
              <Typography variant="caption">Improved</Typography>
              <Typography variant="h6">{Number(data.benchmark.improved) || 0}</Typography>
            </Paper>
            <Paper className={classes.kpi} elevation={0} variant="outlined">
              <Typography variant="caption">SLA breaches</Typography>
              <Typography variant="h6">{Number(data.benchmark.sla_breaches) || 0}</Typography>
            </Paper>
          </Box>
          <Box className={classes.row}>
            <TextField size="small" label="Filter transaction" value={filter} onChange={(event) => setFilter(event.target.value)} />
          </Box>
          <Table size="small" aria-label="Benchmark table" style={{ marginTop: 16 }}>
            <TableHead>
              <TableRow>
                <TableCell>
                  <TableSortLabel active={sortKey === 'transaction'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('transaction')}>
                    Transaction
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">Baseline (s)</TableCell>
                <TableCell align="right">Current (s)</TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'delta_pct'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('delta_pct')}>
                    Delta %
                  </TableSortLabel>
                </TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sorted.slice(0, 50).map((row, index) => (
                <TableRow key={`${row.transaction}-${index}`}>
                  <TableCell>{row.transaction}</TableCell>
                  <TableCell align="right">{row.baseline_sec.toFixed(2)}</TableCell>
                  <TableCell align="right">{row.current_sec.toFixed(2)}</TableCell>
                  <TableCell align="right">{row.delta_pct.toFixed(1)}</TableCell>
                  <TableCell>{row.status}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </Paper>
  );
}
