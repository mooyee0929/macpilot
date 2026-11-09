"""Headless actions on one accessibility element.

Everything here goes through the Accessibility API (or key events addressed to one process),
so the human's pointer, keyboard focus and clipboard are never touched. There is no fallback
to physical input: when the app offers no accessibility route the action fails closed with a
``DeskError``.

Each successful result ends with how sure we are that it landed. An AX success code only
means the app accepted the request, so most actions are ``delivered_unverified`` and the
caller is expected to read the UI again; writes that can be read back say
``delivered_verified``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from macpilot import ax
from macpilot.errors import (
    ACTION_FAILED,
    ACTION_NOT_SUPPORTED,
    APP_UNRESPONSIVE,
    INVALID_ARGS,
    POLICY_DENIED,
    STALE_REF,
    DeskError,
)
from macpilot.keys import (
    FLAG_COMMAND,
    FLAG_CONTROL,
    FLAG_OPTION,
    FLAG_SHIFT,
    KEYCODES,
    KeyChord,
    parse_chord,
)
from macpilot.tree import SCROLL_ACTIONS, TreeLayer

VERIFIED = "delivered_verified"
UNVERIFIED = "delivered_unverified"

SCROLL_BY_PAGE = {
    "up": "AXScrollUpByPage",
    "down": "AXScrollDownByPage",
    "left": "AXScrollLeftByPage",
    "right": "AXScrollRightByPage",
}
# Fraction of the scroll range moved per page when only the scroll bar can be driven.
SCROLL_BAR_STEP = 0.25
SCROLL_ANCESTORS = 8

# Chords that quit, log out or force-quit. An agent must ask for these explicitly.
_MODIFIER_MASK = FLAG_COMMAND | FLAG_CONTROL | FLAG_OPTION | FLAG_SHIFT
BLOCKED_CHORDS: frozenset[tuple[int, int]] = frozenset({
    (KEYCODES["q"], FLAG_COMMAND),
    (KEYCODES["q"], FLAG_COMMAND | FLAG_SHIFT),
    (KEYCODES["q"], FLAG_COMMAND | FLAG_CONTROL),
    (KEYCODES["escape"], FLAG_COMMAND | FLAG_OPTION),
    (KEYCODES["delete"], FLAG_COMMAND | FLAG_SHIFT),
})
# Unmodified keys that have an accessibility equivalent on the focused element.
SEMANTIC_KEYS = {KEYCODES["return"]: "AXConfirm", KEYCODES["escape"]: "AXCancel"}

PostKey = Callable[[int, KeyChord], None]
PostText = Callable[[int, str], None]


def _perform(layer: TreeLayer, el: Any, action: str, what: str) -> None:
    status = layer.perform_status(el, action)
    if status == ax.AX_SUCCESS:
        return
    if status == ax.AX_CANNOT_COMPLETE:
        raise DeskError(
            APP_UNRESPONSIVE,
            f"{action} on {what} got no answer in time; a menu or dialog may have opened",
            delivery="uncertain",
            hint="do not retry; take a new ax_snapshot to see what happened",
        )
    if status == ax.AX_INVALID_ELEMENT:
        raise DeskError(STALE_REF, f"{what} disappeared before {action} could run",
                        hint="take a new ax_snapshot and use a ref from it")
    raise DeskError(ACTION_FAILED, f"{action} on {what} was refused (AX error {status})")


def press(layer: TreeLayer, el: Any, button: str = "left", count: int = 1) -> str:
    """Click equivalent: AXPress / AXOpen / AXShowMenu, or select a row that has no action."""
    wanted = ax.CLICK_ACTIONS.get((button, count))
    what = ax.describe(layer, el)
    if wanted is None:
        raise DeskError(
            ACTION_NOT_SUPPORTED,
            f"{button} x{count} has no accessibility equivalent; it needs physical input, "
            "which the ref tools never send",
        )
    found = ax.find_action(layer, el, wanted)
    if found is None:
        if button == "left" and count == 1 and layer.is_settable(el, "AXSelected"):
            if layer.set_attribute(el, "AXSelected", True):
                return f"selected {what}; {UNVERIFIED}"
            raise DeskError(ACTION_FAILED, f"{what} refused to be selected")
        raise DeskError(
            ACTION_NOT_SUPPORTED,
            f"{what} supports none of {'/'.join(wanted)}",
            hint="ax_perform lists the actions it does support",
        )
    target, action = found
    target_name = ax.describe(layer, target)
    if layer.attribute(target, "AXEnabled") is False:
        raise DeskError(ACTION_FAILED, f"{target_name} is disabled",
                        hint="ax_wait for the UI to change, then snapshot again")
    _perform(layer, target, action, target_name)
    return f"{action} on {target_name}; {UNVERIFIED}"


def perform(layer: TreeLayer, el: Any, action: str) -> str:
    """Run one named accessibility action exactly as the element advertises it."""
    name = action.strip()
    if not name.startswith("AX"):
        name = "AX" + name[:1].upper() + name[1:]
    available = [a for a in layer.actions(el) if a.startswith("AX")]
    what = ax.describe(layer, el)
    if name not in available:
        raise DeskError(
            ACTION_NOT_SUPPORTED,
            f"{what} has no action {name}; it offers: {', '.join(available) or 'none'}",
        )
    _perform(layer, el, name, what)
    return f"{name} on {what}; {UNVERIFIED}"


def _coerce(current: Any, text: str) -> Any:
    if isinstance(current, bool) or not isinstance(current, int | float):
        return text
    try:
        number = float(text)
    except ValueError as exc:
        raise DeskError(
            INVALID_ARGS, f"this element holds a number ({current:g}); {text!r} is not one"
        ) from exc
    return int(number) if isinstance(current, int) and number.is_integer() else number


def set_value(layer: TreeLayer, el: Any, text: str) -> str:
    """Replace the element's whole value (text field contents, slider position)."""
    what = ax.describe(layer, el)
    if not layer.is_settable(el, "AXValue"):
        raise DeskError(
            ACTION_NOT_SUPPORTED,
            f"{what} has no writable value",
            hint="ax_insert_text types at the caret; ax_type_keys sends key events to the app",
        )
    new = _coerce(layer.attribute(el, "AXValue"), text)
    if not layer.set_attribute(el, "AXValue", new):
        raise DeskError(ACTION_FAILED, f"{what} refused the new value")
    if layer.attribute(el, "AXSubrole") == "AXSecureTextField":
        return f"set value of {what}; {UNVERIFIED} (secure field cannot be read back)"
    after = layer.attribute(el, "AXValue")
    if after == new:
        return f"set value of {what}; {VERIFIED}"
    shown = repr(after)[:60]
    return f"set value of {what}; {UNVERIFIED} (reads back as {shown})"


