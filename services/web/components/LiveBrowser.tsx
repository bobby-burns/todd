"use client";

import { useEffect, useState } from "react";
import { motion } from "motion/react";
import { ExternalLink, Eye, Hand, Lock } from "lucide-react";
import { api } from "@/lib/api";
import { spring } from "@/lib/motion";

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

/** The agents' shared Chromium via noVNC, framed like a browser window. "Take control" lets you click and type. */
export function LiveBrowser({
  defaultControl = false,
  className = "",
  fallback,
  pageUrl,
  footer,
}: {
  defaultControl?: boolean;
  className?: string;
  fallback?: string;
  pageUrl?: string | null;
  footer?: React.ReactNode;
}) {
  const url = useLiveUrl();
  const [control, setControl] = useState(defaultControl);
  const src = url ? `${url}${url.includes("?") ? "&" : "?"}view_only=${control ? 0 : 1}` : null;
  let host = "";
  try {
    host = pageUrl ? new URL(pageUrl).host + new URL(pageUrl).pathname.replace(/\/$/, "") : "";
  } catch {
    host = pageUrl ?? "";
  }
  return (
    <div className={`glass flex min-h-0 flex-col overflow-hidden rounded-[28px] ${className}`}>
      <div className="flex items-center gap-2 px-3.5 pt-3.5 pb-3">
        <span className="flex gap-1.5 pr-1">
          <span className="h-3 w-3 rounded-full bg-[#ff5f57]" />
          <span className="h-3 w-3 rounded-full bg-[#febc2e]" />
          <span className="h-3 w-3 rounded-full bg-[#28c840]" />
        </span>
        <div className="flex h-8 min-w-0 flex-1 items-center justify-center gap-1.5 rounded-full bg-fill px-3 text-[12px] text-fg-2">
          {host ? <Lock size={11} className="shrink-0" /> : null}
          <span className="truncate">{host || "Agents' browser"}</span>
        </div>
        <button
          className={`btn btn-sm ${control ? "btn-accent" : "btn-glass"}`}
          onClick={() => setControl(!control)}
          title={control ? "Hand control back to the agents" : "Click and type in this browser (sign-ins, captchas, 2FA)"}
        >
          <motion.span key={String(control)} initial={{ scale: 0.5, rotate: -20 }} animate={{ scale: 1, rotate: 0 }} transition={spring} className="flex">
            {control ? <Hand size={13} /> : <Eye size={13} />}
          </motion.span>
          {control ? "In control" : "Take control"}
        </button>
        {url && (
          <a className="btn btn-glass btn-sm btn-icon w-7" href={url} target="_blank" rel="noreferrer" title="Open in a new tab">
            <ExternalLink size={13} />
          </a>
        )}
      </div>
      <div className="relative mx-2 mb-2 aspect-[16/10] overflow-hidden rounded-[20px] bg-black ring-1 ring-sep">
        {src ? (
          <iframe key={String(control)} src={src} className="absolute inset-0 h-full w-full" title="Live browser" />
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
