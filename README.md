# macpilot

macOS "computer use" for Claude Code: an MCP server that captures any display and drives the
mouse and keyboard, so Claude can operate every app on the desktop the way the Chrome
extension lets it operate a browser tab.

## Install

```bash
uv tool install -e ~/dev/macpilot
claude mcp add --scope user macpilot -- macpilot-mcp
```

Grant the terminal app that runs Claude Code both **Screen Recording** and **Accessibility**
in System Settings > Privacy & Security, then restart the terminal. `macpilot perms` reports
the current state and triggers the system prompt if either is missing.

## Tools

| tool | what it does |
| --- | --- |
| `screenshot(display?, scale?)` | PNG of one display at its logical size; pixel = point |
| `zoom(x, y, w, h)` | native-resolution crop for reading small text |
| `click(x, y, button?, count?)` | left/right/middle, single or double |
| `move_mouse`, `drag`, `scroll` | pointer motion, drag-and-drop, wheel scrolling |
| `type_text(text)` | Unicode typing into the focused field (CJK ok) |
| `press_key("cmd+shift+t")` | key chords, several separated by spaces |
| `list_displays`, `list_windows`, `frontmost_app` | where things are |
| `open_app`, `open_url`, `wait`, `permissions` | plumbing |

Coordinates are logical points relative to the top-left of the chosen display (main display
by default). Nothing is stateful between calls.

## CLI

The same operations exist as `macpilot <cmd>` for shell use and manual testing:

```bash
macpilot shot /tmp/s.png --scale 0.5
macpilot click 640 400 --count 2
macpilot type "hello 世界"
macpilot key "cmd+l cmd+c"
```

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```
