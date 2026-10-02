"""A hand-off is attributed to its AUTHOR, never to whoever owns the loop tick (#4479).

``current_session_id()``'s last resort is the loop registry's ``t3-loop-tick-owner``
record. That fallback answers "which principal holds the t3-master lease", and Claude
Code delivers a session id only in the hook JSON payload — so every session running
``handover create`` from a Bash-tool subprocess fell through to it and authored under
the tick owner's name. An author holds at most one unclaimed row, so three sessions'
state absorbed into one row behind fences and the single-slot invariant bound unrelated
sessions together.

The refusal these tests demand was already written and simply unreachable: the command
refuses an empty author id, and the registry made the id never empty. So the carve-out
is a narrower SOURCE, not a new gate — and the registry fallback stays exactly where its
legitimate consumer reads it, which the loop-principal test at the bottom pins.
"""

import json
import os
import pathlib
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import SessionHandover
from teatree.core.session_identity import SESSION_ID_ENV_VARS, current_session_id, loop_principal
from teatree.utils.env import patched_environ

_TICK_OWNER = "3605e428-3f54-4843-ab40-80e9e9646b5d"
_AUTHOR = "969fa1ae-cb35-42fb-8007-955f7a1bee93"
_AUTHORED = "# Hand-off\n\nSTANDING CONSTRAINT: never force-push.\n"


class _ForeignTickOwnerCase(TestCase):
    """A registry naming ANOTHER live session, with no session id in this process's env."""

    def setUp(self) -> None:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_path = pathlib.Path(tmp_dir.name)
        registry = self.tmp_path / "registry"
        registry.mkdir()
        (registry / "loop-registry.json").write_text(
            json.dumps({"t3-loop-tick-owner": {"session_id": _TICK_OWNER, "pid": os.getpid()}}),
            encoding="utf-8",
        )
        self.enterContext(
            patched_environ(
                {
                    "T3_LOOP_REGISTRY_DIR": str(registry),
                    "TEATREE_CLAUDE_STATUSLINE_STATE_DIR": str(self.tmp_path / "state"),
                    "XDG_DATA_HOME": str(self.tmp_path / "xdg"),
                },
                remove=SESSION_ID_ENV_VARS,
            )
        )
        os.environ.pop("T3_DATA_DIR", None)
        (self.tmp_path / "state").mkdir(parents=True, exist_ok=True)
        driver = mock.patch("teatree.core.management.commands.handover.drive_subagents_to_fast_push", return_value=[])
        driver.start()
        self.addCleanup(driver.stop)

    @staticmethod
    def _run(*args: str, **kwargs) -> tuple[dict, str, int]:
        out, err = StringIO(), StringIO()
        code = 0
        try:
            call_command("handover", *args, stdout=out, stderr=err, json_output=True, **kwargs)
        except SystemExit as exc:
            code = int(exc.code or 0)
        return json.loads(out.getvalue() or "{}"), err.getvalue(), code


class TestCreateRefusesRatherThanAuthoringUnderTheTickOwner(_ForeignTickOwnerCase):
    def test_it_refuses_loudly(self) -> None:
        data, err, code = self._run("create", body=_AUTHORED, to="other-session", drive_subagents=False)
        assert code == 2
        assert data["ok"] is False
        assert "session id" in data["error"]
        assert "ERROR" in err

    def test_no_row_is_written_under_the_tick_owners_name(self) -> None:
        self._run("create", body=_AUTHORED, to="other-session", drive_subagents=False)
        assert SessionHandover.objects.count() == 0

    def test_the_refusal_names_the_env_vars_that_would_resolve_it(self) -> None:
        """A refusal the operator cannot act on is a wedge, not a guard."""
        data, _err, _code = self._run("create", body=_AUTHORED, drive_subagents=False)
        assert all(name in data["error"] for name in SESSION_ID_ENV_VARS)


class TestTheEnvSessionAuthorsEvenBesideAForeignRegistry(_ForeignTickOwnerCase):
    """The positive criterion: a resolvable author still hands off, under its OWN id."""

    def test_the_row_carries_the_env_session_not_the_tick_owner(self) -> None:
        with patched_environ({SESSION_ID_ENV_VARS[0]: _AUTHOR}):
            data, _err, code = self._run("create", body=_AUTHORED, to="other-session", drive_subagents=False)
        assert code == 0
        assert data["from_session"] == _AUTHOR
        assert SessionHandover.objects.get().from_session == _AUTHOR

    def test_whoami_reports_the_env_session(self) -> None:
        with patched_environ({SESSION_ID_ENV_VARS[0]: _AUTHOR}):
            data, _err, _code = self._run("whoami")
        assert data["session_id"] == _AUTHOR


class TestWhoamiAndClaimDoNotBorrowTheTickOwnersIdentity(_ForeignTickOwnerCase):
    def test_whoami_reports_nothing_rather_than_the_tick_owner(self) -> None:
        data, _err, _code = self._run("whoami")
        assert data["session_id"] == ""

    def test_claim_on_start_does_not_drain_the_tick_owners_inbox(self) -> None:
        """Claiming under a borrowed id consumes a hand-off addressed to somebody else."""
        SessionHandover.objects.create(from_session="some-author", to_session=_TICK_OWNER, payload="THEIR STATE")
        data, _err, _code = self._run("claim-on-start")
        assert data["claimed"] is False
        assert SessionHandover.objects.get().claimed_at is None


class TestTheRegistryFallbackStaysForItsLegitimateConsumer(_ForeignTickOwnerCase):
    """The t3-master lease principal is exactly what the fallback was built to answer."""

    def test_current_session_id_still_resolves_the_registry(self) -> None:
        assert current_session_id() == _TICK_OWNER

    def test_loop_principal_still_resolves_the_registry(self) -> None:
        assert loop_principal() == (_TICK_OWNER, os.getpid())
