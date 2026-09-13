"""Mouse and keyboard events posted through CoreGraphics (needs Accessibility permission)."""

from __future__ import annotations

import time

import Quartz

from macpilot.keys import KeyChord, parse_chord

Button = str  # "left" | "right" | "middle"

_DOWN = {
    "left": Quartz.kCGEventLeftMouseDown,
    "right": Quartz.kCGEventRightMouseDown,
    "middle": Quartz.kCGEventOtherMouseDown,
}
_UP = {
    "left": Quartz.kCGEventLeftMouseUp,
    "right": Quartz.kCGEventRightMouseUp,
    "middle": Quartz.kCGEventOtherMouseUp,
}
_DRAG = {
    "left": Quartz.kCGEventLeftMouseDragged,
    "right": Quartz.kCGEventRightMouseDragged,
    "middle": Quartz.kCGEventOtherMouseDragged,
}
_BUTTON = {
    "left": Quartz.kCGMouseButtonLeft,
    "right": Quartz.kCGMouseButtonRight,
    "middle": Quartz.kCGMouseButtonCenter,
}


class InputError(RuntimeError):
    pass


def has_input_access() -> bool:
    return bool(Quartz.CGPreflightPostEventAccess())


def request_input_access() -> bool:
    return bool(Quartz.CGRequestPostEventAccess())


def _check() -> None:
    if not has_input_access():
        raise InputError(
            "Accessibility permission missing. System Settings > Privacy & Security > "
            "Accessibility: enable the terminal app running Claude Code, then restart it."
        )


def _post(event: object) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _mouse_event(kind: int, x: float, y: float, button: Button) -> object:
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), _BUTTON[button])
    if ev is None:
        raise InputError("CGEventCreateMouseEvent failed")
    return ev


def mouse_position() -> tuple[float, float]:
    ev = Quartz.CGEventCreate(None)
    loc = Quartz.CGEventGetLocation(ev)
    return loc.x, loc.y


def move(x: float, y: float) -> None:
    _check()
    _post(_mouse_event(Quartz.kCGEventMouseMoved, x, y, "left"))


def click(x: float, y: float, button: Button = "left", count: int = 1, hold: float = 0.02) -> None:
    _check()
    if button not in _BUTTON:
        raise InputError(f"unknown button {button!r}")
    move(x, y)
    time.sleep(0.03)
    for n in range(1, count + 1):
        down = _mouse_event(_DOWN[button], x, y, button)
        up = _mouse_event(_UP[button], x, y, button)
        Quartz.CGEventSetIntegerValueField(down, Quartz.kCGMouseEventClickState, n)
        Quartz.CGEventSetIntegerValueField(up, Quartz.kCGMouseEventClickState, n)
        _post(down)
        time.sleep(hold)
        _post(up)
        if n < count:
            time.sleep(0.06)


def drag(
    x1: float, y1: float, x2: float, y2: float, button: Button = "left", steps: int = 12,
    duration: float = 0.3,
) -> None:
    _check()
    move(x1, y1)
    time.sleep(0.05)
    _post(_mouse_event(_DOWN[button], x1, y1, button))
    time.sleep(0.05)
    for i in range(1, steps + 1):
        t = i / steps
        _post(_mouse_event(_DRAG[button], x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, button))
        time.sleep(duration / steps)
    _post(_mouse_event(_UP[button], x2, y2, button))


def scroll(x: float, y: float, dx: int = 0, dy: int = 0) -> None:
    """Scroll by lines. dy>0 scrolls content up (like a finger swipe down), dy<0 down."""
    _check()
    move(x, y)
    time.sleep(0.03)
    ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, dy, dx)
    if ev is None:
        raise InputError("CGEventCreateScrollWheelEvent failed")
    _post(ev)


def _key_event(keycode: int, down: bool, flags: int = 0) -> object:
    ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
    if ev is None:
        raise InputError("CGEventCreateKeyboardEvent failed")
    if flags:
        Quartz.CGEventSetFlags(ev, flags)
    return ev


def press(spec: str, hold: float = 0.02) -> KeyChord:
    """Press a chord like "cmd+c", "enter", "cmd+shift+4"."""
    _check()
    chord = parse_chord(spec)
    _post(_key_event(chord.keycode, True, chord.flags))
    time.sleep(hold)
    _post(_key_event(chord.keycode, False, chord.flags))
    return chord


def type_text(text: str, delay: float = 0.012) -> int:
    """Type arbitrary Unicode text, one character per key event pair.

    Newlines are sent as Return so multi-line text behaves as in a real keyboard session.
    """
    _check()
    for ch in text:
        if ch == "\n":
            press("return")
        else:
            down = _key_event(0, True)
            Quartz.CGEventKeyboardSetUnicodeString(down, len(ch), ch)
            up = _key_event(0, False)
            Quartz.CGEventKeyboardSetUnicodeString(up, len(ch), ch)
            _post(down)
            _post(up)
        time.sleep(delay)
    return len(text)
