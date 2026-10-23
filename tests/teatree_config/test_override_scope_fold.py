from teatree.config.override_reader import OverlayRows, column_spelling, fold_overlay_rows


def test_fold_overlay_rows_uses_every_alias_and_exact_spelling_last() -> None:
    scopes = {
        "t3-example": {"shared": "qualified", "qualified_only": 1},
        "example": {"shared": "bare", "bare_only": 2},
        "other": {"shared": "other"},
    }

    assert fold_overlay_rows(scopes, "example") == OverlayRows(
        {"shared": "bare", "qualified_only": 1, "bare_only": 2},
        {"shared": "example", "qualified_only": "t3-example", "bare_only": "example"},
    )
    assert fold_overlay_rows(scopes, "t3-example").rows["shared"] == "qualified"


def test_column_spelling_prefers_the_registered_scope() -> None:
    assert column_spelling({"example", "t3-example"}, {"t3-example"}) == "t3-example"
    assert column_spelling({"example", "t3-example"}, set()) == "example"
