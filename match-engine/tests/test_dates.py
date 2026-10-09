"""SPEC §14.2 dates (Task 6.1): one parametrised case per brief row, plus Hypothesis totality.

Every expected value is a hand-derived literal. AS_OF is a parameter; nothing reads the clock.
Quarter midpoints: Q1 Feb 15, Q2 May 16, Q3 Aug 16, Q4 Nov 16 (bare year: Jul 1).
"""

from __future__ import annotations

import datetime as dt
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from matcher.errors import FLAG_TEXT, Flag
from matcher.models import DatePrecision, DateValue
from matcher.parse.dates import DateRange, find_date_ranges, months_between, parse_date, parse_range

D = dt.date
AS_OF = D(2026, 10, 8)
DAY = DatePrecision.DAY
MONTH = DatePrecision.MONTH
SEASON = DatePrecision.SEASON
QUARTER = DatePrecision.QUARTER
YEAR = DatePrecision.YEAR
IMPRECISE = Flag.IMPRECISE_DATE
TWO_DIGIT = Flag.TWO_DIGIT_YEAR
CONVERTED = Flag.NON_GREGORIAN_CONVERTED


def dv(value: dt.date, earliest: dt.date, latest: dt.date, precision: DatePrecision) -> DateValue:
    return DateValue(value=value, earliest=earliest, latest=latest, precision=precision)


JAN_2020 = dv(D(2020, 1, 1), D(2020, 1, 1), D(2020, 1, 31), MONTH)
MAR_15_2020 = dv(D(2020, 3, 15), D(2020, 3, 15), D(2020, 3, 15), DAY)
YEAR_2020 = dv(D(2020, 7, 1), D(2020, 1, 1), D(2020, 12, 31), YEAR)
FEB_2021 = dv(D(2021, 2, 1), D(2021, 2, 1), D(2021, 2, 28), MONTH)
JAN_2020_START = JAN_2020
MAR_2021_END = dv(D(2021, 3, 31), D(2021, 3, 1), D(2021, 3, 31), MONTH)


# ─── Row 1: point formats ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected, flags",
    [
        ("Jan 2020", JAN_2020, ()),
        ("January 2020", JAN_2020, ()),
        ("JANUARY 2020", JAN_2020, ()),
        ("Jan. 2020", JAN_2020, ()),
        ("  Jan 2020  ", JAN_2020, ()),
        ("(Jan 2020).", JAN_2020, ()),
        ("Sept 2020", dv(D(2020, 9, 1), D(2020, 9, 1), D(2020, 9, 30), MONTH), ()),
        ("01/2020", JAN_2020, ()),
        ("1/2020", JAN_2020, ()),
        ("2020-01", JAN_2020, ()),
        ("2020/01", JAN_2020, ()),
        ("01.2020", JAN_2020, ()),
        # A valid month wins over the academic year it could also spell (2011-12 / 2011/12).
        ("2011-12", dv(D(2011, 12, 1), D(2011, 12, 1), D(2011, 12, 31), MONTH), ()),
        ("2011/12", dv(D(2011, 12, 1), D(2011, 12, 1), D(2011, 12, 31), MONTH), ()),
        ("2020", YEAR_2020, (IMPRECISE,)),
        ("Jan '20", JAN_2020, (TWO_DIGIT,)),
        ("Jan ’20", JAN_2020, (TWO_DIGIT,)),
        ("'19", dv(D(2019, 7, 1), D(2019, 1, 1), D(2019, 12, 31), YEAR), (IMPRECISE, TWO_DIGIT)),
        ("2020-03-15", MAR_15_2020, ()),
        ("03/15/2020", MAR_15_2020, ()),
        ("15/03/2020", MAR_15_2020, ()),
        ("March 15, 2020", MAR_15_2020, ()),
        ("March 15th, 2020", MAR_15_2020, ()),
        ("15 March 2020", MAR_15_2020, ()),
    ],
)
def test_point_formats_en(text: str, expected: DateValue, flags: tuple[Flag, ...]) -> None:
    assert parse_date(text, as_of=AS_OF) == (expected, flags)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("janvier 2020", JAN_2020),
        ("févr. 2021", FEB_2021),
        ("fevrier 2021", FEB_2021),
        ("août 2019", dv(D(2019, 8, 1), D(2019, 8, 1), D(2019, 8, 31), MONTH)),
        ("déc. 2018", dv(D(2018, 12, 1), D(2018, 12, 1), D(2018, 12, 31), MONTH)),
        ("juil. 2022", dv(D(2022, 7, 1), D(2022, 7, 1), D(2022, 7, 31), MONTH)),
        ("15 mars 2020", MAR_15_2020),
        ("1er mars 2020", dv(D(2020, 3, 1), D(2020, 3, 1), D(2020, 3, 1), DAY)),
    ],
)
@pytest.mark.parametrize("lang", ["fr", "en"])  # mixed-language résumés (SPEC §14.1)
def test_point_formats_fr(text: str, expected: DateValue, lang: str) -> None:
    assert parse_date(text, as_of=AS_OF, lang=lang) == (expected, ())


@pytest.mark.parametrize(
    "text, lang, day",
    [
        ("03/04/2020", "en", D(2020, 3, 4)),  # ambiguous: M/D/Y in en
        ("03/04/2020", "fr", D(2020, 4, 3)),  # ambiguous: D/M/Y in fr
        ("15/03/2020", "en", D(2020, 3, 15)),  # day > 12 forces D/M/Y in any language
        ("03/15/2020", "fr", D(2020, 3, 15)),  # day > 12 forces M/D/Y in any language
        ("15.03.2020", "en", D(2020, 3, 15)),
    ],
)
def test_numeric_day_order(text: str, lang: str, day: dt.date) -> None:
    assert parse_date(text, as_of=AS_OF, lang=lang) == (dv(day, day, day, DAY), ())


