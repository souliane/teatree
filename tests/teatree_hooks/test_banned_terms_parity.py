"""Parity meta-test: every banned-terms entry point agrees on a golden corpus.

The #1839 whole-token migration claimed ``term_match`` was "shared by
``check-banned-terms.sh``", but the shell hook actually carried its OWN
bash-inlined copy of the tokenizer/matcher — a second source of truth that
could drift from :mod:`teatree.hooks.term_match` without anything noticing.
The fix routes the shell hook through ``teatree.hooks.banned_terms_cli``
(which uses ``term_match``), so all three entry points now run ONE matcher:

1. the shell pre-commit hook ``scripts/hooks/check-banned-terms.sh``;
2. the in-process posting gate ``teatree.hooks.banned_terms_scanner``;
3. the core-leak gate's matcher ``teatree.hooks.term_match.matched_term``
(consumed by ``scripts/hooks/check_no_overlay_leak.py``).

This test PINS them to identical verdicts on a shared golden corpus so they
cannot diverge again. The ``MUST_NOT_FLAG`` set is the regression guard: it
goes RED the moment any entry point reverts to substring matching, because
innocent words that merely *contain* a banned substring (``cooperative``,
``operation``, ``operator``, ``desperate``) would then be flagged.

All terms here are SYNTHETIC. ``acme`` stands in for a real single-token
customer term; ``widget-margin`` for a glued multiword term. No real
customer/overlay value appears, so this public test leaks nothing.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from teatree.hooks import banned_terms_scanner
from teatree.hooks.term_match import matched_term

# Anchor the script under test to THIS repo (the one carrying the test), not
# ``find_project_root`` — that helper resolves a worktree back to its primary
# clone, which would invoke the OTHER clone's (possibly older) shell hook.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Synthetic term list shared by every entry point under test.
#   - ``acme``          single-token term.
#   - ``widget-margin`` glued multiword term.
_TERMS: tuple[str, ...] = ("acme", "widget-margin")

# Strings that MUST flag under whole-token matching.
_MUST_FLAG: tuple[str, ...] = (
    "acme",  # bare token
    "AcmeConfig",  # camelCase -> [acme, config]
    "class AcmeProvisionTests:",  # camelCase inside a class name
    "xx-acme-zz",  # kebab-delimited token
    "widget margin",  # glued multiword, space-separated
    "widget_margin",  # glued multiword, snake_case
    "widgetmargin",  # glued multiword, no separator
    'title="widget margin",',  # multiword inside a Python kwarg
)

# Strings that MUST NOT flag — the substring-matching regression guard. Each
# embeds a banned-term substring but has no banned term as a WHOLE token.
_MUST_NOT_FLAG: tuple[str, ...] = (
    "cooperative",  # one unbroken run -> no whole-token "acme"
    "operation",
    "operator",
    "desperate",
    "acmecorp",  # one unbroken lowercase run -> NOT the bare token "acme"
    "acmeology",
    "a clean unrelated sentence about widgets and margins separately",
    "margin widget",  # reversed order -> not the contiguous run
    "",  # empty line
)


def _shell_verdict(tmp_path: Path, text: str) -> bool:
    """Whether ``check-banned-terms.sh`` flags *text* (exit 1)."""
    config = tmp_path / "config.toml"
    config.write_text("[teatree]\nbanned_terms = " + repr(list(_TERMS)) + "\n", encoding="utf-8")
    sample = tmp_path / "sample.txt"
    sample.write_text(text + "\n", encoding="utf-8")
    script = _REPO_ROOT / "scripts" / "hooks" / "check-banned-terms.sh"
    result = subprocess.run(
        [str(script), "--config", str(config), str(sample)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in {0, 1}, f"shell hook crashed: {result.returncode}\n{result.stderr}"
    return result.returncode == 1


def _cli_verdict(tmp_path: Path, text: str) -> bool:
    """Whether the ``banned_terms_cli`` module flags *text* (exit 1)."""
    config = tmp_path / "config.toml"
    config.write_text("[teatree]\nbanned_terms = " + repr(list(_TERMS)) + "\n", encoding="utf-8")
    sample = tmp_path / "sample.txt"
    sample.write_text(text + "\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "teatree.hooks.banned_terms_cli", "--config", str(config), str(sample)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in {0, 1}, f"cli crashed: {result.returncode}\n{result.stderr}"
    return result.returncode == 1


def _scanner_verdict(tmp_path: Path, text: str) -> bool:
    """Whether the posting gate ``banned_terms_scanner`` flags *text*."""
    config = tmp_path / "config.toml"
    config.write_text("[teatree]\nbanned_terms = " + repr(list(_TERMS)) + "\n", encoding="utf-8")
    return banned_terms_scanner.scan_text(text, config_path=config) is not None


def _term_match_verdict(_tmp_path: Path, text: str) -> bool:
    """Whether the shared matcher (consumed by the overlay-leak gate) flags *text*."""
    return matched_term(text, _TERMS) is not None


_ENTRY_POINTS = {
    "shell-hook": _shell_verdict,
    "banned_terms_cli": _cli_verdict,
    "posting-gate": _scanner_verdict,
    "overlay-leak-matcher": _term_match_verdict,
}


def _dir(base: Path, name: str) -> Path:
    """Per-entry-point scratch dir so the temp config/sample files do not collide."""
    sub = base / name
    sub.mkdir(exist_ok=True)
    return sub


@pytest.mark.integration
@pytest.mark.parametrize("text", _MUST_FLAG)
def test_all_entry_points_flag_whole_token_hits(text: str, tmp_path: Path) -> None:
    verdicts = {name: fn(_dir(tmp_path, name), text) for name, fn in _ENTRY_POINTS.items()}
    assert all(verdicts.values()), f"a whole-token hit was not flagged everywhere: {text!r} -> {verdicts}"


@pytest.mark.integration
@pytest.mark.parametrize("text", _MUST_NOT_FLAG)
def test_no_entry_point_flags_innocent_substrings(text: str, tmp_path: Path) -> None:
    verdicts = {name: fn(_dir(tmp_path, name), text) for name, fn in _ENTRY_POINTS.items()}
    assert not any(verdicts.values()), f"an innocent substring was flagged (substring match?): {text!r} -> {verdicts}"
