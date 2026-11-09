from __future__ import annotations

import asyncio
from pathlib import Path

from fakes import El, FakeApps, FakeTree, button

from macpilot.desk import Desk
from macpilot.server import build_server
from macpilot.session import AgentConfig, Claims

PHYSICAL = {"click", "move_mouse", "drag", "scroll", "type_text", "press_key", "open_app",
            "open_url"}
HEADLESS = {
    "ax_snapshot", "ax_find", "ax_read", "ax_wait", "ax_press", "ax_set_value",
    "ax_insert_text", "ax_toggle", "ax_focus", "ax_scroll", "ax_perform", "ax_key",
    "ax_type_keys", "ax_launch", "ax_release", "ax_claims",
}


def _desk(tmp_path: Path, agent: str) -> Desk:
    layer = FakeTree()
    layer.add_app(500, El("AXWindow", "Notes", children=[button("New Note")]))
    claims = Claims(tmp_path / "claims", agent, alive=lambda pid: True)
    return Desk(layer, FakeApps({"Notes": 500}), claims, agent=agent)


def _tools(env: dict[str, str], tmp_path: Path) -> tuple[set[str], str]:
    config = AgentConfig.from_env({"MACPILOT_HOME": str(tmp_path), **env}, 1)
    server = build_server(config, desk=_desk(tmp_path, config.agent))
    tools = asyncio.run(server.list_tools())
    return {tool.name for tool in tools}, server.instructions or ""


def test_solo_server_has_both_tool_families(tmp_path: Path) -> None:
    names, instructions = _tools({}, tmp_path)
    assert names >= PHYSICAL and names >= HEADLESS
    assert "interrupt the human" in instructions and "You are agent" not in instructions


def test_agent_server_has_no_tool_that_sends_physical_input(tmp_path: Path) -> None:
    names, instructions = _tools({"MACPILOT_AGENT": "notes", "MACPILOT_APPS": "Notes"}, tmp_path)
    assert not names & PHYSICAL and names >= HEADLESS
    assert {"screenshot_window", "read_text"} <= names
    assert "You are agent 'notes'" in instructions and "may only operate: notes" in instructions


def test_agent_can_be_given_the_pointer_back_explicitly(tmp_path: Path) -> None:
    names, _ = _tools({"MACPILOT_AGENT": "notes", "MACPILOT_POINTER": "on"}, tmp_path)
    assert names >= PHYSICAL
