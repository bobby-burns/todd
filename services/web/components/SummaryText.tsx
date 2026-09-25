/** Renders a "Done: … / Outputs: … / How: … / Left / needs you: …" recap as a label/value list. */
const LABELS: Record<string, string> = { "Left / needs you": "Next", "Left": "Next" };

export function SummaryText({ text, className = "", compact = false }: { text: string; className?: string; compact?: boolean }) {
  const lines = text.split("\n").filter((l) => l.trim());
  return (
    <dl className={`grid gap-x-4 gap-y-2.5 ${compact ? "grid-cols-[64px_1fr]" : "grid-cols-[76px_1fr]"} ${className}`}>
      {lines.map((l, i) => {
        const m = l.match(/^\s*([A-Z][A-Za-z /]{1,24}):\s*(.*)$/);
        if (!m) {
          return (
            <dd key={i} className="col-span-2 leading-relaxed text-fg">
              {l}
            </dd>
          );
        }
        const label = LABELS[m[1]] ?? m[1];
        const parts = m[2].split(" · ").filter(Boolean);
        return (
          <div key={i} className="contents">
            <dt className="pt-px text-[12px] font-medium text-fg-3">{label}</dt>
            <dd className="min-w-0 leading-relaxed text-fg">
              {parts.length > 1 ? (
                <ul className="space-y-1">
                  {parts.map((p, j) => (
                    <li key={j} className="flex gap-2">
                      <span className="mt-[0.6em] h-1 w-1 shrink-0 rounded-full bg-fg-3" />
                      <span className="min-w-0 break-words">{p}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <span className="break-words">{m[2]}</span>
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
