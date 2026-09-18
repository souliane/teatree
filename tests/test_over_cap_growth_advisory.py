# test-path: cross-cutting — a hooks/scripts sibling driven against check_module_health; no src/teatree/ mirror.
"""PostToolUse advisory: the shrink ratchet's refusal, surfaced at edit time (#2663).

The ratchet only speaks at commit time, so an agent that grows an over-cap module
discovers the refusal after the work is done and pays for the extraction twice. This
advisory measures the SAME growth the ratchet will refuse, the moment the write lands.

It can never block: ``PostToolUse`` carries no deny, and the handler returns ``None``
on every path.
"""

import json
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.over_cap_growth_advisory as advisory
from teatree.hooks.portable.check_module_health import MAX_LOC


def _lines(loc: int) -> str:
    return "\n".join(f"a_{i} = {i}" for i in range(loc)) + "\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)  # noqa: S607 — fixture drives the same git the advisory resolves


@pytest.fixture(autouse=True)
def _ledger(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """One already-advised ledger dir per test — a fresh dir per call would defeat the latch."""
    state = tmp_path_factory.mktemp("advised")
    monkeypatch.setattr(advisory, "_ledger_path", lambda session_id: state / f"{session_id}.advised")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose ``src/teatree/big.py`` is committed already over the cap."""
    _git(tmp_path.parent, "init", "-q", "-b", "main", str(tmp_path))
    _git(tmp_path, "config", "user.email", "t@e.st")  # privacy-scan:allow (fake test git-config email, not PII)
    _git(tmp_path, "config", "user.name", "T")
    big = tmp_path / "src" / "teatree" / "big.py"
    big.parent.mkdir(parents=True)
    big.write_text(_lines(MAX_LOC + 3), encoding="utf-8")
    (tmp_path / "src" / "teatree" / "small.py").write_text(_lines(10), encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def _edit(repo: Path, rel: str, *, session: str = "s1") -> dict:
    return {
        "session_id": session,
        "cwd": str(repo),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(repo / rel)},
    }


def _run(data: dict) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        assert advisory.handle_over_cap_growth_advisory(data) is None
    return out.getvalue(), err.getvalue()


def _grow(repo: Path, rel: str, loc: int) -> None:
    (repo / rel).write_text(_lines(loc), encoding="utf-8")


class TestFiresOnExactlyWhatTheRatchetRefuses:
    def test_growing_an_over_cap_module_is_advised_with_its_net_delta(self, repo: Path) -> None:
        _grow(repo, "src/teatree/big.py", MAX_LOC + 13)

        out, err = _run(_edit(repo, "src/teatree/big.py"))

        assert "net +10" in err
        assert "extract" in err.lower()
        assert "docs/module-health.md" in err
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert "net +10" in payload["hookSpecificOutput"]["additionalContext"]

    def test_shrinking_an_over_cap_module_is_silent(self, repo: Path) -> None:
        """Shrinking is the behaviour the rule asks for — never nag it."""
        _grow(repo, "src/teatree/big.py", MAX_LOC + 1)

        assert _run(_edit(repo, "src/teatree/big.py")) == ("", "")

    def test_an_under_cap_module_is_silent(self, repo: Path) -> None:
        _grow(repo, "src/teatree/small.py", 400)

        assert _run(_edit(repo, "src/teatree/small.py")) == ("", "")

    def test_a_test_file_is_not_first_party_and_is_silent(self, repo: Path) -> None:
        target = repo / "tests" / "test_big.py"
        target.parent.mkdir(parents=True)
        target.write_text(_lines(MAX_LOC + 30), encoding="utf-8")

        assert _run(_edit(repo, "tests/test_big.py")) == ("", "")

    def test_a_non_python_file_is_silent(self, repo: Path) -> None:
        (repo / "src" / "teatree" / "big.md").write_text(_lines(MAX_LOC + 30), encoding="utf-8")

        assert _run(_edit(repo, "src/teatree/big.md")) == ("", "")

    def test_an_untracked_new_file_over_the_cap_is_silent(self, repo: Path) -> None:
        """A brand-new file has no grandfathered baseline — that is the ratchet's other branch."""
        (repo / "src" / "teatree" / "fresh.py").write_text(_lines(MAX_LOC + 30), encoding="utf-8")

        assert _run(_edit(repo, "src/teatree/fresh.py")) == ("", "")


class TestSaysItOncePerFile:
    def test_a_second_edit_of_the_same_file_is_silent(self, repo: Path) -> None:
        _grow(repo, "src/teatree/big.py", MAX_LOC + 13)

        assert "net +10" in _run(_edit(repo, "src/teatree/big.py"))[1]
        assert _run(_edit(repo, "src/teatree/big.py")) == ("", "")

    def test_a_different_session_hears_it_again(self, repo: Path) -> None:
        _grow(repo, "src/teatree/big.py", MAX_LOC + 13)

        assert "net +10" in _run(_edit(repo, "src/teatree/big.py", session="s1"))[1]
        assert "net +10" in _run(_edit(repo, "src/teatree/big.py", session="s2"))[1]


class TestBashWritesReachItToo:
    def test_a_sed_in_place_write_is_advised(self, repo: Path) -> None:
        _grow(repo, "src/teatree/big.py", MAX_LOC + 13)
        data = {
            "session_id": "s1",
            "cwd": str(repo),
            "tool_name": "Bash",
            "tool_input": {"command": f"sed -i s/a/b/ {repo}/src/teatree/big.py"},
        }

        assert "net +10" in _run(data)[1]

    def test_an_unpinnable_target_is_silent(self, repo: Path) -> None:
        """Precision-biased, like the resolver it consumes: never guess at a `$VAR` path."""
        _grow(repo, "src/teatree/big.py", MAX_LOC + 13)
        data = {
            "session_id": "s1",
            "cwd": str(repo),
            "tool_name": "Bash",
            "tool_input": {"command": 'sed -i s/a/b/ "$TARGET"'},
        }

        assert _run(data) == ("", "")

    def test_a_read_only_command_is_silent(self, repo: Path) -> None:
        _grow(repo, "src/teatree/big.py", MAX_LOC + 13)
        data = {
            "session_id": "s1",
            "cwd": str(repo),
            "tool_name": "Bash",
            "tool_input": {"command": "grep -n a src/teatree/big.py"},
        }

        assert _run(data) == ("", "")


class TestNeverSpeaksUpWhenItCannotTell:
    def test_a_path_outside_any_repo_is_silent(self, tmp_path: Path) -> None:
        loose = tmp_path / "loose.py"
        loose.write_text(_lines(MAX_LOC + 30), encoding="utf-8")

        data = {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"file_path": str(loose)},
        }
        assert _run(data) == ("", "")

    def test_a_missing_session_id_is_silent(self, repo: Path) -> None:
        data = _edit(repo, "src/teatree/big.py")
        data["session_id"] = ""

        assert _run(data) == ("", "")

    def test_an_unrelated_tool_is_silent(self, repo: Path) -> None:
        assert _run({"session_id": "s1", "cwd": str(repo), "tool_name": "Read", "tool_input": {}}) == ("", "")

    def test_a_resolver_crash_emits_no_advisory_and_names_the_skip(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crash-proof in-handler, and never mute: a swallowed crash reads as a clean file."""
        _grow(repo, "src/teatree/big.py", MAX_LOC + 13)

        def _boom(*_args: object, **_kwargs: object) -> None:
            message = "resolver down"
            raise RuntimeError(message)

        monkeypatch.setattr(advisory, "_written_paths", _boom)
        out, err = _run(_edit(repo, "src/teatree/big.py"))

        assert out == "", "a crashed advisory must inject nothing into the model's context"
        assert "net +" not in err
        assert "skipped" in err
        assert "resolver down" in err


def test_the_module_has_no_deny_path() -> None:
    """Structural pin: an advisory that could deny would sit on the edit hot path."""
    source = Path(advisory.__file__).read_text(encoding="utf-8")

    assert "emit_pretooluse_deny" not in source
    assert "_fail_open_or_deny" not in source
    assert '"decision"' not in source


def test_only_read_dedup_shares_the_post_tool_use_stdout_and_never_the_same_call() -> None:
    """Two stdout payloads on one call would not parse, costing BOTH advisories.

    This advisory emits `additionalContext` JSON; `handle_read_dedup` emits raw text.
    They coexist only because their tool sets are disjoint — pin that, so widening
    either one to the other's tools turns red here instead of in a live session.
    """
    post_tool_use = router._HANDLERS["PostToolUse"]

    assert advisory.handle_over_cap_growth_advisory in post_tool_use
    assert router.handle_read_dedup in post_tool_use
    assert "Read" not in advisory._WRITE_TOOLS
    assert _run({"session_id": "s1", "cwd": ".", "tool_name": "Read", "tool_input": {"file_path": "x.py"}}) == ("", "")
