from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui.theme import PALETTE, PillButton, RoundedFrame, apply_ttk_theme, center_to_parent, enable_window_drag, font, scale


class MessageDialog(tk.Toplevel):
    ICONS = {
        "info": "i",
        "warning": "!",
        "error": "!",
        "confirm": "?",
    }

    def __init__(
        self,
        master: tk.Misc,
        *,
        title: str,
        message: str,
        kind: str = "info",
        buttons: list[tuple[str, object, str]] | None = None,
    ) -> None:
        super().__init__(master)
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.overrideredirect(True)
        self.configure(bg=PALETTE.card_bg)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.result: object | None = None
        self.kind = kind
        self.message = message
        self.ui_family = getattr(master, "font_family_var", None).get() if hasattr(master, "font_family_var") else "Microsoft YaHei UI"
        self.ui_scale = float(getattr(master, "ui_scale", 1.0))
        self.buttons = buttons or [("知道了", True, "Primary.TButton")]
        apply_ttk_theme(ttk.Style(self), self.ui_family, self.ui_scale)

        self._build(title)
        center_to_parent(self, self.master if isinstance(self.master, tk.Misc) else None)
        self.deiconify()
        self._raise_dialog()
        self.grab_set()
        self.focus_force()
        self.bind("<Escape>", lambda _event: self._cancel())

    def _build(self, title: str) -> None:
        shell = tk.Frame(self, bg=PALETTE.card_bg, padx=0, pady=0)
        shell.pack(fill=tk.BOTH, expand=True)

        card_shell = RoundedFrame(
            shell,
            radius=scale(14, self.ui_scale),
            padding=scale(20, self.ui_scale),
            fill=PALETTE.card_bg,
            outline=PALETTE.border,
            shadow=False,
            bg=PALETTE.card_bg,
            width=scale(330, self.ui_scale),
            height=scale(190, self.ui_scale),
        )
        card_shell.pack(fill=tk.BOTH, expand=True)
        card = card_shell.content

        header = tk.Frame(card, bg=PALETTE.card_bg)
        header.pack(fill=tk.X)
        enable_window_drag(self, shell, card_shell, card, header)

        tk.Label(
            header,
            text=self.ICONS.get(self.kind, "i"),
            width=2,
            bg=PALETTE.accent_soft,
            fg=PALETTE.accent_text if self.kind != "error" else PALETTE.danger_text,
            font=font(self.ui_family, 10, self.ui_scale, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text=title,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.ui_family, 12, self.ui_scale, "bold"),
            padx=scale(10, self.ui_scale),
        ).pack(side=tk.LEFT)
        close_label = tk.Label(
            header,
            text="×",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.ui_family, 12, self.ui_scale),
            cursor="hand2",
            padx=scale(8, self.ui_scale),
        )
        close_label.pack(side=tk.RIGHT)
        close_label.bind("<Button-1>", lambda _event: self._cancel())

        tk.Label(
            card,
            text=self.message,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_secondary,
            font=font(self.ui_family, 10, self.ui_scale),
            justify=tk.LEFT,
            wraplength=scale(340, self.ui_scale),
            pady=scale(18, self.ui_scale),
        ).pack(anchor="w")

        actions = tk.Frame(card, bg=PALETTE.card_bg)
        actions.pack(fill=tk.X)
        for index, (label, value, style) in enumerate(self.buttons):
            actions.grid_columnconfigure(index, weight=1)
            PillButton(
                actions,
                label,
                lambda result=value: self._finish(result),
                width=scale(96, self.ui_scale),
                height=scale(34, self.ui_scale),
                family=self.ui_family,
                ui_scale=self.ui_scale,
                fill=PALETTE.accent_soft if style == "Primary.TButton" else PALETTE.card_bg,
                active_fill="#e5d5c0" if style == "Primary.TButton" else "#faf6ef",
                radius=scale(9, self.ui_scale),
                bg=PALETTE.card_bg,
            ).grid(row=0, column=index, sticky="ew", padx=(scale(10, self.ui_scale), 0) if index else 0)

    def _finish(self, result: object) -> None:
        self.result = result
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _raise_dialog(self) -> None:
        try:
            self.lift(self.master)
            self.attributes("-topmost", True)
        except tk.TclError:
            pass


def show_info(master: tk.Misc, title: str, message: str) -> None:
    dialog = MessageDialog(master, title=title, message=message, kind="info")
    master.wait_window(dialog)


def show_warning(master: tk.Misc, title: str, message: str) -> None:
    dialog = MessageDialog(master, title=title, message=message, kind="warning")
    master.wait_window(dialog)


def show_error(master: tk.Misc, title: str, message: str) -> None:
    dialog = MessageDialog(master, title=title, message=message, kind="error")
    master.wait_window(dialog)


def ask_yes_no(master: tk.Misc, title: str, message: str) -> bool:
    dialog = MessageDialog(
        master,
        title=title,
        message=message,
        kind="confirm",
        buttons=[("取消", False, "Secondary.TButton"), ("确认", True, "Primary.TButton")],
    )
    master.wait_window(dialog)
    return bool(dialog.result)
