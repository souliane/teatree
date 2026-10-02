"""Tests for the captures a test plan carries in git.

A plan cites its captures by path; a plan issued outside the repository commits
them beside itself under ``test-plans/evidence/<plan name>/`` instead. That second
shape had no command support, so those captures were placed by hand and no
preflight ever saw them — a plan could ship screenshots with no highlight box
while every gate reported green.

These tests pin the close: the command is the only way to put a capture there,
and the captures already there are re-validated before any run may write —
through the same ``validate_test_plan_images`` the manifest path uses, with the
same user-authorised bypass and no second implementation.
"""

import io
import json
from pathlib import Path
from typing import Literal, cast
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core import invocation_cwd as _invocation_cwd
from teatree.core.evidence.bdd_scenario_source import BddScenarioSource, render_bdd_source
from teatree.core.management.commands._test_plan import committed_captures as _captures
from teatree.core.management.commands._test_plan import file_store as _file_store
from teatree.core.management.commands._test_plan import render as _render
from teatree.core.management.commands._test_plan import write as _write
from teatree.core.models import Ticket

_MOCK_OVERLAY_NAME = "test"
_Side = Literal["dev", "local", "stack"]
_ISSUE_URL = "https://gitlab.com/org/repo/-/issues/4521"
_FINAL_BDD_SOURCE: BddScenarioSource = {
    "prd_page": "https://notion.so/example-prd",
    "bdd_revision": "2026-09-22",
    "status": "final",
    "scenario_ids": ["BDD-4521-001"],
}


def _red_boxed_png(path: Path, *, fill: tuple[int, int, int] = (245, 245, 245)) -> Path:
    """Write a real PNG carrying a ``highlightAndShoot`` red outline box."""
    from PIL import Image, ImageDraw  # noqa: PLC0415 — deferred: call-time import

    img = Image.new("RGB", (400, 300), fill)
    draw = ImageDraw.Draw(img)
    for off in range(6):
        draw.rectangle([20 + off, 20 + off, 380 - off, 280 - off], outline=(255, 0, 0))
    img.save(path, "PNG")
    return path


def _plain_png(path: Path) -> Path:
    """Write a real PNG with NO red highlight box — the capture the gate must refuse."""
    from PIL import Image  # noqa: PLC0415 — deferred: call-time import

    Image.new("RGB", (400, 300), (240, 240, 240)).save(path, "PNG")
    return path


# The video gates have their own tests; these runs are stills-only via the documented escape.
_STILLS_ONLY = {"allow_no_video": True}


