"use client";

import { use, useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import Link from "next/link";
import { AnimatePresence, LayoutGroup, motion } from "motion/react";
import { ArrowUp, Bot, Brain, ChevronLeft, CircleCheck, CircleX, Globe, LayoutGrid, Pause, Play, RotateCcw, Rows3, Square, X } from "lucide-react";
import { agentColor, api, usd, type AgentInfo, type Interaction, type Run, type TEvent } from "@/lib/api";
import { softSpring, spring } from "@/lib/motion";
import { ActivityIndicator, AgentAvatar, Segmented, StatusPill } from "@/components/ui";
import { buildFeed, EventRow } from "@/components/EventRow";
import { InteractionCard } from "@/components/InteractionCard";
import { AgentWindow } from "@/components/AgentWindow";
import { LiveBrowser } from "@/components/LiveBrowser";
import { SummaryText } from "@/components/SummaryText";

type View = "windows" | "timeline";

const FINISHED = ["succeeded", "failed", "cancelled", "interrupted"];

function loadPref<T>(key: string, dflt: T): T {
  try {
    const v = localStorage.getItem(key);
    return v === null ? dflt : (JSON.parse(v) as T);
  } catch {
    return dflt;
  }
}
function savePref(key: string, v: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(v));
  } catch {}
}

