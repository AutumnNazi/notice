from __future__ import annotations

import gc
import io
import json
import logging
import queue
import subprocess
import threading
import time
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
from core.scheduler import DEFAULT_SNOOZE_MINUTES, SNOOZE_JOB_PREFIX, TaskScheduler
import core.notifier as notifier_module
from store import TaskStore
import gui.app as app_module
from gui.theme import PALETTE, PillButton, SlimScrollbar, elide_text, scale
from gui.task_dialog import TRIGGER_KEY_TO_LABEL, TaskDialog
from types import SimpleNamespace


def _reclaim_idle_tk_interpreters() -> None:
    """在主线程里回收前面用例遗留的 Tk 解释器。

    启动真实调度器线程的用例必须先做这一步：进程里先前留下的 Tk 解释器如果还没被回收，
    就可能在线程池的工作线程里被回收，而 Tcl 不允许在非创建线程删除解释器，
    会直接 abort（Tcl_AsyncDelete: async handler deleted by the wrong thread），
    整套测试随之崩掉且看不出是哪个用例失败。
    """
    gc.collect()


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

    def test_failed_background_save_refreshes_ui_and_retries(self):
        self.app._persist_tasks = MagicMock(side_effect=[OSError("磁盘已满"), None])
        self.app._refresh_queue.put(True)
        with self.assertLogs("gui.app", level="ERROR") as diagnostics:
            self.app._drain_refresh_requests()
        self.assertIn("磁盘已满", "\n".join(diagnostics.output))
        self.assertEqual(self.calls, ["refresh"])
        self.assertFalse(self.app._refresh_queue.empty())
        self.app._drain_refresh_requests()
        self.assertEqual(self.app._persist_tasks.call_count, 2)
        self.assertEqual(self.calls, ["refresh", "refresh"])
        self.assertTrue(self.app._refresh_queue.empty())

    def test_persistent_background_save_failure_does_not_flood_logs(self):
        self.app._persist_tasks = MagicMock(side_effect=[OSError("磁盘已满"), OSError("磁盘已满"), None])
        self.app._refresh_queue.put(True)
        with self.assertLogs("gui.app", level="ERROR") as diagnostics:
            self.app._drain_refresh_requests()
            self.app._drain_refresh_requests()
        self.assertEqual(len(diagnostics.output), 1)
        self.assertEqual(self.calls, ["refresh", "refresh"])
        self.app._drain_refresh_requests()
        self.assertFalse(self.app._tasks_save_failed)
        self.assertTrue(self.app._refresh_queue.empty())


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

    def test_button_wide_enough_for_its_longest_label(self):
        """按钮宽度必须放得下文案：动作语义的文案比状态语义长，曾被右边缘切掉。"""
        import tkinter.font as tkfont

        from gui.theme import font as make_font

        for paused in (False, True):
            with self.subTest(paused=paused):
                self.app.scheduler.paused = paused
                self.app._refresh_ui()
                self.app.root.update()

                button = self.app.pause_button
                label = str(button.text)
                metrics = tkfont.Font(root=button, font=make_font(button.family, 10, button.ui_scale))
                text_w = metrics.measure(label)
                dot_r = max(2, int(round(3 * button.ui_scale)))
                gap = max(3, int(round(6 * button.ui_scale)))
                needed = dot_r * 2 + gap + text_w
                available = max(1, button.winfo_width() or int(button["width"]))

                self.assertGreaterEqual(
                    available, needed,
                    f"“{label}”需要 {needed}px，按钮只有 {available}px，文字会被切掉",
                )


class PillButtonSizingTests(unittest.TestCase):
    """自绘按钮的宽度以声明值为下限，文案更长时按内容撑开。"""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def build(self, text: str, width: int, *, dot: bool = False) -> PillButton:
        button = PillButton(
            self.root, text, lambda: None,
            width=width, height=42, family="Microsoft YaHei", ui_scale=1.0,
            radius=14, dot_color="#10b981" if dot else None,
        )
        self.addCleanup(button.destroy)
        return button

    def test_long_label_is_not_clipped(self):
        button = self.build("暂停全部提醒", 88, dot=True)

        self.assertGreater(int(button["width"]), 88, "长文案应当把按钮撑开，而不是被切掉")

    def test_short_label_keeps_declared_width(self):
        """对话框里的短文案按钮宽度是有意设计的，不能被这套逻辑改掉。"""
        for label in ("取消", "保存", "知道了"):
            with self.subTest(label=label):
                button = self.build(label, 112)

                self.assertEqual(int(button["width"]), 112, f"“{label}”不该改变声明宽度")

    def test_set_text_refits_when_label_grows(self):
        button = self.build("取消", 92)
        baseline = int(button["width"])
        button.set_text("打开数据目录")

        self.assertEqual(baseline, 92)
        self.assertGreater(int(button["width"]), 92, "换成长文案后必须重新贴合宽度")


