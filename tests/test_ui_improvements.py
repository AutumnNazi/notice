from __future__ import annotations

import contextlib
import io
import json
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import MagicMock, patch

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from core.models import Task
from core.live_info import Weather
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


class ThreadSafeRefreshTests(unittest.TestCase):
    """调度器 worker 线程不得直接碰 Tk：非主线程只投队列，主线程消费后再刷新。"""

    def setUp(self):
        self.app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        self.app.root = MagicMock()
        self.app._refresh_queue = queue.Queue()
        self.calls: list[str] = []
        self.app._persist_tasks = lambda: self.calls.append("persist")
        self.app._refresh_ui = lambda: self.calls.append("refresh")

    def test_worker_thread_does_not_touch_tk(self):
        worker = threading.Thread(target=self.app._schedule_refresh, name="scheduler-worker")
        worker.start()
        worker.join(5)

        # 后台线程里任何 Tk 调用都会和主线程的 Tcl 状态打架，这里必须一次都不发生。
        self.app.root.after.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.app._refresh_queue.empty())

    def test_main_thread_refreshes_immediately(self):
        self.app._schedule_refresh()

        self.app.root.after.assert_not_called()
        self.assertEqual(self.calls, ["persist", "refresh"])
        self.assertTrue(self.app._refresh_queue.empty(), "主线程不需要绕一圈队列")

    def test_drain_applies_pending_worker_refresh_exactly_once(self):
        for _ in range(3):
            worker = threading.Thread(target=self.app._schedule_refresh, name="scheduler-worker")
            worker.start()
            worker.join(5)

        self.app._drain_refresh_requests()
        self.assertEqual(self.calls, ["persist", "refresh"])

        self.app._drain_refresh_requests()
        self.assertEqual(self.calls, ["persist", "refresh"], "没有新请求时不得重复刷新")


class _RealAppTests(unittest.TestCase):
    """需要真实 Tk 主窗口的界面测试；调度器与托盘替身避免真的排程和起托盘线程。"""

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
        # 取数必须打桩：真实实现会拉起 PowerShell 子进程（12 秒超时）并访问网络，
        # 界面断言既会因此变慢，也会被环境波动带偏（与 test_live_info.py 同一做法）。
        self.now = datetime(2026, 10, 14, 23, 59, 59)
        weather_source = patch.object(
            app_module, "fetch_weather",
            return_value=Weather("测试城", "设备定位", 18.0, "多云", self.now.astimezone(timezone.utc)))
        weather_source.start()
        self.addCleanup(weather_source.stop)
        lunar_source = patch.object(app_module, "lunar_date", return_value=("九月初五", "丙午年  马年"))
        lunar_source.start()
        self.addCleanup(lunar_source.stop)
        self.app = app_module.NoticeApp()
        # 空状态与按钮文案要断言「真的显示出来了」，而 withdraw 的窗口下子控件一律未映射，
        # 所以按 test_live_info.py 的既有做法：先映射窗口，再断言可见性。
        self.app.root.deiconify()
        self.app.root.update()
        # 替身把 paused 暴露成 MagicMock，永远为真；界面测试需要显式状态才能断言分支。
        self.app.scheduler.paused = False
        self.addCleanup(self._destroy_window)

    def _destroy_window(self):
        # 先撤掉在跑的定时器，再关窗口：否则销毁后的 after 回调会继续触发。
        for timer in self.app.root.tk.call("after", "info"):
            self.app.root.after_cancel(timer)
        self.app.exit_app()


class EmptyListCopyTests(_RealAppTests):
    """列表清空时要区分“真没有提醒”和“只是被筛选/搜索挡掉了”。"""

    def _empty_text(self) -> str:
        self.assertTrue(self.app.empty_label.winfo_ismapped(), "空状态提示没有显示")
        return str(self.app.empty_label.cget("text"))

    def test_copy_invites_creation_only_when_there_are_no_tasks_at_all(self):
        self.assertEqual(self.app.tasks, [])
        self.app._refresh_ui()
        self.app.root.update()

        text = self._empty_text()
        self.assertIn("新建", text)
        self.assertNotIn("匹配", text)

    def test_copy_says_filtered_out_when_tasks_exist_but_are_hidden(self):
        self.app.tasks = [self._task()]
        self.app.search_var.set("绝不匹配的关键词")
        self.app._refresh_ui()
        self.app.root.update()

        text = self._empty_text()
        self.assertNotIn("新建", text, "有提醒只是被搜索挡掉时不能引导用户去新建")
        self.assertIn("匹配", text)
        self.assertIn("清除", text)

    def _task(self) -> Task:
        return Task(id="hidden", title="写周报", body="本周总结",
                    trigger_type="weekly", trigger_config={"weekdays": [0], "hour": 9})


class PauseButtonTests(_RealAppTests):
    """暂停按钮显示“点下去会发生什么”，与托盘措辞一致，且与点击行为相符。"""

    def setUp(self):
        super().setUp()
        self.pause_actions: list[str] = []
        self.app.scheduler = MagicMock()
        self.app.scheduler.paused = False
        self.app.scheduler.pause_all.side_effect = lambda: self.pause_actions.append("pause")
        self.app.scheduler.resume_all.side_effect = lambda: self.pause_actions.append("resume")

    def test_button_text_matches_the_action_it_triggers(self):
        for paused, expected, action in (
            (False, {"暂停", "提醒"}, "pause"),
            (True, {"恢复", "提醒"}, "resume"),
        ):
            with self.subTest(paused=paused):
                self.app.scheduler.paused = paused
                self.app._refresh_ui()
                self.app.root.update()

                label = str(self.app.pause_button.text)
                self.assertTrue(
                    all(part in label for part in expected),
                    f"按钮文案应当描述点击后会发生什么，实际是“{label}”",
                )
                # 文案与实际行为对不上就是本测试要挡住的问题。
                self.pause_actions.clear()
                self.app.pause_button.command()
                self.assertEqual(self.pause_actions, [action], f"按钮写着“{label}”，点下去却做了别的")


