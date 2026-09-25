"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "motion/react";
import { ArrowLeft, ArrowRight, Bot, Check, CircleDollarSign, Cpu, Link2, LogIn, PartyPopper, Rocket, X } from "lucide-react";
import { api, type Account, type Onboarding } from "@/lib/api";
import { softSpring, spring } from "@/lib/motion";
import { Favicon, SignInQueue, useAccounts } from "@/components/accounts";
import { LiveBrowser } from "@/components/LiveBrowser";
import { ActivityIndicator, IconTile, listSep } from "@/components/ui";
import { ClaudeSignIn } from "@/components/ClaudeCode";

const STEPS = [
  { id: "model", label: "Model" },
  { id: "integrations", label: "Integrations" },
  { id: "spending", label: "Spending" },
  { id: "accounts", label: "Accounts" },
  { id: "done", label: "Ready" },
] as const;

const PROVIDERS = [
  { id: "anthropic", label: "Anthropic", color: "#d97757", key: "ANTHROPIC_API_KEY",
    models: { planner: "anthropic/claude-sonnet-5", worker: "anthropic/claude-sonnet-5", fast: "anthropic/claude-haiku-4-5-20251001", browser: "anthropic/claude-sonnet-5" } },
  { id: "openai", label: "OpenAI", color: "#10a37f", key: "OPENAI_API_KEY",
    models: { planner: "openai/gpt-5.1", worker: "openai/gpt-5.1", fast: "openai/gpt-5.4-mini", browser: "openai/gpt-5.1" } },
  { id: "gemini", label: "Gemini", color: "#4285f4", key: "GEMINI_API_KEY",
    models: { planner: "gemini/gemini-3.1-pro-preview", worker: "gemini/gemini-3.1-pro-preview", fast: "gemini/gemini-3.5-flash", browser: "gemini/gemini-3.1-pro-preview" } },
  { id: "openrouter", label: "OpenRouter", color: "#6467f2", key: "OPENROUTER_API_KEY",
    models: { planner: "openrouter/anthropic/claude-sonnet-5", worker: "openrouter/anthropic/claude-sonnet-5", fast: "openrouter/anthropic/claude-sonnet-5", browser: "openrouter/anthropic/claude-sonnet-5" } },
  { id: "ollama", label: "Local", color: "#8e8e93", key: null,
    models: { planner: "ollama_chat/qwen3:14b", worker: "ollama_chat/qwen3:14b", fast: "ollama_chat/qwen3:14b", browser: "ollama_chat/qwen3:14b" } },
];

const SUGGESTED = ["github", "vercel", "google", "stripe", "namecheap", "x", "linkedin", "producthunt"];

