from __future__ import annotations

import html as html_lib
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from modules.telegram_app.main import create_app as create_telegram_app
from modules.web_app.main import create_app as create_web_app
from modules.x_app.main import create_app as create_x_app
from payeshgar.auth_db import PERM_LABELS, AuthDB, required_permission_for_subapp
from payeshgar.captcha import CaptchaService
from payeshgar.workspaces import WorkspaceRegistry

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "static"

COOKIE_NAME = "payeshgar_session"
SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours


def is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return proto == "https"


def append_token_to_url(target_url: str, token: str) -> str:
    parsed = urlparse(target_url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["_at"] = token
    return urlunparse(parsed._replace(query=urlencode(q)))


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    if is_https_request(request):
        cookie_val = (
            f"{COOKIE_NAME}={token}; Max-Age={SESSION_MAX_AGE}; Path=/; "
            f"HttpOnly; Secure; SameSite=None; Partitioned"
        )
        response.headers.append("Set-Cookie", cookie_val)
        response.headers.append(
            "Set-Cookie",
            f"pg_ck=1; Max-Age={SESSION_MAX_AGE}; Path=/; Secure; SameSite=None; Partitioned",
        )
    else:
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            max_age=SESSION_MAX_AGE,
            path="/",
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            key="pg_ck",
            value="1",
            max_age=SESSION_MAX_AGE,
            path="/",
            httponly=False,
            samesite="lax",
        )


def clear_session_cookie(response: Response, request: Request) -> None:
    if is_https_request(request):
        response.headers.append(
            "Set-Cookie",
            f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=None; Partitioned",
        )
        response.headers.append(
            "Set-Cookie",
            "pg_ck=; Max-Age=0; Path=/; Secure; SameSite=None; Partitioned",
        )
    else:
        response.delete_cookie(key=COOKIE_NAME, path="/")
        response.delete_cookie(key="pg_ck", path="/")


def build_subapp_acl_css(mod: str, perms: Set[str]) -> str:
    rules = []
    if f"{mod}.crawl" not in perms:
        rules.append('#btnSyncAll, button[data-action="sync"] { display: none !important; }')
    if f"{mod}.sources" not in perms:
        rules.append(
            '#addSiteForm, #addChannelForm, button[data-action="edit"], '
            'button[data-action="toggle"], #btnSeedDemo { display: none !important; }'
        )
    if f"{mod}.export" not in perms:
        rules.append(
            "#btnQuickExcel, #btnQuickSqlite, #btnExportFilteredExcel, #btnExportFilteredSqlite, "
            "#btnDownloadBackup { display: none !important; }"
        )
    if f"{mod}.delete" not in perms:
        rules.append(
            '[data-tab="cleanup"], #tab-cleanup, button[data-action="delete"], '
            'button[data-action="reset"], [data-post-delete], #btnDeleteSelectedPosts, '
            "#btnModalDeletePost, #btnClearLogs, #btnClearDemo { display: none !important; }"
        )
    if f"{mod}.settings" not in perms:
        rules.append('[data-tab="settings"], #tab-settings { display: none !important; }')
    if f"{mod}.export" not in perms and f"{mod}.settings" not in perms:
        rules.append('[data-tab="backup"], #tab-backup { display: none !important; }')
    return "\n".join(rules)


