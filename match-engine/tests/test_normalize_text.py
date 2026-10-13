"""normalize_text: NFKC and friends at ingest, before any matching (SPEC §14.1, brief row 9)."""

from __future__ import annotations

import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from matcher.ingest import normalize_text

ZWSP, ZWNJ, ZWJ, WJ, BOM = (chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
LRM, RLM, SHY, NBSP = (chr(c) for c in (0x200E, 0x200F, 0x00AD, 0x00A0))
LIG_FF, LIG_FI, LIG_FL, LIG_FFI = (chr(c) for c in (0xFB00, 0xFB01, 0xFB02, 0xFB03))


def test_nfkc_expands_ligatures() -> None:
    assert normalize_text(f"{LIG_FI}le {LIG_FL}ow e{LIG_FF}ort o{LIG_FFI}ce") == "file flow effort office"


def test_trademark_service_mark_and_registered_signs_are_dropped() -> None:
    # Task 7.3 fix round 1: NFKC spells U+2122 as the letters TM glued to the name ("SparkTM").
    assert normalize_text("Apache Spark™, Tableau™ and Python®; FedEx℠") == (
        "Apache Spark, Tableau and Python; FedEx")
    # fix round 2: a sign between two letters separates them instead of gluing them
    assert normalize_text("Spark™and Kafka®Streams") == "Spark and Kafka Streams"
    assert normalize_text("Spark™ and") == "Spark and"


def test_nfkc_folds_compatibility_forms() -> None:
    fullwidth = "".join(chr(0xFF00 + ord(c) - 0x20) for c in "Python")
    assert normalize_text(fullwidth) == "Python"
    assert normalize_text("m" + chr(0x00B2)) == "m2"
    assert normalize_text("e" + chr(0x0301)) == chr(0x00E9)  # composed


@pytest.mark.parametrize("char", [ZWSP, ZWNJ, ZWJ, WJ, BOM, LRM, RLM, chr(0x2066), chr(0x2069), chr(0x180E)])
def test_zero_width_and_format_characters_are_removed(char: str) -> None:
    assert normalize_text(f"Py{char}thon {char}SQL") == "Python SQL"


def test_soft_hyphens_inside_words_are_removed() -> None:
    assert normalize_text(f"experi{SHY}ence and know{SHY}ledge") == "experience and knowledge"


def test_soft_hyphen_at_a_line_end_joins_the_word() -> None:
    assert normalize_text(f"Led the devel{SHY}\nopment of tools") == "Led the development\nof tools"
    assert normalize_text(f"Java{SHY}\nScript developer") == "JavaScript\ndeveloper"
    assert normalize_text(f"trailing{SHY}") == "trailing"


def test_hyphen_at_a_line_end_joins_a_lowercase_continuation() -> None:
    assert normalize_text("Led the devel-\nopment of tools") == "Led the development\nof tools"
    assert normalize_text("Led the devel-  \n   opment, testing") == "Led the development,\ntesting"
    assert normalize_text("devel-\nopment") == "development"  # the second line is used up
    assert normalize_text("devel" + chr(0x2010) + "\nopment") == "development"  # U+2010 HYPHEN


def test_hyphen_joins_chain() -> None:
    assert normalize_text("a-\nb-\nc and more") == "abc\nand more"


@pytest.mark.parametrize("text", [
    "Software-\nEntwicklung",  # uppercase continuation: a real compound
    "2019-\n2020",  # digits are not hyphenated words
    "skills -\nmore",  # a dash after a space is not a hyphenation
    "devel-\n\nopment",  # a blank line between is a paragraph break
    "devel-\n- opment",  # the next line is a bullet
])
def test_hyphen_is_kept_when_it_is_not_a_line_break_hyphenation(text: str) -> None:
    assert normalize_text(text) == text


def test_smart_quotes_become_straight() -> None:
    text = (f"{chr(0x201C)}Analyst{chr(0x201D)} {chr(0x2018)}data{chr(0x2019)} {chr(0x201E)}x{chr(0x201F)} "
            f"{chr(0x201A)}y{chr(0x201B)} team{chr(0x2019)}s don{chr(0x02BC)}t")
    assert normalize_text(text) == "\"Analyst\" 'data' \"x\" 'y' team's don't"


@pytest.mark.parametrize("glyph", [0x2022, 0x25AA, 0x25E6, 0x25CF, 0x2023, 0x2219, 0x27A2, 0x2713, 0x2013,  # brief
                                   0x25A0, 0x25CB, 0x25C6, 0x25BA, 0x25B6, 0x27A4, 0x2714, 0x2043, 0x00B7,
                                   0x2014, 0xF0B7, 0xF0A7, 0xF076, 0xF0D8, 0xF0FC, 0x002A])
def test_bullet_glyphs_at_line_start_become_a_dash(glyph: int) -> None:
    assert normalize_text(f"{chr(glyph)} Python\n  {chr(glyph)}SQL") == "- Python\n- SQL"


def test_bullets_and_dashes_inside_a_line_are_kept() -> None:
    assert normalize_text(f"Python {chr(0x2022)} SQL") == f"Python {chr(0x2022)} SQL"
    assert normalize_text(f"2019 {chr(0x2013)} 2021") == f"2019 {chr(0x2013)} 2021"
    assert normalize_text("- Already a dash") == "- Already a dash"


def test_spaces() -> None:
    for space in (NBSP, chr(0x2009), chr(0x202F), chr(0x3000), chr(0x2007), "\t"):
        assert normalize_text(f"Data{space}Analyst") == "Data Analyst"
    assert normalize_text("   Data    Analyst  \t ") == "Data Analyst"


def test_line_breaks_are_kept() -> None:
    assert normalize_text("a\nb\r\nc\rd") == "a\nb\nc\nd"
    assert normalize_text("a\n\nb") == "a\n\nb"
    assert normalize_text("a" + chr(0x2028) + "b" + chr(0x2029) + "c\x0cd") == "a\nb\nc\nd"


def test_control_characters_become_spaces() -> None:
    assert normalize_text("a\x00b\x07c\x7fd") == "a b c d"


def test_found_by_hypothesis() -> None:
    # A control character before a final soft hyphen used to leave a trailing space.
    assert normalize_text("0\x1b" + SHY) == "0"
    for text in ("x " + SHY + "\nnext", "\x1b" + SHY + "\nA", "- " + SHY):
        once = normalize_text(text)
        assert normalize_text(once) == once and not once.endswith(" ")


def test_empty() -> None:
    assert normalize_text("") == ""
    assert normalize_text("\n") == "\n"


@settings(max_examples=400)
@given(st.text())
def test_idempotent(text: str) -> None:
    once = normalize_text(text)
    assert normalize_text(once) == once


@settings(max_examples=400)
@given(st.text())
def test_output_invariants(text: str) -> None:
    out = normalize_text(text)
    assert unicodedata.is_normalized("NFKC", out)
    assert not any(unicodedata.category(c) == "Cf" for c in out)  # zero-width, soft hyphen, bidi marks
    assert not any(unicodedata.category(c) in ("Cc", "Zs") and c not in "\n " for c in out)
    assert "\r" not in out and "  " not in out
    assert all(line == line.strip() for line in out.split("\n"))
