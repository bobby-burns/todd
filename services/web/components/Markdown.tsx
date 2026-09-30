"use client";

import { Fragment, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/* A small, safe Markdown renderer for what agents write (summaries, .md files): paragraphs, "- " and "1." lists,
   headings, quotes, code, **bold**, *italic*, `code`, [links](https://…) and bare URLs. No raw HTML, and only
   http(s) links. With a glossary, technical words get a plain-language explanation on hover or tap. */

/* ───────────────────────── Glossary ───────────────────────── */

export type Entry = { term: string; meaning: string; cs?: boolean };

/** Everyday meanings for words agents use a lot. A summary's own "Terms:" line adds to (and overrides) these. */
export const GLOSSARY: Entry[] = [
  { term: "API key", meaning: "A password-like code that lets Todd use a service on your behalf." },
  { term: "API", meaning: "A way for programs to talk to a service directly, without clicking through its website.", cs: true },
  { term: "repository", meaning: "The project's folder of code, kept (for example on GitHub) with its full history." },
  { term: "repo", meaning: "Short for repository: the project's folder of code, with its full history." },
  { term: "pull request", meaning: "A proposed change to the code, waiting to be reviewed and added." },
  { term: "PR", meaning: "Pull request: a proposed change to the code, waiting to be reviewed and added.", cs: true },
  { term: "commit", meaning: "A saved snapshot of changes to the code." },
  { term: "deployed", meaning: "Put online, so people can use the latest version." },
  { term: "deployment", meaning: "Putting the latest version online, so people can use it." },
  { term: "deploy", meaning: "Put the latest version online, so people can use it." },
  { term: "domain", meaning: "A web address, like example.com." },
  { term: "DNS", meaning: "The internet's address book: it points a web address at the computer that hosts the site.", cs: true },
  { term: "CLI", meaning: "A tool you use by typing commands instead of clicking.", cs: true },
  { term: "vault", meaning: "Todd's locked storage for passwords and keys. Agents use them without seeing them." },
  { term: "token", meaning: "A password-like code that proves who you are to a service." },
  { term: "environment variable", meaning: "A setting (often a key) that an app reads when it starts." },
  { term: "env var", meaning: "Environment variable: a setting (often a key) that an app reads when it starts." },
  { term: "sandbox", meaning: "A separate, safe computer where Todd's agents write and run code." },
  { term: "TestFlight", meaning: "Apple's app for trying a new iPhone app before it's in the App Store.", cs: true },
  { term: "App Store Connect", meaning: "Apple's website for managing your apps and sending them to the App Store.", cs: true },
  { term: "EAS", meaning: "Expo's service that builds phone apps in the cloud.", cs: true },
  { term: "Expo", meaning: "A set of tools for building iPhone and Android apps from one project.", cs: true },
  { term: "React Native", meaning: "A way to build iPhone and Android apps from the same code.", cs: true },
  { term: "Next.js", meaning: "A popular toolkit for building websites.", cs: true },
  { term: "Vercel", meaning: "A service that puts websites online.", cs: true },
  { term: "Netlify", meaning: "A service that puts websites online.", cs: true },
  { term: "Stripe", meaning: "A service for taking card payments online.", cs: true },
  { term: "Supabase", meaning: "A ready-made backend: a database, sign-in and file storage for your app.", cs: true },
  { term: "Firebase", meaning: "Google's ready-made backend: a database, sign-in and hosting for your app.", cs: true },
  { term: "webhook", meaning: "An automatic message one service sends another when something happens." },
  { term: "frontend", meaning: "The part of an app people see and click." },
  { term: "backend", meaning: "The behind-the-scenes part of an app that stores data and does the work." },
  { term: "database", meaning: "Where an app keeps its information." },
  { term: "HTTPS", meaning: "The padlock in your browser: the connection to the site is private.", cs: true },
  { term: "SSL", meaning: "What gives a site the padlock in your browser, so the connection is private.", cs: true },
  { term: "bundle identifier", meaning: "The unique name Apple uses for an app, like com.yourname.app." },
  { term: "bundle ID", meaning: "The unique name Apple uses for an app, like com.yourname.app." },
  { term: "npm", meaning: "The main library of ready-made code packages for JavaScript.", cs: true },
  { term: "localhost", meaning: "The computer the code is running on itself, not the internet." },
  { term: "MCP", meaning: "A standard way for AI agents to use another app's features.", cs: true },
  { term: "OAuth", meaning: "The “Sign in with…” approval that lets one service act for you on another.", cs: true },
  { term: "2FA", meaning: "A second check when signing in, like a code sent to your phone.", cs: true },
  { term: "CI", meaning: "Automatic checks that run every time the code changes.", cs: true },
  { term: "JSON", meaning: "A simple text format programs use to pass data around.", cs: true },
];

/** Words to explain in one render: each is explained the first time it appears. Make a fresh one per render. */
export type Glossed = { entries: Entry[]; used: Set<string> };

/** The built-in glossary plus `extra` (a summary's own Terms, which win). */
export function makeGlossary(extra: Entry[] = []): Glossed {
  const seen = new Set<string>();
  const entries = [...extra, ...GLOSSARY]
    .filter((e) => e.term.trim() && (seen.has(e.term.toLowerCase()) ? false : (seen.add(e.term.toLowerCase()), true)))
    .sort((a, b) => b.term.length - a.term.length); // "API key" before "API"
  return { entries, used: new Set() };
}

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function glossText(text: string, g: Glossed | null, key: string): ReactNode {
  if (!g || !text.trim()) return text;
  const open = g.entries.filter((e) => !g.used.has(e.term.toLowerCase()));
  if (!open.length) return text;
  const re = new RegExp(`(?<![\\w.\\-/@])(${open.map((e) => esc(e.term)).join("|")})(?![\\w\\-/@]|\\.\\w)`, "gi");
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    const e = open.find((x) => (x.cs ? x.term === m[1] : x.term.toLowerCase() === m[1].toLowerCase()));
    if (!e || g.used.has(e.term.toLowerCase())) continue;
    g.used.add(e.term.toLowerCase());
    out.push(text.slice(last, m.index), <Term key={`${key}-${m.index}`} word={m[1]} meaning={e.meaning} />);
    last = m.index! + m[1].length;
  }
  if (!out.length) return text;
  out.push(text.slice(last));
  return out;
}

