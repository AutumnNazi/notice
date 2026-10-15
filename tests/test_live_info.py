from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui.app as app_module
from core import live_info
from core.live_info import Weather


class WeatherServiceTests(unittest.TestCase):
    """core/live_info.py 天气拉取与数据校验的服务层测试。"""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.payload = {
            "current": {"temperature_2m": 0, "weather_code": 0, "time": self.now.timestamp()},
            "current_units": {"temperature_2m": "°C"},
        }

    def response(self, payload):
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def test_device_position_and_zero_temperature(self):
        position = subprocess.CompletedProcess([], 0, '{"latitude":0,"longitude":0}')
        with patch.object(live_info.subprocess, "run", return_value=position), \
             patch.object(live_info, "urlopen", return_value=self.response(self.payload)) as network:
            result = live_info.fetch_weather()
        self.assertEqual(result.display(self.now), "0℃  晴")
        self.assertEqual(result.source, "设备定位")
        self.assertIn("latitude=0.0", network.call_args.args[0].full_url)

    def test_ip_fallback_is_marked(self):
        position = {"success": True, "city": "Zhengzhou", "latitude": 34.7, "longitude": 113.6}
        with patch.object(live_info.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 12)), \
             patch.object(live_info, "urlopen", side_effect=[self.response(position), self.response(self.payload)]):
            result = live_info.fetch_weather()
        self.assertEqual((result.location, result.source), ("Zhengzhou", "IP 近似定位"))

    def test_rejects_invalid_weather_and_stale_values(self):
        position = subprocess.CompletedProcess([], 0, '{"latitude":34.7,"longitude":113.6}')
        for key, value in (("temperature_2m", None), ("temperature_2m", float("nan")),
                           ("weather_code", 999), ("time", self.now.timestamp() - 7200),
                           ("time", self.now.timestamp() + 7200)):
            with self.subTest(key=key, value=value):
                payload = {**self.payload, "current": {**self.payload["current"], key: value}}
                with patch.object(live_info.subprocess, "run", return_value=position), \
                     patch.object(live_info, "urlopen", return_value=self.response(payload)):
                    with self.assertRaises(ValueError):
                        live_info.fetch_weather()

    def test_old_data_does_not_remain_presented_as_current(self):
        weather = Weather("城市", "设备定位", 20, "晴", self.now)
        self.assertIn("已过期", weather.display(self.now + timedelta(hours=2)))

    def test_lunar_dates_using_windows_calendar(self):
        for day, expected in ((date(2026, 10, 14), ("九月初五", "丙午年  马年")),
                              (date(2026, 2, 17), ("正月初一", "丙午年  马年")),
                              (date(2025, 7, 25), ("闰六月初一", "乙巳年  蛇年"))):
            with self.subTest(day=day):
                self.assertEqual(live_info.lunar_date(day), expected)


class LiveInfoUITests(unittest.TestCase):
    """主界面实时信息（天气、农历、时钟）的回归测试。"""

    def setUp(self):
        for name in ("Notifier", "TaskScheduler", "TrayController", "register_font_directories",
                     "ensure_icon_assets", "stop_helper"):
            patcher = patch.object(app_module, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        store = patch.object(app_module, "TaskStore")
        mock_store = store.start().return_value
        self.addCleanup(store.stop)
        mock_store.load_tasks.return_value = []
        mock_store.load_settings.return_value = {}
        self.now = datetime(2026, 10, 14, 23, 59, 59)
        clock = patch.object(app_module, "datetime", wraps=datetime)
        clock.start().now.side_effect = lambda: self.now
        self.addCleanup(clock.stop)
        # 用确定的取数结果替代真实网络与 PowerShell，保证界面断言稳定。
        self.weather = Weather("测试城", "设备定位", 18.0, "多云", self.now.astimezone(timezone.utc))
        weather_source = patch.object(app_module, "fetch_weather", return_value=self.weather)
        weather_source.start()
        self.addCleanup(weather_source.stop)
        lunar_source = patch.object(app_module, "lunar_date", return_value=("九月初五", "丙午年  马年"))
        lunar_source.start()
        self.addCleanup(lunar_source.stop)
        self.app = app_module.NoticeApp()
        self.app.root.withdraw()
        self.addCleanup(self.destroy_window)

    def destroy_window(self):
        for timer in self.app.root.tk.call("after", "info"):
            self.app.root.after_cancel(timer)
        self.app.root.destroy()

    def wait_for(self, condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            self.app.root.update()
            time.sleep(0.01)
        return condition()

    def test_clock_updates_without_user_action_across_midnight(self):
        self.assertIn("2026.10.14", self.app.today_var.get())
        self.assertIn("23:59:59", self.app.today_var.get())
        self.now = datetime(2026, 10, 15, 0, 0, 1)
        self.assertTrue(self.wait_for(lambda: "00:00:01" in self.app.today_var.get()))
        self.assertIn("2026.10.15", self.app.today_var.get())
        self.assertIn("周四", self.app.today_var.get())
        self.app.search_var.set("no matching reminder")
        self.assertIn("2026.10.15", self.app.today_var.get())

    def test_weather_and_lunar_labels_show_live_data(self):
        self.assertTrue(self.wait_for(lambda: self.app.weather_var.get() == "18℃  多云"))
        self.assertEqual(self.app.lunar_var.get(), "九月初五")
        self.assertEqual(self.app.ganzhi_var.get(), "丙午年  马年")

    def test_weather_label_is_not_a_fixed_demo_temperature(self):
        self.assertTrue(self.wait_for(lambda: self.app.weather_var.get() == "18℃  多云"))
        labels = []

        def collect(widget):
            if isinstance(widget, app_module.tk.Label):
                labels.append(str(widget.cget("text")))
            for child in widget.winfo_children():
                collect(child)

        collect(self.app.root)
        self.assertFalse(any("25℃ 晴" in text for text in labels), labels)

    def test_exit_cancels_clock_and_weather_callbacks(self):
        self.assertTrue(self.wait_for(lambda: self.app.weather_var.get() == "18℃  多云"))
        self.app.exit_app()
        commands = [str(self.app.root.tk.call("after", "info", timer))
                    for timer in self.app.root.tk.call("after", "info")]
        self.assertFalse(any("_refresh_clock" in command for command in commands))
        self.assertFalse(any("_refresh_weather" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
