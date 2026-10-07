"""Per-phase tool least-privilege at the headless harness (PR-11).

``_build_options`` injects the per-phase disallow list on the ``ClaudeAgentOptions``
it hands the SDK. Two guarantees are pinned here. First, the external-contact floor:
the interactive/external-effect built-ins (``AskUserQuestion``/``PushNotification``/
``RemoteTrigger``/``SendMessage``/``Monitor``) are denied on EVERY headless phase, so
no headless agent can reach the user or an external endpoint via a built-in — the only
sanctioned user-contact path is ``needs_user_input`` → DeferredQuestion → Slack.
Second, the per-phase complement: a verdict-producing review-phase dispatch keeps the
shell it needs to cold-check-out the head and record its verdict, and is UNABLE to
mutate source (``Write``/``Edit``), while a work phase's capability tools
(Read/Write/Edit/Bash) are untouched by the floor. Third, the teatree MCP server: only
a phase holding ``mcp_write`` launches it with its write tools, and no other server loads.
"""

from django.test import TestCase

from teatree.agents._runner_options import _EXTERNAL_CONTACT_BUILTINS, _build_options, _disallowed_tools_for_phase
from teatree.core.modelkit.phase_tools import VERDICT_REVIEW_PHASES
from teatree.core.models import Session, Task, Ticket
from teatree.llm.builtin_tools import KNOWN_BUILTIN_TOOLS


class TestDisallowedToolsForPhase(TestCase):
    def test_askuserquestion_always_denied(self) -> None:
        for phase in ("coding", "reviewing", "planning", "shipping"):
            assert "AskUserQuestion" in _disallowed_tools_for_phase(phase), phase

    def test_external_contact_builtins_denied_on_every_headless_phase(self) -> None:
        # No headless phase — work, planning, review, shipping, debugging — can reach
        # the user or an external endpoint via a built-in.
        for phase in ("coding", "testing", "planning", "shipping", "reviewing", "debugging", "e2e"):
            disallowed = set(_disallowed_tools_for_phase(phase))
            missing = set(_EXTERNAL_CONTACT_BUILTINS) - disallowed
            assert not missing, (phase, missing)

    def test_floor_is_a_valid_subset_of_the_builtin_registry(self) -> None:
        # Every floor name must be a real CLI built-in or the CLI rejects it as a
        # deny rule ("matches no known tool").
        assert set(_EXTERNAL_CONTACT_BUILTINS) <= set(KNOWN_BUILTIN_TOOLS)
        assert {"AskUserQuestion", "PushNotification", "RemoteTrigger", "SendMessage", "Monitor"} == set(
            _EXTERNAL_CONTACT_BUILTINS
        )

    def test_review_phase_keeps_shell_but_denies_file_mutation(self) -> None:
        disallowed = set(_disallowed_tools_for_phase("reviewing"))
        # F4: the reviewer keeps the shell (Bash) to run the cold-review checkout,
        # verify-gates, and post the verdict; it never mutates source (Write/Edit).
        assert {"Write", "Edit"} <= disallowed
        assert "Bash" not in disallowed

    def test_write_phase_denies_exactly_the_external_contact_floor(self) -> None:
        # A full-access phase adds nothing beyond the floor, and the floor never
        # touches its capability tools (Read/Write/Edit/Bash stay granted).
        for phase in ("coding", "testing"):
            disallowed = _disallowed_tools_for_phase(phase)
            assert disallowed == sorted(_EXTERNAL_CONTACT_BUILTINS), phase
            assert {"Read", "Write", "Edit", "Bash"}.isdisjoint(disallowed), phase

    def test_sorted_and_deduplicated(self) -> None:
        result = _disallowed_tools_for_phase("reviewing")
        assert list(result) == sorted(result)
        assert len(result) == len(set(result))


