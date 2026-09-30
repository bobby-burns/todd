"use client";

import { useEffect, useState } from "react";
import { motion } from "motion/react";
import { ArrowUpRight, Check, Circle, Rocket } from "lucide-react";
import { api } from "@/lib/api";
import { softSpring } from "@/lib/motion";

/* For a website a run built: what's in place for real visitors, read from the project's files (titles and
   descriptions, a social preview, icons, robots.txt, sitemap.xml, llms.txt, a 404 page, security headers…). A missing
   item, or "Add the missing ones", puts a prompt in the Continue box. */

type Item = { id: string; label: string; ok: boolean; where: string | null; fix: string; optional: boolean };
type Check_ = { web: boolean; project?: string; framework?: string; items: Item[]; missing?: number; prompt?: string };

export function LaunchChecklist({ runId, onPick }: { runId: string; onPick: (text: string) => void }) {
  const [data, setData] = useState<Check_ | null>(null);
  useEffect(() => {
    let alive = true;
    api<Check_>(`/runs/${runId}/launch-check`)
      .then((d) => alive && setData(d))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [runId]);
  if (!data?.web || !data.items.length) return null;
  const done = data.items.filter((i) => i.ok).length;
  const missing = data.items.filter((i) => !i.ok && !i.optional);
  return (
    <section className="mt-5">
      <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 px-1">
        <Rocket size={13} className="text-accent" />
        <h4 className="text-[12.5px] font-semibold text-fg-2">Ready for real visitors?</h4>
        <span className="text-[12px] text-fg-3 tabular">
          {done} of {data.items.length} in place{data.framework ? ` · ${data.framework}` : ""}
        </span>
        {missing.length > 0 && data.prompt && (
          <button className="btn btn-accent btn-sm ml-auto h-7" onClick={() => onPick(data.prompt!)}>
            Add the {missing.length} missing <ArrowUpRight size={13} />
          </button>
        )}
      </div>
      <div className="grid gap-1 rounded-[18px] bg-fill/40 p-2 sm:grid-cols-2">
        {data.items.map((it, i) => (
          <motion.button
            key={it.id}
            type="button"
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ ...softSpring, delay: 0.02 * i }}
            disabled={it.ok}
            onClick={() => onPick(`Add ${it.fix} to the site and check it on localhost. If it's already online, deploy it again the same way (ask me first).`)}
            title={it.ok ? `Found in ${it.where}` : `Tap to ask Todd to add ${it.fix}`}
            className="flex min-w-0 items-center gap-2 rounded-[12px] px-2.5 py-1.5 text-left transition-colors enabled:hover:bg-fill disabled:cursor-default"
          >
            {it.ok ? (
              <span className="grid h-[18px] w-[18px] shrink-0 place-items-center rounded-full bg-green text-white">
                <Check size={11} strokeWidth={3.2} />
              </span>
            ) : (
              <Circle size={18} className="shrink-0 text-fg-3" strokeWidth={1.8} />
            )}
            <span className={`min-w-0 flex-1 truncate text-[13px] ${it.ok ? "text-fg" : "text-fg-2"}`}>{it.label}</span>
            {it.ok ? (
              <span className="hidden max-w-[45%] shrink-0 truncate font-mono text-[11px] text-fg-3 md:block">{it.where}</span>
            ) : (
              <span className="shrink-0 text-[11px] font-medium text-fg-3">{it.optional ? "recommended" : "missing"}</span>
            )}
          </motion.button>
        ))}
      </div>
    </section>
  );
}
