"""PDF reading (SPEC §14.1; brief rows 2-5, 7, 8): text with coordinates, hidden text removed,
scanned pages OCRed, the page layout resolved into reading-order lines.

Readers. PyMuPDF first: `page.get_texttrace()` decides visibility span by span, as the brief lists
it: invisible render mode (type 3), opacity 0, a near-white colour (every channel >= 0.95) over a
light background, a font size under 2 pt, a glyph centred outside the page box. Glyph text and
positions come from `page.get_text("rawdict")`, matched to the trace by (code point, origin). A
glyph rawdict reports as unpainted (alpha 0, neither filled nor stroked) is hidden as well unless
the trace has a painted copy at the same origin: clip-only text (render mode 7) never reaches the
trace, so this is the only place it shows. White text on a dark fill (a Canva sidebar) is visible.
pdfplumber is the fallback when PyMuPDF is missing, with the same colour, size and off-page rules
(it exposes neither render mode nor opacity).

Hidden glyphs other than spaces flag STUFFING_SUSPECTED, except a scanner's OCR layer: invisible
text on a page with no visible text whose images cover at least half of it.

Image pages. A page with no visible text is OCRed when anything is drawn on it (ocr.py, 300 dpi).
When every page read is an image page the document is a scan: OCR unavailable, or a mean line
confidence below `ingest.ocr_min_confidence`, makes it UNREADABLE. In a document that also has a
text layer, an image page that OCR cannot read (unavailable, or below the threshold) is skipped.
More than `ingest.max_pages` pages: only the first ones are read (TRUNCATED). A file without
"%%EOF" in its last 8 KB is truncated (CORRUPT), even though PyMuPDF could "repair" it.

Damage. MuPDF's messages are read page by page (`_quiet_mupdf` keeps them off stdout). Decoding
trouble on a page ("zlib error", "premature end of data", ...) makes the file CORRUPT only when
the page lost content (`_page_lost`): nothing readable came out of it, or one of its content
streams is itself damaged and shows a loss (decoding stopped early, deflate data invalid or cut
short, garbage reaching the interpreter). A bad or missing checksum, or a damaged embedded font
that MuPDF replaces, leaves every line readable and is not damage.

Layout (also used for OCR lines and image files):
- headers/footers: a line in the top or bottom 8 % of a page whose lowercase, digit-masked form
  recurs on at least two pages and on half of the pages is removed, and so is a margin line that
  is only a page number ("Page 2 of 3", "2", "- 2 -") alone on its row (a year beside a title
  stays);
- columns: line x-starts are clustered per page; each cluster start is a candidate gutter, kept
  when at least 3 lines lie on each side, no narrow line crosses it beside the right-hand group
  (full-width lines such as a header may, up to 10 % of the lines), and the right-hand group is
  not an annotation of the left rows (narrow, sharing those rows, and right-aligned or sparse:
  dates and locations next to titles);
- reading order: bands between lines spanning a gutter; within a band, column by column, each
  column row by row; the fragments of one row of a column join into one line ("title  dates");
- verification, on pages with columns: a line ending mid-sentence (a comma, a word broken by a
  hyphen, or a function word such as "for", "the", "de") must be followed by a line that is not a
  bullet or a heading. Two or more failures outnumbering the successes make the reading order
  uncertain: tier B and COLUMN_ORDER_UNCERTAIN.
"""

from __future__ import annotations

import io
import math
import re
import statistics
import threading
import zlib
from bisect import bisect_left, bisect_right
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

import matcher.ingest.ocr as ocr
from matcher.errors import Flag
from matcher.ingest.detect import internal_fault
from matcher.ingest.normalize_text import BULLET_GLYPHS, normalize_line
from matcher.ingest.quality import MIN_VISIBLE_SIZE_PT, digit_masked, is_near_white, is_page_number, to_rgb

