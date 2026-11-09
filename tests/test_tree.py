from __future__ import annotations

import pytest
from fakes import El, FakeTree, button, field, text

from macpilot import tree
from macpilot.errors import DeskError


def _window(*children: El, title: str = "Main") -> El:
    return El("AXWindow", title, children=children)


def _snapshot(layer: FakeTree, root: El, refs: tree.RefTable | None = None, **kw: object) -> str:
    reading = tree.read_tree(layer, [root], **kw)  # type: ignore[arg-type]
    tree.assign_refs(reading.nodes, refs if refs is not None else tree.RefTable(), 1)
    return tree.render(reading.nodes)


# --- rendering ---------------------------------------------------------------------------


def test_snapshot_lines_show_role_label_value_and_states() -> None:
    layer = FakeTree()
    win = _window(
        button("Save"),
        button("Delete", attrs={"AXEnabled": False}),
        field("Name", "Ada", attrs={"AXFocused": True}),
        text("3 items"),
    )
    assert _snapshot(layer, win).splitlines() == [
        '- window "Main" [ref=e1]',
        '  - button "Save" [ref=e2]',
        '  - button "Delete" [ref=e3] [disabled]',
        '  - textfield "Name" = "Ada" [ref=e4] [focused, editable]',
        '  - text "3 items"',
    ]


def test_plain_text_gets_no_ref_but_containers_do() -> None:
    layer = FakeTree()
    win = _window(El("AXGroup", "Sidebar", children=[text("Inbox")]))
    lines = _snapshot(layer, win).splitlines()
    assert lines[1] == '  - group "Sidebar" [ref=e2]'
    assert lines[2] == '    - text "Inbox"'


def test_unlabeled_inert_wrappers_are_skipped_and_children_hoisted() -> None:
    layer = FakeTree()
    win = _window(El("AXGroup", children=[El("AXSplitGroup", children=[button("OK")])]))
    assert _snapshot(layer, win).splitlines() == [
        '- window "Main" [ref=e1]',
        '  - button "OK" [ref=e2]',
    ]


def test_scroll_bars_and_other_chrome_are_dropped_with_their_subtrees() -> None:
    layer = FakeTree()
    bar = El("AXScrollBar", children=[button("page down")])
    assert "page down" not in _snapshot(layer, _window(bar, button("OK")))


def test_toggle_state_subrole_names_and_unlabeled_fallbacks() -> None:
    layer = FakeTree()
    win = _window(
        El("AXCheckBox", "Wi-Fi", value=1, actions=("AXPress",)),
        El("AXCheckBox", "VPN", value=0, actions=("AXPress",), attrs={"AXSubrole": "AXSwitch"}),
        El("AXButton", actions=("AXPress",), attrs={"AXSubrole": "AXCloseButton"}),
        El("AXButton", actions=("AXPress",), attrs={"AXIdentifier": "calc_7"}),
        El("AXSlider", "Volume", value=0.5, actions=("AXIncrement",)),
        El("AXOutline", children=[El("AXRow", attrs={"AXExpanded": False, "AXSelected": True})]),
    )
    lines = _snapshot(layer, win).splitlines()
    assert lines[1] == '  - checkbox "Wi-Fi" [ref=e2] [checked]'
    assert lines[2] == '  - switch "VPN" [ref=e3]'
    assert lines[3] == '  - button "close" [ref=e4]'
    assert lines[4] == '  - button "#calc_7" [ref=e5]'
    assert lines[5] == '  - slider "Volume" = "0.5" [ref=e6]'
    assert lines[7] == "    - row [ref=e8] [selected, collapsed]"


def test_invisible_direction_marks_are_stripped_so_text_can_be_matched() -> None:
    layer = FakeTree()
    win = _window(text("\u200e12\u200e×\u200e34"), field("Total", "\u200e408"))
    assert _snapshot(layer, win).splitlines()[1:] == [
        '  - text "12×34"',
        '  - textfield "Total" = "408" [ref=e2] [editable]',
    ]
    assert [n.label for n in tree.search(layer, [win], "12×34").nodes] == ["12×34"]
    assert tree.text_of(layer, win) == "Main\n12×34\n408"


def test_generated_identifiers_and_menu_separators_are_not_shown() -> None:
    layer = FakeTree()
    menu = El("AXMenu", attrs={"AXIdentifier": "_NS:864"}, children=[
        El("AXMenuItem", "New", actions=("AXPress",)),
        El("AXMenuItem", attrs={"AXIdentifier": "_NS:877", "AXEnabled": False}),
        El("AXMenuItem", "Close", actions=("AXPress",)),
    ])
    assert _snapshot(layer, menu).splitlines() == [
        "- menu [ref=e1]",
        '  - menuitem "New" [ref=e2]',
        '  - menuitem "Close" [ref=e3]',
    ]


def test_menu_items_do_not_report_expansion_state() -> None:
    layer = FakeTree()
    bar = El("AXMenuBar", attrs={"AXExpanded": False}, children=[
        El("AXMenuBarItem", "File", actions=("AXPress",), attrs={"AXExpanded": False}),
    ])
    assert _snapshot(layer, bar).splitlines() == [
        "- menubar [ref=e1]",
        '  - menubaritem "File" [ref=e2]',
    ]


