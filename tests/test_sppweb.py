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
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch

import requests
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

import sppweb

# Never pace the offline suite; the delay only exists to be polite to the real site.
sppweb.REQUEST_GAP = 0


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

# Cloudflare injects this fingerprinting beacon into ordinary SUCCESSFUL pages.
# It is not a challenge.  Treating it as one made every request look blocked.
# Copied from a real HTTP 200 response served by the live site.
JSD_BEACON = (
    "<script>(function(){var a=document.createElement('script');"
    "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';"
    "document.getElementsByTagName('head')[0].appendChild(a);})();</script>"
)


def with_beacon(html: str) -> str:
    return html.replace("</body>", JSD_BEACON + "</body>")

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


class RetryAfterTests(unittest.TestCase):
    def test_retry_after_accepts_delta_seconds(self):
        response = FakeResponse(429, headers={"Retry-After": "17"})
        self.assertEqual(sppweb._retry_after(response, 0), 17.0)

    def test_retry_after_accepts_http_date(self):
        now = 1_700_000_000.0
        retry_at = datetime.fromtimestamp(now + 17, tz=timezone.utc)
        response = FakeResponse(
            503, headers={"Retry-After": format_datetime(retry_at, usegmt=True)})
        self.assertEqual(sppweb._retry_after(response, 0, now=now), 17.0)

    def test_retry_after_too_far_away_does_not_shorten_the_wait(self):
        response = FakeResponse(429, headers={"Retry-After": "120"})
        self.assertIsNone(sppweb._retry_after(response, 0))

    def test_send_sleeps_for_the_full_retry_after_before_retrying(self):
        busy = FakeResponse(429, headers={"Retry-After": "7"})
        success = FakeResponse(200, text="ok")
        sess = FakeSession().add("GET", "https://example.test/data", busy, success)

        with patch.object(sppweb, "_pace"), patch.object(sppweb.time, "sleep") as sleep:
            result = sppweb._send(sess, "get", "https://example.test/data")

        self.assertIs(result, success)
        self.assertEqual(len(sess.calls), 2)
        sleep.assert_called_once_with(7.0)
        self.assertTrue(busy.closed)

    def test_send_returns_long_retry_after_response_without_retrying_early(self):
        busy = FakeResponse(429, headers={"Retry-After": "120"})
        success = FakeResponse(200, text="must not be requested yet")
        sess = FakeSession().add("GET", "https://example.test/data", busy, success)

        with patch.object(sppweb, "_pace"), patch.object(sppweb.time, "sleep") as sleep:
            result = sppweb._send(sess, "get", "https://example.test/data")

        self.assertIs(result, busy)
        self.assertEqual(len(sess.calls), 1)
        sleep.assert_not_called()
        self.assertFalse(busy.closed, "returned response still belongs to the caller")


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

    def test_is_logged_in_returns_false_for_login_page_even_with_http_403(self):
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(403, text=LOGIN_HTML)
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

    def test_jsd_beacon_on_successful_page_is_not_a_block(self):
        """Cloudflare's fingerprinting script rides along on ordinary 200s.

        Matching it as a challenge marker made every successful request look
        blocked, and only on networks Cloudflare chose to fingerprint.
        """
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(200, text=with_beacon(list_html()))
        )
        self.assertTrue(sppweb.is_logged_in(sess))

    def test_jsd_beacon_on_login_page_reports_logged_out_not_blocked(self):
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(200, text=with_beacon(LOGIN_HTML))
        )
        self.assertFalse(sppweb.is_logged_in(sess))

    def test_real_challenge_page_is_still_detected(self):
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(200, text=CLOUDFLARE_HTML)
        )
        with self.assertRaises(sppweb.AccessBlockedError):
            sppweb.is_logged_in(sess)

    def test_cf_mitigated_header_alone_is_enough_to_detect_a_challenge(self):
        sess = FakeSession().add(
            "GET",
            sppweb.NEWS_URL,
            FakeResponse(200, text="<html><body>x</body></html>",
                         headers={"cf-mitigated": "challenge"}),
        )
        with self.assertRaises(sppweb.AccessBlockedError):
            sppweb.is_logged_in(sess)

    def test_403_carrying_a_real_page_is_not_reported_as_cloudflare(self):
        """The PHP app answers 403 on its own; that is not a WAF block."""
        sess = FakeSession().add(
            "GET", sppweb.NEWS_URL, FakeResponse(403, text=list_html())
        )
        with self.assertRaises(sppweb.UpstreamResponseError) as caught:
            sppweb.is_logged_in(sess)
        self.assertNotIsInstance(caught.exception, sppweb.AccessBlockedError)


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

    def _site_with_pager_lagging_behind(self):
        """เว็บที่แถบเลขหน้าโชว์ถึงหน้า ๒ แต่ของจริงมีถึงหน้า ๔

        เว็บ สพป. เป็นแบบนี้จริง และเลขหน้าที่เกินจะถูก "หนีบ" ให้คืนเนื้อหา
        หน้าสุดท้ายซ้ำมา (ขอหน้า ๕ ได้หน้า ๔)
        """
        landing = list_html("123").replace(
            "</body>", f'<a href="{sppweb.NEWS_URL}&page=2">2</a></body>')
        sess = FakeSession()
        sess.add("GET", sppweb.NEWS_URL, FakeResponse(text=landing))
        for page, book in ((1, "123"), (2, "124"), (3, "125"), (4, "126")):
            sess.add("GET", f"{sppweb.NEWS_URL}&page={page}",
                     FakeResponse(text=list_html(book)))
        sess.add("GET", f"{sppweb.NEWS_URL}&page=5",
                 FakeResponse(text=list_html("126")))
        return sess

    def test_finds_last_page_even_when_pager_lags_several_pages_behind(self):
        # ของเดิมเขยิบแค่หน้าเดียวแล้วหยุด เลยได้ ๓ ทั้งที่ของจริงคือ ๔
        self.assertEqual(sppweb.find_last_page(self._site_with_pager_lagging_behind()), 4)

    def test_list_documents_includes_newest_page_beyond_the_pager(self):
        # เรื่องใหม่สุดอยู่หน้า ๔ ถ้าหาหน้าสุดท้ายพลาด เรื่องนี้จะหายไปเงียบๆ
        docs = sppweb.list_documents(self._site_with_pager_lagging_behind(), pages=2)
        self.assertEqual([d["book_id"] for d in docs], ["126", "125"])

    def test_pagination_safety_cap_raises_instead_of_returning_incomplete_page(self):
        landing = list_html("100").replace(
            "</body>", f'<a href="{sppweb.NEWS_URL}&page=2">2</a></body>')
        sess = FakeSession().add("GET", sppweb.NEWS_URL, FakeResponse(text=landing))
        sess.add(
            "GET", f"{sppweb.NEWS_URL}&page=2",
            FakeResponse(text=list_html("102")),
        )
        # Every bounded probe still has a new id.  The old implementation silently
        # returned page 14 even though the real last page could be much later.
        for page in range(3, 3 + sppweb.MAX_PAGE_PROBE):
            sess.add(
                "GET", f"{sppweb.NEWS_URL}&page={page}",
                FakeResponse(text=list_html(str(100 + page))),
            )

        with self.assertRaisesRegex(sppweb.UnexpectedPageError, "ไม่ส่งรายการที่ไม่ครบ"):
            sppweb.find_last_page(sess)

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


