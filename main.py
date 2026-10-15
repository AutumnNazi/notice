import ctypes

from config import APP_ID


def _enable_high_dpi() -> None:
    user32 = ctypes.windll.user32
    shcore = ctypes.windll.shcore

    # Windows 10/11: Per-Monitor DPI V2
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass

    # Windows 8.1
    try:
        shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    # Legacy fallback
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def _set_windows_app_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


_enable_high_dpi()
_set_windows_app_id()

from single_instance import single_instance_guard, register_show_callback


def main() -> None:
    single_instance_guard()
    from gui.app import NoticeApp

    app = NoticeApp()
    register_show_callback(app.show_window)
    app.run()


if __name__ == "__main__":
    main()
