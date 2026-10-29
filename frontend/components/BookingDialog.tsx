"use client";

/**
 * Modal for turning a clicked free interval into an actual meeting. Used
 * from two places:
 *   - /people/[id]  → authed booking with a connected teammate (peer known)
 *   - /u/[token]    → public booking by an anonymous visitor (self-identify)
 *
 * Layout: bottom-sheet on phones, centered card on tablet+. Big visual
 * Online/In-person tiles; time slot picker as chips (falls back to a
 * scrollable dropdown when there are too many); duration chips limited to
 * options that actually fit the clicked free interval so the user can't
 * assemble a nonsense combo.
 */

import { useEffect, useMemo, useState } from "react";
import { MapPin, Video, X as XIcon } from "lucide-react";
import { AddressAutocomplete } from "@/components/AddressAutocomplete";
import { Button, FormError, Input } from "@/components/ui";
import type { MeetingTypeQuestion } from "@/lib/meeting-types";

export type BookingKind = "online" | "physical";

export type BookingSubmit = {
  startIso: string;
  endIso: string;
  title: string;
  notes: string;
  kind: BookingKind;
  /** Non-empty only for physical bookings — the address/room. */
  location?: string;
  visitorName?: string;
  visitorEmail?: string;
  /** Answers to a MeetingType's custom questions, keyed by question id.
   * Empty for the generic (no-type) flow. */
  customAnswers?: Record<string, string>;
};

const ALL_DURATIONS = [15, 30, 45, 60, 90, 120];
const START_STEP_MIN = 15;
const CHIP_LIMIT = 12; // switch to a dropdown when there are more slots than this

export type LockedMeetingType = {
  name: string;
  duration_min: number;
  kind: "online" | "physical";
  location?: string;
  description?: string;
  questions?: MeetingTypeQuestion[];
  /** Nullable FK to a saved UserLocation (G5). When set, it pre-selects
   * the matching entry in the host_locations picker (if present), or falls
   * back to showing the free-form input prefilled with the address. */
  default_location?: { id: number; name: string; address: string } | null;
};

export type HostLocation = {
  id: number;
  name: string;
  address: string;
  is_default: boolean;
};

