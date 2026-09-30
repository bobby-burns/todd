"use client";

import type { ReactNode } from "react";
import { BookOpen, CircleCheck, ExternalLink, Info, Lightbulb, ListChecks, Package, Users, Wrench } from "lucide-react";
import { inline, makeGlossary, parseBlocks, prettyUrl, renderBlocks, BULLET, type Entry, type Glossed } from "./Markdown";

/* An agent's or run's recap ("In plain words: … / Done: … / Outputs: … / How: … / Left / needs you: … / Terms: …"),
   rendered as sections instead of a wall of text. Each section's body is Markdown; older one-line recaps with
   " · " between items still read as lists. Technical words get a plain-language explanation on hover. */

type Kind = "plain" | "done" | "outputs" | "agents" | "how" | "next" | "terms" | "other";
type Section = { kind: Kind; label: string; body: string };

const KINDS: Record<string, Kind> = {
  "in plain words": "plain", "plain words": "plain", "in short": "plain", "tl;dr": "plain", tldr: "plain",
  done: "done", summary: "done", result: "done", results: "done", answer: "done",
  outputs: "outputs", output: "outputs", links: "outputs", deliverables: "outputs",
  agents: "agents",
  how: "how",
  "left / needs you": "next", left: "next", next: "next", "next steps": "next", "needs you": "next",
  "left/needs you": "next",
  terms: "terms", "words to know": "terms", glossary: "terms",
};

const LOOK: Record<Kind, { title: string; short: string; icon: typeof Info; color: string }> = {
  plain: { title: "In plain words", short: "In short", icon: Lightbulb, color: "var(--accent)" },
  done: { title: "What got done", short: "Done", icon: CircleCheck, color: "var(--green)" },
  outputs: { title: "What you got", short: "Outputs", icon: Package, color: "var(--accent)" },
  agents: { title: "Who did what", short: "Agents", icon: Users, color: "var(--indigo)" },
  how: { title: "How it was done", short: "How", icon: Wrench, color: "var(--fg-3)" },
  next: { title: "What's next for you", short: "Next", icon: ListChecks, color: "var(--orange)" },
  terms: { title: "Words to know", short: "Terms", icon: BookOpen, color: "var(--purple)" },
  other: { title: "", short: "", icon: Info, color: "var(--fg-3)" },
};

