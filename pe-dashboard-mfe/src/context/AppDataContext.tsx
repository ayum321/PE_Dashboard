import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { clearSession, DashboardPayload, ResourceServer } from '../api/dashboardApi';

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
