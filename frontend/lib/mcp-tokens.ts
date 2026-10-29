/**
 * Client for the MCP token resource. Session-authed (same CSRF pattern as
 * lib/calendars.ts). Never includes MCPTokenAuthentication — that lives on
 * /mcp/v1/* only.
 */

import { refreshCsrf } from "./api";

export type McpTokenScope =
  | "list_teams"
  | "list_members"
  | "find_slots"
  | "check_time"
  | "create_booking"
  | "write_event";

export type McpTokenScopeGroup = "read" | "write";

export type McpTokenScopeChoice = {
  name: McpTokenScope;
  label: string;
  group: McpTokenScopeGroup;
};

/** Fallback list used when /api/mcp/scopes is unreachable. */
export const MCP_SCOPE_CHOICES_FALLBACK: McpTokenScopeChoice[] = [
  { name: "list_teams", label: "List teams", group: "read" },
  { name: "list_members", label: "List team members", group: "read" },
  { name: "find_slots", label: "Find free slots", group: "read" },
  { name: "check_time", label: "Check specific time", group: "read" },
  { name: "create_booking", label: "Create / update / cancel bookings", group: "write" },
  { name: "write_event", label: "Write bare calendar events", group: "write" },
];

export const MCP_TOKEN_SCOPES: McpTokenScope[] = [
  "list_teams",
  "list_members",
  "find_slots",
  "check_time",
  "create_booking",
  "write_event",
];

export const MCP_TOKEN_SCOPE_LABELS: Record<McpTokenScope, string> = {
  list_teams: "Read teams you belong to",
  list_members: "Read team member details",
  find_slots: "Find free slots across a team",
  check_time: "Check whether a time window is free",
  create_booking: "Create / update / cancel bookings",
  write_event: "Write bare calendar events",
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
  status: "ok" | "error";
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

async function doFetch(path: string, init: RequestInit): Promise<Response> {
  return fetch(path, {
    credentials: "include",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...csrfHeader(),
      ...(init.headers ?? {}),
    },
  });
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res = await doFetch(path, init);

  // Recovery for a stale csrftoken cookie (user signed in before the
  // login/signup flow was updated to refresh CSRF explicitly). Delete
  // the cookie first — Django leaves well-formed csrftoken cookies alone
  // on GET /_allauth/browser/v1/config even when they don't match the
  // current session secret, so just fetching config without clearing
  // doesn't actually refresh anything. Delete → fetch → the server sees
  // the cookie missing, generates a new token matching the current
  // session, and emits Set-Cookie. Retry once.
  const method = (init.method ?? "GET").toUpperCase();
  if (method !== "GET" && res.status === 403) {
    const peek = await res.clone().text().catch(() => "");
    if (peek.includes("CSRF")) {
      try {
        await refreshCsrf();
      } catch {
        // Ignore — fallthrough will surface the original 403 to the caller.
      }
      res = await doFetch(path, init);
    }
  }

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

export function purgeMcpToken(id: number): Promise<void> {
  return request<void>(`/api/mcp/tokens/${id}/purge`, { method: "DELETE" });
}

export function listMcpTokenCalls(id: number): Promise<McpTokenCall[]> {
  return request<McpTokenCall[]>(`/api/mcp/tokens/${id}/calls`);
}

export function listMcpTokenScopes(): Promise<McpTokenScopeChoice[]> {
  return request<{ scopes: McpTokenScopeChoice[] }>("/api/mcp/scopes").then(
    (d) => d.scopes,
  );
}
