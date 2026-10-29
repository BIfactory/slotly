type Width = "narrow" | "standard" | "wide";
const WIDTH_CLASS: Record<Width, string> = {
  narrow: "max-w-2xl",     // forms, single column — profile, working-hours, notifications, account
  standard: "max-w-4xl",   // mixed form + list — meeting-types, locations, settings index
  wide: "max-w-5xl",       // data tables — api-tokens, calendars
};
export function SettingsLayout({ width = "standard", children }: { width?: Width; children: React.ReactNode }) {
  return (
    <main className={`mx-auto ${WIDTH_CLASS[width]} space-y-6 px-6 py-10`}>
      {children}
    </main>
  );
}
