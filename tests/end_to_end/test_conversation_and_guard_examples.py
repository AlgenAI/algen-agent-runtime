"""What the quickstart_conversation and pattern_guarded_tools examples demonstrate, in-process."""

from __future__ import annotations

import pytest

from examples.pattern_guarded_tools.app import demonstrate
from examples.quickstart_conversation.app import NEW_ADDRESS, run


async def test_conversation_example_streams_steers_and_resumes_after_approval(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outcome = await run("approved")
    printed = capsys.readouterr().out

    assert outcome["status"] == "completed" and outcome["address"] == NEW_ADDRESS
    assert "[tool note] orders.track: Checked the carrier for A-100" in printed
    assert "[steering] 1 message(s) delivered to the model" in printed
    assert "its next call ended with the steering message" in printed
    assert "[assistant] resumed: approved by example-user" in printed
    # Streamed word by word: the answer arrives as several delta events on one line.
    assert "assistant> Order A-100 is in transit" in printed


async def test_conversation_example_rejection_leaves_the_address() -> None:
    outcome = await run("rejected")

    assert outcome["status"] == "failed" and outcome["address"] != NEW_ADDRESS


async def test_guarded_tools_example_shows_every_control(
    capsys: pytest.CaptureFixture[str],
) -> None:
    outcome = await demonstrate()
    printed = capsys.readouterr().out

    assert outcome == {
        "guards": "completed",
        # Five set_discount calls: the precheck refuses three, so two reach the handler.
        "discount_handler_calls": 2,
        "my_orders_handler_runs": 2,
        "meter_refused": "failed",
    }
    assert "repeated (tool.repeated)" in printed
    assert "refusal 2 in a row; the note says stop calling it" in printed
    assert "shop.set_discount  failed" in printed
    assert "over_limit (tool.call_limit)" in printed
    assert "wrap-up notice added" in printed
    assert "alice  completed: served from the cache" in printed
    assert "bob    completed: the handler ran" in printed
    assert "meter refused the mock/deterministic call: carol has 0 credits left" in printed
