"""Ingest edge cases (SPEC §14.1): every fixture in tests/fixtures/edge/ has a defined outcome.

The fixture directory is the specification (SPEC §14); EXPECTED below is its answer key and the
first test fails if a file has no entry. The remaining tests follow the brief's twelve rows,
each against its fixtures.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from matcher.config import EngineConfig
from matcher.errors import NEEDS_REVIEW_REASONS, Flag
from matcher.ingest import (
    SUPPORTED_LANGUAGES,
    IngestResult,
    TextLine,
    ingest_bytes,
    ingest_text,
    resume_likelihood,
)
from matcher.models import ParseTier, canonical_json
from tests.fixtures.edge import make_edge_fixtures as gen

EDGE = gen.FIXTURE_DIR
ENGINE_ROOT = Path(__file__).resolve().parents[1]

A, B, C = ParseTier.A, ParseTier.B, ParseTier.C
STUFF = Flag.STUFFING_SUSPECTED
OCR = Flag.OCR_USED


@dataclass(frozen=True)
class Expect:
    reasons: tuple[Flag, ...] = ()  # empty = ok
    tier: Optional[ParseTier] = None  # None = not asserted
    flags: frozenset[Flag] = field(default_factory=frozenset)  # exact set of informational flags
    language: Optional[str] = "?"  # "?" = not asserted

    @property
    def ok(self) -> bool:
        return not self.reasons


def ok(tier: ParseTier = A, *flags: Flag, language: Optional[str] = "en") -> Expect:
    return Expect((), tier, frozenset(flags), language)


def review(*reasons: Flag, flags: tuple[Flag, ...] = (), language: Optional[str] = "?") -> Expect:
    return Expect(tuple(reasons), None, frozenset(flags), language)


EXPECTED: dict[str, Expect] = {
    # PDF with a text layer
    "pdf_clean_one_column.pdf": ok(),
    "pdf_right_aligned_dates.pdf": ok(),
    "pdf_two_column.pdf": ok(),
    "pdf_three_column.pdf": ok(),
    "pdf_canva_sidebar.pdf": ok(),
    "pdf_header_footer.pdf": ok(),
    "pdf_page_numbers_plain.pdf": ok(),
    "pdf_page_numbers_dashed.pdf": ok(),
    "pdf_ligatures.pdf": ok(),
    "pdf_zero_width.pdf": ok(),
    "pdf_soft_hyphens.pdf": ok(),
    "pdf_hyphenated_lines.pdf": ok(),
    "pdf_smart_quotes.pdf": ok(),
    "pdf_bullet_glyphs.pdf": ok(),
    "pdf_white_text.pdf": ok(A, STUFF),
    "pdf_tiny_text.pdf": ok(A, STUFF),
    "pdf_invisible_render_mode.pdf": ok(A, STUFF),
    "pdf_clip_render_modes.pdf": ok(A, STUFF),
    "pdf_fake_bold.pdf": ok(),
    "pdf_zero_opacity.pdf": ok(A, STUFF),
    "pdf_offpage_text.pdf": ok(A, STUFF),
    "pdf_encrypted.pdf": review(Flag.ENCRYPTED, language=None),
    "pdf_owner_password_only.pdf": ok(),
    "pdf_truncated.pdf": review(Flag.CORRUPT, language=None),
    "pdf_garbled.pdf": review(Flag.CORRUPT, language=None),
    "pdf_junk_prefix.pdf": ok(),
    # Scans, images, photos
    "scan_300dpi.pdf": ok(C, OCR),
    "scan_noisy_72dpi.pdf": review(Flag.UNREADABLE, flags=(OCR,), language=None),
    "pdf_searchable_scan.pdf": ok(C, OCR),
    "pdf_blank_page.pdf": review(Flag.NOT_A_RESUME, language=None),
    "image_resume.png": ok(C, OCR),
    "photo_no_text.jpg": review(Flag.NOT_A_RESUME, flags=(OCR,), language=None),
    # Long documents
    "pdf_90_pages.pdf": ok(),
    "pdf_120_pages.pdf": ok(A, Flag.TRUNCATED),
    # Wrong / minimal documents, reading order
    "pdf_two_line_resume.pdf": ok(A, language=None),
    "pdf_cover_letter.pdf": review(Flag.NOT_A_RESUME, language="en"),
    "pdf_transcript.pdf": review(Flag.NOT_A_RESUME),
    "pdf_column_order_uncertain.pdf": ok(B, Flag.COLUMN_ORDER_UNCERTAIN),
    # Languages (D-70)
    "pdf_french_resume.pdf": ok(language="fr"),
    "pdf_spanish_resume.pdf": review(Flag.UNSUPPORTED_LANGUAGE, language="es"),
    "pdf_german_resume.pdf": review(Flag.UNSUPPORTED_LANGUAGE, language="de"),
    "pdf_mixed_fr_en.pdf": ok(A, Flag.MIXED_LANGUAGE, language="fr"),
    "pdf_arabic_resume.pdf": review(Flag.UNSUPPORTED_LANGUAGE, language="ar"),
    "pdf_hebrew_resume.pdf": review(Flag.UNSUPPORTED_LANGUAGE, language="he"),
    # DOCX
    "docx_clean.docx": ok(),
    "docx_tables.docx": ok(),
    "docx_text_boxes.docx": ok(B, Flag.COLUMN_ORDER_UNCERTAIN),
    "docx_text_box_inline.docx": ok(),
    "docx_header_footer.docx": ok(),
    "docx_hidden_text.docx": ok(A, STUFF),
    "docx_white_text.docx": ok(A, STUFF),
    "docx_tiny_text.docx": ok(A, STUFF),
    "docx_white_on_dark.docx": ok(),
    "docx_corrupt_zip.docx": review(Flag.CORRUPT, language=None),
    "docx_zip_without_document.docx": review(Flag.CORRUPT, language=None),
    # Plain text
    "text_utf8.txt": ok(),
    "text_utf8_bom.txt": ok(),
    "text_utf16le_bom.txt": ok(),
    "text_utf16be_bom.txt": ok(),
    "text_cp1252_french.txt": ok(language="fr"),
    "text_two_line_resume.txt": ok(A, language=None),
    "text_binary_named.txt": review(Flag.CORRUPT, language=None),
    # Type detection
    "random_bytes.bin": review(Flag.CORRUPT, language=None),
    "zero_bytes.pdf": review(Flag.CORRUPT, language=None),
    "pdf_named_txt.txt": ok(),
    "text_named_pdf.pdf": review(Flag.CORRUPT, language=None),
    "docx_named_pdf.pdf": ok(),
    "gif_image.gif": review(Flag.CORRUPT, language=None),
    "png_corrupt.png": review(Flag.CORRUPT, language=None),
}

HIDDEN = gen.HIDDEN_WORDS.split()


class _Cache:
    """One ingest per fixture per session (OCR fixtures take seconds); tests read from here."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self._results: dict[str, IngestResult] = {}

    def __call__(self, name: str) -> IngestResult:
        if name not in self._results:
            self._results[name] = ingest_bytes((EDGE / name).read_bytes(), name, config=self.config)
        return self._results[name]