@pytest.mark.parametrize("text", ["13/13/2020", "02/30/2020", "2020-02-30", "2020-13", "Febtember 2020"])
def test_impossible_calendar_values_are_not_dates(text: str) -> None:
    assert parse_date(text, as_of=AS_OF) == (None, ())


# ─── Row 2: month and day precision vs role ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role, value", [("start", D(2021, 3, 1)), ("end", D(2021, 3, 31)), ("point", D(2021, 3, 1))]
)
def test_month_value_depends_on_role(role: str, value: dt.date) -> None:
    got, _ = parse_date("Mar 2021", as_of=AS_OF, role=role)
    assert got == dv(value, D(2021, 3, 1), D(2021, 3, 31), MONTH)


def test_month_end_respects_leap_february() -> None:
    got, _ = parse_date("Feb 2020", as_of=AS_OF, role="end")
    assert got == dv(D(2020, 2, 29), D(2020, 2, 1), D(2020, 2, 29), MONTH)


@pytest.mark.parametrize("role", ["start", "end", "point"])
def test_day_precision_ignores_role(role: str) -> None:
    assert parse_date("2020-03-15", as_of=AS_OF, role=role) == (MAR_15_2020, ())


# ─── Row 3: seasons, quarters, bare years ────────────────────────────────────────────────────

SUMMER_2020 = dv(D(2020, 8, 16), D(2020, 7, 1), D(2020, 9, 30), SEASON)
FALL_2019 = dv(D(2019, 11, 16), D(2019, 10, 1), D(2019, 12, 31), SEASON)
Q3_2021 = dv(D(2021, 8, 16), D(2021, 7, 1), D(2021, 9, 30), QUARTER)


@pytest.mark.parametrize(
    "text, expected, flags",
    [
        ("Winter 2021", dv(D(2021, 2, 15), D(2021, 1, 1), D(2021, 3, 31), SEASON), (IMPRECISE,)),
        ("Spring 2021", dv(D(2021, 5, 16), D(2021, 4, 1), D(2021, 6, 30), SEASON), (IMPRECISE,)),
        ("Summer 2020", SUMMER_2020, (IMPRECISE,)),
        ("Fall 2019", FALL_2019, (IMPRECISE,)),
        ("Autumn 2019", FALL_2019, (IMPRECISE,)),
        ("Fall '19", FALL_2019, (IMPRECISE, TWO_DIGIT)),
        ("Q3 2021", Q3_2021, (IMPRECISE,)),
        ("2021 Q3", Q3_2021, (IMPRECISE,)),
        ("Q1 2022", dv(D(2022, 2, 15), D(2022, 1, 1), D(2022, 3, 31), QUARTER), (IMPRECISE,)),
        ("Q4 2019", dv(D(2019, 11, 16), D(2019, 10, 1), D(2019, 12, 31), QUARTER), (IMPRECISE,)),
        ("hiver 2021", dv(D(2021, 2, 15), D(2021, 1, 1), D(2021, 3, 31), SEASON), (IMPRECISE,)),
        ("printemps 2021", dv(D(2021, 5, 16), D(2021, 4, 1), D(2021, 6, 30), SEASON), (IMPRECISE,)),
        ("été 2020", SUMMER_2020, (IMPRECISE,)),
        ("ete 2020", SUMMER_2020, (IMPRECISE,)),
        ("automne 2019", FALL_2019, (IMPRECISE,)),
    ],
)
def test_seasons_and_quarters_map_to_quarter_midpoint(
    text: str, expected: DateValue, flags: tuple[Flag, ...]
) -> None:
    assert parse_date(text, as_of=AS_OF) == (expected, flags)


@pytest.mark.parametrize("role", ["start", "end", "point"])
@pytest.mark.parametrize("text, expected", [("Summer 2020", SUMMER_2020), ("2020", YEAR_2020)])
def test_imprecise_value_is_the_midpoint_for_every_role(role: str, text: str, expected: DateValue) -> None:
    assert parse_date(text, as_of=AS_OF, role=role) == (expected, (IMPRECISE,))


# ─── Row 4: a range written as one period spans it ───────────────────────────────────────────


@pytest.mark.parametrize(
    "text, start, end, flags",
    [
        (
            "Summer 2020",
            dv(D(2020, 7, 1), D(2020, 7, 1), D(2020, 8, 16), SEASON),
            dv(D(2020, 9, 30), D(2020, 8, 16), D(2020, 9, 30), SEASON),
            (IMPRECISE,),
        ),
        (
            "2020",
            dv(D(2020, 1, 1), D(2020, 1, 1), D(2020, 7, 1), YEAR),
            dv(D(2020, 12, 31), D(2020, 7, 1), D(2020, 12, 31), YEAR),
            (IMPRECISE,),
        ),
        (
            "Q3 2021",
            dv(D(2021, 7, 1), D(2021, 7, 1), D(2021, 8, 16), QUARTER),
            dv(D(2021, 9, 30), D(2021, 8, 16), D(2021, 9, 30), QUARTER),
            (IMPRECISE,),
        ),
        (  # the same period on both sides reads as that one period
            "2020 – 2020",
            dv(D(2020, 1, 1), D(2020, 1, 1), D(2020, 7, 1), YEAR),
            dv(D(2020, 12, 31), D(2020, 7, 1), D(2020, 12, 31), YEAR),
            (IMPRECISE,),
        ),
        (  # one month: first day to last day, never imprecise
            "Mar 2021",
            dv(D(2021, 3, 1), D(2021, 3, 1), D(2021, 3, 31), MONTH),
            MAR_2021_END,
            (),
        ),
    ],
)
def test_single_period_spans_the_period(
    text: str, start: DateValue, end: DateValue, flags: tuple[Flag, ...]
) -> None:
    assert parse_range(text, as_of=AS_OF) == DateRange(
        start=start, end=end, is_current=False, flags=flags, raw=text
    )


