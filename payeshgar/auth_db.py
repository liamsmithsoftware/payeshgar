from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set

from werkzeug.security import check_password_hash, generate_password_hash

ROOT_DIR = Path(__file__).resolve().parent.parent
USERS_DATA_DIR = Path(os.getenv("PAYESHGAR_USERS_DIR", str(ROOT_DIR / "data" / "users")))
USERS_DB_PATH = Path(os.getenv("PAYESHGAR_USERS_DB", str(USERS_DATA_DIR / "users.db")))

# 27 Granular Permissions (from high-level section access down to individual actions)
PERMISSIONS = [
    # محیط وب
    ("web.view", "مشاهده محیط وب، آمار، لیست سایت‌ها و آرشیو مطالب", "وب"),
    ("web.sources", "افزودن، ویرایش، تست اتصال و تغییر وضعیت وب‌سایت‌ها", "وب"),
    ("web.crawl", "دریافت و همگام‌سازی فوری وب‌سایت‌ها (تکی و همگانی)", "وب"),
    ("web.delete", "حذف وب‌سایت، حذف مطالب، پاک‌سازی آرشیو و گزارش‌ها", "وب"),
    ("web.export", "دریافت خروجی اکسل (.xlsx)، دیتابیس SQLite و پشتیبان JSON", "وب"),
    ("web.settings", "ویرایش تنظیمات شبکه/پروکسی و بازیابی فایل پشتیبان در وب", "وب"),
    # محیط تلگرام
    ("telegram.view", "مشاهده محیط تلگرام، آمار، لیست کانال‌ها و آرشیو مطالب", "تلگرام"),
    ("telegram.sources", "افزودن، ویرایش و تغییر وضعیت کانال‌های تلگرام", "تلگرام"),
    ("telegram.crawl", "دریافت و همگام‌سازی فوری کانال‌ها (تکی و همگانی)", "تلگرام"),
    ("telegram.delete", "حذف کانال، حذف مطالب، پاک‌سازی آرشیو و گزارش‌ها", "تلگرام"),
    ("telegram.export", "دریافت خروجی اکسل (.xlsx)، دیتابیس SQLite و پشتیبان JSON", "تلگرام"),
    ("telegram.settings", "ویرایش تنظیمات، اتصال Telethon و بازیابی پشتیبان در تلگرام", "تلگرام"),
    # محیط ایکس
    ("x.view", "مشاهده محیط ایکس، آمار، لیست حساب‌ها و آرشیو مطالب", "ایکس"),
    ("x.sources", "افزودن، ویرایش و تغییر وضعیت حساب‌های ایکس", "ایکس"),
    ("x.crawl", "دریافت و همگام‌سازی فوری حساب‌ها (تکی و همگانی)", "ایکس"),
    ("x.delete", "حذف حساب، حذف مطالب، بازنشانی نشانگر و پاک‌سازی آرشیو", "ایکس"),
    ("x.export", "دریافت خروجی اکسل (.xlsx)، دیتابیس SQLite و پشتیبان JSON", "ایکس"),
    ("x.settings", "ویرایش تنظیمات پروکسی و بازیابی فایل پشتیبان در ایکس", "ایکس"),
    # مدیریت کاربران
    ("users.view", "مشاهده لیست کاربران سامانه", "کاربران"),
    ("users.create", "ایجاد کاربر جدید", "کاربران"),
    ("users.edit", "ویرایش کاربر، رمز عبور، وضعیت و نقش‌ها", "کاربران"),
    ("users.delete", "حذف کاربر", "کاربران"),
    # مدیریت نقش‌ها
    ("roles.view", "مشاهده نقش‌ها و دسترسی‌ها", "نقش‌ها"),
    ("roles.create", "تعریف نقش جدید", "نقش‌ها"),
    ("roles.edit", "ویرایش نقش و تخصیص دسترسی‌ها", "نقش‌ها"),
    ("roles.delete", "حذف نقش", "نقش‌ها"),
    # پنل مدیریت
    ("admin.access", "دسترسی به پنل مدیریت ACL", "مدیریت"),
]

PERM_LABELS: Dict[str, str] = {name: desc for name, desc, _ in PERMISSIONS}


