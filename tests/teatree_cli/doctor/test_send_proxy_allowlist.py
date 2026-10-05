"""The doctor reports an unusable send-proxy configuration before startup."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.cli.doctor.checks_send_proxy import _check_malformed_private_repos, _check_send_proxy_allowlist
from teatree.cli.doctor.run_checks import _check_enabled_but_unprovisioned
from teatree.core.send_proxy import SendChannel, destination_allowed


def test_empty_allowlist_fails_for_non_self_destinations(capsys: pytest.CaptureFixture[str]) -> None:
    overlay = SimpleNamespace(
        get_repos=lambda: ["owner/repo"], config=SimpleNamespace(get_review_channel=lambda: ("review", "C123"))
    )
    with (
        patch("teatree.config.discover_overlays", return_value=[SimpleNamespace(name="team")]),
        patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
        patch("teatree.core.send_proxy.get_effective_settings", return_value=SimpleNamespace(send_proxy_allowlist=[])),
    ):
        assert _check_send_proxy_allowlist() is False
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "send_proxy_allowlist" in output
    assert "t3 teatree config_setting set send_proxy_allowlist" in output
    assert '\'["github:owner/repo","slack:C123"]\'' in output


def test_overlay_scoped_allowlist_does_not_satisfy_global_senders(capsys: pytest.CaptureFixture[str]) -> None:
    overlay = SimpleNamespace(
        get_repos=lambda: ["owner/repo"], config=SimpleNamespace(get_review_channel=lambda: ("", ""))
    )

    def settings(scope: str | None) -> SimpleNamespace:
        return SimpleNamespace(send_proxy_allowlist=["github:owner/repo"] if scope == "team" else [])

    with (
        patch("teatree.config.discover_overlays", return_value=[SimpleNamespace(name="team")]),
        patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
        patch("teatree.core.send_proxy.get_effective_settings", side_effect=settings) as resolve,
    ):
        assert _check_send_proxy_allowlist() is False
    assert all(call.args == (None,) for call in resolve.call_args_list)
    assert "global send_proxy_allowlist" in capsys.readouterr().out


def test_populated_allowlist_passes_for_non_self_destinations(capsys: pytest.CaptureFixture[str]) -> None:
    overlay = SimpleNamespace(
        get_repos=lambda: ["owner/repo"], config=SimpleNamespace(get_review_channel=lambda: ("review", "C123"))
    )
    with (
        patch("teatree.config.discover_overlays", return_value=[SimpleNamespace(name="team")]),
        patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
        patch(
            "teatree.core.send_proxy.get_effective_settings",
            return_value=SimpleNamespace(send_proxy_allowlist=["github:owner/repo", "slack:C123"]),
        ),
    ):
        assert _check_send_proxy_allowlist() is True
    assert capsys.readouterr().out == ""


def test_allowlist_must_cover_every_overlay_destination(capsys: pytest.CaptureFixture[str]) -> None:
    overlay = SimpleNamespace(
        get_repos=lambda: ["owner/one", "owner/two"],
        config=SimpleNamespace(get_review_channel=lambda: ("review", "C123")),
    )
    with (
        patch("teatree.config.discover_overlays", return_value=[SimpleNamespace(name="team")]),
        patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
        patch(
            "teatree.core.send_proxy.get_effective_settings",
            return_value=SimpleNamespace(send_proxy_allowlist=["github:owner/one", "slack:C123"]),
        ),
    ):
        assert _check_send_proxy_allowlist() is False
    assert "github:owner/two" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("allowlist", "expected"),
    [(["github:souliane/teatree"], True), ([], False)],
)
def test_bare_repo_name_uses_clone_url_for_allowlist(
    capsys: pytest.CaptureFixture[str], allowlist: list[str], *, expected: bool
) -> None:
    overlay = SimpleNamespace(
        get_repos=lambda: ["teatree"],
        provisioning=SimpleNamespace(repo_clone_url=lambda _repo: "https://github.com/souliane/teatree.git"),
        config=SimpleNamespace(get_review_channel=lambda: ("", "")),
    )
    with (
        patch("teatree.config.discover_overlays", return_value=[SimpleNamespace(name="team")]),
        patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
        patch(
            "teatree.core.send_proxy.get_effective_settings",
            return_value=SimpleNamespace(send_proxy_allowlist=allowlist),
        ),
    ):
        assert _check_send_proxy_allowlist() is expected
    output = capsys.readouterr().out
    if expected:
        assert output == ""
    else:
        assert "global send_proxy_allowlist does not cover github:souliane/teatree" in output


@pytest.mark.parametrize(
    ("pattern", "allowed"),
    [("gitlab:group/repo", True), ("group/repo", True), ("forge:group/repo", False)],
)
def test_gitlab_doctor_matches_sender(pattern: str, *, allowed: bool) -> None:
    overlay = SimpleNamespace(
        get_repos=lambda: ["group/repo"],
        config=SimpleNamespace(get_review_channel=lambda: ("", "")),
        provisioning=SimpleNamespace(repo_clone_url=lambda _repo: "https://gitlab.com/group/repo"),
    )
    with (
        patch("teatree.config.discover_overlays", return_value=[SimpleNamespace(name="team")]),
        patch("teatree.core.overlay_loader.get_overlay", return_value=overlay),
        patch(
            "teatree.core.send_proxy.get_effective_settings",
            return_value=SimpleNamespace(send_proxy_allowlist=[pattern]),
        ),
    ):
        assert _check_send_proxy_allowlist() is allowed
        assert destination_allowed(SendChannel.GITLAB, "gitlab:group/repo", overlay="") is allowed


def test_overlay_load_error_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("teatree.config.discover_overlays", return_value=[SimpleNamespace(name="broken")]),
        patch("teatree.core.overlay_loader.get_overlay", side_effect=ImportError("missing module")),
    ):
        assert _check_send_proxy_allowlist() is False
    assert "could not load overlay 'broken'" in capsys.readouterr().out


def test_overlay_configuration_error_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("teatree.config.discover_overlays", return_value=[SimpleNamespace(name="broken")]),
        patch("teatree.core.overlay_loader.get_overlay", side_effect=ImproperlyConfigured("not registered")),
    ):
        assert _check_send_proxy_allowlist() is False
    assert "could not load overlay 'broken'" in capsys.readouterr().out


def test_doctor_lists_malformed_stored_private_repos(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("teatree.config.cold_reader.read_setting", return_value=["acme/widget", "github.com/acme/widget"]):
        assert _check_malformed_private_repos() is False
    output = capsys.readouterr().out
    assert "FAIL" in output
    assert "acme/widget" in output
    assert "github.com/acme/widget" not in output


def test_empty_allowlist_reddens_doctor_aggregate() -> None:
    module = "teatree.cli.doctor.run_checks"
    with (
        patch(f"{module}._check_declared_dependencies_provisioned", return_value=True),
        patch(f"{module}._check_configured_review_skills", return_value=True),
        patch(f"{module}._check_pyright_lsp_plugin", return_value=True),
        patch(f"{module}._check_dispatched_overlay_skills", return_value=True),
        patch(f"{module}._check_skill_source_drift", return_value=True),
        patch(f"{module}._check_notion_credentials", return_value=True),
        patch(f"{module}._check_send_proxy_allowlist", return_value=False),
        patch(f"{module}._check_malformed_private_repos", return_value=True),
    ):
        assert _check_enabled_but_unprovisioned() is False
