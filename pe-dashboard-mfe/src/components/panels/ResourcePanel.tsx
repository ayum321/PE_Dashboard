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
} from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';

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

  React.useEffect(() => {
    getAzureAuthStatus()
      .then(setAzureAuth)
      .catch(() => setAzureAuth(null));
    getAzureStatus().catch(() => undefined);
  }, []);

  const servers = data.resource?.servers || [];
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
    <Paper className={classes.panel} elevation={0}>
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
          <Box className={classes.controls}>
            <TextField
              size="small"
              label="Filter by host"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
            />
          </Box>
          <Table size="small" aria-label="Resource review table">
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
