"use client";

import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import {
  Blocks,
  Bot,
  Check,
  ChevronRight,
  CircleDollarSign,
  Cpu,
  CreditCard,
  Globe,
  Link2,
  Lock,
  Plus,
  RefreshCw,
  ScrollText,
  SunMoon,
  Trash2,
  X,
  type LucideIcon,
} from "lucide-react";
import { api, type ToolInfo, type ToolsetInfo } from "@/lib/api";
import { spring } from "@/lib/motion";
import { ActivityIndicator, IconTile, PageHeader, Section, Segmented, Switch, Toast, listSep } from "@/components/ui";
import { ThemeSwitch } from "@/components/Nav";
import { ClaudeSignIn } from "@/components/ClaudeCode";

type Secret = { name: string; masked: string; updated_at: string };
type Settings = {
  engine: "claude_code" | "api";
  claude_code: { models: Record<"planner" | "worker" | "fast", string> };
  models: Record<"planner" | "worker" | "fast" | "browser", string>;
  thinking: { enabled: boolean; effort: string };
  limits: { max_concurrent_agents: number; max_agents_per_run: number };
  ollama_base_url: string;
  openai_compatible_base_url: string;
  spend_policy: { auto_approve_under_usd: number; default_run_budget_usd: number; always_ask: boolean };
  integrations: { vercel_team_id: string; github_owner: string };
  registrant_contact: Record<string, string>;
  mcp_servers: Record<string, unknown>;
};
type PromptInfo = { content: string; source: string; default: string };

const MODEL_SUGGESTIONS = [
  "anthropic/claude-sonnet-5",
  "anthropic/claude-opus-5-5",
  "anthropic/claude-haiku-4-5-20251001",
  "openai/gpt-5.1",
  "openai/gpt-5.4-mini",
  "gemini/gemini-3.1-pro-preview",
  "gemini/gemini-3.5-flash",
  "openrouter/anthropic/claude-sonnet-5",
  "ollama_chat/qwen3:14b",
];
const PROVIDER_KEYS = [
  ["ANTHROPIC_API_KEY", "Anthropic"],
  ["OPENAI_API_KEY", "OpenAI"],
  ["GEMINI_API_KEY", "Google Gemini"],
  ["OPENROUTER_API_KEY", "OpenRouter"],
  ["GROQ_API_KEY", "Groq"],
  ["DEEPSEEK_API_KEY", "DeepSeek"],
  ["XAI_API_KEY", "xAI"],
] as const;
const TIERS = {
  planner: "Plans the run and designs agents",
  worker: "Default for spawned agents",
  fast: "Quick, simple work",
  browser: "Drives the browser",
} as const;
const NAV: [string, string, LucideIcon, string][] = [
  ["engine", "Engine", Bot, "#d97757"],
  ["models", "API models", Cpu, "var(--purple)"],
  ["integrations", "Integrations", Link2, "var(--accent)"],
  ["spend", "Spend policy", CircleDollarSign, "var(--green)"],
  ["registrant", "Domain registrant", Globe, "var(--orange)"],
  ["card", "Payment card", CreditCard, "#8e8e93"],
  ["prompts", "Prompts", ScrollText, "var(--pink)"],
  ["tools", "Tools & MCP", Blocks, "var(--teal)"],
  ["vault", "Vault", Lock, "var(--indigo)"],
  ["appearance", "Appearance", SunMoon, "#0a84ff"],
];
const sec = (id: string) => NAV.find((n) => n[0] === id)!;