const LABEL = /^\s*(?:#{1,4}\s*)?(?:\*\*|__)?([A-Za-z][A-Za-z /;']{1,28}?)(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$/;
const HEADING = /^\s*#{1,4}\s+(.+?)\s*:?\s*$/;

export function parseSummary(text: string): Section[] {
  const out: Section[] = [];
  let cur: Section | null = null;
  // "Done: … · Outputs: … · How: …" on one line: a new line per label
  const labels = Object.keys(KINDS).map((k) => k.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")).join("|");
  const split = text.replace(/\r\n?/g, "\n").replace(new RegExp(`\\s+·\\s+(?=(?:\\*\\*)?(?:${labels})(?:\\*\\*)?\\s*:)`, "gi"), "\n");
  for (const line of split.split("\n")) {
    const m = line.match(LABEL);
    const h = !m ? line.match(HEADING) : null;
    const name = (m?.[1] ?? h?.[1] ?? "").trim();
    const kind = KINDS[name.toLowerCase().replace(/\s+/g, " ")];
    if (kind && !BULLET.test(line)) {
      cur = { kind, label: name, body: m ? m[2] : "" };
      out.push(cur);
    } else if (cur) {
      cur.body += "\n" + line;
    } else {
      cur = { kind: "other", label: "", body: line };
      out.push(cur);
    }
  }
  return out
    .map((s) => ({ ...s, body: listify(s.body.trim()) }))
    .filter((s) => s.body);
}

/** "a · b · c" on one line (the older format) becomes a list. */
function listify(body: string) {
  if (body.includes("\n") || !body.includes(" · ")) return body;
  return body.split(" · ").filter(Boolean).map((p) => `- ${p}`).join("\n");
}

/** "Word — meaning" / "Word: meaning" lines (or " · " separated) from a Terms section. */
function parseTerms(body: string): Entry[] {
  return body
    .split("\n")
    .map((l) => l.replace(BULLET, "$3").replace(/\*\*|__|`/g, "").trim())
    .filter(Boolean)
    .map((l) => l.match(/^(.{1,40}?)\s*(?:—|–|-|:)\s+(.+)$/))
    .filter((m): m is RegExpMatchArray => !!m)
    .map((m) => ({ term: m[1].trim(), meaning: m[2].trim() }));
}

const NOTHING = /^(nothing|none|n\/a|-|—)\.?(\s*[—–-].*)?$/i;

/** One line to stand for a whole recap (e.g. a finished agent's card). */
export function recapLine(text: string) {
  const s = parseSummary(text);
  const pick = s.find((x) => x.kind === "plain") ?? s.find((x) => x.kind === "done") ?? s[0];
  return (pick?.body ?? "").split("\n").map((l) => l.replace(BULLET, "$3").replace(/\*\*|__|`/g, "").trim()).find(Boolean) ?? "";
}

export function SummaryText({ text, className = "", compact = false }: { text: string; className?: string; compact?: boolean }) {
  const sections = parseSummary(text);
  const terms = sections.filter((s) => s.kind === "terms").flatMap((s) => parseTerms(s.body));
  // All text is rendered right here, in reading order, with a fresh glossary: each technical word gets its
  // explanation where it first appears (the helpers below are plain functions, not components, so a re-render
  // can't find the glossary half used).
  const gloss = makeGlossary(terms);
  return compact ? compactView(sections, gloss, className) : fullView(sections, gloss, terms, className);
}

function body(text: string, gloss: Glossed, dense = false) {
  return <div className={`${dense ? "space-y-1.5" : "space-y-2.5"} min-w-0 leading-relaxed`}>{renderBlocks(parseBlocks(text), gloss, dense)}</div>;
}

/* ───────────────────────── Agent recap (cards in windows and the timeline) ───────────────────────── */

function compactView(sections: Section[], gloss: Glossed, className: string) {
  return (
    <div className={`space-y-2.5 ${className}`}>
      {sections.map((s, i) =>
        s.kind === "plain" ? (
          <div key={i} className="text-fg">
            {body(s.body, gloss, true)}
          </div>
        ) : (
          <div key={i} className="grid grid-cols-[64px_1fr] gap-x-3">
            <div className="pt-[3px] text-[12px] font-medium text-fg-3">{LOOK[s.kind].short || s.label}</div>
            <div className={s.kind === "how" ? "text-fg-2" : "text-fg"}>
              {s.kind === "next" && NOTHING.test(s.body) ? <span className="text-fg-2">Nothing</span> : body(s.body, gloss, true)}
            </div>
          </div>
        ),
      )}
    </div>
  );
}

/* ───────────────────────── Run recap (the completed screen) ───────────────────────── */

function fullView(sections: Section[], gloss: Glossed, terms: Entry[], className: string) {
  const has = (k: Kind) => sections.some((s) => s.kind === k);
  // pairs sit side by side on wide screens; a section without its partner takes the full width
  const wide = (k: Kind) =>
    k === "outputs" ? !has("next") : k === "next" ? !has("outputs") : k === "agents" ? !has("terms") : k === "terms" ? !has("agents") : true;
  const order: Kind[] = ["plain", "other", "done", "outputs", "next", "agents", "terms", "how"];
  const sorted = [...sections].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind));
  return (
    <div className={`grid gap-3 md:grid-cols-2 ${className}`}>
      {sorted.map((s, i) => {
        if (s.kind === "plain")
          return (
            <div key={i} className="rounded-[20px] px-4 py-3.5 md:col-span-2" style={{ background: "color-mix(in oklab, var(--accent) 9%, transparent)", boxShadow: "inset 0 0 0 0.5px color-mix(in oklab, var(--accent) 28%, transparent)" }}>
              <div className="mb-1.5 flex items-center gap-1.5 text-[12px] font-semibold text-accent">
                <Lightbulb size={13} strokeWidth={2.4} /> In plain words
              </div>
              <div className="text-[15.5px] leading-[1.55] text-fg">{body(s.body, gloss)}</div>
            </div>
          );
        if (s.kind === "other" && !s.label)
          return (
            <div key={i} className="px-1 text-fg md:col-span-2">
              {body(s.body, gloss)}
            </div>
          );
        const look = LOOK[s.kind];
        return (
          <Tile key={i} icon={look.icon} color={look.color} title={look.title || s.label} wide={wide(s.kind)} muted={s.kind === "how"}>
            {s.kind === "outputs"
              ? outputs(s.body, gloss)
              : s.kind === "terms"
                ? termList(terms, s.body, gloss)
                : s.kind === "next" && NOTHING.test(s.body)
                  ? (
                    <span className="flex items-center gap-2 text-fg-2">
                      <CircleCheck size={15} className="text-green" /> Nothing. You&apos;re all set.
                    </span>
                  )
                  : body(s.body, gloss)}
          </Tile>
        );
      })}
    </div>
  );
}

