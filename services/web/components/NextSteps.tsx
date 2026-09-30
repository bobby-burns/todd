"use client";

import { motion } from "motion/react";
import { ArrowUpRight, ShieldCheck, Sparkles } from "lucide-react";
import { softSpring } from "@/lib/motion";
import { BULLET } from "./Markdown";
import { parseSummary } from "./SummaryText";

/* Prompts to send next, under a finished run's recap: the planner's own "Try next" first, then production-readiness
   steps that fit what was built (security, access rules, payments, monitoring…). Tapping one puts it in the
   Continue box to send or edit. */

type Step = { text: string; ready?: boolean };

const READY: { when: RegExp; text: string }[] = [
  { when: /iphone|ios|testflight|app store|android|google play|expo/i, text: "Get the app ready for store review: privacy policy, the App Privacy / Data safety answers, screenshots, and a crash reporter." },
  { when: /firebase|firestore/i, text: "Lock down the Firestore and Storage security rules so people can only read and write their own data, and add tests for the rules." },
  { when: /supabase/i, text: "Turn on row-level security for every Supabase table and add policies so people only see their own rows; make sure the service key is never in client code." },
  { when: /stripe|payment|checkout|subscription/i, text: "Get payments ready for real customers: verify Stripe webhook signatures, handle failed and refunded payments, then switch to live mode and do one small real purchase." },
  { when: /\b(web ?site|web ?app|landing page|vercel|netlify|deploy|next\.js|site)\b/i, text: "Do a security pass on the site: security headers (CSP, HSTS), rate limits on forms and APIs, no secrets in client code, and dependency updates." },
  { when: /\b(web ?site|web ?app|landing page|vercel|netlify|next\.js|site)\b/i, text: "Help people find and share it: submit the sitemap to Google Search Console, check the social preview on X and LinkedIn, and fix anything Lighthouse flags for SEO and accessibility." },
  { when: /\b(web ?site|web ?app|landing page|vercel|netlify|next\.js|site)\b/i, text: "Add privacy-friendly analytics, uptime monitoring and error alerts, so you know when people use it and when it breaks." },
  { when: /\b(backend|server|endpoints?|auth|sign[- ]?in|login)\b/i, text: "Do a security pass on the backend: validate every input, check that each endpoint enforces who can do what, and add rate limiting and logging for sign-ins." },
  { when: /github|repo/i, text: "Add CI on GitHub that runs the build and tests on every push, and turn on Dependabot security updates and branch protection for main." },
  { when: /domain|dns/i, text: "Set up email for the domain (MX, SPF, DKIM and DMARC) so mail from it doesn't land in spam, and make sure HTTPS is enforced." },
  { when: /\b(database|firestore|supabase|postgres|mongodb)\b/i, text: "Set up automatic backups for the data and check that a restore actually works." },
];
const GENERIC: Step[] = [
  { text: "Review what you built for security problems and fix anything serious.", ready: true },
  { text: "Write a short README that explains how to run, change and deploy this, in plain words." },
];
const READY_WORDS = /secur|rule|row[- ]level|webhook|backup|monitor|rate limit|production|live mode|privacy|seo|sitemap|robots|llms\.txt|lighthouse|analytics|header/i;

/** Production-readiness steps first (up to 3, so ideas can't crowd them out), then the planner's other ideas. */
export function nextSteps(summary: string, prompt = "", max = 5): Step[] {
  const own = parseSummary(summary)
    .filter((s) => s.kind === "suggest")
    .flatMap((s) => s.body.split("\n"))
    .map((l) => l.replace(BULLET, "$3").replace(/\*\*|__|`/g, "").trim())
    .filter((l) => l.length > 8)
    .map((text) => ({ text, ready: READY_WORDS.test(text) }));
  const hay = `${summary}\n${prompt}`;
  const built = READY.filter((r) => r.when.test(hay)).map((r) => ({ text: r.text, ready: true }));
  const unique = (list: Step[]) => list.filter((s, i) => list.findIndex((o) => o.text.toLowerCase() === s.text.toLowerCase()) === i);
  const ready = unique([...own.filter((s) => s.ready), ...built, ...GENERIC.filter((s) => s.ready)]).slice(0, 3);
  const ideas = unique([...own.filter((s) => !s.ready), ...GENERIC.filter((s) => !s.ready)]).slice(0, max - ready.length);
  return [...ready, ...ideas];
}

export function NextSteps({ summary, prompt, onPick }: { summary: string; prompt?: string; onPick: (text: string) => void }) {
  const steps = nextSteps(summary, prompt);
  if (!steps.length) return null;
  return (
    <section className="mt-5">
      <h4 className="mb-2 flex items-center gap-2 px-1 text-[12.5px] font-semibold text-fg-2">
        <Sparkles size={13} className="text-accent" /> Suggested next steps
        <span className="font-normal text-fg-3">· tap one to send it (you can edit it first)</span>
      </h4>
      <div className="grid gap-2 md:grid-cols-2">
        {steps.map((s, i) => (
          <motion.button
            key={s.text}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ ...softSpring, delay: 0.04 * i }}
            onClick={() => onPick(s.text)}
            className="group flex items-start gap-2.5 rounded-[16px] bg-[var(--glass-tint-strong)] px-3.5 py-2.5 text-left ring-1 ring-sep transition-colors hover:ring-accent/40"
          >
            {s.ready ? (
              <ShieldCheck size={15} className="mt-[3px] shrink-0 text-green" aria-label="Production readiness" />
            ) : (
              <Sparkles size={15} className="mt-[3px] shrink-0 text-accent" />
            )}
            <span className="min-w-0 flex-1 text-[13.5px] leading-snug text-fg">{s.text}</span>
            <ArrowUpRight size={14} className="mt-[3px] shrink-0 text-fg-3 transition-colors group-hover:text-accent" />
          </motion.button>
        ))}
      </div>
    </section>
  );
}