EOF_WINDOW = 8192  # bytes at the end of the file that must hold "%%EOF"
MARGIN_BAND = 0.08  # brief row 5: top and bottom 8 % of the page
REPEAT_MIN_PAGES = 2  # a margin line repeated on at least this many pages ...
REPEAT_MIN_SHARE = 0.5  # ... and on at least this share of the pages read is a header/footer
MAX_PAGE_NUMBER_CANDIDATES = 50  # more page-number-like margin lines than this: junk, dropped unchecked
X_CLUSTER_TOL = 0.03  # x-starts within 3 % of the page width form one cluster
MIN_COLUMN_LINES = 3  # lines a column needs on each side of a gutter
WIDE_CROSSER = 0.45  # a line at least this wide (share of the page) may span a gutter ...
MAX_CROSSERS = 0.10  # ... and at most this share of the page's lines may do so
ANNOTATION_ALIGNED = 0.75  # share of a right-hand group sharing rows with left text (annotation)
ANNOTATION_WIDTH = 0.20  # median width (share of the page) below which that group is narrow
RIGHT_ALIGN_TOL = 2.0  # pt; right edges this close are right-aligned
ADJACENT_PITCH = 1.8  # consecutive lines closer than 1.8 x their height are adjacent (dense)
ROW_OVERLAP = 0.5  # fragments overlapping by half the smaller height share a row
GAP_SPLIT = 1.5  # a horizontal gap above 1.5 x the font size splits a text line into fragments
SPACE_GAP = 0.2  # pdfplumber: a gap above 0.2 x the font size between glyphs is a word space
DUPLICATE_TOL = 1.5  # pt; repeated identical text this close is a fake-bold double strike
MAX_LAYOUT_FRAGMENTS = 2000  # beyond this a page is read as one column (cost guard)
SCAN_IMAGE_COVERAGE = 0.5  # images covering half the page + only invisible text = a scan's OCR layer
MIN_FAILURES = 2  # continuity failures needed before the order is called uncertain
OFFPAGE_TOL = 0.5  # pt
# MuPDF messages that mean decoding part of the file's data went wrong (a zeroed hole in a
# compressed content stream, a file cut short and patched with %%EOF, but also a bad or missing
# checksum on data that was fully recovered). Repairs and syntax warnings alone are not here:
# real PDFs often have them and still read correctly. Damage makes a file CORRUPT only together
# with a sign of loss on the page (`_page_lost`): garbage in a content stream, or a read cut off.
_DAMAGE_MARKERS = ("zlib error", "premature end of data", "read error", "flate")
# The content interpreter's own complaints (garbage decoded into a page's operators); the object
# parser's "syntax error: invalid key in dict" about a damaged font does not count.
_SYNTAX_MARKERS = ("syntax error in content stream", "encountered syntax errors")
_INFLATE_CHUNK = 1 << 20  # bytes per re-inflation step
_INFLATE_BUDGET = 64 << 20  # a stream inflating beyond this is not re-checked
_MUPDF_LOCK = threading.Lock()
_UNMAPPED = "\ufffd"
_PAINTED = 16 | 32  # rawdict char_flags: FILLED | STROKED
_INVISIBLE_TYPE = 3  # get_texttrace span "type" for render mode 3

_WORDS = re.compile(r"[^\W\d_]+")
_FUNCTION_WORDS = frozenset(
    "a an the of to in for with and or on at by from as into via "
    "le la les un une des du de d l et ou à au aux en pour par avec sur dans chez".split()
)
_LTR_RUN = re.compile(r"[0-9A-Za-zÀ-ɏ](?:[0-9A-Za-zÀ-ɏ.,:/@%+#'-]*[0-9A-Za-zÀ-ɏ])?")


# ─── Data ──────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Fragment:
    """A run of text on one baseline with no large gap, normalised (normalize_line)."""

    page: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float = 0.0
    bold: bool = False
    rtl: bool = False

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.1)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0


@dataclass(frozen=True)
class PlacedLine:
    page: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    column: int


@dataclass(frozen=True)
class PageLayout:
    width: float
    height: float
    fragments: tuple[Fragment, ...]


@dataclass
class PdfResult:
    lines: list[PlacedLine]
    page_count: int
    flags: set[Flag]
    ocr_confidence: Optional[float] = None
    failure: Optional[Flag] = None

    @classmethod
    def failed(cls, reason: Flag, page_count: int = 0, flags: Iterable[Flag] = (),
               ocr_confidence: Optional[float] = None) -> "PdfResult":
        return cls([], page_count, set(flags), ocr_confidence, reason)


@dataclass(frozen=True)
class _Glyph:
    char: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool


@dataclass(frozen=True)
class _PageRead:
    index: int
    width: float
    height: float
    fragments: tuple[Fragment, ...]
    stuffing: bool  # hidden glyphs found that are not a scanner's OCR layer

    @property
    def image_page(self) -> bool:
        return not self.fragments


# ─── Fragments from glyphs ───────────────────────────────────────────────────────────────────


def _rtl_letter(char: str) -> bool:
    code = ord(char)
    return 0x0590 <= code <= 0x08FF or 0xFB1D <= code <= 0xFDFF or 0xFE70 <= code <= 0xFEFF


def is_rtl(text: str) -> bool:
    """Most of the letters are Hebrew or Arabic."""
    rtl = sum(1 for c in text if c.isalpha() and _rtl_letter(c))
    return rtl > 0 and 2 * rtl > sum(1 for c in text if c.isalpha())


def visual_to_logical(text: str) -> str:
    """A right-to-left line stored left to right (visual order) back in logical order: reverse it,
    then restore the left-to-right runs (digits, Latin words) inside it."""
    return _LTR_RUN.sub(lambda match: match.group(0)[::-1], text[::-1])


def _duplicate_glyph(previous: _Glyph, glyph: _Glyph) -> bool:
    width = max(previous.x1 - previous.x0, 0.0)
    return (glyph.char == previous.char and abs(glyph.x0 - previous.x0) < max(1.0, 0.3 * width)
            and abs(glyph.y0 - previous.y0) < 1.0)


def _split_glyphs(glyphs: Sequence[_Glyph]) -> list[list[_Glyph]]:
    groups: list[list[_Glyph]] = []
    current: list[_Glyph] = []
    for glyph in glyphs:
        if current:
            previous = current[-1]
            if _duplicate_glyph(previous, glyph):
                continue
            gap = max(glyph.x0 - previous.x1, previous.x0 - glyph.x1)
            if gap > GAP_SPLIT * max(previous.size, glyph.size, 1.0):
                groups.append(current)
                current = []
        current.append(glyph)
    if current:
        groups.append(current)
    return groups


