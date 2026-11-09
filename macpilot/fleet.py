"""Run several headless agents at once, each confined to its own apps.

A plan is a TOML file:

    [defaults]
    model = "sonnet"      # optional, passed to `claude --model`
    timeout = 900         # seconds per agent

    [[agent]]
    name = "notes"
    apps = ["Notes"]
    task = "Create a note titled Groceries with three items."

    [[agent]]
    name = "calc"
    app = "Calculator"
    task = "Compute 12 * 34 and report the result."

Each agent is one `claude -p` process with a private MCP config that starts this package's
server in agent mode (``MACPILOT_AGENT`` / ``MACPILOT_APPS``): only the headless ax_* tools
exist there, so no agent can move the pointer or take keyboard focus, and the app claims stop
two agents from driving the same app. No two agents in one plan may list the same app.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from macpilot.session import (
    ENV_AGENT,
    ENV_APPS,
    ENV_HOME,
    ENV_TRACE,
    default_home,
    valid_agent_name,
)

DEFAULT_TIMEOUT = 900.0
SERVER_NAME = "macpilot"

PROMPT = """\
You are agent '{name}', one of several agents working at the same time on one macOS desktop. \
A human is using the mouse and keyboard, so you work only through the macpilot ax_* tools, \
which act through the Accessibility API without moving the pointer, taking keyboard focus or \
touching the clipboard.

You may operate only: {apps}.

How to work:
- Start with ax_snapshot on your app (ax_launch first if it is not running).
- Act on refs from the snapshot, then read again to verify: a successful result only means \
the app accepted the request.
- For a dense app use ax_snapshot skeleton=true, then drill with root=<ref>; ax_find jumps \
straight to a known label.
- Errors read "CODE: message [delivery]". Retry as-is only when delivery is not_delivered. \
After "uncertain", read the UI before doing anything else. STALE_REF means take a new snapshot.
- The tools fail closed. If a step has no accessibility route, stop and report exactly what \
blocked you; do not look for another way around.

Task:
{task}

Finish with a short report: what you did, the outcome, and anything left undone. Make the \
very last line exactly one of:
OUTCOME: done
OUTCOME: partial
OUTCOME: blocked"""


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class AgentSpec:
    name: str
    apps: tuple[str, ...]
    task: str
    model: str | None = None
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class Completed:
    """What a runner reports for one finished process."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


# "failed" is the run itself breaking (crash, timeout, API error). The other values are what
# the agent said about its task; "unknown" means it finished without saying.
Outcome = Literal["done", "partial", "blocked", "unknown", "failed"]
_OUTCOME_LINE = re.compile(r"^[\s*`_>]*OUTCOME:\s*(done|partial|blocked)\b[\s*`_.]*$",
                           re.IGNORECASE)


@dataclass(frozen=True)
class Result:
    name: str
    outcome: Outcome
    summary: str
    seconds: float
    turns: int | None = None
    cost_usd: float | None = None
    log: Path | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "done"


def split_outcome(report: str) -> tuple[Outcome, str]:
    """The agent's declared outcome and its report without that line."""
    lines = report.rstrip().splitlines()
    for index in range(len(lines) - 1, max(len(lines) - 4, -1), -1):
        match = _OUTCOME_LINE.match(lines[index])
        if match:
            word = match.group(1).lower()
            body = "\n".join(lines[:index] + lines[index + 1:]).strip()
            outcome: Outcome = "done" if word == "done" else (
                "partial" if word == "partial" else "blocked"
            )
            return outcome, body
    return "unknown", report.strip()


Runner = Callable[[Sequence[str], float, Path], Completed]


def _as_apps(entry: dict[str, object], where: str) -> tuple[str, ...]:
    raw = entry.get("apps", entry.get("app"))
    if isinstance(raw, str):
        raw = [raw]
    valid = isinstance(raw, list) and bool(raw) and all(
        isinstance(a, str) and a.strip() for a in raw
    )
    if not valid or not isinstance(raw, list):
        raise PlanError(f"{where}: give app = \"Name\" or apps = [\"A\", \"B\"]")
    return tuple(a.strip() for a in raw)


def parse_plan(data: dict[str, object]) -> list[AgentSpec]:
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise PlanError("[defaults] must be a table")
    entries = data.get("agent")
    if not isinstance(entries, list) or not entries:
        raise PlanError("the plan has no [[agent]] entries")
    specs: list[AgentSpec] = []
    for index, entry in enumerate(entries, start=1):
        where = f"agent #{index}"
        if not isinstance(entry, dict):
            raise PlanError(f"{where}: must be a table")
        name = entry.get("name")
        if not isinstance(name, str) or not valid_agent_name(name):
            raise PlanError(f"{where}: name must be 1-64 letters, digits, '-' or '_'")
        where = f"agent '{name}'"
        task = entry.get("task")
        if not isinstance(task, str) or not task.strip():
            raise PlanError(f"{where}: task is empty")
        model = entry.get("model", defaults.get("model"))
        if model is not None and not isinstance(model, str):
            raise PlanError(f"{where}: model must be a string")
        timeout = entry.get("timeout", defaults.get("timeout", DEFAULT_TIMEOUT))
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise PlanError(f"{where}: timeout must be a positive number of seconds")
        specs.append(AgentSpec(name=name, apps=_as_apps(entry, where), task=task.strip(),
                               model=model, timeout=float(timeout)))
    validate(specs)
    return specs


def validate(specs: Sequence[AgentSpec]) -> None:
    """Names are unique and no app is given to two agents."""
    names: set[str] = set()
    owners: dict[str, str] = {}
    for spec in specs:
        if spec.name in names:
            raise PlanError(f"two agents are named '{spec.name}'")
        names.add(spec.name)
        for app in spec.apps:
            key = app.lower()
            if key in owners:
                raise PlanError(
                    f"{app} is given to both '{owners[key]}' and '{spec.name}'; "
                    "one app can only be driven by one agent"
                )
            owners[key] = spec.name


