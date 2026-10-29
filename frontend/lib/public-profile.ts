/**
 * Client for /api/public/profile/<token> — anonymous availability view.
 */

import type { WorkingHours } from "./me";

/**
 * Granularity constant (minutes) — must match the backend engine default.
 * Free-interval starts are always snapped UP to the next multiple of this value
 * and intervals shorter than this are dropped as non-bookable noise.
 * Change this one constant if the grid ever changes project-wide.
 */
const GRANULARITY_MIN = 15;

/**
 * Round `d` UP to the next `granularityMin`-minute grid boundary in LOCAL
 * wall-clock time (using the Date object's implicit local offset, which is
 * the browser's local timezone — matching how working hours are displayed).
 *
 * Invariants (verified manually for each edge case):
 *   I1. Exact-on-grid input → unchanged (14:45:00.000 → 14:45:00.000).
 *        Math: (45 + 0) % 15 == 0, so remainder == 0 and advance == 0.
 *   I2. Off-grid → advanced to next grid point, seconds+ms zeroed.
 *        Math: 14:43:17.500 → remainder = 43%15 = 13, advance = 15-13 = 2 → 14:45:00.000.
 *        Note: seconds and sub-minute components are zeroed so the advance is
 *        to the next full boundary, not 14:45:17. This is intentional.
 *   I3. Boundary exactly at minute=0 stays at 0 for that hour. E.g. 15:00 exact → 15:00.
 *
 * Edge-case notes:
 *   - When `now` is passed and it is on the grid, the start is `now` itself
 *     (I1 applies). The interval is NOT advanced to the next boundary.
 *   - When `freeStart > now`, `now` never affects the result (max picks freeStart).
 *   - When `freeEnd <= now`, the caller drops the interval before calling this.
 */
function snapUpToGrid(d: Date, granularityMin: number): Date {
  const totalMin = d.getHours() * 60 + d.getMinutes();
  const remainder = totalMin % granularityMin;
  const advanceMin = remainder === 0 ? 0 : granularityMin - remainder;
  const snapped = new Date(d);
  snapped.setMinutes(d.getMinutes() + advanceMin, 0, 0); // zeroes seconds + ms
  return snapped;
}

export type PublicProfile = {
  display_name: string;
  avatar_url: string | null;
  country: string;
  working_hours: WorkingHours;
};

export type BusyInterval = { start: string; end: string };

export type PublicProfileResponse = {
  profile: PublicProfile;
  window: { start: string; end: string };
  busy: BusyInterval[];
  holidays: { date: string; name: string }[];
  /** Whether the host has an OAuth-connected calendar we can write events
   * into. Only present on /api/public/profile/<token>. False for other
   * availability endpoints (they're consumed by authed views that make
   * their own connect-status calls). */
  booking_enabled?: boolean;
  /** Active MeetingType presets the visitor can pick from. Empty list
   * means the host hasn't defined any and /u/[token] should show the
   * generic "pick any free time" flow. */
  meeting_types?: import("./meeting-types").PublicMeetingType[];
  /** Sorted list of OAuth provider names the host has connected.
   * Present when both or one are connected; empty array when neither.
   * Used to conditionally show the meeting-link selector on the public
   * booking page (only shown when length >= 2). */
  host_providers?: ("google" | "microsoft")[];
  /** Whether the host has Google calendar bookings enabled.
   * Only present on /api/public/profile/<token>. */
  accepts_google_bookings?: boolean;
  /** Whether the host has Microsoft calendar bookings enabled.
   * Only present on /api/public/profile/<token>. */
  accepts_microsoft_bookings?: boolean;
  /** Host's predefined in-person locations. Empty array when none are set.
   * Sorted: default row first, then alphabetical by name. */
  host_locations?: Array<{
    id: number;
    name: string;
    address: string;
    is_default: boolean;
  }>;
};

export class PublicProfileNotFoundError extends Error {
  constructor() {
    super("Profile not found or no longer public");
  }
}

