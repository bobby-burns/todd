"use client";

import { useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Check, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import { api, type Account } from "@/lib/api";
import { fadeUp, softSpring, spring, stagger } from "@/lib/motion";
import { AccountCard, Favicon, SignInQueue, StatusChip, useAccounts } from "@/components/accounts";
import { LiveBrowser } from "@/components/LiveBrowser";
import { PageHeader, ProgressRing, Segmented } from "@/components/ui";

export default function AccountsPage() {
  const { data, err, reload } = useAccounts(8000);
  const [tab, setTab] = useState<"mine" | "catalog">("mine");
  const [q, setQ] = useState("");
  const [queue, setQueue] = useState<Account[] | null>(null);
  const [checking, setChecking] = useState(false);

  const accounts = data?.accounts ?? [];
  const mine = accounts.filter((a) => a.selected);
  const missing = mine.filter((a) => a.status !== "signed_in" && !a.via);
  const shown = useMemo(() => {
    const base = tab === "mine" ? mine : accounts;
    const needle = q.trim().toLowerCase();
    return needle ? base.filter((a) => (a.name + a.category + a.domains.join(" ")).toLowerCase().includes(needle)) : base;
  }, [tab, mine, accounts, q]);
  const groups = useMemo(() => {
    const g: Record<string, Account[]> = {};
    for (const a of shown) (g[a.category] ??= []).push(a);
    return g;
  }, [shown]);

  async function toggle(a: Account) {
    const ids = new Set(mine.map((m) => m.id));
    ids.has(a.id) ? ids.delete(a.id) : ids.add(a.id);
    await api("/accounts-selection", { method: "PUT", json: { ids: [...ids] } });
    reload();
  }

  async function checkAll() {
    setChecking(true);
    for (const a of mine.filter((x) => !x.via && x.status === "unknown")) {
      await api(`/accounts/${a.id}/verify`, { method: "POST" }).catch(() => {});
    }
    setChecking(false);
    reload();
  }

  const signedIn = mine.filter((a) => a.status === "signed_in").length;
  const online = data?.browser_online;

  return (
    <div className="mx-auto flex max-w-[1560px] flex-col gap-6 px-4 md:px-8 xl:flex-row">
      <div className="min-w-0 flex-1">
        <PageHeader
          title="Accounts"
          subtitle="Sign in once in the agents' browser. Agents reuse these sessions, and the planner checks them before a run starts."
          actions={
            <span
              className="inline-flex h-7 items-center gap-1.5 rounded-full px-3 text-[12px] font-semibold"
              style={{
                color: online ? "var(--green)" : "var(--red)",
                background: `color-mix(in oklab, ${online ? "var(--green)" : "var(--red)"} 14%, transparent)`,
              }}
            >
              <span className="h-1.5 w-1.5 rounded-full bg-current" /> Browser {online ? "online" : "offline"}
            </span>
          }
        />

        {mine.length > 0 && (
          <motion.div initial="hidden" animate="show" variants={fadeUp} className="glass mb-5 flex flex-wrap items-center gap-4 rounded-[24px] p-4">
            <ProgressRing value={mine.length ? signedIn / mine.length : 0} size={44} stroke={4.5} />
            <div className="min-w-0 flex-1">
              <div className="text-[15px] font-semibold tracking-[-0.01em] tabular">
                {signedIn} of {mine.length} signed in
              </div>
              <div className="text-[12.5px] text-fg-2">{missing.length ? `${missing.length} still need a sign-in` : "Agents won't get stuck on a login screen."}</div>
            </div>
            <button className="btn btn-glass" disabled={checking || !mine.length} onClick={checkAll}>
              <RefreshCw size={13} className={checking ? "animate-spin" : ""} /> Check unknown
            </button>
            <button className="btn btn-accent" disabled={!missing.length} onClick={() => setQueue(missing)}>
              Sign in to {missing.length} missing
            </button>
          </motion.div>
        )}

        <div className="flex flex-wrap items-center gap-2.5">
          <Segmented
            id="acct-tab"
            value={tab}
            onChange={setTab}
            options={[
              { value: "mine", label: `My accounts · ${mine.length}` },
              { value: "catalog", label: `All services · ${accounts.length}` },
            ]}
          />
          <label className="relative min-w-44 flex-1 md:max-w-xs">
            <Search size={14} className="absolute top-1/2 left-3.5 -translate-y-1/2 text-fg-3" />
            <input className="field h-9 rounded-full pl-9" placeholder="Search services" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
        </div>
        {err && <p className="mt-3 text-[13px] text-red">{err}</p>}

        <AnimatePresence>
          {queue && (
            <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={softSpring} className="overflow-hidden">
              <div className="pt-5">
                <SignInQueue
                  accounts={queue}
                  onChange={reload}
                  onDone={() => {
                    setQueue(null);
                    reload();
                  }}
                />
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {tab === "mine" && mine.length === 0 && data && (
          <div className="glass mt-6 rounded-[24px] p-6 text-[13.5px] text-fg-2">
            You haven&apos;t picked any accounts yet. Open <b className="text-fg">All services</b> and pick the ones your agents should use, or run Setup.
          </div>
        )}

        <div className="mt-7 space-y-8 pb-10">
          {Object.entries(groups).map(([cat, list]) => (
            <section key={cat}>
              <h2 className="eyebrow mb-3 px-1">{cat}</h2>
              {tab === "mine" ? (
                <motion.div initial="hidden" animate="show" variants={stagger(0.03)} className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(260px,1fr))]">
                  {list.map((a) => (
                    <motion.div key={a.id} variants={fadeUp} className="group relative">
                      <AccountCard a={a} onChange={reload} onSignIn={(x) => setQueue([x])} />
                      {a.custom && (
                        <button
                          className="absolute top-3 right-3 text-fg-3 opacity-0 transition-opacity group-hover:opacity-100 hover:text-red"
                          title="Remove custom site"
                          onClick={async () => {
                            await api(`/accounts-custom/${a.id}`, { method: "DELETE" });
                            reload();
                          }}
                        >
                          <Trash2 size={13} />
                        </button>
                      )}
                    </motion.div>
                  ))}
                </motion.div>
              ) : (
                <div className="grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(240px,1fr))]">
                  {list.map((a) => (
                    <button
                      key={a.id}
                      onClick={() => toggle(a)}
                      className={`glass flex items-center gap-3 rounded-[18px] px-3 py-2.5 text-left transition-transform active:scale-[0.98] ${
                        a.selected ? "shadow-[var(--glass-shadow),0_0_0_2px_var(--accent)]" : ""
                      }`}
                    >
                      <Favicon a={a} size={30} />
                      <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium">{a.name}</span>
                      {a.status === "signed_in" && <StatusChip a={a} />}
                      <span
                        className={`grid h-[22px] w-[22px] shrink-0 place-items-center rounded-full transition-colors ${a.selected ? "bg-accent text-white" : "ring-[1.5px] ring-fg-3 ring-inset"}`}
                      >
                        <AnimatePresence>
                          {a.selected && (
                            <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} exit={{ scale: 0 }} transition={spring}>
                              <Check size={13} strokeWidth={3.2} />
                            </motion.span>
                          )}
                        </AnimatePresence>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </section>
          ))}
          <AddCustom onAdded={reload} />
        </div>
      </div>

      <aside className="pt-2 xl:sticky xl:top-3 xl:h-fit xl:w-[560px] xl:pt-12">
        <LiveBrowser
          defaultControl
          footer="This is the agents' real browser. Anything you sign in to here, agents can use, so only add accounts you want them to act on. Agents must ask before posting or sending anything."
        />
      </aside>
    </div>
  );
}

function AddCustom({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [login, setLogin] = useState("");
  const [check, setCheck] = useState("");
  const [err, setErr] = useState<string | null>(null);
  return (
    <div>
      <AnimatePresence mode="wait" initial={false}>
        {!open ? (
          <motion.button key="btn" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="btn btn-glass" onClick={() => setOpen(true)}>
            <Plus size={14} /> Add a site that isn&apos;t listed
          </motion.button>
        ) : (
          <motion.div key="form" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={softSpring} className="glass rounded-[24px] p-5">
            <div className="title-3 mb-4">Add a custom site</div>
            <div className="grid gap-3 md:grid-cols-3">
              <input className="field" placeholder="Name (e.g. Kalshi)" value={name} onChange={(e) => setName(e.target.value)} />
              <input className="field" placeholder="Login URL (https://…)" value={login} onChange={(e) => setLogin(e.target.value)} />
              <input className="field" placeholder="Page that needs login (optional)" value={check} onChange={(e) => setCheck(e.target.value)} />
            </div>
            <div className="mt-4 flex gap-2">
              <button
                className="btn btn-accent"
                disabled={!name || !login.startsWith("http")}
                onClick={async () => {
                  try {
                    await api("/accounts-custom", { method: "POST", json: { name, login, check: check || null } });
                    setOpen(false);
                    setName("");
                    setLogin("");
                    setCheck("");
                    onAdded();
                  } catch (e: any) {
                    setErr(e.message);
                  }
                }}
              >
                Add site
              </button>
              <button className="btn btn-plain" onClick={() => setOpen(false)}>
                Cancel
              </button>
            </div>
            {err && <p className="mt-2 text-[12px] text-red">{err}</p>}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
