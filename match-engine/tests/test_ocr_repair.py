"""OCR text repair (Task 5.4, the M6 format-invariance gate; config/parse/ocr.yaml).

A scanned résumé must parse like its PDF. OCR misreads three things the parser depends on, and only
OCR-sourced text is repaired: a roman level after a title word ("Engineer Ill" for "Engineer III"),
a bullet glyph at a line start (":" or "." for "-"), and an acronym the resolver knows ("Bl" for
"BI", "Cl/CD" for "CI/CD"), repaired only when the repaired form is an exact key of the resolver and
the text as read is not. Nothing is ever added. The examples are invented.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from matcher.errors import Flag
from matcher.graph.loader import load_resolver
from matcher.ingest import IngestResult, TextLine
from matcher.ingest.ocr_repair import OcrTable, exact_key, repair_lines, repair_ocr
from matcher.models import ParseTier
from matcher.parse._entries import TITLE_NOUNS, place_of
from matcher.parse.config import header_key, load_parse_config
from matcher.parse.resume import load_resume_resources, parse_ingested

AS_OF = date(2026, 10, 9)
TAXONOMY = Path(__file__).resolve().parents[1] / "data" / "taxonomy"


@pytest.fixture(scope="module")
def pc():
    return load_parse_config()


@pytest.fixture(scope="module")
def resolver(config):
    return load_resolver(TAXONOMY, config)


def _header(pc):
    return lambda text: header_key(text) in pc.sections.header_index


def _known(resolver):
    return exact_key(resolver)


def _repair(lines: list[str], pc, resolver) -> list[str]:
    return repair_lines(lines, table=pc.ocr, title_words=TITLE_NOUNS, known=_known(resolver), place=place_of,
                        header=_header(pc))


# ─── the table ──────────────────────────────────────────────────────────────────────────────


def test_the_table_is_loaded_with_the_parse_config(pc) -> None:
    assert isinstance(pc.ocr, OcrTable)
    assert pc.ocr.level_tokens["Ill"] == "III" and pc.ocr.level_tokens["Il"] == "II"
    assert ":" in pc.ocr.bullet_glyphs


def test_a_level_token_must_be_a_roman_level(pc) -> None:
    with pytest.raises(ValueError):
        OcrTable(**{**pc.ocr.model_dump(), "level_tokens": {"Ill": "Senior"}})


# ─── levels ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("read, repaired", [
    ("Data Analyst Ill, Tidewater Research Oct 2020 - May 2023", "Data Analyst III, Tidewater Research Oct 2020 - May 2023"),
    ("Software Engineer IlI", "Software Engineer III"),
    ("Marketing Specialist Il, Granite Bay Brands", "Marketing Specialist II, Granite Bay Brands"),
    ("Product Designer lI | Juniper Design", "Product Designer II | Juniper Design"),
])
def test_a_misread_level_after_a_title_word_is_repaired(pc, resolver, read: str, repaired: str) -> None:
    assert _repair([read], pc, resolver) == [repaired]


@pytest.mark.parametrize("line", [
    "Ill health forced a leave in 2019",  # no title word before it
    "Il a dirigé une équipe de 4 analystes",  # French "il"
    "Engineer Illustrations for the catalogue",  # part of a longer word
    "Data Analyst III, Tidewater Research",  # already right
])
def test_other_ls_and_is_are_left_alone(pc, resolver, line: str) -> None:
    assert _repair([line], pc, resolver) == [line]


# ─── bullets ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("read, repaired", [
    (": Deployed requirements documents with Amplitude", "- Deployed requirements documents with Amplitude"),
    (". Automated launch plans with Jira", "- Automated launch plans with Jira"),
    (":. Cut costs by 12%", "- Cut costs by 12%"),
    (": : Built 3 dashboards", "- Built 3 dashboards"),
    (". 'Led the rollout of launch campaigns", "- Led the rollout of launch campaigns"),
    ("。 Lab demonstrator, Practical Methods Studio", "- Lab demonstrator, Practical Methods Studio"),
    ("e Mentored 4 interns", "- Mentored 4 interns"),
])
def test_a_misread_bullet_glyph_at_a_line_start_is_a_bullet(pc, resolver, read: str, repaired: str) -> None:
    assert _repair([read], pc, resolver) == [repaired]


@pytest.mark.parametrize("line", [
    "e-commerce analytics for 3 brands",  # a word, not a glyph
    "eBay Seller Tools",
    "...and 40 more",  # lower case after it: not a bullet
    "Deployed requirements documents",
])
def test_a_line_start_that_is_no_bullet_glyph_is_left_alone(pc, resolver, line: str) -> None:
    assert _repair([line], pc, resolver) == [line]


# ─── spaces and places ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("read, repaired", [
    ("Staff Data Analyst, Harborview Data 07/2023 to.Present", "Staff Data Analyst, Harborview Data 07/2023 to Present"),
    ("- Tutor, Introductory Quantitative.Reasoning", "- Tutor, Introductory Quantitative Reasoning"),
    ("Lead Data Analyst, Evergreen Insights May 2024. - Present", "Lead Data Analyst, Evergreen Insights May 2024 - Present"),
    ("Data Analyst II, Pinecrest Analytics July 2025 -.Present", "Data Analyst II, Pinecrest Analytics July 2025 - Present"),
])
def test_a_dot_read_for_a_space_before_a_capitalised_word_is_a_space(pc, resolver, read: str, repaired: str) -> None:
    assert _repair(["Jordan Sample", read], pc, resolver) == ["Jordan Sample", repaired]


@pytest.mark.parametrize("line", [
    "- Built services in Node.js and ASP.NET",  # no capitalised word after the dot, or a known key
    "jordan.sample@example.com | Denver, CO",
    "- Shipped v2.Released to 4 teams",  # a digit before the dot
])
def test_dots_inside_names_stay(pc, resolver, line: str) -> None:
    assert _repair(["Jordan Sample", line], pc, resolver) == ["Jordan Sample", line]


@pytest.mark.parametrize("read, repaired", [
    ("Denver, co", "Denver, CO"),
    ("Halifax, Ns", "Halifax, NS"),
])
def test_a_region_code_read_in_lower_case_is_a_place_again(pc, resolver, read: str, repaired: str) -> None:
    assert _repair(["Jordan Sample", read], pc, resolver) == ["Jordan Sample", repaired]


def test_a_speck_after_a_listed_header_is_dropped(pc, resolver) -> None:
    """OCR reads a speck after "Education" as a period; typed text keeps "Experience." a sentence
    (test_sections), so only OCR text is repaired, and only a line that is otherwise exactly a
    listed header."""
    lines = ["Avery Sample", "Education.", "Relevant Technical Experience.", "Built it."]
    assert _repair(lines, pc, resolver) == ["Avery Sample", "Education", "Relevant Technical Experience.", "Built it."]


def test_a_line_that_names_no_place_keeps_its_case(pc, resolver) -> None:
    assert _repair(["Jordan Sample", "Shipped it, so"], pc, resolver) == ["Jordan Sample", "Shipped it, so"]


# ─── acronyms ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("read, repaired", [
    ("- Built Bl dashboards with Tableau", "- Built BI dashboards with Tableau"),
    ("- Owned the Cl/CD pipeline", "- Owned the CI/CD pipeline"),
    ("Tools: Terraform, laC, Docker", "Tools: Terraform, IaC, Docker"),
    ("Tools: Figma, Adobe Ilustrator, Sketch", "Tools: Figma, Adobe Illustrator, Sketch"),  # one l of a pair lost
])
def test_a_misread_acronym_the_resolver_knows_is_repaired(pc, resolver, read: str, repaired: str) -> None:
    assert _repair(["Jordan Sample", read], pc, resolver) == ["Jordan Sample", repaired]


@pytest.mark.parametrize("line", [
    "- Analysed sales with Excel",  # a known word as read
    "- Worked with lllustrative partners",  # the repaired form is no skill either
    "- Built AwS Lambda functions",  # the resolver already reads it
])
def test_words_that_need_no_repair_are_left_alone(pc, resolver, line: str) -> None:
    assert _repair(["Jordan Sample", line], pc, resolver) == ["Jordan Sample", line]


def test_the_name_line_is_never_repaired(pc, resolver) -> None:
    assert _repair(["Al Sample", "- Built Bl dashboards"], pc, resolver) == ["Al Sample", "- Built BI dashboards"]


# ─── only OCR-sourced text ──────────────────────────────────────────────────────────────────


def _ingested(lines: list[str], flags: tuple[Flag, ...], ocr_lines: set[int] | None = None) -> IngestResult:
    """Lines placed one per row; `ocr_lines` (default: every line when OCR_USED) came from OCR."""
    if ocr_lines is None:
        ocr_lines = set(range(len(lines))) if Flag.OCR_USED in flags else set()
    placed = tuple(TextLine(text=t, page=0, x0=0.0, y0=12.0 * i, x1=200.0, y1=12.0 * i + 10, size=10.0,
                            ocr=i in ocr_lines) for i, t in enumerate(lines))
    return IngestResult(ok=True, text="\n".join(lines), lines=placed, tier=ParseTier.C, language="en",
                        languages={"en": 1.0}, flags=flags, reasons=(), page_count=1, resume_likelihood=0.9)


SCAN = ["Jordan Sample", "Work Experience", "Data Analyst Ill, Tidewater Research Oct 2020 - May 2023",
        ": Built Bl dashboards in Tableau", ". Automated reports with Python"]


def test_repair_ocr_repairs_only_the_lines_ocr_read(pc, resolver) -> None:
    """Fix round 1 (I3): a PDF with one scanned page among typed pages is flagged OCR_USED for the
    whole document; only the lines OCR read (per page) are repaired."""
    typed_then_scanned = _ingested(["Jordan Sample", "- Built Bl dashboards", ": Built Bl dashboards"],
                                   (Flag.OCR_USED,), ocr_lines={2})
    repaired = repair_ocr(typed_then_scanned, table=pc.ocr, title_words=TITLE_NOUNS, known=_known(resolver),
                          place=place_of, header=_header(pc))
    assert [line.text for line in repaired.lines] == ["Jordan Sample", "- Built Bl dashboards", "- Built BI dashboards"]


@pytest.mark.parametrize("line", [
    ".NET Developer, Acme Corp | 2020 – 2022",  # a dot glued to a word is no bullet
    "Al Ain, UAE",  # a name before another capitalised word is never an acronym
    "References: Al Smith, Manager",
    "Graphic Designer, Mel Studio",
    "Server, Il Fornaio | 2019 – 2020",  # Italian "Il" after a comma: no title word right before it
    "Chef Il Fornaio",  # a word follows the token
    "Coordinator Ill-health leave program",  # hyphenated: part of a word
    "Director, Ill Communications",
])
def test_real_text_is_never_repaired_into_something_else(pc, resolver, line: str) -> None:
    """Fix round 1 (I3, M3): the reviewer's false positives."""
    assert _repair(["Jordan Sample", line], pc, resolver) == ["Jordan Sample", line]


