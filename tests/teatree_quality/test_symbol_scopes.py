"""Unit surface of the source-derived scope lookups: what a module BINDS but does not carry.

The three lookups are the resolver's module-hop fallback (#4448). Each is deliberately
narrow, so the tests here are as much about what must NOT resolve — an imported class's
members, a runtime import, the ``else:`` branch of a guard — as about what must.
"""

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

from teatree.quality.symbol_scopes import (
    annotated_in_mro,
    module_class_members,
    source_bound_object,
    type_checking_bindings,
)
from tests.teatree_quality.conftest import Planter


def _bindings(tmp_path: Path, source: str, package: str = "pkg.leaf") -> dict[str, str]:
    module_file = tmp_path / "probe.py"
    module_file.write_text(source, encoding="utf-8")
    return dict(type_checking_bindings(module_file, package))


@dataclass
class _Base:
    inherited: list[str] = field(default_factory=list)


@dataclass
class _Derived(_Base):
    own: int = 0


class TestAnnotatedInMro:
    def test_an_own_annotation_is_found(self) -> None:
        assert annotated_in_mro(_Derived, "own")

    def test_an_inherited_annotation_is_found_despite_the_shadowing_own_dict(self) -> None:
        # The shape the guard reported as rot: `default_factory` leaves no class attribute,
        # and _Derived's own __annotations__ shadows _Base's under plain attribute lookup.
        assert not hasattr(_Derived, "inherited")
        assert annotated_in_mro(_Derived, "inherited")

    def test_an_absent_name_is_not_found(self) -> None:
        assert not annotated_in_mro(_Derived, "never_declared")


class TestModuleClassMembers:
    def test_a_method_of_a_defined_class_is_a_member(self, planted: Planter) -> None:
        module = planted("t3scope_defines", "class Holder:\n    def carried(self) -> None:\n        return None\n")
        assert "carried" in module_class_members(module)

    def test_a_staticmethod_of_a_defined_class_is_a_member(self, planted: Planter) -> None:
        module = planted(
            "t3scope_static",
            "class Holder:\n    @staticmethod\n    def _leaked() -> int:\n        return 1\n",
        )
        assert "_leaked" in module_class_members(module)

    def test_an_imported_class_does_not_vouch_for_its_members(self, planted: Planter) -> None:
        planted("t3scope_home", "class Carrier:\n    def carried(self) -> None:\n        return None\n")
        importer = planted("t3scope_reexport", "from t3scope_home import Carrier\n")
        assert "carried" not in module_class_members(importer)

    def test_two_classes_carrying_one_name_report_it_once(self, planted: Planter) -> None:
        module = planted("t3scope_twice", "class A:\n    shared = 1\n\n\nclass B:\n    shared = 2\n")
        assert "shared" in module_class_members(module)
        # Name order decides, so the winner is stable rather than import-order dependent.
        assert source_bound_object(module, "shared") == 1


