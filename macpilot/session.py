"""Who is driving which app: per-agent configuration and cross-process app claims.

Several agents share one desktop, each in its own MCP server process. An agent claims an app
before reading or acting on it, and a claim is exclusive: a second agent asking for the same
app is refused until the first releases it or its process exits. Claims are small JSON files
so that unrelated processes agree on them without a daemon.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from macpilot.errors import APP_CLAIMED, INVALID_ARGS, DeskError

ENV_AGENT = "MACPILOT_AGENT"
ENV_APPS = "MACPILOT_APPS"
ENV_POINTER = "MACPILOT_POINTER"
ENV_HOME = "MACPILOT_HOME"
ENV_TRACE = "MACPILOT_TRACE"

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ON = {"1", "on", "true", "yes"}
_OFF = {"0", "off", "false", "no"}


def default_home() -> Path:
    return Path.home() / "Library" / "Application Support" / "macpilot"


def valid_agent_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


@dataclass(frozen=True)
class AgentConfig:
    """How this server process behaves, read once from the environment.

    Without ``MACPILOT_AGENT`` the server is a solo desktop driver with every tool. With it,
    the server is one agent among several: the tools that move the real pointer, send global
    key events or bring an app to the front are not registered at all, and ``apps`` (when
    set) is the only set of apps it may claim.
    """

    agent: str
    named: bool
    apps: frozenset[str] | None
    pointer: bool
    home: Path
    trace: Path | None

    @classmethod
    def from_env(cls, env: Mapping[str, str], pid: int) -> AgentConfig:
        raw = env.get(ENV_AGENT, "").strip()
        if raw and not valid_agent_name(raw):
            raise ValueError(
                f"{ENV_AGENT} must be 1-64 letters, digits, '-' or '_', got {raw!r}"
            )
        apps_raw = [a.strip().lower() for a in env.get(ENV_APPS, "").split(",") if a.strip()]
        pointer_raw = env.get(ENV_POINTER, "").strip().lower()
        if pointer_raw and pointer_raw not in _ON | _OFF:
            raise ValueError(f"{ENV_POINTER} must be on or off, got {pointer_raw!r}")
        pointer = pointer_raw in _ON if pointer_raw else not raw
        home = Path(env[ENV_HOME]).expanduser() if env.get(ENV_HOME) else default_home()
        trace = Path(env[ENV_TRACE]).expanduser() if env.get(ENV_TRACE) else None
        return cls(
            agent=raw or f"solo-{pid}",
            named=bool(raw),
            apps=frozenset(apps_raw) if apps_raw else None,
            pointer=pointer,
            home=home,
            trace=trace,
        )


@dataclass(frozen=True)
class Claim:
    app: str
    pid: int
    agent: str
    owner_pid: int
    since: float


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Claims:
    """Exclusive app claims for one agent, stored under ``<home>/claims/<app pid>.json``."""

    def __init__(
        self,
        root: Path,
        agent: str,
        owner_pid: int | None = None,
        alive: Callable[[int], bool] = process_alive,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self.agent = agent
        self.owner_pid = os.getpid() if owner_pid is None else owner_pid
        self._alive = alive
        self._clock = clock

    def _path(self, pid: int) -> Path:
        return self.root / f"{pid}.json"

    def _read(self, path: Path) -> Claim | None:
        try:
            data = json.loads(path.read_text())
            return Claim(
                app=str(data["app"]),
                pid=int(data["pid"]),
                agent=str(data["agent"]),
                owner_pid=int(data["owner_pid"]),
                since=float(data["since"]),
            )
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError):
            # A claim nobody can read protects nothing; treat it as abandoned.
            return None

    def _stale(self, claim: Claim, app: str) -> bool:
        return not self._alive(claim.owner_pid) or claim.app != app

    def holder(self, pid: int) -> Claim | None:
        return self._read(self._path(pid))

    def holds(self, pid: int) -> bool:
        claim = self.holder(pid)
        return claim is not None and claim.agent == self.agent and claim.owner_pid == self.owner_pid

    def acquire(self, app: str, pid: int) -> Claim:
        """Claim `app` (process `pid`) for this agent, or raise APP_CLAIMED."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(pid)
        mine = Claim(app=app, pid=pid, agent=self.agent, owner_pid=self.owner_pid,
                     since=self._clock())
        for _ in range(3):
            current = self._read(path)
            if current is not None and current.agent == self.agent and current.app == app:
                if current.owner_pid == self.owner_pid:
                    return current
                if self._alive(current.owner_pid):
                    raise DeskError(
                        APP_CLAIMED,
                        f"{app} is held by another running process of agent "
                        f"'{current.agent}' (pid {current.owner_pid})",
                    )
            elif current is not None and not self._stale(current, app):
                raise DeskError(
                    APP_CLAIMED,
                    f"{app} is being driven by agent '{current.agent}'",
                    hint="work on another app, or wait until that agent releases it",
                )
            if path.exists() and not self._remove_if_unchanged(path, current):
                continue
            if self._create(path, mine):
                return mine
        raise DeskError(APP_CLAIMED, f"could not claim {app}: other agents keep taking it")

    def _create(self, path: Path, claim: Claim) -> bool:
        """Publish the claim atomically; False when someone else got there first."""
        temp = path.with_name(f".{path.name}.{self.owner_pid}.tmp")
        temp.write_text(json.dumps(asdict(claim)))
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        finally:
            temp.unlink(missing_ok=True)
        return True

    def _remove_if_unchanged(self, path: Path, seen: Claim | None) -> bool:
        """Retire the abandoned claim we just read, unless it was replaced meanwhile."""
        parked = path.with_name(f".{path.name}.{self.owner_pid}.stale")
        try:
            os.rename(path, parked)
        except FileNotFoundError:
            return True
        if self._read(parked) != seen:
            # Someone published a fresh claim between our read and the rename: put it back,
            # unless a third claim already took its place, in which case that one stands.
            with contextlib.suppress(FileExistsError):
                os.link(parked, path)
            parked.unlink(missing_ok=True)
            return False
        parked.unlink(missing_ok=True)
        return True

    def release(self, pid: int) -> bool:
        if not self.holds(pid):
            return False
        self._path(pid).unlink(missing_ok=True)
        return True

    def release_all(self) -> int:
        released = 0
        for claim in self.all():
            if self.release(claim.pid):
                released += 1
        return released

    def all(self) -> list[Claim]:
        """Every live claim, oldest first. Abandoned ones are skipped, not deleted."""
        if not self.root.is_dir():
            return []
        claims = [c for p in self.root.glob("[0-9]*.json") if (c := self._read(p)) is not None]
        live = (c for c in claims if self._alive(c.owner_pid))
        return sorted(live, key=lambda c: (c.since, c.pid))


def require_agent_name(name: str) -> str:
    if not valid_agent_name(name):
        raise DeskError(
            INVALID_ARGS, f"agent name must be 1-64 letters, digits, '-' or '_', got {name!r}"
        )
    return name
