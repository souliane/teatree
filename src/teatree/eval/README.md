# Behavioral evals

Behavioral evals are runtime checks on agent behavior. A scenario hands a
`SKILL.md` to a one-shot `claude -p` session, watches the resulting
`stream-json` transcript, and asserts the agent reached for the right
tool calls (and avoided the wrong ones). The point is to convert "the
agent knows this rule" into "the agent's compliance with this rule is
observable and gated", so regressions surface as a red test rather than
as a recurring red-card moment.

The harness is intentionally tiny — a YAML loader, a stream-json parser,
and a subprocess wrapper around `claude -p`. There is no test framework
coupling: the runner returns an `EvalRun` dataclass and the matchers
operate on plain captured tool calls.

This file is the dedicated evals-architecture doc: where the parts live,
the tech stack, how runs are triggered (local default, CI manual + weekly),
and the per-skill coverage gate (`t3 eval coverage`).

## Architecture

### Where the parts live

| Concern | Location |
|---|---|
| CLI surface (`t3 eval *`) | `src/teatree/cli/eval/` (`app.py` command wiring; `__init__.py` re-exports `eval_app`; `multi_trial.py` pass@k/matrix; `transcript_replay.py` replay command + resolver; `docker.py` CI-image run; `all.py` lane orchestration + table; `run_modes.py` persist/grade/manifest helpers; `negative_control.py` + `capture_subagent.py` + `history.py` commands) |
| Scenario specs | `src/teatree/eval/scenarios/*.yaml` (core flat catalog) + co-located `skills/<name>/evals.yaml` (a skill ships its own evals beside `SKILL.md`) + each overlay's `eval/scenarios/` (`OverlayBase.get_eval_scenarios_dir()`) |
| Spec discovery | `src/teatree/eval/discovery.py` |
| Grading (matchers, judge) | `src/teatree/eval/report.py`, `matrix.py`, `pass_at_k.py` |
| Transcript readers | `src/teatree/eval/transcript.py` (stream-json), `session_transcript.py` + `subagent_transcript.py` (on-disk session schema) |
| Deterministic lanes | `src/teatree/eval/trigger_qa.py`, `regression_corpus.py`, `negative_control.py`, `transcript_conformance.py` |
| Run-store | `src/teatree/core/models/eval_run.py` (`EvalRunRecord` + `EvalScenarioResult`) |
| Generated corpus | `scripts/eval/corpus_gen/` + `generate_corpus.py` |
| Prek hooks | `.pre-commit-config.yaml`: `eval-skill-triggers` (commit stage → `t3 eval skill-triggers`) + `eval-pinned-regressions` (push stage → `t3 eval pinned-regressions`) |
| CI triggers | `.github/workflows/ci.yml` + `.gitlab-ci.yml` (`eval-weekly`), `scripts/eval/first_pr_of_week.py` |

### Tech stack

Python 3.12+, typer (CLI), Django ORM (run-store only), `claude -p`
`stream-json` for the AI lane, PyYAML for specs. No PyPI package — teatree is
installed editable from a clone; the eval harness ships inside it.

### How it runs

- **Local, on the host (the default).** `t3 eval all` / `t3 eval all
  --free-only` run directly on the host — no container, no setup. This is the
  default for every local invocation.
- **Local, in the CI image (opt-in parity).** `t3 eval all --docker` runs the
  same gate inside `dev/Dockerfile.test` — the exact image the CI test job
  builds — for environment parity when debugging a CI-only failure. Host-run
  stays the default; `--docker` is the explicit opt-in.
- **Prek (deterministic gates).** The two deterministic lanes are wired into
  prek under their explicit names: the sub-second `eval-skill-triggers` hook
  (`t3 eval skill-triggers`, pure frontmatter parsing) runs at the **commit**
  stage, and `eval-pinned-regressions` (`t3 eval pinned-regressions`, real
  git/FSM work) runs at the **push** stage — both token-free, failing the
  commit/push on a real violation.
