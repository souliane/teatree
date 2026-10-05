"""``t3 <overlay> ticket attach-gaps`` — the sweep folds pending dream gaps into an existing host."""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConfigSetting, Ticket, TicketSweepRun
from tests.teatree_loops.dream._own_umbrella import claims_self, ours

pytestmark = pytest.mark.usefixtures("configured_banned_term_registry")

UMBRELLA = "https://gitlab.com/o/factory/-/work_items/249"


def _forge() -> CodeHostBackend:
    state = {"body": "## Host\n"}

    def _update(**kwargs: object) -> dict[str, int]:
        state["body"] = str(kwargs["body"])
        return {"iid": 56}

    forge = claims_self(MagicMock(spec=CodeHostBackend))
    forge.get_issue.side_effect = lambda *_a, **_k: ours({"description": state["body"]})
    forge.update_issue.side_effect = _update
    forge.repo_for_issue_url.return_value = "o/factory"
    return forge


def _attach(host: Ticket, manifest: str, *options: str) -> tuple[int, str]:
    out, err = StringIO(), StringIO()
    try:
        call_command("ticket", "attach-gaps", str(host.pk), "--manifest", manifest, *options, stdout=out, stderr=err)
    except SystemExit as exc:
        return int(exc.code or 0), out.getvalue() + err.getvalue()
    return 0, out.getvalue()


class TestAttachGapsCommand(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("send_proxy_allowlist", ["gitlab:o/factory"])
        Ticket.objects.create(issue_url=UMBRELLA, extra={"dream_gap_pending": [{"gap_key": "g1", "title": "Fix g1"}]})
        for patcher in (
            patch(
                "teatree.core.models.dream_gap_ledger.get_effective_settings",
                return_value=UserSettings(dream_umbrella_url=UMBRELLA),
            ),
            patch("teatree.core.management.commands._close_commands.code_host_for", return_value=_forge()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_manifest_of_pending_gaps_is_attached(self) -> None:
        host = Ticket.objects.create(
            issue_url="https://gitlab.com/o/factory/-/work_items/56", state=Ticket.State.PLAN_RECORDED
        )

        code, out = _attach(host, json.dumps([{"gap_key": "g1", "theme": "gates"}]))

        host.refresh_from_db()
        assert code == 0
        assert "attached 1 gap(s)" in out
        assert host.extra["dream_gap_batch"][0]["gap_key"] == "g1"

    def test_the_attach_is_counted_against_the_named_sweep_run(self) -> None:
        host = Ticket.objects.create(
            issue_url="https://gitlab.com/o/factory/-/work_items/56", state=Ticket.State.PLAN_RECORDED
        )
        run = TicketSweepRun.objects.begin(source="loop")

        assert _attach(host, '["g1"]', "--sweep-run-id", run.run_id)[0] == 0

        run.refresh_from_db()
        assert run.changed_urls == [host.issue_url]

    def test_bare_gap_keys_are_accepted(self) -> None:
        host = Ticket.objects.create(
            issue_url="https://gitlab.com/o/factory/-/work_items/56", state=Ticket.State.PLAN_RECORDED
        )

        assert _attach(host, '["g1"]')[0] == 0

    def test_a_malformed_or_unknown_manifest_exits_one_and_attaches_nothing(self) -> None:
        host = Ticket.objects.create(issue_url="https://gitlab.com/o/factory/-/work_items/56")
        for manifest in ("not json", '{"gap_key": "g1"}', "[]", '["zz"]'):
            with self.subTest(manifest=manifest):
                code, _out = _attach(host, manifest)

                host.refresh_from_db()
                assert code == 1
                assert "dream_gap_batch" not in (host.extra or {})

    def test_an_unknown_host_exits_one(self) -> None:
        host = Ticket(pk=9999)

        assert _attach(host, '["g1"]')[0] == 1
