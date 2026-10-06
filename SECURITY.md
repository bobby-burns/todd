# Security Policy

Todd runs agents that can spend money, act from your signed-in accounts and execute code, so we take security
reports seriously.

## Supported versions

Todd is pre-1.0. Security fixes land on `main` and in the latest release.

| Version | Supported |
|---------|-----------|
| 0.4.x   | ✅        |
| < 0.4   | ❌        |

## Reporting a vulnerability

**Please don't open a public issue, discussion or pull request for security problems.**

Report privately through GitHub's
[private vulnerability reporting](https://github.com/bobby-burns/todd/security/advisories/new)
(Security tab → **Report a vulnerability**). Include:

- what the issue is and what an attacker could do with it
- steps to reproduce, or a proof of concept
- the version or commit, the engine (Claude plan or API keys) and any relevant configuration
- any ideas you have for a fix

You can expect an acknowledgement within a few days and updates as we investigate. Once a fix is released we'll
publish an advisory and credit you, unless you'd rather stay anonymous.

## Scope

Especially interested in:

- **Spend-policy bypass:** any way to charge money, or exceed a budget, without the approval the policy
  requires
- **Secret exposure:** vault values, integration tokens, model keys or card details reaching a prompt, event,
  log, screenshot or an unapproved host
- **Isolation breaks:** code in the `sandbox` or a page in the `browser` calling the API, reading the
  database, reaching the dashboard or stealing browser cookies; the `media` service fetching a host outside
  `MEDIA_FETCH_HOSTS` or touching files outside a run's folder
- **Approval bypass:** posting, messaging or publishing from a user's account without the approval flow
- **Prompt injection that defeats a code-level control:** content that gets an agent past a guard enforced
  in code (not just one that makes the model misbehave)
- `git_push` hardening, `api_request` host binding and the API token check

Out of scope:

- Exposing the dashboard to the internet. Todd has no dashboard login yet, and it binds to `127.0.0.1` for
  this reason (see [Known limits](README.md#status-and-known-limits)).
- A model making a poor decision that a code-level control then correctly blocks
- Vulnerabilities in third-party services or dependencies with no Todd-specific impact (please report those
  upstream)
- A signed-in command running the project's own code: a deploy that runs the project's build, `firebase.json`
  predeploy hooks, Expo's `app.config.js`. The project is agent-written, so this is a documented limit (see
  [Known limits](#known-limits)).

## How secrets stay out of the models' context

Reviewed September 2026 (a code audit of every path from a tool, page or person to a model). What's in place:

- **Known values are scrubbed everywhere.** Every string in a tool result is scrubbed of vault values before the
  result is serialized (JSON escaping used to hide multi-line keys from the match), and so is every timeline event
  (thinking, messages, tool arguments, browser steps). The scrub also catches a value's JSON-escaped, URL-encoded
  and base64 forms, each line of a multi-line key, and the tokens inside CLI sign-in snapshots.
- **Unknown secrets are caught by their shape** (`todd/redact.py`): private key blocks, Stripe, GitHub, GitLab,
  Slack, OpenAI, Anthropic, AWS, npm, SendGrid, Hugging Face and Replicate tokens, passwords in URLs, and
  secret-bearing URL parameters (`access_token`, `code`, `signature`…). On web pages, JWTs, Google API keys and
  long random strings are hidden too.
- **Pages can't leak a key into a screenshot.** Before either browser engine captures the page, anything that looks
  like a secret is blurred on screen, then restored, so the model, the timeline and saved screenshots only see the
  blur. Page text, read text, search results and console output are redacted the same way. Agents keep keys with
  `browser_save_secret` / `save_to_vault`, which copy them from the page straight into the vault.
- **New secrets never pass through the model.** `shell(save_to_vault=…)` and `api_request(save_to_vault=…)` store
  a secret that a command or API response returns, and the model sees `{{secret:NAME}}` instead. `browser_type`
  types `{{secret:NAME}}` without the model seeing the value.
- **People never paste secrets into chat.** `ask_human(secret_name=…)` shows a password field and saves the
  answer to the vault; a key pasted into a normal answer is hidden before the model (or the stored answer) sees it.
- **Checkouts:** during a card payment the browser agent gets no page-reading or script actions and no form values
  in the page state; afterwards the checkout tabs are sent to a blank page so the card isn't on screen for the next
  agent.
- **Settings files:** `read_file` and the dashboard's Files view show `.env` names, not values; private key files
  aren't shown at all.
- **Web tools stay on the public internet:** `fetch_url` and `api_request` refuse Todd's own services, loopback,
  link-local (cloud metadata) and private addresses, and never follow a redirect with an injected secret.
- **Git and CLIs:** GitHub sign-in reaches git only through a credential helper, for github.com, for one command,
  after the repo's config is checked for anything that could redirect the push or run code. Blocked CLI
  subcommands are recognized anywhere in the command, not only first.

## What needs a person, enforced in code

Reviewed September 2026, after an outside review found that spending and public actions outside three tools were
held back only by the agents' instructions. Now (`todd/gates.py`):

- **Purchases.** In the browser, a click on something labelled like a purchase (Buy, Upgrade to Pro, Subscribe,
  Pay $12, Place order, Start trial…), and anything done on a payment page (Stripe Checkout, PayPal, Paddle…) or a
  billing page (`…/billing`, `…/checkout`, `…/upgrade`), is held back until the agent has an approved purchase for
  that site: `authorize_purchase` (the human approves the amount; it goes in the Ledger) or a card checkout started
  with `payment_*`. Scripts can't click or submit on websites. Known purchase APIs (domain registrars, paid servers,
  phone numbers) and commands (`vercel domains buy`) need the same approval.
- **The amount.** Before a card is typed or a Pay button is clicked, the page's total must fit the approved amount
  (with 3% or $1 of room for rounding and tax).
- **Card details** are typed only on the exact approved sites (and their `www.`), never their subdomains. On a
  payment page every merchant shares (checkout.stripe.com, paypal.com…) the card is filled in only when the tab
  got there from the approved merchant's own site, and approving the shared page alone is refused.
- **Public actions.** Doing anything on a site where the human's account posts, messages or emails people (x.com,
  LinkedIn, Reddit, Discord, Gmail, Outlook…) needs their OK for that site (`request_approval(…, sites=[…])`), for
  30 minutes, for that agent. Reading and scrolling don't. Known posting and email APIs (X, LinkedIn, Meta,
  Reddit, Bluesky, Mastodon, Slack, Discord, Telegram, Gmail, SendGrid, Resend, Postmark, Mailgun, Twilio, GitHub
  issues and releases) and publishing commands (`npm publish`, `docker push`, `eas submit`, a public GitHub repo,
  release or gist, a live-mode Stripe change) show the human the exact request and wait for them.
- **Deploying and public code.** Every deploy (previews too), connecting a domain, a Git connection that deploys
  every push, and Deploy/Publish clicks on hosting dashboards wait for the human unless their launch plan says to
  put it live when ready, and a deploy is refused while another agent is still working on the project. New GitHub repositories are private unless the plan says public.
- **The dashboard only serves your own browser tabs.** It answers only to `localhost` (or names in
  `TODD_ALLOWED_HOSTS`), which stops DNS rebinding and Todd's own containers from calling it, and refuses any
  request a browser marks as coming from another site or another localhost port (CSRF). Nothing happens on a GET.
- **The live view has a password.** Unless you set `VNC_PASSWORD`, the browser container makes a random one and the
  dashboard's live view uses it. Without one, any website you visit could connect to `localhost:6080` and use the
  signed-in browser. The raw VNC port only listens inside the browser container, and Chromium no longer accepts
  debugging connections from web pages.
- **Web tools check the address when they connect,** not only before, so a name that turns into 127.0.0.1 between
  the check and the request (DNS rebinding) can't reach Todd's services.
- **Signed-in commands run in their own container** (`signedin`): git push/pull, `gh`, the deploy CLIs, EAS and CLI
  sign-ins. It shares the workspace with the sandbox but nothing else, so a dev server or an npm install script
  running in the sandbox can't read their tokens or sign-in files. npm-based CLIs run from the runner's own install,
  never from the project's `node_modules` (which the project's code could replace).
