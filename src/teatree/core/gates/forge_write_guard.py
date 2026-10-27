"""Optional durable policy for GitLab write transports."""

import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Never
from urllib.parse import unquote, urlparse

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from teatree.config import cold_reader
from teatree.core.self_forge_identities import issue_author_is_self, issue_author_login, self_identity_set
from teatree.core.send_proxy import record_allowed_write


class WriteRefusedError(RuntimeError):
    pass


def _refuse(reason: str) -> Never:
    raise WriteRefusedError(reason)


def item_key_of_endpoint(endpoint: str) -> str:
    parts = endpoint.lstrip("/").split("?", 1)[0].split("/")
    match parts:
        case ["projects", project, ("issues" | "merge_requests") as kind, iid, *_]:
            return f"{unquote(project).casefold()}/{kind}/{iid}"
        case ["projects", project, kind, *_]:
            return f"{unquote(project).casefold()}/{kind}"
        case ["projects", project]:
            return unquote(project).casefold()
        case _:
            return "gitlab"


def item_endpoint_of(endpoint: str) -> str:
    match endpoint.lstrip("/").split("?", 1)[0].split("/"):
        case ["projects", project, ("issues" | "merge_requests") as kind, iid, *_]:
            return f"projects/{project}/{kind}/{iid}"
        case _:
            return ""


def item_key_of_url(url: str) -> str:
    project, separator, tail = urlparse(url).path.strip("/").partition("/-/")
    if separator:
        kind, _, iid = tail.partition("/")
        return f"{project.casefold()}/{kind}/{iid}"
    return project.casefold()


@dataclass(frozen=True, slots=True)
class ForgeWriteGuard:
    cap_per_hour: int = 0
    cap_per_day: int = 0
    label_allowlist: frozenset[str] | None = None
    own_items_only: bool = False
    escape_quick_actions: bool = False
    receipt_channel: str = ""

    @classmethod
    def configured(cls) -> "ForgeWriteGuard | None":
        try:
            setting = cold_reader.read_setting_confirmed("gitlab_write_guard")
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            _refuse("GitLab write guard setting could not be read")
        if not setting.readable:
            _refuse("GitLab write guard setting could not be read")
        value = setting.value
        if value is None or value == {}:
            return None
        if not isinstance(value, dict) or set(value) - {
            "cap_per_hour",
            "cap_per_day",
            "label_allowlist",
            "own_items_only",
            "escape_quick_actions",
            "receipt_channel",
        }:
            _refuse("GitLab write guard setting could not be read")
        caps = [value.get(key, 0) for key in ("cap_per_hour", "cap_per_day")]
        flags = [value.get(key, False) for key in ("own_items_only", "escape_quick_actions")]
        labels = value.get("label_allowlist")
        channel = value.get("receipt_channel", "")
        if any(type(cap) is not int or cap < 0 for cap in caps) or any(type(flag) is not bool for flag in flags):
            _refuse("GitLab write guard setting could not be read")
        if labels is not None and (not isinstance(labels, list) or any(not isinstance(x, str) for x in labels)):
            _refuse("GitLab write guard setting could not be read")
        if not isinstance(channel, str):
            _refuse("GitLab write guard setting could not be read")
        return cls(caps[0], caps[1], frozenset(labels) if labels is not None else None, flags[0], flags[1], channel)

    def admit(self, item: str, action: str) -> None:
        SendAudit = apps.get_model("core", "SendAudit")
        now = timezone.now()
        try:
            with transaction.atomic():
                rows = SendAudit.objects.filter(channel="gitlab", target=item, action__startswith="guard:")
                for cap, window in ((self.cap_per_hour, timedelta(hours=1)), (self.cap_per_day, timedelta(days=1))):
                    if cap and rows.filter(created_at__gte=now - window).count() >= cap:
                        _refuse(f"GitLab write cap reached for {item}")
                if record_allowed_write(item=item, action=action) is None:
                    _refuse("GitLab write ledger unavailable")
        except WriteRefusedError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            _refuse("GitLab write ledger unavailable")

    def vet_labels(self, payload: object) -> None:
        if self.label_allowlist is None or not isinstance(payload, dict):
            return
        for key in ("labels", "add_labels", "remove_labels"):
            labels = payload.get(key)
            if labels is None:
                continue
            values = labels.split(",") if isinstance(labels, str) else labels
            if not isinstance(values, list) or any(
                not isinstance(x, str) or x.strip() not in self.label_allowlist for x in values
            ):
                msg = "GitLab label is outside the allowlist"
                raise WriteRefusedError(msg)
        if any("label" in str(key).casefold() and "id" in str(key).casefold() for key in payload):
            msg = "GitLab label ids cannot be checked against the allowlist"
            raise WriteRefusedError(msg)
        for value in payload.values():
            self.vet_labels(value)

    @staticmethod
    def require_own_item(read_json: Callable[[str], object], endpoint: str) -> None:
        item_endpoint = item_endpoint_of(endpoint)
        if not item_endpoint:
            return
        try:
            issue = read_json(item_endpoint)
            user = read_json("user")
            author = issue_author_login(issue)
            login = user.get("username", "") if isinstance(user, dict) else ""
            url = issue.get("web_url", "") if isinstance(issue, dict) else ""
            identities: set[str] = set()
            with suppress(Exception):
                identities = self_identity_set(url)
            if isinstance(login, str) and login:
                identities.add(login.casefold())
            if issue_author_is_self(author, identities):
                return
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            _refuse("GitLab item authorship could not be confirmed")
        msg = "GitLab item is not authored by this identity"
        raise WriteRefusedError(msg)

    def shape(self, text: str) -> str:
        return re.sub(r"^/", r"\\/", text, flags=re.MULTILINE) if self.escape_quick_actions else text


def vet_text_write(item: str, text: str) -> str:
    guard = ForgeWriteGuard.configured()
    if guard is None:
        return text
    shaped = guard.shape(text)
    guard.admit(item, "text")
    return shaped


def vet_gitlab_write(method: str, endpoint: str, payload: object, read_json: Callable[[str], object]) -> object:
    guard = ForgeWriteGuard.configured()
    if guard is None:
        return payload
    guard.vet_labels(payload)
    if guard.own_items_only and method in {"PUT", "DELETE"}:
        guard.require_own_item(read_json, endpoint)
    if isinstance(payload, dict):
        payload = {
            key: guard.shape(value) if key in {"body", "note", "description"} and isinstance(value, str) else value
            for key, value in payload.items()
        }
    guard.admit(item_key_of_endpoint(endpoint), method.lower())
    return payload
