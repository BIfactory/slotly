"use client";

import { T, type Locale } from "@/lib/i18n";

export function LangSwitch({
  locale,
  onChange,
}: {
  locale: Locale;
  onChange: (l: Locale) => void;
}) {
  return (
    <div
      role="group"
      aria-label={T[locale].langSwitchAria}
      className="inline-flex overflow-hidden rounded-md border border-zinc-200 text-[11px] font-semibold uppercase tracking-wider dark:border-zinc-700"
    >
      <button
        type="button"
        onClick={() => onChange("cs")}
        aria-pressed={locale === "cs"}
        className={
          // 44x44 tap target on mobile per WCAG 2.5.5; desktop stays tight.
          "min-h-[44px] min-w-[44px] px-3 py-2 transition-colors sm:min-h-0 sm:min-w-0 sm:px-2 sm:py-1 " +
          (locale === "cs"
            ? "bg-indigo-600 text-white dark:bg-indigo-500"
            : "bg-white text-zinc-600 hover:bg-zinc-50 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800")
        }
      >
        CZ
      </button>
      <button
        type="button"
        onClick={() => onChange("en")}
        aria-pressed={locale === "en"}
        className={
          "min-h-[44px] min-w-[44px] border-l px-3 py-2 transition-colors sm:min-h-0 sm:min-w-0 sm:px-2 sm:py-1 " +
          (locale === "en"
            ? "border-indigo-600 bg-indigo-600 text-white dark:border-indigo-500 dark:bg-indigo-500"
            : "border-zinc-200 bg-white text-zinc-600 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800")
        }
      >
        EN
      </button>
    </div>
  );
}
