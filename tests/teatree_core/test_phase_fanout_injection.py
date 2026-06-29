"""Route-invariance + injection for selective per-phase fan-out (teatree#2229).

The load-bearing test is route-invariance against the **real interactive
composer** ``loop_dispatch._task_to_dict`` (surfaced via ``pending-spawn
--json``) — NOT ``build_task_prompt`` (the false-green trap: it feeds only the
disabled headless route, so a directive that leaks there but misses the
interactive payload would pass vacuously).

Anti-vacuous spine: with no ``[agent.phase_fanout]`` opt-in the dispatch payload
carries ``fanout_directive == ""`` (byte-identical to today). The PRESENT tests
prove the opt-in renders the directive; the headless parity tests prove
``build_system_context`` carries the same directive so switching ``agent_runtime``
between interactive and a headless runtime does not lose it. (Under #2650 the
``/loop`` body just runs ``t3 loops tick --loop <name>``, so the directive is
threaded by the dispatch code path — the ``claim-next`` payload + the headless
composer below — not by ``/loop`` slot prose; the legacy fat-``/loop`` slot-prose
append, and its literal test, retired with the dedicated-loop slot generator in
LOOP-PR-A.)
"""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.agents.prompt import build_system_context
from teatree.core.models import Task, Ticket
from teatree.core.models.ticket import schedule_external_review


def _config(body: str) -> Path:
    cfg = Path(tempfile.mkdtemp()) / ".teatree.toml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


class _FanoutDispatchTest(TestCase):
    def setUp(self) -> None:
        # Blocker 1 (hermetic default-OFF spine): ``config_agent`` value-binds
        # ``CONFIG_PATH`` at import, so the autouse ``_isolate_teatree_config``
        # fixture (which patches ``teatree.config.CONFIG_PATH``) does NOT reach
        # the resolver here. Without this pin the absent-opt-in tests would read
        # the developer's real ``~/.teatree.toml`` and turn red the moment they
        # opt a pair in. Pin to an empty config; opt-in tests override it via
        # ``_entry_with_config`` / ``_context``.
        super().setUp()
        empty = Path(tempfile.mkdtemp()) / ".teatree.toml"
        empty.write_text("[teatree]\n", encoding="utf-8")
        patcher = patch("teatree.config_agent.CONFIG_PATH", empty)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _reviewer_task(self, *, url: str = "https://example.com/pr/1") -> Task:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url=url,
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": "x"},
        )
        return schedule_external_review(ticket)

    def _planning_task(self, *, url: str = "https://example.com/issues/9") -> Task:
        ticket = Ticket.objects.create(overlay="acme", issue_url=url, role=Ticket.Role.AUTHOR)
        return ticket.schedule_planning()

    def _coding_task(self, *, url: str = "https://example.com/issues/7") -> Task:
        ticket = Ticket.objects.create(overlay="acme", issue_url=url, role=Ticket.Role.AUTHOR)
        return ticket.schedule_coding()

    def _entry(self) -> dict:
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)
        return json.loads(stdout.getvalue())[0]

    def _entry_with_config(self, cfg: Path) -> dict:
        with patch("teatree.config_agent.CONFIG_PATH", cfg):
            return self._entry()