def _fragment(group: Sequence[_Glyph], page: int, visual_order: bool) -> Optional[Fragment]:
    raw = "".join(g.char for g in group)
    if visual_order and is_rtl(raw):
        raw = visual_to_logical(raw)
    text = normalize_line(raw)
    if not text:
        return None
    ink = [g for g in group if not g.char.isspace()] or list(group)
    bold = 2 * sum(1 for g in ink if g.bold) > len(ink)
    return Fragment(page, text, round(min(g.x0 for g in ink), 2), round(min(g.y0 for g in ink), 2),
                    round(max(g.x1 for g in ink), 2), round(max(g.y1 for g in ink), 2),
                    round(max(g.size for g in ink), 2), bold, is_rtl(text))


def _fragments(lines: Iterable[Sequence[_Glyph]], page: int, visual_order: bool = False) -> tuple[Fragment, ...]:
    out = []
    for glyphs in lines:
        for group in _split_glyphs(glyphs):
            fragment = _fragment(group, page, visual_order)
            if fragment is not None:
                out.append(fragment)
    return tuple(out)


def fragments_from_ocr(lines: Sequence[ocr.OcrLine], page: int, points_per_pixel: float) -> tuple[Fragment, ...]:
    """OCR lines kept as text (confidence >= ocr.KEEP_LINE_CONFIDENCE), in page points."""
    out = []
    s = points_per_pixel
    for line in lines:
        if line.confidence < ocr.KEEP_LINE_CONFIDENCE:
            continue
        text = normalize_line(line.text)
        if text:
            out.append(Fragment(page, text, round(line.x0 * s, 2), round(line.y0 * s, 2), round(line.x1 * s, 2),
                                round(line.y1 * s, 2), round((line.y1 - line.y0) * s, 2), False, is_rtl(text)))
    return tuple(out)


# ─── Layout: headers and footers ─────────────────────────────────────────────────────────────


def _in_margin(fragment: Fragment, page: PageLayout) -> bool:
    return fragment.cy < MARGIN_BAND * page.height or fragment.cy > (1.0 - MARGIN_BAND) * page.height


def strip_repeated_margins(pages: Sequence[PageLayout]) -> list[PageLayout]:
    seen: Counter[str] = Counter()
    for page in pages:
        seen.update({digit_masked(f.text) for f in page.fragments if _in_margin(f, page)})
    repeated = {key for key, n in seen.items() if n >= REPEAT_MIN_PAGES and n >= REPEAT_MIN_SHARE * len(pages)}

    def page_numbers(page: PageLayout) -> set[int]:
        """Margin fragments that read as a page number and stand alone on their row (a year
        right-aligned beside a title in the margin band shares its row and stays)."""
        candidates = [i for i, f in enumerate(page.fragments) if _in_margin(f, page) and is_page_number(f.text)]
        if len(candidates) > MAX_PAGE_NUMBER_CANDIDATES:  # a hostile page: no row check, just junk
            return set(candidates)
        return {i for i in candidates
                if not any(j != i and _same_row(page.fragments[i], other) for j, other in enumerate(page.fragments))}

    out = []
    for page in pages:
        numbers = page_numbers(page)
        out.append(replace(page, fragments=tuple(
            f for i, f in enumerate(page.fragments)
            if i not in numbers and not (_in_margin(f, page) and digit_masked(f.text) in repeated))))
    return out


# ─── Layout: columns and reading order ───────────────────────────────────────────────────────


def _same_row(a: Fragment, b: Fragment) -> bool:
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    return overlap >= ROW_OVERLAP * min(a.height, b.height)


def _dedupe(fragments: Sequence[Fragment]) -> list[Fragment]:
    """Drop fake-bold double strikes: the same text drawn again within DUPLICATE_TOL."""
    kept: list[Fragment] = []
    buckets: dict[tuple[str, int, int], list[Fragment]] = {}
    for fragment in fragments:
        bx, by = math.floor(fragment.x0 / DUPLICATE_TOL), math.floor(fragment.y0 / DUPLICATE_TOL)
        neighbours = (other for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                      for other in buckets.get((fragment.text, bx + dx, by + dy), ()))
        if any(abs(o.x0 - fragment.x0) <= DUPLICATE_TOL and abs(o.y0 - fragment.y0) <= DUPLICATE_TOL
               for o in neighbours):
            continue
        buckets.setdefault((fragment.text, bx, by), []).append(fragment)
        kept.append(fragment)
    return kept


def _sparse(group: Sequence[Fragment]) -> bool:
    ordered = sorted(group, key=lambda f: f.cy)
    adjacent: set[int] = set()
    for i in range(len(ordered) - 1):
        a, b = ordered[i], ordered[i + 1]
        if b.cy - a.cy <= ADJACENT_PITCH * max(a.height, b.height):
            adjacent.update((i, i + 1))
    return len(adjacent) < 0.5 * len(ordered)


