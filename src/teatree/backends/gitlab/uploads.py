"""GitLab upload embedding + existence check — the post-evidence media concern (#2156, #2165).

GitLab claims (and therefore renders) an uploaded file only when the **saved
note markdown** carries the **relative** reference GitLab itself returns from
the upload API: ``![label](/uploads/<secret>/<file>)``. On save, GitLab's
reference scanner matches that relative ``/uploads/<secret>/...`` pattern,
claims the upload, and serves it — the rendered DOM ``<img>``/``<video>`` then
points at the absolute ``/-/project/<id>/uploads/...`` form and loads. An
**absolute** URL embedded directly in the raw markdown is NOT recognised by the
scanner, so the upload is never claimed and every browser route 404s (the #2165
regression this supersedes). :func:`verify_upload` therefore embeds the relative
``/uploads/<secret>/<file>`` reference.

The render correctness comes from GitLab claiming that relative reference on
save — NOT from the token fetch. :func:`verify_upload` still fetches the bytes
through the token-authenticated upload API (the web route a rendered
``<img>``/``<video>`` points at rejects a ``PRIVATE-TOKEN``, so it is unusable
here) as an **existence** guard: it proves the upload succeeded and is the right
media kind (magic-byte check), not that it renders.

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
# carrying the project id, used for the cross-project guard and to fetch the
# bytes for the existence check. The embedded reference is the RELATIVE form
# (``/uploads/<secret>/<file>``), which is what GitLab's scanner claims on save.
_UPLOAD_FULL_PATH_RE = re.compile(r"^/-/project/(?P<project_id>\d+)/uploads/(?P<secret>[0-9a-f]+)/(?P<filename>.+)$")

_HTTP_OK = 200


def _parse_upload(upload: RawAPIDict) -> tuple[int, str, str] | None:
    """Parse ``(project_id, secret, filename)`` from an upload response.

    Reads the ``full_path`` field (``/-/project/<id>/uploads/<secret>/<file>``)
    — the only field carrying the project id (needed for the cross-project guard
    and the token-fetch existence check). Returns ``None`` when the field is
    absent or malformed (e.g. an ``{"error": ...}`` response), so the caller
    fails the verification loudly rather than embedding a guess.
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


def verify_upload(client: GitLabAPI, *, project: ProjectInfo | None, upload: RawAPIDict) -> UploadVerification:
    """Existence-check an uploaded artifact and return the RELATIVE reference to embed.

    ``embed_url`` is the **relative** ``/uploads/<secret>/<file>`` reference
    GitLab returns — embedding *that* is what makes GitLab claim the upload on
    save so it renders (an absolute ``/-/project/...`` URL is never claimed and
    404s in a browser; #2165). ``ok`` is True only when the upload was re-fetched
    (token-authenticated) and its bytes magic-byte-match the artifact's media
    kind — an *existence + right-media-kind* guard, NOT a render guarantee (the
    token route returns 200 for an unclaimed upload too).

    *project* (the repo's resolved project, or ``None`` when unresolvable) is
    cross-checked against the upload's own project id so a response from the
    wrong project is caught before it embeds. The returned ``embed_url`` is
    populated even on an existence failure so the caller can name the artifact.
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
    # The relative reference GitLab returns (its ``url`` field) — the form its
    # scanner claims on save. Built from the parsed secret/filename so there is
    # one source of truth for the upload identity.
    embed_url = f"/uploads/{secret}/{filename}"
    status, content = client.fetch_upload(project_id, secret, filename)
    if status != _HTTP_OK:
        return UploadVerification(ok=False, embed_url=embed_url, detail=f"upload fetch returned HTTP {status}")
    kind = media_kind(filename)
    if not content_matches_kind(content[:MAGIC_PREFIX_LEN], kind):
        return UploadVerification(
            ok=False,
            embed_url=embed_url,
            detail=f"fetched bytes are not a {kind.value} ({len(content)} bytes)",
        )
    return UploadVerification(ok=True, embed_url=embed_url)
