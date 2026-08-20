/*
 * ===============================================================================================================
 *                                Copyright 2021, Blue Yonder Group, Inc.
 *                                           All Rights Reserved
 *
 *                               THIS IS UNPUBLISHED PROPRIETARY SOURCE CODE OF
 *                                          BLUE YONDER GROUP, INC.
 *
 *
 *                         The copyright notice above does not evidence any actual
 *                                 or intended publication of such source code.
 *
 * ===============================================================================================================
 */

import React, { ChangeEvent, useEffect, useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  makeStyles,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Theme,
  Typography,
} from '@material-ui/core';
import { CentralZone, EastZone, LayoutWrapper, NorthZone, WestZone } from '@jda/lui-dashboard-scaffolding-layouts';
import { LuiLogoStacked } from '@jda/lui-common-component-library';
import {
  AuditContext,
  exportReport,
  fetchAzureResources,
  generateFindings,
  getAzureStatus,
  getExecutiveDashboard,
  getRedFlags,
  SmartUploadResponse,
  uploadDashboardFile,
} from '../../api/dashboardApi';

const useStyles = makeStyles((theme: Theme) => {
  return {
    welcomeContainer: {
      paddingLeft: theme.spacing(1),
      paddingRight: theme.spacing(1),
    },
    welcomePaper: {
      width: '100%',
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      alignItems: 'center',
      paddingTop: theme.spacing(10),
      paddingBottom: theme.spacing(10),
      height: `calc(100vh - ${theme.spacing(50.75)}px)`,
      marginBottom: theme.spacing(1),
      overflow: 'hidden',
    },
    logoContainer: {
      width: theme.spacing(60),
      display: 'flex',
      flexDirection: 'column',
      justifyContent: 'center',
      alignItems: 'center',
    },
    welcomeMessageTitle: {
      paddingBottom: theme.spacing(5),
    },
    workspace: {
      width: 'min(100%, 760px)',
      padding: theme.spacing(4),
    },
    uploadRow: {
      display: 'flex',
      alignItems: 'center',
      gap: theme.spacing(2),
      flexWrap: 'wrap',
      marginTop: theme.spacing(3),
    },
    fileInput: {
      display: 'none',
    },
    status: {
      marginTop: theme.spacing(2),
      minHeight: theme.spacing(3),
    },
    error: {
      color: theme.palette.error.main,
    },
    summary: {
      marginTop: theme.spacing(3),
      padding: theme.spacing(2),
      borderLeft: `3px solid ${theme.palette.primary.main}`,
    },
    tableContainer: {
      marginTop: theme.spacing(3),
      overflowX: 'auto',
    },
    metricRow: {
      display: 'flex',
      gap: theme.spacing(1),
      flexWrap: 'wrap',
      marginTop: theme.spacing(2),
    },
    metric: {
      minWidth: 110,
      padding: theme.spacing(1.5),
      border: `1px solid ${theme.palette.divider}`,
    },
    panel: {
      marginTop: theme.spacing(3),
      padding: theme.spacing(2),
      border: `1px solid ${theme.palette.divider}`,
    },
    paperWestZone: {
      height: `calc(100vh - ${theme.spacing(30.75)}px)`,
    },
    paperEastZone: {
      height: `calc(100vh - ${theme.spacing(30.75)}px)`,
    },
    paperNorthZone: {
      height: `${theme.spacing(12.5)}px`,
    },
  };
});

