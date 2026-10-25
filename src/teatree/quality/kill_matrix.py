"""Attribute mutation kills to exact changed tests and identify dominated tests."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from teatree.quality.mutation_run import MutationResult, _run_mutmut, load_settings


class KillMatrixError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KillRow:
    node: str
    killed: tuple[str, ...]
    survived: tuple[str, ...]
    inconclusive: tuple[str, ...]
    redundant_with: tuple[str, ...]

    @property
    def measured(self) -> bool:
        return bool(self.killed or self.survived)

    @property
    def zero_kills(self) -> bool:
        return self.measured and not self.killed


@dataclass(frozen=True, slots=True)
class KillMatrix:
    head_sha: str
    rows: tuple[KillRow, ...]

    def for_head(self, head_sha: str) -> "KillMatrix | None":
        return self if self.head_sha == head_sha else None

    def render(self) -> str:
        if not self.rows:
            return "No changed test nodes were supplied for mutation measurement."
        return "\n".join(
            f"{row.node}: killed={len(row.killed)} {', '.join(row.killed) or '(none)'}; "
            f"survived={len(row.survived)}; inconclusive={len(row.inconclusive)}; "
            f"redundant_with={', '.join(row.redundant_with) or '(none)'}; "
            f"verdict={'UNMEASURED' if not row.measured else 'ZERO_KILLS' if row.zero_kills else 'MEASURED'}"
            for row in self.rows
        )


def _run_test_mutants(node: str, modules: tuple[str, ...]) -> MutationResult:
    settings = load_settings()
    return _run_mutmut(
        modules,
        tests_dir=(node.split("::", maxsplit=1)[0],),
        test_selection=(node,),
        repo=".",
        timeout=settings.timeout_seconds,
    )


def build_kill_matrix(
    test_nodes: Sequence[str],
    modules: Sequence[str],
    *,
    run_test: Callable[[str, tuple[str, ...]], MutationResult] = _run_test_mutants,
    head_sha: str = "",
) -> KillMatrix:
    if not modules:
        if test_nodes:
            message = "changed test nodes have no source module to mutate"
            raise KillMatrixError(message)
        return KillMatrix(head_sha, ())
    results = [(node, run_test(node, tuple(modules))) for node in sorted(set(test_nodes))]
    rows: list[KillRow] = []
    for index, (node, result) in enumerate(results):
        if not (result.killed or result.survived or result.inconclusive):
            message = f"{node} classified no mutants; mutation evidence is unavailable"
            raise KillMatrixError(message)
        killed = frozenset(result.killed)
        dominators = tuple(
            other_node
            for other_index, (other_node, other) in enumerate(results)
            if other_index != index
            and killed
            and killed <= frozenset(other.killed)
            and (killed < frozenset(other.killed) or other_index < index)
        )
        rows.append(KillRow(node, result.killed, result.survived, result.inconclusive, dominators))
    return KillMatrix(head_sha, tuple(rows))
