"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Copy, RefreshCw, Terminal, X } from "lucide-react";
import { api } from "@/lib/api";
import { ActivityIndicator } from "./ui";

export type CCStatus = {
  installed: boolean;
  version?: string;
  loggedIn?: boolean;
  authMethod?: string;
  email?: string;
  subscriptionType?: string;
  error?: string;
  login_command: string;
};

/** Claude Code sign-in status, a Test button, and the one command to sign in (Anthropic's own flow). */
export function ClaudeSignIn({ onStatus }: { onStatus?: (s: CCStatus) => void }) {
  const [st, setSt] = useState<CCStatus | null>(null);
  const [test, setTest] = useState<{ state: "idle" | "busy" | "ok" | "err"; text?: string }>({ state: "idle" });
  const [copied, setCopied] = useState(false);
  const load = useCallback(() => {
    setSt(null);
    api<CCStatus>("/claude-code/status")
      .then((s) => {
        setSt(s);
        onStatus?.(s);
      })
      .catch((e) => setSt({ installed: false, error: e.message, login_command: "" }));
  }, [onStatus]);
  useEffect(load, [load]);
  const signedIn = !!st?.loggedIn;
  return (
    <div className="space-y-3">
      <div className="well flex flex-wrap items-center gap-3 rounded-[18px] p-4">
        <span
          className="grid h-10 w-10 shrink-0 place-items-center rounded-[12px] text-white"
          style={{
            background: st === null ? "var(--fill-2)" : signedIn ? "linear-gradient(150deg,#5ad97a,var(--green))" : "linear-gradient(150deg,#ffb340,var(--orange))",
          }}
        >
          {st === null ? <ActivityIndicator size={16} /> : signedIn ? <Check size={20} strokeWidth={3} /> : <Terminal size={18} />}
        </span>
        <div className="min-w-0 flex-1 basis-48">
          <div className="text-[14px] font-semibold">
            {st === null ? "Checking Claude Code…" : !st.installed ? "Claude Code isn't installed" : signedIn ? "Signed in to Claude" : "Not signed in yet"}
          </div>
          <div className="truncate text-[12px] text-fg-2">
            {[st?.version, st?.authMethod, st?.email, st?.subscriptionType].filter(Boolean).join(" · ")}
            {st?.error ? ` ${st.error}` : ""}
          </div>
        </div>
        <button className="btn btn-glass btn-sm" onClick={load}>
          <RefreshCw size={12} /> Check again
        </button>
        <button
          className="btn btn-glass btn-sm"
          disabled={!signedIn || test.state === "busy"}
          onClick={async () => {
            setTest({ state: "busy" });
            const r = await api<any>("/claude-code/test", { method: "POST" }).catch((e) => ({ ok: false, error: e.message }));
            setTest(r.ok ? { state: "ok", text: `${r.model} replied “${r.reply}”` } : { state: "err", text: r.error });
          }}
        >
          {test.state === "busy" ? <ActivityIndicator size={12} /> : null} Test
        </button>
      </div>
      {test.text && (
        <p className={`flex items-center gap-1.5 px-1 text-[12.5px] ${test.state === "ok" ? "text-green" : "text-red"}`}>
          {test.state === "ok" ? <Check size={13} /> : <X size={13} />} {test.text}
        </p>
      )}
      {st && !signedIn && st.installed && (
        <div className="rounded-[18px] bg-accent/8 p-4 text-[13px] leading-relaxed">
          <div className="font-semibold">Sign in once, from a terminal on the machine running Todd:</div>
          <div className="mt-2 flex items-center gap-2">
            <code className="well min-w-0 flex-1 truncate rounded-[10px] px-3 py-2 font-mono text-[12.5px]">{st.login_command}</code>
            <button
              className="btn btn-glass btn-sm"
              onClick={() => {
                navigator.clipboard?.writeText(st.login_command);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
            >
              {copied ? <Check size={12} /> : <Copy size={12} />} {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="mt-2 text-[12px] text-fg-2">
            Pick your Claude subscription when asked. It&apos;s Anthropic&apos;s own sign-in: Todd never sees your credentials, which stay
            with Claude Code in Todd&apos;s data volume. Then press Check again.
          </p>
        </div>
      )}
    </div>
  );
}