# ─── Row 5: ranges ────────────────────────────────────────────────────────────────────────────

Y2019 = dv(D(2019, 7, 1), D(2019, 1, 1), D(2019, 12, 31), YEAR)
Y2021 = dv(D(2021, 7, 1), D(2021, 1, 1), D(2021, 12, 31), YEAR)


@pytest.mark.parametrize(
    "text, start, end, flags",
    [
        ("Jan 2020 – Mar 2021", JAN_2020_START, MAR_2021_END, ()),
        ("Jan 2020–Mar 2021", JAN_2020_START, MAR_2021_END, ()),
        ("de janvier 2020 à mars 2021", JAN_2020_START, MAR_2021_END, ()),
        ("du janvier 2020 au mars 2021", JAN_2020_START, MAR_2021_END, ()),
        ("from Jan 2020 to Mar 2021", JAN_2020_START, MAR_2021_END, ()),
        ("2019-2021", Y2019, Y2021, (IMPRECISE,)),
        ("2019 to 2021", Y2019, Y2021, (IMPRECISE,)),
        (  # start month inherits the end's year
            "May–Aug 2020",
            dv(D(2020, 5, 1), D(2020, 5, 1), D(2020, 5, 31), MONTH),
            dv(D(2020, 8, 31), D(2020, 8, 1), D(2020, 8, 31), MONTH),
            (),
        ),
        (  # ... from the previous year when its month comes later than the end's
            "Nov – Feb 2020",
            dv(D(2019, 11, 1), D(2019, 11, 1), D(2019, 11, 30), MONTH),
            dv(D(2020, 2, 29), D(2020, 2, 1), D(2020, 2, 29), MONTH),
            (),
        ),
        (
            "Sept 2019 - Apr. 2020",
            dv(D(2019, 9, 1), D(2019, 9, 1), D(2019, 9, 30), MONTH),
            dv(D(2020, 4, 30), D(2020, 4, 1), D(2020, 4, 30), MONTH),
            (),
        ),
        (  # academic year: September to April, month precision
            "2018/19",
            dv(D(2018, 9, 1), D(2018, 9, 1), D(2018, 9, 30), MONTH),
            dv(D(2019, 4, 30), D(2019, 4, 1), D(2019, 4, 30), MONTH),
            (),
        ),
        (
            "2018-19",
            dv(D(2018, 9, 1), D(2018, 9, 1), D(2018, 9, 30), MONTH),
            dv(D(2019, 4, 30), D(2019, 4, 1), D(2019, 4, 30), MONTH),
            (),
        ),
        (
            "2018/2019",
            dv(D(2018, 9, 1), D(2018, 9, 1), D(2018, 9, 30), MONTH),
            dv(D(2019, 4, 30), D(2019, 4, 1), D(2019, 4, 30), MONTH),
            (),
        ),
        (  # an end month without a year takes the start's, or the next year's when earlier
            "Nov 2019 – Feb",
            dv(D(2019, 11, 1), D(2019, 11, 1), D(2019, 11, 30), MONTH),
            dv(D(2020, 2, 29), D(2020, 2, 1), D(2020, 2, 29), MONTH),
            (),
        ),
        (  # a short end year that is not the next year is a plain year range
            "2015-19",
            dv(D(2015, 7, 1), D(2015, 1, 1), D(2015, 12, 31), YEAR),
            Y2019,
            (IMPRECISE,),
        ),
        ("Jan '20 – Mar '21", JAN_2020_START, MAR_2021_END, (TWO_DIGIT,)),
        (
            "Summer 2019 – Fall 2020",
            dv(D(2019, 8, 16), D(2019, 7, 1), D(2019, 9, 30), SEASON),
            dv(D(2020, 11, 16), D(2020, 10, 1), D(2020, 12, 31), SEASON),
            (IMPRECISE,),
        ),
    ],
)
def test_ranges(text: str, start: DateValue, end: DateValue, flags: tuple[Flag, ...]) -> None:
    assert parse_range(text, as_of=AS_OF) == DateRange(
        start=start, end=end, is_current=False, flags=flags, raw=text
    )


@pytest.mark.parametrize(
    "sep", ["–", "—", "-", "to", "through", "until", "till", "à", "au", "jusqu'à", "jusqu’à", "TO"]
)
def test_every_range_separator(sep: str) -> None:
    got = parse_range(f"Jan 2020 {sep} Mar 2021", as_of=AS_OF)
    assert got is not None and (got.start, got.end) == (JAN_2020_START, MAR_2021_END)


