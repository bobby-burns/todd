"use client";

import { useState } from "react";
import { motion } from "motion/react";
import { Globe, Link2, Lock, Rocket, ShoppingCart, Unlock } from "lucide-react";
import { api, type Interaction } from "@/lib/api";
import { softSpring } from "@/lib/motion";
import { AgentAvatar, IconTile, Segmented } from "./ui";

/* The launch plan, asked when a run that builds a website starts (agents keep building meanwhile): where it will
   live, whether the code is private, and whether Todd asks before deploying. Todd enforces the answer: deploys
   (previews too) and domains wait for you unless you pick "When it's ready", and a public repo needs "Public". */

type Domain = "own" | "buy" | "free";

const PLACES: { value: Domain; icon: typeof Globe; title: string; hint: string }[] = [
  { value: "own", icon: Globe, title: "My own domain", hint: "Todd connects it where you bought it (Squarespace, Namecheap, GoDaddy…)." },
  { value: "buy", icon: ShoppingCart, title: "Buy a new domain", hint: "Todd checks a few names and prices; you pick one and approve the purchase." },
  { value: "free", icon: Link2, title: "A free address for now", hint: "Like your-site.vercel.app. You can add a domain any time." },
];

export function LaunchPlanCard({
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
  const [domain, setDomain] = useState<Domain>("free");
  const [name, setName] = useState("");
  const [repo, setRepo] = useState<"private" | "public">("private");
  const [live, setLive] = useState<"ask" | "auto">("ask");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const needsName = domain === "own" && !name.trim();

  async function send(plan: Record<string, string>) {
    setBusy(true);
    setErr(null);
    try {
      await api(`/interactions/${it.id}`, { method: "POST", json: { decision: null, answer: JSON.stringify(plan) } });
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
      className="glass glass-strong @container overflow-hidden rounded-[28px] p-5"
      style={{ background: "radial-gradient(100% 55% at 50% 0%, color-mix(in oklab, var(--indigo) 16%, transparent), transparent 75%), var(--glass-tint-strong)" }}
    >
      <div className="flex items-center gap-3">
        <IconTile icon={Rocket} color="var(--indigo)" size={34} />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold tracking-[-0.01em]">Launch plan</div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[12px] text-fg-2">
            from <AgentAvatar name={agentName} color={agentColor} planner={planner} size={16} /> <span className="truncate font-medium text-fg">{agentName}</span>
          </div>
        </div>
      </div>
      <p className="mt-4 text-[15px] leading-snug font-medium tracking-[-0.01em]">{it.prompt}</p>
      <p className="mt-1 text-[12.5px] leading-snug text-fg-2">
        Agents are already building. Nothing is deployed or made public until you answer here or approve it.
      </p>

      <h5 className="mt-4 mb-2 text-[12.5px] font-semibold text-fg-2">Where should it live?</h5>
      <div className="grid gap-2 @2xl:grid-cols-3">
        {PLACES.map((p) => {
          const on = domain === p.value;
          return (
            <button
              key={p.value}
              type="button"
              onClick={() => setDomain(p.value)}
              aria-pressed={on}
              className={`flex flex-col items-start gap-0.5 rounded-[16px] px-3 py-2.5 text-left ring-1 transition-colors ${
                on ? "bg-accent/12 ring-accent/60" : "bg-fill/50 ring-sep hover:ring-accent/30"
              }`}
            >
              <span className="flex items-center gap-1.5 text-[13px] font-semibold">
                <p.icon size={14} className={on ? "text-accent" : "text-fg-2"} /> {p.title}
              </span>
              <span className="text-[11.5px] leading-snug text-fg-2">{p.hint}</span>
            </button>
          );
        })}
      </div>
      {domain !== "free" && (
        <input
          className="field mt-2 h-10 w-full rounded-full px-4"
          placeholder={domain === "own" ? "yourdomain.com" : "Name ideas, if you have any (optional)"}
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoFocus
        />
      )}

      <div className="mt-4 grid gap-3 @md:grid-cols-2">
        <div>
          <h5 className="mb-1.5 text-[12.5px] font-semibold text-fg-2">The code on GitHub</h5>
          <Segmented
            id={`repo-${it.id}`}
            value={repo}
            onChange={setRepo}
            options={[
              { value: "private", label: "Private", icon: Lock, title: "Only you can see it (recommended)" },
              { value: "public", label: "Public", icon: Unlock, title: "Anyone can see the code" },
            ]}
          />
        </div>
        <div>
          <h5 className="mb-1.5 text-[12.5px] font-semibold text-fg-2">Going live</h5>
          <Segmented
            id={`live-${it.id}`}
            value={live}
            onChange={setLive}
            options={[
              { value: "ask", label: "Ask me first", title: "Todd asks before deploying anything, previews included (recommended)" },
              { value: "auto", label: "When it's ready", title: "Todd deploys it once it's built and checked" },
            ]}
          />
        </div>
      </div>

      <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <button className="btn btn-glass btn-lg" disabled={busy} onClick={() => send({ domain: "free", repo: "private", live: "ask", note: "decide later" })}>
          Decide later
        </button>
        <button
          className="btn btn-accent btn-lg"
          disabled={busy || needsName}
          title={needsName ? "Type your domain first" : undefined}
          onClick={() => send({ domain, domain_name: name.trim(), repo, live })}
        >
          Save plan
        </button>
      </div>
      {err && <p className="mt-2 text-[12px] text-red">{err}</p>}
    </motion.div>
  );
}
