from __future__ import annotations

import io
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from modules.telegram_app.utils.channel_parser import parse_channel_input as parse_tg_input
from modules.web_app.utils.site_parser import parse_site_input as parse_web_input
from modules.x_app.utils.channel_parser import parse_channel_input as parse_x_input

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PORTAL_DB = ROOT_DIR / "data" / "users" / "portal.db"

PLATFORM_LABELS_FA = {
    "web": "وب‌سایت",
    "telegram": "کانال تلگرام",
    "x": "صفحه ایکس (X)",
}

STATUS_LABELS_FA = {
    "pending": "در انتظار بررسی",
    "approved": "تایید و فعال‌شده",
    "rejected": "رد شده",
}


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _strip_www(host: str) -> str:
    h = (host or "").strip().lower()
    return h[4:] if h.startswith("www.") else h


def _clean_excel_str(value: Optional[Any]) -> str:
    if value is None:
        return ""
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", str(value))
    if cleaned.startswith(("=", "+", "-", "@")):
        cleaned = "\u200c" + cleaned
    return cleaned


def normalize_request_source(platform: str, raw_input: str) -> Dict[str, str]:
    """
    Validates and normalizes a user's requested monitoring target using the exact
    same parsers as the Web, Telegram, and X collector modules.
    """
    plat = (platform or "auto").strip().lower()
    raw = (raw_input or "").strip()
    if not raw:
        raise ValueError("لطفاً آدرس وب‌سایت، کانال تلگرام یا صفحه ایکس مورد نظر را وارد کنید.")

    if plat == "auto":
        lower = raw.lower()
        if any(h in lower for h in ("x.com/", "twitter.com/", "www.x.com/", "www.twitter.com/")):
            plat = "x"
        elif any(h in lower for h in ("t.me/", "telegram.me/", "telegram.dog/")) or raw.startswith("@"):
            plat = "telegram"
        else:
            plat = "web"

    if plat == "web":
        parsed = parse_web_input(raw)
        return {
            "platform": "web",
            "raw_input": raw,
            "source_key": parsed.site_key,
            "source_handle": _strip_www(parsed.domain),
            "canonical_url": parsed.canonical_url,
            "default_title": parsed.default_title,
        }
    if plat == "telegram":
        parsed_tg = parse_tg_input(raw)
        handle = (parsed_tg.username or parsed_tg.channel_key).strip().lower()
        return {
            "platform": "telegram",
            "raw_input": raw,
            "source_key": parsed_tg.channel_key,
            "source_handle": handle,
            "canonical_url": parsed_tg.canonical_url,
            "default_title": parsed_tg.default_title,
        }
    if plat == "x":
        parsed_x = parse_x_input(raw)
        handle_x = (parsed_x.username or parsed_x.channel_key).strip().lower()
        return {
            "platform": "x",
            "raw_input": raw,
            "source_key": parsed_x.channel_key,
            "source_handle": handle_x,
            "canonical_url": parsed_x.canonical_url,
            "default_title": parsed_x.default_title,
        }

    raise ValueError("نوع پلتفرم انتخابی معتبر نیست (فقط web، telegram یا x مجاز است).")