@pytest.fixture(scope="session")
def ingested(config: EngineConfig) -> _Cache:
    return _Cache(config)


def _lines(result: IngestResult) -> list[str]:
    return [line.text for line in result.lines]


def _with(config: EngineConfig, **ingest_changes: object) -> EngineConfig:
    ingest = config.scoring.ingest.model_copy(update=ingest_changes)
    scoring = config.scoring.model_copy(update={"ingest": ingest})
    return config.model_copy(update={"scoring": scoring})


# ─── The catalogue ───────────────────────────────────────────────────────────────────────────


def test_every_fixture_has_an_expectation() -> None:
    on_disk = {p.name for p in EDGE.iterdir() if p.is_file() and p.suffix != ".py" and p.name != ".gitattributes"}
    assert on_disk == set(EXPECTED), "fixture directory and EXPECTED disagree"
    assert set(gen.fixture_names()) == set(EXPECTED), "generator and EXPECTED disagree"
    assert len(EXPECTED) >= 60
    assert sum((EDGE / name).stat().st_size for name in EXPECTED) < gen.MAX_TOTAL_BYTES


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_outcome(ingested: _Cache, name: str) -> None:
    expect = EXPECTED[name]
    result = ingested(name)
    assert result.ok is expect.ok, (result.reasons, result.text[:300])
    assert result.reasons == expect.reasons
    assert set(result.flags) == set(expect.flags)
    if expect.tier is not None:
        assert result.tier is expect.tier
    if expect.language != "?":
        assert result.language == expect.language
    if not result.ok:
        assert set(result.reasons) <= NEEDS_REVIEW_REASONS
    assert result.text == "\n".join(_lines(result))
    assert all(line.text and line.text == line.text.strip() for line in result.lines)


# ─── Row 1: type detection (magic bytes first, extension second) ─────────────────────────────


def test_magic_bytes_beat_the_extension(ingested: _Cache) -> None:
    as_txt = ingested("pdf_named_txt.txt")  # PDF bytes named .txt: parsed as a PDF
    assert as_txt.ok and "Data Analyst, Example Corp Jan 2020 - Present" in _lines(as_txt)
    assert as_txt.page_count == 1
    as_pdf = ingested("docx_named_pdf.pdf")  # DOCX bytes named .pdf: parsed as a DOCX
    assert as_pdf.ok and "- Built reporting pipelines in Python and SQL for the finance team" in _lines(as_pdf)
    # Bytes no magic recognises fall back to the extension: a ".pdf" that is not a PDF is damaged.
    assert ingested("text_named_pdf.pdf").reasons == (Flag.CORRUPT,)
    junk = ingested("pdf_junk_prefix.pdf")  # %PDF- within the first 1024 bytes still counts
    assert junk.ok and junk.lines[0].text == "Alex Example"


@pytest.mark.parametrize("name", ["text_utf8.txt", "text_utf8_bom.txt", "text_utf16le_bom.txt",
                                  "text_utf16be_bom.txt"])
def test_text_encodings_decode_identically(ingested: _Cache, name: str) -> None:
    result = ingested(name)
    lines = _lines(result)
    assert lines[0] == "Alex Example"  # no BOM residue
    assert "- Built reporting pipelines in Python and SQL for the finance team" in lines
    assert "\r" not in result.text and chr(0xFEFF) not in result.text
    reference = [line.replace("Montréal", "Toronto") for line in _lines(ingested("text_utf8.txt"))]
    assert lines == reference or lines == _lines(ingested("text_utf8.txt"))


