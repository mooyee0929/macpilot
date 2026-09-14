from __future__ import annotations

from typing import Any

import pytest

from macpilot.ax import AXError, Outcome, ax_click, ax_type, click, parse_mode, type_text


class Element:
    def __init__(
        self,
        role: str = "AXButton",
        title: str | None = None,
        actions: tuple[str, ...] = (),
        parent: Element | None = None,
        settable: tuple[str, ...] = (),
        attrs: dict[str, Any] | None = None,
        refuse_set: bool = False,
        refuse_perform: bool = False,
    ) -> None:
        self.attrs: dict[str, Any] = {"AXRole": role, **(attrs or {})}
        if title is not None:
            self.attrs["AXTitle"] = title
        self.actions = list(actions)
        self.parent = parent
        self.settable = set(settable)
        self.refuse_set = refuse_set
        self.refuse_perform = refuse_perform


class FakeLayer:
    """In-memory AXLayer: elements by position, focused element by pid, and a log."""

    def __init__(self, trusted: bool = True) -> None:
        self._trusted = trusted
        self.at: dict[tuple[float, float], Element] = {}
        self.focused: dict[int | None, Element] = {}
        self.performed: list[tuple[Element, str]] = []
        self.sets: list[tuple[Element, str, Any]] = []
        self.hit_tests = 0

    def trusted(self) -> bool:
        return self._trusted

    def element_at(self, gx: float, gy: float) -> Element | None:
        self.hit_tests += 1
        return self.at.get((gx, gy))

    def focused_element(self, pid: int | None) -> Element | None:
        return self.focused.get(pid)

    def parent(self, el: Element) -> Element | None:
        return el.parent

    def attribute(self, el: Element, name: str) -> Any | None:
        return el.attrs.get(name)

    def is_settable(self, el: Element, name: str) -> bool:
        return name in el.settable

    def set_attribute(self, el: Element, name: str, value: Any) -> bool:
        self.sets.append((el, name, value))
        if el.refuse_set:
            return False
        if name == "AXSelectedText":
            el.attrs["AXValue"] = el.attrs.get("AXValue", "") + value
        else:
            el.attrs[name] = value
        return True

    def actions(self, el: Element) -> list[str]:
        return el.actions

    def perform(self, el: Element, action: str) -> bool:
        self.performed.append((el, action))
        return not el.refuse_perform


class Fallback:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


# --- mode parsing ------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["ax", "AX", " auto ", "cgevent"])
def test_parse_mode_normalizes(raw: str) -> None:
    assert parse_mode(raw) == raw.strip().lower()


