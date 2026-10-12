"""Staying logged in when the server restarts.

The login used to live in the server's memory (token -> name).  Render restarts the
service on every deploy and whenever the free instance wakes from sleep, so everyone
was silently logged out; a teacher who had the document page open pressed save and got
"กรุณาเข้าสู่ระบบก่อน".  Now the cookie carries the name and a signature, and the
server only needs a key it can work out again after a restart.  Offline throughout.
"""
from __future__ import annotations

import base64
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import core

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    webmain = None

SECRET_NAMES = ("SARABAN_SESSION_SECRET", "SARABAN_SA_JSON", "SARABAN_DRIVE_OAUTH",
                "SARABAN_GEMINI_API_KEY", "SARABAN_LINE_ACCESS_TOKEN")


def token_at(name, issued, salt, nonce="n0"):
    who = base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")
    return f"{who}.{issued}.{nonce}.{webmain._session_sig(name, issued, nonce, salt)}"


@unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class SessionKeyTests(unittest.TestCase):
    def without_secrets(self):
        p = patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        for n in SECRET_NAMES:
            os.environ.pop(n, None)

    def test_same_key_after_a_restart_from_a_secret_already_set_on_the_host(self):
        self.without_secrets()
        os.environ["SARABAN_SA_JSON"] = '{"private_key": "-----BEGIN PRIVATE KEY-----abc"}'
        first = webmain._session_key()
        self.assertEqual(webmain._session_key(), first)
        self.assertNotIn(b"abc", first)                    # not the secret itself
        os.environ["SARABAN_SESSION_SECRET"] = "set-on-purpose"
        self.assertNotEqual(webmain._session_key(), first)  # an explicit secret wins

    def test_phone_token_is_never_the_key(self):
        """the phone token is copied onto phones; holding it must not let anyone sign a login"""
        self.without_secrets()
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        with patch.object(core, "WORK_DIR", work):
            plain = webmain._session_key()
            os.environ["SARABAN_PHONE_TOKEN"] = "phone-secret"
            self.assertEqual(webmain._session_key(), plain)

    def test_school_computer_keeps_its_key_in_a_file(self):
        self.without_secrets()
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        with patch.object(core, "WORK_DIR", work):
            first = webmain._session_key()
            self.assertEqual(webmain._session_key(), first)
            path = os.path.join(work, "session.key")
            self.assertTrue(os.path.isfile(path))
            if os.name == "posix":
                self.assertEqual(os.stat(path).st_mode & 0o077, 0)   # only the server can read it

    def test_cookie_from_before_the_restart_still_works(self):
        self.without_secrets()
        os.environ["SARABAN_SA_JSON"] = "{}x"
        users = {"ครูเอ": {"salt": "s", "status": "approved"}}
        with patch.object(webmain, "_SESSION_KEY", webmain._session_key()):
            token = webmain.make_session("ครูเอ", users["ครูเอ"])
        with patch.object(webmain, "_SESSION_KEY", webmain._session_key()):   # new process
            self.assertEqual(webmain.session_user(token, users), "ครูเอ")


@unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class SessionCookieTests(unittest.TestCase):
    def setUp(self):
        self.users = {"ครูเอ": {"salt": "s1", "status": "approved", "role": "user"},
                      "bee": {"salt": "s2", "status": "approved", "role": "user"}}
        p = patch.object(webmain, "load_users", side_effect=lambda raise_on_error=False: self.users)
        p.start()
        self.addCleanup(p.stop)
        self.token = webmain.make_session("ครูเอ", self.users["ครูเอ"])

    def client(self, token=None):
        return TestClient(webmain.app, cookies={"session": token or self.token},
                          follow_redirects=False)

    def test_forged_or_damaged_cookies_are_refused(self):
        who, issued, nonce, sig = self.token.split(".")
        bee = base64.urlsafe_b64encode("bee".encode()).decode().rstrip("=")
        for bad in (f"{bee}.{issued}.{nonce}.{sig}",                # someone else's name
                    f"{who}.{int(issued) + 1}.{nonce}.{sig}",         # pushed the time forward
                    f"{who}.{issued}.x{nonce}.{sig}",
                    f"{who}.{issued}.{nonce}.{sig[:-2]}AA",
                    f"{who}.{issued}.{nonce}.{sig}ก",
                    "", None, "abc", "a.b", "a.b.c.d", "ก.ข.ค.ง", "a.b.c.d.e",
                    f"{who}.x.{nonce}.{sig}", "!!!.1.2.3"):
            self.assertIsNone(webmain.session_user(bad, self.users), bad)
        with patch.object(webmain, "_SESSION_KEY", b"another server" * 3):
            self.assertIsNone(webmain.session_user(self.token, self.users))

    def test_cookie_lasts_twelve_hours(self):
        now = int(time.time())
        self.assertEqual(webmain.session_user(token_at("ครูเอ", now - 11 * 3600, "s1"), self.users), "ครูเอ")
        self.assertIsNone(webmain.session_user(token_at("ครูเอ", now - 13 * 3600, "s1"), self.users))

    def test_pages_and_api_accept_the_cookie_and_send_strangers_to_login(self):
        self.assertEqual(self.client().get("/doc").status_code, 200)
        self.assertEqual(self.client().get("/api/me").json()["user"], "ครูเอ")
        stranger = TestClient(webmain.app, follow_redirects=False)
        self.assertEqual(stranger.get("/doc").headers["location"], "/login")
        self.assertEqual(stranger.get("/api/me").status_code, 401)

    def test_login_hands_out_a_cookie_that_works(self):
        self.users["ครูซี"] = webmain._make_user("ครูซี", "secret-pass", "", "user", "approved")
        c = TestClient(webmain.app, follow_redirects=False)
        r = c.post("/login", data={"username": "ครูซี", "password": "secret-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client(r.cookies["session"]).get("/api/me").json()["user"], "ครูซี")

    def test_logout_makes_that_cookie_useless_even_if_someone_kept_a_copy(self):
        phone = token_at("ครูเอ", int(time.time()) - 10, "s1")     # same teacher, on the phone
        self.client().post("/logout")
        self.assertEqual(self.client().get("/api/me").status_code, 401)
        self.assertEqual(self.client(phone).get("/api/me").status_code, 200)

    def test_changing_the_password_logs_out_every_other_cookie(self):
        self.users["ครูเอ"] = webmain._make_user("ครูเอ", "old-pass-1", "", "user", "approved")
        old = webmain.make_session("ครูเอ", self.users["ครูเอ"])
        with patch.object(webmain, "save_users"):
            r = self.client(old).post("/api/me/password", json={"old": "old-pass-1", "new": "new-pass-2"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client(r.cookies["session"]).get("/api/me").status_code, 200)
        self.assertEqual(self.client(old).get("/api/me").status_code, 401)

    def test_suspended_or_deleted_accounts_are_shut_out_at_once(self):
        self.users["ครูเอ"]["status"] = "rejected"
        self.assertEqual(self.client().get("/api/me").status_code, 403)
        self.assertEqual(self.client().get("/doc").headers["location"], "/login")
        del self.users["ครูเอ"]
        self.assertEqual(self.client().get("/api/me").status_code, 401)

    def test_sheets_hiccup_is_not_reported_as_being_logged_out(self):
        def down(raise_on_error=False):
            if raise_on_error:
                raise webmain.StoreDown("timeout")
            return {}

        with patch.object(webmain, "load_users", side_effect=down):
            r = self.client().get("/api/me")
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("เข้าสู่ระบบ", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
