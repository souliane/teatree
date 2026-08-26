---
name: running-evals
description: Single in-session entrypoint that auto-orchestrates the whole eval picture — model-free deterministic lanes (the eval-coverage gate `t3 evals coverage`, pinned-regressions) plus the transcript AI/trajectory lane (prepare → produce transcripts in-session → grade) — and prints one unified results table. Use when running the full eval suite, producing recorded transcripts, or deciding between `t3 evals run` (AI evals) and `t3 teatree run tests` (deterministic tests).
eval_exempt: in-session driver for the eval harness itself; its commands are covered by the eval CLI tests, not by a self-referential behavioural eval
compatibility: any
metadata:
  version: 0.0.1
requires:
  - rules
---

# Running Evals — the single in-session entrypoint

Running the full eval picture by hand takes several easy-to-forget commands, and the AI/trajectory lane **cannot** be a pure CLI: a standalone process has no in-session `Agent` and cannot spend subscription tokens, so only an in-session driver can produce the transcripts the grader reads. This skill is that driver. Bare `/t3:running-evals` runs the full default suite.

## Run it as `t3 evals` from a sub-agent

- Agent bash-permission layers read a bare `eval` token as the shell builtin and **refuse the command before it runs** — `t3 eval list`, and even `ls src/teatree/eval/`, are rejected at dispatch. The refusal is not a run failure, so it surfaces as an eval failure nowhere: an agent asked to make evals green can only read code and reason, never measure.
- `t3 evals` is the SAME group under a non-colliding name — every subcommand, flag and exit code identical. Use it in sub-agent briefs and dispatch prompts.
- `t3 eval` is unchanged and stays canonical; both spellings resolve.
- `src/teatree/eval/` carries the token too. The tree IS there — reach it with `Glob`/`Read`/`Grep`, never `ls`.

## test vs eval — two sides of one coin

| term | what it is | command | determinism |
|------|-----------|---------|-------------|
| **test** | unit / integration code test | `t3 teatree run tests` | deterministic, model-free |
| **eval** | AI / trajectory eval (agent behaviour) | `t3 eval run` | non-deterministic (model) |

The CLI mirror is **noun-first**: deterministic tests run under the overlay's `t3 teatree run tests` (a future top-level "t3 test run" form is owned by the separate CLI-simplification audit), AI evals are `t3 eval run`. There is intentionally no "t3 run evals" group — `t3 eval run` is canonical, with `t3 evals run` its alias mount (above), not a second surface. When in doubt: "eval" grades what the agent *did* on a prompt; "test" asserts what a function *returns*.

## Cost split (never silently meter)

Two buckets under one umbrella. The model-free deterministic lanes are **tests** — they assert code/config behaviour with fixed I/O, no live model, every commit. The metered lanes are genuine **evals** — they judge a live model's behaviour on a cadence. The `t3 eval …` command surface is shared; the split is about what each lane *is*.

| kind | lane | command surface | cost |
|------|------|-----------------|------|
| **test** (deterministic, no model) | pinned-regressions (regression corpus) | `t3 eval pinned-regressions` | model-free |
| **eval** (fresh model run) | AI/trajectory (api, CI cadence) | `t3 eval run --backend api` | on the `agent_harness_provider` credential — default subscription OAuth (right-sized CI lane), metered `ANTHROPIC_API_KEY` selectable via `--credential api_key` |

The default backend is `transcript` — it REUSES an already-recorded run by grading its on-disk transcript ($0 extra, no model run); the in-session step this skill drives produces that transcript (`prepare-transcript` → dispatch sub-agent → `capture-subagent` → `run --backend transcript`). The `--backend api` path RUNS the model fresh on the credential `agent_harness_provider` selects — DEFAULT the subscription OAuth token (no per-token bill, so the CI lane is right-sized — single effort tier, smaller trial count, per-account OAuth routing — to stay inside the plan's usage window), with the metered `ANTHROPIC_API_KEY` selectable per run via `--credential api_key`. It is **never** a silent fallback — the `api` backend runs only when passed explicitly (CI's cadence). The `transcript` backend runs no model, so it authenticates nothing.

## What this skill auto-drives

In ONE invocation, without the human running `prepare-transcript` or `capture-subagent` by hand:

1. `t3 evals prepare-transcript` → the per-scenario agent definition, prompt, and the transcript path the `transcript` backend will read.
2. For each scenario, dispatch an in-session `Agent` sub-agent that runs the prompt. Claude Code writes that sub-agent's trajectory to `~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl` — NOT to the grader's path.
3. `t3 evals capture-subagent <scenario> --since <epoch>` copies the freshest sub-agent JSONL to the transcript path the grader reads. Record the epoch BEFORE each dispatch and pass it as `--since` so back-to-back scenarios never grab a prior sub-agent's file.
4. `t3 evals run --backend transcript` to grade the captured transcripts.
5. Print ONE unified results table.

The model-free deterministic lane (`t3 evals pinned-regressions`) — deterministic tests, no model — runs alongside. Only steps 2–3 — producing and capturing the recorded transcripts — need this in-session skill; bare `t3 evals` (below) folds in everything else.

The captured transcript is the on-disk session schema (`isSidechain`/`agentId`, no `result` event, terminus via the final assistant `stop_reason`). The `transcript` backend auto-detects it and grades on matchers identically to a stream-json transcript — capture and grade read on-disk files only, so the lane runs no model.

