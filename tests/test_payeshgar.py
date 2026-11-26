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
        r = client.get("/")
        assert r.status_code == 200
        html = r.text
        assert "ورود به سامانه" in html
        assert "Website Collector" not in html
        assert "Telegram Collector" not in html
        assert "X Collector" not in html

        assert client.get("/web/api/overview").status_code == 401
        assert client.get("/telegram/api/overview").status_code == 401
        assert client.get("/x/api/overview").status_code == 401
        assert client.get("/api/user/requests").status_code == 401
        assert client.get("/api/user/results").status_code == 401

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

        loc = r_login.headers["location"]
        assert "_at=" in loc
        tok = re.search(r"_at=([^&]+)", loc).group(1)

    with TestClient(app) as fresh_nocookie_client:
        fresh_nocookie_client.cookies.clear()
        for mod in ("web", "telegram", "x"):
            r_css = fresh_nocookie_client.get(f"/{mod}/static/css/style.css", follow_redirects=False)
            assert r_css.status_code == 200
            assert "text/css" in r_css.headers.get("content-type", "")
            r_js = fresh_nocookie_client.get(f"/{mod}/static/js/app.js", follow_redirects=False)
            assert r_js.status_code == 200

        r_dash = fresh_nocookie_client.get(loc)
        assert r_dash.status_code == 200
        assert "Website Collector" in r_dash.text
        r_ov = fresh_nocookie_client.get("/web/api/overview", headers={"X-Auth-Token": tok})
        assert r_ov.status_code == 200


def test_admin_full_features_and_domain_rules(tmp_path: Path):
    app = _make_test_app(tmp_path)
    with TestClient(app) as client:
        hz = client.get("/healthz").json()
        assert hz["schedulers"] == {"web": True, "telegram": True, "x": True}

        r_login = _login(client, app, "admin", "Admin@1234", use_persian_digits=True)
        assert r_login.status_code == 302

        # Verify UI/UX Logical CSS & Footer = پایشگر
        admin_dash = client.get("/").text
        css_text = client.get("/static/style.css").text
        assert "font-variant-numeric: tabular-nums" in css_text
        assert "prefers-reduced-motion: reduce" in css_text
        for forbidden in ("margin-left", "margin-right", "padding-left", "padding-right", "text-align: left", "text-align: right"):
            assert forbidden not in css_text
        footer_match = re.search(r'<footer class="footer">\s*(.*?)\s*</footer>', admin_dash, re.S)
        assert footer_match and footer_match.group(1) == "پایشگر"

        # Verify Admin-only collector pages
        for mod, expected_title in (
            ("web", "آرشیو هوشمند مطالب وب‌سایت‌ها"),
            ("telegram", "آرشیو هوشمند کانال‌های تلگرام"),
            ("x", "آرشیو هوشمند حساب‌های ایکس"),
        ):
            page = client.get(f"/{mod}/")
            assert page.status_code == 200
            assert expected_title in page.text

        # Domain rules
        assert client.post("/web/api/sites", json={"site_input": "https://x.com/elonmusk", "fetch_now": False}).status_code in (400, 422)
        assert client.post("/web/api/sites", json={"site_input": "https://t.me/telegram", "fetch_now": False}).status_code in (400, 422)
        assert client.post("/web/api/sites", json={"site_input": "https://zoomit.ir", "fetch_now": False}).status_code in (200, 201)

        assert client.post("/x/api/channels", json={"channel_input": "https://zoomit.ir", "fetch_now": False}).status_code in (400, 422)
        assert client.post("/x/api/channels", json={"channel_input": "https://x.com/elonmusk", "fetch_now": False}).status_code in (200, 201)

        assert client.post("/telegram/api/channels", json={"channel_input": "https://zoomit.ir", "fetch_now": False}).status_code in (400, 422)
        assert client.post("/telegram/api/channels", json={"channel_input": "https://t.me/telegram", "fetch_now": False}).status_code in (200, 201)