def load_plan(path: Path) -> list[AgentSpec]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise PlanError(f"no plan file at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PlanError(f"{path} is not valid TOML: {exc}") from exc
    return parse_plan(data)


def mcp_config(spec: AgentSpec, home: Path, trace: Path, python: str) -> dict[str, object]:
    return {
        "mcpServers": {
            SERVER_NAME: {
                "command": python,
                "args": ["-m", "macpilot.server"],
                "env": {
                    ENV_AGENT: spec.name,
                    ENV_APPS: ",".join(spec.apps),
                    ENV_HOME: str(home),
                    ENV_TRACE: str(trace),
                },
            }
        }
    }


def prompt_for(spec: AgentSpec) -> str:
    return PROMPT.format(name=spec.name, apps=", ".join(spec.apps), task=spec.task)


def command_for(spec: AgentSpec, config_path: Path, claude: str = "claude") -> list[str]:
    cmd = [
        claude, "-p", prompt_for(spec),
        "--mcp-config", str(config_path),
        "--strict-mcp-config",
        "--allowedTools", f"mcp__{SERVER_NAME}",
        "--output-format", "json",
    ]
    if spec.model:
        cmd += ["--model", spec.model]
    return cmd


def _text(stream: str | bytes | None) -> str:
    if isinstance(stream, bytes):
        return stream.decode(errors="replace")
    return stream or ""


def subprocess_runner(cmd: Sequence[str], timeout: float, cwd: Path) -> Completed:
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout,
                              cwd=cwd, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        return Completed(None, _text(exc.stdout), _text(exc.stderr), timed_out=True)
    except FileNotFoundError as exc:
        return Completed(127, "", f"cannot start {cmd[0]}: {exc}")
    return Completed(proc.returncode, proc.stdout, proc.stderr)


def interpret(spec: AgentSpec, done: Completed, seconds: float, log: Path) -> Result:
    """Turn one finished `claude -p --output-format json` run into a Result."""
    if done.timed_out:
        return Result(spec.name, "failed", f"timed out after {spec.timeout:g}s", seconds, log=log)
    try:
        payload = json.loads(done.stdout)
    except ValueError:
        tail = (done.stderr.strip() or done.stdout.strip() or "no output")[-400:]
        return Result(spec.name, "failed", f"exit {done.returncode}, unreadable output: {tail}",
                      seconds, log=log)
    if not isinstance(payload, dict):
        return Result(spec.name, "failed", "unexpected output shape", seconds, log=log)
    outcome, text = split_outcome(str(payload.get("result") or ""))
    turns = payload.get("num_turns")
    cost = payload.get("total_cost_usd")
    if bool(payload.get("is_error")) or done.returncode != 0:
        outcome = "failed"
    return Result(
        name=spec.name,
        outcome=outcome,
        summary=text or "(empty report)",
        seconds=seconds,
        turns=turns if isinstance(turns, int) else None,
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        log=log,
    )


def run_agent(
    spec: AgentSpec,
    run_dir: Path,
    home: Path,
    runner: Runner,
    python: str,
    clock: Callable[[], float] = time.monotonic,
) -> Result:
    config_path = run_dir / f"{spec.name}.mcp.json"
    trace = run_dir / f"{spec.name}.trace.jsonl"
    log = run_dir / f"{spec.name}.out.json"
    config_path.write_text(json.dumps(mcp_config(spec, home, trace, python), indent=1))
    started = clock()
    done = runner(command_for(spec, config_path), spec.timeout, run_dir)
    seconds = clock() - started
    log.write_text(done.stdout)
    if done.stderr.strip():
        (run_dir / f"{spec.name}.err.txt").write_text(done.stderr)
    return interpret(spec, done, seconds, log)


def run_plan(
    specs: Sequence[AgentSpec],
    run_dir: Path,
    *,
    home: Path | None = None,
    runner: Runner = subprocess_runner,
    python: str = sys.executable,
    on_done: Callable[[Result], None] | None = None,
) -> list[Result]:
    """Run every agent concurrently; results come back in plan order."""
    validate(specs)
    run_dir.mkdir(parents=True, exist_ok=True)
    base = home if home is not None else default_home()
    results: dict[str, Result] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(specs))) as pool:
        futures = {
            pool.submit(run_agent, spec, run_dir, base, runner, python): spec for spec in specs
        }
        for future in as_completed(futures):
            result = future.result()
            results[result.name] = result
            if on_done is not None:
                on_done(result)
    return [results[spec.name] for spec in specs]


def new_run_dir(home: Path | None = None, now: Callable[[], float] = time.time) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
    return (home if home is not None else default_home()) / "runs" / stamp


def format_result(result: Result) -> str:
    status = "FAILED" if result.outcome == "failed" else result.outcome
    extra = []
    if result.turns is not None:
        extra.append(f"{result.turns} turns")
    if result.cost_usd is not None:
        extra.append(f"${result.cost_usd:.2f}")
    meta = f"{result.seconds:.0f}s" + (", " + ", ".join(extra) if extra else "")
    return f"[{result.name}] {status} ({meta})\n{result.summary}"


def tally(results: Sequence[Result]) -> str:
    """One line such as "2 done, 1 blocked" in a fixed order."""
    order: tuple[Outcome, ...] = ("done", "partial", "blocked", "unknown", "failed")
    counts = [(sum(1 for r in results if r.outcome == o), o) for o in order]
    return ", ".join(f"{n} {o}" for n, o in counts if n) or "no agents"
