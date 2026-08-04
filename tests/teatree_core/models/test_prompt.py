"""First-class reusable prompt model (#2513).

Pins the prompt's identity (unique name), defaults, and the manager lookup.
Integration-first against the real DB; ``demo-*`` names never collide with the
seeded production prompt names.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from teatree.core.models import Loop, Prompt


class TestPromptDefaults(TestCase):
    def test_minimal_create_defaults_blank_overlay_and_description(self) -> None:
        prompt = Prompt.objects.create(name="demo-p", body="do the thing")
        assert prompt.description == ""
        assert prompt.overlay == ""
        assert prompt.created_at is not None

    def test_str_describes_name(self) -> None:
        prompt = Prompt.objects.create(name="demo-ship", body="ship it")
        assert "demo-ship" in str(prompt)

    def test_name_is_unique(self) -> None:
        Prompt.objects.create(name="demo-dup", body="a")
        with pytest.raises(IntegrityError), transaction.atomic():
            Prompt.objects.create(name="demo-dup", body="b")

    def test_overlay_stores_backend_name_generically(self) -> None:
        Prompt.objects.create(name="demo-ov", body="x", overlay="some-backend")
        assert Prompt.objects.get(name="demo-ov").overlay == "some-backend"


class TestPromptManager(TestCase):
    def test_by_name_returns_the_prompt(self) -> None:
        Prompt.objects.create(name="demo-find", body="x")
        assert Prompt.objects.by_name("demo-find").name == "demo-find"

    def test_by_name_returns_none_when_absent(self) -> None:
        assert Prompt.objects.by_name("demo-missing") is None


class TestAnEmptyBody(TestCase):
    """Empty is legal exactly where it means "switched off" (#4166)."""

    def test_an_unreferenced_prompt_may_have_an_empty_body(self) -> None:
        prompt = Prompt.objects.create(name="demo-off", body="")

        prompt.full_clean()

        assert prompt.body == ""

    def test_a_prompt_a_loop_runs_refuses_an_empty_body(self) -> None:
        prompt = Prompt.objects.create(name="demo-loop-body", body="do the thing")
        Loop.objects.create(name="demo-loop", prompt=prompt)
        prompt.body = "   "

        with pytest.raises(ValidationError) as exc:
            prompt.full_clean()

        assert "body" in exc.value.message_dict
