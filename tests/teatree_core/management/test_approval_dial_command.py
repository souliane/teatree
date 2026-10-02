"""``t3 <overlay> approval_dial`` — set/clear/show the per-action-class dial (#119).

The documented operator config action for graduating a class. Integration-first via
``call_command`` against the real ``ConfigSetting`` store.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.models import ConfigSetting
from teatree.core.models.approval_dial import DIAL_CONFIG_KEY

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _run(*args: str) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    try:
        call_command("approval_dial", *args, stdout=out, stderr=err)
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class TestApprovalDialSet:
    def test_set_graduates_a_class(self) -> None:
        code, _, _ = _run("set", "outer_loop_keep", "auto")
        assert code == 0
        assert ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope="") == {"outer_loop_keep": "auto"}

    def test_set_merges_without_dropping_other_classes(self) -> None:
        _run("set", "outer_loop_keep", "auto")
        _run("set", "directive_admit", "auto")
        assert ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope="") == {
            "outer_loop_keep": "auto",
            "directive_admit": "auto",
        }

    def test_set_refuses_an_unknown_class(self) -> None:
        code, _, err = _run("set", "not_a_class", "auto")
        assert code == 2
        assert "not an approval action class" in err

    def test_set_refuses_an_invalid_trust_word(self) -> None:
        code, _, err = _run("set", "outer_loop_keep", "maybe")
        assert code == 2
        assert "not a trust level" in err

    def test_set_refuses_auto_on_a_never_fades_class(self) -> None:
        code, _, err = _run("set", "gate_or_policy_change", "auto")
        assert code == 2
        assert "never-fades" in err
        assert ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope="") is None

    def test_set_scopes_to_an_overlay(self) -> None:
        _run("set", "outer_loop_keep", "auto", "--overlay", "acme")
        assert ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope="acme") == {"outer_loop_keep": "auto"}
        assert ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope="") is None


class TestApprovalDialClear:
    def test_clear_removes_the_last_class_and_the_row(self) -> None:
        _run("set", "outer_loop_keep", "auto")
        code, _, _ = _run("clear", "outer_loop_keep")
        assert code == 0
        assert ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope="") is None

    def test_clear_keeps_other_classes(self) -> None:
        _run("set", "outer_loop_keep", "auto")
        _run("set", "directive_admit", "auto")
        _run("clear", "outer_loop_keep")
        assert ConfigSetting.objects.get_effective(DIAL_CONFIG_KEY, scope="") == {"directive_admit": "auto"}

    def test_clear_an_unset_class_is_loud(self) -> None:
        code, _, err = _run("clear", "outer_loop_keep")
        assert code == 1
        assert "no dial entry" in err


class TestApprovalDialShow:
    def test_show_renders_each_class_with_its_verdict(self) -> None:
        _run("set", "outer_loop_keep", "auto")
        code, out, _ = _run("show")
        assert code == 0
        assert "outer_loop_keep" in out
        assert "auto_approve" in out  # the graduated class's verdict
        assert "public_issue_create" in out
        assert "never-fades" in out
