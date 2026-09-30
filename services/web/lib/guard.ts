/* Who may use the dashboard's API route (app/api/[...path]/route.ts). The route adds the API token, so whoever reaches
   it can act as you (approve spending, change settings); only your own dashboard may:
   * Host must be this machine (or a name in TODD_ALLOWED_HOSTS): stops DNS rebinding (a website whose name suddenly
     points at 127.0.0.1) and Todd's own containers calling http://web:3000.
   * Browsers mark every request with where it came from: anything not from this dashboard's own pages is refused, so
     another site (or another app on localhost) can't make your browser call Todd, not even a "simple" GET or POST. */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const EXTRA_HOSTS = (process.env.TODD_ALLOWED_HOSTS || "")
  .split(",")
  .map((h) => h.trim().toLowerCase())
  .filter(Boolean);

function hostnameOf(host: string) {
  try {
    return new URL(`http://${host}`).hostname.toLowerCase();
  } catch {
    return "";
  }
}

export function refusal(method: string, headers: Headers): string | null {
  const host = (headers.get("host") || "").toLowerCase();
  const name = hostnameOf(host);
  if (!name || !(LOCAL_HOSTS.has(name) || EXTRA_HOSTS.includes(name) || EXTRA_HOSTS.includes(host)))
    return `Todd only answers on localhost (this request was for "${host}"). To use another name, add it to TODD_ALLOWED_HOSTS.`;
  const site = headers.get("sec-fetch-site");
  if (site && site !== "same-origin" && site !== "none") return "Refused: this request came from another site.";
  if (method !== "GET" && method !== "HEAD") {
    const origin = headers.get("origin");
    if (origin !== null) {
      let o = "";
      try {
        o = new URL(origin).host.toLowerCase();
      } catch {}
      if (o !== host) return "Refused: this request came from another site.";
    }
  }
  return null;
}
