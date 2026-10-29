// One width across all /settings/* pages — same container as the rest of
// the app (/bookings, /profile, /search, home). The `width` prop is kept
// as a no-op for call-site compatibility.
type Width = "narrow" | "standard" | "wide";
export function SettingsLayout({ children }: { width?: Width; children: React.ReactNode }) {
  return (
    <main className="mx-auto max-w-3xl space-y-6 px-6 py-10">
      {children}
    </main>
  );
}
