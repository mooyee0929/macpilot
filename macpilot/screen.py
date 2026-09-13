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

    def contains(self, x: float, y: float) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height


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


def has_screen_capture_access() -> bool:
    return bool(Quartz.CGPreflightScreenCaptureAccess())


def request_screen_capture_access() -> bool:
    return bool(Quartz.CGRequestScreenCaptureAccess())


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


def capture(
    display: Display,
    region: tuple[float, float, float, float] | None = None,
    scale: float = 1.0,
    native: bool = False,
) -> tuple[bytes, int, int]:
    """Capture a display (or a region of it, in display-relative points) and return PNG bytes.

    scale multiplies the logical size (0.5 halves it). native=True keeps the physical
    pixel resolution, useful for reading small text.
    """
    if not has_screen_capture_access():
        raise ScreenError(
            "Screen Recording permission missing. System Settings > Privacy & Security > "
            "Screen Recording: enable the terminal app running Claude Code, then restart it."
        )
    if region is None:
        image = Quartz.CGDisplayCreateImage(display.display_id)
        logical_w, logical_h = display.width, display.height
    else:
        rx, ry, rw, rh = region
        rect = Quartz.CGRectMake(rx, ry, rw, rh)
        image = Quartz.CGDisplayCreateImageForRect(display.display_id, rect)
        logical_w, logical_h = rw, rh
    if image is None:
        raise ScreenError("CGDisplayCreateImage returned nothing")
    if native:
        out_w = Quartz.CGImageGetWidth(image)
        out_h = Quartz.CGImageGetHeight(image)
    else:
        out_w = max(1, round(logical_w * scale))
        out_h = max(1, round(logical_h * scale))
    if (out_w, out_h) != (Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)):
        image = _scaled(image, out_w, out_h)
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="macpilot-")
    os.close(fd)
    path = Path(tmp)
    try:
        _write_png(image, path)
        return path.read_bytes(), out_w, out_h
    finally:
        path.unlink(missing_ok=True)
