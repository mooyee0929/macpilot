from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from macpilot import fleet
from macpilot.fleet import AgentSpec, Completed, PlanError

PLAN = """
[defaults]
model = "sonnet"
timeout = 120

[[agent]]
name = "notes"
apps = ["Notes", "TextEdit"]
task = "  Write a grocery list.  "

[[agent]]
name = "calc"
app = "Calculator"
task = "Compute 12 * 34."
model = "haiku"
timeout = 60.5
"""


def _plan(tmp_path: Path, body: str = PLAN) -> Path:
    path = tmp_path / "plan.toml"
    path.write_text(body)
    return path


def _ok(result: str = "done", **extra: object) -> Completed:
    return Completed(0, json.dumps({"result": result, "is_error": False, **extra}), "")


# --- plan --------------------------------------------------------------------------------


def test_plan_applies_defaults_and_per_agent_overrides(tmp_path: Path) -> None:
    assert fleet.load_plan(_plan(tmp_path)) == [
        AgentSpec("notes", ("Notes", "TextEdit"), "Write a grocery list.", "sonnet", 120.0),
        AgentSpec("calc", ("Calculator",), "Compute 12 * 34.", "haiku", 60.5),
    ]


def test_plan_without_defaults_uses_the_builtin_timeout() -> None:
    specs = fleet.parse_plan({"agent": [{"name": "a", "app": "Notes", "task": "t"}]})
    assert specs == [AgentSpec("a", ("Notes",), "t", None, fleet.DEFAULT_TIMEOUT)]


@pytest.mark.parametrize(("agents", "message"), [
    ([], "no \\[\\[agent\\]\\] entries"),
    ([{"name": "a b", "app": "Notes", "task": "t"}], "name must be"),
    ([{"name": "a", "app": "Notes", "task": "  "}], "agent 'a': task is empty"),
    ([{"name": "a", "task": "t"}], "agent 'a': give app"),
    ([{"name": "a", "apps": [], "task": "t"}], "give app"),
    ([{"name": "a", "apps": ["Notes", 3], "task": "t"}], "give app"),
    ([{"name": "a", "app": "Notes", "task": "t", "timeout": 0}], "timeout must be"),
    ([{"name": "a", "app": "Notes", "task": "t", "timeout": True}], "timeout must be"),
    ([{"name": "a", "app": "Notes", "task": "t", "model": 5}], "model must be"),
    ([{"name": "a", "app": "Notes", "task": "t"}, {"name": "a", "app": "Mail", "task": "t"}],
     "two agents are named 'a'"),
    ([{"name": "a", "app": "Notes", "task": "t"}, {"name": "b", "apps": ["Mail", "notes"],
                                                    "task": "t"}],
     "notes is given to both 'a' and 'b'"),
])
def test_invalid_plans_are_rejected(agents: list[dict[str, object]], message: str) -> None:
    with pytest.raises(PlanError, match=message):
        fleet.parse_plan({"agent": agents})


def test_missing_or_malformed_plan_files(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="no plan file"):
        fleet.load_plan(tmp_path / "nope.toml")
    with pytest.raises(PlanError, match="not valid TOML"):
        fleet.load_plan(_plan(tmp_path, "[[agent]\nname ="))
    with pytest.raises(PlanError, match="defaults"):
        fleet.parse_plan({"defaults": 3, "agent": [{"name": "a", "app": "N", "task": "t"}]})


# --- what each agent is started with -----------------------------------------------------


def test_mcp_config_starts_the_server_in_agent_mode(tmp_path: Path) -> None:
    spec = AgentSpec("notes", ("Notes", "TextEdit"), "t")
    config = fleet.mcp_config(spec, tmp_path, tmp_path / "notes.trace.jsonl", "/usr/bin/python3")
    assert config == {"mcpServers": {"macpilot": {
        "command": "/usr/bin/python3",
        "args": ["-m", "macpilot.server"],
        "env": {
            "MACPILOT_AGENT": "notes",
            "MACPILOT_APPS": "Notes,TextEdit",
            "MACPILOT_HOME": str(tmp_path),
            "MACPILOT_TRACE": str(tmp_path / "notes.trace.jsonl"),
        },
    }}}