function duration(a: string, b: string) {
  const ms = new Date(b).getTime() - new Date(a).getTime();
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export default function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [run, setRun] = useState<Run | null>(null);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [events, setEvents] = useState<Map<number, TEvent>>(new Map());
  const [pending, setPending] = useState<Interaction[]>([]);
  const [view, setView] = useState<View>("windows");
  const [showThinking, setShowThinking] = useState(true);
  const [showBrowser, setShowBrowser] = useState(true);
  const [maxed, setMaxed] = useState<string | null>(null);
  const [showPrompt, setShowPrompt] = useState(false);
  const [shot, setShot] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [followup, setFollowup] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [opened, setOpened] = useState<string[]>([]); // finished agents the human opened from the tray
  const [hidden, setHidden] = useState<string[]>([]); // finished agents the human dismissed (this browser only)

  useEffect(() => {
    setView(loadPref<View>("todd.view", "windows"));
    setShowThinking(loadPref("todd.thinking", true));
    setShowBrowser(loadPref("todd.browserWindow", true));
  }, []);
  useEffect(() => setHidden(loadPref<string[]>(`todd.hidden.${id}`, [])), [id]);
  const saveHidden = (next: string[]) => {
    setHidden(next);
    savePref(`todd.hidden.${id}`, next);
  };

  const refresh = useCallback(() => {
    api<Run>(`/runs/${id}`).then(setRun).catch((e) => setErr(e.message));
    api<AgentInfo[]>(`/runs/${id}/agents`).then(setAgents).catch(() => {});
    api<Interaction[]>(`/runs/${id}/interactions?pending_only=true`).then(setPending).catch(() => {});
  }, [id]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2500);
    return () => clearInterval(t);
  }, [refresh]);

  useEffect(() => {
    const es = new EventSource(`/api/runs/${id}/events`);
    es.addEventListener("event", (m) => {
      const ev: TEvent = JSON.parse((m as MessageEvent).data);
      setEvents((prev) => {
        if (prev.has(ev.id)) return prev;
        const next = new Map(prev);
        next.set(ev.id, ev);
        return next;
      });
      if (["interaction", "agent_spawned", "human_reply", "summary"].includes(ev.kind) || (ev.kind === "status" && ev.data?.agent_status)) refresh();
    });
    return () => es.close();
  }, [id, refresh]);

  const list = useMemo(() => [...events.values()].sort((a, b) => a.id - b.id), [events]);
  const byAgent = useMemo(() => {
    const m = new Map<string, TEvent[]>();
    for (const ev of list) {
      if (ev.kind === "summary" && ev.data?.run) continue; // shown as the run summary card
      const key = ev.agent === "system" || ev.agent === "human" ? "planner" : ev.agent;
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(ev);
    }
    return m;
  }, [list]);
  const timeline = useMemo(() => buildFeed(list), [list]);
  const lastStep = useMemo(() => [...list].reverse().find((e) => e.kind === "browser_step"), [list]);
  const lastShot = useMemo(() => [...list].reverse().find((e) => e.data?.screenshot)?.data.screenshot as string | undefined, [list]);
  const browserUser = useMemo(() => {
    const b = [...list].reverse().find((e) => e.kind === "status" && e.data?.browser);
    return b && b.data.browser === "start" ? agents.find((a) => a.id === b.agent) ?? null : null;
  }, [list, agents]);

  async function act(path: string, json?: unknown) {
    setErr(null);
    try {
      await api(`/runs/${id}/${path}`, { method: "POST", json });
      refresh();
    } catch (e: any) {
      setErr(e.message);
    }
  }

  if (!run) {
    return (
      <div className="grid h-[60vh] place-items-center text-fg-3">
        {err ?? <ActivityIndicator size={22} />}
      </div>
    );
  }
  const canResume = !run.active && ["failed", "interrupted", "cancelled"].includes(run.status);
  const toggle = (set: (v: any) => void, key: string, v: unknown) => {
    set(v);
    savePref(key, v);
  };
  const spawned = agents.filter((a) => a.id !== "planner");
  const isFinished = (a: AgentInfo) => a.id !== "planner" && FINISHED.includes(a.status);
  const windows = agents.filter((a) => !isFinished(a) || opened.includes(a.id));
  const tray = agents.filter((a) => isFinished(a) && !opened.includes(a.id) && !hidden.includes(a.id));
  const hiddenCount = agents.filter((a) => isFinished(a) && !opened.includes(a.id) && hidden.includes(a.id)).length;
  const pausedCount = run.paused_agents?.length ?? 0;
  const budgetUsed = run.budget_usd ? Math.min(1, run.spent_usd / run.budget_usd) : 0;

  const browserPanel = (
    <LiveBrowser
      fallback={lastShot}
      pageUrl={browserUser ? lastStep?.data?.url : null}
      footer={
        browserUser ? (
          <span className="flex items-center gap-1.5">
            <AgentAvatar name={browserUser.name} color={agentColor(agents, browserUser.id)} size={16} />
            <b className="font-semibold text-fg">{browserUser.name}</b> is using the browser. Take control for captchas or 2FA when asked.
          </span>
        ) : (
          "Idle. Take control any time to sign in or help an agent."
        )
      }
    />
  );

  return (
    <div className="flex min-h-dvh flex-col">
      {/* Toolbar */}
      <div className="sticky top-0 z-30 px-3 pt-3 md:pr-4 md:pl-1">
        <motion.div layout transition={softSpring} className="glass glass-strong rounded-[26px] px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2.5">
            <Link href="/" className="btn btn-glass btn-icon h-9 w-9" title="All runs">
              <ChevronLeft size={18} strokeWidth={2.4} />
            </Link>
            <div className="min-w-0 flex-1 basis-60">
              <button onClick={() => setShowPrompt(!showPrompt)} className="block max-w-full truncate text-left text-[15px] font-semibold tracking-[-0.018em]" title="Show the full prompt">
                {run.title}
              </button>
              <div className="mt-0.5 flex min-w-0 items-center gap-2.5 text-[12px] text-fg-2">
                <StatusPill status={run.status} live={run.active} className="h-5 text-[11px]" />
                {spawned.length > 0 && (
                  <span className="flex items-center gap-1.5">
                    <span className="flex -space-x-1">
                      {spawned.slice(0, 5).map((a) => (
                        <span
                          key={a.id}
                          title={a.name}
                          className="h-3.5 w-3.5 rounded-full ring-2 ring-[var(--wp-base)]"
                          style={{ background: `linear-gradient(150deg, color-mix(in oklab, ${agentColor(agents, a.id)} 70%, white), ${agentColor(agents, a.id)})` }}
                        />
                      ))}
                    </span>
                    <span className="hidden sm:inline">
                      {spawned.length} agent{spawned.length === 1 ? "" : "s"}
                      {pausedCount > 0 && <span className="text-indigo"> · {pausedCount} paused</span>}
                    </span>
                  </span>
                )}
                <span className="hidden items-center gap-1.5 tabular md:flex">
                  <span className="h-1 w-12 overflow-hidden rounded-full bg-fill-2">
                    <motion.span className="block h-full rounded-full bg-green" animate={{ width: `${budgetUsed * 100}%` }} transition={softSpring} />
                  </span>
                  {usd(run.spent_usd)} of {usd(run.budget_usd, 0)}
                </span>
                {run.engine === "claude_code" ? (
                  <span className="hidden items-center gap-1 rounded-full bg-[#d97757]/15 px-2 py-0.5 text-[11px] font-semibold text-[#d97757] lg:inline-flex" title="Model usage counts toward your Claude plan">
                    <Bot size={11} /> Claude plan
                  </span>
                ) : (
                  <span className="hidden tabular lg:inline">· model {usd(run.llm_cost_usd, 3)}</span>
                )}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Segmented
                id="view"
                value={view}
                onChange={(v) => toggle(setView, "todd.view", v)}
                options={[
                  { value: "windows", icon: LayoutGrid, label: <span className="hidden sm:inline">Windows</span>, title: "One window per agent" },
                  { value: "timeline", icon: Rows3, label: <span className="hidden sm:inline">Timeline</span>, title: "Everything in order" },
                ]}
              />
              <div className="flex rounded-full bg-fill p-[3px]">
                <IconToggle on={showThinking} onClick={() => toggle(setShowThinking, "todd.thinking", !showThinking)} icon={Brain} label="Thinking" />
                <IconToggle on={showBrowser} onClick={() => toggle(setShowBrowser, "todd.browserWindow", !showBrowser)} icon={Globe} label="Browser" />
              </div>
              {run.active &&
                (pausedCount > 0 ? (
                  <button className="btn btn-accent" onClick={() => act("resume-all")} title="Resume the planner and every paused agent">
                    <Play size={13} fill="currentColor" /> Resume all
                  </button>
                ) : (
                  <button className="btn btn-glass" onClick={() => act("pause-all")} title="Pause the planner and every running agent at their next step">
                    <Pause size={13} fill="currentColor" /> Pause all
                  </button>
                ))}
              {run.active && (
                <button className="btn btn-danger" onClick={() => act("cancel")}>
                  <Square size={11} fill="currentColor" /> Stop
                </button>
              )}
              {canResume && (
                <button className="btn btn-tinted" onClick={() => act("resume")} title="Continue from the last checkpoint">
                  <RotateCcw size={13} /> Resume
                </button>
              )}
            </div>
          </div>
          <AnimatePresence initial={false}>
            {showPrompt && (
              <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={softSpring} className="overflow-hidden">
                <div className="well mt-3 max-h-60 overflow-auto rounded-[16px] px-4 py-3 text-[13px] leading-relaxed whitespace-pre-wrap text-fg-2">{run.prompt}</div>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>
      </div>

      <div className="flex-1 px-3 pt-4 pb-6 md:pr-4 md:pl-1">
        {/* Things waiting on you */}
        <AnimatePresence>
          {pending.length > 0 && (
            <motion.div layout className="mb-4 grid gap-3 xl:grid-cols-2">
              <AnimatePresence mode="popLayout">
                {pending.map((it) => {
                  const a = agents.find((x) => x.id === it.agent);
                  return (
                    <InteractionCard
                      key={it.id}
                      it={it}
                      agentName={a?.name ?? (it.agent === "planner" ? "Planner" : it.agent)}
                      agentColor={agentColor(agents, it.agent)}
                      planner={it.agent === "planner"}
                      onDone={refresh}
                    />
                  );
                })}
              </AnimatePresence>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Run summary */}
        <AnimatePresence>
          {run.summary && !run.active && (
            <motion.div
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={softSpring}
              className="glass mb-4 overflow-hidden rounded-[28px] p-5 md:p-6"
              style={{
                background: `radial-gradient(70% 90% at 0% 0%, color-mix(in oklab, ${run.status === "succeeded" ? "var(--green)" : "var(--red)"} 14%, transparent), transparent 70%), var(--glass-tint)`,
              }}
            >
              <div className="flex flex-wrap items-center gap-3">
                <span className={`grid h-10 w-10 place-items-center rounded-[13px] text-white ${run.status === "succeeded" ? "bg-green" : "bg-red"}`}>
                  {run.status === "succeeded" ? <CircleCheck size={22} /> : <CircleX size={22} />}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="title-3">{run.status === "succeeded" ? "Run complete" : "Run ended"}</div>
                  <div className="text-[12.5px] text-fg-2 tabular">
                    {spawned.length} agent{spawned.length === 1 ? "" : "s"} · {duration(run.created_at, run.updated_at)} · spent {usd(run.spent_usd)} ·{" "}
                    {run.engine === "claude_code" ? "via your Claude plan" : `model ${usd(run.llm_cost_usd, 3)}`}
                  </div>
                </div>
              </div>
              <div className="mt-5 border-t border-sep pt-5">
                <SummaryText text={run.summary} className="text-[14px]" />
              </div>
              <form
                className="mt-5 flex h-11 items-center gap-2 rounded-full bg-fill pr-1.5 pl-4 transition-shadow focus-within:shadow-[0_0_0_3.5px_color-mix(in_oklab,var(--accent)_30%,transparent)]"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!followup.trim()) return;
                  act("message", { text: followup });
                  setFollowup("");
                }}
              >
                <input
                  className="min-w-0 flex-1 bg-transparent text-[14px] outline-none placeholder:text-fg-3"
                  placeholder="Keep going: ask the planner for a change or the next step…"
                  value={followup}
                  onChange={(e) => setFollowup(e.target.value)}
                />
                <button className="btn btn-accent btn-sm h-8 shrink-0" disabled={!followup.trim()} title="Continue this run with your message">
                  Continue <ArrowUp size={14} strokeWidth={2.6} />
                </button>
              </form>
            </motion.div>
          )}
        </AnimatePresence>

        {view === "windows" ? (
          <LayoutGroup>
            {(tray.length > 0 || hiddenCount > 0) && (
              <motion.div layout transition={softSpring} className="mb-4">
                <div className="mb-2 flex items-center gap-2 px-1.5">
                  <span className="eyebrow">Finished agents</span>
                  <span className="text-[11.5px] text-fg-3 tabular">{tray.length}</span>
                  <span className="ml-auto flex items-center gap-3 text-[12px] font-medium">
                    {hiddenCount > 0 && (
                      <button className="text-accent" onClick={() => saveHidden([])}>
                        Show {hiddenCount} hidden
                      </button>
                    )}
                    {tray.length > 1 && (
                      <button className="text-fg-2 hover:text-fg" onClick={() => saveHidden([...hidden, ...tray.map((a) => a.id)])}>
                        Hide all
                      </button>
                    )}
                  </span>
                </div>
                <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 300px), 1fr))" }}>
                  <AnimatePresence mode="popLayout" initial={false}>
                    {tray.map((a) => (
                      <FinishedCard
                        key={a.id}
                        agent={a}
                        color={agentColor(agents, a.id)}
                        onOpen={() => setOpened([...opened, a.id])}
                        onHide={() => saveHidden([...hidden, a.id])}
                      />
                    ))}
                  </AnimatePresence>
                </div>
              </motion.div>
            )}
            <div className="grid gap-3 md:gap-4" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 440px), 1fr))" }}>
              {windows.map((a) => (
                <AgentWindow
                  key={a.id}
                  runId={id}
                  agent={a}
                  agents={agents}
                  events={byAgent.get(a.id) ?? []}
                  active={run.active}
                  showThinking={showThinking}
                  maximized={maxed === a.id}
                  onToggleMax={() => setMaxed(maxed === a.id ? null : a.id)}
                  onShot={setShot}
                  onChanged={refresh}
                  onCollapse={isFinished(a) ? () => setOpened(opened.filter((x) => x !== a.id)) : undefined}
                />
              ))}
              {showBrowser && (
                <motion.div layout transition={softSpring}>
                  {browserPanel}
                </motion.div>
              )}
            </div>
          </LayoutGroup>
        ) : (
          <div className="flex flex-col gap-4 lg:flex-row">
            <div className="glass min-w-0 flex-1 rounded-[28px] p-4 md:p-5">
              <div className="space-y-3">
                {timeline.map((ev) => (
                  <EventRow key={ev.id} ev={ev} agents={agents} showAgent showThinking={showThinking} live={run.active} onShot={setShot} />
                ))}
              </div>
            </div>
            {showBrowser && <aside className="lg:sticky lg:top-28 lg:h-fit lg:w-[44%] lg:max-w-[700px]">{browserPanel}</aside>}
          </div>
        )}
      </div>

      {view === "timeline" && (
        <form
          className="sticky bottom-24 z-20 mx-auto mb-4 w-full max-w-2xl px-3 md:bottom-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (!note.trim()) return;
            act("message", { text: note });
            setNote("");
          }}
        >
          <div className="glass glass-strong flex h-12 items-center gap-2 rounded-full pr-1.5 pl-5">
            <input className="min-w-0 flex-1 bg-transparent text-[14px] outline-none placeholder:text-fg-3" placeholder={run.active ? "Message the planner…" : "Continue this run: tell the planner what to do next…"} value={note} onChange={(e) => setNote(e.target.value)} />
            <button className="grid h-9 w-9 place-items-center rounded-full bg-accent text-white disabled:opacity-30" disabled={!note.trim()}>
              <ArrowUp size={17} strokeWidth={2.6} />
            </button>
          </div>
        </form>
      )}
      {err && <div className="mx-3 mb-4 rounded-[14px] bg-red/10 px-4 py-2 text-[12.5px] text-red">{err}</div>}

      <Lightbox src={shot} onClose={() => setShot(null)} />
    </div>
  );
}

