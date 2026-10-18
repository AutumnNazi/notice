from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import ttk

from apscheduler.triggers.cron import CronTrigger

from config import TRIGGER_TYPES, WEEKDAY_LABELS
from core.models import Task, validate_interval_config
from gui.message_dialog import show_error
from gui.theme import (
    PALETTE,
    PillButton,
    RoundedEntry,
    RoundedFrame,
    RoundedSelect,
    RoundedText,
    apply_ttk_theme,
    center_to_parent,
    enable_window_drag,
    font,
    scale,
)

TRIGGER_LABEL_TO_KEY = {label: key for key, label in TRIGGER_TYPES.items()}
TRIGGER_KEY_TO_LABEL = TRIGGER_TYPES.copy()


class TaskDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, task: Task | None = None) -> None:
        super().__init__(master)
        self.withdraw()
        self.title("编辑提醒" if task else "新建提醒")
        self.resizable(False, False)
        self.transient(master)
        self.overrideredirect(True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.configure(bg=PALETTE.card_bg)

        self.result: Task | None = None
        self._task = task
        self.ui_family = getattr(master, "font_family_var", None).get() if hasattr(master, "font_family_var") else "Microsoft YaHei UI"
        self.ui_scale = float(getattr(master, "ui_scale", 1.0))
        apply_ttk_theme(ttk.Style(self), self.ui_family, self.ui_scale)

        self.title_var = tk.StringVar(value=task.title if task else "")
        self.body_var = tk.StringVar(value=task.body if task else "")
        self.trigger_type_var = tk.StringVar(value=task.trigger_type if task else "once")
        self.trigger_label_var = tk.StringVar(value=TRIGGER_KEY_TO_LABEL[self.trigger_type_var.get()])
        self.enabled_var = tk.BooleanVar(value=True if task is None else task.enabled)

        self.once_date_var = tk.StringVar(value=self._cfg("date", datetime.now().strftime("%Y-%m-%d")))
        self.once_time_var = tk.StringVar(value=self._cfg("time", datetime.now().strftime("%H:%M:%S")))
        self.interval_weeks_var = tk.StringVar(value=str(self._cfg("weeks", 0)))
        self.interval_days_var = tk.StringVar(value=str(self._cfg("days", 0)))
        self.interval_hours_var = tk.StringVar(value=str(self._cfg("hours", 0)))
        self.interval_minutes_var = tk.StringVar(value=str(self._cfg("minutes", 30 if task is None else 0)))
        self.interval_seconds_var = tk.StringVar(value=str(self._cfg("seconds", 0)))
        self.weekly_hour_var = tk.StringVar(value=str(self._cfg("hour", 9)))
        self.weekly_minute_var = tk.StringVar(value=str(self._cfg("minute", 0)))
        self.weekly_second_var = tk.StringVar(value=str(self._cfg("second", 0)))
        self.weekly_last_workday_hour_var = tk.StringVar(value=str(self._cfg("hour", 18)))
        self.weekly_last_workday_minute_var = tk.StringVar(value=str(self._cfg("minute", 0)))
        self.weekly_last_workday_second_var = tk.StringVar(value=str(self._cfg("second", 0)))
        self.yearly_month_var = tk.StringVar(value=str(self._cfg("month", 1)))
        self.yearly_day_var = tk.StringVar(value=str(self._cfg("day", 1)))
        self.yearly_hour_var = tk.StringVar(value=str(self._cfg("hour", 0)))
        self.yearly_minute_var = tk.StringVar(value=str(self._cfg("minute", 0)))
        self.yearly_second_var = tk.StringVar(value=str(self._cfg("second", 0)))
        self.cron_var = tk.StringVar(value=self._cfg("expression", "0 9 * * 1-5"))
        # 脏星期字段仍要能打开编辑器；只展示合法索引，不改写任务原始配置。
        configured_weekdays = self._cfg("weekdays", [0])
        weekdays = {day for day in configured_weekdays if type(day) is int and 0 <= day <= 6} \
            if isinstance(configured_weekdays, list) else set()
        self.weekday_vars = {
            value: tk.BooleanVar(value=value in weekdays)
            for _, value in WEEKDAY_LABELS
        }
        self.enabled_indicator: tk.Label | None = None
        self.weekday_chips: dict[int, tk.Label] = {}

        self.trigger_frame: tk.Frame | None = None
        self.title_entry: RoundedEntry | None = None
        self.body_text: RoundedText | None = None

        self._build()
        self._render_trigger_fields()
        center_to_parent(self, self.master if isinstance(self.master, tk.Misc) else None)
        self.deiconify()
        self._raise_dialog()
        self.grab_set()
        self.focus_force()
        self._focus_initial_input()
        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self.destroy())

    def _cfg(self, key: str, default):
        if not self._task:
            return default
        return self._task.trigger_config.get(key, default)

    def _build(self) -> None:
        shell = tk.Frame(self, bg=PALETTE.card_bg, padx=0, pady=0)
        shell.pack(fill=tk.BOTH, expand=True)

        card_shell = RoundedFrame(
            shell,
            radius=scale(14, self.ui_scale),
            padding=scale(26, self.ui_scale),
            fill=PALETTE.card_bg,
            outline=PALETTE.border,
            shadow=False,
            bg=PALETTE.card_bg,
            width=scale(420, self.ui_scale),
            height=scale(650, self.ui_scale),
        )
        card_shell.pack(fill=tk.BOTH, expand=True)
        card = card_shell.content
        card.grid_columnconfigure(1, weight=1)
        enable_window_drag(self, shell, card_shell, card)

        tk.Label(
            card,
            text="编辑提醒" if self._task else "新建提醒",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.ui_family, 13, self.ui_scale, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            card,
            text="×",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.ui_family, 13, self.ui_scale),
            cursor="hand2",
            padx=scale(8, self.ui_scale),
        ).grid(row=0, column=1, sticky="e")
        card.grid_slaves(row=0, column=1)[0].bind("<Button-1>", lambda _event: self.destroy())

        self._form_label(card, 1, "标题", top=18)
        self.title_entry = RoundedEntry(
            card,
            textvariable=self.title_var,
            width=scale(200, self.ui_scale),
            height=scale(36, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            bg=PALETTE.card_bg,
        )
        self.title_entry.grid(row=1, column=1, sticky="ew", pady=(scale(18, self.ui_scale), scale(8, self.ui_scale)))

        self._form_label(card, 2, "触发方式")
        RoundedSelect(
            card,
            variable=self.trigger_label_var,
            values=list(TRIGGER_TYPES.values()),
            width=scale(200, self.ui_scale),
            height=scale(36, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            command=lambda _value: self._on_trigger_change(),
            bg=PALETTE.card_bg,
        ).grid(row=2, column=1, sticky="ew", pady=(0, scale(8, self.ui_scale)))

        self.trigger_frame = tk.Frame(card, bg=PALETTE.card_bg)
        self.trigger_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(scale(2, self.ui_scale), scale(8, self.ui_scale)))
        self.trigger_frame.grid_columnconfigure(1, weight=1)

        self._form_label(card, 4, "提醒内容")
        self.body_text = RoundedText(
            card,
            width=scale(240, self.ui_scale),
            height=scale(110, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            bg=PALETTE.card_bg,
        )
        self.body_text.set_text(self.body_var.get())
        self.body_text.grid(row=4, column=1, sticky="ew", pady=(scale(4, self.ui_scale), scale(10, self.ui_scale)))

        self._check_row(card, 5, "启用这条提醒", self.enabled_var).grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(scale(4, self.ui_scale), scale(16, self.ui_scale)),
        )

        actions = tk.Frame(card, bg=PALETTE.card_bg)
        actions.grid(row=6, column=0, columnspan=2, sticky="ew")
        PillButton(
            actions,
            "取消",
            self.destroy,
            width=scale(92, self.ui_scale),
            height=scale(34, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            radius=scale(9, self.ui_scale),
            bg=PALETTE.card_bg,
        ).pack(side=tk.LEFT)
        PillButton(
            actions,
            "保存",
            self._save,
            width=scale(92, self.ui_scale),
            height=scale(34, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            fill=PALETTE.accent_soft,
            active_fill="#e5d5c0",
            radius=scale(9, self.ui_scale),
            bg=PALETTE.card_bg,
        ).pack(side=tk.RIGHT)

    def _form_label(self, parent: tk.Misc, row: int, text: str, *, top: int = 0) -> None:
        tk.Label(
            parent,
            text=text,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.ui_family, 10, self.ui_scale),
        ).grid(
            row=row,
            column=0,
            sticky="nw",
            pady=(scale(top, self.ui_scale), scale(8, self.ui_scale)),
            padx=(0, scale(14, self.ui_scale)),
        )

    def _focus_initial_input(self) -> None:
        if self.title_entry is None:
            return
        self.title_entry.focus_set()
        self.title_entry.icursor(tk.END)

    def _on_trigger_change(self, _event=None) -> None:
        self.trigger_type_var.set(TRIGGER_LABEL_TO_KEY[self.trigger_label_var.get()])
        self._render_trigger_fields()

    def _render_trigger_fields(self) -> None:
        if self.trigger_frame is None:
            return
        for widget in self.trigger_frame.winfo_children():
            widget.destroy()

        trigger_type = self.trigger_type_var.get()
        if trigger_type == "once":
            self._row(self.trigger_frame, 0, "日期", self.once_date_var, "例如 2026-06-20")
            self._row(self.trigger_frame, 1, "时间", self.once_time_var, "例如 09:00:00")
        elif trigger_type == "interval":
            self._row(self.trigger_frame, 0, "间隔周数", self.interval_weeks_var, "不用就填 0")
            self._row(self.trigger_frame, 1, "间隔天数", self.interval_days_var, "不用就填 0")
            self._row(self.trigger_frame, 2, "间隔小时", self.interval_hours_var, "不用就填 0")
            self._row(self.trigger_frame, 3, "间隔分钟", self.interval_minutes_var, "例如 30")
            self._row(self.trigger_frame, 4, "间隔秒数", self.interval_seconds_var, "不用就填 0")
        elif trigger_type == "weekly":
            tk.Label(
                self.trigger_frame,
                text="星期",
                bg=PALETTE.card_bg,
                fg=PALETTE.text_secondary,
                font=font(self.ui_family, 10, self.ui_scale),
            ).grid(row=0, column=0, sticky="nw", pady=(0, scale(8, self.ui_scale)))
            weekday_frame = tk.Frame(self.trigger_frame, bg=PALETTE.card_bg)
            weekday_frame.grid(row=0, column=1, sticky="w", pady=(0, scale(8, self.ui_scale)))
            self.weekday_chips.clear()
            for index, (label, value) in enumerate(WEEKDAY_LABELS):
                self._weekday_chip(weekday_frame, label, value).grid(
                    row=index // 4,
                    column=index % 4,
                    sticky="w",
                    padx=(0, scale(8, self.ui_scale)),
                    pady=(0, scale(8, self.ui_scale)),
                )
            self._row(self.trigger_frame, 1, "小时", self.weekly_hour_var, "0 到 23")
            self._row(self.trigger_frame, 2, "分钟", self.weekly_minute_var, "0 到 59")
            self._row(self.trigger_frame, 3, "秒", self.weekly_second_var, "通常填 0")
        elif trigger_type == "weekly_last_workday":
            self._hint("按连续工作日自动判断每周最后一天。")
            self._row(self.trigger_frame, 1, "小时", self.weekly_last_workday_hour_var, "0 到 23")
            self._row(self.trigger_frame, 2, "分钟", self.weekly_last_workday_minute_var, "0 到 59")
            self._row(self.trigger_frame, 3, "秒", self.weekly_last_workday_second_var, "通常填 0")
        elif trigger_type == "yearly":
            self._row(self.trigger_frame, 0, "月份", self.yearly_month_var, "1 到 12")
            self._row(self.trigger_frame, 1, "日期", self.yearly_day_var, "1 到 31")
            self._row(self.trigger_frame, 2, "小时", self.yearly_hour_var, "0 到 23")
            self._row(self.trigger_frame, 3, "分钟", self.yearly_minute_var, "0 到 59")
            self._row(self.trigger_frame, 4, "秒", self.yearly_second_var, "通常填 0")
        elif trigger_type == "cron":
            self._row(self.trigger_frame, 0, "Cron", self.cron_var, "例如 0 9 * * 1-5")

    def _hint(self, text: str) -> None:
        if self.trigger_frame is None:
            return
        tk.Label(
            self.trigger_frame,
            text=text,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.ui_family, 9, self.ui_scale),
            justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, scale(10, self.ui_scale)))

    def _row(self, parent: tk.Misc, row: int, label: str, variable: tk.StringVar, hint: str | None = None) -> None:
        tk.Label(
            parent,
            text=label,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.ui_family, 10, self.ui_scale),
        ).grid(row=row, column=0, sticky="w", pady=(0, scale(8, self.ui_scale)), padx=(0, scale(12, self.ui_scale)))
        RoundedEntry(
            parent,
            textvariable=variable,
            width=scale(180, self.ui_scale),
            height=scale(34, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            bg=PALETTE.card_bg,
        ).grid(row=row, column=1, sticky="ew", pady=(0, scale(8, self.ui_scale)))
        if hint:
            tk.Label(
                parent,
                text=hint,
                bg=PALETTE.card_bg,
                fg=PALETTE.text_muted,
                font=font(self.ui_family, 9, self.ui_scale),
            ).grid(row=row, column=2, sticky="w", padx=(scale(10, self.ui_scale), 0), pady=(0, scale(8, self.ui_scale)))

    def _save(self) -> None:
        try:
            trigger_type = self.trigger_type_var.get()
            trigger_config = self._collect_trigger_config(trigger_type)
            if self.body_text is not None:
                self.body_var.set(self.body_text.get_text())
            if not self.title_var.get().strip() and not self.body_var.get().strip():
                raise ValueError("标题和内容不能同时为空。")
            task = Task(
                id=self._task.id if self._task else Task().id,
                title=self.title_var.get().strip(),
                body=self.body_var.get().strip(),
                trigger_type=trigger_type,
                trigger_config=trigger_config,
                enabled=self.enabled_var.get(),
                sound=False,
                priority=self._task.priority if self._task else "normal",
                created_at=self._task.created_at if self._task else Task().created_at,
                next_run=self._task.next_run if self._task else None,
            )
            task.validate()
        except Exception as exc:
            show_error(self, "保存失败", str(exc))
            return

        self.result = task
        self.destroy()

    def _collect_trigger_config(self, trigger_type: str) -> dict[str, object]:
        if trigger_type == "once":
            run_at = datetime.fromisoformat(f"{self.once_date_var.get()}T{self.once_time_var.get()}")
            if run_at <= datetime.now(run_at.tzinfo):
                raise ValueError("单次提醒时间必须晚于当前时间。")
            return {"date": self.once_date_var.get(), "time": self.once_time_var.get()}
        if trigger_type == "interval":
            values = {
                "weeks": int(self.interval_weeks_var.get() or "0"),
                "days": int(self.interval_days_var.get() or "0"),
                "hours": int(self.interval_hours_var.get() or "0"),
                "minutes": int(self.interval_minutes_var.get() or "0"),
                "seconds": int(self.interval_seconds_var.get() or "0"),
            }
            return validate_interval_config(values)
        if trigger_type == "weekly":
            weekdays = [value for value, var in self.weekday_vars.items() if var.get()]
            if not weekdays:
                raise ValueError("请至少选择一个星期。")
            return {
                "weekdays": weekdays,
                "hour": int(self.weekly_hour_var.get() or "0"),
                "minute": int(self.weekly_minute_var.get() or "0"),
                "second": int(self.weekly_second_var.get() or "0"),
            }
        if trigger_type == "yearly":
            month = int(self.yearly_month_var.get())
            day = int(self.yearly_day_var.get())
            datetime(2000, month, day)
            return {
                "month": month,
                "day": day,
                "hour": int(self.yearly_hour_var.get() or "0"),
                "minute": int(self.yearly_minute_var.get() or "0"),
                "second": int(self.yearly_second_var.get() or "0"),
            }
        if trigger_type == "weekly_last_workday":
            return {
                "hour": int(self.weekly_last_workday_hour_var.get() or "0"),
                "minute": int(self.weekly_last_workday_minute_var.get() or "0"),
                "second": int(self.weekly_last_workday_second_var.get() or "0"),
            }
        expression = self.cron_var.get().strip()
        if len(expression.split()) != 5:
            raise ValueError("Cron 表达式需要 5 段。")
        CronTrigger.from_crontab(expression)
        return {"expression": expression}

    def _check_row(self, parent: tk.Misc, row: int, text: str, variable: tk.BooleanVar) -> tk.Frame:
        holder = tk.Frame(parent, bg=PALETTE.card_bg, cursor="hand2")
        indicator = tk.Label(
            holder,
            text="",
            width=2,
            bg=PALETTE.card_bg,
            fg=PALETTE.accent_text,
            highlightthickness=1,
            highlightbackground=PALETTE.border,
            font=font(self.ui_family, 9, self.ui_scale, "bold"),
            cursor="hand2",
        )
        indicator.pack(side=tk.LEFT)
        label = tk.Label(
            holder,
            text=text,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.ui_family, 10, self.ui_scale),
            padx=scale(10, self.ui_scale),
            cursor="hand2",
        )
        label.pack(side=tk.LEFT)
        self.enabled_indicator = indicator
        for widget in (holder, indicator, label):
            widget.bind("<Button-1>", lambda _event: self._toggle_enabled())
        self._refresh_enabled_indicator()
        return holder

    def _toggle_enabled(self) -> None:
        self.enabled_var.set(not self.enabled_var.get())
        self._refresh_enabled_indicator()

    def _refresh_enabled_indicator(self) -> None:
        if self.enabled_indicator is None:
            return
        checked = self.enabled_var.get()
        self.enabled_indicator.configure(
            text="✓" if checked else "",
            bg=PALETTE.accent_soft if checked else PALETTE.card_bg,
            highlightbackground=PALETTE.accent if checked else PALETTE.border,
        )

    def _weekday_chip(self, parent: tk.Misc, label_text: str, value: int) -> tk.Label:
        chip = tk.Label(
            parent,
            text=label_text,
            width=4,
            padx=scale(6, self.ui_scale),
            pady=scale(5, self.ui_scale),
            font=font(self.ui_family, 9, self.ui_scale),
            cursor="hand2",
            highlightthickness=1,
        )
        self.weekday_chips[value] = chip
        chip.bind("<Button-1>", lambda _event, weekday=value: self._toggle_weekday(weekday))
        self._refresh_weekday_chip(value)
        return chip

    def _toggle_weekday(self, value: int) -> None:
        self.weekday_vars[value].set(not self.weekday_vars[value].get())
        self._refresh_weekday_chip(value)

    def _refresh_weekday_chip(self, value: int) -> None:
        chip = self.weekday_chips.get(value)
        if chip is None:
            return
        checked = self.weekday_vars[value].get()
        chip.configure(
            bg=PALETTE.accent_soft if checked else PALETTE.card_bg,
            fg=PALETTE.accent_text if checked else PALETTE.text_secondary,
            highlightbackground=PALETTE.accent if checked else PALETTE.border_soft,
        )

    def _raise_dialog(self) -> None:
        try:
            self.lift(self.master)
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
