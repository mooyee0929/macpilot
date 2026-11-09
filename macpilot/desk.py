"""The headless desktop one agent sees: its apps, their accessibility trees and ref actions.

``Desk`` ties the pieces together and returns the text the MCP tools hand back. It claims an
app before touching it, keeps the ref table, re-verifies every ref, and records each action
in the agent's trace. Nothing here moves the pointer, activates an app or uses the clipboard.
"""

from __future__ import annotations

import functools
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Concatenate, Literal, ParamSpec, Protocol

from macpilot import act, ax, tree
from macpilot.errors import (
    APP_CLAIMED,
    APP_NOT_FOUND,
    INVALID_ARGS,
    PERM_DENIED,
    POLICY_DENIED,
    TIMEOUT,
    DeskError,
)
from macpilot.session import Claims

P = ParamSpec("P")

# Electron and Chromium do not build their accessibility tree until an assistive client asks.
# Apps recognised as one of them are asked before the first read. For anything else, fewer
# nodes than this under its windows is taken as a hint and the harmless Electron switch is
# tried once.
SPARSE_TREE = 6
ENHANCED_SETTLE = 2.0
MANUAL_SETTLE = 0.6
LAUNCH_TIMEOUT = 15.0
# How long ax_launch waits for the new app's first window to become readable.
WINDOW_READY_TIMEOUT = 6.0
MAX_WAIT = 60.0
SURFACES = ("windows", "menubar")
CHROMIUM_BUNDLES = (
    "com.google.Chrome", "org.chromium.Chromium", "com.microsoft.edgemac", "com.brave.Browser",
    "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "company.thebrowser.Browser",
)
ELECTRON_FRAMEWORK = "Contents/Frameworks/Electron Framework.framework"
CEF_FRAMEWORK = "Contents/Frameworks/Chromium Embedded Framework.framework"
Renderer = Literal["electron", "chromium"]
# Arguments whose content should not be written to a trace file.
_PRIVATE_ARGS = frozenset({"text"})

Trace = Callable[[dict[str, object]], None]


class AppLookup(Protocol):
    def pid_for(self, name: str) -> int: ...

    def name_for(self, pid: int) -> str: ...

    def frontmost_pid(self) -> int | None: ...

    def launch(self, name: str) -> int: ...

    def renderer(self, pid: int) -> Renderer | None: ...


class FullTreeLayer(tree.TreeLayer, Protocol):
    def enable_full_tree(self, pid: int, chromium: bool) -> str | None: ...


def _traced(
    fn: Callable[Concatenate[Desk, P], str],
) -> Callable[Concatenate[Desk, P], str]:
    """Record the call and its outcome in the agent's trace."""

    @functools.wraps(fn)
    def wrapper(self: Desk, /, *args: P.args, **kwargs: P.kwargs) -> str:
        entry: dict[str, object] = {
            "t": round(self._wall(), 3),
            "agent": self.agent,
            "tool": fn.__name__,
            "args": {
                k: (f"<{len(str(v))} chars>" if k in _PRIVATE_ARGS else v)
                for k, v in kwargs.items()
            },
        }
        if args:
            entry["positional"] = [str(a) for a in args]
        try:
            result = fn(self, *args, **kwargs)
        except DeskError as exc:
            entry["error"] = exc.code
            entry["delivery"] = exc.delivery
            entry["message"] = exc.message
            self._emit(entry)
            raise
        entry["result"] = result.splitlines()[0] if result else ""
        self._emit(entry)
        return result

    return wrapper


