"""Drift-detecting parity: ``KNOWN_BUILTIN_TOOLS`` agrees with the bundled CLI.

``compute_disallowed_tools`` feeds the complement of a scenario's allowlist to the
bundled ``claude`` CLI's ``--disallowedTools`` lever. The CLI VALIDATES every deny
rule against its own tool registry and prints
``Permission deny rule "<name>" matches no known tool — check for typos.`` for any
name it does not recognise. A stale :data:`KNOWN_BUILTIN_TOOLS` entry (``MultiEdit``
after the CLI dropped it in 2.1.x) therefore made that warning fire on EVERY
clean-room SDK invocation — harmless on its own (the rule is dropped) but proof the
denylist had silently diverged from the binary.

The catalog-level test in ``test_sdk_runner.py`` pins the EXPECTED set; this test
PROBES the actual bundled binary so a future add/remove of a CLI tool fails CI
deterministically rather than drifting unnoticed. It is marked ``integration`` so it
only runs where the bundled ``claude`` is present (it auto-skips otherwise) and is
written as a SINGLE batched ``--disallowedTools`` invocation — the CLI reports one
warning line per unknown name — so it costs one CLI startup, not 26.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import claude_agent_sdk
import pytest

from teatree.eval.toolset import KNOWN_BUILTIN_TOOLS

_UNKNOWN_RE = re.compile(r'Permission deny rule "([^"]+)" matches no known tool')


def _bundled_claude() -> Path | None:
    """The bundled ``claude`` the SDK transport spawns, or ``None`` if unavailable."""
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return bundled
    on_path = shutil.which("claude")
    return Path(on_path) if on_path else None


def _rejected_names(claude: Path, names: list[str]) -> set[str]:
    """The subset of *names* the CLI rejects as unknown deny-rule tools."""
    proc = subprocess.run(
        [str(claude), "-p", "--disallowedTools", ",".join(names), "--max-turns", "1"],
        input="x",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ},
    )
    blob = f"{proc.stdout}\n{proc.stderr}"
    return set(_UNKNOWN_RE.findall(blob))


@pytest.mark.integration
class TestToolsetParityWithBundledCli:
    def test_no_known_builtin_is_rejected_by_the_cli(self) -> None:
        claude = _bundled_claude()
        if claude is None:
            pytest.skip("bundled claude CLI not available")
        # A sentinel bogus name MUST be rejected — proves the probe actually observes
        # the CLI's validation (a probe that never sees a rejection would pass vacuously
        # even if every real name were stale).
        probe = [*KNOWN_BUILTIN_TOOLS, "Zzdefinitelynotatool"]
        rejected = _rejected_names(claude, probe)
        assert "Zzdefinitelynotatool" in rejected, (
            "the bogus sentinel was not rejected — the parity probe is not observing "
            "the CLI's tool-registry validation, so it would pass vacuously"
        )
        stale = sorted(rejected & set(KNOWN_BUILTIN_TOOLS))
        assert not stale, (
            f"KNOWN_BUILTIN_TOOLS contains {stale} which the bundled claude CLI no longer "
            "recognises — every clean-room SDK invocation prints 'matches no known tool' "
            "for each. Remove the stale name(s) from teatree.eval.toolset.KNOWN_BUILTIN_TOOLS."
        )

    def test_removed_multiedit_is_rejected_by_the_cli(self) -> None:
        # The specific regression: MultiEdit was a CLI built-in, then removed. If a
        # future CLI re-adds it, this test fails and we re-add it to the set.
        claude = _bundled_claude()
        if claude is None:
            pytest.skip("bundled claude CLI not available")
        assert "MultiEdit" in _rejected_names(claude, ["MultiEdit"]), (
            "the bundled claude CLI now ACCEPTS MultiEdit again — re-add it to "
            "KNOWN_BUILTIN_TOOLS so the denylist stays exhaustive"
        )
