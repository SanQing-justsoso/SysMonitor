import os
import sys
import unittest
from pathlib import Path
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from taskbar_monitor import DashboardRenderer


class DashboardRendererTests(unittest.TestCase):
    def setUp(self):
        self.renderer = DashboardRenderer()

    def test_render_without_gpu_and_fps(self):
        img, w, h = self.renderer.render(
            cpu_pct=18.5,
            cpu_temp=48.0,
            gpu_data=None,
            ram_pct=49.0,
            ram_used_bytes=22.9 * 1024**3,
            ram_total_bytes=47.2 * 1024**3,
            net_down=1.8 * 1024**2,
            net_up=240.0 * 1024,
            fps=None,
            target_height=36,
        )
        self.assertIsInstance(img, Image.Image)
        self.assertGreater(w, 200)
        self.assertEqual(h, 36)
        self.assertEqual(img.size, (w, h))

    def test_render_with_gpu_and_fps(self):
        gpu_data = {
            "util": 32,
            "temp": 52,
            "vram_used": 3.2 * 1024**3,
            "vram_total": 8.0 * 1024**3,
        }
        img, w, h = self.renderer.render(
            cpu_pct=88.0,  # High CPU, should trigger warning color
            cpu_temp=82.0,
            gpu_data=gpu_data,
            ram_pct=76.0,  # Moderate RAM
            ram_used_bytes=35.0 * 1024**3,
            ram_total_bytes=47.2 * 1024**3,
            net_down=5.4 * 1024**2,
            net_up=1.2 * 1024**2,
            fps=144,
            target_height=38,
        )
        self.assertIsInstance(img, Image.Image)
        self.assertGreater(w, 300)
        self.assertEqual(h, 38)

    def test_color_thresholds(self):
        c_low = self.renderer.get_metric_color(15.0)
        c_mid = self.renderer.get_metric_color(75.0)
        c_high = self.renderer.get_metric_color(92.0)
        self.assertNotEqual(c_low, c_mid)
    def test_gpu_placeholder_rendered_when_none(self):
        img, w, h = self.renderer.render(
            cpu_pct=5.0,
            cpu_temp=None,
            gpu_data=None,
            ram_pct=48.0,
            ram_used_bytes=22.8 * 1024**3,
            ram_total_bytes=47.2 * 1024**3,
            net_down=206.0,
            net_up=91.0,
            fps=None,
            target_height=36,
        )
        self.assertGreaterEqual(w, 350)
        self.assertEqual(h, 36)


if __name__ == "__main__":
    unittest.main()
