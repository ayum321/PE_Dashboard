import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Box, Button, CircularProgress, Typography } from '@material-ui/core';
import {
  connectAzure,
  DashboardPayload,
  discoverAzureVms,
  disconnectAzure,
  fetchAzureResourcesWithProgress,
  getAzureAuthStatus,
  getAzureResourceGroups,
  getAzureSubscriptions,
  ResourceServer,
  searchAzureVms,
  updateConfig,
} from '../../api/dashboardApi';

export interface AzureVm {
  resource_id: string;
  name: string;
  type: string;
  location?: string;
  vm_size?: string;
  resource_group?: string;
  subscription_id?: string;
  tags?: Record<string, string>;
  product_group?: string;
  customer?: string;
  application?: string;
  environment?: string;
}

interface Props {
  open: boolean;
  /** Start browser sign-in from an explicit parent "Connect Azure" action. */
  autoStartAuth?: boolean;
  onClose: () => void;
  /** Complete only after the parent has stored the resolved fleet evidence.
   * This prevents a reviewer from closing the modal and exporting a transient
   * "servers only" snapshot before the shared severity engine returns. */
  onFetched: (
    servers: ResourceServer[],
    meta: AzureFetchMeta,
    resolved: DashboardPayload,
  ) => void | Promise<void>;
  onAuthChanged?: (auth: DashboardPayload) => void;
}

const TYPE_COLOR: Record<string, string> = { APP: '#10b981', DB: '#3b82f6', SRE: '#f59e0b' };
const ENV_COLOR: Record<string, string> = { PROD: '#f87171', TEST: '#38bdf8', UAT: '#a78bfa', STG: '#fb923c', DEV: '#2dd4bf' };
const UNTAGGED = 'Untagged';

export interface AzureFetchMeta {
  hoursBack: number;
  customer?: string;
  customerStatus: 'identified' | 'untagged' | 'mixed';
  customerMessage: string;
  taggedVmCount: number;
  selectedVmCount: number;
}

/** Ported from _getVmEnv() (app.js): infer environment from tags, then name prefix. */
function getVmEnv(vm: AzureVm): string {
  const tags = vm.tags || {};
  const tagEnv = (tags.Environment || tags.environment || tags.Env || tags.env || tags.Tier || '').toUpperCase();
  const rules: [string, RegExp][] = [['PROD', /PROD/], ['TEST', /TEST|QA/], ['UAT', /UAT/], ['STG', /STG|STAGE/], ['DEV', /DEV/]];
  for (const [env, rx] of rules) {
    if (rx.test(tagEnv)) return env;
  }
  const n = (vm.name || '').toUpperCase();
  if (/^PR[A-Z]{2}\d|PROD[_-]/.test(n)) return 'PROD';
  if (/^TS[A-Z]{2}\d|^TST|TEST[_-]/.test(n)) return 'TEST';
  if (/^UA[A-Z]{2}\d|UAT[_-]/.test(n)) return 'UAT';
  if (/^ST[A-Z]{2}\d|STG[_-]|STAGE[_-]/.test(n)) return 'STG';
  if (/^DV[A-Z]{2}\d|DEV[_-]/.test(n)) return 'DEV';
  return 'PROD';
}

function customerOf(vm: AzureVm): string {
  if (vm.customer?.trim()) return vm.customer.trim();
  const aliases = new Set(['customername', 'customer', 'clientname', 'client']);
  for (const [key, value] of Object.entries(vm.tags || {})) {
    if (aliases.has(key.toLowerCase()) && String(value || '').trim()) return String(value).trim();
  }
  return UNTAGGED;
}

function segBtnStyle(active: boolean, accent?: string): React.CSSProperties {
  return {
    display: 'inline-flex', alignItems: 'center', gap: 5, padding: '4px 10px', borderRadius: 6,
    fontSize: 10.5, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap',
    border: `1px solid ${active ? (accent || '#3b82f6') + '80' : '#213060'}`,
    background: active ? (accent || '#3b82f6') + '1f' : 'transparent',
    color: active ? (accent || '#93c5fd') : '#6b7db3',
  };
}

const chipStyle: React.CSSProperties = { fontSize: 9, opacity: 0.8, marginLeft: 2 };

/** Full "Fetch from Azure Monitor" workflow — search/browse → discover → fleet
 * filters (type/env/region/product group) → customer-grouped VM selection →
 * fetch. Ported from the #azure-fetch-modal two-step dialog (index.html/app.js). */
