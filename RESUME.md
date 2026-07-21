# RESUME — directive #16, CI OAuth account auto-switch

Branch `feat/ci-oauth-token-switch`, off `origin/main` @ `9ce01693`.

**The live CI secret was NOT rotated and must not be by this work.** The mechanism is built
and tested only. The operator switches deliberately at a clean chunk boundary, because cost
figures are not comparable across accounts on different plans.

## The selection rule I settled on

`teatree.ci_oauth_switch.select_account(rows, *, run_start)`. Two separable steps.

**Eligibility.** Candidates are `pass`-stored OAuth rows only (a metered API key cannot fill
an OAuth secret, so it is *filtered*, not rejected). A candidate is ineligible when its
`t3 tokens` status is `EXHAUSTED` (which is already "5h ≥ 95 % **or** 7d ≥ 99 % **or** a
rejected weekly window" — i.e. exhausted on *either* window), `MISSING`, `UNREACHABLE`, or
`OUT_OF_CREDITS`. Each rejection carries an operator-readable reason.

Deliberate choice: exhaustion is judged on the **current** reading even when a reset falls
before `run_start`. An account the local routing selector refuses today is never promoted by
a projection.

**Ranking — lexicographic, two named parts.**

1. `binding_headroom` = `min(headroom_5h, headroom_7d)`. A run is throttled by whichever
   window empties first, so a near-spent 5h window disqualifies an otherwise-rich weekly
   balance (that account stalls at the *start* of the run — the exact failure mode this
   exists to prevent), and vice versa.
2. `weighted_headroom` = `WEIGHT_5H * headroom_5h + WEIGHT_7D * headroom_7d`
   (`0.4` / `0.6`) breaks ties between accounts equally constrained on their binding window.
   Weekly weighs more: a multi-hour benchmark outlasts several 5h windows, never the weekly.

Ties then break on account name, so the choice is deterministic.

**Reset timestamps** enter through `_headroom()`: a window whose reset is `<= run_start`
counts as **fully free**. That is what makes the directive's case work — `the spare account`
at 87 % 5h / 13 % weekly loses a run starting *now* (binding 0.13 < souliane's 0.18) and wins
a run starting after its 5h reset (binding becomes 0.87). `--starting-in <minutes>` on the
CLI moves `run_start` forward.

Why not a plain weighted sum: I tried `0.4*5h + 0.6*7d` first and it picked
`the spare account` for a run starting *now*, which the directive explicitly calls the wrong
answer. A pure weighted sum lets a rich weekly balance mask a starved 5h window. The
binding-window minimum is what encodes "either window can throttle you".

## Implemented

- `src/teatree/ci_oauth_switch.py` — eligibility, ranking, `CiAccountSwitcher` (reads/writes
  via an injected `gh` client + `pass` reader), `NoEligibleAccountError`.
- `src/teatree/cli/eval/ci_account.py` — `t3 eval ci-account show|switch`
  (`--dry-run`, `--json`, `--starting-in`, `--repo`), registered in `_registration.py`.
- `src/teatree/core/gates/gh_token_preflight.py` — two new probes (below).
- `deploy/entrypoint.sh` — the bash mirror of those two probes (pinned equal by a test).
- `.github/workflows/eval-weekly-reusable.yml` — a step recording the run's cost basis.
- `tach.toml` — new `teatree.ci_oauth_switch` domain module; `tach check` green.
- Docs aligned: `BLUEPRINT.md` §preflight, `deploy/README.md`.

**Secret handling.** The token is read from `pass` and piped to `gh secret set --body-file -`
on **stdin** — never an argv, log, return value, or exception message. The failing-secret-set
path deliberately drops `gh`'s output, since that is the one place a value could echo back.
A test asserts the token never appears in captured CLI output.

**Idempotency / cost basis.** A GitHub secret is write-only, so the account *identity* travels
separately as the plain readable repo variable `CLAUDE_CODE_OAUTH_ACCOUNT`. That single
variable is both the no-op key (`switch` compares against it) and the benchmark's cost-basis
attribution. The secret is written *before* the variable, so a half-failure re-writes
harmlessly on the next run rather than recording a lie.

## PAT scope preflight — and one deviation you must review

`gh secret set` on a repo Actions secret is `PUT /repos/{owner}/{repo}/actions/secrets/{name}`:

- **fine-grained PAT** → repository permission **`secrets: write`**
- **classic PAT** → the **`repo`** scope (already covered by the existing `repo` check)

The companion variable write is `PUT /repos/{owner}/{repo}/actions/variables/{name}` →
**`variables: write`** (fine-grained); also bundled into `repo` for classic.

Both are probed side-effect-free by `DELETE` against the sentinel name
`TEATREE_PREFLIGHT_NONEXISTENT` — 404 when permitted, 403 when denied — matching the
existing `_write_probe_verdict` three-way classification. A test asserts every DELETE probe
targets that sentinel, so the probe can never remove a real secret.

**DEVIATION — needs an owner call.** The directive said to add these to the **required** set
so `t3 setup` fails loud. I put them in **RECOMMENDED (WARN-only)** instead. Reason: the
module's documented never-lockout invariant is that only the original four
`REQUIRED_PERMISSION_LABELS` can fail deploy/doctor, and it is pinned by
`test_entrypoint_exit1_paths_reference_only_required_labels`. Widening REQUIRED would mean a
PAT without `secrets: write` **cannot boot the box at all** — a self-healing deployment
locked out by an optional convenience feature. I took the non-destructive reading and flagged
it rather than unilaterally weakening a lockout guard.

To flip it if the owner wants it required: move both labels from
`RECOMMENDED_PERMISSION_LABELS` to `REQUIRED_PERMISSION_LABELS`, change the two `Probe`
tiers from `"recommended"` to `"required"`, move them above the `exit 1` block in
`deploy/entrypoint.sh`, and update `TestRequiredLabels` / `TestRecommendedLabels`.

## Workflow-invoked vs operator-invoked — recommendation

**Operator-invoked for the rotation; workflow-invoked only for recording.** Implemented that
way.

- A workflow that rotates its own auth secret needs `secrets: write` *inside CI*, which
  widens the blast radius of every PR that can influence a workflow file.
- Concurrent runs would race: two benchmark runs starting minutes apart could rotate
  mid-flight and split one benchmark across two accounts — producing exactly the
  incomparable-cost-basis problem the directive wants avoided.
- The rotation changes the cost basis, and the operator explicitly wants that at a *clean
  chunk boundary*, not whenever CI happens to fire.

So the workflow only *reads* `vars.CLAUDE_CODE_OAUTH_ACCOUNT` into the job summary, and the
operator runs `t3 eval ci-account switch` between chunks.

## Verification actually performed

- `tests/test_ci_oauth_switch.py` — **17 passed**
- `tests/teatree_cli/eval/test_ci_account.py` — **6 passed**
- `tests/teatree_core/gates/test_gh_token_preflight.py` + `tests/test_deploy_entrypoint_token_preflight.py` — **62 passed**
- `uv run tach check` — green
- `uv run ruff check` on all touched files — green
- **Anti-vacuity confirmed**: neutering the eligibility filter (`rejected = ()`) turned
  `test_all_exhausted_fails_loud_and_writes_nothing` RED with `DID NOT RAISE
  NoEligibleAccountError`, plus 2 more. Restored and re-verified green.

## NOT verified — do these first

1. `uv run prek run ty-check --all-files` — **never run.** Most likely gap: the deliberately
   un-annotated `_switcher()` helper in `cli/eval/ci_account.py` (it carries
   `# noqa: ANN202` because the concrete type needs a deferred Django import). If ty-check
   objects, prefer a `TYPE_CHECKING` import of `CiAccountSwitcher` over widening the noqa.
2. `uv run ruff format --check` — never run.
3. `bash dev/ci-parity-fast.sh` — never run.
4. Full suite — never run. (`main` is independently RED on a leaked sqlite connection
   causing a nondeterministic `PytestUnraisableExceptionWarning`; fix in flight on
   `fix/scanner-pool-connection-hygiene`. That failure is not from this branch.)
5. Open the PR (non-draft — teatree draft PRs block the autonomous merge loop) with an
   `## Architecture pre-check` section; the ten-check draft is in the gitignored
   `ARCHITECTURE.md` at the worktree root.
6. Surface the REQUIRED-vs-RECOMMENDED deviation above to the owner explicitly.
7. Optional, unimplemented: nothing writes `CLAUDE_CODE_OAUTH_ACCOUNT` for the *current*
   secret, so the first `switch` will report `previous = ""` ("unrecorded") and write
   unconditionally. That is correct and safe, but the operator may prefer to seed the
   variable by hand first so the very first run can no-op.

## Dead ends — do not retry

- **Plain weighted sum `0.4*5h + 0.6*7d` as the primary score.** Picks the weekly-rich,
  5h-starved account for a run starting now — the exact answer the directive calls wrong.
  Weighted geometric mean has the same defect at these values (I checked: 0.41 vs 0.28).
  The binding-window minimum is the part that carries the rule; the weighted blend is only
  a tie-break.
- **Recording the active account in a local `ConfigSetting` row instead of a GH variable.**
  Avoids needing `variables: write`, but CI cannot then see the cost basis, and local state
  drifts from the forge silently. The readable variable is the better seam.
- **Reading the current secret to compare.** Not possible — GitHub Actions secrets are
  write-only. The companion variable exists precisely because of this.
- **Promoting an `EXHAUSTED` account because its 5h reset falls before `run_start`.**
  Considered and rejected; it would let the switcher pick an account the local routing
  selector is actively refusing. Reset projection belongs in scoring only.
