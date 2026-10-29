"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthedHeader } from "@/components/AuthedHeader";
import { SettingsLayout } from "@/components/SettingsLayout";
import { SettingsNav } from "@/components/SettingsNav";
import { CardSkeleton, PageSkeleton } from "@/components/Skeleton";
import { Button, FormSuccess } from "@/components/ui";
import { getSession } from "@/lib/auth";
import {
  EVENT_LABELS,
  getNotificationPrefs,
  NOTIFICATION_EVENTS,
  patchNotificationPrefs,
  type NotificationEvent,
  type NotificationPrefs,
} from "@/lib/notifications";

export default function NotificationPrefsPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [prefs, setPrefs] = useState<NotificationPrefs | null>(null);
  const [saving, setSaving] = useState(false);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const session = await getSession();
      if (!session.meta?.is_authenticated || !session.data?.user) {
        router.replace("/auth/login");
        return;
      }
      setEmail(session.data.user.email);
      setPrefs(await getNotificationPrefs());
    })().catch(() => router.replace("/auth/login"));
  }, [router]);

  async function toggle(event: NotificationEvent, channel: "email" | "in_app") {
    if (!prefs) return;
    const next = {
      ...prefs,
      [event]: { ...prefs[event], [channel]: !prefs[event][channel] },
    };
    setPrefs(next);
    setSuccess(null);
    setSaving(true);
    try {
      const updated = await patchNotificationPrefs({ [event]: next[event] } as Partial<NotificationPrefs>);
      setPrefs(updated);
    } catch (err) {
      // revert on error
      setPrefs(prefs);
      alert(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  if (!prefs) {
    return (
      <PageSkeleton>
        <CardSkeleton rows={10} />
      </PageSkeleton>
    );
  }

  async function setAll(channel: "email" | "in_app", value: boolean) {
    if (!prefs) return;
    const next = Object.fromEntries(
      NOTIFICATION_EVENTS.map((e) => [e, { ...prefs[e], [channel]: value }]),
    ) as NotificationPrefs;
    setPrefs(next);
    setSuccess(null);
    setSaving(true);
    try {
      const updated = await patchNotificationPrefs(next);
      setPrefs(updated);
      setSuccess("Saved.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <AuthedHeader email={email} />
      <SettingsLayout width="narrow">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            Notification preferences
          </h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Toggle which channels deliver each kind of event.
          </p>
        </div>

        <SettingsNav />

        <FormSuccess message={success} />

        <section className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-100 dark:border-zinc-800">
                <th className="pb-2 text-left text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  Event
                </th>
                <th className="pb-2 text-center text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  In-app
                </th>
                <th className="pb-2 text-center text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  Email
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
              {NOTIFICATION_EVENTS.map((event) => {
                const row = prefs[event];
                return (
                  <tr key={event}>
                    <td className="py-3 pr-3 text-zinc-900 dark:text-zinc-100">
                      {EVENT_LABELS[event]}
                    </td>
                    <td className="py-3 text-center">
                      <input
                        type="checkbox"
                        checked={row.in_app}
                        onChange={() => toggle(event, "in_app")}
                        disabled={saving}
                        className="h-4 w-4 rounded border-zinc-300 text-zinc-900 focus:ring-zinc-900 dark:border-zinc-700 dark:bg-zinc-900"
                      />
                    </td>
                    <td className="py-3 text-center">
                      <input
                        type="checkbox"
                        checked={row.email}
                        onChange={() => toggle(event, "email")}
                        disabled={saving}
                        className="h-4 w-4 rounded border-zinc-300 text-zinc-900 focus:ring-zinc-900 dark:border-zinc-700 dark:bg-zinc-900"
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-zinc-100 pt-4 text-xs dark:border-zinc-800">
            <span className="text-zinc-500">Bulk:</span>
            <button
              type="button"
              onClick={() => setAll("in_app", true)}
              className="rounded-md border border-zinc-200 px-2 py-1 text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              All in-app on
            </button>
            <button
              type="button"
              onClick={() => setAll("in_app", false)}
              className="rounded-md border border-zinc-200 px-2 py-1 text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              All in-app off
            </button>
            <button
              type="button"
              onClick={() => setAll("email", true)}
              className="rounded-md border border-zinc-200 px-2 py-1 text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              All email on
            </button>
            <button
              type="button"
              onClick={() => setAll("email", false)}
              className="rounded-md border border-zinc-200 px-2 py-1 text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
            >
              All email off
            </button>
          </div>
        </section>

        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Email-verification and password-reset emails are always sent regardless of these settings.
        </p>
      </SettingsLayout>
    </div>
  );
}