class TestRouteInvarianceAgainstRealComposer(_FanoutDispatchTest):
    """The DEFAULT route is the interactive composer ``_task_to_dict``.

    These assert against the payload ``pending-spawn``/``claim-next`` emit (the
    real composer), the exact phases the false-green design pass missed.
    """

    def test_payload_always_carries_a_fanout_directive_key(self) -> None:
        # The key is part of the dispatch contract — the slot reads it.
        self._reviewer_task()
        assert "fanout_directive" in self._entry()

    def test_absent_opt_in_renders_empty_directive_for_reviewing(self) -> None:
        # THE anti-vacuous spine: default-OFF → empty string → byte-identical.
        self._reviewer_task()
        assert self._entry()["fanout_directive"] == ""

    def test_absent_opt_in_renders_empty_directive_for_planning(self) -> None:
        self._planning_task()
        assert self._entry()["fanout_directive"] == ""

    def test_opt_in_renders_directive_for_reviewing(self) -> None:
        self._reviewer_task()
        cfg = _config('[agent.phase_fanout]\n"reviewer:reviewing" = true\n')
        directive = self._entry_with_config(cfg)["fanout_directive"]
        assert directive != ""
        assert "adversarial-verify" in directive
        assert "N=3" in directive  # registry default width

    def test_opt_in_renders_directive_for_planning(self) -> None:
        self._planning_task()
        cfg = _config('[agent.phase_fanout]\n"author:planning" = true\n')
        directive = self._entry_with_config(cfg)["fanout_directive"]
        assert directive != ""
        assert "judge-panel" in directive
        assert "N=3" in directive

    def test_int_opt_in_renders_overridden_width(self) -> None:
        self._planning_task()
        cfg = _config('[agent.phase_fanout]\n"author:planning" = 5\n')
        directive = self._entry_with_config(cfg)["fanout_directive"]
        assert "N=5" in directive
        assert "N=3" not in directive

    def test_opt_in_on_a_different_pair_does_not_leak_to_this_phase(self) -> None:
        # Opting in planning must NOT render a directive on a reviewing dispatch.
        self._reviewer_task()
        cfg = _config('[agent.phase_fanout]\n"author:planning" = true\n')
        assert self._entry_with_config(cfg)["fanout_directive"] == ""

    def test_no_fanout_registered_phase_stays_empty_even_when_opted_in(self) -> None:
        # coding has no FANOUT_BY_PHASE entry → directive empty regardless of a
        # (meaningless) opt-in key, proving the registry gates the render.
        self._coding_task()
        cfg = _config('[agent.phase_fanout]\n"author:coding" = true\n')
        assert self._entry_with_config(cfg)["fanout_directive"] == ""

    def test_short_verb_config_key_resolves_like_canonical(self) -> None:
        # The user may write the short-verb spelling in [agent.phase_fanout];
        # it must resolve the same as the canonical gerund (mirrors the registry
        # normalization), not silently no-op.
        self._reviewer_task()
        cfg = _config('[agent.phase_fanout]\n"reviewer:review" = true\n')
        directive = self._entry_with_config(cfg)["fanout_directive"]
        assert "adversarial-verify" in directive
        assert "N=3" in directive

    def test_out_of_bounds_int_fails_loud_on_the_dispatch_path(self) -> None:
        self._planning_task()
        cfg = _config('[agent.phase_fanout]\n"author:planning" = 99\n')
        with patch("teatree.config_agent.CONFIG_PATH", cfg), pytest.raises(ValueError, match="Invalid phase_fanout N"):
            self._entry()


class TestClaimNextCarriesFanoutDirective(_FanoutDispatchTest):
    def test_claim_next_payload_carries_the_directive(self) -> None:
        self._reviewer_task()
        cfg = _config('[agent.phase_fanout]\n"reviewer:reviewing" = true\n')
        stdout = StringIO()
        with patch("teatree.config_agent.CONFIG_PATH", cfg):
            call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)
        entry = json.loads(stdout.getvalue())[0]
        assert "adversarial-verify" in entry["fanout_directive"]


class TestHeadlessParity(_FanoutDispatchTest):
    """Secondary headless-composer parity for the fan-out directive.

    ``build_system_context`` (the headless composer) carries the same directive
    as the interactive composer, so switching ``agent_runtime`` between
    interactive and a headless runtime keeps it.
    """

    def _context(self, task: Task, cfg: Path | None) -> str:
        if cfg is None:
            return build_system_context(task, skills=[], lifecycle_skill="t3:review")
        with patch("teatree.config_agent.CONFIG_PATH", cfg):
            return build_system_context(task, skills=[], lifecycle_skill="t3:review")

    def test_reviewing_context_absent_by_default(self) -> None:
        task = self._reviewer_task()
        assert "adversarial-verify" not in self._context(task, None)

    def test_reviewing_context_carries_directive_when_opted_in(self) -> None:
        task = self._reviewer_task()
        cfg = _config('[agent.phase_fanout]\n"reviewer:reviewing" = true\n')
        assert "adversarial-verify" in self._context(task, cfg)

    def test_planning_context_absent_by_default(self) -> None:
        task = self._planning_task()
        assert "judge-panel" not in self._context(task, None)

    def test_planning_context_carries_directive_when_opted_in(self) -> None:
        task = self._planning_task()
        cfg = _config('[agent.phase_fanout]\n"author:planning" = 4\n')
        context = self._context(task, cfg)
        assert "judge-panel" in context
        assert "N=4" in context