@pytest.mark.parametrize("bad", ["", "dom", "ax,cgevent"])
def test_parse_mode_rejects_unknown(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_mode(bad)


def test_outcome_notes_state_the_path() -> None:
    assert "path=ax" in Outcome("ax", detail="AXPress on AXButton 'OK'").note()
    assert "AXPress on AXButton 'OK'" in Outcome("ax", detail="AXPress on AXButton 'OK'").note()
    assert Outcome("cgevent").note().startswith("path=cgevent;")
    assert "fell back because nope" in Outcome("cgevent", reason="nope").note()
    assert Outcome("cgevent").moved_pointer and not Outcome("ax").moved_pointer


# --- click -------------------------------------------------------------------------------


def test_click_cgevent_mode_never_touches_ax() -> None:
    layer, fb = FakeLayer(), Fallback()
    layer.at[(10.0, 10.0)] = Element(actions=("AXPress",))
    out = click(layer, "cgevent", 10.0, 10.0, "left", 1, fb)
    assert out == Outcome("cgevent")
    assert fb.calls == 1 and layer.hit_tests == 0 and layer.performed == []


def test_click_auto_presses_element_and_skips_fallback() -> None:
    layer, fb = FakeLayer(), Fallback()
    button = Element(title="Save", actions=("AXPress",))
    layer.at[(10.0, 10.0)] = button
    out = click(layer, "auto", 10.0, 10.0, "left", 1, fb)
    assert out.path == "ax" and "AXPress on AXButton 'Save'" in out.detail
    assert layer.performed == [(button, "AXPress")]
    assert fb.calls == 0


def test_click_auto_falls_back_when_nothing_is_there() -> None:
    layer, fb = FakeLayer(), Fallback()
    out = click(layer, "auto", 5.0, 5.0, "left", 1, fb)
    assert out.path == "cgevent" and "no accessible element" in out.reason
    assert fb.calls == 1


def test_click_walks_up_to_a_pressable_ancestor() -> None:
    layer, fb = FakeLayer(), Fallback()
    button = Element(title="OK", actions=("AXPress",))
    text = Element(role="AXStaticText", parent=button)
    layer.at[(1.0, 1.0)] = text
    out = click(layer, "auto", 1.0, 1.0, "left", 1, fb)
    assert out.path == "ax" and layer.performed == [(button, "AXPress")]


def test_click_ancestor_search_is_bounded() -> None:
    layer, fb = FakeLayer(), Fallback()
    node = Element(role="AXWindow", actions=("AXPress",))
    for _ in range(4):
        node = Element(role="AXGroup", parent=node)
    layer.at[(1.0, 1.0)] = node
    out = click(layer, "auto", 1.0, 1.0, "left", 1, fb)
    assert out.path == "cgevent" and "supports none of AXPress/AXPick/AXConfirm" in out.reason
    assert layer.performed == [] and fb.calls == 1


def test_click_right_uses_show_menu_and_middle_has_no_ax_path() -> None:
    layer, fb = FakeLayer(), Fallback()
    el = Element(actions=("AXPress", "AXShowMenu"))
    layer.at[(1.0, 1.0)] = el
    assert click(layer, "auto", 1.0, 1.0, "right", 1, fb).path == "ax"
    assert layer.performed == [(el, "AXShowMenu")]
    out = click(layer, "auto", 1.0, 1.0, "middle", 1, fb)
    assert out.path == "cgevent" and "middle x1" in out.reason and fb.calls == 1


def test_double_click_prefers_open_then_press() -> None:
    layer, fb = FakeLayer(), Fallback()
    row = Element(role="AXRow", actions=("AXPress", "AXOpen"))
    layer.at[(1.0, 1.0)] = row
    click(layer, "auto", 1.0, 1.0, "left", 2, fb)
    assert layer.performed == [(row, "AXOpen")]
    plain = Element(actions=("AXPress",))
    layer.at[(2.0, 2.0)] = plain
    click(layer, "auto", 2.0, 2.0, "left", 2, fb)
    assert layer.performed[-1] == (plain, "AXPress")
    assert click(layer, "auto", 2.0, 2.0, "left", 3, fb).path == "cgevent"


def test_click_disabled_or_refused_falls_back() -> None:
    layer, fb = FakeLayer(), Fallback()
    layer.at[(1.0, 1.0)] = Element(actions=("AXPress",), attrs={"AXEnabled": False})
    layer.at[(2.0, 2.0)] = Element(actions=("AXPress",), refuse_perform=True)
    assert "is disabled" in click(layer, "auto", 1.0, 1.0, "left", 1, fb).reason
    assert "was refused" in click(layer, "auto", 2.0, 2.0, "left", 1, fb).reason
    assert fb.calls == 2


def test_click_untrusted_process_falls_back_without_hit_testing() -> None:
    layer = FakeLayer(trusted=False)
    ok, reason = ax_click(layer, 1.0, 1.0, "left", 1)
    assert not ok and "not trusted" in reason and layer.hit_tests == 0


def test_click_ax_mode_raises_instead_of_falling_back() -> None:
    layer, fb = FakeLayer(), Fallback()
    with pytest.raises(AXError, match="no accessible element"):
        click(layer, "ax", 1.0, 1.0, "left", 1, fb)
    assert fb.calls == 0


# --- type_text ---------------------------------------------------------------------------


def _field(role: str = "AXTextField", **kw: Any) -> Element:
    return Element(role=role, **kw)


def test_type_cgevent_mode_skips_ax() -> None:
    layer, fb = FakeLayer(), Fallback()
    layer.focused[None] = _field(settable=("AXSelectedText",))
    out = type_text(layer, "cgevent", "hi", None, 42, fb)
    assert out == Outcome("cgevent") and fb.calls == 1 and layer.sets == []


def test_type_inserts_at_selection() -> None:
    layer, fb = FakeLayer(), Fallback()
    field = _field(settable=("AXSelectedText", "AXValue"), attrs={"AXValue": "ab"})
    layer.focused[None] = field
    out = type_text(layer, "auto", "cd", None, 42, fb)
    assert out.path == "ax" and "inserted 2 chars" in out.detail and "unverified" not in out.detail
    assert layer.sets == [(field, "AXSelectedText", "cd")]
    assert field.attrs["AXValue"] == "abcd" and fb.calls == 0


def test_type_appends_to_value_of_single_line_field() -> None:
    layer, fb = FakeLayer(), Fallback()
    field = _field(role="AXSearchField", settable=("AXValue",), attrs={"AXValue": "foo"})
    layer.focused[None] = field
    out = type_text(layer, "auto", "bar", None, 42, fb)
    assert out.path == "ax" and "appended 3 chars" in out.detail
    assert layer.sets == [(field, "AXValue", "foobar")] and fb.calls == 0


def test_type_never_rewrites_value_of_multi_line_area() -> None:
    layer, fb = FakeLayer(), Fallback()
    layer.focused[None] = _field(role="AXTextArea", settable=("AXValue",), attrs={"AXValue": "x"})
    out = type_text(layer, "auto", "y", None, 42, fb)
    assert out.path == "cgevent" and "no writable AXSelectedText or AXValue" in out.reason
    assert layer.sets == [] and fb.calls == 1


def test_type_newlines_need_key_events_in_single_line_fields() -> None:
    layer, fb = FakeLayer(), Fallback()
    layer.focused[None] = _field(settable=("AXSelectedText",))
    out = type_text(layer, "auto", "a\nb", None, 42, fb)
    assert out.path == "cgevent" and "newlines" in out.reason and fb.calls == 1
    layer.focused[None] = _field(role="AXTextArea", settable=("AXSelectedText",))
    assert type_text(layer, "auto", "a\nb", None, 42, fb).path == "ax"


def test_type_falls_back_when_nothing_is_focused_or_selected_text_is_refused() -> None:
    layer, fb = FakeLayer(), Fallback()
    out = type_text(layer, "auto", "x", None, 42, fb)
    assert out.path == "cgevent" and "no focused element" in out.reason
    layer.focused[None] = _field(role="AXTextArea", settable=("AXSelectedText",), refuse_set=True)
    out = type_text(layer, "auto", "x", None, 42, fb)
    assert out.path == "cgevent" and fb.calls == 2


def test_type_accepted_but_unverified_does_not_double_type() -> None:
    layer, fb = FakeLayer(), Fallback()
    field = _field(settable=("AXSelectedText",), attrs={"AXValue": "stale"})
    layer.focused[None] = field
    field.attrs["AXValue"] = "stale"
    original_set = layer.set_attribute

    def stale_set(el: Element, name: str, value: Any) -> bool:
        original_set(el, name, value)
        el.attrs["AXValue"] = "stale"
        return True

    layer.set_attribute = stale_set  # type: ignore[method-assign]
    out = type_text(layer, "auto", "new", None, 42, fb)
    assert out.path == "ax" and "unverified" in out.detail and fb.calls == 0


def test_type_secure_field_is_not_verified() -> None:
    layer = FakeLayer()
    field = _field(settable=("AXSelectedText",), attrs={"AXSubrole": "AXSecureTextField"})
    layer.focused[None] = field
    ok, detail = ax_type(layer, "pw", None)
    assert ok and "unverified" not in detail


def test_type_targets_a_specific_app_by_pid() -> None:
    layer, fb = FakeLayer(), Fallback()
    notes = _field(role="AXTextArea", title="Notes", settable=("AXSelectedText",))
    layer.focused[777] = notes
    layer.focused[None] = _field(settable=("AXSelectedText",))
    out = type_text(layer, "auto", "hi", 777, 42, fb)
    assert out.path == "ax" and layer.sets == [(notes, "AXSelectedText", "hi")]


def test_type_background_app_never_falls_back_to_key_events() -> None:
    layer, fb = FakeLayer(), Fallback()
    with pytest.raises(AXError, match="not the frontmost app"):
        type_text(layer, "auto", "hi", 777, 42, fb)
    with pytest.raises(AXError, match="not frontmost"):
        type_text(layer, "cgevent", "hi", 777, 42, fb)
    assert fb.calls == 0


def test_type_frontmost_app_may_fall_back() -> None:
    layer, fb = FakeLayer(), Fallback()
    out = type_text(layer, "auto", "hi", 42, 42, fb)
    assert out.path == "cgevent" and fb.calls == 1


def test_type_ax_mode_raises() -> None:
    layer, fb = FakeLayer(), Fallback()
    with pytest.raises(AXError, match="no focused element"):
        type_text(layer, "ax", "hi", None, 42, fb)
    assert fb.calls == 0
