from __future__ import annotations

from pathlib import Path

import pytest
from fakes import El, FakeApps, FakeTree, button, field, text

from macpilot import ax
from macpilot.desk import Desk, file_trace
from macpilot.errors import DeskError
from macpilot.keys import KeyChord
from macpilot.session import Claims

NOTES, CALC = 500, 600


class World:
    """Two running apps and helpers to build one Desk per agent over the same claims."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "claims"
        self.layer = FakeTree()
        self.apps = FakeApps({"Notes": NOTES, "Calculator": CALC})
        self.body = El("AXTextArea", "Body", value="", settable=("AXSelectedText", "AXFocused"))
        self.new = button("New Note")
        self.notes_window = El("AXWindow", "Notes", children=[self.new, self.body])
        self.layer.add_app(NOTES, self.notes_window)
        self.seven = button("7")
        self.layer.add_app(CALC, El("AXWindow", "Calculator", children=[self.seven, text("0")]))
        self.keys: list[tuple[int, str]] = []
        self.texts: list[tuple[int, str]] = []
        self.sleeps: list[float] = []
        self.now = 0.0
        self.trace: list[dict[str, object]] = []
        self._owner = 100

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def _post_key(self, pid: int, chord: KeyChord) -> None:
        self.keys.append((pid, chord.name))

    def desk(self, agent: str, allowed: frozenset[str] | None = None) -> Desk:
        self._owner += 1
        claims = Claims(self.root, agent, owner_pid=self._owner, alive=lambda pid: True)
        return Desk(
            self.layer, self.apps, claims, agent=agent, allowed=allowed,
            post_key=self._post_key,
            post_text=lambda pid, text: self.texts.append((pid, text)),
            trace=self.trace.append, sleep=self._sleep, clock=lambda: self.now,
            wall=lambda: 1000.0 + self.now,
        )

    def observer(self) -> Desk:
        return Desk(self.layer, self.apps, None)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _error(fn: object, *args: object, **kw: object) -> DeskError:
    with pytest.raises(DeskError) as err:
        fn(*args, **kw)  # type: ignore[operator]
    return err.value


# --- snapshot ----------------------------------------------------------------------------


def test_snapshot_reads_a_background_app_and_claims_it(world: World) -> None:
    desk = world.desk("alice")
    out = desk.snapshot("notes").splitlines()
    assert out[0] == 'app "Notes" pid 500 | windows | background | agent alice'
    assert out[1:4] == [
        '- window "Notes" [ref=e1]',
        '  - button "New Note" [ref=e2]',
        '  - textarea "Body" [ref=e3]',
    ]
    assert out[4] == "3 elements, 3 refs"
    assert desk.claims is not None and desk.claims.holds(NOTES)


def test_snapshot_warns_when_the_human_is_in_that_app(world: World) -> None:
    world.apps.frontmost = NOTES
    assert "FRONTMOST (the human may be using it)" in world.desk("alice").snapshot("Notes")


def test_snapshot_then_act_on_a_ref(world: World) -> None:
    desk = world.desk("alice")
    desk.snapshot("Notes")
    assert desk.press(ref="e2") == "AXPress on AXButton 'New Note'; delivered_unverified"
    assert world.layer.performed == [(world.new, "AXPress")]
    assert desk.insert_text(ref="e3", text="milk").endswith("delivered_verified")
    assert world.body.attrs["AXValue"] == "milk"


def test_skeleton_cuts_at_three_levels_and_root_drills_in(world: World) -> None:
    deep = El("AXGroup", "Editor", children=[El("AXGroup", "Pane", children=[button("Bold")])])
    world.notes_window.add(deep)
    desk = world.desk("alice")
    skeleton = desk.snapshot("Notes", skeleton=True)
    assert '    - group "Pane" [ref=e5] (+1 children, drill with root=e5)' in skeleton
    assert "Bold" not in skeleton
    drilled = desk.snapshot(root="e5").splitlines()
    assert drilled[0] == 'app "Notes" pid 500 | subtree of e5 | background | agent alice'
    assert drilled[1:3] == ['- group "Pane" [ref=e5]', '  - button "Bold" [ref=e6]']


def test_window_of_a_background_launched_app_is_found_through_the_main_window(
    world: World,
) -> None:
    window = El("AXWindow", "Untitled", children=[button("Bold")])
    app = world.layer.add_app(NOTES)
    app.attrs["AXMainWindow"] = window
    app.attrs["AXFocusedWindow"] = window
    out = world.desk("alice").snapshot("Notes").splitlines()
    assert out[1:] == ['- window "Untitled" [ref=e1]', '  - button "Bold" [ref=e2]',
                       "2 elements, 2 refs"]


def test_main_window_and_focused_sheet_are_not_listed_twice(world: World) -> None:
    sheet = El("AXSheet", "save", children=[button("Delete")])
    world.notes_window.add(sheet)
    app = world.layer.apps[NOTES]
    app.attrs["AXMainWindow"] = world.notes_window
    app.attrs["AXFocusedWindow"] = sheet
    desk = world.desk("alice")
    out = desk.snapshot("Notes")
    assert out.count('- window "Notes"') == 1 and out.count('sheet "save"') == 1
    assert desk.find("Notes", query="Delete").startswith("1 matches")


def test_incomplete_snapshot_says_so(world: World) -> None:
    for i in range(20):
        world.notes_window.add(button(f"b{i}"))
    out = world.desk("alice").snapshot("Notes", max_nodes=5)
    assert "INCOMPLETE" in out.splitlines()[-1]


def test_menubar_surface_and_missing_windows(world: World) -> None:
    bar = El("AXMenuBar", children=[El("AXMenuBarItem", "File", actions=("AXPress",))])
    world.layer.add_app(NOTES, menubar=bar)
    desk = world.desk("alice")
    assert '  - menubaritem "File" [ref=e2]' in desk.snapshot("Notes", surface="menubar")
    assert "no windows" in desk.snapshot("Notes")
    assert _error(desk.snapshot, "Notes", surface="dock").code == "INVALID_ARGS"
    assert _error(desk.snapshot).code == "INVALID_ARGS"


def test_sparse_tree_of_an_unrecognised_app_tries_the_electron_switch_once(world: World) -> None:
    slack = 700
    world.apps.running["Slack"] = slack
    window = El("AXWindow", "Slack")
    world.layer.add_app(slack, window)
    world.layer.full_tree[slack] = ax.MANUAL_ACCESSIBILITY
    original = world.layer.enable_full_tree

    def enable(pid: int, chromium: bool) -> str | None:
        window.add(button("Send"))
        return original(pid, chromium)

    world.layer.enable_full_tree = enable  # type: ignore[method-assign]
    desk = world.desk("alice")
    assert 'button "Send"' in desk.snapshot("Slack")
    assert world.sleeps == [0.6] and world.layer.full_tree_calls == [(slack, False)]
    window.children.clear()
    desk.snapshot("Slack")
    assert len(world.layer.full_tree_calls) == 1


def test_chromium_browsers_get_the_enhanced_flag_and_a_longer_settle(world: World) -> None:
    chrome = 800
    world.apps.running["Google Chrome"] = chrome
    world.apps.renderers[chrome] = "chromium"
    world.layer.add_app(chrome, El("AXWindow", "New Tab"))
    world.layer.full_tree[chrome] = ax.ENHANCED_USER_INTERFACE
    world.desk("alice").snapshot("Google Chrome")
    assert world.layer.full_tree_calls == [(chrome, True)] and world.sleeps == [2.0]


def test_recognised_electron_app_is_woken_before_the_first_read(world: World) -> None:
    code = 810
    world.apps.running["Code"] = code
    world.apps.renderers[code] = "electron"
    chrome_only = [button(f"titlebar {i}") for i in range(8)]
    window = El("AXWindow", "Code", children=chrome_only)
    world.layer.add_app(code, window)
    world.layer.full_tree[code] = ax.MANUAL_ACCESSIBILITY
    desk = world.desk("alice")
    desk.snapshot("Code")
    desk.snapshot("Code")
    assert world.layer.full_tree_calls == [(code, False)] and world.sleeps == [0.6]


def test_renderer_already_awake_costs_no_wait(world: World) -> None:
    code = 810
    world.apps.running["Code"] = code
    world.apps.renderers[code] = "electron"
    world.layer.add_app(code, El("AXWindow", "Code", children=[button("Run")]))
    world.desk("alice").snapshot("Code")
    assert world.layer.full_tree_calls == [(code, False)] and world.sleeps == []


def test_native_apps_with_a_full_tree_are_left_alone(world: World) -> None:
    for i in range(6):
        world.notes_window.add(button(f"b{i}"))
    world.desk("alice").snapshot("Notes")
    assert world.layer.full_tree_calls == []


# --- isolation between agents ------------------------------------------------------------


def test_second_agent_cannot_read_or_drive_a_claimed_app(world: World) -> None:
    alice, bob = world.desk("alice"), world.desk("bob")
    alice.snapshot("Notes")
    err = _error(bob.snapshot, "Notes")
    assert err.code == "APP_CLAIMED" and "alice" in err.message
    assert _error(bob.key, "Notes", keys="return").code == "APP_CLAIMED"
    assert world.keys == []


def test_agents_work_on_different_apps_side_by_side(world: World) -> None:
    alice, bob = world.desk("alice"), world.desk("bob")
    alice.snapshot("Notes")
    bob.snapshot("Calculator")
    alice.press(ref="e2")
    bob.press(ref="e2")
    assert world.layer.performed == [(world.new, "AXPress"), (world.seven, "AXPress")]
    report = alice.claims_report().splitlines()
    assert report == ["Notes (pid 500): agent alice (you), for 0s",
                      "Calculator (pid 600): agent bob, for 0s"]


def test_release_frees_the_app_and_invalidates_the_old_refs(world: World) -> None:
    alice, bob = world.desk("alice"), world.desk("bob")
    alice.snapshot("Notes")
    assert alice.release("Notes") == "released Notes"
    assert _error(alice.press, ref="e2").code == "STALE_REF"
    assert "agent bob" in bob.snapshot("Notes")
    assert alice.release("Notes") == "agent alice did not hold Notes"


def test_ref_is_refused_once_another_agent_holds_the_app(world: World) -> None:
    alice, bob = world.desk("alice"), world.desk("bob")
    alice.snapshot("Notes")
    assert alice.claims is not None
    alice.claims.release(NOTES)
    bob.snapshot("Notes")
    err = _error(alice.press, ref="e2")
    assert err.code == "APP_CLAIMED" and world.layer.performed == []


def test_closing_a_desk_releases_everything_it_held(world: World) -> None:
    alice, bob = world.desk("alice"), world.desk("bob")
    alice.snapshot("Notes")
    alice.close()
    assert "agent bob" in bob.snapshot("Notes")


def test_agent_is_confined_to_its_allowed_apps(world: World) -> None:
    alice = world.desk("alice", allowed=frozenset({"notes"}))
    assert "Notes" in alice.snapshot("Notes")
    err = _error(alice.snapshot, "Calculator")
    assert err.code == "POLICY_DENIED" and "may only operate: notes" in err.message
    assert _error(alice.launch, "Safari").code == "POLICY_DENIED"
    assert _error(alice.snapshot, "Safari").code == "POLICY_DENIED"
    assert world.apps.launched == []
    assert [c.app for c in Claims(world.root, "probe", alive=lambda pid: True).all()] == ["Notes"]


def test_untrusted_process_gets_a_permission_error(world: World, tmp_path: Path) -> None:
    desk = Desk(FakeTree(trusted=False), world.apps, Claims(tmp_path / "c", "alice"))
    assert _error(desk.snapshot, "Notes").code == "PERM_DENIED"


# --- apps --------------------------------------------------------------------------------


def test_app_that_is_not_running_must_be_launched(world: World) -> None:
    desk = world.desk("alice")
    err = _error(desk.snapshot, "TextEdit")
    assert err.code == "APP_NOT_FOUND" and "ax_launch" in err.hint
    world.apps.installed["TextEdit"] = 900
    world.layer.add_app(900, El("AXWindow", "Untitled"))
    assert desk.launch("TextEdit") == (
        '"TextEdit" is running in the background as pid 900, claimed by agent alice; '
        "its window is ready for ax_snapshot"
    )
    assert desk.claims is not None and desk.claims.holds(900) and world.sleeps == []
    assert _error(desk.launch, "Nonexistent").code == "APP_NOT_FOUND"


def test_launch_waits_for_the_first_window(world: World) -> None:
    world.apps.installed["TextEdit"] = 900
    app = world.layer.add_app(900)
    desk = world.desk("alice")
    original = world._sleep

    def sleep(seconds: float) -> None:
        original(seconds)
        if len(world.sleeps) == 3:
            app.attrs["AXMainWindow"] = El("AXWindow", "Untitled")

    desk._sleep = sleep
    assert desk.launch("TextEdit").endswith("its window is ready for ax_snapshot")
    assert world.sleeps == [0.25, 0.25, 0.25]


def test_launch_of_an_app_that_never_shows_a_window_still_returns(world: World) -> None:
    world.apps.installed["Bartender"] = 910
    world.layer.add_app(910)
    out = world.desk("alice").launch("Bartender")
    assert "claimed by agent alice; it has shown no window yet" in out
    assert sum(world.sleeps) == 6.0


# --- find, read, wait --------------------------------------------------------------------


def test_find_returns_refs_usable_by_actions(world: World) -> None:
    desk = world.desk("alice")
    assert desk.find("Notes", query="new note") == '1 matches\n- button "New Note" [ref=e1]'
    desk.press(ref="e1")
    assert world.layer.performed == [(world.new, "AXPress")]
    assert desk.find("Notes", query="nothing here") == "no element matches 'nothing here'"
    assert _error(desk.find, "Notes", query=" ").code == "INVALID_ARGS"


def test_find_within_a_region(world: World) -> None:
    world.notes_window.add(El("AXGroup", "Sidebar", children=[button("New Folder")]))
    desk = world.desk("alice")
    desk.snapshot("Notes")
    assert desk.find(root="e4", query="new") == '1 matches\n- button "New Folder" [ref=e5]'


def test_read_returns_the_text_of_a_region(world: World) -> None:
    world.body.attrs["AXValue"] = "eggs\nmilk"
    desk = world.desk("alice")
    desk.snapshot("Notes")
    assert desk.read(ref="e3") == "eggs\nmilk"
    assert desk.read(ref="e1") == "Notes\nNew Note\neggs\nmilk"


def test_wait_for_polls_until_the_element_appears(world: World) -> None:
    desk = world.desk("alice")
    original = world._sleep

    def sleep(seconds: float) -> None:
        original(seconds)
        if len(world.sleeps) == 2:
            world.notes_window.add(text("Saved"))

    desk._sleep = sleep
    assert desk.wait_for("Notes", query="saved") == 'found\n- text "Saved"'
    assert world.sleeps == [0.3, 0.3]


def test_wait_for_times_out_and_can_wait_for_disappearance(world: World) -> None:
    desk = world.desk("alice")
    err = _error(desk.wait_for, "Notes", query="Saved", timeout=1.0)
    assert err.code == "TIMEOUT" and "did not appear within 1s" in err.message
    assert desk.wait_for("Notes", query="Saved", gone=True) == "'Saved' is gone"
    err = _error(desk.wait_for, "Notes", query="New Note", gone=True, timeout=0.5)
    assert "still present" in err.message


# --- keys --------------------------------------------------------------------------------


def test_keys_go_to_the_claimed_app_only(world: World) -> None:
    world.layer.focused[NOTES] = world.body
    desk = world.desk("alice")
    out = desk.key("Notes", keys="cmd+b cmd+i")
    assert world.keys == [(NOTES, "cmd+b"), (NOTES, "cmd+i")] and out.count("posted") == 2
    assert desk.type_keys("Notes", text="hi").startswith("typed 2 characters")
    assert world.texts == [(NOTES, "hi")]


def test_bad_or_blocked_keys_send_nothing(world: World) -> None:
    world.layer.focused[NOTES] = world.body
    desk = world.desk("alice")
    assert _error(desk.key, "Notes", keys="cmd+nosuchkey").code == "INVALID_ARGS"
    assert _error(desk.key, "Notes", keys="cmd+q").code == "POLICY_DENIED"
    assert _error(desk.key, "Notes", keys="  ").code == "INVALID_ARGS"
    assert world.keys == []


# --- observer and trace ------------------------------------------------------------------


def test_observer_reads_without_claiming_and_cannot_act(world: World) -> None:
    alice = world.desk("alice")
    alice.snapshot("Notes")
    watcher = world.observer()
    out = watcher.snapshot("Notes")
    assert 'button "New Note" [ref=e2]' in out and "agent observer" in out
    assert _error(watcher.press, ref="e2").code == "POLICY_DENIED"
    assert _error(watcher.key, "Notes", keys="return").code == "POLICY_DENIED"
    assert _error(watcher.launch, "Notes").code == "POLICY_DENIED"
    assert world.layer.performed == [] and alice.claims is not None and alice.claims.holds(NOTES)


def test_trace_records_actions_outcomes_and_hides_typed_text(world: World) -> None:
    desk = world.desk("alice")
    desk.snapshot("Notes")
    desk.insert_text(ref="e3", text="my secret diary")
    _error(desk.press, ref="e99")
    tools = [(e["tool"], e.get("error")) for e in world.trace]
    assert tools == [("snapshot", None), ("insert_text", None), ("press", "STALE_REF")]
    assert world.trace[1]["args"] == {"ref": "e3", "text": "<15 chars>"}
    assert "my secret diary" not in str(world.trace)
    assert world.trace[2]["delivery"] == "not_delivered"
    assert all(e["agent"] == "alice" for e in world.trace)


def test_file_trace_appends_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "alice.trace.jsonl"
    write = file_trace(path)
    write({"tool": "press", "agent": "alice"})
    write({"tool": "snapshot", "note": "筆記"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"tool": "press", "agent": "alice"}', '{"tool": "snapshot", "note": "筆記"}']


def test_actions_on_fields_route_through_the_desk(world: World) -> None:
    name = field("Title", "old")
    box = El("AXCheckBox", "Pinned", value=0, actions=("AXPress",),
             on_press=lambda el: el.attrs.update(AXValue=1))
    area = El("AXScrollArea", actions=("AXScrollDownByPage",), children=[name, box])
    world.notes_window.add(area)
    desk = world.desk("alice")
    desk.snapshot("Notes")
    assert desk.set_value(ref="e5", text="new").endswith("delivered_verified")
    assert desk.toggle(ref="e6", on=True).endswith("delivered_verified")
    assert desk.focus(ref="e5").endswith("delivered_verified")
    assert desk.scroll(ref="e6", direction="down", pages=2).startswith("AXScrollDownByPage x2")
    assert desk.perform(ref="e6", action="press").startswith("AXPress on")
