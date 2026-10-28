"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  BellRing,
  Building2,
  Calendar,
  CalendarDays,
  CheckCircle2,
  Circle,
  MailOpen,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { AuthedHeader } from "@/components/AuthedHeader";
import { Logo, LogoMark } from "@/components/Logo";
import { CardSkeleton, PageSkeleton } from "@/components/Skeleton";
import { Button } from "@/components/ui";
import { getSession } from "@/lib/auth";
import { listUnavailabilities, type Unavailability } from "@/lib/availability";
import { listCalendars, type Calendar as UserCalendar } from "@/lib/calendars";
import { getMe, type Me } from "@/lib/me";
import { listNotifications } from "@/lib/notifications";
import {
  listRecentSearches,
  listSavedSearches,
  type RecentSearch,
  type SavedSearch,
} from "@/lib/saved-searches";
import {
  acceptInvitation,
  listMyInvitations,
  listTeams,
  rejectInvitation,
  type IncomingInvitation,
  type TeamSummary,
} from "@/lib/teams";

type Dashboard = {
  me: Me;
  teams: TeamSummary[];
  calendars: UserCalendar[];
  invitations: IncomingInvitation[];
  unread: number;
  saved: SavedSearch[];
  recent: RecentSearch[];
  unavailability: Unavailability[];
};

type State = "loading" | "guest" | "authed";

