from __future__ import annotations

from algen_agent_runtime.types.contracts import RunResult, RunState, RunStatus
from algen_agent_runtime.types.interfaces import ResponseComposer


class DefaultResponseComposer:
    name = "default"

    async def compose(self, state: RunState) -> RunResult:
        return RunResult(
            run_id=state.id,
            status=state.status,
            session_id=state.session_id,
            # Only a completed run delivers an answer: a failed run's text did not pass
            # verification or the final_response guardrails.
            output=state.output_text if state.status == RunStatus.COMPLETED else None,
            error=state.error,
            error_type=state.error_type if state.status == RunStatus.FAILED else None,
            limit_reached=state.limit_reached,
            # A completed run that reached a limit answered from what it had (on_limit: summarize).
            partial=state.status == RunStatus.COMPLETED and state.limit_reached is not None,
            rejected_output=state.rejected_output,
            artifacts=tuple(state.context_artifact_ids),
            sources=tuple(dict.fromkeys(item.source for item in state.citations)),
            citations=tuple(state.citations),
            execution_summary=state.summary,
        )


class ResponseComposerRegistry:
    def __init__(self) -> None:
        default = DefaultResponseComposer()
        self._items: dict[str, ResponseComposer] = {default.name: default}

    def register(self, composer: ResponseComposer) -> None:
        self._items[composer.name] = composer

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    def get(self, name: str) -> ResponseComposer:
        try:
            return self._items[name]
        except KeyError as exc:
            raise KeyError(f"response composer {name!r} is not registered") from exc
