#!/usr/bin/env python3
"""Local power controller for a scheduled cloud development machine."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_PATH = Path("/etc/devbox-power.conf")
DEFAULT_STATE_PATH = Path("/var/lib/devbox-power/state.json")
DEFAULT_LOCK_PATH = Path("/run/devbox-power/state.lock")
STATE_VERSION = 1
VALID_STATUSES = {"scheduled", "stopping"}


class PowerError(RuntimeError):
    """A user-facing controller error."""


@dataclass(frozen=True)
class Config:
    timezone: ZoneInfo
    timezone_name: str
    default_stop_hour: int
    notify_before_minutes: int
    terminal_users: tuple[str, ...]
    state_file: Path = DEFAULT_STATE_PATH
    lock_file: Path = DEFAULT_LOCK_PATH


def load_config(path: Path) -> Config:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise PowerError(f"Configuration file not found: {path}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PowerError(f"Invalid configuration at {path}:{line_number}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    timezone_name = values.get("TIMEZONE", "America/Los_Angeles")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise PowerError(f"Unknown timezone: {timezone_name}") from exc

    default_stop_hour = _bounded_int(
        values.get("DEFAULT_STOP_HOUR", "19"),
        "DEFAULT_STOP_HOUR",
        minimum=0,
        maximum=23,
    )
    notify_before_minutes = _bounded_int(
        values.get("NOTIFY_BEFORE_MINUTES", "30"),
        "NOTIFY_BEFORE_MINUTES",
        minimum=1,
        maximum=1440,
    )
    terminal_users = tuple(
        user.strip()
        for user in values.get("TERMINAL_USERS", "ubuntu").split(",")
        if user.strip()
    )
    if not terminal_users:
        raise PowerError("TERMINAL_USERS must contain at least one user")

    state_file = Path(values.get("STATE_FILE", str(DEFAULT_STATE_PATH)))
    lock_file = Path(values.get("LOCK_FILE", str(DEFAULT_LOCK_PATH)))
    if not state_file.is_absolute() or not lock_file.is_absolute():
        raise PowerError("STATE_FILE and LOCK_FILE must be absolute paths")

    return Config(
        timezone=timezone,
        timezone_name=timezone_name,
        default_stop_hour=default_stop_hour,
        notify_before_minutes=notify_before_minutes,
        terminal_users=terminal_users,
        state_file=state_file,
        lock_file=lock_file,
    )


def _bounded_int(value: str, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise PowerError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise PowerError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def next_hour_epoch(now_epoch: int, hour: int, timezone: ZoneInfo) -> int:
    """Return the next occurrence of a whole local clock hour."""
    local_now = datetime.fromtimestamp(now_epoch, timezone)
    candidate = datetime.combine(
        local_now.date(), datetime_time(hour=hour), tzinfo=timezone
    )
    if int(candidate.timestamp()) <= now_epoch:
        next_date = local_now.date() + timedelta(days=1)
        candidate = datetime.combine(
            next_date, datetime_time(hour=hour), tzinfo=timezone
        )
    return int(candidate.timestamp())


def new_state(stop_at: int, now_epoch: int, updated_by: str) -> dict[str, object]:
    return {
        "version": STATE_VERSION,
        "stop_at": stop_at,
        "warning_sent_for": None,
        "status": "scheduled",
        "updated_at": now_epoch,
        "updated_by": updated_by,
    }


def initialize_state(
    state: dict[str, object] | None,
    now_epoch: int,
    config: Config,
) -> tuple[dict[str, object], bool]:
    """Preserve a future deadline, otherwise create the next default deadline."""
    if state is not None and int(state["stop_at"]) > now_epoch:
        if state["status"] == "stopping":
            state = copy.deepcopy(state)
            state["status"] = "scheduled"
            state["updated_at"] = now_epoch
            state["updated_by"] = "boot-recovery"
            return state, True
        return state, False

    stop_at = next_hour_epoch(now_epoch, config.default_stop_hour, config.timezone)
    return new_state(stop_at, now_epoch, "boot-default"), True


def evaluate_state(
    state: dict[str, object],
    now_epoch: int,
    notify_before_minutes: int,
) -> tuple[dict[str, object], str | None]:
    """Return updated state and one of: warn, stop, or None."""
    if state["status"] == "stopping":
        return state, None

    stop_at = int(state["stop_at"])
    if now_epoch >= stop_at:
        updated = copy.deepcopy(state)
        updated["status"] = "stopping"
        updated["updated_at"] = now_epoch
        updated["updated_by"] = "controller"
        return updated, "stop"

    warning_at = stop_at - notify_before_minutes * 60
    if now_epoch >= warning_at and state["warning_sent_for"] != stop_at:
        updated = copy.deepcopy(state)
        updated["warning_sent_for"] = stop_at
        updated["updated_at"] = now_epoch
        updated["updated_by"] = "controller"
        return updated, "warn"

    return state, None


def delay_state(
    state: dict[str, object], hours: int, now_epoch: int, updated_by: str
) -> dict[str, object]:
    if hours <= 0:
        raise PowerError("Delay must be a positive whole number of hours")
    if state["status"] == "stopping":
        raise PowerError("Shutdown has already started; the deadline cannot be changed")

    new_stop_at = int(state["stop_at"]) + hours * 3600
    if new_stop_at <= now_epoch:
        raise PowerError("The delayed deadline would still be in the past")

    updated = copy.deepcopy(state)
    updated["stop_at"] = new_stop_at
    updated["warning_sent_for"] = None
    updated["updated_at"] = now_epoch
    updated["updated_by"] = updated_by
    return updated


def set_stop_hour(
    state: dict[str, object],
    hour: int,
    now_epoch: int,
    config: Config,
    updated_by: str,
) -> dict[str, object]:
    if state["status"] == "stopping":
        raise PowerError("Shutdown has already started; the deadline cannot be changed")

    updated = copy.deepcopy(state)
    updated["stop_at"] = next_hour_epoch(now_epoch, hour, config.timezone)
    updated["warning_sent_for"] = None
    updated["status"] = "scheduled"
    updated["updated_at"] = now_epoch
    updated["updated_by"] = updated_by
    return updated


def reset_state(now_epoch: int, config: Config, updated_by: str) -> dict[str, object]:
    stop_at = next_hour_epoch(now_epoch, config.default_stop_hour, config.timezone)
    return new_state(stop_at, now_epoch, updated_by)


def validate_state(raw_state: object) -> dict[str, object]:
    if not isinstance(raw_state, dict):
        raise PowerError("State file must contain a JSON object")
    if raw_state.get("version") != STATE_VERSION:
        raise PowerError("Unsupported or missing state version")
    if not isinstance(raw_state.get("stop_at"), int):
        raise PowerError("State stop_at must be an integer epoch timestamp")
    if raw_state.get("warning_sent_for") is not None and not isinstance(
        raw_state.get("warning_sent_for"), int
    ):
        raise PowerError("State warning_sent_for must be null or an integer")
    if raw_state.get("status") not in VALID_STATUSES:
        raise PowerError("State status is invalid")
    return raw_state


def read_state(path: Path) -> dict[str, object] | None:
    try:
        with path.open("r", encoding="utf-8") as state_file:
            return validate_state(json.load(state_file))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise PowerError(f"State file is not valid JSON: {path}") from exc


def write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=".state.", text=True
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@contextmanager
def state_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PowerError(f"Unable to open state lock safely: {path}") from exc

    lock_stat = os.fstat(descriptor)
    if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid():
        os.close(descriptor)
        raise PowerError(f"State lock has unsafe ownership or type: {path}")

    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def format_deadline(stop_at: int, config: Config) -> str:
    return datetime.fromtimestamp(stop_at, config.timezone).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def format_remaining(seconds: int) -> str:
    if seconds <= 0:
        return "due now"
    minutes = (seconds + 59) // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def terminal_message(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"\n\a\033[1;31m[devbox-power] {title}\033[0m\n{body}\n\n"


def notify_terminals(config: Config, message: str) -> int:
    allowed_uids: set[int] = set()
    for user in config.terminal_users:
        try:
            allowed_uids.add(pwd.getpwnam(user).pw_uid)
        except KeyError as exc:
            raise PowerError(f"Terminal user does not exist: {user}") from exc

    notified = 0
    pts_directory = Path("/dev/pts")
    for terminal_path in sorted(pts_directory.iterdir(), key=lambda path: path.name):
        if not terminal_path.name.isdigit():
            continue
        try:
            terminal_stat = terminal_path.stat()
            if not stat.S_ISCHR(terminal_stat.st_mode):
                continue
            if terminal_stat.st_uid not in allowed_uids:
                continue
            descriptor = os.open(
                terminal_path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY
            )
            try:
                os.write(descriptor, message.encode("utf-8"))
                notified += 1
            finally:
                os.close(descriptor)
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return notified


def require_state(config: Config, now_epoch: int) -> dict[str, object]:
    state = read_state(config.state_file)
    if state is None:
        state, _ = initialize_state(None, now_epoch, config)
        write_state(config.state_file, state)
    return state


def command_init(config: Config, now_epoch: int) -> int:
    with state_lock(config.lock_file):
        state, changed = initialize_state(
            read_state(config.state_file), now_epoch, config
        )
        if changed:
            write_state(config.state_file, state)
    verb = "Initialized" if changed else "Preserved"
    print(f"{verb} shutdown deadline: {format_deadline(int(state['stop_at']), config)}")
    return 0


def command_status(config: Config, now_epoch: int) -> int:
    with state_lock(config.lock_file):
        state = read_state(config.state_file)
    if state is None:
        print("Power schedule is not initialized")
        return 1

    stop_at = int(state["stop_at"])
    print(f"Scheduled shutdown: {format_deadline(stop_at, config)}")
    print(f"Time remaining:     {format_remaining(stop_at - now_epoch)}")
    print(f"Status:             {state['status']}")
    print(f"Timezone:           {config.timezone_name}")
    print(f"Reminder:           {config.notify_before_minutes} minutes before")
    return 0


def command_delay(config: Config, now_epoch: int, hours: int) -> int:
    with state_lock(config.lock_file):
        state = require_state(config, now_epoch)
        updated = delay_state(state, hours, now_epoch, caller_identity())
        try:
            format_deadline(int(updated["stop_at"]), config)
        except (OverflowError, OSError, ValueError) as exc:
            raise PowerError(
                "The requested delay is outside the supported date range"
            ) from exc
        write_state(config.state_file, updated)
    print(f"Shutdown delayed to: {format_deadline(int(updated['stop_at']), config)}")
    return 0


def parse_stop_hour(value: str) -> int:
    match = re.fullmatch(r"(?:[01]?\d|2[0-3])(?::00)?", value)
    if not match:
        raise PowerError("stop-at accepts a whole local hour from 0 through 23")
    return int(value.split(":", 1)[0])


def command_stop_at(config: Config, now_epoch: int, hour: int) -> int:
    with state_lock(config.lock_file):
        state = require_state(config, now_epoch)
        updated = set_stop_hour(state, hour, now_epoch, config, caller_identity())
        write_state(config.state_file, updated)
    print(
        f"Shutdown rescheduled to: {format_deadline(int(updated['stop_at']), config)}"
    )
    return 0


def command_reset(config: Config, now_epoch: int) -> int:
    with state_lock(config.lock_file):
        state = read_state(config.state_file)
        if state is not None and state["status"] == "stopping":
            raise PowerError(
                "Shutdown has already started; the deadline cannot be reset"
            )
        updated = reset_state(now_epoch, config, caller_identity())
        write_state(config.state_file, updated)
    print(f"Shutdown reset to: {format_deadline(int(updated['stop_at']), config)}")
    return 0


def command_notify_test(config: Config) -> int:
    message = terminal_message(
        "test notification",
        [
            "Terminal notifications are configured correctly.",
            "No shutdown was scheduled or changed.",
        ],
    )
    count = notify_terminals(config, message)
    print(f"Notification written to {count} terminal(s)")
    return 0 if count else 1


def command_check(config: Config, now_epoch: int) -> int:
    warning_count: int | None = None
    with state_lock(config.lock_file):
        state = require_state(config, now_epoch)
        updated, action = evaluate_state(state, now_epoch, config.notify_before_minutes)
        if action == "warn":
            stop_at = int(updated["stop_at"])
            message = terminal_message(
                "scheduled shutdown approaching",
                [
                    f"This EC2 instance will shut down at {format_deadline(stop_at, config)}.",
                    "To delay it: devbox-power delay 1",
                    "To choose an hour: devbox-power stop-at 22",
                ],
            )
            warning_count = notify_terminals(config, message)
            if warning_count > 0:
                write_state(config.state_file, updated)
        elif updated is not state:
            write_state(config.state_file, updated)

    if action == "warn":
        print(f"Sent shutdown warning to {warning_count} terminal(s)")
        return 0

    if action == "stop":
        stop_at = int(updated["stop_at"])
        message = terminal_message(
            "shutting down now",
            [f"The scheduled deadline was {format_deadline(stop_at, config)}."],
        )
        count = notify_terminals(config, message)
        print(f"Sent final shutdown notice to {count} terminal(s)")
        result = subprocess.run(["/usr/bin/systemctl", "poweroff"], check=False)
        if result.returncode != 0:
            with state_lock(config.lock_file):
                current = read_state(config.state_file)
                if (
                    current is not None
                    and current["status"] == "stopping"
                    and current["stop_at"] == stop_at
                ):
                    current["status"] = "scheduled"
                    current["updated_at"] = int(time.time())
                    current["updated_by"] = "poweroff-failed"
                    write_state(config.state_file, current)
            raise PowerError(
                f"systemctl poweroff failed with exit code {result.returncode}"
            )
        return 0

    return 0


def caller_identity() -> str:
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"
    return f"cli:{user}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the local shutdown deadline for a cloud devbox"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="initialize the default deadline at boot")
    subparsers.add_parser("check", help="run one controller check")
    subparsers.add_parser("status", help="show the current shutdown deadline")

    delay_parser = subparsers.add_parser(
        "delay", help="delay the current deadline by whole hours"
    )
    delay_parser.add_argument("hours", type=int)

    stop_at_parser = subparsers.add_parser(
        "stop-at", help="set the deadline to the next occurrence of a whole hour"
    )
    stop_at_parser.add_argument("hour")

    subparsers.add_parser("reset", help="restore the next default shutdown hour")
    subparsers.add_parser(
        "notify-test", help="send a harmless test message to active terminals"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        now_epoch = int(time.time())
        if args.command == "init":
            return command_init(config, now_epoch)
        if args.command == "check":
            return command_check(config, now_epoch)
        if args.command == "status":
            return command_status(config, now_epoch)
        if args.command == "delay":
            return command_delay(config, now_epoch, args.hours)
        if args.command == "stop-at":
            return command_stop_at(config, now_epoch, parse_stop_hour(args.hour))
        if args.command == "reset":
            return command_reset(config, now_epoch)
        if args.command == "notify-test":
            return command_notify_test(config)
        parser.error(f"Unknown command: {args.command}")
    except PowerError as exc:
        print(f"devbox-power: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
