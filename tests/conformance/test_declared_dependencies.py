"""A requirement's distribution name survives every PEP 508 form it can be written in.

Both dependency lanes decide "is this declared?" by matching a canonical name, so a form
the reader mangles reads as an UNDECLARED distribution — and the lane then either passes
an unbounded dependency or fails a declared one, with the same grammar bug in both.
Written once, so it is fixed once.
"""

import pytest

from tests.conformance._declared_dependencies import spec_name


class TestTheSpecNameIsTheBareDistribution:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("Some_Dist", "some-dist"),
            ("coverage>=7", "coverage"),
            ("django>=6,<6.1", "django"),
            ("claude-agent-sdk==0.2.128", "claude-agent-sdk"),
            ("pydantic-ai-slim[anthropic,openai]>=2.3,<3", "pydantic-ai-slim"),
            ("tomlkit~=0.13", "tomlkit"),
            ("tzdata ; sys_platform == 'win32'", "tzdata"),
            ("backports-zoneinfo; python_version < '3.9'", "backports-zoneinfo"),
            ("teatree@ https://example.invalid/teatree.whl", "teatree"),
        ],
    )
    def test_every_written_form_reduces_to_it(self, spec: str, expected: str) -> None:
        assert spec_name(spec) == expected
