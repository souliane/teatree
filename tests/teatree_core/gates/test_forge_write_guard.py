from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.backends.gitlab.http_client import GitLabHTTPClient
from teatree.config.cold_reader import SettingRead
from teatree.core.gates.forge_write_guard import (
    ForgeWriteGuard,
    WriteRefusedError,
    item_key_of_endpoint,
    item_key_of_url,
)
from teatree.core.models.send_audit import SendAudit
from teatree.core.push.fast_push import GlabForge


def _response(method: str) -> httpx.Response:
    return httpx.Response(
        200, json={}, request=httpx.Request(method, "https://gitlab.example/api/v4/projects/group%2Frepo/issues/7")
    )


class TestGitLabWriteGuard(TestCase):
    def _setting(self, value: object, *, readable: bool = True) -> AbstractContextManager[object]:
        return patch(
            "teatree.config.cold_reader.read_setting_confirmed",
            return_value=SettingRead(value=value, readable=readable),
        )

    def test_off_keeps_body_and_writes_no_ledger(self) -> None:
        with self._setting(None), patch("httpx.Client.post", return_value=_response("POST")) as request:
            GitLabHTTPClient(token="CLIENT").post_json("projects/group%2Frepo/issues/7/notes", {"body": "/close"})
        assert request.call_args.kwargs["json"] == {"body": "/close"}
        assert SendAudit.objects.filter(action__startswith="guard:").count() == 0

    def test_cap_persists_across_clients_and_items_are_independent(self) -> None:
        with self._setting({"cap_per_hour": 2}), patch("httpx.Client.post", return_value=_response("POST")) as request:
            for _ in range(2):
                GitLabHTTPClient(token="CLIENT").post_json("projects/group%2Frepo/issues/7/notes", {"body": "hi"})
            with pytest.raises(WriteRefusedError):
                GitLabHTTPClient(token="CLIENT").post_json("projects/group%2Frepo/issues/7/notes", {"body": "hi"})
            GitLabHTTPClient(token="CLIENT").post_json("projects/group%2Frepo/issues/8/notes", {"body": "hi"})
        assert request.call_count == 3
        assert SendAudit.objects.filter(target="group/repo/issues/7", action__startswith="guard:").count() == 2

    def test_labels_and_quick_actions(self) -> None:
        setting = {"label_allowlist": ["allowed"], "escape_quick_actions": True}
        with self._setting(setting), patch("httpx.Client.post", return_value=_response("POST")) as request:
            with pytest.raises(WriteRefusedError):
                GitLabHTTPClient(token="CLIENT").post_json("projects/group%2Frepo/issues/7", {"labels": "other"})
            GitLabHTTPClient(token="CLIENT").post_json(
                "projects/group%2Frepo/issues/7/notes", {"body": "/label allowed\nhi /close"}
            )
        assert request.call_count == 1
        assert request.call_args.kwargs["json"] == {"body": "\\/label allowed\nhi /close"}

    def test_label_allowlist_admits_allowed_labels_in_string_and_list_forms(self) -> None:
        payloads = ({"labels": "allowed"}, {"labels": "allowed, also-allowed"}, {"labels": ["allowed", "also-allowed"]})
        for payload in payloads:
            with (
                self.subTest(payload=payload),
                self._setting({"label_allowlist": ["allowed", "also-allowed"]}),
                patch("httpx.Client.post", return_value=_response("POST")) as request,
            ):
                GitLabHTTPClient(token="CLIENT").post_json("projects/group%2Frepo/issues/7", payload)
            assert request.call_args.kwargs["json"] == payload

    def test_label_allowlist_refuses_a_foreign_label_in_list_form(self) -> None:
        with (
            self._setting({"label_allowlist": ["allowed"]}),
            patch("httpx.Client.post", return_value=_response("POST")) as request,
            pytest.raises(WriteRefusedError),
        ):
            GitLabHTTPClient(token="CLIENT").post_json(
                "projects/group%2Frepo/issues/7", {"labels": ["allowed", "other"]}
            )
        request.assert_not_called()

    def test_label_allowlist_vets_add_labels_and_remove_labels(self) -> None:
        for key in ("add_labels", "remove_labels"):
            with self.subTest(key=key), self._setting({"label_allowlist": ["allowed"]}):
                with patch("httpx.Client.put", return_value=_response("PUT")) as request:
                    GitLabHTTPClient(token="CLIENT").put_json("projects/group%2Frepo/issues/7", {key: "allowed"})
                request.assert_called_once()
                with (
                    patch("httpx.Client.put", return_value=_response("PUT")) as request,
                    pytest.raises(WriteRefusedError),
                ):
                    GitLabHTTPClient(token="CLIENT").put_json("projects/group%2Frepo/issues/7", {key: "other"})
                request.assert_not_called()

    def test_invalid_setting_refuses_without_wire_call(self) -> None:
        for value, readable in (({"cap_per_hour": "many"}, True), (None, False)):
            with self.subTest(value=value, readable=readable):
                with (
                    self._setting(value, readable=readable),
                    patch("httpx.Client.post", return_value=_response("POST")) as request,
                    pytest.raises(WriteRefusedError),
                ):
                    GitLabHTTPClient(token="CLIENT").post_json("projects/group%2Frepo/issues/7", {})
                request.assert_not_called()

    def test_own_items_only_refuses_colleague_and_unreadable_author(self) -> None:
        setting = {"own_items_only": True}
        with (
            self._setting(setting),
            patch("httpx.Client.get", return_value=_response("GET")),
            patch("httpx.Client.put", return_value=_response("PUT")) as request,
            pytest.raises(WriteRefusedError),
        ):
            GitLabHTTPClient(token="CLIENT").put_json("projects/group%2Frepo/issues/7", {"title": "x"})
        request.assert_not_called()

    def test_own_items_only_accepts_current_user(self) -> None:
        issue = httpx.Response(
            200,
            json={"author": {"username": "me"}, "web_url": "https://gitlab.com/group/repo/-/issues/7"},
            request=httpx.Request("GET", "https://gitlab.example/api/v4/projects/group%2Frepo/issues/7"),
        )
        user = httpx.Response(
            200, json={"username": "me"}, request=httpx.Request("GET", "https://gitlab.example/api/v4/user")
        )
        with (
            self._setting({"own_items_only": True}),
            patch("httpx.Client.get", side_effect=[issue, user]),
            patch("httpx.Client.put", return_value=_response("PUT")) as request,
        ):
            GitLabHTTPClient(token="CLIENT").put_json("projects/group%2Frepo/issues/7", {"title": "x"})
        request.assert_called_once()

    def _item_reads(self, *, author: str, status: int = 200) -> Callable[..., httpx.Response]:
        def read(url: str, **_kwargs: object) -> httpx.Response:
            request = httpx.Request("GET", url)
            if url.endswith("/user"):
                return httpx.Response(200, json={"username": "me"}, request=request)
            if url.endswith("/merge_requests/7"):
                item = {"author": {"username": author}, "web_url": "https://gitlab.com/group/repo/-/merge_requests/7"}
                return httpx.Response(status, json=item, request=request)
            return httpx.Response(404, json={}, request=request)

        return read

    def test_own_items_only_reads_the_item_behind_a_sub_resource_write(self) -> None:
        cases = (("me", 200, True), ("colleague", 200, False), ("me", 404, False))
        for suffix in ("merge", "rebase", "notes/5"):
            for author, status, allowed in cases:
                with (
                    self.subTest(suffix=suffix, author=author, status=status),
                    self._setting({"own_items_only": True}),
                    patch("httpx.Client.get", side_effect=self._item_reads(author=author, status=status)),
                    patch("httpx.Client.put", return_value=_response("PUT")) as request,
                ):
                    client = GitLabHTTPClient(token="CLIENT")
                    endpoint = f"projects/group%2Frepo/merge_requests/7/{suffix}"
                    if allowed:
                        client.put_response(endpoint, {})
                        request.assert_called_once()
                    else:
                        with pytest.raises(WriteRefusedError):
                            client.put_response(endpoint, {})
                        request.assert_not_called()

    def test_own_items_only_refuses_paths_that_do_not_name_one_item(self) -> None:
        endpoints = (
            "projects/group%2Frepo/merge_requests/7/../../issues/8",
            "projects/group%2Frepo/merge_requests/7/%2e%2e/%2e%2e/issues/8",
            "projects/group%2Frepo/merge_requests/7abc",
            "projects/group%2Frepo/merge_requests/%37",
            "projects/group%2Frepo/labels/3",
            "projects/group/repo/merge_requests/7",
            "projects/group%2Frepo/merge_requests/7/..%2f..%2fissues/8",
            "projects/group%2Frepo/merge_requests/7/..;/issues/8",
            "projects/group%2Frepo/merge_requests/7/%252e%252e/issues/8",
            "projects/group%2Frepo%5C..%2Fother/merge_requests/7",
            "groups/5/labels/3",
        )
        client = GitLabHTTPClient(token="CLIENT")
        sends = {"put": lambda endpoint: client.put_response(endpoint, {}), "delete": client.delete}
        for method, send in sends.items():
            for endpoint in endpoints:
                with (
                    self.subTest(method=method, endpoint=endpoint),
                    self._setting({"own_items_only": True}),
                    patch("httpx.Client.get", side_effect=self._item_reads(author="me")) as read,
                    patch(f"httpx.Client.{method}", return_value=_response(method.upper())) as write,
                    pytest.raises(WriteRefusedError),
                ):
                    send(endpoint)
                read.assert_not_called()
                write.assert_not_called()

    def test_own_items_only_reads_the_item_whatever_the_spelling_of_projects(self) -> None:
        client = GitLabHTTPClient(token="CLIENT")
        sends = {"put": lambda endpoint: client.put_response(endpoint, {}), "delete": client.delete}
        for method, send in sends.items():
            for first in ("%70rojects", "Projects", "PROJECTS"):
                endpoint = f"{first}/group%2Frepo/merge_requests/7"
                with (
                    self.subTest(method=method, first=first),
                    self._setting({"own_items_only": True}),
                    patch("httpx.Client.get", side_effect=self._item_reads(author="colleague")) as read,
                    patch(f"httpx.Client.{method}", return_value=_response(method.upper())) as write,
                    pytest.raises(WriteRefusedError),
                ):
                    send(endpoint)
                assert read.call_args_list[0].args[0].endswith("/projects/group%2Frepo/merge_requests/7")
                write.assert_not_called()

    def test_own_items_only_checks_authorship_on_delete(self) -> None:
        for author, allowed in (("me", True), ("colleague", False)):
            with (
                self.subTest(author=author),
                self._setting({"own_items_only": True}),
                patch("httpx.Client.get", side_effect=self._item_reads(author=author)),
                patch("httpx.Client.delete", return_value=_response("DELETE")) as write,
            ):
                client = GitLabHTTPClient(token="CLIENT")
                if allowed:
                    client.delete("projects/group%2Frepo/merge_requests/7/notes/5")
                    write.assert_called_once()
                else:
                    with pytest.raises(WriteRefusedError):
                        client.delete("projects/group%2Frepo/merge_requests/7/notes/5")
                    write.assert_not_called()

    def test_item_keys_casefold_the_project_and_quick_actions_escape_on_every_line(self) -> None:
        assert item_key_of_endpoint("projects/Group%2FRepo/merge_requests/7/notes") == "group/repo/merge_requests/7"
        assert item_key_of_url("https://gitlab.com/Group/Repo/-/merge_requests/7") == "group/repo/merge_requests/7"
        assert ForgeWriteGuard(escape_quick_actions=True).shape("ok\n/close") == "ok\n\\/close"

    def test_hour_window_expires_while_day_window_remains(self) -> None:
        endpoint = "projects/group%2Frepo/issues/7/notes"
        with self._setting({"cap_per_hour": 1}), patch("httpx.Client.post", return_value=_response("POST")) as request:
            GitLabHTTPClient(token="CLIENT").post_json(endpoint, {})
            SendAudit.objects.filter(action__startswith="guard:").update(created_at=timezone.now() - timedelta(hours=2))
            GitLabHTTPClient(token="CLIENT").post_json(endpoint, {})
        assert request.call_count == 2
        with (
            self._setting({"cap_per_day": 2}),
            patch("httpx.Client.post", return_value=_response("POST")) as request,
            pytest.raises(WriteRefusedError),
        ):
            GitLabHTTPClient(token="CLIENT").post_json(endpoint, {})
        request.assert_not_called()

    def test_glab_update_shares_durable_cap_bucket(self) -> None:
        forge = GlabForge(Path())
        with (
            self._setting({"cap_per_hour": 1}),
            patch.object(GlabForge, "_owner_token", return_value="TOKEN"),
            patch("teatree.core.push.fast_push.run_checked") as command,
        ):
            forge.update_pr(url="https://gitlab.com/group/repo/-/merge_requests/7", body="first")
            with pytest.raises(WriteRefusedError):
                forge.update_pr(url="https://gitlab.com/group/repo/-/merge_requests/7", body="second")
        command.assert_called_once()

    def test_glab_create_caps_per_project(self) -> None:
        forge = GlabForge(Path())
        with (
            self._setting({"cap_per_hour": 1}),
            patch("teatree.core.push.fast_push.git.remote_url", return_value="https://gitlab.com/group/repo.git"),
            patch(
                "teatree.core.push.fast_push.authoring_credential_for_remote",
                return_value=SimpleNamespace(token="TOKEN", refusal=""),
            ),
            patch("teatree.core.push.fast_push.unapprovable_author_refusal", return_value=""),
            patch(
                "teatree.core.push.fast_push.run_checked",
                return_value=SimpleNamespace(stdout="https://gitlab.com/group/repo/-/merge_requests/7"),
            ) as command,
        ):
            assert forge.create_pr(branch="branch", title="title", body="first")
            with pytest.raises(WriteRefusedError):
                forge.create_pr(branch="branch", title="title", body="second")
        command.assert_called_once()

    def test_glab_create_shares_cap_bucket_with_rest_across_remote_casing(self) -> None:
        forge = GlabForge(Path())
        with (
            self._setting({"cap_per_hour": 1}),
            patch("teatree.core.push.fast_push.git.remote_url", return_value="https://gitlab.com/Group/Repo.git"),
            patch(
                "teatree.core.push.fast_push.authoring_credential_for_remote",
                return_value=SimpleNamespace(token="TOKEN", refusal=""),
            ),
            patch("teatree.core.push.fast_push.unapprovable_author_refusal", return_value=""),
            patch(
                "teatree.core.push.fast_push.run_checked",
                return_value=SimpleNamespace(stdout="https://gitlab.com/Group/Repo/-/merge_requests/7"),
            ),
            patch("httpx.Client.post", return_value=_response("POST")) as request,
        ):
            forge.create_pr(branch="branch", title="title", body="first")
            with pytest.raises(WriteRefusedError):
                GitLabHTTPClient(token="CLIENT").post_json("projects/Group%2FRepo/merge_requests", {"title": "second"})
        request.assert_not_called()

    def test_graphql_label_ids_are_refused_before_post(self) -> None:
        with (
            self._setting({"label_allowlist": ["allowed"]}),
            patch("httpx.Client.post", return_value=_response("POST")) as request,
            pytest.raises(WriteRefusedError),
        ):
            GitLabHTTPClient(token="CLIENT").graphql("mutation { updateIssue }", {"input": {"labelIds": [1]}})
        request.assert_not_called()
