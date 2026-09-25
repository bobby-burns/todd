// Same-origin proxy to the Todd API (so the browser never needs CORS or the API's address).
// Streams responses, which keeps server-sent events live.
import { readFileSync } from "node:fs";
import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const API_URL = process.env.API_URL || "http://localhost:8000";
let cachedToken: string | null = null;
function apiToken(): string {
  if (cachedToken !== null) return cachedToken;
  if (process.env.TODD_API_TOKEN) return (cachedToken = process.env.TODD_API_TOKEN);
  const file = process.env.TODD_API_TOKEN_FILE;
  if (file) {
    try {
      const t = readFileSync(file, "utf8").trim();
      if (t) return (cachedToken = t); // only cache once the api has generated it
    } catch {}
  }
  return "";
}
const HOP_BY_HOP = ["host", "connection", "content-length", "transfer-encoding", "keep-alive", "upgrade"];

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const url = `${API_URL}/api/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const headers = new Headers(req.headers);
  HOP_BY_HOP.forEach((h) => headers.delete(h));
  headers.delete("accept-encoding");
  headers.delete("x-todd-token");
  const token = apiToken();
  if (token) headers.set("x-todd-token", token);

  const init: RequestInit = { method: req.method, headers, redirect: "manual", signal: req.signal };
  if (req.method !== "GET" && req.method !== "HEAD") init.body = await req.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(url, init);
  } catch (e) {
    return Response.json({ detail: `Todd API unreachable at ${API_URL}: ${String(e)}` }, { status: 502 });
  }
  const out = new Headers(upstream.headers);
  ["content-encoding", "content-length", "transfer-encoding", "connection"].forEach((h) => out.delete(h));
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
