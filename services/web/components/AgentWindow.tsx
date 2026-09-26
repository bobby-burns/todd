"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { ArrowUp, ChevronsDownUp, Maximize2, Minimize2, Pause, Play, Square } from "lucide-react";
import { agentColor, api, shortModel, usd, type AgentInfo, type TEvent } from "@/lib/api";
import { softSpring, spring } from "@/lib/motion";
import { buildFeed, EventRow } from "./EventRow";
import { AgentAvatar, StatusPill } from "./ui";

export function AgentWindow({
  runId,
  agent,
  agents,
  events,
  active,
  showThinking,
  maximized,
  onToggleMax,
  onShot,
  onChanged,
  onCollapse,
}: {
  runId: string;
  agent: AgentInfo;
  agents: AgentInfo[];
  events: TEvent[];
  active: boolean;
  showThinking: boolean;
  maximized: boolean;
  onToggleMax: () => void;
  onShot: (src: string) => void;
  onChanged?: () => void;
  /** A finished agent the human opened from the tray: fold it back. */
  onCollapse?: () => void;
}) {
  const color = agentColor(agents, agent.id);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [busy, setBusy] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const isPlanner = agent.id === "planner";
  const running = isPlanner ? active : active && ["running", "waiting", "paused"].includes(agent.status);
  const paused = agent.status === "paused";
  const feed = useMemo(() => buildFeed(events), [events]);
  const last = feed[feed.length - 1];
  const working = running && !paused && agent.status === "running" && last && !(last.kind === "tool_call" && !last.result);
  const canMessage = running || isPlanner; // a run that has ended continues when its planner gets a message

  async function togglePause() {
    setBusy(true);
    setErr(null);
    try {
      await api(`/runs/${runId}/agents/${agent.id}/${paused ? "resume" : "pause"}`, { method: "POST" });
      onChanged?.();
    } catch (e: any) {
      setErr(e.message);
    }
    setBusy(false);
  }

  useEffect(() => {
    const el = scroller.current;
    if (follow && el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" }); // this window only, never the page
  }, [feed.length, follow, working]);

  async function send() {
    if (!msg.trim()) return;
    setErr(null);
    try {
      await api(`/runs/${runId}/message`, { method: "POST", json: { text: msg, agent_id: agent.id } });
      setMsg("");
      if (!active) onChanged?.();
    } catch (e: any) {
      setErr(e.message);
    }
  }

  return (
    <motion.section
      layout
      transition={softSpring}
      className={`glass flex min-h-0 flex-col overflow-hidden rounded-[28px] ${maximized ? "col-span-full h-[80vh]" : "h-[580px]"}`}
      style={{
        background: `radial-gradient(90% 38% at 0% 0%, color-mix(in oklab, ${color} 14%, transparent), transparent 70%), radial-gradient(120% 60% at 0% 0%, var(--glass-sheen), transparent 55%), var(--glass-tint)`,
      }}
    >
      <motion.header layout="position" className="flex items-center gap-3 px-4 pt-4 pb-3">
        <AgentAvatar name={agent.name} color={color} size={36} planner={isPlanner} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-[15px] font-semibold tracking-[-0.018em]">{agent.name}</h3>
            <StatusPill status={agent.status} live={running} />
          </div>
          <div className="mt-0.5 flex min-w-0 items-center gap-1 truncate text-[11.5px] text-fg-3">
            <span className="truncate">{shortModel(agent.model)}</span>
            {agent.toolsets.length > 0 && <span className="truncate">· {agent.toolsets.join(", ")}</span>}
            {agent.llm_cost_usd != null && agent.llm_cost_usd > 0 && <span className="tabular">· {usd(agent.llm_cost_usd, 3)}</span>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {running && (
            <button
              className={`btn btn-sm ${paused ? "btn-accent" : "btn-glass"}`}
              title={paused ? "Resume this agent" : "Pause this agent (it stops at its next step)"}
              disabled={busy}
              onClick={togglePause}
            >
              {paused ? <Play size={12} fill="currentColor" /> : <Pause size={12} fill="currentColor" />}
              {paused ? "Resume" : "Pause"}
            </button>
          )}
          {!isPlanner && running && (
            <button
              className="btn btn-glass btn-sm btn-icon w-7 hover:text-red"
              title="Stop this agent"
              onClick={() => api(`/runs/${runId}/agents/${agent.id}/cancel`, { method: "POST" }).catch((e) => setErr(e.message))}
            >
              <Square size={11} fill="currentColor" />
            </button>
          )}
          {onCollapse && (
            <button className="btn btn-plain btn-sm btn-icon w-7 text-fg-2" title="Collapse" onClick={onCollapse}>
              <ChevronsDownUp size={14} />
            </button>
          )}
          <button className="btn btn-plain btn-sm btn-icon w-7 text-fg-2" title={maximized ? "Restore" : "Expand"} onClick={onToggleMax}>
            {maximized ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
        </div>
      </motion.header>
      <div className="mx-4 h-px bg-sep" />

      <div
        ref={scroller}
        className="scrollbar-thin min-h-0 flex-1 overflow-y-auto px-4 pt-3 pb-4"
        style={{ maskImage: "linear-gradient(to bottom, transparent, #000 14px, #000 calc(100% - 18px), transparent)" }}
        onScroll={(e) => {
          const el = e.currentTarget;
          setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
        }}
      >
        {!isPlanner && agent.task && <TaskCard task={agent.task} color={color} />}
        {feed.length === 0 && <p className="py-10 text-center text-[12.5px] text-fg-3">Waiting for its first step…</p>}
        <div className="space-y-2.5">
          {feed.map((ev) => (
            <EventRow key={ev.id} ev={ev} agents={agents} showThinking={showThinking} live={running && !paused} onShot={onShot} />
          ))}
          {working && (
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex items-center gap-2 pt-1 pl-0.5 text-[12.5px] font-medium">
              <span className="flex gap-[3px]">
                {[0, 1, 2].map((i) => (
                  <motion.span
                    key={i}
                    className="h-[5px] w-[5px] rounded-full"
                    style={{ background: color }}
                    animate={{ opacity: [0.25, 1, 0.25], y: [0, -2, 0] }}
                    transition={{ duration: 1.1, repeat: Infinity, delay: i * 0.15 }}
                  />
                ))}
              </span>
              <span className="shimmer">Working</span>
            </motion.div>
          )}
        </div>
      </div>

      <AnimatePresence>
        {paused && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={softSpring}
            className="overflow-hidden"
          >
            <div className="mx-3 mb-2 flex items-center gap-2.5 rounded-[14px] bg-indigo/14 px-3 py-2.5 text-[12.5px] leading-snug">
              <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-indigo text-white">
                <Pause size={11} fill="currentColor" />
              </span>
              <span className="text-fg">
                <b className="font-semibold">Paused.</b> <span className="text-fg-2">Send a message to change course. Sending it resumes the agent.</span>
              </span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {canMessage && (
        <form
          className="mx-3 mb-3 flex h-10 items-center gap-2 rounded-full bg-fill pr-1 pl-4 transition-shadow focus-within:shadow-[0_0_0_3.5px_color-mix(in_oklab,var(--accent)_30%,transparent)]"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <input
            className="min-w-0 flex-1 bg-transparent text-[13.5px] outline-none placeholder:text-fg-3"
            placeholder={running ? `Message ${agent.name}…` : "Continue this run: tell the planner what to do next…"}
            value={msg}
            onChange={(e) => setMsg(e.target.value)}
          />
          <AnimatePresence>
            {msg.trim() && (
              <motion.button
                initial={{ scale: 0.4, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.4, opacity: 0 }}
                transition={spring}
                className="grid h-8 w-8 place-items-center rounded-full bg-accent text-white"
                aria-label="Send"
              >
                <ArrowUp size={16} strokeWidth={2.6} />
              </motion.button>
            )}
          </AnimatePresence>
        </form>
      )}
      {err && <div className="mx-3 mb-3 rounded-[12px] bg-red/10 px-3 py-1.5 text-[11.5px] text-red">{err}</div>}
    </motion.section>
  );
}

function TaskCard({ task, color }: { task: string; color: string }) {
  const [open, setOpen] = useState(false);
  const [head, checklist] = task.split(/\n+Checklist[^\n]*\n/);
  const items = (checklist ?? "")
    .split("\n")
    .map((l) => l.replace(/^\s*\d+\.\s*/, "").trim())
    .filter(Boolean);
  return (
    <button onClick={() => setOpen(!open)} className="mb-3 block w-full rounded-[14px] bg-fill/70 px-3 py-2.5 text-left">
      <div className="flex items-center gap-2">
        <span className="eyebrow">Task</span>
        {items.length > 0 && <span className="text-[11px] text-fg-3">· {items.length} items</span>}
      </div>
      <div className={`mt-1 text-[12.5px] leading-snug text-fg ${open ? "whitespace-pre-wrap" : "line-clamp-1"}`}>{head}</div>
      {open && items.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {items.map((it, i) => (
            <li key={i} className="flex gap-2 text-[12.5px] leading-snug text-fg-2">
              <span className="mt-[2px] h-3.5 w-3.5 shrink-0 rounded-full border-[1.5px]" style={{ borderColor: color }} />
              {it}
            </li>
          ))}
        </ul>
      )}
    </button>
  );
}
