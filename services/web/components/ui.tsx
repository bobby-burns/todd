"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "motion/react";
import { Check, Workflow, type LucideIcon } from "lucide-react";
import { initials } from "@/lib/api";
import { spring, softSpring } from "@/lib/motion";

/* ───────────────────────── Status ───────────────────────── */

type StatusDef = { label: string; color: string; live?: boolean };
export const STATUS: Record<string, StatusDef> = {
  queued: { label: "Queued", color: "var(--fg-2)" },
  running: { label: "Running", color: "var(--accent)", live: true },
  waiting: { label: "Needs you", color: "var(--orange)", live: true },
  paused: { label: "Paused", color: "var(--indigo)" },
  succeeded: { label: "Done", color: "var(--green)" },
  failed: { label: "Failed", color: "var(--red)" },
  cancelled: { label: "Stopped", color: "var(--fg-2)" },
  interrupted: { label: "Interrupted", color: "var(--orange)" },
};

export function StatusDot({ status, live = true, size = 7 }: { status: string; live?: boolean; size?: number }) {
  const s = STATUS[status] ?? STATUS.queued;
  return (
    <span
      className={`relative inline-block shrink-0 rounded-full ${live && s.live ? "pulse-ring" : ""}`}
      style={{ width: size, height: size, background: s.color, color: s.color }}
    />
  );
}

export function StatusPill({ status, live = true, className = "" }: { status: string; live?: boolean; className?: string }) {
  const s = STATUS[status] ?? { label: status, color: "var(--fg-2)" };
  return (
    <motion.span
      layout
      transition={spring}
      className={`inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full pr-2.5 pl-2 text-[12px] font-semibold tracking-[-0.01em] ${className}`}
      style={{ color: s.color, background: `color-mix(in oklab, ${s.color} 15%, transparent)` }}
    >
      <StatusDot status={status} live={live} size={6} />
      {s.label}
    </motion.span>
  );
}

/** Kept for older imports. */
export const StatusBadge = ({ status }: { status: string }) => <StatusPill status={status} />;

/* ───────────────────────── Avatars & tiles ───────────────────────── */

export function AgentAvatar({ name, color, size = 28, planner = false }: { name: string; color: string; size?: number; planner?: boolean }) {
  return (
    <span
      className="relative grid shrink-0 place-items-center font-semibold text-white"
      style={{
        width: size,
        height: size,
        borderRadius: size * 0.3,
        fontSize: size * 0.38,
        letterSpacing: "-0.02em",
        background: `linear-gradient(150deg, color-mix(in oklab, ${color} 72%, white), ${color} 55%, color-mix(in oklab, ${color} 80%, black))`,
        boxShadow: `inset 0 1px 0 rgb(255 255 255 / 0.35), 0 4px 12px -4px color-mix(in oklab, ${color} 70%, transparent)`,
      }}
    >
      {planner ? <Workflow size={size * 0.52} strokeWidth={2.2} /> : initials(name)}
    </span>
  );
}

export function IconTile({ icon: Icon, color, size = 28 }: { icon: LucideIcon; color: string; size?: number }) {
  return (
    <span
      className="grid shrink-0 place-items-center text-white"
      style={{
        width: size,
        height: size,
        borderRadius: size * 0.28,
        background: `linear-gradient(160deg, color-mix(in oklab, ${color} 78%, white), ${color})`,
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / 0.3)",
      }}
    >
      <Icon size={size * 0.56} strokeWidth={2.1} />
    </span>
  );
}

/* ───────────────────────── Controls ───────────────────────── */

