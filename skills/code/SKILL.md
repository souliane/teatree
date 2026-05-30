---
name: code
description: Writing code with TDD methodology. Use when user says "implement", "write", "add", "code", "feature", "build", or is actively coding a solution.
compatibility: macOS/Linux, any language/framework supported by the project.
requires:
  - workspace
  - architecture-design
companions:
  - test-driven-development
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Writing Code (TDD)

## Delegation

This skill delegates the generic implementation doctrine to:

- `test-driven-development` — red/green/refactor discipline and failing-test-first rules
- `verification-before-completion` — proof before any completion claim

Optional [obra/superpowers](https://github.com/obra/superpowers) companions provide generic methodology. TeaTree keeps the project-specific workflow locally.

The implementation phase. Follow test-driven development and project conventions.

## Dependencies

- **t3:workspace** (required) — provides dev servers for live reload. **Load `/t3:workspace` now** if not already loaded.
- **Framework/language convention skills** (when backend is in scope) — e.g., Django conventions, Python style guides. TeaTree auto-detects the relevant `ac-*` skill from the repo shape. **If the loader didn't fire**, self-load the appropriate coding skill: `/ac-python` for Python code, `/ac-django` for Django projects. Load these **before writing any code**, not after.

## TDD Discipline

Write the **failing test first**, then the implementation that makes it pass. The test proves the feature works; writing it after implementation risks testing the implementation rather than the behavior. When fixing a bug, the test must reproduce the bug (red) before the fix (green).

**A regression test is only valid if it has been observed to FAIL on the pre-fix code.** A test that asserts a structurally-guaranteed outcome (final state that holds even on the buggy code) passes green without guarding anything — it is vacuous. For a concurrency/state fix this is acute: assert on the *observable contract the bug violates* (e.g. the emitted decision, the surviving row), not a post-condition the code path always produces. Before claiming a regression test guards a fix, temporarily revert the fix (or otherwise re-introduce the defect) and confirm the test goes red; if it stays green, the test does not guard the fix.

**On a connection-level-write-serialized backend, the RED reproduction must model the pre-fix code's *actual* transaction shape — not an `atomic()`-wrapped approximation.** When the production backend serializes writers at the connection level (e.g. the file-backed SQLite prod config's `BEGIN IMMEDIATE`), wrapping the racing writers of a lost-update RED test in `transaction.atomic()` makes the two writers serialize and the second's re-read see the first's commit — the clobber never reproduces and the RED test passes vacuously, "proving" a bug that the harness silently prevented. If the buggy code does the read-modify-write with **no `transaction.atomic()` wrapper** (bare autocommit `save()` from a possibly-stale in-memory read — the unlocked-RMW lost-update class), the RED harness must do exactly that: stale read first, then a bare autocommit write, no `atomic()`. Match the defect's real concurrency primitive (autocommit vs. atomic, where the stale read happens) or the prod backend's serialization will mask the very race the test claims to pin.

**A fail-safe-to-empty primitive must not be consumed by a "claim/grant if empty" caller without an explicit dependency-unavailable check.** When a helper degrades to a neutral value (empty set/dict, `None`, `0`) both when the real answer is genuinely empty *and* when the dependency it needs is unavailable (an unimportable module, a down service), that neutral value is ambiguous. A caller that interprets "empty" as "safe to proceed/claim" will take the unsafe action precisely in the can't-tell case — the opposite of fail-safe. In a safety-critical degraded path (a crash-proof Stop hook, a lock acquisition, an idempotency guard) the consumer must probe the dependency's availability itself and fail *closed* (skip/deny) when it cannot be confirmed, rather than infer safety from the ambiguous empty. Reusing a fail-open prune/read primitive inside a claim path is the canonical way this regresses; the guarding test asserts the degraded path takes the *conservative* branch (no claim, no pump, no grant).

Misleading names are bugs — rename the symbol instead of explaining it with a comment.

## Workflow

### 0a. Scoping Gate — Warn When Skipped

