"""MCP server exposing the desktop over stdio. Run with ``macpilot-mcp``.

Two families of tools:

* ``ax_*``: read an app's accessibility tree as text and act on elements by ref. Headless:
  the pointer, keyboard focus and clipboard are never touched, and an action with no
  accessibility route fails instead of falling back to physical input.
* Screenshot, pointer and keyboard tools. Coordinates are logical points relative to the
  top-left of one display (default: the main display); a screenshot is rendered at that
  display's logical size, so a pixel in the returned image is exactly one point to click.

With ``MACPILOT_AGENT`` set the server is one agent among several sharing the desktop, and
the tools that send physical input or bring an app to the front are not registered.
"""

from __future__ import annotations

import atexit
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from macpilot import ax, ocr, screen, windows
from macpilot import input as inp
from macpilot.desk import Desk, SystemApps, file_trace
from macpilot.session import AgentConfig, Claims

Mouse = Literal["left", "right", "middle"]


def _display_line(d: screen.Display) -> str:
    tag = " (main)" if d.main else ""
    return (
        f"display #{d.index}{tag}: {int(d.width)}x{int(d.height)} points, "
        f"origin ({int(d.x)},{int(d.y)}), scale {d.scale:g}x"
    )


def _global(display: int | None, x: float, y: float) -> tuple[screen.Display, float, float]:
    d = screen.get_display(display)
    if not d.contains(x, y):
        raise ToolError(f"({x},{y}) is outside {_display_line(d)}")
    gx, gy = d.to_global(x, y)
    return d, gx, gy


