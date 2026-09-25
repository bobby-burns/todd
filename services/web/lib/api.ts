export type RunStatus =
  | "queued"
  | "running"
  | "waiting"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";

export type Run = {
  id: string;
  title: string;
  prompt: string;
  status: RunStatus;
  budget_usd: number;
  spent_usd: number;
  llm_cost_usd: number;
  summary: string | null;
  created_at: string;
  updated_at: string;
  active: boolean;
  pending_interactions: number;
  paused_agents?: string[];
  engine?: "claude_code" | "api";
};

export type TEvent = {
  id: number;
  run_id: string;
  ts: string;
  agent: string;
  kind: string;
  text: string;
  data: Record<string, any>;
};

export type Interaction = {
  id: string;
  run_id: string;
  agent: string;
  kind: "question" | "approval" | "spend";
  prompt: string;
  data: Record<string, any>;
  status: "pending" | "approved" | "denied" | "answered" | "cancelled";
  answer: string | null;
  created_at: string;
  resolved_at: string | null;
};

export type LedgerEntry = {
  id: number;
  run_id: string;
  ts: string;
  merchant: string;
  amount_usd: number;
  description: string;
  method: string;
  status: string;
  approved_by: string;
};

export type ToolInfo = {
  name: string;
  description: string;
  toolset: string;
  planner: boolean;
  source: string;
};

export type ToolsetInfo = { name: string; description: string; source: string; tools: string[] };

export type AgentInfo = {
  id: string;
  name: string;
  status: string;
  toolsets: string[];
  model_tier: string;
  model: string;
  summary: string | null;
  llm_cost_usd: number | null;
  task: string;
  instructions: string;
  background: boolean;
  created_at: string;
};

export type AccountStatus = "signed_in" | "signed_out" | "unknown";

export type Account = {
  id: string;
  name: string;
  category: string;
  login: string;
  check: string;
  domains: string[];
  cookies: string[];
  via: string | null;
  custom: boolean;
  selected: boolean;
  status: AccountStatus;
  source: string;
  expires?: number | null;
  checked_at?: string | null;
  cookie_count?: number;
};

export type AccountsResponse = { browser_online: boolean; accounts: Account[]; categories: string[] };

export type Onboarding = {
  completed: boolean;
  steps: {
    model: { done: boolean; detail: string };
    integrations: { done: boolean; vercel: boolean; github: boolean };
    spending: { done: boolean; registrant: boolean };
    accounts: { done: boolean; selected: number; signed_in: number; browser_online: boolean };
  };
};

/** Apple system colors. The planner is always indigo; spawned agents cycle through the rest. */
const AGENT_COLORS = [
  "#7d7aff", // planner (indigo)
  "#30d158", // green
  "#ff9f0a", // orange
  "#ff375f", // pink
  "#40c8e0", // teal
  "#bf5af2", // purple
  "#ffcc00", // yellow
  "#66d4cf", // mint
  "#ff453a", // red
  "#ac8e68", // brown
];

export function agentColor(agents: { id: string }[], id: string): string {
  if (id === "planner" || id === "system") return AGENT_COLORS[0];
  if (id === "human") return "#0a84ff";
  const spawned = agents.filter((a) => a.id !== "planner");
  const idx = spawned.findIndex((a) => a.id === id);
  const n = idx < 0 ? [...id].reduce((h, c) => h + c.charCodeAt(0), 0) : idx;
  return AGENT_COLORS[1 + (n % (AGENT_COLORS.length - 1))];
}

/** "anthropic/claude-sonnet-5" → "claude-sonnet-5" */
export const shortModel = (m: string | null | undefined) => (m ?? "").split("/").pop() ?? "";

export function initials(name: string): string {
  const words = name.replace(/[^A-Za-z0-9 &]/g, " ").split(/\s+|&/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).replace(/^./, (c) => c.toUpperCase());
  return (words[0][0] + words[1][0]).toUpperCase();
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T = any>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {};
  const res = await fetch(`/api${path}`, {
    ...rest,
    headers: { ...(json !== undefined ? { "content-type": "application/json" } : {}), ...(rest.headers ?? {}) },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
    cache: "no-store",
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {}
    throw new ApiError(res.status, msg);
  }
  return (await res.json()) as T;
}

export const usd = (n: number | null | undefined, digits = 2) =>
  `$${(n ?? 0).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export function timeAgo(iso: string): string {
  const t = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").getTime();
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
