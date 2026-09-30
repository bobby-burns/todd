"use client";

import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { ChevronDown, Hand, Pause, Radio } from "lucide-react";
import { agentColor, type AgentInfo, type Interaction, type TEvent } from "@/lib/api";
import { softSpring } from "@/lib/motion";
import { explainTool, type Doing } from "@/lib/plain";
import { AgentAvatar } from "./ui";

/* "Right now": for each working agent, what it's doing in plain words ("Building the site to make sure it works"),
   then, smaller, its own words about what it's after and the actual command, file or page. Plus who's waiting on you
   or paused. */

/** plain: what it's doing, in everyday words; words: the agent's own latest thought; detail: the actual command,
 *  file or page (shown small). */
type Row = { id: string; name: string; plain: string; words: string; detail?: string; since: string | null; state: "working" | "waiting" | "paused" };

const short = (t: string, n = 60) => (t.length > n ? t.slice(0, n - 1) + "…" : t);

const firstSentence = (t: string) => short((t.split(/(?<=[.!?])\s+/)[0] ?? t).replace(/\s+/g, " ").trim(), 150);

function ago(iso: string | null, now: number) {
  if (!iso) return "";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m`;
}

export function StatusWidget({ agents, events, pending, pausedIds }: { agents: AgentInfo[]; events: TEvent[]; pending: Interaction[]; pausedIds: string[] }) {
  const [open, setOpen] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(t);
  }, []);

  const rows = useMemo<Row[]>(() => {
    const active = agents.filter((a) => a.id === "planner" || ["running", "paused", "waiting"].includes(a.status));
    const byAgent = new Map<string, TEvent[]>();
    for (const e of events) {
      const k = e.agent === "system" || e.agent === "human" ? "planner" : e.agent;
      if (!byAgent.has(k)) byAgent.set(k, []);
      byAgent.get(k)!.push(e);
    }
    return active.map((a) => {
      const evs = byAgent.get(a.id) ?? [];
      const thought = [...evs].reverse().find((e) => e.kind === "thought" || e.kind === "message");
      const done = new Set(evs.filter((e) => e.kind === "tool_result").map((e) => e.data?.call_id));
      const call = [...evs].reverse().find((e) => e.kind === "tool_call" && !done.has(e.data?.call_id));
      const step = [...evs].reverse().find((e) => e.kind === "browser_step");
      const ask = pending.find((p) => p.agent === a.id && !p.data?.background); // the launch plan card doesn't stop anyone
      const paused = pausedIds.includes(a.id) || a.status === "paused";
      const taskHead = (a.task ?? "").split("\n")[0];
      const words = thought ? firstSentence(thought.text) : taskHead ? short(taskHead, 150) : "";
      let doing: Doing = call ? explainTool(call.data?.tool, call.data?.args) : { plain: thought ? "Thinking about the next step" : "Getting started" };
      if (call && String(call.data?.tool ?? "").replace(/^mcp__todd__/, "").startsWith("browser") && step && step.id > call.id)
        doing = { plain: "Clicking through a website", detail: short(step.text, 90) };
      if (ask)
        doing = {
          plain: ask.kind === "question" ? "Waiting for your answer" : ask.kind === "spend" ? "Waiting for you to approve a payment" : "Waiting for your OK",
          detail: short(ask.prompt, 90),
        };
      if (paused) doing = { plain: "Paused", detail: doing.plain };
      return {
        id: a.id,
        name: a.name,
        plain: doing.plain,
        words,
        detail: doing.detail,
        since: (call ?? thought)?.ts ?? null,
        state: paused ? "paused" : ask ? "waiting" : "working",
      };
    });
  }, [agents, events, pending, pausedIds]);

  if (!rows.length) return null;
  const waiting = rows.filter((r) => r.state === "waiting").length;
  const paused = rows.filter((r) => r.state === "paused").length;
  const working = rows.length - waiting - paused;
  return (
    <motion.section layout transition={softSpring} className="glass mb-4 overflow-hidden rounded-[24px]">
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 px-4 py-3 text-left">
        <span className="relative grid h-5 w-5 place-items-center">
          <span className="absolute h-2.5 w-2.5 animate-ping rounded-full bg-green/60" />
          <span className="h-2 w-2 rounded-full bg-green" />
        </span>
        <span className="text-[13.5px] font-semibold tracking-[-0.01em]">Right now</span>
        <span className="text-[12.5px] text-fg-2">
          · {working} working{waiting ? ` · ${waiting} waiting for you` : ""}{paused ? ` · ${paused} paused` : ""}
        </span>
        <ChevronDown size={14} className={`ml-auto text-fg-3 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={softSpring} className="overflow-hidden">
            <div className="grid gap-1.5 px-3 pb-3 md:grid-cols-2">
              {rows.map((r) => (
                <div key={r.id} className="flex items-start gap-2.5 rounded-[16px] bg-fill/45 px-3 py-2.5">
                  <AgentAvatar name={r.name} color={agentColor(agents, r.id)} planner={r.id === "planner"} size={26} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-[13px] font-semibold">{r.name}</span>
                      {r.since && r.state === "working" && <span className="shrink-0 text-[11px] text-fg-3 tabular">{ago(r.since, now)}</span>}
                    </div>
                    <p
                      className={`mt-0.5 flex items-start gap-1.5 text-[13.5px] leading-snug font-medium ${
                        r.state === "waiting" ? "text-orange" : r.state === "paused" ? "text-indigo" : "text-fg"
                      }`}
                    >
                      {r.state === "waiting" ? <Hand size={13} className="mt-[3px] shrink-0" /> : r.state === "paused" ? <Pause size={13} className="mt-[3px] shrink-0" /> : <Radio size={13} className="mt-[3px] shrink-0 text-green" />}
                      <span className={`min-w-0 ${r.state === "working" ? "shimmer" : ""}`}>{r.plain}</span>
                    </p>
                    {r.words && r.words !== r.plain && <p className="mt-1 line-clamp-2 text-[11.5px] leading-snug text-fg-2">{r.words}</p>}
                    {r.detail && (
                      <p className="mt-0.5 truncate font-mono text-[10.5px] text-fg-3" title={r.detail}>
                        {r.detail}
                      </p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.section>
  );
}
