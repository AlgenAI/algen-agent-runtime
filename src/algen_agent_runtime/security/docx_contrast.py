"""Work out what a DOCX run is drawn on, so text is judged by contrast, not colour alone.

White text on a dark table cell, highlight or sidebar text box is ordinary design. Text is
only treated as invisible when it nearly matches its own background. A background that
cannot be resolved (a theme colour) is never judged, so it cannot cause a false alarm.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from xml.etree import ElementTree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
UNKNOWN = -1
WHITE = 0xFFFFFF
# Contrast ratio below which text is indistinguishable from its background (WCAG scale).
_INVISIBLE_RATIO = 1.25
_HIGHLIGHTS = {
    "black": 0x000000,
    "blue": 0x0000FF,
    "cyan": 0x00FFFF,
    "green": 0x00FF00,
    "magenta": 0xFF00FF,
    "red": 0xFF0000,
    "yellow": 0xFFFF00,
    "white": 0xFFFFFF,
    "darkblue": 0x000080,
    "darkcyan": 0x008080,
    "darkgreen": 0x008000,
    "darkmagenta": 0x800080,
    "darkred": 0x800000,
    "darkyellow": 0x808000,
    "darkgray": 0x808080,
    "lightgray": 0xC0C0C0,
}


def hex_color(raw: str | None) -> int | None:
    value = (raw or "").lstrip("#")
    return int(value, 16) if re.fullmatch(r"[0-9A-Fa-f]{6}", value) else None


def _resolved(raw: str | None) -> int:
    value = hex_color(raw)
    return UNKNOWN if value is None else value


def shading(properties: ElementTree.Element | None) -> int | None:
    """Fill of a w:shd in run, paragraph or cell properties; None when there is none."""
    shd = properties.find(f"{W}shd") if properties is not None else None
    if shd is None or shd.get(f"{W}fill", "auto").lower() == "auto":
        return None
    return _resolved(shd.get(f"{W}fill"))


def run_background(properties: ElementTree.Element | None, inherited: int) -> int:
    highlight = properties.find(f"{W}highlight") if properties is not None else None
    name = (highlight.get(f"{W}val") or "") if highlight is not None else ""
    if name and name.lower() != "none":
        return _HIGHLIGHTS.get(name.lower(), UNKNOWN)
    fill = shading(properties)
    return inherited if fill is None else fill


def _shape_fill(element: ElementTree.Element) -> int | None:
    if element.get("fillcolor"):  # VML text box
        return _resolved(element.get("fillcolor"))
    for child in element:
        if child.tag.endswith("}spPr"):  # DrawingML shape properties
            solid = child.find(f"{_A}solidFill")
            if solid is None:
                return None
            rgb = solid.find(f"{_A}srgbClr")
            return _resolved(rgb.get("val") if rgb is not None else None)
    return None


def page_background(root: ElementTree.Element) -> int:
    background = root.find(f"{W}background")
    if background is None or background.get(f"{W}color") in {None, "auto"}:
        return WHITE
    return _resolved(background.get(f"{W}color"))


def paragraphs_with_background(
    element: ElementTree.Element, inherited: int
) -> Iterator[tuple[ElementTree.Element, int]]:
    """Every paragraph, in document order, with the fill it is drawn on."""
    for child in element:
        fill = inherited
        if child.tag == f"{W}tc":
            cell = shading(child.find(f"{W}tcPr"))
            fill = inherited if cell is None else cell
        else:
            shape = _shape_fill(child)
            fill = inherited if shape is None else shape
        if child.tag == f"{W}p":
            own = shading(child.find(f"{W}pPr"))
            yield child, fill if own is None else own
        yield from paragraphs_with_background(child, fill)


def _luminance(rgb: int) -> float:
    def channel(value: int) -> float:
        scaled = value / 255
        return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4

    red, green, blue = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def is_invisible(text_color: int | None, background: int) -> bool:
    if text_color is None or background == UNKNOWN:
        return False
    lighter, darker = sorted((_luminance(text_color), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05) < _INVISIBLE_RATIO
