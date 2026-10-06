# Changelog

All notable changes to Todd are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor versions may include breaking
changes).

## [Unreleased]

### Added

- **Slideshow videos (video, step 1):** a `video` toolset (`video_new`, `video_find_shots`, `video_pick`,
  `video_render`) that writes a storyboard to `video/<slug>/storyboard.json`, searches each shot by meaning across stock
  photos (Pixabay, Pexels) and the run's own images (`video/library/`, or paths the agent names), ranks them by fit to
  the shot and to the look of the shots already picked (`text_score`, `style_score`, a small same-photographer bonus),
  and renders a 1080×1920 MP4 (`video/<slug>/<slug>.mp4`: H.264, 30 fps, a slow zoom on each slide or `motion="none"`,
  optional music from the run folder trimmed and faded to fit), the captioned slides it's made of
  (`video/<slug>/slides/NN.png`, also a TikTok photo post; stock photos fill the slide, the run's own screenshots are
  framed on a blurred backdrop, a tall phone screenshot large enough to read: it keeps its top part and fades out at
  the bottom), a contact sheet (`preview.png`) and `CREDITS.md`. Stock photos need a free
  `PIXABAY_API_KEY` in the vault (Pexels has paused new API keys; an existing `PEXELS_API_KEY` works too, and both are
  searched when both are there). Pixabay search responses are cached for 24 hours and its expiring download links are
  renewed when a photo is picked, as its API terms ask. Both are in the integrations catalog.
- **Feedback on the free scaffold before anything is spent:** `short_review` renders a numbered scaffold version
  (beats labelled b1, b2… in the corner) and asks the human through the usual question card: approve (with the
  generation price), "Slower", "Faster", or anything in their own words; every answer is kept with the version it
  was about, presets are applied, and the agent acts on the rest and reviews again. `short_pace` sets how fast a
  short moves: voice speed, a pause after every line, the least time any shot stays on screen (short lines are held
  automatically) and words per caption page. The default pace is calmer than before.
