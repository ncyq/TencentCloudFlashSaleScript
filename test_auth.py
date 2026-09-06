"""Cookie/配置测试完全离线，浏览器由 Mock 替代。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import snap_up_server as app


def cookies_fixture():
    return [{"name": "skey", "value": "fake-session", "domain": ".cloud.tencent.com",
             "path": "/", "secure": True, "expires": -1}]


def credentials_fixture():
    return {"csrf_token": "config-token", "user_agent": "test-browser",
            "activity_url": "https://cloud.tencent.com/act/pro/test",
            "cookies": cookies_fixture()}


class AuthTests(unittest.TestCase):
    def test_session_uses_config_csrf_and_cookie_file_cookie(self):
        with app.create_session(credentials_fixture()) as client:
            self.assertEqual(client.headers["x-csrf-token"], "config-token")
            self.assertEqual(client.headers["User-Agent"], "test-browser")
            self.assertEqual(client.headers["referer"], credentials_fixture()["activity_url"])
            self.assertEqual(client.cookies.get("skey"), "fake-session")
            self.assertTrue(next(iter(client.cookies)).secure)
            self.assertEqual(client.adapters["https://"].max_retries.total, 0)

    def test_load_config_reads_csrf_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "csrf_token": "manually-confirmed-token",
                "activity_url": "https://cloud.tencent.com/act/pro/test",
            }), encoding="utf-8")
            loaded = app.load_config(path)
            self.assertEqual(loaded["csrf_token"], "manually-confirmed-token")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["csrf_token"],
                             "manually-confirmed-token")
            uppercase = {"CSRF_TOKEN": "uppercase-token",
                         "activity_url": "https://cloud.tencent.com/act/pro/test"}
            path.write_text(json.dumps(uppercase), encoding="utf-8")
            self.assertEqual(app.load_config(path)["csrf_token"], "uppercase-token")

    def test_invalid_config_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for config in ({}, {"csrf_token": ""},
                           {"csrf_token": "x", "activity_url": "https://example.com/act/x"}):
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(config=config), self.assertRaises(ValueError):
                    app.load_config(path)

    def test_cookie_file_validation(self):
        self.assertEqual(app.validate_cookies(cookies_fixture()), cookies_fixture())
        for cookies in ([], [{}], [{"name": "skey", "value": ""}],
                        [{"name": "other", "value": "x"}]):
            with self.subTest(cookies=cookies), self.assertRaises(ValueError):
                app.validate_cookies(cookies)

    def test_login_url_keeps_activity_return_target(self):
        login_url = app.build_login_url(app.ACTIVITY_URL)
        self.assertTrue(login_url.startswith(app.LOGIN_URL + "?s_url="))
        self.assertIn("featured-202607", login_url)
        self.assertNotIn("?s_url=https://", login_url)

    def test_browser_login_ignores_login_zero_and_saves_post_login_csrf_separately(self):
        runtime = MagicMock()
        browser = runtime.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        context.cookies.return_value = cookies_fixture()
        page.url = "https://cloud.tencent.com/act/pro/test"
        callbacks = {}
        context.on.side_effect = lambda event, callback: callbacks.update({event: callback})
        request_count = 0

        def navigate(*args, **kwargs):
            nonlocal request_count
            request_count += 1
            request = MagicMock()
            request.url = app.CHECK_URL
            request.all_headers.return_value = {
                "x-csrf-token": "0" if request_count == 1 else "48508734"
            }
            callbacks["request"](request)

        page.goto.side_effect = navigate
        page.reload.side_effect = navigate
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "sync_playwright") as playwright, patch.object(app, "CONFIG_FILE"), patch.object(app, "COOKIE_FILE"):
            playwright.return_value.start.return_value = runtime
            path = Path(directory) / "cookies.json"
            config_path = Path(directory) / "config.json"
            app.CONFIG_FILE = config_path
            app.COOKIE_FILE = path
            config_path.write_text(json.dumps({"csrf_token": "old-token"}), encoding="utf-8")
            session = app.start_browser_credentials(app.ACTIVITY_URL)
            saved = session["credentials"]["cookies"]
            self.assertEqual(session["credentials"]["csrf_token"], "48508734")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), cookies_fixture())
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["csrf_token"], "48508734")
            self.assertNotEqual(config["csrf_token"], "0")
            session["browser"].close()
            session["playwright"].stop()
        context.cookies.assert_called_once_with()
        pattern, handler = context.route.call_args.args
        self.assertIn("/do-goods", pattern)
        route = MagicMock()
        handler(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
        browser.close.assert_called_once()
        self.assertEqual(browser.new_context.call_args.args, ())
        self.assertEqual(browser.new_context.call_args.kwargs, {})


if __name__ == "__main__":
    unittest.main()