class _PlanCaptureTestBase(TestCase):
    """A ticket whose plan file resolves into a temporary e2e checkout."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path
        self._checkout = tmp_path / "checkout"
        (self._checkout / "e2e").mkdir(parents=True)
        self._monkeypatch.setattr(
            _write,
            "resolve_worktree",
            MagicMock(side_effect=_write.WorktreeNotFoundError("none")),
        )

    def _ticket(self) -> Ticket:
        ticket = Ticket.objects.create(overlay=_MOCK_OVERLAY_NAME, issue_url=_ISSUE_URL)
        self._monkeypatch.setattr(
            _write,
            "plan_path_for_ticket",
            lambda _ticket: self._plan_path,
        )
        return ticket

    @property
    def _plan_path(self) -> Path:
        return self._checkout / "test-plans" / "repo-4521.md"

    @property
    def _evidence_dir(self) -> Path:
        return self._checkout / "test-plans" / "evidence" / "repo-4521"

    def _manifest(self, *, images: list[Path], env: str = "local") -> str:
        return json.dumps(
            {
                "ticket": "4521",
                "title": "Example plan",
                "scenario_source": _FINAL_BDD_SOURCE,
                env: {"commits": {"repo": "aabb"}},
                "workflows": [
                    {
                        "workflow": "Login",
                        "steps": ["Open the app", "Sign in"],
                        env: {"images": [str(i) for i in images]},
                    },
                ],
            },
        )

    def _run(self, manifest: str, **kwargs: object) -> dict[str, object]:
        return cast(
            "dict[str, object]",
            call_command("e2e", "write-test-plan", ticket=_ISSUE_URL, manifest=manifest, **_STILLS_ONLY, **kwargs),
        )

    def _run_expecting_exit(self, manifest: str, **kwargs: object) -> None:
        with pytest.raises(SystemExit):
            call_command("e2e", "write-test-plan", ticket=_ISSUE_URL, manifest=manifest, **_STILLS_ONLY, **kwargs)

    def _make_capture_legacy_flat(self, *, env: _Side, name: str, also_cite_from: _Side | None = None) -> Path:
        namespaced = self._evidence_dir / env / name
        legacy = self._evidence_dir / name
        namespaced.replace(legacy)
        state = _file_store.read_plan_state(self._plan_path)
        flat_link = f"evidence/{self._evidence_dir.name}/{name}"
        namespaced_link = f"evidence/{self._evidence_dir.name}/{env}/{name}"
        for workflow in state[env]["workflows"].values():
            workflow["video_md"] = workflow["video_md"].replace(namespaced_link, flat_link)
            workflow["image_md"] = [link.replace(namespaced_link, flat_link) for link in workflow["image_md"]]
        if also_cite_from is not None:
            state[also_cite_from] = {
                "commits": {},
                "workflows": state[env]["workflows"],
                "env": also_cite_from,
            }
        _file_store.write_plan(self._plan_path, _render.render_body(state))
        return legacy


class TestCapturesCommittedBesideThePlan(_PlanCaptureTestBase):
    """``--embed-captures`` is the only way a capture gets into the repo — and it is gated."""

    def test_captures_are_copied_and_embedded_by_relative_path(self) -> None:
        self._ticket()
        image = _red_boxed_png(self._tmp / "step1.png")

        result = self._run(self._manifest(images=[image]), embed_captures=True)

        assert (self._evidence_dir / "local" / "step1.png").read_bytes() == image.read_bytes()
        body = self._plan_path.read_text(encoding="utf-8")
        assert "](evidence/repo-4521/local/step1.png)" in body
        assert str(self._tmp) not in body
        assert result["action"] == "created"

    def test_without_the_flag_captures_are_cited_not_copied(self) -> None:
        self._ticket()
        self._run(self._manifest(images=[_red_boxed_png(self._tmp / "step1.png")]))

        assert not self._evidence_dir.exists()
        assert "](evidence/" not in self._plan_path.read_text(encoding="utf-8")

    def test_a_capture_with_no_red_box_is_refused_and_nothing_is_written(self) -> None:
        self._ticket()

        self._run_expecting_exit(self._manifest(images=[_plain_png(self._tmp / "plain.png")]), embed_captures=True)

        assert not self._plan_path.exists()
        assert not self._evidence_dir.exists()

    def test_two_captures_sharing_a_name_are_refused(self) -> None:
        self._ticket()
        (self._tmp / "one").mkdir()
        (self._tmp / "two").mkdir()
        first = _red_boxed_png(self._tmp / "one" / "shot.png")
        second = _red_boxed_png(self._tmp / "two" / "shot.png", fill=(200, 220, 240))

        self._run_expecting_exit(self._manifest(images=[first, second]), embed_captures=True)

        assert not self._plan_path.exists()


class TestStackCapturesTakeTheSameEmbedPath(_PlanCaptureTestBase):
    """A run against a remote stack records its captures as ``stack``, and an outward plan needs them in git."""

    def _refusal(self, manifest: str, **kwargs: object) -> str:
        err = io.StringIO()
        with pytest.raises(SystemExit):
            call_command(
                "e2e", "write-test-plan", ticket=_ISSUE_URL, manifest=manifest, stderr=err, **_STILLS_ONLY, **kwargs
            )
        return err.getvalue()

    def test_stack_captures_are_copied_and_embedded_in_the_stack_column(self) -> None:
        self._ticket()
        image = _red_boxed_png(self._tmp / "step1.png")

        result = self._run(self._manifest(images=[image], env="stack"), embed_captures=True)

        assert (self._evidence_dir / "stack" / "step1.png").read_bytes() == image.read_bytes()
        body = self._plan_path.read_text(encoding="utf-8")
        assert "| Dev | Local | Stack |" in body
        assert "| — | — | ![Login — step1](evidence/repo-4521/stack/step1.png) |" in body
        assert result["envs"] == ["stack"]

    def test_without_the_flag_stack_captures_are_cited_not_copied(self) -> None:
        self._ticket()
        artifacts_root = self._tmp / "artifacts"
        (artifacts_root / "stack").mkdir(parents=True)
        image = _red_boxed_png(artifacts_root / "stack" / "step1.png")

        self._run(self._manifest(images=[image], env="stack"), artifacts_dir=str(artifacts_root))

        assert not self._evidence_dir.exists()
        assert "| — | — | `stack/step1.png` |" in self._plan_path.read_text(encoding="utf-8")

    def test_local_and_stack_captures_land_in_their_own_columns(self) -> None:
        self._ticket()
        (self._tmp / "local").mkdir()
        (self._tmp / "stack").mkdir()
        local = _red_boxed_png(self._tmp / "local" / "step1.png")
        stack = _red_boxed_png(self._tmp / "stack" / "step1.png", fill=(200, 220, 240))
        manifest = json.dumps(
            {
                "ticket": "4521",
                "scenario_source": _FINAL_BDD_SOURCE,
                "local": {"commits": {"repo": "aabb"}},
                "stack": {"commits": {"repo": "ccdd"}},
                "workflows": [
                    {"workflow": "Login", "local": {"images": [str(local)]}, "stack": {"images": [str(stack)]}},
                ],
            },
        )

        result = self._run(manifest, embed_captures=True)

        body = self._plan_path.read_text(encoding="utf-8")
        assert (
            "| — | ![Login — step1](evidence/repo-4521/local/step1.png) | "
            "![Login — step1](evidence/repo-4521/stack/step1.png) |"
        ) in body
        assert (self._evidence_dir / "local" / "step1.png").read_bytes() == local.read_bytes()
        assert (self._evidence_dir / "stack" / "step1.png").read_bytes() == stack.read_bytes()
        assert result["envs"] == ["local", "stack"]

    def test_a_later_stack_capture_keeps_the_frozen_local_bytes(self) -> None:
        self._ticket()
        (self._tmp / "local").mkdir()
        (self._tmp / "stack").mkdir()
        local = _red_boxed_png(self._tmp / "local" / "step1.png")
        stack = _red_boxed_png(self._tmp / "stack" / "step1.png", fill=(200, 220, 240))

        self._run(self._manifest(images=[local]), embed_captures=True)
        self._run(self._manifest(images=[stack], env="stack"), embed_captures=True)

        body = self._plan_path.read_text(encoding="utf-8")
        assert "evidence/repo-4521/local/step1.png" in body
        assert "evidence/repo-4521/stack/step1.png" in body
        assert (self._evidence_dir / "local" / "step1.png").read_bytes() == local.read_bytes()
        assert (self._evidence_dir / "stack" / "step1.png").read_bytes() == stack.read_bytes()

    def test_a_legacy_flat_capture_stays_in_place_beside_a_namespaced_one(self) -> None:
        self._ticket()
        self._evidence_dir.mkdir(parents=True)
        legacy = _red_boxed_png(self._evidence_dir / "step1.png", fill=(210, 230, 250))
        legacy_bytes = legacy.read_bytes()
        (self._tmp / "stack").mkdir()
        stack = _red_boxed_png(self._tmp / "stack" / "step1.png")

        self._run(self._manifest(images=[stack], env="stack"), embed_captures=True)

        assert legacy.read_bytes() == legacy_bytes
        assert (self._evidence_dir / "stack" / "step1.png").read_bytes() == stack.read_bytes()

    def test_an_identical_legacy_flat_capture_is_migrated_on_same_side_rerun(self) -> None:
        self._ticket()
        fresh = _red_boxed_png(self._tmp / "step1.png")
        self._run(self._manifest(images=[fresh]), embed_captures=True)
        legacy = self._make_capture_legacy_flat(env="local", name="step1.png")

        self._run(self._manifest(images=[fresh]), embed_captures=True)

        assert not legacy.exists()
        assert (self._evidence_dir / "local" / "step1.png").read_bytes() == fresh.read_bytes()
        assert "evidence/repo-4521/step1.png" not in self._plan_path.read_text(encoding="utf-8")

    def test_a_changed_legacy_flat_capture_is_replaced_on_same_side_rerun(self) -> None:
        self._ticket()
        self._run(self._manifest(images=[_red_boxed_png(self._tmp / "step1.png")]), embed_captures=True)
        legacy = self._make_capture_legacy_flat(env="local", name="step1.png")
        (self._tmp / "recapture").mkdir()
        changed = _red_boxed_png(self._tmp / "recapture" / "step1.png", fill=(200, 220, 240))

        self._run(self._manifest(images=[changed]), embed_captures=True)

        assert not legacy.exists()
        assert (self._evidence_dir / "local" / "step1.png").read_bytes() == changed.read_bytes()
        assert "evidence/repo-4521/step1.png" not in self._plan_path.read_text(encoding="utf-8")

    def test_an_identical_legacy_flat_capture_another_side_cites_is_refused_as_a_duplicate(self) -> None:
        self._ticket()
        fresh = _red_boxed_png(self._tmp / "step1.png")
        self._run(self._manifest(images=[fresh]), embed_captures=True)
        legacy = self._make_capture_legacy_flat(env="local", name="step1.png", also_cite_from="stack")
        body = self._plan_path.read_text(encoding="utf-8")

        self._run_expecting_exit(self._manifest(images=[fresh]), embed_captures=True)

        assert legacy.read_bytes() == fresh.read_bytes()
        assert not (self._evidence_dir / "local" / "step1.png").exists()
        assert self._plan_path.read_text(encoding="utf-8") == body

    def test_a_stack_capture_identical_to_a_committed_local_one_is_refused(self) -> None:
        self._ticket()
        (self._tmp / "local").mkdir()
        (self._tmp / "stack").mkdir()
        local = _red_boxed_png(self._tmp / "local" / "login.png")
        stack = _red_boxed_png(self._tmp / "stack" / "dashboard.png")
        self._run(self._manifest(images=[local]), embed_captures=True)

        err = self._refusal(self._manifest(images=[stack], env="stack"), embed_captures=True)

        assert "dashboard.png is byte-identical to login.png" in err

    def test_a_stack_capture_with_no_red_box_is_refused_by_the_preflight(self) -> None:
        self._ticket()

        err = self._refusal(
            self._manifest(images=[_plain_png(self._tmp / "plain.png")], env="stack"), embed_captures=True
        )

        assert "no red highlight box found in plain.png" in err
        assert not self._plan_path.exists()
        assert not self._evidence_dir.exists()

    def test_byte_identical_stack_captures_are_refused_by_the_preflight(self) -> None:
        self._ticket()
        first = _red_boxed_png(self._tmp / "first.png")
        second = _red_boxed_png(self._tmp / "second.png")

        err = self._refusal(self._manifest(images=[first, second], env="stack"), embed_captures=True)

        assert "second.png is byte-identical to first.png" in err
        assert not self._plan_path.exists()
        assert not self._evidence_dir.exists()

    def test_a_stack_re_capture_replaces_a_stale_committed_image(self) -> None:
        self._ticket()
        (self._evidence_dir / "stack").mkdir(parents=True)
        _plain_png(self._evidence_dir / "stack" / "step1.png")
        fresh = _red_boxed_png(self._tmp / "step1.png")

        self._run(self._manifest(images=[fresh], env="stack"), embed_captures=True)

        assert (self._evidence_dir / "stack" / "step1.png").read_bytes() == fresh.read_bytes()


class TestCapturesAlreadyInGitAreGated(_PlanCaptureTestBase):
    """The half a command alone cannot own: evidence someone committed by hand."""

    def _commit_by_hand(self, name: str, *, valid: bool) -> Path:
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        target = self._evidence_dir / name
        return _red_boxed_png(target) if valid else _plain_png(target)

    def test_a_hand_placed_capture_with_no_red_box_refuses_the_write(self) -> None:
        self._ticket()
        self._commit_by_hand("hand-dropped.png", valid=False)

        self._run_expecting_exit(self._manifest(images=[_red_boxed_png(self._tmp / "step1.png")]))

        assert not self._plan_path.exists()

    def test_a_hand_placed_capture_with_a_red_box_lets_the_write_through(self) -> None:
        self._ticket()
        self._commit_by_hand("hand-dropped.png", valid=True)

        self._run(self._manifest(images=[_red_boxed_png(self._tmp / "step1.png", fill=(230, 240, 250))]))

        assert self._plan_path.is_file()

    def test_a_body_file_cannot_smuggle_an_unvalidated_capture_past_the_gate(self) -> None:
        self._ticket()
        self._commit_by_hand("hand-dropped.png", valid=False)
        body = self._tmp / "plan.md"
        body.write_text(
            f"""\
{render_bdd_source(_FINAL_BDD_SOURCE)}

