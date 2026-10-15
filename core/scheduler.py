from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from chinese_calendar import is_workday

from core.models import Task
from core.notifier import Notifier


class TaskScheduler:
    def __init__(self, notifier: Notifier, on_change: Callable[[], None] | None = None) -> None:
        self.notifier = notifier
        self.on_change = on_change
        self._scheduler = BackgroundScheduler()
        self._lock = RLock()
        self._tasks: dict[str, Task] = {}
        self._paused = False

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def sync_tasks(self, tasks: list[Task]) -> None:
        with self._lock:
            self._scheduler.remove_all_jobs()
            self._tasks = {task.id: task for task in tasks}
            for task in tasks:
                self._schedule_task(task)
            self._notify_changed()

    def upsert_task(self, task: Task) -> None:
        with self._lock:
            self._tasks[task.id] = task
            self._remove_job(task.id)
            self._schedule_task(task)
            self._notify_changed()

    def remove_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
            self._remove_job(task_id)
            self._notify_changed()

    def pause_all(self) -> None:
        self._paused = True
        self._scheduler.pause()
        self._refresh_next_runs()
        self._notify_changed()

    def resume_all(self) -> None:
        self._paused = False
        self._scheduler.resume()
        self._refresh_next_runs()
        self._notify_changed()

    @property
    def paused(self) -> bool:
        return self._paused

    def task_next_run(self, task_id: str) -> str | None:
        job = self._scheduler.get_job(task_id)
        if not job or not job.next_run_time:
            return None
        return job.next_run_time.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def _remove_job(self, task_id: str) -> None:
        if self._scheduler.get_job(task_id):
            self._scheduler.remove_job(task_id)

    def _schedule_task(self, task: Task) -> None:
        if not task.enabled:
            task.next_run = None
            return
        if task.trigger_type == "once":
            run_at = datetime.fromisoformat(f"{task.trigger_config['date']}T{task.trigger_config['time']}")
            if run_at <= datetime.now(run_at.tzinfo):
                task.enabled = False
                task.next_run = None
                return
        trigger = self._build_trigger(task)
        self._scheduler.add_job(
            self._run_task,
            trigger=trigger,
            id=task.id,
            args=[task.id],
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=30,
        )
        task.next_run = self.task_next_run(task.id)

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return
        self.notifier.send(task)
        if task.trigger_type == "weekly_last_workday" and task.enabled:
            with self._lock:
                self._remove_job(task.id)
                self._schedule_task(task)
        else:
            task.next_run = self.task_next_run(task_id)
        self._notify_changed()

    def _build_trigger(self, task: Task):
        config = task.trigger_config
        if task.trigger_type == "once":
            run_at = datetime.fromisoformat(f"{config['date']}T{config['time']}")
            return DateTrigger(run_date=run_at)
        if task.trigger_type == "interval":
            return IntervalTrigger(
                weeks=int(config.get("weeks", 0)),
                days=int(config.get("days", 0)),
                hours=int(config.get("hours", 0)),
                minutes=int(config.get("minutes", 0)),
                seconds=int(config.get("seconds", 0)),
            )
        if task.trigger_type == "weekly":
            weekdays = ",".join(str(day) for day in config.get("weekdays", []))
            return CronTrigger(
                day_of_week=weekdays,
                hour=int(config.get("hour", 0)),
                minute=int(config.get("minute", 0)),
                second=int(config.get("second", 0)),
            )
        if task.trigger_type == "weekly_last_workday":
            run_at = self._next_weekly_last_workday(
                hour=int(config.get("hour", 18)),
                minute=int(config.get("minute", 0)),
                second=int(config.get("second", 0)),
            )
            return DateTrigger(run_date=run_at)
        if task.trigger_type == "yearly":
            return CronTrigger(
                month=int(config["month"]),
                day=int(config["day"]),
                hour=int(config.get("hour", 0)),
                minute=int(config.get("minute", 0)),
                second=int(config.get("second", 0)),
            )
        if task.trigger_type == "cron":
            return CronTrigger.from_crontab(config["expression"])
        raise ValueError(f"Unsupported trigger type: {task.trigger_type}")

    def _next_weekly_last_workday(self, hour: int, minute: int, second: int) -> datetime:
        now = datetime.now()
        candidate = self._last_workday_in_week(now, hour, minute, second)
        if candidate <= now:
            next_week_anchor = datetime.fromordinal(now.date().toordinal() + 7)
            candidate = self._last_workday_in_week(next_week_anchor, hour, minute, second)
        return candidate

    def _last_workday_in_week(self, reference: datetime, hour: int, minute: int, second: int) -> datetime:
        start = reference.date().fromordinal(reference.date().toordinal() - reference.weekday())
        streaks: list[list[datetime]] = []
        current_streak: list[datetime] = []
        for offset in range(7):
            current_date = start.fromordinal(start.toordinal() + offset)
            try:
                if is_workday(current_date):
                    current_streak.append(datetime.combine(current_date, datetime.min.time()).replace(
                        hour=hour,
                        minute=minute,
                        second=second,
                        microsecond=0,
                    ))
                elif current_streak:
                    streaks.append(current_streak)
                    current_streak = []
            except NotImplementedError as exc:
                raise ValueError(
                    f"法定节假日数据暂不支持 {current_date.year} 年，请更新节假日日历后再使用该触发方式。"
                ) from exc
        if current_streak:
            streaks.append(current_streak)
        if not streaks:
            raise ValueError(f"{start.isoformat()} 所在周没有可用的法定工作日。")
        best_streak = max(streaks, key=lambda streak: (len(streak), streak[-1]))
        return best_streak[-1]

    def _refresh_next_runs(self) -> None:
        for task_id, task in self._tasks.items():
            task.next_run = None if self._paused else self.task_next_run(task_id)

    def _notify_changed(self) -> None:
        if self.on_change:
            self.on_change()