def build_payeshgar_topbar_html(
    mod: str,
    username: str,
    perms: Set[str],
    *,
    is_admin: bool = False,
    target_user: Optional[Dict[str, Any]] = None,
    all_users: Optional[List[Dict[str, Any]]] = None,
) -> str:
    esc_user = html_lib.escape(username)
    initial = html_lib.escape((username[:1] or "U").upper())
    viewing_other = target_user is not None

    links = [
        '<a href="/" onclick="window.payeshgarNav(\'/\');return false;" class="pg-nav-link">🏠 داشبورد پایشگر</a>'
    ]
    if "web.view" in perms:
        cls = "pg-nav-link active" if (mod == "web" and not viewing_other) else "pg-nav-link"
        links.append(f'<a href="/web/" onclick="window.payeshgarNav(\'/web/\');return false;" class="{cls}">🌐 محیط وب</a>')
    if "telegram.view" in perms:
        cls = "pg-nav-link active" if (mod == "telegram" and not viewing_other) else "pg-nav-link"
        links.append(f'<a href="/telegram/" onclick="window.payeshgarNav(\'/telegram/\');return false;" class="{cls}">✈️ محیط تلگرام</a>')
    if "x.view" in perms:
        cls = "pg-nav-link active" if (mod == "x" and not viewing_other) else "pg-nav-link"
        links.append(f'<a href="/x/" onclick="window.payeshgarNav(\'/x/\');return false;" class="{cls}">𝕏 محیط ایکس</a>')
    if is_admin:
        cls = "pg-nav-link active" if viewing_other else "pg-nav-link"
        links.append(f'<a href="/workspaces" onclick="window.payeshgarNav(\'/workspaces\');return false;" class="{cls}">👁️ محیط کاربران</a>')
    if "admin.access" in perms:
        links.append('<a href="/admin" onclick="window.payeshgarNav(\'/admin\');return false;" class="pg-nav-link">⚙️ مدیریت ACL</a>')

    inspector_banner = ""
    if is_admin and target_user is not None:
        tid = int(target_user["id"])
        tname = html_lib.escape(target_user["username"])
        options_html = ""
        for u in all_users or []:
            uid = int(u["id"])
            uname = html_lib.escape(u["username"])
            sel = "selected" if uid == tid else ""
            options_html += f'<option value="{uid}" {sel}>{uname} (#{uid})</option>'

        w_cls = "pg-ws-pill active" if mod == "web" else "pg-ws-pill"
        t_cls = "pg-ws-pill active" if mod == "telegram" else "pg-ws-pill"
        x_cls = "pg-ws-pill active" if mod == "x" else "pg-ws-pill"

        inspector_banner = f"""
<div class="pg-inspector-bar" role="region" aria-label="مدیریت محیط کاربری انتخاب‌شده">
    <div class="pg-topbar-inner">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span class="pg-inspector-badge">👁️ حالت مدیریت ادمین</span>
            <span>در حال مشاهده، خروجی‌گیری و ویرایش محیط کاربری: <strong style="color:#fde68a">{tname}</strong></span>
            <select onchange="window.payeshgarNav('/{mod}/?as_user=' + this.value)" class="pg-ws-select" title="تغییر سریع کاربر">
                {options_html}
            </select>
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            <a href="/web/?as_user={tid}" onclick="window.payeshgarNav('/web/?as_user={tid}');return false;" class="{w_cls}">🌐 وب این کاربر</a>
            <a href="/telegram/?as_user={tid}" onclick="window.payeshgarNav('/telegram/?as_user={tid}');return false;" class="{t_cls}">✈️ تلگرام این کاربر</a>
            <a href="/x/?as_user={tid}" onclick="window.payeshgarNav('/x/?as_user={tid}');return false;" class="{x_cls}">𝕏 ایکس این کاربر</a>
            <a href="/workspaces" onclick="window.payeshgarNav('/workspaces');return false;" class="pg-ws-pill back">👥 لیست همه کاربران</a>
        </div>
    </div>
</div>
"""

    return f"""
<style>
.pg-topbar {{
    background: #0f172a;
    color: #f8fafc;
    border-bottom: 1px solid #1e293b;
    font-family: Vazirmatn, Tahoma, sans-serif;
    font-size: 13px;
    direction: rtl;
    position: relative;
    z-index: 999;
}}
.pg-topbar-inner {{
    max-width: 1320px;
    margin: 0 auto;
    padding: 8px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
}}
.pg-brand {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-weight: 800;
    color: #fff;
    text-decoration: none;
}}
.pg-brand-badge {{
    background: linear-gradient(135deg, #3b82f6, #06b6d4);
    width: 26px;
    height: 26px;
    border-radius: 7px;
    display: inline-grid;
    place-items: center;
    font-size: 14px;
}}
.pg-links {{
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
}}
.pg-nav-link {{
    color: #cbd5e1;
    text-decoration: none;
    padding: 5px 11px;
    border-radius: 8px;
    transition: background 0.15s, color 0.15s;
    font-weight: 600;
}}
.pg-nav-link:hover {{
    background: #1e293b;
    color: #fff;
    text-decoration: none;
}}
.pg-nav-link.active {{
    background: #2563eb;
    color: #fff;
}}
.pg-user-area {{
    display: flex;
    align-items: center;
    gap: 10px;
}}
.pg-user-chip {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #1e293b;
    border: 1px solid #334155;
    padding: 3px 10px 3px 6px;
    border-radius: 999px;
    color: #e2e8f0;
    font-size: 12px;
}}
.pg-avatar {{
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: #3b82f6;
    color: #fff;
    display: inline-grid;
    place-items: center;
    font-size: 11px;
    font-weight: 700;
}}
.pg-logout {{
    color: #fca5a5;
    text-decoration: none;
    padding: 4px 10px;
    border-radius: 8px;
    background: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.28);
    font-size: 12px;
    font-weight: 600;
}}
.pg-logout:hover {{
    background: rgba(239, 68, 68, 0.22);
    text-decoration: none;
}}
.pg-inspector-bar {{
    background: linear-gradient(90deg, #312e81, #4c1d95);
    color: #f8fafc;
    border-bottom: 1px solid #6d28d9;
    font-family: Vazirmatn, Tahoma, sans-serif;
    font-size: 12.5px;
    direction: rtl;
    position: relative;
    z-index: 998;
}}
.pg-inspector-badge {{
    background: rgba(245, 158, 11, 0.2);
    border: 1px solid rgba(245, 158, 11, 0.55);
    color: #fde68a;
    padding: 2px 9px;
    border-radius: 999px;
    font-weight: 800;
    font-size: 11.5px;
}}
.pg-ws-select {{
    background: #1e1b4b;
    color: #fff;
    border: 1px solid #6366f1;
    border-radius: 7px;
    padding: 3px 8px;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
}}
.pg-ws-pill {{
    color: #e0e7ff;
    text-decoration: none;
    padding: 4px 10px;
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.16);
    font-weight: 700;
    font-size: 12px;
}}
.pg-ws-pill:hover {{
    background: rgba(255, 255, 255, 0.18);
    color: #fff;
    text-decoration: none;
}}
.pg-ws-pill.active {{
    background: #f59e0b;
    border-color: #f59e0b;
    color: #111827;
}}
.pg-ws-pill.back {{
    background: rgba(15, 23, 42, 0.55);
    border-color: #94a3b8;
}}
</style>
<div class="pg-topbar" role="navigation" aria-label="نوار ناوبری پایشگر">
    <div class="pg-topbar-inner">
        <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
            <a href="/" onclick="window.payeshgarNav('/');return false;" class="pg-brand">
                <span class="pg-brand-badge">📡</span>
                <span>پایشگر</span>
            </a>
            <div class="pg-links">
                {"".join(links)}
            </div>
        </div>
        <div class="pg-user-area">
            <span class="pg-user-chip"><span class="pg-avatar">{initial}</span> {esc_user}</span>
            <a href="/logout" onclick="sessionStorage.removeItem('payeshgar_at');" class="pg-logout">🚪 خروج</a>
        </div>
    </div>
</div>
{inspector_banner}
"""


