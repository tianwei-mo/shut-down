from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from shut_down import (
    Config,
    PowerError,
    build_parser,
    command_check,
    delay_state,
    evaluate_state,
    initialize_state,
    new_state,
    next_hour_epoch,
    parse_stop_hour,
    read_state,
    set_stop_hour,
    state_lock,
    terminal_message,
    write_state,
)

UTC = ZoneInfo("UTC")


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


def config() -> Config:
    return Config(
        timezone=UTC,
        timezone_name="UTC",
        default_stop_hour=19,
        notify_before_minutes=30,
        terminal_users=("ubuntu",),
    )


class DeadlineTests(unittest.TestCase):
    def test_next_hour_uses_today_when_future(self) -> None:
        now = epoch("2026-07-30T10:00:00+00:00")
        self.assertEqual(
            next_hour_epoch(now, 19, UTC),
            epoch("2026-07-30T19:00:00+00:00"),
        )

    def test_next_hour_rolls_to_tomorrow_when_passed(self) -> None:
        now = epoch("2026-07-30T20:00:00+00:00")
        self.assertEqual(
            next_hour_epoch(now, 19, UTC),
            epoch("2026-07-31T19:00:00+00:00"),
        )

    def test_boot_preserves_a_future_delayed_deadline(self) -> None:
        now = epoch("2026-07-30T12:00:00+00:00")
        existing = new_state(epoch("2026-07-30T22:00:00+00:00"), now, "test")
        initialized, changed = initialize_state(existing, now, config())
        self.assertFalse(changed)
        self.assertEqual(initialized["stop_at"], existing["stop_at"])

    def test_boot_replaces_an_expired_deadline(self) -> None:
        now = epoch("2026-07-31T09:30:00+00:00")
        existing = new_state(epoch("2026-07-30T19:00:00+00:00"), now - 1, "test")
        initialized, changed = initialize_state(existing, now, config())
        self.assertTrue(changed)
        self.assertEqual(initialized["stop_at"], epoch("2026-07-31T19:00:00+00:00"))


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stop_at = epoch("2026-07-30T19:00:00+00:00")
        self.state = new_state(self.stop_at, epoch("2026-07-30T09:30:00+00:00"), "test")

    def test_warning_is_emitted_once_for_a_deadline(self) -> None:
        warning_time = epoch("2026-07-30T18:30:00+00:00")
        warned, action = evaluate_state(self.state, warning_time, 30)
        self.assertEqual(action, "warn")
        self.assertEqual(warned["warning_sent_for"], self.stop_at)

        unchanged, second_action = evaluate_state(warned, warning_time + 60, 30)
        self.assertIsNone(second_action)
        self.assertEqual(unchanged, warned)

    def test_deadline_moves_state_to_stopping(self) -> None:
        stopped, action = evaluate_state(self.state, self.stop_at, 30)
        self.assertEqual(action, "stop")
        self.assertEqual(stopped["status"], "stopping")

    def test_delay_adds_hours_and_resets_warning(self) -> None:
        self.state["warning_sent_for"] = self.stop_at
        delayed = delay_state(
            self.state,
            2,
            epoch("2026-07-30T18:45:00+00:00"),
            "cli:test",
        )
        self.assertEqual(delayed["stop_at"], epoch("2026-07-30T21:00:00+00:00"))
        self.assertIsNone(delayed["warning_sent_for"])

    def test_delay_rejects_non_positive_values(self) -> None:
        with self.assertRaises(PowerError):
            delay_state(self.state, 0, epoch("2026-07-30T18:00:00+00:00"), "test")

    def test_delay_can_win_the_lock_at_the_existing_deadline(self) -> None:
        delayed = delay_state(self.state, 1, self.stop_at, "cli:test")
        self.assertEqual(delayed["stop_at"], epoch("2026-07-30T20:00:00+00:00"))

    def test_stop_at_uses_next_occurrence(self) -> None:
        updated = set_stop_hour(
            self.state,
            17,
            epoch("2026-07-30T18:00:00+00:00"),
            config(),
            "cli:test",
        )
        self.assertEqual(updated["stop_at"], epoch("2026-07-31T17:00:00+00:00"))


class ParsingTests(unittest.TestCase):
    def test_parse_stop_hour(self) -> None:
        self.assertEqual(parse_stop_hour("9"), 9)
        self.assertEqual(parse_stop_hour("09:00"), 9)
        self.assertEqual(parse_stop_hour("23"), 23)

    def test_parse_stop_hour_rejects_minutes(self) -> None:
        with self.assertRaises(PowerError):
            parse_stop_hour("19:30")

    def test_public_program_name_is_off(self) -> None:
        self.assertTrue(build_parser().format_usage().startswith("usage: off"))
        message = terminal_message("test", ["body"])
        self.assertIn("[off]", message)
        self.assertNotIn("[shut-down]", message)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.config = Config(
            timezone=UTC,
            timezone_name="UTC",
            default_stop_hour=19,
            notify_before_minutes=30,
            terminal_users=("ubuntu",),
            state_file=root / "state.json",
            lock_file=root / "state.lock",
        )
        self.stop_at = epoch("2026-07-30T19:00:00+00:00")
        write_state(
            self.config.state_file,
            new_state(
                self.stop_at,
                epoch("2026-07-30T09:30:00+00:00"),
                "test",
            ),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @patch("shut_down.notify_terminals", return_value=0)
    def test_warning_retries_when_no_terminal_received_it(self, _: Mock) -> None:
        with redirect_stdout(io.StringIO()):
            command_check(self.config, epoch("2026-07-30T18:30:00+00:00"))
        state = read_state(self.config.state_file)
        self.assertIsNotNone(state)
        self.assertIsNone(state["warning_sent_for"])

    @patch("shut_down.notify_terminals", return_value=2)
    def test_warning_is_recorded_after_terminal_delivery(self, _: Mock) -> None:
        with redirect_stdout(io.StringIO()):
            command_check(self.config, epoch("2026-07-30T18:30:00+00:00"))
        state = read_state(self.config.state_file)
        self.assertIsNotNone(state)
        self.assertEqual(state["warning_sent_for"], self.stop_at)

    @patch("shut_down.subprocess.run")
    @patch("shut_down.notify_terminals", return_value=1)
    def test_due_deadline_calls_poweroff(self, _: Mock, run_mock: Mock) -> None:
        run_mock.return_value = Mock(returncode=0)
        with redirect_stdout(io.StringIO()):
            command_check(self.config, self.stop_at)
        run_mock.assert_called_once_with(
            ["/usr/bin/systemctl", "poweroff"], check=False
        )
        state = read_state(self.config.state_file)
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "stopping")

    def test_lock_rejects_a_symbolic_link(self) -> None:
        target = self.config.lock_file.parent / "target"
        target.write_text("", encoding="utf-8")
        self.config.lock_file.unlink(missing_ok=True)
        self.config.lock_file.symlink_to(target)
        with self.assertRaises(PowerError), state_lock(self.config.lock_file):
            self.fail("Unsafe symbolic-link lock was accepted")


if __name__ == "__main__":
    unittest.main()
