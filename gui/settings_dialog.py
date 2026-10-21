from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, ttk

from PIL import Image, ImageDraw, ImageTk

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
        # Tk 原生下拉列表负责外部点击、Esc、失焦和 modal grab 的配对恢复。
        for option, value in {
            "font": font(family, 10, ui_scale),
            "background": PALETTE.card_bg,
            "foreground": PALETTE.text_primary,
            "selectBackground": PALETTE.accent_soft,
            "selectForeground": PALETTE.text_primary,
        }.items():
            self.option_add(f"*TCombobox*Listbox.{option}", value)
        style = ttk.Style(self)
        style_name = f"FontPicker{round(ui_scale * 100)}.TCombobox"
        arrow_element = f"FontPicker{round(ui_scale * 100)}.downarrow"
        if arrow_element not in style.element_names():
            # 高分辨率绘制后缩小，保持细线箭头在不同 DPI 下平滑。
            image_width, image_height = scale(26, ui_scale), scale(18, ui_scale)
            arrow_images = []
            for color in (PALETTE.text_secondary, PALETTE.text_primary, PALETTE.text_muted):
                image = Image.new("RGBA", (image_width * 4, image_height * 4))
                draw = ImageDraw.Draw(image)
                points = [(scale(x, ui_scale) * 4, scale(y, ui_scale) * 4)
                          for x, y in ((8, 7), (12, 11), (16, 7))]
                draw.line(points, fill=color, width=max(4, round(5 * ui_scale)), joint="curve")
                image = image.resize((image_width, image_height), Image.Resampling.LANCZOS)
                arrow_images.append(ImageTk.PhotoImage(image, master=self))
            style.element_create(arrow_element, "image", arrow_images[0],
                                 ("active", arrow_images[1]), ("disabled", arrow_images[2]))
            # ttk 元素会跨设置窗口重用，图片须与 Tk 根窗口同寿命。
            root = self._root()
            if not hasattr(root, "_font_picker_arrow_images"):
                root._font_picker_arrow_images = []
            root._font_picker_arrow_images.extend(arrow_images)
        style.layout(style_name, [
            ("Combobox.field", {"sticky": "nswe", "children": [
                (arrow_element, {"side": "right", "sticky": ""}),
                ("Combobox.padding", {"sticky": "nswe", "children": [
                    ("Combobox.textarea", {"sticky": "nswe"}),
                ]}),
            ]}),
        ])
        self.combobox = ttk.Combobox(
            self, textvariable=variable, values=self.values, state="readonly",
            height=10, font=font(family, 10, ui_scale), style=style_name,
        )
        self.combobox.pack(fill=tk.BOTH, expand=True)

    def set_values(self, values: list[str]) -> None:
        self.values = list(values)
        self.combobox.configure(values=self.values)


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
        self._topmost_after_id: str | None = None
        self.bind("<Destroy>", self._cancel_topmost_reset, add="+")
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
            outline=PALETTE.border,
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
            if self._topmost_after_id is not None:
                self.after_cancel(self._topmost_after_id)
            self._topmost_after_id = self.after(120, self._reset_topmost)
        except tk.TclError:
            pass

    def _reset_topmost(self) -> None:
        self._topmost_after_id = None
        self.attributes("-topmost", False)

    def _cancel_topmost_reset(self, event: tk.Event) -> None:
        if event.widget is self and self._topmost_after_id is not None:
            self.after_cancel(self._topmost_after_id)
            self._topmost_after_id = None

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
        self._raise_dialog()
        try:
            self.grab_set()
            self.focus_force()
        except tk.TclError:
            pass