def test_command_isolates_the_agent_to_the_macpilot_server() -> None:
    spec = AgentSpec("calc", ("Calculator",), "Compute 12 * 34.", model="haiku")
    cmd = fleet.command_for(spec, Path("/runs/calc.mcp.json"))
    assert cmd[:2] == ["claude", "-p"]
    assert "Task:\nCompute 12 * 34." in cmd[2] and "agent 'calc'" in cmd[2]
    assert "You may operate only: Calculator." in cmd[2]
    assert cmd[3:] == [
        "--mcp-config", "/runs/calc.mcp.json", "--strict-mcp-config",
        "--allowedTools", "mcp__macpilot", "--output-format", "json", "--model", "haiku",
    ]
    assert "--model" not in fleet.command_for(AgentSpec("a", ("N",), "t"), Path("c.json"))


# --- interpreting a finished agent -------------------------------------------------------


def test_successful_run_is_summarised(tmp_path: Path) -> None:
    spec = AgentSpec("calc", ("Calculator",), "t")
    done = _ok("The result is 408.\n\nOUTCOME: done", num_turns=7, total_cost_usd=0.0312)
    result = fleet.interpret(spec, done, 12.4, tmp_path / "calc.out.json")
    assert result.ok and result.outcome == "done" and result.summary == "The result is 408."
    assert result.turns == 7 and result.cost_usd == pytest.approx(0.0312)
    assert fleet.format_result(result) == "[calc] done (12s, 7 turns, $0.03)\nThe result is 408."


@pytest.mark.parametrize(("report", "outcome", "body"), [
    ("Pressed the keys.\nOUTCOME: done", "done", "Pressed the keys."),
    ("Could not launch.\n\n**OUTCOME: blocked**\n", "blocked", "Could not launch."),
    ("Half of it.\n`outcome: Partial`", "partial", "Half of it."),
    ("OUTCOME: done\nA trailing remark.", "done", "A trailing remark."),
    ("I am done with the outcome: all good.", "unknown", "I am done with the outcome: all good."),
    ("OUTCOME: done\n1\n2\n3\n4", "unknown", "OUTCOME: done\n1\n2\n3\n4"),
    ("", "unknown", ""),
])
def test_declared_outcome_is_read_from_the_last_lines(report: str, outcome: str, body: str) -> None:
    assert fleet.split_outcome(report) == (outcome, body)


def test_an_agent_that_finishes_blocked_is_not_counted_as_done(tmp_path: Path) -> None:
    spec = AgentSpec("calc", ("Calculator",), "t")
    result = fleet.interpret(spec, _ok("Calculator never started.\nOUTCOME: blocked"), 42.0,
                             tmp_path / "calc.out.json")
    assert not result.ok and result.outcome == "blocked"
    assert fleet.format_result(result).startswith("[calc] blocked (42s)")
    silent = fleet.interpret(spec, _ok("Did things."), 1.0, tmp_path / "calc.out.json")
    assert silent.outcome == "unknown" and not silent.ok


def test_tally_counts_each_outcome_in_a_fixed_order() -> None:
    results = [fleet.Result(str(i), o, "", 1.0) for i, o in enumerate(  # type: ignore[arg-type]
        ["blocked", "done", "failed", "done"])]
    assert fleet.tally(results) == "2 done, 1 blocked, 1 failed"
    assert fleet.tally([]) == "no agents"