def test_utf8_and_windows_1252(ingested: _Cache) -> None:
    assert "alex.example@example.com | 555-0100 | Montréal, ON" in _lines(ingested("text_utf8.txt"))
    french = ingested("text_cp1252_french.txt")
    lines = _lines(french)
    assert "Analyste de données avec quatre ans d'expérience dans la création de rapports." in lines
    assert "Analyste de données, Exemple Inc. janv. 2020 \u2013 aujourd'hui" in lines
    assert chr(0xFFFD) not in french.text


@pytest.mark.parametrize("name", ["random_bytes.bin", "text_binary_named.txt", "gif_image.gif", "png_corrupt.png",
                                  "zero_bytes.pdf"])
def test_unrecognised_or_empty_bytes_are_corrupt(ingested: _Cache, name: str) -> None:
    result = ingested(name)
    assert result.reasons == (Flag.CORRUPT,)
    assert result.text == "" and result.lines == ()
    assert result.page_count == 0


def test_detection_rules() -> None:
    from matcher.ingest.detect import Kind, decode_text, detect_kind

    assert detect_kind(b"BMW-certified technician, 2019 - 2023", "cv.txt") is Kind.TEXT  # words, not a bitmap
    assert detect_kind(b"BM\x36\x00\x00\x00\x00\x00\x00\x00\x36\x00\x00\x00", "") is Kind.UNKNOWN
    assert detect_kind(b"{\\rtf1\\ansi Alex Example}", "cv.rtf") is Kind.UNKNOWN
    assert detect_kind(b"PK\x03\x04 not really a zip", "cv.docx") is Kind.UNKNOWN
    assert detect_kind(b"plain words", "scan.JPG") is Kind.IMAGE  # the extension decides when no magic matches
    assert detect_kind(b"plain words", "notes") is Kind.TEXT
    assert decode_text("Résumé – café".encode("cp1252")) == "Résumé – café"
    assert decode_text(b"\xff\xfe" + "Ångström".encode("utf-16-le")) == "Ångström"
    assert decode_text(b"Alex\x00Example") is None
    assert decode_text(b"Alex Example, data analyst. " * 20 + b"\x07") is not None  # one stray control byte
    assert decode_text(b"Alex Example \x07\x07\x07 data") is None  # many: binary


def test_wrong_argument_types_never_raise(config: EngineConfig) -> None:
    assert ingest_bytes("not bytes", "x.txt", config=config).reasons == (Flag.INVALID_INPUT,)  # type: ignore[arg-type]
    assert ingest_bytes(None, config=config).reasons == (Flag.INVALID_INPUT,)  # type: ignore[arg-type]
    assert ingest_text(b"bytes", config=config).reasons == (Flag.INVALID_INPUT,)  # type: ignore[arg-type]
    accepted = ingest_bytes(bytearray((EDGE / "text_utf8.txt").read_bytes()), "cv.txt", config=config)
    assert accepted.ok


# ─── Row 2: encrypted, truncated, garbled ────────────────────────────────────────────────────


def test_encrypted_pdf_needs_review(ingested: _Cache) -> None:
    result = ingested("pdf_encrypted.pdf")
    assert result.reasons == (Flag.ENCRYPTED,) and result.text == ""


def test_owner_password_only_pdf_is_readable(ingested: _Cache) -> None:
    result = ingested("pdf_owner_password_only.pdf")
    assert result.ok and result.tier is A
    assert _lines(result) == _lines(ingested("pdf_clean_one_column.pdf"))


@pytest.mark.parametrize("name", ["pdf_truncated.pdf", "pdf_garbled.pdf", "docx_corrupt_zip.docx",
                                  "docx_zip_without_document.docx"])
def test_damaged_files_are_corrupt(ingested: _Cache, name: str) -> None:
    result = ingested(name)
    assert result.reasons == (Flag.CORRUPT,) and result.lines == ()


# ─── Row 3: hidden text is removed and flagged ───────────────────────────────────────────────


@pytest.mark.parametrize("name", ["pdf_white_text.pdf", "pdf_tiny_text.pdf", "pdf_invisible_render_mode.pdf",
                                  "pdf_clip_render_modes.pdf", "pdf_zero_opacity.pdf", "pdf_offpage_text.pdf"])
def test_hidden_pdf_text_is_removed_and_flagged(ingested: _Cache, name: str) -> None:
    result = ingested(name)
    assert STUFF in result.flags
    for word in HIDDEN + ["Rust", "Haskell", "Elixir", "Erlang"]:
        assert word not in result.text, word
    lines = _lines(result)
    assert "References available on request" in lines  # text drawn after the hidden text
    assert "- Built reporting pipelines in Python and SQL for the finance team" in lines


def test_fill_and_clip_text_is_read_once(ingested: _Cache) -> None:
    # Render mode 4 is visible; MuPDF's extra clip copy of it must neither repeat nor flag it.
    assert _lines(ingested("pdf_clip_render_modes.pdf")).count("Fluent in English and French") == 1


def test_fake_bold_double_strikes_are_read_once(ingested: _Cache) -> None:
    result = ingested("pdf_fake_bold.pdf")
    assert _lines(result).count("Data Analyst, Example Corp Jan 2020 - Present") == 1
    assert result.text.count("Example Corp") == 1