def _guarded(fn):  # type: ignore[no-untyped-def]
    """Turn our RuntimeErrors into ToolErrors so the message reaches the client."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return fn(*args, **kwargs)
        except (screen.ScreenError, inp.InputError, RuntimeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _window_note(w: windows.Window) -> str:
    d = screen.display_for_global(w.x, w.y)
    if d is None:
        return f"window origin ({int(w.x)},{int(w.y)}) is off every display"
    lx, ly = d.to_local(w.x, w.y)
    return (
        f"window '{w.title}' of {w.app}, {int(w.width)}x{int(w.height)} points, on display "
        f"#{d.index}; to click image point (px,py) use display={d.index}, "
        f"x=px+{int(lx)}, y=py+{int(ly)}"
    )


def _grab_target(
    display: int | None,
    region: list[float] | None,
    app: str | None,
    title: str | None,
) -> tuple[screen.Grab, str]:
    if app:
        w = windows.find_window(app, title)
        return screen.grab_window(w.window_id, w.x, w.y, w.width, w.height), _window_note(w)
    d = screen.get_display(display)
    if region:
        if len(region) != 4:
            raise ToolError("region must be [x, y, width, height]")
        rx, ry, rw, rh = region
        note = f"region ({rx:g},{ry:g}) {rw:g}x{rh:g} on {_display_line(d)}; add ({rx:g},{ry:g})"
        return screen.grab_display(d, (rx, ry, rw, rh)), note
    return screen.grab_display(d), _display_line(d)


AX_INSTRUCTIONS = (
    "Operate macOS apps through their accessibility tree with the ax_* tools. They never "
    "move the pointer, take keyboard focus or use the clipboard, so the human can keep "
    "working. Loop: ax_snapshot (or ax_find) -> act on a ref -> read again to verify; a "
    "successful result only means the app accepted the request. Refs look like e12 and stay "
    "valid while the element exists. Dense apps: ax_snapshot skeleton=true, then drill with "
    "root=<ref>. Errors read 'CODE: message [delivery]': retry as-is only when delivery is "
    "not_delivered; after 'uncertain' read the UI first; STALE_REF means take a new snapshot. "
    "The ax_* tools fail closed: there is no fallback to physical input. An app is claimed by "
    "the first agent that reads it, and APP_CLAIMED means another agent is driving it."
)
POINTER_INSTRUCTIONS = (
    " The screenshot, click, type_text and press_key tools drive the real pointer and "
    "keyboard and interrupt the human: use them only for what the ax_* tools cannot do. "
    "Coordinates are points relative to the display the screenshot came from (default main "
    "display); take a screenshot after every such action."
)
AGENT_INSTRUCTIONS = (
    " You are agent '{agent}', sharing this desktop with other agents and a human. Physical "
    "input tools are not available to you.{apps}"
)


def build_desk(config: AgentConfig) -> Desk:
    return Desk(
        ax.system_layer(),
        SystemApps(),
        Claims(config.home / "claims", config.agent),
        agent=config.agent,
        allowed=config.apps,
        post_key=inp.press_to_pid,
        post_text=inp.type_to_pid,
        trace=file_trace(config.trace) if config.trace is not None else None,
    )


def _instructions(config: AgentConfig) -> str:
    text = AX_INSTRUCTIONS
    if config.pointer:
        text += POINTER_INSTRUCTIONS
    if config.named:
        apps = ""
        if config.apps is not None:
            apps = " You may only operate: " + ", ".join(sorted(config.apps)) + "."
        text += AGENT_INSTRUCTIONS.format(agent=config.agent, apps=apps)
    return text


def build_server(config: AgentConfig | None = None, desk: Desk | None = None) -> MCPServer[None]:
    cfg = config if config is not None else AgentConfig.from_env(os.environ, os.getpid())
    server: MCPServer[None] = MCPServer("macpilot", instructions=_instructions(cfg))
    _register_ax_tools(server, desk if desk is not None else build_desk(cfg))

    def physical() -> Callable[[Any], Any]:
        """Register a tool only when this server may send physical input or steal focus."""
        return server.tool() if cfg.pointer else (lambda fn: fn)

    @server.tool()
    @_guarded
    def list_displays() -> str:
        """List connected displays with index, logical size, origin and scale factor."""
        return "\n".join(_display_line(d) for d in screen.list_displays())

    @server.tool()
    @_guarded
    def screenshot(display: int | None = None, scale: float = 1.0) -> list[Image | str]:
        """Capture one display (default main). Image pixels map 1:1 to click coordinates
        when scale is 1.0; use scale 0.5 for a cheaper overview (then multiply coords by 2)."""
        d = screen.get_display(display)
        png, w, h = screen.capture(d, scale=scale)
        note = f"{_display_line(d)}; image {w}x{h}; coordinate multiplier {1 / scale:g}"
        return [Image(data=png, format="png"), note]

    @server.tool()
    @_guarded
    def screenshot_window(
        app: str, title: str | None = None, scale: float = 1.0
    ) -> list[Image | str]:
        """Capture one app's window by app name (largest match, or the one whose title
        contains `title`), even if other windows cover it. The note tells you how to turn
        image points into click coordinates."""
        grab, note = _grab_target(None, None, app, title)
        png, w, h = screen.encode(grab, scale=scale)
        return [Image(data=png, format="png"), f"{note}; image {w}x{h}, multiplier {1 / scale:g}"]

    @server.tool()
    @_guarded
    def read_text(
        display: int | None = None,
        region: list[float] | None = None,
        app: str | None = None,
        title: str | None = None,
        fast: bool = False,
        languages: list[str] | None = None,
    ) -> str:
        """OCR the screen and return one line per text block as "(x,y)<TAB>text", where
        (x,y) is the block centre in click coordinates. Much cheaper than a screenshot for
        reading logs, tables or finding a button. Scope with display+region, or app(+title)
        for one window; window results are window-relative (see the note). Default
        languages favour Traditional Chinese then English; pass ["en-US"] for code/logs.
        fast=True is quicker but garbles CJK text; only use it for English screens."""
        grab, note = _grab_target(display, region, app, title)
        boxes = ocr.recognize(grab.image, grab.width, grab.height, fast=fast, languages=languages)
        return f"{note}\n{len(boxes)} blocks\n{ocr.format_boxes(boxes)}"

    @server.tool()
    @_guarded
    def zoom(
        x: float, y: float, width: float, height: float, display: int | None = None
    ) -> list[Image | str]:
        """Capture a region at native (Retina) resolution to read small text. x,y is the
        region's top-left in display points."""
        d = screen.get_display(display)
        png, w, h = screen.capture(d, region=(x, y, width, height), native=True)
        note = f"region ({x:g},{y:g}) {width:g}x{height:g} points, image {w}x{h}"
        return [Image(data=png, format="png"), note]

    @physical()
    @_guarded
    def click(
        x: float,
        y: float,
        button: Mouse = "left",
        count: int = 1,
        display: int | None = None,
        mode: ax.Mode = "auto",
    ) -> str:
        """Click at a display-relative point. count=2 double-clicks; button="right" opens
        a context menu. mode="auto" (default) asks the accessible element under the point
        to perform the click (AXPress etc.) so the real pointer is not moved, and only
        falls back to a CGEvent click when nothing pressable is there; mode="ax" never
        falls back (errors instead); mode="cgevent" always moves the pointer. The result
        says which path was used."""
        _, gx, gy = _global(display, x, y)
        out = ax.click(
            ax.system_layer(), mode, gx, gy, button, count,
            lambda: inp.click(gx, gy, button=button, count=count),
        )
        return f"clicked {button} x{count} at ({x},{y}); {out.note()}"

    @physical()
    @_guarded
    def move_mouse(x: float, y: float, display: int | None = None) -> str:
        """Move the pointer without clicking (hover menus, tooltips)."""
        _, gx, gy = _global(display, x, y)
        inp.move(gx, gy)
        return f"moved to ({x},{y})"

    @physical()
    @_guarded
    def drag(x1: float, y1: float, x2: float, y2: float, display: int | None = None) -> str:
        """Press at (x1,y1), drag to (x2,y2), release."""
        _, g1x, g1y = _global(display, x1, y1)
        _, g2x, g2y = _global(display, x2, y2)
        inp.drag(g1x, g1y, g2x, g2y)
        return f"dragged ({x1},{y1}) -> ({x2},{y2})"

    @physical()
    @_guarded
    def scroll(x: float, y: float, dy: int = -5, dx: int = 0, display: int | None = None) -> str:
        """Scroll the element under (x,y) by lines. dy<0 scrolls down (see more below),
        dy>0 scrolls up."""
        _, gx, gy = _global(display, x, y)
        inp.scroll(gx, gy, dx=dx, dy=dy)
        return f"scrolled dy={dy} dx={dx} at ({x},{y})"

    @physical()
    @_guarded
    def type_text(
        text: str, delay_ms: int = 12, mode: ax.Mode = "auto", app: str | None = None
    ) -> str:
        """Type Unicode text into the focused field (CJK works). Click the field first.
        mode="auto" (default) inserts the text through the Accessibility API into the
        focused element (no key events, the user keeps the keyboard) and falls back to
        typing key events into the frontmost app when the field is not AX-writable;
        mode="ax" never falls back; mode="cgevent" always sends key events, where
        newlines become Return. app="Notes" targets that app's focused field even when
        it is in the background (AX only; no fallback unless it is frontmost)."""
        pid = ax.pid_for_app(app) if app else None
        out = ax.type_text(
            ax.system_layer(), mode, text, pid, ax.frontmost_pid(),
            lambda: inp.type_text(text, delay=delay_ms / 1000),
        )
        target = app if app else windows.frontmost_app()
        return f"typed {len(text)} characters into {target}; {out.note()}"

    @physical()
    @_guarded
    def press_key(keys: str) -> str:
        """Press one chord: "enter", "tab", "esc", "cmd+c", "cmd+shift+t", "ctrl+left".
        Separate several chords with spaces to press them in sequence. Chords are always
        real key events delivered to the frontmost app (there is no AX equivalent)."""
        pressed = []
        for spec in keys.split():
            chord = inp.press(spec)
            pressed.append(chord.name)
            time.sleep(0.05)
        return (
            "pressed " + ", ".join(pressed) + f" in {windows.frontmost_app()}; "
            "path=cgevent; real key events were sent to the frontmost app"
        )

    @server.tool()
    @_guarded
    def list_windows(app: str | None = None) -> str:
        """List on-screen windows (app, title, bounds in global points). Filter by app name."""
        wins = windows.list_windows(app)
        return json.dumps([asdict(w) for w in wins], ensure_ascii=False, indent=1)

    @server.tool()
    @_guarded
    def frontmost_app() -> str:
        """Name of the app that currently has keyboard focus."""
        return windows.frontmost_app()

    @physical()
    @_guarded
    def open_app(name: str) -> str:
        """Launch or bring to front an app by name (e.g. "Safari", "Finder", "Terminal").
        Waits until it actually has focus before returning."""
        front = windows.activate_app(name)
        if front.lower() != name.lower():
            return f"launched {name} but frontmost is still {front}; take a screenshot"
        return f"{name} is frontmost"

    @physical()
    @_guarded
    def open_url(url: str) -> str:
        """Open a URL or file path with the default handler."""
        windows.open_url(url)
        return f"opened {url}"

    @server.tool()
    @_guarded
    def wait(seconds: float = 1.0) -> str:
        """Sleep so animations, page loads or app launches can finish (max 10s)."""
        s = max(0.0, min(seconds, 10.0))
        time.sleep(s)
        return f"waited {s:g}s"

    @server.tool()
    @_guarded
    def permissions() -> str:
        """Report whether Screen Recording and Accessibility are granted; request them if not."""
        cap = screen.has_screen_capture_access() or screen.request_screen_capture_access()
        access = inp.has_input_access() or inp.request_input_access()
        return json.dumps({"screen_recording": cap, "accessibility": access})

    return server