def _aligned(right: Sequence[Fragment], left: Sequence[Fragment]) -> int:
    """How many right-hand fragments share a row with some left-hand one, in O((R + L) log L).

    Two fragments overlap by at least half the smaller height (`_same_row`) exactly when the
    centre of one lies within the other's span. So a right fragment has a partner when a left
    fragment's centre lies within its span (bisect over sorted centres) or a left fragment's span
    contains its centre (sorted starts with a running "furthest end"). Each witness found this
    way is confirmed with `_same_row` itself; tests check equality with the all-pairs count."""
    if not left:
        return 0
    by_centre = sorted(left, key=lambda f: f.cy)
    centres = [f.cy for f in by_centre]
    by_start = sorted(left, key=lambda f: f.y0)
    starts = [f.y0 for f in by_start]
    furthest: list[Fragment] = []  # furthest[k]: the fragment ending lowest among by_start[:k + 1]
    for fragment in by_start:
        furthest.append(fragment if not furthest or fragment.y1 > furthest[-1].y1 else furthest[-1])
    count = 0
    for fragment in right:
        lo = bisect_left(centres, fragment.y0)
        hi = min(bisect_right(centres, fragment.y1), lo + 4)  # the first witness suffices
        if any(_same_row(fragment, other) for other in by_centre[lo:hi]):
            count += 1
            continue
        k = bisect_right(starts, fragment.cy) - 1
        if k >= 0 and furthest[k].y1 >= fragment.cy and _same_row(fragment, furthest[k]):
            count += 1
    return count


def _is_annotation(right: Sequence[Fragment], left: Sequence[Fragment], width: float) -> bool:
    """Dates or places beside titles: narrow, on the left text's rows, right-aligned or sparse."""
    if _aligned(right, left) < ANNOTATION_ALIGNED * len(right):
        return False
    if statistics.median(r.x1 - r.x0 for r in right) >= ANNOTATION_WIDTH * width:
        return False
    edge = statistics.median(r.x1 for r in right)
    right_aligned = sum(1 for r in right if abs(r.x1 - edge) <= RIGHT_ALIGN_TOL) >= ANNOTATION_ALIGNED * len(right)
    return right_aligned or _sparse(right)


def _is_gutter(fragments: Sequence[Fragment], b: float, width: float) -> bool:
    right = [f for f in fragments if f.x0 >= b - 0.5]
    left = [f for f in fragments if f.x0 < b - 0.5 and f.x1 <= b + 1.0]
    if len(right) < MIN_COLUMN_LINES or len(left) < MIN_COLUMN_LINES:
        return False
    top, bottom = min(f.y0 for f in right), max(f.y1 for f in right)

    def beside(f: Fragment) -> bool:
        return f.y1 > top and f.y0 < bottom

    crossers = [f for f in fragments if f.x0 < b - 0.5 and f.x1 > b + 1.0 and beside(f)]
    if any(f.x1 - f.x0 < WIDE_CROSSER * width for f in crossers):
        return False  # a left-hand line runs past b: b is inside a column
    if len(crossers) > MAX_CROSSERS * (len(left) + len(right)):
        return False
    left_beside = [f for f in left if beside(f)]
    if len(left_beside) < MIN_COLUMN_LINES:
        return False
    return not _is_annotation(right, left_beside, width)


def find_gutters(fragments: Sequence[Fragment], width: float) -> list[float]:
    """x positions where a new column starts (empty for a single column)."""
    if len(fragments) < 2 * MIN_COLUMN_LINES or len(fragments) > MAX_LAYOUT_FRAGMENTS:
        return []
    tolerance = X_CLUSTER_TOL * width
    starts: list[float] = []
    previous: Optional[float] = None
    for x in sorted(f.x0 for f in fragments):
        if previous is None or x - previous > tolerance:
            starts.append(x)
        previous = x
    return [b for b in starts[1:] if _is_gutter(fragments, b, width)]


def _crosses(fragment: Fragment, gutters: Sequence[float]) -> bool:
    return any(fragment.x0 < g - 0.5 and fragment.x1 > g + 1.0 for g in gutters)


def _column_of(fragment: Fragment, gutters: Sequence[float]) -> int:
    return sum(1 for g in gutters if fragment.x0 >= g - 0.5)


def _group_rows(fragments: Iterable[Fragment]) -> list[list[Fragment]]:
    rows: list[list[Fragment]] = []
    for fragment in sorted(fragments, key=lambda f: (f.cy, f.x0)):
        if rows and _same_row(rows[-1][0], fragment):
            rows[-1].append(fragment)
        else:
            rows.append([fragment])
    return rows


def _merge_row(row: Sequence[Fragment], column: int) -> PlacedLine:
    total = sum(len(f.text) for f in row) or 1
    if 2 * sum(len(f.text) for f in row if f.rtl) > total:  # a right-to-left row reads from the right
        ordered = sorted(row, key=lambda f: (-f.x1, f.y0))
    else:
        ordered = sorted(row, key=lambda f: (f.x0, f.y0))
    return PlacedLine(
        page=row[0].page, text=" ".join(f.text for f in ordered),
        x0=min(f.x0 for f in row), y0=min(f.y0 for f in row), x1=max(f.x1 for f in row),
        y1=max(f.y1 for f in row), size=max(f.size for f in row),
        bold=2 * sum(len(f.text) for f in row if f.bold) > total, column=column,
    )


