"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { motion } from "motion/react";
import { KeyRound, Monitor, Moon, Receipt, Rocket, Settings2, Sun, Zap } from "lucide-react";
import { api, type Interaction, type Run } from "@/lib/api";
import { spring } from "@/lib/motion";
import { Segmented, StatusDot } from "./ui";

const items = [
  { href: "/", label: "Runs", icon: Zap },
  { href: "/accounts", label: "Accounts", icon: KeyRound },
  { href: "/ledger", label: "Ledger", icon: Receipt },
  { href: "/settings", label: "Settings", icon: Settings2 },
  { href: "/onboarding", label: "Setup", icon: Rocket },
];

const isActive = (path: string, href: string) => (href === "/" ? path === "/" || path.startsWith("/runs") : path.startsWith(href));

function usePolled<T>(path: string, ms: number, init: T): T {
  const [v, setV] = useState<T>(init);
  useEffect(() => {
    let alive = true;
    const tick = () =>
      api<T>(path)
        .then((r) => alive && setV(r))
        .catch(() => {});
    tick();
    const t = setInterval(tick, ms);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [path, ms]);
  return v;
}

export function Logo({ size = 34 }: { size?: number }) {
  return (
    <span
      className="grid shrink-0 place-items-center"
      style={{
        width: size,
        height: size,
        borderRadius: size * 0.3,
        background: "linear-gradient(150deg, #5ac8fa 0%, #0a84ff 45%, #5e5ce6 100%)",
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / 0.45), 0 6px 16px -6px rgb(10 132 255 / 0.7)",
      }}
    >
      <svg viewBox="0 0 24 24" width={size * 0.62} height={size * 0.62} aria-hidden>
        <path d="M6.5 7.5h11M12 7.5v10" stroke="white" strokeWidth="2.3" strokeLinecap="round" />
        <circle cx="6.2" cy="7.5" r="2.4" fill="white" />
        <circle cx="17.8" cy="7.5" r="2.4" fill="white" />
        <circle cx="12" cy="17.8" r="2.4" fill="white" />
      </svg>
    </span>
  );
}

type Theme = "system" | "light" | "dark";
export function ThemeSwitch() {
  const [theme, setTheme] = useState<Theme>("system");
  useEffect(() => {
    try {
      const t = localStorage.getItem("todd.theme");
      if (t === "light" || t === "dark") setTheme(t);
    } catch {}
  }, []);
  const set = (t: Theme) => {
    setTheme(t);
    try {
      if (t === "system") {
        localStorage.removeItem("todd.theme");
        delete document.documentElement.dataset.theme;
      } else {
        localStorage.setItem("todd.theme", t);
        document.documentElement.dataset.theme = t;
      }
    } catch {}
  };
  return (
    <Segmented
      id="theme"
      size="sm"
      value={theme}
      onChange={set}
      options={[
        { value: "system", icon: Monitor, title: "Match system" },
        { value: "light", icon: Sun, title: "Light" },
        { value: "dark", icon: Moon, title: "Dark" },
      ]}
    />
  );
}

export function Nav() {
  const path = usePathname();
  const pending = usePolled<Interaction[]>("/interactions", 4000, []).length;
  const runs = usePolled<Run[]>("/runs", 5000, []);
  const recent = runs.slice(0, 6);

  return (
    <aside className="sticky top-0 hidden h-dvh shrink-0 p-3 md:block md:w-[92px] lg:w-[272px]">
      <div className="glass flex h-full flex-col rounded-[30px] p-3">
        <Link href="/" className="mb-5 flex items-center gap-3 px-1.5 pt-1.5 max-lg:justify-center">
          <Logo />
          <div className="hidden lg:block">
            <div className="title-3 leading-none">Todd</div>
            <div className="mt-1 text-[11px] text-fg-3">agent operator</div>
          </div>
        </Link>

        <nav className="flex flex-col gap-0.5">
          {items.map(({ href, label, icon: Icon }) => {
            const active = isActive(path, href);
            return (
              <Link
                key={href}
                href={href}
                title={label}
                className={`relative flex h-10 items-center gap-3 rounded-[14px] px-3 text-[14px] font-medium tracking-[-0.01em] transition-colors max-lg:justify-center ${
                  active ? "text-fg" : "text-fg-2 hover:text-fg"
                }`}
              >
                {active && (
                  <motion.span
                    layoutId="nav-active"
                    transition={spring}
                    className="absolute inset-0 rounded-[14px] bg-[var(--control-hover)] shadow-[var(--control-shadow)]"
                  />
                )}
                <Icon size={18} strokeWidth={2} className={`relative ${active ? "text-accent" : ""}`} />
                <span className="relative hidden lg:block">{label}</span>
                {href === "/" && pending > 0 && (
                  <span className="relative ml-auto grid h-5 min-w-5 place-items-center rounded-full bg-orange px-1.5 text-[11px] font-semibold text-white max-lg:absolute max-lg:top-0.5 max-lg:right-1.5 max-lg:h-4 max-lg:min-w-4 max-lg:text-[10px]">
                    {pending}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>

        {recent.length > 0 && (
          <div className="mt-6 hidden min-h-0 flex-1 flex-col lg:flex">
            <div className="eyebrow mb-1.5 px-3">Recent</div>
            <div className="scrollbar-thin -mx-1 min-h-0 overflow-y-auto px-1">
              {recent.map((r) => {
                const active = path === `/runs/${r.id}`;
                return (
                  <Link
                    key={r.id}
                    href={`/runs/${r.id}`}
                    className={`flex items-center gap-2.5 rounded-[12px] px-3 py-2 text-[13px] transition-colors ${
                      active ? "bg-fill text-fg" : "text-fg-2 hover:bg-fill hover:text-fg"
                    }`}
                  >
                    <StatusDot status={r.status} live={r.active} size={6} />
                    <span className="truncate">{r.title}</span>
                  </Link>
                );
              })}
            </div>
          </div>
        )}

        <div className="mt-auto flex flex-col items-center gap-3 pt-4 lg:flex-row lg:justify-between lg:px-1">
          <span className="hidden text-[11px] font-medium text-fg-3 lg:block">v0.4 · open source</span>
          <div className="max-lg:hidden">
            <ThemeSwitch />
          </div>
        </div>
      </div>
    </aside>
  );
}

/** Floating glass tab bar on phones. */
export function TabBar() {
  const path = usePathname();
  const pending = usePolled<Interaction[]>("/interactions", 5000, []).length;
  return (
    <nav className="fixed inset-x-3 bottom-[max(12px,env(safe-area-inset-bottom))] z-40 md:hidden">
      <div className="glass glass-strong mx-auto flex h-[62px] max-w-md items-center justify-around rounded-full px-2">
        {items.map(({ href, label, icon: Icon }) => {
          const active = isActive(path, href);
          return (
            <Link key={href} href={href} className="relative flex h-[50px] flex-1 flex-col items-center justify-center gap-0.5">
              {active && (
                <motion.span
                  layoutId="tab-active"
                  transition={spring}
                  className="absolute inset-0 rounded-full bg-[var(--control-hover)] shadow-[var(--control-shadow)]"
                />
              )}
              <Icon size={20} strokeWidth={2} className={`relative ${active ? "text-accent" : "text-fg-2"}`} />
              <span className={`relative text-[10px] font-medium ${active ? "text-accent" : "text-fg-2"}`}>{label}</span>
              {href === "/" && pending > 0 && (
                <span className="absolute top-1 right-[calc(50%-18px)] grid h-4 min-w-4 place-items-center rounded-full bg-orange px-1 text-[10px] font-semibold text-white">
                  {pending}
                </span>
              )}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
