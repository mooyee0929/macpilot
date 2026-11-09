# macpilot

macOS "computer use" for Claude Code: an MCP server that lets Claude operate every app on the
desktop the way the Chrome extension lets it operate a browser tab. It has two ways of doing
so:

- **Headless (`ax_*` tools).** Read an app's accessibility tree as text, with a ref on every
  element, and act on refs. No screenshots, no coordinates, and the pointer, keyboard focus
  and clipboard are never touched, so the human keeps working and several agents can each
  drive their own app at the same time.
- **Physical (screenshot, pointer and keyboard tools).** Capture any display and send real
  mouse and key events, for what has no accessibility route.

## Install

```bash
uv tool install -e ~/dev/macpilot
claude mcp add --scope user macpilot -- macpilot-mcp
```

Grant the terminal app that runs Claude Code both **Screen Recording** and **Accessibility**
in System Settings > Privacy & Security, then restart the terminal. `macpilot perms` reports
the current state and triggers the system prompt if either is missing.

## Headless tools: read the UI as a tree, act on refs

```
ax_snapshot app="Calculator"

app "Calculator" pid 6195 | windows | background | agent solo-4242
- window "Calculator" [ref=e1]
  - group "#CalculatorKeypadView" [ref=e3] [focused]
    - scrollarea "Edit field" [ref=e4] [scrollable]
      - text "78"
    - button "All Clear" [ref=e6]
    - button "7" [ref=e9]
    ...
31 elements, 30 refs

ax_press ref="e9"   ->   AXPress on AXButton '7'; delivered_unverified
```

The loop is observe, act, observe again: an AX success code only means the app accepted the
request, so read the UI again to confirm.

| tool | what it does |
| --- | --- |
| `ax_snapshot(app?, root?, surface?, skeleton?, depth?, interactive_only?, max_nodes?)` | one line per element: role, label, value, states, ref. `surface="menubar"` reads the menus |
| `ax_find(query, app?, role?, root?)` | elements whose label or value contains the query, without a full snapshot |
| `ax_read(ref)` | all text under an element (a page, a document, a table) |
| `ax_wait(app, query, gone?, timeout?)` | wait for an element to appear or disappear |
| `ax_press(ref, button?, count?)` | click equivalent: `AXPress`, `AXOpen` for a double click, `AXShowMenu` for a right click |
| `ax_set_value(ref, text)` | replace a field's whole value (also sliders); read back and verified |
| `ax_insert_text(ref, text)` | insert at the caret through `AXSelectedText`, no key events |
| `ax_toggle(ref, on)` | bring a checkbox or switch to a state; no-op when already there |
| `ax_focus(ref)`, `ax_scroll(ref, direction, pages?)`, `ax_perform(ref, action)` | keyboard target inside the app, paging, any named AX action |
| `ax_key(app, keys, force?)` | chords delivered to one process, not to the frontmost app |
| `ax_type_keys(app, text)` | text as key events to one process, for fields AX cannot write |
| `ax_launch(app)`, `ax_release(app)`, `ax_claims()` | start an app in the background; app ownership |

How the pieces behave:

- **Refs.** Actionable elements and containers get a ref such as `e12`; plain text gets none.
  A ref follows the element's AX identity, so the same element keeps its ref across
  snapshots. Before every use the ref is checked against the live element: if it is gone or
  has become a different role the call fails with `STALE_REF`. It is never matched to a
  similar-looking element.
- **Dense apps.** `skeleton=true` reads three levels; a container that was cut shows
  `(+N children, drill with root=e7)`. Pass `root` to read just that region. A snapshot that
  runs out of its node budget says `INCOMPLETE`.
- **Sheets.** An open sheet is listed first under its window, and the window is marked
  `[sheet open]`: the controls behind a sheet are not usable.
- **Fail closed.** These tools never fall back to physical input. A failure reads
  `CODE: message [delivery]`, where delivery is `not_delivered` (safe to retry),
  `delivered_unverified`, or `uncertain` (the app did not answer, typically because a menu
  or dialog opened: read the UI before doing anything else).
- **Keys.** Unmodified Return and Escape first try `AXConfirm` / `AXCancel` on the focused
  element; everything else is posted to the target process with `CGEventPostToPid`, which
  needs a focused element inside that app (`ax_focus`). Quit chords such as `cmd+q` are
  refused unless `force=true`.
- **Electron and Chromium.** These build their accessibility tree only when asked. Electron
  apps get `AXManualAccessibility`, Chromium browsers `AXEnhancedUserInterface` followed by a
  2 s settle, once per app, before the first read.

What has no accessibility route, and so fails here: drag and drop, hover, middle and triple
clicks, and custom-drawn UI that exposes no elements (canvases, games, some terminals). Use
the physical tools for those.

The design of this half (snapshot plus refs, headless by default, strict ref resolution,
fail-closed errors with a delivery disposition, skeleton and drill-down) follows
[vercel-labs/agent-desktop](https://github.com/vercel-labs/agent-desktop). The differences
come from macpilot being a long-lived MCP server instead of a one-shot CLI: refs are short
and live in memory, and apps are claimed exclusively per agent.

## Several agents on one desktop

Each agent runs its own MCP server process. Setting `MACPILOT_AGENT` turns a server into one
agent among several:

| variable | effect |
| --- | --- |
| `MACPILOT_AGENT=notes` | names the agent. The tools that send physical input or bring an app to the front (`click`, `drag`, `type_text`, `press_key`, `open_app`, ...) are not registered at all |
| `MACPILOT_APPS=Notes,TextEdit` | the only apps this agent may claim |
| `MACPILOT_POINTER=on` | give a named agent the physical tools back |
| `MACPILOT_TRACE=path.jsonl` | append one JSON line per `ax_*` call (typed text is recorded as a length only) |
| `MACPILOT_HOME=dir` | where claims and runs live; default `~/Library/Application Support/macpilot` |

An app is claimed by the first agent that reads it. A claim is exclusive: another agent gets
`APP_CLAIMED` until the owner calls `ax_release` or its process exits. Claims are files under
`<home>/claims/`, so unrelated processes agree on them without a daemon; `macpilot claims`
lists them.

### Running a fleet

`macpilot fleet plan.toml` starts one `claude -p` per agent, all at once, each with a private
MCP config that puts the server in agent mode:

```toml
[defaults]
model = "sonnet"     # optional
timeout = 300        # seconds per agent

[[agent]]
name = "calc"
app = "Calculator"
task = "Compute 17 * 23 with the calculator buttons and report the display."

[[agent]]
name = "dict"
apps = ["Dictionary"]
task = "Look up 'serendipity' and report the first sentence of its definition."
```

```
$ macpilot fleet plan.toml
starting 2 agents; logs and traces in ~/Library/Application Support/macpilot/runs/20261109-185636
[dict] done (9s, 4 turns, $0.09)
...
[calc] done (16s, 13 turns, $0.11)
...
2 agents: 2 done
```

A plan that gives one app to two agents is rejected before anything starts. Each agent ends
its report with `OUTCOME: done | partial | blocked`, which is what the summary counts;
`FAILED` means the run itself broke (crash, timeout). The run directory holds each agent's
MCP config, raw output and action trace. `--dry-run` validates the plan and prints it.

## Physical tools

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
macpilot snapshot Finder --skeleton       # accessibility tree, read-only, claims nothing
macpilot find Safari "Reload" --role button
macpilot claims
macpilot fleet plan.toml --dry-run
```

Refs only live inside a running MCP server, so the CLI can look at a tree but not act on it.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```
