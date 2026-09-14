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
| `screenshot_window(app, title?)` | one window by app name, even when covered by others |
| `read_text(display?, region?, app?, title?)` | OCR via Vision: `(x,y)<TAB>text` per block, centre = click point |
| `zoom(x, y, w, h)` | native-resolution crop for reading small text |
| `click(x, y, button?, count?, mode?)` | left/right/middle, single or double; AX press first, pointer click as fallback |
| `move_mouse`, `drag`, `scroll` | pointer motion, drag-and-drop, wheel scrolling |
| `type_text(text, mode?, app?)` | Unicode text into the focused field (CJK ok); AX insert first, key events as fallback |
| `press_key("cmd+shift+t")` | key chords, several separated by spaces (always real key events) |
| `list_displays`, `list_windows`, `frontmost_app` | where things are |
| `open_app`, `open_url`, `wait`, `permissions` | plumbing |

Coordinates are logical points relative to the top-left of the chosen display (main display
by default). Nothing is stateful between calls.

## Sharing the desktop: AX path vs real input events

Real mouse and keyboard events (CGEvent) move the system pointer and land in the frontmost
window, so while the agent is working the human cannot use the machine. `click` and
`type_text` therefore try the Accessibility API first and only post real events when they
must. The `mode` parameter controls this and the result text always says which path ran:

| `mode` | behaviour |
| --- | --- |
| `auto` (default) | try AX; fall back to CGEvent when AX cannot do it. Result: `path=ax (...)` or `path=cgevent, fell back because ...` |
| `ax` | AX only; the tool errors instead of touching the pointer or keyboard |
| `cgevent` | old behaviour: always real events |

What runs without touching the pointer:

- `click` on anything that exposes an AX action: buttons, links, checkboxes, menu items,
  tabs, table rows, toolbar items, most web page controls in Safari/Chrome. The element under
  the point (or one of its three nearest ancestors) is asked to `AXPress`, `AXPick` or
  `AXConfirm`; a double click tries `AXOpen`/`AXConfirm`/`AXPress`; a right click uses
  `AXShowMenu`. The app performs the action itself; the pointer stays where the user left it.
- `type_text` into a focused field whose `AXSelectedText` is writable (Cocoa text fields and
  text views, WebKit inputs, most native apps): the text is inserted at the caret with no key
  events. Single-line fields (`AXTextField`, `AXComboBox`, `AXSearchField`) that only expose
  a writable `AXValue` get the text appended to their value. With `app="Notes"` the text goes
  into that app's focused field even while another app is frontmost.

What still needs the real pointer or keyboard (falls back in `auto`, errors in `ax`):

- clicks on plain pixels: canvas-style apps (drawing tools, games, terminals' text grid,
  Electron apps with poor accessibility, remote desktops), custom-drawn controls, and points
  that hit only a window background or scroll area. Middle button and triple clicks have no
  AX equivalent.
- `drag`, `move_mouse` (hover) and `scroll`: these are pointer motion by definition.
- `type_text` into fields with no writable text attribute (terminals, code editors that do
  not expose `AXSelectedText`, secure fields in some apps), or text containing newlines
  aimed at a single-line field (Return there means "submit", which only a key event does).
- `press_key`: chords such as `cmd+s`, `enter` or `tab` are always real key events and go to
  the frontmost app. Even when `type_text` runs through AX, anything that needs a shortcut
  will briefly take the keyboard from the user.

The AX fallback never double-types: if the app accepted the AX insert but its `AXValue` does
not yet reflect it, the result says "unverified" and no key events are sent. Take a
screenshot to confirm.

## CLI

The same operations exist as `macpilot <cmd>` for shell use and manual testing:

```bash
macpilot shot /tmp/s.png --scale 0.5
macpilot click 640 400 --count 2
macpilot click 640 400 --mode ax          # error instead of moving the pointer
macpilot type "hello 世界"
macpilot type "hello" --app Notes         # AX insert into a background app
macpilot key "cmd+l cmd+c"
macpilot winshot /tmp/w.png Terminal
macpilot ocr --app Terminal --lang en-US
```

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```