export function BookingDialog({
  open,
  onClose,
  interval,
  mode,
  defaultTitle,
  defaultDurationMin = 30,
  submitting,
  errorMessage,
  successMessage,
  onSubmit,
  hostName,
  allowPhysical = false,
  lockedType = null,
  hostLocations = [],
  defaultKind = "online",
}: {
  open: boolean;
  onClose: () => void;
  interval: { start: Date; end: Date } | null;
  mode: "authed" | "public";
  defaultTitle: string;
  defaultDurationMin?: number;
  submitting: boolean;
  errorMessage?: string | null;
  /** Rendered inside the dialog after a successful booking. Accepts JSX
   * so callers can inject action buttons (Join meeting, Add to calendar,
   * Manage booking) alongside the confirmation text. */
  successMessage?: React.ReactNode;
  onSubmit: (v: BookingSubmit) => void;
  /** Only used in public mode — shown to visitors so they know whose
   * calendar they're booking. */
  hostName?: string;
  /** Show the Online / In-person selector. Physical requests need host
   * approval; online books immediately. Only exposed on the public flow. */
  allowPhysical?: boolean;
  /** When set, kind + duration are dictated by the host-defined meeting
   * type — no toggles, single-choice chip pickers, title auto-fills to
   * the type's name. Overrides `allowPhysical`. */
  lockedType?: LockedMeetingType | null;
  /** Host's predefined in-person locations. When non-empty and kind is
   * "physical", a picker is shown above the free-form address input. */
  hostLocations?: HostLocation[];
  /** Initial kind when the dialog opens in the generic (no-type) flow.
   * Defaults to "online". Pass "physical" for in-person-only hosts. */
  defaultKind?: BookingKind;
}) {
  const [title, setTitle] = useState(defaultTitle);
  const [notes, setNotes] = useState("");
  const [durationMin, setDurationMin] = useState(defaultDurationMin);
  const [startMin, setStartMin] = useState<number | null>(null);
  const [visitorName, setVisitorName] = useState("");
  const [visitorEmail, setVisitorEmail] = useState("");
  const [kind, setKind] = useState<BookingKind>("online");
  const [location, setLocation] = useState("");
  // Notes hidden by default — most bookings don't need them, and burying
  // the textarea behind a "+ Add note" toggle removes ~120px of default
  // dialog height (biggest single-source of vertical scroll on laptops).
  const [showNotes, setShowNotes] = useState(false);
  // Answers to MeetingType.questions, keyed by question id. Populated only
  // when lockedType has questions; ignored on generic bookings.
  const [answers, setAnswers] = useState<Record<string, string>>({});
  // Honeypot — off-screen text field that legit users never see. Bots
  // that greedily fill every input trip it and get silently 204'd.
  const [hp, setHp] = useState("");
  // Selected host location id, or "custom" when the visitor wants to type
  // their own address. null means the picker hasn't been shown yet (no host
  // locations, or not physical kind).
  const [pickedLocationId, setPickedLocationId] = useState<number | "custom" | null>(null);

  // Only offer durations that actually fit the clicked free block, so the
  // user can't pick "2h" inside a 30-minute gap and hit a confusing error
  // after submit. If none fit (tiny block), we fall back to allowing the
  // block's whole length as the one option.
  const intervalLenMin = useMemo(() => {
    if (!interval) return 0;
    return Math.floor((interval.end.getTime() - interval.start.getTime()) / 60_000);
  }, [interval]);

  const durationOptions = useMemo(() => {
    if (intervalLenMin <= 0) return [] as number[];
    if (lockedType) {
      // Locked: only the type's own duration is offered (still gated on
      // whether it fits the clicked free block; if not, no start options
      // will render and the visitor sees the "no fit" message).
      return lockedType.duration_min <= intervalLenMin ? [lockedType.duration_min] : [];
    }
    const fits = ALL_DURATIONS.filter((d) => d <= intervalLenMin);
    return fits.length > 0 ? fits : [intervalLenMin];
  }, [intervalLenMin, lockedType]);

  const startOptions = useMemo(() => {
    if (!interval) return [] as number[];
    const first = minutesInDay(interval.start);
    const lastAllowed = minutesInDay(interval.end) - durationMin;
    if (lastAllowed < first) return [];
    const first15 = Math.ceil(first / START_STEP_MIN) * START_STEP_MIN;
    const out: number[] = [];
    for (let m = first15; m <= lastAllowed; m += START_STEP_MIN) out.push(m);
    if (out.length === 0 || out[0] !== first) {
      if (first <= lastAllowed) out.unshift(first);
    }
    return out;
  }, [interval, durationMin]);

  // Reset per-open — dialog opens fresh each time. Also auto-pick sensible
  // defaults for duration + start based on the newly-clicked interval.
  useEffect(() => {
    if (!open || !interval) return;
    setTitle(lockedType?.name ?? defaultTitle);
    setNotes("");
    setVisitorName("");
    setVisitorEmail("");
    const initialKind = lockedType?.kind ?? defaultKind;
    setKind(initialKind);
    setHp("");
    setShowNotes(false);
    setAnswers({});
    const initialDuration = lockedType
      ? lockedType.duration_min
      : (ALL_DURATIONS.find((x) => x === defaultDurationMin && x <= intervalLenMin)
        ?? ALL_DURATIONS.filter((x) => x <= intervalLenMin).slice(-1)[0]
        ?? intervalLenMin);
    setDurationMin(initialDuration);
    setStartMin(null);

    // Location picker default selection for physical kind.
    // Priority:
    //   1. lockedType.default_location FK — try to find it in hostLocations
    //      to drive the radio/select picker; if not found (defensive), fall
    //      back to the free-form input prefilled with the address.
    //   2. lockedType.location string — free-form pre-fill (legacy / no FK).
    //   3. host_locations default/first — pre-select the default saved loc.
    //   4. Nothing — empty free-form.
    if (lockedType?.default_location) {
      const dl = lockedType.default_location;
      const inList = hostLocations.find((l) => l.id === dl.id);
      if (inList) {
        setPickedLocationId(inList.id);
        setLocation(inList.address);
      } else {
        // The saved location isn't in host_locations (shouldn't happen, but
        // be defensive). Show the free-form input prefilled with the address.
        setPickedLocationId(null);
        setLocation(dl.address);
      }
    } else if (lockedType?.location) {
      setLocation(lockedType.location);
      setPickedLocationId(null);
    } else if (hostLocations.length > 0) {
      const defaultLoc = hostLocations.find((l) => l.is_default) ?? hostLocations[0];
      setPickedLocationId(defaultLoc.id);
      setLocation(defaultLoc.address);
    } else {
      setLocation("");
      setPickedLocationId(null);
    }
  }, [open, interval, defaultTitle, defaultDurationMin, intervalLenMin, lockedType, hostLocations, defaultKind]);

  // If the current start becomes invalid (duration change shifts the last-
  // allowed start earlier), snap to the earliest still-valid option.
  useEffect(() => {
    if (!open || !interval || startOptions.length === 0) return;
    if (startMin === null || !startOptions.includes(startMin)) {
      setStartMin(startOptions[0]);
    }
  }, [open, interval, startOptions, startMin]);

  // When kind transitions TO "physical", ensure pickedLocationId points to a
  // valid host location (or "custom"). If it's stale (e.g. null or an id that
  // no longer exists), snap to the host's default (or first) location.
  // We do NOT reset when the user has explicitly chosen "custom" — that is a
  // deliberate selection and should survive a round-trip online→physical.
  useEffect(function syncPhysicalLocation() {
    if (!open || kind !== "physical" || hostLocations.length === 0) return;
    const isValidId = typeof pickedLocationId === "number" && hostLocations.some((l) => l.id === pickedLocationId);
    if (!isValidId && pickedLocationId !== "custom") {
      const defaultLoc = hostLocations.find((l) => l.is_default) ?? hostLocations[0];
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPickedLocationId(defaultLoc.id);
      setLocation(defaultLoc.address);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, hostLocations]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !submitting) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, submitting]);

  // Compute BEFORE the early return so the hooks count is stable across
  // open/closed renders — otherwise React throws "Rendered more hooks
  // than during the previous render" the first time the dialog opens.
  const requiredQuestionsAnswered = useMemo(() => {
    const qs = lockedType?.questions ?? [];
    return qs.every((q) => !q.required || (answers[q.id] ?? "").trim().length > 0);
  }, [lockedType, answers]);

  if (!open || !interval) return null;

  const dayLabel = interval.start.toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
  });

  const canSubmit =
    startMin !== null &&
    startOptions.length > 0 &&
    title.trim().length > 0 &&
    !submitting &&
    (kind !== "physical" || location.trim().length > 0) &&
    (mode === "authed" || (visitorName.trim().length > 0 && isEmail(visitorEmail))) &&
    requiredQuestionsAnswered;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit || startMin === null || !interval) return;
    const start = new Date(interval.start);
    start.setHours(0, 0, 0, 0);
    start.setMinutes(startMin);
    const end = new Date(start.getTime() + durationMin * 60_000);
    onSubmit({
      startIso: toLocalIso(start),
      endIso: toLocalIso(end),
      title: title.trim(),
      notes: notes.trim(),
      kind,
      location: kind === "physical" ? location.trim() : undefined,
      visitorName: visitorName.trim() || undefined,
      visitorEmail: visitorEmail.trim() || undefined,
      customAnswers:
        lockedType?.questions?.length
          ? Object.fromEntries(
              lockedType.questions
                .map((q) => [q.id, (answers[q.id] ?? "").trim()])
                .filter(([, v]) => v.length > 0),
            )
          : undefined,
    });
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3 sm:p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-label={mode === "public" ? "Book a meeting" : "Create meeting"}
    >
      <form
        onSubmit={handleSubmit}
        className="flex max-h-[calc(100dvh-1.5rem)] w-full max-w-md flex-col overflow-hidden rounded-2xl border border-zinc-200 bg-white shadow-2xl dark:border-zinc-800 dark:bg-zinc-900 sm:max-h-[92vh] sm:max-w-lg"
      >
        {/* Header — sticky, compact. */}
        <header className="flex items-center gap-2 border-b border-zinc-100 px-4 py-2.5 dark:border-zinc-800 sm:px-5 sm:py-3">
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-[15px] font-semibold text-zinc-900 dark:text-zinc-50 sm:text-base">
              {mode === "public"
                ? hostName
                  ? `Book with ${hostName}`
                  : "Book a meeting"
                : "New meeting"}
            </h2>
            <p className="truncate text-[11px] text-zinc-500 dark:text-zinc-400 sm:text-xs">
              {dayLabel}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 disabled:opacity-40 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            aria-label="Close"
          >
            <XIcon size={18} aria-hidden />
          </button>
        </header>

        {/* Body — everything shown at once. If the total exceeds the
            dialog's max-h, the body scrolls internally with a thin,
            theme-aware scrollbar (never the default black bar on dark
            surfaces). */}
        <div className="flex-1 space-y-3 overflow-y-auto overscroll-contain px-4 py-3 [scrollbar-width:thin] [scrollbar-color:theme(colors.zinc.300)_transparent] [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-thumb]:rounded [&::-webkit-scrollbar-thumb]:bg-zinc-300 [&::-webkit-scrollbar-track]:bg-transparent dark:[scrollbar-color:theme(colors.zinc.700)_transparent] dark:[&::-webkit-scrollbar-thumb]:bg-zinc-700 sm:space-y-4 sm:px-5 sm:py-4">
          {/* Locked-type flow shows a static banner instead of the toggle so
              the visitor sees which preset they're booking. */}
          {lockedType ? (
            <div
              className={
                "rounded-lg border p-3 " +
                (lockedType.kind === "physical"
                  ? "border-amber-300 bg-amber-50 dark:border-amber-800/60 dark:bg-amber-950/30"
                  : "border-indigo-300 bg-indigo-50 dark:border-indigo-800/60 dark:bg-indigo-950/30")
              }
            >
              <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900 dark:text-zinc-50">
                {lockedType.kind === "physical" ? (
                  <MapPin size={14} aria-hidden className="text-amber-700 dark:text-amber-300" />
                ) : (
                  <Video size={14} aria-hidden className="text-indigo-600 dark:text-indigo-300" />
                )}
                {lockedType.name} · {lockedType.duration_min} min
              </div>
              {lockedType.description && (
                <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-300">
                  {lockedType.description}
                </p>
              )}
            </div>
          ) : allowPhysical && (
            <fieldset>
              <legend className="sr-only">Meeting type</legend>
              <div className="grid grid-cols-2 gap-2">
                <KindTile
                  active={kind === "online"}
                  onClick={() => setKind("online")}
                  icon={<Video size={14} aria-hidden />}
                  title="Online"
                  sub="Books now · meeting link"
                  tone="indigo"
                  recommended
                />
                <KindTile
                  active={kind === "physical"}
                  onClick={() => setKind("physical")}
                  icon={<MapPin size={14} aria-hidden />}
                  title="In person"
                  sub="Needs host approval"
                  tone="amber"
                />
              </div>
            </fieldset>
          )}

          {kind === "physical" && hostLocations.length > 0 && (
            <div className="space-y-1">
              <MicroLabel id="pick-location-label">Pick a location</MicroLabel>
              {hostLocations.length <= 3 ? (
                // Radio list for 1–3 locations.
                <div role="radiogroup" aria-labelledby="pick-location-label" className="space-y-1.5">
                  {hostLocations.map((loc) => (
                    <label
                      key={loc.id}
                      className={
                        "flex cursor-pointer items-start gap-2.5 rounded-lg border p-2.5 text-sm transition-colors " +
                        (pickedLocationId === loc.id
                          ? "border-amber-500 bg-amber-50 dark:border-amber-600 dark:bg-amber-950/30"
                          : "border-zinc-200 bg-white hover:border-zinc-300 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:border-zinc-600")
                      }
                    >
                      <input
                        type="radio"
                        name="host_location"
                        value={loc.id}
                        checked={pickedLocationId === loc.id}
                        onChange={() => {
                          setPickedLocationId(loc.id);
                          setLocation(loc.address);
                        }}
                        className="mt-0.5 accent-amber-600 shrink-0"
                      />
                      <div className="min-w-0">
                        <div className="font-medium text-zinc-900 dark:text-zinc-50">
                          {loc.name}
                        </div>
                        <div className="text-xs text-zinc-500 dark:text-zinc-400 truncate">
                          {loc.address}
                        </div>
                      </div>
                    </label>
                  ))}
                  <label
                    className={
                      "flex cursor-pointer items-center gap-2.5 rounded-lg border p-2.5 text-sm transition-colors " +
                      (pickedLocationId === "custom"
                        ? "border-amber-500 bg-amber-50 dark:border-amber-600 dark:bg-amber-950/30"
                        : "border-zinc-200 bg-white hover:border-zinc-300 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:border-zinc-600")
                    }
                  >
                    <input
                      type="radio"
                      name="host_location"
                      value="custom"
                      checked={pickedLocationId === "custom"}
                      onChange={() => {
                        setPickedLocationId("custom");
                        setLocation("");
                      }}
                      className="accent-amber-600 shrink-0"
                    />
                    <span className="text-zinc-600 dark:text-zinc-400">Enter a different address</span>
                  </label>
                </div>
              ) : (
                // Select dropdown for 4+ locations.
                <select
                  value={pickedLocationId ?? ""}
                  onChange={(e) => {
                    const val = e.target.value;
                    if (val === "custom") {
                      setPickedLocationId("custom");
                      setLocation("");
                    } else {
                      const id = Number(val);
                      const found = hostLocations.find((l) => l.id === id);
                      setPickedLocationId(id);
                      if (found) setLocation(found.address);
                    }
                  }}
                  className="h-10 w-full rounded-md border border-zinc-200 bg-white px-3 text-sm dark:border-zinc-800 dark:bg-zinc-950"
                >
                  {hostLocations.map((loc) => (
                    <option key={loc.id} value={loc.id}>
                      {loc.name} — {loc.address}
                    </option>
                  ))}
                  <option value="custom">Enter a different address…</option>
                </select>
              )}
            </div>
          )}

          {kind === "physical" && (hostLocations.length === 0 || pickedLocationId === "custom") && (
            <div className="space-y-1">
              <MicroLabel htmlFor="location">
                {hostLocations.length > 0 ? "Your address" : "Where"}
              </MicroLabel>
              <AddressAutocomplete
                id="location"
                value={location}
                onChange={(v) => {
                  setLocation(v);
                }}
                placeholder="Address, café, meeting room…"
                required
                autoFocus={pickedLocationId === "custom"}
              />
            </div>
          )}

          <div className="space-y-2">
            <div className="space-y-1">
              <MicroLabel htmlFor="duration-group">Duration</MicroLabel>
              <ChipRow>
                {durationOptions.map((d) => (
                  <ChipButton
                    key={d}
                    active={d === durationMin}
                    onClick={() => setDurationMin(d)}
                    label={formatDuration(d)}
                  />
                ))}
              </ChipRow>
              {durationOptions.length === 1 && durationOptions[0] < 30 && (
                <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                  Only {formatDuration(durationOptions[0])} fits this free
                  block — pick another day for a longer slot.
                </p>
              )}
            </div>

            <div className="space-y-1">
              <MicroLabel htmlFor="start-group">Start</MicroLabel>
              {startOptions.length === 0 ? (
                <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-100">
                  No start time fits the picked duration. Try a shorter one.
                </p>
              ) : startOptions.length <= CHIP_LIMIT ? (
                <ChipRow>
                  {startOptions.map((m) => (
                    <ChipButton
                      key={m}
                      active={m === startMin}
                      onClick={() => setStartMin(m)}
                      label={formatMinutes(m)}
                    />
                  ))}
                </ChipRow>
              ) : (
                <select
                  id="start-group"
                  value={startMin ?? ""}
                  onChange={(e) => setStartMin(Number(e.target.value))}
                  className="h-10 w-full rounded-md border border-zinc-200 bg-white px-3 text-sm dark:border-zinc-800 dark:bg-zinc-950"
                >
                  {startOptions.map((m) => (
                    <option key={m} value={m}>
                      {formatMinutes(m)}
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>

          {mode === "public" && (
            <div className="grid grid-cols-2 gap-2 border-t border-zinc-100 pt-3 sm:gap-3 sm:pt-4 dark:border-zinc-800">
              <div className="space-y-1">
                <MicroLabel htmlFor="visitor-name">Your name</MicroLabel>
                <Input
                  id="visitor-name"
                  value={visitorName}
                  onChange={(e) => setVisitorName(e.target.value)}
                  required
                  autoComplete="name"
                />
              </div>
              <div className="space-y-1">
                <MicroLabel htmlFor="visitor-email">Your email</MicroLabel>
                <Input
                  id="visitor-email"
                  type="email"
                  value={visitorEmail}
                  onChange={(e) => setVisitorEmail(e.target.value)}
                  required
                  autoComplete="email"
                />
              </div>
              {/* Honeypot — off-screen. */}
              <div
                aria-hidden
                className="pointer-events-none absolute -left-[9999px] h-0 w-0 overflow-hidden opacity-0"
              >
                <label>
                  Website (leave empty)
                  <input
                    tabIndex={-1}
                    autoComplete="off"
                    value={hp}
                    onChange={(e) => setHp(e.target.value)}
                  />
                </label>
              </div>
            </div>
          )}

          {lockedType?.questions?.map((q) => (
            <QuestionField
              key={q.id}
              question={q}
              value={answers[q.id] ?? ""}
              onChange={(v) => setAnswers((prev) => ({ ...prev, [q.id]: v }))}
            />
          ))}

          <div className="space-y-1">
            <MicroLabel htmlFor="title">Title</MicroLabel>
            <Input
              id="title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
              maxLength={200}
            />
          </div>

          {showNotes ? (
            <div className="space-y-1">
              <MicroLabel htmlFor="notes">Notes</MicroLabel>
              <textarea
                id="notes"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                rows={2}
                maxLength={2000}
                autoFocus
                placeholder={
                  kind === "physical"
                    ? "Anything the host should know?"
                    : "Agenda, links, context…"
                }
                className="w-full resize-y rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm placeholder:text-zinc-400 dark:border-zinc-800 dark:bg-zinc-950 dark:placeholder:text-zinc-500"
              />
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setShowNotes(true)}
              className="text-xs font-medium text-indigo-600 hover:text-indigo-700 dark:text-indigo-400 dark:hover:text-indigo-300"
            >
              + Add a note
            </button>
          )}

          {errorMessage && <FormError message={errorMessage} />}
          {successMessage && (
            <div className="rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
              {successMessage}
            </div>
          )}
        </div>

        {/* Sticky footer with Cancel + primary action. iOS safe-area
            padding lifts the buttons above the home indicator. */}
        <footer
          className="flex items-center justify-end gap-2 border-t border-zinc-100 bg-white px-4 py-2.5 dark:border-zinc-800 dark:bg-zinc-900 sm:px-5 sm:py-3"
          style={{ paddingBottom: "max(0.625rem, env(safe-area-inset-bottom))" }}
        >
          <Button
            type="button"
            variant="secondary"
            onClick={onClose}
            disabled={submitting}
            className="w-auto px-4"
          >
            Cancel
          </Button>
          <Button
            type="submit"
            disabled={!canSubmit}
            className={
              "w-auto px-5 " +
              (kind === "physical"
                ? "!bg-amber-600 hover:!bg-amber-700 dark:!bg-amber-600 dark:hover:!bg-amber-700"
                : "")
            }
          >
            {submitting
              ? kind === "physical" ? "Sending…" : "Booking…"
              : kind === "physical" ? "Send request" : "Book"}
          </Button>
        </footer>
      </form>
    </div>
  );
}

function KindTile({
  active,
  onClick,
  icon,
  title,
  sub,
  tone,
  recommended = false,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  title: string;
  sub: string;
  tone: "indigo" | "amber";
  recommended?: boolean;
}) {
  const activeClass =
    tone === "indigo"
      ? "border-indigo-500 bg-indigo-50 ring-2 ring-indigo-500 ring-offset-1 dark:border-indigo-400 dark:bg-indigo-950/50 dark:ring-offset-zinc-900"
      : "border-amber-500 bg-amber-50 ring-2 ring-amber-500 ring-offset-1 dark:border-amber-400 dark:bg-amber-950/50 dark:ring-offset-zinc-900";
  const iconClass =
    tone === "indigo"
      ? "text-indigo-600 dark:text-indigo-300"
      : "text-amber-700 dark:text-amber-300";
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        "relative flex flex-col gap-0.5 rounded-lg border p-2.5 text-left transition-all sm:gap-1 sm:p-3 " +
        (active
          ? activeClass
          : "border-zinc-200 bg-white hover:border-zinc-300 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800/60")
      }
    >
      <div className="flex items-center gap-1.5 text-sm font-semibold text-zinc-900 dark:text-zinc-50">
        <span className={iconClass}>{icon}</span>
        {title}
        {recommended && (
          <span className="ml-auto hidden rounded-full bg-indigo-100 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-indigo-700 dark:bg-indigo-950/60 dark:text-indigo-300 sm:inline">
            Recommended
          </span>
        )}
      </div>
      {/* Subtitle: on mobile we sacrifice it for height (tile title +
          icon already carries the meaning); on sm+ we bring it back. */}
      <div className="hidden text-[11px] leading-snug text-zinc-500 dark:text-zinc-400 sm:block">
        {sub}
      </div>
    </button>
  );
}

