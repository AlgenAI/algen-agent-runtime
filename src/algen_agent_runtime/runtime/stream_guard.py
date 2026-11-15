"""Streamed model text, released only after the run's output policies have seen it.

A model that streams sends its answer in small pieces; the Runtime records them as
``model.delta`` events, which conversations forward to the client as
``assistant.message.delta``. The final answer goes through the ``after_model`` and
``final_response`` policies (secret and PII redaction, output validation), so the pieces must
too, or a client would see what the final answer masks.

:class:`StreamTextGuard` holds the last ``holdback`` characters back, releases text up to a
whitespace boundary before them, and runs the policies on the released text together with the
``context`` characters before it, so a secret or an identifier split across pieces is still
recognised:

* a match that starts in released text and ends in later text (``api_key: `` released, then
  the key) changes only the new text, which is redacted as the policies redact it in context;
* text is released only when the policies give it the same result with and without the text
  held back after it, so a card number cut by the boundary is not released half-masked.

When a policy refuses the text (``output_validation`` finding privileged instructions, say),
the guard stops releasing anything for that response: the run's own ``after_model`` check
then decides, as it does without streaming.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

#: Characters held back from the end of the streamed text until more arrives or it ends.
STREAM_HOLDBACK_CHARS = 128
#: Characters of already released text the policies see before the text being released.
STREAM_CONTEXT_CHARS = 256

#: Checks text: returns it as the policies deliver it, or None when they refuse it.
TextCheck = Callable[[str], Awaitable[str | None]]


class StreamTextGuard:
    """Hold streamed text back and release it through ``check`` (see the module docstring)."""

    def __init__(
        self,
        check: TextCheck,
        *,
        holdback: int = STREAM_HOLDBACK_CHARS,
        context: int = STREAM_CONTEXT_CHARS,
    ) -> None:
        self._check = check
        self._holdback = max(0, holdback)
        self._context = max(0, context)
        # Text from absolute offset ``_base``: the released context, then unreleased text.
        self._buffer = ""
        self._base = 0
        self._released = 0  # absolute offset of the first unreleased character
        self.withheld = False
        self.emitted = False

    def add(self, delta: str) -> None:
        if not self.withheld:
            self._buffer += delta

    async def release(self, *, final: bool = False) -> str:
        """The text to send now, policies applied; empty when nothing is due (or withheld)."""
        if self.withheld:
            return ""
        end = self._base + len(self._buffer)
        limit = end if final else end - self._holdback
        if limit <= self._released:
            return ""
        cut = limit
        if not final:
            # Prefer a whitespace boundary, so a word or a token is released whole.
            for index in range(limit - 1, self._released - 1, -1):
                if self._buffer[index - self._base].isspace():
                    cut = index + 1
                    break
        start = max(self._base, self._released - self._context)
        context = self._slice(start, self._released)
        segment = self._slice(self._released, cut)
        before = await self._checked(context)
        current = await self._checked(context + segment)
        if before is None or current is None:
            return self._withhold()
        if cut < end:
            following = await self._checked(context + segment + self._slice(cut, end))
            if following is None:
                return self._withhold()
            if not following.startswith(current):
                # A match runs across the boundary: wait until the text after it arrives.
                return ""
        if not current.startswith(before):
            # A match reaches back into text already sent; the final answer is authoritative.
            return self._withhold()
        released = current[len(before) :]
        self._released = cut
        keep_from = max(self._base, self._released - self._context)
        self._buffer = self._buffer[keep_from - self._base :]
        self._base = keep_from
        if released:
            self.emitted = True
        return released

    def _slice(self, start: int, stop: int) -> str:
        return self._buffer[start - self._base : stop - self._base]

    def _withhold(self) -> str:
        self.withheld = True
        self._buffer = ""
        return ""

    async def _checked(self, text: str) -> str | None:
        if not text.strip():
            return text
        return await self._check(text)
