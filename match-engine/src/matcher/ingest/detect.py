"""File type detection and text decoding (SPEC §14.1; brief row 1).

Magic bytes decide first: "%PDF-" within the first 1024 bytes (readers allow a prefix), a zip
holding word/document.xml (DOCX), PNG, JPEG. Recognised formats we do not read (GIF, TIFF, OLE
.doc, RTF, PostScript, archives, other zips) are UNKNOWN, which ingest reports as CORRUPT. When no
magic matches, the extension decides: a ".pdf" / ".docx" / ".png" / ".jpg" whose bytes are not
that format is a damaged file (its parser fails, CORRUPT); anything else is tried as plain text:
UTF-8 with or without BOM, UTF-16 with a BOM, else Windows-1252. Decoded text with NUL bytes or
more than 1 % control characters (or undecodable bytes) is binary, not text.
"""

from __future__ import annotations

import codecs
import io
import zipfile
from enum import Enum
from typing import Optional


class Kind(str, Enum):
    PDF = "PDF"
    DOCX = "DOCX"
    IMAGE = "IMAGE"
    TEXT = "TEXT"
    UNKNOWN = "UNKNOWN"


PDF_MAGIC_WINDOW = 1024
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")
_OTHER_FORMATS = (
    b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"{\\rtf", b"%!PS",
    b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!", b"RIFF", b"BM", b"\x00\x00\x01\x00",
)
_BY_EXTENSION = {"pdf": Kind.PDF, "docx": Kind.DOCX, "png": Kind.IMAGE, "jpg": Kind.IMAGE, "jpeg": Kind.IMAGE}
MAX_CONTROL_SHARE = 0.01
_ALLOWED_CONTROLS = frozenset("\t\n\r\x0b\x0c")


def _is_word_zip(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return "word/document.xml" in archive.namelist()
    except Exception:  # a broken archive is not a readable DOCX
        return False


def detect_kind(data: bytes, filename: str = "") -> Kind:
    if b"%PDF-" in data[:PDF_MAGIC_WINDOW]:
        return Kind.PDF
    if data.startswith(_ZIP):
        return Kind.DOCX if _is_word_zip(data) else Kind.UNKNOWN
    if data.startswith(_PNG) or data.startswith(_JPEG):
        return Kind.IMAGE
    if data.startswith(_OTHER_FORMATS) and not _plausible_text_start(data):
        return Kind.UNKNOWN
    _, dot, extension = filename.rpartition(".")
    if dot and extension.lower() in _BY_EXTENSION:
        return _BY_EXTENSION[extension.lower()]
    return Kind.TEXT


def _plausible_text_start(data: bytes) -> bool:
    """'BM' also starts plain words ("BMW ..."); a real bitmap has binary bytes right after."""
    return data.startswith(b"BM") and all(32 <= b < 127 or b in (9, 10, 13) for b in data[2:16])


def decode_text(data: bytes) -> Optional[str]:
    """The decoded text, or None when the bytes are not text."""
    if data.startswith(codecs.BOM_UTF8):
        text = data[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace")
    elif data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        text = data.decode("utf-16", errors="replace")  # the codec reads and drops the BOM
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    return None if _looks_binary(text) else text


def _looks_binary(text: str) -> bool:
    if "\x00" in text:
        return True
    bad = sum(1 for c in text if c == "\ufffd" or ((c < " " or "\x7f" <= c <= "\x9f") and c not in _ALLOWED_CONTROLS))
    return bad > MAX_CONTROL_SHARE * len(text)