- **CI manual.** The weekly eval gate can be triggered on demand
  ([#1992](https://github.com/souliane/teatree/pull/1992)).
- **CI weekly.** The metered `claude -p` scenario run runs once per ISO week
  (first PR of the week, or a Sunday cron backstop for a PR-less week); the
  deterministic lanes are gated by prek per push + pytest per PR, not re-run
  here. See "Triggering" below.

## Invocation

```bash
t3 eval list                                # show available scenarios as a rich table
t3 eval all                                  # all lanes (skill-triggers + skill-coverage + pinned-regressions + negative-control + transcript-replay + AI) in one summary table
t3 eval all --free-only                       # the five free deterministic lanes only (no AI lane) — on-demand; the pre-push gate is eval-pinned-regressions
t3 eval all --docker                          # run the gate inside the CI image (dev/Dockerfile.test) for parity; host-run is the default
t3 eval run                                 # run all (DEFAULT backend = subscription, no API spend)
t3 eval run worktree_first                  # run one
t3 eval run --format json                   # JSON output
t3 eval run --format html > report.html     # self-contained HTML report (single-trial; inline CSS, no external assets)
t3 eval run worktree_first --max-turns 5    # override max_turns
t3 eval run --no-persist                     # run without recording to the ledger
t3 eval run --trials 3                        # pass@k: 3 trials, pass if any passes
t3 eval run --trials 3 --require all          # pass^k: regression gate, all must pass
t3 eval run --models opus,sonnet,haiku        # model-regression matrix (per-model columns)
t3 eval run --judge                           # also grade judge-opted scenarios with an LLM judge
t3 eval run --baseline                        # persist + mark this run as its model's baseline
t3 eval run --gate-regressions               # persist + fail on a drop vs each model's baseline
t3 eval history                               # list past recorded runs (newest first)
t3 eval history --baseline                    # show the current baseline run(s)
t3 eval history --mark-baseline 7             # promote run #7 to its model's baseline
t3 eval history --model opus --format json    # filter + JSON
t3 eval run --backend subscription            # explicit subscription (the default)
t3 eval run --backend sdk                       # metered claude -p path (CI; ANTHROPIC_API_KEY)
t3 eval prepare-subscription                  # emit prompts/paths for a subscription run
t3 eval transcript-replay                     # replay a real session against invariants
t3 eval skill-triggers                        # deterministic skill-activation eval (no claude run)
t3 eval coverage                              # per-skill eval coverage (covered / eval_exempt / gap); warn-first, no claude run
t3 eval coverage --fail-on-gap                # Phase-B enforcement: exit non-zero on any uncovered, non-exempt skill
t3 eval pinned-regressions                    # deterministic real-code-path regression corpus (no claude run)
t3 eval pinned-regressions --format json      # JSON: per-class ok/skipped/origin/detail
t3 eval negative-control                      # harness self-test: plant a violation, assert it is caught (no claude run)
t3 eval negative-control --format json        # JSON: caught / violated_rule / offending_tool_call
```

The two deterministic lanes are wired into prek under their explicit names: the
sub-second `eval-skill-triggers` hook runs at the **pre-commit** stage (pure
frontmatter parsing) and `eval-pinned-regressions` runs at the **pre-push** stage
(real git/FSM work) — both token-free, no model, no spec discovery. Each fails
the commit/push on a real deterministic violation. The full free-lane summary
(`t3 eval all --free-only`) — which also folds in the warn-first skill-coverage
lane, negative-control, and the SKIP-when-out-of-scope transcript-replay lane —
stays runnable on demand. Run a single prek lane on demand with:

```bash
prek run --hook-stage commit eval-skill-triggers
prek run --hook-stage push eval-pinned-regressions
```

### Execution backends and the cost split (default = subscription)

A single-trial `t3 eval run` picks one of two backends; **the default is
`subscription`** so a local run never accidentally bills the API after the
2026-06-15 metered-Agent-SDK change.

| Backend | Spend | Who runs it | What it does |
|---|---|---|---|
| `subscription` (default) | none (subscription) | local / manual | grades a subscription-produced `<scenario>.jsonl` transcript |
| `sdk` | metered `claude -p` (`ANTHROPIC_API_KEY`) | CI (`eval-weekly`, explicit `--backend sdk`) | shells `claude -p` to produce + grade the run live |

The free, no-model commands — `skill-triggers`, `pinned-regressions`, and
`transcript-replay` — never invoke any model and are unaffected by the backend.

### `t3 eval all` — the combined multi-lane surface (#1781)

`t3 eval all` runs every lane in one summary table: the five free deterministic
lanes (`skill-triggers`, `skill-coverage`, `pinned-regressions`, `negative-control`,
`transcript-replay`) plus the metered AI lane. The `skill-coverage` lane is
warn-first (reports a gap, exit 0). The AI lane never meters silently — `--backend sdk` opts in.
A missing real transcript SKIPs (never FAILs) the transcript-replay lane, and the
command exits non-zero only on a real FAIL. Driver: `/t3:running-evals`.

`--trials`/`--models` always force the metered `sdk` runner regardless of
`--backend` (a multi-trial / matrix run cannot be served from a single saved
transcript); combining them with the subscription default prints a one-line
metered notice on stderr.

**CI stays on the API path explicitly.** The `eval-weekly` jobs in
`.github/workflows/ci.yml` and `.gitlab-ci.yml` pass `--backend sdk` so CI runs
the budgeted `claude -p` path while LOCAL defaults to `subscription`. (CI also
passes `--trials 3`, which already forces the sdk runner; the explicit
`--backend sdk` is the debuggable statement of that intent.)

**Missing-transcript UX.** With `subscription` the default, a bare `t3 eval run`
before any transcript exists prints, per skipped scenario, the exact expected
path plus the `t3 eval prepare-subscription` + re-run recipe (on stderr) and
exits cleanly — not a silent no-op.

#### subscription backend — two accepted transcript shapes

`SubscriptionTranscriptRunner` auto-detects, per file, which of two shapes a
`<scenario>.jsonl` is and grades both identically — on matchers, no API spend:

- **`claude -p` stream-json** (`transcript.py`) — terminus is the top-level
  `result` event.
- **in-session sub-agent JSONL** (`subagent_transcript.py`) — the session
  schema Claude Code writes under
  `~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl`. This is the
  ONLY transcript a subscription-covered turn produces (spending subscription
  tokens requires an in-session `Agent`). It shares the stream-json
  `message.content[]` block shape (so tool/text extraction is reused verbatim)
  but carries NO `result` event — completion is the final assistant message's
  `stop_reason`, which on disk is frequently `null` (a clean finish, not an
  abort). Feeding it to the stream-json reader returned `("aborted", True)`, so
  a genuinely-produced transcript spurious-failed; the session-aware terminus in
  `subagent_transcript.py` fixes that.

`t3 eval capture-subagent <scenario> --since <epoch>` (`subagent_capture.py`)
locates the freshest sub-agent JSONL (validated by `is_subagent_transcript`) and
copies it to the grader's path. Capture and grade read on-disk files only — the
subscription lane never meters. The `/t3:running-evals` skill drives the full
chain: prepare → dispatch sub-agent → capture-subagent → grade.

#### sdk backend (`claude -p`)

Each scenario invocation shells out to `claude -p` in `--output-format
stream-json` mode with a 120-second wall-clock watchdog and a
`--max-budget-usd 0.10` circuit breaker. When `claude` is not on `PATH` the
runner emits `SKIP <scenario>: claude binary not on PATH` and exits 0.

### pass@k (multi-trial)

A single trial against an LLM is noisy. `--trials k` re-runs each scenario `k`
times and aggregates: `--require any` (default) is **pass@k** — capable-of the
behavior; `--require all` is **pass^k** — a regression gate where intermittent
compliance is itself a failure. The aggregation lives in `pass_at_k.py`.

### Run-store and history (#1160)

Every `t3 eval run` persists to the durable `EvalRunRecord` +
`EvalScenarioResult` ledger (`src/teatree/core/models/eval_run.py`) unless
`--no-persist` is given. One run row carries the model id, the `git_sha`, and a
UTC timestamp; one scenario-result row carries the verdict (pass/fail/skip),
the per-result model, the pass-rate `score` and trial count, the *trajectory*
signal (captured tool calls), the *side-effect* signal (terminal reason + error
flag), the per-matcher detail, and any LLM-judge rationale — so a historical run
is reconstructable without re-invoking the model. `t3 eval history` lists past
runs (newest first) with each scenario's pass-rate.

`--baseline` marks the persisted run as the baseline for its model (demoting the
prior baseline). `--gate-regressions` diffs the just-persisted run against each
model's current baseline and prints `REGRESSED`/`IMPROVED` lines; a scenario
whose score fell exits non-zero. Aggregation and the diff live on the model
(`EvalRunRecord.pass_rates()` / `EvalRunRecord.regression_diff(baseline=…,
candidate=…)`), and the per-model baseline is what the model matrix compares
against. The store is a Django model so history survives across machines that
share the control DB.

### Model matrix

`--models opus,sonnet,haiku` runs the suite once per model and renders a
scenario-by-model table (`pass` / `FAIL` per cell, or the pass-rate under
`--trials`), followed by a per-model tally. It persists one scenario-result row
per `(scenario, model)` cell (unless `--no-persist`); combined with
`--gate-regressions` it flags per-model drops against each model's baseline.
`--format json` emits a
`{models, scenarios:[{name, results:{model:{passed,score,...}}}]}` payload.

### LLM-judge (opt-in, per scenario)

Matcher grading is the default and stays so. A scenario whose pass/fail is not
cleanly matcher-gradeable (tone, faithfulness, "did it actually answer") opts in
to an LLM judge by adding a `judge:` block:

```yaml
- name: explains_change_faithfully
  scenario: the agent's explanation matches the diff it made
  prompt: >-
    ...
  judge:
    rubric: |
      The explanation names every file it changed and does not claim a change
      it did not make.
    model: haiku            # optional, default "claude-sonnet-4-6" (the run tier)
    max_output_tokens: 512  # optional cap on the judge reply
```

A judged scenario passes only when its matchers pass **and** the judge returns
`PASS`. The judge runs only under `t3 eval run --judge`; cost is bounded by the
cheap default model tier, a per-call `--max-budget-usd` cap, and a per-run
`--judge-budget` call cap (default 20). When `claude` is not on PATH the judge
skips (it never fails a scenario by absence). A scenario may carry `judge:` with
no `expect:` (judge-only) or alongside matchers (both must pass).

### Skill-triggers (skill activation)

`t3 eval skill-triggers` is a Layer-1 (deterministic, free, no `claude` run) eval.
It loads each skill's `triggers.keywords` frontmatter and checks the
must-fire / must-not-fire prompt corpus in `trigger_qa_corpus.yaml`: an
under-trigger (in-scope prompt that does not fire) or over-trigger (control
prompt that fires) exits non-zero. A skill author registers expectations by
editing the corpus.

### Pinned-regressions corpus (real gate/checker code paths)

`t3 eval pinned-regressions` is a Layer-1 (deterministic, free, no `claude` run)
eval — sibling of skill-triggers. Where a scenario grades what an agent *says* it
would do, the pinned-regressions corpus (`regression_corpus.py`) grades what the gate/checker
code *does*: each `RegressionCheck` calls the **real** function for a recurring
failure class on a constructed must-block input and a must-allow input, and
reports a violation when either direction is wrong. Checks that need git build
a throwaway repo under `tempfile`; checks that need the ORM run under the test
DB (and skip cleanly when Django is not configured). `tests/eval/
test_regression_corpus.py` proves each check is non-vacuous — a deliberately
broken stand-in for the same code path turns the corpus RED — so a check that
would silently pass on the pre-fix behavior is caught at test time. The corpus
also runs in the normal pytest gate on every PR (via that test), so it is not
gated behind the weekly cadence.

Add a check by appending a `RegressionCheck` to `_CHECKS` with its
`failure_class`, a clickable `origin` URL (the originating fix PR/issue), the
`invariant` it pins, and a `predicate` that returns `True` only when the real
code path still honors the invariant — then add the matching anti-vacuous test.

## Triggering

- **Manual, on demand.** Run `t3 eval run` / `t3 eval run --trials 3` /
  `t3 eval skill-triggers` / `t3 eval pinned-regressions` locally whenever you want.
- **Every commit / push (deterministic lanes via prek).** The
  `eval-skill-triggers` hook gates every commit and `eval-pinned-regressions`
  gates every push (token-free).
- **Every PR (deterministic layers).** The pinned-regressions corpus is exercised by
  `tests/eval/test_regression_corpus.py` in the normal pytest gate on every
  PR, and skill-triggers + the scenario anti-vacuous matchers are pinned by
  `tests/eval/test_scenarios_anti_vacuous.py` / `tests/teatree_cli/
  test_eval.py`. The deterministic, free layers therefore guard every PR
  through pytest — only the paid `claude -p` scenario *run* is weekly.
- **Weekly, on the first PR of the ISO week.** CI runs the paid scenario suite
  once a week — not on every push, not on every PR. The deterministic lanes are
  NOT re-run standalone in the weekly job (prek per push + pytest per PR is the
  single source of truth). `scripts/eval/
  first_pr_of_week.py` decides whether the current MR is the earliest-created
  MR of the current ISO week (order-independent, re-run safe). The rule is
  wired in `.gitlab-ci.yml` (`eval-gate` → `eval-weekly`) and mirrored in
  `.github/workflows/ci.yml` (`eval-weekly` job).

## Failure-class coverage

The pinned-regressions corpus (`t3 eval pinned-regressions`, real code-path checks) and the
behavioral scenarios (`t3 eval run`, agent-trajectory checks) together pin the
recurring failure classes of the last development cycle. Each row names the
class, where it is pinned, and the originating fix:

| Failure class | Where pinned | Originating fix |
|---|---|---|
| migration-fork / multiple leaf nodes | `regression_corpus` (graph leaf count) | [#1721](https://github.com/souliane/teatree/pull/1721) |
| branch-currency §940 (conflict-only, never behind-only) | `regression_corpus` | [#1719](https://github.com/souliane/teatree/pull/1719) |
| substrate-merge human-authorize floor | `regression_corpus` (merge precondition) | [#1498](https://github.com/souliane/teatree/pull/1498) |
| substrate-merge full-autonomy carve-out | `regression_corpus` (merge precondition) | [#1748](https://github.com/souliane/teatree/issues/1748) |
| maker≠checker at merge time | `regression_corpus` (merge precondition) | [#1601](https://github.com/souliane/teatree/pull/1601) |
| loop-owner hijack / pid-anchored lease | `regression_corpus` (lease claim) | [#1724](https://github.com/souliane/teatree/pull/1724) |
| account-switch detect-invalidate-reprobe (`/login`) | `regression_corpus` (full switch-and-verify cycle) | [#1916](https://github.com/souliane/teatree/issues/1916) |
| orchestrator boundary — long work + foreground edit | `scenarios/orchestrator_boundary.yaml` | [#1446](https://github.com/souliane/teatree/pull/1446) |
| structured-question — AskUserQuestion, one decision | `skills/rules/evals.yaml` (co-located) | [#1622](https://github.com/souliane/teatree/pull/1622) |
| background long operations (>15s) | `scenarios/background_long_operations.yaml` | [#1701](https://github.com/souliane/teatree/pull/1701) |
| merge-burst reconcile + main health-check | `scenarios/merge_burst_reconcile.yaml` | [#1721](https://github.com/souliane/teatree/pull/1721) |
| never-edit-main-clone + ff-not-reset | `scenarios/main_clone_protected.yaml` | [#1662](https://github.com/souliane/teatree/pull/1662) |
| do-work-now (run the command, don't hand back) | `skills/rules/evals.yaml` (co-located) | [#1623](https://github.com/souliane/teatree/pull/1623) |
| BLUEPRINT size-budget headroom (trim, don't override) | `scenarios/blueprint_size_budget.yaml` | [#1723](https://github.com/souliane/teatree/pull/1723) |
| CLI read-vs-write effective-flag (`-X GET` is a read) | `regression_corpus` (bare-ref path) + `scenarios/review.yaml` | [#1589](https://github.com/souliane/teatree/pull/1589) |
| overlay-defined skill set loaded — reviewing / coding / planning, regression + generalization (incl. dynamic-workflow reviews) | `scenarios/skill_routing.yaml` | review ran without the overlay skill + legal-entity skill (null review); [#1160](https://github.com/souliane/teatree/issues/1160), [#1640](https://github.com/souliane/teatree/issues/1640) |
| root-cause not dirty-patch (trace origin, never silence the test) | `scenarios/root_cause_not_dirty_patch.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| never post on behalf via the bot token (draft + DM, personal token for colleagues) | `scenarios/never_post_on_behalf_via_bot_token.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| review-claim means review now (eyes → read the diff; skip eyes-claimed MRs) | `scenarios/review_claim_means_review_now.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| background long operations (build / migrate / e2e / clone / await job) | `scenarios/background_long_operations_extra.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| stale-OPEN-issue gate (search before filing, verify number, reconcile before redispatch) | `scenarios/stale_open_issue_gate.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| MR-first-line validation (conventional-commit title, no bare subject) | `scenarios/mr_first_line_validation.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| never foreground-poll CI / deploy / job (background, no sleep-loop) | `scenarios/never_foreground_poll_ci.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| keystone merge not raw `gh`/`glab` (ticket clear+merge, independent reviewer, human-authorized substrate) | `scenarios/keystone_merge_not_raw_gh.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| never edit the main clone (kill-switch relief, worktree+PR for the durable fix) | `scenarios/never_edit_main_clone_extra.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| anti-vacuous self-review before review-request/merge (revert fix → RED proof; don't ship a green vacuous regression test) | `scenarios/anti_vacuous_self_review.yaml` | [#34](https://github.com/souliane/teatree/issues/34) |
| record the SHA-bound anti-vacuity attestation before requesting review (the structural gate's recording seam, not posting un-attested) | `scenarios/anti_vacuous_self_review.yaml` | [#1829](https://github.com/souliane/teatree/issues/1829) |
| blocked sub-agent surfaces a structured block, never silently works around; orchestrator escalates, never swallows | `scenarios/blocked_subagent_escalation.yaml` | [#1915](https://github.com/souliane/teatree/issues/1915) |
| near-zero-comments — agent does not write a code-restating comment first-try (the worked example of the gate-failure feedback loop) | `skills/code/evals.yaml` (`comment_density_writes_sparse_code`, co-located) | [#2024](https://github.com/souliane/teatree/issues/2024) |
| skip the bot's OWN TTS audio attachment on Slack read (transcribe the user's voice note, never the bot's own speech.m4a) | `scenarios/skip_own_tts_audio.yaml` | [#2089](https://github.com/souliane/teatree/issues/2089) |
| private-repo allowlist path-segment match (security — a public slug containing the org as a substring never downgrades) | `regression_corpus` (allowlist resolver) | [#2084](https://github.com/souliane/teatree/pull/2084) |
| banned-terms scanner fail-closed on a crashing scanner (security — a dead/timed-out scanner blocks, never ALLOW) | `regression_corpus` (`scan_text` crash path) | [#2079](https://github.com/souliane/teatree/pull/2079) |
| forge backend resolves by repo origin host, not token precedence | `regression_corpus` (`forge_from_remote`) | [#2085](https://github.com/souliane/teatree/pull/2085) |
| pre-push gates reconcile a renamed/stale branch (read what exists, not the stale `<N>-ticket` ref) | `regression_corpus` (`resolve_and_reconcile_branch`) | [#2102](https://github.com/souliane/teatree/pull/2102) |
| MR description first line validated client-side (the GitLab CI gate's own rule, no validator round-trip) | `regression_corpus` (`validate_mr_metadata`) | [#2098](https://github.com/souliane/teatree/pull/2098) |

The on-behalf / answerer-draft, sweep-merge-never-rebase, review-branch-current,
skill-ref-resolve, and per-phase scenarios (answerer, sweeping-prs, review,
ticket, …) cover the remaining classes already shipped on this branch.

### Co-located evals (`skills/<name>/evals.yaml`)

A skill ships its behavioral evals beside its `SKILL.md`, the way a unit's
tests live next to the unit (the Anthropic skill-authoring convention). A
co-located `evals.yaml` is the **same `EvalSpec` schema** as a flat-catalog
scenario; the only convenience is that a spec which omits `agent_path` defaults
to its owning `skills/<name>/SKILL.md`, so authors don't repeat the path.
Discovery (`discover_specs()`) walks the flat catalog, then `skills/*/evals.yaml`,
then overlays — and rejects a duplicate scenario name across all three sources
with a hard `EvalSpecError`. Co-located specs flow through every lane (`t3 eval
list/run/all`, the anti-vacuity gate, the weekly paid lane) with zero extra
wiring. The flat catalog stays valid — co-location is additive, not a migration.

### Per-skill coverage gate (`t3 eval coverage`)

The per-skill coverage map is **generated, not hand-maintained** — run
`t3 eval coverage` (add `--format json` for a machine read). A skill is
**covered** when ≥1 discovered scenario targets its `SKILL.md` (flat catalog OR
co-located), or **exempt** when its frontmatter carries a non-empty
`eval_exempt: <reason>` (pure-doc / methodology skills). A skill that is neither
is a **gap**. `coverage.py` (`skill_eval_coverage`) is a pure function over
`discover_specs()` + frontmatter — deterministic, free, no model.

The gate is general and declarative: a new `skills/<name>/` with no eval and no
`eval_exempt` trips it by default, and a new skill is covered-or-exempt with a
one-line frontmatter key. It is **warn-first** in Phase A — the coverage lane in
`t3 eval all` and the `tests/eval/test_skill_eval_coverage.py` gate report a gap
but exit 0 (never red-blocking an unrelated push); `t3 eval coverage
--fail-on-gap` is the Phase-B enforcement (a follow-up flip once the team is
ready). The shipped corpus is gap-free today (the 4 co-located seeds — ship,
review, rules, code — plus the pure-doc exemptions).

### Generated catalog (`scripts/eval/corpus_gen`)

A scenario and its three anti-vacuous fixtures (`_pass` / `_fail` / `_noop`)
must stay mutually consistent. The themed scenarios added in [#34](https://github.com/souliane/teatree/issues/34)
(root-cause, on-behalf, review-claim, background-ops, stale-issue, MR-first-line,
no-CI-poll, keystone-merge, never-edit-main-clone, plus broad per-skill coverage
for `workspace` / `ship` / `test` / `code` / `debug` / `ticket` / `sweeping-prs`
/ orchestration / privacy-safety / communication) are declared once in
`scripts/eval/corpus_gen/catalog.py` (+ `per_skill.py`) and emitted by
`uv run python scripts/eval/generate_corpus.py` into both the scenario YAML and
the fixtures. `tests/eval/test_corpus_generation.py` re-runs the emitter and
fails on any drift, and re-checks the anti-vacuous contract from the
declaration, so the catalog is the single source of truth. Hand-written
scenarios (the originals) stay hand-written; only the generated files carry the
`# GENERATED` header.

### Skill-routing scenarios (`scenarios/skill_routing.yaml`)

These pin that a trajectory loads the skill set the **active overlay declares**,
not a hardcoded list. The overlay's declaration is the ground truth:

- `OverlayConfig.companion_skills` — the standing dev + project skills loaded
  alongside the lifecycle skill (the overlay skill, `/backend-dev` or
  `/frontend-dev`, the project's legal-entity skill).
- `OverlayBase.get_review_companion_skills()` → `[pr_review_companion,
  *companion_skills]` — the deduped set a reviewer must hold, threaded through
  `SkillLoadingPolicy.select_for_runtime_phase(review_skills=…)` and
  `agents.skill_bundle.active_overlay_review_skills()`.

Core stays overlay-agnostic (BLUEPRINT § 1), so the prompts use placeholder
identities — `t3-widget` for the overlay workspace skill, `widget-le` for its
legal-entity review skill, `widget-product` / `widget-workspace` /
`widget-microservice` for its repos. An installed overlay supplies the real
names via its own `eval/scenarios/` directory (it maps the placeholder
workspace skill to its own overlay skill, the placeholder legal-entity skill to
its own, and reuses `backend-dev` / `frontend-dev` unchanged); the contract
under test is identical. Grading inspects the agent's `Skill` tool calls
(`input.skill`).

#### Coverage matrix

The user's requirement is that **every** phase load the right skill set —
across core / companion / overlay tiers, in both the *regression* direction
(the exact must-load case) and the *generalization* direction (a held-out case
where the prompt states the rule but withholds the skill names, so a green
trajectory has to derive the set rather than pattern-match the prompt).

| phase | tier(s) under test | direction | scenario |
|---|---|---|---|
| coding | overlay + dev | regression | `overlay_repo_task_loads_overlay_skill` |
| coding | overlay + dev + **companion** (`ac-django`) | regression | `overlay_django_coding_loads_companion_bible` |
| coding | overlay + dev + **companion** (`ac-python`) | **generalization** | `overlay_python_coding_generalizes_to_python_bible` |
| coding | non-overlay (must NOT load overlay skill) | regression (negative) | `non_overlay_task_does_not_require_overlay_skill` |
| reviewing | overlay + `/t3:review` + dev + legal-entity | regression | `overlay_review_loads_overlay_review_skill_set` |
| reviewing | overlay + `/t3:review` + dev | **generalization** | `overlay_review_generalizes_to_declared_skill_set` |
| reviewing | dynamic-workflow spawned (overlay set from dispatch prompt) | regression | `workflow_spawned_review_loads_overlay_skill_set` |
| reviewing | non-overlay (must NOT load the overlay skill) | regression (negative) | `non_overlay_review_does_not_load_overlay_skill` |
| planning | core planning + overlay | regression | `overlay_planning_loads_planning_and_overlay_skill` |

Notes on the load-bearing cases:

- **Companion bible on coding** (`overlay_django_coding_loads_companion_bible`,
  `overlay_python_coding_generalizes_to_python_bible`): the project dev skill
  (`/backend-dev`) is not enough — the generic language bible it layers on
  (`/ac-django` for Django, `/ac-python` for plain Python) must load too. The
  Python case is held out: the prompt says "load the bible that matches THIS
  service's language" but never names `ac-python`, and loading `ac-django`
  there (pattern-matching the Django case) FAILS. This is the exact class the
  user flagged ("I'd been loading only `/backend-dev`//`/frontend-dev`").
- **Overlay review, generalization**
  (`overlay_review_generalizes_to_declared_skill_set`): the prompt does NOT
  enumerate the review set — it only states the overlay declares it via
  `get_review_companion_skills()` and that a review without that set is null. A
  green trajectory derives `overlay skill + /t3:review + dev` itself. This puts
  the null-review incident under test without spoon-feeding the skill names.
- **Dynamic-workflow review**
  (`workflow_spawned_review_loads_overlay_skill_set`): a review running inside a
  spawned sub-agent starts cold (skill prose does not propagate into a spawned
  agent — see `skills/ship/SKILL.md` § "Review Gate"), so the overlay set named
  in its dispatch prompt must be self-loaded before the diff is read. The
  reviewing-phase evidence gate
  ([review-skill evidence gate](https://github.com/souliane/teatree/issues/1539))
  is the code-side complement.
- **Negative directions**: a teatree-only change/review loads its framework
  skill but must NOT pull in the overlay skill — the over-load failure
  symmetric to the missing-overlay-skill one
  (`non_overlay_task_does_not_require_overlay_skill`,
  `non_overlay_review_does_not_load_overlay_skill`).
- **Planning** (`overlay_planning_loads_planning_and_overlay_skill`): planning
  an overlay change loads the core planning skill plus the overlay workspace
  skill before any plan file is written. Per [#1640](https://github.com/souliane/teatree/issues/1640)
  the planning signal is *implementation* planning (architecture-design), not
  `teatree-plan` board prioritization.

#### Anti-vacuity

Every scenario ships three fixtures — `_pass` (compliant → GREEN), `_fail`
(regressing → RED), and `_noop` (no tool calls → RED). The `_noop` fixture is
what proves the scenario is not vacuous: a spec made only of negative matchers
(`no_tool_call_matching`) is trivially satisfied by an agent that does nothing,
so each scenario carries a positive `Skill` matcher that a no-op transcript
fails. `tests/eval/test_scenarios_anti_vacuous.py` runs all three directions on
every PR, so a toothless skill-routing matcher cannot merge.

## Run history and baselines

```bash
t3 eval history                             # recent runs + per-scenario pass-rate
t3 eval history --model haiku               # scope to one model
t3 eval history --baseline                  # show the current baseline run(s)
t3 eval history --mark-baseline <run-id>    # promote a run to baseline
t3 eval history --format json               # JSON for tooling
```

The aggregation and diff live on the model — `EvalRunRecord.pass_rates()` and
`EvalRunRecord.regression_diff(baseline=…, candidate=…)` — and are surfaced
through `t3 eval history` and `t3 eval run --gate-regressions`. See the
"Run-store and history" section above for the persisted shape.

## Scenario shape

Scenarios live in `src/teatree/eval/scenarios/*.yaml`. Each file holds a
YAML list of one or more specs.

```yaml
- name: worktree_first
  scenario: agent must create a worktree before editing the canonical clone
  agent_path: skills/code/SKILL.md
  model: haiku            # optional, default "claude-sonnet-4-6"
  max_turns: 3            # optional, default 4
  tools: [Bash]           # optional, default [Bash]
  prompt: >-
    You are working in <path>. ...
  expect:
    - tool_call: bash
      args.command: contains "git worktree add"
    - no_tool_call_matching:
        bash.command: ~ "Edit.*README\\.md"
```

Fields:

- `name` — unique identifier; used by `t3 eval run <name>` and as a test id.
- `scenario` — human-readable one-line description; printed by `t3 eval list`.
- `agent_path` — path to a `SKILL.md` (relative to the teatree repo root).
- `prompt` — full prompt text passed as the user message.
- `model` — Claude model alias (default `"claude-sonnet-4-6"`).
- `max_turns` — turn budget for the CLI (default `4`).
- `tools` — allow-list of tools exposed to the agent (default `["Bash"]`).
- `expect` — list of matchers (see below); required unless a `judge` block is
  present (a judge-only scenario may omit it).
- `judge` — optional LLM-judge block (`rubric`, optional `model`, optional
  `max_output_tokens`); see "LLM-judge" above.

Supported matcher operators:

- `tool_call: <tool>` with `args.<path>: contains "<substring>"` — at
  least one matching tool call must exist.
- `tool_call: <tool>` with `args.<path>: ~ "<regex>"` — at least one
  matching tool call must exist (regex variant). Use this as the
  positive matcher that pairs with a `no_tool_call_matching` line to
  prevent the scenario from being satisfied vacuously by a no-op
  transcript.
- `no_tool_call_matching: { <tool>.<arg>: ~ "<regex>" }` — no matching
  tool call may exist.
- `any_of: [ <tool_call branch>, ... ]` — a disjunction of positive
  `tool_call` branches; the entry passes when **any** branch holds. Use it
  to pin a rule that a documented set of equally-valid actions satisfies —
  e.g. "background the long op via a `Task` dispatch OR a Bash call with
  `run_in_background: true`" — so a compliant response taking either branch
  stays green instead of over-fitting to one. Branches are positive only.

A scalar arg value that is not a string (a boolean / number such as Bash's
`run_in_background: true`) is compared against the operator as its `str()`
form, so `args.run_in_background: ~ "(?i)true"` matches.

## Adding a scenario

1. Decide on the surface:
   - **Core** (`src/teatree/eval/scenarios/`) — cross-overlay invariants.
     Fixtures use placeholder identities (`widget-user`, `U_USER`,
     `https://example.com/widget/example/pull/42`).
   - **Overlay** (`<overlay>/eval/scenarios/`) — scenarios that reference
     tenant identities, per-workspace channel ids, or overlay-specific
     banned-jargon lists. The overlay class returns the directory from
     `OverlayBase.get_eval_scenarios_dir()`.
2. Pick the smallest `agent_path` that exhibits the behavior (a single
   `SKILL.md`, not a bundle).
3. Keep prompts hermetic — no real network, no secrets — and keep
   `max_turns` low so a single run costs cents, not dollars.
4. Ship at least a `<name>_fail.stream.jsonl` fixture under
   `tests/eval/fixtures/`. Add a `<name>_pass.stream.jsonl` when the
   behavior shape is binary. The `test_scenarios_anti_vacuous` pytest
   parametrizes every shipped scenario against its fixtures and asserts
   the fail fixture goes RED and the pass fixture goes GREEN — a
   matcher-toothless scenario is caught at test time, not in production.
5. Run `t3 eval list` to confirm the scenario shows up in both core and
   overlay surfaces. Run `t3 eval run <name>` to invoke a live
   `claude -p` session when you want to confirm the prompt fires the
   intended behavior end-to-end.

## Overlay-contributed scenarios

Overlays register a scenarios directory by overriding
`OverlayBase.get_eval_scenarios_dir()` to return the absolute path of
their `eval/scenarios/` directory. `discover_specs()` walks every
installed overlay's directory after the core catalog. Discovery is
isolated: a broken overlay (missing dir, malformed YAML, raising hook)
is logged and skipped rather than failing the catalog.

## Layered enforcement

Behavioral rules fall into two layers:

- **Layer 1 — integration tests.** When a rule is code-enforceable
  (e.g. "scanner skips MRs the user authored", "Slack reaction path
  short-circuits on `ticket.role == 'author'`"), pin it with a real
  pytest test that mocks the boundary (Slack transport, GitLab API)
  and asserts the side-effect is absent on the violating input. The
  canonical reference is `tests/teatree_backends/test_slack_reactions.py`
  (`test_skips_eyes_on_authored_ticket` — landed via PR #1329).
- **Layer 2 — transcript scenarios** (this directory). For
  LLM-output-only behaviors where the rule constrains what the agent
  *says* or *invokes* rather than what a code path *does*
  (e.g. "agent does not declare 'done' without artifact evidence",
  "stakeholder messages avoid code jargon"), a YAML+JSONL scenario
  captures the captured tool-call shape and applies matchers.

Prefer Layer 1 every time it applies — code-level tests run in CI for
free; eval scenarios require a paid Claude run. Layer 2 is for what
Layer 1 cannot reach.

## Transcript-replay conformance (the other half)

The scenario harness above runs a *fresh* `claude -p` session and watches the
**stream-json** CLI output (`transcript.py`). The transcript-replay eval
(`session_transcript.py` + `transcript_conformance.py`, `t3 eval
transcript-replay`) instead replays the **on-disk session JSONL** that Claude
Code already wrote under `~/.claude/projects/<slug>/<session-id>.jsonl`, and
asserts a set of deterministic behavioural invariants held over that real run.

**Two schemas, one reader trap.** The stream-json schema and the on-disk
session schema are NOT interchangeable — `transcript.py` parses the former,
`session_transcript.py` the latter. The on-disk envelope carries
parent/child uuids, a sidechain marker, cwd, git branch, and folds hook
outcomes in as `attachment` events (`hook` / `hook_success` / `hook_*`, carrying
`hookEvent` / `exitCode` / `command` / privacy-sensitive `stdout`/`stderr`)
rather than as a separate stream. A `Skill` tool call carries `input.skill`
(e.g. `t3:teatree-plan`). The parser is fail-soft: a missing field or an
unrecognised hook discriminator yields a best-effort event rather than raising,
because the on-disk schema drifts between Claude Code versions.

It complements the gate-liveness corpus (`tests/test_gate_liveness_corpus.py`,
[#168](https://github.com/souliane/teatree/issues/168)): #168 proves a gate
**can** fire on a synthetic must-DENY payload; transcript-replay
([#169](https://github.com/souliane/teatree/issues/169)) proves the invariants
**did** hold (or weren't needed) in real runs. Four GREEN-tier
(`confidence="deterministic"`, low false-positive) invariants ship live —
`no_edit_in_main_clone`, `no_raw_out_of_band_merge`, `no_raw_review_post`,
`no_raw_slack_overlay_post`.

```bash
t3 eval transcript-replay                       # newest session for this project
t3 eval transcript-replay --session <id>        # a specific session in scope
t3 eval transcript-replay --file <path.jsonl>   # an explicit file
t3 eval transcript-replay --format json         # JSON report
```

**Privacy:** local-only, stdout-only, no transport, and project-slug scoped so
it never reads another project's logs. The report emits ONLY the invariant id,
the offending event index, the tool name, and the fixed description — never a
tool input, prompt text, hook stdout/stderr, file contents, or any quote. Its
fixtures (`tests/fixtures/transcripts/`) are hand-written synthetic placeholder
sessions; a real session log is never committed.

The command-shape regexes and the plan-skill recognition predicate are MIRRORED
from `hooks.scripts.hook_router` (not imported, to stay independent of the
concurrently-evolving router and the tach module-edge rules); a lockstep test in
`tests/test_transcript_replay_conformance.py` asserts they stay equal to the
router source.

## Gate-failure feedback loop

`t3 <overlay> retro gate-failures` ([#2024](https://github.com/souliane/teatree/issues/2024))
closes the loop from "a quality gate fired on agent output" to "an eval that
stops the gate firing first-try".

**The real on-disk schema (verified against `~/.claude/projects/*/*.jsonl`).** A
teatree gate BLOCK is a `hook_blocking_error` attachment carrying NO `exitCode`;
the gate identity lives in `attachment.blockingError.blockingError`, whose text
leads with a `TEATREE GATE — <phrase>` marker. `attachment.hookName` is the
EVENT:TOOL bucket (`Stop`, `PreToolUse:Bash`) — never a gate name — and
`attachment.command` is the same `hook_router.py` runner invocation across every
gate, so neither identifies the gate. (`TEATREE LOOP SELF-PUMP` is the same
attachment type but a continue-the-loop signal, not a failure.) A
`hook_non_blocking_error` carries `exitCode:1` and is an infra/dependency
breakage (a missing plugin dir, a hook-runner traceback).

The command reads the SINGLE transcript chokepoint (`extract_hook_events` — no
per-gate instrumentation), keys on the attachment TYPE plus the marker (NEVER on
`exitCode`, which a blocking gate lacks), excludes the self-pump signal, and
reduces the gate identity to a bounded slug (`gate_identity_slug`). It classifies
each `preventable` / `environmental` via one declarative table
(`gate_failures._ENVIRONMENTAL_SLUG_FRAGMENTS`: any `hook_non_blocking_error`
infra breakage is environmental; everything agent-output-shaped, and any unknown
gate BLOCK, is preventable — fail toward an eval), records each to the durable
per-key store (so recurrence across sessions is observable), and emits JSON + a
human summary.

`--escalate` files one scoped, deduped enforcement issue per *recurring*
*preventable* failure, reusing `core.review_findings.file_class_c_issue` so it is
fingerprint-deduped (a re-run never refiles), banned-terms-safe (a hit withholds
rather than leaks), and clickable-link safe. The issue body names the
preventable gate, its recurrence, and the smallest anti-vacuous eval to stop it
first-try; labels are `enforcement-gap` + `needs-triage`.

```bash
t3 <overlay> retro gate-failures                       # latest in-scope session
t3 <overlay> retro gate-failures --file <path.jsonl>   # an explicit session log
t3 <overlay> retro gate-failures --session <id>        # a specific session in scope
t3 <overlay> retro gate-failures --escalate --repo <slug> --pr-url <url>
```

**Privacy by construction.** `GateFailure` carries ONLY the gate-identity slug +
the session id — NEVER the blockingError message, the `stderr`, the `command`, or
`stdout` (the diff/banned content the gate was reacting to). The slug for a
blocking gate is a bounded first-sentence token from the gate's OWN fixed marker
text; a non-blocking `stderr` is arbitrary so it is matched to a canonical infra
slug and never echoed verbatim. The fingerprint hashes the slug, so two firings
of the same gate (any session, any tool) hash together while a different gate
hashes apart. The extractor and classifier live in `eval/gate_failures.py` (layer
`integration`), not `core` (layer `domain`) — a `core -> eval` import is a
backwards tach edge; the `retro` command (layer `interface`) calls into `eval` on
a forward edge.

The **worked example** (the ticket's whole point) is the
`comment_density_writes_sparse_code` scenario co-located in `skills/code/evals.yaml`:
the agent tends to write code-restating comments, the comment-density gate blocks
them post-hoc, and this eval asserts the agent's first-try output passes the gate
so the trial-and-error cycle stops. Its `_fail` fixture (a transcript that writes
a restating comment) goes RED, proving the eval is anti-vacuous.

**Out of scope (clean follow-up):** auto-invoking `gate-failures` from the loop
tick / retro synthesis is orchestrator-only ([#837](https://github.com/souliane/teatree/issues/837))
and lands separately; this PR ships the deterministic extractor + classifier +
CLI only.

## Negative control (harness self-test)

`t3 eval negative-control` ([teatree#1160](https://github.com/souliane/teatree/issues/1160)
AC5/AC6) is the harness's own self-test: it plants a known rule violation (an
agent editing the canonical clone without `git worktree add` first), drives it
through the *public* report path, and exits 0 only when the harness reports the
violation — naming the violated rule and the offending tool call. It is
token-free and deterministic (it never shells `claude -p`), so it runs as one of
the free lanes `t3 eval all --free-only` gates on. A non-zero
exit means the harness went green on a genuine violation, i.e. the harness
itself is broken.

It is anti-vacuous by construction: `src/teatree/eval/negative_control.py`
builds both a violating run (caught) and a compliant run (not caught) of the
same scenario, and `tests/eval/test_negative_control.py` asserts the control
fires on the former and stays quiet on the latter.

The generic per-scenario anti-vacuity gate (`tests/eval/test_scenarios_anti_vacuous.py`)
proves every scenario's `_fail` fixture drives a red *verdict*; the negative
control additionally proves the red *report content* (the violated rule + the
offending tool call) is emitted in both text and JSON.

## Deferred

- Final-state matcher.
- The remaining pain-point catalog from
  [teatree#1160](https://github.com/souliane/teatree/issues/1160) beyond the
  5+ scenarios already shipped (CI integration, UI/screenshot eval, perf
  benchmarking — all flagged out-of-scope in the ticket itself).
- Transcript-replay AMBER/RED-tier invariants (correlative / judgement
  confidence) and loop-signal-derived invariants — the conformance registry
  ships GREEN-tier only for now.
- Catalog linkage to [#166](https://github.com/souliane/teatree/issues/166): the
  `Invariant.catalog_ref` field is wired but unset.
