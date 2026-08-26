# test-path: cross-cutting — drives deploy/peer-forward.sh and deploy/t3 (no src mirror).
"""The host side of ``t3 peer`` — the ONE place a peer's forward is opened, reused or refused.

It executes on the operator's own machine because that is where the transport binaries and
their credentials are. The runner is driven here as a real bash program against real
listeners on throwaway ports, because its whole job is to decide what a port's holder means
and that decision is only true if the probe it makes is the one the shell actually runs.

It knows nothing about any box: the peer's label, its port and the whole command that opens
its forward all arrive in a plan file the CLI resolved from the registry. Nothing below
names a host, a project or a zone, and that is the property, not an accident of fixtures.
"""

import shutil
import socket
import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from teatree.core.peer_forward import PLAN_FILE, RUNNER

_BASH = shutil.which("bash") or "/bin/bash"
_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ROOT / RUNNER
_WRAPPER = _ROOT / "deploy" / "t3"


@pytest.fixture(scope="module")
def runner_source() -> str:
    return _RUNNER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def wrapper() -> str:
    return _WRAPPER.read_text(encoding="utf-8")


@pytest.fixture
def held_port() -> Iterator[int]:
    """A port this test process itself holds — a listener that opens no forward at all."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock.getsockname()[1]


def _ask(predicate: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """One predicate out of the runner, sourced rather than reimplemented in the test."""
    passed = " ".join(repr(argument) for argument in arguments)
    script = textwrap.dedent(f"""
        source {_RUNNER}
        {predicate} {passed}
    """)
    return subprocess.run([_BASH, "-c", script], capture_output=True, text=True, check=False)


_SSH_COMMAND = "ssh -N -L 9401:127.0.0.1:8000 box-b"
_GCLOUD_COMMAND = "gcloud compute ssh box-c --project p --zone z --tunnel-through-iap -- -N -L 9402:127.0.0.1:8000"


class TestOnlyATransportThatOpensForwardsMayBeAdopted:
    @pytest.mark.parametrize("holder", ["ssh", "gcloud"])
    @pytest.mark.parametrize("command", [_SSH_COMMAND, _GCLOUD_COMMAND])
    def test_a_transport_teatree_opens_forwards_with_is_adoptable(self, holder: str, command: str) -> None:
        assert _ask("is_tunnel", holder, command).returncode == 0

    @pytest.mark.parametrize("holder", ["node", "nginx", "Google", "postgres", "docker"])
    @pytest.mark.parametrize("command", [_SSH_COMMAND, _GCLOUD_COMMAND])
    def test_anything_else_holding_the_port_is_not_adoptable(self, holder: str, command: str) -> None:
        assert _ask("is_tunnel", holder, command).returncode != 0


class TestThePythonAllowanceIsScopedToThePeerThatEarnsIt:
    """gcloud IS a python program, so lsof can name the interpreter instead of the wrapper.

    Blanket, that allowance adopted ANY python listener: a local runserver on a peer's port
    was "reused", and the compare page then read THIS box under that peer's label. So it is
    read off the peer's own command, which is the only place the transport is actually known.
    """

    @pytest.mark.parametrize("holder", ["python", "python3.13"])
    def test_a_gcloud_peer_may_adopt_the_interpreter_that_runs_it(self, holder: str) -> None:
        assert _ask("is_tunnel", holder, _GCLOUD_COMMAND).returncode == 0

    @pytest.mark.parametrize("holder", ["python", "python3.13"])
    def test_an_ssh_peer_may_not(self, holder: str) -> None:
        assert _ask("is_tunnel", holder, _SSH_COMMAND).returncode != 0

    @pytest.mark.parametrize("holder", ["python", "python3.13"])
    def test_and_neither_may_a_peer_whose_command_is_unknown(self, holder: str) -> None:
        assert _ask("is_tunnel", holder, "").returncode != 0

    def test_the_transport_is_the_commands_first_word_however_it_is_pathed(self) -> None:
        assert _ask("is_tunnel", "python3.13", "/opt/homebrew/bin/gcloud compute ssh box-c").returncode == 0


def _forward_up(peer: str, port: int, command: str, *, holder: str) -> subprocess.CompletedProcess[str]:
    """``forward_up`` against a live listener *holder* names, which lsof is the only source of.

    Which process holds a port is the one thing the runner cannot decide for itself, so it is
    the one thing stubbed; the decision it draws from that answer is what is under test.
    """
    script = textwrap.dedent(f"""
        source {_RUNNER}
        STATE_DIR=$(mktemp -d)
        WAIT_SECONDS=1
        holder_of() {{ echo {holder!r}; }}
        forward_up {peer!r} {port!r} {command!r}
    """)
    return subprocess.run([_BASH, "-c", script], capture_output=True, text=True, check=False)


class TestAForeignListenerIsRefusedRatherThanReadThrough:
    def test_the_refusal_says_the_read_would_reach_that_process_instead(self, held_port: int) -> None:
        result = _forward_up("box-b", held_port, "true", holder="nginx")

        assert result.returncode != 0
        assert "instead of the box" in result.stderr

    def test_the_refusal_names_the_peer_and_never_its_resolved_host(self, held_port: int) -> None:
        command = "ssh -N -o ExitOnForwardFailure=yes -L 1:127.0.0.1:8000 box-b.example.invalid"

        result = _forward_up("box-b", held_port, command, holder="nginx")

        assert "box-b" in result.stderr
        assert "box-b.example.invalid" not in result.stderr


class TestAHolderThatCannotBeNamedIsRefusedRatherThanAdopted:
    """lsof answers nothing for a port held by another user, and when it is absent entirely."""

    def test_an_unnameable_holder_is_refused(self, held_port: int) -> None:
        # Adopting it reads the peer through whatever that is. "I could not identify it" is
        # not "it is a tunnel", and treating the two alike is fail-open on the whole check.
        result = _forward_up("box-b", held_port, "true", holder="")

        assert result.returncode != 0

    def test_the_refusal_says_the_holder_could_not_be_identified(self, held_port: int) -> None:
        assert "could not identify" in _forward_up("box-b", held_port, "true", holder="").stderr


class TestALiveForwardIsReusedRatherThanDuplicated:
    def test_a_port_a_transport_already_holds_is_adopted(self, held_port: int) -> None:
        # Spawning a second forward onto a port one already holds is what leaves an operator
        # with a stray process they never asked for and cannot account for.
        result = _forward_up("box-b", held_port, "false", holder="gcloud")

        assert result.returncode == 0
        assert "reusing it" in result.stdout


class TestAForwardThatNeverComesUpFailsLoud:
    def test_a_command_that_opens_nothing_is_reported_not_returned_from(self, tmp_path: Path) -> None:
        # Returning before the forward is live is the fail-open this runner exists to close.
        plan = tmp_path / PLAN_FILE
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            dead_port = sock.getsockname()[1]
        plan.write_text(f"action=up\nwait_seconds=1\nbox-b\t{dead_port}\ttrue\n", encoding="utf-8")

        result = subprocess.run([_BASH, str(_RUNNER), str(plan)], capture_output=True, text=True, check=False)

        assert result.returncode != 0
        assert "did not come up" in result.stderr


class TestOneTransportsStdinNeverSwallowsThePeersBelowIt:
    """The plan file IS the loop's stdin, so a forward that reads stdin reads the plan.

    A transport that prompts (gcloud does) then consumes the rows beneath its own and the loop
    sees EOF: the peers below it are never attempted, and the run exits naming only the first.
    Silently descoping the rest of the plan is worse than failing on it, because the operator
    is told about one peer and believes they were told about all of them.
    """

    _PEERS = ("reads-stdin", "second", "third")

    @staticmethod
    def _dead_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def test_every_peer_in_the_plan_is_still_reported_on(self, tmp_path: Path) -> None:
        # `cat` is the smallest honest stand-in for a prompting transport: it drains whatever
        # stdin it inherits, which is precisely what a shared fd 0 hands it.
        commands = ("cat", "true", "true")
        rows = [f"{peer}\t{self._dead_port()}\t{command}" for peer, command in zip(self._PEERS, commands, strict=True)]
        plan = tmp_path / PLAN_FILE
        plan.write_text("action=up\nwait_seconds=1\n" + "".join(f"{row}\n" for row in rows), encoding="utf-8")

        result = subprocess.run([_BASH, str(_RUNNER), str(plan)], capture_output=True, text=True, check=False)

        reported = result.stdout + result.stderr
        assert [peer for peer in self._PEERS if peer in reported] == list(self._PEERS)


class TestOnlyAForwardTeatreeOpenedMayBeClosed:
    def test_closing_a_port_teatree_did_not_open_is_refused(self, tmp_path: Path, held_port: int) -> None:
        plan = tmp_path / PLAN_FILE
        plan.write_text(f"action=down\nwait_seconds=1\nbox-b\t{held_port}\ttrue\n", encoding="utf-8")

        result = subprocess.run([_BASH, str(_RUNNER), str(plan)], capture_output=True, text=True, check=False)

        assert result.returncode != 0
        assert "teatree did not open it" in result.stderr


class TestTheRunnerCarriesNoCoordinateOfItsOwn:
    def test_every_coordinate_arrives_in_the_plan(self, runner_source: str) -> None:
        # The whole reason the plan file exists: a host, a project, a zone or a key path in
        # this file would be in the repository, and the registry is where they belong.
        assert "--project" not in runner_source
        assert "--zone" not in runner_source
        assert "-L " not in runner_source


class TestTheWrapperHandsTheForwardVerbsToTheHost:
    @pytest.mark.parametrize("verb", ["up", "down", "status", "open"])
    def test_each_forward_verb_takes_the_host_hop(self, wrapper: str, verb: str) -> None:
        routed = wrapper.split("wants_host_forward()", 1)[1].split("\n}\n", 1)[0]
        assert verb in routed

    def test_the_wrapper_reads_the_plan_file_the_cli_writes(self, wrapper: str) -> None:
        assert PLAN_FILE in wrapper

    def test_the_wrapper_runs_the_runner_the_cli_names(self, wrapper: str) -> None:
        assert Path(RUNNER).name in wrapper
