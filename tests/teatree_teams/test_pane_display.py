"""Pane-display presentation layer — tmux split-window rendering (WI-5, #1838).

The PRESENTATION half of a Track-B maker pane. Where the SDK session lives
in-process (``build_pane_options`` is the source of truth for the resolved
session id), the optional display renders that SAME session in a tmux pane via
``tmux split-window -- claude --resume <session_id>``. Under ``tmux -CC`` iTerm2
renders the pane as a native split; elsewhere it degrades to a plain tmux pane.

DEFAULT-OFF + graceful degradation is the load-bearing contract: no tmux, no TTY,
or any spawn failure → ``spawn_pane`` returns ``None`` and the in-process SDK run
is unchanged. These tests pin the detection matrix, the argv, the degradation
fallback, idempotent teardown, and the orphan reconcile.

Pure-logic unit tests with the unstoppable externals mocked: the
``run_allowed_to_fail`` tmux egress wrapper (the tmux child), ``shutil.which``
(the binary probe), and ``os.environ``/``isatty`` (the multiplexer probes).
"""

from dataclasses import dataclass
from unittest import mock

from claude_agent_sdk import ClaudeAgentOptions

from teatree.teams.pane_display import PaneHandle, detect_multiplexer, reconcile_orphan_panes, spawn_pane, teardown_pane


def _options(*, resume: str = "11111111-1111-1111-1111-111111111111") -> ClaudeAgentOptions:
    """A minimal ``ClaudeAgentOptions`` mirroring what ``build_pane_options`` emits."""
    return ClaudeAgentOptions(
        model="claude-sonnet-4-5",
        cwd="/tmp/wt/ticket/repo",
        permission_mode="bypassPermissions",
        resume=resume,
    )


@dataclass(frozen=True)
class _FakePane:
    """A stand-in for ``TeammatePane`` carrying only the fields the display reads."""

    claim_slot: str = "team:core-maker"


class TestDetectMultiplexer:
    def test_tmux_env_set_resolves_tmux(self) -> None:
        with mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-501/default,123,0"}, clear=False):
            assert detect_multiplexer() == "tmux"

    def test_unset_and_no_binary_resolves_none(self) -> None:
        env = {k: v for k, v in __import__("os").environ.items() if k != "TMUX"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("teatree.teams.pane_display.shutil.which", return_value=None),
            mock.patch("teatree.teams.pane_display.sys.stdout.isatty", return_value=True),
        ):
            assert detect_multiplexer() == "none"

    def test_non_tty_resolves_none_even_with_binary(self) -> None:
        env = {k: v for k, v in __import__("os").environ.items() if k != "TMUX"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("teatree.teams.pane_display.shutil.which", return_value="/usr/bin/tmux"),
            mock.patch("teatree.teams.pane_display.sys.stdout.isatty", return_value=False),
        ):
            assert detect_multiplexer() == "none"

    def test_binary_present_and_tty_resolves_tmux(self) -> None:
        # No $TMUX but a tmux binary + a real TTY → a fresh `tmux -CC` attach is
        # plausible, so the display is usable.
        env = {k: v for k, v in __import__("os").environ.items() if k != "TMUX"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("teatree.teams.pane_display.shutil.which", return_value="/usr/bin/tmux"),
            mock.patch("teatree.teams.pane_display.sys.stdout.isatty", return_value=True),
        ):
            assert detect_multiplexer() == "tmux"


