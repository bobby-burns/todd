"use client";

import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import {
  AlertTriangle,
  ArrowUpRight,
  BookOpen,
  Brain,
  Check,
  ChevronDown,
  CircleDollarSign,
  ClipboardCheck,
  Cloud,
  FileText,
  FolderOpen,
  GitBranch,
  Globe,
  Hand,
  Hourglass,
  KeyRound,
  ListChecks,
  MessageCircleQuestion,
  Pause,
  Play,
  Plug,
  Send,
  ShieldCheck,
  Square,
  SquareTerminal,
  Users,
  Wrench,
  X,
  type LucideIcon,
} from "lucide-react";
import { agentColor, type AgentInfo, type TEvent } from "@/lib/api";
import { eventIn, softSpring } from "@/lib/motion";
import { ActivityIndicator, AgentAvatar } from "./ui";
import { SummaryText } from "./SummaryText";

/* ───────────────────────── Feed model ───────────────────────── */

export type FeedItem = TEvent & { result?: TEvent };

/** Pairs each tool_call with its tool_result (by call id, else first unmatched result of the same tool). */
export function buildFeed(events: TEvent[]): FeedItem[] {
  const out: FeedItem[] = [];
  const open = new Map<string, FeedItem>();
  const openByTool = new Map<string, FeedItem[]>();
  for (const ev of events) {
    if (ev.kind === "tool_call") {
      const item: FeedItem = { ...ev };
      out.push(item);
      if (ev.data?.call_id) open.set(ev.data.call_id, item);
      const k = `${ev.agent}:${ev.data?.tool}`;
      openByTool.set(k, [...(openByTool.get(k) ?? []), item]);
      continue;
    }
    if (ev.kind === "tool_result") {
      let target = ev.data?.call_id ? open.get(ev.data.call_id) : undefined;
      const k = `${ev.agent}:${ev.data?.tool}`;
      const q = openByTool.get(k) ?? [];
      if (!target) target = q.find((i) => !i.result);
      if (target && !target.result) {
        target.result = ev;
        openByTool.set(k, q.filter((i) => i !== target));
        continue;
      }
    }
    out.push(ev);
  }
  // A successful spawn_agent call is shown by its "Spawned" card instead.
  return out.filter((e) => !(e.kind === "tool_call" && e.data?.tool === "spawn_agent" && e.result && e.result.data?.ok !== false));
}

/* ───────────────────────── Tool presentation ───────────────────────── */

type ToolLook = { icon: LucideIcon; color?: string };
const TOOLS: Record<string, ToolLook> = {
  shell: { icon: SquareTerminal },
  write_file: { icon: FileText },
  read_file: { icon: BookOpen },
  list_files: { icon: FolderOpen },
  git_push: { icon: GitBranch, color: "var(--orange)" },
  browse: { icon: Globe, color: "var(--accent)" },
  fetch_url: { icon: ArrowUpRight, color: "var(--teal)" },
  api_request: { icon: ArrowUpRight, color: "var(--teal)" },
  find_integrations: { icon: Plug, color: "var(--purple)" },
  check_accounts: { icon: KeyRound, color: "var(--pink)" },
  request_signins: { icon: KeyRound, color: "var(--pink)" },
  request_approval: { icon: ShieldCheck, color: "var(--orange)" },
  ask_human: { icon: MessageCircleQuestion, color: "var(--orange)" },
  spawn_agent: { icon: Users, color: "var(--indigo)" },
  wait_for_agents: { icon: Hourglass },
  message_agent: { icon: Send, color: "var(--accent)" },
  cancel_agent: { icon: Square, color: "var(--red)" },
  list_agents: { icon: ListChecks },
};
function toolLook(tool: string): ToolLook {
  if (TOOLS[tool]) return TOOLS[tool];
  if (tool.startsWith("browser_")) return { icon: Globe, color: "var(--accent)" };
  if (/buy|purchase|charge|pay/.test(tool)) return { icon: CircleDollarSign, color: "var(--green)" };
  if (tool.startsWith("vercel") || tool.startsWith("github")) return { icon: Cloud };
  if (tool.startsWith("mcp")) return { icon: Plug, color: "var(--purple)" };
  return { icon: Wrench };
}

