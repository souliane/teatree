---
name: code
description: Writing code with TDD methodology. Use when user says "implement", "write", "add", "code", "feature", "build", or is actively coding a solution.
compatibility: macOS/Linux, any language/framework supported by the project.
requires:
  - workspace
  - architecture-design
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

Optional [obra/superpowers](https://github.com/obra/superpowers) skills provide generic methodology. TeaTree keeps the project-specific workflow locally.

The implementation phase. Follow test-driven development and project conventions.

## Dependencies

- **t3:workspace** (required) — provides dev servers for live reload. **Load `/t3:workspace` now** if not already loaded.
- **Framework/language convention skills** (when backend is in scope) — e.g., Django conventions, Python style guides. TeaTree auto-detects the relevant `ac-*` skill from the repo shape. **If the loader didn't fire**, self-load the appropriate coding skill: `/ac-python` for Python code, `/ac-django` for Django projects. Load these **before writing any code**, not after.

## TDD Discipline

Write the **failing test first**, then the implementation that makes it pass. The test proves the feature works; writing it after implementation risks testing the implementation rather than the behavior. When fixing a bug, the test must reproduce the bug (red) before the fix (green).

**A regression test is only valid if it has been observed to FAIL on the pre-fix code.** A test that asserts a structurally-guaranteed outcome (final state that holds even on the buggy code) passes green without guarding anything — it is vacuous. For a concurrency/state fix this is acute: assert on the *observable contract the bug violates* (e.g. the emitted decision, the surviving row), not a post-condition the code path always produces. Before claiming a regression test guards a fix, temporarily revert the fix (or otherwise re-introduce the defect) and confirm the test goes red; if it stays green, the test does not guard the fix.

**On a connection-level-write-serialized backend, the RED reproduction must model the pre-fix code's *actual* transaction shape — not an `atomic()`-wrapped approximation.** When the production backend serializes writers at the connection level (e.g. the file-backed SQLite prod config's `BEGIN IMMEDIATE`), wrapping the racing writers of a lost-update RED test in `transaction.atomic()` makes the two writers serialize and the second's re-read see the first's commit — the clobber never reproduces and the RED test passes vacuously, "proving" a bug that the harness silently prevented. If the buggy code does the read-modify-write with **no `transaction.atomic()` wrapper** (bare autocommit `save()` from a possibly-stale in-memory read — the unlocked-RMW lost-update class), the RED harness must do exactly that: stale read first, then a bare autocommit write, no `atomic()`. Match the defect's real concurrency primitive (autocommit vs. atomic, where the stale read happens) or the prod backend's serialization will mask the very race the test claims to pin.

**A fail-safe-to-empty primitive must not be consumed by a "claim/grant if empty" caller without an explicit dependency-unavailable check.** When a helper degrades to a neutral value (empty set/dict, `None`, `0`) both when the real answer is genuinely empty *and* when the dependency it needs is unavailable (an unimportable module, a down service), that neutral value is ambiguous. A caller that interprets "empty" as "safe to proceed/claim" will take the unsafe action precisely in the can't-tell case — the opposite of fail-safe. In a safety-critical degraded path (a crash-proof Stop hook, a lock acquisition, an idempotency guard) the consumer must probe the dependency's availability itself and fail *closed* (skip/deny) when it cannot be confirmed, rather than infer safety from the ambiguous empty. Reusing a fail-open prune/read primitive inside a claim path is the canonical way this regresses; the guarding test asserts the degraded path takes the *conservative* branch (no claim, no pump, no grant).

