"use client";

/**
 * Calendar grid of free intervals. Defaults to 7-day week on desktop, 3-day
 * range on tablet, single-day on phone, with a Day/3/Week toggle. Each day
 * is a column; the time axis runs vertically. Adjacent slots ≤15 min apart
 * merge into a single block — a 1-hour search returning slots every 15 min
 * from 08:00 to 16:00 collapses to one block "08:00 – 17:00" rather than
 * 33 stacked tiles.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { T, type Locale } from "@/lib/i18n";
import type { Slot } from "@/lib/search";

const HOUR_PX = 48;          // pixel height of one hour row
const GAP_GRACE_MIN = 15;    // adjacent slots within this gap merge
const DEFAULT_MIN_HOUR = 7;
const DEFAULT_MAX_HOUR = 19;

type FreeInterval = { start: Date; end: Date };
type ViewDays = 1 | 3 | 7;

/** A user-managed "I'm not available" block. Rendered on the calendar
 * as an amber band — distinct from the green free intervals. */
export type UnavailabilityBlock = {
  id: number;
  label: string;
  starts_at: string;
  ends_at: string;
  is_all_day: boolean;
};

function defaultViewForWidth(w: number): ViewDays {
  if (w < 640) return 1;
  if (w < 1024) return 3;
  return 7;
}

