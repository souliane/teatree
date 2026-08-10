---
name: rules
description: Cross-cutting agent safety rules — clickable refs, temp files, sub-agent limits, UX preservation. Auto-loaded as a dependency by other skills.
compatibility: any
metadata:
  version: 0.0.1
---

# Agent Rules

Cross-cutting rules that apply to all teatree skills. Loaded automatically via `requires:`.

## Index

Use `Ctrl+F`/`grep` to jump to a rule. Sections are grouped below by theme; numbering is for navigation only — every rule is binding.

**Skill loading & verification**

1. [Invoke Skills Before ANY Response](#invoke-skills-before-any-response)
2. [Verification Before Completion](#verification-before-completion-non-negotiable)
2b. [A Diagnosis Cites What Was Read](#a-diagnosis-cites-what-was-read-non-negotiable)
2a. [An Acceptance Criterion That Cannot Fail Is Not a Criterion](#an-acceptance-criterion-that-cannot-fail-is-not-a-criterion-non-negotiable)
3. [A Diagnosis Cites What Was Read](#a-diagnosis-cites-what-was-read-non-negotiable)
4. [An Acceptance Criterion That Cannot Fail Is Not a Criterion](#an-acceptance-criterion-that-cannot-fail-is-not-a-criterion-non-negotiable)
5. [Grep Before Claiming Cross-Reference Coverage](#grep-before-claiming-cross-reference-coverage-non-negotiable)
6. [User Instructions Are Priority 1](#user-instructions-are-priority-1)
7. [On an Ambiguous Directive, Take the Non-Destructive Reading](#on-an-ambiguous-directive-take-the-non-destructive-reading-non-negotiable)
8. [Classifier Denial Protocol](#classifier-denial-protocol-non-negotiable)
9. [Anticipate a Predictable Gate: Offer Enable-Setting or Approve-Once, Never Bypass-or-DIY](#anticipate-a-predictable-gate-offer-enable-setting-or-approve-once-never-bypass-or-diy-non-negotiable)
10. [Re-Derive the Minimal Blocker](#re-derive-the-minimal-blocker)
11. [External Read Failure Must Fail Loud, Never Silent-Empty](#external-read-failure-must-fail-loud-never-silent-empty-non-negotiable)
12. [Read the Canonical Source Before Fixing a Conformance Bug](#read-the-canonical-source-before-fixing-a-conformance-bug)
13. [Re-Verify Cross-Agent State Before Reporting a Dependent Request](#re-verify-cross-agent-state-before-reporting-a-dependent-request)
14. [Lead a Completion Report With the Assigned-Work Status](#lead-a-completion-report-with-the-assigned-work-status)
15. [Keep Turn Output Terse and TTS-Ready](#keep-turn-output-terse-and-tts-ready)
16. [Context Transparency](#context-transparency)
17. [Clickable References](#clickable-references)
18. [Render the Title Inline, Never a Bare/Link-Only Id](#render-the-title-inline-never-a-barelink-only-id-non-negotiable)
19. [ID Namespace Disambiguation](#id-namespace-disambiguation-non-negotiable)
20. [Read Secrets From the Secret Store](#read-secrets-from-the-secret-store-non-negotiable)
21. [Read the Canonical Source Before a Structural Action](#read-the-canonical-source-before-a-structural-action-non-negotiable)
22. [Overlay Skills Are Scoped to Overlay Repos](#overlay-skills-are-scoped-to-overlay-repos-non-negotiable)
23. [Token Extraction](#token-extraction)
24. [Temp File Safety](#temp-file-safety)
25. [Complex API Payloads: Use curl or Python](#complex-api-payloads-use-curl-or-python)
26. [Never Pipe, Redirect, or Chain a gh/glab Publish Command](#never-pipe-redirect-or-chain-a-ghglab-publish-command)
27. [Preserve Existing UX Patterns](#preserve-existing-ux-patterns)
28. [No AI Signature on Posts Made on the User's Behalf](#no-ai-signature-on-posts-made-on-the-users-behalf-non-negotiable)
29. [Ask Before Posting on the User's Behalf](#ask-before-posting-on-the-users-behalf-non-negotiable)
30. [Never Post PR Comments from Parallel Agents](#never-post-pr-comments-from-parallel-agents-non-negotiable)
31. [Evidence Comes From the Deployed Environment](#evidence-comes-from-the-deployed-environment-non-negotiable)
32. [Never Modify a Remote Database Without Explicit User Approval](#never-modify-a-remote-database-without-explicit-user-approval-non-negotiable)
33. [Verify Repo Visibility Before Filing External Issues](#verify-repo-visibility-before-filing-external-issues-non-negotiable)
34. [Self-Apply `needs-triage` on Agent-Filed Issues](#self-apply-needs-triage-on-agent-filed-issues-non-negotiable)
34a. [A Filed Issue Separates OBSERVED From INFERRED](#a-filed-issue-separates-observed-from-inferred-non-negotiable)
35. [Leak Remediation — Silent Scrubs](#leak-remediation--silent-scrubs-non-negotiable)
36. [Public-Repo Commit Author Identity](#public-repo-commit-author-identity-non-negotiable)
37. [Sub-Agent Limitations](#sub-agent-limitations)
38. [Prefer Native Tool APIs Over Filesystem Heuristics](#prefer-native-tool-apis-over-filesystem-heuristics)
39. [Symlink Safety](#symlink-safety)
40. [Read Before Overwriting a Tracked Config/Dotfile](#read-before-overwriting-a-tracked-configdotfile-non-negotiable)
41. [Shell Alias Safety](#shell-alias-safety)
42. [Shell Probes Run Under zsh — a Probe Without a Control Is Unfalsifiable](#shell-probes-run-under-zsh--a-probe-without-a-control-is-unfalsifiable)
43. [Skill File Writes Require a Git Repo](#skill-file-writes-require-a-git-repo)
44. [Fix TeaTree/Skill Bugs Immediately](#fix-teatreeskill-bugs-immediately)
45. [Teatree Extension Point Changes Must Update All Registered Overlays](#teatree-extension-point-changes-must-update-all-registered-overlays-non-negotiable)
46. [Do Work Now, Don't Defer to "Later" Tickets](#do-work-now-dont-defer-to-later-tickets-non-negotiable)
47. [Contribute Mode: Promote Findings to Skills, Not Personal Memory](#contribute-mode-promote-findings-to-skills-not-personal-memory-non-negotiable)
48. [Autonomous Directive Adoption](#autonomous-directive-adoption)
49. [Ask About Auth Before External Service Integrations](#ask-about-auth-before-external-service-integrations)
50. [Never Change PR Base Branch or Dependencies](#never-change-pr-base-branch-or-dependencies-non-negotiable)
51. [Fewest PRs for Related Work — Splitting Requires Approval](#fewest-prs-for-related-work--splitting-requires-approval-non-negotiable)
52. [Always Create Tasks](#always-create-tasks)
53. [Mid-Task Interrupts](#mid-task-interrupts-non-negotiable)
54. [Background Long Operations](#background-long-operations-non-negotiable)
55. [Always Use AskUserQuestion for Questions](#always-use-askuserquestion-for-questions)
56. [The User Asked a Question — Answer It](#the-user-asked-a-question--answer-it-non-negotiable)
57. [Never Introduce Tech Debt; Reduce It](#never-introduce-tech-debt-reduce-it-non-negotiable)
58. [Publishing Actions Are Mode-Conditional](#publishing-actions-are-mode-conditional-non-negotiable)
59. [Three Orthogonal Repo Axes — Visibility, Ownership, Collaboration](#three-orthogonal-repo-axes--visibility-ownership-collaboration-non-negotiable)
60. [Run Retro Before Ending Non-Trivial Sessions](#run-retro-before-ending-non-trivial-sessions)
61. [Verify Imports Before Applying External Code](#verify-imports-before-applying-external-code)
4a. [Read the Canonical Source Before Fixing a Conformance Bug](#read-the-canonical-source-before-fixing-a-conformance-bug)
4b. [Re-Verify Cross-Agent State Before Reporting a Dependent Request](#re-verify-cross-agent-state-before-reporting-a-dependent-request)

**User intent, interruptions, and asking**

5a. [On an Ambiguous Directive, Take the Non-Destructive Reading](#on-an-ambiguous-directive-take-the-non-destructive-reading-non-negotiable)
6a. [The User Asked a Question — Answer It](#the-user-asked-a-question--answer-it-non-negotiable)
8a. [Background Long Operations](#background-long-operations-non-negotiable)
62. [Context Longevity](#context-longevity)

**Permissions, classifier, and authorization**

13a. [Never Modify a Remote Database Without Explicit User Approval](#never-modify-a-remote-database-without-explicit-user-approval-non-negotiable)

**Communication & references**

14c. [Render the Title Inline, Never a Bare/Link-Only Id](#render-the-title-inline-never-a-barelink-only-id-non-negotiable)
14b. [ID Namespace Disambiguation](#id-namespace-disambiguation-non-negotiable)
14a. [Lead a Completion Report With the Assigned-Work Status](#lead-a-completion-report-with-the-assigned-work-status)
14d. [Keep Turn Output Terse and TTS-Ready](#keep-turn-output-terse-and-tts-ready)
15a. [Ask Before Posting on the User's Behalf](#ask-before-posting-on-the-users-behalf-non-negotiable)
17a. [Evidence Comes From the Deployed Environment](#evidence-comes-from-the-deployed-environment-non-negotiable)
63. [Commit Before Declaring Done](#commit-before-declaring-done-non-negotiable)
64. [Pre-Commit Hook Failures on Unrelated Tests](#pre-commit-hook-failures-on-unrelated-tests)
65. [Worktree-First Work](#worktree-first-work-non-negotiable)
66. [Concurrent Agent Safety](#concurrent-agent-safety-non-negotiable)
67. [Deprecated Code](#deprecated-code)
68. [GitLab Inline Comments](#gitlab-inline-comments)

**API & shell recipes**

19a. [Read Secrets From the Secret Store](#read-secrets-from-the-secret-store-non-negotiable)
19b. [Read the Canonical Source Before a Structural Action](#read-the-canonical-source-before-a-structural-action-non-negotiable)
19c. [Overlay Skills Are Scoped to Overlay Repos](#overlay-skills-are-scoped-to-overlay-repos-non-negotiable)
25a. [Shell Probes Run Under zsh — a Probe Without a Control Is Unfalsifiable](#shell-probes-run-under-zsh--a-probe-without-a-control-is-unfalsifiable)

**Files, agents, and worktrees**

27a. [Read Before Overwriting a Tracked Config/Dotfile](#read-before-overwriting-a-tracked-configdotfile-non-negotiable)

**Workflow discipline**

38a. [Re-Derive the Minimal Blocker](#re-derive-the-minimal-blocker)
38b. [External Read Failure Must Fail Loud, Never Silent-Empty](#external-read-failure-must-fail-loud-never-silent-empty-non-negotiable)

**Design principles**

69. [Prefer Standard Over Clever](#prefer-standard-over-clever)
70. [Split Long Skills With Progressive Disclosure](#split-long-skills-with-progressive-disclosure)
71. [Session Scope Management](#session-scope-management)
72. [Skill Auto-Loading Must Work](#skill-auto-loading-must-work)
73. [Escalate Honesty-Critical Verification to the Most-Honest Model](#escalate-honesty-critical-verification-to-the-most-honest-model)
74. [Re-Validate a Reused Guard in a New Destructive Context](#re-validate-a-reused-guard-in-a-new-destructive-context)

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

**Read the state the claim is ABOUT, never a local proxy for it.** Step 3 says read the output; this says read the right thing. A claim about what LANDED is settled by reading the pushed commit, the remote, or the deployed surface — never the working tree, which goes on showing your edit whether or not it travelled. The recurring shape: a correction made after `git add` never reaches the commit, because the pre-commit runner stashes the unstaged change, commits the INDEX, and restores afterwards — so the file on disk still looks right and a local look "confirms" a fix that is absent from the remote history. Reasoning correctly about that mechanism is not the read. Name the read that settles it — `git show origin/<branch>:<path>` — and where you cannot run it yet, say the status is unsettled until you have.

**Multi-deliverable tickets: measure done from the SPEC, not the artifacts you produced (Non-Negotiable).** On a ticket with more than one deliverable, a completeness assertion — "done", "no blockers anywhere", "everything is here", "ready to merge/review" — is measured from **every deliverable the authoritative spec defines (incl. the spec's comments) verified on the actual merge target**, never from the artifacts you happen to have in hand. The recurring, highest-severity failure: claiming "no blockers anywhere" while the crucial deliverable was registered on the wrong surface and its fix was stranded off the merge target — invisible to a check that only inspects what exists. A false completion claim that propagates downstream is not an internal slip. Before any completion claim on a multi-deliverable ticket:

1. **Read the authoritative spec and its comments first.** A claim emitted before the spec source was read leans on proxies (the work item, repo docs, the baseline). If you have not read the spec, you cannot claim done.
2. **Enumerate EVERY spec deliverable** — not just the MRs/PRs created.
3. **Attach on-target evidence to EACH** — merged to the merge target / verified on the correct surface / passing E2E. "An MR exists" is NOT evidence.
4. **Verify the crucial/authoring deliverable explicitly on its correct surface** — the one that silently degrades to the wrong surface.
5. **Any deliverable lacking on-target evidence → say "NOT done: <X> missing / on the wrong surface / stranded off target"**, never "done".

This is enforced, not just prose: the BLOCKING Stop gate `handle_completion_claim_gate` (#2665) refuses turn-end on a multi-deliverable completion claim with no complete on-target deliverable→evidence map. It fires only on loop-driven turns; a legitimate single-deliverable "done" or a complete on-target map is never blocked. Never-lockout escapes: the `[skip-completion-gate: <reason>]` token in the turn text and the `[teatree] completion_claim_gate_enabled = false` kill-switch (`t3 <overlay> gate completion-claim disable`). This is the hard-blocking sibling of the WARN-only closure-reverify advisory (#1448).

**The answer to a gate rejection is evidence, not concessions (Non-Negotiable).** When the gate above — or any blocking gate, hook, or reviewer — rejects a claim, re-derive the assessment from the evidence. Do **not** go looking through your own correct work for something to concede so the pushback has an answer. Inventing defects is a worse failure than the over-claim the gate was catching: it corrupts every future self-assessment, and acting on a fabricated finding causes real damage (retracting a sound artifact, "fixing" correct text into something wrong). Concretely:

- A rejection means _"prove it"_, not _"find something wrong"_. Answer with the deliverable→evidence map; where a deliverable genuinely lacks on-target evidence, say so — and leave every other verdict untouched.
- Before reporting a defect in your own artifact, **quote the exact text and name the concrete failure**. If the quoted text does not actually exhibit the flaw, there is no finding — drop it.
- Keep the severity vocabulary honest: a **conflict** contradicts the spec; a **gap** is uncovered scope; an **optional extra** is a side note someone flagged as nice-to-have. Reporting a gap or a side note as a conflict inflates severity and invites a needless retraction.
- **In a bug report, "Actual Behavior" states the DEFECT, not the target.** Evidence and test plans demonstrate the _Expected Behavior_; never judge them for disagreeing with the Actual section. Misreading those two inverts the entire review.

## A Diagnosis Cites What Was Read (Non-Negotiable)

The rule above demands evidence for a COMPLETION claim. The same requirement applies one step earlier, to the DIAGNOSTIC claim — **a statement about why a system failed, and any escalation of a finding, must cite the artefact you actually read.**

The failure this prevents is fluent, not sloppy: a well-formed cause assembled from check NAMES, stated with the confidence of a diagnosis, by an agent that never opened the log. A colleague opens the log.

- **Quote the line.** "CI failed because X" needs the excerpt, the `file:line`, the rule or exception code, or the job link. One citation is the whole cost.
- **An unread hypothesis is labelled as one.** "I have not opened the logs; my guess is the ratchet fired" is honest and welcome. The same sentence without the hedge is an invention.
- **Severity waits for the evidence.** Escalating above a threshold — `SEVERE`, `CRITICAL`, `BLOCKER`, `P0` — requires the artefact that establishes it, not a relayed symptom. If your own brief asked for that evidence, the alarm waits for it to come back; report the symptom at its real confidence until then.

```text
# do X — the diagnosis carries what settled it:
#   "#4001 failed on one real violation: `ticket.py: 523 LOC, up from 510 (over the 500 cap)`."
# never Y — a plausible cause generated from the check names, log unopened:
#   "#4001 failed because 466 files were meeting the gates for the first time."
```

Enforced by the BLOCKING Stop gate `handle_unbacked_claim_gate` (`hooks/scripts/unbacked_claim_gate.py`, detector `teatree.hooks.unbacked_claim_scanner`): it refuses turn-end on a causal failure diagnosis, or a severity label, with no citation anywhere in the turn — and on a severity label whose own turn says the settling evidence has not come back. An explicitly-hedged diagnosis never fires. Never-lockout escapes: the `[skip-evidence-gate: <reason>]` token in the turn text and the `[teatree] unbacked_claim_gate_enabled = false` kill-switch (`t3 <overlay> gate unbacked-claim disable`).

## An Acceptance Criterion That Cannot Fail Is Not a Criterion (Non-Negotiable)

`/t3:code` § "TDD Discipline" mandates observing every regression test RED before trusting its green. This is that rule one level up, applied to feature acceptance: **before building against a criterion, name the state of the world that makes it FAIL.** If no such state exists, it is not a criterion — it certifies whatever you do, including doing nothing.

- **Absence-satisfied criteria are the highest-risk shape.** "X stays unchanged", "Y is untouched", "no regression in Z", "suite S still passes unmodified" are all satisfied by never touching the module — so skipping the work scores as success, and the skipped phase reports complete.
- **Pair every absence-satisfied criterion with a positive one only the implemented feature can satisfy** — a behaviour observably absent before the change and observably present after. The positive criterion is what the phase is verified against; the absence one is a guard, never the proof.
- **Say so when a handed-down criterion is unfalsifiable.** It is a defect in the spec, not a licence to satisfy it cheaply — surface it and add the positive pair before implementing.

The E2E-scoped statements of the same principle are `/t3:e2e` § "Writing Tests" (author side) and `/t3:e2e-review` § "Test the ticket, not the MR diff" (reviewer side): a test built against the diff's current behaviour passes regardless of whether the feature is correct. This section is the general form — apply it to acceptance criteria; they apply it to tests.

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

**Step 0 — the denial reason usually names the in-scope form; re-issue in it rather than escalating.** Parse the stated reason for the corrective scope, and check the user's `~/.claude/settings.json` `autoMode.allow` / `permissions.allow` for a rule that already authorizes the action under the correct form. If either resolves it, the action was never out of policy — re-issue it in the **authorized form**; that is not a relaxation and needs no user prompt. Escalate only when neither resolves it: escalating a wrong-form mistake wastes the user's time on a decision they should never have been asked.

**Required response when Step 0 does not resolve it:**

1. **Stop.** Drop whatever you were doing. Do not start an alternative approach in the same response.
2. **Inform the user** in plain text: which command was denied, what you were trying to accomplish, and the smallest static permission rule that would have allowed it (e.g. `Bash(gh issue create *)`, `Bash(docker buildx prune *)`). The rule must be the smallest rule that covers the use case — never a blanket `Bash` or `Bash(* *)`.
3. **Ask via `AskUserQuestion`** with exactly two options: **"Allow it (relax classifier)"** — the preferred one — or **"Keep the denial (do it differently)"**, for which you propose a concrete alternative path and proceed only after the user picks one.
4. **If the user picked "Allow it":** attempt the `~/.claude/settings.json` edit yourself via `Edit`, then retry the original command. A paste-ready snippet for the user to apply is the fallback **after** the harness blocks that write, never the default path.
5. **Wait for the answer.** Do not retry the denied command, do not invent workarounds, do not file tickets, do not start unrelated work, until the user has chosen and (if relaxing) the new rule is in place.

**Banned reactions to a classifier denial:**

- Silently retrying with a different argument shape hoping the classifier passes (`gh issue create` → `gh api repos/.../issues`).
- Switching tools (Bash → MCP, MCP → Python subprocess) to bypass the rule.
- Decomposing the command into pieces that each pass individually.
- Editing teatree's plugin `settings.json`, `CLAUDE.md`, or any plugin-distributed permissions file to add an allow rule — the user-scope `settings.json` is the only right knob, and teatree never relaxes permissions on the user's behalf.
- Continuing the surrounding work and "leaving the denial for later".

The Step 0 worked example, the settings-file edit procedure, the rationale, the standing recommended authorization set (and the read-only `t3 doctor authorizations` check that suggests it), and the full permissions boundary are in [`skills/rules/references/classifier-denial-escalation.md`](references/classifier-denial-escalation.md).

## Anticipate a Predictable Gate: Offer Enable-Setting or Approve-Once, Never Bypass-or-DIY (Non-Negotiable)

Gates — the on-behalf pre-gate, the E2E gate, the merge/CLEAR keystone, the auto-mode classifier — are an **extra safety net, not the primary control**. Ideally teatree never hits one. When you can foresee that the next action **will** hit a gate (a colleague-visible on-behalf post while `on_behalf_post_mode` is `ask`/`draft_or_ask`, a merge that needs a recorded human approver, a command no `autoMode.allow` / `permissions.allow` rule covers), surface the choice to the owner **proactively, BEFORE hitting the block** — do not blunder into the gate and only then react.

The choice you offer is **solution-oriented** and has exactly two options:

1. **Enable the setting durably** — flip the standing knob so the friction is gone for good (`t3 <overlay> config_setting set on_behalf_post_mode immediate`, add the `permissions.allow` / `autoMode.allow` rule, record the standing authorization).
2. **Approve just this once** — a single-use, scoped authorization for exactly this one action (`t3 review approve-on-behalf <target> <action> --approver <user-id>`, `t3 <overlay> ticket e2e-bypass <id> --approver <user-id> --head-sha <sha>`).

**Never** frame the choice as "**bypass the gate, or do it yourself**". That pair is wrong on both sides: _bypass_ rips out the safety net with nothing durable in its place, and handing the whole action _back to the user_ is the very friction the enable-setting option exists to remove. Offering bypass-or-DIY is the anti-pattern this rule bans — asking it means you waited for the gate instead of anticipating it.

This composes with the Classifier Denial Protocol above: that section governs _reacting_ to a denial already hit; this one governs _anticipating_ a predictable block one action ahead, so the reactive path is rarely needed. Enforced in code by `teatree.core.on_behalf_gate_recorded.format_on_behalf_block_message` (the block message names both solution-oriented options, never a bypass) and `teatree.on_behalf_gate.on_behalf_post_will_block(action)` (the proactive pre-check that predicts the block before the post). Pinned by `proactive_gate_offers_enable_or_approve_once` and `proactive_gate_anticipates_before_hitting_not_bypass_or_diy` in `evals/scenarios/proactive_gate_doctrine.yaml`.

## Re-Derive the Minimal Blocker

When an operation is blocked — a classifier denial, a failing gate, an external or human-gated wait — re-derive the **minimal** set of work that genuinely depends on that exact operation before declaring anything else blocked. A blocked merge does not block PR creation, implementation, review, or research; a blocked deploy does not block the next feature. Before reporting "nothing actionable", ask of each pending task: does it consume the blocked operation's output, or does it merely share a goal (or sit later in the same chain) reachable by a different, available path? Reporting "nothing actionable" for two or more cycles behind a single external block is itself the signal to audit for a non-blocked path rather than continue idling. This complements the Classifier Denial Protocol (which governs the denied operation itself); this rule governs not over-propagating that block to independent work.

## External Read Failure Must Fail Loud, Never Silent-Empty (Non-Negotiable)

An external / third-party read that FAILS — a missing MCP connector, an absent API token, a forge/API error, a down service — must **fail loud**: raise or surface the failure. Never degrade a read _error_ to an empty/degraded result that a caller then consumes as truth. A confidently-empty answer manufactured from a read failure is the trap — "no open PRs" / "no in-flight work" / "no findings" returned because the read errored, indistinguishable from a genuine empty, leads the caller to proceed on data it does not actually have. Silent-empty is forbidden.

- **A read ERROR ≠ a genuine empty.** "The forge returned zero rows" and "the forge read raised" are different outcomes; do not collapse them into the same empty return. Surface the error so the consumer cannot mistake a failure for "nothing there".
- **A sanctioned fallback transport is fine — a silent-empty is not.** Compatible with the #1192 fallback-transport pattern: a _deliberate, known_ fallback (a configured secondary channel, an explicitly-chosen local-only mode when a dependency is _absent by configuration_) is legitimate and may proceed. What is banned is laundering a _read failure_ into an empty result with no loud signal. The distinction is the seam: "no connector configured" (a known state → sanctioned degraded path) versus "the configured connector's read errored" (a failure → fail loud).
- **Fail-open at the ORCHESTRATION layer is a separate, explicit choice.** A caller may legitimately decide "this read must not block me" and catch the loud failure to continue (e.g. intake must not be blocked by a transient forge outage). That is a conscious, local decision at the _caller_, not a license for the _reader_ to hide the failure behind an empty return. The reader fails loud; the caller decides what to do with the failure.

Enforced in code by `teatree.core.intake.landscape_gather.run_landscape`, which raises `LandscapeForgeReadError` when a _configured_ forge's read errors (rather than returning a degraded-empty survey), while the _no-host-configured_ sanctioned fallback still degrades to a local-only survey.

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

This is the canonical home; `/t3:checking` § "Output contract" cross-references it for the `task TODO-<id> (ticket #<n>)` line shape, and the disambiguation eval is `evals/scenarios/id_namespace_disambiguation.yaml`.

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

- Asked to "enable team mode" / "enable agent team mode": your single next action is **one** `Read` of the canonical role split — for team mode that file is **`skills/health/SKILL.md`** (the health skill owns the team-role split; BLUEPRINT.md's roles section or CLAUDE.md are equivalent canonical sources) — and it names the panes/roles and the overlay seam (one pane teatree, one pane the overlay). Issue that `Read` **before** any `Agent`/`Task` dispatch. **You ALREADY know the canonical roles from prior context — that knowledge is NOT a license to skip the Read.** Spawning `CORE_MAKER`/`OVERLAY_MAKER`/`REVIEWER` panes "from memory" because you remember the role names is the exact drift: read the source first even when you are confident you recall it, because the source is the spec and your memory is not. The Read comes first; the spawn comes after.
- **The canonical `Read` IS the single action — issue it and STOP.** Do not first shell out to locate the file (`find … BLUEPRINT.md`, `echo "$T3_REPO"`, `ls`, `cat`), and do not loop retrying alternate paths if a `Read` comes back not-found. Read `BLUEPRINT.md` (or `skills/health/SKILL.md`) by its repo-relative path in one call; that read is the structural-action gate, whether or not the file resolves on the first try. **And the STOP is symmetric — do not path-hunt AFTER the read either.** The metered drift the lane caught is read-FIRST-then-over-explore: the agent issues the correct canonical `Read`, then keeps going with `find`/`grep`/`git rev-parse`/`ls`/`echo`/`cat` calls to locate or re-locate the file "to be thorough" before acting. That over-exploration is the same violation in mirror image — the canonical read already gave you the spec, so once it returns, proceed to the structural action (or stop); do NOT shell out to hunt for the file again. One canonical Read, then act — no path-hunting on either side of it.

```bash
# do X first — ONE canonical read by its repo-relative path, then stop:
#   Read(file_path="BLUEPRINT.md")            # or skills/health/SKILL.md / CLAUDE.md
# never Y — do not hunt for the path with shell calls before the read:
#   Bash(command="find ~ -name BLUEPRINT.md")  ← FORBIDDEN: the Read is the action
# never Z — do not dispatch panes from memory before that read:
#   Agent(prompt="you are CORE_MAKER …")       ← FORBIDDEN as the first action
```

This is the structural-action sibling of § "Read the Canonical Source Before Fixing a Conformance Bug" (which governs conformance bugs); both say: the authority is the spec, read it before you act. Pinned by `read_canonical_before_structural_action_under_load` (`evals/scenarios/rules.yaml`).

## Overlay Skills Are Scoped to Overlay Repos (Non-Negotiable)

Load the overlay playbook skill (`/t3-<overlay>`) for **any** task in an overlay-managed repo — and ONLY for those. A non-overlay task needs no overlay skill.

- **Overlay-repo task** (coding/reviewing in an overlay's product repo): self-load the overlay skill `/t3-<overlay>` alongside the dev + language skills **before** reading a diff or editing source — it carries the repo's run/test/review wiring (see `overlay_work_requires_overlay_skill.yaml`).
- **Non-overlay task** (a change inside `souliane/teatree` itself, or any standalone repo with no active overlay): load only the skill(s) that actually apply — `ac-django` / `/t3:code` / `/t3:internals` for a teatree Django change. Do NOT pull in a different project's overlay skill; teatree is its own Django project, not an overlay repo.

```text
# teatree-only change → load what applies, not an overlay skill:
Skill(skill="ac-django")   # or t3:code / t3:internals
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
- **Always use `>|`** (clobber override) not `>` — zsh `noclobber` silently prevents overwrite (an instance of § "Shell Probes Run Under zsh").
- **Always clean up** the temp file immediately after use (`os.unlink()` in Python, `rm` in shell).
- **Exception: pre-compaction snapshots** — files matching `/tmp/t3-snapshot-*.md` are recovered automatically on the post-compaction `SessionStart` (`source=="compact"`) event (issue #845). Use `t3-snapshot-${CLAUDE_SESSION_ID:-manual}-$(date +%Y%m%d-%H%M).md` for the filename. Delete after persisting findings to durable storage.

## Complex API Payloads: Use curl or Python

Some issue tracker CLIs cannot serialize nested JSON. **Always use `curl`** with `-H "Content-Type: application/json"` and a proper JSON `-d` body for payloads containing nested objects.

For note bodies containing markdown images (`![alt](url)`), shell variable interpolation and `jq --arg` both escape `!` to `\!`. **Always use Python** (`urllib.request` or `requests`) to serialize the JSON payload.

## Never Pipe, Redirect, or Chain a gh/glab Publish Command

The banned-terms (#1415) and quote-scanner (#1213) gates classify a command's visibility by walking EVERY top-level `&&`/`;`/`|`/newline segment — a segment carrying any redirection, heredoc, or substitution construct (`>`, `<<`, `2>&1`, `$(...)`) forces a conservative SCAN of the whole command, even when the actual `gh`/`glab` post targets a known-private repo that would otherwise skip. This is by design (an unrecognised construct could hide a second, unverifiable command), but it means a habitual `... 2>&1 | python3 -c "..."` tacked onto a `gh pr create`/`glab api` call — or writing a body file via heredoc in the SAME Bash call as the post — reliably trips the gate on a repo that is genuinely private.

**Issue the publish command ALONE, in its own Bash call, with no trailing `2>&1`, no pipe, no heredoc.** Write any body file in a separate, prior Bash call; inspect the JSON response (if needed) in a separate, later call. Splitting the call costs nothing and avoids a false block that has nothing to do with the destination's actual visibility.

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

**Canonical setting:** `on_behalf_post_mode` (DB-home, default `"draft_or_ask"`, per-overlay overridable) — set with `t3 <overlay> config_setting set on_behalf_post_mode <value>` (add `--overlay <name>` for the per-overlay scope). It takes three values.

The gate covers colleague-**VISIBLE** posts only. A **draft** (`post_draft_note`) is colleague-invisible — only the user can submit it — so it is **exempt under every mode** and never needs approval; that exemption is the whole point of the setting.

`teatree.core.on_behalf_egress.OnBehalfSlackEgress` is the single owner of **every colleague-surface Slack post AND react** under the user's identity — review-DONE reactions, the `:merge:` reaction, broadcast outcome reactions, review-nag posts, the `notify post` / `notify react` CLI, and `t3 slack react`. All of them run gate→route→emit→audit in one place, so a colleague reaction can never slip past the gate; a self-DM short-circuits ungated, so a self-ack stays free.

The three mode values (`draft_or_ask`, `ask`, `immediate`), the verdict resolver, and the other two chokepoints are in [`skills/rules/references/on-behalf-posting.md`](references/on-behalf-posting.md).

When the verdict is `BLOCK`, before any post/comment/approval/reaction the agent makes **under the user's identity to a colleague or customer surface** — a GitLab/GitHub PR/MR comment, an issue comment, a PR/MR approve or unapprove, a Slack channel or thread message, a Notion page or comment, an emoji reaction on someone else's message — the agent must obtain the user's explicit approval **first** (via `AskUserQuestion` for ad-hoc agent posts, or by recording an `OnBehalfApproval` for teatree code paths — see below) and publish only after the user confirms.

How the gate is satisfied by a recorded `OnBehalfApproval`, what sits outside it, and the `notify_on_post_on_behalf` receipt are in [`skills/rules/references/on-behalf-posting.md`](references/on-behalf-posting.md).

**Which CLI to run — the DESTINATION picks the credential, you never name one.** Both shapes below route through `OnBehalfSlackEgress`, which classifies the destination and selects the credential itself: the user's own DM goes out as the overlay bot, a colleague or channel goes out under the user's own identity. So the command carries only a destination and a body. No teatree surface accepts a credential or an identity-switch flag — if you find yourself reaching for one, the command is wrong, not incomplete.

```bash
# colleague channel (or a colleague's DM) — gated, then routed to the user's own identity:
t3 <overlay> notify post --channel <channel> --text '<message>'
# an emoji reaction on a colleague's message — the same gated egress:
t3 slack react --channel <channel> --ts <timestamp> --emoji <name>
# the user's OWN DM (bot→user self-DM) — exempt from the gate, never on-behalf:
t3 <overlay> notify send '<body>' --idempotency-key <key>
```

Never hand-roll the colleague egress: a raw Slack Web API call carrying your own credential, or any post/react outside that class, fails an import-guard test in the build.

**Failure mode this prevents:** the agent posts a poorly-worded reply or an approval the user did not intend under the user's name to a colleague, and the user only learns of it after the fact (or via the notify receipt). The pre-gate keeps the user in control of their own voice until they choose to delegate it.

## Never Post PR Comments from Parallel Agents (Non-Negotiable)

MR/PR comment posting (test plans, evidence, review notes) must be **serialized** — never dispatch two parallel agents that both post comments on PRs. Parallel agents cannot check for each other's posts, resulting in duplicate comments.

**Serialized means one poster at a time — it does NOT mean the main agent posts directly (do X, never Y).** "Serialize" governs ordering, not who acts. The main/orchestrating agent is never the poster itself: per § "DISPATCH IMMEDIATELY — the orchestrate-only boundary" below, a colleague-visible publish (`t3 review post-comment`, `post-draft-note`, a test-plan or evidence comment) is dispatched to a single sub-agent, exactly like a code edit — the boundary is about WHO touches a colleague-facing surface, not about the call being short enough to "just do it here." Serialize by dispatching one sub-agent, collecting its result, then dispatching the next — never by having the main agent shortcut the dispatch and run the posting command itself in the foreground.

```python
# EXAMPLE — `my-org/my-repo` and `acme` are stand-ins, not a teatree target. Nothing here is a work item.
# do X — dispatch the single posting action to a sub-agent, then stop:
Task(description="Post review finding", prompt="Post an inline `t3 review post-comment` on my-org/my-repo!4120, src/acme/billing/sweep.py line 88: <finding text>. Report the comment URL.")
# never Y — the main agent runs the posting command itself because it's short/serialized:
# Bash(command="t3 review post-comment my-org/my-repo 4120 '<finding>' --file src/acme/billing/sweep.py --line 88")   # FORBIDDEN in the main agent
```

## Evidence Comes From the Deployed Environment (Non-Negotiable)

Before posting any screenshot, PDF, or "proof it works" artifact on an MR/PR/issue, **load `/t3:e2e`** and follow § "Evidence Source Integrity". The short version that every agent must remember even without the full skill loaded:

- **Required:** browser screenshots from the deployed dev/staging URL, OR documents regenerated on the deployed environment after merge + deploy.
- **Prohibited:** golden test PDFs from `build/test-results/` or `src/test/resources/`, `pdftotext` from a local build, screenshots of `localhost`, **and side-by-side comparisons assembled from PDFs extracted at different git commits**.

A passing local test suite is not evidence. The deployed system is the only artifact that proves a user-visible feature works. If the proper evidence requires steps you can't complete this session, don't substitute a prohibited source — and don't just narrate the limitation in prose and stop, either. This is exactly the "fact you genuinely cannot obtain" case in § "Always Use AskUserQuestion for Questions" § "The boundary — what you SHOULD still ask" below: while you're still mid-turn with the user/orchestrator (nothing posted yet), surface the blocker with `AskUserQuestion` — ask for the deployed dev/staging URL, or whether the deploy has actually finished — rather than declaring "I can't verify this" as your final answer with no way for the user to unblock you. Only once you're recording the outcome durably on the PR/MR/issue itself does the unavailability get written into the comment as the fallback note.

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

The label governs an issue that is going to exist. Whether it should exist at all is decided one step earlier by the backlog-reuse precondition — search the open backlog, extend a suitable host rather than adding a near-duplicate, one issue per root cause — which is canonical in `AGENTS.md` § "Issue Creation" and is not restated here.

## A Filed Issue Separates OBSERVED From INFERRED (Non-Negotiable)

A root-cause claim in a filed issue is load-bearing: the next reader starts from it, so a wrong mechanism sends them down the wrong path and costs more than filing no mechanism at all. Keep the two apart in the body, labelled:

- **Observed** — the commands run, their verbatim output, the states read, the `file:line` inspected. Reproducible: anyone re-running it gets the same thing.
- **Inferred** — the mechanism you believe connects those observations. A hypothesis until an experiment separates it from the alternatives.

Then:

- **An inference written in the observed voice is a claim you did not make.** "X fails because Y", when all you saw was X, reads to the next person as a measured fact.
- **When the mechanism rests on a SINGLE observation, say so and name the experiment that would confirm it.** One observation is consistent with several mechanisms; naming the discriminating experiment turns a guess into a next step.
- Stated uncertainty is cheap; a confidently wrong root cause is not — and it is invisible, because a plausible mechanism is never questioned again.

This is the published-artifact sibling of § "Re-Validate a Reused Guard in a New Destructive Context" and its "mark every load-bearing premise VERIFIED or UNVERIFIED" clause: that rule scopes to a sub-agent BRIEF, this one to anything that leaves the machine.

## Leak Remediation — Silent Scrubs (Non-Negotiable)

When remediating a privacy leak on a public repo (force-push to drop PII, delete a comment that exposed a credential, rewrite a branch that leaked internal data), **every public artifact produced during the remediation must be neutral**. Do not name what leaked, do not name that a leak occurred, do not describe the scrub. Announcing the remediation on a public surface amplifies the leak (Streisand effect) — the commit subject, the PR comment, and the branch name are all crawled, cached, and indexed.

## Public-Repo Commit Author Identity (Non-Negotiable)

Commits pushed to a PUBLIC repo (`souliane/*`) must have an author **and** committer email that is a GitHub noreply address — `<id>+<login>@users.noreply.github.com` (e.g. `21343492+souliane@users.noreply.github.com`). A real/deliverable address (any customer/personal domain inherited from local `.git/config` or the XDG global) in public history is a permanent PII leak that GitHub's own "block pushes that expose my email" does **not** catch for third-party domains. The accepted shape is the noreply pattern itself — not one hardcoded login — so any GitHub identity passes and any real email blocks. Private overlay repos are exempt. Enforced deterministically by the pre-push gate `scripts/hooks/refuse-public-push-with-leak.sh` (#730): on a violation it blocks and prints the offending identity plus the `git filter-branch --env-filter` rewrite to the repo's GitHub noreply identity; re-push after the metadata-only rewrite.

The banned-word list, the required form for each public artifact (commit subject, branch name, PR-close comment, push description), and the pre-done grep that checks them are in [`skills/rules/references/leak-remediation.md`](references/leak-remediation.md).

## Sub-Agent Limitations

Sub-agents (Agent tool) **lose all loaded skills, MCP access, and shell functions**. By default, never dispatch sub-agents for skill-dependent work. Do all skill-dependent work sequentially in the main conversation.

**A sub-agent that needs a skill gets the skill, never an exemption:** generate the brief's preamble with `t3 <overlay> skill-preamble` (the Non-Negotiable below) so the dispatched agent carries the SKILL.md bodies it would otherwise lose. MCP access and shell functions do not travel that way — work needing either stays in the main conversation.

**Exception (monitor/work-trigger loop only):** `/t3:wip` deliberately delegates each ticket's full delivery to a single **singleton** sub-agent, run one at a time. That sub-agent loads the skills it needs via the Skill tool itself, so the "loses all loaded skills" caveat does not apply. This keeps the batch orchestrator's context lean across a long backlog. The singleton constraint is scoped narrowly to the loop that _monitors external systems and triggers work_ — it says nothing about loops in general or sub-agent use in general, and an ordinary session remains free to use loops and sub-agents as usual. The canonical statement (with the full scope boundary) lives in `/t3:wip` § Rules "Singleton delivery sub-agent (canonical statement)"; this is a reference to it, not a second copy.

**Every raw Agent-tool spawn MUST carry the skill preamble (Non-Negotiable).** A sub-agent dispatched through the raw harness Agent tool gets only its thin subagent-type system prompt — it never receives the SKILL.md bodies the orchestrator has loaded, so it over-provisions for remote e2e, runs raw `playwright`/`glab` instead of `t3`, and ignores overlay rules. Before spawning an e2e / coder / reviewer sub-agent, generate the inline skill preamble with `t3 <overlay> skill-preamble --skills t3:rules,t3:e2e[,<overlay-skill>]` (it concatenates each `SKILL.md` body, resolving framework **and** the active overlay's skills) and **prepend it to the brief**. The dispatched prompt must contain the embedded skill bodies (the `--- SKILL: <name> ---` markers), not a bare task description. A bare brief is the bug this gate exists to catch. (Pinned by `evals/scenarios/orchestrator_embeds_skills_in_subagent_brief.yaml`; the agent dispatch path injects the same bodies via `teatree.agents.skill_injection`.)

**Before delegating platform API work:** Read the relevant platform reference (`t3:platforms`) before writing sub-agent prompts that involve API calls (draft notes, discussions, PR operations). Sub-agents can't read skills themselves — copy the exact API recipe into the agent prompt.

**After a sub-agent completes, re-read any files it modified.** Sub-agents get a forked copy of your file state — their edits don't update your cache. Writing to a file without re-reading first will silently overwrite their changes.

**A blocked sub-agent surfaces the block to the orchestrator — it never silently works around the gate (Non-Negotiable).** When a sub-agent hits a gate it cannot satisfy — a missing skill, an autonomy/on-behalf block, a missing token, a classifier denial, a missing approval — it must **stop and return a structured blocked result naming the reason**, not guess, retry with a different shape, partial-ship, or fabricate a workaround. The structured channel is the result envelope's `needs_user_input: true` + `user_input_reason: "<why>"` (`teatree.agents.result_schema`); a free-prose "I couldn't do X so I did Y instead" is not a surfaced block — it is a swallowed one. The orchestrator, on receiving a blocked result, **escalates** (AskUserQuestion when interactive, or a Slack DM / a `DeferredQuestion` when away) — it never records the sub-agent's run as done, never advances the FSM over it, and never re-dispatches the same blocked unit without resolving the block first. Silent work-around masks the problem and produces invisible partial work; the fix is satisfiable, not pure suppression — once the human supplies the missing skill/token/approval, the unit re-runs and proceeds. (Issue [#1915](https://github.com/souliane/teatree/issues/1915); the agent-facing side of the Classifier Denial Protocol above; pinned by `evals/scenarios/blocked_subagent_escalation.yaml`.)

**A killed run's empty return does NOT mean no side effect landed — reconcile against the world before re-dispatching (Non-Negotiable).** The blocked-result guard above keys on a structured blocked envelope. A run killed mid-flight by a **usage limit** never produces one: it returns an empty/absent report. That empty report is not evidence — a killed process is _more_ likely to under-report than an intact one, because reporting is the last thing it does. So any reasoning of the form "the run said it did nothing, therefore nothing exists" is unsound whenever the run could have been killed. The concrete failure: a batch that files issues on a public tracker was killed after filing one, reported nothing filed, was re-dispatched, and created a **duplicate public issue**. Before re-dispatching any batch with external side effects, **query the tracker for what already exists and attach to it** (comment/update the existing artifact), rather than trust the previous run's account of itself — keyed on a durable idempotency record written BEFORE the side effect, not on the empty return.

The safe shape is idempotency by construction, not orchestrator vigilance. Core already does this for the issue-implementer batch, and it is the pattern to copy: the claim is recorded as an `ImplementedIssueMarker` **before** the work is dispatched (`ImplementedIssueMarker.objects.claim(issue_url, overlay)` is a `get_or_create` keyed on `(issue_url, overlay)` — a second claim of the same issue returns `None`, so a killed-then-retried run never re-dispatches it), the `Ticket` is likewise a `get_or_create(issue_url=…)` so a retry attaches to the existing ticket instead of forking a second one, and the scanner's forge read-back (`teatree.loop.scanners.forge_readback.existing_work_for_issue`) queries the tracker for a branch/PR that already references the issue before claiming — reconciling against the world, not against a dead run's report. Orphaned `DISPATCHED` markers (claimed, process died before the ticket was created) are reconciled by `ImplementedIssueMarker.objects.reconcile_stale` (#3275). When you build a NEW batch with external side effects, give it the same shape: a durable idempotency key per intended artifact, recorded before the side effect, and a re-run that attaches rather than re-creates. (Issue [#3360](https://github.com/souliane/teatree/issues/3360).)

**Dispatch-prompt hygiene — match the target repo's conventions, don't drift to your own defaults (Non-Negotiable).** A sub-agent prompt that scaffolds a branch or opens a PR must carry the **target repo's** convention, not a habitual default carried over from another repo.

The branch-scheme and no-reflexive-`--draft` rules, with their do-X/never-Y `git`/`gh` examples, are in [`skills/rules/references/worked-dispatch-examples.md`](references/worked-dispatch-examples.md).

Pinned by `subagent_prompt_drift_branch_prefix` and `subagent_prompt_drift_no_draft_default` (`evals/scenarios/subagent_prompt_drift.yaml`).

**A dispatch brief must BOUND the test-worker multiplier (Non-Negotiable).** `-n auto` is in the repo's pytest `addopts` and in the lane runners, so EVERY dispatched agent sizes its own pool from the box's cores regardless of how many agents already run: N agents on a C-core box is N × C workers competing for one machine's RAM. **"Do not run the full suite" is NOT a bound** — it constrains which tests are selected, not how many processes they fork; a narrow node id at `-n auto` still spawns a worker per core. The lane runners' `bound_xdist_workers_to_memory` default is not one either: it reads the container's cgroup cap for **its own** process and cannot see the sibling agents. The only bound is the env var, and it belongs in the brief.

The bound is a **ceiling, not a literal to paste**. Pick a small number, and where the environment already exports `PYTEST_XDIST_AUTO_NUM_WORKERS`, defer to it rather than overwrite it — a headless-dispatched agent already receives a per-agent value the governor computed from the live core count, the active-agent count and free memory (`src/teatree/agents/runner.py` → `with_test_worker_cap` → `src/teatree/core/admission_governor.py::per_agent_test_workers`), and on a loaded box that value is frequently **1**. On 8 cores with 4 agents live and 8 GB available it resolves to exactly 1, so a brief hardcoding `=4` quadruples the parallelism the governor just decided was safe — causing the OOM the rule exists to prevent:

```bash
# small ceiling, but NEVER above a value the environment already exported:
PYTEST_XDIST_AUTO_NUM_WORKERS=${PYTEST_XDIST_AUTO_NUM_WORKERS:-4} uv run --no-sync python -m pytest <paths> -q --no-migrations -p no:cacheprovider
```

**Watch AVAILABLE MEMORY, not load average.** Load 30 with 10 GB free is a healthy box; load 12 with 2 GB free is an OOM about to happen — load says how many runnable processes there are, memory says whether the next one survives. Read free memory before dispatching another test-running agent, and wait rather than stack one more.

A deterministic cross-agent worker cap HAS landed, but only for the **headless** lane — [#4107](https://github.com/souliane/teatree/issues/4107) and [#4157](https://github.com/souliane/teatree/issues/4157) shipped it, and every agent dispatch now exports the governor's computed per-agent value. The harness `Agent`/`Task` sub-agent path is NOT capped: neither `src/teatree/core/dispatch_admission.py` nor `hooks/scripts/dispatch_admission_gate.py` carries any test-worker or `XDIST` term, so what landed there is an agent-COUNT ceiling, not a worker cap. That is why the brief-level bound above still matters — on the harness dispatch path it remains the only thing between N sub-agents and an OOM.

## Prefer Native Tool APIs Over Filesystem Heuristics

When integrating with tools (issue trackers, CI, chat), prefer their API or CLI over scraping files. File-based approaches break on layout changes, don't handle pagination, and miss metadata.

## Symlink Safety

Never replace a symlink with a real file. `ls -la` first if unsure. If a path is a symlink, edit the target — never delete the link and write a new file.

## Read Before Overwriting a Tracked Config/Dotfile (Non-Negotiable)

A user config file or dotfile (a `dotfiles`-repo file, an XDG `.config` file, `.zshrc`, …) is **authoritative as it exists on disk right now** — even when that on-disk content diverges from the committed version. The user may have made uncommitted edits directly on disk. So before you clobber it you must **read its current content this session**:

- A full **`Write`** that overwrites an existing config/dotfile, OR a **`git checkout` / `git restore`** that restores a tracked config from a committed version, discards the live on-disk content. Do **not** do either blind — `Read` the file first, confirm what you intend to change, then re-issue the write.
- **Uncommitted-on-disk beats committed.** Never "restore the config from git to a clean state" without first reading the working-tree copy — the committed version is NOT the source of truth for a user config; the file on disk is.
- This is the file-write sibling of § "Read the Canonical Source Before a Structural Action" and § "Read the Canonical Source Before Fixing a Conformance Bug": the live artifact is the spec; read it before you act on it.

**Deterministically enforced.** The PreToolUse gate `handle_block_config_overwrite` (`hooks/scripts/config_overwrite_guard.py` + `teatree.core.gates.config_overwrite_guard`) refuses a blind `Write` over an existing config/dotfile and a blind `git checkout`/`git restore` of one when the path was not read this session (it consumes the existing `<session>.reads` capture). Reading the file first clears it. Never-lockout escapes: a per-call `[config-overwrite-ok: <reason>]` token, the `[teatree] config_overwrite_gate_enabled = false` kill-switch (`t3 <overlay> gate config-overwrite disable`), and the shared `_fail_open_or_deny` chain.

**Failure mode this prevents.** An agent overwrote a tracked dotfile (a symlink into the user's dotfiles repo) with a blind `Write`, and on another occasion nearly restored a config from git without reading the live copy — both would have silently destroyed the user's uncommitted edits.

## Shell Alias Safety

Use `command rm`, `command cp`, `command mv` in Bash tool calls to avoid zsh interactive aliases that hang. Also `gs` is aliased to `git status` — use `command gs` for GhostScript. (An instance of the general fact below: the Bash tool's shell is zsh.)

## Shell Probes Run Under zsh — a Probe Without a Control Is Unfalsifiable

**The Bash tool's shell is zsh. bash idioms do not error here — they answer WRONGLY.** State this once and generalize it: the two notes above (`>|` in § "Temp File Safety", `command rm` in § "Shell Alias Safety") are instances of this one fact, not isolated trivia. An agent writing a shell **probe** — a throwaway command to check whether some property holds — reads neither of those sections (it is not writing a temp file, not calling `rm`), writes bash out of habit, and gets **confident, meaningless output instead of an error**. The four ways this has actually broken a probe:

| Bash idiom | What zsh actually does |
| --- | --- |
| `${BASH_SOURCE[0]}` | **Empty.** `cd "$(dirname "${BASH_SOURCE[0]}")"` silently resolves to `dirname ""` → `.` → **cwd**, so the probe "works" against the wrong directory. |
| `for x in $var` (unquoted) | **No word-splitting** in zsh. The loop iterates **once**, over the whole string, and reports one clean pass. |
| `> file` on an existing stub | `noclobber` silently blocks the rewrite. The probe reads back the **stub's old content** and concludes the property holds. |
| `grep -r <pat>` with no file arg | Recurses **cwd** instead of reading stdin. Returns matches from the tree, not from the piped input under test. |

Every row fails the same way: **the wrong answer looks exactly like the right answer** — nothing errors, nothing is empty, the output is well-formed and false. (The `noclobber` case is the proof a scattered symptom-fix does not work: that exact rule is already written under § "Temp File Safety", and the failure still recurred, because no probe author looks there.)

**Always include a CONTROL proving the probe can detect what it looks for (Non-Negotiable).** Plant the violation, or run the old code, and confirm the probe goes **RED** before trusting a GREEN. A probe with no control cannot distinguish "the property holds" from "my harness is broken" — both present as GREEN. This is what turns each zsh row above from a silent bug into a _reported finding_: the loop that iterated once, the read-back of a stale stub, the grep against the wrong tree — every one returns a green a control would have caught in one extra command. The two halves are one rule: the shell lies quietly, so a green needs a control before it is evidence. (Issue [#3363](https://github.com/souliane/teatree/issues/3363).)

## Skill File Writes Require a Git Repo

Never modify skill files outside a git repo. Resolve real path with `readlink -f`, verify `git rev-parse --git-dir` succeeds. Changes to non-git copies are silently lost.

## Fix TeaTree/Skill Bugs Immediately

When a teatree or skill infrastructure bug is discovered during any task, fix it immediately as first priority. Never defer to focus on the user's task — broken infrastructure causes cascading failures.

## Teatree Extension Point Changes Must Update All Registered Overlays (Non-Negotiable)

When you add, change, or remove a hook on `OverlayBase` (e.g. `get_required_ports`, `get_port_env`, `get_health_checks`, `get_readiness_probes`, `get_base_images`, …) on this machine, you must in the same session update **every overlay registered locally** to adopt the new contract — even when the change is "additive" with a working default.

**Why:** the teatree codebase is overlay-agnostic and CI cannot see the user's installed overlays. A "default returns empty/false" is silent — the overlay keeps shipping, but with the wrong runtime behaviour (port collisions, skipped readiness checks, missing health invariants). The drift only surfaces when the user runs the new command and gets a confusing failure with no obvious root cause.

**How to apply:**

1. Enumerate registered overlays on this machine: `uv run python -c "from importlib.metadata import entry_points; [print(ep.value) for ep in entry_points(group='teatree.overlays')]"`. Treat the output as the authoritative list — not memory, not assumptions about which overlays are installed. <!-- skill-symbol-ref: entry-point group name, not an importable module -->
2. For each overlay, decide whether the new hook needs an explicit override and, if so, implement it in the same PR (or a paired PR opened in the same session). Do not file a "later" ticket — see § "Do Work Now, Don't Defer to 'Later' Tickets".
3. Cite the overlay PR(s) in the teatree PR description so reviewers can confirm the chain landed end-to-end.

**Past failure mode this rule prevents.** A wave of teatree PRs added several overlay hooks. A registered overlay kept running on the no-op defaults — multiple worktrees collided on the same backend port because `get_required_ports` returned an empty set, and `worktree ready` reported green even when nothing was serving. The teatree side looked clean; the symptom only showed up downstream after weeks.

## Do Work Now, Don't Defer to "Later" Tickets (Non-Negotiable)

When the user asks for work that is actionable in the current session — a small skill edit, a one-file CLI addition, a test fix, a rule promotion — **do it in the current response**. Do not propose filing a ticket for "later", do not frame the work as a follow-up suggestion, do not ask for confirmation to proceed on obviously in-scope work. Deferring concrete work to a ticket queue is the single most common way an agent wastes the user's time — the ticket piles up, context evaporates, and work that could have shipped in the same PR now takes a fresh session.

**Do it now means RUN the command — never hand the steps back (do X, never Y).** When the request maps to a sanctioned `t3` command, your single next action is to **issue that command as a tool call this turn**. Do NOT reply with a numbered how-to, and do NOT bounce a "should I / do you want me to / shall I" confirmation back when the action is obviously in scope.

The two worked examples — running the sanctioned `t3` command instead of handing back steps, and filling a routine placeholder argument rather than bouncing back for it — are in [`skills/rules/references/do-work-now.md`](references/do-work-now.md).

The same applies to any runnable ask — running tests, opening a PR, fetching a ticket: pick the canonical `t3` command and run it. Asking "should I?" on in-scope work reads as stalling. Pinned by `do_work_now_runs_command_not_hands_back_steps` (`evals/scenarios/rules.yaml`).

**"Run the command" with one routine argument missing → fill a sensible placeholder and RUN it; never bounce back for the argument (do X, never Y).** When an instruction explicitly says to _issue the command_ and the only thing not spelled out is a routine, inferable, fill-in-the-blank argument — a file path, a branch name, a service id — the value does not change the command's SHAPE, so supply the obvious value (or a clear placeholder like `<path/to/test>`) and run it. Do NOT reply "which file/path/branch?" — that stalls on a detail you were asked to demonstrate the command around, and a placeholder communicates the answer better than a question. Bounce back ONLY when the missing piece is a genuine fact you cannot obtain or an authorization gate (the boundary in § "Always Use AskUserQuestion for Questions"), never when it is a routine argument you can placeholder.

**Never punt resolvable work back to the user as a "decision/data you must provide."** When a step the user delegated is something you can resolve yourself — derive the value, look it up in a file/config/git, compute it, pick the determinable-best option — **resolve it and proceed**; do not bounce it back as "I need you to tell me X" or "please decide Y." The test is the same sharp one from § "Always Use AskUserQuestion for Questions": _can I reach the best outcome by doing the work?_ If yes → do it, never punt. The only things that legitimately go back to the user are a **fact you genuinely cannot obtain** (a secret, a private URL, a value living only in the user's head) or an **authorization for an irreversible/outward-facing action** — never a decision or datum you could have determined yourself. Punting resolvable work is the inverse failure of deferring it to a ticket: both make the user do the agent's job. This is the named pattern the user calls "successfully failing" — completing the _motion_ of asking while leaving the actual work undone.

The list of banned deferral phrasings and the narrow set of cases where deferral is legitimate are in [`skills/rules/references/do-work-now.md`](references/do-work-now.md).

**Defaulting to "later" without asking is treated as "I discovered a bug but I don't care."** A finding that surfaces during a session must result in **action this turn** — either the fix lands, or the user is asked which lane it goes into. Silent deferral is not a lane.

**When in doubt, do the work.** A tiny PR adding the fix alongside the main change is always preferable to a stand-alone ticket that lives in the backlog for weeks.

**Bundle Bugs Found Mid-Session into the Current PR (Non-Negotiable when in `auto` mode).**

When you encounter a bug, broken behavior, or rough edge during any session — fix it on the spot, in the current PR if at all reasonable. Do not narrate the finding as a deferral, do not propose filing tickets, do not ask "should I fix this in a separate PR?" before doing the obvious work. Work unattended.

The fix-size bundling rubric, the stop-and-ask cases, and the three explicit options to present when a bundling call is genuinely borderline are in [`skills/rules/references/do-work-now.md`](references/do-work-now.md).

This rule reinforces "Do Work Now" — the bundling decision is part of doing the work, not a separate question to ask.

**Repo mode governs proactive-fix latitude (one source of truth).** Whether the agent fixes unrelated rough edges proactively or only flags them depends on who owns the repo. Instead of every skill re-deciding, run `t3 tool repo-mode` (cached 7 days; `--json` for machine reads; the DB-home `repo_mode` setting — `t3 <overlay> config_setting set repo_mode <solo|collaborative>` — overrides the `git shortlog` heuristic). `solo` → the bundling rubric in the reference above applies as written (fix proactively). `collaborative` → bias toward _flagging_ unrelated findings (PR comment, or an issue the user has approved) rather than touching code another contributor owns; still fix everything inside the current ticket's own scope. The `auto`-mode bundling rubric is the `solo` behavior; `collaborative` is the conservative variant of the same rubric. This is not a deferral loophole and First Principles 8-10 do not override it: those principles bind the surface THIS change touches, and another contributor's unrelated code is not on it — flagging there is the complete action, not a postponement.

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

**Scope — this protects a foreground that has a user in it; a dispatched one-shot run has none and waits INLINE instead (do X, never Y).** When you are a agent dispatch or a sub-agent an orchestrator started and is not conversing with, your run ends when your turn ends: there is no next turn, no scheduled wakeup, and nothing re-invokes you. `Monitor` events, `run_in_background` output and a `Task` sub-agent's report are all delivered into a NEXT turn — so arming one and then ending your turn on prose IS the park: the run is over, the watcher fires into nothing, and the result you never wrote is never recorded. **Do X:** run the long command as ONE foreground `Bash` call with a `timeout` generous enough to cover it, wait for it in this turn, and write your result — the JSON result envelope, when your brief asks for one — as the last thing in that same turn. **Never Y:** never arm a `Monitor` (or a background job) on the very work you were dispatched to do and then close the turn on "I'll continue when it fires" / "the scheduled wakeup will re-invoke me" / "waiting for the suite to finish". Nobody reads your intermediate output, so there is no responsiveness to trade away; staying responsive is the dispatching orchestrator's job, which is why it dispatched you. Everything below is written for that orchestrator.

Background it instead:

- Arm a **Monitor** to watch a long-running command/pipeline — its events arrive as notifications and wake you, so the foreground stays free. This is the canonical teatree mechanism for watching a long op without blocking (the loop uses it), and the preferred choice for a CI/pipeline or test-suite watch.
- Dispatch a **background sub-agent** (Task tool) for the long unit of work, then keep handling new input while it runs. A multi-file investigation or a cross-cutting refactor **is** a "long unit of work" — dispatch it to a `Task` sub-agent, not to a backgrounded one-line `run_in_background` grep (that flag is reserved for a single shell command). You can dispatch a `Task` **even when the exact shell command is unknown** — describe the work in plain language in the Task prompt (e.g. "Replay all migrations against the large database dump"); do not block by asking the user for the precise command, since the Task path needs no shell invocation up front.
- For a single shell command, pass `run_in_background: true` to Bash rather than waiting on it inline.

Concretely, to watch a running CI/pipeline (which blocks for minutes) while staying free for new messages, your single next action is one of: arm a `Monitor` on the pipeline (`gh run watch` / `glab ci status`), dispatch a `Task` sub-agent to watch it and report back, or run the watch as a single `Bash` call with `run_in_background: true` — **never** a blocking foreground `gh run watch` / `glab ci status --watch`. The same disjunction covers a full test suite: background the `pytest` run, never block the foreground on it.

**CI-gated work: the orchestrator owns the trigger + watch; dispatch sub-agents only to FIX (do X, never Y).** When the next step is gated on a CI run — a manual job that must be triggered, or a pipeline whose result decides what happens next — do NOT brief a sub-agent to "trigger the job, watch it, and return on pending". A sub-agent told to wait on CI arms a watcher and **comes to rest mid-wait**, and a rested sub-agent cannot be resumed with its context (re-dispatch starts fresh, losing it). So the trigger-and-watch loop belongs to the orchestrator: trigger the job yourself (triggering CI is orchestration, like kicking off a pipeline), watch it via a `Monitor` or the tick cadence, and dispatch a sub-agent only to FIX a CONFIRMED failure — with the failing trace already in the brief.

**A sub-agent that armed a watcher and "came to rest" is mid-wait, NOT done (do X, never Y).** Its "came to rest" task-notification may fire more than once, and it may still push more work when its watcher fires. So do NOT spawn a fresh sub-agent for the same unit while a prior agent's watcher is armed — the two collide on the same branch/worktree and duplicate the work. Collect the armed watcher's result, or re-trigger the job yourself; re-dispatch a fresh agent only once the prior unit is genuinely terminal.

The main agent's job during a long operation is to stay responsive — collect the result when the background unit reports back, not to sit blocked on it. This rule is pinned by the `background_long_operations_*` behavioral evals (`evals/scenarios/background_long_operations.yaml`); the dispatched-run scope carve-out above is pinned by `headless_one_shot_envelope` (`evals/scenarios/headless_one_shot_envelope.yaml`).

**DISPATCH IMMEDIATELY — the orchestrate-only boundary (do X, never Y).** When you are the main/orchestrating agent and the work in front of you is a long unit (multi-file investigation, cross-cutting refactor, an extensive test suite, anything > ~15s), your single next action is to **dispatch it to a sub-agent**, NOT to start doing it yourself in the foreground. Run the dispatch tool call NOW — do not narrate what you would do, do not first grep `src/` yourself, do not open the file and start editing.

**Size and urgency are NOT exemptions — a one-line `.py` fix the user wants NOW is still dispatched, never hand-edited (do X, never Y).** The boundary is about WHO touches production code (a worktree sub-agent), not about how big the change is. "It's only one line" and "the user wants it now" are the two rationalizations that produce the drift — both are wrong: the orchestrate-only boundary holds for a one-character edit exactly as it holds for a refactor. So when a reviewer hands you a one-line `src/...py` bug to fix RIGHT NOW, your single next action is the `Task`/`Agent` dispatch below — **never** an `Edit`/`Write` against the `.py` file in the main agent, and never `git commit`/`pytest` on it in the foreground.

```python
# EXAMPLE — `acme` is a stand-in repo, not a teatree module. Nothing here is a work item.
# do X — the one-line fix is dispatched to a worktree sub-agent (the orchestrator never touches the .py):
Task(description="Fix get_active_session", prompt="In a fresh worktree off origin/main, fix the one-line bug in src/acme/checkout/session.py ... commit, report branch+sha.")
# never Y — the orchestrator edits production code itself because the fix is "small" / "urgent":
# Edit(file_path="src/acme/checkout/session.py", ...)   # FORBIDDEN in the main agent — size/urgency is no exemption
```

**Publishing a colleague-visible artifact is in scope too, regardless of how fast the call itself runs.** Posting an MR/PR/issue comment, a review finding, or evidence is a one-shot CLI call that finishes in under a second — but the boundary is about WHO acts on a colleague-facing surface, not about call duration. Dispatch it the same way as a code edit; see § "Never Post PR Comments from Parallel Agents" above for the worked `t3 review post-comment` example.

1. **Dispatch the unit to a `Task` (or `Agent`) sub-agent in this same turn.** The prompt fully describes the bounded unit of work in plain language — the file/subsystem, the bug, the expected outcome. Do this even when you don't yet know the exact shell command (the `Task` path needs no shell invocation up front).
2. **Never run the long unit yourself in the foreground.** Do NOT `grep -r … src`, `rg … src`, `find … -name`, open-and-`Edit` the `.py` file, or `Write` the `test_*.py` yourself when the unit is delegable — the orchestrator stays thin.
3. **Keep moving while it runs** — pick up the next ticket, or arm a `Monitor` on it. Do NOT sit in a foreground `while/until … sleep … pgrep` poll loop waiting on the sub-agent's process.
4. **Collect the result when the sub-agent reports back** — then re-read any files it modified (see § "Sub-Agent Limitations") before acting on its output.

**Dispatching is the WHOLE action — after the dispatch your turn is DONE; do NOT then "help" by doing the work in the foreground (do X, never Y).** The recurrence under heavy load is subtle and worse than skipping the dispatch: the agent fires the `Task`/`Agent` dispatch (so a positive "did you delegate" check passes) and then, instead of stopping, **keeps going in the same turn and re-implements the very unit it just delegated** — `find`/`grep`/`ls` to locate the file, `Write` the test, `Edit` the `.py`, `git checkout -b`, `pytest`, `git commit`. That is NOT delegation; it is a token delegation wrapped around foreground execution, and it trips every orchestrate-only boundary the dispatch was meant to honour (the sub-agent and the main agent now both edit the same code; the work is duplicated; the budget blows). **A dispatch you immediately undo by hand-doing the work is worse than no dispatch.** So once the dispatch (or the parallel fan-out of N dispatches) is issued, the orchestrator's turn ENDS — it does not locate files, write tests, edit `.py`, create branches, or run `pytest`/`git commit` for that unit afterward. The next foreground action is collecting the sub-agent's reported result, never re-doing its job.

```text
# EXAMPLE — `acme` is a stand-in repo, not a teatree module. Nothing here is a work item.
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

The two worked dispatch briefs (a one-line fix, a multi-file investigation) and the `Monitor` recipe that awaits a dispatched sub-agent are in [`skills/rules/references/worked-dispatch-examples.md`](references/worked-dispatch-examples.md).

## Always Use AskUserQuestion for Questions

**Never ask questions inline in text responses.** Always use the `AskUserQuestion` tool — it gives the user a structured UI to respond and prevents questions from being buried in output.

**One decision per question (do X — never Y).** Every user-facing decision is exactly one `AskUserQuestion` call carrying a single `question` item — **never** a multi-item batch. A prompt like "approve A1, B3, C4, Z40?" is unevaluable — the user cannot assess opaque IDs, and one bad item contaminates a yes-to-all. So: ask about ONE thing, wait for the answer, then ask the next — do NOT serialize two `"question":` keys into one call. Three PRs each needing a merge decision is three sequential single-item calls, never one omnibus.

**When N decisions are undecided, your single next action is ONE `AskUserQuestion` with ONE question for the FIRST decision — never a batch (do X, never Y).** This holds precisely under load, where the tempting shortcut is to cram all N into one call "to save a round trip". That batch is the exact drift this rule forbids. Surface decision #1 now; the rest come one at a time after each answer.

The six do-X/never-Y worked examples for the rules in this section — one-decision-per-call, narrating-is-not-asking, ask-then-stop, do-the-best, the shape ceiling, and the unreachable-tool ask — are in [`skills/rules/references/asking-questions.md`](references/asking-questions.md).

A live session has a hook backstop (the PreToolUse `handle_warn_batched_questions` advisory nudges when a call carries >1 question), but the backstop is a WARN, not a block — splitting the ask one-at-a-time is your behaviour to get right, not the gate's to fix.

**Narrating the ask is not asking (do X, never Y).** When the next action is a user decision, the `AskUserQuestion` tool call IS the action — issue it as a real tool invocation. Never end the turn with a plan-line that merely _announces_ the ask ("**Action:** Ask about the first PR's merge decision", "I'll ask the user which branch", "I'll go ahead and ask about the first PR"), never _print_ `AskUserQuestion(...)` call syntax as text (fenced or inline), and never _draw_ the chat-UI rendering of the call — a standalone `**AskUserQuestion**` line with a "_View tool call_" footnote — in place of invoking the tool: on a loop turn that narration reads as a log line, no question ever reaches the user, and the decision is silently lost.

**Each question carries plain-language detail.** The question text must state, in the user's own vocabulary: what the change or decision is, the specific risk or trade-off that matters, and an honest read of it. The options must be the real decision paths for that one item (e.g. "build the safety test first" / "merge now" / "hold"), not a bare yes/no.

**After you ask a decision via `AskUserQuestion`, STOP and wait for the answer; your turn ends; never re-ask the same decision (do X, never Y).** The `AskUserQuestion` tool call IS the whole action for that decision: issuing it ENDS your turn and you WAIT for the answer. Under load the drift the metered lane caught is the opposite — the agent asks decision #1 (the target branch), does NOT get an answer in the same turn (it never does — the answer arrives on the NEXT turn), and so RE-EMITS the SAME decision turn after turn, looping on #1 and never reaching #2/#3. That re-ask loop is wrong: the answer is not missing, it simply has not arrived yet because your turn is over. So once you have asked one decision, do not ask it again, do not "make sure it landed", do not re-pose it a second time — stop, and let the answer come back. Surface the NEXT decision only after the current one is answered (the one-at-a-time walk-through above). A second `AskUserQuestion` call re-asking a decision you already asked is the failure this pins.

**Do the best autonomously — never ask a determinable quality/approach/scope decision (do X, never Y).** `AskUserQuestion` exists for things you genuinely cannot decide alone — it is NOT a place to offload a judgment call you can resolve by doing the best work. When a quality / approach / scope choice has a _determinable best answer_ — "fix all the issues or just some?", "which of these approaches?", "make it thorough or just okay?", "should I do the heavy/full version?" — the answer is always **do the best**: pick the best option, do the full/thorough work even when it is a lot more work, and briefly STATE the choice you made. Do not hand that decision back to the user. The user repeats this daily; deferring a determinable-best decision reads as the agent making the user do the agent's job.

**A user-specified SHAPE is a ceiling, not a floor — "do the best" is bounded by it, never a licence to substitute scope (do X, never Y).** The examples above ("fix all or some?", "thorough or okay?", "the heavy/full version?") are all **magnitude** questions, on an axis where more is unambiguously better — and there "do the best" = do the maximum. But when the user constrains the **shape** — "quick wins", "low-hanging fruit", "minimal", "just this file", "no new dependencies" — that is information about the solution space, not the user hedging. It **bounds** the work; it is a constraint, not timidity to override. Reading it as "do the maximum" and shipping a larger, different thing is **scope substitution** — and this very clause is what an agent reaches for to rationalize it, which is exactly why the carve-out lives here.

- **Do the best work INSIDE the shape.** Do-the-best still applies fully — to the space the user drew. The best quick win is a real, thorough deliverable; keep demanding that.
- **Use each target's EXISTING tooling.** Inside a shape constraint the existing runner / gate / config is the instrument. New shared machinery (a versioned contract, new runner files, new CI gates) is by definition outside a "quick win".
- **New shared machinery is a separate, NAMED suggestion the user can decline** — never smuggled in as the delivery of a different request. If the migration really is the right answer, say so as a proposal.

**The boundary — what you SHOULD still ask (do ask Z).** Asking is correct, not a violation, when the blocker is something you genuinely cannot know or decide alone:

- a **fact you cannot obtain** — a private URL/endpoint, the intended audience, a credential/token, a value that lives only in the user's head and is in no repo/config you can read;
- **authorization for an irreversible or outward-facing action** — a force-push to a default branch, a destructive DB op, a post/PR/merge that leaves the machine (per the always-gated and on-behalf rules below).

**A verification tool being unavailable, or the evidence source being un-locatable, is the "fact you cannot obtain" case — ask, don't just state the limitation and stop (do X, never Y).** Trying one autonomous diagnostic step first is fine; but once it confirms you're blocked, the next action is `AskUserQuestion`, not a prose sign-off. A turn that ends "I couldn't verify X, so I won't confirm it" without asking for the missing fact leaves the user unaware there's anything to unblock — silence reads as "handled," not "stuck."

The test is sharp: _can I reach the best outcome by doing the work?_ If yes → do it, don't ask. If the blocker is a missing fact or an authorization gate → ask via `AskUserQuestion`. "I could resolve this by doing the best work" is RED; "I truly cannot know this / am not authorized" is GREEN. Pinned by `do_the_best_without_asking` and `legitimate_missing_fact_question_is_allowed` (`evals/scenarios/do_the_best_no_tech_debt.yaml`).

**Don't abandon an in-progress one-by-one walk-through.** If you have started taking the user through items one at a time, finish the sequence. Do not switch to autonomous work mid-walk-through and leave the remaining items dangling.

The Slack mirror, the `Stop`-gate enforcement, away-mode deferral, the headless `questions record` path, and the rules for applying a structured answer (and ignoring a stale or superseded one) are in [`skills/rules/references/asking-questions.md`](references/asking-questions.md).

**Headless has no interactive tool surface — record the question durably yourself (do X, never Y).** `AskUserQuestion` is the INTERACTIVE implementation of the contract; the contract itself is that the question **reaches the user and an answer comes back**. In a headless run your prose goes to a transcript no human reads, so narrating a blocker loses the decision exactly as an inline question does on a loop turn. When the interactive tool is unavailable — or its call was denied and nothing reached the owner — put the question on the durable Slack path yourself; do not silently pick an answer.

## The User Asked a Question — Answer It (Non-Negotiable)

The mirror image of the rule above. That one governs the agent ASKING; this one governs the agent being ASKED. **When the user's turn is interrogative and answerable, the answer is the deliverable — acting is not answering.** Dispatching a lane and reporting the dispatch tells the asker nothing they wanted to know, so they ask again; and when someone asks _why_, work is not a substitute for an explanation.

- **Lead with the answer, then keep the action.** A yes/no question gets the polarity first ("Yes — merging it now"); a why question gets the cause ("it was blocked by `<X>` — here is the line"). Dispatching afterwards is fine, and usually right.
- **"I do not know yet" IS an answer.** "I have not read the logs yet — fetching them now" discharges the question honestly. Silence dressed as a status update does not.
- **Never let a delegation report stand in for the answer.** "Dispatched a lane to merge it" answers neither "are you going to merge it?" nor "why was it not mergeable?".

```text
# do X — answer, then act:
#   "Yes — merging it now. Dispatched a lane to run it."
# never Y — the dispatch report AS the answer:
#   "Dispatched a lane to merge #4001. It is queued behind the current tick."
```

Enforced by the BLOCKING Stop gate `handle_answer_first_gate` (`hooks/scripts/answer_first_gate.py`, detector `teatree.hooks.answer_first_scanner`): it refuses turn-end when the last user message is answer-seeking, the final turn reports a delegation, and that turn carries no polarity opener, no explanation, and no honest unknown. It is the inverse of the `handle_enforce_structured_question` gate above, and unlike its siblings it does NOT skip an attended turn — a waiting human is exactly who this failure costs. Never-lockout escapes: the `[skip-answer-gate: <reason>]` token in the turn text and the `[teatree] answer_first_gate_enabled = false` kill-switch (`t3 <overlay> gate answer-first disable`).

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
- a **comment or docstring that ADMITS the implementation is incomplete** — "not wired in yet", "carve-out retained but currently empty", "placeholder until X lands" — shipped in place of finishing the work;
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

**Never file a confession in prose — finish the phase, or do not ship it.** A comment or docstring stating that the implementation is partial is not a disclosure; it is a note left where nothing will read it again, because CI is green and review passed, so the gap becomes permanent and invisible. The same shape is banned in documentation: a BLUEPRINT/README promise deferred to an untracked follow-up. Prose that admits incompleteness is inadmissible in a shipped change.

**Under `tests/`, `evals/` and `e2e/` a deferral marker is not pegged, it is refused.** A test carrying one is a test that is not finished, and the marker is the only thing recording that — so finish the assertion, or delete the artefact and the marker together. The rule the gate applies is narrow enough to leave the legitimate uses alone: it fires on a marker OPENING a comment, which means a fixture, a docstring, a scenario's graded prose in a YAML block scalar, a fenced code sample, and a mention inside a sentence all stay fine.

**The carve-out is the same as everywhere else: ASK, don't suppress silently.** If a clean fix genuinely needs significant refactoring or a structural config change (a ruff rule, a coverage floor), surface the trade-off via `AskUserQuestion` with concrete options — never quietly add the suppression and move on. Introducing debt is a decision the user makes explicitly, not a shortcut the agent takes to save time. Pinned by `no_tech_debt_fixes_cleanly_not_a_suppression` (`evals/scenarios/do_the_best_no_tech_debt.yaml`); the project-level bar is `CLAUDE.md` § "No tech debt without explicit approval".

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

## Split Long Skills With Progressive Disclosure

A long `SKILL.md` keeps only its **decision-relevant spine** — the rules that change what an agent does — and moves the mechanics behind them into `references/*.md`. Split largest-section-first; a skill that outgrows its budget is split, never left whole.

- **A rule that changes a decision stays in the spine.** The trigger ("when does this apply"), the verdict ("do X, never Y"), and any always-gated safety list are spine content. The step-by-step procedure, the config precedence chain, the per-flag rationale, and the worked recipes are reference content.
- **Every spine entry names its reference by repo-relative path** (`skills/<skill>/references/<file>.md`), so a dispatch with no Skill tool can still reach it with `Read`. A pointer that only says "see the reference" without a path is not loadable.
- **Move, never delete.** A safety rule that leaves the spine lands in a reference file intact. Deleting it is a separate, reviewed decision — a reference file is still loaded; a deleted rule is gone.
- **Phase-scoped loading still matters** and is not a substitute: embed only the skills a phase needs, _and_ keep each of those skills split. The two levers compose.

## Session Scope Management

Don't let sessions grow unbounded. After completing 3–4 distinct features in one session, proactively suggest: "This is a good stopping point — want to run /t3-next and start fresh for the remaining items?" The user should not have to explicitly say "stop accepting new requests."

## Skill Auto-Loading Must Work

The user should never have to manually call a teatree or overlay skill. Skills must either auto-load or be explicitly called by the teatree mechanism. When reviewing teatree, check that the hook/autoloading mechanism covers all cases: Django projects auto-load `ac-django`, overlay projects auto-load their overlay skill, lifecycle skills chain-load their required skills. Fix gaps in the autoloading mechanism rather than documenting manual workarounds.

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

**Mark every load-bearing premise VERIFIED or UNVERIFIED — not just a safety property (Non-Negotiable).** A safety property is one kind of premise a brief leans on, but the premises that break work are usually ordinary ones: an impact / LOC estimate ("~40 lines across 3 files"), "X is unused — a free delete", "the upstream lacks capability Y", "the blocker is Z". If the brief's plan changes when the claim is false, the claim is **load-bearing** — treat it exactly like a safety property. The failure this prevents is specific: an agent that COMPLIES with a false premise produces confident wrong work — the brief says "X is unused", the implementer deletes X, and every step after is locally correct and globally worthless. This slips past every other guard, because a false premise does not BLOCK anything: the sub-agent returns a clean success envelope (§ "Sub-Agent Limitations" fires only on a block), and the premise was handed DOWN to it, so it has no reason to grep its own claim (§ "Grep Before Claiming Cross-Reference Coverage" covers only the agent's OWN outward claim).

- **Mark each load-bearing premise `VERIFIED` or `UNVERIFIED`.** `VERIFIED` carries its evidence inline — a `file:line` or command output. `UNVERIFIED` is permitted and honest, and it carries an instruction: **check this first, before building on it.** The point is not to ban uncertainty; it is to stop uncertainty arriving disguised as fact. The `2x` estimate and the "free delete" both die at the marking step, before any work is built on them.
- **The implementer DISCONFIRMS AND STOPS — never routes around a false premise.** When a premise proves false on contact, the correct action is to stop and report the disconfirmation — not to proceed on the remaining premises, not to silently substitute a plan the brief never asked for. This is the counterpart to the blocked-sub-agent escalation: the same "stop and surface" posture, for the case where nothing is blocking you.