def test_a_failing_repair_leaves_that_line_as_read(pc) -> None:
    """Fix round 1 (M4): a repair that raises leaves its line unrepaired and never fails the rest."""
    def known(word: str) -> bool:
        if word == "Bl":
            raise RuntimeError("resolver down")
        return word == "IaC"
    out = repair_lines(["Jordan Sample", "- Built Bl dashboards", "Tools: laC", "Data Analyst Ill, Acme Corp"],
                       table=pc.ocr, title_words=TITLE_NOUNS, known=known)
    assert out == ["Jordan Sample", "- Built Bl dashboards", "Tools: IaC", "Data Analyst III, Acme Corp"]


def test_repair_ocr_repairs_only_ocr_text(pc, resolver) -> None:
    scanned = repair_ocr(_ingested(SCAN, (Flag.OCR_USED,)), table=pc.ocr, title_words=TITLE_NOUNS,
                         known=_known(resolver), place=place_of,
                        header=_header(pc))
    assert [line.text for line in scanned.lines][2:] == [
        "Data Analyst III, Tidewater Research Oct 2020 - May 2023", "- Built BI dashboards in Tableau",
        "- Automated reports with Python"]
    assert scanned.text == "\n".join(line.text for line in scanned.lines)
    typed = _ingested(SCAN, ())
    assert repair_ocr(typed, table=pc.ocr, title_words=TITLE_NOUNS, known=_known(resolver), place=place_of,
                        header=_header(pc)) == typed


def test_a_scanned_resume_parses_like_its_text(pc, resolver, config) -> None:
    resources = load_resume_resources(TAXONOMY, parse_config=pc)
    clean = ["Jordan Sample", "Work Experience", "Data Analyst III, Tidewater Research Oct 2020 - May 2023",
             "- Built BI dashboards in Tableau", "- Automated reports with Python"]

    def parsed(lines: list[str], flags: tuple[Flag, ...]):
        return parse_ingested(_ingested(lines, flags), as_of=AS_OF, resolver=resolver, config=config,
                              parse_config=pc, resources=resources).resume

    scan, text = parsed(SCAN, (Flag.OCR_USED,)), parsed(clean, (Flag.OCR_USED,))
    assert scan == text
    (role,) = scan.roles
    assert role.seniority is not None and role.seniority.level == 3
    assert len(role.skills) == len(text.roles[0].skills) >= 3
