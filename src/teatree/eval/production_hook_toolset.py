"""Bounded production-hook denial recovery for direct-model eval tools."""

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic_ai.exceptions import ModelRetry, UnexpectedModelBehavior
from pydantic_ai.toolsets import WrapperToolset

if TYPE_CHECKING:
    from pydantic_ai.tools import RunContext
    from pydantic_ai.toolsets.abstract import ToolsetTool

    from teatree.eval.production_hook_bridge import ProductionHookBridge

_HOOK_DENIAL_ATTEMPTS = 3


@dataclass
class ProductionHookToolset(WrapperToolset[None]):
    """Refuse denied calls while preserving an independent correction budget."""

    bridge: "ProductionHookBridge | None" = None
    _hook_denials: dict[str, int] = field(default_factory=dict, init=False)
    _ordinary_retry_limits: dict[str, int] = field(default_factory=dict, init=False)

    async def get_tools(self, ctx: "RunContext[None]") -> dict[str, "ToolsetTool[None]"]:
        tools = await super().get_tools(ctx)
        expanded: dict[str, ToolsetTool[None]] = {}
        for name, tool in tools.items():
            self._ordinary_retry_limits[name] = tool.max_retries
            expanded[name] = replace(
                tool,
                max_retries=max(tool.max_retries + _HOOK_DENIAL_ATTEMPTS, _HOOK_DENIAL_ATTEMPTS),
            )
        return expanded

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: "RunContext[None]", tool: "ToolsetTool[None]"
    ) -> Any:  # noqa: ANN401 -- WrapperToolset's contract returns arbitrary tool output.
        if self.bridge is None:
            return await super().call_tool(name, tool_args, ctx, tool)

        tool_call_id = ctx.tool_call_id or f"direct-{len(self.bridge.events) + 1}"
        reason = self.bridge.pre_tool_use(name, tool_args, tool_call_id=tool_call_id)
        if reason is not None:
            self.bridge.record_tool_result(tool_call_id, reason, is_error=True)
            denials = self._hook_denials.get(name, 0) + 1
            self._hook_denials[name] = denials
            if denials >= _HOOK_DENIAL_ATTEMPTS:
                message = f"Production hook denied tool {name!r} {_HOOK_DENIAL_ATTEMPTS} consecutive times"
                raise UnexpectedModelBehavior(message)
            raise ModelRetry(reason)

        try:
            result = await super().call_tool(name, tool_args, ctx, tool)
        except ModelRetry as exc:
            hook_denials = self._hook_denials.get(name, 0)
            ordinary_retry = max(ctx.retry - hook_denials, 0)
            ordinary_limit = self._ordinary_retry_limits.get(name, 1)
            if ordinary_retry >= ordinary_limit:
                message = f"Tool {name!r} exceeded max retries count of {ordinary_limit}"
                raise UnexpectedModelBehavior(message) from exc
            raise
        self._hook_denials.pop(name, None)
        self.bridge.record_tool_result(tool_call_id, result)
        return result


__all__ = ["ProductionHookToolset"]
