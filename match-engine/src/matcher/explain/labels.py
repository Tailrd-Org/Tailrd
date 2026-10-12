"""Display text shared by the explanation layer (SPEC §8, §17.2, §18).

* `skill_label`: a canonical id as a person reads it. A graph node's label is already display text,
  except ESCO's, which are lower-case phrases ("accounting techniques"): those get a capital first
  letter, unless the second letter is a capital too ("pH meters"). An id the graph does not hold
  (an interval pseudo-requirement, a skill the taxonomy has not placed yet) is shown from its own
  spelling: the part after the namespace, underscores as spaces, the first letter capitalised.
* `join_labels`: "A", "A and B", "A, B and C".
* `flag_sentence`: the plain sentence of a reason code (`matcher.errors.FLAG_TEXT`); a code without
  one gets a generic sentence rather than its raw name, so no candidate ever reads "LOW_PARSE_QUALITY".
* `signed`: an impact on the raw scale, signed, two decimals (SPEC §8: "+0.31").
* `years`: months as years to one decimal.

Pure: no I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Sequence

from matcher.errors import FLAG_TEXT, Flag
from matcher.graph.model import SkillGraph

__all__ = ["flag_sentence", "join_labels", "signed", "skill_label", "years"]

_GENERIC = "Some information affected this result."
_LOWER_CASE_SOURCES = frozenset({"esco"})
_MAX_LABEL = 120  # a label longer than this is cut, so one odd node never floods a sentence


def _capitalised(text: str) -> str:
    if not text or not text[0].islower():
        return text
    if len(text) > 1 and text[1].isupper():  # "pH meters", "iOS", "eBay": a deliberate casing
        return text
    return text[0].upper() + text[1:]


def _from_id(skill_id: str) -> str:
    """A readable name for an id the graph does not hold: `skill.foo_bar` -> "Foo bar",
    `unresolved:kafka streams` -> "Kafka streams"."""
    text = skill_id
    for separator in (":", "."):
        if separator in text:
            text = text.split(separator, 1)[1]
            break
    text = " ".join(text.replace("_", " ").split())
    return _capitalised(text) or skill_id


def _cut(text: str) -> str:
    return text if len(text) <= _MAX_LABEL else text[: _MAX_LABEL - 3].rstrip() + "..."


def skill_label(graph: SkillGraph, skill_id: str) -> str:
    """`skill_id` in display casing (see the module docstring)."""
    node = graph.node(skill_id)
    if node is None or not node.label.strip():
        return _cut(_from_id(skill_id))
    label = " ".join(node.label.split())
    if node.source in _LOWER_CASE_SOURCES:
        label = _capitalised(label)
    return _cut(label)


def join_labels(labels: Sequence[str]) -> str:
    """"A", "A and B", "A, B and C"."""
    items = [label for label in labels if label]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def flag_sentence(flag: Flag) -> str:
    """The candidate-facing sentence for a reason code."""
    try:
        return FLAG_TEXT.get(Flag(flag), _GENERIC)
    except ValueError:
        return _GENERIC


def signed(value: float) -> str:
    """`+0.31` / `-0.08`, rounded first: a value that rounds to zero is "0.00", never "-0.00"."""
    rounded = round(value, 2)
    if rounded == 0:
        return "0.00"
    return f"{rounded:+.2f}"


def years(months: float) -> str:
    """Months as years to one decimal ("3.5")."""
    return f"{max(0.0, months) / 12.0:.1f}"
