"""The teatree repo must be a valid APM package.

``README``'s "For users" flow runs ``apm install -g souliane/teatree``.  APM's
detection cascade classifies any directory that carries ``apm.yml`` as an
``APM_PACKAGE`` and then *requires* a ``.apm/`` directory holding at least one
primitive — otherwise the install aborts with
``Missing required directory: .apm/``.

These assertions mirror APM's canonical validator
(``apm_cli.models.validation._validate_apm_package_with_yml``) so the package
structure can never silently regress out of conformance without APM installed
as a test dependency.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirrors apm_cli.models.validation: primitive markdown lives in one of these
# subdirectories of ``.apm/`` (hooks JSON is the alternative primitive form).
_PRIMITIVE_DIRS = ("instructions", "chatmodes", "contexts", "prompts")


def _has_primitive(apm_dir: Path) -> bool:
    for primitive_type in _PRIMITIVE_DIRS:
        if list((apm_dir / primitive_type).glob("*.md")):
            return True
    for hooks_dir in (apm_dir / "hooks", apm_dir.parent / "hooks"):
        if hooks_dir.is_dir() and list(hooks_dir.glob("*.json")):
            return True
    return False


class TestApmPackageStructure:
    def test_apm_yml_present_and_parseable(self) -> None:
        apm_yml = REPO_ROOT / "apm.yml"
        assert apm_yml.is_file()
        data = yaml.safe_load(apm_yml.read_text(encoding="utf-8"))
        assert data.get("name")
        assert data.get("version")

    def test_apm_directory_exists(self) -> None:
        apm_dir = REPO_ROOT / ".apm"
        assert apm_dir.is_dir(), (
            "APM classifies a repo with apm.yml as an APM_PACKAGE and requires a "
            ".apm/ directory; without it `apm install -g souliane/teatree` aborts "
            "with 'Missing required directory: .apm/'."
        )

    def test_apm_directory_has_a_primitive(self) -> None:
        apm_dir = REPO_ROOT / ".apm"
        assert _has_primitive(apm_dir), (
            ".apm/ must hold at least one primitive (instructions/chatmodes/"
            "contexts/prompts *.md, or hooks/*.json) or APM warns the package is "
            "empty."
        )
