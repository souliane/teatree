"""GitLab upload embedding + render verification — the post-evidence media concern (#2156).

The relative ``/uploads/<secret>/<file>`` markdown GitLab returns for an upload
renders only when the markdown renderer applies project context to rewrite it;
in the work-items UI that rewrite is unreliable, so a relative embed shows a
broken image / a dead video player. :func:`verify_upload` instead builds the
absolute ``https://<host>/-/project/<id>/uploads/<secret>/<file>`` form (GitLab's
own rewrite target, which renders context-independently) and proves it resolves
by fetching the bytes through the token-authenticated upload API — the web route
a rendered ``<img>``/``<video>`` points at rejects a ``PRIVATE-TOKEN``, so it is
unusable for verification — then magic-byte-checks them against the artifact's
media kind.

These free functions hold that machinery so
:class:`teatree.backends.gitlab.GitLabCodeHost` delegates with its injected
``GitLabAPI`` client (the same shape as :mod:`subissues` and the merge RPC),
keeping the host class focused on the cross-host Protocol surface.
"""

import re

from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.core.backend_protocols import UploadVerification
from teatree.types import RawAPIDict
from teatree.utils.media import MAGIC_PREFIX_LEN, content_matches_kind, media_kind

# The absolute ``full_path`` GitLab returns for an upload — the only field
# carrying the project id, and the form that renders context-independently.
_UPLOAD_FULL_PATH_RE = re.compile(r"^/-/project/(?P<project_id>\d+)/uploads/(?P<secret>[0-9a-f]+)/(?P<filename>.+)$")

_HTTP_OK = 200


def _parse_upload(upload: RawAPIDict) -> tuple[int, str, str] | None:
    """Parse ``(project_id, secret, filename)`` from an upload response.

    Reads the ``full_path`` field (``/-/project/<id>/uploads/<secret>/<file>``)
    — the only field carrying the project id. Returns ``None`` when the field
    is absent or malformed (e.g. an ``{"error": ...}`` response), so the
    caller fails the verification loudly rather than embedding a guess.
    """
    full_path = upload.get("full_path")
    if not isinstance(full_path, str):
        return None
    match = _UPLOAD_FULL_PATH_RE.match(full_path)
    if match is None:
        return None
    return int(match.group("project_id")), match.group("secret"), match.group("filename")


def upload_file(client: GitLabAPI, *, project: ProjectInfo | None, repo: str, filepath: str) -> RawAPIDict:
    """Upload *filepath* to *project*'s uploads, or return a resolution error."""
    if project is None:
        return {"error": f"Could not resolve project: {repo}"}
    return client.upload_file(project.project_id, filepath) or {}


def _web_host(client: GitLabAPI) -> str:
    """The GitLab web origin (``https://gitlab.com``) derived from the API base."""
    return client.base_url.split("/api/", 1)[0].rstrip("/")


def verify_upload(client: GitLabAPI, *, project: ProjectInfo | None, upload: RawAPIDict) -> UploadVerification:
    """Confirm an uploaded artifact resolves + renders, and return its embed URL.

    Returns the absolute embed URL and an ``ok`` that is True only when the
    upload was re-fetched (token-authenticated) and its bytes magic-byte-match
    the artifact's media kind. *project* (the repo's resolved project, or
    ``None`` when unresolvable) is cross-checked against the upload's own
    project id so a response from the wrong project — a relative ``/uploads/``
    upload silently lands cross-project — is caught before it embeds an
    unrenderable URL.
    """
    parsed = _parse_upload(upload)
    if parsed is None:
        return UploadVerification(ok=False, embed_url="", detail=f"unparsable upload response: {upload}")
    project_id, secret, filename = parsed
    if project is not None and project.project_id != project_id:
        return UploadVerification(
            ok=False,
            embed_url="",
            detail=f"upload landed on project {project_id}, expected {project.project_id}",
        )
    embed_url = f"{_web_host(client)}/-/project/{project_id}/uploads/{secret}/{filename}"
    status, content = client.fetch_upload(project_id, secret, filename)
    if status != _HTTP_OK:
        return UploadVerification(ok=False, embed_url=embed_url, detail=f"upload fetch returned HTTP {status}")
    kind = media_kind(filename)
    if not content_matches_kind(content[:MAGIC_PREFIX_LEN], kind):
        return UploadVerification(
            ok=False,
            embed_url=embed_url,
            detail=f"fetched bytes are not a renderable {kind.value} ({len(content)} bytes)",
        )
    return UploadVerification(ok=True, embed_url=embed_url)
