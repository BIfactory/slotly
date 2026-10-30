/**
 * Public availability page — Server Component wrapper.
 *
 * Owns generateMetadata() so social-crawler bots (Slack, LinkedIn,
 * Twitter, Messenger, iMessage, Facebook, Teams) see a share preview
 * with the host's name + avatar + localized title/description. The
 * crawler fetches the HTML, reads <meta> + <title>, and renders a card
 * — it never executes the client JS, so metadata MUST live on the
 * server side of the route.
 *
 * All interactive UI lives in PublicProfileClient.tsx.
 */

import type { Metadata } from "next";
import { PublicProfileClient } from "./PublicProfileClient";

type Params = { token: string };
type SearchParams = { lang?: string };

const API =
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  // Fallback for local dev — rewrites in next.config.ts normally cover
  // /api/* in the browser, but a Server Component can't rely on them.
  "http://localhost:8000";

type ProfileSnapshot = {
  display_name: string;
  country: string;
  avatar_url: string | null;
};

async function fetchProfileSnapshot(token: string): Promise<ProfileSnapshot | null> {
  try {
    const res = await fetch(`${API}/api/public/profile/${token}`, {
      // Short cache so the OG preview doesn't go stale for days after
      // the host updates their avatar, but we don't hammer the API on
      // every bot that re-crawls.
      next: { revalidate: 300 },
      headers: { Accept: "application/json" },
    });
    if (!res.ok) return null;
    const data = await res.json();
    const p = data?.profile ?? data;
    return {
      display_name: p.display_name ?? "",
      country: p.country ?? "",
      avatar_url: p.avatar_url ?? null,
    };
  } catch {
    // Backend down → fall back to generic metadata rather than throw
    // and 500 the whole route for a visitor who could otherwise still
    // see the client-rendered page.
    return null;
  }
}

export async function generateMetadata({
  params,
  searchParams,
}: {
  params: Promise<Params>;
  searchParams: Promise<SearchParams>;
}): Promise<Metadata> {
  const { token } = await params;
  const sp = await searchParams;
  // Social crawlers don't send a `?lang=` param. The only signal we
  // reliably have on the server is the URL. Default to EN; CZ previews
  // only appear when the host explicitly shares a `?lang=cs` link.
  const locale: "cs" | "en" = sp?.lang === "cs" ? "cs" : "en";

  const snap = await fetchProfileSnapshot(token);

  if (!snap || !snap.display_name) {
    // Preserve the pre-split generic title so bots that hit a dead or
    // revoked share link don't show a broken card — just the app brand.
    return {
      title: "Slotly — find time to meet",
      description:
        locale === "cs"
          ? "Rezervujte si schůzku přes Slotly."
          : "Book a meeting via Slotly.",
    };
  }

  const title =
    locale === "cs"
      ? `Rezervace schůzky — ${snap.display_name}`
      : `Book a meeting — ${snap.display_name}`;
  // Keep the name out of the description — Czech "s/pro/u {name}" would
  // need case-inflected forms that we can't synthesize for arbitrary host
  // names. Title already carries the name; description stays action-first.
  const description =
    locale === "cs"
      ? "Vyberte volný termín v příštích 8 týdnech a potvrďte rezervaci schůzky."
      : "Pick a free slot in the next 8 weeks and confirm your meeting booking.";

  const images = snap.avatar_url ? [{ url: snap.avatar_url }] : undefined;

  return {
    title,
    description,
    openGraph: {
      title,
      description,
      type: "website",
      locale: locale === "cs" ? "cs_CZ" : "en_US",
      images,
    },
    twitter: {
      card: "summary",
      title,
      description,
      images: snap.avatar_url ? [snap.avatar_url] : undefined,
    },
    // Alternate language link so crawlers can discover the other locale.
    alternates: {
      languages: {
        cs: `?lang=cs`,
        en: `?lang=en`,
      },
    },
  };
}

export default function PublicProfilePage() {
  return <PublicProfileClient />;
}
