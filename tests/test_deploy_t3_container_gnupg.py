# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""The containerized CLI wrapper's credential prologue: bounded, loud, and GPG-free.

TWO PROPERTIES, one file, because they were one incident.

**The GPG home is no longer this wrapper's business.** ``deploy/t3`` used to carry an
inline predicate that re-derived ``GNUPGHOME`` for the process it exec'd, and
``deploy/profile-gnupg-home.sh`` carried a second copy for a login shell. Both existed
because the image baked the HOST mount and only the entrypoint's own process tree got a
corrected value — so every venue had to repair it for itself, and each unpatched venue
stayed broken. The image now bakes ``GNUPGHOME`` at the container-local path
(``deploy/Dockerfile``) and the entrypoint seeds it (``deploy/entrypoint.sh``), so there
is exactly one value and nothing to repair. A wrapper that still reached for the host
home would put a container process back on the shared keybox — which is the whole defect
(``tests/test_deploy_gnupg_lock_isolation.py`` pins the class).

**The token read is bounded and fails loud.** ``pass show`` shells out to gpg, which
blocks with no deadline of its own on a keybox lock it cannot judge stale; the previous
unbounded, ``2>/dev/null`` read left 322 orphaned ``pass``/``gpg`` pairs alive for ten
hours and exported an EMPTY ``GITLAB_TOKEN`` — which GitLab reports as ``HTTP Basic:
Access denied``, indistinguishable from a branch that does not exist. A read that fails
must REFUSE, naming the wedge on stderr, rather than hand on a credential that is
absent in all but name.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from _deploy_wrapper_paths import PROLOGUE_NAME, container_credential_prologue

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_SH = shutil.which("sh") or "sh"
_REAL_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"


def _fake_pass(bin_dir: Path, body: str) -> Path:
    """Put a ``pass`` on PATH whose behaviour the test dictates."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "pass"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_prologue(
    *,
    bin_dir: Path | None = None,
    env: dict[str, str] | None = None,
    report: str = '"${GITLAB_TOKEN:-<unset>}"',
    check: bool = True,
    with_explicit_pass_route: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Evaluate the wrapper's prologue the way the container does, and report *report*."""
    path = f"{bin_dir}:{_REAL_PATH}" if bin_dir else _REAL_PATH
    return subprocess.run(
        [
            _SH,
            "-c",
            container_credential_prologue(),
            "t3-in-container",
            "sh",
            "-c",
            f'printf "%s" {report}',
        ],
        capture_output=True,
        text=True,
        check=check,
        env={
            "PATH": path,
            **({"TEATREE_GITLAB_TOKEN_PASS_PATH": "gitlab/pat"} if with_explicit_pass_route else {}),
            **(env or {}),
        },
    )


class TestThePrologueNoLongerTouchesTheGpgHome:
    """The retirement is the fix: one baked GNUPGHOME, repaired by nobody."""

    def test_gnupghome_is_left_exactly_as_the_image_baked_it(self, tmp_path: Path) -> None:
        _fake_pass(tmp_path / "bin", 'printf "tok\\n"')
        baked = "/home/teatree/.gnupg-run/gnupg"
        proc = _run_prologue(bin_dir=tmp_path / "bin", env={"GNUPGHOME": baked}, report='"${GNUPGHOME:-<unset>}"')
        assert proc.stdout.strip() == baked

    def test_the_prologue_never_assigns_gnupghome_at_all(self) -> None:
        # Textual, deliberately: a second venue re-deriving the home is the CLASS this
        # change retires, so the assertion is "nobody writes it here", not "this input
        # happens to round-trip".
        # Statement-anchored, not a bare substring: the prologue legitimately REPORTS
        # $GNUPGHOME in its diagnostics, which is the opposite of writing it.
        assignments = [
            line.strip()
            for line in container_credential_prologue().splitlines()
            if line.strip().startswith(("GNUPGHOME=", "export GNUPGHOME"))
        ]
        assert not assignments, f"the wrapper must not re-derive a GPG home; the image bakes it: {assignments}"

    def test_the_image_profile_hook_is_gone(self) -> None:
        # Its only reason to exist was the per-venue repair this change retires.
        assert not (WRAPPER.parent / "profile-gnupg-home.sh").exists()


class TestTokenRead:
    def test_reads_the_token_when_none_is_set(self, tmp_path: Path) -> None:
        _fake_pass(tmp_path / "bin", 'printf "s3cret\\nmetadata\\n"')
        assert _run_prologue(bin_dir=tmp_path / "bin").stdout.strip() == "s3cret"

    def test_an_already_set_token_is_never_overwritten(self, tmp_path: Path) -> None:
        # A forwarded host token, or one the entrypoint already exported.
        _fake_pass(tmp_path / "bin", 'printf "from-the-store\\n"')
        proc = _run_prologue(bin_dir=tmp_path / "bin", env={"GITLAB_TOKEN": "forwarded"})
        assert proc.stdout.strip() == "forwarded"

    def test_it_reads_the_configured_pass_path(self, tmp_path: Path) -> None:
        _fake_pass(tmp_path / "bin", 'printf "%s\\n" "$2"')
        proc = _run_prologue(bin_dir=tmp_path / "bin", env={"TEATREE_GITLAB_TOKEN_PASS_PATH": "gitlab/other"})
        assert proc.stdout.strip() == "gitlab/other"

    def test_no_explicit_route_does_not_read_a_populated_legacy_entry(self, tmp_path: Path) -> None:
        _fake_pass(tmp_path / "bin", 'printf "legacy-token\\n"')

        proc = _run_prologue(bin_dir=tmp_path / "bin", with_explicit_pass_route=False)

        assert proc.stdout.strip() == "<unset>"


