"""The push scanner and forge gates share positions and legacy exclusions."""

import pytest

from teatree.hooks.privacy_generic_detectors import generic_findings


def test_repeated_findings_have_distinct_absolute_positions() -> None:
    text = "person@firm.tld\nperson@firm.tld"  # privacy-scan:allow synthetic fixture

    findings = generic_findings(text)

    assert [(hit.category, hit.match, hit.position) for hit in findings] == [
        ("email", "person@firm.tld", 0),  # privacy-scan:allow synthetic fixture
        ("email", "person@firm.tld", 16),  # privacy-scan:allow synthetic fixture
    ]


def test_legacy_email_exclusions_do_not_hide_adjacent_real_address() -> None:
    text = "git@github.com:org/repo.git @app.route person@firm.tld"  # privacy-scan:allow synthetic fixture

    assert [(hit.category, hit.match) for hit in generic_findings(text)] == [
        ("email", "person@firm.tld")  # privacy-scan:allow synthetic fixture
    ]


def test_empty_text_has_no_findings() -> None:
    assert generic_findings("") == []


@pytest.mark.parametrize(
    ("allowed", "text", "categories"),
    [
        ("jdoe@gmail.com", "jdoe@gmail.com", []),  # privacy-scan:allow synthetic fixture
        ("glpat-XXXXXXXXXXXXXXXXXXXX", "glpat-XXXXXXXXXXXXXXXXXXXX", []),  # privacy-scan:allow synthetic fixture
        ("jdoe", "jdoe@gmail.com", ["email"]),  # privacy-scan:allow synthetic fixture
        ("jdoe", "/home/jdoe/src", ["home_path"]),  # privacy-scan:allow synthetic fixture
        ("/home/jdoe", "/home/jdoe/src", []),  # privacy-scan:allow synthetic fixture
    ],
)
def test_allow_entry_exempts_only_a_finding_it_wholly_covers(allowed: str, text: str, categories: list[str]) -> None:
    assert [hit.category for hit in generic_findings(text, allowlist=(allowed,))] == categories


def test_macos_runner_workspace_is_exempt_only_on_forge_writes() -> None:
    path = "/Users/runner/work/project"  # privacy-scan:allow synthetic fixture
    assert generic_findings(path, forge_write=True) == []
    assert [hit.category for hit in generic_findings(path)] == ["home_path"]
