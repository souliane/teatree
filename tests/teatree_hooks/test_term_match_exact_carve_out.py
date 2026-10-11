"""An allow-listed identifier is exempt only where it occurs exactly; an identifier extending it is still a leak.

Every consumer of the registry ``allow`` class is driven with one placeholder registry: the shared matcher,
the fast-push leak gate, ``scripts/privacy_scan.py`` and the forge-write seam.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from teatree.core.gates.privacy_gate import scan_outbound_text
from teatree.core.push.fast_push import LeakGateScan
from teatree.hooks import _repo_visibility, term_match

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "privacy_scan.py"

HANDLE = "acme-owner"
ALIAS = "alice.work"
ADDRESS = "alice@acme.example"
LEAK = ["acme", "alice"]

EXEMPT_FORMS = (
    HANDLE,
    HANDLE.title(),
    HANDLE.title().replace("-", ""),
    f'"{HANDLE}"',
    f"github.com/{HANDLE}",
    f"<{ADDRESS}>",
    f"mail {ADDRESS}.",
    ALIAS,
    f"ping {ALIAS}.",
)
SIBLINGS = (
    f"{HANDLE}-bot",
    f"bot-{HANDLE}",
    f"{HANDLE}-bot".replace("-", "_"),
    f"{HANDLE}-bot".title().replace("-", ""),
    f"{HANDLE}2",
    ADDRESS.replace("@", "2@"),
    f"{ADDRESS}.evil",
    f"x-{ADDRESS}",
    f"bob.{ADDRESS}",
    f"{ALIAS}.x",
    f"x.{ALIAS}",
)

type Registry = dict[str, list[str]]
type Consumer = Callable[[tuple[str, ...], Registry, pytest.MonkeyPatch], list[bool]]


def _registry(allow: list[str]) -> Registry:
    return {"leak": LEAK, "prose_collider": [], "tone": [], "overlay": [], "allow": allow}


def _shared_matcher(lines: tuple[str, ...], registry: Registry, _monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    return [
        term_match.matched_term(line, tuple(registry["leak"]), tuple(registry["allow"])) is not None for line in lines
    ]


def _fast_push(lines: tuple[str, ...], registry: Registry, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps(registry))
    return [bool(LeakGateScan._banned_terms({"sample.txt": [line]})) for line in lines]


def _banned_term_lines(stdin: str, registry: Registry, *mode: str) -> set[int]:
    env = {key: value for key, value in os.environ.items() if key != "T3_CONFIG_DB"}
    env["TEATREE_TERM_REGISTRY"] = json.dumps(registry)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "-", *mode, "--terms-only", "--json", "--no-strict"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return {item["line"] for item in json.loads(result.stdout) if item["category"] == "banned_term"}


def _privacy_scan(lines: tuple[str, ...], registry: Registry, _monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    flagged = _banned_term_lines("\n".join(lines) + "\n", registry, "--text")
    return [number in flagged for number in range(1, len(lines) + 1)]


def _forge_write(lines: tuple[str, ...], registry: Registry, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps(registry))
    monkeypatch.setattr(_repo_visibility, "probe_visibility", lambda _slug: "PUBLIC")
    verdicts = [scan_outbound_text(text=line, target_repo="acme/widget", forge="github") for line in lines]
    assert all(verdict.is_public for verdict in verdicts)
    assert not [m for v in verdicts for m in v.matches if m.pattern_name.endswith("-unresolvable")]
    return [any(m.pattern_name.startswith("redact:") for m in verdict.matches) for verdict in verdicts]


CONSUMERS: dict[str, Consumer] = {
    "shared-matcher": _shared_matcher,
    "fast-push": _fast_push,
    "privacy-scan": _privacy_scan,
    "forge-write": _forge_write,
}


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_exact_forms_of_an_allow_entry_are_exempt(consumer: str, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = CONSUMERS[consumer](EXEMPT_FORMS, _registry([HANDLE, ALIAS, ADDRESS]), monkeypatch)

    assert [form for form, hit in zip(EXEMPT_FORMS, blocked, strict=True) if hit] == []


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_every_sibling_extending_an_allow_entry_still_blocks(consumer: str, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = CONSUMERS[consumer](SIBLINGS, _registry([HANDLE, ALIAS, ADDRESS]), monkeypatch)

    assert [sibling for sibling, hit in zip(SIBLINGS, blocked, strict=True) if not hit] == []


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_without_the_allow_class_the_exact_forms_block_too(consumer: str, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = CONSUMERS[consumer](EXEMPT_FORMS, _registry([]), monkeypatch)

    assert [form for form, hit in zip(EXEMPT_FORMS, blocked, strict=True) if not hit] == []


@pytest.mark.parametrize(("allow", "flagged"), [([HANDLE, ALIAS, ADDRESS], set()), ([], {4, 5})])
def test_a_diff_marker_before_an_address_entry_does_not_extend_it(allow: list[str], flagged: set[int]) -> None:
    added = (f"+{ALIAS} ping", f"+{ADDRESS}")
    diff = "\n".join(("--- a/notes.md", "+++ b/notes.md", f"@@ -0,0 +1,{len(added)} @@", *added)) + "\n"

    assert _banned_term_lines(diff, _registry(allow)) == flagged
