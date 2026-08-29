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

const finiteNumber = (value: unknown): number | null => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

/**
 * Preferred contract: `/api/batch-sla/upload` returns `sla_matrix` directly.
 * This fallback keeps a locally running older FastAPI process usable while the
 * React MFE has already been updated. It uses only the returned workbook rows;
 * it never asks Ctrl-M for a runtime and never invents an observation.
 */
export const workbookSlaSnapshotFromUpload = (payload: DashboardPayload): DashboardPayload => {
  const supplied = payload.sla_matrix;
  // When raw workbook rows accompany the response, rebuild the snapshot from
  // them. This prevents a locally running older FastAPI process from returning
  // a stale pre-fix `sla_matrix` that suppresses a supplied Current end time.
  if (supplied && typeof supplied === 'object' && !Array.isArray(supplied) && !Array.isArray(payload.workflows)) return supplied as DashboardPayload;

  const rawWorkflows = Array.isArray(payload.workflows) ? payload.workflows : [];
  let observed = 0;
  const counts: Record<string, number> = { OK: 0, LONG_JOB: 0, AT_RISK: 0, NO_BUFFER: 0, BREACH: 0 };
  const workflowSummary = rawWorkflows.map((item) => {
    const row = (item && typeof item === 'object' ? item : {}) as DashboardPayload;
    const contractConflict = row.contract_conflict === true || String(row.sla_source || '').toUpperCase() === 'CONTRACT_CONFLICT';
    const declared = String(row.sla_source || '').toUpperCase() === 'BATCH_SLA_XLSX';
    const sla = declared ? finiteNumber(row.sla_hours) : null;
    const endEqualsTarget = row.runtime_source_caveat === 'REPORTED_END_EQUALS_TARGET'
      || row.reported_end_equals_target === true;
    const candidateRuntime = finiteNumber(row.last_run_hours_xlsx);
    const runtime = sla != null && candidateRuntime != null ? candidateRuntime : null;
    const measured = runtime != null && sla != null && sla > 0;
    let status: string;
    let reasonCode: string;
    let reasonDetail: string;
    if (measured) {
      observed += 1;
      status = String(row.compliance || 'UNKNOWN');
      if (counts[status] != null) counts[status] += 1;
      reasonCode = endEqualsTarget ? 'REPORTED_END_EQUALS_TARGET' : 'WORKBOOK_REPORTED_COMPLETION';
      reasonDetail = endEqualsTarget
        ? 'Duration is calculated from supplied Start Time and Current end time. Current end equals Expected End; verify the source completion value.'
        : 'Duration is calculated from workbook timing returned by the BatchSLA upload.';
    } else if (contractConflict) {
      status = 'SLA_CONTRACT_CONFLICT';
      reasonCode = 'CLOCK_DURATION_CONFLICT';
      reasonDetail = String(row.contract_conflict_detail || 'Workbook clock-window and declared Duration values conflict; no SLA was selected.');
    } else if (!declared) {
      status = 'SLA_MISSING';
      reasonCode = 'SLA_NOT_DECLARED_IN_WORKBOOK';
      reasonDetail = 'This workbook does not declare an SLA target for this row.';
    } else if (row.runtime_is_placeholder === true) {
      status = 'NOT_OBSERVED';
      reasonCode = 'PLACEHOLDER_CURRENT_END';
      reasonDetail = 'Current end equals the contractual target and is not treated as an observed completion.';
    } else {
      status = 'NOT_OBSERVED';
      reasonCode = 'COMPLETION_NOT_REPORTED';
      reasonDetail = 'The workbook supplies the contract but not a distinct reported completion.';
    }
    const buffer = measured && sla != null && runtime != null ? ((sla - runtime) / sla) * 100 : null;
    return {
      workflow_name: row.workflow,
      workflow_key: row.workflow,
      batch_type: row.batch_type,
      sla_h: sla,
      sla_source: contractConflict ? 'batch_sla_xlsx_conflict' : declared ? 'batch_sla_xlsx' : 'global',
      runtime_h: runtime,
      buffer_pct: buffer,
      duration_headroom_mins: measured && sla != null && runtime != null ? Math.round((sla - runtime) * 60) : null,
      status,
      workbook_timing_source: row.workbook_timing_source || (endEqualsTarget ? 'WORKBOOK_REPORTED_CURRENT_END_EQUALS_TARGET' : measured ? 'WORKBOOK_REPORTED_COMPLETION' : 'WORKBOOK_COMPLETION_NOT_REPORTED'),
      workbook_start_time: row.workbook_start_time || row.sla_start_time,
      workbook_expected_end: row.workbook_expected_end || row.sla_end_time,
      workbook_reported_end: row.workbook_reported_end,
      workbook_clock_window_hours: finiteNumber(row.workbook_clock_window_hours),
      workbook_contract_duration_hours: finiteNumber(row.workbook_contract_duration_hours),
      runtime_source_caveat: endEqualsTarget ? 'REPORTED_END_EQUALS_TARGET' : row.runtime_source_caveat,
      contract_conflict: contractConflict,
      contract_conflict_detail: row.contract_conflict_detail,
      measurement_reason_code: reasonCode,
      measurement_reason_detail: reasonDetail,
    };
  });
  const declared = workflowSummary.map((row) => finiteNumber(row.sla_h)).filter((value): value is number => value != null && value > 0);
  const compliance = observed ? 100 * (counts.OK + counts.LONG_JOB + counts.AT_RISK + counts.NO_BUFFER) / observed : null;
  return {
    workbook_only: true,
    filename: payload.filename,
    total_jobs: workflowSummary.length,
    total_runs: observed,
    observed_workflow_count: observed,
    not_observed_workflow_count: workflowSummary.length - observed,
    compliance_pct: compliance,
    window_day_compliance_pct: compliance,
    ok_runs: counts.OK,
    long_job_runs: counts.LONG_JOB,
    at_risk_runs: counts.AT_RISK,
    no_buffer_runs: counts.NO_BUFFER,
    breaching_runs: counts.BREACH,
    failed_runs: 0,
    explicit_sla_matrix: declared.length > 0,
    sla_limit_hrs: declared.length ? Math.max(...declared) : null,
    sla_label: 'Workbook-declared SLA',
    workflow_summary: workflowSummary,
    job_summary: [], breaches: [], outliers: [], resource_linked: [],
    batch_sla_mapping_report: payload.mapping_report || {},
  };
};

