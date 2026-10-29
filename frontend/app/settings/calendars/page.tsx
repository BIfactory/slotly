"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Copy,
  ExternalLink,
  HelpCircle,
  Plus,
  RefreshCw,
  Share2,
  Unplug,
} from "lucide-react";
import { AuthedHeader } from "@/components/AuthedHeader";
import { SettingsNav } from "@/components/SettingsNav";
import { ProviderBadge } from "@/components/ProviderBadge";
import { CardSkeleton, ListSkeleton, PageSkeleton } from "@/components/Skeleton";
import { Button, FormError, FormSuccess, Input, Label } from "@/components/ui";
import { getSession } from "@/lib/auth";
import {
  type Calendar,
  type CalendarProvider,
  CalendarApiError,
  createCalendar,
  deleteCalendar,
  listCalendars,
  rotateBridgeToken,
  syncCalendar,
  updateCalendar,
} from "@/lib/calendars";
import {
  GOOGLE_CONNECT_URL,
  MICROSOFT_CONNECT_URL,
  getWritableCalendars,
  getMicrosoftWritableCalendars,
  type WritableCalendar,
} from "@/lib/google";
import {
  type CalendarAccount,
  listCalendarAccounts,
  setDefaultAccount,
  disconnectAccount,
  patchAccountWriteCalendar,
} from "@/lib/calendar-accounts";

const PROVIDER_LABEL: Record<CalendarProvider, string> = {
  google: "Google",
  apple: "Apple iCloud",
  outlook: "Outlook",
  other: "ICS",
};

const STATUS_BADGE: Record<Calendar["status"], { label: string; className: string }> = {
  ok: {
    label: "OK",
    className: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300",
  },
  syncing: {
    label: "Syncing…",
    className: "bg-amber-100 text-amber-800 dark:bg-amber-950/40 dark:text-amber-300",
  },
  sync_failing: {
    label: "Sync failing",
    className: "bg-amber-100 text-amber-800 dark:bg-amber-950/40 dark:text-amber-300",
  },
  unreachable: {
    label: "Unreachable",
    className: "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300",
  },
};

export default function CalendarsPage() {
  return (
    <Suspense
      fallback={
        <PageSkeleton>
          <CardSkeleton rows={3} />
          <ListSkeleton rows={3} className="mt-6" />
        </PageSkeleton>
      }
    >
      <CalendarsPageInner />
    </Suspense>
  );
}

function CalendarsPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [loaded, setLoaded] = useState(false);
  const [calendars, setCalendars] = useState<Calendar[]>([]);
  const [email, setEmail] = useState<string>("");
  const [accounts, setAccounts] = useState<CalendarAccount[]>([]);
  const [accountsError, setAccountsError] = useState<string | null>(null);

  const googleParam = searchParams?.get("google");
  const microsoftParam = searchParams?.get("microsoft");
  const linkedEmail = searchParams?.get("email");
  const reason = searchParams?.get("reason");
  const errorParam = searchParams?.get("error");

  // Clear transient query params without a full page reload.
  useEffect(() => {
    const hasTransient =
      googleParam != null ||
      microsoftParam != null ||
      linkedEmail != null ||
      reason != null ||
      errorParam != null;
    if (hasTransient) {
      router.replace("/settings/calendars");
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    (async () => {
      const session = await getSession();
      if (!session.meta?.is_authenticated) {
        router.replace("/auth/login");
        return;
      }
      setEmail(session.data?.user?.email ?? "");
      const [cals, accts] = await Promise.allSettled([
        listCalendars(),
        listCalendarAccounts(),
      ]);
      setCalendars(cals.status === "fulfilled" ? cals.value : []);
      if (accts.status === "fulfilled") {
        setAccounts(accts.value);
      } else {
        setAccountsError("Couldn't load connected accounts.");
      }
      setLoaded(true);
    })().catch(() => router.replace("/auth/login"));
  }, [router]);

  // While any calendar is in the transient "syncing" state, refresh the
  // list every 3s so the badge flips to OK / sync_failing as soon as the
  // worker finishes — no manual reload needed.
  useEffect(() => {
    const anySyncing = calendars.some((c) => c.status === "syncing");
    if (!anySyncing) return;
    const t = setInterval(async () => {
      try {
        const fresh = await listCalendars();
        setCalendars(fresh);
      } catch {
        /* transient — try again */
      }
    }, 3000);
    return () => clearInterval(t);
  }, [calendars]);

  const handleSetDefault = useCallback(async (id: string) => {
    // Determine which provider this account belongs to.
    const clickedProvider = id.startsWith("g:") ? "google" : "microsoft";

    // Capture snapshot before optimistic update.
    const snapshot = accounts;

    // Optimistic update: set is_default=true for the clicked account and
    // clear is_default on other accounts of the SAME provider only.
    // Accounts of the other provider keep their is_default state untouched.
    setAccounts((prev) =>
      prev.map((a) => {
        if (a.provider !== clickedProvider) return a;
        return { ...a, is_default: a.id === id };
      }),
    );
    try {
      const result = await setDefaultAccount(id);
      // Server response tells us the canonical per-provider default IDs.
      // Apply them to ensure UI is in sync without a round-trip.
      setAccounts((prev) =>
        prev.map((a) => ({
          ...a,
          is_default:
            a.id === result.google_default_id ||
            a.id === result.microsoft_default_id,
        })),
      );
    } catch (err) {
      // Revert to snapshot immediately on error.
      setAccounts(snapshot);
      // Optionally still re-fetch for defensive sync.
      listCalendarAccounts()
        .then(setAccounts)
        .catch(() => {});
      setAccountsError(
        err instanceof Error ? err.message : "Couldn't set default account.",
      );
      setTimeout(() => setAccountsError(null), 5000);
    }
  }, [accounts]);

  const handleDisconnect = useCallback(async (id: string) => {
    const account = accounts.find((a) => a.id === id);
    const label = account ? `${account.email} (${account.provider})` : id;
    if (
      !confirm(
        `Disconnect ${label}? Existing events stay in your calendar; only Slotly's access is revoked.`,
      )
    )
      return;
    try {
      await disconnectAccount(id);
      setAccounts((prev) => prev.filter((a) => a.id !== id));
    } catch (err) {
      setAccountsError(
        err instanceof Error ? err.message : "Couldn't disconnect account.",
      );
      setTimeout(() => setAccountsError(null), 5000);
    }
  }, [accounts]);

  const handlePatchWriteCalendar = useCallback(
    async (id: string, writeCalendarId: string) => {
      try {
        const updated = await patchAccountWriteCalendar(id, writeCalendarId);
        setAccounts((prev) => prev.map((a) => (a.id === id ? updated : a)));
      } catch (err) {
        setAccountsError(
          err instanceof Error ? err.message : "Couldn't save calendar choice.",
        );
        setTimeout(() => setAccountsError(null), 5000);
      }
    },
    [],
  );

  if (!loaded) {
    return (
      <PageSkeleton>
        <CardSkeleton rows={3} />
        <ListSkeleton rows={3} className="mt-6" />
      </PageSkeleton>
    );
  }

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <AuthedHeader email={email} />

      <main className="mx-auto max-w-2xl space-y-6 px-6 py-10">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            Your calendars
          </h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Connect a calendar so people can book time with you and new
            meetings land in the right place.
          </p>
        </div>

        <SettingsNav />

        {/* Connection result banners — visible before the param-clear redirect fires */}
        {googleParam === "connected" && (
          <Banner kind="ok">
            Google connected{linkedEmail ? ` as ${linkedEmail}` : ""}. You can
            now book meetings and receive bookings.
          </Banner>
        )}
        {googleParam === "error" && (
          <Banner kind="err">
            Google connection didn&apos;t go through{reason ? ` (${reason})` : ""}.
            Try again — if it keeps failing, sign out of Google in another tab first.
          </Banner>
        )}
        {microsoftParam === "connected" && (
          <Banner kind="ok">
            Microsoft connected{linkedEmail ? ` as ${linkedEmail}` : ""}. You can
            now book meetings and receive bookings.
          </Banner>
        )}
        {microsoftParam === "error" &&
          (reason === "admin_consent_required" ? (
            <Banner kind="err">
              Your IT admin needs to approve Slotly for your organization before
              you can connect. Ask them to sign in to{" "}
              <strong>Microsoft Entra admin center</strong> → Enterprise
              applications → search for &ldquo;Slotly&rdquo; → grant admin
              consent for your organization.
            </Banner>
          ) : (
            <Banner kind="err">
              Microsoft connection didn&apos;t go through{reason ? ` (${reason})` : ""}.
              Try again — if it keeps failing, sign out of Microsoft in another tab
              first.
            </Banner>
          ))}
        {errorParam === "too_many_accounts" && (
          <Banner kind="err">
            You can connect at most 10 accounts per provider. Disconnect one before
            adding another.
          </Banner>
        )}

        {/* Multi-account connected accounts section */}
        <section className="space-y-3">
          <SectionHeading
            title="Connected calendar accounts"
            subtitle="Sign in with Google or Microsoft — Slotly reads your free/busy and creates meetings with everyone invited. Each provider has its own default. New bookings use the provider matching the selected meeting link (Google Meet / Teams)."
          />

          {accountsError && (
            <Banner kind="err">{accountsError}</Banner>
          )}

          {accounts.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-2">
              <DefaultPickerCard
                provider="google"
                accounts={accounts.filter((a) => a.provider === "google")}
                currentDefaultId={
                  accounts.find((a) => a.provider === "google" && a.is_default)?.id ?? null
                }
                onSelect={handleSetDefault}
              />
              <DefaultPickerCard
                provider="microsoft"
                accounts={accounts.filter((a) => a.provider === "microsoft")}
                currentDefaultId={
                  accounts.find((a) => a.provider === "microsoft" && a.is_default)?.id ?? null
                }
                onSelect={handleSetDefault}
              />
            </div>
          )}

          {accounts.length === 0 ? (
            <EmptyAccountsPanel />
          ) : (
            <AccountsTable
              accounts={accounts}
              onDisconnect={handleDisconnect}
              onPatchWriteCalendar={handlePatchWriteCalendar}
            />
          )}

          {/* Add account buttons */}
          <div className="flex flex-wrap gap-2 pt-1">
            <a
              href={GOOGLE_CONNECT_URL}
              title="You'll be sent through Google's sign-in again"
              className="inline-flex items-center gap-1.5 rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
            >
              <Plus size={14} aria-hidden />
              {accounts.some((a) => a.provider === "google")
                ? "Add another Google account"
                : "Connect Google Calendar"}
            </a>
            <a
              href={MICROSOFT_CONNECT_URL}
              title="You'll be sent through Microsoft's sign-in again"
              className="inline-flex items-center gap-1.5 rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
            >
              <Plus size={14} aria-hidden />
              {accounts.some((a) => a.provider === "microsoft")
                ? "Add another Microsoft account"
                : "Connect Microsoft 365 / Outlook"}
            </a>
          </div>
        </section>

        <section className="space-y-3">
          <SectionHeading
            title="URL subscriptions (ICS)"
            subtitle="Older / alternative method — paste a private iCal URL from any provider. Slotly reads free/busy only, refreshing every 5 minutes. Use this for iCloud or any provider without OAuth."
          />
          <AddCalendarForm
            onAdded={(cal) =>
              setCalendars((prev) => [cal, ...prev.filter((c) => c.id !== cal.id)])
            }
          />
          <BusyRulesHelp />
          <CalendarList calendars={calendars} onChange={setCalendars} />
        </section>
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Multi-account table
// ---------------------------------------------------------------------------

