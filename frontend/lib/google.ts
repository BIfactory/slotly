/**
 * Client for the Google Calendar connection endpoints (M18a).
 *
 * The OAuth flow itself is server-side: the frontend just navigates the
 * browser to /api/oauth/google/start, Google handles consent, and the
 * callback redirects back to /settings/integrations?google=<status>.
 * The functions here only read/clear the resulting GoogleAccount row.
 */

export type GoogleAccountStatus =
  | { connected: false }
  | { connected: true; google_email: string; write_calendar_id: string };

export type WritableCalendar = {
  id: string;
  summary: string;
  primary: boolean;
  access_role: "owner" | "writer" | string;
  time_zone: string;
};

function csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
  return m ? { "X-CSRFToken": decodeURIComponent(m[1]) } : {};
}

export async function getGoogleAccount(): Promise<GoogleAccountStatus> {
  const res = await fetch("/api/google-account", { credentials: "include" });
  if (!res.ok) throw new Error(`status ${res.status}`);
  return (await res.json()) as GoogleAccountStatus;
}

/** True when the logged-in user has any OAuth-connected calendar we can
 * write to (Google today; Microsoft when connected). Feeds the pre-flight
 * "connect Google to book" hint on /people/[id]. */
export async function hasWritableProvider(): Promise<boolean> {
  const [g, m] = await Promise.all([
    getGoogleAccount().catch(() => ({ connected: false as const })),
    getMicrosoftAccount().catch(() => ({ connected: false as const })),
  ]);
  return g.connected || m.connected;
}

export async function disconnectGoogleAccount(): Promise<void> {
  const res = await fetch("/api/google-account", {
    method: "DELETE",
    credentials: "include",
    headers: { ...csrfHeader() },
  });
  if (!res.ok) throw new Error(`status ${res.status}`);
}

export async function setWriteCalendar(
  writeCalendarId: string,
): Promise<GoogleAccountStatus> {
  const res = await fetch("/api/google-account", {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify({ write_calendar_id: writeCalendarId }),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  return (await res.json()) as GoogleAccountStatus;
}

export class ReconnectRequiredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ReconnectRequiredError";
  }
}

export async function getWritableCalendars(accountId?: string): Promise<WritableCalendar[]> {
  const url = accountId
    ? `/api/google-account/writable-calendars?account_id=${encodeURIComponent(accountId)}`
    : "/api/google-account/writable-calendars";
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as {
      detail?: string;
      reconnect_required?: boolean;
    };
    if (body.reconnect_required) {
      throw new ReconnectRequiredError(body.detail ?? "Reconnect required");
    }
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  const data = (await res.json()) as { calendars: WritableCalendar[] };
  return data.calendars;
}

// ---------------------------------------------------------------------------
// Meeting creation
// ---------------------------------------------------------------------------

export type CreatedMeeting = {
  ok: true;
  event: {
    id: string;
    html_link?: string;
    /** Video-call URL (Google Meet / Teams). Empty for physical bookings
     * or providers that couldn't create an online meeting. */
    meet_link?: string;
    start: string;
    end: string;
    provider?: "google" | "microsoft";
  };
};

/** Authenticated booking — one call for both single-peer (/people/[id])
 * and group (/search) flows. `attendeeUserIds` may hold one id (peer
 * flow) or many (a search-result slot with a whole team). */
export async function createMeeting(input: {
  attendeeUserIds: number[];
  start: string; // ISO 8601 (may include tz or be local)
  end: string;
  title?: string;
  notes?: string;
}): Promise<CreatedMeeting> {
  const res = await fetch("/api/meetings", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify({
      attendee_user_ids: input.attendeeUserIds,
      start: input.start,
      end: input.end,
      title: input.title,
      notes: input.notes,
    }),
  });
  const body = (await res.json().catch(() => ({}))) as {
    detail?: string;
    ok?: boolean;
    event?: CreatedMeeting["event"];
  };
  if (!res.ok || !body.ok || !body.event) {
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  return { ok: true, event: body.event };
}

/** Thin single-peer alias so existing callers don't have to wrap in
 * arrays. Delegates to createMeeting. */
export async function createMeetingWithPeer(input: {
  peerUserId: number;
  start: string;
  end: string;
  title?: string;
  notes?: string;
}): Promise<CreatedMeeting> {
  return createMeeting({
    attendeeUserIds: [input.peerUserId],
    start: input.start,
    end: input.end,
    title: input.title,
    notes: input.notes,
  });
}

