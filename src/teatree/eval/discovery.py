"""Discover eval scenarios shipped with teatree.

MR 1 keeps this narrow: walk ``src/teatree/eval/scenarios/*.yaml`` only.
Overlay-contributed scenarios are deferred to MR 2.
"""

from pathlib import Path

from teatree.eval.loader import load_eval_yaml
from teatree.eval.models import EvalSpec

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def discover_specs() -> list[EvalSpec]:
    specs: list[EvalSpec] = []
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        specs.extend(load_eval_yaml(path))
    return specs


def find_spec(name: str) -> EvalSpec | None:
    for spec in discover_specs():
        if spec.name == name:
            return spec
    return None
