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
  sla_sec?: number | null;
  sla_breach?: boolean;
}

interface BenchmarkCategory {
  name: string;
  total: number;
  passed: number;
  failed: number;
  degraded: number;
  avg_delta: number;
}

const STATUS_BADGE: Record<string, string> = {
  OK: 'metric-badge-green',
  WATCH: 'metric-badge-amber',
  BREACH: 'metric-badge-red',
  REFERENCE: 'metric-badge-blue',
};

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
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
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
            <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
              <Typography variant="caption">Transactions</Typography>
              <Typography variant="h6">{Number(data.benchmark.total_transactions) || 0}</Typography>
            </Paper>
            <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
              <Typography variant="caption">Degraded</Typography>
              <Typography variant="h6" style={{ color: '#f43f5e' }}>{Number(data.benchmark.degraded) || 0}</Typography>
            </Paper>
            <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
              <Typography variant="caption">Improved</Typography>
              <Typography variant="h6" style={{ color: '#10d96e' }}>{Number(data.benchmark.improved) || 0}</Typography>
            </Paper>
            <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
              <Typography variant="caption">SLA breaches</Typography>
              <Typography variant="h6" style={{ color: '#f43f5e' }}>{Number(data.benchmark.sla_breaches) || 0}</Typography>
            </Paper>
          </Box>
          {typeof data.benchmark.summary === 'string' && data.benchmark.summary && (
            <Typography variant="body2" style={{ marginTop: 12, color: '#f0f4ff' }}>{data.benchmark.summary}</Typography>
          )}
          {((data.benchmark.categories as BenchmarkCategory[]) || []).length > 0 && (
            <Box style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
              {(data.benchmark.categories as BenchmarkCategory[]).map((category) => (
                <span key={category.name} className="metric-badge metric-badge-purple">
                  {category.name}: {category.passed}/{category.total} passed · {category.avg_delta.toFixed(1)}% avg
                </span>
              ))}
            </Box>
          )}
          <Box className={classes.row}>
            <TextField size="small" label="Filter transaction" value={filter} onChange={(event) => setFilter(event.target.value)} />
          </Box>
          <Table size="small" className="pe-table" aria-label="Benchmark table" style={{ marginTop: 16 }}>
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
                <TableCell align="right">SLA (s)</TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sorted.slice(0, 50).map((row, index) => (
                <TableRow key={`${row.transaction}-${index}`}>
                  <TableCell>{row.transaction}</TableCell>
                  <TableCell align="right">{row.baseline_sec.toFixed(2)}</TableCell>
                  <TableCell align="right">{row.current_sec.toFixed(2)}</TableCell>
                  <TableCell align="right" style={{ color: row.delta_pct > 0 ? '#f43f5e' : '#10d96e' }}>{row.delta_pct.toFixed(1)}</TableCell>
                  <TableCell align="right">{row.sla_sec != null ? row.sla_sec.toFixed(2) : '—'}</TableCell>
                  <TableCell>
                    <span className={`metric-badge ${STATUS_BADGE[row.status] || 'metric-badge-blue'}`}>{row.status}</span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </Paper>
  );
}
