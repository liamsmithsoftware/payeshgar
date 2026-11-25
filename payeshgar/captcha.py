from __future__ import annotations

import base64
import random
import re
from typing import Dict

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(val: str) -> str:
    return (val or "").translate(DIGIT_MAP).strip()


def build_captcha_svg(code: str) -> str:
    width, height = 150, 48
    palette = ["#3b82f6", "#06b6d4", "#60a5fa", "#38bdf8", "#818cf8"]
    lines = []
    for _ in range(5):
        x1, y1 = random.randint(4, width - 4), random.randint(4, height - 4)
        x2, y2 = random.randint(4, width - 4), random.randint(4, height - 4)
        col = random.choice(palette)
        lines.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{col}" stroke-opacity="0.28" stroke-width="1.5"/>'
        )
    dots = []
    for _ in range(18):
        cx, cy = random.randint(4, width - 4), random.randint(4, height - 4)
        dots.append(f'<circle cx="{cx}" cy="{cy}" r="1.2" fill="#94a3b8" fill-opacity="0.35"/>')
    chars = []
    for idx, ch in enumerate(code):
        x = 24 + idx * 29
        y = random.randint(31, 35)
        rot = random.randint(-12, 12)
        col = palette[idx % len(palette)]
        chars.append(
            f'<text x="{x}" y="{y}" fill="{col}" font-family="monospace, sans-serif" '
            f'font-size="26" font-weight="bold" transform="rotate({rot} {x} {y})">{ch}</text>'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect width="100%" height="100%" rx="8" fill="#0b1120"/>'
        f'{"".join(lines)}{"".join(dots)}{"".join(chars)}'
        f"</svg>"
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


class CaptchaService:
    def __init__(self, secret_key: str) -> None:
        self.signer = URLSafeTimedSerializer(secret_key, salt="payeshgar-captcha-v2")

    def generate(self) -> Dict[str, str]:
        code = "".join(random.choices("123456789", k=4))
        nonce = random.randint(100000, 999999)
        token = self.signer.dumps({"a": code, "n": nonce})
        return {"code": code, "token": token, "svg": build_captcha_svg(code)}

    def verify(self, user_input: str, token: str, max_age: int = 900) -> bool:
        cleaned = re.sub(r"\s+", "", normalize_digits(user_input))
        if not cleaned or not token:
            return False
        try:
            payload = self.signer.loads(token, max_age=max_age)
            expected = normalize_digits(str(payload.get("a", "")))
            return bool(expected and cleaned == expected)
        except (BadSignature, SignatureExpired, Exception):
            return False
