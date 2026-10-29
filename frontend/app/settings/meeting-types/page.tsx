"use client";

/**
 * /settings/meeting-types — Calendly-style preset manager.
 *
 * Each row is a MeetingType the host has defined. Visitors on /u/<token>
 * see these as clickable cards; picking one locks the booking to the
 * type's duration, kind, and default location. Hosts with zero types
 * defined keep the generic "pick any free time" flow.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, MapPin, Pencil, PlusCircle, Trash2, Video, X as XIcon } from "lucide-react";
import { AuthedHeader } from "@/components/AuthedHeader";
import { SettingsNav } from "@/components/SettingsNav";
import { CardSkeleton, PageSkeleton } from "@/components/Skeleton";
import { Button, FormError, Input, Label } from "@/components/ui";
import { getSession } from "@/lib/auth";
import {
  createMeetingType,
  deleteMeetingType,
  listMeetingTypes,
  updateMeetingType,
  type MeetingLinkKind,
  type MeetingType,
  type MeetingTypeQuestion,
} from "@/lib/meeting-types";

const DURATIONS = [15, 30, 45, 60, 90, 120, 180, 240];
const PRESET_COLORS = ["#4f46e5", "#0ea5e9", "#059669", "#d97706", "#dc2626", "#7c3aed"];

export default function MeetingTypesPage() {
  const router = useRouter();
  const [email, setEmail] = useState<string>("");
  const [types, setTypes] = useState<MeetingType[] | null>(null);
  const [editing, setEditing] = useState<MeetingType | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      const session = await getSession();
      if (!session.meta?.is_authenticated) {
        router.replace("/auth/login?next=/settings/meeting-types");
        return;
      }
      setEmail(session.data?.user?.email ?? "");
      try {
        setTypes(await listMeetingTypes());
      } catch (err) {
        setError(err instanceof Error ? err.message : "Couldn't load meeting types");
      }
    })().catch(() => router.replace("/auth/login"));
  }, [router]);

  async function onDelete(t: MeetingType) {
    if (!confirm(`Delete "${t.name}"? This can't be undone.`)) return;
    try {
      await deleteMeetingType(t.id);
      setTypes((prev) => (prev ?? []).filter((x) => x.id !== t.id));
    } catch (err) {
      alert(err instanceof Error ? err.message : "Delete failed");
    }
  }

  async function onToggleActive(t: MeetingType) {
    try {
      const updated = await updateMeetingType(t.id, { is_active: !t.is_active });
      setTypes((prev) => (prev ?? []).map((x) => (x.id === t.id ? updated : x)));
    } catch (err) {
      alert(err instanceof Error ? err.message : "Update failed");
    }
  }

  if (!email || types === null) {
    return (
      <PageSkeleton>
        <CardSkeleton rows={3} />
        <CardSkeleton rows={5} className="mt-6" />
      </PageSkeleton>
    );
  }

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <AuthedHeader email={email} />
      <main className="mx-auto max-w-2xl space-y-6 px-6 py-10">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            Meeting types
          </h1>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            Define preset options for visitors: a &quot;15-min quick chat&quot; and
            a &quot;60-min deep dive&quot; each lock the booking to a specific
            duration. If you keep this list empty, your public link just
            lets visitors pick any free time.
          </p>
        </header>

        <SettingsNav />

        {error && <FormError message={error} />}

        <section className="flex justify-between gap-3">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            {types.length === 0
              ? "No types yet — add one to give visitors a choice."
              : `${types.length} type${types.length === 1 ? "" : "s"}, ${types.filter((t) => t.is_active).length} active`}
          </p>
          <Button
            onClick={() => setEditing("new")}
            className="w-auto px-3 py-1.5 text-sm"
          >
            <PlusCircle size={14} className="mr-1.5" />
            Add type
          </Button>
        </section>

        <ul className="space-y-3">
          {types.map((t) => (
            <li
              key={t.id}
              className={
                "rounded-xl border p-4 shadow-sm " +
                (t.is_active
                  ? "border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900"
                  : "border-zinc-100 bg-zinc-50 opacity-70 dark:border-zinc-800/60 dark:bg-zinc-900/40")
              }
            >
              <div className="flex items-start gap-3">
                <span
                  className="mt-1 h-3 w-3 shrink-0 rounded-full"
                  style={{ backgroundColor: t.color }}
                  aria-hidden
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-2">
                    <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
                      {t.name}
                    </h3>
                    <span className="text-xs text-zinc-500 dark:text-zinc-400">
                      {t.duration_min} min · {t.kind === "physical" ? "in person" : "online"}
                    </span>
                    {!t.is_active && (
                      <span className="rounded-full bg-zinc-200 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
                        Inactive
                      </span>
                    )}
                  </div>
                  {t.description && (
                    <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{t.description}</p>
                  )}
                  {t.location && (
                    <p className="mt-1 inline-flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
                      <MapPin size={12} aria-hidden /> {t.location}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 gap-1">
                  <button
                    type="button"
                    onClick={() => onToggleActive(t)}
                    title={t.is_active ? "Deactivate" : "Activate"}
                    className="inline-flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"
                  >
                    <CheckCircle2 size={14} />
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditing(t)}
                    title="Edit"
                    className="inline-flex h-8 w-8 items-center justify-center rounded-md text-zinc-500 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"
                  >
                    <Pencil size={14} />
                  </button>
                  <button
                    type="button"
                    onClick={() => onDelete(t)}
                    title="Delete"
                    className="inline-flex h-8 w-8 items-center justify-center rounded-md text-red-600 hover:bg-red-50 dark:text-red-300 dark:hover:bg-red-950/30"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            </li>
          ))}
        </ul>

        {editing !== null && (
          <EditorModal
            initial={editing === "new" ? null : editing}
            onClose={() => setEditing(null)}
            onSaved={(saved, isNew) => {
              setEditing(null);
              if (isNew) {
                setTypes((prev) => [...(prev ?? []), saved]);
              } else {
                setTypes((prev) => (prev ?? []).map((x) => (x.id === saved.id ? saved : x)));
              }
            }}
          />
        )}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------

function EditorModal({
  initial,
  onClose,
  onSaved,
}: {
  initial: MeetingType | null;
  onClose: () => void;
  onSaved: (saved: MeetingType, isNew: boolean) => void;
}) {
  const isNew = initial === null;
  const [name, setName] = useState(initial?.name ?? "");
  const [duration, setDuration] = useState(initial?.duration_min ?? 30);
  const [kind, setKind] = useState<"online" | "physical">(initial?.kind ?? "online");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [location, setLocation] = useState(initial?.location ?? "");
  const [color, setColor] = useState(initial?.color ?? PRESET_COLORS[0]);
  const [questions, setQuestions] = useState<MeetingTypeQuestion[]>(initial?.questions ?? []);
  const [redirectUrl, setRedirectUrl] = useState(initial?.redirect_url ?? "");
  const [meetingLinkKind, setMeetingLinkKind] = useState<MeetingLinkKind>(initial?.meeting_link_kind ?? "auto");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function addQuestion() {
    setQuestions((prev) => [
      ...prev,
      {
        id: crypto.randomUUID().replaceAll("-", ""),
        label: "",
        kind: "text",
        required: false,
      },
    ]);
  }
  function updateQuestion(idx: number, patch: Partial<MeetingTypeQuestion>) {
    setQuestions((prev) => prev.map((q, i) => (i === idx ? { ...q, ...patch } : q)));
  }
  function removeQuestion(idx: number) {
    setQuestions((prev) => prev.filter((_, i) => i !== idx));
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const payload = {
        name: name.trim(),
        duration_min: duration,
        kind,
        description: description.trim(),
        location: kind === "physical" ? location.trim() : "",
        color,
        questions: questions
          .map((q) => ({ ...q, label: q.label.trim() }))
          .filter((q) => q.label.length > 0),
        redirect_url: redirectUrl.trim(),
        meeting_link_kind: meetingLinkKind,
      };
      const saved = isNew
        ? await createMeetingType(payload)
        : await updateMeetingType(initial!.id, payload);
      onSaved(saved, isNew);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose();
      }}
      role="dialog"
      aria-modal
    >
      <form
        onSubmit={onSubmit}
        className="w-full max-w-md space-y-4 rounded-xl border border-zinc-200 bg-white p-5 shadow-2xl dark:border-zinc-800 dark:bg-zinc-900"
      >
        <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
          {isNew ? "New meeting type" : "Edit meeting type"}
        </h2>
        <div className="space-y-1">
          <Label htmlFor="mt-name">Name</Label>
          <Input
            id="mt-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="15-min quick chat"
            required
            maxLength={80}
            autoFocus
          />
        </div>

        <fieldset className="space-y-1">
          <span className="text-sm font-medium text-zinc-700 dark:text-zinc-200">Type</span>
          <div className="grid grid-cols-2 gap-2">
            <KindOption
              active={kind === "online"}
              onClick={() => setKind("online")}
              icon={<Video size={14} aria-hidden />}
              label="Online"
            />
            <KindOption
              active={kind === "physical"}
              onClick={() => setKind("physical")}
              icon={<MapPin size={14} aria-hidden />}
              label="In person"
            />
          </div>
        </fieldset>

        {kind === "physical" && (
          <div className="space-y-1">
            <Label htmlFor="mt-location">Default location</Label>
            <Input
              id="mt-location"
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              placeholder="Café at Slavín, Petřín, Prague…"
              maxLength={300}
            />
            <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
              Visitors can override this when booking if the exact spot varies.
            </p>
          </div>
        )}

        <div className="space-y-1">
          <Label htmlFor="mt-duration">Duration</Label>
          <div id="mt-duration" className="flex flex-wrap gap-1.5">
            {DURATIONS.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setDuration(d)}
                aria-pressed={d === duration}
                className={
                  "rounded-full border px-3 py-1 text-sm " +
                  (d === duration
                    ? "border-indigo-600 bg-indigo-600 text-white"
                    : "border-zinc-300 bg-white text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200")
                }
              >
                {d < 60 ? `${d}m` : d % 60 === 0 ? `${d / 60}h` : `${Math.floor(d / 60)}h${d % 60}m`}
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-1">
          <Label htmlFor="mt-description">Description (optional)</Label>
          <textarea
            id="mt-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            maxLength={1000}
            placeholder="What is this meeting for? Shown on the booking page."
            className="w-full resize-y rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-950"
          />
        </div>

        <div className="space-y-1">
          <span className="text-sm font-medium text-zinc-700 dark:text-zinc-200">Color</span>
          <div className="flex gap-2">
            {PRESET_COLORS.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => setColor(c)}
                aria-label={`Pick colour ${c}`}
                aria-pressed={c === color}
                className={
                  "h-7 w-7 rounded-full ring-offset-2 dark:ring-offset-zinc-900 " +
                  (c === color ? "ring-2 ring-zinc-900 dark:ring-zinc-100" : "")
                }
                style={{ backgroundColor: c }}
              />
            ))}
          </div>
        </div>

        <div className="space-y-1">
          <Label htmlFor="mt-redirect">Redirect after booking (optional)</Label>
          <Input
            id="mt-redirect"
            type="url"
            value={redirectUrl}
            onChange={(e) => setRedirectUrl(e.target.value)}
            placeholder="https://yourdomain.com/thanks"
            maxLength={2000}
          />
          <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
            When set, the visitor is sent here after booking instead of
            seeing the built-in confirmation — useful for a &quot;thank
            you&quot; page or a conversion pixel.
          </p>
        </div>

        <div className="space-y-1">
          <Label htmlFor="mt-meeting-link-kind">Meeting link</Label>
          <select
            id="mt-meeting-link-kind"
            value={meetingLinkKind}
            onChange={(e) => setMeetingLinkKind(e.target.value as MeetingLinkKind)}
            className="w-full rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-950"
          >
            <option value="auto">Auto (default)</option>
            <option value="google_meet">Google Meet</option>
            <option value="teams">Microsoft Teams</option>
            <option value="none">No video link</option>
          </select>
          <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
            Which video-link type to attach to bookings using this meeting
            type. Visitors can still override this when booking.
          </p>
        </div>

        {/* Custom questions the visitor must fill in when they pick this
            type. Optional — skip the section if you don't need it. */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-zinc-700 dark:text-zinc-200">
              Questions
            </span>
            <button
              type="button"
              onClick={addQuestion}
              className="text-xs font-medium text-indigo-600 hover:text-indigo-700 dark:text-indigo-400"
            >
              + Add question
            </button>
          </div>
          {questions.length === 0 ? (
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              No questions. Add one to ask visitors for context before they book
              (e.g. &quot;What&apos;s your company?&quot;, &quot;What do you want to
              discuss?&quot;).
            </p>
          ) : (
            <ul className="space-y-2">
              {questions.map((q, idx) => (
                <li
                  key={q.id}
                  className="rounded-md border border-zinc-200 bg-zinc-50 p-2 dark:border-zinc-800 dark:bg-zinc-950/40"
                >
                  <div className="flex items-start gap-2">
                    <Input
                      value={q.label}
                      onChange={(e) => updateQuestion(idx, { label: e.target.value })}
                      placeholder="Question text"
                      className="!h-8 !text-xs"
                      maxLength={120}
                    />
                    <select
                      value={q.kind}
                      onChange={(e) =>
                        updateQuestion(idx, { kind: e.target.value as MeetingTypeQuestion["kind"] })
                      }
                      className="h-8 rounded-md border border-zinc-200 bg-white px-2 text-xs dark:border-zinc-800 dark:bg-zinc-950"
                    >
                      <option value="text">Text</option>
                      <option value="textarea">Long text</option>
                      <option value="select">Select</option>
                    </select>
                    <button
                      type="button"
                      onClick={() => removeQuestion(idx)}
                      className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-red-600 hover:bg-red-50 dark:text-red-300 dark:hover:bg-red-950/30"
                      title="Remove"
                    >
                      <XIcon size={14} />
                    </button>
                  </div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-3 pl-1">
                    <label className="flex items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400">
                      <input
                        type="checkbox"
                        checked={q.required}
                        onChange={(e) => updateQuestion(idx, { required: e.target.checked })}
                      />
                      Required
                    </label>
                    {q.kind === "select" && (
                      <Input
                        value={(q.options ?? []).join(", ")}
                        onChange={(e) =>
                          updateQuestion(idx, {
                            options: e.target.value.split(",").map((o) => o.trim()).filter(Boolean),
                          })
                        }
                        placeholder="Options separated by commas"
                        className="!h-7 !text-[11px]"
                      />
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {error && <FormError message={error} />}

        <div className="flex justify-end gap-2 pt-1">
          <Button
            type="button"
            variant="secondary"
            onClick={onClose}
            disabled={submitting}
            className="w-auto px-4"
          >
            Cancel
          </Button>
          <Button type="submit" disabled={submitting} className="w-auto px-5">
            {submitting ? "Saving…" : isNew ? "Create" : "Save"}
          </Button>
        </div>
      </form>
    </div>
  );
}

function KindOption({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        "inline-flex items-center justify-center gap-1.5 rounded-lg border py-2 text-sm font-medium transition-colors " +
        (active
          ? "border-indigo-500 bg-indigo-50 text-indigo-800 dark:border-indigo-400 dark:bg-indigo-950/40 dark:text-indigo-100"
          : "border-zinc-200 bg-white text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200")
      }
    >
      {icon}
      {label}
    </button>
  );
}