class AuthDB:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else USERS_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def init_db(self) -> None:
        conn = self.connect()
        cur = conn.cursor()
        cur.executescript(
            """
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
            """
        )

        for name, desc, cat in PERMISSIONS:
            cur.execute(
                "INSERT INTO permissions (name, description, category) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description, category=excluded.category",
                (name, desc, cat),
            )

        cur.execute("INSERT OR IGNORE INTO roles (name, description) VALUES (?,?)", ("admin", "مدیر کل سیستم — دسترسی کامل"))
        cur.execute("INSERT OR IGNORE INTO roles (name, description) VALUES (?,?)", ("operator", "اپراتور پایش — مشاهده، افزودن منبع، دریافت فوری و خروجی"))
        cur.execute("INSERT OR IGNORE INTO roles (name, description) VALUES (?,?)", ("viewer", "مشاهده‌کننده — فقط مشاهده و خروجی"))

        # Ensure admin always holds every permission
        admin_role = cur.execute("SELECT id FROM roles WHERE name='admin'").fetchone()
        if admin_role:
            for p in cur.execute("SELECT id FROM permissions").fetchall():
                cur.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (admin_role["id"], p["id"]))

        # Seed default operator & viewer permissions only if they currently have 0 permissions
        op_role = cur.execute("SELECT id FROM roles WHERE name='operator'").fetchone()
        if op_role:
            op_count = cur.execute("SELECT COUNT(*) AS c FROM role_permissions WHERE role_id=?", (op_role["id"],)).fetchone()["c"]
            if op_count == 0:
                for m in ("web", "telegram", "x"):
                    for act in ("view", "sources", "crawl", "export"):
                        pr = cur.execute("SELECT id FROM permissions WHERE name=?", (f"{m}.{act}",)).fetchone()
                        if pr:
                            cur.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (op_role["id"], pr["id"]))

        vw_role = cur.execute("SELECT id FROM roles WHERE name='viewer'").fetchone()
        if vw_role:
            vw_count = cur.execute("SELECT COUNT(*) AS c FROM role_permissions WHERE role_id=?", (vw_role["id"],)).fetchone()["c"]
            if vw_count == 0:
                for m in ("web", "telegram", "x"):
                    for act in ("view", "export"):
                        pr = cur.execute("SELECT id FROM permissions WHERE name=?", (f"{m}.{act}",)).fetchone()
                        if pr:
                            cur.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (vw_role["id"], pr["id"]))

        admin_user = cur.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not admin_user:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (?,?)",
                ("admin", generate_password_hash("Admin@1234")),
            )
            uid = cur.lastrowid
            if admin_role:
                cur.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, admin_role["id"]))

        conn.commit()
        conn.close()

    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        conn = self.connect()
        row = conn.execute("SELECT * FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def verify_credentials(self, username: str, password: str) -> tuple[Optional[Dict], Optional[str]]:
        conn = self.connect()
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if not row or not check_password_hash(row["password_hash"], password):
            return None, "نام کاربری یا رمز عبور اشتباه است."
        if not row["is_active"]:
            return None, "حساب کاربری شما غیرفعال شده است."
        return dict(row), None

    def get_user_permissions(self, user_id: int) -> Set[str]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT DISTINCT p.name FROM permissions p
            JOIN role_permissions rp ON rp.permission_id = p.id
            JOIN user_roles ur ON ur.role_id = rp.role_id
            WHERE ur.user_id = ?
            """,
            (user_id,),
        ).fetchall()
        conn.close()
        perms = {r["name"] for r in rows}

        # Implicit section view if any action inside that section is granted
        for mod in ("web", "telegram", "x"):
            if any(p.startswith(f"{mod}.") for p in perms):
                perms.add(f"{mod}.view")
        if any(p.startswith(("users.", "roles.")) for p in perms):
            perms.add("admin.access")
            if perms & {"users.create", "users.edit", "users.delete"}:
                perms.add("users.view")
            if perms & {"roles.create", "roles.edit", "roles.delete"}:
                perms.add("roles.view")
        return perms

    def get_user_roles(self, user_id: int) -> List[Dict]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT r.* FROM roles r
            JOIN user_roles ur ON ur.role_id = r.id
            WHERE ur.user_id = ?
            """,
            (user_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def list_users(self) -> List[Dict]:
        conn = self.connect()
        users = conn.execute("SELECT id, username, is_active, created_at FROM users ORDER BY id").fetchall()
        result = []
        for u in users:
            roles = conn.execute(
                "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?",
                (u["id"],),
            ).fetchall()
            result.append(
                {
                    "id": u["id"],
                    "username": u["username"],
                    "is_active": u["is_active"],
                    "created_at": u["created_at"],
                    "roles": [r["name"] for r in roles],
                }
            )
        conn.close()
        return result

    def create_user(self, username: str, password: str, roles: List[str]) -> int:
        conn = self.connect()
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?,?)",
                (username, generate_password_hash(password)),
            )
            uid = cur.lastrowid
            for rn in roles:
                r = conn.execute("SELECT id FROM roles WHERE name=?", (rn,)).fetchone()
                if r:
                    conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, r["id"]))
            conn.commit()
            return int(uid)
        finally:
            conn.close()

    def update_user(self, uid: int, *, password: Optional[str] = None, is_active: Optional[bool] = None, roles: Optional[List[str]] = None) -> bool:
        conn = self.connect()
        try:
            user = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
            if not user:
                return False
            if password:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), uid))
            if is_active is not None:
                conn.execute("UPDATE users SET is_active=? WHERE id=?", (1 if is_active else 0, uid))
            if roles is not None:
                conn.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
                for rn in roles:
                    r = conn.execute("SELECT id FROM roles WHERE name=?", (rn,)).fetchone()
                    if r:
                        conn.execute("INSERT OR IGNORE INTO user_roles (user_id, role_id) VALUES (?,?)", (uid, r["id"]))
            conn.commit()
            return True
        finally:
            conn.close()

    def delete_user(self, uid: int) -> None:
        conn = self.connect()
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        conn.close()

    def list_roles(self) -> List[Dict]:
        conn = self.connect()
        roles = conn.execute("SELECT * FROM roles ORDER BY id").fetchall()
        result = []
        for r in roles:
            perms = conn.execute(
                "SELECT p.name FROM permissions p JOIN role_permissions rp ON rp.permission_id=p.id WHERE rp.role_id=?",
                (r["id"],),
            ).fetchall()
            result.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "description": r["description"],
                    "created_at": r["created_at"],
                    "permissions": [p["name"] for p in perms],
                }
            )
        conn.close()
        return result

    def create_role(self, name: str, description: str, permissions: List[str]) -> int:
        conn = self.connect()
        try:
            cur = conn.execute("INSERT INTO roles (name, description) VALUES (?,?)", (name, description))
            rid = cur.lastrowid
            for pn in permissions:
                p = conn.execute("SELECT id FROM permissions WHERE name=?", (pn,)).fetchone()
                if p:
                    conn.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (rid, p["id"]))
            conn.commit()
            return int(rid)
        finally:
            conn.close()

    def update_role(self, rid: int, *, description: Optional[str] = None, permissions: Optional[List[str]] = None) -> bool:
        conn = self.connect()
        try:
            role = conn.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
            if not role:
                return False
            if description is not None:
                conn.execute("UPDATE roles SET description=? WHERE id=?", (description, rid))
            if permissions is not None:
                conn.execute("DELETE FROM role_permissions WHERE role_id=?", (rid,))
                for pn in permissions:
                    p = conn.execute("SELECT id FROM permissions WHERE name=?", (pn,)).fetchone()
                    if p:
                        conn.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) VALUES (?,?)", (rid, p["id"]))
            conn.commit()
            return True
        finally:
            conn.close()

    def delete_role(self, rid: int) -> tuple[bool, Optional[str]]:
        conn = self.connect()
        try:
            role = conn.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
            if not role:
                return False, "نقش یافت نشد."
            if role["name"] == "admin":
                return False, "نقش admin قابل حذف نیست."
            conn.execute("DELETE FROM roles WHERE id=?", (rid,))
            conn.commit()
            return True, None
        finally:
            conn.close()

    def list_permissions(self) -> List[Dict]:
        conn = self.connect()
        rows = conn.execute("SELECT * FROM permissions ORDER BY id").fetchall()
        conn.close()
        return [dict(r) for r in rows]


