"use client";

import { useState, type ReactNode } from "react";
import { usd } from "@/lib/api";

/* Usage building blocks: numbers people can read at a glance (stat tiles), labelled bars for "who used what", and
   one column chart of usage per day. One hue for magnitude (the accent); values and labels in text colors. */

export type UsageNumbers = {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  tokens: number;
  cost_usd: number;
  plan_usd: number;
};

/** 1,284 · 12.9K · 4.2M */
export function compact(n: number) {
  if (!n) return "0";
  if (n < 1000) return n.toLocaleString();
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}K`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

export function duration(secs?: number | null) {
  if (secs == null) return "—";
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  return m < 60 ? `${m}m ${secs % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** What model usage cost: billed to your API keys, or (Claude plan) what it would cost at API prices. */
export function costLine(u: Pick<UsageNumbers, "cost_usd" | "plan_usd">) {
  if (u.cost_usd > 0) return usd(u.cost_usd, u.cost_usd < 1 ? 3 : 2);
  if (u.plan_usd > 0) return `≈ ${usd(u.plan_usd, u.plan_usd < 1 ? 3 : 2)}`;
  return usd(0);
}

export function StatTile({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="min-w-0 rounded-[18px] bg-fill/45 px-4 py-3">
      <div className="truncate text-[12px] font-medium text-fg-2">{label}</div>
      <div className="mt-0.5 truncate text-[22px] font-semibold tracking-[-0.02em] text-fg">{value}</div>
      {hint && <div className="mt-0.5 truncate text-[11.5px] text-fg-3">{hint}</div>}
    </div>
  );
}

/** A labelled horizontal bar (one hue; the value is printed, so nothing depends on hovering). */
export function BarRow({ label, value, max, display, lead, title }: { label: ReactNode; value: number; max: number; display: ReactNode; lead?: ReactNode; title?: string }) {
  const pct = max > 0 ? Math.max(value > 0 ? 1.5 : 0, (value / max) * 100) : 0;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1" title={title}>
      <div className="flex min-w-0 items-center gap-2 text-[13px]">
        {lead}
        <span className="min-w-0 truncate text-fg">{label}</span>
      </div>
      <div className="text-right text-[12.5px] font-medium text-fg tabular">{display}</div>
      <div className="col-span-2 h-2 overflow-hidden rounded-full bg-fill/70">
        <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

type Day = UsageNumbers & { day: string };

/** Tokens per day as columns: hover or focus a day for its numbers. */
export function DailyColumns({ series, height = 180 }: { series: Day[]; height?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(...series.map((d) => d.tokens), 0);
  const step = niceStep(max / 3);
  const top = Math.max(step * 3, step);
  const ticks = [0, step, step * 2, step * 3];
  const n = series.length;
  const label = (d: string) => new Date(`${d}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  const every = n > 45 ? 14 : n > 20 ? 7 : n > 10 ? 2 : 1;
  const h = hover != null ? series[hover] : null;
  return (
    <div className="relative">
      <div className="flex gap-2">
        {/* y axis */}
        <div className="relative w-10 shrink-0 text-right text-[11px] text-fg-3 tabular" style={{ height }}>
          {ticks.map((t) => (
            <span key={t} className="absolute right-0 -translate-y-1/2" style={{ top: height - (t / top) * height }}>
              {compact(t)}
            </span>
          ))}
        </div>
        <div className="relative min-w-0 flex-1" style={{ height }} onMouseLeave={() => setHover(null)}>
          {ticks.map((t) => (
            <div key={t} className="absolute inset-x-0 h-px bg-sep" style={{ top: height - (t / top) * height }} />
          ))}
          <div className="absolute inset-0 flex items-end">
            {series.map((d, i) => {
              const bh = top > 0 ? (d.tokens / top) * height : 0;
              return (
                <button
                  key={d.day}
                  type="button"
                  className="group relative flex h-full min-w-0 flex-1 items-end justify-center px-[1px] outline-none"
                  onMouseEnter={() => setHover(i)}
                  onFocus={() => setHover(i)}
                  onBlur={() => setHover(null)}
                  aria-label={`${label(d.day)}: ${compact(d.tokens)} tokens, ${d.calls} model calls`}
                >
                  <span className={`absolute inset-0 rounded-[6px] transition-colors ${hover === i ? "bg-fill/60" : ""}`} />
                  <span
                    className={`relative w-full max-w-[24px] rounded-t-[4px] transition-opacity ${hover != null && hover !== i ? "opacity-60" : ""}`}
                    style={{ height: d.tokens > 0 ? Math.max(bh, 2) : 0, background: "var(--accent)" }}
                  />
                </button>
              );
            })}
          </div>
          {h && hover != null && (
            <div
              className="glass glass-strong pointer-events-none absolute bottom-full z-10 mb-2 w-max max-w-[220px] rounded-[12px] px-3 py-2 text-[12px] shadow-lg"
              style={{ left: `${((hover + 0.5) / n) * 100}%`, transform: `translateX(${hover / n > 0.7 ? "-90%" : hover / n < 0.3 ? "-10%" : "-50%"})` }}
            >
              <div className="text-[15px] font-semibold text-fg tabular">{compact(h.tokens)} tokens</div>
              <div className="text-fg-2">{label(h.day)}</div>
              <div className="mt-1 space-y-0.5 text-fg-2 tabular">
                <div>{h.calls.toLocaleString()} model calls</div>
                <div>{compact(h.input_tokens + h.cache_read_tokens + h.cache_write_tokens)} in · {compact(h.output_tokens)} out</div>
                {(h.cost_usd > 0 || h.plan_usd > 0) && <div>{costLine(h)}</div>}
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="mt-1.5 ml-12 flex text-[11px] text-fg-3">
        {series.map((d, i) => (
          <span key={d.day} className="min-w-0 flex-1 overflow-visible text-center whitespace-nowrap">
            {i === n - 1 || (i % every === 0 && n - 1 - i >= Math.ceil(every / 2)) ? label(d.day) : ""}
          </span>
        ))}
      </div>
    </div>
  );
}

function niceStep(x: number) {
  if (x <= 0) return 1000;
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  const m = x / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
}
