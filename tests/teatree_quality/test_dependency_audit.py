"""Tests for reachability annotation of audit findings (souliane/teatree#4346).

The headline property is that a NEGATIVE is never manufactured: "not reachable"
is the reassuring answer, so it is only reported when the distribution→import
mapping came from installed metadata. A guessed mapping that finds nothing must
report UNKNOWN — the class of bug where `pyyaml` is looked up as `pyyaml`,
never found, and confidently reported as unused.
"""

import json
from pathlib import Path

import pytest

from teatree.quality.dependency_audit import (
    Advisory,
    Basis,
    Reach,
    ReportError,
    annotate,
    build_import_index,
    format_report,
    parse_report,
    resolve_import_names,
)

_REPO_SRC = Path(__file__).resolve().parents[2] / "src"

_REPORT = json.dumps(
    {
        "dependencies": [
            {
                "name": "django",
                "version": "6.0.7",
                "vulns": [
                    {
                        "id": "PYSEC-2026-1",
                        "aliases": ["CVE-2026-15307"],
                        "description": "Spatial lookups passing str to django.contrib.gis.gdal.GDALRaster.",
                    },
                    {"id": "PYSEC-2026-2", "aliases": [], "description": "The admin renders URLField values."},
                ],
            },
            {"name": "clean-package", "version": "1.0", "vulns": []},
        ],
        "fixes": [],
    }
)


def _src(tmp_path: Path, body: str) -> Path:
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "mod.py").write_text(body, encoding="utf-8")
    return tmp_path / "src"


class TestImportIndex:
    def test_plain_and_from_imports_are_indexed(self, tmp_path: Path) -> None:
        src = _src(tmp_path, "import django.db.models\nfrom django.contrib import admin\n")
        assert build_import_index(src) == frozenset({"django.db.models", "django.contrib"})

    def test_relative_imports_are_not_module_paths(self, tmp_path: Path) -> None:
        src = _src(tmp_path, "from . import sibling\nfrom .. import parent\n")
        assert build_import_index(src) == frozenset()

    def test_an_unparseable_file_never_aborts_the_index(self, tmp_path: Path) -> None:
        src = _src(tmp_path, "import django\n")
        (src / "pkg" / "broken.py").write_text("def (:\n", encoding="utf-8")
        assert "django" in build_import_index(src)


class TestResolveImportNames:
    def test_installed_metadata_is_authoritative(self) -> None:
        names, basis = resolve_import_names("pyyaml", distributions={"yaml": ["PyYAML"]})
        assert names == frozenset({"yaml"})
        assert basis is Basis.METADATA

    def test_normalization_is_applied_to_both_sides(self) -> None:
        names, basis = resolve_import_names("zope.interface", distributions={"zope": ["zope-interface"]})
        assert names == frozenset({"zope"})
        assert basis is Basis.METADATA

    def test_an_uninstalled_distribution_falls_back_to_a_guess(self) -> None:
        names, basis = resolve_import_names("some-package", distributions={})
        assert names == frozenset({"some_package"})
        assert basis is Basis.GUESSED


class TestReachability:
    @staticmethod
    def _annotate(index: frozenset[str], distributions: dict[str, list[str]]) -> tuple:
        advisory = Advisory(package="django", version="6.0.7", vuln_id="X", aliases=(), description="")
        return annotate([advisory], index=index, distributions=distributions)

    def test_imported_package_is_reachable(self) -> None:
        (entry,) = self._annotate(frozenset({"django.db.models"}), {"django": ["Django"]})
        assert entry.package_reach is Reach.IMPORTED

    def test_unimported_package_with_authoritative_mapping_is_not_reachable(self) -> None:
        (entry,) = self._annotate(frozenset({"httpx"}), {"django": ["Django"]})
        assert entry.package_reach is Reach.NOT_IMPORTED

    def test_unimported_package_with_a_guessed_mapping_is_unknown_never_a_clean_bill(self) -> None:
        # The whole point: an unresolvable name must not read as "we don't use it".
        (entry,) = self._annotate(frozenset({"httpx"}), {})
        assert entry.package_reach is Reach.UNKNOWN

    def test_a_prefix_collision_is_not_a_match(self) -> None:
        (entry,) = self._annotate(frozenset({"djangoish.thing"}), {"django": ["Django"]})
        assert entry.package_reach is Reach.NOT_IMPORTED