function EmptyAccountsPanel() {
  return (
    <section className="rounded-xl border border-dashed border-zinc-300 bg-white p-8 text-center dark:border-zinc-700 dark:bg-zinc-900">
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        You haven&apos;t connected any calendar accounts yet. Use the buttons
        below to connect Google or Microsoft.
      </p>
    </section>
  );
}

function AccountsTable({
  accounts,
  onDisconnect,
  onPatchWriteCalendar,
}: {
  accounts: CalendarAccount[];
  onDisconnect: (id: string) => Promise<void>;
  onPatchWriteCalendar: (id: string, writeCalendarId: string) => Promise<void>;
}) {
  const rowProps = {
    onDisconnect,
    onPatchWriteCalendar,
  };

  return (
    <>
      {/* ── Desktop (≥768px): 4-column table (no Default column — picker above) ── */}
      <div className="hidden overflow-hidden rounded-xl border border-zinc-200 bg-white md:block dark:border-zinc-800 dark:bg-zinc-900">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                Provider
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                Account
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                Write calendar
              </th>
              <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
            {accounts.map((account) => (
              <AccountRow key={account.id} account={account} {...rowProps} />
            ))}
          </tbody>
        </table>
      </div>

      {/* ── Mobile (<768px): stacked card list ── */}
      <div className="space-y-2 md:hidden">
        {accounts.map((account) => (
          <AccountCard key={account.id} account={account} {...rowProps} />
        ))}
      </div>
    </>
  );
}

