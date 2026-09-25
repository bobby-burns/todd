# Contributing to Todd

Thanks for your interest in Todd! Bug reports, fixes, new toolsets, docs and design feedback are all welcome.
This guide covers how to get a development environment running and what we look for in a pull request.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Report a bug:** [open an issue](https://github.com/bobby-burns/todd/issues/new/choose) with steps to
  reproduce, the engine you used (Claude plan or API keys) and the relevant `docker compose logs api` output.
  Remove tokens, cookies and personal data first.
- **Suggest a feature:** open a feature request and describe the outcome you want, not only the
  implementation.
- **Report a security issue:** don't open a public issue. See [SECURITY.md](SECURITY.md).
- **Share a toolset:** a plugin file in `./plugins` is often the easiest way to add a capability. If it's
  broadly useful, open a PR.
- **Improve the docs:** typos, unclear steps and missing examples are all worth fixing.

For anything larger than a small fix, please open an issue first so we can agree on the approach before you
spend time on it.

## Development setup

Requirements: Docker with Compose v2.24+, Python 3.12, Node.js 22.

### Full stack (Docker)

```bash
cp .env.example .env
docker compose up -d --build
open http://localhost:3000
```

Rebuild a single service after changing it, e.g. `docker compose up -d --build api`.

### API (FastAPI + LangGraph)

```bash
cd services/api
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
TODD_DATA_DIR=$(mktemp -d) TODD_PLUGINS_DIR=tests/plugins pytest -q tests
```

With no `DATABASE_URL` set, the API uses SQLite in `TODD_DATA_DIR`, so no Postgres is needed for tests.
Models are replaced by a scripted fake (`tests/helpers.py`), so no API keys are needed either.

Some tests run only when their dependency is available and are skipped otherwise:

| Tests                                          | Needs                                                   |
|------------------------------------------------|---------------------------------------------------------|
| `test_claude_code.py`                          | the `claude` CLI on `PATH` (`npm i -g @anthropic-ai/claude-code`) |
| sandbox cases in `test_flow.py`, `test_safety.py` | the sandbox server (`SANDBOX_URL`)                   |
| `test_browser_live.py`, `test_accounts_live.py` | a Chromium reachable over CDP (`BROWSER_CDP_HOST`)     |

To run everything, including the live tests, inside the stack:

```bash
docker compose exec api sh -c 'pip install pytest && TODD_DATA_DIR=$(mktemp -d) DATABASE_URL= TODD_API_TOKEN_FILE= TODD_PLUGINS_DIR=tests/plugins SANDBOX_WORKSPACE=/workspace pytest -q tests'
docker compose exec api python tests/browser_smoke.py
```

### Dashboard (Next.js)

```bash
cd services/web
npm install
API_URL=http://localhost:8000 npm run dev
npm run typecheck && npm run build     # what CI runs
```

## Project layout

```
services/
  api/        FastAPI + LangGraph orchestrator, tools, vault, spend policy (Python)
    todd/     application code (see ARCHITECTURE.md for a module map)
    tests/    pytest suite with scripted fake models
  web/        Next.js dashboard (TypeScript, Tailwind v4, Motion)
  sandbox/    container where code agents run commands
  browser/    headful Chromium + noVNC live view
plugins/      user toolsets, loaded at startup
prompts/      user prompt overrides
```

[ARCHITECTURE.md](ARCHITECTURE.md) explains how a run works end to end and the security model.

## Guidelines

### Code style

- Match the surrounding code: naming, comment density and idiom.
- **Python:** 3.12, type hints on public functions, `async` for anything that does I/O. Tools use
  Google-style docstrings, because they become the schema the model sees, so write them for the model.
- **TypeScript:** strict mode must stay clean (`npm run typecheck`). Reuse the primitives in
  `components/ui.tsx`, the tokens in `app/globals.css` and the spring presets in `lib/motion.ts` rather than
  adding one-off styles.
- Keep dependencies pinned in `services/api/requirements.txt` and `services/web/package-lock.json`.

### Safety-critical code

Todd can spend money and act from the user's accounts, so some areas get extra scrutiny. Changes to these
need tests and a clear explanation in the PR:

- `policy.py`: spend authorization and the ledger
- `vault.py` and output scrubbing: secrets must never reach a prompt, event or log
- `tools/sandbox_tools.py` (`git_push`) and `tools/web.py` (`api_request` host binding)
- card handling in `agents/browser.py` and `tools/browser_direct.py`
- API token checks and network isolation in `main.py` and `docker-compose.yml`

The rule of thumb is that **policy lives in code, not in prompts**. A model should never be able to talk its
way past a spend limit or an approval.

### Tests

- Add or update tests for behaviour changes. `tests/helpers.py` has `ScriptedModel` and `call()` for
  scripting an agent's tool calls without a real model.
- Make sure `pytest` passes and the dashboard type-checks and builds before you open a PR. CI runs both.

### Commits and pull requests

- Keep PRs focused: one logical change per PR is easier to review and to revert.
- Write commit messages in the imperative mood ("Add Cloudflare toolset", not "Added…").
- Fill in the PR template, including how you tested the change.
- Update `README.md`, `ARCHITECTURE.md` or `prompts/README.md` if you change behaviour they describe, and
  add a line to [CHANGELOG.md](CHANGELOG.md) under **Unreleased**.
- UI changes: include a screenshot or short clip, in light and dark mode.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
