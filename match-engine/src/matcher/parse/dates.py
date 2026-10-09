"""Dates (SPEC §14.2): every date format a résumé uses, as one standalone, total module.

"Date parsing will produce more bugs than any other single component" (SPEC §14.2), so it stands
alone with its own exhaustive test file (tests/test_dates.py). Three entry points:

* `parse_date`: one date filling the whole text ("Jan 2020", "Summer 2020", "98", "Present").
* `parse_range`: one range; the whole text when it is a range, else the first range inside it.
* `find_date_ranges`: every range inside a longer line, with offsets, so the role parser can cut
  the date out of a header ("Software Intern, Acme — May 2023 – Aug 2023, Ottawa").

How: a lexer finds date *atoms* (a dated point, a month or season with no year, a present
marker, or a range written as one token such as "2018/19"); two atoms joined by a separator
become a range; each range is then settled against `as_of` (inverted ranges swapped or dropped,
future starts dropped, future ends clamped). Reading a whole text accepts a bare two-digit year
("98"); searching inside a line does not, and rejects numbers that are plainly not dates
(`$2020`, `2020 users`, phone numbers).

Interpretations beyond the SPEC table (pinned by tests): month and season names are read in
both languages whatever `lang` says (mixed-language résumés, SPEC §14.1; `lang` only orders an
ambiguous 03/04/2020); "2018-19" is an academic year like "2018/19", while "2011/12" reads as
December 2011 because a valid month wins; "Mon D(D)" never invents a year: beside a four-digit
year it is a day in that year ("June 15 – August 15, 2020"), and with no four-digit year beside
it, it is no date ("May 15", "June 15 – August 30", "May 15 – Present"), because after a month
two digits are a year only with an apostrophe ("Jan '19") or when they cannot be a day (00 and
32–99: "Jan 98"); an imprecise start whose period contains `as_of`
("Fall 2026 – Present" in October 2026) has begun and is kept inside [earliest, as_of]; no bound
of a returned range lies after `as_of`; a dropped range carries only the reason it was dropped.

Never raises on any `str`, never reads the clock (`as_of` is a parameter), and imports only the
standard library and pydantic (DECISIONS D-02).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Literal, Optional

from pydantic import model_validator

from matcher.errors import Flag
from matcher.models import DatePrecision, DateValue, Frozen

__all__ = ["DateRange", "find_date_ranges", "months_between", "parse_date", "parse_range"]

_DAYS_PER_MONTH = 30.4375  # 365.25 / 12
_YEARS_BACK = 80  # the oldest plausible year, and the two-digit pivot (SPEC §14.2)
_YEARS_AHEAD = 10  # the newest plausible four-digit year
_MAX_RANGE_YEARS = 50  # a longer range is not a role (brief row 9)
_EARLIEST_CONVERTED = 1900  # a converted calendar year before this is not a career date

_IMPRECISE = frozenset({DatePrecision.SEASON, DatePrecision.QUARTER, DatePrecision.YEAR})
# Quarter → (first day, midpoint, last day) as (month, day); seasons map to quarters.
_QUARTER_DAYS: dict[int, tuple[tuple[int, int], ...]] = {
    1: ((1, 1), (2, 15), (3, 31)),
    2: ((4, 1), (5, 16), (6, 30)),
    3: ((7, 1), (8, 16), (9, 30)),
    4: ((10, 1), (11, 16), (12, 31)),
}
_YEAR_DAYS = ((1, 1), (7, 1), (12, 31))
_ACADEMIC_FIRST_MONTH, _ACADEMIC_LAST_MONTH = 9, 4  # an academic year runs September to April

_MONTHS = {
    "january": 1, "jan": 1, "janvier": 1, "janv": 1,
    "february": 2, "feb": 2, "février": 2, "fevrier": 2, "févr": 2, "fevr": 2, "fév": 2, "fev": 2,
    "march": 3, "mar": 3, "mars": 3,
    "april": 4, "apr": 4, "avril": 4, "avr": 4,
    "may": 5, "mai": 5,
    "june": 6, "jun": 6, "juin": 6,
    "july": 7, "jul": 7, "juillet": 7, "juil": 7,
    "august": 8, "aug": 8, "août": 8, "aout": 8,
    "september": 9, "sep": 9, "sept": 9, "septembre": 9,
    "october": 10, "oct": 10, "octobre": 10,
    "november": 11, "nov": 11, "novembre": 11,
    "december": 12, "dec": 12, "décembre": 12, "decembre": 12, "déc": 12,
}  # fmt: skip
_SEASONS = {
    "winter": 1, "hiver": 1, "spring": 2, "printemps": 2, "summer": 3, "été": 3, "ete": 3,
    "fall": 4, "autumn": 4, "automne": 4,
}  # fmt: skip

# Marker → (raw years that read as that calendar, conversion to a Gregorian year). The window
# keeps a marker-like word from turning an ordinary number into a calendar date: "BE 2019" is a
# Bachelor of Engineering and "1000 AM" a time, so neither is converted (nor flagged).
_CALENDARS: dict[str, tuple[tuple[int, int], Callable[[int], int]]] = {
    "BE": ((2400, 2799), lambda y: y - 543),  # Buddhist Era (Thai พ.ศ.)
    "SH": ((1200, 1599), lambda y: y + 621),  # Solar Hijri (هـ.ش)
    "AH": ((1200, 1599), lambda y: round(y * 0.970229 + 621.5643)),  # Hijri (هـ)
    "AM": ((5000, 5999), lambda y: y - 3760),  # Hebrew
}

# Arabic-Indic, Extended Arabic-Indic and Thai digits → ASCII, one character for one, so every
# offset into the translated text is an offset into the original.
_DIGITS = str.maketrans({chr(base + i): str(i) for base in (0x0660, 0x06F0, 0x0E50) for i in range(10)})


class DateRange(Frozen):
    """A parsed range. `end` is None for a current role (as on `models.Role`, `as_of` stands in
    for it); both bounds are None when the range was dropped, and `flags` say why."""

    start: Optional[DateValue]
    end: Optional[DateValue]
    is_current: bool
    flags: tuple[Flag, ...] = ()
    raw: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> "DateRange":
        if self.is_current and self.end is not None:
            raise ValueError("a current range has no end (as_of stands in for it)")
        if self.start is not None and self.end is not None and self.end.value < self.start.value:
            raise ValueError("a range never ends before it starts; the parser swaps or drops it")
        return self


def months_between(a: dt.date, b: dt.date) -> float:
    """Months from `a` to `b` at the mean Gregorian month length; negative when b is before a."""
    return (b - a).days / _DAYS_PER_MONTH


# ─── Regular expressions ─────────────────────────────────────────────────────────────────────


def _alternation(words: dict[str, int]) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=lambda w: (-len(w), w)))


def _initials(words: dict[str, int]) -> str:
    return "".join(sorted({w[0] for w in words}))


_I = re.IGNORECASE
_LB = r"(?<![^\W\d_])"  # not preceded by a letter
_LA = r"(?![^\W\d_])"  # not followed by a letter
_NB = r"(?<![0-9])"  # not preceded by a digit
_NA = r"(?![0-9])"  # not followed by a digit
_XB = r"(?<![^\W_])"  # not preceded by a letter or digit
_XA = r"(?![^\W_])"  # not followed by a letter or digit
_APOS = "['’‘`´]"
# A run of dashes, entered only at its first dash and never given back: a pattern retried at every
# position of a long run ("-" * 8000 in an uploaded file) would otherwise take quadratic time.
_DASHES = "(?<![–—‒―−‐‑~-])[–—‒―−‐‑~-]++"
# Each name list is guarded by a lookahead on its first letters: one class test rejects almost
# every position instead of trying ~45 case-insensitive alternatives there (the lexer's hot spot).
_MON = rf"{_LB}(?=[{_initials(_MONTHS)}])(?P<mon>{_alternation(_MONTHS)}){_LA}\.?"
_SEA = rf"{_LB}(?=[{_initials(_SEASONS)}])(?P<sea>{_alternation(_SEASONS)}){_LA}"
_Y4 = rf"(?P<y>[0-9]{{4}}){_NA}"
_YA = rf"(?P<ya>{_APOS}\s?[0-9]{{2}}){_NA}"
_SEP = (
    rf"(?:{_DASHES}|{_LB}(?:jusqu{_APOS}\s?(?:au|à|a)|through|thru|until|till|to|au|à){_LA})"
)
_PRESENT_CORE = (
    r"pr[ée]sent(?:\s+day)?|current(?:ly)?|now|on-?\s?going|to\s+date|till\s+date|date|today"
    r"|aujourd['’]hui|[àa]\s+ce\s+jour|ce\s+jour|en\s+cours|actuel(?:le)?(?:ment)?|maintenant"
)
_LATIN_MARK = r"(?P<mk>B\.?E\.?|S\.?H\.?|A\.?H\.?|A\.?M\.?)"  # case-sensitive: "am" is a word

_RX_ISO_DAY = re.compile(
    rf"{_NB}(?P<y>[0-9]{{4}})(?P<s>[-/.])(?P<m>[0-9]{{1,2}})(?P=s)(?P<d>[0-9]{{1,2}}){_NA}"
)
_RX_NUMERIC_DAY = re.compile(
    rf"{_NB}(?P<a>[0-9]{{1,2}})(?P<s>[-/.])(?P<b>[0-9]{{1,2}})(?P=s)(?P<y>[0-9]{{4}}){_NA}"
)
_RX_MONTH_DAY = re.compile(rf"{_MON}\s*(?P<d>[0-9]{{1,2}}){_NA}(?:st|nd|rd|th)?{_LA},?\s*{_Y4}", _I)
_RX_DAY_MONTH = re.compile(
    rf"{_NB}(?P<d>[0-9]{{1,2}}){_NA}(?:st|nd|rd|th|er)?(?:-|\s*){_MON}(?:-|,?\s*){_Y4}", _I
)
# After a month, two digits are a year with an apostrophe ("Jan '19") or when they cannot be a day
# (00, 32–99: "Jan 98"). "Jan 19" may be a day, and a wrong year would invent years of experience.
_RX_NAMED_MONTH = re.compile(
    rf"{_MON}(?:[-/]|,?\s*)(?:{_Y4}|{_YA}|(?P<yn>00|3[2-9]|[4-9][0-9]){_NA})", _I
)
# "Mon D(D)": a day whose year only a four-digit year at the other end of its range can give.
_RX_WEAK_MONTH = re.compile(rf"{_MON}\s++(?P<d>[0-2]?[0-9]|3[01]){_NA}(?:st|nd|rd|th)?{_LA}", _I)
_RX_SEASON = re.compile(rf"{_SEA}\s*(?:of\s+)?(?:[-/,]\s*)?(?:{_Y4}|{_YA}|(?P<yn>[0-9]{{2}}){_NA})", _I)
_RX_QUARTER_FIRST = re.compile(rf"{_LB}Q(?P<q>[1-4])\s*(?:[-/,]\s*)?(?:{_Y4}|{_YA})", _I)
_RX_QUARTER_LAST = re.compile(rf"{_NB}(?P<y>[0-9]{{4}})\s*+(?:[-/]\s*+)?Q(?P<q>[1-4]){_NA}{_LA}", _I)
_RX_ISO_MONTH = re.compile(rf"{_NB}(?P<y>[0-9]{{4}})[-/](?P<m>0[1-9]|1[0-2]){_NA}")
_RX_NUMERIC_MONTH = re.compile(rf"{_NB}(?P<m>0?[1-9]|1[0-2])[-/.]{_Y4}")
_RX_SHORT_END = re.compile(rf"{_NB}(?P<y>[0-9]{{4}})\s?[-/–—]\s?(?P<yy>[0-9]{{2}}){_NA}")
_RX_YEAR_SLASH = re.compile(rf"{_NB}(?P<y>[0-9]{{4}})\s?/\s?(?P<y2>[0-9]{{4}}){_NA}")
_RX_YEAR = re.compile(rf"{_XB}(?P<y>[0-9]{{4}}){_XA}")
_RX_APOSTROPHE_YEAR = re.compile(rf"{_XB}{_APOS}(?P<yy>[0-9]{{2}}){_XA}")
_RX_BARE_TWO = re.compile(rf"{_XB}(?P<yy>[0-9]{{2}}){_XA}")
_RX_MONTH_ONLY = re.compile(_MON, _I)
_RX_SEASON_ONLY = re.compile(_SEA, _I)
# The guard lists the first character of every separator and present marker above.
_RX_PRESENT = re.compile(
    rf"(?=[–—‒―−‐‑~jtuaàpcnodem-])(?:(?P<sep>{_SEP})\s*+)?{_LB}(?P<p>{_PRESENT_CORE}){_LA}", _I
)
_RX_CAL_AFTER = re.compile(rf"{_NB}(?P<y>[0-9]{{4}})\s?{_LATIN_MARK}{_LA}")
_RX_CAL_BEFORE = re.compile(rf"{_LB}{_LATIN_MARK}\s?(?P<y>[0-9]{{4}}){_NA}")
_RX_THAI_BEFORE = re.compile(rf"พ\.?\s?ศ\.?\s?(?P<y>[0-9]{{4}}){_NA}")
_RX_THAI_AFTER = re.compile(rf"{_NB}(?P<y>[0-9]{{4}})\s?พ\.?\s?ศ\.?")
_RX_SH_AFTER = re.compile(rf"{_NB}(?P<y>[0-9]{{4}})\s?(?:هـ|ه)\s?\.?\s?ش")
_RX_SH_BEFORE = re.compile(rf"(?:هـ|ه)\s?\.?\s?ش\s?(?P<y>[0-9]{{4}}){_NA}")
_RX_AH_AFTER = re.compile(rf"{_NB}(?P<y>[0-9]{{4}})\s?(?:هـ|ه)(?!\s?\.?\s?ش){_LA}")
_RX_AH_BEFORE = re.compile(rf"{_LB}(?:هـ|ه)\s?(?P<y>[0-9]{{4}}){_NA}")

_GAP = re.compile(rf"\s*+(?:(?P<sep>{_SEP})\s*+)?", _I)
_DANGLING = re.compile(rf"[^\S\r\n]*+(?P<dash>{_DASHES})(?=[^\S\r\n]*+(?:$|\r?\n|[,;|)\]•·]))")
_PREFIX = re.compile(rf"{_LB}(?P<p>since|depuis|dès|from|de|du)\s*$", _I)
_NEXT_WORD = re.compile(r"\s+([^\W\d_]+)")
_LEFT_NUMBER = re.compile(r"(?:[0-9][\s.,()/–—-]{0,2}|[$€£¥#+]\s?)$")
_RIGHT_NUMBER = re.compile(r"[\s.,()/–—-]{0,2}[0-9]|[%+]|[kKmM](?![^\W\d_])")
_EDGE = frozenset(",;:|()[]{}.")


# ─── Periods ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Period:
    """What a date expression names: a day, month, season, quarter or year (year 0 = unknown)."""

    precision: DatePrecision
    year: int
    month: int = 1
    day: int = 1
    quarter: int = 0


def _safe_date(year: int, month: int, day: int) -> Optional[dt.date]:
    try:
        return dt.date(year, month, day)
    except (ValueError, OverflowError):
        return None


def _bounds(p: _Period) -> Optional[tuple[dt.date, dt.date, dt.date]]:
    """(earliest, point estimate, latest) of a period, or None when the calendar cannot hold it."""
    if p.precision is DatePrecision.DAY:
        day = _safe_date(p.year, p.month, p.day)
        return None if day is None else (day, day, day)
    if p.precision is DatePrecision.MONTH:
        first = _safe_date(p.year, p.month, 1) if 1 <= p.month <= 12 else None
        if first is None:
            return None
        after = _safe_date(p.year + p.month // 12, p.month % 12 + 1, 1)
        last = after - dt.timedelta(days=1) if after is not None else dt.date(p.year, 12, 31)
        return first, first, last
    days = _YEAR_DAYS if p.precision is DatePrecision.YEAR else _QUARTER_DAYS.get(p.quarter)
    if days is None:
        return None
    first, middle, last = (_safe_date(p.year, m, d) for m, d in days)
    if first is None or middle is None or last is None:
        return None
    return first, middle, last


# `_date_value` and `_spanning` return None only for a period the calendar cannot hold. That is
# unreachable by construction (every period is checked by `_bounds` when it is lexed, inherited or
# read), and the callers drop the range rather than invent a date if it ever happens.


def _date_value(p: _Period, role: str) -> Optional[DateValue]:
    bounds = _bounds(p)
    if bounds is None:
        return None
    earliest, middle, latest = bounds
    value = latest if p.precision is DatePrecision.MONTH and role == "end" else middle
    return DateValue(value=value, earliest=earliest, latest=latest, precision=p.precision)


def _spanning(p: _Period) -> tuple[Optional[DateValue], Optional[DateValue]]:
    """Start and end of a role that spanned exactly one period ("Summer 2020"; brief row 4)."""
    bounds = _bounds(p)
    if bounds is None or p.precision not in _IMPRECISE:
        return _date_value(p, "start"), _date_value(p, "end")
    earliest, middle, latest = bounds
    return (
        DateValue(value=earliest, earliest=earliest, latest=middle, precision=p.precision),
        DateValue(value=latest, earliest=middle, latest=latest, precision=p.precision),
    )


def _four_digit_year(digits: str, as_of: dt.date) -> Optional[int]:
    """Years outside [as_of.year − 80, as_of.year + 10] are not dates (brief row 11)."""
    year = int(digits)
    low = max(dt.MINYEAR, as_of.year - _YEARS_BACK)
    high = min(dt.MAXYEAR, as_of.year + _YEARS_AHEAD)
    return year if low <= year <= high else None


def _two_digit_year(digits: str, as_of: dt.date) -> Optional[int]:
    """The one year in [as_of.year − 80, as_of.year + 19] ending in these two digits (row 7)."""
    base = as_of.year - _YEARS_BACK
    year = base + (int(digits) - base) % 100
    return year if dt.MINYEAR <= year <= dt.MAXYEAR else None


# ─── Lexer: date atoms ───────────────────────────────────────────────────────────────────────

_POINT, _NOYEAR, _WEAK, _PRESENT, _SPAN, _UNSUPPORTED = (
    "point", "noyear", "weak", "present", "span", "unsupported",
)  # fmt: skip


@dataclass(frozen=True)
class _Atom:
    start: int
    end: int
    kind: str  # one of the six kinds above; _WEAK = "Mon D(D)", read only beside its range partner
    period: Optional[_Period] = None
    end_period: Optional[_Period] = None  # _SPAN only ("2018/19", "2015-19")
    flags: tuple[Flag, ...] = ()
    numeric: bool = False  # digits only: searching a line checks its neighbours more strictly
    bare_year: bool = False  # a lone four-digit number
    two_digit: bool = False
    bare_two: bool = False  # a lone two-digit number (whole-text reading only)
    own_sep: bool = False  # a present marker that carries its separator ("to date")


_Handler = Callable[[re.Match[str], dt.date, str], Optional[_Atom]]


def _point(
    m: re.Match[str],
    precision: DatePrecision,
    year: Optional[int],
    month: int = 1,
    day: int = 1,
    quarter: int = 0,
    *,
    kind: str = _POINT,
    two_digit: bool = False,
    numeric: bool = False,
    bare_year: bool = False,
    bare_two: bool = False,
) -> Optional[_Atom]:
    if year is None:
        return None
    period = _Period(precision, year, month, day, quarter)
    if _bounds(period) is None:
        return None
    flags = ((Flag.IMPRECISE_DATE,) if precision in _IMPRECISE else ()) + (
        (Flag.TWO_DIGIT_YEAR,) if two_digit else ()
    )
    return _Atom(m.start(), m.end(), kind, period, None, flags, numeric, bare_year, two_digit, bare_two)


def _match_year(m: re.Match[str], as_of: dt.date) -> tuple[Optional[int], bool]:
    found = m.groupdict()
    if found.get("y"):
        return _four_digit_year(found["y"], as_of), False
    two = found.get("ya") or found.get("yn")
    return (_two_digit_year(two[-2:], as_of), True) if two else (None, False)


def _month_of(m: re.Match[str]) -> int:
    return _MONTHS.get(m["mon"].casefold().rstrip("."), 0)


def _iso_day(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year = _four_digit_year(m["y"], as_of)
    return _point(m, DatePrecision.DAY, year, int(m["m"]), int(m["d"]), numeric=True)


def _numeric_day(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    a, b = int(m["a"]), int(m["b"])
    day_first = a > 12 or (b <= 12 and lang == "fr")  # ambiguous 03/04: M/D in en, D/M in fr
    month, day = (b, a) if day_first else (a, b)
    return _point(m, DatePrecision.DAY, _four_digit_year(m["y"], as_of), month, day, numeric=True)


def _named_day(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    return _point(m, DatePrecision.DAY, _four_digit_year(m["y"], as_of), _month_of(m), int(m["d"]))


def _named_month(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year, two = _match_year(m, as_of)
    return _point(m, DatePrecision.MONTH, year, _month_of(m), two_digit=two)


def _weak_month(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    """A day with no year yet (year 0); `_read_weak` gives it its range partner's year."""
    month = _month_of(m)
    if not month:
        return None
    return _Atom(m.start(), m.end(), _WEAK, _Period(DatePrecision.DAY, 0, month, int(m["d"])))