export async function getPublicProfile(token: string): Promise<PublicProfileResponse> {
  const res = await fetch(`/api/public/profile/${token}`, {
    method: "GET",
    headers: { Accept: "application/json" },
  });
  if (res.status === 404) throw new PublicProfileNotFoundError();
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/**
 * Compute free intervals (working hours minus busy) for the next N days
 * in the app's local time zone. Returns intervals shaped like search Slots
 * so the existing SlotsCalendar can render them.
 *
 * Three post-processing invariants applied to every raw free interval [s, e]:
 *   P1. Clip to now: if e <= now, drop entirely; otherwise s = max(s, now).
 *   P2. Snap start UP to next GRANULARITY_MIN boundary: s = snapUpToGrid(s).
 *   P3. Drop if too short: if e - s < GRANULARITY_MIN minutes, drop.
 *
 * These mirror what the backend engine does for its _snap_up_to_grid step
 * (apps/search/engine.py) so the public-profile and profile pages show the
 * same well-formed slot boundaries as the search results page.
 *
 * The END is intentionally NOT snapped down — a free block ending at an
 * off-grid time (e.g. 16:03) is fine; SlotsCalendar renders durations from
 * the snapped start and the booking flow always picks a fixed duration.
 */
export function computeFreeSlots(
  workingHours: WorkingHours,
  busy: BusyInterval[],
  windowStart: Date,
  windowEnd: Date,
): { start: string; end: string }[] {
  const WEEKDAYS = [
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
  ] as const;

  const busyDates = busy.map((b) => ({
    start: new Date(b.start),
    end: new Date(b.end),
  }));

  const raw: { start: Date; end: Date }[] = [];
  const cursor = new Date(windowStart);
  cursor.setHours(0, 0, 0, 0);

  while (cursor < windowEnd) {
    const dayName = WEEKDAYS[cursor.getDay()];
    const wh = workingHours[dayName];
    if (wh.available) {
      const [sh, sm] = wh.start.split(":").map(Number);
      const [eh, em] = wh.end.split(":").map(Number);
      const dayStart = new Date(cursor);
      dayStart.setHours(sh, sm, 0, 0);
      const dayEnd = new Date(cursor);
      dayEnd.setHours(eh, em, 0, 0);

      // Subtract overlapping busy intervals from this day's working window.
      let frees: { start: Date; end: Date }[] = [{ start: dayStart, end: dayEnd }];
      for (const b of busyDates) {
        if (b.end <= dayStart || b.start >= dayEnd) continue;
        const next: { start: Date; end: Date }[] = [];
        for (const f of frees) {
          if (b.end <= f.start || b.start >= f.end) {
            next.push(f);
            continue;
          }
          if (b.start > f.start) next.push({ start: f.start, end: b.start });
          if (b.end < f.end) next.push({ start: b.end, end: f.end });
        }
        frees = next.filter((f) => f.end.getTime() > f.start.getTime());
      }
      raw.push(...frees);
    }
    cursor.setDate(cursor.getDate() + 1);
  }

  // ── Post-processing: P1 clip-to-now + P2 snap-up + P3 drop-short ─────────
  const now = new Date();
  const granularityMs = GRANULARITY_MIN * 60_000;
  const out: { start: string; end: string }[] = [];

  for (const f of raw) {
    // P1: drop intervals entirely in the past.
    if (f.end.getTime() <= now.getTime()) continue;

    // P1: clip start to now (never show a start in the past).
    const clipped = f.start.getTime() < now.getTime() ? now : f.start;

    // P2: snap start UP to the next grid boundary.
    const snappedStart = snapUpToGrid(clipped, GRANULARITY_MIN);

    // P3: drop if the snapped interval is shorter than one granularity step.
    if (f.end.getTime() - snappedStart.getTime() < granularityMs) continue;

    out.push({ start: snappedStart.toISOString(), end: f.end.toISOString() });
  }

  return out;
}

// ---------------------------------------------------------------------------
// Peer availability — /api/me/peer-availability/<id>
// ---------------------------------------------------------------------------

export type PeerAvailabilityResponse = {
  peer: PublicProfile;
  window: { start: string; end: string };
  slots: { start: string; end: string }[];
  count: number;
  truncated: boolean;
  holidays: { date: string; name: string }[];
};

/**
 * Teammate's *own* availability — the same view they see on their own /profile.
 * No intersection with the caller, no min-duration filter. Feeds /people/<id>.
 */
export async function getTeammateAvailability(
  userId: number,
): Promise<PublicProfileResponse> {
  const res = await fetch(`/api/users/${userId}/availability`, {
    method: "GET",
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function getPeerAvailability(
  userId: number,
  opts: { from?: string; to?: string; durationMin: number; bufferMin?: number },
): Promise<PeerAvailabilityResponse> {
  const csrf = (() => {
    if (typeof document === "undefined") return undefined;
    const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : undefined;
  })();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (csrf) headers["X-CSRFToken"] = csrf;
  const res = await fetch(`/api/me/peer-availability/${userId}`, {
    method: "POST",
    credentials: "include",
    headers,
    body: JSON.stringify({
      from: opts.from,
      to: opts.to,
      duration_min: opts.durationMin,
      buffer_min: opts.bufferMin ?? 0,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/** Reduce a per-weekday WorkingHours object to [minStartHour, maxEndHour]
 * across the days that are marked available. Returns undefined when there
 * are no available days. Used to anchor the SlotsCalendar time axis so it
 * shows the user's working window even when the only free slots are
 * deep inside it. */
export function workingHoursRangeFromHours(
  wh: WorkingHours,
): [number, number] | undefined {
  let minStart = 24;
  let maxEnd = 0;
  let any = false;
  for (const day of Object.values(wh)) {
    if (!day.available) continue;
    any = true;
    const [sh] = day.start.split(":").map(Number);
    const [eh, em] = day.end.split(":").map(Number);
    if (sh < minStart) minStart = sh;
    const endHourCeil = em > 0 ? eh + 1 : eh;
    if (endHourCeil > maxEnd) maxEnd = endHourCeil;
  }
  if (!any) return undefined;
  return [minStart, maxEnd];
}

export function getInitials(displayName: string): string {
  const parts = displayName.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** Hash a string into a stable hue for the initials avatar background. */
export function colorFromName(displayName: string): string {
  let h = 0;
  for (let i = 0; i < displayName.length; i++) {
    h = (h * 31 + displayName.charCodeAt(i)) | 0;
  }
  return `hsl(${Math.abs(h) % 360}, 60%, 55%)`;
}