export function SlotsCalendar({
  slots,
  durationMin,
  holidays,
  workingHoursRange,
  unavailabilityBlocks,
  onDeleteUnavailability,
  stickyView = false,
  onIntervalClick,
  locale = "en",
}: {
  slots: Slot[];
  durationMin: number;
  /** YYYY-MM-DD → public-holiday name. Days listed get an amber tint and a
   * small flag in the header so users notice when the slot they're picking
   * lands on a national holiday. */
  holidays?: Map<string, string>;
  /** Optional [startHour, endHour] (0-24, hour granularity) of the user's
   * working day. When provided, the time axis includes at least this range
   * so the user can see the "lead-up" to their first free slot — otherwise
   * a working day with one 9:00 free slot would clip the axis to 9-..., not
   * showing the 8:00-9:00 busy band before it. */
  workingHoursRange?: [number, number];
  /** User's manual "I'm out" blocks. Rendered as amber bands on the days
   * they cover. When `onDeleteUnavailability` is also provided, clicking
   * a band confirms then deletes. */
  unavailabilityBlocks?: UnavailabilityBlock[];
  onDeleteUnavailability?: (id: number) => void | Promise<void>;
  /** When true, auto-anchor fires only on the first non-empty slot load —
   * subsequent slot changes (e.g. a parent-level filter toggle) leave the
   * user's current viewStart untouched. Default false keeps the existing
   * "jump to first result on every change" behavior for pages like /search. */
  stickyView?: boolean;
  /** When provided, clicking a free-interval block calls this instead of
   * the default clipboard-copy behavior. The interval spans the whole free
   * window on that day (potentially many hours) — the receiver picks the
   * actual meeting sub-slot. */
  onIntervalClick?: (interval: { start: Date; end: Date }) => void;
  /** Visitor language for the public booking pages. Omit on authed pages
   * (defaults to English, matching the rest of the authed app). */
  locale?: Locale;
}) {
  const tr = T[locale];
  const bcp47 = locale === "cs" ? "cs-CZ" : "en-US";
  const VIEW_LABEL: Record<ViewDays, string> = {
    1: tr.viewDay,
    3: tr.viewThreeDays,
    7: tr.viewWeek,
  };
  const intervalsByDay = useMemo(() => groupAndMerge(slots), [slots]);
  const blocksByDay = useMemo(
    () => unavailabilityByDay(unavailabilityBlocks ?? []),
    [unavailabilityBlocks],
  );
  // Re-compute "now" each minute so a calendar left open over time still
  // hides slots that have just lapsed.
  const [_now, setNow] = useState<Date>(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);

  // viewDays — start with desktop default during SSR; pick a sensible
  // default on mount based on real window width. After the user explicitly
  // toggles, we stop overriding so a phone scroll (which fires `resize`
  // when the URL bar hides) doesn't bounce them back to 1d view.
  const [viewDays, _setViewDays] = useState<ViewDays>(7);
  const [userPicked, setUserPicked] = useState(false);
  function setViewDays(v: ViewDays) {
    _setViewDays(v);
    setUserPicked(true);
  }
  useEffect(() => {
    if (userPicked) return;
    _setViewDays(defaultViewForWidth(window.innerWidth));
  }, [userPicked]);

  // Default the calendar to the start of the period that contains the first slot.
  const sortedDayKeys = useMemo(
    () => Array.from(intervalsByDay.keys()).sort(),
    [intervalsByDay],
  );
  const [viewStart, setViewStart] = useState<Date>(() => startOfPeriod(new Date(), 7));

  // Anchor viewStart to the first slot's period on data load. In sticky mode
  // this fires exactly once (the first non-empty payload) — good for pages
  // like /people/<id> where a filter toggle swaps the slot set and the user
  // wants to stay on whatever week they'd navigated to.
  const anchoredRef = useRef(false);
  useEffect(() => {
    if (sortedDayKeys.length === 0) return;
    if (stickyView && anchoredRef.current) return;
    anchoredRef.current = true;
    setViewStart((prev) => {
      const visibleKeys = new Set(
        Array.from({ length: viewDays }, (_, i) => toDayKey(addDays(prev, i))),
      );
      if (sortedDayKeys.some((k) => visibleKeys.has(k))) return prev;
      return startOfPeriod(parseDayKey(sortedDayKeys[0]), viewDays);
    });
  }, [sortedDayKeys, viewDays, stickyView]);

  // When the viewDays changes (toggle clicked or screen resize), re-anchor
  // viewStart so the visible range still includes "today" if we were on it.
  useEffect(() => {
    setViewStart((prev) => startOfPeriod(prev, viewDays));
  }, [viewDays]);

  const visibleDays = Array.from({ length: viewDays }, (_, i) => addDays(viewStart, i));

  // Auto-fit time axis. Start from working-hours range when supplied, then
  // expand to cover any slots that fall outside. With no working hours and
  // no slots, fall back to the global default 7-19 band.
  const visibleIntervals = visibleDays.flatMap((d) => intervalsByDay.get(toDayKey(d)) ?? []);
  const slotMin = visibleIntervals.length
    ? Math.min(...visibleIntervals.map((i) => i.start.getHours()))
    : null;
  const slotMax = visibleIntervals.length
    ? Math.max(...visibleIntervals.map((i) => Math.ceil(toMinutes(i.end) / 60)))
    : null;
  const candidates = [
    workingHoursRange?.[0] ?? null,
    slotMin,
  ].filter((n): n is number => n !== null);
  const candidatesMax = [
    workingHoursRange?.[1] ?? null,
    slotMax,
  ].filter((n): n is number => n !== null);
  const minHour = Math.max(
    0,
    candidates.length ? Math.min(...candidates) : DEFAULT_MIN_HOUR,
  );
  const maxHour = Math.min(
    24,
    candidatesMax.length ? Math.max(...candidatesMax) : DEFAULT_MAX_HOUR,
  );
  const hours = Array.from({ length: Math.max(0, maxHour - minHour) }, (_, i) => minHour + i);

  function nudge(delta: -1 | 1) {
    setViewStart((s) => addDays(s, viewDays * delta));
  }

  const isToday = sameDay(viewStart, startOfPeriod(new Date(), viewDays));
  const viewEnd = addDays(viewStart, viewDays - 1);
  const rangeLabel = formatRange(viewStart, viewEnd, viewDays, bcp47);
  const isoWk = isoWeekNumber(viewStart);

  return (
    <section className="overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      {/* navigation — flex-1 outer cells anchor the day-toggle in the true
          center; the range label lives inside the right cell and its dynamic
          width no longer shifts the toggle horizontally. */}
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 px-3 py-3 dark:border-zinc-800 sm:px-4">
        <div className="flex items-center gap-1.5 sm:flex-1 sm:justify-start">
          <button
            type="button"
            onClick={() => nudge(-1)}
            aria-label={viewDays === 1 ? tr.previousDay : viewDays === 3 ? tr.previousThreeDays : tr.previousWeek}
            className="rounded-md border border-zinc-200 px-2.5 py-1 text-sm leading-none hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            ‹
          </button>
          <button
            type="button"
            onClick={() => setViewStart(startOfPeriod(new Date(), viewDays))}
            disabled={isToday}
            className="rounded-md border border-zinc-200 px-2.5 py-1 text-xs font-medium hover:bg-zinc-50 disabled:opacity-40 disabled:hover:bg-transparent dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            {tr.todayLabel}
          </button>
          <button
            type="button"
            onClick={() => nudge(1)}
            aria-label={viewDays === 1 ? tr.nextDay : viewDays === 3 ? tr.nextThreeDays : tr.nextWeek}
            className="rounded-md border border-zinc-200 px-2.5 py-1 text-sm leading-none hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            ›
          </button>
        </div>

        {/* View toggle */}
        <div className="flex shrink-0 rounded-md border border-zinc-200 dark:border-zinc-700">
          {([1, 3, 7] as ViewDays[]).map((v, i) => {
            const active = v === viewDays;
            return (
              <button
                key={v}
                type="button"
                onClick={() => setViewDays(v)}
                className={
                  "px-2 py-1 text-xs font-medium transition-colors " +
                  (i === 0 ? "rounded-l-md " : "") +
                  (i === 2 ? "rounded-r-md " : "") +
                  (i !== 0 ? "border-l border-zinc-200 dark:border-zinc-700 " : "") +
                  (active
                    ? "bg-indigo-600 text-white dark:bg-indigo-500"
                    : "text-zinc-600 hover:bg-zinc-50 dark:text-zinc-300 dark:hover:bg-zinc-800")
                }
                aria-pressed={active}
              >
                {VIEW_LABEL[v]}
              </button>
            );
          })}
        </div>

        <h3 className="min-w-0 basis-full truncate text-sm font-medium text-zinc-900 dark:text-zinc-50 sm:basis-0 sm:flex-1 sm:text-right">
          {viewDays === 7 && (
            <>
              {tr.weekLabel(isoWk)}
              <span className="text-zinc-400"> · </span>
            </>
          )}
          <span className="font-normal text-zinc-600 dark:text-zinc-400">{rangeLabel}</span>
        </h3>
      </header>

      {/* day headers */}
      <div
        className="grid border-b border-zinc-100 dark:border-zinc-800"
        style={{ gridTemplateColumns: `48px repeat(${viewDays}, minmax(0, 1fr))` }}
      >
        <div />
        {visibleDays.map((day) => {
          const isCurrent = sameDay(day, new Date());
          const holidayName = holidays?.get(toDayKey(day));
          const bg = holidayName
            ? "bg-amber-50 dark:bg-amber-950/30"
            : isCurrent
              ? "bg-zinc-50 dark:bg-zinc-800/30"
              : "";
          return (
            <div
              key={day.toISOString()}
              className={`border-l border-zinc-100 px-2 py-2 text-center dark:border-zinc-800 ${bg}`}
              title={holidayName ? tr.publicHolidayTooltip(holidayName) : undefined}
            >
              <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                {day.toLocaleDateString(bcp47, { weekday: "short" })}
              </div>
              <div
                className={
                  "text-sm " +
                  (isCurrent
                    ? "font-bold text-zinc-900 dark:text-zinc-50"
                    : "text-zinc-700 dark:text-zinc-300")
                }
              >
                {day.toLocaleDateString(bcp47, { day: "numeric", month: "short" })}
              </div>
              {holidayName && (
                <div className="mt-0.5 truncate text-[10px] font-medium text-amber-700 dark:text-amber-300">
                  ⚑ {holidayName}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* time grid */}
      <div
        className="grid"
        style={{ gridTemplateColumns: `48px repeat(${viewDays}, minmax(0, 1fr))` }}
      >
        {/* time axis — gridlines live inside day columns, not here */}
        <div>
          {hours.map((h) => (
            <div
              key={h}
              style={{ height: HOUR_PX }}
              className="relative pr-2 text-right text-[10px] text-zinc-500"
            >
              <span className="absolute top-0.5 right-2 leading-none">{String(h).padStart(2, "0")}:00</span>
            </div>
          ))}
        </div>

        {/* day columns */}
        {visibleDays.map((day) => {
          const rawIntervals = intervalsByDay.get(toDayKey(day)) ?? [];
          // For today, intervals that have already ended are useless to
          // show, and intervals that are mid-flight should start at "now"
          // (so the visualisation matches reality, not the historical
          // working-hours window). Other days render unchanged.
          const intervals = sameDay(day, _now)
            ? rawIntervals
                .filter((iv) => iv.end.getTime() > _now.getTime())
                .map((iv) =>
                  iv.start.getTime() < _now.getTime()
                    ? { start: _now, end: iv.end }
                    : iv,
                )
            : rawIntervals;
          const isHoliday = holidays?.has(toDayKey(day)) ?? false;
          return (
            <div
              key={day.toISOString()}
              className={
                "relative border-l border-zinc-100 dark:border-zinc-800 " +
                (isHoliday ? "bg-amber-50/40 dark:bg-amber-950/10" : "")
              }
              style={{ height: hours.length * HOUR_PX }}
            >
              {/* hour gridlines */}
              {hours.map((h, i) => (
                <div
                  key={h}
                  className="absolute left-0 right-0 border-t border-zinc-100 dark:border-zinc-800"
                  style={{ top: i * HOUR_PX }}
                />
              ))}
              {/* user's unavailability blocks (drawn under free intervals so
                  click priority on overlapping free wins, though they shouldn't
                  overlap in practice — the search engine already excludes
                  unavailability from busy). */}
              {(blocksByDay.get(toDayKey(day)) ?? []).map((b) => {
                const top = b.isAllDay
                  ? 0
                  : Math.max(0, ((b.dayStartMin - minHour * 60) * HOUR_PX) / 60);
                const height = b.isAllDay
                  ? hours.length * HOUR_PX
                  : Math.max(18, ((b.dayEndMin - b.dayStartMin) * HOUR_PX) / 60);
                const clickable = !!onDeleteUnavailability;
                return (
                  <div
                    key={`u-${b.id}-${day.toISOString()}`}
                    className="absolute inset-x-1 overflow-hidden rounded border border-amber-300 bg-amber-100 px-1 py-0.5 text-[11px] leading-tight text-amber-900 dark:border-amber-700/60 dark:bg-amber-900/40 dark:text-amber-100"
                    style={{ top, height }}
                    title={b.label}
                  >
                    <div className="pr-5 font-medium">
                      {b.label}
                      {b.isAllDay && (
                        <span className="ml-1 text-[10px] opacity-70">{tr.allDayLabel}</span>
                      )}
                    </div>
                    {clickable && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (!confirm(`Delete "${b.label}"?`)) return;
                          void onDeleteUnavailability(b.id);
                        }}
                        aria-label={`Delete "${b.label}"`}
                        className="absolute right-0.5 top-0.5 inline-flex h-5 w-5 items-center justify-center rounded text-amber-700 opacity-70 transition-opacity hover:bg-amber-200 hover:opacity-100 active:bg-amber-300 dark:text-amber-200 dark:hover:bg-amber-900/70"
                      >
                        <Trash2 size={12} aria-hidden />
                      </button>
                    )}
                  </div>
                );
              })}
              {/* free interval blocks */}
              {intervals.map((iv, i) => {
                const startMin = toMinutes(iv.start);
                const endMin = toMinutes(iv.end);
                const top = ((startMin - minHour * 60) * HOUR_PX) / 60;
                const height = Math.max(18, ((endMin - startMin) * HOUR_PX) / 60);
                return (
                  <div
                    key={i}
                    className="absolute inset-x-1 cursor-pointer overflow-hidden rounded border border-emerald-300 bg-emerald-100 px-1 py-0.5 text-[11px] leading-tight text-emerald-900 transition-colors hover:bg-emerald-200 dark:border-emerald-700/60 dark:bg-emerald-900/40 dark:text-emerald-100 dark:hover:bg-emerald-900/60"
                    style={{ top, height }}
                    title={
                      onIntervalClick
                        ? tr.slotTooltipBookable(
                            durationMin,
                            formatHM(iv.start),
                            formatHM(iv.end),
                            formatHM(new Date(iv.end.getTime() - durationMin * 60_000)),
                          )
                        : tr.slotTooltipCopyable(
                            durationMin,
                            formatHM(iv.start),
                            formatHM(iv.end),
                            formatHM(new Date(iv.end.getTime() - durationMin * 60_000)),
                          )
                    }
                    onClick={() => {
                      if (onIntervalClick) {
                        onIntervalClick({ start: iv.start, end: iv.end });
                        return;
                      }
                      navigator.clipboard
                        ?.writeText(
                          tr.freeIntervalTooltip(
                            `${day.toLocaleDateString(bcp47, { weekday: "short", day: "numeric", month: "short" })}, ${formatHM(iv.start)}–${formatHM(iv.end)}`,
                          ),
                        )
                        .catch(() => {});
                    }}
                  >
                    <div className="font-medium">
                      {formatHM(iv.start)} – {formatHM(iv.end)}
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/** For each visible day, return the unavailability block portions that
 * overlap it. A multi-day all-day block contributes one entry per day it
 * spans; a time-bound block is clipped to the day window. */
type BlockInDay = {
  id: number;
  label: string;
  isAllDay: boolean;
  dayStartMin: number;  // minutes from 00:00 local
  dayEndMin: number;
};

function unavailabilityByDay(blocks: UnavailabilityBlock[]): Map<string, BlockInDay[]> {
  const out = new Map<string, BlockInDay[]>();
  for (const b of blocks) {
    const start = new Date(b.starts_at);
    const end = new Date(b.ends_at);
    if (end <= start) continue;
    // Walk each day from start's local midnight to (end's local) midnight.
    const cursor = new Date(start);
    cursor.setHours(0, 0, 0, 0);
    while (cursor < end) {
      const dayStart = new Date(cursor);
      const dayEnd = new Date(cursor);
      dayEnd.setDate(dayEnd.getDate() + 1);
      const segStart = start > dayStart ? start : dayStart;
      const segEnd = end < dayEnd ? end : dayEnd;
      if (segEnd > segStart) {
        const dayMs = dayStart.getTime();
        const startMin = Math.floor((segStart.getTime() - dayMs) / 60_000);
        const endMin = Math.ceil((segEnd.getTime() - dayMs) / 60_000);
        const key = toDayKey(cursor);
        const list = out.get(key) ?? [];
        list.push({
          id: b.id,
          label: b.label,
          isAllDay: b.is_all_day,
          dayStartMin: startMin,
          dayEndMin: endMin,
        });
        out.set(key, list);
      }
      cursor.setDate(cursor.getDate() + 1);
    }
  }
  return out;
}

function groupAndMerge(slots: Slot[]): Map<string, FreeInterval[]> {
  const byDay = new Map<string, FreeInterval[]>();
  if (slots.length === 0) return byDay;

  const sorted = [...slots]
    .map((s) => ({ start: new Date(s.start), end: new Date(s.end) }))
    .sort((a, b) => a.start.getTime() - b.start.getTime());

  for (const s of sorted) {
    const key = toDayKey(s.start);
    const list = byDay.get(key) ?? [];
    if (list.length > 0) {
      const last = list[list.length - 1];
      if (s.start.getTime() <= last.end.getTime() + GAP_GRACE_MIN * 60_000) {
        last.end = new Date(Math.max(last.end.getTime(), s.end.getTime()));
        continue;
      }
    }
    list.push({ start: s.start, end: s.end });
    byDay.set(key, list);
  }

  return byDay;
}

/** Anchor a date to the start of its current view window:
 *  - 7-day → Monday of the week
 *  - 3-day → keep the day as-is (3-day window starts on the given day)
 *  - 1-day → keep the day as-is */
function startOfPeriod(d: Date, viewDays: ViewDays): Date {
  if (viewDays === 7) return mondayOf(d);
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  return out;
}

function mondayOf(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  // getDay(): 0=Sunday, 1=Monday, ..., 6=Saturday
  const offset = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - offset);
  return out;
}

function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function toDayKey(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function parseDayKey(s: string): Date {
  return new Date(`${s}T00:00:00`);
}

function toMinutes(d: Date): number {
  return d.getHours() * 60 + d.getMinutes();
}

function formatHM(d: Date): string {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** ISO 8601 week number (Czech "kalendářní týden"). Monday-based, the week
 * containing the year's first Thursday is week 1. */
function isoWeekNumber(d: Date): number {
  const target = new Date(d.valueOf());
  target.setHours(0, 0, 0, 0);
  const dayOfWeek = (target.getDay() + 6) % 7;
  target.setDate(target.getDate() - dayOfWeek + 3);
  const firstThursday = new Date(target.getFullYear(), 0, 4);
  const diffDays = (target.getTime() - firstThursday.getTime()) / 86_400_000;
  return 1 + Math.floor(diffDays / 7);
}

function formatRange(start: Date, end: Date, viewDays: ViewDays, bcp47: string = "en-US"): string {
  const day = (d: Date) => d.getDate();
  const monthShort = (d: Date) => d.toLocaleDateString(bcp47, { month: "short" });
  const year = (d: Date) => d.getFullYear();

  if (viewDays === 1) {
    return start.toLocaleDateString(bcp47, {
      weekday: "long",
      day: "numeric",
      month: "short",
      year: "numeric",
    });
  }

  const sameYear = start.getFullYear() === end.getFullYear();
  const sameMonth = sameYear && start.getMonth() === end.getMonth();
  if (sameMonth) {
    return `${day(start)}–${day(end)} ${monthShort(end)} ${year(end)}`;
  }
  if (sameYear) {
    return `${day(start)} ${monthShort(start)} – ${day(end)} ${monthShort(end)} ${year(end)}`;
  }
  return `${day(start)} ${monthShort(start)} ${year(start)} – ${day(end)} ${monthShort(end)} ${year(end)}`;
}
