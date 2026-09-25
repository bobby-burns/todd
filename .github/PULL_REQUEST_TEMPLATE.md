## Summary

<!-- What does this change and why? Link the issue it addresses, e.g. "Closes #123". -->

## Type of change

- [ ] Bug fix
- [ ] New feature or toolset
- [ ] Refactor / cleanup
- [ ] Documentation
- [ ] Build / CI / dependencies

## How was this tested?

<!-- Commands you ran, runs you tried in the dashboard, screenshots for UI changes (light and dark). -->

## Checklist

- [ ] `pytest` passes in `services/api`
- [ ] `npm run typecheck && npm run build` pass in `services/web` (if the dashboard changed)
- [ ] Docs updated (`README.md`, `ARCHITECTURE.md`, `prompts/README.md`) if behaviour changed
- [ ] `CHANGELOG.md` updated under **Unreleased**
- [ ] No secrets, tokens or personal data in code, tests, logs or screenshots
- [ ] If this touches spending, secrets, approvals, `git_push`, `api_request` or network isolation: tests
      cover the change, and the safety impact is explained above