/** A finished agent, folded down to one line: open it to see its window again, or hide it from this view. */
function FinishedCard({ agent, color, onOpen, onHide }: { agent: AgentInfo; color: string; onOpen: () => void; onHide: () => void }) {
  const line = (agent.summary ?? "")
    .split("\n")
    .map((l) => l.replace(/^[-*\s]*(\*\*)?done:?(\*\*)?\s*/i, "").trim())
    .find(Boolean);
  return (
    <motion.div
      layout
      initial={{ opacity: 0, scale: 0.96 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.96 }}
      transition={softSpring}
      className="glass flex items-center gap-1 rounded-[20px] py-2 pr-1.5 pl-2.5"
    >
      <button onClick={onOpen} className="flex min-w-0 flex-1 items-center gap-2.5 text-left" title="Open this agent's window">
        <AgentAvatar name={agent.name} color={color} size={30} />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate text-[13.5px] font-semibold tracking-[-0.01em]">{agent.name}</span>
            <StatusPill status={agent.status} live={false} className="h-5 text-[11px]" />
          </span>
          <span className="block truncate text-[12px] text-fg-2">{line || agent.task.split("\n")[0]}</span>
        </span>
      </button>
      <button className="btn btn-plain btn-sm btn-icon w-7 shrink-0 text-fg-3 hover:text-fg" title="Hide from this view" onClick={onHide}>
        <X size={14} />
      </button>
    </motion.div>
  );
}