def focus(layer: TreeLayer, el: Any) -> str:
    """Make the element the keyboard target inside its own app, without activating the app."""
    what = ax.describe(layer, el)
    if not layer.is_settable(el, "AXFocused"):
        raise DeskError(ACTION_NOT_SUPPORTED, f"{what} cannot take focus")
    if not layer.set_attribute(el, "AXFocused", True):
        raise DeskError(ACTION_FAILED, f"{what} refused focus")
    state = VERIFIED if layer.attribute(el, "AXFocused") is True else UNVERIFIED
    return f"focused {what}; {state}"


def insert_text(layer: TreeLayer, el: Any, text: str) -> str:
    """Insert at the caret of a text element, replacing the selection."""
    if layer.is_settable(el, "AXFocused"):
        layer.set_attribute(el, "AXFocused", True)
    ok, info = ax.type_into(layer, el, text)
    if not ok:
        raise DeskError(
            ACTION_NOT_SUPPORTED,
            info,
            hint="ax_set_value replaces the whole value; ax_type_keys sends key events to the app",
        )
    state = UNVERIFIED if "unverified" in info else VERIFIED
    return f"{info.split(' (unverified')[0]}; {state}"


def toggle(layer: TreeLayer, el: Any, on: bool) -> str:
    """Bring a checkbox, switch or radio button to the wanted state; no-op if already there."""
    what = ax.describe(layer, el)
    current = layer.attribute(el, "AXValue")
    if isinstance(current, str) or not isinstance(current, bool | int | float):
        raise DeskError(ACTION_NOT_SUPPORTED, f"{what} has no on/off state to read",
                        hint="use ax_press and verify with a snapshot")
    wanted = "on" if on else "off"
    if bool(current) == on:
        return f"{what} is already {wanted}; nothing sent"
    press(layer, el)
    after = layer.attribute(el, "AXValue")
    if isinstance(after, bool | int | float) and bool(after) == on:
        return f"turned {what} {wanted}; {VERIFIED}"
    return f"pressed {what} to turn it {wanted}; {UNVERIFIED} (state reads back as {after!r})"


