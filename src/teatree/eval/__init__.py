"""Behavioral evals — runtime checks on agent behavior via the Agent SDK.

Each scenario declares an agent definition (a ``SKILL.md``), a prompt, and a
set of matchers over the resulting tool calls. The harness shells out to the
Claude CLI in stream-json mode, parses the transcript into structured tool
calls, and dispatches the matchers. The point is to convert "the agent knows
this rule" into "the agent's compliance with this rule is observable and
gated", so regressions surface as a red test rather than as a recurring
red-card moment.

See ``src/teatree/eval/README.md`` for the scenario YAML schema and the
list of supported operators.
"""

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher

__all__ = ["EvalRun", "EvalSpec", "EvalToolCall", "Matcher"]