export default function OnboardingPage() {
  const router = useRouter();
  const [[step, dir], setStepDir] = useState<[number, number]>([0, 1]);
  const [status, setStatus] = useState<Onboarding | null>(null);
  const refresh = useCallback(() => api<Onboarding>("/onboarding").then(setStatus).catch(() => {}), []);
  useEffect(() => {
    refresh();
  }, [refresh, step]);

  const go = (i: number) => setStepDir(([s]) => [Math.max(0, Math.min(STEPS.length - 1, i)), i > s ? 1 : -1]);
  const next = () => go(step + 1);
  const back = () => go(step - 1);
  const cur = STEPS[step].id;

  return (
    <div className={`mx-auto px-4 pt-8 pb-16 md:px-8 md:pt-12 ${cur === "accounts" ? "max-w-[1380px]" : "max-w-[760px]"}`}>
      <div className="mb-8 text-center">
        <div className="eyebrow mb-2">Setup</div>
        <h1 className="title-large">Get Todd ready</h1>
        <p className="mx-auto mt-2 max-w-md text-[15px] leading-relaxed text-fg-2">A few minutes now means your agents never stop to ask for a login later.</p>
      </div>

      {/* Stepper */}
      <div className="mb-8 flex justify-center">
        <div className="glass flex gap-1 overflow-x-auto rounded-full p-1.5 [scrollbar-width:none]">
          {STEPS.map((s, i) => {
            const done = s.id !== "done" && status?.steps[s.id as keyof Onboarding["steps"]]?.done;
            const on = i === step;
            return (
              <button key={s.id} onClick={() => go(i)} className={`relative isolate flex h-8 shrink-0 items-center gap-2 rounded-full pr-3.5 pl-1.5 text-[13px] font-medium ${on ? "text-fg" : "text-fg-2 hover:text-fg"}`}>
                {on && <motion.span layoutId="step-pill" transition={spring} className="absolute inset-0 -z-10 rounded-full bg-[var(--control-hover)] shadow-[var(--control-shadow)]" />}
                <span
                  className={`grid h-5 w-5 place-items-center rounded-full text-[10.5px] font-semibold ${done ? "bg-green text-white" : on ? "bg-accent text-white" : "bg-fill-2 text-fg-2"}`}
                >
                  {done ? <Check size={11} strokeWidth={3.2} /> : i + 1}
                </span>
                {s.label}
              </button>
            );
          })}
        </div>
      </div>

      <AnimatePresence mode="wait" custom={dir} initial={false}>
        <motion.div
          key={cur}
          custom={dir}
          variants={{
            enter: (d: number) => ({ opacity: 0, x: 40 * d }),
            center: { opacity: 1, x: 0 },
            exit: (d: number) => ({ opacity: 0, x: -40 * d }),
          }}
          initial="enter"
          animate="center"
          exit="exit"
          transition={softSpring}
        >
          {cur === "model" && <ModelStep onNext={next} />}
          {cur === "integrations" && <IntegrationsStep status={status} onNext={next} onBack={back} />}
          {cur === "spending" && <SpendingStep onNext={next} onBack={back} />}
          {cur === "accounts" && <AccountsStep onNext={next} onBack={back} />}
          {cur === "done" && (
            <Card>
              <div className="flex flex-col items-center text-center">
                <motion.span
                  initial={{ scale: 0.3, rotate: -30 }}
                  animate={{ scale: 1, rotate: 0 }}
                  transition={{ type: "spring", stiffness: 300, damping: 14 }}
                  className="grid h-16 w-16 place-items-center rounded-[20px] text-white"
                  style={{ background: "linear-gradient(150deg, #5ad97a, var(--green))", boxShadow: "0 12px 30px -10px var(--green)" }}
                >
                  <PartyPopper size={30} />
                </motion.span>
                <h2 className="title-2 mt-5">You&apos;re set</h2>
                <p className="mt-1 text-[14px] text-fg-2">You can change any of this later in Settings and Accounts.</p>
              </div>
              {status && (
                <div className="well mt-6 overflow-hidden rounded-[18px]">
                  {(["model", "integrations", "spending", "accounts"] as const).map((k) => (
                    <div key={k} className={`${listSep} flex items-center gap-3 px-4 py-3 [--inset:16px]`}>
                      <span className={`grid h-6 w-6 place-items-center rounded-full ${status.steps[k].done ? "bg-green text-white" : "bg-orange/20 text-orange"}`}>
                        {status.steps[k].done ? <Check size={13} strokeWidth={3} /> : <span className="text-[13px] font-bold">!</span>}
                      </span>
                      <span className="text-[13.5px]">
                        {k === "model" && `Model: ${status.steps.model.detail}`}
                        {k === "integrations" &&
                          `Vercel ${status.steps.integrations.vercel ? "connected" : "not connected"} · GitHub ${status.steps.integrations.github ? "connected" : "not connected"}`}
                        {k === "spending" && (status.steps.spending.registrant ? "Registrant contact saved" : "No registrant contact (domain purchases will ask)")}
                        {k === "accounts" && `${status.steps.accounts.signed_in} of ${status.steps.accounts.selected} accounts signed in`}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              <div className="mt-6 flex justify-center">
                <button
                  className="btn btn-accent btn-lg"
                  onClick={async () => {
                    await api("/onboarding/complete", { method: "POST" });
                    router.push("/");
                  }}
                >
                  <Rocket size={15} /> Start using Todd
                </button>
              </div>
            </Card>
          )}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`glass rounded-[32px] p-6 md:p-8 ${className}`}>{children}</div>;
}

function StepHead({ icon, color, title, children }: { icon: typeof Cpu; color: string; title: string; children: React.ReactNode }) {
  return (
    <div className="mb-6 flex items-start gap-4">
      <IconTile icon={icon} color={color} size={44} />
      <div>
        <h2 className="title-2">{title}</h2>
        <p className="mt-1 text-[14px] leading-relaxed text-fg-2">{children}</p>
      </div>
    </div>
  );
}

function Footer({ onNext, onBack, nextLabel = "Continue", disabled = false, extra }: { onNext: () => void; onBack?: () => void; nextLabel?: string; disabled?: boolean; extra?: React.ReactNode }) {
  return (
    <div className="mt-8 flex flex-wrap items-center gap-2">
      {onBack && (
        <button className="btn btn-glass btn-lg" onClick={onBack}>
          <ArrowLeft size={15} /> Back
        </button>
      )}
      {extra}
      <button className="btn btn-accent btn-lg ml-auto" onClick={onNext} disabled={disabled}>
        {nextLabel} <ArrowRight size={15} />
      </button>
    </div>
  );
}

function KeyInput({ name, label, hint }: { name: string; label: string; hint?: string }) {
  const [v, setV] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  useEffect(() => {
    api<{ name: string; masked: string }[]>("/secrets").then((l) => setSaved(l.find((x) => x.name === name)?.masked ?? null));
  }, [name]);
  return (
    <div>
      <label className="mb-1.5 flex items-center gap-1.5 px-0.5 text-[12.5px] font-medium text-fg-2">
        {label}
        {saved && <Check size={12} strokeWidth={3} className="text-green" />}
        {hint && <span className="truncate font-normal text-fg-3">· {hint}</span>}
      </label>
      <div className="flex gap-2">
        <input className="field h-10 font-mono text-[12.5px]" type="password" autoComplete="off" placeholder={saved ? `Saved · ${saved}` : "Paste here"} value={v} onChange={(e) => setV(e.target.value)} />
        <button
          className="btn btn-accent h-10"
          disabled={!v}
          onClick={async () => {
            await api(`/secrets/${name}`, { method: "PUT", json: { value: v } });
            setSaved(v.slice(0, 3) + "…" + v.slice(-4));
            setV("");
          }}
        >
          Save
        </button>
      </div>
    </div>
  );
}

function ModelStep({ onNext }: { onNext: () => void }) {
  const [engine, setEngine] = useState<"claude_code" | "api" | null>(null);
  useEffect(() => {
    api<{ engine: "claude_code" | "api" }>("/settings").then((s) => setEngine(s.engine ?? "claude_code"));
  }, []);
  const pick = async (e: "claude_code" | "api") => {
    setEngine(e);
    await api("/settings", { method: "PATCH", json: { engine: e } });
  };
  if (engine === null)
    return (
      <Card>
        <ActivityIndicator />
      </Card>
    );
  return (
    <Card>
      <StepHead icon={Cpu} color="var(--purple)" title="Choose what runs your agents">
        Use your Claude subscription, or bring API keys for any provider. You can switch any time in Settings → Engine.
      </StepHead>
      <div className="grid gap-2.5 sm:grid-cols-2">
        {(
          [
            ["claude_code", "Your Claude plan", "Every agent runs as Claude Code signed in with your Claude account. Usage counts toward your plan, not API credits.", "#d97757", "Recommended"],
            ["api", "API keys", "Anthropic, OpenAI, Gemini, OpenRouter, local models… billed per token by each provider.", "var(--purple)", ""],
          ] as const
        ).map(([id, title, desc, color, badge]) => {
          const on = engine === id;
          return (
            <button
              key={id}
              onClick={() => pick(id)}
              className={`well relative rounded-[20px] p-4 text-left transition-transform active:scale-[0.98] ${on ? "shadow-[inset_0_0_0_2px_var(--accent)]" : ""}`}
            >
              <div className="flex items-center gap-2.5">
                <span className="grid h-9 w-9 place-items-center rounded-[11px] text-white" style={{ background: `linear-gradient(155deg, color-mix(in oklab, ${color} 70%, white), ${color})` }}>
                  {id === "claude_code" ? <Bot size={18} /> : <Cpu size={18} />}
                </span>
                <span className="text-[14.5px] font-semibold">{title}</span>
                {badge && <span className="rounded-full bg-green/15 px-2 py-0.5 text-[10.5px] font-semibold text-green">{badge}</span>}
              </div>
              <p className="mt-2 text-[12.5px] leading-snug text-fg-2">{desc}</p>
              {on && (
                <motion.span layoutId="engine-check" transition={spring} className="absolute top-3 right-3 grid h-5 w-5 place-items-center rounded-full bg-accent text-white">
                  <Check size={11} strokeWidth={3.2} />
                </motion.span>
              )}
            </button>
          );
        })}
      </div>
      <div className="mt-6">{engine === "claude_code" ? <ClaudeSignIn /> : <ApiModelPicker />}</div>
      <Footer onNext={onNext} />
    </Card>
  );
}

function ApiModelPicker() {
  const [provider, setProvider] = useState(PROVIDERS[0]);
  const [test, setTest] = useState<{ state: "idle" | "busy" | "ok" | "err"; text?: string }>({ state: "idle" });
  const choose = async (p: (typeof PROVIDERS)[number]) => {
    setProvider(p);
    setTest({ state: "idle" });
    await api("/settings", { method: "PATCH", json: { models: p.models } });
  };
  return (
    <div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {PROVIDERS.map((p) => {
          const on = p.id === provider.id;
          return (
            <button
              key={p.id}
              onClick={() => choose(p)}
              className={`well relative flex flex-col items-center gap-2 rounded-[18px] px-2 py-3.5 transition-transform active:scale-[0.97] ${on ? "shadow-[inset_0_0_0_2px_var(--accent)]" : ""}`}
            >
              <span className="grid h-10 w-10 place-items-center rounded-[12px] text-[16px] font-bold text-white" style={{ background: `linear-gradient(155deg, color-mix(in oklab, ${p.color} 70%, white), ${p.color})` }}>
                {p.label[0]}
              </span>
              <span className="text-[12.5px] font-medium">{p.label}</span>
              {on && (
                <motion.span layoutId="prov-check" transition={spring} className="absolute top-2 right-2 grid h-5 w-5 place-items-center rounded-full bg-accent text-white">
                  <Check size={11} strokeWidth={3.2} />
                </motion.span>
              )}
            </button>
          );
        })}
      </div>
      <div className="mt-6 space-y-5">
        {provider.key ? (
          <KeyInput name={provider.key} label={`${provider.label} API key`} />
        ) : (
          <p className="well rounded-[14px] px-4 py-3 text-[13px] leading-relaxed text-fg-2">
            Run <code className="font-mono text-fg">docker compose --profile local up -d</code> and <code className="font-mono text-fg">docker compose exec ollama ollama pull qwen3:14b</code>.
          </p>
        )}
        <div className="well overflow-hidden rounded-[18px]">
          {Object.entries(provider.models).map(([tier, m]) => (
            <div key={tier} className={`${listSep} flex items-center gap-3 px-4 py-2.5 [--inset:16px]`}>
              <span className="w-16 text-[12.5px] font-medium text-fg-2 capitalize">{tier}</span>
              <span className="truncate font-mono text-[12px]">{m}</span>
            </div>
          ))}
        </div>
        <div className="flex items-center gap-3">
          <button
            className="btn btn-glass"
            onClick={async () => {
              await api("/settings", { method: "PATCH", json: { models: provider.models } });
              setTest({ state: "busy" });
              const r = await api<any>("/settings/test-model/planner", { method: "POST" }).catch((e) => ({ ok: false, error: e.message }));
              setTest(r.ok ? { state: "ok", text: `${r.model} replied “${r.reply}”` } : { state: "err", text: r.error });
            }}
          >
            {test.state === "busy" ? <ActivityIndicator size={13} /> : null} Test connection
          </button>
          {test.text && (
            <span className={`flex min-w-0 items-center gap-1.5 truncate text-[12.5px] ${test.state === "ok" ? "text-green" : "text-red"}`}>
              {test.state === "ok" ? <Check size={13} /> : <X size={13} />} {test.text}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function IntegrationsStep({ status, onNext, onBack }: { status: Onboarding | null; onNext: () => void; onBack: () => void }) {
  return (
    <Card>
      <StepHead icon={Link2} color="var(--accent)" title="Connect your APIs">
        Agents use APIs first. They&apos;re faster and more reliable than clicking through dashboards. Both are optional.
      </StepHead>
      <div className="grid gap-5">
        <KeyInput name="VERCEL_TOKEN" label="Vercel token" hint="domains, DNS, deploys" />
        <KeyInput name="GITHUB_TOKEN" label="GitHub token" hint="repo administration + contents" />
      </div>
      {status && (
        <div className="mt-5 flex gap-2">
          {(["vercel", "github"] as const).map((k) => {
            const ok = status.steps.integrations[k];
            return (
              <span key={k} className="rounded-full px-2.5 py-1 text-[12px] font-semibold capitalize" style={{ color: ok ? "var(--green)" : "var(--fg-2)", background: ok ? "color-mix(in oklab, var(--green) 14%, transparent)" : "var(--fill)" }}>
                {k} {ok ? "connected" : "not set"}
              </span>
            );
          })}
        </div>
      )}
      <Footer onNext={onNext} onBack={onBack} />
    </Card>
  );
}

function SpendingStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const [s, setS] = useState<any>(null);
  useEffect(() => {
    api("/settings").then(setS);
  }, []);
  if (!s)
    return (
      <Card>
        <ActivityIndicator />
      </Card>
    );
  const contact = s.registrant_contact;
  const set = (k: string, v: string) => setS({ ...s, registrant_contact: { ...contact, [k]: v } });
  const num = (k: string, v: string) => setS({ ...s, spend_policy: { ...s.spend_policy, [k]: Number(v.replace(/[^0-9.]/g, "") || 0) } });
  const save = async () => {
    await api("/settings", { method: "PATCH", json: { spend_policy: s.spend_policy, registrant_contact: contact } });
    onNext();
  };
  return (
    <Card>
      <StepHead icon={CircleDollarSign} color="var(--green)" title="Spending rules">
        Enforced outside the model. Anything above these asks you first, and card payments always do.
      </StepHead>
      <div className="well overflow-hidden rounded-[18px]">
        {[
          ["auto_approve_under_usd", "Auto-approve purchases under"],
          ["default_run_budget_usd", "Default budget per run"],
        ].map(([k, label]) => (
          <div key={k} className={`${listSep} flex items-center gap-3 px-4 py-3 [--inset:16px]`}>
            <span className="flex-1 text-[13.5px] font-medium">{label}</span>
            <label className="flex h-8 w-24 items-center rounded-[10px] bg-fill px-2.5 text-[13.5px]">
              <span className="text-fg-3">$</span>
              <input className="w-full bg-transparent text-right font-medium outline-none tabular" inputMode="decimal" value={s.spend_policy[k]} onChange={(e) => num(k, e.target.value)} />
            </label>
          </div>
        ))}
      </div>
      <div className="mt-7 mb-3 px-0.5">
        <div className="title-3">Domain registrant contact</div>
        <div className="text-[12.5px] text-fg-2">Registrars require it for purchases. Optional for now.</div>
      </div>
      <div className="grid gap-2.5 md:grid-cols-2">
        {[
          ["firstName", "First name"],
          ["lastName", "Last name"],
          ["email", "Email"],
          ["phone", "Phone (+1.5555555555)"],
          ["address1", "Address"],
          ["city", "City"],
          ["state", "State"],
          ["zip", "ZIP"],
        ].map(([k, label]) => (
          <input key={k} className="field h-10" placeholder={label} value={contact[k] ?? ""} onChange={(e) => set(k, e.target.value)} />
        ))}
      </div>
      <p className="mt-4 text-[12px] text-fg-3">Add a payment card for browser checkouts later in Settings → Payment card. Use a virtual card with a limit.</p>
      <Footer onNext={save} onBack={onBack} nextLabel="Save & continue" />
    </Card>
  );
}

function AccountsStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const { data, reload } = useAccounts();
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const [queue, setQueue] = useState<Account[] | null>(null);
  const accounts = data?.accounts ?? [];

  useEffect(() => {
    if (!data || picked) return;
    const already = data.accounts.filter((a) => a.selected).map((a) => a.id);
    setPicked(new Set(already.length ? already : SUGGESTED));
  }, [data, picked]);

  const groups = useMemo(() => {
    const g: Record<string, Account[]> = {};
    for (const a of accounts) (g[a.category] ??= []).push(a);
    return g;
  }, [accounts]);

  if (!data || !picked)
    return (
      <Card>
        <ActivityIndicator />
      </Card>
    );
  const toggle = (id: string) => {
    const n = new Set(picked);
    n.has(id) ? n.delete(id) : n.add(id);
    setPicked(n);
  };
  const start = async () => {
    await api("/accounts-selection", { method: "PUT", json: { ids: [...picked] } });
    const fresh = await api<{ accounts: Account[] }>("/accounts");
    setQueue(fresh.accounts.filter((a) => picked.has(a.id)));
    reload();
  };

  return (
    <div className="flex flex-col gap-5 xl:flex-row">
      <Card className="min-w-0 flex-1">
        <StepHead icon={LogIn} color="var(--pink)" title="Sign in once">
          Pick the accounts your agents should use, then sign in to each in the agents&apos; browser. Todd notices each sign-in and moves on.
          {!data.browser_online && <span className="text-red"> The browser is offline. Start the stack first.</span>}
        </StepHead>
        {queue ? (
          <div>
            <SignInQueue
              accounts={queue}
              onChange={reload}
              onDone={() => {
                reload();
                onNext();
              }}
            />
            <button className="btn btn-plain mt-3" onClick={() => setQueue(null)}>
              Change selection
            </button>
          </div>
        ) : (
          <>
            <div className="space-y-5">
              {Object.entries(groups).map(([cat, list]) => (
                <section key={cat}>
                  <h3 className="eyebrow mb-2">{cat}</h3>
                  <div className="flex flex-wrap gap-1.5">
                    {list.map((a) => {
                      const on = picked.has(a.id);
                      return (
                        <motion.button
                          key={a.id}
                          whileTap={{ scale: 0.95 }}
                          onClick={() => toggle(a.id)}
                          className={`flex h-9 items-center gap-2 rounded-full pr-3 pl-1.5 text-[13px] font-medium transition-colors ${
                            on ? "bg-accent text-white shadow-[0_4px_14px_-6px_var(--accent)]" : "bg-fill text-fg hover:bg-fill-2"
                          }`}
                        >
                          <Favicon a={a} size={24} />
                          {a.name}
                          {a.status === "signed_in" && <Check size={12} strokeWidth={3} className={on ? "text-white" : "text-green"} />}
                        </motion.button>
                      );
                    })}
                  </div>
                </section>
              ))}
            </div>
            <Footer
              onBack={onBack}
              onNext={start}
              disabled={!picked.size}
              nextLabel={`Sign in to ${picked.size} selected`}
              extra={
                <button className="btn btn-plain btn-lg" onClick={onNext}>
                  Skip for now
                </button>
              }
            />
          </>
        )}
      </Card>
      <aside className="xl:sticky xl:top-6 xl:h-fit xl:w-[560px]">
        <LiveBrowser defaultControl footer="Sign in here. Agents reuse these sessions; they must ask before posting or sending anything." />
      </aside>
    </div>
  );
}
