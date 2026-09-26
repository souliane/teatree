---
name: rules
description: Cross-cutting agent safety rules — the always-embedded core carrying every non-negotiable's trigger and verdict, each naming the skills/rules/references/ file with its full text. Auto-loaded as a dependency by other skills.
compatibility: any
metadata:
  version: 0.0.1
---

# Agent Rules

Cross-cutting rules for all teatree skills, loaded via `requires:`; every rule is binding.
Each entry is the trigger and the verdict; its full text is the named `skills/rules/references/<file>.md`.
Read that file with the Read tool when the entry applies.

## Index

Every stub below names its file. The rules with no stub, by file:

- `skills/rules/references/verification.md`: Read the Canonical Source Before Fixing a Conformance Bug; Re-Verify Cross-Agent State Before Reporting a Dependent Request.
- `skills/rules/references/asking-questions.md`: Context Transparency; Always Create Tasks.
- `skills/rules/references/classifier-denial-escalation.md`: Re-Derive the Minimal Blocker.
- `skills/rules/references/shell-and-files.md`: Token Extraction; Temp File Safety; Complex API Payloads: Use curl or Python; Never Pipe, Redirect, or Chain a gh/glab Publish Command; Prefer Native Tool APIs Over Filesystem Heuristics; Prefer the Teatree MCP Tools Over the `t3` CLI; Symlink Safety; Shell Alias Safety.
- `skills/rules/references/skills-and-sessions.md`: Skill File Writes Require a Git Repo (`git rev-parse --git-dir`); Run Retro Before Ending Non-Trivial Sessions; Context Longevity; Prefer Standard Over Clever; Session Scope Management; Skill Auto-Loading Must Work.
- `skills/rules/references/do-work-now.md`: Preserve Existing UX Patterns; Fix TeaTree/Skill Bugs Immediately; Autonomous Directive Adoption; Ask About Auth Before External Service Integrations.
- `skills/rules/references/worktrees-and-commits.md`: Verify Imports Before Applying External Code; Pre-Commit Hook Failures on Unrelated Tests; Deprecated Code; GitLab Inline Comments.
- `skills/rules/references/worked-dispatch-examples.md`: worked dispatch briefs and the Monitor recipe.

## Invoke Skills Before ANY Response

When a skill might apply — even a 1% chance — invoke it BEFORE responding, exploring, or asking; load every suggested skill and say which you loaded. Full text: `skills/rules/references/skills-and-sessions.md`.

## Verification Before Completion (Non-Negotiable)

No completion claim without fresh evidence read this response; read the state the claim is about (`git show origin/<branch>:<path>`), never a proxy or sha ancestry. A multi-deliverable "done" needs on-target evidence per deliverable; answer a gate rejection with evidence, never concessions (`t3 <overlay> gate completion-claim disable`). Full text: `skills/rules/references/verification.md`.

## A Diagnosis Cites What Was Read (Non-Negotiable)

A causal diagnosis or severity label must cite the artefact read (quoted line, `file:line`, job link); an unread guess says so (`t3 <overlay> gate unbacked-claim disable`). Full text: `skills/rules/references/verification.md`.

## An Acceptance Criterion That Cannot Fail Is Not a Criterion (Non-Negotiable)

Before building against a criterion, name the state that makes it FAIL; pair every absence-satisfied criterion with a positive one only the feature satisfies. Full text: `skills/rules/references/verification.md`.

## A Dispatch on a Closed Issue Halts and Asks (Non-Negotiable)

Read the issue's live state before the first edit of an implementing dispatch. CLOSED halts it — never edit, commit, push or open a PR — and asks via `t3 <overlay> questions record`; OPEN or unreadable proceeds. Full text: `skills/rules/references/do-work-now.md`.

## Grep Before Claiming Cross-Reference Coverage (Non-Negotiable)

Never claim "X is covered" or "a gap" against an external reference without grepping two framings and citing `file:line` (or the missing seam); ask when you cannot grep. Full text: `skills/rules/references/verification.md`.

## Nothing Is Parked on the User — You Own Everything You Know About (Non-Negotiable)

Every item you know about is yours until a peer session acknowledges it or a ticket carries it. The user only answers questions: an item needing them is one question you must ask and chase, never a task on their list. Full text: `skills/rules/references/asking-questions.md`.

## User Instructions Are Priority 1

Execute a direct, explicit user instruction immediately; suggest an alternative only after. Full text: `skills/rules/references/asking-questions.md`.

## On an Ambiguous Directive, Take the Non-Destructive Reading (Non-Negotiable)

When a directive has a destructive and a non-destructive reading, always take the reversible one — read before any `git reset --hard`, checkout or delete. Full text: `skills/rules/references/asking-questions.md`.

