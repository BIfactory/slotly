/**
 * Domain wrappers around allauth-headless browser endpoints.
 * Reference: https://docs.allauth.org/en/latest/headless/openapi-specification/
 */

import { api, refreshCsrf, type AllauthResponse } from "./api";
import { clearMeCache } from "./me";

const BASE = "/_allauth/browser/v1";

type SessionMeta = { is_authenticated: boolean };

export type SessionPayload = {
  user?: { id: number; email: string; display: string };
  flows?: Array<{ id: string; is_pending?: boolean }>;
};

// ---------------------------------------------------------------------------
// /auth/session cache: stale-while-revalidate, mirrors lib/me.ts.
// Hot navigations (Find a slot ↔ People ↔ Settings) gate on getSession()
// to decide whether to redirect to login. Caching the result removes a
// round-trip per page change. Login / signup / logout / confirmEmail
// invalidate the cache so the next call refetches.
// ---------------------------------------------------------------------------

let _sessionCache: AllauthResponse<SessionPayload> | null = null;
let _sessionInflight: Promise<AllauthResponse<SessionPayload>> | null = null;

async function _fetchSession(): Promise<AllauthResponse<SessionPayload>> {
  const r = await api<SessionPayload>(`${BASE}/auth/session`);
  _sessionCache = r;
  return r;
}

export async function getSession(): Promise<AllauthResponse<SessionPayload>> {
  if (_sessionCache) {
    if (!_sessionInflight) {
      _sessionInflight = _fetchSession()
        .catch(() => _sessionCache!)
        .finally(() => {
          _sessionInflight = null;
        });
    }
    return _sessionCache;
  }
  if (!_sessionInflight) {
    _sessionInflight = _fetchSession().finally(() => { _sessionInflight = null; });
  }
  return _sessionInflight;
}

function _clearSessionCache() {
  _sessionCache = null;
  _sessionInflight = null;
}

export async function getConfig(): Promise<AllauthResponse> {
  return api(`${BASE}/config`);
}

export async function signup(email: string, password: string) {
  const r = await api<SessionPayload>(`${BASE}/auth/signup`, {
    method: "POST",
    body: { email, password },
  });
  _clearSessionCache();
  clearMeCache();
  // Django rotates the server-side CSRF on session change; re-emit the
  // cookie so later unsafe requests send the matching token.
  await refreshCsrf();
  return r;
}

export async function login(email: string, password: string) {
  const r = await api<SessionPayload>(`${BASE}/auth/login`, {
    method: "POST",
    body: { email, password },
  });
  _clearSessionCache();
  clearMeCache();
  await refreshCsrf();
  return r;
}

export async function logout(): Promise<AllauthResponse> {
  const r = await api(`${BASE}/auth/session`, { method: "DELETE" });
  _clearSessionCache();
  clearMeCache();
  await refreshCsrf();
  return r;
}

export async function confirmEmail(key: string) {
  const r = await api<SessionPayload>(`${BASE}/auth/email/verify`, {
    method: "POST",
    body: { key },
  });
  _clearSessionCache();
  clearMeCache();
  await refreshCsrf();
  return r;
}

export async function requestPasswordReset(email: string) {
  return api(`${BASE}/auth/password/request`, {
    method: "POST",
    body: { email },
  });
}

export async function resetPassword(key: string, password: string) {
  return api(`${BASE}/auth/password/reset`, {
    method: "POST",
    body: { key, password },
  });
}

/** Return true if the user has a fully authenticated session. */
export function isAuthed(meta?: SessionMeta) {
  return meta?.is_authenticated === true;
}

/** Detect whether the response indicates verify_email is the next step. */
export function needsEmailVerification(res: AllauthResponse<SessionPayload>) {
  return Boolean(
    res.data?.flows?.some((f) => f.id === "verify_email" && f.is_pending),
  );
}