@pytest.mark.parametrize(
    "text, start, flags",
    [
        ("Since 2019", Y2019, (IMPRECISE,)),
        ("Depuis 2019", Y2019, (IMPRECISE,)),
        ("since May 2019", dv(D(2019, 5, 1), D(2019, 5, 1), D(2019, 5, 31), MONTH), ()),
        ("depuis janvier 2020", JAN_2020_START, ()),
    ],
)
def test_since_means_current(text: str, start: DateValue, flags: tuple[Flag, ...]) -> None:
    assert parse_range(text, as_of=AS_OF) == DateRange(
        start=start, end=None, is_current=True, flags=flags, raw=text
    )


def test_a_lone_month_with_two_digits_is_a_day_not_a_year() -> None:
    # "May 15" could be the 15th of May; a wrong year would invent experience, so it is not a date.
    assert parse_range("May 15", as_of=AS_OF) is None
    assert parse_date("May 15", as_of=AS_OF) == (None, ())


# ─── Row 6: present markers ───────────────────────────────────────────────────────────────────

PRESENT_MARKERS = [
    "Present", "present", "PRESENT", "Current", "Currently", "Now", "Ongoing", "To date", "to date",
    "Today", "Présent", "Aujourd'hui", "Aujourd’hui", "À ce jour", "A ce jour", "En cours", "Actuel",
    "Maintenant",
]


@pytest.mark.parametrize("marker", PRESENT_MARKERS)
def test_present_markers_resolve_to_current(marker: str) -> None:
    text = f"Jan 2020 – {marker}"
    assert parse_range(text, as_of=AS_OF) == DateRange(
        start=JAN_2020_START, end=None, is_current=True, flags=(), raw=text
    )


@pytest.mark.parametrize(
    "text",
    [
        "Jan 2020 to date",
        "Jan 2020 till date",
        "janvier 2020 à aujourd'hui",
        "janvier 2020 jusqu'à ce jour",
        "Jan 2020 –",
        "Jan 2020 -",
        "Jan 2020 —  ",
    ],
)
def test_present_without_dash_and_dangling_dash(text: str) -> None:
    got = parse_range(text, as_of=AS_OF)
    assert got is not None
    assert (got.start, got.end, got.is_current) == (JAN_2020_START, None, True)


def test_dangling_em_dash_after_a_year() -> None:
    got = parse_range("2019 —", as_of=AS_OF)
    assert got == DateRange(start=Y2019, end=None, is_current=True, flags=(IMPRECISE,), raw="2019 —")


@pytest.mark.parametrize("marker", ["Present", "Aujourd'hui", "now"])
def test_parse_date_resolves_present_to_as_of(marker: str) -> None:
    assert parse_date(marker, as_of=AS_OF, role="end") == (dv(AS_OF, AS_OF, AS_OF, DAY), ())


def test_present_alone_is_not_a_range() -> None:
    assert parse_range("Present", as_of=AS_OF) is None


# ─── Row 7: two-digit years pivot at as_of − 80 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, as_of, year",
    [
        ("'19", AS_OF, 2019),
        ("98", AS_OF, 1998),
        ("'45", AS_OF, 2045),  # as_of.year + 19: the top of the window
        ("'46", AS_OF, 1946),  # as_of.year − 80: the bottom of the window
        ("'19", D(2000, 1, 1), 2019),
        ("'20", D(2000, 1, 1), 1920),
    ],
)
def test_two_digit_year_pivot(text: str, as_of: dt.date, year: int) -> None:
    expected = dv(D(year, 7, 1), D(year, 1, 1), D(year, 12, 31), YEAR)
    assert parse_date(text, as_of=as_of) == (expected, (IMPRECISE, TWO_DIGIT))


def test_bare_two_digit_years_pair_only_with_each_other() -> None:
    got = parse_range("98 - 02", as_of=AS_OF)
    assert got is not None and got.start is not None and got.end is not None
    assert (got.start.value, got.end.value) == (D(1998, 7, 1), D(2002, 7, 1))
    assert got.flags == (IMPRECISE, TWO_DIGIT)
    # Beside a four-digit year a lone "13" is no year: this is not 2013 – 2020 swapped.
    assert parse_range("2020-13", as_of=AS_OF) is None


# ─── Row 8: future dates ──────────────────────────────────────────────────────────────────────


def test_future_end_is_clamped_to_as_of() -> None:
    assert parse_range("Jan 2025 – Dec 2027", as_of=AS_OF) == DateRange(
        start=dv(D(2025, 1, 1), D(2025, 1, 1), D(2025, 1, 31), MONTH),
        end=dv(AS_OF, AS_OF, AS_OF, DAY),
        is_current=False,
        flags=(Flag.FUTURE_END_CLAMPED,),
        raw="Jan 2025 – Dec 2027",
    )


@pytest.mark.parametrize("text", ["Jan 2027 – Mar 2028", "Since 2028", "Nov 2026 – Present"])
def test_future_start_drops_the_range(text: str) -> None:
    assert parse_range(text, as_of=AS_OF) == DateRange(
        start=None, end=None, is_current=False, flags=(Flag.FUTURE_START,), raw=text
    )


@pytest.mark.parametrize(
    "text, as_of, start",
    [  # an imprecise start whose period contains as_of has begun: keep it, inside [earliest, as_of]
        ("Fall 2026 – Present", AS_OF, dv(D(2026, 10, 4), D(2026, 10, 1), AS_OF, SEASON)),
        ("2027 – Present", D(2027, 1, 15), dv(D(2027, 1, 8), D(2027, 1, 1), D(2027, 1, 15), YEAR)),
    ],
)
def test_start_period_containing_as_of_is_kept(text: str, as_of: dt.date, start: DateValue) -> None:
    assert parse_range(text, as_of=as_of) == DateRange(
        start=start, end=None, is_current=True, flags=(IMPRECISE,), raw=text
    )


