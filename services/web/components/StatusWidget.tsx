"use client";

import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { ChevronDown, Hand, Pause, Radio } from "lucide-react";
import { agentColor, type AgentInfo, type Interaction, type TEvent } from "@/lib/api";
import { softSpring } from "@/lib/motion";
import { AgentAvatar } from "./ui";

/* "Right now": one line per working agent on what it's trying to do (its latest thought, in its own words) and what
   it's doing this moment (the tool it's running, in plain words), plus who's waiting on you or paused. */

type Row = { id: string; name: string; goal: string; doing: string; since: string | null; state: "working" | "waiting" | "paused" };

const host = (u?: string) => {
  try {
    return u ? new URL(u).host.replace(/^www\./, "") : "";
  } catch {
    return "";
  }
};
const short = (t: string, n = 60) => (t.length > n ? t.slice(0, n - 1) + "…" : t);

/** A tool call in plain words ("Pushing code to GitHub", "Reading app/page.tsx"). */
export function describeTool(tool: string, args: Record<string, any> = {}): string {
  const a = args ?? {};
  switch (tool) {
    case "shell":
      return a.cmd ? `Running \`${short(String(a.cmd), 48)}\`` : "Running a command";
    case "write_file":
      return `Writing ${a.path ?? "a file"}`;
    case "read_file":
      return `Reading ${a.path ?? "a file"}`;
    case "list_files":
      return "Looking through the project files";
    case "git_push":
      return "Pushing code to GitHub";
    case "git":
      return `Running git ${String(a.command ?? "").split(" ")[0] || ""}`.trim();
    case "gh":
      return "Working with GitHub";
    case "cli":
      return `Using the ${a.service ?? ""} CLI`.replace("  ", " ");
    case "eas":
      return "Building the app with Expo";
    case "browse":
      return a.task ? `Using the browser: ${short(String(a.task), 60)}` : "Using the browser";
    case "spawn_agent":
      return `Starting ${a.name ?? "an agent"}`;
    case "wait_for_agents":
      return "Waiting for its agents to finish";
    case "message_agent":
      return "Sending an agent new instructions";
    case "ask_human":
      return "Waiting for your answer";
    case "request_approval":
      return "Waiting for your approval";
    case "find_integrations":
      return "Working out the best way to use each service";
    case "check_accounts":
    case "request_signins":
      return "Checking your accounts";
    case "cli_login":
      return `Connecting ${a.service ?? "a service"}`;
    case "api_request":
      return `Calling ${host(a.url) || "an API"}`;
    case "fetch_url":
      return `Reading ${host(a.url) || "a web page"}`;
  }
  if (tool.startsWith("browser_")) return "Using the browser";
  if (tool.startsWith("vercel")) return "Working with Vercel";
  if (tool.startsWith("github")) return "Working with GitHub";
  return tool.replace(/^mcp_/, "").replace(/_/g, " ");
}

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
      const ask = pending.find((p) => p.agent === a.id);
      const paused = pausedIds.includes(a.id) || a.status === "paused";
      const taskHead = (a.task ?? "").split("\n")[0];
      let doing = call ? describeTool(call.data?.tool, call.data?.args) : "Thinking";
      if (call && String(call.data?.tool ?? "").startsWith("browser") && step && step.id > call.id) doing = `In the browser: ${short(step.text, 70)}`;
      if (ask) doing = ask.kind === "question" ? "Waiting for your answer" : ask.kind === "spend" ? "Waiting for you to approve a payment" : "Waiting for your approval";
      if (paused) doing = "Paused";
      return {
        id: a.id,
        name: a.name,
        goal: thought ? firstSentence(thought.text) : taskHead ? short(taskHead, 150) : "Getting started",
        doing,
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
                    <p className="mt-0.5 line-clamp-2 text-[13px] leading-snug text-fg">{r.goal}</p>
                    <p
                      className={`mt-1 flex items-center gap-1.5 truncate text-[12px] font-medium ${
                        r.state === "waiting" ? "text-orange" : r.state === "paused" ? "text-indigo" : "text-fg-2"
                      }`}
                    >
                      {r.state === "waiting" ? <Hand size={12} /> : r.state === "paused" ? <Pause size={12} /> : <Radio size={12} className="text-green" />}
                      <span className={`truncate ${r.state === "working" ? "shimmer" : ""}`}>{r.doing}</span>
                    </p>
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
