import React, { useEffect, useState } from 'react';
import {
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
import { getApiBaseUrl, getReportArchive } from '../../api/dashboardApi';

interface ArchiveRow {
  customer_slug: string;
  customer: string;
  generated_at: string;
  env?: string;
  pe_approved?: boolean;
  cust_approved?: boolean;
  checklist_mismatches?: number;
  sla_breach_count?: number;
  sla_at_risk_count?: number;
  sla_total_jobs?: number;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  empty: { marginTop: theme.spacing(2) },
}));

export function ArchivePanel() {
  const classes = useStyles();
  const [reports, setReports] = useState<ArchiveRow[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getReportArchive()
      .then((result) => setReports((result.reports as ArchiveRow[]) || []))
      .catch((fetchError) => setError(fetchError instanceof Error ? fetchError.message : 'Failed to load report archive.'))
      .finally(() => setBusy(false));
  }, []);

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">Review Registry</Typography>
      {busy && <CircularProgress size={22} aria-label="Loading archive" style={{ marginTop: 16 }} />}
      {error && <Typography variant="body2" color="error">{error}</Typography>}
      {!busy && reports.length === 0 && (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          No reports have been generated and archived yet.
        </Typography>
      )}
      {reports.length > 0 && (
        <Table size="small" className="pe-table" aria-label="Report archive table" style={{ marginTop: 16 }}>
          <TableHead>
            <TableRow>
              <TableCell>Customer</TableCell>
              <TableCell>Generated</TableCell>
              <TableCell>Environment</TableCell>
              <TableCell align="right">Breaches</TableCell>
              <TableCell align="right">At risk</TableCell>
              <TableCell>Sign-off</TableCell>
              <TableCell>Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {reports.map((report) => (
              <TableRow key={report.customer_slug}>
                <TableCell>{report.customer}</TableCell>
                <TableCell>{report.generated_at}</TableCell>
                <TableCell>{report.env || '-'}</TableCell>
                <TableCell align="right">{report.sla_breach_count ?? '-'}</TableCell>
                <TableCell align="right">{report.sla_at_risk_count ?? '-'}</TableCell>
                <TableCell>
                  <span className={`metric-badge ${report.pe_approved ? 'metric-badge-green' : 'metric-badge-blue'}`}>
                    PE {report.pe_approved ? '✓' : '—'}
                  </span>{' '}
                  <span className={`metric-badge ${report.cust_approved ? 'metric-badge-green' : 'metric-badge-blue'}`}>
                    Cust {report.cust_approved ? '✓' : '—'}
                  </span>
                  {!!report.checklist_mismatches && (
                    <span className="metric-badge metric-badge-amber" style={{ marginLeft: 4 }}>
                      {report.checklist_mismatches} mismatch(es)
                    </span>
                  )}
                </TableCell>
                <TableCell>
                  <Button
                    size="small"
                    href={`${getApiBaseUrl()}/api/report-archive/${report.customer_slug}`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    View
                  </Button>
                  <Button
                    size="small"
                    href={`${getApiBaseUrl()}/api/report-archive/${report.customer_slug}/download`}
                  >
                    Download
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Paper>
  );
}
