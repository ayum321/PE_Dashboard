import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { clearSession, DashboardPayload, ResourceServer } from '../api/dashboardApi';

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
  resource: { servers: ResourceServer[] } | null;
  slaMatrix: DashboardPayload | null;
  benchmark: DashboardPayload | null;
  sowBaseline: DashboardPayload | null;
  sowCompare: DashboardPayload | null;
  findings: DashboardPayload | null;
  redFlags: DashboardPayload | null;
  executive: DashboardPayload | null;
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
  executive: null,
  customerName: null,
  issues: [],
  approvals: EMPTY_APPROVALS,
  reviewedProducts: [],
};

interface AppDataContextValue {
  data: AppData;
  setBatch: (value: DashboardPayload | null) => void;
  setResource: (value: { servers: ResourceServer[] } | null) => void;
  setSlaMatrix: (value: DashboardPayload | null) => void;
  setBenchmark: (value: DashboardPayload | null) => void;
  setSowBaseline: (value: DashboardPayload | null) => void;
  setSowCompare: (value: DashboardPayload | null) => void;
  setFindings: (value: DashboardPayload | null) => void;
  setRedFlags: (value: DashboardPayload | null) => void;
  setExecutive: (value: DashboardPayload | null) => void;
  setCustomerName: (value: string | null) => void;
  setIssues: (value: IssueRecord[]) => void;
  setApprovals: (value: ApprovalsState) => void;
  setReviewedProducts: (value: string[]) => void;
  resetSession: () => Promise<void>;
}

const AppDataContext = createContext<AppDataContextValue | undefined>(undefined);

export const AppDataProvider = ({ children }: { children: React.ReactNode }) => {
  const [data, setData] = useState<AppData>(EMPTY_APP_DATA);

  const setBatch = useCallback((value: DashboardPayload | null) => setData((prev) => ({ ...prev, batch: value })), []);
  const setResource = useCallback(
    (value: { servers: ResourceServer[] } | null) => setData((prev) => ({ ...prev, resource: value })),
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
  const setExecutive = useCallback(
    (value: DashboardPayload | null) => setData((prev) => ({ ...prev, executive: value })),
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
      setExecutive,
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
      setExecutive,
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
