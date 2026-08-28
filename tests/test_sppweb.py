"""Offline contract tests for the Sakon Area 1 HTTP client.

These tests deliberately use fake sessions and responses.  They must never
contact the real SPP website or require working login credentials.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import defaultdict
from unittest.mock import patch

import requests
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

import sppweb


LOGIN_HTML = """
<!doctype html>
<html><body>
  <form action="/index.php" method="post">
    <input type="hidden" name="user_os" value="windows">
    <input type="text" name="username" value="">
    <input type="password" name="pass" value="">
    <button type="submit" name="login_submit" value="login">เข้าสู่ระบบ</button>
  </form>
</body></html>
"""

CLOUDFLARE_HTML = """
<!doctype html>
<html><head><title>Just a moment...</title></head>
<body>Checking your browser before accessing the site. cloudflare
<div id="cf-browser-verification"></div></body></html>
"""

EMPTY_LIST_HTML = """
<!doctype html><html><body>
  <table id="book-list">
    <thead><tr><th>ID</th><th>เลขหนังสือ</th><th>เรื่อง</th><th>รายละเอียด</th></tr></thead>
    <tbody></tbody>
  </table>
  <div class="empty">ไม่พบข้อมูล</div>
</body></html>
"""


def list_html(book_id: str = "123", title: str = "หนังสือทดสอบ") -> str:
    return f"""
    <!doctype html><html><body>
      <table id="book-list"><tbody><tr>
        <td>{book_id}</td>
        <td>ศธ 04001/99</td>
        <td>{title}</td>
        <td><a href="#" onclick="bookdetail_school_total.php?b_id={book_id}">รายละเอียด</a></td>
        <td>27 สิงหาคม 2569</td>
        <td>กลุ่มอำนวยการ [ระบบ]</td>
        <td>27 สค 2569 09:19:09 น.</td>
      </tr></tbody></table>
    </body></html>
    """


class FakeResponse:
    """Small requests.Response stand-in used by FakeSession."""

    def __init__(
        self,
        status_code: int = 200,
        *,
        text: str | None = "",
        content: bytes | None = None,
        headers: dict | None = None,
        url: str = "",
        reason: str = "",
    ):
        self.status_code = status_code
        self.url = url
        self.reason = reason or ("OK" if 200 <= status_code < 300 else "Error")
        self.headers = CaseInsensitiveDict(headers or {})
        self.encoding = "utf-8"
        if content is None:
            content = (text or "").encode("utf-8")
        self.content = content
        self.text = text if text is not None else content.decode("utf-8", errors="replace")
        self.history = []
        self.closed = False

    @property
    def ok(self):
        return self.status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} {self.reason}", response=self
            )

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class FakeSession:
    """Route-based requests.Session stand-in with call recording."""

    def __init__(self):
        self.headers = CaseInsensitiveDict()
        self.cookies = RequestsCookieJar()
        self.calls = []
        self._routes = defaultdict(list)

    def add(self, method: str, url: str, *responses):
        self._routes[(method.upper(), url)].extend(responses)
        return self

    def request(self, method: str, url: str, **kwargs):
        method = method.upper()
        self.calls.append({"method": method, "url": url, **kwargs})
        queued = self._routes.get((method, url))

        # Attachment discovery may use an authenticated HEAD probe.  It is
        # intentionally accepted for any URL; tests still inspect the resolved
        # URL returned by fetch_detail.
        if not queued and method == "HEAD":
            return FakeResponse(
                200,
                content=b"",
                headers={"Content-Type": "application/pdf"},
                url=url,
            )
        if not queued:
            raise AssertionError(f"Unexpected offline HTTP call: {method} {url}")

        # A single queued response is reusable.  This keeps pagination tests
        # independent of harmless implementation-level refetches.
        item = queued[0] if len(queued) == 1 else queued.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(method, url, kwargs)
        if not item.url:
            item.url = url
        return item

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def head(self, url, **kwargs):
        return self.request("HEAD", url, **kwargs)

    def close(self):
        return None


class SessionStateTests(unittest.TestCase):
    def test_new_session_accepts_legacy_phpsessid_string(self):
        sess = sppweb.new_session("legacy-session-id")
        self.assertEqual(sess.cookies.get("PHPSESSID"), "legacy-session-id")

    def test_new_session_accepts_cookie_header_string(self):
        sess = sppweb.new_session(
            "PHPSESSID=session-1; cf_clearance=clearance-1; __cf_bm=bot-cookie"
        )
        cookies = sess.cookies.get_dict()
        self.assertEqual(cookies["PHPSESSID"], "session-1")
        self.assertEqual(cookies["cf_clearance"], "clearance-1")
        self.assertEqual(cookies["__cf_bm"], "bot-cookie")

    def test_exported_state_is_json_safe_and_round_trips_cookies_and_user_agent(self):
        original = sppweb.new_session(
            "PHPSESSID=session-2; cf_clearance=clearance-2"
        )
        original.headers["User-Agent"] = "School-Desktop-Browser/1.0"
        original.cookies.set(
            "scoped-cookie",
            "scoped-value",
            domain="office.sakonarea1.go.th",
            path="/modules/book",
        )

        state = sppweb.export_session(original)
        json.dumps(state)
        restored = sppweb.new_session(state)

        self.assertEqual(restored.headers["User-Agent"], "School-Desktop-Browser/1.0")
        values = {cookie.name: cookie.value for cookie in restored.cookies}
        self.assertEqual(values["PHPSESSID"], "session-2")
        self.assertEqual(values["cf_clearance"], "clearance-2")
        self.assertEqual(values["scoped-cookie"], "scoped-value")
        scoped = next(c for c in restored.cookies if c.name == "scoped-cookie")
        self.assertEqual(scoped.domain, "office.sakonarea1.go.th")
        self.assertEqual(scoped.path, "/modules/book")


class ValidationTests(unittest.TestCase):
    def test_is_logged_in_returns_false_for_real_login_page(self):
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(text=LOGIN_HTML)
        )
        self.assertFalse(sppweb.is_logged_in(sess))

    def test_is_logged_in_raises_access_blocked_for_http_403(self):
        sess = FakeSession().add(
            "GET",
            sppweb.NEWS_URL,
            FakeResponse(403, text=CLOUDFLARE_HTML),
        )
        with self.assertRaises(sppweb.AccessBlockedError):
            sppweb.is_logged_in(sess)

    def test_is_logged_in_raises_access_blocked_for_200_challenge(self):
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(200, text=CLOUDFLARE_HTML)
        )
        with self.assertRaises(sppweb.AccessBlockedError):
            sppweb.is_logged_in(sess)

    def test_is_logged_in_rejects_unrelated_success_page(self):
        sess = FakeSession().add(
            "GET",
            sppweb.NEWS_URL,
            FakeResponse(200, text="<html><body>maintenance</body></html>"),
        )
        with self.assertRaises(sppweb.UnexpectedPageError):
            sppweb.is_logged_in(sess)

    def test_is_logged_in_accepts_recognizable_empty_news_table(self):
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(200, text=EMPTY_LIST_HTML)
        )
        self.assertTrue(sppweb.is_logged_in(sess))


class LoginTests(unittest.TestCase):
    def test_login_posts_hidden_fields_and_submit_button(self):
        sess = FakeSession()
        sess.add("GET", sppweb.BASE, FakeResponse(text=LOGIN_HTML))
        sess.add(
            "POST",
            sppweb.BASE + "index.php",
            FakeResponse(text=list_html(), url=sppweb.NEWS_URL),
        )
        sess.add("GET", sppweb.NEWS_URL, FakeResponse(text=list_html()))

        with patch.object(sppweb, "new_session", return_value=sess):
            result = sppweb.login("school-user", "school-password")

        self.assertIs(result, sess)
        posted = next(c for c in sess.calls if c["method"] == "POST")
        self.assertEqual(posted["data"]["username"], "school-user")
        self.assertEqual(posted["data"]["pass"], "school-password")
        self.assertEqual(posted["data"]["user_os"], "windows")
        self.assertIn("login_submit", posted["data"])

    def test_login_surfaces_cloudflare_instead_of_reporting_bad_password(self):
        sess = FakeSession().add(
            "GET", sppweb.BASE, FakeResponse(403, text=CLOUDFLARE_HTML)
        )
        with patch.object(sppweb, "new_session", return_value=sess):
            with self.assertRaises(sppweb.AccessBlockedError):
                sppweb.login("school-user", "school-password")

    def test_login_page_after_submit_is_a_login_error(self):
        sess = FakeSession()
        sess.add("GET", sppweb.BASE, FakeResponse(text=LOGIN_HTML))
        sess.add(
            "POST",
            sppweb.BASE + "index.php",
            FakeResponse(text=LOGIN_HTML, url=sppweb.BASE),
        )
        sess.add("GET", sppweb.NEWS_URL, FakeResponse(text=LOGIN_HTML))

        with patch.object(sppweb, "new_session", return_value=sess):
            with self.assertRaises(sppweb.LoginError):
                sppweb.login("school-user", "wrong-password")


class ListTests(unittest.TestCase):
    def test_list_documents_parses_valid_page_and_keeps_newest_first(self):
        first = list_html("123", "เรื่องเก่า")
        second = list_html("124", "เรื่องใหม่")
        landing = first.replace(
            "</body>", f'<a href="{sppweb.NEWS_URL}&page=2">2</a></body>'
        )
        sess = FakeSession()
        sess.add("GET", sppweb.NEWS_URL, FakeResponse(text=landing))
        sess.add(
            "GET", f"{sppweb.NEWS_URL}&page=3", FakeResponse(text=EMPTY_LIST_HTML)
        )
        sess.add("GET", f"{sppweb.NEWS_URL}&page=1", FakeResponse(text=first))
        sess.add("GET", f"{sppweb.NEWS_URL}&page=2", FakeResponse(text=second))

        docs = sppweb.list_documents(sess, pages=2)

        self.assertEqual([d["book_id"] for d in docs], ["124", "123"])
        self.assertEqual(docs[0]["doc_title"], "เรื่องใหม่")
        self.assertEqual(docs[0]["sent_key"], "2026-08-27")

    def test_list_documents_raises_for_403_instead_of_returning_empty(self):
        sess = FakeSession().add(
            "GET",
            sppweb.NEWS_URL,
            FakeResponse(403, text=CLOUDFLARE_HTML),
        )
        with self.assertRaises(sppweb.AccessBlockedError):
            sppweb.list_documents(sess, pages=1)

    def test_list_documents_raises_session_expired_for_login_html(self):
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(200, text=LOGIN_HTML)
        )
        with self.assertRaises(sppweb.SessionExpiredError):
            sppweb.list_documents(sess, pages=1)

    def test_list_documents_rejects_unrelated_200_page(self):
        sess = FakeSession().add(
            "GET",
            sppweb.NEWS_URL,
            FakeResponse(200, text="<html><body>temporarily unavailable</body></html>"),
        )
        with self.assertRaises(sppweb.UnexpectedPageError):
            sppweb.list_documents(sess, pages=1)


class DetailTests(unittest.TestCase):
    def test_fetch_detail_accepts_pdf_url_with_query_string(self):
        detail_url = sppweb.DETAIL_URL.format("123")
        html = """
        <!doctype html><html><body>
          <h2>รายละเอียดหนังสือ</h2>
          <a href="/files/main.PDF?token=abc123&amp;download=1">1. หนังสือหลัก</a>
          <a href="/files/appendix.docx?token=def456">2. เอกสารแนบ</a>
        </body></html>
        """
        main_url = sppweb.BASE + "files/main.PDF?token=abc123&download=1"
        appendix_url = sppweb.BASE + "files/appendix.docx?token=def456"
        sess = FakeSession().add("GET", detail_url, FakeResponse(text=html))
        # The production implementation may validate candidate attachments
        # with an authenticated streaming GET.  Route those probes offline too.
        sess.add(
            "GET",
            main_url,
            FakeResponse(
                content=b"%PDF-1.7\n%%EOF\n",
                text=None,
                headers={"Content-Type": "application/pdf"},
            ),
        )
        sess.add(
            "GET",
            appendix_url,
            FakeResponse(
                content=b"word attachment",
                text=None,
                headers={"Content-Type": "application/octet-stream"},
            ),
        )

        detail = sppweb.fetch_detail(sess, "123")

        self.assertEqual(len(detail["attachments"]), 2)
        self.assertEqual(
            detail["main_pdf"],
            main_url,
        )

    def test_fetch_detail_raises_session_expired_for_login_html(self):
        detail_url = sppweb.DETAIL_URL.format("123")
        sess = FakeSession().add("GET", detail_url, FakeResponse(text=LOGIN_HTML))
        with self.assertRaises(sppweb.SessionExpiredError):
            sppweb.fetch_detail(sess, "123")

    def test_fetch_detail_raises_access_blocked_for_403(self):
        detail_url = sppweb.DETAIL_URL.format("123")
        sess = FakeSession().add(
            "GET", detail_url, FakeResponse(403, text=CLOUDFLARE_HTML)
        )
        with self.assertRaises(sppweb.AccessBlockedError):
            sppweb.fetch_detail(sess, "123")

    def test_fetch_detail_rejects_unrelated_200_page(self):
        detail_url = sppweb.DETAIL_URL.format("123")
        sess = FakeSession().add(
            "GET",
            detail_url,
            FakeResponse(text="<html><body>maintenance</body></html>"),
        )
        with self.assertRaises(sppweb.UnexpectedPageError):
            sppweb.fetch_detail(sess, "123")


class DownloadTests(unittest.TestCase):
    PDF_URL = sppweb.BASE + "files/main.pdf?token=abc123"

    def _dest(self, folder):
        return os.path.join(folder, "document.pdf")

    def test_download_rejects_403_without_creating_destination(self):
        sess = FakeSession().add(
            "GET", self.PDF_URL, FakeResponse(403, text=CLOUDFLARE_HTML)
        )
        with tempfile.TemporaryDirectory() as folder:
            dest = self._dest(folder)
            with self.assertRaises(sppweb.AccessBlockedError):
                sppweb.download(sess, self.PDF_URL, dest)
            self.assertFalse(os.path.exists(dest))

    def test_download_recognizes_expired_session_html(self):
        sess = FakeSession().add(
            "GET",
            self.PDF_URL,
            FakeResponse(
                200, text=LOGIN_HTML, headers={"Content-Type": "text/html; charset=utf-8"}
            ),
        )
        with tempfile.TemporaryDirectory() as folder:
            dest = self._dest(folder)
            with self.assertRaises(sppweb.SessionExpiredError):
                sppweb.download(sess, self.PDF_URL, dest)
            self.assertFalse(os.path.exists(dest))

    def test_download_recognizes_200_cloudflare_challenge(self):
        sess = FakeSession().add(
            "GET",
            self.PDF_URL,
            FakeResponse(
                200,
                text=CLOUDFLARE_HTML,
                headers={"Content-Type": "text/html; charset=utf-8"},
            ),
        )
        with tempfile.TemporaryDirectory() as folder:
            dest = self._dest(folder)
            with self.assertRaises(sppweb.AccessBlockedError):
                sppweb.download(sess, self.PDF_URL, dest)
            self.assertFalse(os.path.exists(dest))

    def test_download_rejects_non_pdf_200_body(self):
        sess = FakeSession().add(
            "GET",
            self.PDF_URL,
            FakeResponse(
                200,
                content=b"<html><body>not a pdf</body></html>",
                text=None,
                headers={"Content-Type": "text/html"},
            ),
        )
        with tempfile.TemporaryDirectory() as folder:
            dest = self._dest(folder)
            with self.assertRaises(sppweb.DownloadError):
                sppweb.download(sess, self.PDF_URL, dest)
            self.assertFalse(os.path.exists(dest))

    def test_download_accepts_pdf_magic_with_generic_content_type(self):
        pdf = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
        sess = FakeSession().add(
            "GET",
            self.PDF_URL,
            FakeResponse(
                200,
                content=pdf,
                text=None,
                headers={"Content-Type": "application/octet-stream"},
            ),
        )
        with tempfile.TemporaryDirectory() as folder:
            dest = self._dest(folder)
            result = sppweb.download(sess, self.PDF_URL, dest)
            self.assertEqual(result, dest)
            with open(dest, "rb") as saved:
                self.assertEqual(saved.read(), pdf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
