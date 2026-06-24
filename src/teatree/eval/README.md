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

- **tests** — deterministic, no live model, free, run every commit (skill-triggers,
  pinned-regressions, skill-command-validity, coverage, negative-control,
  transcript-replay, corpus-grade, and the fixture replay of `scenarios/*.yaml`).
- **evals** — a live model + grader, run on a cadence and fail-loud (the
  `--backend api` AI lane, `--judge`/`judge:` oracles, `benchmark`,
  `skill-prose-judge`). The `sdk` backend RUNS the model fresh, metered
  EXCLUSIVELY on `ANTHROPIC_API_KEY` (never the subscription OAuth token, which a
  full run would throttle — #2707); the `transcript` backend REUSES a recorded
  run and authenticates nothing.
