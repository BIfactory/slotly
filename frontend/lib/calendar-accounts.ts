/**
 * Client for the multi-account /api/calendar-accounts endpoints (Task 3).
 *
 * `id` is a composite string like "g:3" (Google account PK 3) or "m:7"
 * (Microsoft account PK 7).  The provider field is either "google" or
 * "microsoft".
 */

export type CalendarAccount = {
  id: string;
  provider: "google" | "microsoft";
  email: string;
  write_calendar_id: string;
  is_default: boolean;
  created_at: string;
};

function csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
  return m ? { "X-CSRFToken": decodeURIComponent(m[1]) } : {};
}

/** Fetch all connected calendar accounts for the current user. */
export async function listCalendarAccounts(): Promise<CalendarAccount[]> {
  const res = await fetch("/api/calendar-accounts", { credentials: "include" });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  const data = (await res.json()) as { accounts: CalendarAccount[] };
  return data.accounts;
}

/** Mark an account as the default (one across all providers). */
export async function setDefaultAccount(id: string): Promise<void> {
  const res = await fetch(`/api/calendar-accounts/${encodeURIComponent(id)}/set-default`, {
    method: "POST",
    credentials: "include",
    headers: { ...csrfHeader() },
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
}

/** Delete (disconnect) a single calendar account. */
export async function disconnectAccount(id: string): Promise<void> {
  const res = await fetch(`/api/calendar-accounts/${encodeURIComponent(id)}`, {
    method: "DELETE",
    credentials: "include",
    headers: { ...csrfHeader() },
  });
  if (!res.ok && res.status !== 204) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
}

/** Update the write-calendar target for a single account. */
export async function patchAccountWriteCalendar(
  id: string,
  writeCalendarId: string,
): Promise<CalendarAccount> {
  const res = await fetch(`/api/calendar-accounts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify({ write_calendar_id: writeCalendarId }),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  return (await res.json()) as CalendarAccount;
}