def _season(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year, two = _match_year(m, as_of)
    quarter = _SEASONS.get(m["sea"].casefold(), 0)
    return _point(m, DatePrecision.SEASON, year, quarter=quarter, two_digit=two)


def _quarter(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year, two = _match_year(m, as_of)
    return _point(m, DatePrecision.QUARTER, year, quarter=int(m["q"]), two_digit=two)


def _numeric_month(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year = _four_digit_year(m["y"], as_of)
    return _point(m, DatePrecision.MONTH, year, int(m["m"]), numeric=True)


def _year(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year = _four_digit_year(m["y"], as_of)
    return _point(m, DatePrecision.YEAR, year, numeric=True, bare_year=True)


def _apostrophe_year(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year = _two_digit_year(m["yy"], as_of)
    return _point(m, DatePrecision.YEAR, year, two_digit=True, numeric=True)


def _bare_two(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year = _two_digit_year(m["yy"], as_of)
    return _point(m, DatePrecision.YEAR, year, two_digit=True, numeric=True, bare_two=True)


def _month_only(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    month = _month_of(m)
    return _Atom(m.start(), m.end(), _NOYEAR, _Period(DatePrecision.MONTH, 0, month)) if month else None


def _season_only(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    quarter = _SEASONS.get(m["sea"].casefold(), 0)
    if not quarter:
        return None
    period = _Period(DatePrecision.SEASON, 0, quarter=quarter)
    return _Atom(m.start(), m.end(), _NOYEAR, period, flags=(Flag.IMPRECISE_DATE,))


def _present(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    words = m["p"].casefold().split()
    own_sep = m["sep"] is not None or (len(words) > 1 and words[0] in ("to", "till", "à", "a"))
    return _Atom(m.start(), m.end(), _PRESENT, own_sep=own_sep)


def _academic(m: re.Match[str], year: int) -> Optional[_Atom]:
    first = _Period(DatePrecision.MONTH, year, _ACADEMIC_FIRST_MONTH)
    last = _Period(DatePrecision.MONTH, year + 1, _ACADEMIC_LAST_MONTH)
    if _bounds(first) is None or _bounds(last) is None:
        return None
    return _Atom(m.start(), m.end(), _SPAN, first, last, numeric=True)


def _year_span(m: re.Match[str], year: int, end: int) -> Optional[_Atom]:
    if not year < end <= min(year + _MAX_RANGE_YEARS, dt.MAXYEAR):
        return None
    first, last = _Period(DatePrecision.YEAR, year), _Period(DatePrecision.YEAR, end)
    return _Atom(m.start(), m.end(), _SPAN, first, last, (Flag.IMPRECISE_DATE,), numeric=True)


def _short_end(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year, short = _four_digit_year(m["y"], as_of), int(m["yy"])
    if year is None:
        return None
    if short == (year + 1) % 100:
        return _academic(m, year)
    end = year - year % 100 + short
    return _year_span(m, year, end if end > year else end + 100)


def _year_slash(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
    year, end = _four_digit_year(m["y"], as_of), _four_digit_year(m["y2"], as_of)
    if year is None or end is None:
        return None
    return _academic(m, year) if end == year + 1 else _year_span(m, year, end)


def _calendar(marker: Optional[str]) -> _Handler:
    def handle(m: re.Match[str], as_of: dt.date, lang: str) -> Optional[_Atom]:
        calendar = _CALENDARS.get(marker or m["mk"].replace(".", ""))
        raw = int(m["y"])
        if calendar is None or not calendar[0][0] <= raw <= calendar[0][1]:
            return None
        year = calendar[1](raw)
        if _EARLIEST_CONVERTED <= year <= min(as_of.year + 1, dt.MAXYEAR):
            period = _Period(DatePrecision.YEAR, year)
            flags = (Flag.IMPRECISE_DATE, Flag.NON_GREGORIAN_CONVERTED)
            return _Atom(m.start(), m.end(), _POINT, period, flags=flags)
        return _Atom(m.start(), m.end(), _UNSUPPORTED, flags=(Flag.UNSUPPORTED_CALENDAR,))

    return handle


# (pattern, handler, whole-text reading only). On the same start and length, earlier wins: so
# "2011-12" is the ISO month December 2011, never the academic year it could also spell.
_LEXICON: tuple[tuple[re.Pattern[str], _Handler, bool], ...] = (
    (_RX_CAL_AFTER, _calendar(None), False),
    (_RX_CAL_BEFORE, _calendar(None), False),
    (_RX_THAI_BEFORE, _calendar("BE"), False),
    (_RX_THAI_AFTER, _calendar("BE"), False),
    (_RX_SH_AFTER, _calendar("SH"), False),
    (_RX_SH_BEFORE, _calendar("SH"), False),
    (_RX_AH_AFTER, _calendar("AH"), False),
    (_RX_AH_BEFORE, _calendar("AH"), False),
    (_RX_ISO_DAY, _iso_day, False),
    (_RX_NUMERIC_DAY, _numeric_day, False),
    (_RX_MONTH_DAY, _named_day, False),
    (_RX_DAY_MONTH, _named_day, False),
    (_RX_NAMED_MONTH, _named_month, False),
    (_RX_SEASON, _season, False),
    (_RX_QUARTER_FIRST, _quarter, False),
    (_RX_QUARTER_LAST, _quarter, False),
    (_RX_ISO_MONTH, _numeric_month, False),
    (_RX_NUMERIC_MONTH, _numeric_month, False),
    (_RX_SHORT_END, _short_end, False),
    (_RX_YEAR_SLASH, _year_slash, False),
    (_RX_WEAK_MONTH, _weak_month, False),
    (_RX_YEAR, _year, False),
    (_RX_APOSTROPHE_YEAR, _apostrophe_year, False),
    (_RX_BARE_TWO, _bare_two, True),
    (_RX_PRESENT, _present, False),
    (_RX_MONTH_ONLY, _month_only, False),
    (_RX_SEASON_ONLY, _season_only, False),
)


def _lex(text: str, as_of: dt.date, lang: str, whole: bool) -> list[_Atom]:
    """Every atom in `text`, leftmost first and longest first; overlapping candidates lose."""
    candidates: list[tuple[int, int, int, _Atom]] = []
    for rank, (pattern, handler, whole_only) in enumerate(_LEXICON):
        if whole_only and not whole:
            continue
        for m in pattern.finditer(text):
            atom = handler(m, as_of, lang)
            if atom is not None:
                candidates.append((atom.start, atom.start - atom.end, rank, atom))
    candidates.sort(key=lambda c: c[:3])
    atoms: list[_Atom] = []
    reach = 0
    for start, _, _, atom in candidates:
        if start >= reach:
            atoms.append(atom)
            reach = atom.end
    return atoms


# ─── Assembly: atoms → ranges ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Group:
    start: int
    end: int
    atoms: tuple[_Atom, ...]
    first: Optional[_Period] = None
    last: Optional[_Period] = None  # None: single period, current, or unsupported
    current: bool = False
    single: bool = False  # one period written alone: the role spanned it (brief row 4)
    unsupported: bool = False
    flags: tuple[Flag, ...] = ()
    prefixed: bool = False  # led by "since", "from", "de", ...


def _month_key(p: _Period) -> int:
    """Month a period starts in, for year inheritance; 0 for a bare year (no order)."""
    if p.precision in (DatePrecision.SEASON, DatePrecision.QUARTER):
        return 3 * p.quarter - 2
    return 0 if p.precision is DatePrecision.YEAR else p.month


def _inherit(p: _Period, other: _Period, *, before: bool) -> Optional[_Period]:
    """Give a year-less month or season the other side's year ("May–Aug 2020", "Nov – Feb 2020")."""
    year, mine, theirs = other.year, _month_key(p), _month_key(other)
    if theirs and before and mine > theirs:
        year -= 1
    elif theirs and not before and mine < theirs:
        year += 1
    if not dt.MINYEAR <= year <= dt.MAXYEAR:
        return None
    filled = replace(p, year=year)
    return filled if _bounds(filled) is not None else None


def _linked(text: str, a: _Atom, b: _Atom) -> bool:
    """Whether `a` and `b` are the two ends of one range."""
    gap = _GAP.fullmatch(text, a.end, b.start)
    if gap is None:
        return False
    if b.kind == _PRESENT:  # never a "Mon D(D)" start: "May 15 – Present" is not "since 2015"
        return a.kind in (_POINT, _UNSUPPORTED) and (gap["sep"] is not None or b.own_sep)
    if gap["sep"] is None or b.kind not in (_POINT, _WEAK, _NOYEAR, _UNSUPPORTED):
        return False
    kinds = (a.kind, b.kind)
    if _NOYEAR in kinds and (a.kind == b.kind or _UNSUPPORTED in kinds or _WEAK in kinds):
        return False
    if _WEAK in kinds and _UNSUPPORTED in kinds:
        return False
    # A lone two-digit number is a year only beside another two-digit year ("98 - 02").
    return not ((a.bare_two and not b.two_digit) or (b.bare_two and not a.two_digit))


def _read_weak(weak: _Atom, partner: _Atom, *, before: bool) -> Optional[_Period]:
    """Read "Mon D(D)" from the other end of its range, or None: it is no date.

    Beside a four-digit year it is a day in that year, by `_inherit`'s month-order rule
    ("June 15 – August 15, 2020"; "Dec 15 – Jan 2021" starts in 2020). Beside anything else
    (another "Mon D(D)", an apostrophe year, Present) it is nothing: written without an
    apostrophe it is never a two-digit year, since "June 15 – August 30" read as 2015 – 2030
    would invent years of experience.
    """
    if weak.period is None or partner.kind != _POINT or partner.period is None or partner.two_digit:
        return None
    return _inherit(weak.period, partner.period, before=before)


def _pair(a: _Atom, b: _Atom) -> Optional[_Group]:
    if _UNSUPPORTED in (a.kind, b.kind):
        return _Group(a.start, b.end, (a, b), unsupported=True, flags=(Flag.UNSUPPORTED_CALENDAR,))
    if a.period is None:
        return None
    if b.kind == _PRESENT:
        return _Group(a.start, b.end, (a, b), a.period, current=True, flags=a.flags)
    first, last, flags = a.period, b.period, a.flags + b.flags
    if last is None:
        return None
    if a.kind == _WEAK:
        first = _read_weak(a, b, before=True)
    if b.kind == _WEAK:
        last = _read_weak(b, a, before=False)
    if a.kind == _NOYEAR:
        first = _inherit(first, last, before=True)
    elif b.kind == _NOYEAR:
        last = _inherit(last, first, before=False)
    if first is None or last is None:
        return None
    if first == last and first.precision in _IMPRECISE:  # "2020 – 2020" is the one period
        return _Group(a.start, b.end, (a, b), first, single=True, flags=flags)
    return _Group(a.start, b.end, (a, b), first, last, flags=flags)


def _alone(text: str, atom: _Atom, limit: int) -> _Group:
    if atom.kind == _UNSUPPORTED:
        return _Group(atom.start, atom.end, (atom,), unsupported=True, flags=atom.flags)
    dangling = _DANGLING.match(text, atom.end)
    if dangling is not None and dangling.end() <= limit:  # "Jan 2020 –": still going
        return _Group(atom.start, dangling.end(), (atom,), atom.period, current=True, flags=atom.flags)
    return _Group(atom.start, atom.end, (atom,), atom.period, single=True, flags=atom.flags)


def _with_prefix(text: str, group: _Group, floor: int) -> _Group:
    found = _PREFIX.search(text, max(floor, group.start - 16), group.start)
    if found is None or group.unsupported:
        return group
    if found["p"].casefold() in ("since", "depuis", "dès"):
        if group.single:
            return replace(group, start=found.start(), current=True, single=False, prefixed=True)
        return replace(group, start=found.start(), prefixed=True)
    if group.single:  # "from 2019" alone says nothing more than "2019"
        return group
    return replace(group, start=found.start(), prefixed=True)


def _assemble(text: str, atoms: list[_Atom]) -> list[_Group]:
    groups: list[_Group] = []
    i = 0
    while i < len(atoms):
        atom = atoms[i]
        following = atoms[i + 1] if i + 1 < len(atoms) else None
        group: Optional[_Group] = None
        if atom.kind == _SPAN:
            group = _Group(atom.start, atom.end, (atom,), atom.period, atom.end_period, flags=atom.flags)
        elif following is not None and atom.kind != _PRESENT and _linked(text, atom, following):
            group = _pair(atom, following)
            if group is not None:
                i += 1
        if group is None and atom.kind in (_POINT, _UNSUPPORTED):
            group = _alone(text, atom, following.start if following is not None else len(text))
        if group is not None:
            groups.append(_with_prefix(text, group, groups[-1].end if groups else 0))
        i += 1
    return groups


def _credible(text: str, group: _Group, floor: int, ceiling: int) -> bool:
    """When searching a line, reject digits that are not dates ($2020, 2020 users, 613-555-2020).

    Words (a month, a season, "since", "Present") are evidence enough; digits alone must not
    touch other numbers, currency or percent signs, and a lone four-digit year must not be
    followed by a lowercase word longer than a preposition ("2020 users", but "2020 at Acme").
    """
    if group.prefixed or any(not atom.numeric for atom in group.atoms):
        return True
    left = text[max(floor, group.start - 3) : group.start]
    right = text[group.end : min(ceiling, group.end + 3)]
    if _LEFT_NUMBER.search(left) or _RIGHT_NUMBER.match(right):
        return False
    if group.single and group.atoms[0].bare_year:
        word = _NEXT_WORD.match(text, group.end)
        if word is not None and word[1][:1].islower() and len(word[1]) > 3:
            return False
    return True


# ─── Settling a range against as_of ──────────────────────────────────────────────────────────


def _ordered(flags: Iterable[Flag]) -> tuple[Flag, ...]:
    return tuple(sorted(set(flags), key=lambda f: f.value))


def _dropped(reason: Flag, raw: str) -> DateRange:
    return DateRange(start=None, end=None, is_current=False, flags=(reason,), raw=raw)


def _shift_years(day: dt.date, years: int) -> dt.date:
    year = day.year + years
    if year < dt.MINYEAR:
        return dt.date.min
    if year > dt.MAXYEAR:
        return dt.date.max
    return _safe_date(year, day.month, day.day) or dt.date(year, day.month, 28)


def _revalue(v: DateValue, value: dt.date) -> DateValue:
    return DateValue(value=value, earliest=v.earliest, latest=v.latest, precision=v.precision)


def _cap(v: DateValue, as_of: dt.date) -> DateValue:
    """No bound after as_of: nothing in the past can have happened later than today."""
    if v.latest <= as_of:
        return v
    return DateValue(value=v.value, earliest=v.earliest, latest=as_of, precision=v.precision)


def _settle(group: _Group, as_of: dt.date, raw: str) -> DateRange:
    if group.unsupported or group.first is None:
        return _dropped(Flag.UNSUPPORTED_CALENDAR, raw)
    flags = set(group.flags)
    if group.single:
        start, end = _spanning(group.first)
    else:
        start = _date_value(group.first, "start")
        end = None if group.last is None else _date_value(group.last, "end")
    if start is None or (end is None and (group.single or group.last is not None)):
        return _dropped(Flag.DATE_RANGE_DROPPED, raw)  # an unholdable period: see _date_value
    if end is not None and group.last is not None and end.value < start.value:
        if start.earliest <= end.latest and end.earliest <= start.latest:
            # Imprecise ends that overlap ("Summer 2020 – 2020"): nothing was written backwards;
            # the role lies in the intersection of the two periods.
            start, end = (
                _revalue(start, max(start.earliest, end.earliest)),
                _revalue(end, min(end.latest, start.latest)),
            )
        else:
            start, end = _date_value(group.last, "start"), _date_value(group.first, "end")
            if start is None or end is None:
                return _dropped(Flag.DATE_RANGE_DROPPED, raw)  # an unholdable period: see _date_value
            too_old = start.value < _shift_years(as_of, -_YEARS_BACK)
            if too_old or end.value > _shift_years(start.value, _MAX_RANGE_YEARS):
                return _dropped(Flag.DATE_RANGE_DROPPED, raw)
            flags.add(Flag.DATE_RANGE_SWAPPED)
    if start.value > as_of:
        if start.earliest > as_of:
            return _dropped(Flag.FUTURE_START, raw)  # the caller drops the role
        # The start's period contains as_of ("Fall 2026" in October 2026): it has begun.
        middle = dt.date.fromordinal((start.earliest.toordinal() + as_of.toordinal()) // 2)
        start = DateValue(value=middle, earliest=start.earliest, latest=as_of, precision=start.precision)
    if end is not None and end.value > as_of:
        end = DateValue.exact(as_of, DatePrecision.DAY)
        flags.add(Flag.FUTURE_END_CLAMPED)
    return DateRange(
        start=_cap(start, as_of),
        end=None if end is None else _cap(end, as_of),
        is_current=group.current,
        flags=_ordered(flags),
        raw=raw,
    )


# ─── Public API ───────────────────────────────────────────────────────────────────────────────


def _covers(text: str, start: int, end: int) -> bool:
    """Whether [start, end) is all of `text` but surrounding blanks and punctuation."""
    outside = text[:start] + text[end:]
    return all(c.isspace() or c in _EDGE for c in outside)


def parse_date(
    text: str,
    *,
    as_of: dt.date,
    role: Literal["start", "end", "point"] = "point",
    lang: Literal["en", "fr"] = "en",
) -> tuple[Optional[DateValue], tuple[Flag, ...]]:
    """One date filling the whole of `text`, or (None, flags). A month is its first day as a
    start or point and its last day as an end; a season, quarter or year is its midpoint."""
    norm = text.translate(_DIGITS)
    atoms = _lex(norm, as_of, lang, whole=True)
    if len(atoms) != 1 or not _covers(norm, atoms[0].start, atoms[0].end):
        return None, ()
    atom = atoms[0]
    if atom.kind == _PRESENT:
        return DateValue.exact(as_of, DatePrecision.DAY), ()
    if atom.kind == _UNSUPPORTED:
        return None, (Flag.UNSUPPORTED_CALENDAR,)
    value = _date_value(atom.period, role) if atom.kind == _POINT and atom.period is not None else None
    return (value, _ordered(atom.flags)) if value is not None else (None, ())


def find_date_ranges(
    text: str, *, as_of: dt.date, lang: Literal["en", "fr"] = "en"
) -> tuple[tuple[int, int, DateRange], ...]:
    """Every date range inside `text` as (start offset, end offset, range): sorted, never
    overlapping, `text[start:end] == range.raw`. Dropped ranges are included (their flags say
    why) so the caller can still cut them out of the line."""
    norm = text.translate(_DIGITS)
    groups = _assemble(norm, _lex(norm, as_of, lang, whole=False))
    found: list[tuple[int, int, DateRange]] = []
    for k, group in enumerate(groups):
        floor = groups[k - 1].end if k else 0
        ceiling = groups[k + 1].start if k + 1 < len(groups) else len(norm)
        if _credible(norm, group, floor, ceiling):
            found.append((group.start, group.end, _settle(group, as_of, text[group.start : group.end])))
    return tuple(found)


def parse_range(text: str, *, as_of: dt.date, lang: Literal["en", "fr"] = "en") -> Optional[DateRange]:
    """The range `text` states: all of it when it is one range (a bare two-digit year allowed),
    else the first range found inside it; None when there is none."""
    norm = text.translate(_DIGITS)
    groups = _assemble(norm, _lex(norm, as_of, lang, whole=True))
    if len(groups) == 1 and _covers(norm, groups[0].start, groups[0].end):
        return _settle(groups[0], as_of, text[groups[0].start : groups[0].end])
    found = find_date_ranges(text, as_of=as_of, lang=lang)
    return found[0][2] if found else None
