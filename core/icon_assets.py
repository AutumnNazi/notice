from __future__ import annotations

from threading import Lock

from PIL import Image, ImageDraw

from config import ICON_FILE, ICON_ICO_FILE, ICON_PNG_FILES, ICON_PNG_SIZES, TOAST_IMAGE_FILE

_ICON_LOCK = Lock()
_generated_assets = None


def ensure_icon_assets() -> None:
    global _generated_assets
    files = (ICON_FILE, TOAST_IMAGE_FILE, ICON_ICO_FILE, *ICON_PNG_FILES)
    key = (files, ICON_PNG_SIZES)
    with _ICON_LOCK:
        # 每次新进程首次运行仍刷新图标；同进程多处初始化不重复绘制，缺失时补齐。
        if _generated_assets == key and all(path.is_file() for path in files):
            return
        _generated_assets = None
        ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
        tray_image = _build_leaf_icon(256)
        toast_image = _build_leaf_icon(256)
        icon_images = [_build_leaf_icon(size) for size in ICON_PNG_SIZES]
        try:
            tray_image.save(ICON_FILE)
            toast_image.save(TOAST_IMAGE_FILE)
            for icon_file, icon_image in zip(ICON_PNG_FILES, icon_images):
                icon_image.save(icon_file)
            toast_image.save(ICON_ICO_FILE, sizes=[(size, size) for size in ICON_PNG_SIZES])
            _generated_assets = key
        finally:
            for image in (tray_image, toast_image, *icon_images):
                image.close()


def _build_leaf_icon(size: int) -> Image.Image:
    render_size = size * 5
    image = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = render_size
    circle_margin = round(unit * 0.08)
    circle_fill = (237, 229, 217, 255)
    ink = (104, 88, 70, 255)
    soft_ink = (135, 117, 95, 230)

    draw.ellipse(
        (circle_margin, circle_margin, unit - circle_margin, unit - circle_margin),
        fill=circle_fill,
    )

    stroke = max(6, round(unit * 0.028))
    vein = max(4, round(stroke * 0.72))
    base = (unit * 0.39, unit * 0.63)
    tip = (unit * 0.68, unit * 0.34)
    upper = _quadratic_points(base, (unit * 0.44, unit * 0.32), tip)
    lower = _quadratic_points(base, (unit * 0.75, unit * 0.48), tip)

    draw.line(_int_points(upper), fill=ink, width=stroke, joint="curve")
    draw.line(_int_points(lower), fill=ink, width=stroke, joint="curve")
    draw.line(_int_points([base, tip]), fill=soft_ink, width=vein)
    draw.line(
        _int_points([(unit * 0.37, unit * 0.65), (unit * 0.28, unit * 0.72)]),
        fill=ink,
        width=vein,
    )
    draw.line(
        _int_points([(unit * 0.52, unit * 0.50), (unit * 0.45, unit * 0.47)]),
        fill=soft_ink,
        width=max(3, round(vein * 0.72)),
    )
    draw.line(
        _int_points([(unit * 0.57, unit * 0.45), (unit * 0.61, unit * 0.52)]),
        fill=soft_ink,
        width=max(3, round(vein * 0.72)),
    )

    return image.resize((size, size), Image.Resampling.LANCZOS)


def _quadratic_points(
    start: tuple[float, float],
    control: tuple[float, float],
    end: tuple[float, float],
    steps: int = 22,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for index in range(steps + 1):
        t = index / steps
        inv = 1 - t
        x = inv * inv * start[0] + 2 * inv * t * control[0] + t * t * end[0]
        y = inv * inv * start[1] + 2 * inv * t * control[1] + t * t * end[1]
        points.append((x, y))
    return points


def _int_points(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    return [(round(x), round(y)) for x, y in points]
