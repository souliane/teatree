"""Integration tests for the deploy entrypoint's fleet-loop disable step.

`deploy/entrypoint.sh` (init role) disables the loops the headless box must
not run — the Slack-facing `inbox` / `review` / `directive_loop` — through
`t3 loop disable`, driven by `TEATREE_DISABLED_LOOPS`. Because `t3 loop
disable` exits 0 even on an unregistered name (it only flips a real `Loop`
row), a typo in `TEATREE_DISABLED_LOOPS` would silently disable nothing and
leave the real loop running — a double-run against the operator's laptop. The
step therefore validates the whole requested list against the registered
mini-loops (`t3 loop list --json`) and fails loudly, before disabling
anything, on an unknown name.

In the spirit of the Test-Writing Doctrine these run the REAL shell function
(extracted verbatim from `deploy/entrypoint.sh`) in a bash subprocess with a
stub `t3` on PATH and real `jq` — nothing about the shell logic is
reimplemented. This mirrors the standalone-shell-script tests
(`tests/test_refuse_public_push_with_leak.py`).
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="needs bash + jq (both present in the deploy image and CI)",
)

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"  # absolute path (the skipif guarantees it resolves)

# The registered mini-loops the stub `t3 loop list --json` reports. `loop-tick`
# sits under infra_slots to prove the validator keys on mini-loops only (it is
# NOT a valid disable target).
_STUB_LOOP_STATUS = {
    "mini_loops": [
        {"name": "inbox"},
        {"name": "review"},
        {"name": "directive_loop"},
        {"name": "tickets"},
        {"name": "ship"},
    ],
    "infra_slots": [{"name": "loop-tick"}],
}


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint.

    Slices from the ``name() {`` opener to its column-0 ``}`` terminator — the
    formatting the shellcheck-clean script guarantees.
    """
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _write_t3_stub(bin_dir: Path) -> None:
    """A `t3` shim: reports the loop list from a file, records disable calls.

    Behaviour is env-driven so one shim covers every case:
    ``T3_LIST_JSON`` (file to emit for ``loop list``), ``T3_DISABLE_LOG``
    (append each disabled name), ``T3_LIST_FAIL`` (make ``loop list`` exit
    non-zero), ``T3_FAIL_LOOP`` (make ``loop disable`` fail for that name).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "t3"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "loop" ] && [ "${2:-}" = "list" ]; then\n'
        '  [ -n "${T3_LIST_FAIL:-}" ] && exit 3\n'
        '  cat "$T3_LIST_JSON"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "loop" ] && [ "${2:-}" = "disable" ]; then\n'
        '  echo "$3" >> "$T3_DISABLE_LOG"\n'
        '  if [ "$3" = "${T3_FAIL_LOOP:-}" ]; then echo "boom" >&2; exit 1; fi\n'
        "  echo \"OK    loop '$3' is now disabled.\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path: Path, disabled: str | None, **stub_env: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the extracted function once; return (result, list of disabled names)."""
    bin_dir = tmp_path / "bin"
    _write_t3_stub(bin_dir)
    list_json = tmp_path / "loops.json"
    list_json.write_text(json.dumps(_STUB_LOOP_STATUS), encoding="utf-8")
    disable_log = tmp_path / "disabled.log"
    disable_log.write_text("", encoding="utf-8")

    func = _extract_shell_function("disable_fleet_scoped_loops")
    harness = tmp_path / "harness.sh"
    harness.write_text(f"set -euo pipefail\n{func}\ndisable_fleet_scoped_loops\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["T3_LIST_JSON"] = str(list_json)
    env["T3_DISABLE_LOG"] = str(disable_log)
    env.update(stub_env)
    if disabled is None:
        env.pop("TEATREE_DISABLED_LOOPS", None)
    else:
        env["TEATREE_DISABLED_LOOPS"] = disabled

    result = subprocess.run(
        [_BASH, str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    disabled_names = [line for line in disable_log.read_text(encoding="utf-8").splitlines() if line]
    return result, disabled_names


class TestDisableFleetScopedLoops:
    def test_unset_disables_the_default_fleet_loops(self, tmp_path: Path) -> None:
        result, disabled = _run(tmp_path, None)
        assert result.returncode == 0, result.stderr
        assert disabled == ["inbox", "review", "directive_loop"]

    def test_explicit_subset_disables_only_those(self, tmp_path: Path) -> None:
        result, disabled = _run(tmp_path, "inbox,review")
        assert result.returncode == 0, result.stderr
        assert disabled == ["inbox", "review"]

    def test_whitespace_and_trailing_comma_tolerated(self, tmp_path: Path) -> None:
        result, disabled = _run(tmp_path, "inbox, review, ,")
        assert result.returncode == 0, result.stderr
        assert disabled == ["inbox", "review"]

    def test_empty_value_disables_nothing_and_succeeds(self, tmp_path: Path) -> None:
        result, disabled = _run(tmp_path, "")
        assert result.returncode == 0, result.stderr
        assert disabled == []

    def test_unknown_loop_fails_before_disabling_anything(self, tmp_path: Path) -> None:
        """A typo must fail loudly AND disable nothing — the whole point of the gate.

        `revieww` is unregistered; `inbox` precedes it in the list. Validation
        happens up front, so NOTHING is disabled even though a valid name came
        first — the box is never left half-configured.
        """
        result, disabled = _run(tmp_path, "inbox,revieww")
        assert result.returncode != 0
        assert disabled == [], "a bad list must not disable any loop"
        assert "unknown loop 'revieww'" in result.stderr
        assert "valid loops are:" in result.stderr

    def test_infra_slot_name_is_not_a_valid_target(self, tmp_path: Path) -> None:
        """`loop-tick` is an infra slot, not a disable-able mini-loop → rejected."""
        result, disabled = _run(tmp_path, "loop-tick")
        assert result.returncode != 0
        assert disabled == []
        assert "unknown loop 'loop-tick'" in result.stderr

    def test_disable_failure_is_loud(self, tmp_path: Path) -> None:
        result, _ = _run(tmp_path, "inbox,review", T3_FAIL_LOOP="review")
        assert result.returncode != 0
        assert "'t3 loop disable review' FAILED" in result.stderr

    def test_unreadable_loop_list_fails_loud(self, tmp_path: Path) -> None:
        result, disabled = _run(tmp_path, "inbox", T3_LIST_FAIL="1")
        assert result.returncode != 0
        assert disabled == []
        assert "could not read the registered loops" in result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