class TestBuildOptionsHarnessPin(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options_for(self, phase: str):
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        return _build_options(task, "ctx", phase=phase, skills=[])

    def test_review_dispatch_keeps_shell_but_denies_file_mutation(self) -> None:
        options = self._options_for("reviewing")
        # The reviewer CAN shell out (checkout/verify-gates/post) but never writes.
        assert "Bash" not in options.disallowed_tools
        assert "Write" in options.disallowed_tools
        assert "Edit" in options.disallowed_tools

    def test_review_dispatch_reaches_the_teatree_mcp_reads_but_not_its_writes(self) -> None:
        # A reviewer records its verdict through the envelope or the shell, so its
        # teatree server is the read-only one; the reads are never name-denied.
        options = self._options_for("reviewing")
        assert options.setting_sources is None
        assert list(options.mcp_servers["teatree"]["args"]) == ["mcp", "serve", "--read-only"]
        assert "mcp__teatree__github_pr_diff" not in options.disallowed_tools

    def test_every_verdict_review_dispatch_comes_up_with_a_shell(self) -> None:
        # End-to-end regression for the live outage: two auto-dispatched
        # `codex_adversarial_reviewing` runs came up with Bash denied, could not
        # check out the head or run `t3 tool verify-gates`, and filed deferred
        # questions ("no Bash, Write, Edit, or git/gh access") instead of a
        # verdict. Assert the whole option build, not just the table, since the
        # dispatch is what the agent actually receives.
        for phase in sorted(VERDICT_REVIEW_PHASES):
            disallowed = self._options_for(phase).disallowed_tools
            assert "Bash" not in disallowed, phase
            assert "Write" in disallowed, phase
            assert "Edit" in disallowed, phase

    def test_requesting_review_dispatch_cannot_invoke_git_write(self) -> None:
        # Control: the non-verdict review phase is still shell-denied at dispatch.
        assert "Bash" in self._options_for("requesting_review").disallowed_tools

    def test_coding_dispatch_keeps_full_shell_access(self) -> None:
        options = self._options_for("coding")
        assert "Bash" not in options.disallowed_tools
        assert "Write" not in options.disallowed_tools
        assert "Read" not in options.disallowed_tools
        assert options.disallowed_tools == sorted(_EXTERNAL_CONTACT_BUILTINS)

    def test_work_dispatch_denies_the_external_contact_builtins(self) -> None:
        # The live gap this closes: a work-phase dispatch used to carry only the
        # AskUserQuestion floor, leaving PushNotification/RemoteTrigger/SendMessage/
        # Monitor reachable so a headless agent could contact the user directly.
        disallowed = set(self._options_for("coding").disallowed_tools)
        assert {"AskUserQuestion", "PushNotification", "RemoteTrigger", "SendMessage", "Monitor"} <= disallowed

    def test_lifecycle_dispatch_wires_the_teatree_mcp_server(self) -> None:
        # #3242: plugin sub-agents ignore the mcpServers frontmatter, so the
        # headless lifecycle dispatch must inject the teatree local-stdio server
        # itself — otherwise coder/reviewer/shipper come up without mcp__teatree__*
        # and fall back to shelling out to the CLI for every structured read.
        for phase in ("coding", "reviewing", "testing", "shipping"):
            server = self._options_for(phase).mcp_servers.get("teatree")
            assert server is not None, phase
            assert server["command"] == "t3"
            assert list(server["args"])[:2] == ["mcp", "serve"]


class TestTeatreeMcpLeastPrivilege(TestCase):
    """A phase without the ``mcp_write`` capability launches the read-only teatree server."""

    READ_ONLY_PHASES = (
        "scanning_news",
        "triage_assessing",
        "answering",
        "directive_interpreting",
        "critic_reviewing",
        "reviewing",
        "codex_reviewing",
        "codex_adversarial_reviewing",
        "e2e_reviewing",
        "planning",
        "scoping",
        "bughunt",
        "dogfood_smoke",
        "eval_local",
        "backlog_sweep",
        "retro",
        "no-such-phase",
    )
    GRANTED_PHASES = ("coding", "testing", "e2e", "debugging", "shipping", "architectural_review")

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options_for(self, phase: str):
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        return _build_options(task, "ctx", phase=phase, skills=[])

    def test_a_read_only_phase_launches_the_read_only_server(self) -> None:
        for phase in self.READ_ONLY_PHASES:
            with self.subTest(phase=phase):
                options = self._options_for(phase)
                assert list(options.mcp_servers["teatree"]["args"]) == ["mcp", "serve", "--read-only"]

    def test_requesting_review_launches_the_read_only_server_plus_its_post_tool(self) -> None:
        args = list(self._options_for("requesting_review").mcp_servers["teatree"]["args"])
        assert args == ["mcp", "serve", "--read-only", "--allow-write", "review_request_post"]

    def test_a_granted_phase_launches_the_full_server(self) -> None:
        for phase in self.GRANTED_PHASES:
            with self.subTest(phase=phase):
                assert list(self._options_for(phase).mcp_servers["teatree"]["args"]) == ["mcp", "serve"]

    def test_every_headless_spawn_loads_only_the_injected_server(self) -> None:
        for phase in (*self.READ_ONLY_PHASES, "requesting_review", *self.GRANTED_PHASES):
            with self.subTest(phase=phase):
                assert self._options_for(phase).strict_mcp_config is True


class TestWritePermissionMode(TestCase):
    """Active work phases keep unattended write permission."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options_for_phase(self, phase: str) -> object:
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        return _build_options(task, "ctx", phase=phase, skills=[])

    def test_write_phases_keep_bypass_so_they_can_act_unattended(self) -> None:
        # A detached write run has no human to grant permissions; downgrading these
        # to dontAsk would deny every edit and silently strand the factory.
        for phase in ("coding", "planning", "shipping", "reviewing"):
            assert self._options_for_phase(phase).permission_mode == "bypassPermissions", phase