def test_as_of_itself_is_neither_future_start_nor_future_end() -> None:
    exact = dv(AS_OF, AS_OF, AS_OF, DAY)
    ending_today = parse_range("2026-01-05 – 2026-10-08", as_of=AS_OF)
    assert ending_today is not None and (ending_today.end, ending_today.flags) == (exact, ())
    starting_today = parse_range("2026-10-08 – Present", as_of=AS_OF)
    assert starting_today == DateRange(
        start=exact, end=None, is_current=True, raw="2026-10-08 – Present"
    )
    tomorrow = parse_range("2026-10-09 – Present", as_of=AS_OF)
    assert tomorrow is not None and tomorrow.flags == (Flag.FUTURE_START,)


def test_no_bound_lies_after_as_of() -> None:
    got = parse_range("2019 – 2026", as_of=AS_OF)
    assert got is not None and got.flags == (IMPRECISE,)
    assert got.end == dv(D(2026, 7, 1), D(2026, 1, 1), AS_OF, YEAR)


# ─── Row 9: end before start ──────────────────────────────────────────────────────────────────


def test_end_before_start_is_swapped_when_plausible() -> None:
    assert parse_range("Mar 2021 – Jan 2020", as_of=AS_OF) == DateRange(
        start=JAN_2020_START,
        end=MAR_2021_END,
        is_current=False,
        flags=(Flag.DATE_RANGE_SWAPPED,),
        raw="Mar 2021 – Jan 2020",
    )


def test_swap_at_exactly_fifty_years_is_plausible() -> None:
    got = parse_range("1997 – 1947", as_of=AS_OF)
    assert got is not None and got.start is not None and got.end is not None
    assert Flag.DATE_RANGE_SWAPPED in got.flags
    assert (got.start.value, got.end.value) == (D(1947, 7, 1), D(1997, 7, 1))


@pytest.mark.parametrize(
    "text",
    [
        "1998 – 1947",  # swapped it would last 51 years
        "2020 – 1950",  # 70 years
        "1960 – 1946",  # swapped start (1 Jul 1946) is earlier than as_of − 80 years (8 Oct 1946)
    ],
)
def test_implausible_inverted_range_is_dropped(text: str) -> None:
    assert parse_range(text, as_of=AS_OF) == DateRange(
        start=None, end=None, is_current=False, flags=(Flag.DATE_RANGE_DROPPED,), raw=text
    )


def test_overlapping_imprecise_ends_are_reconciled_not_swapped() -> None:
    # "Summer 2020 – 2020": the point estimates invert (16 Aug > 1 Jul) but the periods overlap,
    # so the role lies in their intersection (Jul 1 – Sep 30); nothing was written backwards.
    assert parse_range("Summer 2020 – 2020", as_of=AS_OF) == DateRange(
        start=dv(D(2020, 7, 1), D(2020, 7, 1), D(2020, 9, 30), SEASON),
        end=dv(D(2020, 9, 30), D(2020, 1, 1), D(2020, 12, 31), YEAR),
        is_current=False,
        flags=(IMPRECISE,),
        raw="Summer 2020 – 2020",
    )


# ─── Row 10: non-Gregorian calendars with an explicit marker ──────────────────────────────────


@pytest.mark.parametrize(
    "text, year",
    [
        ("2563 BE", 2020),  # Buddhist Era: − 543
        ("พ.ศ. 2563", 2020),
        ("1399 SH", 2020),  # Solar Hijri: + 621
        ("1399 هـ.ش", 2020),
        ("1441 AH", 2020),  # Hijri: round(1441 × 0.970229 + 621.5643) = round(2019.664) = 2020
        ("1441 هـ", 2020),
        ("١٤٤١ هـ", 2020),  # Arabic-Indic digits
        ("1445 AH", 2024),  # round(1445 × 0.970229 + 621.5643) = round(2023.545) = 2024
        ("5780 AM", 2020),  # Hebrew: − 3760
    ],
)
def test_non_gregorian_years_are_converted(text: str, year: int) -> None:
    expected = dv(D(year, 7, 1), D(year, 1, 1), D(year, 12, 31), YEAR)
    assert parse_date(text, as_of=AS_OF) == (expected, (IMPRECISE, CONVERTED))


def test_calendar_conversion_window_is_1900_to_as_of_plus_one() -> None:
    def year(text: str) -> int | None:
        got, _ = parse_date(text, as_of=AS_OF)
        return got.value.year if got else None

    assert (year("2443 BE"), year("2570 BE")) == (1900, 2027)
    assert parse_date("2442 BE", as_of=AS_OF) == (None, (Flag.UNSUPPORTED_CALENDAR,))  # 1899
    assert parse_date("2571 BE", as_of=AS_OF) == (None, (Flag.UNSUPPORTED_CALENDAR,))  # 2028


@pytest.mark.parametrize(
    "text",
    [
        "2400 BE",  # → 1857, before 1900
        "1250 SH",  # → 1871
        "1500 AH",  # → 2077, after as_of.year + 1
        "5500 AM",  # → 1740
    ],
)
def test_unconvertible_calendar_dates_are_flagged(text: str) -> None:
    assert parse_date(text, as_of=AS_OF) == (None, (Flag.UNSUPPORTED_CALENDAR,))
    assert parse_range(text, as_of=AS_OF) == DateRange(
        start=None, end=None, is_current=False, flags=(Flag.UNSUPPORTED_CALENDAR,), raw=text
    )


