"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion } from "motion/react";
import { ExternalLink, Eye, Hand, Lock, Maximize2 } from "lucide-react";
import { api } from "@/lib/api";
import { softSpring, spring } from "@/lib/motion";

let cachedUrl: string | null = null;

export function useLiveUrl() {
  const [url, setUrl] = useState<string | null>(cachedUrl);
  useEffect(() => {
    if (cachedUrl) return;
    api<{ browser_live_url: string }>("/meta")
      .then((m) => {
        cachedUrl = m.browser_live_url;
        setUrl(cachedUrl);
      })
      .catch(() => {});
  }, []);
  return url;
}

/** noVNC URL for viewing or controlling. noVNC treats any view_only value as "on" (even "0"), so control omits it. */
function frameSrc(url: string, control: boolean) {
  return control ? url : `${url}${url.includes("?") ? "&" : "?"}view_only=1`;
}

function hostOf(pageUrl?: string | null) {
  if (!pageUrl) return "";
  try {
    const u = new URL(pageUrl);
    return u.host + u.pathname.replace(/\/$/, "");
  } catch {
    return pageUrl;
  }
}

type OpenRequest = { pageUrl?: string | null; note?: string | null };
const OPEN_EVENT = "todd:take-control";

/** Open the agents' browser big, with the human in control (from anywhere: a card, a sign-in prompt, a CLI row). */
export function openLiveBrowser(req: OpenRequest = {}) {
  window.dispatchEvent(new CustomEvent<OpenRequest>(OPEN_EVENT, { detail: req }));
}

function TrafficLights() {
  return (
    <span className="flex gap-1.5 pr-1">
      <span className="h-3 w-3 rounded-full bg-[#ff5f57]" />
      <span className="h-3 w-3 rounded-full bg-[#febc2e]" />
      <span className="h-3 w-3 rounded-full bg-[#28c840]" />
    </span>
  );
}

function AddressBar({ host }: { host: string }) {
  return (
    <div className="flex h-8 min-w-0 flex-1 items-center justify-center gap-1.5 rounded-full bg-fill px-3 text-[12px] text-fg-2">
      {host ? <Lock size={11} className="shrink-0" /> : null}
      <span className="truncate">{host || "Agents' browser"}</span>
    </div>
  );
}

/** The agents' shared Chromium via noVNC, framed like a browser window. "Take control" opens it big and interactive. */
export function LiveBrowser({
  defaultControl = false,
  className = "",
  fallback,
  pageUrl,
  footer,
}: {
  /** Interactive in place (Setup, Accounts), with an Expand button for the big window. */
  defaultControl?: boolean;
  className?: string;
  fallback?: string;
  pageUrl?: string | null;
  footer?: React.ReactNode;
}) {
  const url = useLiveUrl();
  const [control, setControl] = useState(defaultControl);
  const host = hostOf(pageUrl);
  return (
    <div className={`glass flex min-h-0 flex-col overflow-hidden rounded-[28px] ${className}`}>
      <div className="flex items-center gap-2 px-3.5 pt-3.5 pb-3">
        <TrafficLights />
        <AddressBar host={host} />
        {defaultControl && (
          <button
            className={`btn btn-sm ${control ? "btn-accent" : "btn-glass"}`}
            onClick={() => setControl(!control)}
            title={control ? "Watch only (clicks won't reach the browser)" : "Click and type in this browser"}
          >
            <motion.span key={String(control)} initial={{ scale: 0.5, rotate: -20 }} animate={{ scale: 1, rotate: 0 }} transition={spring} className="flex">
              {control ? <Hand size={13} /> : <Eye size={13} />}
            </motion.span>
            {control ? "In control" : "Watching"}
          </button>
        )}
        <button
          className={`btn btn-sm ${defaultControl ? "btn-glass btn-icon w-7" : "btn-glass"}`}
          onClick={() => openLiveBrowser({ pageUrl })}
          title="Open big and take control (sign-ins, captchas, 2FA, or to help an agent)"
        >
          {defaultControl ? (
            <Maximize2 size={13} />
          ) : (
            <>
              <Hand size={13} /> Take control
            </>
          )}
        </button>
        {url && (
          <a className="btn btn-glass btn-sm btn-icon w-7" href={url} target="_blank" rel="noreferrer" title="Open in a new tab">
            <ExternalLink size={13} />
          </a>
        )}
      </div>
      <div className="relative mx-2 mb-2 aspect-[16/10] overflow-hidden rounded-[20px] bg-black ring-1 ring-sep">
        {url ? (
          <iframe key={String(control)} src={frameSrc(url, control)} className="absolute inset-0 h-full w-full" title="Live browser" />
        ) : fallback ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={fallback} alt="latest browser screenshot" className="h-full w-full object-contain" />
        ) : (
          <div className="grid h-full place-items-center text-[12px] text-white/50">Browser view unavailable</div>
        )}
        {control && <div className="pointer-events-none absolute inset-0 rounded-[20px] ring-2 ring-accent ring-inset" />}
      </div>
      {footer && <div className="px-4 pt-1 pb-3.5 text-[12px] leading-snug text-fg-2">{footer}</div>}
    </div>
  );
}