def build_subapp_bridge_head(
    mod: str,
    auth_token: str,
    perms: Set[str],
    target_user_id: Optional[int] = None,
) -> str:
    prefix = f"/{mod}"
    acl_css = build_subapp_acl_css(mod, perms)
    token_json = json.dumps(auth_token)
    prefix_json = json.dumps(prefix)
    target_uid_json = json.dumps(target_user_id)
    return f"""
<script>
(function() {{
    const MODULE_PREFIX = {prefix_json};
    const SERVER_TOKEN = {token_json};
    const TARGET_USER_ID = {target_uid_json};
    if (SERVER_TOKEN) {{
        sessionStorage.setItem('payeshgar_at', SERVER_TOKEN);
    }}
    try {{
        const u = new URL(window.location.href);
        if (u.searchParams.has('_at')) {{
            sessionStorage.setItem('payeshgar_at', u.searchParams.get('_at'));
            if (document.cookie.includes('pg_ck=1')) {{
                u.searchParams.delete('_at');
                window.history.replaceState({{}}, '', u.pathname + (u.search ? u.search : '') + u.hash);
            }}
        }}
    }} catch (e) {{}}

    window.payeshgarNav = function(path) {{
        const tok = sessionStorage.getItem('payeshgar_at');
        if (tok && path.startsWith('/') && !path.startsWith('/logout')) {{
            const sep = path.includes('?') ? '&' : '?';
            window.location.href = path + sep + '_at=' + encodeURIComponent(tok);
        }} else {{
            window.location.href = path;
        }}
    }};

    // Rewrite root-relative /api/... calls to /{{mod}}/api/... and attach X-Auth-Token + X-Workspace-User
    const origFetch = window.fetch.bind(window);
    window.fetch = function(input, init) {{
        if (typeof input === 'string' && input.startsWith('/api/')) {{
            input = MODULE_PREFIX + input;
        }}
        const tok = sessionStorage.getItem('payeshgar_at');
        if (tok || TARGET_USER_ID !== null) {{
            init = Object.assign({{}}, init);
            const headers = new Headers(init.headers || {{}});
            if (tok && !headers.has('X-Auth-Token')) {{
                headers.set('X-Auth-Token', tok);
            }}
            if (TARGET_USER_ID !== null && !headers.has('X-Workspace-User')) {{
                headers.set('X-Workspace-User', String(TARGET_USER_ID));
            }}
            init.headers = headers;
        }}
        return origFetch(input, init);
    }};

    // Intercept programmatic download links (triggerDownload('/api/exports/...'))
    const origAnchorClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function() {{
        try {{
            const rawHref = this.getAttribute('href') || '';
            if (rawHref.startsWith('/api/')) {{
                let rewritten = MODULE_PREFIX + rawHref;
                if (TARGET_USER_ID !== null && !rewritten.includes('as_user=')) {{
                    rewritten += (rewritten.includes('?') ? '&' : '?') + 'as_user=' + encodeURIComponent(String(TARGET_USER_ID));
                }}
                const tok = sessionStorage.getItem('payeshgar_at');
                if (tok && !rewritten.includes('_at=')) {{
                    rewritten += (rewritten.includes('?') ? '&' : '?') + '_at=' + encodeURIComponent(tok);
                }}
                this.setAttribute('href', rewritten);
            }}
        }} catch (e) {{}}
        return origAnchorClick.apply(this, arguments);
    }};
}})();
</script>
<style>
{acl_css}
</style>
"""


