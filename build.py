"""打包脚本 —— 主程序用 --onedir（快启动），wake.exe 用 --onefile（快唤醒）。"""

import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path

from core.icon_assets import ensure_icon_assets

EXE_NAME = "时辰筏"
ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
DIR_OUT = DIST / EXE_NAME          # --onedir 输出目录
MAIN_EXE = DIR_OUT / f"{EXE_NAME}.exe"
WAKE_EXE = DIR_OUT / "wake.exe"
SPEC = ROOT / f"{EXE_NAME}.spec"
ICON_PATH = ROOT / "data" / "app_icon.ico"
PRESERVED_DATA = ROOT / "_dist_data_preserved"
USER_DATA_FILES = {"tasks.json", "settings.json"}

# 等旧实例释放产物锁：上限秒数与探测间隔，超时后仍继续打包但必须提示
LOCK_TIMEOUT = 6.0
LOCK_POLL_INTERVAL = 0.2

EXCLUDE = [
    "numpy", "scipy", "pandas", "matplotlib",
    "sqlalchemy", "cryptography", "bcrypt", "psutil",
    "psycopg2", "mysql",
]


def _is_locked(path: Path) -> bool:
    """探测产物是否仍被进程占用。

    以追加方式打开只做探测、不写内容，所以不会改动产物；Windows 上文件被运行中的进程占着时
    这次打开会抛 PermissionError，就是「仍被占用」的信号。
    """
    if not path.exists():
        return False
    try:
        with path.open("ab"):
            return False
    except PermissionError:
        return True
    except OSError:
        # 其它错误（路径异常等）按未占用处理，避免打包前又白等满超时。
        return False


def _wait_until_unlocked(paths: Iterable[Path], *, timeout: float = LOCK_TIMEOUT,
                         interval: float = LOCK_POLL_INTERVAL) -> list[Path]:
    """等旧实例释放这些产物的文件锁，返回超时后仍被占用的文件。

    判据是真占用而不是「产物文件是否存在」：产物本来就在，拿存在与否判断会让每次打包都白等
    满超时。未被占用的产物不会触发任何等待。
    """
    paths = tuple(paths)
    deadline = time.monotonic() + timeout
    while True:
        locked = [path for path in paths if _is_locked(path)]
        if not locked or time.monotonic() >= deadline:
            return locked
        time.sleep(interval)


def kill_old():
    """终止正在运行的旧实例。"""
    print("[1/5] 终止旧进程...")
    # 杀所有匹配的时辰筏进程（含 onedir 和旧 onefile 产物）
    ps = (
        f"Get-Process | Where-Object {{$_.ProcessName -like '*{EXE_NAME}*'}} "
        f"| Stop-Process -Force -ErrorAction SilentlyContinue"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 也杀 wake.exe
    ps2 = (
        "Get-Process | Where-Object {$_.ProcessName -like '*wake*'} "
        "| Stop-Process -Force -ErrorAction SilentlyContinue"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps2],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 等文件锁释放：只有真的还被占用才等，未占用立刻返回
    locked = _wait_until_unlocked([MAIN_EXE, DIST / f"{EXE_NAME}.exe", DIST / "wake.exe"])
    if locked:
        print(f"       等待 {LOCK_TIMEOUT:.0f} 秒后仍有进程占用产物，继续打包可能失败：")
        for path in locked:
            print(f"         - {path}")


def clean():
    print("[2/5] 清理旧构建产物...")
    _preserve_dist_data()
    if DIR_OUT.exists():
        shutil.rmtree(DIR_OUT)
    if SPEC.exists():
        SPEC.unlink()
    build_dir = ROOT / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    # 清理 dist 根目录残留
    for stale in DIST.glob("*"):
        if stale.is_file():
            stale.unlink(missing_ok=True)
    wake_spec = ROOT / "wake.spec"
    if wake_spec.exists():
        wake_spec.unlink()


def _preserve_dist_data():
    """打包清理前保留用户运行数据，避免覆盖提醒和设置。"""
    src = DIR_OUT / "data"
    if src.exists():
        if PRESERVED_DATA.exists():
            shutil.rmtree(PRESERVED_DATA)
        shutil.copytree(src, PRESERVED_DATA)
        print("       已保护现有 data/ 目录")
    elif PRESERVED_DATA.exists():
        print("       继续使用上次保留的 data/ 备份")


def build_main():
    print("[3/5] 打包主程序 (--onedir)...\n")
    ensure_icon_assets()
    exclude_args = []
    for mod in EXCLUDE:
        exclude_args += ["--exclude-module", mod]
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         "--onedir", "--windowed",
         "--name", EXE_NAME,
         "--noconfirm",
         "--icon", str(ICON_PATH),
         *exclude_args,
         "main.py"],
        cwd=ROOT,
    )
    return result.returncode


