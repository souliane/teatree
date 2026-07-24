"""Push-gate scans the pushed commit range, and names the finding's commit+file (#3675 defect 2).

Two properties the public-repo pre-push leak gate must hold. It evaluates the pushed
commit range, never the working tree — a clean commit pushes even when the working tree
carries unrelated in-progress edits (including a secret in an uncommitted file that is
not going to be pushed). And when it does refuse, the message names the commit and the
file the finding is in, so the operator can locate and scrub it instead of distrusting
an unlocatable gate.

Integration tests in the spirit of the sibling suite: a real ``git`` repo, a real
``origin`` remote, a real ``gh`` shim, and the real ``privacy_scan.py`` — nothing mocked.
"""

import os
import subprocess
from pathlib import Path

from tests.test_refuse_public_push_with_leak import _clone_with_remote, _git, _push_stdin, _run_hook


def _rev_parse(work: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607 - git resolved on PATH (test)
        cwd=work,
        capture_output=True,
        text=True,
        check=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    ).stdout.strip()


# Planted fixture value the gate must flag; annotated so this repo's own pre-push
# gate does not flag the test source itself.
_PLANTED_SECRET = "token = glpat-XXXXXXXXXXXXXXXX\n"  # privacy-scan:allow fixture


class TestPushGateScansCommitRangeNotWorkingTree:
    def test_clean_commit_pushes_despite_a_dirty_working_tree_carrying_a_secret(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "feature.txt").write_text("a clean new feature line\n", encoding="utf-8")
        _git(work, "add", "feature.txt")
        _git(work, "commit", "-m", "add clean feature")

        # Unrelated in-progress edit left UNCOMMITTED in the working tree — it is not in
        # the pushed commit and must not influence the gate's decision.
        (work / "scratch.txt").write_text(_PLANTED_SECRET, encoding="utf-8")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 0, result.stdout + result.stderr


class TestRefusalNamesCommitAndFile:
    def test_refusal_names_the_offending_commit_and_file(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "config.env").write_text(_PLANTED_SECRET, encoding="utf-8")
        _git(work, "add", "config.env")
        _git(work, "commit", "-m", "add config")

        sha = _rev_parse(work)
        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert sha[:12] in combined, f"refusal must name the commit; got:\n{combined}"
        assert "config.env" in combined, f"refusal must name the file; got:\n{combined}"

    def test_refusal_names_the_specific_leaking_commit_among_several(self, tmp_path: Path) -> None:
        work, env = _clone_with_remote(tmp_path, "PUBLIC")
        (work / "clean_a.txt").write_text("nothing here\n", encoding="utf-8")
        _git(work, "add", "clean_a.txt")
        _git(work, "commit", "-m", "clean a")

        (work / "leaky.env").write_text(_PLANTED_SECRET, encoding="utf-8")
        _git(work, "add", "leaky.env")
        _git(work, "commit", "-m", "add secret")
        leaking_sha = _rev_parse(work)

        (work / "clean_b.txt").write_text("also nothing\n", encoding="utf-8")
        _git(work, "add", "clean_b.txt")
        _git(work, "commit", "-m", "clean b")

        result = _run_hook(work, env, _push_stdin(work))

        assert result.returncode == 1, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert leaking_sha[:12] in combined, f"refusal must name the leaking commit; got:\n{combined}"
        assert "leaky.env" in combined, f"refusal must name the leaking file; got:\n{combined}"
