import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import {
  clearSession,
  DashboardPayload,
  getReviewedProducts,
  getSessionRestore,
  getSowState,
  ResourceServer,
} from '../api/dashboardApi';

export interface IssueRecord {
  ID: string;
  Type: string;
  Severity: string;
  Status: string;
  Owner: string;
  ETA: string;
  Description: string;
  Mitigation: string;
  Logged: string;
}

export interface ApprovalsState {
  checklist: Record<'batch' | 'issues' | 'ui' | 'res' | 'perf' | 'sow' | 'data' | 'ctrlm' | 'res15', boolean>;
  pe: { name: string; approved: boolean; date: string | null; override_blockers: boolean };
  customer: { name: string; approved: boolean; date: string | null };
  notes: string;
}

const EMPTY_APPROVALS: ApprovalsState = {
  checklist: { batch: false, issues: false, ui: false, res: false, perf: false, sow: false, data: false, ctrlm: false, res15: false },
  pe: { name: '', approved: false, date: null, override_blockers: false },
  customer: { name: '', approved: false, date: null },
  notes: '',
};

export interface AppData {
  batch: DashboardPayload | null;
  resource: (DashboardPayload & { servers: ResourceServer[] }) | null;
  slaMatrix: DashboardPayload | null;
  benchmark: DashboardPayload | null;
  sowBaseline: DashboardPayload | null;
  sowCompare: DashboardPayload | null;
  findings: DashboardPayload | null;
  redFlags: DashboardPayload | null;
  peNarrative: DashboardPayload | null;
  executive: DashboardPayload | null;
  finalJudgment: DashboardPayload | null;
  customerName: string | null;
  issues: IssueRecord[];
  approvals: ApprovalsState;
  reviewedProducts: string[];
}

const EMPTY_APP_DATA: AppData = {
  batch: null,
  resource: null,
  slaMatrix: null,
  benchmark: null,
  sowBaseline: null,
  sowCompare: null,
  findings: null,
  redFlags: null,
  peNarrative: null,
  executive: null,
  finalJudgment: null,
  customerName: null,
  issues: [],
  approvals: EMPTY_APPROVALS,
  reviewedProducts: [],
};

const isDashboardPayload = (value: unknown): value is DashboardPayload =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const hasDashboardPayload = (value: unknown): value is DashboardPayload =>
  isDashboardPayload(value) && Object.keys(value as Record<string, unknown>).length > 0;

const isEmptyDashboardPayload = (value: DashboardPayload | null | undefined): boolean =>
  !hasDashboardPayload(value);

const normalizeStringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? value
      .map((item) => String(item || '').trim())
      .filter(Boolean)
    : [];

export const normalizeCustomer = (name: unknown): string => {
  if (typeof name !== 'string') return '';
  return name.toLowerCase().replace(/[^a-z0-9]/g, '');
};

export const isCustomerChange = (current: string | null | undefined, incoming: string | null | undefined): boolean => {
  const normCurrent = normalizeCustomer(current);
  const normIncoming = normalizeCustomer(incoming);
  if (!normCurrent || !normIncoming) return false;
  return normCurrent !== normIncoming;
};

interface AppDataContextValue {
  data: AppData;
  lastSyncTime: number | null;
  isLiveSyncing: boolean;
  syncLiveState: () => Promise<void>;
  setBatch: (value: DashboardPayload | null) => void;
  setResource: (value: (DashboardPayload & { servers: ResourceServer[] }) | null) => void;
  setSlaMatrix: (value: DashboardPayload | null) => void;
  setBenchmark: (value: DashboardPayload | null) => void;
  setSowBaseline: (value: DashboardPayload | null) => void;
  setSowCompare: (value: DashboardPayload | null) => void;
  setFindings: (value: DashboardPayload | null) => void;
  setRedFlags: (value: DashboardPayload | null) => void;
  setPeNarrative: (value: DashboardPayload | null) => void;
  setExecutive: (value: DashboardPayload | null) => void;
  setFinalJudgment: (value: DashboardPayload | null) => void;
  setCustomerName: (value: string | null) => void;
  setIssues: (value: IssueRecord[]) => void;
  setApprovals: (value: ApprovalsState) => void;
  setReviewedProducts: (value: string[]) => void;
  resetSession: () => Promise<void>;
}

