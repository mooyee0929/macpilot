"""Display enumeration and screenshots via CoreGraphics.

Coordinates everywhere in macpilot are *logical points* relative to the top-left corner
of one display. Screenshots are rendered at exactly that logical size, so a pixel in the
returned PNG is one point on that display: what you see is where you click.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import Quartz


@dataclass(frozen=True)
class Display:
    index: int
    display_id: int
    x: float
    y: float
    width: float
    height: float
    scale: float
    main: bool

    def to_global(self, x: float, y: float) -> tuple[float, float]:
        """Map a point relative to this display into the global coordinate space."""
        return self.x + x, self.y + y

    def to_local(self, gx: float, gy: float) -> tuple[float, float]:
        """Map a global point into this display's coordinate space."""
        return gx - self.x, gy - self.y

    def contains(self, x: float, y: float) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def contains_global(self, gx: float, gy: float) -> bool:
        return self.contains(*self.to_local(gx, gy))


@dataclass(frozen=True)
class Grab:
    """A captured CGImage plus the logical frame it covers.

    origin_x/origin_y are global points of the top-left corner; width/height are the
    logical size in points, so a point (px, py) in a scale-1 rendering of this image sits
    at global (origin_x + px, origin_y + py).
    """

    image: object
    origin_x: float
    origin_y: float
    width: float
    height: float

    @property
    def pixel_width(self) -> int:
        return int(Quartz.CGImageGetWidth(self.image))

    @property
    def pixel_height(self) -> int:
        return int(Quartz.CGImageGetHeight(self.image))


class ScreenError(RuntimeError):
    pass


def list_displays() -> list[Display]:
    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err != 0:
        raise ScreenError(f"CGGetActiveDisplayList failed with {err}")
    main_id = Quartz.CGMainDisplayID()
    out: list[Display] = []
    for i, did in enumerate(list(ids)[:count], start=1):
        bounds = Quartz.CGDisplayBounds(did)
        mode = Quartz.CGDisplayCopyDisplayMode(did)
        pixel_w = Quartz.CGDisplayModeGetPixelWidth(mode) if mode else bounds.size.width
        width = bounds.size.width
        scale = pixel_w / width if width else 1.0
        out.append(
            Display(
                index=i,
                display_id=int(did),
                x=bounds.origin.x,
                y=bounds.origin.y,
                width=width,
                height=bounds.size.height,
                scale=scale,
                main=int(did) == int(main_id),
            )
        )
    return out


def get_display(index: int | None) -> Display:
    displays = list_displays()
    if index is None:
        for d in displays:
            if d.main:
                return d
        return displays[0]
    for d in displays:
        if d.index == index:
            return d
    raise ScreenError(f"no display #{index}; have 1..{len(displays)}")


def display_for_global(gx: float, gy: float) -> Display | None:
    for d in list_displays():
        if d.contains_global(gx, gy):
            return d
    return None


def has_screen_capture_access() -> bool:
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def request_screen_capture_access() -> bool:
    return bool(Quartz.CGRequestScreenCaptureAccess())


def _require_access() -> None:
    if not has_screen_capture_access():
        raise ScreenError(
            "Screen Recording permission missing. System Settings > Privacy & Security > "
            "Screen Recording: enable the terminal app running Claude Code, then restart it."
        )


def grab_display(
    display: Display, region: tuple[float, float, float, float] | None = None
) -> Grab:
    """Capture a whole display, or a display-relative region of it."""
    _require_access()
    if region is None:
        image = Quartz.CGDisplayCreateImage(display.display_id)
        ox, oy, w, h = display.x, display.y, display.width, display.height
    else:
        rx, ry, rw, rh = region
        image = Quartz.CGDisplayCreateImageForRect(
            display.display_id, Quartz.CGRectMake(rx, ry, rw, rh)
        )
        ox, oy, w, h = display.x + rx, display.y + ry, rw, rh
    if image is None:
        raise ScreenError("CGDisplayCreateImage returned nothing")
    return Grab(image=image, origin_x=ox, origin_y=oy, width=w, height=h)


def grab_window(window_id: int, x: float, y: float, width: float, height: float) -> Grab:
    """Capture a single window by id, even when other windows overlap it."""
    _require_access()
    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution,
    )
    if image is None or Quartz.CGImageGetWidth(image) == 0:
        raise ScreenError(f"could not capture window {window_id}; is it still on screen?")
    return Grab(image=image, origin_x=x, origin_y=y, width=width, height=height)


def _write_png(image: object, path: Path) -> None:
    url = Quartz.CFURLCreateWithFileSystemPath(None, str(path), Quartz.kCFURLPOSIXPathStyle, False)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    if dest is None:
        raise ScreenError(f"cannot create PNG at {path}")
    Quartz.CGImageDestinationAddImage(dest, image, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise ScreenError(f"failed to write PNG at {path}")


def _scaled(image: object, width: int, height: int) -> object:
    colorspace = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0, colorspace, Quartz.kCGImageAlphaPremultipliedLast
    )
    if ctx is None:
        raise ScreenError("cannot create bitmap context")
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, width, height), image)
    return Quartz.CGBitmapContextCreateImage(ctx)


def encode(grab: Grab, scale: float = 1.0, native: bool = False) -> tuple[bytes, int, int]:
    """Render a grab to PNG bytes at logical size * scale (or native pixels)."""
    if native:
        out_w, out_h = grab.pixel_width, grab.pixel_height
    else:
        out_w = max(1, round(grab.width * scale))
        out_h = max(1, round(grab.height * scale))
    image = grab.image
    if (out_w, out_h) != (grab.pixel_width, grab.pixel_height):
        image = _scaled(image, out_w, out_h)
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="macpilot-")
    os.close(fd)
    path = Path(tmp)
    try:
        _write_png(image, path)
        return path.read_bytes(), out_w, out_h
    finally:
        path.unlink(missing_ok=True)


def capture(
    display: Display,
    region: tuple[float, float, float, float] | None = None,
    scale: float = 1.0,
    native: bool = False,
) -> tuple[bytes, int, int]:
    """Capture a display (or a region of it) and return PNG bytes plus pixel size."""
    return encode(grab_display(display, region), scale=scale, native=native)
