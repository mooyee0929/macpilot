from macpilot.ocr import (
    TextBox,
    format_boxes,
    normalize_punctuation,
    normalized_to_points,
    sort_reading_order,
)


def test_normalized_box_flips_y_axis() -> None:
    # A box hugging the bottom-left corner in Vision space is at the bottom in points.
    x, y, w, h = normalized_to_points(0.0, 0.0, 0.5, 0.1, 1000, 500)
    assert (x, y, w, h) == (0.0, 450.0, 500.0, 50.0)


def test_normalized_top_left_maps_to_origin() -> None:
    x, y, _, _ = normalized_to_points(0.0, 0.9, 0.2, 0.1, 1000, 500)
    assert (x, round(y, 6)) == (0.0, 0.0)


def _box(text: str, x: float, y: float) -> TextBox:
    return TextBox(text=text, x=x, y=y, width=40, height=10, confidence=1.0)


def test_reading_order_groups_lines_then_sorts_left_to_right() -> None:
    boxes = [_box("c", 200, 101), _box("a", 10, 100), _box("d", 10, 140), _box("b", 100, 98)]
    assert [b.text for b in sort_reading_order(boxes)] == ["a", "b", "c", "d"]


def test_format_boxes_reports_centres() -> None:
    out = format_boxes([_box("Submit", 100, 200)])
    assert out == "(120,205)\tSubmit"


def test_format_boxes_empty() -> None:
    assert "no text" in format_boxes([])


def test_normalize_fullwidth_on_ascii_only_lines() -> None:
    assert normalize_punctuation("out = f（［1， 2］）") == "out = f([1, 2])"


def test_normalize_keeps_cjk_lines() -> None:
    line = "整個螢幕截取（測試）"
    assert normalize_punctuation(line) == line