def order_page(page: PageLayout) -> tuple[list[PlacedLine], bool]:
    """Reading-order lines of one page and whether it has columns."""
    fragments = _dedupe(page.fragments)
    if not fragments:
        return [], False
    gutters = find_gutters(fragments, page.width)
    if not gutters:
        return [_merge_row(row, 0) for row in _group_rows(fragments)], False
    spanning = [f for f in fragments if _crosses(f, gutters)]
    span_rows = _group_rows(spanning)
    cuts = [statistics.fmean(f.cy for f in row) for row in span_rows]
    regions: list[list[Fragment]] = [[] for _ in range(len(span_rows) + 1)]
    for fragment in fragments:
        if not _crosses(fragment, gutters):
            regions[bisect_right(cuts, fragment.cy)].append(fragment)
    lines: list[PlacedLine] = []
    for r, members in enumerate(regions):
        for column in range(len(gutters) + 1):
            in_column = [f for f in members if _column_of(f, gutters) == column]
            lines.extend(_merge_row(row, column) for row in _group_rows(in_column))
        if r < len(span_rows):
            lines.append(_merge_row(span_rows[r], 0))
    return lines, True


def _ends_mid_sentence(text: str) -> bool:
    if not text or text[-1] in ".:;!?)]\"'":
        return False
    if text[-1] == ",":
        return True
    if len(text) >= 2 and text[-1] in "-\u2010" and text[-2].isalpha():
        return True
    words = _WORDS.findall(text.lower())
    return bool(words) and words[-1] in _FUNCTION_WORDS and text.lower().endswith(words[-1])


def _breaks_flow(text: str) -> bool:
    """A bullet or a heading cannot continue the previous line's sentence."""
    if text == "-" or text.startswith("- "):
        return True
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 2 and all(c.isupper() for c in letters):
        return True
    return text.endswith(":")


def reading_order_uncertain(lines: Sequence[PlacedLine]) -> bool:
    passes = fails = 0
    for current, following in zip(lines, lines[1:]):
        if _ends_mid_sentence(current.text):
            if _breaks_flow(following.text):
                fails += 1
            else:
                passes += 1
    return fails >= MIN_FAILURES and fails > passes


def layout_document(pages: Sequence[PageLayout]) -> tuple[list[PlacedLine], bool]:
    """All pages' lines in reading order, and whether any page's column order is uncertain."""
    lines: list[PlacedLine] = []
    uncertain = False
    for page in strip_repeated_margins(pages):
        page_lines, has_columns = order_page(page)
        if has_columns and reading_order_uncertain(page_lines):
            uncertain = True
        lines.extend(page_lines)
    return lines, uncertain


# ─── Image pages (OCR) and the result ────────────────────────────────────────────────────────

Render = Callable[[int], Optional[tuple[Any, float]]]  # page index -> (RGB array, pixels per point)


def _finish(reads: Sequence[_PageRead], has_ink: Callable[[int], bool], render: Render, page_count: int,
            max_pages: int, ocr_min_confidence: float) -> PdfResult:
    flags: set[Flag] = set()
    if page_count > max_pages:
        flags.add(Flag.TRUNCATED)
    if any(read.stuffing for read in reads):
        flags.add(Flag.STUFFING_SUSPECTED)
    scanned = all(read.image_page for read in reads)
    confidences: list[float] = []
    layouts: list[PageLayout] = []
    for read in reads:
        layout = PageLayout(read.width, read.height, read.fragments)
        if read.image_page and has_ink(read.index):
            if ocr.get_engine() is None:
                if scanned:
                    return PdfResult.failed(Flag.UNREADABLE, page_count, flags)
            else:
                rendered = render(read.index)
                lines = ocr.run_ocr(rendered[0]) if rendered is not None else None
                if lines is None:
                    if scanned:
                        return PdfResult.failed(Flag.UNREADABLE, page_count, flags)
                else:
                    page_confidences = [line.confidence for line in lines]
                    readable = not page_confidences or (
                        math.fsum(page_confidences) / len(page_confidences) >= ocr_min_confidence)
                    if scanned or readable:
                        confidences.extend(page_confidences)
                        layout = PageLayout(read.width, read.height,
                                            fragments_from_ocr(lines, read.index, 1.0 / rendered[1]))
                        if scanned or layout.fragments:
                            flags.add(Flag.OCR_USED)
        layouts.append(layout)
    mean = round(math.fsum(confidences) / len(confidences), 4) if confidences else None
    if scanned and mean is not None and mean < ocr_min_confidence:
        return PdfResult.failed(Flag.UNREADABLE, page_count, flags, mean)
    lines, uncertain = layout_document(layouts)
    if uncertain:
        flags.add(Flag.COLUMN_ORDER_UNCERTAIN)
    return PdfResult(lines, page_count, flags, mean)


def extract_pdf(data: bytes, *, max_pages: int, ocr_min_confidence: float) -> PdfResult:
    start = data.find(b"%PDF-")
    if start > 0:  # bytes before the header (mail gateways): offsets count from the header
        data = data[start:]
    if b"%%EOF" not in data[-EOF_WINDOW:]:
        return PdfResult.failed(Flag.CORRUPT)
    try:
        fitz = _import_fitz()
    except ImportError:
        return _extract_with_pdfplumber(data, max_pages, ocr_min_confidence)
    return _extract_with_pymupdf(fitz, data, max_pages, ocr_min_confidence)


# ─── PyMuPDF ───────────────────────────────────────────────────────────────────────────────────


def _import_fitz() -> Any:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore[no-redef]  # older wheels only ship the legacy name
    return fitz


def _key(code: int, origin: Sequence[float]) -> tuple[int, float, float]:
    return code, round(float(origin[0]), 1), round(float(origin[1]), 1)


