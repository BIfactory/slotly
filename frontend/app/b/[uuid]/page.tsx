"use client";

/**
 * /b/<uuid> — visitor-facing "manage your booking" page.
 *
 * The uuid comes from the manage URL Slotly puts in every calendar event
 * we create on the host's behalf. Access control is by unguessable uuid
 * only (no auth) — a leaked link exposes exactly one visitor's own
 * booking, nothing else.
 *
 * Currently supports Cancel only. Reschedule is a follow-up (would swap
 * this page for a picker that reuses the host's public availability view).
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, CalendarClock, CheckCircle2, MapPin, Video } from "lucide-react";
import { LangSwitch } from "@/components/LangSwitch";
import { Logo } from "@/components/Logo";
import { Skeleton } from "@/components/Skeleton";
import { SlotsCalendar } from "@/components/SlotsCalendar";
import { Button, FormError, Label } from "@/components/ui";
import {
  cancelManagedBooking,
  getManagedBooking,
  rescheduleManagedBooking,
  type ManagedBooking,
} from "@/lib/google";
import { T, type Locale, useLocale } from "@/lib/i18n";
import { computeFreeSlots, workingHoursRangeFromHours } from "@/lib/public-profile";

export default function ManageBookingPage() {
  const params = useParams<{ uuid: string }>();
  const uuid = params.uuid;
  const [locale, setLocale] = useLocale();
  const t = T[locale];

  const [state, setState] = useState<
    "loading" | "not_found" | "load_error" | "loaded"
  >("loading");
  const [booking, setBooking] = useState<ManagedBooking | null>(null);
  const [reason, setReason] = useState("");
  const [showConfirm, setShowConfirm] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"detail" | "reschedule">("detail");
  const [reschedulePick, setReschedulePick] = useState<{ start: Date; end: Date } | null>(null);
  const [rescheduleError, setRescheduleError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const b = await getManagedBooking(uuid);
        if (!alive) return;
        if (b === null) {
          setState("not_found");
        } else {
          setBooking(b);
          setState("loaded");
        }
      } catch {
        if (alive) setState("load_error");
      }
    })();
    return () => {
      alive = false;
    };
  }, [uuid]);

  async function onCancel() {
    setSubmitting(true);
    setError(null);
    try {
      const updated = await cancelManagedBooking(uuid, reason);
      setBooking(updated);
      setShowConfirm(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : t.couldntCancel);
    } finally {
      setSubmitting(false);
    }
  }

  async function onReschedule() {
    if (!reschedulePick) return;
    setSubmitting(true);
    setRescheduleError(null);
    try {
      const updated = await rescheduleManagedBooking(uuid, {
        start: toLocalIso(reschedulePick.start),
        end: toLocalIso(reschedulePick.end),
      });
      setBooking(updated);
      setReschedulePick(null);
      setView("detail");
    } catch (err) {
      setRescheduleError(err instanceof Error ? err.message : t.couldntReschedule);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <div className="border-b border-zinc-200 bg-white px-6 py-3 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mx-auto flex max-w-2xl items-center justify-between gap-3">
          <Link href="/" aria-label="Slotly" className="inline-flex items-center">
            <Logo size={20} />
          </Link>
          <LangSwitch locale={locale} onChange={setLocale} />
        </div>
      </div>

      <main className="mx-auto max-w-2xl px-6 py-10">
        {state === "loading" && <ManageBookingSkeleton />}

        {state === "not_found" && (
          <div className="rounded-xl border border-zinc-200 bg-white p-8 text-center dark:border-zinc-800 dark:bg-zinc-900">
            <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-50">
              {t.bookingNotFoundTitle}
            </h1>
            <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
              {t.bookingNotFoundBody}
            </p>
          </div>
        )}

        {state === "load_error" && (
          <FormError message={t.couldntLoadBooking} />
        )}

        {state === "loaded" && booking && view === "detail" && (
          <BookingCard
            booking={booking}
            locale={locale}
            showConfirm={showConfirm}
            onOpenConfirm={() => setShowConfirm(true)}
            onCloseConfirm={() => {
              setShowConfirm(false);
              setError(null);
            }}
            onOpenReschedule={() => {
              setRescheduleError(null);
              setReschedulePick(null);
              setView("reschedule");
            }}
            reason={reason}
            onReasonChange={setReason}
            onCancel={onCancel}
            submitting={submitting}
            error={error}
          />
        )}

        {state === "loaded" && booking && view === "reschedule" && (
          <ReschedulePanel
            booking={booking}
            locale={locale}
            pick={reschedulePick}
            onPick={setReschedulePick}
            onSubmit={onReschedule}
            onBack={() => {
              setView("detail");
              setReschedulePick(null);
              setRescheduleError(null);
            }}
            submitting={submitting}
            error={rescheduleError}
          />
        )}
      </main>
    </div>
  );
}

function BookingCard({
  booking,
  locale,
  showConfirm,
  onOpenConfirm,
  onCloseConfirm,
  onOpenReschedule,
  reason,
  onReasonChange,
  onCancel,
  submitting,
  error,
}: {
  booking: ManagedBooking;
  locale: Locale;
  showConfirm: boolean;
  onOpenConfirm: () => void;
  onCloseConfirm: () => void;
  onOpenReschedule: () => void;
  reason: string;
  onReasonChange: (v: string) => void;
  onCancel: () => void | Promise<void>;
  submitting: boolean;
  error: string | null;
}) {
  const t = T[locale];
  const bcp47 = locale === "cs" ? "cs-CZ" : "en-US";
  const start = new Date(booking.start);
  const end = new Date(booking.end);
  const dayLabel = start.toLocaleDateString(bcp47, {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });
  const timeLabel = `${fmt(start)}–${fmt(end)}`;
  const isCancelled = booking.status === "cancelled";
  const isPast = end.getTime() < Date.now();

  return (
    <>
      <section className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex items-start gap-3">
          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-indigo-100 text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300">
            {booking.kind === "physical" ? (
              <MapPin size={18} aria-hidden />
            ) : (
              <Video size={18} aria-hidden />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-50">
              {booking.title || t.meetingWith(booking.host_name)}
            </h1>
            <p className="text-sm text-zinc-600 dark:text-zinc-400">
              {t.bookingAttendeeWith(booking.host_name)}
            </p>
          </div>
          <StatusPill status={booking.status} past={isPast && !isCancelled} locale={locale} />
        </div>

        <dl className="mt-6 space-y-3 border-t border-zinc-100 pt-5 text-sm dark:border-zinc-800">
          <Row label={t.rowWhen} value={`${dayLabel} · ${timeLabel}`} />
          {booking.kind === "physical" && booking.location && (
            <Row label={t.rowWhere} value={booking.location} />
          )}
          <Row
            label={t.rowBookedAs}
            value={
              booking.visitor_name
                ? `${booking.visitor_name} <${booking.visitor_email}>`
                : booking.visitor_email
            }
          />
        </dl>

        {isCancelled ? (
          <div className="mt-6 flex items-start gap-2 rounded-md bg-emerald-50 p-3 text-sm text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200">
            <CheckCircle2 size={16} className="mt-0.5 shrink-0" aria-hidden />
            <p>
              {t.bookingCancelledTemplate(
                booking.cancelled_at
                  ? new Date(booking.cancelled_at).toLocaleString(bcp47)
                  : null,
              )}
            </p>
          </div>
        ) : isPast ? (
          <p className="mt-6 rounded-md bg-zinc-100 p-3 text-sm text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
            {t.bookingAlreadyEnded}
          </p>
        ) : (
          <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={onOpenConfirm}
              className="inline-flex items-center justify-center gap-1.5 rounded-md border border-red-200 px-3 py-2 text-sm font-medium text-red-700 hover:bg-red-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950/40"
            >
              {t.cancelBookingAction}
            </button>
            <button
              type="button"
              onClick={onOpenReschedule}
              className="inline-flex items-center justify-center gap-1.5 rounded-md border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm font-medium text-indigo-700 hover:bg-indigo-100 dark:border-indigo-800 dark:bg-indigo-950/40 dark:text-indigo-200 dark:hover:bg-indigo-900/40"
            >
              <CalendarClock size={14} aria-hidden />
              {t.rescheduleAction}
            </button>
          </div>
        )}
      </section>

      {showConfirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget && !submitting) onCloseConfirm();
          }}
          role="dialog"
          aria-modal="true"
        >
          <div className="w-full max-w-md rounded-xl border border-zinc-200 bg-white p-5 shadow-2xl dark:border-zinc-800 dark:bg-zinc-900">
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
              {t.cancelConfirmTitle}
            </h2>
            <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
              {t.cancelConfirmBody}
            </p>
            <div className="mt-4 space-y-1">
              <Label htmlFor="cancel-reason">{t.cancelReasonLabel}</Label>
              <textarea
                id="cancel-reason"
                value={reason}
                onChange={(e) => onReasonChange(e.target.value)}
                rows={3}
                maxLength={500}
                placeholder={t.cancelReasonPlaceholder}
                className="w-full resize-y rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-950"
              />
            </div>
            {error && <FormError message={error} />}
            <div className="mt-4 flex justify-end gap-2">
              <Button
                type="button"
                variant="secondary"
                onClick={onCloseConfirm}
                disabled={submitting}
                className="w-auto px-4"
              >
                {t.keepIt}
              </Button>
              <Button
                type="button"
                onClick={onCancel}
                disabled={submitting}
                className="w-auto px-5 !bg-red-600 hover:!bg-red-700 dark:!bg-red-600 dark:hover:!bg-red-700"
              >
                {submitting ? t.cancelling : t.yesCancel}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function ReschedulePanel({
  booking,
  locale,
  pick,
  onPick,
  onSubmit,
  onBack,
  submitting,
  error,
}: {
  booking: ManagedBooking;
  locale: Locale;
  pick: { start: Date; end: Date } | null;
  onPick: (iv: { start: Date; end: Date } | null) => void;
  onSubmit: () => void | Promise<void>;
  onBack: () => void;
  submitting: boolean;
  error: string | null;
}) {
  const t = T[locale];
  const bcp47 = locale === "cs" ? "cs-CZ" : "en-US";
  const availability = booking.availability;
  const durationMin = Math.round(
    (new Date(booking.end).getTime() - new Date(booking.start).getTime()) / 60_000,
  );

  const slots = useMemo(() => {
    if (!availability) return [];
    return computeFreeSlots(
      availability.working_hours,
      availability.busy,
      new Date(availability.window.start),
      new Date(availability.window.end),
    );
  }, [availability]);

  const holidays = useMemo(() => {
    if (!availability) return new Map<string, string>();
    return new Map(availability.holidays.map((h) => [h.date, h.name]));
  }, [availability]);

  return (
    <div className="space-y-4">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-sm text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
      >
        <ArrowLeft size={14} aria-hidden />
        {t.backToBooking}
      </button>

      <header>
        <h1 className="text-xl font-semibold text-zinc-900 dark:text-zinc-50">
          {t.pickNewTime}
        </h1>
        <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
          {t.rescheduleHint(booking.host_name)}
        </p>
      </header>

      {!availability ? (
        <FormError message={t.couldntLoadAvailability} />
      ) : slots.length === 0 ? (
        <div className="rounded-xl border border-zinc-200 bg-white p-8 text-center text-sm text-zinc-500 shadow-sm dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
          {t.noFreeTime}
        </div>
      ) : (
        <SlotsCalendar
          slots={slots}
          durationMin={durationMin}
          holidays={holidays}
          workingHoursRange={workingHoursRangeFromHours(availability.working_hours)}
          locale={locale}
          onIntervalClick={(iv) => {
            // Snap to first `durationMin` chunk of the clicked free interval.
            const start = new Date(iv.start);
            const end = new Date(start.getTime() + durationMin * 60_000);
            if (end.getTime() > iv.end.getTime()) return;
            onPick({ start, end });
          }}
        />
      )}

      {pick && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget && !submitting) onPick(null);
          }}
          role="dialog"
          aria-modal="true"
        >
          <div className="w-full max-w-md rounded-xl border border-zinc-200 bg-white p-5 shadow-2xl dark:border-zinc-800 dark:bg-zinc-900">
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
              {t.moveMeetingTitle}
            </h2>
            <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
              {t.moveMeetingBody(booking.host_name)}
            </p>
            <dl className="mt-4 space-y-2 text-sm">
              <div className="flex gap-3">
                <dt className="w-16 text-xs uppercase tracking-wider text-zinc-500">
                  {t.rowWas}
                </dt>
                <dd className="text-zinc-800 dark:text-zinc-100">
                  {new Date(booking.start).toLocaleString(bcp47)}
                </dd>
              </div>
              <div className="flex gap-3">
                <dt className="w-16 text-xs uppercase tracking-wider text-zinc-500">
                  {t.rowNow}
                </dt>
                <dd className="font-medium text-indigo-700 dark:text-indigo-300">
                  {pick.start.toLocaleString(bcp47)} – {fmt(pick.end)}
                </dd>
              </div>
            </dl>
            {error && <FormError message={error} />}
            <div className="mt-4 flex justify-end gap-2">
              <Button
                type="button"
                variant="secondary"
                onClick={() => onPick(null)}
                disabled={submitting}
                className="w-auto px-4"
              >
                {t.backBtn}
              </Button>
              <Button
                type="button"
                onClick={onSubmit}
                disabled={submitting}
                className="w-auto px-5"
              >
                {submitting ? t.moving : t.confirmMove}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/** Render a Date as ISO 8601 in the browser's local time zone, matching
 * the format BookingDialog uses on the create side. */
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

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-wrap gap-2 sm:gap-4">
      <dt className="w-20 shrink-0 text-xs uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        {label}
      </dt>
      <dd className="min-w-0 flex-1 text-zinc-800 dark:text-zinc-100">{value}</dd>
    </div>
  );
}

