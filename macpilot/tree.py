"""Read an app's accessibility tree as text, with a ref on every element worth acting on.

A snapshot replaces "take a screenshot and guess coordinates": each line is one element
(role, label, value, states) and actionable elements and containers carry a ref such as
``e12`` that the action tools accept. Refs follow the element's AX identity, so the same
element keeps its ref across snapshots, and a ref is re-verified against the live element
before every use: if the element is gone or has become something else the ref is stale and
is refused rather than matched to a look-alike.

Dense apps are read in two passes: a shallow skeleton first, where cut containers report how
many children were left out, then a drill into one container by ref.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from macpilot.ax import AXLayer
from macpilot.errors import INVALID_ARGS, STALE_REF, DeskError


class TreeLayer(AXLayer, Protocol):
    """AXLayer plus what tree reading and status-aware actions need."""

    def app_element(self, pid: int) -> Any: ...

    def attributes(self, el: Any, names: Sequence[str]) -> dict[str, Any]: ...

    def perform_status(self, el: Any, action: str) -> int: ...


FETCH: tuple[str, ...] = (
    "AXRole",
    "AXSubrole",
    "AXTitle",
    "AXDescription",
    "AXValue",
    "AXPlaceholderValue",
    "AXIdentifier",
    "AXEnabled",
    "AXFocused",
    "AXSelected",
    "AXExpanded",
    "AXChildren",
    "AXVisibleRows",
)

INTERACTIVE_ROLES = frozenset({
    "AXButton", "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton", "AXMenuItem",
    "AXMenuBarItem", "AXLink", "AXTextField", "AXTextArea", "AXComboBox", "AXSlider",
    "AXIncrementor", "AXColorWell", "AXDisclosureTriangle", "AXDockItem", "AXRow",
})
TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
TOGGLE_ROLES = frozenset({"AXCheckBox", "AXRadioButton"})
# Kept in the output even when they have no label, because they give the tree its shape.
STRUCTURAL_ROLES = frozenset({
    "AXWindow", "AXSheet", "AXDrawer", "AXPopover", "AXToolbar", "AXTabGroup", "AXTable",
    "AXOutline", "AXList", "AXMenu", "AXMenuBar", "AXWebArea", "AXRow",
})
WINDOW_ROLES = frozenset({"AXWindow", "AXSheet", "AXDrawer", "AXPopover", "AXMenuBar"})
# Chrome that never matters to an agent and only costs lines.
SKIP_ROLES = frozenset({
    "AXScrollBar", "AXGrowArea", "AXSplitter", "AXRuler", "AXRulerMarker", "AXValueIndicator",
    "AXHandle", "AXMatte",
})
# A table can hold tens of thousands of rows; only the visible ones are read.
ROW_CONTAINERS = frozenset({"AXTable", "AXOutline"})
PRIMARY_ACTIONS = frozenset({
    "AXPress", "AXOpen", "AXPick", "AXConfirm", "AXIncrement", "AXDecrement",
})
SCROLL_ACTIONS = frozenset({
    "AXScrollUpByPage", "AXScrollDownByPage", "AXScrollLeftByPage", "AXScrollRightByPage",
})
# Menus report AXExpanded on every item; it says nothing an agent can use.
MENU_ROLES = frozenset({"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"})
# Invisible direction marks that apps sprinkle into labels and that break text matching.
_BIDI_MARKS = dict.fromkeys(
    [0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)]
)
# AppKit numbers elements that the developer did not name; the number identifies nothing.
GENERATED_ID_PREFIX = "_NS:"
ROLE_NAMES = {"AXStaticText": "text"}
SUBROLE_NAMES = {
    "AXSearchField": "searchfield",
    "AXSecureTextField": "securefield",
    "AXSwitch": "switch",
    "AXToggle": "switch",
}
SUBROLE_LABELS = {
    "AXCloseButton": "close",
    "AXMinimizeButton": "minimize",
    "AXZoomButton": "zoom",
    "AXFullScreenButton": "full screen",
}

DEFAULT_MAX_NODES = 300
DEFAULT_MAX_DEPTH = 12
SKELETON_DEPTH = 3
# Hard stops for pathological trees: wrappers that are never shown still cost IPC.
VISIT_FACTOR = 8
REAL_DEPTH_CAP = 80
LABEL_CHARS = 80
VALUE_CHARS = 120


@dataclass
class Node:
    el: Any
    depth: int
    ax_role: str
    role: str
    label: str
    value: str | None
    states: tuple[str, ...]
    actionable: bool
    container: bool
    hidden_children: int = 0
    more: int = 0
    ref: str = ""

    def line(self) -> str:
        parts = [f"{'  ' * self.depth}- {self.role}"]
        if self.label:
            parts.append(f'"{self.label}"')
        if self.value is not None:
            parts.append(f'= "{self.value}"')
        if self.ref:
            parts.append(f"[ref={self.ref}]")
        if self.states:
            parts.append(f"[{', '.join(self.states)}]")
        if self.hidden_children:
            parts.append(f"(+{self.hidden_children} children, drill with root={self.ref})")
        elif self.more:
            parts.append(f"(+{self.more} more not shown, drill with root={self.ref})")
        return " ".join(parts)


@dataclass(frozen=True)
class Reading:
    nodes: list[Node]
    visited: int
    complete: bool


@dataclass(frozen=True)
class RefEntry:
    el: Any
    pid: int
    role: str


def normalize_ref(ref: str) -> str:
    """Accept "e12", "ref=e12", "[ref=e12]" and "@e12"; reject anything else."""
    token = ref.strip().strip("[]").removeprefix("ref=").removeprefix("@").strip()
    if len(token) < 2 or token[0] != "e" or not token[1:].isdigit():
        raise DeskError(
            INVALID_ARGS,
            f"{ref!r} is not a ref; expected e<N> as printed by ax_snapshot",
        )
    return token


def _hashable(el: Any) -> bool:
    """Elements that cannot be dict keys are not indexed and get a fresh ref each time."""
    try:
        hash(el)
    except TypeError:
        return False
    return True


class RefTable:
    """Refs for live elements. One table per server process, so one per agent."""

    def __init__(self, limit: int = 20000) -> None:
        self._limit = limit
        self._entries: dict[str, RefEntry] = {}
        self._by_element: dict[Any, str] = {}
        self._next = 1

    def assign(self, el: Any, pid: int, role: str) -> str:
        known = self._known(el)
        if known is not None:
            self._entries[known] = RefEntry(el, pid, role)
            return known
        ref = f"e{self._next}"
        self._next += 1
        self._entries[ref] = RefEntry(el, pid, role)
        if _hashable(el):
            self._by_element[el] = ref
        while len(self._entries) > self._limit:
            oldest = next(iter(self._entries))
            self._forget(oldest)
        return ref

    def lookup(self, ref: str) -> RefEntry:
        token = normalize_ref(ref)
        entry = self._entries.get(token)
        if entry is None:
            raise DeskError(
                STALE_REF,
                f"no element is recorded for {token}",
                hint="take a new ax_snapshot and use a ref from it",
            )
        return entry

    def forget_pid(self, pid: int) -> None:
        for ref in [r for r, e in self._entries.items() if e.pid == pid]:
            self._forget(ref)

    def _known(self, el: Any) -> str | None:
        return self._by_element.get(el) if _hashable(el) else None

    def _forget(self, ref: str) -> None:
        entry = self._entries.pop(ref)
        if _hashable(entry.el):
            self._by_element.pop(entry.el, None)


def resolve(layer: TreeLayer, refs: RefTable, ref: str) -> RefEntry:
    """The live element for `ref`, or STALE_REF. Never substitutes a similar element."""
    entry = refs.lookup(ref)
    token = normalize_ref(ref)
    role = layer.attribute(entry.el, "AXRole")
    if not isinstance(role, str):
        raise DeskError(
            STALE_REF,
            f"the element behind {token} no longer exists",
            hint="take a new ax_snapshot and use a ref from it",
        )
    if role != entry.role:
        raise DeskError(
            STALE_REF,
            f"{token} was a {display_role(entry.role, '')} and is now a {display_role(role, '')}",
            hint="take a new ax_snapshot and use a ref from it",
        )
    return entry


def display_role(ax_role: str, subrole: str) -> str:
    if subrole in SUBROLE_NAMES:
        return SUBROLE_NAMES[subrole]
    if ax_role in ROLE_NAMES:
        return ROLE_NAMES[ax_role]
    return ax_role.removeprefix("AX").lower() or "element"


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _text(value: Any) -> str:
    return value.translate(_BIDI_MARKS).strip() if isinstance(value, str) else ""


def _children(role: str, attrs: dict[str, Any]) -> list[Any]:
    source = attrs.get("AXVisibleRows") if role in ROW_CONTAINERS else None
    if source is None:
        source = attrs.get("AXChildren")
    if source is None or isinstance(source, str):
        return []
    try:
        return list(source)
    except TypeError:
        return []


def _describe(
    layer: TreeLayer, el: Any, depth: int, role: str, attrs: dict[str, Any], container: bool
) -> Node:
    subrole = _text(attrs.get("AXSubrole"))
    raw_value = attrs.get("AXValue")
    label = (
        _text(attrs.get("AXTitle"))
        or _text(attrs.get("AXDescription"))
        or _text(attrs.get("AXPlaceholderValue"))
    )
    value: str | None = None
    states: list[str] = []
    if role == "AXStaticText":
        label = label or _text(raw_value)
    elif role in TOGGLE_ROLES:
        if raw_value == 1:
            states.append("checked")
        elif raw_value == 2:
            states.append("mixed")
    elif subrole == "AXSecureTextField":
        value = None
    elif isinstance(raw_value, str):
        if _text(raw_value) and _text(raw_value) != label:
            value = _clip(_text(raw_value), VALUE_CHARS)
    elif isinstance(raw_value, int | float) and not isinstance(raw_value, bool):
        value = f"{raw_value:g}"
    if not label:
        label = SUBROLE_LABELS.get(subrole, "")
    identifier = _text(attrs.get("AXIdentifier"))
    if not label and identifier and not identifier.startswith(GENERATED_ID_PREFIX):
        label = "#" + identifier

    actions = set() if role == "AXStaticText" else set(layer.actions(el))
    scrollable = bool(actions & SCROLL_ACTIONS)
    actionable = role in INTERACTIVE_ROLES or bool(actions & PRIMARY_ACTIONS) or scrollable

    if attrs.get("AXEnabled") is False:
        states.append("disabled")
    if attrs.get("AXFocused") is True:
        states.append("focused")
    if attrs.get("AXSelected") is True:
        states.append("selected")
    expanded = attrs.get("AXExpanded")
    if isinstance(expanded, bool) and role not in MENU_ROLES:
        states.append("expanded" if expanded else "collapsed")
    if role in TEXT_ROLES and layer.is_settable(el, "AXValue"):
        states.append("editable")
    if scrollable:
        states.append("scrollable")
    if role not in INTERACTIVE_ROLES and actions & {"AXPress", "AXOpen", "AXPick"}:
        states.append("clickable")

    return Node(
        el=el,
        depth=depth,
        ax_role=role,
        role=display_role(role, subrole),
        label=_clip(label, LABEL_CHARS),
        value=value,
        states=tuple(states),
        actionable=actionable,
        container=container,
    )


def shown_in_snapshot(interactive_only: bool) -> Callable[[Node], bool]:
    def shown(node: Node) -> bool:
        if node.ax_role == "AXMenuItem" and not node.label:
            return False  # a separator
        if node.ax_role in WINDOW_ROLES or node.actionable:
            return True
        if interactive_only:
            return node.container and bool(node.label)
        return bool(node.label) or node.value is not None or node.ax_role in STRUCTURAL_ROLES

    return shown


class _Walk:
    def __init__(
        self,
        layer: TreeLayer,
        shown: Callable[[Node], bool],
        max_nodes: int,
        max_depth: int,
        max_visit: int,
        flat: bool,
    ) -> None:
        self.layer = layer
        self.shown = shown
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.max_visit = max_visit
        self.flat = flat
        self.nodes: list[Node] = []
        self.visited = 0
        self.complete = True

    def visit(self, el: Any, depth: int, real_depth: int, owner: Node | None) -> bool:
        """Read `el` and its subtree. False means the budget ran out; stop reading siblings."""
        if self.visited >= self.max_visit:
            self.complete = False
            return False
        self.visited += 1
        attrs = self.layer.attributes(el, FETCH)
        role = attrs.get("AXRole")
        if not isinstance(role, str) or role in SKIP_ROLES:
            return True
        children = _children(role, attrs)
        node = _describe(self.layer, el, 0 if self.flat else depth, role, attrs, bool(children))
        if role == "AXWindow":
            # A sheet blocks the window behind it, so it is what the agent must see first,
            # before the node budget is spent on controls it cannot use.
            sheets = [c for c in children if self.layer.attribute(c, "AXRole") == "AXSheet"]
            if sheets:
                children = sheets + [c for c in children if c not in sheets]
                node.states = (*node.states, "sheet open")
        child_depth = depth
        if self.shown(node):
            if len(self.nodes) >= self.max_nodes:
                self.complete = False
                return False
            self.nodes.append(node)
            if not self.flat:
                owner = node
                child_depth = depth + 1
                if children and depth >= self.max_depth - 1:
                    node.hidden_children = len(children)
                    return True
        if real_depth >= REAL_DEPTH_CAP:
            self.complete = False
            return True
        for index, child in enumerate(children):
            if not self.visit(child, child_depth, real_depth + 1, owner):
                if owner is not None and not self.flat:
                    owner.more += len(children) - index
                return False
        return True


def read_tree(
    layer: TreeLayer,
    roots: Iterable[Any],
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    interactive_only: bool = False,
) -> Reading:
    """Depth-first reading of `roots`, compacted: unlabeled inert wrappers are skipped and
    their children shown in their place."""
    walk = _Walk(
        layer,
        shown_in_snapshot(interactive_only),
        max_nodes=max(1, max_nodes),
        max_depth=max(1, max_depth),
        max_visit=max(1, max_nodes) * VISIT_FACTOR,
        flat=False,
    )
    for root in roots:
        if not walk.visit(root, 0, 0, None):
            break
    return Reading(walk.nodes, walk.visited, walk.complete)


def search(
    layer: TreeLayer,
    roots: Iterable[Any],
    query: str,
    *,
    role: str | None = None,
    limit: int = 30,
    max_visit: int = 4000,
) -> Reading:
    """Every element whose label or value contains `query` (case-insensitive), flat."""
    needle = query.strip().lower()
    wanted_role = role.strip().lower().removeprefix("ax") if role else None

    def matches(node: Node) -> bool:
        if wanted_role and wanted_role not in (node.role, node.ax_role.removeprefix("AX").lower()):
            return False
        haystack = f"{node.label} {node.value or ''}".lower()
        return needle in haystack

    walk = _Walk(layer, matches, max_nodes=max(1, limit), max_depth=1, max_visit=max_visit,
                 flat=True)
    for root in roots:
        if not walk.visit(root, 0, 0, None):
            break
    return Reading(walk.nodes, walk.visited, walk.complete)


def assign_refs(nodes: Iterable[Node], refs: RefTable, pid: int) -> None:
    """Give a ref to everything an action or a drill can target; plain text gets none."""
    for node in nodes:
        if node.actionable or node.container:
            node.ref = refs.assign(node.el, pid, node.ax_role)


def render(nodes: Sequence[Node]) -> str:
    return "\n".join(node.line() for node in nodes)


def text_of(layer: TreeLayer, el: Any, *, limit: int = 8000, max_visit: int = 4000) -> str:
    """All readable text under `el` in document order, for reading a page or a document."""
    own = layer.attribute(el, "AXValue")
    if isinstance(own, str) and layer.attribute(el, "AXRole") in TEXT_ROLES:
        return own if len(own) <= limit else own[:limit] + "\n…[truncated]"
    pieces: list[str] = []
    size = 0
    visited = 0
    stack: list[Any] = [el]
    while stack:
        current = stack.pop()
        visited += 1
        if visited > max_visit:
            pieces.append("…[stopped: subtree too large, read a smaller region]")
            break
        attrs = layer.attributes(current, ("AXRole", "AXTitle", "AXDescription", "AXValue",
                                           "AXChildren", "AXVisibleRows"))
        role = attrs.get("AXRole")
        if not isinstance(role, str) or role in SKIP_ROLES:
            continue
        value = attrs.get("AXValue")
        piece = _text(value) if role == "AXStaticText" or role in TEXT_ROLES else ""
        piece = piece or _text(attrs.get("AXTitle")) or _text(attrs.get("AXDescription"))
        if piece and (not pieces or pieces[-1] != piece):
            pieces.append(piece)
            size += len(piece) + 1
            if size > limit:
                pieces.append("…[truncated]")
                break
        stack.extend(reversed(_children(role, attrs)))
    return "\n".join(pieces)
