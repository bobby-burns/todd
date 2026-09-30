/* What an agent is doing, in plain words, from the tool it's running: "Building the site to make sure it works"
   (the plain part) and `npm run build` (the actual part, shown smaller). No jargon in `plain`: it's for anyone. */

export type Doing = { plain: string; detail?: string };

const short = (t: string, n = 70) => (t.length > n ? t.slice(0, n - 1) + "…" : t);

function host(u?: string) {
  try {
    return u ? new URL(u).host.replace(/^www\./, "") : "";
  } catch {
    return "";
  }
}

/** Well-known services by API host, so "api.vercel.com" reads as "Vercel". */
const SERVICES: [RegExp, string][] = [
  [/vercel/, "Vercel"], [/github/, "GitHub"], [/stripe/, "Stripe"], [/supabase/, "Supabase"], [/firebase|googleapis/, "Google"],
  [/netlify/, "Netlify"], [/cloudflare/, "Cloudflare"], [/openai/, "OpenAI"], [/anthropic/, "Anthropic"], [/resend/, "Resend"],
  [/sendgrid/, "SendGrid"], [/expo\.dev/, "Expo"], [/namecheap/, "Namecheap"], [/godaddy/, "GoDaddy"], [/twilio/, "Twilio"],
  [/x\.com|twitter/, "X"], [/linkedin/, "LinkedIn"], [/slack/, "Slack"], [/discord/, "Discord"], [/notion/, "Notion"],
];
function service(h: string) {
  return SERVICES.find(([re]) => re.test(h))?.[1] ?? h;
}

