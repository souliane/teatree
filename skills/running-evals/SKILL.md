---
name: running-evals
description: Single in-session entrypoint that auto-orchestrates the whole eval picture — free deterministic lanes (trigger-qa, regression) plus the subscription AI/trajectory lane (prepare → produce transcripts in-session → grade) — and prints one unified results table. Use when running the full eval suite, producing subscription transcripts, or deciding between `t3 eval run` (AI evals) and `t3 teatree run tests` (deterministic tests).
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

| lane | command surface | cost |
|------|-----------------|------|
| skill-trigger eval | `t3 eval trigger-qa` | free |
| regression corpus | `t3 eval regression` | free |
| AI/trajectory eval (subscription) | `t3 eval prepare-subscription` → produce transcripts in-session → `t3 eval run --backend subscription` | subscription |
| AI/trajectory eval (metered CI) | `t3 eval run --backend sdk` | metered API (`ANTHROPIC_API_KEY`) |

The default backend is `subscription` (grade in-session transcripts, no API spend). The metered `claude -p`/SDK path is **never** a silent fallback — it runs only when `--backend sdk` is passed explicitly (CI's path).

## What this skill auto-drives

In ONE invocation, without the human running `prepare-subscription` by hand:

1. `t3 eval prepare-subscription` → the per-scenario agent definition, prompt, and the transcript path the `subscription` backend will read.
2. For each scenario, dispatch an in-session `Agent` sub-agent that runs the prompt with `--output-format stream-json` and writes the transcript to the printed path.
3. `t3 eval run --backend subscription` to grade the transcripts.
4. Print ONE unified results table.

The free deterministic lanes (`t3 eval trigger-qa`, `t3 eval regression`) run alongside. The non-in-session pieces are bundled under `t3 eval all` (below); only step 2 — producing the subscription transcripts — needs this in-session skill.

## `t3 eval all` — the orchestratable non-session piece

```bash
# Free deterministic lanes + AI lane, one unified summary table.
# Grades subscription transcripts when present in the transcript dir;
# with none, emits the manifest + this skill's recipe — never meters.
t3 eval all
t3 eval all --transcript-dir ./transcripts

# Explicit metered opt-in (CI, with ANTHROPIC_API_KEY).
t3 eval all --backend sdk
```

`t3 eval all` runs the FREE lanes, then for the AI lane grades subscription transcripts when they exist, otherwise emits the subscription manifest plus the "produce transcripts in-session — see /t3:running-evals" guidance. It NEVER silently falls back to the metered API path. This skill is the in-session entrypoint that produces the transcripts `t3 eval all` then grades; `t3 eval run` stays canonical and unchanged.

## CLI surface

```bash
# Free deterministic lanes (no model spend).
t3 eval trigger-qa
t3 eval regression

# List discovered scenarios (rich table: Name / Scenario / Agent / File / Asserts).
t3 eval list

# Subscription AI path (no API spend): prepare → produce in-session → grade.
t3 eval prepare-subscription
t3 eval run --backend subscription --transcript-dir ./transcripts

# Whole picture in one command (the non-session orchestratable piece).
t3 eval all
```

## Related

- BLUEPRINT.md — Behavioral eval harness (`src/teatree/eval/`), subscription-default backend, all-skipped guard.
- `src/teatree/eval/README.md` — eval schema, failure-class index, CLI reference.
- `/t3:test` — the deterministic `t3 teatree run tests` side of the test-vs-eval coin.
- `/t3:rules` § "Verification Before Completion" — evals are the behavioural half of that proof.
