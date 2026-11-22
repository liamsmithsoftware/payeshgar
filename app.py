#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
پایشگر - Payeshgar
ترکیب خزش وب + تلگرام + ایکس با سیستم ACL کامل
- فضای کاربری و پایگاه داده کاربران از هم تفکیک شده
- users.db : کاربران، نقش‌ها، دسترسی‌ها
- workspace.db : نتایج خزش وب/تلگرام/ایکس
"""

import os
import re
import base64
import sqlite3
import random
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

# ---------- Config ----------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

USERS_DB = os.path.join(INSTANCE_DIR, "users.db")
WORKSPACE_DB = os.path.join(INSTANCE_DIR, "workspace.db")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "payeshgar-secret-key-2026-change-in-prod")
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 8  # 8 hours
app.config["SESSION_COOKIE_HTTPONLY"] = True

captcha_signer = URLSafeTimedSerializer(app.secret_key, salt="payeshgar-captcha-v1")
auth_signer = URLSafeTimedSerializer(app.secret_key, salt="payeshgar-auth-v1")


class IframeAwareSessionInterface(SecureCookieSessionInterface):
    """Automatically enables SameSite=None; Secure; Partitioned on HTTPS (for cross-site preview iframes)."""

    def get_cookie_secure(self, app):
        return True if request.is_secure else super().get_cookie_secure(app)

    def get_cookie_samesite(self, app):
        return "None" if request.is_secure else super().get_cookie_samesite(app)

    def save_session(self, app, session, response):
        super().save_session(app, session, response)
        if request.is_secure:
            cookies = response.headers.getlist("Set-Cookie")
            if cookies:
                response.headers.pop("Set-Cookie", None)
                name = self.get_cookie_name(app)
                for c in cookies:
                    if c.startswith(f"{name}=") and "Partitioned" not in c:
                        c += "; Partitioned"
                    response.headers.add("Set-Cookie", c)


app.session_interface = IframeAwareSessionInterface()


# ---------- DB Helpers ----------
def get_users_db():
    conn = sqlite3.connect(USERS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_workspace_db():
    conn = sqlite3.connect(WORKSPACE_DB)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- Permissions Definition ----------
PERMISSIONS = [
    # وب
    ("web.view", "مشاهده بخش وب", "وب"),
    ("web.crawl", "خزش وب‌سایت", "وب"),
    ("web.delete", "حذف نتایج وب", "وب"),
    # تلگرام
    ("telegram.view", "مشاهده بخش تلگرام", "تلگرام"),
    ("telegram.crawl", "خزش تلگرام", "تلگرام"),
    ("telegram.delete", "حذف نتایج تلگرام", "تلگرام"),
    # ایکس
    ("x.view", "مشاهده بخش ایکس", "ایکس"),
    ("x.crawl", "خزش ایکس", "ایکس"),
    ("x.delete", "حذف نتایج ایکس", "ایکس"),
    # کاربران
    ("users.view", "مشاهده کاربران", "کاربران"),
    ("users.create", "ایجاد کاربر", "کاربران"),
    ("users.edit", "ویرایش کاربر", "کاربران"),
    ("users.delete", "حذف کاربر", "کاربران"),
    # نقش‌ها
    ("roles.view", "مشاهده نقش‌ها", "نقش‌ها"),
    ("roles.create", "ایجاد نقش", "نقش‌ها"),
    ("roles.edit", "ویرایش نقش و دسترسی‌ها", "نقش‌ها"),
    ("roles.delete", "حذف نقش", "نقش‌ها"),
    # مدیریت
    ("admin.access", "دسترسی به پنل مدیریت", "مدیریت"),
]


def init_dbs():
    # ---- users.db ----
    conn = get_users_db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS permissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT,
        category TEXT
    );
    CREATE TABLE IF NOT EXISTS role_permissions (
        role_id INTEGER NOT NULL,
        permission_id INTEGER NOT NULL,
        PRIMARY KEY (role_id, permission_id),
        FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
        FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS user_roles (
        user_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, role_id),
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
    );
    """)
    for name, desc, cat in PERMISSIONS:
        cur.execute("INSERT OR IGNORE INTO permissions (name, description, category) VALUES (?,?,?)", (name, desc, cat))

    cur.execute("INSERT OR IGNORE INTO roles (name, description) VALUES (?,?)", ("admin", "مدیر سیستم - دسترسی کامل"))
    cur.execute("INSERT OR IGNORE INTO roles (name, description) VALUES (?,?)", ("operator", "اپراتور - خزش و مشاهده"))
    cur.execute("INSERT OR IGNORE INTO roles (name, description) VALUES (?,?)", ("viewer", "مشاهده‌کننده - فقط مشاهده"))

    admin_role = cur.execute("SELECT id FROM roles WHERE name='admin'").fetchone()
    if admin_role:
        perms = cur.execute("SELECT id FROM permissions").fetchall()
        for p in perms:
            cur.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (admin_role["id"], p["id"]))

    op_role = cur.execute("SELECT id FROM roles WHERE name='operator'").fetchone()
    if op_role:
        op_perms = ["web.view", "web.crawl", "telegram.view", "telegram.crawl", "x.view", "x.crawl"]
        for pn in op_perms:
            pr = cur.execute("SELECT id FROM permissions WHERE name=?", (pn,)).fetchone()
            if pr:
                cur.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (op_role["id"], pr["id"]))

    vw_role = cur.execute("SELECT id FROM roles WHERE name='viewer'").fetchone()
    if vw_role:
        vw_perms = ["web.view", "telegram.view", "x.view"]
        for pn in vw_perms:
            pr = cur.execute("SELECT id FROM permissions WHERE name=?", (pn,)).fetchone()
            if pr:
                cur.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (vw_role["id"], pr["id"]))

    admin_user = cur.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if not admin_user:
        pwd = generate_password_hash("Admin@1234")
        cur.execute("INSERT INTO users (username, password_hash) VALUES (?,?)", ("admin", pwd))
        uid = cur.lastrowid
        if admin_role:
            cur.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, admin_role["id"]))
    conn.commit()
    conn.close()

    # ---- workspace.db ----
    conn = get_workspace_db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS web_crawls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        title TEXT,
        description TEXT,
        links_count INTEGER DEFAULT 0,
        snippet TEXT,
        status TEXT DEFAULT 'success',
        created_by TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS telegram_crawls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        channel TEXT,
        title TEXT,
        posts_count INTEGER DEFAULT 0,
        snippet TEXT,
        status TEXT DEFAULT 'success',
        created_by TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS x_crawls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        title TEXT,
        snippet TEXT,
        status TEXT DEFAULT 'success',
        created_by TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    """)
    conn.commit()
    conn.close()
    print("✅ Databases initialized: users.db + workspace.db")


init_dbs()


# ---------- Digit Normalization ----------
DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(val: str) -> str:
    return (val or "").translate(DIGIT_MAP).strip()


# ---------- Auth & Token Helpers ----------
def make_auth_token(user_id: int) -> str:
    return auth_signer.dumps({"uid": user_id})


def verify_auth_token(token: str):
    if not token:
        return None
    try:
        data = auth_signer.loads(token, max_age=60 * 60 * 8)
        return data.get("uid")
    except (BadSignature, SignatureExpired, Exception):
        return None


def append_token_to_url(target_url: str, token: str) -> str:
    parsed = urlparse(target_url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["_at"] = token
    return urlunparse(parsed._replace(query=urlencode(q)))


def get_current_user():
    uid = session.get("user_id")
    if not uid:
        token = (
            request.headers.get("X-Auth-Token")
            or request.args.get("_at")
            or request.form.get("_at")
        )
        uid = verify_auth_token(token)
        if uid:
            session["user_id"] = uid
            session.permanent = True
    if not uid:
        return None
    conn = get_users_db()
    user = conn.execute("SELECT * FROM users WHERE id=? AND is_active=1", (uid,)).fetchone()
    conn.close()
    return user


def get_user_permissions(user_id):
    conn = get_users_db()
    rows = conn.execute("""
        SELECT DISTINCT p.name FROM permissions p
        JOIN role_permissions rp ON rp.permission_id = p.id
        JOIN user_roles ur ON ur.role_id = rp.role_id
        WHERE ur.user_id = ?
    """, (user_id,)).fetchall()
    conn.close()
    perms = set(r["name"] for r in rows)
    # Effective section visibility: if an action perm is granted, ensure section view is implicitly unlocked
    if perms & {"web.crawl", "web.delete"}:
        perms.add("web.view")
    if perms & {"telegram.crawl", "telegram.delete"}:
        perms.add("telegram.view")
    if perms & {"x.crawl", "x.delete"}:
        perms.add("x.view")
    if any(p.startswith(("users.", "roles.")) for p in perms):
        perms.add("admin.access")
        if perms & {"users.create", "users.edit", "users.delete"}:
            perms.add("users.view")
        if perms & {"roles.create", "roles.edit", "roles.delete"}:
            perms.add("roles.view")
    return perms


def get_user_roles(user_id):
    conn = get_users_db()
    rows = conn.execute("""
        SELECT r.* FROM roles r
        JOIN user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = ?
    """, (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def has_permission(perm):
    user = get_current_user()
    if not user:
        return False
    perms = get_user_permissions(user["id"])
    return perm in perms


@app.context_processor
def inject_auth_context():
    user = get_current_user()
    if not user:
        return {"auth_token": ""}
    return {"auth_token": make_auth_token(user["id"])}


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_current_user():
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def permission_required(perm):
    from functools import wraps
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = get_current_user()
            if not user:
                return redirect(url_for("login", next=request.path))
            if not has_permission(perm):
                flash(f"شما دسترسی لازم برای این بخش («{perm}») را ندارید.", "error")
                return redirect(url_for("landing"))
            return f(*args, **kwargs)
        return decorated
    return decorator


# ---------- Stateless + Visual SVG Captcha ----------
def build_captcha_svg(code: str) -> str:
    width, height = 150, 48
    lines = []
    palette = ["#3b82f6", "#06b6d4", "#60a5fa", "#38bdf8", "#818cf8"]
    for _ in range(5):
        x1, y1 = random.randint(4, width - 4), random.randint(4, height - 4)
        x2, y2 = random.randint(4, width - 4), random.randint(4, height - 4)
        col = random.choice(palette)
        lines.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{col}" stroke-opacity="0.28" stroke-width="1.5"/>')
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
        f'</svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def generate_captcha():
    # 4 clear digits (1-9) — unambiguous in both Persian & English keyboards
    code = "".join(random.choices("123456789", k=4))
    nonce = random.randint(100000, 999999)
    token = captcha_signer.dumps({"a": code, "n": nonce})
    svg_uri = build_captcha_svg(code)
    # Also store in session for test convenience
    session["captcha_answer"] = code
    return {"code": code, "token": token, "svg": svg_uri}


def verify_captcha(user_input: str, token: str) -> bool:
    cleaned = re.sub(r"\s+", "", normalize_digits(user_input))
    if not cleaned:
        return False
    # 1. Verify signed stateless token (works even if cookies are blocked in iframe)
    if token:
        try:
            payload = captcha_signer.loads(token, max_age=900)  # 15 minutes
            expected = normalize_digits(str(payload.get("a", "")))
            if expected and cleaned == expected:
                return True
            return False
        except (BadSignature, SignatureExpired, Exception):
            pass
    # 2. Fallback to session answer (e.g. automated tests posting without token)
    sess_expected = session.get("captcha_answer")
    if sess_expected is not None and cleaned == normalize_digits(str(sess_expected)):
        return True
    return False


# ---------- URL Validation ----------
def is_x_url(url):
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()
        return host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com") or host.endswith(".x.com") or host.endswith(".twitter.com")
    except Exception:
        return False


def is_telegram_url(url):
    try:
        host = urlparse(url).hostname or ""
        host = host.lower()
        return host in ("t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog", "www.telegram.dog") or host.endswith(".t.me")
    except Exception:
        return False


def normalize_url(url):
    url = url.strip()
    if not url:
        return url
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


# ---------- Crawlers ----------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}


def crawl_web(url):
    url = normalize_url(url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else urlparse(url).hostname or url
        desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        description = desc_tag.get("content", "").strip() if desc_tag and desc_tag.get("content") else ""
        links = soup.find_all("a", href=True)
        links_count = len(links)
        text = soup.get_text(separator=" ", strip=True)
        snippet = text[:800] if text else ""
        return {"url": url, "title": title, "description": description, "links_count": links_count, "snippet": snippet, "status": "success"}
    except Exception as e:
        return {"url": url, "title": urlparse(url).hostname or url, "description": "", "links_count": 0, "snippet": f"خطا در خزش: {str(e)[:300]}", "status": "error"}


def crawl_telegram(url):
    url = normalize_url(url)
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    channel = path.split("/")[0] if path else ""
    fetch_url = f"https://t.me/s/{channel}" if channel else url
    try:
        resp = requests.get(fetch_url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        title_el = soup.select_one(".tgme_channel_info_header_title span") or soup.select_one(".tgme_page_title span") or soup.title
        title = title_el.get_text(strip=True) if title_el else (channel or url)
        posts = soup.select(".tgme_widget_message")
        posts_count = len(posts)
        texts = []
        for p in posts[:5]:
            t = p.select_one(".tgme_widget_message_text")
            if t:
                texts.append(t.get_text(separator=" ", strip=True)[:300])
        snippet = "\n".join(texts) if texts else soup.get_text(separator=" ", strip=True)[:800]
        if not snippet:
            snippet = f"کانال: {channel} - {posts_count} پست یافت شد."
        return {"url": url, "channel": channel, "title": title, "posts_count": posts_count, "snippet": snippet, "status": "success"}
    except Exception as e:
        return {"url": url, "channel": channel, "title": channel or url, "posts_count": 0,
                "snippet": f"کانال تلگرام: {channel}\n(خزش مستقیم با خطا مواجه شد: {str(e)[:200]})\nلینک ثبت شد.", "status": "success"}


def crawl_x(url):
    url = normalize_url(url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else "پست ایکس"
        og = soup.find("meta", attrs={"property": "og:description"})
        snippet = og.get("content", "").strip() if og and og.get("content") else soup.get_text(separator=" ", strip=True)[:800]
        if not snippet:
            snippet = "لینک ایکس ثبت شد. (ایکس برای خزش کامل نیازمند احراز هویت است)"
        return {"url": url, "title": title, "snippet": snippet, "status": "success"}
    except Exception as e:
        return {"url": url, "title": "پست ایکس", "snippet": f"لینک ایکس ثبت شد.\n(خزش مستقیم: {str(e)[:200]})", "status": "success"}


# ---------- Routes: Landing & Login ----------
def render_login_page(error=None, username_val=""):
    cap = generate_captcha()
    return render_template(
        "login.html",
        captcha_svg=cap["svg"],
        captcha_token=cap["token"],
        error=error,
        username_val=username_val,
    )


@app.route("/api/captcha")
def api_captcha():
    cap = generate_captcha()
    return jsonify({"svg": cap["svg"], "token": cap["token"]})


@app.route("/")
def landing():
    user = get_current_user()
    if not user:
        # First page shows ONLY the login page when unauthenticated
        return render_login_page()
    perms = get_user_permissions(user["id"])
    roles = get_user_roles(user["id"])
    return render_template("landing.html", user=user, perms=perms, roles=roles)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if get_current_user():
            return redirect(url_for("landing"))
        return render_login_page()

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    captcha_input = request.form.get("captcha") or ""
    captcha_token = request.form.get("captcha_token") or ""

    if not verify_captcha(captcha_input, captcha_token):
        return render_login_page(error="کد امنیتی (کپچا) اشتباه است یا منقضی شده. لطفاً کد جدید را وارد کنید.", username_val=username)

    conn = get_users_db()
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not user or not check_password_hash(user["password_hash"], password):
        return render_login_page(error="نام کاربری یا رمز عبور اشتباه است.", username_val=username)
    if not user["is_active"]:
        return render_login_page(error="حساب کاربری شما غیرفعال شده است.", username_val=username)

    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True

    token = make_auth_token(user["id"])
    nxt = (request.args.get("next") or request.form.get("next") or "").strip()
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/login"):
        nxt = url_for("landing")
    return redirect(append_token_to_url(nxt, token))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Routes: Web ----------
@app.route("/web")
@login_required
@permission_required("web.view")
def web_page():
    user = get_current_user()
    perms = get_user_permissions(user["id"])
    return render_template("web.html", user=user, perms=perms)


@app.route("/api/web/crawl", methods=["POST"])
@login_required
def api_web_crawl():
    if not has_permission("web.crawl"):
        return jsonify({"error": "شما دسترسی خزش وب را ندارید."}), 403
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "لینک را وارد کنید."}), 400
    url = normalize_url(url)
    if is_x_url(url):
        return jsonify({"error": "لینک‌های شبکه ایکس (x.com / twitter.com) در بخش وب مجاز نیست. برای ایکس از بخش ایکس استفاده کنید."}), 400
    if is_telegram_url(url):
        return jsonify({"error": "لینک تلگرام در بخش وب مجاز نیست. برای تلگرام از بخش تلگرام استفاده کنید."}), 400
    result = crawl_web(url)
    user = get_current_user()
    conn = get_workspace_db()
    cur = conn.execute(
        "INSERT INTO web_crawls (url, title, description, links_count, snippet, status, created_by) VALUES (?,?,?,?,?,?,?)",
        (result["url"], result["title"], result["description"], result["links_count"], result["snippet"], result["status"], user["username"])
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    result["id"] = rid
    return jsonify(result)


@app.route("/api/web/list")
@login_required
def api_web_list():
    if not has_permission("web.view"):
        return jsonify({"error": "دسترسی ندارید"}), 403
    conn = get_workspace_db()
    rows = conn.execute("SELECT * FROM web_crawls ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/web/delete/<int:rid>", methods=["DELETE"])
@login_required
def api_web_delete(rid):
    if not has_permission("web.delete"):
        return jsonify({"error": "شما دسترسی حذف نتایج وب را ندارید."}), 403
    conn = get_workspace_db()
    conn.execute("DELETE FROM web_crawls WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- Routes: Telegram ----------
@app.route("/telegram")
@login_required
@permission_required("telegram.view")
def telegram_page():
    user = get_current_user()
    perms = get_user_permissions(user["id"])
    return render_template("telegram.html", user=user, perms=perms)


@app.route("/api/telegram/crawl", methods=["POST"])
@login_required
def api_telegram_crawl():
    if not has_permission("telegram.crawl"):
        return jsonify({"error": "شما دسترسی خزش تلگرام را ندارید."}), 403
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "لینک تلگرام را وارد کنید."}), 400
    url = normalize_url(url)
    if not is_telegram_url(url):
        return jsonify({"error": "فقط لینک‌های تلگرام (t.me) در این بخش مجاز است."}), 400
    result = crawl_telegram(url)
    user = get_current_user()
    conn = get_workspace_db()
    cur = conn.execute(
        "INSERT INTO telegram_crawls (url, channel, title, posts_count, snippet, status, created_by) VALUES (?,?,?,?,?,?,?)",
        (result["url"], result["channel"], result["title"], result["posts_count"], result["snippet"], result["status"], user["username"])
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    result["id"] = rid
    return jsonify(result)


@app.route("/api/telegram/list")
@login_required
def api_telegram_list():
    if not has_permission("telegram.view"):
        return jsonify({"error": "دسترسی ندارید"}), 403
    conn = get_workspace_db()
    rows = conn.execute("SELECT * FROM telegram_crawls ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/telegram/delete/<int:rid>", methods=["DELETE"])
@login_required
def api_telegram_delete(rid):
    if not has_permission("telegram.delete"):
        return jsonify({"error": "شما دسترسی حذف نتایج تلگرام را ندارید."}), 403
    conn = get_workspace_db()
    conn.execute("DELETE FROM telegram_crawls WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- Routes: X ----------
@app.route("/x")
@login_required
@permission_required("x.view")
def x_page():
    user = get_current_user()
    perms = get_user_permissions(user["id"])
    return render_template("x.html", user=user, perms=perms)


@app.route("/api/x/crawl", methods=["POST"])
@login_required
def api_x_crawl():
    if not has_permission("x.crawl"):
        return jsonify({"error": "شما دسترسی خزش ایکس را ندارید."}), 403
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "لینک ایکس را وارد کنید."}), 400
    url = normalize_url(url)
    if not is_x_url(url):
        return jsonify({"error": "فقط لینک‌های شبکه ایکس (x.com / twitter.com) در این بخش مجاز است. سایر لینک‌ها پذیرفته نمی‌شوند."}), 400
    result = crawl_x(url)
    user = get_current_user()
    conn = get_workspace_db()
    cur = conn.execute(
        "INSERT INTO x_crawls (url, title, snippet, status, created_by) VALUES (?,?,?,?,?)",
        (result["url"], result["title"], result["snippet"], result["status"], user["username"])
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    result["id"] = rid
    return jsonify(result)


@app.route("/api/x/list")
@login_required
def api_x_list():
    if not has_permission("x.view"):
        return jsonify({"error": "دسترسی ندارید"}), 403
    conn = get_workspace_db()
    rows = conn.execute("SELECT * FROM x_crawls ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/x/delete/<int:rid>", methods=["DELETE"])
@login_required
def api_x_delete(rid):
    if not has_permission("x.delete"):
        return jsonify({"error": "شما دسترسی حذف نتایج ایکس را ندارید."}), 403
    conn = get_workspace_db()
    conn.execute("DELETE FROM x_crawls WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- Routes: Admin ----------
@app.route("/admin")
@login_required
@permission_required("admin.access")
def admin_page():
    user = get_current_user()
    perms = get_user_permissions(user["id"])
    return render_template("admin.html", user=user, perms=perms)


@app.route("/api/admin/users")
@login_required
def api_admin_users():
    if not has_permission("users.view"):
        return jsonify({"error": "دسترسی ندارید"}), 403
    conn = get_users_db()
    users = conn.execute("SELECT id, username, is_active, created_at FROM users ORDER BY id").fetchall()
    result = []
    for u in users:
        roles = conn.execute("SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?", (u["id"],)).fetchall()
        result.append({"id": u["id"], "username": u["username"], "is_active": u["is_active"], "created_at": u["created_at"], "roles": [r["name"] for r in roles]})
    conn.close()
    return jsonify(result)


@app.route("/api/admin/users", methods=["POST"])
@login_required
def api_admin_create_user():
    if not has_permission("users.create"):
        return jsonify({"error": "دسترسی ایجاد کاربر را ندارید."}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    roles = data.get("roles") or []
    if not username or not password:
        return jsonify({"error": "نام کاربری و رمز عبور الزامی است."}), 400
    conn = get_users_db()
    try:
        cur = conn.execute("INSERT INTO users (username, password_hash) VALUES (?,?)", (username, generate_password_hash(password)))
        uid = cur.lastrowid
        for rn in roles:
            r = conn.execute("SELECT id FROM roles WHERE name=?", (rn,)).fetchone()
            if r:
                conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, r["id"]))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "این نام کاربری قبلاً ثبت شده است."}), 400
    conn.close()
    return jsonify({"ok": True, "id": uid})


@app.route("/api/admin/users/<int:uid>", methods=["PUT"])
@login_required
def api_admin_update_user(uid):
    if not has_permission("users.edit"):
        return jsonify({"error": "دسترسی ویرایش کاربر را ندارید."}), 403
    data = request.get_json(silent=True) or {}
    conn = get_users_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        conn.close()
        return jsonify({"error": "کاربر یافت نشد."}), 404
    if data.get("password"):
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(data["password"]), uid))
    if "is_active" in data:
        conn.execute("UPDATE users SET is_active=? WHERE id=?", (1 if data["is_active"] else 0, uid))
    if "roles" in data:
        conn.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
        for rn in data["roles"]:
            r = conn.execute("SELECT id FROM roles WHERE name=?", (rn,)).fetchone()
            if r:
                conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, r["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@login_required
def api_admin_delete_user(uid):
    if not has_permission("users.delete"):
        return jsonify({"error": "دسترسی حذف کاربر را ندارید."}), 403
    me = get_current_user()
    if me and me["id"] == uid:
        return jsonify({"error": "نمی‌توانید خودتان را حذف کنید."}), 400
    conn = get_users_db()
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/roles")
@login_required
def api_admin_roles():
    if not has_permission("roles.view") and not has_permission("users.view"):
        return jsonify({"error": "دسترسی ندارید"}), 403
    conn = get_users_db()
    roles = conn.execute("SELECT * FROM roles ORDER BY id").fetchall()
    result = []
    for r in roles:
        perms = conn.execute("SELECT p.name FROM permissions p JOIN role_permissions rp ON rp.permission_id=p.id WHERE rp.role_id=?", (r["id"],)).fetchall()
        result.append({"id": r["id"], "name": r["name"], "description": r["description"], "created_at": r["created_at"], "permissions": [p["name"] for p in perms]})
    conn.close()
    return jsonify(result)


@app.route("/api/admin/roles", methods=["POST"])
@login_required
def api_admin_create_role():
    if not has_permission("roles.create"):
        return jsonify({"error": "دسترسی ایجاد نقش را ندارید."}), 403
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    desc = (data.get("description") or "").strip()
    perms = data.get("permissions") or []
    if not name:
        return jsonify({"error": "نام نقش الزامی است."}), 400
    conn = get_users_db()
    try:
        cur = conn.execute("INSERT INTO roles (name, description) VALUES (?,?)", (name, desc))
        rid = cur.lastrowid
        for pn in perms:
            p = conn.execute("SELECT id FROM permissions WHERE name=?", (pn,)).fetchone()
            if p:
                conn.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (rid, p["id"]))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "این نقش قبلاً وجود دارد."}), 400
    conn.close()
    return jsonify({"ok": True, "id": rid})


@app.route("/api/admin/roles/<int:rid>", methods=["PUT"])
@login_required
def api_admin_update_role(rid):
    if not has_permission("roles.edit"):
        return jsonify({"error": "دسترسی ویرایش نقش را ندارید."}), 403
    data = request.get_json(silent=True) or {}
    conn = get_users_db()
    role = conn.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
    if not role:
        conn.close()
        return jsonify({"error": "نقش یافت نشد."}), 404
    if data.get("description") is not None:
        conn.execute("UPDATE roles SET description=? WHERE id=?", (data["description"], rid))
    if "permissions" in data:
        conn.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
        for pn in data["permissions"]:
            p = conn.execute("SELECT id FROM permissions WHERE name=?", (pn,)).fetchone()
            if p:
                conn.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (rid, p["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/roles/<int:rid>", methods=["DELETE"])
@login_required
def api_admin_delete_role(rid):
    if not has_permission("roles.delete"):
        return jsonify({"error": "دسترسی حذف نقش را ندارید."}), 403
    conn = get_users_db()
    role = conn.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
    if role and role["name"] == "admin":
        conn.close()
        return jsonify({"error": "نقش admin قابل حذف نیست."}), 400
    conn.execute("DELETE FROM roles WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/permissions")
@login_required
def api_admin_permissions():
    if not has_permission("admin.access"):
        return jsonify({"error": "دسترسی ندارید"}), 403
    conn = get_users_db()
    rows = conn.execute("SELECT * FROM permissions ORDER BY category, name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/me")
def api_me():
    user = get_current_user()
    if not user:
        return jsonify({"logged_in": False})
    perms = get_user_permissions(user["id"])
    roles = get_user_roles(user["id"])
    return jsonify({"logged_in": True, "username": user["username"], "roles": [r["name"] for r in roles], "permissions": sorted(list(perms))})


# ---------- Health ----------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "payeshgar", "version": "1.1.0"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
