from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from payeshgar.gateway import create_payeshgar_app


def _make_test_app(tmp_path: Path):
    users_dir = tmp_path / "users"
    ws_dir = tmp_path / "workspace"
    users_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "web").mkdir(parents=True, exist_ok=True)
    (ws_dir / "telegram").mkdir(parents=True, exist_ok=True)
    (ws_dir / "x").mkdir(parents=True, exist_ok=True)
    return create_payeshgar_app(
        users_db_path=users_dir / "users.db",
        web_db_path=ws_dir / "web" / "web.sqlite",
        telegram_db_path=ws_dir / "telegram" / "tg.sqlite",
        x_db_path=ws_dir / "x" / "x.sqlite",
        enable_schedulers=True,
    )


def _login(client: TestClient, app, username: str, password: str, use_persian_digits: bool = False):
    cap = client.get("/api/captcha").json()
    code = app.state.captcha.signer.loads(cap["token"])["a"]
    if use_persian_digits:
        code = code.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "captcha": code,
            "captcha_token": cap["token"],
        },
        follow_redirects=False,
    )


def test_login_only_first_page_and_cookieless_iframe_auth(tmp_path: Path):
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        # 1. Root page shows ONLY login page before authentication
        r = client.get("/")
        assert r.status_code == 200
        html = r.text
        assert "ورود به سامانه" in html
        assert "Website Collector" not in html
        assert "Telegram Collector" not in html
        assert "X Collector" not in html

        # Unauthenticated sub-app API returns 401
        assert client.get("/web/api/overview").status_code == 401
        assert client.get("/telegram/api/overview").status_code == 401
        assert client.get("/x/api/overview").status_code == 401

        # 2. Stateless Captcha + Persian digits + HTTPS Partitioned Cookie
        cap = client.get("/api/captcha").json()
        code = app.state.captcha.signer.loads(cap["token"])["a"]
        persian_code = code.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))

        r_login = client.post(
            "/login",
            headers={"x-forwarded-proto": "https"},
            data={
                "username": "admin",
                "password": "Admin@1234",
                "captcha": persian_code,
                "captcha_token": cap["token"],
            },
            follow_redirects=False,
        )
        assert r_login.status_code == 302
        set_cookies = r_login.headers.get_list("set-cookie")
        assert any("SameSite=None" in c and "Partitioned" in c and "Secure" in c for c in set_cookies)

        # Verify _at token works even on a fresh client with empty cookies!
        loc = r_login.headers["location"]
        assert "_at=" in loc
        tok = re.search(r"_at=([^&]+)", loc).group(1)

    with TestClient(app) as fresh_nocookie_client:
        fresh_nocookie_client.cookies.clear()
        # Static CSS & JS of all 3 environments must load with 200 OK even without any cookies or _at token!
        for mod in ("web", "telegram", "x"):
            r_css = fresh_nocookie_client.get(f"/{mod}/static/css/style.css", follow_redirects=False)
            assert r_css.status_code == 200, f"/{mod}/static/css/style.css -> {r_css.status_code}"
            assert "text/css" in r_css.headers.get("content-type", "")
            r_js = fresh_nocookie_client.get(f"/{mod}/static/js/app.js", follow_redirects=False)
            assert r_js.status_code == 200, f"/{mod}/static/js/app.js -> {r_js.status_code}"

        r_dash = fresh_nocookie_client.get(loc)
        assert r_dash.status_code == 200
        assert "Website Collector" in r_dash.text
        r_ov = fresh_nocookie_client.get("/web/api/overview", headers={"X-Auth-Token": tok})
        assert r_ov.status_code == 200


