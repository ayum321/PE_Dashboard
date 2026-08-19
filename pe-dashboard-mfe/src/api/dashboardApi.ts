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

const getApiBaseUrl = (): string => (window.env.API_BASE_URL || '').replace(/\/$/, '');

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