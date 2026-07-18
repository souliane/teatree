"""Integration tests for the deploy entrypoint's fleet-loop policy step.

`deploy/entrypoint.sh` (init role) declares this box's per-loop role through
`apply_fleet_loop_policy`, driven by `TEATREE_ENABLED_LOOPS` /
`TEATREE_DISABLED_LOOPS`. Per-loop enable/disable is now EMERGENCY-only (#3248)
and admission resolves ``hold > forced > preset > base`` — so no preset, schedule,
or ``t3 loop override`` can revive a loop a prior deploy left in a durable
``LoopState`` hold (older images ran ``t3 loop disable inbox``). The step therefore:

It force-enables the ENABLED set (default ``inbox``) with
``t3 loop enable <name> --emergency`` — the ONE handle that clears a stale hold, so
the DM-only box's inbox recovers even from a prior durable disable. It forces the
DISABLED set (default ``review,directive_loop``) off with
``t3 loop override <name> off`` — the sanctioned NON-emergency successor to the
now-refused ``t3 loop disable``.

It never calls the deprecated ``t3 loop disable``. Because a typo in either list
would silently mis-configure the box, the step validates the whole requested set
against the registered mini-loops (``t3 loop list --json``) and fails loudly,
before touching anything, on an unknown name.

In the spirit of the Test-Writing Doctrine these run the REAL shell function
(extracted verbatim from `deploy/entrypoint.sh`) in a bash subprocess with a
stub `t3` on PATH and real `jq` — nothing about the shell logic is
reimplemented. The stub models the emergency gate (bare ``enable`` is refused
with exit 2) and the ``disable`` refusal, exactly as the real CLI does, so the
tests prove the function passes ``--emergency`` and never reaches for ``disable``.
This mirrors the standalone-shell-script tests
(`tests/test_refuse_public_push_with_leak.py`).
"""

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
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
# NOT a valid target).
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
    """A `t3` shim modelling the loop control CLI; records what the policy step calls.

    Behaviour is env-driven so one shim covers every case:
    ``T3_LIST_JSON`` (file to emit for ``loop list``), ``T3_ENABLE_LOG`` /
    ``T3_OVERRIDE_LOG`` / ``T3_DISABLE_LOG`` (append each acted-on name),
    ``T3_LIST_FAIL`` (make ``loop list`` exit non-zero), ``T3_FAIL_LOOP`` (make the
    acting verb fail for that name). It mirrors the real CLI's two gates: a bare
    ``loop enable`` (no ``--emergency``) is refused with exit 2, and ``loop disable``
    is refused outright (emergency-only) — so any use of the deprecated verb, or a
    missing ``--emergency``, trips the function's loud-failure path.
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
        'if [ "${1:-}" = "loop" ] && [ "${2:-}" = "enable" ]; then\n'
        '  name="$3"\n'
        "  emergency=\n"
        '  for arg in "$@"; do [ "$arg" = "--emergency" ] && emergency=1; done\n'
        '  if [ -z "$emergency" ]; then echo "refused: per-loop enable is EMERGENCY-only" >&2; exit 2; fi\n'
        '  echo "$name" >> "$T3_ENABLE_LOG"\n'
        '  if [ "$name" = "${T3_FAIL_LOOP:-}" ]; then echo "boom" >&2; exit 1; fi\n'
        "  echo \"OK    loop '$name' is now enabled.\"\n"
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "loop" ] && [ "${2:-}" = "override" ]; then\n'
        '  name="$3"; state="$4"\n'
        '  echo "$name $state" >> "$T3_OVERRIDE_LOG"\n'
        '  if [ "$name" = "${T3_FAIL_LOOP:-}" ] || [ "$name" = "${T3_FAIL_OVERRIDE_LOOP:-}" ]; '
        'then echo "boom" >&2; exit 1; fi\n'
        "  echo \"OK    loop '$name' override is now $state.\"\n"
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "loop" ] && [ "${2:-}" = "disable" ]; then\n'
        '  echo "$3" >> "$T3_DISABLE_LOG"\n'
        '  echo "refused: per-loop disable is EMERGENCY-only" >&2\n'
        "  exit 2\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What the policy step did: the exit result plus each verb's captured names."""

    result: subprocess.CompletedProcess[str]
    enabled: list[str]
    overridden: list[str]  # "<name> <state>" per call
    disabled: list[str]  # any deprecated `t3 loop disable` reach — must stay empty


