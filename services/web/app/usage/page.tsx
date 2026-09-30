"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, usd } from "@/lib/api";
import { ActivityIndicator, PageHeader, Segmented, StatusPill } from "@/components/ui";
import { compact, costLine, DailyColumns, StatTile, type UsageNumbers } from "@/components/usage";

type Overview = {
  days: number;
  total: UsageNumbers;
  series: (UsageNumbers & { day: string })[];
  runs: (UsageNumbers & { run_id: string; title: string; status: string | null; exists: boolean })[];
  models: (UsageNumbers & { model: string })[];
};

const RANGES = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
];

export default function UsagePage() {
  const [days, setDays] = useState("30");
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    api<Overview>(`/usage?days=${days}`)
      .then((d) => (setData(d), setErr(null)))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [days]);

  const t = data?.total;
  return (
    <div className="mx-auto max-w-[1100px] px-4 md:px-8">
      <PageHeader title="Usage" subtitle="What your runs used: model tokens and calls, and what they cost. Purchases are in the Ledger." />

      <div className="mb-4 flex items-center gap-3">
        <Segmented id="usage-range" value={days} onChange={setDays} options={RANGES} />
        {loading && <ActivityIndicator size={14} className="text-fg-3" />}
      </div>
      {err && <p className="mb-4 rounded-[14px] bg-red/10 px-4 py-3 text-[13px] text-red">{err}</p>}

      <div className={`transition-opacity ${loading && data ? "opacity-60" : ""}`}>
        {!data || !t ? (
          <div className="grid h-40 place-items-center text-fg-3">{!err && <ActivityIndicator size={20} />}</div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
              <StatTile label="Tokens" value={compact(t.tokens)} hint={`${compact(t.input_tokens + t.cache_read_tokens + t.cache_write_tokens)} in · ${compact(t.output_tokens)} out`} />
              <StatTile label="Model calls" value={t.calls.toLocaleString()} hint={`across ${data.runs.length} run${data.runs.length === 1 ? "" : "s"}`} />
              <StatTile label="Billed to API keys" value={usd(t.cost_usd, t.cost_usd < 1 ? 3 : 2)} hint="model usage on the API engine" />
              <StatTile label="On your Claude plan" value={t.plan_usd ? `≈ ${usd(t.plan_usd, t.plan_usd < 1 ? 3 : 2)}` : "—"} hint="at API prices; not billed" />
            </div>

            <section className="glass mt-4 rounded-[24px] p-5">
              <h2 className="mb-4 text-[13px] font-semibold text-fg-2">Tokens per day</h2>
              {t.tokens === 0 ? <p className="py-8 text-center text-[13px] text-fg-3">No model usage in this period yet.</p> : <DailyColumns series={data.series} />}
            </section>

            <div className="mt-4 grid gap-4 lg:grid-cols-[3fr_2fr]">
              <section className="glass rounded-[24px] p-5">
                <h2 className="mb-2 text-[13px] font-semibold text-fg-2">Runs that used the most</h2>
                {data.runs.length === 0 ? (
                  <p className="py-4 text-[13px] text-fg-3">No runs in this period.</p>
                ) : (
                  <table className="w-full table-fixed text-[13px]">
                    <thead>
                      <tr className="text-left text-[11.5px] text-fg-3">
                        <th className="pb-1.5 font-medium">Run</th>
                        <th className="w-20 pb-1.5 text-right font-medium">Tokens</th>
                        <th className="hidden w-16 pb-1.5 text-right font-medium sm:table-cell">Calls</th>
                        <th className="w-20 pb-1.5 text-right font-medium">Cost</th>
                      </tr>
                    </thead>
                    <tbody className="tabular">
                      {data.runs.map((r) => (
                        <tr key={r.run_id} className="border-t border-sep">
                          <td className="py-2 pr-3">
                            <div className="flex min-w-0 items-center gap-2">
                              {r.exists ? (
                                <Link href={`/runs/${r.run_id}`} className="min-w-0 truncate font-medium hover:text-accent">
                                  {r.title}
                                </Link>
                              ) : (
                                <span className="min-w-0 truncate text-fg-2">{r.title}</span>
                              )}
                              {r.status && <StatusPill status={r.status} live={false} className="h-5 shrink-0 text-[10.5px]" />}
                            </div>
                          </td>
                          <td className="py-2 text-right">{compact(r.tokens)}</td>
                          <td className="hidden py-2 text-right sm:table-cell">{r.calls.toLocaleString()}</td>
                          <td className="py-2 text-right">{costLine(r)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
              <section className="glass rounded-[24px] p-5">
                <h2 className="mb-2 text-[13px] font-semibold text-fg-2">Models</h2>
                {data.models.length === 0 ? (
                  <p className="py-4 text-[13px] text-fg-3">No model usage in this period.</p>
                ) : (
                  <table className="w-full table-fixed text-[13px]">
                    <thead>
                      <tr className="text-left text-[11.5px] text-fg-3">
                        <th className="pb-1.5 font-medium">Model</th>
                        <th className="w-20 pb-1.5 text-right font-medium">Tokens</th>
                        <th className="w-20 pb-1.5 text-right font-medium">Cost</th>
                      </tr>
                    </thead>
                    <tbody className="tabular">
                      {data.models.map((m) => (
                        <tr key={m.model} className="border-t border-sep">
                          <td className="truncate py-2 pr-3 font-mono text-[12px]" title={m.model}>{m.model}</td>
                          <td className="py-2 text-right">{compact(m.tokens)}</td>
                          <td className="py-2 text-right">{costLine(m)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
            </div>
            <p className="mt-4 mb-16 px-1 text-[12px] leading-snug text-fg-3">
              Tokens are the pieces of text models read and write. With the Claude Code engine, usage counts toward your Claude plan; the amount shown is what
              it would cost at API prices, for comparison. Usage from runs you deleted stays here.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
