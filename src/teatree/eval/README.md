# `teatree.eval` — harness code

This package holds the eval/test **harness code** — the loader, matchers, judge
seam, transcript readers, deterministic lane engines, and the run-store models.

The architecture guide — the lane/tier table, the test-vs-eval split, the
scenario schema, the failure-class index, and the CLI reference — lives next to
the scenarios and fixtures it documents:

➡ [`evals/README.md`](../../../evals/README.md)

The concise end-to-end guide (where evals live, the three cost tiers, how to run,
with-skill/baseline, what CI does) is
[`docs/testing-skill-evals.md`](../../../docs/testing-skill-evals.md).

The short version of the split:

- **tests** — deterministic, no live model, free, run every commit (
  pinned-regressions, skill-command-validity, coverage, negative-control,
  transcript-replay, corpus-grade, and the fixture replay of `scenarios/*.yaml`).
- **evals** — a live model + grader, run on a cadence and fail-loud (the
  `--backend api` AI lane, `--judge`/`judge:` oracles, `benchmark`,
  `skill-prose-judge`). The `api` backend RUNS the model fresh on the credential
  `agent_harness_provider` selects — DEFAULT the subscription OAuth token (no
  per-token bill, so the CI lane is right-sized — single effort tier, smaller trial
  count, per-account OAuth routing — to stay inside the plan's usage window), with
  the metered `ANTHROPIC_API_KEY` selectable per run via `t3 eval run --credential
  api_key`; the
  `anthropic_api` backend runs the SAME Claude model through the Anthropic Messages
  API DIRECTLY (no `claude` CLI child, #3222 — the CLI-free lane, metered on
  `ANTHROPIC_API_KEY`); the `transcript` backend REUSES a recorded run and
  authenticates nothing.
