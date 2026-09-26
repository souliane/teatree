"""The leak-gate "why did this SCAN?" diagnostic (#1415/#1213 friction).

Two block shapes had nothing to do with the body and said nothing about
themselves: a publish CHAINED with anything the skip predicate cannot prove
inert (correct — an unrecognised segment could hide a second publish — but
discoverable only by stalling), and a flagless publish whose destination did not
resolve because the hook's cwd is not inside the target repo (so a post to a repo
the config already declares internal blocks as if it were public).

The diagnostic is TEXT ONLY. :class:`TestNeverChangesAVerdict` is the load-bearing
half: the hint is silent wherever the gate is right to block, so nothing here can
be mistaken for a carve-out.
"""

from pathlib import Path

import pytest

from teatree.hooks import public_visibility
from teatree.hooks.scan_scope_hint import scan_scope_hint

_PRIVATE_NS = "acme-internal"
_PUBLISH = "glab mr create --title 'techdebt(x): y' --description-file /tmp/body.md"


@pytest.fixture
def private_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout whose ``origin`` is in a namespace declared internal offline."""
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "config.sqlite3"))
    monkeypatch.setenv("T3_INTERNAL_PUBLISH_NAMESPACES", _PRIVATE_NS)
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = git@gitlab.com:{_PRIVATE_NS}/widget.git\n', encoding="utf-8"
    )
    return clone


@pytest.fixture
def not_a_repo(tmp_path: Path) -> Path:
    plain = tmp_path / "plain"
    plain.mkdir()
    return plain


class TestChainedPublish:
    def test_a_chained_publish_that_would_skip_alone_is_named_as_such(self, private_clone: Path) -> None:
        chained = f"{_PUBLISH} | python3 -c 'print(1)'"
        assert public_visibility.gate_skips_for_visibility(_PUBLISH, private_clone), "fixture must skip when alone"
        assert not public_visibility.gate_skips_for_visibility(chained, private_clone)

        hint = scan_scope_hint(chained, private_clone)
        assert "CHAINED" in hint
        assert "own Bash call" in hint

    def test_the_isolated_publish_is_offered_verbatim_when_faithful(self, private_clone: Path) -> None:
        hint = scan_scope_hint(f"{_PUBLISH} | python3 -c 'print(1)'", private_clone)
        assert _PUBLISH in hint

    def test_no_unfaithful_reconstruction_is_ever_offered(self, private_clone: Path) -> None:
        # A subshell's `cd` is untracked (fail-closed, mirrors `segment_cwds`), so
        # the segment's effective dir is genuinely unprovable here — the category
        # is honestly UNRESOLVED, not a falsely-confident CHAINED. Either way, no
        # reconstructed command (whose closing paren would land inside the last
        # word, producing a command that does not run) is ever offered.
        hint = scan_scope_hint(f"(cd {private_clone} && {_PUBLISH})", private_clone)
        assert "WHY THIS SCANNED" in hint
        assert "body.md)" not in hint


class TestSegmentCwdBinding:
    """MINOR (MR !225 finding): each publish is judged against its OWN effective cwd.

    Every `cd` in the chain re-points it — never the hook's ambient cwd — and any
    suggested retry is bound to that directory.
    """

    def test_private_launch_dir_public_target_is_judged_at_the_target_not_the_launch(
        self, private_clone: Path, not_a_repo: Path
    ) -> None:
        # `cd /work/public-checkout && glab mr create ...` launched from an
        # allowlisted PRIVATE checkout. Pre-fix, this was judged against the
        # PRIVATE launch dir: it falsely claimed "issued alone this would have
        # skipped" and suggested re-issuing a command that, run from the launch
        # dir (no cd), would hit the WRONG (private) repo instead.
        chained = f"cd {not_a_repo} && {_PUBLISH}"
        assert not public_visibility.gate_skips_for_visibility(chained, private_clone)

        hint = scan_scope_hint(chained, private_clone)
        assert "CHAINED" not in hint

    def test_the_reverse_public_launch_dir_private_target_still_offers_the_fix(
        self, private_clone: Path, not_a_repo: Path
    ) -> None:
        # The reverse pairing: launched from a dir with no resolvable destination,
        # `cd`-ing into the PRIVATE clone before a chained publish. The CHAINED fix
        # must still be offered — bound to the dir the isolated publish actually
        # runs in, never the launch dir it was judged from before the fix.
        chained = f"cd {private_clone} && {_PUBLISH} | python3 -c 'print(1)'"
        assert public_visibility.gate_skips_for_visibility(_PUBLISH, private_clone), "fixture must skip when alone"
        assert not public_visibility.gate_skips_for_visibility(chained, not_a_repo)

        hint = scan_scope_hint(chained, not_a_repo)
        assert "CHAINED" in hint
        assert f"cd {private_clone}" in hint


class TestUnresolvableDestination:
    def test_a_flagless_publish_from_outside_the_repo_names_the_repo_flag(self, not_a_repo: Path) -> None:
        hint = scan_scope_hint(_PUBLISH, not_a_repo)
        assert "no publish destination could be resolved" in hint
        assert "--repo <owner/repo>" in hint
        assert "private_repos" in hint

    def test_naming_the_target_explicitly_removes_the_block_and_the_hint(self, private_clone: Path) -> None:
        named = f"{_PUBLISH} --repo {_PRIVATE_NS}/widget"
        assert public_visibility.gate_skips_for_visibility(named, private_clone)
        assert scan_scope_hint(named, private_clone) == ""


class TestNeverChangesAVerdict:
    """The hint is silent wherever the gate is right to block — it explains, never excuses."""

    def test_silent_on_a_clean_lone_publish_the_gate_already_skips(self, private_clone: Path) -> None:
        assert scan_scope_hint(_PUBLISH, private_clone) == ""

    def test_silent_on_a_resolved_non_internal_destination(self, private_clone: Path) -> None:
        # Resolvable but not provably internal: the gate SCANS (so a hint could
        # fire) and the diagnostic still declines — the destination is the
        # command's own explicit choice, and there is no scope fix to offer.
        outward = f"{_PUBLISH} --repo some-public-org/widget"
        assert not public_visibility.gate_skips_for_visibility(outward, private_clone)
        assert scan_scope_hint(outward, private_clone) == ""

    def test_silent_when_the_command_is_not_a_publish(self, private_clone: Path) -> None:
        assert scan_scope_hint("echo hello && git status", private_clone) == ""

    @pytest.mark.parametrize("command", ["", "   ", "\n"])
    def test_silent_on_an_empty_command(self, command: str, private_clone: Path) -> None:
        assert scan_scope_hint(command, private_clone) == ""

    def test_an_internal_error_degrades_to_silence_never_an_exception(
        self, private_clone: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A diagnostic must never be able to break the gate it annotates.
        def _boom(*_args: object, **_kwargs: object) -> bool:
            msg = "probe exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(public_visibility, "gate_skips_for_visibility", _boom)
        assert scan_scope_hint(_PUBLISH, private_clone) == ""