def _scroll_area(layer: TreeLayer, el: Any) -> Any | None:
    node = el
    for _ in range(SCROLL_ANCESTORS + 1):
        if node is None:
            return None
        if layer.attribute(node, "AXRole") == "AXScrollArea" or (
            set(layer.actions(node)) & SCROLL_ACTIONS
        ):
            return node
        node = layer.parent(node)
    return None


def scroll(layer: TreeLayer, el: Any, direction: str, pages: int = 1) -> str:
    """Scroll the nearest scroll area by pages, through its actions or its scroll bar."""
    way = direction.strip().lower()
    if way not in SCROLL_BY_PAGE:
        raise DeskError(
            INVALID_ARGS, f"direction must be up, down, left or right, got {direction!r}"
        )
    pages = max(1, min(pages, 20))
    area = _scroll_area(layer, el)
    if area is None:
        raise DeskError(
            ACTION_NOT_SUPPORTED, f"{ax.describe(layer, el)} is not inside a scroll area"
        )
    what = ax.describe(layer, area)
    action = SCROLL_BY_PAGE[way]
    if action in layer.actions(area):
        for _ in range(pages):
            _perform(layer, area, action, what)
        return f"{action} x{pages} on {what}; {UNVERIFIED}"
    bar_name = "AXVerticalScrollBar" if way in ("up", "down") else "AXHorizontalScrollBar"
    bar = layer.attribute(area, bar_name)
    position = layer.attribute(bar, "AXValue") if bar is not None else None
    if not isinstance(position, int | float) or not layer.is_settable(bar, "AXValue"):
        raise DeskError(ACTION_NOT_SUPPORTED, f"{what} exposes no scroll action or scroll bar")
    step = SCROLL_BAR_STEP * pages * (1 if way in ("down", "right") else -1)
    target = max(0.0, min(1.0, float(position) + step))
    if not layer.set_attribute(bar, "AXValue", target):
        raise DeskError(ACTION_FAILED, f"the scroll bar of {what} refused the new position")
    return f"moved {bar_name} of {what} from {float(position):.2f} to {target:.2f}; {UNVERIFIED}"


def _require_focus(layer: TreeLayer, pid: int) -> Any:
    focused = layer.focused_element(pid)
    if focused is None:
        raise DeskError(
            ACTION_FAILED,
            "the app has no focused element to receive keys",
            hint="ax_focus a field first, or press the menu item instead: "
            'ax_snapshot surface="menubar"',
        )
    return focused


def key(layer: TreeLayer, pid: int, spec: str, post: PostKey, force: bool = False) -> str:
    """Send one chord to one app without focusing it.

    Unmodified Return and Escape first try the focused element's AXConfirm / AXCancel; every
    other chord is a key event posted to the process, which needs a focused element there.
    """
    chord = parse_chord(spec)
    if (chord.keycode, chord.flags & _MODIFIER_MASK) in BLOCKED_CHORDS and not force:
        raise DeskError(
            POLICY_DENIED,
            f"{chord.name} quits or force-quits an app and is blocked",
            hint="only send it when the task explicitly calls for it, with force=true",
        )
    semantic = SEMANTIC_KEYS.get(chord.keycode) if not chord.flags else None
    focused = layer.focused_element(pid)
    if semantic and focused is not None and semantic in layer.actions(focused):
        what = ax.describe(layer, focused)
        _perform(layer, focused, semantic, what)
        return f"{chord.name} as {semantic} on {what}; {UNVERIFIED}"
    _require_focus(layer, pid)
    post(pid, chord)
    return f"{chord.name} posted to pid {pid}; {UNVERIFIED}"


def type_keys(layer: TreeLayer, pid: int, text: str, post: PostText) -> str:
    """Type text as key events addressed to one app, for fields AX cannot write to."""
    focused = _require_focus(layer, pid)
    post(pid, text)
    what = ax.describe(layer, focused)
    return f"typed {len(text)} characters as key events into {what}; {UNVERIFIED}"
