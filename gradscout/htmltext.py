"""Convert HTML job descriptions to readable plain text.

Stdlib only (no bs4). Preserves useful structure — paragraphs become newlines,
list items become "- " bullets — so requirements/qualifications stay legible for
the deterministic eligibility rules in Phase 3. Some sources (e.g. Greenhouse)
HTML-escape their content, so callers should html.unescape() before calling, or
rely on the double-unescape guard here.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "header", "footer",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "li":
            self._parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(value: str | None) -> str | None:
    if value is None:
        return None
    # Guard for sources that HTML-escape their markup (e.g. "&lt;p&gt;").
    if "<" not in value and "&lt;" in value:
        value = html.unescape(value)

    parser = _TextExtractor()
    parser.feed(value)
    text = parser.text()

    # Collapse runs of spaces/tabs, trim each line, drop excess blank lines.
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    result = "\n".join(out).strip()
    return result or None