- **What's working, as data (`trends` toolset):** `trend_scan` pulls recent TikToks for a niche's hashtags through
  Apify (`APIFY_API_TOKEN` in the vault; one priced spend per scan, the actual cost recorded) and ranks them by reach
  (plays ÷ the creator's followers) and share rate, so videos that broke out of small accounts come first;
  `trend_analyze` studies a few: TikTok's subtitles or a free local transcription, caption, sound, metrics and a frame
  contact sheet, with the downloaded video deleted once its frames are taken; `format_save` / `format_search` keep a
  shared library of format cards (hook, beats, why it works, how a product fits in without becoming an ad, the real
  examples with their numbers). Apify's token only ever goes to api.apify.com. Recordings can start positioned on a
  section (`start_at`).
- **Todd films its own products:** `short_record` opens a 9:16 phone tab (or a desktop one) in the agents' browser,
  runs simple steps (wait, scroll, scroll to, tap, type, go to, mark) with real touch gestures, and saves a 30 fps
  recording with the time of every step, so a shot can land a recorded moment on a spoken word. Todd built the
  product, so it records the real pages and flows (live site or local preview). Recordings only read: no taps on
  social or payment sites or on buttons that buy, post, send, delete or approve, no typing into password fields, no
  form submits. The media service assembles the frames (`/screencast/put`, `/screencast/assemble`).
- **A free scaffold:** `short_voiceover` uses a local voice by default (Piper, timed by a local faster-whisper
  recogniser; caption words come from the script, only the times from what was heard), so a whole scaffold (voice,
  captions, Todd's recordings, priced AI placeholders) costs nothing. ElevenLabs (`provider="elevenlabs"`) is the final
  voice, after the human approves. Both models are baked into the media image.
- **Shorts timed from the voice (video, step 2 foundation):** a `shorts` toolset (`short_new`, `short_edit`,
  `short_voices`, `short_voiceover`, `short_plan`, `short_render`). The voiceover is rendered first (ElevenLabs, one
  take per hook variant with character timestamps, one charge for all takes) and is the master clock: the planner
  (`shorts_plan.py`) cuts 2 frames ahead of each line, adds holds in the pauses between lines without stretching the
  voice, lands a clip's moment on a chosen word (`shot.sync`), pages captions from the same timestamps, fits each
  recording (trim, slight slow-down, short freeze, or sped up to fit) and gives every AI shot an exact length and
  price. A sync report flags anything outside the limits. The media service renders the plan (`/render/timeline`):
  exact-frame segments joined video-only, one continuous voice track placed by sample offsets (a test checks the cut
  and the voice agree within one frame), word-highlighted captions and on-screen text via libass, an optional music
  track ducked under the voice, and labelled placeholders for the animatic. Also `/files/put` (the API hands over the
  voiceover bytes) and `/probe`. ElevenLabs is in the integrations catalog. No AI video is generated yet; see
  `docs/video-step2-plan.md`.
- **Videos play in the Files view:** `.mp4`, `.webm`, `.mov` and `.m4v` files get a player, streamed by
  `GET /api/runs/{id}/files/media?path=` with byte ranges (Safari needs them).
- **`media` service:** a new container for image embeddings (CLIP ViT-B/32 via fastembed, ONNX on CPU, models
  baked into the image), slide composition (Pillow) and MP4 rendering (FFmpeg). Own network to the API, token auth,
  the shared workspace, no vault; downloads only from `MEDIA_FETCH_HOSTS`. Embeddings are cached in a new
  `MediaAsset` table (a stock photo is embedded once, whichever run finds it) and searched in Python, or with
  pgvector when the database has the extension (the compose Postgres image is unchanged for now: see
  media_index.py).

- **Launch plan** for website goals: at the start, one card asks where it should live (your domain, a new domain,
  or a free address for now), whether the GitHub repository is private (default) or public, and whether to ask
  before deploying. Agents keep building meanwhile. Enforced in code: deploys (previews too), connecting a domain,
  Git connections that deploy every push and Deploy/Publish clicks on hosting dashboards wait for your OK unless
  you chose "when it's ready"; new repositories are private unless you chose public. Tools: `plan_launch`,
  `go_live`.
- **Ready for real visitors?** checklist on a finished website run, read from the project's files: titles and
  descriptions, social preview, icons, robots.txt, sitemap.xml, llms.txt, 404 page, security headers (plus manifest
  and privacy page, recommended). Tap a missing item, or "Add the N missing", to ask Todd for it.
  API: `GET /runs/{id}/launch-check`.
- **Right now in plain words:** each agent's line says what it's doing in everyday language ("Building the site to
  make sure it works", "Writing the streak badge part of the page", "Putting up a preview on Vercel"), with its own
  words and the actual command or file shown smaller underneath.
- **Questions you can tap:** `ask_human(options=[…])` shows the choices as buttons, with "Something else…" for your
  own answer.

- **Usage** for every run and across runs: model tokens (in, out, cached) and calls per agent and model, tool
  calls, browser steps, time, and cost (billed on the API engine; on your Claude plan, what it would cost at API
  prices). A Usage view on the run page and a Usage page with tokens per day, the runs that used the most and
  per-model totals. API: `GET /runs/{id}/usage`, `GET /usage?days=`.
- **Right now**: a live card on a running run showing what each agent is trying to do (its latest thought) and
  what it's doing this moment (the tool it's running, in plain words), and who's waiting on you or paused.
- **Suggested next steps** under a finished run: the planner's own "Try next" prompts plus production-readiness
  steps that fit what was built (security headers and rate limits, Firestore rules, Supabase row-level security,
  live-mode Stripe with verified webhooks, store review, CI, domain email, backups). Tap one to put it in the
  Continue box.
- **Sign in with Squarespace** for domains (Settings → Domain registrant, and the Accounts page): Squarespace
  Domains (where Google Domains went) has no API, so agents set up DNS in the browser with your sign-in, following
  a DNS playbook from `find_integrations` (e.g. Vercel's A and CNAME records). Goals that mention Squarespace or
  Google Domains ask for the sign-in up-front.
- **Previews on localhost:** `http://localhost:PORT` in the agents' browser reaches the same port in the sandbox
  (ports 3000, 3001, 4173, 4321, 5000, 5173, 8000, 8080, 8081, 19006, relayed through the API), so agents look at
  what they're building without deploying it. Agents are told never to deploy just to look at something.
- **`git` tool**: normal git (`push -u origin main`, `pull`, `clone`, `status`…) signed in to GitHub for commands
  that talk to a remote. `ask_human(secret_name=…)` collects a secret into the vault through a password field.
  `shell` and `api_request` take `save_to_vault` to store a secret they return without the model seeing it.
  `browser_type` types `{{secret:NAME}}`.

- **Files** view for every run: a read-only browser over the run's folder in the sandbox (what its agents made or
  changed), live while the run works. Recently changed files, a folder tree (installed packages and build output
  listed but not opened), code with line numbers, Markdown formatted (or its source), images, and downloads.
  Vault values are masked everywhere, `.env` values are hidden until you show them, and private key files are
  never shown. Nothing outside the run's folder is reachable, symlinks included. API: `GET /runs/{id}/files`,
  `…/files/view`, `…/files/download`; sandbox: `/files/tree`, `/files/view`.
- Vault keys by run: each secret remembers the run and agent that saved it (or that you added it, or that it came
  from a CLI sign-in). Settings → Vault groups keys that way, the Files view lists the keys its run saved, and
  `GET /secrets?run_id=` filters by run.
- Plain-language recaps: the planner's summary starts with **In plain words** (what now exists and what you can do
  with it) and ends with **Terms** for any jargon; every agent is told to explain technical words the first time
  it uses them with you.

- Accounts up-front: when a run starts, Todd reads which accounts the goal needs (services you name, plus what
  the work implies, e.g. an iPhone app → Expo, App Store Connect, Apple Developer, GitHub), skips those it can
  already use through a key or connected CLI, and pauses on one **Sign in to continue** card: a Sign in button per
  service (opens the login page in the live browser), live status, and Later / Continue. The card closes itself
  once everything is signed in; Todd then connects those CLIs and tells the planner what was left for later.
  `request_signins` shows the same card.
- Expo signs in once: `cli_login("expo")` (and signing in to Expo on the Accounts page) approves `eas login --browser`
  with the browser session; the `eas` tool uses it, with `EXPO_TOKEN` as a fallback.
- Keys Todd sets up itself: `find_integrations` has a `setup` route with the steps for services whose keys are
  created in the browser. App Store Connect: generate a Team API key and save the Issuer ID, Key ID and `.p8`
  straight into the vault.
- `browser_save_download` (and `save_download_to_vault` / `save_to_vault` for the browse() agent): click a Download
  button and put the file's contents in the vault without the model seeing it or it landing in Downloads.
- iPhone and Android apps: an `eas` tool runs the Expo EAS CLI in the sandbox, signed in with `EXPO_TOKEN` from the
  vault. When the vault has an App Store Connect API key, EAS gets it for one command at a time (written to a
  private file and removed afterwards) so it can sign iOS builds and upload to TestFlight. `cli("expo", …)` routes
  to it. The sandbox image now includes `eas-cli`; `find_integrations` knows Expo (and "iphone", "react native")
  and asks for the token when it's missing; the planner prompt has a mobile playbook. `EXPO_TOKEN` is protected
  like `GITHUB_TOKEN`.
- Sign in once: signing in to GitHub, Vercel, Netlify, Railway, Cloudflare, Stripe or Firebase (Accounts page,
  Setup) also signs in that service's CLI with the same session. Todd approves it in the browser itself and only
  asks you for a password/2FA page. Agents can do the same with `cli_login`.
- `cli` tool: run a connected CLI (`cli("vercel", "deploy --prod --yes")`) with its saved sign-in.
- `gh` tool: the GitHub CLI in the sandbox, signed in per command from the vault. The sandbox image now includes `gh`.
- `browser_console`: JavaScript errors, console warnings and failed requests on the current page, for agents
  checking the sites they build. The prompts now say verifying your own work is a normal use of the browser.
- `browser_read_text`: copy the full text of an element or page (shadow DOM and field values included, never
  password fields) into the agent's work.
- `browser_save_secret`: save a key a page shows straight into the vault without the model seeing it.
- Continue a finished run: message its planner (in its window, the run summary or the timeline) and it picks its
  conversation back up, on both engines.
- Rename, run again, copy the prompt of, stop or delete a run from the sidebar or the runs list; **Edit** in the
  sidebar deletes several at once. Deleting keeps ledger entries and workspace files.
- Finished, failed and stopped agents fold into a compact "Finished agents" tray on the run page. Open one to see
  its window again, or hide it.
- **Take control** opens the agents' browser in a large window you can click and type in. Sign-in prompts and CLI
  rows that need you have an **Open browser** button.

### Changed

- The sandbox's `agent` user now has an explicit uid (1001, the same it had), which the `media` container shares so
  both can write run folders.

- **Agents work side by side without touching the same code:** one agent writes the code; helpers run alongside it
  for work that feeds it without editing it (content or data in their own files, like a questions file the site
  reads; research; assets); reviews and fixes come after the build and deploying is last. Todd refuses a write to
  a file another running agent is working on. Default limits: 6 agents at once, 16 per run.
- **Deploying asks first and comes last:** every deploy, previews included, waits for your OK (unless the launch
  plan says "when it's ready"), and a deploy is refused while another agent is still working on the project.
- **Production readiness is part of every site build** (the builder's last tasks: SEO and sharing, robots.txt,
  sitemap.xml, llms.txt, 404, security headers, accessibility, Lighthouse), and "Try next" leads with what's left
  for production. Suggested next steps keep up to three production-readiness items so feature ideas can't crowd
  them out.
- `git_push` pushes like a developer would: to `origin` (set from `repo` when given) with upstream tracking, the
  current branch instead of renaming it to main, a default .gitignore when the project has none (node_modules,
  .env, build folders, key files) and `--force-with-lease` for force. Before, agents couldn't push with plain git
  (the sandbox has no GitHub credentials) and fell back to odd workarounds.
- Secrets in context (security review): tool results are scrubbed string by string before serializing (JSON
  escaping hid multi-line keys), including encoded forms and CLI sign-in tokens; secret-shaped strings are hidden
  from models and the timeline; events are scrubbed before they're stored; the browser blurs secret-looking text
  before every screenshot and redacts page text, console output and URLs; checkouts give the browser agent no
  page-reading actions or form values and blank the checkout tabs afterwards; `read_file` hides `.env` values;
  `fetch_url`/`api_request` refuse internal and private addresses and don't follow redirects with a secret; the
  CLI blocklist sees subcommands after global flags; the vault shows only the last 4 characters. See SECURITY.md.
- The completed-run screen is redesigned: the recap renders as Markdown (lists, bold, code, links) in sections:
  In plain words, What got done, What you got (clickable link cards), What's next for you, Who did what, Words to
  know and How. Technical words explain themselves on hover or tap (a built-in glossary plus the recap's own
  Terms). Agent recaps in windows use the same renderer, including older one-line recaps.
- The **Sign in to continue** card says why: a short "Why these accounts" line from the goal (e.g. "You're making
  an iPhone app, so Todd needs to build it, sign it and upload it to the App Store."), and each account's row says
  what Todd uses it for ("Builds the app in the cloud").
- Agent windows are a little bigger (taller, wider minimum) with slightly larger text.
- `spawn_agent` runs agents in the background by default, and `wait_for_agents` ends early when you message the
  planner, so it can act on the message while agents keep working.
- Sending a message to a paused agent (or the planner) resumes it.
- The planner can't finish while agents it spawned are still running.
- A signed-in account's CLI now connects automatically when the Accounts page or Setup loads (one at a time, once per
  start; the button retries). Disconnecting a CLI turns this off for it.
- CLI approval: when a site keeps its final Authorize/Allow button disabled until a person interacts, Todd scrolls to
  it, outlines it and asks you for that one click instead of waiting. After you finish a password or 2FA page, Todd
  takes over again on the next page.
- `find_integrations` routes a service that isn't connected but has a browser-session login to `connect`
  (`cli_login`). Agents are told never to create tokens in the browser or copy credentials through their context.

### Fixed

- The planner no longer blocks on a sub-agent: messages sent while it waited weren't seen until the agent finished.
- `find_integrations` reported GitHub as "ready" without a `GITHUB_TOKEN` (the built-in `github` toolset was
  mistaken for a plugin).
- Sandbox commands that left a process running in the background (dev servers, CLI logins) hung until their
  timeout, because leaked descriptors kept the output stream open.
- A sandbox command that timed out lost everything it had printed; the output is now kept.
- The sandbox runs with an init process, so exited background processes are reaped.
- **Take control** didn't let you control the browser (only the new-tab view did): noVNC treats `view_only=0` as
  on. The Setup and Accounts browsers were affected too.
- Vercel's device-code field (`autocomplete="one-time-code"`) was mistaken for a 2FA prompt, so the Vercel CLI
  sign-in stopped for you right away.

### Security

From an outside review of spending and public-action controls, plus what checking it turned up:

- **The live view had no password,** so any website you visited could connect to `localhost:6080` (browsers don't
  limit WebSockets to the page's own site) and use the signed-in browser. It now always has one: random unless
  you set `VNC_PASSWORD`, shared with the API and put in the dashboard's live-view URL. The raw VNC port listens only
  inside the browser container, and Chromium no longer accepts debugging connections from web pages.
- **The dashboard only serves its own pages.** Its API route checks the Host (localhost, or `TODD_ALLOWED_HOSTS`),
  which stops DNS rebinding and Todd's own containers calling `http://web:3000`, and refuses requests a browser marks
  as coming from another site or origin (CSRF, including "simple" GETs and body-less POSTs like sign-out). Signing
  in a CLI after an account is signed in moved from `GET /api/accounts` to `POST /api/accounts/auto-connect`.
- **Purchases and public actions are checked in code** (`gates.py`), not only by instructions: purchase-looking
  clicks, payment and billing pages, and known purchase APIs and commands need `authorize_purchase` (new, every
  agent; human-approved, in the Ledger); posting/messaging/email sites need `request_approval(sites=[…])`; known
  posting APIs and publishing commands (npm publish, docker push, eas submit, public GitHub repos, releases and
  gists, live-mode Stripe changes) show the exact request and wait. Scripts can't click or submit on websites.
- **Card checkouts:** card details only on the exact approved sites (no subdomains); a shared payment page such as
  checkout.stripe.com must be approved together with the merchant and gets the card only when the tab came from the
  merchant; the page's total must fit the approved amount.
- **Signed-in commands run in their own container** (`signedin`, same image and workspace): git push/pull, `gh`,
  deploy CLIs, EAS and CLI sign-ins. Code running in the sandbox can't read their tokens or sign-in files anymore.
  npm-based CLIs run from the runner's own install, not the project's `node_modules` (`npx` picks the project's).
- **One browser lock for every run** and the Accounts page's CLI sign-ins (it was per run, so two runs could drive
  the browser at once).
- **Web tools check the address again when connecting,** so DNS rebinding can't reach internal services.
- Docs: SECURITY.md's "What needs a person, enforced in code" and known limits (recognition-based gates, a
  deploy running the project's own code with a sign-in, the GitHub sign-in's scope).

## [0.4.0] - 2026-09-25

First public release.

### Added

- Dynamic multi-agent planner: `spawn_agent` designs agents around groups of related work and runs them in
  parallel, with `wait_for_agents`, `message_agent` and `cancel_agent`.
- Two engines: **Your Claude plan** (headless Claude Code sessions, the default) and **API keys** (any
  LiteLLM model per tier, including local models via Ollama).
- Toolsets: code sandbox, signed-in browser (browser-use over CDP with a noVNC live view), Vercel, GitHub,
  web/API requests, vault, accounts, plugin files and MCP servers.
- API-first routing through `find_integrations`, with the browser as a last resort (`why_not_api`).
- Accounts catalog of about 70 services, with login detection and a setup wizard.
- Spend policy and ledger: auto-approve limit, per-run budgets, duplicate-charge guard, and human approval
  for all card payments.
- Approval flow for public actions (posting, messaging, publishing).
- Dashboard with one live window per agent, including reasoning, tool calls, browser steps, pause/resume,
  steering messages and summary cards.
- Crash-safe runs via LangGraph + Postgres checkpoints, with Resume.
- Editable prompts (`./prompts` or the dashboard).

### Fixed

- The API image failed to install its dependencies: `browser-use` 0.13.10 pins `mcp==2.1.1`, which
  conflicts with `langchain-mcp-adapters` 0.3.2 (`mcp<2`). Pinned `browser-use` to 0.13.9.

[Unreleased]: https://github.com/bobby-burns/todd/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/bobby-burns/todd/releases/tag/v0.4.0
