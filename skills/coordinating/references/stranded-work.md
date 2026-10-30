# Stranded work — the five states and the mechanisms that drain them

Work is *work-bearing* from the moment it exists in the working tree. There are five such states, and none of them is a place work may come to rest:

| State | Rests when |
|---|---|
| unstaged in the working tree | committed |
| staged, uncommitted | committed |
| committed, unpushed | pushed |
| pushed, no PR | a PR exists |
| PR open, unmerged | merged, or closed with a reason |

Every state either advances or leaves a durable record that something else drains. Nothing may exit 0 having observed work and stored nothing. The duty that reads them is in [`skills/coordinating/SKILL.md`](../SKILL.md) § "Drain stranded work".

## The mechanisms

The invariant is not kept by remembering it. Three mechanisms enforce it in the factory, each verifiable on its own:

**Durable deferral + drain.** `ensure-pr` runs pre-push, and a branch's FIRST push legitimately has no remote ref to open a PR against. That deferral persists a row carrying the repo, the branch and the PR spec rather than exiting quietly; the `dispatch` loop drains it on a later tick, and a row that ages without draining becomes a `t3 doctor check` failure. Verify: `t3 doctor check`.

**Sweep capture.** A checkout is snapshotted whenever a sweep OBSERVES it — tracked modifications, staged changes and unpushed commits, recorded in the DB rather than only on disk, then aged into a `t3 doctor check` line. Every disposition is covered, KEPT ones included: capturing only before a teardown missed the one disposition that tears nothing down, a row whose ticket is still open, so 75 of 77 registered worktrees held work no surface named. Dirtiness is read with `git status --porcelain` / `git diff HEAD` everywhere it is decided; a bare `git diff` reports zero bytes against a worktree holding only staged work. A checkout this venue cannot resolve is reported as a WRONG VENUE, never as a broken repository — `t3` runs in Docker, so a container-created checkout reads as corrupt from the host. Verify: `t3 doctor check`, `t3 teatree workspace emit`, and `/t3:sweeping-worktrees` for what to do with each emitted item.

**Aged-skip surfacing.** A repeated merge-sweep skip is announced once per PR and reason group while that group continues (the CI verdicts `ci_pending` and `ci_red` are one group, so their flapping is one announcement). A different group, including a flip back to an earlier group after another skip, starts a new episode and is announced again once it has persisted for the threshold; `t3 doctor check` reports every aged hold standing.

## Advancing an item by hand

The states are ordered, so an item usually needs its own next step and nothing more — commit, push, `t3 teatree pr ensure-pr --branch <name> --repo <absolute-worktree-path>`, or let the ship loop take the PR (`t3 loops tick --loop ship`).

**Keep the `--repo` the printed command carries — typing `ensure-pr` by hand without it fails on EXIT 0.** The pre-push hook passes `--repo` itself. Invoked by hand, `t3` execs into a container whose cwd is the image's own `WORKDIR` and never the host's, so `.` is not a checkout and the command refuses by name:

```text
{'error': "the process cwd '<image WORKDIR>' (no --repo given) is not a git checkout on this filesystem. Pass --repo …"}
```

That is the whole failure — **no PR created, and the process exits 0**. `ensure-pr` is the sole subcommand exempted from the loud-refusal contract, because it runs inside the pre-push hook where reporting and letting the push through is the designed behaviour (`/t3:internals` § `soft_refusal_commands`, #792) — so by hand the refusal reads as success and nothing downstream catches it. Pass the worktree's ABSOLUTE filesystem path; a forge slug (`owner/repo`) is refused up front (#2937).

**`ensure-pr` has no `--base`/`--target` — it opens against the repo's DEFAULT branch.** A target is read from the owning ticket, and an orphan branch (the case this command exists for) has no `Worktree` row to carry one. On a stack that is a silent wrong target rather than an error, so retarget the layer in the same turn you create it — `/t3:ship` § "Stacked Delivery — One Stack Per Repo (Default)" carries the command.

Deleting an item is a decision, not a default: `/t3:sweeping-worktrees` covers salvaging unmerged work to a fresh PR versus deleting something demonstrably shipped. The reaper refuses a dirty checkout for that reason, so a kept worktree is not a finished one.
