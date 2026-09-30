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
  database, reaching the dashboard or stealing browser cookies
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
- Code already running in the sandbox *during* a `git_push` reading that process's environment. This is a
  documented limit (see [ARCHITECTURE.md](ARCHITECTURE.md#security-model)).

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

Known limits:

- The sandbox runs every command as one user. Code already running there while a signed-in command runs (a
  `git push`, a `cli(...)` deploy) could read that command's environment or its temporary sign-in files. Their
  output is scrubbed of those tokens, but the next step is a separate user (or a credential proxy) for signed-in
  commands.
- Format-based redaction can't recognize every secret: an ordinary-looking password shown on a page is only
  hidden if it's in the vault.
- Screenshots and Claude Code session transcripts are kept on disk until you delete the run.

## Hardening your deployment

- Keep the published ports bound to `127.0.0.1`. For remote access, use an SSH tunnel or a private network
  such as Tailscale, never a public port.
- Change `SANDBOX_TOKEN` and `POSTGRES_PASSWORD` in `.env`, and set `VNC_PASSWORD` if other people can reach
  the machine.
- Use a virtual card with a low limit for browser checkouts, and keep the auto-approve limit small.
- Only store vault keys you're comfortable letting agents use. See the notes on `api_request` in the README.
- Rebuild regularly (`docker compose build --pull`) to pick up base-image and dependency fixes.
