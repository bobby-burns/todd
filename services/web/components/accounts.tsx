"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Check, LogIn, LogOut, RefreshCw, ShieldCheck, SkipForward, TerminalSquare } from "lucide-react";
import { api, type Account, type AccountsResponse, type CliStatus } from "@/lib/api";
import { softSpring } from "@/lib/motion";
import { openLiveBrowser } from "./LiveBrowser";
import { ActivityIndicator } from "./ui";

export function useAccounts(pollMs = 0) {
  const [data, setData] = useState<AccountsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(
    () =>
      api<AccountsResponse>("/accounts")
        .then((d) => {
          setData(d);
          setErr(null);
        })
        .catch((e) => setErr(e.message)),
    [],
  );
  useEffect(() => {
    load();
    if (!pollMs) return;
    const t = setInterval(load, pollMs);
    return () => clearInterval(t);
  }, [load, pollMs]);
  return { data, err, reload: load, setData };
}

const STATUS = {
  signed_in: { label: "Signed in", color: "var(--green)" },
  signed_out: { label: "Signed out", color: "var(--fg-2)" },
  unknown: { label: "Unknown", color: "var(--orange)" },
} as const;

export function StatusChip({ a }: { a: Account }) {
  const m = STATUS[a.status];
  return (
    <span
      className="inline-flex h-[22px] shrink-0 items-center gap-1 rounded-full px-2 text-[11px] font-semibold"
      style={{ color: m.color, background: `color-mix(in oklab, ${m.color} 14%, transparent)` }}
      title={a.source}
    >
      {a.status === "signed_in" && <Check size={11} strokeWidth={3} />}
      {m.label}
    </span>
  );
}

const APP_COLORS = ["#0a84ff", "#30d158", "#ff9f0a", "#ff375f", "#bf5af2", "#5e5ce6", "#40c8e0", "#ff453a", "#64d2ff", "#ffcc00", "#ac8e68", "#32ade6"];

/** App-icon style letter avatar (no third-party icon requests: which services you use stays private). */
export function Favicon({ a, size = 20 }: { a: Account; size?: number }) {
  let h = 0;
  for (const ch of a.id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const c = APP_COLORS[h % APP_COLORS.length];
  return (
    <span
      className="grid shrink-0 place-items-center font-semibold text-white"
      style={{
        width: size,
        height: size,
        borderRadius: size * 0.26,
        fontSize: size * 0.46,
        letterSpacing: "-0.02em",
        background: `linear-gradient(155deg, color-mix(in oklab, ${c} 70%, white), ${c} 60%, color-mix(in oklab, ${c} 85%, black))`,
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / 0.35), 0 2px 6px -2px rgb(0 0 0 / 0.3)",
      }}
    >
      {a.name[0]}
    </span>
  );
}

export function expiresLabel(a: Account): string | null {
  if (!a.expires) return null;
  const days = Math.round((a.expires * 1000 - Date.now()) / 86400000);
  if (days < 1) return "session expires today";
  return `session ~${days}d left`;
}

const CLI_BUSY = ["starting", "approving", "needs_you"];

/** Connect (and follow) the account's CLI with the browser session. */
function useCli(a: Account, onChange?: () => void) {
  const [st, setSt] = useState<CliStatus | null>(a.cli ?? null);
  const busy = !!st && CLI_BUSY.includes(st.state);
  useEffect(() => {
    if (a.cli && !CLI_BUSY.includes(st?.state ?? "")) setSt(a.cli);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [a.cli?.state, a.cli?.connected]);
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(
      () =>
        api<CliStatus>(`/accounts/${a.id}/connect`)
          .then((s) => {
            setSt(s);
            if (!CLI_BUSY.includes(s.state)) onChange?.();
          })
          .catch(() => {}),
      1500,
    );
    return () => clearInterval(t);
  }, [busy, a.id, onChange]);
  const start = useCallback(
    () => api<CliStatus>(`/accounts/${a.id}/connect`, { method: "POST" }).then(setSt).catch((e) => setSt((s) => s && { ...s, state: "failed", message: e.message })),
    [a.id],
  );
  return { st, busy, start };
}