def test_visible_light_and_small_text_is_kept(ingested: _Cache) -> None:
    assert "Updated October 2026" in _lines(ingested("pdf_white_text.pdf"))  # grey 0.6 < 0.95
    assert "Portfolio at example.com/alex" in _lines(ingested("pdf_tiny_text.pdf"))  # 6 pt >= 2 pt


def test_white_text_on_a_dark_fill_is_visible(ingested: _Cache) -> None:
    result = ingested("pdf_canva_sidebar.pdf")
    assert STUFF not in result.flags
    for word in ("CONTACT", "alex.example@example.com", "Python", "Tableau", "LANGUAGES", "French"):
        assert word in result.text


def test_clean_documents_raise_no_stuffing_flag(ingested: _Cache) -> None:
    for name, expect in EXPECTED.items():
        if STUFF not in expect.flags:
            assert STUFF not in ingested(name).flags, name


# ─── Row 4: columns and reading order ────────────────────────────────────────────────────────


def _order(text: str, *phrases: str) -> list[int]:
    positions = [text.index(p) for p in phrases]
    assert positions == sorted(positions), list(zip(phrases, positions))
    return positions


def test_single_column_is_tier_a_column_zero(ingested: _Cache) -> None:
    result = ingested("pdf_clean_one_column.pdf")
    assert result.tier is A and {line.column for line in result.lines} == {0}
    assert _lines(result)[:4] == ["Alex Example", "alex.example@example.com | 555-0100 | Toronto, ON", "SUMMARY",
                                  "Data analyst with four years of experience building reporting pipelines."]
    assert "Data Analyst, Example Corp Jan 2020 - Present" in _lines(result)


def test_two_columns_read_column_by_column(ingested: _Cache) -> None:
    result = ingested("pdf_two_column.pdf")
    text = result.text
    _order(text, "Alex Example", "open to relocation", "EXPERIENCE", "documented every validation rule",
           "EDUCATION", "English and French")
    lines = _lines(result)
    first = lines.index("- Built reporting pipelines in Python and SQL for the")
    assert lines[first + 1] == "finance team and two regional offices"
    column = {line.text: line.column for line in result.lines}
    assert column["EXPERIENCE"] == 0 and column["EDUCATION"] == 1 and column["English and French"] == 1
    assert Flag.COLUMN_ORDER_UNCERTAIN not in result.flags


def test_three_columns_read_left_to_right(ingested: _Cache) -> None:
    result = ingested("pdf_three_column.pdf")
    _order(result.text, "PROFILE", "reports for the finance team.", "555-0100, Toronto", "EXPERIENCE",
           "Tableau for regional managers", "EDUCATION", "Statistics and Forecasting")
    column = {line.text: line.column for line in result.lines}
    assert (column["PROFILE"], column["EXPERIENCE"], column["EDUCATION"]) == (0, 1, 2)
    lines = _lines(result)
    assert lines[lines.index("- Built pipelines in Python for") + 1] == "the finance team and auditors"


def test_canva_sidebar_is_read_before_the_main_column(ingested: _Cache) -> None:
    result = ingested("pdf_canva_sidebar.pdf")
    _order(result.text, "CONTACT", "SKILLS", "French", "Alex Example", "PROFILE", "EXPERIENCE", "EDUCATION")
    lines = _lines(result)
    # Bullet glyph and text are separate drawing operations; they join into one line.
    assert "- Built reporting pipelines in Python and SQL" in lines
    assert "Data Analyst, Example Corp Jan 2020 - Present" in lines


def test_uncertain_reading_order_downgrades_to_tier_b(ingested: _Cache) -> None:
    result = ingested("pdf_column_order_uncertain.pdf")
    assert result.ok and result.tier is B and Flag.COLUMN_ORDER_UNCERTAIN in result.flags
    assert "- Designed the ingestion layer for" in _lines(result)


def test_title_and_right_aligned_dates_stay_on_one_line(ingested: _Cache) -> None:
    # A narrow right-aligned group that shares rows with the left text is not a column.
    result = ingested("pdf_right_aligned_dates.pdf")
    lines = _lines(result)
    for row in ("Data Analyst, Example Corp Jan 2020 - Present",
                "Junior Analyst, Sample Industries Jun 2018 - Dec 2019",
                "Intern, Example Lab May 2017 - Aug 2017", "BSc Statistics, Example University 2014 - 2018"):
        assert row in lines, lines
    assert {line.column for line in result.lines} == {0} and result.tier is A


# ─── Row 5: repeated headers, footers and page numbers ───────────────────────────────────────


def test_repeated_header_and_page_x_of_y_footer_are_removed(ingested: _Cache) -> None:
    result = ingested("pdf_header_footer.pdf")
    assert result.page_count == 3
    assert "Curriculum Vitae" not in result.text and "Page " not in result.text
    _order(result.text, "Alex Example", "Junior Analyst", "Example Certified Data Analyst (2021)")
    assert {line.page for line in result.lines} == {0, 1, 2}


@pytest.mark.parametrize("name", ["pdf_page_numbers_plain.pdf", "pdf_page_numbers_dashed.pdf"])
def test_bare_page_numbers_are_removed(ingested: _Cache, name: str) -> None:
    lines = _lines(ingested(name))
    assert not any(line.strip("- ").isdigit() for line in lines), lines
    assert "Example Certified Data Analyst (2021)" in lines