def _run(
    tmp_path: Path,
    *,
    enabled: str | None = None,
    disabled: str | None = None,
    **stub_env: str,
) -> _Outcome:
    """Run the extracted function once; return the captured outcome."""
    bin_dir = tmp_path / "bin"
    _write_t3_stub(bin_dir)
    list_json = tmp_path / "loops.json"
    list_json.write_text(json.dumps(_STUB_LOOP_STATUS), encoding="utf-8")
    enable_log = tmp_path / "enabled.log"
    override_log = tmp_path / "overridden.log"
    disable_log = tmp_path / "disabled.log"
    for log in (enable_log, override_log, disable_log):
        log.write_text("", encoding="utf-8")

    func = _extract_shell_function("apply_fleet_loop_policy")
    harness = tmp_path / "harness.sh"
    harness.write_text(f"set -euo pipefail\n{func}\napply_fleet_loop_policy\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["T3_LIST_JSON"] = str(list_json)
    env["T3_ENABLE_LOG"] = str(enable_log)
    env["T3_OVERRIDE_LOG"] = str(override_log)
    env["T3_DISABLE_LOG"] = str(disable_log)
    env.update(stub_env)
    for name, value in (("TEATREE_ENABLED_LOOPS", enabled), ("TEATREE_DISABLED_LOOPS", disabled)):
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value

    result = subprocess.run(
        [_BASH, str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    def _lines(path: Path) -> list[str]:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

    return _Outcome(
        result=result,
        enabled=_lines(enable_log),
        overridden=_lines(override_log),
        disabled=_lines(disable_log),
    )


class TestApplyFleetLoopPolicy:
    def test_defaults_enable_inbox_and_force_colleague_loops_off(self, tmp_path: Path) -> None:
        # The DM-only box: `inbox` is force-enabled (recovering any stale hold),
        # and the colleague-facing `review` / `directive_loop` are forced off — via
        # the override plane, never the deprecated `t3 loop disable`.
        out = _run(tmp_path)
        assert out.result.returncode == 0, out.result.stderr
        assert out.enabled == ["inbox"]
        # `inbox clear` drops any stale forced-off override left by a prior deploy
        # so the sanctioned-enabled inbox can never stay masked; then the
        # colleague loops are forced off.
        assert out.overridden == ["inbox clear", "review off", "directive_loop off"]
        assert out.disabled == [], "the deprecated `t3 loop disable` must never be called"

    def test_enable_passes_emergency(self, tmp_path: Path) -> None:
        # The stub refuses a bare `loop enable` (exit 2). A green run therefore
        # proves the function passes `--emergency` — the only handle that lifts a
        # durable hold so a previously-disabled inbox actually comes back.
        out = _run(tmp_path, enabled="inbox", disabled="")
        assert out.result.returncode == 0, out.result.stderr
        assert out.enabled == ["inbox"]

    def test_explicit_env_overrides_both_lists(self, tmp_path: Path) -> None:
        out = _run(tmp_path, enabled="inbox,tickets", disabled="review")
        assert out.result.returncode == 0, out.result.stderr
        assert out.enabled == ["inbox", "tickets"]
        assert out.overridden == ["inbox clear", "tickets clear", "review off"]
        assert out.disabled == []

    def test_whitespace_and_trailing_comma_tolerated(self, tmp_path: Path) -> None:
        out = _run(tmp_path, enabled="inbox, ,", disabled="review, directive_loop ,")
        assert out.result.returncode == 0, out.result.stderr
        assert out.enabled == ["inbox"]
        assert out.overridden == ["inbox clear", "review off", "directive_loop off"]

    def test_both_empty_is_a_noop_and_succeeds(self, tmp_path: Path) -> None:
        out = _run(tmp_path, enabled="", disabled="")
        assert out.result.returncode == 0, out.result.stderr
        assert out.enabled == []
        assert out.overridden == []
        assert out.disabled == []

    def test_unknown_enabled_loop_fails_before_acting(self, tmp_path: Path) -> None:
        """A typo in the ENABLED list must fail loudly AND touch nothing."""
        out = _run(tmp_path, enabled="inbxo", disabled="review")
        assert out.result.returncode != 0
        assert out.enabled == []
        assert out.overridden == [], "a bad list must not act on any loop"
        assert "unknown loop 'inbxo'" in out.result.stderr
        assert "valid loops are:" in out.result.stderr

    def test_unknown_disabled_loop_fails_before_acting(self, tmp_path: Path) -> None:
        """A typo in the DISABLED list is caught up front, before any enable runs."""
        out = _run(tmp_path, enabled="inbox", disabled="revieww")
        assert out.result.returncode != 0
        assert out.enabled == [], "validation precedes every action — even a valid enable"
        assert out.overridden == []
        assert "unknown loop 'revieww'" in out.result.stderr

    def test_infra_slot_name_is_not_a_valid_target(self, tmp_path: Path) -> None:
        """`loop-tick` is an infra slot, not a mini-loop → rejected."""
        out = _run(tmp_path, enabled="loop-tick", disabled="")
        assert out.result.returncode != 0
        assert out.enabled == []
        assert "unknown loop 'loop-tick'" in out.result.stderr

    def test_enable_failure_is_loud(self, tmp_path: Path) -> None:
        out = _run(tmp_path, enabled="inbox", disabled="review", T3_FAIL_LOOP="inbox")
        assert out.result.returncode != 0
        assert "'t3 loop enable inbox --emergency' FAILED" in out.result.stderr

    def test_override_failure_is_loud(self, tmp_path: Path) -> None:
        out = _run(tmp_path, enabled="inbox", disabled="review", T3_FAIL_LOOP="review")
        assert out.result.returncode != 0
        assert "'t3 loop override review off' FAILED" in out.result.stderr

    def test_unreadable_loop_list_fails_loud(self, tmp_path: Path) -> None:
        out = _run(tmp_path, enabled="inbox", disabled="review", T3_LIST_FAIL="1")
        assert out.result.returncode != 0
        assert out.enabled == []
        assert out.overridden == []
        assert "could not read the registered loops" in out.result.stderr

    def test_loop_in_both_lists_stays_enabled_with_warning(self, tmp_path: Path) -> None:
        """A loop in BOTH lists stays ENABLED (not re-masked) — this was the `inbox` bug.

        The ENABLE pass forces inbox on; the DISABLE pass would then force it off
        on every init, leaving it silently masked. ENABLED wins: the loop is
        dropped from the disable set and a loud warning surfaces the misconfig.
        Resolving (not `exit 1`) is deliberate so an already-deployed box carrying
        the overlap doesn't crash-loop init.
        """
        out = _run(tmp_path, enabled="inbox", disabled="inbox")
        assert out.result.returncode == 0, out.result.stderr
        assert out.enabled == ["inbox"]
        # only the enable-path clear ran; inbox was NOT forced off.
        assert out.overridden == ["inbox clear"]
        assert "in BOTH" in out.result.stderr
        assert "keeping it ENABLED" in out.result.stderr

    def test_overlap_prunes_only_the_shared_loop(self, tmp_path: Path) -> None:
        """Overlap resolution drops only the shared loop; other disables still apply."""
        out = _run(tmp_path, enabled="inbox", disabled="inbox,review")
        assert out.result.returncode == 0, out.result.stderr
        assert out.enabled == ["inbox"]
        assert out.overridden == ["inbox clear", "review off"]

    def test_enable_clears_stale_forced_off_override(self, tmp_path: Path) -> None:
        """Enabling a loop clears any override so a stale forced-off can't keep it masked.

        `t3 loop enable` lifts holds but NOT a forced-off override; without the
        follow-up `clear`, promoting a loop from the DISABLED to the ENABLED set
        between deploys would leave it masked by the stale override.
        """
        out = _run(tmp_path, enabled="inbox", disabled="")
        assert out.result.returncode == 0, out.result.stderr
        assert out.enabled == ["inbox"]
        assert out.overridden == ["inbox clear"]

    def test_enable_path_override_clear_failure_is_loud(self, tmp_path: Path) -> None:
        """A failing override-clear on the enable path aborts loudly (control plane down)."""
        out = _run(tmp_path, enabled="inbox", disabled="", T3_FAIL_OVERRIDE_LOOP="inbox")
        assert out.result.returncode != 0
        assert out.enabled == ["inbox"], "enable ran before the clear failed"
        assert "'t3 loop override inbox clear' FAILED" in out.result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
