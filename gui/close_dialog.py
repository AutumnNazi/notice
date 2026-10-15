from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui.theme import PALETTE, PillButton, RoundedFrame, apply_ttk_theme, center_to_parent, enable_window_drag, font, scale


class CloseChoiceDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, ui_family: str) -> None:
        super().__init__(master)
        self.withdraw()
        self.title("关闭方式")
        self.resizable(False, False)
        self.transient(master)
        self.overrideredirect(True)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.configure(bg=PALETTE.card_bg)

        self.result: dict[str, object] | None = None
        self.remember_var = tk.BooleanVar(value=False)
        self.remember_indicator: tk.Label | None = None
        self.ui_family = ui_family or "Microsoft YaHei UI"
        self.ui_scale = float(getattr(master, "ui_scale", 1.0))
        apply_ttk_theme(ttk.Style(self), self.ui_family, self.ui_scale)

        self._build()
        self._refresh_remember_indicator()
        center_to_parent(self, self.master if isinstance(self.master, tk.Misc) else None)
        self.deiconify()
        self._raise_dialog()
        self.grab_set()
        self.focus_force()
        self.bind("<Escape>", lambda _event: self._cancel())

    def _build(self) -> None:
        shell = tk.Frame(self, bg=PALETTE.card_bg, padx=0, pady=0)
        shell.pack(fill=tk.BOTH, expand=True)

        card_shell = RoundedFrame(
            shell,
            radius=scale(14, self.ui_scale),
            padding=scale(20, self.ui_scale),
            fill=PALETTE.card_bg,
            outline=PALETTE.border_soft,
            shadow=False,
            bg=PALETTE.card_bg,
            width=scale(300, self.ui_scale),
            height=scale(276, self.ui_scale),
        )
        card_shell.pack(fill=tk.BOTH, expand=True)
        card = card_shell.content

        header = tk.Frame(card, bg=PALETTE.card_bg)
        header.pack(fill=tk.X)
        enable_window_drag(self, shell, card_shell, card, header)
        tk.Label(
            header,
            text="",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.ui_family, 12, self.ui_scale, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text="×",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_muted,
            font=font(self.ui_family, 12, self.ui_scale),
            cursor="hand2",
            padx=scale(8, self.ui_scale),
        ).pack(side=tk.RIGHT)
        header.winfo_children()[-1].bind("<Button-1>", lambda _event: self._cancel())

        badge = tk.Canvas(
            card,
            width=scale(50, self.ui_scale),
            height=scale(50, self.ui_scale),
            bg=PALETTE.card_bg,
            highlightthickness=0,
            bd=0,
        )
        badge.pack(pady=(scale(4, self.ui_scale), scale(12, self.ui_scale)))
        badge.create_oval(4, 4, scale(46, self.ui_scale), scale(46, self.ui_scale), fill=PALETTE.accent_soft, outline="")
        badge.create_text(
            scale(25, self.ui_scale),
            scale(25, self.ui_scale),
            text="◒",
            fill=PALETTE.accent_text,
            font=font(self.ui_family, 18, self.ui_scale),
        )
        tk.Label(
            card,
            text="关闭「时辰筏」时要怎么处理？",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.ui_family, 11, self.ui_scale, "bold"),
            wraplength=scale(240, self.ui_scale),
            justify=tk.CENTER,
        ).pack(anchor="center")

        remember_row = tk.Frame(
            card,
            bg=PALETTE.card_bg,
            cursor="hand2",
        )
        remember_row.pack(anchor="center", pady=(scale(14, self.ui_scale), scale(16, self.ui_scale)))
        self.remember_indicator = tk.Label(
            remember_row,
            text="",
            width=2,
            height=1,
            bg=PALETTE.card_bg,
            fg=PALETTE.accent_text,
            highlightthickness=1,
            highlightbackground=PALETTE.border,
            font=font(self.ui_family, 10, self.ui_scale),
        )
        self.remember_indicator.pack(side=tk.LEFT)
        remember_text = tk.Label(
            remember_row,
            text="记住这次选择",
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            font=font(self.ui_family, 10, self.ui_scale),
            padx=scale(10, self.ui_scale),
            cursor="hand2",
        )
        remember_text.pack(side=tk.LEFT)
        for widget in (remember_row, self.remember_indicator, remember_text):
            widget.bind("<Button-1>", lambda _event: self._toggle_remember())

        actions = tk.Frame(card, bg=PALETTE.card_bg)
        actions.pack(fill=tk.X)
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)
        PillButton(
            actions,
            "缩到托盘",
            lambda: self._finish("tray"),
            width=scale(112, self.ui_scale),
            height=scale(34, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            radius=scale(9, self.ui_scale),
            bg=PALETTE.card_bg,
        ).grid(row=0, column=0, sticky="ew")
        PillButton(
            actions,
            "直接关闭",
            lambda: self._finish("exit"),
            width=scale(112, self.ui_scale),
            height=scale(34, self.ui_scale),
            family=self.ui_family,
            ui_scale=self.ui_scale,
            fill=PALETTE.accent_soft,
            active_fill="#e5d5c0",
            radius=scale(9, self.ui_scale),
            bg=PALETTE.card_bg,
        ).grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(scale(12, self.ui_scale), 0),
        )

    def _finish(self, action: str) -> None:
        self.result = {"action": action, "remember": self.remember_var.get()}
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _toggle_remember(self) -> None:
        self.remember_var.set(not self.remember_var.get())
        self._refresh_remember_indicator()

    def _refresh_remember_indicator(self) -> None:
        if self.remember_indicator is None:
            return
        checked = self.remember_var.get()
        self.remember_indicator.configure(
            text="✓" if checked else "",
            bg=PALETTE.accent_soft if checked else PALETTE.card_bg,
            highlightbackground=PALETTE.accent if checked else PALETTE.border,
        )

    def _raise_dialog(self) -> None:
        try:
            self.lift(self.master)
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