export default function SettingsPage() {
  const [s, setS] = useState<Settings | null>(null);
  const [secrets, setSecrets] = useState<Secret[]>([]);
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    api<Settings>("/settings").then(setS).catch((e) => setErr(e.message));
    api<Secret[]>("/secrets").then(setSecrets).catch(() => {});
  }, []);
  useEffect(load, [load]);

  const flash = (msg: string) => {
    setSaved(msg);
    setTimeout(() => setSaved(null), 1800);
  };

  async function patch(p: Partial<Settings>, msg = "Saved") {
    setErr(null);
    try {
      setS(await api<Settings>("/settings", { method: "PATCH", json: p }));
      flash(msg);
    } catch (e: any) {
      setErr(e.message);
    }
  }

  if (!s) {
    return <div className="grid h-[60vh] place-items-center text-fg-3">{err ?? <ActivityIndicator size={22} />}</div>;
  }
  const secretMap = Object.fromEntries(secrets.map((x) => [x.name, x]));
  const S = (id: string, title: string | undefined, desc: React.ReactNode, children: React.ReactNode) => {
    const [, label, icon, color] = sec(id);
    return (
      <Section id={id} title={title ?? label} icon={icon} color={color} desc={desc}>
        {children}
      </Section>
    );
  };

  return (
    <div className="mx-auto max-w-[1180px] px-4 md:px-8">
      <PageHeader title="Settings" subtitle="Models, keys, spending rules, prompts and tools. Everything is stored locally and encrypted." />
      <Toast message={saved} />

      <div className="flex gap-8 pb-16">
        <nav className="sticky top-6 hidden h-fit w-60 shrink-0 lg:block">
          <div className="glass overflow-hidden rounded-[22px] py-1">
            {NAV.map(([id, label, icon, color]) => (
              <a key={id} href={`#${id}`} className={`${listSep} flex items-center gap-3 px-3 py-2 [--inset:52px] hover:bg-fill`}>
                <IconTile icon={icon} color={color} size={28} />
                <span className="flex-1 text-[13.5px] font-medium">{label}</span>
                <ChevronRight size={14} className="text-fg-3" />
              </a>
            ))}
          </div>
        </nav>

        <div className="min-w-0 flex-1 space-y-10">
          {err && <p className="text-[13px] text-red">{err}</p>}

          {S(
            "engine",
            undefined,
            "What runs the planner and agents. Your Claude plan: every agent is a Claude Code session signed in with your Claude account, so usage counts toward your plan, not API credits. API keys: any provider through LiteLLM, billed per token.",
            <EngineSection s={s} patch={patch} />,
          )}

          {S(
            "models",
            undefined,
            s.engine === "claude_code"
              ? "Used only when Engine is set to API keys. Any LiteLLM model string works, cloud or local."
              : "Any LiteLLM model string works, cloud or local. The planner picks a tier for each agent it spawns.",
            <>
              <datalist id="models">
                {MODEL_SUGGESTIONS.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              <div className="grid gap-3 md:grid-cols-2">
                {(["planner", "worker", "fast", "browser"] as const).map((role) => (
                  <ModelField key={role} role={role} hint={TIERS[role]} value={s.models[role]} onSave={(v) => patch({ models: { ...s.models, [role]: v } })} />
                ))}
              </div>
              <Group className="mt-4">
                <Row label="Extended reasoning" hint="Adds the model's native thinking where supported. Costs more tokens.">
                  <Segmented
                    id="effort"
                    size="sm"
                    value={s.thinking.effort}
                    onChange={(v) => patch({ thinking: { ...s.thinking, effort: v } })}
                    options={["low", "medium", "high"].map((x) => ({ value: x, label: x[0].toUpperCase() + x.slice(1) }))}
                  />
                  <Switch checked={s.thinking.enabled} onChange={(v) => patch({ thinking: { ...s.thinking, enabled: v } })} />
                </Row>
                <Row label="Agents running at once">
                  <NumberField value={s.limits.max_concurrent_agents} onSave={(v) => patch({ limits: { ...s.limits, max_concurrent_agents: v } })} />
                </Row>
                <Row label="Agents per run">
                  <NumberField value={s.limits.max_agents_per_run} onSave={(v) => patch({ limits: { ...s.limits, max_agents_per_run: v } })} />
                </Row>
              </Group>
              <div className="eyebrow mt-7 mb-2.5">Provider keys</div>
              <div className="grid gap-3 md:grid-cols-2">
                {PROVIDER_KEYS.map(([name, label]) => (
                  <SecretField key={name} name={name} label={label} existing={secretMap[name]} onChange={load} />
                ))}
              </div>
              <div className="mt-6 grid gap-4 md:grid-cols-2">
                <TextSetting label="Ollama base URL" value={s.ollama_base_url} onSave={(v) => patch({ ollama_base_url: v })} />
                <TextSetting
                  label="OpenAI-compatible base URL"
                  value={s.openai_compatible_base_url}
                  placeholder="http://host.docker.internal:1234/v1"
                  onSave={(v) => patch({ openai_compatible_base_url: v })}
                />
              </div>
            </>,
          )}

          {S(
            "integrations",
            undefined,
            "Agents use these APIs before touching the browser. Tokens are encrypted and never shown to the model.",
            <>
              <div className="grid gap-3 md:grid-cols-2">
                <SecretField name="VERCEL_TOKEN" label="Vercel token" existing={secretMap.VERCEL_TOKEN} onChange={load} hint="vercel.com/account/tokens" />
                <TextSetting label="Vercel team ID" value={s.integrations.vercel_team_id} placeholder="optional" onSave={(v) => patch({ integrations: { ...s.integrations, vercel_team_id: v } })} />
                <SecretField name="GITHUB_TOKEN" label="GitHub token" existing={secretMap.GITHUB_TOKEN} onChange={load} hint="repo admin + contents" />
                <TextSetting label="GitHub owner / org" value={s.integrations.github_owner} placeholder="optional" onSave={(v) => patch({ integrations: { ...s.integrations, github_owner: v } })} />
              </div>
              <IntegrationCheck />
            </>,
          )}

          {S(
            "spend",
            undefined,
            "Enforced outside the model. Anything above the limit or over a run's budget pauses for your approval.",
            <Group>
              <Row label="Auto-approve under">
                <NumberField prefix="$" value={s.spend_policy.auto_approve_under_usd} onSave={(v) => patch({ spend_policy: { ...s.spend_policy, auto_approve_under_usd: v } })} />
              </Row>
              <Row label="Default budget per run">
                <NumberField prefix="$" value={s.spend_policy.default_run_budget_usd} onSave={(v) => patch({ spend_policy: { ...s.spend_policy, default_run_budget_usd: v } })} />
              </Row>
              <Row label="Always ask before any spend">
                <Switch checked={s.spend_policy.always_ask} onChange={(v) => patch({ spend_policy: { ...s.spend_policy, always_ask: v } })} />
              </Row>
            </Group>,
          )}

          {S("registrant", undefined, "Registrars need contact details for domain purchases.", <RegistrantForm value={s.registrant_contact} onSave={(v) => patch({ registrant_contact: v }, "Contact saved")} />)}

          {S(
            "card",
            undefined,
            "Used only by the browser for approved checkouts, as masked placeholders limited to the approved merchant's domains. Use a virtual card with a limit.",
            <div className="grid gap-3 md:grid-cols-2">
              <SecretField name="CARD_NUMBER" label="Card number" existing={secretMap.CARD_NUMBER} onChange={load} />
              <SecretField name="CARD_EXP" label="Expiry (MM/YY)" existing={secretMap.CARD_EXP} onChange={load} />
              <SecretField name="CARD_CVC" label="CVC" existing={secretMap.CARD_CVC} onChange={load} />
              <SecretField name="CARD_NAME" label="Name on card" existing={secretMap.CARD_NAME} onChange={load} />
              <SecretField name="CARD_ZIP" label="Billing ZIP" existing={secretMap.CARD_ZIP} onChange={load} />
            </div>,
          )}

          {S(
            "prompts",
            undefined,
            <>
              The harness prompt is prepended to every agent. Priority: dashboard → <code className="font-mono text-[12px]">./prompts/&lt;name&gt;.md</code> → built-in.
            </>,
            <PromptsEditor onSaved={() => flash("Prompt saved")} />,
          )}

          {S(
            "tools",
            undefined,
            "Toolsets are what the planner hands to the agents it spawns. Each plugin file in ./plugins and each MCP server becomes a toolset.",
            <ToolsPanel mcp={s.mcp_servers} onSaveMcp={(v) => patch({ mcp_servers: v }, "MCP servers saved")} />,
          )}

          {S(
            "vault",
            undefined,
            <>
              Every stored secret. Tools reference them as <code className="font-mono text-[12px]">{"{{secret:NAME}}"}</code>.
            </>,
            <VaultPanel secrets={secrets} onChange={load} />,
          )}

          {S(
            "appearance",
            undefined,
            undefined,
            <Group>
              <Row label="Theme" hint="Follows your system unless you pick one.">
                <ThemeSwitch />
              </Row>
            </Group>,
          )}
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────── Engine ───────────────────────── */

const CC_MODELS = ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001", "opus", "sonnet", "haiku"];
const CC_TIERS = {
  planner: "Plans the run and designs agents",
  worker: "Default for spawned agents",
  fast: "Quick, simple work",
} as const;

function EngineSection({ s, patch }: { s: Settings; patch: (p: Partial<Settings>, msg?: string) => void }) {
  const cc = s.engine === "claude_code";
  return (
    <div>
      <Segmented
        id="engine"
        value={s.engine}
        onChange={(v) => patch({ engine: v }, v === "claude_code" ? "Using your Claude plan" : "Using API keys")}
        options={[
          { value: "claude_code", label: "Your Claude plan", icon: Bot },
          { value: "api", label: "API keys", icon: Cpu },
        ]}
      />
      <AnimatePresence mode="wait" initial={false}>
        {cc ? (
          <motion.div key="cc" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-5 space-y-4">
            <ClaudeSignIn />
            <div className="grid gap-3 md:grid-cols-3">
              <datalist id="cc-models">
                {CC_MODELS.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
              {(["planner", "worker", "fast"] as const).map((role) => (
                <CCModelField
                  key={role}
                  role={role}
                  hint={CC_TIERS[role]}
                  value={s.claude_code.models[role]}
                  onSave={(v) => patch({ claude_code: { models: { ...s.claude_code.models, [role]: v } } } as Partial<Settings>)}
                />
              ))}
            </div>
            <p className="px-1 text-[12px] leading-relaxed text-fg-3">
              Plan usage limits apply: several Opus agents in parallel use your limit faster. If it runs out, the run stops with a
              message and you can resume it when the limit resets. Extended reasoning maps to Claude Code&apos;s effort level.
            </p>
          </motion.div>
        ) : (
          <motion.p key="api" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="mt-4 px-1 text-[12.5px] text-fg-2">
            Agents call models directly with the keys under API models. Usage is billed by each provider.
          </motion.p>
        )}
      </AnimatePresence>
    </div>
  );
}

function CCModelField({ role, hint, value, onSave }: { role: string; hint: string; value: string; onSave: (v: string) => void }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  return (
    <div className="well rounded-[18px] p-3.5">
      <div className="mb-2 px-0.5">
        <div className="text-[13.5px] font-semibold capitalize">{role}</div>
        <div className="text-[11.5px] text-fg-3">{hint}</div>
      </div>
      <input className="field font-mono text-[12.5px]" list="cc-models" value={v} onChange={(e) => setV(e.target.value)} onBlur={() => v !== value && onSave(v)} />
    </div>
  );
}

/* ───────────────────────── Building blocks ───────────────────────── */

function Group({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`well overflow-hidden rounded-[18px] ${className}`}>{children}</div>;
}

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className={`${listSep} flex min-h-[52px] flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5 [--inset:16px]`}>
      <div className="min-w-0 flex-1">
        <div className="text-[13.5px] font-medium">{label}</div>
        {hint && <div className="text-[12px] leading-snug text-fg-2">{hint}</div>}
      </div>
      <div className="flex items-center gap-3">{children}</div>
    </div>
  );
}

function Label({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <label className="mb-1.5 flex items-baseline gap-1.5 px-0.5 text-[12.5px] font-medium text-fg-2">
      {children} {hint && <span className="truncate text-[11.5px] font-normal text-fg-3">· {hint}</span>}
    </label>
  );
}

function ModelField({ role, hint, value, onSave }: { role: string; hint: string; value: string; onSave: (v: string) => void }) {
  const [v, setV] = useState(value);
  const [test, setTest] = useState<{ state: "idle" | "busy" | "ok" | "err"; text?: string }>({ state: "idle" });
  useEffect(() => setV(value), [value]);
  return (
    <div className="well rounded-[18px] p-3.5">
      <div className="mb-2 flex items-center justify-between gap-2 px-0.5">
        <div>
          <div className="text-[13.5px] font-semibold capitalize">{role}</div>
          <div className="text-[11.5px] text-fg-3">{hint}</div>
        </div>
        <button
          className="btn btn-plain btn-sm"
          onClick={async () => {
            setTest({ state: "busy" });
            const r = await api<any>(`/settings/test-model/${role}`, { method: "POST" }).catch((e) => ({ ok: false, error: e.message }));
            setTest(r.ok ? { state: "ok", text: r.reply } : { state: "err", text: r.error });
          }}
        >
          {test.state === "busy" ? <ActivityIndicator size={12} /> : test.state === "ok" ? <Check size={13} className="text-green" /> : test.state === "err" ? <X size={13} className="text-red" /> : null}
          Test
        </button>
      </div>
      <input className="field font-mono text-[12.5px]" list="models" value={v} onChange={(e) => setV(e.target.value)} onBlur={() => v !== value && onSave(v)} />
      {test.text && <p className={`mt-1.5 truncate px-0.5 text-[11.5px] ${test.state === "ok" ? "text-green" : "text-red"}`}>{test.text}</p>}
    </div>
  );
}

function SecretField({ name, label, existing, onChange, hint }: { name: string; label: string; existing?: Secret; onChange: () => void; hint?: string }) {
  const [v, setV] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div>
      <Label hint={hint}>
        {label}
        {existing && <Check size={12} strokeWidth={3} className="self-center text-green" />}
      </Label>
      <div className="flex gap-2">
        <input
          className="field font-mono text-[12.5px]"
          type="password"
          autoComplete="off"
          placeholder={existing ? `Saved · ${existing.masked}` : "Not set"}
          value={v}
          onChange={(e) => setV(e.target.value)}
        />
        <AnimatePresence initial={false}>
          {v && (
            <motion.button
              initial={{ opacity: 0, width: 0 }}
              animate={{ opacity: 1, width: "auto" }}
              exit={{ opacity: 0, width: 0 }}
              transition={spring}
              className="btn btn-accent h-9 overflow-hidden"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                await api(`/secrets/${name}`, { method: "PUT", json: { value: v } }).catch(() => {});
                setV("");
                setBusy(false);
                onChange();
              }}
            >
              Save
            </motion.button>
          )}
        </AnimatePresence>
        {existing && !v && (
          <button
            className="btn btn-glass btn-icon h-9 w-9 shrink-0 hover:text-red"
            title="Remove"
            onClick={async () => {
              await api(`/secrets/${name}`, { method: "DELETE" });
              onChange();
            }}
          >
            <Trash2 size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

function TextSetting({ label, value, onSave, placeholder }: { label: string; value: string; onSave: (v: string) => void; placeholder?: string }) {
  const [v, setV] = useState(value ?? "");
  useEffect(() => setV(value ?? ""), [value]);
  return (
    <div>
      <Label>{label}</Label>
      <input className="field" value={v} placeholder={placeholder} onChange={(e) => setV(e.target.value)} onBlur={() => v !== (value ?? "") && onSave(v)} />
    </div>
  );
}

function NumberField({ value, onSave, prefix }: { value: number; onSave: (v: number) => void; prefix?: string }) {
  const [v, setV] = useState(String(value));
  useEffect(() => setV(String(value)), [value]);
  return (
    <label className="flex h-8 w-24 items-center rounded-[10px] bg-fill px-2.5 text-[13.5px] focus-within:shadow-[0_0_0_3px_color-mix(in_oklab,var(--accent)_35%,transparent)]">
      {prefix && <span className="text-fg-3">{prefix}</span>}
      <input
        className="w-full bg-transparent text-right font-medium outline-none tabular"
        inputMode="decimal"
        value={v}
        onChange={(e) => setV(e.target.value.replace(/[^0-9.]/g, ""))}
        onBlur={() => Number(v) !== value && onSave(Number(v || 0))}
      />
    </label>
  );
}

function IntegrationCheck() {
  const [r, setR] = useState<any>(null);
  return (
    <div className="mt-5 flex flex-wrap items-center gap-3">
      <button
        className="btn btn-glass"
        onClick={async () => {
          setR("…");
          setR(await api("/integrations/check").catch((e) => ({ error: e.message })));
        }}
      >
        {r === "…" ? <ActivityIndicator size={13} /> : <RefreshCw size={13} />} Check connections
      </button>
      {r && typeof r === "object" && (
        <div className="flex flex-wrap gap-2 text-[12px] font-medium">
          {["vercel", "github"].map((k) =>
            r[k] ? (
              <span
                key={k}
                className="rounded-full px-2.5 py-1"
                style={{ color: r[k].ok ? "var(--green)" : "var(--red)", background: `color-mix(in oklab, ${r[k].ok ? "var(--green)" : "var(--red)"} 14%, transparent)` }}
              >
                {k} {r[k].ok ? `· ${r[k].user}` : `· ${r[k].error ?? "invalid token"}`}
              </span>
            ) : null,
          )}
          {r.error && <span className="text-red">{r.error}</span>}
        </div>
      )}
    </div>
  );
}

const CONTACT_FIELDS: [string, string][] = [
  ["firstName", "First name"],
  ["lastName", "Last name"],
  ["email", "Email"],
  ["phone", "Phone (+1.5555555555)"],
  ["address1", "Address"],
  ["city", "City"],
  ["state", "State"],
  ["zip", "ZIP"],
  ["country", "Country (ISO, e.g. US)"],
  ["companyName", "Company (optional)"],
];

function RegistrantForm({ value, onSave }: { value: Record<string, string>; onSave: (v: Record<string, string>) => void }) {
  const [v, setV] = useState(value);
  useEffect(() => setV(value), [value]);
  return (
    <div>
      <div className="grid gap-3 md:grid-cols-2">
        {CONTACT_FIELDS.map(([k, label]) => (
          <div key={k}>
            <Label>{label}</Label>
            <input className="field" value={v[k] ?? ""} onChange={(e) => setV({ ...v, [k]: e.target.value })} />
          </div>
        ))}
      </div>
      <button className="btn btn-accent mt-5" onClick={() => onSave(v)}>
        Save contact
      </button>
    </div>
  );
}

function PromptsEditor({ onSaved }: { onSaved: () => void }) {
  const [prompts, setPrompts] = useState<Record<string, PromptInfo> | null>(null);
  const [tab, setTab] = useState("harness");
  const [draft, setDraft] = useState("");
  useEffect(() => {
    api<Record<string, PromptInfo>>("/prompts").then((p) => {
      setPrompts(p);
      setDraft(p.harness.content);
    });
  }, []);
  if (!prompts) return <ActivityIndicator />;
  const cur = prompts[tab];
  const save = async (content: string) => {
    const r = await api<PromptInfo>(`/prompts/${tab}`, { method: "PUT", json: { content } });
    setPrompts({ ...prompts, [tab]: r });
    setDraft(r.content);
    onSaved();
  };
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Segmented
          id="prompt-tab"
          value={tab}
          onChange={(n) => {
            setTab(n);
            setDraft(prompts[n].content);
          }}
          options={Object.keys(prompts).map((n) => ({ value: n, label: n[0].toUpperCase() + n.slice(1) }))}
        />
        <span className="ml-auto rounded-full bg-fill px-2.5 py-1 text-[11.5px] font-medium text-fg-2">
          Source: <b className="text-fg">{cur.source}</b>
        </span>
      </div>
      <textarea className="field field-area scrollbar-thin h-80 rounded-[16px] font-mono text-[12px]" value={draft} onChange={(e) => setDraft(e.target.value)} spellCheck={false} />
      <div className="mt-3 flex gap-2">
        <button className="btn btn-accent" disabled={draft === cur.content} onClick={() => save(draft)}>
          Save override
        </button>
        <button className="btn btn-glass" disabled={cur.source !== "dashboard"} onClick={() => save("")} title="Remove the dashboard override">
          Reset to default
        </button>
      </div>
    </div>
  );
}

function ToolsPanel({ mcp, onSaveMcp }: { mcp: Record<string, unknown>; onSaveMcp: (v: Record<string, unknown>) => void }) {
  const [cat, setCat] = useState<{ toolsets: ToolsetInfo[]; tools: ToolInfo[]; plugin_errors: { file: string; error: string }[]; plugins_dir: string; mcp_errors?: any[] } | null>(null);
  const [mcpText, setMcpText] = useState(JSON.stringify(mcp, null, 2));
  const [mcpErr, setMcpErr] = useState<string | null>(null);
  const [showTools, setShowTools] = useState(false);
  const load = (withMcp = false) => api(`/tools${withMcp ? "?include_mcp=true" : ""}`).then(setCat);
  useEffect(() => {
    load();
  }, []);
  useEffect(() => setMcpText(JSON.stringify(mcp, null, 2)), [mcp]);
  return (
    <div>
      <McpSuggestions
        current={mcp}
        onAdd={(id, url, name) => {
          const next = { ...(mcp as Record<string, unknown>), [id]: { transport: "streamable_http", url, description: `${name} (official MCP server)` } };
          onSaveMcp(next);
        }}
      />

      <div className="mt-7 mb-3 flex flex-wrap items-center gap-2">
        <div className="eyebrow flex-1">Toolsets · {cat?.toolsets.length ?? 0}</div>
        <button className="btn btn-glass btn-sm" onClick={async () => setCat(await api("/tools/reload", { method: "POST" }))}>
          <RefreshCw size={12} /> Reload plugins
        </button>
        <button className="btn btn-glass btn-sm" onClick={() => load(true)}>
          Load MCP tools
        </button>
      </div>
      {cat?.plugin_errors?.map((e) => (
        <pre key={e.file} className="mb-2 overflow-auto rounded-[12px] bg-red/10 p-2.5 font-mono text-[11px] text-red">
          {e.file}: {e.error}
        </pre>
      ))}
      {cat?.mcp_errors?.map((e: any) => (
        <pre key={e.server} className="mb-2 overflow-auto rounded-[12px] bg-red/10 p-2.5 font-mono text-[11px] text-red">
          MCP {e.server}: {e.error}
        </pre>
      ))}
      <div className="grid gap-2.5 md:grid-cols-2">
        {cat?.toolsets.map((ts) => (
          <div key={ts.name} className="well rounded-[16px] px-3.5 py-3">
            <div className="flex items-center gap-2">
              <span className="font-mono text-[13px] font-semibold">{ts.name}</span>
              <span className="rounded-full bg-fill px-2 py-0.5 text-[10.5px] font-medium text-fg-2">
                {ts.source} · {ts.tools.length}
              </span>
            </div>
            <div className="mt-1 line-clamp-2 text-[12px] leading-snug text-fg-2">{ts.description}</div>
          </div>
        ))}
      </div>

      <button className="btn btn-plain mt-4 -ml-3" onClick={() => setShowTools(!showTools)}>
        {showTools ? "Hide" : "Show"} all {cat?.tools.length ?? ""} tools <ChevronRight size={13} className={`transition-transform ${showTools ? "rotate-90" : ""}`} />
      </button>
      <AnimatePresence initial={false}>
        {showTools && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="well scrollbar-thin mt-2 max-h-96 overflow-auto rounded-[16px]">
              {cat?.tools.map((t) => (
                <div key={t.name} className={`${listSep} flex gap-3 px-3.5 py-2 [--inset:14px]`}>
                  <span className="w-44 shrink-0 truncate font-mono text-[12px] font-medium">{t.name}</span>
                  <span className="w-28 shrink-0 truncate font-mono text-[11.5px] text-fg-3">{t.toolset}</span>
                  <span className="min-w-0 flex-1 truncate text-[12px] text-fg-2">{t.description}</span>
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="eyebrow mt-7 mb-2">MCP servers (JSON)</div>
      <textarea className="field field-area h-40 rounded-[16px] font-mono text-[12px]" value={mcpText} onChange={(e) => setMcpText(e.target.value)} spellCheck={false} />
      <p className="mt-2 text-[11.5px] leading-relaxed break-all text-fg-3">
        e.g. {`{"linear": {"transport": "streamable_http", "url": "https://mcp.linear.app/mcp", "headers": {"Authorization": "Bearer {{secret:LINEAR_TOKEN}}"}}}`} → toolset{" "}
        <code className="font-mono">mcp_linear</code>
      </p>
      <button
        className="btn btn-accent mt-3"
        onClick={() => {
          try {
            setMcpErr(null);
            onSaveMcp(JSON.parse(mcpText || "{}"));
          } catch (e: any) {
            setMcpErr(e.message);
          }
        }}
      >
        Save MCP servers
      </button>
      {mcpErr && <p className="mt-1.5 text-[12px] text-red">{mcpErr}</p>}
    </div>
  );
}

type CatalogItem = { id: string; name: string; mcp_url: string | null; mcp_docs: string | null; mcp_configured: boolean; api_docs: string | null };
const MCP_COLORS = ["#0a84ff", "#30d158", "#ff9f0a", "#bf5af2", "#ff375f", "#40c8e0", "#5e5ce6", "#ffcc00", "#ff453a"];

function McpSuggestions({ current, onAdd }: { current: Record<string, unknown>; onAdd: (id: string, url: string, name: string) => void }) {
  const [items, setItems] = useState<CatalogItem[]>([]);
  useEffect(() => {
    api<CatalogItem[]>("/integrations/catalog").then(setItems).catch(() => {});
  }, [current]);
  const withMcp = items.filter((i) => i.mcp_url);
  if (!withMcp.length) return null;
  return (
    <div>
      <div className="eyebrow mb-1">Official MCP servers</div>
      <p className="mb-3 text-[12.5px] leading-snug text-fg-2">Agents prefer APIs and MCP servers over the browser. Each one you add becomes a toolset. Some ask you to authorize after adding.</p>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {withMcp.map((i, n) => {
          const added = i.mcp_configured || i.id in current;
          const c = MCP_COLORS[n % MCP_COLORS.length];
          return (
            <div key={i.id} className="well flex items-center gap-2.5 rounded-[16px] p-2.5" title={i.mcp_url ?? ""}>
              <span
                className="grid h-8 w-8 shrink-0 place-items-center rounded-[9px] text-[14px] font-semibold text-white"
                style={{ background: `linear-gradient(155deg, color-mix(in oklab, ${c} 70%, white), ${c})` }}
              >
                {i.name[0]}
              </span>
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{i.name}</span>
              {added ? (
                <span className="grid h-7 w-7 place-items-center rounded-full bg-green/15 text-green">
                  <Check size={14} strokeWidth={3} />
                </span>
              ) : (
                <button className="btn btn-tinted btn-sm btn-icon h-7 w-7" onClick={() => onAdd(i.id, i.mcp_url!, i.name)} title={`Add ${i.name}`}>
                  <Plus size={14} strokeWidth={2.6} />
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function VaultPanel({ secrets, onChange }: { secrets: Secret[]; onChange: () => void }) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  return (
    <div>
      <Group>
        {secrets.length === 0 && <div className="px-4 py-3 text-[13px] text-fg-2">Empty</div>}
        {secrets.map((x) => (
          <div key={x.name} className={`${listSep} flex items-center gap-3 px-4 py-2.5 [--inset:16px]`}>
            <Lock size={13} className="text-fg-3" />
            <span className="font-mono text-[12.5px] font-medium">{x.name}</span>
            <span className="font-mono text-[12px] text-fg-3">{x.masked}</span>
            <button
              className="ml-auto text-fg-3 transition-colors hover:text-red"
              onClick={async () => {
                await api(`/secrets/${x.name}`, { method: "DELETE" });
                onChange();
              }}
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
      </Group>
      <div className="mt-3 flex flex-wrap gap-2">
        <input className="field w-56 font-mono text-[12.5px]" placeholder="NAME" value={name} onChange={(e) => setName(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, ""))} />
        <input className="field min-w-48 flex-1 font-mono text-[12.5px]" type="password" placeholder="value" value={value} onChange={(e) => setValue(e.target.value)} />
        <button
          className="btn btn-accent h-9"
          disabled={!name || !value}
          onClick={async () => {
            await api(`/secrets/${name}`, { method: "PUT", json: { value } });
            setName("");
            setValue("");
            onChange();
          }}
        >
          Add secret
        </button>
      </div>
    </div>
  );
}
