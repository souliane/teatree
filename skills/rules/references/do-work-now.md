# Do work now — closed issues, deferral, extension points, and tech debt

The full text of the `/t3:rules` sections on acting now: closed-issue dispatches, UX patterns, teatree bugs, extension-point changes, § "Do Work Now, Don't Defer to \"Later\" Tickets", contribute mode, directive adoption, auth questions and tech debt, followed by the worked commands, the banned deferral phrasings, the narrow legitimate-deferral set, and the mid-session bundling rubric. The core `skills/rules/SKILL.md` carries each Non-Negotiable rule's trigger and verdict.

## A Dispatch on a Closed Issue Halts and Asks (Non-Negotiable)

A brief is written when the task is QUEUED; the issue can close before the agent starts. So the issue's live state — never the brief — decides whether the work is still wanted. Read it before the first edit of an implementing dispatch (coding, testing, e2e, shipping) and act on what it says.

- **A CLOSED issue halts the dispatch.** `state: CLOSED`, most sharply with `stateReason: NOT_PLANNED`, means the owner already decided against this work; implementing it overrules them and the lines can never land. No edit, no commit, no push, no PR.
- **The halt is a real question, not a prose sign-off.** The two decisions — reopen the issue because the close was wrong, or ignore the ticket because it was right — are the owner's. A turn that only narrates the conflict leaves the ticket to be re-offered next tick, so the same cycle burns again. An implementing dispatch (coding, testing, e2e, shipping) is a HEADLESS run with no `AskUserQuestion` tool on its surface — see § "Always Use AskUserQuestion for Questions" — so the halt is `t3 <overlay> questions record "…"`, the same durable path every other headless blocker uses, not the interactive tool.
- **An OPEN issue is in-scope work you carry forward.** `state: OPEN` with a recorded plan and no blocker is the ordinary case: read the issue body, provision the worktree, write the failing test. Do not manufacture a closure to stall on, and do not ask whether to proceed — the absence of a blocker IS the signal to proceed (`/t3:internals` § "Lifecycle Phases").
- **A state you could not read lets the dispatch through.** A forge outage or an unclassifiable payload is not evidence of a closure. Uncertainty resolves toward proceeding, because a wrong halt blocks live work while a genuine closure is caught by the next disposition sweep anyway.

The deterministic backstop is `teatree.core.gates.closed_issue_dispatch_gate.closed_issue_dispatch_refusal` at the pre-harness dispatch seam beside the plan gate: it refuses an implementing dispatch whose issue the forge reports closed before a turn is billed, and records `FailureKind.ISSUE_CLOSED` (HALT) on the attempt. It fires only once a brief has already reached dispatch — the discipline above is what keeps it from having to.

## Preserve Existing UX Patterns

When fixing a broken UX mechanism (web terminal, browser launch, notification method), fix it **in-kind** — do not replace it with a different mechanism without asking. If proposing a different approach, ask the user first: "Currently this uses X. Want to keep that or switch to Y?"

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

## Run-the-command worked examples

```bash
# "help me create the worktree for this ticket" → RUN it, do not explain it:
t3 <overlay> workspace ticket <id>           # or: t3 <overlay> worktree provision <id>
# never: a prose list of "1. cd …  2. git worktree add …" handed back to the user
# never: AskUserQuestion("should I create the worktree?") on obviously in-scope work
```

```bash
# "Run the ONE command to list the commits that touched this test recently" (path not spelled out):
git log --oneline -- <path/to/test>          # do X — a sensible placeholder, command issued
# never Y: reply "which test file path?" — the instruction said RUN it; the path is a fill-in-the-blank
```

## Banned deferral phrasings and the legitimate-deferral set

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

**When deferral IS legitimate** (narrow set):

- The user explicitly asked for planning only, not execution.
- The work requires an external dependency that is unavailable right now (missing auth, missing approval from a third party, missing DB snapshot).
- The work would genuinely balloon this change into scope creep — and even then, ask the user directly, don't announce a ticket.

## Mid-session bundling rubric, repo mode, and the three-option ask

Decision rubric (apply silently — don't narrate to the user):

| Fix size | Action |
|---|---|
| **Small (≤ ~50 LOC, no architectural decisions)** | Bundle into the current PR. Skip the "Isolate Unrelated Fixes" rule from `t3:ship` — small fixes have lower scope-creep cost than coordination cost. |
| **Medium (related domain, fits the current ticket's spirit)** | Still bundle if the PR title can fairly cover it (e.g., assorted shipping-flow bug fixes during a CLI refactor). Mention in the PR body so reviewers see it. |
| **Large (architectural, cross-cutting, or genuinely orthogonal)** | Create a worktree + PR immediately, implement, ship. No new ticket. |
| **Truly large work that cannot fit a session** | Still ship it — split the run, not the work. A ticket is a record of work in flight, never a place to leave work you already understand (`AGENTS.md` First Principles 8 and 10). |

**Only stop and ask when:**

- The fix has security/destructive blast radius (DB drops, force-push to default, secret rotation).
- The architectural choice has multiple equally valid options.
- The work is genuinely big enough to need its own ticket _and_ the user hasn't opted into auto mode for this overlay.

**When genuinely unsure, ASK — never silently defer.** If the fix is borderline (small but truly orthogonal, or medium-sized but the current PR is already large), present three explicit options to the user via `AskUserQuestion`:

1. **Fix right now and bundle into the current PR** (default — pick this unless reason not to)
2. **Fix it before this PR ships** (same session, same PR — a session TODO entry, never a `TODO` marker left in the code)
3. **Fix it in its own PR, now** (genuinely orthogonal — worktree + PR immediately, no new ticket)

Options 2 and 3 need a concrete reason against option 1; none of the three is a deferral. If the finding is genuinely outside the surface this change touches, state it in the PR body as a finding and let the user decide whether it becomes an issue — `AGENTS.md` § "Issue Creation" forbids filing one without their approval, and First Principles 8-10 forbid filing one for work you could have done here. Asking is acceptable; silently writing "worth filing later" and moving on is not.