Features benefit from a scoping pass (intent discovery, acceptance-criteria framing) BEFORE coding. The teatree session FSM carries a `scoping` phase (`Ticket.State.SCOPED`) for exactly this — feature tickets are expected to transition `not_started → scoped → started` before coding starts.

**Before writing any code, check:**

1. Did this ticket visit the `scoping` phase? Inspect `ticket.state` and the ticket's visited phases.
2. If the ticket is a **feature** (new capability, ambiguous scope, architectural choice to make) and scoping was skipped, **warn once** — "scoping phase was skipped; this is a feature, want to run `/t3:ticket` or brainstorm first?" — then proceed. Do NOT hard-block.
3. Bug fixes, docs, and small tactical changes don't need scoping — skip this gate.

The goal is to surface the missed step so the user can redirect early, not to add friction to every coding session.

### 0. Ticket-Required Overlay Check

When the active overlay has `require_ticket = True` in its configuration, a tracked issue must exist before writing any code.

- **Detection:** check `overlay.config.require_ticket`. Overlays that dogfood their own workflow (e.g., the teatree overlay) enable this flag.
- **If no ticket context exists:** ask "Which ticket is this for?" or offer to create one. Do not proceed without a ticket reference.
- **Use the overlay** for worktree creation and lifecycle management.
- **Exception:** changes from `/t3:retro` are exempt. Retro findings are small tactical fixes committed directly on the current branch by design.

### 1. Plan First

**Always make a plan before writing code.** Never jump straight to coding.

- **Verify the codebase matches expectations.** Run `git fetch origin main` and check: (1) are any ticket items already implemented on main? (2) does the current architecture match what the ticket assumes? Read the actual files before assuming the ticket description is current. Tickets derived from external analysis (source code leaks, competitor research, blog posts) are especially prone to stale assumptions.
- **Check for prior work:** Search git history (`git log --grep`, PR list) for previous attempts at this task. Existing research, rejected approaches, and partial implementations save hours.
- **Stale-OPEN-issue gate (autonomous backlog sweeps):** An issue tracker's "open" state is not authoritative — fixes routinely merge without the issue auto-closing (a `Relates-to` partial PR, a closing keyword that didn't fire, an umbrella-protection convention). Before implementing any issue picked from a backlog sweep, prove it is genuinely unfixed: (1) `git log origin/main --oneline --grep="(#<n>)"` — a merged commit referencing it is a strong stale signal; (2) read the issue's cited `file:line` **on `origin/main`** (`git show origin/main:<path>`) and confirm the defect is actually still present. Only proceed if both say unfixed. Skipping this wastes a full provision+investigate cycle per stale issue (observed: several consecutive sweep picks were already-merged-but-unclosed). If already fixed, do not re-implement — report it as stale (closing the issue is a coordinator/user action, not loop self-work) and move to the next candidate.
- Identify scope: which files, modules, and repos are affected.
- Review existing patterns in the codebase before writing new code.
- If the task matches a playbook, follow playbook-specific patterns.
- **Feature flag check:** Follow the decision gate in [`references/multi-tenant-development.md`](references/multi-tenant-development.md). Detect the target tenant using the priority chain from `t3:ticket § 6. Detect Variant/Tenant`.

**Scaling the plan to the task:**

