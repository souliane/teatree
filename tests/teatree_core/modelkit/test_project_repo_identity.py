from unittest.mock import patch

import pytest

from teatree.core.modelkit import gate_registry, project_repo_identity
from teatree.core.modelkit.project_repo_identity import ProjectRepoIdentity
from teatree.core.models import Ticket


def test_declared_bare_name_qualifies_to_full_project_path() -> None:
    identity = ProjectRepoIdentity({"teatree": "souliane/teatree"})
    assert identity.canonical(" teatree ") == "souliane/teatree"
    assert identity.canonical("souliane/teatree") == "souliane/teatree"
    assert identity.canonical("other/teatree") == "other/teatree"
    assert identity.canonical("unknown") == "unknown"


def test_distinct_keeps_first_spelling_and_drops_blank_entries() -> None:
    identity = ProjectRepoIdentity({"teatree": "souliane/teatree"})
    assert identity.distinct([" teatree ", "", "souliane/teatree", "other/teatree"]) == ["teatree", "other/teatree"]


def test_coverage_compares_canonical_identities() -> None:
    identity = ProjectRepoIdentity({"teatree": "souliane/teatree"})
    assert identity.covers(["teatree", "org/adapter"], ["souliane/teatree", "org/adapter"])
    assert not identity.covers(["teatree", "org/adapter"], ["other/teatree", "org/adapter"])
    assert identity.covers([], [])


def test_empty_declaration_keeps_old_string_comparison() -> None:
    identity = ProjectRepoIdentity({})
    assert identity.distinct(["teatree", "souliane/teatree"]) == ["teatree", "souliane/teatree"]
    assert not identity.covers(["teatree"], ["souliane/teatree"])


def test_missing_project_paths_provider_fails_loud() -> None:
    with (
        patch.dict(gate_registry._REGISTRY, {}, clear=True),
        pytest.raises(KeyError, match="repo_project_paths"),
    ):
        project_repo_identity.project_repo_identity_for_ticket(Ticket(overlay="t3-teatree"))