function time(iso: string) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function Disclosure({ open, children }: { open: boolean; children: React.ReactNode }) {
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={softSpring}
          className="overflow-hidden"
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

const Code = ({ children, max = "max-h-72" }: { children: React.ReactNode; max?: string }) => (
  <pre className={`well scrollbar-thin mt-1.5 ${max} overflow-auto rounded-[10px] p-2.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-fg-2`}>
    {children}
  </pre>
);

/* ───────────────────────── Rows ───────────────────────── */

export function EventRow({
  ev,
  agents,
  showAgent = false,
  showThinking = true,
  live = false,
  onShot,
}: {
  ev: FeedItem;
  agents: AgentInfo[];
  showAgent?: boolean;
  showThinking?: boolean;
  /** The run is active (pending tool calls show a spinner). */
  live?: boolean;
  onShot?: (src: string) => void;
}) {
  if (!showThinking && (ev.kind === "thought" || ev.kind === "thinking")) return null;
  const body = <Body ev={ev} agents={agents} showThinking={showThinking} live={live} onShot={onShot} />;
  const agent = agents.find((a) => a.id === ev.agent);
  return (
    <motion.div {...eventIn} className="group" title={time(ev.ts)}>
      {showAgent ? (
        <div className="flex gap-3">
          <div className="flex w-[132px] shrink-0 items-start gap-2 pt-0.5">
            <span className="w-12 shrink-0 pt-0.5 text-right font-mono text-[10.5px] text-fg-3 tabular">{time(ev.ts)}</span>
            <AgentAvatar
              name={agent?.name ?? (ev.agent === "human" ? "You" : "System")}
              color={agentColor(agents, ev.agent)}
              planner={ev.agent === "planner" || ev.agent === "system"}
              size={18}
            />
            <span className="truncate pt-px text-[11.5px] font-medium text-fg-2">{agent?.name ?? (ev.agent === "human" ? "You" : "System")}</span>
          </div>
          <div className="min-w-0 flex-1">{body}</div>
        </div>
      ) : (
        body
      )}
    </motion.div>
  );
}

function Body({
  ev,
  agents,
  showThinking,
  live,
  onShot,
}: {
  ev: FeedItem;
  agents: AgentInfo[];
  showThinking: boolean;
  live: boolean;
  onShot?: (src: string) => void;
}) {
  const [open, setOpen] = useState(false);

  switch (ev.kind) {
    case "message":
    case "thought":
      if (ev.kind === "thought" && !showThinking) return null;
      return <p className="text-[13.5px] leading-[1.55] whitespace-pre-wrap text-fg">{ev.text}</p>;

    case "thinking":
      if (!showThinking) return null;
      return (
        <button onClick={() => setOpen(!open)} className="block w-full text-left">
          <span className="mb-1 flex items-center gap-1.5 text-[11.5px] font-medium text-fg-3">
            <Brain size={12} /> Reasoning
            <ChevronDown size={12} className={`transition-transform duration-300 ${open ? "rotate-180" : ""}`} />
          </span>
          <span className={`block border-l-2 border-sep pl-3 text-[12.5px] leading-relaxed text-fg-2 ${open ? "whitespace-pre-wrap" : "line-clamp-2"}`}>
            {ev.text}
          </span>
        </button>
      );

    case "tool_call": {
      const tool = ev.data?.tool as string;
      const look = toolLook(tool);
      const detail = ev.text.replace(`${tool}: `, "").replace(tool, "").trim();
      const res = ev.result;
      const ok = res ? res.data?.ok !== false : null;
      return (
        <div>
          <button
            onClick={() => setOpen(!open)}
            className={`flex w-full items-center gap-2.5 rounded-[12px] py-1.5 pr-2.5 pl-1.5 text-left transition-colors ${
              open ? "bg-fill" : "hover:bg-fill"
            }`}
          >
            <span
              className="grid h-[22px] w-[22px] shrink-0 place-items-center rounded-[7px]"
              style={
                look.color
                  ? { background: `color-mix(in oklab, ${look.color} 16%, transparent)`, color: look.color }
                  : { background: "var(--fill-2)", color: "var(--fg-2)" }
              }
            >
              <look.icon size={12.5} strokeWidth={2.2} />
            </span>
            <span className="shrink-0 text-[12.5px] font-semibold tracking-[-0.01em] text-fg">{tool}</span>
            <span className="min-w-0 flex-1 truncate font-mono text-[11.5px] text-fg-2">{detail}</span>
            {ok === null ? (
              live ? <ActivityIndicator size={12} className="text-fg-3" /> : null
            ) : ok ? (
              <Check size={13} strokeWidth={2.6} className="shrink-0 text-green" />
            ) : (
              <X size={13} strokeWidth={2.6} className="shrink-0 text-red" />
            )}
          </button>
          <Disclosure open={open}>
            <div className="px-1.5 pb-1">
              <Code>{JSON.stringify(ev.data?.args ?? {}, null, 2)}</Code>
              {res && <Code max="max-h-96">{res.text}</Code>}
            </div>
          </Disclosure>
        </div>
      );
    }

    case "tool_result": {
      const ok = ev.data?.ok !== false;
      return (
        <button onClick={() => setOpen(!open)} className={`flex max-w-full items-center gap-1.5 pl-1.5 text-left font-mono text-[11px] ${ok ? "text-fg-2" : "text-red"}`}>
          {ok ? <Check size={12} className="shrink-0 text-green" /> : <X size={12} className="shrink-0" />}
          <span className={open ? "whitespace-pre-wrap" : "truncate"}>{ev.text}</span>
        </button>
      );
    }

    case "browser_step": {
      const shot = ev.data?.screenshot as string | undefined;
      return (
        <div className="flex gap-3 rounded-[14px] bg-fill/70 p-2">
          {shot ? (
            <button onClick={() => onShot?.(shot)} className="shrink-0 overflow-hidden rounded-[9px] ring-1 ring-sep transition-transform hover:scale-[1.03]">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={shot} alt="browser step" className="h-[58px] w-[92px] object-cover object-top" loading="lazy" />
            </button>
          ) : (
            <span className="grid h-[58px] w-[58px] shrink-0 place-items-center rounded-[9px] bg-accent/12 text-accent">
              <Globe size={18} />
            </span>
          )}
          <div className="min-w-0 flex-1 py-0.5">
            <div className="flex items-center gap-1.5 text-[11px] font-semibold text-accent">
              <Globe size={11} /> Browser
            </div>
            <div className="mt-0.5 line-clamp-2 text-[13px] leading-snug text-fg">{ev.text}</div>
            {ev.data?.url && <div className="mt-0.5 truncate font-mono text-[10.5px] text-fg-3">{ev.data.url}</div>}
          </div>
        </div>
      );
    }

    case "agent_spawned": {
      const d = ev.data ?? {};
      const color = agentColor(agents, d.agent_id);
      const task: string = d.task ?? "";
      const [head, checklist] = task.split(/\n+Checklist[^\n]*\n/);
      const items = (checklist ?? "")
        .split("\n")
        .map((l) => l.replace(/^\s*\d+\.\s*/, "").trim())
        .filter(Boolean);
      return (
        <div
          className="rounded-[16px] p-3"
          style={{ background: `color-mix(in oklab, ${color} 9%, transparent)`, boxShadow: `inset 0 0 0 0.5px color-mix(in oklab, ${color} 30%, transparent)` }}
        >
          <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-3 text-left">
            <AgentAvatar name={d.name ?? "?"} color={color} size={32} />
            <div className="min-w-0 flex-1">
              <div className="text-[11px] font-medium text-fg-3">{d.background ? "Spawned in background" : "Spawned"}</div>
              <div className="truncate text-[14px] font-semibold tracking-[-0.015em]">{d.name}</div>
            </div>
            <ChevronDown size={14} className={`shrink-0 text-fg-3 transition-transform duration-300 ${open ? "rotate-180" : ""}`} />
          </button>
          <div className="mt-2.5 flex flex-wrap gap-1">
            {(d.toolsets ?? []).map((t: string) => (
              <span key={t} className="rounded-full bg-fill px-2 py-0.5 text-[10.5px] font-medium text-fg-2">
                {t}
              </span>
            ))}
            {d.tier && d.tier !== "default" && (
              <span className="rounded-full px-2 py-0.5 text-[10.5px] font-semibold" style={{ color, background: `color-mix(in oklab, ${color} 16%, transparent)` }}>
                {d.tier}
              </span>
            )}
          </div>
          <Disclosure open={open}>
            <div className="mt-3 space-y-3 text-[12.5px] leading-relaxed">
              <div>
                <div className="eyebrow mb-1">Role</div>
                <div className="whitespace-pre-wrap text-fg-2">{d.instructions}</div>
              </div>
              <div>
                <div className="eyebrow mb-1">Task</div>
                <div className="whitespace-pre-wrap text-fg-2">{head}</div>
                {items.length > 0 && (
                  <ul className="mt-2 space-y-1.5">
                    {items.map((it, i) => (
                      <li key={i} className="flex gap-2 text-fg">
                        <span className="mt-[3px] h-3.5 w-3.5 shrink-0 rounded-full border-[1.5px]" style={{ borderColor: color }} />
                        {it}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="font-mono text-[11px] text-fg-3">{d.model}</div>
            </div>
          </Disclosure>
        </div>
      );
    }

    case "summary": {
      const st = ev.data?.agent_status as string;
      const color = st === "succeeded" ? "var(--green)" : st === "failed" ? "var(--red)" : "var(--fg-2)";
      return (
        <div
          className="mt-1 rounded-[18px] p-4"
          style={{
            background: `linear-gradient(180deg, color-mix(in oklab, ${color} 13%, transparent), color-mix(in oklab, ${color} 5%, transparent))`,
            boxShadow: `inset 0 0 0 0.5px color-mix(in oklab, ${color} 35%, transparent)`,
          }}
        >
          <div className="mb-3 flex items-center gap-2 text-[12px] font-semibold" style={{ color }}>
            <ClipboardCheck size={14} /> {ev.data?.run ? "Run summary" : "Summary"}
          </div>
          <SummaryText text={ev.text} className="text-[13px]" compact />
        </div>
      );
    }

    case "note": {
      const fromPlanner = ev.data?.from === "planner";
      return (
        <div className={`flex ${fromPlanner ? "justify-start" : "justify-end"}`}>
          <div className="max-w-[85%]">
            <div className={`mb-1 text-[10.5px] font-medium text-fg-3 ${fromPlanner ? "" : "text-right"}`}>{fromPlanner ? "Planner" : "You"}</div>
            <div
              className={`rounded-[18px] px-3.5 py-2 text-[13.5px] leading-snug ${
                fromPlanner ? "rounded-bl-[6px] bg-fill-2 text-fg" : "rounded-br-[6px] text-white"
              }`}
              style={fromPlanner ? undefined : { background: "linear-gradient(180deg, color-mix(in oklab, var(--accent) 85%, white), var(--accent))" }}
            >
              {ev.text}
            </div>
          </div>
        </div>
      );
    }

    case "human_reply": {
      const approved = /^approved/i.test(ev.text);
      const denied = /^denied/i.test(ev.text);
      return (
        <div className="flex justify-end">
          <div
            className="flex max-w-[85%] items-center gap-1.5 rounded-[18px] rounded-br-[6px] px-3.5 py-2 text-[13.5px] leading-snug text-white"
            style={{
              background: denied
                ? "linear-gradient(180deg, color-mix(in oklab, var(--red) 85%, white), var(--red))"
                : approved
                  ? "linear-gradient(180deg, color-mix(in oklab, var(--green) 85%, white), var(--green))"
                  : "linear-gradient(180deg, color-mix(in oklab, var(--accent) 85%, white), var(--accent))",
            }}
          >
            {approved && <Check size={14} strokeWidth={2.8} />}
            {denied && <X size={14} strokeWidth={2.8} />}
            {ev.text}
          </div>
        </div>
      );
    }

    case "interaction": {
      const kind = ev.data?.interaction_kind;
      const label = kind === "question" ? "Asked you" : kind === "spend" ? "Asked to spend" : "Asked for approval";
      return (
        <div className="flex gap-2.5 rounded-[14px] bg-orange/12 px-3 py-2.5">
          <Hand size={15} className="mt-0.5 shrink-0 text-orange" />
          <div className="min-w-0 text-[13px] leading-snug">
            <div className="text-[11.5px] font-semibold text-orange">{label}</div>
            <div className="mt-0.5 text-fg">{ev.text}</div>
          </div>
        </div>
      );
    }

    default: {
      if (ev.kind === "status" && ev.data?.browser_reason) {
        return (
          <div className="flex gap-2 rounded-[12px] bg-accent/8 px-2.5 py-2 text-[12px] leading-snug text-fg-2">
            <Globe size={13} className="mt-px shrink-0 text-accent" />
            <span>
              <b className="font-semibold text-fg">Using the browser.</b>{" "}
              {typeof ev.data.browser_reason === "string" ? ev.data.browser_reason : ev.text.replace(/^Using the browser:\s*/, "")}
            </span>
          </div>
        );
      }
      if (ev.kind === "error") {
        return (
          <div className="rounded-[12px] bg-red/10 px-3 py-2 text-[12.5px] text-red">
            <div className="flex items-start gap-2">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <span className="whitespace-pre-wrap">{ev.text}</span>
            </div>
            {ev.data?.traceback && (
              <>
                <button className="mt-1 text-[11px] underline" onClick={() => setOpen(!open)}>
                  {open ? "Hide details" : "Details"}
                </button>
                <Disclosure open={open}>
                  <Code>{ev.data.traceback}</Code>
                </Disclosure>
              </>
            )}
          </div>
        );
      }
      // Status: short ones are centered system capsules (like iMessage notices); long ones are rows.
      const paused = ev.data?.agent_status === "paused";
      const resumed = ev.text.startsWith("Resumed");
      const Icon = paused ? Pause : resumed ? Play : ev.data?.browser ? Globe : null;
      const tone = paused || resumed ? "text-indigo" : "text-fg-3";
      const text = ev.text.startsWith("Started · ") ? "Started" : ev.text;
      if (text.length <= 64) {
        return (
          <div className="flex justify-center py-0.5">
            <span className={`inline-flex items-center gap-1.5 rounded-full bg-fill px-2.5 py-1 text-[11px] font-medium ${tone}`}>
              {Icon && <Icon size={11} strokeWidth={2.4} />} {text}
            </span>
          </div>
        );
      }
      return (
        <div className={`flex gap-2 text-[12px] leading-snug ${tone}`}>
          {Icon && <Icon size={12} className="mt-0.5 shrink-0" />}
          <span className="whitespace-pre-wrap">{text}</span>
        </div>
      );
    }
  }
}
