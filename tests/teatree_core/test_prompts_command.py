"""``manage.py prompts_list`` / ``prompts_render`` — the ``/prompts`` trigger surface (#2513).

Integration-first: drives the real management commands via ``call_command`` against
the ``Prompt`` table, asserting the rendered list columns, the ``--json`` shape, and
the templated-param render. Read-only — never mutates a row.
"""

import io
import json

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import Prompt


def _list(*args: str) -> str:
    out = io.StringIO()
    call_command("prompts_list", *args, stdout=out)
    return out.getvalue()


def _render(*args: str) -> str:
    out = io.StringIO()
    call_command("prompts_render", *args, stdout=out)
    return out.getvalue()


@django.test.override_settings(USE_TZ=True)
class TestPromptsList(django.test.TestCase):
    def test_lists_a_prompt_with_its_params(self) -> None:
        Prompt.objects.create(name="demo-list", body="hi {who}", params=["who"], description="greet")
        line = next(ln for ln in _list().splitlines() if "demo-list" in ln)
        assert "who" in line
        assert "greet" in line

    def test_json_shape_carries_name_body_params(self) -> None:
        Prompt.objects.create(name="demo-json", body="b", params=["a", "b"])
        payload = json.loads(_list("--json"))
        row = next(p for p in payload["prompts"] if p["name"] == "demo-json")
        assert row["params"] == ["a", "b"]
        assert row["body"] == "b"


@django.test.override_settings(USE_TZ=True)
class TestPromptsRender(django.test.TestCase):
    def test_render_substitutes_supplied_args(self) -> None:
        Prompt.objects.create(name="demo-r", body="run {what}", params=["what"])
        out = _render("demo-r", "--arg", "what=ship")
        assert "run ship" in out

    def test_render_unknown_prompt_errors(self) -> None:
        from django.core.management.base import CommandError  # noqa: PLC0415

        with pytest.raises(CommandError):
            _render("demo-missing")

    def test_render_no_params_prints_body(self) -> None:
        Prompt.objects.create(name="demo-static", body="just do it")
        assert "just do it" in _render("demo-static")
