"""An ``upload-artifact`` step whose path is a DOTFILE uploads nothing, green (#4584).

``actions/upload-artifact`` v4.4+ excludes hidden files unless
``include-hidden-files: true``, and ``if-no-files-found`` defaults to ``warn`` — so a
step naming ``dev/.test_durations`` matches zero files, publishes no artifact, and
still reports success. Measured on dispatch run 31869191957: twelve green
``test-shard`` jobs, zero ``durations-shard-*`` artifacts, and ``refresh-durations``
red at the merge step one job later, a month after the upload silently stopped
working.

Both inputs are asserted for every hidden-path upload in every workflow: the first is
what makes such an upload publish anything at all, the second is what makes a future
silent-empty upload fail at the PRODUCER rather than at a consumer a job later.
Relaxing either on a new step is a reviewed decision, not a default.
"""

from pathlib import Path
from typing import Any, NamedTuple, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_UPLOAD_ACTION = "actions/upload-artifact"


class HiddenUpload(NamedTuple):
    workflow: str
    job: str
    paths: tuple[str, ...]
    inputs: dict[str, Any]

    @property
    def where(self) -> str:
        return f"{self.workflow}:{self.job} (path {', '.join(self.paths)})"


def _is_hidden(path: str) -> bool:
    """A leading ``!`` is upload-artifact's EXCLUDE syntax — it never publishes anything."""
    if not path or path.startswith("!"):
        return False
    return any(part.startswith(".") and part not in {".", ".."} for part in Path(path).parts)


def _hidden_uploads() -> list[HiddenUpload]:
    found: list[HiddenUpload] = []
    for workflow in sorted(_WORKFLOWS.glob("*.y*ml")):
        loaded = cast("dict[str, Any]", yaml.safe_load(workflow.read_text(encoding="utf-8")))
        for job_name, job in cast("dict[str, Any]", loaded.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if not isinstance(step, dict) or _UPLOAD_ACTION not in str(step.get("uses", "")):
                    continue
                inputs = cast("dict[str, Any]", step.get("with") or {})
                hidden = tuple(p.strip() for p in str(inputs.get("path", "")).splitlines() if _is_hidden(p.strip()))
                if hidden:
                    found.append(HiddenUpload(workflow.name, str(job_name), hidden, inputs))
    return found


class TestHiddenPathUploadsPublishSomething:
    def test_the_durations_upload_is_still_discovered(self) -> None:
        """Anti-vacuity: the two loops below must never pass over an empty set."""
        assert any("durations" in str(u.inputs.get("name", "")) for u in _hidden_uploads()), (
            "The durations-shard upload names a hidden path (dev/.test_durations) and must be "
            "covered here; if it moved, re-point this guard rather than dropping it."
        )

    def test_every_hidden_path_upload_includes_hidden_files(self) -> None:
        for upload in _hidden_uploads():
            assert str(upload.inputs.get("include-hidden-files", "")).lower() == "true", (
                f"{upload.where} uploads a hidden path without include-hidden-files: true. "
                "upload-artifact v4.4+ excludes hidden files by default, so the step matches "
                "zero files, publishes NO artifact, and still reports success (#4584)."
            )

    def test_every_hidden_path_upload_fails_on_no_files(self) -> None:
        for upload in _hidden_uploads():
            assert str(upload.inputs.get("if-no-files-found", "")).lower() == "error", (
                f"{upload.where} leaves if-no-files-found at its 'warn' default, so an upload "
                "matching nothing reads GREEN at the producer and surfaces only when a consumer "
                "job fails later — the half that let #4584 run a month undetected."
            )
