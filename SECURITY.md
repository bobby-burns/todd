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

## Hardening your deployment

- Keep the published ports bound to `127.0.0.1`. For remote access, use an SSH tunnel or a private network
  such as Tailscale, never a public port.
- Change `SANDBOX_TOKEN` and `POSTGRES_PASSWORD` in `.env`, and set `VNC_PASSWORD` if other people can reach
  the machine.
- Use a virtual card with a low limit for browser checkouts, and keep the auto-approve limit small.
- Only store vault keys you're comfortable letting agents use. See the notes on `api_request` in the README.
- Rebuild regularly (`docker compose build --pull`) to pick up base-image and dependency fixes.
