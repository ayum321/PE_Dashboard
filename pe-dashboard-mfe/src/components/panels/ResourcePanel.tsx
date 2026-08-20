import React, { useMemo, useState } from 'react';
import {
  Box,
  Button,
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
import {
  connectAzure,
  discoverAzureVms,
  disconnectAzure,
  getAzureAuthStatus,
  getAzureResourceGroups,
  getAzureStatus,
  getAzureSubscriptions,
  processResource,
} from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { KpiStatCard } from '../shared/KpiStatCard';

interface FleetKpis {
  fleet_grade?: string;
  fleet_score?: number;
  avg_cpu?: number;
  avg_mem?: number;
  avg_disk?: number;
  n_critical?: number;
  n_warning?: number;
  n_healthy?: number;
}

interface ResourceAnomaly {
  host: string;
  metric: string;
  value: number;
  z: number;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  controls: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2), flexWrap: 'wrap' },
  azureRow: { display: 'flex', gap: theme.spacing(1), alignItems: 'center', marginTop: theme.spacing(2), flexWrap: 'wrap' },
  empty: { marginTop: theme.spacing(2) },
}));

type SortKey = 'host' | 'cpu_used' | 'mem_used' | 'disk_used_max' | 'health_score';

export function ResourcePanel() {
  const classes = useStyles();
  const { data } = useAppData();
  const [filter, setFilter] = useState('');
  const [sortKey, setSortKey] = useState<SortKey>('cpu_used');
  const [sortDesc, setSortDesc] = useState(true);
  const [azureAuth, setAzureAuth] = useState<Record<string, unknown> | null>(null);
  const [subscriptions, setSubscriptions] = useState<Record<string, unknown>[]>([]);
  const [groups, setGroups] = useState<Record<string, unknown>[]>([]);
  const [vms, setVms] = useState<Record<string, unknown>[]>([]);
  const [azureBusy, setAzureBusy] = useState(false);
  const [azureError, setAzureError] = useState<string | null>(null);
  const [fleetKpis, setFleetKpis] = useState<FleetKpis | null>(null);
  const [anomalies, setAnomalies] = useState<ResourceAnomaly[]>([]);

  React.useEffect(() => {
    getAzureAuthStatus()
      .then(setAzureAuth)
      .catch(() => setAzureAuth(null));
    getAzureStatus().catch(() => undefined);
  }, []);

  React.useEffect(() => {
    const rows = data.resource?.servers || [];
    if (rows.length === 0) {
      setFleetKpis(null);
      setAnomalies([]);
      return;
    }
    processResource(rows)
      .then((result) => {
        setFleetKpis((result.kpis as FleetKpis) || null);
        setAnomalies((result.anomalies as ResourceAnomaly[]) || []);
      })
      .catch(() => {
        setFleetKpis(null);
        setAnomalies([]);
      });
  }, [data.resource]);

  const servers = data.resource?.servers || [];
  const fleetAvg = useMemo(() => {
    const rows = data.resource?.servers || [];
    const count = rows.length || 1;
    const sum = (key: 'cpu_used' | 'mem_used' | 'disk_used_max' | 'health_score') =>
      rows.reduce((total, server) => total + (server[key] || 0), 0);
    return {
      cpu: sum('cpu_used') / count,
      mem: sum('mem_used') / count,
      disk: sum('disk_used_max') / count,
      health: sum('health_score') / count,
    };
  }, [data.resource]);
  const filtered = useMemo(() => {
    const rows = data.resource?.servers || [];
    return rows.filter((server) => server.host.toLowerCase().includes(filter.toLowerCase()));
  }, [data.resource, filter]);
  const sorted = useMemo(() => {
    const rows = [...filtered];
    rows.sort((a, b) => {
      const av = a[sortKey] ?? 0;
      const bv = b[sortKey] ?? 0;
      const cmp = typeof av === 'string' ? String(av).localeCompare(String(bv)) : Number(av) - Number(bv);
      return sortDesc ? -cmp : cmp;
    });
    return rows;
  }, [filtered, sortKey, sortDesc]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDesc(!sortDesc);
    } else {
      setSortKey(key);
      setSortDesc(true);
    }
  };

  const handleConnect = async () => {
    setAzureBusy(true);
    setAzureError(null);
    try {
      const result = await connectAzure();
      setAzureAuth(result);
      const subs = await getAzureSubscriptions();
      setSubscriptions((subs.subscriptions as Record<string, unknown>[]) || []);
    } catch (error) {
      setAzureError(error instanceof Error ? error.message : 'Azure sign-in failed.');
    } finally {
      setAzureBusy(false);
    }
  };

  const handleDisconnect = async () => {
    setAzureBusy(true);
    try {
      await disconnectAzure();
      setAzureAuth({ method: 'none' });
      setSubscriptions([]);
      setGroups([]);
      setVms([]);
    } finally {
      setAzureBusy(false);
    }
  };

  const handleScope = async (subscriptionId: string) => {
    setAzureBusy(true);
    try {
      const groupResult = await getAzureResourceGroups(subscriptionId);
      setGroups((groupResult.resource_groups as Record<string, unknown>[]) || []);
      const discovered = await discoverAzureVms({ subscription_id: subscriptionId });
      setVms((discovered.vms as Record<string, unknown>[]) || []);
    } catch (error) {
      setAzureError(error instanceof Error ? error.message : 'Azure discovery failed.');
    } finally {
      setAzureBusy(false);
    }
  };

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">Resource Review</Typography>

      <Box className={classes.azureRow}>
        <Typography variant="body2" color="textSecondary">
          Azure: {azureAuth?.method === 'browser' ? `connected as ${azureAuth.display_name || azureAuth.name}` : 'not connected'}
        </Typography>
        {azureAuth?.method === 'browser' ? (
          <Button size="small" variant="outlined" onClick={handleDisconnect} disabled={azureBusy}>Disconnect Azure</Button>
        ) : (
          <Button size="small" variant="outlined" onClick={handleConnect} disabled={azureBusy}>Connect Azure</Button>
        )}
        {subscriptions.length > 0 && (
          <select aria-label="Azure subscription" onChange={(event) => handleScope(event.target.value)} defaultValue="">
            <option value="" disabled>Select subscription</option>
            {subscriptions.map((sub) => (
              <option key={String(sub.id)} value={String(sub.id)}>{String(sub.name || sub.id)}</option>
            ))}
          </select>
        )}
        {groups.length > 0 && <Typography variant="caption">{groups.length} resource groups</Typography>}
        {vms.length > 0 && <Typography variant="caption">{vms.length} VMs discovered</Typography>}
        {azureError && <Typography variant="caption" color="error">{azureError}</Typography>}
      </Box>

      {servers.length === 0 ? (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload a resource report in Upload &amp; Intake, or fetch live Azure metrics, to populate this view.
        </Typography>
      ) : (
        <>
          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 12, marginBottom: 16 }}>
            <KpiStatCard label="Servers" value={servers.length} sub={`${servers.filter((s) => (s.type || 'APP') === 'APP').length} APP · ${servers.filter((s) => s.type === 'DB').length} DB`} accent="#3b82f6" />
            {fleetKpis ? (
              <>
                <KpiStatCard label="Fleet Grade" value={fleetKpis.fleet_grade || '?'} sub={`Score ${(fleetKpis.fleet_score || 0).toFixed(0)}/100`} accent="#a855f7" />
                <KpiStatCard label="Avg CPU" value={`${(fleetKpis.avg_cpu || 0).toFixed(0)}%`} sub="Threshold 80%" accent={(fleetKpis.avg_cpu || 0) >= 80 ? '#f43f5e' : '#10d96e'} />
                <KpiStatCard label="Avg Memory" value={`${(fleetKpis.avg_mem || 0).toFixed(0)}%`} sub="Threshold 80%" accent={(fleetKpis.avg_mem || 0) >= 80 ? '#f43f5e' : '#10d96e'} />
                <KpiStatCard label="Avg Disk" value={`${(fleetKpis.avg_disk || 0).toFixed(0)}%`} sub="Threshold 85%" accent={(fleetKpis.avg_disk || 0) >= 85 ? '#f43f5e' : '#10d96e'} />
                <KpiStatCard label="Health" accent="#f43f5e" value={
                  <span>
                    <span style={{ color: '#f43f5e' }}>{fleetKpis.n_critical || 0}</span>
                    <span style={{ color: '#6b7db3', margin: '0 4px', fontSize: 16 }}>·</span>
                    <span style={{ color: '#f59e0b' }}>{fleetKpis.n_warning || 0}</span>
                    <span style={{ color: '#6b7db3', margin: '0 4px', fontSize: 16 }}>·</span>
                    <span style={{ color: '#10d96e' }}>{fleetKpis.n_healthy || 0}</span>
                  </span>
                } sub="Critical · Warning · Healthy" />
              </>
            ) : (
              <>
                <KpiStatCard label="Avg CPU" value={`${fleetAvg.cpu.toFixed(0)}%`} sub="Threshold 80%" accent={fleetAvg.cpu >= 80 ? '#f43f5e' : '#10d96e'} />
                <KpiStatCard label="Avg Memory" value={`${fleetAvg.mem.toFixed(0)}%`} sub="Threshold 80%" accent={fleetAvg.mem >= 80 ? '#f43f5e' : '#10d96e'} />
                <KpiStatCard label="Avg Disk" value={`${fleetAvg.disk.toFixed(0)}%`} sub="Threshold 85%" accent={fleetAvg.disk >= 85 ? '#f43f5e' : '#10d96e'} />
                <KpiStatCard label="Fleet Health" value={fleetAvg.health.toFixed(0)} sub="Score /100" accent="#a855f7" />
              </>
            )}
          </Box>

          {anomalies.length > 0 && (
            <Box
              style={{ borderRadius: 12, border: '1px solid rgba(245,158,11,.3)', background: 'rgba(245,158,11,.05)', padding: 16, marginBottom: 16 }}
            >
              <Typography variant="subtitle2" style={{ color: '#f59e0b' }}>🎯 Anomaly Spotlight</Typography>
              <Typography variant="caption" color="textSecondary">Servers whose metrics deviate significantly (|z| ≥ 2.0) from the fleet.</Typography>
              <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8, marginTop: 8 }}>
                {anomalies.slice(0, 9).map((anomaly, index) => (
                  <Box key={`${anomaly.host}-${anomaly.metric}-${index}`} className="insight-card warning" style={{ padding: 10 }}>
                    <Typography variant="body2" style={{ fontWeight: 700 }}>{anomaly.host}</Typography>
                    <Typography variant="caption" color="textSecondary">
                      {anomaly.metric}: {anomaly.value.toFixed(1)} (z={anomaly.z.toFixed(2)})
                    </Typography>
                  </Box>
                ))}
              </Box>
            </Box>
          )}

          <Box className={classes.controls}>
            <TextField
              size="small"
              label="Filter by host"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            />
          </Box>
          <Table size="small" className="pe-table" aria-label="Resource review table">
            <TableHead>
              <TableRow>
                <TableCell>
                  <TableSortLabel active={sortKey === 'host'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('host')}>
                    Host
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'cpu_used'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('cpu_used')}>
                    CPU %
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'mem_used'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('mem_used')}>
                    Memory %
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'disk_used_max'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('disk_used_max')}>
                    Disk %
                  </TableSortLabel>
                </TableCell>
                <TableCell align="right">
                  <TableSortLabel active={sortKey === 'health_score'} direction={sortDesc ? 'desc' : 'asc'} onClick={() => handleSort('health_score')}>
                    Health
                  </TableSortLabel>
                </TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sorted.map((server) => (
                <TableRow key={`${server.host}-${server.type || 'APP'}`}>
                  <TableCell>{server.host}</TableCell>
                  <TableCell align="right">{(server.cpu_used || 0).toFixed(1)}</TableCell>
                  <TableCell align="right">{(server.mem_used || 0).toFixed(1)}</TableCell>
                  <TableCell align="right">{(server.disk_used_max || 0).toFixed(1)}</TableCell>
                  <TableCell align="right">{(server.health_score || 0).toFixed(1)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </Paper>
  );
}
