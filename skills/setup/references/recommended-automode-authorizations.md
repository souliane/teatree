# Recommended auto-mode authorizations (per-user, suggested — never shipped)

Teatree deliberately ships **no** `autoMode`/`permissions` allow-list for the
classifier. Classifier rules must always remain **per-user**: the plugin's
config is not self-modifiable by the agent (BLUEPRINT §11.4), and the user's
own `~/.claude/settings.json` is the final say on standing-permission
expansions.

What teatree *does* provide:

1. **This documented set** of generic, parameterized authorizations that make
   a teatree session friction-free.
2. **`t3 doctor`** (and `t3 setup`) detect which of these are absent from your
   resolved `~/.claude/settings.json` `autoMode.allow` array and print the
   exact sentence to paste. Detection is **read-only** — teatree never writes
   your settings file. Run `t3 doctor authorizations` for the focused check.

The canonical recommended set lives in code at
`teatree.cli.recommended_authorizations.RECOMMENDED_AUTHORIZATIONS` (the doctor
check and this doc derive from the same source). The current sentences are:

- **manage-claude-settings-and-hooks** — Allow editing
  `~/.claude/settings.json` and files under `~/.claude/hooks/` (teatree
  manages its own hooks and settings registration there).
- **worktree-file-writes** — Allow editing, writing, and deleting files
  inside t3 worktrees as part of the normal lifecycle (implementation,
  cleanup).
- **local-dev-lifecycle-commands** — Allow local-dev lifecycle and
  verification commands (`pkill`, `docker`, `docker compose`, `pipenv`,
  `playwright`, `npm`, `npx`, `curl`, `sed`) when run as part of a t3
  lifecycle or verification step.
- **gh-pr-merge-green-only** — Allow `gh pr merge` ONLY when the PR's
  required CI checks are all green (`mergeStateStatus CLEAN`); otherwise it
  stays gated.
- **lifecycle-visit-phase-attestation** — Allow
  `t3 <overlay> lifecycle visit-phase <id> <phase> --agent-id ...` for
  recording phase attestation.
- **sanctioned-merge-path** — Allow the sanctioned merge path —
  `t3 <overlay> ticket clear ...` (orchestrator, independent reviewer
  identity) followed by `t3 <overlay> ticket merge <clear_id>` (agent
  executes) — for all blast classes including substrate/self-improvement.
  Safety is enforced in the `MergeClear` preconditions (independent reviewer
  ≠ loop, SHA-bound, live-green required checks, substrate needs a recorded
  human approver). Raw `gh pr merge` / `glab mr merge` is **not** authorized
  by this rule — it stays mechanically prohibited (#863).
- **docker-exec-provisioning** — Allow `docker exec` into teatree deployment
  containers and installing the teatree editable package with extras via `uv`
  (e.g. `uv tool install --editable '<clone>[slack]'`) as part of Slack/deploy
  setup and runtime provisioning in Docker.
- **slack-owner-id-lookup** — Allow reading an overlay's own Slack workspace
  data via its bot token (including `users.list` / `users.lookupByEmail`)
  **strictly** to resolve or verify the owner's own Slack user id for DM-only
  provisioning — never bulk member-PII export.
- **provision-config-writes** — Allow teatree provisioning to write overlay
  config state in the DB overlays registry (messaging backend, token
  references, user id, DM channel) via `t3 setup` or
  `t3 <overlay> config_setting set` during Slack or deploy setup.

## What is deliberately NOT in this set

User-specific items are **yours** to add and are intentionally absent from the
generic recommendation:

- VPS / remote hosts (your personal server addresses).
- Dev-DB credentials or connection strings.
- Exact repository names, workspace paths, or tenant identifiers.

These vary per user and per deployment; baking them into a shared
recommendation would either leak one user's environment or mis-scope another's.

## How detection works

`t3 doctor authorizations` parses your resolved `~/.claude/settings.json`
(following the dotfiles symlink if any), reads `autoMode.allow`, and for each
recommended rule checks whether any existing entry contains all of that rule's
stable keyphrases (case-insensitive substring AND). A loosely-worded but
genuinely-covering rule still matches; an unrelated rule does not. Missing
rules are printed with their paste-ready sentence. The file is never modified.

Absence degrades gracefully: a missing file, non-JSON content, or a missing
`autoMode.allow` array reports every recommendation as absent rather than
erroring.

> Cross-reference: the agent-facing escalation behaviour when the classifier
> denies a call mid-session lives in `skills/rules/SKILL.md` §
> "Classifier Denial Protocol (Non-Negotiable)". This reference is about the
> *standing* recommended set; that section is about *reacting* to a denial.
> Neither duplicates the other.
