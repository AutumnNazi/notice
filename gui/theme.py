from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Callable, Sequence


@dataclass(frozen=True)
class UiPalette:
    window_bg: str = "#f4f1ec"
    panel_bg: str = "#fffefd"
    card_bg: str = "#fffefd"
    card_alt_bg: str = "#fbf7f1"
    border: str = "#e9e2d8"
    border_soft: str = "#f1ece5"
    text_primary: str = "#342b24"
    text_secondary: str = "#756960"
    text_muted: str = "#a89f96"
    accent: str = "#d7c3aa"
    accent_soft: str = "#eee2d3"
    accent_text: str = "#6c5542"
    danger_text: str = "#c95f55"
    shadow: str = "#e8e2da"


PALETTE = UiPalette()


def draw_round_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    *,
    fill: str,
    outline: str = "",
    width: int = 1,
    tag: str = "shape",
) -> None:
    radius = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    canvas.create_polygon(points, smooth=True, splinesteps=18, fill=fill, outline=outline, width=width, tags=tag)


class RoundedFrame(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        radius: int = 14,
        padding: int = 12,
        fill: str = PALETTE.card_bg,
        outline: str = PALETTE.border_soft,
        shadow: bool = False,
        shadow_color: str = PALETTE.shadow,
        bg: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            master,
            bg=bg or getattr(master, "cget", lambda _name: PALETTE.window_bg)("bg"),
            highlightthickness=0,
            bd=0,
            **kwargs,
        )
        self.radius = radius
        self.padding = padding
        self.fill = fill
        self.outline = outline
        self.shadow = shadow
        self.shadow_color = shadow_color
        self.content = tk.Frame(self, bg=fill)
        self._window_id = self.create_window(padding, padding, window=self.content, anchor="nw")
        self.bind("<Configure>", self._redraw)

    def _redraw(self, event: tk.Event) -> None:
        self.delete("shape")
        width = max(1, event.width)
        height = max(1, event.height)
        inset = 5 if self.shadow else 1
        if self.shadow:
            draw_round_rect(
                self,
                inset + 2,
                inset + 3,
                width - inset + 1,
                height - inset + 1,
                self.radius,
                fill=self.shadow_color,
                outline="",
                tag="shape",
            )
        draw_round_rect(
            self,
            inset,
            inset,
            width - inset,
            height - inset,
            self.radius,
            fill=self.fill,
            outline=self.outline,
            width=1,
            tag="shape",
        )
        self.tag_lower("shape")
        inner_width = max(1, width - (self.padding + inset) * 2)
        inner_height = max(1, height - (self.padding + inset) * 2)
        self.coords(self._window_id, self.padding + inset, self.padding + inset)
        self.itemconfigure(self._window_id, width=inner_width, height=inner_height)


class PillButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command,
        *,
        width: int,
        height: int,
        family: str,
        ui_scale: float,
        fill: str = PALETTE.card_bg,
        active_fill: str = "#faf6ef",
        outline: str = PALETTE.border_soft,
        fg: str = PALETTE.text_primary,
        radius: int = 14,
        weight: str = "normal",
        bg: str | None = None,
        dot_color: str | None = None,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg or getattr(master, "cget", lambda _name: PALETTE.panel_bg)("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.text = text
        self.command = command
        self.fill = fill
        self.active_fill = active_fill
        self.outline = outline
        self.fg = fg
        self.radius = radius
        self.family = family
        self.ui_scale = ui_scale
        self.weight = weight
        self.dot_color = dot_color
        self._hover = False
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", lambda _event: self.command())
        self._draw()

    def set_text(self, text: str) -> None:
        self.text = text
        self._draw()

    def set_dot_color(self, dot_color: str | None) -> None:
        self.dot_color = dot_color
        self._draw()

    def set_colors(
        self,
        *,
        fill: str | None = None,
        active_fill: str | None = None,
        fg: str | None = None,
        outline: str | None = None,
        dot_color: str | None = None,
    ) -> None:
        if fill is not None:
            self.fill = fill
        if active_fill is not None:
            self.active_fill = active_fill
        if fg is not None:
            self.fg = fg
        if outline is not None:
            self.outline = outline
        if dot_color is not None:
            self.dot_color = dot_color
        self._draw()

    def _enter(self, _event: tk.Event) -> None:
        self._hover = True
        self._draw()

    def _leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width() or int(self["width"]))
        height = max(1, self.winfo_height() or int(self["height"]))
        draw_round_rect(
            self,
            1,
            1,
            width - 1,
            height - 1,
            self.radius,
            fill=self.active_fill if self._hover else self.fill,
            outline=self.outline,
            tag="button",
        )
        btn_font = font(self.family, 10, self.ui_scale, self.weight)
        if self.dot_color:
            import tkinter.font as tkfont
            dot_r = max(2, int(round(3 * self.ui_scale)))
            gap = max(3, int(round(6 * self.ui_scale)))
            font_obj = tkfont.Font(font=btn_font)
            text_w = font_obj.measure(self.text)
            total_w = dot_r * 2 + gap + text_w
            dot_x = max(dot_r + 2, (width - total_w) // 2 + dot_r)
            dot_y = height // 2
            self.create_oval(
                dot_x - dot_r,
                dot_y - dot_r,
                dot_x + dot_r,
                dot_y + dot_r,
                fill=self.dot_color,
                outline="",
            )
            self.create_text(
                dot_x + dot_r + gap,
                dot_y,
                text=self.text,
                fill=self.fg,
                font=btn_font,
                anchor="w",
            )
        else:
            self.create_text(
                width // 2,
                height // 2,
                text=self.text,
                fill=self.fg,
                font=btn_font,
            )


class RoundedEntry(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        textvariable: tk.StringVar,
        width: int,
        height: int,
        family: str,
        ui_scale: float,
        placeholder: str = "",
        bg: str | None = None,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg or getattr(master, "cget", lambda _name: PALETTE.card_bg)("bg"),
            highlightthickness=0,
            bd=0,
            cursor="xterm",
        )
        self.radius = scale(10, ui_scale)
        self.family = family
        self.ui_scale = ui_scale
        self.textvariable = textvariable
        self.placeholder = placeholder
        self._placeholder_active = False
        self.entry = tk.Entry(
            self,
            textvariable=textvariable,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            insertbackground=PALETTE.text_primary,
            relief="flat",
            highlightthickness=0,
            bd=0,
            font=font(family, 10, ui_scale),
        )
        self._window_id = self.create_window(
            scale(12, ui_scale),
            height // 2,
            window=self.entry,
            anchor="w",
            width=max(1, width - scale(24, ui_scale)),
            height=max(1, height - scale(12, ui_scale)),
        )
        self.bind("<Configure>", self._redraw)
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.bind("<Button-1>", lambda _event: self.entry.focus_set())
        self._sync_placeholder()
        self._draw(width, height)

    def _redraw(self, event: tk.Event) -> None:
        self._draw(event.width, event.height)

    def _draw(self, width: int, height: int) -> None:
        self.delete("shape")
        draw_round_rect(
            self,
            1,
            1,
            max(2, width - 1),
            max(2, height - 1),
            self.radius,
            fill=PALETTE.card_bg,
            outline=PALETTE.border,
            tag="shape",
        )
        self.tag_lower("shape")
        self.coords(self._window_id, scale(12, self.ui_scale), height // 2)
        self.itemconfigure(
            self._window_id,
            width=max(1, width - scale(24, self.ui_scale)),
            height=max(1, height - scale(12, self.ui_scale)),
        )

    def focus_set(self) -> None:
        self.entry.focus_set()

    def icursor(self, index) -> None:
        self.entry.icursor(index)

    def _sync_placeholder(self) -> None:
        if self.placeholder and not self.textvariable.get():
            self._placeholder_active = True
            self.textvariable.set(self.placeholder)
            self.entry.configure(fg=PALETTE.text_muted)

    def _focus_in(self, _event: tk.Event) -> None:
        if self._placeholder_active:
            self._placeholder_active = False
            self.textvariable.set("")
        self.entry.configure(fg=PALETTE.text_primary)

    def _focus_out(self, _event: tk.Event) -> None:
        if not self.textvariable.get().strip():
            self._sync_placeholder()


class RoundedText(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        width: int,
        height: int,
        family: str,
        ui_scale: float,
        bg: str | None = None,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg or getattr(master, "cget", lambda _name: PALETTE.card_bg)("bg"),
            highlightthickness=0,
            bd=0,
        )
        self.radius = scale(10, ui_scale)
        self.ui_scale = ui_scale
        self.text = tk.Text(
            self,
            bg=PALETTE.card_bg,
            fg=PALETTE.text_primary,
            insertbackground=PALETTE.text_primary,
            relief="flat",
            highlightthickness=0,
            bd=0,
            wrap=tk.WORD,
            font=font(family, 10, ui_scale),
        )
        self._window_id = self.create_window(
            scale(12, ui_scale),
            scale(9, ui_scale),
            window=self.text,
            anchor="nw",
            width=max(1, width - scale(24, ui_scale)),
            height=max(1, height - scale(18, ui_scale)),
        )
        self.bind("<Configure>", self._redraw)
        self.bind("<Button-1>", lambda _event: self.text.focus_set())
        self._draw(width, height)

    def _redraw(self, event: tk.Event) -> None:
        self._draw(event.width, event.height)

    def _draw(self, width: int, height: int) -> None:
        self.delete("shape")
        draw_round_rect(
            self,
            1,
            1,
            max(2, width - 1),
            max(2, height - 1),
            self.radius,
            fill=PALETTE.card_bg,
            outline=PALETTE.border,
            tag="shape",
        )
        self.tag_lower("shape")
        self.coords(self._window_id, scale(12, self.ui_scale), scale(9, self.ui_scale))
        self.itemconfigure(
            self._window_id,
            width=max(1, width - scale(24, self.ui_scale)),
            height=max(1, height - scale(18, self.ui_scale)),
        )

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    def set_text(self, value: str) -> None:
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", value)


class RoundedSelect(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        variable: tk.StringVar,
        values: Sequence[str],
        width: int,
        height: int,
        family: str,
        ui_scale: float,
        command: Callable[[str], None] | None = None,
        bg: str | None = None,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg or getattr(master, "cget", lambda _name: PALETTE.card_bg)("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.variable = variable
        self.values = list(values)
        self.command = command
        self.family = family
        self.ui_scale = ui_scale
        self._hover = False
        self.variable.trace_add("write", lambda *_args: self._draw())
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Button-1>", self._open_menu)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self._draw()

    def _enter(self, _event: tk.Event) -> None:
        self._hover = True
        self._draw()

    def _leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width() or int(self["width"]))
        height = max(1, self.winfo_height() or int(self["height"]))
        draw_round_rect(
            self,
            1,
            1,
            width - 1,
            height - 1,
            scale(10, self.ui_scale),
            fill="#fffefd" if not self._hover else "#fbf8f3",
            outline=PALETTE.border,
            tag="select",
        )
        self.create_text(
            scale(12, self.ui_scale),
            height // 2,
            text=self.variable.get(),
            anchor="w",
            fill=PALETTE.text_primary,
            font=font(self.family, 10, self.ui_scale),
        )
        self.create_text(
            width - scale(14, self.ui_scale),
            height // 2,
            text="⌄",
            anchor="e",
            fill=PALETTE.text_secondary,
            font=font(self.family, 11, self.ui_scale),
        )

    def _open_menu(self, _event: tk.Event) -> None:
        menu = tk.Menu(self, tearoff=False, bg=PALETTE.card_bg, fg=PALETTE.text_primary, activebackground=PALETTE.accent_soft)
        for value in self.values:
            menu.add_command(label=value, command=lambda selected=value: self._choose(selected))
        menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        menu.grab_release()

    def _choose(self, value: str) -> None:
        self.variable.set(value)
        if self.command is not None:
            self.command(value)

    def set_values(self, values: Sequence[str]) -> None:
        self.values = list(values)


class ToggleSwitch(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        *,
        variable: tk.BooleanVar,
        width: int,
        height: int,
        ui_scale: float,
        bg: str | None = None,
        command: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=bg or getattr(master, "cget", lambda _name: PALETTE.card_bg)("bg"),
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.variable = variable
        self.ui_scale = ui_scale
        self.command = command
        self.variable.trace_add("write", lambda *_args: self._draw())
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Button-1>", self._toggle)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width() or int(self["width"]))
        height = max(1, self.winfo_height() or int(self["height"]))
        checked = self.variable.get()
        fill = "#b58f6d" if checked else PALETTE.border
        draw_round_rect(self, 1, 1, width - 1, height - 1, height // 2, fill=fill, outline="", tag="switch")
        knob_size = height - scale(6, self.ui_scale)
        knob_x = width - knob_size - scale(3, self.ui_scale) if checked else scale(3, self.ui_scale)
        self.create_oval(
            knob_x,
            scale(3, self.ui_scale),
            knob_x + knob_size,
            scale(3, self.ui_scale) + knob_size,
            fill=PALETTE.card_bg,
            outline="",
        )

    def _toggle(self, _event: tk.Event) -> None:
        self.variable.set(not self.variable.get())
        if self.command is not None:
            self.command(self.variable.get())


def scale(value: int, ui_scale: float) -> int:
    return max(1, round(value * ui_scale))


def font(family: str, size: int, ui_scale: float, weight: str = "normal") -> tuple[str, int, str]:
    # Tk font sizes are points and already affected by ``tk scaling``.
    # Scaling them again makes high-DPI displays render comically large text.
    return (family, max(1, round(size)), weight)


def center_to_parent(window: tk.Toplevel | tk.Tk, parent: tk.Misc | None) -> None:
    window.update_idletasks()
    if parent is not None:
        parent.update_idletasks()
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        parent_width = parent.winfo_width()
        parent_height = parent.winfo_height()
    else:
        parent_x = window.winfo_screenwidth() // 4
        parent_y = window.winfo_screenheight() // 4
        parent_width = window.winfo_screenwidth() // 2
        parent_height = window.winfo_screenheight() // 2

    width = window.winfo_reqwidth()
    height = window.winfo_reqheight()
    x = parent_x + max((parent_width - width) // 2, 0)
    y = parent_y + max((parent_height - height) // 2, 0)
    window.geometry(f"{width}x{height}+{x}+{y}")


def enable_window_drag(window: tk.Toplevel, *handles: tk.Widget) -> None:
    drag_state = {"x": 0, "y": 0}

    def start(event: tk.Event) -> None:
        drag_state["x"] = event.x_root - window.winfo_x()
        drag_state["y"] = event.y_root - window.winfo_y()

    def move(event: tk.Event) -> None:
        window.geometry(f"+{event.x_root - drag_state['x']}+{event.y_root - drag_state['y']}")

    for handle in handles:
        handle.bind("<ButtonPress-1>", start, add="+")
        handle.bind("<B1-Motion>", move, add="+")


def apply_ttk_theme(style: ttk.Style, family: str, ui_scale: float) -> None:
    body = font(family, 10, ui_scale)
    small = font(family, 9, ui_scale)
    style.theme_use("clam")
    style.configure(".", background=PALETTE.window_bg, foreground=PALETTE.text_primary, font=body)
    style.configure(
        "App.TFrame",
        background=PALETTE.window_bg,
    )
    style.configure(
        "Panel.TFrame",
        background=PALETTE.panel_bg,
        relief="flat",
        borderwidth=0,
    )
    style.configure(
        "Soft.TLabel",
        background=PALETTE.panel_bg,
        foreground=PALETTE.text_secondary,
        font=body,
    )
    style.configure(
        "Muted.TLabel",
        background=PALETTE.panel_bg,
        foreground=PALETTE.text_muted,
        font=small,
    )
    style.configure(
        "Primary.TButton",
        background=PALETTE.accent_soft,
        foreground=PALETTE.accent_text,
        borderwidth=0,
        relief="flat",
        padding=(scale(16, ui_scale), scale(9, ui_scale)),
        font=body,
    )
    style.map(
        "Primary.TButton",
        background=[("active", "#e6d7c1"), ("pressed", "#dcc9ae")],
        foreground=[("disabled", PALETTE.text_muted)],
    )
    style.configure(
        "Secondary.TButton",
        background=PALETTE.card_bg,
        foreground=PALETTE.text_primary,
        bordercolor=PALETTE.border,
        borderwidth=1,
        relief="flat",
        padding=(scale(14, ui_scale), scale(9, ui_scale)),
        font=body,
    )
    style.map(
        "Secondary.TButton",
        background=[("active", "#faf6ef"), ("pressed", "#f2eade")],
        foreground=[("disabled", PALETTE.text_muted)],
    )
    style.configure(
        "Ghost.TButton",
        background=PALETTE.panel_bg,
        foreground=PALETTE.text_secondary,
        borderwidth=0,
        relief="flat",
        padding=(scale(10, ui_scale), scale(8, ui_scale)),
        font=small,
    )
    style.map("Ghost.TButton", background=[("active", PALETTE.panel_bg)])
    style.configure(
        "TEntry",
        fieldbackground=PALETTE.card_bg,
        foreground=PALETTE.text_primary,
        bordercolor=PALETTE.border,
        lightcolor=PALETTE.border,
        darkcolor=PALETTE.border,
        insertcolor=PALETTE.text_primary,
        padding=(scale(12, ui_scale), scale(7, ui_scale)),
        relief="flat",
        font=body,
    )
    style.configure(
        "TCombobox",
        fieldbackground=PALETTE.card_bg,
        background=PALETTE.card_bg,
        foreground=PALETTE.text_primary,
        bordercolor=PALETTE.border,
        lightcolor=PALETTE.border,
        darkcolor=PALETTE.border,
        arrowcolor=PALETTE.text_secondary,
        padding=(scale(12, ui_scale), scale(7, ui_scale)),
        relief="flat",
        font=body,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", PALETTE.card_bg)],
        selectbackground=[("readonly", PALETTE.card_bg)],
        selectforeground=[("readonly", PALETTE.text_primary)],
    )