export interface AzureStatus {
  configured: boolean;
  authenticated?: boolean;
  [key: string]: unknown;
}

export const getApiBaseUrl = (): string => {
  const isLocalPage = window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost';
  const configured = (window.env.API_BASE_URL || '').trim();
  if (configured) {
    // Older local env.js files used 127.0.0.1 while CRA opens localhost.  Keep
    // the endpoint's port but use the page host so an existing dev session
    // cannot split the Azure pe_sid cookie across two browser sites.
    try {
      const configuredUrl = new URL(configured);
      const configuredIsLocal = configuredUrl.hostname === '127.0.0.1' || configuredUrl.hostname === 'localhost';
      if (isLocalPage && configuredIsLocal && configuredUrl.hostname !== window.location.hostname) {
        return `${configuredUrl.protocol}//${window.location.hostname}${configuredUrl.port ? `:${configuredUrl.port}` : ''}`;
      }
    } catch {
      // Preserve the supplied value below; request() will surface a clear error.
    }
    return configured.replace(/\/$/, '');
  }
  // Keep the API hostname aligned with the page hostname in local mode.
  // `localhost` and `127.0.0.1` are different browser sites: mixing them
  // prevents the pe_sid Azure session cookie from surviving login → discovery.
  if (isLocalPage) {
    return `http://${window.location.hostname}:8765`;
  }
  return '';
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
    // dev server to its same-host local API (for example localhost:8765).
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
/**
 * The additional byte arguments deliberately remain optional.  Existing
 * callers that only need a percentage retain their current contract, while
 * Upload & Intake can show the same loaded/total detail as the local app.
 */
export type UploadProgressHandler = (pct: number, loaded?: number, total?: number) => void;

const requestWithProgress = <T>(path: string, formData: FormData, onProgress?: UploadProgressHandler): Promise<T> =>
  new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${getApiBaseUrl()}${path}`);
    xhr.withCredentials = true;
    xhr.responseType = 'json';
    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100), event.loaded, event.total);
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        resolve((xhr.response ?? {}) as T);
      } else {
        const detail = xhr.response?.detail || xhr.response?.message;
        const message = typeof detail === 'string'
          ? detail
          : typeof detail?.message === 'string'
            ? detail.message
            : `Request failed (${xhr.status})`;
        reject(new Error(message));
      }
    };
    xhr.onerror = () => reject(new Error('Network error during upload.'));
    xhr.send(formData);
  });

export const uploadDashboardFile = (file: File, onProgress?: UploadProgressHandler): Promise<SmartUploadResponse> => {
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

export const processBatchMulti = (files: File[], onProgress?: UploadProgressHandler): Promise<DashboardPayload> => {
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
export const uploadBatchSlaXlsx = (file: File, onProgress?: UploadProgressHandler): Promise<DashboardPayload> => {
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

/** Current engagement SOW state.  Used to rehydrate routes after navigation. */
export const getSowState = (): Promise<DashboardPayload> => request<DashboardPayload>('/api/sow/state');

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

/** The populated four-section PE review shown by the local FastAPI dashboard.
 * The backend owns the calculations and text; the MFE only renders this
 * evidence response. */
export const getPeNarrative = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/pe-narrative', payload);

export const getExecutiveDashboard = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/executive-dashboard', payload);

export const getFinalJudgment = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/final-judgment', payload);

export const compareSow = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/sow/compare', payload);

export const parseSow = (file: File, onProgress?: UploadProgressHandler): Promise<DashboardPayload> => {
  const formData = new FormData();
  formData.append('file', file);
  return requestWithProgress<DashboardPayload>('/api/sow/parse', formData, onProgress);
};

export const uploadBenchmark = (file: File, onProgress?: UploadProgressHandler): Promise<DashboardPayload> => {
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

export interface AzureFetchProgress {
  phase?: string;
  done?: number;
  total?: number;
}

/**
 * Fetch Azure metrics with server-sent progress.  A 15/30-day selection is a
 * real remote Azure Monitor operation, not a local calculation; using the
 * streaming route keeps the reviewer informed instead of displaying a frozen
 * spinner until every VM has returned.
 */
export const fetchAzureResourcesWithProgress = async (
  payload: DashboardPayload = {},
  onProgress?: (progress: AzureFetchProgress) => void,
): Promise<DashboardPayload> => {
  const response = await fetch(`${getApiBaseUrl()}/api/azure/fetch-resources-stream`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    let detail = `Azure fetch failed (${response.status}).`;
    try {
      const error = await response.json();
      detail = String(error?.detail || detail);
    } catch { /* retain the HTTP status message */ }
    throw new Error(detail);
  }
  if (!response.body) {
    // Defensive fallback for an older browser/runtime that does not expose a
    // readable response stream.  The calculation path remains unchanged.
    return fetchAzureResources(payload);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let result: DashboardPayload | null = null;

  const processEvent = (rawEvent: string) => {
    const event = rawEvent.match(/^event:\s*(.+)$/m)?.[1]?.trim();
    const rawData = rawEvent.match(/^data:\s*(.+)$/m)?.[1];
    if (!event || !rawData) return;
    let data: DashboardPayload;
    try {
      data = JSON.parse(rawData) as DashboardPayload;
    } catch {
      return;
    }
    if (event === 'progress') {
      onProgress?.(data as AzureFetchProgress);
    } else if (event === 'result') {
      result = data;
    } else if (event === 'error') {
      throw new Error(String(data.detail || 'Azure fetch failed.'));
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    let separator = buffer.indexOf('\n\n');
    while (separator >= 0) {
      processEvent(buffer.slice(0, separator));
      buffer = buffer.slice(separator + 2);
      separator = buffer.indexOf('\n\n');
    }
    if (done) break;
  }
  if (buffer.trim()) processEvent(buffer);
  if (!result) throw new Error('Azure Monitor finished without a result payload.');
  return result;
};

export const fetchAzureTimeseries = (payload: DashboardPayload): Promise<DashboardPayload> =>
  postDashboardPayload('/api/azure/timeseries', payload);

export const processResource = (servers: ResourceServer[]): Promise<DashboardPayload> =>
  postDashboardPayload('/api/process-resource', { servers });

export interface ExportReportResult {
  blob: Blob;
  /** FastAPI writes this only after it attempts the frozen Review Registry save. */
  archiveStatus: 'saved' | 'skipped' | 'failed' | 'unknown';
}

export const exportReportWithStatus = async (payload: DashboardPayload): Promise<ExportReportResult> => {
  const response = await fetch(`${getApiBaseUrl()}/api/export-report`, {
    method: 'POST',
    credentials: 'include',
    headers: { Accept: 'text/html', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await readError(response));
  const archiveStatus = response.headers.get('X-Archive-Status');
  return {
    blob: await response.blob(),
    archiveStatus: archiveStatus === 'saved' || archiveStatus === 'skipped' || archiveStatus === 'failed' ? archiveStatus : 'unknown',
  };
};

/** Compatibility for callers that only need a downloaded blob. */
export const exportReport = async (payload: DashboardPayload): Promise<Blob> =>
  (await exportReportWithStatus(payload)).blob;

export interface SessionRestorePayload {
  batch?: DashboardPayload | null;
  resource?: DashboardPayload | null;
  sla_matrix?: DashboardPayload | null;
  benchmark?: DashboardPayload | null;
  findings?: DashboardPayload | null;
  red_flags?: DashboardPayload | null;
  pe_narrative?: DashboardPayload | null;
  executive?: DashboardPayload | null;
  final_judgment?: DashboardPayload | null;
  customer_name?: string | null;
  reviewed_products?: string[];
}

/** Session-scoped cached payloads used to rebuild the MFE after a browser refresh. */
export const getSessionRestore = (): Promise<SessionRestorePayload> =>
  request<SessionRestorePayload>('/api/session/restore');