class TestComponentReachability:
    @staticmethod
    def _components(index: frozenset[str], description: str | None = None) -> tuple:
        advisory = Advisory(
            package="django",
            version="6.0.7",
            vuln_id="X",
            aliases=(),
            description=description or "Passing a str to django.contrib.gis.gdal.GDALRaster can write a file.",
        )
        (entry,) = annotate([advisory], index=index, distributions={"django": ["Django"]})
        return entry.components

    def test_a_dotted_component_named_in_the_advisory_is_assessed(self) -> None:
        components = self._components(frozenset({"django.db.models"}))
        assert [(c.module, c.reach) for c in components] == [("django.contrib.gis.gdal.GDALRaster", Reach.NOT_IMPORTED)]

    def test_a_component_under_an_imported_ancestor_module_is_reachable(self) -> None:
        # The index only ever holds MODULE paths (what `import` statements name);
        # a component extracted from advisory text is a SYMBOL path one or more
        # levels below that. "django.contrib.gis.gdal" is what `build_import_index`
        # can produce; "django.contrib.gis.gdal.GDALRaster" is what the advisory
        # names. Reachability must match the symbol against its ancestor module,
        # never require the symbol itself to appear in the index verbatim.
        components = self._components(frozenset({"django.contrib.gis.gdal"}))
        assert components[0].reach is Reach.IMPORTED

    def test_a_component_exactly_equal_to_an_index_entry_is_reachable(self) -> None:
        components = self._components(frozenset({"django.contrib.gis.gdal.GDALRaster"}))
        assert components[0].reach is Reach.IMPORTED

    def test_no_dotted_component_leaves_the_package_verdict_alone(self) -> None:
        advisory = Advisory(package="django", version="6.0.7", vuln_id="X", aliases=(), description="A DoS.")
        (entry,) = annotate([advisory], index=frozenset({"django"}), distributions={"django": ["Django"]})
        assert entry.components == ()

    def test_component_matching_does_not_over_reach_a_broad_namespace_entry(self, tmp_path: Path) -> None:
        # `build_import_index` collapses `from django.contrib import admin` to the
        # bare namespace package "django.contrib" (see TestImportIndex above) —
        # ancestor-at-any-depth against that entry would make every unrelated
        # symbol under django.contrib (auth, gis, staticfiles, ...) register as
        # reachable. Only the DIRECT parent module counts.
        src = _src(tmp_path, "from django.contrib import admin\n")
        index = build_import_index(src)
        assert index == frozenset({"django.contrib"})
        components = self._components(index)  # default description names GDALRaster under gis
        assert components[0].reach is Reach.NOT_IMPORTED

    def test_the_real_src_index_resolves_the_django_6_0_8_advisories_as_the_issue_ranked_them(self) -> None:
        # souliane/teatree#4346's own motivating example, against the actual
        # repository (not a synthetic tmp_path): the admin CVE the issue says DOES
        # reach teatree must render differently from the gis CVEs it says do not.
        index = build_import_index(_REPO_SRC)
        components = self._components(
            index,
            description=(
                "The admin renders URLField values as clickable links; see "
                "django.contrib.admin.helpers and django.db.models.URLField. Also "
                "django.contrib.gis.gdal.GDALRaster and django.contrib.gis.geos.GEOSGeometry."
            ),
        )
        assert {c.module: c.reach for c in components} == {
            "django.contrib.admin.helpers": Reach.IMPORTED,
            "django.db.models.URLField": Reach.IMPORTED,
            "django.contrib.gis.gdal.GDALRaster": Reach.NOT_IMPORTED,
            "django.contrib.gis.geos.GEOSGeometry": Reach.NOT_IMPORTED,
        }


class TestParseReport:
    def test_only_vulnerable_dependencies_become_advisories(self) -> None:
        advisories = parse_report(_REPORT)
        assert [a.vuln_id for a in advisories] == ["PYSEC-2026-1", "PYSEC-2026-2"]
        assert advisories[0].aliases == ("CVE-2026-15307",)

    def test_one_advisory_per_id_however_many_sources_carry_it(self) -> None:
        # pip-audit repeats an advisory once per source; four copies of one
        # PYSEC id is noise the reader has to filter by hand.
        report = json.dumps(
            {
                "dependencies": [
                    {
                        "name": "flask",
                        "version": "0.12.2",
                        "vulns": [
                            {"id": "PYSEC-2018-66", "aliases": ["CVE-2018-1000656"], "description": "short"},
                            {"id": "PYSEC-2018-66", "aliases": ["GHSA-562c"], "description": "a longer wording"},
                        ],
                    }
                ]
            }
        )
        (advisory,) = parse_report(report)
        assert advisory.aliases == ("CVE-2018-1000656", "GHSA-562c")
        assert advisory.description == "a longer wording"

    @pytest.mark.parametrize(
        "text", ["", "not json", '{"no_key": 1}', '{"dependencies": "x"}', '{"dependencies": [1]}']
    )
    def test_a_broken_report_raises_never_degrades_to_no_findings(self, text: str) -> None:
        # Returning () here would report "no advisories" from a parse failure —
        # the same silent-miss shape #4346 is about.
        with pytest.raises(ReportError):
            parse_report(text)


class TestFormatReport:
    def test_each_advisory_names_its_reachability(self) -> None:
        annotated = annotate(
            parse_report(_REPORT), index=frozenset({"django.db.models"}), distributions={"django": ["Django"]}
        )
        rendered = format_report(annotated)
        assert "django 6.0.7 — PYSEC-2026-1 (CVE-2026-15307)" in rendered
        assert "REACHABLE from src/" in rendered
        assert "django.contrib.gis.gdal.GDALRaster — NOT reachable from src/" in rendered
        assert "static lower bound" in rendered

    def test_unknown_is_rendered_as_untrustworthy_not_as_a_negative(self) -> None:
        advisory = Advisory(package="mystery-dist", version="1.0", vuln_id="X", aliases=(), description="")
        rendered = format_report(annotate([advisory], index=frozenset(), distributions={}))
        verdict = next(line for line in rendered.splitlines() if line.startswith("  package:"))
        assert "UNKNOWN" in verdict
        assert "NOT reachable" not in verdict

    def test_an_empty_finding_set_says_so(self) -> None:
        assert format_report([]) == "dependency-audit: no advisories to assess."
