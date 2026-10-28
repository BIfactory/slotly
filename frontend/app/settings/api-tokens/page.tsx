"use client";

import { Suspense, useCallback, useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Copy, KeyRound, X } from "lucide-react";
import { AuthedHeader } from "@/components/AuthedHeader";
import { SettingsNav } from "@/components/SettingsNav";
import { CardSkeleton, ListSkeleton, PageSkeleton } from "@/components/Skeleton";
import { Button, FormError, Input, Label } from "@/components/ui";
import { getSession } from "@/lib/auth";
import {
  type McpToken,
  type McpTokenCall,
  type McpTokenCreated,
  type McpTokenScope,
  type McpTokenScopeChoice,
  MCP_SCOPE_CHOICES_FALLBACK,
  McpTokenApiError,
  createMcpToken,
  listMcpTokenCalls,
  listMcpTokenScopes,
  listMcpTokens,
  revokeMcpToken,
} from "@/lib/mcp-tokens";

// ---------------------------------------------------------------------------
// Page shell
// ---------------------------------------------------------------------------

export default function ApiTokensPage() {
  return (
    <Suspense
      fallback={
        <PageSkeleton>
          <CardSkeleton rows={4} />
          <ListSkeleton rows={3} className="mt-6" />
        </PageSkeleton>
      }
    >
      <ApiTokensPageInner />
    </Suspense>
  );
}

