"""Window and application queries via CoreGraphics plus small osascript helpers."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

import Quartz


@dataclass(frozen=True)
class Window:
    app: str
    title: str
    pid: int
    window_id: int
    x: float
    y: float
    width: float
    height: float
    layer: int


def list_windows(app: str | None = None) -> list[Window]:
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    infos = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    out: list[Window] = []
    for info in infos:
        owner = str(info.get("kCGWindowOwnerName", ""))
        if app and app.lower() not in owner.lower():
            continue
        bounds = info.get("kCGWindowBounds", {})
        layer = int(info.get("kCGWindowLayer", 0))
        if layer != 0:
            continue
        w = float(bounds.get("Width", 0))
        h = float(bounds.get("Height", 0))
        if w < 40 or h < 40:
            continue
        out.append(
            Window(
                app=owner,
                title=str(info.get("kCGWindowName", "")),
                pid=int(info.get("kCGWindowOwnerPID", 0)),
                window_id=int(info.get("kCGWindowNumber", 0)),
                x=float(bounds.get("X", 0)),
                y=float(bounds.get("Y", 0)),
                width=w,
                height=h,
                layer=layer,
            )
        )
    return out


def find_window(app: str, title: str | None = None) -> Window:
    """Largest on-screen window whose owner contains `app` (and title contains `title`)."""
    matches = [
        w for w in list_windows(app) if not title or title.lower() in w.title.lower()
    ]
    if not matches:
        what = f"{app!r}" + (f" with title containing {title!r}" if title else "")
        raise RuntimeError(f"no on-screen window for {what}")
    return max(matches, key=lambda w: w.width * w.height)


def _osascript(script: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"osascript exited {proc.returncode}")
    return proc.stdout.strip()


def frontmost_app() -> str:
    return _osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    )


def activate_app(name: str, timeout: float = 6.0) -> str:
    """Launch or focus an app and block until it is frontmost (or timeout expires).

    Returns the frontmost app name observed at the end, so callers can tell whether
    focus actually moved.
    """
    proc = subprocess.run(["open", "-a", name], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"open -a {name} failed")
    deadline = time.monotonic() + timeout
    front = ""
    while time.monotonic() < deadline:
        front = frontmost_app()
        if front.lower() == name.lower():
            time.sleep(0.3)
            return front
        time.sleep(0.2)
    return front


def open_url(url: str) -> str:
    proc = subprocess.run(["open", url], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"open {url} failed")
    return url
