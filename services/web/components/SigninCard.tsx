"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { motion } from "motion/react";
import { ArrowRight, Check, Clock, KeyRound, LogIn, RefreshCw, TerminalSquare } from "lucide-react";
import { api, type Account, type Interaction } from "@/lib/api";
import { softSpring } from "@/lib/motion";
import { Favicon, StatusChip } from "./accounts";
import { openLiveBrowser } from "./LiveBrowser";
import { ActivityIndicator, AgentAvatar, IconTile } from "./ui";

type Wanted = { id: string; name: string; why?: string; status?: Account["status"] };

/** "Sign in once": the accounts a run needs, each with a Sign in button, plus Continue / Later. The run waits here;
 *  Todd closes the card by itself once every service is signed in (and then connects their CLIs). */
export function SigninCard({
  it,
  agentName,
  agentColor,
  planner = false,
  onDone,
}: {
  it: Interaction;
  agentName: string;
  agentColor: string;
  planner?: boolean;
  onDone: () => void;
}) {
  const wanted: Wanted[] = it.data.services ?? [];
  const [rows, setRows] = useState<Record<string, Account>>({});
  const [opening, setOpening] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const latest = useRef(0);

  const load = useCallback(async () => {
    const req = ++latest.current; // an older, slower poll must not overwrite a newer status
    const got = await Promise.all(
      wanted.map((w) =>
        api<Account>(`/accounts/${w.id}`)
          .then((a): [string, Account] => [w.id, a])
          .catch(() => null),
      ),
    );
    if (req !== latest.current) return;
    setRows(Object.fromEntries(got.filter((x): x is [string, Account] => x !== null)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [it.id]);

  useEffect(() => {
    load();
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [load]);

  async function signIn(w: Wanted) {
    setOpening(w.id);
    setErr(null);
    try {
      await api(`/accounts/${w.id}/open`, { method: "POST" });
      openLiveBrowser({ pageUrl: rows[w.id]?.login ?? null, note: `Sign in to ${w.name}. This card updates when you're done.` });
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setOpening(null);
    }
  }

  async function check(w: Wanted) {
    setOpening(w.id);
    try {
      await api(`/accounts/${w.id}/verify`, { method: "POST" });
      await load();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setOpening(null);
    }
  }

  async function send(decision: "approve" | "deny") {
    setBusy(true);
    setErr(null);
    try {
      await api(`/interactions/${it.id}`, {
        method: "POST",
        json: { decision, answer: decision === "deny" ? "Later" : "Continue" },
      });
      onDone();
    } catch (e: any) {
      setErr(e.message);
      setBusy(false);
    }
  }

  const done = wanted.filter((w) => rows[w.id]?.status === "signed_in").length;
  const all = wanted.length > 0 && done === wanted.length;
  const color = "var(--accent)";

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 16, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.18 } }}
      transition={softSpring}
      className="glass glass-strong overflow-hidden rounded-[28px] p-5"
      style={{
        background: `radial-gradient(100% 55% at 50% 0%, color-mix(in oklab, ${color} 16%, transparent), transparent 75%), var(--glass-tint-strong)`,
      }}
    >
      <div className="flex items-center gap-3">
        <IconTile icon={KeyRound} color={color} size={34} />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold tracking-[-0.01em]">Sign in to continue</div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[12px] text-fg-2">
            for <AgentAvatar name={agentName} color={agentColor} planner={planner} size={16} />
            <span className="truncate font-medium text-fg">{agentName}</span>
          </div>
        </div>
        <span className="text-[12px] font-medium text-fg-2 tabular">
          {done}/{wanted.length}
        </span>
      </div>

      <p className="mt-4 text-[14px] leading-snug text-fg-2">
        Sign in once and Todd sets up the keys and command-line tools itself. Nothing else will stop for a login.
        {it.data.reason ? <span className="mt-1 block text-fg">{it.data.reason}</span> : null}
      </p>

      <div className="mt-4 flex flex-col gap-1.5">
        {wanted.map((w) => {
          const a = rows[w.id];
          const signed = a?.status === "signed_in";
          const cli = a?.cli;
          return (
            <div key={w.id} className="well flex items-center gap-3 rounded-[16px] px-3 py-2.5">
              <Favicon a={(a ?? { id: w.id, name: w.name }) as Account} size={28} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[13.5px] font-semibold">{w.name}</span>
                  {a && <StatusChip a={a} />}
                </div>
                <div className="mt-0.5 flex items-center gap-1.5 truncate text-[11.5px] text-fg-2">
                  {cli && signed ? (
                    <>
                      <TerminalSquare size={11} className="shrink-0" />
                      <span className="truncate">{cli.connected ? `${cli.name} connected` : cli.message || `${cli.name} connects next`}</span>
                    </>
                  ) : (
                    <span className="truncate">{w.why}</span>
                  )}
                </div>
              </div>
              {signed ? (
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-green/15 text-green">
                  <Check size={15} strokeWidth={3} />
                </span>
              ) : a?.status === "unknown" ? (
                <div className="flex shrink-0 gap-1.5">
                  <button className="btn btn-glass btn-sm" disabled={opening === w.id} onClick={() => check(w)} title="Visit the site to check">
                    {opening === w.id ? <ActivityIndicator size={12} /> : <RefreshCw size={12} />} Check
                  </button>
                  <button className="btn btn-accent btn-sm" disabled={opening === w.id} onClick={() => signIn(w)}>
                    <LogIn size={12} /> Sign in
                  </button>
                </div>
              ) : (
                <button className="btn btn-accent btn-sm shrink-0" disabled={opening === w.id} onClick={() => signIn(w)}>
                  {opening === w.id ? <ActivityIndicator size={12} /> : <LogIn size={12} />} Sign in
                </button>
              )}
            </div>
          );
        })}
      </div>

      <div className="mt-4 flex gap-2">
        <button className="btn btn-glass btn-lg flex-1" disabled={busy} onClick={() => send("deny")} title="Todd works around these for now">
          <Clock size={15} /> Later
        </button>
        <button className="btn btn-accent btn-lg flex-1" disabled={busy} onClick={() => send("approve")}>
          {all ? "Continue" : "Continue anyway"} <ArrowRight size={15} strokeWidth={2.4} />
        </button>
      </div>
      {err && <p className="mt-2 text-[12px] text-red">{err}</p>}
    </motion.div>
  );
}
