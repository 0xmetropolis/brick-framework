"""Small helpers shared by the tools that emit Mermaid: safe text and stable node ids. Standard library only."""

from __future__ import annotations

import re

_ESCAPES = {
    "#": "#35;",
    "&": "#38;",
    ";": "#59;",
    '"': "#quot;",
    "<": "#lt;",
    ">": "#gt;",
    "|": "#124;",
    "%": "#37;",
    "`": "#96;",
    "\n": " ",
    "\r": "",
}
_TABLE = str.maketrans(_ESCAPES)


def escape(text: str) -> str:
    """Make text safe inside a quoted label, a participant alias or a message.

    One pass over the characters, so the entity codes written for one character are
    never escaped again by another. A newline becomes a space; a label is one line.
    """
    return text.translate(_TABLE)


def identifier(prefix: str, name: str, taken: set[str]) -> str:
    """A node id made of ``prefix`` and the name's safe characters, unique within ``taken``.

    Ids are chosen in the order the caller asks, so asking in sorted order gives the
    same ids every time. The prefix keeps a brick called ``end`` or ``graph`` from
    colliding with a Mermaid keyword.
    """
    base = prefix + re.sub(r"[^A-Za-z0-9_]", "_", name)
    candidate, count = base, 1
    while candidate in taken:
        count += 1
        candidate = f"{base}_{count}"
    taken.add(candidate)
    return candidate