def test_degree_abbreviation_is_not_a_calendar_marker() -> None:
    # "BE" is also a Bachelor of Engineering; 2015 is no Buddhist-Era year (that would be 1472).
    got = parse_range("BE Computer Engineering, 2015 – 2019", as_of=AS_OF)
    assert got is not None and got.start is not None and got.end is not None
    assert (got.start.value, got.end.value, got.flags) == (D(2015, 7, 1), D(2019, 7, 1), (IMPRECISE,))
    assert parse_range("Engineer, BE 2019", as_of=AS_OF) == DateRange(
        start=dv(D(2019, 1, 1), D(2019, 1, 1), D(2019, 7, 1), YEAR),
        end=dv(D(2019, 12, 31), D(2019, 7, 1), D(2019, 12, 31), YEAR),
        is_current=False,
        flags=(IMPRECISE,),
        raw="2019",
    )
    assert parse_date("B.E. 2019", as_of=AS_OF) == (None, ())  # a degree and a year, not 1476


@pytest.mark.parametrize("marker", ["SH", "AH"])
def test_a_gregorian_year_beside_a_hijri_marker_is_not_converted(marker: str) -> None:
    # 2019 is no Solar Hijri or Hijri year (those run near 1400 now), so the marker is not read:
    # the number stays the Gregorian year it plainly is, with no UNSUPPORTED_CALENDAR review.
    assert parse_date(f"2019 {marker}", as_of=AS_OF) == (None, ())
    assert parse_range(f"Analyst, 2019 {marker}", as_of=AS_OF) == DateRange(
        start=dv(D(2019, 1, 1), D(2019, 1, 1), D(2019, 7, 1), YEAR),
        end=dv(D(2019, 12, 31), D(2019, 7, 1), D(2019, 12, 31), YEAR),
        is_current=False,
        flags=(IMPRECISE,),
        raw="2019",
    )


def test_a_clock_time_is_not_a_calendar_date() -> None:
    # "1000 AM" would convert from the Hebrew calendar to year −2760; it is a time, so no flag.
    assert parse_date("1000 AM", as_of=AS_OF) == (None, ())
    assert parse_range("1000 AM", as_of=AS_OF) is None
    assert find_date_ranges("Daily stand-up at 1000 AM", as_of=AS_OF) == ()


# ─── Row 11: what is not a date ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, year", [("1945", None), ("1946", 1946), ("2036", 2036), ("2037", None)]
)
def test_year_window_is_as_of_minus_80_to_plus_10(text: str, year: int | None) -> None:
    got, _ = parse_date(text, as_of=AS_OF)
    assert (got.value.year if got else None) == year


NOT_DATES = [
    "$2020",
    "€2020",
    "2020 users",
    "Call 613-555-2020",
    "+1 613 555 2020",
    "(613) 555-2020",
    "40k",
    "2,020",
    "2020%",
    "the 2020s",
    "v2020",
    "Grew revenue 40% to $2020k",
]


@pytest.mark.parametrize("text", NOT_DATES)
def test_numbers_that_are_not_dates_never_parse(text: str) -> None:
    assert find_date_ranges(text, as_of=AS_OF) == ()
    assert parse_range(text, as_of=AS_OF) is None
    assert parse_date(text, as_of=AS_OF) == (None, ())


# ─── find_date_ranges: dates located inside a longer line ─────────────────────────────────────


def test_finds_the_range_inside_a_header_line() -> None:
    line = "Software Intern, Acme — May 2023 – Aug 2023, Ottawa"
    start = line.index("May")
    end = start + len("May 2023 – Aug 2023")
    assert find_date_ranges(line, as_of=AS_OF) == (
        (
            start,
            end,
            DateRange(
                start=dv(D(2023, 5, 1), D(2023, 5, 1), D(2023, 5, 31), MONTH),
                end=dv(D(2023, 8, 31), D(2023, 8, 1), D(2023, 8, 31), MONTH),
                is_current=False,
                raw="May 2023 – Aug 2023",
            ),
        ),
    )


def test_finds_every_range_in_order_and_parse_range_takes_the_first() -> None:
    line = "Analyst, Jan 2019 – Mar 2020; Lead, Apr 2020 – Present"
    spans = find_date_ranges(line, as_of=AS_OF)
    assert [line[s:e] for s, e, _ in spans] == ["Jan 2019 – Mar 2020", "Apr 2020 – Present"]
    assert [r.is_current for _, _, r in spans] == [False, True]
    assert parse_range(line, as_of=AS_OF) == spans[0][2]


@pytest.mark.parametrize(
    "line, raw, current",
    [
        ("Developer, Acme (since 2019)", "since 2019", True),
        ("Barista, Café Nuit — Jan 2020 –", "Jan 2020 –", True),
        ("Tutor | 2018 - 2019 | Remote", "2018 - 2019", False),
        ("Stage de janvier 2020 à mars 2021, Montréal", "de janvier 2020 à mars 2021", False),
        ("Camp counsellor, Summer 2019 internship", "Summer 2019", False),
        ("Founded 2020 at Acme", "2020", False),
        ("Volunteer, from 2019", "2019", False),  # "from" with no end adds nothing
        ("Jan 2018 – Dec 2018, 2019 – 2020", "Jan 2018 – Dec 2018", False),
    ],
)
def test_span_covers_the_whole_date_expression(line: str, raw: str, current: bool) -> None:
    spans = find_date_ranges(line, as_of=AS_OF)
    assert spans and line[spans[0][0] : spans[0][1]] == raw
    assert spans[0][2].raw == raw and spans[0][2].is_current is current


