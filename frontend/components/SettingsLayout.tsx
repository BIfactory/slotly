type Width = "narrow" | "standard" | "wide";
// Width tiers aligned with the app's baseline container (max-w-3xl used by
// /bookings, /profile, /search, home). "standard" matches that baseline so
// settings pages don't feel conspicuously wider than the surrounding app.
const WIDTH_CLASS: Record<Width, string> = {
  narrow: "max-w-2xl",     // forms, single column — profile, working-hours, notifications, account
  standard: "max-w-3xl",   // mixed form + list — meeting-types, locations, settings index
  wide: "max-w-4xl",       // data tables — api-tokens, calendars
};
export function SettingsLayout({ width = "standard", children }: { width?: Width; children: React.ReactNode }) {
  return (
    <main className={`mx-auto ${WIDTH_CLASS[width]} space-y-6 px-6 py-10`}>
      {children}
    </main>
  );
}
