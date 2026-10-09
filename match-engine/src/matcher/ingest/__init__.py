"""Ingest (SPEC §14.1): bytes in; normalised text and layout lines, a parse tier, a language and
flags out, or NEEDS_REVIEW with a reason. It never raises (SPEC §13): every failure is an
IngestResult with ok=False and its reasons.

Pipeline: detect the type (detect.py) -> extract (pdf.py, docx.py, ocr.py; plain text decoded) ->
normalise each line and join words hyphenated across lines (normalize_text.py) -> language
(language.py) -> résumé likelihood (quality.py) -> outcome.

Outcome rules, in order:
1. extraction failed: CORRUPT (damaged or unrecognised bytes, zero bytes), ENCRYPTED (a PDF that
   needs a password), UNREADABLE (a scan or image OCR cannot read: unavailable, or mean line
   confidence below `ingest.ocr_min_confidence`), INVALID_INPUT (not bytes / not text);
2. no text at all: NOT_A_RESUME (a blank page, a photo, nothing but hidden text);
3. the majority language is not supported: UNSUPPORTED_LANGUAGE (D-70; RTL text included);
4. résumé likelihood below `ingest.resume_likelihood_threshold`: NOT_A_RESUME.
The text and lines are still returned for outcomes 3 and 4 (a reviewer reads them).

Tier: C when any text came from OCR, else B when the reading order is uncertain (columns that fail
the continuity check, floating DOCX text boxes), else A. Informational flags: TRUNCATED,
STUFFING_SUSPECTED, OCR_USED, COLUMN_ORDER_UNCERTAIN, MIXED_LANGUAGE (two supported languages
each holding at least 20 % of the lines).

Lines: one TextLine per non-empty line in reading order; `text` is exactly their texts joined by
newlines. Coordinates are PDF points from the page's top-left (OCR boxes are scaled to points);
plain text and DOCX have no geometry, so their coordinates are 0 and their page is 0.
`page_count` is the number of pages in the PDF (all of them, also those beyond max_pages), 1 for
plain text, DOCX and images, 0 when the file could not be opened.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional, Sequence

from pydantic import Field, model_validator

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.ingest import detect, docx, ocr, pdf
from matcher.ingest.language import SUPPORTED_LANGUAGES, detect_language, language_profile
from matcher.ingest.normalize_text import normalize_lines, normalize_text, split_lines
from matcher.ingest.quality import resume_likelihood
from matcher.models import Frozen, ParseTier

__all__ = [
    "SUPPORTED_LANGUAGES",
    "IngestResult",
    "TextLine",
    "detect_language",
    "ingest_bytes",
    "ingest_text",
    "normalize_text",
    "resume_likelihood",
]

MIXED_LANGUAGE_SHARE = 0.20  # brief row 10
_logger = logging.getLogger(__name__)


class TextLine(Frozen):
    text: str
    page: int = Field(ge=0)  # 0-based page index
    x0: float
    y0: float
    x1: float
    y1: float
    size: float = Field(default=0.0, ge=0.0)  # font size in points (OCR: box height in points)
    bold: bool = False
    column: int = Field(default=0, ge=0)  # column index on its page, left to right


class IngestResult(Frozen):
    ok: bool
    text: str
    lines: tuple[TextLine, ...]
    tier: ParseTier
    language: Optional[str]  # ISO 639-1 of the majority of lines; None when undetermined
    languages: dict[str, float]  # share of lines per detected language
    flags: tuple[Flag, ...]
    reasons: tuple[Flag, ...]  # non-empty exactly when ok is False
    page_count: int = Field(ge=0)
    ocr_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # mean OCR line confidence
    resume_likelihood: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ok_iff_no_reasons(self) -> "IngestResult":
        if self.ok == bool(self.reasons):
            raise ValueError("an IngestResult has reasons exactly when it is not ok")
        return self


def _ordered(flags: set[Flag]) -> tuple[Flag, ...]:
    return tuple(flag for flag in Flag if flag in flags)


def _failed(reason: Flag, *, page_count: int = 0, flags: set[Flag] | None = None,
            ocr_confidence: Optional[float] = None, tier: ParseTier = ParseTier.C) -> IngestResult:
    return IngestResult(ok=False, text="", lines=(), tier=tier, language=None, languages={},
                        flags=_ordered(flags or set()), reasons=(reason,), page_count=page_count,
                        ocr_confidence=ocr_confidence, resume_likelihood=0.0)


def _finite(value: Any) -> float:
    number = float(value)
    return round(number, 2) if math.isfinite(number) else 0.0


def _result(raw: Sequence[Any], *, flags: set[Flag], page_count: int, ocr_confidence: Optional[float],
            config: EngineConfig, positioned: bool) -> IngestResult:
    """Normalise the extracted lines and decide the outcome (rules 2-4)."""
    texts = normalize_lines([line.text for line in raw])
    lines = []
    for line, text in zip(raw, texts):
        if not text:
            continue
        if positioned:
            lines.append(TextLine(text=text, page=line.page, x0=_finite(line.x0), y0=_finite(line.y0),
                                  x1=_finite(line.x1), y1=_finite(line.y1), size=max(0.0, _finite(line.size)),
                                  bold=bool(line.bold), column=line.column))
        else:
            lines.append(TextLine(text=text, page=0, x0=0.0, y0=0.0, x1=0.0, y1=0.0,
                                  size=max(0.0, _finite(getattr(line, "size", 0.0))),
                                  bold=bool(getattr(line, "bold", False))))
    text = "\n".join(line.text for line in lines)
    language, _, shares = language_profile(text)
    flags = set(flags)
    if sum(1 for code in SUPPORTED_LANGUAGES if shares.get(code, 0.0) >= MIXED_LANGUAGE_SHARE) >= 2:
        flags.add(Flag.MIXED_LANGUAGE)
    likelihood = resume_likelihood(text)
    if Flag.OCR_USED in flags:
        tier = ParseTier.C
    elif Flag.COLUMN_ORDER_UNCERTAIN in flags:
        tier = ParseTier.B
    else:
        tier = ParseTier.A
    if not lines:
        reason: Optional[Flag] = Flag.NOT_A_RESUME
    elif language is not None and language not in SUPPORTED_LANGUAGES:
        reason = Flag.UNSUPPORTED_LANGUAGE
    elif likelihood < config.scoring.ingest.resume_likelihood_threshold:
        reason = Flag.NOT_A_RESUME
    else:
        reason = None
    return IngestResult(ok=reason is None, text=text, lines=tuple(lines), tier=tier, language=language,
                        languages=shares, flags=_ordered(flags), reasons=(reason,) if reason else (),
                        page_count=page_count, ocr_confidence=ocr_confidence, resume_likelihood=likelihood)


class _PlainLine:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def _from_text(text: str, config: EngineConfig) -> IngestResult:
    return _result([_PlainLine(t) for t in split_lines(text)], flags=set(), page_count=1, ocr_confidence=None,
                   config=config, positioned=False)


def _from_pdf(data: bytes, config: EngineConfig) -> IngestResult:
    ingest = config.scoring.ingest
    extracted = pdf.extract_pdf(data, max_pages=ingest.max_pages, ocr_min_confidence=ingest.ocr_min_confidence)
    if extracted.failure is not None:
        return _failed(extracted.failure, page_count=extracted.page_count, flags=extracted.flags,
                       ocr_confidence=extracted.ocr_confidence)
    return _result(extracted.lines, flags=extracted.flags, page_count=extracted.page_count,
                   ocr_confidence=extracted.ocr_confidence, config=config, positioned=True)


def _from_docx(data: bytes, config: EngineConfig) -> IngestResult:
    extracted = docx.extract_docx(data)
    if extracted.failure is not None:
        return _failed(extracted.failure)
    return _result(extracted.lines, flags=extracted.flags, page_count=1, ocr_confidence=None, config=config,
                   positioned=False)


def _from_image(data: bytes, config: EngineConfig) -> IngestResult:
    try:
        array, points_per_pixel = ocr.image_to_array(data)
    except Exception as exc:  # not decodable: damaged, truncated, a bomb
        if detect.internal_fault(exc):
            raise
        return _failed(Flag.CORRUPT)
    lines = ocr.run_ocr(array)
    if lines is None:
        return _failed(Flag.UNREADABLE, page_count=1)
    flags = {Flag.OCR_USED}
    confidence = round(math.fsum(line.confidence for line in lines) / len(lines), 4) if lines else None
    if confidence is not None and confidence < config.scoring.ingest.ocr_min_confidence:
        return _failed(Flag.UNREADABLE, page_count=1, flags=flags, ocr_confidence=confidence)
    height, width = array.shape[0], array.shape[1]
    page = pdf.PageLayout(width * points_per_pixel, height * points_per_pixel,
                          pdf.fragments_from_ocr(lines, 0, points_per_pixel))
    placed, uncertain = pdf.layout_document([page])
    if uncertain:
        flags.add(Flag.COLUMN_ORDER_UNCERTAIN)
    return _result(placed, flags=flags, page_count=1, ocr_confidence=confidence, config=config, positioned=True)


def _ingest(data: bytes, filename: str, config: EngineConfig) -> IngestResult:
    if not data:
        return _failed(Flag.CORRUPT)
    kind = detect.detect_kind(data, filename)
    if kind is detect.Kind.PDF:
        return _from_pdf(data, config)
    if kind is detect.Kind.DOCX:
        return _from_docx(data, config)
    if kind is detect.Kind.IMAGE:
        return _from_image(data, config)
    if kind is detect.Kind.TEXT:
        text = detect.decode_text(data)
        return _failed(Flag.CORRUPT) if text is None else _from_text(text, config)
    return _failed(Flag.CORRUPT)


def ingest_bytes(data: bytes, filename: str = "", *, config: EngineConfig) -> IngestResult:
    """Read a résumé file. Magic bytes decide the type, the extension of `filename` breaks ties."""
    try:
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, bytes):
            return _failed(Flag.INVALID_INPUT)
        return _ingest(data, filename if isinstance(filename, str) else "", config)
    except Exception:  # SPEC §13: nothing escapes; an engine defect is reported, not raised
        size = len(data) if isinstance(data, (bytes, bytearray, memoryview)) else -1
        name = filename if isinstance(filename, str) else "?"
        _logger.warning("ingest_bytes: internal error (%d bytes, %r)", size, name, exc_info=True)
        return _failed(Flag.INTERNAL_ERROR)


def ingest_text(text: str, *, config: EngineConfig) -> IngestResult:
    """Read résumé text that is already decoded (pasted text, a stored raw_text)."""
    try:
        if not isinstance(text, str):
            return _failed(Flag.INVALID_INPUT)
        return _from_text(text, config)
    except Exception:
        _logger.warning("ingest_text: internal error (%d characters)", len(text), exc_info=True)
        return _failed(Flag.INTERNAL_ERROR)
