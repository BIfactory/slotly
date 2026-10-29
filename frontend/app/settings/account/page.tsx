"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { AuthedHeader } from "@/components/AuthedHeader";
import { SettingsLayout } from "@/components/SettingsLayout";
import { SettingsNav } from "@/components/SettingsNav";
import { CardSkeleton, PageSkeleton } from "@/components/Skeleton";
import { Button, FormError, Input, Label } from "@/components/ui";
import { getSession } from "@/lib/auth";
import {
  deleteMe,
  getDeleteMePreview,
  MeApiError,
  type DeleteMePreview,
} from "@/lib/me";

export default function AccountSettingsPage() {
  const router = useRouter();
  const [email, setEmail] = useState<string | null>(null);
  const [preview, setPreview] = useState<DeleteMePreview | null>(null);
  const [confirmEmail, setConfirmEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const session = await getSession();
      if (!session.meta?.is_authenticated || !session.data?.user) {
        router.replace("/auth/login");
        return;
      }
      setEmail(session.data.user.email);
      setPreview(await getDeleteMePreview());
    })().catch(() => router.replace("/auth/login"));
  }, [router]);

  if (email === null || preview === null) {
    return (
      <PageSkeleton>
        <CardSkeleton rows={6} />
      </PageSkeleton>
    );
  }

  const confirmsMatch = confirmEmail.trim().toLowerCase() === email.toLowerCase();

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    if (!confirmsMatch) {
      setError("The email you typed doesn't match.");
      return;
    }
    setSubmitting(true);
    try {
      await deleteMe(password);
      // Account is gone; session cookie is cleared by the server. Hard refresh
      // to drop any in-memory state and land on the public landing page.
      window.location.href = "/";
    } catch (err) {
      if (err instanceof MeApiError && typeof err.fields.password === "string") {
        setError(err.fields.password);
      } else {
        setError(err instanceof Error ? err.message : "Delete failed");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <AuthedHeader email={email} />
      <SettingsLayout width="narrow">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            Account
          </h1>
        </div>

        <SettingsNav />

        <section className="rounded-xl border border-red-200 bg-red-50/40 p-6 dark:border-red-900 dark:bg-red-950/20">
          <h2 className="text-base font-semibold text-red-900 dark:text-red-200">
            Delete this account permanently
          </h2>
          <p className="mt-1 text-sm text-red-900/80 dark:text-red-200/80">
            This is immediate and cannot be undone. The email{" "}
            <strong>{email}</strong> will become available for re-registration.
          </p>

          <ul className="mt-4 space-y-1 text-sm text-red-900 dark:text-red-200">
            <li>
              You'll be removed from{" "}
              <strong>{preview.teams_member_count}</strong> team
              {preview.teams_member_count === 1 ? "" : "s"}.
            </li>
            {preview.teams_will_be_deleted > 0 && (
              <li>
                <strong>{preview.teams_will_be_deleted}</strong> team
                {preview.teams_will_be_deleted === 1 ? "" : "s"} where you're the
                only admin will be deleted.{" "}
                <strong>{preview.team_members_will_be_notified}</strong> teammate
                {preview.team_members_will_be_notified === 1 ? "" : "s"} will be
                notified.
              </li>
            )}
            <li>
              <strong>{preview.calendars_count}</strong> calendar subscription
              {preview.calendars_count === 1 ? "" : "s"} and{" "}
              <strong>{preview.cached_events_count}</strong> cached event
              {preview.cached_events_count === 1 ? "" : "s"} will be deleted.
            </li>
            <li>
              <strong>{preview.notifications_count}</strong> notification
              {preview.notifications_count === 1 ? "" : "s"} will be deleted.
            </li>
          </ul>

          <form onSubmit={onSubmit} className="mt-6 space-y-4 border-t border-red-200/70 pt-5 dark:border-red-900/70">
            <div className="space-y-1.5">
              <Label htmlFor="confirm-email">
                To confirm, type your email <strong>{email}</strong>
              </Label>
              <Input
                id="confirm-email"
                type="email"
                autoComplete="off"
                value={confirmEmail}
                onChange={(e) => setConfirmEmail(e.target.value)}
                placeholder={email}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Your current password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <FormError message={error} />
            <Button
              type="submit"
              disabled={submitting || !confirmsMatch || !password}
              className="bg-red-600 text-white hover:bg-red-700 disabled:bg-red-300 dark:bg-red-600 dark:hover:bg-red-700 sm:w-auto sm:px-6"
            >
              {submitting ? "Deleting…" : "Delete my account permanently"}
            </Button>
          </form>
        </section>
      </SettingsLayout>
    </div>
  );
}