def test_long_document_footers_are_removed(ingested: _Cache) -> None:
    result = ingested("pdf_90_pages.pdf")
    assert " of 90" not in result.text


# ─── Row 6: DOCX ─────────────────────────────────────────────────────────────────────────────


def test_docx_paragraphs_and_list_bullets(ingested: _Cache) -> None:
    result = ingested("docx_clean.docx")
    assert result.page_count == 1 and result.tier is A
    assert _lines(result) == [
        "Alex Example", "alex.example@example.com | 555-0100 | Toronto, ON", "EXPERIENCE",
        "Data Analyst, Example Corp Jan 2020 - Present",
        "- Built reporting pipelines in Python and SQL for the finance team",
        "- Automated weekly dashboards in Tableau, saving six hours per week", "EDUCATION",
        "BSc Statistics, Example University 2014 - 2018", "SKILLS", "Python, SQL, Tableau, Excel, Statistics",
    ]


def test_docx_table_rows_join_cells_in_order(ingested: _Cache) -> None:
    lines = _lines(ingested("docx_tables.docx"))
    assert "Data Analyst Example Corp Jan 2020 - Present" in lines
    assert "Junior Analyst Sample Industries Jun 2018 - Dec 2019" in lines
    start = lines.index("SKILLS")  # a layout table: each cell's paragraphs, cell after cell
    assert lines[start:start + 6] == ["SKILLS", "Python, SQL, Tableau", "Excel and Statistics", "EDUCATION",
                                      "BSc Statistics, Example University", "2014 - 2018"]


def test_docx_text_boxes_are_read_once(ingested: _Cache) -> None:
    anchored = ingested("docx_text_boxes.docx")
    assert anchored.text.count("Example Certified Data Analyst") == 1  # not again from mc:Fallback
    assert anchored.tier is B and Flag.COLUMN_ORDER_UNCERTAIN in anchored.flags
    inline = ingested("docx_text_box_inline.docx")
    assert inline.text.count("Example Certified Data Analyst") == 1
    assert inline.tier is A and not inline.flags
    lines = _lines(inline)
    assert lines.index("English and French") < lines.index("EXPERIENCE")  # at its anchor


def test_docx_header_and_footer_are_read_once(ingested: _Cache) -> None:
    result = ingested("docx_header_footer.docx")
    lines = _lines(result)
    assert lines[0] == "Alex Example | alex.example@example.com | 555-0100"
    assert lines[-1] == "Portfolio: example.com/alex"
    assert result.text.count("555-0100") == 1
    assert "Page" not in result.text  # the PAGE field line is a page number


@pytest.mark.parametrize("name", ["docx_hidden_text.docx", "docx_white_text.docx", "docx_tiny_text.docx"])
def test_docx_hidden_runs_are_removed_and_flagged(ingested: _Cache, name: str) -> None:
    result = ingested(name)
    assert STUFF in result.flags
    for word in ("Kubernetes", "Terraform", "Golang", "Scala"):
        assert word not in result.text
    assert any(line.startswith("References available on request") for line in _lines(result))


def test_docx_visible_variants_are_kept(ingested: _Cache) -> None:
    assert "References available on request. Updated October 2026" in _lines(ingested("docx_white_text.docx"))
    assert "References available on request. Portfolio at example.com/alex" in _lines(ingested("docx_tiny_text.docx"))
    dark = ingested("docx_white_on_dark.docx")
    assert _lines(dark)[:2] == ["Alex Example - Data Analyst", "alex.example@example.com | 555-0100"]
    assert STUFF not in dark.flags


# ─── Row 7: OCR ──────────────────────────────────────────────────────────────────────────────


def test_scanned_pdf_is_ocred_at_tier_c(ingested: _Cache, config: EngineConfig) -> None:
    result = ingested("scan_300dpi.pdf")
    assert result.ok and result.tier is C and OCR in result.flags
    assert result.ocr_confidence is not None
    assert result.ocr_confidence >= config.scoring.ingest.ocr_min_confidence
    for word in ("EXPERIENCE", "EDUCATION", "SKILLS", "Example Corp", "Tableau"):
        assert word in result.text, word


def test_noisy_scan_is_unreadable(ingested: _Cache, config: EngineConfig) -> None:
    result = ingested("scan_noisy_72dpi.pdf")
    assert result.reasons == (Flag.UNREADABLE,)
    assert result.ocr_confidence is not None and result.ocr_confidence < config.scoring.ingest.ocr_min_confidence
    assert result.text == "" and result.lines == ()


def test_image_file_is_ocred(ingested: _Cache) -> None:
    result = ingested("image_resume.png")
    assert result.ok and result.tier is C and result.page_count == 1
    assert "EXPERIENCE" in result.text and "Python" in result.text


def test_image_without_text_is_not_a_resume(ingested: _Cache) -> None:
    result = ingested("photo_no_text.jpg")
    assert result.reasons == (Flag.NOT_A_RESUME,) and result.ocr_confidence is None


def test_searchable_scan_text_layer_is_not_stuffing(ingested: _Cache) -> None:
    result = ingested("pdf_searchable_scan.pdf")
    assert result.ok and OCR in result.flags and STUFF not in result.flags
    assert "EXPERIENCE" in result.text


