# test-path: cross-cutting — scans src/teatree + scripts for legacy-key reads; no single-module mirror.
"""Conformance guards for the consolidated banned-term registry.

Two static invariants keep the consolidation from silently regressing:

* **Single resolver** — the four legacy term keys (``banned_terms`` /
    ``banned_brands`` / ``banned_terms_allowlist`` / ``overlay_leak_terms``) are read
    from the ``ConfigSetting`` store ONLY by the registry module and the legacy
    resolvers it delegates to. Every scanning gate resolves through
    :mod:`teatree.hooks.banned_term_registry` instead of reading a legacy row itself,
    so a new consumer that bypasses the registry is caught here rather than in a leak.
* **Secret/defaults boundary** — the term keys are ``SECRET_SETTINGS`` (DB-only,
    empty in code), so if a ``config/defaults.toml`` ever ships it must carry none of
    them. A forward guard, inert until such a file exists.
"""

import ast
import tomllib
from pathlib import Path

import pytest

from teatree.config.secret_settings import SECRET_SETTINGS

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The four DB-home lists the registry folds together. A direct ``cold_reader`` read of
#: any of these outside the sanctioned resolvers bypasses the registry's dual-read.
_LEGACY_KEYS: frozenset[str] = frozenset(
    {"banned_terms", "banned_brands", "banned_terms_allowlist", "overlay_leak_terms"}
)

#: The modules allowed to read a legacy key directly: the registry itself
#: (``banned_terms_allowlist`` / ``overlay_leak_terms``) plus the three legacy
#: resolvers it delegates to — ``banned_terms_cli`` (``banned_terms``),
#: ``banned_terms_tree_scan`` (``banned_brands``), and ``banned_terms_scanner``'s
#: configured-check (``banned_terms``). No other consumer may read a legacy row.
_SANCTIONED_RESOLVERS: frozenset[str] = frozenset(
    {
        "src/teatree/hooks/banned_term_registry.py",
        "src/teatree/hooks/banned_terms_cli.py",
        "src/teatree/hooks/banned_terms_tree_scan.py",
        "src/teatree/hooks/banned_terms_scanner.py",
    }
)

_READERS: frozenset[str] = frozenset({"read_setting", "list_setting"})


def _module_str_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings — so ``read_setting(_TERMS_KEY)`` resolves."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
    return consts


def _legacy_key_of_read(call: ast.Call, consts: dict[str, str]) -> str | None:
    """The legacy key a ``read_setting``/``list_setting`` call reads, or ``None``.

    Resolves an inline string literal AND a same-file module-level string constant
    (``read_setting(_TERMS_KEY)``). A first arg that is neither is unresolvable here
    (a function parameter) and is not treated as a violation.
    """
    if not isinstance(call.func, ast.Attribute) or call.func.attr not in _READERS or not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        key = arg.value
    elif isinstance(arg, ast.Name):
        key = consts.get(arg.id, "")
    else:
        return None
    return key if key in _LEGACY_KEYS else None


def _files_reading_legacy_keys(root: Path, base: Path | None = None) -> dict[str, set[str]]:
    """Map each scanned file (relative to *base*) to the legacy keys it reads directly."""
    rel_to = base if base is not None else root
    found: dict[str, set[str]] = {}
    for py in sorted(root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        consts = _module_str_constants(tree)
        keys: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                key = _legacy_key_of_read(node, consts)
                if key is not None:
                    keys.add(key)
        if keys:
            found[py.relative_to(rel_to).as_posix()] = keys
    return found


class TestLegacyKeysHaveASingleResolver:
    """Only the registry + its legacy resolvers read a legacy term key directly."""

    def test_no_unsanctioned_module_reads_a_legacy_key(self) -> None:
        offenders = {
            path: keys
            for scanned in (_REPO_ROOT / "src" / "teatree", _REPO_ROOT / "scripts")
            for path, keys in _files_reading_legacy_keys(scanned, base=_REPO_ROOT).items()
            if path not in _SANCTIONED_RESOLVERS
        }
        assert offenders == {}, (
            "these modules read a legacy banned-terms key directly instead of routing "
            f"through banned_term_registry: {offenders}"
        )

    def test_detector_is_anti_vacuous(self, tmp_path: Path) -> None:
        # Control: the detector MUST flag a synthetic direct read, or the green above
        # proves nothing. Both an inline literal and a module-constant read are caught.
        (tmp_path / "leaky.py").write_text(
            'import cold_reader\n_K = "overlay_leak_terms"\n'
            'a = cold_reader.read_setting("banned_terms")\n'
            "b = cold_reader.list_setting(_K)\n",
            encoding="utf-8",
        )
        found = _files_reading_legacy_keys(tmp_path)
        assert found == {"leaky.py": {"banned_terms", "overlay_leak_terms"}}


class TestTermKeysNeverInDefaultsToml:
    """SECRET term keys are DB-only: a shipped defaults.toml must carry none of them.

    Forward guard — inert until ``config/defaults.toml`` exists. The term keys are the
    intersection of the four legacy keys, the consolidated ``banned_term_registry``,
    and ``SECRET_SETTINGS``, all of which must stay out of any committed defaults file.
    """

    def _all_keys(self, table: object, prefix: str = "") -> set[str]:
        keys: set[str] = set()
        if isinstance(table, dict):
            for key, value in table.items():
                keys.add(key)
                keys.add(f"{prefix}{key}")
                keys |= self._all_keys(value, prefix=f"{prefix}{key}.")
        return keys

    def test_no_secret_setting_appears_in_defaults_toml(self) -> None:
        defaults = _REPO_ROOT / "config" / "defaults.toml"
        if not defaults.is_file():
            pytest.skip("no config/defaults.toml ships today — the forward guard stays inert")
        data = tomllib.loads(defaults.read_text(encoding="utf-8"))
        assert SECRET_SETTINGS.isdisjoint(self._all_keys(data))

    def test_the_five_term_keys_are_all_secret(self) -> None:
        # The keys this PR routes through the registry are ALL export-guarded secrets,
        # so the forward guard above genuinely covers them.
        term_keys = {
            "banned_terms",
            "banned_terms_allowlist",
            "banned_brands",
            "overlay_leak_terms",
            "banned_term_registry",
        }
        assert term_keys <= SECRET_SETTINGS
