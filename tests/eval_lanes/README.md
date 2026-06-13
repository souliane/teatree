# Eval lanes

This directory holds the **agent/skill behavioral-eval lanes** — NOT the normal
teatree unit tests. The eval-engine unit tests live in `tests/teatree_eval/`,
and everything else lives in `tests/` and the `tests/teatree_*/` packages.

## Layout

- `scenarios/` — the scenario specs (YAML). The loader (`teatree.eval.discovery`)
  reads them from here as the core catalog.
- `fixtures/` — the anti-vacuous transcript fixtures (`_pass` / `_fail` / `_noop`
  `stream-json` files) that the graders replay.
- `deterministic/` — token-free graders that replay the committed fixtures,
  regenerate the corpus, and check matchers + lane wiring. These run on **every
  PR** via pytest (plus the `eval-pinned-regressions` prek hook). No API cost.
- `metered/` — tests of the **metered-lane harness**: the live Agent-SDK runner,
  the model matrix, pass@k, the LLM-judge, and virgin-isolation. The pytest tests
  here verify that machinery deterministically (mocked); they do not call a model.
  The actual metered behavioral run is `.github/workflows/eval.yml`
  (`t3 eval run --backend sdk`), which is weekly / on-demand and **off the PR path**.

The det-vs-metered split is cosmetic for the pytest tests — they all run on every
PR regardless of subdir. The split exists to make the file tree self-documenting:
deterministic graders vs. tests of the metered machinery.

## Engine

The eval engine code and the full eval guide live at
[`src/teatree/eval/README.md`](../../src/teatree/eval/README.md).
