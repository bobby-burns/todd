"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "motion/react";
import { ArrowUp, Check, ChevronRight, Globe, Hand, Rocket, ShoppingBag, Wrench, X, Zap, type LucideIcon } from "lucide-react";
import { api, timeAgo, usd, type Onboarding, type Run } from "@/lib/api";
import { fadeUp, softSpring, stagger } from "@/lib/motion";
import { ActivityIndicator, Empty, PageHeader, ProgressRing, StatusPill, listRow, listSep } from "@/components/ui";

const TEMPLATES: { icon: LucideIcon; label: string; color: string; text: string }[] = [
  {
    icon: Rocket,
    label: "Launch a website",
    color: "var(--accent)",
    text:
      "Build and launch a website for <idea>.\n\n" +
      "- Domain: <domain.com> (buy it if available and under $20)\n" +
      "- Stack: Next.js + Tailwind, Firebase for auth + Firestore\n" +
      "- Create a GitHub repo, a Vercel project connected to it, attach the domain\n" +
      "- Set up the Firebase project and web app, put its config in Vercel env vars\n" +
      "- Pages: <landing, pricing, sign in, dashboard>\n" +
      "Finish with the live URL.",
  },
  {
    icon: Globe,
    label: "Check domains",
    color: "var(--teal)",
    text: "Check availability and price for these domains and recommend the best one: <a.com>, <b.app>, <c.io>",
  },
  { icon: Wrench, label: "Fix a repo", color: "var(--orange)", text: "Clone <owner/repo>, get the build passing, fix <issue>, and push a branch named todd/fix." },
  {
    icon: ShoppingBag,
    label: "Buy something",
    color: "var(--pink)",
    text: "Buy <item> from <site.com> for at most $<amount>. Ask me before checkout if anything looks off.",
  },
];

const STATUS_ICON: Record<string, { icon: LucideIcon | null; color: string }> = {
  running: { icon: null, color: "var(--accent)" },
  queued: { icon: null, color: "var(--fg-3)" },
  waiting: { icon: Hand, color: "var(--orange)" },
  succeeded: { icon: Check, color: "var(--green)" },
  failed: { icon: X, color: "var(--red)" },
  cancelled: { icon: X, color: "var(--fg-3)" },
  interrupted: { icon: Hand, color: "var(--orange)" },
};

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