function StatusPill({
  status,
  past,
  locale,
}: {
  status: ManagedBooking["status"];
  past: boolean;
  locale: Locale;
}) {
  const t = T[locale];
  if (status === "cancelled") {
    return (
      <span className="inline-flex items-center rounded-full bg-zinc-200 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
        {t.statusCancelled}
      </span>
    );
  }
  if (past) {
    return (
      <span className="inline-flex items-center rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
        {t.statusEnded}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200">
      {t.statusConfirmed}
    </span>
  );
}

function fmt(d: Date): string {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

// ---------------------------------------------------------------------------
// ManageBookingSkeleton — layout-shaped placeholder for the "loading your
// booking" state. Mirrors the real booking card so the eye lands on the
// right structure when the payload arrives.
// ---------------------------------------------------------------------------

function ManageBookingSkeleton() {
  return (
    <section
      aria-busy="true"
      className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
    >
      <div className="flex items-start gap-3">
        <Skeleton className="h-10 w-10 rounded-lg" />
        <div className="min-w-0 flex-1 space-y-2">
          <Skeleton className="h-6 w-56" />
          <Skeleton className="h-4 w-40" />
        </div>
        <Skeleton className="h-5 w-20 rounded-full" />
      </div>
      <div className="mt-6 space-y-3 border-t border-zinc-100 pt-5 dark:border-zinc-800">
        <div className="flex gap-4">
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 w-64" />
        </div>
        <div className="flex gap-4">
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 w-48" />
        </div>
      </div>
      <div className="mt-6 flex justify-end gap-2">
        <Skeleton className="h-9 w-20 rounded-md" />
        <Skeleton className="h-9 w-28 rounded-md" />
      </div>
    </section>
  );
}
