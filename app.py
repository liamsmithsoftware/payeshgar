#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
پایشگر (Payeshgar) — نقطه اجرای یکپارچه
ترکیب کامل سامانه‌های:
  - آرشیو هوشمند مطالب وب‌سایت‌ها (/web)
  - آرشیو هوشمند کانال‌های تلگرام (/telegram)
  - آرشیو هوشمند حساب‌های ایکس (/x)
همراه با سیستم کنترل دسترسی مبتنی بر نقش (ACL)، ورود با کپچا و تفکیک پایگاه داده کاربران و محیط‌های کاری.
"""
from __future__ import annotations

import os
import uvicorn

from payeshgar.gateway import create_payeshgar_app

app = create_payeshgar_app()


if __name__ == "__main__":
    host = os.environ.get("PAYESHGAR_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("PAYESHGAR_PORT", "5000")))
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
