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

## Invocation

```bash
t3 eval list                                # show available scenarios
t3 eval run                                 # run all
t3 eval run worktree_first                  # run one
t3 eval run --format json                   # JSON output
t3 eval run worktree_first --max-turns 5    # override max_turns
```

Each invocation shells out to `claude -p` in `--output-format stream-json`
mode with a 120-second wall-clock watchdog and a `--max-budget-usd 0.10`
circuit breaker. When `claude` is not on `PATH` the runner emits
`SKIP <scenario>: claude binary not on PATH` and exits 0.

## Scenario shape

Scenarios live in `src/teatree/eval/scenarios/*.yaml`. Each file holds a
YAML list of one or more specs.

```yaml
- name: worktree_first
  scenario: agent must create a worktree before editing the canonical clone
  agent_path: skills/code/SKILL.md
  model: haiku            # optional, default "haiku"
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
- `model` — Claude model alias (default `"haiku"`).
- `max_turns` — turn budget for the CLI (default `4`).
- `tools` — allow-list of tools exposed to the agent (default `["Bash"]`).
- `expect` — non-empty list of matchers (see below).

Supported matcher operators:

- `tool_call: <tool>` with `args.<path>: contains "<substring>"` — at
  least one matching tool call must exist.
- `no_tool_call_matching: { <tool>.<arg>: ~ "<regex>" }` — no matching
  tool call may exist.

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

## Deferred

- Negative-control scenario.
- Final-state matcher.
- prek manual hook integration.
- The remaining catalog from [teatree#1160](https://github.com/souliane/teatree/issues/1160).
