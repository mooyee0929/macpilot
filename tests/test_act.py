from __future__ import annotations

import pytest
from fakes import El, FakeTree, button, field, text

from macpilot import act, ax
from macpilot.errors import DeskError
from macpilot.keys import KeyChord


class Posts:
    def __init__(self) -> None:
        self.keys: list[tuple[int, str]] = []
        self.texts: list[tuple[int, str]] = []

    def key(self, pid: int, chord: KeyChord) -> None:
        self.keys.append((pid, chord.name))

    def text(self, pid: int, text: str) -> None:
        self.texts.append((pid, text))


def _error(fn: object, *args: object, **kw: object) -> DeskError:
    with pytest.raises(DeskError) as err:
        fn(*args, **kw)  # type: ignore[operator]
    return err.value


# --- press -------------------------------------------------------------------------------


def test_press_performs_axpress_and_reports_unverified() -> None:
    layer = FakeTree()
    save = button("Save")
    assert act.press(layer, save) == "AXPress on AXButton 'Save'; delivered_unverified"
    assert layer.performed == [(save, "AXPress")]


def test_press_on_text_inside_a_button_presses_the_button() -> None:
    layer = FakeTree()
    label = text("OK")
    ok = button("OK", children=[label])
    act.press(layer, label)
    assert layer.performed == [(ok, "AXPress")]


def test_right_and_double_press_map_to_menu_and_open() -> None:
    layer = FakeTree()
    row = El("AXRow", actions=("AXPress", "AXOpen", "AXShowMenu"))
    act.press(layer, row, "right")
    act.press(layer, row, "left", 2)
    assert [a for _, a in layer.performed] == ["AXShowMenu", "AXOpen"]


def test_press_selects_a_row_that_has_no_action() -> None:
    layer = FakeTree()
    row = El("AXRow", settable=("AXSelected",))
    assert act.press(layer, row).startswith("selected AXRow")
    assert layer.sets == [(row, "AXSelected", True)] and layer.performed == []


def test_press_without_any_route_fails_closed() -> None:
    layer = FakeTree()
    err = _error(act.press, layer, El("AXImage", "Logo"))
    assert err.code == "ACTION_NOT_SUPPORTED" and err.delivery == "not_delivered"
    assert layer.performed == [] and layer.sets == []


def test_middle_click_has_no_accessibility_equivalent() -> None:
    layer = FakeTree()
    err = _error(act.press, layer, button("X"), "middle")
    assert err.code == "ACTION_NOT_SUPPORTED" and "physical input" in err.message
    assert layer.performed == []


def test_press_on_disabled_element_sends_nothing() -> None:
    layer = FakeTree()
    err = _error(act.press, layer, button("Save", attrs={"AXEnabled": False}))
    assert err.code == "ACTION_FAILED" and err.delivery == "not_delivered"
    assert layer.performed == []


def test_press_timeout_is_uncertain_and_must_not_be_retried() -> None:
    layer = FakeTree()
    popup = El("AXPopUpButton", "Font", actions=("AXPress",), status=ax.AX_CANNOT_COMPLETE)
    err = _error(act.press, layer, popup)
    assert err.code == "APP_UNRESPONSIVE" and err.delivery == "uncertain"
    assert "do not retry" in err.hint


def test_press_on_vanished_element_is_stale_and_other_refusals_are_failures() -> None:
    layer = FakeTree()
    gone = _error(act.press, layer, button("A", status=ax.AX_INVALID_ELEMENT))
    refused = _error(act.press, layer, button("B", status=-25200))
    assert gone.code == "STALE_REF"
    assert refused.code == "ACTION_FAILED" and "-25200" in refused.message


# --- perform -----------------------------------------------------------------------------


def test_perform_accepts_short_and_full_action_names() -> None:
    layer = FakeTree()
    stepper = El("AXIncrementor", "Qty", actions=("AXIncrement", "AXDecrement"))
    act.perform(layer, stepper, "increment")
    act.perform(layer, stepper, "AXDecrement")
    assert [a for _, a in layer.performed] == ["AXIncrement", "AXDecrement"]


def test_perform_unknown_action_lists_what_is_offered() -> None:
    layer = FakeTree()
    el = El("AXButton", "Go", actions=("AXPress", "Name:Copy\nTarget:0x0"))
    err = _error(act.perform, layer, el, "AXOpen")
    assert err.code == "ACTION_NOT_SUPPORTED" and err.message.endswith("it offers: AXPress")
    assert layer.performed == []