## Test Plan

![shot](evidence/repo-4521/hand-dropped.png)
""",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            call_command("e2e", "write-test-plan", ticket=_ISSUE_URL, body_file=str(body))

        assert not self._plan_path.exists()

    def test_re_capturing_over_a_stale_bad_image_is_allowed(self) -> None:
        self._ticket()
        (self._evidence_dir / "local").mkdir(parents=True)
        _plain_png(self._evidence_dir / "local" / "step1.png")

        self._run(self._manifest(images=[_red_boxed_png(self._tmp / "step1.png")]), embed_captures=True)

        assert self._plan_path.is_file()
        assert _captures.committed_captures(self._evidence_dir) == [self._evidence_dir / "local" / "step1.png"]

    def test_skip_validation_is_the_documented_bypass(self) -> None:
        self._ticket()
        self._commit_by_hand("hand-dropped.png", valid=False)

        self._run(
            self._manifest(images=[_red_boxed_png(self._tmp / "step1.png")]),
            embed_captures=True,
            skip_validation=True,
        )

        assert self._plan_path.is_file()


class TestEvidenceDirIsKeyedLikeThePlanFile(_PlanCaptureTestBase):
    """Work-item numbers repeat across repos, so the number alone cannot key the directory."""

    def test_two_repos_same_numbered_tickets_do_not_share_a_directory(self) -> None:
        plans_dir = self._checkout / "test-plans"

        product = _captures.evidence_dir_for(plans_dir / "product-4521.md")
        client = _captures.evidence_dir_for(plans_dir / "client-4521.md")

        assert product != client
        assert product.parent == plans_dir / "evidence"

    def test_a_legacy_unprefixed_plan_keeps_its_unprefixed_directory(self) -> None:
        plans_dir = self._checkout / "test-plans"

        assert _captures.evidence_dir_for(plans_dir / "4521.md") == plans_dir / "evidence" / "4521"


class TestVerifyPlanCapturesCommand(_PlanCaptureTestBase):
    """The standing gate a repo runs over evidence already in git."""

    def _plans_dir(self) -> Path:
        return self._checkout / "test-plans"

    def _write_plan(self, name: str, **source: object) -> Path:
        plan = self._plans_dir() / name
        plan.parent.mkdir(parents=True, exist_ok=True)
        declaration = json.dumps(_FINAL_BDD_SOURCE | source)
        plan.write_text(f"<!-- t3-bdd-source {declaration} -->\n## Test Plan\n", encoding="utf-8")
        return plan

    def _one_capture(self) -> None:
        self._evidence_dir.mkdir(parents=True)
        _red_boxed_png(self._evidence_dir / "a.png")

    def test_refuses_a_plans_dir_with_an_unhighlighted_capture(self) -> None:
        self._evidence_dir.mkdir(parents=True)
        _plain_png(self._evidence_dir / "plain.png")

        with pytest.raises(SystemExit):
            call_command("e2e", "verify-plan-captures", plans_dir=str(self._plans_dir()))

    def test_passes_when_every_capture_carries_a_highlight_box(self) -> None:
        self._evidence_dir.mkdir(parents=True)
        _red_boxed_png(self._evidence_dir / "a.png")
        _red_boxed_png(self._evidence_dir / "b.png", fill=(200, 220, 240))
        self._write_plan("repo-4521.md")

        failures = call_command("e2e", "verify-plan-captures", plans_dir=str(self._plans_dir()))

        assert failures == []

    def test_refuses_captures_no_plan_owns(self) -> None:
        self._one_capture()

        failures = _captures.verify_plans_dir(self._plans_dir())

        assert len(failures) == 1
        assert failures[0].startswith("repo-4521: no plan owns these captures")

    def test_checks_every_language_edition(self) -> None:
        self._one_capture()
        self._write_plan("repo-4521.md")
        self._write_plan("repo-4521-en.md", status="preflight")

        failures = _captures.verify_plans_dir(self._plans_dir())

        assert [failure.split(":", 1)[0] for failure in failures] == ["repo-4521-en"]
        assert "preflight" in failures[0]

    def test_final_editions_pass(self) -> None:
        self._one_capture()
        self._write_plan("repo-4521-de.md")
        self._write_plan("repo-4521-en.md")

        assert _captures.verify_plans_dir(self._plans_dir()) == []

    def test_an_unreadable_plan_is_a_failure_not_a_crash(self) -> None:
        self._one_capture()
        self._write_plan("repo-4521-en.md")
        (self._plans_dir() / "repo-4521-de.md").write_bytes(b"\xff\xfe not utf-8")

        failures = _captures.verify_plans_dir(self._plans_dir())

        assert [failure.split(":", 1)[0] for failure in failures] == ["repo-4521-de"]

    def test_refuses_a_committed_plan_without_a_final_bdd_source(self) -> None:
        self._evidence_dir.mkdir(parents=True)
        _red_boxed_png(self._evidence_dir / "a.png")
        self._plan_path.write_text("## Test Plan\n", encoding="utf-8")

        with pytest.raises(SystemExit):
            call_command("e2e", "verify-plan-captures", plans_dir=str(self._plans_dir()))

    def test_refuses_rather_than_reporting_success_when_there_is_nothing_to_look_at(self) -> None:
        with pytest.raises(SystemExit):
            call_command("e2e", "verify-plan-captures", plans_dir=str(self._tmp / "absent"))

    def test_refuses_an_evidence_root_whose_ticket_directories_hold_no_capture(self) -> None:
        self._evidence_dir.mkdir(parents=True)

        with pytest.raises(SystemExit):
            call_command("e2e", "verify-plan-captures", plans_dir=str(self._plans_dir()))


class TestPlanFileIsNamedPerRepo(_PlanCaptureTestBase):
    """Work-item numbers are per repo, so the plan's filename names both."""

    def test_plan_path_carries_the_ticket_repo_and_number(self) -> None:
        ticket = Ticket.objects.create(overlay=_MOCK_OVERLAY_NAME, issue_url=_ISSUE_URL)
        self._monkeypatch.setattr(_file_store, "_checkout_for", lambda _t, *, repo: self._checkout)
        self._monkeypatch.setattr(
            _file_store,
            "get_overlay",
            lambda _name: MagicMock(metadata=MagicMock(get_e2e_config=lambda: {"project_path": "org/repo"})),
        )

        assert _file_store.plan_path_for_ticket(ticket) == self._plan_path