class _MenuOpeningTests(unittest.TestCase):
    """菜单相关测试的公共夹具：真实 Tk 根窗口 + 最小 NoticeApp 替身。

    tk_popup 是模态调用：它会抢走输入抓取，然后一直等一次真实点击才返回；
    实测 Escape、unpost、合成 Button 事件都无法让它返回，同步调用会永久卡住，
    而且同进程里存在过别的 Tk 解释器时会更容易卡死。这里把 tk_popup 换成只做记录
    的替身：菜单是否泄漏由「root.children 里的 Menu 数量」量测，这与菜单真的弹出
    与否无关；替身同时记下调用次数，用来证明被测代码确实走到了 tk_popup。
    """

    def setUp(self):
        self.app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        self.app.root = tk.Tk()
        self.app.root.withdraw()
        self.addCleanup(self.app.root.destroy)
        self.popup_calls: list[tuple[int, int]] = []
        patcher = patch.object(tk.Menu, "tk_popup", self._recording_popup)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app.root.winfo_pointerx = lambda: 12
        self.app.root.winfo_pointery = lambda: 34
        self.app.selected_task_id = None
        self.app.task_cards = {}
        self.app.filter_type = "all"
        self.app._selected_task = lambda: None
        self.app._set_filter_type = lambda _key: None
        # 卡片菜单分支要读触发描述与下次运行时间，替身省掉真实任务对象。
        self.app._describe_trigger = lambda _task: ""
        self.app._describe_next_run = lambda _task: ""
        self.widget = tk.Label(self.app.root, text="≡")
        self.widget.pack()
        self.addCleanup(self.widget.destroy)

    def _recording_popup(self, x=0, y=0, entry=""):
        # 挂在类上的是绑定方法，调用形式是 menu.tk_popup(x, y)，所以这里拿不到 menu 本身。
        self.popup_calls.append((x, y))
        return "break"

    def _menu_children(self) -> int:
        return sum(
            1
            for child in self.app.root.winfo_children()
            if child.winfo_class() == "Menu"
        )


class MenuLifetimeTests(_MenuOpeningTests):
    """任务菜单与筛选菜单反复打开不得线性泄漏 Tcl widget。"""

    # 替身不再有模态等待，重复次数可以给足：一旦实现退化成每次新建菜单，
    # 20 次会把 root.children 里的 Menu 从 1 个涨到 21 个，泄漏一眼可见。
    OPEN_TIMES = 20

    def _measure_repeated_opens(self, opener) -> tuple[int, int]:
        opener()
        baseline = self._menu_children()
        for _ in range(self.OPEN_TIMES):
            opener()
            self.app.root.update_idletasks()
        # 确认被测代码每次都真的走到了弹出菜单这一步，而不只是没建菜单。
        self.assertEqual(len(self.popup_calls), self.OPEN_TIMES + 1,
                         "菜单没有走到 tk_popup，泄漏断言就失去意义")
        return baseline, self._menu_children()

    def test_task_menu_does_not_accumulate_children(self):
        baseline, after = self._measure_repeated_opens(
            lambda: self.app._open_task_menu("task-1"))

        self.assertGreaterEqual(baseline, 1, "菜单没弹出来，说明测试没走到 tk_popup")
        self.assertEqual(after, baseline, "反复打开任务菜单后 Tcl widget 数量增长")

    def test_filter_menu_does_not_accumulate_children(self):
        baseline, after = self._measure_repeated_opens(
            lambda: self.app._open_filter_menu(self.widget))

        self.assertGreaterEqual(baseline, 1, "菜单没弹出来，说明测试没走到 tk_popup")
        self.assertEqual(after, baseline, "反复打开筛选菜单后 Tcl widget 数量增长")


class CoreRegressionTests(unittest.TestCase):
    def test_one_off_is_disabled_after_real_execution(self):
        notifier = MagicMock()
        finished = Event()
        scheduler = TaskScheduler(notifier)
        scheduler._scheduler.add_listener(lambda _event: finished.set(), EVENT_JOB_EXECUTED)
        scheduler.start()
        try:
            # 0.3 秒窗口在机器高负载时会让任务在注册阶段就被判为“已过期”直接禁用，
            # 于是断言以“配置过期”而不是“真实执行后禁用”的原因变红；放宽窗口只去掉这条竞态。
            when = datetime.now() + timedelta(seconds=2)
            task = Task(id="once", title="一次提醒", trigger_type="once",
                        trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
            scheduler.upsert_task(task)
            self.assertTrue(finished.wait(10))
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
                    # 同 test_one_off_is_disabled_after_real_execution：窗口太窄时任务还没排上就过期，
                    # 断言会以错误原因变红。
                    when = datetime.now() + timedelta(seconds=2)
                    scheduler.upsert_task(Task(
                        title="通知线程测试", trigger_type="once",
                        trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()},
                    ))
                    self.assertTrue(finished.wait(10), "调度任务没有执行")
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
