from __future__ import annotations

import contextlib
import io
import json
import queue
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import MagicMock, patch

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from core.models import Task
from core.scheduler import TaskScheduler
import core.notifier as notifier_module
from store import TaskStore
import gui.app as app_module
from gui.theme import PALETTE, PillButton
from gui.task_dialog import TRIGGER_KEY_TO_LABEL, TaskDialog
from types import SimpleNamespace


class UiImprovementsTests(unittest.TestCase):
    def setUp(self):
        self.app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        self.app.filter_type = "all"
        self.app.search_var = MagicMock()
        self.app.search_var.get.return_value = ""

        self.t1 = Task(id="1", title="写周报", body="本周总结", trigger_type="weekly_last_workday", enabled=True)
        self.t2 = Task(id="2", title="喝水", body="健康提醒", trigger_type="interval", enabled=True)
        self.t3 = Task(id="3", title="会议", body="季度会议", trigger_type="once", enabled=False)
        self.t4 = Task(id="4", title="生日", body="纪念日", trigger_type="yearly", enabled=False)
        self.t5 = Task(id="5", title="定时备份", body="脚本触发", trigger_type="cron", enabled=True)

        self.app.tasks = [self.t1, self.t2, self.t3, self.t4, self.t5]

    def test_task_badge_text(self):
        self.assertEqual(self.app._task_badge_text(self.t1), "工作日")
        self.assertEqual(self.app._task_badge_text(self.t2), "固定间隔")
        self.assertEqual(self.app._task_badge_text(self.t3), "单次")
        self.assertEqual(self.app._task_badge_text(self.t4), "按年")
        self.assertEqual(self.app._task_badge_text(self.t5), "Cron")

    def test_visible_tasks_filter_all(self):
        self.app.filter_type = "all"
        visible = self.app._visible_tasks()
        self.assertEqual(len(visible), 5)

    def test_visible_tasks_filter_enabled(self):
        self.app.filter_type = "enabled"
        visible = self.app._visible_tasks()
        self.assertEqual([t.id for t in visible], ["1", "2", "5"])

    def test_visible_tasks_filter_disabled(self):
        self.app.filter_type = "disabled"
        visible = self.app._visible_tasks()
        self.assertEqual([t.id for t in visible], ["3", "4"])

    def test_visible_tasks_filter_recurring(self):
        self.app.filter_type = "recurring"
        visible = self.app._visible_tasks()
        self.assertEqual([t.id for t in visible], ["1", "2", "4", "5"])

    def test_visible_tasks_filter_once(self):
        self.app.filter_type = "once"
        visible = self.app._visible_tasks()
        self.assertEqual([t.id for t in visible], ["3"])

    def test_visible_tasks_filter_with_keyword(self):
        self.app.filter_type = "enabled"
        self.app.search_var.get.return_value = "周报"
        self.app._describe_trigger = MagicMock(return_value="")
        self.app._describe_next_run = MagicMock(return_value="")
        visible = self.app._visible_tasks()
        self.assertEqual([t.id for t in visible], ["1"])