def test_numeric_range_after_another_date_is_still_found() -> None:
    line = "Jan 2018 – Dec 2018, 2019 – 2020"
    assert [line[s:e] for s, e, _ in find_date_ranges(line, as_of=AS_OF)] == [
        "Jan 2018 – Dec 2018",
        "2019 – 2020",
    ]


def test_find_date_ranges_on_text_without_dates() -> None:
    assert find_date_ranges("Built a REST API in Python", as_of=AS_OF) == ()
    assert find_date_ranges("", as_of=AS_OF) == ()


# ─── months_between, DateRange invariants, flag texts ─────────────────────────────────────────


def test_months_between_uses_mean_month_length() -> None:
    # 487 days = 16 × 30.4375 exactly.
    assert months_between(D(2020, 1, 1), D(2021, 5, 2)) == 16.0
    assert months_between(D(2021, 5, 2), D(2020, 1, 1)) == -16.0
    assert months_between(D(2020, 1, 1), D(2020, 1, 1)) == 0.0


def test_date_range_rejects_inverted_or_current_with_end() -> None:
    later = dv(D(2021, 1, 1), D(2021, 1, 1), D(2021, 1, 1), DAY)
    earlier = dv(D(2020, 1, 1), D(2020, 1, 1), D(2020, 1, 1), DAY)
    with pytest.raises(ValidationError):
        DateRange(start=later, end=earlier, is_current=False)
    with pytest.raises(ValidationError):
        DateRange(start=earlier, end=later, is_current=True)


@pytest.mark.parametrize(
    "flag",
    [
        Flag.IMPRECISE_DATE,
        Flag.DATE_RANGE_SWAPPED,
        Flag.DATE_RANGE_DROPPED,
        Flag.FUTURE_END_CLAMPED,
        Flag.FUTURE_START,
        Flag.TWO_DIGIT_YEAR,
        Flag.NON_GREGORIAN_CONVERTED,
        Flag.UNSUPPORTED_CALENDAR,
    ],
)
def test_every_date_flag_has_candidate_text(flag: Flag) -> None:
    assert FLAG_TEXT[flag]


# ─── "Mon D(D)" never invents a year (review fix round 1) ───────────────────────────────────


def day(y: int, m: int, d: int) -> DateValue:
    return dv(D(y, m, d), D(y, m, d), D(y, m, d), DAY)


@pytest.mark.parametrize(
    "text, start, end",
    [
        ("June 15 – August 15, 2020", day(2020, 6, 15), day(2020, 8, 15)),
        ("June 1 – August 31, 2020", day(2020, 6, 1), day(2020, 8, 31)),
        ("June 1st – August 31st, 2020", day(2020, 6, 1), day(2020, 8, 31)),
        ("May 10 – Aug 20, 2023", day(2023, 5, 10), day(2023, 8, 20)),
        ("Sep 30 – Dec 15, 2025", day(2025, 9, 30), day(2025, 12, 15)),
        ("Sept 2023 – Dec 12", dv(D(2023, 9, 1), D(2023, 9, 1), D(2023, 9, 30), MONTH), day(2023, 12, 12)),
        ("Jan 2020 – Mar 15", JAN_2020_START, day(2020, 3, 15)),
        ("Jan 2020 – Mar 21", JAN_2020_START, day(2020, 3, 21)),
        (  # the month-order rule: a December start before a January end is the year before
            "Dec 15 – Jan 2021",
            day(2020, 12, 15),
            dv(D(2021, 1, 31), D(2021, 1, 1), D(2021, 1, 31), MONTH),
        ),
    ],
)
def test_month_and_day_beside_a_four_digit_year_is_a_day_in_that_year(
    text: str, start: DateValue, end: DateValue
) -> None:
    assert parse_range(text, as_of=AS_OF) == DateRange(start=start, end=end, is_current=False, raw=text)


def test_month_and_day_inside_a_header_line() -> None:
    line = "Camp Counsellor, Camp Ahmek — June 20 – August 25, 2023"
    found = DateRange(
        start=day(2023, 6, 20), end=day(2023, 8, 25), is_current=False, raw="June 20 – August 25, 2023"
    )
    assert find_date_ranges(line, as_of=AS_OF) == ((line.index("June"), len(line), found),)


def test_month_and_day_before_present_is_no_date() -> None:
    # "May 15 – Present" is no more datable than "May 15" alone: never "since 2015".
    assert parse_range("May 15 – Present", as_of=AS_OF) is None
    assert find_date_ranges("Lifeguard, May 15 – Present", as_of=AS_OF) == ()


def test_two_digit_years_after_a_month_need_an_apostrophe() -> None:
    assert parse_range("Jan '19 – Mar '21", as_of=AS_OF) == DateRange(
        start=dv(D(2019, 1, 1), D(2019, 1, 1), D(2019, 1, 31), MONTH),
        end=MAR_2021_END,
        is_current=False,
        flags=(TWO_DIGIT,),
        raw="Jan '19 – Mar '21",
    )


@pytest.mark.parametrize(
    "text",
    [
        "June 15 – August 30",  # not 2015 – 2030 clamped to today: about 11 invented years
        "Jan 19 – Mar 21",
        "Jun 18 – Aug 18",
        "Jan 5 – Mar 21",
        "Jun 18th – Aug 18",
    ],
)
def test_month_and_number_without_a_four_digit_year_is_no_date(text: str) -> None:
    assert parse_range(text, as_of=AS_OF) is None
    assert find_date_ranges(f"Camp staff, {text}", as_of=AS_OF) == ()
    assert parse_date(text, as_of=AS_OF) == (None, ())


