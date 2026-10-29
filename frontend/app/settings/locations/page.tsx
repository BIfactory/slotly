"use client";

/**
 * /settings/locations — manage the user's in-person location library.
 *
 * Add / edit (inline) / delete / set-default. Max 20 locations; client
 * disables the Add button when the cap is reached.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { MapPin, Pencil, Star, Trash2, X as XIcon } from "lucide-react";
import { AuthedHeader } from "@/components/AuthedHeader";
import { SettingsLayout } from "@/components/SettingsLayout";
import { SettingsNav } from "@/components/SettingsNav";
import { CardSkeleton, PageSkeleton } from "@/components/Skeleton";
import { Button, FormError, Input, Label } from "@/components/ui";
import { getSession } from "@/lib/auth";
import {
  createLocation,
  deleteLocation,
  listLocations,
  LocationApiError,
  setDefaultLocation,
  updateLocation,
  type UserLocation,
} from "@/lib/locations";

const MAX_LOCATIONS = 20;

export default function LocationsPage() {
  const router = useRouter();
  const [email, setEmail] = useState<string>("");
  const [locations, setLocations] = useState<UserLocation[] | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);

  // Which row is in inline-edit mode (id) or "new" for the Add form.
  const [editingId, setEditingId] = useState<number | "new" | null>(null);

  useEffect(() => {
    (async () => {
      const session = await getSession();
      if (!session.meta?.is_authenticated) {
        router.replace("/auth/login?next=/settings/locations");
        return;
      }
      setEmail(session.data?.user?.email ?? "");
      try {
        setLocations(await listLocations());
      } catch (err) {
        setPageError(
          err instanceof Error ? err.message : "Couldn't load locations",
        );
        setLocations([]);
      }
    })().catch(() => router.replace("/auth/login"));
  }, [router]);

  async function handleDelete(loc: UserLocation) {
    if (!confirm(`Delete "${loc.name}"? This can't be undone.`)) return;
    try {
      await deleteLocation(loc.id);
      setLocations((prev) => (prev ?? []).filter((l) => l.id !== loc.id));
      if (editingId === loc.id) setEditingId(null);
    } catch (err) {
      setPageError(
        err instanceof Error ? err.message : "Delete failed",
      );
    }
  }

  async function handleSetDefault(id: number) {
    // Optimistic update.
    const snapshot = locations;
    setLocations((prev) =>
      (prev ?? []).map((l) => ({ ...l, is_default: l.id === id })),
    );
    try {
      const updated = await setDefaultLocation(id);
      // Reconcile using the server's canonical truth.
      setLocations((prev) =>
        (prev ?? []).map((l) => (l.id === updated.id ? updated : { ...l, is_default: false })),
      );
    } catch (err) {
      setLocations(snapshot);
      setPageError(
        err instanceof Error ? err.message : "Couldn't set default",
      );
    }
  }

  function handleSaved(saved: UserLocation, isNew: boolean) {
    setEditingId(null);
    if (isNew) {
      setLocations((prev) => {
        const without = (prev ?? []).filter((l) => l.id !== saved.id);
        return [...without, saved];
      });
    } else {
      setLocations((prev) =>
        (prev ?? []).map((l) => (l.id === saved.id ? saved : l)),
      );
    }
  }

  const atCap = (locations ?? []).length >= MAX_LOCATIONS;

  if (!email || locations === null) {
    return (
      <PageSkeleton>
        <CardSkeleton rows={3} />
      </PageSkeleton>
    );
  }

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <AuthedHeader email={email} />

      <SettingsLayout width="standard">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            Locations
          </h1>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            Add places you meet people in person. Visitors booking you will see
            these as options when they pick an in-person meeting.
          </p>
        </header>

        <SettingsNav />

        {pageError && (
          <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm leading-relaxed text-amber-900 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-amber-100">
            <p>{pageError}</p>
          </div>
        )}

        {/* Add button + cap help */}
        <div className="flex items-center justify-between gap-3">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            {locations.length === 0
              ? "No locations yet — add your first below."
              : `${locations.length} location${locations.length === 1 ? "" : "s"}${atCap ? " (limit reached)" : ""}`}
          </p>
          <Button
            onClick={() => setEditingId("new")}
            disabled={atCap || editingId === "new"}
            className="w-auto px-3 py-1.5 text-sm"
          >
            <MapPin size={14} className="mr-1.5" aria-hidden />
            Add location
          </Button>
        </div>
        {atCap && (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            You&apos;ve reached the 20-location limit. Delete one before adding
            another.
          </p>
        )}

        {/* Inline Add form */}
        {editingId === "new" && (
          <LocationForm
            initial={null}
            onSaved={(saved) => handleSaved(saved, true)}
            onCancel={() => setEditingId(null)}
          />
        )}

        {/* Location list */}
        {locations.length === 0 && editingId !== "new" ? (
          <section className="rounded-xl border border-dashed border-zinc-300 bg-white p-8 text-center dark:border-zinc-700 dark:bg-zinc-900">
            <MapPin
              size={24}
              className="mx-auto mb-2 text-zinc-400 dark:text-zinc-500"
              aria-hidden
            />
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              You haven&apos;t added any locations yet. Add your first above.
            </p>
          </section>
        ) : (
          <ul className="space-y-3">
            {locations.map((loc) =>
              editingId === loc.id ? (
                <li key={loc.id}>
                  <LocationForm
                    initial={loc}
                    onSaved={(saved) => handleSaved(saved, false)}
                    onCancel={() => setEditingId(null)}
                  />
                </li>
              ) : (
                <LocationRow
                  key={loc.id}
                  location={loc}
                  onEdit={() => setEditingId(loc.id)}
                  onDelete={() => handleDelete(loc)}
                  onSetDefault={() => handleSetDefault(loc.id)}
                  editDisabled={editingId !== null}
                />
              ),
            )}
          </ul>
        )}
      </SettingsLayout>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row: read-only display with Edit / Delete / Set-default actions
