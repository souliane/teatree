"""Django-fsm-style state machine decorator with introspection."""

import functools
import inspect
from collections.abc import Callable
from typing import Any


class InvalidTransitionError(Exception):
    """Raised when a transition is attempted from an invalid source state."""


class ConditionFailedError(Exception):
    """Raised when a transition guard condition returns False."""


def transition(
    source: str | list[str],
    target: str,
    conditions: list[Callable[..., bool]] | None = None,
) -> Callable[..., Any]:
    """Decorate a method to gate it as a state machine transition."""
    sources = [source] if isinstance(source, str) else source

    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        method_name = method.__name__  # type: ignore[attr-defined]
        resolved_conditions = conditions or []
        method._fsm = {  # type: ignore[attr-defined]  # noqa: SLF001
            "source": sources,
            "target": target,
            "conditions": resolved_conditions,
        }

        @functools.wraps(method)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            if "*" not in sources and self.state not in sources:
                msg = f"Cannot {method_name} from {self.state}"
                raise InvalidTransitionError(msg)
            for cond in resolved_conditions:
                if not cond(self):
                    cond_name = cond.__name__  # type: ignore[attr-defined]
                    msg = f"Condition {cond_name} failed for {method_name}"
                    raise ConditionFailedError(msg)
            result = method(self, *args, **kwargs)
            self.state = target
            if hasattr(self, "save"):
                self.save()
            return result

        return wrapper

    return decorator


def get_transitions(cls: type) -> list[dict[str, Any]]:
    """Return all transitions defined on a class."""
    result = []
    for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
        fsm = getattr(method, "_fsm", None)
        if fsm:
            result.append({"method": name, **fsm})
    return result


def get_available_transitions(obj: object) -> list[dict[str, Any]]:
    """Return transitions available from the object's current state."""
    state = obj.state  # type: ignore[attr-defined]
    return [t for t in get_transitions(type(obj)) if "*" in t["source"] or state in t["source"]]


def generate_mermaid(cls: type) -> str:
    """Generate a Mermaid stateDiagram-v2 from the class's transitions."""
    lines = ["stateDiagram-v2"]
    all_transitions = get_transitions(cls)
    for t in all_transitions:
        label = t["method"]
        cond_names = [c.__name__ for c in t["conditions"]]
        if cond_names:
            label += f" [{', '.join(cond_names)}]"
        sources = t["source"]
        if "*" in sources:
            # Collect all states mentioned as sources or targets
            all_states: set[str] = set()
            for other in all_transitions:
                all_states.update(other["source"])
                all_states.add(other["target"])
            all_states.discard("*")
            sources = sorted(all_states)
        lines.extend(f"    {src} --> {t['target']} : {label}" for src in sources)
    return "\n".join(lines)