function ApiTokensPageInner() {
  const router = useRouter();
  const [loaded, setLoaded] = useState(false);
  const [email, setEmail] = useState<string>("");
  const [tokens, setTokens] = useState<McpToken[]>([]);
  const [scopeChoices, setScopeChoices] = useState<McpTokenScopeChoice[]>(MCP_SCOPE_CHOICES_FALLBACK);
  const [justCreated, setJustCreated] = useState<McpTokenCreated | null>(null);
  const [callsModal, setCallsModal] = useState<{
    token: McpToken;
    calls: McpTokenCall[] | null;
  } | null>(null);

  useEffect(() => {
    (async () => {
      const session = await getSession();
      if (!session.meta?.is_authenticated) {
        router.replace("/auth/login");
        return;
      }
      setEmail(session.data?.user?.email ?? "");
      const [tkns, choices] = await Promise.all([
        listMcpTokens(),
        listMcpTokenScopes().catch((err) => {
          console.error("[mcp-tokens] Failed to load scope choices, using fallback:", err);
          return MCP_SCOPE_CHOICES_FALLBACK;
        }),
      ]);
      setTokens(tkns);
      setScopeChoices(choices);
      setLoaded(true);
    })().catch(() => router.replace("/auth/login"));
  }, [router]);

  const onTokenCreated = useCallback((created: McpTokenCreated) => {
    setJustCreated(created);
    setTokens((prev) => [created, ...prev.filter((t) => t.id !== created.id)]);
  }, []);

  const onRevoke = useCallback(async (token: McpToken) => {
    if (!confirm(`Revoke token "${token.name}"? Any integrations using it will stop working.`)) return;
    try {
      await revokeMcpToken(token.id);
      setTokens((prev) =>
        prev.map((t) =>
          t.id === token.id
            ? { ...t, is_active: false, revoked_at: new Date().toISOString() }
            : t,
        ),
      );
    } catch (err) {
      alert(err instanceof Error ? err.message : "Revoke failed");
    }
  }, []);

  const onViewCalls = useCallback(async (token: McpToken) => {
    setCallsModal({ token, calls: null });
    try {
      const calls = await listMcpTokenCalls(token.id);
      setCallsModal({ token, calls });
    } catch {
      setCallsModal(null);
    }
  }, []);

  if (!loaded) {
    return (
      <PageSkeleton>
        <CardSkeleton rows={4} />
        <ListSkeleton rows={3} className="mt-6" />
      </PageSkeleton>
    );
  }

  return (
    <div className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <AuthedHeader email={email} />

      <main className="mx-auto max-w-2xl space-y-6 px-6 py-10">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            API tokens
          </h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Personal access tokens let external tools call the Slotly MCP API on
            your behalf. Each token has a fixed set of permissions.
          </p>
        </div>

        <SettingsNav />

        {/* Raw token shown once after creation */}
        {justCreated && (
          <RawTokenBanner raw={justCreated.token} onDismiss={() => setJustCreated(null)} />
        )}

        {/* Create form */}
        <CreateTokenForm onCreated={onTokenCreated} scopeChoices={scopeChoices} />

        {/* Token table */}
        <section className="space-y-3">
          <header>
            <h2 className="text-lg font-semibold text-zinc-900 dark:text-zinc-50">
              Your tokens
            </h2>
            <p className="mt-0.5 text-sm text-zinc-600 dark:text-zinc-400">
              Active and revoked tokens. Revoked tokens cannot be re-activated.
            </p>
          </header>
          <TokenTable tokens={tokens} onRevoke={onRevoke} onViewCalls={onViewCalls} />
        </section>
      </main>

      {/* Calls modal */}
      {callsModal && (
        <CallsModal
          token={callsModal.token}
          calls={callsModal.calls}
          onClose={() => setCallsModal(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Raw token banner
// ---------------------------------------------------------------------------

function RawTokenBanner({ raw, onDismiss }: { raw: string; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false);

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  }

  return (
    <div className="rounded-xl border border-red-300 bg-red-50 p-5 dark:border-red-800/60 dark:bg-red-950/20">
      <div className="mb-3 flex items-start gap-2">
        <AlertTriangle
          size={18}
          className="mt-0.5 shrink-0 text-red-600 dark:text-red-400"
          aria-hidden
        />
        <div className="flex-1">
          <p className="text-sm font-semibold text-red-700 dark:text-red-300">
            Save this token now. It will not be shown again.
          </p>
          <p className="mt-0.5 text-xs text-red-600 dark:text-red-400">
            Copy it to a safe location before leaving this page.
          </p>
        </div>
        <button
          onClick={onDismiss}
          className="rounded p-0.5 text-red-500 hover:bg-red-100 dark:text-red-400 dark:hover:bg-red-950/40"
          aria-label="Dismiss"
        >
          <X size={16} />
        </button>
      </div>
      <div className="flex gap-2">
        <pre className="flex-1 overflow-x-auto rounded-md border border-red-200 bg-white px-3 py-2 font-mono text-xs text-zinc-800 dark:border-red-800/40 dark:bg-zinc-900 dark:text-zinc-100">
          {raw}
        </pre>
        <button
          onClick={onCopy}
          className="inline-flex items-center gap-1.5 rounded-md border border-red-300 bg-white px-3 text-sm font-medium text-red-700 hover:bg-red-50 dark:border-red-700 dark:bg-zinc-900 dark:text-red-300 dark:hover:bg-red-950/30"
        >
          <Copy size={14} aria-hidden />
          {copied ? "Copied!" : "Copy"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create token form
// ---------------------------------------------------------------------------

function CreateTokenForm({
  onCreated,
  scopeChoices,
}: {
  onCreated: (t: McpTokenCreated) => void;
  scopeChoices: McpTokenScopeChoice[];
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<Set<McpTokenScope>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const readScopes = scopeChoices.filter((c) => c.group === "read");
  const writeScopes = scopeChoices.filter((c) => c.group === "write");
  const hasWriteScope = writeScopes.some((c) => scopes.has(c.name));

  function toggleScope(scope: McpTokenScope) {
    setScopes((prev) => {
      const next = new Set(prev);
      if (next.has(scope)) next.delete(scope);
      else next.add(scope);
      return next;
    });
  }

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(null);
    if (scopes.size === 0) {
      setError("Select at least one permission.");
      return;
    }
    if (hasWriteScope) {
      const ok = window.confirm(
        "This token will have write access and can create or modify calendar events without further confirmation. Only grant this to clients you fully trust. Continue?",
      );
      if (!ok) return;
    }
    setSubmitting(true);
    try {
      const created = await createMcpToken({ name, scopes: [...scopes] });
      onCreated(created);
      setName("");
      setScopes(new Set());
    } catch (err) {
      if (err instanceof McpTokenApiError) {
        const fields = err.fields;
        const first =
          (typeof fields.name === "string" && fields.name) ||
          (Array.isArray(fields.name) && String(fields.name[0])) ||
          (typeof fields.scopes === "string" && fields.scopes) ||
          (Array.isArray(fields.scopes) && String(fields.scopes[0])) ||
          err.message;
        setError(String(first));
      } else {
        setError(err instanceof Error ? err.message : "Network error");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <header className="mb-5">
        <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
          Create a new token
        </h2>
        <p className="mt-0.5 text-sm text-zinc-600 dark:text-zinc-400">
          Give the token a descriptive name and select the permissions it needs.
        </p>
      </header>
      <form onSubmit={onSubmit} className="space-y-5">
        <div className="space-y-1.5">
          <Label htmlFor="token-name">Token name</Label>
          <Input
            id="token-name"
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Personal Hub prod"
          />
        </div>

        {/* Read scopes */}
        {readScopes.length > 0 && (
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium text-zinc-700 dark:text-zinc-200">
              Read access
            </legend>
            {readScopes.map((choice) => (
              <label
                key={choice.name}
                className="flex cursor-pointer items-start gap-3 rounded-lg border border-zinc-200 bg-zinc-50/60 px-4 py-3 hover:bg-zinc-100/60 dark:border-zinc-700 dark:bg-zinc-800/40 dark:hover:bg-zinc-800/60"
              >
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 rounded accent-indigo-600"
                  checked={scopes.has(choice.name)}
                  onChange={() => toggleScope(choice.name)}
                />
                <span className="flex-1">
                  <span className="block font-mono text-xs text-zinc-700 dark:text-zinc-200">
                    {choice.name}
                  </span>
                  <span className="mt-0.5 block text-xs text-zinc-500 dark:text-zinc-400">
                    {choice.label}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
        )}

        {/* Write scopes — amber card */}
        {writeScopes.length > 0 && (
          <fieldset className="space-y-2 rounded-xl border border-amber-300 bg-amber-50 p-4 dark:border-amber-700/60 dark:bg-amber-950/20">
            <div className="mb-2">
              <legend className="text-sm font-semibold text-amber-800 dark:text-amber-300">
                Write access — use with caution
              </legend>
              <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
                Tyto scopes umožní MCP klientovi zapisovat eventy do tvých a tvých týmových
                kalendářů <strong>bez dalšího potvrzení</strong>. Udělej jen klientům, kterým plně
                důvěřuješ.
              </p>
            </div>
            {writeScopes.map((choice) => (
              <label
                key={choice.name}
                className="flex cursor-pointer items-start gap-3 rounded-lg border border-amber-200 bg-white px-4 py-3 hover:bg-amber-50/60 dark:border-amber-800/50 dark:bg-zinc-800/40 dark:hover:bg-amber-950/30"
              >
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 rounded accent-amber-600"
                  checked={scopes.has(choice.name)}
                  onChange={() => toggleScope(choice.name)}
                />
                <span className="flex-1">
                  <span className="block font-mono text-xs text-zinc-700 dark:text-zinc-200">
                    {choice.name}
                  </span>
                  <span className="mt-0.5 block text-xs text-zinc-500 dark:text-zinc-400">
                    {choice.label}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
        )}

        <FormError message={error} />

        <Button
          type="submit"
          disabled={submitting}
          className={
            hasWriteScope
              ? "bg-amber-500 text-white hover:bg-amber-600 sm:w-auto sm:px-6 dark:bg-amber-600 dark:hover:bg-amber-700"
              : "sm:w-auto sm:px-6"
          }
        >
          {submitting
            ? "Creating…"
            : hasWriteScope
              ? "Create token with write access"
              : "Create token"}
        </Button>
      </form>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Token table
// ---------------------------------------------------------------------------

function TokenTable({
  tokens,
  onRevoke,
  onViewCalls,
}: {
  tokens: McpToken[];
  onRevoke: (t: McpToken) => void;
  onViewCalls: (t: McpToken) => void;
}) {
  if (tokens.length === 0) {
    return (
      <section className="rounded-xl border border-dashed border-zinc-300 bg-white p-8 text-center text-sm text-zinc-500 dark:border-zinc-700 dark:bg-zinc-900">
        <KeyRound size={28} className="mx-auto mb-2 text-zinc-400 dark:text-zinc-500" aria-hidden />
        No tokens yet. Create one above.
      </section>
    );
  }

  return (
    <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
      <table className="w-full text-sm">
        <thead className="border-b border-zinc-200 bg-zinc-50/80 dark:border-zinc-800 dark:bg-zinc-900/60">
          <tr>
            {["Name", "Prefix", "Scopes", "Created", "Last used", "Status", "Actions"].map(
              (h) => (
                <th
                  key={h}
                  className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400"
                >
                  {h}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {tokens.map((t) => (
            <TokenRow key={t.id} token={t} onRevoke={onRevoke} onViewCalls={onViewCalls} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

const WRITE_SCOPES_SET = new Set<string>(["create_booking", "write_event"]);

function TokenRow({
  token,
  onRevoke,
  onViewCalls,
}: {
  token: McpToken;
  onRevoke: (t: McpToken) => void;
  onViewCalls: (t: McpToken) => void;
}) {
  const created = new Date(token.created_at).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  const lastUsed = token.last_used_at ? relativeTime(token.last_used_at) : "Never";
  const hasWriteScope = token.scopes.some((s) => WRITE_SCOPES_SET.has(s));

  return (
    <tr className="hover:bg-zinc-50 dark:hover:bg-zinc-800/40">
      <td className="px-4 py-3 font-medium text-zinc-900 dark:text-zinc-50">
        <span>{token.name}</span>
        {hasWriteScope && (
          <span className="ml-2 inline-block rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
            Write
          </span>
        )}
      </td>
      <td className="px-4 py-3 font-mono text-xs text-zinc-600 dark:text-zinc-300">
        {token.prefix}&hellip;
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap gap-1">
          {token.scopes.map((s) => (
            <span
              key={s}
              className={
                WRITE_SCOPES_SET.has(s)
                  ? "rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
                  : "rounded-full bg-indigo-100 px-2 py-0.5 text-[10px] font-medium text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300"
              }
            >
              {s}
            </span>
          ))}
        </div>
      </td>
      <td className="px-4 py-3 text-zinc-600 dark:text-zinc-300">{created}</td>
      <td className="px-4 py-3 text-zinc-500 dark:text-zinc-400">{lastUsed}</td>
      <td className="px-4 py-3">
        <span
          className={
            token.is_active
              ? "rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300"
              : "rounded-full bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
          }
        >
          {token.is_active ? "Active" : "Revoked"}
        </span>
      </td>
      <td className="px-4 py-3">
        <div className="flex gap-2">
          <button
            onClick={() => onViewCalls(token)}
            className="rounded-md border border-zinc-200 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
          >
            View calls
          </button>
          {token.is_active && (
            <button
              onClick={() => onRevoke(token)}
              className="rounded-md border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 hover:bg-red-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950/40"
            >
              Revoke
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Calls modal
// ---------------------------------------------------------------------------

function CallsModal({
  token,
  calls,
  onClose,
}: {
  token: McpToken;
  calls: McpTokenCall[] | null;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4">
      <div className="relative w-full max-w-2xl rounded-xl border border-zinc-200 bg-white shadow-2xl dark:border-zinc-700 dark:bg-zinc-900">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-zinc-200 px-6 py-4 dark:border-zinc-700">
          <div>
            <h2 className="text-base font-semibold text-zinc-900 dark:text-zinc-50">
              Recent calls
            </h2>
            <p className="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">
              Token: <span className="font-mono">{token.prefix}&hellip;</span> &mdash;{" "}
              {token.name}
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md p-1.5 text-zinc-500 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="max-h-[60vh] overflow-y-auto p-6">
          {calls === null ? (
            <p className="text-center text-sm text-zinc-500">Loading…</p>
          ) : calls.length === 0 ? (
            <p className="text-center text-sm text-zinc-500">
              No calls recorded for this token yet.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-200 dark:border-zinc-700">
                  {["Capability", "Status", "Duration", "Error", "Time"].map((h) => (
                    <th
                      key={h}
                      className="pb-2 text-left text-xs font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
                {calls.map((call) => (
                  <tr key={call.id} className="hover:bg-zinc-50 dark:hover:bg-zinc-800/40">
                    <td className="py-2 font-mono text-xs text-zinc-700 dark:text-zinc-200">
                      {call.capability}
                    </td>
                    <td className="py-2">
                      <span
                        className={
                          call.status === "ok"
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-red-600 dark:text-red-400"
                        }
                      >
                        {call.status}
                      </span>
                    </td>
                    <td className="py-2 text-zinc-500 dark:text-zinc-400">
                      {call.duration_ms}ms
                    </td>
                    <td className="py-2 font-mono text-xs text-zinc-500 dark:text-zinc-400">
                      {call.error_code || "—"}
                    </td>
                    <td className="py-2 text-zinc-500 dark:text-zinc-400">
                      {relativeTime(call.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}
