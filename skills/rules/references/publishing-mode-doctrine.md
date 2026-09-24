# Publishing — PR shape, publishing modes, and repo axes

The full text of the `/t3:rules` sections on PR base branches, fewest PRs, § "Publishing Actions Are Mode-Conditional (Non-Negotiable)" with its always-gated list, and the three repo axes, followed by the mode precedence chain and the per-mode expansion. The core `skills/rules/SKILL.md` carries each rule's trigger and verdict and the always-gated list itself.

## Never Change PR Base Branch or Dependencies (Non-Negotiable)

When a PR targets a non-default branch, that is intentional — it means the PR is part of a dependency chain. **Never** change a PR's target branch, rebase it onto a different base, or remove PR dependencies without explicit user instruction.

- If asked to "merge main" into a branch, merge the specified source — do not change what the branch is based on.
- If a branch is based on another feature branch (not main/master), keep it that way.
- If unsure about the dependency chain, **ask first**.

Destroying PR dependency chains wastes hours of carefully organized work.

## Fewest PRs for Related Work — Splitting Requires Approval (Non-Negotiable)

Ship a piece of **related** work as **one** PR. Do not preemptively carve a single coherent change into a chain of stacked or follow-up PRs. The user's standing policy: teatree ships related work in **as few PRs as possible**, and **splitting related work across multiple PRs needs the user's explicit, up-front approval**. Without that approval, the default is one PR.

- The small-focused-PR habit is a human code-**review** convenience; it does not transfer to agent-driven, self-verified work. When the user is not reviewing PRs, splitting buys nothing and costs more — every extra PR multiplies CI runs, base-branch drift, stacked-rebase overhead, BLUEPRINT churn, and partial-merge states, and each seam is a fresh place for error.
- "Related" is a judgment call: commits that serve **one goal** (one feature, one refactor, one migration — even across several files or several days) belong together. A migration that touches N fields is one PR, not N PRs.
- Genuinely **unrelated** work still gets its own PR — this rule minimises PRs _within_ a coherent change, it does not bundle disjoint concerns.
- When you believe a split is genuinely warranted (e.g. an enormous diff, or a risky change that benefits from landing a safe prerequisite first), **ask the user first** and proceed only on an explicit yes. If you proceed without asking, ship it as one PR.
- Per-commit granularity inside one PR is encouraged — meaningful, self-contained commits on a single branch give you reviewable history without paying the multi-PR cost.

This generalises the `/t3:contribute` "bundle into a single PR by default" rule from retro commits to **all** related work, and gates the stacking option in `/t3:ship` § "One Open PR Per Ticket" behind explicit approval.

## Publishing Actions Are Mode-Conditional (Non-Negotiable)

The DB-home `mode` setting (`t3 <overlay> config_setting set mode <interactive|auto>`, or the `T3_MODE` env var) picks between two doctrines for publishing actions — push, PR create, PR merge, PR approve/unapprove, remote branch deletion, Slack posts, any write that leaves the local machine. The default is `interactive` (security-conservative). `auto` opts into full autonomy.

**Resolve the effective mode before every publishing decision — never assume `interactive`.** The chain is `T3_MODE` → per-overlay `mode` → global `mode` → per-repo memory overrides → `interactive`. The recurring failure is skipping that resolution and saying "not pushed, interactive mode" on a repo the user already opted into `auto`; that reads as ignoring their configured preference. Once it resolves to `auto`, do not ask "should I push?" — push and open the PR.

- **`interactive`:** each publishing action needs its own explicit confirmation. Commit approval ≠ push approval; rebase approval ≠ force-push approval; "recheck" is verify-only and never re-authorizes an approval.
- **`auto`:** ship end to end without confirm prompts — push, open the PR, watch CI, then merge via the §17.4 keystone (`t3 <overlay> ticket clear …` → `t3 <overlay> ticket merge <clear_id>`, never raw `gh pr merge`). The one place you still stop is the merge, and only while `require_human_approval_to_merge` is `true` for the active overlay. Quality gates still run — `auto` drops the confirmation, not the checks.

The resolution order in full, the per-mode expansion, and the `require_human_approval_to_merge` carve-out are in [`skills/rules/references/publishing-mode-doctrine.md`](references/publishing-mode-doctrine.md).

### Always-Gated Actions (Non-Negotiable, both modes)