function QuestionField({
  question,
  value,
  onChange,
}: {
  question: MeetingTypeQuestion;
  value: string;
  onChange: (v: string) => void;
}) {
  const inputId = `q-${question.id}`;
  return (
    <div className="space-y-1">
      <MicroLabel htmlFor={inputId}>
        {question.label}
        {question.required && <span className="ml-1 text-red-500">*</span>}
      </MicroLabel>
      {question.kind === "textarea" ? (
        <textarea
          id={inputId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={2}
          maxLength={2000}
          required={question.required}
          className="w-full resize-y rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-950"
        />
      ) : question.kind === "select" ? (
        <select
          id={inputId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required={question.required}
          className="h-10 w-full rounded-md border border-zinc-200 bg-white px-3 text-sm dark:border-zinc-800 dark:bg-zinc-950"
        >
          <option value="">— pick one —</option>
          {(question.options ?? []).map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
      ) : (
        <Input
          id={inputId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          maxLength={2000}
          required={question.required}
        />
      )}
    </div>
  );
}

/** Micro uppercase label — same info as `<Label>` at a fraction of the
 * vertical footprint. Used inside the dialog to squeeze rows on mobile
 * without hiding what the field means. */
function MicroLabel({ htmlFor, id, children }: { htmlFor?: string; id?: string; children: React.ReactNode }) {
  return (
    <label
      htmlFor={htmlFor}
      id={id}
      className="block text-[10px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400"
    >
      {children}
    </label>
  );
}

/** Chip strip: on mobile it lives on a single horizontally-scrollable
 * row (predictable height regardless of option count). On sm+ chips wrap
 * freely so the desktop view keeps its calm two-row look. */
function ChipRow({ children }: { children: React.ReactNode }) {
  return (
    <div className="-mx-4 flex gap-1.5 overflow-x-auto px-4 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0 sm:pb-0">
      {children}
    </div>
  );
}

function ChipButton({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        "shrink-0 rounded-full border px-3 py-1 text-sm transition-colors " +
        (active
          ? "border-indigo-600 bg-indigo-600 text-white dark:border-indigo-500 dark:bg-indigo-500"
          : "border-zinc-300 bg-white text-zinc-700 hover:border-zinc-400 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800")
      }
    >
      {label}
    </button>
  );
}

function minutesInDay(d: Date): number {
  return d.getHours() * 60 + d.getMinutes();
}

function formatMinutes(m: number): string {
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${String(h).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

function formatDuration(min: number): string {
  if (min < 60) return `${min} min`;
  if (min % 60 === 0) return `${min / 60} h`;
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}

/** Render a Date as ISO 8601 in the browser's local time zone (no Z suffix),
 * so the server treats it as local rather than UTC. */
function toLocalIso(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  const tzOffset = -d.getTimezoneOffset();
  const sign = tzOffset >= 0 ? "+" : "-";
  const abs = Math.abs(tzOffset);
  const oh = pad(Math.floor(abs / 60));
  const om = pad(abs % 60);
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
    `${sign}${oh}:${om}`
  );
}

function isEmail(s: string): boolean {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(s.trim());
}
