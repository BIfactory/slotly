/**
 * Client for the DRF /api/me endpoint. Returns plain DRF JSON (not the
 * allauth-headless envelope), so it talks to fetch directly with manual CSRF.
 */

export const WEEKDAYS = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
] as const;

export type Weekday = (typeof WEEKDAYS)[number];

export type DayHours = { start: string; end: string; available: boolean };

export type WorkingHours = Record<Weekday, DayHours>;

export type Me = {
  email: string;
  first_name: string;
  last_name: string;
  phone: string;
  working_hours: WorkingHours;
  country: string;
  share_enabled: boolean;
  share_token: string;
  avatar_url: string | null;
  buffer_before_min: number;
  buffer_after_min: number;
  accepts_google_bookings: boolean;
  accepts_microsoft_bookings: boolean;
};

export type MePatch = Partial<
  Pick<
    Me,
    | "first_name"
    | "last_name"
    | "phone"
    | "working_hours"
    | "country"
    | "share_enabled"
    | "buffer_before_min"
    | "buffer_after_min"
    | "accepts_google_bookings"
    | "accepts_microsoft_bookings"
  >
>;

export const SUPPORTED_COUNTRIES: Array<{ code: string; name: string }> = [
  { code: "CZ", name: "Czech Republic" },
  { code: "SK", name: "Slovakia" },
  { code: "AT", name: "Austria" },
  { code: "DE", name: "Germany" },
  { code: "PL", name: "Poland" },
  { code: "HU", name: "Hungary" },
  { code: "FR", name: "France" },
  { code: "IT", name: "Italy" },
  { code: "ES", name: "Spain" },
  { code: "GB", name: "United Kingdom" },
  { code: "IE", name: "Ireland" },
  { code: "NL", name: "Netherlands" },
  { code: "BE", name: "Belgium" },
  { code: "US", name: "United States" },
  { code: "CA", name: "Canada" },
  { code: "AU", name: "Australia" },
  { code: "NZ", name: "New Zealand" },
  { code: "NO", name: "Norway" },
  { code: "SE", name: "Sweden" },
  { code: "DK", name: "Denmark" },
  { code: "FI", name: "Finland" },
];

export type FieldErrors = Record<string, unknown>;

export class MeApiError extends Error {
  status: number;
  fields: FieldErrors;
  constructor(status: number, fields: FieldErrors) {
    super("Update failed");
    this.status = status;
    this.fields = fields;
  }
}

function csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
  return m ? { "X-CSRFToken": decodeURIComponent(m[1]) } : {};
}

// ---------------------------------------------------------------------------
// /api/me cache: stale-while-revalidate.
// First call fetches and populates. Subsequent calls return the cached value
// instantly and trigger a background refetch — pages that poll on every
// navigation render immediately instead of awaiting a round-trip.
// Mutating helpers (patchMe, regenerateShareToken, uploadAvatar) and
// auth-state changes (logout) refresh / clear the cache directly.
// ---------------------------------------------------------------------------

let _meCache: Me | null = null;
let _meInflight: Promise<Me> | null = null;

async function _fetchMe(): Promise<Me> {
  const res = await fetch("/api/me", { credentials: "include" });
  if (!res.ok) throw new MeApiError(res.status, await res.json().catch(() => ({})));
  const data = (await res.json()) as Me;
  _meCache = data;
  return data;
}

export async function getMe(): Promise<Me> {
  if (_meCache) {
    // Return immediately, refresh in the background. If a refetch is already
    // in flight, don't kick a second one.
    if (!_meInflight) {
      _meInflight = _fetchMe()
        .catch(() => _meCache!)
        .finally(() => {
          _meInflight = null;
        });
    }
    return _meCache;
  }
  if (!_meInflight) _meInflight = _fetchMe().finally(() => { _meInflight = null; });
  return _meInflight;
}

/** Drop the cached /api/me so the next getMe() forces a fresh fetch.
 * Call after sign-out (or anything else that invalidates the user). */
export function clearMeCache(): void {
  _meCache = null;
  _meInflight = null;
}

export async function patchMe(patch: MePatch): Promise<Me> {
  const res = await fetch("/api/me", {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify(patch),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new MeApiError(res.status, body);
  _meCache = body as Me;
  return body as Me;
}

export type DeleteMePreview = {
  teams_member_count: number;
  teams_will_be_deleted: number;
  team_members_will_be_notified: number;
  calendars_count: number;
  cached_events_count: number;
  notifications_count: number;
};

export async function getDeleteMePreview(): Promise<DeleteMePreview> {
  const res = await fetch("/api/me/delete", { credentials: "include" });
  if (!res.ok) throw new MeApiError(res.status, await res.json().catch(() => ({})));
  return res.json();
}

export async function deleteMe(password: string): Promise<void> {
  const res = await fetch("/api/me/delete", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) throw new MeApiError(res.status, await res.json().catch(() => ({})));
}

export async function regenerateShareToken(): Promise<Me> {
  const res = await fetch("/api/me/share/regenerate", {
    method: "POST",
    credentials: "include",
    headers: csrfHeader(),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new MeApiError(res.status, body);
  _meCache = body as Me;
  return body as Me;
}

export async function uploadAvatar(file: File): Promise<Me> {
  const fd = new FormData();
  fd.append("avatar", file);
  const res = await fetch("/api/me", {
    method: "PATCH",
    credentials: "include",
    headers: csrfHeader(),  // no Content-Type — browser sets multipart boundary
    body: fd,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new MeApiError(res.status, body);
  _meCache = body as Me;
  return body as Me;
}
