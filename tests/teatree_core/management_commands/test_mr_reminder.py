"""``t3 <overlay> mr_reminder`` command — preview + send (TODO-276).

Exercises the full ``call_command`` Typer+Django glue. Only the
unstoppable externals are stubbed: the code-host backend's
``list_my_prs`` (forge HTTP) and the messaging backend's ``post_routed``
(Slack HTTP). Routing config is injected as a real
:class:`MrReminderConfig` via ``get_effective_settings``; assembly,
routing, and the on-behalf egress chokepoint run for real (the gate
itself is covered in ``test_on_behalf_egress``; here it is satisfied by
publishing directly so the destination-routing contract is what's pinned).
"""

from contextlib import ExitStack
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from teatree.config import UserSettings
from teatree.config_mr_reminder import MrReminderConfig
from teatree.core.management.commands import mr_reminder as command_module
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_CONFIG = MrReminderConfig(
    channels=(("souliane/teatree", "C_TEATREE"), ("acme-engineering", "C_ACME")),
    default_channel="C_FALLBACK",
)

_PRS = [
    {"iid": 1, "title": "feat a", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/1"},
    {"number": 2, "title": "fix b", "html_url": "https://github.com/acme-engineering/widget/pull/2"},
]


def _host() -> MagicMock:
    host = MagicMock()
    host.current_user.return_value = "souliane"
    host.list_my_prs.return_value = _PRS
    return host


def _backend() -> MagicMock:
    """A backend whose #1750 classifier reports a ``C…`` channel as a colleague.

    A bare ``MagicMock`` auto-mocks ``_is_self_dm`` truthy, which would send a
    reminder channel down ``OnBehalfSlackEgress``'s self-DM carve-out; pin the
    real classification so the colleague-channel ``post_routed`` xoxp path the
    reminder targets is exercised.
    """
    backend = MagicMock()
    backend._is_self_dm.side_effect = lambda channel: channel.startswith(("D", "U"))
    backend.post_routed.return_value = {"ok": True, "ts": "1.0"}
    return backend


def _settings(config: MrReminderConfig = _CONFIG) -> UserSettings:
    return UserSettings(mr_reminder=config, user_identity_aliases=[])


def _patches(
    host: MagicMock | None,
    backend: MagicMock | None,
    settings: UserSettings,
    *,
    pass_gate: bool = True,
) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch.object(command_module, "code_host_from_overlay", return_value=host))
    stack.enter_context(patch.object(command_module, "messaging_from_overlay", return_value=backend))
    stack.enter_context(patch.object(command_module, "get_effective_settings", return_value=settings))
    # Satisfy the on-behalf gate by publishing directly + silence the
    # after-receipt DM; the gate itself is covered in test_on_behalf_egress.
    if pass_gate:
        stack.enter_context(
            patch(
                "teatree.core.on_behalf_egress.require_on_behalf_approval",
                lambda *, target, action, publish: publish(),
            ),
        )
        stack.enter_context(
            patch("teatree.core.on_behalf_egress.notify_user_on_behalf_post", lambda *_a, **_k: None),
        )
    return stack


def _call(*args: str) -> tuple[object, int]:
    # No ``stdout=`` capture: django-typer re-routes a truthy structured
    # return through ``OutputWrapper`` (string-only), a framework quirk
    # shared by every dict-returning command here (see ``test_standup_command``).
    code = 0
    result: object = None
    try:
        result = call_command(*args)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return result, code


class TestPreview:
    def test_assembles_per_channel_without_posting(self) -> None:
        host, backend = _host(), _backend()
        with _patches(host, backend, _settings()):
            result, code = _call("mr_reminder", "preview")

        assert code == 0
        result = cast("dict[str, object]", result)
        assert result["total"] == 2
        channels = {c["channel"]: c for c in cast("list[dict[str, object]]", result["channels"])}
        assert set(channels) == {"C_TEATREE", "C_ACME"}
        assert channels["C_TEATREE"]["count"] == 1
        assert "souliane/teatree !1" in cast("str", channels["C_TEATREE"]["text"])
        backend.post_routed.assert_not_called()

    def test_reports_error_when_no_channel_map(self) -> None:
        with _patches(_host(), _backend(), _settings(MrReminderConfig())):
            result, code = _call("mr_reminder", "preview")
        assert code == 0
        result = cast("dict[str, object]", result)
        assert result["total"] == 0
        channels = cast("list[dict[str, object]]", result["channels"])
        assert "No mr_reminder channel map" in cast("str", channels[0]["text"])


class TestSend:
    def test_posts_one_message_per_routed_channel(self) -> None:
        host, backend = _host(), _backend()
        with _patches(host, backend, _settings()):
            result, code = _call("mr_reminder", "send")

        assert code == 0
        result = cast("dict[str, object]", result)
        assert sorted(cast("list[str]", result["posted"])) == ["C_ACME", "C_TEATREE"]
        assert backend.post_routed.call_count == 2
        posted_channels = {c.kwargs["channel"] for c in backend.post_routed.call_args_list}
        assert posted_channels == {"C_TEATREE", "C_ACME"}

    def test_routes_namespace_prefix_repo_to_org_channel(self) -> None:
        host, backend = _host(), _backend()
        host.list_my_prs.return_value = [
            {"iid": 9, "title": "deep", "web_url": "https://gitlab.com/acme-engineering/sub/deep/-/merge_requests/9"},
        ]
        with _patches(host, backend, _settings()):
            _, code = _call("mr_reminder", "send")
        assert code == 0
        backend.post_routed.assert_called_once()
        assert backend.post_routed.call_args.kwargs["channel"] == "C_ACME"

    def test_exits_nonzero_when_a_post_fails(self) -> None:
        host, backend = _host(), _backend()
        backend.post_routed.return_value = {"ok": False, "error": "channel_not_found"}
        with _patches(host, backend, _settings()):
            _, code = _call("mr_reminder", "send")
        assert code == 1

    def test_exits_two_when_on_behalf_gate_blocks(self) -> None:
        host, backend = _host(), _backend()
        with (
            _patches(host, backend, _settings(), pass_gate=False),
            patch(
                "teatree.core.on_behalf_egress.require_on_behalf_approval",
                side_effect=OnBehalfPostBlockedError("C_TEATREE", "cli_mr_reminder"),
            ),
        ):
            _, code = _call("mr_reminder", "send")
        assert code == 2
        backend.post_routed.assert_not_called()

    def test_exits_nonzero_when_no_code_host(self) -> None:
        with _patches(None, _backend(), _settings()):
            _, code = _call("mr_reminder", "send")
        assert code == 1

    def test_exits_nonzero_when_no_messaging_backend(self) -> None:
        with _patches(_host(), None, _settings()):
            _, code = _call("mr_reminder", "send")
        assert code == 1