def test_open_sheet_is_listed_first_and_flagged_on_its_window() -> None:
    layer = FakeTree()
    sheet = El("AXSheet", "save", children=[button("Delete"), button("Cancel")])
    win = _window(*[button(f"toolbar {i}") for i in range(6)], sheet)
    reading = tree.read_tree(layer, [win], max_nodes=4)
    tree.assign_refs(reading.nodes, tree.RefTable(), 1)
    assert [n.line().split(" (+")[0] for n in reading.nodes] == [
        '- window "Main" [ref=e1] [sheet open]',
        '  - sheet "save" [ref=e2]',
        '    - button "Delete" [ref=e3]',
        '    - button "Cancel" [ref=e4]',
    ]


def test_secure_field_value_is_never_printed() -> None:
    layer = FakeTree()
    secret = field("Password", "hunter2", attrs={"AXSubrole": "AXSecureTextField"})
    out = _snapshot(layer, _window(secret))
    assert "hunter2" not in out and 'securefield "Password"' in out


def test_inert_element_with_a_press_action_is_marked_clickable() -> None:
    layer = FakeTree()
    out = _snapshot(layer, _window(El("AXImage", "Avatar", actions=("AXPress",))))
    assert '- image "Avatar" [ref=e2] [clickable]' in out


def test_long_text_is_clipped_and_flattened() -> None:
    layer = FakeTree()
    out = _snapshot(layer, _window(text("line one\nline two " + "x" * 200)))
    line = out.splitlines()[1]
    assert "\n" not in line and line.endswith('…"') and "line one line two" in line


def test_table_reads_only_visible_rows() -> None:
    layer = FakeTree()
    rows = [El("AXRow", children=[text(f"row {i}")]) for i in range(5)]
    table = El("AXTable", children=rows, attrs={"AXVisibleRows": rows[:2]})
    out = _snapshot(layer, _window(table))
    assert "row 0" in out and "row 1" in out and "row 2" not in out


def test_interactive_only_drops_static_text_but_keeps_labelled_containers() -> None:
    layer = FakeTree()
    win = _window(El("AXGroup", "Toolbar", children=[text("Ready"), button("Run")]), text("v1"))
    assert _snapshot(layer, win, interactive_only=True).splitlines() == [
        '- window "Main" [ref=e1]',
        '  - group "Toolbar" [ref=e2]',
        '    - button "Run" [ref=e3]',
    ]


# --- skeleton and budget -----------------------------------------------------------------


def test_depth_limit_marks_cut_containers_with_their_child_count() -> None:
    layer = FakeTree()
    pane = El("AXGroup", "Editor", children=[button("A"), button("B"), button("C")])
    lines = _snapshot(layer, _window(pane), max_depth=2).splitlines()
    assert lines == [
        '- window "Main" [ref=e1]',
        '  - group "Editor" [ref=e2] (+3 children, drill with root=e2)',
    ]


def test_drilling_a_cut_container_reads_its_children_and_continues_the_ref_counter() -> None:
    layer = FakeTree()
    pane = El("AXGroup", "Editor", children=[button("A"), button("B")])
    refs = tree.RefTable()
    _snapshot(layer, _window(pane), refs, max_depth=2)
    assert _snapshot(layer, pane, refs).splitlines() == [
        '- group "Editor" [ref=e2]',
        '  - button "A" [ref=e3]',
        '  - button "B" [ref=e4]',
    ]


def test_node_budget_stops_the_read_and_says_how_much_was_left() -> None:
    layer = FakeTree()
    win = _window(*[button(f"b{i}") for i in range(10)])
    reading = tree.read_tree(layer, [win], max_nodes=4)
    tree.assign_refs(reading.nodes, tree.RefTable(), 1)
    assert not reading.complete and len(reading.nodes) == 4
    assert reading.nodes[0].line() == (
        '- window "Main" [ref=e1] (+7 more not shown, drill with root=e1)'
    )


def test_small_tree_is_complete() -> None:
    layer = FakeTree()
    assert tree.read_tree(layer, [_window(button("OK"))]).complete


def test_visit_cap_bounds_ipc_on_trees_of_invisible_wrappers() -> None:
    layer = FakeTree()
    win = _window(*[El("AXGroup") for _ in range(200)])
    reading = tree.read_tree(layer, [win], max_nodes=5)
    assert not reading.complete and layer.reads <= 5 * tree.VISIT_FACTOR


# --- refs --------------------------------------------------------------------------------


def test_same_element_keeps_its_ref_across_snapshots() -> None:
    layer = FakeTree()
    save = button("Save")
    win = _window(save)
    refs = tree.RefTable()
    _snapshot(layer, win, refs)
    win.children.insert(0, button("New"))
    win.children[0].parent = win
    lines = _snapshot(layer, win, refs).splitlines()
    assert lines[1] == '  - button "New" [ref=e3]'
    assert lines[2] == '  - button "Save" [ref=e2]'
    assert tree.resolve(layer, refs, "e2").el is save


