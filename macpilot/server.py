"""MCP server exposing the desktop over stdio. Run with ``macpilot-mcp``.

All coordinates are logical points relative to the top-left of one display (default: the
main display). A screenshot is rendered at that display's logical size, so a pixel in the
returned image is exactly one point you can click.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from macpilot import input as inp
from macpilot import ocr, screen, windows

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


def build_server() -> MCPServer[None]:
    server: MCPServer[None] = MCPServer(
        "macpilot",
        instructions=(
            "Control the whole macOS desktop. Workflow: screenshot -> look -> click/type -> "
            "screenshot again to verify. Coordinates are points relative to the display the "
            "screenshot came from (default main display). Never assume an action worked; take "
            "a screenshot after every action that changes the screen."
        ),
    )

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
        languages favour Traditional Chinese then English; pass ["en-US"] for code/logs."""
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

    @server.tool()
    @_guarded
    def click(
        x: float, y: float, button: Mouse = "left", count: int = 1, display: int | None = None
    ) -> str:
        """Click at a display-relative point. count=2 double-clicks; button="right" opens
        a context menu."""
        _, gx, gy = _global(display, x, y)
        inp.click(gx, gy, button=button, count=count)
        return f"clicked {button} x{count} at ({x},{y})"

    @server.tool()
    @_guarded
    def move_mouse(x: float, y: float, display: int | None = None) -> str:
        """Move the pointer without clicking (hover menus, tooltips)."""
        _, gx, gy = _global(display, x, y)
        inp.move(gx, gy)
        return f"moved to ({x},{y})"

    @server.tool()
    @_guarded
    def drag(x1: float, y1: float, x2: float, y2: float, display: int | None = None) -> str:
        """Press at (x1,y1), drag to (x2,y2), release."""
        _, g1x, g1y = _global(display, x1, y1)
        _, g2x, g2y = _global(display, x2, y2)
        inp.drag(g1x, g1y, g2x, g2y)
        return f"dragged ({x1},{y1}) -> ({x2},{y2})"

    @server.tool()
    @_guarded
    def scroll(x: float, y: float, dy: int = -5, dx: int = 0, display: int | None = None) -> str:
        """Scroll the element under (x,y) by lines. dy<0 scrolls down (see more below),
        dy>0 scrolls up."""
        _, gx, gy = _global(display, x, y)
        inp.scroll(gx, gy, dx=dx, dy=dy)
        return f"scrolled dy={dy} dx={dx} at ({x},{y})"

    @server.tool()
    @_guarded
    def type_text(text: str, delay_ms: int = 12) -> str:
        """Type Unicode text into the focused field (CJK works). Newlines become Return.
        Click the field first."""
        n = inp.type_text(text, delay=delay_ms / 1000)
        return f"typed {n} characters into {windows.frontmost_app()}"

    @server.tool()
    @_guarded
    def press_key(keys: str) -> str:
        """Press one chord: "enter", "tab", "esc", "cmd+c", "cmd+shift+t", "ctrl+left".
        Separate several chords with spaces to press them in sequence."""
        pressed = []
        for spec in keys.split():
            chord = inp.press(spec)
            pressed.append(chord.name)
            time.sleep(0.05)
        return "pressed " + ", ".join(pressed) + f" in {windows.frontmost_app()}"

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

    @server.tool()
    @_guarded
    def open_app(name: str) -> str:
        """Launch or bring to front an app by name (e.g. "Safari", "Finder", "Terminal").
        Waits until it actually has focus before returning."""
        front = windows.activate_app(name)
        if front.lower() != name.lower():
            return f"launched {name} but frontmost is still {front}; take a screenshot"
        return f"{name} is frontmost"

    @server.tool()
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
        ax = inp.has_input_access() or inp.request_input_access()
        return json.dumps({"screen_recording": cap, "accessibility": ax})

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