class TestTypeCheckingBindings:
    def test_a_guarded_from_import_maps_to_its_real_home(self, tmp_path: Path) -> None:
        source = "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from pkg.other import Thing\n"
        assert _bindings(tmp_path, source) == {"Thing": "pkg.other.Thing"}

    def test_an_alias_binds_the_local_name(self, tmp_path: Path) -> None:
        source = "if TYPE_CHECKING:\n    from pkg.other import Thing as Alias\n"
        assert _bindings(tmp_path, source) == {"Alias": "pkg.other.Thing"}

    def test_a_dotted_attribute_guard_is_recognised(self, tmp_path: Path) -> None:
        source = "import typing\n\nif typing.TYPE_CHECKING:\n    from pkg.other import Thing\n"
        assert _bindings(tmp_path, source) == {"Thing": "pkg.other.Thing"}

    def test_a_plain_import_binds_its_head(self, tmp_path: Path) -> None:
        assert _bindings(tmp_path, "if TYPE_CHECKING:\n    import pkg.other\n") == {"pkg": "pkg"}

    def test_an_aliased_plain_import_binds_the_alias_to_the_full_path(self, tmp_path: Path) -> None:
        assert _bindings(tmp_path, "if TYPE_CHECKING:\n    import pkg.other as alt\n") == {"alt": "pkg.other"}

    def test_a_nested_statement_inside_the_guard_is_still_read(self, tmp_path: Path) -> None:
        source = (
            "if TYPE_CHECKING:\n    try:\n        from pkg.other import Thing\n    except ImportError:\n        pass\n"
        )
        assert _bindings(tmp_path, source) == {"Thing": "pkg.other.Thing"}

    @pytest.mark.parametrize(
        ("level", "expected"),
        [(1, "pkg.leaf.sib.Thing"), (2, "pkg.sib.Thing")],
    )
    def test_a_relative_import_resolves_against_the_package(self, tmp_path: Path, level: int, expected: str) -> None:
        source = f"if TYPE_CHECKING:\n    from {'.' * level}sib import Thing\n"
        assert _bindings(tmp_path, source, package="pkg.leaf") == {"Thing": expected}

    def test_a_bare_relative_import_resolves_to_the_package_itself(self, tmp_path: Path) -> None:
        assert _bindings(tmp_path, "if TYPE_CHECKING:\n    from . import sib\n") == {"sib": "pkg.leaf.sib"}

    def test_a_relative_import_above_the_package_root_binds_nothing(self, tmp_path: Path) -> None:
        assert _bindings(tmp_path, "if TYPE_CHECKING:\n    from ...sib import Thing\n", package="pkg") == {}

    def test_a_runtime_import_is_not_a_guarded_binding(self, tmp_path: Path) -> None:
        assert _bindings(tmp_path, "from pkg.other import Thing\n") == {}

    def test_the_else_branch_of_a_guard_is_the_runtime_path(self, tmp_path: Path) -> None:
        source = "if TYPE_CHECKING:\n    pass\nelse:\n    from pkg.other import Thing\n"
        assert _bindings(tmp_path, source) == {}

    def test_a_star_import_binds_no_name(self, tmp_path: Path) -> None:
        assert _bindings(tmp_path, "if TYPE_CHECKING:\n    from pkg.other import *\n") == {}

    def test_an_unparsable_module_binds_nothing(self, tmp_path: Path) -> None:
        assert _bindings(tmp_path, "def broken(:\n") == {}

    def test_a_missing_file_binds_nothing(self, tmp_path: Path) -> None:
        assert dict(type_checking_bindings(tmp_path / "absent.py", "pkg")) == {}


class TestSourceBoundObject:
    def test_a_guarded_import_resolves_at_its_real_home(self, planted: Planter) -> None:
        planted("t3scope_origin", "class Thing:\n    pass\n")
        citer = planted(
            "t3scope_citer",
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from t3scope_origin import Thing\n",
        )
        assert source_bound_object(citer, "Thing") is importlib.import_module("t3scope_origin").Thing

    def test_a_guarded_import_of_an_absent_symbol_binds_nothing(self, planted: Planter) -> None:
        planted("t3scope_thin", "REAL = 1\n")
        citer = planted(
            "t3scope_thin_citer",
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from t3scope_thin import Gone\n",
        )
        assert source_bound_object(citer, "Gone") is None

    def test_a_guarded_import_from_an_absent_module_binds_nothing(self, planted: Planter) -> None:
        citer = planted(
            "t3scope_absent_citer",
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from t3scope_no_such_module import Gone\n",
        )
        assert source_bound_object(citer, "Gone") is None

    def test_a_module_raising_on_import_binds_nothing(self, planted: Planter, monkeypatch: pytest.MonkeyPatch) -> None:
        citer = planted(
            "t3scope_raiser_citer",
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from t3scope_raiser import Thing\n",
        )

        def boom(name: str) -> ModuleType:
            raise RuntimeError(name)

        monkeypatch.setattr(importlib, "import_module", boom)
        assert source_bound_object(citer, "Thing") is None

    def test_a_class_member_is_bound_when_no_guard_names_it(self, planted: Planter) -> None:
        module = planted("t3scope_member", "class Holder:\n    def carried(self) -> None:\n        return None\n")
        assert source_bound_object(module, "carried") is not None

    def test_an_unbound_name_resolves_to_nothing(self, planted: Planter) -> None:
        module = planted("t3scope_empty", "VALUE = 1\n")
        assert source_bound_object(module, "absent") is None

    def test_a_module_with_no_file_falls_through_to_its_classes(self) -> None:
        # A namespace package has no source to parse, so only the class walk can answer.
        assert source_bound_object(ModuleType("t3scope_fileless"), "anything") is None

    def test_a_module_with_no_package_is_read_as_top_level(
        self, planted: Planter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = planted(
            "t3scope_nopkg",
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from t3scope_thin import REAL\n",
        )
        monkeypatch.setattr(module, "__package__", None)
        assert source_bound_object(module, "REAL") is None
