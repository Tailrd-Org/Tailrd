"""DOCX reading (SPEC §14.1; brief row 6) with python-docx, imported on use (DECISIONS D-02).

Order follows the document: body paragraphs and tables as they come, a text box's paragraphs
right after the paragraph that anchors it, header parts first and footer parts last, each part
read once however many pages repeat it. A table row whose cells are single lines becomes one line
with the cells in order (joined by a tab, which normalisation makes a space); a row with
multi-line cells gives each cell's lines in turn. A list paragraph (numbering set on it or on its
style) starts with "- ". Of Word's alternate content (mc:AlternateContent) only mc:Choice is read:
Word stores every text box a second time as a VML fallback.

Hidden runs are removed and flag STUFFING_SUSPECTED: w:vanish (on the run, its character style,
the paragraph style chain or the document defaults), a near-white colour (every channel >= 0.95)
unless shading behind it (run, highlight, paragraph, table cell, page) is not near-white, and a
size under 2 pt. Header and footer lines that are only a page number are dropped.

A floating (anchored) text box makes the reading order a guess: tier B with
COLUMN_ORDER_UNCERTAIN. An inline text box sits in the text flow and changes nothing.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence

from matcher.errors import Flag
from matcher.ingest.detect import internal_fault
from matcher.ingest.normalize_text import SOFT_HYPHEN
from matcher.ingest.quality import MIN_VISIBLE_SIZE_PT, hex_to_rgb, is_near_white, is_page_number

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
V = "{urn:schemas-microsoft-com:vml}"
_RT_HEADER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
_RT_FOOTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
_FALSE = frozenset({"0", "false", "off", "none"})
_INLINE_CONTAINERS = frozenset(W + t for t in ("hyperlink", "smartTag", "ins", "customXml", "fldSimple", "moveTo",
                                                "sdt", "sdtContent", "dir", "bdo"))
_BLOCK_CONTAINERS = frozenset(W + t for t in ("sdtContent", "customXml"))


@dataclass
class DocLine:
    text: str
    size: float = 0.0
    bold: bool = False


@dataclass
class DocxResult:
    lines: list[DocLine]
    flags: set[Flag]
    failure: Optional[Flag] = None


@dataclass
class _RunProps:
    hidden: bool
    size: float
    bold: bool


@dataclass
class _Segment:
    parts: list[tuple[str, _RunProps]] = field(default_factory=list)


def _on(element: Any) -> bool:
    """A toggle property (w:b, w:vanish) element: on unless w:val says otherwise."""
    return (element.get(W + "val") or "true").lower() not in _FALSE


def _fill(shading: Any) -> Optional[str]:
    if shading is None:
        return None
    value = shading.get(W + "fill")
    return None if value in (None, "auto") else value


def _dark(fill_hex: Optional[str]) -> bool:
    """Shading that would show white text (anything but missing, 'auto' or near-white)."""
    if fill_hex is None:
        return False
    rgb = hex_to_rgb(fill_hex)
    return rgb is None or not is_near_white(rgb)


class _Styles:
    def __init__(self, styles: Any) -> None:
        self._by_id: dict[str, tuple[Optional[str], Any, Any]] = {}
        self.default_paragraph: Optional[str] = None
        self.defaults_rpr = None
        if styles is None:
            return
        self.defaults_rpr = styles.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr")
        for style in styles.iterfind(f"{W}style"):
            style_id = style.get(W + "styleId")
            if not style_id:
                continue
            based = style.find(W + "basedOn")
            self._by_id[style_id] = (based.get(W + "val") if based is not None else None,
                                     style.find(W + "rPr"), style.find(W + "pPr"))
            if style.get(W + "type") == "paragraph" and (style.get(W + "default") or "") in ("1", "true"):
                self.default_paragraph = style_id

    def chain(self, style_id: Optional[str]) -> list[tuple[Any, Any]]:
        """[(rPr, pPr), ...] from the style up its basedOn chain (cycle-safe)."""
        out: list[tuple[Any, Any]] = []
        seen: set[str] = set()
        while style_id and style_id not in seen and style_id in self._by_id:
            seen.add(style_id)
            based, rpr, ppr = self._by_id[style_id]
            out.append((rpr, ppr))
            style_id = based
        return out


def _first(sources: Sequence[Any], tag: str) -> Any:
    for source in sources:
        if source is not None:
            found = source.find(tag)
            if found is not None:
                return found
    return None


def _text_boxes(element: Any, anchored: bool = False) -> Iterator[tuple[Any, bool]]:
    """Text box contents under `element` (not nested ones, not mc:Fallback copies)."""
    for child in element:
        tag = child.tag
        if tag == W + "txbxContent":
            yield child, anchored
            continue
        if tag == MC + "Fallback":
            continue
        floating = anchored or tag == WP + "anchor"
        if isinstance(tag, str) and tag.startswith(V):
            style = (child.get("style") or "").replace(" ", "").lower()
            floating = floating or "position:absolute" in style
        yield from _text_boxes(child, floating)


class _Reader:
    def __init__(self, document: Any) -> None:
        self.styles = _Styles(document.styles.element)
        background = document.element.find(W + "background")
        self.page_fill = background.get(W + "color") if background is not None else None
        self.hidden_chars = 0
        self.anchored_boxes = 0

    # ── blocks ──────────────────────────────────────────────────────────────────────────────

    def blocks(self, container: Any, cell_fill: Optional[str] = None) -> list[DocLine]:
        out: list[DocLine] = []
        for child in container:
            tag = child.tag
            if tag == W + "p":
                out.extend(self.paragraph(child, cell_fill))
            elif tag == W + "tbl":
                out.extend(self.table(child))
            elif tag == W + "sdt":
                content = child.find(W + "sdtContent")
                if content is not None:
                    out.extend(self.blocks(content, cell_fill))
            elif tag in _BLOCK_CONTAINERS:
                out.extend(self.blocks(child, cell_fill))
            elif tag == MC + "AlternateContent":
                target = child.find(MC + "Choice")
                if target is None:
                    target = child.find(MC + "Fallback")
                if target is not None:
                    out.extend(self.blocks(target, cell_fill))
        return out

    def _cells(self, row: Any) -> Iterator[Any]:
        for child in row:
            if child.tag == W + "tc":
                yield child
            elif child.tag in (W + "sdt", W + "customXml"):
                content = child.find(W + "sdtContent") if child.tag == W + "sdt" else child
                if content is not None:
                    yield from self._cells(content)

    def table(self, table: Any) -> list[DocLine]:
        out: list[DocLine] = []
        for row in table.iterfind(W + "tr"):
            cells = []
            for cell in self._cells(row):
                properties = cell.find(W + "tcPr")
                fill = _fill(properties.find(W + "shd")) if properties is not None else None
                lines = self.blocks(cell, fill)
                if lines:
                    cells.append(lines)
            if cells and all(len(lines) == 1 for lines in cells):
                out.append(DocLine("\t".join(lines[0].text for lines in cells),
                                   max(lines[0].size for lines in cells), all(lines[0].bold for lines in cells)))
            else:
                for lines in cells:
                    out.extend(lines)
        return out

    # ── paragraphs and runs ───────────────────────────────────────────────────────────────

    def paragraph(self, paragraph: Any, cell_fill: Optional[str]) -> list[DocLine]:
        properties = paragraph.find(W + "pPr")
        style_ref = properties.find(W + "pStyle") if properties is not None else None
        style_id = style_ref.get(W + "val") if style_ref is not None else self.styles.default_paragraph
        chain = self.styles.chain(style_id)
        paragraph_sources = [properties] + [ppr for _, ppr in chain]
        numbering = _first(paragraph_sources, W + "numPr")
        number_id = numbering.find(W + "numId") if numbering is not None else None
        is_list = number_id is not None and number_id.get(W + "val") not in (None, "0")
        context = {
            "para_rpr": [rpr for rpr, _ in chain],
            "fills": [_fill(_first(paragraph_sources, W + "shd")), cell_fill, self.page_fill],
        }
        segments = [_Segment()]
        boxes: list[tuple[Any, bool]] = []
        self._inline(paragraph, context, segments, boxes)
        lines = []
        for segment in segments:
            text = "".join(part for part, _ in segment.parts)
            if not text.strip():
                continue
            props = [p for part, p in segment.parts if part.strip()]
            lines.append(DocLine(text, max((p.size for p in props), default=0.0), all(p.bold for p in props)))
        if is_list and lines:
            lines[0].text = "- " + lines[0].text.lstrip()
        for box, anchored in boxes:
            if anchored:
                self.anchored_boxes += 1
            lines.extend(self.blocks(box))
        return lines

    def _inline(self, element: Any, context: dict, segments: list[_Segment], boxes: list) -> None:
        for child in element:
            tag = child.tag
            if tag == W + "r":
                self._run(child, context, segments, boxes)
            elif tag in _INLINE_CONTAINERS:
                self._inline(child, context, segments, boxes)
            elif tag == MC + "AlternateContent":
                target = child.find(MC + "Choice")
                if target is None:
                    target = child.find(MC + "Fallback")
                if target is not None:
                    self._inline(target, context, segments, boxes)
            # w:del / w:moveFrom (deleted text), w:pPr, bookmarks, proofing marks: not text

    def _props(self, run: Any, context: dict) -> _RunProps:
        rpr = run.find(W + "rPr")
        style_ref = rpr.find(W + "rStyle") if rpr is not None else None
        char_chain = [r for r, _ in self.styles.chain(style_ref.get(W + "val"))] if style_ref is not None else []
        sources = [rpr] + char_chain + context["para_rpr"] + [self.styles.defaults_rpr]
        vanish = _first(sources, W + "vanish")
        size_element = _first(sources, W + "sz")
        try:
            size = int(size_element.get(W + "val")) / 2.0 if size_element is not None else 0.0
        except (TypeError, ValueError):
            size = 0.0
        bold_element = _first(sources, W + "b")
        color = _first(sources, W + "color")
        hidden = vanish is not None and _on(vanish)
        if not hidden and size_element is not None and size < MIN_VISIBLE_SIZE_PT:
            hidden = True
        if not hidden and color is not None and is_near_white(hex_to_rgb(color.get(W + "val"))):
            highlight = _first(sources, W + "highlight")
            highlighted = highlight is not None and (highlight.get(W + "val") or "none") not in ("none", "white")
            fills = [_fill(_first(sources, W + "shd"))] + context["fills"]
            hidden = not highlighted and not any(_dark(fill) for fill in fills)
        return _RunProps(hidden, size, bold_element is not None and _on(bold_element))

    def _run(self, run: Any, context: dict, segments: list[_Segment], boxes: list) -> None:
        props = self._props(run, context)

        def add(text: str) -> None:
            if props.hidden:
                self.hidden_chars += sum(1 for c in text if c.isprintable() and not c.isspace())
            elif text:
                segments[-1].parts.append((text, props))

        for child in run:
            tag = child.tag
            if tag == W + "t":
                add(child.text or "")
            elif tag in (W + "tab", W + "ptab"):
                add("\t")
            elif tag in (W + "br", W + "cr"):
                segments.append(_Segment())
            elif tag == W + "noBreakHyphen":
                add("-")
            elif tag == W + "softHyphen":
                add(SOFT_HYPHEN)
            elif tag == W + "sym":
                code = child.get(W + "char") or ""
                try:
                    add(chr(int(code, 16)))
                except (ValueError, OverflowError):
                    pass
            elif tag in (W + "drawing", W + "pict", MC + "AlternateContent"):
                boxes.extend(_text_boxes(child))


def _parts(document: Any, reltype: str) -> list[Any]:
    parts = []
    for rel in document.part.rels.values():
        if rel.reltype == reltype and not rel.is_external:
            parts.append(rel.target_part)
    return sorted(parts, key=lambda part: str(part.partname))


def extract_docx(data: bytes) -> DocxResult:
    try:
        import docx
    except ImportError:
        return DocxResult([], set(), Flag.UNREADABLE)  # no DOCX reader installed
    try:
        document = docx.Document(io.BytesIO(data))
        reader = _Reader(document)
        header = [line for part in _parts(document, _RT_HEADER) for line in reader.blocks(part.element)]
        if document.element.body is None:  # a w:document without w:body: nothing to read
            return DocxResult([], set(), Flag.CORRUPT)
        body = reader.blocks(document.element.body)
        footer = [line for part in _parts(document, _RT_FOOTER) for line in reader.blocks(part.element)]
    except Exception as exc:  # bad zip, missing parts, not a Word document, malformed XML
        if internal_fault(exc):  # a defect of this reader is not a damaged file
            raise
        return DocxResult([], set(), Flag.CORRUPT)
    lines = [line for line in header if not is_page_number(line.text)] + body
    lines += [line for line in footer if not is_page_number(line.text)]
    flags: set[Flag] = set()
    if reader.hidden_chars:
        flags.add(Flag.STUFFING_SUSPECTED)
    if reader.anchored_boxes:
        flags.add(Flag.COLUMN_ORDER_UNCERTAIN)
    return DocxResult(lines, flags)