**Run the test before claiming it is correct, and never push a regression/golden-master test you have not run (Non-Negotiable).** A freshly-written test is not "correct", "likely correct", or "done" until you have *run it locally and read its output in this same response*. Pushing a test to CI to find out whether it passes — while reporting it as correct/likely-correct — is the banned "seems correct" claim (`/t3:rules` § "Verification Before Completion"): CI is a slow oracle, not a substitute for running the test yourself, and a test authored against a config you assumed produces the right values routinely fails on first run (the assumed golden was wrong). **Where an authoritative reference exists** — a Tilgungsplan/amortization PDF, a spec's worked example, a vendored golden file — the test is a golden-master that must **assert every value the reference fixes** (every row, every monthly figure), not a sampled subset, and you verify the test's output equals the reference *before* pushing. A narrow run on a migration-heavy repo uses `--no-migrations --reuse-db` so "run it first" costs seconds, not a full migration replay — there is no excuse to skip the run. The order is: write the test → run it locally (`--no-migrations --reuse-db` for a narrow node-id) → confirm its output matches the authoritative reference (every value) → only then commit and push. Reporting "likely correct, pushing to CI" before that run is a FAIL.

Misleading names are bugs — rename the symbol instead of explaining it with a comment.

## Comments Are Code — Minimal, Self-Documenting (Non-Negotiable)

**Names + types ARE the documentation. A long comment is a code smell — refactor instead of explain.** Comment ONLY the non-obvious WHY (a threat-model note, a workaround for an external bug, a counter-intuitive invariant). Never restate WHAT the code already says.

