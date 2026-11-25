"""
راه‌انداز خودکار سامانه یکپارچه پایشگر روی ویندوز / لینوکس / مک.
  1) ساخت محیط مجازی (.venv) و نصب وابستگی‌ها از Wheel آماده
  2) بررسی و نصب مرورگر Chromium برای بخش ایکس
  3) اجرای سرور و باز کردن مرورگر
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
MIN_PYTHON = (3, 10)


def find_system_python() -> str | None:
    for candidate in ("py", "python", "python3"):
        try:
            res = subprocess.run(
                [candidate, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode == 0:
                major, minor = (int(x) for x in res.stdout.split()[:2])
                if (major, minor) >= MIN_PYTHON:
                    return "py -3" if candidate == "py" else candidate
        except Exception:
            continue
    return None


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    print("=" * 62)
    print("  Payeshgar — سامانه یکپارچه پایشگر (وب • تلگرام • ایکس)")
    print("=" * 62)

    if not VENV_PY.exists():
        print("[1/3] Creating virtual environment (.venv) ...")
        sys_py = find_system_python()
        if not sys_py:
            print("[ERROR] Python 3.10+ not found. Please install Python 3.10 or newer.")
            return 1
        if subprocess.run([*sys_py.split(), "-m", "venv", str(VENV_DIR)], cwd=str(ROOT)).returncode != 0:
            print("[ERROR] Failed to create .venv")
            return 1

    py = str(VENV_PY)
    print("[2/3] Checking / installing dependencies ...")
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "pip"], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if subprocess.run([py, "-m", "pip", "install", "--prefer-binary", "-r", "requirements.txt"], cwd=str(ROOT)).returncode != 0:
        print("[ERROR] Dependency installation failed.")
        return 1

    subprocess.run([py, "-m", "playwright", "install", "--only-shell", "chromium"], cwd=str(ROOT))

    print("[3/3] Starting Payeshgar server ...")
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    try:
        return subprocess.run([py, str(ROOT / "run.py"), *sys.argv[1:]], cwd=str(ROOT), env=env).returncode
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
