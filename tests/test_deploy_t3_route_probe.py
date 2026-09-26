# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""Route resolution costs ONE daemon call, and no credential read is unbounded.

``deploy/t3`` used to answer "which control-DB route is running?" with a
``docker compose ps`` per candidate service plus a ``docker inspect`` per returned
id — and it asks four times on an ordinary invocation (``SERVICE_CONTAINER``, then
``first_running_service`` at the browser, forward and dispatch hops). ``docker
compose`` is a plugin binary that re-reads and re-interpolates the compose file on
every call before it ever reaches the daemon, so route resolution alone dominated
the wrapper's share of a ``t3`` call. A plain ``docker ps`` label query answers every
route in one round trip, and the answer is reused.

The credential path had the same shape of defect for a different reason: ``pass
show`` ran with ``2>/dev/null`` and an unconditional ``return 0``, so a WEDGED gpg
keybox (a locked ``pubring.db.lock``, orphan ``keyboxd``/``gpg-agent`` daemons under
split ``GNUPGHOME``s) produced the same empty string as "no such entry". The empty
value then crossed the boundary and surfaced much later as ``HTTP Basic: Access
denied`` or as a branch that does not exist. A bounded read that names the wedge is
the only outcome that cannot be mistaken for an absent secret.

No test here ever handles a credential VALUE — only ``pass`` key paths and exit codes.
"""

import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
from _deploy_wrapper_paths import container_credential_prologue

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
WRAPPER = DEPLOY / "t3"
COMPOSE_FILE = DEPLOY / "docker-compose.yml"
_BASH = shutil.which("bash") or "bash"


def _wrapper_text() -> str:
    return WRAPPER.read_text(encoding="utf-8")


def _slice(start_marker: str, end_marker: str) -> str:
    """The wrapper's own source between two markers — never a re-implementation."""
    text = _wrapper_text()
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _shell_function(name: str) -> str:
    lines = _wrapper_text().splitlines()
    start = lines.index(f"{name}() {{")
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


def _run_bash(preamble: str, script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    body = "set -euo pipefail\n" + preamble + "\n" + textwrap.dedent(script)
    return subprocess.run(
        [_BASH, "-c", body],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **(env or {})},
    )


# ── the single-call route probe ───────────────────────────────────────


