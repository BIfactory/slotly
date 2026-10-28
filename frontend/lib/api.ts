/**
 * Thin fetch wrapper that:
 * - sends cookies on every request (Django session + csrftoken)
 * - reads the csrftoken cookie and attaches it as X-CSRFToken on unsafe verbs
 * - parses the allauth-headless envelope shape consistently
 *
 * The browser sees all calls as same-origin thanks to Next.js rewrites
 * (see next.config.ts), so cookies and CSRF "just work".
 */

export type AllauthResponse<T = unknown> = {
  status: number;
  data?: T;
  errors?: Array<{ message: string; code?: string; param?: string }>;
  meta?: { is_authenticated: boolean };
};

export class ApiError extends Error {
  status: number;
  payload: AllauthResponse;
  constructor(payload: AllauthResponse) {
    super(payload.errors?.[0]?.message ?? `Request failed (${payload.status})`);
    this.status = payload.status;
    this.payload = payload;
  }
}

function readCookie(name: string): string | undefined {
  if (typeof document === "undefined") return undefined;
  const match = document.cookie.match(
    new RegExp("(?:^|; )" + name.replace(/[.$?*|{}()\[\]\\\/+^]/g, "\\$&") + "=([^;]*)"),
  );
  return match ? decodeURIComponent(match[1]) : undefined;
}

let csrfSeeded = false;

/** Seed the csrftoken cookie via a GET to a safe endpoint. Idempotent. */
async function ensureCsrf(): Promise<void> {
  if (csrfSeeded || readCookie("csrftoken")) {
    csrfSeeded = true;
    return;
  }
  await fetch("/_allauth/browser/v1/config", { credentials: "include" });
  csrfSeeded = true;
}

/**
 * Delete the csrftoken cookie so the next GET /_allauth/browser/v1/config
 * forces Django to emit a fresh one. Needed because Django's CsrfViewMiddleware
 * only emits a new cookie (Set-Cookie: csrftoken=…) when the current cookie
 * is missing or malformed — a stale-but-well-formed cookie is left alone
 * even if it doesn't match the user's session CSRF secret. Simply re-fetching
 * /_allauth/browser/v1/config therefore does NOT refresh anything.
 */
function clearCsrfCookie(): void {
  if (typeof document === "undefined") return;
  // Match the attributes Django's response set (path=/, domain omitted, secure).
  // Setting the same name with expires in the past removes the browser's copy.
  document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; samesite=Lax";
  if (window.location.protocol === "https:") {
    document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; samesite=Lax; secure";
  }
}

/**
 * Force a fresh csrftoken cookie after a session-rotating event
 * (login / signup / confirmEmail). Django rotates the server-side CSRF
 * secret on login, but the browser's masked csrftoken cookie survives
 * — and the server leaves well-formed cookies alone — so later unsafe
 * requests send a stale X-CSRFToken that doesn't match the new session's
 * secret, and DRF returns
 *   "CSRF Failed: CSRF token from the 'X-Csrftoken' HTTP header incorrect."
 * Deleting the cookie first makes the config endpoint emit a brand-new
 * Set-Cookie that matches the current session.
 */
export async function refreshCsrf(): Promise<void> {
  csrfSeeded = false;
  clearCsrfCookie();
  try {
    await fetch("/_allauth/browser/v1/config", { credentials: "include" });
  } finally {
    csrfSeeded = true;
  }
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export async function api<T = unknown>(
  path: string,
  opts: { method?: Method; body?: unknown } = {},
): Promise<AllauthResponse<T>> {
  const method: Method = opts.method ?? "GET";
  const isUnsafe = method !== "GET";

  if (isUnsafe) await ensureCsrf();

  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (isUnsafe) {
    const csrf = readCookie("csrftoken");
    if (csrf) headers["X-CSRFToken"] = csrf;
  }

  const res = await fetch(path, {
    method,
    credentials: "include",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  let payload: AllauthResponse<T>;
  const text = await res.text();
  try {
    payload = text ? (JSON.parse(text) as AllauthResponse<T>) : { status: res.status };
  } catch {
    throw new ApiError({
      status: res.status,
      errors: [{ message: text.slice(0, 200) || `HTTP ${res.status}` }],
    });
  }

  // allauth returns 200/401 as expected payloads; only treat 5xx and unknown
  // shapes as exceptional.
  if (!res.ok && payload.status >= 500) throw new ApiError(payload);

  return payload;
}
