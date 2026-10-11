"""The one reader of the commit and tag objects a push publishes: what it cuts, what it prints, how it fails."""

import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.hooks.commit_objects import commit_objects, decode_robust, main, stored_message, without_the_noreply_domain

_NOREPLY = "@users" + ".noreply.github.com"


def _git(repo: Path, *args: str, stdin: bytes = b"") -> bytes:
    return subprocess.run(
        [str(shutil.which("git")), "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "dev")
    _git(work, "config", "user.email", "dev" + _NOREPLY)
    return work


def _commit(repo: Path, message: bytes, *config: str) -> str:
    (repo / "f.txt").write_text(message.decode("latin-1"), encoding="latin-1")
    _git(repo, "add", "f.txt")
    _git(repo, *config, "commit", "-q", "-F", "-", stdin=message)
    return _git(repo, "rev-parse", "HEAD").decode().strip()


class TestTheNoreplyCut:
    def test_only_the_exact_domain_on_an_identity_line_is_cut_and_the_login_stays(self) -> None:
        raw = (
            f"tree 1\nauthor dev <sk-token{_NOREPLY}> 1 +0000\ncommitter dev <dev@example.test> 1 +0000\n\n"
            f"a message naming x{_NOREPLY}>\n"
        ).encode()

        cut = without_the_noreply_domain(raw).decode()

        assert "author dev <sk-token>" in cut
        assert "dev@example.test" in cut, "a different domain is no noreply address"
        assert f"x{_NOREPLY}>" in cut, "the message is left as it is"

    def test_the_indented_copy_inside_a_mergetag_block_is_cut_too(self) -> None:
        raw = f"tree 1\nmergetag object 2\n tagger dev <dev{_NOREPLY}> 1 +0000\n\nm\n".encode()

        assert _NOREPLY.encode() not in without_the_noreply_domain(raw)

    def test_an_object_with_no_blank_line_is_all_headers(self) -> None:
        raw = f"author dev <dev{_NOREPLY}> 1 +0000".encode()

        assert without_the_noreply_domain(raw) == b"author dev <dev> 1 +0000"


class TestWhatIsStored:
    def test_a_stored_message_is_everything_after_the_headers(self) -> None:
        assert stored_message(b"tree 1\nauthor a\n\nfirst\n\nsecond\n") == b"first\n\nsecond\n"

    def test_a_non_utf8_message_decodes_to_latin_1_instead_of_raising(self) -> None:
        assert decode_robust(b"caf\xe9") == "café"
        assert decode_robust("café".encode()) == "café"


class TestThePrintedViews:
    def test_a_message_is_printed_as_git_decodes_it_and_as_the_object_stores_it(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capfdbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        _commit(repo, b"caf\xe9 au lait\n", "-c", "i18n.commitEncoding=ISO-8859-1")
        monkeypatch.chdir(repo)

        assert main(["messages", "-1", "HEAD"]) == 0
        printed = capfdbinary.readouterr().out

        assert "café au lait".encode() in printed, "the log decoded into UTF-8"
        assert b"caf\xe9 au lait" in printed, "the bytes the object stores"

    @pytest.mark.parametrize("how", ["raw", "blank"])
    def test_a_commit_prints_whole_with_the_noreply_domain_cut_only_when_asked(
        self,
        how: str,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfdbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        _commit(repo, b"a message\n")
        monkeypatch.chdir(repo)

        assert main(["commit", how, "-1", "HEAD"]) == 0
        printed = capfdbinary.readouterr().out

        assert (_NOREPLY.encode() not in printed) is (how == "blank")
        assert b"author dev <" in printed

    def test_an_annotated_tag_prints_its_tagger_line(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capfdbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        _commit(repo, b"a message\n")
        _git(repo, "tag", "-a", "-m", "a tag", "v1")
        tag = _git(repo, "rev-parse", "v1").decode().strip()
        monkeypatch.chdir(repo)

        assert main(["tag", "blank", tag]) == 0

        assert b"tagger dev <dev>" in capfdbinary.readouterr().out

    def test_the_objects_of_a_range_come_back_in_order(self, repo: Path) -> None:
        first = _commit(repo, b"one\n")
        second = _commit(repo, b"two\n")

        objects = commit_objects([second, "--not", first], repo)

        assert objects is not None
        assert [stored_message(obj) for obj in objects] == [b"two\n"]


class TestAReadThatCannotBeCompletedPrintsNothing:
    @pytest.mark.parametrize(
        "request_args",
        [
            ["messages", "no-such-revision"],
            ["commit", "raw", "no-such-revision"],
            ["tag", "raw", "no-such-tag"],
            ["commit", "raw"],
            ["commit", "sideways", "HEAD"],
            ["unknown"],
            [],
        ],
    )
    def test_it_exits_1_and_the_scan_around_it_sees_no_text(
        self,
        request_args: list[str],
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfdbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        _commit(repo, b"a message\n")
        monkeypatch.chdir(repo)

        assert main(request_args) == 1
        assert capfdbinary.readouterr().out == b""
