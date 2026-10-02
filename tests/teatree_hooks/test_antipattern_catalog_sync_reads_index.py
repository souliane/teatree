"""The antipattern-catalog sync gate reads the git INDEX, not the working tree.

souliane/teatree#166 hardening: a working-tree read let a staged ``antipatterns.yaml``
edit pair with an unstaged (regenerated) doc — the working tree looked in sync
while the index carried a stale committed doc, so the drift shipped. Reading both
the YAML input and the doc from the index (``git show :<path>``) closes the
generate-before-check / stage-one-not-the-other vacuousness class.
"""

import importlib
import sys
from pathlib import Path

from tests._git_repo import make_git_repo, run_git

_REPO_ROOT = Path(__file__).resolve().parents[2]
_YAML_REL = "src/teatree/quality/antipatterns.yaml"
_DOC_REL = "docs/generated/antipattern-catalog.md"


def _sync_hook():
    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "hooks"))
    return importlib.import_module("check_antipattern_catalog_sync")


def _real_yaml() -> str:
    return (_REPO_ROOT / _YAML_REL).read_text(encoding="utf-8")


def _real_doc() -> str:
    return (_REPO_ROOT / _DOC_REL).read_text(encoding="utf-8")


def _seed_in_sync(tmp_path: Path) -> Path:
    """A committed repo whose YAML and doc are the real, in-sync pair."""
    repo = make_git_repo(tmp_path / "repo")
    (repo / _YAML_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / _DOC_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / _YAML_REL).write_text(_real_yaml(), encoding="utf-8")
    (repo / _DOC_REL).write_text(_real_doc(), encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "seed in-sync catalog")
    return repo


def test_passes_on_the_real_in_sync_tree() -> None:
    assert _sync_hook().check(_REPO_ROOT) == 0


def test_red_when_staged_yaml_pairs_with_unstaged_doc_regeneration(tmp_path: Path) -> None:
    """Stage a YAML edit; regenerate the doc in the working tree but do NOT stage it.

    Index: new YAML + old doc (the drift that would be committed). Working tree:
    new YAML + new doc (looks in sync). Only an index read catches this.
    """
    hook = _sync_hook()
    build_markdown = importlib.import_module("generate_antipattern_catalog").build_markdown
    repo = _seed_in_sync(tmp_path)

    # Add a valid new entry to the YAML, stage it (index gets the new YAML).
    new_entry = (
        "\n- id: pr24-index-read-probe\n"
        "  name: PR24 index-read probe\n"
        "  severity: low\n"
        "  detection: judgement\n"
        "  anti_pattern: A probe entry that only exists in the staged YAML.\n"
        "  preferred_pattern: Read the index so this new entry forces a doc regen.\n"
        "  consumers: [architecture-design]\n"
        "  refs: [pr24]\n"
    )
    new_yaml = _real_yaml() + new_entry
    (repo / _YAML_REL).write_text(new_yaml, encoding="utf-8")
    run_git(repo, "add", _YAML_REL)

    # Regenerate the doc into the WORKING TREE only (never staged) — the exact
    # move that masks drift under a working-tree read.
    regenerated_doc = build_markdown(new_yaml)
    (repo / _DOC_REL).write_text(regenerated_doc, encoding="utf-8")

    # A working-tree read would pass vacuously (working tree is internally in sync)...
    assert build_markdown(new_yaml) == (repo / _DOC_REL).read_text(encoding="utf-8")
    # ...but the index read must catch the stale committed doc.
    assert hook.check(repo) == 1


def test_red_when_committed_doc_is_stale_after_worktree_repair(tmp_path: Path) -> None:
    """Commit a stale doc, then repair the working tree — the index read still fires."""
    hook = _sync_hook()
    repo = make_git_repo(tmp_path / "repo")
    (repo / _YAML_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / _DOC_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / _YAML_REL).write_text(_real_yaml(), encoding="utf-8")
    stale = _real_doc() + "\nstale drift line the generator never produces\n"
    (repo / _DOC_REL).write_text(stale, encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-qm", "seed stale committed doc")

    # Repair only the working-tree doc (write, do not stage): index keeps the stale bytes.
    (repo / _DOC_REL).write_text(_real_doc(), encoding="utf-8")
    assert hook.check(repo) == 1