- **The media service is fenced in** (`media`, used by the video toolset): it has no vault access and only the API can
  call it (a random token the API makes). It downloads only over https from `MEDIA_FETCH_HOSTS` (default `pixabay.com`,
  `cdn.pixabay.com`, `images.pexels.com`; redirects to other hosts are refused, 20 MB cap, and anything that isn't an
  image is dropped), and it reads and writes only inside run folders, symlinks included. FFmpeg opens only local files:
  it renders slides the service re-encoded itself, and music only through a named audio demuxer, so a "song" that is
  really a playlist can't make it read other files or reach the network.
- **Trend data stays analysis:** the Apify token is only sent to api.apify.com (results that point elsewhere are
  never fetched with it); reference videos are downloaded only to take frames and are deleted right after, and
  nothing from them is republished.
- **Recordings only read** (`short_record`, the shorts toolset filming a product in the agents' browser, which is
  signed in to the human's accounts): no taps on social or payment sites, none on buttons that buy, post, send,
  delete or approve, no typing into password fields and no form submits; it holds the browser lock while it records.
- **One browser, one driver.** The browser lock covers every run and the Accounts page's CLI sign-ins, not just
  one run.

### Known limits

- The gates recognize purchases and posting by label, page and endpoint. A purchase with an unusual label on an
  ordinary page, or a posting API Todd doesn't know, gets through. The real limits on money are the run budget,
  the auto-approve limit and a virtual card with its own limit. Keep saved cards out of the agents' browser if you
  don't want agents near them.
- A signed-in command still runs the project's own code when the tool does: a deploy that builds locally, Firebase
  predeploy hooks, Expo's `app.config.js`. That code comes from agents, so it could read the token of the command
  running it. Use deploy tokens scoped to one project where the service offers them.
- The GitHub sign-in is broad (all your repositories, plus `workflow` so agents can set up CI). GitHub's CLI
  sign-in can't ask for less. To limit it, put a fine-grained token for chosen repositories in the vault as
  `GITHUB_TOKEN` instead of signing in.
- Format-based redaction can't recognize every secret: an ordinary-looking password shown on a page is only
  hidden if it's in the vault.
- Screenshots and Claude Code session transcripts are kept on disk until you delete the run.
- There's still no dashboard login: anyone who can use your computer's browser can use Todd.

## Hardening your deployment

- Keep the published ports bound to `127.0.0.1`. For remote access, use an SSH tunnel or a private network
  such as Tailscale, never a public port.
- Change `SANDBOX_TOKEN` and `POSTGRES_PASSWORD` in `.env`. The live view gets a random password by default; set
  `VNC_PASSWORD` to choose your own.
- Reaching the dashboard by another name (a Tailscale name, say)? Add it to `TODD_ALLOWED_HOSTS`.
- Use a virtual card with a low limit for browser checkouts, and keep the auto-approve limit small.
- Only store vault keys you're comfortable letting agents use. See the notes on `api_request` in the README.
- Rebuild regularly (`docker compose build --pull`) to pick up base-image and dependency fixes.
