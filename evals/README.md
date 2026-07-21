# Agent-behavior evals & tests — harness reference

> **Start with the guide.** The concise end-to-end walkthrough — where evals
> live, how grading works, the three cost tiers, how to run, with-skill/baseline,
> what CI does — is
> [`docs/testing-skill-evals.md`](../docs/testing-skill-evals.md). This file is
> the **reference**: directory layout, scenario schema, every matcher operator,
> the canonical lane/tier table, and the failure-class index.

This top-level `evals/` tree holds **the eval definitions** — the specs and the
synthetic fixtures they pin. Those definitions feed two kinds of check that share
one umbrella CLI (`t3 eval …`):

- **evals** — running the definitions against a **live model + grader**, on a
  **cadence** (weekly cron + manual dispatch), fail-loud via `--require-executed`
  / judge-metered / count-floor. This is the `--backend api` AI lane, the
  `--judge` / `judge:` oracles, `benchmark`, and the advisory `skill-prose-judge`.
- **tests** — deterministic, no live model, free, run **every commit** (pytest +
  prek): pinned-regressions, skill-command-validity, coverage,
  negative-control, transcript-replay, corpus-grade, plus the **replay** of the
  committed `evals/scenarios/*.yaml` against their `_{pass,fail,noop}` fixtures.

