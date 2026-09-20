from __future__ import annotations

from algen_agent_runtime.types.contracts import RunResult, RunState
from algen_agent_runtime.types.interfaces import ResponseComposer


class DefaultResponseComposer:
    name = "default"

    async def compose(self, state: RunState) -> RunResult:
        return RunResult(
            run_id=state.id,
            status=state.status,
            session_id=state.session_id,
            output=state.output_text,
            error=state.error,
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

    def get(self, name: str) -> ResponseComposer:
        try:
            return self._items[name]
        except KeyError as exc:
            raise KeyError(f"response composer {name!r} is not registered") from exc