def test_compose_project_default_matches_the_compose_file_name() -> None:
    """The label filter's project MUST be the project compose itself would derive.

    The probe scopes `docker ps` with `label=com.docker.compose.project=<name>`. If
    that default and the compose file's `name:` ever drift, the query matches NOTHING
    and every route reads as down — which degrades silently to the one-off `run --rm`
    fallback on every call. Pinning the two spellings together is what makes renaming
    one without the other fail here instead of in production.
    """
    declared = re.search(r"^name:\s*(\S+)\s*$", COMPOSE_FILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert declared is not None, "docker-compose.yml declares no top-level `name:`"
    wrapper_default = re.search(
        r'^COMPOSE_PROJECT="\$\{COMPOSE_PROJECT_NAME:-([^}]+)\}"', _wrapper_text(), re.MULTILINE
    )
    assert wrapper_default is not None, "deploy/t3 no longer defines COMPOSE_PROJECT"
    assert wrapper_default.group(1) == declared.group(1)


def test_route_resolution_no_longer_shells_out_to_compose_ps() -> None:
    """`compose ps` per route is the cost this change removes — it must not return.

    Comments are stripped first: the block explaining WHY the call went away names it,
    and an assertion that trips over its own rationale would have to be deleted the
    moment anyone documented the change.
    """
    code = "\n".join(line for line in _wrapper_text().splitlines() if not line.lstrip().startswith("#"))
    assert "compose ps" not in code


def test_the_daemon_is_probed_once_plus_the_deliberate_update_wait_reprobe() -> None:
    """Exactly two probe CALL sites: the one-time fill, and the update-wait refresh.

    The wait loop re-probes on purpose — it exists to observe a route COMING BACK, so
    a memo would answer "still down" for the whole wait and then report a false
    timeout. Any third call site means the per-ask probing has crept back.
    """
    calls = re.findall(r'RUNNING_SERVICE_TABLE="\$\(probe_running_services\)"', _wrapper_text())
    assert len(calls) == 2, f"expected 2 probe call sites, found {len(calls)}"


PROBE_PREAMBLE = _slice("running_service_container() {", "\nRUNNING_SERVICE_TABLE=") + "\nSERVICE=teatree-worker\n"


def test_the_probe_preamble_actually_defines_the_function_under_test() -> None:
    """Anti-vacuity guard — see the credential slice's twin above."""
    result = _run_bash(PROBE_PREAMBLE, "declare -F running_service_container >/dev/null && echo DEFINED")
    assert result.stdout.strip() == "DEFINED", result.stderr


@pytest.mark.parametrize(
    ("table", "service", "expected"),
    [
        ("abc123 teatree-worker False\ndef456 teatree-admin False", "teatree-worker", "abc123"),
        ("abc123 teatree-worker False\ndef456 teatree-admin False", "teatree-admin", "def456"),
        ("abc123 teatree-worker False", "teatree-nonesuch", ""),
        # A compose one-off carries the SAME service name and can never be exec'd into.
        ("one111 teatree-worker True\nabc123 teatree-worker False", "teatree-worker", "abc123"),
        ("one111 teatree-worker True", "teatree-worker", ""),
        # An ABSENT oneoff label renders as an empty trailing field. It must be KEPT
        # (only a PROVEN one-off is skipped) — and it must not shift the columns,
        # which is why the always-present ID leads the format string.
        ("abc123 teatree-worker ", "teatree-worker", "abc123"),
        ("abc123 teatree-worker", "teatree-worker", "abc123"),
        ("", "teatree-worker", ""),
    ],
)
def test_running_service_container_reads_the_table(table: str, service: str, expected: str) -> None:
    result = _run_bash(
        PROBE_PREAMBLE,
        f"running_service_container {service}",
        env={"RUNNING_SERVICE_TABLE": table},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


# ── bounded, loud credential reads ────────────────────────────────────

CRED_PREAMBLE = _slice("SECRET_READ_DEADLINE_SECONDS=", "# Everything from here on resolves the credential")


def test_the_credential_preamble_actually_defines_what_these_tests_drive() -> None:
    """Anti-vacuity guard for the slice above.

    An undefined shell function exits 127, which sends every `if` below to its ELSE
    branch — so a slice that missed the definitions made the skip tests pass while
    asserting nothing. This is the check that fails instead.
    """
    result = _run_bash(
        CRED_PREAMBLE,
        """
        for fn in run_with_deadline refuse_wedged_secret_store needs_gitlab_credential; do
            declare -F "$fn" >/dev/null || { echo "MISSING $fn"; exit 1; }
        done
        echo ALL_DEFINED
    """,
    )
    assert result.stdout.strip() == "ALL_DEFINED", result.stderr


def test_a_wedged_store_is_bounded_rather_than_waited_on_forever() -> None:
    """A read that never answers must return the deadline code, not hang."""
    result = _run_bash(
        CRED_PREAMBLE,
        """
        rc=0
        run_with_deadline 1 sleep 30 || rc=$?
        echo "$rc"
    """,
    )
    assert result.stdout.strip() == "124", result.stderr


def test_the_deadline_holds_without_gnu_timeout() -> None:
    """MacOS ships no `timeout(1)`, and that is exactly where this wrapper runs.

    A bound implemented only as `timeout …` would be "command not found" on the
    operator laptop that needs it most, so the portable fallback carries the same
    contract — including passing the read's stdout through untouched.
    """
    result = _run_bash(
        CRED_PREAMBLE,
        """
        PATH=/usr/bin:/bin
        rc=0
        run_with_deadline 1 sleep 30 || rc=$?
        echo "timeout_rc=$rc"
        echo "value=$(run_with_deadline 5 printf ok)"
    """,
    )
    assert "timeout_rc=124" in result.stdout, result.stderr
    assert "value=ok" in result.stdout, result.stderr


def test_a_genuine_absence_stays_distinct_from_a_wedge() -> None:
    """rc=1 means "no such entry" and must continue without a host token."""
    result = _run_bash(
        CRED_PREAMBLE,
        """
        rc=0
        run_with_deadline 5 sh -c 'exit 1' || rc=$?
        echo "$rc"
    """,
    )
    assert result.stdout.strip() == "1", result.stderr


@pytest.mark.parametrize("argv", ["--version", "--help", "-h", ""])
def test_an_invocation_that_runs_no_command_resolves_no_credential(argv: str) -> None:
    """These answer from the binary itself — they reach neither a forge nor the DB."""
    result = _run_bash(CRED_PREAMBLE, f"if needs_gitlab_credential {argv}; then echo NEEDS; else echo SKIP; fi")
    assert result.stdout.strip() == "SKIP", result.stderr


def test_a_real_command_still_resolves_the_credential() -> None:
    """The skip covers only the no-command cases; anything else must be unchanged.

    Deliberately NOT an allowlist of credential-needing commands: such a table rots
    the moment a command grows a forge call, and it rots in the SILENT direction.
    """
    result = _run_bash(
        CRED_PREAMBLE, "if needs_gitlab_credential teatree config_setting get x; then echo NEEDS; else echo SKIP; fi"
    )
    assert result.stdout.strip() == "NEEDS", result.stderr


def test_the_host_lock_probe_observes_the_host_name_when_the_lock_names_a_container(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host" / ".gnupg"
    keys = host_home / "public-keys.d"
    keys.mkdir(parents=True)
    (keys / "pubring.db.lock").write_text("78571\ncontainer-namespace\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hostname = bin_dir / "hostname"
    hostname.write_text("#!/bin/sh\nprintf '%s\\n' host-namespace\n", encoding="utf-8")
    hostname.chmod(0o755)

    result = _run_bash(
        _shell_function("host_keybox_lock_is_clear"),
        "host_keybox_lock_is_clear",
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "SCRIPT_DIR": str(DEPLOY),
            "TEATREE_HOST_HOME": str(tmp_path / "host"),
        },
    )

    assert result.returncode == 1
    assert "container-namespace" in result.stderr
    assert "this host: host-namespace" in result.stderr


def test_the_host_lock_probe_precedes_the_host_credential_read() -> None:
    text = _wrapper_text()
    call = text.index("\n    host_keybox_lock_is_clear\n")
    read = text.index('GITLAB_TOKEN="$(host_gitlab_token_from_pass)"')
    assert call < read


# ── the container prologue: an ABSENT store is not a WEDGED one ───────

_SH = shutil.which("sh") or "/bin/sh"
_HAS_TIMEOUT = shutil.which("timeout") is not None

# `/bin/sh` and not `/usr/bin/env bash`: these run under a PATH holding nothing but the
# stub dir, so a shebang that LOOKS its interpreter up would not resolve one.
_ANSWERING_PASS = """#!/bin/sh
printf '%s\\n' "stub-not-a-credential"
"""
_MISSING_ENTRY_PASS = """#!/bin/sh
echo "Error: $2 is not in the password store." >&2
exit 1
"""
# gpg's own failure rc, reached through `pass`: a locked `pubring.db.lock`, or an orphan
# keyboxd/gpg-agent bound under a different GNUPGHOME.
_WEDGED_PASS = """#!/bin/sh
echo "gpg: decryption failed: No Keybox daemon running" >&2
exit 2
"""
# `exec`, so the deadline's TERM reaches the sleeper itself rather than orphaning a
# child that would hold the read's stdout pipe open past the bound.
_UNANSWERING_PASS = """#!/bin/sh
exec sleep 30
"""

DISPATCH_MARKER = "DISPATCHED"


def _prologue_bin(tmp_path: Path, pass_stub: str | None, *, bounded: bool) -> Path:
    """A hermetic bin dir: no real `pass` leaks in, and `timeout` only when asked for."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    if pass_stub is not None:
        stub = stub_dir / "pass"
        stub.write_text(pass_stub, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for tool in ["head", "sleep", *(["timeout"] if bounded else [])]:
        resolved = shutil.which(tool)
        if resolved is not None:
            (stub_dir / tool).symlink_to(resolved)
    return stub_dir


def _run_prologue(
    tmp_path: Path,
    pass_stub: str | None,
    *,
    bounded: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Evaluate the prologue the way a `compose exec` does, and report what it dispatched."""
    stub_dir = _prologue_bin(tmp_path, pass_stub, bounded=bounded)
    return subprocess.run(
        [
            _SH,
            "-c",
            container_credential_prologue(),
            "t3-in-container",
            _SH,
            "-c",
            f'printf "{DISPATCH_MARKER} token=[%s]" "${{GITLAB_TOKEN:-}}"',
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={
            "PATH": str(stub_dir),
            "TEATREE_GNUPG_RUNTIME_DIR": str(tmp_path / "run"),
            "TEATREE_GITLAB_TOKEN_PASS_PATH": "gitlab/pat",
            **(env or {}),
        },
    )


def _assert_dispatched(proc: subprocess.CompletedProcess[str], token: str) -> None:
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"{DISPATCH_MARKER} token=[{token}]", proc.stderr


def _assert_refused(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode != 0, "a wedged store let the dispatch run on an empty credential"
    assert "WEDGED" in proc.stderr, proc.stderr
    assert DISPATCH_MARKER not in proc.stdout, "the prologue dispatched anyway after refusing"


def test_a_store_that_answers_still_exports_what_it_read(tmp_path: Path) -> None:
    """Anti-vacuity for every fall-through below: the read itself must still work."""
    _assert_dispatched(_run_prologue(tmp_path, _ANSWERING_PASS), "stub-not-a-credential")


def test_no_store_at_all_is_not_a_wedge(tmp_path: Path) -> None:
    """No `pass` binary is CI and the laptop that never set one up, not a fault.

    The read reports 127 there, which is `> 1` — so without the `command -v pass` guard
    the wedge refusal fired on every storeless venue and took the whole CLI with it.
    """
    _assert_dispatched(_run_prologue(tmp_path, None), "")


def test_a_missing_entry_is_not_a_wedge(tmp_path: Path) -> None:
    """An absent entry continues without a host token; it never consults ambient glab."""
    _assert_dispatched(_run_prologue(tmp_path, _MISSING_ENTRY_PASS), "")


def test_no_bounding_tool_is_an_absence_too(tmp_path: Path) -> None:
    """`timeout` is GNU coreutils: always in the deploy image, not on every host.

    Its absence reports the same 127, and calling that a wedge would refuse a venue
    where nothing is wedged. No bounded read is possible, so nothing is read — an
    unbounded one is the unmessaged hang this whole block replaced.
    """
    _assert_dispatched(_run_prologue(tmp_path, _ANSWERING_PASS, bounded=False), "")


@pytest.mark.skipif(not _HAS_TIMEOUT, reason="the wedge is only observable where the read can be bounded")
def test_a_present_but_broken_store_refuses_loudly(tmp_path: Path) -> None:
    """The case the refusal exists for: continuing would hand over an EMPTY credential."""
    _assert_refused(_run_prologue(tmp_path, _WEDGED_PASS))


@pytest.mark.skipif(not _HAS_TIMEOUT, reason="the wedge is only observable where the read can be bounded")
def test_a_store_that_never_answers_refuses_loudly(tmp_path: Path) -> None:
    _assert_refused(_run_prologue(tmp_path, _UNANSWERING_PASS, env={"TEATREE_SECRET_READ_DEADLINE_SECONDS": "1"}))


# ── properties that must not regress ──────────────────────────────────


def test_xtrace_is_disabled_before_any_credential_is_resolved() -> None:
    """`bash -x t3` must not be able to print a secret.

    xtrace prints an assignment's VALUE, so every line that can hold a credential has
    to sit after `set +x`. The mount/layout wiring above it stays traceable, which is
    the part worth tracing.
    """
    text = _wrapper_text()
    set_plus_x = text.index("\nset +x\n")
    for resolved in ('GITLAB_TOKEN="$(host_gitlab_token_from_pass)"',):
        assert text.index(resolved) > set_plus_x, f"{resolved} resolves a credential while xtrace may still be on"


def test_the_wrapper_never_harvests_an_ambient_glab_login() -> None:
    text = _wrapper_text()

    assert "host_gitlab_token()" not in text
    assert "glab auth status --show-token" not in text


def test_the_credential_crosses_as_a_bare_env_name() -> None:
    """`--env NAME=value` would put the token in an argv, which is world-readable."""
    assert '--env "$ENV_NAME"' in _wrapper_text()
    assert '--env "$ENV_NAME=' not in _wrapper_text()


def test_the_wedge_refusal_runs_in_the_parent_shell() -> None:
    """An `exit` inside `$( … )` kills only the subshell.

    Refusing there would leave the caller sailing on with the empty value the refusal
    exists to prevent, so the check must sit at the assignment's call site.
    """
    text = _wrapper_text()
    assert 'refuse_wedged_secret_store "${TEATREE_GITLAB_TOKEN_PASS_PATH:-}"' in text
    # the function itself only ever REPORTS the wedge upward
    body = _slice("host_gitlab_token_from_pass() {", "\n# SKIPPED ENTIRELY")
    assert "refuse_wedged_secret_store" not in body
    assert "return 2" in body


def test_every_dispatch_hop_announces_a_fallback_route() -> None:
    """A route the operator did not ask for must never be taken in silence.

    The routes are not interchangeable — only the worker carries the docker-socket
    group — so a provisioning command dispatched at the admin fails in the daemon's
    own vocabulary, about a socket, with nothing tying that to a worker that is down.
    The main dispatch said so; the browser and forward hops resolved the route
    themselves and said nothing.
    """
    for hop in ["dispatch_then_open_browser", "dispatch_then_run_forward", "dispatch_then_run_host_plan"]:
        assert 'announce_route "$route"' in _shell_function(hop), f"{hop} takes a route without announcing it"


def test_the_announcement_fires_once_and_only_for_a_fallback() -> None:
    body = _slice("announce_route() {", "\n# The route to dispatch at")
    assert '[ "$route" != "$SERVICE" ] || return 0' in body, "the preferred route must announce nothing"
    assert '[ -z "$FALLBACK_ROUTE_ANNOUNCED" ] || return 0' in body, "two hops must not print it twice"


# ── the forced one-off escape ─────────────────────────────────────────

ROUTE_PREAMBLE = (
    _shell_function("running_service_container")
    + "\n"
    + _shell_function("first_running_service")
    + "\nSERVICE=teatree-worker\nCLI_SERVICE_FALLBACKS=teatree-admin\n"
)
_UP_TABLE = "abc123 teatree-worker False\ndef456 teatree-admin False"


def test_the_route_preamble_actually_defines_the_function_under_test() -> None:
    """Anti-vacuity guard — a preamble that defines nothing would pass every case below."""
    result = _run_bash(ROUTE_PREAMBLE, "declare -F first_running_service >/dev/null && echo DEFINED")
    assert result.stdout.strip() == "DEFINED", result.stderr


def test_a_running_stack_is_routed_to_by_default() -> None:
    result = _run_bash(ROUTE_PREAMBLE, "first_running_service", env={"RUNNING_SERVICE_TABLE": _UP_TABLE})
    assert result.returncode == 0
    assert result.stdout.strip() == "teatree-worker"


def test_force_one_off_keeps_route_resolution_distinct_from_dispatch_mode() -> None:
    """A one-off is an operator choice, never evidence that no route is running."""
    result = _run_bash(
        ROUTE_PREAMBLE,
        "first_running_service",
        env={"RUNNING_SERVICE_TABLE": _UP_TABLE, "TEATREE_FORCE_ONE_OFF": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "teatree-worker", result.stdout


def test_the_drift_warning_names_the_per_invocation_escape() -> None:
    """The warning that reports the drift must also name the way out of it."""
    warning = _shell_function("warn_source_mount_drift")
    assert "TEATREE_FORCE_ONE_OFF=1" in warning
