"""Accessibility (AXUIElement) input path that leaves the pointer alone.

CGEvent clicks and key events physically move the cursor and land in whatever is
frontmost, so the user cannot keep working while an agent drives the desktop. This module
asks the target app to perform the action through the Accessibility API instead:
``AXUIElementPerformAction`` for clicks and settable ``AXSelectedText`` / ``AXValue`` for
typing. Where the app does not cooperate the caller falls back to the CGEvent path.

The HIServices functions are loaded through the ``objc`` bridge at first use, so no extra
pyobjc framework package is required. Everything that decides between the AX path and the
CGEvent fallback is written against the small ``AXLayer`` protocol so it can be unit-tested
with a fake layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Protocol

Mode = Literal["ax", "cgevent", "auto"]
MODES: tuple[Mode, ...] = ("ax", "cgevent", "auto")
Path = Literal["ax", "cgevent"]

# AXError.h values we branch on.
AX_SUCCESS = 0

# Actions to try for each (button, click count), first supported one wins.
CLICK_ACTIONS: dict[tuple[str, int], tuple[str, ...]] = {
    ("left", 1): ("AXPress", "AXPick", "AXConfirm"),
    ("left", 2): ("AXOpen", "AXConfirm", "AXPress"),
    ("right", 1): ("AXShowMenu",),
}
# How many ancestors to inspect when the hit element itself has no usable action
# (a static text sitting inside a button, for example).
ANCESTOR_DEPTH = 3

# Single-line fields where replacing AXValue with (old value + text) is a safe "append".
# Multi-line areas only get AXSelectedText insertion: rewriting the whole AXValue of an
# editor could truncate a document whose accessibility value is only partially exposed.
SINGLE_LINE_ROLES = frozenset({"AXTextField", "AXComboBox", "AXSearchField"})
MULTI_LINE_ROLES = frozenset({"AXTextArea"})

_AX_FRAMEWORK = "/System/Library/Frameworks/ApplicationServices.framework"


class AXError(RuntimeError):
    """The AX path was required (mode="ax") or could not be substituted, and it failed."""


def parse_mode(value: str) -> Mode:
    v = value.strip().lower()
    if v not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, got {value!r}")
    return v  # type: ignore[return-value]


@dataclass(frozen=True)
class Outcome:
    """Which path an input operation actually took, for the tool result."""

    path: Path
    detail: str = ""
    reason: str = ""

    @property
    def moved_pointer(self) -> bool:
        return self.path == "cgevent"

    def note(self) -> str:
        if self.path == "ax":
            return f"path=ax ({self.detail}); pointer and keyboard untouched"
        if self.reason:
            return f"path=cgevent, fell back because {self.reason}; real input events were sent"
        return "path=cgevent; real input events were sent"


class AXLayer(Protocol):
    """The handful of AX calls the decision logic needs; fakeable in tests."""

    def trusted(self) -> bool: ...

    def element_at(self, gx: float, gy: float) -> Any | None: ...

    def focused_element(self, pid: int | None) -> Any | None: ...

    def parent(self, el: Any) -> Any | None: ...

    def attribute(self, el: Any, name: str) -> Any | None: ...

    def is_settable(self, el: Any, name: str) -> bool: ...

    def set_attribute(self, el: Any, name: str, value: Any) -> bool: ...

    def actions(self, el: Any) -> list[str]: ...

    def perform(self, el: Any, action: str) -> bool: ...


def describe(layer: AXLayer, el: Any) -> str:
    role = layer.attribute(el, "AXRole") or "element"
    for name in ("AXTitle", "AXDescription", "AXPlaceholderValue"):
        label = layer.attribute(el, name)
        if isinstance(label, str) and label.strip():
            return f"{role} '{label.strip()[:40]}'"
    return str(role)


def _find_action(layer: AXLayer, el: Any, wanted: tuple[str, ...]) -> tuple[Any, str] | None:
    node = el
    for _ in range(ANCESTOR_DEPTH + 1):
        if node is None:
            return None
        supported = set(layer.actions(node))
        for action in wanted:
            if action in supported:
                return node, action
        node = layer.parent(node)
    return None


def ax_click(
    layer: AXLayer, gx: float, gy: float, button: str, count: int
) -> tuple[bool, str]:
    """Try the click through AX. Returns (ok, detail) or (False, reason)."""
    wanted = CLICK_ACTIONS.get((button, count))
    if wanted is None:
        return False, f"no AX action corresponds to {button} x{count}"
    if not layer.trusted():
        return False, "process is not trusted for Accessibility"
    el = layer.element_at(gx, gy)
    if el is None:
        return False, f"no accessible element at global ({gx:g},{gy:g})"
    found = _find_action(layer, el, wanted)
    if found is None:
        return False, f"{describe(layer, el)} supports none of {'/'.join(wanted)}"
    target, action = found
    what = describe(layer, target)
    if layer.attribute(target, "AXEnabled") is False:
        return False, f"{what} is disabled"
    if not layer.perform(target, action):
        return False, f"{action} on {what} was refused"
    return True, f"{action} on {what}"


def click(
    layer: AXLayer,
    mode: Mode,
    gx: float,
    gy: float,
    button: str,
    count: int,
    fallback: Callable[[], object],
) -> Outcome:
    """Click via AX, CGEvent, or AX-then-CGEvent according to `mode`."""
    mode = parse_mode(mode)
    if mode == "cgevent":
        fallback()
        return Outcome("cgevent")
    ok, info = ax_click(layer, gx, gy, button, count)
    if ok:
        return Outcome("ax", detail=info)
    if mode == "ax":
        raise AXError(f"AX click failed: {info}")
    fallback()
    return Outcome("cgevent", reason=info)


def _value_contains(layer: AXLayer, el: Any, text: str) -> bool | None:
    """True/False when AXValue is readable text, None when it cannot be checked."""
    if layer.attribute(el, "AXSubrole") == "AXSecureTextField":
        return None
    value = layer.attribute(el, "AXValue")
    if not isinstance(value, str):
        return None
    return text in value


def ax_type(layer: AXLayer, text: str, pid: int | None) -> tuple[bool, str]:
    """Insert `text` into the focused element of `pid` (or the frontmost app) through AX."""
    if not layer.trusted():
        return False, "process is not trusted for Accessibility"
    el = layer.focused_element(pid)
    if el is None:
        where = f"pid {pid}" if pid is not None else "the frontmost app"
        return False, f"{where} has no focused element"
    role = layer.attribute(el, "AXRole")
    what = describe(layer, el)
    if "\n" in text and role not in MULTI_LINE_ROLES:
        return False, f"text contains newlines and {what} is not a multi-line text area"
    if layer.is_settable(el, "AXSelectedText") and layer.set_attribute(
        el, "AXSelectedText", text
    ):
        detail = f"inserted {len(text)} chars at the selection of {what}"
        if _value_contains(layer, el, text) is False:
            detail += " (unverified: AXValue does not show it yet, take a screenshot)"
        return True, detail
    if role in SINGLE_LINE_ROLES and layer.is_settable(el, "AXValue"):
        current = layer.attribute(el, "AXValue")
        prefix = current if isinstance(current, str) else ""
        if layer.set_attribute(el, "AXValue", prefix + text):
            detail = f"appended {len(text)} chars to the value of {what}"
            if _value_contains(layer, el, text) is False:
                detail += " (unverified: AXValue does not show it yet, take a screenshot)"
            return True, detail
        return False, f"{what} refused the new AXValue"
    return False, f"{what} has no writable AXSelectedText or AXValue"


def type_text(
    layer: AXLayer,
    mode: Mode,
    text: str,
    pid: int | None,
    frontmost_pid: int | None,
    fallback: Callable[[], object],
) -> Outcome:
    """Type via AX, CGEvent, or AX-then-CGEvent.

    When `pid` names a specific app, the CGEvent fallback is only allowed if that app is
    frontmost; otherwise the keystrokes would land in a different app.
    """
    mode = parse_mode(mode)
    targeted = pid is not None and pid != frontmost_pid
    if mode == "cgevent":
        if targeted:
            raise AXError(
                f"mode=cgevent types into the frontmost app, but pid {pid} is not frontmost"
            )
        fallback()
        return Outcome("cgevent")
    ok, info = ax_type(layer, text, pid)
    if ok:
        return Outcome("ax", detail=info)
    if mode == "ax":
        raise AXError(f"AX typing failed: {info}")
    if targeted:
        raise AXError(
            f"AX typing failed: {info}; not falling back to key events because pid {pid} "
            "is not the frontmost app"
        )
    fallback()
    return Outcome("cgevent", reason=info)


# --- real implementation -----------------------------------------------------------------


def _load_bridge() -> SimpleNamespace:
    import objc

    ns: dict[str, Any] = {}
    bundle = objc.loadBundle("ApplicationServices", ns, bundle_path=_AX_FRAMEWORK)
    objc.loadBundleFunctions(
        bundle, ns, [("AXUIElementGetTypeID", b"Q"), ("AXValueGetTypeID", b"Q")]
    )
    objc.registerCFSignature("AXUIElementRef", b"^{__AXUIElement=}", ns["AXUIElementGetTypeID"]())
    objc.registerCFSignature("AXValueRef", b"^{__AXValue=}", ns["AXValueGetTypeID"]())
    el = b"^{__AXUIElement=}"
    objc.loadBundleFunctions(
        bundle,
        ns,
        [
            ("AXIsProcessTrusted", b"Z"),
            ("AXUIElementCreateSystemWide", el),
            ("AXUIElementCreateApplication", el + b"i"),
            ("AXUIElementCopyElementAtPosition", b"i" + el + b"ffo^" + el),
            ("AXUIElementCopyAttributeValue", b"i" + el + b"@o^@"),
            ("AXUIElementSetAttributeValue", b"i" + el + b"@@"),
            ("AXUIElementIsAttributeSettable", b"i" + el + b"@o^Z"),
            ("AXUIElementPerformAction", b"i" + el + b"@"),
            ("AXUIElementCopyActionNames", b"i" + el + b"o^@"),
        ],
    )
    return SimpleNamespace(**ns)


class SystemAX:
    """AXLayer backed by the real HIServices API."""

    def __init__(self) -> None:
        self._fn = _load_bridge()
        self._system = self._fn.AXUIElementCreateSystemWide()

    def trusted(self) -> bool:
        return bool(self._fn.AXIsProcessTrusted())

    def element_at(self, gx: float, gy: float) -> Any | None:
        err, el = self._fn.AXUIElementCopyElementAtPosition(self._system, gx, gy, None)
        return el if err == AX_SUCCESS else None

    def focused_element(self, pid: int | None) -> Any | None:
        if pid is not None:
            app = self._fn.AXUIElementCreateApplication(pid)
            return self.attribute(app, "AXFocusedUIElement")
        el = self.attribute(self._system, "AXFocusedUIElement")
        if el is not None:
            return el
        # The system-wide element sometimes answers kAXErrorCannotComplete; asking the
        # frontmost app directly is more reliable.
        front = frontmost_pid()
        if front is None:
            return None
        app = self._fn.AXUIElementCreateApplication(front)
        return self.attribute(app, "AXFocusedUIElement")

    def parent(self, el: Any) -> Any | None:
        return self.attribute(el, "AXParent")

    def attribute(self, el: Any, name: str) -> Any | None:
        err, value = self._fn.AXUIElementCopyAttributeValue(el, name, None)
        return value if err == AX_SUCCESS else None

    def is_settable(self, el: Any, name: str) -> bool:
        err, ok = self._fn.AXUIElementIsAttributeSettable(el, name, None)
        return err == AX_SUCCESS and bool(ok)

    def set_attribute(self, el: Any, name: str, value: Any) -> bool:
        return bool(self._fn.AXUIElementSetAttributeValue(el, name, value) == AX_SUCCESS)

    def actions(self, el: Any) -> list[str]:
        err, names = self._fn.AXUIElementCopyActionNames(el, None)
        if err != AX_SUCCESS or names is None:
            return []
        return [str(n) for n in names]

    def perform(self, el: Any, action: str) -> bool:
        return bool(self._fn.AXUIElementPerformAction(el, action) == AX_SUCCESS)


_layer: SystemAX | None = None


def system_layer() -> SystemAX:
    global _layer
    if _layer is None:
        _layer = SystemAX()
    return _layer


def frontmost_pid() -> int | None:
    import AppKit

    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app is not None else None


def pid_for_app(name: str) -> int:
    """PID of a running app whose name contains `name` (exact match preferred)."""
    import AppKit

    apps = AppKit.NSWorkspace.sharedWorkspace().runningApplications()
    needle = name.lower()
    partial: list[tuple[str, int]] = []
    for app in apps:
        label = str(app.localizedName() or "")
        if label.lower() == needle:
            return int(app.processIdentifier())
        if needle in label.lower():
            partial.append((label, int(app.processIdentifier())))
    if len(partial) == 1:
        return partial[0][1]
    if partial:
        names = ", ".join(sorted(label for label, _ in partial))
        raise RuntimeError(f"{name!r} matches several running apps: {names}")
    raise RuntimeError(f"no running app named {name!r}")
