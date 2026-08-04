# Do work now — worked examples, banned deferral patterns, and the bundling rubric

The catalogues and recipes behind `/t3:rules` § "Do Work Now, Don't Defer to \"Later\" Tickets". That section carries the rule; this file carries its worked commands, the banned deferral phrasings, the narrow legitimate-deferral set, and the mid-session bundling rubric.

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
