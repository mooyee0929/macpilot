"""In-memory accessibility tree and app registry for testing the headless tools."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from macpilot import ax


class El:
    """One fake accessibility element. Identity is the object itself, like a real AX ref."""

    def __init__(
        self,
        role: str = "AXGroup",
        title: str | None = None,
        *,
        value: Any = None,
        actions: Sequence[str] = (),
        children: Sequence[El] = (),
        settable: Sequence[str] = (),
        attrs: dict[str, Any] | None = None,
        status: int = ax.AX_SUCCESS,
        refuse_set: bool = False,
        on_press: Any = None,
    ) -> None:
        self.attrs: dict[str, Any] = {"AXRole": role, **(attrs or {})}
        if title is not None:
            self.attrs["AXTitle"] = title
        if value is not None:
            self.attrs["AXValue"] = value
        self.actions = list(actions)
        self.settable = set(settable)
        self.status = status
        self.refuse_set = refuse_set
        self.on_press = on_press
        self.parent: El | None = None
        self.children: list[El] = []
        for child in children:
            self.add(child)

    def add(self, child: El) -> El:
        child.parent = self
        self.children.append(child)
        return child

    def remove(self, child: El) -> None:
        self.children.remove(child)
        child.parent = None
        child.attrs.pop("AXRole")


def button(title: str, **kw: Any) -> El:
    return El("AXButton", title, actions=("AXPress",), **kw)


def text(value: str) -> El:
    return El("AXStaticText", value=value)


def field(title: str, value: str = "", **kw: Any) -> El:
    kw.setdefault("settable", ("AXValue", "AXSelectedText", "AXFocused"))
    return El("AXTextField", title, value=value, **kw)


class FakeTree:
    """TreeLayer over El objects, with a log of everything that was done."""

    def __init__(self, trusted: bool = True) -> None:
        self._trusted = trusted
        self.apps: dict[int, El] = {}
        self.focused: dict[int | None, El] = {}
        self.performed: list[tuple[El, str]] = []
        self.sets: list[tuple[El, str, Any]] = []
        self.reads = 0
        self.full_tree: dict[int, str | None] = {}
        self.full_tree_calls: list[tuple[int, bool]] = []

    def add_app(self, pid: int, *windows: El, menubar: El | None = None) -> El:
        app = El("AXApplication", attrs={"AXWindows": list(windows)})
        if menubar is not None:
            app.attrs["AXMenuBar"] = menubar
        self.apps[pid] = app
        return app

    def trusted(self) -> bool:
        return self._trusted

    def element_at(self, gx: float, gy: float) -> El | None:
        return None

    def focused_element(self, pid: int | None) -> El | None:
        return self.focused.get(pid)

    def parent(self, el: El) -> El | None:
        return el.parent

    def attribute(self, el: El, name: str) -> Any | None:
        if "AXRole" not in el.attrs:
            return None
        if name == "AXChildren":
            return list(el.children)
        return el.attrs.get(name)

    def attributes(self, el: El, names: Sequence[str]) -> dict[str, Any]:
        self.reads += 1
        return {name: self.attribute(el, name) for name in names}

    def is_settable(self, el: El, name: str) -> bool:
        return name in el.settable

    def set_attribute(self, el: El, name: str, value: Any) -> bool:
        self.sets.append((el, name, value))
        if el.refuse_set:
            return False
        if name == "AXSelectedText":
            el.attrs["AXValue"] = str(el.attrs.get("AXValue", "")) + value
        else:
            el.attrs[name] = value
        return True

    def actions(self, el: El) -> list[str]:
        return list(el.actions)

    def perform(self, el: El, action: str) -> bool:
        return self.perform_status(el, action) == ax.AX_SUCCESS

    def perform_status(self, el: El, action: str) -> int:
        self.performed.append((el, action))
        if el.status == ax.AX_SUCCESS and el.on_press is not None:
            el.on_press(el)
        return el.status

    def app_element(self, pid: int) -> El:
        return self.apps[pid]

    def enable_full_tree(self, pid: int, chromium: bool) -> str | None:
        self.full_tree_calls.append((pid, chromium))
        return self.full_tree.get(pid)


class FakeApps:
    def __init__(self, running: dict[str, int] | None = None, frontmost: int | None = None) -> None:
        self.running = dict(running or {})
        self.installed: dict[str, int] = {}
        self.frontmost = frontmost
        self.renderers: dict[int, str] = {}
        self.launched: list[str] = []

    def pid_for(self, name: str) -> int:
        for app, pid in self.running.items():
            if app.lower() == name.lower():
                return pid
        raise RuntimeError(f"no running app named {name!r}")

    def name_for(self, pid: int) -> str:
        for app, app_pid in self.running.items():
            if app_pid == pid:
                return app
        return ""

    def frontmost_pid(self) -> int | None:
        return self.frontmost

    def renderer(self, pid: int) -> Any:
        return self.renderers.get(pid)

    def launch(self, name: str) -> int:
        self.launched.append(name)
        for app, pid in self.installed.items():
            if app.lower() == name.lower():
                self.running[app] = pid
                return pid
        raise RuntimeError(f"Unable to find application named '{name}'")
