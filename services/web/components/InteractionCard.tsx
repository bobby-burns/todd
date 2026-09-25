"use client";

import { useState } from "react";
import { motion } from "motion/react";
import { Check, CreditCard, MessageCircleQuestion, ShieldCheck, X } from "lucide-react";
import { api, usd, type Interaction } from "@/lib/api";
import { softSpring } from "@/lib/motion";
import { AgentAvatar, IconTile } from "./ui";

const KIND = {
  spend: { label: "Spend request", icon: CreditCard, color: "var(--green)" },
  question: { label: "Question", icon: MessageCircleQuestion, color: "var(--accent)" },
  approval: { label: "Approval needed", icon: ShieldCheck, color: "var(--orange)" },
} as const;

export function InteractionCard({
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
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const k = KIND[it.kind];

  async function send(decision: "approve" | "deny" | null) {
    setBusy(true);
    setErr(null);
    try {
      await api(`/interactions/${it.id}`, { method: "POST", json: { decision, answer: answer || null } });
      onDone();
    } catch (e: any) {
      setErr(e.message);
      setBusy(false);
    }
  }

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 16, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.18 } }}
      transition={softSpring}
      className="glass glass-strong overflow-hidden rounded-[28px] p-5"
      style={{
        background: `radial-gradient(100% 55% at 50% 0%, color-mix(in oklab, ${k.color} 16%, transparent), transparent 75%), var(--glass-tint-strong)`,
      }}
    >
      <div className="flex items-center gap-3">
        <IconTile icon={k.icon} color={k.color} size={34} />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold tracking-[-0.01em]">{k.label}</div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[12px] text-fg-2">
            from <AgentAvatar name={agentName} color={agentColor} planner={planner} size={16} /> <span className="truncate font-medium text-fg">{agentName}</span>
          </div>
        </div>
      </div>

      {it.kind === "spend" ? (
        <div className="mt-5 text-center">
          <div className="font-display text-[44px] leading-none font-bold tracking-[-0.035em] tabular">{usd(it.data.amount_usd)}</div>
          <div className="mt-2 text-[14px] font-medium">{it.data.merchant}</div>
          <div className="mx-auto mt-1 max-w-md text-[13px] leading-snug text-fg-2">{it.data.description ?? it.prompt}</div>
          <div className="mt-3 flex flex-wrap justify-center gap-1.5">
            {it.data.method && <Chip>{it.data.method}</Chip>}
            {it.data.why_asking && <Chip>{it.data.why_asking}</Chip>}
          </div>
        </div>
      ) : (
        <p className="mt-4 text-[15px] leading-snug font-medium tracking-[-0.01em] whitespace-pre-wrap">{it.prompt}</p>
      )}

      {it.data?.details && (
        <div className="well mt-4 rounded-[14px] px-3.5 py-3 text-[12.5px] leading-relaxed whitespace-pre-wrap text-fg-2">{it.data.details}</div>
      )}

      <div className="mt-4 flex flex-col gap-2.5 sm:flex-row sm:items-center">
        <input
          className="field h-10 flex-1 rounded-full px-4"
          placeholder={it.kind === "question" ? "Your answer…" : "Add a note for the agent (optional)"}
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && it.kind === "question" && answer.trim()) send(null);
          }}
        />
        {it.kind === "question" ? (
          <button className="btn btn-accent btn-lg" disabled={busy || !answer.trim()} onClick={() => send(null)}>
            Send
          </button>
        ) : (
          <div className="flex gap-2">
            <button className="btn btn-glass btn-lg flex-1" disabled={busy} onClick={() => send("deny")}>
              <X size={15} strokeWidth={2.5} /> Deny
            </button>
            <button className={`btn btn-lg flex-1 ${it.kind === "spend" ? "btn-success" : "btn-accent"}`} disabled={busy} onClick={() => send("approve")}>
              <Check size={15} strokeWidth={2.8} /> {it.kind === "spend" ? `Pay ${usd(it.data.amount_usd)}` : "Approve"}
            </button>
          </div>
        )}
      </div>
      {err && <p className="mt-2 text-[12px] text-red">{err}</p>}
    </motion.div>
  );
}

const Chip = ({ children }: { children: React.ReactNode }) => (
  <span className="rounded-full bg-fill px-2.5 py-1 text-[11.5px] font-medium text-fg-2">{children}</span>
);
