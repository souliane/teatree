"""The eval surfaces never call a lane "free" — it is a false cost claim."""

from teatree.quality.eval_lane_vocabulary import repo_root, scan, scan_text, scanned_paths


class TestShippedSurfacesAreClean:
    def test_the_scan_set_resolves_inside_the_repo(self) -> None:
        assert (repo_root() / "pyproject.toml").is_file()
        assert all(path.is_relative_to(repo_root()) for path in scanned_paths())

    def test_no_eval_surface_calls_a_lane_free(self) -> None:
        violations = scan()
        assert not violations, (
            "the eval surfaces claim a lane is 'free' — a deterministic lane still spends CPU, "
            "wall-clock and maintenance. Say 'model-free' (the lane calls no live model):\n"
            + "\n".join(violation.render() for violation in sorted(violations, key=lambda v: (v.path, v.line_number)))
        )

    def test_the_scan_set_is_not_empty(self) -> None:
        assert scanned_paths(), "the eval-surface scan set resolved to nothing — the gate would be vacuous"


class TestScannerIsAntiVacuous:
    def test_a_bare_free_cost_claim_is_flagged(self) -> None:
        assert [v.line_number for v in scan_text("the free deterministic lanes\n", path="x.md")] == [1]

    def test_an_uppercase_cost_claim_is_flagged(self) -> None:
        assert [v.line_number for v in scan_text("runs the FREE lanes\n", path="x.md")] == [1]

    def test_a_hyphenated_compound_is_not_a_cost_claim(self) -> None:
        clean = "model-free lanes, a Django-free reader, a gap-free corpus, free-form work\n"
        assert list(scan_text(clean, path="x.md")) == []

    def test_a_compound_wrapped_across_two_lines_is_not_a_cost_claim(self) -> None:
        assert list(scan_text("it is price-table-\n  free: per variant\n", path="x.md")) == []

    def test_the_absence_sense_is_not_a_cost_claim(self) -> None:
        assert list(scan_text("transcripts must stay free of personal content\n", path="x.md")) == []