def build_wake():
    print("\n[4/5] 打包唤醒助手 (wake.exe, --onefile)...\n")
    wake_work = ROOT / "_wake_build"
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         "--onefile", "--windowed",
         "--name", "wake",
         "--noconfirm",
         "--icon", str(ICON_PATH),
         "--distpath", str(DIR_OUT),
         "--workpath", str(wake_work),
         "--specpath", str(wake_work),
         "wake.py"],
        cwd=ROOT,
    )
    # 清理中间产物
    if wake_work.exists():
        shutil.rmtree(wake_work, ignore_errors=True)
    wake_spec = ROOT / "wake.spec"
    if wake_spec.exists():
        wake_spec.unlink()
    return result.returncode


def copy_data():
    """复制程序资源，同时保留已打包目录里的用户运行数据。"""
    src_data = ROOT / "data"
    dst_data = DIR_OUT / "data"
    dst_data.mkdir(parents=True, exist_ok=True)

    if PRESERVED_DATA.exists():
        _merge_data_tree(PRESERVED_DATA, dst_data, overwrite=True)

    if src_data.exists():
        _merge_data_tree(src_data, dst_data, overwrite=False)


def _merge_data_tree(src: Path, dst: Path, *, overwrite: bool) -> None:
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _merge_data_tree(item, target, overwrite=overwrite)
            continue
        if item.name in USER_DATA_FILES and target.exists() and not overwrite:
            continue
        if overwrite or item.name not in USER_DATA_FILES or not target.exists():
            shutil.copy2(item, target)


def verify():
    print(f"\n[5/5] 检查产物...")
    if not MAIN_EXE.exists():
        print("[失败] 主程序未生成")
        return False
    if not WAKE_EXE.exists() or not (DIR_OUT / "data").is_dir():
        print("[失败] 唤醒助手或数据目录缺失")
        return False
    total_size = sum(f.stat().st_size for f in DIR_OUT.rglob("*") if f.is_file())
    print(f"[完成] {DIR_OUT}")
    print(f"       主程序: {MAIN_EXE.name}  ({MAIN_EXE.stat().st_size / 1024 / 1024:.1f} MB)")
    if WAKE_EXE.exists():
        print(f"       唤醒助手: {WAKE_EXE.name}  ({WAKE_EXE.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"       总大小: {total_size / 1024 / 1024:.1f} MB")
    data_dir = DIR_OUT / "data"
    if data_dir.exists():
        print(f"       数据目录: data/  (已包含)")
    return True


def main():
    try:
        kill_old()
        clean()
        if build_main() != 0:
            print("\n[失败] 主程序打包失败，停止后续步骤，保留用户数据备份。")
            return 1
        if build_wake() != 0:
            print("\n[失败] 唤醒助手打包失败，停止后续步骤，保留用户数据备份。")
            return 1
        copy_data()
        if not verify():
            print("\n[失败] 产物不完整，请检查上方日志。")
            return 1
        if PRESERVED_DATA.exists():
            shutil.rmtree(PRESERVED_DATA)
        return 0
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"\n[失败] 打包中断：{exc}，用户数据备份保持不变。")
        return 1
    finally:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\n按回车键关闭...")


if __name__ == "__main__":
    sys.exit(main())