function IconToggle({ on, onClick, icon: Icon, label }: { on: boolean; onClick: () => void; icon: typeof Brain; label: string }) {
  return (
    <button
      onClick={onClick}
      title={`${on ? "Hide" : "Show"} ${label.toLowerCase()}`}
      className={`relative isolate flex h-7 items-center gap-1.5 rounded-full px-2.5 text-[12.5px] font-medium transition-colors ${on ? "text-fg" : "text-fg-3 hover:text-fg-2"}`}
    >
      <AnimatePresence>
        {on && (
          <motion.span
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            transition={spring}
            className="absolute inset-0 -z-10 rounded-full bg-[var(--control-hover)] shadow-[var(--control-shadow)]"
          />
        )}
      </AnimatePresence>
      <Icon size={13} strokeWidth={2.2} className={on ? "text-accent" : ""} />
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
}

function Lightbox({ src, onClose }: { src: string | null; onClose: () => void }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return null;
  return createPortal(
    <AnimatePresence>
      {src && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[70] grid place-items-center bg-black/55 p-6 backdrop-blur-xl"
          onClick={onClose}
        >
          <button className="btn btn-glass btn-icon absolute top-5 right-5 h-9 w-9 text-white">
            <X size={16} />
          </button>
          <motion.img
            initial={{ scale: 0.9, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: 0.95, opacity: 0 }}
            transition={softSpring}
            src={src}
            alt="browser step"
            className="max-h-full max-w-full rounded-[18px] shadow-2xl ring-1 ring-white/15"
          />
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