## Classifier Denial Protocol (Non-Negotiable)

On a classifier denial, stop: re-issue only in a form the denial reason or an allow rule authorizes; else name the smallest allow rule and ask once via AskUserQuestion. Never retry reshaped, switch tools, or edit plugin permissions (`t3 doctor authorizations` is read-only). Full text: `skills/rules/references/classifier-denial-escalation.md`.

## Anticipate a Predictable Gate: Offer Enable-Setting or Approve-Once, Never Bypass-or-DIY (Non-Negotiable)

When the next action will predictably hit a gate, offer the owner BEFORE it: enable the setting durably, or approve just this once (`t3 review approve-on-behalf`). Never offer "bypass, or do it yourself". Full text: `skills/rules/references/classifier-denial-escalation.md`.

## External Read Failure Must Fail Loud, Never Silent-Empty (Non-Negotiable)

A failed external read must surface the failure — never an empty result a caller reads as truth; a configured fallback is fine. Full text: `skills/rules/references/verification.md`.

## Lead a Completion Report With the Assigned-Work Status

Open with whether the assigned work is done and where (branch, PR, HEAD, gates); findings trail, subordinate. On a standing verified-green goal, lead with the blunt binary and keep it open. Evals run via `t3 eval run`, never `t3 <overlay> run tests`. Full text: `skills/rules/references/reporting.md`.

## Keep Turn Output Terse and TTS-Ready

Lead with the answer, one sentence per point, no decorative markdown, no routine status noise. Full text: `skills/rules/references/reporting.md`.

## Clickable References

Every PR, ticket, issue or note reference is a clickable markdown link. Full text: `skills/rules/references/reporting.md`.

## Render the Title Inline, Never a Bare/Link-Only Id (Non-Negotiable)

Every listed id must render its title inline — `#N (title)` via `teatree.core.ref_render.render_ref` — never bare. Full text: `skills/rules/references/reporting.md`.

## ID Namespace Disambiguation (Non-Negotiable)

Ids are never bare: task ids render `TODO-<n>`, forge ids `<repo>#<n>`. That is prose only — `gh issue view 50 --repo <owner>/<repo>` takes a bare number, never `teatree#50`. Full text: `skills/rules/references/reporting.md`.

## Read Secrets From the Secret Store (Non-Negotiable)

Read every credential from the secret store into a variable at point of use (`TOKEN="$(pass show <service>/api-token)"`); never inline, echo or commit a literal secret. Full text: `skills/rules/references/shell-and-files.md`.

## Read the Canonical Source Before a Structural Action (Non-Negotiable)

Before a structural action (team mode, panes, topology), issue ONE Read of the canonical source (`skills/health/SKILL.md`, BLUEPRINT.md), then act — never from memory, never path-hunting. Full text: `skills/rules/references/skills-and-sessions.md`.

## Overlay Skills Are Scoped to Overlay Repos (Non-Negotiable)

Load the `/t3-<overlay>` playbook for any overlay-repo task before touching code, and only then; a teatree-only change never loads an overlay skill. Full text: `skills/rules/references/skills-and-sessions.md`.

## No AI Signature on Posts Made on the User's Behalf (Non-Negotiable)

Anything published under the user's identity must never carry an AI signature — no `Co-Authored-By` agent trailer, no "Generated with" or "Sent using Claude" footer — unless `agent_signature` is true. Full text: `skills/rules/references/on-behalf-posting.md`.

## Ask Before Posting on the User's Behalf (Non-Negotiable)

The preset's egress posture (`t3 loop preset use`) gates every colleague-visible post, approval or reaction: when it blocks, get approval first. Drafts, self-DMs and replies on your own MR are exempt. The destination picks the credential, never you: `t3 <overlay> notify post --channel <channel> --text '<message>'`, `mcp__teatree__slack_react` or `t3 slack react --channel <channel> --ts <ts> --emoji <name>`, `mcp__teatree__notify_user` or `t3 <overlay> notify send '<body>' --idempotency-key <key>`, `t3 review reply-to-discussion`. Full text: `skills/rules/references/on-behalf-posting.md`.

## Never Post PR Comments from Parallel Agents (Non-Negotiable)

PR comment posting is serialized — one poster at a time — and the orchestrator dispatches each `mcp__teatree__review_post_comment` / `t3 review post-comment` to a sub-agent, never posting itself. Full text: `skills/rules/references/on-behalf-posting.md`.

## Evidence Comes From the Deployed Environment (Non-Negotiable)