def test_full_original_features_and_domain_rules_across_all_three_modules(tmp_path: Path):
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        # Check schedulers running
        hz = client.get("/healthz").json()
        assert hz["schedulers"] == {"web": True, "telegram": True, "x": True}

        r_login = _login(client, app, "admin", "Admin@1234", use_persian_digits=True)
        assert r_login.status_code == 302

        # 1. Verify full UI & static files for /web/, /telegram/, /x/
        for mod, expected_title in (
            ("web", "آرشیو هوشمند مطالب وب‌سایت‌ها"),
            ("telegram", "آرشیو هوشمند کانال‌های تلگرام"),
            ("x", "آرشیو هوشمند حساب‌های ایکس"),
        ):
            page = client.get(f"/{mod}/")
            assert page.status_code == 200
            assert expected_title in page.text
            assert 'data-tab="posts"' in page.text
            assert 'data-tab="logs"' in page.text
            assert 'data-tab="settings"' in page.text
            assert 'data-tab="backup"' in page.text
            assert 'data-tab="cleanup"' in page.text
            assert f'href="/{mod}/static/css/style.css' in page.text
            assert f'src="/{mod}/static/js/app.js' in page.text

            assert client.get(f"/{mod}/static/css/style.css").status_code == 200
            assert client.get(f"/{mod}/static/js/app.js").status_code == 200

        # 2. Domain rules:
        # Web rejects X and Telegram URLs, accepts normal websites
        r_wx = client.post("/web/api/sites", json={"site_input": "https://x.com/elonmusk", "fetch_now": False})
        assert r_wx.status_code in (400, 422)
        r_wt = client.post("/web/api/sites", json={"site_input": "https://t.me/telegram", "fetch_now": False})
        assert r_wt.status_code in (400, 422)
        r_wok = client.post("/web/api/sites", json={"site_input": "https://zoomit.ir", "fetch_now": False})
        assert r_wok.status_code in (200, 201), r_wok.text

        # X rejects non-X URLs, accepts X URLs
        r_xw = client.post("/x/api/channels", json={"channel_input": "https://zoomit.ir", "fetch_now": False})
        assert r_xw.status_code in (400, 422)
        r_xt = client.post("/x/api/channels", json={"channel_input": "https://t.me/telegram", "fetch_now": False})
        assert r_xt.status_code in (400, 422)
        r_xok = client.post("/x/api/channels", json={"channel_input": "https://x.com/elonmusk", "fetch_now": False})
        assert r_xok.status_code in (200, 201), r_xok.text

        # Telegram rejects non-Telegram URLs, accepts Telegram URLs
        r_tw = client.post("/telegram/api/channels", json={"channel_input": "https://zoomit.ir", "fetch_now": False})
        assert r_tw.status_code in (400, 422)
        r_tx = client.post("/telegram/api/channels", json={"channel_input": "https://x.com/elonmusk", "fetch_now": False})
        assert r_tx.status_code in (400, 422)
        r_tok = client.post("/telegram/api/channels", json={"channel_input": "https://t.me/telegram", "fetch_now": False})
        assert r_tok.status_code in (200, 201), r_tok.text

        # 3. Independence of databases + full exports & demo seed
        assert client.post("/web/api/demo/seed").status_code == 200
        web_ov = client.get("/web/api/overview").json()
        tg_ov = client.get("/telegram/api/overview").json()
        x_ov = client.get("/x/api/overview").json()
        assert web_ov["stats"]["total_posts"] > 0
        assert tg_ov["stats"]["total_posts"] == 0
        assert x_ov["stats"]["total_posts"] == 0

        for mod in ("web", "telegram", "x"):
            assert client.post(f"/{mod}/api/demo/seed").status_code == 200
            r_xlsx = client.get(f"/{mod}/api/exports/excel")
            assert r_xlsx.status_code == 200
            assert len(r_xlsx.content) > 1000
            r_sql = client.get(f"/{mod}/api/exports/sqlite")
            assert r_sql.status_code == 200
            assert r_sql.content[:15] == b"SQLite format 3"
            r_bkp = client.get(f"/{mod}/api/backup/export")
            assert r_bkp.status_code == 200


def test_granular_action_level_acl(tmp_path: Path):
    app = _make_test_app(tmp_path)
    with TestClient(app) as admin_client:
        _login(admin_client, app, "admin", "Admin@1234")
        # Create role with ONLY web.view
        r_role = admin_client.post(
            "/api/admin/roles",
            json={"name": "web_readonly", "description": "فقط مشاهده وب", "permissions": ["web.view"]},
        )
        assert r_role.status_code == 200
        r_user = admin_client.post(
            "/api/admin/users",
            json={"username": "limited_op", "password": "Secret123", "roles": ["web_readonly"]},
        )
        assert r_user.status_code == 200

    with TestClient(app) as user_client:
        _login(user_client, app, "limited_op", "Secret123")
        dash = user_client.get("/").text
        assert "Website Collector" in dash
        assert "Telegram Collector" not in dash
        assert "X Collector" not in dash
        assert "Users & Roles" not in dash

        # Allowed: view Web
        assert user_client.get("/web/").status_code == 200
        assert user_client.get("/web/api/overview").status_code == 200
        assert user_client.get("/web/api/posts").status_code == 200

        # Blocked actions inside Web (403)
        assert user_client.post("/web/api/sites", json={"site_input": "https://example.org", "fetch_now": False}).status_code == 403
        assert user_client.post("/web/api/sites/sync-all").status_code == 403
        assert user_client.get("/web/api/exports/excel").status_code == 403
        assert user_client.delete("/web/api/logs").status_code == 403
        assert user_client.put("/web/api/settings", json={}).status_code == 403

        # Blocked other environments
        assert user_client.get("/telegram/", follow_redirects=False).status_code == 302
        assert user_client.get("/telegram/api/overview").status_code == 403
        assert user_client.get("/x/api/overview").status_code == 403
        assert user_client.get("/admin", follow_redirects=False).status_code == 302
