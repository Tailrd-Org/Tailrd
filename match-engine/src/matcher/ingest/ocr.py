"""OCR for scanned pages and image files (SPEC §14.1; brief row 7): rapidocr-onnxruntime.

rapidocr, onnxruntime, numpy and Pillow are imported on first use only (DECISIONS D-02). The
engine is built once per process, and each ONNX Runtime session in it is rebuilt with one intra-op
thread, one inter-op thread and sequential execution, so the same pixels always produce the same
text and confidences (determinism, brief row 12).

Engine settings are fixed here, not in config:
- the angle classifier is off: résumé scans are upright, and in a probe on this engine it turned
  a correctly read line upside down;
- detection runs on a copy scaled to a 1600 px long side (recognition still reads crops of the
  full-resolution image), which halves the time of a 300 dpi page with the same lines found;
- every recognised line is returned (text_score 0), so the mean confidence reflects everything the
  engine saw; callers keep lines at or above KEEP_LINE_CONFIDENCE (rapidocr's own default
  text_score) as text.
`get_engine()` returns None when OCR cannot run here (not installed, models missing, a session
that cannot be made single-threaded); ingest then reports UNREADABLE.
"""

from __future__ import annotations

import io
import math
import threading
from dataclasses import dataclass
from typing import Any, Optional

RENDER_DPI = 300  # brief: scanned PDF pages are rendered at 300 dpi
MAX_IMAGE_SIDE = 5000  # px; larger renders / images are scaled down first (memory, time)
DET_LIMIT_SIDE = 1600  # px; detection input long side
KEEP_LINE_CONFIDENCE = 0.5  # rapidocr's default text_score: below it a line is not text
DEFAULT_IMAGE_DPI = 300.0  # assumed when an image file states no plausible resolution
INK_DPI = 36  # resolution of the "is there anything on this page?" check
INK_LEVEL = 250  # a grey value below this is ink


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    x0: float  # pixels
    y0: float
    x1: float
    y1: float


_ENGINE_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_engine: Any = None
_engine_loaded = False


def _single_threaded(ort: Any, session: Any) -> Any:
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 4
    return ort.InferenceSession(session._model_path, sess_options=options, providers=["CPUExecutionProvider"])


def _build_engine() -> Any:
    import onnxruntime as ort
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR(text_score=0.0, use_angle_cls=False, det_limit_type="max",
                      det_limit_side_len=DET_LIMIT_SIDE, det_model_path=None)
    for holder in (engine.text_detector.infer, engine.text_recognizer.session):
        holder.session = _single_threaded(ort, holder.session)
    return engine


def get_engine() -> Any:
    """The shared single-threaded rapidocr engine, or None when OCR is unavailable."""
    global _engine, _engine_loaded
    with _ENGINE_LOCK:
        if not _engine_loaded:
            _engine_loaded = True
            try:
                _engine = _build_engine()
            except Exception:  # missing package, missing model, unexpected rapidocr internals
                _engine = None
        return _engine


def run_ocr(rgb: Any) -> Optional[tuple[OcrLine, ...]]:
    """Every non-empty line OCR reads in an RGB uint8 array (H, W, 3), in the engine's order; None
    when OCR is unavailable or fails on this image."""
    engine = get_engine()
    if engine is None:
        return None
    try:
        with _RUN_LOCK:
            result, _ = engine(rgb)
    except Exception:
        return None
    lines = []
    for box, text, score in result or ():
        text = str(text).strip()
        if not text:
            continue
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence)) if math.isfinite(confidence) else 0.0
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        lines.append(OcrLine(text, confidence, min(xs), min(ys), max(xs), max(ys)))
    return tuple(lines)


def render_page(fitz: Any, page: Any) -> tuple[Any, float]:
    """(RGB array, pixels per point) of a PyMuPDF page at RENDER_DPI, capped at MAX_IMAGE_SIDE."""
    import numpy as np

    zoom = RENDER_DPI / 72.0
    long_side = max(page.rect.width, page.rect.height) * zoom
    if long_side > MAX_IMAGE_SIDE:
        zoom *= MAX_IMAGE_SIDE / long_side
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
    array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return array, zoom


def page_has_ink(fitz: Any, page: Any) -> bool:
    """Whether anything at all is drawn on the page (blank pages are not worth an OCR run)."""
    pix = page.get_pixmap(dpi=INK_DPI, colorspace=fitz.csGRAY, alpha=False)
    return bool(pix.samples) and min(pix.samples) < INK_LEVEL


def pil_to_array(image: Any) -> Any:
    """RGB uint8 array from a PIL image: transparency composited on white, size capped."""
    import numpy as np
    from PIL import Image

    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        canvas.alpha_composite(rgba)
        image = canvas
    rgb = image.convert("RGB")
    if max(rgb.size) > MAX_IMAGE_SIDE:
        scale = MAX_IMAGE_SIDE / max(rgb.size)
        rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                         Image.Resampling.LANCZOS)
    return np.asarray(rgb, dtype=np.uint8)


def image_to_array(data: bytes) -> tuple[Any, float]:
    """(RGB array, points per pixel) of a PNG/JPEG file. Raises when the image cannot be decoded;
    EXIF orientation is applied (phone photos)."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        try:
            oriented = ImageOps.exif_transpose(image)
        except Exception:
            oriented = image
        dpi = image.info.get("dpi")
        width = oriented.width
        array = pil_to_array(oriented)
    try:
        resolution = float(dpi[0]) if dpi else DEFAULT_IMAGE_DPI
    except (TypeError, ValueError, IndexError):
        resolution = DEFAULT_IMAGE_DPI
    if not (50.0 <= resolution <= 1200.0):  # also rejects NaN
        resolution = DEFAULT_IMAGE_DPI
    downscale = width / max(1, array.shape[1])  # > 1 when pil_to_array capped the size
    return array, 72.0 / resolution * downscale