class TestTheDefaultPlansDirIsInvocationRelative(_PlanCaptureTestBase):
    """Under the containerized CLI the process cwd is the image WORKDIR — another tree entirely.

    Defaulting to it verified a directory the operator never named and reported
    their evidence missing, which invites a "fix" to a plan that was never broken.
    """

    def _valid_plans_dir(self) -> Path:
        evidence = self._checkout / "test-plans" / "evidence" / "repo-4521"
        evidence.mkdir(parents=True)
        _red_boxed_png(evidence / "a.png")
        (self._checkout / "test-plans" / "repo-4521.md").write_text(
            f"{render_bdd_source(_FINAL_BDD_SOURCE)}\n## Test Plan\n", encoding="utf-8"
        )
        return self._checkout / "test-plans"

    def _stand_in_another_tree(self) -> Path:
        elsewhere = self._tmp / "image-workdir"
        elsewhere.mkdir()
        self._monkeypatch.chdir(elsewhere)
        return elsewhere

    def _declare(self, cwd: Path) -> None:
        self._monkeypatch.setenv(_invocation_cwd.INVOCATION_CWD_ENV, str(cwd))

    def _refusal(self, **kwargs: object) -> str:
        err = io.StringIO()
        with pytest.raises(SystemExit):
            call_command("e2e", "verify-plan-captures", stderr=err, **kwargs)
        return err.getvalue()

    def test_the_bare_default_reads_the_declared_invocation_cwd_not_the_process_cwd(self) -> None:
        self._valid_plans_dir()
        self._stand_in_another_tree()
        self._declare(self._checkout)

        assert call_command("e2e", "verify-plan-captures") == []

    def test_an_explicit_plans_dir_is_used_verbatim_whatever_the_invocation_cwd_declares(self) -> None:
        self._valid_plans_dir()
        self._stand_in_another_tree()
        self._declare(self._checkout)
        rogue = self._tmp / "rogue" / "test-plans"
        (rogue / "evidence" / "repo-1").mkdir(parents=True)
        _plain_png(rogue / "evidence" / "repo-1" / "plain.png")

        assert "plain.png" in self._refusal(plans_dir=str(rogue))

    def test_an_unresolvable_default_names_the_directory_and_where_it_came_from(self) -> None:
        bare = self._tmp / "no-plans-here"
        bare.mkdir()
        self._stand_in_another_tree()
        self._declare(bare)

        message = self._refusal()

        assert str(bare / _captures.PLANS_DIR_DEFAULT) in message
        assert _invocation_cwd.INVOCATION_CWD_ENV in message

    def test_a_resolution_failure_reads_differently_from_genuinely_missing_captures(self) -> None:
        bare = self._tmp / "no-plans-at-all"
        bare.mkdir()
        self._stand_in_another_tree()
        self._declare(bare)
        unresolvable = self._refusal()

        (self._checkout / "test-plans").mkdir(parents=True)
        self._declare(self._checkout)
        no_captures = self._refusal()

        assert "resolve" in unresolvable
        assert "No committed captures" in no_captures
        assert "No committed captures" not in unresolvable