export function AzureFetchModal({ open, autoStartAuth = false, onClose, onFetched, onAuthChanged }: Props) {
  const [authInfo, setAuthInfo] = useState<DashboardPayload | null>(null);
  const [authBusy, setAuthBusy] = useState(false);
  const [deviceCodeInfo, setDeviceCodeInfo] = useState<{ verification_uri: string; user_code: string; message: string } | null>(null);
  // State updates are asynchronous. A ref closes the small gap in which two
  // clicks can both enter handleSignIn before React re-renders the button.
  const signInInFlight = useRef(false);
  const autoSignInAttempted = useRef(false);
  const modalGeneration = useRef(0);
  const subscriptionLoadGeneration = useRef(0);
  const [step, setStep] = useState<1 | 2>(1);

  const [searchQuery, setSearchQuery] = useState('');
  const [searchBusy, setSearchBusy] = useState(false);
  const [discoverStatus, setDiscoverStatus] = useState<{ text: string; tone: 'muted' | 'amber' | 'red' | 'green' } | null>(null);

  const [subscriptions, setSubscriptions] = useState<{ id: string; name: string }[]>([]);
  const [subscriptionsWarming, setSubscriptionsWarming] = useState(false);
  const [selectedSub, setSelectedSub] = useState('');
  const [groups, setGroups] = useState<{ name: string }[]>([]);
  const [selectedGroup, setSelectedGroup] = useState('');
  const [browseBusy, setBrowseBusy] = useState(false);

  const [discoveredVms, setDiscoveredVms] = useState<AzureVm[]>([]);
  const [selectedVmIds, setSelectedVmIds] = useState<Set<string>>(new Set());
  const [collapsedCustomers, setCollapsedCustomers] = useState<Set<string>>(new Set());
  const [typeFilters, setTypeFilters] = useState<Set<string>>(new Set()); // empty = ALL
  const [envFilter, setEnvFilter] = useState('ALL');
  const [regionFilter, setRegionFilter] = useState('ALL');
  const [pgFilter, setPgFilter] = useState('ALL');
  const [vmSearch, setVmSearch] = useState('');

  const [hoursBack, setHoursBack] = useState(24);
  const [fetchBusy, setFetchBusy] = useState(false);
  const [fetchStatus, setFetchStatus] = useState<string | null>(null);

  const cancelSubscriptionLoads = useCallback(() => {
    subscriptionLoadGeneration.current += 1;
  }, []);

  /**
   * Browser credentials live in the API process and are scoped to its pe_sid
   * cookie.  A restarted local API (or an expired portal session) can make a
   * previously rendered identity stale.  Never leave the modal saying
   * "Signed in" once a protected endpoint has rejected that same session.
   */
  const invalidateAzureSession = useCallback((message: string) => {
    cancelSubscriptionLoads();
    setAuthInfo({ method: 'none' });
    setSubscriptions([]);
    setSubscriptionsWarming(false);
    setSelectedSub('');
    setGroups([]);
    setSelectedGroup('');
    setDiscoverStatus({ text: message, tone: 'red' });
  }, [cancelSubscriptionLoads]);

  const loadSubscriptions = useCallback(async (modalToken: number = modalGeneration.current) => {
    const requestId = ++subscriptionLoadGeneration.current;
    const stillCurrent = () =>
      modalToken === modalGeneration.current && requestId === subscriptionLoadGeneration.current;
    const result = await getAzureSubscriptions();
    if (!stillCurrent()) return false;
    if (result.ok === false) {
      invalidateAzureSession(typeof result.error === 'string'
        ? `${result.error} Your local Azure session may have expired or the API was restarted.`
        : 'Azure session is no longer available. Sign in with Browser again.');
      return false;
    }
    const rows = (result.subscriptions as { id: string; name: string }[]) || [];
    const warming = result._cache_warming === true && rows.length === 0;
    if (!stillCurrent()) return false;
    setSubscriptions(rows);
    setSubscriptionsWarming(warming);
    setDiscoverStatus(warming
      ? { text: 'Azure sign-in succeeded. Loading accessible subscriptions…', tone: 'muted' }
      : null);
    return !warming;
  }, [invalidateAzureSession]);

  useEffect(() => {
    if (!open) {
      modalGeneration.current += 1;
      cancelSubscriptionLoads();
      return;
    }
    const modalToken = ++modalGeneration.current;
    setStep(1);
    setFetchStatus(null);
    setDiscoverStatus(null);
    setSubscriptionsWarming(false);
    getAzureAuthStatus()
      .then((result) => {
        if (modalToken !== modalGeneration.current) return;
        if (result.method !== 'browser') {
          setAuthInfo({ method: 'none' });
          setSubscriptions([]);
          return;
        }
        setAuthInfo(result);
        loadSubscriptions(modalToken).catch(() => {
          if (modalToken === modalGeneration.current) {
            invalidateAzureSession('Azure session could not be verified. Sign in with Browser again.');
          }
        });
      })
      .catch(() => {
        if (modalToken !== modalGeneration.current) return;
        setAuthInfo({ method: 'none' });
        setSubscriptions([]);
      });
  }, [open, loadSubscriptions, invalidateAzureSession, cancelSubscriptionLoads]);

  // The API deliberately warms a large tenant's subscription list in the
  // background. Poll only while it says it is warming, then stop immediately.
  useEffect(() => {
    if (!open || authInfo?.method !== 'browser' || !subscriptionsWarming) return;
    const modalToken = modalGeneration.current;
    const timer = window.setTimeout(() => {
      loadSubscriptions(modalToken).catch(() => {
        if (modalToken === modalGeneration.current) {
          invalidateAzureSession('Azure session could not be verified. Sign in with Browser again.');
        }
      });
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [open, authInfo?.method, subscriptionsWarming, loadSubscriptions, invalidateAzureSession]);

  const handleSignIn = useCallback(async () => {
    if (signInInFlight.current) return;
    const modalToken = modalGeneration.current;
    signInInFlight.current = true;
    setAuthBusy(true);
    setDiscoverStatus({ text: 'Connecting to Azure\u2026', tone: 'muted' });
    try {
      const result = await connectAzure();
      if (modalToken !== modalGeneration.current) return;
      if (result.device_code_required) {
        setDeviceCodeInfo({
          verification_uri: String(result.verification_uri || 'https://microsoft.com/devicelogin'),
          user_code: String(result.user_code || ''),
          message: String(result.message || ''),
        });
        setDiscoverStatus({ text: 'Please complete login on the Microsoft page using the code above.', tone: 'muted' });
        return;
      }
      setDeviceCodeInfo(null);
      setAuthInfo(result);
      onAuthChanged?.(result);
      await loadSubscriptions(modalToken);
    } catch (error) {
      if (modalToken !== modalGeneration.current) return;
      setDiscoverStatus({ text: error instanceof Error ? error.message : 'Azure sign-in failed.', tone: 'red' });
    } finally {
      signInInFlight.current = false;
      setAuthBusy(false);
    }
  }, [loadSubscriptions, onAuthChanged]);

  // "Connect Azure" is already an explicit user gesture. When a parent uses
  // it, do not make the analyst click a second Sign in button in this dialog.
  useEffect(() => {
    if (!open) {
      autoSignInAttempted.current = false;
      return;
    }
    if (autoStartAuth && authInfo?.method === 'none' && !autoSignInAttempted.current) {
      autoSignInAttempted.current = true;
      void handleSignIn();
    }
  }, [open, autoStartAuth, authInfo?.method, handleSignIn]);

  // Poll for Device Code sign-in completion
  useEffect(() => {
    if (!open || !deviceCodeInfo) return;
    const modalToken = modalGeneration.current;
    const interval = window.setInterval(async () => {
      try {
        const status = await getAzureAuthStatus();
        if (modalToken !== modalGeneration.current) return;
        if (status.method === 'browser') {
          setDeviceCodeInfo(null);
          setAuthInfo(status);
          onAuthChanged?.(status);
          await loadSubscriptions(modalToken);
        }
      } catch {}
    }, 2000);
    return () => window.clearInterval(interval);
  }, [open, deviceCodeInfo, loadSubscriptions, onAuthChanged]);

  const handleSignOut = async () => {
    setAuthBusy(true);
    cancelSubscriptionLoads();
    setDeviceCodeInfo(null);
    try {
      await disconnectAzure();
      setAuthInfo({ method: 'none' });
      onAuthChanged?.({ method: 'none' });
      setSubscriptions([]);
      setSubscriptionsWarming(false);
      setDiscoveredVms([]);
    } finally {
      setAuthBusy(false);
    }
  };

  const onDiscovered = (data: DashboardPayload, statusMsg: string) => {
    const raw = (data.vms as AzureVm[]) || [];
    const seen = new Set<string>();
    const deduped: AzureVm[] = [];
    for (const vm of raw) {
      const key = (vm.resource_id || `${vm.name}|${vm.location}`).toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      deduped.push(vm);
    }
    if (!deduped.length) {
      setDiscoverStatus({ text: 'No VMs found.', tone: 'amber' });
      return;
    }
    setDiscoveredVms(deduped);
    setSelectedVmIds(new Set());
    setCollapsedCustomers(new Set());
    setTypeFilters(new Set());
    setEnvFilter('ALL');
    setRegionFilter('ALL');
    setPgFilter('ALL');
    setVmSearch('');
    setStep(2);
    setDiscoverStatus({ text: statusMsg, tone: 'green' });
  };

  const handleSearch = async () => {
    if (!searchQuery.trim()) {
      setDiscoverStatus({ text: 'Enter a search term (customer name, server name, tag\u2026).', tone: 'amber' });
      return;
    }
    setSearchBusy(true);
    const scopeMsg = selectedSub ? `in subscription ${selectedSub}` : 'across accessible subscriptions';
    setDiscoverStatus({ text: `Searching ${scopeMsg} for "${searchQuery}"\u2026`, tone: 'muted' });
    try {
      const data = await searchAzureVms({
        query: searchQuery,
        subscription_ids: selectedSub ? [selectedSub] : undefined,
      });
      onDiscovered(data, `Found ${data.total} VMs matching "${searchQuery}"`);
    } catch (error) {
      setDiscoverStatus({ text: error instanceof Error ? error.message : 'Search failed.', tone: 'red' });
    } finally {
      setSearchBusy(false);
    }
  };

  const handleSubscriptionChange = async (subId: string) => {
    setSelectedSub(subId);
    setSelectedGroup('');
    if (!subId) return;
    try {
      const groupResult = await getAzureResourceGroups(subId);
      setGroups((groupResult.resource_groups as { name: string }[]) || []);
    } catch {
      setGroups([]);
    }
  };

  const handleBrowse = async () => {
    if (!selectedSub) {
      setDiscoverStatus({ text: 'Select a subscription first.', tone: 'amber' });
      return;
    }
    setBrowseBusy(true);
    setDiscoverStatus({ text: 'Listing VMs in subscription\u2026', tone: 'muted' });
    try {
      await updateConfig({ azure_subscription_id: selectedSub, azure_resource_group: selectedGroup || '' });
      const data = await discoverAzureVms({ subscription_id: selectedSub, resource_group: selectedGroup || null });
      if (!((data.vms as AzureVm[]) || []).length) {
        setDiscoverStatus({ text: 'No VMs found in this subscription/resource group.', tone: 'amber' });
        return;
      }
      onDiscovered(data, `Found ${data.total} VMs`);
    } catch (error) {
      setDiscoverStatus({ text: error instanceof Error ? error.message : 'Discovery failed.', tone: 'red' });
    } finally {
      setBrowseBusy(false);
    }
  };

  // ── Fleet-wide counts (always over the full discovered set, not the
  // currently filtered view — these are the menu of choices, not a result). ──
  const typeCounts = useMemo(() => {
    const c: Record<string, number> = { APP: 0, DB: 0, SRE: 0 };
    discoveredVms.forEach((v) => { c[v.type] = (c[v.type] || 0) + 1; });
    return c;
  }, [discoveredVms]);
  const envCounts = useMemo(() => {
    const c: Record<string, number> = {};
    discoveredVms.forEach((v) => { const e = getVmEnv(v); c[e] = (c[e] || 0) + 1; });
    return c;
  }, [discoveredVms]);
  const regionCounts = useMemo(() => {
    const c: Record<string, number> = {};
    discoveredVms.forEach((v) => { const loc = (v.location || 'unknown').trim() || 'unknown'; c[loc] = (c[loc] || 0) + 1; });
    return c;
  }, [discoveredVms]);
  const regions = useMemo(() => Object.keys(regionCounts).sort(), [regionCounts]);
  const pgCounts = useMemo(() => {
    const c: Record<string, number> = {};
    discoveredVms.forEach((v) => { if (v.product_group) c[v.product_group] = (c[v.product_group] || 0) + 1; });
    return c;
  }, [discoveredVms]);
  const productGroups = useMemo(() => Object.keys(pgCounts).sort(), [pgCounts]);
  const customerSet = useMemo(() => new Set(discoveredVms.map(customerOf)), [discoveredVms]);

  const filteredVms = useMemo(() => {
    let list = discoveredVms;
    if (typeFilters.size > 0) list = list.filter((v) => typeFilters.has(v.type));
    if (envFilter !== 'ALL') list = list.filter((v) => getVmEnv(v) === envFilter);
    if (regionFilter !== 'ALL') list = list.filter((v) => (v.location || 'unknown').trim() === regionFilter);
    if (pgFilter !== 'ALL') list = list.filter((v) => (v.product_group || '') === pgFilter);
    const q = vmSearch.toLowerCase().trim();
    if (q) {
      list = list.filter((v) =>
        (v.name || '').toLowerCase().includes(q) ||
        (v.application || '').toLowerCase().includes(q) ||
        customerOf(v).toLowerCase().includes(q));
    }
    return list;
  }, [discoveredVms, typeFilters, envFilter, regionFilter, pgFilter, vmSearch]);

  const { customerOrder, groupedFiltered, multiCustomer } = useMemo(() => {
    const map = new Map<string, AzureVm[]>();
    filteredVms.forEach((vm) => {
      const cust = customerOf(vm);
      if (!map.has(cust)) map.set(cust, []);
      map.get(cust)!.push(vm);
    });
    const customers = Array.from(map.keys()).sort((a, b) => {
      if (a === UNTAGGED) return 1;
      if (b === UNTAGGED) return -1;
      return a.localeCompare(b);
    });
    return {
      customerOrder: customers,
      groupedFiltered: map,
      multiCustomer: customers.length > 1 || (customers.length === 1 && customers[0] !== UNTAGGED),
    };
  }, [filteredVms]);

  const toggleType = (t: string) => {
    if (t === 'ALL') { setTypeFilters(new Set()); return; }
    setTypeFilters((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t); else next.add(t);
      return next;
    });
  };
  const singleSelect = (current: string, setter: (v: string) => void, value: string) =>
    setter(value === 'ALL' ? 'ALL' : current === value ? 'ALL' : value);

  const toggleVm = (id: string) => {
    setSelectedVmIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const selectAllVms = () => setSelectedVmIds(new Set(discoveredVms.map((v) => v.resource_id)));
  const clearSelection = () => setSelectedVmIds(new Set());
  const toggleVisibleVms = (checked: boolean) => {
    setSelectedVmIds((prev) => {
      const next = new Set(prev);
      filteredVms.forEach((v) => { if (checked) next.add(v.resource_id); else next.delete(v.resource_id); });
      return next;
    });
  };
  const customerAllVms = (customer: string) => discoveredVms.filter((v) => customerOf(v) === customer);
  const toggleCustomerSelection = (customer: string, checked: boolean) => {
    const vmsInGroup = groupedFiltered.get(customer) || [];
    setSelectedVmIds((prev) => {
      const next = new Set(prev);
      vmsInGroup.forEach((v) => { if (checked) next.add(v.resource_id); else next.delete(v.resource_id); });
      return next;
    });
  };
  const toggleCustomerGroup = (customer: string) => {
    setCollapsedCustomers((prev) => {
      const next = new Set(prev);
      if (next.has(customer)) next.delete(customer); else next.add(customer);
      return next;
    });
  };
  const expandAllCustomers = (expand: boolean) => {
    setCollapsedCustomers(expand ? new Set() : new Set(discoveredVms.map(customerOf)));
  };
  const resetFilters = () => {
    setTypeFilters(new Set());
    setEnvFilter('ALL');
    setRegionFilter('ALL');
    setPgFilter('ALL');
    setVmSearch('');
  };

  const allVisibleSelected = filteredVms.length > 0 && filteredVms.every((v) => selectedVmIds.has(v.resource_id));
  const someVisibleSelected = filteredVms.some((v) => selectedVmIds.has(v.resource_id));

  const handleFetch = async () => {
    if (!selectedVmIds.size) {
      setFetchStatus('Select at least one VM to fetch metrics for.');
      return;
    }
    setFetchBusy(true);
    setFetchStatus(`Pulling last ${hoursBack}h of CPU / Memory / Disk metrics for ${selectedVmIds.size} VM(s)\u2026`);
    try {
      const selectedVms = discoveredVms.filter((v) => selectedVmIds.has(v.resource_id));
      const result = await fetchAzureResourcesWithProgress(
        { hours_back: hoursBack, vm_ids: Array.from(selectedVmIds), vm_meta: selectedVms },
        ({ phase, done, total }) => {
          const count = total && total > 0 ? ` ${done || 0}/${total}` : '';
          setFetchStatus(`${phase || 'Querying Azure Monitor'}${count}\u2026`);
        },
      );
      // Same customer grouping key the discovery table already uses (customerOf) —
      // reuse it rather than re-deriving a second guess of "who is this for".
      const custCounts = new Map<string, number>();
      for (const v of selectedVms) {
        const c = customerOf(v);
        if (c !== UNTAGGED) custCounts.set(c, (custCounts.get(c) || 0) + 1);
      }
      const identifiedCustomers = Array.from(custCounts.keys());
      const taggedVmCount = Array.from(custCounts.values()).reduce((sum, count) => sum + count, 0);
      const customer = identifiedCustomers.length === 1 ? identifiedCustomers[0] : undefined;
      const customerStatus: AzureFetchMeta['customerStatus'] = customer
        ? 'identified'
        : identifiedCustomers.length > 1 ? 'mixed' : 'untagged';
      const customerMessage = customer
        ? `Identified from Azure customer tags on ${taggedVmCount}/${selectedVms.length} selected VM(s).`
        : identifiedCustomers.length > 1
          ? `Customer was not assigned because the selected VMs contain multiple customer tags: ${identifiedCustomers.sort().join(', ')}.`
          : 'Customer was not identified because the selected VMs have no Customer, CustomerName, Client, or ClientName tag.';
      setFetchStatus('Resolving fleet health and saving evidence…');
      await onFetched((result.servers as ResourceServer[]) || [], {
        hoursBack,
        customer,
        customerStatus,
        customerMessage,
        taggedVmCount,
        selectedVmCount: selectedVms.length,
      }, result);
      onClose();
    } catch (error) {
      setFetchStatus(error instanceof Error ? error.message : 'Azure fetch failed.');
    } finally {
      setFetchBusy(false);
    }
  };

  if (!open) return null;

  const connected = Boolean(authInfo?.method === 'browser' || authInfo?.logged_in || (authInfo?.name && authInfo?.name !== ''));
  const statusColor = { muted: '#6b7db3', amber: '#f59e0b', red: '#f87171', green: '#10d96e' };

  return (
    <Box
      onClick={onClose}
      style={{ position: 'fixed', inset: 0, zIndex: 1300, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,.6)', backdropFilter: 'blur(2px)' }}
    >
      <Box
        onClick={(e) => e.stopPropagation()}
        style={{ background: '#0d1526', border: '1px solid #213060', borderRadius: 16, padding: 20, width: 960, maxWidth: '95vw', maxHeight: '88vh', display: 'flex', flexDirection: 'column', gap: 12, overflow: 'hidden', boxShadow: '0 20px 60px rgba(0,0,0,.6)' }}
      >
        <Box display="flex" alignItems="center" justifyContent="space-between">
          <Box>
            <Typography variant="subtitle1" style={{ fontWeight: 800 }}>Fetch from Azure Monitor</Typography>
            <Typography variant="caption" color="textSecondary">
              Discover VMs {'\u2192'} filter by <span style={{ color: '#10b981', fontWeight: 700 }}>APP</span> / <span style={{ color: '#3b82f6', fontWeight: 700 }}>DB</span> / <span style={{ color: '#f59e0b', fontWeight: 700 }}>SRE</span> and environment {'\u2192'} pull live metrics
            </Typography>
          </Box>
          <Button onClick={onClose} style={{ minWidth: 32, color: '#6b7db3', fontSize: 22, lineHeight: 1 }}>&times;</Button>
        </Box>

        {/* Auth bar */}
        <Box display="flex" alignItems="center" justifyContent="space-between" style={{ borderRadius: 8, border: '1px solid #213060', padding: '10px 14px' }}>
          <Box display="flex" alignItems="center" style={{ gap: 8 }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: connected ? '#10d96e' : '#6b7db3', display: 'inline-block' }} />
            <Typography variant="caption" style={{ color: connected ? '#10d96e' : '#6b7db3' }}>
              {connected ? `Signed in as ${authInfo?.display_name || authInfo?.name}` : 'Not signed in'}
            </Typography>
          </Box>
          {connected ? (
            <Button size="small" onClick={handleSignOut} disabled={authBusy} style={{ fontSize: 10, color: '#6b7db3' }}>Sign out</Button>
          ) : (
            <Button size="small" variant="outlined" onClick={handleSignIn} disabled={authBusy} style={{ fontSize: 10 }}>
              {authBusy ? <CircularProgress size={14} /> : 'Sign in with Azure'}
            </Button>
          )}
        </Box>

        {/* Status notification banner (always visible when present) */}
        {discoverStatus && (
          <Box style={{
            borderRadius: 8,
            padding: '8px 14px',
            background: discoverStatus.tone === 'red' ? 'rgba(239, 68, 68, 0.15)' :
                        discoverStatus.tone === 'amber' ? 'rgba(245, 158, 11, 0.15)' :
                        discoverStatus.tone === 'green' ? 'rgba(16, 217, 110, 0.15)' : 'rgba(59, 130, 246, 0.15)',
            border: `1px solid ${statusColor[discoverStatus.tone]}`,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: statusColor[discoverStatus.tone], display: 'inline-block' }} />
            <Typography variant="caption" style={{ color: statusColor[discoverStatus.tone], fontWeight: 600, fontSize: 12 }}>
              {discoverStatus.text}
            </Typography>
          </Box>
        )}

        {/* Device Code Instructions Banner */}
        {deviceCodeInfo && (
          <Box style={{ borderRadius: 8, border: '1px solid #3b82f6', background: 'rgba(59,130,246,0.12)', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 6 }}>
            <Typography variant="body2" style={{ color: '#93c5fd', fontWeight: 800 }}>
              Action Required: Complete Azure Corporate Sign-In
            </Typography>
            <Typography variant="caption" style={{ color: '#e2e8f0', fontSize: 12 }}>
              1. Open <a href={deviceCodeInfo.verification_uri || 'https://microsoft.com/devicelogin'} target="_blank" rel="noreferrer" style={{ color: '#60a5fa', textDecoration: 'underline', fontWeight: 700 }}>{deviceCodeInfo.verification_uri || 'https://microsoft.com/devicelogin'}</a> in a new tab.
            </Typography>
            <Typography variant="caption" style={{ color: '#e2e8f0', fontSize: 12 }}>
              2. Enter code: <strong style={{ background: '#1e293b', padding: '3px 10px', borderRadius: 4, letterSpacing: '0.12em', fontSize: 14, color: '#38bdf8', border: '1px solid #38bdf8' }}>{deviceCodeInfo.user_code}</strong>
            </Typography>
            <Typography variant="caption" style={{ color: '#94a3b8', fontSize: 11, display: 'flex', alignItems: 'center', gap: 6 }}>
              <CircularProgress size={12} color="inherit" /> Waiting for Azure login to complete in your browser...
            </Typography>
          </Box>
        )}

        {step === 1 && (
          <Box style={{ overflowY: 'auto', flex: 1, display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Box style={{ borderRadius: 10, border: '1px solid #213060', padding: 16 }}>
              <Typography variant="caption" style={{ display: 'block', color: '#6b7db3', fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', fontSize: 9, marginBottom: 8 }}>
                Search by customer, server name, or tag
              </Typography>
              <Box display="flex" style={{ gap: 10 }}>
                <input
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') handleSearch(); }}
                  placeholder={'e.g. Nebraska Furniture Mart, Nfm, tsbh731403001, Oracle DB\u2026'}
                  style={{ flex: 1, background: 'rgba(0,0,0,.4)', border: '1px solid #213060', borderRadius: 6, padding: '10px 14px', color: '#e2e8f0', fontSize: 13, outline: 'none' }}
                />
                <Button variant="contained" color="primary" onClick={handleSearch} disabled={searchBusy}>
                  {searchBusy ? <CircularProgress size={16} color="inherit" /> : 'Search'}
                </Button>
              </Box>
            </Box>

            <Box display="flex" alignItems="center" style={{ gap: 12 }}>
              <Box style={{ flex: 1, height: 1, background: '#213060' }} />
              <Typography variant="caption" color="textSecondary">or browse by subscription</Typography>
              <Box style={{ flex: 1, height: 1, background: '#213060' }} />
            </Box>

            <Box display="flex" style={{ gap: 10 }} alignItems="flex-end">
              <Box style={{ flex: 1 }}>
                <Typography variant="caption" style={{ display: 'block', color: '#6b7db3', fontWeight: 700, fontSize: 9, textTransform: 'uppercase', marginBottom: 4 }}>Subscription</Typography>
                <select value={selectedSub} onChange={(e) => handleSubscriptionChange(e.target.value)} style={selectStyle}>
                  <option value="">
                    {subscriptions.length
                      ? 'Select subscription'
                      : subscriptionsWarming
                        ? 'Loading subscriptions\u2026'
                        : 'Select or paste subscription ID below'}
                  </option>
                  {subscriptions.map((sub) => <option key={sub.id} value={sub.id}>{sub.name || sub.id}</option>)}
                </select>
              </Box>
              <Box style={{ flex: 1 }}>
                <Typography variant="caption" style={{ display: 'block', color: '#6b7db3', fontWeight: 700, fontSize: 9, textTransform: 'uppercase', marginBottom: 4 }}>Resource Group (optional)</Typography>
                <select value={selectedGroup} onChange={(e) => setSelectedGroup(e.target.value)} style={selectStyle}>
                  <option value="">All (entire subscription)</option>
                  {groups.map((g) => <option key={g.name} value={g.name}>{g.name}</option>)}
                </select>
              </Box>
              <Button variant="outlined" onClick={handleBrowse} disabled={browseBusy || !selectedSub}>
                {browseBusy ? <CircularProgress size={16} /> : 'Browse'}
              </Button>
            </Box>

            <Box display="flex" alignItems="center" style={{ gap: 8, marginTop: -4 }}>
              <Typography variant="caption" style={{ color: '#6b7db3', fontSize: 11, whiteSpace: 'nowrap' }}>
                Or enter Subscription ID manually:
              </Typography>
              <input
                value={selectedSub}
                onChange={(e) => handleSubscriptionChange(e.target.value.trim())}
                placeholder="e.g. 00000000-0000-0000-0000-000000000000"
                style={{ flex: 1, background: 'rgba(0,0,0,.4)', border: '1px solid #213060', borderRadius: 6, padding: '6px 10px', color: '#e2e8f0', fontSize: 12, outline: 'none' }}
              />
            </Box>
          </Box>
        )}

        {step === 2 && (
          <Box style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', gap: 10, overflow: 'hidden' }}>
            {/* Summary strip */}
            <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap' }}>
              <Button size="small" onClick={() => setStep(1)} style={{ fontSize: 10, color: '#6b7db3', minWidth: 0 }}>{'\u2039'} Search</Button>
              <span style={{ color: '#334155' }}>|</span>
              <Typography variant="body2" style={{ fontWeight: 800 }}>
                {customerSet.size > 1 ? `${discoveredVms.length} VMs \u00b7 ${customerSet.size} customers` : `${discoveredVms.length} VMs`}
              </Typography>
              <span style={segBtnStyle(false, TYPE_COLOR.APP)}>APP {typeCounts.APP || 0}</span>
              <span style={segBtnStyle(false, TYPE_COLOR.DB)}>DB {typeCounts.DB || 0}</span>
              <span style={segBtnStyle(false, TYPE_COLOR.SRE)}>SRE {typeCounts.SRE || 0}</span>
              <Box display="flex" style={{ gap: 4, flexWrap: 'wrap', paddingLeft: 8, borderLeft: '1px solid #213060' }}>
                {Object.entries(envCounts).sort(([a], [b]) => a.localeCompare(b)).map(([e, c]) => (
                  <span key={e} style={segBtnStyle(false, ENV_COLOR[e])}>{e} {c}</span>
                ))}
              </Box>
              <Box display="flex" style={{ gap: 8, marginLeft: 'auto' }} alignItems="center">
                <Button size="small" variant="outlined" onClick={selectAllVms} style={{ fontSize: 10 }}>Select all VMs</Button>
                <Button size="small" onClick={clearSelection} style={{ fontSize: 10, color: '#6b7db3' }}>Clear selection</Button>
                {multiCustomer && (
                  <>
                    <span style={{ color: '#334155' }}>{'\u00b7'}</span>
                    <Button size="small" onClick={() => expandAllCustomers(true)} style={{ fontSize: 10, color: '#6b7db3' }}>Expand groups</Button>
                    <Button size="small" onClick={() => expandAllCustomers(false)} style={{ fontSize: 10, color: '#6b7db3' }}>Collapse groups</Button>
                  </>
                )}
              </Box>
            </Box>

            {/* Fleet filters */}
            <Box style={{ borderRadius: 10, border: '1px solid #213060', padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
              <Box display="flex" alignItems="center" justifyContent="space-between">
                <Typography variant="caption" style={{ fontWeight: 800, textTransform: 'uppercase', fontSize: 9, letterSpacing: '.1em', color: '#6b7db3' }}>
                  Fleet filters {filteredVms.length !== discoveredVms.length && <span style={{ color: '#3b82f6' }}>({filteredVms.length} of {discoveredVms.length})</span>}
                </Typography>
                <Button size="small" onClick={resetFilters} style={{ fontSize: 9, color: '#6b7db3' }}>Reset filters</Button>
              </Box>

              <Box display="flex" alignItems="center" style={{ gap: 8 }}>
                <span style={filterLabelStyle}>Type</span>
                <Box display="flex" style={{ gap: 6, flexWrap: 'wrap' }}>
                  <button onClick={() => toggleType('ALL')} style={segBtnStyle(typeFilters.size === 0)}>All</button>
                  {(['APP', 'DB', 'SRE'] as const).map((t) => (
                    <button key={t} onClick={() => toggleType(t)} style={segBtnStyle(typeFilters.has(t), TYPE_COLOR[t])}>
                      {t}<span style={chipStyle}>{typeCounts[t] || 0}</span>
                    </button>
                  ))}
                </Box>
              </Box>

              <Box display="flex" alignItems="center" style={{ gap: 8 }}>
                <span style={filterLabelStyle}>Env</span>
                <Box display="flex" style={{ gap: 6, flexWrap: 'wrap' }}>
                  <button onClick={() => singleSelect(envFilter, setEnvFilter, 'ALL')} style={segBtnStyle(envFilter === 'ALL')}>All</button>
                  {(['PROD', 'TEST', 'UAT', 'STG', 'DEV'] as const).map((e) => (
                    <button key={e} onClick={() => singleSelect(envFilter, setEnvFilter, e)} style={segBtnStyle(envFilter === e, ENV_COLOR[e])}>
                      {e}<span style={chipStyle}>{envCounts[e] || 0}</span>
                    </button>
                  ))}
                </Box>
              </Box>

              {regions.length > 1 && (
                <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap' }}>
                  <span style={filterLabelStyle}>Region</span>
                  <Box display="flex" style={{ gap: 6, flexWrap: 'wrap' }}>
                    <button onClick={() => singleSelect(regionFilter, setRegionFilter, 'ALL')} style={segBtnStyle(regionFilter === 'ALL')}>All</button>
                    {regions.map((r) => (
                      <button key={r} onClick={() => singleSelect(regionFilter, setRegionFilter, r)} style={segBtnStyle(regionFilter === r)}>
                        {r}<span style={chipStyle}>{regionCounts[r]}</span>
                      </button>
                    ))}
                  </Box>
                </Box>
              )}

              {productGroups.length > 0 && (
                <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap' }}>
                  <span style={filterLabelStyle}>Product Group</span>
                  <Box display="flex" style={{ gap: 6, flexWrap: 'wrap' }}>
                    <button onClick={() => singleSelect(pgFilter, setPgFilter, 'ALL')} style={segBtnStyle(pgFilter === 'ALL')}>All</button>
                    {productGroups.map((pg) => (
                      <button key={pg} onClick={() => singleSelect(pgFilter, setPgFilter, pg)} style={segBtnStyle(pgFilter === pg)}>
                        {pg}<span style={chipStyle}>{pgCounts[pg]}</span>
                      </button>
                    ))}
                  </Box>
                </Box>
              )}

              <input
                value={vmSearch}
                onChange={(e) => setVmSearch(e.target.value)}
                placeholder={'Filter by VM name, app, or customer\u2026'}
                style={{ background: 'rgba(0,0,0,.35)', border: '1px solid #213060', borderRadius: 6, padding: '7px 10px', color: '#e2e8f0', fontSize: 12, outline: 'none' }}
              />
            </Box>

            {/* VM table */}
            <Box style={{ flex: 1, minHeight: 0, overflow: 'auto', border: '1px solid #213060', borderRadius: 10 }}>
              <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse' }}>
                <thead style={{ position: 'sticky', top: 0, background: '#0a0f1e', zIndex: 1 }}>
                  <tr>
                    <th style={{ padding: '6px 8px', width: 28 }}>
                      <input type="checkbox" checked={allVisibleSelected} ref={(el) => { if (el) el.indeterminate = !allVisibleSelected && someVisibleSelected; }} onChange={(e) => toggleVisibleVms(e.target.checked)} />
                    </th>
                    {['VM Name', 'Type', 'Env', 'App', 'Customer', 'Region'].map((h) => (
                      <th key={h} style={thStyle}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {customerOrder.map((cust) => {
                    const vmsInGroup = groupedFiltered.get(cust) || [];
                    const collapsed = collapsedCustomers.has(cust);
                    const allCustVms = customerAllVms(cust);
                    const allSel = allCustVms.every((v) => selectedVmIds.has(v.resource_id));
                    const anySel = allCustVms.some((v) => selectedVmIds.has(v.resource_id));
                    const typeBreakdown: Record<string, number> = {};
                    vmsInGroup.forEach((v) => { typeBreakdown[v.type] = (typeBreakdown[v.type] || 0) + 1; });
                    return (
                      <React.Fragment key={cust}>
                        {multiCustomer && (
                          <tr style={{ background: 'rgba(59,130,246,.05)', borderTop: '2px solid #213060' }}>
                            <td style={{ padding: '6px 8px' }}>
                              <input type="checkbox" checked={allSel} ref={(el) => { if (el) el.indeterminate = !allSel && anySel; }} onChange={(e) => toggleCustomerSelection(cust, e.target.checked)} />
                            </td>
                            <td colSpan={6} style={{ padding: '6px 8px' }}>
                              <button onClick={() => toggleCustomerGroup(cust)} style={{ background: 'none', border: 'none', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6, color: '#f0f4ff' }}>
                                <span style={{ display: 'inline-block', transform: collapsed ? 'rotate(-90deg)' : 'none', fontSize: 9 }}>&#9660;</span>
                                <span style={{ fontWeight: 700, fontSize: 11 }}>{cust}</span>
                                <span style={{ color: '#6b7db3', fontSize: 10 }}>
                                  {vmsInGroup.length} VM{vmsInGroup.length !== 1 ? 's' : ''} {'\u00b7'} {Object.entries(typeBreakdown).map(([t, c]) => `${t}:${c}`).join(' \u00b7 ')}
                                </span>
                              </button>
                            </td>
                          </tr>
                        )}
                        {!collapsed && vmsInGroup.map((vm) => {
                          const env = getVmEnv(vm);
                          return (
                            <tr key={vm.resource_id} style={{ borderTop: '1px solid rgba(33,48,96,.3)' }}>
                              <td style={{ padding: '5px 8px' }}>
                                <input type="checkbox" checked={selectedVmIds.has(vm.resource_id)} onChange={() => toggleVm(vm.resource_id)} />
                              </td>
                              <td style={{ padding: '5px 8px', fontFamily: 'monospace', color: '#f0f4ff' }}>{vm.name}</td>
                              <td style={{ padding: '5px 8px' }}><span style={{ color: TYPE_COLOR[vm.type] || '#6b7db3', fontWeight: 700, fontSize: 10 }}>{vm.type}</span></td>
                              <td style={{ padding: '5px 8px' }}><span style={{ color: ENV_COLOR[env] || '#6b7db3', fontWeight: 700, fontSize: 10 }}>{env}</span></td>
                              <td style={{ padding: '5px 8px', color: '#6b7db3' }}>{vm.application || vm.tags?.Application || ''}</td>
                              <td style={{ padding: '5px 8px', color: '#6b7db3', maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={cust}>{cust}</td>
                              <td style={{ padding: '5px 8px', color: '#6b7db3' }}>{vm.location}</td>
                            </tr>
                          );
                        })}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </Box>

            {/* Footer */}
            <Box display="flex" alignItems="center" style={{ gap: 12, borderTop: '1px solid #213060', paddingTop: 8, flexWrap: 'wrap' }}>
              <Box display="flex" alignItems="center" style={{ gap: 6 }}>
                <Typography variant="caption" style={{ fontWeight: 700, fontSize: 9, textTransform: 'uppercase', color: '#6b7db3' }}>History</Typography>
                <select value={String(hoursBack)} onChange={(e) => setHoursBack(Number(e.target.value))} style={{ ...selectStyle, width: 'auto', padding: '5px 8px', fontSize: 11 }}>
                  <option value="1">1h</option>
                  <option value="6">6h</option>
                  <option value="24">24h</option>
                  <option value="72">3d</option>
                  <option value="168">7d</option>
                  <option value="360">15d</option>
                  <option value="720">30d</option>
                </select>
              </Box>
              <Typography variant="caption" color="textSecondary">{selectedVmIds.size} of {discoveredVms.length} selected</Typography>
              <Box display="flex" style={{ marginLeft: 'auto', gap: 8 }}>
                <Button variant="contained" color="primary" onClick={handleFetch} disabled={fetchBusy || !selectedVmIds.size}>
                  {fetchBusy ? <CircularProgress size={16} color="inherit" /> : 'Fetch Metrics'}
                </Button>
                <Button variant="outlined" onClick={onClose}>Cancel</Button>
              </Box>
            </Box>
            {fetchStatus && <Typography variant="caption" color="textSecondary">{fetchStatus}</Typography>}
          </Box>
        )}


      </Box>
    </Box>
  );
}

const selectStyle: React.CSSProperties = {
  width: '100%', background: '#0a0f1e', border: '1px solid #213060', borderRadius: 6,
  padding: '9px 10px', color: '#e2e8f0', fontSize: 12, outline: 'none',
};
const thStyle: React.CSSProperties = {
  padding: '6px 8px', textAlign: 'left', fontSize: 9, fontWeight: 800, textTransform: 'uppercase',
  letterSpacing: '.08em', color: 'rgba(107,125,179,.7)',
};
const filterLabelStyle: React.CSSProperties = { fontSize: 9, fontWeight: 800, textTransform: 'uppercase', color: '#6b7db3', minWidth: 48 };
