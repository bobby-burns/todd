"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { usePathname, useRouter } from "next/navigation";
import { AnimatePresence, motion } from "motion/react";
import { Copy, Ellipsis, Pencil, RotateCw, Square, Trash2 } from "lucide-react";
import { api, type Run } from "@/lib/api";
import { spring } from "@/lib/motion";

type Mode = "menu" | "rename" | "delete";

/** "…" on a run: rename, run again, stop, delete. */
export function RunMenu({ run, onChanged, className = "" }: { run: Run; onChanged?: () => void; className?: string }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>("menu");
  const [title, setTitle] = useState(run.title);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const path = usePathname();

  const close = () => {
    setOpen(false);
    setMode("menu");
    setErr(null);
  };

  useLayoutEffect(() => {
    if (!open || !btn.current) return;
    const r = btn.current.getBoundingClientRect();
    const width = 244;
    setPos({ top: Math.min(r.bottom + 6, window.innerHeight - 220), left: Math.max(12, Math.min(r.right - width, window.innerWidth - width - 12)) });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!pop.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    const onScroll = (e: Event) => !pop.current?.contains(e.target as Node) && close();
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  async function run_(fn: () => Promise<void>) {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e: any) {
      setErr(e.message);
      setBusy(false);
      return;
    }
    setBusy(false);
    close();
    onChanged?.();
  }

  const rename = () =>
    run_(async () => {
      await api(`/runs/${run.id}`, { method: "PATCH", json: { title } });
    });
  const again = () =>
    run_(async () => {
      const r = await api<Run>("/runs", { method: "POST", json: { prompt: run.prompt, budget_usd: run.budget_usd } });
      router.push(`/runs/${r.id}`);
    });
  const stop = () =>
    run_(async () => {
      await api(`/runs/${run.id}/cancel`, { method: "POST" });
    });
  const remove = () =>
    run_(async () => {
      await api(`/runs/${run.id}`, { method: "DELETE" });
      if (path === `/runs/${run.id}`) router.push("/");
    });

  const item = "flex h-9 w-full items-center gap-2.5 rounded-[10px] px-2.5 text-left text-[13px] font-medium transition-colors hover:bg-fill disabled:opacity-40";

  return (
    <>
      <button
        ref={btn}
        className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-fg-2 transition-colors hover:bg-fill hover:text-fg ${open ? "bg-fill text-fg" : ""} ${className}`}
        title="Run options"
        aria-label={`Options for ${run.title}`}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setTitle(run.title);
          setOpen(!open);
        }}
      >
        <Ellipsis size={15} />
      </button>
      {typeof document !== "undefined" &&
        createPortal(
          <AnimatePresence>
            {open && pos && (
              <motion.div
                ref={pop}
                initial={{ opacity: 0, scale: 0.94, y: -4 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.96 }}
                transition={spring}
                style={{ top: pos.top, left: pos.left, width: 244, transformOrigin: "top right" }}
                className="glass glass-strong fixed z-[75] rounded-[18px] p-1.5"
                onClick={(e) => e.stopPropagation()}
              >
                {mode === "menu" && (
                  <div className="flex flex-col">
                    <button className={item} onClick={() => setMode("rename")}>
                      <Pencil size={14} className="text-fg-2" /> Rename
                    </button>
                    <button className={item} disabled={busy} onClick={again} title="Start a new run with the same prompt and budget">
                      <RotateCw size={14} className="text-fg-2" /> Run again
                    </button>
                    <button
                      className={item}
                      onClick={() => {
                        navigator.clipboard?.writeText(run.prompt).catch(() => {});
                        close();
                      }}
                    >
                      <Copy size={14} className="text-fg-2" /> Copy prompt
                    </button>
                    {run.active && (
                      <button className={item} disabled={busy} onClick={stop}>
                        <Square size={12} fill="currentColor" className="ml-px text-fg-2" /> Stop
                      </button>
                    )}
                    <div className="mx-2 my-1 h-px bg-sep" />
                    <button className={`${item} text-red hover:bg-red/10`} onClick={() => setMode("delete")}>
                      <Trash2 size={14} /> Delete…
                    </button>
                  </div>
                )}
                {mode === "rename" && (
                  <form
                    className="p-1.5"
                    onSubmit={(e) => {
                      e.preventDefault();
                      if (title.trim()) rename();
                    }}
                  >
                    <div className="eyebrow mb-1.5 px-0.5">Rename run</div>
                    <input className="field h-9 w-full rounded-[10px] px-3 text-[13px]" autoFocus maxLength={120} value={title} onChange={(e) => setTitle(e.target.value)} />
                    <div className="mt-2 flex justify-end gap-1.5">
                      <button type="button" className="btn btn-plain btn-sm" onClick={() => setMode("menu")}>
                        Cancel
                      </button>
                      <button className="btn btn-accent btn-sm" disabled={busy || !title.trim()}>
                        Save
                      </button>
                    </div>
                  </form>
                )}
                {mode === "delete" && (
                  <div className="p-1.5">
                    <div className="text-[13px] font-semibold">Delete this run?</div>
                    <p className="mt-1 text-[12px] leading-snug text-fg-2">
                      {run.active ? "It will be stopped first. " : ""}Its agents, messages and screenshots are removed. Ledger entries and files the agents made stay.
                    </p>
                    <div className="mt-2.5 flex justify-end gap-1.5">
                      <button className="btn btn-plain btn-sm" onClick={() => setMode("menu")}>
                        Cancel
                      </button>
                      <button className="btn btn-danger btn-sm" disabled={busy} onClick={remove}>
                        Delete
                      </button>
                    </div>
                  </div>
                )}
                {err && <p className="px-2 pt-1 pb-1.5 text-[11.5px] text-red">{err}</p>}
              </motion.div>
            )}
          </AnimatePresence>,
          document.body,
        )}
    </>
  );
}
