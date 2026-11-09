from __future__ import annotations

from pathlib import Path

import pytest

from macpilot.errors import DeskError
from macpilot.session import AgentConfig, Claims, default_home, valid_agent_name


class Processes:
    """Which owner processes are alive."""

    def __init__(self, *alive: int) -> None:
        self.alive = set(alive)

    def __call__(self, pid: int) -> bool:
        return pid in self.alive


def _claims(root: Path, agent: str, owner: int, procs: Processes) -> Claims:
    return Claims(root, agent, owner_pid=owner, alive=procs, clock=lambda: 1000.0)


# --- configuration -----------------------------------------------------------------------


def test_without_an_agent_name_the_server_is_a_solo_driver_with_the_pointer() -> None:
    cfg = AgentConfig.from_env({}, 4242)
    assert cfg.agent == "solo-4242" and not cfg.named
    assert cfg.pointer and cfg.apps is None and cfg.trace is None
    assert cfg.home == default_home()


def test_a_named_agent_loses_the_pointer_and_gets_its_app_list() -> None:
    cfg = AgentConfig.from_env(
        {"MACPILOT_AGENT": "notes-1", "MACPILOT_APPS": "Notes, TextEdit ,",
         "MACPILOT_HOME": "/tmp/mp", "MACPILOT_TRACE": "/tmp/mp/t.jsonl"},
        1,
    )
    assert cfg.agent == "notes-1" and cfg.named and not cfg.pointer
    assert cfg.apps == frozenset({"notes", "textedit"})
    assert cfg.home == Path("/tmp/mp") and cfg.trace == Path("/tmp/mp/t.jsonl")


@pytest.mark.parametrize(("raw", "expected"), [("on", True), ("1", True), ("OFF", False)])
def test_pointer_can_be_overridden_either_way(raw: str, expected: bool) -> None:
    assert AgentConfig.from_env({"MACPILOT_AGENT": "a", "MACPILOT_POINTER": raw}, 1).pointer \
        is expected
    assert AgentConfig.from_env({"MACPILOT_POINTER": raw}, 1).pointer is expected


def test_bad_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="MACPILOT_AGENT"):
        AgentConfig.from_env({"MACPILOT_AGENT": "two words"}, 1)
    with pytest.raises(ValueError, match="MACPILOT_POINTER"):
        AgentConfig.from_env({"MACPILOT_POINTER": "maybe"}, 1)


@pytest.mark.parametrize("name", ["a", "notes-1", "Agent_2", "x" * 64])
def test_valid_agent_names(name: str) -> None:
    assert valid_agent_name(name)


@pytest.mark.parametrize("name", ["", "two words", "a/b", "x" * 65, "naïve"])
def test_invalid_agent_names(name: str) -> None:
    assert not valid_agent_name(name)


# --- claims ------------------------------------------------------------------------------


def test_claim_is_exclusive_between_agents(tmp_path: Path) -> None:
    procs = Processes(10, 20)
    alice = _claims(tmp_path, "alice", 10, procs)
    bob = _claims(tmp_path, "bob", 20, procs)
    claim = alice.acquire("Notes", 500)
    assert claim.agent == "alice" and alice.holds(500) and not bob.holds(500)
    with pytest.raises(DeskError, match="driven by agent 'alice'") as err:
        bob.acquire("Notes", 500)
    assert err.value.code == "APP_CLAIMED"
    assert alice.holds(500)


def test_reacquiring_your_own_claim_is_a_noop(tmp_path: Path) -> None:
    alice = _claims(tmp_path, "alice", 10, Processes(10))
    first = alice.acquire("Notes", 500)
    assert alice.acquire("Notes", 500) == first


def test_agents_hold_different_apps_at_the_same_time(tmp_path: Path) -> None:
    procs = Processes(10, 20)
    alice = _claims(tmp_path, "alice", 10, procs)
    bob = _claims(tmp_path, "bob", 20, procs)
    alice.acquire("Notes", 500)
    bob.acquire("Calculator", 600)
    assert [(c.app, c.agent) for c in alice.all()] == [("Notes", "alice"), ("Calculator", "bob")]


def test_release_hands_the_app_to_the_next_agent(tmp_path: Path) -> None:
    procs = Processes(10, 20)
    alice = _claims(tmp_path, "alice", 10, procs)
    bob = _claims(tmp_path, "bob", 20, procs)
    alice.acquire("Notes", 500)
    assert not bob.release(500)
    assert alice.release(500) and not alice.holds(500)
    assert bob.acquire("Notes", 500).agent == "bob"


def test_claim_of_a_dead_process_is_taken_over(tmp_path: Path) -> None:
    procs = Processes(10, 20)
    _claims(tmp_path, "alice", 10, procs).acquire("Notes", 500)
    procs.alive.discard(10)
    bob = _claims(tmp_path, "bob", 20, procs)
    assert bob.acquire("Notes", 500).agent == "bob" and bob.holds(500)


def test_claim_left_on_a_reused_pid_does_not_block_a_different_app(tmp_path: Path) -> None:
    procs = Processes(10, 20)
    _claims(tmp_path, "alice", 10, procs).acquire("Notes", 500)
    bob = _claims(tmp_path, "bob", 20, procs)
    assert bob.acquire("Safari", 500).app == "Safari"


def test_unreadable_claim_file_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "500.json").write_text("{not json")
    alice = _claims(tmp_path, "alice", 10, Processes(10))
    assert alice.holder(500) is None
    assert alice.acquire("Notes", 500).agent == "alice"


def test_restarted_agent_recovers_its_own_claim_but_not_from_a_live_twin(tmp_path: Path) -> None:
    procs = Processes(10, 11)
    _claims(tmp_path, "alice", 10, procs).acquire("Notes", 500)
    twin = _claims(tmp_path, "alice", 11, procs)
    with pytest.raises(DeskError, match="another running process of agent 'alice'"):
        twin.acquire("Notes", 500)
    procs.alive.discard(10)
    assert twin.acquire("Notes", 500).owner_pid == 11 and twin.holds(500)


def test_listing_skips_abandoned_claims_and_release_all_only_frees_mine(tmp_path: Path) -> None:
    procs = Processes(10, 20, 30)
    alice = _claims(tmp_path, "alice", 10, procs)
    bob = _claims(tmp_path, "bob", 20, procs)
    alice.acquire("Notes", 500)
    alice.acquire("TextEdit", 501)
    bob.acquire("Calculator", 600)
    _claims(tmp_path, "carol", 30, procs).acquire("Safari", 700)
    procs.alive.discard(30)
    assert {c.app for c in alice.all()} == {"Notes", "TextEdit", "Calculator"}
    assert alice.release_all() == 2
    assert [c.app for c in bob.all()] == ["Calculator"]


def test_nothing_is_listed_before_any_claim(tmp_path: Path) -> None:
    assert _claims(tmp_path / "missing", "alice", 10, Processes(10)).all() == []


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    procs = Processes(10, 20)
    _claims(tmp_path, "alice", 10, procs).acquire("Notes", 500)
    procs.alive.discard(10)
    _claims(tmp_path, "bob", 20, procs).acquire("Notes", 500)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["500.json"]
