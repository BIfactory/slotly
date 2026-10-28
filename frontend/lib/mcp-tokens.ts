/**
 * Client for the MCP token resource. Session-authed (same CSRF pattern as
 * lib/calendars.ts). Never includes MCPTokenAuthentication — that lives on
 * /mcp/v1/* only.
 */

export type McpTokenScope = "list_teams" | "list_members" | "find_slots" | "check_time";

export const MCP_TOKEN_SCOPES: McpTokenScope[] = [
  "list_teams",
  "list_members",
  "find_slots",
  "check_time",
];

export const MCP_TOKEN_SCOPE_LABELS: Record<McpTokenScope, string> = {
  list_teams: "Read teams you belong to",
  list_members: "Read team member details",
  find_slots: "Find free slots across a team",
  check_time: "Check whether a time window is free",
};

export type McpToken = {
  id: number;
  name: string;
  prefix: string;
  scopes: McpTokenScope[];
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  is_active: boolean;
};

/** Only present on the create response — raw token shown once. */
export type McpTokenCreated = McpToken & { token: string };

export type McpTokenCall = {
  id: number;
  capability: string;
  status: "ok" | "error" | "rate_limited";
  duration_ms: number;
  error_code: string;
  created_at: string;
};

export class McpTokenApiError extends Error {
  status: number;
  fields: Record<string, unknown>;
  constructor(status: number, fields: Record<string, unknown>) {
    super(typeof fields.detail === "string" ? fields.detail : "Request failed");
    this.status = status;
    this.fields = fields;
  }
}

function csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
  return m ? { "X-CSRFToken": decodeURIComponent(m[1]) } : {};
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...csrfHeader(),
      ...(init.headers ?? {}),
    },
  });
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new McpTokenApiError(res.status, body);
  return body as T;
}

export function listMcpTokens(): Promise<McpToken[]> {
  return request<McpToken[]>("/api/mcp/tokens");
}

export function createMcpToken(input: {
  name: string;
  scopes: McpTokenScope[];
}): Promise<McpTokenCreated> {
  return request<McpTokenCreated>("/api/mcp/tokens", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function revokeMcpToken(id: number): Promise<void> {
  return request<void>(`/api/mcp/tokens/${id}`, { method: "DELETE" });
}

export function listMcpTokenCalls(id: number): Promise<McpTokenCall[]> {
  return request<McpTokenCall[]>(`/api/mcp/tokens/${id}/calls`);
}
