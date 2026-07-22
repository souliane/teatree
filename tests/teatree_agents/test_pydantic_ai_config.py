"""The ``pydantic_ai`` capability constants advertise only what the lane actually enforces."""

from pathlib import Path

from teatree.agents import harness
from teatree.agents.pydantic_ai_config import PYDANTIC_AI_NATIVE_CAPABILITIES, PYDANTIC_AI_ROUTER_CAPABILITIES


class TestStructuredOutputCapabilityIsHonest:
    """The lane does NOT enforce a result schema, so neither binding may advertise it.

    Both capability constants claimed ``structured_output=True`` while nothing read the
    flag and the lane only scrapes the last JSON line of agent text. The flag is dead and
    the claim was misleading; both bindings report ``False``.
    """

    def test_router_binding_reports_no_structured_output(self) -> None:
        assert PYDANTIC_AI_ROUTER_CAPABILITIES.structured_output is False

    def test_native_binding_reports_no_structured_output(self) -> None:
        assert PYDANTIC_AI_NATIVE_CAPABILITIES.structured_output is False

    def test_no_agents_code_reads_the_structured_output_capability(self) -> None:
        # Grep-proof that the flag is dead: nothing in the agents package branches on
        # ``capabilities.structured_output`` (the eval judge's ``ResultMessage.structured_output``
        # lives in a different package and is a different concept). The capability is DEFINED
        # with ``structured_output=`` / ``structured_output:``, never read with ``.structured_output``.
        agents_dir = Path(harness.__file__).parent
        offenders = [
            f"{path.relative_to(agents_dir)}:{lineno}: {line.strip()}"
            for path in agents_dir.rglob("*.py")
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if ".structured_output" in line
        ]
        assert not offenders, f"the structured_output capability is dead — remove these reads: {offenders}"

    def test_config_source_only_mentions_structured_output_to_deny_it(self) -> None:
        # The docstrings were corrected to stop misleading the next reader: every mention of
        # the phrase must be negated ("no schema-enforced structured output"), never a positive
        # capability claim. Scans ALL occurrences, not just the first (the phrase recurs).
        from teatree.agents import pydantic_ai_config  # noqa: PLC0415 — reading the module's own source

        source = Path(pydantic_ai_config.__file__).read_text(encoding="utf-8")
        phrase = "schema-enforced structured output"
        index = source.find(phrase)
        assert index != -1, "expected the phrase to appear (negated) in the corrected docstrings"
        while index != -1:
            assert source[:index].rstrip().endswith("no"), (
                "the config source positively claims structured output — it must only deny it"
            )
            index = source.find(phrase, index + len(phrase))