/** Mounted once in the layout: the big, interactive browser window that openLiveBrowser() shows. */
export function BrowserOverlayHost() {
  const url = useLiveUrl();
  const [req, setReq] = useState<OpenRequest | null>(null);
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
    const onOpen = (e: Event) => setReq((e as CustomEvent<OpenRequest>).detail ?? {});
    window.addEventListener(OPEN_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_EVENT, onOpen);
  }, []);
  useEffect(() => {
    if (!req) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [req]);
  if (!mounted) return null;
  return createPortal(
    <AnimatePresence>
      {req && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[80] grid place-items-center bg-black/45 p-3 backdrop-blur-xl md:p-6"
          role="dialog"
          aria-modal="true"
          aria-label="Agents' browser"
        >
          <motion.div
            initial={{ scale: 0.94, opacity: 0, y: 12 }}
            animate={{ scale: 1, opacity: 1, y: 0 }}
            exit={{ scale: 0.96, opacity: 0 }}
            transition={softSpring}
            className="glass glass-strong flex w-full flex-col overflow-hidden rounded-[28px]"
            style={{ maxWidth: "min(100%, calc((100dvh - 150px) * 1.6))" }}
          >
            <div className="flex items-center gap-2 px-3.5 pt-3.5 pb-3">
              <TrafficLights />
              <AddressBar host={hostOf(req.pageUrl)} />
              <span className="hidden items-center gap-1 rounded-full bg-accent/15 px-2.5 py-1 text-[11.5px] font-semibold text-accent sm:inline-flex">
                <Hand size={12} /> You&apos;re in control
              </span>
              {url && (
                <a className="btn btn-glass btn-sm btn-icon w-7" href={url} target="_blank" rel="noreferrer" title="Open in a new tab">
                  <ExternalLink size={13} />
                </a>
              )}
              <button className="btn btn-accent btn-sm" onClick={() => setReq(null)} title="Close and hand the browser back to the agents">
                Done
              </button>
            </div>
            {req.note && <div className="mx-3.5 mb-2.5 rounded-[14px] bg-orange/12 px-3 py-2 text-[12.5px] leading-snug text-fg">{req.note}</div>}
            <div className="relative mx-2 mb-2 aspect-[16/10] overflow-hidden rounded-[20px] bg-black ring-2 ring-accent">
              {url ? (
                <iframe src={frameSrc(url, true)} className="absolute inset-0 h-full w-full" title="Live browser (in control)" allow="clipboard-read; clipboard-write" />
              ) : (
                <div className="grid h-full place-items-center text-[12px] text-white/50">Browser view unavailable</div>
              )}
            </div>
            <div className="px-4 pt-1 pb-3 text-[12px] leading-snug text-fg-2">
              Clicks and typing go to the agents&apos; real browser. Press <b className="font-semibold text-fg">Done</b> when you&apos;re finished; agents carry on from there.
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