Some actions remain confirm-gated regardless of mode because they are irreversible or affect shared history:

- **Force-push to default branches** (`main`, `master`, `development`, `release`, or any branch listed in the overlay's `protected_branches`).
- **History rewrites on shared defaults** — rebase, amend, or filter-branch on any branch another agent or human is tracking.
- **Destructive shared-state ops** — `DROP` / `TRUNCATE` on shared databases, deletions in shared directories, `rm -rf` on paths outside the active worktree.
- **External writes the active overlay has NOT authorised** — posting to channels, repos, or services not listed in the overlay's publishing allow-list.
- **`--no-verify` on any git command** is forbidden in both modes. If a hook fails, fix the underlying issue.

This list applies to all repos, all branches, both modes.

## Three Orthogonal Repo Axes — Visibility, Ownership, Collaboration (Non-Negotiable)

A repo's treatment is decided on three INDEPENDENT axes. Conflating them is the recurrence this rule prevents — most often treating a private overlay repo as if it were colleague-facing and holding back from merging the user's own work.

| Axis | Question | Where it lives | Polarity |
|---|---|---|---|
| **Visibility** | public vs private? | `[teatree] private_repos` + `internal_publish_namespaces` → `teatree.hooks.publish_destination` | leak-prevention; fails **OPEN** (unknown → scan-as-public) |
| **Ownership / scope** | owned vs unknown? | `[overlays.<name>.owned_repos]` (forge-host-keyed) → `teatree.core.intake.repo_scope` + `teatree.core.gates.owned_repo_guard` | unknown-repo gate; fails **CLOSED** (unknown → ask) |
| **Collaboration** | self vs colleague? | the MR AUTHOR → `teatree.core.review.review_candidate.author_is_self` | never auto-merge a colleague's MR |

- **Solo-owned repos merge freely.** `souliane/*` and the user's own overlay repos (e.g. `acme-eng/widget-overlay`, `acme-eng/widget-overlay-e2e`) merge exactly like `souliane/teatree`. The only colleague-facing repos are the shared **product** repos of the org (e.g. `acme-product`, `acme-client-workspace`, `acme-shared-config-*`).
- **Private ≠ colleague-facing.** A repo being private is the _visibility_ axis (leak-prevention still applies). It says nothing about ownership — `widget-overlay` is private AND solo-owned, so the agent merges it without colleague gating.
- **Owned ≠ auto-merge.** Ownership gates the _unknown-repo_ decision only. A shared product repo is still in scope (owned by its overlay) yet still needs colleague review — that is the collaboration axis, decided by `author_is_self`, never collapsed into ownership.
- **`owned_repos` is forge-host-keyed** (`{"github.com": ["souliane", …]}`): a `gitlab.com` repo never matches a `github.com` scope.
- **The unknown-repo gate ships INERT — opt-in, default off.** `require_owned_repo_approval` defaults `false`, so no overlay is gated out of the box. Enabling it requires **first declaring the FULL owned host/namespace list** — including every private/customer forge the operator merges on — because the gate fails **CLOSED** on any repo no listed pattern owns: flipping it on with a partial list would hold the operator's own private-forge keystone merges as "unknown". Opt in from the private DB `ConfigSetting` store (the overlay's `owned_repos` with the full host list + `require_owned_repo_approval = true`), where brand/customer strings are allowed and never reach the public repo.
- **A path-only TOML overlay cannot carry its own scope.** An overlay registered with a `path` but no Python `class` is skipped by overlay discovery (`get_all_overlays` returns only instantiable overlays), so it can never opt itself into the gate. Its repos must be declared under an INSTANTIABLE overlay's `owned_repos` (e.g. the always-registered `t3-teatree`).
- **Never-lockout** regardless: a per-call `[scope-push-ok: <reason>]` token, the `unknown_repo_push_gate_enabled` kill-switch, and fail-open on a resolver exception (incl. a failed Django bootstrap in the hook subprocess) all keep the gate from wedging a push.

Pinned by `tests/teatree_core/intake/test_repo_scope.py` (host-symmetric gate), `tests/teatree_core/gates/test_owned_repo_guard.py` (polarity + orthogonality), `tests/teatree_core/review/test_review_candidate.py` § `TestClassificationIsAuthorNotNamespace` (author-not-namespace), and the A/B eval `evals/scenarios/owned_repo_not_colleague.yaml`.

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