export default function Home() {
  const [state, setState] = useState<State>("loading");
  const [email, setEmail] = useState("");
  const [data, setData] = useState<Dashboard | null>(null);

  async function loadDashboard() {
    const [me, teams, calendars, invitations, notifs, saved, recent, unavail] =
      await Promise.all([
        getMe(),
        listTeams(),
        listCalendars(),
        listMyInvitations(),
        listNotifications(),
        listSavedSearches(),
        listRecentSearches(),
        listUnavailabilities().catch(() => [] as Unavailability[]),
      ]);
    setData({
      me,
      teams,
      calendars,
      invitations,
      unread: notifs.unread_count,
      saved,
      recent,
      unavailability: unavail,
    });
  }

  useEffect(() => {
    (async () => {
      const session = await getSession();
      if (!session.meta?.is_authenticated || !session.data?.user) {
        setState("guest");
        return;
      }
      setEmail(session.data.user.email);
      setState("authed");
      try {
        await loadDashboard();
      } catch {
        // partial-load failures are non-fatal; keep dashboard at "loading"
      }
    })();
  }, []);

  if (state === "loading") {
    return (
      <PageSkeleton>
        <CardSkeleton rows={2} />
        <CardSkeleton rows={4} className="mt-6" />
      </PageSkeleton>
    );
  }

  if (state === "guest") {
    return <GuestLanding />;
  }

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <AuthedHeader email={email} />
      <main className="mx-auto max-w-3xl space-y-6 px-6 py-10">
        {data === null ? (
          <CardSkeleton rows={6} />
        ) : (
          <DashboardView data={data} onChanged={loadDashboard} />
        )}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Authed dashboard
// ---------------------------------------------------------------------------

function DashboardView({
  data,
  onChanged,
}: {
  data: Dashboard;
  onChanged: () => Promise<void>;
}) {
  const greeting = useMemo(() => {
    const name = data.me.first_name || data.me.email.split("@")[0];
    const hour = new Date().getHours();
    const phase =
      hour < 5
        ? "Up late"
        : hour < 12
          ? "Good morning"
          : hour < 18
            ? "Good afternoon"
            : "Good evening";
    return `${phase}, ${name}.`;
  }, [data.me]);

  const onboarding: Array<{ key: string; label: string; href: string; done: boolean }> = [
    {
      key: "name",
      label: "Set your name",
      href: "/settings",
      done: Boolean(data.me.first_name || data.me.last_name),
    },
    {
      key: "calendar",
      label: "Connect a calendar (optional)",
      href: "/settings/calendars",
      done: data.calendars.length > 0,
    },
    {
      key: "team",
      label: "Create or join a group",
      href: "/groups",
      done: data.teams.length > 0,
    },
  ];
  const showOnboarding = onboarding.some((step) => !step.done);

  return (
    <>
      {/* Greeting + primary CTA */}
      <section className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
              {greeting}
            </h1>
            <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
              Find a free slot, view someone&apos;s availability, or block off
              your own time.
            </p>
          </div>
          <Link href="/search" className="w-full sm:w-auto">
            <Button className="inline-flex items-center justify-center gap-2 sm:w-auto sm:px-6">
              <Calendar size={16} />
              Find a slot
              <ArrowRight size={14} />
            </Button>
          </Link>
        </div>
      </section>

      {showOnboarding && (
        <section className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-5 dark:border-indigo-900/60 dark:bg-indigo-950/20">
          <h2 className="mb-3 inline-flex items-center gap-2 text-sm font-semibold text-indigo-900 dark:text-indigo-200">
            <Sparkles size={15} />
            Get started
          </h2>
          <ul className="space-y-1.5">
            {onboarding.map((step) => (
              <li key={step.key} className="flex items-center gap-2 text-sm">
                {step.done ? (
                  <CheckCircle2
                    size={16}
                    className="shrink-0 text-emerald-600 dark:text-emerald-400"
                    aria-hidden="true"
                  />
                ) : (
                  <Circle
                    size={16}
                    className="shrink-0 text-zinc-400 dark:text-zinc-500"
                    aria-hidden="true"
                  />
                )}
                {step.done ? (
                  <span className="text-indigo-900/70 line-through dark:text-indigo-200/60">
                    {step.label}
                  </span>
                ) : (
                  <Link
                    href={step.href}
                    className="font-medium text-indigo-900 hover:underline dark:text-indigo-100"
                  >
                    {step.label}
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Stats grid */}
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label="Groups"
          value={data.teams.length}
          href="/groups"
          icon={Building2}
        />
        <StatCard
          label="Calendars"
          value={data.calendars.length}
          href="/settings/calendars"
          warn={data.calendars.length === 0}
          icon={CalendarDays}
        />
        <StatCard
          label="Invitations"
          value={data.invitations.length}
          href="/groups"
          accent={data.invitations.length > 0}
          icon={MailOpen}
        />
        <StatCard
          label="Unread"
          value={data.unread}
          href="/notifications"
          accent={data.unread > 0}
          icon={BellRing}
        />
      </section>

      {/* Pending invitations */}
      {data.invitations.length > 0 && (
        <PendingInvitationsCard
          invitations={data.invitations}
          onChanged={onChanged}
        />
      )}

      {/* Saved searches */}
      {data.saved.length > 0 && (
        <section className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
          <header className="mb-3 flex items-center justify-between">
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
              Saved searches
            </h2>
            <Link href="/search" className="text-xs text-zinc-500 underline dark:text-zinc-400">
              All
            </Link>
          </header>
          <ul className="flex flex-wrap gap-2">
            {data.saved.slice(0, 8).map((s) => (
              <li key={s.id}>
                <Link
                  href={`/search?saved=${s.id}`}
                  className="inline-flex items-center gap-2 rounded-full border border-zinc-200 bg-zinc-50 px-3 py-1.5 text-xs font-medium text-zinc-800 hover:border-zinc-300 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100 dark:hover:bg-zinc-700"
                  title={`${s.member_ids.length} member${s.member_ids.length === 1 ? "" : "s"} · ${s.duration_min}min · next ${s.window_days}d`}
                >
                  {s.name}
                  <span className="text-zinc-400">→</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Recent searches */}
      {data.recent.length > 0 && (
        <section className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
          <header className="mb-3">
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
              Recent searches
            </h2>
          </header>
          <ul className="space-y-1 text-sm">
            {data.recent.slice(0, 5).map((r) => {
              const teamName =
                data.teams.find((t) => t.id === r.team)?.name ?? `Team #${r.team}`;
              return (
                <li
                  key={r.id}
                  className="flex items-center gap-2 truncate text-zinc-700 dark:text-zinc-300"
                >
                  <Link
                    href={`/search`}
                    className="flex-1 truncate hover:underline"
                  >
                    <strong>{teamName}</strong> · {r.member_ids.length} member
                    {r.member_ids.length === 1 ? "" : "s"} · {r.duration_min}min ·{" "}
                    {new Date(r.window_start).toLocaleDateString()} –{" "}
                    {new Date(r.window_end).toLocaleDateString()}
                  </Link>
                  <span className="shrink-0 text-xs text-zinc-400">
                    {relativeTime(r.created_at)}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {/* Help footer */}
      <section className="rounded-xl border border-dashed border-zinc-300 bg-white p-5 text-sm text-zinc-600 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-400">
        Looking for something else?{" "}
        <Link href="/people" className="font-medium text-zinc-900 underline dark:text-zinc-50">
          Browse people
        </Link>{" "}
        ·{" "}
        <Link href="/settings" className="font-medium text-zinc-900 underline dark:text-zinc-50">
          Settings
        </Link>
      </section>
    </>
  );
}

function StatCard({
  label,
  value,
  href,
  warn = false,
  accent = false,
  icon: Icon,
}: {
  label: string;
  value: number;
  href: string;
  warn?: boolean;
  accent?: boolean;
  icon: LucideIcon;
}) {
  const numberClass = warn
    ? "text-amber-600 dark:text-amber-400"
    : accent
      ? "text-indigo-600 dark:text-indigo-400"
      : "text-zinc-900 dark:text-zinc-50";
  const iconBg = warn
    ? "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
    : accent
      ? "bg-indigo-100 text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300"
      : "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300";
  return (
    <Link
      href={href}
      className="group rounded-xl border border-zinc-200 bg-white p-4 shadow-sm transition-colors hover:border-indigo-300 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-indigo-700"
    >
      <div className="mb-3 flex items-center justify-between">
        <span className={`inline-flex h-8 w-8 items-center justify-center rounded-lg ${iconBg}`}>
          <Icon size={16} aria-hidden="true" />
        </span>
      </div>
      <div className={`text-2xl font-semibold ${numberClass}`}>{value}</div>
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
    </Link>
  );
}

function PendingInvitationsCard({
  invitations,
  onChanged,
}: {
  invitations: IncomingInvitation[];
  onChanged: () => Promise<void>;
}) {
  async function onAccept(inv: IncomingInvitation) {
    try {
      await acceptInvitation(inv.token);
      await onChanged();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Accept failed");
    }
  }
  async function onReject(inv: IncomingInvitation) {
    try {
      await rejectInvitation(inv.token);
      await onChanged();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Reject failed");
    }
  }
  return (
    <section className="rounded-xl border border-amber-200 bg-amber-50 p-5 dark:border-amber-900 dark:bg-amber-950/30">
      <h2 className="mb-2 text-sm font-semibold text-amber-900 dark:text-amber-200">
        You have {invitations.length} pending invitation{invitations.length === 1 ? "" : "s"}
      </h2>
      <ul className="divide-y divide-amber-200/70 dark:divide-amber-900/60">
        {invitations.map((inv) => (
          <li key={inv.id} className="flex items-center gap-3 py-2 text-sm">
            <div className="flex-1">
              <p className="font-medium text-amber-950 dark:text-amber-100">{inv.team_name}</p>
              <p className="text-xs text-amber-800/80 dark:text-amber-300/80">
                Invited by {inv.invited_by_email ?? "?"} as {inv.role_on_accept}
              </p>
            </div>
            <button
              onClick={() => onAccept(inv)}
              className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700"
            >
              Accept
            </button>
            <button
              onClick={() => onReject(inv)}
              className="rounded-md border border-amber-300 px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100 dark:border-amber-800 dark:text-amber-200 dark:hover:bg-amber-900/40"
            >
              Decline
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Guest landing
// ---------------------------------------------------------------------------

function GuestLanding() {
  return (
    <div className="flex min-h-screen flex-1 flex-col bg-zinc-50 dark:bg-zinc-950">
      <header className="px-6 py-5">
        <Logo size={26} />
      </header>
      <main className="flex flex-1 items-center justify-center px-6 pb-10">
        <div className="w-full max-w-xl space-y-10 text-center">
          {/* Hero */}
          <div className="space-y-6">
            <div className="mx-auto inline-block">
              <LogoMark size={56} />
            </div>
            <h1 className="text-3xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50 sm:text-4xl">
              Find time to meet — without the calendar Tetris
            </h1>
            <p className="text-lg text-zinc-600 dark:text-zinc-400">
              Slotly connects to your Google Calendar, finds when you and
              your team are all free, and books the meeting — with a Google
              Meet link, invites sent, one click.
            </p>
            <div className="mx-auto flex max-w-xs flex-col gap-2">
              <Link href="/auth/register" className="block">
                <Button className="inline-flex items-center justify-center gap-2">
                  Create your free account
                  <ArrowRight size={14} />
                </Button>
              </Link>
              <Link href="/auth/login" className="block">
                <Button variant="secondary">Sign in</Button>
              </Link>
            </div>
          </div>

          {/* Feature bullets — written so a Google OAuth reviewer can
              match every listed capability back to a requested scope. */}
          <section className="grid gap-4 text-left sm:grid-cols-2">
            <FeatureCard
              icon={CalendarDays}
              title="Reads your free/busy"
              body="Slotly reads only when you're busy — never event titles, attendees, or descriptions."
            />
            <FeatureCard
              icon={Calendar}
              title="Books meetings for you"
              body="One click creates the event on your calendar with everyone invited and a Meet link attached."
            />
            <FeatureCard
              icon={Sparkles}
              title="Public booking link"
              body="Share a URL so clients or colleagues book you directly — no email ping-pong."
            />
            <FeatureCard
              icon={CheckCircle2}
              title="Privacy first"
              body="Tokens encrypted at rest, no data sold, no ads, delete your account anytime."
            />
          </section>

          {/* Explicit Google API disclosure — helps reviewers tick their
              Limited Use checklist without hunting through the app. */}
          <section className="rounded-xl border border-zinc-200 bg-white p-6 text-left text-sm text-zinc-600 shadow-sm dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
            <h2 className="mb-2 text-sm font-semibold text-zinc-900 dark:text-zinc-50">
              How Slotly uses Google user data
            </h2>
            <ul className="ml-5 list-disc space-y-1">
              <li>
                <strong>Calendar free/busy</strong> — to compute when you and
                the people you&apos;re trying to meet are all available.
              </li>
              <li>
                <strong>Your list of calendars</strong> — to show you a picker
                for which calendar Slotly should write new meetings into (many
                users keep work and personal calendars separate). We read the
                list only; event contents are never read through this scope.
              </li>
              <li>
                <strong>Create calendar events</strong> — only when you or a
                visitor of your public link explicitly books a meeting.
              </li>
              <li>
                <strong>Your name and email</strong> (from Google userinfo) —
                to identify your Slotly account and to render your public
                profile.
              </li>
            </ul>
            <p className="mt-3 text-xs text-zinc-500 dark:text-zinc-500">
              Slotly&apos;s use of information received from Google APIs adheres
              to the{" "}
              <a
                href="https://developers.google.com/terms/api-services-user-data-policy"
                target="_blank"
                rel="noreferrer"
                className="underline"
              >
                Google API Services User Data Policy
              </a>
              , including the Limited Use requirements. Full detail in our{" "}
              <Link href="/privacy" className="underline">
                privacy policy
              </Link>
              .
            </p>
          </section>
        </div>
      </main>
    </div>
  );
}

function FeatureCard({
  icon: Icon,
  title,
  body,
}: {
  icon: LucideIcon;
  title: string;
  body: string;
}) {
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="mb-2 inline-flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-100 text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300">
        <Icon size={16} aria-hidden />
      </div>
      <h3 className="mb-1 text-sm font-semibold text-zinc-900 dark:text-zinc-50">{title}</h3>
      <p className="text-xs leading-relaxed text-zinc-600 dark:text-zinc-400">{body}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------

function relativeTime(iso: string): string {
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 60) return "just now";
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}