- **Do not restate the code.** `# divide the cents by one hundred` above `return cents / 100`, `# update the rows with the metadata` above `.update(**metadata)` — delete it. The line below already says it.
- **No signature-echo docstrings.** `"""Add the feature flag."""` on `def add_feature_flag(...)` adds nothing — drop it. A docstring earns its place only by carrying a non-obvious why the signature does not.
- **A long comment is a refactor signal, not a license.** When you feel the urge to write a multi-line block explaining a function, the function name / structure is wrong — rename or split it. Multi-line comments are legit when they carry a genuine non-obvious why (and that's rare); they are abuse when they narrate the code. Length is the smell: if it's long, refactor; don't explain.
- **Rationale lives in the commit message, not inline.** Why a change was made, which ticket/MR it relates to, what was tried — all of that goes in the commit body. Never an inline `# per review` / `# consolidated into !NNNN` / `# TODO(W20)` tracker note.

The deterministic backstop is the advisory `comment-density` gate (`teatree.hooks.privacy_diff_comment_density` → the pre-push hook, the CI job, and `t3 tool comment-density`). It is content-aware: it flags a comment whose words merely restate the next code line, and a docstring opening that merely echoes the signature — a single such line is enough. Genuine non-obvious-why comments and justified multi-line blocks are NOT flagged. The gate is advisory (it never blocks); the discipline is yours to keep — the gate is the safety net, not the author.

## Workflow

### 0b. Worktree-First — Never Edit a Main Clone (Non-Negotiable)

The path you were handed may be the **canonical clone** (the repo's primary working copy that tracks the default branch). Editing it directly corrupts the shared working tree, blocks other sessions, and ships unreviewed commits onto the wrong branch. Before the **first** edit of any coding task, do X — never Y:

1. **Do** create a dedicated worktree on a fresh branch and `cd` into it — every edit, test run, and commit happens **inside the worktree**, never in the clone you were pointed at.
2. **Never** run an `Edit`/`Write` against a file under the canonical clone path (e.g. `.../<repo>/<repo>/...`) until a worktree exists.

The overlay owns worktree creation (it wires ports, DB, env, and the branch name). Use it when a ticket context exists:

```bash
t3 <overlay> workspace ticket <ticket-url-or-id>   # creates the worktree + branch, provisions it
cd <printed-worktree-path>                          # all edits happen here, not in the main clone
```

For a quick ad-hoc fix with no overlay/ticket (a typo, a one-line doc change), create the worktree by hand from the default branch first — the edit comes **after** the worktree exists, never before:

```bash
git fetch origin main -q
git worktree add -b <short-branch> ../<repo>-wt-<slug> origin/main
cd ../<repo>-wt-<slug>
# only now: Edit / Write the file
```

A session-less branch still gets a session at ship time (see [`../ship/SKILL.md`](../ship/SKILL.md) § 4b) — but the worktree comes first regardless.

### 0c. Overlay-Repo Code — Load the Overlay Playbook Skill First (Non-Negotiable)

When the repo you are about to code in is **managed by an overlay** (a product/service repo the overlay's workspace wiring owns, not teatree core itself), the overlay's playbook skill carries this repo's worktree/run/test/lint commands, tenant rules, and conventions. The generic dev skill is **not** enough on its own. Per `/t3:rules` § "Invoke Skills Before ANY Response", do X — never Y:

1. **Do** self-load the overlay's playbook skill **before touching any code** — unconditionally, before asking for the ticket URL, before reading any diff. If the `UserPromptSubmit` loader did not fire, load it yourself: `/t3-<overlay>` (the overlay's named playbook skill).
2. **Never** issue the first `Edit`/`Write` against an overlay-repo source file until that skill is loaded alongside this one.

```text
# overlay repo `<overlay>-product` detected, loader did not fire → load the playbook FIRST
Skill: t3-<overlay>      # overlay playbook (worktree/run/test wiring, tenant rules)
# then proceed with the dev skill already loaded — only now edit source
```

Loading is unconditional and comes **before** any clarifying question — do not wait to be told the skill name; derive it from the active overlay.

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

**"Just fix it fast" is NOT a license to skip the plan — your single next action is the plan, never an edit/commit/push (do X, never Y).** Under urgency, especially across **multiple unrelated tickets** ("both tickets are tiny, just fix them both fast and push"), the drift is to start editing/committing/pushing with no plan. The plan-first step holds precisely when the user is in a hurry — a do-it-now directive changes nothing about ordering: plan first, then code. So when you are told to fix N tickets fast, your single next action is one of: **record the plan as tasks** (`TaskCreate` naming the tickets/scope), or **surface the two-ticket split** as a structured `AskUserQuestion` (which ticket first / keep them separate). It is **never** an `Edit`/`Write` on a ticket's source, and never `git commit` / `git push` / `gh pr create` / `gh pr merge`, before any plan is presented.

```python
# Two unrelated tickets, user says "fix both fast and push". do X — plan FIRST (record one planning task per ticket, or ask the split):
TaskCreate(subject="Plan TODO-4 (guarantee-matrix tweak)", description="Plan TODO-4 scope + files; keep separate from TODO-6 — do not bundle.")
# never Y — do NOT start editing/committing/pushing across the tickets with no plan because the user is in a hurry:
# Edit(file_path="...guarantee_matrix...", ...)   # FORBIDDEN before a plan
# Bash(command="git commit -am ... && git push")  # FORBIDDEN before a plan
```

Two unrelated tickets are never bundled into one unplanned edit-spree; each gets its own planned, worktree-isolated change. Read-only investigation (`git fetch`, reading files) is exempt — it is part of planning.

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

- **Full type annotations on every new function — do X, never Y.** Every new function and method carries modern type annotations on **all** parameters AND the return: `def slugify(title: str) -> str:`, `def load(path: Path) -> dict[str, int]:` — **never** a bare `def slugify(title):` or a missing `-> ...`. Use the modern lowercase generics (`list[str]`, `dict[str, int]`, `str | None`), not `typing.List` / `Optional`. `Any` only when interfacing with genuinely untyped third-party code. A pure helper is the easy case, so it is the strict case: it ships fully annotated.
- Language/framework conventions from the project's convention skills (when loaded).
- Repository-specific patterns take precedence over generic guidance.
- Feature flag rules for new features (see [`references/multi-tenant-development.md`](references/multi-tenant-development.md)).
- Surface every open question (solved or not) and every non-explicit assumption in both the commit message body and the PR description under an `Open questions & assumptions` section — see [`../ship/SKILL.md`](../ship/SKILL.md) § 5 "Open Questions & Assumptions" (canonical).
- **Verify a config-resolution chain against the specific setting's registration before documenting it — never copy the generic resolver's docstring.** A setting's effective-value chain depends on which override registries it is registered in (e.g. an env layer exists only if the setting is in the env-override registry; a per-overlay layer only if it is in the overridable registry). Documenting "env → per-overlay → global → default" by analogy with a sibling setting, without grepping the registries, produces a chain that names layers that do not exist for that setting. Before writing any "resolved through X → Y → Z" prose in a docstring/skill/BLUEPRINT, grep the registries for the exact setting name and describe only the layers it is actually registered in.

### 3b. Tooling Decisions

- **Prefer existing battle-tested packages** over custom scripts. Only write custom code when no viable alternative exists. Custom scripts introduce untested code that often fails in CI.
- **When migrating state** (databases, APIs, config), fetch current data from the live API rather than trusting local files or config. Local files may be stale.
- **When porting old code**, don't blindly copy the approach. Read the actual data format (JSON files, API responses, configs) and choose the robust technique. Old scripts often used quick hacks (regex on JSON, string splitting) that break on edge cases — use proper parsing in the new code.

### 4. Update Task Tracking

When tasks exist (via the agent's task tracking tools), mark each task `completed` **immediately after finishing it** — before moving to the next task. Never batch-update at the end. Never claim "all done" while the task list is stale.

### 5. E2E Tests

The acceptance scenarios are **planned up front** — the `/plan` phase emits an `E2E test plan / Acceptance scenarios` section for any UI-visible ticket (see `agents/planner.md`). Treat those scenarios the way the unit-level TDD discipline treats a failing test: they are the behaviour-level red→green contract. Where the plan supplies them, write the failing browser-level test from the planned scenario **first** and implement to make it pass — don't leave E2E as an afterthought tacked on at the end. When the plan has no E2E section (a non-UI ticket), there's no behaviour-level contract to satisfy.

Author the failing browser-level spec from the planned scenario first (§ 5 above); switch to `/t3:e2e` to run it red→green and post evidence via the visual QA gate once the implementation it gates is committed.

**Mandatory-E2E gate — attest after posting evidence.** For a change that could impact what is displayed to the customer, the E2E run is a mandatory FSM step before `pr create` / CLEAR. After a green local run AND posting its evidence on the ticket, record the attestation so the gate passes:

```bash
t3 <overlay> lifecycle record-e2e-run <ticket-id> \
  --spec <e2e/spec/path> --result green \
  --head-sha <full-40-char-sha> --posted-url <evidence-url>
```

A green run recorded **without** `--posted-url` does NOT satisfy the gate — the posted evidence URL is the part that clears it. Do not invent an `e2e attest` subcommand; the command is `lifecycle record-e2e-run`.

**The gate only fires for display-impacting changes — never force a spurious bypass on a change that can't reach the customer.** When the diff touches **only** a test file (`tests/...`, `*/test_*.py`), a fixture, a doc, or other code that cannot change what the customer sees — no serializer, view, template, or frontend — the E2E gate does **not** apply, so there is nothing to attest and nothing to bypass. Do X, never Y:

1. **Do** delegate PR creation to the overlay — it evaluates the gate against the actual diff and lets a non-display change through cleanly:

   ```bash
   t3 <overlay> pr create <ticket-id>
   ```

2. **Never** run `t3 <overlay> ticket e2e-bypass ...` (or `--skip-e2e`-style flags) for a test-only / non-display change. A bypass is a recorded exception that exists **only** for a genuinely display-impacting change you cannot run E2E for — inventing one where the gate never fires fabricates a waiver for a guard that was never triggered.

`t3 <overlay> pr create` is the mandatory PR path on **every** ticket (display-impacting or not) — raw `gh pr create` / `glab mr create` is forbidden whenever the overlay exposes `pr create`, because only the overlay command runs the shipping gate, the E2E/visual-QA evaluation, the title/description validator, and the ticket-URL injection (see [`../ship/SKILL.md`](../ship/SKILL.md) § "pr create is mandatory").

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

**String-based `mock.patch` targets are the one surface the textual sweep cannot see.** A `patch("old.dotted.path")` or `patch.object(module_alias, "attr")` left after a move points at a dead name and the test passes *vacuously* — a stale import is a hard error, a stale patch string is silent. The deterministic backstop is `tests/quality/test_patch_targets_resolve.py` (logic in `teatree.quality.patch_targets`): it resolves every constant patch string target in `tests/` + `src/` against the live module tree via `importlib` + `getattr` (mirroring `unittest.mock`) and turns red on any unresolved one. Genuinely dynamic targets are exempt via `create=True` or a `# patch-target: dynamic` line pragma. So the rename is not done while that gate is red.

### 5d. Doc/Prose-Invariant Tests Must Scan All Occurrences, Never the First

When a test asserts something about prose (a BLUEPRINT/skill/docs invariant — "the epic-completion statement exists", "issue X is documented as subsumed near a 'board' qualifier"), **assert that *some* occurrence of the anchor token carries the required nearby context — never key the assertion on the first occurrence**. Issue references, workstream citations, and architecture terms legitimately recur across a doc section (the same `#50`/`#789`/`roster` token appears in unrelated per-workstream paragraphs), so `text.index(token)` / first-match-window assertions produce false REDs against an unrelated mention while the real statement is correct. Use a scan-all-windows helper (`while find(token, start): check ±radius; start = i+1`) that returns true if any window satisfies the predicate. The same recurrence hazard the § 5c mass-rename sweep guards against on the *production* side applies to the *test* side: a single naive lookup is insufficient whenever the token is non-unique. Prove the doc-invariant test is anti-vacuous the same way as any regression test — revert the doc change, confirm RED, restore, confirm GREEN (a prose guard that passes against the pre-change doc guards nothing).

### 6. Quality Gates During Development

- **When adding a prek hook, check for CI duplication.** After adding a new hook to `.pre-commit-config.yaml`, grep the repo's `.gitlab-ci.yml` (or equivalent) for any job that runs the same script directly. If found, remove the standalone CI job — having the check run twice wastes CI time and creates maintenance confusion. One source of truth: prek.
- Run linting after each significant change.
- Run type checking if the project uses it.
- Run the relevant test suite frequently — don't batch test runs.
- **Regression suite is GREEN locally before any push (mandatory).** A narrow node run proves your new test; the regression suite proves you broke nothing else. The discipline is do-X-not-Y: do run the suite and confirm it passes in this same response *before* `pr create` / `git push`; do not push to let CI tell you whether you regressed something.

  ```bash
  uv run pytest --no-cov -x -q          # teatree core: full regression suite
  # overlay repo: use the overlay's wired runner from its playbook skill, e.g.
  t3 <overlay> test run                 # runs the repo's regression suite under its config
  ```

- **Run the language convention skill's review checklist** (if loaded) before declaring implementation complete.
- **100% test coverage is part of the implementation (Non-Negotiable).** New code ships with tests in the same commit. Never lower coverage thresholds, add files to coverage omit lists, or exclude code from coverage measurement without **explicit user approval**. If you can't reach 100% coverage, the implementation scope is too large — break it into smaller pieces.

  The test file **mirrors the production module's path** under `tests/` with a `test_` prefix (a helper at `src/<pkg>/util/money.py` gets `tests/<pkg>/util/test_money.py`). When you add a new production file, create its test file in the same change — do X, never Y: **do** write the mirroring `tests/.../test_*.py` now; **never** declare the helper done with no test file on disk.

  ```bash
  # new production file: src/teatree/util/money.py  →  create its mirror test now
  touch tests/teatree/util/test_money.py
  # write the failing test (RED), then run only that node so feedback is seconds:
  uv run pytest tests/teatree/util/test_money.py -q --no-migrations --reuse-db
  ```

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
