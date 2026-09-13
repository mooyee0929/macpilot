"""On-device text recognition with the Vision framework."""

from __future__ import annotations

from dataclasses import dataclass

import Vision

DEFAULT_LANGUAGES = ["zh-Hant", "zh-Hans", "en-US", "ja-JP"]


@dataclass(frozen=True)
class TextBox:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


class OCRError(RuntimeError):
    pass


_FULLWIDTH = {chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)}
_FULLWIDTH["\u3000"] = " "


def _has_cjk(text: str) -> bool:
    return any(
        "\u4e00" <= ch <= "\u9fff"
        or "\u3040" <= ch <= "\u30ff"
        or "\u3400" <= ch <= "\u4dbf"
        or "\uac00" <= ch <= "\ud7af"
        for ch in text
    )


def normalize_punctuation(text: str) -> str:
    """Turn full-width ASCII back into ASCII when a line has no CJK text at all.

    With a CJK language first in the list Vision emits "（" and "，" for code and logs;
    lines that do contain CJK are left untouched so Chinese punctuation survives.
    """
    if _has_cjk(text):
        return text
    return "".join(_FULLWIDTH.get(ch, ch) for ch in text)


def normalized_to_points(
    nx: float, ny: float, nw: float, nh: float, width: float, height: float
) -> tuple[float, float, float, float]:
    """Vision boxes are normalized with a bottom-left origin; convert to top-left points."""
    return nx * width, (1.0 - ny - nh) * height, nw * width, nh * height


def sort_reading_order(boxes: list[TextBox], line_tolerance: float = 8.0) -> list[TextBox]:
    """Order boxes top-to-bottom, then left-to-right within roughly the same line."""
    ordered = sorted(boxes, key=lambda b: (b.y, b.x))
    out: list[TextBox] = []
    line: list[TextBox] = []
    line_y = None
    for b in ordered:
        if line_y is None or abs(b.y - line_y) <= line_tolerance:
            line.append(b)
            line_y = b.y if line_y is None else line_y
        else:
            out.extend(sorted(line, key=lambda t: t.x))
            line = [b]
            line_y = b.y
    out.extend(sorted(line, key=lambda t: t.x))
    return out


def recognize(
    image: object,
    width: float,
    height: float,
    languages: list[str] | None = None,
    fast: bool = False,
) -> list[TextBox]:
    """Run OCR on a CGImage and return boxes in points of a width x height frame."""
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLanguages_(languages or DEFAULT_LANGUAGES)
    request.setUsesLanguageCorrection_(True)
    level = (
        Vision.VNRequestTextRecognitionLevelFast
        if fast
        else Vision.VNRequestTextRecognitionLevelAccurate
    )
    request.setRecognitionLevel_(level)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise OCRError(str(err))
    boxes: list[TextBox] = []
    for obs in request.results() or []:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        cand = candidates[0]
        bb = obs.boundingBox()
        x, y, w, h = normalized_to_points(
            bb.origin.x, bb.origin.y, bb.size.width, bb.size.height, width, height
        )
        boxes.append(
            TextBox(
                text=normalize_punctuation(str(cand.string())),
                x=round(x, 1),
                y=round(y, 1),
                width=round(w, 1),
                height=round(h, 1),
                confidence=round(float(cand.confidence()), 2),
            )
        )
    return sort_reading_order(boxes)


def format_boxes(boxes: list[TextBox]) -> str:
    """One line per box: text plus centre point, ready to click."""
    if not boxes:
        return "(no text recognised)"
    lines = []
    for b in boxes:
        cx, cy = b.center
        lines.append(f"({int(cx)},{int(cy)})\t{b.text}")
    return "\n".join(lines)
