/**
 * Typed API client.
 *
 * All state lives on the backend. This module does no caching and no derived
 * financial calculation — every figure shown in the UI is one the backend computed
 * and can cite, which is the point of the product.
 */

import type {
  AnomalyFinding,
  AnomalyPrecision,
  AnomalyRun,
  AuditResponse,
  ChangeImpact,
  ClassifiedItem,
  ContractCitation,
  ContractRow,
  ContractRun,
  ContractUploadResult,
  CriticDecisionRow,
  CriticRun,
  DiligenceRoomData,
  EvidenceResponse,
  EvidenceTrace,
  HealthStatus,
  IngestionRun,
  IngestionStats,
  MatchProposal,
  PipelineRun,
  PipelineState,
  QuarantineResponse,
  ReconciliationRun,
  ReportVersionRow,
  ResolutionRun,
  ResolvedCustomer,
  RevenueRun,
  ReviewQueue,
  TokenResponse,
  TraceEvent,
  Workspace,
  WorkspaceSummary,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

const TOKEN_KEY = "revenueproof.token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Turn FastAPI's validation-error shape into something readable in the UI. */
function describeError(status: number, body: unknown): string {
  if (typeof body === "string") return body;
  if (body && typeof body === "object") {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          const loc = Array.isArray(item?.loc)
            ? item.loc.filter((p: unknown) => p !== "body").join(".")
            : "";
          return loc ? `${loc}: ${item.msg}` : item.msg;
        })
        .join("; ");
    }
    const error = (body as { error?: string }).error;
    if (error) return error;
  }
  return `Request failed with status ${status}`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch {
    // A dead backend is the most common local failure; say so plainly rather
    // than surfacing an opaque "Failed to fetch".
    throw new ApiError(0, `Cannot reach the API at ${API_BASE}. Is the backend running?`);
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const body = text ? JSON.parse(text) : null;

  if (!response.ok) {
    if (response.status === 401) setToken(null);
    throw new ApiError(response.status, describeError(response.status, body), body);
  }
  return body as T;
}

/**
 * The filename the server chose, which is the only one that knows what is in the
 * file. Reads RFC 5987 `filename*=` first, then the plain `filename=`.
 *
 * The client used to build its own name from whatever string a component happened
 * to pass, so a missing prop produced `revenueproof-undefined.html` and, when the
 * anchor's `download` attribute did not take, the browser fell back to naming the
 * file after the blob URL — a UUID, which is what "random numbers" in a download
 * folder actually is. The server already names every artefact after the company,
 * the table and the date; the client's job is to stop overriding it.
 */
function filenameFrom(response: Response, fallback: string): string {
  const header = response.headers.get("Content-Disposition") ?? "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim().replace(/"/g, ""));
    } catch {
      // A malformed header is not worth failing a download over.
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : fallback;
}