## Bare `t3 evals` — the whole suite, one summary

```bash
# THE DEFAULT: bare `t3 evals` (no subcommand, no args) runs the WHOLE suite —
# model-free deterministic lanes + AI lane — in one unified summary table.
# Grades recorded transcripts when present in the transcript dir;
# with none, emits the manifest + this skill's recipe — never runs a model.
t3 evals
t3 evals --transcript-dir ./transcripts

# Explicit fresh-run opt-in (CI; on the agent_harness_provider credential, default subscription OAuth).
t3 evals --backend api
```

Bare `t3 evals` runs the MODEL-FREE lanes, then for the AI lane grades recorded transcripts when they exist ($0 extra), otherwise emits the transcript manifest plus the "produce transcripts in-session — see /t3:running-evals" guidance. It NEVER silently falls back to running a model. This skill is the in-session entrypoint that produces the transcripts the suite then grades; subcommands (`run`, a single lane, `history`, `list`) stay the targeted path and are unchanged.

## CLI surface

```bash
# Model-free deterministic lanes (no model spend).
t3 evals coverage         # per-skill eval coverage: covered / eval_exempt / gap (a gap exits 1)
t3 evals pinned-regressions

# List discovered scenarios (rich table: Name / Scenario / Agent / File / Asserts).
t3 evals list

# Transcript AI path ($0 extra): prepare → produce in-session → capture → grade.
t3 evals prepare-transcript --transcript-dir ./transcripts
t3 evals capture-subagent <scenario> --transcript-dir ./transcripts --since <epoch>
t3 evals run --backend transcript --transcript-dir ./transcripts

# Whole picture in one command (the bare-`t3 evals` default suite).
t3 evals
```

## Authoring evals

A skill's behavioral evals live in the central catalog at `evals/scenarios/<skill>.yaml` (one file per skill, the **same `EvalSpec` schema** as any other scenario). Each spec carries an explicit `agent_path: skills/<name>/SKILL.md` that attributes it back to the skill it grades — coverage keys on that path, not on where the YAML sits. Scenario bodies never live inside the `skills/` tree (`tests/eval_replay/test_no_inline_skill_evals.py` keeps it prose-only). Each scenario still ships its three anti-vacuous fixtures (`evals/fixtures/<name>_{pass,fail,noop}.stream.jsonl`). A skill with no eval must instead declare a non-empty `eval_exempt: <reason>` in its frontmatter, or `t3 evals coverage` reports it as a gap and exits non-zero.

## Measuring the shipped hook system (`production_hooks`)

Most scenarios grade the RAW model (hooks stripped) against skill prose. When the behaviour a scenario pins is enforced by a shipped **Claude-Code hook** — the #807 structured-question Stop gate, the #2665 completion-claim Stop gate — grading the raw model understates the shipped system. Set `production_hooks: true` on such a scenario: the runner registers the shipped teatree plugin (`hooks/hooks.json`) into the SDK child and pins the loop/hook state roots inside the sandbox home so the gate fires. The scenario then passes first-try OR via the deterministic gate bounce.

Honesty is load-bearing here — a hooked lane must never spuriously pass:

- **Gate-firing is a REPORT ANNOTATION, never a per-scenario pass condition.** A pass a Stop block carried renders `pass (gate-assisted)`; a required "gate fired" matcher would wrongly RED a first-try-compliant model (the gate only fires on non-compliance).
- **A hooked run with ZERO hook events fails loud** (`hooks_not_registered`) — the plugin silently failed to register, so the lane would degrade back to raw-model measurement. It is a graded FAIL result, not a raised exception, so on the six ADVISORY `production_hooks` scenarios it is reported but does not gate; only `done_only_on_deployed_dev_evidence` still reds a lane on it, and that one is `lane: under_load`. So a leg that does not run it — the nightly's `--lane clean_room` shards, the changed-scenarios PR lane, `--surface interactive`, a `--name` run — reports the degradation without gating on it. Read the run output; do not infer registration from a green exit code.
- **One canary reports the wiring end-to-end** (`harness_canary_stop_gate_fires`): a prose-only decision that can pass ONLY through the #807 bounce, so it fails the moment the Stop gate stops firing under the eval wiring. It is `surface: interactive` (it can only pass via a captured `AskUserQuestion` call), so that failure is ADVISORY — reported in the run, never gating. What still GATES on the shipped hook system is `done_only_on_deployed_dev_evidence` (the #2665 completion-claim gate), the one `production_hooks` scenario on the blocking headless surface; the other six are advisory.

Where production enforcement is the **`t3` CLI** (not a Claude hook), fidelity comes from the stub instead: `cli_stubs: [t3@on_behalf_ask]` provisions a gate-aware `t3` that refuses colleague-surface posts (exit 1, parity-tested against the production block message) exactly as the shipped CLI does under ask-mode.

## Related

- BLUEPRINT.md — Behavioral eval harness (`src/teatree/eval/`), transcript-default backend, all-skipped guard.
- `evals/README.md` — eval schema, failure-class index, CLI reference.
- `/t3:test` — the deterministic `t3 teatree run tests` side of the test-vs-eval coin.
- `/t3:rules` § "Verification Before Completion" — evals are the behavioural half of that proof.