/** A word with a plain-language explanation: dotted underline, explanation on hover, focus or tap. */
export function Term({ word, meaning }: { word: string; meaning: string }) {
  const [at, setAt] = useState<{ x: number; y: number; below: boolean } | null>(null);
  const show = (el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const below = r.top < 120;
    setAt({ x: Math.min(Math.max(r.left + r.width / 2, 150), window.innerWidth - 150), y: below ? r.bottom + 8 : r.top - 8, below });
  };
  return (
    <>
      <span
        tabIndex={0}
        role="button"
        aria-label={`${word}: ${meaning}`}
        className="cursor-help rounded-[3px] underline decoration-fg-3 decoration-dotted underline-offset-[3px] outline-none focus-visible:bg-accent/10"
        onMouseEnter={(e) => show(e.currentTarget)}
        onMouseLeave={() => setAt(null)}
        onFocus={(e) => show(e.currentTarget)}
        onBlur={() => setAt(null)}
        onClick={(e) => (at ? setAt(null) : show(e.currentTarget))}
      >
        {word}
      </span>
      {at &&
        createPortal(
          <span
            role="tooltip"
            className="glass glass-strong pointer-events-none fixed z-[90] block w-max max-w-[280px] rounded-[12px] px-3 py-2 text-[12.5px] leading-snug text-fg shadow-lg"
            style={{ left: at.x, top: at.y, transform: `translate(-50%, ${at.below ? "0" : "-100%"})` }}
          >
            <b className="font-semibold">{word}</b>
            <span className="text-fg-2"> — {meaning}</span>
          </span>,
          document.body,
        )}
    </>
  );
}

/* ───────────────────────── Inline ───────────────────────── */

const INLINE =
  /(`[^`\n]+`)|(\*\*(?=\S)[^*\n]+?\*\*|__(?=\S)[^_\n]+?__)|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()"'`]*[^\s<>()"'`.,;:!?])|(\*(?=[^\s*])[^*\n]*?[^\s*]\*|\*[^\s*]\*)/g;

export function prettyUrl(url: string) {
  try {
    const u = new URL(url);
    const path = u.pathname === "/" ? "" : u.pathname.replace(/\/$/, "");
    const s = u.host.replace(/^www\./, "") + path;
    return s.length > 48 ? s.slice(0, 46) + "…" : s;
  } catch {
    return url;
  }
}

export function Link({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" title={href} className="break-words text-accent underline decoration-accent/30 underline-offset-2 transition-colors hover:decoration-accent">
      {children}
    </a>
  );
}

/** Inline Markdown for one line of text. */
export function Inline({ text, gloss = null }: { text: string; gloss?: Glossed | null }) {
  return <>{inline(text, gloss, "i")}</>;
}

export function inline(text: string, g: Glossed | null, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    const k = `${key}-${n++}`;
    out.push(<Fragment key={`${k}t`}>{glossText(text.slice(last, m.index), g, `${k}g`)}</Fragment>);
    if (m[1]) out.push(<code key={k} className="rounded-[5px] bg-fill px-1 py-px font-mono text-[0.88em] break-words">{m[1].slice(1, -1)}</code>);
    else if (m[2]) out.push(<b key={k} className="font-semibold text-fg">{inline(m[2].slice(2, -2), g, k)}</b>);
    else if (m[3]) out.push(<Link key={k} href={m[4]}>{m[3]}</Link>);
    else if (m[5]) out.push(<Link key={k} href={m[5]}>{prettyUrl(m[5])}</Link>);
    else if (m[6]) out.push(<em key={k}>{inline(m[6].slice(1, -1), g, k)}</em>);
    last = m.index! + m[0].length;
  }
  out.push(<Fragment key={`${key}-end`}>{glossText(text.slice(last), g, `${key}-endg`)}</Fragment>);
  return out;
}

