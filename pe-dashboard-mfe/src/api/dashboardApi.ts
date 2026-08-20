export interface AuditContext {
  status: Record<string, 'loaded' | 'missing'>;
  completeness_pct: number;
}

export interface SmartUploadResponse {
  filename: string;
  classification: {
    type: string;
    confidence?: number;
  };
  data: {
    filename?: string;
    server_count?: number;
    image_only?: boolean;
    servers?: ResourceServer[];
    sla_mode?: string;
    sla_label?: string;
    total_runs?: number;
    total_jobs?: number;
    breaching_runs?: number;
    at_risk_runs?: number;
    ok_runs?: number;
    compliance_pct?: number;
    breach_rate_pct?: number;
    worst_job?: string;
    worst_hrs?: number;
    breaches?: SlaBreach[];
    error?: string;
    ai_summary?: string;
    [key: string]: unknown;
  };
}

export interface ResourceServer {
  host: string;
  type?: string;
  cpu_used?: number;
  mem_used?: number;
  disk_used_max?: number;
  health_score?: number;
}

export interface SlaBreach {
  job_name?: string;
  job?: string;
  status?: string;
  run_hrs?: number;
  breach_margin_hrs?: number;
}

export type DashboardPayload = Record<string, unknown>;

export interface AzureStatus {
  configured: boolean;
  authenticated?: boolean;
  [key: string]: unknown;
}

export const getApiBaseUrl = (): string => {
  const configured = (window.env.API_BASE_URL || '').trim();
  if (configured) {
    return configured.replace(/\/$/, '');
  }
  return window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost'
    ? 'http://127.0.0.1:8765'
    : '';
};

const readError = async (response: Response): Promise<string> => {
  try {
    const body = await response.json();
    if (typeof body?.detail === 'string') {
      return body.detail;
    }
    if (typeof body?.message === 'string') {
      return body.message;
    }
  } catch {
    // Fall back to the HTTP status when the backend did not return JSON.
  }
  return `Request failed (${response.status} ${response.statusText || 'unknown error'})`;
};

const request = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.headers || {}),
    },
  });

  if (!response.ok) {
    throw new Error(await readError(response));
  }

  return response.json() as Promise<T>;
};

export const getAuditContext = (): Promise<AuditContext> => request<AuditContext>('/api/audit-context');

export const uploadDashboardFile = (file: File): Promise<SmartUploadResponse> => {
  const formData = new FormData();
  formData.append('file', file);
  return request<SmartUploadResponse>('/api/smart-upload', {
    method: 'POST',
    body: formData,
  });
};

export const postDashboardPayload = <T = DashboardPayload>(path: string, payload: DashboardPayload): Promise<T> =>
  request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

export const processBatch = (file: File): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  return request<DashboardPayload>('/api/process-batch', { method: 'POST', body: formData });
};

export const processBatchMulti = (files: File[]): Promise<DashboardPayload> => {
  const formData = new FormData();
  files.forEach((file) => formData.append('files', file));
  return request<DashboardPayload>('/api/process-batch/multi', { method: 'POST', body: formData });
};

export const uploadSlaMatrix = (file: File, slaMode = 'daily'): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('sla_mode', slaMode);
  return request<DashboardPayload>('/api/sla-matrix', { method: 'POST', body: formData });
};

export const getConfig = (): Promise<DashboardPayload> => request<DashboardPayload>('/api/config');

export const updateConfig = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/config', payload);

export const clearSession = (): Promise<DashboardPayload> =>
  postDashboardPayload('/api/clear-session', {});

export const getReportArchive = (): Promise<DashboardPayload> => request<DashboardPayload>('/api/report-archive');

export const getSowBaseline = (): Promise<DashboardPayload> => request<DashboardPayload>('/api/sow/baseline');

export const saveSowBaseline = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/sow/baseline', payload);

export const deleteSowBaseline = (): Promise<DashboardPayload> =>
  request<DashboardPayload>('/api/sow/baseline', { method: 'DELETE' });

export const getSowSlaWindows = (): Promise<DashboardPayload> => request<DashboardPayload>('/api/sow/sla-windows');

export const getSowProductTaxonomy = (): Promise<DashboardPayload> =>
  request<DashboardPayload>('/api/sow/product-taxonomy');

export const generateFindings = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/generate-findings', payload);

export const getRedFlags = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/red-flags', payload);

export const getExecutiveDashboard = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/executive-dashboard', payload);

export const compareSow = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/sow/compare', payload);

export const parseSow = (file: File): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  return request<DashboardPayload>('/api/sow/parse', { method: 'POST', body: formData });
};

export const uploadBenchmark = (file: File): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  return request<DashboardPayload>('/api/benchmark', { method: 'POST', body: formData });
};

export const getAzureStatus = (): Promise<AzureStatus> => request<AzureStatus>('/api/azure/status');

export const getAzureAuthStatus = (): Promise<DashboardPayload> => request<DashboardPayload>('/api/azure/auth-status');

export const connectAzure = (): Promise<DashboardPayload> =>
  request<DashboardPayload>('/api/azure/browser-login', { method: 'POST' });

export const disconnectAzure = (): Promise<DashboardPayload> =>
  request<DashboardPayload>('/api/azure/browser-logout', { method: 'POST' });

export const getAzureSubscriptions = (): Promise<DashboardPayload> =>
  request<DashboardPayload>('/api/azure/subscriptions');

export const getAzureResourceGroups = (subscriptionId: string): Promise<DashboardPayload> =>
  request<DashboardPayload>(`/api/azure/resource-groups?subscription_id=${encodeURIComponent(subscriptionId)}`);

export const discoverAzureVms = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/azure/discover-vms', payload);

export const fetchAzureResources = (payload: DashboardPayload = {}): Promise<DashboardPayload> =>
  postDashboardPayload('/api/azure/fetch-resources', payload);

export const fetchAzureTimeseries = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/azure/timeseries', payload);

export const processResource = (servers: ResourceServer[]): Promise<DashboardPayload> =>
  postDashboardPayload('/api/process-resource', { servers });

export const exportReport = (payload: DashboardPayload): Promise<Blob> =>
  fetch(`${getApiBaseUrl()}/api/export-report`, {
    method: 'POST',
    headers: { Accept: 'text/html', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(async (response) => {
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    return response.blob();
  });