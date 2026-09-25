"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { motion } from "motion/react";
import { Receipt } from "lucide-react";
import { api, usd, type LedgerEntry } from "@/lib/api";
import { fadeUp, softSpring, stagger } from "@/lib/motion";
import { ActivityIndicator, Empty, PageHeader, listSep } from "@/components/ui";

const TONE: Record<string, { label: string; color: string }> = {
  completed: { label: "Paid", color: "var(--green)" },
  authorized: { label: "Authorized", color: "var(--accent)" },
  needs_review: { label: "Needs review", color: "var(--orange)" },
  failed: { label: "Failed", color: "var(--red)" },
  denied: { label: "Denied", color: "var(--fg-2)" },
  voided: { label: "Voided", color: "var(--fg-2)" },
};

const HUES = ["#30d158", "#0a84ff", "#ff9f0a", "#bf5af2", "#ff375f", "#40c8e0"];
const merchantColor = (m: string) => HUES[[...m].reduce((h, c) => h + c.charCodeAt(0), 0) % HUES.length];

export default function LedgerPage() {
  const [data, setData] = useState<{ entries: LedgerEntry[]; total_usd: number } | null>(null);
  useEffect(() => {
    api("/ledger").then(setData);
  }, []);
  const byMerchant = new Map<string, number>();
  for (const e of data?.entries ?? []) if (e.status === "completed") byMerchant.set(e.merchant, (byMerchant.get(e.merchant) ?? 0) + e.amount_usd);

  return (
    <div className="mx-auto max-w-[920px] px-4 md:px-8">
      <PageHeader title="Ledger" subtitle="Every purchase the agents made or tried to make. Spending rules are enforced outside the model." />

      <motion.div
        initial={{ opacity: 0, y: 12, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={softSpring}
        className="relative overflow-hidden rounded-[28px] p-6 text-white shadow-[0_24px_60px_-24px_rgb(10_132_255/0.6)]"
        style={{ background: "linear-gradient(135deg, #0a84ff 0%, #5e5ce6 55%, #bf5af2 110%)" }}
      >
        <div className="pointer-events-none absolute -top-24 -right-16 h-64 w-64 rounded-full bg-white/15 blur-3xl" />
        <div className="relative text-[13px] font-medium text-white/75">Total spent</div>
        <div className="relative mt-1 font-display text-[48px] leading-none font-bold tracking-[-0.04em] tabular">{data ? usd(data.total_usd) : "—"}</div>
        <div className="relative mt-5 flex flex-wrap gap-2">
          {[...byMerchant.entries()].map(([m, v]) => (
            <span key={m} className="rounded-full bg-white/18 px-3 py-1 text-[12px] font-medium backdrop-blur">
              {m} · {usd(v)}
            </span>
          ))}
          {data && byMerchant.size === 0 && <span className="text-[12.5px] text-white/70">No completed purchases yet.</span>}
        </div>
      </motion.div>

      <h2 className="title-3 mt-8 mb-3 px-1">Transactions</h2>
      {!data ? (
        <div className="grid h-32 place-items-center text-fg-3">
          <ActivityIndicator size={20} />
        </div>
      ) : data.entries.length === 0 ? (
        <div className="glass rounded-[24px]">
          <Empty icon={Receipt} title="Nothing yet">
            Purchases show up here with who approved them.
          </Empty>
        </div>
      ) : (
        <motion.div initial="hidden" animate="show" variants={stagger(0.03, 0.1)} className="glass mb-12 overflow-hidden rounded-[24px]">
          {data.entries.map((e) => {
            const t = TONE[e.status] ?? { label: e.status, color: "var(--fg-2)" };
            const c = merchantColor(e.merchant);
            return (
              <motion.div key={e.id} variants={fadeUp} className={`${listSep} flex items-center gap-3 px-4 py-3.5 [--inset:68px]`}>
                <span
                  className="grid h-10 w-10 shrink-0 place-items-center rounded-full text-[15px] font-semibold text-white"
                  style={{ background: `linear-gradient(150deg, color-mix(in oklab, ${c} 70%, white), ${c})` }}
                >
                  {e.merchant[0]}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[14px] font-semibold tracking-[-0.01em]">{e.merchant}</div>
                  <div className="truncate text-[12px] text-fg-2">{e.description}</div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11.5px] text-fg-3">
                    <span>{new Date(e.ts).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</span>
                    <span>· approved by {e.approved_by}</span>
                    <Link className="font-medium text-accent hover:underline" href={`/runs/${e.run_id}`}>
                      · View run
                    </Link>
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-[15px] font-semibold tracking-[-0.01em] tabular">{usd(e.amount_usd)}</div>
                  <div className="text-[11.5px] font-semibold" style={{ color: t.color }}>
                    {t.label}
                  </div>
                </div>
              </motion.div>
            );
          })}
        </motion.div>
      )}
    </div>
  );
}