// ---------------------------------------------------------------------------

function LocationRow({
  location,
  onEdit,
  onDelete,
  onSetDefault,
  editDisabled,
}: {
  location: UserLocation;
  onEdit: () => void;
  onDelete: () => void;
  onSetDefault: () => void;
  editDisabled: boolean;
}) {
  return (
    <li className="rounded-xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-start gap-3">
        {/* Default radio */}
        <button
          type="button"
          onClick={onSetDefault}
          disabled={location.is_default}
          title={location.is_default ? "Default location" : "Set as default"}
          aria-label={
            location.is_default
              ? `${location.name} is the default location`
              : `Set ${location.name} as default`
          }
          className={
            "mt-0.5 h-4 w-4 shrink-0 rounded-full border-2 transition-colors " +
            (location.is_default
              ? "border-indigo-600 bg-indigo-600 dark:border-indigo-400 dark:bg-indigo-400"
              : "border-zinc-300 bg-white hover:border-indigo-400 dark:border-zinc-600 dark:bg-zinc-950")
          }
        />

        {/* Name + address */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <p className="font-medium text-zinc-900 dark:text-zinc-50">
              {location.name}
            </p>
            {location.is_default && (
              <span className="inline-flex items-center gap-1 rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300">
                <Star size={10} aria-hidden />
                Default
              </span>
            )}
          </div>
          {/* address is required at create time; this guard is defensive only */}
          {location.address && (
            <p className="mt-0.5 text-sm text-zinc-500 dark:text-zinc-400">
              {location.address}
            </p>
          )}
        </div>

        {/* Actions */}
        <div className="flex shrink-0 gap-1">
          <button
            type="button"
            onClick={onEdit}
            disabled={editDisabled}
            title="Edit"
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-100 disabled:opacity-40 dark:text-zinc-400 dark:hover:bg-zinc-800"
          >
            <Pencil size={14} aria-hidden />
          </button>
          <button
            type="button"
            onClick={onDelete}
            title="Delete"
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-red-600 hover:bg-red-50 dark:text-red-300 dark:hover:bg-red-950/30"
          >
            <Trash2 size={14} aria-hidden />
          </button>
        </div>
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Inline form: add or edit a single location
// ---------------------------------------------------------------------------

function LocationForm({
  initial,
  onSaved,
  onCancel,
}: {
  initial: UserLocation | null;
  onSaved: (saved: UserLocation) => void;
  onCancel: () => void;
}) {
  const isNew = initial === null;
  const [name, setName] = useState(initial?.name ?? "");
  const [address, setAddress] = useState(initial?.address ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);
  const [addressError, setAddressError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setNameError(null);
    setAddressError(null);
    setFormError(null);

    // Client-side required validation — don't round-trip for empty fields.
    let hasClientError = false;
    if (!name.trim()) {
      setNameError("Name is required.");
      hasClientError = true;
    }
    if (!address.trim()) {
      setAddressError("Address is required.");
      hasClientError = true;
    }
    if (hasClientError) return;

    setSubmitting(true);
    try {
      const saved = isNew
        ? await createLocation({ name: name.trim(), address: address.trim() })
        : await updateLocation(initial!.id, {
            name: name.trim(),
            address: address.trim(),
          });
      onSaved(saved);
    } catch (err) {
      if (err instanceof LocationApiError) {
        const fields = err.fields;
        const nameMsg = Array.isArray(fields.name)
          ? String(fields.name[0])
          : typeof fields.name === "string"
            ? fields.name
            : null;
        const addrMsg = Array.isArray(fields.address)
          ? String(fields.address[0])
          : typeof fields.address === "string"
            ? fields.address
            : null;
        if (nameMsg) setNameError(nameMsg);
        if (addrMsg) setAddressError(addrMsg);
        if (!nameMsg && !addrMsg) setFormError(err.message);
      } else {
        setFormError(err instanceof Error ? err.message : "Save failed");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="rounded-xl border border-indigo-200 bg-white p-4 shadow-sm dark:border-indigo-900/60 dark:bg-zinc-900">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-50">
          {isNew ? "New location" : "Edit location"}
        </h3>
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="inline-flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-100 disabled:opacity-40 dark:text-zinc-400 dark:hover:bg-zinc-800"
          title="Cancel"
        >
          <XIcon size={14} aria-hidden />
        </button>
      </div>
      <form onSubmit={onSubmit} className="space-y-3">
        <div className="space-y-1">
          <Label htmlFor={`loc-name-${isNew ? "new" : initial?.id}`}>Name</Label>
          <Input
            id={`loc-name-${isNew ? "new" : initial?.id}`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Office, Café Slavín, Prague HQ…"
            maxLength={150}
            autoFocus
          />
          {nameError && <FormError message={nameError} />}
        </div>
        <div className="space-y-1">
          <Label htmlFor={`loc-address-${isNew ? "new" : initial?.id}`}>
            Address
          </Label>
          <Input
            id={`loc-address-${isNew ? "new" : initial?.id}`}
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Wenceslas Square 1, Prague 1, 110 00"
            maxLength={500}
          />
          {addressError && <FormError message={addressError} />}
        </div>
        {formError && <FormError message={formError} />}
        <div className="flex justify-end gap-2 pt-1">
          <Button
            type="button"
            variant="secondary"
            onClick={onCancel}
            disabled={submitting}
            className="w-auto px-4"
          >
            Cancel
          </Button>
          <Button type="submit" disabled={submitting} className="w-auto px-5">
            {submitting ? "Saving…" : isNew ? "Add" : "Save"}
          </Button>
        </div>
      </form>
    </div>
  );
}