def _counts(code: int) -> bool:
    """Whether a hidden glyph is evidence of stuffing (spaces and format characters are not)."""
    if not 0 < code < 0x110000:
        return False
    char = chr(code)
    return char.isprintable() and not char.isspace()


def _outside(bbox: Sequence[float], left: float, top: float, right: float, bottom: float) -> bool:
    cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    return not (left - OFFPAGE_TOL <= cx <= right + OFFPAGE_TOL and top - OFFPAGE_TOL <= cy <= bottom + OFFPAGE_TOL)


class _Background:
    """What lies under a point of a PyMuPDF page: filled shapes (by drawing order) and images."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._fills: Optional[list[tuple[Any, Optional[tuple[float, float, float]], int]]] = None
        self._images: list[Sequence[float]] = []

    def _load(self) -> None:
        fills = []
        try:
            for path in self._page.get_drawings():
                if path.get("fill") is None or (path.get("fill_opacity") or 0) <= 0:
                    continue
                fills.append((path["rect"], to_rgb(path["fill"]), int(path.get("seqno", 0))))
        except Exception as exc:  # unreadable vector graphics: assume a white page behind the text
            if internal_fault(exc):
                raise
            fills = []
        self._fills = sorted(fills, key=lambda item: -item[2])  # topmost first
        try:
            self._images = [info["bbox"] for info in self._page.get_image_info()]
        except Exception as exc:
            if internal_fault(exc):
                raise
            self._images = []

    def light_under(self, bbox: Sequence[float], seqno: int) -> bool:
        if self._fills is None:
            self._load()
        assert self._fills is not None
        x, y = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
        for rect, rgb, order in self._fills:
            if order < seqno and rect.x0 <= x <= rect.x1 and rect.y0 <= y <= rect.y1:
                return is_near_white(rgb)
        return not any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in self._images)


def _image_coverage(page: Any) -> float:
    area = page.rect.width * page.rect.height
    if area <= 0:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        covered += max(0.0, min(x1, page.rect.x1) - max(x0, page.rect.x0)) * max(
            0.0, min(y1, page.rect.y1) - max(y0, page.rect.y0))
    return min(1.0, covered / area)


def _trace_reason(span: dict, background: _Background) -> Optional[str]:
    if span.get("type") == _INVISIBLE_TYPE:
        return "invisible"
    if float(span.get("opacity", 1.0)) <= 0.001:
        return "transparent"
    if float(span.get("size", 0.0)) < MIN_VISIBLE_SIZE_PT:
        return "tiny"
    if is_near_white(to_rgb(span.get("color"))) and background.light_under(span["bbox"], int(span.get("seqno", 0))):
        return "white"
    return None


def _read_page_pymupdf(fitz: Any, page: Any, index: int) -> _PageRead:
    rect = page.rect
    bounds = (rect.x0, rect.y0, rect.x1, rect.y1)
    background = _Background(page)
    visible: set[tuple[int, float, float]] = set()
    hidden: set[tuple[int, float, float]] = set()
    hidden_glyphs = 0
    invisible_only = True
    for span in page.get_texttrace():
        reason = _trace_reason(span, background)
        for code, _glyph, origin, bbox in span["chars"]:
            key = _key(code, origin)
            why = reason or ("offpage" if _outside(bbox, *bounds) else None)
            if why is None:
                visible.add(key)
                continue
            hidden.add(key)
            if _counts(code):
                hidden_glyphs += 1
                invisible_only = invisible_only and why == "invisible"
    lines: list[list[_Glyph]] = []
    flags = fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE  # no mediabox clip: see off-page text
    for block in page.get_text("rawdict", flags=flags).get("blocks", ()):
        for line in block.get("lines", ()):
            glyphs: list[_Glyph] = []
            for span in line.get("spans", ()):
                painted = span.get("alpha", 255) > 0 and (span.get("char_flags", _PAINTED) & _PAINTED) != 0
                bold = bool(span.get("flags", 0) & 16) or "bold" in str(span.get("font", "")).lower()
                size = float(span.get("size", 0.0))
                for ch in span.get("chars", ()):
                    char = ch.get("c") or ""
                    if len(char) != 1:
                        continue
                    key = _key(ord(char), ch["origin"])
                    if not painted:  # clip-only or invisible: hidden unless a painted copy exists
                        if key not in visible and key not in hidden and _counts(ord(char)):
                            hidden.add(key)
                            hidden_glyphs += 1
                        continue
                    if (key in hidden and key not in visible) or _outside(ch["bbox"], *bounds):
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    glyphs.append(_Glyph(char, float(x0), float(y0), float(x1), float(y1), size, bold))
            lines.append(glyphs)
    fragments = _fragments(lines, index)
    scan_layer = (not fragments and hidden_glyphs > 0 and invisible_only
                  and _image_coverage(page) >= SCAN_IMAGE_COVERAGE)
    return _PageRead(index, rect.width, rect.height, fragments, hidden_glyphs > 0 and not scan_layer)


@contextmanager
def _quiet_mupdf(fitz: Any) -> Iterator[Callable[[], str]]:
    """Own PyMuPDF for the duration (it is not thread-safe, and its message store is process-wide),
    keep MuPDF's errors and warnings off stdout, and yield a reader of the messages it logged
    meanwhile. The caller's display settings are restored; the message store is left empty."""
    tools = fitz.TOOLS
    with _MUPDF_LOCK:
        show_errors, show_warnings = tools.mupdf_display_errors(), tools.mupdf_display_warnings()
        tools.mupdf_display_errors(False)
        tools.mupdf_display_warnings(False)
        tools.reset_mupdf_warnings()
        try:
            yield lambda: tools.mupdf_warnings(reset=False)
        finally:
            tools.reset_mupdf_warnings()
            tools.mupdf_display_errors(bool(show_errors))
            tools.mupdf_display_warnings(bool(show_warnings))


