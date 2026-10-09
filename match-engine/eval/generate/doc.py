"""The neutral intermediate document (EVAL.md section 2.1).

A Jinja2 template emits a small line-oriented markup; `parse_markup` turns it into a `Document`
of headings, paragraphs, bullet lists and side-blocks that every format renderer lays out in its
own way. One directive per line, `@kind field || field || field`:

    @name Alex Example                       the person (first line, once)
    @headline Senior Software Engineer       optional line under the name
    @contact email || a@example.com || icon  contact items, before the first section (icon optional)
    @runhead Alex Example - Federal Resume   a running header for paged formats (optional)
    @section skills || Technical Skills || icon    starts a section; `kind` tells renderers what it holds
    @side ... @endside                       wraps whole sections that belong in the side column
    @entry title || organisation || dates || place   a role, project or degree (all but title optional)
    @bullet text        @para text        @kv label || value
    @group label || item, item            @skills item, item        @bar label || level (0-5)

Every field is a single line, so a field must never contain `||` or a line break; `clean_field`
makes sure of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SEPARATOR = "||"

_ITEM_FIELDS = {  # directive -> (min fields, max fields)
    "entry": (1, 4),
    "bullet": (1, 1),
    "para": (1, 1),
    "kv": (2, 2),
    "group": (2, 2),
    "skills": (1, 1),
    "bar": (2, 2),
}


def clean_field(text: str) -> str:
    """One line, no field separator, single spaces."""
    return re.sub(r"\s+", " ", str(text).replace(SEPARATOR, "|")).strip()


@dataclass(frozen=True)
class Item:
    kind: str  # entry | bullet | para | kv | group | skills | bar
    args: tuple[str, ...]

    def arg(self, i: int) -> str:
        return self.args[i] if i < len(self.args) else ""


@dataclass(frozen=True)
class Section:
    kind: str  # summary, experience, projects, education, certifications, skills, ...
    title: str
    icon: str
    side: bool
    items: tuple[Item, ...]


@dataclass(frozen=True)
class Contact:
    kind: str  # email | phone | place | link
    text: str
    icon: str = ""


@dataclass(frozen=True)
class Document:
    name: str
    headline: str
    contacts: tuple[Contact, ...]
    sections: tuple[Section, ...]
    running_head: str = ""

    @property
    def has_side(self) -> bool:
        return any(s.side for s in self.sections)

    def with_sidebar(self, kinds: frozenset[str]) -> "Document":
        """The same document with every section of the given kinds moved to the side column."""
        moved = tuple(
            Section(s.kind, s.title, s.icon, True, s.items) if s.kind in kinds else s for s in self.sections
        )
        return Document(self.name, self.headline, self.contacts, moved, self.running_head)


def _fields(payload: str) -> list[str]:
    return [f.strip() for f in payload.split(SEPARATOR)]


def parse_markup(text: str) -> Document:
    name = headline = running_head = ""
    contacts: list[Contact] = []
    sections: list[Section] = []
    current: tuple[str, str, str, bool] | None = None
    items: list[Item] = []
    in_side = False

    def close() -> None:
        nonlocal current, items
        if current is not None:
            sections.append(Section(*current, tuple(items)))
        current, items = None, []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("@"):
            raise ValueError(f"line {number}: expected a directive, got {line[:40]!r}")
        kind, _, payload = line[1:].partition(" ")
        fields = _fields(payload)
        if kind == "name":
            if name:
                raise ValueError(f"line {number}: a document has one @name")
            name = payload.strip()
        elif kind == "headline":
            headline = payload.strip()
        elif kind == "runhead":
            running_head = payload.strip()
        elif kind == "contact":
            if sections or current is not None:
                raise ValueError(f"line {number}: @contact belongs before the first section")
            if not 2 <= len(fields) <= 3:
                raise ValueError(f"line {number}: @contact takes kind || text [|| icon]")
            contacts.append(Contact(fields[0], fields[1], fields[2] if len(fields) == 3 else ""))
        elif kind == "side":
            if in_side:
                raise ValueError(f"line {number}: @side inside @side")
            close()
            in_side = True
        elif kind == "endside":
            if not in_side:
                raise ValueError(f"line {number}: @endside without @side")
            close()
            in_side = False
        elif kind == "section":
            if not 2 <= len(fields) <= 3:
                raise ValueError(f"line {number}: @section takes kind || title [|| icon]")
            close()
            current = (fields[0], fields[1], fields[2] if len(fields) == 3 else "", in_side)
        elif kind in _ITEM_FIELDS:
            low, high = _ITEM_FIELDS[kind]
            if current is None:
                raise ValueError(f"line {number}: @{kind} outside a section")
            if not low <= len(fields) <= high:
                raise ValueError(f"line {number}: @{kind} takes {low}-{high} fields, got {len(fields)}")
            items.append(
                Item(kind, tuple(fields) if kind in ("entry", "kv", "group", "bar") else (payload.strip(),))
            )
        else:
            raise ValueError(f"line {number}: unknown directive @{kind}")
    if in_side:
        raise ValueError("@side was never closed")
    close()
    if not name:
        raise ValueError("a document needs a @name")
    return Document(name, headline, tuple(contacts), tuple(sections), running_head)
