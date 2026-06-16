"""Tests for ``t3 codex review`` — manual fire-and-forget surface (#1254)."""

import json

import pytest
from typer.testing import CliRunner

from teatree.cli.codex import codex_app
from teatree.core.models.codex_review_marker import CodexReviewMarker

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


PR_URL = "https://github.com/souliane/teatree/pull/1254"
SHA = "feedfacecafebabe1234567890abcdef12345678"


def _run(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(codex_app, list(args))
    if result.exit_code != 0 and result.exception is not None:
        import traceback  # noqa: PLC0415

        traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
    return result.exit_code, result.stdout


class TestCodexReview:
    def test_emits_dispatch_envelope_and_records_marker(self) -> None:
        code, out = _run(PR_URL, "--head-sha", SHA)

        assert code == 0
        envelope = json.loads(out)
        assert envelope["dispatched"] is True
        assert envelope["slug"] == "souliane/teatree"
        assert envelope["pr_id"] == 1254
        assert envelope["head_sha"] == SHA
        assert envelope["variant"] == "codex:review"
        assert envelope["reason"] == "claimed"
        assert CodexReviewMarker.objects.filter(slug="souliane/teatree", pr_id=1254, head_sha=SHA).exists()

    def test_repeat_invocation_is_skipped(self) -> None:
        _run(PR_URL, "--head-sha", SHA)
        code, out = _run(PR_URL, "--head-sha", SHA)

        assert code == 0
        envelope = json.loads(out)
        assert envelope["dispatched"] is False
        assert envelope["reason"] == "already_dispatched"

    def test_force_clears_marker_and_redispatches(self) -> None:
        _run(PR_URL, "--head-sha", SHA)
        code, out = _run(PR_URL, "--head-sha", SHA, "--force")

        assert code == 0
        envelope = json.loads(out)
        assert envelope["dispatched"] is True
        assert envelope["reason"] == "claimed"

    def test_security_path_selects_adversarial_variant(self) -> None:
        code, out = _run(
            PR_URL,
            "--head-sha",
            SHA,
            "--path",
            "src/teatree/permissions/policy.py",
        )

        assert code == 0
        envelope = json.loads(out)
        assert envelope["variant"] == "codex:adversarial-review"

    def test_malformed_pr_url_exits_with_error(self) -> None:
        code, _out = _run("not-a-url", "--head-sha", SHA)
        assert code == 2
