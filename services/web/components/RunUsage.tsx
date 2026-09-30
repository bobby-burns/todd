"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Gauge, RefreshCw } from "lucide-react";
import { agentColor, api, usd, type AgentInfo } from "@/lib/api";
import { ActivityIndicator, AgentAvatar } from "./ui";
import { BarRow, compact, costLine, duration, StatTile, type UsageNumbers } from "./usage";

type AgentUsage = UsageNumbers & {
  id: string;
  name: string;
  status: string | null;
  model: string;
  tool_calls: number;
  browser_steps: number;
  top_tools: [string, number][];
  seconds: number | null;
};
type RunUsageData = {
  total: UsageNumbers & { tool_calls: number; browser_steps: number; agents: number; seconds: number | null; spent_usd: number };
  agents: AgentUsage[];
  models: (UsageNumbers & { model: string })[];
  tools: { tool: string; calls: number }[];
};

/** What this run used: model tokens and calls, tool calls, browser steps, time and money, per agent. */
export function RunUsage({ runId, active, agents, claudePlan }: { runId: string; active: boolean; agents: AgentInfo[]; claudePlan: boolean }) {
  const [data, setData] = useState<RunUsageData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () => {
      setBusy(true);
      api<RunUsageData>(`/runs/${runId}/usage`)
        .then((d) => alive && (setData(d), setErr(null)))
        .catch((e) => alive && setErr(e.message))
        .finally(() => alive && setBusy(false));
    };
    load();
    if (!active) return () => void (alive = false);
    const t = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [runId, active]);

  if (err) return <p className="rounded-[14px] bg-red/10 px-4 py-3 text-[13px] text-red">{err}</p>;
  if (!data)
    return (
      <div className="grid h-40 place-items-center text-fg-3">
        <ActivityIndicator size={20} />
      </div>
    );
  const t = data.total;
  const maxTokens = Math.max(...data.agents.map((a) => a.tokens), 0);
  const maxTool = Math.max(...data.tools.map((x) => x.calls), 0);
  return (
    <div className="glass rounded-[28px] p-5 md:p-6">
      <div className="mb-4 flex items-center gap-2">
        <Gauge size={17} className="text-accent" />
        <h2 className="flex-1 text-[15px] font-semibold tracking-[-0.01em]">Usage</h2>
        {busy && <RefreshCw size={13} className="animate-spin text-fg-3" />}
        <Link href="/usage" className="text-[12.5px] font-medium text-accent">
          All runs
        </Link>
      </div>

      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-6">
        <StatTile label="Tokens" value={compact(t.tokens)} hint={`${compact(t.input_tokens + t.cache_read_tokens + t.cache_write_tokens)} in · ${compact(t.output_tokens)} out`} />
        <StatTile label="Model calls" value={t.calls.toLocaleString()} hint={`${t.agents + 1} agent${t.agents ? "s" : ""} incl. the planner`} />
        <StatTile label="Tool calls" value={t.tool_calls.toLocaleString()} hint={t.browser_steps ? `${t.browser_steps} browser steps` : "no browser steps"} />
        <StatTile label="Time" value={duration(t.seconds)} hint={active ? "still running" : "start to last activity"} />
        <StatTile
          label={claudePlan ? "On your Claude plan" : "Model cost"}
          value={claudePlan && !t.cost_usd ? (t.plan_usd ? `≈ ${usd(t.plan_usd, t.plan_usd < 1 ? 3 : 2)}` : "—") : usd(t.cost_usd, t.cost_usd < 1 ? 3 : 2)}
          hint={claudePlan && !t.cost_usd ? "what it would cost at API prices; not billed" : "billed to your API keys"}
        />
        <StatTile label="Spent" value={usd(t.spent_usd)} hint="purchases (see Ledger)" />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[3fr_2fr]">
        <section>
          <h3 className="mb-3 text-[12.5px] font-semibold text-fg-2">By agent · tokens</h3>
          <div className="space-y-3.5">
            {data.agents.map((a) => (
              <div key={a.id}>
                <BarRow
                  label={a.name}
                  value={a.tokens}
                  max={maxTokens}
                  display={compact(a.tokens)}
                  lead={<AgentAvatar name={a.name} color={agentColor(agents, a.id)} planner={a.id === "planner"} size={18} />}
                  title={`${a.name}: ${a.tokens.toLocaleString()} tokens, ${a.calls} model calls`}
                />
                <div className="mt-1 text-[11.5px] text-fg-3 tabular">
                  {a.calls} model call{a.calls === 1 ? "" : "s"} · {a.tool_calls} tool call{a.tool_calls === 1 ? "" : "s"}
                  {a.browser_steps ? ` · ${a.browser_steps} browser steps` : ""} · {duration(a.seconds)}
                  {a.cost_usd || a.plan_usd ? ` · ${costLine(a)}` : ""}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="space-y-6">
          <div>
            <h3 className="mb-3 text-[12.5px] font-semibold text-fg-2">Tools used most</h3>
            {data.tools.length === 0 ? (
              <p className="text-[12.5px] text-fg-3">No tool calls yet.</p>
            ) : (
              <div className="space-y-2.5">
                {data.tools.map((x) => (
                  <BarRow key={x.tool} label={<span className="font-mono text-[12px]">{x.tool}</span>} value={x.calls} max={maxTool} display={x.calls.toLocaleString()} />
                ))}
              </div>
            )}
          </div>
          {data.models.length > 0 && (
            <div>
              <h3 className="mb-2 text-[12.5px] font-semibold text-fg-2">Models</h3>
              <table className="w-full table-fixed text-[12.5px]">
                <thead>
                  <tr className="text-left text-[11.5px] text-fg-3">
                    <th className="pb-1 font-medium">Model</th>
                    <th className="w-14 pb-1 text-right font-medium">Calls</th>
                    <th className="w-16 pb-1 text-right font-medium">Tokens</th>
                    <th className="w-16 pb-1 text-right font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody className="tabular">
                  {data.models.map((m) => (
                    <tr key={m.model} className="border-t border-sep">
                      <td className="truncate py-1.5 pr-2 font-mono text-[12px]" title={m.model}>{m.model}</td>
                      <td className="py-1.5 text-right">{m.calls.toLocaleString()}</td>
                      <td className="py-1.5 text-right">{compact(m.tokens)}</td>
                      <td className="py-1.5 text-right">{costLine(m)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
      <p className="mt-5 text-[11.5px] leading-snug text-fg-3">
        Tokens are the pieces of text models read and write; they&apos;re what model usage is measured in. Cached tokens (repeated
        context the model has already seen) count here but cost much less.
      </p>
    </div>
  );
}