function Tile({ icon: Icon, color, title, wide, muted, children }: { icon: typeof Info; color: string; title: string; wide: boolean; muted?: boolean; children: ReactNode }) {
  return (
    <section className={`min-w-0 rounded-[20px] bg-fill/45 px-4 py-3.5 ${wide ? "md:col-span-2" : ""} ${muted ? "text-[13px] text-fg-2" : "text-fg"}`}>
      <h4 className="mb-2 flex items-center gap-2 text-[12.5px] font-semibold text-fg-2">
        <span className="grid h-[22px] w-[22px] place-items-center rounded-[7px]" style={{ color, background: `color-mix(in oklab, ${color} 15%, transparent)` }}>
          <Icon size={13} strokeWidth={2.3} />
        </span>
        {title}
      </h4>
      {children}
    </section>
  );
}

/** Outputs: a link that stands alone on its line becomes a row you can click; everything else reads as usual. */
function outputs(text: string, gloss: Glossed) {
  const lines = text.split("\n").filter((l) => l.trim());
  const rows = lines.map((l) => {
    const t = l.replace(BULLET, "$3").trim();
    const url = t.match(/https?:\/\/[^\s<>()"'`]*[^\s<>()"'`.,;:!?]/)?.[0];
    const md = t.match(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/);
    const href = md?.[2] ?? url;
    if (!href) return { text: t };
    const label = t.replace(md?.[0] ?? href, md?.[1] ?? "").replace(/\s*[:—–-]\s*$/, "").replace(/^\s*[:—–-]\s*/, "").replace(/\*\*|__/g, "").trim();
    return { text: t, href, label };
  });
  if (!rows.some((r) => r.href)) return body(text, gloss);
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) =>
        r.href ? (
          <a key={i} href={r.href} target="_blank" rel="noreferrer noopener" className="group flex items-center gap-3 rounded-[14px] bg-[var(--glass-tint-strong)] px-3 py-2 ring-1 ring-sep transition-colors hover:ring-accent/40">
            <span className="min-w-0 flex-1">
              {r.label && <span className="block truncate text-[13.5px] font-medium text-fg">{r.label}</span>}
              <span className={`block truncate ${r.label ? "text-[12px] text-fg-2" : "text-[13.5px] font-medium text-accent"}`}>{prettyUrl(r.href)}</span>
            </span>
            <ExternalLink size={14} className="shrink-0 text-fg-3 transition-colors group-hover:text-accent" />
          </a>
        ) : (
          <div key={i} className="flex gap-2.5 px-1 leading-relaxed">
            <span className="mt-[0.62em] h-[5px] w-[5px] shrink-0 rounded-full bg-fg-3" />
            <span className="min-w-0 flex-1 break-words">{inline(r.text, gloss, `o${i}`)}</span>
          </div>
        ),
      )}
    </div>
  );
}

function termList(terms: Entry[], fallback: string, gloss: Glossed) {
  if (!terms.length) return body(fallback, gloss);
  return (
    <dl className="space-y-2">
      {terms.map((t) => (
        <div key={t.term}>
          <dt className="text-[13.5px] font-semibold text-fg">{t.term}</dt>
          <dd className="text-[13.5px] leading-snug text-fg-2">{t.meaning}</dd>
        </div>
      ))}
    </dl>
  );
}