class CoreRegressionTests(unittest.TestCase):
    def test_one_off_is_disabled_after_real_execution(self):
        notifier = MagicMock()
        finished = Event()
        scheduler = TaskScheduler(notifier)
        scheduler._scheduler.add_listener(lambda _event: finished.set(), EVENT_JOB_EXECUTED)
        scheduler.start()
        try:
            when = datetime.now() + timedelta(seconds=0.3)
            task = Task(id="once", title="一次提醒", trigger_type="once",
                        trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
            scheduler.upsert_task(task)
            self.assertTrue(finished.wait(3))
            notifier.send.assert_called_once_with(task)
            self.assertFalse(task.enabled)
            self.assertIsNone(task.next_run)
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_weekly_workday_skips_a_full_holiday_week(self):
        scheduler = TaskScheduler(MagicMock())
        with patch("core.scheduler.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 2, 14, 18, 0, 1)
            result = scheduler._next_weekly_last_workday(18, 0, 0)
        self.assertEqual(result, datetime(2026, 2, 28, 18))

    def test_weekly_workday_still_uses_longest_work_streak(self):
        scheduler = TaskScheduler(MagicMock())
        monday = datetime(2026, 10, 19)
        working = {monday.date() + timedelta(days=offset) for offset in (0, 1, 2, 4, 5)}
        with patch("core.scheduler.is_workday", side_effect=lambda day: day in working):
            self.assertEqual(scheduler._last_workday_in_week(monday, 18, 0, 0), datetime(2026, 10, 21, 18))

    def test_rejected_task_serialization_preserves_existing_json(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            before = b'{"tasks": []}'
            path.write_bytes(before)
            task = Task(trigger_config={"invalid": object()})
            with self.assertRaises(TypeError):
                TaskStore(path).save_tasks([task])
            self.assertEqual(path.read_bytes(), before)

    def test_edit_failure_in_job_store_keeps_original_task(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        try:
            old = Task(id="edit-store", trigger_type="weekly", trigger_config={"weekdays": [0], "hour": 9})
            scheduler.upsert_task(old)
            next_run = scheduler.task_next_run(old.id)
            jobstore = scheduler._scheduler._lookup_jobstore("default")
            with patch.object(jobstore, "update_job", side_effect=OSError("调度存储失败")):
                with self.assertRaises(OSError):
                    scheduler.upsert_task(Task(id=old.id, trigger_type="weekly",
                                               trigger_config={"weekdays": [1], "hour": 10}))
            self.assertIs(scheduler._tasks[old.id], old)
            self.assertEqual(scheduler.task_next_run(old.id), next_run)
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_invalid_edit_keeps_original_schedule(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        try:
            old = Task(id="edit", title="原提醒", trigger_type="weekly",
                       trigger_config={"weekdays": [0], "hour": 9})
            scheduler.upsert_task(old)
            next_run = scheduler.task_next_run(old.id)
            invalid = Task(id=old.id, title="错误修改", trigger_type="weekly",
                           trigger_config={"weekdays": [0], "hour": 25})
            with self.assertRaises(ValueError):
                scheduler.upsert_task(invalid)
            self.assertEqual(scheduler.task_next_run(old.id), next_run)
            self.assertIs(scheduler._tasks[old.id], old)
            valid = Task(id=old.id, title="正确修改", trigger_type="weekly",
                         trigger_config={"weekdays": [1], "hour": 10})
            scheduler.upsert_task(valid)
            self.assertIs(scheduler._tasks[old.id], valid)
            self.assertNotEqual(scheduler.task_next_run(old.id), next_run)
            valid.enabled = False
            scheduler.upsert_task(valid)
            self.assertIsNone(scheduler.task_next_run(old.id))
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_failed_atomic_save_keeps_tasks_and_settings(self):
        for operation in ("os.fsync", "pathlib.Path.replace"):
            for settings in (False, True):
                with self.subTest(operation=operation, settings=settings), TemporaryDirectory() as directory:
                    path = Path(directory) / "data.json"
                    before = b'{"tasks": [], "font_family": "old"}'
                    path.write_bytes(before)
                    store = TaskStore(path)
                    with patch(operation, side_effect=OSError("模拟磁盘或替换失败")):
                        with self.assertRaises(OSError):
                            if settings:
                                store.save_settings({"font_family": "new"}, path)
                            else:
                                store.save_tasks([Task(title="新提醒")])
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_save_preserves_utf8_bom_and_crlf(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_bytes(b'\xef\xbb\xbf{\r\n  "font_family": "old"\r\n}')
            store = TaskStore(path)
            store.save_settings({"font_family": "微软雅黑", "close_action": "tray"}, path)
            saved = path.read_bytes()
            self.assertTrue(saved.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", saved)
            self.assertEqual(store.load_settings(path)["font_family"], "微软雅黑")

    def test_invalid_intervals_are_rejected_by_ui_and_scheduler(self):
        fields = ("weeks", "days", "hours", "minutes", "seconds")
        for invalid in ({"minutes": -1}, {"hours": 1, "minutes": -1}, {}):
            with self.subTest(config=invalid):
                dialog = TaskDialog.__new__(TaskDialog)
                for key in fields:
                    value = str(invalid.get(key, 0))
                    setattr(dialog, f"interval_{key}_var", SimpleNamespace(get=lambda v=value: v))
                with self.assertRaises(ValueError):
                    dialog._collect_trigger_config("interval")
                task = Task(trigger_type="interval", trigger_config=invalid)
                with self.assertRaises(ValueError):
                    task.validate()
                with self.assertRaises(ValueError):
                    TaskScheduler(MagicMock())._build_trigger(task)

    def test_notification_sends_from_real_scheduler_worker(self):
        with TemporaryDirectory() as directory:
            notifier = notifier_module.Notifier.__new__(notifier_module.Notifier)
            finished, errors = Event(), []

            def completed(event):
                if event.exception:
                    errors.append(event.exception)
                finished.set()

            scheduler = TaskScheduler(notifier)
            scheduler._scheduler.add_listener(completed, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
            with patch.object(notifier_module, "START_MENU_DIR", Path(directory)), \
                 patch.object(notifier_module, "SHORTCUT_FILE", Path(directory) / "notice.lnk"), \
                 patch.object(notifier_module, "Notification") as toast:
                scheduler.start()
                try:
                    when = datetime.now() + timedelta(seconds=0.3)
                    scheduler.upsert_task(Task(
                        title="通知线程测试", trigger_type="once",
                        trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()},
                    ))
                    self.assertTrue(finished.wait(3), "调度任务没有执行")
                    self.assertEqual(errors, [])
                    toast.return_value.show.assert_called_once()
                finally:
                    scheduler._scheduler.shutdown(wait=True)

    def _load_records(self, records: list[object]) -> tuple[list[Task], str, list[object]]:
        """按启动时的真实路径读一份 tasks.json，并回放 stderr 中的降级报告。"""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "tasks.json"
        path.write_text(json.dumps({"tasks": records}, ensure_ascii=False), encoding="utf-8")
        saved = json.loads(path.read_text(encoding="utf-8"))["tasks"]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tasks = TaskStore(path).load_tasks()
        return tasks, stderr.getvalue(), saved

    def test_broken_interval_record_is_degraded_without_losing_healthy_ones(self):
        broken = {"id": "bad", "title": "坏间隔", "body": "", "trigger_type": "interval",
                  "trigger_config": {"minutes": -5}, "enabled": True, "next_run": "2026-10-15 09:00:00"}
        healthy = {"id": "good", "title": "喝水", "body": "该喝水了", "trigger_type": "weekly",
                   "trigger_config": {"weekdays": [0], "hour": 9}, "enabled": True}
        tasks, stderr, saved = self._load_records([broken, healthy])

        self.assertEqual([task.id for task in tasks], ["bad", "good"])
        degraded, intact = tasks
        self.assertFalse(degraded.enabled)
        self.assertIsNone(degraded.next_run)
        self.assertEqual(degraded.trigger_type, "interval")
        # 降级只是停用，配置必须与文件里的记录逐字节等价：不改写默认值、不做数值归一。
        self.assertEqual(json.dumps(degraded.trigger_config, ensure_ascii=False, sort_keys=True),
                         json.dumps(saved[0]["trigger_config"], ensure_ascii=False, sort_keys=True))
        self.assertEqual(degraded.trigger_config, {"minutes": -5})
        self.assertTrue(intact.enabled)
        self.assertEqual(intact.trigger_config, {"weekdays": [0], "hour": 9})
        self.assertEqual(intact.title, "喝水")
        # 降级不得静默：stderr 要能看出是哪一条、因为什么被降级。
        self.assertIn("bad", stderr)
        self.assertIn("循环间隔", stderr)

    def test_unknown_trigger_type_loads_into_a_form_the_editor_can_open(self):
        record = {"id": "weird", "title": "未知触发", "trigger_type": "every_full_moon",
                  "trigger_config": {"phase": "full"}, "enabled": True}
        tasks, stderr, _saved = self._load_records([record])

        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        # 编辑对话框用 TRIGGER_KEY_TO_LABEL[trigger_type] 查表，能查到的类型才打得开。
        self.assertIn(task.trigger_type, TRIGGER_KEY_TO_LABEL)
        self.assertFalse(task.enabled)
        self.assertIsNone(task.next_run)
        self.assertEqual(task.trigger_config, {"phase": "full"})
        self.assertIn("every_full_moon", stderr)

    def test_degraded_record_is_accepted_by_scheduler_but_rejected_when_enabled(self):
        broken = {"id": "bad", "title": "坏间隔", "trigger_type": "interval",
                  "trigger_config": {"minutes": -5}, "enabled": True}
        tasks, _stderr, _saved = self._load_records([broken])
        task = tasks[0]

        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        try:
            scheduler.upsert_task(task)
            self.assertIsNone(scheduler.task_next_run(task.id))
            self.assertIsNone(scheduler._scheduler.get_job(task.id))
            task.enabled = True
            with self.assertRaises(ValueError):
                scheduler.upsert_task(task)
            self.assertIsNone(scheduler._scheduler.get_job(task.id))
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_numeric_next_run_is_shown_without_raising(self):
        app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        app.scheduler = MagicMock()
        app.scheduler.paused = False
        task = Task(id="dirty-next-run", title="脏时间", trigger_type="once", next_run=20261015)

        text = app._describe_next_run(task)
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith("下次："))

    def test_clock_chain_is_rearmed_after_a_failed_refresh(self):
        app = MagicMock()
        app._display_day = None
        app._refresh_ui.side_effect = RuntimeError("卡片渲染失败")

        with self.assertRaises(RuntimeError):
            app_module.NoticeApp._refresh_clock(app)

        # 刷新体抛异常时必须已经重挂下一次刷新，否则天气、农历与跨日刷新永久停摆。
        self.assertEqual(app.root.after.call_count, 1)
        self.assertEqual(app.root.after.call_args.args[0], 1000)
        self.assertTrue(callable(app.root.after.call_args.args[1]))

    def test_unknown_live_info_message_is_not_decoded_as_lunar(self):
        app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        app._live_info_queue = queue.Queue()
        app._live_info_queue.put(("system-notice", {"text": "未来新增的消息类型"}))
        app._apply_weather = MagicMock()
        app._apply_lunar = MagicMock()

        app._drain_live_info()

        app._apply_weather.assert_not_called()
        app._apply_lunar.assert_not_called()

    def test_weekly_config_that_cannot_be_scheduled_is_disabled_at_startup(self):
        record = {"id": "weekly-bad", "title": "坏按周", "trigger_type": "weekly",
                  "trigger_config": {"weekdays": [0], "hour": 25}, "enabled": True}
        tasks, _stderr, _saved = self._load_records([record])

        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        try:
            scheduler.sync_tasks(tasks)
            self.assertFalse(tasks[0].enabled)
            self.assertIsNone(scheduler.task_next_run(tasks[0].id))
        finally:
            scheduler._scheduler.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
