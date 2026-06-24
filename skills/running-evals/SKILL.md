---
name: running-evals
description: Single in-session entrypoint that auto-orchestrates the whole eval picture — free deterministic lanes (skill-triggers, pinned-regressions) plus the transcript AI/trajectory lane (prepare → produce transcripts in-session → grade) — and prints one unified results table. Use when running the full eval suite, producing recorded transcripts, or deciding between `t3 eval run` (AI evals) and `t3 teatree run tests` (deterministic tests).
eval_exempt: in-session driver for the eval harness itself; its commands are covered by the eval CLI tests, not by a self-referential behavioural eval
compatibility: any
metadata:
  version: 0.0.1
  subagent_safe: false
requires:
  - rules
---

# Running Evals — the single in-session entrypoint

Running the full eval picture by hand takes several easy-to-forget commands, and the AI/trajectory lane **cannot** be a pure CLI: a standalone process has no in-session `Agent` and cannot spend subscription tokens, so only an in-session driver can produce the transcripts the grader reads. This skill is that driver. Bare `/t3:running-evals` runs the full default suite.

## test vs eval — two sides of one coin

| term | what it is | command | determinism |
|------|-----------|---------|-------------|
| **test** | unit / integration code test | `t3 teatree run tests` | deterministic, free |
| **eval** | AI / trajectory eval (agent behaviour) | `t3 eval run` | non-deterministic (model) |

The CLI mirror is **noun-first**: deterministic tests run under the overlay's `t3 teatree run tests` (a future top-level "t3 test run" form is owned by the separate CLI-simplification audit), AI evals are `t3 eval run`. There is intentionally no "t3 run evals" group — `t3 eval run` is canonical. When in doubt: "eval" grades what the agent *did* on a prompt; "test" asserts what a function *returns*.

## Cost split (never silently meter)

Two buckets under one umbrella. The free deterministic lanes are **tests** — they assert code/config behaviour with fixed I/O, no live model, every commit. The metered lanes are genuine **evals** — they judge a live model's behaviour on a cadence. The `t3 eval …` command surface is shared; the split is about what each lane *is*.

| kind | lane | command surface | cost |
|------|------|-----------------|------|
| **test** (deterministic, no model) | skill-triggers (trigger test) | `t3 eval skill-triggers` | free |
| **test** (deterministic, no model) | pinned-regressions (regression corpus) | `t3 eval pinned-regressions` | free |
| **eval** (fresh model run) | AI/trajectory (api, CI cadence) | `t3 eval run --backend api` | metered on `ANTHROPIC_API_KEY` (never the subscription OAuth token — #2707) |

The default backend is `transcript` — it REUSES an already-recorded run by grading its on-disk transcript ($0 extra, no model run); the in-session step this skill drives produces that transcript (`prepare-transcript` → dispatch sub-agent → `capture-subagent` → `run --backend transcript`). The `--backend api` path RUNS the model fresh, metered EXCLUSIVELY on the `ANTHROPIC_API_KEY` (never the subscription OAuth token, whose usage window a full run would throttle — #2707), and is **never** a silent fallback — it runs only when passed explicitly (CI's cadence). The `transcript` backend runs no model, so it authenticates nothing.

## What this skill auto-drives

In ONE invocation, without the human running `prepare-transcript` or `capture-subagent` by hand:

1. `t3 eval prepare-transcript` → the per-scenario agent definition, prompt, and the transcript path the `transcript` backend will read.
2. For each scenario, dispatch an in-session `Agent` sub-agent that runs the prompt. Claude Code writes that sub-agent's trajectory to `~/.claude/projects/<slug>/<session>/subagents/agent-<id>.jsonl` — NOT to the grader's path.
3. `t3 eval capture-subagent <scenario> --since <epoch>` copies the freshest sub-agent JSONL to the transcript path the grader reads. Record the epoch BEFORE each dispatch and pass it as `--since` so back-to-back scenarios never grab a prior sub-agent's file.
4. `t3 eval run --backend transcript` to grade the captured transcripts.
5. Print ONE unified results table.

The free deterministic lanes (`t3 eval skill-triggers`, `t3 eval pinned-regressions`) — deterministic tests, no model — run alongside. Only steps 2–3 — producing and capturing the recorded transcripts — need this in-session skill; bare `t3 eval` (below) folds in everything else.

The captured transcript is the on-disk session schema (`isSidechain`/`agentId`, no `result` event, terminus via the final assistant `stop_reason`). The `transcript` backend auto-detects it and grades on matchers identically to a stream-json transcript — capture and grade read on-disk files only, so the lane runs no model.

## Bare `t3 eval` — the whole suite, one summary

```bash
# THE DEFAULT: bare `t3 eval` (no subcommand, no args) runs the WHOLE suite —
# free deterministic lanes + AI lane — in one unified summary table.
# Grades recorded transcripts when present in the transcript dir;
# with none, emits the manifest + this skill's recipe — never runs a model.
t3 eval
t3 eval --transcript-dir ./transcripts

# Explicit fresh-run opt-in (CI; metered EXCLUSIVELY on ANTHROPIC_API_KEY).
t3 eval --backend api
```

Bare `t3 eval` runs the FREE lanes, then for the AI lane grades recorded transcripts when they exist ($0 extra), otherwise emits the transcript manifest plus the "produce transcripts in-session — see /t3:running-evals" guidance. It NEVER silently falls back to running a model. This skill is the in-session entrypoint that produces the transcripts the suite then grades; subcommands (`run`, a single lane, `history`, `list`) stay the targeted path and are unchanged.

## CLI surface

```bash
# Free deterministic lanes (no model spend).
t3 eval skill-triggers
t3 eval coverage          # per-skill eval coverage: covered / eval_exempt / gap (warn-first)
t3 eval pinned-regressions

# List discovered scenarios (rich table: Name / Scenario / Agent / File / Asserts).
t3 eval list

# Transcript AI path ($0 extra): prepare → produce in-session → capture → grade.
t3 eval prepare-transcript --transcript-dir ./transcripts
t3 eval capture-subagent <scenario> --transcript-dir ./transcripts --since <epoch>
t3 eval run --backend transcript --transcript-dir ./transcripts

# Whole picture in one command (the bare-`t3 eval` default suite).
t3 eval
```

## Authoring evals

A skill's behavioral evals live in the central catalog at `evals/scenarios/<skill>.yaml` (one file per skill, the **same `EvalSpec` schema** as any other scenario). Each spec carries an explicit `agent_path: skills/<name>/SKILL.md` that attributes it back to the skill it grades — coverage keys on that path, not on where the YAML sits. Scenario bodies never live inside the `skills/` tree (`tests/eval_replay/test_no_inline_skill_evals.py` keeps it prose-only). Each scenario still ships its three anti-vacuous fixtures (`evals/fixtures/<name>_{pass,fail,noop}.stream.jsonl`). A skill with no eval must instead declare a non-empty `eval_exempt: <reason>` in its frontmatter, or `t3 eval coverage` reports it as a gap.

## Related

- BLUEPRINT.md — Behavioral eval harness (`src/teatree/eval/`), transcript-default backend, all-skipped guard.
- `evals/README.md` — eval schema, failure-class index, CLI reference.
- `/t3:test` — the deterministic `t3 teatree run tests` side of the test-vs-eval coin.
- `/t3:rules` § "Verification Before Completion" — evals are the behavioural half of that proof.