/** "CLI" line on an account: the same sign-in, for the service's command-line tool. */
export function CliRow({ a, onChange }: { a: Account; onChange?: () => void }) {
  const { st, busy, start } = useCli(a, onChange);
  if (!st) return null;
  const tone = st.connected ? "var(--green)" : st.state === "needs_you" ? "var(--orange)" : st.state === "failed" ? "var(--red)" : "var(--fg-2)";
  return (
    <div className="mt-3 flex items-start gap-2 rounded-[12px] bg-fill/60 px-2.5 py-2 text-[11.5px] leading-snug">
      <TerminalSquare size={13} className="mt-px shrink-0 text-fg-3" />
      <div className="min-w-0 flex-1">
        <span className="font-semibold text-fg">{st.name}</span>{" "}
        <span style={{ color: tone }}>
          {st.connected ? "signed in with this account" : busy ? st.message || "Connecting…" : st.state === "failed" ? st.message : "not connected"}
        </span>
        {st.state === "needs_you" && <div className="mt-0.5 text-fg-2">Todd continues on its own right after.</div>}
      </div>
      {st.state === "needs_you" ? (
        <button className="btn btn-accent btn-sm shrink-0" onClick={() => openLiveBrowser({ note: st.message })} title="Open the browser big, in control">
          Open browser
        </button>
      ) : busy ? (
        <ActivityIndicator size={12} />
      ) : (
        !st.connected &&
        a.status === "signed_in" && (
          <button className="btn btn-glass btn-sm shrink-0" onClick={start} title="Sign in the CLI with this browser session">
            {st.state === "failed" ? "Retry" : "Connect"}
          </button>
        )
      )}
    </div>
  );
}

/**
 * Guided sign-in: for each account in the queue, open its login page in the live browser and wait until the
 * sign-in is detected (session cookie), then move to the next. For sites without a known cookie, the human
 * confirms and Todd verifies by visiting the site. Sign in once: when the account has a CLI, Todd connects it with
 * the fresh session right away (the human is still here if a 2FA page appears), then moves on.
 */