def _register_ax_tools(server: MCPServer[None], desk: Desk) -> None:
    atexit.register(desk.close)

    @server.tool()
    @_guarded
    def ax_snapshot(
        app: str | None = None,
        root: str | None = None,
        surface: Literal["windows", "menubar"] = "windows",
        skeleton: bool = False,
        depth: int | None = None,
        interactive_only: bool = False,
        max_nodes: int = 300,
    ) -> str:
        """Read an app's UI as an indented tree, one element per line with role, label,
        value and states. Actionable elements and containers carry [ref=eN] for the other
        ax_* tools; plain text has no ref. Works on background apps. Pass app="Notes" for
        its windows, surface="menubar" for its menus, or root=<ref> to re-read one region.
        skeleton=true reads only 3 levels: containers then show (+N children) and are drilled
        with root. interactive_only drops static text. Claims the app for this agent."""
        return desk.snapshot(app, root=root, surface=surface, skeleton=skeleton, depth=depth,
                             interactive_only=interactive_only, max_nodes=max_nodes)

    @server.tool()
    @_guarded
    def ax_find(
        query: str,
        app: str | None = None,
        role: str | None = None,
        root: str | None = None,
        limit: int = 30,
    ) -> str:
        """Find elements whose label or value contains `query` (case-insensitive) anywhere
        in an app's windows, or under root=<ref>. role narrows it ("button", "textfield",
        "link", "text"). Faster and cheaper than a full snapshot when you know the label."""
        return desk.find(app, query=query, role=role, root=root, limit=limit)

    @server.tool()
    @_guarded
    def ax_read(ref: str, max_chars: int = 8000) -> str:
        """All readable text under an element, in order: a document, a web page, a table,
        or the full untruncated value of a text field."""
        return desk.read(ref=ref, max_chars=max_chars)

    @server.tool()
    @_guarded
    def ax_wait(app: str, query: str, gone: bool = False, timeout: float = 10.0) -> str:
        """Wait until an element matching `query` appears in the app (or, with gone=true,
        disappears). Use after an action that loads or opens something. Max 60 s."""
        return desk.wait_for(app, query=query, gone=gone, timeout=timeout)

    @server.tool()
    @_guarded
    def ax_press(ref: str, button: Literal["left", "right"] = "left", count: int = 1) -> str:
        """Click equivalent through the accessibility API: presses a button, link, menu
        item, tab or checkbox, or selects a row. button="right" opens the context menu;
        count=2 opens (double-click) where the element supports it. No pointer movement."""
        return desk.press(ref=ref, button=button, count=count)

    @server.tool()
    @_guarded
    def ax_set_value(ref: str, text: str) -> str:
        """Replace the whole value of a text field, combo box or slider. Read back and
        reported as verified when the app shows the new value."""
        return desk.set_value(ref=ref, text=text)

    @server.tool()
    @_guarded
    def ax_insert_text(ref: str, text: str) -> str:
        """Insert text at the caret of a text field or text area, replacing the selection,
        without key events. Use ax_set_value to replace everything instead."""
        return desk.insert_text(ref=ref, text=text)

    @server.tool()
    @_guarded
    def ax_toggle(ref: str, on: bool) -> str:
        """Bring a checkbox, switch or radio button to the wanted state. Does nothing when
        it is already there, so it is safe to repeat."""
        return desk.toggle(ref=ref, on=on)

    @server.tool()
    @_guarded
    def ax_focus(ref: str) -> str:
        """Make an element the keyboard target inside its app (the app is not activated),
        so that ax_key and ax_type_keys reach it."""
        return desk.focus(ref=ref)

    @server.tool()
    @_guarded
    def ax_scroll(
        ref: str, direction: Literal["up", "down", "left", "right"] = "down", pages: int = 1
    ) -> str:
        """Scroll the scroll area that contains the element, by pages."""
        return desk.scroll(ref=ref, direction=direction, pages=pages)

    @server.tool()
    @_guarded
    def ax_perform(ref: str, action: str) -> str:
        """Run one named accessibility action on an element, e.g. "AXShowMenu",
        "AXIncrement", "AXScrollToVisible". An unknown name lists what the element offers."""
        return desk.perform(ref=ref, action=action)

    @server.tool()
    @_guarded
    def ax_key(app: str, keys: str, force: bool = False) -> str:
        """Send key chords to one app without focusing it: "return", "esc", "cmd+s",
        "cmd+shift+t"; separate several with spaces. They go to the element focused inside
        that app (ax_focus first). Prefer pressing the menu item when there is one. Quit
        chords such as cmd+q are refused unless force=true."""
        return desk.key(app, keys=keys, force=force)

    @server.tool()
    @_guarded
    def ax_type_keys(app: str, text: str) -> str:
        """Type text as key events addressed to one app, for fields the accessibility API
        cannot write (some web and Electron editors). Try ax_insert_text first."""
        return desk.type_keys(app, text=text)

    @server.tool()
    @_guarded
    def ax_launch(app: str) -> str:
        """Start an app in the background (it does not come to the front) and claim it."""
        return desk.launch(app)

    @server.tool()
    @_guarded
    def ax_release(app: str) -> str:
        """Give up this agent's claim on an app so another agent can drive it."""
        return desk.release(app)

    @server.tool()
    @_guarded
    def ax_claims() -> str:
        """Which agent is driving which app right now."""
        return desk.claims_report()


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
