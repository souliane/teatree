# Testing skill evals

A behaviour-bearing skill ships **evals** — small behavioural scenarios that
grade what an agent *does* when it loads the skill. This page is the end-to-end
guide: how to author a skill's evals, how to run them locally, and how they run
in CI.

For the in-session driver that produces the AI-lane transcripts, use the
`/t3:running-evals` skill. For the harness internals (full schema, every matcher
operator, the failure-class index), see
[`evals/README.md`](https://github.com/souliane/teatree/blob/main/evals/README.md) —
the architecture source of truth.

## test vs eval

Two different things, two different commands:

| Term | What it is | Command | Determinism |
|------|-----------|---------|-------------|
| **test** | unit / integration code test — what a function *returns* | `t3 teatree run tests` | deterministic, free |
| **eval** | AI / trajectory eval — what an agent *did* on a prompt | `t3 eval …` | model-dependent (when metered) |

"Eval" grades behaviour; "test" asserts a return value. This page is about
evals.

## Where evals live

Eval **definitions** (the data) live in a single top-level catalog, not inside
the `skills/` tree:

- `evals/scenarios/<skill>.yaml` — one file per skill. Each spec carries an
  explicit `agent_path: skills/<skill>/SKILL.md` that attributes it back to the
  skill it grades. Coverage keys on that `agent_path`, not on where the YAML
  sits.
- `evals/fixtures/<name>_{pass,fail,noop}.stream.jsonl` — the replay fixtures,
  siblings of the scenarios they pin.
- `evals/README.md` — the architecture source of truth.

The `skills/` tree carries skill **prose only**. A re-introduced
`skills/*/evals.yaml` turns `tests/eval_replay/test_no_inline_skill_evals.py`
RED — scenario bodies must stay in the central catalog. An overlay ships its own
scenarios under `<overlay>/eval/scenarios/`, discovered via
`OverlayBase.get_eval_scenarios_dir()`.

## The two cost lanes

Evals split into a **free deterministic** set and a **metered AI** set. The
distinction matters: the free lanes gate every push and cost nothing; the
metered lane spends API tokens and runs weekly in CI, never on the PR path.

| Lane | Cost | When it runs |
|------|------|-------------|
| skill-triggers | free | every push (commit-stage prek hook) |
| skill-coverage | free | every PR (pytest gate) + inside `t3 eval` |
| pinned-regressions | free | every PR (pytest, push-stage prek hook) |
| negative-control / transcript-replay / corpus-grade / skill-command-validity | free | inside `t3 eval` |
| AI / trajectory (subscription) | subscription | in-session via `/t3:running-evals` |
| AI / trajectory (metered SDK) | metered API | weekly cron + manual dispatch in CI |

The default AI backend is `subscription` (grade in-session transcripts, no API
spend). The metered `--backend sdk` path is **never** a silent fallback — it
runs only when passed explicitly. Bare `t3 eval` runs the free lanes, then
grades subscription transcripts when present in the transcript dir; with none it
emits the manifest and points at `/t3:running-evals` — it never silently meters.

## Authoring a skill's evals

A behaviour-bearing skill is **covered** by ≥1 scenario whose `agent_path`
targets its `SKILL.md`. A skill that carries no behaviour instead declares a
non-empty `eval_exempt: <reason>` in its frontmatter (see
[Coverage](#coverage-every-skill-is-covered-or-exempt)).

### 1. Write the scenario

Add a spec to `evals/scenarios/<skill>.yaml` (create the file if the skill has
no scenarios yet). It is a YAML list of one or more specs:

```yaml
- name: worktree_first
  scenario: agent must create a worktree before editing the canonical clone
  agent_path: skills/code/SKILL.md
  prompt: >-
    You are working in <path>. Fix the typo on line 12 of README.md.
  expect:
    - tool_call: bash
      args.command: contains "git worktree add"
    - no_tool_call_matching:
        bash.command: ~ "Edit.*README\\.md"
```

Key fields (`model` defaults to `claude-sonnet-4-6`; `max_turns` defaults to 30;
`tools` defaults to `[Bash]`):

- `name` — unique across the whole corpus; a duplicate name is a hard
  `EvalSpecError`.
- `agent_path` — the `SKILL.md` the scenario grades; coverage keys on it.
- `scenario` — one-line description, printed by `t3 eval list`.
- `prompt` — the full user message. Keep it hermetic: no real network, no
  secrets, low `max_turns` so a metered run costs cents.
- `expect` — the matchers (below). Required unless a `judge` block is present.

Matcher operators (see the harness README for the full list):

- `tool_call: <tool>` with `args.<path>: contains "<substring>"` or `~ "<regex>"`
  — at least one matching tool call must exist.
- `no_tool_call_matching: { <tool>.<arg>: ~ "<regex>" }` — no matching call may
  exist. Always pair a negative with a positive matcher so a no-op transcript
  cannot pass vacuously.
- `any_of: [ <tool_call branch>, … ]` — passes when any branch holds; use it
  when several equally-valid actions satisfy the rule.
- `final_state: contains "<substr>"` / `~ "<regex>"` — assert the run's final
  assistant message (its terminal answer), not any mid-trajectory call.

### 2. Ship the three anti-vacuous fixtures

Every scenario ships three stream fixtures under
`evals/fixtures/<name>_{pass,fail,noop}.stream.jsonl`. The
`tests/eval_replay/test_scenarios_anti_vacuous.py` test parametrizes every
scenario against them and asserts the `_pass` fixture goes GREEN while `_fail`
and `_noop` go RED — so a matcher with no teeth is caught at test time, not in
production. A scenario that only ships a `_fail` fixture is the minimum; add
`_pass` when the behaviour shape is binary.

Many scenarios and their fixtures are **generated** from a single declaration in
`scripts/eval/corpus_gen/` (run `uv run python scripts/eval/generate_corpus.py`);
`tests/eval_replay/test_corpus_generation.py` re-runs the emitter and fails on
any drift. Hand-written scenarios stay hand-written; only generated files carry
the `# GENERATED` header.

### 3. Verify it is discovered and exercises the rule

```bash
t3 eval list            # confirm the scenario shows up (Name / Scenario / Agent / File / Asserts)
t3 eval run <name>      # (optional, metered) invoke a live Agent-SDK query end-to-end
```

`t3 eval run <name>` is the only metered step here — use it only when you want
to confirm the prompt actually fires the intended behaviour. The free lanes and
the anti-vacuity replay test are enough to land a scenario.

### Coverage: every skill is covered or exempt

The per-skill coverage map is **generated, not hand-maintained**:

```bash
t3 eval coverage                 # warn-first table: covered / exempt / gap
t3 eval coverage --format json   # machine read
t3 eval coverage --fail-on-gap   # enforcing form (exits non-zero on any gap)
```

A skill is **covered** when ≥1 discovered scenario's `agent_path` targets its
`SKILL.md`, **exempt** when its frontmatter carries a non-empty
`eval_exempt: <reason>` (pure-doc / methodology skills), and a **gap** otherwise.
The dedicated pytest gate
(`tests/eval_replay/test_skill_eval_coverage.py`) is enforcing — it asserts zero
gaps, so a new skill with neither an eval nor an `eval_exempt` reason is a hard
RED on every PR. The corpus is gap-free today.

## Running evals locally

### The fast pre-push gate (free, one command)

```bash
t3 eval --free-only
```

This runs every free deterministic lane and prints one unified summary table.
It is the fast local gate — no API spend, no transcripts needed. A clean run on
`main` looks like this (captured 2026-06-16):

```text
                             Eval suite — all lanes
┏━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Lane                   ┃ Cost ┃ Status ┃ Detail                              ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ skill-triggers         │ free │   PASS │ 28 checks, 0 failed                 │
│ skill-coverage         │ free │   PASS │ 35 skills, all covered or exempt    │
│ pinned-regressions     │ free │   PASS │ 15 checks, 0 failed                 │
│ negative-control       │ free │   PASS │ harness caught the planted violation│
│ transcript-replay      │ free │   PASS │ 4 invariants, 0 violated            │
│ corpus-grade           │ free │   PASS │ 2 graded, 0 failed, 1 judge-skipped │
│ skill-command-validity │ free │   PASS │ 256 `t3 …` invocations all resolve  │
└────────────────────────┴──────┴────────┴─────────────────────────────────────┘
✅ ALL GOOD — every check passed (7 lanes).
```

### Individual free lanes

```bash
t3 eval skill-triggers       # every skill's trigger keywords resolve
t3 eval pinned-regressions   # real gate/checker code paths (the regression corpus)
t3 eval negative-control     # harness self-test: plants a known violation, asserts it is caught
t3 eval coverage             # per-skill eval coverage
t3 eval list                 # discovered scenarios (incl. overlay-contributed)
```

### The AI / trajectory lane (no API spend)

The behavioural AI lane cannot be a pure CLI — a standalone process has no
in-session `Agent` and cannot spend subscription tokens. Use the
`/t3:running-evals` skill, which auto-drives the whole chain in one invocation:
`prepare-subscription` → dispatch an in-session sub-agent per scenario →
`capture-subagent` (copies the sub-agent's JSONL to the grader's path) →
`t3 eval run --backend subscription` → one unified table. Reading and grading
on-disk transcripts never meters.

### The metered lane (explicit opt-in only)

```bash
t3 eval run --backend sdk --require-executed
```

This is the metered in-process Agent-SDK path. It authenticates from
`CLAUDE_CODE_OAUTH_TOKEN` (the OAuth token from `claude setup-token`), reaches
the network, and spends API tokens. `--require-executed` makes a
collected-but-all-skipped run exit non-zero, so it can never pass green with
zero coverage. Run it deliberately — it is the same path CI runs weekly.

## Running evals in CI

Two CI surfaces, by cost:

- **Free lanes — every PR.** The deterministic lanes gate every push: the
  `skill-triggers` prek hook at commit-stage, the `pinned-regressions` corpus
  and the `skill-coverage` gate as pytest in the main `ci.yml` pipeline at
  push-stage. `t3 tool verify-gates` runs the same hook set locally — run it
  before pushing.
- **Metered lane — weekly + on demand.** The metered behavioural suite lives in
  its own standalone workflow,
  [`.github/workflows/eval.yml`](https://github.com/souliane/teatree/blob/main/.github/workflows/eval.yml),
  fully independent of the PR pipeline. It runs on a weekly cron (Monday 06:00
  UTC) and on manual `workflow_dispatch`. The scheduled run skips cleanly (exit
  0, logged) when no PR merged in the lookback window — a pre-check, not a
  skip-as-pass. Once invoked it asserts `claude --version` and passes
  `--require-executed` unconditionally, so a missing binary or all-skipped run
  fails RED rather than reporting a decorative green. It publishes a
  self-contained HTML report as a job artifact.

The metered workflow authenticates from the `CLAUDE_CODE_OAUTH_TOKEN` repo
secret. Until that secret is set the job correctly fails RED — that loud failure
is intended, not a regression.

## Latest run summary

Both the core teatree skills and an installed overlay's skills were run through
the free deterministic suite on 2026-06-16 (`t3 eval --free-only`). Both passed
all 7 lanes:

| Tree | Lanes | Result |
|------|-------|--------|
| teatree (35 skills, 22 covered / 13 exempt / 0 gap) | 7 free lanes | all PASS |
| installed overlay (scenarios discovered via the overlay hook) | 7 free lanes | all PASS |

The metered AI lane (`--backend sdk`) was **not** run here — it spends API
tokens and is gated behind the explicit opt-in and the weekly CI workflow.
