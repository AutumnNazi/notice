from __future__ import annotations

import contextlib
import io
import json
import logging
import logging.handlers
import re
import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from core import app_logging
from core.models import Task
from core.scheduler import TaskScheduler
from store import TaskStore


class AppLoggingTests(unittest.TestCase):
    """core/app_logging.py 的日志落地、轮转参数、幂等与降级行为测试。

    全部用临时目录，避免污染程序同级的真实 data/。
    """

    def setUp(self):
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.log_dir = Path(self._temp.name) / "logs"
        self._root_level = logging.getLogger().level
        self.addCleanup(self._release_root_logger)

    def _release_root_logger(self):
        """撤销本测试装的 handler 与级别，避免影响同进程内其它测试模块。"""
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler.get_name() in (app_logging.FILE_HANDLER_NAME, app_logging.FALLBACK_HANDLER_NAME):
                root.removeHandler(handler)
                handler.close()
        root.setLevel(self._root_level)

    def test_log_is_written_to_target_file_without_mojibake(self):
        app_logging.setup_logging(self.log_dir)
        app_logging.get_logger("core.live_info").warning("天气获取失败：设备定位不可用")

        content = (self.log_dir / app_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("天气获取失败：设备定位不可用", content)
        self.assertIn("[WARNING]", content)
        self.assertIn("core.live_info", content)
        match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", content)
        self.assertIsNotNone(match, content)
        # 时间戳来自本机本地时间（项目约定北京时间 UTC+8），未引入时区库
        logged_at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        self.assertLess(abs((datetime.now() - logged_at).total_seconds()), 60)

    def test_file_handler_rotates_with_agreed_limits(self):
        root = app_logging.setup_logging(self.log_dir)
        handlers = [h for h in root.handlers if h.get_name() == app_logging.FILE_HANDLER_NAME]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        self.assertIsInstance(handler, logging.handlers.RotatingFileHandler)
        self.assertEqual(handler.maxBytes, 1024 * 1024)
        self.assertEqual(handler.backupCount, 3)
        self.assertEqual(handler.encoding, "utf-8")
        self.assertEqual(Path(handler.baseFilename), self.log_dir / app_logging.LOG_FILE_NAME)

    def test_corrupt_file_diagnostic_reaches_log_without_stderr(self):
        path = Path(self._temp.name) / "tasks.json"
        path.write_bytes(b'{"tasks": [')
        app_logging.setup_logging(self.log_dir)
        with patch.object(sys, "stderr", None):
            self.assertEqual(TaskStore(path).load_tasks(), [])
        content = (self.log_dir / app_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn(str(path), content)
        self.assertIn("JSON", content)
        self.assertIn("corrupt-", content)

    def test_degraded_record_and_invalid_schedule_reach_log(self):
        path = Path(self._temp.name) / "tasks.json"
        path.write_text(json.dumps({"tasks": [{"id": "broken-interval", "trigger_type": "interval",
                                               "trigger_config": {"minutes": -1}}]}), encoding="utf-8")
        app_logging.setup_logging(self.log_dir)
        with patch.object(sys, "stderr", None):
            self.assertFalse(TaskStore(path).load_tasks()[0].enabled)
            scheduler = TaskScheduler(MagicMock())
            scheduler.start()
            try:
                scheduler.sync_tasks([Task(id="broken-weekly", title="坏星期配置", trigger_type="weekly",
                                           trigger_config={"weekdays": [0], "hour": 25})])
            finally:
                scheduler._scheduler.shutdown(wait=True)
        content = (self.log_dir / app_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("broken-interval", content)
        self.assertIn("循环间隔", content)
        self.assertIn("坏星期配置", content)
        self.assertIn("无法排程", content)

    def test_repeated_setup_keeps_one_handler_and_one_record(self):
        app_logging.setup_logging(self.log_dir)
        root = app_logging.setup_logging(self.log_dir)
        app_logging.setup_logging(self.log_dir)

        ours = [h for h in root.handlers if h.get_name() == app_logging.FILE_HANDLER_NAME]
        self.assertEqual(len(ours), 1, "重复调用不应重复添加 handler")

        app_logging.get_logger("core.live_info").warning("同一条失败只应写一次")
        lines = (self.log_dir / app_logging.LOG_FILE_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len([line for line in lines if "同一条失败只应写一次" in line]), 1)

    def test_default_dir_comes_from_config_at_call_time(self):
        with patch.object(config, "LOG_DIR", self.log_dir):
            app_logging.setup_logging()
        self.assertTrue((self.log_dir / app_logging.LOG_FILE_NAME).exists())

    def test_unwritable_dir_falls_back_to_stderr_without_raising(self):
        buffer = io.StringIO()
        with patch.object(app_logging.Path, "mkdir", side_effect=OSError("只读目录")), \
             contextlib.redirect_stderr(buffer):
            root = app_logging.setup_logging(self.log_dir)

            names = [handler.get_name() for handler in root.handlers]
            self.assertIn(app_logging.FALLBACK_HANDLER_NAME, names)
            self.assertNotIn(app_logging.FILE_HANDLER_NAME, names)
            self.assertFalse((self.log_dir / app_logging.LOG_FILE_NAME).exists())

        app_logging.get_logger("core.live_info").warning("降级后仍要能记录")
        self.assertIn("降级后仍要能记录", buffer.getvalue())

    def test_file_open_failure_falls_back_to_stderr(self):
        with patch.object(app_logging.logging.handlers, "RotatingFileHandler",
                          side_effect=OSError("没有写权限")):
            root = app_logging.setup_logging(self.log_dir)

        names = [handler.get_name() for handler in root.handlers]
        self.assertIn(app_logging.FALLBACK_HANDLER_NAME, names)
        self.assertNotIn(app_logging.FILE_HANDLER_NAME, names)

    def test_uncaught_exception_is_logged_once_and_chained(self):
        calls = []
        self.addCleanup(setattr, app_logging, "_original_thread_excepthook", getattr(app_logging, "_original_thread_excepthook", None))
        self.addCleanup(setattr, threading, "excepthook", threading.excepthook)
        self.addCleanup(setattr, app_logging, "_original_excepthook", None)
        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        app_logging._original_excepthook = None  # 重置包装状态，保证用例可重复执行
        sys.excepthook = lambda *args: calls.append(args)

        app_logging.install_excepthook()
        wrapper = sys.excepthook
        app_logging.install_excepthook()
        self.assertIs(sys.excepthook, wrapper, "重复调用不应叠加包装")

        with self.assertLogs(app_logging.UNCAUGHT_LOGGER_NAME, level="CRITICAL") as captured:
            try:
                raise ValueError("启动阶段崩溃")
            except ValueError:
                sys.excepthook(*sys.exc_info())

        self.assertIn("未捕获异常导致退出", captured.output[0])
        self.assertIn("启动阶段崩溃", captured.output[0])
        self.assertEqual(len(calls), 1, "原有 excepthook 必须仍被调用")
        self.assertIs(calls[0][0], ValueError)

    def test_uncaught_thread_exception_is_logged_once_and_chained(self):
        calls = []
        self.addCleanup(setattr, app_logging, "_original_excepthook", app_logging._original_excepthook)
        self.addCleanup(setattr, app_logging, "_original_thread_excepthook", getattr(app_logging, "_original_thread_excepthook", None))
        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        self.addCleanup(setattr, threading, "excepthook", threading.excepthook)
        app_logging._original_excepthook = None
        app_logging._original_thread_excepthook = None
        threading.excepthook = lambda args: calls.append(args.exc_type)
        app_logging.setup_logging(self.log_dir)
        app_logging.install_excepthook()
        wrapper = threading.excepthook
        app_logging.install_excepthook()
        self.assertIs(threading.excepthook, wrapper)

        def fail():
            raise RuntimeError("后台线程崩溃")

        thread = threading.Thread(target=fail, name="notice-test-worker")
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        content = (self.log_dir / app_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertEqual(content.count("后台线程异常"), 1)
        self.assertIn("notice-test-worker", content)
        self.assertIn("后台线程崩溃", content)
        self.assertEqual(calls, [RuntimeError])

        thread = threading.Thread(target=lambda: sys.exit(0), name="normal-exit-worker")
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        content = (self.log_dir / app_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertNotIn("normal-exit-worker", content)
        self.assertEqual(calls, [RuntimeError, SystemExit])

    def test_unreadable_font_directory_does_not_block_other_directories(self):
        import ctypes
        from core.font_manager import register_font_directories

        bad = Path(self._temp.name) / "bad-fonts"
        good = Path(self._temp.name) / "good-fonts"
        bad.mkdir()
        good.mkdir()
        font = good / "sample.ttf"
        font.write_bytes(b"test font fixture")
        original_glob = Path.glob

        def scan(path, pattern):
            if path == bad:
                raise PermissionError("字体目录不可读")
            return original_glob(path, pattern)

        windows = MagicMock()
        windows.gdi32.AddFontResourceExW.return_value = 1
        app_logging.setup_logging(self.log_dir)
        with patch.object(Path, "glob", scan), patch.object(ctypes, "windll", windows):
            self.assertEqual(register_font_directories([bad, good]), [font])
        content = (self.log_dir / app_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn(str(bad), content)
        self.assertIn("字体目录不可读", content)
        windows.gdi32.AddFontResourceExW.assert_called_once()

    def test_font_registration_failures_keep_healthy_font_and_log_details(self):
        import ctypes
        from core.font_manager import register_font_directory

        directory = Path(self._temp.name) / "fonts"
        directory.mkdir()
        files = [directory / name for name in ("a-rejected.ttf", "b-failed.ttf", "c-valid.otf")]
        for path in files:
            path.write_bytes(b"test font fixture")
        windows = MagicMock()
        windows.gdi32.AddFontResourceExW.side_effect = [0, OSError("字体 API 失败"), 1]
        windows.user32.SendMessageTimeoutW.side_effect = OSError("广播 API 失败")
        app_logging.setup_logging(self.log_dir)
        with patch.object(ctypes, "windll", windows):
            self.assertEqual(register_font_directory(directory), [files[2]])
        content = (self.log_dir / app_logging.LOG_FILE_NAME).read_text(encoding="utf-8")
        self.assertIn(str(files[0]), content)
        self.assertIn(str(files[1]), content)
        self.assertIn("字体 API 失败", content)
        self.assertIn("广播 API 失败", content)
        self.assertEqual(windows.gdi32.AddFontResourceExW.call_count, 3)
        windows.user32.SendMessageTimeoutW.assert_called_once()

    def test_missing_font_directory_has_no_api_calls_and_failed_broadcast_keeps_font(self):
        import ctypes
        from core.font_manager import register_font_directory

        directory = Path(self._temp.name) / "fonts"
        windows = MagicMock()
        with patch.object(ctypes, "windll", windows):
            self.assertEqual(register_font_directory(directory), [])
            windows.gdi32.AddFontResourceExW.assert_not_called()
            windows.user32.SendMessageTimeoutW.assert_not_called()
            directory.mkdir()
            path = directory / "valid.ttf"
            path.write_bytes(b"test font fixture")
            windows.gdi32.AddFontResourceExW.return_value = 1
            windows.user32.SendMessageTimeoutW.return_value = 0
            with self.assertLogs("core.font_manager", level="WARNING") as diagnostics:
                self.assertEqual(register_font_directory(directory), [path])
            self.assertIn("广播字体更新失败", "\n".join(diagnostics.output))

    def test_reimported_fonts_are_reused_but_remain_available_to_picker(self):
        import ctypes
        from core.font_manager import register_font_directories, register_font_directory

        directory = Path(self._temp.name) / "reused-fonts"
        directory.mkdir()
        path = directory / "sample.ttf"
        path.write_bytes(b"test font fixture")
        windows = MagicMock()
        windows.gdi32.AddFontResourceExW.return_value = 1
        with patch.object(ctypes, "windll", windows):
            self.assertEqual(register_font_directory(directory), [path])
            self.assertEqual(register_font_directory(directory / "."), [path])
            self.assertEqual(register_font_directories([directory, directory]), [path])
        windows.gdi32.AddFontResourceExW.assert_called_once()
        windows.user32.SendMessageTimeoutW.assert_called_once()

    def test_failed_font_registration_can_retry_and_new_files_are_loaded(self):
        import ctypes
        from core.font_manager import register_font_directory

        directory = Path(self._temp.name) / "retry-fonts"
        directory.mkdir()
        first = directory / "a.ttf"
        first.write_bytes(b"test font fixture")
        windows = MagicMock()
        windows.gdi32.AddFontResourceExW.side_effect = [0, 1, 1]
        with patch.object(ctypes, "windll", windows):
            with self.assertLogs("core.font_manager", level="WARNING"):
                self.assertEqual(register_font_directory(directory), [])
            self.assertEqual(register_font_directory(directory), [first])
            second = directory / "b.otf"
            second.write_bytes(b"new test font fixture")
            self.assertEqual(register_font_directory(directory), [first, second])
        self.assertEqual(windows.gdi32.AddFontResourceExW.call_count, 3)
        self.assertEqual(windows.user32.SendMessageTimeoutW.call_count, 2)

    def test_concurrent_font_import_does_not_register_same_path_twice(self):
        import ctypes
        from core.font_manager import register_font_directory

        directory = Path(self._temp.name) / "concurrent-fonts"
        directory.mkdir()
        font = directory / "sample.ttf"
        font.write_bytes(b"test font fixture")
        windows = MagicMock()
        windows.gdi32.AddFontResourceExW.return_value = 1
        results, errors = [], []
        barrier = threading.Barrier(3)

        def load():
            try:
                barrier.wait(timeout=3)
                results.append(register_font_directory(directory))
            except Exception as exc:
                errors.append(exc)

        with patch.object(ctypes, "windll", windows):
            workers = [threading.Thread(target=load) for _ in range(3)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(3)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(results, [[font]] * 3)
        windows.gdi32.AddFontResourceExW.assert_called_once()
        windows.user32.SendMessageTimeoutW.assert_called_once()


if __name__ == "__main__":
    unittest.main()
