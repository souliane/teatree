"""E2E test-plan regression predicates for the pinned-regression corpus.

The two deterministic checks guarding the ``e2e post-test-plan`` media path live
here (split out of :mod:`teatree.eval.regression_corpus` to keep that module
under the health cap): the embed uses the claimable relative ``/uploads`` ref,
and artifacts upload to the note's OWN project. Each returns ``True`` when the
real backend code path still honors the invariant; both stay in the eval layer
by exercising :mod:`teatree.backends.gitlab` only (never the management command).
"""

from unittest.mock import MagicMock

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab import uploads as _uploads
from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo

__all__ = [
    "check_e2e_test_plan_embeds_claimable_relative_ref",
    "check_e2e_test_plan_uploads_to_note_project",
]


def check_e2e_test_plan_embeds_claimable_relative_ref() -> bool:
    """#2165: the test-plan embed uses the CLAIMABLE relative /uploads ref.

    GitLab claims (and renders) an upload only when the SAVED note markdown
    carries the relative ``/uploads/<secret>/<file>`` reference its scanner
    recognises. PR #2165 embedded the ABSOLUTE
    ``https://<host>/-/project/<id>/uploads/...`` form directly, which the
    scanner does NOT recognise, so the upload is never claimed and every browser
    route 404s (broken image + dead video). The fixed real path
    (:func:`teatree.backends.gitlab.uploads.verify_upload`, the single source of
    the embedded reference that ``_verified_embed`` wraps as ``![label](ref)``)
    must choose the relative ``/uploads/<secret>/<file>`` reference and NEVER an
    absolute ``/-/project/`` or any ``https://`` upload URL.

    Anti-vacuity: restore the absolute-embed form (embed ``full_path`` or an
    ``https://`` upload URL) in ``verify_upload`` and this check goes RED.
    """
    upload = {
        "url": "/uploads/deadbeefcafe/shot.png",
        "full_path": "/-/project/42/uploads/deadbeefcafe/shot.png",
        "markdown": "![shot](/uploads/deadbeefcafe/shot.png)",
    }
    # A real GitLabAPI client (stubbed transport): the 200 + PNG magic bytes
    # pass the existence check, so the embedded ref is whatever the REAL
    # verify_upload chose — the assertion is on that choice.
    client = MagicMock()
    client.base_url = "https://gitlab.com/api/v4"
    client.fetch_upload.return_value = (200, b"\x89PNG\r\n\x1a\n" + b"rest")

    verification = _uploads.verify_upload(client, project=None, upload=upload)
    embed = f"![before]({verification.embed_url})"
    return (
        verification.ok
        and "](/uploads/deadbeefcafe/shot.png)" in embed
        and "/-/project/" not in embed
        and "https://" not in embed
    )


def check_e2e_test_plan_uploads_to_note_project() -> bool:
    """Test-plan artifacts upload to the note's OWN project, not the manifest's 2nd repo.

    Live-run bug: the note was created on the ticket's project (the issue URL's
    project) but every artifact uploaded to a *different* project (the overlay CI
    project — often the second repo in a multi-repo manifest). GitLab serves a
    note's relative ``/uploads/<secret>/<file>`` reference from the NOTE'S OWN
    project namespace, so uploads on a different project are invisible to the note
    and every image 404s.

    The fix routes the upload target through the backend layer: the command
    resolves the upload project from ``issue_url`` via
    :meth:`CodeHostBackend.repo_for_issue_url` (the note's own project), and the
    backend's ``verify_upload`` cross-project guard rejects any upload whose
    response landed on a different project than the resolved one. This check
    exercises both backend invariants (the eval layer may import
    ``teatree.backends`` but not the ``teatree.core.management`` command).
    First: ``repo_for_issue_url`` of a GitLab issue/work-item URL is the URL's
    own project slug — so the command uploads to the note's project. Second:
    ``verify_upload`` flags a response that landed on a different project than
    the (note's) project handed to it — the silent-wrong-project guard.

    Anti-vacuity: make ``repo_for_issue_url`` return a constant / the wrong slug,
    or drop the cross-project guard in ``verify_upload``, and this check goes RED.
    """
    host = GitLabCodeHost(client=MagicMock(spec=GitLabAPI))
    # 1) the upload project the command will use is the note's OWN project.
    note_project = host.repo_for_issue_url("https://gitlab.com/group/client/-/issues/8521")
    resolves_to_note_project = note_project == "group/client"

    # 2) the cross-project guard rejects an upload that landed elsewhere — so a
    #    wrong upload project can never silently embed. The note's project is 42;
    #    the upload response says project 99 (a second/CI repo).
    note = ProjectInfo(project_id=42, path_with_namespace="group/client", short_name="client", default_branch="main")
    cross = {"full_path": "/-/project/99/uploads/deadbeefcafe/shot.png"}
    verification = _uploads.verify_upload(MagicMock(), project=note, upload=cross)
    guard_rejects_wrong_project = (not verification.ok) and "expected 42" in verification.detail

    return resolves_to_note_project and guard_rejects_wrong_project