class AttachmentProbeTests(unittest.TestCase):
    WRONG = sppweb.BASE + "guessed/wrong.pdf"
    RIGHT = sppweb.BASE + "files/right.pdf"

    def test_head_403_falls_back_to_get_and_continues_to_next_candidate(self):
        wrong_head = FakeResponse(403, text="")
        wrong_get = FakeResponse(
            403, text="Forbidden", headers={"Content-Type": "text/plain"})
        right_head = FakeResponse(
            200, text=None, content=b"", headers={"Content-Type": "application/pdf"})
        sess = FakeSession()
        sess.add("HEAD", self.WRONG, wrong_head)
        sess.add("GET", self.WRONG, wrong_get)
        sess.add("HEAD", self.RIGHT, right_head)

        result = sppweb._pick_attachment_url(
            sess, [("wrong", self.WRONG), ("right", self.RIGHT)])

        self.assertEqual(result, self.RIGHT)
        self.assertEqual(
            [(call["method"], call["url"]) for call in sess.calls],
            [("HEAD", self.WRONG), ("GET", self.WRONG), ("HEAD", self.RIGHT)],
        )
        self.assertTrue(all(r.closed for r in (wrong_head, wrong_get, right_head)))

    def test_http_403_login_html_during_range_probe_is_session_expired(self):
        head = FakeResponse(403, text="")
        ranged = FakeResponse(
            403, text=LOGIN_HTML, headers={"Content-Type": "text/html"})
        sess = FakeSession().add("HEAD", self.WRONG, head).add("GET", self.WRONG, ranged)

        with self.assertRaises(sppweb.SessionExpiredError):
            sppweb._pick_attachment_url(sess, [("wrong", self.WRONG)])

        self.assertTrue(head.closed)
        self.assertTrue(ranged.closed)

    def test_ambiguous_2xx_inspects_prefix_and_rejects_mislabeled_html(self):
        ambiguous_head = FakeResponse(200, text=None, content=b"", headers={})
        html_get = FakeResponse(
            200,
            text="<!doctype html><html><body>file not found</body></html>",
            # Deliberately wrong: the body, not this header, must win.
            headers={"Content-Type": "application/pdf"},
        )
        right_head = FakeResponse(
            200, text=None, content=b"", headers={"Content-Type": "application/pdf"})
        sess = FakeSession()
        sess.add("HEAD", self.WRONG, ambiguous_head)
        sess.add("GET", self.WRONG, html_get)
        sess.add("HEAD", self.RIGHT, right_head)

        result = sppweb._pick_attachment_url(
            sess, [("wrong", self.WRONG), ("right", self.RIGHT)])

        self.assertEqual(result, self.RIGHT)
        self.assertTrue(all(r.closed for r in (ambiguous_head, html_get, right_head)))

    def test_ambiguous_2xx_rejects_plain_error_without_content_type(self):
        head = FakeResponse(200, text=None, content=b"", headers={})
        body = FakeResponse(200, text="File not found", headers={})
        sess = FakeSession().add("HEAD", self.WRONG, head).add("GET", self.WRONG, body)

        result = sppweb._pick_attachment_url(sess, [("wrong", self.WRONG)])

        self.assertIsNone(result)
        self.assertTrue(head.closed)
        self.assertTrue(body.closed)

    def test_explicit_cloudflare_challenge_still_aborts_probe(self):
        challenged = FakeResponse(403, text=CLOUDFLARE_HTML)
        sess = FakeSession().add("HEAD", self.WRONG, challenged)

        with self.assertRaises(sppweb.AccessBlockedError):
            sppweb._pick_attachment_url(sess, [("wrong", self.WRONG)])

        self.assertTrue(challenged.closed)