/* ───────────────────────── Blocks ───────────────────────── */

export type Block =
  | { type: "p"; lines: string[] }
  | { type: "list"; ordered: boolean; items: { text: string; depth: number }[] }
  | { type: "h"; level: number; text: string }
  | { type: "quote"; lines: string[] }
  | { type: "code"; text: string }
  | { type: "hr" };

export const BULLET = /^(\s*)([-*•+]|\d+[.)])\s+(.*)$/;

export function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const prev = blocks[blocks.length - 1];
    if (/^\s*```/.test(line)) {
      const body: string[] = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) body.push(lines[i]);
      blocks.push({ type: "code", text: body.join("\n") });
      continue;
    }
    if (!line.trim()) {
      blocks.push({ type: "p", lines: [] }); // a break; empty paragraphs are dropped below
      continue;
    }
    const h = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (h) {
      blocks.push({ type: "h", level: h[1].length, text: h[2] });
      continue;
    }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      blocks.push({ type: "hr" });
      continue;
    }
    const b = line.match(BULLET);
    if (b) {
      const ordered = /\d/.test(b[2]);
      const item = { text: b[3], depth: b[1].replace(/\t/g, "  ").length >= 2 ? 1 : 0 };
      if (prev?.type === "list" && (prev.ordered === ordered || item.depth > 0)) prev.items.push(item);
      else blocks.push({ type: "list", ordered, items: [item] });
      continue;
    }
    const q = line.match(/^\s*>\s?(.*)$/);
    if (q) {
      if (prev?.type === "quote") prev.lines.push(q[1]);
      else blocks.push({ type: "quote", lines: [q[1]] });
      continue;
    }
    if (prev?.type === "list" && /^\s{2,}\S/.test(line)) {
      prev.items[prev.items.length - 1].text += " " + line.trim(); // a wrapped list item
      continue;
    }
    if (prev?.type === "p" && prev.lines.length) prev.lines.push(line.trim());
    else blocks.push({ type: "p", lines: [line.trim()] });
  }
  return blocks.filter((b) => b.type !== "p" || b.lines.length);
}

/** Renders Markdown. `dense` tightens the spacing for small cards; `gloss` explains technical words. */
export function Markdown({ text, className = "", dense = false, gloss = null }: { text: string; className?: string; dense?: boolean; gloss?: Glossed | null }) {
  return <div className={`${dense ? "space-y-1.5" : "space-y-3"} min-w-0 leading-relaxed ${className}`}>{renderBlocks(parseBlocks(text), gloss, dense)}</div>;
}

export function renderBlocks(blocks: Block[], g: Glossed | null, dense = false): ReactNode[] {
  return blocks.map((b, i) => {
    const k = `b${i}`;
    switch (b.type) {
      case "p":
        return (
          <p key={k} className="break-words">
            {b.lines.map((l, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(l, g, `${k}-${j}`)}
              </Fragment>
            ))}
          </p>
        );
      case "list":
        return list(k, b.ordered, b.items, g, dense); // a plain call: words are explained in reading order
      case "h":
        return (
          <div key={k} className={`font-semibold tracking-[-0.012em] text-fg ${b.level <= 1 ? "pt-1 text-[1.35em]" : b.level === 2 ? "pt-1 text-[1.18em]" : "text-[1.05em]"}`}>
            {inline(b.text, g, k)}
          </div>
        );
      case "quote":
        return (
          <blockquote key={k} className="border-l-[3px] border-sep pl-3 text-fg-2">
            {b.lines.map((l, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(l, g, `${k}-${j}`)}
              </Fragment>
            ))}
          </blockquote>
        );
      case "code":
        return (
          <pre key={k} className="well scrollbar-thin overflow-x-auto rounded-[12px] px-3 py-2.5 font-mono text-[0.85em] leading-relaxed text-fg-2">
            {b.text}
          </pre>
        );
      case "hr":
        return <hr key={k} className="border-sep" />;
    }
  });
}

function list(id: string, ordered: boolean, items: { text: string; depth: number }[], g: Glossed | null, dense: boolean) {
  let n = 0;
  return (
    <ul key={id} className={dense ? "space-y-1" : "space-y-1.5"}>
      {items.map((it, j) => {
        if (it.depth === 0) n++;
        return (
          <li key={j} className={`flex gap-2.5 ${it.depth ? "pl-5" : ""}`}>
            {ordered && it.depth === 0 ? (
              <span className="min-w-[1.1em] shrink-0 text-right font-medium text-fg-3 tabular">{n}.</span>
            ) : (
              <span className={`mt-[0.62em] h-[5px] w-[5px] shrink-0 rounded-full ${it.depth ? "bg-fg-3/60" : "bg-fg-3"}`} />
            )}
            <span className="min-w-0 flex-1 break-words">{inline(it.text, g, `${id}-${j}`)}</span>
          </li>
        );
      })}
    </ul>
  );
}
