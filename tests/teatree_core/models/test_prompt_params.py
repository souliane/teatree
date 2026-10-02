"""Prompt templated params + version history (#2513, D2).

A :class:`Prompt` declares ``params`` (named templated args) and renders its
``body`` by substituting them. Each content change snapshots the SUPERSEDED
body+params as a :class:`PromptVersion` row so the edit history is durable and
auditable. Integration-first against the real DB; ``demo-*`` names never collide
with the seeded production prompts.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Prompt, PromptVersion
from teatree.core.models.prompt import MissingPromptParamError, UnknownPromptParamError


class TestPromptParamsRender(TestCase):
    def test_render_substitutes_declared_params(self) -> None:
        prompt = Prompt.objects.create(
            name="demo-greet", body="Hello {who}, run the {what} loop.", params=["who", "what"]
        )
        assert prompt.render(who="adrien", what="ship") == "Hello adrien, run the ship loop."

    def test_render_with_no_params_returns_body_verbatim(self) -> None:
        prompt = Prompt.objects.create(name="demo-static", body="just do it")
        assert prompt.render() == "just do it"

    def test_render_missing_declared_param_raises(self) -> None:
        prompt = Prompt.objects.create(name="demo-miss", body="Hi {who}", params=["who"])
        with pytest.raises(MissingPromptParamError):
            prompt.render()

    def test_render_rejects_undeclared_param(self) -> None:
        prompt = Prompt.objects.create(name="demo-extra", body="Hi {who}", params=["who"])
        with pytest.raises(UnknownPromptParamError):
            prompt.render(who="x", bogus="y")

    def test_render_leaves_literal_braces_when_not_a_declared_param(self) -> None:
        # A body that mentions a JSON snippet `{...}` must not be treated as a
        # format field — only declared params are substituted.
        prompt = Prompt.objects.create(name="demo-json", body="emit {} then {who}", params=["who"])
        assert prompt.render(who="me") == "emit {} then me"

    def test_params_default_is_empty_list(self) -> None:
        prompt = Prompt.objects.create(name="demo-default-params", body="x")
        assert prompt.params == []


class TestPromptVersioning(TestCase):
    def test_first_edit_snapshots_the_superseded_content(self) -> None:
        prompt = Prompt.objects.create(name="demo-v", body="v1 body", params=["a"])
        prompt.revise(body="v2 body", params=["a", "b"])
        versions = list(PromptVersion.objects.filter(prompt=prompt).order_by("version"))
        assert [v.version for v in versions] == [1]
        # The snapshot holds the OLD content (the superseded v1), not the new one.
        assert versions[0].body == "v1 body"
        assert versions[0].params == ["a"]
        # The live row now carries the new content.
        prompt.refresh_from_db()
        assert prompt.body == "v2 body"
        assert prompt.params == ["a", "b"]

    def test_each_edit_increments_the_version_number(self) -> None:
        prompt = Prompt.objects.create(name="demo-v2", body="b1")
        prompt.revise(body="b2")
        prompt.revise(body="b3")
        versions = list(PromptVersion.objects.filter(prompt=prompt).order_by("version"))
        assert [v.version for v in versions] == [1, 2]
        assert [v.body for v in versions] == ["b1", "b2"]

    def test_revise_with_identical_content_does_not_snapshot(self) -> None:
        prompt = Prompt.objects.create(name="demo-noop", body="same", params=["p"])
        prompt.revise(body="same", params=["p"])
        assert PromptVersion.objects.filter(prompt=prompt).count() == 0
        prompt.refresh_from_db()
        assert prompt.body == "same"

    def test_current_version_number_reflects_history_depth(self) -> None:
        prompt = Prompt.objects.create(name="demo-cur", body="b1")
        assert prompt.current_version == 0
        prompt.revise(body="b2")
        assert prompt.current_version == 1
        prompt.revise(body="b3")
        assert prompt.current_version == 2

    def test_version_is_unique_per_prompt(self) -> None:
        # Two prompts can each have a version 1 — the key is (prompt, version).
        p1 = Prompt.objects.create(name="demo-u1", body="a")
        p2 = Prompt.objects.create(name="demo-u2", body="x")
        p1.revise(body="b")
        p2.revise(body="y")
        assert PromptVersion.objects.filter(version=1).count() == 2

    def test_str_describes_prompt_and_version(self) -> None:
        prompt = Prompt.objects.create(name="demo-vs", body="a")
        prompt.revise(body="b")
        version = PromptVersion.objects.get(prompt=prompt, version=1)
        assert "demo-vs" in str(version)
        assert "1" in str(version)
