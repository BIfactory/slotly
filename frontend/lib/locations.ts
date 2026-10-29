/**
 * Client for the /api/me/locations* endpoints (G2).
 *
 * All mutating calls include a CSRF token header (read from the cookie set by
 * Django) and use `credentials: "include"` so the session cookie is sent.
 */

export type UserLocation = {
  id: number;
  name: string;
  address: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
};

export type FieldErrors = Record<string, unknown>;

export class LocationApiError extends Error {
  status: number;
  fields: FieldErrors;
  constructor(status: number, fields: FieldErrors, message?: string) {
    super(message ?? "Location API error");
    this.status = status;
    this.fields = fields;
  }
}

function csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
  return m ? { "X-CSRFToken": decodeURIComponent(m[1]) } : {};
}

async function handleResponse<T>(res: Response): Promise<T> {
  const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (!res.ok) {
    // Prefer a detail string, otherwise pass the whole body as field errors.
    const detail =
      typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new LocationApiError(res.status, body, detail);
  }
  return body as T;
}

/** Fetch all locations for the current user. */
export async function listLocations(): Promise<UserLocation[]> {
  const res = await fetch("/api/me/locations", { credentials: "include" });
  // Backend returns a bare JSON array, not a {locations:[]} envelope.
  return handleResponse<UserLocation[]>(res);
}

/** Create a new location. */
export async function createLocation(input: {
  name: string;
  address: string;
  is_default?: boolean;
}): Promise<UserLocation> {
  const res = await fetch("/api/me/locations", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify(input),
  });
  return handleResponse<UserLocation>(res);
}

/** Partially update an existing location. */
export async function updateLocation(
  id: number,
  input: Partial<Pick<UserLocation, "name" | "address" | "is_default">>,
): Promise<UserLocation> {
  const res = await fetch(`/api/me/locations/${id}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify(input),
  });
  return handleResponse<UserLocation>(res);
}

/** Delete a location. */
export async function deleteLocation(id: number): Promise<void> {
  const res = await fetch(`/api/me/locations/${id}`, {
    method: "DELETE",
    credentials: "include",
    headers: { ...csrfHeader() },
  });
  if (!res.ok && res.status !== 204) {
    const body = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    const detail =
      typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
    throw new LocationApiError(res.status, body, detail);
  }
}

/**
 * Set the given location as the user's default.
 * Returns the updated location with is_default=true. The backend clears
 * is_default on any previously-default location.
 */
export async function setDefaultLocation(id: number): Promise<UserLocation> {
  const res = await fetch(`/api/me/locations/${id}/set-default`, {
    method: "POST",
    credentials: "include",
    headers: { ...csrfHeader() },
  });
  return handleResponse<UserLocation>(res);
}