@pytest.mark.parametrize("raw", ["e2", " e2 ", "ref=e2", "[ref=e2]", "@e2"])
def test_ref_spellings_are_accepted(raw: str) -> None:
    assert tree.normalize_ref(raw) == "e2"


@pytest.mark.parametrize("raw", ["", "2", "e", "button", "e2x", "@s8f3:e2"])
def test_malformed_refs_are_invalid_args(raw: str) -> None:
    with pytest.raises(DeskError) as err:
        tree.normalize_ref(raw)
    assert err.value.code == "INVALID_ARGS"


def test_unknown_ref_is_stale() -> None:
    with pytest.raises(DeskError) as err:
        tree.resolve(FakeTree(), tree.RefTable(), "e99")
    assert err.value.code == "STALE_REF" and err.value.delivery == "not_delivered"


def test_ref_to_a_removed_element_is_stale() -> None:
    layer = FakeTree()
    save = button("Save")
    win = _window(save)
    refs = tree.RefTable()
    _snapshot(layer, win, refs)
    win.remove(save)
    with pytest.raises(DeskError, match="no longer exists") as err:
        tree.resolve(layer, refs, "e2")
    assert err.value.code == "STALE_REF"


def test_ref_whose_element_changed_role_is_refused_not_reused() -> None:
    layer = FakeTree()
    save = button("Save")
    refs = tree.RefTable()
    _snapshot(layer, _window(save), refs)
    save.attrs["AXRole"] = "AXStaticText"
    with pytest.raises(DeskError, match="was a button and is now a text") as err:
        tree.resolve(layer, refs, "e2")
    assert err.value.code == "STALE_REF"


def test_ref_table_evicts_oldest_entries_past_its_limit() -> None:
    refs = tree.RefTable(limit=2)
    first, second, third = button("1"), button("2"), button("3")
    assert [refs.assign(el, 1, "AXButton") for el in (first, second, third)] == ["e1", "e2", "e3"]
    with pytest.raises(DeskError):
        refs.lookup("e1")
    assert refs.lookup("e3").el is third
    assert refs.assign(first, 1, "AXButton") == "e4"


def test_forget_pid_drops_only_that_apps_refs() -> None:
    refs = tree.RefTable()
    a, b = button("a"), button("b")
    refs.assign(a, 1, "AXButton")
    refs.assign(b, 2, "AXButton")
    refs.forget_pid(1)
    with pytest.raises(DeskError):
        refs.lookup("e1")
    assert refs.lookup("e2").pid == 2


def test_unhashable_elements_still_get_refs() -> None:
    refs = tree.RefTable()
    el: list[int] = []
    assert refs.assign(el, 1, "AXButton") == "e1"
    assert refs.assign(el, 1, "AXButton") == "e2"
    assert refs.lookup("e1").el is el


# --- search and text ---------------------------------------------------------------------


def test_search_matches_label_or_value_case_insensitively() -> None:
    layer = FakeTree()
    win = _window(
        El("AXGroup", children=[button("Send Message")]),
        field("To", "message@example.com"),
        text("No new messages"),
        button("Cancel"),
    )
    reading = tree.search(layer, [win], "MESSAGE")
    tree.assign_refs(reading.nodes, tree.RefTable(), 1)
    assert tree.render(reading.nodes).splitlines() == [
        '- button "Send Message" [ref=e1]',
        '- textfield "To" = "message@example.com" [ref=e2] [editable]',
        '- text "No new messages"',
    ]


def test_search_role_filter_and_limit() -> None:
    layer = FakeTree()
    win = _window(text("Save draft"), button("Save"), button("Save as"), button("Save all"))
    only_buttons = tree.search(layer, [win], "save", role="button")
    assert [n.label for n in only_buttons.nodes] == ["Save", "Save as", "Save all"]
    capped = tree.search(layer, [win], "save", role="AXButton", limit=2)
    assert len(capped.nodes) == 2 and not capped.complete


def test_text_of_reads_a_region_in_document_order_without_repeats() -> None:
    layer = FakeTree()
    page = El("AXWebArea", "Docs", children=[
        El("AXHeading", "Install"),
        El("AXGroup", children=[text("Run the installer."), text("Run the installer.")]),
        El("AXLink", "Next page", children=[text("Next page")]),
    ])
    assert tree.text_of(layer, page) == "Docs\nInstall\nRun the installer.\nNext page"


def test_text_of_returns_the_full_value_of_a_text_field() -> None:
    layer = FakeTree()
    body = El("AXTextArea", value="x" * 500)
    assert tree.text_of(layer, body) == "x" * 500
    assert tree.text_of(layer, body, limit=10) == "x" * 10 + "\n…[truncated]"


def test_text_of_truncates_long_regions() -> None:
    layer = FakeTree()
    page = El("AXWebArea", children=[text(f"paragraph {i} " + "y" * 50) for i in range(50)])
    out = tree.text_of(layer, page, limit=300)
    assert out.endswith("…[truncated]") and len(out) < 500