@pytest.mark.parametrize(
    "text, expected",
    [  # two digits that cannot be a day (00, 32–99) are a two-digit year even without an apostrophe
        ("Jan 98", dv(D(1998, 1, 1), D(1998, 1, 1), D(1998, 1, 31), MONTH)),
        ("Mar 00", dv(D(2000, 3, 1), D(2000, 3, 1), D(2000, 3, 31), MONTH)),
    ],
)
def test_month_and_number_that_cannot_be_a_day_is_a_two_digit_year(text: str, expected: DateValue) -> None:
    assert parse_date(text, as_of=AS_OF) == (expected, (TWO_DIGIT,))


def test_range_of_months_with_unambiguous_two_digit_years() -> None:
    assert parse_range("Jan 98 – Mar 99", as_of=AS_OF) == DateRange(
        start=dv(D(1998, 1, 1), D(1998, 1, 1), D(1998, 1, 31), MONTH),
        end=dv(D(1999, 3, 31), D(1999, 3, 1), D(1999, 3, 31), MONTH),
        is_current=False,
        flags=(TWO_DIGIT,),
        raw="Jan 98 – Mar 99",
    )


@pytest.mark.parametrize("text", ["Jan 5 – Mar '21", "Jan 19 – Mar '21"])
def test_month_and_day_beside_an_apostrophe_year_leaves_that_month_alone(text: str) -> None:
    # The day side has no four-digit year to take, so it gives nothing: never a day of 2021, never
    # 2005 or 2019. The apostrophe side stands alone as March 2021.
    assert parse_range(text, as_of=AS_OF) == DateRange(
        start=dv(D(2021, 3, 1), D(2021, 3, 1), D(2021, 3, 31), MONTH),
        end=MAR_2021_END,
        is_current=False,
        flags=(TWO_DIGIT,),
        raw="Mar '21",
    )


# ─── Linear time on long runs (review fix round 1) ───────────────────────────────────────────

LONG_RUNS = {
    "dashes": "-" * 20_000,
    "en dashes then present": "–" * 20_000 + " Present",
    "spaces": " " * 20_000,
    "spaces after a year": "2020" + " " * 20_000 + "x",
    "spaces after a month": "Jan" + " " * 20_000 + "x",
    "spaces after a separator": "to" + " " * 20_000 + "x",
    "dash space pairs": "- " * 10_000,
}


@pytest.mark.parametrize("text", list(LONG_RUNS.values()), ids=list(LONG_RUNS))
def test_long_runs_parse_in_linear_time(text: str) -> None:
    for parse in (parse_date, parse_range, find_date_ranges):
        began = time.perf_counter()
        parse(text, as_of=AS_OF)
        assert time.perf_counter() - began < 0.5, parse.__name__


# ─── Row 12: totality ─────────────────────────────────────────────────────────────────────────

_TOKENS = st.sampled_from(
    [
        "Jan", "janv.", "Sept", "août", "May", "Summer", "été", "Fall", "Q3", "Q5", "2020", "2019",
        "1999", "2563", "1441", "5780", "'19", "’98", "98", "19", "03", "15", "31", "00", "0",
        "/", "-", "–", "—", ".", ",", "'", " ", "to", "à", "au", "jusqu'à", "since", "Depuis",
        "Present", "now", "to date", "BE", "AH", "SH", "AM", "هـ", "هـ.ش", "พ.ศ.", "$", "%", "k",
        "users", "(", ")", "1er", "15th", "٢٠٢٠",
    ]
)
_SOUP = st.lists(_TOKENS, max_size=14).flatmap(
    lambda parts: st.sampled_from(["", " "]).map(lambda glue: glue.join(parts))
)


def _assert_total(text: str, as_of: dt.date) -> None:
    for lang in ("en", "fr"):
        for role in ("start", "end", "point"):
            value, flags = parse_date(text, as_of=as_of, role=role, lang=lang)
            assert value is None or isinstance(value, DateValue)
            assert all(isinstance(f, Flag) for f in flags)
        ranges = [parse_range(text, as_of=as_of, lang=lang)]
        previous_end = 0
        for start, end, found in find_date_ranges(text, as_of=as_of, lang=lang):
            assert previous_end <= start < end <= len(text)
            assert found.raw == text[start:end]
            previous_end = end
            ranges.append(found)
        for r in ranges:
            if r is None:
                continue
            if r.start is not None and r.end is not None:
                assert r.start.value <= r.end.value
            if r.is_current:
                assert r.end is None
            for bound in (r.start, r.end):
                assert bound is None or bound.latest <= as_of
            assert list(r.flags) == sorted(set(r.flags), key=lambda f: f.value)


@given(text=st.text(max_size=120), as_of=st.dates())
def test_total_on_arbitrary_text(text: str, as_of: dt.date) -> None:
    _assert_total(text, as_of)


@given(text=_SOUP, as_of=st.dates(min_value=D(1990, 1, 1), max_value=D(2060, 12, 31)))
def test_total_on_date_like_token_soup(text: str, as_of: dt.date) -> None:
    _assert_total(text, as_of)


@given(text=_SOUP, as_of=st.dates())
def test_total_for_any_as_of(text: str, as_of: dt.date) -> None:
    _assert_total(text, as_of)
