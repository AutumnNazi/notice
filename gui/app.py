from __future__ import annotations

import ctypes
import logging
import queue
import random
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import winreg
from datetime import date, datetime
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageTk

from config import APP_NAME, BOAT_ART_FILE, FONT_DIR, ICON_FILE, ICON_ICO_FILE, ICON_PNG_FILES, TRIGGER_TYPES
from core.font_manager import register_font_directories
from core.icon_assets import ensure_icon_assets
from core.live_info import WEATHER_REFRESH_SECONDS, WEATHER_RETRY_SECONDS, Weather, fetch_weather, lunar_date
from core.models import Task
from core.notifier import Notifier
from core.quotes import APP_QUOTES
from core.scheduler import TaskScheduler
from gui.close_dialog import CloseChoiceDialog
from gui.message_dialog import ask_yes_no, show_error, show_info
from gui.settings_dialog import SettingsDialog
from gui.task_dialog import TaskDialog
from gui.theme import PALETTE, PillButton, RoundedFrame, apply_ttk_theme, font, scale
from gui.tray import TrayController
from single_instance import stop_helper
from store import TaskStore


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


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
        self.weather_var = tk.StringVar(value="天气获取中…")
        self.lunar_var = tk.StringVar()
        self.ganzhi_var = tk.StringVar()
        self._weather: Weather | None = None
        self._weather_failed = False
        self._lunar_day: date | None = None
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
        self.task_canvas: tk.Canvas | None = None
        self.shell: tk.Frame | None = None
        self.left_panel: tk.Frame | None = None
        self.home_intro_content: tk.Frame | None = None
        self.home_intro_frame: tk.Frame | None = None
        self.home_intro_title: tk.Label | None = None
        self.home_intro_body: tk.Label | None = None
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
        self._refresh_ui()
        self._refresh_clock()
        self._start_live_info()
        self._set_window_icons()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close_requested)
        self.tray.start()

    def run(self) -> None:
        self.root.mainloop()

    def _configure_display_metrics(self) -> None:
        self.root.update_idletasks()
        dpi = self._detect_window_dpi()
        self.ui_scale = max(dpi / 96.0, 1.0)
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except tk.TclError:
            pass

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(scale(1280, self.ui_scale), max(screen_width - scale(80, self.ui_scale), scale(1080, self.ui_scale)))
        height = min(scale(760, self.ui_scale), max(screen_height - scale(80, self.ui_scale), scale(680, self.ui_scale)))
        min_width = min(scale(1080, self.ui_scale), screen_width)
        min_height = min(scale(660, self.ui_scale), screen_height)
        x = max((screen_width - width) // 2, 0)
        y = max((screen_height - height) // 2, 0)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min_width, min_height)

    def _detect_window_dpi(self) -> int:
        try:
            hwnd = self.root.winfo_id()
            return int(ctypes.windll.user32.GetDpiForWindow(hwnd))
        except Exception:
            return 96

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
        self.task_canvas = None
        self.shell = None
        self.left_panel = None
        self.home_intro_content = None
        self.home_intro_frame = None
        self.home_intro_title = None
        self.home_intro_body = None
        self.home_art = None
        self.home_art_size = None
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
                tk.PhotoImage(file=str(path))
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
                image = tk.PhotoImage(file=str(path))
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
            "☷",
            self.toggle_pause_all,
            width=scale(58, self.ui_scale),
            height=scale(42, self.ui_scale),
            family=self.font_family_var.get(),
            ui_scale=self.ui_scale,
            radius=scale(14, self.ui_scale),
            bg=PALETTE.card_bg,
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
            outline=PALETTE.border_soft,
            bg=PALETTE.card_bg,
            height=scale(40, self.ui_scale),
        )
        search_box.grid(row=0, column=0, sticky="ew")
        search_box.content.grid_columnconfigure(1, weight=1)
        if not self.search_var.get().strip():
            self.search_var.set("搜索提醒")
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
            fg=PALETTE.text_muted if self.search_var.get() == "搜索提醒" else PALETTE.text_primary,
            insertbackground=PALETTE.text_primary,
            relief="flat",
            highlightthickness=0,
            borderwidth=0,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
        )
        self.search_entry.grid(row=0, column=1, sticky="ew")
        tk.Label(
            search_box.content,
            text="≡",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 11, self.ui_scale),
        ).grid(row=0, column=2, sticky="e", padx=(scale(8, self.ui_scale), scale(2, self.ui_scale)))
        self.search_entry.bind("<FocusIn>", self._search_focus_in)
        self.search_entry.bind("<FocusOut>", self._search_focus_out)

        list_shell = tk.Frame(left, bg=PALETTE.card_bg, padx=scale(22, self.ui_scale), pady=scale(6, self.ui_scale))
        list_shell.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(scale(16, self.ui_scale), 0))
        list_shell.grid_rowconfigure(0, weight=1)
        list_shell.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(list_shell, bg=PALETTE.card_bg, highlightthickness=0, bd=0)
        self.task_canvas = canvas
        self.scroll_frame = tk.Frame(canvas, bg=PALETTE.card_bg)
        self.scroll_frame.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas_window = canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(canvas_window, width=event.width),
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        canvas.bind("<MouseWheel>", self._scroll_task_list)
        self.scroll_frame.bind("<MouseWheel>", self._scroll_task_list)

        self.empty_label = tk.Label(
            list_shell,
            text="还没有提醒。\n点左上角新建第一条。",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
            justify=tk.CENTER,
        )

        bottom = tk.Frame(left, bg=PALETTE.card_bg, padx=scale(28, self.ui_scale), pady=scale(18, self.ui_scale))
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew")
        tk.Frame(left, bg=PALETTE.border_soft, height=1).grid(row=3, column=0, columnspan=2, sticky="new")
        tk.Label(
            bottom,
            text="⚙",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            cursor="hand2",
            font=font(self.font_family_var.get(), 15, self.ui_scale),
        ).pack(side=tk.LEFT)
        bottom.winfo_children()[0].bind("<Button-1>", lambda _event: self.open_settings())
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

        quote_block = tk.Frame(panel.content, bg=PALETTE.card_bg)
        quote_block.grid(row=0, column=0, sticky="nw", pady=(scale(36, self.ui_scale), 0))
        tk.Label(
            quote_block,
            textvariable=self.quote_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.font_family_var.get(), 22, self.ui_scale),
            justify=tk.LEFT,
            wraplength=scale(760, self.ui_scale),
        ).pack(anchor="w")
        tk.Label(
            quote_block,
            textvariable=self.quote_source_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 11, self.ui_scale),
            pady=scale(18, self.ui_scale),
        ).pack(anchor="w")

        side = tk.Frame(panel.content, bg=PALETTE.card_bg)
        side.grid(row=0, column=1, sticky="ne", padx=(scale(28, self.ui_scale), 0), pady=(scale(4, self.ui_scale), 0))
        tk.Label(
            side,
            textvariable=self.weather_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
        ).pack(anchor="e")
        tk.Label(
            side,
            textvariable=self.today_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
            pady=scale(46, self.ui_scale),
        ).pack(anchor="e", pady=(0, scale(10, self.ui_scale)))
        tk.Label(
            side,
            textvariable=self.lunar_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.font_family_var.get(), 26, self.ui_scale),
        ).pack(anchor="e")
        tk.Label(
            side,
            textvariable=self.ganzhi_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
            pady=scale(10, self.ui_scale),
        ).pack(anchor="e")

        divider = tk.Frame(panel.content, bg=PALETTE.border_soft, height=1)
        divider.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(scale(34, self.ui_scale), scale(28, self.ui_scale)))

        content = tk.Frame(panel.content, bg=PALETTE.card_bg)
        self.home_intro_content = content
        content.grid(row=2, column=0, columnspan=2, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=0)
        content.grid_rowconfigure(1, weight=1)
        intro_text = tk.Frame(content, bg=PALETTE.card_bg)
        self.home_intro_frame = intro_text
        intro_text.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(scale(8, self.ui_scale), 0))
        intro_text.grid_columnconfigure(0, weight=1)
        self.home_intro_title = tk.Label(
            intro_text,
            textvariable=self.subtitle_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.font_family_var.get(), 15, self.ui_scale, "bold"),
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
            font=font(self.font_family_var.get(), 13, self.ui_scale),
            justify=tk.LEFT,
            anchor="nw",
            pady=scale(18, self.ui_scale),
            wraplength=scale(480, self.ui_scale),
        )
        self.home_intro_body.grid(row=1, column=0, sticky="ew")
        art = tk.Canvas(
            content,
            width=scale(420, self.ui_scale),
            height=scale(220, self.ui_scale),
            bg=PALETTE.card_bg,
            highlightthickness=0,
            bd=0,
        )
        self.home_art = art
        art.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(scale(40, self.ui_scale), 0), pady=(scale(120, self.ui_scale), scale(32, self.ui_scale)))
        self._configure_home_art_size(scale(420, self.ui_scale), scale(220, self.ui_scale))
        content.bind("<Configure>", self._resize_home_intro)

    def _resize_main_layout(self, event: tk.Event) -> None:
        if not self.left_panel:
            return
        min_width = scale(460, self.ui_scale)
        max_width = scale(920, self.ui_scale)
        target_width = min(max_width, max(min_width, int(event.width * 0.30)))
        if abs(self.left_panel.winfo_width() - target_width) > 1:
            self.left_panel.configure(width=target_width)

    def _resize_home_intro(self, event: tk.Event) -> None:
        if not all((self.home_intro_content, self.home_intro_frame, self.home_intro_title, self.home_intro_body, self.home_art)):
            return
        narrow = event.width < scale(1500, self.ui_scale)
        compact_height = event.height < scale(620, self.ui_scale)
        tight_height = event.height < scale(360, self.ui_scale)
        if tight_height:
            art_width = scale(160, self.ui_scale)
            art_height = scale(84, self.ui_scale)
        else:
            art_width = scale(320 if narrow or compact_height else 420, self.ui_scale)
            art_height = scale(168 if narrow or compact_height else 220, self.ui_scale)
        self._configure_home_art_size(art_width, art_height)
        if narrow:
            wrap_width = max(scale(320, self.ui_scale), event.width - scale(48, self.ui_scale))
            self.home_intro_content.grid_columnconfigure(0, weight=1)
            self.home_intro_content.grid_columnconfigure(1, weight=0)
            self.home_intro_frame.grid_configure(
                row=0,
                column=0,
                rowspan=1,
                sticky="new",
                padx=(scale(8, self.ui_scale), scale(8, self.ui_scale)),
            )
            self.home_art.grid()
            self.home_art.grid_configure(
                row=1,
                column=0,
                rowspan=1,
                sticky="ne",
                padx=(0, scale(8, self.ui_scale)),
                pady=(scale(12 if tight_height else 22, self.ui_scale), scale(18 if tight_height else 32, self.ui_scale)),
            )
        else:
            wrap_width = max(scale(420, self.ui_scale), event.width - scale(560, self.ui_scale))
            self.home_intro_content.grid_columnconfigure(0, weight=1)
            self.home_intro_content.grid_columnconfigure(1, weight=0)
            self.home_intro_frame.grid_configure(
                row=0,
                column=0,
                rowspan=2,
                sticky="nw",
                padx=(scale(8, self.ui_scale), 0),
            )
            self.home_art.grid()
            self.home_art.grid_configure(
                row=0,
                column=1,
                rowspan=2,
                sticky="ne",
                padx=(scale(40, self.ui_scale), 0),
                pady=(scale(120, self.ui_scale), scale(32, self.ui_scale)),
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
        state = "已暂停全部提醒" if self.scheduler.paused else "按计划运行中"
        self.summary_var.set(f"共 {len(self.tasks)} 条提醒，启用 {enabled_count} 条，{state}")
        self.subtitle_var.set(f"{APP_NAME} · 一个安静的提醒工具")
        if self.pause_button:
            self.pause_button.set_text("恢复" if self.scheduler.paused else "暂停")
        if self.empty_label:
            if filtered_tasks:
                self.empty_label.place_forget()
            else:
                self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.tray.refresh()

    def _refresh_clock(self) -> None:
        # 每次读取系统时间，跨日、休眠恢复和校时后都显示当前值。
        now = datetime.now()
        weekday = "一二三四五六日"[now.weekday()]
        self.today_var.set(f"{now:%Y.%m.%d}  周{weekday}\n{now:%H:%M:%S}")
        self._drain_live_info()
        self._refresh_live_info(now)
        self._clock_after_id = self.root.after(1000, self._refresh_clock)

    def _refresh_live_info(self, now: datetime) -> None:
        # 天气展示后台线程拉到的结果，数据是否过期由 Weather.display 统一判定。
        if self._weather is not None:
            self.weather_var.set(self._weather.display(now))
        elif not self._weather_failed:
            self.weather_var.set("天气获取中…")
        if now.date() != self._lunar_day:
            self._lunar_day = now.date()
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
        self._schedule_weather_refresh(WEATHER_REFRESH_SECONDS)

    def _schedule_weather_refresh(self, delay: int) -> None:
        self._weather_after_id = self.root.after(delay * 1000, self._refresh_weather)

    def _refresh_lunar_async(self, day: date) -> None:
        def worker() -> None:
            try:
                month_day, ganzhi = lunar_date(day)
            except Exception as exc:
                logging.getLogger(__name__).info("农历获取失败：%s", exc)
                return
            self._live_info_queue.put(("lunar", (month_day, ganzhi)))

        threading.Thread(target=worker, daemon=True, name="live-info-lunar").start()

    def _apply_lunar(self, month_day: str, ganzhi: str) -> None:
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
            else:
                self._apply_lunar(*payload)

    def _render_task_card(self, task: Task, index: int) -> None:
        if not self.scroll_frame:
            return
        is_selected = task.id == self.selected_task_id
        bg = PALETTE.card_alt_bg if is_selected else PALETTE.card_bg
        is_effectively_active = task.enabled and not self.scheduler.paused
        title_fg = PALETTE.text_primary if is_effectively_active else PALETTE.text_muted
        desc_fg = PALETTE.text_secondary if is_effectively_active else PALETTE.text_muted
        icon_fg = PALETTE.accent_text if is_effectively_active else PALETTE.text_muted
        if self.scheduler.paused and task.enabled:
            status_text = "暂停"
        else:
            status_text = "启用" if task.enabled else "停用"
        status_fg = PALETTE.accent_text if is_effectively_active else PALETTE.text_muted
        card_height = scale(132, self.ui_scale)
        description_width = scale(210, self.ui_scale)
        card_shell = RoundedFrame(
            self.scroll_frame,
            radius=scale(12, self.ui_scale),
            padding=scale(8, self.ui_scale),
            fill=bg,
            outline=PALETTE.border if is_selected else PALETTE.border_soft,
            shadow=is_selected,
            bg=PALETTE.card_bg,
            height=card_height,
            cursor="hand2",
        )
        card = card_shell.content
        card_shell.grid(row=index, column=0, sticky="ew", pady=(0, scale(10, self.ui_scale)))
        card.grid_columnconfigure(2, weight=1, minsize=description_width)
        card.grid_columnconfigure(3, weight=0)
        card.grid_columnconfigure(4, weight=0)
        card.grid_rowconfigure(1, weight=1)
        self.scroll_frame.grid_columnconfigure(0, weight=1)

        icon = tk.Label(
            card,
            text=self._task_icon(task),
            bg=bg,
            fg=icon_fg,
            font=font(self.font_family_var.get(), 14, self.ui_scale),
        )
        icon.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, scale(10, self.ui_scale)), pady=(scale(8, self.ui_scale), 0))
        time_box = tk.Frame(card, bg=bg)
        time_box.grid(row=0, column=1, rowspan=2, sticky="nw", padx=(0, scale(14, self.ui_scale)), pady=(scale(5, self.ui_scale), 0))
        time_label = tk.Label(
            time_box,
            text=self._task_time(task),
            bg=bg,
            fg=desc_fg,
            font=font(self.font_family_var.get(), 10, self.ui_scale),
        )
        time_label.pack(anchor="w")
        tk.Label(
            time_box,
            text=self._task_time(task),
            bg=bg,
            fg=PALETTE.text_muted,
            font=font(self.font_family_var.get(), 8, self.ui_scale),
        ).pack(anchor="w")
        title_label = tk.Label(
            card,
            text=task.title,
            bg=bg,
            fg=title_fg,
            font=font(self.font_family_var.get(), 12, self.ui_scale, "bold"),
            anchor="w",
        )
        title_label.grid(row=0, column=2, sticky="ew", pady=(scale(5, self.ui_scale), 0), padx=(0, scale(8, self.ui_scale)))
        desc_label = tk.Label(
            card,
            text=self._describe_card_detail(task),
            bg=bg,
            fg=desc_fg,
            font=font(self.font_family_var.get(), 9, self.ui_scale),
            wraplength=description_width,
            justify=tk.LEFT,
            anchor="nw",
        )
        desc_label.grid(row=1, column=2, sticky="new", pady=(scale(4, self.ui_scale), 0), padx=(0, scale(8, self.ui_scale)))
        card.bind(
            "<Configure>",
            lambda event, label=desc_label: label.configure(
                wraplength=max(scale(210, self.ui_scale), event.width - scale(190, self.ui_scale))
            ),
            add="+",
        )
        status_label = tk.Label(
            card,
            text=status_text,
            bg=bg,
            fg=status_fg,
            font=font(self.font_family_var.get(), 8, self.ui_scale),
            cursor="hand2",
        )
        status_label.grid(row=0, column=3, sticky="ne", padx=(scale(4, self.ui_scale), scale(8, self.ui_scale)), pady=(scale(7, self.ui_scale), 0))
        status_label.bind("<Button-1>", lambda _event, task_id=task.id: self._toggle_task_by_id(task_id))
        status_label.bind("<MouseWheel>", self._scroll_task_list)
        action_button = tk.Label(
            card,
            text="⋮",
            bg=bg,
            fg=PALETTE.text_secondary,
            font=font(self.font_family_var.get(), 11, self.ui_scale),
            cursor="hand2",
        )
        action_button.bind("<Button-1>", lambda _event, task_id=task.id: self._open_task_menu(task_id))
        action_button.grid(row=0, column=4, rowspan=2, sticky="ne", pady=(scale(5, self.ui_scale), 0))
        action_button.bind("<MouseWheel>", self._scroll_task_list)

        for widget in (card_shell, card, icon, time_box, time_label, title_label, desc_label):
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
        photo = ImageTk.PhotoImage(image)
        self.tk_images.append(photo)
        canvas.delete("all")
        canvas.create_image(canvas_width // 2, canvas_height // 2, image=photo, anchor="center")
        return True

    def _select_task(self, task_id: str) -> None:
        self.selected_task_id = task_id
        self._refresh_ui()

    def _scroll_task_list(self, event) -> str:
        if self.task_canvas is not None:
            self.task_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _open_task_menu(self, task_id: str) -> None:
        self.selected_task_id = task_id
        task = self._selected_task()
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="编辑", command=self.edit_selected_task)
        menu.add_command(label="停用提醒" if task and task.enabled else "启用提醒", command=self.toggle_selected_task)
        menu.add_command(label="删除", command=self.delete_selected_task)
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _edit_task_by_id(self, task_id: str) -> None:
        self.selected_task_id = task_id
        self.edit_selected_task()

    def _toggle_task_by_id(self, task_id: str) -> None:
        self.selected_task_id = task_id
        self.toggle_selected_task()

    def _visible_tasks(self) -> list[Task]:
        keyword = self.search_var.get().strip()
        if not keyword or keyword == "搜索提醒":
            return self.tasks
        keyword_lower = keyword.lower()
        return [
            task
            for task in self.tasks
            if keyword_lower in task.title.lower()
            or keyword_lower in (task.body or "").lower()
            or keyword_lower in self._describe_trigger(task).lower()
            or keyword_lower in self._describe_next_run(task).lower()
        ]

    def _search_focus_in(self, _event) -> None:
        if self.search_entry is None:
            return
        if self.search_var.get() == "搜索提醒":
            self.search_var.set("")
        self.search_entry.configure(fg=PALETTE.text_primary)

    def _search_focus_out(self, _event) -> None:
        if self.search_entry is None:
            return
        if not self.search_var.get().strip():
            self.search_var.set("搜索提醒")
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

    def _task_time(self, task: Task) -> str:
        config = task.trigger_config
        if task.trigger_type == "once":
            return str(config.get("time", "--:--"))[:5]
        if task.trigger_type in {"weekly", "weekly_last_workday", "yearly"}:
            hour = int(config.get("hour", 0))
            minute = int(config.get("minute", 0))
            return f"{hour:02d}:{minute:02d}"
        if task.trigger_type == "interval":
            minutes = int(config.get("minutes", 0))
            hours = int(config.get("hours", 0))
            if hours:
                return f"每 {hours} 小时"
            if minutes:
                return f"每 {minutes} 分钟"
        return "提醒"

    def _describe_card_detail(self, task: Task) -> str:
        return f"{self._describe_trigger(task)}\n{self._describe_next_run(task)}"

    def _describe_next_run(self, task: Task) -> str:
        if self.scheduler.paused and task.enabled:
            return "下次提醒：全部提醒已暂停"
        if not task.enabled:
            return "下次提醒：未启用"
        if not task.next_run:
            return "下次提醒：暂无"
        try:
            run_at = datetime.fromisoformat(task.next_run)
            return f"下次提醒：{run_at:%Y-%m-%d %H:%M}"
        except ValueError:
            return f"下次提醒：{task.next_run}"

    def _schedule_refresh(self) -> None:
        self.root.after(0, self._sync_from_scheduler)

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
        self.quote_source_var.set(f"—— {source}")

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

    def add_task(self) -> None:
        dialog = TaskDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.result:
            try:
                self.tasks.append(dialog.result)
                self.selected_task_id = dialog.result.id
                self.scheduler.upsert_task(dialog.result)
            except Exception as exc:
                self.tasks = [task for task in self.tasks if task.id != dialog.result.id]
                show_error(self.root, "添加提醒", f"保存失败：{exc}")

    def edit_selected_task(self) -> None:
        task = self._selected_task()
        if not task:
            return
        dialog = TaskDialog(self.root, task=task)
        self.root.wait_window(dialog)
        if dialog.result:
            index = self.tasks.index(task)
            try:
                self.tasks[index] = dialog.result
                self.selected_task_id = dialog.result.id
                self.scheduler.upsert_task(dialog.result)
            except Exception as exc:
                self.tasks[index] = task
                show_error(self.root, "编辑提醒", f"保存失败：{exc}")

    def delete_selected_task(self) -> None:
        task = self._selected_task()
        if not task:
            return
        if not ask_yes_no(self.root, "删除提醒", f"确认删除“{task.title}”吗？"):
            return
        self.tasks = [item for item in self.tasks if item.id != task.id]
        self.selected_task_id = self.tasks[0].id if self.tasks else None
        self.scheduler.remove_task(task.id)

    def toggle_selected_task(self) -> None:
        task = self._selected_task()
        if not task:
            return
        previous = task.enabled
        try:
            task.enabled = not task.enabled
            self.scheduler.upsert_task(task)
            self._refresh_ui()
        except Exception as exc:
            task.enabled = previous
            show_error(self.root, "更新提醒", f"状态切换失败：{exc}")

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
        if self._clock_after_id is not None:
            self.root.after_cancel(self._clock_after_id)
            self._clock_after_id = None
        if self._weather_after_id is not None:
            self.root.after_cancel(self._weather_after_id)
            self._weather_after_id = None
        self.tray.stop()
        stop_helper()
        self.scheduler.shutdown()
        self._persist_tasks()
        self.root.after(0, self.root.destroy)

    def request_exit_app(self) -> None:
        self.root.after(0, self.exit_app)

    def _selected_task(self) -> Task | None:
        if not self.selected_task_id:
            show_info(self.root, "提示", "请先选择一条提醒。")
            return None
        return next((task for task in self.tasks if task.id == self.selected_task_id), None)

    def _describe_trigger(self, task: Task) -> str:
        trigger_name = TRIGGER_TYPES.get(task.trigger_type, task.trigger_type)
        config = task.trigger_config
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