Posted proof comes only from the deployed environment (load `/t3:e2e`), never local builds; ask for a missing deploy URL. Only a human-approved `t3 <overlay> ticket e2e-bypass` bypasses the E2E gate; record runs with `mcp__teatree__record_e2e_run` or `t3 <overlay> lifecycle record-e2e-run` and the posted URL. Full text: `skills/rules/references/verification.md`.

## Never Modify a Remote Database Without Explicit User Approval (Non-Negotiable)

Never write to a remote or shared database without the user's explicit approval for that action; reads are fine, and dev-only self-created test data is the one carve-out. Full text: `skills/rules/references/shell-and-files.md`.

## Verify Repo Visibility Before Filing External Issues (Non-Negotiable)

Check the target repo's visibility before filing; a public body must never carry internal names, URLs, ids or paths. Ask when the destination is ambiguous. Full text: `skills/rules/references/leak-remediation.md`.

## Self-Apply `needs-triage` on Agent-Filed Issues (Non-Negotiable)

Always add `needs-triage` to an issue you file that is not a direct user order. Reuse the open backlog — extend a host issue, per `AGENTS.md` § "Issue Creation". Full text: `skills/rules/references/leak-remediation.md`.

## A Filed Issue Separates OBSERVED From INFERRED (Non-Negotiable)

A filed issue must label what was observed (commands, output, `file:line`) apart from the inferred mechanism, and name the experiment that would confirm a single-observation cause. Full text: `skills/rules/references/leak-remediation.md`.

## Leak Remediation — Silent Scrubs (Non-Negotiable)

Every public artifact of a leak remediation must be neutral — never name what leaked, that a leak occurred, or the scrub. Full text: `skills/rules/references/leak-remediation.md`.

## Public-Repo Commit Author Identity (Non-Negotiable)

Commits to a public repo must use a GitHub noreply author and committer email; rewrite with `git filter-branch --env-filter` when the pre-push gate refuses. Full text: `skills/rules/references/leak-remediation.md`.

## Sub-Agent Limitations

Sub-agents lose loaded skills: every raw Agent-tool brief must embed `t3 <overlay> skill-preamble --skills t3:rules,<skills>`. A blocked sub-agent returns a structured block, never a workaround; a killed run's empty report proves nothing — reconcile first. Briefs follow the target repo's conventions, anchor assertions (`t3 <overlay> gate brief-anchor disable`), and bound `PYTEST_XDIST_AUTO_NUM_WORKERS`. Full text: `skills/rules/references/sub-agents.md`.

## Read Before Overwriting a Tracked Config/Dotfile (Non-Negotiable)

Never overwrite a config or dotfile, or `git restore` one, without reading its live on-disk content this session — uncommitted-on-disk beats committed (`t3 <overlay> gate config-overwrite disable`). Full text: `skills/rules/references/shell-and-files.md`.

## Never Cron a `t3 loop` Command From a Session (Non-Negotiable)

The worker owns loop cadence: run `t3 worker status` first, and never register a cron or wakeup that shells `t3 loops tick --loop <name>` or `t3 loop <slot> run` (`t3 loop drain-queue run`) while it is alive (`t3 <overlay> gate cron-loop-shell disable`). Full text: `skills/rules/references/shell-and-files.md`.

## Shell Probes Run Under zsh — a Probe Without a Control Is Unfalsifiable

The Bash tool's shell is zsh: bash idioms answer wrongly without erroring, and a pipe reports the last command's status. Always plant a control that turns the probe RED; a probe spawning shells owns its process group and times out every case. Full text: `skills/rules/references/shell-and-files.md`.

## Teatree Extension Point Changes Must Update All Registered Overlays (Non-Negotiable)

An `OverlayBase` hook change must update every locally registered overlay in the same session, citing those PRs. Full text: `skills/rules/references/do-work-now.md`.

## Do Work Now, Don't Defer to "Later" Tickets (Non-Negotiable)

Do in-scope work now: run the command instead of handing back steps, never punt resolvable work, and fix mid-session bugs in the current PR (`t3 tool repo-mode` — `git shortlog`, or `mcp__teatree__config_setting_set` `repo_mode` — sets the latitude). Full text: `skills/rules/references/do-work-now.md`.

## Contribute Mode: Promote Findings to Skills, Not Personal Memory (Non-Negotiable)

With `contribute` true, guardrails and rules must land in an existing teatree skill (retro findings via `t3 <overlay> retro finding`); personal memory only holds user preferences and environment facts. Full text: `skills/rules/references/do-work-now.md`.

## Never Change PR Base Branch or Dependencies (Non-Negotiable)

Never retarget, rebase onto another base, or unchain a PR without explicit user instruction. Full text: `skills/rules/references/publishing-mode-doctrine.md`.

