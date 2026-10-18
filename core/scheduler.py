from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import RLock
from typing import Callable, Iterator

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_PAUSED
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from chinese_calendar import is_workday

from core.app_logging import get_logger
from core.models import Task, validate_notification_text
from core.notifier import Notifier

_LOGGER = get_logger(__name__)

# 稍后提醒用的独立 job 前缀：补发的这一次通知不能占用提醒自己的调度槽位。
SNOOZE_JOB_PREFIX = "snooze:"
DEFAULT_SNOOZE_MINUTES = 10
NOTIFICATION_RETRY_SECONDS = 60


class _DispatchSafeScheduler(BackgroundScheduler):
    def __init__(self) -> None:
        self._dispatch_lock = RLock()
        super().__init__()

    def _process_jobs(self):
        with self._dispatch_lock:
            return super()._process_jobs()

    def shutdown(self, wait=True):
        # APScheduler 在停止时先切换存储模式；必须等当前派发结束，避免移除单次 job 报错。
        with self._dispatch_lock:
            BlockingScheduler.shutdown(self, wait)
        # 唤醒后的调度线程仍会进入 _process_jobs，不能持有派发锁等待它退出。
        self._thread.join()
        del self._thread


class TaskScheduler:
    def __init__(self, notifier: Notifier, on_change: Callable[[], None] | None = None) -> None:
        self.notifier = notifier
        self.on_change = on_change
        self._scheduler = _DispatchSafeScheduler()
        self._lock = RLock()
        self._tasks: dict[str, Task] = {}
        self._snooze_requests: dict[str, object] = {}
        self._paused = False
        self._stopping = False

    def start(self) -> None:
        self._stopping = False
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        with self._lock:
            self._stopping = True
            self._snooze_requests.clear()
        # 不持有任务锁等待调度器退出，避免与尚未结束的发送线程互相等待。
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def sync_tasks(self, tasks: list[Task]) -> None:
        with self._lock:
            self._scheduler.remove_all_jobs()
            self._snooze_requests.clear()
            self._tasks = {task.id: task for task in tasks}
            for task in tasks:
                try:
                    self._schedule_task(task)
                except (ValueError, TypeError, KeyError) as exc:
                    # 启动时整批载入：某条提醒的触发配置没法排程（例如按周的小时写成 25）时
                    # 只停用这一条并留痕，同批其它提醒照常注册，程序也照常打得开。
                    task.enabled = False
                    task.next_run = None
                    _LOGGER.warning("[提醒调度] 提醒“%s”的触发配置无法排程，已停用：%s", task.title, exc)
            self._notify_changed()

    def upsert_task(self, task: Task, *, notify: bool = True) -> None:
        with self._lock:
            # 先验证并安装新 job，失败时不触碰原任务和原调度。
            self._schedule_task(task)
            self._tasks[task.id] = task
            if notify:
                self._notify_changed()

    @contextmanager
    def task_transaction(self, task_id: str) -> Iterator[None]:
        # 持久化失败时恢复原 job，而不是重新计算周期；稍后提醒也要跟随删除回滚。
        # 同时锁住 job store，避免回滚窗口内新 job 已被调度线程取走执行。
        with self._lock, self._scheduler._jobstores_lock:
            previous = self._tasks.get(task_id)
            request = self._snooze_requests.get(task_id)
            state = (previous.enabled, previous.next_run) if previous else None
            jobs = {key: self._scheduler.get_job(key)
                    for key in (task_id, self._snooze_job_id(task_id))}
            try:
                yield
            except Exception:
                if request is None:
                    self._snooze_requests.pop(task_id, None)
                else:
                    self._snooze_requests[task_id] = request
                if previous is None:
                    self._tasks.pop(task_id, None)
                else:
                    self._tasks[task_id] = previous
                    previous.enabled, previous.next_run = state
                for key, job in jobs.items():
                    current = self._scheduler.get_job(key)
                    if current is job:
                        continue
                    if job is None:
                        self._remove_job(key)
                    else:
                        self._scheduler.add_job(
                            job.func, trigger=job.trigger, id=job.id, args=job.args,
                            kwargs=job.kwargs, executor=job.executor,
                            next_run_time=job.next_run_time, name=job.name,
                            misfire_grace_time=job.misfire_grace_time,
                            coalesce=job.coalesce, max_instances=job.max_instances,
                            replace_existing=True,
                        )
                raise

    def remove_task(self, task_id: str, *, notify: bool = True) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
            self._snooze_requests.pop(task_id, None)
            self._remove_job(task_id)
            # 连挂起的「稍后提醒」一起撤掉，否则删掉提醒后还会再弹一次。
            self._remove_job(self._snooze_job_id(task_id))
            if notify:
                self._notify_changed()

    def pause_all(self) -> None:
        with self._lock:
            try:
                self._scheduler.pause()
            finally:
                # API 或后续回调失败时也按真实调度状态同步，不预先宣布暂停成功。
                self._paused = self._scheduler.state == STATE_PAUSED
                self._refresh_next_runs()
            self._notify_changed()

    def resume_all(self) -> None:
        with self._lock:
            try:
                self._scheduler.resume()
            finally:
                self._paused = self._scheduler.state == STATE_PAUSED
                self._refresh_next_runs()
            self._notify_changed()

    def _defer_paused_job(self, task: Task, *, snooze: bool = False, request: object | None = None) -> None:
        if not self._paused or self._stopping:
            return
        job_id = self._snooze_job_id(task.id) if snooze else task.id
        # 等调度线程完成取走单次 job，再放回暂停中的队列；不覆盖编辑或新安排的 job。
        with self._scheduler._jobstores_lock:
            if self._scheduler.get_job(job_id) is None:
                self._scheduler.add_job(
                    self._fire_snooze if snooze else self._run_task,
                    DateTrigger(run_date=datetime.now()), id=job_id, args=[task.id, request] if snooze else [task.id, task],
                    # 恢复可能先于旧 worker 收尾，允许这次不发送的旧调用与补发调用短暂重叠。
                    replace_existing=True, misfire_grace_time=None, max_instances=2,
                )
        if not snooze:
            task.next_run = None

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
            # 停用中的提醒不排程、也不校验配置：读取时降级出来的坏记录要能被原样保存，
            # 用户改好并重新启用时才会走到下面的严格校验。
            self._remove_job(task.id)
            task.next_run = None
            return
        task.validate()
        if task.trigger_type == "once":
            run_at = datetime.fromisoformat(f"{task.trigger_config['date']}T{task.trigger_config['time']}")
            if run_at <= datetime.now(run_at.tzinfo):
                self._remove_job(task.id)
                task.enabled = False
                task.next_run = None
                return
        trigger = self._build_trigger(task)
        self._scheduler.add_job(
            self._run_task,
            trigger=trigger,
            id=task.id,
            args=[task.id, task],
            replace_existing=True,
            coalesce=True,
            # 已排好的单次 job 在休眠/暂停恢复后仍需执行；周期任务只合并近期错过的触发。
            misfire_grace_time=None if isinstance(trigger, DateTrigger) else 30,
        )
        task.next_run = self.task_next_run(task.id)

    def _notify(self, task_id: str, *, expected: Task | None = None) -> Task | None:
        """定时触发路径：停用中的提醒不发通知，返回实际通知的任务。

        只读任务、不推进任何调度状态，所以 _run_task 可以据此把「发通知」和
        「推进任务状态」分开；稍后提醒的补发走 _fire_snooze，判据不同，不能共用这里。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or not task.enabled or self._paused or self._stopping or (expected is not None and task is not expected):
                return None
        self.notifier.send(task)
        return task

    def _fire_snooze(self, task_id: str, request: object | None = None) -> None:
        """稍后提醒的补发路径：用户明确要求再提醒一次，所以不要求提醒仍处于启用状态。

        单次提醒触发后会自动停用，而那一刻恰恰最可能被点「稍后再说」；
        这里若沿用 _notify 的启用判断，这条补发就永远不会发出来。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or self._stopping:
                return
            if request is None:
                request = self._snooze_requests.setdefault(task_id, object())
            if self._snooze_requests.get(task_id) is not request:
                return
            if self._paused:
                self._defer_paused_job(task, snooze=True, request=request)
                return
        try:
            self.notifier.send(task)
        except Exception:
            _LOGGER.exception("提醒“%s”的稍后补发失败", task.title)
            with self._lock, self._scheduler._jobstores_lock:
                job_id = self._snooze_job_id(task_id)
                if (not self._stopping and self._tasks.get(task_id) is task
                        and self._snooze_requests.get(task_id) is request and self._scheduler.get_job(job_id) is None):
                    self._scheduler.add_job(
                        self._fire_snooze,
                        DateTrigger(run_date=datetime.now() + timedelta(seconds=NOTIFICATION_RETRY_SECONDS)),
                        id=job_id, args=[task_id, request], replace_existing=True, misfire_grace_time=None,
                    )
            raise
        else:
            with self._lock:
                if self._snooze_requests.get(task_id) is request:
                    self._snooze_requests.pop(task_id, None)

    def _run_task(self, task_id: str, expected: Task | None = None) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or not task.enabled or self._stopping:
                return
            # 已排队的旧 job 不得按 ID 取走编辑后的新配置，提前发送并消耗新单次提醒。
            if expected is not None and task is not expected:
                return
            if self._paused:
                if task.trigger_type in {"once", "weekly_last_workday"}:
                    self._defer_paused_job(task)
                return
        failure = None
        try:
            # 锁内已确定发送边界；暂停后恢复的二次检查不能吞掉已取走的单次 job。
            self.notifier.send(task)
        except Exception as exc:
            failure = exc
            _LOGGER.error("提醒“%s”发送失败：%s", task.title, exc)
        with self._lock:
            # 发送期间用户可能已编辑或删除提醒，不覆盖新任务的状态与调度。
            if self._stopping or self._tasks.get(task_id) is not task:
                return
            if task.trigger_type == "once":
                if failure is None:
                    task.enabled = False
                    task.next_run = None
                else:
                    # 单次 job 已被调度器取走；临时发送失败要重试，不能假装已提醒并停用。
                    self._scheduler.add_job(
                        self._run_task, DateTrigger(run_date=datetime.now() + timedelta(seconds=NOTIFICATION_RETRY_SECONDS)),
                        id=task.id, args=[task.id, task], replace_existing=True, misfire_grace_time=None,
                    )
                    task.next_run = self.task_next_run(task.id)
            elif task.trigger_type == "weekly_last_workday" and task.enabled:
                self._schedule_task(task)
            else:
                task.next_run = None if self._paused else self.task_next_run(task_id)
        self._notify_changed()
        if failure is not None:
            raise failure

    def _snooze_job_id(self, task_id: str) -> str:
        return f"{SNOOZE_JOB_PREFIX}{task_id}"

    def snooze_task(self, task_id: str, *, minutes: int = DEFAULT_SNOOZE_MINUTES) -> bool:
        """稍后再提醒一次：到点只补发一条通知，不改动提醒自身的周期与启用状态。

        任务不存在时返回 False，让界面能如实说明「这条提醒已经不在了」。
        """
        with self._lock:
            if self._stopping or task_id not in self._tasks:
                return False
            task = self._tasks[task_id]
            validate_notification_text(task.title, task.body)
            request = object()
            self._scheduler.add_job(
                self._fire_snooze,
                trigger=DateTrigger(run_date=datetime.now() + timedelta(minutes=minutes)),
                id=self._snooze_job_id(task_id),
                args=[task_id, request],
                replace_existing=True,
                misfire_grace_time=None,
            )
            self._snooze_requests[task_id] = request
        return True

    def _build_trigger(self, task: Task):
        task.validate()
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
        reference = now
        # 整周放假没有候选日，跳过该周；仍保留最长连续工作段的业务规则。
        for _ in range(53):
            candidate = self._last_workday_in_week(reference, hour, minute, second)
            if candidate is not None and candidate > now:
                return candidate
            reference += timedelta(days=7)
        raise ValueError("未来一年内没有可用的法定工作日。")

    def _last_workday_in_week(self, reference: datetime, hour: int, minute: int, second: int) -> datetime | None:
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
            return None
        best_streak = max(streaks, key=lambda streak: (len(streak), streak[-1]))
        return best_streak[-1]

    def _refresh_next_runs(self) -> None:
        for task_id, task in self._tasks.items():
            task.next_run = None if self._paused else self.task_next_run(task_id)

    def _notify_changed(self) -> None:
        if self.on_change and not self._stopping:
            self.on_change()
