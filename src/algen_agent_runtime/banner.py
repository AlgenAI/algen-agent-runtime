"""The startup banner a server prints before it starts: what is being served, and where.

``algen-agent-runtime serve`` prints it when its output is a terminal. An application that runs
its own server prints it with :func:`print_startup_banner` before ``uvicorn.run``, and can name
itself and add its own rows::

    print_startup_banner(
        settings,
        title="ms-email-test",
        tagline="a governed Outlook agent",
        details={"mailbox": "fake (in-process Graph)"},
    )

The banner shows only names from the settings (agents, models, tool and guard counts, URLs),
never secrets or connection strings.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from importlib import metadata
from typing import TYPE_CHECKING, TextIO

from algen_agent_runtime import __version__

if TYPE_CHECKING:
    from algen_agent_runtime.config.settings import AppSettings

WIDTH = 62  # inside the frame

_LOGO = (
    " █████╗ ██╗      ██████╗ ███████╗███╗   ██╗",
    "██╔══██╗██║     ██╔════╝ ██╔════╝████╗  ██║",
    "███████║██║     ██║  ███╗█████╗  ██╔██╗ ██║",
    "██╔══██║██║     ██║   ██║██╔══╝  ██║╚██╗██║",
    "██║  ██║███████╗╚██████╔╝███████╗██║ ╚████║",
    "╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝",
)
# algen.ai's gold, light at the top to deep amber at the bottom.
_GOLD = (
    (247, 214, 120),
    (240, 196, 84),
    (226, 176, 48),
    (210, 157, 25),
    (186, 134, 18),
    (156, 110, 14),
)
_ACCENT = (210, 157, 25)
_MUTED = (138, 128, 112)
_FRAME = (96, 88, 76)


def startup_banner(
    settings: AppSettings,
    *,
    title: str | None = None,
    tagline: str | None = None,
    details: Mapping[str, str] | None = None,
    url: str | None = None,
    color: bool = False,
    unicode: bool = True,
) -> str:
    """The banner as text.

    ``title`` and ``tagline`` default to the agent's name and description when the settings
    declare one agent. ``details`` adds rows after the built-in ones, or replaces a built-in row
    with the same label (``agent``, ``model``, ``tools``, ``guards``, ``workflows``, ``api``,
    ``ui``). ``url`` is the server's base URL (default: ``api.host`` and ``api.port``).
    ``unicode=False`` draws a plain ASCII frame without the logo, for terminals that cannot show
    box-drawing characters.
    """
    agents = settings.agents
    only = agents[0] if len(agents) == 1 else None
    title = title or (only.name if only else "Algen Agent Runtime")
    if tagline is None and only is not None:
        tagline = only.description
    base = (url or f"http://{settings.api.host}:{settings.api.port}").rstrip("/")

    rows = dict(_rows(settings, base))
    for label, value in (details or {}).items():
        rows[label] = value

    built_with = f"built with Algen Agent Runtime {__version__}"
    if tools_version := _version("algen-agent-tools"):
        built_with += f" · Tools {tools_version}"

    paint = _Painter(color)
    lines: list[str] = [""]
    if unicode:
        lines += [paint.rgb(f"    {line}", _GOLD[index]) for index, line in enumerate(_LOGO)]
        lines.append("")
    mark = "✻" if unicode else "*"
    heading = f"  {mark} {title}" + (f" · {tagline}" if tagline else "")
    lines.append(paint.bold(paint.rgb(_fit(heading), _ACCENT)))
    lines.append(paint.rgb(_fit(f"    {built_with}"), _MUTED))
    lines.append("")
    label_width = max((len(label) for label in rows), default=0)
    for label, value in rows.items():
        head = f"    {label.ljust(label_width)}  "
        text = _fit(head + value)
        lines.append(paint.rgb(head, _MUTED) + text[len(head) :])
    lines.append("")
    if not unicode:
        lines = [line.replace("·", "-").replace("…", "~") for line in lines]
    return _frame(lines, paint, unicode)


def print_startup_banner(
    settings: AppSettings,
    *,
    title: str | None = None,
    tagline: str | None = None,
    details: Mapping[str, str] | None = None,
    url: str | None = None,
    file: TextIO | None = None,
) -> None:
    """Print the banner, in colour when ``file`` (default stdout) is a terminal that has it.

    ``NO_COLOR`` turns colour off and ``FORCE_COLOR`` on. A stream whose encoding cannot show
    the logo gets the plain ASCII frame.
    """
    stream = file or sys.stdout
    text = startup_banner(
        settings,
        title=title,
        tagline=tagline,
        details=details,
        url=url,
        color=_wants_color(stream),
        unicode=_can_encode(stream, "█╗╭─✻·…"),
    )
    print(text, file=stream, flush=True)


def _rows(settings: AppSettings, base: str) -> list[tuple[str, str]]:
    agents = settings.agents
    rows: list[tuple[str, str]] = []
    if len(agents) == 1:
        rows.append(("agent", f"{agents[0].name} {agents[0].version}"))
    elif agents:
        rows.append(("agents", f"{len(agents)} · " + ", ".join(agent.name for agent in agents)))
    models = []
    for agent in agents:
        profile = agent.default_model
        name = " · ".join(part for part in (profile.provider, profile.model) if part)
        if name and name not in models:
            models.append(name)
    if models:
        rows.append(("model" if len(models) == 1 else "models", ", ".join(models)))
    tools = set(tool for agent in agents for tool in agent.enabled_tools)
    if tools:
        rows.append(("tools", f"{len(tools)} enabled"))
    guards = list(dict.fromkeys(p for agent in agents for p in agent.guardrail_policy.policies))
    if guards:
        rows.append(("guards", f"{len(guards)} active · " + ", ".join(guards)))
    if settings.workflows:
        rows.append(("workflows", ", ".join(sorted(settings.workflows))))
    rows.append(("api", base))
    if settings.api.agent_ui_enabled:
        rows.append(("ui", base + settings.api.agent_ui_path))
    return rows


def _frame(lines: list[str], paint: _Painter, unicode: bool) -> str:
    top, bottom, side = ("╭╮", "╰╯", "│") if unicode else ("++", "++", "|")
    rule = ("─" if unicode else "-") * WIDTH
    edge = paint.rgb(side, _FRAME)
    framed = [paint.rgb(top[0] + rule + top[1], _FRAME)]
    for line in lines:
        padding = " " * (WIDTH - _visible_len(line))
        framed.append(f"{edge}{line}{padding}{edge}")
    framed.append(paint.rgb(bottom[0] + rule + bottom[1], _FRAME))
    return "\n".join(framed)


def _fit(text: str, width: int = WIDTH - 2) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _visible_len(text: str) -> int:
    length = 0
    escape = False
    for char in text:
        if char == "\x1b":
            escape = True
        elif escape:
            escape = char != "m"
        else:
            length += 1
    return length


def _version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _wants_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty()) and os.environ.get("TERM") != "dumb"


def _can_encode(stream: TextIO, sample: str) -> bool:
    try:
        sample.encode(getattr(stream, "encoding", None) or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


class _Painter:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def rgb(self, text: str, rgb: tuple[int, int, int]) -> str:
        if not self.enabled:
            return text
        red, green, blue = rgb
        return f"\x1b[38;2;{red};{green};{blue}m{text}\x1b[0m"

    def bold(self, text: str) -> str:
        return f"\x1b[1m{text}\x1b[0m" if self.enabled else text
