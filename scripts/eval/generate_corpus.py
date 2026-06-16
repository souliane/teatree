r"""Generate the behavioral-eval corpus: scenario YAML + anti-vacuous fixtures.

Single source of truth is the declaration in
``scripts/eval/corpus_gen/catalog.py``. This entry point renders it to:

*   ``evals/scenarios/<file>.yaml`` — what the loader reads;
*   ``evals/fixtures/<scenario>_{pass,fail,noop}.stream.jsonl`` — what
    the anti-vacuous gate replays.

Run it after editing the catalog::

    uv run python scripts/eval/generate_corpus.py

``tests/eval_replay/test_corpus_generation.py`` re-runs the emitter
and asserts the
committed files match, so a stale checkout (catalog edited, not regenerated)
fails CI rather than shipping drift.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.eval.corpus_gen.all_scenarios import ALL_SCENARIOS
from scripts.eval.corpus_gen.emit import emit_catalog, write_catalog

SCENARIOS_DIR = _ROOT / "evals" / "scenarios"
FIXTURES_DIR = _ROOT / "evals" / "fixtures"


def planned_files() -> tuple[dict[Path, str], dict[Path, str]]:
    return emit_catalog(ALL_SCENARIOS, scenarios_dir=SCENARIOS_DIR, fixtures_dir=FIXTURES_DIR)


def main() -> int:
    written = write_catalog(ALL_SCENARIOS, scenarios_dir=SCENARIOS_DIR, fixtures_dir=FIXTURES_DIR)
    print(f"generated {len(ALL_SCENARIOS)} scenarios, {len(written)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
