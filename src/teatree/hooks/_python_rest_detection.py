r"""Python REST-publish detection for the pre-publish gates (#2943 gap).

``_command_parser.is_publish_command`` gated ALL scanning (banned-terms
#1415, quote-scanner #1213) on a leader-keyed catalogue of ``gh``/``glab``/
``git``/``curl`` shapes -- a ``python3``/``python``-led segment POSTing/
PATCHing to a forge REST API (``requests``/``httpx``/``urllib``/a raw
``http.client`` call) was never recognised as a publish action at all, so
the leak-prevention scan never ran against it, on ANY repo, public or
private (the "Post or Update Note with Images" recipe in
``skills/platforms/references/gitlab.md``, added by #2943, is exactly this
shape).

``segment_is_python_rest_publish`` / ``command_has_python_rest_publish_surface``
generalise the SAME write-method + forge-target two-part test
``_publish_detection.segment_is_api_write`` already applies to a ``gh``/
``glab api`` call, to a script the CLI-specific argument walkers cannot
parse a ``--repo``/URL flag out of: a python REST client hitting a forge's
REST API is structurally the same shape as a raw ``curl`` POST, just
authored in Python instead of CLI flags.

``find_python_forge_rest_urls`` is the shared URL-shape resolver: it mirrors
the ``gh api``/``glab api`` ``repos/``/``projects/`` path resolution
(``publish_destination._destination_from_api``) so both the write-verb
classifier here (existence check) and ``publish_destination`` (slug
extraction) stay in lock-step.

Split out of ``_publish_detection`` to keep that module under the project's
per-file public-function ceiling (``scripts/hooks/check_module_health.py``).
"""

import re
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Final

from teatree.hooks._publish_detection import segment_word_lists

# A ``python3``/``python``-led segment is structurally the same publish shape
# ``segment_is_api_write`` already recognises for ``gh api``/``glab api`` -- a
# write verb plus a forge-targeted endpoint -- just authored in Python
# instead of CLI flags.
_PYTHON_LEADER_RE: Final[re.Pattern[str]] = re.compile(r"python\d*(?:\.\d+)?")


def is_python_leader(word: str) -> bool:
    """Return True iff ``word`` names a python interpreter, bare or path-qualified.

    Matches ``python``, ``python3``, ``python3.11`` and a path-qualified form
    (``/usr/bin/python3``) via the basename, mirroring how ``t3`` path-form
    leaders are canonicalised elsewhere. A merely-prefixed word (``pythonic``,
    ``python-is-fun``) is rejected by requiring a FULL match on the basename.
    """
    return bool(_PYTHON_LEADER_RE.fullmatch(PurePosixPath(word).name))


# GitHub's ``repos/<owner>/<repo>`` path alone is too generic a shape to trust
# on an arbitrary host, so GitHub resolution additionally requires the URL's
# host be a recognised GitHub endpoint. GitLab's versioned
# ``api/v<N>/projects/<url-encoded-slug>`` path is distinctive enough to trust
# regardless of host -- this is what lets a self-hosted GitLab instance
# resolve the same way ``gitlab.com`` does, with no host allowlist / config
# lookup needed in this pure-detection module (a configured self-hosted
# GitLab host's PRIVACY is a ``publish_destination``/``private_repos``
# concern, not a detection-shape one).
_GITHUB_REST_HOSTS: Final[frozenset[str]] = frozenset({"github.com", "api.github.com"})
_GITHUB_REPOS_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://(?P<host>[^/\s'\"]+)/(?:api/v\d+/)?repos/(?P<slug>[^/\s'\"]+/[^/\s'\"]+)",
)
_GITLAB_PROJECTS_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^https?://[^/\s'\"]+/api/v\d+/projects/(?P<slug>[^/\s'\"?]+)",
)
_URL_LITERAL_RE: Final[re.Pattern[str]] = re.compile(r"https?://[^\s'\"]+")