/** Save a fetched blob to disk without ever putting a token in a URL. */
function saveBlob(blob: Blob, filename: string): void {
  // `application/octet-stream` stops the browser deciding it can render the file
  // itself. An HTML report handed over as `text/html` is exactly the case where
  // Chrome may open a tab instead of saving, which is what made the report look
  // like "just HTML" on screen rather than a file in Downloads.
  const url = URL.createObjectURL(
    new Blob([blob], { type: "application/octet-stream" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  // Removing the anchor in the same tick can cancel the download before the
  // browser has read the `download` attribute. Both cleanups wait a tick.
  setTimeout(() => {
    link.remove();
    URL.revokeObjectURL(url);
  }, 1500);
}

/** Fetch with the bearer header, then save under the server's own filename. */
async function downloadWithAuth(path: string, fallback: string): Promise<string> {
  const token = getToken();
  const response = await fetch(`${API_BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    let detail = "";
    try {
      detail = ((await response.json()) as { detail?: string }).detail ?? "";
    } catch {
      // A non-JSON error body is not more informative than the status.
    }
    throw new ApiError(
      response.status,
      detail || `Could not build the download (${response.status})`,
    );
  }
  const filename = filenameFrom(response, fallback);
  saveBlob(await response.blob(), filename);
  return filename;
}

export const api = {
  health: () => request<HealthStatus>("/health"),

  register: (email: string, password: string, fullName?: string) =>
    request<TokenResponse>("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, full_name: fullName || null }),
    }),

  login: (email: string, password: string) =>
    request<TokenResponse>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  me: () => request<{ id: string; email: string; full_name: string | null }>("/api/v1/auth/me"),

  listWorkspaces: () => request<Workspace[]>("/api/v1/workspaces"),

  createWorkspace: (payload: Record<string, unknown>) =>
    request<Workspace>("/api/v1/workspaces", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  getWorkspace: (id: string) => request<Workspace>(`/api/v1/workspaces/${id}`),

  /** Pipeline state, workspace summary and the review queue in one round trip. */
  workspaceOverview: (id: string) =>
    request<{
      summary: WorkspaceSummary;
      pipeline: PipelineState;
      review: ReviewQueue;
    }>(`/api/v1/workspaces/${id}/overview`),

  workspaceSummary: (id: string) =>
    request<WorkspaceSummary>(`/api/v1/workspaces/${id}/summary`),

  deleteWorkspace: (id: string) =>
    request<void>(`/api/v1/workspaces/${id}`, { method: "DELETE" }),

  recentEvents: (id: string) =>
    request<{ events: TraceEvent[] }>(`/api/v1/workspaces/${id}/events`),

  auditLog: (id: string) => request<AuditResponse>(`/api/v1/workspaces/${id}/audit`),

  // --- Feature 1: evidence ingestion ------------------------------------
  runIngestion: (id: string, useDemoData = false, datasetSeed?: string) =>
    request<IngestionRun>(`/api/v1/workspaces/${id}/ingest`, {
      method: "POST",
      body: JSON.stringify({
        include_bank_sample: true,
        use_demo_data: useDemoData,
        // A seed asks for a generated roster: the same adversarial cases under
        // companies the fixture has never contained.
        dataset_seed: datasetSeed ?? null,
      }),
    }),

  // Tokens are encrypted before storage by the backend and are never included in
  // any prompt sent to the language model.
  connectSource: (
    id: string,
    sourceSystem: string,
    accessToken: string,
    externalAccountId?: string,
  ) =>
    request<{ source_system: string; is_active: boolean; is_synthetic: boolean }>(
      `/api/v1/workspaces/${id}/connections`,
      {
        method: "POST",
        body: JSON.stringify({
          source_system: sourceSystem,
          access_token: accessToken,
          external_account_id: externalAccountId || null,
          is_test_mode: true,
        }),
      },
    ),

  listEvidence: (id: string) =>
    request<EvidenceResponse>(`/api/v1/workspaces/${id}/evidence`),

  evidenceLineage: (id: string, sourceId: string) =>
    request<{ source_id: string; versions: Record<string, unknown>[] }>(
      `/api/v1/workspaces/${id}/evidence/${encodeURIComponent(sourceId)}/lineage`,
    ),

  quarantine: (id: string) =>
    request<QuarantineResponse>(`/api/v1/workspaces/${id}/quarantine`),

  // --- Feature 2: identity resolution -----------------------------------
  resolveIdentities: (id: string, useCritic: boolean) =>
    request<ResolutionRun>(`/api/v1/workspaces/${id}/identity/resolve`, {
      method: "POST",
      body: JSON.stringify({ use_critic: useCritic }),
    }),

  listResolvedCustomers: (id: string) =>
    request<{ customers: ResolvedCustomer[] }>(
      `/api/v1/workspaces/${id}/identity/customers`,
    ),

  listMatches: (id: string, decision?: string) =>
    request<{ counts: Record<string, number>; matches: MatchProposal[] }>(
      `/api/v1/workspaces/${id}/identity/matches` +
        (decision ? `?decision=${decision}` : ""),
    ),

  decideMatch: (id: string, matchId: string, decision: string, reason: string) =>
    request<{ id: string; decision: string; remembered: boolean }>(
      `/api/v1/workspaces/${id}/identity/matches/${matchId}/decide`,
      { method: "POST", body: JSON.stringify({ decision, reason }) },
    ),

  // --- Feature 3: contract intelligence ---------------------------------
  processContracts: (id: string) =>
    request<ContractRun>(`/api/v1/workspaces/${id}/contracts/process`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  listContracts: (id: string) =>
    request<{ contracts: ContractRow[] }>(`/api/v1/workspaces/${id}/contracts`),

  contractCitations: (id: string, contractId: string) =>
    request<{ document_name: string; citations: ContractCitation[] }>(
      `/api/v1/workspaces/${id}/contracts/${contractId}/citations`,
    ),

  // --- Feature 4: cash reconciliation -----------------------------------
  reconcile: (id: string) =>
    request<ReconciliationRun>(`/api/v1/workspaces/${id}/reconcile`, {
      method: "POST",
    }),

  // The stored position, so reopening the page shows what was already reconciled
  // instead of inviting the reader to reconcile a workspace that already is.
  reconciliation: (id: string) =>
    request<ReconciliationRun & { reconciled: boolean }>(
      `/api/v1/workspaces/${id}/reconciliation`,
    ),

  // --- Feature 5: revenue truth -----------------------------------------
  // The stored position, so reopening the page still states claimed against verified.
  revenueSummary: (id: string) =>
    request<RevenueRun & { verified: boolean }>(
      `/api/v1/workspaces/${id}/revenue/summary`,
    ),

  verifyRevenue: (id: string) =>
    request<RevenueRun>(`/api/v1/workspaces/${id}/revenue/verify`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  listRevenueItems: (id: string) =>
    request<{ items: ClassifiedItem[] }>(`/api/v1/workspaces/${id}/revenue/items`),

  // --- Feature 6: anomaly detection --------------------------------------
  scanAnomalies: (id: string, useLlm = true) =>
    request<AnomalyRun>(`/api/v1/workspaces/${id}/anomalies/scan`, {
      method: "POST",
      body: JSON.stringify({ use_llm: useLlm }),
    }),

  listAnomalies: (id: string) =>
    request<{ anomalies: AnomalyFinding[]; disclaimer: string }>(
      `/api/v1/workspaces/${id}/anomalies`,
    ),

  anomalyPrecision: (id: string) =>
    request<AnomalyPrecision>(`/api/v1/workspaces/${id}/anomalies/precision`),

  // The reviewer's verdict on one finding. This is the only label the precision
  // measurement and the model gate have to work from.
  // --- Feature 8: diligence room -----------------------------------------
  diligenceRoom: (id: string) =>
    request<DiligenceRoomData>(`/api/v1/workspaces/${id}/room`),

  traceEvidence: (id: string, itemId: string) =>
    request<EvidenceTrace>(`/api/v1/workspaces/${id}/room/trace/${itemId}`),

  detectChanges: (id: string, days = 365) =>
    request<ChangeImpact>(`/api/v1/workspaces/${id}/room/changes?days=${days}`),

  rerunAffected: (id: string, force = false) =>
    request<{ ran: string[]; skipped: string; version: { version: number; explanation: string } }>(
      `/api/v1/workspaces/${id}/room/rerun`,
      { method: "POST", body: JSON.stringify({ days: 365, force, use_llm: true }) },
    ),

  roomVersions: (id: string) =>
    request<{ versions: ReportVersionRow[]; total: number }>(
      `/api/v1/workspaces/${id}/room/versions`,
    ),

  publishVersion: (id: string, force = false) =>
    request<{ version: number; created: boolean; explanation: string }>(
      `/api/v1/workspaces/${id}/room/publish`,
      { method: "POST", body: JSON.stringify({ force }) },
    ),

  // --- Feature 7: maker-checker ------------------------------------------
  runCritic: (id: string, useLlm = true) =>
    request<CriticRun>(`/api/v1/workspaces/${id}/critic/run`, {
      method: "POST",
      body: JSON.stringify({ use_llm: useLlm }),
    }),

  listCriticDecisions: (id: string) =>
    request<{ decisions: CriticDecisionRow[]; note: string }>(
      `/api/v1/workspaces/${id}/critic`,
    ),

  // --- Feature 7: review queue -------------------------------------------
  listReview: (id: string, status = "open") =>
    request<ReviewQueue>(`/api/v1/workspaces/${id}/review?status=${status}`),

  claimReview: (id: string, itemId: string) =>
    request<{ id: string; status: string }>(
      `/api/v1/workspaces/${id}/review/${itemId}/claim`,
      { method: "POST" },
    ),

  resolveReview: (id: string, itemId: string, decision: string, reason: string) =>
    request<{ id: string; status: string; resolution: string; summary: ReviewQueue["summary"] }>(
      `/api/v1/workspaces/${id}/review/${itemId}/resolve`,
      { method: "POST", body: JSON.stringify({ decision, reason }) },
    ),

  // Downloads are fetched with the auth header and saved from a blob rather than
  // opened as a URL. A token in a query string ends up in browser history, in the
  // referrer, and in every proxy log between here and the server; a bearer header
  // does not.
  downloadReport: (id: string) =>
    downloadWithAuth(`/api/v1/workspaces/${id}/report`, "revenueproof-report.html"),

  /** What this workspace can hand over, so the UI renders buttons for real files. */
  listDownloads: (id: string) =>
    request<{ artifacts: { key: string; label: string; format: string }[] }>(
      `/api/v1/workspaces/${id}/downloads`,
    ),

  /** Everything at once: the report, every table as CSV, and a README. */
  downloadBundle: (id: string) =>
    downloadWithAuth(
      `/api/v1/workspaces/${id}/downloads/bundle`,
      "revenueproof-full-export.zip",
    ),

  /** One named table, or the report on its own. */
  downloadArtifact: (id: string, key: string) =>
    downloadWithAuth(
      `/api/v1/workspaces/${id}/downloads/${key}`,
      `revenueproof-${key}.csv`,
    ),

  downloadDemoDataset: async (seed?: string) => {
    const response = await fetch(
      `${API_BASE}/api/v1/demo-dataset${seed ? `?seed=${encodeURIComponent(seed)}` : ""}`,
    );
    if (!response.ok) {
      throw new ApiError(response.status, `Could not build the dataset (${response.status})`);
    }
    saveBlob(
      await response.blob(),
      filenameFrom(response, `revenueproof-demo-${seed ?? "template"}.zip`),
    );
  },

  anomalyFeedback: (id: string, anomalyId: string, isFalsePositive: boolean) =>
    request<{ id: string; status: string; is_false_positive: boolean; precision: AnomalyPrecision }>(
      `/api/v1/workspaces/${id}/anomalies/${anomalyId}/feedback`,
      { method: "POST", body: JSON.stringify({ is_false_positive: isFalsePositive }) },
    ),

  // --- Running the agents ------------------------------------------------
  pipelineState: (id: string) =>
    request<PipelineState>(`/api/v1/workspaces/${id}/pipeline`),

  runPipeline: (id: string, stages?: string[]) =>
    request<PipelineRun>(`/api/v1/workspaces/${id}/pipeline/run`, {
      method: "POST",
      body: JSON.stringify(stages ? { stages } : {}),
    }),

  uploadContracts: (id: string, files: File[]) => {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    // No Content-Type header: the browser must set the multipart boundary itself.
    return request<ContractUploadResult>(
      `/api/v1/workspaces/${id}/contracts/upload`,
      { method: "POST", body: form },
    );
  },

  uploadBankCsv: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    // No Content-Type header: the browser must set the multipart boundary itself.
    return request<IngestionStats>(`/api/v1/workspaces/${id}/bank-csv`, {
      method: "POST",
      body: form,
    });
  },
};

/**
 * Open the live trace socket for a workspace.
 *
 * The token goes in the query string because browsers cannot set an
 * Authorization header on a WebSocket handshake; the backend still verifies it
 * and checks workspace membership before streaming anything.
 */
export function openTraceSocket(
  workspaceId: string,
  onEvent: (event: TraceEvent) => void,
  onStatus?: (status: "connecting" | "open" | "closed") => void,
): () => void {
  const token = getToken();
  if (!token) return () => {};

  const wsBase = API_BASE.replace(/^http/, "ws");
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let closedByCaller = false;
  let attempt = 0;

  const connect = () => {
    if (closedByCaller) return;
    onStatus?.("connecting");
    socket = new WebSocket(
      `${wsBase}/api/v1/events/ws/${workspaceId}?token=${encodeURIComponent(token)}`,
    );

    socket.onopen = () => {
      attempt = 0;
      onStatus?.("open");
    };

    socket.onmessage = (message) => {
      try {
        const payload = JSON.parse(message.data);
        if (payload.type === "event") onEvent(payload.event as TraceEvent);
      } catch {
        // A malformed frame should drop that frame, not tear down the trace.
      }
    };

    socket.onclose = () => {
      onStatus?.("closed");
      if (closedByCaller) return;
      // Capped exponential backoff: a restarting backend reconnects quickly,
      // an unauthorised socket does not spin.
      attempt += 1;
      const delay = Math.min(1000 * 2 ** (attempt - 1), 15000);
      retryTimer = setTimeout(connect, delay);
    };

    socket.onerror = () => socket?.close();
  };

  connect();

  return () => {
    closedByCaller = true;
    if (retryTimer) clearTimeout(retryTimer);
    socket?.close();
  };
}
