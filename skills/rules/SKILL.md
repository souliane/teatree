---
name: rules
description: Cross-cutting agent safety rules — clickable refs, temp files, sub-agent limits, UX preservation. Auto-loaded as a dependency by other skills.
compatibility: any
metadata:
  version: 0.0.1
  subagent_safe: true
---

# Agent Rules

Cross-cutting rules that apply to all teatree skills. Loaded automatically via `requires:`.

## Index

Use `Ctrl+F`/`grep` to jump to a rule. Sections are grouped below by theme; numbering is for navigation only — every rule is binding.

**Skill loading & verification**

1. [Invoke Skills Before ANY Response](#invoke-skills-before-any-response)
2. [Verification Before Completion](#verification-before-completion-non-negotiable)
3. [Grep Before Claiming Cross-Reference Coverage](#grep-before-claiming-cross-reference-coverage-non-negotiable)
4. [Verify Imports Before Applying External Code](#verify-imports-before-applying-external-code)
4a. [Read the Canonical Source Before Fixing a Conformance Bug](#read-the-canonical-source-before-fixing-a-conformance-bug)
4b. [Re-Verify Cross-Agent State Before Reporting a Dependent Request](#re-verify-cross-agent-state-before-reporting-a-dependent-request)

**User intent, interruptions, and asking**

5. [User Instructions Are Priority 1](#user-instructions-are-priority-1)
5a. [On an Ambiguous Directive, Take the Non-Destructive Reading](#on-an-ambiguous-directive-take-the-non-destructive-reading-non-negotiable)
6. [Always Use AskUserQuestion for Questions](#always-use-askuserquestion-for-questions)
7. [Always Create Tasks](#always-create-tasks)
8. [Mid-Task Interrupts](#mid-task-interrupts-non-negotiable)
8a. [Background Long Operations](#background-long-operations-non-negotiable)
9. [Context Transparency](#context-transparency)
10. [Context Longevity](#context-longevity)

**Permissions, classifier, and authorization**

11. [Classifier Denial Protocol](#classifier-denial-protocol-non-negotiable)
12. [Ask About Auth Before External Service Integrations](#ask-about-auth-before-external-service-integrations)
13. [Publishing Actions Are Mode-Conditional](#publishing-actions-are-mode-conditional-non-negotiable)
13a. [Never Modify a Remote Database Without Explicit User Approval](#never-modify-a-remote-database-without-explicit-user-approval-non-negotiable)

**Communication & references**

14. [Clickable References](#clickable-references)
14c. [Render the Title Inline, Never a Bare/Link-Only Id](#render-the-title-inline-never-a-barelink-only-id-non-negotiable)
14b. [ID Namespace Disambiguation](#id-namespace-disambiguation-non-negotiable)
14a. [Lead a Completion Report With the Assigned-Work Status](#lead-a-completion-report-with-the-assigned-work-status)
14d. [Keep Turn Output Terse and TTS-Ready](#keep-turn-output-terse-and-tts-ready)
15. [No AI Signature on Posts Made on the User's Behalf](#no-ai-signature-on-posts-made-on-the-users-behalf-non-negotiable)
15a. [Ask Before Posting on the User's Behalf](#ask-before-posting-on-the-users-behalf-non-negotiable)
16. [Never Post PR Comments from Parallel Agents](#never-post-pr-comments-from-parallel-agents-non-negotiable)
17a. [Evidence Comes From the Deployed Environment](#evidence-comes-from-the-deployed-environment-non-negotiable)
17. [Verify Repo Visibility Before Filing External Issues](#verify-repo-visibility-before-filing-external-issues-non-negotiable)
18. [Leak Remediation — Silent Scrubs](#leak-remediation--silent-scrubs-non-negotiable)
19. [Public-Repo Commit Author Identity](#public-repo-commit-author-identity-non-negotiable)
20. [GitLab Inline Comments](#gitlab-inline-comments)

**API & shell recipes**

19a. [Read Secrets From the Secret Store](#read-secrets-from-the-secret-store-non-negotiable)
19b. [Read the Canonical Source Before a Structural Action](#read-the-canonical-source-before-a-structural-action-non-negotiable)
19c. [Overlay Skills Are Scoped to Overlay Repos](#overlay-skills-are-scoped-to-overlay-repos-non-negotiable)
20. [Token Extraction](#token-extraction)
21. [Temp File Safety](#temp-file-safety)
22. [Complex API Payloads: Use curl or Python](#complex-api-payloads-use-curl-or-python)
23. [Preserve Existing UX Patterns](#preserve-existing-ux-patterns)
24. [Prefer Native Tool APIs Over Filesystem Heuristics](#prefer-native-tool-apis-over-filesystem-heuristics)
25. [Shell Alias Safety](#shell-alias-safety)

**Files, agents, and worktrees**

26. [Sub-Agent Limitations](#sub-agent-limitations)
27. [Symlink Safety](#symlink-safety)
27a. [Read Before Overwriting a Tracked Config/Dotfile](#read-before-overwriting-a-tracked-configdotfile-non-negotiable)
28. [Skill File Writes Require a Git Repo](#skill-file-writes-require-a-git-repo)
29. [Worktree-First Work](#worktree-first-work-non-negotiable)
30. [Concurrent Agent Safety](#concurrent-agent-safety-non-negotiable)

**Workflow discipline**

31. [Fix TeaTree/Skill Bugs Immediately](#fix-teatreeskill-bugs-immediately)
32. [Teatree Extension Point Changes Must Update All Registered Overlays](#teatree-extension-point-changes-must-update-all-registered-overlays-non-negotiable)
33. [Do Work Now, Don't Defer to "Later" Tickets](#do-work-now-dont-defer-to-later-tickets-non-negotiable)
34. [Contribute Mode: Promote Findings to Skills, Not Personal Memory](#contribute-mode-promote-findings-to-skills-not-personal-memory-non-negotiable)
35. [Never Change PR Base Branch or Dependencies](#never-change-pr-base-branch-or-dependencies-non-negotiable)
36. [Run Retro Before Ending Non-Trivial Sessions](#run-retro-before-ending-non-trivial-sessions)
37. [Commit Before Declaring Done](#commit-before-declaring-done-non-negotiable)
38. [Pre-Commit Hook Failures on Unrelated Tests](#pre-commit-hook-failures-on-unrelated-tests)
38a. [Re-Derive the Minimal Blocker](#re-derive-the-minimal-blocker)

**Design principles**

39. [Prefer Standard Over Clever](#prefer-standard-over-clever)
40. [Never Slim Skills](#never-slim-skills)
41. [Session Scope Management](#session-scope-management)
42. [Skill Auto-Loading Must Work](#skill-auto-loading-must-work)
43. [Escalate Honesty-Critical Verification to the Most-Honest Model](#escalate-honesty-critical-verification-to-the-most-honest-model)
44. [Re-Validate a Reused Guard in a New Destructive Context](#re-validate-a-reused-guard-in-a-new-destructive-context)

## Invoke Skills Before ANY Response

_Adapted from [superpowers/using-superpowers](https://github.com/obra/superpowers)._

When a skill might apply — even a 1% chance — **invoke it BEFORE responding, exploring, or asking clarifying questions.** The `UserPromptSubmit` hook suggests skills; you must load every suggestion. If the hook doesn't fire, pick the right skill yourself.

**Stop rationalizing.** These thoughts mean you're skipping a skill:

| Thought | Reality |
|---------|---------|
| "This is just a simple question" | Questions are tasks. Check for skills. |
| "Let me explore the codebase first" | Skills tell you HOW to explore. Load first. |
| "I need more context first" | Skill check comes BEFORE clarifying questions. |
| "The skill is overkill for this" | Simple tasks become complex. Use it. |
| "I already know how to do this" | Skills evolve. Load the current version. |
| "I'll just do this one thing first" | Load skills BEFORE doing anything. |

**Announce at start:** State which skill(s) you loaded and why, so the user can verify you're on the right track.

## Verification Before Completion (Non-Negotiable)

_Adapted from [superpowers/verification-before-completion](https://github.com/obra/superpowers)._

**No completion claims without fresh verification evidence in the same response.** If you haven't run the command and read its output in this message, you cannot claim it passes.

1. **Identify** — what command proves this claim? (tests, lint, build, manual check)
2. **Run** — execute it fresh and completely
3. **Read** — full output, check exit code, count failures
4. **Claim** — state the result WITH evidence

**Banned language without evidence:** "should pass", "probably works", "seems correct", "looks good", "I'm confident". These words without a command output are lies, not claims.

**Multi-deliverable tickets: measure done from the SPEC, not the artifacts you produced (Non-Negotiable).** On a ticket with more than one deliverable, a completeness assertion — "done", "no blockers anywhere", "everything is here", "ready to merge/review" — is measured from **every deliverable the authoritative spec defines (incl. the spec's comments) verified on the actual merge target**, never from the artifacts you happen to have in hand. The recurring, highest-severity failure: claiming "no blockers anywhere" while the crucial deliverable was registered on the wrong surface and its fix was stranded off the merge target — invisible to a check that only inspects what exists. A false completion claim that propagates downstream is not an internal slip. Before any completion claim on a multi-deliverable ticket:

1. **Read the authoritative spec and its comments first.** A claim emitted before the spec source was read leans on proxies (the work item, repo docs, the baseline). If you have not read the spec, you cannot claim done.
2. **Enumerate EVERY spec deliverable** — not just the MRs/PRs created.
3. **Attach on-target evidence to EACH** — merged to the merge target / verified on the correct surface / passing E2E. "An MR exists" is NOT evidence.
4. **Verify the crucial/authoring deliverable explicitly on its correct surface** — the one that silently degrades to the wrong surface.
5. **Any deliverable lacking on-target evidence → say "NOT done: <X> missing / on the wrong surface / stranded off target"**, never "done".

This is enforced, not just prose: the BLOCKING Stop gate `handle_completion_claim_gate` (#2665) refuses turn-end on a multi-deliverable completion claim with no complete on-target deliverable→evidence map. It fires only on loop-driven turns; a legitimate single-deliverable "done" or a complete on-target map is never blocked. Never-lockout escapes: the `[skip-completion-gate: <reason>]` token in the turn text and the `[teatree] completion_claim_gate_enabled = false` kill-switch (`t3 <overlay> gate completion-claim disable`). This is the hard-blocking sibling of the WARN-only closure-reverify advisory (#1448).

## Grep Before Claiming Cross-Reference Coverage (Non-Negotiable)

When the user asks how their codebase or harness compares to an external reference — an article, a framework's docs, a competitor's product, a popular library — the reflex is to pattern-match: a name in the reference resembles a skill or file or function the agent has seen, so the agent claims it's covered (or claims the inverse). This pattern-match is unreliable across naming differences and partial-implementation gaps, and it always defaults toward overclaiming coverage when the agent has the user's project context loaded.

**Required before any "X is covered / X is a gap" claim:**

1. **Grep the actual repo** for the concept under at least two framings. `rg`, `grep -r`, or `git log -S` against the codebase, not against memory.
2. **Cite `file:line`** for each "covered" assertion. A claim that something exists must point at where it exists.
3. **Cite the specific gap** for each "missing" assertion. Name the function, regex, or section that would have to exist and link the file path where you'd expect it. If you can't, you don't have enough evidence to call it a gap.
4. **If you can't grep** (no read access, ambiguous naming, the concept is implementation-shape rather than keyword-shape), **ask the user** before making the claim. Do not paper over the uncertainty with hedge words.

**Banned shortcuts:**

- Naming a skill ("/t3:code", "/t3:ship") and asserting it covers an article concept on the strength of its description alone.
- Listing items as "covered" because they sound like things the harness probably does.
- Producing a "what's missing" list without grepping for each item first.

**Why this rule exists.** When the user's project state is loaded into context (CLAUDE.md, MEMORY.md, recent file reads), the agent's pattern-matching defaults aggressive — it treats name-similarity as coverage and produces flattering comparisons that don't survive a `rg` check. The corrective is to require evidence at the point of claim, not at the point of correction.

## User Instructions Are Priority 1

When the user gives a direct, explicit instruction (skip tests, push now, use this approach), execute it IMMEDIATELY. Do not try a "better" approach first, do not retry the same failing approach hoping it works, and do not silently substitute your own plan. Execute the instruction first (it's fast and safe), then suggest an alternative if you have one.

## On an Ambiguous Directive, Take the Non-Destructive Reading (Non-Negotiable)

When a directive admits two readings — one destructive (overwrites/deletes/restores/force-pushes/drops) and one non-destructive (reads, inspects, leaves state intact) — **take the non-destructive reading and proceed; surface the ambiguity only if the safe path doesn't resolve the request.** A vague "reset the config" / "clean that up" / "fix the file" is NOT authorization to clobber state: do the reversible, inspectable thing first.

- "reset/restore X" → first **read** X's current state and report it; do not `git checkout`/`git reset --hard` it until you have read it and confirmed the destructive action is what the user meant.
- "clean up / remove the stale Y" → inspect what Y contains before deleting; an unread artifact may hold unpushed work or uncommitted edits.
- The cost of the safe reading is one extra read; the cost of the destructive reading is irreversible data loss. When the readings diverge on reversibility, reversibility wins.

This composes with § "User Instructions Are Priority 1" (an EXPLICIT destructive instruction — "yes, `git checkout` the file" — is executed immediately) and § "Always Use AskUserQuestion for Questions" (a genuinely undecidable destructive choice is one structured question, not a silent guess). The rule here governs the _default reading_ of an ambiguous directive: lean safe.

## Classifier Denial Protocol (Non-Negotiable)

When the auto-mode classifier denies a tool call (Bash command rejected, MCP call refused, "permission denied" from the harness, etc.), **stop immediately**. Do not retry, do not work around it with a different command, do not "find another way". A classifier denial is an **immediate session blocker** — handle it before doing anything else.

**Step 0 — read the denial reason and check existing allow-rules before escalating.** The denial message states _why_ it was blocked, and that reason frequently names the in-scope form the action must take (e.g. "database outside the authorized `development-<tenant>` scope" → the authorized DB name is `development-<tenant>`, not the one you used). Before treating this as "needs relaxation": (a) parse the stated reason for the corrective scope, and (b) read the user's `~/.claude/settings.json` `autoMode.allow` and `permissions.allow` entries for a rule that already authorizes this action under the correct form. If either resolves it, the action was never out of policy — re-issue it in the **authorized form** (this is not a relaxation and needs no user prompt). Only if neither the reason nor an existing rule resolves it do you run the escalation below. Skipping Step 0 and escalating a mere wrong-form mistake wastes the user's time on a decision they should never have been asked.

**Required response when Step 0 does not resolve it:**

1. **Stop.** Drop whatever you were doing. Do not start an alternative approach in the same response.
2. **Inform the user** in plain text: which command was denied, what you were trying to accomplish, and the smallest static permission rule that would have allowed it (e.g. `Bash(gh issue create *)`, `Bash(docker buildx prune *)`). The rule must be the smallest rule that covers the use case — never a blanket `Bash` or `Bash(* *)`.
3. **Ask via `AskUserQuestion`** with two options:
   - **"Allow it (relax classifier)"** — preferred. You then attempt the edit yourself (see step 4); only if the harness blocks the write do you fall back to a paste-ready snippet for the user to apply.
   - **"Keep the denial (do it differently)"** — you propose a concrete alternative path (different tool, manual step, API call) and proceed only after the user picks one.
4. **If the user picked "Allow it":** attempt to add the rule to the user's `~/.claude/settings.json` (`permissions.allow` array) yourself, via the `Edit` tool. Read the file first, merge the new entry into the existing array, write it back. **If the write succeeds**, retry the original command. **If the write is denied** by the harness self-modification guardrail, only then fall back: hand over a paste-ready snippet, wait for the user to apply it, then retry. Do not preemptively skip the edit attempt — the goal is zero manual operations for the user when the harness allows it.
5. **Wait for the answer.** Do not retry the denied command, do not invent workarounds, do not file tickets, do not start unrelated work, until the user has chosen and (if relaxing) the new rule is in place.

**Banned reactions to a classifier denial:**

- Silently retrying with a different argument shape hoping the classifier passes (`gh issue create` → `gh api repos/.../issues`).
- Switching tools (Bash → MCP, MCP → Python subprocess) to bypass the rule.
- Decomposing the command into pieces that each pass individually.
- Editing teatree's plugin `settings.json`, `CLAUDE.md`, or any plugin-distributed permissions file to add an allow rule.
- Continuing the surrounding work and "leaving the denial for later".

**Why this rule exists.** The classifier exists to give the user a final say on standing-permission expansions. Auto-mode aggressiveness combined with classifier strictness is a recurring source of teatree workflow breakage — agents that retry, decompose, or sidestep silently accumulate scope, lose user trust, and ship work the user never authorized. The right escalation is to **ask once, fix permission at the user-scope settings file, retry**.

**Standing recommended set (proactive, not reactive).** This protocol governs _reacting_ to a mid-session denial. The _standing_ generic set of authorizations that prevents most denials in the first place — and the read-only `t3 doctor authorizations` check that suggests (never applies) the absent ones — is documented in `skills/setup/references/recommended-automode-authorizations.md`. That doc and this section do not duplicate: one is the standing recommendation, the other the in-session escalation.

**Boundary: who edits permissions where.**

- Teatree (this skill, BLUEPRINT, plugin `settings.json`) defines the _protocol_. Teatree never relaxes permissions on the user's behalf.
- The agent **attempts** the edit to `~/.claude/settings.json` (user scope) directly — that's the path with zero manual steps for the user. Many users have a standing authorization for this in their `autoMode.allow`. The agent only falls back to handing over a paste-ready snippet **after** the harness self-modification guardrail blocks the write — never as the default path. The snippet is the manual fallback, not the primary mechanism.
- Plugin-distributed permissions (`plugins/t3/settings.json`, `CLAUDE.md` standing clauses) are **never** the right place to relax for a single workflow — that would grant the standing right to every user of the plugin. Refuse if asked to do this; explain that user-scope `settings.json` is the right knob.

## Re-Derive the Minimal Blocker

When an operation is blocked — a classifier denial, a failing gate, an external or human-gated wait — re-derive the **minimal** set of work that genuinely depends on that exact operation before declaring anything else blocked. A blocked merge does not block PR creation, implementation, review, or research; a blocked deploy does not block the next feature. Before reporting "nothing actionable", ask of each pending task: does it consume the blocked operation's output, or does it merely share a goal (or sit later in the same chain) reachable by a different, available path? Reporting "nothing actionable" for two or more cycles behind a single external block is itself the signal to audit for a non-blocked path rather than continue idling. This complements the Classifier Denial Protocol (which governs the denied operation itself); this rule governs not over-propagating that block to independent work.

## Read the Canonical Source Before Fixing a Conformance Bug

When a bug's root cause is "our code disagrees with an external authority" — a CI validator, a wire protocol, a spec, a sibling service's schema, an upstream library's behaviour — **read that authority's actual source before writing the fix or the red test**, not after. The fix for a conformance bug is _parity with the authority_, so the authority's exact behaviour (regexes, normalization, edge cases, what it does and does NOT check) is the specification. Implementing from the symptom or from an assumed root cause produces a fix that re-diverges differently: a discarded implement-and-test cycle, then a re-implement against the source that should have been read first.

- Locate the authority's source (vendored copy, sibling repo under the workspace, pinned dependency, the CI job's invoked script) and read the relevant function end to end.
- Derive the red test from the authority's behaviour, not from a hypothesis about it. If the authority does NOT enforce the thing you assumed, the bug is elsewhere — discover that before coding.
- Prefer vendoring the authority verbatim (pointer comment + drift-detecting parity test) over hand-reimplementing its rules, so future divergence is caught mechanically rather than by the next incident.

## Re-Verify Cross-Agent State Before Reporting a Dependent Request

In a multi-agent / multi-loop environment, another agent may have advanced a shared artifact (a PR merged, an issue closed, a branch rebased, a baseline moved) while your task was running. Before reporting a request or recommendation whose validity depends on that artifact's state ("dispatch a reviewer for PR N", "merge X next", "rebase Y"), **re-fetch the artifact's current state in the same turn you report it**. A request built on the artifact's state at task-start is stale by the time a long task finishes; reporting it makes the agent look out of sync and wastes the coordinator's turn correcting it. The cost of one `gh pr view` / `glab mr view` before the report is trivial; the cost of a stale dependent request is a wasted round-trip.

## Lead a Completion Report With the Assigned-Work Status

When reporting back on assigned work, the reader's first need is an unambiguous answer to **"is the assigned work done, and where is it?"** — deliverable status, branch/PR/HEAD, gate results. Out-of-scope observations, systemic findings, or follow-up recommendations surfaced along the way must be **clearly separated and subordinate**: a labelled trailing section, never positioned so they displace, precede, or read as a substitute for the deliverable status. A correct systemic analysis that buries the "done?" answer reads as "did the analysis instead of the work" — the coordinator concludes nothing shipped and spends a round-trip re-asking for what was already finished. Separate the two concerns physically; lead with the in-scope status every time.

**On a STANDING verified-green goal, LEAD with the blunt binary — a status report is a checkpoint, not the deliverable (do X, never Y).** When the work is a standing "make X verified-green" goal (the eval suite, the e2e suite) and X is NOT yet green with achievable work remaining, a status report must OPEN with the binary truth on each suite — **"evals green? NO. e2e green? NO."** — BEFORE any wins, and must keep the goal **explicitly open**. The recurring, critical drift this forbids: the agent does a chunk of work, foregrounds the wins (merged-PR counts, per-lane greens, "good progress"), surfaces a blocker, and ends the turn on a positive-framed status that READS as-if-done — so the goal stays unmet and the user has to re-prod for weeks. Surfacing a blocker is a checkpoint, not completion. The honest report is one of exactly two shapes, both leading with the binary: **keep driving** the next achievable fix, or **surface-and-hold** (name the specific blocker AND state the goal stays open). It is never a win-led wrap-up.

```text
# do X — LEAD with the binary on each suite, then wins, and keep the goal open:
#   "Eval suite green? NO — 3 scenarios still red. E2E green? NO — 2 specs still red.
#    Goal unmet, stays open. Wins: 3 PRs merged, 5 lanes green. Next: triage the first red."
# never Y — a win-led report that ends the turn as-if-done while the goal is unmet:
#   "Merged 3 PRs, 5 lanes green — good progress. Solid checkpoint, picking the rest up next time."
```

This must not be gameable by an `AskUserQuestion`-to-defer or a positive-framed partial report that ends the turn: while the goal is unmet and work remains, the only honest stop is actually-green OR a user-acknowledged external ceiling. Pinned by `standing_green_goal_keeps_driving_never_stops_done` (the keep-driving ACTION) and `verified_green_status_report_leads_binary_never_stops_as_done` (the report-LEAD text) in `evals/scenarios/rules.yaml`.

## Keep Turn Output Terse and TTS-Ready

Every turn response must be short enough to speak aloud without losing the listener. The whole turn output — not just a summary — should fit TTS comfortably.

**Required:**

- Lead with the answer or the action taken. The first sentence is the payload; context and reasoning follow only if necessary.
- One sentence per point. No long prose paragraphs.
- No decorative markdown (headers, horizontal rules, nested bullet trees, bold-for-structure) when speaking. Plain sentences work for speech; heading hierarchies do not.
- Suppress routine status noise. "N signals, N actions" and "still running" progress reports are not actionable → omit them unless something changed that the user must act on.
- Background work: report on completion or decision only, not on each in-progress tick.
- The only proactive user-DMs are mergeable customer MRs, blockers, and genuine asks — never routine status. A "everything green, still running" tick is not a DM; pinned by `evals/scenarios/slack_only_human_needed.yaml`.

**Anti-patterns:**

- A multi-paragraph narrative of what was done, what was found, and what comes next — when the answer is "done, here is the PR link".
- A section-headed summary where every item is restated twice (once as a heading, once as prose).
- A tick report that says "everything is fine" in 8 lines when silence would be correct.

**TTS cap:** If `t3 speak` is active (`[teatree.speak] local = "all"`), the per-turn text passed to `clean_for_speech` is capped at 600 characters. Write turns that fit without truncation by default — the cap is a hard backstop, not a target. A turn that requires aggressive truncation before it fits TTS was too verbose to start.

## Context Transparency

The user cannot see system-reminders, memory content, or hook output injected into your context. When your response is influenced by any of this invisible context, **briefly state what you received** so the user can follow your reasoning. For example: "Teatree suggested loading `/t3:code`. Memory mentions X."

If the user's message is ambiguous (references "this", "it", a link they forgot to paste, etc.) — **ask for clarification**. Do NOT guess based on context the user can't see. Guessing leads to confusing exchanges where the user has no idea what you're talking about.

## Clickable References

Every PR, ticket, issue, or note reference — in markdown files, platform comments, **and** agent responses — must be a clickable markdown link.

- `[!5657](https://example.com/org/repo/-/merge_requests/5657)` — not `!5657`
- `[PROJ-1234](https://example.com/org/repo/-/issues/1234)` — not `PROJ-1234`

This applies everywhere: MR/PR descriptions, inline comments, test evidence, chat messages, and responses to the user. When you are handed the id **and** its URL, emit the markdown link — do X, never Y:

- **do:** `MR [!7551](https://git.example.com/acme/app/-/merge_requests/7551) is ready for review.`
- **never:** `MR !7551 is ready for review.` (a bare id the reader cannot click)

## Render the Title Inline, Never a Bare/Link-Only Id (Non-Negotiable)

Every surface that _lists_ a ticket/MR/PR/issue id must render the human-readable title inline — `#N (short ≤6-word title)` (or `[#N (short title)](url)` where a link applies) — so the reader knows _what_ `#N` is without opening it. A bare `#N`, or a clickable number next to no title, is the anti-pattern: the reader cannot tell one row from another. The title and the URL are two halves of one contract — the clickable-link rule above resolves the URL; this rule supplies the title.

- The single chokepoint is `teatree.core.ref_render.render_ref(label, *, title, url)` — every id-listing surface (loop-tick statusline, `/checking`, `/todos`, notify/standup recaps) formats through it so they read identically. Do not hand-roll the `#N (title)` shape per call site.
- A row whose ticket has no known title degrades to the plain id (still clickable when a URL applies), never an empty `()`.
- This is the _listing_ rule; the namespace-disambiguation rule below governs _which_ id token (`TODO-<n>` vs `<repo>#<n>`) the `label` is. They compose: a todo line is `task TODO-<id> (ticket #<n> (<title>) …)`.

## ID Namespace Disambiguation (Non-Negotiable)

Id references must be namespace-qualified — they are never bare. A harness/teatree **task id** and a forge **issue/ticket/PR id** are different namespaces that both number from ~1, so a bare `#<n>` standing next to another bare `#<n>` is undecidable: an agent cannot tell whether `task #5` next to `ticket #5` are the same thing or two unrelated objects, and may resolve a task id against the issue tracker and act on the wrong object.

- **Harness/teatree task ids** render as `TODO-<n>` (e.g. `TODO-7`) — never `task #<n>` or bare `#<n>`. This is `Task` PKs and harness TODO ids alike.
- **Forge issue/ticket/PR ids** render as `<repo>#<n>` when ambiguity with a task id (or a cross-repo ref) is possible (e.g. `teatree#11`, `<overlay-repo>#42`/`!42`). A bare `#<n>` for a forge ref is acceptable only inside a context already scoped to one forge namespace (e.g. a statusline line prefixed `[overlay]`, or a single-namespace section), never side-by-side with a task id.
- Never emit a bare `#<n>` for a task id sitting next to a bare `#<n>` for a ticket.
- **A repo-qualified ref is a rendering convention, not `gh`/`glab` CLI syntax — do X, never Y.** `<repo>#<n>` (e.g. `teatree#50`) is how you _write_ the ref in prose, a statusline, or a commit body. Neither `gh` nor `glab` accepts that slash/hash-qualified string as a single positional argument — pass the bare number and name the repo with its own flag:

  ```bash
  # do X — bare number + explicit repo flag:
  gh issue view 50 --repo souliane/teatree
  gh pr view 50 --repo souliane/teatree
  glab issue view 50 --repo souliane/teatree
  # never Y — a repo-qualified single argument is not valid gh/glab CLI syntax,
  # even though "teatree#50" is the correct PROSE rendering of the same ref:
  gh issue view teatree#50              # FORBIDDEN — gh rejects this argument shape
  gh issue view souliane/teatree#50     # FORBIDDEN — same error
  ```

  Inside the repo's own working tree (`gh`/`glab` resolve the repo from the git remote), `--repo`/`-R` can be omitted — `gh issue view 50` is fine there. Add the flag whenever the command runs outside that repo's tree, or whenever the surrounding text disambiguates against a same-numbered task id and the command must stay unambiguous too.

This is the canonical home; `/t3:todos` § "Output contract" cross-references it for the `task TODO-<id> (ticket #<n>)` line shape, and the disambiguation eval is `evals/scenarios/id_namespace_disambiguation.yaml`.

## Read Secrets From the Secret Store (Non-Negotiable)

Every credential — API token, service password, signing key — is read **from the secret store at point of use**, never hard-coded in a command, a file, a commit, or echoed into the transcript. The canonical fetch is a secret-manager read into a variable, so the literal value never appears in your tool call or in shell history.

Do X — read from the store:

```bash
TOKEN="$(pass show <service>/api-token)"     # password-store
# or: TOKEN="$(op read 'op://<vault>/<item>/token')"   # 1Password CLI
# or: TOKEN="$(vault kv get -field=token <path>)"        # HashiCorp Vault
```

Never Y — never inline or echo a literal secret (the `<...>` below stands in for the real value, which must never appear):

```bash
export SERVICE_TOKEN=<the-literal-token>        # FORBIDDEN — literal in history + transcript
curl -H "Authorization: Bearer <the-literal-token>"   # FORBIDDEN — literal in the command
```

Reference the variable (`"$TOKEN"`) in the call that needs it; never the literal. See `t3:platforms` § "Token Extraction" for the per-platform CLI recipe. Pinned by `evals/scenarios/privacy_and_safety.yaml` (`safety_secret_read_from_secret_store`).

## Read the Canonical Source Before a Structural Action (Non-Negotiable)

Before a **structural** action — standing up an agent team / fleet, spawning panes, reorganizing worktrees, changing an extension-point contract, anything that commits the session to a topology — **read the canonical source that defines that structure FIRST**, in the same turn, before you dispatch anything. The structure's source of truth (a skill's SKILL.md, the BLUEPRINT roles section, the loops skill, CLAUDE.md) is the spec; acting from memory invents a divergent shape that then has to be unwound.

- Asked to "enable team mode" / "enable agent team mode": your single next action is **one** `Read` of the canonical role split — for team mode that file is **`skills/loops/SKILL.md`** (the loops skill owns the team-role split; BLUEPRINT.md's roles section or CLAUDE.md are equivalent canonical sources) — and it names the panes/roles and the overlay seam (one pane teatree, one pane the overlay). Issue that `Read` **before** any `Agent`/`Task` dispatch. **You ALREADY know the canonical roles from prior context — that knowledge is NOT a license to skip the Read.** Spawning `CORE_MAKER`/`OVERLAY_MAKER`/`REVIEWER` panes "from memory" because you remember the role names is the exact drift: read the source first even when you are confident you recall it, because the source is the spec and your memory is not. The Read comes first; the spawn comes after.
- **The canonical `Read` IS the single action — issue it and STOP.** Do not first shell out to locate the file (`find … BLUEPRINT.md`, `echo "$T3_REPO"`, `ls`, `cat ~/.teatree`), and do not loop retrying alternate paths if a `Read` comes back not-found. Read `BLUEPRINT.md` (or `skills/loops/SKILL.md`) by its repo-relative path in one call; that read is the structural-action gate, whether or not the file resolves on the first try. **And the STOP is symmetric — do not path-hunt AFTER the read either.** The metered drift the lane caught is read-FIRST-then-over-explore: the agent issues the correct canonical `Read`, then keeps going with `find`/`grep`/`git rev-parse`/`ls`/`echo`/`cat` calls to locate or re-locate the file "to be thorough" before acting. That over-exploration is the same violation in mirror image — the canonical read already gave you the spec, so once it returns, proceed to the structural action (or stop); do NOT shell out to hunt for the file again. One canonical Read, then act — no path-hunting on either side of it.

```bash
# do X first — ONE canonical read by its repo-relative path, then stop:
#   Read(file_path="BLUEPRINT.md")            # or skills/loops/SKILL.md / CLAUDE.md
# never Y — do not hunt for the path with shell calls before the read:
#   Bash(command="find ~ -name BLUEPRINT.md")  ← FORBIDDEN: the Read is the action
# never Z — do not dispatch panes from memory before that read:
#   Agent(prompt="you are CORE_MAKER …")       ← FORBIDDEN as the first action
```

This is the structural-action sibling of § "Read the Canonical Source Before Fixing a Conformance Bug" (which governs conformance bugs); both say: the authority is the spec, read it before you act. Pinned by `read_canonical_before_structural_action_under_load` (`evals/scenarios/rules.yaml`).

## Overlay Skills Are Scoped to Overlay Repos (Non-Negotiable)

Load the overlay playbook skill (`/t3-<overlay>`) for **any** task in an overlay-managed repo — and ONLY for those. A non-overlay task needs no overlay skill.

- **Overlay-repo task** (coding/reviewing in an overlay's product repo): self-load the overlay skill `/t3-<overlay>` alongside the dev + language skills **before** reading a diff or editing source — it carries the repo's run/test/review wiring (see `overlay_work_requires_overlay_skill.yaml`).
- **Non-overlay task** (a change inside `souliane/teatree` itself, or any standalone repo with no active overlay): load only the skill(s) that actually apply — `ac-django` / `/t3:code` / `/t3:teatree` for a teatree Django change. Do NOT pull in a different project's overlay skill; teatree is its own Django project, not an overlay repo.

```text
# teatree-only change → load what applies, not an overlay skill:
Skill(skill="ac-django")   # or t3:code / t3:teatree
# do NOT: Skill(skill="t3-<overlay>")   ← wrong scope for a non-overlay task
```

Pinned by `non_overlay_task_does_not_require_overlay_skill` (`evals/scenarios/skill_routing.yaml`).

## Token Extraction

When extracting an API token from a CLI tool, always extract to a variable first — never inline in curl. See your platform reference (`t3:platforms`) § "Token Extraction" for the platform-specific recipe.

**In Python heredocs:** shell variables are NOT inherited. Use `os.popen(...)` inside Python or `export TOKEN` before the heredoc.

## Temp File Safety

When using temporary files (for PR note bodies, test data, etc.):

- Hardcoded paths are forbidden like `/tmp/mr_note_body.md` — stale content from other sessions gets posted to the wrong PR.
- **Always use `mktemp`** or inline Python heredocs instead.
- **Always use `>|`** (clobber override) not `>` — zsh `noclobber` silently prevents overwrite.
- **Always clean up** the temp file immediately after use (`os.unlink()` in Python, `rm` in shell).
- **Exception: pre-compaction snapshots** — files matching `/tmp/t3-snapshot-*.md` are recovered automatically on the post-compaction `SessionStart` (`source=="compact"`) event (issue #845). Use `t3-snapshot-${CLAUDE_SESSION_ID:-manual}-$(date +%Y%m%d-%H%M).md` for the filename. Delete after persisting findings to durable storage.

## Complex API Payloads: Use curl or Python

Some issue tracker CLIs cannot serialize nested JSON. **Always use `curl`** with `-H "Content-Type: application/json"` and a proper JSON `-d` body for payloads containing nested objects.

For note bodies containing markdown images (`![alt](url)`), shell variable interpolation and `jq --arg` both escape `!` to `\!`. **Always use Python** (`urllib.request` or `requests`) to serialize the JSON payload.

## Preserve Existing UX Patterns

When fixing a broken UX mechanism (web terminal, browser launch, notification method), fix it **in-kind** — do not replace it with a different mechanism without asking. If proposing a different approach, ask the user first: "Currently this uses X. Want to keep that or switch to Y?"

## No AI Signature on Posts Made on the User's Behalf (Non-Negotiable)

Every artifact you publish under the user's identity — git commits, MR/PR descriptions, MR/PR comments and discussions, issue bodies, Slack/Teams messages, email drafts, release notes — must read as if the user wrote it. **Never append AI/agent signatures or footers**.

**Canonical setting:** `agent_signature` (DB-home, default `false`) — set with `t3 <overlay> config_setting set agent_signature <true|false>` (add `--overlay <name>` for the per-overlay scope). Programmatic teatree code paths that post on the user's behalf consult `teatree.identity.agent_signature_enabled()` (or wrap their suffix in `agent_signature_suffix(...)`). When you publish through an external tool (MCP Slack send, `gh` comment, `glab` discussion, raw `httpx`), apply the same policy by hand: omit the signature unless the setting is `true`.

**Banned trailers and footers in any user-on-behalf artifact:**

- `Co-Authored-By: <model> <noreply@anthropic.com>` (or any other agent identity)
- `🤖 Generated with Claude Code` / `Generated with [Claude Code](...)`
- `Sent using Claude` / `Drafted by Claude` / `via Claude` / `(via AI)` / `via the assistant`
- Any emoji-bot signature or "this message was written by …" footer
- Slack-block "Posted by Claude" / "AI-generated" formatting

**This rule is global, not commit-specific.** The original "no Co-Authored-By in commits" rule was a special case; the principle generalizes to every venue where the agent posts on the user's behalf. If you would not put `Co-Authored-By` on a commit, do not put `Sent using Claude` on a Slack message. The user is responsible for the content; the agent is the typist, not the author.

**When the user is the author and explicitly invokes you:** if the user asks for a draft to review before sending themselves, no signature is needed (they will send it themselves anyway). When **you** post on their behalf (Slack DM, PR discussion, GitHub comment, email), the rule still applies — the message must be indistinguishable in form from one the user wrote.

**Failure mode this rule prevents:** the agent appends "Sent using Claude" to a Slack message it sends to a colleague on the user's behalf. The colleague now sees that the user did not write the message themselves; the user looks lazy or impersonal, and the rapport with the colleague is damaged. Same logic for `Co-Authored-By` in commits, "🤖 Generated" footers in PR descriptions, and "via the assistant" suffixes in issue comments.

## Ask Before Posting on the User's Behalf (Non-Negotiable)

**Canonical setting:** `on_behalf_post_mode` (DB-home, default `"draft_or_ask"`, per-overlay overridable) — set with `t3 <overlay> config_setting set on_behalf_post_mode <value>` (add `--overlay <name>` for the per-overlay scope). Three values:

The gate covers colleague-**VISIBLE** posts only. A **draft** (`post_draft_note`) is colleague-invisible — only the user can submit it — so it is **exempt under every mode** and never needs approval; that exemption is the whole point of the setting.

- `"draft_or_ask"` (default) — the agent produces a **draft** without asking (`post_draft_note` returns AUTO_DRAFT and the gate emits a bot→user DM with idempotency key `on_behalf_autodraft:{target}:{action}` so the user can review and publish). Every colleague-VISIBLE action (publish, comment, approve, reply, react…) is BLOCKED until the user records an approval.
- `"ask"` — identical to `draft_or_ask` for drafts (a draft still auto-publishes + DMs the user — it is exempt) and for colleague-visible posts (all BLOCKED until the user records an approval). `ask` does **not** block draft-note creation.
- `"immediate"` — gate is lifted: the agent publishes per the resolved `mode` doctrine without the pre-ask (the user has opted the overlay into trusted unattended posting).

Resolved by `teatree.on_behalf_gate.resolve_on_behalf_verdict(action)` which returns `PASS` / `BLOCK` / `AUTO_DRAFT`. Enforced uniformly inside teatree at three chokepoints: `teatree.core.reply_transport._BaseReplier` (Slack thread reply / Slack channel / GitLab MR comment / GitHub PR comment), `teatree.cli.review.ReviewService` (`post_comment`, `post_draft_note`, `publish_draft_notes`, `reply_to_discussion`, `resolve_discussion`, `update_note`, `delete_discussion`), and `teatree.core.on_behalf_egress.OnBehalfSlackEgress` — the single owner of **every colleague-surface Slack post/react** under the user's identity (review-DONE reactions, the `:merge:` reaction, broadcast outcome reactions, review-nag posts, the `notify post`/`notify react` CLI, `t3 slack react`). `OnBehalfSlackEgress.post/.react` run gate→route→emit→audit in one place; a self-DM (the #1750 `route_token` classifier, fail-closed to colleague on an unknown surface) short-circuits ungated, so a colleague reaction can never bypass the gate while a self-ack stays free. The `PullRequest.approve()` → ✅ reaction signal and the ticket-transition emoji signal route through the same gate (on the separate `slack_reactions` transport).

When the verdict is `BLOCK`, before any post/comment/approval/reaction the agent makes **under the user's identity to a colleague or customer surface** — a GitLab/GitHub PR/MR comment, an issue comment, a PR/MR approve or unapprove, a Slack channel or thread message, a Notion page or comment, an emoji reaction on someone else's message — the agent must obtain the user's explicit approval **first** (via `AskUserQuestion` for ad-hoc agent posts, or by recording an `OnBehalfApproval` for teatree code paths — see below) and publish only after the user confirms.

The gate is **satisfiable, not pure suppression**. The teatree code paths consult `teatree.core.on_behalf_gate_recorded.require_on_behalf_approval`, which mirrors the #953 `DbApproval` / §17.4 `MergeClear` shape: BLOCK verdict + a recorded, unconsumed, exactly-scoped `OnBehalfApproval` row → the post proceeds and the row is consumed single-use (an `OnBehalfAudit` row is written); BLOCK verdict + no recorded approval → the helper raises `OnBehalfPostBlockedError` and the caller surfaces the blocked post to the user — never silently dropped, never posted unattended. **No TTY is required** to satisfy it: a chat-only operator records the approval via `t3 review approve-on-behalf <target> <action> --approver <user-id>` and the next on-behalf attempt publishes. The factory refuses a maker/coding-agent/loop approver id (maker≠checker), so the executing agent can never self-authorize the post it is about to make.

- **Out of scope** (no pre-ask needed): DMs _to the user themselves_ (`Replier.post_dm`), the DailyDigest user thread, the `AskUserQuestion` Slack mirror, the bot→user notify path, and internal-only orchestration writes — our own teatree backlog issues, durable memory, task bookkeeping, the sanctioned `t3 <overlay> ticket clear` / `ticket merge` keystone. The CLI for a bot→user self-DM is **`t3 <overlay> notify send <body> --idempotency-key <key>`** — not `notify post` (which is the gated colleague/channel path that routes via `OnBehalfSlackEgress` and requires `--channel` + `--text`).
- **Relationship to the notify-_after_ rule:** this is the _pre_-gate; the post-on-behalf notification is the _after_ receipt, now a real default-ON DB-home `UserSettings` field `notify_on_post_on_behalf` (default `true`, per-overlay overridable, **no env var**) — set with `t3 <overlay> config_setting set notify_on_post_on_behalf <true|false>` (`--overlay <name>` for the per-overlay scope). After every colleague-visible on-behalf publish, `teatree.core.on_behalf_post_receipt.notify_user_on_behalf_post` DMs the user the destination, a clickable artifact link, and a one-line summary (recorded in the `BotPing` ledger; record-and-proceed — it never blocks or rolls back the post). This durable enforcement **retires** the per-session memory `notify-user-on-every-post-on-behalf` (souliane/teatree#949). Both ship on. The user widens `on_behalf_post_mode` per-overlay (`"draft_or_ask"` → `"immediate"`) once confident the system posts well via `config_setting set on_behalf_post_mode immediate --overlay <name>`; set `notify_on_post_on_behalf` to `false` per-overlay independently — the notify stays on longer.
- **Backward compatibility:** the legacy `ask_before_post_on_behalf` boolean is retired — under the #1775 partition its old `[teatree]` TOML key is ignored on read. Use `on_behalf_post_mode` (DB-home): `t3 <overlay> config_setting set on_behalf_post_mode <value>`, or migrate an existing `~/.teatree.toml` once with `t3 <overlay> config_setting import`.

**Failure mode this prevents:** the agent posts a poorly-worded reply or an approval the user did not intend under the user's name to a colleague, and the user only learns of it after the fact (or via the notify receipt). The pre-gate keeps the user in control of their own voice until they choose to delegate it.

## Never Post PR Comments from Parallel Agents (Non-Negotiable)

MR/PR comment posting (test plans, evidence, review notes) must be **serialized** — never dispatch two parallel agents that both post comments on PRs. Parallel agents cannot check for each other's posts, resulting in duplicate comments.

**Serialized means one poster at a time — it does NOT mean the main agent posts directly (do X, never Y).** "Serialize" governs ordering, not who acts. The main/orchestrating agent is never the poster itself: per § "DISPATCH IMMEDIATELY — the orchestrate-only boundary" below, a colleague-visible publish (`t3 review post-comment`, `post-draft-note`, a test-plan or evidence comment) is dispatched to a single sub-agent, exactly like a code edit — the boundary is about WHO touches a colleague-facing surface, not about the call being short enough to "just do it here." Serialize by dispatching one sub-agent, collecting its result, then dispatching the next — never by having the main agent shortcut the dispatch and run the posting command itself in the foreground.

```python
# do X — dispatch the single posting action to a sub-agent, then stop:
Task(description="Post review finding", prompt="Post an inline `t3 review post-comment` on my-org/my-repo!4120, src/teatree/core/sweep.py line 88: <finding text>. Report the comment URL.")
# never Y — the main agent runs the posting command itself because it's short/serialized:
# Bash(command="t3 review post-comment my-org/my-repo 4120 '<finding>' --file src/teatree/core/sweep.py --line 88")   # FORBIDDEN in the main agent
```

## Evidence Comes From the Deployed Environment (Non-Negotiable)

Before posting any screenshot, PDF, or "proof it works" artifact on an MR/PR/issue, **load `/t3:e2e`** and follow § "Evidence Source Integrity". The short version that every agent must remember even without the full skill loaded:

- **Required:** browser screenshots from the deployed dev/staging URL, OR documents regenerated on the deployed environment after merge + deploy.
- **Prohibited:** golden test PDFs from `build/test-results/` or `src/test/resources/`, `pdftotext` from a local build, screenshots of `localhost`, **and side-by-side comparisons assembled from PDFs extracted at different git commits**.

A passing local test suite is not evidence. The deployed system is the only artifact that proves a user-visible feature works. If the proper evidence requires steps you can't complete this session, say so explicitly in the comment — don't substitute a prohibited source.

**The mandatory-E2E gate is bypassed ONLY by a recorded user approval — never by the agent self-asserting a skip.** For a display-impacting change that genuinely cannot get E2E this session, the single sanctioned escape is the user-authorized bypass command:

```bash
t3 <overlay> ticket e2e-bypass <ticket-id> --approver <human-user-id> --head-sha <full-40-char-sha>
```

It is durable, single-use, and scoped to the ticket + reviewed head SHA; the next ship-gate / §17.4 CLEAR at that exact SHA consumes it once. Maker≠checker is enforced — a `--approver` that is a maker / coding-agent / loop id is refused (#1967), so the implementing agent can never authorize its own bypass. There is no `--skip-e2e` flag and no `approve-on-behalf` path for the E2E gate; `ticket e2e-bypass` with a human approver is the only one. Conversely, once a green run's evidence is POSTED, record the attestation with `t3 <overlay> lifecycle record-e2e-run <ticket-id> --spec <path> --result green --head-sha <sha> --posted-url <evidence-url>` — a run recorded WITHOUT `--posted-url` does not clear the gate.

## Never Modify a Remote Database Without Explicit User Approval (Non-Negotiable)

Never write to, mutate, seed, or delete data in a remote/shared database (dev, staging, production, or any environment the agent did not provision locally) without explicit user approval in the chat for that specific action. This covers direct SQL/`psql`, ORM shells against a remote `DATABASE_URL`, seed/fixture scripts pointed at a remote DB, and API calls whose side effect is a remote write performed solely to set up the agent's own task. Read-only queries are fine. Generating a document or other persisted record on a remote environment is a remote write — ask first. A request to "finish the task" or "get the evidence" is not approval to mutate a shared DB; surface the blocker and let the user decide.

**Testing carve-out (dev only).** When running E2E or other tests against a **dev** environment, creating the agent's own task data — new loan requests, offers, documents, fixture rows — is allowed without per-action approval. This carve-out exists because dev is a testing environment and undeployed work must still be E2E-tested end to end. It is bounded: never mutate, reassign, or delete objects the agent did not itself create (no hijacking other people's records), never run destructive or bulk operations, and never touch staging or production under this carve-out — those still require explicit approval as above. When the dev testing carve-out applies it takes precedence over the general "ask first" rule for the agent's own test-scoped writes; when in doubt about whether a write is test-scoped and self-owned, fall back to asking.

## Verify Repo Visibility Before Filing External Issues (Non-Negotiable)

Before creating an issue, PR, discussion, or any body of content on an external repo, **check the target repo's visibility**:

```bash
gh repo view <owner>/<repo> --json visibility,isPrivate
```

If the target is **PUBLIC**, the body must not contain internal identifiers: customer names, internal GitLab/Jira/Notion URLs, client-specific repo names, ticket IDs from private trackers, CI job/pipeline IDs, local filesystem paths (`/Users/…`, `/home/…`), environment variable values, or internal hostnames. Replace with generic placeholders (`<repo>`, `<namespace>`, `<ticket_url>`, `$T3_WORKSPACE_DIR/<ticket>/<repo>`) before posting.

**Ambiguous destinations need a question.** When the user says "file a bug" without a repo and there are multiple candidates (public upstream vs. private overlay, team repo vs. personal repo), use `AskUserQuestion` to confirm the target before writing the body. Never guess — the cost of asking is low; the cost of publishing internal info is high.

**The authorization to "file a bug" does not authorize posting internal info to a public repo.** User instructions like "file a teatree bug" authorize the _action_ of filing, not the _destination_. A public target always requires a scrubbed body.

## Self-Apply `needs-triage` on Agent-Filed Issues (Non-Negotiable)

`needs-triage` is a maintainer-review gate: the autonomous loop's issue-implementer claim path filters out any open issue carrying it (`IssueImplementerScanner` skips it at selection time, before the claim), so the factory never starts an issue the maintainer has not cleared.

The complication is that the factory files its own backlog issues **as the maintainer's own account** (e.g. `souliane`). The auto-apply GitHub Action keys on the issue author, so it cannot distinguish a human maintainer's issue from an agent-filed one — both look like the maintainer. The author-only Action therefore can't gate agent-filed issues on its own.

The convention closes that gap: **an agent self-applies `needs-triage` by default on anything it files that is not a direct user implementation order.** Concretely:

- When the agent files an issue autonomously (a backlog item surfaced mid-session, a retro/review enforcement gap, a research finding), include `needs-triage` in the labels.
- Omit `needs-triage` only when the user explicitly directed the implementation (the issue is the tracking record for work the user asked for now).
- Teatree's programmatic filing path encodes this: `FilingContext.auto_filed` defaults to `True` and adds `NEEDS_TRIAGE_LABEL`; a user-directed caller sets `auto_filed=False`.

When in doubt, apply `needs-triage` — a withheld issue costs the maintainer one label-removal; an un-withheld one risks the factory implementing something the maintainer never decided to build.

## Leak Remediation — Silent Scrubs (Non-Negotiable)

When remediating a privacy leak on a public repo (force-push to drop PII, delete a comment that exposed a credential, rewrite a branch that leaked internal data), **every public artifact produced during the remediation must be neutral**. Do not name what leaked, do not name that a leak occurred, do not describe the scrub. Announcing the remediation on a public surface amplifies the leak (Streisand effect) — the commit subject, the PR comment, and the branch name are all crawled, cached, and indexed.

## Public-Repo Commit Author Identity (Non-Negotiable)

Commits pushed to a PUBLIC repo (`souliane/*`) must have an author **and** committer email that is a GitHub noreply address — `<id>+<login>@users.noreply.github.com` (e.g. `21343492+souliane@users.noreply.github.com`). A real/deliverable address (any customer/personal domain inherited from local `.git/config` or the XDG global) in public history is a permanent PII leak that GitHub's own "block pushes that expose my email" does **not** catch for third-party domains. The accepted shape is the noreply pattern itself — not one hardcoded login — so any GitHub identity passes and any real email blocks. Private overlay repos are exempt. Enforced deterministically by the pre-push gate `scripts/hooks/refuse-public-push-with-leak.sh` (#730): on a violation it blocks and prints the offending identity plus the `git filter-branch --env-filter` rewrite to the repo's GitHub noreply identity; re-push after the metadata-only rewrite.

**Banned words in any public artifact produced during remediation** (commit subject/body, PR or issue description or comment, release note, changelog, public branch name):

`leak` / `leaked` / `scrub` / `redact` / `real` (as in "real phone number") / `private` / `personal` / `sensitive` / `accidental` / `accident` / specific classes of the leaked data (`phone`, `email`, `password`, `token`, `credential`, `secret`, `key`, `address`, `ssn`).

**Required form:**

- **Scrub commit subject:** neutral verb only. Good: `fix(<scope>): update example values`, `refactor(<scope>): replace placeholder`, `docs(<scope>): refresh example`. Bad: `scrub real phone number`, `remove leaked credential`, `redact personal email`.
- **Remediation branch name:** no signal. Good: `fix/update-examples`, `chore/refresh-docs`. Bad: `fix/scrub-phone-leak`, `hotfix/leaked-token`.
- **Closing a remediation PR:** prefer no comment at all. If one is required, keep it to the shortest neutral phrasing (`Superseded.` / `Not needed.`). Do not explain _why_.
- **Public push description:** same rule. No class-of-data words.
- **Secure explanations** (to GitHub Support, to the user, to incident response) belong only in the corresponding private channel — never in git history or public comments.

**Pre-done grep** (run before claiming the remediation is complete):

```bash
git log --format='%H %s%n%b' <branch-start>..HEAD | \
  grep -iE 'leak|scrub|redact|real|private|personal|sensitive|accident|phone|email|password|token|credential|secret|address'
```

Also grep every PR/issue body or comment authored during remediation. Any hit is Streisand — rewrite the artifact (or delete the comment) before declaring done.

**Why:** A commit subject is as public as the diff, and a PR-close comment is permanent. Describing what was removed tells the next reader exactly what used to be there and where to look in the commit graph. The fix is silent.

## Sub-Agent Limitations

Sub-agents (Agent tool) **lose all loaded skills, MCP access, and shell functions**. By default, never dispatch sub-agents for skill-dependent work. Do all skill-dependent work sequentially in the main conversation.

**Exception:** Skills with `subagent_safe: true` in their YAML frontmatter are pure methodology/guidelines that work without shell functions, MCP tools, or cross-skill state.

**Exception (monitor/work-trigger loop only):** `/teatree-batch` deliberately delegates each ticket's full delivery to a single **singleton** sub-agent, run one at a time. That sub-agent loads the skills it needs via the Skill tool itself, so the "loses all loaded skills" caveat does not apply. This keeps the batch orchestrator's context lean across a long backlog. The singleton constraint is scoped narrowly to the loop that _monitors external systems and triggers work_ — it says nothing about loops in general or sub-agent use in general, and an ordinary session remains free to use loops and sub-agents as usual. The canonical statement (with the full scope boundary) lives in `/teatree-batch` § Rules "Singleton delivery sub-agent (canonical statement)"; this is a reference to it, not a second copy.

**Every raw Agent-tool spawn MUST carry the skill preamble (Non-Negotiable).** A sub-agent dispatched through the raw harness Agent tool gets only its thin subagent-type system prompt — it never receives the SKILL.md bodies the orchestrator has loaded, so it over-provisions for remote e2e, runs raw `playwright`/`glab` instead of `t3`, and ignores overlay rules. Before spawning an e2e / coder / reviewer sub-agent, generate the inline skill preamble with `t3 <overlay> skill-preamble --skills t3:rules,t3:e2e[,<overlay-skill>]` (it concatenates each `SKILL.md` body, resolving framework **and** the active overlay's skills) and **prepend it to the brief**. The dispatched prompt must contain the embedded skill bodies (the `--- SKILL: <name> ---` markers), not a bare task description. A bare brief is the bug this gate exists to catch. (Pinned by `evals/scenarios/orchestrator_embeds_skills_in_subagent_brief.yaml`; the headless dispatch path injects the same bodies via `teatree.agents.skill_injection`.)

**Before delegating platform API work:** Read the relevant platform reference (`t3:platforms`) before writing sub-agent prompts that involve API calls (draft notes, discussions, PR operations). Sub-agents can't read skills themselves — copy the exact API recipe into the agent prompt.

**After a sub-agent completes, re-read any files it modified.** Sub-agents get a forked copy of your file state — their edits don't update your cache. Writing to a file without re-reading first will silently overwrite their changes.

**A blocked sub-agent surfaces the block to the orchestrator — it never silently works around the gate (Non-Negotiable).** When a sub-agent hits a gate it cannot satisfy — a missing skill, an autonomy/on-behalf block, a missing token, a classifier denial, a missing approval — it must **stop and return a structured blocked result naming the reason**, not guess, retry with a different shape, partial-ship, or fabricate a workaround. The structured channel is the result envelope's `needs_user_input: true` + `user_input_reason: "<why>"` (`teatree.agents.result_schema`); a free-prose "I couldn't do X so I did Y instead" is not a surfaced block — it is a swallowed one. The orchestrator, on receiving a blocked result, **escalates** (AskUserQuestion when interactive, or a Slack DM / a `DeferredQuestion` when away) — it never records the sub-agent's run as done, never advances the FSM over it, and never re-dispatches the same blocked unit without resolving the block first. Silent work-around masks the problem and produces invisible partial work; the fix is satisfiable, not pure suppression — once the human supplies the missing skill/token/approval, the unit re-runs and proceeds. (Issue [#1915](https://github.com/souliane/teatree/issues/1915); the agent-facing side of the Classifier Denial Protocol above; pinned by `evals/scenarios/blocked_subagent_escalation.yaml`.)

**Dispatch-prompt hygiene — match the target repo's conventions, don't drift to your own defaults (Non-Negotiable).** A sub-agent prompt that scaffolds a branch or opens a PR must carry the **target repo's** convention, not a habitual default carried over from another repo.

- **Branch name = the repo's own scheme.** If the repo uses a flat `<number>-<type>-<short-description>` scheme with NO prefix, scaffold exactly that — never inject an `ac/` / `a-` / `ac-` prefix the repo doesn't use.

```bash
# do X — flat, repo-native, no prefix (ticket 42, feature add-dark-mode):
git worktree add ../42-feature-add-dark-mode -b 42-feature-add-dark-mode origin/main
# never Y — do not prefix a flat-scheme repo's branch:
git worktree add ../ac/add-dark-mode -b ac/add-dark-mode origin/main   # FORBIDDEN
```

- **No reflexive `--draft`.** Opening a PR for your OWN finished, pushed feature branch (a non-e2e repo) is a real PR, not a draft. Issue `pr create` without `--draft` unless the user or the repo's policy asks for a draft.

```bash
# do X — open the real PR for your own finished branch:
gh pr create --base main --head 42-fix-empty-owner --fill
# never Y — do not default to draft for your own ready work:
gh pr create --base main --head 42-fix-empty-owner --fill --draft   # FORBIDDEN by default
```

Pinned by `subagent_prompt_drift_branch_prefix` and `subagent_prompt_drift_no_draft_default` (`evals/scenarios/subagent_prompt_drift.yaml`).

## Prefer Native Tool APIs Over Filesystem Heuristics

When integrating with tools (issue trackers, CI, chat), prefer their API or CLI over scraping files. File-based approaches break on layout changes, don't handle pagination, and miss metadata.

## Symlink Safety

Never replace a symlink with a real file. `ls -la` first if unsure. If a path is a symlink, edit the target — never delete the link and write a new file.

## Read Before Overwriting a Tracked Config/Dotfile (Non-Negotiable)

A user config file or dotfile (`~/.teatree.toml`, a `dotfiles`-repo file, an XDG `.config` file, `.zshrc`, …) is **authoritative as it exists on disk right now** — even when that on-disk content diverges from the committed version. The user may have made uncommitted edits directly on disk. So before you clobber it you must **read its current content this session**:

- A full **`Write`** that overwrites an existing config/dotfile, OR a **`git checkout` / `git restore`** that restores a tracked config from a committed version, discards the live on-disk content. Do **not** do either blind — `Read` the file first, confirm what you intend to change, then re-issue the write.
- **Uncommitted-on-disk beats committed.** Never "restore the config from git to a clean state" without first reading the working-tree copy — the committed version is NOT the source of truth for a user config; the file on disk is.
- This is the file-write sibling of § "Read the Canonical Source Before a Structural Action" and § "Read the Canonical Source Before Fixing a Conformance Bug": the live artifact is the spec; read it before you act on it.

**Deterministically enforced.** The PreToolUse gate `handle_block_config_overwrite` (`hooks/scripts/config_overwrite_guard.py` + `teatree.core.gates.config_overwrite_guard`) refuses a blind `Write` over an existing config/dotfile and a blind `git checkout`/`git restore` of one when the path was not read this session (it consumes the existing `<session>.reads` capture). Reading the file first clears it. Never-lockout escapes: a per-call `[config-overwrite-ok: <reason>]` token, the `[teatree] config_overwrite_gate_enabled = false` kill-switch (`t3 <overlay> gate config-overwrite disable`), and the shared `_fail_open_or_deny` chain.

**Failure mode this prevents.** An agent overwrote `~/.teatree.toml` (a symlink into the user's dotfiles repo) with a blind `Write`, and on another occasion nearly restored a config from git without reading the live copy — both would have silently destroyed the user's uncommitted edits.

## Shell Alias Safety

Use `command rm`, `command cp`, `command mv` in Bash tool calls to avoid zsh interactive aliases that hang. Also `gs` is aliased to `git status` — use `command gs` for GhostScript.

## Skill File Writes Require a Git Repo

Never modify skill files outside a git repo. Resolve real path with `readlink -f`, verify `git rev-parse --git-dir` succeeds. Changes to non-git copies are silently lost.

## Fix TeaTree/Skill Bugs Immediately

When a teatree or skill infrastructure bug is discovered during any task, fix it immediately as first priority. Never defer to focus on the user's task — broken infrastructure causes cascading failures.

## Teatree Extension Point Changes Must Update All Registered Overlays (Non-Negotiable)

When you add, change, or remove a hook on `OverlayBase` (e.g. `get_required_ports`, `get_port_env`, `get_health_checks`, `get_readiness_probes`, `get_base_images`, …) on this machine, you must in the same session update **every overlay registered locally** to adopt the new contract — even when the change is "additive" with a working default.

**Why:** the teatree codebase is overlay-agnostic and CI cannot see the user's installed overlays. A "default returns empty/false" is silent — the overlay keeps shipping, but with the wrong runtime behaviour (port collisions, skipped readiness checks, missing health invariants). The drift only surfaces when the user runs the new command and gets a confusing failure with no obvious root cause.

**How to apply:**

1. Enumerate registered overlays on this machine: `uv run python -c "from importlib.metadata import entry_points; [print(ep.value) for ep in entry_points(group='teatree.overlays')]"`. Treat the output as the authoritative list — not memory, not assumptions about which overlays are installed.
2. For each overlay, decide whether the new hook needs an explicit override and, if so, implement it in the same PR (or a paired PR opened in the same session). Do not file a "later" ticket — see § "Do Work Now, Don't Defer to 'Later' Tickets".
3. Cite the overlay PR(s) in the teatree PR description so reviewers can confirm the chain landed end-to-end.

**Past failure mode this rule prevents.** A wave of teatree PRs added several overlay hooks. A registered overlay kept running on the no-op defaults — multiple worktrees collided on the same backend port because `get_required_ports` returned an empty set, and `worktree ready` reported green even when nothing was serving. The teatree side looked clean; the symptom only showed up downstream after weeks.

## Do Work Now, Don't Defer to "Later" Tickets (Non-Negotiable)

When the user asks for work that is actionable in the current session — a small skill edit, a one-file CLI addition, a test fix, a rule promotion — **do it in the current response**. Do not propose filing a ticket for "later", do not frame the work as a follow-up suggestion, do not ask for confirmation to proceed on obviously in-scope work. Deferring concrete work to a ticket queue is the single most common way an agent wastes the user's time — the ticket piles up, context evaporates, and work that could have shipped in the same PR now takes a fresh session.

**Do it now means RUN the command — never hand the steps back (do X, never Y).** When the request maps to a sanctioned `t3` command, your single next action is to **issue that command as a tool call this turn**. Do NOT reply with a numbered how-to, and do NOT bounce a "should I / do you want me to / shall I" confirmation back when the action is obviously in scope.

```bash
# "help me create the worktree for this ticket" → RUN it, do not explain it:
t3 <overlay> workspace ticket <id>           # or: t3 <overlay> worktree provision <id>
# never: a prose list of "1. cd …  2. git worktree add …" handed back to the user
# never: AskUserQuestion("should I create the worktree?") on obviously in-scope work
```

The same applies to any runnable ask — running tests, opening a PR, fetching a ticket: pick the canonical `t3` command and run it. Asking "should I?" on in-scope work reads as stalling. Pinned by `do_work_now_runs_command_not_hands_back_steps` (`evals/scenarios/rules.yaml`).

**Never punt resolvable work back to the user as a "decision/data you must provide."** When a step the user delegated is something you can resolve yourself — derive the value, look it up in a file/config/git, compute it, pick the determinable-best option — **resolve it and proceed**; do not bounce it back as "I need you to tell me X" or "please decide Y." The test is the same sharp one from § "Always Use AskUserQuestion for Questions": _can I reach the best outcome by doing the work?_ If yes → do it, never punt. The only things that legitimately go back to the user are a **fact you genuinely cannot obtain** (a secret, a private URL, a value living only in the user's head) or an **authorization for an irreversible/outward-facing action** — never a decision or datum you could have determined yourself. Punting resolvable work is the inverse failure of deferring it to a ticket: both make the user do the agent's job. This is the named pattern the user calls "successfully failing" — completing the _motion_ of asking while leaving the actual work undone.

**Banned patterns when the work is actionable in this turn:**

- "I'd suggest filing a ticket to…"
- "Follow-up (not in this PR)…"
- "Want me to open an issue for …?"
- "As a separate ticket, we should …"
- "File tickets for (a) and (b), or one combined…?"
- "separate bug worth fixing later"
- "worth filing later"
- "out of scope for this PR" (when the fix is small enough to bundle)
- "I'll note this for follow-up"

**Defaulting to "later" without asking is treated as "I discovered a bug but I don't care."** A finding that surfaces during a session must result in **action this turn** — either the fix lands, or the user is asked which lane it goes into. Silent deferral is not a lane.

**When deferral IS legitimate** (narrow set):

- The user explicitly asked for planning only, not execution.
- The work requires an external dependency that is unavailable right now (missing auth, missing approval from a third party, missing DB snapshot).
- The work would genuinely balloon this change into scope creep — and even then, ask the user directly, don't announce a ticket.

**When in doubt, do the work.** A tiny PR adding the fix alongside the main change is always preferable to a stand-alone ticket that lives in the backlog for weeks.

**Bundle Bugs Found Mid-Session into the Current PR (Non-Negotiable when in `auto` mode).**

When you encounter a bug, broken behavior, or rough edge during any session — fix it on the spot, in the current PR if at all reasonable. Do not narrate the finding as a deferral, do not propose filing tickets, do not ask "should I fix this in a separate PR?" before doing the obvious work. Work unattended.

Decision rubric (apply silently — don't narrate to the user):

| Fix size | Action |
|---|---|
| **Small (≤ ~50 LOC, no architectural decisions)** | Bundle into the current PR. Skip the "Isolate Unrelated Fixes" rule from `t3:ship` — small fixes have lower scope-creep cost than coordination cost. |
| **Medium (related domain, fits the current ticket's spirit)** | Still bundle if the PR title can fairly cover it (e.g., assorted shipping-flow bug fixes during a CLI refactor). Mention in the PR body so reviewers see it. |
| **Large (architectural, cross-cutting, or genuinely orthogonal)** | Create a worktree + PR immediately, implement, ship. No new ticket. |
| **Truly large work that cannot fit a session** | File a ticket and leave it. Last resort. |

**Only stop and ask when:**

- The fix has security/destructive blast radius (DB drops, force-push to default, secret rotation).
- The architectural choice has multiple equally valid options.
- The work is genuinely big enough to need its own ticket _and_ the user hasn't opted into auto mode for this overlay.

This rule reinforces "Do Work Now" — the bundling decision is part of doing the work, not a separate question to ask.

**Repo mode governs proactive-fix latitude (one source of truth).** Whether the agent fixes unrelated rough edges proactively or only flags them depends on who owns the repo. Instead of every skill re-deciding, run `t3 tool repo-mode` (cached 7 days; `--json` for machine reads; the DB-home `repo_mode` setting — `t3 <overlay> config_setting set repo_mode <solo|collaborative>` — overrides the `git shortlog` heuristic). `solo` → the bundling rubric above applies as written (fix proactively). `collaborative` → bias toward _flagging_ unrelated findings (PR comment / follow-up issue) rather than touching code another contributor owns; still fix anything inside the current ticket's own scope. The `auto`-mode bundling rubric is the `solo` behavior; `collaborative` is the conservative variant of the same rubric.

**When genuinely unsure, ASK — never silently defer.** If the fix is borderline (small but truly orthogonal, or medium-sized but the current PR is already large), present three explicit options to the user via `AskUserQuestion`:

1. **Fix right now and bundle into the current PR** (default — pick this unless reason not to)
2. **Add to the session TODO list** (fix later this same session, before wrapping up)
3. **File as a separate issue** (truly out-of-scope or would balloon the change)

Use options 2 and 3 only when there is a concrete reason against option 1. Asking is acceptable; silently writing "worth filing later" and moving on is not.

## Contribute Mode: Promote Findings to Skills, Not Personal Memory (Non-Negotiable)

When `contribute` is `true` (a DB-home setting — `t3 <overlay> config_setting set contribute true`), retro findings and cross-cutting rules **must land in teatree skill files**, not in the agent's personal memory/config. Personal memory is the fallback for user-specific facts — paths, credentials, editor preferences, one-machine workflow choices. For anything that would help another user of these skills, write to the skill.

**Before writing a feedback/guardrail to personal memory, check:**

1. `contribute` set to `true` (`config_setting set contribute true`)? → yes almost always makes this a skill edit.
2. Does the rule encode a guardrail, pattern, or "do this not that"? → skill.
3. Would another user benefit? → skill.
4. Is it a user preference (tone, formatting) or environment fact (path, credential)? → personal memory is legitimate.

**Promote means edit an existing skill.** Pick the best-fit existing skill (`/t3:rules`, `/t3:next`, `/t3:ship`, etc.) and insert the rule there. Do not invent a new skill for a single rule — that fragments the skill graph.

## Autonomous Directive Adoption

This is the meta-policy that gives the "promote findings" rule above its trigger. It has no clean code home — it describes how to read the user's intent, which is methodology, not a deterministic gate — so it lives here as prose.

In contribute mode (`contribute` set to `true` via `config_setting set contribute true`), a user statement of the form "it should…" / "you should…" / "the agent shouldn't…" about agent behaviour is read as a request to adopt that behaviour into teatree itself — a skill edit where the behaviour is methodology, a code change (hook deny, FSM condition, CLI rejection) where a deterministic home exists. It is not a one-off instruction to satisfy for the current task and forget. The expected response is to make the teatree change in the same session, the same way change 1 of any retro finding lands: act on it, rather than asking "should I make a ticket or just fix it?".

The session default in contribute mode is full autonomy. The agent carries the work to completion — implement, test, commit — without pausing to ask permission for in-scope work that the "Do Work Now" rule already covers. A clarifying question via `AskUserQuestion` is reserved for the case where the agent is genuinely unsure: a debatable architectural choice with several equally reasonable options, an ambiguous destination, or a directive whose scope the agent cannot infer from context. Uncertainty is the signal to interrupt; the absence of uncertainty is the signal to proceed. Treating every "should" as a question to bounce back is the failure this policy names — it converts a standing behaviour change into conversational acknowledgement that evaporates with the session.

When the directive is genuinely ambiguous about _where_ it belongs (skill prose vs. code, which skill, which overlay), that ambiguity is itself the trigger for one `AskUserQuestion` — not for deferral, and not for a silent guess.

## Ask About Auth Before External Service Integrations

When implementing features that require an external service (Notion, Slack, CI, etc.), ask "how do you authenticate with this service?" BEFORE writing any code. The answer (direct API token, CLI auth, MCP tool, OAuth, etc.) determines the entire architecture. Skipping this question leads to multiple implementation pivots.

**Zero user effort when the user says "I do nothing."** When the user signals they want a hands-off path — "I do nothing", "set it all up for me", "I shouldn't have to touch anything" — that is a directive to make the **agent** perform every step it possibly can, leaving the user with zero manual operations. Do not hand back a checklist of steps for the user to run; run them. The only residue allowed to fall to the user is the genuinely un-automatable: a secret only they hold, an OAuth consent screen only they can click, an authorization the harness blocks the agent from self-granting. Everything mechanically doable by the agent (writing config, running `t3` commands, editing files it can edit, retrying) the agent does. This is the same posture as the Classifier Denial Protocol's "the agent **attempts** the edit to `~/.claude/settings.json` itself, falling back to a paste-ready snippet only after the harness blocks the write" — the manual fallback is the last resort, never the default.

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

## Always Create Tasks

On **every prompt**, use `TaskCreate` to create tasks before doing any work — even for a single task. Mark each task `in_progress` when starting, `completed` when done. Never skip this. Visible task tracking prevents forgotten steps and shows the user your progress.

- **Simple tasks** (1-2 steps): a brief bullet list in the response is sufficient.
- **Complex tasks** (3+ steps): use the task tracking tools for each step, update status as you go.
- **Never skip this.** If you find yourself doing 3+ things without a plan, stop and create one.

## Mid-Task Interrupts (Non-Negotiable)

When a new request arrives while you are in the middle of work, **do not silently pivot**. Default to finishing the current task, queue the new one, and tell the user.

1. **Add the new request as a task** (`TaskCreate`) before doing anything else.
2. **Decide whether it blocks the current task.** Blocking means the new request invalidates the in-progress work, fixes an actively-broken state, or the user explicitly says "stop and do this first." Routine new requests do NOT block.
3. **Tell the user the order.** "I'll finish [current task], then handle [new task]." One sentence — don't bury it.
4. **Default = finish what you were doing.** Silent pivots abandon the in-progress context the user was tracking and force them to re-prompt to recover it.

This rule does NOT override `User Instructions Are Priority 1` — explicit corrections like "skip tests, push now" are blocking by definition. The interrupt rule handles the routine case where a new request looks important but isn't tied to the current state.

## Background Long Operations (Non-Negotiable)

Any operation expected to run longer than ~15 seconds — CI/pipeline watches, full test suites, heavy analysis or research, multi-step API sweeps — must **not** block the foreground. A blocking foreground call freezes the main agent: it stops reading new user messages until the call returns, so the user is ignored for minutes.

Background it instead:

- Arm a **Monitor** to watch a long-running command/pipeline — its events arrive as notifications and wake you, so the foreground stays free. This is the canonical teatree mechanism for watching a long op without blocking (the loop uses it), and the preferred choice for a CI/pipeline or test-suite watch.
- Dispatch a **background sub-agent** (Task tool) for the long unit of work, then keep handling new input while it runs. A multi-file investigation or a cross-cutting refactor **is** a "long unit of work" — dispatch it to a `Task` sub-agent, not to a backgrounded one-line `run_in_background` grep (that flag is reserved for a single shell command). You can dispatch a `Task` **even when the exact shell command is unknown** — describe the work in plain language in the Task prompt (e.g. "Replay all migrations against the large database dump"); do not block by asking the user for the precise command, since the Task path needs no shell invocation up front.
- For a single shell command, pass `run_in_background: true` to Bash rather than waiting on it inline.

Concretely, to watch a running CI/pipeline (which blocks for minutes) while staying free for new messages, your single next action is one of: arm a `Monitor` on the pipeline (`gh run watch` / `glab ci status`), dispatch a `Task` sub-agent to watch it and report back, or run the watch as a single `Bash` call with `run_in_background: true` — **never** a blocking foreground `gh run watch` / `glab ci status --watch`. The same disjunction covers a full test suite: background the `pytest` run, never block the foreground on it.

**CI-gated work: the orchestrator owns the trigger + watch; dispatch sub-agents only to FIX (do X, never Y).** When the next step is gated on a CI run — a manual job that must be triggered, or a pipeline whose result decides what happens next — do NOT brief a sub-agent to "trigger the job, watch it, and return on pending". A sub-agent told to wait on CI arms a watcher and **comes to rest mid-wait**, and a rested sub-agent cannot be resumed with its context (re-dispatch starts fresh, losing it). So the trigger-and-watch loop belongs to the orchestrator: trigger the job yourself (triggering CI is orchestration, like kicking off a pipeline), watch it via a `Monitor` or the tick cadence, and dispatch a sub-agent only to FIX a CONFIRMED failure — with the failing trace already in the brief.

**A sub-agent that armed a watcher and "came to rest" is mid-wait, NOT done (do X, never Y).** Its "came to rest" task-notification may fire more than once, and it may still push more work when its watcher fires. So do NOT spawn a fresh sub-agent for the same unit while a prior agent's watcher is armed — the two collide on the same branch/worktree and duplicate the work. Collect the armed watcher's result, or re-trigger the job yourself; re-dispatch a fresh agent only once the prior unit is genuinely terminal.

The main agent's job during a long operation is to stay responsive — collect the result when the background unit reports back, not to sit blocked on it. This rule is pinned by the `background_long_operations_*` behavioral evals (`evals/scenarios/background_long_operations.yaml`).

**DISPATCH IMMEDIATELY — the orchestrate-only boundary (do X, never Y).** When you are the main/orchestrating agent and the work in front of you is a long unit (multi-file investigation, cross-cutting refactor, an extensive test suite, anything > ~15s), your single next action is to **dispatch it to a sub-agent**, NOT to start doing it yourself in the foreground. Run the dispatch tool call NOW — do not narrate what you would do, do not first grep `src/` yourself, do not open the file and start editing.

**Size and urgency are NOT exemptions — a one-line `.py` fix the user wants NOW is still dispatched, never hand-edited (do X, never Y).** The boundary is about WHO touches production code (a worktree sub-agent), not about how big the change is. "It's only one line" and "the user wants it now" are the two rationalizations that produce the drift — both are wrong: the orchestrate-only boundary holds for a one-character edit exactly as it holds for a refactor. So when a reviewer hands you a one-line `src/...py` bug to fix RIGHT NOW, your single next action is the `Task`/`Agent` dispatch below — **never** an `Edit`/`Write` against the `.py` file in the main agent, and never `git commit`/`pytest` on it in the foreground.

```python
# do X — the one-line fix is dispatched to a worktree sub-agent (the orchestrator never touches the .py):
Task(description="Fix get_active_session", prompt="In a fresh worktree off origin/main, fix the one-line bug in src/teatree/core/session.py ... commit, report branch+sha.")
# never Y — the orchestrator edits production code itself because the fix is "small" / "urgent":
# Edit(file_path="src/teatree/core/session.py", ...)   # FORBIDDEN in the main agent — size/urgency is no exemption
```

**Publishing a colleague-visible artifact is in scope too, regardless of how fast the call itself runs.** Posting an MR/PR/issue comment, a review finding, or evidence is a one-shot CLI call that finishes in under a second — but the boundary is about WHO acts on a colleague-facing surface, not about call duration. Dispatch it the same way as a code edit; see § "Never Post PR Comments from Parallel Agents" above for the worked `t3 review post-comment` example.

1. **Dispatch the unit to a `Task` (or `Agent`) sub-agent in this same turn.** The prompt fully describes the bounded unit of work in plain language — the file/subsystem, the bug, the expected outcome. Do this even when you don't yet know the exact shell command (the `Task` path needs no shell invocation up front).
2. **Never run the long unit yourself in the foreground.** Do NOT `grep -r … src`, `rg … src`, `find … -name`, open-and-`Edit` the `.py` file, or `Write` the `test_*.py` yourself when the unit is delegable — the orchestrator stays thin.
3. **Keep moving while it runs** — pick up the next ticket, or arm a `Monitor` on it. Do NOT sit in a foreground `while/until … sleep … pgrep` poll loop waiting on the sub-agent's process.
4. **Collect the result when the sub-agent reports back** — then re-read any files it modified (see § "Sub-Agent Limitations") before acting on its output.

**Dispatching is the WHOLE action — after the dispatch your turn is DONE; do NOT then "help" by doing the work in the foreground (do X, never Y).** The recurrence under heavy load is subtle and worse than skipping the dispatch: the agent fires the `Task`/`Agent` dispatch (so a positive "did you delegate" check passes) and then, instead of stopping, **keeps going in the same turn and re-implements the very unit it just delegated** — `find`/`grep`/`ls` to locate the file, `Write` the test, `Edit` the `.py`, `git checkout -b`, `pytest`, `git commit`. That is NOT delegation; it is a token delegation wrapped around foreground execution, and it trips every orchestrate-only boundary the dispatch was meant to honour (the sub-agent and the main agent now both edit the same code; the work is duplicated; the budget blows). **A dispatch you immediately undo by hand-doing the work is worse than no dispatch.** So once the dispatch (or the parallel fan-out of N dispatches) is issued, the orchestrator's turn ENDS — it does not locate files, write tests, edit `.py`, create branches, or run `pytest`/`git commit` for that unit afterward. The next foreground action is collecting the sub-agent's reported result, never re-doing its job.

```text
# do X — dispatch (or fan out N dispatches), then STOP this turn:
Task(description="Fix get_active_session", prompt="In a fresh worktree … fix the one-line bug … commit, report branch+sha.")
# … turn ends here. Nothing else. Wait for the sub-agent's result.
# never Y — dispatch, then re-do the same unit by hand in the foreground:
# Task(description="Fix get_active_session", prompt="…")
# Bash(command="find /app -name session.py")     ← FORBIDDEN: re-locating the delegated unit
# Edit(file_path=".../session.py", …)             ← FORBIDDEN: hand-doing what you delegated
# Bash(command="pytest … && git commit -m …")     ← FORBIDDEN: running the delegated unit yourself
```

**Post-dispatch checklist — the dispatch is a HARD turn boundary; re-INVESTIGATION is forbidden too, not only re-implementation.** The drift hides in a softer move than re-editing: after the dispatch, the agent "just has a quick look" — `find`/`cat`/`ls`/`grep`/`rg`/`Read`/`Glob` to inspect the file it just delegated — and that read-only peek slides into editing, testing, and committing the unit in the foreground. A read-only probe of a delegated unit is NOT a harmless look; it is the first step of re-doing the work, and it has no purpose for the orchestrator (the worker reads its own files). So treat the dispatch as the **last tool call of the turn**. Concretely, once the dispatch (or the N-way fan-out) is issued:

1. **The very next tool call is forbidden if it touches a dispatched unit's surface — in ANY tool.** Not just `Edit`/`Write`/`pytest`/`git commit` (re-implementation), but also `find`/`cat`/`ls`/`grep`/`rg`/`head`/`tail` in `Bash`, and `Read`/`Glob`/`Grep` (re-investigation). The orchestrator does not locate, open, inspect, diff, or test a file it just handed to a worker.
2. **The only permitted next foreground actions are dispatcher work, never executor work** — fanning out the NEXT ticket's worker, arming a `Monitor`, or surfacing a `(b)`/`(c)` decision via `AskUserQuestion`. Each is dispatch/route/ask, never do.
3. **End the turn.** When there is no further ticket to dispatch and no decision to surface, the turn is over — STOP and wait for the workers' reported results. Filling the post-dispatch silence with foreground `find`/`cat`/`Edit` is the recurrence; an empty post-dispatch turn is the correct shape.

The test: after a dispatch, if your next tool call names or touches the file/module/ticket you just delegated — to read it OR to write it — you have re-entered executor mode. The dispatch was supposed to be the whole action; honour it by stopping.

Worked dispatch — a one-line fix a reviewer found, delegated rather than edited in the foreground:

```text
Task(
  description="Fix get_active_session",
  prompt="In a fresh worktree off origin/main of this repo, fix the one-line bug in "
         "src/teatree/core/session.py: get_active_session() returns None instead of "
         "raising SessionNotFound when no active session exists. Add a fail-before/"
         "pass-after regression test, run the suite, commit, and report the branch + sha.",
)
```

Worked dispatch — a long multi-file investigation, delegated rather than grepped in the foreground:

```text
Task(
  description="Investigate the subsystem",
  prompt="Run a deep multi-file investigation across the codebase: trace how the "
         "overlay resolver is called from every call site, map the data flow, and "
         "report findings with file:line citations. Do not change code.",
)
```

Arm a Monitor to await a dispatched sub-agent instead of foreground-polling its process:

```bash
t3 monitor watch --label subagent-42 --until-exit   # wakes you on completion; foreground stays free
```

## Always Use AskUserQuestion for Questions

**Never ask questions inline in text responses.** Always use the `AskUserQuestion` tool — it gives the user a structured UI to respond and prevents questions from being buried in output.

**One decision per question (do X — never Y).** Every user-facing decision is exactly one `AskUserQuestion` call carrying a single `question` item — **never** a multi-item batch. A prompt like "approve A1, B3, C4, Z40?" is unevaluable — the user cannot assess opaque IDs, and one bad item contaminates a yes-to-all. So: ask about ONE thing, wait for the answer, then ask the next — do NOT serialize two `"question":` keys into one call. Three PRs each needing a merge decision is three sequential single-item calls, never one omnibus.

**When N decisions are undecided, your single next action is ONE `AskUserQuestion` with ONE question for the FIRST decision — never a batch (do X, never Y).** This holds precisely under load, where the tempting shortcut is to cram all N into one call "to save a round trip". That batch is the exact drift this rule forbids. Surface decision #1 now; the rest come one at a time after each answer.

```python
# Three things are undecided (target branch, commit type, squash?). do X — one call, one question, the FIRST decision:
AskUserQuestion(questions=[{"question": "Which target branch — main or develop?", "options": [...]}])
# never Y — do NOT batch the three undecided items into one multi-question call to "save a round trip":
# AskUserQuestion(questions=[{"question": "target branch?", ...}, {"question": "commit type?", ...}, {"question": "squash?", ...}])  # FORBIDDEN
```

A live session has a hook backstop (the PreToolUse `handle_warn_batched_questions` advisory nudges when a call carries >1 question), but the backstop is a WARN, not a block — splitting the ask one-at-a-time is your behaviour to get right, not the gate's to fix.

**Each question carries plain-language detail.** The question text must state, in the user's own vocabulary: what the change or decision is, the specific risk or trade-off that matters, and an honest read of it. The options must be the real decision paths for that one item (e.g. "build the safety test first" / "merge now" / "hold"), not a bare yes/no.

**After you ask a decision via `AskUserQuestion`, STOP and wait for the answer; your turn ends; never re-ask the same decision (do X, never Y).** The `AskUserQuestion` tool call IS the whole action for that decision: issuing it ENDS your turn and you WAIT for the answer. Under load the drift the metered lane caught is the opposite — the agent asks decision #1 (the target branch), does NOT get an answer in the same turn (it never does — the answer arrives on the NEXT turn), and so RE-EMITS the SAME decision turn after turn, looping on #1 and never reaching #2/#3. That re-ask loop is wrong: the answer is not missing, it simply has not arrived yet because your turn is over. So once you have asked one decision, do not ask it again, do not "make sure it landed", do not re-pose it a second time — stop, and let the answer come back. Surface the NEXT decision only after the current one is answered (the one-at-a-time walk-through above). A second `AskUserQuestion` call re-asking a decision you already asked is the failure this pins.

```python
# do X — ask ONE decision, then your turn is DONE; wait for the answer:
AskUserQuestion(questions=[{"question": "Which target branch — main or develop?", "options": [...]}])
# … turn ends here. Do NOT re-ask. The next decision comes AFTER this one is answered.
# never Y — re-emit the SAME decision because the answer "hasn't landed" (it just arrives next turn):
# AskUserQuestion(questions=[{"question": "Which target branch — main or develop?", ...}])  # FORBIDDEN re-ask
```

**Do the best autonomously — never ask a determinable quality/approach/scope decision (do X, never Y).** `AskUserQuestion` exists for things you genuinely cannot decide alone — it is NOT a place to offload a judgment call you can resolve by doing the best work. When a quality / approach / scope choice has a _determinable best answer_ — "fix all the issues or just some?", "which of these approaches?", "make it thorough or just okay?", "should I do the heavy/full version?" — the answer is always **do the best**: pick the best option, do the full/thorough work even when it is a lot more work, and briefly STATE the choice you made. Do not hand that decision back to the user. The user repeats this daily; deferring a determinable-best decision reads as the agent making the user do the agent's job.

```python
# Determinable-best scope/approach decision — do X: pick the best, do the full work, state it. NO AskUserQuestion.
# "Fixing all five related issues is the best outcome and fully determinable — done all five; stating it here."
Edit(file_path="module.py", ...)   # do the thorough fix
# never Y — do NOT defer a decision you can resolve by doing the best work:
# AskUserQuestion(questions=[{"question": "Fix all five issues or just the one the ticket names?", ...}])  # FORBIDDEN
```

**The boundary — what you SHOULD still ask (do ask Z).** Asking is correct, not a violation, when the blocker is something you genuinely cannot know or decide alone:

- a **fact you cannot obtain** — a private URL/endpoint, the intended audience, a credential/token, a value that lives only in the user's head and is in no repo/config you can read;
- **authorization for an irreversible or outward-facing action** — a force-push to a default branch, a destructive DB op, a post/PR/merge that leaves the machine (per the always-gated and on-behalf rules below).

The test is sharp: _can I reach the best outcome by doing the work?_ If yes → do it, don't ask. If the blocker is a missing fact or an authorization gate → ask via `AskUserQuestion`. "I could resolve this by doing the best work" is RED; "I truly cannot know this / am not authorized" is GREEN. Pinned by `do_the_best_without_asking` and `legitimate_missing_fact_question_is_allowed` (`evals/scenarios/do_the_best_no_tech_debt.yaml`).

**Don't abandon an in-progress one-by-one walk-through.** If you have started taking the user through items one at a time, finish the sequence. Do not switch to autonomous work mid-walk-through and leave the remaining items dangling.

**Why this matters beyond UX:** when Slack is configured, the `PreToolUse` hook automatically mirrors every `AskUserQuestion` call to the user's Slack DM. The user can see pending questions on their phone even when away from the terminal. Plain-text questions bypass this mirror and are invisible on Slack.

**This is hook-enforced, not a remembered preference (#807).** A `Stop` gate (`handle_enforce_structured_question` in `hook_router.py`) inspects the final assistant turn: if it poses a user-directed decision question inline in prose with no `AskUserQuestion` tool call in that turn, the Stop hook **blocks** and instructs the agent to re-ask through the structured tool. There is no `relax:` escape — it is a gate, like the other Stop-time gates. Detection is a precision-tuned heuristic (`?` + a second-person/decision cue, or a "let me know if/whether …" soft-ask; fenced code stripped first). A bare `?` (rhetorical aside, explanatory sentence, echoing the user) does not trip it. **Scope:** the gate only enforces on a loop-driven turn (`_session_drives_loop`: this session owns the tick, or there is no live owner) — that is where an inline question is invisible (it reads as a log line, so the decision is lost). In an attended interactive session that a _different_ live owner is driving, a human is reading the prose, so the gate is skipped; an unknown/unreadable ownership signal fails safe and keeps it firing. See `BLUEPRINT.md` § "Structured-question Stop gate" for the full heuristic and rationale.

**Away-mode (24/7 dual question-mode, #58).** When `t3 teatree availability show` resolves to `away`, the PreToolUse hook converts the `AskUserQuestion` tool call into a durable `DeferredQuestion` row instead of waiting on a TTY — the §807 gate stays satisfied because the tool_use block is still recorded. Use `/t3:availability` for the configuration surface (`t3 teatree availability away`, `t3 teatree availability present`, `t3 teatree availability auto`, `t3 teatree questions list`, `t3 teatree questions answer`, `t3 teatree questions dismiss`) and BLUEPRINT.md §5.6.3 + §17.1 invariant 9 for the spec.

### Receiving a structured answer (apply X — never apply a stale Y)

Asking is half the contract; **applying the right answer** is the other half. A structured answer arrives one of two ways: as `additionalContext` injected this turn ("Your AskUserQuestion (#N) was answered by the user on Slack: `<value>`. Apply it now.") or as the local TTY result of the call. When it arrives:

1. **Apply ONLY the answer that cites the currently-live question** — match the cited `#N` to the question you actually have open this turn, then act on it directly (run the command with the chosen value). Do NOT re-ask a question that has already been answered.
2. **Ignore a stale already-answered reply.** A raw Slack DM that arrives as ordinary chat ("User replied on Slack at `<ts>`: `1`") AFTER you already resolved that question locally found **no live row** — it is NOT the AskUserQuestion result. Do not switch course on the strength of it; continue the action you already started from the real answer.
3. **Ignore a superseded-generation reply.** If you asked Q1, then replaced it with a newer Q2 (Q1 marked stale), a reply citing the OLD Q1 is dead — apply only the answer to the current Q2. The cited `#N` disambiguates which generation the answer belongs to.
4. **One answer resolves one question.** A single injected answer applies to exactly the one question it cites — never fan it out across other open or already-closed questions.

The failure mode this prevents: flipping a deploy target / region mid-action because a late or superseded "1"/"yes" landed in chat after the real decision was already made and acted on. Pinned by `evals/scenarios/askuserquestion_slack_resolution.yaml` (`applies_injected_askuserquestion_answer`, `does_not_apply_stale_locally_answered_reply`, `does_not_apply_superseded_generation_reply`).

## Never Introduce Tech Debt; Reduce It (Non-Negotiable)

Doing the best (the rule above) extends to HOW the work lands, not only whether you ask. **Solve the underlying problem cleanly — never introduce tech debt to finish faster, and take any opportunity to reduce existing debt in the area you touch (do X, never Y).**

When a fix trips a real linter/type error, a failing test, or an awkward edge, the right move is to fix the _cause_. The drift this pins is the fast-but-dirty shortcut that papers over the cause to go green sooner:

- a lint/type **suppression** — `# noqa`, `# type: ignore`, a new `per-file-ignores` entry, a relaxed ruff rule;
- a **TODO/FIXME-for-later** left in code instead of the fix;
- a **workaround** that masks the cause rather than removing it;
- a **weakened, xfailed, or skipped test** (`pytest.mark.xfail` / `.skip`) slapped on instead of making the assertion pass honestly;
- lowering a **coverage threshold** or adding a file to a coverage/omit list.

```python
# Linter complains the function is too complex — do X: refactor so it passes on its merits.
Edit(file_path="module.py", old_string="<the tangled function>", new_string="<the cleanly split version>")
# never Y — do NOT silence the cause to finish faster:
# Edit(file_path="module.py", new_string="def f(...):  # noqa: C901  TODO: refactor later")  # FORBIDDEN
# Edit(file_path="test_module.py", new_string="@pytest.mark.skip  # flaky, fix later")        # FORBIDDEN
```

**Reduce debt when you are already there.** If the file you are fixing carries existing debt — a stale suppression you can now remove, a duplicated helper you can collapse, a misleading name you can rename — clean it in the same change. You are already in the file; leaving the debt for "later" is the deferral the rule above forbids, applied to code health.

**The carve-out is the same as everywhere else: ASK, don't suppress silently.** If a clean fix genuinely needs significant refactoring or a structural config change (a ruff rule, a coverage floor), surface the trade-off via `AskUserQuestion` with concrete options — never quietly add the suppression and move on. Introducing debt is a decision the user makes explicitly, not a shortcut the agent takes to save time. Pinned by `no_tech_debt_fixes_cleanly_not_a_suppression` (`evals/scenarios/do_the_best_no_tech_debt.yaml`); the project-level bar is `CLAUDE.md` § "No tech debt without explicit approval".

## Publishing Actions Are Mode-Conditional (Non-Negotiable)

The DB-home `mode` setting (`t3 <overlay> config_setting set mode <interactive|auto>`, or the `T3_MODE` env var) picks between two doctrines for publishing actions — push, PR create, PR merge, PR approve/unapprove, remote branch deletion, Slack posts, any write that leaves the local machine. The default is `interactive` (security-conservative). `auto` opts into full autonomy.

### Resolve the effective mode before every publishing decision

Do not assume interactive mode. Before saying "not pushed, your call", before asking "push?", and before prompting for any publishing confirmation, **actively resolve the effective mode in this order** (first match wins):

1. `T3_MODE` environment variable (`auto` or `interactive`).
2. Active overlay's per-overlay `mode` value in the `ConfigSetting` DB store (`config_setting set mode … --overlay <active>`, where `<active>` = `T3_OVERLAY_NAME` env var or the repo's registered overlay). The `[overlays.<active>] mode` TOML key is ignored on read.
3. Global `mode` value in the `ConfigSetting` DB store (`config_setting set mode …`). The `[teatree] mode` TOML key is ignored on read.
4. Per-repo overrides from agent memory / personal config (e.g. "this repo is auto — don't ask"). These supplement the config.
5. If nothing matched: default to `interactive`.

If the effective mode resolves to `auto`, apply the auto-mode doctrine below — do not ask for push confirmation, do not phrase the end-of-task as "your call", just push.

The most common failure mode is defaulting to `interactive` without performing steps 1-4 — saying "not pushed, interactive mode" on a repo the user has already opted into auto. That reads as the agent ignoring the user's configured preference and forces them to repeat it every session.

### Interactive mode (default)

Commit approval ≠ push approval. **Squash approval ≠ push approval. "All done" ≠ push approval. Rebase approval ≠ force-push approval.** Always present the final state and ask "Push?" as a **separate question** after committing, squashing, or rebasing — use `AskUserQuestion`, not an inline question.

- Every publishing action (push, PR create/update, PR merge, PR approve/unapprove, remote branch delete, Slack post) requires a separate explicit confirmation. "Recheck" / "re-review" / "look again" are verify-only instructions — they do **not** authorize re-approval.
- **Force-push (`--force-with-lease`)**: get separate explicit confirmation even if the user already approved the rebase. A rebase and a force-push are two decisions.

### Auto mode (DB-home `mode = auto` via `config_setting set mode auto`, or `T3_MODE=auto`)

The user has opted into end-to-end autonomy. The agent ships complete features without pausing for confirm prompts on the publishing actions listed above. In particular:

- Push the feature branch after local quality gates pass (lint, tests, `makemigrations --dry-run --check`).
- Open the PR, watch the pipeline, then **merge via the §17.4 keystone** (orchestrator `t3 <overlay> ticket clear …` → loop `t3 <overlay> ticket merge <clear_id>`; never raw `gh pr merge`) **when green unless `require_human_approval_to_merge` is `true` for the active overlay**, delete the remote branch.
- Post the overlay-approved Slack messages (review request, release note) as part of the normal flow.

**`require_human_approval_to_merge` is the merge-only carve-out.** Some overlays opt into auto-push but keep auto-merge gated because the upstream enforces a human-review gate (e.g., GitLab Code Review approval rules where CI green is necessary but not sufficient). The setting lives on `UserSettings` (DB-home) and is overridable per-overlay via `t3 <overlay> config_setting set require_human_approval_to_merge true --overlay <name>`. When `true`, the agent pushes and opens the PR/MR without asking but stops before issuing the per-diff CLEAR (`t3 <overlay> ticket clear …`) or running the keystone merge (`t3 <overlay> ticket merge <clear_id>`) — raw `gh pr merge` / `glab mr merge` are mechanically blocked regardless. The user flips it to `false` once they're comfortable trusting CI green alone. Default is `true` (training wheel on). The setting is intentionally orthogonal to `mode`: `mode = "auto"` everywhere is fine while `require_human_approval_to_merge` stays `true` on client/team overlays.

**Mode is per-overlay.** A per-overlay `mode` value (`config_setting set mode … --overlay <name>`) overrides the global `mode` value. A user can run `auto` mode on a personal dogfooding overlay while keeping `interactive` on a client overlay — the active overlay (resolved via `T3_OVERLAY_NAME`) determines which doctrine applies. See `BLUEPRINT.md` § 11.1.1.

**Quality gates still run — they just don't depend on user confirmation.** The objection auto mode answers is "stop gating on _confirmation_," not "skip quality checks."

**Don't ask after resolving to `auto`.** Once steps 1–3 of the resolution order resolve to `auto`, asking "should I push?" or "should I open the PR?" reads as ignoring the user's configured preference and forces them to repeat it every session. Just push and open the PR. The only place you still ask is the merge step, and only when `require_human_approval_to_merge` is `true` for the active overlay.

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
| **Ownership / scope** | owned vs unknown? | `[overlays.<name>.owned_repos]` (forge-host-keyed) → `teatree.core.repo_scope` + `teatree.core.gates.owned_repo_guard` | unknown-repo gate; fails **CLOSED** (unknown → ask) |
| **Collaboration** | self vs colleague? | the MR AUTHOR → `teatree.core.review_candidate.author_is_self` | never auto-merge a colleague's MR |

- **Solo-owned repos merge freely.** `souliane/*` and the user's own overlay repos (e.g. `acme-eng/widget-overlay`, `acme-eng/widget-overlay-e2e`) merge exactly like `souliane/teatree`. The only colleague-facing repos are the shared **product** repos of the org (e.g. `acme-product`, `acme-client-workspace`, `acme-shared-config-*`).
- **Private ≠ colleague-facing.** A repo being private is the _visibility_ axis (leak-prevention still applies). It says nothing about ownership — `widget-overlay` is private AND solo-owned, so the agent merges it without colleague gating.
- **Owned ≠ auto-merge.** Ownership gates the _unknown-repo_ decision only. A shared product repo is still in scope (owned by its overlay) yet still needs colleague review — that is the collaboration axis, decided by `author_is_self`, never collapsed into ownership.
- **`owned_repos` is forge-host-keyed** (`{"github.com": ["souliane", …]}`): a `gitlab.com` repo never matches a `github.com` scope.
- **The unknown-repo gate ships INERT — opt-in, default off.** `require_owned_repo_approval` defaults `false`, so no overlay is gated out of the box. Enabling it requires **first declaring the FULL owned host/namespace list** — including every private/customer forge the operator merges on — because the gate fails **CLOSED** on any repo no listed pattern owns: flipping it on with a partial list would hold the operator's own private-forge keystone merges as "unknown". Opt in from private `~/.teatree.toml` (`[overlays.<name>.owned_repos]` with the full host list + `require_owned_repo_approval = true`), where brand/customer strings are allowed and never reach the public repo.
- **A path-only TOML overlay cannot carry its own scope.** An overlay registered with a `path` but no Python `class` is skipped by overlay discovery (`get_all_overlays` returns only instantiable overlays), so it can never opt itself into the gate. Its repos must be declared under an INSTANTIABLE overlay's `owned_repos` (e.g. the always-registered `t3-teatree`).
- **Never-lockout** regardless: a per-call `[scope-push-ok: <reason>]` token, the `unknown_repo_push_gate_enabled` kill-switch, and fail-open on a resolver exception (incl. a failed Django bootstrap in the hook subprocess) all keep the gate from wedging a push.

Pinned by `tests/teatree_core/test_repo_scope.py` (host-symmetric gate), `tests/teatree_core/gates/test_owned_repo_guard.py` (polarity + orthogonality), `tests/teatree_core/test_review_candidate.py` § `TestClassificationIsAuthorNotNamespace` (author-not-namespace), and the A/B eval `evals/scenarios/owned_repo_not_colleague.yaml`.

## Run Retro Before Ending Non-Trivial Sessions

Before ending any session that involved multi-file edits, debugging, or implementation work, run `/t3:next` (which includes `/t3:retro`). Do NOT wait for the user to ask — self-trigger this. A session without retro loses compound learning.

- **Trivial sessions** (single question, quick lookup, one-line fix): skip.
- **Everything else**: run `/t3:next` before your final response.

## Verify Imports Before Applying External Code

When cherry-picking code from orphan commits, stashes, snapshots, or other branches, verify every import and function call exists in the target codebase before applying. Snapshot code assumes a different state — modules, classes, and function signatures may not exist in HEAD. Apply each change surgically and run the type checker (`ty-check`) before moving on.

## Context Longevity

Long sessions lose context to automatic compaction. Proactively manage session length:

- **After 15+ tool calls**, suggest `/t3:next` or `/t3:retro` to preserve findings before compaction.
- **Before switching phases** (coding → testing, testing → reviewing), suggest wrapping up the current phase — phase transitions are natural breakpoints.
- **Re-reading a file you already read earlier** is a sign of context pressure. Consider wrapping up.
- **When context gets compacted**, critical state must survive — see the user's global agent config § Compact Instructions for what to preserve. The `PreCompact` hook automatically writes a durable-state snapshot (no agent action needed), and the post-compaction `SessionStart` (`source=="compact"`) recovers any `/tmp/t3-snapshot-*.md` files into context (issue #845).

## Commit Before Declaring Done (Non-Negotiable)

When implementation is complete (all files written, tests pass or verified), **commit immediately** in the same response — do not wait for the user to ask. An uncommitted change is not "done"; it is in-progress work at risk of being lost to context compaction, parallel agents, or session timeout.

**Commit before any long pre-push step (Non-Negotiable).** Some mandated steps between "implementation complete" and "push" are multi-tool and multi-minute (a privacy scan, a final full-suite run, an evidence-gathering pass). Running any of them with the entire change set uncommitted leaves it exposed for that whole window: a concurrent `workspace clean-all` / worktree prune that removes the worktree in that window **irrecoverably destroys uncommitted work** — no branch ref, no reflog, no remote, nothing to `git fsck`. The sequence is **implementation complete → verify → local commit → privacy scan → push (`pr create`)**. A local commit is cheap and reversible, and makes the work recoverable even if the worktree vanishes. Never start a long mandated step with the deliverable uncommitted. (#837: retro is no longer a per-ticket pre-push step — it is an orchestrator-level periodic synthesis over durable signal; sub-agents emit findings into durable state and do not self-retro before `pr create`.)

## Pre-Commit Hook Failures on Unrelated Tests

When a pre-commit hook runs the full test suite and fails on tests **unrelated to your changes** (pre-existing failures), do not fix them one by one in a loop. After the **second** unrelated failure, stop and tell the user: the hook is failing on pre-existing test issues, and list the failing tests so they can be fixed separately. Never suggest or use `--no-verify` — see `t3:ship § Never use --no-verify`.

## Worktree-First Work (Non-Negotiable)

**All development work MUST happen in a worktree**, never on the main clone. Use `t3 <overlay> workspace ticket` or the `using-git-worktrees` skill to create one before writing any code. The worktree exists _before the first file change_ — the failure mode this forecloses is editing the main clone first and "moving it into a worktree later", which loses uncommitted work and pollutes shared state. Enforced deterministically by the `refuse-main-clone-commit` pre-commit hook and the `protect-default-branch` PreToolUse deny.

**Pre-edit check — before editing ANY project file:** If the file path lives directly under `$T3_WORKSPACE_DIR/<repo>/` (not under a ticket subdirectory like `$T3_WORKSPACE_DIR/<ticket>/<repo>/`), **stop** — you are in the main clone. Find or create the correct worktree first via `t3 <overlay> workspace ticket`. The main clone may happen to be on the PR branch (from a previous checkout) — editing there "works" but pollutes the shared clone, risks merge conflicts for other worktrees, and violates isolation.

**Pre-commit check — before running `git commit` (Non-Negotiable):** Run `git rev-parse --show-toplevel`. If the result is the main clone (e.g., `$T3_REPO`, `~/workspace/<repo>/<repo>` — i.e. NOT a `$T3_WORKSPACE_DIR/<ticket>/<repo>` path), **abort the commit**. Do not proceed to commit on `main` or any default branch in the main clone, even if the staged changes are already there from a prior session. Recovery path:

1. Pick a branch name (`ac/<short-slug>` matching the change).
2. `git branch <branch> HEAD` (snapshots the current staged + working state to the new branch).
3. If staged-but-not-committed: `git stash push --staged`, `git worktree add ~/workspace/<branch>/<repo> -b <branch>`, `cd` into the worktree, `git stash pop`, then commit there.
4. If already-committed-on-main: `git branch <branch> HEAD`, `git reset --hard origin/main` (or `git reset --hard <previous-HEAD>`), then `git worktree add ~/workspace/<branch>/<repo> <branch>` and continue from the worktree.

**Collision detection — check on EVERY file write or git operation:**

1. Before writing to a file, run `git status`. If you see unexpected modifications to files you did not touch, **another agent is working in the same directory**.
2. **If you are NOT in a worktree:** STOP writing code. Move all your work to a worktree immediately (`t3 <overlay> workspace ticket` or `EnterWorktree`), then continue there.
3. **If you ARE in a worktree and see someone else's changes:** STOP ALL WORK IMMEDIATELY. Alert the user: _"ALERT: Another agent is modifying files in my worktree at `<path>`. I've stopped all work to avoid conflicts. Please resolve before I continue."_ Do NOT attempt to continue, merge, or work around the collision.

**Why:** Parallel agents modifying the same checkout cause silent data loss — commits overwrite each other, stashes destroy in-progress work, and merge conflicts go undetected. This has cost hours of wasted work. Worktrees give each agent an isolated copy. The rules below are secondary defenses.

**Pre-task check — before tackling a known issue (failing CI job, regression, "fix X" ticket):** Run `git worktree list` first. If a worktree branch name matches the bug surface (e.g., `ac/fix-loop-scanner-*` for scanner failures, or any branch with relevant commits in `git log --oneline main..HEAD`), **another agent is likely already on it**. Do NOT spawn a parallel worktree on the same problem — coordinate or stand down. The collision rule above catches conflicts at write-time; this catches them before any work starts.

## Concurrent Agent Safety (Non-Negotiable)

Assume another agent may be modifying the same repo concurrently. Never `git stash`, `git checkout --`, or `git restore` files you didn't change — this destroys the other agent's in-progress work. Only stage and commit files you explicitly modified.

## Deprecated Code

When removing a function, class, flag, or CLI argument: delete it completely. Deprecated aliases, backward-compat re-exports, and `# removed` comments create maintenance debt. If callers exist, update them in the same change. Teatree is experimental — no deprecation warnings, no migration helpers. Break cleanly.

## GitLab Inline Comments

When posting inline PR comments, target **added lines only** — not context or unchanged lines.

## Prefer Standard Over Clever

When choosing between a clever in-process approach and the framework's standard approach, choose the standard. Prefer explicit/standard/boring over clever/implicit. If you're uncertain which is better, that uncertainty is the signal to go standard. Django's `setup()` is designed to be called once per process — subprocess via `__main__.py` beats in-process `call_command()` for entry-point overlays.

## Never Slim Skills

Never extract SKILL.md content into `references/` files to save tokens. Agents don't reliably load reference files on demand, so critical instructions get ignored. When optimizing context consumption, focus on phase-scoped loading (only embed the skills needed for the task), not on shrinking individual skills.

## Session Scope Management

Don't let sessions grow unbounded. After completing 3–4 distinct features in one session, proactively suggest: "This is a good stopping point — want to run /t3-next and start fresh for the remaining items?" The user should not have to explicitly say "stop accepting new requests."

## Skill Auto-Loading Must Work

The user should never have to manually call a teatree or overlay skill. Skills must either auto-load or be explicitly called by the teatree mechanism. When reviewing teatree, check that the hook/autoloading mechanism covers all cases: Django projects auto-load `ac-django`, overlay projects auto-load their overlay skill, lifecycle skills chain-load companions. Fix gaps in the autoloading mechanism rather than documenting manual workarounds.

## Escalate Honesty-Critical Verification to the Most-Honest Model

When ANY of these holds, record an honesty escalation **before the next verification/review/grading spawn**, so that work routes to the most-honest configured model (`[agent] honesty_model`, default Opus — requires no operator opt-in):

1. the user explicitly asks you to be honest;
2. you judge you have been dishonest;
3. the user accuses you of lying or of having "successfully failed" a task;
4. you shipped a job you cannot verify is complete.

Record it with:

```bash
t3 <overlay> honesty escalate --reason <user_asked|self_assessed_dishonest|accused_of_lying|shipped_incomplete> [--task <id>]
```

The escalation is **situational and auto-clears** — it is NOT a standing reviewer-model change. It is session-scoped, idempotent (re-firing the same trigger is a no-op), and bounded by a 6-hour safety-net TTL; the primary clear is an honest, verified-complete landing (a fully-passed rubric grade). Rationale: models learn honesty over time, so the most-honest model is the right one to _verify_ a moment the agent's own honesty is in question. The firing is yours to judge (it is prompt-level, SDK-portable — not a CLI-only flag); the consequence (raise → auto-clear) is deterministic. Trigger #4 also has a deterministic backstop: when the rubric done-gate refuses a merge, it records the `shipped_incomplete` escalation for you.

**Pick the escalation (and any per-stage override) target deliberately — cost is the operator's decision, not the agent's default.** Teatree carries no standalone "most expensive model" kill-switch: the per-phase/per-tier routing (`model_tiering.py`'s `TIER_MODELS`/`TIER_EFFORT`) is already explicit-opt-in, so nothing routes to a costlier-than-frontier model unless the operator names that model id themselves in config. This applies equally to a teatree Workflow's `model:`/`effort:` per-stage override (resolved DIRECTLY by the Workflow runtime, not through `model_tiering`) — pick the model per stage deliberately, and default to the resolved `honesty_model` / phase tier rather than reaching for whatever the most expensive available model happens to be.

## Re-Validate a Reused Guard in a New Destructive Context

A guard or classifier that is safe for gating one action is not automatically safe for authorizing another. Before reusing an existing guard to authorize a NEW destructive operation (`git reset --hard`, force-delete, force-push, `DROP`/`DELETE`), re-validate that the guard's safety property actually holds for THIS operation — e.g. subject-matching is sufficient to clean up a forge-merged branch, but only content-equivalence (patch-id) is safe before resetting a branch. In a sub-agent brief, never assert a safety property you have not verified; require the implementer to prove the property holds in the new context, and route any change that introduces a destructive operation through an adversarial review that specifically attacks the data-loss path.
