# Custom prompts

Put `<name>.md` files here to override Todd's built-in prompts (mounted into the API container).

| File          | Used by                                                  |
|---------------|----------------------------------------------------------|
| `harness.md`  | Prepended to **every** agent — your house rules          |
| `planner.md`  | The orchestrator                                         |
| `worker.md`   | Template for every agent the planner spawns (its name, instructions and toolsets are filled in) |
| `browser.md`  | The browser agent (appended to browser-use's own prompt) |

Resolution order: dashboard override (Settings → Prompts) → file here → built-in default
(`services/api/todd/prompts/`). Copy a default as a starting point.

Placeholders: `{{today}}`; planner: `{{budget_usd}}`, `{{integrations}}`, `{{toolsets}}`; worker:
`{{agent_name}}`, `{{instructions}}`, `{{toolsets}}`, `{{run_goal}}`.

The defaults encode Todd's core behaviour, so keep these if you override them: APIs/MCP servers before the
browser (`find_integrations` first, `browse` needs `why_not_api`), group related work into few agents
(`spawn_agent(..., tasks=[...])`), and always end with a `finish` summary in the Done / Outputs / How /
Left-needs-you shape.
