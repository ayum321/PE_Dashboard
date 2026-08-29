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

interface AppDataContextValue {
  data: AppData;
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

export const AppDataProvider = ({ children }: { children: React.ReactNode }) => {
  const [data, setData] = useState<AppData>(EMPTY_APP_DATA);

  // The provider is recreated by a browser refresh and can be bypassed by a
  // direct route. Restore saved SOW, cached dashboard payloads, and reviewed
  // products once so refreshes do not blank analysis screens that the backend
  // already holds for the active session.
  useEffect(() => {
    let active = true;
    Promise.allSettled([getSowState(), getSessionRestore(), getReviewedProducts()])
      .then(([sowResult, sessionRestoreResult, reviewedProductsResult]) => {
        if (!active) return;
        setData((prev) => {
          const updates: Partial<AppData> = {};

          if (sowResult.status === 'fulfilled') {
            const baseline = sowResult.value.baseline;
            const comparison = sowResult.value.compare;
            if (isEmptyDashboardPayload(prev.sowBaseline) && hasDashboardPayload(baseline)) updates.sowBaseline = baseline;
            if (isEmptyDashboardPayload(prev.sowCompare) && hasDashboardPayload(comparison)) updates.sowCompare = comparison;
          }

          if (sessionRestoreResult.status === 'fulfilled') {
            const restored = sessionRestoreResult.value;
            if (isEmptyDashboardPayload(prev.batch) && hasDashboardPayload(restored.batch)) updates.batch = restored.batch;
            if (isEmptyDashboardPayload(prev.resource) && hasDashboardPayload(restored.resource)) updates.resource = restored.resource as AppData['resource'];
            if (isEmptyDashboardPayload(prev.slaMatrix) && hasDashboardPayload(restored.sla_matrix)) updates.slaMatrix = restored.sla_matrix;
            if (isEmptyDashboardPayload(prev.benchmark) && hasDashboardPayload(restored.benchmark)) updates.benchmark = restored.benchmark;
            if (isEmptyDashboardPayload(prev.findings) && hasDashboardPayload(restored.findings)) updates.findings = restored.findings;
            if (isEmptyDashboardPayload(prev.redFlags) && hasDashboardPayload(restored.red_flags)) updates.redFlags = restored.red_flags;
            if (isEmptyDashboardPayload(prev.peNarrative) && hasDashboardPayload(restored.pe_narrative)) updates.peNarrative = restored.pe_narrative;
            if (isEmptyDashboardPayload(prev.executive) && hasDashboardPayload(restored.executive)) updates.executive = restored.executive;
            if (isEmptyDashboardPayload(prev.finalJudgment) && hasDashboardPayload(restored.final_judgment)) updates.finalJudgment = restored.final_judgment;
            if ((!prev.customerName || !prev.customerName.trim()) && typeof restored.customer_name === 'string' && restored.customer_name.trim()) {
              updates.customerName = restored.customer_name.trim();
            }
            if (prev.reviewedProducts.length === 0) {
              const restoredProducts = normalizeStringList(restored.reviewed_products);
              if (restoredProducts.length) updates.reviewedProducts = restoredProducts;
            }
          }

          if (reviewedProductsResult.status === 'fulfilled' && prev.reviewedProducts.length === 0 && !updates.reviewedProducts?.length) {
            const restoredProducts = normalizeStringList(reviewedProductsResult.value.products);
            if (restoredProducts.length) updates.reviewedProducts = restoredProducts;
          }

          return Object.keys(updates).length ? { ...prev, ...updates } : prev;
        });
      })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  const setBatch = useCallback((value: DashboardPayload | null) => setData((prev) => ({ ...prev, batch: value })), []);
  const setResource = useCallback(
    (value: (DashboardPayload & { servers: ResourceServer[] }) | null) => setData((prev) => ({ ...prev, resource: value })),
    [],
  );
  const setSlaMatrix = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, slaMatrix: value })),
    [],
  );
  const setBenchmark = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, benchmark: value })),
    [],
  );
  const setSowBaseline = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, sowBaseline: value })),
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
    (value: string | null) => setData((prev) => ({ ...prev, customerName: value })),
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