def test_two_tier_separation_requests_shared_sources_and_coin_accounting(tmp_path: Path):
    app = _make_test_app(tmp_path)

    with (
        TestClient(app) as admin_c,
        TestClient(app) as u1_c,
        TestClient(app) as u2_c,
        TestClient(app) as u3_c,
    ):
        _login(admin_c, app, "admin", "Admin@1234")

        # Create 3 regular users:
        # - user1: rate = 3 news per 1 coin, initial charge = 2 coins (capacity = 6 news)
        # - user2: rate = 2 news per 1 coin, initial charge = 1 coin  (capacity = 2 news)
        # - user3: 0 coins (to verify zero-coin locking!)
        r_u1 = admin_c.post(
            "/api/admin/users",
            json={"username": "user1", "password": "Pass@123", "roles": ["user"], "initial_coins": 2, "posts_per_coin": 3},
        )
        assert r_u1.status_code == 200
        u1_id = r_u1.json()["id"]

        r_u2 = admin_c.post(
            "/api/admin/users",
            json={"username": "user2", "password": "Pass@123", "roles": ["operator"], "initial_coins": 0, "posts_per_coin": 100},
        )
        assert r_u2.status_code == 200
        u2_id = r_u2.json()["id"]

        # Configure user2's rate & coins via dedicated Admin Accounting APIs
        assert admin_c.put(f"/api/admin/accounting/users/{u2_id}/rate", json={"posts_per_coin": 2, "coins_per_batch": 1}).status_code == 200
        assert admin_c.post(f"/api/admin/accounting/users/{u2_id}/charge", json={"amount": 1, "description": "شارژ تست کاربر ۲"}).status_code == 200

        r_u3 = admin_c.post(
            "/api/admin/users",
            json={"username": "user3", "password": "Pass@123", "roles": ["user"], "initial_coins": 0, "posts_per_coin": 5},
        )
        assert r_u3.status_code == 200
        u3_id = r_u3.json()["id"]

        _login(u1_c, app, "user1", "Pass@123")
        _login(u2_c, app, "user2", "Pass@123")
        _login(u3_c, app, "user3", "Pass@123")

        # 1. Verify ALL existing Admin environments (/web, /telegram, /x, /admin, /workspaces, /admin/requests, /admin/accounting)
        # are 100% blocked for regular users (even user2 who has 'operator' role!)
        for reg_c in (u1_c, u2_c, u3_c):
            dash = reg_c.get("/").text
            assert "Website Collector" not in dash
            assert "Monitoring Requests" in dash
            assert "Results & Export" in dash
            for blocked_page in ("/web/", "/telegram/", "/x/", "/admin", "/workspaces", "/admin/requests", "/admin/accounting"):
                assert reg_c.get(blocked_page, follow_redirects=False).status_code == 302
            for blocked_api in (
                "/web/api/overview",
                "/telegram/api/overview",
                "/x/api/overview",
                "/api/admin/users",
                "/api/admin/requests",
                "/api/admin/accounting/users",
                "/api/workspaces/users",
            ):
                assert reg_c.get(blocked_api).status_code == 403

            # Regular users CAN open /requests and /results
            assert reg_c.get("/requests").status_code == 200
            assert reg_c.get("/results").status_code == 200

        # 2. Users submit overlapping & distinct monitoring requests
        # Platform domain validation on /api/user/requests
        assert u1_c.post("/api/user/requests", json={"platform": "web", "raw_input": "https://x.com/nasa"}).status_code == 400
        assert u1_c.post("/api/user/requests", json={"platform": "x", "raw_input": "https://zoomit.ir"}).status_code == 400

        # user1 requests zoomit.ir (Web) + @bbcpersian (Telegram)
        r_req_u1_web = u1_c.post("/api/user/requests", json={"platform": "web", "raw_input": "https://zoomit.ir", "title": "زومیت"})
        assert r_req_u1_web.status_code == 201
        u1_zoomit_req_id = r_req_u1_web.json()["item"]["id"]

        r_req_u1_tg = u1_c.post("/api/user/requests", json={"platform": "telegram", "raw_input": "@bbcpersian", "title": "کانال خبری"})
        assert r_req_u1_tg.status_code == 201

        # user2 ALSO requests zoomit.ir (shared!) + https://x.com/nasa (X)
        assert u2_c.post("/api/user/requests", json={"platform": "web", "raw_input": "zoomit.ir"}).status_code == 201
        assert u2_c.post("/api/user/requests", json={"platform": "x", "raw_input": "https://x.com/nasa"}).status_code == 201

        # user3 requests digiato.com (Web)
        assert u3_c.post("/api/user/requests", json={"platform": "web", "raw_input": "https://digiato.com"}).status_code == 201

        # 3. Admin inspects all requests, sees shared_requesters_count == 2 for zoomit.ir, replies & registers sources!
        assert admin_c.get("/admin/requests").status_code == 200
        assert admin_c.get("/admin/accounting").status_code == 200
        adm_reqs = admin_c.get("/api/admin/requests").json()
        assert adm_reqs["summary"]["total"] == 5
        zoomit_entries = [x for x in adm_reqs["items"] if "zoomit.ir" in x["canonical_url"]]
        assert len(zoomit_entries) == 2
        assert all(x["shared_requesters_count"] == 2 for x in zoomit_entries)

        # Admin replies to user1's zoomit request AND uses 1-click add_to_collector=True
        r_reply = admin_c.put(
            f"/api/admin/requests/{u1_zoomit_req_id}",
            json={
                "status": "approved",
                "admin_reply": "به فهرست پایش اضافه گردید.",
                "add_to_collector": True,
            },
        )
        assert r_reply.status_code == 200
        assert r_reply.json()["collector"]["platform"] == "web"

        # User1 sees Admin's reply on /api/user/requests
        u1_reqs = u1_c.get("/api/user/requests").json()["items"]
        z_item = next(x for x in u1_reqs if x["id"] == u1_zoomit_req_id)
        assert z_item["status"] == "approved"
        assert z_item["admin_reply"] == "به فهرست پایش اضافه گردید."
        assert z_item["in_collector"] is True

        # Admin also adds digiato.com, @bbcpersian, and x.com/nasa in Admin's central collectors (/web, /telegram, /x)
        assert admin_c.post("/web/api/sites", json={"site_input": "https://digiato.com", "fetch_now": False}).status_code in (200, 201)
        assert admin_c.post("/telegram/api/channels", json={"channel_input": "https://t.me/bbcpersian", "fetch_now": False}).status_code in (200, 201)
        assert admin_c.post("/x/api/channels", json={"channel_input": "https://x.com/nasa", "fetch_now": False}).status_code in (200, 201)

        # Seed realistic collected posts into Admin's central DBs (uid=1)
        web_db = app.state.workspaces.get_runtime(1, "web").db
        tg_db = app.state.workspaces.get_runtime(1, "telegram").db
        x_db = app.state.workspaces.get_runtime(1, "x").db

        sites_by_dom = {s["domain"].replace("www.", ""): s for s in web_db.list_sites()}
        zoomit_site = sites_by_dom["zoomit.ir"]
        digiato_site = sites_by_dom["digiato.com"]

        with web_db.transaction() as conn:
            for i in range(1, 6):  # 5 posts for zoomit.ir
                conn.execute(
                    """
                    INSERT INTO posts
                    (site_id, site_domain, site_title, item_seq, post_guid, post_url, title, published_date, published_jalali_date, published_time, published_at_utc, content, media_type, fetched_at)
                    VALUES (?, 'zoomit.ir', 'زومیت', ?, ?, ?, ?, '2026-11-22', '1405/09/01', '12:00:00', ?, ?, 'text', '2026-11-22T12:00:00Z')
                    """,
                    (
                        zoomit_site["id"],
                        i,
                        f"zg-{i}",
                        f"https://zoomit.ir/post-{i}",
                        f"خبر زومیت شماره {i}",
                        f"2026-11-22T12:0{i}:00Z",
                        f"متن کامل خبر فناوری زومیت شماره {i}",
                    ),
                )
            for i in range(1, 4):  # 3 posts for digiato.com
                conn.execute(
                    """
                    INSERT INTO posts
                    (site_id, site_domain, site_title, item_seq, post_guid, post_url, title, published_date, published_jalali_date, published_time, published_at_utc, content, media_type, fetched_at)
                    VALUES (?, 'digiato.com', 'دیجیاتو', ?, ?, ?, ?, '2026-11-22', '1405/09/01', '11:00:00', ?, ?, 'text', '2026-11-22T11:00:00Z')
                    """,
                    (
                        digiato_site["id"],
                        i,
                        f"dg-{i}",
                        f"https://digiato.com/post-{i}",
                        f"خبر دیجیاتو شماره {i}",
                        f"2026-11-22T11:0{i}:00Z",
                        f"متن خبر اختصاصی دیجیاتو شماره {i}",
                    ),
                )

        tg_ch = tg_db.list_channels()[0]
        with tg_db.transaction() as conn:
            for i in range(1, 3):  # 2 posts for @bbcpersian
                conn.execute(
                    """
                    INSERT INTO posts
                    (channel_id, channel_username, channel_title, message_id, post_url, published_date, published_jalali_date, published_time, published_at_utc, content, media_type, fetched_at)
                    VALUES (?, 'bbcpersian', 'کانال خبری', ?, ?, '2026-11-22', '1405/09/01', '13:00:00', ?, ?, 'text', '2026-11-22T13:00:00Z')
                    """,
                    (
                        tg_ch["id"],
                        100 + i,
                        f"https://t.me/bbcpersian/{100 + i}",
                        f"2026-11-22T13:0{i}:00Z",
                        f"گزارش تلگرامی شماره {i}",
                    ),
                )

        x_ch = x_db.list_channels()[0]
        with x_db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO posts
                (channel_id, channel_username, channel_title, message_id, post_url, published_date, published_jalali_date, published_time, published_at_utc, content, media_type, fetched_at)
                VALUES (?, 'nasa', '@nasa', 501, 'https://x.com/nasa/status/501', '2026-11-22', '1405/09/01', '14:00:00', '2026-11-22T14:00:00Z', 'توییت فضایی ناسا درباره مریخ', 'text', '2026-11-22T14:00:00Z')
                """,
                (x_ch["id"],),
            )

        # 4. Verify Coin Accounting & Source Isolation!
        # user3 has 0 coins -> sees 0 items and locked_new_count == 3 (digiato posts)
        res_u3 = u3_c.get("/api/user/results").json()
        assert res_u3["items"] == []
        assert res_u3["locked_new_count"] == 3
        assert res_u3["insufficient_coins"] is True

        # user1 has 2 coins, rate = 3 news per 1 coin (capacity = 6 news out of 5 zoomit + 2 tg = 7 total!)
        # Fetch page 1 with page_size=3 -> unlocks 3 items -> deducts EXACTLY 1 coin!
        res_u1_p1 = u1_c.get("/api/user/results?page=1&page_size=3").json()
        assert len(res_u1_p1["items"]) == 3
        assert res_u1_p1["coins_deducted_now"] == 1
        assert res_u1_p1["wallet"]["coin_balance"] == 1
        assert res_u1_p1["wallet"]["remaining_posts_allowance"] == 3
        assert res_u1_p1["locked_new_count"] == 1  # 7 candidate posts - 6 capacity = 1 locked!
        # Strictly isolated: NEVER contains digiato or nasa!
        for it in res_u1_p1["items"]:
            assert it["source_handle"] in ("zoomit.ir", "bbcpersian")

        # Refreshing the EXACT same page 1 deducts 0 extra coins!
        res_u1_refresh = u1_c.get("/api/user/results?page=1&page_size=3").json()
        assert res_u1_refresh["coins_deducted_now"] == 0
        assert res_u1_refresh["wallet"]["coin_balance"] == 1

        # Fetching page 2 (next 3 unseen items) deducts the 2nd coin -> balance becomes 0!
        res_u1_p2 = u1_c.get("/api/user/results?page=2&page_size=3").json()
        assert len(res_u1_p2["items"]) == 3
        assert res_u1_p2["coins_deducted_now"] == 1
        assert res_u1_p2["wallet"]["coin_balance"] == 0
        assert res_u1_p2["wallet"]["remaining_posts_allowance"] == 0

        # Filtering & Excel export of already-unlocked items works even at 0 coins without charging again!
        res_u1_filter = u1_c.get("/api/user/results?platform=telegram").json()
        assert res_u1_filter["total"] == 2
        assert all(it["platform"] == "telegram" for it in res_u1_filter["items"])

        r_xlsx = u1_c.get("/api/user/results/export.xlsx")
        assert r_xlsx.status_code == 200
        assert len(r_xlsx.content) > 1000

        # user2 has 1 coin, rate = 2 news per 1 coin (capacity = 2 news out of 5 zoomit + 1 nasa = 6 total)
        res_u2 = u2_c.get("/api/user/results?page=1&page_size=10").json()
        assert len(res_u2["items"]) == 2
        assert res_u2["coins_deducted_now"] == 1
        assert res_u2["wallet"]["coin_balance"] == 0
        assert res_u2["locked_new_count"] == 4
        # First newest item for user2 is nasa ('2026-11-22T14:00:00Z'), second is zoomit-5!
        handles_u2 = {it["source_handle"] for it in res_u2["items"]}
        assert handles_u2 == {"nasa", "zoomit.ir"}

        # Admin checks accounting ledger -> sees auto_deduct entries for user1 and user2
        txs = admin_c.get("/api/admin/accounting/transactions").json()
        auto_txs = [t for t in txs if t["tx_type"] == "auto_deduct"]
        assert len(auto_txs) >= 3