- **Simple/clear tasks** (single file, obvious change): State the plan in a short bullet list, then start implementing immediately. No need for plan mode or user confirmation.
- **Complex/ambiguous tasks** (multi-file, architectural decisions, unclear scope): Use the agent's plan mode (if available) to block edits while planning. Explore the codebase, write the plan, present it for user approval. Only start coding after approval.
- **Config/discovery with multiple fallback sources** (settings resolution, env detection, overlay discovery): Map ALL user workflows in a table (who, how they install, what they need) BEFORE coding. One clean implementation beats 6 iterative patches.
- **Removing or replacing a CLI parameter**: Ask the user what the replacement API should look like BEFORE writing code. Don't assume auto-detect-only — the user may want a human-readable argument (e.g., `--path` instead of a DB ID). Design the API first, then update source, then update tests. Running tests before committing is mandatory — don't rely on pre-commit hooks to catch failures.
- **Extracting overlay code to core** (generalization, refactoring): Write the BLUEPRINT spec first, then have the user review it before coding. Existing overlay code evolved organically — extracting it as-is copies its shortcuts. Design the clean-slate API from the spec, not from the existing implementation.
- **Adding a file/dir under a path an existing scanner or invariant owns** (a recursive health check, a "single canonical X" guard, a cleanup walker): grep for every walker of that directory BEFORE choosing the location. A new managed artifact nested inside a directory another module treats as exclusively-canonical will be misclassified by that module. Prefer relocating the artifact OUTSIDE the guarded root (one structural fix, no consumer touched) over teaching N walkers to skip a namespace (N stale-reference risks). Add a regression test that the owning invariant stays clean once the new artifact exists.

- **UI layout / multi-column structures**: Ask for the full desired layout (column order, which columns to show/hide) BEFORE writing any template code. Rewriting headers and cell blocks for repeated column-order changes wastes time.

**How to decide:** If you would normally ask the user "is this approach okay?" before coding, that's a complex task — use plan mode.

### 2. TDD Cycle

```text
Write failing test → Implement → Green → Refactor
```

- **Red:** write a test that captures the expected behavior. Run it — must fail.
- **Green:** implement the minimum code to make the test pass.
- **Refactor:** clean up without changing behavior. Tests must stay green.

### 3. Follow Conventions

- Language/framework conventions from the project's convention skills (when loaded).
- Repository-specific patterns take precedence over generic guidance.
- Feature flag rules for new features (see [`references/multi-tenant-development.md`](references/multi-tenant-development.md)).
- **Verify a config-resolution chain against the specific setting's registration before documenting it — never copy the generic resolver's docstring.** A setting's effective-value chain depends on which override registries it is registered in (e.g. an env layer exists only if the setting is in the env-override registry; a per-overlay layer only if it is in the overridable registry). Documenting "env → per-overlay → global → default" by analogy with a sibling setting, without grepping the registries, produces a chain that names layers that do not exist for that setting. Before writing any "resolved through X → Y → Z" prose in a docstring/skill/BLUEPRINT, grep the registries for the exact setting name and describe only the layers it is actually registered in.

### 3b. Tooling Decisions

- **Prefer existing battle-tested packages** over custom scripts. Only write custom code when no viable alternative exists. Custom scripts introduce untested code that often fails in CI.
- **When migrating state** (databases, APIs, config), fetch current data from the live API rather than trusting local files or config. Local files may be stale.
- **When porting old code**, don't blindly copy the approach. Read the actual data format (JSON files, API responses, configs) and choose the robust technique. Old scripts often used quick hacks (regex on JSON, string splitting) that break on edge cases — use proper parsing in the new code.

### 4. Update Task Tracking

