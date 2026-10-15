"""打包脚本 —— 主程序用 --onedir（快启动），wake.exe 用 --onefile（快唤醒）。"""

import shutil
import subprocess
import sys
import time
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

EXCLUDE = [
    "numpy", "scipy", "pandas", "matplotlib",
    "sqlalchemy", "cryptography", "bcrypt", "psutil",
    "psycopg2", "mysql",
]


def kill_old():
    """终止正在运行的旧实例。"""
    import time

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

    # 等文件锁释放
    for _ in range(30):
        locked = any(
            p.exists() for p in [MAIN_EXE, DIST / f"{EXE_NAME}.exe", DIST / "wake.exe"]
        )
        if not locked:
            break
        time.sleep(0.2)


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

    if PRESERVED_DATA.exists():
        shutil.rmtree(PRESERVED_DATA)


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
        return
    total_size = sum(f.stat().st_size for f in DIR_OUT.rglob("*") if f.is_file())
    print(f"[完成] {DIR_OUT}")
    print(f"       主程序: {MAIN_EXE.name}  ({MAIN_EXE.stat().st_size / 1024 / 1024:.1f} MB)")
    if WAKE_EXE.exists():
        print(f"       唤醒助手: {WAKE_EXE.name}  ({WAKE_EXE.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"       总大小: {total_size / 1024 / 1024:.1f} MB")
    data_dir = DIR_OUT / "data"
    if data_dir.exists():
        print(f"       数据目录: data/  (已包含)")


def main():
    kill_old()
    clean()
    rc1 = build_main()
    rc2 = build_wake()
    copy_data()
    verify()
    if rc1 != 0 or rc2 != 0:
        print("\n[警告] 部分步骤失败，请检查上方日志。")
    input("\n按回车键关闭...")


if __name__ == "__main__":
    main()