def test_blank_pdf_is_not_a_resume(ingested: _Cache) -> None:
    result = ingested("pdf_blank_page.pdf")
    assert result.reasons == (Flag.NOT_A_RESUME,) and result.text == ""


@pytest.mark.parametrize("name", ["scan_300dpi.pdf", "image_resume.png", "photo_no_text.jpg"])
def test_ocr_unavailable_is_unreadable(config: EngineConfig, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    import matcher.ingest.ocr as ocr

    monkeypatch.setattr(ocr, "get_engine", lambda: None)
    result = ingest_bytes((EDGE / name).read_bytes(), name, config=config)
    assert result.reasons == (Flag.UNREADABLE,) and result.ocr_confidence is None


# ─── Row 8: page cap ─────────────────────────────────────────────────────────────────────────


def test_ninety_page_cv_is_read_in_full(ingested: _Cache) -> None:
    result = ingested("pdf_90_pages.pdf")
    assert result.ok and result.page_count == 90 and Flag.TRUNCATED not in result.flags
    assert "project 090" in result.text and "PhD Statistics" in result.text
    assert max(line.page for line in result.lines) == 89


def test_pages_beyond_max_pages_are_not_read(ingested: _Cache, config: EngineConfig) -> None:
    assert config.scoring.ingest.max_pages == 100
    result = ingested("pdf_120_pages.pdf")
    assert result.page_count == 120 and Flag.TRUNCATED in result.flags
    assert "project 100" in result.text and "project 101" not in result.text
    assert max(line.page for line in result.lines) == 99


def test_max_pages_comes_from_config(config: EngineConfig) -> None:
    capped = _with(config, max_pages=2)
    result = ingest_bytes((EDGE / "pdf_header_footer.pdf").read_bytes(), config=capped)
    assert Flag.TRUNCATED in result.flags and "CERTIFICATIONS" not in result.text
    assert "BSc Statistics" in result.text


# ─── Row 9: normalisation through real files ─────────────────────────────────────────────────


def test_ligatures_are_expanded(ingested: _Cache) -> None:
    lines = _lines(ingested("pdf_ligatures.pdf"))
    assert "Office Manager, Example Corp 2019 - 2023" in lines
    assert "- Reduced workflow effort for finance staff" in lines
    assert "Scheduling, filing, budgeting" in lines


def test_zero_width_characters_are_removed(ingested: _Cache) -> None:
    result = ingested("pdf_zero_width.pdf")
    assert "- Built pipelines in Python and SQL on Kubernetes" in _lines(result)
    assert "Tableau, Excel, Python" in _lines(result)
    assert not any(ch in result.text for ch in (gen.ZWSP, gen.ZWNJ, gen.ZWJ, gen.WJ, gen.BOM))


def test_soft_hyphens_are_removed_and_joined(ingested: _Cache) -> None:
    lines = _lines(ingested("pdf_soft_hyphens.pdf"))
    assert "Experienced analyst responsible for the development" in lines
    assert "of reporting tools for the finance team." in lines
    assert not any(gen.SHY in line for line in lines)


def test_words_hyphenated_across_lines_are_joined(ingested: _Cache) -> None:
    lines = _lines(ingested("pdf_hyphenated_lines.pdf"))
    assert "- Led the development" in lines
    assert "of internal reporting tools for the finance team" in lines
    assert "- Self-taught in statistics and data visualization" in lines  # a real hyphen stays


def test_smart_quotes_become_straight(ingested: _Cache) -> None:
    lines = _lines(ingested("pdf_smart_quotes.pdf"))
    assert "- Awarded \"Analyst of the Year\" for the 'Clean Data' project" in lines
    assert "- Rebuilt the team's reporting pipeline in Python" in lines


def test_bullet_glyphs_become_dashes(ingested: _Cache) -> None:
    lines = _lines(ingested("pdf_bullet_glyphs.pdf"))
    for item in ("Python", "SQL", "Tableau", "Excel", "Statistics", "Reporting", "Dashboards", "Forecasting",
                 "Budgeting"):
        assert f"- {item}" in lines, item


# ─── Row 10: language ────────────────────────────────────────────────────────────────────────


def test_supported_languages() -> None:
    assert SUPPORTED_LANGUAGES == ("en", "fr")


def test_french_resume_is_supported(ingested: _Cache) -> None:
    result = ingested("pdf_french_resume.pdf")
    assert result.ok and result.language == "fr" and result.languages["fr"] >= 0.8


@pytest.mark.parametrize("name,code", [("pdf_spanish_resume.pdf", "es"), ("pdf_german_resume.pdf", "de")])
def test_other_latin_languages_are_unsupported(ingested: _Cache, name: str, code: str) -> None:
    result = ingested(name)
    assert result.reasons == (Flag.UNSUPPORTED_LANGUAGE,) and result.language == code
    assert result.text  # the text is still returned for the reviewer


def test_mixed_language_is_flagged_not_rejected(ingested: _Cache) -> None:
    result = ingested("pdf_mixed_fr_en.pdf")
    assert result.ok and Flag.MIXED_LANGUAGE in result.flags
    assert result.languages["fr"] >= 0.2 and result.languages["en"] >= 0.2
    assert abs(sum(result.languages.values()) - 1.0) < 1e-6


def test_rtl_resumes_extract_in_logical_order_then_route_to_review(ingested: _Cache) -> None:
    import unicodedata

    arabic = ingested("pdf_arabic_resume.pdf")
    assert arabic.reasons == (Flag.UNSUPPORTED_LANGUAGE,) and arabic.language == "ar"
    assert unicodedata.normalize("NFKC", "مهندس برمجيات") in arabic.text
    assert "تطوير تطبيقات" in arabic.text
    hebrew = ingested("pdf_hebrew_resume.pdf")  # stored in visual order in the PDF
    assert hebrew.reasons == (Flag.UNSUPPORTED_LANGUAGE,) and hebrew.language == "he"
    for line in gen.HEBREW_LINES:
        assert line in _lines(hebrew), line


def test_visual_order_rtl_text_is_put_back_in_logical_order() -> None:
    from matcher.ingest.pdf import is_rtl, visual_to_logical

    logical = "מהנדס תוכנה 2019 Python"
    visual = "Python 2019 " + "תוכנה"[::-1] + " " + "מהנדס"[::-1]  # how a left-to-right reader stores it
    assert is_rtl(logical) and not is_rtl("Python 2019")
    assert visual_to_logical(visual) == logical


# ─── Row 11: résumé likelihood ───────────────────────────────────────────────────────────────


def test_wrong_documents_are_not_resumes(ingested: _Cache, config: EngineConfig) -> None:
    threshold = config.scoring.ingest.resume_likelihood_threshold
    assert threshold == 0.5
    for name in ("pdf_cover_letter.pdf", "pdf_transcript.pdf"):
        result = ingested(name)
        assert result.reasons == (Flag.NOT_A_RESUME,) and result.resume_likelihood < threshold, name
        assert result.text  # kept for the reviewer


def test_two_line_resume_is_accepted(ingested: _Cache, config: EngineConfig) -> None:
    for name in ("pdf_two_line_resume.pdf", "text_two_line_resume.txt"):
        result = ingested(name)
        assert result.ok and result.resume_likelihood >= config.scoring.ingest.resume_likelihood_threshold
        assert len(result.lines) == 2


def test_resume_likelihood_signals() -> None:
    resume = (ENGINE_ROOT / "tests" / "fixtures" / "edge" / "text_utf8.txt").read_text(encoding="utf-8")
    assert resume_likelihood(resume) >= 0.9
    assert resume_likelihood("") < 0.5
    prose = "The river ran past the old mill and the children watched the boats go by in the evening."
    assert resume_likelihood(prose) < 0.5
    cover = "Dear Hiring Manager,\nI am writing to apply for the analyst role.\nSincerely,\nAlex Example"
    assert resume_likelihood(cover) < 0.1
    transcript = "Official Transcript\nCourse Code Course Title Credits Grade\nTerm GPA: 3.8\nCumulative GPA: 3.7"
    assert resume_likelihood(transcript) < 0.1
    # Each family of signal moves the score the way the brief says.
    base = "Alex Example\nData analyst"
    assert resume_likelihood(base + "\nEXPERIENCE\nEDUCATION\nSKILLS") > resume_likelihood(base)
    assert resume_likelihood(base + "\nAnalyst 2019 - 2021") > resume_likelihood(base)
    assert resume_likelihood(base + "\n- Built reports\n- Cleaned data") > resume_likelihood(base)
    assert resume_likelihood(resume + "\nDear Hiring Manager,") < resume_likelihood(resume)
    french = "EXPÉRIENCE PROFESSIONNELLE\nFORMATION\nCOMPÉTENCES\nAnalyste 2019 - aujourd'hui"
    assert resume_likelihood(french) >= 0.9
    assert 0.0 <= resume_likelihood("x" * 10000) <= 1.0


def test_threshold_comes_from_config(config: EngineConfig) -> None:
    strict = _with(config, resume_likelihood_threshold=0.999)
    result = ingest_bytes((EDGE / "text_two_line_resume.txt").read_bytes(), "cv.txt", config=strict)
    assert result.reasons == (Flag.NOT_A_RESUME,)


# ─── Row 12: totality and determinism ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_same_bytes_twice_give_identical_results(ingested: _Cache, config: EngineConfig, name: str) -> None:
    first = ingested(name)
    second = ingest_bytes((EDGE / name).read_bytes(), name, config=config)
    assert first == second
    assert canonical_json(first) == canonical_json(second)


_PREFIXES = [b"", b"%PDF-1.7\n", b"PK\x03\x04", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff\xe0", b"\xef\xbb\xbf",
             b"\xff\xfe", b"\xfe\xff", b"GIF89a"]
_NAMES = ["", "resume.pdf", "resume.docx", "cv.txt", "scan.png", "photo.jpg", "weird.bin"]


@settings(max_examples=150)
@given(st.sampled_from(_PREFIXES), st.binary(max_size=1500), st.sampled_from(_NAMES))
def test_arbitrary_bytes_never_raise(config: EngineConfig, prefix: bytes, body: bytes, filename: str) -> None:
    data = prefix + body
    first = ingest_bytes(data, filename, config=config)
    assert isinstance(first, IngestResult)
    assert first.ok == (not first.reasons)
    assert first == ingest_bytes(data, filename, config=config)


@settings(max_examples=150)
@given(st.text(max_size=1500))
def test_arbitrary_text_never_raises(config: EngineConfig, text: str) -> None:
    first = ingest_text(text, config=config)
    assert isinstance(first, IngestResult) and first.tier is A
    assert first.ok == (not first.reasons)
    assert first == ingest_text(text, config=config)
    assert 0.0 <= first.resume_likelihood <= 1.0


def test_ingest_text_matches_the_text_file(ingested: _Cache, config: EngineConfig) -> None:
    from_text = ingest_text((EDGE / "text_utf8.txt").read_text(encoding="utf-8"), config=config)
    assert from_text == ingested("text_utf8.txt")


def test_empty_text_is_not_a_resume(config: EngineConfig) -> None:
    for text in ("", "   \n\t\n"):
        result = ingest_text(text, config=config)
        assert result.reasons == (Flag.NOT_A_RESUME,) and result.lines == ()


# ─── Contracts ───────────────────────────────────────────────────────────────────────────────


def test_result_model_enforces_ok_iff_no_reasons() -> None:
    common = dict(text="", lines=(), tier=A, language=None, languages={}, flags=(), page_count=0,
                  ocr_confidence=None, resume_likelihood=0.0)
    with pytest.raises(ValueError):
        IngestResult(ok=True, reasons=(Flag.CORRUPT,), **common)
    with pytest.raises(ValueError):
        IngestResult(ok=False, reasons=(), **common)
    line = TextLine(text="x", page=0, x0=0, y0=0, x1=1, y1=1)
    assert (line.size, line.bold, line.column) == (0.0, False, 0)


def test_importing_ingest_does_not_import_heavy_libraries() -> None:
    """DECISIONS D-02: pdfplumber, fitz, docx, rapidocr (and numpy, PIL) load only when needed."""
    code = (
        "import sys\n"
        "import matcher.ingest as m\n"
        "from matcher.config import load_config\n"
        "r = m.ingest_text('Alex Example\\nEXPERIENCE\\nAnalyst 2019 - 2021', config=load_config())\n"
        "heavy = ('fitz', 'pymupdf', 'pdfplumber', 'pdfminer', 'docx', 'rapidocr_onnxruntime', 'onnxruntime',\n"
        "         'cv2', 'numpy', 'PIL')\n"
        "print(sorted(name for name in heavy if name in sys.modules))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(ENGINE_ROOT / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ENGINE_ROOT,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ─── pdfplumber fallback (PyMuPDF missing) ───────────────────────────────────────────────────


def _without_pymupdf(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make PyMuPDF look uninstalled; returns a list that grows by one per pdfplumber read."""
    import matcher.ingest.pdf as pdf

    def missing():
        raise ImportError("PyMuPDF is not installed")

    calls: list[int] = []
    real = pdf._extract_with_pdfplumber

    def spy(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(pdf, "_import_fitz", missing)
    monkeypatch.setattr(pdf, "_extract_with_pdfplumber", spy)
    return calls


def test_pdfplumber_fallback_reads_text_and_columns(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    data = (EDGE / "pdf_clean_one_column.pdf").read_bytes()
    reference = ingest_bytes(data, config=config)  # PyMuPDF, before it is "uninstalled"
    calls = _without_pymupdf(monkeypatch)
    clean = ingest_bytes(data, config=config)
    assert calls and clean.ok and clean.tier is A
    assert _lines(clean) == _lines(reference)
    two = ingest_bytes((EDGE / "pdf_two_column.pdf").read_bytes(), config=config)
    _order(two.text, "EXPERIENCE", "documented every validation rule", "EDUCATION", "English and French")


def test_pdfplumber_fallback_strips_hidden_text(config: EngineConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _without_pymupdf(monkeypatch)
    for name in ("pdf_white_text.pdf", "pdf_tiny_text.pdf", "pdf_offpage_text.pdf"):
        result = ingest_bytes((EDGE / name).read_bytes(), config=config)
        assert STUFF in result.flags and "Kubernetes" not in result.text, name
    sidebar = ingest_bytes((EDGE / "pdf_canva_sidebar.pdf").read_bytes(), config=config)
    assert STUFF not in sidebar.flags and "LANGUAGES" in sidebar.text
    assert ingest_bytes((EDGE / "pdf_encrypted.pdf").read_bytes(), config=config).reasons == (Flag.ENCRYPTED,)
    assert len(calls) == 5  # every PDF above went through pdfplumber
    hebrew = ingest_bytes((EDGE / "pdf_hebrew_resume.pdf").read_bytes(), config=config)  # visual order on disk
    for line in gen.HEBREW_LINES:
        assert line in _lines(hebrew), line
    # The truncation check runs before either reader is chosen.
    assert ingest_bytes((EDGE / "pdf_truncated.pdf").read_bytes(), config=config).reasons == (Flag.CORRUPT,)


# ─── The generator itself ────────────────────────────────────────────────────────────────────


@pytest.mark.slow
def test_fixture_generator_is_deterministic_and_current() -> None:
    first, second = gen.build_all(), gen.build_all()
    assert first == second, "make_edge_fixtures.py is not a pure function"
    stale = [name for name, data in first.items() if (EDGE / name).read_bytes() != data]
    assert not stale, f"committed fixtures differ from the generator; regenerate: {stale}"
