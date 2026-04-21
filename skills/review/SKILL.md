---
name: review
description: Code review — self-review before finalization, giving review, receiving review feedback. Use when user says "review", "check the code", "feedback", "review comments", "quality check", or is in a review cycle.
compatibility: macOS/Linux, git, testing tools for verification.
requires:
  - workspace
  - platforms
  - code
companions:
  - requesting-code-review
  - verification-before-completion
triggers:
  priority: 40
  keywords:
    - '\b(review|check the code|check my code|feedback|quality check|code review)\b'
search_hints:
  - review
  - feedback
  - check the code
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Code Review

## Delegation

This skill delegates the generic review doctrine to:

- `requesting-code-review` — when to request an independent review pass
- `verification-before-completion` — proof before any “review-ready” claim

These are optional companion skills from [obra/superpowers](https://github.com/obra/superpowers). If not installed, this skill still works — you just won't get the external review and verification guidelines. TeaTree keeps the platform-specific review workflow locally: MR discussion handling, inline draft-note rules, and repo policy checks.

Both self-review and external review cycles.

## Dependencies

- **workspace** (required) — provides environment context. **Load `/t3:workspace` now** if not already loaded.
- **Framework/language convention skills** (when reviewing backend code) — e.g., Django conventions, Python style guides. TeaTree auto-detects the relevant `ac-*` skill from the repo shape. **If the loader didn't fire**, self-load the appropriate coding skill: `/ac-python` for Python code, `/ac-django` for Django projects.

## Workflows

### Self-Review Before Finalization

**Review ALL diverging code**, not just the last commit:

```bash
git diff --merge-base main
```

Cleanup checklist:

- [ ] No code duplication introduced
- [ ] No dead code left behind
- [ ] **Routing reachability:** every modified component is reachable via the target flow's route tree. Read the relevant `routes.ts` and confirm the component (or its parent shell) appears there. If the component lives in a flow-specific folder (e.g., `natural-person-calculation/`), verify the target flow actually routes through it.
- [ ] Naming follows project conventions
- [ ] Patterns match existing codebase
- [ ] No debug/temporary code remaining

#### Active Verification Against Repo Rules (Non-Negotiable)

After the cleanup checklist, **actively verify each changed file against the repo's agent config files** (`AGENTS.md` or the repo's equivalent agent instructions file) — not as a passive reminder, but as a file-by-file gate:

1. **Read** the repo's agent config files (e.g., `AGENTS.md` or the repo's equivalent agent instructions file).
2. **For each changed file**, check against every applicable rule section. Focus on:

- Architectural patterns (e.g., container-presentational, signals-first, inject vs constructor)
- Feature flag and multi-tenant rules (see [`references/multi-tenant-development.md`](../t3:code/references/multi-tenant-development.md) § Review Checklist)
- Banned patterns (e.g., manual `.subscribe()`, `any` types, hardcoded strings)

3. **Check consistency across the changeset** — if the same pattern is applied differently in two files within the same MR, that's a finding.
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
3. **When a threshold is crossed**, either refactor to comply or create a ticket for the debt — do not suppress the lint rule.
4. **Check `pyproject.toml` per-file-ignores** for the touched files. If any suppress complexity rules that are not in the project's boilerplate baseline, flag them as findings.

This step prevents architectural drift. Each diff looks fine in isolation — this check catches the cumulative effect by examining the full module.

#### New-Test Shape Check (Non-Negotiable)

When the diff adds or modifies test files, verify the new tests follow the repo's test-writing doctrine (see the repo's `AGENTS.md` § "Test-Writing Doctrine" — teatree and every overlay repo carry the same rule):

1. **Mock density.** If a new test file is mostly `Mock()`, `patch()`, `MagicMock`, or `mock.call_args` assertions, flag it. Ask: could this have been a Django test client call, a `call_command` invocation, a real `tmp_path` git repo, or a Playwright E2E?
2. **Mock targets.** Mocks should hit unstoppable externals only — network (GitHub, GitLab, Slack, Sentry), clock, `pass`, third-party subprocesses. Mocking teatree code, Django models, filesystem under `tmp_path`, or `git` itself is a finding.
3. **Missing integration coverage.** If the diff adds a view, a management command, or a new CLI surface and only ships unit tests, flag it — the happy path belongs in an integration test.
4. **Coverage preservation.** Any test rebalancing (removing units, adding integration) must keep the coverage gate satisfied. Report the before/after coverage number in the review.

