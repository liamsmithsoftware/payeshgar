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


def test_per_user_workspace_isolation_and_admin_only_workspaces_inspector(tmp_path: Path):
    app = _make_test_app(tmp_path)

    with TestClient(app) as admin_client, TestClient(app) as alice_client, TestClient(app) as bob_client:
        _login(admin_client, app, "admin", "Admin@1234")

        # Create a high-privilege non-admin role (even with admin.access & users.view, NOT the 'admin' role)
        r_role = admin_client.post(
            "/api/admin/roles",
            json={
                "name": "sub_manager",
                "description": "مدیر میانی بدون نقش admin",
                "permissions": [
                    "admin.access",
                    "users.view",
                    "web.view",
                    "web.sources",
                    "web.crawl",
                    "web.export",
                    "web.settings",
                    "web.delete",
                    "telegram.view",
                    "telegram.sources",
                    "telegram.export",
                    "x.view",
                    "x.sources",
                    "x.export",
                ],
            },
        )
        assert r_role.status_code == 200

        r_alice = admin_client.post(
            "/api/admin/users",
            json={"username": "alice", "password": "Alice@1234", "roles": ["operator"]},
        )
        assert r_alice.status_code == 200
        alice_id = r_alice.json()["id"]

        r_bob = admin_client.post(
            "/api/admin/users",
            json={"username": "bob", "password": "Bob@1234", "roles": ["sub_manager"]},
        )
        assert r_bob.status_code == 200
        bob_id = r_bob.json()["id"]

        # 1. Alice logs in and adds sites/channels + seeds demo data in her own workspace
        _login(alice_client, app, "alice", "Alice@1234")
        assert alice_client.post("/web/api/sites", json={"site_input": "https://zoomit.ir", "fetch_now": False}).status_code in (200, 201)
        assert alice_client.post("/web/api/demo/seed").status_code == 200
        assert alice_client.post("/telegram/api/channels", json={"channel_input": "https://t.me/telegram", "fetch_now": False}).status_code in (200, 201)
        assert alice_client.post("/x/api/channels", json={"channel_input": "https://x.com/nasa", "fetch_now": False}).status_code in (200, 201)

        alice_web_ov = alice_client.get("/web/api/overview").json()["stats"]
        assert alice_web_ov["total_sites"] >= 2
        assert alice_web_ov["total_posts"] > 0

        # 2. Bob logs in: his workspace is 100% isolated (0 sites, 0 posts, 0 channels)
        _login(bob_client, app, "bob", "Bob@1234")
        bob_web_ov = bob_client.get("/web/api/overview").json()["stats"]
        bob_tg_ov = bob_client.get("/telegram/api/overview").json()["stats"]
        bob_x_ov = bob_client.get("/x/api/overview").json()["stats"]
        assert bob_web_ov["total_sites"] == 0
        assert bob_web_ov["total_posts"] == 0
        assert bob_tg_ov["total_channels"] == 0
        assert bob_x_ov["total_channels"] == 0

        # Bob adds his own distinct site; Alice and Admin do not see it in their own workspaces
        assert bob_client.post("/web/api/sites", json={"site_input": "https://digiato.com", "fetch_now": False}).status_code in (200, 201)
        assert admin_client.get("/web/api/overview").json()["stats"]["total_sites"] == 0

        # 3. Strict Admin-only enforcement: neither Alice (operator) nor Bob (sub_manager with admin.access!)
        # can see or access /workspaces, /api/workspaces/users, ?as_user=, or X-Workspace-User
        for non_admin in (alice_client, bob_client):
            dash_html = non_admin.get("/").text
            assert "Admin User Workspaces" not in dash_html
            assert 'href="/workspaces"' not in dash_html
            assert non_admin.get("/workspaces", follow_redirects=False).status_code == 302
            assert non_admin.get("/api/workspaces/users").status_code == 403
            assert non_admin.get(f"/web/?as_user={alice_id}" if non_admin is bob_client else "/web/?as_user=1", follow_redirects=False).status_code == 302
            assert non_admin.get("/web/api/overview", headers={"X-Workspace-User": "1"}).status_code == 403
            assert non_admin.get(f"/web/api/exports/excel?as_user={alice_id}" if non_admin is bob_client else "/web/api/exports/excel?as_user=1").status_code == 403

        # 4. Admin sees /workspaces on dashboard, inspects every user's stats, exports, and edits Alice's workspace!
        admin_dash = admin_client.get("/").text
        assert "Admin User Workspaces" in admin_dash
        assert "پایشگر" in admin_dash

        ws_page = admin_client.get("/workspaces")
        assert ws_page.status_code == 200
        assert "مدیریت محیط کاربری کاربران" in ws_page.text

        ws_users = admin_client.get("/api/workspaces/users")
        assert ws_users.status_code == 200
        by_name = {u["username"]: u for u in ws_users.json()}
        assert by_name["admin"]["workspace"]["web"]["total_sources"] == 0
        assert by_name["alice"]["workspace"]["web"]["total_sources"] == alice_web_ov["total_sites"]
        assert by_name["alice"]["workspace"]["web"]["total_posts"] == alice_web_ov["total_posts"]
        assert by_name["alice"]["workspace"]["telegram"]["total_sources"] == 1
        assert by_name["alice"]["workspace"]["x"]["total_sources"] == 1
        assert by_name["bob"]["workspace"]["web"]["total_sources"] == 1

        # Admin opens Alice's Web workspace UI (?as_user=alice_id) -> sees Admin Inspector Banner
        insp_html = admin_client.get(f"/web/?as_user={alice_id}").text
        assert "حالت مدیریت ادمین" in insp_html
        assert "alice" in insp_html
        assert f"const TARGET_USER_ID = {alice_id};" in insp_html

        # Admin exports Alice's Excel, SQLite, and JSON backup directly
        r_excel = admin_client.get(f"/web/api/exports/excel?as_user={alice_id}")
        assert r_excel.status_code == 200
        assert len(r_excel.content) > 1000

        r_sqlite = admin_client.get(f"/web/api/exports/sqlite?as_user={alice_id}")
        assert r_sqlite.status_code == 200
        assert r_sqlite.content[:15] == b"SQLite format 3"

        r_bkp = admin_client.get(f"/web/api/backup/export?as_user={alice_id}")
        assert r_bkp.status_code == 200
        bkp_data = r_bkp.json()
        alice_domains = [s.get("canonical_url", "") or s.get("domain", "") for s in bkp_data.get("sites", [])]
        assert any("zoomit.ir" in u for u in alice_domains)
        assert not any("digiato.com" in u for u in alice_domains)

        # Admin modifies Alice's workspace (adds a new site on Alice's behalf!)
        r_admin_edit = admin_client.post(
            "/web/api/sites",
            headers={"X-Workspace-User": str(alice_id)},
            json={"site_input": "https://isna.ir", "fetch_now": False},
        )
        assert r_admin_edit.status_code in (200, 201), r_admin_edit.text

        # Alice immediately sees https://isna.ir in her own workspace, while Admin's and Bob's own workspaces remain untouched!
        alice_sites = alice_client.get("/web/api/sites").json()["items"]
        assert any("isna.ir" in (s.get("canonical_url") or s.get("domain") or "") for s in alice_sites)
        assert admin_client.get("/web/api/overview").json()["stats"]["total_sites"] == 0
        assert bob_client.get("/web/api/overview").json()["stats"]["total_sites"] == 1

        # 5. Verify UI/UX, Logical RTL CSS, Accessibility & Footer requirements
        css_text = admin_client.get("/static/style.css").text
        assert "font-variant-numeric: tabular-nums" in css_text
        assert "prefers-reduced-motion: reduce" in css_text
        assert ":focus-visible" in css_text
        for forbidden in ("margin-left", "margin-right", "padding-left", "padding-right", "text-align: left", "text-align: right"):
            assert forbidden not in css_text

        assert 'class="skip-link"' in admin_dash
        assert '<a href="/web/" class="env-card">' in admin_dash
        assert "<bdi>" in admin_dash
        footer_match = re.search(r'<footer class="footer">\s*(.*?)\s*</footer>', admin_dash, re.S)
        assert footer_match and footer_match.group(1) == "پایشگر"


