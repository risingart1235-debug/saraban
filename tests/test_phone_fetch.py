"""Offline tests for how the phone uploader copes with a restarting Render server.

Render's free instance can restart in the middle of a run (out of memory, a new
deploy, waking from sleep).  While it boots, Render's own front door answers
502/503/504 or 429 instead of our app.  These tests fake requests and the clock;
they never contact Render or the SPP website.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import requests

import phone_fetch

PDF = b"%PDF-1.4 fake document body"
RENDER_PAGE = b"<html><body>Service Unavailable</body></html>"   # Render's page, not our JSON


def response(status, body=b"", headers=None):
    r = requests.models.Response()
    r.status_code = status
    r._content = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    r.headers.update(headers or {})
    r.reason = {200: "OK", 400: "Bad Request", 409: "Conflict", 429: "Too Many Requests",
                503: "Service Unavailable"}.get(status, "Error")
    return r


class FakeClock:
    """Stands in for the time module inside phone_fetch: sleeping just moves the clock."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakePost:
    """Replays canned answers and records the file bytes each attempt actually sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, url, headers=None, files=None, data=None, timeout=None):
        self.sent.append(files["file"][1].read())
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        fd, self.pdf = tempfile.mkstemp(suffix=".pdf")
        with os.fdopen(fd, "wb") as f:
            f.write(PDF)
        for p in (patch.object(phone_fetch, "time", self.clock),
                  patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        os.remove(self.pdf)

    def submit_with(self, post):
        with patch.object(phone_fetch.requests, "post", post):
            return phone_fetch.submit(self.pdf, {"book_id": "186769"})

    def test_waits_through_a_render_restart_then_sends(self):
        post = FakePost(response(503, RENDER_PAGE), response(503, RENDER_PAGE),
                        response(200, {"ok": True, "created": True, "job_id": "j1"}))
        res = self.submit_with(post)
        self.assertEqual(res["job_id"], "j1")
        self.assertEqual(self.clock.sleeps, [10, 20])
        # every attempt must carry the whole file, not an already-read empty handle
        self.assertEqual(post.sent, [PDF, PDF, PDF])

    def test_render_429_is_waited_out_not_reported_as_a_full_queue(self):
        post = FakePost(response(429, RENDER_PAGE, {"Retry-After": "30"}),
                        response(200, {"ok": True, "created": True, "job_id": "j2"}))
        self.assertEqual(self.submit_with(post)["job_id"], "j2")
        self.assertEqual(self.clock.sleeps, [30])

    def test_dropped_connection_is_retried(self):
        post = FakePost(requests.ConnectionError("reset"),
                        response(200, {"ok": True, "created": False, "job_id": "j3"}))
        self.assertEqual(self.submit_with(post)["job_id"], "j3")

    def test_gives_up_once_the_wait_limit_is_spent(self):
        post = FakePost(*[response(503, RENDER_PAGE)] * 50)
        with self.assertRaises(phone_fetch.ServerDown) as ctx:
            self.submit_with(post)
        self.assertIn("503", str(ctx.exception))
        self.assertLessEqual(sum(self.clock.sleeps), phone_fetch.WAIT_SERVER_SEC)
        self.assertLess(len(post.sent), 10)

    def test_conflict_means_handled_elsewhere_without_retrying(self):
        post = FakePost(response(409, {"detail": {"message": "ลงรับแล้ว", "status": "registered"}}))
        res = self.submit_with(post)
        self.assertTrue(res["already_handled"])
        self.assertEqual(self.clock.sleeps, [])

    def test_our_own_error_is_shown_with_its_message_and_not_retried(self):
        post = FakePost(response(400, {"detail": "รับเฉพาะไฟล์ PDF ที่ถูกต้อง"}))
        with self.assertRaises(RuntimeError) as ctx:
            self.submit_with(post)
        self.assertIn("รับเฉพาะไฟล์ PDF", str(ctx.exception))
        self.assertEqual(len(post.sent), 1)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        for p in (patch.object(phone_fetch, "time", self.clock),
                  patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)

    def test_waits_for_render_to_wake_up(self):
        answers = [response(503, RENDER_PAGE), response(200, {"ok": True, "done": ["1", "2"]})]
        with patch.object(phone_fetch.requests, "get", lambda *a, **k: answers.pop(0)):
            self.assertEqual(phone_fetch.fetch_history(), {"1", "2"})

    def test_missing_token_message_only_for_our_own_503(self):
        own = response(503, {"detail": "เซิร์ฟเวอร์ยังไม่ได้ตั้ง SARABAN_PHONE_TOKEN"})
        with patch.object(phone_fetch.requests, "get", lambda *a, **k: own), \
                patch.object(phone_fetch, "die", side_effect=SystemExit) as die:
            with self.assertRaises(SystemExit):
                phone_fetch.fetch_history()
        self.assertIn("SARABAN_PHONE_TOKEN", die.call_args[0][0])
        self.assertEqual(self.clock.sleeps, [])


class RunTests(unittest.TestCase):
    """The run must stop once the server is gone, not burn through every document."""

    def test_stops_downloading_after_the_server_stays_down(self):
        docs = [{"book_id": str(i), "doc_title": "เรื่อง %d" % i} for i in range(1, 9)]
        calls = []

        def submit(path, meta):
            calls.append(meta["book_id"])
            if len(calls) >= 3:
                raise phone_fetch.ServerDown("เซิร์ฟเวอร์ไม่พร้อม (503)")
            return {"ok": True, "created": True, "job_id": "j" + meta["book_id"]}

        def download(sess, url, dest):
            with open(dest, "wb") as f:
                f.write(PDF)

        sppweb = phone_fetch.sppweb
        with patch.object(phone_fetch, "TOKEN", "t"), \
                patch.object(phone_fetch, "ask_credentials", return_value=("u", "p")), \
                patch.object(phone_fetch, "fetch_history", return_value=set()), \
                patch.object(phone_fetch, "submit", submit), \
                patch.object(sppweb, "login", return_value=object()), \
                patch.object(sppweb, "list_documents", return_value=docs), \
                patch.object(sppweb, "fetch_detail",
                             return_value={"main_pdf": "x.pdf", "attachments": []}), \
                patch.object(sppweb, "download", side_effect=download) as dl, \
                patch.object(sppweb, "attach_text", return_value=""), \
                patch("builtins.print"):
            phone_fetch.main()
        self.assertEqual(calls, ["1", "2", "3"])
        self.assertEqual(dl.call_count, 3)


if __name__ == "__main__":
    unittest.main()
