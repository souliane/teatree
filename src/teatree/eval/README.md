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

1. Drop a YAML file under `src/teatree/eval/scenarios/`.
2. Pick the smallest `agent_path` that exhibits the behavior (a single
   `SKILL.md`, not a bundle).
3. Keep prompts hermetic — no real network, no secrets — and keep
   `max_turns` low so a single run costs cents, not dollars.
4. Run `t3 eval run <name>` locally and confirm the matchers behave on a
   known-good and a known-bad agent definition.

## Deferred (later MRs)

- Overlay-contributed scenario discovery (MR 2).
- Negative-control scenario (MR 3).
- Final-state matcher (MR 3).
- The remaining catalog from [teatree#1160](https://github.com/souliane/teatree/issues/1160).
- prek manual hook integration (MR 2).
