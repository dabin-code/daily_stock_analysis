import unittest
from unittest.mock import patch

from src.scheduler import Scheduler


class _FakeJob:
    def __init__(self, registry):
        self.registry = registry
        self.time_value = None

    @property
    def day(self):
        return self

    def at(self, schedule_time):
        self.time_value = schedule_time
        return self

    def do(self, callback):
        self.registry.append({"time": self.time_value, "callback": callback})
        return self


class _FakeSchedule:
    def __init__(self):
        self.registered = []

    def every(self):
        return _FakeJob(self.registered)

    def run_pending(self):
        return None

    def get_jobs(self):
        return []


class SchedulerTestCase(unittest.TestCase):
    def test_add_daily_task_registers_multiple_jobs(self) -> None:
        fake_schedule = _FakeSchedule()

        with patch("src.scheduler.schedule", fake_schedule, create=True):
            scheduler = Scheduler(schedule_time="18:00")
            scheduler.schedule = fake_schedule

            scheduler.add_daily_task("analysis", lambda: None, schedule_time="18:00")
            scheduler.add_daily_task("board_sync", lambda: None, schedule_time="15:05")

        self.assertEqual(len(fake_schedule.registered), 2)
        self.assertEqual(fake_schedule.registered[0]["time"], "18:00")
        self.assertEqual(fake_schedule.registered[1]["time"], "15:05")

    def test_set_daily_task_remains_compatible_wrapper(self) -> None:
        fake_schedule = _FakeSchedule()

        with patch("src.scheduler.schedule", fake_schedule, create=True):
            scheduler = Scheduler(schedule_time="18:00")
            scheduler.schedule = fake_schedule

            scheduler.set_daily_task(lambda: None, run_immediately=False)

        self.assertEqual(len(fake_schedule.registered), 1)
        self.assertEqual(fake_schedule.registered[0]["time"], "18:00")

    def test_safe_run_named_task_reraises_callback_error(self) -> None:
        fake_schedule = _FakeSchedule()

        with patch("src.scheduler.schedule", fake_schedule, create=True):
            scheduler = Scheduler(schedule_time="18:00")
            scheduler.schedule = fake_schedule
            scheduler.add_daily_task("failing_task", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

            with self.assertRaisesRegex(RuntimeError, "boom"):
                scheduler._safe_run_named_task("failing_task")


if __name__ == "__main__":
    unittest.main()