function AccountRow({
  account,
  onDisconnect,
  onPatchWriteCalendar,
}: {
  account: CalendarAccount;
  onDisconnect: (id: string) => Promise<void>;
  onPatchWriteCalendar: (id: string, writeCalendarId: string) => Promise<void>;
}) {
  const [disconnecting, setDisconnecting] = useState(false);
  const [calendars, setCalendars] = useState<WritableCalendar[] | null>(null);
  const [savingCalendar, setSavingCalendar] = useState(false);

  // Fetch writable calendars for THIS account specifically using its composite
  // id (e.g. "g:3" / "m:7") so the backend resolves the correct OAuth token.
  useEffect(() => {
    let alive = true;
    const fetcher =
      account.provider === "google" ? getWritableCalendars : getMicrosoftWritableCalendars;
    fetcher(account.id)
      .then((list) => {
        if (alive) setCalendars(list);
      })
      .catch(() => {
        if (alive) setCalendars([]);
      });
    return () => {
      alive = false;
    };
  }, [account.id, account.provider]);

  async function handleDisconnect() {
    setDisconnecting(true);
    try {
      await onDisconnect(account.id);
    } finally {
      setDisconnecting(false);
    }
  }

  async function handlePickCalendar(writeCalendarId: string) {
    if (savingCalendar) return;
    setSavingCalendar(true);
    try {
      await onPatchWriteCalendar(account.id, writeCalendarId);
    } finally {
      setSavingCalendar(false);
    }
  }

  return (
    <tr className="group align-middle">
      {/* Provider icon */}
      <td className="px-4 py-3">
        {account.provider === "google" ? (
          <GoogleMark size="sm" />
        ) : (
          <MicrosoftMark size="sm" />
        )}
      </td>

      {/* Email + optional default badge */}
      <td className="px-4 py-3">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-zinc-900 dark:text-zinc-50">
            {account.email}
          </span>
          <span className="text-xs text-zinc-400 dark:text-zinc-500">
            {account.provider === "google" ? "Google" : "Microsoft"}
          </span>
          {account.is_default && (
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
              <span className="text-emerald-500" aria-hidden>●</span>
              Default
            </span>
          )}
        </div>
      </td>

      {/* Write calendar picker */}
      <td className="px-4 py-3">
        {calendars === null ? (
          <div className="h-7 w-32 animate-pulse rounded-md bg-zinc-100 dark:bg-zinc-800" />
        ) : calendars.length > 0 ? (
          <select
            value={account.write_calendar_id}
            onChange={(e) => handlePickCalendar(e.target.value)}
            disabled={savingCalendar}
            className="min-w-[10rem] rounded-md border border-zinc-300 bg-white px-2 py-1 text-xs text-zinc-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
          >
            {!calendars.some((c) => c.id === account.write_calendar_id) &&
              account.write_calendar_id !== "" && (
                <option value={account.write_calendar_id}>
                  {account.write_calendar_id} (missing)
                </option>
              )}
            {calendars.map((c) => (
              <option key={c.id} value={c.id}>
                {c.summary}
                {c.primary ? " (primary)" : ""}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-xs text-zinc-400 dark:text-zinc-500">—</span>
        )}
      </td>

      {/* Actions */}
      <td className="px-4 py-3 text-right">
        <button
          type="button"
          onClick={handleDisconnect}
          disabled={disconnecting}
          className="inline-flex items-center gap-1 rounded-md border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950/40"
          aria-label={`Disconnect ${account.email}`}
        >
          <Unplug size={12} aria-hidden />
          {disconnecting ? "Removing…" : "Disconnect"}
        </button>
      </td>
    </tr>
  );
}

// Mobile-only stacked card for a single account (<md breakpoint).
// Mirrors the same state logic as AccountRow but renders as a card.
function AccountCard({
  account,
  onDisconnect,
  onPatchWriteCalendar,
}: {
  account: CalendarAccount;
  onDisconnect: (id: string) => Promise<void>;
  onPatchWriteCalendar: (id: string, writeCalendarId: string) => Promise<void>;
}) {
  const [disconnecting, setDisconnecting] = useState(false);
  const [calendars, setCalendars] = useState<WritableCalendar[] | null>(null);
  const [savingCalendar, setSavingCalendar] = useState(false);

  useEffect(() => {
    let alive = true;
    const fetcher =
      account.provider === "google" ? getWritableCalendars : getMicrosoftWritableCalendars;
    fetcher(account.id)
      .then((list) => {
        if (alive) setCalendars(list);
      })
      .catch(() => {
        if (alive) setCalendars([]);
      });
    return () => {
      alive = false;
    };
  }, [account.id, account.provider]);

  async function handleDisconnect() {
    setDisconnecting(true);
    try {
      await onDisconnect(account.id);
    } finally {
      setDisconnecting(false);
    }
  }

  async function handlePickCalendar(writeCalendarId: string) {
    if (savingCalendar) return;
    setSavingCalendar(true);
    try {
      await onPatchWriteCalendar(account.id, writeCalendarId);
    } finally {
      setSavingCalendar(false);
    }
  }

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      {/* Row 1: provider icon + email + provider label + optional default badge */}
      <div className="flex items-center gap-3">
        {account.provider === "google" ? (
          <GoogleMark size="sm" />
        ) : (
          <MicrosoftMark size="sm" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="truncate font-medium text-zinc-900 dark:text-zinc-50">
              {account.email}
            </p>
            {account.is_default && (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
                <span className="text-emerald-500" aria-hidden>●</span>
                Default
              </span>
            )}
          </div>
          <p className="text-xs text-zinc-400 dark:text-zinc-500">
            {account.provider === "google" ? "Google" : "Microsoft"}
          </p>
        </div>
      </div>

      {/* Row 2: Write calendar picker */}
      <div className="mt-3">
        <label className="mb-1 block text-xs font-medium text-zinc-500 dark:text-zinc-400">
          Write calendar
        </label>
        {calendars === null ? (
          <div className="h-8 w-full animate-pulse rounded-md bg-zinc-100 dark:bg-zinc-800" />
        ) : calendars.length > 0 ? (
          <select
            value={account.write_calendar_id}
            onChange={(e) => handlePickCalendar(e.target.value)}
            disabled={savingCalendar}
            className="w-full max-w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm text-zinc-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
          >
            {!calendars.some((c) => c.id === account.write_calendar_id) &&
              account.write_calendar_id !== "" && (
                <option value={account.write_calendar_id}>
                  {account.write_calendar_id} (missing)
                </option>
              )}
            {calendars.map((c) => (
              <option key={c.id} value={c.id}>
                {c.summary}
                {c.primary ? " (primary)" : ""}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-xs text-zinc-400 dark:text-zinc-500">—</span>
        )}
      </div>

      {/* Row 3: Disconnect button, right-aligned, ≥44px touch target */}
      <div className="mt-3 flex justify-end">
        <button
          type="button"
          onClick={handleDisconnect}
          disabled={disconnecting}
          className="inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-red-200 px-4 py-2 text-sm font-medium text-red-600 hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950/40"
          aria-label={`Disconnect ${account.email}`}
        >
          <Unplug size={14} aria-hidden />
          {disconnecting ? "Removing…" : "Disconnect"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-provider default picker card
// ---------------------------------------------------------------------------

function DefaultPickerCard({
  provider,
  accounts,
  currentDefaultId,
  onSelect,
}: {
  provider: "google" | "microsoft";
  accounts: CalendarAccount[];
  currentDefaultId: string | null;
  onSelect: (id: string) => Promise<void>;
}) {
  const [loading, setLoading] = useState(false);

  const providerLabel = provider === "google" ? "Google" : "Microsoft";
  const noAccountsPlaceholder =
    provider === "google"
      ? "No Google accounts connected"
      : "No Microsoft accounts connected";
  const isDisabled = accounts.length <= 1 || loading;

  async function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const id = e.target.value;
    if (!id || id === currentDefaultId) return;
    setLoading(true);
    try {
      await onSelect(id);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
      <label
        htmlFor={`default-picker-${provider}`}
        className="mb-2 block text-sm font-medium text-zinc-700 dark:text-zinc-300"
      >
        Default {providerLabel} account for new bookings
      </label>
      {accounts.length === 0 ? (
        <>
          <select
            id={`default-picker-${provider}`}
            disabled
            className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-400 disabled:cursor-not-allowed disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-500"
            aria-label={noAccountsPlaceholder}
          >
            <option value="">{noAccountsPlaceholder}</option>
          </select>
        </>
      ) : (
        <>
          <select
            id={`default-picker-${provider}`}
            value={currentDefaultId ?? ""}
            onChange={handleChange}
            disabled={isDisabled}
            className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:cursor-not-allowed disabled:opacity-60 dark:border-zinc-700 dark:bg-zinc-950 dark:text-zinc-100"
          >
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.email}
              </option>
            ))}
          </select>
          {accounts.length === 1 && (
            <p className="mt-1.5 text-xs text-zinc-500 dark:text-zinc-400">
              Only one account — default automatic
            </p>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared presentational components
// ---------------------------------------------------------------------------

function SectionHeading({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <header>
      <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-50">{title}</h2>
      <p className="mt-0.5 text-sm text-zinc-600 dark:text-zinc-400">{subtitle}</p>
    </header>
  );
}

function Banner({ kind, children }: { kind: "ok" | "err"; children: ReactNode }) {
  const Icon = kind === "ok" ? CheckCircle2 : AlertTriangle;
  const tone =
    kind === "ok"
      ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800/60 dark:bg-emerald-950/30 dark:text-emerald-100"
      : "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-100";
  return (
    <div className={`flex items-start gap-2 rounded-md border p-3 text-sm leading-relaxed ${tone}`}>
      <Icon size={16} className="mt-0.5 shrink-0" aria-hidden />
      <p>{children}</p>
    </div>
  );
}

function GoogleMark({ size = "md" }: { size?: "sm" | "md" }) {
  const dim = size === "sm" ? "h-7 w-7" : "h-9 w-9";
  const svgDim = size === "sm" ? 18 : 22;
  return (
    <span
      aria-hidden
      className={`inline-flex ${dim} shrink-0 items-center justify-center rounded-full border border-zinc-200 bg-white dark:border-zinc-700 dark:bg-zinc-950`}
    >
      <svg viewBox="0 0 48 48" width={svgDim} height={svgDim}>
        <path fill="#4285F4" d="M44 24c0-1.6-.1-2.8-.5-4.1H24v7.7h11.4c-.5 2.8-2.1 5.2-4.5 6.8v5.6h7.3c4.3-3.9 6.8-9.7 6.8-16z" />
        <path fill="#34A853" d="M24 44c6.2 0 11.4-2 15.2-5.6l-7.3-5.6c-2 1.4-4.6 2.3-7.9 2.3-6 0-11.1-4.1-12.9-9.5H3.5v6c3.8 7.5 11.6 12.4 20.5 12.4z" />
        <path fill="#FBBC05" d="M11.1 25.6c-.5-1.4-.7-2.9-.7-4.6s.3-3.2.7-4.6v-6H3.5C1.8 13.7 1 17 1 20.5s.8 6.8 2.5 9.6l7.6-4.5z" />
        <path fill="#EA4335" d="M24 8.6c3.4 0 6.4 1.2 8.8 3.5l6.6-6.6C35.4 2 30.2 0 24 0 15.1 0 7.3 5 3.5 12.4l7.6 6c1.8-5.4 6.9-9.5 12.9-9.5z" />
      </svg>
    </span>
  );
}

function MicrosoftMark({ size = "md" }: { size?: "sm" | "md" }) {
  const dim = size === "sm" ? "h-7 w-7" : "h-9 w-9";
  const svgDim = size === "sm" ? 16 : 20;
  return (
    <span
      aria-hidden
      className={`inline-flex ${dim} shrink-0 items-center justify-center rounded-full border border-zinc-200 bg-white dark:border-zinc-700 dark:bg-zinc-950`}
    >
      <svg viewBox="0 0 24 24" width={svgDim} height={svgDim}>
        <rect x="1" y="1" width="10" height="10" fill="#F25022" />
        <rect x="13" y="1" width="10" height="10" fill="#7FBA00" />
        <rect x="1" y="13" width="10" height="10" fill="#00A4EF" />
        <rect x="13" y="13" width="10" height="10" fill="#FFB900" />
      </svg>
    </span>
  );
}

// ---------------------------------------------------------------------------

function AddCalendarForm({ onAdded }: { onAdded: (cal: Calendar) => void }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    setSuccess(null);
    setSubmitting(true);
    try {
      const created = await createCalendar({ name, url });
      setSuccess(
        "Saved. Slotly is reading the calendar — it'll appear in your free/busy data in a few seconds.",
      );
      onAdded(created);
      setName("");
      setUrl("");
    } catch (err) {
      if (err instanceof CalendarApiError) {
        const fields = err.fields;
        const firstError =
          (typeof fields.url === "string" && fields.url) ||
          (Array.isArray(fields.url) && String(fields.url[0])) ||
          (typeof fields.name === "string" && fields.name) ||
          (Array.isArray(fields.name) && String(fields.name[0])) ||
          err.message;
        setError(String(firstError));
      } else {
        setError(err instanceof Error ? err.message : "Network error");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <header className="mb-5">
        <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">Add a calendar</h2>
        <p className="mt-0.5 text-sm text-zinc-600 dark:text-zinc-400">
          Paste your private iCal/ICS URL — find it in your provider&apos;s settings.
        </p>
      </header>
      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="cal-name">Display name</Label>
          <Input
            id="cal-name"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Work, Personal…"
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="cal-url">ICS URL</Label>
          <Input
            id="cal-url"
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…/basic.ics or webcal://…"
          />
          <ProviderHelp />
        </div>
        <FormError message={error} />
        <FormSuccess message={success} />
        <Button type="submit" disabled={submitting} className="sm:w-auto sm:px-6">
          {submitting ? "Connecting…" : "Connect calendar"}
        </Button>
      </form>
    </section>
  );
}

function BusyRulesHelp() {
  return (
    <details className="group/help overflow-hidden rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-medium text-zinc-900 dark:text-zinc-100 transition-colors hover:bg-zinc-50 dark:hover:bg-zinc-800/40">
        <HelpCircle size={16} className="shrink-0" aria-hidden="true" />
        <span className="flex-1">What counts as &ldquo;busy&rdquo; in Slotly?</span>
        <ChevronDown
          size={16}
          className="shrink-0 transition-transform group-open/help:rotate-180"
          aria-hidden="true"
        />
      </summary>
      <div className="space-y-3 border-t border-zinc-200 px-4 py-3 text-sm text-zinc-700 dark:border-zinc-800 dark:text-zinc-300">
        <p>
          When Slotly searches for free time, it intersects everyone&apos;s
          working hours and subtracts their busy intervals. An event from
          your calendar counts as &ldquo;busy&rdquo; when:
        </p>
        <ul className="ml-4 list-disc space-y-1">
          <li>It&apos;s inside the search window <em>and</em> not cancelled.</li>
          <li>
            It&apos;s either marked <strong>busy</strong> by your calendar
            (default for normal time-bound events) <strong>or</strong> it&apos;s
            an <strong>all-day event</strong>. All-day events block the whole
            day even if your calendar marks them &ldquo;free&rdquo; — typical
            use is vacation, sick day, or out-of-office.
          </li>
          <li>
            Manual <em>Unavailability</em> blocks (Profile → Unavailability)
            also count and are independent of any calendar.
          </li>
        </ul>
        <p>
          What does <em>not</em> block: events you marked &ldquo;Show as
          free&rdquo; in your calendar (e.g. a lunch reminder), cancelled
          events, and tentative events whose calendar marks them transparent.
        </p>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Slotly never reads event titles, descriptions, or attendees — only
          start/end and busy/free flags.
        </p>
      </div>
    </details>
  );
}

function ProviderHelp() {
  return (
    <details className="group/help overflow-hidden rounded-lg border border-indigo-200 bg-indigo-50/60 dark:border-indigo-900/60 dark:bg-indigo-950/20">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-medium text-indigo-900 transition-colors hover:bg-indigo-100/60 dark:text-indigo-100 dark:hover:bg-indigo-950/40">
        <HelpCircle size={16} className="shrink-0" aria-hidden="true" />
        <span className="flex-1">How do I find my ICS URL?</span>
        <ChevronDown
          size={16}
          aria-hidden="true"
          className="shrink-0 text-indigo-600 transition-transform group-open/help:rotate-180 dark:text-indigo-300"
        />
      </summary>
      <div className="space-y-3 border-t border-indigo-200/70 bg-white/80 p-4 dark:border-indigo-900/60 dark:bg-zinc-900/60">
        <ProviderHelpCard
          provider="google"
          name="Google Calendar"
          steps={[
            <>
              Open{" "}
              <ExtLink href="https://calendar.google.com/calendar/u/0/r/settings">
                Google Calendar settings
              </ExtLink>{" "}
              and pick the calendar you want to share.
            </>,
            <>
              Scroll to <em>Integrate calendar</em> →{" "}
              <em>Secret address in iCal format</em>.
            </>,
            "Copy that URL and paste it above.",
          ]}
        />

        <ProviderHelpCard
          provider="outlook"
          name="Microsoft 365 / Outlook"
          steps={[
            <>
              Open{" "}
              <ExtLink href="https://outlook.office.com/calendar/options/calendar/SharedCalendars">
                Outlook web → Settings → Shared calendars
              </ExtLink>
              .
            </>,
            <>
              Under <em>Publish a calendar</em>, pick the calendar (usually
              &quot;Calendar&quot;) and choose <em>Can view all details</em>.
              Click <em>Publish</em>.
            </>,
            <>
              Two links appear — copy the one that ends in{" "}
              <Code>.ics</Code> and paste it above.
            </>,
          ]}
        >
          <Alert>
            <strong>Heads up:</strong> many corporate Microsoft 365 tenants
            disable calendar publishing by IT policy. If{" "}
            <em>Publish a calendar</em> is missing or greyed out, ask your
            admin or use a personal Outlook.com account.
          </Alert>
        </ProviderHelpCard>

        <ProviderHelpCard
          provider="apple"
          name="Apple iCloud"
          steps={[
            <>
              Sign in to{" "}
              <ExtLink href="https://www.icloud.com/calendar">icloud.com/calendar</ExtLink>.
            </>,
            <>
              Hover the calendar in the left sidebar → click the share icon →
              toggle <em>Public Calendar</em>.
            </>,
            <>
              Click <em>Copy Link</em>. If the URL starts with{" "}
              <Code>webcal://</Code> we&apos;ll convert it automatically.
            </>,
          ]}
        />

        <ProviderHelpCard
          provider="other"
          name="Other / generic ICS"
          intro={
            <>
              Any HTTPS URL ending in <Code>.ics</Code> that returns valid
              iCalendar (RFC 5545) data works. Slotly polls it every 5 minutes.
            </>
          }
        />
      </div>
    </details>
  );
}

function ProviderHelpCard({
  provider,
  name,
  steps,
  intro,
  children,
}: {
  provider: "google" | "outlook" | "apple" | "other";
  name: string;
  steps?: React.ReactNode[];
  intro?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <section className="rounded-md border border-zinc-200 bg-white p-3 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <header className="mb-2 flex items-center gap-2">
        <ProviderBadge provider={provider} size={28} />
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-50">{name}</h3>
      </header>
      {intro && (
        <p className="text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">{intro}</p>
      )}
      {steps && (
        <ol className="space-y-2 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
          {steps.map((step, i) => (
            <li key={i} className="flex items-start gap-2.5">
              <span className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-indigo-100 text-[11px] font-semibold text-indigo-700 dark:bg-indigo-950/60 dark:text-indigo-300">
                {i + 1}
              </span>
              <span className="flex-1">{step}</span>
            </li>
          ))}
        </ol>
      )}
      {children}
    </section>
  );
}

function ExtLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-0.5 font-medium text-indigo-700 underline decoration-indigo-300 underline-offset-2 hover:decoration-indigo-500 dark:text-indigo-300 dark:decoration-indigo-700"
    >
      {children}
      <ExternalLink size={12} className="shrink-0" aria-hidden="true" />
    </a>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded border border-zinc-200 bg-zinc-50 px-1.5 py-0.5 font-mono text-[0.85em] text-zinc-700 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200">
      {children}
    </code>
  );
}

function Alert({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-3 flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm leading-relaxed text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-100">
      <AlertTriangle
        size={16}
        className="mt-0.5 shrink-0 text-amber-600 dark:text-amber-400"
        aria-hidden="true"
      />
      <p>{children}</p>
    </div>
  );
}

// ---------------------------------------------------------------------------

function CalendarList({
  calendars,
  onChange,
}: {
  calendars: Calendar[];
  onChange: (next: Calendar[]) => void;
}) {
  if (calendars.length === 0) {
    return (
      <section className="rounded-xl border border-dashed border-zinc-300 bg-white p-8 text-center text-sm text-zinc-500 dark:border-zinc-700 dark:bg-zinc-900">
        No calendars connected yet. Add one above to get started.
      </section>
    );
  }
  return (
    <section className="space-y-3">
      {calendars.map((c) => (
        <CalendarRow
          key={c.id}
          calendar={c}
          onUpdated={(updated) => onChange(calendars.map((x) => (x.id === updated.id ? updated : x)))}
          onDeleted={() => onChange(calendars.filter((x) => x.id !== c.id))}
        />
      ))}
    </section>
  );
}

function CalendarRow({
  calendar,
  onUpdated,
  onDeleted,
}: {
  calendar: Calendar;
  onUpdated: (c: Calendar) => void;
  onDeleted: () => void;
}) {
  const [busy, setBusy] = useState<"sync" | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onSync() {
    setError(null);
    setBusy("sync");
    try {
      onUpdated(await syncCalendar(calendar.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sync failed");
    } finally {
      setBusy(null);
    }
  }

  async function onDelete() {
    if (!confirm(`Remove "${calendar.name}"? This deletes its cached events from Slotly.`)) return;
    setBusy("delete");
    try {
      await deleteCalendar(calendar.id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
      setBusy(null);
    }
  }

  const lastSynced = calendar.last_synced_at
    ? new Date(calendar.last_synced_at).toLocaleString()
    : "never";
  const badge = STATUS_BADGE[calendar.status];

  return (
    <article className="rounded-xl border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-start gap-4">
        <ProviderBadge provider={calendar.provider} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate font-medium text-zinc-900 dark:text-zinc-50">{calendar.name}</h3>
            <span className="rounded-full border border-zinc-200 px-2 py-0.5 text-xs text-zinc-600 dark:border-zinc-700 dark:text-zinc-300">
              {PROVIDER_LABEL[calendar.provider]}
            </span>
            <span className={`rounded-full px-2 py-0.5 text-xs ${badge.className}`}>{badge.label}</span>
          </div>
          <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">Last synced: {lastSynced}</p>
          {calendar.last_error && calendar.status !== "ok" && (
            <p className="mt-1 text-xs text-red-600 dark:text-red-300">{calendar.last_error}</p>
          )}
          {calendar.sync && calendar.sync.status_code === 200 && (
            <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
              Last fetch: {calendar.sync.written} events stored
              {typeof calendar.sync.deleted === "number" && `, ${calendar.sync.deleted} replaced`}.
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            onClick={onSync}
            disabled={busy !== null}
            className="rounded-md border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
          >
            {busy === "sync" ? "Syncing…" : "Sync now"}
          </button>
          <button
            onClick={onDelete}
            disabled={busy !== null}
            className="rounded-md border border-red-200 px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950/40"
          >
            {busy === "delete" ? "Removing…" : "Remove"}
          </button>
        </div>
      </div>
      {error && <p className="mt-3 text-xs text-red-600 dark:text-red-300">{error}</p>}
      <BridgeSection calendar={calendar} onUpdated={onUpdated} />
    </article>
  );
}

// ---------------------------------------------------------------------------

const COMMON_TIMEZONES = [
  "Europe/Prague",
  "Europe/Berlin",
  "Europe/Vienna",
  "Europe/London",
  "Europe/Paris",
  "Europe/Warsaw",
  "Europe/Budapest",
  "Europe/Bucharest",
  "Europe/Istanbul",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "UTC",
];

function BridgeSection({
  calendar,
  onUpdated,
}: {
  calendar: Calendar;
  onUpdated: (c: Calendar) => void;
}) {
  const [open, setOpen] = useState(calendar.bridge_enabled);
  const [busy, setBusy] = useState<"toggle" | "tz" | "rotate" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [tz, setTz] = useState(calendar.source_timezone || "Europe/Prague");

  async function setEnabled(next: boolean) {
    setError(null);
    setBusy("toggle");
    try {
      const updated = await updateCalendar(calendar.id, {
        bridge_enabled: next,
        source_timezone: tz,
      });
      onUpdated(updated);
      if (next) setOpen(true);
    } catch (err) {
      setError(extractErr(err, "Couldn't update bridge"));
    } finally {
      setBusy(null);
    }
  }

  async function saveTz(value: string) {
    setError(null);
    setTz(value);
    if (!calendar.bridge_enabled) return;
    setBusy("tz");
    try {
      const updated = await updateCalendar(calendar.id, { source_timezone: value });
      onUpdated(updated);
    } catch (err) {
      setError(extractErr(err, "Couldn't save timezone"));
    } finally {
      setBusy(null);
    }
  }

  async function onRotate() {
    if (!confirm("Generate a new bridge URL? The old one will stop working immediately.")) return;
    setError(null);
    setBusy("rotate");
    try {
      onUpdated(await rotateBridgeToken(calendar.id));
    } catch (err) {
      setError(extractErr(err, "Couldn't rotate token"));
    } finally {
      setBusy(null);
    }
  }

  async function onCopy() {
    if (!calendar.bridge_url) return;
    try {
      await navigator.clipboard.writeText(calendar.bridge_url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Copy failed — select the URL manually.");
    }
  }

  return (
    <div className="mt-4 rounded-lg border border-zinc-200 bg-zinc-50/60 dark:border-zinc-800 dark:bg-zinc-950/40">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm font-medium text-zinc-800 dark:text-zinc-100"
      >
        <Share2 size={16} className="shrink-0 text-indigo-600 dark:text-indigo-300" aria-hidden />
        <span className="flex-1">Bridge to Google Calendar</span>
        {calendar.bridge_enabled && (
          <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
            On
          </span>
        )}
        <ChevronDown
          size={16}
          className={`shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
          aria-hidden
        />
      </button>
      {open && (
        <div className="space-y-4 border-t border-zinc-200 px-4 py-4 dark:border-zinc-800">
          <p className="text-sm text-zinc-700 dark:text-zinc-300">
            Outlook&apos;s published feeds use Windows-style timezone names that
            Google Calendar ignores, causing times to display shifted. Turn this
            on and we&apos;ll re-serve the calendar with IANA timezones — paste
            the resulting URL into Google&apos;s &quot;From URL&quot; option.
          </p>

          <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
            <div className="space-y-1.5">
              <Label htmlFor={`tz-${calendar.id}`}>Calendar timezone</Label>
              <select
                id={`tz-${calendar.id}`}
                value={tz}
                onChange={(e) => saveTz(e.target.value)}
                disabled={busy !== null}
                className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100"
              >
                {COMMON_TIMEZONES.includes(tz) ? null : <option value={tz}>{tz}</option>}
                {COMMON_TIMEZONES.map((z) => (
                  <option key={z} value={z}>
                    {z}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              onClick={() => setEnabled(!calendar.bridge_enabled)}
              disabled={busy !== null}
              className={`h-9 rounded-md px-4 text-sm font-medium disabled:opacity-50 ${
                calendar.bridge_enabled
                  ? "border border-zinc-300 text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
                  : "bg-indigo-600 text-white hover:bg-indigo-700"
              }`}
            >
              {busy === "toggle"
                ? "Saving…"
                : calendar.bridge_enabled
                  ? "Disable bridge"
                  : "Enable bridge"}
            </button>
          </div>

          {calendar.bridge_enabled && calendar.bridge_url && (
            <>
              <div className="space-y-1.5">
                <Label htmlFor={`bridge-url-${calendar.id}`}>Bridge URL (paste into Google)</Label>
                <div className="flex gap-2">
                  <input
                    id={`bridge-url-${calendar.id}`}
                    readOnly
                    value={calendar.bridge_url}
                    onFocus={(e) => e.currentTarget.select()}
                    className="flex-1 rounded-md border border-zinc-300 bg-white px-3 py-2 font-mono text-xs text-zinc-700 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200"
                  />
                  <button
                    type="button"
                    onClick={onCopy}
                    className="inline-flex items-center gap-1.5 rounded-md border border-zinc-300 px-3 text-sm font-medium text-zinc-700 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
                  >
                    <Copy size={14} aria-hidden />
                    {copied ? "Copied" : "Copy"}
                  </button>
                  <button
                    type="button"
                    onClick={onRotate}
                    disabled={busy !== null}
                    className="inline-flex items-center gap-1.5 rounded-md border border-zinc-300 px-3 text-sm font-medium text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
                  >
                    <RefreshCw size={14} aria-hidden />
                    {busy === "rotate" ? "…" : "Rotate"}
                  </button>
                </div>
              </div>

              <ol className="ml-5 list-decimal space-y-1 text-sm text-zinc-700 dark:text-zinc-300">
                <li>
                  Open{" "}
                  <a
                    href="https://calendar.google.com/calendar/u/0/r/settings/addbyurl"
                    target="_blank"
                    rel="noreferrer"
                    className="font-medium text-indigo-700 underline decoration-indigo-300 hover:decoration-indigo-500 dark:text-indigo-300 dark:decoration-indigo-700"
                  >
                    Google Calendar → Add from URL
                  </a>
                  .
                </li>
                <li>Paste the URL above and click <em>Add calendar</em>.</li>
                <li>
                  Wait up to 24 hours — Google refreshes external calendars
                  slowly. Changes in Outlook won&apos;t appear immediately.
                </li>
              </ol>

              <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs leading-relaxed text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-100">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden />
                <p>
                  Anyone with this URL can read the calendar — treat it like a
                  password. Use <em>Rotate</em> if you ever paste it somewhere
                  by mistake.
                </p>
              </div>
            </>
          )}

          {error && <p className="text-xs text-red-600 dark:text-red-300">{error}</p>}
        </div>
      )}
    </div>
  );
}

function extractErr(err: unknown, fallback: string): string {
  if (err instanceof CalendarApiError) {
    const fields = err.fields;
    for (const v of Object.values(fields)) {
      if (typeof v === "string") return v;
      if (Array.isArray(v) && typeof v[0] === "string") return v[0];
    }
    return err.message || fallback;
  }
  return err instanceof Error ? err.message : fallback;
}
