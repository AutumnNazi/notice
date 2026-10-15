from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, ttk

from core.font_manager import register_font_directory
from gui.message_dialog import show_warning
from gui.theme import (
    PALETTE,
    PillButton,
    RoundedFrame,
    RoundedSelect,
    ToggleSwitch,
    apply_ttk_theme,
    center_to_parent,
    enable_window_drag,
    font,
    scale,
)


class FontPicker(tk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        *,
        variable: tk.StringVar,
        values: list[str],
        width: int,
        height: int,
        family: str,
        ui_scale: float,
        bg: str | None = None,
    ) -> None:
        super().__init__(master, width=width, height=height, bg=bg or PALETTE.card_bg)
        self.grid_propagate(False)
        self.pack_propagate(False)
        self.variable = variable
        self.values = list(values)
        self.family = family
        self.ui_scale = ui_scale
        self.popup: tk.Toplevel | None = None
        self.search_var = tk.StringVar()

        self.shell = RoundedFrame(
            self,
            radius=scale(10, ui_scale),
            padding=scale(8, ui_scale),
            fill=PALETTE.card_bg,
            outline=PALETTE.border,
            bg=bg or PALETTE.card_bg,
            width=width,
            height=height,
            cursor="hand2",
        )
        self.shell.pack(fill=tk.BOTH, expand=True)
        self.shell.content.grid_columnconfigure(0, weight=1)
        self.label = tk.Label(
            self.shell.content,
            text=self.variable.get(),
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(family, 10, ui_scale),
            anchor="w",
        )
        self.label.grid(row=0, column=0, sticky="ew")
        self.arrow = tk.Label(
            self.shell.content,
            text="⌄",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(family, 11, ui_scale),
        )
        self.arrow.grid(row=0, column=1, sticky="e", padx=(scale(8, ui_scale), 0))

        for widget in (self, self.shell, self.shell.content, self.label, self.arrow):
            widget.bind("<Button-1>", self._open_popup)
        self.variable.trace_add("write", lambda *_args: self._sync_label())

    def _sync_label(self) -> None:
        self.label.configure(text=self.variable.get())

    def set_values(self, values: list[str]) -> None:
        self.values = list(values)
        if self.popup is not None and self.popup.winfo_exists():
            self._refresh_popup_values()

    def _open_popup(self, _event: tk.Event | None = None) -> None:
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.destroy()
            self.popup = None
            return

        self.popup = tk.Toplevel(self)
        self.popup.overrideredirect(True)
        self.popup.configure(bg=PALETTE.card_bg)
        self.popup.transient(self.winfo_toplevel())
        popup_width = max(self.winfo_width(), scale(260, self.ui_scale))
        popup_height = scale(260, self.ui_scale)
        self.popup.geometry(f"{popup_width}x{popup_height}+{self.winfo_rootx()}+{self.winfo_rooty() + self.winfo_height() + scale(6, self.ui_scale)}")

        container = RoundedFrame(
            self.popup,
            radius=scale(12, self.ui_scale),
            padding=scale(10, self.ui_scale),
            fill=PALETTE.card_bg,
            outline=PALETTE.border,
            shadow=True,
            bg=PALETTE.card_bg,
            width=popup_width,
            height=popup_height,
        )
        container.pack(fill=tk.BOTH, expand=True)
        body = container.content
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        search = tk.Entry(
            body,
            textvariable=self.search_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            insertbackground=PALETTE.text_primary,
            relief="flat",
            highlightthickness=1,
            highlightbackground=PALETTE.border_soft,
            highlightcolor=PALETTE.border,
            font=font(self.family, 10, self.ui_scale),
        )
        search.grid(row=0, column=0, columnspan=2, sticky="ew", ipady=scale(5, self.ui_scale), pady=(0, scale(8, self.ui_scale)))

        self.listbox = tk.Listbox(
            body,
            activestyle="none",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            selectbackground=PALETTE.accent_soft,
            selectforeground=PALETTE.text_primary,
            highlightthickness=0,
            relief="flat",
            borderwidth=0,
            exportselection=False,
            font=font(self.family, 10, self.ui_scale),
        )
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

        self.search_var.set("")
        self.search_var.trace_add("write", lambda *_args: self._refresh_popup_values())
        self._refresh_popup_values()
        self.listbox.bind("<Double-Button-1>", lambda _event: self._choose_active())
        self.listbox.bind("<Return>", lambda _event: self._choose_active())
        self.popup.bind("<Escape>", lambda _event: self._close_popup())
        search.bind("<Return>", lambda _event: self._choose_active())
        search.focus_set()

    def _refresh_popup_values(self) -> None:
        if not hasattr(self, "listbox"):
            return
        keyword = self.search_var.get().strip().lower()
        filtered = [value for value in self.values if keyword in value.lower()]
        self.listbox.delete(0, tk.END)
        for value in filtered:
            self.listbox.insert(tk.END, value)
        current = self.variable.get()
        if current in filtered:
            index = filtered.index(current)
            self.listbox.selection_set(index)
            self.listbox.see(index)
        elif filtered:
            self.listbox.selection_set(0)

    def _choose_active(self) -> str:
        selection = self.listbox.curselection()
        if not selection:
            return "break"
        self.variable.set(self.listbox.get(selection[0]))
        self._close_popup()
        return "break"

    def _close_popup(self) -> None:
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.destroy()
        self.popup = None