# --- values and text ---------------------------------------------------------------------


def test_set_value_replaces_and_verifies() -> None:
    layer = FakeTree()
    name = field("Name", "old")
    assert act.set_value(layer, name, "new").endswith("delivered_verified")
    assert name.attrs["AXValue"] == "new"


def test_set_value_coerces_numbers_for_sliders() -> None:
    layer = FakeTree()
    slider = El("AXSlider", "Volume", value=0.2, settable=("AXValue",))
    count = El("AXIncrementor", "Count", value=3, settable=("AXValue",))
    act.set_value(layer, slider, "0.75")
    act.set_value(layer, count, "8")
    assert slider.attrs["AXValue"] == 0.75
    assert count.attrs["AXValue"] == 8 and isinstance(count.attrs["AXValue"], int)
    err = _error(act.set_value, layer, slider, "loud")
    assert err.code == "INVALID_ARGS"


def test_set_value_needs_a_writable_value() -> None:
    layer = FakeTree()
    err = _error(act.set_value, layer, text("label"), "x")
    assert err.code == "ACTION_NOT_SUPPORTED" and layer.sets == []
    refused = _error(act.set_value, layer, field("Name", refuse_set=True), "x")
    assert refused.code == "ACTION_FAILED"


def test_set_value_that_does_not_read_back_is_unverified() -> None:
    layer = FakeTree()
    name = field("Name", "old")
    original = layer.set_attribute

    def formatting_set(el: El, attr: str, value: object) -> bool:
        original(el, attr, value)
        el.attrs["AXValue"] = "NEW"
        return True

    layer.set_attribute = formatting_set  # type: ignore[method-assign]
    out = act.set_value(layer, name, "new")
    assert "delivered_unverified" in out and "'NEW'" in out


def test_set_value_on_secure_field_is_never_called_verified() -> None:
    layer = FakeTree()
    secret = field("Password", attrs={"AXSubrole": "AXSecureTextField"})
    assert "delivered_unverified (secure field" in act.set_value(layer, secret, "pw")


def test_insert_text_focuses_the_field_then_inserts_at_the_caret() -> None:
    layer = FakeTree()
    body = El("AXTextArea", "Body", value="Hi", settable=("AXSelectedText", "AXFocused"))
    out = act.insert_text(layer, body, " there")
    assert out.endswith("delivered_verified") and body.attrs["AXValue"] == "Hi there"
    assert layer.sets == [(body, "AXFocused", True), (body, "AXSelectedText", " there")]


def test_insert_text_fails_closed_when_the_field_is_not_ax_writable() -> None:
    layer = FakeTree()
    err = _error(act.insert_text, layer, El("AXTextArea", "Editor"), "x")
    assert err.code == "ACTION_NOT_SUPPORTED" and "ax_type_keys" in err.hint


def test_focus_sets_and_verifies_keyboard_focus() -> None:
    layer = FakeTree()
    name = field("Name")
    assert act.focus(layer, name).endswith("delivered_verified")
    assert _error(act.focus, layer, text("label")).code == "ACTION_NOT_SUPPORTED"


# --- toggle ------------------------------------------------------------------------------


def _checkbox(value: int, flips: bool = True) -> El:
    def flip(el: El) -> None:
        el.attrs["AXValue"] = 1 - el.attrs["AXValue"]

    return El("AXCheckBox", "Wi-Fi", value=value, actions=("AXPress",),
              on_press=flip if flips else None)


def test_toggle_is_a_noop_when_already_in_the_wanted_state() -> None:
    layer = FakeTree()
    assert "already on; nothing sent" in act.toggle(layer, _checkbox(1), True)
    assert layer.performed == []


def test_toggle_presses_and_verifies_the_new_state() -> None:
    layer = FakeTree()
    box = _checkbox(0)
    assert act.toggle(layer, box, True).endswith("delivered_verified")
    assert box.attrs["AXValue"] == 1 and len(layer.performed) == 1
    assert act.toggle(layer, box, False).endswith("delivered_verified")


def test_toggle_that_does_not_take_is_reported_unverified() -> None:
    layer = FakeTree()
    assert "delivered_unverified" in act.toggle(layer, _checkbox(0, flips=False), True)


def test_toggle_needs_an_on_off_state() -> None:
    layer = FakeTree()
    assert _error(act.toggle, layer, button("Go"), True).code == "ACTION_NOT_SUPPORTED"
    assert _error(act.toggle, layer, field("Name", "x"), True).code == "ACTION_NOT_SUPPORTED"