class SlimScrollbarTests(unittest.TestCase):
    """自绘滚动条：滑块要跟着视图走，并且真的能拖动列表。"""

    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("60x200+80+80")
        # 滑块几何依赖真实高度，窗口必须先映射出来。
        self.root.deiconify()
        self.addCleanup(self.root.destroy)
        self.calls: list[tuple] = []
        self.bar = SlimScrollbar(self.root, command=lambda *args: self.calls.append(args), ui_scale=1.0)
        self.bar.pack(fill="y", expand=True)
        self.root.update()
        self.height = self.bar.winfo_height()

    def _thumb(self) -> tuple[int, int, int, int]:
        items = self.bar.find_withtag("thumb")
        self.assertTrue(items, "画布上没有滑块")
        return self.bar.bbox(items[0])

    def test_reported_range_becomes_the_thumb_position(self):
        self.bar.set(0.0, 0.5)
        top_half = self._thumb()
        self.bar.set(0.5, 1.0)
        bottom_half = self._thumb()

        self.assertGreater(top_half[1], -2)
        self.assertLess(top_half[3], self.height // 2 + 4, "上半段的范围没有画在上半段")
        self.assertGreater(bottom_half[1], self.height // 2 - 4, "下半段的范围没有画在下半段")

    def test_get_returns_what_set_received(self):
        self.bar.set(0.25, 0.75)

        self.assertEqual(self.bar.get(), (0.25, 0.75))

    def test_dragging_the_thumb_asks_the_view_to_move(self):
        self.bar.set(0.0, 0.5)
        box = self._thumb()

        self.bar._press(SimpleNamespace(y=(box[1] + box[3]) // 2))
        self.assertTrue(self.bar._dragging)
        self.bar._drag(SimpleNamespace(y=0))
        self.bar._release(SimpleNamespace(y=0))

        self.assertTrue(self.calls, "拖动滑块没有通知视图滚动")
        self.assertEqual(self.calls[-1], ("moveto", 0.0))

    def test_pressing_below_the_thumb_jumps_there_then_keeps_dragging(self):
        self.bar.set(0.0, 0.5)

        self.bar._press(SimpleNamespace(y=int(self.height * 0.9)))
        self.bar._drag(SimpleNamespace(y=int(self.height * 0.9)))

        self.assertTrue(self.calls, "点在滑块之外没有把视图带过去")
        self.assertGreater(self.calls[-1][1], 0.0)

    def test_very_long_content_keeps_a_grabbable_thumb(self):
        self.bar.set(0.0, 0.001)

        box = self._thumb()

        self.assertGreaterEqual(box[3] - box[1], scale(18, 1.0), "滑块被压得抓不住")


class ElideTextTests(unittest.TestCase):
    """文本裁剪助手：放得下就原样返回，放不下才截断并补省略号。"""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.metrics = tkfont.Font(root=self.root, font=("Microsoft YaHei UI", 11, "bold"))

    def test_text_that_fits_is_left_alone(self):
        text = "周报"

        self.assertEqual(elide_text(text, self.metrics, scale(400, 1.0)), text)

    def test_long_text_is_trimmed_and_marked_with_ellipsis(self):
        text = "这是一个很长的提醒标题" * 6
        width = scale(200, 1.0)
        result = elide_text(text, self.metrics, width)

        self.assertTrue(result.endswith("…"), f"截断后应带省略号，实际是 {result!r}")
        self.assertLess(len(result), len(text))
        self.assertTrue(text.startswith(result[:-1]), f"{result!r} 应当是原文本的前缀")
        self.assertLessEqual(self.metrics.measure(result), width, f"{result!r} 裁剪后仍放不下")

    def test_empty_text_stays_empty(self):
        self.assertEqual(elide_text("", self.metrics, scale(100, 1.0)), "")

    def test_keeps_original_when_even_the_ellipsis_cannot_fit(self):
        text = "很长的提醒标题" * 6

        self.assertEqual(elide_text(text, self.metrics, 1), text)


class CardTitleElisionTests(_RealAppTests):
    """卡片是固定高度的列表行，标题放不下时要补省略号，而不是硬截成半句。"""

    def _render_title(self, title: str) -> tuple[tk.Label, tkfont.Font]:
        self.app.tasks = [
            Task(id="t1", title=title, body="正文", trigger_type="weekly",
                 trigger_config={"hour": 9, "minute": 0, "weekdays": [0]}, enabled=True),
        ]
        self.app.selected_task_id = "t1"
        self.app._refresh_ui()
        self.app.root.update()
        shell = self.app.scroll_frame.grid_slaves(row=0, column=0)[0]
        label = shell.content.grid_slaves(row=0, column=2)[0]
        return label, tkfont.Font(root=label, font=label.cget("font"))

    def test_long_title_is_elided_to_fit_the_column(self):
        long_title = "每天下午提醒我起来走动一下别老坐着顺便去接杯水再回来"
        label, metrics = self._render_title(long_title)
        shown = str(label.cget("text"))

        self.assertNotEqual(shown, long_title, "长标题被原样塞进放不下的列里")
        self.assertTrue(shown.endswith("…"), f"长标题应带省略号提示还有内容，实际显示 {shown!r}")
        self.assertLessEqual(
            metrics.measure(shown), label.winfo_width(),
            f"{shown!r} 需要 {metrics.measure(shown)}px，列宽只有 {label.winfo_width()}px",
        )

    def test_short_title_is_shown_whole(self):
        label, _ = self._render_title("周报")

        self.assertEqual(str(label.cget("text")), "周报", "放得下的标题不该被加省略号")


class TaskListScrollbarTests(_RealAppTests):
    """提醒多到超出可视高度时，列表必须给出看得见的滚动条。"""

    def _render(self, count: int) -> None:
        self.app.tasks = [
            Task(id=f"t{i}", title=f"提醒 {i}", body=f"第 {i} 条", trigger_type="weekly",
                 trigger_config={"hour": 9, "minute": 0, "weekdays": [0]}, enabled=True)
            for i in range(1, count + 1)
        ]
        self.app.selected_task_id = None
        self.app._refresh_ui()
        self.app.root.update()

    def test_scrollbar_stays_hidden_while_every_reminder_fits(self):
        self._render(1)

        self.assertFalse(self.app.task_scrollbar.winfo_ismapped(), "内容没超出却出现了滚动条")

    def test_scrollbar_appears_once_the_list_overflows(self):
        self._render(12)

        self.assertTrue(self.app.task_scrollbar.winfo_ismapped(), "列表装不下了却没有滚动条")

    def test_shrinking_the_list_to_one_reminder_stops_it_scrolling(self):
        """提醒由多变少时滚动范围必须跟着缩回去。

        卡片是整批销毁重建的，重建后画布窗口项的实际高度还没缩回，
        scrollregion 会停在旧值：仅剩的一条会被滚出视区，滚轮也仍然能滚。
        """
        self._render(12)
        canvas = self.app.task_canvas
        canvas.yview_moveto(1.0)
        self.app.root.update()

        self.app.tasks = self.app.tasks[:1]
        self.app.selected_task_id = None
        self.app._refresh_ui()
        self.app.root.update()

        first, last = canvas.yview()
        self.assertAlmostEqual(first, 0.0, places=3, msg="内容已经装得下，列表却还停在下方")
        self.assertAlmostEqual(last, 1.0, places=3)
        self.assertFalse(self.app.task_scrollbar.winfo_ismapped(), "只剩一条却还显示滚动条")

    def test_the_wheel_cannot_move_a_list_that_already_fits(self):
        self._render(1)
        canvas = self.app.task_canvas
        before = canvas.yview()

        for _ in range(3):
            self.app._scroll_task_list(SimpleNamespace(delta=-120))
        self.app.root.update()

        self.assertEqual(canvas.yview(), before, "内容装得下时滚轮不该让列表移动")

    def test_a_newly_created_reminder_is_brought_into_view(self):
        """长列表里新建的提醒落在末尾，不滚过去用户会以为没建成功。"""
        self._render(12)
        canvas = self.app.task_canvas
        canvas.yview_moveto(0.0)
        self.app.root.update()

        new = Task(id="t99", title="新提醒", body="刚建的", trigger_type="weekly",
                   trigger_config={"hour": 8, "minute": 0, "weekdays": [0]}, enabled=True)
        self.app.tasks = self.app.tasks + [new]
        self.app.selected_task_id = new.id
        self.app._refresh_ui()
        self.app.root.update()
        self.app.root.update_idletasks()

        shell = self.app.task_cards[new.id]
        view_top = canvas.winfo_rooty()
        view_bottom = view_top + canvas.winfo_height()
        self.assertGreaterEqual(shell.winfo_rooty(), view_top, "新建的提醒落在视区上方")
        self.assertLessEqual(
            shell.winfo_rooty() + shell.winfo_height(), view_bottom,
            "新建的提醒被建在了视区之外，用户看不到自己刚建的那条",
        )

    def test_a_plain_refresh_leaves_the_user_scroll_position_alone(self):
        """没有换选中项时刷新不得把用户滚开的位置拽回去。"""
        self._render(12)
        canvas = self.app.task_canvas
        # 选中末尾那条，再手动滚回顶部：此刻选中项在视区之外，
        # 这正是「刷新会不会把用户拽回选中项」最容易露馅的状态。
        self.app.selected_task_id = self.app.tasks[-1].id
        self.app._refresh_ui()
        self.app.root.update()
        canvas.yview_moveto(0.0)
        self.app.root.update()
        user_position = canvas.yview()
        selected = self.app.task_cards[self.app.selected_task_id]
        view_top = canvas.winfo_rooty()
        view_bottom = view_top + canvas.winfo_height()
        self.assertTrue(
            selected.winfo_rooty() + selected.winfo_height() <= view_top
            or selected.winfo_rooty() >= view_bottom,
            "前置条件不成立：选中项应当已经在视区之外",
        )

        for _ in range(3):
            self.app._refresh_ui()
            self.app.root.update()

        self.assertEqual(canvas.yview(), user_position, "刷新把用户滚开的位置拽回去了")

    def test_dragging_the_scrollbar_reaches_the_end_of_the_list(self):
        self._render(12)
        bar = self.app.task_scrollbar
        canvas = self.app.task_canvas
        canvas.yview_moveto(0.0)
        self.app.root.update()
        bar.set(*canvas.yview())

        turned_off = SimpleNamespace(y=0)
        box = bar.bbox(bar.find_withtag("thumb")[0])
        bar._press(SimpleNamespace(y=box[3]))  # 抓住滑块下沿往下拖到底
        bar._drag(SimpleNamespace(y=bar.winfo_height()))
        bar._release(turned_off)
        self.app.root.update()

        first, last = canvas.yview()
        self.assertGreater(first, 0.0, "拖动滑块后列表没有滚动")
        self.assertAlmostEqual(last, 1.0, places=2, msg="拖到底也没看到列表末尾")


class NewTaskShortcutTests(_RealAppTests):
    """Ctrl+N 是新建提醒的键盘入口，要和按钮走同一条路径。"""

    def test_control_n_is_bound_on_the_main_window(self):
        self.assertTrue(self.app.root.bind("<Control-n>"), "Ctrl+N 没有绑定到主窗口")

    def test_pressing_control_n_opens_the_new_task_flow(self):
        with patch.object(self.app, "add_task") as add_task:
            self.app.root.event_generate("<Control-n>")
            self.app.root.update()

        add_task.assert_called_once()


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


class SnoozeSchedulerTests(unittest.TestCase):
    """稍后提醒只补发一次通知，不能把提醒本身的调度语义带坏。"""

    def setUp(self):
        _reclaim_idle_tk_interpreters()

    @staticmethod
    def _future_once_task(task_id: str = "snz") -> Task:
        # 排在 1 小时之后：既让任务保持启用，也保证它自己的 job 不会在测试里抢先触发。
        when = datetime.now() + timedelta(hours=1)
        return Task(
            id=task_id, title="周报", body="改写周报了",
            trigger_type="once",
            trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()},
            enabled=True,
        )

    def test_snooze_notifies_without_disabling_a_one_off_task(self):
        notifier = MagicMock()
        fired = Event()
        scheduler = TaskScheduler(notifier)
        scheduler._scheduler.add_listener(lambda _event: fired.set(), EVENT_JOB_EXECUTED)
        scheduler.start()
        try:
            task = self._future_once_task()
            scheduler.upsert_task(task)
            self.assertTrue(task.enabled)

            # minutes=0 让补发立刻执行，从而跑到真实的「注册 job -> 触发 -> 发通知」路径。
            self.assertTrue(scheduler.snooze_task(task.id, minutes=0))
            self.assertTrue(fired.wait(10), "稍后提醒没有触发")

            notifier.send.assert_called_once_with(task)
            self.assertTrue(task.enabled, "稍后提醒把单次提醒停用了——补发不该消耗提醒本身")
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_snooze_still_fires_for_a_reminder_that_auto_disabled_itself(self):
        """单次提醒响过就自动停用，而那一刻的「稍后再说」必须仍然发得出来。"""
        notifier = MagicMock()
        fired = Event()
        scheduler = TaskScheduler(notifier)
        scheduler._scheduler.add_listener(lambda _event: fired.set(), EVENT_JOB_EXECUTED)
        scheduler.start()
        try:
            task = self._future_once_task()
            scheduler.upsert_task(task)
            task.enabled = False  # 已经响过一次，按单次提醒的语义自动停用

            self.assertTrue(scheduler.snooze_task(task.id, minutes=0))
            self.assertTrue(fired.wait(10), "稍后提醒没有触发")

            notifier.send.assert_called_once_with(task)
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_snooze_keeps_its_own_job_id_apart_from_the_task(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        try:
            task = self._future_once_task()
            scheduler.upsert_task(task)
            own_next_run = scheduler.task_next_run(task.id)

            self.assertTrue(scheduler.snooze_task(task.id, minutes=5))

            self.assertIsNotNone(scheduler._scheduler.get_job(f"{SNOOZE_JOB_PREFIX}{task.id}"),
                                 "稍后提醒没有排上")
            self.assertIsNotNone(scheduler._scheduler.get_job(task.id), "提醒自己的调度被顶掉了")
            self.assertEqual(scheduler.task_next_run(task.id), own_next_run,
                             "稍后提醒改动了提醒自己的下次运行时间")
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_snooze_refuses_a_task_that_does_not_exist(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        try:
            self.assertFalse(scheduler.snooze_task("这条提醒不存在"))
            self.assertIsNone(scheduler._scheduler.get_job(f"{SNOOZE_JOB_PREFIX}这条提醒不存在"))
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_removing_a_task_cancels_its_pending_snooze(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        try:
            task = self._future_once_task()
            scheduler.upsert_task(task)
            self.assertTrue(scheduler.snooze_task(task.id, minutes=5))

            scheduler.remove_task(task.id)

            self.assertIsNone(scheduler._scheduler.get_job(f"{SNOOZE_JOB_PREFIX}{task.id}"),
                              "提醒都删了，挂起的稍后提醒还会再弹一次")
        finally:
            scheduler._scheduler.shutdown(wait=True)

    def test_snooze_stays_silent_while_all_reminders_are_paused(self):
        notifier = MagicMock()
        scheduler = TaskScheduler(notifier)
        scheduler.start()
        try:
            task = self._future_once_task()
            scheduler.upsert_task(task)
            scheduler.pause_all()

            scheduler._notify(task.id)

            notifier.send.assert_not_called()
        finally:
            scheduler._scheduler.shutdown(wait=True)


class SnoozeMenuTests(_MenuOpeningTests):
    """卡片菜单要能安排稍后提醒，并且菜单项真的连到调度器。"""

    LABEL = f"{DEFAULT_SNOOZE_MINUTES} 分钟后再说"

    def _snooze_entry_index(self) -> int:
        menu = self.app._task_menu
        labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)]
        self.assertIn(self.LABEL, labels, f"卡片菜单里没有稍后提醒：{labels}")
        return labels.index(self.LABEL)

    def _open_menu_with(self, *, paused: bool):
        task = Task(id="snz", title="周报", trigger_type="weekly",
                    trigger_config={"hour": 9, "minute": 0, "weekdays": [0]}, enabled=True)
        self.app._selected_task = lambda: task
        self.app.scheduler = MagicMock()
        self.app.scheduler.paused = paused
        self.app.scheduler.snooze_task.return_value = True
        self.app._open_task_menu("snz")

    def test_menu_offers_snooze_and_schedules_it(self):
        with patch.object(app_module, "show_info") as info:
            self._open_menu_with(paused=False)

            # 光有菜单项不算数，点下去必须真的调到调度器。
            self.app._task_menu.invoke(self._snooze_entry_index())

        self.app.scheduler.snooze_task.assert_called_once_with("snz")
        info.assert_called_once()
        self.assertIn("10 分钟", info.call_args[0][2])

    def test_menu_refuses_snooze_while_all_reminders_are_paused(self):
        """暂停期间补发不会响，界面必须先说明清楚，别给出落空的成功提示。"""
        with patch.object(app_module, "show_info") as info:
            self._open_menu_with(paused=True)

            self.app._task_menu.invoke(self._snooze_entry_index())

        self.app.scheduler.snooze_task.assert_not_called()
        self.assertIn("暂停", info.call_args[0][2])


class TaskSaveRollbackTests(unittest.TestCase):
    def setUp(self):
        _reclaim_idle_tk_interpreters()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "tasks.json"
        self.app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        self.app.root = MagicMock()
        self.app.tasks = []
        self.app.selected_task_id = None
        self.app._ui_ready = True
        self.app._refresh_queue = queue.Queue()
        self.app._refresh_ui = MagicMock()
        self.app.store = TaskStore(self.path)
        self.app.scheduler = TaskScheduler(MagicMock(), on_change=self.app._schedule_refresh)
        self.app.scheduler.start()
        self.addCleanup(self.app.scheduler._scheduler.shutdown, wait=True)

    def test_failed_add_leaves_no_active_reminder(self):
        self.app.store.save_tasks([])
        before = self.path.read_bytes()
        task = Task(id="save-add", trigger_type="interval", trigger_config={"hours": 1})
        with patch.object(app_module, "TaskDialog", return_value=SimpleNamespace(result=task)), \
             patch("store.os.fsync", side_effect=OSError("磁盘已满")), \
             patch.object(app_module, "show_error") as error:
            self.app.add_task()
        self.assertEqual(self.app.tasks, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.app.scheduler.task_next_run(task.id), "保存失败不得留下幽灵调度")
        self.assertNotIn(task.id, self.app.scheduler._tasks)
        self.assertIsNone(self.app.selected_task_id)
        error.assert_called_once()

    def _existing_task(self) -> Task:
        task = Task(id="save-existing", title="原提醒", trigger_type="interval", trigger_config={"hours": 1})
        self.app._save_task_change(task)
        self.app.scheduler.snooze_task(task.id)
        return task

    def test_failed_edit_keeps_old_content_and_exact_next_run(self):
        task = self._existing_task()
        before = self.path.read_bytes()
        next_run = self.app.scheduler._scheduler.get_job(task.id).next_run_time
        edited = Task(id=task.id, title="新提醒", trigger_type="interval", trigger_config={"minutes": 5})
        with patch.object(app_module, "TaskDialog", return_value=SimpleNamespace(result=edited)), \
             patch("store.os.fsync", side_effect=OSError("磁盘已满")), \
             patch.object(app_module, "show_error") as error:
            self.app.edit_selected_task()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIs(self.app.tasks[0], task)
        self.assertIs(self.app.scheduler._tasks[task.id], task)
        job = self.app.scheduler._scheduler.get_job(task.id)
        self.assertEqual(job.next_run_time, next_run, "回滚不得重置固定间隔计时")
        self.assertEqual(job.trigger.interval, timedelta(hours=1))
        error.assert_called_once()

    def test_failed_delete_restores_reminder_and_snooze(self):
        task = self._existing_task()
        before = self.path.read_bytes()
        next_run = self.app.scheduler._scheduler.get_job(task.id).next_run_time
        snooze_id = self.app.scheduler._snooze_job_id(task.id)
        snooze_run = self.app.scheduler._scheduler.get_job(snooze_id).next_run_time
        with patch.object(app_module, "ask_yes_no", return_value=True), \
             patch("store.os.fsync", side_effect=OSError("磁盘已满")), \
             patch.object(app_module, "show_error") as error:
            self.app.delete_selected_task()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.app.tasks, [task])
        self.assertEqual(self.app.selected_task_id, task.id)
        self.assertEqual(self.app.scheduler._scheduler.get_job(task.id).next_run_time, next_run)
        self.assertEqual(self.app.scheduler._scheduler.get_job(snooze_id).next_run_time, snooze_run)
        error.assert_called_once()

    def test_failed_disable_keeps_enabled_state_and_job(self):
        task = self._existing_task()
        before = self.path.read_bytes()
        next_run = self.app.scheduler._scheduler.get_job(task.id).next_run_time
        with patch("store.os.fsync", side_effect=OSError("磁盘已满")), \
             patch.object(app_module, "show_error") as error:
            self.app.toggle_selected_task()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(task.enabled)
        self.assertIs(self.app.tasks[0], task)
        self.assertEqual(self.app.scheduler._scheduler.get_job(task.id).next_run_time, next_run)
        error.assert_called_once()

    def test_successful_changes_keep_file_and_scheduler_in_sync(self):
        task = self._existing_task()
        self.app.toggle_selected_task()
        self.assertFalse(self.app.store.load_tasks()[0].enabled)
        self.assertIsNone(self.app.scheduler.task_next_run(task.id))
        self.app.toggle_selected_task()
        self.assertTrue(self.app.store.load_tasks()[0].enabled)
        self.assertIsNotNone(self.app.scheduler.task_next_run(task.id))
        with patch.object(app_module, "ask_yes_no", return_value=True):
            self.app.delete_selected_task()
        self.assertEqual(self.app.store.load_tasks(), [])
        self.assertNotIn(task.id, self.app.scheduler._tasks)
        self.assertEqual(self.app.scheduler._scheduler.get_jobs(), [])

    def test_failed_schedule_keeps_old_file_and_job(self):
        task = self._existing_task()
        before = self.path.read_bytes()
        job = self.app.scheduler._scheduler.get_job(task.id)
        edited = Task(id=task.id, trigger_type="interval", trigger_config={"minutes": 5})
        jobstore = self.app.scheduler._scheduler._lookup_jobstore("default")
        with patch.object(jobstore, "update_job", side_effect=OSError("调度存储失败")):
            with self.assertRaises(OSError):
                self.app._save_task_change(edited)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIs(self.app.tasks[0], task)
        self.assertIs(self.app.scheduler._scheduler.get_job(task.id), job)

    def test_new_job_cannot_fire_during_failed_disk_write(self):
        when = datetime.now() + timedelta(seconds=1)
        task = Task(id="save-race", trigger_type="once",
                    trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})

        def fail_after_due(_descriptor):
            self.assertIsNotNone(self.app.scheduler.task_next_run(task.id))
            time.sleep(max(0, (when - datetime.now()).total_seconds()) + 0.2)
            raise OSError("写盘失败")

        with patch("store.os.fsync", side_effect=fail_after_due):
            with self.assertRaises(OSError):
                self.app._save_task_change(task)
        time.sleep(0.1)
        self.app.scheduler.notifier.send.assert_not_called()
        self.assertEqual(self.app.tasks, [])
        self.assertIsNone(self.app.scheduler.task_next_run(task.id))

    def test_failed_enable_keeps_disabled_state_and_no_job(self):
        task = Task(id="save-disabled", enabled=False, trigger_type="interval", trigger_config={"hours": 1})
        self.app._save_task_change(task)
        before = self.path.read_bytes()
        with patch("store.os.fsync", side_effect=OSError("磁盘已满")), \
             patch.object(app_module, "show_error") as error:
            self.app.toggle_selected_task()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.app.tasks[0].enabled)
        self.assertIs(self.app.scheduler._tasks[task.id], task)
        self.assertIsNone(self.app.scheduler.task_next_run(task.id))
        error.assert_called_once()

    def test_refresh_error_is_not_misreported_as_failed_save(self):
        task = Task(id="save-refresh", trigger_type="interval", trigger_config={"hours": 1})
        self.app._refresh_ui.side_effect = RuntimeError("渲染失败")
        with patch.object(app_module, "TaskDialog", return_value=SimpleNamespace(result=task)), \
             patch.object(app_module, "show_error") as error:
            with self.assertRaisesRegex(RuntimeError, "渲染失败"):
                self.app.add_task()
        self.assertEqual(self.app.store.load_tasks()[0].id, task.id)
        self.assertIsNotNone(self.app.scheduler.task_next_run(task.id))
        error.assert_not_called()


class CoreRegressionTests(unittest.TestCase):
    def setUp(self):
        # 这里同样会启动真实调度器线程，和 SnoozeSchedulerTests 一样先收干净 Tk 解释器。
        _reclaim_idle_tk_interpreters()

    def test_repeated_icon_generation_reuses_assets_and_repairs_missing_files(self):
        import core.icon_assets as assets

        with TemporaryDirectory() as directory:
            root = Path(directory)
            sizes = (16, 32, 256)
            pngs = tuple(root / f"icon-{size}.png" for size in sizes)
            with patch.multiple(assets, ICON_FILE=root / "tray.png", TOAST_IMAGE_FILE=root / "toast.png",
                                ICON_ICO_FILE=root / "app.ico", ICON_PNG_FILES=pngs, ICON_PNG_SIZES=sizes):
                assets.ensure_icon_assets()
                with patch.object(assets, "_build_leaf_icon", wraps=assets._build_leaf_icon) as render:
                    assets.ensure_icon_assets()
                    render.assert_not_called()
                    pngs[0].unlink()
                    assets.ensure_icon_assets()
                    self.assertTrue(pngs[0].is_file())
                    self.assertGreater(render.call_count, 0)

    def test_tray_icon_load_does_not_retain_file_handle(self):
        import gui.tray as tray_module
        from PIL import Image

        with TemporaryDirectory() as directory:
            path = Path(directory) / "tray.png"
            with Image.new("RGBA", (16, 16), "red") as source:
                source.save(path)
            controller = tray_module.TrayController.__new__(tray_module.TrayController)
            with patch.object(tray_module, "ICON_FILE", path), patch.object(tray_module, "ensure_icon_assets"):
                image = controller._load_icon()
            try:
                self.assertIsNone(getattr(image, "fp", None))
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))
            finally:
                image.close()

    def test_concurrent_icon_initialization_generates_one_complete_set(self):
        import core.icon_assets as assets

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(assets, ICON_FILE=root / "tray.png", TOAST_IMAGE_FILE=root / "toast.png",
                                ICON_ICO_FILE=root / "app.ico", ICON_PNG_FILES=(root / "small.png",), ICON_PNG_SIZES=(16,)), \
                 patch.object(assets, "_build_leaf_icon", wraps=assets._build_leaf_icon) as render:
                errors = []

                def initialize():
                    try:
                        assets.ensure_icon_assets()
                    except Exception as exc:
                        errors.append(exc)

                workers = [threading.Thread(target=initialize) for _ in range(3)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(3)
                self.assertFalse(any(worker.is_alive() for worker in workers))
                self.assertEqual(errors, [])
                self.assertEqual(render.call_count, 3)

    def test_failed_icon_write_is_retried_instead_of_marked_complete(self):
        import core.icon_assets as assets
        from PIL import Image

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(assets, ICON_FILE=root / "tray.png", TOAST_IMAGE_FILE=root / "toast.png",
                                ICON_ICO_FILE=root / "app.ico", ICON_PNG_FILES=(root / "small.png",), ICON_PNG_SIZES=(16,)):
                with patch.object(Image.Image, "save", side_effect=OSError("磁盘已满")):
                    with self.assertRaises(OSError):
                        assets.ensure_icon_assets()
                assets.ensure_icon_assets()
                self.assertTrue(all(path.is_file() for path in root.iterdir()))
                self.assertEqual(len(list(root.iterdir())), 4)

    def test_dpi_setup_uses_api_results_to_choose_fallback(self):
        import ctypes

        for v2_result, shcore_result, expected in [(1, 0, "v2"), (0, 0, "shcore"), (0, -1, "legacy")]:
            with self.subTest(expected=expected):
                windows = MagicMock()
                windows.user32.SetProcessDpiAwarenessContext.return_value = v2_result
                windows.shcore.SetProcessDpiAwareness.return_value = shcore_result
                with patch.object(ctypes, "windll", windows):
                    import main
                    windows.reset_mock()
                    main._enable_high_dpi()
                self.assertEqual(windows.shcore.SetProcessDpiAwareness.called, expected != "v2")
                self.assertEqual(windows.user32.SetProcessDPIAware.called, expected == "legacy")

    def test_missing_shcore_dll_does_not_prevent_legacy_dpi_fallback(self):
        import ctypes
        import main
        from unittest.mock import PropertyMock

        windows = MagicMock()
        windows.user32.SetProcessDpiAwarenessContext.side_effect = AttributeError("V2 unavailable")
        type(windows).shcore = PropertyMock(side_effect=OSError("shcore unavailable"))
        with patch.object(ctypes, "windll", windows):
            main._enable_high_dpi()
        windows.user32.SetProcessDPIAware.assert_called_once()

    def test_pause_after_dispatch_replays_once_reminder_on_resume(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        finished = Event()
        scheduler._scheduler.add_listener(lambda _event: finished.set(), EVENT_JOB_EXECUTED)
        with scheduler._lock:
            when = datetime.now() + timedelta(seconds=0.3)
            task = Task(id="pause-dispatch", trigger_type="once",
                        trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
            scheduler.upsert_task(task)
            deadline = time.monotonic() + 3
            while scheduler._scheduler.get_job(task.id) is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(scheduler._scheduler.get_job(task.id), "等待调度器确实取走单次 job")
            scheduler.pause_all()
        self.assertTrue(finished.wait(3))
        scheduler.notifier.send.assert_not_called()
        self.assertIsNotNone(scheduler._scheduler.get_job(task.id), "暂停必须保留已取走的单次提醒")
        finished.clear()
        scheduler.resume_all()
        self.assertTrue(finished.wait(3))
        scheduler.notifier.send.assert_called_once_with(task)
        self.assertFalse(task.enabled)

    def test_pause_after_snooze_dispatch_preserves_supplemental_reminder(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        finished = Event()
        scheduler._scheduler.add_listener(lambda _event: finished.set(), EVENT_JOB_EXECUTED)
        task = Task(id="pause-snooze", enabled=False)
        with scheduler._lock:
            scheduler.upsert_task(task)
            scheduler.snooze_task(task.id, minutes=0)
            job_id = scheduler._snooze_job_id(task.id)
            deadline = time.monotonic() + 3
            while scheduler._scheduler.get_job(job_id) is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(scheduler._scheduler.get_job(job_id))
            scheduler.pause_all()
        self.assertTrue(finished.wait(3))
        self.assertIsNotNone(scheduler._scheduler.get_job(job_id))
        scheduler.notifier.send.assert_not_called()
        finished.clear()
        scheduler.resume_all()
        self.assertTrue(finished.wait(3))
        scheduler.notifier.send.assert_called_once_with(task)
        self.assertFalse(task.enabled)

    def test_paused_replay_does_not_replace_existing_new_schedule(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        when = datetime.now() + timedelta(hours=1)
        task = Task(id="pause-existing", trigger_type="once",
                    trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
        scheduler.upsert_task(task)
        original = scheduler._scheduler.get_job(task.id)
        scheduler.pause_all()
        scheduler._run_task(task.id)
        self.assertIs(scheduler._scheduler.get_job(task.id), original)
        scheduler.resume_all()
        scheduler.notifier.send.assert_not_called()

    def test_resume_before_paused_worker_finishes_does_not_drop_replay(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        deferred, release, sent = Event(), Event(), Event()
        scheduler.notifier.send.side_effect = lambda _task: sent.set()
        original_run = scheduler._run_task
        calls = []

        def delayed_worker(task_id):
            calls.append(task_id)
            original_run(task_id)
            if len(calls) == 1:
                deferred.set()
                release.wait(3)

        with patch.object(scheduler, "_run_task", delayed_worker):
            try:
                with scheduler._lock:
                    when = datetime.now() + timedelta(seconds=0.3)
                    task = Task(id="resume-inflight", trigger_type="once",
                                trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
                    scheduler.upsert_task(task)
                    deadline = time.monotonic() + 3
                    while scheduler._scheduler.get_job(task.id) is not None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIsNone(scheduler._scheduler.get_job(task.id))
                    scheduler.pause_all()
                self.assertTrue(deferred.wait(3))
                scheduler.resume_all()
                self.assertTrue(sent.wait(3), "旧 worker 尚未退出也不能让补发被 max_instances 丢弃")
            finally:
                release.set()
        scheduler.notifier.send.assert_called_once_with(task)

    def test_malformed_weekdays_can_be_opened_and_repaired_in_editor(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        for value in (None, 7, "10", [True, 8, "1"]):
            with self.subTest(value=value):
                config = {"weekdays": value, "hour": 9}
                task = Task(trigger_type="weekly", trigger_config=config, enabled=False)
                dialog = TaskDialog(root, task=task)
                try:
                    self.assertFalse(any(var.get() for var in dialog.weekday_vars.values()))
                    self.assertEqual(task.trigger_config, config)
                    dialog.weekday_vars[0].set(True)
                    self.assertEqual(dialog._collect_trigger_config("weekly"),
                                     {"weekdays": [0], "hour": 9, "minute": 0, "second": 0})
                finally:
                    dialog.destroy()

    def test_once_editor_accepts_future_time_with_explicit_offset(self):
        when = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)
        dialog = TaskDialog.__new__(TaskDialog)
        dialog.once_date_var = SimpleNamespace(get=lambda: when.date().isoformat())
        dialog.once_time_var = SimpleNamespace(get=lambda: when.timetz().isoformat())
        self.assertEqual(dialog._collect_trigger_config("once"),
                         {"date": when.date().isoformat(), "time": when.timetz().isoformat()})

    def test_once_editor_rejects_past_time_with_explicit_offset(self):
        when = datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1)
        dialog = TaskDialog.__new__(TaskDialog)
        dialog.once_date_var = SimpleNamespace(get=lambda: when.date().isoformat())
        dialog.once_time_var = SimpleNamespace(get=lambda: when.timetz().isoformat())
        with self.assertRaisesRegex(ValueError, "晚于当前时间"):
            dialog._collect_trigger_config("once")

    def test_editor_keeps_valid_weekdays_from_mixed_corrupt_array(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        config = {"weekdays": [0, True, 6, -1, "2"], "hour": 9}
        task = Task(trigger_type="weekly", trigger_config=config, enabled=False)
        dialog = TaskDialog(root, task=task)
        try:
            self.assertEqual([day for day, var in dialog.weekday_vars.items() if var.get()], [0, 6])
            self.assertEqual(task.trigger_config, config)
        finally:
            dialog.destroy()

    def test_calendar_configs_reject_impossible_dates_and_ambiguous_numbers(self):
        scheduler = TaskScheduler(MagicMock())
        cases = [("yearly", {"month": 2, "day": 30}),
                 ("weekly", {"weekdays": "10", "hour": 9}),
                 ("weekly", {"weekdays": [0], "hour": 9.9}),
                 ("weekly", {"weekdays": [True], "hour": 9}),
                 ("weekly_last_workday", {"hour": True})]
        for kind, config in cases:
            with self.subTest(kind=kind, config=config), self.assertRaises(ValueError):
                scheduler._build_trigger(Task(trigger_type=kind, trigger_config=config))

    def test_calendar_configs_keep_leap_day_and_integer_string_times(self):
        scheduler = TaskScheduler(MagicMock())
        task = Task(trigger_type="yearly", trigger_config={"month": "2", "day": "29", "hour": "9"})
        trigger = scheduler._build_trigger(task)
        next_run = trigger.get_next_fire_time(None, datetime(2026, 10, 18, tzinfo=trigger.timezone))
        self.assertEqual((next_run.year, next_run.month, next_run.day, next_run.hour), (2028, 2, 29, 9))
        scheduler._build_trigger(Task(trigger_type="weekly", trigger_config={"weekdays": [0, 6], "hour": "9"}))

    def test_impossible_yearly_date_is_isolated_at_startup(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        broken = Task(id="invalid-yearly", trigger_type="yearly", trigger_config={"month": 2, "day": 30})
        healthy = Task(id="valid-weekly", trigger_type="weekly", trigger_config={"weekdays": [0], "hour": 9})
        scheduler.sync_tasks([broken, healthy])
        self.assertFalse(broken.enabled)
        self.assertIsNone(scheduler._scheduler.get_job(broken.id))
        self.assertIsNotNone(scheduler.task_next_run(healthy.id))

    def test_editor_rejects_invalid_calendar_time_before_returning_task(self):
        dialog = SimpleNamespace(
            trigger_type_var=SimpleNamespace(get=lambda: "weekly"),
            _collect_trigger_config=lambda _kind: {"weekdays": [0], "hour": 25},
            body_text=None, title_var=SimpleNamespace(get=lambda: "有效标题"),
            body_var=SimpleNamespace(get=lambda: ""), enabled_var=SimpleNamespace(get=lambda: True),
            _task=None, result=None, destroy=MagicMock(),
        )
        with patch("gui.task_dialog.show_error") as error:
            TaskDialog._save(dialog)
        self.assertIsNone(dialog.result)
        dialog.destroy.assert_not_called()
        error.assert_called_once()

    def test_extreme_interval_is_disabled_without_interrupting_healthy_tasks(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        broken = Task(id="overflow-interval", trigger_type="interval", trigger_config={"days": 10 ** 15})
        healthy = Task(id="healthy-interval", trigger_type="interval", trigger_config={"minutes": 1})
        scheduler.sync_tasks([broken, healthy])
        self.assertFalse(broken.enabled)
        self.assertIsNone(scheduler.task_next_run(broken.id))
        self.assertIsNotNone(scheduler.task_next_run(healthy.id))
        broken.enabled = True
        with self.assertRaises(ValueError):
            broken.validate()

    def test_interval_date_overflow_and_infinity_are_validation_errors(self):
        for values in ({"days": 4_000_000}, {"seconds": float("inf")}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    Task(trigger_type="interval", trigger_config=values).validate()
        Task(trigger_type="interval", trigger_config={"weeks": 52, "seconds": 1}).validate()

    def test_notification_text_is_data_and_child_failure_is_reported(self):
        import base64
        import xml.etree.ElementTree as ET

        notifier = notifier_module.Notifier.__new__(notifier_module.Notifier)
        title = 'Total $5 "hello" ]]> $(throw "not code")'
        task = Task(title=title, body='正文 & < >\n"@')
        with patch.object(notifier, "_ensure_start_menu_shortcut"), \
             patch.object(notifier_module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            notifier.send(task)
        run.assert_called_once()
        command = run.call_args.args[0]
        script = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertNotIn(title, script)
        xml_data = run.call_args.kwargs.get("input")
        self.assertIsNotNone(xml_data, "通知正文应从标准输入传递，避免命令行长度限制")
        document = ET.fromstring(base64.b64decode(xml_data).decode("utf-8"))
        self.assertEqual(document.find(".//text[@id='1']").text, title)
        self.assertEqual(document.find(".//text[@id='2']").text, task.body)
        self.assertTrue(run.call_args.kwargs["check"])
        with patch.object(notifier, "_ensure_start_menu_shortcut"), \
             patch.object(notifier_module.subprocess, "run", side_effect=subprocess.CalledProcessError(7, "powershell")):
            with self.assertRaises(subprocess.CalledProcessError):
                notifier.send(task)

    def test_failed_workday_send_still_schedules_next_week(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.notifier.send.side_effect = OSError("发送失败")
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        finished = Event()
        scheduler._scheduler.add_listener(lambda _event: finished.set(), EVENT_JOB_ERROR)
        when = datetime.now() + timedelta(seconds=1)
        following = when + timedelta(days=7)
        task = Task(id="workday-fail", trigger_type="weekly_last_workday", trigger_config={"hour": 18})
        with patch.object(scheduler, "_next_weekly_last_workday", side_effect=[when, following]):
            scheduler.upsert_task(task)
            self.assertTrue(finished.wait(5))
        self.assertTrue(task.enabled)
        self.assertEqual(scheduler._scheduler.get_job(task.id).next_run_time.replace(tzinfo=None), following)

    def test_failed_once_send_is_not_marked_as_success(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.notifier.send.side_effect = OSError("发送失败")
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        when = datetime.now() + timedelta(hours=1)
        task = Task(id="once-fail", trigger_type="once",
                    trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
        scheduler.upsert_task(task)
        with self.assertRaises(OSError):
            scheduler._run_task(task.id)
        self.assertTrue(task.enabled)
        self.assertIsNotNone(scheduler.task_next_run(task.id))
        self.assertLess(scheduler._scheduler.get_job(task.id).next_run_time.replace(tzinfo=None), when)
        scheduler.notifier.send.side_effect = None
        scheduler._run_task(task.id)
        self.assertFalse(task.enabled)

    def test_failed_exit_save_keeps_application_running(self):
        app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        app.root = MagicMock()
        app.tray = MagicMock()
        app.scheduler = MagicMock()
        app._clock_after_id = "clock"
        app._weather_after_id = "weather"
        app._persist_tasks = MagicMock(side_effect=OSError("磁盘已满"))
        with patch.object(app_module, "stop_helper") as stop, patch.object(app_module, "show_error") as error:
            app.exit_app()
        app.root.after_cancel.assert_not_called()
        app.tray.stop.assert_not_called()
        app.scheduler.shutdown.assert_not_called()
        stop.assert_not_called()
        error.assert_called_once()
        self.assertEqual(app._clock_after_id, "clock")

    def test_wake_helper_failure_allows_direct_fallback(self):
        import single_instance as instance

        with patch.object(instance, "_wake_exe_path", return_value=Path("wake.exe")), \
             patch.object(instance.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertFalse(instance._send_via_helper())

    def test_first_instance_starts_only_one_pipe_listener(self):
        import single_instance as instance

        kernel = MagicMock()
        kernel.CreateMutexW.return_value = 123
        kernel.GetLastError.return_value = 0
        with patch.object(instance, "kernel32", kernel), \
             patch.object(instance, "_mutex_handle", None), \
             patch.object(instance.subprocess, "Popen") as helper, \
             patch.object(instance.threading, "Thread") as thread:
            instance.single_instance_guard()
        helper.assert_not_called()
        thread.return_value.start.assert_called_once()

    def test_wake_write_failure_is_not_success(self):
        import single_instance as instance
        import wake

        for module, operation in [(instance, instance._send_direct), (wake, lambda: wake._send_to_pipe(b"WAKE"))]:
            kernel = MagicMock()
            kernel.WaitNamedPipeW.return_value = True
            kernel.CreateFileW.return_value = 123
            kernel.WriteFile.return_value = False
            with self.subTest(module=module.__name__), patch.object(module, "kernel32", kernel):
                if module is instance:
                    with patch.object(instance, "_listener_ready"):
                        self.assertFalse(operation())
                else:
                    self.assertFalse(operation())
            kernel.CloseHandle.assert_called_once_with(123)

    def test_real_pipe_roundtrip_uses_only_main_listener(self):
        import single_instance as instance
        import wake
        from uuid import uuid4

        name = rf"\\.\pipe\NoticeTest_{uuid4().hex}"
        ready, stop, received = Event(), Event(), Event()
        with patch.object(instance, "PIPE_NAME", name), patch.object(wake, "PIPE_NAME", name), \
             patch.object(instance, "_listener_ready", ready), patch.object(instance, "_listener_stop", stop), \
             patch.object(instance, "_SHOW_CALLBACK", received.set):
            thread = threading.Thread(target=instance._listener_thread, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                self.assertTrue(wake._send_to_pipe(b"WAKE"))
                self.assertTrue(received.wait(3))
            finally:
                stop.set()
                deadline = time.monotonic() + 3
                while thread.is_alive() and time.monotonic() < deadline:
                    instance._send_direct()
                    thread.join(0.05)
                self.assertFalse(thread.is_alive(), "测试监听线程必须退出")

    def test_startup_wake_is_delivered_once_after_callback_registration(self):
        import single_instance as instance
        from uuid import uuid4

        name = rf"\\.\pipe\NoticeStartupTest_{uuid4().hex}"
        ready, stop = Event(), Event()
        callback = MagicMock()
        with patch.object(instance, "PIPE_NAME", name), patch.object(instance, "_listener_ready", ready), \
             patch.object(instance, "_listener_stop", stop), patch.object(instance, "_SHOW_CALLBACK", None), \
             patch.object(instance, "_pending_wake", False):
            thread = threading.Thread(target=instance._listener_thread, daemon=True)
            thread.start()
            try:
                self.assertTrue(ready.wait(3))
                self.assertTrue(instance._send_direct())
                # 再连上下一轮管道，证明上一条请求已被主监听读完，避免靠 sleep 猜处理时机。
                self.assertTrue(instance.kernel32.WaitNamedPipeW(name, 3000))
                instance.register_show_callback(callback)
                callback.assert_called_once()
                instance.register_show_callback(callback)
                callback.assert_called_once()
            finally:
                stop.set()
                deadline = time.monotonic() + 3
                while thread.is_alive() and time.monotonic() < deadline:
                    instance._send_direct()
                    thread.join(0.05)
                self.assertFalse(thread.is_alive())

    def test_shutdown_during_send_does_not_rearm_or_refresh(self):
        entered, release = Event(), Event()

        def delayed_failure(_task):
            entered.set()
            self.assertTrue(release.wait(3))
            raise OSError("退出期间发送失败")

        changed = MagicMock()
        scheduler = TaskScheduler(MagicMock(), on_change=changed)
        scheduler.notifier.send.side_effect = delayed_failure
        scheduler.start()
        when = datetime.now() + timedelta(hours=1)
        task = Task(id="shutdown-send", trigger_type="once",
                    trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
        scheduler.upsert_task(task)
        changed.reset_mock()
        errors = []

        def worker():
            try:
                scheduler._run_task(task.id)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            scheduler.shutdown()
        finally:
            release.set()
            thread.join(3)
            if scheduler._scheduler.running:
                scheduler._scheduler.shutdown(wait=True)
        self.assertFalse(thread.is_alive())
        self.assertEqual(scheduler._scheduler.get_jobs(), [])
        changed.assert_not_called()

    def test_stopped_scheduler_does_not_send_or_accept_snooze(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        when = datetime.now() + timedelta(hours=1)
        task = Task(id="stopped", trigger_type="once",
                    trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
        scheduler.upsert_task(task)
        scheduler.shutdown()
        scheduler._run_task(task.id)
        scheduler._fire_snooze(task.id)
        scheduler.notifier.send.assert_not_called()
        self.assertFalse(scheduler.snooze_task(task.id))

    def test_stopped_listener_discards_pending_startup_wake(self):
        import single_instance as instance

        stop = Event()
        stop.set()
        callback = MagicMock()
        with patch.object(instance, "_pending_wake", True), patch.object(instance, "_SHOW_CALLBACK", None), \
             patch.object(instance, "_listener_stop", stop):
            instance.register_show_callback(callback)
            callback.assert_not_called()
            self.assertFalse(instance._pending_wake)

    def test_wake_cli_returns_failure_exit_code(self):
        import wake

        for success in (False, True):
            with self.subTest(success=success), patch.object(wake, "_send_to_pipe", return_value=success):
                with self.assertRaises(SystemExit) as result:
                    wake._run_wake()
                self.assertEqual(result.exception.code, 0 if success else 1)

    def test_notification_timeout_is_reported(self):
        notifier = notifier_module.Notifier.__new__(notifier_module.Notifier)
        with patch.object(notifier, "_ensure_start_menu_shortcut"), \
             patch.object(notifier_module.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell", 15)), \
             patch.object(notifier_module.pythoncom, "CoUninitialize") as release:
            with self.assertRaises(subprocess.TimeoutExpired):
                notifier.send(Task())
        release.assert_called_once()

    def test_long_notification_does_not_expand_windows_command_line(self):
        notifier = notifier_module.Notifier.__new__(notifier_module.Notifier)
        task = Task(body="长正文" * 5000)
        with patch.object(notifier, "_ensure_start_menu_shortcut"), \
             patch.object(notifier_module.subprocess, "run") as run:
            notifier.send(task)
        command = subprocess.list2cmdline(run.call_args.args[0])
        self.assertLess(len(command), 32767)
        self.assertTrue(run.call_args.kwargs.get("input"))

    def test_failed_settings_save_preserves_active_settings(self):
        app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        app.root = MagicMock()
        app.font_family_var = MagicMock()
        app.font_family_var.get.return_value = "旧字体"
        app.autostart_var = MagicMock()
        app.autostart_var.get.return_value = False
        app._font_choices = lambda: []
        app.settings = {"font_family": "旧字体", "custom_font_dir": "", "close_action": "ask"}
        before = app.settings.copy()
        app.store = MagicMock()
        app.store.save_settings.side_effect = OSError("磁盘已满")
        app._rebuild_ui = MagicMock()
        result = {"font_family": "新字体", "custom_font_dir": "", "close_action": "exit", "autostart_enabled": False}
        with patch.object(app_module, "SettingsDialog", return_value=SimpleNamespace(result=result)), \
             patch.object(app_module, "show_error") as error:
            app.open_settings()
        self.assertEqual(app.settings, before)
        app.font_family_var.set.assert_not_called()
        app._rebuild_ui.assert_not_called()
        error.assert_called_once()

    def test_failed_remember_close_keeps_window_and_old_choice(self):
        app = app_module.NoticeApp.__new__(app_module.NoticeApp)
        app.root = MagicMock()
        app.font_family_var = MagicMock()
        app.font_family_var.get.return_value = "旧字体"
        app.settings = {"font_family": "旧字体", "close_action": "ask"}
        app.store = MagicMock()
        app.store.save_settings.side_effect = OSError("磁盘已满")
        app.hide_window = MagicMock()
        app.exit_app = MagicMock()
        with patch.object(app_module, "CloseChoiceDialog", return_value=SimpleNamespace(result={"action": "exit", "remember": True})), \
             patch.object(app_module, "show_error") as error:
            app.on_close_requested()
        self.assertEqual(app.settings["close_action"], "ask")
        app.hide_window.assert_not_called()
        app.exit_app.assert_not_called()
        error.assert_called_once()

    def test_delayed_once_job_still_executes_after_grace_window(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        finished = Event()
        scheduler._scheduler.add_listener(lambda _event: finished.set(), EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        when = datetime.now() + timedelta(hours=1)
        task = Task(id="delayed-once", trigger_type="once",
                    trigger_config={"date": when.date().isoformat(), "time": when.time().isoformat()})
        scheduler.upsert_task(task)
        scheduler._scheduler.modify_job(task.id, next_run_time=datetime.now(scheduler._scheduler.timezone) - timedelta(seconds=35))
        self.assertTrue(finished.wait(3), "已排程单次任务不应因暂停或休眠超过 30 秒而丢失")
        scheduler.notifier.send.assert_called_once_with(task)
        self.assertFalse(task.enabled)

    def test_delayed_snooze_still_executes_without_changing_task(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        finished = Event()
        scheduler._scheduler.add_listener(lambda _event: finished.set(), EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        task = Task(id="delayed-snooze", enabled=False)
        scheduler.upsert_task(task)
        scheduler.snooze_task(task.id)
        scheduler._scheduler.modify_job(scheduler._snooze_job_id(task.id),
                                        next_run_time=datetime.now(scheduler._scheduler.timezone) - timedelta(seconds=35))
        self.assertTrue(finished.wait(3))
        scheduler.notifier.send.assert_called_once_with(task)
        self.assertFalse(task.enabled)

    def test_periodic_jobs_keep_bounded_misfire_policy(self):
        scheduler = TaskScheduler(MagicMock())
        scheduler.start()
        self.addCleanup(scheduler._scheduler.shutdown, wait=True)
        task = Task(id="periodic-grace", trigger_type="interval", trigger_config={"hours": 1})
        scheduler.upsert_task(task)
        self.assertEqual(scheduler._scheduler.get_job(task.id).misfire_grace_time, 30)

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

    def test_interval_rejects_fractional_numbers_and_boolean_values(self):
        for config in ({"hours": 1, "minutes": -0.5}, {"minutes": 1.9}, {"seconds": True}):
            with self.subTest(config=config):
                task = Task(trigger_type="interval", trigger_config=config)
                with self.assertRaises(ValueError):
                    task.validate()
                with self.assertRaises(ValueError):
                    TaskScheduler(MagicMock())._build_trigger(task)
                loaded = Task.from_dict(task.to_dict(), strict=False)
                self.assertFalse(loaded.enabled)
                self.assertEqual(loaded.trigger_config, config)
                self.assertIsNotNone(loaded.load_warning)

    def test_interval_keeps_integer_and_integer_string_compatibility(self):
        for value in (2, "2", 2.0):
            with self.subTest(value=value):
                task = Task(trigger_type="interval", trigger_config={"minutes": value})
                task.validate()
                trigger = TaskScheduler(MagicMock())._build_trigger(task)
                self.assertEqual(trigger.interval, timedelta(minutes=2))

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
                  patch.object(notifier_module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as send:
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
                    send.assert_called_once()
                finally:
                    scheduler._scheduler.shutdown(wait=True)

    def _load_records(self, records: list[object]) -> tuple[list[Task], str, list[object]]:
        """按启动时的真实路径读一份 tasks.json，并检查日志中的降级报告。"""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "tasks.json"
        path.write_text(json.dumps({"tasks": records}, ensure_ascii=False), encoding="utf-8")
        saved = json.loads(path.read_text(encoding="utf-8"))["tasks"]
        output = io.StringIO()
        logger = logging.Logger("test-store")
        logger.addHandler(logging.StreamHandler(output))
        with patch("store._LOGGER", logger):
            tasks = TaskStore(path).load_tasks()
        return tasks, output.getvalue(), saved

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
        # 降级不得静默：日志要能看出是哪一条、因为什么被降级。
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
