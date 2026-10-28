import Link from "next/link";
import { Logo } from "@/components/Logo";

export const metadata = {
  title: "Privacy Policy — Slotly",
  description:
    "How Slotly collects, uses, and protects your personal data and calendar information.",
};

export default function PrivacyPage() {
  return (
    <div className="min-h-screen bg-white text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <header className="border-b border-zinc-200 dark:border-zinc-800">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-6 py-3">
          <Link href="/" aria-label="Slotly home">
            <Logo size={22} />
          </Link>
          <Link href="/terms" className="text-sm text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100">
            Terms
          </Link>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 py-10">
        <h1 className="text-3xl font-semibold tracking-tight">Privacy Policy</h1>
        <p className="mt-2 text-sm text-zinc-500 dark:text-zinc-400">
          Effective 1 September 2026 · Last updated 1 September 2026
        </p>

        <div className="prose prose-zinc mt-8 max-w-none space-y-6 text-[15px] leading-relaxed dark:prose-invert">
          <section>
            <h2 className="text-xl font-semibold">Who we are</h2>
            <p>
              Slotly is a scheduling service operated by <strong>bifactory s.r.o.</strong>,
              Czech Republic. The data controller for personal information processed
              by Slotly is bifactory s.r.o. You can reach us at{" "}
              <a className="underline" href="mailto:hulin@bifactory.cz">
                hulin@bifactory.cz
              </a>
              .
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Data we collect</h2>
            <p>When you use Slotly we collect only what we need to run the service:</p>
            <ul className="ml-5 list-disc space-y-1">
              <li>
                <strong>Account details</strong> — email address, first and last name,
                optional avatar and phone number. Provided by you at sign-up (or by
                Google / Microsoft if you sign in via SSO).
              </li>
              <li>
                <strong>Working hours &amp; unavailability</strong> — the weekly hours
                you are open for meetings and any "out of office" blocks you add.
              </li>
              <li>
                <strong>Calendar free/busy data</strong> — busy intervals from calendars
                you connect (Google Calendar via OAuth, or ICS feed URLs you paste).
                We do <em>not</em> store event titles, attendees or bodies from your
                calendars; we only store the start/end times and whether the block is
                busy or free.
              </li>
              <li>
                <strong>Booking metadata</strong> — meetings you create through Slotly
                or that someone books via your public link. We store the participants'
                emails, meeting time, and any title/notes you or the visitor provided.
              </li>
              <li>
                <strong>OAuth tokens</strong> — when you connect Google or Microsoft,
                we store access and refresh tokens so we can create events on your
                behalf. Tokens are encrypted at rest with a per-deployment key.
              </li>
              <li>
                <strong>Basic technical logs</strong> — IP address, user-agent, and
                timestamps for security and rate-limiting. Kept for at most 30 days.
              </li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold">How we use Google user data</h2>
            <p>
              Slotly&apos;s use of information received from Google APIs adheres to the{" "}
              <a
                className="underline"
                href="https://developers.google.com/terms/api-services-user-data-policy"
                target="_blank"
                rel="noreferrer"
              >
                Google API Services User Data Policy
              </a>
              , including the Limited Use requirements.
            </p>
            <p>Specifically, Google data is used only to:</p>
            <ul className="ml-5 list-disc space-y-1">
              <li>Read free/busy times from your Google Calendar to compute your availability.</li>
              <li>List the calendars on your Google account so you can pick which one Slotly writes new events into. We read only the calendar list, never event contents, through this scope.</li>
              <li>Create events in the calendar you selected as the write target, when you or a visitor books a meeting.</li>
              <li>Display your name, email, and avatar (from Google userinfo) in your Slotly profile.</li>
            </ul>
            <p>
              We do <strong>not</strong> sell, share, or use Google user data for
              advertising. We do not use it to train AI/ML models. Human review of
              this data only happens when strictly necessary to investigate abuse,
              debug a specific customer-reported bug you asked us to look into, or
              comply with law.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Sharing</h2>
            <p>
              We don&apos;t sell your data. We share it only with the third-party
              providers we need to run the service:
            </p>
            <ul className="ml-5 list-disc space-y-1">
              <li>
                <strong>Google</strong> and <strong>Microsoft</strong> — for the OAuth
                connections you initiate and the calendar events you or your visitors
                create through Slotly.
              </li>
              <li>
                <strong>Microsoft Azure</strong> (EU region) — our infrastructure
                provider (compute, database, blob storage, email).
              </li>
            </ul>
            <p>
              When a visitor books time via your public link, the visitor&apos;s email
              and name are shared with your calendar provider so they can be added as
              an event attendee. That is the only outward sharing of visitor data.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Public booking link</h2>
            <p>
              When you enable your public booking link, anyone with the URL can see
              your free/busy availability for the next 8 weeks and, if you have a
              calendar connected, book time with you. They see your display name,
              country, and avatar; they never see your email or the titles of
              events on your calendar. You can turn the link off or rotate it at any
              time from your profile page.
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Retention</h2>
            <ul className="ml-5 list-disc space-y-1">
              <li>Account data — for as long as your account exists.</li>
              <li>OAuth tokens — until you disconnect the integration or delete your account.</li>
              <li>Cached calendar events — up to 90 days after they end; older entries are purged automatically.</li>
              <li>Booking requests — indefinitely, so you have a record; you can delete individual rows on request.</li>
              <li>Technical logs — up to 30 days.</li>
            </ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Your rights (GDPR)</h2>
            <p>
              You can access, correct, export, or delete your personal data at any time
              from your account settings. Full account deletion (Settings → Account →
              Delete account) removes all your data within 24 hours, except backups
              that expire on their own rolling schedule (max 30 days). You can also
              contact <a className="underline" href="mailto:hulin@bifactory.cz">
                hulin@bifactory.cz
              </a>{" "}
              to exercise any GDPR right, including lodging a complaint with the Czech
              data-protection authority (ÚOOÚ).
            </p>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Changes</h2>
            <p>
              We&apos;ll update this policy when we materially change how Slotly
              handles data. When we do, we&apos;ll email active users and post the new
              effective date at the top of this page.
            </p>
          </section>
        </div>

        <footer className="mt-10 border-t border-zinc-200 pt-6 text-xs text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
          <p>
            Questions? Email{" "}
            <a className="underline" href="mailto:hulin@bifactory.cz">
              hulin@bifactory.cz
            </a>
            .
          </p>
        </footer>
      </main>
    </div>
  );
}
