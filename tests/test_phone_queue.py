"""The phone asks the server what is already in the queue, so it never pulls a document twice.

The record is the server's own queue (backed up on Drive), not a list kept on the
phone: if the server loses a document, it drops out of the answer and the phone
sends it again.  Offline: no Google, no Render, no SPP website.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from core import now_th
from web import docmode

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    webmain = None


def put_job(test, book_id, status, source="phone"):
    job = {"id": "t-%s-%s" % (book_id, status), "book_id": book_id, "status": status,
           "source": source, "created": now_th()}
    with docmode._lock:
        docmode._jobs[job["id"]] = job
    test.addCleanup(lambda: docmode._jobs.pop(job["id"], None))
    return job


class HeldIdsTests(unittest.TestCase):
    def test_counts_documents_whose_file_the_server_holds(self):
        for book_id, status in (("1", "stored"), ("2", "ready"), ("3", "save_error"),
                                ("4", "done")):
            put_job(self, book_id, status)
        self.assertLessEqual({"1", "2", "3", "4"}, docmode.phone_held_ids())

    def test_failed_or_unfinished_uploads_are_pulled_again(self):
        put_job(self, "5", "error")
        put_job(self, "6", "uploading")
        put_job(self, "7", "stored", source="web")
        self.assertFalse({"5", "6", "7"} & docmode.phone_held_ids())


class RestoreSignalTests(unittest.TestCase):
    def setUp(self):
        docmode._queue_restored.clear()
        self.addCleanup(docmode._queue_restored.set)

    def test_waiting_ends_when_restore_finishes_even_without_drive(self):
        self.assertFalse(docmode.wait_queue_restored(0.01))
        with patch.dict(sys.modules, {"drive": types.SimpleNamespace(enabled=lambda: False)}):
            self.assertEqual(docmode.restore_queue(), 0)
        self.assertTrue(docmode.wait_queue_restored(0.01))

    def test_waiting_ends_even_when_restore_blows_up(self):
        def boom():
            raise RuntimeError("drive down")

        with patch.object(docmode, "_restore_from_drive", boom):
            with self.assertRaises(RuntimeError):
                docmode.restore_queue()
        self.assertTrue(docmode.wait_queue_restored(0.01))


class FakeStore:
    def __init__(self, done):
        self.done = set(done)

    def history_ids(self):
        return set(self.done)


@unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class HistoryEndpointTests(unittest.TestCase):
    HEADERS = {"X-Phone-Token": "tok"}

    def setUp(self):
        for p in (patch.dict(os.environ, {"SARABAN_PHONE_TOKEN": "tok"}),
                  patch("store.get_store", return_value=FakeStore({"9"}))):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(webmain.app)

    def test_lists_queued_documents_next_to_handled_ones(self):
        docmode._queue_restored.set()
        put_job(self, "186796", "stored")
        put_job(self, "186782", "error")
        r = self.client.get("/api/phone/history", headers=self.HEADERS)
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["done"], ["9"])
        self.assertIn("186796", d["queued"])
        self.assertNotIn("186782", d["queued"])
        self.assertTrue(d["queue_ready"])

    def test_waits_for_the_queue_to_come_back_after_a_restart(self):
        docmode._queue_restored.clear()
        self.addCleanup(docmode._queue_restored.set)

        def restore_from_drive_later():
            time.sleep(0.2)
            put_job(self, "186769", "stored")
            docmode._queue_restored.set()

        threading.Thread(target=restore_from_drive_later).start()
        r = self.client.get("/api/phone/history", headers=self.HEADERS)
        self.assertIn("186769", r.json()["queued"])

    def test_still_needs_the_phone_token(self):
        r = self.client.get("/api/phone/history", headers={"X-Phone-Token": "wrong"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
