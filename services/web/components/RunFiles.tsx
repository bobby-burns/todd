"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "motion/react";
import {
  ArrowLeft,
  ChevronRight,
  Clock,
  Code2,
  Download,
  Eye,
  EyeOff,
  FileCode,
  FileImage,
  FileLock,
  FileText,
  Folder,
  FolderOpen,
  KeyRound,
  Lock,
  RefreshCw,
} from "lucide-react";
import { agentColor, api, type AgentInfo } from "@/lib/api";
import { softSpring } from "@/lib/motion";
import { ActivityIndicator, AgentAvatar } from "./ui";
import { Markdown } from "./Markdown";

/* The run's workspace, read-only: every file its agents made or changed (their shared folder in the sandbox), and
   the vault keys this run saved. Updates live while the run works. */

type Entry = { path: string; dir: boolean; size?: number; mtime?: number | null; skipped?: boolean; link?: boolean };
type Tree = { exists: boolean; entries: Entry[]; truncated: boolean };
type FileView = {
  path: string;
  kind: "text" | "image" | "binary" | "hidden";
  size?: number;
  mtime?: number;
  content?: string;
  truncated?: boolean;
  env?: boolean;
  mime?: string;
  data?: string;
  note?: string;
};
type Key = { name: string; masked: string; updated_at: string; agent: string | null };
type Node = Entry & { name: string; children: Node[] };

const CODE = /\.(tsx?|jsx?|mjs|cjs|json|py|rb|go|rs|java|kt|swift|c|h|cpp|cs|php|sh|ya?ml|toml|css|scss|html|sql|graphql|xml|gradle|plist|lock)$/i;
const IMAGE = /\.(png|jpe?g|gif|webp|avif|ico|bmp|svg)$/i;
const KEYISH = /(\.(pem|p8|p12|pfx|key|keystore|jks|mobileprovision)$|(^|\/)id_(rsa|ed25519|ecdsa|dsa)$|(^|\/)\.env)/i;
const MARKDOWN = /\.(md|mdx|markdown)$/i;

function build(entries: Entry[]): Node {
  const root: Node = { path: "", name: "", dir: true, children: [] };
  const byPath = new Map<string, Node>([["", root]]);
  const parentOf = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
  for (const e of entries) {
    const node: Node = { ...e, name: e.path.split("/").pop() ?? e.path, children: [] };
    byPath.set(e.path, node);
    (byPath.get(parentOf(e.path)) ?? root).children.push(node);
  }
  const sort = (n: Node) => {
    n.children.sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name) : a.dir ? -1 : 1));
    n.children.forEach(sort);
  };
  sort(root);
  return root;
}

function size(n?: number) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function ago(mtime?: number | null) {
  if (!mtime) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - mtime));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(mtime * 1000).toLocaleDateString();
}

function FileIcon({ path, size: px = 14, className = "" }: { path: string; size?: number; className?: string }) {
  const Icon = KEYISH.test(path) ? FileLock : IMAGE.test(path) ? FileImage : CODE.test(path) ? FileCode : FileText;
  return <Icon size={px} className={`shrink-0 ${className}`} />;
}

const ancestors = (p: string) => p.split("/").slice(0, -1).map((_, i, a) => a.slice(0, i + 1).join("/"));

