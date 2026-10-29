/**
 * Client for /api/meeting-types (host CRUD) and the meeting-types field
 * on /api/public/profile/<token> (visitor-facing read).
 */

export type MeetingTypeQuestion = {
  id: string;
  label: string;
  kind: "text" | "textarea" | "select";
  required: boolean;
  options?: string[];
};

export type MeetingLinkKind = "auto" | "google_meet" | "teams" | "none";

export type LocationRef = {
  id: number;
  name: string;
  address: string;
};

export type MeetingType = {
  id: number;
  name: string;
  slug: string;
  description: string;
  duration_min: number;
  kind: "online" | "physical";
  location: string;
  color: string;
  is_active: boolean;
  display_order: number;
  questions: MeetingTypeQuestion[];
  redirect_url: string;
  meeting_link_kind: MeetingLinkKind;
  /** Nullable FK to a saved UserLocation (G5). */
  default_location_id: number | null;
  default_location: LocationRef | null;
};

/** Trimmed shape the public profile endpoint returns — no id/is_active
 * because visitors only ever see active types. */
export type PublicMeetingType = {
  slug: string;
  name: string;
  description: string;
  duration_min: number;
  kind: "online" | "physical";
  location: string;
  color: string;
  questions: MeetingTypeQuestion[];
  redirect_url: string;
  /** Nullable FK to a saved UserLocation (G5). */
  default_location: LocationRef | null;
};

function csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
  return m ? { "X-CSRFToken": decodeURIComponent(m[1]) } : {};
}

export async function listMeetingTypes(): Promise<MeetingType[]> {
  const res = await fetch("/api/meeting-types", { credentials: "include" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = (await res.json()) as { types: MeetingType[] };
  return body.types;
}

export type MeetingTypeInput = {
  name: string;
  duration_min: number;
  kind?: "online" | "physical";
  description?: string;
  location?: string;
  color?: string;
  is_active?: boolean;
  display_order?: number;
  questions?: MeetingTypeQuestion[];
  redirect_url?: string;
  meeting_link_kind?: MeetingLinkKind;
  /** Nullable FK to a saved UserLocation (G5). */
  default_location_id?: number | null;
};

export async function createMeetingType(input: MeetingTypeInput): Promise<MeetingType> {
  const res = await fetch("/api/meeting-types", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify(input),
  });
  const body = (await res.json().catch(() => ({}))) as MeetingType | Record<string, string>;
  if (!res.ok) {
    const first = Object.values(body).find((v): v is string => typeof v === "string");
    throw new Error(first ?? `HTTP ${res.status}`);
  }
  return body as MeetingType;
}

export async function updateMeetingType(
  id: number,
  input: Partial<MeetingTypeInput>,
): Promise<MeetingType> {
  const res = await fetch(`/api/meeting-types/${id}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json", ...csrfHeader() },
    body: JSON.stringify(input),
  });
  const body = (await res.json().catch(() => ({}))) as MeetingType | Record<string, string>;
  if (!res.ok) {
    const first = Object.values(body).find((v): v is string => typeof v === "string");
    throw new Error(first ?? `HTTP ${res.status}`);
  }
  return body as MeetingType;
}

export async function deleteMeetingType(id: number): Promise<void> {
  const res = await fetch(`/api/meeting-types/${id}`, {
    method: "DELETE",
    credentials: "include",
    headers: { ...csrfHeader() },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}