class RequestBudgetTests(unittest.TestCase):
    """Bursts of requests are what gets an IP rate-limited; keep the count down."""

    DETAIL = sppweb.DETAIL_URL.format("123")
    WORKING = sppweb.BASE + "modules/book/bookregister/files/x.pdf"
    DEAD = (
        sppweb.BASE + "modules/bookregister/files/x.pdf",
        sppweb.BASE + "modules/bookregister/bookregister/files/x.pdf",
    )
    HTML = """
    <!doctype html><html><body><h2>รายละเอียดหนังสือ</h2>
      <a href="bookregister/files/x.pdf">1. หนังสือหลัก</a>
    </body></html>
    """

    def setUp(self):
        sppweb._rule_hint = None          # a learned rule must not leak between tests

    def _session(self):
        sess = FakeSession()
        sess.add("GET", self.DETAIL, FakeResponse(text=self.HTML))
        for url in self.DEAD:
            sess.add("HEAD", url, FakeResponse(404, content=b"", text=None))
        sess.add(
            "HEAD",
            self.WORKING,
            FakeResponse(200, content=b"", text=None,
                         headers={"Content-Type": "application/pdf"}),
        )
        return sess

    def test_attachment_probe_uses_head_not_a_body_download(self):
        sess = self._session()
        sppweb.fetch_detail(sess, "123")
        probes = [c for c in sess.calls if c["url"].endswith("x.pdf")]
        self.assertTrue(probes)
        self.assertTrue(all(c["method"] == "HEAD" for c in probes),
                        f"ต้องใช้ HEAD ตรวจลิงก์ ไม่ใช่โหลดตัวไฟล์: {probes}")

    def test_working_path_rule_is_reused_on_the_next_document(self):
        first = self._session()
        detail = sppweb.fetch_detail(first, "123")
        self.assertEqual(detail["main_pdf"], self.WORKING)
        self.assertEqual(len([c for c in first.calls if c["method"] == "HEAD"]), 3)

        second = self._session()
        sppweb.fetch_detail(second, "123")
        self.assertEqual(
            [c["url"] for c in second.calls if c["method"] == "HEAD"],
            [self.WORKING],
            "เอกสารถัดไปต้องยิงแค่ครั้งเดียว เพราะจำกฎที่ใช้ได้ไว้แล้ว")

    def test_listing_two_pages_stays_within_a_small_request_budget(self):
        landing = list_html("123").replace(
            "</body>", f'<a href="{sppweb.NEWS_URL}&page=2">2</a></body>')
        sess = FakeSession()
        sess.add("GET", sppweb.NEWS_URL, FakeResponse(text=landing))
        sess.add("GET", f"{sppweb.NEWS_URL}&page=3", FakeResponse(text=EMPTY_LIST_HTML))
        sess.add("GET", f"{sppweb.NEWS_URL}&page=1", FakeResponse(text=list_html("123")))
        sess.add("GET", f"{sppweb.NEWS_URL}&page=2", FakeResponse(text=list_html("124")))

        sppweb.list_documents(sess, pages=2)

        # landing + probe + หน้า 1 + หน้า 2 = 4 ครั้ง ไม่ควรเกินนี้
        self.assertLessEqual(len(sess.calls), 4, [c["url"] for c in sess.calls])


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

    def test_download_treats_login_html_with_http_403_as_expired_session(self):
        response = FakeResponse(
            403, text=LOGIN_HTML, headers={"Content-Type": "text/html; charset=utf-8"})
        sess = FakeSession().add("GET", self.PDF_URL, response)
        with tempfile.TemporaryDirectory() as folder:
            dest = self._dest(folder)
            with self.assertRaises(sppweb.SessionExpiredError):
                sppweb.download(sess, self.PDF_URL, dest)
            self.assertFalse(os.path.exists(dest))
        self.assertTrue(response.closed)

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
