from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
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


def _bind_variable_redraw(widget: tk.Canvas, variable: tk.Variable) -> None:
    trace = variable.trace_add("write", lambda *_args: widget._draw())

    def remove_trace(event: tk.Event) -> None:
        nonlocal trace
        # 变量可能比控件活得更久，只解除本控件持有的监听。
        if event.widget is widget and trace is not None:
            variable.trace_remove("write", trace)
            trace = None

    widget.bind("<Destroy>", remove_trace)


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


class _Unset:
    """区分「调用方没传这个参数」与「显式传 None 要求清除」。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试显示
        return "<未设置>"


_UNSET = _Unset()


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
        # 声明宽只作下限：文案比它长时按内容撑开，避免文字被右边缘切掉。
        self._min_width = width
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", lambda _event: self.command())
        self._fit_to_text()
        self._draw()

    def _text_metrics(self) -> tuple[int, int, int]:
        """返回（圆点半径, 圆点与文字间距, 文字像素宽）。"""
        btn_font = font(self.family, 10, self.ui_scale, self.weight)
        font_obj = tkfont.Font(root=self, font=btn_font)
        dot_r = max(2, int(round(3 * self.ui_scale)))
        gap = max(3, int(round(6 * self.ui_scale)))
        return dot_r, gap, font_obj.measure(self.text)

    def _required_width(self) -> int:
        """把圆点与左右留白算进去后，完整显示当前文案所需的宽度。"""
        dot_r, gap, text_w = self._text_metrics()
        # 左右各留一点内边距，免得文字贴着圆角边框。
        padding = max(4, int(round(8 * self.ui_scale)))
        if self.dot_color:
            return dot_r * 2 + gap + text_w + padding * 2
        return text_w + padding * 2

    def _fit_to_text(self) -> None:
        """内容比声明宽时撑开控件；比声明窄时保持原宽度，不动既有布局。"""
        required = self._required_width()
        if required > self._min_width:
            self.configure(width=required)

    def set_text(self, text: str) -> None:
        self.text = text
        self._fit_to_text()
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
        dot_color: str | None | _Unset = _UNSET,
    ) -> None:
        if fill is not None:
            self.fill = fill
        if active_fill is not None:
            self.active_fill = active_fill
        if fg is not None:
            self.fg = fg
        if outline is not None:
            self.outline = outline
        # 允许显式传 None 去掉圆点：圆点会改变所需宽度，所以要按新内容重新贴合。
        if dot_color is not _UNSET:
            self.dot_color = dot_color
        self._fit_to_text()
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
            dot_r, gap, text_w = self._text_metrics()
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
        self._menu: tk.Menu | None = None
        _bind_variable_redraw(self, self.variable)
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
        if self._menu is None:
            self._menu = tk.Menu(self, tearoff=False, bg=PALETTE.card_bg, fg=PALETTE.text_primary,
                                 activebackground=PALETTE.accent_soft)
        menu = self._menu
        menu.delete(0, "end")
        for value in self.values:
            menu.add_command(label=value, command=lambda selected=value: self._choose(selected))
        try:
            menu.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        finally:
            menu.grab_release()

    def _choose(self, value: str) -> None:
        self.variable.set(value)
        if self.command is not None:
            self.command(value)

    def set_values(self, values: Sequence[str]) -> None:
        self.values = list(values)


def _fit_inside_screen(position: int, size: int, screen: int) -> int:
    """把弹出位置收进屏幕，贴边弹出时整块菜单都要能看见。

    指针已经在主屏之外（多显示器副屏）时原样返回：那里按主屏宽度硬拉回来，
    菜单反而会跳到另一块屏幕上。
    """
    if position + size <= screen or position > screen:
        return position
    return max(0, screen - size)


class CanvasMenu(tk.Toplevel):
    """自绘弹出菜单，替代原生 tk.Menu。

    原生菜单由系统绘制，字体、配色、圆角都跟界面其余控件对不上。这里在 Canvas 上
    自己画面板与条目，并保留 gui/app.py 用到的 tk.Menu 子集
    （delete / add_command / index / entrycget / invoke / tk_popup / grab_release），
    调用方的弹出流程不必跟着改。

    与 tk.Menu 唯一的差别：tk_popup 不再阻塞到用户点选为止，弹出后立即返回，
    关闭交给自己的绑定（点菜单外、Esc、点条目），因此不会卡住任何调用方。
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        family: str,
        ui_scale: float,
        radius: int | None = None,
        item_height: int | None = None,
        min_width: int | None = None,
    ) -> None:
        super().__init__(master)
        self.overrideredirect(True)
        self.withdraw()
        self.configure(bg=PALETTE.card_bg)
        self.ui_scale = ui_scale
        self.radius = radius if radius is not None else scale(10, ui_scale)
        self.item_height = item_height if item_height is not None else scale(28, ui_scale)
        self.padding_x = scale(12, ui_scale)
        self.padding_y = scale(6, ui_scale)
        self._hover_inset = scale(4, ui_scale)
        self._min_width = min_width if min_width is not None else scale(132, ui_scale)
        self._items: list[tuple[str, Callable[[], None] | None]] = []
        self._font = font(family, 10, ui_scale)
        self._metrics = tkfont.Font(root=self, font=self._font)
        self._hover_index: int | None = None
        self._width = self._min_width
        self._height = self.item_height + self.padding_y * 2
        self._shown = False
        self._previous_focus: tk.Misc | None = None
        # 面板与条目都画在同一张画布上：反复弹出只改图元，不新建子控件。
        self.canvas = tk.Canvas(self, bg=PALETTE.card_bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        # 绑定挂在弹层自己身上：抓住输入后落在别处的点击也会派发到这里，
        # 「点菜单外面」与「点菜单项」因此共用同一套命中判断。
        self.bind("<Button-1>", self._on_press)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Escape>", self._on_escape)

    @property
    def is_open(self) -> bool:
        return self._shown

    # ── tk.Menu 兼容接口 ────────────────────────────────────────────
    def delete(self, first: int | str, last: int | str | None = None) -> None:
        """只支持整份清空：调用方每次弹出前都重填全部条目。"""
        if first not in (0, "0") or last not in ("end", tk.END):
            raise ValueError(f"CanvasMenu 只支持 delete(0, \"end\")，收到 {first!r} 与 {last!r}")
        self._items.clear()
        self._hover_index = None

    def add_command(self, *, label: str, command: Callable[[], None] | None = None) -> None:
        self._items.append((label, command))

    def index(self, index: int | str) -> int:
        """tk.Menu.index 的等价物：只支持 "end" 与整数下标。"""
        if index == "end" or index == tk.END:
            return len(self._items) - 1
        return int(index)

    def entrycget(self, index: int, option: str) -> str:
        if option != "label":
            raise ValueError(f"CanvasMenu 只记录 label，收到 {option!r}")
        return self._items[index][0]

    def invoke(self, index: int) -> None:
        command = self._items[index][1]
        if command is not None:
            command()

    def tk_popup(self, x: int, y: int) -> None:
        """在 (x, y) 处弹出并立刻返回，不像原生菜单那样阻塞到点选为止。"""
        self._measure()
        self._draw()
        pos_x = _fit_inside_screen(x, self._width, self.winfo_screenwidth())
        pos_y = _fit_inside_screen(y, self._height, self.winfo_screenheight())
        self._previous_focus = self._focused_widget()
        self.geometry(f"{self._width}x{self._height}+{pos_x}+{pos_y}")
        self.deiconify()
        self.lift()
        self.update_idletasks()
        try:
            self.grab_set()
        except Exception:
            # 抢不到输入抓取就别把窗口留在屏幕上：那样会变成一个点不动的浮层。
            self.close()
            raise
        self._shown = True
        try:
            self.focus_force()
        except tk.TclError:
            # 拿不到焦点时 Esc 不生效，鼠标路径照常可用，不值得为它中断弹出。
            pass

    def grab_release(self) -> None:
        # 没有抓住输入时也允许调用：调用方在 finally 里释放，不该为状态判断出错。
        try:
            super().grab_release()
        except tk.TclError:
            pass

    def close(self) -> None:
        """收起弹层：先还输入抓取，再藏窗口，最后把焦点交回原处。"""
        self._shown = False
        self._hover_index = None
        self.grab_release()
        try:
            self.withdraw()
        except tk.TclError:
            pass
        previous = self._previous_focus
        self._previous_focus = None
        if previous is not None:
            try:
                if previous.winfo_exists():
                    previous.focus_set()
            except tk.TclError:
                pass

    # ── 内部实现 ────────────────────────────────────────────────────
    def _focused_widget(self) -> tk.Misc | None:
        try:
            widget = self.focus_get()
        except (tk.TclError, KeyError):
            return None
        # 复用弹层时焦点可能还停在它自己身上，那不算「弹出前的焦点」。
        return None if widget is None or widget is self else widget

    def _measure(self) -> None:
        text_width = max(
            (self._metrics.measure(label) for label, _command in self._items),
            default=0,
        )
        self._width = max(self._min_width, text_width + self.padding_x * 2)
        self._height = len(self._items) * self.item_height + self.padding_y * 2

    def _index_at(self, y: int) -> int | None:
        local = y - self.padding_y
        if local < 0 or not self._items:
            return None
        index = local // self.item_height
        return int(index) if index < len(self._items) else None

    def _draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(1, self._width)
        height = max(1, self._height)
        draw_round_rect(
            canvas, 0, 0, width, height, self.radius,
            fill=PALETTE.card_bg, outline=PALETTE.border, tag="panel",
        )
        for index, (label, _command) in enumerate(self._items):
            top = self.padding_y + index * self.item_height
            if index == self._hover_index:
                draw_round_rect(
                    canvas,
                    self._hover_inset,
                    top,
                    width - self._hover_inset,
                    top + self.item_height,
                    max(1, self.radius - self._hover_inset),
                    fill=PALETTE.accent_soft,
                    outline="",
                    tag="hover",
                )
            canvas.create_text(
                self.padding_x,
                top + self.item_height // 2,
                text=label,
                anchor="w",
                fill=PALETTE.text_primary,
                font=self._font,
                tags="label",
            )

    def _on_press(self, event: tk.Event) -> None:
        index = self._index_at(event.y)
        # 先关再执行：菜单项可能弹出对话框，浮层不该盖在上面，也不该握着输入抓取。
        self.close()
        if index is not None:
            self.invoke(index)

    def _on_motion(self, event: tk.Event) -> None:
        index = self._index_at(event.y)
        if index != self._hover_index:
            self._hover_index = index
            self._draw()

    def _on_escape(self, _event: tk.Event) -> str:
        self.close()
        return "break"


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
        _bind_variable_redraw(self, self.variable)
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


def elide_text(text: str, font_obj: tkfont.Font, max_width: int, *, suffix: str = "…") -> str:
    """把文本裁进 max_width 像素，末尾用省略号表示还有内容。

    卡片是固定高度的列表行，标题偏长时既不能换行（会把下面的描述挤出去），
    也不能直接截断——硬截出来的文字看不出被切了，用户会以为标题本来就这么短。
    """
    if not text or max_width <= 0:
        return text
    if font_obj.measure(text) <= max_width:
        return text

    suffix_width = font_obj.measure(suffix)
    if suffix_width > max_width:
        # 连省略号都放不下：这一格已经窄到没有展示价值，原样交给上层裁剪。
        return text

    # 二分找能放下的最长前缀，长标题不必逐字测量。
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font_obj.measure(text[:middle]) + suffix_width <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + suffix


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


class SlimScrollbar(tk.Canvas):
    """细长的圆角滚动条。

    ttk 的 clam 滚动条宽度由内置图片决定，``width`` 选项根本不生效（实测只能得到
    14px 上下），拿不到想要的 6px。界面其余控件本来就是自绘的，这里也自绘一条，
    既能统一圆角风格，也方便只在真的需要时才出现。

    对外接口与 ``ttk.Scrollbar`` 一致：接一个 ``command``，并实现 ``set(first, last)``，
    所以可以直接挂到 Canvas 的 ``yscrollcommand`` 上。
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        command: Callable[..., None],
        ui_scale: float,
        thickness: int | None = None,
        bg: str | None = None,
        thumb: str = PALETTE.accent,
        thumb_active: str = "#c3ab8c",
    ) -> None:
        self._thickness = thickness if thickness is not None else scale(6, ui_scale)
        super().__init__(
            master,
            width=self._thickness,
            bg=bg or PALETTE.card_bg,
            highlightthickness=0,
            bd=0,
        )
        self.command = command
        self.ui_scale = ui_scale
        self.thumb = thumb
        self.thumb_active = thumb_active
        self._first = 0.0
        self._last = 1.0
        self._grab_offset = 0.0
        self._dragging = False
        self._hover = False
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", lambda _event: self._draw(hover=True))
        self.bind("<Leave>", lambda _event: self._draw(hover=False))
        self._draw()

    # ── 与 ttk.Scrollbar 对齐的接口 ──────────────────────────────────
    def set(self, first: float | str, last: float | str) -> None:
        self._first = float(first)
        self._last = float(last)
        self._draw()

    def get(self) -> tuple[float, float]:
        return self._first, self._last

    def _thumb_span(self) -> tuple[float, float, int]:
        height = max(1, self.winfo_height())
        top = self._first * height
        bottom = self._last * height
        # 内容特别长时滑块会被压成一条线，留一个还抓得住的最小高度。
        minimum = max(scale(18, self.ui_scale), self._thickness * 3)
        if bottom - top < minimum:
            bottom = min(top + minimum, height)
            top = max(0.0, bottom - minimum)
        return top, bottom, height

    def _draw(self, hover: bool | None = None) -> None:
        if hover is not None:
            self._hover = hover
        self.delete("thumb")
        top, bottom, _ = self._thumb_span()
        if bottom - top < max(2, self._thickness):
            return
        draw_round_rect(
            self,
            0,
            top,
            self._thickness,
            bottom,
            self._thickness / 2,
            fill=self.thumb_active if (self._hover or self._dragging) else self.thumb,
            outline="",
            tag="thumb",
        )

    def _press(self, event: tk.Event) -> None:
        top, bottom, _ = self._thumb_span()
        if top <= event.y <= bottom:
            self._grab_offset = event.y - top
        else:
            # 点在滑块之外：先把滑块挪到指针下面，再顺势接着拖，和系统滚动条行为一致。
            self._grab_offset = (bottom - top) / 2
            self._move_thumb_to(event.y)
        self._dragging = True
        self._draw()

    def _drag(self, event: tk.Event) -> None:
        if self._dragging:
            self._move_thumb_to(event.y)

    def _release(self, _event: tk.Event) -> None:
        self._dragging = False
        self._draw()

    def _move_thumb_to(self, pointer_y: float) -> None:
        _, _, height = self._thumb_span()
        target = pointer_y - self._grab_offset
        # 滑块顶端像素换回视图比例：moveto 用的是同一套比例语义。
        self.command("moveto", min(max(target / height, 0.0), 1.0))


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