# --- scroll ------------------------------------------------------------------------------


def test_scroll_uses_the_scroll_areas_page_actions() -> None:
    layer = FakeTree()
    row = text("row")
    area = El("AXScrollArea", actions=("AXScrollDownByPage", "AXScrollUpByPage"),
              children=[El("AXGroup", children=[row])])
    out = act.scroll(layer, row, "down", 3)
    assert out.startswith("AXScrollDownByPage x3")
    assert layer.performed == [(area, "AXScrollDownByPage")] * 3


def test_scroll_falls_back_to_the_scroll_bar_and_clamps() -> None:
    layer = FakeTree()
    bar = El("AXScrollBar", value=0.9, settable=("AXValue",))
    area = El("AXScrollArea", attrs={"AXVerticalScrollBar": bar}, children=[text("row")])
    out = act.scroll(layer, area, "down", 2)
    assert bar.attrs["AXValue"] == 1.0 and "from 0.90 to 1.00" in out
    act.scroll(layer, area, "up")
    assert bar.attrs["AXValue"] == 0.75


def test_scroll_rejects_bad_direction_and_missing_scroll_area() -> None:
    layer = FakeTree()
    assert _error(act.scroll, layer, text("x"), "sideways").code == "INVALID_ARGS"
    assert _error(act.scroll, layer, text("x"), "down").code == "ACTION_NOT_SUPPORTED"
    bare = El("AXScrollArea")
    assert _error(act.scroll, layer, bare, "left").code == "ACTION_NOT_SUPPORTED"


# --- keys --------------------------------------------------------------------------------


@pytest.mark.parametrize("chord", ["cmd+q", "cmd+shift+q", "ctrl+cmd+q", "cmd+alt+esc",
                                   "cmd+shift+delete", "CMD+Q"])
def test_quit_chords_are_blocked_without_force(chord: str) -> None:
    layer, posts = FakeTree(), Posts()
    layer.focused[7] = field("Name")
    err = _error(act.key, layer, 7, chord, posts.key)
    assert err.code == "POLICY_DENIED" and posts.keys == []


def test_forced_quit_chord_is_posted() -> None:
    layer, posts = FakeTree(), Posts()
    layer.focused[7] = field("Name")
    act.key(layer, 7, "cmd+q", posts.key, force=True)
    assert posts.keys == [(7, "cmd+q")]


def test_return_and_escape_use_the_focused_elements_actions_first() -> None:
    layer, posts = FakeTree(), Posts()
    search = El("AXTextField", "Search", actions=("AXConfirm", "AXCancel"))
    layer.focused[7] = search
    assert "return as AXConfirm" in act.key(layer, 7, "return", posts.key)
    assert "esc as AXCancel" in act.key(layer, 7, "esc", posts.key)
    assert layer.performed == [(search, "AXConfirm"), (search, "AXCancel")] and posts.keys == []


def test_other_chords_are_posted_to_the_process() -> None:
    layer, posts = FakeTree(), Posts()
    layer.focused[7] = El("AXTextField", "Search", actions=("AXConfirm",))
    assert act.key(layer, 7, "cmd+s", posts.key) == "cmd+s posted to pid 7; delivered_unverified"
    act.key(layer, 7, "shift+return", posts.key)
    assert posts.keys == [(7, "cmd+s"), (7, "shift+return")] and layer.performed == []


def test_return_is_posted_when_the_focused_element_has_no_confirm_action() -> None:
    layer, posts = FakeTree(), Posts()
    layer.focused[7] = field("Name")
    act.key(layer, 7, "return", posts.key)
    assert posts.keys == [(7, "return")]


def test_keys_need_a_focused_element_in_the_target_app() -> None:
    layer, posts = FakeTree(), Posts()
    err = _error(act.key, layer, 7, "cmd+s", posts.key)
    assert err.code == "ACTION_FAILED" and err.delivery == "not_delivered" and posts.keys == []
    err = _error(act.type_keys, layer, 7, "hello", posts.text)
    assert err.code == "ACTION_FAILED" and posts.texts == []


def test_type_keys_posts_text_to_the_process() -> None:
    layer, posts = FakeTree(), Posts()
    layer.focused[7] = El("AXTextArea", "Editor")
    out = act.type_keys(layer, 7, "héllo", posts.text)
    assert posts.texts == [(7, "héllo")] and "typed 5 characters" in out