class PortalService:
    """
    Manages:
      1. User monitoring requests & Admin replies (`monitoring_requests`)
      2. Coin & Accounting wallets with per-user news-to-coin ratio (`user_wallets`, `coin_transactions`)
      3. Per-user unlocked news tracking (`user_delivered_posts`) and isolated result querying/exporting
         against Admin's central Web, Telegram, and X collector databases.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_PORTAL_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS monitoring_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        platform TEXT NOT NULL,
                        raw_input TEXT NOT NULL,
                        source_key TEXT NOT NULL,
                        source_handle TEXT NOT NULL,
                        canonical_url TEXT NOT NULL,
                        title TEXT NOT NULL,
                        user_note TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        admin_reply TEXT NOT NULL DEFAULT '',
                        replied_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(user_id, platform, source_key)
                    );

                    CREATE INDEX IF NOT EXISTS idx_mon_req_user
                        ON monitoring_requests(user_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_mon_req_status
                        ON monitoring_requests(status, created_at DESC);

                    CREATE TABLE IF NOT EXISTS user_wallets (
                        user_id INTEGER PRIMARY KEY,
                        coin_balance REAL NOT NULL DEFAULT 0,
                        posts_per_coin INTEGER NOT NULL DEFAULT 100,
                        coins_per_batch REAL NOT NULL DEFAULT 1,
                        total_delivered_posts INTEGER NOT NULL DEFAULT 0,
                        unbilled_posts INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS coin_transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        tx_type TEXT NOT NULL,
                        amount REAL NOT NULL,
                        balance_after REAL NOT NULL,
                        posts_count INTEGER NOT NULL DEFAULT 0,
                        description TEXT NOT NULL,
                        created_by TEXT NOT NULL DEFAULT 'system',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_coin_tx_user
                        ON coin_transactions(user_id, id DESC);

                    CREATE TABLE IF NOT EXISTS user_delivered_posts (
                        user_id INTEGER NOT NULL,
                        post_uid TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        delivered_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, post_uid)
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Wallet & Coin Accounting
    # ------------------------------------------------------------------
    def _ensure_wallet_row(self, conn: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
        uid = int(user_id)
        row = conn.execute("SELECT * FROM user_wallets WHERE user_id = ?", (uid,)).fetchone()
        if row:
            return dict(row)
        now = utc_now_str()
        conn.execute(
            """
            INSERT INTO user_wallets (user_id, coin_balance, posts_per_coin, coins_per_batch, total_delivered_posts, unbilled_posts, updated_at)
            VALUES (?, 0, 100, 1, 0, 0, ?)
            """,
            (uid, now),
        )
        row = conn.execute("SELECT * FROM user_wallets WHERE user_id = ?", (uid,)).fetchone()
        return dict(row)

    @staticmethod
    def _compute_remaining_allowance(wallet: Dict[str, Any]) -> int:
        balance = max(0.0, float(wallet["coin_balance"] or 0.0))
        ppc = max(1, int(wallet["posts_per_coin"] or 100))
        cpb = max(0.0001, float(wallet["coins_per_batch"] or 1.0))
        unbilled = max(0, int(wallet["unbilled_posts"] or 0))
        paid_batches = math.floor((balance + 1e-9) / cpb)
        if paid_batches <= 0:
            return 0
        return max(0, int(paid_batches * ppc) - unbilled)

    @staticmethod
    def _format_coin_num(val: float) -> Any:
        rounded = round(float(val), 4)
        if abs(rounded - round(rounded)) < 1e-6:
            return int(round(rounded))
        return rounded

    def _serialize_wallet(self, wallet: Dict[str, Any]) -> Dict[str, Any]:
        ppc = max(1, int(wallet["posts_per_coin"] or 100))
        cpb = float(wallet["coins_per_batch"] or 1.0)
        unbilled = max(0, int(wallet["unbilled_posts"] or 0))
        allowance = self._compute_remaining_allowance(wallet)
        return {
            "user_id": int(wallet["user_id"]),
            "coin_balance": self._format_coin_num(wallet["coin_balance"]),
            "posts_per_coin": ppc,
            "coins_per_batch": self._format_coin_num(cpb),
            "total_delivered_posts": int(wallet["total_delivered_posts"] or 0),
            "unbilled_posts": unbilled,
            "remaining_posts_allowance": allowance,
            "progress_percent": min(100, round((unbilled / ppc) * 100)) if allowance > 0 else 0,
            "updated_at": wallet["updated_at"],
        }

    def get_wallet(self, user_id: int) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                w = self._ensure_wallet_row(conn, user_id)
                conn.commit()
                return self._serialize_wallet(w)
            finally:
                conn.close()

    def set_user_rate(
        self,
        user_id: int,
        *,
        posts_per_coin: int,
        coins_per_batch: float = 1.0,
        actor: str = "admin",
    ) -> Dict[str, Any]:
        if int(posts_per_coin) < 1:
            raise ValueError("تعداد خبر به ازای سکه باید حداقل ۱ باشد.")
        if float(coins_per_batch) <= 0:
            raise ValueError("مقدار سکه در نسبت تعیین‌شده باید بزرگ‌تر از صفر باشد.")

        ppc = int(posts_per_coin)
        cpb = round(float(coins_per_batch), 4)
        now = utc_now_str()

        with self._lock:
            conn = self._connect()
            try:
                w = self._ensure_wallet_row(conn, user_id)
                unbilled = min(int(w["unbilled_posts"] or 0), ppc - 1)
                conn.execute(
                    """
                    UPDATE user_wallets
                    SET posts_per_coin = ?, coins_per_batch = ?, unbilled_posts = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (ppc, cpb, unbilled, now, int(user_id)),
                )
                cpb_disp = self._format_coin_num(cpb)
                conn.execute(
                    """
                    INSERT INTO coin_transactions (user_id, tx_type, amount, balance_after, posts_count, description, created_by, created_at)
                    VALUES (?, 'rate_change', 0, ?, 0, ?, ?, ?)
                    """,
                    (
                        int(user_id),
                        float(w["coin_balance"]),
                        f"تنظیم تعرفه اختصاصی کاربر: هر {ppc} خبر = {cpb_disp} سکه",
                        actor,
                        now,
                    ),
                )
                conn.commit()
                updated = self._ensure_wallet_row(conn, user_id)
                return self._serialize_wallet(updated)
            finally:
                conn.close()

    def adjust_coins(
        self,
        user_id: int,
        *,
        amount: float,
        description: str = "",
        actor: str = "admin",
    ) -> Dict[str, Any]:
        delta = round(float(amount), 4)
        if abs(delta) < 1e-6:
            raise ValueError("مقدار شارژ یا کسر سکه نمی‌تواند صفر باشد.")

        now = utc_now_str()
        with self._lock:
            conn = self._connect()
            try:
                w = self._ensure_wallet_row(conn, user_id)
                old_bal = float(w["coin_balance"] or 0.0)
                new_bal = round(max(0.0, old_bal + delta), 4)
                actual_delta = round(new_bal - old_bal, 4)
                unbilled = int(w["unbilled_posts"] or 0)
                if new_bal <= 0:
                    unbilled = 0

                conn.execute(
                    """
                    UPDATE user_wallets
                    SET coin_balance = ?, unbilled_posts = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (new_bal, unbilled, now, int(user_id)),
                )
                tx_type = "charge" if delta > 0 else "admin_deduct"
                disp_amt = self._format_coin_num(abs(actual_delta))
                default_desc = (
                    f"شارژ حساب توسط ادمین (+{disp_amt} سکه)"
                    if delta > 0
                    else f"کسر دستی توسط ادمین (-{disp_amt} سکه)"
                )
                conn.execute(
                    """
                    INSERT INTO coin_transactions (user_id, tx_type, amount, balance_after, posts_count, description, created_by, created_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        int(user_id),
                        tx_type,
                        actual_delta,
                        new_bal,
                        (description or "").strip() or default_desc,
                        actor,
                        now,
                    ),
                )
                conn.commit()
                updated = self._ensure_wallet_row(conn, user_id)
                return self._serialize_wallet(updated)
            finally:
                conn.close()

    def list_transactions(self, user_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            if user_id is not None:
                rows = conn.execute(
                    "SELECT * FROM coin_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                    (int(user_id), int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM coin_transactions ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["amount"] = self._format_coin_num(d["amount"])
                d["balance_after"] = self._format_coin_num(d["balance_after"])
                result.append(d)
            return result
        finally:
            conn.close()

    def delete_user_data(self, user_id: int) -> None:
        uid = int(user_id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM monitoring_requests WHERE user_id = ?", (uid,))
                conn.execute("DELETE FROM user_delivered_posts WHERE user_id = ?", (uid,))
                conn.execute("DELETE FROM coin_transactions WHERE user_id = ?", (uid,))
                conn.execute("DELETE FROM user_wallets WHERE user_id = ?", (uid,))
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Monitoring Requests (User Submission & Admin Reply/Management)
    # ------------------------------------------------------------------
    def create_request(
        self,
        user_id: int,
        *,
        platform: str,
        raw_input: str,
        title: str = "",
        user_note: str = "",
    ) -> Dict[str, Any]:
        norm = normalize_request_source(platform, raw_input)
        final_title = (title or "").strip() or norm["default_title"]
        note = (user_note or "").strip()
        now = utc_now_str()
        uid = int(user_id)

        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    """
                    SELECT * FROM monitoring_requests
                    WHERE user_id = ? AND platform = ? AND source_key = ?
                    """,
                    (uid, norm["platform"], norm["source_key"]),
                ).fetchone()
                if existing:
                    if existing["status"] == "rejected":
                        conn.execute(
                            """
                            UPDATE monitoring_requests
                            SET raw_input = ?, title = ?, user_note = ?, status = 'pending',
                                admin_reply = '', replied_at = NULL, updated_at = ?
                            WHERE id = ?
                            """,
                            (norm["raw_input"], final_title, note, now, existing["id"]),
                        )
                        conn.commit()
                        row = conn.execute("SELECT * FROM monitoring_requests WHERE id = ?", (existing["id"],)).fetchone()
                        return dict(row)
                    raise ValueError("این منبع قبلاً در فهرست درخواست‌های شما ثبت شده است.")

                cur = conn.execute(
                    """
                    INSERT INTO monitoring_requests
                    (user_id, platform, raw_input, source_key, source_handle, canonical_url, title, user_note, status, admin_reply, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)
                    """,
                    (
                        uid,
                        norm["platform"],
                        norm["raw_input"],
                        norm["source_key"],
                        norm["source_handle"],
                        norm["canonical_url"],
                        final_title,
                        note,
                        now,
                        now,
                    ),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM monitoring_requests WHERE id = ?", (cur.lastrowid,)).fetchone()
                return dict(row)
            finally:
                conn.close()

    def delete_user_request(self, user_id: int, request_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM monitoring_requests WHERE id = ? AND user_id = ?",
                    (int(request_id), int(user_id)),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_request_by_id(self, request_id: int) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM monitoring_requests WHERE id = ?", (int(request_id),)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_request_by_admin(
        self,
        request_id: int,
        *,
        status: Optional[str] = None,
        admin_reply: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        now = utc_now_str()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM monitoring_requests WHERE id = ?", (int(request_id),)).fetchone()
                if not row:
                    return None
                new_status = (status or row["status"]).strip().lower()
                if new_status not in ("pending", "approved", "rejected"):
                    raise ValueError("وضعیت درخواست معتبر نیست.")
                new_reply = row["admin_reply"] if admin_reply is None else admin_reply.strip()
                replied_at = now if (admin_reply is not None and new_reply) else row["replied_at"]

                conn.execute(
                    """
                    UPDATE monitoring_requests
                    SET status = ?, admin_reply = ?, replied_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status, new_reply, replied_at, now, int(request_id)),
                )
                conn.commit()
                updated = conn.execute("SELECT * FROM monitoring_requests WHERE id = ?", (int(request_id),)).fetchone()
                return dict(updated)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Central Collector Matching & Isolated User Results
    # ------------------------------------------------------------------
    @staticmethod
    def _index_central_sources(workspaces: Any) -> Dict[str, List[Dict[str, Any]]]:
        """Reads all configured sources from Admin's central collector workspaces (uid=1)."""
        web_db = workspaces.get_runtime(1, "web").db
        tg_db = workspaces.get_runtime(1, "telegram").db
        x_db = workspaces.get_runtime(1, "x").db
        return {
            "web": web_db.list_sites(),
            "telegram": tg_db.list_channels(),
            "x": x_db.list_channels(),
        }

    @staticmethod
    def _match_request_to_central_ids(
        req: Dict[str, Any],
        central_sources: Dict[str, List[Dict[str, Any]]],
    ) -> List[int]:
        plat = req["platform"]
        s_key = (req["source_key"] or "").strip().lower()
        s_handle = _strip_www(req["source_handle"] or "")
        s_url = (req["canonical_url"] or "").strip().lower().rstrip("/")

        matched_ids: List[int] = []
        if plat == "web":
            for s in central_sources.get("web", []):
                c_key = (s.get("site_key") or "").strip().lower()
                c_dom = _strip_www(s.get("domain") or "")
                c_url = (s.get("canonical_url") or "").strip().lower().rstrip("/")
                if c_key == s_key or (c_url and c_url == s_url) or (c_dom and c_dom == s_handle):
                    matched_ids.append(int(s["id"]))
        elif plat in ("telegram", "x"):
            for ch in central_sources.get(plat, []):
                c_key = (ch.get("channel_key") or "").strip().lower()
                c_user = (ch.get("username") or "").strip().lower()
                c_url = (ch.get("canonical_url") or "").strip().lower().rstrip("/")
                if c_key == s_key or (c_user and c_user == s_handle) or (c_url and c_url == s_url):
                    matched_ids.append(int(ch["id"]))
        return matched_ids

    def list_user_requests(self, user_id: int, workspaces: Any) -> List[Dict[str, Any]]:
        central = self._index_central_sources(workspaces)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM monitoring_requests WHERE user_id = ? ORDER BY id DESC",
                (int(user_id),),
            ).fetchall()
        finally:
            conn.close()

        items = []
        for r in rows:
            d = dict(r)
            matched_ids = self._match_request_to_central_ids(d, central)
            d["platform_label"] = PLATFORM_LABELS_FA.get(d["platform"], d["platform"])
            d["status_label"] = STATUS_LABELS_FA.get(d["status"], d["status"])
            d["in_collector"] = len(matched_ids) > 0 and d["status"] != "rejected"
            items.append(d)
        return items

    def list_all_requests_for_admin(
        self,
        users_by_id: Dict[int, Dict[str, Any]],
        workspaces: Any,
        *,
        status: Optional[str] = None,
        platform: Optional[str] = None,
        q: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        central = self._index_central_sources(workspaces)
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM monitoring_requests ORDER BY id DESC").fetchall()
            # Count how many users requested each (platform, source_key)
            shared_counts: Dict[Tuple[str, str], int] = {}
            for r in rows:
                k = (r["platform"], r["source_key"])
                shared_counts[k] = shared_counts.get(k, 0) + 1
        finally:
            conn.close()

        q_norm = (q or "").strip().lower()
        result: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if status and status != "all" and d["status"] != status:
                continue
            if platform and platform != "all" and d["platform"] != platform:
                continue
            u = users_by_id.get(int(d["user_id"]), {})
            username = u.get("username", f"user_{d['user_id']}")
            if q_norm:
                hay = f"{username} {d['title']} {d['canonical_url']} {d['raw_input']} {d['user_note']} {d['admin_reply']}".lower()
                if q_norm not in hay:
                    continue

            matched_ids = self._match_request_to_central_ids(d, central)
            d["username"] = username
            d["platform_label"] = PLATFORM_LABELS_FA.get(d["platform"], d["platform"])
            d["status_label"] = STATUS_LABELS_FA.get(d["status"], d["status"])
            d["in_collector"] = len(matched_ids) > 0
            d["shared_requesters_count"] = shared_counts.get((d["platform"], d["source_key"]), 1)
            result.append(d)
        return result

    def register_request_in_central_collector(self, req: Dict[str, Any], workspaces: Any) -> Dict[str, Any]:
        """
        1-Click helper for Admin: registers the requested Website / Telegram channel / X account
        directly into Admin's central collector workspace (uid=1) if not already present.
        """
        plat = req["platform"]
        rt = workspaces.get_runtime(1, plat)
        central = self._index_central_sources(workspaces)
        existing_ids = self._match_request_to_central_ids(req, central)
        if existing_ids:
            return {"already_exists": True, "source_id": existing_ids[0], "platform": plat}

        if plat == "web":
            parsed = parse_web_input(req["raw_input"])
            created = rt.db.create_site(
                parsed=parsed,
                title=req.get("title") or parsed.default_title,
                interval_value=6,
                interval_unit="hours",
                initial_limit=20,
            )
            return {"already_exists": False, "source_id": created["id"], "platform": "web"}

        if plat == "telegram":
            parsed_tg = parse_tg_input(req["raw_input"])
            created_tg = rt.db.create_channel(
                parsed=parsed_tg,
                title=req.get("title") or parsed_tg.default_title,
                interval_value=6,
                interval_unit="hours",
                initial_limit=20,
            )
            return {"already_exists": False, "source_id": created_tg["id"], "platform": "telegram"}

        if plat == "x":
            parsed_x = parse_x_input(req["raw_input"])
            created_x = rt.db.create_channel(
                parsed=parsed_x,
                title=req.get("title") or parsed_x.default_title,
                interval_value=6,
                interval_unit="hours",
                initial_limit=20,
            )
            return {"already_exists": False, "source_id": created_x["id"], "platform": "x"}

        raise ValueError("پلتفرم نامعتبر است.")

    def _collect_candidate_posts_for_user(
        self,
        user_id: int,
        workspaces: Any,
        *,
        platform: Optional[str] = None,
        request_id: Optional[int] = None,
        q: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Fetches all posts from Admin's central Web, Telegram, and X databases that
        strictly match this user's non-rejected monitoring requests.
        """
        user_reqs = self.list_user_requests(user_id, workspaces)
        active_reqs = [r for r in user_reqs if r["status"] != "rejected"]
        if request_id is not None:
            active_reqs = [r for r in active_reqs if int(r["id"]) == int(request_id)]
        if platform and platform != "all":
            active_reqs = [r for r in active_reqs if r["platform"] == platform]

        central = self._index_central_sources(workspaces)
        web_site_to_req: Dict[int, Dict[str, Any]] = {}
        tg_ch_to_req: Dict[int, Dict[str, Any]] = {}
        x_ch_to_req: Dict[int, Dict[str, Any]] = {}

        for req in active_reqs:
            m_ids = self._match_request_to_central_ids(req, central)
            for mid in m_ids:
                if req["platform"] == "web":
                    web_site_to_req.setdefault(mid, req)
                elif req["platform"] == "telegram":
                    tg_ch_to_req.setdefault(mid, req)
                elif req["platform"] == "x":
                    x_ch_to_req.setdefault(mid, req)

        candidates: List[Dict[str, Any]] = []
        q_norm = (q or "").strip().lower()
        d_from = (date_from or "").strip()
        d_to = (date_to or "").strip()

        # 1. Query Web posts
        if web_site_to_req:
            web_db = workspaces.get_runtime(1, "web").db
            placeholders = ",".join("?" for _ in web_site_to_req)
            params: List[Any] = list(web_site_to_req.keys())
            sql = f"SELECT * FROM posts WHERE site_id IN ({placeholders})"
            if d_from:
                sql += " AND published_date >= ?"
                params.append(d_from)
            if d_to:
                sql += " AND published_date <= ?"
                params.append(d_to)
            with web_db.connection() as wconn:
                for row in wconn.execute(sql, params).fetchall():
                    title = row["title"] or ""
                    content = row["content"] or ""
                    if q_norm and q_norm not in f"{title} {content} {row['site_title']}".lower():
                        continue
                    req_ref = web_site_to_req[int(row["site_id"])]
                    post_url = row["post_url"] or f"web-{row['id']}"
                    candidates.append(
                        {
                            "post_uid": f"web:{post_url}",
                            "platform": "web",
                            "platform_label": PLATFORM_LABELS_FA["web"],
                            "request_id": req_ref["id"],
                            "source_title": row["site_title"] or req_ref["title"],
                            "source_handle": row["site_domain"] or req_ref["source_handle"],
                            "title": title,
                            "content": content,
                            "post_url": row["post_url"],
                            "published_date": row["published_date"],
                            "published_jalali_date": row["published_jalali_date"],
                            "published_time": row["published_time"],
                            "published_at_utc": row["published_at_utc"],
                            "views": row["views"],
                            "media_type": row["media_type"],
                        }
                    )

        # 2. Query Telegram & X posts
        for mod, mapping in (("telegram", tg_ch_to_req), ("x", x_ch_to_req)):
            if not mapping:
                continue
            mod_db = workspaces.get_runtime(1, mod).db
            placeholders = ",".join("?" for _ in mapping)
            params_m: List[Any] = list(mapping.keys())
            sql_m = f"SELECT * FROM posts WHERE channel_id IN ({placeholders})"
            if d_from:
                sql_m += " AND published_date >= ?"
                params_m.append(d_from)
            if d_to:
                sql_m += " AND published_date <= ?"
                params_m.append(d_to)
            with mod_db.connection() as mconn:
                for row in mconn.execute(sql_m, params_m).fetchall():
                    content = row["content"] or ""
                    first_line = content.splitlines()[0].strip() if content.strip() else ""
                    headline = (first_line[:95] + "…") if len(first_line) > 95 else (first_line or f"پیام #{row['message_id']}")
                    if q_norm and q_norm not in f"{content} {row['channel_title']} {row['channel_username']}".lower():
                        continue
                    req_ref = mapping[int(row["channel_id"])]
                    post_url = row["post_url"] or f"{mod}-{row['id']}"
                    candidates.append(
                        {
                            "post_uid": f"{mod}:{post_url}",
                            "platform": mod,
                            "platform_label": PLATFORM_LABELS_FA[mod],
                            "request_id": req_ref["id"],
                            "source_title": row["channel_title"] or req_ref["title"],
                            "source_handle": row["channel_username"] or req_ref["source_handle"],
                            "title": headline,
                            "content": content,
                            "post_url": row["post_url"],
                            "published_date": row["published_date"],
                            "published_jalali_date": row["published_jalali_date"],
                            "published_time": row["published_time"],
                            "published_at_utc": row["published_at_utc"],
                            "views": row["views"],
                            "media_type": row["media_type"],
                        }
                    )

        candidates.sort(key=lambda x: x.get("published_at_utc") or "", reverse=True)
        return candidates, user_reqs

    def query_user_results(
        self,
        user_id: int,
        workspaces: Any,
        *,
        platform: Optional[str] = None,
        request_id: Optional[int] = None,
        q: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
        export_all: bool = False,
    ) -> Dict[str, Any]:
        """
        Returns isolated news/posts for `user_id` while enforcing coin accounting:
          - Posts previously delivered to `user_id` are always accessible at zero extra cost.
          - Unseen posts are unlocked only up to the user's remaining coin allowance, and
            automatically deduct coins every `posts_per_coin` newly delivered items!
        """
        uid = int(user_id)
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))

        candidates, user_reqs = self._collect_candidate_posts_for_user(
            uid,
            workspaces,
            platform=platform,
            request_id=request_id,
            q=q,
            date_from=date_from,
            date_to=date_to,
        )

        with self._lock:
            conn = self._connect()
            try:
                wallet_row = self._ensure_wallet_row(conn, uid)
                delivered_rows = conn.execute(
                    "SELECT post_uid FROM user_delivered_posts WHERE user_id = ?",
                    (uid,),
                ).fetchall()
                delivered_set: Set[str] = {r["post_uid"] for r in delivered_rows}

                allowance = self._compute_remaining_allowance(wallet_row)

                # Partition/filter candidates according to current coin allowance:
                # Already-delivered items are always available; unseen items can only be shown up to `allowance`.
                accessible_items: List[Dict[str, Any]] = []
                locked_new_count = 0
                unseen_budget_probe = allowance

                for item in candidates:
                    if item["post_uid"] in delivered_set:
                        accessible_items.append(item)
                    elif unseen_budget_probe > 0:
                        accessible_items.append(item)
                        unseen_budget_probe -= 1
                    else:
                        locked_new_count += 1

                total_accessible = len(accessible_items)
                if export_all:
                    shown_slice = accessible_items
                else:
                    start = (page - 1) * page_size
                    shown_slice = accessible_items[start : start + page_size]

                # Meter ONLY the newly delivered items actually shown/exported in this response!
                newly_unlocked = [it for it in shown_slice if it["post_uid"] not in delivered_set]
                coins_deducted = 0.0
                if newly_unlocked:
                    now = utc_now_str()
                    conn.executemany(
                        """
                        INSERT OR IGNORE INTO user_delivered_posts (user_id, post_uid, platform, delivered_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(uid, it["post_uid"], it["platform"], now) for it in newly_unlocked],
                    )
                    new_count = len(newly_unlocked)
                    ppc = max(1, int(wallet_row["posts_per_coin"] or 100))
                    cpb = max(0.0001, float(wallet_row["coins_per_batch"] or 1.0))
                    total_deliv = int(wallet_row["total_delivered_posts"] or 0) + new_count
                    unbilled_total = int(wallet_row["unbilled_posts"] or 0) + new_count

                    completed_batches = unbilled_total // ppc
                    rem_unbilled = unbilled_total % ppc
                    old_bal = float(wallet_row["coin_balance"] or 0.0)
                    new_bal = old_bal

                    if completed_batches > 0:
                        coins_deducted = round(completed_batches * cpb, 4)
                        new_bal = round(max(0.0, old_bal - coins_deducted), 4)
                        if new_bal <= 0:
                            rem_unbilled = 0
                        billed_posts = completed_batches * ppc
                        disp_ded = self._format_coin_num(coins_deducted)
                        disp_cpb = self._format_coin_num(cpb)
                        conn.execute(
                            """
                            INSERT INTO coin_transactions
                            (user_id, tx_type, amount, balance_after, posts_count, description, created_by, created_at)
                            VALUES (?, 'auto_deduct', ?, ?, ?, ?, 'system', ?)
                            """,
                            (
                                uid,
                                -coins_deducted,
                                new_bal,
                                billed_posts,
                                f"کسر خودکار {disp_ded} سکه بابت نمایش {billed_posts} خبر جدید (نرخ: هر {ppc} خبر = {disp_cpb} سکه)",
                                now,
                            ),
                        )

                    conn.execute(
                        """
                        UPDATE user_wallets
                        SET coin_balance = ?, total_delivered_posts = ?, unbilled_posts = ?, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (new_bal, total_deliv, rem_unbilled, now, uid),
                    )

                conn.commit()
                final_wallet = self._serialize_wallet(self._ensure_wallet_row(conn, uid))
            finally:
                conn.close()

        total_pages = max(1, math.ceil(total_accessible / page_size)) if not export_all else 1
        return {
            "items": shown_slice,
            "total": total_accessible,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "locked_new_count": locked_new_count,
            "insufficient_coins": locked_new_count > 0 and final_wallet["remaining_posts_allowance"] == 0,
            "newly_unlocked_count": len(newly_unlocked),
            "coins_deducted_now": self._format_coin_num(coins_deducted),
            "wallet": final_wallet,
            "requests": user_reqs,
        }

    @staticmethod
    def build_results_excel_bytes(items: List[Dict[str, Any]], username: str) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.title = "نتایج درخواست‌ها"
        ws.views.sheetView[0].rightToLeft = True

        headers = [
            "ردیف",
            "پلتفرم",
            "منبع درخواستی",
            "شناسه / دامنه",
            "عنوان خبر / مطلب",
            "متن کامل",
            "تاریخ شمسی",
            "تاریخ میلادی",
            "ساعت",
            "بازدید",
            "لینک مستقیم",
        ]
        ws.append(headers)

        header_fill = PatternFill("solid", fgColor="1E3A8A")
        header_font = Font(name="Tahoma", size=10, bold=True, color="FFFFFF")
        body_font = Font(name="Tahoma", size=10)
        thin = Side(border_style="thin", color="CBD5E1")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border

        for idx, it in enumerate(items, start=1):
            row_vals = [
                idx,
                _clean_excel_str(it.get("platform_label")),
                _clean_excel_str(it.get("source_title")),
                _clean_excel_str(it.get("source_handle")),
                _clean_excel_str(it.get("title")),
                _clean_excel_str(it.get("content")),
                _clean_excel_str(it.get("published_jalali_date")),
                _clean_excel_str(it.get("published_date")),
                _clean_excel_str(it.get("published_time")),
                it.get("views") if it.get("views") is not None else "",
                _clean_excel_str(it.get("post_url")),
            ]
            ws.append(row_vals)
            r_num = idx + 1
            for c_idx in range(1, len(headers) + 1):
                c = ws.cell(row=r_num, column=c_idx)
                c.font = body_font
                c.border = border
                c.alignment = Alignment(horizontal="right", vertical="top", wrap_text=(c_idx in (5, 6)))

        widths = [8, 16, 24, 20, 38, 60, 14, 14, 10, 12, 42]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