class TestSpawnPaneArgv:
    def test_builds_split_window_with_resume_and_title(self) -> None:
        pane = _FakePane(claim_slot="team:core-maker")
        options = _options(resume="abc-session-id")
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            run.return_value = mock.Mock(returncode=0, stdout="%7\n")
            handle = spawn_pane(pane, options)

        assert isinstance(handle, PaneHandle)
        assert handle.pane_id == "%7"
        assert handle.role == "team:core-maker"
        assert handle.session_id == "abc-session-id"

        split_argv = run.call_args_list[0].args[0]
        assert split_argv[0] == "tmux"
        assert "split-window" in split_argv
        # The pane hosts the SAME SDK session: --resume carries the resolved id.
        assert "--resume" in split_argv
        assert "abc-session-id" in split_argv
        assert split_argv[split_argv.index("--resume") + 1] == "abc-session-id"
        # The visible child is interactive `claude`, NOT a metered `claude -p`.
        assert "claude" in split_argv
        assert "-p" not in split_argv
        assert "--model" in split_argv
        assert "claude-sonnet-4-5" in split_argv
        assert "bypassPermissions" in split_argv
        # cwd is passed via -c so the pane starts in the worktree.
        assert "-c" in split_argv
        assert "/tmp/wt/ticket/repo" in split_argv
        # pane_id capture: -P -F '#{pane_id}'.
        assert "-P" in split_argv
        assert "#{pane_id}" in split_argv

        title_argv = run.call_args_list[1].args[0]
        assert title_argv[0] == "tmux"
        assert "select-pane" in title_argv
        assert "-T" in title_argv
        assert "team:core-maker" in title_argv

    def test_returns_none_on_tmux_failure(self) -> None:
        pane = _FakePane()
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            run.return_value = mock.Mock(returncode=1, stdout="")
            assert spawn_pane(pane, _options()) is None

    def test_returns_none_when_tmux_binary_missing(self) -> None:
        pane = _FakePane()
        with mock.patch(
            "teatree.teams.pane_display.run_allowed_to_fail",
            side_effect=FileNotFoundError("tmux"),
        ):
            assert spawn_pane(pane, _options()) is None

    def test_returns_none_when_no_resume_session_id(self) -> None:
        # No SDK session to attach → there is nothing to display; never spawn a
        # detached `claude` with no resume id (it would NOT be the SDK session).
        pane = _FakePane()
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            assert spawn_pane(pane, _options(resume="")) is None
            run.assert_not_called()

    def test_returns_none_when_tmux_prints_no_pane_id(self) -> None:
        # split-window exits 0 but prints nothing (no captured pane id) → degrade.
        pane = _FakePane()
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            run.return_value = mock.Mock(returncode=0, stdout="\n")
            assert spawn_pane(pane, _options()) is None

    def test_omits_model_flag_when_options_has_no_model(self) -> None:
        # A pane whose options carry no model resolution: the argv must NOT carry
        # a dangling --model (the child inherits the user default).
        pane = _FakePane()
        options = ClaudeAgentOptions(
            cwd="/tmp/wt/ticket/repo",
            permission_mode="bypassPermissions",
            resume="s-id",
        )
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            run.return_value = mock.Mock(returncode=0, stdout="%2\n")
            spawn_pane(pane, options)
        split_argv = run.call_args_list[0].args[0]
        assert "--model" not in split_argv
        assert "--resume" in split_argv

    def test_omits_cwd_flag_when_options_has_no_cwd(self) -> None:
        # No cwd resolved → no -c flag (the pane starts wherever tmux split lands).
        pane = _FakePane()
        options = ClaudeAgentOptions(permission_mode="bypassPermissions", resume="s-id")
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            run.return_value = mock.Mock(returncode=0, stdout="%2\n")
            spawn_pane(pane, options)
        split_argv = run.call_args_list[0].args[0]
        assert "-c" not in split_argv

    def test_spawn_succeeds_even_if_titling_is_unavailable(self) -> None:
        # The split lands but select-pane is unavailable (returns None): the pane
        # still exists, so spawn returns its handle (titling is best-effort).
        pane = _FakePane()

        def fake_run(argv, **_k):
            if "split-window" in argv:
                return mock.Mock(returncode=0, stdout="%9\n")
            return None  # select-pane: simulate tmux vanishing mid-spawn.

        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail", side_effect=fake_run):
            handle = spawn_pane(pane, _options())
        assert handle is not None
        assert handle.pane_id == "%9"


class TestTeardownPaneIdempotent:
    def test_kills_pane_by_id(self) -> None:
        handle = PaneHandle(pane_id="%7", role="team:core-maker", session_id="s")
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            teardown_pane(handle)
        argv = run.call_args.args[0]
        assert argv[0] == "tmux"
        assert "kill-pane" in argv
        assert "-t" in argv
        assert "%7" in argv

    def test_missing_pane_does_not_raise(self) -> None:
        handle = PaneHandle(pane_id="%99", role="team:core-maker", session_id="s")
        # tmux exits non-zero ("can't find pane") for an already-gone pane.
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="can't find pane %99")
            teardown_pane(handle)  # idempotent: no raise.

    def test_tmux_binary_missing_does_not_raise(self) -> None:
        handle = PaneHandle(pane_id="%99", role="team:core-maker", session_id="s")
        with mock.patch(
            "teatree.teams.pane_display.run_allowed_to_fail",
            side_effect=FileNotFoundError("tmux"),
        ):
            teardown_pane(handle)  # idempotent: no raise.


class TestReconcileOrphanPanes:
    def test_kills_team_titled_panes_with_no_live_claim(self) -> None:
        # list-panes returns two team:* panes; only one slot is live → the other
        # is orphaned and reaped.
        list_out = "%3 team:core-maker\n%4 team:overlay-maker\n%5 someones-shell\n"
        calls: list[list[str]] = []

        def fake_run(argv, *_a, **_k):
            calls.append(argv)
            if "list-panes" in argv:
                return mock.Mock(returncode=0, stdout=list_out)
            return mock.Mock(returncode=0, stdout="")

        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail", side_effect=fake_run):
            killed = reconcile_orphan_panes(live_claim_slots={"team:core-maker"})

        # The live slot is kept; the orphaned team pane is killed; the plain
        # shell pane is never touched (only team:* titled panes are candidates).
        assert killed == ["%4"]
        kill_calls = [c for c in calls if "kill-pane" in c]
        assert len(kill_calls) == 1
        assert "%4" in kill_calls[0]

    def test_no_tmux_is_a_noop(self) -> None:
        with mock.patch(
            "teatree.teams.pane_display.run_allowed_to_fail",
            side_effect=FileNotFoundError("tmux"),
        ):
            assert reconcile_orphan_panes(live_claim_slots=set()) == []

    def test_empty_pane_list_is_a_noop(self) -> None:
        with mock.patch("teatree.teams.pane_display.run_allowed_to_fail") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            assert reconcile_orphan_panes(live_claim_slots=set()) == []
