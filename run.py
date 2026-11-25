#!/usr/bin/env python3
"""
اجرای سامانه یکپارچه پایشگر (وب + تلگرام + ایکس + ACL).

استفاده:
    python run.py                 # اجرا روی 0.0.0.0:8000 (در صورت اشغال، پورت آزاد بعدی)
    python run.py --port 5000     # تغییر پورت
    python run.py --reload        # حالت توسعه
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.web_app.utils.port_utils import find_free_port, is_our_app_running, is_port_free


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="سامانه یکپارچه پایشگر (Payeshgar)")
    parser.add_argument("--host", default=os.getenv("PAYESHGAR_HOST", "0.0.0.0"), help="آدرس اتصال")
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("PORT", os.getenv("PAYESHGAR_PORT", "8000"))), help="پورت سرور"
    )
    parser.add_argument("--reload", action="store_true", help="بارگذاری خودکار هنگام تغییر کد")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    import uvicorn

    port = args.port
    if not is_port_free(port, args.host):
        if is_our_app_running(port, args.host, app_id="payeshgar"):
            print(f"پایشگر از قبل روی http://localhost:{port} در حال اجراست.")
            raise SystemExit(0)
        port = find_free_port(port + 1, host=args.host)
        print(f"پورت {args.port} اشغال است؛ انتقال خودکار به پورت آزاد {port} ...")

    print("=" * 68)
    print("  سامانه یکپارچه پایشگر (Payeshgar) — وب • تلگرام • ایکس • ACL")
    print(f"  آدرس ورود به سامانه:  http://localhost:{port}")
    print("  برای خروج کلیدهای Ctrl + C را فشار دهید")
    print("=" * 68)

    uvicorn.run(
        "app:app",
        host=args.host,
        port=port,
        reload=args.reload,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