When tasks exist (via the agent's task tracking tools), mark each task `completed` **immediately after finishing it** — before moving to the next task. Never batch-update at the end. Never claim "all done" while the task list is stale.

### 5. E2E Tests

After TDD and commit, frontend changes that affect UI behavior need E2E tests as a follow-up step. Switch to `/t3:e2e` for writing Playwright tests, posting evidence, and the visual QA gate.

### 5b. When to Switch Skills

- Stay in `t3:code` for TDD, implementation-time tests, and feature-building.
- Switch to `/t3:e2e` for E2E test writing, evidence posting, and visual QA.
- Switch to `/t3:test` for broader verification, CI failure analysis, or test-plan writing.

### 5c. Mass-Rename / Cross-Cutting Refactor Verification (Non-Negotiable)

When the task is a rename, type-renaming, key-renaming, or any refactor that should remove **every occurrence** of an old name across the repo, the agent **must not declare "done" on the strength of a single grep iteration**. #545's MR→PR rename produced four false-completion claims in a row (test files missed, single-quoted `'mrs'` missed, error message strings missed, `_infer_state_from_mrs` class name missed) because each pass was driven by a narrow grep that didn't cover all the surface forms.

**Before claiming a rename is finished, run an exhaustive sweep that covers every surface form the old name can take:**

1. **Plain occurrences across all file types**, not just `.py`:

   ```bash
   rg -n --hidden --no-ignore -g '!{.git,node_modules,.venv,*.pyc,*.lock}' '<OLD_NAME>'
   ```

   Run from the repo root, not from a sub-directory — sub-directory greps miss `tests/`, `docs/`, `skills/`, `agents/`, top-level `BLUEPRINT.md`/`README.md`, and CI configs.

2. **Quote-variant fan-out** when the name appears in dict keys, error strings, or fixture data:

   ```bash
   rg -n "['\"]<OLD_NAME>['\"]"     # 'mrs' AND "mrs"
   rg -n "\.<OLD_NAME>\b"           # attribute access (.mrs)
   rg -n "\[['\"]<OLD_NAME>['\"]\]" # subscript access (['mrs'])
   ```

3. **Compound and CamelCase forms** — the old name may be embedded:

   ```bash
   # If renaming MergeRequest → PullRequest, also catch MR/MREntry/_check_mr/list_open_mrs/MergeRequestSpec
   rg -ni '\b(<OLD_SHORT>|<OLD_LONG>|<OLD_VARIANT_1>|<OLD_VARIANT_2>)\b'
   ```

   Build the variant list explicitly at the start of the rename — don't discover them mid-flight one at a time.

4. **Migration data and JSON keys**: when the old name lives in `Ticket.extra`, fixture JSON, OpenAPI specs, or other serialised data, the rename needs a data migration **and** a grep of every fixture/test JSON for the old key.

5. **Sibling repos in `$T3_WORKSPACE_DIR`**: a rename of a public symbol that crosses a service boundary (a Pydantic model, an API field name, a wire format, a published Protocol) must be greped in every consumer repo too. Don't ship the producer's rename without verifying the consumers.

**The verification gate:** rerun the full sweep with `rg --count` and confirm **zero hits** for every surface form before marking the task complete. A non-zero count means the rename is not done — paste the remaining hits into the response and keep going. "I think I got them all" is not a verification result; `rg --count` is.

**This rule complements `verification-before-completion`** (no completion claim without fresh evidence in the same response). The mass-rename case is the variant where a single command's output isn't enough — you need *zero hits across N variant queries* as the receipt.

### 5d. Doc/Prose-Invariant Tests Must Scan All Occurrences, Never the First

When a test asserts something about prose (a BLUEPRINT/skill/docs invariant — "the epic-completion statement exists", "issue X is documented as subsumed near a 'board' qualifier"), **assert that *some* occurrence of the anchor token carries the required nearby context — never key the assertion on the first occurrence**. Issue references, workstream citations, and architecture terms legitimately recur across a doc section (the same `#50`/`#789`/`roster` token appears in unrelated per-workstream paragraphs), so `text.index(token)` / first-match-window assertions produce false REDs against an unrelated mention while the real statement is correct. Use a scan-all-windows helper (`while find(token, start): check ±radius; start = i+1`) that returns true if any window satisfies the predicate. The same recurrence hazard the § 5c mass-rename sweep guards against on the *production* side applies to the *test* side: a single naive lookup is insufficient whenever the token is non-unique. Prove the doc-invariant test is anti-vacuous the same way as any regression test — revert the doc change, confirm RED, restore, confirm GREEN (a prose guard that passes against the pre-change doc guards nothing).

### 6. Quality Gates During Development

- **When adding a prek hook, check for CI duplication.** After adding a new hook to `.pre-commit-config.yaml`, grep the repo's `.gitlab-ci.yml` (or equivalent) for any job that runs the same script directly. If found, remove the standalone CI job — having the check run twice wastes CI time and creates maintenance confusion. One source of truth: prek.
- Run linting after each significant change.
- Run type checking if the project uses it.
- Run the relevant test suite frequently — don't batch test runs.
- **Run the language convention skill's review checklist** (if loaded) before declaring implementation complete.
- **100% test coverage is part of the implementation (Non-Negotiable).** New code ships with tests in the same commit. Never lower coverage thresholds, add files to coverage omit lists, or exclude code from coverage measurement without **explicit user approval**. If you can't reach 100% coverage, the implementation scope is too large — break it into smaller pieces.

- **Don't create an uncoverable/unreachable defensive guard — restructure so the type is precise.** A "shared core returning `T | None` + a thin wrapper that re-asserts non-`None`" shape forces an unreachable branch: the wrapper's `if x is None: raise` (or `assert x is not None`) can never execute when the core's contract guarantees non-`None` for that call, so it is both uncoverable (the 100%-new-line rule can't be met without breaking the contract) and a lint violation (`assert` is banned in `src/`; `# noqa` is not an option). The fix is at the design level, not a suppression: split into **two purpose-typed methods** — one that always produces and returns `T` (the create/attestation path), one that returns `T | None` (the read-only path) — sharing the *policy/logic*, not a `T | None` return. Each call site then gets a precisely-typed value with no narrowing, no defensive guard, no `assert`, and full coverage falls out naturally. When you find yourself adding an "unreachable / defensive, should never happen" guard purely to satisfy the type checker, that is the signal the return type is too loose — tighten it by splitting, don't guard it.

