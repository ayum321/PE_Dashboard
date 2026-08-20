import React, { useMemo, useState } from 'react';
import {
  Box,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  TableSortLabel,
  Typography,
  makeStyles,
} from '@material-ui/core';
import Highcharts from 'highcharts';
import HighchartsReact from 'highcharts-react-official';
import { useAppData } from '../../context/AppDataContext';

interface BatchKpis {
  compliance_pct?: number;
  total_runs?: number;
  total_jobs?: number;
  total_hrs?: number;
  jobs_breach?: number;
  jobs_at_risk?: number;
  jobs_ok?: number;
  failed_runs?: number;
  fail_rate_pct?: number;
}

interface TopJobRow {
  Job_Name: string;
  peak_hrs: number;
  avg_hrs: number;
  total_hrs: number;
  buffer_pct?: number | null;
  buffer_status: string;
}

interface WindowPoint {
  run_date: string;
  total_hrs: number;
  job_count: number;
  breach: boolean;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  kpiRow: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2) },
  kpi: { padding: theme.spacing(1.5), minWidth: 130 },
  chart: { marginTop: theme.spacing(3) },
  empty: { marginTop: theme.spacing(2) },
}));

type SortKey = 'Job_Name' | 'peak_hrs' | 'avg_hrs' | 'total_hrs';

export function BatchPanel() {
  const classes = useStyles();
  const { data } = useAppData();
  const [sortKey, setSortKey] = useState<SortKey>('peak_hrs');
  const [sortDesc, setSortDesc] = useState(true);

  const kpis = (data.batch?.kpis || {}) as BatchKpis;
  const topJobs = ((data.batch?.top_jobs as TopJobRow[]) || []).slice();
  const window = ((data.batch?.window as WindowPoint[]) || []).slice();

  const sortedJobs = useMemo(() => {
    const rows = [...topJobs];
    rows.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : Number(av) - Number(bv);
      return sortDesc ? -cmp : cmp;
    });
    return rows;
  }, [topJobs, sortKey, sortDesc]);

  const chartOptions: Highcharts.Options = {
    chart: { type: 'column', height: 260 },
    title: { text: undefined },
    xAxis: { categories: window.map((point) => point.run_date) },
    yAxis: { title: { text: 'Total hours' } },
    series: [
      {
        type: 'column',
        name: 'Daily batch hours',
        data: window.map((point) => point.total_hrs),
        color: '#3b82f6',
      },
    ],
    credits: { enabled: false },
  };

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDesc(!sortDesc);
    } else {
      setSortKey(key);
      setSortDesc(true);
    }
  };

  if (!data.batch) {
    return (
      <Paper className={`${classes.panel} kpi-card`} elevation={0}>
        <Typography variant="h6">Batch Review</Typography>
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload a Ctrl-M batch export in Upload &amp; Intake to populate this view.
        </Typography>
      </Paper>
    );
  }

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">Batch Review</Typography>
      <Box className={classes.kpiRow}>
        <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
          <Typography variant="caption">Compliance</Typography>
          <Typography variant="h6">{(kpis.compliance_pct || 0).toFixed(1)}%</Typography>
        </Paper>
        <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
          <Typography variant="caption">Total runs</Typography>
          <Typography variant="h6">{kpis.total_runs || 0}</Typography>
        </Paper>
        <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
          <Typography variant="caption">Total jobs</Typography>
          <Typography variant="h6">{kpis.total_jobs || 0}</Typography>
        </Paper>
        <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
          <Typography variant="caption">Breaching</Typography>
          <Typography variant="h6" style={{ color: '#f43f5e' }}>{kpis.jobs_breach || 0}</Typography>
        </Paper>
        <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
          <Typography variant="caption">At risk</Typography>
          <Typography variant="h6" style={{ color: '#f59e0b' }}>{kpis.jobs_at_risk || 0}</Typography>
        </Paper>
        <Paper className={`${classes.kpi} kpi-card`} elevation={0}>
          <Typography variant="caption">Failed runs</Typography>
          <Typography variant="h6" style={{ color: '#f43f5e' }}>{kpis.failed_runs || 0}</Typography>
        </Paper>
      </Box>

      {window.length > 0 && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">Daily batch window</Typography>
          <HighchartsReact highcharts={Highcharts} options={chartOptions} />
        </Box>
      )}

      {sortedJobs.length > 0 && (
        <Box className={classes.chart}>
          <Typography variant="subtitle2">Top jobs</Typography>
          <Table size="small" className="pe-table" aria-label="Top jobs table">
            <TableHead>
              <TableRow>
                <TableCell>
                  <TableSortLabel active={sortKey === 'Job_Name'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('Job_Name')}>
                    Job
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'peak_hrs'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('peak_hrs')}>
                    Peak hours
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'avg_hrs'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('avg_hrs')}>
                    Avg hours
                  </TableSortLabel>
                </TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sortedJobs.slice(0, 25).map((job) => (
                <TableRow key={job.Job_Name}>
                  <TableCell>{job.Job_Name}</TableCell>
                  <TableCell align="right">{job.peak_hrs.toFixed(2)}</TableCell>
                  <TableCell align="right">{job.avg_hrs.toFixed(2)}</TableCell>
                  <TableCell>{job.buffer_status}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      )}
    </Paper>
  );
}