## Fewest PRs for Related Work — Splitting Requires Approval (Non-Negotiable)

Ship related work as one PR; split it only with the user's explicit up-front approval. Unrelated work gets its own PR. Full text: `skills/rules/references/publishing-mode-doctrine.md`.

## Mid-Task Interrupts (Non-Negotiable)

A new request mid-task becomes a task; say the order and finish the current task unless the new one blocks it. Never silently pivot. Full text: `skills/rules/references/asking-questions.md`.

## Background Long Operations (Non-Negotiable)

An orchestrator never blocks its foreground over ~15s: use a `Monitor` (`gh run watch`, `glab ci status`), a `Task` sub-agent or `run_in_background` — never a foreground `glab ci status --watch`. A dispatched one-shot run waits INLINE. The orchestrator dispatches even a one-line fix, then never re-does the delegated unit. Full text: `skills/rules/references/sub-agents.md`.

## Always Use AskUserQuestion for Questions

Ask through `AskUserQuestion`, one decision per call, then stop; never narrate or re-ask. Do the determinable best without asking, within any user-set shape; ask only for a missing fact or an authorization. Headless: `t3 <overlay> questions record`. Full text: `skills/rules/references/asking-questions.md`.

## The User Asked a Question — Answer It (Non-Negotiable)

When the user asks, lead with the answer — polarity, cause, or an honest "not known yet" — and never let a dispatch report stand in for it (`t3 <overlay> gate answer-first disable`). Full text: `skills/rules/references/asking-questions.md`.

## Never Introduce Tech Debt; Reduce It (Non-Negotiable)

Fix the cause, never suppress it: no `# noqa`, skipped test, lowered floor, TODO or confession comment; reduce debt in files you touch, and ask before any deliberate debt. Full text: `skills/rules/references/do-work-now.md`.

## Publishing Actions Are Mode-Conditional (Non-Negotiable)

Resolve the effective `mode` (`T3_MODE`, then `mcp__teatree__config_setting_set` / `t3 <overlay> config_setting set mode`) before every publishing decision. `interactive` confirms each action; `auto` ships end to end and merges only via `t3 <overlay> ticket clear` then `t3 <overlay> ticket merge`, never raw `gh pr merge`. Full text: `skills/rules/references/publishing-mode-doctrine.md`.

### Always-Gated Actions (Non-Negotiable, both modes)

- Force-push to default or protected branches.
- History rewrites on shared branches.
- Destructive shared-state ops (`DROP`/`TRUNCATE`, shared deletions, `rm -rf` outside the worktree).
- External writes the overlay has not authorised.
- `--no-verify` on any git command is forbidden.

## Three Orthogonal Repo Axes — Visibility, Ownership, Collaboration (Non-Negotiable)

Visibility, ownership and collaboration are independent: a private or owned repo is never colleague-facing by that alone; never auto-merge a colleague's MR. Full text: `skills/rules/references/publishing-mode-doctrine.md`.

## Commit Before Declaring Done (Non-Negotiable)

Commit as soon as implementation is verified, and always before any long pre-push step — uncommitted work in a reaped worktree leaves nothing for `git fsck`. Full text: `skills/rules/references/worktrees-and-commits.md`.

## Worktree-First Work (Non-Negotiable)

All work happens in a worktree (`t3 <overlay> workspace ticket`), never the main clone: check `git rev-parse --show-toplevel`, check `git worktree list` and `git log --oneline main..HEAD` for work in flight, and stop on another agent's changes. Full text: `skills/rules/references/worktrees-and-commits.md`.

## Concurrent Agent Safety (Non-Negotiable)

Never `git stash`, `git checkout --` or `git restore` files you did not change; stage right after each edit and commit only your paths (`git commit -o -- <paths>`). Full text: `skills/rules/references/worktrees-and-commits.md`.

## Split Long Skills With Progressive Disclosure

A long SKILL.md keeps its decision-relevant spine and moves mechanics into `references/*.md`, each named by repo-relative path; move, never delete. Full text: `skills/rules/references/skills-and-sessions.md`.

## Escalate Honesty-Critical Verification to the Most-Honest Model

When honesty is in question, run `t3 <overlay> honesty escalate --reason <user_asked|self_assessed_dishonest|accused_of_lying|shipped_incomplete>` before the next verification spawn. Full text: `skills/rules/references/verification.md`.

## Re-Validate a Reused Guard in a New Destructive Context

Prove a reused guard's safety property holds for the new destructive operation, and mark every load-bearing brief premise VERIFIED or UNVERIFIED. Full text: `skills/rules/references/verification.md`.