Accept a mock-heavy test only when the MR description justifies why a higher-level test couldn't cover the same behavior (e.g., a rare error branch that's painful to trigger through the real entry point).

### Quality Gate Verification (Verify-Fix-Repeat)

Before declaring review-ready, run all gates and **iterate until they pass**. Do not declare review-ready after a single pass — re-run gates after every fix, because fixes can introduce new failures.

```text
Run gates → Any failure? → Fix → Re-run gates → Repeat until clean
```

**Gates (run in order):**

1. **Lint:** zero errors from the project linter
2. **Type check:** passes (if the project uses it)
3. **Tests:** full suite green (use `t3 run tests` or project equivalent)
4. **No uncommitted changes:** all fixes staged and committed
5. **No regressions:** diff review confirms no unintended changes

**Iteration limit:** After 3 fix-verify cycles without convergence, **stop and ask the user** — the issue may be systemic rather than incremental.

**Stop hook integration:** If the repo has a Stop hook (in the agent's settings), it enforces this loop automatically. Without a hook, run the gates manually before claiming done.

**References:** [Ralph Loop](https://github.com/snarktank/ralph) (external verification over self-assessed completion), [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) (Anthropic, feature-list-driven incremental verification).

### Giving Code Review

**Pre-flight gate — complete BEFORE reading any diff:**

1. Determine own vs external MR (Step -1)
2. Fetch ticket context for every MR (Step 0) — without this you cannot judge correctness
3. List all commits per MR (Step 0b)
4. Read the repo's `AGENTS.md` / agent instructions file and any project-specific coding guidelines

Do NOT skip these steps to "save time" when reviewing multiple MRs. Each step exists because skipping it caused missed findings in real reviews.

**Step -1 — Own MR vs External MR:**

When the MR under review belongs to the **user themselves**, do NOT post review comments. Instead, **implement the fixes directly** on the branch — commit and push. Present findings to the user as a summary of what you fixed, not as review comments to post. The user is asking you to take over and improve their code, not to leave notes for themselves.

**Step 0 — Gather Ticket Context:**

Before reading any code, fetch the referenced ticket/issue to understand the *intended* behavior:

1. Extract the ticket URL or number from the MR title/description.
2. Fetch the issue via the project's issue tracker CLI (e.g., `glab issue view`, `gh issue view`).
3. If external requirements links are referenced, fetch those too.
4. Use the ticket context as the ground truth for evaluating correctness.

Without ticket context you cannot judge whether the implementation is correct — only whether it compiles.

**Step 0b — Review All Commits, Not Just the Final Diff:**

The combined diff can hide mistakes. Always check individual commits:

1. List all MR commits (e.g., `glab api .../merge_requests/<IID>/commits`).
2. Inspect each commit's diff individually — a later commit may accidentally revert an earlier fix.
3. Look for "Tests fix" / "Fix tests" follow-up commits that change production code alongside test adjustments.

**Step 0c — Discuss Before Posting:**

Present ALL findings to the user before posting any comments. Never silently drop findings between the discussion phase and the posting phase — if a finding was discussed, it gets posted unless the user explicitly removes it. The user curates; you surface.

When raising concerns about caching, stale data, or side effects: **investigate first**. Check the actual code paths and real data before speculating. A concern backed by evidence ("I checked the DB — durations do vary") is useful; a speculative "this might be a problem" wastes the author's time.

**Step 1 — Structured Review Checklist:**

1. **Correctness** — does the code do what the ticket requires? Are all acceptance criteria met? When a change tightens a public contract (e.g., serializer field becomes required, API parameter becomes mandatory), trace all callers — the change affects every flow that uses that interface, not just the one the ticket describes.
2. **Completeness** — are there missing production code changes that the tests assume? Do test expectation changes have matching implementation changes?
3. **Feature flag** — follow the review checklist in [`references/multi-tenant-development.md`](../t3:code/references/multi-tenant-development.md). **Before raising a "missing feature flag" finding, trace the full gating chain upward** — the component under review may not have a flag itself but could be hidden/disabled at the container or routing level (e.g., `hidden: !featureFlagService.hasFeatureFlag(...)` in the parent that renders it). A finding is only valid if the feature is reachable without the flag.
4. **Style** — follows project conventions?
5. **Tests** — adequate coverage of new behavior?
6. **Safety** — no security issues, no data loss risks?
7. **Migrations** — reversible? data-safe? performance-safe?
8. **Scope** — are unrelated changes bundled in? Flag only if genuinely unrelated; small related fixes alongside the main change are normal practice.
9. **MR metadata** — title and description comply with the overlay's commit message format? If the overlay provides `validate_pr()`, run it programmatically rather than checking by eye.

**Step 2 — Review Tone & Formatting:**

Follow the [Google Engineering Practices — Code Review Standard](https://google.github.io/eng-practices/review/reviewer/standard.html): approve if the CL improves overall code health, even if it isn't perfect. Don't block on style preferences or theoretical improvements. The bar is "does this improve the codebase?" — not "is this how I would have written it?"

Comments are posted under the user's name. They must sound like a **real human colleague** wrote them — not an AI, not a linter, not a manager.

**Voice & attitude:**

- **Be the best colleague.** Helpful, curious, humble. Happy to teach, never to humiliate. You're a peer who genuinely wants the code (and the author) to succeed.
- **Never parent.** Don't lecture, don't explain things the author obviously knows. If you're providing context, frame it as "in case it helps" or "I think this might…" — not "you should be aware that…".
- **Be collegial.** Phrase observations as questions or suggestions, not orders. "Would it make sense to…?" beats "You must…".
- **Assume good intent.** A reverted line is more likely an accidental rebase artifact than carelessness. Frame it that way.
- **Acknowledge what's good.** If the approach is sound, say so briefly before raising issues.
- **Scale severity to impact.** A missing production code change that breaks tests is critical. A minor style nit is not. Don't escalate small things.
- **Never demand separate tickets/MRs** for minor scope additions. A small related fix alongside the main change is normal — only raise scope if genuinely orthogonal work is smuggled in.

**Formatting rules:**

- **Prefix nits.** When a comment is nitpicking (style, naming, minor preference), prefix with `Nit:` so the author knows it's non-blocking.
- **Backticks for code.** Always wrap code symbols, class names, method names, variable names, file paths, and CLI commands in backticks (`` ` ``).
- **Use suggestion blocks for concrete code changes.** When you have a specific replacement in mind, use the platform's suggestion feature (` ```suggestion ` fenced block on both GitLab and GitHub) so the author can accept with one click. GitLab supports `:-N+M` to expand the range. Combine explanation text **before** the suggestion block.
- **Readable structure for longer comments.** Use empty lines to separate distinct sections (problem, suggestion, example). Within a section, use line breaks between sentences (without empty lines) to keep things scannable. Short comments stay on one line — don't over-structure a one-liner.
- **No walls of text.** If a comment needs more than ~5 lines, break it up visually. Paragraphs, not monoliths.

**Step 3 — Post Draft Review Comments:**

**Always use draft notes** (or the platform's equivalent "pending review" feature), not direct/immediate comments. Draft notes are only visible to the reviewer until explicitly submitted — this lets the user review, edit, and submit all comments as a batch.

**Pre-flight: read existing comments (Non-Negotiable).** Before posting any new comments, fetch all existing discussions and notes on the MR (from all authors, not just the current user):

1. **List all discussions** via `GET .../merge_requests/<IID>/discussions?per_page=100` and read each note's `body`.
2. **For each finding**, check whether an existing comment already raises the same concern — same file, same line range, same substance. If so, **do not post a duplicate**.
3. **If you have something to add** to an existing discussion (additional context, a related concern on the same code), **reply in that thread** instead of creating a new top-level comment. Use the Reply to Discussion recipe from the platform reference.
4. **Only post new draft notes** for findings not already covered by existing comments.

This prevents noise from multiple review passes or multiple reviewers covering the same ground.

**Post all *new* findings.** Don't self-censor or hold back comments because they seem minor. The user will review every draft note in the platform's UI, edit wording, and delete anything they don't want before submitting. Your job is to surface everything you noticed — the user curates. But "everything" means everything *not already said* — duplicating an existing comment wastes the author's time.

When reviewing an external MR/PR, **always post comments inline on the correct file and line** in the diff view. For comments that aren't tied to a specific line (e.g., description feedback), post a general note without position data.

**Extend the CLI, never inline API recipes.** If a `t3 review` operation is missing (e.g., bulk-publish, reply, resolve), implement it in `src/teatree/cli/review.py` — do NOT document a raw API snippet or inline script here. Skills describe what command to run, not how to replicate missing CLI functionality.

**Use `t3 review post-draft-note` (Mandatory).** It handles token extraction, diff refs, position serialization, and added-line validation. Never use raw API calls.

```bash
t3 review post-draft-note <REPO> <MR_IID> "Comment text" --file <path/to/file> --line <line_number>
```

**Pre-flight: verify target line is an added line.** Before posting each inline note, confirm the target `new_line` corresponds to an added (`+`) or modified line in the diff — never a context (unchanged) line. Targeting a context line causes GitLab to render the comment in **every hunk** that references that line, creating duplicates. When the finding is about an unchanged line, target the nearest added line and reference the unchanged line in the comment text instead.

**Post-flight: verify response.** Response must confirm the comment landed on the correct file/line — if position data is missing in the response, the comment landed as a general comment (wrong). After posting all notes, list them via the API and confirm the count and positions match expectations.

**Do NOT submit the review without explicit user instruction.** By default, the user reviews draft notes in the platform's UI, edits if needed, and submits manually. If the user explicitly asks to publish (e.g., "post with t3 cli", "submit the review"), use:

```bash
t3 review publish-draft-notes <REPO> <MR_IID>
```

**`WARNING: inline position was not accepted`** means GitLab did not store the `position` data — the note will render as a general comment, not inline on the diff. Check that `--file` matches a path in the MR diff and `--line` is within the changed range.

**If `t3 review delete-draft-note` returns 404** — the draft was already submitted (published to regular notes) by the user from the GitLab UI. Use `DELETE projects/{encoded_repo}/merge_requests/{iid}/notes/{note_id}` via the regular notes endpoint instead.

#### Position field reference

| Field | GitLab | GitHub |
|---|---|---|
| File path | `old_path` / `new_path` | `path` |
| New line (added/modified) | `new_line` | `line` + `side=RIGHT` |
| Old line (deleted) | `old_line` | `line` + `side=LEFT` |

### Receiving Code Review

- **User feedback** = trusted direction. Verify scope, then implement.
- **External reviewer** = verify technically before implementing.
- **Push back when:** suggestion breaks functionality (show evidence), violates YAGNI, is based on stale context, or conflicts with user's stated architecture.
- **Anti-performative:** No "You're absolutely right!" — just state the fix or the technical disagreement.
- **Technical rigor:** verify reviewer suggestions against the actual codebase before implementing.

#### Replying to Review Discussions

When posting replies to reviewer discussions (e.g., "Done in `<commit>`"):

1. **Fetch all discussions via API** and inspect each one's first note — read the actual body, don't rely on assumptions about which discussion covers which topic.
2. **Match reply to the specific concern.** Read each discussion's first note body in full. The reply must use the same framing as the reviewer — if they asked about `FeatureFlagService`, don't reply about `takeUntilDestroyed`. Never post a generic "addressed in commit X" reply to a discussion about a different topic.
3. **Skip already-answered discussions.** If the user (or someone else) already replied with a resolution, do not post a duplicate reply.
4. **Present the mapping to the user before posting.** Show a table: `| Discussion | Topic | Reply |` and get confirmation. Never batch-post replies without review.

See your [issue tracker platform reference](../t3:platforms/references/) § "Reply to Discussion" for the API recipe.

## Commands

| Command | When to use |
|---------|-------------|
| `t3 ci quality-check` | Quality analysis for self-review |
| `t3 <overlay> run tests` | Verification after review changes |
