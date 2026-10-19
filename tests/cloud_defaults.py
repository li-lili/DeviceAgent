"""Date defaults and CLI behavior for the persistent mock cloud."""
from datetime import datetime
from pathlib import Path
import sys
import queue
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mocks"))
import cloud as module
from common import cloud as pb

REAL_CLOUD = module.CloudSimulator


class FixedDate(datetime):
    day = 16

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, cls.day, 12, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(tz)


class DefaultsTest(unittest.TestCase):
    def test_day_boundaries_and_midnight_refresh(self):
        with patch.object(module, "datetime", FixedDate):
            for day in (16, 17):
                FixedDate.day = day
                command = REAL_CLOUD.make_command(None, "days")
                args = command.collect_logs
                self.assertEqual(args.selection_mode, pb.TIME_RANGE)
                self.assertEqual(args.time_to_ns - args.time_from_ns, 86400 * 10**9)
                start = datetime.fromtimestamp(args.time_from_ns / 10**9, ZoneInfo("Asia/Shanghai"))
                self.assertEqual((start.day, start.hour, start.minute), (day, 0, 0))
            FixedDate.day = 16

    def test_single_date_and_explicit_latest(self):
        command = REAL_CLOUD.make_command(None, "days", start="2026-10-15")
        self.assertEqual(command.collect_logs.time_to_ns - command.collect_logs.time_from_ns, 86400 * 10**9)
        self.assertEqual(REAL_CLOUD.make_command(None, "latest").collect_logs.selection_mode, pb.LATEST_FILE)
        with self.assertRaises(ValueError):
            REAL_CLOUD.make_command(None, "days", start="2026-10-16", end="2026-10-15")

    def test_cli_defaults_to_today_and_keeps_accepting_requests(self):
        class FakeCloud:
            commands = []

            def __init__(self, *args):
                self.ready = threading.Event()
                self.ready.set()
                self.handshakes = queue.Queue()
                self.handshakes.put("initial-handshake")
                self.verified = {}

            make_command = REAL_CLOUD.make_command

            def send(self, command):
                self.commands.append(command)

            def terminal(self, command_id, cancel=None):
                return pb.CommandResult(command_id=command_id, status=pb.CMD_SUCCEEDED)

            def close(self):
                pass

        commands = iter(["today business", "days", "quit"])
        def read_command(prompt):
            # Let the initial automatic collection complete before manual input.
            module.wait_for(lambda: len(FakeCloud.commands) >= 1, timeout=5)
            return next(commands)

        with patch.object(module, "CloudSimulator", FakeCloud), patch.object(module.time, "sleep"), patch.object(module, "datetime", FixedDate), patch.object(sys, "argv", ["cloud.py"]), patch("builtins.input", side_effect=read_command):
            module.main()
        self.assertEqual(len(FakeCloud.commands), 3)
        self.assertTrue(all(c.collect_logs.selection_mode == pb.TIME_RANGE for c in FakeCloud.commands))
        self.assertEqual(list(FakeCloud.commands[1].collect_logs.sources), ["business"])


if __name__ == "__main__":
    unittest.main()