export function Welcome() {
  const { LOCAL_APP_NAME = 'PE Audit Dashboard' } = window.env;
  const classes = useStyles();
  const [context, setContext] = useState<AuditContext | null>(null);
  const [upload, setUpload] = useState<SmartUploadResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downstream, setDownstream] = useState<Record<string, unknown>>({});
  const [azure, setAzure] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    getAuditContext()
      .then(setContext)
      .catch(() => setContext(null));
    getAzureStatus()
      .then(setAzure)
      .catch(() => setAzure(null));
  }, []);

  const handleFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) {
      return;
    }

    setError(null);
    setUpload(null);
    setIsLoading(true);
    try {
      const result = await uploadDashboardFile(file);
      if (result.data.error) {
        throw new Error(result.data.error);
      }
      setUpload(result);
      setContext(await getAuditContext());
      if (result.classification.type === 'batch') {
        const payload = result.data as Record<string, unknown>;
        const responses = await Promise.allSettled([
          generateFindings(payload),
          getRedFlags(payload),
          getExecutiveDashboard(payload),
        ]);
        const next: Record<string, unknown> = {};
        responses.forEach((response, index) => {
          if (response.status === 'fulfilled') {
            next[['findings', 'redFlags', 'executive'][index]] = response.value;
          }
        });
        setDownstream(next);
      }
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Upload failed.');
    } finally {
      setIsLoading(false);
    }
  };

  const resourceServers = upload?.classification.type === 'resource' ? upload.data.servers || [] : [];
  const isSlaUpload = upload?.classification.type === 'sla_matrix';
  const slaBreaches = isSlaUpload ? upload.data.breaches || [] : [];
  const batchKpis = upload?.classification.type === 'batch' ? (upload.data.kpis as Record<string, unknown> | undefined) : undefined;
  const findings = (downstream.findings as Record<string, unknown> | undefined)?.findings as unknown[] | undefined;
  const redFlags = (downstream.redFlags as Record<string, unknown> | undefined)?.flags as unknown[] | undefined;
  const executive = downstream.executive as Record<string, unknown> | undefined;

  return (
    <div className={classes.welcomeContainer}>
      <LayoutWrapper>
        {/* 
        // @ts-ignore */}
        <NorthZone title={LOCAL_APP_NAME} stickyData="Sticky data hidden until scroll" isHidden={false} isSticky={true}>
          <Paper className={classes.paperNorthZone}></Paper>
        </NorthZone>
        <WestZone isHidden={false} isCollapsed={false} isSticky={false}>
          <Paper className={classes.paperWestZone}></Paper>
        </WestZone>
        <CentralZone>
          <Paper className={classes.welcomePaper} component="div">
            <Box className={classes.workspace} component="section">
              <Box className={classes.logoContainer} component="div">
                <LuiLogoStacked style={{ height: 180, width: 180 }} />
              </Box>
              <Typography variant="h4" component="h1" className={classes.welcomeMessageTitle}>
                {LOCAL_APP_NAME}
              </Typography>
              <Typography variant="body1">
                Upload a PE document to begin analysis. Processing and business rules remain in the FastAPI backend.
              </Typography>
              <Box className={classes.uploadRow}>
                <input
                  accept=".csv,.doc,.docx,.pdf,.txt,.xls,.xlsx,.zip"
                  className={classes.fileInput}
                  id="dashboard-file-upload"
                  type="file"
                  onChange={handleFileChange}
                />
                <label htmlFor="dashboard-file-upload">
                  <Button component="span" variant="contained" color="primary" disabled={isLoading}>
                    Select PE document
                  </Button>
                </label>
                {isLoading && <CircularProgress size={24} aria-label="Uploading document" />}
              </Box>
              <Typography className={`${classes.status} ${error ? classes.error : ''}`} role={error ? 'alert' : undefined}>
                {error || (upload ? `${upload.filename} classified as ${upload.classification.type}.` : 'No document uploaded in this session.')}
              </Typography>
              {upload?.data.ai_summary && (
                <Paper className={classes.summary} elevation={0}>
                  <Typography variant="subtitle2">Backend summary</Typography>
                  <Typography variant="body2">{upload.data.ai_summary}</Typography>
                </Paper>
              )}
              {upload?.classification.type === 'resource' && resourceServers.length > 0 && (
                <Box className={classes.tableContainer} component="section">
                  <Typography variant="subtitle2">Resource health</Typography>
                  <Table size="small" aria-label="Resource health results">
                    <TableHead>
                      <TableRow>
                        <TableCell>Host</TableCell>
                        <TableCell>Type</TableCell>
                        <TableCell align="right">CPU %</TableCell>
                        <TableCell align="right">Memory %</TableCell>
                        <TableCell align="right">Disk %</TableCell>
                        <TableCell align="right">Health</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {resourceServers.map((server) => (
                        <TableRow key={`${server.host}-${server.type || 'APP'}`}>
                          <TableCell>{server.host}</TableCell>
                          <TableCell>{server.type || 'APP'}</TableCell>
                          <TableCell align="right">{(server.cpu_used || 0).toFixed(1)}</TableCell>
                          <TableCell align="right">{(server.mem_used || 0).toFixed(1)}</TableCell>
                          <TableCell align="right">{(server.disk_used_max || 0).toFixed(1)}</TableCell>
                          <TableCell align="right">{(server.health_score || 0).toFixed(1)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </Box>
              )}
              {upload?.classification.type === 'resource' && resourceServers.length === 0 && (
                <Typography variant="body2" color="textSecondary">
                  The resource upload completed without server metric rows.
                </Typography>
              )}
              {isSlaUpload && (
                <Box className={classes.tableContainer} component="section">
                  <Typography variant="subtitle2">SLA matrix</Typography>
                  <Box className={classes.metricRow}>
                    <Paper className={classes.metric} elevation={0}>
                      <Typography variant="caption">Compliance</Typography>
                      <Typography variant="h6">{(upload.data.compliance_pct || 0).toFixed(1)}%</Typography>
                    </Paper>
                    <Paper className={classes.metric} elevation={0}>
                      <Typography variant="caption">Runs</Typography>
                      <Typography variant="h6">{upload.data.total_runs || 0}</Typography>
                    </Paper>
                    <Paper className={classes.metric} elevation={0}>
                      <Typography variant="caption">Breaches</Typography>
                      <Typography variant="h6">{upload.data.breaching_runs || 0}</Typography>
                    </Paper>
                    <Paper className={classes.metric} elevation={0}>
                      <Typography variant="caption">At risk</Typography>
                      <Typography variant="h6">{upload.data.at_risk_runs || 0}</Typography>
                    </Paper>
                  </Box>
                  {upload.data.worst_job && (
                    <Typography variant="body2" className={classes.status}>
                      Worst job: {upload.data.worst_job} ({(upload.data.worst_hrs || 0).toFixed(2)} hours)
                    </Typography>
                  )}
                  {slaBreaches.length > 0 && (
                    <Table size="small" aria-label="SLA breach results">
                      <TableHead>
                        <TableRow>
                          <TableCell>Job</TableCell>
                          <TableCell>Status</TableCell>
                          <TableCell align="right">Runtime hours</TableCell>
                          <TableCell align="right">Over SLA hours</TableCell>
                        </TableRow>
                      </TableHead>
                      <TableBody>
                        {slaBreaches.slice(0, 10).map((breach, index) => (
                          <TableRow key={`${breach.job_name || breach.job || 'job'}-${index}`}>
                            <TableCell>{breach.job_name || breach.job || 'Unnamed job'}</TableCell>
                            <TableCell>{breach.status || 'BREACH'}</TableCell>
                            <TableCell align="right">{(breach.run_hrs || 0).toFixed(2)}</TableCell>
                            <TableCell align="right">{(breach.breach_margin_hrs || 0).toFixed(2)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </Box>
              )}
              {upload?.classification.type === 'batch' && (
                <Box className={classes.panel} component="section">
                  <Typography variant="subtitle2">Batch analysis</Typography>
                  <Box className={classes.metricRow}>
                    {['total_runs', 'total_jobs', 'breaching_runs', 'compliance_pct'].map((key) => (
                      <Paper className={classes.metric} elevation={0} key={key}>
                        <Typography variant="caption">{key.replace(/_/g, ' ')}</Typography>
                        <Typography variant="h6">{String(batchKpis?.[key] ?? 'Not available')}</Typography>
                      </Paper>
                    ))}
                  </Box>
                  <Typography variant="body2" className={classes.status}>
                    Executive, findings, and red-flag calculations use the same backend batch payload.
                  </Typography>
                </Box>
              )}
              {(executive || findings || redFlags) && (
                <Box className={classes.panel} component="section">
                  <Typography variant="subtitle2">Analysis results</Typography>
                  {executive && <Typography variant="body2">Executive dashboard response received from the backend.</Typography>}
                  {findings && <Typography variant="body2">Findings: {findings.length} returned.</Typography>}
                  {redFlags && <Typography variant="body2">Red flags: {redFlags.length} returned.</Typography>}
                </Box>
              )}
              <Box className={classes.panel} component="section">
                <Typography variant="subtitle2">SOW, benchmark, Azure, and export</Typography>
                <Typography variant="body2">
                  Upload SOW or benchmark files through the same control to populate their backend results.
                </Typography>
                <Typography variant="body2" color="textSecondary">
                  Azure connection: {azure ? (azure.configured ? 'configured' : 'not configured') : 'status unavailable'}
                </Typography>
                <Button
                  variant="outlined"
                  disabled={!upload}
                  onClick={async () => {
                    if (!upload) return;
                    try {
                      const blob = await exportReport({ upload, resource: upload.data, batch: upload.data });
                      const url = URL.createObjectURL(blob);
                      const link = document.createElement('a');
                      link.href = url;
                      link.download = 'pe-audit-report.html';
                      link.click();
                      URL.revokeObjectURL(url);
                    } catch (exportError) {
                      setError(exportError instanceof Error ? exportError.message : 'Report export failed.');
                    }
                  }}
                >
                  Export report
                </Button>
              </Box>
              {context && (
                <Typography variant="body2" color="textSecondary">
                  Audit context completeness: {context.completeness_pct}%
                </Typography>
              )}
            </Box>
          </Paper>
        </CentralZone>
        <EastZone isHidden={false} isCollapsed={false} isSticky={false}>
          <Paper className={classes.paperEastZone}></Paper>
        </EastZone>
      </LayoutWrapper>
    </div>
  );
}