class TestAWedgedStoreRefusesRatherThanRunningOnAnEmptyToken:
    """An EMPTY token authenticates as nobody and reads as a missing branch."""

    def test_a_store_that_fails_to_answer_stops_the_dispatch(self, tmp_path: Path) -> None:
        _fake_pass(tmp_path / "bin", 'echo "gpg: No Keybox daemon running" >&2; exit 2')
        proc = _run_prologue(bin_dir=tmp_path / "bin", check=False)
        assert proc.returncode != 0, "the command ran anyway, on a credential the store never supplied"
        assert proc.stdout == "", f"the dispatched command produced output, so it ran: {proc.stdout!r}"

    def test_the_refusal_names_the_wedge_rather_than_a_missing_secret(self, tmp_path: Path) -> None:
        _fake_pass(tmp_path / "bin", 'echo "gpg: No Keybox daemon running" >&2; exit 2')
        proc = _run_prologue(bin_dir=tmp_path / "bin", check=False)
        assert "WEDGED store" in proc.stderr, proc.stderr
        assert "gpgconf --kill all" in proc.stderr, "the refusal must carry the remedy, not just the verdict"

    def test_an_absent_store_is_not_a_wedged_one(self, tmp_path: Path) -> None:
        """CI and a laptop that never set `pass` up must pass straight through, never refuse."""
        # PATH is overridden outright rather than prepended: `_run_prologue` appends the
        # real PATH, on which this box HAS a `pass` — the absence would never be modelled.
        # `sh` is linked in because the prologue still has to `exec` what it was handed.
        bare = tmp_path / "no-pass-here"
        bare.mkdir()
        (bare / "sh").symlink_to(_SH)
        proc = _run_prologue(env={"PATH": str(bare)}, check=False)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "<unset>"

    def test_an_empty_answer_is_never_exported_as_a_token(self, tmp_path: Path) -> None:
        # rc=0 with no output is what a wedged store produced; exporting "" is the bug.
        _fake_pass(tmp_path / "bin", "exit 0")
        assert _run_prologue(bin_dir=tmp_path / "bin").stdout.strip() == "<unset>"


@pytest.mark.skipif(shutil.which("timeout") is None, reason="needs coreutils timeout (present in the deploy image)")
class TestTheReadIsBounded:
    """gpg blocks with no deadline of its own on a lock it cannot judge stale."""

    def test_a_hanging_read_is_cut_off_rather_than_waited_out(self, tmp_path: Path) -> None:
        _fake_pass(tmp_path / "bin", "sleep 30")
        proc = _run_prologue(bin_dir=tmp_path / "bin", env={"TEATREE_SECRET_READ_DEADLINE_SECONDS": "1"}, check=False)
        assert proc.returncode != 0
        assert "did not answer" in proc.stderr, proc.stderr

    def test_the_deadline_is_configurable_and_defaults_to_twenty_seconds(self) -> None:
        prologue = container_credential_prologue()
        assert 'deadline="${TEATREE_SECRET_READ_DEADLINE_SECONDS:-20}"' in prologue
        assert 'timeout "$deadline" pass show "$pass_path"' in prologue


class TestWrapperUsesThePrologue:
    def test_execs_the_command_it_was_handed(self, tmp_path: Path) -> None:
        # The prologue wraps every containerized `t3` invocation, so one that swallowed
        # its arguments would break the whole CLI rather than one secret read.
        _fake_pass(tmp_path / "bin", 'printf "tok\\n"')
        proc = subprocess.run(
            [
                _SH,
                "-c",
                container_credential_prologue(),
                "t3-in-container",
                "sh",
                "-c",
                'printf "argv=[%s]" "$*"',
                "_",
                "review",
                "post-comment",
            ],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": f"{tmp_path / 'bin'}:{_REAL_PATH}"},
        )
        assert proc.stdout.strip() == "argv=[review post-comment]"

    def test_the_running_worker_exec_path_carries_it(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")
        # Matched on `docker compose` rather than the `-f` spelling: since #4193 the
        # wrapper passes its compose files through `${COMPOSE_ARGS[@]}`, so anchoring on
        # `-f` silently matched NOTHING and the loop below asserted over an empty list.
        exec_lines = [line for line in wrapper.splitlines() if "docker compose" in line and " exec " in line]
        assert exec_lines, "the wrapper must still exec into the running worker"
        for line in exec_lines:
            assert f'"${PROLOGUE_NAME}"' in line, f"exec path bypasses the credential prologue: {line.strip()}"

    def test_never_advertises_the_host_install_as_a_remedy(self) -> None:
        # The host `t3` is the forbidden path — the sanctioned wrapper pointing at it
        # is what keeps host processes holding descriptors on the control DB.
        assert "~/.local/bin/t3" not in WRAPPER.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