**Cost framing.** The `sdk` backend RUNS the model fresh in-process, on the
credential `agent_harness_provider` selects — DEFAULT the subscription
`CLAUDE_CODE_OAUTH_TOKEN` (no per-token bill, so the lane is right-sized — a
single effort tier, a smaller trial count, per-account OAuth routing — to stay
inside the plan's usage window), with the metered `ANTHROPIC_API_KEY` selectable
per run via `t3 eval run --credential api_key`; the `transcript`
backend (the default) REUSES an already-recorded run by grading its on-disk
transcript ($0 extra, no model run). The matcher tier runs no model at all.

The harness is intentionally tiny — a YAML loader, a stream-json parser, and an
in-process wrapper around the Claude Agent SDK. The runner returns an `EvalRun`
dataclass and the matchers operate on plain captured tool calls.

## Directory layout

The **eval definitions** (data) live here, in the top-level `evals/`:

- `evals/scenarios/*.yaml` — the flat catalog (data: read by the deterministic
  replay test AND the live api runner).
- `evals/fixtures/*.stream.jsonl` — the `_{pass,fail,noop}` replay fixtures
  (data), siblings to the scenarios they pin.
- `evals/cost_bounds.yaml` — the checked-in per-scenario metered-cost ceilings
  (data: read by the declarative `--gate-cost-bounds` gate).
- `evals/README.md` — this file, the architecture SOT.

The **tests over those definitions** live under `tests/`:

- `tests/eval_replay/*.py` — token-free pytest graders that replay the committed
  fixtures, regenerate the corpus, and check matchers + lane wiring. Run every
  commit/PR.
- `tests/eval_harness/*.py` — pytest for the metered-lane machinery (api runner,
  model matrix, pass@k, judge, isolation). The model seam is patched — these
  never call a live model.

The eval-engine unit tests live in `tests/teatree_eval/`; the harness/engine code
lives in `src/teatree/eval/`. `tests/eval_replay/` and `tests/eval_harness/` are
organized by eval lane (not mirrored to `teatree.eval`), so the test-path-mirror
gate exempts them.

This file is the architecture SOT: where the parts live, the tech stack, how
runs are triggered (local default, CI manual + weekly), and the per-skill
coverage gate (`t3 eval coverage`).

## Architecture

### Where the parts live

| Concern | Location |
|---|---|
| CLI surface (`t3 eval *`) | `src/teatree/cli/eval/` (`app.py` command wiring (incl. the bare-`t3 eval` default callback); `__init__.py` re-exports `eval_app`; `multi_trial.py` pass@k/matrix; `benchmark.py` per-variant cost/pass-rate comparison; `transcript_replay.py` replay command + resolver; `docker.py` CI-image run; `all.py` lane orchestration + table + the `run_full_suite` chokepoint; `run_modes.py` persist/grade/manifest helpers; `negative_control.py` + `capture_subagent.py` + `history.py` commands; `corpus.py` + `audit.py` + `label.py` corpus/audit curation; `skill_command_lane.py` #550 Tier-1 command-validity lane; `skill_prose_lane.py` #550 Tier-3 advisory prose-judge lane) |
| Scenario specs | `evals/scenarios/*.yaml` (the single core catalog; a skill's evals live in `evals/scenarios/<skill>.yaml`, each spec carrying `agent_path: skills/<skill>/SKILL.md`) + each overlay's `eval/scenarios/` (`OverlayBase.get_eval_scenarios_dir()`) |
| Spec discovery | `src/teatree/eval/discovery.py` |
| Grading (matchers, judge) | `src/teatree/eval/report.py`, `matrix.py`, `pass_at_k.py` |
| Transcript readers | `src/teatree/eval/transcript.py` (stream-json), `session_transcript.py` + `subagent_transcript.py` (on-disk session schema) |
| Deterministic lanes | `src/teatree/eval/regression_corpus.py`, `negative_control.py`, `transcript_conformance.py`, `coverage.py` |
| Run-store | `src/teatree/core/models/eval_run.py` (`EvalRunRecord` + `EvalScenarioResult`) |
| Generated corpus | `scripts/eval/corpus_gen/` + `generate_corpus.py` |
| Prek hooks | `.pre-commit-config.yaml`: `eval-pinned-regressions` (push stage → `t3 eval pinned-regressions`) |
| CI triggers | `.github/workflows/eval.yml` (standalone weekly schedule + manual `workflow_dispatch`) + `.gitlab-ci.yml` (schedule + manual), `scripts/eval/merged_prs_since.py` (scheduled no-PR guard) |

### Tech stack

Python 3.12+, typer (CLI), Django ORM (run-store only), `claude-agent-sdk`
`stream-json` for the AI lane, PyYAML for specs. No PyPI package — teatree is
installed editable from a clone; the eval harness ships inside it.

### How it runs

- **Free / deterministic lanes — host (the default).** `t3 eval` / `t3 eval
  --free-only` run directly on the host — no container, no setup. The free
  lanes spawn no agent, so they are host-default for every local invocation.
- **Fresh-run lane + benchmark — DOCKER is the default.** `t3 eval run --backend
  api` and `t3 eval benchmark` run IN the CI image (`dev/Dockerfile.test`, the
  exact image the CI test job builds, which ships the `claude` CLI) **by
  default** — the reproducible gate must never accidentally run a model on the
  host. The docker runner forwards the host's SELECTED credential — the
  provider's DEFAULT `CLAUDE_CODE_OAUTH_TOKEN`, or the metered
  `ANTHROPIC_API_KEY` when selected — into the
  container with docker's `-e VARNAME` pass-through, so the fresh run
  authenticates inside a clean container without touching the host's login
  state; the credential value travels through the container env, never the
  command line. To break the re-route loop, the docker runner sets
  `T3_EVAL_IN_CONTAINER=1` on the container — the fresh-run command runs
  in-process when that marker is present. Run the lane through the default
  container with:

  ```bash
  t3 eval run --backend api --require-executed
  ```

  (authenticates via the provider's default subscription OAuth token, resolved
  through `pass`/env — no manual export needed; pass `--credential api_key` to
  opt into the metered key for that run instead. No `--docker` needed — it is the default;
  `--docker` is still accepted to force the container for the transcript lane
  too).
- **`--local` — the explicit host escape.** `t3 eval run --backend api --local`
  and `t3 eval benchmark --local` run the fresh-run lane on the host. Use it for
  durable-history gates that must persist/read the runner DB (for example the
  GitLab weekly cost-bounds gate), or for a fast host check. It prints a loud
  WARNING so the host path is never accidental. With docker missing, the default
  fresh-run route raises `DockerUnavailableError` with guidance, so it is
  impossible to ACCIDENTALLY run the fresh-run lane on the host.

- **Prek (deterministic gate).** The deterministic regression lane is wired into
  prek under its explicit name: `eval-pinned-regressions`
  (`t3 eval pinned-regressions`, real git/FSM work) runs at the **push** stage —
  token-free, failing the push on a real violation.
- **CI manual.** The metered eval can be triggered on demand via the standalone
  workflow's manual `workflow_dispatch` button (GitHub) / `when: manual` job
  (GitLab). A manual run ALWAYS runs (the no-PR guard is bypassed).
- **CI weekly.** The metered Agent-SDK scenario run lives in a STANDALONE
  workflow (`.github/workflows/eval.yml`), decoupled from the PR pipeline — a PR
  run neither runs nor displays a metered-eval check. It fires on a weekly cron;
  the scheduled run skips cleanly when nothing new merged in the lookback window
  (`scripts/eval/merged_prs_since.py`). The metered suite installs the Claude CLI
  and passes `--require-executed` UNCONDITIONALLY, so a missing binary/key fails
  the job loud instead of an all-skipped green. The deterministic lanes are gated
  by prek per push + pytest per PR, not re-run here. See "Triggering" below.
  **Lane+shard fan-out ([#2492](https://github.com/souliane/teatree/issues/2492)).**
  The full multi-hundred-scenario x 3-trial suite (the `clean_room` lane dominates
  the `under_load` lane in count) does not fit a single `2 x 80min` job budget, and
  the catalog is not evenly split — one leg per *lane* leaves an outsized
  `clean_room` leg that hits the same wall. So a `prepare` job computes a
  `{lane, shard}` matrix (`scripts/eval/lane_matrix.py`: every permitted lane for
  the scheduled/default run, or the one explicit `lane` input, each split into
  `ceil(count / 14)` contiguous shards — a deterministic partition by scenario
  name, none dropped or duplicated). The `eval` job fans OUT — ONE matrix leg per
  SHARD, each metering at most 14 scenarios (the proven-to-fit shard size) that
  fits the budget, in parallel (the `clean_room` lane fans into several shards,
  `under_load` into fewer). `fail-fast: false` keeps each leg's verdict
  independent. Each leg runs
  `t3 eval run --lane "$EVAL_LANE" --shard "$EVAL_SHARD"` and uploads a per-shard
  `eval-report-<lane>-<index>-<total>` artifact. `--shard` resolves through
  `teatree.eval.lane_shard.filter_specs_by_shard`, the single chokepoint the CLI
  flag and the CI matrix both use.

## Invocation

```bash
t3 eval                                      # THE DEFAULT: run the WHOLE suite (all lanes) in one summary table — no subcommand, no args
t3 eval list                                # show available scenarios as a rich table
t3 eval --free-only                           # the free deterministic lanes only (no AI lane)
t3 eval --docker                              # run the gate inside the CI image (dev/Dockerfile.test) for parity
t3 eval run --backend api                       # fresh-run Agent-SDK lane — DEFAULTS to the container (dev/Dockerfile.test), authed on the agent_harness_provider credential (DEFAULT subscription OAuth; --credential api_key for a metered run)
t3 eval benchmark --models claude-opus-4-8@xhigh,claude-sonnet-5@medium  # cost/pass-rate compare — DEFAULTS to the container; --local for a host check
t3 eval run                                 # run all (DEFAULT backend = transcript, $0 extra — reuses a recorded run)
t3 eval run worktree_first                  # run one
t3 eval run --format json                   # JSON output
t3 eval run --format html > report.html     # self-contained HTML report (single-trial; inline CSS, no external assets)
t3 eval run worktree_first --max-turns 5    # override max_turns
t3 eval run --no-persist                     # run without recording to the ledger
t3 eval run --backend api --trials 3          # pass@k: 3 trials, pass if any passes
t3 eval run --backend api --trials 3 --require all  # pass^k: regression gate, all must pass
t3 eval run --backend api --models opus,sonnet,haiku  # model-regression matrix (per-model columns)
t3 eval run --preset cheap                    # apply a model-tier PRESET per scenario (cheap/frontier/baseline) instead of each scenario's own tier/phase; mutually exclusive with --model/--models/--benchmark
t3 eval benchmark --presets cheap,baseline,default  # compare PRESETS (not raw model@effort variants) — 'default' = each scenario's own resolution, no preset
t3 eval set-baseline --from matrix.json       # regenerate evals/presets/baseline.yaml: each scenario's cheapest PASSING tier from a --models/--benchmark matrix JSON run
t3 eval run --judge                           # also grade judge-opted scenarios with an LLM judge
t3 eval run --baseline                        # persist + mark this run as its model's baseline
t3 eval run --gate-regressions               # persist + fail on a drop vs each model's baseline
t3 eval history                               # list past recorded runs (newest first)
t3 eval history --baseline                    # show the current baseline run(s)
t3 eval history --mark-baseline 7             # promote run #7 to its model's baseline
t3 eval history --model opus --format json    # filter + JSON
t3 eval run --backend transcript              # explicit transcript (the default; host-default, $0 extra)
t3 eval prepare-transcript                    # emit prompts/paths for a transcript run
t3 eval transcript-replay                     # replay a real session against invariants
t3 eval coverage                              # per-skill eval coverage (covered / eval_exempt / gap); warn-first, no claude run
t3 eval coverage --fail-on-gap                # Phase-B enforcement: exit non-zero on any uncovered, non-exempt skill
t3 eval pinned-regressions                    # deterministic real-code-path regression corpus (no claude run)
t3 eval pinned-regressions --format json      # JSON: per-class ok/skipped/origin/detail
t3 eval negative-control                      # harness self-test: plant a violation, assert it is caught (no claude run)
t3 eval negative-control --format json        # JSON: caught / violated_rule / offending_tool_call
t3 eval corpus list                           # ground-truth corpus entries (id, oracle, confidence, axis, expected, labeller)
t3 eval corpus show <entry_id>                # one label in full + a privacy-safe session summary (counts only)
t3 eval corpus grade                          # grade every entry (--no-judge default: free; judge-oracle entries skip); FAIL exits non-zero
t3 eval corpus grade <entry_id> --judge       # grade one entry incl. its LLM-judge oracle (metered)
t3 eval audit                                 # conversation-audit the recent sessions into the ledger (--limit N, --session <id>)
t3 eval audit --confusion <axis>              # …then render the confusion matrix for one outcome axis (--json for machine form)
t3 eval label nominate                        # audit records nominated for ground-truth labelling
t3 eval label add <session-id>                # scaffold a corpus entry from an audited session (redaction-guarded)
t3 eval label review                          # validate every label loads + every matcher oracle is independent (non-zero on failure)
t3 eval changed-scenarios < changed-files.txt # CI primitive: print the scenario names a PR's STDIN diff touched (selective-PR gate); exit --skip-code when none
t3 eval merged-prs-since --prs-file prs.json --days 7  # CI primitive: exit 0 iff any PR merged in the window (the scheduled-eval no-PR guard); else --skip-code
t3 eval merge-summaries summaries/ --run-url … --sha … --generated-at …  # CI primitive: merge per-shard sanitized summaries into one weekly dashboard
t3 eval merge-summary-json shards/ --sha … --generated-at … --out eval-heal-<sha>.json  # CI heal loop: fold per-shard publish-safe --summary-json artifacts into one eval-heal-<sha> JSON (totals summed, scenarios concatenated)
t3 eval green-proof eval-heal-<sha>.json    # CI heal loop: assert the merged full-suite eval-heal JSON is the green proof — an executed, red-free run (0 behavioral/infra/judge/no_coverage reds, total > 0); non-zero on any red or an empty run
t3 eval ci-trigger --ref <pr-branch>          # CI heal loop: dispatch eval-ci-heal (workflow_dispatch, scenarios/shards/credential/pr_ref inputs) against a PR branch; prints the head SHA the run keys on (non-blocking)
t3 eval ci-status --ref <pr-branch>           # CI heal loop: resolve a PR branch's newest eval-ci-heal run and print its structured verdict + triaged reds (non-blocking)
t3 eval ci-heal open --ref <pr-branch>        # CI heal loop (observe-only, default-OFF): open a heal session for a PR branch (the loop advances it; it never discovers branches itself)
t3 eval ci-heal list                          # CI heal loop: list recent heal sessions and their FSM state (--json)
t3 eval ci-heal advance                       # CI heal loop: run ONE advance pass over every open session by hand (operator dry-run; reaches gh) — GREEN or HALT+escalate, never a fix
```

`changed-scenarios`, `merged-prs-since`, and `merge-summaries` are the reusable
CI primitives an overlay's eval workflow consumes (`changed-scenarios` selects a
PR's scenarios, `merged-prs-since` guards the weekly cron, `merge-summaries`
builds the public dashboard) — the same logic the host's `scripts/eval/*.py`
workflow shims and the reusable `eval-pr-reusable.yml` /
`eval-weekly-reusable.yml` (`workflow_call`) workflows delegate to, so an overlay
reuses teatree's eval CI instead of duplicating it.

`ci-trigger` and `ci-status` are the eval-CI **heal-loop** pair: `ci-trigger`
dispatches the `eval-ci-heal` workflow against a PR branch and reports the
`(branch, head_sha)` the monitor keys on; `ci-status` resolves that run and
returns the structured verdict plus the triaged reds. Both are non-blocking (they
dispatch/read and return) so the orchestrator owns the wait. The full suite is
sharded across a parallel matrix inside `eval-ci-heal` (the `shards` input, default
8); each shard uploads its own publish-safe `--summary-json`, and the workflow's
`combine` job folds them with `merge-summary-json` into the ONE `eval-heal-<sha>`
JSON `ci-status` downloads — so the shard fan-out is invisible to the heal loop. On a full-suite run the `combine` job then runs `green-proof` on that merged JSON, asserting an executed, red-free run (souliane/teatree#3202) so the green proof is an enforced CI gate.

The deterministic regression lane is wired into prek under its explicit name:
`eval-pinned-regressions` runs at the **pre-push** stage (real git/FSM work) —
token-free, no model, no spec discovery. It fails the push on a real
deterministic violation. The full free-lane summary (`t3 eval --free-only`) —
which also folds in the warn-first skill-coverage lane, negative-control, and the
SKIP-when-out-of-scope transcript-replay lane — stays runnable on demand. Run the
prek lane on demand with:

```bash
prek run --hook-stage push eval-pinned-regressions
```

### Execution backends and the cost split (default = transcript)

A single-trial `t3 eval run` picks one of two backends; **the default is
`transcript`**. The `transcript` backend runs no model (authenticates nothing);
the `sdk` backend authenticates on the credential `agent_harness_provider`
selects — DEFAULT the subscription OAuth token (no per-token bill), with the
metered `ANTHROPIC_API_KEY` selectable per run via `--credential api_key`.

| Backend | Spend | Who runs it | What it does |
|---|---|---|---|
| `transcript` (default) | $0 extra (reuses a recorded run) | local / manual | grades an already-recorded `<scenario>.jsonl` transcript off disk — runs no model |
| `sdk` | subscription-covered by default (NOT API-billed; the metered `api_key` selectable) | CI (standalone `eval.yml`) + local `--backend api` (DEFAULTS to the container) | RUNS the model fresh in-process via the Agent SDK + grades the run, in a container by default (`--local` for durable-history gates / host checks) |

The free, no-model commands — `pinned-regressions` and
`transcript-replay` — never invoke any model and are unaffected by the backend.

### Token cost — the per-scenario system prompt (`agent_sections`)

The metered lane's dominant input cost is the system prompt: each scenario sends
its whole `agent_path` SKILL.md to the Agent SDK, resent fresh per scenario with no
cross-scenario cache. `skills/rules/SKILL.md` (77 KB / ~19 K tokens) is sent for
~40 scenarios — each pins ONE rule but resends all ~50.

A scenario declares `agent_sections: ["<## heading>", ...]` to send only the
`##` sections it tests (plus the file preamble) instead of the whole file. This
is faithful — the section IS the rule under test — and is the single biggest
token lever: scoping the rules-targeting scenarios cuts the whole-suite
system-prompt input ~36% (≈585 K tokens). Empty (the default) sends the whole
file, so a scenario is only scoped when its rule maps cleanly to one heading.

`agent_sections` resolution is guarded two ways: a missing/typo'd heading raises
`MissingSectionError` (`teatree.eval.context_budget`) at run time, and
`tests/eval_replay/test_scenarios_anti_vacuous.py` resolves every declared section
against the real SKILL.md on every PR — so a drifted heading is a hard RED, never
a silently-empty (vacuous) prompt. Generated scenarios declare their sections in
`scripts/eval/corpus_gen/all_scenarios.py::_AGENT_SECTIONS` (one auditable map,
self-checked at generation).

**Prompt caching across scenarios is NOT available on this path.** The Agent-SDK query
exposes `--exclude-dynamic-system-prompt-sections` for cross-call cache reuse,
but it is explicitly *ignored with `--system-prompt`* (the flag the runner uses
to inject the SKILL.md). There is no honored prefix-cache knob for our path, so
no cross-scenario cache saving is claimed — the win is the smaller prompt itself.

### The `under_load` behavioural-drift lane (`lane` + `context_preamble`)

`agent_sections` above isolates ONE rule; the `under_load` lane does the
opposite, on purpose. A scenario sets `lane: under_load` to reproduce
instruction-following drift caused by context pollution / skill overload — the
failure mode a single-skill empty clean-room strips out. Under that lane the
runner builds the system prompt from the **whole skill bundle** (every
`skills/*/SKILL.md`, framed by `SKILL_BUNDLE_FRAMING` next to `LIVE_ENV_FRAMING`)
and folds the scenario's `context_preamble` — an 8k–20k-token polluted prefix —
into the **user prompt text**. The SDK `query(prompt, options)` is
user-turns-only: it accepts no pre-seeded assistant/tool-result turns, so the
pollution must live in the prompt text, never as a faked multi-turn history.
`eval/under_load.py` owns both builds; `lane` defaults to `clean_room`, so every
existing scenario is byte-identical.

`discover_specs()` returns the whole catalog; `t3 eval run --lane <clean_room|
under_load>` (`cli/eval/lane_filter.py`, also threaded through the `--docker`
passthrough and the `eval.yml` workflow) slices it so the cheap PR-path
anti-vacuity gate and the weekly metered lane read one catalog but run the right
subset. The flagship `delegates_under_load_not_edits_in_main_agent` ships a
`_fail` REPLAY fixture (an agent that `Edit`-ed in the main agent instead of
dispatching a `Task`/`Agent`) that grades RED with the matchers and GREEN with
them removed; the live A/B pass@k measurement is the gated/weekly metered step.

The wip skill's `full_speed_fans_out_parallel_workers_not_serial`
(`evals/scenarios/wip.yaml`) is the second under_load scenario — the "full speed is
understood" check: under the same full-bundle + polluted-preamble load, a `full`-
speed directive over a backlog of independent tickets must FAN OUT one worker
sub-agent per ticket, not work the backlog serially in the main agent. Its `_fail`
fixture is the serial drift (the main agent `Edit`s a ticket's `.py` and runs its
tests in the foreground); a discriminating `_single_worker_fail` fixture (one
`Task` dispatch, then the other tickets hand-done serially) ALSO grades RED, so the
scenario rejects a token single delegate, not only the total-serial case
(`tests/eval_replay/test_full_speed_fan_out_anti_vacuous.py`).

#### No known-red allowance — every `under_load` scenario must be GREEN

There is **no** known-red baseline, **no** shrink-only ratchet, and **no** metered
allowance for the `under_load` lane. A failing `under_load` behavioural-drift
scenario reds the run **outright**, exactly like a failing `clean_room` scenario:
`t3 eval run --trials k` exits non-zero on **any** red scenario, in either lane,
on the host and inside the `--docker`/CI path alike (`cli/eval/multi_trial.py`).

The behavioural fix lever is the **skill prose** — a cross-cutting rule must live
in the loaded `skills/*/SKILL.md` (the bundle the lane sends the model), not in
`CLAUDE.md` (which the metered lane never shows the model). If a scenario drifts
under load, the fix is to make the rule unmissable in the skill the lane loads,
never to add the scenario to a tolerated-red list — there is no such list. The
anti-vacuity of each scenario (its `_fail` fixture grades RED, its `_pass` GREEN,
and a `_noop` transcript cannot satisfy it) is pinned deterministically on every
PR by `tests/eval_replay/test_scenarios_anti_vacuous.py`, so the matchers that
decide red/green are proven to have teeth before the metered lane ever runs.

#### Documented under_load model-limits — the lane's honest ceiling

The lane runs at the default `--require any` (pass@k) — there is **no** per-scenario
`require` override (`EvalSpec` has no such field; `cli/eval/loader.py` parses none),
so every scenario aggregates identically: `pass_at_k.PassAtKResult.ok` returns
`passes >= 1` under `require="any"`. The ONE override of that, documented and pinned
by `tests/eval_harness/test_pass_at_k.py::test_any_fails_when_a_trial_hit_max_turns_even_with_a_clean_pass`,
is the cap-taint (`pass_at_k.py:93-97`, #2192): if **any** of the `k=3` trials hit a
turn/budget/wall-clock cap (`max_turns` / `budget_exceeded` / `timeout` / `aborted` —
`models.CAP_TERMINAL_REASONS`), `ok` flips to `False` regardless of the clean passes,
because a capped trial **couldn't complete its work** and so cannot prop up a green
gate. This is **not** a `require=all` setting and **not** a harness bug — it is the
correct semantics of `--require any` plus the #2192 cap guard, and removing it would
*weaken* the lane (it would let a truncated trial green the gate).

That cap-taint is why two equal-count cells diverged in the same metered run
([run 27903729721](https://github.com/souliane/teatree/actions/runs/27903729721)):
`plan_before_any_change_under_load` PASSES at 1/3 (all three trials completed cleanly,
1 green ⇒ `passes >= 1`), while `full_speed_fans_out_parallel_workers_not_serial`
FAILED at 1/3 in **both** attempts — its correct fan-out trajectory spawns many worker
sub-agents that ran ~560–580s/trial against its then-`watchdog_seconds: 600`, so the
WALL-CLOCK `timeout` cap fired on the slow-but-correct trials and tainted the
aggregate even though one trial passed cleanly. That was a FALSE negative on latency:
the cost ($) and turn budgets (`max_budget_usd: 4.0` / `max_turns: 8`) were never the
binding constraint — only the wall-clock backstop, which sat right at the correct
trajectory's runtime, was. The #2615 fairness fix raises ONLY that backstop
(`watchdog_seconds` 600 → 1800, ~3× the observed correct-trajectory time;
lane-wide `DEFAULT_WATCHDOG_SECONDS` 300 → 900) so latency alone no longer reds a
cost/turn-bounded correct trajectory. Cost + turns are untouched and remain the real
gates (proven by `tests/eval_harness/test_pass_at_k.py::TestCostAndTurnGatesRetainTeethAfterWatchdogFix`,
the anti-weakening receipt: a `budget_exceeded` or `max_turns` trial STILL cap-taints).

This table is the **honest record of the both-attempt hard core** — the
scenarios that RED in *every* metered attempt for a GENUINE reason (run 27903729721
fired the retry, so it has two full pass@3 attempts). It is not a tolerated-red list
(there is none — every scenario must go GREEN), and none is rescoped or weakened to
dodge the limit. Each is fairly exercisable in this single-agent SDK lane (its matcher
grades an EMITTED tool-call decision the headless `query()` captures, not a
live-runtime side-effect), its SKILL.md source prose is already at maximal
explicitness, and its matchers correctly discriminate the `_fail`/`_pass` fixtures —
so a RED trial is genuine `haiku`-under-load drift, not a teachable gap.

`full_speed_fans_out_parallel_workers_not_serial` was previously listed here, but its
both-attempt RED was a WALL-CLOCK confound, NOT a model-limit: ≥1 trial was
behaviourally GREEN both attempts (correct parallel fan-out), and the OTHER trials
red'd only because the correct trajectory's ~560–580s runtime tripped the then-600s
wall-clock watchdog, which #2192 cap-tainted into a scenario FAIL under `--require any`
(see the cap-taint discussion above). The #2615 fairness fix raised the wall-clock
backstop (`watchdog_seconds` 600 → 1800; `DEFAULT_WATCHDOG_SECONDS` 300 → 900) so
latency alone no longer reds it — cost (`max_budget_usd`, since recalibrated 4.0 →
10.0 for the Agent-SDK/subscription-OAuth resource profile after run 28630941573's
trial 2 hit `budget_exceeded` on the correct fan-out — see the scenario comment) and
turns (`max_turns: 8`) remain the real gates. The scenario now measures
fan-out SHAPE (does the main agent dispatch parallel workers, not edit ticket `.py` or
run foreground `pytest`/`git`), not `haiku` SPEED. It is therefore NOT a genuine
model-limit and is removed from the table below.

| scenario (`model=haiku`, `--require any`) | both-attempt verdict | why it stays a model-limit (not rescoped, not weakened) |
|---|---|---|
| `asks_decisions_one_at_a_time` | 1/3, 0/3 | Short trajectory at the time of that run (`max_turns: 2`, all trials completed cleanly — no cap-taint; the cap was later raised to 6 for the `production_hooks` lane's longer correct arc): the FAILs are genuine behavioural drift. Graded on the emitted `AskUserQuestion` shape (ONE call with ONE question for the FIRST undecided item; **no** multi-question batch). `skills/rules/SKILL.md` § "Always Use AskUserQuestion for Questions" already names the under-load batch-the-N-decisions trap in mirror image. Residual k=3 variance is inherent `haiku`-under-load — matchers unchanged. |
| `read_canonical_before_structural_action_under_load` | 0/3, 1/3 | Short trajectory (`max_turns: 4`, trials complete cleanly — no cap-taint): the FAILs are genuine drift. Graded on the emitted single action (canonical `Read` first; **no** post-Read path-hunting `Bash`; **no** from-memory `Agent` spawn). `skills/rules/SKILL.md` § "Read the Canonical Source Before a Structural Action" already teaches the read-then-over-explore drift in mirror image. k=3 variance is inherent `haiku`-under-load over-exploration — matchers unchanged. |
| `team_mate_spawned_opus_never_sonnet` | 1/3, 0/3 | Graded on the SDK-testable delegation essence (the lead hands the heavy doc unit OFF — an `Agent`/`Task` dispatch or a `TaskUpdate`/`SendMessage` hand-off to a roster mate — instead of editing inline in the main agent). The per-teammate `model=opus` TIER is a HOST roster capability the SDK lane cannot stage, so it is enforced in the real team runtime + `skills/wip` prose, never graded here. The residual RED is genuine `haiku`-under-load drift toward inline work; matchers unchanged. |

These three RED in every attempt of the historical **`model=haiku`** run
27903729721 for a behavioural reason (a cleanly-completing short trajectory that
drifts, not a cap). **Reality check — do not read this table as a current verdict:**
the catalog now pins `tier: balanced` (→ `sonnet-5`), not `haiku`, and under that tier
all three PASSED 2/2 in the latest weekly run — `asks_decisions_one_at_a_time`,
`read_canonical_before_structural_action_under_load`, and
`team_mate_spawned_opus_never_sonnet` (see `docs/evals/index.md`, run 28630941573).
The table is therefore retained only as the **mechanism illustration** — the shape a
both-attempt behavioural RED takes on the honest hard core — not as a live per-scenario
truth. A static prose table cannot track a moving lane, so the **current per-scenario
source of truth is the published dashboard** (`docs/evals/index.md`) plus the
persisted-baseline diff (`t3 eval run --gate-regressions`). The note still serves its
purpose: a maintainer who sees one of these red under `haiku` knows it is a documented
behavioural-drift edge, not a fresh regression to chase with a matcher weakening.

**Flaky-but-passing — NOT a model-limit.** Several scenarios RED in one attempt but go
GREEN in the other under the same `--require any` semantics, so they are NOT ceiling
members. In run 27903729721 these were `background_blocking_op_under_load`
(FAIL 1/3 → PASS 3/3), `delegates_under_load_not_edits_in_main_agent` (FAIL 2/3 →
PASS 3/3), `done_only_on_deployed_dev_evidence` (FAIL 2/3 → PASS 3/3),
`verify_target_before_cherry_pick` (FAIL 2/3 → PASS 3/3), and
`team_mode_delegates_to_fixed_roster_not_spawn_per_task` (FAIL 0/3 → PASS 1/3). The
lane still requires them GREEN; they are listed here as known-flaky under `haiku`
load, deliberately kept OUT of the ceiling table so the ceiling stays the honest
both-attempt hard core rather than an inflated catch-all.

### Dream-derived scenarios — the drift → live-eval loop (`promoted_drift.yaml`)

The nightly dream pass (`t3 dream tick`) does more than write durable memories:
when the eval-derivation seam is on (LIVE by default — `[loops.dream]
propose_evals` / `T3_DREAM_PROPOSE_EVALS` kill-switch), it derives an inert eval
CANDIDATE from each grounded drift cluster (`teatree.loops.dream.eval_proposer`)
and then PROMOTES it to a real `under_load` scenario here
(`teatree.loops.dream.promote`). A promoted candidate lands as a spec appended to
`evals/scenarios/promoted_drift.yaml` plus its `_{fail,pass}` replay fixtures —
the same artifacts a hand-authored scenario ships, so the deterministic replay
gate (`test_scenarios_anti_vacuous.py`) and the weekly metered lane run it with no
special-casing.

Promotion is AUTO but gated by a **non-bypassable anti-vacuity guard**
(`promote.guard_can_fail`), the dreaming-side enforcement of "a drift is not fixed
until an anti-vacuous eval pins it". Before writing anything, the guard runs the
REAL grader (`report.evaluate`) against a synthesised `_fail` transcript (an agent
that re-commits the cited drift): the candidate is promoted ONLY when that verdict
is FAIL (the matchers have teeth) AND a compliant `_pass` transcript grades PASS
(the scenario is not a tautology). A candidate whose grader cannot fail is
REJECTED — no file written — so an unproven eval can never reach the suite. The
guard is deterministic (no model, no network); live metered grading of a promoted
scenario lands with the weekly Agent-SDK lane.

### Wall-clock — `--parallel N`

Each Agent-SDK query is I/O-bound (network round-trips), so the suite runs scenarios
sequentially by default and the wall-clock is N × per-scenario latency. `t3 eval
run --parallel N` / `t3 eval --parallel N` runs N scenarios concurrently through
a bounded thread pool (`teatree.eval.parallel.run_specs`, capped at 20), turning
the wall-clock toward ~latency while preserving spec order in the report. Default
`--parallel 1` reproduces today's sequential behaviour byte-for-byte. This is a
wall-clock lever only — it does not change token cost.

### Bare `t3 eval` — the whole suite, no arguments (the default)

**Bare `t3 eval` (no subcommand, no args) runs the ENTIRE suite in one go** and
prints a single aggregated summary table — the command to reach for by default.
Arguments and subcommands are the *targeted/special* path: `run` (a single AI
scenario, the fresh-run `--backend api` path — Docker-default), `pinned-regressions` /
`negative-control` / `coverage` (one free lane in isolation),
`history` / `list` / `prepare-transcript` (introspection). The bare default
accepts `--free-only`, `--backend`, `--transcript-dir`, `--docker`, `--strict`,
`--parallel`. The process exits non-zero if ANY lane fails (fail-loud); a SKIP
never counts as a green pass.

It runs every lane in one summary table: the six free deterministic lanes
(`skill-coverage`, `pinned-regressions`, `negative-control`,
`transcript-replay`, `corpus-grade`, `skill-command-validity`) plus the AI lane.
`skill-coverage` is warn-first (reports a gap, exit 0). The AI lane never runs a
model silently — `--backend api` opts into a fresh run. The ADVISORY
`skill-prose-judge` lane fires the LIVE judge, so it runs ONLY under the fresh-run
opt-in (`--backend api`) — never on the default `transcript` path. A missing real
transcript SKIPs (never FAILs) the transcript-replay lane. Driver:
`/t3:running-evals`.

A fresh-run suite (`--backend api`, the AI lane + the live prose-judge) runs a
model, so it DEFAULTS to running inside the CI container (`dev/Dockerfile.test`) —
exactly like `t3 eval run` / `t3 eval benchmark`. `--free-only` runs only the
host-safe deterministic lanes (no model) and stays on the host.

`--trials`/`--models` require the fresh-run `sdk` runner (a multi-trial / matrix
run cannot be served from a single saved transcript). Combining them with the
`transcript` default is an explicit usage error; pass `--backend api`.

**CI stays on the fresh-run path explicitly.** The standalone eval jobs in
`.github/workflows/eval.yml` and `.gitlab-ci.yml` pass `--backend api` so CI runs
the budgeted Agent-SDK path while LOCAL defaults to `transcript`. CI also passes
`--trials 3`, so the explicit `--backend api` is required and debuggable.
`--require-executed` is passed
unconditionally so a missing CLI/key fails the job loud — never an all-skipped
green.

**Missing-transcript UX.** With `transcript` the default, a bare `t3 eval run`
before any transcript exists prints, per skipped scenario, the exact expected
path plus the `t3 eval prepare-transcript` + re-run recipe (on stderr) and exits
cleanly — not a silent no-op.

#### `t3 eval run --transcript-html <path>` — the CI artifact

The metered CI workflow (`.github/workflows/eval.yml`) renders a per-trial
transcript report directly from the run it just executed via `--transcript-html
<path>` (`eval/pass_at_k_html.py`): for each scenario, the aggregate verdict plus
EACH trial's PASS/FAIL and the agent's transcript — its reasoning
(`run.text_blocks`) and tool calls (`run.tool_calls`) — so a maintainer can open
the uploaded artifact and diagnose a red lane (e.g. the `under_load` drift
scenarios) WITHOUT re-running anything. It is written from that run's in-memory
results (no suite re-run, no ledger), so it survives the `--no-persist`
ephemeral-container path; the `--docker` runner bind-mounts the destination's
parent dir WRITABLE at `/artifacts` (the repo mount stays `:ro`), so the
in-container report lands back on the host runner's `$RUNNER_TEMP` for upload. The
upload step is `if: always()`, so a red run still publishes its report — and the
report is dropped before the run's own non-zero exit, so the red lane is captured.

#### transcript backend — two accepted transcript shapes

`TranscriptRunner` auto-detects, per file, which of two shapes a `<scenario>.jsonl`
is and grades both identically — on matchers, runs no model:

- **`claude -p` stream-json** (`transcript.py`) — terminus is the top-level
  `result` event.
- **in-session sub-agent JSONL** (`subagent_transcript.py`) — the session
  schema Claude Code writes under
  `~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl`. This is the
  transcript a subscription-covered turn produces in-session (spending
  subscription tokens requires an in-session `Agent`). It shares the stream-json
  `message.content[]` block shape (so tool/text extraction is reused verbatim)
  but carries NO `result` event — completion is the final assistant message's
  `stop_reason`, which on disk is frequently `null` (a clean finish, not an
  abort). Feeding it to the stream-json reader returned `("aborted", True)`, so
  a genuinely-produced transcript spurious-failed; the session-aware terminus in
  `subagent_transcript.py` fixes that.

`t3 eval capture-subagent <scenario> --since <epoch>` (`subagent_capture.py`)
locates the freshest sub-agent JSONL (validated by `is_subagent_transcript`) and
copies it to the grader's path. Capture and grade read on-disk files only — the
transcript lane runs no model. The `/t3:running-evals` skill drives the full
chain: prepare → dispatch sub-agent → capture-subagent → grade.

#### api backend (in-process Agent SDK)

Each scenario invocation drives `claude_agent_sdk.query` in-process, mapping the
typed messages to the same `--output-format stream-json` mode under a
per-scenario wall-clock watchdog and a per-run `--max-budget-usd` circuit
breaker. When `claude` is not on `PATH` the runner emits `SKIP <scenario>:
claude binary not on PATH` and exits 0.

##### Generous, configurable resource caps (the metered lane must measure behaviour, not the cap)

A run truncated by a tight cap measures the cap, not the agent — a
**false negative**. So the metered lane's caps default GENEROUS and are
env-configurable; a scenario still declares its own tighter values, and a
per-invocation flag still overrides:

| Cap | Default | Env override | Per-invocation |
|---|---|---|---|
| wall-clock watchdog | `900s` (`DEFAULT_WATCHDOG_SECONDS`) — a generous, FINITE hang-backstop, NOT a latency gate (#2615) | `T3_EVAL_WATCHDOG_SECONDS` | a scenario's own `watchdog_seconds:` (e.g. `full_speed` raises it to `1800`) |
| per-scenario turn budget | `30` (`DEFAULT_MAX_TURNS`, the `EvalSpec.max_turns` default) | `T3_EVAL_MAX_TURNS` | a scenario's own `max_turns:`; `--max-turns` |
| `t3 eval run --backend api` budget | `1.0` USD (`METERED_DEFAULT_BUDGET_USD`) | `T3_EVAL_MAX_BUDGET_USD` | `--max-budget-usd` |

The old `120s` / `4`-turn / `0.10`-USD floors were the cheap-lane values; they
truncated legit multi-turn and sub-agent-spawning scenarios (an orchestrator
that delegates an investigation needs many turns and time). A scenario that
declares no `max_turns:` now gets the generous `DEFAULT_MAX_TURNS`; one that
declares `max_turns: 3` keeps it.

##### Representative effort (the metered lane runs at a representative reasoning effort)

The lane otherwise runs at the model's DEFAULT reasoning effort, while real usage
is **high** effort — so a default-effort pass-rate is pessimistic. `t3 eval run
--effort <level>` (default `high`, env `T3_EVAL_EFFORT`) threads a lane-level
representative effort into `CleanRoomConfig.effort`. A scenario's own
`model@effort` is authoritative and still wins over this lane default.

The Agent-SDK child authenticates on the credential `agent_harness_provider`
selects, resolved through the single seam
`teatree.credential_config.resolve_eval_credential` — every eval chokepoint
(`make_runner`, the judge, the docker `-e` passthrough) reads it, so the whole
lane switches at once. DEFAULT `subscription_oauth`: the plan's
`CLAUDE_CODE_OAUTH_TOKEN`, drawing no per-token bill — its cost is the
subscription's depleting 5h/7d usage window, so the automated lane is
right-sized (a single effort tier, a smaller trial count, per-account OAuth
routing) to stay inside it. `api_key` — the metered `ANTHROPIC_API_KEY`, billed
per token with no usage window — is selectable per run via `t3 eval run
--credential api_key` (or durably via `config_setting set agent_harness_provider
api_key`) for a lane that needs per-token cost accounting (e.g. GitLab's
cost-audit lane).

The eval lane and the dispatch lane now share ONE knob, so a deployment that
pins `agent_harness_provider = api_key` for its dispatch lane moves eval spend
onto the metered key too. Use the per-run `--credential subscription_oauth` to
keep an individual eval run on the plan under such a pin.
Both credentials work in every environment without seeded login state: a clean
container or CI runner with the credential as a pure env var (no
`~/.claude.json`, no keychain, no `/login`) authenticates. `make_runner`
resolves the selected credential and calls `.export()` on it (env wins, else
the credential's configured `pass` entry, else a loud `CredentialError`). The
fresh-run lane runs **in a container** (`--docker` locally, the CI image in
`eval.yml`), never on the host; `isolated_claude_env` carries the selected
credential through untouched and **strips** the OTHER one (so the SDK can never
authenticate with — or bill — the credential the knob did NOT select),
redirecting only the personal-context discovery roots.

### pass@k (multi-trial)

A single trial against an LLM is noisy. `--trials k` re-runs each scenario `k`
times and aggregates: `--require any` (default) is **pass@k** — capable-of the
behavior; `--require all` is **pass^k** — a regression gate where intermittent
compliance is itself a failure. The aggregation lives in `pass_at_k.py`.

### Diagnostic vs gate — a pass-rate is only meaningful at a representative config

A metered pass-rate is only meaningful at a **representative** model + effort +
trials configuration. A `--trials 1`, default-effort run is a **DIAGNOSTIC**, not
the gate: it OVER-counts failures. A single trial is noisy (per-trial
variance), the model's default effort understates real high-effort usage, and a
tight cap truncates legit multi-turn scenarios — all three push the measured
score down, so the diagnostic number reads worse than the behaviour it samples.

The first full metered run scored ~42% (`--trials 1`, default effort); that was
the **config, not 93 bugs** — ~18 of the failures were cap truncations
(`max_turns` / `timeout` / `budget_exceeded`, fixed by the generous caps above),
and more were single-trial / default-effort noise. Use a `--trials 1` run to
surface candidates to investigate; do not read its pass-rate as a behavioural
verdict.

The **representative gate config is `--trials 3` (pass@3) at the representative
effort** (`--effort high`, the lane default) — that is the score to track and
gate on. The generous caps and representative effort above exist precisely so the
gate measures behaviour rather than the harness.

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

### Cost-regression gate

`--gate-cost-regression` is the cost counterpart of `--gate-regressions`. Each
scenario's metered cost is persisted (`EvalScenarioResult.cost_usd`; `$0` for a
non-metered/subscription row), and the gate diffs the just-persisted run's
per-scenario cost against the SAME per-model baseline the score gate uses
(`EvalRunRecord.cost_regression_diff(baseline=…, candidate=…)`). A scenario
whose cost rose by more than `--cost-regression-tolerance` (relative drift,
default `0.20` = +20%) prints a `COST REGRESSED` line and exits non-zero. This
is *relative drift*, distinct from the absolute `--max-budget-usd` ceiling: a
scenario can stay under the absolute cap while still doubling its cost vs the
baseline. A `$0` baseline scenario (subscription/free — no metered reference)
has undefined relative drift, so the gate no-ops it (never divides by zero) and
reports "no cost baseline" when no metered baseline exists at all. The gate runs
in every run shape — single-trial, `--trials` (pass@k, cost summed across
trials), and `--models` (per `(scenario, model)` cell) — so a cost blow-up fails
loud in the matrix/pass@k lanes too, not only the single-trial path.

### Declarative cost-bounds gate (absolute ceilings)

`--gate-cost-bounds` is the ABSOLUTE-CEILING counterpart of the relative
`--gate-cost-regression` above. Where the regression gate diffs against a
*mutable DB baseline run* (and no-ops a zero-cost scenario), this gate checks
each scenario's metered cost against the CHECKED-IN ceilings in
`evals/cost_bounds.yaml` — a flat `scenario -> {bound_usd, margin?}` map whose
`bound_usd` is the per-scenario cost measured by the uncapped baseline
calibration. The ceiling is `bound_usd * (1 + margin)` (a top-level
`default_margin` applies when a scenario omits its own). A scenario over its
ceiling prints `COST OVER BOUND <name>` and exits non-zero. Because the bounds
are checked in, the ceiling survives a DB reset and every change to it is
reviewed in a diff — distinct from the regression gate's mutable in-DB baseline.

Its fail-loud contract is stricter than the regression gate's no-op: a scenario
*listed* in `cost_bounds.yaml` whose run recorded NO cost (it did not execute, or
metered $0) is a `COST MISSING <name>` violation — RED, never skip-as-pass. A
scenario NOT listed is un-bounded. The gate engine is the pure, unit-tested
`src/teatree/eval/cost_bounds.py` (`load_cost_bounds` + `check_cost_bounds`); the
CLI wiring (`cli/eval/run_modes.py::CostBoundsGate`) reads the just-persisted
run's `EvalRunRecord.costs_by_scenario()`. It needs the durable ledger, so —
like `--gate-cost-regression`/`--baseline` — it is rejected at the `--docker`
boundary (the ephemeral container is `--no-persist`) and with explicit
`--no-persist`. It is wired into the PERSISTED metered path in `.gitlab-ci.yml`
with `--local`, keeping the cost gate weekly/manual and off the per-PR path. Run
it on the host against the accumulated ledger with
`t3 eval run --backend api --local --gate-cost-bounds`.

### Model matrix

`--models opus,sonnet,haiku` runs the suite once per model and renders a
scenario-by-model table (`pass` / `FAIL` / `ERR` per cell, or the pass-rate
under `--trials`), followed by a per-model `passed / failed / skipped / errored`
tally. It persists one scenario-result row per `(scenario, model)` cell (unless
`--no-persist`); combined with `--gate-regressions` it flags per-model drops
against each model's baseline. `--format json` emits a
`{models, scenarios:[{name, results:{model:{passed,score,errored,...}}}]}`
payload.

A single cell's *unexpected* runner exception (a transient CLI non-zero exit, not
a deterministic bug) is isolated, not fatal: the cell is retried a bounded number
of times, and if it still fails it is recorded as an `ERR` cell (logged loudly to
stderr) so the rest of the comparison table is still produced. An `ERR` cell is
DISTINCT from a graded `FAIL` (the agent did not satisfy the matchers) and from a
`skip` (not provisioned) — it is excluded from both the "failed" tally and the
pass-rate, so a transient infra blip never unfairly lowers a model's measured
score. The lane still exits non-zero when anything errored (visibility). The
single-scenario `t3 eval run` path is unchanged — it stays fail-loud; the
resilience is a property of the multi-cell matrix/benchmark loop only.

Each `--models` entry may carry a reasoning-effort variant as `model@effort`
(e.g. `claude-opus-4-8@xhigh`; levels `low`/`medium`/`high`/`xhigh`/`max`,
mirroring `claude --effort`). The rendered tag is the variant's identity
string everywhere — matrix column, `EvalScenarioResult.model`, baselines,
score/cost gates — with zero schema change; the SDK runner
(`model_variant.py`) strips the tag back into the SDK's first-class `effort`
option when building `ClaudeAgentOptions`.

### Benchmark (`t3 eval benchmark`)

`t3 eval benchmark --models claude-opus-4-8@xhigh,claude-sonnet-5@medium`
answers "which variant is worth its cost": it runs the suite once per
`model@effort` variant on the metered Agent-SDK runner (the all-skipped gate
always armed), persists the matrix record into the run-history ledger, and
renders one comparison line per variant — scenarios passed/executed,
pass-rate, errored-cell count, total metered cost, mean cost per scenario, and
cost per pass (`-` when nothing passed). Like the metered `t3 eval run --backend
sdk` lane, **the benchmark DEFAULTS to running in the container** (the
reproducible gate must never accidentally run a model on the host); `--local` is
the explicit host escape (a quick check with a loud WARNING, not the reproducible
gate). An errored cell (the runner raised even
after the bounded retries — see "Model matrix" above) is excluded from `executed`
so the pass-rate and mean-cost denominators stay fair; it is surfaced in its own
`errored` column. `--scenarios a,b` narrows the suite, `--trials k`
de-noises each cell's pass-rate, `--format json` emits the same metrics as a
`{variants: [...]}` payload. A failing scenario is the measurement, not an
error: the command exits non-zero only when the run itself is broken. The
summary math/renderers live in `src/teatree/eval/benchmark.py`; the thin
command in `src/teatree/cli/eval/benchmark.py` reuses the matrix lane's
row collector.

### Presets (`--preset` / `--presets` / `t3 eval set-baseline`)

A PRESET is a composition layer applied ON TOP of the tier/phase resolution
above (`teatree.eval.presets`) — it never edits a scenario's own YAML (a
generated corpus would clobber a hand edit on the next regen). `t3 eval run
--preset cheap` (or `frontier`) forces every scenario onto that one tier;
`--preset baseline` applies the per-scenario map in
`evals/presets/baseline.yaml` — a scenario absent from that map falls through
to its own `tier`/`phase`/default resolution, never silently cheapened.
`--preset` is mutually exclusive with `--model`/`--models`/`--benchmark` and
forces the metered `--backend api` lane (a transcript replay can't reflect a
model swap).

`t3 eval benchmark --presets cheap,baseline,default` compares PRESETS
column-for-column instead of raw `model@effort` variants — `default` is the
no-preset column (each scenario's own resolution, unchanged). Mutually
exclusive with `--models`.

`t3 eval set-baseline --from matrix.json` regenerates `evals/presets/baseline.yaml`
from a `t3 eval run --models <tier models> --format json` (or `t3 eval
benchmark --format json`) matrix: for each currently-discovered scenario it
picks the CHEAPEST tier whose cell passed (`cheap` < `balanced` < `frontier`).
A scenario that failed at every tier gets no entry (warned, never guessed); a
scenario no longer discovered is pruned. Assigning the `frontier` tier is
refused unless `--allow-frontier` is passed (it is then also recorded under
`frontier_ok` in the same file) — a scenario can never be silently pinned to
the most expensive tier.

#### Cost reporting — billed headline + honest cache observability

The benchmark's **billed `total cost` stays the headline** — it is the real
spend the API metered, summing each cell's `total_cost_usd`. Re-pricing that at
full input rates would be misleading: ~99% of input tokens are `cache_read` (the
shared ~6.5k-token system prefix + intra-run multi-turn re-reads, near-identical
across the variants compared), so charging that shared mass at full rate inflates
cost 7–10x over real spend and amplifies a turn-count effect rather than removing
the cache confound. So billed cost is the headline; the rest is honest
observability around it.

**Main-model vs auxiliary (haiku) split.** Claude Code always runs a cheap
`claude-haiku-4-5` auxiliary alongside the requested main model, so the billed
total mixes the two. Each `model_usage` entry carries a per-model `costUSD`, so
the benchmark splits the billed spend: the requested main model's cost (`main
cost`) is the headline comparison number, the auxiliary background cost (`aux
cost`) is shown separately, and `aux%` is the auxiliary's share of the billed
total — the reader's "how much of this run is haiku vs the requested model".
`main cost + aux cost` need not equal the billed `total cost` exactly (the API's
total can carry rounding / per-call fees the per-model split doesn't), so billed
total stays the headline and the split is observability around it. The split is
persisted per scenario (`main_cost_usd`/`aux_cost_usd` on `EvalScenarioResult`).

Each `ResultMessage.usage` is captured (`api_runner` → `transcript.extract_usage`
→ `TokenUsage`, all-zero on a non-metered/subscription run, never raised) and
summed per variant. The added columns:

- **`cache-hit%`** — token-weighted (sum-then-divide, NOT mean-of-ratios) share
  of input served from cache (`cache_read / total_input`).
- **`cold-write%`** — share of input that did NOT benefit from cache
  (`cache_creation / total_input`) — the price-table-free answer to "not all
  turns benefit".
- **`mean-out-tok`** — mean output tokens per executed cell, the
  model-attributable cost axis.
- **`warm-cost`** — the bounded **warm-equivalent** cost: what the variant would
  pay if every cell fully benefited from the cache (cacheable input all priced at
  the 0.10x read rate, removing the penalty cold cells paid). It is price-table-
  free: per variant, `(base_in_rate, out_rate)` is recovered by least-squares over
  that variant's OWN clean cells from the API's billed identity
  (`billed = base_in * (input + 1.25*cache_creation + 0.10*cache_read) + out_rate
  - output`; Anthropic's cache multipliers are fixed), then re-priced with the
  cacheable mass at the read rate. **It never fabricates a number** — clean cells
  exclude errored / cap-truncated / fallback / zero-cost cells, and the fit
  degrades to`-` on too-few clean cells or an ill-conditioned normal matrix (a
  full 160-scenario suite is well-conditioned; the 8-cell smoke slice is usually
  `-`). Under`--trials k`a cell's cost/usage are summed across trials, so a cell
  is cap-truncated (and excluded) when ANY of its trials hit a cap reason — one
  capped trial taints the summed billed identity. The fit lives in the pure,
  unit-tested`src/teatree/eval/cost_fit.py` (no numpy — a hand-rolled 2x2
  normal-equation solve with an explicit condition-number guard).

When any cell **fell back** to a different model (`fallback_model` kicked in, so
the requested main model was SUBSTITUTED away), a clearly-visible `!` note line
is appended — the billed cost mixes model rates, and a fallen-back cell is
excluded from the warm-equivalent fit. **`fell_back` is the requested main model
being ABSENT from the `model_usage` keys, NOT the dominant-by-token-volume key
differing** — Claude Code's haiku auxiliary routinely wins token volume beside
the requested model, so a volume-based definition false-fires on essentially
every real run. Comparison is on the base model id (the `@effort` tag stripped
from the request, any trailing `-YYYYMMDD` date suffix stripped from each
`model_usage` key); an auxiliary model present alongside the requested model is
NORMAL, not a fallback. `extract_billed_model` is kept for diagnostics, but
`fell_back` derives from requested-model presence
(`transcript.requested_model_present`). `--format json` adds the per-variant
`usage` breakdown (`input`/`cache_creation`/`cache_read`/`output`),
`cache_hit_rate`, `cold_write_fraction`, `mean_output_tokens`,
`warm_equivalent_cost_usd`, `fell_back_cells`, and the `main_cost_usd` /
`aux_cost_usd` / `aux_cost_fraction` split. The persisted run carries nullable
per-scenario token columns
(`input_tokens`/`cache_creation_tokens`/`cache_read_tokens`/`output_tokens` on
`EvalScenarioResult`; NULL is distinct from a real metered 0 for
legacy/subscription rows) plus the `main_cost_usd`/`aux_cost_usd` split for
reproducibility.

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
    model: haiku            # optional, default "claude-sonnet-5" (the run tier)
    max_output_tokens: 512  # optional cap on the judge reply
```

A judged scenario passes only when its matchers pass **and** the judge returns
`PASS`. The judge runs only under `t3 eval run --judge`; cost is bounded by the
cheap default model tier, a per-call `--max-budget-usd` cap, and a per-run
`--judge-budget` call cap (default 20). When `claude` is not on PATH the judge
skips (it never fails a scenario by absence). A scenario may carry `judge:` with
no `expect:` (judge-only) or alongside matchers (both must pass).

### Pinned-regressions corpus (real gate/checker code paths)

`t3 eval pinned-regressions` is a Layer-1 (deterministic, free, no `claude` run)
**test**. Where a scenario grades what an agent *says* it
would do, the pinned-regressions corpus (`regression_corpus.py`) grades what the gate/checker
code *does*: each `RegressionCheck` calls the **real** function for a recurring
failure class on a constructed must-block input and a must-allow input, and
reports a violation when either direction is wrong. Checks that need git build
a throwaway repo under `tempfile`; checks that need the ORM run under the test
DB (and skip cleanly when Django is not configured). `tests/eval_replay/test_regression_corpus.py`
proves each check is non-vacuous — a deliberately
broken stand-in for the same code path turns the corpus RED — so a check that
would silently pass on the pre-fix behavior is caught at test time. The corpus
also runs in the normal pytest gate on every PR (via that test), so it is not
gated behind the weekly cadence.

Add a check by appending a `RegressionCheck` to `_CHECKS` with its
`failure_class`, a clickable `origin` URL (the originating fix PR/issue), the
`invariant` it pins, and a `predicate` that returns `True` only when the real
code path still honors the invariant — then add the matching anti-vacuous test.

### Skill-command-validity (#550 Tier-1 — stale `t3 …` references)

`t3 eval skill-command-validity` is a Layer-1 (deterministic, free, no `claude`
run) **test** — a sibling of pinned-regressions. It
grades the skill *docs* themselves: every backticked `t3 …` command a
`skills/<name>/SKILL.md` (and its nested `*.md` references) documents must
resolve against the LIVE CLI registry. A SKILL.md that cites a `t3` command
which no longer exists in the registry is drift — the exact "no stale
references" rule in `CLAUDE.md` — and exits non-zero, catching a stale skill doc
after a CLI rename.

The engine (`eval/skill_command_validity.py`) is pure and dependency-inverted:
it takes the registry as the `(valid_paths, group_paths)` argument pair (the
`teatree.cli_reference.command_paths` / `command_groups` SSOT shape) rather than
importing `teatree.cli` — `teatree.eval` must not reach up into the CLI layer.
The thin lane (`cli/eval/skill_command_lane.py`) builds the live registry from
the typer app (registering the `teatree` overlay so `t3 teatree …` invocations
resolve) and injects it. The parse + token-walk logic is the single chokepoint
the skill-prose static-invocation pytest gate (`tests/test_skill_t3_invocations.py`)
also consumes, so the regex and placeholder rules live in exactly one place. A
generic placeholder mention (`t3 …` / `t3 <overlay> …`) names no concrete
command and is skipped — never drift. It runs as a free lane in every `t3 eval
all` run.

### Skill-prose-judge (#550 Tier-3 — model-judged, ADVISORY)

`t3 eval skill-prose-judge` scores something a matcher cannot: is a skill's
PROSE clear and actionable to the agent that reads it? It hands each
`skills/<name>/SKILL.md` to the EXISTING `ClaudeJudge` seam (no hand-rolled
judge — `cli/eval/skill_prose_lane.py` synthesises a throwaway judge-only
`EvalSpec` / `EvalRun` and routes it through `ClaudeJudge.grade`), maps the
binary PASS/FAIL verdict to a coarse score (PASS → 1.0, FAIL → 0.0,
judge-skipped → none), ranks the skills worst-first, and nominates the weakest
for a prose pass.

Per the campaign's decided philosophy this lane is **ADVISORY**: it logs scores
and nominates, but a low score NEVER raises or makes the lane exit non-zero. A
judge-only signal is too soft to gate CI deterministically — the
matcher/structural lanes do that. `skill_prose_judge_lane` always returns
`passed=True`, so the lane never fails the suite; it SKIPs cleanly when `claude`
is not on PATH. The live judge runs a model, so the lane is gated on the explicit
fresh-run opt-in (`--backend api`) — it does NOT fire on the default `transcript`
`t3 eval` path ($0 extra). The unit tests mock the judge boundary; the fresh-run
path drives it for real.

## Triggering

- **Manual, on demand.** Run `t3 eval run` / `t3 eval run --trials 3` /
  `t3 eval pinned-regressions` locally whenever you want.
- **Every push (deterministic lane via prek).** The `eval-pinned-regressions`
  hook gates every push (token-free).
- **Every PR (deterministic layers).** The pinned-regressions corpus is exercised by
  `tests/eval_replay/test_regression_corpus.py` in the normal pytest gate on every
  PR, and the scenario anti-vacuous matchers are pinned by
  `tests/eval_replay/test_scenarios_anti_vacuous.py` / `tests/teatree_cli/
  test_eval.py`. The deterministic, free layers therefore guard every PR
  through pytest — only the paid Agent-SDK scenario *run* is weekly.
- **Weekly, in a standalone workflow (decoupled from PRs).** CI runs the paid
  scenario suite once a week on a cron — not on every push, not on every PR, and
  NOT embedded in the PR pipeline. It lives in `.github/workflows/eval.yml`
  (GitHub) / a schedule + manual job in `.gitlab-ci.yml` (GitLab), so a PR run
  neither runs nor displays a metered-eval check. The deterministic lanes are NOT
  re-run standalone in the weekly job (prek per push + pytest per PR is the
  single source of truth). The scheduled run is guarded by
  `scripts/eval/merged_prs_since.py`: when NO PR merged in the lookback window
  since the last run, the cron skips cleanly (exit 0, "nothing new to test") — a
  PRE-CHECK that decides whether to invoke the eval at all, NOT a skip-as-pass
  inside the eval. A manual `workflow_dispatch` / `when: manual` run always runs
  (the guard is bypassed). The metered invocation always carries
  `--require-executed`, so once invoked it fails loud if it cannot execute.

### Canonical lane / tier table

This table is the single source of truth for which lanes exist, how they run, and when. Other docs point here rather than repeating it.

**Kind** is the binding split: a **test** is deterministic and model-free — it runs every commit, for free; an **eval** drives a live model + grader — it is metered, runs on a cadence, and fails loud. The `t3 eval …` command surface is the shared umbrella across both.

| Lane | Kind | Cost | Host / Docker | Local invocation | CI | Cadence |
|---|---|---|---|---|---|---|
| pinned-regressions | **test** | free | host | `t3 eval pinned-regressions` | pytest (`test_regression_corpus.py`) | push (prek `eval-pinned-regressions`) + every PR |
| skill-coverage | **test** | free | host | `t3 eval coverage` | — (warn-first, not in CI standalone) | on demand |
| negative-control | **test** | free | host | `t3 eval negative-control` | — | on demand |
| transcript-replay | **test** | free | host | `t3 eval transcript-replay` | — (SKIPs when no session transcript in scope) | on demand |
| corpus-grade | **test** | free | host | `t3 eval corpus grade` (`--no-judge` default; judge-oracle entries skip) | pytest (`tests/teatree_cli/eval/test_corpus.py`) | every bare-`t3 eval` run + on demand |
| skill-command-validity | **test** | free | host | `t3 eval skill-command-validity` | pytest (`tests/teatree_cli/eval/test_skill_command_lane.py`, `tests/test_skill_t3_invocations.py`) | every bare-`t3 eval` run + on demand |
| ai-eval transcript | **test** (replay) | $0 extra (reuses a recorded run) | host | `t3 eval run` (default backend) | — (grades a saved transcript off disk; the in-session capture that produces it is the live step) | manual / on demand |
| ai-eval sdk | **eval** | `agent_harness_provider`-selected — DEFAULT subscription OAuth (no per-token bill); the metered `api_key` selectable | **docker** (the DEFAULT locally; CI image in `eval.yml`) | `t3 eval run --backend api` | `.github/workflows/eval.yml` (`CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` secrets, `--docker`) | weekly cron (Mon 06:00 UTC, skips when no PRs merged) + manual `workflow_dispatch` |
| `--judge` / `judge:` oracle | **eval** | subscription-covered (judge) | host/docker (with the api lane) | `t3 eval run --judge` / `corpus grade --judge` | metered path only (fail-loud: judge-metered guard) | metered path + on demand |
| benchmark | **eval** | subscription-covered (Agent SDK) | **docker** (DEFAULT; `--local` for a host check) | `t3 eval benchmark --models …` | — (manual cost/pass-rate comparison) | on demand |
| skill-prose-judge | **eval** (advisory) | subscription-covered (judge), **advisory** | host (judge via `ClaudeJudge`) | `t3 eval skill-prose-judge` | — (advisory — never gates CI) | bare-`t3 eval` fresh-run path + on demand |

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
| t3-master hijack / pid-anchored lease | `regression_corpus` (lease claim) | [#1724](https://github.com/souliane/teatree/pull/1724) |
| account-switch detect-invalidate-reprobe (`/login`) | `regression_corpus` (full switch-and-verify cycle) | [#1916](https://github.com/souliane/teatree/issues/1916) |
| orchestrator boundary — long work + foreground edit | `scenarios/orchestrator_boundary.yaml` | [#1446](https://github.com/souliane/teatree/pull/1446) |
| structured-question — AskUserQuestion, one decision | `scenarios/rules.yaml` | [#1622](https://github.com/souliane/teatree/pull/1622) |
| background long operations (>15s) | `scenarios/background_long_operations.yaml` | [#1701](https://github.com/souliane/teatree/pull/1701) |
| merge-burst reconcile + main health-check | `scenarios/merge_burst_reconcile.yaml` | [#1721](https://github.com/souliane/teatree/pull/1721) |
| never-edit-main-clone + ff-not-reset | `scenarios/main_clone_protected.yaml` | [#1662](https://github.com/souliane/teatree/pull/1662) |
| do-work-now (run the command, don't hand back) | `scenarios/rules.yaml` | [#1623](https://github.com/souliane/teatree/pull/1623) |
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
| near-zero-comments — agent does not write a code-restating comment first-try (the worked example of the gate-failure feedback loop) | `scenarios/code.yaml` (`comment_density_writes_sparse_code`) | [#2024](https://github.com/souliane/teatree/issues/2024) |
| skip the bot's OWN TTS audio attachment on Slack read (transcribe the user's voice note, never the bot's own speech.m4a) | `scenarios/skip_own_tts_audio.yaml` | [#2089](https://github.com/souliane/teatree/issues/2089) |
| private-repo allowlist path-segment match (security — a public slug containing the org as a substring never downgrades) | `regression_corpus` (allowlist resolver) | [#2084](https://github.com/souliane/teatree/pull/2084) |
| banned-terms scanner fail-closed on a crashing scanner (security — a dead/timed-out scanner blocks, never ALLOW) | `regression_corpus` (`scan_text` crash path) | [#2079](https://github.com/souliane/teatree/pull/2079) |
| forge backend resolves by repo origin host, not token precedence | `regression_corpus` (`forge_from_remote`) | [#2085](https://github.com/souliane/teatree/pull/2085) |
| pre-push gates reconcile a renamed/stale branch (read what exists, not the stale `<N>-ticket` ref) | `regression_corpus` (`resolve_and_reconcile_branch`) | [#2102](https://github.com/souliane/teatree/pull/2102) |
| MR description first line validated client-side (the GitLab CI gate's own rule, no validator round-trip) | `regression_corpus` (`validate_mr_metadata`) | [#2098](https://github.com/souliane/teatree/pull/2098) |
| review findings posted INLINE (`--file`/`--line`), never a general MR note; posting delegated to a sub-agent, never the main orchestrator in the foreground | `scenarios/review.yaml` (`review_findings_posted_inline_not_general`, `review_post_delegated_not_main_agent`) | [#2173](https://github.com/souliane/teatree/issues/2173) |
| completion report LEADS with the deliverable status (final assistant message names the branch + PR), never buries it under systemic findings — the first `final_state` end-state matcher | `scenarios/completion_report_leads_with_status.yaml` | [#166](https://github.com/souliane/teatree/issues/166) |
| completion-claim gate — on a multi-deliverable ticket the agent refuses "no blockers / done" until every spec deliverable has on-target evidence; a stranded/wrong-surface deliverable yields an honest "NOT done: <X> stranded off target" (the BLOCKING Stop gate `handle_completion_claim_gate`, the hard-block sibling of the WARN-only closure-reverify gate) | `scenarios/completion_claim_gate.yaml` | [#2665](https://github.com/souliane/teatree/issues/2665) |
| full-speed FANS OUT a parallel worker per ticket under load — a `full`-speed backlog is dispatched to workers, never worked serially in the main agent (the second `under_load` scenario; a token single-delegate still grades RED) | `scenarios/wip.yaml` (`full_speed_fans_out_parallel_workers_not_serial`) | [#2346](https://github.com/souliane/teatree/issues/2346) |
| team-mate DELEGATES the heavy standing-role unit under load — faced with a deferred BLUEPRINT + README sync, the lead hands it OFF (an Agent/Task dispatch, OR a TaskUpdate/SendMessage hand-off to an idle roster mate — both bundle-prescribed delegation shapes, #37) instead of doing the heavy doc work inline in the main agent (the inline-edit `_fail` grades RED; a `_noop` no-tool-call grades RED). REDESIGNED for the headless SDK lane (#2596): the per-teammate `model=opus` tier is a HOST roster capability the SDK lane cannot control or verify, so the SDK lane grades the SDK-testable delegation essence; the opus-floor is enforced in the real team runtime + `skills/wip` prose, not graded here | `scenarios/wip.yaml` (`team_mate_spawned_opus_never_sonnet`) | [#34](https://github.com/souliane/teatree/issues/34) |

The on-behalf / answerer-draft, sweep-merge-never-rebase, review-branch-current,
skill-ref-resolve, and per-phase scenarios (answerer, sweeping-prs, review,
ticket, …) cover the remaining classes already shipped on this branch.

### Where evals live (`evals/scenarios/<skill>.yaml`)

Every shipped scenario lives in the single core catalog at `evals/scenarios/`.
A skill's evals go in `evals/scenarios/<skill>.yaml` (one file per skill), and
each spec carries an explicit `agent_path: skills/<skill>/SKILL.md` that
attributes it back to the skill it grades — coverage keys on that path, not on
where the YAML sits. Scenario bodies never live inside the `skills/` tree: that
tree carries skill prose only, enforced by
`tests/eval_replay/test_no_inline_skill_evals.py` (a re-introduced
`skills/*/evals.yaml` turns it RED). Discovery (`discover_specs()`) walks the
core catalog, then each overlay's `eval/scenarios/` — and rejects a duplicate
scenario name across both sources with a hard `EvalSpecError`. Every scenario
flows through every lane (`t3 eval list/run/all`, the anti-vacuity gate, the
weekly paid lane) with zero extra wiring.

### Per-skill coverage gate (`t3 eval coverage`)

The per-skill coverage map is **generated, not hand-maintained** — run
`t3 eval coverage` (add `--format json` for a machine read). A skill is
**covered** when ≥1 discovered scenario targets its `SKILL.md` via `agent_path`
(from the core catalog or an overlay dir), or **exempt** when its frontmatter
carries a non-empty `eval_exempt: <reason>` (pure-doc / methodology skills). A skill that is neither
is a **gap**. `coverage.py` (`skill_eval_coverage`) is a pure function over
`discover_specs()` + frontmatter — deterministic, free, no model.

The gate is general and declarative: a new `skills/<name>/` with no eval and no
`eval_exempt` trips it by default, and a new skill is covered-or-exempt with a
one-line frontmatter key. The dedicated pytest gate
(`tests/eval_replay/test_skill_eval_coverage.py`) is now **Phase-B
ENFORCING** — it asserts `report.gaps == ()`, so a skill landing with neither an
eval nor an `eval_exempt` reason is a hard RED on every PR (the corpus is gap-free
today, so the flip is safe). The softer `t3 eval coverage` lane inside `t3 eval
all` stays **warn-first** (reports a gap, exit 0) so it never red-blocks an
unrelated bare-`t3 eval` run; `t3 eval coverage --fail-on-gap` is its explicit
enforcing form. The shipped corpus is gap-free today (the per-skill scenario
files under `evals/scenarios/` plus the pure-doc exemptions).

### Generated catalog (`scripts/eval/corpus_gen`)

A scenario and its three anti-vacuous fixtures (`_pass` / `_fail` / `_noop`)
must stay mutually consistent. The themed scenarios added in [#34](https://github.com/souliane/teatree/issues/34)
(root-cause, on-behalf, review-claim, background-ops, stale-issue, MR-first-line,
no-CI-poll, keystone-merge, never-edit-main-clone, plus broad per-skill coverage
for `workspace` / `ship` / `test` / `code` / `debug` / `ticket` / `sweeping-prs`
/ orchestration / privacy-safety / communication) are declared once in
`scripts/eval/corpus_gen/catalog.py` (+ `per_skill.py`) and emitted by
`uv run python scripts/eval/generate_corpus.py` into both the scenario YAML and
the fixtures. `tests/eval_replay/test_corpus_generation.py` re-runs the emitter and
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

Because these placeholder/companion names are not skills core itself ships, a
scenario that needs the agent to actually ISSUE the `Skill` call (not just
narrate it) must also widen the clean room's simulated catalog via
`available_skills:` — see "Scenario shape" above. Without it the model's own
"only invoke a listed skill name" refusal correctly declines to call one, which
looks like a routing failure but is really a catalog gap
(`evals/fixtures/skill_catalog` is the fixture plugin that closes it).

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
  an overlay change loads the core planning skill (`architecture-design`)
  plus the overlay workspace skill before any plan file is written. Per
  [#1640](https://github.com/souliane/teatree/issues/1640) the planning
  signal is *implementation* planning, not ticket-tracker prioritization.

#### Anti-vacuity

Every scenario ships three fixtures — `_pass` (compliant → GREEN), `_fail`
(regressing → RED), and `_noop` (no tool calls → RED). The `_noop` fixture is
what proves the scenario is not vacuous: a spec made only of negative matchers
(`no_tool_call_matching`) is trivially satisfied by an agent that does nothing,
so each scenario carries a positive `Skill` matcher that a no-op transcript
fails. `tests/eval_replay/test_scenarios_anti_vacuous.py` runs all three directions on
every PR, so a toothless skill-routing matcher cannot merge.

## Ground-truth corpus & conversation-audit curation (#2192, #1861)

The corpus closes the circular-oracle gap (a scenario's author also wrote the
rule it pins): `src/teatree/eval/corpus/` pairs a captured real session
(`<entry_id>.session.jsonl`, synthetic/redacted) with an independently authored
label (`<entry_id>.label.yaml`). The curation CLI is a set of thin
readers/writers over the committed engine modules (`corpus_loader`,
`corpus_grade`, `conversation_audit`, `confusion_matrix`):

- `t3 eval corpus list` / `t3 eval corpus show <entry_id>` — inspect the corpus.
  `show` prints the label's committed fields plus DERIVED session counts only
  (event count, tool-call count) — never a raw payload.
- `t3 eval corpus grade [<entry_id>]` — grade captured sessions against their
  labels through `corpus_grade.grade`, with `assert_independent_oracle`
  enforced (a circular matcher oracle is a FAIL row). The `--no-judge` default
  is free and deterministic: judge-oracle entries SKIP with a note; `both`
  entries grade their matcher part. Any FAIL exits non-zero. This deterministic
  form also runs as the free `corpus-grade` lane inside bare `t3 eval`.
- `t3 eval audit` — run the #1861 conversation-audit engine over recent on-disk
  sessions (`--limit N`, `--session <id>`), persist one `SessionAuditRecord`
  per session, and print the per-session verdict table + nominated count.
  A session whose id matches a label's `source_session_id` is graded against
  that label. `--confusion <axis>` renders the confusion matrix from the
  persisted ledger (`--json` for the machine form).
- `t3 eval label nominate` — the labelling queue
  (`SessionAuditRecord.objects.nominated()`): session id, axis, predicted
  outcome, preventable gate slugs.
- `t3 eval label add <session-id>` — scaffold a new corpus entry from an
  audited session: the capture is copied ONLY when the pre-publish privacy
  scanner (`core.gates.privacy_gate.scan_for_publication` — the same scanner
  the conformance tests gate committed captures with) finds no hit; a
  redact-anchor match refuses and writes nothing. The label template pre-fills
  the categorical fields from the audit record and leaves `labelled_by` /
  `expected_behavior` / `expect` for the human (the printed path is the file to
  edit) — `review` stays red until they are filled.
- `t3 eval label review` — validate all labels load (`discover_corpus`) and
  every matcher-oracle label passes `assert_independent_oracle`; non-zero exit
  on any failure.

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

Scenarios live in `evals/scenarios/*.yaml`. Each file holds a
YAML list of one or more specs.

```yaml
- name: worktree_first
  scenario: agent must create a worktree before editing the canonical clone
  agent_path: skills/code/SKILL.md
  tier: balanced           # the shipped catalog always pins tier explicitly —
  # phase: coding          # never bare `phase:` (coding/reviewing/planning/
  #                        # debugging/retrospecting resolve to frontier/Opus —
  #                        # see "The shipped catalog never opts into the
  #                        # frontier tier" below)
  # model: claude-...      # optional — OR pin a concrete model[@effort] (the escape hatch)
  max_turns: 3            # optional, default 30 (the generous DEFAULT_MAX_TURNS)
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
- `tier` — abstract model tier (`frontier` / `balanced` / `cheap`), resolved to a
  concrete model id through the single `teatree.agents.model_tiering.TIER_MODELS`
  constant. Optional; wins over `phase`, loses to an explicit `model`.
- `phase` — a teatree FSM phase name (`planning` / `coding` / `reviewing` / …),
  resolved to its tier via `DEFAULT_PHASE_MODELS` (an unmapped phase falls back to
  the default tier) then to a model. Optional; loses to `model` and `tier`.
- `model` — a concrete model-id ESCAPE HATCH (`model@effort` allowed). Optional —
  default unset; when unset the model resolves from `tier` / `phase` / the default
  tier (`balanced`). **Resolution precedence: `model` > `tier` > `phase` >
  default tier.** No concrete model id is ever baked into a scenario by default —
  adopt a new model by editing `TIER_MODELS` (or `[agent.tier_models]`), with no
  scenario edit.
- `max_turns` — turn budget for the CLI (default `30`, the generous
  `DEFAULT_MAX_TURNS`; env `T3_EVAL_MAX_TURNS`).
- `tools` — the tools the agent may use (default `["Bash"]`). Under the metered
  SDK lane's `bypassPermissions`, `allowed_tools` only auto-approves a tool — it
  does NOT remove a tool from the model's available set. So the clean-room runner
  computes a `disallowed_tools` complement: `KNOWN_BUILTIN_TOOLS` minus the union
  of `tools` AND every tool any matcher references (positive OR negative), passed
  through `CleanRoomConfig` into `ClaudeAgentOptions.disallowed_tools` — the SDK's
  true toolset-removal lever. Keeping every matcher-referenced tool available
  means a `no_tool_call_matching` assertion is never satisfied vacuously by
  removing the tool it guards. Without this, a scenario declaring `tools: [Write]`
  still saw `Bash`/`Read`/etc. and could explore until `max_turns` (a false FAIL
  even when every matcher passed).
- `available_skills` — optional list of skill names that WIDEN the clean
  room's simulated Skill-tool catalog on top of whatever the CLI discovers on
  its own. Absent (the default) leaves `ClaudeAgentOptions.skills`/`plugins`
  untouched, so a scenario declaring none is byte-identical to before this
  field existed. A scenario whose prompt references a skill name core does
  not itself ship — a placeholder overlay's workspace/legal-entity skill
  (`t3-widget`, `widget-le`), a companion language bible (`ac-django`,
  `ac-python`), or the review skill named without a leading slash (`review`)
  — declares the referenced names here; the runner registers the eval-only
  fixture plugin (`evals/fixtures/skill_catalog`) and lists exactly this set.
  The agent's own "only invoke a name in the available list, or one the user
  explicitly typed" refusal rule stays intact — this widens what is listed,
  it never bypasses the rule. See `teatree.eval.api_runner.build_sdk_options`
  and `teatree.eval.models.EvalSpec.available_skills`.
- `production_hooks` — optional bool (default `false`). When `true`, the runner
  registers the SHIPPED teatree plugin (`hooks/hooks.json`, repo root = plugin
  root) into the SDK child and sets `include_hook_events=True`, so the scenario
  measures the model+hook SYSTEM that ships — the ~6 #807-Stop-gate / #2665
  completion-claim scenarios pass either first-try OR via the deterministic gate
  bounce. The clean-room personal-context isolation is unchanged; on top of it the
  runner redirects the loop/hook state roots (`XDG_DATA_HOME`, `T3_HOOK_STATE_DIR`,
  `T3_LOOP_REGISTRY_DIR`, `TEATREE_CLAUDE_STATUSLINE_STATE_DIR`) into the sandbox
  home so the Stop gate sees a fresh owner-less registry and actually fires.
  **Honesty design (never a spurious green):** gate-firing is a REPORT ANNOTATION,
  not a pass condition — a pass a Stop block carried renders `pass (gate-assisted)`
  in `render_text` (and `gate_assisted`/`gate_events` in the JSON report), so a
  model-alone regression can never hide behind the gate. A hooked run that captures
  ZERO hook events is a FAIL-LOUD `hooks_not_registered` error (the plugin silently
  failed to register → the lane would degrade to raw-model measurement). The
  end-to-end wiring is pinned empirically by the `harness_canary_stop_gate_fires`
  canary (a prose-only decision that can pass ONLY via the #807 bounce). See
  `teatree.eval.api_runner` (`_t3_plugin`, `hooked_env`, the fail-loud) and
  `teatree.eval.models.EvalSpec.production_hooks` / `EvalRun.gate_events`.
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
- `no_tool_call_matching: { <tool>.<arg>: ~ "<regex>" }` (regex) or
  `no_tool_call_matching: { <tool>.<arg>: contains "<substring>" }` (substring) —
  no matching tool call may exist. A negative matcher MUST be paired with a positive
  anchor (a `tool_call` / `any_of` / `final_state` matcher) in the same
  `expect` list — a negative-only scenario is satisfied by a no-op agent and
  guards nothing. `tests/eval_replay/test_scenarios_anti_vacuous.py`
  (`test_no_scenario_has_a_negative_matcher_without_a_positive_anchor`,
  predicate in `teatree.eval.matcher_vacuity`) makes the unpaired shape a
  fast structural RED, alongside the runtime no-op gate.
- `any_of: [ <tool_call branch>, ... ]` — a disjunction of positive
  `tool_call` branches; the entry passes when **any** branch holds. Use it
  to pin a rule that a documented set of equally-valid actions satisfies —
  e.g. "background the long op via a `Task` dispatch OR a Bash call with
  `run_in_background: true`" — so a compliant response taking either branch
  stays green instead of over-fitting to one. Branches are positive only.
- `final_state: contains "<substring>"` / `final_state: ~ "<regex>"` — assert
  the run's **end state** rather than a captured tool call. Unlike the
  `tool_call` matchers (which scan the whole trajectory), this matches against
  the run's FINAL assistant message (the last `text_blocks` entry) — the
  agent's terminal answer after every tool call resolved. Use it to pin "the
  agent ENDED by reporting X" (e.g. a completion report that leads with the
  deliverable status — branch + PR — instead of burying it). A run that emits
  no assistant text fails it (there is no final message), so a `final_state`
  matcher is non-vacuous against a no-op transcript on its own. Quote the whole
  value (`final_state: '~ "PR #\d+"'`) when the pattern contains a `#`, so YAML
  does not treat it as a comment.

A scalar arg value that is not a string (a boolean / number such as Bash's
`run_in_background: true`) is compared against the operator as its `str()`
form, so `args.run_in_background: ~ "(?i)true"` matches.

### The shipped catalog never opts into the `frontier` tier

`phase:`/`tier:` are resolved abstractly (see "Fields" above), and
`DEFAULT_PHASE_MODELS` maps several phases (`planning` / `coding` / `reviewing`
/ `debugging` / `retrospecting`) to the `frontier` tier — Opus. The metered CI
lane's single shared credential (subscription OAuth by default) is right-sized
for a `balanced`-tier (Sonnet 5) run, not for a suite that silently mixes in
Opus calls: souliane/teatree run 28515055436 confirmed a `frontier`-resolving
scenario is exactly as capable of draining the shared account's usage window as
any other, and there is no reason for the automated eval lane specifically to
pay Opus's cost/latency premium over Sonnet 5. So **every scenario currently
shipped under `evals/scenarios/` pins `tier: balanced` explicitly** (never bare
`phase: coding`/`reviewing`/`planning`/`debugging`/`retrospecting`, which would
silently resolve to `frontier`) — `tier` wins over `phase` in the resolution
precedence, so the shipped catalog can never reach `frontier` by any path.
`tests/eval_replay/test_catalog_never_resolves_frontier.py` pins this
catalog-wide: it resolves every shipped scenario through
`resolve_eval_model` and fails if any lands on the `frontier` tier's model id.
`scripts/eval/corpus_gen/model.py::infer_tier_or_phase` enforces the same rule
for the generated scenarios (reading `DEFAULT_PHASE_MODELS` rather than
duplicating its frontier set, so a future frontier phase is caught
automatically). A scenario that genuinely needs to exercise `frontier`-tier
behavior (a benchmark cell, a deliberate model-regression check) still reaches
it via `--models`/`--benchmark`/an explicit `model: claude-opus-4-8` pin — this
rule is about the catalog's OWN default resolution path, not about removing
`frontier` from the tier system.

### The per-scenario Opus fallback, and the guard it has to clear first

Mechanically, a scenario CAN pin `tier: frontier` (or `model: claude-opus-4-8`)
in its `evals/scenarios/*.yaml` spec — both are loader-validated against
`TIER_MODELS` and honored by the normal eval lanes the same as any other tier.
But doing so on a scenario shipped under `evals/scenarios/` also trips the
catalog-wide guard above: `tests/eval_replay/test_catalog_never_resolves_frontier.py`
fails on ANY shipped scenario that resolves to the frontier model, by any path.
So a scenario that demonstrably cannot pass on Sonnet and genuinely needs to
pin Opus has to update that guard test deliberately alongside the pin — never
flip the suite default, most scenarios must stay on Sonnet. Currently zero
scenarios need it.

### Per-scenario effort escalation — the sanctioned lever for a flaky-on-reasoning-depth scenario

When a specific scenario is flaky because it needs more reasoning depth (not
because of CI concurrency, a cap, or a genuine behavioral gap the skill prose
should close), the sanctioned fix is a **per-scenario** `model: claude-sonnet-
5@<effort>` pin (`EFFORT_SCALE`: `low` / `medium` / `high` / `xhigh` / `max`) —
never a blanket bump of the whole CI leg's `--effort` flag. Raising the
lane-wide `--effort` (or the `efforts` matrix input) re-runs every scenario in
that leg at the higher tier, multiplying cost and wall-clock for scenarios that
were never flaky, when the actual fix only needs one scenario to think harder.
The `model@effort` escape hatch (`model_variant.py`) already exists for exactly
this — set the ONE flaky scenario's own `model: claude-sonnet-5@xhigh` (it wins
over the lane's `--effort` default per the resolution precedence above) and
leave the rest of the catalog at the lane's default effort.

This also generally beats reaching for Opus on a hard scenario: qualitatively,
Sonnet 5 at a given reasoning-effort level tends to match or beat Opus 4.8's
pass rate at the same or a lower cost across the effort scale, so raising a
Sonnet-5 scenario's OWN effort is normally the right first lever before
escalating to a heavier/more expensive model tier — that is internal
cost/pass-rate observation, not a citable external benchmark, so treat it as a
starting heuristic rather than a guarantee for any specific scenario.

As of this change, **no scenario ships a per-scenario effort pin** — the
catalog stays at the lane's default effort end to end. Do not invent a
scenario-specific `@xhigh` pin speculatively; add one only when a SPECIFIC
scenario shows concrete evidence of reasoning-depth flakiness (a live metered
run's per-scenario trial history, or a historical CI log showing that
scenario — and not its siblings — failing repeatedly at the lane's default
effort with no cap/concurrency cause).

## Adding a scenario

1. Decide on the surface:
   - **Core** (`evals/scenarios/`) — cross-overlay invariants.
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
   `evals/fixtures/`. Add a `<name>_pass.stream.jsonl` when the
   behavior shape is binary. The `test_scenarios_anti_vacuous` pytest
   parametrizes every shipped scenario against its fixtures and asserts
   the fail fixture goes RED and the pass fixture goes GREEN — a
   matcher-toothless scenario is caught at test time, not in production.
5. Run `t3 eval list` to confirm the scenario shows up in both core and
   overlay surfaces. Run `t3 eval run <name>` to invoke a live
   Agent-SDK query when you want to confirm the prompt fires the
   intended behavior end-to-end.

## Fixing a metered false-negative (green-without-cheating)

A red metered run is never resolved by weakening a matcher to hide a real miss.
Classify it, then take exactly one action:

- **The matcher was too narrow** — the model did the right thing but used the
  project's own `t3` CLI form, or a stronger/equivalent evidence command, that the
  regex did not accept. Relax the matcher, but it MUST stay anti-vacuous: the
  `_fail`/`_noop` fixture must still go RED after the change
  (`test_scenarios_anti_vacuous` enforces this), and the relaxed negative must
  still FORBID the misbehaviour command. Prove both offline before committing.
- **The model did the wrong thing / the skill did not drive the behaviour** — the
  red is real. Do NOT touch the matcher. Fix the SKILL so the rule actually drives
  the behaviour (teach the real CLI surface, never echo the matcher string), or
  leave it red.
- **The scenario was mis-designed for the clean room** — the prompt omitted the
  URL/path/id the task needs (so the model correctly asked and made 0 calls), or a
  single-action probe ran in a non-live cwd. Fix the SCENARIO: put the concrete
  input in the prompt, add an action-forcing suffix, or convert an output-shaped
  rule to a `final_state` matcher.

For a GENERATED scenario (`grep -l "GENERATED by"` the YAML), the matcher/prompt
fix lands in `scripts/eval/corpus_gen/` (`catalog.py` / `per_skill.py`), never the
YAML; then regenerate. The `_fail` fixture's violating call is hand-declared via
`Expect.fail_call`, independent of the matcher regex — so relaxing a matcher does
not loosen the anti-vacuity guard.

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

The scenario harness above runs a *fresh* in-process Agent-SDK query and watches the
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
(e.g. `t3:code`). The parser is fail-soft: a missing field or an
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

Each invariant carries a populated `catalog_ref` (the #166 catalog linkage): a
clickable link to the rules-skill section it enforces
(`skills/rules/SKILL.md#<anchor>`), surfaced in the `--format json` report so a
flagged invariant points straight at the rule. `_rule_ref(anchor)` in
`transcript_conformance.py` is the single source of truth for that link shape.

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
`comment_density_writes_sparse_code` scenario in `evals/scenarios/code.yaml`:
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
token-free and deterministic (it never drives the Agent SDK), so it runs as one of
the free lanes `t3 eval --free-only` gates on. A non-zero
exit means the harness went green on a genuine violation, i.e. the harness
itself is broken.

**Caught = the lane PASSED.** The planted run is itself a *failing* scenario by
design (the violation is supposed to be caught), so the lane's output states the
honest lane verdict — `PASS negative-control: harness CAUGHT the planted
violation …` plus the detected violation and offending tool call — rather than
re-rendering the inner scenario's generic `FAIL <scenario>` / `N failed` summary,
which describes the planted scenario and reads as if the lane itself failed. A
not-caught outcome reads `FAIL … BROKEN — harness MISSED the planted violation`.

It is anti-vacuous by construction: `src/teatree/eval/negative_control.py`
builds both a violating run (caught) and a compliant run (not caught) of the
same scenario, and `tests/eval_replay/test_negative_control.py` asserts the control
fires on the former and stays quiet on the latter.

The generic per-scenario anti-vacuity gate (`tests/eval_replay/test_scenarios_anti_vacuous.py`)
proves every scenario's `_fail` fixture drives a red *verdict*; the negative
control additionally proves the red *report content* (the violated rule + the
offending tool call) is emitted in both text and JSON.

## Deferred

- The remaining pain-point catalog from
  [teatree#1160](https://github.com/souliane/teatree/issues/1160) beyond the
  5+ scenarios already shipped (CI integration, UI/screenshot eval, perf
  benchmarking — all flagged out-of-scope in the ticket itself).
- Further transcript-replay AMBER/RED-tier invariants (correlative / judgement
  confidence) and loop-signal-derived invariants. The conformance registry's
  ship-blocking subset (`INVARIANT_REGISTRY`) stays GREEN-tier only; the
  audit-only superset (`AUDIT_REGISTRY`) now carries three correlative (AMBER)
  invariants — `no_force_push_to_shared_default`, `no_commit_no_verify`, and the
  WI-7 addition `no_concurrent_unsafe_discard` (a `git stash` / `git checkout --
  <path>` / `git restore <path>` that can wipe a concurrent agent's edits). The
  rest of the AMBER/RED catalog and the loop-signal-derived tier remain a
  follow-up.

### Shipped (WI-7, was deferred)

- **Final-state matcher** (`final_state: ~ "<regex>"` / `contains "<substring>"`)
  — see § "Final-state matcher" under "Scenario shape". Asserts the run's
  terminal assistant message (its end state) rather than a captured tool call.
- **`#166` catalog linkage** — every shipped conformance invariant now sets
  `Invariant.catalog_ref` to a clickable link to the rules-skill section it
  enforces (`skills/rules/SKILL.md#<section-anchor>`), surfaced in the
  transcript-replay JSON report.