def damaged(messages: str) -> bool:
    """MuPDF reported trouble decoding part of the file's data. Not by itself a loss: a bad or
    missing checksum is reported the same way although every byte was recovered."""
    lowered = messages.lower()
    return any(marker in lowered for marker in _DAMAGE_MARKERS)


def _deflate_cut_short(doc: Any, xref: int) -> bool:
    """Whether a plain FlateDecode stream's deflate data is invalid or ends before the stream
    does (data lost), as opposed to only a bad or missing Adler-32 checksum (data intact).
    Streams with other or chained filters are not judged here."""
    kind, value = doc.xref_get_key(xref, "Filter")
    if value.replace("[", "").replace("]", "").strip() != "/FlateDecode":
        return False
    raw = doc.xref_stream_raw(xref) or b""
    if len(raw) < 2:
        return True
    if (raw[0] & 0x0F) == 8 and (raw[0] * 256 + raw[1]) % 31 == 0:
        if raw[1] & 0x20:  # a preset dictionary: not something PDF writers use; do not judge
            return False
        raw = raw[2:]  # the zlib header; the deflate data follows
    inflater = zlib.decompressobj(-15)  # raw deflate: the end of the data is known, the checksum is not read
    pending, produced = raw, 0
    try:
        while True:
            produced += len(inflater.decompress(pending, _INFLATE_CHUNK))
            if inflater.eof:
                return False
            pending = inflater.unconsumed_tail
            if not pending:
                return True  # the input ran out before the deflate data ended
            if produced > _INFLATE_BUDGET:
                return False  # too large to re-check; MuPDF's own result stands
    except zlib.error:
        return True


def _page_lost(tools: Any, doc: Any, page: Any, read: _PageRead, messages: str) -> bool:
    """Decoding trouble was logged while this page was read: did the page actually lose content?

    It did when nothing readable came out of it, or when one of its content streams is itself
    damaged (decoding that stream alone reports trouble) and shows a loss: MuPDF stopped decoding
    it early ("read error"), its deflate data is invalid or cut short, or garbage reached the
    interpreter (syntax errors on the page). Trouble confined to other streams (an embedded font
    MuPDF replaces, an image) or a stream that only has a bad or missing checksum loses no text."""
    if not read.fragments:
        return True
    garbage = any(marker in messages.lower() for marker in _SYNTAX_MARKERS)
    for xref in page.get_contents():
        tools.reset_mupdf_warnings()
        try:
            doc.xref_stream(xref)
        except Exception as exc:
            if internal_fault(exc):
                raise
            return True
        stream_messages = tools.mupdf_warnings(reset=False).lower()
        if damaged(stream_messages) and (garbage or "read error" in stream_messages
                                         or _deflate_cut_short(doc, xref)):
            return True
    return False


def _extract_with_pymupdf(fitz: Any, data: bytes, max_pages: int, ocr_min_confidence: float) -> PdfResult:
    """MuPDF work (open, text, damage checks, the renders for OCR, close) holds the MuPDF lock;
    OCR inference itself runs outside it, so one slow scan does not hold up other PDFs."""
    reads: list[_PageRead] = []
    page_count = 0
    with _quiet_mupdf(fitz) as messages:
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            if internal_fault(exc):
                raise
            return PdfResult.failed(Flag.CORRUPT)
        failure: Optional[PdfResult] = None
        try:
            if doc.needs_pass:
                failure = PdfResult.failed(Flag.ENCRYPTED)
            elif doc.page_count < 1:
                failure = PdfResult.failed(Flag.CORRUPT)
            else:
                page_count = doc.page_count
                for i in range(min(page_count, max_pages)):
                    fitz.TOOLS.reset_mupdf_warnings()  # attribute messages to this page
                    page = doc.load_page(i)
                    read = _read_page_pymupdf(fitz, page, i)
                    logged = messages()
                    if damaged(logged) and _page_lost(fitz.TOOLS, doc, page, read, logged):
                        failure = PdfResult.failed(Flag.CORRUPT, page_count)
                        break
                    reads.append(read)
        except Exception as exc:  # MuPDF gave up on a page
            doc.close()
            if internal_fault(exc):
                raise
            return PdfResult.failed(Flag.CORRUPT)
        if failure is not None:
            doc.close()
            return failure

    def has_ink(i: int) -> bool:
        with _quiet_mupdf(fitz):
            return ocr.page_has_ink(fitz, doc.load_page(i))

    def render(i: int) -> Optional[tuple[Any, float]]:
        with _quiet_mupdf(fitz):
            return ocr.render_page(fitz, doc.load_page(i))

    try:
        return _finish(reads, has_ink, render, page_count, max_pages, ocr_min_confidence)
    except Exception as exc:  # MuPDF gave up rendering a page
        if internal_fault(exc):
            raise
        return PdfResult.failed(Flag.CORRUPT)
    finally:
        with _quiet_mupdf(fitz):
            doc.close()