export type PublicBookingResult =
  | (CreatedMeeting & { manage_url?: string; redirect_url?: string })
  | { ok: true; pending: true; request_id: number };

/** Public share booking — /u/[token] SlotsCalendar → this. No auth. When
 * `kind` is "physical", the backend creates a pending BookingRequest
 * instead of a calendar event; the response then carries `pending: true`
 * so the caller can show "waiting for approval" copy. */
export async function createPublicMeeting(input: {
  token: string;
  visitorName: string;
  visitorEmail: string;
  start: string;
  end: string;
  title?: string;
  notes?: string;
  kind?: "online" | "physical";
  location?: string;
  /** When set, backend looks up the MeetingType and locks kind + duration
   * to its values. Visitor's `kind`/`end` are ignored to prevent stretching. */
  typeSlug?: string;
  /** Answers to the type's custom questions, keyed by question id. */
  customAnswers?: Record<string, string>;
  /** Honeypot field. Leave empty; if a script populates every text field
   * it fills this too and the backend silently drops the request. */
  hp?: string;
}): Promise<PublicBookingResult> {
  const res = await fetch(`/api/public/meetings/${input.token}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      visitor_name: input.visitorName,
      visitor_email: input.visitorEmail,
      start: input.start,
      end: input.end,
      title: input.title,
      notes: input.notes,
      kind: input.kind ?? "online",
      location: input.location ?? "",
      type_slug: input.typeSlug,
      custom_answers: input.customAnswers ?? {},
      hp: input.hp ?? "",
    }),
  });
  if (res.status === 204) {
    // Honeypot triggered — mimic success so bots don't learn.
    return { ok: true, event: { id: "", start: input.start, end: input.end } };
  }
  const body = (await res.json().catch(() => ({}))) as {
    detail?: string;
    ok?: boolean;
    pending?: boolean;
    request_id?: number;
    event?: CreatedMeeting["event"];
    manage_url?: string;
    redirect_url?: string;
  };
  if (!res.ok || !body.ok) {
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  if (body.pending && body.request_id) {
    return { ok: true, pending: true, request_id: body.request_id };
  }
  if (body.event) {
    return {
      ok: true,
      event: body.event,
      manage_url: body.manage_url,
      redirect_url: body.redirect_url,
    };
  }
  throw new Error("Unexpected response shape");
}

// ---------------------------------------------------------------------------
// Booking requests (host side — /bookings page)
// ---------------------------------------------------------------------------

export type BookingRequestRow = {
  id: number;
  kind: "physical";
  status: "pending" | "approved" | "rejected" | "cancelled";
  start: string;
  end: string;
  title: string;
  notes: string;
  location: string;
  visitor_name: string;
  visitor_email: string;
  decision_note: string;
  created_at: string;
  decided_at: string | null;
};

export async function listBookingRequests(
  status: "pending" | "all" = "pending",
): Promise<BookingRequestRow[]> {
  const res = await fetch(`/api/booking-requests?status=${status}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = (await res.json()) as { requests: BookingRequestRow[] };
  return body.requests;
}

export type HostBooking = {
  uuid: string;
  kind: "online" | "physical";
  status: "confirmed" | "cancelled";
  start: string;
  end: string;
  title: string;
  location: string;
  visitor_name: string;
  visitor_email: string;
  /** Full attendee list. One element for public single-visitor bookings;
   * many for group meetings from /people or /search. */
  attendee_emails: string[];
  cancelled_at: string | null;
  cancelled_by_visitor: boolean;
  created_at: string;
};

export async function listHostBookings(
  status: "upcoming" | "past" | "cancelled" | "all" = "upcoming",
): Promise<HostBooking[]> {
  const res = await fetch(`/api/host-bookings?status=${status}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = (await res.json()) as { bookings: HostBooking[] };
  return body.bookings;
}

/** Host-initiated cancel. Mirrors cancelManagedBooking but requires auth
 * and mails the *visitor* rather than the host. */
export async function cancelHostBooking(
  uuid: string,
  reason?: string,
): Promise<HostBooking> {
  const res = await fetch(`/api/host-bookings/${uuid}/cancel`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify({ reason: reason ?? "" }),
  });
  const body = (await res.json().catch(() => ({}))) as
    | HostBooking
    | { detail?: string };
  if (!res.ok) {
    throw new Error(("detail" in body && body.detail) || `HTTP ${res.status}`);
  }
  return body as HostBooking;
}

// ---------------------------------------------------------------------------
// Visitor-side booking management — /b/<uuid>
// ---------------------------------------------------------------------------

export type ManagedBooking = {
  uuid: string;
  host_name: string;
  kind: "online" | "physical";
  status: "confirmed" | "cancelled";
  start: string;
  end: string;
  title: string;
  location: string;
  visitor_name: string;
  visitor_email: string;
  cancelled_at: string | null;
  /** Present only on GET — payload the reschedule view needs to render
   * the host's availability calendar without a second API call. */
  availability?: {
    working_hours: import("./me").WorkingHours;
    country: string;
    window: { start: string; end: string };
    busy: { start: string; end: string }[];
    holidays: { date: string; name: string }[];
  };
};

export async function getManagedBooking(uuid: string): Promise<ManagedBooking | null> {
  const res = await fetch(`/api/public/bookings/${uuid}`, {
    method: "GET",
    headers: { Accept: "application/json" },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as ManagedBooking;
}

export async function cancelManagedBooking(
  uuid: string,
  reason?: string,
): Promise<ManagedBooking> {
  const res = await fetch(`/api/public/bookings/${uuid}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason ?? "" }),
  });
  const body = (await res.json().catch(() => ({}))) as
    | ManagedBooking
    | { detail?: string };
  if (!res.ok) {
    throw new Error(("detail" in body && body.detail) || `HTTP ${res.status}`);
  }
  return body as ManagedBooking;
}

