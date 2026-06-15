import logging
import re

from teatree.config import get_effective_settings

_log = logging.getLogger(__name__)

_BLOCKED_PHRASES: tuple[str, ...] = (
    "unable to test",
    "could not test",
    "couldn't test",
    "blocked",
    "dev verification pending",
    "verification pending",
    "not verified",
    "pending cred",
    "not automatable",
    "was unable to",
)


class BlockedTestPlanPostError(ValueError):
    pass


def _matching_phrase(body: str) -> str | None:
    lower = body.lower()
    return next((p for p in _BLOCKED_PHRASES if p in lower), None)


def _blocked_regexes() -> tuple[re.Pattern[str] | None, re.Pattern[str] | None]:
    settings = get_effective_settings()
    colleague_re = re.compile(settings.colleague_repo_url_pattern) if settings.colleague_repo_url_pattern else None
    solo_re = re.compile(settings.solo_repo_url_pattern) if settings.solo_repo_url_pattern else None
    return colleague_re, solo_re


def check_blocked_body(
    body: str,
    issue_url: str,
    *,
    colleague_re: re.Pattern[str] | None = None,
    solo_re: re.Pattern[str] | None = None,
) -> None:
    phrase = _matching_phrase(body)
    if phrase is None:
        return
    if colleague_re is not None and colleague_re.search(issue_url):
        msg = f"Refusing post to {issue_url}: body contains blocked phrase {phrase!r}."
        raise BlockedTestPlanPostError(msg)
    if solo_re is not None and solo_re.search(issue_url):
        _log.warning(
            "Test plan body contains blocked phrase %r on solo repo %s — proceeding.",
            phrase,
            issue_url,
        )


def check_blocked_body_from_config(body: str, issue_url: str) -> None:
    colleague_re, solo_re = _blocked_regexes()
    check_blocked_body(body, issue_url, colleague_re=colleague_re, solo_re=solo_re)
