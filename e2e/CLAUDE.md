# e2e — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This file adds only what is specific to `e2e/`.

- **Load `/t3:e2e` before working here.** It covers Playwright test writing, running, visual snapshots, evidence posting, and the pre-push visual QA gate.
- **Full worktree per PR (non-negotiable).** Each PR under test gets its own backend + frontend via `t3 <overlay> worktree provision` + `t3 <overlay> worktree start`. Never mix one worktree's backend with another's frontend; never hand-patch an incomplete worktree — delete and recreate.
- **Evidence comes from the deployed environment**, never from local builds or `localhost` screenshots (`/t3:rules` § "Evidence Comes From the Deployed Environment").
- **Never blindly accept snapshot baselines** — verify the captured PNG shows the asserted state before updating (`/t3:e2e` visual QA gate).
