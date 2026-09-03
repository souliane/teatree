---
name: review
description: Code review — self-review before finalization, giving review, receiving review feedback. Use when user says "review", "check the code", "feedback", "review comments", "quality check", or is in a review cycle.
compatibility: macOS/Linux, git, testing tools for verification.
requires:
  - workspace
  - platforms
  - code
  - requesting-code-review
metadata:
  version: 0.0.1
---

# Code Review

## Delegation

This skill delegates the generic review doctrine to:

- `requesting-code-review` — when to request an independent review pass
- `verification-before-completion` — proof before any “review-ready” claim

Optional [obra/superpowers](https://github.com/obra/superpowers) skills provide generic methodology. TeaTree keeps the project-specific workflow locally.

Both self-review and external review cycles.

## Dependencies

- **workspace** (required) — provides environment context. **Load `/t3:workspace` now** if not already loaded.
- **Framework/language convention skills** (when reviewing backend code) — e.g., Django conventions, Python style guides. TeaTree auto-detects the relevant `ac-*` skill from the repo shape. **If the loader didn't fire**, self-load the appropriate coding skill: `/ac-python` for Python code, `/ac-django` for Django projects.
- **Overlay review skill set** (when reviewing an overlay repo) — the active overlay declares its full reviewer skill set via `overlay.config.get_review_companion_skills()`, which returns `[pr_review_companion, *companion_skills]`: the overlay's review-quality bar plus its standing companion skills (the overlay workspace playbook skill and the project dev skills). When the repo under review is an overlay repo, **derive that set and self-load every skill in it immediately — before asking for the MR URL, before fetching ticket context, before reading any diff**. Skill loading is unconditional and comes before clarifying questions; do not wait to be told the names.

  **Do this — never skip it (imperative, the prose above is the WHY):** reviewing ANY overlay repo, the FIRST actions — before reading the diff, fetching ticket context, or asking for an MR URL — are to derive the declared set (`get_review_companion_skills()` = `[pr_review_companion, *companion_skills]`) and load every skill in it via the `Skill` tool, in order — never proceed to the diff with only the generic `/t3:review`:

  1. `/t3-<overlay>` — the overlay's own workspace/review-quality skill (the `pr_review_companion`).
  2. `/t3:review` — this skill (the generic review doctrine).
  3. the overlay's dev skill(s) — e.g. the backend / frontend skill the overlay declares as companion skills.

  Each is a separate `Skill`-tool call; load all of them before the first `t3 review run` / diff read. A review run with only the generic review skill loaded — diving straight into the diff without the overlay's declared set — is a **null review** and does not satisfy the gate. Derive the names by **convention, not runtime introspection**: an overlay repo `<name>-product` (more generally `<name>-*`) is served by the skill `/t3-<name>`, which together with this `/t3:review` and the overlay's dev skill(s) IS the declared set — call the `Skill` tool on each directly, as your first action. Do **not** shell out to a `t3 …` command or grep the source to read `get_review_companion_skills()` at runtime: that name points at WHERE the contract lives in code, it is not a CLI to run, and applying the stable naming convention IS the derivation. Do not wait for the prompt to enumerate the names.

## Workflows

### North-Star Rubric — Seven Quality Attributes

Everything you write and everything you review aims at seven attributes. Treat them as the lens for both self-review and giving review:

- **Clean** — readable, no dead code or duplication, names that say what they hold, and comments that earn their line. An ADDED comment is one line carrying a non-obvious *why*; a multi-line block that narrates what the next lines already say is a real finding (the fix is a rename or a split, not a shorter paragraph), not a style preference. Judge only what the diff ADDS — pre-existing comments are not this review's business. See [`../code/SKILL.md`](../code/SKILL.md) § "Comments Are Code".
- **Robust** — survives the real failure case, not only the favorable one; edge cases handled, inputs validated.
- **Maintainable** — the next reader can change it safely; structure documents itself.
- **Coherent** — fits the surrounding patterns and stays consistent across the whole changeset. Coherence includes **cross-repo coherence** (a referenced artifact — a skill name, a CLI command, a sibling-repo path — must actually exist where it's referenced) and **wired-and-exercised** (a mechanism must actually fire — a hook that's defined but never invoked, or a gate that's declared but never reached, is incoherent even if it reads correctly).
- **Reliable** — does what it claims under repeated and concurrent use; no flaky or order-dependent behavior.
- **Proactive** — sweeps the class, not just the instance; when a fix reveals a broader pattern, address the pattern rather than the single symptom.
- **Scoped** — carries everything the ask requires, and no behaviour it did not. **Under-delivery** is work the author already understands, parked as a follow-up issue, a `TODO`, or a "phase 2" — refused by First Principles 8-10; the only sanctioned deferral is one the OWNER stated and governs. **Over-delivery is measured in customer-observable behaviour, never in diff size**: an unrequested feature, a reworded label, a moved control, an altered default is a finding even when it is an improvement, while extra *mechanism* — an extraction, a guard, a hardened path, a test nobody asked for — changes nothing a customer can perceive and is how **Clean**, **Robust** and **Maintainable** get paid for. So size only earns the question "which of these does the ask require?": a 300-file refactor that answers it is fine, a 3-line diff that quietly changes a default is not. This is also the boundary against **Proactive** — sweeping a class *within* the surface the change already touches is completeness; adding capability past it is scope the owner never approved (`AGENTS.md` § "What 8-10 bind — and what they do not").

### Spawn the t3:reviewer Sub-Agent Before Pushing (Non-Negotiable)

**Self-review by the implementing conversation never satisfies the shipping gate's `reviewing` phase.** The implementer's context carries every "looks done" blind spot that allowed the gap in the first place — that is exactly what produced souliane/teatree#545's six rounds of follow-up review fixes (missed renames, broken tests, undocumented contract changes, bypassed FSM). The corrective is an independent sub-agent that hasn't seen the implementation conversation.

**The only sanctioned path** to advancing a ticket from `TESTED → REVIEWED` is:

1. Spawn the `t3:reviewer` sub-agent from the main conversation via the `Agent` tool. The full Agent invocation snippet, FSM transition mechanics, and "drive transitions, not visit phases" rules live in [`../ship/SKILL.md`](../ship/SKILL.md) § "Review Gate" — that section is the source of truth.
2. Apply every finding the sub-agent surfaces. Reviewer agents are read-only; the implementing conversation owns the edits.
3. Drive the FSM `review` transition by completing the reviewing task (auto-fires `ticket.review()` and keeps the task ledger clean). **Never** use `t3 <overlay> lifecycle visit-phase reviewing` to *skip* the independent reviewer — since #694 the shipping gate reconciles `Ticket.state` from `Session.visited_phases`, so a manual visit *will* unblock `pr create`; that is precisely why recording `reviewing` without an independent review having happened defeats the gate. Earn the phase first, then record it.

When `review_skill` (env `T3_REVIEW_SKILL`) is configured, the reviewing-phase evidence gate (#1539) hardens this further: `lifecycle visit-phase <id> reviewing` refuses unless a `review_skill_run` artifact attests the configured skill ran. After running the skill, stamp the evidence with `t3 <overlay> lifecycle record-review-skill-run <id> <skill>`, then record the phase. With `review_skill` unset the gate is a NO-OP (opt-in default).

Reviewing carries the same responsibility as implementing, so deep retrieval is a **constraint, not a rule**: when `require_review_context` is set, the FSM `→ reviewing` transition (`teatree.core.gates.review_context_gate`) mechanically refuses until the work item is fetched from its source (Notion / GitLab — follow the MR description's links), every referenced document is downloaded + read, and the implementation is analyzed against them — stamp it with `t3 <overlay> lifecycle record-review-context <id> --work-item <url> --documents <urls> --analysis <how-checked>`. A diff-only verdict cannot enter `reviewing`.

**NEVER directly assign a reviewer on an MR/PR — review is REQUESTED, never assigned (Non-Negotiable).** Colleague review is obtained ONLY by posting the MR link to the Slack/approval channel (`/t3:review-request` → `review-request post`); the reviewer self-claims from there. Never set a reviewer directly — not via `glab mr update --reviewer`, not via a `reviewer_ids`/`requested_reviewers` API write, not via the MR-update MCP tool's reviewer arg — least of all on the user's OWN MR (this happened on the user's MRs and is forbidden). A PreToolUse gate (`handle_block_self_reviewer_assign`) blocks every direct-assignment surface — including a raw `curl` REST write; a vetted one-off on a colleague's MR needs an explicit `[reviewer-ok: <reason>]` token. The one assignment that is not a colleague request is the overlay's own standing `pr_auto_reviewers` policy: set in the same POST that opens the MR, and caught up on already-open MRs by `t3 <overlay> review apply-reviewer-policy`, which names no username, applies only on a repo the overlay writes under a non-owner credential, and refuses any MR that identity did not author.

The "Self-Review Before Finalization" workflow below is a **complement** to the sub-agent pass, not a replacement. Run it first to catch the obvious things, then spawn the reviewer.

### Self-Review Before Finalization

**Review ALL diverging code**, not just the last commit:

```bash
git diff --merge-base main
```

**Precondition — branch must be current with main.** If main has advanced since the branch's merge-base, the diff will surface those new commits as phantom "reversions" — code the author looks like they deleted but actually never had. Reviewing on top of a stale branch produces spurious scope-creep findings AND can let real silent-revert PRs through.

```bash
git fetch origin main --quiet
git merge-base --is-ancestor origin/main HEAD || git merge origin/main --no-edit
```

Run this **before** the cleanup checklist. Resolve any conflicts the same way you would on a normal merge — no rebase, no stash.

**Cold-review checkout — fetch the exact pushed head, never `git worktree add <branch>` (Non-Negotiable).** A cold-review sub-agent reviewing a PR on a fresh checkout must NOT run `git worktree add <dir> origin/<branch>` (or the local-branch form). When that branch is already checked out in another worktree on the same machine, `worktree add` fails and the agent silently falls back to a pre-existing (stale) checkout — reviewing a tree one commit behind the pushed head and producing a spurious CHANGES_NEEDED (souliane/teatree#2132). Use the verify-or-fail helper `teatree.utils.review_checkout.add_review_worktree_at_head(repo, ref=<branch>, expected_sha=<pr-head-sha>)`: it fetches the ref into a guaranteed-unique temp dir, checks it out with `git worktree add --detach FETCH_HEAD` (cannot collide with a branch worktree), and asserts the materialised HEAD equals the PR head SHA — hard-failing with `StaleReviewCheckoutError` rather than ever falling back to a stale tree. Remove the returned worktree with `teatree.utils.git.worktree_remove` when the review is done.

#### Two Axes: Read the Diff Three-Dot, MEASURE on the Merge Result (Non-Negotiable)

The three-dot guidance above covers the **diff axis** — what the branch introduced. It says nothing about the **runtime axis** — which tree you import, run, and measure on. A reviewer can diff three-dot correctly and still take every runtime measurement against the branch checkout, which reports what `main` did to a file since the branch was cut. That is how a docs-only PR (zero `src/` files changed) was blocked by a confident, high-severity `src/` finding about code it does not touch, whose every prescription was already on `main` — applying them would have turned an `rc=0` merge into a conflict. A stale finding is not inert.

**Probe the merge result, in one step:**

```bash
t3 review merge-tree --repo <clone> --base origin/main --head <pr-head-sha>
# → {"path": "/tmp/t3-merge-tree-XXXX", "tree_oid": ..., "base_sha": ..., "head_sha": ...}
```

It extracts the merge result to a **plain directory** and git-inits it with the source clone's real `origin` URL. Do not hand-roll it; each of the obvious hand-rolled shortcuts has produced a confident, reproducible, wrong answer on this repo:

1. **Never a git worktree.** `resolve_data_dir` (`src/teatree/paths.py`) auto-isolates a worktree onto a per-worktree DB, so the probe measures a different database than the one under discussion.
2. **Never a bare `git archive` extract.** A tree with no `.git` breaks every test that shells out to git.
3. **Never a clone whose `origin` is a local path.** `resolved_repo_slug` (`src/teatree/core/merge/pr_slug_resolution.py`) returns `""` for an unresolvable origin, silently defeating every repo-scoped match downstream — a test then fails on the branch and passes on `main` for reasons that have nothing to do with the diff.

**Before filing a finding whose evidence is a difference between two trees, enumerate what differs between them besides the diff.** Origin URL, presence of `.git`, working directory, data dir, installed venv, and ambient credentials have each produced a false result here. "Fails on the branch, passes on main" is a claim about a *difference*; the diff is only one candidate for it.

**A recorded HOLD is READ, never re-derived (#4476).** Findings are persisted on the verdict and rendered by two surfaces, so an author fixes what the reviewer actually found and a later reviewer CHECKS the findings were addressed rather than reaching a fresh judgment:

```bash
t3 <overlay> review findings <pr-url>            # the findings, rendered; --json for the machine shape, --sha to pin a tree
t3 <overlay> review status <pr-url> --json       # the full status record, findings included
t3 <overlay> review publish-findings <pr-url>    # post them to the PR (idempotent) — `review record` already tries
```

`review record` posts a HOLD's findings to the PR itself, so the author sees them where the work is. That post is colleague-visible, so it passes the on-behalf pre-gate: on the shipped `draft_or_ask` it is WITHHELD and the reason is reported on the record result (plus a DM carrying the findings). Clear it the solution-oriented way — `t3 <overlay> config_setting set on_behalf_auto_actions '["post_e2e_evidence","post_review_findings"]'` to enable it durably for this overlay, or `t3 review approve-on-behalf <slug>#<pr> post_review_findings --approver <user-id>` for one post — then `review publish-findings` to deliver it. A payload that cannot be rendered is a loud refusal, never a `findings_count` with nothing behind it.

Discharging a hold needs no new state: verdicts are newest-wins, so a later `merge_safe` recorded at the same head supersedes the HOLD.

**Mechanically enforced (#4251):** a blocking finding (`blocker`/`major`/`high`/`critical`) citing a file outside the PR's own changed-file set is REFUSED at record time — by `t3 <overlay> review record` and by the headless orchestrator that records a returned `review_verdict` alike. Re-measure on the merge result, then re-record with `--merge-result-retake` (CLI) or `"merge_result_retake": true` (envelope) if the finding survives. The gate declines to judge when the changed-file set cannot be read: an unread diff proves nothing. The check is also the cheapest one available to you by hand — a finding that asserts the PR changes no `src/` file *and* reports a `src/` regression refutes itself, and `git log origin/main -- <cited-file>` names the PR actually responsible.

Cleanup checklist:

- [ ] No code duplication introduced
- [ ] No dead code left behind
- [ ] **Comments earn their line:** every comment the diff ADDS is one line carrying a non-obvious *why*. A multi-line block narrating the code is a refactor signal — rename or split rather than shorten the prose. Pre-existing comments stay untouched.
- [ ] **Routing reachability:** every modified component is reachable via the target flow's route tree. Read the relevant `routes.ts` and confirm the component (or its parent shell) appears there. If the component lives in a flow-specific folder (e.g., `natural-person-calculation/`), verify the target flow actually routes through it.
- [ ] Naming follows project conventions
- [ ] Patterns match existing codebase
- [ ] No debug/temporary code remaining

#### Active Verification Against Repo Rules (Non-Negotiable)

After the cleanup checklist, **actively verify each changed file against the repo's agent config files** (`AGENTS.md` or the repo's equivalent agent instructions file) — not as a passive reminder, but as a file-by-file gate:

1. **Read** the repo's agent config files (e.g., `AGENTS.md` or the repo's equivalent agent instructions file).
2. **For each changed file**, check against every applicable rule section. Focus on:

- Architectural patterns (e.g., container-presentational, signals-first, inject vs constructor)
- Feature flag and multi-tenant rules (see [`references/multi-tenant-development.md`](../code/references/multi-tenant-development.md) § Review Checklist)
- Banned patterns (e.g., manual `.subscribe()`, `any` types, hardcoded strings)

3. **Check consistency across the changeset** — if the same pattern is applied differently in two files within the same PR, that's a finding.
4. **When a repo rule conflicts with a teatree or overlay skill rule**, do NOT silently pick one. Present both rules to the user with the specific conflict, ask which takes precedence, and save their decision to the agent's memory for future reference.

This step catches the class of bugs where the rules exist but weren't applied during implementation — missed feature flags, wrong DI pattern, manual subscriptions where signals were required, etc.

#### Module-Level Architectural Check (Non-Negotiable)

After verifying repo rules, **check the full file** (not just changed lines) of every file touched by the diff against the loaded coding skills' **"Architectural Health"** review checklist.

1. **Identify loaded coding skills.** TeaTree auto-detects `ac-*` skills from the repo shape (e.g., `ac-python`, `ac-django`). If they have an "Architectural Health" review checklist section, apply it.
2. **For each touched file**, evaluate the FULL file against those checklists. Key checks (skill-specific details are in the skill itself):
   - Module size (LOC)
   - Module-level function count and justification
   - God-module detection (unrelated concerns in one file)
   - Complexity rule suppressions in `pyproject.toml` — any `C901`/`PLR09xx` per-file-ignores beyond the project's boilerplate baseline are findings
3. **When a threshold is crossed**, never suppress the lint rule. On **your own** change, refactor to comply in this same PR — the module is one this diff already touches, so `AGENTS.md` First Principles 8-10 put the fix here, not in a follow-up ticket. When reviewing **someone else's** change, post it as a finding and leave the fix to the author (maker ≠ checker).
4. **Check `pyproject.toml` per-file-ignores** for the touched files. If any suppress complexity rules that are not in the project's boilerplate baseline, flag them as findings.

This step prevents architectural drift. Each diff looks fine in isolation — this check catches the cumulative effect by examining the full module.

#### File-Hierarchy & Module-Placement Check (Non-Negotiable)

The Module-Level Architectural Check above asks *what's inside* each touched file. This one asks *where the changed files live* — but **scoped strictly to the diff**, never a whole-tree audit. Examine only files the change adds, moves, or renames, plus the directories they land in:

1. **New files in the wrong directory or module.** For each added file, confirm it sits in the package whose concern it shares. A scanner belongs under the scanners package, a CLI command under the CLI package, a model under the models package — flag a file dropped beside unrelated neighbors with a concrete "this new file should live at `X`" suggestion.
2. **Should the change have created or moved into a subpackage?** When a diff adds the third or fourth sibling file all serving one new concern into an already-crowded directory, flag that the cohesive set should become its own subpackage (with the proposed path).
3. **Files added at the repo root that belong under a directory.** A new script, config, or module dropped at the repo root is a finding unless the repo's conventions place it there — name the directory it should move under.
4. **Diffs that worsen module cohesion or scoping.** Flag a change that widens a module's responsibility (an unrelated concern bolted onto an existing file), leaks a private helper across a package boundary, or imports across a layer the architecture keeps separate — point at the boundary the change crosses.
5. **Obvious reorg opportunities the change reveals.** When implementing the change makes a misplacement plain (e.g. the file you just edited clearly belongs next to the collaborators it now calls), surface the concrete move — but only for files this diff touches.

Each finding must name the suggested target path so the implementer can act without re-deriving it. **Full-tree reorganization audits are out of scope here** — sweeping the entire repository's layout for misplaced modules is the `ac-reviewing-codebase` skill's job (the periodic holistic review dispatched by the architectural-review loop). Keep this per-change check scoped to the diff so the two surfaces complement rather than duplicate each other.

#### Keep BLUEPRINT Tight (Qualitative — Not a Byte Gate)

When the diff touches `BLUEPRINT.md` (or a `docs/blueprint/*.md` appendix), review the prose for bloat as **reviewer judgment** — there is no hard size cap or byte-delta budget (a single hand-edited KB constant every BLUEPRINT-touching PR had to bump just made concurrent PRs re-break each other's CI, with no quality signal). The BLUEPRINT is architectural, not a prose mirror of the code. Flag:

1. **Prose that restates code rather than capturing architecture.** A paragraph that walks through what a function does line-by-line belongs in a docstring, `--help` text, `CLAUDE.md`/`AGENTS.md`, or the code itself — not the BLUEPRINT. The BLUEPRINT answers "why is the system shaped this way", not "what does this function do".
2. **Stale or duplicated sections.** A section describing a mechanism that the diff just changed (or removed) must be updated or deleted in the same PR — see the documentation-alignment rule. Two sections saying the same thing is a consolidation finding: point at the one that should remain.
3. **Appendix-class detail in the top-level file.** When a section grows past architectural overview into implementation depth, suggest splitting it into a linked appendix under `docs/blueprint/` (name the target path) so the top-level file stays digestible. The top-level file holds the architecture; appendices hold the depth. BLUEPRINT.md stays one file — move detail out, never split the top-level file itself.

Scale the finding to impact: a section that legitimately documents a new architectural invariant is fine even if it grows the file — the test is "does this prose earn its place as architecture", not "how many bytes did it add". The full-tree staleness sweep (every section vs current code) is the periodic holistic review's job (`ac-reviewing-codebase` / the architectural-review loop); this per-diff check is scoped to what the change touches.

#### Read BLUEPRINT.md Before Designing (Non-Negotiable)

Before proposing a design that changes how existing code is structured, read `BLUEPRINT.md` and any architectural-invariants doc FIRST, not last. Inventory existing patterns touching the same subsystem before proposing new ones. If the proposed design reverses a BLUEPRINT invariant, surface that to the user BEFORE designing around it — the user decides whether to overturn the invariant; if yes, update BLUEPRINT.md in the same change.

#### Architecture Refactor Blast-Radius Checklist

After any architectural refactor, before declaring done:

1. **Grep all file types** for old API names — not just `.py` but `.md`, `.toml`, skill files, mermaid diagrams, comments.
2. **Lint rules are architectural guardrails** — never suppress; fix the design instead.
3. **Cross-repo consumers** must be updated in the same session.
4. **Documentation drift is invisible to tests** — re-read every doc/skill file that references the changed subsystem. 100% test coverage does not catch stale docs.

#### Consolidation Scan (cross-reference)

During any review that touches architecture, configuration, or tooling setup: scan for behavior encoded outside the framework that belongs inside it — ad-hoc hooks, manually wired permissions, personal-config automation. Classify and promote where warranted. Full decision rule: see `retro/SKILL.md` § "9. Consolidation over Drift".

#### New-Test Shape Check (Non-Negotiable)

When the diff adds or modifies test files, verify the new tests follow the repo's test-writing doctrine (see the repo's `AGENTS.md` § "Test-Writing Doctrine" — teatree and every overlay repo carry the same rule):

1. **Mock density.** If a new test file is mostly `Mock()`, `patch()`, `MagicMock`, or `mock.call_args` assertions, flag it. Ask: could this have been a Django test client call, a `call_command` invocation, a real `tmp_path` git repo, or a Playwright E2E?
2. **Mock targets.** Mocks should hit unstoppable externals only — network (GitHub, GitLab, Slack, Sentry), clock, `pass`, third-party subprocesses. Mocking teatree code, Django models, filesystem under `tmp_path`, or `git` itself is a finding.
3. **Missing integration coverage.** If the diff adds a view, a management command, or a new CLI surface and only ships unit tests, flag it — the happy path belongs in an integration test.
4. **Coverage preservation.** Any test rebalancing (removing units, adding integration) must keep the coverage gate satisfied. Report the before/after coverage number in the review.

Accept a mock-heavy test only when the PR description justifies why a higher-level test couldn't cover the same behavior (e.g., a rare error branch that's painful to trigger through the real entry point).

### The Skilled Lifecycle Is the Bar Before Requesting Review or Merging (Non-Negotiable)

Correctness is the **maker's** responsibility, not the reviewer's. Colleagues review shallowly and a wrong MR sent to them ships — so the gate that catches our bugs is the maker's own *skilled* lifecycle, run before the work ever leaves our hands. Before requesting colleague review **or** merging any MR (in any repo — this repo and every overlay alike), confirm every step below was actually done, using the relevant skills at each step:

1. **Retrieved and analyzed in depth** — the ticket / Notion / spec and every linked document were fetched and read (the deep-retrieval constraint above), and the diff was mapped against the acceptance criteria, not assumed.
2. **Planned in depth using the overlay skills** — the architecture pass (`/t3:architecture-design`) and the overlay's coding skill informed the approach before code was written.
3. **Coded using the skills** — implementation followed the loaded coding skills, not improvised.
4. **Self-reviewed using the skills** — the checklist above plus the **anti-vacuity proof on every NEW regression test**: revert the production fix and confirm the test goes **RED**; if it stays green it guards nothing. The canonical vacuity pattern is a guard that **skips the failing case** — a `seen >= 2` / `>= N` gate, a first-iteration skip, an assertion on a structurally-guaranteed post-condition the buggy code also satisfies. The full rule is the source of truth in [`../code/SKILL.md`](../code/SKILL.md) § "TDD Discipline" ("A regression test is only valid if it has been observed to FAIL on the pre-fix code"); do not duplicate it, apply it.
5. **E2E created when relevant** — UI / cross-service behavior carries a Playwright spec (`/t3:e2e`).

A vacuous regression test passing green is **not** evidence the fix works — it is the failure mode this gate exists to catch. If the anti-vacuity proof can't be produced (the test stays green with the fix reverted), the work is not review-ready: fix the test and the code first, then re-run the proof.

When `require_anti_vacuity_attestation` is set, stamp the proof with the `record-anti-vacuity` lifecycle command before the `request review` or merge transition — the gate mechanically refuses the transition without it:

```bash
t3 <overlay> lifecycle record-anti-vacuity <ticket-id> \
  --head-sha "$(git rev-parse HEAD)" \
  --ac-coverage 'how the diff was mapped to each acceptance criterion' \
  --proven-test 'tests/path::test_name'   # OR --no-new-tests if the diff adds no regression test
```

The flag is `--head-sha`, not `--sha`.

**Independent adversarial review is an *optional escalation*, not a requirement.** For a complicated implementation — subtle concurrency, a wide blast radius, a contract change across services — escalate to an independent adversarial pass (e.g. a `codex` cold-review, reviewer ≠ maker) to falsify the diff against each acceptance criterion. For ordinary changes the skilled self-review above is the bar; don't gate every MR on a second reviewer.

### Quality Gate Verification (Verify-Fix-Repeat)

Before declaring review-ready, run all gates and **iterate until they pass**. Do not declare review-ready after a single pass — re-run gates after every fix, because fixes can introduce new failures.

```text
Run gates → Any failure? → Fix → Re-run gates → Repeat until clean
```

**Gates (run in order):**

1. **Lint:** zero errors from the project linter
2. **Type check:** passes (if the project uses it)
3. **Tests:** full suite green (use `t3 <overlay> run tests` or project equivalent)
4. **No uncommitted changes:** all fixes staged and committed
5. **No regressions:** diff review confirms no unintended changes
6. **Skill references resolve:** run `t3 tool validate-skill-refs`. Every skill *name* referenced — the `$HOME/.teatree-skills.yml` keyword→skill routing config and the `agents/*.md` frontmatter `skills:` / `companion_skills:` lists — must resolve to a real skill in the canonical (installed/remote) skill set. A dangling name (the real `ac-reviewing-skills` → `ac-reviewing-codebase` case) exits non-zero with file:line, the bad name, and the nearest valid matches. The repo's own agent refs are also gated in pre-commit (`validate-skill-refs`); this command additionally covers the personal `$HOME/.teatree-skills.yml`, which lives outside the repo.

**Iteration limit:** After 3 fix-verify cycles without convergence, **stop and ask the user** — the issue may be systemic rather than incremental.

**Stop hook integration:** If the repo has a Stop hook (in the agent's settings), it enforces this loop automatically. Without a hook, run the gates manually before claiming done.

**References:** [Ralph Loop](https://github.com/snarktank/ralph) (external verification over self-assessed completion), [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) (Anthropic, feature-list-driven incremental verification).

### Giving Code Review

#### Two Lanes — a Colleague-Facing Post, and the Verdict Envelope (decide this first)

This chapter's deliverable is one of two things, and the reporting rules are **opposite** between them. Decide which before drafting anything.

- **Colleague-facing post** — a comment, discussion, or approval published on someone else's MR/PR **under the owner's identity**. Noise costs real credibility here, so the suppression rules below are binding on this lane: scale severity to confidence, say nothing on a check that came back clean, don't police formatting, don't block on style.
- **Verdict envelope** — the headless cold reviewer's `review_verdict` result (`teatree.agents.result_schema.ReviewVerdictEnvelope`), recorded server-side as the durable `ReviewVerdict` the merge gate reads. Nothing is published and `findings` is schema-optional, so suppression here buys nothing and costs only recall.

**In the envelope, record what you actually observed** — every finding, including the uncertain and the low-severity ones, each carrying its severity and your confidence. Coverage is your job; filtering is the merge gate's, downstream. Silence on a check you performed is a **missing record**, not a clean bill of health. `verdict: merge_safe` with an empty `findings` array asserts you looked and found nothing worth saying — emit it only when that is true, and record anything you could not check as a finding rather than leaving the array empty.

#### Fetch-Only vs Comprehensive Review — Pick the Right Entry Command (do X — never Y)

A colleague-authored MR on a **shared product repo** (a repo you do NOT solely own — shared with colleagues, gated on their review) is **review work, not merge work**. The action when you are handed one is to **fetch its diff and review it** — never to land it yourself. Do X (fetch + review); never Y (merge a teammate's product-repo MR):

- **Do — fetch the diff to start the review** (read-only, no state change):

  ```bash
  t3 review run <MR_URL>               # read-only review-shape audit (#1206): changes, complexity, existing review, findings catalog
  glab mr diff <MR_IID> --repo <repo>  # raw diff for a manual read (gh: gh pr diff <N>)
  glab mr view <MR_IID> --repo <repo>  # MR metadata / description (gh: gh pr view <N>)
  ```

  `t3 review run <MR_URL>` is the canonical first command — it never publishes and never merges; it just gathers what the reviewer needs. The plain `glab mr diff` / `gh pr diff` are the fetch-only fallbacks when you only need the raw patch.

- **The merge commands are out of scope on a colleague's product-repo MR.** `glab mr merge`, `gh pr merge`, and `t3 <overlay> ticket merge` do not belong on a teammate-authored shared-repo MR — merging a colleague's product-repo MR treats their work as yours to land. The keystone merge (`t3 <overlay> ticket merge <id>`) is reserved for **your OWN** green, cold-review-cleared work (a solo-owned overlay repo you authored), not a colleague's. A repo being private is a visibility axis, not an ownership one — private ≠ yours-to-merge.

- **Provisioning and E2E are out of scope too.** Reviewing a colleague's MR is a **static diff review** plus **trusting their CI** — never a local checkout, `t3 <overlay> workspace ticket` / `worktree provision` / `worktree start` of their branch, nor an E2E/Playwright run of it; their pipeline is the runtime gate, your read of the diff is the review.

The A/B distinction: your own solo-owned overlay repo, green and cleared → merge it via the keystone; a teammate's shared product-repo MR → fetch the diff and review it, hold for the colleague, never auto-merge. (Own-vs-external routing is Step -1 below.)

**Pre-flight gate — complete BEFORE reading any diff:**

1. Determine own vs external PR (Step -1)
2. Fetch ticket context for every PR (Step 0) — without this you cannot judge correctness
3. List all commits per PR (Step 0b)
4. Read the repo's `AGENTS.md` / agent instructions file and any project-specific coding guidelines

Do NOT skip these steps to "save time" when reviewing multiple PRs. Each step exists because skipping it caused missed findings in real reviews.

**BINDING — never review an MR/PR already :eyes:-claimed by a colleague.** Do NOT dispatch or perform a review of any MR/PR whose review-broadcast / review-request message already carries a `:eyes:` (👀) reaction from someone other than the user — that reaction is the colleague's claim on the review, and a second pass duplicates their in-flight work. The only override is the user explicitly naming that MR (an `<@user_slack_id>` mention on the broadcast, or a direct instruction). This is enforced structurally in `SlackBroadcastsScanner` (`src/teatree/loop/scanners/slack_broadcasts.py`) via `eyes_reacted_by_other` (`src/teatree/core/review/review_candidate.py`), which excludes the user's own `:eyes:` so the gate only fires on a colleague's claim. When reviewing manually, check the broadcast's reactions first and skip a colleague-claimed MR unless the user named it. To enumerate the open MRs you are scanning and move to the next unclaimed candidate, list them with `glab mr list` (GitLab) / `gh pr list` (GitHub), then skip past any that already carry a colleague's :eyes: — there is no `t3` command for advancing to the next MR, so do not invent one.

**Emit only YOUR OWN verdict reaction — never re-add a check a colleague already placed.** When posting your review verdict as a reaction on the review-broadcast message, react with the emoji for YOUR verdict only. If the broadcast already carries a `:white_check_mark:` (or another verdict emoji) from a different reviewer, do not re-add it alongside your own — that duplicates a colleague's already-recorded signal and reads as if you independently re-verified their check. Your own distinct verdict (e.g. `:question:` for blocking) is the only reaction your review adds. Prefer the `mcp__teatree__slack_react` MCP tool — it places the reaction through the same on-behalf seam; fall back to the CLI when the MCP server isn't connected.

```bash
# do X — react with only your own verdict, leave the colleague's existing reaction alone:
t3 slack react C_REVIEW 171.5 question
# never Y — re-adding a check/verdict emoji someone else already placed:
t3 slack react C_REVIEW 171.5 white_check_mark   # FORBIDDEN when another reviewer already added it
```

Pinned by `review_reaction_dedups_existing_reactor` (`evals/scenarios/review.yaml`).

#### The review-DONE Slack signal is `mcp__teatree__slack_react`, or `t3 slack react` with three positional arguments

A finished review emits its verdict on the MR's review-broadcast message as a Slack reaction. Prefer the
`mcp__teatree__slack_react` MCP tool — same seam, structured result; when the MCP server isn't connected there is
exactly one CLI command for it and it takes **positional** `channel`, `ts`, and `emoji` — no `--emoji` flag,
and no reaction subcommand under `t3 review` (there is none; do not invent one), and the emoji name
carries **no colons**:

```bash
t3 slack react <channel> <ts> <emoji>          # e.g. t3 slack react C_REVIEW 171.5 white_check_mark
```

The verdict → emoji mapping is the one `teatree.loop.review_done_reactions.emit_review_done_reactions`
posts, so a hand-issued reaction matches what the loop would have emitted:

| verdict | emoji argument |
| --- | --- |
| clean — no blocking findings | `white_check_mark` |
| has blocking comments | `question` |

The command is idempotent: an emoji already on the message (whether teatree placed it or a colleague did)
is skipped and still exits `0`. So when a colleague's `:white_check_mark:` is already there and your own
verdict differs, react with **your** verdict only — one command, the emoji that is actually yours. Never
re-issue the reaction that is already present, and never substitute a DM to the author for the reaction;
the substance of a review is its inline MR comments, and the reaction is the only Slack signal it emits.

#### Colleague-MR Autonomy — Act on the Verdict, Don't Ask (config-driven)

What the agent does *after* an independent cold-review verdict exists on a **colleague-authored** MR (the MR's author is not your identity) is governed by **one config knob**, the per-overlay `autonomy` switch (`src/teatree/config/settings.py`; tiers `full > notify > babysit`, see [`docs/blueprint/configuration.md`](../../docs/blueprint/configuration.md) § 10.1). Read the resolved tier with `t3 <overlay> autonomy show` and set it with `t3 <overlay> autonomy set <level>` (`--global` for the workspace default) — never hand-edit config. It is *not* a per-MR judgement call and *not* a personal memory rule — read the resolved tier and follow it.

**Autonomous tiers (`autonomy = "full"` or `"notify"`):** once an independent cold-review verdict exists, act on it — no "say the word", no per-MR ask. What the tier removes is the *asking*, not the egress gate:

- **Merge-safe verdict** → post the terse verdict / nits **and** `t3 review approve`.
- **Nits only** → post them; approve per the merge-safe rule above.
- **A blocking finding** → post it; do **not** approve.

`notify` additionally DMs the user after each on-behalf post (derived `notify_on_behalf`); `full` posts without the after-the-fact DM.

**The tier does not open colleague egress (#3895).** `_AUTONOMY_COLLAPSED_GATE_VALUES` holds `require_human_approval_to_answer` only; `on_behalf_post_mode` sits outside it exactly as `require_human_approval_to_merge` does (#3630), so speaking to a colleague under the user's own identity stays its own named opt-in. Under the shipped `draft_or_ask` the on-behalf pre-gate still BLOCKs a live post at any tier — so the autonomous form of "act on the verdict" is `t3 review post-comment` (a draft, colleague-invisible, exempt under every mode, and the agent DMs the user the publish command). Go live only where the overlay pins `on_behalf_post_mode = "immediate"`, or where an `OnBehalfApproval` is recorded for that action.

**Live posts still need a token, even under `full`.** The `--live` colleague-visible publish is gated by the #1207 single-use `LivePostApproval` token (`teatree.core.gates.live_post_gate.require_live_post_approval`), which `publish_live_post` enforces **orthogonally to `on_behalf_post_mode` and to `autonomy`** — it is *not* in the collapsed-gate set, so `post-comment --live` is refused with no token regardless of tier. Under an autonomous tier, mint the token in the same one step that records the on-behalf authorization — `t3 review authorize <repo>!<mr> --approver <user-id>` (#126) — then post live; or post the verdict as a **draft note** (`t3 review post-comment`, the default), which needs no token. Either path keeps the autonomous "act on the verdict, don't ask the user per-MR" posture; the token is a single-use idempotency/audit seal on the outward publish, not a per-MR user decision.

**Babysit tier (`autonomy = "babysit"`):** keep the draft-and-ask flow — drafts publish autonomously, every live post / approval waits for the user (Step 3 below; `t3 review authorize`). This is the right setting for client / shared-team overlays.

**The quality floor is identical under every tier and is never relaxed by this knob:** the verdict must come from an *independent* cold reviewer (maker ≠ checker — never self-approve your own MR), findings are verified against ground truth before posting (a Blocker you cannot falsify is posted as a question, not a Blocker), `t3 review approve` keeps its review-first precondition (no approval without a prior reviewing footprint), and CI must be green. The knob decides *whether to ask the user*, never *whether the work is correct*.

**Verifying a colleague's finding before posting (and retracting if it was wrong):** before a finding goes out under the user's name on a colleague's MR, confirm it against ground truth — the real code, live data or the DB, and the domain conventions — not against your own mental model of how the code probably behaves. A *recheck* is an independent re-derivation that tries to **falsify** the finding (re-grep, re-query, re-read the producer's schema), never a re-read of your own earlier note — re-reading your note only re-confirms the mistake that produced it. If the verification cannot pull the finding to certainty, post it as a question, not a Blocker. And if a finding you already posted turns out to be wrong, retract **all** affected findings at once and quickly: a stale false Blocker sitting on a colleague's correct MR reads as the user not understanding the code, and the longer it stays the more it costs the working relationship.

**Step -1 — Own PR vs External PR:**

When the PR under review belongs to the **user themselves**, do NOT post review comments. Instead, **implement the fixes directly** on the branch — commit and push. Present findings to the user as a summary of what you fixed, not as review comments to post. The user is asking you to take over and improve their code, not to leave notes for themselves.

**Step 0 — Gather Ticket Context:**

Before reading any code, fetch the referenced ticket/issue to understand the *intended* behavior. Extract the ticket URL from the PR title/description, then fetch it via the `mcp__teatree__github_issue` / `mcp__teatree__gitlab_issue` MCP tool — falling back to the issue-tracker CLI (`gh issue view`, `glab issue view`) when the MCP server is not connected. Fetch every attached spec and linked external requirement too; attachments are the authoritative spec, and an author's docstring summarising them is not a substitute.

**Hard rule — refuse blind reviews.** If a ticket references a spec attachment or external requirements document you cannot retrieve, **STOP**. Do not post review notes. Report back to the user: which document you could not fetch, what you tried, and what access is needed. An overlay MAY declare specific sources out of scope (a partner portal behind SSO); honour those exceptions. For anything else, a review with missing spec context is not a review — it is guessing, and guessing attached to the user's account damages the author's trust.

**The reviewer does the verification, not the author (Non-Negotiable).** Every comment goes out under the user's name, so one that boils down to "I'm unsure, please confirm" makes the user look like they do not know their own codebase. If a draft comment names a file, function, schema, enum, or downstream caller reachable from the local checkout, open it and read it before posting. "Worth checking `foo.py`" is not a review comment — it is the reviewer outsourcing their job. Either the file says the code is wrong (post a verified finding) or it says the code is fine (post nothing).

The rest of steps 0 through 0h — the attachment-fetching recipes and annotation reading, reviewing all commits individually, discussing before posting, the full investigation ladder behind the rule above, not policing another author's title format, the overlay's auto-close policy, cross-service verification, and choosing PR over chat as the venue — are in [`skills/review/references/giving-review-investigation.md`](references/giving-review-investigation.md).

**Step 1 — Structured Review Checklist:**

1. **Correctness** — does the code do what the ticket requires? Are all acceptance criteria met? When a change tightens a public contract (e.g., serializer field becomes required, API parameter becomes mandatory), trace all callers — the change affects every flow that uses that interface, not just the one the ticket describes.
2. **Completeness** — are there missing production code changes that the tests assume? Do test expectation changes have matching implementation changes?
3. **Feature flag** — follow the review checklist in [`references/multi-tenant-development.md`](../code/references/multi-tenant-development.md). **Before raising a "missing feature flag" finding, trace the full gating chain upward** — the component under review may not have a flag itself but could be hidden/disabled at the container or routing level (e.g., `hidden: !featureFlagService.hasFeatureFlag(...)` in the parent that renders it). A finding is only valid if the feature is reachable without the flag.
4. **Style** — follows project conventions?
5. **Tests** — adequate coverage of new behavior?
6. **Safety** — no security issues, no data loss risks? For shared mutable state (a registry/cache file, a row touched by concurrent processes), the **whole read→decide→write must be inside one lock/transaction** — a flock (or DB lock) that guards only the write still allows a lost-update / double-claim TOCTOU when two processes both read the old value, decide independently, and write in turn. A docstring or BLUEPRINT claim that writes "cannot lose a read-modify-write update" is false unless the read is inside the same critical section as the write; flag the mismatch.
7. **Migrations** — reversible? data-safe? performance-safe?
8. **Scope** — are unrelated changes bundled in? Flag only if genuinely unrelated; small related fixes alongside the main change are normal practice.
9. **PR metadata** — title and description comply with the overlay's commit message format? If the overlay provides `validate_pr()`, run it programmatically rather than checking by eye.

**Step 2 — Review Tone & Formatting:**

Follow the [Google Engineering Practices — Code Review Standard](https://google.github.io/eng-practices/review/reviewer/standard.html): approve if the CL improves overall code health, even if it isn't perfect. Don't block on style preferences or theoretical improvements. The bar is "does this improve the codebase?" — not "is this how I would have written it?" That governs the **verdict**, not the record: a style preference is not a reason to `hold`, and it is still worth recording as a low-severity finding in the verdict envelope.

Comments are posted under the user's name. They must sound like a **real human colleague** wrote them — not an AI, not a linter, not a manager.

**Verification belongs to the reviewer, not the author:**

Before posting a concern, open the relevant file and verify it yourself. Comments like "worth checking" or "please confirm" push verification work onto the author when the reviewer has the same codebase access. Grep enums, read migrations, check sibling repos — silence when the code is correct.

Speculative questions ("is this correct?", "could this cause issues?") without evidence waste the author's time. If unsure, investigate first — a concern backed by evidence is useful; a guess is noise.

**Voice & attitude:**

- **Be the best colleague.** Helpful, curious, humble. Happy to teach, never to humiliate. You're a peer who genuinely wants the code (and the author) to succeed.
- **Never parent.** Don't lecture, don't explain things the author obviously knows. If you're providing context, frame it as "in case it helps" or "I think this might…" — not "you should be aware that…".
- **Be collegial.** Phrase observations as questions or suggestions, not orders. "Would it make sense to…?" beats "You must…".
- **Assume good intent.** A reverted line is more likely an accidental rebase artifact than carelessness. Frame it that way.
- **Acknowledge what's good.** If the approach is sound, say so briefly before raising issues.
- **Scale severity to impact.** A missing production code change that breaks tests is critical. A minor style nit is not. Don't escalate small things.
- Separate tickets/PRs are not needed for minor scope additions. A small related fix alongside the main change is normal — only raise scope if genuinely orthogonal work is smuggled in.

**Formatting rules:**

- **Concise, bullet-form, no prose (directive #4).** A review is findings, not an essay. Lead with the finding; skip the preamble and the summary of what the diff does (the author wrote it). One point per bullet, `severity: finding` shape. No "Overall this looks great, however…" wind-up, no restating the PR description. Be RIGHT but concise — trim words, never the evidence that makes a finding actionable. The shape/bloat gates below enforce this structurally on colleague MRs.
- **Single terse inline finding on a colleague MR.** On a colleague's MR (the MR's author is not your identity), the binding shape for an on-behalf review is **one terse inline comment anchored on the file:line that motivated it, keeping the finding's own severity label** — `HIGH (correctness): ...`, `MED: ...`, `LOW: ...`, and a bare `Nit:` reserved only for a genuinely trivial item (style, naming preference). Never a multi-section Problem/Fix/Verification dump, and **never downgrade a HIGH/MED finding to `Nit:`** to squeeze under the cap (that produces the nonsensical "Nit (MED)" — terseness is about length, not severity). Enforced structurally by the colleague-MR shape gate in `src/teatree/cli/review/shape_gate.py` (souliane/teatree#1114, loosened in #1159): the body is capped at 3 blank-line-separated paragraphs and 200 words; the gate refuses the post with steering text before any GitLab API call. Multi-sentence findings are fine — the cap targets abuse (multi-section dumps), not legitimate ≤3-sentence findings. Own-MR reviews are exempt (long-form self-review summaries are fine).
- **A review comment is about the diff, not the tracker.** Keep project chatter out of the comment body — no `@handle` stakeholder mentions, no Slack-thread timestamps, no "ping the author / sync with the team / discuss in standup" coordination. State the finding on the code. A *bare* `tracked at #1234` non-blocker pointer is fine (it adds genuine context); it is the chatter directive wrapped around the id that bloats. Enforced structurally by the comment-bloat gate in `src/teatree/cli/review/bloat_gate.py` (souliane/teatree#2663): a chatter-laden body is refused before any GitLab API call. `--allow-bloat` is the per-call escape for a genuinely load-bearing reference. The note-length dimension stays with the shape gate above; this gate is orthogonal.
- **Prefix nits.** When a comment is nitpicking (style, naming, minor preference), prefix with `Nit:` so the author knows it's non-blocking.
- **Backticks for code.** Always wrap code symbols, class names, method names, variable names, file paths, and CLI commands in backticks (`` ` ``).
- **Use suggestion blocks for concrete code changes.** When you have a specific replacement in mind, use the platform's suggestion feature (` ```suggestion ` fenced block on both GitLab and GitHub) so the author can accept with one click. GitLab supports `:-N+M` to expand the range. Combine explanation text **before** the suggestion block.
- **Readable structure for longer comments.** Use empty lines to separate distinct sections (problem, suggestion, example). Within a section, use line breaks between sentences (without empty lines) to keep things scannable. Short comments stay on one line — don't over-structure a one-liner.
- **No walls of text.** If a comment needs more than ~5 lines, break it up visually. Paragraphs, not monoliths.

**Author-Marked TODO/FIXME — Never a Blocker (Non-Negotiable):**

A `// TODO`, `# TODO`, `/* TODO */`, `// FIXME`, `# FIXME`, `// XXX`, `# XXX`, `// HACK`, `# HACK` marker on an added line — or the phrases "not in this MR", "follow-up", "deferred", "implement later", "out of scope" — is the **author explicitly documenting that the work is deferred**. NEVER post a blocker-shaped (REQUEST_CHANGES) comment anchored to (or within ±3 lines of) such a marker. The strongest verdict allowed is a non-blocker comment, and only when it adds genuinely new context (e.g. "tracked at [#NNN]") — not re-stating what the author already said.

`t3 review post-comment` and `post-draft-note` enforce this deterministically via `src/teatree/cli/review/todo_gate.py` (souliane/teatree#1186): a blocker-shaped body anchored on a TODO-adjacent line is REFUSED with a clear error before any GitLab API call. If you genuinely believe the TODO must be addressed in THIS MR (rare — the author knows their scope), STOP and surface to the user — never post on their identity.

Failure mode this prevents: re-asking a colleague to do work they have explicitly deferred makes the reviewer (and the user, whose identity posts on-behalf) look unable to read code.

**Scope — this is a reviewer rule, not a licence to leave one.** It governs how you treat *another author's* deferral on *their* MR: their scope decision is theirs, and re-litigating it in a blocker is the failure above. It says nothing about the work you author yourself, where `AGENTS.md` First Principle 8 forbids leaving a `TODO`/"follow-up"/"out of scope" marker at all — self-review catches it before the diff leaves your hands. The two never collide, because they never apply to the same author.

**Step 3 — Post Draft Review Comments (babysit tier):**

The babysit-tier draft flow — the pre-flight read of existing discussions, the `t3 review post-comment` / `--body-file` / `--live` recipes, the anchoring pre-flight and post-flight checks, the collapsed-diff workaround, and the publish/delete subcommands — is in [`skills/review/references/posting-review-comments.md`](references/posting-review-comments.md).

#### Position field reference

| Field | GitLab | GitHub |
|---|---|---|
| File path | `old_path` / `new_path` | `path` |
| New line (added/modified) | `new_line` | `line` + `side=RIGHT` |
| Old line (deleted) | `old_line` | `line` + `side=LEFT` |

### Receiving Code Review

- **User feedback** = trusted direction. Verify scope, then implement.
- **External reviewer** = verify technically before implementing.
- **Default stance toward a colleague's concern:** assume it is correct until you have exhaustively disproven it. Verify it deeply against ground truth before concluding it's a non-issue; a shallow check that confirms your first instinct is not a disproof. The cost of taking a wrong concern seriously is small; the cost of dismissing a right one is a missed bug and a colleague who stops raising them.
- **Push back when:** suggestion breaks functionality (show evidence), violates YAGNI, is based on stale context, or conflicts with user's stated architecture.
- **Anti-performative:** No "You're absolutely right!" — just state the fix or the technical disagreement.
- **Technical rigor:** verify reviewer suggestions against the actual codebase before implementing.

#### Replying to Review Discussions

When posting replies to reviewer discussions (e.g., "Done in `<commit>`"):

1. **Fetch all discussions via API** and inspect each one's first note — read the actual body, don't rely on assumptions about which discussion covers which topic.
2. **Match reply to the specific concern.** Read each discussion's first note body in full. The reply must use the same framing as the reviewer — if they asked about `FeatureFlagService`, don't reply about `takeUntilDestroyed`. Never post a generic "addressed in commit X" reply to a discussion about a different topic.
3. **Skip already-answered discussions.** If the user (or someone else) already replied with a resolution, do not post a duplicate reply.
4. **Present the mapping to the user before posting.** Show a table: `| Discussion | Topic | Reply |` and get confirmation. Never batch-post replies without review.

Post each reply via `t3 review reply-to-discussion <REPO> <MR_IID> <DISCUSSION_ID> "body"`. To mark a thread resolved after the reply, use `t3 review resolve-discussion <REPO> <MR_IID> <DISCUSSION_ID>` (pass `--no-resolved` to re-open).

### Recording a GIVE-review approval — `t3 review approve` (Mandatory)

When giving review on a colleague's MR and the verdict is approve, record it through the sanctioned CLI — never raw `glab mr approve` / `gh pr review --approve` (prohibited for state-changing review actions):

```bash
t3 review approve <REPO> <MR_IID>      # approve
t3 review unapprove <REPO> <MR_IID>    # revoke your approval
```

**Review-first precondition (enforced, not advisory) — never forces a public note (#2716).** `approve` refuses unless the approver has left a *reviewing footprint*, encoding the approve-on-review doctrine in the tool: you cannot record an approval without having reviewed first. The doctrine is an anti-rubber-stamp guarantee, **not** a public comment — so a content-free "APPROVE" prose note is never posted to satisfy it. Any of three footprints clears the gate, none of which is a colleague-visible auto-comment:

- a **published note/discussion** authored by your identity (a genuine inline finding — still fine, never auto-posted just to clear this gate);
- a **draft note** authored by your identity (a colleague-invisible review — no public comment);
- the **recorded internal verdict** — an `OnBehalfApproval` for `(<repo>!<mr>, "approve")`, the human-recorded, maker≠checker attribution the on-behalf approve path already requires. On the on-behalf path the `approve-on-behalf` row *is* the footprint, so approving records the verdict + the GitLab `approved_by` (and the ✅ reaction) with **zero** MR notes/discussions posted.

If it refuses, leave a genuine review (a real finding via `t3 review post-comment` — default draft, #1207 — or record the internal verdict), then approve. `unapprove` has no precondition — revoking is the safe direction and is always reachable.

**On-behalf gate.** An approval is an outward, state-changing post under your identity, so `approve`/`unapprove` also respect the `on_behalf_post_mode` pre-gate (souliane/teatree#960). No autonomy tier collapses that mode (#3895), so under the shipped `"draft_or_ask"` the command refuses unattended at every tier with an actionable message — record an `OnBehalfApproval` via `t3 review approve-on-behalf <target> approve --approver <user-id>` and re-run, or open egress for the overlay with `t3 <overlay> config_setting set on_behalf_post_mode immediate --overlay <name>`. Raising the `autonomy` tier removes the per-MR ask, not this gate.

### Concluding a no-postable-action external review — `mark_review_no_action` (Mandatory, #1077)

When the external PR under review is one where the correct outcome is **post nothing and approve nothing** — a bot MR (Aikido / Dependabot / Renovate), an auto-generated bump there is no diff worth commenting on, an MR you are not the right reviewer for and have no finding on — the reviewing Task still has to reach a terminal state. An FSM-owned review drives `review()` by completing its reviewing task; an external review that **approves** drives `t3 review approve`. The no-action outcome has its own terminal transition — without it the reviewing Task re-dispatches every Stop-hook pump forever (the PR never gets a forge reviewer assignment, so neither the dedup nor the orphan sweep can ever fire):

```bash
t3 <overlay> ticket transition <ticket_id> mark_review_no_action
```

This records `last_review_state = reviewed_no_action` (deliberately **not** `approved`, so a later genuine review is never suppressed) at the current head SHA and consumes the PENDING reviewing task. If a new revision is pushed (head SHA moves) the recorded state is dropped and the PR is reviewed again — concluding "no action" now never costs a future obligation. Maker≠checker is preserved: the reviewer sub-agent runs its own dispatch and invokes this itself; it is not a self-approval.

Use this **only** when there is genuinely nothing to post or approve. If you have a finding, post it (`t3 review post-comment` — default draft, #1207); if the verdict is approve, use `t3 review approve`. `mark_review_no_action` is the third, distinct outcome — not a shortcut to skip a review you should have done.

## Commands

| Command | When to use |
|---------|-------------|
| `t3 ci quality-check` | Quality analysis for self-review |
| `t3 <overlay> run tests` | Verification after review changes |