/** A page or piece of a site, from its file path: "app/quiz/page.tsx" → "the quiz page". */
function fileMeaning(path: string): string {
  const p = path.replace(/\\/g, "/").toLowerCase();
  const base = path.replace(/\\/g, "/").split("/").pop() ?? path; // original case, so "StreakBadge" reads "Streak Badge"
  const words = (s: string) =>
    s.replace(/\.[a-z0-9]+$/i, "").replace(/[-_[\]()]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").trim().toLowerCase();
  if (/(^|\/)(robots|sitemap|llms)[^/]*$/.test(p)) return "files that help search engines and AI assistants find the site";
  if (/opengraph-image|twitter-image|(^|\/)og[-.]/.test(p)) return "the preview image people see when the link is shared";
  if (/(favicon|apple-icon|(^|\/)icon)\.[a-z]+$/.test(p)) return "the site's icon";
  if (/manifest\.(json|webmanifest|ts)$/.test(p)) return "the app settings for phones";
  if (/not-found|(^|\/)404\./.test(p)) return "the \"page not found\" page";
  if (/privacy|terms/.test(p)) return "the privacy and terms pages";
  if (/readme/i.test(base)) return "the instructions for the project";
  if (/\.(test|spec)\.[a-z]+$|(^|\/)(tests?|__tests__)\//.test(p)) return "tests that check the code works";
  if (/\.(css|scss)$|tailwind/.test(p)) return "the look of the site (colors, fonts, spacing)";
  if (/(^|\/)(next|vite|astro|tailwind|postcss|eslint|tsconfig|babel|app)\.?config|tsconfig|package\.json$|vercel\.json|eas\.json|app\.json/.test(p))
    return "the project's settings";
  if (/(^|\/)\.env/.test(p)) return "the project's settings (keys stay hidden)";
  if (/(^|\/)(data|content|questions|seed)[^/]*\/|\.(json|csv|md|mdx)$/.test(p)) return "the content";
  if (/(^|\/)(api|server|actions|routes?)\//.test(p) || /route\.(ts|js)$/.test(p)) return "the behind-the-scenes code that handles requests";
  if (/(^|\/)(lib|utils|hooks|store|state)\//.test(p)) return "the logic behind the features";
  if (/layout\.[a-z]+$/.test(p)) return "the layout every page shares";
  const page = p.match(/(?:^|\/)(?:app|pages|src\/app|src\/pages)\/(.*?)\/?(?:page|index)\.[a-z]+$/);
  if (page) return page[1] ? `the ${words(page[1].split("/").filter((s) => !s.startsWith("(")).pop() ?? "")} page` : "the home page";
  if (/(^|\/)components?\//.test(p)) return `the ${words(base)} part of the page`;
  return `the file ${base}`;
}

/** A shell command in plain words. */
function commandMeaning(cmd: string): string {
  const c = cmd.trim().replace(/^cd\s+\S+\s*&&\s*/, "");
  const is = (re: RegExp) => re.test(c);
  if (is(/create-(next|expo|vite|react|astro|svelte)|npm create|pnpm create|yarn create|degit|npx (sv|nuxi) (create|init)/)) return "Setting up a new project";
  if (is(/\b(npm (i|install|ci|add)|pnpm (i|install|add)|yarn (add|install)|bun (i|install|add)|pip3? install|poetry add)\b|^yarn\s*$/))
    return "Installing the building blocks the project needs";
  if (is(/lighthouse/)) return "Checking speed, accessibility and search-friendliness";
  if (is(/\b(test|vitest|jest|playwright test|pytest|cypress)\b/)) return "Running the tests to make sure everything works";
  if (is(/\b(tsc|eslint|lint|typecheck|prettier|biome)\b/)) return "Checking the code for mistakes";
  if (is(/\b(next build|vite build|astro build|npm run build|pnpm (run )?build|yarn build|expo export)\b/)) return "Building the site to make sure it works";
  if (is(/\b(next dev|vite|npm run dev|pnpm (run )?dev|yarn dev|npm start|serve|http-server|expo start)\b/)) return "Starting a preview to look at";
  if (is(/\bcurl\b|\bwget\b/)) return "Checking a web address";
  if (is(/\bgit\b/)) return "Saving the work in the code history";
  if (is(/\b(mkdir|touch|cp|mv|rm)\b/)) return "Organizing the project's files";
  if (is(/\b(ls|find|tree|cat|head|tail|grep|rg|wc)\b/)) return "Looking through the project's files";
  if (is(/\b(node|python3?|tsx|ts-node)\b/)) return "Running a small script";
  return "Running a command";
}

/** What the agent is doing, from the tool call it's making. */
export function explainTool(rawTool: string, args: Record<string, any> = {}): Doing {
  const tool = String(rawTool ?? "").replace(/^mcp__todd__/, "");
  const a = args ?? {};
  const cmd = String(a.command ?? "");
  switch (tool) {
    case "shell":
      return { plain: commandMeaning(String(a.cmd ?? "")), detail: a.cmd ? short(String(a.cmd), 90) : undefined };
    case "write_file":
      return { plain: `Writing ${fileMeaning(String(a.path ?? ""))}`, detail: a.path };
    case "read_file":
      return { plain: `Reading ${fileMeaning(String(a.path ?? ""))}`, detail: a.path };
    case "list_files":
      return { plain: "Looking through the project's files", detail: a.path };
    case "git_push":
      return { plain: "Saving the code to GitHub", detail: a.repo ? `git push → ${a.repo}` : "git push" };
    case "git": {
      const sub = cmd.split(" ")[0];
      const plain =
        sub === "push" ? "Saving the code to GitHub" : sub === "pull" || sub === "fetch" ? "Getting the latest code from GitHub"
        : sub === "clone" ? "Copying code from GitHub" : sub === "commit" || sub === "add" ? "Saving the work in the code history"
        : "Checking the code history";
      return { plain, detail: `git ${short(cmd, 80)}` };
    }
    case "gh": {
      const plain = /^repo create/.test(cmd)
        ? `Creating a ${/--public/.test(cmd) ? "public" : "private"} home for the code on GitHub`
        : /^release/.test(cmd) ? "Publishing a release on GitHub" : /^pr /.test(cmd) ? "Proposing a change on GitHub" : "Working with GitHub";
      return { plain, detail: `gh ${short(cmd, 80)}` };
    }
    case "cli": {
      const svc = String(a.service ?? "");
      const name = service(svc.toLowerCase()) || svc;
      const preview = /--target[= ]preview|channel:deploy/.test(cmd) || (svc === "netlify" && !/--prod/.test(cmd));
      const plain = /domains? (add|buy)/.test(cmd) ? `Setting up the address on ${name}`
        : /\bdeploy\b|^up\b/.test(cmd) ? (preview ? `Putting up a preview on ${name}` : `Putting it live on ${name}`)
        : /\benv\b/.test(cmd) ? `Changing settings on ${name}` : `Working with ${name}`;
      return { plain, detail: `${svc} ${short(cmd, 80)}` };
    }
    case "eas": {
      const plain = /^build/.test(cmd) ? "Building the app in the cloud" : /^submit/.test(cmd) ? "Sending the app to Apple / Google"
        : /^update/.test(cmd) ? "Sending an update to the app" : "Working on the app with Expo";
      return { plain, detail: `eas ${short(cmd, 80)}` };
    }
    case "browse":
      return { plain: "Using a website in the browser", detail: a.task ? short(String(a.task), 90) : undefined };
    case "browser_start":
      return { plain: "Opening the browser", detail: a.why_not_api ? short(String(a.why_not_api), 90) : undefined };
    case "browser_navigate": {
      const h = host(a.url);
      const where = !h ? "a page" : /^(localhost|127\.0\.0\.1)(:|$)/.test(h) ? "the preview of the site" : service(h);
      return { plain: `Opening ${where} in the browser`, detail: a.url };
    }
    case "browser_click":
    case "browser_type":
    case "browser_keys":
    case "browser_select":
      return { plain: "Clicking through a website", detail: a.what || undefined };
    case "browser_console":
      return { plain: "Checking the site for errors" };
    case "browser_save_secret":
    case "browser_save_download":
    case "vault_store":
      return { plain: "Keeping a key safe in the vault", detail: a.name };
    case "spawn_agent":
      return { plain: `Bringing in a teammate: ${a.name ?? "a new agent"}`, detail: a.task ? short(String(a.task), 90) : undefined };
    case "wait_for_agents":
      return { plain: "Waiting for its teammates to finish" };
    case "message_agent":
      return { plain: "Giving a teammate new instructions" };
    case "cancel_agent":
      return { plain: "Stopping a teammate" };
    case "ask_human":
      return { plain: "Waiting for your answer", detail: a.question ? short(String(a.question), 90) : undefined };
    case "request_approval":
      return { plain: "Waiting for your OK", detail: a.action };
    case "authorize_purchase":
      return { plain: "Asking you to approve a purchase", detail: a.merchant && a.amount_usd != null ? `${a.merchant} · $${a.amount_usd}` : undefined };
    case "go_live":
      return { plain: "Asking to put it live", detail: a.where };
    case "plan_launch":
      return { plain: "Checking where you want it to live" };
    case "find_integrations":
      return { plain: "Working out the best way to connect to each service" };
    case "check_accounts":
    case "request_signins":
      return { plain: "Checking which accounts are signed in" };
    case "cli_login":
      return { plain: `Connecting your ${service(String(a.service ?? "")) || "account"}`, detail: a.service };
    case "api_request":
      return { plain: `Talking to ${host(a.url) ? service(host(a.url)) : "a service"}`, detail: [a.method, a.url].filter(Boolean).join(" ") };
    case "fetch_url":
      return { plain: "Reading a web page", detail: a.url };
    case "vercel_buy_domain":
      return { plain: "Buying the domain", detail: a.domain };
    case "vercel_check_domain":
      return { plain: "Checking if a domain is free and what it costs", detail: a.domain };
    case "vercel_add_domain":
      return { plain: "Connecting the domain to the site", detail: a.domain };
    case "vercel_create_project":
      return { plain: "Setting up hosting for the site", detail: a.name };
    case "vercel_deploy":
      return { plain: "Putting it live", detail: a.project };
    case "vercel_set_env":
      return { plain: "Setting the site's settings on Vercel", detail: a.key };
    case "github_create_repo":
      return { plain: `Creating a ${a.private === false ? "public" : "private"} home for the code on GitHub`, detail: a.name };
    case "finish":
      return { plain: "Wrapping up and writing the summary" };
  }
  if (tool.startsWith("browser_")) return { plain: "Using a website in the browser" };
  if (tool.startsWith("vercel")) return { plain: "Working with Vercel" };
  if (tool.startsWith("github")) return { plain: "Working with GitHub" };
  if (tool.startsWith("vault")) return { plain: "Using the vault" };
  const mcp = tool.match(/^mcp_([a-z0-9-]+?)_(.+)$/i);  // a connected MCP server's tool
  if (mcp) return { plain: `Working with ${mcp[1][0].toUpperCase()}${mcp[1].slice(1)}`, detail: mcp[2].replace(/_/g, " ") };
  return { plain: `Using ${tool.replace(/_/g, " ")}` };
}