export function SignInQueue({ accounts, onDone, onChange }: { accounts: Account[]; onDone: () => void; onChange?: () => void }) {
  const queue = useMemo(() => accounts.filter((a) => a.status !== "signed_in" && !a.via), [accounts]);
  const [ids] = useState(() => queue.map((a) => a.id));
  const [idx, setIdx] = useState(0);
  const [current, setCurrent] = useState<Account | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const opened = useRef<string | null>(null);
  const connecting = useRef<string | null>(null);
  const id = ids[idx];

  useEffect(() => {
    if (!id) return;
    let alive = true;
    if (opened.current !== id) {
      opened.current = id;
      setMsg(null);
      api(`/accounts/${id}/open`, { method: "POST" }).catch((e) => setMsg(`Couldn't open the login page: ${e.message}`));
    }
    const tick = () =>
      api<Account>(`/accounts/${id}`)
        .then((a) => {
          if (!alive) return;
          setCurrent(a);
          if (a.status !== "signed_in" || (a.source !== "session cookie" && connecting.current !== id)) return;
          if (a.cli && !a.cli.connected && a.cli.state !== "failed") {
            if (connecting.current !== id) {
              connecting.current = id;
              api(`/accounts/${id}/connect`, { method: "POST" }).catch(() => {});
            }
            return; // wait for the CLI too: sign in once
          }
          onChange?.();
          setTimeout(() => alive && setIdx((i) => i + 1), 900);
        })
        .catch(() => {});
    tick();
    const t = setInterval(tick, 1500);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [id, onChange]);

  if (ids.length === 0 || !id) {
    return (
      <motion.div initial={{ opacity: 0, scale: 0.97 }} animate={{ opacity: 1, scale: 1 }} transition={softSpring} className="glass flex items-center gap-3 rounded-[24px] p-4">
        <span className="grid h-10 w-10 place-items-center rounded-full bg-green text-white">
          <Check size={20} strokeWidth={3} />
        </span>
        <div className="flex-1 text-[14px] font-medium">
          {ids.length === 0 ? "Everything you picked is already signed in." : `All done. Went through ${ids.length} account${ids.length === 1 ? "" : "s"}.`}
        </div>
        <button className="btn btn-accent" onClick={onDone}>
          {ids.length === 0 ? "Continue" : "Finish"}
        </button>
      </motion.div>
    );
  }

  const confirmAndVerify = async () => {
    setBusy(true);
    setMsg("Checking…");
    try {
      const r = await api<{ signed_in: boolean | null; final_url?: string; note?: string }>(`/accounts/${id}/verify`, { method: "POST" });
      if (r.signed_in === null) {
        await api(`/accounts/${id}/mark`, { method: "POST", json: { signed_in: true } });
        setMsg("Marked as signed in.");
      } else if (!r.signed_in) {
        setMsg(`Still looks signed out (landed on ${r.final_url}). Finish signing in, then try again.`);
        setBusy(false);
        return;
      }
      onChange?.();
      const acct = accounts.find((x) => x.id === id);
      if (acct?.cli && !acct.cli.connected) {
        connecting.current = id; // the tick advances once the CLI is connected too
        await api(`/accounts/${id}/connect`, { method: "POST" });
        setMsg(null);
      } else {
        setIdx((i) => i + 1);
      }
    } catch (e: any) {
      setMsg(e.message);
    }
    setBusy(false);
  };

  const a = current && current.id === id ? current : accounts.find((x) => x.id === id);
  return (
    <div className="glass overflow-hidden rounded-[26px] p-5">
      <div className="mb-4 flex items-center gap-3 text-[12px] font-medium text-fg-2 tabular">
        <span>
          {idx + 1} of {ids.length}
        </span>
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-fill-2">
          <motion.div className="h-full rounded-full bg-accent" animate={{ width: `${(idx / ids.length) * 100}%` }} transition={softSpring} />
        </div>
      </div>
      <AnimatePresence mode="wait">
        {a && (
          <motion.div
            key={a.id}
            initial={{ opacity: 0, x: 24 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -24 }}
            transition={softSpring}
            className="flex flex-wrap items-center gap-4"
          >
            <Favicon a={a} size={52} />
            <div className="min-w-0 flex-1 basis-60">
              <div className="title-3">Sign in to {a.name}</div>
              <div className="mt-0.5 text-[12.5px] leading-snug text-fg-2">
                The login page is open in the agents&apos; browser. Take control and sign in.{" "}
                {a.cookies.length ? "Todd notices automatically." : "Then tap “I'm signed in” and Todd will check."}
                {a.cli && ` Todd then signs in the ${a.cli.name} with the same session.`}
              </div>
              {a.status === "signed_in" && a.cli && <CliRow a={a} />}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {a.cookies.length > 0 && a.status !== "signed_in" && (
                <span className="flex items-center gap-2 text-[12px] text-fg-2">
                  <ActivityIndicator size={13} /> Waiting
                </span>
              )}
              {a.status === "signed_in" && <StatusChip a={a} />}
              {!a.cookies.length && (
                <button className="btn btn-accent" disabled={busy} onClick={confirmAndVerify}>
                  <ShieldCheck size={14} /> I&apos;m signed in
                </button>
              )}
              <button className="btn btn-glass" onClick={() => api(`/accounts/${id}/open`, { method: "POST" })}>
                <LogIn size={13} /> Reopen
              </button>
              <button className="btn btn-plain" onClick={() => setIdx((i) => i + 1)}>
                Skip <SkipForward size={13} />
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      {msg && <p className="mt-3 text-[12px] text-fg-2">{msg}</p>}
    </div>
  );
}

export function AccountCard({ a, onChange, onSignIn }: { a: Account; onChange: () => void; onSignIn: (a: Account) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const act = async (what: string, fn: () => Promise<any>) => {
    setBusy(what);
    setNote(null);
    try {
      const r = await fn();
      if (what === "verify" && r) setNote(r.signed_in === null ? r.note : r.signed_in ? "Verified: signed in" : `Signed out (→ ${r.final_url})`);
      if (what === "signout" && r) setNote(`Removed ${r.cookies_removed} cookies`);
    } catch (e: any) {
      setNote(e.message);
    }
    setBusy(null);
    onChange();
  };
  const exp = expiresLabel(a);
  return (
    <div className="glass flex h-full flex-col rounded-[22px] p-4">
      <div className="flex items-center gap-3">
        <Favicon a={a} size={40} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-semibold tracking-[-0.01em]">{a.name}</div>
          <div className="truncate text-[11.5px] text-fg-3">{a.via ? a.source : [a.source, exp].filter(Boolean).join(" · ")}</div>
        </div>
        <StatusChip a={a} />
      </div>
      {!a.via && (
        <div className="mt-3.5 flex flex-wrap items-center gap-1.5">
          {a.status !== "signed_in" && (
            <button className="btn btn-accent btn-sm" onClick={() => onSignIn(a)}>
              <LogIn size={12} /> Sign in
            </button>
          )}
          <button className="btn btn-glass btn-sm" disabled={!!busy} onClick={() => act("verify", () => api(`/accounts/${a.id}/verify`, { method: "POST" }))}>
            <RefreshCw size={11} className={busy === "verify" ? "animate-spin" : ""} /> Verify
          </button>
          {a.status === "signed_in" && (
            <button
              className="btn btn-plain btn-sm text-fg-2"
              disabled={!!busy}
              onClick={() => {
                if (confirm(`Sign the agents' browser out of ${a.name}?`)) act("signout", () => api(`/accounts/${a.id}/signout`, { method: "POST" }));
              }}
            >
              <LogOut size={11} /> Sign out
            </button>
          )}
          {a.status === "unknown" && (
            <button className="btn btn-plain btn-sm" onClick={() => act("mark", () => api(`/accounts/${a.id}/mark`, { method: "POST", json: { signed_in: true } }))}>
              Mark signed in
            </button>
          )}
        </div>
      )}
      {a.cli && <CliRow a={a} onChange={onChange} />}
      {note && <p className="mt-2 text-[11.5px] text-fg-2">{note}</p>}
    </div>
  );
}