## Agent Rules

### Delegating Code to Sub-Agents

When launching parallel agents to write code (especially tests), the dispatch prompt MUST open with this verbatim block — it is not optional and not a "remember to add it" note. Skill prose does not propagate into a spawned agent's context, so the near-zero-comments rule is lost unless it is inline in the prompt itself:

```text
NEAR-ZERO COMMENTS: names + types are the documentation. Do NOT add comments that restate the code. NO comments referencing MRs/tickets/workstreams/Slack threads. Rationale belongs in the commit message, never inline.
```

Then include these requirements in every prompt:

- **Run `uv run ruff check <files>` and fix all violations** before declaring done
- **Run `uv run ruff format <files>`** to ensure formatting matches the project
- **Check type annotations** — if the project uses a type checker (ty, mypy), the code must pass
- **Never add `# noqa` without justification** — prefer fixing the issue

Sub-agents don't inherit project context. Include the specific linting rules and pre-commit expectations in their prompts.

**After a sub-agent completes, re-read any files it modified before continuing.** Your file state cache is stale — the sub-agent's changes are invisible until you re-read. Writing to a stale-cached file will overwrite the sub-agent's work.

### Systematic Debugging Protocol

If implementation breaks, switch to `t3:debug` and follow `systematic-debugging` before attempting speculative fixes.

### Fixing Existing Tests

When fixing a test you didn't write, apply **minimum viable changes**:

- **Only change the broken assertion** — don't restructure the test class, test framework, or data setup.
- **Never remove existing comments** — they document the original author's intent.
- **Never change test infrastructure** (e.g., `pytest.mark.django_db` → `TestCase`) unless explicitly asked.
- **Never add cleanup/delete calls** for data from migrations — make assertions robust instead (e.g., `sorted()` + subset `<=` checks).
- If the fix requires more than changing the assertion, ask the user first.

### Receiving Review Feedback

See `review/SKILL.md § Receiving Code Review` for the full policy.

### Emit Retro Signal — Do NOT Self-Retro (#837)

Retro is **orchestrator-only**. As a sub-agent implementing a ticket, do **not** run `/t3:retro` as a per-ticket synthesis/judgment step at the end of the work. Instead, as a lesson surfaces during implementation (a repeated mistake, a missing guardrail, a stale doc), **emit it as structured signal into durable state** — task metadata or a `/tmp/t3-snapshot-*.md` snapshot — and keep going. The orchestrator later synthesises across the whole session and biases the output to the smallest enforcement artifact (a gate, test, or hook), not a prose rule. The durability discipline (snapshots, durable task state) is load-bearing and unchanged — it is exactly the channel the orchestrator's synthesis reads from.