class SettingsDialog(tk.Toplevel):
    CLOSE_ACTION_LABELS = {
        "ask": "每次询问",
        "tray": "缩小到托盘",
        "exit": "直接关闭",
    }
    CLOSE_ACTION_VALUES = {value: key for key, value in CLOSE_ACTION_LABELS.items()}

    def __init__(
        self,
        master: tk.Misc,
        *,
        font_choices: list[str],
        selected_font: str,
        autostart_enabled: bool,
        custom_font_dir: str,
        close_action: str,
    ) -> None:
        super().__init__(master)
        self.withdraw()
        self.title("设置")
        self.resizable(False, False)
        self.transient(master)
        self.overrideredirect(True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.configure(bg=PALETTE.card_bg)

        self.result: dict[str, object] | None = None
        self.ui_family = getattr(master, "font_family_var", None).get() if hasattr(master, "font_family_var") else "Microsoft YaHei UI"
        self.ui_scale = float(getattr(master, "ui_scale", 1.0))
        apply_ttk_theme(ttk.Style(self), self.ui_family, self.ui_scale)
        self.font_choices = list(font_choices)

        self.font_family_var = tk.StringVar(value=selected_font)
        self.autostart_enabled_var = tk.BooleanVar(value=autostart_enabled)
        self.custom_font_dir_var = tk.StringVar(value=custom_font_dir)
        self.close_action_var = tk.StringVar(value=self.CLOSE_ACTION_LABELS.get(close_action or "ask", "每次询问"))
        self.font_dir_hint_var = tk.StringVar(value=self._font_dir_hint(custom_font_dir))

        self.font_box: FontPicker | None = None
        self._build()
        center_to_parent(self, self.master if isinstance(self.master, tk.Misc) else None)
        self.deiconify()
        self._raise_dialog()
        self.grab_set()
        self.focus_force()
        self.bind("<Escape>", lambda _event: self.destroy())

    def _build(self) -> None:
        shell = tk.Frame(self, bg=PALETTE.card_bg, padx=0, pady=0)
        shell.pack(fill=tk.BOTH, expand=True)

        card_shell = RoundedFrame(
            shell,
            radius=scale(14, self.ui_scale),
            padding=scale(22, self.ui_scale),
            fill=PALETTE.card_bg,
            outline=PALETTE.border_soft,
            shadow=False,
            bg=PALETTE.card_bg,
            width=scale(440, self.ui_scale),
            height=scale(420, self.ui_scale),
        )
        card_shell.pack(fill=tk.BOTH, expand=True)
        card = card_shell.content
        card.grid_columnconfigure(1, weight=1)
        enable_window_drag(self, shell, card_shell, card)

        tk.Label(
            card,
            text="设置",
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

        body = tk.Frame(card, bg=PALETTE.card_bg)
        body.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(scale(18, self.ui_scale), 0))
        body.grid_columnconfigure(0, weight=1)

        form = tk.Frame(body, bg=PALETTE.card_bg)
        form.grid(row=0, column=0, sticky="nsew")
        form.grid_columnconfigure(1, weight=1)

        grid_row = 0
        self._row_label(form, grid_row, "界面字体")
        self.font_box = FontPicker(
            form,
            variable=self.font_family_var,
            values=self.font_choices,
            width=scale(220, self.ui_scale),
            height=scale(36, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            bg=PALETTE.card_bg,
        )
        self.font_box.grid(row=grid_row, column=1, sticky="w", pady=(0, scale(14, self.ui_scale)))

        grid_row += 1
        self._row_label(form, grid_row, "开机启动")
        toggle_holder = tk.Frame(form, bg=PALETTE.card_bg, width=scale(220, self.ui_scale), height=scale(24, self.ui_scale))
        toggle_holder.grid(row=grid_row, column=1, sticky="w", pady=(0, scale(18, self.ui_scale)))
        toggle_holder.grid_propagate(False)
        toggle_holder.pack_propagate(False)
        ToggleSwitch(
            toggle_holder,
            variable=self.autostart_enabled_var,
            width=scale(44, self.ui_scale),
            height=scale(24, self.ui_scale),
            ui_scale=self.ui_scale,
            bg=PALETTE.card_bg,
        ).pack(side=tk.RIGHT)

        grid_row += 1
        self._row_label(form, grid_row, "点击关闭时")
        RoundedSelect(
            form,
            variable=self.close_action_var,
            values=list(self.CLOSE_ACTION_VALUES.keys()),
            width=scale(220, self.ui_scale),
            height=scale(36, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            bg=PALETTE.card_bg,
        ).grid(row=grid_row, column=1, sticky="w", pady=(0, scale(14, self.ui_scale)))

        grid_row += 1
        self._row_label(form, grid_row, "字体目录")
        PillButton(
            form,
            "选择目录",
            self._pick_font_dir,
            width=scale(220, self.ui_scale),
            height=scale(36, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            radius=scale(10, self.ui_scale),
            bg=PALETTE.card_bg,
        ).grid(row=grid_row, column=1, sticky="w", pady=(0, scale(6, self.ui_scale)))

        grid_row += 1
        tk.Label(
            form,
            textvariable=self.font_dir_hint_var,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.ui_family, 8, self.ui_scale),
            wraplength=scale(280, self.ui_scale),
            justify=tk.LEFT,
        ).grid(row=grid_row, column=1, sticky="w", pady=(0, scale(18, self.ui_scale)))

        grid_row += 1
        actions = tk.Frame(form, bg=PALETTE.card_bg)
        actions.grid(row=grid_row, column=0, columnspan=2, sticky="ew", pady=(scale(14, self.ui_scale), 0))
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

    def _row_label(self, parent: tk.Misc, row: int, text: str) -> None:
        tk.Label(
            parent,
            text=text,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.ui_family, 10, self.ui_scale),
        ).grid(row=row, column=0, sticky="w", pady=(0, scale(14, self.ui_scale)), padx=(0, scale(32, self.ui_scale)))

    def _pick_font_dir(self) -> None:
        self._release_modal_for_native_dialog()
        try:
            selected = filedialog.askdirectory(parent=self.master, title="选择自定义字体目录")
        finally:
            self._restore_modal_after_native_dialog()
        if not selected:
            return
        font_dir = Path(selected)
        loaded = register_font_directory(font_dir)
        if not loaded:
            show_warning(self, "导入字体", "这个目录里没有可注册的 .ttf 或 .otf 字体文件。")
            return
        self.custom_font_dir_var.set(str(font_dir))
        self.font_dir_hint_var.set(self._font_dir_hint(str(font_dir)))
        current_families = sorted(set(tkfont.families(self)))
        self.font_choices = sorted(set(self.font_choices + current_families))
        if self.font_box is not None:
            self.font_box.set_values(self.font_choices)
        if self.font_family_var.get() not in self.font_choices and self.font_choices:
            self.font_family_var.set(self.font_choices[0])

    def _save(self) -> None:
        self.result = {
            "font_family": self.font_family_var.get(),
            "autostart_enabled": self.autostart_enabled_var.get(),
            "custom_font_dir": self.custom_font_dir_var.get().strip(),
            "close_action": self.CLOSE_ACTION_VALUES[self.close_action_var.get()],
        }
        self.destroy()

    def _font_dir_hint(self, value: str) -> str:
        path = value.strip()
        return f"当前目录：{path}" if path else "当前目录：未选择"

    def _raise_dialog(self) -> None:
        try:
            self.lift(self.master)
            self.attributes("-topmost", True)
            self.after(120, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _release_modal_for_native_dialog(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.attributes("-topmost", False)
            self.lower(self.master)
        except tk.TclError:
            pass
        self.update_idletasks()

    def _restore_modal_after_native_dialog(self) -> None:
        try:
            self.lift(self.master)
            self.attributes("-topmost", True)
            self.after(120, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            pass
        try:
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            pass
