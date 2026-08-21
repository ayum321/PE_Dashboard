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
    // Required so the pe_sid session cookie (Azure credential cache, batch
    // session cache, etc.) is sent on cross-origin requests from the MFE
    // dev server (localhost:3000) to the API (127.0.0.1:8765).
    credentials: 'include',
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

/** Same contract as request(), but uses XMLHttpRequest instead of fetch so
 * real upload-progress events are available (fetch has no upload progress
 * API) — drives the percentage shown by UploadTile's progress bar. */
const requestWithProgress = <T>(path: string, formData: FormData, onProgress?: (pct: number) => void): Promise<T> =>
  new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${getApiBaseUrl()}${path}`);
    xhr.withCredentials = true;
    xhr.responseType = 'json';
    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        resolve((xhr.response ?? {}) as T);
      } else {
        const detail = xhr.response?.detail || xhr.response?.message;
        reject(new Error(typeof detail === 'string' ? detail : `Request failed (${xhr.status})`));
      }
    };
    xhr.onerror = () => reject(new Error('Network error during upload.'));
    xhr.send(formData);
  });

export const uploadDashboardFile = (file: File, onProgress?: (pct: number) => void): Promise<SmartUploadResponse> => {
  const formData = new FormData();
  formData.append('file', file);
  return requestWithProgress<SmartUploadResponse>('/api/smart-upload', formData, onProgress);
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

export const processBatchMulti = (files: File[], onProgress?: (pct: number) => void): Promise<DashboardPayload> => {
  const formData = new FormData();
  files.forEach((file) => formData.append('files', file));
  return requestWithProgress<DashboardPayload>('/api/process-batch/multi', formData, onProgress);
};

export const uploadSlaMatrix = (file: File, slaMode = 'daily'): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('sla_mode', slaMode);
  return request<DashboardPayload>('/api/sla-matrix', { method: 'POST', body: formData });
};

/** Upload the customer's per-workflow SLA contract (BatchSLA_info.xlsx) — the
 * Tier-1 source every batch SLA calculation reads. Distinct from
 * uploadSlaMatrix() above, which POSTs a Ctrl-M file to /api/sla-matrix for a
 * separate flat-custom-ceiling comparison view (SLA Matrix tab). Confusing
 * two different backend features by name was the root cause of Batch Review
 * never recomputing after a "Workflow SLA Info" upload. */
export const uploadBatchSlaXlsx = (file: File, onProgress?: (pct: number) => void): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  return requestWithProgress<DashboardPayload>('/api/batch-sla/upload', formData, onProgress);
};

/** Re-run batch KPIs from the cached Ctrl-M rows with current manual job exclusions. */
export const refreshBatch = (manualExclusions: { name: string; reason: string }[] = []): Promise<DashboardPayload> =>
  postDashboardPayload('/api/batch/refresh', { manual_exclusions: manualExclusions });

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

export const getReviewedProducts = (): Promise<DashboardPayload> =>
  request<DashboardPayload>('/api/sow/reviewed-products');

export const saveReviewedProducts = (products: string[]): Promise<DashboardPayload> =>
  postDashboardPayload('/api/sow/reviewed-products', { products });

export const generateFindings = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/generate-findings', payload);

export const getRedFlags = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/red-flags', payload);

export const getExecutiveDashboard = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/executive-dashboard', payload);

export const compareSow = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/sow/compare', payload);

export const parseSow = (file: File, onProgress?: (pct: number) => void): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  return requestWithProgress<DashboardPayload>('/api/sow/parse', formData, onProgress);
};

export const uploadBenchmark = (file: File, onProgress?: (pct: number) => void): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  return requestWithProgress<DashboardPayload>('/api/benchmark', formData, onProgress);
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

export const searchAzureVms = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/azure/search-vms', payload);

export const fetchAzureResources = (payload: DashboardPayload = {}): Promise<DashboardPayload> =>
  postDashboardPayload('/api/azure/fetch-resources', payload);

export const fetchAzureTimeseries = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/azure/timeseries', payload);

export const processResource = (servers: ResourceServer[]): Promise<DashboardPayload> =>
  postDashboardPayload('/api/process-resource', { servers });

export const exportReport = (payload: DashboardPayload): Promise<Blob> =>
  fetch(`${getApiBaseUrl()}/api/export-report`, {
    method: 'POST',
    credentials: 'include',
    headers: { Accept: 'text/html', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(async (response) => {
    if (!response.ok) {
      throw new Error(await readError(response));
    }
    return response.blob();
  });