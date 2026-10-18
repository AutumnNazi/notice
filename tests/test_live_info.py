from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gui.app as app_module
from core import live_info
from core.live_info import Weather
from core.models import Task
from gui.settings_dialog import SettingsDialog
from store import TaskStore


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

    def test_invalid_weather_units_have_explicit_validation_error(self):
        for units in (None, [], "C", {}, {"temperature_2m": "F"}):
            with self.subTest(units=units):
                payload = {**self.payload, "current_units": units}
                with patch.object(live_info, "_powershell_json", return_value={"latitude": 0, "longitude": 0}), \
                     patch.object(live_info, "_read_json", return_value=payload), \
                     self.assertLogs("core.live_info", level="WARNING") as diagnostics:
                    with self.assertRaisesRegex(ValueError, "温度单位"):
                        live_info.fetch_weather()
                self.assertIn("current_units", "\n".join(diagnostics.output))

    def test_invalid_lunar_fields_are_rejected_instead_of_truncated(self):
        valid = {"month": 1, "day": 1, "leap": 0, "cycle": 1}
        samples = [{**valid, "day": 1.9}, {**valid, "month": True}, {**valid, "cycle": True},
                   {**valid, "month": 13}, {**valid, "leap": 1}, {**valid, "day": None},
                   {"month": 1, "day": 1, "leap": 0}]
        for sample in samples:
            with self.subTest(sample=sample):
                with patch.object(live_info, "_powershell_json", return_value=sample), \
                     self.assertLogs("core.live_info", level="WARNING") as diagnostics:
                    with self.assertRaisesRegex(ValueError, "农历数据无效"):
                        live_info.lunar_date(date(2026, 10, 18))
                self.assertIn("2026-10-18", "\n".join(diagnostics.output))

    def test_valid_lunar_leap_month_boundaries_remain_displayable(self):
        samples = [({"month": 1, "day": 1, "leap": 0, "cycle": 1}, "正月初一"),
                   ({"month": 2, "day": 1, "leap": 2, "cycle": 1}, "闰正月初一"),
                   ({"month": 13, "day": 30, "leap": 13, "cycle": 60}, "闰腊月三十"),
                   ({"month": 13, "day": 20, "leap": 7, "cycle": 1}, "腊月二十")]
        for sample, expected in samples:
            with self.subTest(sample=sample), patch.object(live_info, "_powershell_json", return_value=sample):
                self.assertEqual(live_info.lunar_date(date(2026, 10, 18))[0], expected)

    def test_valid_negative_temperature_and_celsius_units_are_preserved(self):
        payload = {**self.payload, "current": {**self.payload["current"], "temperature_2m": -12.5}}
        with patch.object(live_info, "_powershell_json", return_value={"latitude": 0, "longitude": 0}), \
             patch.object(live_info, "_read_json", return_value=payload):
            weather = live_info.fetch_weather()
        self.assertEqual(weather.temperature, -12.5)
        self.assertEqual(weather.display(self.now), "-12.5℃  晴")

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

    def test_tk_callback_exception_is_logged_without_stopping_window(self):
        def fail():
            raise ValueError("按钮回调失败")

        with self.assertLogs("gui.app", level="ERROR") as diagnostics:
            self.app.root.after(0, fail)
            self.app.root.update()
        self.assertIn("按钮回调失败", "\n".join(diagnostics.output))
        self.assertTrue(self.app.root.winfo_exists())

    def test_weather_and_lunar_labels_show_live_data(self):
        self.assertTrue(self.wait_for(lambda: self.app.weather_var.get() == "18℃  多云"))
        self.assertEqual(self.app.lunar_var.get(), "九月初五")
        self.assertEqual(self.app.ganzhi_var.get(), "丙午年  马年")

    def test_weather_failure_remains_visible_after_clock_refresh(self):
        self.assertTrue(self.wait_for(lambda: self.app._weather is not None))
        self.app._live_info_queue.put(("weather", None))
        self.app._refresh_clock()
        self.assertIn("失败", self.app.weather_var.get())
        self.assertIn("18℃", self.app.weather_var.get())
        self.app._refresh_clock()
        self.assertIn("失败", self.app.weather_var.get())

    def test_weather_card_shows_city_without_technical_metadata(self):
        self.assertTrue(self.wait_for(lambda: self.app._weather is not None))
        self.app._apply_weather(Weather("测试城", "IP 近似定位", 18.0, "多云", self.weather.observed_at))
        self.app._refresh_clock()
        labels = "\n".join(str(widget.cget("text")) for widget in self.app.season_card.content.winfo_children())
        self.assertIn("测试城", labels)
        self.assertIn("18℃  多云", labels)
        self.assertEqual(self.app.weather_meta_var.get(), "测试城")
        for technical_text in ("近似定位", "设备定位", "Open-Meteo", "数据 10-14"):
            self.assertNotIn(technical_text, labels)

    def test_lunar_failure_retries_and_recovers_on_the_same_day(self):
        self.assertTrue(self.wait_for(lambda: self.app.lunar_var.get() == "九月初五"))
        self.app._lunar_day = None
        # lunar_date 是在后台取数线程里调的，而时钟链与本次手动刷新都可能同时发起取数，
        # 用 side_effect 固定「第一次失败、第二次成功」会变成两个线程抢序列的抛硬币：
        # 谁先拿到成功都不确定，序列耗尽还会让后一次取数抛 StopIteration 而一直停在失败态。
        # 这里改成加锁计数：无论哪个线程先调，首次必失败、其后必成功，语义不变但结果确定。
        calls_lock = threading.Lock()
        calls = 0

        def sometimes_failing_lunar(_day):
            nonlocal calls
            with calls_lock:
                first = calls == 0
                calls += 1
            if first:
                raise OSError("日历暂不可用")
            return ("九月初五", "丙午年  马年")

        with patch.object(app_module, "LUNAR_RETRY_SECONDS", 0.1), \
             patch.object(app_module, "lunar_date", side_effect=sometimes_failing_lunar):
            # 本用例针对「失败后当天重试并恢复」，不是调度守卫的重试节流；上一拍时钟可能刚
            # 发起过一次取数或刚写过退避时间，手动刷新会被守卫跳过而看不到失败态。这里显式
            # 复位守卫条件，让这次刷新必然发起取数。
            self.app._lunar_pending = None
            self.app._lunar_retry_at = 0.0
            self.app._refresh_clock()
            self.assertTrue(self.wait_for(lambda: "失败" in self.app.lunar_var.get()))
            self.assertTrue(self.wait_for(lambda: self.app.lunar_var.get() == "九月初五"))
            self.assertEqual(self.app.ganzhi_var.get(), "丙午年  马年")

    def test_previous_day_lunar_result_does_not_overwrite_current_date(self):
        self.assertTrue(self.wait_for(lambda: self.app.lunar_var.get() == "九月初五"))
        self.app._live_info_queue.put(("lunar", (date(2026, 10, 13), "九月初四", "旧年")))
        self.app._refresh_clock()
        self.assertEqual(self.app.lunar_var.get(), "九月初五")
        self.assertEqual(self.app.ganzhi_var.get(), "丙午年  马年")

    def test_stale_weather_and_failure_do_not_look_current(self):
        self.assertTrue(self.wait_for(lambda: self.app._weather is not None))
        self.now += timedelta(hours=2)
        self.app._live_info_queue.put(("weather", None))
        self.app._refresh_clock()
        self.assertIn("已过期", self.app.weather_var.get())
        self.assertIn("失败", self.app.weather_var.get())
        self.assertNotIn("18℃", self.app.weather_var.get())

    def test_long_body_keeps_next_run_visible(self):
        self.app.scheduler.paused = False
        self.app.tasks = [Task(id="long", title="工作汇报", body="项目进度、风险问题及下周安排需要整理后发给项目组。" * 20,
                              trigger_type="weekly", trigger_config={"weekdays": [0], "hour": 9},
                              next_run="2026-10-15 09:00:00")]
        self.app._refresh_ui()
        self.app.root.update()
        card = self.app.task_cards["long"].content
        next_labels = [widget for widget in card.winfo_children()
                       if isinstance(widget, app_module.tk.Label) and str(widget.cget("text")).startswith("下次：")]
        self.assertEqual(len(next_labels), 1)
        label = next_labels[0]
        self.assertGreaterEqual(label.winfo_height(), label.winfo_reqheight())
        self.assertLessEqual(label.winfo_y() + label.winfo_height(), card.winfo_height())

    def test_time_and_frequency_are_centered_as_one_group(self):
        self.app.scheduler.paused = False
        self.app.root.deiconify()
        for trigger, config in (("weekly", {"weekdays": [0], "hour": 9}),
                                ("interval", {"hours": 1, "minutes": 30})):
            for selected in (True, False):
                with self.subTest(trigger=trigger, selected=selected):
                    self.app.tasks = [Task(id="center", title="周报", trigger_type=trigger, trigger_config=config)]
                    self.app._refresh_ui()
                    self.app.selected_task_id = "center" if selected else None
                    for widget in self.app.scroll_frame.winfo_children():
                        widget.destroy()
                    self.app._render_task_card(self.app.tasks[0], 0)
                    self.app.root.update()
                    card = self.app.task_cards["center"].content
                    box = card.grid_slaves(row=0, column=0)[0]
                    labels = []

                    def collect(widget):
                        if isinstance(widget, app_module.tk.Label):
                            labels.append(widget)
                        for child in widget.winfo_children():
                            collect(child)

                    collect(box)
                    self.assertEqual(len(labels), 2)
                    top = min(label.winfo_rooty() for label in labels)
                    bottom = max(label.winfo_rooty() + label.winfo_height() for label in labels)
                    self.assertAlmostEqual((top + bottom) / 2, box.winfo_rooty() + box.winfo_height() / 2, delta=1)
                    for label in labels:
                        self.assertAlmostEqual(label.winfo_rootx() + label.winfo_width() / 2,
                                               box.winfo_rootx() + box.winfo_width() / 2, delta=1)

    def _open_font_dropdown(self, dialog):
        field = dialog.font_box.combobox
        self.app.root.update()
        field.event_generate("<Button-1>", x=field.winfo_width() - 5, y=field.winfo_height() // 2)
        field.event_generate("<ButtonRelease-1>", x=field.winfo_width() - 5, y=field.winfo_height() // 2)
        self.app.root.update()
        popup = str(field.tk.call("ttk::combobox::PopdownWindow", field))
        self.assertTrue(field.tk.call("winfo", "ismapped", popup))
        return field, popup, popup + ".f.l"

    def test_font_popup_single_click_selects_and_restores_modal_dialog(self):
        self.app.root.deiconify()
        dialog = SettingsDialog(self.app.root, font_choices=["Microsoft YaHei UI", "Arial", "SimSun"],
                                selected_font="Microsoft YaHei UI", autostart_enabled=False,
                                custom_font_dir="", close_action="tray")
        try:
            self.app.root.update()
            field, popup, listbox = self._open_font_dropdown(dialog)
            target = 1
            x, y, width, height = field.tk.call(listbox, "bbox", target)
            field.tk.call("event", "generate", listbox, "<Button-1>", "-x", x + 5, "-y", y + height // 2)
            field.tk.call("event", "generate", listbox, "<ButtonRelease-1>", "-x", x + 5, "-y", y + height // 2)
            self.app.root.update()
            self.assertEqual(dialog.font_family_var.get(), "Arial")
            self.assertFalse(field.tk.call("winfo", "ismapped", popup))
            self.assertIs(self.app.root.grab_current(), dialog)
            dialog._save()
            self.assertEqual(dialog.result["font_family"], "Arial")
        finally:
            if dialog.winfo_exists():
                dialog.destroy()

    def test_font_dropdown_has_no_extra_search_and_closes_on_escape(self):
        self.app.root.deiconify()
        dialog = SettingsDialog(self.app.root, font_choices=["Microsoft YaHei UI", "Arial", "SimSun"],
                                selected_font="Microsoft YaHei UI", autostart_enabled=False,
                                custom_font_dir="", close_action="tray")
        try:
            self.app.root.update()
            for _ in range(3):
                field, popup, listbox = self._open_font_dropdown(dialog)
                self.assertEqual(int(field.tk.call("winfo", "width", popup)), field.winfo_width())
                classes = []

                def collect(path):
                    classes.append(str(field.tk.call("winfo", "class", path)))
                    for child in field.tk.splitlist(field.tk.call("winfo", "children", path)):
                        collect(str(child))

                collect(popup)
                self.assertNotIn("Entry", classes)
                self.assertNotIn("TEntry", classes)
                field.tk.call("event", "generate", listbox, "<KeyPress-Escape>")
                self.app.root.update()
                self.assertFalse(field.tk.call("winfo", "ismapped", popup))
                self.assertTrue(dialog.winfo_exists())
                self.assertIs(self.app.root.grab_current(), dialog)
                self.assertEqual(dialog.font_family_var.get(), "Microsoft YaHei UI")
        finally:
            dialog.destroy()

    def test_font_dropdown_outside_click_closes(self):
        self.app.root.deiconify()
        dialog = SettingsDialog(self.app.root, font_choices=["Microsoft YaHei UI", "Arial", "SimSun"],
                                selected_font="Microsoft YaHei UI", autostart_enabled=False,
                                custom_font_dir="", close_action="tray")
        try:
            self.app.root.update()
            field, popup, listbox = self._open_font_dropdown(dialog)
            field.tk.call("event", "generate", popup, "<Button-1>", "-x", -10, "-y", -10)
            self.app.root.update()
            self.assertFalse(field.tk.call("winfo", "ismapped", popup), "点外部后下拉框仍打开")
            self.assertIs(self.app.root.grab_current(), dialog)
        finally:
            dialog.destroy()

    def test_font_arrow_uses_one_flat_field_and_still_opens(self):
        self.app.root.deiconify()
        dialog = SettingsDialog(self.app.root, font_choices=["Microsoft YaHei UI", "Arial"],
                                selected_font="Microsoft YaHei UI", autostart_enabled=False,
                                custom_font_dir="", close_action="tray")
        try:
            field = dialog.font_box.combobox
            style = app_module.ttk.Style(field)
            layout = style.layout(field.cget("style") or "TCombobox")
            self.assertEqual(len(layout), 1, "箭头不能使用独立的凸起按钮区域")
            self.assertEqual(layout[0][0], "Combobox.field")
            field, popup, listbox = self._open_font_dropdown(dialog)
            self.assertEqual(int(field.tk.call("winfo", "width", popup)), field.winfo_width())
            field.tk.call("event", "generate", listbox, "<KeyPress-Escape>")
            self.app.root.update()
            self.assertFalse(field.tk.call("winfo", "ismapped", popup))
        finally:
            dialog.destroy()

    def test_font_dropdown_closes_on_same_field_click_and_focus_loss(self):
        self.app.root.deiconify()
        dialog = SettingsDialog(self.app.root, font_choices=["Microsoft YaHei UI", "Arial", "SimSun"],
                                selected_font="Microsoft YaHei UI", autostart_enabled=False,
                                custom_font_dir="", close_action="tray")
        try:
            for reason in ("same_field", "focus_loss"):
                with self.subTest(reason=reason):
                    field, popup, listbox = self._open_font_dropdown(dialog)
                    if reason == "same_field":
                        x = field.winfo_rootx() - int(field.tk.call("winfo", "rootx", popup)) + field.winfo_width() // 2
                        y = field.winfo_rooty() - int(field.tk.call("winfo", "rooty", popup)) + field.winfo_height() // 2
                        field.tk.call("event", "generate", popup, "<Button-1>", "-x", x, "-y", y)
                    else:
                        field.tk.call("event", "generate", listbox, "<FocusOut>")
                    self.app.root.update()
                    self.assertFalse(field.tk.call("winfo", "ismapped", popup))
                    self.assertIs(self.app.root.grab_current(), dialog)
                    self.assertEqual(dialog.font_family_var.get(), "Microsoft YaHei UI")
        finally:
            dialog.destroy()

    def test_font_dropdown_does_not_block_settings_close_or_leave_grab(self):
        self.app.root.deiconify()
        errors = []
        self.app.root.report_callback_exception = lambda *_args: errors.append(str(_args[1]))
        for reason in ("close_button", "window_close"):
            with self.subTest(reason=reason):
                dialog = SettingsDialog(self.app.root, font_choices=["Microsoft YaHei UI", "Arial"],
                                        selected_font="Microsoft YaHei UI", autostart_enabled=False,
                                        custom_font_dir="", close_action="tray")
                field, popup, listbox = self._open_font_dropdown(dialog)
                if reason == "close_button":
                    # modal 下拉列表先收起；第二次点设置的关闭按钮完成关闭。
                    field.tk.call("event", "generate", popup, "<Button-1>", "-x", -10, "-y", -10)
                    self.app.root.update()
                    close_label = next(widget for widget in dialog.winfo_children()[0].winfo_children()[0].content.winfo_children()
                                       if isinstance(widget, app_module.tk.Label) and widget.cget("text") == "×")
                    close_label.event_generate("<Button-1>", x=3, y=3)
                else:
                    dialog.tk.call(dialog.protocol("WM_DELETE_WINDOW"))
                self.app.root.update()
                self.assertFalse(dialog.winfo_exists())
                self.assertFalse(self.app.root.tk.call("winfo", "exists", popup))
                self.assertIsNone(self.app.root.grab_current())
        self.assertEqual(errors, [])

    def test_font_values_update_without_extra_popup_widgets(self):
        self.app.root.deiconify()
        dialog = SettingsDialog(self.app.root, font_choices=["Microsoft YaHei UI", "Arial"],
                                selected_font="Microsoft YaHei UI", autostart_enabled=False,
                                custom_font_dir="", close_action="tray")
        try:
            dialog.font_box.set_values(["Microsoft YaHei UI", "Arial", "SimSun"])
            field, popup, listbox = self._open_font_dropdown(dialog)
            self.assertIn("SimSun", field.tk.call(listbox, "get", 0, "end"))
            field.tk.call("event", "generate", listbox, "<KeyPress-Escape>")
            self.app.root.update()
            self.assertEqual(len(dialog.font_box.winfo_children()), 1)
        finally:
            dialog.destroy()

    def test_selected_font_is_saved_and_applied_to_main_window(self):
        self.app.root.deiconify()
        callback_errors = []
        self.app.root.report_callback_exception = lambda *_args: callback_errors.append(str(_args[1]))
        with TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            real_store = TaskStore(Path(directory) / "tasks.json")
            self.app.store = SimpleNamespace(save_settings=lambda settings: real_store.save_settings(settings, settings_path))

            def choose_font():
                dialog = next(widget for widget in self.app.root.winfo_children() if isinstance(widget, SettingsDialog))
                field, popup, listbox = self._open_font_dropdown(dialog)
                values = list(field.tk.call(listbox, "get", 0, "end"))
                index = values.index("Arial")
                field.tk.call(listbox, "see", index)
                self.app.root.update()
                x, y, width, height = field.tk.call(listbox, "bbox", index)
                field.tk.call("event", "generate", listbox, "<Button-1>", "-x", x + 5, "-y", y + height // 2)
                field.tk.call("event", "generate", listbox, "<ButtonRelease-1>", "-x", x + 5, "-y", y + height // 2)
                dialog._save()

            self.app.root.after(100, choose_font)
            self.app.root.after(4000, lambda: [widget.destroy() for widget in self.app.root.winfo_children()
                                             if isinstance(widget, SettingsDialog)])
            self.app.open_settings()
            self.app.root.update()
            self.assertEqual(callback_errors, [])
            self.assertEqual(self.app.font_family_var.get(), "Arial")
            self.assertEqual(real_store.load_settings(settings_path)["font_family"], "Arial")
            actual = app_module.tkfont.Font(font=self.app.quote_label.cget("font")).actual("family")
            self.assertEqual(actual, "Arial")

    def test_next_run_changes_from_tomorrow_to_today_at_midnight(self):
        self.app.scheduler.paused = False
        self.app.tasks = [Task(id="midnight", title="晨会", trigger_type="weekly",
                              trigger_config={"weekdays": [0], "hour": 9}, next_run="2026-10-15 09:00:00")]
        with patch.object(app_module, "date", wraps=date) as calendar:
            calendar.today.side_effect = lambda: self.now.date()
            self.app._refresh_ui()
            self.app.root.update()
            self.now = datetime(2026, 10, 15, 0, 0, 1)
            self.assertTrue(self.wait_for(lambda: "00:00:01" in self.app.today_var.get()))
            labels = [widget.cget("text") for widget in self.app.task_cards["midnight"].content.winfo_children()
                      if isinstance(widget, app_module.tk.Label)]
            self.assertTrue(any("下次：今日 09:00" in str(text) for text in labels), labels)

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

    def test_window_fits_primary_work_area(self):
        # 混合 DPI 多显示器回归：窗口必须完整落在主屏工作区内，不被推出屏幕。
        self.app._primary_work_area = lambda: (1400, 900)
        self.app._configure_display_metrics()
        self.app.root.update()
        width = self.app.root.winfo_width()
        height = self.app.root.winfo_height()
        x, y = self.app.root.winfo_rootx(), self.app.root.winfo_rooty()
        self.assertLessEqual(width, 1400)
        self.assertLessEqual(height, 900)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, 1400)
        self.assertLessEqual(y + height, 900)

    def test_small_work_area_at_high_dpi_fits_window(self):
        self.app._primary_work_area = lambda: (1024, 728)
        self.app._detect_window_dpi = lambda: 144
        self.app._configure_display_metrics()
        self.app.root.deiconify()
        self.app.root.update()
        self.assertLessEqual(self.app.root.winfo_width(), 1024)
        self.assertLessEqual(self.app.root.winfo_height(), 728)
        self.assertLessEqual(self.app.root.winfo_rootx() + self.app.root.winfo_width(), 1024)
        self.assertLessEqual(self.app.root.winfo_rooty() + self.app.root.winfo_height(), 728)

    def test_quote_does_not_overlap_season_card(self):
        # 长名言 + 并排布局：名言右缘不得越过时间天气卡左缘（防遮挡回归）。
        self.app.quote_var.set("长风破浪会有时，直挂云帆济沧海。会当凌绝顶，一览众山小。")
        self.app.root.update()
        self.app.root.update_idletasks()
        quote_right = self.app.quote_label.winfo_rootx() + self.app.quote_label.winfo_width()
        season_left = self.app.season_card.winfo_rootx()
        self.assertLessEqual(quote_right, season_left, "名言右缘越过了时间天气卡左缘")

    def test_dpi_change_rebuilds_ui_at_new_scale(self):
        # 运行期 DPI 变化回归：显示缩放变化后按新 DPI 重建界面，内容与窗口不脱节。
        changed_dpi = 96 if self.app._last_dpi != 96 else 144
        old_card = self.app.season_card
        with patch.object(self.app, "_current_window_dpi", return_value=changed_dpi):
            self.app._on_root_configure(SimpleNamespace(widget=self.app.root))
        self.assertEqual(self.app.ui_scale, max(changed_dpi / 96.0, 1.0))
        self.assertEqual(self.app._last_dpi, changed_dpi)
        self.assertIsNot(self.app.season_card, old_card)
        self.assertIsNotNone(self.app.panel_content)

    def test_same_dpi_configure_does_not_rebuild(self):
        # DPI 没变时（如普通拖动改变窗口大小）不应触发重建。
        old_card = self.app.season_card
        with patch.object(self.app, "_current_window_dpi", return_value=self.app._last_dpi):
            self.app._on_root_configure(SimpleNamespace(widget=self.app.root))
        self.assertIs(self.app.season_card, old_card)

    def test_narrow_content_stacks_season_card_below_quote(self):
        # 内容区过窄时时间天气卡移到名言下方（堆叠），恢复宽度后回到并排。
        self.app._resize_quote_layout(SimpleNamespace(width=300))
        self.assertFalse(self.app._quote_side_by_side)
        self.assertEqual(self.app.season_card.grid_info()["row"], 1)
        self.assertEqual(self.app.home_divider.grid_info()["row"], 2)
        self.assertEqual(self.app.home_intro_content.grid_info()["row"], 3)
        self.app._resize_quote_layout(SimpleNamespace(width=2000))
        self.assertTrue(self.app._quote_side_by_side)
        self.assertEqual(self.app.season_card.grid_info()["row"], 0)
        self.assertEqual(self.app.home_divider.grid_info()["row"], 1)
        self.assertEqual(self.app.home_intro_content.grid_info()["row"], 2)


if __name__ == "__main__":
    unittest.main()