class Desk:
    def __init__(
        self,
        layer: FullTreeLayer,
        apps: AppLookup,
        claims: Claims | None,
        *,
        agent: str = "observer",
        allowed: frozenset[str] | None = None,
        post_key: act.PostKey | None = None,
        post_text: act.PostText | None = None,
        trace: Trace | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """`claims=None` builds a read-only observer (the CLI): it can read any app without
        claiming it and every action is refused."""
        self.layer = layer
        self.apps = apps
        self.claims = claims
        self.agent = agent
        self.allowed = allowed
        self.refs = tree.RefTable()
        self._post_key = post_key
        self._post_text = post_text
        self._trace = trace
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._activated: set[int] = set()

    def _emit(self, entry: dict[str, object]) -> None:
        if self._trace is not None:
            self._trace(entry)

    # --- targets -------------------------------------------------------------------------

    def _check_allowed(self, *names: str) -> None:
        if self.allowed is None:
            return
        if not any(name.strip().lower() in self.allowed for name in names):
            raise DeskError(
                POLICY_DENIED,
                f"agent '{self.agent}' may only operate: {', '.join(sorted(self.allowed))}; "
                f"{names[0]!r} is not one of them",
            )

    def _app(self, app: str) -> tuple[int, str]:
        """Resolve, authorise and claim an app. Returns (pid, display name)."""
        if not app or not app.strip():
            raise DeskError(INVALID_ARGS, "app name is empty")
        if not self.layer.trusted():
            raise DeskError(
                PERM_DENIED,
                "this process is not trusted for Accessibility",
                hint="System Settings > Privacy & Security > Accessibility: enable the "
                "terminal app that runs the agent",
            )
        try:
            pid = self.apps.pid_for(app)
        except DeskError:
            raise
        except RuntimeError as exc:
            self._check_allowed(app)
            raise DeskError(APP_NOT_FOUND, str(exc), hint="ax_launch starts it in the background") \
                from exc
        name = self.apps.name_for(pid) or app
        self._check_allowed(app, name)
        if self.claims is not None:
            self.claims.acquire(name, pid)
        return pid, name

    def _element(self, ref: str) -> tree.RefEntry:
        """The live element for a ref, in an app this agent still holds."""
        if self.claims is None:
            raise DeskError(POLICY_DENIED, "this is a read-only observer; actions are disabled")
        entry = tree.resolve(self.layer, self.refs, ref)
        if not self.claims.holds(entry.pid):
            raise DeskError(
                APP_CLAIMED,
                f"agent '{self.agent}' no longer holds the app this ref belongs to",
                hint="ax_snapshot the app again to claim it",
            )
        return entry

    def _roots(self, pid: int, surface: str) -> list[Any]:
        app_el = self.layer.app_element(pid)
        if surface == "menubar":
            bar = self.layer.attribute(app_el, "AXMenuBar")
            return [bar] if bar is not None else []
        listed = self.layer.attribute(app_el, "AXWindows")
        windows = list(listed) if listed is not None else []
        if windows:
            return windows
        # An app started in the background has not ordered its window in yet: AXWindows is
        # empty while the main window is already there and fully readable.
        for name in ("AXMainWindow", "AXFocusedWindow"):
            fallback = self.layer.attribute(app_el, name)
            if fallback is not None:
                return [fallback]
        return []

    def _wake_renderer(self, pid: int, sparse: bool) -> bool:
        """Ask an Electron / Chromium app for its full tree, once per app. True if it was
        switched on just now, in which case the windows must be read again."""
        if pid in self._activated:
            return False
        kind = self.apps.renderer(pid)
        if kind is None and not sparse:
            return False
        self._activated.add(pid)
        switched = self.layer.enable_full_tree(pid, kind == "chromium")
        if switched is None:
            return False
        self._sleep(ENHANCED_SETTLE if switched == ax.ENHANCED_USER_INTERFACE else MANUAL_SETTLE)
        return True

    # --- observation ---------------------------------------------------------------------

    @_traced
    def snapshot(
        self,
        app: str | None = None,
        *,
        root: str | None = None,
        surface: str = "windows",
        skeleton: bool = False,
        depth: int | None = None,
        interactive_only: bool = False,
        max_nodes: int = tree.DEFAULT_MAX_NODES,
    ) -> str:
        if surface not in SURFACES:
            raise DeskError(INVALID_ARGS, f"surface must be one of {', '.join(SURFACES)}")
        max_depth = depth if depth is not None else tree.DEFAULT_MAX_DEPTH
        if skeleton:
            max_depth = min(max_depth, tree.SKELETON_DEPTH)
        if root:
            entry = self._drill_root(root)
            pid, name = entry.pid, self.apps.name_for(entry.pid)
            roots, scope = [entry.el], f"subtree of {tree.normalize_ref(root)}"
        elif app:
            pid, name = self._app(app)
            roots, scope = self._roots(pid, surface), surface
        else:
            raise DeskError(INVALID_ARGS, "give an app name, or root=<ref> to drill into a region")

        def read() -> tree.Reading:
            return tree.read_tree(self.layer, roots, max_nodes=max_nodes, max_depth=max_depth,
                                  interactive_only=interactive_only)

        whole_app = not root and surface == "windows"
        if whole_app and self._wake_renderer(pid, sparse=False):
            roots = self._roots(pid, surface)
        reading = read()
        if whole_app and reading.visited < SPARSE_TREE and self._wake_renderer(pid, sparse=True):
            roots = self._roots(pid, surface)
            reading = read()
        tree.assign_refs(reading.nodes, self.refs, pid)

        lines = [self._header(pid, name, scope)]
        if not reading.nodes:
            lines.append(self._empty_note(surface, bool(roots)))
            return "\n".join(lines)
        lines.append(tree.render(reading.nodes))
        refs = sum(1 for n in reading.nodes if n.ref)
        footer = f"{len(reading.nodes)} elements, {refs} refs"
        if not reading.complete:
            footer += (
                "; INCOMPLETE: the node budget ran out. Drill into one container with "
                "root=<ref>, or use ax_find"
            )
        lines.append(footer)
        return "\n".join(lines)

    def _drill_root(self, ref: str) -> tree.RefEntry:
        entry = tree.resolve(self.layer, self.refs, ref)
        if self.claims is not None and not self.claims.holds(entry.pid):
            raise DeskError(APP_CLAIMED, f"agent '{self.agent}' no longer holds that app",
                            hint="ax_snapshot the app again to claim it")
        return entry

    def _header(self, pid: int, name: str, scope: str) -> str:
        front = self.apps.frontmost_pid() == pid
        where = "FRONTMOST (the human may be using it)" if front else "background"
        return f'app "{name}" pid {pid} | {scope} | {where} | agent {self.agent}'

    @staticmethod
    def _empty_note(surface: str, had_roots: bool) -> str:
        if surface == "menubar":
            return "this app exposes no menu bar"
        if had_roots:
            return ("the windows expose no accessibility elements (custom-drawn UI); "
                    "screenshot_window can still show it")
        return ("no windows: the app may be hidden, minimised to nothing, or still starting. "
                'Try ax_wait, or surface="menubar"')

    @_traced
    def find(
        self,
        app: str | None = None,
        *,
        query: str,
        role: str | None = None,
        root: str | None = None,
        limit: int = 30,
    ) -> str:
        if not query.strip() and not role:
            raise DeskError(INVALID_ARGS, "give a query, a role, or both")
        if root:
            entry = self._drill_root(root)
            pid, roots = entry.pid, [entry.el]
        elif app:
            pid, _ = self._app(app)
            roots = self._roots(pid, "windows")
        else:
            raise DeskError(INVALID_ARGS, "give an app name, or root=<ref> to search a region")
        reading = tree.search(self.layer, roots, query, role=role, limit=limit)
        tree.assign_refs(reading.nodes, self.refs, pid)
        if not reading.nodes:
            tail = "" if reading.complete else " (search stopped early: the tree is very large)"
            return f"no element matches {query!r}{tail}"
        note = "" if reading.complete else "\nmore may exist: narrow the query or pass root=<ref>"
        return f"{len(reading.nodes)} matches\n{tree.render(reading.nodes)}{note}"

    @_traced
    def read(self, *, ref: str, max_chars: int = 8000) -> str:
        entry = self._drill_root(ref)
        text = tree.text_of(self.layer, entry.el, limit=max(200, max_chars))
        return text or "(no readable text under this element)"

    @_traced
    def wait_for(
        self,
        app: str,
        *,
        query: str,
        gone: bool = False,
        timeout: float = 10.0,
    ) -> str:
        """Poll until an element matching `query` exists (or, with gone, no longer does)."""
        pid, _ = self._app(app)
        budget = max(0.0, min(timeout, MAX_WAIT))
        deadline = self._clock() + budget
        while True:
            reading = tree.search(self.layer, self._roots(pid, "windows"), query, limit=1)
            present = bool(reading.nodes)
            if present != gone:
                if gone:
                    return f"{query!r} is gone"
                tree.assign_refs(reading.nodes, self.refs, pid)
                return f"found\n{tree.render(reading.nodes)}"
            if self._clock() >= deadline:
                state = "still present" if gone else "did not appear"
                raise DeskError(TIMEOUT, f"{query!r} {state} within {budget:g}s",
                                hint="take an ax_snapshot to see the current state")
            self._sleep(0.3)

    # --- actions -------------------------------------------------------------------------

    @_traced
    def press(self, *, ref: str, button: str = "left", count: int = 1) -> str:
        return act.press(self.layer, self._element(ref).el, button, count)

    @_traced
    def set_value(self, *, ref: str, text: str) -> str:
        return act.set_value(self.layer, self._element(ref).el, text)

    @_traced
    def insert_text(self, *, ref: str, text: str) -> str:
        return act.insert_text(self.layer, self._element(ref).el, text)

    @_traced
    def toggle(self, *, ref: str, on: bool) -> str:
        return act.toggle(self.layer, self._element(ref).el, on)

    @_traced
    def focus(self, *, ref: str) -> str:
        return act.focus(self.layer, self._element(ref).el)

    @_traced
    def scroll(self, *, ref: str, direction: str, pages: int = 1) -> str:
        return act.scroll(self.layer, self._element(ref).el, direction, pages)

    @_traced
    def perform(self, *, ref: str, action: str) -> str:
        return act.perform(self.layer, self._element(ref).el, action)

    def _keyboard_target(self, app: str) -> int:
        if self.claims is None:
            raise DeskError(POLICY_DENIED, "this is a read-only observer; actions are disabled")
        pid, _ = self._app(app)
        return pid

    @_traced
    def key(self, app: str, *, keys: str, force: bool = False) -> str:
        if self._post_key is None:
            raise DeskError(POLICY_DENIED, "key delivery is not configured for this desk")
        pid = self._keyboard_target(app)
        chords = keys.split()
        if not chords:
            raise DeskError(INVALID_ARGS, "no key given")
        results = []
        for spec in chords:
            try:
                results.append(act.key(self.layer, pid, spec, self._post_key, force))
            except ValueError as exc:
                raise DeskError(INVALID_ARGS, str(exc)) from exc
            self._sleep(0.05)
        return "\n".join(results)

    @_traced
    def type_keys(self, app: str, *, text: str) -> str:
        if self._post_text is None:
            raise DeskError(POLICY_DENIED, "key delivery is not configured for this desk")
        return act.type_keys(self.layer, self._keyboard_target(app), text, self._post_text)

    # --- apps and claims -----------------------------------------------------------------

    @_traced
    def launch(self, app: str) -> str:
        """Start an app without bringing it to the front, then claim it."""
        if self.claims is None:
            raise DeskError(POLICY_DENIED, "this is a read-only observer; actions are disabled")
        self._check_allowed(app)
        try:
            pid = self.apps.launch(app)
        except DeskError:
            raise
        except RuntimeError as exc:
            raise DeskError(APP_NOT_FOUND, str(exc)) from exc
        name = self.apps.name_for(pid) or app
        self.claims.acquire(name, pid)
        status = (
            f'"{name}" is running in the background as pid {pid}, '
            f"claimed by agent {self.agent}"
        )
        deadline = self._clock() + WINDOW_READY_TIMEOUT
        while not self._roots(pid, "windows"):
            if self._clock() >= deadline:
                return (f"{status}; it has shown no window yet (it may be a menu-bar app, or "
                        'still starting: try ax_wait or surface="menubar")')
            self._sleep(0.25)
        return f"{status}; its window is ready for ax_snapshot"

    @_traced
    def release(self, app: str) -> str:
        if self.claims is None:
            return "observers hold no claims"
        try:
            pid = self.apps.pid_for(app)
        except RuntimeError as exc:
            raise DeskError(APP_NOT_FOUND, str(exc)) from exc
        self.refs.forget_pid(pid)
        if self.claims.release(pid):
            return f"released {app}"
        return f"agent {self.agent} did not hold {app}"

    def claims_report(self) -> str:
        if self.claims is None:
            return "observer mode: no claims"
        rows = self.claims.all()
        if not rows:
            return "no app is claimed"
        now = self._wall()
        lines = []
        for c in rows:
            you = " (you)" if self.claims.holds(c.pid) else ""
            held = max(0, int(now - c.since))
            lines.append(f"{c.app} (pid {c.pid}): agent {c.agent}{you}, for {held}s")
        return "\n".join(lines)

    def close(self) -> None:
        if self.claims is not None:
            self.claims.release_all()


# --- real implementation -----------------------------------------------------------------


class SystemApps:
    """AppLookup backed by NSWorkspace and ``open``."""

    def pid_for(self, name: str) -> int:
        return ax.pid_for_app(name)

    def _running(self, pid: int) -> Any | None:
        import AppKit

        return AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)

    def name_for(self, pid: int) -> str:
        app = self._running(pid)
        return str(app.localizedName() or "") if app is not None else ""

    def frontmost_pid(self) -> int | None:
        return ax.frontmost_pid()

    def renderer(self, pid: int) -> Renderer | None:
        app = self._running(pid)
        if app is None:
            return None
        bundle = str(app.bundleIdentifier() or "")
        if any(bundle == known or bundle.startswith(known + ".") for known in CHROMIUM_BUNDLES):
            return "chromium"
        url = app.bundleURL()
        location = Path(str(url.path())) if url is not None else None
        if location is None:
            return None
        if (location / ELECTRON_FRAMEWORK).exists():
            return "electron"
        if (location / CEF_FRAMEWORK).exists():
            return "chromium"
        return None

    def launch(self, name: str) -> int:
        # -g keeps the app in the background so the human's frontmost window stays put.
        proc = subprocess.run(["open", "-g", "-a", name], capture_output=True, text=True,
                              check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"open -g -a {name} failed")
        deadline = time.monotonic() + LAUNCH_TIMEOUT
        last = ""
        while time.monotonic() < deadline:
            try:
                return self.pid_for(name)
            except RuntimeError as exc:
                last = str(exc)
                time.sleep(0.25)
        raise RuntimeError(f"{name} did not start within {LAUNCH_TIMEOUT:g}s ({last})")


def file_trace(path: Path) -> Trace:
    """Append one JSON object per action to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(entry: dict[str, object]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return write