const AppDataContext = createContext<AppDataContextValue | undefined>(undefined);

// Settle a promise into a fulfilled/rejected descriptor without depending on the
// environment's Promise.allSettled implementation — some polyfilled test/build
// targets return a settled status but silently drop the resolved value.
type Settled<T> = { status: 'fulfilled'; value: T } | { status: 'rejected'; reason: unknown };
const settle = <T,>(promise: Promise<T>): Promise<Settled<T>> =>
  promise.then(
    (value): Settled<T> => ({ status: 'fulfilled', value }),
    (reason): Settled<T> => ({ status: 'rejected', reason }),
  );

export const AppDataProvider = ({ children }: { children: React.ReactNode }) => {
  const [data, setData] = useState<AppData>(EMPTY_APP_DATA);
  const [lastSyncTime, setLastSyncTime] = useState<number | null>(null);
  const [isLiveSyncing, setIsLiveSyncing] = useState<boolean>(false);
  const isSyncingRef = React.useRef(false);

  const syncLiveState = useCallback(async () => {
    if (isSyncingRef.current) return;
    isSyncingRef.current = true;
    setIsLiveSyncing(true);
    try {
      const [sowResult, sessionRestoreResult, reviewedProductsResult] = await Promise.all([
        settle(getSowState()),
        settle(getSessionRestore()),
        settle(getReviewedProducts()),
      ]);

      setData((prev) => {
        let current = prev;
        const restored = sessionRestoreResult.status === 'fulfilled' ? sessionRestoreResult.value : null;
        const sow = sowResult.status === 'fulfilled' ? sowResult.value : null;
        const reviewed = reviewedProductsResult.status === 'fulfilled' ? reviewedProductsResult.value : null;

        const restoredCustomer = typeof restored?.customer_name === 'string' && restored.customer_name.trim()
          ? restored.customer_name.trim()
          : null;

        // Check if the backend customer changed from what frontend holds
        const customerSwitched = Boolean(restoredCustomer && isCustomerChange(current.customerName, restoredCustomer));
        if (customerSwitched && restoredCustomer) {
          // Complete customer data isolation: wipe old customer state
          current = {
            ...EMPTY_APP_DATA,
            customerName: restoredCustomer,
          };
        }

        const updates: Partial<AppData> = {};

        if (sow) {
          const baseline = sow.baseline;
          const comparison = sow.compare;
          if (hasDashboardPayload(baseline)) {
            if (customerSwitched || isEmptyDashboardPayload(current.sowBaseline)) updates.sowBaseline = baseline;
          }
          if (hasDashboardPayload(comparison)) {
            if (customerSwitched || isEmptyDashboardPayload(current.sowCompare)) updates.sowCompare = comparison;
          }
        }

        if (restored) {
          if (customerSwitched) {
            // When customer switched, adopt exactly what backend currently holds
            updates.batch = hasDashboardPayload(restored.batch) ? restored.batch : null;
            updates.resource = hasDashboardPayload(restored.resource) ? (restored.resource as AppData['resource']) : null;
            updates.slaMatrix = hasDashboardPayload(restored.sla_matrix) ? restored.sla_matrix : null;
            updates.benchmark = hasDashboardPayload(restored.benchmark) ? restored.benchmark : null;
            updates.findings = hasDashboardPayload(restored.findings) ? restored.findings : null;
            updates.redFlags = hasDashboardPayload(restored.red_flags) ? restored.red_flags : null;
            updates.peNarrative = hasDashboardPayload(restored.pe_narrative) ? restored.pe_narrative : null;
            updates.executive = hasDashboardPayload(restored.executive) ? restored.executive : null;
            updates.finalJudgment = hasDashboardPayload(restored.final_judgment) ? restored.final_judgment : null;
            updates.customerName = restoredCustomer;
          } else {
            // Normal sync without customer switch: fill missing or update
            if (isEmptyDashboardPayload(current.batch) && hasDashboardPayload(restored.batch)) updates.batch = restored.batch;
            if (isEmptyDashboardPayload(current.resource) && hasDashboardPayload(restored.resource)) updates.resource = restored.resource as AppData['resource'];
            if (isEmptyDashboardPayload(current.slaMatrix) && hasDashboardPayload(restored.sla_matrix)) updates.slaMatrix = restored.sla_matrix;
            if (isEmptyDashboardPayload(current.benchmark) && hasDashboardPayload(restored.benchmark)) updates.benchmark = restored.benchmark;
            if (isEmptyDashboardPayload(current.findings) && hasDashboardPayload(restored.findings)) updates.findings = restored.findings;
            if (isEmptyDashboardPayload(current.redFlags) && hasDashboardPayload(restored.red_flags)) updates.redFlags = restored.red_flags;
            if (isEmptyDashboardPayload(current.peNarrative) && hasDashboardPayload(restored.pe_narrative)) updates.peNarrative = restored.pe_narrative;
            if (isEmptyDashboardPayload(current.executive) && hasDashboardPayload(restored.executive)) updates.executive = restored.executive;
            if (isEmptyDashboardPayload(current.finalJudgment) && hasDashboardPayload(restored.final_judgment)) updates.finalJudgment = restored.final_judgment;
            if ((!current.customerName || !current.customerName.trim()) && restoredCustomer) {
              updates.customerName = restoredCustomer;
            }
          }

          if (current.reviewedProducts.length === 0) {
            const restoredProducts = normalizeStringList(restored.reviewed_products);
            if (restoredProducts.length) updates.reviewedProducts = restoredProducts;
          }
        }

        if (reviewed && current.reviewedProducts.length === 0 && !updates.reviewedProducts?.length) {
          const restoredProducts = normalizeStringList(reviewed.products);
          if (restoredProducts.length) updates.reviewedProducts = restoredProducts;
        }

        return Object.keys(updates).length ? { ...current, ...updates } : current;
      });

      setLastSyncTime(Date.now());
    } catch {
      // Non-blocking sync
    } finally {
      isSyncingRef.current = false;
      setIsLiveSyncing(false);
    }
  }, []);

  useEffect(() => {
    // Initial sync
    void syncLiveState();

    // Periodic live web sync every 10s
    const timer = setInterval(() => {
      void syncLiveState();
    }, 10000);

    // Sync when returning to window or tab
    const handleFocus = () => {
      void syncLiveState();
    };
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') {
        void syncLiveState();
      }
    };

    window.addEventListener('focus', handleFocus);
    document.addEventListener('visibilitychange', handleVisibility);

    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', handleFocus);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [syncLiveState]);

  const setBatch = useCallback((value: DashboardPayload | null) => {
    setData((prev) => {
      const incomingCustomer = typeof (value as any)?.customer_name === 'string'
        ? (value as any).customer_name.trim()
        : null;

      if (incomingCustomer && isCustomerChange(prev.customerName, incomingCustomer)) {
        return {
          ...EMPTY_APP_DATA,
          customerName: incomingCustomer,
          batch: value,
        };
      }

      return {
        ...prev,
        batch: value,
        customerName: prev.customerName || incomingCustomer,
      };
    });
  }, []);

  const setResource = useCallback(
    (value: (DashboardPayload & { servers: ResourceServer[] }) | null) => {
      setData((prev) => {
        const incomingCustomer = typeof (value as any)?.customer_name === 'string'
          ? (value as any).customer_name.trim()
          : (value as any)?.servers?.[0]?.customer?.trim() || null;

        if (incomingCustomer && isCustomerChange(prev.customerName, incomingCustomer)) {
          return {
            ...EMPTY_APP_DATA,
            customerName: incomingCustomer,
            resource: value,
          };
        }

        return {
          ...prev,
          resource: value,
          customerName: prev.customerName || incomingCustomer,
        };
      });
    },
    [],
  );

  const setSlaMatrix = useCallback(
    (value: DashboardPayload | null) => {
      setData((prev) => {
        const incomingCustomer = typeof (value as any)?.customer_name === 'string'
          ? (value as any).customer_name.trim()
          : null;

        if (incomingCustomer && isCustomerChange(prev.customerName, incomingCustomer)) {
          return {
            ...EMPTY_APP_DATA,
            customerName: incomingCustomer,
            slaMatrix: value,
          };
        }

        return {
          ...prev,
          slaMatrix: value,
          customerName: prev.customerName || incomingCustomer,
        };
      });
    },
    [],
  );

  const setBenchmark = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, benchmark: value })),
    [],
  );

  const setSowBaseline = useCallback(
    (value: DashboardPayload | null) => {
      setData((prev) => {
        const incomingCustomer = typeof (value as any)?.customer_name === 'string'
          ? (value as any).customer_name.trim()
          : null;

        if (incomingCustomer && isCustomerChange(prev.customerName, incomingCustomer)) {
          return {
            ...EMPTY_APP_DATA,
            customerName: incomingCustomer,
            sowBaseline: value,
          };
        }

        return {
          ...prev,
          sowBaseline: value,
          customerName: prev.customerName || incomingCustomer,
        };
      });
    },
    [],
  );

  const setSowCompare = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, sowCompare: value })),
    [],
  );

  const setFindings = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, findings: value })),
    [],
  );

  const setRedFlags = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, redFlags: value })),
    [],
  );

  const setPeNarrative = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, peNarrative: value })),
    [],
  );

  const setExecutive = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, executive: value })),
    [],
  );

  const setFinalJudgment = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, finalJudgment: value })),
    [],
  );

  const setCustomerName = useCallback(
    (value: string | null) => {
      setData((prev) => {
        const newCustomer = value?.trim() || null;
        if (newCustomer && isCustomerChange(prev.customerName, newCustomer)) {
          return {
            ...EMPTY_APP_DATA,
            customerName: newCustomer,
          };
        }
        return { ...prev, customerName: newCustomer ?? prev.customerName };
      });
    },
    [],
  );

  const setIssues = useCallback(
    (value: IssueRecord[]) => setData((prev) => ({ ...prev, issues: value })),
    [],
  );

  const setApprovals = useCallback(
    (value: ApprovalsState) => setData((prev) => ({ ...prev, approvals: value })),
    [],
  );

  const setReviewedProducts = useCallback(
    (value: string[]) => setData((prev) => ({ ...prev, reviewedProducts: value })),
    [],
  );

  const resetSession = useCallback(async () => {
    try {
      await clearSession();
    } finally {
      try {
        window.sessionStorage.removeItem('pe-dashboard:sow-form-draft-v2');
        window.sessionStorage.removeItem('pe-dashboard:sow-actual-draft');
      } catch { /* optional browser storage */ }
      setData(EMPTY_APP_DATA);
    }
  }, []);

  const value = useMemo<AppDataContextValue>(
    () => ({
      data,
      lastSyncTime,
      isLiveSyncing,
      syncLiveState,
      setBatch,
      setResource,
      setSlaMatrix,
      setBenchmark,
      setSowBaseline,
      setSowCompare,
      setFindings,
      setRedFlags,
      setPeNarrative,
      setExecutive,
      setFinalJudgment,
      setCustomerName,
      setIssues,
      setApprovals,
      setReviewedProducts,
      resetSession,
    }),
    [
      data,
      lastSyncTime,
      isLiveSyncing,
      syncLiveState,
      setBatch,
      setResource,
      setSlaMatrix,
      setBenchmark,
      setSowBaseline,
      setSowCompare,
      setFindings,
      setRedFlags,
      setPeNarrative,
      setExecutive,
      setFinalJudgment,
      setCustomerName,
      setIssues,
      setApprovals,
      setReviewedProducts,
      resetSession,
    ],
  );

  return <AppDataContext.Provider value={value}>{children}</AppDataContext.Provider>;
};

export const useAppData = (): AppDataContextValue => {
  const ctx = useContext(AppDataContext);
  if (!ctx) {
    throw new Error('useAppData must be used within an AppDataProvider');
  }
  return ctx;
};
