"""Small CLI mirroring the MCP tools for use from a shell (and for quick manual tests).

    macpilot displays
    macpilot shot out.png [--display N] [--scale 0.5]
    macpilot zoom out.png X Y W H
    macpilot click X Y [--button right] [--count 2] [--mode auto|ax|cgevent]
    macpilot move X Y | drag X1 Y1 X2 Y2 | scroll X Y DY
    macpilot winshot out.png APP [--title T]
    macpilot ocr [--display N | --app APP [--title T] | --region X Y W H] [--fast]
    macpilot type "text" [--mode auto|ax|cgevent] [--app APP]
    macpilot key "cmd+c" | windows [APP] | front | open APP | perms
    macpilot snapshot APP [--skeleton] [-i] [--menubar] [--depth N] [--max-nodes N]
    macpilot find APP QUERY [--role ROLE]
    macpilot claims
    macpilot fleet PLAN.toml [--dry-run]

snapshot and find read an app's accessibility tree without claiming it; refs only live inside
a running MCP server, so the CLI is for looking, not acting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from macpilot import ax, fleet, ocr, screen, windows
from macpilot import input as inp
from macpilot.desk import Desk, SystemApps
from macpilot.session import AgentConfig, Claims


def _display_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--display", type=int, default=None)


def _global(display: int | None, x: float, y: float) -> tuple[float, float]:
    d = screen.get_display(display)
    if not d.contains(x, y):
        raise SystemExit(f"({x},{y}) is outside display #{d.index} {d.width}x{d.height}")
    return d.to_global(x, y)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="macpilot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("displays")
    sub.add_parser("perms")
    sub.add_parser("front")

    p = sub.add_parser("shot")
    p.add_argument("out", type=Path)
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--native", action="store_true")
    _display_arg(p)

    p = sub.add_parser("zoom")
    p.add_argument("out", type=Path)
    for name in ("x", "y", "w", "h"):
        p.add_argument(name, type=float)
    _display_arg(p)

    p = sub.add_parser("winshot")
    p.add_argument("out", type=Path)
    p.add_argument("app")
    p.add_argument("--title")
    p.add_argument("--scale", type=float, default=1.0)

    p = sub.add_parser("ocr")
    p.add_argument("--app")
    p.add_argument("--title")
    p.add_argument("--region", type=float, nargs=4, metavar=("X", "Y", "W", "H"))
    p.add_argument("--fast", action="store_true")
    p.add_argument("--lang", nargs="+", help="e.g. --lang en-US")
    _display_arg(p)

    p = sub.add_parser("click")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("--button", choices=["left", "right", "middle"], default="left")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--mode", choices=list(ax.MODES), default="auto")
    _display_arg(p)

    p = sub.add_parser("move")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    _display_arg(p)

    p = sub.add_parser("drag")
    for name in ("x1", "y1", "x2", "y2"):
        p.add_argument(name, type=float)
    _display_arg(p)

    p = sub.add_parser("scroll")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("dy", type=int)
    p.add_argument("--dx", type=int, default=0)
    _display_arg(p)

    p = sub.add_parser("type")
    p.add_argument("text")
    p.add_argument("--mode", choices=list(ax.MODES), default="auto")
    p.add_argument("--app", help="type into this app's focused field via AX, even in background")

    p = sub.add_parser("key")
    p.add_argument("keys")

    p = sub.add_parser("windows")
    p.add_argument("app", nargs="?")

    p = sub.add_parser("open")
    p.add_argument("name")

    p = sub.add_parser("snapshot", help="print an app's accessibility tree")
    p.add_argument("app")
    p.add_argument("--skeleton", action="store_true", help="top 3 levels only")
    p.add_argument("-i", "--interactive-only", action="store_true")
    p.add_argument("--menubar", action="store_true", help="read the menu bar, not the windows")
    p.add_argument("--depth", type=int, default=None)
    p.add_argument("--max-nodes", type=int, default=300)

    p = sub.add_parser("find", help="search an app's accessibility tree")
    p.add_argument("app")
    p.add_argument("query")
    p.add_argument("--role")

    sub.add_parser("claims", help="which agent is driving which app")

    p = sub.add_parser("fleet", help="run several headless agents from a TOML plan")
    p.add_argument("plan", type=Path)
    p.add_argument("--dry-run", action="store_true", help="validate and print, start nothing")

    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (screen.ScreenError, inp.InputError, RuntimeError, ValueError) as exc:
        print(f"macpilot: {exc}", file=sys.stderr)
        return 1


def _run(args: argparse.Namespace) -> int:
    if args.cmd == "displays":
        for d in screen.list_displays():
            print(json.dumps(asdict(d)))
    elif args.cmd == "perms":
        cap = screen.has_screen_capture_access() or screen.request_screen_capture_access()
        access = inp.has_input_access() or inp.request_input_access()
        print(json.dumps({"screen_recording": cap, "accessibility": access}))
    elif args.cmd == "front":
        print(windows.frontmost_app())
    elif args.cmd == "shot":
        d = screen.get_display(args.display)
        png, w, h = screen.capture(d, scale=args.scale, native=args.native)
        args.out.write_bytes(png)
        print(f"display #{d.index} {int(d.width)}x{int(d.height)} points -> {args.out} ({w}x{h})")
    elif args.cmd == "zoom":
        d = screen.get_display(args.display)
        png, w, h = screen.capture(d, region=(args.x, args.y, args.w, args.h), native=True)
        args.out.write_bytes(png)
        print(f"{args.out} ({w}x{h})")
    elif args.cmd == "winshot":
        w = windows.find_window(args.app, args.title)
        grab = screen.grab_window(w.window_id, w.x, w.y, w.width, w.height)
        png, pw, ph = screen.encode(grab, scale=args.scale)
        args.out.write_bytes(png)
        print(f"{w.app} '{w.title}' at global ({int(w.x)},{int(w.y)}) -> {args.out} ({pw}x{ph})")
    elif args.cmd == "ocr":
        if args.app:
            w = windows.find_window(args.app, args.title)
            grab = screen.grab_window(w.window_id, w.x, w.y, w.width, w.height)
        else:
            d = screen.get_display(args.display)
            grab = screen.grab_display(d, tuple(args.region) if args.region else None)
        boxes = ocr.recognize(
            grab.image, grab.width, grab.height, fast=args.fast, languages=args.lang
        )
        print(ocr.format_boxes(boxes))
    elif args.cmd == "click":
        gx, gy = _global(args.display, args.x, args.y)
        out = ax.click(
            ax.system_layer(), args.mode, gx, gy, args.button, args.count,
            lambda: inp.click(gx, gy, button=args.button, count=args.count),
        )
        print(f"clicked {args.button} x{args.count} at ({args.x},{args.y}); {out.note()}")
    elif args.cmd == "move":
        gx, gy = _global(args.display, args.x, args.y)
        inp.move(gx, gy)
    elif args.cmd == "drag":
        g1 = _global(args.display, args.x1, args.y1)
        g2 = _global(args.display, args.x2, args.y2)
        inp.drag(*g1, *g2)
    elif args.cmd == "scroll":
        gx, gy = _global(args.display, args.x, args.y)
        inp.scroll(gx, gy, dx=args.dx, dy=args.dy)
    elif args.cmd == "type":
        pid = ax.pid_for_app(args.app) if args.app else None
        out = ax.type_text(
            ax.system_layer(), args.mode, args.text, pid, ax.frontmost_pid(),
            lambda: inp.type_text(args.text),
        )
        target = args.app if args.app else windows.frontmost_app()
        print(f"typed {len(args.text)} chars into {target}; {out.note()}")
    elif args.cmd == "key":
        for spec in args.keys.split():
            print(f"pressed {inp.press(spec).name}")
    elif args.cmd == "windows":
        for w in windows.list_windows(args.app):
            print(json.dumps(asdict(w), ensure_ascii=False))
    elif args.cmd == "open":
        print(f"frontmost: {windows.activate_app(args.name)}")
    elif args.cmd == "snapshot":
        print(_observer().snapshot(
            args.app,
            surface="menubar" if args.menubar else "windows",
            skeleton=args.skeleton,
            depth=args.depth,
            interactive_only=args.interactive_only,
            max_nodes=args.max_nodes,
        ))
    elif args.cmd == "find":
        print(_observer().find(args.app, query=args.query, role=args.role))
    elif args.cmd == "claims":
        config = AgentConfig.from_env(os.environ, os.getpid())
        rows = Claims(config.home / "claims", config.agent).all()
        for c in rows:
            print(json.dumps(asdict(c), ensure_ascii=False))
        if not rows:
            print("no app is claimed")
    elif args.cmd == "fleet":
        return _fleet(args.plan, args.dry_run)
    return 0


def _observer() -> Desk:
    return Desk(ax.system_layer(), SystemApps(), None)


def _fleet(plan: Path, dry_run: bool) -> int:
    specs = fleet.load_plan(plan)
    config = AgentConfig.from_env(os.environ, os.getpid())
    if dry_run:
        for spec in specs:
            print(f"{spec.name}: apps={', '.join(spec.apps)} model={spec.model or 'default'} "
                  f"timeout={spec.timeout:g}s")
        return 0
    run_dir = fleet.new_run_dir(config.home)
    print(f"starting {len(specs)} agents; logs and traces in {run_dir}", flush=True)
    results = fleet.run_plan(
        specs, run_dir, home=config.home,
        on_done=lambda r: print(fleet.format_result(r) + "\n", flush=True),
    )
    print(f"{len(results)} agents: {fleet.tally(results)}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