export default function Home() {
  const router = useRouter();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [prompt, setPrompt] = useState("");
  const [budget, setBudget] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [setup, setSetup] = useState<Onboarding | null>(null);
  const [hello, setHello] = useState("");

  useEffect(() => {
    setHello(greeting());
    api<Onboarding>("/onboarding").then(setSetup).catch(() => {});
  }, []);

  useEffect(() => {
    let alive = true;
    const load = () => api<Run[]>("/runs").then((r) => alive && setRuns(r)).catch((e) => alive && setErr(String(e.message)));
    load();
    const t = setInterval(load, 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  async function start() {
    if (!prompt.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const run = await api<Run>("/runs", { method: "POST", json: { prompt, budget_usd: budget ? Number(budget) : undefined } });
      router.push(`/runs/${run.id}`);
    } catch (e: any) {
      setErr(e.message);
      setBusy(false);
    }
  }

  const waiting = runs?.filter((r) => r.pending_interactions > 0) ?? [];
  const activeCount = runs?.filter((r) => r.active).length ?? 0;
  const spent = runs?.reduce((s, r) => s + r.spent_usd, 0) ?? 0;
  const acc = setup?.steps.accounts;

  return (
    <div className="mx-auto max-w-[920px] px-4 md:px-8">
      <PageHeader eyebrow={hello || " "} title="What should Todd do?" subtitle="Describe the outcome. The planner designs the agents it needs, runs them in parallel, and checks with you before spending money or posting." />

      {setup && !setup.completed && (
        <motion.div initial="hidden" animate="show" variants={fadeUp}>
          <Link href="/onboarding" className="glass mb-5 flex items-center gap-4 rounded-[24px] p-4 transition-transform active:scale-[0.99]">
            <ProgressRing value={acc && acc.selected ? acc.signed_in / acc.selected : 0} size={40} color="var(--accent)" />
            <div className="min-w-0 flex-1">
              <div className="text-[14.5px] font-semibold tracking-[-0.01em]">Finish setting up Todd</div>
              <div className="text-[12.5px] leading-snug text-fg-2">Sign in to your accounts once, so agents never stop for a login.</div>
            </div>
            <span className="btn btn-tinted">Continue</span>
          </Link>
        </motion.div>
      )}

      {/* Composer */}
      <motion.div
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ ...softSpring, delay: 0.04 }}
        className="glass rounded-[30px] p-2 transition-shadow focus-within:shadow-[var(--glass-shadow),0_0_0_4px_color-mix(in_oklab,var(--accent)_22%,transparent)]"
      >
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) start();
          }}
          rows={5}
          placeholder="Launch a waitlist site for my hockey pickup app at dropin.hockey with Firebase auth…"
          className="scrollbar-thin block w-full resize-none bg-transparent px-4 pt-3.5 pb-2 text-[16px] leading-relaxed tracking-[-0.011em] outline-none placeholder:text-fg-3"
        />
        <div className="flex flex-wrap items-center gap-2 px-2 pb-1.5">
          <div className="-mx-1 flex min-w-0 flex-1 gap-1.5 overflow-x-auto px-1 py-0.5 [scrollbar-width:none] max-sm:basis-full">
            {TEMPLATES.map(({ icon: Icon, label, text, color }) => (
              <button key={label} className="btn btn-glass btn-sm shrink-0" onClick={() => setPrompt(text)}>
                <Icon size={13} style={{ color }} strokeWidth={2.2} /> {label}
              </button>
            ))}
          </div>
          <label className="flex h-8 items-center gap-1 rounded-full bg-fill pr-1 pl-3 text-[12.5px] text-fg-2 max-sm:ml-auto">
            Budget $
            <input
              value={budget}
              onChange={(e) => setBudget(e.target.value.replace(/[^0-9.]/g, ""))}
              placeholder="auto"
              className="w-12 bg-transparent font-medium text-fg outline-none placeholder:text-fg-3"
            />
          </label>
          <motion.button
            whileTap={{ scale: 0.9 }}
            className="grid h-10 w-10 place-items-center rounded-full bg-accent text-white shadow-[0_6px_18px_-6px_var(--accent)] transition-opacity disabled:opacity-30 disabled:shadow-none"
            disabled={busy || !prompt.trim()}
            onClick={start}
            title="Run (⌘/Ctrl + Enter)"
          >
            {busy ? <ActivityIndicator size={16} /> : <ArrowUp size={19} strokeWidth={2.6} />}
          </motion.button>
        </div>
      </motion.div>
      {err && <p className="mt-3 px-2 text-[13px] text-red">{err}</p>}

      {/* Stats */}
      {runs && runs.length > 0 && (
        <motion.div initial="hidden" animate="show" variants={stagger(0.05, 0.1)} className="mt-6 grid grid-cols-3 gap-3">
          {[
            { label: "Running", value: String(activeCount), color: "var(--accent)" },
            { label: "Needs you", value: String(waiting.length), color: waiting.length ? "var(--orange)" : undefined },
            { label: "Spent", value: usd(spent), color: undefined },
          ].map((s) => (
            <motion.div key={s.label} variants={fadeUp} className="glass rounded-[22px] px-4 py-3.5">
              <div className="text-[12px] font-medium text-fg-2">{s.label}</div>
              <div className="mt-1 font-display text-[26px] leading-none font-semibold tracking-[-0.03em] tabular" style={{ color: s.color }}>
                {s.value}
              </div>
            </motion.div>
          ))}
        </motion.div>
      )}

      {/* Waiting on you */}
      <AnimatePresence>
        {waiting.length > 0 && (
          <motion.section initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={softSpring} className="mt-8">
            <h2 className="title-3 mb-3 px-1">Waiting on you</h2>
            <div className="glass overflow-hidden rounded-[24px]">
              {waiting.map((r) => (
                <Link key={r.id} href={`/runs/${r.id}`} className={`${listRow} hover:bg-fill`}>
                  <span className="grid h-9 w-9 shrink-0 place-items-center rounded-[11px] bg-orange text-white">
                    <Hand size={17} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[14px] font-semibold tracking-[-0.01em]">{r.title}</div>
                    <div className="text-[12px] text-orange">
                      {r.pending_interactions} request{r.pending_interactions === 1 ? "" : "s"} waiting
                    </div>
                  </div>
                  <span className="btn btn-sm btn-tinted">Review</span>
                </Link>
              ))}
            </div>
          </motion.section>
        )}
      </AnimatePresence>

      {/* Runs */}
      <section className="mt-8 mb-12">
        <h2 className="title-3 mb-3 px-1">Runs</h2>
        {runs === null ? (
          <div className="grid h-32 place-items-center text-fg-3">
            <ActivityIndicator size={20} />
          </div>
        ) : runs.length === 0 ? (
          <div className="glass rounded-[24px]">
            <Empty icon={Zap} title="No runs yet">
              Add a model key in Settings, then describe what you want done above.
            </Empty>
          </div>
        ) : (
          <motion.div initial="hidden" animate="show" variants={stagger(0.03, 0.12)} className="glass overflow-hidden rounded-[24px]">
            {runs.map((r) => {
              const si = STATUS_ICON[r.status] ?? STATUS_ICON.queued;
              return (
                <motion.div key={r.id} variants={fadeUp} className={listSep}>
                  <Link href={`/runs/${r.id}`} className="flex items-center gap-3 px-4 py-3.5 transition-colors hover:bg-fill">
                    <span
                      className="grid h-9 w-9 shrink-0 place-items-center rounded-[11px]"
                      style={{ background: `color-mix(in oklab, ${si.color} 16%, transparent)`, color: si.color }}
                    >
                      {si.icon ? <si.icon size={17} strokeWidth={2.4} /> : <ActivityIndicator size={15} />}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-[14px] font-semibold tracking-[-0.01em]">{r.title}</div>
                      <div className="mt-0.5 truncate text-[12px] text-fg-2 tabular">
                        {timeAgo(r.created_at)} · {usd(r.spent_usd)} of {usd(r.budget_usd, 0)} ·{" "}
                        {r.engine === "claude_code" ? "Claude plan" : `model ${usd(r.llm_cost_usd, 3)}`}
                      </div>
                    </div>
                    <StatusPill status={r.status} live={r.active} className="max-sm:hidden" />
                    <ChevronRight size={16} className="shrink-0 text-fg-3" />
                  </Link>
                </motion.div>
              );
            })}
          </motion.div>
        )}
      </section>
    </div>
  );
}
