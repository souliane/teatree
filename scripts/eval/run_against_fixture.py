r"""Run an eval scenario against a fixture stream-json file, offline.

The behavioral-eval harness normally shells out to ``claude -p`` (cost,
network, model variance). This script bypasses the CLI by feeding a
pre-recorded ``stream-json`` fixture into the same runner+evaluator code
path, so a scenario's matchers can be verified locally on a known-good
and a known-bad transcript before the real ``claude`` run.

Usage::

    uv run python scripts/eval/run_against_fixture.py \
        tests/eval_lanes/scenarios/<slug>.yaml \
        tests/eval_lanes/fixtures/<slug>_fail.stream.jsonl \
        --expect fail

The script exits 0 when the observed verdict matches ``--expect``
(``pass`` / ``fail``), and exits 1 otherwise — wire it into a shell loop
to drive the whole catalog.
"""

import argparse
import tempfile
from pathlib import Path

from teatree.eval.backends import SubscriptionTranscriptRunner
from teatree.eval.loader import load_eval_yaml
from teatree.eval.models import AnyOf, EvalSpec, ExpectItem, FinalStateMatcher
from teatree.eval.report import evaluate


def _parse_args() -> argparse.Namespace:
    summary = (__doc__ or "").splitlines()[0]
    parser = argparse.ArgumentParser(description=summary)
    parser.add_argument("spec_path", type=Path, help="YAML scenario file")
    parser.add_argument("fixture_path", type=Path, help="stream-json fixture file")
    parser.add_argument(
        "--expect",
        choices=("pass", "fail"),
        required=True,
        help="expected verdict against this fixture",
    )
    parser.add_argument(
        "--spec-name",
        default=None,
        help="when the YAML holds >1 spec, pick by name (default: first)",
    )
    return parser.parse_args()


def _pick_spec(specs: list[EvalSpec], spec_name: str | None, spec_path: Path) -> EvalSpec:
    if spec_name is None:
        return specs[0]
    matches = [s for s in specs if s.name == spec_name]
    if not matches:
        msg = f"spec {spec_name!r} not found in {spec_path}"
        raise SystemExit(msg)
    return matches[0]


def main() -> int:
    args = _parse_args()
    specs = load_eval_yaml(args.spec_path)
    spec = _pick_spec(specs, args.spec_name, args.spec_path)
    fixture_text = args.fixture_path.read_text(encoding="utf-8")
    transcript_dir = Path(tempfile.mkdtemp(prefix="eval-scenarios-fixture-"))
    (transcript_dir / f"{spec.name}.jsonl").write_text(fixture_text, encoding="utf-8")
    run = SubscriptionTranscriptRunner(transcript_dir=transcript_dir).run(spec)

    result = evaluate(spec, run)
    actual = "pass" if result.passed else "fail"
    matched = actual == args.expect
    verdict = "OK" if matched else "MISMATCH"
    print(f"{verdict} scenario={spec.name} fixture={args.fixture_path.name} expected={args.expect} actual={actual}")
    if not matched:
        for m in result.matcher_results:
            label = "PASS" if m.passed else "FAIL"
            print(f"  matcher[{label}] {_describe(m.matcher)}")
            if not m.passed and m.message:
                for line in m.message.splitlines():
                    print(f"    {line}")
    return 0 if matched else 1


def _describe(matcher: ExpectItem) -> str:
    if isinstance(matcher, AnyOf):
        return "any_of[" + " | ".join(_describe(alt) for alt in matcher.alternatives) + "]"
    if isinstance(matcher, FinalStateMatcher):
        return f"final_state {matcher.operator} {matcher.value!r}"
    return f"{matcher.kind} {matcher.tool}.{matcher.arg_path} {matcher.operator}"


if __name__ == "__main__":
    raise SystemExit(main())