def find_python_forge_rest_urls(source: str) -> Iterator[tuple[str, str]]:
    """Yield ``(forge, slug)`` for each forge-shaped REST URL literal in ``source``.

    Mirrors the ``gh api``/``glab api`` URL-path resolution
    (``publish_destination._destination_from_api``) applied to a URL LITERAL
    embedded in a python REST script, so both a raw-REST CLI call and a python
    REST client resolve their target the same way. Shared by the write-verb
    classifier below (existence check) and ``publish_destination`` (slug
    extraction), so the two stay in lock-step. A dynamically-built URL
    (string concatenation, an f-string variable) carries no literal
    ``https?://`` substring and yields nothing -- the target is genuinely
    unresolvable, not private, so callers must treat it as PUBLIC/unproven
    rather than infer privacy from a shape it cannot read.
    """
    for match in _URL_LITERAL_RE.finditer(source):
        url = match.group(0)
        gh_match = _GITHUB_REPOS_URL_RE.match(url)
        if gh_match and gh_match.group("host").lower() in _GITHUB_REST_HOSTS:
            yield "github", gh_match.group("slug")
            continue
        glab_match = _GITLAB_PROJECTS_URL_RE.match(url)
        if glab_match:
            yield "gitlab", glab_match.group("slug").replace("%2F", "/").replace("%2f", "/")


# Write-verb signals a python REST client carries, mirroring the effective-
# method resolution ``_publish_detection._api_effective_method`` applies to
# ``gh``/``glab api``: an explicit client-library write call (``requests``/
# ``httpx`` ``.post``/``.patch``), an explicit ``method=`` kwarg
# (``urllib.request.Request``), or -- for a raw ``http.client``/socket call
# with no recognised client-library name -- a write-method string literal
# alongside a forge auth-header pattern (``Authorization``/
# ``PRIVATE-TOKEN``), per the "raw HTTP calls with an Authorization header
# pattern" shape.
_PYTHON_WRITE_CALL_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:requests|httpx)\.(?:post|patch)\s*\(")
_PYTHON_WRITE_METHOD_KWARG_RE: Final[re.Pattern[str]] = re.compile(
    r"method\s*=\s*['\"](?:POST|PATCH|PUT|DELETE)['\"]",
    re.IGNORECASE,
)
_PYTHON_WRITE_METHOD_LITERAL_RE: Final[re.Pattern[str]] = re.compile(r"['\"](?:POST|PATCH|PUT|DELETE)['\"]")
_PYTHON_AUTH_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:Authorization|PRIVATE-TOKEN)\b", re.IGNORECASE)


def _python_source_has_write_signal(source: str) -> bool:
    """Return True iff ``source`` carries a python REST write-verb signal."""
    if _PYTHON_WRITE_CALL_RE.search(source):
        return True
    if _PYTHON_WRITE_METHOD_KWARG_RE.search(source):
        return True
    return bool(_PYTHON_WRITE_METHOD_LITERAL_RE.search(source)) and bool(_PYTHON_AUTH_HEADER_RE.search(source))


_HEREDOC_WORD_PREFIX: Final[str] = "<<"


def segment_is_python_rest_publish(words: list[str], command: str) -> bool:
    """Return True iff ``words`` is a python-led segment POSTing/PATCHing a forge REST API.

    The write-method + forge-target two-part test, mirroring
    ``_publish_detection.segment_is_api_write``. ``command`` is the full raw
    command text: a heredoc-fed script's body lives on subsequent physical
    lines the lexer does not tokenize into WORDs (mirrors
    ``extract_bash_payload``'s separate unconditional heredoc-body pass), so
    a heredoc-carrying segment (its last WORD glued to a ``<<`` operator)
    falls back to searching the raw command text -- the only place that body
    is readable -- instead of the (heredoc-body-less) tokenized words alone.
    """
    if not words or not is_python_leader(words[0]):
        return False
    source = " ".join(words[1:])
    if any(word.startswith(_HEREDOC_WORD_PREFIX) for word in words[1:]):
        source = f"{source}\n{command}"
    return _python_source_has_write_signal(source) and next(find_python_forge_rest_urls(source), None) is not None


def command_has_python_rest_publish_surface(command: str) -> bool:
    """Return True iff any segment is a python-led REST-publish write.

    The whole-command counterpart of
    ``_publish_detection.command_has_token_aware_publish_surface``, used by
    ``_command_parser.is_publish_command`` to catch a python REST client the
    CLI-specific detectors never see.
    """
    return any(segment_is_python_rest_publish(words, command) for words in segment_word_lists(command))
