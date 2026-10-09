# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""A caller's xdist ceiling must cross the containerized ``t3`` boundary."""

from pathlib import Path

_WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"


def test_deploy_wrapper_forwards_the_xdist_worker_ceiling() -> None:
    source = _WRAPPER.read_text(encoding="utf-8")
    start = source.index("FORWARD_ENV_NAMES=(")
    forwarded_names = source[start : source.index("\n)\n", start)].splitlines()

    assert "    PYTEST_XDIST_AUTO_NUM_WORKERS" in forwarded_names
