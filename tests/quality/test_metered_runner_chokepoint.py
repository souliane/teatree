"""Metered-runner construction chokepoint fitness function (souliane/teatree#2328).

The metered ``SdkInProcessRunner`` must be built ONLY through
``teatree.eval.backends.make_runner`` — the single non-Docker path that calls
``ensure_oauth_token()`` before a metered runner exists. A lane that constructs
``SdkInProcessRunner(...)`` directly bypasses that resolver, so on a host
``--local`` run (token only in ``pass``, not the env) the isolated ``claude``
child authenticates as nothing and the run reports a zero-cost auth failure. That
is exactly the bypass the ``t3 eval benchmark`` and ``t3 eval run --trials k``
lanes had.

This AST gate walks the eval source tree and turns RED if any module OTHER than
the allowed chokepoint constructs ``SdkInProcessRunner`` by name — so the bypass
class cannot regress. The construction is allowed only in
``teatree.eval.backends`` (the ``make_runner`` factory). Modeled on
``tests/quality/test_spawn_model_chokepoint.py``.
"""

# test-path: cross-cutting
import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "teatree"

#: The eval source subtrees scanned. Every metered-runner construction lives
#: under one of these, so a bypass anywhere in eval is caught.
_SCANNED_ROOTS = (
    _SRC_ROOT / "cli" / "eval",
    _SRC_ROOT / "eval",
)

_RUNNER_SYMBOL = "SdkInProcessRunner"

#: The ONLY module allowed to construct the metered runner — the ``make_runner``
#: factory that resolves the OAuth token first.
_ALLOWED_MODULES = frozenset({"teatree.eval.backends"})


def _module_dotted(path: Path) -> str:
    rel = path.resolve().relative_to(_SRC_ROOT.parent).with_suffix("")
    parts = rel.parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _constructs_runner(path: Path) -> list[int]:
    """Lines in *path* that call ``SdkInProcessRunner(...)`` as a bare constructor."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == _RUNNER_SYMBOL:
            hits.add(node.lineno)
    return sorted(hits)


def _eval_modules() -> list[Path]:
    seen: dict[Path, None] = {}
    for root in _SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            seen[path.resolve()] = None
    return list(seen)


class TestMeteredRunnerChokepoint:
    def test_scanned_roots_exist(self) -> None:
        for root in _SCANNED_ROOTS:
            assert root.is_dir(), root

    def test_allowed_module_actually_constructs_the_runner(self) -> None:
        # The chokepoint is real: backends.make_runner genuinely builds the runner.
        # If this stops being true the allow-list is stale, not the gate.
        backends = _SRC_ROOT / "eval" / "backends.py"
        assert _constructs_runner(backends), "teatree.eval.backends no longer constructs SdkInProcessRunner"

    def test_no_eval_module_constructs_the_runner_outside_the_chokepoint(self) -> None:
        offenders: dict[str, list[int]] = {}
        for path in _eval_modules():
            module = _module_dotted(path)
            if module in _ALLOWED_MODULES:
                continue
            lines = _constructs_runner(path)
            if lines:
                offenders[module] = lines
        assert not offenders, (
            f"{_RUNNER_SYMBOL} is constructed directly outside teatree.eval.backends — "
            "the OAuth-token resolution in make_runner is bypassed, so a host --local "
            f"metered run authenticates as nothing (souliane/teatree#2328): {offenders}"
        )

    def test_predicate_catches_a_bare_construction(self, tmp_path: Path) -> None:
        bait = tmp_path / "bait.py"
        bait.write_text(
            "from teatree.eval.sdk_runner import SdkInProcessRunner\n"
            "runner = SdkInProcessRunner(max_turns_override=None)\n",
            encoding="utf-8",
        )
        assert _constructs_runner(bait)

    def test_predicate_ignores_make_runner_routing(self, tmp_path: Path) -> None:
        clean = tmp_path / "clean.py"
        clean.write_text(
            "from teatree.eval.backends import SDK_BACKEND, make_runner\n"
            "runner = make_runner(SDK_BACKEND, max_turns_override=None)\n",
            encoding="utf-8",
        )
        assert not _constructs_runner(clean)