export async function rescheduleManagedBooking(
  uuid: string,
  input: { start: string; end: string },
): Promise<ManagedBooking> {
  const res = await fetch(`/api/public/bookings/${uuid}/reschedule`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const body = (await res.json().catch(() => ({}))) as
    | ManagedBooking
    | { detail?: string };
  if (!res.ok) {
    throw new Error(("detail" in body && body.detail) || `HTTP ${res.status}`);
  }
  return body as ManagedBooking;
}

export async function decideBookingRequest(
  id: number,
  decision: "approve" | "reject",
  note?: string,
): Promise<BookingRequestRow> {
  const res = await fetch(`/api/booking-requests/${id}/decide`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify({ decision, note: note ?? "" }),
  });
  const body = (await res.json().catch(() => ({}))) as
    | BookingRequestRow
    | { detail?: string };
  if (!res.ok) {
    throw new Error(("detail" in body && body.detail) || `HTTP ${res.status}`);
  }
  return body as BookingRequestRow;
}

/** Absolute URL we navigate the top-level browser to. The backend issues a
 * 302 to Google's consent screen, then bounces back here. */
export const GOOGLE_CONNECT_URL = "/api/oauth/google/start";

// ---------------------------------------------------------------------------
// Microsoft (parallel of the Google helpers above — same endpoint shape).
// Lives in this file so a caller can treat both providers with one import.
// ---------------------------------------------------------------------------

export type MicrosoftAccountStatus =
  | { connected: false }
  | { connected: true; microsoft_email: string; write_calendar_id: string };

export const MICROSOFT_CONNECT_URL = "/api/oauth/microsoft/start";

export async function getMicrosoftAccount(): Promise<MicrosoftAccountStatus> {
  const res = await fetch("/api/microsoft-account", { credentials: "include" });
  if (!res.ok) throw new Error(`status ${res.status}`);
  return (await res.json()) as MicrosoftAccountStatus;
}

export async function disconnectMicrosoftAccount(): Promise<void> {
  const res = await fetch("/api/microsoft-account", {
    method: "DELETE",
    credentials: "include",
    headers: { ...csrfHeader() },
  });
  if (!res.ok) throw new Error(`status ${res.status}`);
}

export async function setMicrosoftWriteCalendar(
  writeCalendarId: string,
): Promise<MicrosoftAccountStatus> {
  const res = await fetch("/api/microsoft-account", {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify({ write_calendar_id: writeCalendarId }),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  return (await res.json()) as MicrosoftAccountStatus;
}

export async function getMicrosoftWritableCalendars(accountId?: string): Promise<WritableCalendar[]> {
  const url = accountId
    ? `/api/microsoft-account/writable-calendars?account_id=${encodeURIComponent(accountId)}`
    : "/api/microsoft-account/writable-calendars";
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  const data = (await res.json()) as { calendars: WritableCalendar[] };
  return data.calendars;
}
