import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import taskbar_monitor as monitor


class SingleInstanceTests(unittest.TestCase):
    def test_only_first_instance_acquires_named_mutex(self):
        name = "SysMonitor.Test.%s" % os.getpid()
        first = monitor.SingleInstance(name)
        second = monitor.SingleInstance(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            first.release()
            second.release()


class PresentMonParsingTests(unittest.TestCase):
    def test_csv_parser_preserves_commas_in_application_name(self):
        reader = monitor.FpsReader.__new__(monitor.FpsReader)
        reader.header = ["Application", "PresentTime"]
        reader.app_idx = 0
        row = '"C:\\Games\\Foo, Bar\\game.exe",123.4'
        self.assertEqual(reader._parse_line(row), 'C:\\Games\\Foo, Bar\\game.exe')


class SamplingTests(unittest.TestCase):
    def test_network_counter_reset_does_not_produce_negative_rates(self):
        down, up = monitor.network_rates(
            previous=(1000, 2000), current=(100, 100), elapsed=1.0
        )
        self.assertEqual((down, up), (0.0, 0.0))


class CleanupTests(unittest.TestCase):
    def test_close_quietly_does_not_prevent_other_cleanup(self):
        calls = []

        def failing_cleanup():
            calls.append("first")
            raise RuntimeError("cleanup failed")

        def successful_cleanup():
            calls.append("second")

        monitor.close_quietly(failing_cleanup)
        monitor.close_quietly(successful_cleanup)
        self.assertEqual(calls, ["first", "second"])


class LayoutTests(unittest.TestCase):
    def test_horizontal_taskbar_places_overlay_at_left_edge(self):
        taskbar = monitor.rect(0, 1000, 1920, 1040)
        notify = monitor.rect(1800, 1000, 1920, 1040)
        self.assertEqual(
            monitor.overlay_geometry(taskbar, notify, 300, 20),
            (8, 1003, 300, 34),
        )

    def test_vertical_taskbar_keeps_overlay_inside_taskbar_bounds(self):
        taskbar = monitor.rect(0, 0, 48, 1080)
        self.assertEqual(
            monitor.overlay_geometry(taskbar, taskbar, 300, 20),
            (3, 3, 42, 20),
        )

    def test_horizontal_taskbar_clips_overlay_before_notification_area(self):
        taskbar = monitor.rect(0, 1000, 500, 1040)
        notify = monitor.rect(450, 1000, 500, 1040)
        x, y, width, height = monitor.overlay_geometry(taskbar, notify, 500, 20)
        self.assertEqual((x, y, width, height), (8, 1003, 500, 34))


if __name__ == "__main__":
    unittest.main()