def required_permission_for_subapp(mod: str, method: str, sub_path: str) -> str:
    """
    Maps any request inside /{mod}/... (where mod in {'web','telegram','x'})
    to its exact granular ACL permission.
    """
    m = method.upper()
    p = "/" + sub_path.lstrip("/")

    if p.startswith("/static/") or p in ("/", "/healthz"):
        return f"{mod}.view"

    if not p.startswith("/api/"):
        return f"{mod}.view"

    # Exports & Backup Export/Inspect
    if p.startswith("/api/exports/") or p in ("/api/backup/export", "/api/backup/inspect"):
        return f"{mod}.export"

    # Backup Restore / Validate & Settings Mutation & Telethon Auth
    if p.startswith("/api/backup/restore") or p == "/api/backup/validate-file":
        return f"{mod}.settings"
    if p.startswith("/api/telethon/"):
        return f"{mod}.settings"
    if p == "/api/settings" and m in ("PUT", "POST", "PATCH"):
        return f"{mod}.settings"

    # Crawl / Sync triggers
    if m == "POST" and (p.endswith("/sync") or p.endswith("/sync-all")):
        return f"{mod}.crawl"

    # Destructive / Cleanup / Reset / Delete actions
    if m == "DELETE":
        return f"{mod}.delete"
    if m == "POST" and (
        p in ("/api/posts/bulk-delete", "/api/posts/purge")
        or p.endswith("/reset")
    ):
        return f"{mod}.delete"

    # Adding / Updating / Probing sources or seeding demo data
    if p == "/api/demo/seed" and m == "POST":
        return f"{mod}.sources"
    if p.startswith(("/api/sites", "/api/channels")) and m in ("POST", "PUT", "PATCH"):
        return f"{mod}.sources"

    # All remaining GET / read endpoints (/api/overview, /api/sites, /api/channels, /api/posts, /api/logs, /api/settings)
    return f"{mod}.view"
