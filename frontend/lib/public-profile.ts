/**
 * Client for /api/public/profile/<token> — anonymous availability view.
 */

import type { WorkingHours } from "./me";

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

  const out: { start: Date; end: Date }[] = [];
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
      out.push(...frees);
    }
    cursor.setDate(cursor.getDate() + 1);
  }

  return out.map((f) => ({ start: f.start.toISOString(), end: f.end.toISOString() }));
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
