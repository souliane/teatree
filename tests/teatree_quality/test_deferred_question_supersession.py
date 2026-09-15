"""The supersession-bypass detector bites on the literal shape, and only on it."""

from pathlib import Path

from teatree.quality.deferred_question_supersession import scan_bypasses


def _write(tmp_path: Path, body: str, name: str = "caller.py") -> Path:
    (tmp_path / name).write_text(body)
    return tmp_path


class TestDetectorBites:
    def test_a_session_and_run_scoped_pending_filter_is_a_bypass(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path,
            "def supersede(session_id, run_id):\n"
            "    for prior in DeferredQuestion.pending().filter(session_id=session_id, run_id=run_id):\n"
            "        prior.mark_stale('superseded')\n",
        )

        found = scan_bypasses(root)

        assert [b.kwargs for b in found] == [("run_id", "session_id")]

    def test_a_session_only_pending_filter_is_the_silent_widening(self, tmp_path: Path) -> None:
        # The shape that sweeps the WHOLE session when the run cannot be named.
        root = _write(tmp_path, "rows = DeferredQuestion.pending().filter(session_id=session_id)\n")

        assert [b.kwargs for b in scan_bypasses(root)] == [("session_id",)]

    def test_an_intermediate_call_between_pending_and_filter_is_still_a_bypass(self, tmp_path: Path) -> None:
        root = _write(tmp_path, "rows = DeferredQuestion.pending().order_by('pk').filter(run_id=run_id)\n")

        assert [b.kwargs for b in scan_bypasses(root)] == [("run_id",)]

    def test_the_bypass_reports_its_module_and_line(self, tmp_path: Path) -> None:
        root = _write(tmp_path, "x = 1\nrows = DeferredQuestion.pending().filter(run_id=run_id)\n")

        (bypass,) = scan_bypasses(root)

        assert bypass.lineno == 2
        assert bypass.module.endswith("caller.py")
        assert bypass.key.endswith(":2:run_id")


class TestDetectorIsQuietOnLegitimateReads:
    def test_supersedable_is_the_seam_not_a_bypass(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path,
            "for prior in DeferredQuestion.supersedable(session_id=session_id, run_id=run_id):\n"
            "    prior.mark_stale('superseded')\n",
        )

        assert scan_bypasses(root) == []

    def test_an_audience_scoped_pending_filter_is_a_read(self, tmp_path: Path) -> None:
        # The owner_threads shape — a read of the owner's queue, not a supersession.
        root = _write(tmp_path, "pending = DeferredQuestion.pending().filter(audience=Audience.OWNER_QUESTION)\n")

        assert scan_bypasses(root) == []

    def test_a_dedupe_marker_lookup_is_a_read(self, tmp_path: Path) -> None:
        # The hook_router shape — finds THIS question's own row, supersedes nothing.
        root = _write(tmp_path, "row = DeferredQuestion.pending().filter(dedupe_marker=marker).first()\n")

        assert scan_bypasses(root) == []

    def test_a_scope_filter_on_another_queryset_is_not_reported(self, tmp_path: Path) -> None:
        root = _write(tmp_path, "rows = Task.objects.filter(session_id=session_id, run_id=run_id)\n")

        assert scan_bypasses(root) == []

    def test_the_model_module_defining_supersedable_is_exempt(self, tmp_path: Path) -> None:
        owning = tmp_path / "teatree" / "core" / "models"
        owning.mkdir(parents=True)
        (owning / "deferred_question.py").write_text(
            "def supersedable(session_id, run_id):\n"
            "    return cls.pending().filter(session_id=session_id, run_id=run_id, slack_ts='')\n"
        )

        assert scan_bypasses(tmp_path) == []
