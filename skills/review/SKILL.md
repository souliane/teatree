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
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Code Review

## Delegation

This skill delegates the generic review doctrine to:

- `requesting-code-review` — when to request an independent review pass
- `verification-before-completion` — proof before any “review-ready” claim

Optional [obra/superpowers](https://github.com/obra/superpowers) companions provide generic methodology. TeaTree keeps the project-specific workflow locally.

Both self-review and external review cycles.

## Dependencies

- **workspace** (required) — provides environment context. **Load `/t3:workspace` now** if not already loaded.
- **Framework/language convention skills** (when reviewing backend code) — e.g., Django conventions, Python style guides. TeaTree auto-detects the relevant `ac-*` skill from the repo shape. **If the loader didn't fire**, self-load the appropriate coding skill: `/ac-python` for Python code, `/ac-django` for Django projects.
- **Overlay review skill set** (when reviewing an overlay repo) — the active overlay declares its full reviewer skill set via `OverlayBase.get_review_companion_skills()`, which returns `[pr_review_companion, *companion_skills]`: the overlay's review-quality bar plus its standing companions (the overlay workspace playbook skill and the project dev skills). When the repo under review is an overlay repo, **derive that set and self-load every skill in it immediately — before asking for the MR URL, before fetching ticket context, before reading any diff**. Skill loading is unconditional and comes before clarifying questions; do not wait to be told the names.

## Workflows

### North-Star Rubric — Six Quality Attributes

Everything you write and everything you review aims at six attributes. Treat them as the lens for both self-review and giving review:

- **Clean** — readable, no dead code or duplication, names that say what they hold.
- **Robust** — survives the real failure case, not only the favorable one; edge cases handled, inputs validated.
- **Maintainable** — the next reader can change it safely; structure documents itself.
- **Coherent** — fits the surrounding patterns and stays consistent across the whole changeset. Coherence includes **cross-repo coherence** (a referenced artifact — a skill name, a CLI command, a sibling-repo path — must actually exist where it's referenced) and **wired-and-exercised** (a mechanism must actually fire — a hook that's defined but never invoked, or a gate that's declared but never reached, is incoherent even if it reads correctly).
- **Reliable** — does what it claims under repeated and concurrent use; no flaky or order-dependent behavior.
- **Proactive** — sweeps the class, not just the instance; when a fix reveals a broader pattern, address the pattern rather than the single symptom.

### Spawn the t3:reviewer Sub-Agent Before Pushing (Non-Negotiable)

**Self-review by the implementing conversation never satisfies the shipping gate's `reviewing` phase.** The implementer's context carries every "looks done" blind spot that allowed the gap in the first place — that is exactly what produced souliane/teatree#545's six rounds of follow-up review fixes (missed renames, broken tests, undocumented contract changes, bypassed FSM). The corrective is an independent sub-agent that hasn't seen the implementation conversation.

**The only sanctioned path** to advancing a ticket from `TESTED → REVIEWED` is:

1. Spawn the `t3:reviewer` sub-agent from the main conversation via the `Agent` tool. The full Agent invocation snippet, FSM transition mechanics, and "drive transitions, not visit phases" rules live in [`../ship/SKILL.md`](../ship/SKILL.md) § "Review Gate" — that section is the source of truth.
2. Apply every finding the sub-agent surfaces. Reviewer agents are read-only; the implementing conversation owns the edits.
3. Drive the FSM `review` transition by completing the reviewing task (auto-fires `ticket.review()` and keeps the task ledger clean). **Never** use `t3 <overlay> lifecycle visit-phase reviewing` to *skip* the independent reviewer — since #694 the shipping gate reconciles `Ticket.state` from `Session.visited_phases`, so a manual visit *will* unblock `pr create`; that is precisely why recording `reviewing` without an independent review having happened defeats the gate. Earn the phase first, then record it.

When `review_skill` (env `T3_REVIEW_SKILL`) is configured, the reviewing-phase evidence gate (#1539) hardens this further: `lifecycle visit-phase <id> reviewing` refuses unless a `review_skill_run` artifact attests the configured skill ran. After running the skill, stamp the evidence with `t3 <overlay> lifecycle record-review-skill-run <id> <skill>`, then record the phase. With `review_skill` unset the gate is a NO-OP (opt-in default).

Reviewing carries the same responsibility as implementing, so deep retrieval is a **constraint, not a rule**: when `require_review_context` is set, the FSM `→ reviewing` transition (`teatree.core.gates.review_context_gate`) mechanically refuses until the work item is fetched from its source (Notion / GitLab — follow the MR description's links), every referenced document is downloaded + read, and the implementation is analyzed against them — stamp it with `t3 <overlay> lifecycle record-review-context <id> --work-item <url> --documents <urls> --analysis <how-checked>`. A diff-only verdict cannot enter `reviewing`.

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
3. **When a threshold is crossed**, either refactor to comply or create a ticket for the debt — do not suppress the lint rule.
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
6. **Skill references resolve:** run `t3 tool validate-skill-refs`. Every skill *name* referenced — the `~/.teatree-skills.yml` keyword→skill routing config and the `agents/*.md` frontmatter `skills:` / `companion_skills:` lists — must resolve to a real skill in the canonical (installed/remote) skill set. A dangling name (the real `ac-reviewing-skills` → `ac-reviewing-codebase` case) exits non-zero with file:line, the bad name, and the nearest valid matches. The repo's own agent refs are also gated in pre-commit (`validate-skill-refs`); this command additionally covers the personal `~/.teatree-skills.yml`, which lives outside the repo.

**Iteration limit:** After 3 fix-verify cycles without convergence, **stop and ask the user** — the issue may be systemic rather than incremental.

**Stop hook integration:** If the repo has a Stop hook (in the agent's settings), it enforces this loop automatically. Without a hook, run the gates manually before claiming done.

**References:** [Ralph Loop](https://github.com/snarktank/ralph) (external verification over self-assessed completion), [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) (Anthropic, feature-list-driven incremental verification).

### Giving Code Review

**Pre-flight gate — complete BEFORE reading any diff:**

1. Determine own vs external PR (Step -1)
2. Fetch ticket context for every PR (Step 0) — without this you cannot judge correctness
3. List all commits per PR (Step 0b)
4. Read the repo's `AGENTS.md` / agent instructions file and any project-specific coding guidelines

Do NOT skip these steps to "save time" when reviewing multiple PRs. Each step exists because skipping it caused missed findings in real reviews.

**BINDING — never review an MR/PR already :eyes:-claimed by a colleague.** Do NOT dispatch or perform a review of any MR/PR whose review-broadcast / review-request message already carries a `:eyes:` (👀) reaction from someone other than the user — that reaction is the colleague's claim on the review, and a second pass duplicates their in-flight work. The only override is the user explicitly naming that MR (an `<@user_slack_id>` mention on the broadcast, or a direct instruction). This is enforced structurally in `SlackBroadcastsScanner` (`src/teatree/loop/scanners/slack_broadcasts.py`) via `eyes_reacted_by_other` (`src/teatree/core/review_candidate.py`), which excludes the user's own `:eyes:` so the gate only fires on a colleague's claim. When reviewing manually, check the broadcast's reactions first and skip a colleague-claimed MR unless the user named it.

#### Colleague-MR Autonomy — Act on the Verdict, Don't Ask (config-driven)

What the agent does *after* an independent cold-review verdict exists on a **colleague-authored** MR (the MR's author is not your identity) is governed by **one config knob**, the per-overlay `autonomy` switch (`src/teatree/config.py`; tiers `full > notify > babysit`, see [`docs/blueprint/configuration.md`](../../docs/blueprint/configuration.md) § 10.1). Read the resolved tier with `t3 <overlay> autonomy show` and set it with `t3 <overlay> autonomy set <level>` (`--global` for the workspace default) — never hand-edit `~/.teatree.toml`. It is *not* a per-MR judgement call and *not* a personal memory rule — read the resolved tier and follow it.

**Autonomous tiers (`autonomy = "full"` or `"notify"`, which collapse `on_behalf_post_mode → immediate`):** once an independent cold-review verdict exists, act directly — no draft-default, no "say the word", no per-MR ask:

- **Merge-safe verdict** → post the terse verdict / nits live (`t3 review post-comment --live`) **and** `t3 review approve`.
- **Nits only** → post them directly (`t3 review post-comment --live`); approve per the merge-safe rule above.
- **A blocking finding** → post it (`t3 review post-comment --live`); do **not** approve.

`notify` additionally DMs the user after each on-behalf post (derived `notify_on_behalf`); `full` posts without the after-the-fact DM. The autonomy collapse relaxes exactly the three gates in `_AUTONOMY_COLLAPSED_GATE_VALUES` (`on_behalf_post_mode → immediate`, `require_human_approval_to_merge → False`, `require_human_approval_to_answer → False`), so the on-behalf pre-gate no longer refuses unattended.

**Live posts still need a token, even under `full`.** The `--live` colleague-visible publish is gated by the #1207 single-use `LivePostApproval` token (`teatree.core.gates.live_post_gate.require_live_post_approval`), which `check_live_post` enforces **orthogonally to `on_behalf_post_mode` and to `autonomy`** — it is *not* in the collapsed-gate set, so `post-comment --live` is refused with no token regardless of tier. Under an autonomous tier, mint the token in the same one step that records the on-behalf authorization — `t3 review authorize <repo>!<mr> --approver <user-id>` (#126) — then post live; or post the verdict as a **draft note** (`t3 review post-comment`, the default), which needs no token. Either path keeps the autonomous "act on the verdict, don't ask the user per-MR" posture; the token is a single-use idempotency/audit seal on the outward publish, not a per-MR user decision.

**Babysit tier (`autonomy = "babysit"`, the conservative default):** keep the draft-and-ask flow — drafts publish autonomously, every live post / approval waits for the user (Step 3 below; `t3 review authorize`). This is the right setting for client / shared-team overlays.

**The quality floor is identical under every tier and is never relaxed by this knob:** the verdict must come from an *independent* cold reviewer (maker ≠ checker — never self-approve your own MR), findings are verified against ground truth before posting (a Blocker you cannot falsify is posted as a question, not a Blocker), `t3 review approve` keeps its review-first precondition (no approval without a prior reviewing footprint), and CI must be green. The knob decides *whether to ask the user*, never *whether the work is correct*.

**Verifying a colleague's finding before posting (and retracting if it was wrong):** before a finding goes out under the user's name on a colleague's MR, confirm it against ground truth — the real code, live data or the DB, and the domain conventions — not against your own mental model of how the code probably behaves. A *recheck* is an independent re-derivation that tries to **falsify** the finding (re-grep, re-query, re-read the producer's schema), never a re-read of your own earlier note — re-reading your note only re-confirms the mistake that produced it. If the verification cannot pull the finding to certainty, post it as a question, not a Blocker. And if a finding you already posted turns out to be wrong, retract **all** affected findings at once and quickly: a stale false Blocker sitting on a colleague's correct MR reads as the user not understanding the code, and the longer it stays the more it costs the working relationship.

**Step -1 — Own PR vs External PR:**

When the PR under review belongs to the **user themselves**, do NOT post review comments. Instead, **implement the fixes directly** on the branch — commit and push. Present findings to the user as a summary of what you fixed, not as review comments to post. The user is asking you to take over and improve their code, not to leave notes for themselves.

**Step 0 — Gather Ticket Context:**

Before reading any code, fetch the referenced ticket/issue to understand the *intended* behavior:

1. Extract the ticket URL or number from the PR title/description.
2. Fetch the issue via the project's issue tracker CLI (e.g., `glab issue view`, `gh issue view`).
3. **Fetch every attached spec** (PDFs, OpenAPI files, vendor docs) and every linked external requirement. For GitLab attachments, the working path is `glab api projects/<id>/uploads/<secret>/<filename>` — browser-style URLs (`gitlab.com/<group>/<repo>/uploads/...`, `gitlab.com/-/project/<id>/uploads/...`) require session cookies and return login HTML when hit with a PAT. Attachments are the authoritative spec; an author docstring summarising them is not a substitute.

   A posted image or screenshot is two layers, and a fetched image is read in two passes. The first pass is the raw capture — what the tool, page, or table actually shows. The second pass is the poster's overlay drawn on top: borders and boxes, arrows, circles, highlights and colour, underlines, callout text, numbering, redaction. Those marks are a deliberate hint pointing at exactly what the poster wants seen; reading only the raw content answers a question the poster did not ask. For each annotation, ask "why did they mark exactly this" and resolve it concretely: a boxed cell is the load-bearing value the argument turns on; an arrow is an A→B link being asserted; bordered individual letters spell an acronym — decode it; a circled token is the disputed item; added callout text is the poster's claim restated. When an annotation's meaning is non-obvious, decoding it is required investigation, not optional — an unresolved mark is an unread part of the spec, treated the same as an attachment that did not download.
4. If external requirements links are referenced, fetch those too.
5. Use the ticket context + attachments as the ground truth for evaluating correctness.

**Hard rule — refuse blind reviews.** If a ticket references a spec attachment or external requirements document that you cannot retrieve, **STOP**. Do not post review notes. Report back to the user: which document you couldn't fetch, what you tried, and what permission / access / exception is needed. Overlay skills MAY declare specific sources as out-of-scope (partner portals behind SSO the sandbox cannot reach, for example); honour those per-overlay exceptions. For anything else, a review with missing spec context is not a review — it's guessing, and guessing attached to the user's account damages the author's trust.

Without ticket context you cannot judge whether the implementation is correct — only whether it compiles.

**Step 0b — Review All Commits, Not Just the Final Diff:**

The combined diff can hide mistakes. Always check individual commits:

1. List all PR commits (e.g., `glab api .../merge_requests/<IID>/commits`).
2. Inspect each commit's diff individually — a later commit may accidentally revert an earlier fix.
3. Look for "Tests fix" / "Fix tests" follow-up commits that change production code alongside test adjustments.

**Step 0c — Discuss Before Posting:**

Present ALL findings to the user before posting any comments. Never silently drop findings between the discussion phase and the posting phase — if a finding was discussed, it gets posted unless the user explicitly removes it. The user curates; you surface.

When raising concerns about caching, stale data, or side effects: **investigate first**. Check the actual code paths and real data before speculating. A concern backed by evidence ("I checked the DB — durations do vary") is useful; a speculative "this might be a problem" wastes the author's time.

**Step 0d — Answer Your Own Questions Before Posting (Non-Negotiable):**

Every review comment is posted under the user's name. A comment that boils down to "I'm unsure, please confirm" makes the *user* look like they don't know their own codebase. Do not post it.

Before drafting any comment, if it would contain any of the following phrases — or their equivalents — **STOP and investigate first**:

- "worth confirming with the business that…"
- "worth checking `<file>` / `<function>` / the downstream serializer / etc."
- "can you confirm this value matches what upstream emits?"
- "is this string / identifier / enum value correct?"
- "does this field exist in the producer schema?"
- "I'm not sure whether…"
- "does this mean that… / or… / or…?" (listing options instead of picking one)
- "verify that …" / "please check …" / "confirm whether …" — any imperative that asks the author to do verification work the reviewer is capable of doing themselves.

**The reviewer does the verification, not the author.** If the comment names a file, function, schema, enum, downstream caller, or any other artifact reachable from the local checkout, **open it and read it before posting**. "Worth checking `foo.py`" is not a review comment — it is the reviewer outsourcing their job. Either the file says the code is wrong (post a verified finding) or it says the code is fine (post nothing).

Investigate first by exhausting the sources you **can** reach:

1. **Grep the repo** for the symbol / string / identifier — producers, consumers, enums, tests, fixtures, docs.
2. **Grep sibling repos** when the value crosses a service boundary (e.g., webhook producer → consumer, API schema → client). The upstream producer's source of truth lives there. Discover sibling repos via `T3_WORKSPACE_DIR` or the overlay's configured repo list — never hardcode a user-specific path.
3. **Read the producer's schema / enum / migration** — whichever repo emits the value. If it's a Django model, check the field's `choices=` and the migration history. If it's a Pydantic model, check the field type.
4. **Check commit history** for the rename, addition, or removal — `git log -S "<symbol>" --all --oneline` often shows exactly when and why the value changed.
5. **Read the test fixtures** — realistic test inputs show what the producer actually sends.
6. **Check related PRs** on the same or upstream repos for the same symbol — someone may have already merged or discussed it.

Only after all reachable sources are exhausted can you post a question-style comment — and only when the answer truly requires access you do not have (partner portal behind SSO, vendor-only documentation, product owner's desk knowledge). State what you checked and why the answer isn't reachable, so the author sees you did the work.

**Scale severity to confidence.** A speculative "maybe wrong?" is a nit at best; drop it. A verified finding ("grepped `foo-producer`, canonical spelling is `X`, branch has `Y` — will fail at runtime") is a blocker and belongs in the review.

**When the investigation confirms the code is correct, say nothing.** Silence on a check you performed is the correct outcome — not a "looks good, but…" comment. Positive comments belong in the summary to the user, not in the PR.

**Step 0e — Don't Police Other Authors' Title/Description Format (Non-Negotiable):**

Do NOT leave review comments about an external author's PR title format, description wording, commit-message style, work-item link spacing, or whether their description "reads better" in a different shape. These rules are enforced by CI and by the overlay's `validate_pr()` check — not by the reviewer. Raising them manually duplicates the bot and nags a colleague for something a machine already polices.

The reviewer's responsibility is to ensure **their own** PRs pass the title/description check. On other authors' PRs, silence on formatting is the correct outcome. If something is objectively wrong in a way that affects traceability or release notes (e.g., the title references the wrong ticket), frame it as a **correctness** finding, not as a style nit.

**Step 0f — Respect the Overlay's Auto-Close Policy (Non-Negotiable):**

Do NOT suggest adding `Closes #NNN`, `Fixes #NNN`, `Resolves #NNN`, or any other auto-close keyword to a PR description unless the active overlay's conventions explicitly require it. Many overlays manage issue closure via their own ticket/PR linking rather than via GitHub-style auto-close trailers, and suggesting them contradicts the overlay convention.

Check the overlay skill's commit-message and PR-description rules **before** proposing any trailer. The default when the overlay is silent on the topic is: do not suggest auto-close trailers.

**Step 0g — Cross-Service Verification (Non-Negotiable):**

A review of a service that talks to other services is incomplete until those other services have been checked. Reviewing one repo in isolation produces blind comments — the reviewer asserts "this is the convention" or "this default is fine" without knowing what the producers and consumers across the platform actually do. Comments built on that premise make the user look stupid when the author replies with "have you checked the FE / the gateway / the sibling microservice?".

**Before posting any comment about a name, contract, default value, schema field, response shape, or wire format, exhaust the cross-service grep:**

1. **Enumerate the related services** at the start of the review. From the PR's repo, list every service that produces or consumes the same data: upstream gateway, downstream consumers (frontend, sibling backend, document generation, data warehouse), shared schema/proto repos. Discover them via `T3_WORKSPACE_DIR` (or the overlay's configured repo list) — never hardcode a user-specific path. State the list explicitly so the user can correct gaps before you start.
2. **Grep every related service** for the symbol/string/identifier/field name. Frontend models, backend serializers, fixture files, generated docs, OpenAPI specs, migration histories. Note where each occurrence lives.
3. **Cite the cross-service evidence in the comment.** "Frontend has 18 references to `idExpirationDate` in `libs/shared/data-model/...`; the gateway-side Python repo has matching references in `report-generator/serializers/...`" is a finding. "I think this should be spelled differently" is a guess.
4. **When the cross-service check reveals the comment was wrong, drop the comment.** A comment that survives the check survives because the platform-wide convention contradicts the diff. Silence is the correct outcome on a check that confirmed the diff is fine.
5. **When the cross-service check is impossible** (a repo is not in scope, sandboxed, or behind access you don't have), say so explicitly in the comment and name what was checked vs not. Don't pretend you ran a check you didn't.

**Triggers for this step** — every diff touching:

- A schema field name, enum value, or wire format (Pydantic models, DRF serializers, TypeScript interfaces, OpenAPI definitions).
- A default value or boolean flag that previously had a different default (especially flags with always-on/always-off semantics like loyalty enrichment, feature gates, search filters).
- A response shape or wrapper type returned by an endpoint already consumed by another service.
- A query parameter name or required/optional toggle on a public endpoint.
- A renamed function, method, or class that is used cross-repo (gateway client, shared library, public CLI command).

If none of those triggers apply (purely internal refactor, test-only change, comment fix), this step is satisfied automatically.

**Failure mode this step prevents:** a reviewer posts "the canonical name should be X" based on the local repo's pattern, the author replies "the FE has 20 usages of Y, please check before commenting", and the user (whose name is on the comment) loses credibility for a finding that would have been correct if the reviewer had grepped the FE first.

**Step 0h — Discussion Venue: PR Over Chat (Non-Negotiable):**

Discussion topics that anchor to specific code in a PR — design questions about a function, a TODO in the diff, a missing call compared to a sibling endpoint, a hardcoded value, an architectural choice visible in the patch — belong on the **PR**, not in a Slack/Teams DM or chat thread. Default to PR notes whenever the topic references something the reviewer can point to in the diff.

**Why PR over chat:**

- PR notes are persistent, threaded per topic, and resolve with the PR. Chat scrolls away.
- Other reviewers and stakeholders see PR notes; chat is a private channel between two people.
- PR notes attach to the line/file, so the conversation stays anchored to the code that triggered it.
- The ticket's audit trail benefits from the discussion living next to the code change.

**When chat is the right venue:**

- A heads-up that the review is ready and points to the PR for the discussion ("left some thoughts on !351").
- Coordination/scheduling ("can we pair on the LE flow tomorrow?").
- Sensitive feedback that doesn't belong in a public review trail.
- Topics genuinely unrelated to the diff (e.g., process discussion about how the team reviews PRs).

**Inline first, general note second:**

When posting on the PR, prefer **inline** (line-anchored) discussions over **general** PR comments. Inline notes show the exact code that triggered the question and let the author resolve them per topic. Use a general PR comment only when the topic is not anchorable to a single line — for example, an architectural question that spans the whole file or a code block that is not part of the PR's diff (so GitLab cannot anchor an inline note to it).

**Failure mode this step prevents:** the reviewer drafts a Slack message containing 5 design questions about specific lines of a PR, sends it as a DM, and the discussion lives in chat where it is invisible to the rest of the team and disconnected from the code. The author then has to copy-paste the chat back into PR comments to track resolution. The right move was to post the topics as PR discussions in the first place and send a one-line Slack heads-up pointing to the PR.

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

Follow the [Google Engineering Practices — Code Review Standard](https://google.github.io/eng-practices/review/reviewer/standard.html): approve if the CL improves overall code health, even if it isn't perfect. Don't block on style preferences or theoretical improvements. The bar is "does this improve the codebase?" — not "is this how I would have written it?"

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

- **Single terse inline nit on a colleague MR.** On a colleague's MR (the MR's author is not your identity), the binding shape for an on-behalf review is **one terse `Nit:`-prefixed inline comment anchored on the file:line that motivated it** — never a multi-section Problem/Fix/Verification dump. Enforced structurally by the colleague-MR shape gate in `src/teatree/cli/review/shape_gate.py` (souliane/teatree#1114, loosened in #1159): the body is capped at 3 blank-line-separated paragraphs and 200 words; the gate refuses the post with steering text before any GitLab API call. Multi-sentence findings are fine — the cap targets abuse (multi-section dumps), not legitimate ≤3-sentence nits. Own-MR reviews are exempt (long-form self-review summaries are fine).
- **Prefix nits.** When a comment is nitpicking (style, naming, minor preference), prefix with `Nit:` so the author knows it's non-blocking.
- **Backticks for code.** Always wrap code symbols, class names, method names, variable names, file paths, and CLI commands in backticks (`` ` ``).
- **Use suggestion blocks for concrete code changes.** When you have a specific replacement in mind, use the platform's suggestion feature (` ```suggestion ` fenced block on both GitLab and GitHub) so the author can accept with one click. GitLab supports `:-N+M` to expand the range. Combine explanation text **before** the suggestion block.
- **Readable structure for longer comments.** Use empty lines to separate distinct sections (problem, suggestion, example). Within a section, use line breaks between sentences (without empty lines) to keep things scannable. Short comments stay on one line — don't over-structure a one-liner.
- **No walls of text.** If a comment needs more than ~5 lines, break it up visually. Paragraphs, not monoliths.

**Author-Marked TODO/FIXME — Never a Blocker (Non-Negotiable):**

A `// TODO`, `# TODO`, `/* TODO */`, `// FIXME`, `# FIXME`, `// XXX`, `# XXX`, `// HACK`, `# HACK` marker on an added line — or the phrases "not in this MR", "follow-up", "deferred", "implement later", "out of scope" — is the **author explicitly documenting that the work is deferred**. NEVER post a blocker-shaped (REQUEST_CHANGES) comment anchored to (or within ±3 lines of) such a marker. The strongest verdict allowed is a non-blocker comment, and only when it adds genuinely new context (e.g. "tracked at [#NNN]") — not re-stating what the author already said.

`t3 review post-comment` and `post-draft-note` enforce this deterministically via `src/teatree/cli/review/todo_gate.py` (souliane/teatree#1186): a blocker-shaped body anchored on a TODO-adjacent line is REFUSED with a clear error before any GitLab API call. If you genuinely believe the TODO must be addressed in THIS MR (rare — the author knows their scope), STOP and surface to the user — never post on their identity.

Failure mode this prevents: re-asking a colleague to do work they have explicitly deferred makes the reviewer (and the user, whose identity posts on-behalf) look unable to read code.

**Step 3 — Post Draft Review Comments (babysit tier):**

This step is the **`autonomy = "babysit"`** flow (the conservative default). Under an autonomous tier (`full` / `notify`), follow "Colleague-MR Autonomy — Act on the Verdict, Don't Ask" above instead: post the verdict / nits live and approve per the merge-safe rule, no draft-and-ask round-trip.

**Under babysit, always use draft notes** (or the platform's equivalent "pending review" feature), not direct/immediate comments. Draft notes are only visible to the reviewer until explicitly submitted — this lets the user review, edit, and submit all comments as a batch.

**Pre-flight: read existing comments (Non-Negotiable).** Before posting any new comments, fetch all existing discussions and notes on the PR (from all authors, not just the current user):

1. **List all discussions** via `GET .../merge_requests/<IID>/discussions?per_page=100` and read each note's `body`.
2. **For each finding**, check whether an existing comment already raises the same concern — same file, same line range, same substance. If so, **do not post a duplicate**.
3. **If you have something to add** to an existing discussion (additional context, a related concern on the same code), **reply in that thread** via `t3 review reply-to-discussion <REPO> <MR_IID> <DISCUSSION_ID> "body"` instead of creating a new top-level comment.
4. **Only post new draft notes** for findings not already covered by existing comments.

This prevents noise from multiple review passes or multiple reviewers covering the same ground.

**Post all *new* findings.** Don't self-censor or hold back comments because they seem minor. The user will review every draft note in the platform's UI, edit wording, and delete anything they don't want before submitting. Your job is to surface everything you noticed — the user curates. But "everything" means everything *not already said* — duplicating an existing comment wastes the author's time.

When reviewing an external MR/PR, **always post comments inline on the correct file and line** in the diff view. For comments that aren't tied to a specific line (e.g., description feedback), post a general note without position data.

**Extend the CLI, never inline API recipes.** If a `t3 review` operation is missing, implement it in `src/teatree/cli/review/service.py` — do NOT document a raw API snippet or inline script here. Skills describe what command to run, not how to replicate missing CLI functionality. Current subcommands: `run`, `post-comment`, `authorize`, `approve-live-post`, `delete-draft-note`, `delete-discussion`, `publish-draft-notes`, `list-draft-notes`, `update-note`, `reply-to-discussion`, `resolve-discussion`, `approve`, `unapprove`. (`post-draft-note` is deprecated — see below.)

**Read-only review-shape audit — `t3 review run <MR_URL>` (#1206).** Run before manually scanning the diff: the CLI emits a JSON summary (`changes.{files,additions,deletions}`, `complexity`, `existing_review.{open_discussions,draft_notes,approvals}`, `findings_catalog`, `verdict`) so every reviewer sub-agent starts from the same shape instead of improvising. The command never publishes; it just gathers what the reviewer needs to decide what to post via `post-comment` / `post-draft-note`. GitHub PR URLs return `unsupported_forge` (exit 2) deterministically — no masquerading success.

**Default-safe `t3 review post-comment` (Mandatory, #1207).** The subcommand creates a DRAFT by default and DMs the user the link — the CLI itself enforces the draft-by-default rule, so no separate prose check is required. To publish live (colleague-visible), authorize the MR in **one step** with `t3 review authorize <repo>!<mr> --approver <user-id>` (records the durable on-behalf authorization AND mints the single-use live-post token), then the agent re-runs with `--live`. Without an authorization `--live` refuses without any GitLab side effect, naming the `authorize` command in the refusal. The earlier two-command dance (`approve-on-behalf` + `approve-live-post --from-on-behalf`) still works and remains for the Slack-ts verification path, but `authorize` is the one-step collapse (#126).

```bash
t3 review post-comment <REPO> <MR_IID> "Comment text" --file <path/to/file> --line <line_number>
```

The CLI validates the target line is an added (`+`) line in the MR diff before posting, and verifies the response anchored correctly (non-null `line_code`). When something goes wrong it refuses upfront — common rejected cases:

- **Context line:** the target is unchanged in the diff. CLI rejects and lists the nearby added lines so you can pick one.
- **File not in diff:** the file path isn't part of the MR. CLI rejects with the list of changed files.
- **Collapsed-diff file:** GitLab's draft-note anchoring fails on large files whose diff was collapsed server-side. CLI detects the null `line_code` after posting, deletes the broken draft, and suggests `post-comment` (below).

**Workaround for collapsed-diff files — `t3 review post-comment --live`.** When the file is too large for GitLab to anchor a draft, the post-flight anchor check refuses the draft. The historical workaround used the `/discussions` endpoint, which anchors even on collapsed diffs. Under #1207 that path requires a Slack-recorded approval — the user DMs an approval phrase ("post live" / "submit it" / "go ahead"), the agent records it via `t3 review approve-live-post <mr-url> --slack-ts <ts>`, and then re-runs:

```bash
t3 review post-comment <REPO> <MR_IID> "Comment text" --file <path/to/file> --line <line_number> --live
```

The `--live` post lands immediately instead of batching with a review. Reserve this for the cases where the default draft path explicitly errors with the collapsed-diff message AND the user has authorised the live post in Slack.

**Pre-flight: the file you anchor on MUST be the file the body discusses.** If the comment body describes code in `foo.py` (e.g., "`foo.py`'s `bar()` is missing X that the sibling `baz.py` got"), anchor the comment on `foo.py` — not on `baz.py`, even if `baz.py` has more added lines in the diff. Two defensible patterns when `foo.py` has no added lines:

1. Pick the nearest added line in `foo.py` (even a whitespace or adjacent-line change) and open the body with "Note on an unchanged line below:" so the reader sees the anchor is a stand-in.
2. Post a general (PR-level) note instead of anchoring on a sibling.
A comment anchored on the wrong file is worse than a general note — the author opens `baz.py` looking for the problem, finds nothing, and loses trust in the review.

**Post-flight: verify response.** Response must confirm the comment landed on the correct file/line — if position data is missing in the response, the comment landed as a general comment (wrong). After posting all notes, list them via the API and confirm the count and positions match expectations.

**Do NOT submit the review without explicit user instruction.** By default, the user reviews draft notes in the platform's UI, edits if needed, and submits manually. If the user explicitly asks to publish (e.g., "post with t3 cli", "submit the review"), use:

```bash
t3 review publish-draft-notes <REPO> <MR_IID>
```

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

**Review-first precondition (enforced, not advisory).** `approve` refuses unless a review note/discussion authored by *your* identity already exists on that MR. This encodes the approve-on-review doctrine in the tool itself: you cannot record an approval without having left a reviewing footprint first. If it refuses, post your review (`t3 review post-comment` — default draft, #1207) and then approve. `unapprove` has no precondition — revoking is the safe direction and is always reachable.

**On-behalf gate.** An approval is an outward, state-changing post under your identity, so `approve`/`unapprove` also respect the `on_behalf_post_mode` pre-gate (souliane/teatree#960). Under an autonomous overlay (`autonomy = "full"` / `"notify"`, which collapse the mode to `"immediate"`) the approval proceeds unattended — that is the "Colleague-MR Autonomy" behavior above, no further step. Under `"babysit"` (mode stays `"ask"` / `"draft_or_ask"`) the command refuses unattended with an actionable message — record an `OnBehalfApproval` via `t3 review approve-on-behalf <target> approve --approver <user-id>` and re-run, or raise the tier with `t3 <overlay> autonomy set full` / `t3 <overlay> autonomy set notify` (preferred — one knob) for the overlay.

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
