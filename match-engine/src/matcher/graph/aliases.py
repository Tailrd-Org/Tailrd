"""Surface-form normalisation shared by the alias index and every lookup (SPEC §3.3).

The same function produces the keys the resolver indexes (labels, aliases) and the keys it looks
up (résumé and posting text), so a surface matches exactly when both sides normalise alike.
"""

from __future__ import annotations

import unicodedata

# NFKC would spell these out as letters ("Java™" -> "JavaTM"), so they become spaces first.
_BEFORE_NFKC = str.maketrans({"\u2122": " ", "\u2120": " "})  # ™ ℠

# Invisible characters that split words without meaning to (PDF and word-processor artefacts).
INVISIBLE = "\u00ad\u200b\u200c\u200d\ufeff"  # soft hyphen, zero-width space/non-joiner/joiner, BOM
APOSTROPHES = "'\u2018\u2019"
# U+2010..U+2015: hyphen, non-breaking hyphen (NFKC folds it into U+2010), figure dash, en dash,
# em dash, horizontal bar. All of them read as "-".
DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015"
_TO_SPACE = '()[]{},;:!?"*|\u00ae\u2122\u00a9'  # ® ™ ©

_TABLE = str.maketrans(
    {
        **{ch: None for ch in INVISIBLE + APOSTROPHES},
        **{ch: "-" for ch in DASHES},
        "\u201c": " ",  # curly double quotes -> '"' -> space
        "\u201d": " ",
        **{ch: " " for ch in _TO_SPACE},
    }
)
_LEAD = "-/_"
_TRAIL = "-/_."  # trailing dots go too ("Node.js." -> "node.js"); a leading dot stays (".net")

# ASCII fast path (the taxonomy loads ~200k surfaces per cold start): NFKC leaves ASCII unchanged,
# `_BEFORE_NFKC` maps no ASCII character and casefold() is lower() on ASCII, so for ASCII text the
# pipeline is one byte-level translate with the ASCII part of `_TABLE`: the apostrophe goes, the
# ASCII characters of `_TO_SPACE` become spaces. Same result, a fraction of the cost.
_ASCII_TO_SPACE = "".join(ch for ch in _TO_SPACE if ch.isascii()).encode("ascii")
_ASCII_TABLE = bytes.maketrans(_ASCII_TO_SPACE, b" " * len(_ASCII_TO_SPACE))
_ASCII_DELETE = "".join(ch for ch in APOSTROPHES if ch.isascii()).encode("ascii")


def _normalize(text: str, fold: bool) -> str:
    if text.isascii():
        text = text.encode("ascii").translate(_ASCII_TABLE, _ASCII_DELETE).decode("ascii")
        if fold:
            text = text.lower()
    else:
        text = unicodedata.normalize("NFKC", text.translate(_BEFORE_NFKC)).translate(_TABLE)
        if fold:
            text = text.casefold()
    if "-" not in text and "/" not in text and "_" not in text and "." not in text:
        return " ".join(text.split())  # nothing to strip from any token: the same result, cheaper
    tokens = (token.lstrip(_LEAD).rstrip(_TRAIL) for token in text.split())
    return " ".join(token for token in tokens if token)


def normalize_surface(text: str) -> str:
    """Canonical lookup key for a surface form.

    NFKC; drop invisible characters and apostrophes; curly double quotes and the bracket and
    list punctuation become spaces; Unicode dashes become "-"; casefold; strip "-", "/", "_" from
    both ends of every token and dots from its end; collapse whitespace. Inner and leading-dot
    forms survive: ".net", "node.js", "c++", "c#", "ci/cd", "scikit-learn".
    """
    return _normalize(text, fold=True)


def normalize_keep_case(text: str) -> str:
    """`normalize_surface` without the casefold: what ambiguous keys compare against ("Go")."""
    return _normalize(text, fold=False)


def surface_variants(normalized: str) -> tuple[str, ...]:
    """Lookup keys to try, in order: the form itself, then hyphens as spaces, then hyphens dropped.

    `surface_variants("front-end")` -> ("front-end", "front end", "frontend"); a form without a
    hyphen is its own only variant. An empty form has none.
    """
    if not normalized:
        return ()
    if "-" not in normalized:
        return (normalized,)
    variants = [normalized]
    for candidate in (" ".join(normalized.replace("-", " ").split()), normalized.replace("-", "")):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return tuple(variants)