export function Segmented<T extends string>({
  value,
  onChange,
  options,
  id,
  size = "md",
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label?: React.ReactNode; icon?: LucideIcon; title?: string }[];
  id: string;
  size?: "sm" | "md";
}) {
  return (
    <div className="inline-flex shrink-0 rounded-full bg-fill p-[3px]" role="tablist">
      {options.map((o) => {
        const on = o.value === value;
        const Icon = o.icon;
        return (
          <button
            key={o.value}
            role="tab"
            aria-selected={on}
            title={o.title}
            onClick={() => onChange(o.value)}
            className={`relative isolate flex items-center gap-1.5 rounded-full font-medium tracking-[-0.01em] transition-colors ${
              size === "sm" ? "h-6 px-2.5 text-[12px]" : "h-7 px-3 text-[12.5px]"
            } ${on ? "text-fg" : "text-fg-2 hover:text-fg"}`}
          >
            {on && (
              <motion.span
                layoutId={`seg-${id}`}
                transition={spring}
                className="absolute inset-0 -z-10 rounded-full bg-[var(--control-hover)] shadow-[var(--control-shadow)]"
              />
            )}
            {Icon && <Icon size={size === "sm" ? 12 : 13} strokeWidth={2.2} />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function Switch({ checked, onChange, disabled }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="relative h-[28px] w-[48px] shrink-0 rounded-full transition-colors duration-200 disabled:opacity-40"
      style={{ background: checked ? "var(--green)" : "var(--fill-2)" }}
    >
      <motion.span
        animate={{ x: checked ? 20 : 0 }}
        transition={spring}
        className="absolute top-[2px] left-[2px] h-[24px] w-[24px] rounded-full bg-white"
        style={{ boxShadow: "0 3px 8px rgb(0 0 0 / 0.15), 0 1px 1px rgb(0 0 0 / 0.16)" }}
      />
    </button>
  );
}

/* ───────────────────────── Feedback ───────────────────────── */

export function ActivityIndicator({ size = 14, className = "" }: { size?: number; className?: string }) {
  return (
    <span className={`spinner ${className}`} style={{ ["--s" as string]: `${size}px` }} aria-label="Loading">
      {Array.from({ length: 8 }, (_, i) => (
        <i key={i} style={{ transform: `rotate(${i * 45}deg)`, animationDelay: `${-0.8 + i * 0.1}s` }} />
      ))}
    </span>
  );
}

export function ProgressRing({ value, size = 36, stroke = 3.5, color = "var(--green)" }: { value: number; size?: number; stroke?: number; color?: string }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return (
    <svg width={size} height={size} className="-rotate-90 shrink-0">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--fill-2)" strokeWidth={stroke} />
      <motion.circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={c}
        initial={{ strokeDashoffset: c }}
        animate={{ strokeDashoffset: c * (1 - Math.max(0, Math.min(1, value))) }}
        transition={softSpring}
      />
    </svg>
  );
}

export function Toast({ message }: { message: string | null }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return null;
  return createPortal(
    <div className="pointer-events-none fixed inset-x-0 top-5 z-[60] flex justify-center">
      <AnimatePresence>
        {message && (
          <motion.div
            initial={{ opacity: 0, y: -16, scale: 0.92 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -12, scale: 0.96 }}
            transition={spring}
            className="glass glass-strong flex h-10 items-center gap-2 rounded-full pr-4 pl-3 text-[13px] font-medium"
          >
            <span className="grid h-5 w-5 place-items-center rounded-full bg-green text-white">
              <Check size={12} strokeWidth={3} />
            </span>
            {message}
          </motion.div>
        )}
      </AnimatePresence>
    </div>,
    document.body,
  );
}

/* ───────────────────────── Layout ───────────────────────── */

export function PageHeader({
  title,
  subtitle,
  actions,
  eyebrow,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  eyebrow?: React.ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-end gap-x-6 gap-y-4 pt-8 pb-7 md:pt-12">
      <div className="min-w-0 flex-1">
        {eyebrow && <div className="eyebrow mb-2">{eyebrow}</div>}
        <h1 className="title-large">{title}</h1>
        {subtitle && <p className="mt-2 max-w-2xl text-[15px] leading-relaxed text-fg-2">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Section({
  title,
  desc,
  children,
  id,
  icon,
  color = "var(--accent)",
}: {
  title: string;
  desc?: React.ReactNode;
  children: React.ReactNode;
  id?: string;
  icon?: LucideIcon;
  color?: string;
}) {
  return (
    <section id={id} className="scroll-mt-8">
      <div className="mb-3 flex items-center gap-2.5 px-1">
        {icon && <IconTile icon={icon} color={color} size={26} />}
        <h2 className="title-3">{title}</h2>
      </div>
      <div className="glass rounded-[24px] p-5 md:p-6">
        {desc && <p className="-mt-0.5 mb-5 max-w-3xl text-[13px] leading-relaxed text-fg-2">{desc}</p>}
        {children}
      </div>
    </section>
  );
}

/** Inset-grouped list row (iOS Settings style) with an inset hairline separator. */
export const listSep =
  "relative after:absolute after:right-0 after:bottom-0 after:left-[var(--inset,64px)] after:h-px after:bg-sep last:after:hidden";
export const listRow = `${listSep} flex items-center gap-3 px-4 py-3 transition-colors`;

export function Empty({ icon: Icon, title, children }: { icon: LucideIcon; title: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center px-6 py-12 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-2xl bg-fill text-fg-2">
        <Icon size={22} />
      </span>
      <div className="title-3 mt-4">{title}</div>
      {children && <div className="mt-1.5 max-w-sm text-[13px] leading-relaxed text-fg-2">{children}</div>}
    </div>
  );
}