# ─── pdfplumber (fallback) ───────────────────────────────────────────────────────────────────


def _password_error(exc: BaseException) -> bool:
    try:
        from pdfminer.pdfdocument import PDFEncryptionError
    except ImportError:  # pragma: no cover - pdfminer ships with pdfplumber
        return False
    seen: list[object] = [exc]
    while seen:
        item = seen.pop()
        if isinstance(item, PDFEncryptionError):
            return True
        if isinstance(item, BaseException):
            seen.extend(x for x in (item.__cause__, item.__context__, *item.args) if x is not None)
            if len(seen) > 50:
                break
    return False


def _read_page_pdfplumber(page: Any, index: int) -> _PageRead:
    left, top, right, bottom = (float(v) for v in page.bbox)
    fills = [(float(r["x0"]), float(r["top"]), float(r["x1"]), float(r["bottom"]), to_rgb(r.get("non_stroking_color")))
             for r in page.rects if r.get("fill")]
    images = [(float(i["x0"]), float(i["top"]), float(i["x1"]), float(i["bottom"])) for i in page.images]

    def light_under(x: float, y: float) -> bool:
        for x0, y0, x1, y1, rgb in reversed(fills):  # drawn last = on top
            if x0 <= x <= x1 and y0 <= y <= y1:
                return is_near_white(rgb)
        return not any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in images)

    hidden_glyphs = 0
    glyphs: list[_Glyph] = []
    for ch in page.chars:
        char = ch.get("text") or ""
        if char.startswith("(cid:"):  # a glyph pdfminer cannot map to Unicode
            char = _UNMAPPED
        if len(char) != 1:
            continue
        x0, x1, y0, y1 = float(ch["x0"]), float(ch["x1"]), float(ch["top"]), float(ch["bottom"])
        size = float(ch.get("size") or 0.0)
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        hidden = (size < MIN_VISIBLE_SIZE_PT or _outside((x0, y0, x1, y1), left, top, right, bottom)
                  or (is_near_white(to_rgb(ch.get("non_stroking_color"))) and light_under(cx, cy)))
        if hidden:
            hidden_glyphs += 1 if _counts(ord(char)) else 0
            continue
        glyphs.append(_Glyph(char, x0, y0, x1, y1, size, "bold" in str(ch.get("fontname", "")).lower()))
    lines: list[list[_Glyph]] = []
    for glyph in sorted(glyphs, key=lambda g: (round(g.y1, 1), g.x0)):
        if lines and abs(glyph.y1 - lines[-1][0].y1) <= max(1.0, 0.3 * max(glyph.size, lines[-1][0].size)):
            lines[-1].append(glyph)
        else:
            lines.append([glyph])
    spaced: list[list[_Glyph]] = []
    for line in lines:
        line.sort(key=lambda g: g.x0)
        out: list[_Glyph] = []
        for glyph in line:
            if out and not out[-1].char.isspace() and not glyph.char.isspace():
                gap = glyph.x0 - out[-1].x1
                size = max(glyph.size, out[-1].size, 1.0)
                if SPACE_GAP * size < gap <= GAP_SPLIT * size:  # PDFs without space glyphs (LaTeX)
                    out.append(_Glyph(" ", out[-1].x1, glyph.y0, glyph.x0, glyph.y1, glyph.size, glyph.bold))
            out.append(glyph)
        # An unmappable glyph opening a line is a bullet symbol (WinAnsi code 127, Symbol and
        # Wingdings bullets all reach pdfminer as "(cid:N)").
        if out and out[0].char == _UNMAPPED:
            out[0] = replace(out[0], char=BULLET_GLYPHS[0])
        spaced.append(out)
    fragments = _fragments(spaced, index, visual_order=True)
    return _PageRead(index, right - left, bottom - top, fragments, hidden_glyphs > 0)


def _extract_with_pdfplumber(data: bytes, max_pages: int, ocr_min_confidence: float) -> PdfResult:
    try:
        import pdfplumber
    except ImportError:
        return PdfResult.failed(Flag.UNREADABLE)  # no PDF reader at all
    try:
        document = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        if internal_fault(exc):
            raise
        return PdfResult.failed(Flag.ENCRYPTED if _password_error(exc) else Flag.CORRUPT)
    try:
        pages = document.pages
        page_count = len(pages)
        if page_count < 1:
            return PdfResult.failed(Flag.CORRUPT)
        reads = [_read_page_pdfplumber(pages[i], i) for i in range(min(page_count, max_pages))]

        def has_ink(i: int) -> bool:
            low = pages[i].to_image(resolution=ocr.INK_DPI).original.convert("L")
            return low.getextrema()[0] < ocr.INK_LEVEL

        def render(i: int) -> Optional[tuple[Any, float]]:
            array = ocr.pil_to_array(pages[i].to_image(resolution=ocr.RENDER_DPI).original)
            return array, array.shape[1] / max(float(pages[i].width), 1.0)

        return _finish(reads, has_ink, render, page_count, max_pages, ocr_min_confidence)
    except Exception as exc:
        if internal_fault(exc):
            raise
        return PdfResult.failed(Flag.ENCRYPTED if _password_error(exc) else Flag.CORRUPT)
    finally:
        document.close()
