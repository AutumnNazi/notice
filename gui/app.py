from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import queue
import random
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageTk

from config import APP_NAME, BOAT_ART_FILE, FONT_DIR, ICON_FILE, ICON_ICO_FILE, ICON_PNG_FILES, TRIGGER_TYPES
from core.font_manager import register_font_directories
from core.icon_assets import ensure_icon_assets
from core.live_info import LUNAR_RETRY_SECONDS, WEATHER_REFRESH_SECONDS, WEATHER_RETRY_SECONDS, Weather, fetch_weather, lunar_date
from core.models import Task
from core.notifier import Notifier
from core.quotes import APP_QUOTES
from core.scheduler import DEFAULT_SNOOZE_MINUTES, TaskScheduler
from gui.close_dialog import CloseChoiceDialog
from gui.message_dialog import ask_yes_no, show_error, show_info
from gui.settings_dialog import SettingsDialog
from gui.task_dialog import TaskDialog
from gui.theme import PALETTE, PillButton, RoundedFrame, SlimScrollbar, apply_ttk_theme, elide_text, font, scale
from gui.tray import TrayController
from single_instance import stop_helper
from store import TaskStore


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SEARCH_PLACEHOLDER = "搜索标题、内容或周期..."


class NoticeApp:
    def __init__(self) -> None:
        self.store = TaskStore()
        self.settings = self.store.load_settings()
        register_font_directories(self._font_directories())

        self.root = tk.Tk()
        self.ui_scale = 1.0
        self._configure_display_metrics()
        self.root.ui_scale = self.ui_scale
        self.root.title(APP_NAME)
        self.root.configure(bg=PALETTE.window_bg)
        ensure_icon_assets()
        self.root.font_family_var = None
        self.tasks = self.store.load_tasks()
        self.notifier = Notifier()
        # 调度器的变更回调可能来自它的工作线程：那里只能投请求，界面刷新一律回主线程消费。
        self._refresh_queue: queue.Queue = queue.Queue()
        # 界面还没搭起来时到来的变更请求先挂起：此刻还没有卡片与空状态控件可刷。
        self._ui_ready = False
        self.scheduler = TaskScheduler(self.notifier, on_change=self._schedule_refresh)
        self.scheduler.start()
        self.scheduler.sync_tasks(self.tasks)

        self.fonts = sorted(set(tkfont.families(self.root)))
        self.font_family_var = tk.StringVar(value=self._default_font_family())
        self.root.font_family_var = self.font_family_var
        self.autostart_var = tk.BooleanVar(value=self._is_autostart_enabled())
        self.summary_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.quote_var = tk.StringVar()
        self.quote_source_var = tk.StringVar()
        self.today_var = tk.StringVar()
        self._clock_after_id: str | None = None
        self._display_day: date | None = None
        self.weather_var = tk.StringVar(value="天气获取中…")
        self.weather_meta_var = tk.StringVar()
        self.lunar_var = tk.StringVar()
        self.ganzhi_var = tk.StringVar()
        self._weather: Weather | None = None
        self._weather_failed = False
        self._lunar_day: date | None = None
        self._lunar_pending: date | None = None
        self._lunar_retry_at = 0.0
        self._weather_after_id: str | None = None
        self._live_info_queue: queue.Queue = queue.Queue()
        self.subtitle_var = tk.StringVar()
        self.current_quote_index: int | None = None
        self.selected_task_id: str | None = None
        self._restore_window_state = "normal"

        self.style = ttk.Style()
        self.task_cards: dict[str, tk.Widget] = {}
        self.task_dots: dict[str, tk.Label] = {}
        self.tk_images: list[tk.PhotoImage] = []
        self.empty_label: tk.Label | None = None
        self.pause_button: PillButton | None = None
        self.scroll_frame: tk.Frame | None = None
        self.search_entry: tk.Entry | None = None
        self.filter_btn: tk.Label | None = None
        # 两类菜单都只建一次、反复复用：每次弹出都新建会让 Tcl widget 随点击次数线性增长。
        self._task_menu: tk.Menu | None = None
        self._filter_menu: tk.Menu | None = None
        self.filter_type: str = "all"
        self.task_canvas: tk.Canvas | None = None
        # 列表滚动范围的重算句柄：卡片整批重建后要等布局稳定再算一次。
        self._scroll_sync_id: str | None = None
        # 需要在滚动范围重算之后带进视野的卡片，以及上次已经带过的选中项。
        self._pending_into_view: str | None = None
        self._last_scrolled_selection: str | None = None
        self.shell: tk.Frame | None = None
        self.left_panel: tk.Frame | None = None
        self.home_intro_content: tk.Frame | None = None
        self.home_intro_frame: tk.Frame | None = None
        self.home_intro_title: tk.Label | None = None
        self.home_intro_body: tk.Label | None = None
        self.quote_label: tk.Label | None = None
        self.season_card = None
        self.panel_content: tk.Frame | None = None
        self.home_divider: tk.Frame | None = None
        self._quote_wrap: int | None = None
        self._quote_side_by_side: bool | None = None
        self.home_art: tk.Canvas | None = None
        self.home_art_size: tuple[int, int] | None = None

        self._configure_styles()
        self.search_var.trace_add("write", lambda *_args: self._refresh_ui())
        self.tray = TrayController(
            on_open=self.show_window,
            on_add_task=self.add_task_from_tray,
            on_open_settings=self.open_settings_from_tray,
            on_pause_toggle=self.toggle_pause_all_from_tray,
            is_paused=lambda: self.scheduler.paused,
            on_exit=self.request_exit_app,
        )

        self._build_ui()
        self._set_random_quote()
        self._start_quote_rotation()
        self._maximize_window()
        # 界面已就绪：此后的调度变更回调可以立刻刷新，挂起期间的请求在这里一并补上。
        self._ui_ready = True
        self._drain_refresh_requests()
        self._refresh_ui()
        self._refresh_clock()
        self._start_live_info()
        self._set_window_icons()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close_requested)
        # 桌面提醒里最常用的动作是新建，给一个不依赖鼠标位置的入口。
        self.root.bind("<Control-n>", self._on_new_task_shortcut)
        self.tray.start()

    def run(self) -> None:
        self.root.mainloop()

    def _configure_display_metrics(self) -> None:
        self.root.update_idletasks()
        dpi = self._detect_window_dpi()
        self._last_dpi = dpi
        self.ui_scale = max(dpi / 96.0, 1.0)
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except tk.TclError:
            pass

        # 混合 DPI 多显示器下 winfo_screenwidth() 的单位不可靠，改用主屏工作区。
        # DPI 感知进程的 Tk 几何与 GetSystemMetrics 均为物理像素，这里保持物理单位。
        work_width, work_height = self._primary_work_area()
        avail_width = max(1, work_width - scale(80, self.ui_scale))
        avail_height = max(1, work_height - scale(80, self.ui_scale))
        width = min(scale(1280, self.ui_scale), avail_width)
        height = min(scale(760, self.ui_scale), avail_height)
        min_width = min(scale(1080, self.ui_scale), avail_width)
        min_height = min(scale(660, self.ui_scale), avail_height)
        origin_x, origin_y = getattr(self, "_work_area_origin", (0, 0))
        x = origin_x + max((work_width - width) // 2, 0)
        y = origin_y + max((work_height - height) // 2, 0)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min_width, min_height)

    def _primary_work_area(self) -> tuple[int, int]:
        # SPI_GETWORKAREA 返回主屏工作区（不含任务栏）的物理像素。
        try:
            rect = ctypes.wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                self._work_area_origin = (rect.left, rect.top)
                return rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
        self._work_area_origin = (0, 0)
        return self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _detect_window_dpi(self) -> int:
        # 取窗口当前的实际 DPI：GetDpiForMonitor 的 MDT_DEFAULT 在多屏/缩放切换
        # 环境下会返回过期值，只有窗口自身报告的 DPI 与系统缩放行为一致。
        return self._current_window_dpi()

    def _current_window_dpi(self) -> int:
        try:
            hwnd = self.root.winfo_id()
            return int(ctypes.windll.user32.GetDpiForWindow(hwnd))
        except Exception:
            return 96

    def _on_root_configure(self, event: tk.Event) -> None:
        # 显示缩放变化（改缩放、RDP 重连、跨屏拖动）时窗口像素会被系统重缩放，
        # 而 ui_scale 还停留在启动值，内容会与窗口脱节。检测到变化就按新 DPI 重建。
        if event.widget is not self.root:
            return
        dpi = self._current_window_dpi()
        if dpi != self._last_dpi:
            logging.getLogger(__name__).info("显示缩放变化，按新 DPI 重建界面：%s -> %s", self._last_dpi, dpi)
            self._last_dpi = dpi
            self.ui_scale = max(dpi / 96.0, 1.0)
            self.root.ui_scale = self.ui_scale
            try:
                self.root.tk.call("tk", "scaling", dpi / 72.0)
            except tk.TclError:
                pass
            self._rebuild_ui()

    def _configure_styles(self) -> None:
        apply_ttk_theme(self.style, self.font_family_var.get(), self.ui_scale)

    def _default_font_family(self) -> str:
        configured = str(self.settings.get("font_family", "")).strip()
        if configured and configured in self.fonts:
            return configured
        for family_name in (
            "JetBrainsMono NFP",
            "JetBrainsMono NL",
            "JetBrainsMono NF",
            "JetBrains Mono",
            "LXGW WenKai",
            "Microsoft YaHei UI",
        ):
            if family_name in self.fonts:
                return family_name
        return self.fonts[0] if self.fonts else "Microsoft YaHei UI"

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=0, style="App.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)

        shell = tk.Frame(outer, bg=PALETTE.panel_bg, bd=0)
        self.shell = shell
        shell.pack(fill=tk.BOTH, expand=True)
        shell.grid_columnconfigure(0, weight=0)
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(1, weight=1)
        shell.bind("<Configure>", self._resize_main_layout)
        self.root.bind("<Configure>", self._on_root_configure)

        self._build_titlebar(shell)
        self._build_left_panel(shell)
        self._build_right_panel(shell)

    def _rebuild_ui(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        self.task_cards.clear()
        self.task_dots.clear()
        self.empty_label = None
        self.pause_button = None
        self.scroll_frame = None
        self.search_entry = None
        self.filter_btn = None
        self.task_canvas = None
        self.task_scrollbar = None
        # 重建后按新尺寸把当前选中项重新带进视野一次。
        self._last_scrolled_selection = None
        self.shell = None
        self.left_panel = None
        self.home_intro_content = None
        self.home_intro_frame = None
        self.home_intro_title = None
        self.home_intro_body = None
        self.home_art = None
        self.home_art_size = None
        self.quote_label = None
        self.season_card = None
        self.panel_content = None
        self.home_divider = None
        self._quote_wrap = None
        self._quote_side_by_side = None
        self.root.configure(bg=PALETTE.window_bg)
        self._configure_styles()
        self._build_ui()
        self._refresh_ui()

    def _build_titlebar(self, parent: tk.Frame) -> None:
        bar = tk.Frame(parent, bg=PALETTE.card_bg, padx=scale(22, self.ui_scale), pady=scale(10, self.ui_scale))
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        header_icon = self._load_icon_photo(20)
        if header_icon is not None:
            tk.Label(bar, image=header_icon, bg=PALETTE.card_bg).grid(row=0, column=0, sticky="w")
        else:
            tk.Label(
                bar,
                text="◔",
                bg=PALETTE.card_bg,
                fg=PALETTE.accent_text,
                font=font(self.font_family_var.get(), 14, self.ui_scale),
            ).grid(row=0, column=0, sticky="w")
        tk.Label(
            bar,
            text=APP_NAME,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.font_family_var.get(), 13, self.ui_scale, "bold"),
        ).grid(row=0, column=1, sticky="w", padx=(scale(10, self.ui_scale), 0))
        tk.Frame(parent, bg=PALETTE.border_soft, height=1).grid(row=0, column=0, columnspan=2, sticky="sew")

    def _set_window_icons(self) -> None:
        try:
            icon_paths = [path for path in ICON_PNG_FILES if path.exists()]
            icon_paths.sort(key=lambda path: int(path.stem.rsplit("_", 1)[-1]), reverse=True)
            images = [
                # 必须显式绑定到本窗口的 root：不传 master 时图片会挂到进程默认解释器上，
                # 同进程存在过别的 Tk 窗口时（测试连续建窗就是这种情形）图片名会失效。
                tk.PhotoImage(master=self.root, file=str(path))
                for path in icon_paths
            ]
            if images:
                self.tk_images.extend(images)
                self.root.iconphoto(True, *images)
        except tk.TclError:
            pass
        try:
            self.root.iconbitmap(str(ICON_ICO_FILE))
        except tk.TclError:
            pass

    def _load_icon_photo(self, target_size: int) -> tk.PhotoImage | None:
        candidates = sorted(ICON_PNG_FILES, key=lambda path: abs(int(path.stem.rsplit("_", 1)[-1]) - target_size))
        for path in candidates:
            if not path.exists():
                continue
            try:
                image = tk.PhotoImage(master=self.root, file=str(path))
            except tk.TclError:
                continue
            self.tk_images.append(image)
            return image
        return None

    def _build_left_panel(self, parent: tk.Frame) -> None:
        left = tk.Frame(parent, bg=PALETTE.card_bg, width=scale(460, self.ui_scale), highlightthickness=0)
        self.left_panel = left
        left.grid(row=1, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.grid_columnconfigure(0, weight=1)
        left.grid_columnconfigure(1, weight=0)
        left.grid_rowconfigure(2, weight=1)

        top = tk.Frame(left, bg=PALETTE.card_bg, padx=scale(22, self.ui_scale), pady=scale(20, self.ui_scale))
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=0)
        PillButton(
            top,
            "+  新建提醒",
            self.add_task,
            width=scale(238, self.ui_scale),
            height=scale(42, self.ui_scale),
            family=self.font_family_var.get(),
            ui_scale=self.ui_scale,
            radius=scale(16, self.ui_scale),
            bg=PALETTE.card_bg,
        ).grid(row=0, column=0, sticky="ew")
        self.pause_button = PillButton(
            top,
            "全部运行",
            self.toggle_pause_all,
            width=scale(88, self.ui_scale),
            height=scale(42, self.ui_scale),
            family=self.font_family_var.get(),
            ui_scale=self.ui_scale,
            radius=scale(14, self.ui_scale),
            bg=PALETTE.card_bg,
            fill="#fbf7f1",
            active_fill="#eee2d3",
            outline=PALETTE.border,
            fg=PALETTE.text_secondary,
            dot_color="#10b981",
        )
        self.pause_button.grid(row=0, column=1, sticky="e", padx=(scale(12, self.ui_scale), 0))

        search = tk.Frame(left, bg=PALETTE.card_bg, padx=scale(22, self.ui_scale))
        search.grid(row=1, column=0, columnspan=2, sticky="ew")
        search.grid_columnconfigure(0, weight=1)
        search_box = RoundedFrame(
            search,
            radius=scale(13, self.ui_scale),
            padding=scale(9, self.ui_scale),
            fill=PALETTE.card_bg,
            outline=PALETTE.border,
            bg=PALETTE.card_bg,
            height=scale(40, self.ui_scale),
        )
        search_box.grid(row=0, column=0, sticky="ew")
        search_box.content.grid_columnconfigure(1, weight=1)
        if not self.search_var.get().strip():
            self.search_var.set(SEARCH_PLACEHOLDER)
        tk.Label(
            search_box.content,
            text="⌕",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.font_family_var.get(), 11, self.ui_scale),
        ).grid(row=0, column=0, sticky="w", padx=(scale(3, self.ui_scale), scale(8, self.ui_scale)))
        self.search_entry = tk.Entry(
            search_box.content,
            textvariable=self.search_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted if self.search_var.get() in (SEARCH_PLACEHOLDER, "搜索提醒") else PALETTE.text_primary,
            insertbackground=PALETTE.text_primary,
            relief="flat",
            highlightthickness=0,
            borderwidth=0,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
        )
        self.search_entry.grid(row=0, column=1, sticky="ew")
        self.filter_btn = tk.Label(
            search_box.content,
            text="≡",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary if self.filter_type == "all" else PALETTE.accent_text,
            font=font(self.font_family_var.get(), 11, self.ui_scale),
            cursor="hand2",
        )
        self.filter_btn.grid(row=0, column=2, sticky="e", padx=(scale(8, self.ui_scale), scale(2, self.ui_scale)))
        self.filter_btn.bind("<Button-1>", lambda _event: self._open_filter_menu(self.filter_btn))
        self.search_entry.bind("<FocusIn>", self._search_focus_in)
        self.search_entry.bind("<FocusOut>", self._search_focus_out)

        list_shell = tk.Frame(left, bg=PALETTE.card_bg, padx=scale(22, self.ui_scale), pady=scale(6, self.ui_scale))
        list_shell.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(scale(16, self.ui_scale), 0))
        list_shell.grid_rowconfigure(0, weight=1)
        list_shell.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(list_shell, bg=PALETTE.card_bg, highlightthickness=0, bd=0)
        self.task_canvas = canvas
        self.scroll_frame = tk.Frame(canvas, bg=PALETTE.card_bg)
        self.scroll_frame.bind("<Configure>", lambda _event: self._schedule_scroll_sync())
        canvas_window = canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(canvas_window, width=event.width),
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        canvas.bind("<MouseWheel>", self._scroll_task_list)
        self.scroll_frame.bind("<MouseWheel>", self._scroll_task_list)

        # 滚动条只在内容真的超出可视高度时出现，缺省界面保持原来的干净观感。
        self.task_scrollbar = SlimScrollbar(
            list_shell, command=canvas.yview, ui_scale=self.ui_scale, bg=PALETTE.card_bg
        )
        self.task_scrollbar.grid(row=0, column=1, sticky="ns", padx=(scale(4, self.ui_scale), 0))
        self.task_scrollbar.grid_remove()
        self._scrollbar_shown = False
        canvas.configure(yscrollcommand=self._on_task_scroll)

        self.empty_label = tk.Label(
            list_shell,
            text="还没有提醒。\n点左上角新建第一条。",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
            justify=tk.CENTER,
        )

        bottom = tk.Frame(left, bg=PALETTE.card_bg, padx=scale(20, self.ui_scale), pady=scale(14, self.ui_scale))
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew")
        tk.Frame(left, bg=PALETTE.border_soft, height=1).grid(row=3, column=0, columnspan=2, sticky="new")

        settings_btn_frame = tk.Frame(bottom, bg=PALETTE.card_bg, cursor="hand2")
        settings_btn_frame.pack(side=tk.LEFT)
        gear_lbl = tk.Label(
            settings_btn_frame,
            text="⚙",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 12, self.ui_scale),
            cursor="hand2",
        )
        gear_lbl.pack(side=tk.LEFT)
        settings_text_lbl = tk.Label(
            settings_btn_frame,
            text="偏好设置",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 9, self.ui_scale),
            cursor="hand2",
            padx=scale(6, self.ui_scale),
        )
        settings_text_lbl.pack(side=tk.LEFT)

        for w in (settings_btn_frame, gear_lbl, settings_text_lbl):
            w.bind("<Button-1>", lambda _event: self.open_settings())
            w.bind("<Enter>", lambda _event: (gear_lbl.configure(fg=PALETTE.accent_text), settings_text_lbl.configure(fg=PALETTE.accent_text)))
            w.bind("<Leave>", lambda _event: (gear_lbl.configure(fg=PALETTE.text_secondary), settings_text_lbl.configure(fg=PALETTE.text_secondary)))

        tk.Label(
            bottom,
            textvariable=self.summary_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.font_family_var.get(), 9, self.ui_scale),
        ).pack(side=tk.RIGHT)
        tk.Frame(parent, bg=PALETTE.border_soft, width=1).grid(row=1, column=0, sticky="nse")

    def _build_right_panel(self, parent: tk.Frame) -> None:
        right = tk.Frame(parent, bg=PALETTE.card_bg, padx=0, pady=0)
        right.grid(row=1, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        panel = RoundedFrame(
            right,
            radius=1,
            padding=scale(28, self.ui_scale),
            fill=PALETTE.card_bg,
            outline=PALETTE.card_bg,
            bg=PALETTE.card_bg,
            width=scale(1180, self.ui_scale),
            height=scale(650, self.ui_scale),
        )
        panel.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=scale(20, self.ui_scale),
            pady=scale(40, self.ui_scale),
        )
        panel.content.grid_columnconfigure(0, weight=1)
        panel.content.grid_columnconfigure(1, weight=0)
        panel.content.grid_rowconfigure(2, weight=1)
        panel.content.bind("<Configure>", self._resize_quote_layout)
        self.panel_content = panel.content

        quote_block = tk.Frame(panel.content, bg=PALETTE.card_bg)
        quote_block.grid(row=0, column=0, sticky="nw", pady=(scale(30, self.ui_scale), 0))
        quote_lbl = tk.Label(
            quote_block,
            textvariable=self.quote_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.font_family_var.get(), 22, self.ui_scale),
            justify=tk.LEFT,
            anchor="w",
            wraplength=scale(680, self.ui_scale),
        )
        quote_lbl.pack(anchor="w")
        self.quote_label = quote_lbl

        quote_meta = tk.Frame(quote_block, bg=PALETTE.card_bg)
        quote_meta.pack(anchor="e", fill=tk.X, pady=(scale(14, self.ui_scale), 0))

        meta_inner = tk.Frame(quote_meta, bg=PALETTE.card_bg)
        meta_inner.pack(side=tk.RIGHT)

        tk.Frame(meta_inner, bg="#d7c3aa", width=scale(24, self.ui_scale), height=1).pack(side=tk.LEFT, padx=(0, scale(8, self.ui_scale)), pady=scale(6, self.ui_scale))
        tk.Label(
            meta_inner,
            textvariable=self.quote_source_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 11, self.ui_scale),
        ).pack(side=tk.LEFT)
        refresh_quote_lbl = tk.Label(
            meta_inner,
            text="换一句",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.font_family_var.get(), 9, self.ui_scale),
            cursor="hand2",
            padx=scale(10, self.ui_scale),
        )
        refresh_quote_lbl.pack(side=tk.LEFT)
        refresh_quote_lbl.bind("<Enter>", lambda _e: refresh_quote_lbl.configure(fg=PALETTE.accent_text))
        refresh_quote_lbl.bind("<Leave>", lambda _e: refresh_quote_lbl.configure(fg=PALETTE.text_muted))
        refresh_quote_lbl.bind("<Button-1>", lambda _e: self._set_random_quote())

        season_card = RoundedFrame(
            panel.content,
            radius=scale(14, self.ui_scale),
            padding=scale(16, self.ui_scale),
            fill=PALETTE.card_alt_bg,
            outline=PALETTE.border,
            shadow=False,
            bg=PALETTE.card_bg,
            width=scale(210, self.ui_scale),
        )
        season_card.grid(row=0, column=1, sticky="ne", padx=(scale(24, self.ui_scale), 0), pady=(scale(6, self.ui_scale), 0))
        self.season_card = season_card
        sc_content = season_card.content
        sc_content.configure(bg=PALETTE.card_alt_bg)

        tk.Label(
            sc_content,
            textvariable=self.weather_var,
            bg=PALETTE.card_alt_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
            anchor="e",
        ).pack(anchor="e", fill=tk.X)
        tk.Label(
            sc_content,
            textvariable=self.weather_meta_var,
            bg=PALETTE.card_alt_bg,
            fg=PALETTE.text_muted,
            font=font(self.font_family_var.get(), 8, self.ui_scale),
            wraplength=scale(176, self.ui_scale),
            justify=tk.RIGHT,
            anchor="e",
        ).pack(anchor="e", fill=tk.X, pady=(scale(3, self.ui_scale), scale(6, self.ui_scale)))
        tk.Label(
            sc_content,
            textvariable=self.today_var,
            bg=PALETTE.card_alt_bg,
            fg=PALETTE.text_muted,
            font=font(self.font_family_var.get(), 9, self.ui_scale),
            anchor="e",
        ).pack(anchor="e", fill=tk.X, pady=(scale(3, self.ui_scale), scale(4, self.ui_scale)))
        tk.Label(
            sc_content,
            textvariable=self.lunar_var,
            bg=PALETTE.card_alt_bg,
            fg=PALETTE.text_primary,
            font=font(self.font_family_var.get(), 20, self.ui_scale, "bold"),
            anchor="e",
        ).pack(anchor="e", fill=tk.X)
        tk.Label(
            sc_content,
            textvariable=self.ganzhi_var,
            bg=PALETTE.card_alt_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 9, self.ui_scale),
            anchor="e",
        ).pack(anchor="e", fill=tk.X, pady=(scale(3, self.ui_scale), 0))

        divider = tk.Frame(panel.content, bg=PALETTE.border_soft, height=1)
        divider.grid(row=1, column=0, columnspan=2, sticky="ew", padx=scale(16, self.ui_scale), pady=(scale(34, self.ui_scale), scale(28, self.ui_scale)))
        self.home_divider = divider

        content = tk.Frame(panel.content, bg=PALETTE.card_bg)
        self.home_intro_content = content
        content.grid(row=2, column=0, columnspan=2, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=0)
        content.grid_rowconfigure(0, weight=1)
        intro_text = tk.Frame(content, bg=PALETTE.card_bg)
        self.home_intro_frame = intro_text
        intro_text.grid(row=0, column=0, sticky="sw", padx=(scale(12, self.ui_scale), 0), pady=(0, scale(12, self.ui_scale)))
        intro_text.grid_columnconfigure(0, weight=1)
        self.home_intro_title = tk.Label(
            intro_text,
            textvariable=self.subtitle_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.font_family_var.get(), 14, self.ui_scale, "bold"),
            justify=tk.LEFT,
            anchor="w",
            wraplength=scale(480, self.ui_scale),
        )
        self.home_intro_title.grid(row=0, column=0, sticky="ew")
        self.home_intro_body = tk.Label(
            intro_text,
            text="在纷杂的世界里，\n为重要的事情，设一只小小的筏。\n它不喧哗，只在恰当的时候，轻轻提醒你。",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 11, self.ui_scale),
            justify=tk.LEFT,
            anchor="nw",
            pady=scale(12, self.ui_scale),
            wraplength=scale(480, self.ui_scale),
        )
        self.home_intro_body.grid(row=1, column=0, sticky="ew")
        art = tk.Canvas(
            content,
            width=scale(360, self.ui_scale),
            height=scale(180, self.ui_scale),
            bg=PALETTE.card_bg,
            highlightthickness=0,
            bd=0,
        )
        self.home_art = art
        art.grid(row=0, column=1, sticky="se", padx=(scale(20, self.ui_scale), scale(12, self.ui_scale)), pady=(0, scale(12, self.ui_scale)))
        self._configure_home_art_size(scale(360, self.ui_scale), scale(180, self.ui_scale))
        content.bind("<Configure>", self._resize_home_intro)

    def _resize_main_layout(self, event: tk.Event) -> None:
        if not self.left_panel:
            return
        min_width = scale(460, self.ui_scale)
        max_width = scale(920, self.ui_scale)
        target_width = min(max_width, max(min_width, int(event.width * 0.30)))
        if abs(self.left_panel.winfo_width() - target_width) > 1:
            self.left_panel.configure(width=target_width)

    def _resize_quote_layout(self, event: tk.Event) -> None:
        # 名言与时间天气卡随可用宽度在「并排 / 堆叠」间切换；并排时名言换行宽度
        # 按剩余空间收窄，避免溢出第 0 列被右侧时间天气卡盖住。
        if not all((self.quote_label, self.season_card, self.home_divider,
                    self.home_intro_content, self.panel_content)):
            return
        season_width = self.season_card.winfo_reqwidth()
        gap = scale(24, self.ui_scale)
        min_quote = scale(240, self.ui_scale)
        side_by_side = event.width - season_width - gap - scale(8, self.ui_scale) >= min_quote
        if side_by_side != self._quote_side_by_side:
            self._quote_side_by_side = side_by_side
            if side_by_side:
                self.season_card.grid_configure(
                    row=0, column=1, sticky="ne",
                    padx=(gap, 0), pady=(scale(6, self.ui_scale), 0))
                self.home_divider.grid_configure(row=1)
                self.home_intro_content.grid_configure(row=2)
                self.panel_content.grid_rowconfigure(2, weight=1)
                self.panel_content.grid_rowconfigure(3, weight=0)
            else:
                self.season_card.grid_configure(
                    row=1, column=0, sticky="ne",
                    padx=(0, 0), pady=(scale(10, self.ui_scale), 0))
                self.home_divider.grid_configure(row=2)
                self.home_intro_content.grid_configure(row=3)
                self.panel_content.grid_rowconfigure(2, weight=0)
                self.panel_content.grid_rowconfigure(3, weight=1)
        if side_by_side:
            available = event.width - season_width - gap - scale(8, self.ui_scale)
            wrap = max(min_quote, min(scale(680, self.ui_scale), available))
        else:
            wrap = max(min_quote, event.width - scale(16, self.ui_scale))
        if wrap != self._quote_wrap:
            self._quote_wrap = wrap
            self.quote_label.configure(wraplength=wrap)

    def _resize_home_intro(self, event: tk.Event) -> None:
        if not all((self.home_intro_content, self.home_intro_frame, self.home_intro_title, self.home_intro_body, self.home_art)):
            return
        narrow = event.width < scale(560, self.ui_scale)
        compact_height = event.height < scale(360, self.ui_scale)
        tight_height = event.height < scale(240, self.ui_scale)
        if tight_height:
            art_width = scale(180, self.ui_scale)
            art_height = scale(90, self.ui_scale)
        elif compact_height or narrow:
            art_width = scale(260, self.ui_scale)
            art_height = scale(130, self.ui_scale)
        else:
            art_width = scale(340, self.ui_scale)
            art_height = scale(170, self.ui_scale)
        self._configure_home_art_size(art_width, art_height)

        if narrow:
            wrap_width = max(scale(260, self.ui_scale), event.width - scale(32, self.ui_scale))
            self.home_intro_content.grid_columnconfigure(0, weight=1)
            self.home_intro_content.grid_columnconfigure(1, weight=0)
            self.home_intro_content.grid_rowconfigure(0, weight=1)
            self.home_intro_content.grid_rowconfigure(1, weight=0)
            self.home_intro_frame.grid_configure(
                row=0,
                column=0,
                rowspan=1,
                sticky="sw",
                padx=(scale(12, self.ui_scale), scale(12, self.ui_scale)),
                pady=(0, scale(4, self.ui_scale)),
            )
            self.home_art.grid()
            self.home_art.grid_configure(
                row=1,
                column=0,
                rowspan=1,
                sticky="se",
                padx=(0, scale(12, self.ui_scale)),
                pady=(0, scale(12, self.ui_scale)),
            )
        else:
            wrap_width = max(scale(320, self.ui_scale), event.width - art_width - scale(48, self.ui_scale))
            self.home_intro_content.grid_columnconfigure(0, weight=1)
            self.home_intro_content.grid_columnconfigure(1, weight=0)
            self.home_intro_content.grid_rowconfigure(0, weight=1)
            self.home_intro_content.grid_rowconfigure(1, weight=0)
            self.home_intro_frame.grid_configure(
                row=0,
                column=0,
                rowspan=1,
                sticky="sw",
                padx=(scale(12, self.ui_scale), scale(16, self.ui_scale)),
                pady=(0, scale(12, self.ui_scale)),
            )
            self.home_art.grid()
            self.home_art.grid_configure(
                row=0,
                column=1,
                rowspan=1,
                sticky="se",
                padx=(scale(16, self.ui_scale), scale(12, self.ui_scale)),
                pady=(0, scale(12, self.ui_scale)),
            )
        self.home_intro_title.configure(wraplength=wrap_width)
        self.home_intro_body.configure(wraplength=wrap_width)

    def _configure_home_art_size(self, width: int, height: int) -> None:
        if not self.home_art:
            return
        size = (max(1, width), max(1, height))
        if self.home_art_size == size:
            return
        self.home_art_size = size
        self.home_art.configure(width=size[0], height=size[1])
        if not self._draw_boat_asset(self.home_art):
            self._draw_boat_art(self.home_art)

    def _maximize_window(self) -> None:
        self.root.update_idletasks()
        try:
            self.root.state("normal")
        except tk.TclError:
            pass

    def _refresh_ui(self) -> None:
        if not self.scroll_frame:
            return
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
        self.task_cards.clear()
        self.task_dots.clear()

        if self.selected_task_id and not any(task.id == self.selected_task_id for task in self.tasks):
            self.selected_task_id = None
        if not self.selected_task_id and self.tasks:
            self.selected_task_id = self.tasks[0].id

        filtered_tasks = self._visible_tasks()

        if self.selected_task_id and not any(task.id == self.selected_task_id for task in filtered_tasks):
            self.selected_task_id = filtered_tasks[0].id if filtered_tasks else None
        if not self.selected_task_id and filtered_tasks:
            self.selected_task_id = filtered_tasks[0].id

        for index, task in enumerate(filtered_tasks):
            self._render_task_card(task, index)

        enabled_count = sum(1 for task in self.tasks if task.enabled)
        if len(filtered_tasks) != len(self.tasks):
            self.summary_var.set(f"筛选 {len(filtered_tasks)}/{len(self.tasks)} 条")
        else:
            self.summary_var.set(f"共 {len(self.tasks)} 条提醒")
        self.subtitle_var.set(f"{APP_NAME} · 静水深流，按时而渡")
        if self.pause_button:
            # 按钮写“点下去会发生什么”，与托盘菜单的措辞保持一致；当前是运行还是暂停交给圆点颜色表达，
            # 免得按钮写着状态、点下去却做相反的事。
            if self.scheduler.paused:
                self.pause_button.set_text("恢复全部提醒")
                self.pause_button.set_colors(
                    fill="#fbf3e6",
                    active_fill="#f7e8d0",
                    fg="#b58f6d",
                    outline="#d7c3aa",
                    dot_color="#d97706",
                )
            else:
                self.pause_button.set_text("暂停全部提醒")
                self.pause_button.set_colors(
                    fill="#fbf7f1",
                    active_fill="#eee2d3",
                    fg=PALETTE.text_secondary,
                    outline=PALETTE.border,
                    dot_color="#10b981",
                )
        if self.filter_btn:
            self.filter_btn.configure(
                fg=PALETTE.accent_text if self.filter_type != "all" else PALETTE.text_secondary
            )
        if self.empty_label:
            if filtered_tasks:
                self.empty_label.place_forget()
            else:
                # 区分“一条提醒都没有”和“有提醒但被筛选/搜索挡掉”：后者引导新建就南辕北辙了。
                self.empty_label.configure(
                    text="没有匹配的提醒。\n换个关键词或清除筛选条件。"
                    if self.tasks
                    else "还没有提醒。\n点左上角新建第一条。"
                )
                self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        # 选中项变了（例如刚新建完）就把它带进视野：长列表里新提醒落在末尾，
        # 不滚过去用户会以为没建成功。只在选择变化时做，免得每次刷新都跟用户抢滚动位置。
        if self.selected_task_id and self.selected_task_id != self._last_scrolled_selection:
            self._last_scrolled_selection = self.selected_task_id
            self._pending_into_view = self.selected_task_id
        # 这一轮卡片数量可能变了，滚动范围要跟着重新算，否则会停在旧内容高度上。
        self._schedule_scroll_sync()
        self.tray.refresh()

    def _refresh_clock(self) -> None:
        # 每次读取系统时间，跨日、休眠恢复和校时后都显示当前值。
        # 刷新体里任何一步抛错都要先把下一次刷新挂回去：这条 1 秒时钟链一断，
        # 天气、农历回填和跨日刷新就永久停摆。
        try:
            now = datetime.now()
            weekday = "一二三四五六日"[now.weekday()]
            self.today_var.set(f"{now:%Y.%m.%d} 周{weekday} {now:%H:%M:%S}")
            if self._display_day != now.date():
                self._display_day = now.date()
                self._refresh_ui()
            self._drain_refresh_requests()
            self._drain_live_info()
            self._refresh_live_info(now)
        finally:
            self._clock_after_id = self.root.after(1000, self._refresh_clock)

    def _refresh_live_info(self, now: datetime) -> None:
        # 天气展示后台线程拉到的结果，数据是否过期由 Weather.display 统一判定。
        if self._weather is not None:
            text = self._weather.display(now)
            if self._weather_failed:
                text += "\n更新失败，稍后重试"
            self.weather_var.set(text)
        elif not self._weather_failed:
            self.weather_var.set("天气获取中…")
        if (now.date() != self._lunar_day and self._lunar_pending is None
                and time.monotonic() >= self._lunar_retry_at):
            self._lunar_pending = now.date()
            self.lunar_var.set("农历获取中…")
            self.ganzhi_var.set("")
            self._refresh_lunar_async(now.date())

    def _start_live_info(self) -> None:
        self._refresh_weather()

    def _refresh_weather(self) -> None:
        self._weather_after_id = None
        self._fetch_weather_async()

    def _fetch_weather_async(self) -> None:
        # 定位与天气请求都是秒级阻塞操作，放后台线程避免卡住界面。
        def worker() -> None:
            try:
                weather = fetch_weather()
            except Exception as exc:
                logging.getLogger(__name__).info("天气获取失败：%s", exc)
                weather = None
            self._live_info_queue.put(("weather", weather))

        threading.Thread(target=worker, daemon=True, name="live-info-weather").start()

    def _apply_weather(self, weather: Weather | None) -> None:
        if weather is None:
            # 失败后按重试间隔快速再试，成功后回到正常刷新节奏。
            self._weather_failed = True
            self.weather_var.set("天气暂不可用，稍后重试")
            self._schedule_weather_refresh(WEATHER_RETRY_SECONDS)
            return
        self._weather_failed = False
        self._weather = weather
        self.weather_var.set(weather.display(datetime.now()))
        self.weather_meta_var.set(weather.location)
        self._schedule_weather_refresh(WEATHER_REFRESH_SECONDS)

    def _schedule_weather_refresh(self, delay: int) -> None:
        if self._weather_after_id is not None:
            self.root.after_cancel(self._weather_after_id)
        self._weather_after_id = self.root.after(delay * 1000, self._refresh_weather)

    def _refresh_lunar_async(self, day: date) -> None:
        def worker() -> None:
            try:
                month_day, ganzhi = lunar_date(day)
            except Exception as exc:
                logging.getLogger(__name__).info("农历获取失败：%s", exc)
                month_day, ganzhi = None, None
            self._live_info_queue.put(("lunar", (day, month_day, ganzhi)))

        threading.Thread(target=worker, daemon=True, name="live-info-lunar").start()

    def _apply_lunar(self, day: date, month_day: str | None, ganzhi: str | None) -> None:
        self._lunar_pending = None
        if day != datetime.now().date():
            self._lunar_retry_at = 0.0
            return
        if month_day is None or ganzhi is None:
            self._lunar_retry_at = time.monotonic() + LUNAR_RETRY_SECONDS
            self.lunar_var.set("农历获取失败，稍后重试")
            self.ganzhi_var.set("")
            return
        self._lunar_day = day
        self._lunar_retry_at = 0.0
        self.lunar_var.set(month_day)
        self.ganzhi_var.set(ganzhi)

    def _drain_live_info(self) -> None:
        # Tk 调用只能发生在主线程，后台线程的取数结果统一在这里回填。
        while True:
            try:
                kind, payload = self._live_info_queue.get_nowait()
            except queue.Empty:
                return
            if kind == "weather":
                self._apply_weather(payload)
            elif kind == "lunar":
                self._apply_lunar(*payload)
            else:
                # 消息类型在这里显式匹配：未知类型丢弃并留痕，
                # 不能沿用“不是天气就是农历”的假设去解包，否则会把时间链带崩。
                logging.getLogger(__name__).warning("忽略未知的实时信息消息：%s", kind)

    def _render_task_card(self, task: Task, index: int) -> None:
        if not self.scroll_frame:
            return
        is_selected = task.id == self.selected_task_id
        bg = PALETTE.card_alt_bg if is_selected else PALETTE.card_bg
        is_effectively_active = task.enabled and not self.scheduler.paused
        title_fg = PALETTE.text_primary if is_effectively_active else PALETTE.text_muted
        desc_fg = PALETTE.text_secondary if is_effectively_active else PALETTE.text_muted

        if is_selected:
            time_fg = "#b58f6d"
            badge_bg = "#f4e8dc"
            badge_fg = "#a06c3f"
            sep_bg = "#d7c3aa"
        elif is_effectively_active:
            time_fg = PALETTE.text_primary
            badge_bg = "#f4f1ec"
            badge_fg = "#756960"
            sep_bg = PALETTE.border_soft
        else:
            time_fg = PALETTE.text_muted
            badge_bg = "#eeeae3"
            badge_fg = PALETTE.text_muted
            sep_bg = PALETTE.border_soft

        if self.scheduler.paused and task.enabled:
            status_text = "暂停"
            status_bg = "#fbf3e6"
            status_fg = "#b58f6d"
        elif task.enabled:
            status_text = "启用"
            status_bg = "#edf6ee"
            status_fg = "#2e7d32"
        else:
            status_text = "停用"
            status_bg = "#f4f1ec"
            status_fg = PALETTE.text_muted

        card_height = scale(116, self.ui_scale)
        description_width = scale(210, self.ui_scale)
        card_shell = RoundedFrame(
            self.scroll_frame,
            radius=scale(12, self.ui_scale),
            padding=scale(10, self.ui_scale),
            fill=bg,
            outline="#d7c3aa" if is_selected else PALETTE.border_soft,
            shadow=is_selected,
            bg=PALETTE.card_bg,
            height=card_height,
            cursor="hand2",
        )
        card = card_shell.content
        card_shell.grid(row=index, column=0, sticky="ew", pady=(0, scale(10, self.ui_scale)))

        card.grid_columnconfigure(0, weight=0, minsize=scale(68, self.ui_scale))
        card.grid_columnconfigure(1, weight=0)
        card.grid_columnconfigure(2, weight=1, minsize=description_width)
        card.grid_columnconfigure(3, weight=0)
        card.grid_columnconfigure(4, weight=0)
        card.grid_rowconfigure(0, weight=0)
        card.grid_rowconfigure(1, weight=1)
        card.grid_rowconfigure(2, weight=0)
        self.scroll_frame.grid_columnconfigure(0, weight=1)

        time_box = tk.Frame(card, bg=bg, width=scale(68, self.ui_scale))
        time_box.grid(row=0, column=0, rowspan=3, sticky="ns", padx=(scale(2, self.ui_scale), scale(4, self.ui_scale)), pady=(scale(4, self.ui_scale), scale(4, self.ui_scale)))
        # 时间和频率作为一个整体占用居中空间，不能把两个标签直接堆在顶端。
        time_group = tk.Frame(time_box, bg=bg)
        time_group.pack(expand=True, fill=tk.X)

        time_label = tk.Label(
            time_group,
            text=self._task_time(task),
            bg=bg,
            fg=time_fg,
            font=font(self.font_family_var.get(), 14, self.ui_scale, "bold"),
            anchor="center",
            justify=tk.CENTER,
        )
        time_label.pack(anchor="center", fill=tk.X)

        badge_label = tk.Label(
            time_group,
            text=self._task_badge_text(task),
            bg=badge_bg,
            fg=badge_fg,
            font=font(self.font_family_var.get(), 8, self.ui_scale),
            padx=scale(6, self.ui_scale),
            pady=scale(1, self.ui_scale),
            anchor="center",
            justify=tk.CENTER,
        )
        badge_label.pack(anchor="center", pady=(scale(4, self.ui_scale), 0))

        sep = tk.Frame(card, bg=sep_bg, width=1)
        sep.grid(row=0, column=1, rowspan=3, sticky="ns", padx=(scale(4, self.ui_scale), scale(12, self.ui_scale)), pady=(scale(4, self.ui_scale), scale(4, self.ui_scale)))

        title_label = tk.Label(
            card,
            text=task.title,
            bg=bg,
            fg=title_fg,
            font=font(self.font_family_var.get(), 11, self.ui_scale, "bold"),
            anchor="w",
        )
        title_label.grid(row=0, column=2, sticky="ew", pady=(scale(2, self.ui_scale), 0), padx=(0, scale(6, self.ui_scale)))
        # 卡片是固定高度的列表行，标题放不下时不能换行；按当前列宽加省略号收尾，
        # 免得长标题被硬截成半句、用户还以为标题本来就那么短。
        title_font_obj = tkfont.Font(root=title_label, font=font(self.font_family_var.get(), 11, self.ui_scale, "bold"))
        title_label.bind(
            "<Configure>",
            lambda event, label=title_label, full=task.title, metrics=title_font_obj: self._elide_card_title(
                label, full, metrics, event.width
            ),
            add="+",
        )

        desc_label = tk.Label(
            card,
            text=self._describe_card_detail(task),
            bg=bg,
            fg=desc_fg,
            font=font(self.font_family_var.get(), 9, self.ui_scale),
            justify=tk.LEFT,
            anchor="nw",
            wraplength=description_width,
        )
        desc_label.grid(row=1, column=2, sticky="ew", pady=(scale(3, self.ui_scale), 0), padx=(0, scale(6, self.ui_scale)))
        # 卡片会随窗口宽度伸缩，预览要按描述标签的实际宽度换行；
        # 固定换行宽度会让超出列宽的正文被无声裁掉，末尾的字连同省略号都看不到。
        desc_label.bind(
            "<Configure>",
            lambda event, label=desc_label: label.configure(wraplength=event.width),
            add="+",
        )
        next_run_label = tk.Label(
            card, text=self._describe_next_run(task), bg=bg, fg=desc_fg,
            font=font(self.font_family_var.get(), 9, self.ui_scale), anchor="w",
        )
        next_run_label.grid(row=2, column=2, columnspan=3, sticky="ew", pady=(0, scale(2, self.ui_scale)))

        status_label = tk.Label(
            card,
            text=f"• {status_text}",
            bg=status_bg,
            fg=status_fg,
            font=font(self.font_family_var.get(), 8, self.ui_scale),
            cursor="hand2",
            padx=scale(7, self.ui_scale),
            pady=scale(2, self.ui_scale),
        )
        status_label.grid(row=0, column=3, sticky="ne", padx=(scale(4, self.ui_scale), scale(4, self.ui_scale)), pady=(scale(4, self.ui_scale), 0))
        status_label.bind("<Button-1>", lambda _event, task_id=task.id: self._toggle_task_by_id(task_id))
        status_label.bind("<MouseWheel>", self._scroll_task_list)

        action_button = tk.Label(
            card,
            text="⋮",
            bg=bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 11, self.ui_scale),
            cursor="hand2",
            padx=scale(4, self.ui_scale),
            pady=scale(1, self.ui_scale),
        )
        action_button.bind("<Enter>", lambda _event, btn=action_button: btn.configure(bg="#f2ede4"))
        action_button.bind("<Leave>", lambda _event, btn=action_button, b=bg: btn.configure(bg=b))
        action_button.bind("<Button-1>", lambda _event, task_id=task.id: self._open_task_menu(task_id))
        action_button.grid(row=0, column=4, sticky="ne", pady=(scale(2, self.ui_scale), 0))
        action_button.bind("<MouseWheel>", self._scroll_task_list)

        for widget in (card_shell, card, time_box, time_group, time_label, badge_label, sep, title_label, desc_label, next_run_label):
            widget.bind("<Button-1>", lambda _event, task_id=task.id: self._select_task(task_id))
            widget.bind("<Double-Button-1>", lambda _event, task_id=task.id: self._edit_task_by_id(task_id))
            widget.bind("<MouseWheel>", self._scroll_task_list)

        self.task_cards[task.id] = card_shell

    def _draw_boat_art(self, canvas: tk.Canvas) -> None:
        canvas.update_idletasks()
        width = max(int(canvas["width"]), 260)
        height = max(int(canvas["height"]), 120)
        mountain = "#e9e7e2"
        ink = "#8b7764"
        soft = "#c8beb2"
        water = "#eee9e1"
        canvas.create_polygon(
            width * 0.08,
            height * 0.70,
            width * 0.26,
            height * 0.43,
            width * 0.41,
            height * 0.69,
            fill=mountain,
            outline="",
        )
        canvas.create_polygon(
            width * 0.28,
            height * 0.70,
            width * 0.48,
            height * 0.34,
            width * 0.67,
            height * 0.70,
            fill="#e2dfd9",
            outline="",
        )
        for y in (0.72, 0.79, 0.87):
            canvas.create_line(width * 0.08, height * y, width * 0.96, height * y, fill=water, width=1)
        canvas.create_line(width * 0.55, height * 0.68, width * 0.74, height * 0.78, fill=ink, width=2)
        canvas.create_line(width * 0.74, height * 0.78, width * 0.87, height * 0.68, fill=ink, width=2)
        canvas.create_arc(
            width * 0.54,
            height * 0.58,
            width * 0.88,
            height * 0.86,
            start=190,
            extent=150,
            outline=ink,
            width=2,
            style=tk.ARC,
        )
        canvas.create_oval(width * 0.64, height * 0.58, width * 0.76, height * 0.76, outline=soft, width=2)
        canvas.create_line(width * 0.70, height * 0.61, width * 0.66, height * 0.20, fill=ink, width=2)
        canvas.create_line(width * 0.66, height * 0.20, width * 0.72, height * 0.27, fill=ink, width=2)
        canvas.create_line(width * 0.62, height * 0.84, width * 0.84, height * 0.89, fill="#d8d1c7", width=1)

    def _draw_boat_asset(self, canvas: tk.Canvas) -> bool:
        if not BOAT_ART_FILE.exists():
            return False
        try:
            image = Image.open(BOAT_ART_FILE).convert("RGBA")
        except OSError:
            return False
        canvas_width = max(1, int(float(canvas["width"])))
        canvas_height = max(1, int(float(canvas["height"])))
        fit_width = max(1, canvas_width - scale(16, self.ui_scale))
        fit_height = max(1, canvas_height - scale(12, self.ui_scale))
        image.thumbnail((fit_width, fit_height), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image, master=self.root)
        self.tk_images.append(photo)
        canvas.delete("all")
        canvas.create_image(canvas_width // 2, canvas_height // 2, image=photo, anchor="center")
        return True

    def _select_task(self, task_id: str) -> None:
        self.selected_task_id = task_id
        self._refresh_ui()

    def _schedule_scroll_sync(self) -> None:
        # frame 的 <Configure> 在布局刷新之前就触发，此刻读到的还是旧高度，
        # 所以统一推迟到空闲时按实际尺寸重算，并合并同一批事件只算一次。
        if self._scroll_sync_id is not None:
            try:
                self.root.after_cancel(self._scroll_sync_id)
            except tk.TclError:
                pass
        self._scroll_sync_id = self.root.after_idle(self._sync_task_scrollregion)

    def _sync_task_scrollregion(self) -> None:
        """按当前卡片内容重算列表的滚动范围。

        卡片是整批销毁重建的，重建后 frame 的高度虽然已经变小，
        但 <Configure> 早于布局刷新触发，scrollregion 会一直停在旧高度上：
        提醒删到只剩一条时列表仍然能滚动，仅有的一张卡片还会被滚出视区。
        """
        self._scroll_sync_id = None
        canvas, frame = self.task_canvas, self.scroll_frame
        if canvas is None or frame is None:
            return
        # 用请求高度而不是 winfo_height()：卡片整批重建后，画布窗口项的实际高度
        # 要等更晚的布局回合才会缩回去，这里读到的仍会是旧内容的高度。
        canvas.configure(
            scrollregion=(0, 0, max(1, canvas.winfo_width()), max(1, frame.winfo_reqheight()))
        )
        # 滚动范围刚校准，此刻把待带进视野的卡片滚过去最准确。
        if self._pending_into_view is not None:
            task_id, self._pending_into_view = self._pending_into_view, None
            self._scroll_card_into_view(task_id)

    def _scroll_card_into_view(self, task_id: str) -> None:
        """把指定卡片滚进可视区，已经看得见就不动。"""
        shell = self.task_cards.get(task_id)
        canvas, frame = self.task_canvas, self.scroll_frame
        if shell is None or canvas is None or frame is None:
            return
        content = frame.winfo_reqheight()
        view = canvas.winfo_height()
        if content <= view:
            return
        top = shell.winfo_y()
        bottom = top + shell.winfo_height()
        view_top = canvas.yview()[0] * content
        if top >= view_top and bottom <= view_top + view:
            return
        # 优先让整张卡片贴着视区顶部；靠近末尾时退到还能滚到的最远处。
        target = min(max(top, 0), content - view)
        canvas.yview_moveto(target / content)

    def _on_task_scroll(self, first: str, last: str) -> None:
        # canvas 每次视图或滚动范围变化都会回调这里，所以列表增删后能自动重新判断。
        # 卡片高度是固定的，滚动条出现只会占掉一点宽度、不会改变内容高度，
        # 因此这里的显隐不会反过来又触发一次判断而形成来回抖动。
        overflowing = float(first) > 0.0 or float(last) < 1.0
        if overflowing != self._scrollbar_shown:
            self._scrollbar_shown = overflowing
            if overflowing:
                self.task_scrollbar.grid()
            else:
                self.task_scrollbar.grid_remove()
        self.task_scrollbar.set(first, last)

    def _scroll_task_list(self, event) -> str:
        if self.task_canvas is not None:
            self.task_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _open_task_menu(self, task_id: str) -> None:
        self.selected_task_id = task_id
        task = self._selected_task()
        menu = self._reusable_menu("_task_menu")
        menu.delete(0, "end")
        menu.add_command(label="编辑", command=self.edit_selected_task)
        menu.add_command(label=f"{DEFAULT_SNOOZE_MINUTES} 分钟后再说", command=self.snooze_selected_task)
        menu.add_command(label="停用提醒" if task and task.enabled else "启用提醒", command=self.toggle_selected_task)
        menu.add_command(label="删除", command=self.delete_selected_task)
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _reusable_menu(self, attribute: str) -> tk.Menu:
        # 菜单复用同一个 Tcl widget：菜单项每次重填，窗口本身不重建，点多少次都不累积。
        menu = getattr(self, attribute, None)
        if menu is None or not menu.winfo_exists():
            menu = tk.Menu(self.root, tearoff=False)
            setattr(self, attribute, menu)
        return menu

    def _edit_task_by_id(self, task_id: str) -> None:
        self.selected_task_id = task_id
        self.edit_selected_task()

    def _toggle_task_by_id(self, task_id: str) -> None:
        self.selected_task_id = task_id
        self.toggle_selected_task()

    def _visible_tasks(self) -> list[Task]:
        tasks = self.tasks
        if self.filter_type == "enabled":
            tasks = [task for task in tasks if task.enabled]
        elif self.filter_type == "disabled":
            tasks = [task for task in tasks if not task.enabled]
        elif self.filter_type == "recurring":
            tasks = [task for task in tasks if task.trigger_type != "once"]
        elif self.filter_type == "once":
            tasks = [task for task in tasks if task.trigger_type == "once"]

        keyword = self.search_var.get().strip()
        if not keyword or keyword in (SEARCH_PLACEHOLDER, "搜索提醒"):
            return tasks
        keyword_lower = keyword.lower()
        return [
            task
            for task in tasks
            if keyword_lower in task.title.lower()
            or keyword_lower in (task.body or "").lower()
            or keyword_lower in self._describe_trigger(task).lower()
            or keyword_lower in self._describe_next_run(task).lower()
            or keyword_lower in self._task_badge_text(task).lower()
        ]

    def _open_filter_menu(self, widget: tk.Widget) -> None:
        menu = self._reusable_menu("_filter_menu")
        menu.delete(0, "end")
        options = [
            ("all", "全部提醒"),
            ("enabled", "仅显示启用"),
            ("disabled", "仅显示停用"),
            ("recurring", "仅周期提醒"),
            ("once", "仅单次定时"),
        ]
        for key, label in options:
            prefix = "✓ " if self.filter_type == key else "   "
            menu.add_command(
                label=f"{prefix}{label}",
                command=lambda k=key: self._set_filter_type(k),
            )
        try:
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _set_filter_type(self, filter_type: str) -> None:
        self.filter_type = filter_type
        if self.filter_btn:
            self.filter_btn.configure(
                fg=PALETTE.accent_text if filter_type != "all" else PALETTE.text_secondary
            )
        self._refresh_ui()

    def _search_focus_in(self, _event) -> None:
        if self.search_entry is None:
            return
        if self.search_var.get() in (SEARCH_PLACEHOLDER, "搜索提醒"):
            self.search_var.set("")
        self.search_entry.configure(fg=PALETTE.text_primary)

    def _search_focus_out(self, _event) -> None:
        if self.search_entry is None:
            return
        if not self.search_var.get().strip():
            self.search_var.set(SEARCH_PLACEHOLDER)
            self.search_entry.configure(fg=PALETTE.text_muted)

    def _task_icon(self, task: Task) -> str:
        mapping = {
            "once": "☼",
            "interval": "◎",
            "weekly": "◴",
            "weekly_last_workday": "◷",
            "yearly": "▣",
            "cron": "⌘",
        }
        return mapping.get(task.trigger_type, "•")

    def _task_badge_text(self, task: Task) -> str:
        mapping = {
            "once": "单次",
            "interval": "固定间隔",
            "weekly": "按周",
            "weekly_last_workday": "工作日",
            "yearly": "按年",
            "cron": "Cron",
        }
        return mapping.get(task.trigger_type, "提醒")

    def _task_time(self, task: Task) -> str:
        # 卡片上的时间只是展示：配置被手工改坏时（例如分钟写成 null）退回占位文案，
        # 一条脏记录不能把整个提醒列表的渲染带崩。
        config = task.trigger_config if isinstance(task.trigger_config, dict) else {}
        try:
            if task.trigger_type == "once":
                return str(config.get("time", "--:--"))[:5]
            if task.trigger_type in {"weekly", "weekly_last_workday", "yearly"}:
                hour = int(config.get("hour", 0))
                minute = int(config.get("minute", 0))
                return f"{hour:02d}:{minute:02d}"
            if task.trigger_type == "interval":
                minutes = int(config.get("minutes", 0))
                hours = int(config.get("hours", 0))
                if hours and minutes:
                    return f"{hours}h{minutes}m"
                if hours:
                    return f"{hours}h"
                if minutes:
                    return f"{minutes}m"
        except (TypeError, ValueError):
            return "待修复"
        return "提醒"

    def _describe_card_detail(self, task: Task) -> str:
        body = task.body.strip() if task.body and task.body.strip() else self._describe_trigger(task)
        preview = " ".join(body.split())
        return preview[:30] + "…" if len(preview) > 30 else preview

    def _describe_next_run(self, task: Task) -> str:
        if self.scheduler.paused and task.enabled:
            return "下次：全部已暂停"
        if not task.enabled:
            return "下次：未启用"
        if not task.next_run:
            return "下次：暂无"
        try:
            run_at = datetime.fromisoformat(task.next_run)
            today = datetime.now().date()
            if run_at.date() == today:
                day_prefix = "今日"
            elif run_at.date() == today + timedelta(days=1):
                day_prefix = "明日"
            elif run_at.year == today.year:
                day_prefix = f"{run_at.month:02d}-{run_at.day:02d}"
            else:
                day_prefix = f"{run_at.year}-{run_at.month:02d}-{run_at.day:02d}"
            return f"下次：{day_prefix} {run_at.strftime('%H:%M')}"
        except (ValueError, TypeError):
            # next_run 直接来自本地文件，可能被写成了数字等非文本值，
            # 解析不了就原样展示，不能把异常抛给刷新的时钟链。
            return f"下次：{task.next_run}"

    def _elide_card_title(self, label: tk.Label, full_title: str, metrics: tkfont.Font, width: int) -> None:
        # 首帧列宽还没定下来（或窄到放不下省略号）时保持原文案，
        # 否则标题会先闪成一个省略号，再在窗口布局完成后跳回正文。
        if width <= metrics.measure("…"):
            return
        fitted = elide_text(full_title, metrics, width)
        if fitted != label.cget("text"):
            label.configure(text=fitted)

    def _schedule_refresh(self) -> None:
        # Tkinter 只允许主线程调用，而调度器的这个回调会从它的工作线程进来。
        # 主线程的调用（用户点暂停、新建、编辑、删除）立刻刷新，保证操作即时可见；
        # 后台线程只留下一个刷新请求，交给每秒的时钟链回主线程执行。
        if threading.current_thread() is threading.main_thread() and getattr(self, "_ui_ready", True):
            self._sync_from_scheduler()
            return
        self._refresh_queue.put(True)

    def _drain_refresh_requests(self) -> None:
        # 一秒内可能累积多次变更（例如批量同步），合并成一次刷新，避免重复重建卡片。
        pending = False
        while True:
            try:
                self._refresh_queue.get_nowait()
            except queue.Empty:
                break
            pending = True
        if pending:
            self._sync_from_scheduler()

    def _sync_from_scheduler(self) -> None:
        self._persist_tasks()
        self._refresh_ui()

    def _persist_tasks(self) -> None:
        self.store.save_tasks(self.tasks)

    def _set_random_quote(self) -> None:
        if not APP_QUOTES:
            self.quote_var.set("")
            self.quote_source_var.set("")
            return
        next_index = random.randrange(len(APP_QUOTES))
        if self.current_quote_index is not None and len(APP_QUOTES) > 1:
            while next_index == self.current_quote_index:
                next_index = random.randrange(len(APP_QUOTES))
        self.current_quote_index = next_index
        quote_text, source = APP_QUOTES[next_index]
        self.quote_var.set(quote_text)
        self.quote_source_var.set(source)

    def _start_quote_rotation(self) -> None:
        self.root.after(18000, self._rotate_quote)

    def _rotate_quote(self) -> None:
        self._set_random_quote()
        self._start_quote_rotation()

    def _persist_settings(self) -> None:
        self.settings["font_family"] = self.font_family_var.get()
        self.store.save_settings(self.settings)

    def _font_directories(self) -> list[Path]:
        dirs = [FONT_DIR]
        custom = str(self.settings.get("custom_font_dir", "")).strip()
        if custom:
            dirs.append(Path(custom))
        return dirs

    def _font_choices(self) -> list[str]:
        preferred = [
            "JetBrains Mono",
            "JetBrainsMono NFP",
            "JetBrainsMono NF",
            "LXGW WenKai",
            "Microsoft YaHei UI",
            "Microsoft YaHei",
        ]
        choices: list[str] = []
        for family_name in preferred + self.fonts:
            if family_name in self.fonts and family_name not in choices:
                choices.append(family_name)
        return choices

    def open_settings(self) -> None:
        dialog = SettingsDialog(
            self.root,
            font_choices=self._font_choices(),
            selected_font=self.font_family_var.get(),
            autostart_enabled=self.autostart_var.get(),
            custom_font_dir=str(self.settings.get("custom_font_dir", "")).strip(),
            close_action=str(self.settings.get("close_action", "")).strip() or "ask",
        )
        self.root.wait_window(dialog)
        if not dialog.result:
            return

        desired_autostart = bool(dialog.result["autostart_enabled"])
        selected_font = str(dialog.result["font_family"])
        self.font_family_var.set(selected_font)
        self.settings["font_family"] = selected_font
        self.settings["custom_font_dir"] = str(dialog.result["custom_font_dir"])
        self.settings["close_action"] = str(dialog.result["close_action"])
        if self.settings["custom_font_dir"]:
            register_font_directories([Path(str(self.settings["custom_font_dir"]))])
            self.fonts = sorted(set(tkfont.families(self.root)))
        self._persist_settings()
        self._rebuild_ui()
        if desired_autostart != self.autostart_var.get():
            self.autostart_var.set(desired_autostart)
            self.toggle_autostart()

    def _on_new_task_shortcut(self, _event: tk.Event | None = None) -> None:
        # 按键事件本来就在主线程，和点“新建提醒”按钮走同一条路径。
        self.add_task()

    def add_task(self) -> None:
        dialog = TaskDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.result:
            try:
                self._save_task_change(dialog.result)
            except Exception as exc:
                show_error(self.root, "添加提醒", f"保存失败：{exc}")
            else:
                self._refresh_ui()

    def _save_task_change(self, task: Task, *, delete: bool = False) -> None:
        previous_tasks = self.tasks[:]
        previous_selection = self.selected_task_id
        try:
            with self.scheduler.task_transaction(task.id):
                if delete:
                    self.tasks = [item for item in self.tasks if item.id != task.id]
                    self.selected_task_id = self.tasks[0].id if self.tasks else None
                    self.scheduler.remove_task(task.id, notify=False)
                else:
                    index = next((i for i, item in enumerate(self.tasks) if item.id == task.id), None)
                    if index is None:
                        self.tasks.append(task)
                    else:
                        self.tasks[index] = task
                    self.selected_task_id = task.id
                    self.scheduler.upsert_task(task, notify=False)
                self._persist_tasks()
        except Exception:
            self.tasks = previous_tasks
            self.selected_task_id = previous_selection
            raise

    def edit_selected_task(self) -> None:
        task = self._selected_task()
        if not task:
            return
        dialog = TaskDialog(self.root, task=task)
        self.root.wait_window(dialog)
        if dialog.result:
            try:
                self._save_task_change(dialog.result)
            except Exception as exc:
                show_error(self.root, "编辑提醒", f"保存失败：{exc}")
            else:
                self._refresh_ui()

    def snooze_selected_task(self) -> None:
        # 这条提醒现在处理不了时，补一次通知就够，不动它本身的周期设置。
        # 已停用的提醒仍然可以稍后提醒：单次提醒触发后会自行停用，而那时正是最需要补一次的时刻。
        task = self._selected_task()
        if not task:
            return
        if self.scheduler.paused:
            # 暂停期间补发不会响；先如实说明，别让成功提示落空。
            show_info(self.root, "稍后提醒", "全部提醒已暂停，恢复后稍后提醒才会生效。")
            return
        try:
            scheduled = self.scheduler.snooze_task(task.id)
        except Exception as exc:
            show_error(self.root, "稍后提醒", f"安排失败：{exc}")
            return
        if not scheduled:
            show_info(self.root, "稍后提醒", "这条提醒已经不在了，没法再提醒。")
            return
        show_info(self.root, "稍后提醒", f"{DEFAULT_SNOOZE_MINUTES} 分钟后会再提醒一次“{task.title}”。")

    def delete_selected_task(self) -> None:
        task = self._selected_task()
        if not task:
            return
        if not ask_yes_no(self.root, "删除提醒", f"确认删除“{task.title}”吗？"):
            return
        try:
            self._save_task_change(task, delete=True)
        except Exception as exc:
            show_error(self.root, "删除提醒", f"删除失败：{exc}")
        else:
            self._refresh_ui()

    def toggle_selected_task(self) -> None:
        task = self._selected_task()
        if not task:
            return
        try:
            updated = replace(task, enabled=not task.enabled)
            self._save_task_change(updated)
        except Exception as exc:
            show_error(self.root, "更新提醒", f"状态切换失败：{exc}")
        else:
            self._refresh_ui()

    def toggle_pause_all(self) -> None:
        if self.scheduler.paused:
            self.scheduler.resume_all()
        else:
            self.scheduler.pause_all()
        self._refresh_ui()

    def toggle_pause_all_from_tray(self) -> None:
        self.root.after(0, self.toggle_pause_all)

    def toggle_autostart(self) -> None:
        try:
            command = self._autostart_command()
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                if self.autostart_var.get():
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            self.autostart_var.set(not self.autostart_var.get())
            show_error(self.root, "开机自启", f"设置失败：{exc}")

    def _is_autostart_enabled(self) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def _autostart_command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else Path(sys.executable)
        script = Path(__file__).resolve().parents[1] / "main.py"
        return f'"{interpreter}" "{script}"'

    def hide_window(self) -> None:
        self._remember_window_state()
        self.root.withdraw()
        self.tray.refresh()

    def request_hide_window(self) -> None:
        self.root.after(0, self.hide_window)

    def on_close_requested(self) -> None:
        action = str(self.settings.get("close_action", "")).strip()
        if action in {"tray", "exit"}:
            if action == "tray":
                self.hide_window()
            else:
                self.exit_app()
            return

        dialog = CloseChoiceDialog(self.root, self.font_family_var.get())
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        chosen = str(dialog.result["action"])
        if bool(dialog.result["remember"]):
            self.settings["close_action"] = chosen
            self._persist_settings()
        if chosen == "tray":
            self.hide_window()
        else:
            self.exit_app()

    def show_window(self) -> None:
        self.root.after(0, self._show_window_sync)

    def _show_window_sync(self) -> None:
        self.root.deiconify()
        try:
            if self._restore_window_state == "zoomed":
                self.root.state("zoomed")
            else:
                self.root.state("normal")
        except tk.TclError:
            pass
        self.root.lift()
        self.root.focus_force()
        self.tray.refresh()

    def _remember_window_state(self) -> None:
        try:
            state = self.root.state()
        except tk.TclError:
            return
        if state in {"normal", "zoomed"}:
            self._restore_window_state = state

    def is_window_visible(self) -> bool:
        return bool(self.root.winfo_viewable())

    def add_task_from_tray(self) -> None:
        self.show_window()
        self.root.after(120, self.add_task)

    def open_settings_from_tray(self) -> None:
        self.show_window()
        self.root.after(120, self.open_settings)

    def exit_app(self) -> None:
        # 保存失败时保留时钟、托盘和调度，用户可释放磁盘空间后重新退出。
        try:
            self._persist_tasks()
        except Exception as exc:
            logging.getLogger(__name__).error("退出前保存提醒失败：%s", exc, exc_info=True)
            show_error(self.root, "退出失败", f"提醒保存失败，程序仍继续运行。请处理后重试：{exc}")
            return
        if self._clock_after_id is not None:
            self.root.after_cancel(self._clock_after_id)
            self._clock_after_id = None
        if self._weather_after_id is not None:
            self.root.after_cancel(self._weather_after_id)
            self._weather_after_id = None
        self.tray.stop()
        stop_helper()
        self.scheduler.shutdown()
        self.root.after(0, self.root.destroy)

    def request_exit_app(self) -> None:
        self.root.after(0, self.exit_app)

    def _selected_task(self) -> Task | None:
        if not self.selected_task_id:
            show_info(self.root, "提示", "请先选择一条提醒。")
            return None
        return next((task for task in self.tasks if task.id == self.selected_task_id), None)

    def _describe_trigger(self, task: Task) -> str:
        # 触发方式说明用于卡片预览与搜索匹配：配置被手工改坏时给出修复提示，
        # 不能让一条脏记录把列表刷新或搜索整体带崩。
        config = task.trigger_config if isinstance(task.trigger_config, dict) else {}
        try:
            trigger_name = TRIGGER_TYPES.get(task.trigger_type, task.trigger_type)
            if task.trigger_type == "once":
                return f"{trigger_name} · {config.get('date')} {config.get('time')}"
            if task.trigger_type == "interval":
                parts = []
                for key, label in [("weeks", "周"), ("days", "天"), ("hours", "小时"), ("minutes", "分钟"), ("seconds", "秒")]:
                    value = int(config.get(key, 0))
                    if value:
                        parts.append(f"{value}{label}")
                return f"每隔 {' '.join(parts)}"
            if task.trigger_type == "weekly":
                names = ["一", "二", "三", "四", "五", "六", "日"]
                weekdays = "".join(names[index] for index in config.get("weekdays", []))
                return f"每周 周{weekdays} {int(config.get('hour', 0)):02d}:{int(config.get('minute', 0)):02d}"
            if task.trigger_type == "weekly_last_workday":
                return f"每周连续工作段最后一天 {int(config.get('hour', 18)):02d}:{int(config.get('minute', 0)):02d}"
            if task.trigger_type == "yearly":
                return f"每年 {int(config.get('month', 1)):02d}-{int(config.get('day', 1)):02d} {int(config.get('hour', 0)):02d}:{int(config.get('minute', 0)):02d}"
            return f"Cron · {config.get('expression', '')}"
        except (TypeError, ValueError, IndexError, KeyError):
            return "配置需要修复"
