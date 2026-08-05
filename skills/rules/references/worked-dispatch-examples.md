# Worked dispatch examples

The worked recipes behind `/t3:rules` § "Background Long Operations" and § "Sub-Agent Limitations". Those sections carry the rule; this file carries the dispatch briefs, the Monitor recipe, and the branch/draft convention examples.

## Dispatch briefs and the Monitor recipe

Worked dispatch — a one-line fix a reviewer found, delegated rather than edited in the foreground:

```text
# EXAMPLE — `acme` is a stand-in repo, not a teatree module. Nothing here is a work item.
Task(
  description="Fix get_active_session",
  prompt="In a fresh worktree off origin/main of this repo, fix the one-line bug in "
         "src/acme/checkout/session.py: get_active_session() returns None instead of "
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

Arm a Monitor to await a dispatched sub-agent instead of foreground-polling its process. It is
the harness `Monitor` TOOL, not a `t3` subcommand — and its command carries its own deadline,
because an unbounded `until … sleep` is refused by the bounded-wait gate:

```text
Monitor(
  command="timeout 1800 bash -c 'until <the artifact exists>; do sleep 20; done' || echo TIMED-OUT",
  description="subagent-42 completion",
)
```

## Dispatch-prompt hygiene — branch scheme and draft default

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
