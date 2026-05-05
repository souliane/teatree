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

## User Instructions Are Priority 1

When the user gives a direct, explicit instruction (skip tests, push now, use this approach), execute it IMMEDIATELY. Do not try a "better" approach first, do not retry the same failing approach hoping it works, and do not silently substitute your own plan. Execute the instruction first (it's fast and safe), then suggest an alternative if you have one.

## Classifier Denial Protocol (Non-Negotiable)

When the auto-mode classifier denies a tool call (Bash command rejected, MCP call refused, "permission denied" from the harness, etc.), **stop immediately**. Do not retry, do not work around it with a different command, do not "find another way". A classifier denial is an **immediate session blocker** — handle it before doing anything else.

**Required response, every time:**

1. **Stop.** Drop whatever you were doing. Do not start an alternative approach in the same response.
2. **Inform the user** in plain text: which command was denied, what you were trying to accomplish, and the smallest static permission rule that would have allowed it (e.g. `Bash(gh issue create *)`, `Bash(docker buildx prune *)`).
3. **Suggest the fix.** Provide a paste-ready snippet for the user's `~/.claude/settings.json` (`permissions.allow` array) that, once applied, lets the same command succeed when retried in the **same session**. The snippet must be the smallest rule that covers the use case — never a blanket `Bash` or `Bash(* *)`.
4. **Ask via `AskUserQuestion`** with two options:
   - **"Allow it (relax classifier)"** — user pastes the snippet into `~/.claude/settings.json`, then you retry the original command.
   - **"Keep the denial (do it differently)"** — you propose a concrete alternative path (different tool, manual step, API call) and proceed only after the user picks one.
5. **Wait for the answer.** Do not retry the denied command, do not invent workarounds, do not file tickets, do not start unrelated work, until the user has chosen.

**Banned reactions to a classifier denial:**

- Silently retrying with a different argument shape hoping the classifier passes (`gh issue create` → `gh api repos/.../issues`).
- Switching tools (Bash → MCP, MCP → Python subprocess) to bypass the rule.
- Decomposing the command into pieces that each pass individually.
- Editing teatree's plugin `settings.json`, `CLAUDE.md`, or any plugin-distributed permissions file to add an allow rule.
- Continuing the surrounding work and "leaving the denial for later".

**Why this rule exists.** The classifier exists to give the user a final say on standing-permission expansions. Auto-mode aggressiveness combined with classifier strictness is a recurring source of teatree workflow breakage — agents that retry, decompose, or sidestep silently accumulate scope, lose user trust, and ship work the user never authorized. The right escalation is to **ask once, fix permission at the user-scope settings file, retry**.

**Boundary: who edits permissions where.**

- Teatree (this skill, BLUEPRINT, plugin `settings.json`) defines the _protocol_. Teatree never relaxes permissions on the user's behalf.
- The agent **may suggest** an addition to `~/.claude/settings.json` (user scope) but **must not write** to it — Claude Code's autonomy guardrail blocks those edits, and that's by design. Hand the snippet over; the user pastes it.
- Plugin-distributed permissions (`plugins/t3/settings.json`, `CLAUDE.md` standing clauses) are **never** the right place to relax for a single workflow — that would grant the standing right to every user of the plugin. Refuse if asked to do this; explain that user-scope `settings.json` is the right knob.

## Context Transparency

The user cannot see system-reminders, memory content, or hook output injected into your context. When your response is influenced by any of this invisible context, **briefly state what you received** so the user can follow your reasoning. For example: "Teatree suggested loading `/t3:code`. Memory mentions X."

If the user's message is ambiguous (references "this", "it", a link they forgot to paste, etc.) — **ask for clarification**. Do NOT guess based on context the user can't see. Guessing leads to confusing exchanges where the user has no idea what you're talking about.

## Clickable References

Every MR, ticket, issue, or note reference — in markdown files, platform comments, **and** agent responses — must be a clickable markdown link.

- `[!5657](https://example.com/org/repo/-/merge_requests/5657)` — not `!5657`
- `[PROJ-1234](https://example.com/org/repo/-/issues/1234)` — not `PROJ-1234`

This applies everywhere: MR/PR descriptions, inline comments, test evidence, chat messages, and responses to the user.

## Token Extraction

When extracting an API token from a CLI tool, always extract to a variable first — never inline in curl. See your platform reference (`t3:platforms`) § "Token Extraction" for the platform-specific recipe.

**In Python heredocs:** shell variables are NOT inherited. Use `os.popen(...)` inside Python or `export TOKEN` before the heredoc.

## Temp File Safety

When using temporary files (for MR note bodies, test data, etc.):

- **Never use hardcoded paths** like `/tmp/mr_note_body.md` — stale content from other sessions gets posted to the wrong MR.
- **Always use `mktemp`** or inline Python heredocs instead.
- **Always use `>|`** (clobber override) not `>` — zsh `noclobber` silently prevents overwrite.
- **Always clean up** the temp file immediately after use (`os.unlink()` in Python, `rm` in shell).
- **Exception: pre-compaction snapshots** — files matching `/tmp/t3-snapshot-*.md` are recovered automatically by the `PostCompact` hook. Use `t3-snapshot-${CLAUDE_SESSION_ID:-manual}-$(date +%Y%m%d-%H%M).md` for the filename. Delete after persisting findings to durable storage.

## Complex API Payloads: Use curl or Python

Some issue tracker CLIs cannot serialize nested JSON. **Always use `curl`** with `-H "Content-Type: application/json"` and a proper JSON `-d` body for payloads containing nested objects.

For note bodies containing markdown images (`![alt](url)`), shell variable interpolation and `jq --arg` both escape `!` to `\!`. **Always use Python** (`urllib.request` or `requests`) to serialize the JSON payload.

## Preserve Existing UX Patterns

When fixing a broken UX mechanism (web terminal, browser launch, notification method), fix it **in-kind** — do not replace it with a different mechanism without asking. If proposing a different approach, ask the user first: "Currently this uses X. Want to keep that or switch to Y?"

## No AI Signature on Posts Made on the User's Behalf (Non-Negotiable)

Every artifact you publish under the user's identity — git commits, MR/PR descriptions, MR/PR comments and discussions, issue bodies, Slack/Teams messages, email drafts, release notes — must read as if the user wrote it. **Never append AI/agent signatures or footers**.

**Canonical setting:** `[teatree] agent_signature` in `~/.teatree.toml` (default `false`). Programmatic teatree code paths that post on the user's behalf consult `teatree.identity.agent_signature_enabled()` (or wrap their suffix in `agent_signature_suffix(...)`). When you publish through an external tool (MCP Slack send, `gh` comment, `glab` discussion, raw `httpx`), apply the same policy by hand: omit the signature unless the setting is `true`.

**Banned trailers and footers in any user-on-behalf artifact:**

- `Co-Authored-By: <model> <noreply@anthropic.com>` (or any other agent identity)
- `🤖 Generated with Claude Code` / `Generated with [Claude Code](...)`
- `Sent using Claude` / `Drafted by Claude` / `via Claude` / `(via AI)` / `via the assistant`
- Any emoji-bot signature or "this message was written by …" footer
- Slack-block "Posted by Claude" / "AI-generated" formatting

**This rule is global, not commit-specific.** The original "no Co-Authored-By in commits" rule was a special case; the principle generalizes to every venue where the agent posts on the user's behalf. If you would not put `Co-Authored-By` on a commit, do not put `Sent using Claude` on a Slack message. The user is responsible for the content; the agent is the typist, not the author.

**When the user is the author and explicitly invokes you:** if the user asks for a draft to review before sending themselves, no signature is needed (they will send it themselves anyway). When **you** post on their behalf (Slack DM, MR discussion, GitHub comment, email), the rule still applies — the message must be indistinguishable in form from one the user wrote.

**Failure mode this rule prevents:** the agent appends "Sent using Claude" to a Slack message it sends to a colleague on the user's behalf. The colleague now sees that the user did not write the message themselves; the user looks lazy or impersonal, and the rapport with the colleague is damaged. Same logic for `Co-Authored-By` in commits, "🤖 Generated" footers in MR descriptions, and "via the assistant" suffixes in issue comments.

## Never Post MR Comments from Parallel Agents (Non-Negotiable)

MR/PR comment posting (test plans, evidence, review notes) must be **serialized** — never dispatch two parallel agents that both post comments on MRs. Parallel agents cannot check for each other's posts, resulting in duplicate comments. Post all MR comments from the main conversation thread, or serialize agent tasks so only one posts at a time.

## Verify Repo Visibility Before Filing External Issues (Non-Negotiable)

Before creating an issue, PR, discussion, or any body of content on an external repo, **check the target repo's visibility**:

```bash
gh repo view <owner>/<repo> --json visibility,isPrivate
```

If the target is **PUBLIC**, the body must not contain internal identifiers: customer names, internal GitLab/Jira/Notion URLs, client-specific repo names, ticket IDs from private trackers, CI job/pipeline IDs, local filesystem paths (`/Users/…`, `/home/…`), environment variable values, or internal hostnames. Replace with generic placeholders (`<repo>`, `<namespace>`, `<ticket_url>`, `$T3_WORKSPACE_DIR/<ticket>/<repo>`) before posting.

**Ambiguous destinations need a question.** When the user says "file a bug" without a repo and there are multiple candidates (public upstream vs. private overlay, team repo vs. personal repo), use `AskUserQuestion` to confirm the target before writing the body. Never guess — the cost of asking is low; the cost of publishing internal info is high.

**The authorization to "file a bug" does not authorize posting internal info to a public repo.** User instructions like "file a teatree bug" authorize the _action_ of filing, not the _destination_. A public target always requires a scrubbed body.

## Leak Remediation — Silent Scrubs (Non-Negotiable)

When remediating a privacy leak on a public repo (force-push to drop PII, delete a comment that exposed a credential, rewrite a branch that leaked internal data), **every public artifact produced during the remediation must be neutral**. Do not name what leaked, do not name that a leak occurred, do not describe the scrub. Announcing the remediation on a public surface amplifies the leak (Streisand effect) — the commit subject, the PR comment, and the branch name are all crawled, cached, and indexed.

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

**Before delegating platform API work:** Read the relevant platform reference (`t3:platforms`) before writing sub-agent prompts that involve API calls (draft notes, discussions, MR operations). Sub-agents can't read skills themselves — copy the exact API recipe into the agent prompt.

**After a sub-agent completes, re-read any files it modified.** Sub-agents get a forked copy of your file state — their edits don't update your cache. Writing to a file without re-reading first will silently overwrite their changes.

## Prefer Native Tool APIs Over Filesystem Heuristics

When integrating with tools (issue trackers, CI, chat), prefer their API or CLI over scraping files. File-based approaches break on layout changes, don't handle pagination, and miss metadata.

## Symlink Safety

Never replace a symlink with a real file. `ls -la` first if unsure. If a path is a symlink, edit the target — never delete the link and write a new file.

## Shell Alias Safety

Use `command rm`, `command cp`, `command mv` in Bash tool calls to avoid zsh interactive aliases that hang. Also `gs` is aliased to `git status` — use `command gs` for GhostScript.

## Skill File Writes Require a Git Repo

Never modify skill files outside a git repo. Resolve real path with `readlink -f`, verify `git rev-parse --git-dir` succeeds. Changes to non-git copies are silently lost.

## Fix TeaTree/Skill Bugs Immediately

When a teatree or skill infrastructure bug is discovered during any task, fix it immediately as first priority. Never defer to focus on the user's task — broken infrastructure causes cascading failures.

## Do Work Now, Don't Defer to "Later" Tickets (Non-Negotiable)

When the user asks for work that is actionable in the current session — a small skill edit, a one-file CLI addition, a test fix, a rule promotion — **do it in the current response**. Do not propose filing a ticket for "later", do not frame the work as a follow-up suggestion, do not ask for confirmation to proceed on obviously in-scope work. Deferring concrete work to a ticket queue is the single most common way an agent wastes the user's time — the ticket piles up, context evaporates, and work that could have shipped in the same PR now takes a fresh session.

**Banned patterns when the work is actionable in this turn:**

- "I'd suggest filing a ticket to…"
- "Follow-up (not in this PR)…"
- "Want me to open an issue for …?"
- "As a separate ticket, we should …"
- "File tickets for (a) and (b), or one combined…?"

**When deferral IS legitimate** (narrow set):

- The user explicitly asked for planning only, not execution.
- The work requires an external dependency that is unavailable right now (missing auth, missing approval from a third party, missing DB snapshot).
- The work would genuinely balloon this change into scope creep — and even then, ask the user directly, don't announce a ticket.

**When in doubt, do the work.** A tiny PR adding the fix alongside the main change is always preferable to a stand-alone ticket that lives in the backlog for weeks.

**Bundle Bugs Found Mid-Session into the Current PR (Non-Negotiable when in `auto` mode).**

When you encounter a bug, broken behavior, or rough edge during any session — fix it on the spot, in the current MR if at all reasonable. Do not narrate the finding as a deferral, do not propose filing tickets, do not ask "should I fix this in a separate PR?" before doing the obvious work. Work unattended.

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

## Contribute Mode: Promote Findings to Skills, Not Personal Memory (Non-Negotiable)

When `contribute = true` in `~/.teatree.toml`, retro findings and cross-cutting rules **must land in teatree skill files**, not in the agent's personal memory/config. Personal memory is the fallback for user-specific facts — paths, credentials, editor preferences, one-machine workflow choices. For anything that would help another user of these skills, write to the skill.

**Before writing a feedback/guardrail to personal memory, check:**

1. `contribute = true` in `~/.teatree.toml`? → yes almost always makes this a skill edit.
2. Does the rule encode a guardrail, pattern, or "do this not that"? → skill.
3. Would another user benefit? → skill.
4. Is it a user preference (tone, formatting) or environment fact (path, credential)? → personal memory is legitimate.

**Promote means edit an existing skill.** Pick the best-fit existing skill (`/t3:rules`, `/t3:next`, `/t3:ship`, etc.) and insert the rule there. Do not invent a new skill for a single rule — that fragments the skill graph.

**Past failure (2026-04-24):** Retro saved a scoping-phase auto-enqueue rule to `~/.claude/.../memory/feedback_scoping_auto_enqueue_coding.md` when `contribute = true` and the correct home was `skills/next/SKILL.md`. The user had to explicitly call out the deferral and push for the promotion. Prevention: this checklist.

## Ask About Auth Before External Service Integrations

When implementing features that require an external service (Notion, Slack, CI, etc.), ask "how do you authenticate with this service?" BEFORE writing any code. The answer (direct API token, CLI auth, MCP tool, OAuth, etc.) determines the entire architecture. Skipping this question leads to multiple implementation pivots.

## Never Change MR Base Branch or Dependencies (Non-Negotiable)

When an MR targets a non-default branch, that is intentional — it means the MR is part of a dependency chain. **Never** change an MR's target branch, rebase it onto a different base, or remove MR dependencies without explicit user instruction.

- If asked to "merge main" into a branch, merge the specified source — do not change what the branch is based on.
- If a branch is based on another feature branch (not main/master), keep it that way.
- If unsure about the dependency chain, **ask first**.

Destroying MR dependency chains wastes hours of carefully organized work.

## Always Create Tasks

On **every prompt**, use `TaskCreate` to create tasks before doing any work — even for a single task. Mark each task `in_progress` when starting, `completed` when done. Never skip this. Visible task tracking prevents forgotten steps and shows the user your progress.

- **Simple tasks** (1-2 steps): a brief bullet list in the response is sufficient.
- **Complex tasks** (3+ steps): use the task tracking tools for each step, update status as you go.
- **Never skip this.** If you find yourself doing 3+ things without a plan, stop and create one.

## Always Use AskUserQuestion for Questions

**Never ask questions inline in text responses.** Always use the `AskUserQuestion` tool — it gives the user a structured UI to respond and prevents questions from being buried in output. One question at a time; wait for the answer before asking the next.

## Publishing Actions Are Mode-Conditional (Non-Negotiable)

The setting `teatree.mode` in `~/.teatree.toml` (or the `T3_MODE` env var) picks between two doctrines for publishing actions — push, MR create, MR merge, MR approve/unapprove, remote branch deletion, Slack posts, any write that leaves the local machine. The default is `interactive` (security-conservative). `auto` opts into full autonomy.

### Resolve the effective mode before every publishing decision

Do not assume interactive mode. Before saying "not pushed, your call", before asking "push?", and before prompting for any publishing confirmation, **actively resolve the effective mode in this order** (first match wins):

1. `T3_MODE` environment variable (`auto` or `interactive`).
2. Active overlay config: `[overlays.<active>]` table in `~/.teatree.toml` where `<active>` = `T3_OVERLAY_NAME` env var or the repo's registered overlay.
3. Global `[teatree]` table in `~/.teatree.toml`.
4. Per-repo overrides from agent memory / personal config (e.g. "this repo is auto — don't ask"). These supplement the config.
5. If nothing matched: default to `interactive`.

If the effective mode resolves to `auto`, apply the auto-mode doctrine below — do not ask for push confirmation, do not phrase the end-of-task as "your call", just push.

The most common failure mode is defaulting to `interactive` without performing steps 1-4 — saying "not pushed, interactive mode" on a repo the user has already opted into auto. That reads as the agent ignoring the user's configured preference and forces them to repeat it every session.

### Interactive mode (default)

Commit approval ≠ push approval. **Squash approval ≠ push approval. "All done" ≠ push approval. Rebase approval ≠ force-push approval.** Always present the final state and ask "Push?" as a **separate question** after committing, squashing, or rebasing — use `AskUserQuestion`, not an inline question.

- Every publishing action (push, MR create/update, MR merge, MR approve/unapprove, remote branch delete, Slack post) requires a separate explicit confirmation. "Recheck" / "re-review" / "look again" are verify-only instructions — they do **not** authorize re-approval.
- **Force-push (`--force-with-lease`)**: get separate explicit confirmation even if the user already approved the rebase. A rebase and a force-push are two decisions.

### Auto mode (`t3.mode = "auto"` or `T3_MODE=auto`)

The user has opted into end-to-end autonomy. The agent ships complete features without pausing for confirm prompts on the publishing actions listed above. In particular:

- Push the feature branch after local quality gates pass (lint, tests, `makemigrations --dry-run --check`).
- Open the MR, watch the pipeline, merge when green, delete the remote branch.
- Post the overlay-approved Slack messages (review request, release note) as part of the normal flow.

**Mode is per-overlay.** The setting can live under `[overlays.<name>]` and override the global `[teatree].mode`. A user can run `auto` mode on a personal dogfooding overlay while keeping `interactive` on a client overlay — the active overlay (resolved via `T3_OVERLAY_NAME`) determines which doctrine applies. See `BLUEPRINT.md` § 11.1.1.

**Quality gates still run — they just don't depend on user confirmation.** The objection auto mode answers is "stop gating on _confirmation_," not "skip quality checks."

### Always-Gated Actions (Non-Negotiable, both modes)

Some actions remain confirm-gated regardless of mode because they are irreversible or affect shared history:

- **Force-push to default branches** (`main`, `master`, `development`, `release`, or any branch listed in the overlay's `protected_branches`).
- **History rewrites on shared defaults** — rebase, amend, or filter-branch on any branch another agent or human is tracking.
- **Destructive shared-state ops** — `DROP` / `TRUNCATE` on shared databases, deletions in shared directories, `rm -rf` on paths outside the active worktree.
- **External writes the active overlay has NOT authorised** — posting to channels, repos, or services not listed in the overlay's publishing allow-list.
- **`--no-verify` on any git command** is forbidden in both modes. If a hook fails, fix the underlying issue.

This list applies to all repos, all branches, both modes.

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
- **When context gets compacted**, critical state must survive — see the user's global agent config § Compact Instructions for what to preserve. The `PostCompact` hook automatically recovers any `/tmp/t3-snapshot-*.md` files into context.

## Commit Before Declaring Done (Non-Negotiable)

When implementation is complete (all files written, tests pass or verified), **commit immediately** in the same response — do not wait for the user to ask. An uncommitted change is not "done"; it is in-progress work at risk of being lost to context compaction, parallel agents, or session timeout.

## Pre-Commit Hook Failures on Unrelated Tests

When a pre-commit hook runs the full test suite and fails on tests **unrelated to your changes** (pre-existing failures), do not fix them one by one in a loop. After the **second** unrelated failure, stop and tell the user: the hook is failing on pre-existing test issues, and list the failing tests so they can be fixed separately. Never suggest or use `--no-verify` — see `t3:ship § Never use --no-verify`.

## Worktree-First Work (Non-Negotiable)

**All development work MUST happen in a worktree**, never on the main clone. Use `t3 <overlay> workspace ticket` or the `using-git-worktrees` skill to create one before writing any code.

**Pre-edit check — before editing ANY project file:** If the file path lives directly under `$T3_WORKSPACE_DIR/<repo>/` (not under a ticket subdirectory like `$T3_WORKSPACE_DIR/<ticket>/<repo>/`), **stop** — you are in the main clone. Find or create the correct worktree first via `t3 <overlay> workspace ticket`. The main clone may happen to be on the MR branch (from a previous checkout) — editing there "works" but pollutes the shared clone, risks merge conflicts for other worktrees, and violates isolation.

**Collision detection — check on EVERY file write or git operation:**

1. Before writing to a file, run `git status`. If you see unexpected modifications to files you did not touch, **another agent is working in the same directory**.
2. **If you are NOT in a worktree:** STOP writing code. Move all your work to a worktree immediately (`t3 <overlay> workspace ticket` or `EnterWorktree`), then continue there.
3. **If you ARE in a worktree and see someone else's changes:** STOP ALL WORK IMMEDIATELY. Alert the user: _"ALERT: Another agent is modifying files in my worktree at `<path>`. I've stopped all work to avoid conflicts. Please resolve before I continue."_ Do NOT attempt to continue, merge, or work around the collision.

**Why:** Parallel agents modifying the same checkout cause silent data loss — commits overwrite each other, stashes destroy in-progress work, and merge conflicts go undetected. This has cost hours of wasted work. Worktrees give each agent an isolated copy. The rules below are secondary defenses.

**Pre-task check — before tackling a known issue (failing CI job, regression, "fix X" ticket):** Run `git worktree list` first. If a worktree branch name matches the bug surface (e.g., `ac/fix-e2e-dashboard-*` for dashboard E2E failures, or any branch with relevant commits in `git log --oneline main..HEAD`), **another agent is likely already on it**. Do NOT spawn a parallel worktree on the same problem — coordinate or stand down. The collision rule above catches conflicts at write-time; this catches them before any work starts.

## Concurrent Agent Safety (Non-Negotiable)

Assume another agent may be modifying the same repo concurrently. Never `git stash`, `git checkout --`, or `git restore` files you didn't change — this destroys the other agent's in-progress work. Only stage and commit files you explicitly modified.

## GitLab Inline Comments

When posting inline MR comments, target **added lines only** — not context or unchanged lines.
