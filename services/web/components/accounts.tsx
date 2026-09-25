"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Check, LogIn, LogOut, RefreshCw, ShieldCheck, SkipForward } from "lucide-react";
import { api, type Account, type AccountsResponse } from "@/lib/api";
import { softSpring } from "@/lib/motion";
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

/**
 * Guided sign-in: for each account in the queue, open its login page in the live browser and wait until the
 * sign-in is detected (session cookie), then move to the next. For sites without a known cookie, the human
 * confirms and Todd verifies by visiting the site.
 */
export function SignInQueue({ accounts, onDone, onChange }: { accounts: Account[]; onDone: () => void; onChange?: () => void }) {
  const queue = useMemo(() => accounts.filter((a) => a.status !== "signed_in" && !a.via), [accounts]);
  const [ids] = useState(() => queue.map((a) => a.id));
  const [idx, setIdx] = useState(0);
  const [current, setCurrent] = useState<Account | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const opened = useRef<string | null>(null);
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
          if (a.status === "signed_in" && a.source === "session cookie") {
            onChange?.();
            setTimeout(() => alive && setIdx((i) => i + 1), 900);
          }
        })
        .catch(() => {});
    tick();
    const t = setInterval(tick, 2500);
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
      setIdx((i) => i + 1);
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
              </div>
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
      {note && <p className="mt-2 text-[11.5px] text-fg-2">{note}</p>}
    </div>
  );
}
