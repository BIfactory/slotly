"use client";

/**
 * Sticky tab strip rendered at the top of every /settings/* page so
 * users can jump between sections without scrolling. Sections that live
 * on the main /settings page (Profile, Sharing, Working hours) link to
 * in-page anchors; the rest navigate to dedicated sub-routes.
 *
 * Teams intentionally not listed here — it's a top-nav destination on
 * its own per the wider IA decision.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Bell,
  CalendarDays,
  Clock,
  KeyRound,
  Layers,
  MapPin,
  UserCircle,
  UserCog,
  type LucideIcon,
} from "lucide-react";

type SettingsTab = {
  /** Anchor or full path. Anchors begin with `#` and only fire on /settings. */
  href: string;
  label: string;
  icon: LucideIcon;
  /** Path prefix that should highlight this tab as active. */
  activePath?: string;
};

const TABS: SettingsTab[] = [
  { href: "/settings", label: "Profile", icon: UserCircle, activePath: "/settings" },
  { href: "/settings/working-hours", label: "Working hours", icon: Clock, activePath: "/settings/working-hours" },
  { href: "/settings/calendars", label: "Calendars", icon: CalendarDays, activePath: "/settings/calendars" },
  { href: "/settings/locations", label: "Locations", icon: MapPin, activePath: "/settings/locations" },
  { href: "/settings/meeting-types", label: "Meeting types", icon: Layers, activePath: "/settings/meeting-types" },
  { href: "/settings/notifications", label: "Notifications", icon: Bell, activePath: "/settings/notifications" },
  { href: "/settings/api-tokens", label: "API tokens", icon: KeyRound, activePath: "/settings/api-tokens" },
  { href: "/settings/account", label: "Account", icon: UserCog, activePath: "/settings/account" },
];

export function SettingsNav() {
  const pathname = usePathname() ?? "";
  return (
    <nav
      className="sticky z-10 -mx-6 mb-2 border-b border-zinc-200 bg-white px-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-950 sm:-mx-0 sm:rounded-lg sm:border sm:px-1 sm:dark:bg-zinc-900"
      style={{ top: "var(--header-h, 60px)" }}
    >
      <ul className="flex flex-wrap items-center gap-x-1 gap-y-0.5 py-1.5">
        {TABS.map((t) => {
          const Icon = t.icon;
          const active = t.activePath === pathname;
          return (
            <li key={t.href}>
              <Link
                href={t.href}
                className={
                  "inline-flex items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1.5 text-sm transition-colors " +
                  (active
                    ? "bg-indigo-50 font-medium text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-200"
                    : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800")
                }
              >
                <Icon size={14} aria-hidden="true" />
                <span>{t.label}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