@pytest.mark.parametrize(("done", "summary"), [
    (Completed(None, "", "", timed_out=True), "timed out after 900s"),
    (Completed(1, "", "Not logged in"), "exit 1, unreadable output: Not logged in"),
    (Completed(0, "[1, 2]", ""), "unexpected output shape"),
    (Completed(0, json.dumps({"result": "Hit the turn limit", "is_error": True}), ""),
     "Hit the turn limit"),
    (Completed(2, json.dumps({"result": "cut short\nOUTCOME: done"}), ""), "cut short"),
    (Completed(127, "", "cannot start claude: missing"), "cannot start claude: missing"),
])
def test_failed_runs_are_not_reported_as_ok(done: Completed, summary: str, tmp_path: Path) -> None:
    result = fleet.interpret(AgentSpec("a", ("N",), "t"), done, 1.0, tmp_path / "a.out.json")
    assert not result.ok and result.outcome == "failed" and summary in result.summary
    assert fleet.format_result(result).startswith("[a] FAILED")


# --- running a plan ----------------------------------------------------------------------


def test_agents_run_at_the_same_time_and_results_keep_plan_order(tmp_path: Path) -> None:
    specs = fleet.load_plan(_plan(tmp_path))
    both_running = threading.Barrier(2, timeout=5)
    seen: list[tuple[str, float, Path]] = []
    finished: list[str] = []

    def runner(cmd: Sequence[str], timeout: float, cwd: Path) -> Completed:
        both_running.wait()
        config = json.loads(Path(cmd[cmd.index("--mcp-config") + 1]).read_text())
        agent = config["mcpServers"]["macpilot"]["env"]["MACPILOT_AGENT"]
        seen.append((agent, timeout, cwd))
        if agent == "calc":
            return Completed(1, "", "boom")
        return _ok(f"{agent} finished\nOUTCOME: done")

    run_dir = tmp_path / "runs" / "r1"
    results = fleet.run_plan(specs, run_dir, home=tmp_path, runner=runner, python="py",
                             on_done=lambda r: finished.append(r.name))
    assert [r.name for r in results] == ["notes", "calc"]
    assert [r.ok for r in results] == [True, False]
    assert sorted(seen) == [("calc", 60.5, run_dir), ("notes", 120.0, run_dir)]
    assert sorted(finished) == ["calc", "notes"]
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "calc.err.txt", "calc.mcp.json", "calc.out.json", "notes.mcp.json", "notes.out.json",
    ]
    env = json.loads((run_dir / "notes.mcp.json").read_text())["mcpServers"]["macpilot"]["env"]
    assert env["MACPILOT_TRACE"] == str(run_dir / "notes.trace.jsonl")
    raw = json.loads((run_dir / "notes.out.json").read_text())["result"]
    assert raw == "notes finished\nOUTCOME: done" and results[0].summary == "notes finished"


def test_a_plan_that_shares_an_app_never_starts(tmp_path: Path) -> None:
    specs = [AgentSpec("a", ("Notes",), "t"), AgentSpec("b", ("notes",), "t")]
    started: list[Sequence[str]] = []

    def runner(cmd: Sequence[str], timeout: float, cwd: Path) -> Completed:
        started.append(cmd)
        return _ok()

    with pytest.raises(PlanError, match="given to both"):
        fleet.run_plan(specs, tmp_path / "run", home=tmp_path, runner=runner)
    assert started == [] and not (tmp_path / "run").exists()


def test_subprocess_runner_reports_timeouts_and_missing_binaries(tmp_path: Path) -> None:
    slow = fleet.subprocess_runner(["sleep", "5"], 0.2, tmp_path)
    assert slow.timed_out and slow.returncode is None
    missing = fleet.subprocess_runner(["macpilot-no-such-binary"], 5, tmp_path)
    assert missing.returncode == 127 and "cannot start" in missing.stderr
    echo = fleet.subprocess_runner(["sh", "-c", "pwd; echo err >&2; exit 3"], 5, tmp_path)
    assert echo.returncode == 3 and echo.stderr.strip() == "err"
    assert Path(echo.stdout.strip()).resolve() == tmp_path.resolve()


def test_run_directories_are_timestamped_under_home(tmp_path: Path) -> None:
    run_dir = fleet.new_run_dir(tmp_path, now=lambda: 0.0)
    assert run_dir.parent == tmp_path / "runs" and len(run_dir.name) == len("19700101-000000")
