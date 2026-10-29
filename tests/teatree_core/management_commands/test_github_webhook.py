import hashlib
import hmac
import json
import logging
from subprocess import CompletedProcess, TimeoutExpired
from typing import Literal, cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.http import HttpResponse
from django.test import Client, TestCase
from django.urls import reverse

import teatree.backends.github.api as github_api_mod
from teatree.core.management.commands._github_webhook_connect import ConnectResult, WebhookConnection, validate_target
from teatree.core.models import ConfigSetting, IncomingEvent, Loop, Mode
from teatree.core.views._webhook_secrets import reset_webhook_secrets
from teatree.forge_credentials import ForgeTokenResolution, ForgeTokenState
from teatree.utils.run import CommandFailedError
from teatree.utils.secrets import SecretStoreError, use_store
from tests.secret_memory_store import MemoryStore

URL = "https://hooks.example.org/hooks/github/"
KEY = "teatree/github-webhook/repository-101"
CANARY = "a" * 64
FORBIDDEN = "HTTP 403: Resource not accessible"


def post_signed_ping(client: Client, secret: str, delivery: str) -> HttpResponse:
    body = json.dumps({"repository": {"id": 101}}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        reverse("teatree:github_webhook"),
        data=body,
        content_type="application/json",
        headers={
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": "ping",
            "X-GitHub-Hook-Installation-Target-Type": "repository",
            "X-GitHub-Hook-Installation-Target-ID": "101",
            "X-Hub-Signature-256": signature,
        },
    )