export function RunFiles({ runId, active, agents }: { runId: string; active: boolean; agents: AgentInfo[] }) {
  const [tree, setTree] = useState<Tree | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [file, setFile] = useState<FileView | null>(null);
  const [fileErr, setFileErr] = useState<string | null>(null);
  const [loadingFile, setLoadingFile] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [source, setSource] = useState(false);
  const [keys, setKeys] = useState<Key[]>([]);
  const [busy, setBusy] = useState(false);
  const first = useRef(true);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const t = await api<Tree>(`/runs/${runId}/files`);
      setTree(t);
      setErr(null);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
    api<Key[]>(`/secrets?run_id=${runId}`).then(setKeys).catch(() => {});
  }, [runId]);

  useEffect(() => {
    load();
    if (!active) return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load, active]);

  const root = useMemo(() => build(tree?.entries ?? []), [tree]);
  const files = useMemo(() => (tree?.entries ?? []).filter((e) => !e.dir), [tree]);
  const recent = useMemo(() => [...files].sort((a, b) => (b.mtime ?? 0) - (a.mtime ?? 0)).slice(0, 5), [files]);
  const selEntry = files.find((f) => f.path === sel);

  // First load: open the top folders and, on wide screens, the file changed last.
  useEffect(() => {
    if (!tree?.exists || !first.current) return;
    first.current = false;
    const next = new Set<string>();
    const dirs = root.children.filter((c) => c.dir && !c.skipped);
    if (dirs.length <= 2) dirs.forEach((d) => next.add(d.path));
    const wide = typeof window !== "undefined" && window.matchMedia("(min-width: 768px)").matches;
    const pick = files.find((f) => /(^|\/)README\.md$/i.test(f.path) && f.path.split("/").length <= 2) ?? recent[0];
    if (wide && pick) {
      ancestors(pick.path).forEach((a) => next.add(a));
      setSel(pick.path);
    }
    setOpen(next);
  }, [tree, root, files, recent]);

  const loadFile = useCallback(
    async (path: string, rev: boolean) => {
      setLoadingFile(true);
      setFileErr(null);
      try {
        setFile(await api<FileView>(`/runs/${runId}/files/view?path=${encodeURIComponent(path)}${rev ? "&reveal=true" : ""}`));
      } catch (e: any) {
        setFile(null);
        setFileErr(e.message);
      } finally {
        setLoadingFile(false);
      }
    },
    [runId],
  );

  useEffect(() => {
    if (sel) loadFile(sel, reveal);
  }, [sel, reveal, loadFile]);

  // The open file changed while the agents work: show the new version.
  const seen = useRef<number | null | undefined>(undefined);
  useEffect(() => {
    if (!sel || !selEntry) return;
    if (seen.current !== undefined && selEntry.mtime !== seen.current) loadFile(sel, reveal);
    seen.current = selEntry.mtime;
  }, [selEntry?.mtime]); // eslint-disable-line react-hooks/exhaustive-deps

  function choose(path: string) {
    seen.current = undefined;
    setReveal(false);
    setSource(false);
    setSel(path);
    setOpen((o) => new Set([...o, ...ancestors(path)]));
  }
  const toggle = (p: string) => setOpen((o) => (o.has(p) ? new Set([...o].filter((x) => x !== p)) : new Set([...o, p])));

  const agentName = (id: string | null) => agents.find((a) => a.id === id)?.name ?? (id === "planner" ? "Planner" : id ?? "");

  return (
    <div className="glass overflow-hidden rounded-[28px]">
      <div className="grid md:h-[calc(100dvh-150px)] md:min-h-[520px] md:grid-cols-[300px_1fr]">
        {/* Sidebar: recent files, the tree, this run's keys */}
        <aside className={`scrollbar-thin min-h-0 overflow-y-auto border-sep md:border-r ${sel ? "hidden md:block" : ""}`}>
          <div className="flex items-center gap-2 px-4 pt-4 pb-2">
            <FolderOpen size={16} className="text-accent" />
            <span className="flex-1 text-[14px] font-semibold tracking-[-0.01em]">Files</span>
            {active && <span className="text-[11.5px] text-fg-3">live</span>}
            <button className="btn btn-plain btn-sm btn-icon w-7 text-fg-2" title="Refresh" onClick={load}>
              <RefreshCw size={13} className={busy ? "animate-spin" : ""} />
            </button>
          </div>
          <p className="px-4 pb-3 text-[12px] leading-snug text-fg-3">What this run's agents made, in their shared folder. Read-only.</p>

          {err ? (
            <p className="mx-3 rounded-[12px] bg-red/10 px-3 py-2 text-[12.5px] text-red">{err}</p>
          ) : !tree ? (
            <div className="grid place-items-center py-10 text-fg-3">
              <ActivityIndicator size={18} />
            </div>
          ) : !tree.exists || files.length === 0 ? (
            <p className="mx-3 rounded-[14px] bg-fill/60 px-3 py-3 text-[12.5px] leading-snug text-fg-2">
              Nothing here yet. Files show up as soon as an agent creates them.
            </p>
          ) : (
            <>
              {recent.length > 0 && (
                <div className="px-2 pb-2">
                  <div className="eyebrow px-2 pb-1 text-fg-3">Recently changed</div>
                  {recent.map((f) => (
                    <button
                      key={f.path}
                      onClick={() => choose(f.path)}
                      className={`flex w-full items-center gap-2 rounded-[10px] px-2 py-1.5 text-left transition-colors ${sel === f.path ? "bg-accent/12" : "hover:bg-fill"}`}
                    >
                      <FileIcon path={f.path} className="text-fg-3" />
                      <span className="min-w-0 flex-1 truncate text-[13px]">{f.path.split("/").pop()}</span>
                      <span className="shrink-0 text-[11px] text-fg-3 tabular">{ago(f.mtime)}</span>
                    </button>
                  ))}
                </div>
              )}
              <div className="mx-4 h-px bg-sep" />
              <div className="px-2 py-2">
                <div className="eyebrow px-2 pb-1 text-fg-3">All files</div>
                {root.children.map((n) => (
                  <TreeNode key={n.path} node={n} depth={0} open={open} sel={sel} onToggle={toggle} onPick={choose} />
                ))}
                {tree.truncated && <p className="px-2 pt-2 text-[11.5px] text-fg-3">Showing the first 5,000 entries.</p>}
              </div>
            </>
          )}

          {keys.length > 0 && (
            <div className="px-2 pt-1 pb-4">
              <div className="mx-2 mb-2 h-px bg-sep" />
              <div className="flex items-center gap-1.5 px-2 pb-1">
                <KeyRound size={12} className="text-pink" />
                <span className="eyebrow flex-1 text-fg-3">Keys this run saved</span>
                <Link href="/settings#vault" className="text-[11.5px] font-medium text-accent">
                  Vault
                </Link>
              </div>
              {keys.map((k) => (
                <div key={k.name} className="flex items-center gap-2 rounded-[10px] px-2 py-1.5" title={k.agent ? `Saved by ${agentName(k.agent)}` : undefined}>
                  <Lock size={12} className="shrink-0 text-fg-3" />
                  <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-medium">{k.name}</span>
                  {k.agent && <AgentAvatar name={agentName(k.agent)} color={agentColor(agents, k.agent)} planner={k.agent === "planner"} size={16} />}
                </div>
              ))}
              <p className="px-2 pt-1 text-[11.5px] leading-snug text-fg-3">Values stay in the vault. Agents use them without seeing them.</p>
            </div>
          )}
        </aside>

        {/* Viewer */}
        <section className={`flex min-h-[60vh] min-w-0 flex-col md:min-h-0 ${sel ? "" : "hidden md:flex"}`}>
          {!sel ? (
            <div className="grid flex-1 place-items-center p-8 text-center text-[13px] text-fg-3">Pick a file to see it here.</div>
          ) : (
            <>
              <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-sep px-4 py-3">
                <button className="btn btn-glass btn-sm btn-icon w-8 md:hidden" onClick={() => setSel(null)} title="Back to files">
                  <ArrowLeft size={14} />
                </button>
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center gap-1 text-[12.5px] text-fg-2">
                    {sel.split("/").map((part, i, all) => (
                      <span key={i} className={`flex min-w-0 items-center gap-1 ${i === all.length - 1 ? "font-semibold text-fg" : "shrink-0"}`}>
                        {i > 0 && <ChevronRight size={12} className="shrink-0 text-fg-3" />}
                        <span className="truncate">{part}</span>
                      </span>
                    ))}
                  </div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[11.5px] text-fg-3 tabular">
                    {file?.size != null && <span>{size(file.size)}</span>}
                    {file?.mtime && (
                      <span className="flex items-center gap-1">
                        · <Clock size={11} /> changed {ago(file.mtime)}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  {file?.kind === "text" && MARKDOWN.test(sel) && (
                    <button className="btn btn-glass btn-sm" onClick={() => setSource(!source)} title={source ? "Show it formatted" : "Show the raw text"}>
                      {source ? <Eye size={12} /> : <Code2 size={12} />} {source ? "Preview" : "Source"}
                    </button>
                  )}
                  {file?.env && (
                    <button className="btn btn-glass btn-sm" onClick={() => setReveal(!reveal)} title="Values that are in the vault stay hidden either way">
                      {reveal ? <EyeOff size={12} /> : <Eye size={12} />} {reveal ? "Hide values" : "Show values"}
                    </button>
                  )}
                  {file && file.kind !== "hidden" && (
                    <a className="btn btn-glass btn-sm btn-icon w-8" href={`/api/runs/${runId}/files/download?path=${encodeURIComponent(sel)}`} title="Download" download>
                      <Download size={13} />
                    </a>
                  )}
                </div>
              </header>
              <div className="relative min-h-0 flex-1">
                <AnimatePresence>
                  {loadingFile && !file && (
                    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="absolute inset-0 grid place-items-center text-fg-3">
                      <ActivityIndicator size={18} />
                    </motion.div>
                  )}
                </AnimatePresence>
                {fileErr && <p className="m-4 rounded-[12px] bg-red/10 px-3 py-2 text-[12.5px] text-red">{fileErr}</p>}
                {file && <Viewer file={file} source={source} />}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function TreeNode({ node, depth, open, sel, onToggle, onPick }: { node: Node; depth: number; open: Set<string>; sel: string | null; onToggle: (p: string) => void; onPick: (p: string) => void }) {
  const pad = { paddingLeft: 8 + depth * 14 };
  if (node.dir) {
    const isOpen = open.has(node.path) && !node.skipped;
    return (
      <div>
        <button
          onClick={() => !node.skipped && onToggle(node.path)}
          className={`flex w-full items-center gap-1.5 rounded-[9px] py-1 pr-2 text-left text-[13px] ${node.skipped ? "cursor-default text-fg-3" : "hover:bg-fill"}`}
          style={pad}
          title={node.skipped ? "Installed packages or build output: not shown" : undefined}
        >
          <ChevronRight size={12} className={`shrink-0 text-fg-3 transition-transform duration-200 ${isOpen ? "rotate-90" : ""} ${node.skipped ? "opacity-0" : ""}`} />
          {isOpen ? <FolderOpen size={14} className="shrink-0 text-accent" /> : <Folder size={14} className={`shrink-0 ${node.skipped ? "text-fg-3" : "text-accent"}`} />}
          <span className="min-w-0 flex-1 truncate">{node.name}</span>
          {node.skipped && <span className="shrink-0 text-[10.5px] text-fg-3">hidden</span>}
        </button>
        <AnimatePresence initial={false}>
          {isOpen && (
            <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={softSpring} className="overflow-hidden">
              {node.children.map((c) => (
                <TreeNode key={c.path} node={c} depth={depth + 1} open={open} sel={sel} onToggle={onToggle} onPick={onPick} />
              ))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  }
  const fresh = node.mtime != null && Date.now() / 1000 - node.mtime < 120;
  return (
    <button
      onClick={() => onPick(node.path)}
      className={`flex w-full items-center gap-1.5 rounded-[9px] py-1 pr-2 text-left text-[13px] transition-colors ${sel === node.path ? "bg-accent/12 text-fg" : "hover:bg-fill"}`}
      style={{ paddingLeft: 8 + depth * 14 + 18 }}
    >
      <FileIcon path={node.path} className="text-fg-3" />
      <span className="min-w-0 flex-1 truncate">{node.name}</span>
      {fresh && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent" title="Changed in the last 2 minutes" />}
    </button>
  );
}

function Viewer({ file, source }: { file: FileView; source: boolean }) {
  if (file.kind === "hidden") {
    return (
      <div className="grid h-full min-h-[40vh] place-items-center p-8 text-center">
        <div>
          <span className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-full bg-fill text-fg-2">
            <Lock size={18} />
          </span>
          <p className="text-[13.5px] text-fg-2">{file.note}</p>
        </div>
      </div>
    );
  }
  if (file.kind === "image" && file.data) {
    return (
      <div className="grid h-full min-h-[40vh] place-items-center overflow-auto p-6" style={{ backgroundImage: "repeating-conic-gradient(var(--fill) 0 25%, transparent 0 50%)", backgroundSize: "18px 18px" }}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={`data:${file.mime};base64,${file.data}`} alt={file.path} className="max-h-full max-w-full rounded-[8px] shadow-md" />
      </div>
    );
  }
  if (file.kind === "binary") {
    return (
      <div className="grid h-full min-h-[40vh] place-items-center p-8 text-center text-[13.5px] text-fg-2">
        This file isn&apos;t text ({size(file.size)}). Download it to open it.
      </div>
    );
  }
  const text = file.content ?? "";
  return (
    <div className="scrollbar-thin h-full overflow-auto">
      {file.env && (
        <p className="mx-4 mt-3 rounded-[12px] bg-orange/10 px-3 py-2 text-[12px] leading-snug text-fg-2">
          Settings file: values are hidden until you show them. Anything that&apos;s also in the vault stays hidden.
        </p>
      )}
      {MARKDOWN.test(file.path) && !source ? (
        <div className="mx-auto max-w-[760px] px-6 py-5 text-[14.5px]">
          <Markdown text={text} />
        </div>
      ) : (
        <Code text={text} />
      )}
      {file.truncated && <p className="px-4 pb-4 text-[12px] text-fg-3">The file is longer than this. Download it to see all of it.</p>}
    </div>
  );
}

function Code({ text }: { text: string }) {
  const lines = text.endsWith("\n") ? text.slice(0, -1).split("\n") : text.split("\n");
  const nums = useMemo(() => lines.map((_, i) => i + 1).join("\n"), [lines.length]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="flex min-w-max py-3 font-mono text-[12.5px] leading-[1.65]">
      <pre className="sticky left-0 z-10 bg-[var(--glass-tint-strong)] pr-4 pl-4 text-right text-fg-3 backdrop-blur-md select-none">{nums}</pre>
      <pre className="pr-6 text-fg">{lines.join("\n")}</pre>
    </div>
  );
}
