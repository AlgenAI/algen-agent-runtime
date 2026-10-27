"""Tool names at the provider boundary: readable where they can be, and mapped back even when a
model shortens or rewrites them."""

from __future__ import annotations

from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.types.contracts import ModelCapabilities


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "custom", "https://model.example/v1", None, "demo", ModelCapabilities()
    )


def test_a_dotted_name_is_sent_readable_and_mapped_back() -> None:
    provider = _provider()
    assert provider._wire_tool_name("hr.find_people") == "hr__find_people"
    assert provider._wire_tool_name("plain_name") == "plain_name"
    assert provider._runtime_tool_name("hr__find_people") == "hr.find_people"
    # A model that writes the Runtime name itself is understood too.
    assert provider._runtime_tool_name("hr.find_people") == "hr.find_people"


def test_a_name_too_long_to_read_gets_a_hash_and_its_stem_maps_back() -> None:
    provider = _provider()
    name = "recruiting." + "x" * 60
    alias = provider._wire_tool_name(name)
    assert len(alias) <= 64 and alias != name.replace(".", "__")
    stem = alias.rsplit("_", 1)[0]
    # The stem-only name a model sometimes returns, without the hash suffix.
    assert provider._runtime_tool_name(stem) == name


def test_a_readable_alias_never_takes_another_tools_name() -> None:
    dotted_first = _provider()
    assert dotted_first._wire_tool_name("hr.find") == "hr__find"
    native = dotted_first._wire_tool_name("hr__find")
    assert native != "hr__find"
    assert dotted_first._runtime_tool_name("hr__find") == "hr.find"
    assert dotted_first._runtime_tool_name(native) == "hr__find"

    native_first = _provider()
    assert native_first._wire_tool_name("hr__find") == "hr__find"
    hashed = native_first._wire_tool_name("hr.find")
    assert hashed.startswith("hr_find_") and native_first._runtime_tool_name(hashed) == "hr.find"


def test_an_ambiguous_or_unknown_name_is_returned_unchanged() -> None:
    provider = _provider()
    first = provider._wire_tool_name("a." + "y" * 70)
    second = provider._wire_tool_name("a_" + "y" * 70)
    assert first.rsplit("_", 1)[0] == second.rsplit("_", 1)[0]
    stem = first.rsplit("_", 1)[0]
    assert provider._runtime_tool_name(stem) == stem
    assert provider._runtime_tool_name("no_such_tool") == "no_such_tool"