class FakeGitHub:
    def __init__(self, test: TestCase) -> None:
        self.test = test
        self.hooks: list[dict[str, object]] = []
        self.calls: list[tuple[list[str], str]] = []
        self.refusals: dict[tuple[str, str], str] = {}
        self.timeouts: set[tuple[str, str]] = set()
        self.interrupts: set[tuple[str, str]] = set()
        self.delete_outcome: Literal["deleted", "leaves_hook", "already_gone"] = "deleted"
        self.stored_events: list[str] | None = None
        self.malformed_list = False
        self.other_hook_ping = False
        self.deliver_ping = True
        self.deliver_from_ping = 1
        self.pings = 0
        self.delivery = 0
        self.other_delivery = 0

    def config(self) -> dict[str, object]:
        return cast("dict[str, object]", self.hooks[0]["config"])

    def run(self, argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        stdin = str(kwargs.get("stdin_text") or "")
        self.calls.append((argv, stdin))
        endpoint = argv[2]
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        self._raise_if_refused(argv, endpoint, method)
        if endpoint.endswith("/pings"):
            response = self._ping()
        elif endpoint.endswith("/hooks/7"):
            response = self._hook(argv, method, stdin)
        elif endpoint == "repos/sample/repo/hooks":
            response = self._hooks(argv, method, stdin)
        elif endpoint == "repos/sample/repo":
            response = {"id": 101}
        else:
            raise AssertionError((argv, stdin))
        return CompletedProcess(argv, 0, stdout=json.dumps(response) if response is not None else "", stderr="")

    def _hooks(self, argv: list[str], method: str, stdin: str) -> object:
        if method == "GET":
            return (
                {"message": "unexpected"} if self.malformed_list else [self.hooks] if "--slurp" in argv else self.hooks
            )
        if method == "POST":
            hook = {"id": 7, **json.loads(stdin)}
            if self.stored_events is not None:
                hook["events"] = self.stored_events
            self.hooks.append(hook)
            return hook
        raise AssertionError((argv, stdin))

    def _hook(self, argv: list[str], method: str, stdin: str) -> object:
        if method == "DELETE":
            return self._delete(argv)
        if method == "PATCH":
            self.hooks[0].update(json.loads(stdin))
        return self.hooks[0]

    def _delete(self, argv: list[str]) -> None:
        if self.delete_outcome != "leaves_hook":
            self.hooks.clear()
        if self.delete_outcome == "already_gone":
            raise CommandFailedError(argv, 1, "", "HTTP 404: Not Found")

    def _ping(self) -> None:
        self.pings += 1
        if self.deliver_ping and self.pings >= self.deliver_from_ping:
            self._deliver_ping()
        if self.other_hook_ping:
            self.other_delivery += 1
            IncomingEvent.objects.create(
                source="github",
                event_name="ping",
                origin="webhook",
                repo_key="github:101",
                signer="integration-999",
                idempotency_key=f"github:other-hook-ping-{self.other_delivery}",
            )

    def refuse(self, method: str, suffix: str, stderr: str = FORBIDDEN) -> None:
        self.refusals[method, suffix] = stderr

    def _raise_if_refused(self, argv: list[str], endpoint: str, method: str) -> None:
        if any(method == hit_method and endpoint.endswith(suffix) for hit_method, suffix in self.interrupts):
            raise KeyboardInterrupt
        if any(method == timed_out_method and endpoint.endswith(suffix) for timed_out_method, suffix in self.timeouts):
            raise TimeoutExpired(argv, 1)
        for (refused_method, suffix), stderr in self.refusals.items():
            if method == refused_method and endpoint.endswith(suffix):
                raise CommandFailedError(argv, 1, "", stderr)

    def _deliver_ping(self) -> None:
        self.delivery += 1
        answer = post_signed_ping(self.test.client, str(self.config()["secret"]), f"ping-{self.delivery}")
        assert answer.status_code == 200


class GitHubWebhookCommandTests(TestCase):
    @pytest.fixture(autouse=True)
    def _capture(self, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
        self.capsys = capsys
        self.caplog = caplog

    def setUp(self) -> None:
        self.store = MemoryStore()
        self.enterContext(use_store(self.store))
        reset_webhook_secrets()
        self.fake = FakeGitHub(self)
        self.enterContext(patch("teatree.backends.github.api.run_checked", self.fake.run))
        self.enterContext(
            patch.object(
                github_api_mod,
                "_run_pooled_api",
                side_effect=lambda args, **kwargs: self.fake.run(list(args), **kwargs),
            ),
        )
        self.enterContext(
            patch(
                "teatree.core.management.commands.github_webhook.resolve_slug_token",
                return_value=ForgeTokenResolution(
                    "github_token", "t3-teatree", ForgeTokenState.TOKEN, token="test-token"
                ),
            ),
        )
        self.enterContext(
            patch("teatree.core.management.commands._github_webhook_connect.token_hex", return_value=CANARY)
        )
        self.enterContext(patch("teatree.core.management.commands._github_webhook_connect.sleep"))

    def connect(self) -> ConnectResult:
        return cast("ConnectResult", call_command("github_webhook", "connect", repo="sample/repo", url=URL))

    def test_connect_writes_secret_and_receiver_stores_signed_ping(self) -> None:
        result = self.connect()
        assert self.store.entries[KEY] == CANARY
        assert result["status"] == "created"
        assert IncomingEvent.objects.filter(source="github", repo_key="github:101", event_name="ping").exists()
        assert self.fake.hooks[0]["events"] == [
            "pull_request_review",
            "pull_request_review_comment",
            "issue_comment",
            "commit_comment",
            "pull_request",
            "issues",
            "check_suite",
            "workflow_run",
            "push",
        ]
        assert "ping" not in self.fake.hooks[0]["events"]
        assert all(stdin == "" for argv, stdin in self.fake.calls if argv[2].endswith("/pings"))

    def test_second_connect_reuses_secret_and_does_not_mutate_hook(self) -> None:
        self.connect()
        before = len(
            [
                call
                for call in self.fake.calls
                if "--method" in call[0] and "POST" in call[0] and not call[0][2].endswith("/pings")
            ]
        )
        with (
            patch("teatree.core.management.commands._github_webhook_connect.token_hex", return_value="b" * 64),
            patch.object(self.store, "write", wraps=self.store.write) as write,
        ):
            result = self.connect()
        write.assert_not_called()
        after = len(
            [
                call
                for call in self.fake.calls
                if "--method" in call[0] and "POST" in call[0] and not call[0][2].endswith("/pings")
            ]
        )
        assert result["status"] == "unchanged"
        assert (before, after) == (1, 1)
        assert self.store.entries[KEY] == CANARY
        assert not any(key.startswith(KEY + ".bak-") for key in self.store.entries)

    def test_connect_reconciles_old_check_run_subscription(self) -> None:
        self.connect()
        events = cast("list[str]", self.fake.hooks[0]["events"])
        events.append("check_run")
        before = sum("PATCH" in argv for argv, _ in self.fake.calls)

        result = self.connect()

        assert result["status"] == "updated"
        assert sum("PATCH" in argv for argv, _ in self.fake.calls) == before + 1
        assert self.fake.hooks[0]["events"] == [
            "pull_request_review",
            "pull_request_review_comment",
            "issue_comment",
            "commit_comment",
            "pull_request",
            "issues",
            "check_suite",
            "workflow_run",
            "push",
        ]
        assert self.store.entries[KEY] == CANARY

    def test_secret_only_reaches_stdin_and_store(self) -> None:
        self.caplog.set_level(logging.DEBUG)
        assert self.caplog.handler.level == logging.DEBUG
        self.connect()
        assert any(CANARY in stdin for _, stdin in self.fake.calls)
        assert CANARY not in str([argv for argv, _ in self.fake.calls])
        output = self.capsys.readouterr()
        assert CANARY not in output.out + output.err + self.caplog.text

    def test_missing_hook_permission_refuses_before_store_write(self) -> None:
        self.fake.refuse("GET", "/hooks")
        with pytest.raises(SystemExit) as error:
            self.connect()
        assert error.value.code == 1
        assert self.store.entries == {}
        assert "Webhooks permission" in self.capsys.readouterr().err

    def test_unreadable_hook_list_refuses_before_store_write(self) -> None:
        self.fake.malformed_list = True
        with pytest.raises(SystemExit):
            self.connect()
        assert self.store.entries == {}

    def test_create_permission_failure_names_webhooks_permission(self) -> None:
        self.fake.refuse("POST", "/hooks")
        with pytest.raises(SystemExit):
            self.connect()
        assert "Webhooks permission" in self.capsys.readouterr().err
        assert KEY not in self.store.entries

    def test_failed_secret_replacement_restores_store_and_plain_connect_repairs_drift(self) -> None:
        self.connect()
        self.fake.hooks[0]["active"] = False
        self.fake.refuse("PATCH", "/hooks/7")
        with (
            patch("teatree.core.management.commands._github_webhook_connect.token_hex", return_value="b" * 64),
            pytest.raises(SystemExit) as error,
        ):
            call_command("github_webhook", "connect", repo="sample/repo", url=URL, replace_secret=True)
        assert error.value.code == 1
        assert self.store.entries == {KEY: CANARY}
        assert self.fake.config()["secret"] == CANARY
        assert "Backed up" not in self.capsys.readouterr().err
        self.fake.refusals.clear()
        reset_webhook_secrets()
        before = sum("PATCH" in argv for argv, _ in self.fake.calls)
        result = self.connect()
        assert result["status"] == "updated"
        assert sum("PATCH" in argv for argv, _ in self.fake.calls) == before + 1
        assert "unchanged" not in self.capsys.readouterr().out

    def test_update_timeout_restores_old_secret_and_reports_plain_error(self) -> None:
        self.connect()
        self.fake.timeouts.add(("PATCH", "/hooks/7"))
        with (
            patch("teatree.core.management.commands._github_webhook_connect.token_hex", return_value="b" * 64),
            pytest.raises(SystemExit) as error,
        ):
            call_command("github_webhook", "connect", repo="sample/repo", url=URL, replace_secret=True)
        assert error.value.code == 1
        assert self.store.entries == {KEY: CANARY}
        output = self.capsys.readouterr().err
        assert "timed out" in output.lower()
        assert "may have been updated" in output
        assert "--replace-secret" in output
        assert "Traceback" not in output

    def test_disconnect_timeout_gives_no_replace_secret_hint(self) -> None:
        self.connect()
        self.fake.timeouts.add(("DELETE", "/hooks/7"))
        with pytest.raises(SystemExit):
            call_command("github_webhook", "disconnect", repo="sample/repo", url=URL)
        output = self.capsys.readouterr().err
        assert "timed out" in output.lower()
        assert "--replace-secret" not in output

    def test_create_timeout_removes_new_secret_and_reports_plain_error(self) -> None:
        self.fake.timeouts.add(("POST", "/hooks"))
        with pytest.raises(SystemExit) as error:
            self.connect()
        assert error.value.code == 1
        assert self.store.entries == {}
        assert "timed out" in self.capsys.readouterr().err.lower()

    def test_failed_create_restores_prior_empty_entry(self) -> None:
        self.store.entries[KEY] = ""
        self.fake.refuse("POST", "/hooks")
        with pytest.raises(SystemExit):
            self.connect()
        assert self.store.entries == {KEY: ""}

    def test_failed_update_with_no_prior_secret_removes_new_secret(self) -> None:
        self.connect()
        self.store.entries.clear()
        reset_webhook_secrets()
        self.fake.hooks[0]["active"] = False
        self.fake.refuse("PATCH", "/hooks/7")
        with pytest.raises(SystemExit):
            self.connect()
        assert self.store.entries == {}

    def replace_secret_with(self, secret: str) -> None:
        with patch("teatree.core.management.commands._github_webhook_connect.token_hex", return_value=secret):
            call_command("github_webhook", "connect", repo="sample/repo", url=URL, replace_secret=True)

    def backups(self) -> dict[str, str]:
        return {key: value for key, value in self.store.entries.items() if key.startswith(KEY + ".bak-")}

    def test_failed_restore_keeps_backup_and_reports_original_error(self) -> None:
        self.connect()
        self.fake.refuse("PATCH", "/hooks/7", "HTTP 500: Server Error")
        original_write = self.store.write

        def fail_restore(key: str, value: str) -> bool:
            return False if (key, value) == (KEY, CANARY) else original_write(key, value)

        with patch.object(self.store, "write", side_effect=fail_restore), pytest.raises(SystemExit) as error:
            self.replace_secret_with("b" * 64)
        assert error.value.code == 1
        message = self.capsys.readouterr().err
        ((backup_key, backup_value),) = self.backups().items()
        assert backup_value == CANARY
        assert backup_key in message
        assert "HTTP 500: Server Error" in message
        assert "could not restore" in message
        assert "rerun with --replace-secret" in message
        assert self.store.entries[KEY] == "b" * 64
        assert "b" * 64 not in message
        assert CANARY not in message

    def test_restore_that_times_out_keeps_backup_and_reports_original_error(self) -> None:
        self.connect()
        self.fake.refuse("PATCH", "/hooks/7", "HTTP 500: Server Error")
        original_write = self.store.write

        def hang_on_restore(key: str, value: str) -> bool:
            if (key, value) == (KEY, CANARY):
                raise SecretStoreError.timed_out(key, 1)
            return original_write(key, value)

        with patch.object(self.store, "write", side_effect=hang_on_restore), pytest.raises(SystemExit) as error:
            self.replace_secret_with("b" * 64)
        assert error.value.code == 1
        message = self.capsys.readouterr().err
        ((backup_key, backup_value),) = self.backups().items()
        assert backup_value == CANARY
        assert backup_key in message
        assert "HTTP 500: Server Error" in message
        assert "timed out" in message
        assert "rerun with --replace-secret" in message

    def test_failed_removal_of_a_new_secret_names_the_error_and_needs_no_backup(self) -> None:
        self.fake.refuse("POST", "/hooks", "HTTP 500: Server Error")
        with patch.object(self.store, "remove", return_value=False), pytest.raises(SystemExit):
            self.connect()
        message = self.capsys.readouterr().err
        assert "HTTP 500: Server Error" in message
        assert "could not restore" in message
        assert "still in" not in message
        assert self.store.entries[KEY] == CANARY

    def test_backup_that_cannot_be_removed_after_restore_keeps_the_hook_error(self) -> None:
        self.connect()
        self.fake.refuse("PATCH", "/hooks/7", "HTTP 500: Server Error")
        with (
            patch.object(self.store, "remove", side_effect=SecretStoreError.timed_out(KEY, 1)),
            pytest.raises(SystemExit),
        ):
            self.replace_secret_with("b" * 64)
        message = self.capsys.readouterr().err
        assert "HTTP 500: Server Error" in message
        assert "could not be removed" in message
        assert self.store.entries[KEY] == CANARY
        assert self.backups() != {}

    def test_interrupted_update_restores_store_and_reraises(self) -> None:
        self.connect()
        self.fake.interrupts.add(("PATCH", "/hooks/7"))
        connection = WebhookConnection("sample/repo", URL, "test-token", echo=lambda _: None)
        with (
            patch("teatree.core.management.commands._github_webhook_connect.token_hex", return_value="b" * 64),
            pytest.raises(KeyboardInterrupt),
        ):
            connection.connect(replace_secret=True)
        assert self.store.entries == {KEY: CANARY}
        assert self.fake.config()["secret"] == CANARY

    def test_connect_with_stored_secret_and_no_hook_creates_hook_with_stored_value(self) -> None:
        self.store.entries[KEY] = "c" * 64
        with patch.object(self.store, "write", wraps=self.store.write) as write:
            result = self.connect()
        write.assert_not_called()
        assert result["status"] == "created"
        assert self.fake.config()["secret"] == "c" * 64
        assert self.store.entries == {KEY: "c" * 64}

    def test_failed_update_with_reused_secret_writes_nothing_to_the_store(self) -> None:
        self.connect()
        self.fake.hooks[0]["active"] = False
        self.fake.refuse("PATCH", "/hooks/7")
        with (
            patch.object(self.store, "write", wraps=self.store.write) as write,
            patch.object(self.store, "remove", wraps=self.store.remove) as remove,
            pytest.raises(SystemExit),
        ):
            self.connect()
        write.assert_not_called()
        remove.assert_not_called()
        assert self.store.entries == {KEY: CANARY}

    def test_non_public_url_refuses_before_network_and_store(self) -> None:
        private_ip_urls = (
            "https://10.0.0.1/hooks/github/",  # privacy-scan:allow RFC1918 URL-validation fixture
            "https://169.254.169.254/hooks/github/",  # privacy-scan:allow link-local URL-validation fixture
        )
        bad_urls = (
            "http://hooks.example.org/hooks/github/",
            "https://example.org/other/",
            "https://localhost/hooks/github/",
            "https://hooks.local/hooks/github/",
            "https://hooks.internal/hooks/github/",
            "https://singlelabel/hooks/github/",
            "https://127.0.0.1/hooks/github/",
            *private_ip_urls,
            "https://192.0.2.2/hooks/github/",  # privacy-scan:allow documentation address for URL validation
            "https://[::1]/hooks/github/",
            "https://user@hooks.example.org/hooks/github/",
            "https://hooks.example.org/hooks/github/?key=value",
            "https://hooks.example.org/hooks/github/#part",
            "https://hooks.example.org:bad/hooks/github/",
        )
        for bad_url in bad_urls:
            with self.subTest(url=bad_url), pytest.raises(SystemExit):
                call_command("github_webhook", "connect", repo="sample/repo", url=bad_url)
            if bad_url in private_ip_urls:
                assert "HTTPS" in self.capsys.readouterr().err
        assert self.fake.calls == []
        assert self.store.entries == {}

    def test_unrouted_token_refuses_before_network_and_store(self) -> None:
        resolution = ForgeTokenResolution("github_token", "t3-teatree", ForgeTokenState.UNSET)
        with (
            patch("teatree.core.management.commands.github_webhook.resolve_slug_token", return_value=resolution),
            pytest.raises(SystemExit),
        ):
            self.connect()
        assert "github_token_pass_key is unset" in self.capsys.readouterr().err
        assert self.fake.calls == []
        assert self.store.entries == {}

    def test_connect_does_not_select_transport_or_change_loops(self) -> None:
        before = tuple(model.objects.count() for model in (ConfigSetting, Mode, Loop))
        self.connect()
        assert tuple(model.objects.count() for model in (ConfigSetting, Mode, Loop)) == before
        assert IncomingEvent.objects.filter(source="github", event_name="ping").exists()
        assert len(self.fake.hooks) == 1

    def test_disconnect_removes_hook_then_secret(self) -> None:
        self.connect()
        result = cast("ConnectResult", call_command("github_webhook", "disconnect", repo="sample/repo", url=URL))
        assert result["status"] == "disconnected"
        assert self.fake.hooks == []
        assert KEY not in self.store.entries

    def test_receiver_refuses_the_removed_secret_once_its_cache_expires(self) -> None:
        self.connect()
        call_command("github_webhook", "disconnect", repo="sample/repo", url=URL)
        reset_webhook_secrets()
        assert post_signed_ping(self.client, CANARY, "after-disconnect").status_code == 401

    def test_disconnect_flag_removes_hook_and_secret(self) -> None:
        self.connect()
        result = cast(
            "ConnectResult", call_command("github_webhook", "connect", repo="sample/repo", url=URL, disconnect=True)
        )
        assert result["status"] == "disconnected"
        assert self.fake.hooks == []
        assert KEY not in self.store.entries

    def test_missing_ping_fails_and_keeps_hook_and_secret(self) -> None:
        self.fake.deliver_ping = False
        with pytest.raises(SystemExit):
            self.connect()
        assert len(self.fake.hooks) == 1
        assert self.store.entries[KEY] == CANARY

    def test_old_ping_cannot_verify_a_new_attempt(self) -> None:
        self.connect()
        self.fake.deliver_ping = False
        with pytest.raises(SystemExit):
            self.connect()

    def test_ping_retries_past_receiver_secret_refresh_window(self) -> None:
        self.fake.deliver_from_ping = 5
        result = self.connect()
        assert result["ping_verified"]
        assert self.fake.pings == 5

    def test_ping_from_another_hook_cannot_verify_this_hook(self) -> None:
        self.fake.deliver_ping = False
        self.fake.other_hook_ping = True
        with pytest.raises(SystemExit):
            self.connect()
        assert IncomingEvent.objects.filter(signer="integration-999", event_name="ping").exists()

    def test_drift_is_patched_with_full_secret(self) -> None:
        self.connect()
        self.fake.hooks[0]["active"] = False
        result = self.connect()
        assert result["status"] == "updated"
        assert self.fake.config()["secret"] == CANARY
        assert self.fake.hooks[0]["active"]

    def test_replace_secret_backs_up_and_updates_hook(self) -> None:
        self.connect()
        with patch("teatree.core.management.commands._github_webhook_connect.token_hex", return_value="b" * 64):
            result = cast(
                "ConnectResult",
                call_command("github_webhook", "connect", repo="sample/repo", url=URL, replace_secret=True),
            )
        assert result["status"] == "updated"
        assert self.store.entries[KEY] == "b" * 64
        assert any(key.startswith(KEY + ".bak-") and value == CANARY for key, value in self.store.entries.items())
        assert self.fake.config()["secret"] == "b" * 64

    def test_delete_failure_keeps_secret(self) -> None:
        self.connect()
        self.fake.refuse("DELETE", "/hooks/7")
        with pytest.raises(SystemExit):
            call_command("github_webhook", "disconnect", repo="sample/repo", url=URL)
        assert self.store.entries[KEY] == CANARY

    def test_conflicting_disconnect_and_replace_secret_refuse_before_network(self) -> None:
        with pytest.raises(SystemExit):
            call_command("github_webhook", "connect", repo="sample/repo", url=URL, disconnect=True, replace_secret=True)
        assert "cannot be combined" in self.capsys.readouterr().err
        assert self.fake.calls == []

    def test_invalid_repository_slug_refuses_before_network(self) -> None:
        for bad_repo in ("", "sample", "sample/repo/extra", "../repo", "sample/..", "sam ple/repo"):
            with self.subTest(repo=bad_repo), pytest.raises(SystemExit):
                call_command("github_webhook", "connect", repo=bad_repo, url=URL)
        assert self.fake.calls == []

    def test_public_ip_url_with_port_is_accepted(self) -> None:
        validate_target("sample/repo", "https://8.8.8.8:8443/hooks/github/")

    def test_server_error_is_not_reported_as_missing_permission(self) -> None:
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                self.fake.refusals = {(method, "/hooks"): "HTTP 500: Server Error"}
                with pytest.raises(SystemExit):
                    self.connect()
                error = self.capsys.readouterr().err
                assert "HTTP 500" in error
                assert "Webhooks permission" not in error

    def test_hook_that_does_not_match_after_write_fails_before_ping(self) -> None:
        self.fake.stored_events = ["push"]
        with pytest.raises(SystemExit):
            self.connect()
        assert "did not match after write" in self.capsys.readouterr().err
        assert self.fake.pings == 0
        assert self.store.entries == {}

    def test_json_output_is_only_the_result(self) -> None:
        call_command("github_webhook", "connect", repo="sample/repo", url=URL, json_output=True)
        assert json.loads(self.capsys.readouterr().out) == {
            "repo": "sample/repo",
            "hook_id": 7,
            "status": "created",
            "ping_verified": True,
        }

    def test_disconnect_without_a_matching_hook_changes_nothing(self) -> None:
        self.connect()
        other = "https://other.example.org/hooks/github/"
        result = cast("ConnectResult", call_command("github_webhook", "disconnect", repo="sample/repo", url=other))
        assert result["status"] == "absent"
        assert len(self.fake.hooks) == 1
        assert self.store.entries[KEY] == CANARY

    def test_disconnect_fails_when_the_hook_survives_the_delete(self) -> None:
        self.connect()
        self.fake.delete_outcome = "leaves_hook"
        with pytest.raises(SystemExit):
            call_command("github_webhook", "disconnect", repo="sample/repo", url=URL)
        assert "still appears" in self.capsys.readouterr().err
        assert self.store.entries[KEY] == CANARY

    def test_disconnect_tolerates_a_hook_that_vanished_during_the_delete(self) -> None:
        self.connect()
        self.fake.delete_outcome = "already_gone"
        result = cast("ConnectResult", call_command("github_webhook", "disconnect", repo="sample/repo", url=URL))
        assert result["status"] == "disconnected"
        assert KEY not in self.store.entries

    def test_disconnect_names_a_secret_the_store_could_not_remove(self) -> None:
        self.connect()
        with patch.object(self.store, "remove", return_value=False), pytest.raises(SystemExit):
            call_command("github_webhook", "disconnect", repo="sample/repo", url=URL)
        assert KEY in self.capsys.readouterr().err
        assert self.fake.hooks == []

    def test_disconnect_succeeds_when_the_secret_was_already_gone(self) -> None:
        self.connect()
        del self.store.entries[KEY]
        result = cast("ConnectResult", call_command("github_webhook", "disconnect", repo="sample/repo", url=URL))
        assert result["status"] == "disconnected"