def render_subapp_index(
    mod: str,
    template_file: Path,
    user: Dict[str, Any],
    perms: Set[str],
    auth_token: str,
    *,
    is_admin: bool = False,
    target_user: Optional[Dict[str, Any]] = None,
    all_users: Optional[List[Dict[str, Any]]] = None,
) -> str:
    raw_html = template_file.read_text(encoding="utf-8")
    rewritten = raw_html.replace('href="/static/', f'href="/{mod}/static/').replace('src="/static/', f'src="/{mod}/static/')
    target_uid = int(target_user["id"]) if target_user is not None else None
    bridge_head = build_subapp_bridge_head(mod, auth_token, perms, target_user_id=target_uid)
    topbar_html = build_payeshgar_topbar_html(
        mod,
        user["username"],
        perms,
        is_admin=is_admin,
        target_user=target_user,
        all_users=all_users,
    )

    rewritten = rewritten.replace("</head>", f"{bridge_head}\n</head>", 1)
    rewritten = re.sub(r"(<body[^>]*>)", rf"\1\n{topbar_html}", rewritten, count=1)
    return rewritten


def create_payeshgar_app(
    *,
    users_db_path: Optional[Path] = None,
    workspaces_root: Optional[Path] = None,
    web_db_path: Optional[Path] = None,
    telegram_db_path: Optional[Path] = None,
    x_db_path: Optional[Path] = None,
    enable_schedulers: bool = True,
) -> FastAPI:
    secret_key = os.environ.get("SECRET_KEY", "payeshgar-secret-key-2026-change-in-prod")
    auth_signer = URLSafeTimedSerializer(secret_key, salt="payeshgar-auth-v2")
    captcha_service = CaptchaService(secret_key)
    auth_db = AuthDB(db_path=users_db_path)

    # Derive isolated workspaces_root cleanly (also handles test tmp_path passed via *_db_path)
    resolved_ws_root = workspaces_root
    if resolved_ws_root is None and web_db_path is not None:
        resolved_ws_root = Path(web_db_path).resolve().parent.parent

    workspaces = WorkspaceRegistry(
        workspaces_root=resolved_ws_root,
        enable_schedulers=enable_schedulers,
    )

    # Sub-apps act as stateless FastAPI routers + static file servers; DB/SyncManager/Scheduler
    # are injected per-request from `workspaces.get_runtime(effective_user_id, mod)`.
    default_web_rt = workspaces.get_runtime(1, "web")
    default_tg_rt = workspaces.get_runtime(1, "telegram")
    default_x_rt = workspaces.get_runtime(1, "x")

    web_subapp = create_web_app(db_path=default_web_rt.db.db_path, enable_scheduler=False)
    telegram_subapp = create_telegram_app(db_path=default_tg_rt.db.db_path, enable_scheduler=False)
    x_subapp = create_x_app(db_path=default_x_rt.db.db_path, enable_scheduler=False)

    subapp_templates = {
        "web": ROOT_DIR / "modules" / "web_app" / "templates" / "index.html",
        "telegram": ROOT_DIR / "modules" / "telegram_app" / "templates" / "index.html",
        "x": ROOT_DIR / "modules" / "x_app" / "templates" / "index.html",
    }

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        existing_uids = [u["id"] for u in auth_db.list_users()]
        workspaces.start(existing_uids)
        try:
            yield
        finally:
            workspaces.stop_all()

    app = FastAPI(
        title="سامانه یکپارچه پایشگر (Payeshgar)",
        version="2.1.0",
        lifespan=lifespan,
    )
    app.state.auth_db = auth_db
    app.state.captcha = captcha_service
    app.state.auth_signer = auth_signer
    app.state.workspaces = workspaces
    app.state.web_subapp = web_subapp
    app.state.telegram_subapp = telegram_subapp
    app.state.x_subapp = x_subapp

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="payeshgar_static")

    def make_auth_token(user_id: int) -> str:
        return auth_signer.dumps({"uid": user_id})

    def verify_auth_token(token: Optional[str]) -> Optional[int]:
        if not token:
            return None
        try:
            payload = auth_signer.loads(token, max_age=SESSION_MAX_AGE)
            uid = payload.get("uid")
            return int(uid) if uid is not None else None
        except (BadSignature, SignatureExpired, Exception):
            return None

    def get_request_user(request: Request) -> tuple[Optional[Dict[str, Any]], bool]:
        cookie_tok = request.cookies.get(COOKIE_NAME)
        uid = verify_auth_token(cookie_tok)
        from_fallback = False
        if not uid:
            fallback_tok = request.headers.get("X-Auth-Token") or request.query_params.get("_at")
            uid = verify_auth_token(fallback_tok)
            if uid:
                from_fallback = True
        if not uid:
            return None, False
        user = auth_db.get_user_by_id(uid)
        return user, from_fallback

    def resolve_target_workspace_user(
        request: Request,
        caller: Dict[str, Any],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str], int]:
        """
        Determines which user's workspace this request operates on.
        - Default: caller's own workspace.
        - If `X-Workspace-User` header or `as_user` query param is specified:
          ONLY Admin (`auth_db.is_admin(caller['id'])`) may access another user's workspace!
        Returns `(target_user_dict, error_message, http_status)`.
        """
        raw_target = (
            request.headers.get("X-Workspace-User")
            or request.query_params.get("as_user")
            or ""
        ).strip()
        if not raw_target:
            return caller, None, 200

        if not raw_target.isdigit():
            return None, "شناسه کاربر هدف معتبر نیست.", 400

        target_uid = int(raw_target)
        if target_uid != int(caller["id"]) and not auth_db.is_admin(caller["id"]):
            return None, "فقط ادمین سیستم مجاز به مشاهده، خروجی‌گیری و ویرایش محیط سایر کاربران است.", 403

        if not auth_db.is_admin(caller["id"]) and target_uid == int(caller["id"]):
            return caller, None, 200

        target_user = auth_db.get_user_any_status(target_uid)
        if not target_user:
            return None, "کاربر مورد نظر یافت نشد.", 404
        return target_user, None, 200

    # ---------- ACL + Per-User Workspace Middleware for (/web/*, /telegram/*, /x/*) ----------
    @app.middleware("http")
    async def acl_subapp_guard(request: Request, call_next):
        path = request.url.path
        matched_mod = None
        sub_path = "/"
        for mod in ("web", "telegram", "x"):
            if path == f"/{mod}" or path.startswith(f"/{mod}/"):
                matched_mod = mod
                sub_path = path[len(mod) + 1 :] or "/"
                break

        if matched_mod:
            if sub_path.startswith("/static/") or sub_path == "/healthz":
                return await call_next(request)

            user, from_fallback = get_request_user(request)
            is_api = sub_path.startswith("/api/")
            if not user:
                if is_api:
                    return JSONResponse(
                        {"detail": "نشست کاربری معتبر نیست. لطفاً مجدداً وارد سامانه پایشگر شوید."},
                        status_code=401,
                    )
                return RedirectResponse(url=f"/login?next=/{matched_mod}/", status_code=302)

            perms = auth_db.get_user_permissions(user["id"])
            needed = required_permission_for_subapp(matched_mod, request.method, sub_path)
            if needed not in perms:
                desc = PERM_LABELS.get(needed, needed)
                if is_api:
                    return JSONResponse(
                        {"detail": f"شما دسترسی انجام این عملیات را ندارید: «{desc}» ({needed})"},
                        status_code=403,
                    )
                return RedirectResponse(url="/", status_code=302)

            target_user, ws_err, ws_status = resolve_target_workspace_user(request, user)
            if ws_err or not target_user:
                if is_api or sub_path.startswith("/api/"):
                    return JSONResponse({"detail": ws_err or "خطای دسترسی به محیط کاربر"}, status_code=ws_status)
                return RedirectResponse(url="/", status_code=302)

            # Bind this request to the target user's isolated workspace!
            rt = workspaces.get_runtime(target_user["id"], matched_mod)
            request.state.ws_db = rt.db
            request.state.ws_sync_manager = rt.sync_manager
            request.state.ws_scheduler = rt.scheduler
            request.state.ws_target_user = target_user

            response = await call_next(request)
            if from_fallback:
                set_session_cookie(response, request, make_auth_token(user["id"]))
            return response

        return await call_next(request)

    # ---------- Helper: Render Login ----------
    def render_login(request: Request, error: Optional[str] = None, username_val: str = "", next_val: str = "") -> Response:
        cap = captcha_service.generate()
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "captcha_svg": cap["svg"],
                "captcha_token": cap["token"],
                "error": error,
                "username_val": username_val,
                "next_val": next_val,
            },
        )

    # ---------- Gateway Routes ----------
    @app.get("/api/captcha", include_in_schema=False)
    async def api_captcha() -> Dict[str, str]:
        cap = captcha_service.generate()
        return {"svg": cap["svg"], "token": cap["token"]}

    @app.get("/", include_in_schema=False)
    async def root_page(request: Request) -> Response:
        user, from_fallback = get_request_user(request)
        if not user:
            return render_login(request)
        perms = auth_db.get_user_permissions(user["id"])
        roles = auth_db.get_user_roles(user["id"])
        is_admin = auth_db.is_admin(user["id"])
        token = make_auth_token(user["id"])
        resp = templates.TemplateResponse(
            request,
            "landing.html",
            {
                "user": user,
                "perms": perms,
                "roles": roles,
                "is_admin": is_admin,
                "auth_token": token,
            },
        )
        if from_fallback:
            set_session_cookie(resp, request, token)
        return resp

    @app.get("/login", include_in_schema=False)
    async def login_get(request: Request) -> Response:
        user, _ = get_request_user(request)
        if user:
            return RedirectResponse(url="/", status_code=302)
        next_val = request.query_params.get("next", "")
        return render_login(request, next_val=next_val)

    @app.post("/login", include_in_schema=False)
    async def login_post(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        captcha: str = Form(""),
        captcha_token: str = Form(""),
        next: str = Form(""),
    ) -> Response:
        username = username.strip()
        next_target = (next or request.query_params.get("next") or "").strip()

        if not captcha_service.verify(captcha, captcha_token):
            return render_login(
                request,
                error="کد امنیتی (کپچا) اشتباه است یا منقضی شده. لطفاً کد جدید تصویر را وارد کنید.",
                username_val=username,
                next_val=next_target,
            )

        user, err = auth_db.verify_credentials(username, password)
        if not user:
            return render_login(
                request,
                error=err or "نام کاربری یا رمز عبور اشتباه است.",
                username_val=username,
                next_val=next_target,
            )

        token = make_auth_token(user["id"])
        if not next_target or not next_target.startswith("/") or next_target.startswith("//") or next_target.startswith("/login"):
            next_target = "/"
        redirect_url = append_token_to_url(next_target, token)
        resp = RedirectResponse(url=redirect_url, status_code=302)
        set_session_cookie(resp, request, token)
        return resp

    @app.get("/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        resp = RedirectResponse(url="/login", status_code=302)
        clear_session_cookie(resp, request)
        return resp

    @app.get("/api/me", include_in_schema=False)
    async def api_me(request: Request) -> Dict[str, Any]:
        user, _ = get_request_user(request)
        if not user:
            return {"logged_in": False}
        perms = auth_db.get_user_permissions(user["id"])
        roles = auth_db.get_user_roles(user["id"])
        return {
            "logged_in": True,
            "username": user["username"],
            "is_admin": auth_db.is_admin(user["id"]),
            "roles": [r["name"] for r in roles],
            "permissions": sorted(perms),
        }

    # ---------- Admin-Only Section: User Workspaces Inspector (/workspaces) ----------
    @app.get("/workspaces", include_in_schema=False)
    async def workspaces_page(request: Request) -> Response:
        user, from_fallback = get_request_user(request)
        if not user:
            return RedirectResponse(url="/login?next=/workspaces", status_code=302)
        if not auth_db.is_admin(user["id"]):
            return RedirectResponse(url="/", status_code=302)
        perms = auth_db.get_user_permissions(user["id"])
        token = make_auth_token(user["id"])
        resp = templates.TemplateResponse(
            request,
            "workspaces.html",
            {
                "user": user,
                "perms": perms,
                "is_admin": True,
                "auth_token": token,
            },
        )
        if from_fallback:
            set_session_cookie(resp, request, token)
        return resp

    @app.get("/api/workspaces/users", include_in_schema=False)
    async def api_workspaces_users(request: Request) -> Response:
        user, _ = get_request_user(request)
        if not user:
            return JSONResponse({"error": "ابتدا وارد حساب کاربری شوید."}, status_code=401)
        if not auth_db.is_admin(user["id"]):
            return JSONResponse(
                {"error": "فقط ادمین سیستم مجاز به مشاهده و مدیریت محیط کاربری کاربران است."},
                status_code=403,
            )
        users = auth_db.list_users()
        items = []
        for u in users:
            summary = workspaces.get_user_summary(u["id"])
            items.append({**u, "workspace": summary})
        return JSONResponse(items)

    # ---------- Admin ACL Panel & APIs ----------
    @app.get("/admin", include_in_schema=False)
    async def admin_page(request: Request) -> Response:
        user, from_fallback = get_request_user(request)
        if not user:
            return RedirectResponse(url="/login?next=/admin", status_code=302)
        perms = auth_db.get_user_permissions(user["id"])
        if "admin.access" not in perms:
            return RedirectResponse(url="/", status_code=302)
        token = make_auth_token(user["id"])
        resp = templates.TemplateResponse(
            request,
            "admin.html",
            {
                "user": user,
                "perms": perms,
                "is_admin": auth_db.is_admin(user["id"]),
                "auth_token": token,
            },
        )
        if from_fallback:
            set_session_cookie(resp, request, token)
        return resp

    def _require_api_perm(request: Request, perm: str) -> tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
        user, _ = get_request_user(request)
        if not user:
            return None, JSONResponse({"error": "ابتدا وارد حساب کاربری شوید."}, status_code=401)
        perms = auth_db.get_user_permissions(user["id"])
        if perm not in perms:
            return None, JSONResponse({"error": f"شما دسترسی لازم («{perm}») را ندارید."}, status_code=403)
        return user, None

    @app.get("/api/admin/permissions", include_in_schema=False)
    async def api_admin_permissions(request: Request) -> Response:
        _, err = _require_api_perm(request, "admin.access")
        if err:
            return err
        return JSONResponse(auth_db.list_permissions())

    @app.get("/api/admin/users", include_in_schema=False)
    async def api_admin_users(request: Request) -> Response:
        _, err = _require_api_perm(request, "users.view")
        if err:
            return err
        return JSONResponse(auth_db.list_users())

    @app.post("/api/admin/users", include_in_schema=False)
    async def api_admin_create_user(request: Request) -> Response:
        _, err = _require_api_perm(request, "users.create")
        if err:
            return err
        data = await request.json()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        roles = data.get("roles") or []
        if not username or not password:
            return JSONResponse({"error": "نام کاربری و رمز عبور الزامی است."}, status_code=400)
        try:
            uid = auth_db.create_user(username, password, roles)
            return JSONResponse({"ok": True, "id": uid})
        except sqlite3.IntegrityError:
            return JSONResponse({"error": "این نام کاربری قبلاً ثبت شده است."}, status_code=400)

    @app.put("/api/admin/users/{uid}", include_in_schema=False)
    async def api_admin_update_user(uid: int, request: Request) -> Response:
        _, err = _require_api_perm(request, "users.edit")
        if err:
            return err
        data = await request.json()
        ok = auth_db.update_user(
            uid,
            password=data.get("password") or None,
            is_active=data["is_active"] if "is_active" in data else None,
            roles=data["roles"] if "roles" in data else None,
        )
        if not ok:
            return JSONResponse({"error": "کاربر یافت نشد."}, status_code=404)
        return JSONResponse({"ok": True})

    @app.delete("/api/admin/users/{uid}", include_in_schema=False)
    async def api_admin_delete_user(uid: int, request: Request) -> Response:
        me, err = _require_api_perm(request, "users.delete")
        if err:
            return err
        if me and me["id"] == uid:
            return JSONResponse({"error": "نمی‌توانید حساب کاربری خودتان را حذف کنید."}, status_code=400)
        workspaces.stop_user(uid)
        auth_db.delete_user(uid)
        return JSONResponse({"ok": True})

    @app.get("/api/admin/roles", include_in_schema=False)
    async def api_admin_roles(request: Request) -> Response:
        user, _ = get_request_user(request)
        if not user:
            return JSONResponse({"error": "ابتدا وارد شوید."}, status_code=401)
        perms = auth_db.get_user_permissions(user["id"])
        if "roles.view" not in perms and "users.view" not in perms:
            return JSONResponse({"error": "دسترسی ندارید."}, status_code=403)
        return JSONResponse(auth_db.list_roles())

    @app.post("/api/admin/roles", include_in_schema=False)
    async def api_admin_create_role(request: Request) -> Response:
        _, err = _require_api_perm(request, "roles.create")
        if err:
            return err
        data = await request.json()
        name = (data.get("name") or "").strip()
        desc = (data.get("description") or "").strip()
        permissions = data.get("permissions") or []
        if not name:
            return JSONResponse({"error": "نام نقش الزامی است."}, status_code=400)
        try:
            rid = auth_db.create_role(name, desc, permissions)
            return JSONResponse({"ok": True, "id": rid})
        except sqlite3.IntegrityError:
            return JSONResponse({"error": "این نقش قبلاً تعریف شده است."}, status_code=400)

    @app.put("/api/admin/roles/{rid}", include_in_schema=False)
    async def api_admin_update_role(rid: int, request: Request) -> Response:
        _, err = _require_api_perm(request, "roles.edit")
        if err:
            return err
        data = await request.json()
        ok = auth_db.update_role(
            rid,
            description=data["description"] if "description" in data else None,
            permissions=data["permissions"] if "permissions" in data else None,
        )
        if not ok:
            return JSONResponse({"error": "نقش یافت نشد."}, status_code=404)
        return JSONResponse({"ok": True})

    @app.delete("/api/admin/roles/{rid}", include_in_schema=False)
    async def api_admin_delete_role(rid: int, request: Request) -> Response:
        _, err = _require_api_perm(request, "roles.delete")
        if err:
            return err
        ok, msg = auth_db.delete_role(rid)
        if not ok:
            return JSONResponse({"error": msg or "خطا در حذف نقش"}, status_code=400)
        return JSONResponse({"ok": True})

    # ---------- Sub-Application Entry HTML Routes ----------
    def _serve_module_home(mod: str, request: Request) -> Response:
        user, _ = get_request_user(request)
        if not user:
            return RedirectResponse(url=f"/login?next=/{mod}/", status_code=302)
        perms = auth_db.get_user_permissions(user["id"])
        if f"{mod}.view" not in perms:
            return RedirectResponse(url="/", status_code=302)

        is_admin = auth_db.is_admin(user["id"])
        raw_as_user = (request.query_params.get("as_user") or "").strip()
        inspector_target = None
        all_users = None
        if raw_as_user:
            target_user, ws_err, _ = resolve_target_workspace_user(request, user)
            if ws_err or not target_user:
                return RedirectResponse(url="/", status_code=302)
            if is_admin:
                inspector_target = target_user
                all_users = auth_db.list_users()

        token = make_auth_token(user["id"])
        rendered = render_subapp_index(
            mod,
            subapp_templates[mod],
            user,
            perms,
            token,
            is_admin=is_admin,
            target_user=inspector_target,
            all_users=all_users,
        )
        return HTMLResponse(content=rendered, headers={"Cache-Control": "no-cache"})

    @app.get("/web", include_in_schema=False)
    async def web_slash_redirect(request: Request) -> Response:
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/web/{qs}", status_code=302)

    @app.get("/web/", include_in_schema=False)
    async def web_home(request: Request) -> Response:
        return _serve_module_home("web", request)

    @app.get("/telegram", include_in_schema=False)
    async def telegram_slash_redirect(request: Request) -> Response:
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/telegram/{qs}", status_code=302)

    @app.get("/telegram/", include_in_schema=False)
    async def telegram_home(request: Request) -> Response:
        return _serve_module_home("telegram", request)

    @app.get("/x", include_in_schema=False)
    async def x_slash_redirect(request: Request) -> Response:
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/x/{qs}", status_code=302)

    @app.get("/x/", include_in_schema=False)
    async def x_home(request: Request) -> Response:
        return _serve_module_home("x", request)

    @app.get("/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    async def healthcheck() -> Dict[str, Any]:
        return {
            "status": "ok",
            "app": "payeshgar",
            "version": "2.1.0",
            "pid": os.getpid(),
            "schedulers": {
                "web": workspaces.get_runtime(1, "web").scheduler.is_running,
                "telegram": workspaces.get_runtime(1, "telegram").scheduler.is_running,
                "x": workspaces.get_runtime(1, "x").scheduler.is_running,
            },
        }

    # ---------- Mount Full Original FastAPI Sub-Applications ----------
    app.mount("/web", web_subapp, name="web_module")
    app.mount("/telegram", telegram_subapp, name="telegram_module")
    app.mount("/x", x_subapp, name="x_module")

    return app
