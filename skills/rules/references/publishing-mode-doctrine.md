# Publishing mode doctrine — resolution order and per-mode detail

The mechanics behind `/t3:rules` § "Publishing Actions Are Mode-Conditional (Non-Negotiable)". That section carries the decision — resolve the mode before every publishing decision, and the always-gated list that holds in both modes. This file carries the precedence chain and the per-mode expansion.

## Resolve the effective mode before every publishing decision

Do not assume interactive mode. Before saying "not pushed, your call", before asking "push?", and before prompting for any publishing confirmation, **actively resolve the effective mode in this order** (first match wins):

1. `T3_MODE` environment variable (`auto` or `interactive`).
2. Active overlay's per-overlay `mode` value in the `ConfigSetting` DB store (`config_setting set mode … --overlay <active>`, where `<active>` = `T3_OVERLAY_NAME` env var or the repo's registered overlay). The `[overlays.<active>] mode` TOML key is ignored on read.
3. Global `mode` value in the `ConfigSetting` DB store (`config_setting set mode …`). The `[teatree] mode` TOML key is ignored on read.
4. The DEFAULTS tier: the shipped `src/teatree/config/defaults.toml` value (`teatree.config.resolution._toml_default_rows`). This is the terminal step — there is no further fallback.

Assistant memory (`MEMORY.md`, `~/.claude/**/memory`, a per-project memory file) is **never** a tier. Publishing/merge doctrine resolves only from teatree's own stores — `AGENTS.md` § "teatree never reads assistant memory as a functional input" and BLUEPRINT §17.1 invariant 14, pinned by `tests/test_no_agent_memory_dependency.py`. A memory line claiming "this repo is auto" is not an input; read the config.

If the effective mode resolves to `auto`, apply the auto-mode doctrine below — do not ask for push confirmation, do not phrase the end-of-task as "your call", just push.

The most common failure mode is assuming `interactive` without walking the chain — saying "not pushed, interactive mode" on a repo whose resolved mode is `auto`. That reads as the agent ignoring the user's configured preference and forces them to repeat it every session.

## Interactive mode

Commit approval ≠ push approval. **Squash approval ≠ push approval. "All done" ≠ push approval. Rebase approval ≠ force-push approval.** Always present the final state and ask "Push?" as a **separate question** after committing, squashing, or rebasing — use `AskUserQuestion`, not an inline question.

- Every publishing action (push, PR create/update, PR merge, PR approve/unapprove, remote branch delete, Slack post) requires a separate explicit confirmation. "Recheck" / "re-review" / "look again" are verify-only instructions — they do **not** authorize re-approval.
- **Force-push (`--force-with-lease`)**: get separate explicit confirmation even if the user already approved the rebase. A rebase and a force-push are two decisions.

## Auto mode (DB-home `mode = auto` via `config_setting set mode auto`, or `T3_MODE=auto`)

The user has opted into end-to-end autonomy. The agent ships complete features without pausing for confirm prompts on the publishing actions listed above. In particular:

- Push the feature branch after local quality gates pass (lint, tests, `makemigrations --dry-run --check`).
- Open the PR, watch the pipeline, then **merge via the §17.4 keystone** (orchestrator `t3 <overlay> ticket clear …` → loop `t3 <overlay> ticket merge <clear_id>`; never raw `gh pr merge`) **when green unless `require_human_approval_to_merge` is `true` for the active overlay**, delete the remote branch.
- Post the overlay-approved Slack messages (review request, release note) as part of the normal flow.

**`require_human_approval_to_merge` is the merge-only carve-out.** Some overlays opt into auto-push but keep auto-merge gated because the upstream enforces a human-review gate (e.g., GitLab Code Review approval rules where CI green is necessary but not sufficient). The setting lives on `UserSettings` (DB-home) and is overridable per-overlay via `t3 <overlay> config_setting set require_human_approval_to_merge true --overlay <name>`. When `true`, the agent pushes and opens the PR/MR without asking but stops before issuing the per-diff CLEAR (`t3 <overlay> ticket clear …`) or running the keystone merge (`t3 <overlay> ticket merge <clear_id>`) — raw `gh pr merge` / `glab mr merge` are mechanically blocked regardless. The user flips it to `false` once they're comfortable trusting CI green alone. Default is `true` (training wheel on). The setting is intentionally orthogonal to `mode`: `mode = "auto"` everywhere is fine while `require_human_approval_to_merge` stays `true` on client/team overlays.

**Mode is per-overlay.** A per-overlay `mode` value (`config_setting set mode … --overlay <name>`) overrides the global `mode` value. A user can run `auto` mode on a personal dogfooding overlay while keeping `interactive` on a client overlay — the active overlay (resolved via `T3_OVERLAY_NAME`) determines which doctrine applies. See `docs/blueprint/configuration.md` § 10.1.1 "Per-Overlay Setting Overrides".

**Quality gates still run — they just don't depend on user confirmation.** The objection auto mode answers is "stop gating on _confirmation_," not "skip quality checks."

**Don't ask after resolving to `auto`.** Once the resolution order resolves to `auto`, asking "should I push?" or "should I open the PR?" reads as ignoring the user's configured preference and forces them to repeat it every session. Just push and open the PR. The only place you still ask is the merge step, and only when `require_human_approval_to_merge` is `true` for the active overlay.
