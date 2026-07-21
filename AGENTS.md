# Zoolanding Integrations Agent Workflow

Start with `README.md`, then verify the current branch and worktree before editing.

- Keep the core provider-neutral and draft-scoped.
- Never store or log credentials, secret values, provider payloads, hosted URLs, or customer PII.
- `dev` is local/CI only. This repository has no AWS dev stack or deployment workflow/profile.
- Work test-first and run three audit/fix/retest cycles before closeout.
- Do not deploy or call a provider without explicit authorization and the later rollout gates.
