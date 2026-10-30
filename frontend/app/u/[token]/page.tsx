"use client";

/**
 * Public availability page — anyone with the link sees this read-only view.
 * Token is in the URL; backend gates on share_enabled and returns 404
 * otherwise. No nav, no auth required, OG tags for nice link previews.
 */

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowRight, Globe, MapPin, Video } from "lucide-react";
import { BookingDialog, type BookingSubmit } from "@/components/BookingDialog";
import { LangSwitch } from "@/components/LangSwitch";
import { Logo } from "@/components/Logo";
import { Skeleton } from "@/components/Skeleton";
import { SlotsCalendar } from "@/components/SlotsCalendar";
import { createPublicMeeting } from "@/lib/google";
import { T, useLocale } from "@/lib/i18n";
import type { PublicMeetingType } from "@/lib/meeting-types";
import {
  colorFromName,
  computeFreeSlots,
  getInitials,
  getPublicProfile,
  PublicProfileNotFoundError,
  workingHoursRangeFromHours,
  type PublicProfileResponse,
} from "@/lib/public-profile";

export default function PublicProfilePage() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-zinc-500" />}>
      <PublicProfilePageInner />
    </Suspense>
  );
}

function PublicProfilePageInner() {
  const params = useParams<{ token: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const token = params.token;
  const selectedSlug = searchParams?.get("type") ?? null;
  const [locale, setLocale] = useLocale();
  const t = T[locale];

  const [data, setData] = useState<PublicProfileResponse | null>(null);
  const [error, setError] = useState<"not_found" | "load" | null>(null);

  const [bookingInterval, setBookingInterval] = useState<{ start: Date; end: Date } | null>(null);
  const [bookingSubmitting, setBookingSubmitting] = useState(false);
  const [bookingError, setBookingError] = useState<string | null>(null);
  const [bookingSuccess, setBookingSuccess] = useState<React.ReactNode | null>(null);
  /** Visitor's preferred meeting-link provider. Only relevant (and only
   * rendered) when the host has both Google and Microsoft connected. */
  const [meetingLinkKind, setMeetingLinkKind] = useState<"auto" | "google_meet" | "teams">("auto");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await getPublicProfile(token);
        if (alive) setData(r);
      } catch (err) {
        if (!alive) return;
        if (err instanceof PublicProfileNotFoundError) {
          setError("not_found");
        } else {
          setError("load");
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [token]);

  const selectedType: PublicMeetingType | null = useMemo(() => {
    if (!data || !selectedSlug) return null;
    return data.meeting_types?.find((t) => t.slug === selectedSlug) ?? null;
  }, [data, selectedSlug]);

  const slots = useMemo(() => {
    if (!data) return [];
    const start = new Date(data.window.start);
    const end = new Date(data.window.end);
    const free = computeFreeSlots(data.profile.working_hours, data.busy, start, end);
    return free.map((f) => ({ start: f.start, end: f.end }));
  }, [data]);

  const holidayMap = useMemo(() => {
    if (!data) return new Map<string, string>();
    return new Map(data.holidays.map((h) => [h.date, h.name]));
  }, [data]);

  /** Providers the host has connected AND whose booking toggle is on. */
  const activeOnlineProviders = useMemo(() => {
    if (!data) return [] as ("google" | "microsoft")[];
    const all = data.host_providers ?? [];
    return all.filter((p) => {
      if (p === "google") return data.accepts_google_bookings !== false;
      if (p === "microsoft") return data.accepts_microsoft_bookings !== false;
      return true;
    });
  }, [data]);

  /** Host's predefined in-person locations (empty array when none). */
  const hostLocations = useMemo(() => data?.host_locations ?? [], [data]);

  /**
   * True when the host has no active online providers but has at least one
   * predefined location — the booking page should allow in-person-only
   * bookings instead of showing a fully-degraded "not accepting" banner.
   */
  const inPersonOnly = useMemo(
    () => activeOnlineProviders.length === 0 && hostLocations.length > 0,
    [activeOnlineProviders, hostLocations],
  );

  /**
   * True when there are no active online providers AND no predefined locations
   * — the host is genuinely not accepting any kind of booking right now.
   */
  const fullyDegraded = useMemo(
    () => activeOnlineProviders.length === 0 && hostLocations.length === 0,
    [activeOnlineProviders, hostLocations],
  );

  if (error === "not_found") {
    return (
      <main className="grid min-h-screen place-items-center bg-zinc-50 px-6 dark:bg-zinc-950">
        <div className="mx-auto max-w-md text-center">
          <h1 className="text-2xl font-semibold text-zinc-900 dark:text-zinc-50">
            {t.profileNotFound}
          </h1>
          <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
            {t.profileNotFoundHint}
          </p>
        </div>
      </main>
    );
  }

  if (!data) {
    return <PublicProfileSkeleton />;
  }

  const { display_name, avatar_url, country } = data.profile;
  const initials = getInitials(display_name);
  const bgColor = colorFromName(display_name);

  return (
    <main className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      {/* Top brand bar — same Slotly mark as the authed app, so visitors know
          immediately whose service is showing them this page. */}
      <div className="border-b border-zinc-200 bg-white px-6 py-3 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mx-auto flex max-w-3xl items-center justify-between gap-3">
          <Link
            href="/"
            className="inline-flex items-center text-sm hover:opacity-80"
            aria-label="Slotly"
          >
            <Logo size={20} />
          </Link>
          <div className="flex items-center gap-3">
            <LangSwitch locale={locale} onChange={setLocale} />
            <Link
              href="/auth/register"
              className="text-xs font-medium text-zinc-500 hover:text-indigo-600 dark:text-zinc-400 dark:hover:text-indigo-400"
            >
              {t.getYourOwn}
            </Link>
          </div>
        </div>
      </div>
      <header className="border-b border-zinc-200 bg-white py-10 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mx-auto flex max-w-3xl items-center gap-5 px-6">
          {avatar_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={avatar_url}
              alt={display_name}
              className="h-20 w-20 shrink-0 rounded-full object-cover ring-2 ring-zinc-200 dark:ring-zinc-700"
            />
          ) : (
            <div
              className="grid h-20 w-20 shrink-0 place-items-center rounded-full text-2xl font-semibold text-white ring-2 ring-zinc-200 dark:ring-zinc-700"
              style={{ backgroundColor: bgColor }}
              aria-hidden
            >
              {initials}
            </div>
          )}
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
              {display_name}
            </h1>
            <p className="mt-1 flex items-center gap-1.5 text-sm text-zinc-500 dark:text-zinc-400">
              <Globe className="h-4 w-4" />
              <span>{t.countryAndWindow(country)}</span>
            </p>
          </div>
        </div>
      </header>

      <section className="mx-auto max-w-3xl px-6 py-8">
        {fullyDegraded ? (
          <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-100">
            <p className="font-medium">{t.notAcceptingBookings}</p>
          </div>
        ) : inPersonOnly ? (
          <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-100">
            <p className="font-medium">{t.inPersonOnlyTitle}</p>
            <p className="mt-1 text-amber-800 dark:text-amber-200/80">
              {t.inPersonOnlyHint}
            </p>
          </div>
        ) : data.booking_enabled === false ? (
          <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-100">
            <p className="font-medium">{t.bookingsOffFor(display_name)}</p>
            <p className="mt-1 text-amber-800 dark:text-amber-200/80">
              {t.bookingsOffHint}
            </p>
          </div>
        ) : null}
        {(data.meeting_types?.length ?? 0) > 0 && selectedType === null ? (
          // Landing: pick a meeting type first.
          <TypeList
            types={data.meeting_types ?? []}
            onPick={(slug) => router.push(`?type=${encodeURIComponent(slug)}`)}
            heading={t.pickAMeeting}
            hint={t.pickAMeetingHint}
          />
        ) : slots.length === 0 ? (
          <div className="rounded-xl border border-zinc-200 bg-white p-8 text-center text-sm text-zinc-500 shadow-sm dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
            {t.noFreeTime}
          </div>
        ) : (
          <>
            {selectedType && (
              <button
                type="button"
                onClick={() => router.push(location.pathname)}
                className="mb-3 inline-flex items-center gap-1.5 text-xs text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
              >
                <ArrowLeft size={12} aria-hidden />
                {t.backToMeetingTypes}
              </button>
            )}
            {activeOnlineProviders.length >= 2 && (
              <fieldset className="mb-4 rounded-lg border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
                <legend className="px-1 text-xs font-medium text-zinc-500 dark:text-zinc-400">
                  {t.meetingLinkLabel}
                </legend>
                <div className="mt-2 flex flex-wrap gap-3">
                  {(
                    [
                      { value: "auto", label: t.meetingLinkAuto },
                      { value: "google_meet", label: t.meetingLinkGoogleMeet },
                      { value: "teams", label: t.meetingLinkTeams },
                    ] as const
                  ).map(({ value, label }) => (
                    <label
                      key={value}
                      className="flex cursor-pointer items-center gap-2 text-sm text-zinc-700 dark:text-zinc-300"
                    >
                      <input
                        type="radio"
                        name="meeting_link_kind"
                        value={value}
                        checked={meetingLinkKind === value}
                        onChange={() => setMeetingLinkKind(value)}
                        className="accent-indigo-600"
                      />
                      {label}
                    </label>
                  ))}
                </div>
              </fieldset>
            )}
            <SlotsCalendar
              slots={slots}
              durationMin={selectedType?.duration_min ?? 30}
              holidays={holidayMap}
              workingHoursRange={workingHoursRangeFromHours(data.profile.working_hours)}
              locale={locale}
              onIntervalClick={
                // Allow clicks when online booking is enabled OR when there are
                // predefined in-person locations (in-person-only mode).
                fullyDegraded
                  ? undefined
                  : (iv) => {
                      setBookingError(null);
                      setBookingSuccess(null);
                      setBookingInterval(iv);
                    }
              }
            />
          </>
        )}
      </section>

      <BookingDialog
        open={bookingInterval !== null}
        onClose={() => setBookingInterval(null)}
        interval={bookingInterval}
        mode="public"
        locale={locale}
        hostName={display_name}
        defaultTitle={selectedType?.name ?? t.meetingWith(display_name)}
        // In-person-only: force physical kind and enable the physical UI.
        // Generic flow: allow the toggle only when no type is selected.
        // Locked-type flow: kind is baked in via lockedType.
        allowPhysical={selectedType === null || inPersonOnly}
        defaultKind={inPersonOnly ? "physical" : "online"}
        hostLocations={hostLocations}
        lockedType={
          selectedType
            ? {
                name: selectedType.name,
                duration_min: selectedType.duration_min,
                kind: selectedType.kind,
                location: selectedType.location,
                description: selectedType.description,
                questions: selectedType.questions,
                default_location: selectedType.default_location ?? null,
              }
            : null
        }
        submitting={bookingSubmitting}
        errorMessage={bookingError}
        successMessage={bookingSuccess}
        onSubmit={async (v: BookingSubmit) => {
          setBookingSubmitting(true);
          setBookingError(null);
          setBookingSuccess(null);
          try {
            const result = await createPublicMeeting({
              token,
              visitorName: v.visitorName ?? "",
              visitorEmail: v.visitorEmail ?? "",
              start: v.startIso,
              end: v.endIso,
              title: v.title,
              notes: v.notes,
              kind: v.kind,
              location: v.location,
              typeSlug: selectedType?.slug,
              customAnswers: v.customAnswers,
              // Only send when visitor explicitly picked a provider (not "auto").
              meetingLinkKind: meetingLinkKind !== "auto" ? meetingLinkKind : undefined,
              // Picked language — server stores it on the Booking row so
              // later emails (reminders, host-cancel) stay in this language.
              locale,
            });
            // Meeting type may configure a post-booking redirect URL —
            // typically the host's own "thank you" page for conversion
            // tracking. Bounce there instead of showing inline success.
            if ("event" in result && result.redirect_url) {
              window.location.href = result.redirect_url;
              return;
            }
            if ("pending" in result) {
              setBookingSuccess(<span>{t.bookingRequestSent(display_name)}</span>);
            } else {
              // Confirmed online booking — surface the Meet/Teams join link
              // and the manage link inline so the visitor doesn't have to
              // dig through the calendar invite email just to save them.
              const meetLink = result.event.meet_link;
              const manageUrl = result.manage_url;
              setBookingSuccess(
                <div className="space-y-2">
                  <p className="font-medium">{t.bookingConfirmed}</p>
                  <div className="flex flex-wrap gap-2">
                    {meetLink && (
                      <a
                        href={meetLink}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1.5 rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700"
                      >
                        {t.joinMeeting}
                      </a>
                    )}
                    {manageUrl && (
                      <a
                        href={manageUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1.5 rounded-md border border-emerald-300 px-3 py-1.5 text-xs font-medium text-emerald-800 hover:bg-emerald-100 dark:border-emerald-700 dark:text-emerald-200 dark:hover:bg-emerald-900/40"
                      >
                        {t.manageBooking}
                      </a>
                    )}
                  </div>
                </div>,
              );
            }
            // Longer delay so the visitor has time to click Join/Manage.
            setTimeout(() => {
              setBookingInterval(null);
              setBookingSuccess(null);
              getPublicProfile(token).then(setData).catch(() => {});
            }, 6000);
          } catch (err) {
            setBookingError(err instanceof Error ? err.message : t.couldntCreateMeeting);
          } finally {
            setBookingSubmitting(false);
          }
        }}
      />
    </main>
  );
}

// ---------------------------------------------------------------------------
// TypeList — landing view when the host has defined meeting types.
// ---------------------------------------------------------------------------

function TypeList({
  types,
  onPick,
  heading,
  hint,
}: {
  types: PublicMeetingType[];
  onPick: (slug: string) => void;
  heading: string;
  hint: string;
}) {
  return (
    <section className="space-y-3">
      <header>
        <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-50">
          {heading}
        </h2>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">{hint}</p>
      </header>
      <ul className="space-y-2">
        {types.map((t) => (
          <li key={t.slug}>
            <button
              type="button"
              onClick={() => onPick(t.slug)}
              className="group flex w-full items-start gap-3 rounded-xl border border-zinc-200 bg-white p-4 text-left shadow-sm transition-colors hover:border-indigo-300 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-indigo-700"
            >
              <span
                className="mt-1 h-3 w-3 shrink-0 rounded-full"
                style={{ backgroundColor: t.color || "#4f46e5" }}
                aria-hidden
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
                    {t.name}
                  </span>
                  <span className="inline-flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
                    {t.kind === "physical" ? (
                      <MapPin size={12} aria-hidden />
                    ) : (
                      <Video size={12} aria-hidden />
                    )}
                    {t.duration_min} min
                  </span>
                </div>
                {t.description && (
                  <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
                    {t.description}
                  </p>
                )}
                {t.location && t.kind === "physical" && (
                  <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
                    {t.location}
                  </p>
                )}
              </div>
              <ArrowRight
                size={16}
                aria-hidden
                className="mt-1 shrink-0 text-zinc-400 transition-transform group-hover:translate-x-0.5 group-hover:text-indigo-600 dark:group-hover:text-indigo-400"
              />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// PublicProfileSkeleton — layout-shaped placeholder shown while the profile
// payload is in flight. Mirrors the brand bar, hero card, and the calendar
// grid so the real content lands where the eye already expects it.
// ---------------------------------------------------------------------------

function PublicProfileSkeleton() {
  return (
    <main className="min-h-screen bg-zinc-50 dark:bg-zinc-950" aria-busy="true">
      <div className="border-b border-zinc-200 bg-white px-6 py-3 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mx-auto flex max-w-3xl items-center justify-between gap-3">
          <Skeleton className="h-5 w-16" />
          <Skeleton className="h-6 w-20 rounded-md" />
        </div>
      </div>
      <header className="border-b border-zinc-200 bg-white py-10 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="mx-auto flex max-w-3xl items-center gap-5 px-6">
          <Skeleton className="h-20 w-20 rounded-full" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-6 w-56" />
            <Skeleton className="h-4 w-72" />
          </div>
        </div>
      </header>
      <section className="mx-auto max-w-3xl px-6 py-8">
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
          <div className="flex items-center justify-between border-b border-zinc-100 px-3 py-3 dark:border-zinc-800">
            <Skeleton className="h-7 w-24 rounded-md" />
            <Skeleton className="h-7 w-32 rounded-md" />
            <Skeleton className="h-5 w-28" />
          </div>
          <div className="grid grid-cols-7 gap-px bg-zinc-100 p-px dark:bg-zinc-800">
            {Array.from({ length: 7 }).map((_, i) => (
              <div key={i} className="bg-white px-2 py-2 dark:bg-zinc-900">
                <Skeleton className="mx-auto h-3 w-6" />
                <Skeleton className="mx-auto mt-1 h-4 w-10" />
              </div>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-px bg-zinc-100 p-px dark:bg-zinc-800">
            {Array.from({ length: 7 }).map((_, i) => (
              <div key={i} className="relative h-[384px] bg-white dark:bg-zinc-900">
                {i % 2 === 0 && (
                  <Skeleton
                    className="absolute inset-x-1 h-14 bg-emerald-100 dark:bg-emerald-900/40"
                    style={{ top: "80px" }}
                  />
                )}
                {i % 3 === 0 && (
                  <Skeleton
                    className="absolute inset-x-1 h-10 bg-emerald-100 dark:bg-emerald-900/40"
                    style={{ top: "180px" }}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      </section>
    </main>
  );
}
