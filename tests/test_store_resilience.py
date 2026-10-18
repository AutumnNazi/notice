from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
import time
import unittest
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

    def test_invalid_utf8_returns_empty_list_and_preserves_original(self):
        raw = b'{"tasks": [], "bad": "\xff"}'
        path, tasks, diagnostics = self.load(raw)
        self.assertEqual(tasks, [])
        self.assert_quarantined(path, raw, diagnostics, "UTF-8")

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
        with patch("store.shutil.copy2", side_effect=OSError("磁盘已满")), \
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

    def test_later_save_replaces_corrupt_file_and_keeps_backup(self):
        """读损坏之后的一次正常保存会覆盖原文件，备份是用户找回旧内容的唯一依据。"""
        raw = b'{"tasks": ['
        path, _, _ = self.load(raw)
        TaskStore(path).save_tasks([Task(title="喝水")])
        self.assertIn("喝水", path.read_text(encoding="utf-8"))
        backups = list(path.parent.glob("tasks.json.corrupt-*"))
        self.assertEqual([backup.read_bytes() for backup in backups], [raw])


class SettingsFileCorruptionTests(unittest.TestCase):
    """settings.json 整体损坏时回退默认设置，不能让窗口化启动直接崩。"""

    def test_corrupt_settings_fall_back_to_defaults(self):
        cases = {
            "非法 JSON": b'{"font_family": ',
            "非法 UTF-8": b'{"font_family": "\xff"}',
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
