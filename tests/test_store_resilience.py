from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
import time
import unittest
from datetime import datetime
from ctypes import wintypes
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build
from config import SETTINGS_DEFAULTS
from core.models import Task
from store import TaskStore


@contextlib.contextmanager
def hold_exclusive(path: Path):
    """用共享模式 0 的独占句柄占住文件，模拟还在运行的旧实例锁着打包产物。

    实测在这种占用下 `open(path, "ab")` 抛 PermissionError，所以占用探测不必靠 mock 注入结果。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING = 0x80000000, 0x40000000, 3
    handle = kernel32.CreateFileW(
        str(path), GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


class TaskFileCorruptionTests(unittest.TestCase):
    """tasks.json 整体损坏（而非单条记录坏）时，启动要拿到空列表而不是抛异常。"""

    def load(self, raw: bytes) -> tuple[Path, list[Task], str]:
        """在临时目录写一份损坏文件，跑一次真实加载，返回（文件路径, 提醒, 日志文本）。"""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "tasks.json"
        path.write_bytes(raw)
        with self.assertLogs("store", level="WARNING") as diagnostics:
            tasks = TaskStore(path).load_tasks()
        return path, tasks, "\n".join(diagnostics.output)

    def assert_quarantined(self, path: Path, raw: bytes, stderr: str, reason: str) -> None:
        """损坏文件必须：原文一字不动、留一份备份、日志讲清坏的是哪个文件与为什么。"""
        self.assertEqual(path.read_bytes(), raw, "损坏的原文件不得改写，用户可能手工修复")
        backups = list(path.parent.glob(f"{path.name}.corrupt-*"))
        self.assertEqual(len(backups), 1, f"应留一份损坏备份，实际 {backups}")
        self.assertEqual(backups[0].read_bytes(), raw, "备份必须是损坏原文的副本")
        self.assertIn(str(path), stderr, "日志必须指出损坏的文件路径")
        self.assertIn(backups[0].name, stderr, "日志必须给出备份文件位置")
        self.assertIn("默认", stderr, "日志必须说明已回退到安全值")
        self.assertIn(reason, stderr, "日志必须说明具体原因")

    def test_invalid_json_returns_empty_list(self):
        raw = b'{"tasks": ['
        path, tasks, stderr = self.load(raw)
        self.assertEqual(tasks, [])
        self.assert_quarantined(path, raw, stderr, "JSON")

    def test_duplicate_json_keys_are_archived_instead_of_overriding_task_rules(self):
        raw = (b'{"tasks":[{"id":"duplicate-key","trigger_type":"interval",'
               b'"trigger_config":{"minutes":5,"minutes":1},"enabled":false,"enabled":true}]}')
        path, tasks, diagnostics = self.load(raw)
        self.assertEqual(tasks, [])
        self.assert_quarantined(path, raw, diagnostics, "重复")
        TaskStore(path).save_tasks(tasks)
        self.assertEqual([p.read_bytes() for p in path.parent.glob("tasks.json.corrupt-*")], [raw])

    def test_same_keys_in_separate_task_objects_remain_valid(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            records = [{"id": task_id, "title": "合法提醒", "trigger_type": "interval",
                        "trigger_config": {"minutes": minutes}, "enabled": enabled}
                       for task_id, minutes, enabled in (("first", 5, False), ("second", 1, True))]
            path.write_text(json.dumps({"tasks": records}, ensure_ascii=False), encoding="utf-8")
            store = TaskStore(path)
            tasks = store.load_tasks()
            self.assertEqual([(task.id, task.enabled, task.trigger_config["minutes"]) for task in tasks],
                             [("first", False, 5), ("second", True, 1)])
            store.save_tasks(tasks)
            self.assertEqual(list(path.parent.glob("*.corrupt-*")), [])

    def test_invalid_utf8_returns_empty_list_and_preserves_original(self):
        raw = b'{"tasks": [], "bad": "\xff"}'
        path, tasks, diagnostics = self.load(raw)
        self.assertEqual(tasks, [])
        self.assert_quarantined(path, raw, diagnostics, "UTF-8")

    def test_unsupported_json_numbers_are_quarantined_and_safe_to_save(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e999", "9" * 5000):
            with self.subTest(value=value[:20]):
                raw = ('{"tasks": [], "number": ' + value + '}').encode("ascii")
                path, tasks, diagnostics = self.load(raw)
                self.assertEqual(tasks, [])
                self.assert_quarantined(path, raw, diagnostics, "JSON")
                TaskStore(path).save_tasks(tasks)

    def test_duplicate_ids_preserve_both_records_without_schedule_collision(self):
        records = [{"id": "same", "title": title, "trigger_type": "interval",
                    "trigger_config": {"hours": 1}} for title in ("第一条", "第二条")]
        raw = json.dumps({"tasks": records}, ensure_ascii=False).encode("utf-8")
        path, tasks, diagnostics = self.load(raw)
        self.assertEqual([task.title for task in tasks], ["第一条", "第二条"])
        self.assertEqual(len({task.id for task in tasks}), 2)
        self.assertTrue(tasks[0].enabled)
        self.assertFalse(tasks[1].enabled)
        self.assertIn("重复", diagnostics)
        self.assertEqual(path.read_bytes(), raw)

    def test_reserved_or_empty_task_ids_are_reassigned_and_disabled(self):
        records = [{"id": value, "title": "保留记录", "trigger_type": "interval",
                    "trigger_config": {"hours": 1}} for value in ("", "snooze:other")]
        raw = json.dumps({"tasks": records}, ensure_ascii=False).encode("utf-8")
        _path, tasks, diagnostics = self.load(raw)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task.id and not task.id.startswith("snooze:") and not task.enabled for task in tasks))
        self.assertIn("不可用", diagnostics)

    def test_invalid_boolean_flags_are_disabled_and_reported(self):
        for value in ("false", "0", 1, None, [], {}):
            with self.subTest(value=value):
                record = {"id": "bad-flag", "trigger_type": "interval",
                          "trigger_config": {"minutes": 1}, "enabled": value}
                raw = json.dumps({"tasks": [record]}).encode("utf-8")
                _path, tasks, diagnostics = self.load(raw)
                self.assertFalse(tasks[0].enabled)
                self.assertIn("enabled", diagnostics)
                with self.assertRaises(ValueError):
                    Task.from_dict(record)

    def test_valid_boolean_flags_keep_their_original_meaning(self):
        for enabled in (True, False):
            for sound in (True, False):
                with self.subTest(enabled=enabled, sound=sound):
                    task = Task.from_dict({"trigger_type": "interval", "trigger_config": {"minutes": 1},
                                           "enabled": enabled, "sound": sound}, strict=False)
                    self.assertEqual((task.enabled, task.sound), (enabled, sound))
                    self.assertIsNone(task.load_warning)

    def test_invalid_sound_flag_is_reported_without_enabling_task(self):
        record = {"id": "bad-sound", "trigger_type": "interval", "trigger_config": {"minutes": 1}, "sound": "false"}
        _path, tasks, diagnostics = self.load(json.dumps({"tasks": [record]}).encode("utf-8"))
        self.assertFalse(tasks[0].enabled)
        self.assertFalse(tasks[0].sound)
        self.assertIn("sound", diagnostics)

    def test_invalid_calendar_records_are_archived_before_scheduler_changes(self):
        cases = [("yearly", {"month": 2, "day": 30}),
                 ("weekly", {"weekdays": "10", "hour": 9}),
                 ("weekly_last_workday", {"hour": 25})]
        for kind, config in cases:
            with self.subTest(kind=kind):
                record = {"id": "calendar-load", "trigger_type": kind, "trigger_config": config, "enabled": True}
                raw = json.dumps({"tasks": [record]}).encode("utf-8")
                path, tasks, diagnostics = self.load(raw)
                self.assertFalse(tasks[0].enabled)
                self.assertEqual(tasks[0].trigger_config, config)
                self.assertIsNotNone(tasks[0].load_warning)
                self.assertIn("calendar-load", diagnostics)
                self.assertEqual([p.read_bytes() for p in path.parent.glob("tasks.json.corrupt-*")], [raw])
                with self.assertRaises(ValueError):
                    Task.from_dict(record)

    def test_invalid_once_and_cron_records_are_disabled_and_archived(self):
        cases = [("once", {"date": "2026-02-30", "time": "09:00"}),
                 ("once", {}), ("once", {"date": 20261018, "time": "09:00"}),
                 ("cron", {"expression": "invalid cron"}), ("cron", {"expression": 7}),
                 ("cron", {"expression": None}), ("cron", {})]
        for kind, config in cases:
            with self.subTest(kind=kind, config=config):
                record = {"id": "bad-once-cron", "trigger_type": kind, "trigger_config": config}
                raw = json.dumps({"tasks": [record]}).encode("utf-8")
                path, tasks, diagnostics = self.load(raw)
                self.assertFalse(tasks[0].enabled)
                self.assertEqual(tasks[0].trigger_config, config)
                self.assertIsNotNone(tasks[0].load_warning)
                self.assertIn("bad-once-cron", diagnostics)
                self.assertEqual([p.read_bytes() for p in path.parent.glob("tasks.json.corrupt-*")], [raw])
                with self.assertRaises(ValueError):
                    Task.from_dict(record)

    def test_valid_once_and_cron_formats_roundtrip_without_corruption_backup(self):
        cases = [("once", {"date": "2020-01-01", "time": "09:00"}),
                 ("once", {"date": "2090-01-01", "time": "09:00:00+08:00"}),
                 ("cron", {"expression": "*/5 9-18 * * mon-fri"})]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            records = [{"id": str(index), "trigger_type": kind, "trigger_config": config}
                       for index, (kind, config) in enumerate(cases)]
            path.write_text(json.dumps({"tasks": records}), encoding="utf-8")
            store = TaskStore(path)
            tasks = store.load_tasks()
            self.assertTrue(all(task.enabled and task.load_warning is None for task in tasks))
            store.save_tasks(tasks)
            self.assertEqual([task.trigger_config for task in store.load_tasks()], [config for _, config in cases])
            self.assertEqual(list(path.parent.glob("*.corrupt-*")), [])

    def test_cron_with_no_possible_date_is_disabled_and_archived(self):
        for expression in ("0 9 30 2 *", "0 9 31 4 *"):
            with self.subTest(expression=expression):
                config = {"expression": expression}
                record = {"id": "impossible-cron", "title": "无解周期", "trigger_type": "cron",
                          "trigger_config": config, "enabled": True}
                raw = json.dumps({"tasks": [record]}, ensure_ascii=False).encode("utf-8")
                path, tasks, diagnostics = self.load(raw)
                self.assertFalse(tasks[0].enabled)
                self.assertEqual(tasks[0].trigger_config, config)
                self.assertIn("未来触发", diagnostics)
                self.assertEqual([p.read_bytes() for p in path.parent.glob("tasks.json.corrupt-*")], [raw])
                with self.assertRaisesRegex(ValueError, "未来触发"):
                    Task.from_dict(record)

    def test_valid_calendar_records_keep_enabled_state_and_do_not_create_backup(self):
        cases = [("yearly", {"month": "2", "day": "29", "hour": "9"}),
                 ("weekly", {"weekdays": [0, 6], "hour": 9}),
                 ("weekly_last_workday", {"hour": 18})]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            records = [{"id": kind, "trigger_type": kind, "trigger_config": config, "enabled": True}
                       for kind, config in cases]
            path.write_text(json.dumps({"tasks": records}), encoding="utf-8")
            tasks = TaskStore(path).load_tasks()
            self.assertTrue(all(task.enabled and task.load_warning is None for task in tasks))
            self.assertEqual(list(path.parent.glob("*.corrupt-*")), [])

    def test_repaired_calendar_record_can_be_enabled_and_saved_with_original_backup(self):
        record = {"id": "calendar-repair", "title": "生日", "trigger_type": "yearly",
                  "trigger_config": {"month": 2, "day": 30}, "enabled": True}
        raw = json.dumps({"tasks": [record]}, ensure_ascii=False).encode("utf-8")
        path, tasks, _diagnostics = self.load(raw)
        task = tasks[0]
        task.trigger_config = {"month": 2, "day": 29, "hour": 9}
        task.enabled = True
        task.validate()
        store = TaskStore(path)
        store.save_tasks(tasks)
        loaded = store.load_tasks()[0]
        self.assertTrue(loaded.enabled)
        self.assertIsNone(loaded.load_warning)
        self.assertEqual(loaded.trigger_config, task.trigger_config)
        self.assertEqual([p.read_bytes() for p in path.parent.glob("tasks.json.corrupt-*")], [raw])

    def test_non_object_top_level_returns_empty_list(self):
        raw = b'[{"id": "1"}]'
        path, tasks, stderr = self.load(raw)
        self.assertEqual(tasks, [])
        self.assert_quarantined(path, raw, stderr, "对象")

    def test_non_list_tasks_returns_empty_list(self):
        raw = b'{"tasks": {"id": "1"}}'
        path, tasks, stderr = self.load(raw)
        self.assertEqual(tasks, [])
        self.assert_quarantined(path, raw, stderr, "数组")

    def test_missing_tasks_key_returns_empty_list(self):
        raw = b'{"version": 2}'
        path, tasks, stderr = self.load(raw)
        self.assertEqual(tasks, [])
        self.assert_quarantined(path, raw, stderr, "缺少")

    def test_backup_failure_does_not_block_startup(self):
        """磁盘满或权限不足导致备份失败时仍要返回安全值，不能把启动拖崩。"""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "tasks.json"
        raw = b'{"tasks": ['
        path.write_bytes(raw)
        with patch("store.os.fsync", side_effect=OSError("磁盘已满")), \
             self.assertLogs("store", level="WARNING") as diagnostics:
            tasks = TaskStore(path).load_tasks()
        self.assertEqual(tasks, [])
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(list(path.parent.glob("tasks.json.corrupt-*")), [])
        self.assertIn("备份", "\n".join(diagnostics.output))

    def test_existing_identical_backup_is_not_duplicated(self):
        """同一份损坏内容只留一份备份：用户反复双击启动不该堆出一目录副本。"""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "tasks.json"
        raw = b'{"tasks": ['
        path.write_bytes(raw)
        earlier = path.with_name("tasks.json.corrupt-20200101-000000")
        earlier.write_bytes(raw)
        with self.assertLogs("store", level="WARNING") as diagnostics:
            self.assertEqual(TaskStore(path).load_tasks(), [])
        self.assertEqual(list(path.parent.glob("tasks.json.corrupt-*")), [earlier])
        self.assertIn(earlier.name, "\n".join(diagnostics.output))

    def test_different_corrupt_contents_in_same_second_keep_both_backups(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            contents = (b'{"first": ', b'{"second": ')
            with patch("store.datetime") as clock, self.assertLogs("store", level="WARNING"):
                clock.now.return_value = datetime(2026, 10, 18, 13, 10, 0)
                for raw in contents:
                    path.write_bytes(raw)
                    self.assertEqual(TaskStore(path).load_tasks(), [])
            backups = list(path.parent.glob("tasks.json.corrupt-*"))
            self.assertEqual(len(backups), 2, "同秒留档不能覆盖之前的损坏原文")
            self.assertEqual({backup.read_bytes() for backup in backups}, set(contents))
            self.assertEqual(path.read_bytes(), contents[-1])

    def test_existing_backup_survives_failed_new_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            old = path.with_name("tasks.json.corrupt-20261018-131000")
            old.write_bytes(b"previous original")
            raw = b'{"current": '
            path.write_bytes(raw)
            with patch("store.datetime") as clock, patch("store.os.fsync", side_effect=OSError("磁盘已满")), \
                 self.assertLogs("store", level="WARNING"):
                clock.now.return_value = datetime(2026, 10, 18, 13, 10, 0)
                self.assertEqual(TaskStore(path).load_tasks(), [])
            self.assertEqual(old.read_bytes(), b"previous original")
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(list(path.parent.glob("tasks.json.corrupt-*")), [old])

    def test_later_save_replaces_corrupt_file_and_keeps_backup(self):
        """读损坏之后的一次正常保存会覆盖原文件，备份是用户找回旧内容的唯一依据。"""
        raw = b'{"tasks": ['
        path, _, _ = self.load(raw)
        TaskStore(path).save_tasks([Task(title="喝水")])
        self.assertIn("喝水", path.read_text(encoding="utf-8"))
        backups = list(path.parent.glob("tasks.json.corrupt-*"))
        self.assertEqual([backup.read_bytes() for backup in backups], [raw])

    def test_skipped_record_is_backed_up_before_later_save(self):
        raw = json.dumps({"tasks": ["原始异常记录", {"id": "healthy", "trigger_type": "interval",
                                                   "trigger_config": {"minutes": 1}}]}, ensure_ascii=False).encode("utf-8")
        path, tasks, _diagnostics = self.load(raw)
        self.assertEqual(len(tasks), 1)
        TaskStore(path).save_tasks(tasks)
        self.assertEqual([p.read_bytes() for p in path.parent.glob("tasks.json.corrupt-*")], [raw])

    def test_failed_backup_blocks_overwrite_until_original_is_protected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            raw = b'{"tasks": ['
            path.write_bytes(raw)
            store = TaskStore(path)
            with patch("store.os.fsync", side_effect=OSError("磁盘已满")), self.assertLogs("store", level="WARNING"):
                tasks = store.load_tasks()
                with self.assertRaises(OSError):
                    store.save_tasks(tasks)
            self.assertEqual(path.read_bytes(), raw)
            with self.assertLogs("store", level="WARNING"):
                store.save_tasks(tasks)
            self.assertEqual([p.read_bytes() for p in path.parent.glob("tasks.json.corrupt-*")], [raw])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"tasks": []})


class SettingsFileCorruptionTests(unittest.TestCase):
    """settings.json 整体损坏时回退默认设置，不能让窗口化启动直接崩。"""

    def test_corrupt_settings_fall_back_to_defaults(self):
        cases = {
            "非法 JSON": b'{"font_family": ',
            "非法 UTF-8": b'{"font_family": "\xff"}',
            "重复关闭选项": b'{"close_action":"tray","close_action":"exit"}',
            "顶层是数组": b'["font_family"]',
            "顶层是字符串": b'"font_family"',
            "顶层是 null": b"null",
        }
        for scenario, raw in cases.items():
            with self.subTest(scenario=scenario):
                directory = TemporaryDirectory()
                self.addCleanup(directory.cleanup)
                path = Path(directory.name) / "settings.json"
                path.write_bytes(raw)
                with self.assertLogs("store", level="WARNING") as diagnostics:
                    settings = TaskStore(path).load_settings(path)
                self.assertEqual(settings, SETTINGS_DEFAULTS)
                self.assertIsNot(settings, SETTINGS_DEFAULTS, "必须返回副本，别让调用方改坏默认值")
                self.assertEqual(path.read_bytes(), raw, "损坏的原文件不得改写")
                backups = list(path.parent.glob("settings.json.corrupt-*"))
                self.assertEqual(len(backups), 1, f"应留一份损坏备份，实际 {backups}")
                self.assertEqual(backups[0].read_bytes(), raw)
                stderr_text = "\n".join(diagnostics.output)
                self.assertIn(str(path), stderr_text)
                self.assertIn(backups[0].name, stderr_text)

    def test_failed_settings_backup_blocks_default_save_until_protected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            raw = b'{"font_family": '
            path.write_bytes(raw)
            store = TaskStore(path)
            with patch("store.os.fsync", side_effect=OSError("磁盘已满")), self.assertLogs("store", level="WARNING"):
                settings = store.load_settings(path)
                with self.assertRaises(OSError):
                    store.save_settings(settings, path)
            self.assertEqual(path.read_bytes(), raw)
            with self.assertLogs("store", level="WARNING"):
                store.save_settings(settings, path)
            self.assertEqual([p.read_bytes() for p in path.parent.glob("settings.json.corrupt-*")], [raw])
            self.assertEqual(store.load_settings(path), SETTINGS_DEFAULTS)

    def test_healthy_files_do_not_create_corruption_backups(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            settings_path = Path(directory) / "settings.json"
            store = TaskStore(path)
            store.save_tasks([Task(trigger_type="interval", trigger_config={"minutes": 1})])
            store.save_settings(SETTINGS_DEFAULTS, settings_path)
            store.save_tasks(store.load_tasks())
            store.save_settings(store.load_settings(settings_path), settings_path)
            self.assertEqual(list(Path(directory).glob("*.corrupt-*")), [])

    def test_finite_json_numbers_roundtrip_but_nonfinite_settings_are_backed_up(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = TaskStore(path)
            values = {"number": 1.25, "negative": -2.5, "large": 1e100, "integer": 123}
            store.save_settings(values, path)
            loaded = store.load_settings(path)
            self.assertTrue(all(loaded[key] == value for key, value in values.items()))
            raw = b'{"number": NaN}'
            path.write_bytes(raw)
            with self.assertLogs("store", level="WARNING"):
                self.assertEqual(store.load_settings(path), SETTINGS_DEFAULTS)
            self.assertEqual([p.read_bytes() for p in path.parent.glob("settings.json.corrupt-*")], [raw])


class BuildLockProbeTests(unittest.TestCase):
    """打包前等旧实例释放产物：判据必须是真占用，不能拿「产物文件存在」顶替。"""

    def test_existing_but_unlocked_product_returns_at_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "时辰筏.exe"
            path.write_bytes(b"MZ")
            with patch("build.time.sleep") as sleep:
                locked = build._wait_until_unlocked([path])
            self.assertEqual(locked, [])
            sleep.assert_not_called()

    def test_missing_product_is_not_locked(self):
        with TemporaryDirectory() as directory:
            self.assertFalse(build._is_locked(Path(directory) / "没有这个文件.exe"))

    def test_stop_old_instances_matches_exact_executable_paths(self):
        with patch("build.subprocess.run") as run, patch("build._wait_until_unlocked", return_value=[]):
            build.kill_old()
        run.assert_called_once()
        script = run.call_args.args[0][-1]
        for path in (build.MAIN_EXE, build.WAKE_EXE, build.DIST / f"{build.EXE_NAME}.exe", build.DIST / "wake.exe"):
            self.assertIn(str(path.resolve()).replace("'", "''"), script)
        self.assertIn("$_.Path -in $targets", script)
        self.assertNotIn("ProcessName", script)
        self.assertNotIn("SilentlyContinue", script)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertGreater(run.call_args.kwargs["timeout"], 0)

    def test_stop_failure_prevents_cleanup_and_build(self):
        import subprocess

        with patch("build.subprocess.run", side_effect=subprocess.CalledProcessError(1, "powershell")), \
             patch("build._wait_until_unlocked") as wait, patch("build.clean") as clean, \
             patch("build.build_main") as main, patch("builtins.input", return_value=""):
            self.assertEqual(build.main(), 1)
        wait.assert_not_called()
        clean.assert_not_called()
        main.assert_not_called()

    def test_stop_old_instances_escapes_quote_in_workspace_path(self):
        root = Path("C:/test's workspace/dist")
        with patch("build.MAIN_EXE", root / "main.exe"), patch("build.WAKE_EXE", root / "wake.exe"), \
             patch("build.DIST", root), patch("build.subprocess.run") as run, \
             patch("build._wait_until_unlocked", return_value=[]):
            build.kill_old()
        self.assertIn("test''s workspace", run.call_args.args[0][-1])

    def test_stop_timeout_does_not_start_cleanup(self):
        import subprocess

        with patch("build.subprocess.run", side_effect=subprocess.TimeoutExpired("powershell", 6)), \
             patch("build.clean") as clean, patch("builtins.input", return_value=""):
            self.assertEqual(build.main(), 1)
        clean.assert_not_called()

    def test_single_use_path_iterator_is_checked_on_every_poll(self):
        path = Path("test-only.exe")
        with patch("build._is_locked", return_value=True) as locked, \
             patch("build.time.monotonic", side_effect=[0, 0, 7]), patch("build.time.sleep"):
            self.assertEqual(build._wait_until_unlocked(iter([path]), timeout=6), [path])
        self.assertEqual(locked.call_count, 2)

    def test_failed_main_build_stops_before_wake_and_data_copy(self):
        with patch("build.kill_old"), patch("build.clean"), patch("build.build_main", return_value=1), \
             patch("build.build_wake") as wake, patch("build.copy_data") as copy, \
             patch("build.verify") as verify, patch("builtins.input", return_value=""):
            self.assertEqual(build.main(), 1)
        wake.assert_not_called()
        copy.assert_not_called()
        verify.assert_not_called()

    def test_failed_wake_build_stops_before_data_copy(self):
        with patch("build.kill_old"), patch("build.clean"), patch("build.build_main", return_value=0), \
             patch("build.build_wake", return_value=2), patch("build.copy_data") as copy, \
             patch("build.verify") as verify, patch("builtins.input", return_value=""):
            self.assertEqual(build.main(), 1)
        copy.assert_not_called()
        verify.assert_not_called()

    def test_complete_build_releases_backup_only_after_verification(self):
        with TemporaryDirectory() as directory:
            backup = Path(directory) / "preserved"
            backup.mkdir()
            (backup / "tasks.json").write_bytes(b"original user data")
            with patch("build.PRESERVED_DATA", backup), patch("build.kill_old"), patch("build.clean"), \
                 patch("build.build_main", return_value=0), patch("build.build_wake", return_value=0), \
                 patch("build.copy_data"), patch("build.verify", side_effect=lambda: backup.exists()), \
                 patch("builtins.input", return_value=""):
                self.assertEqual(build.main(), 0)
            self.assertFalse(backup.exists())

    def test_incomplete_build_preserves_backup(self):
        with TemporaryDirectory() as directory:
            backup = Path(directory) / "preserved"
            backup.mkdir()
            original = backup / "tasks.json"
            original.write_bytes(b"original user data")
            with patch("build.PRESERVED_DATA", backup), patch("build.kill_old"), patch("build.clean"), \
                 patch("build.build_main", return_value=0), patch("build.build_wake", return_value=0), \
                 patch("build.copy_data"), patch("build.verify", return_value=False), \
                 patch("builtins.input", return_value=""):
                self.assertEqual(build.main(), 1)
            self.assertEqual(original.read_bytes(), b"original user data")

    def test_retry_preserve_keeps_existing_backup_and_current_data(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            current = output / "data"
            current.mkdir(parents=True)
            (current / "tasks.json").write_bytes(b"partially recovered current data")
            backup = Path(directory) / "preserved"
            backup.mkdir()
            (backup / "tasks.json").write_bytes(b"previous recoverable original")
            with patch("build.DIR_OUT", output), patch("build.PRESERVED_DATA", backup), \
                 patch("build.shutil.copytree") as copy:
                with self.assertRaisesRegex(OSError, "备份"):
                    build._preserve_dist_data()
            copy.assert_not_called()
            self.assertEqual((backup / "tasks.json").read_bytes(), b"previous recoverable original")
            self.assertEqual((current / "tasks.json").read_bytes(), b"partially recovered current data")

    def test_first_preserve_copies_data_and_missing_output_reuses_backup(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            current = output / "data"
            current.mkdir(parents=True)
            (current / "tasks.json").write_bytes(b"original reminders")
            backup = Path(directory) / "preserved"
            with patch("build.DIR_OUT", output), patch("build.PRESERVED_DATA", backup):
                build._preserve_dist_data()
            self.assertEqual((backup / "tasks.json").read_bytes(), b"original reminders")
            with patch("build.DIR_OUT", Path(directory) / "missing-output"), patch("build.PRESERVED_DATA", backup):
                build._preserve_dist_data()
            self.assertEqual((backup / "tasks.json").read_bytes(), b"original reminders")

    def test_clean_stops_before_deleting_output_when_backup_conflicts(self):
        with patch("build._preserve_dist_data", side_effect=OSError("存在旧备份")), \
             patch("build.shutil.rmtree") as remove:
            with self.assertRaises(OSError):
                build.clean()
        remove.assert_not_called()

    def test_build_exception_returns_failure_without_running_later_steps(self):
        with patch("build.kill_old", side_effect=OSError("无法停止旧程序")), patch("build.clean") as clean, \
             patch("build.build_main") as main, patch("builtins.input", return_value=""):
            self.assertEqual(build.main(), 1)
        clean.assert_not_called()
        main.assert_not_called()

    def test_missing_wake_product_is_not_reported_as_complete(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            executable = output / "main.exe"
            executable.write_bytes(b"MZ")
            (output / "data").mkdir()
            with patch("build.DIR_OUT", output), patch("build.MAIN_EXE", executable), \
                 patch("build.WAKE_EXE", output / "missing.exe"):
                self.assertFalse(build.verify())

    @unittest.skipIf(os.name != "nt", "独占句柄占用是 Windows 文件语义")
    def test_real_exclusive_handle_is_detected_and_released(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "时辰筏.exe"
            path.write_bytes(b"MZ")
            self.assertFalse(build._is_locked(path))
            with hold_exclusive(path):
                self.assertTrue(build._is_locked(path))
            self.assertFalse(build._is_locked(path), "句柄关闭后必须立刻判定为未占用")

    @unittest.skipIf(os.name != "nt", "独占句柄占用是 Windows 文件语义")
    def test_locked_product_is_retried_until_timeout(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "时辰筏.exe"
            path.write_bytes(b"MZ")
            with hold_exclusive(path):
                started = time.monotonic()
                with patch("build.time.sleep", wraps=time.sleep) as sleep:
                    locked = build._wait_until_unlocked([path], timeout=0.3, interval=0.05)
                elapsed = time.monotonic() - started
            self.assertEqual(locked, [path], "超时后要报出仍被占用的产物，供调用方提示")
            self.assertGreaterEqual(sleep.call_count, 1, "被占用时必须重试等待")
            self.assertGreaterEqual(elapsed, 0.3)
            self.assertLess(elapsed, 3.0, "等待必须有上限，不能无限等")


if __name__ == "__main__":
    unittest.main()
