"""Background Drive work must go out one request at a time.

The shared Drive client sits on httplib2, which is not thread-safe, and every
phone submission used to start its own upload thread.  Several uploads at once
on Render's 512 MB free instance can take the whole web service down.  These
tests use a fake drive module and fake requests; they never touch Google.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import core
import drive
from core import now_th
from web import docmode


class FakeDrive:
    """Stands in for the drive module and records how many uploads overlap."""

    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()
        self.put = []
        self.dropped = []

    def queue_put(self, pdf, meta):
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        with self.guard:
            self.active -= 1
        self.put.append(meta["book_id"])
        return "f" + meta["book_id"]

    def queue_drop(self, fid):
        self.dropped.append(fid)
        return True


def phone_job(book_id, status="stored"):
    return {"id": "job" + book_id, "book_id": book_id, "created": now_th(),
            "status": status, "source": "phone"}


class DriveTaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDrive()
        p = patch.dict(sys.modules, {"drive": self.fake})
        p.start()
        self.addCleanup(p.stop)

    def finish(self):
        docmode._drive_tasks.join()

    def test_backups_from_a_burst_of_submissions_run_one_at_a_time(self):
        jobs = [phone_job(str(i)) for i in range(6)]
        for job in jobs:
            docmode._backup_to_drive(job, "doc.pdf")
        self.finish()
        self.assertEqual(self.fake.max_active, 1)
        self.assertEqual([j.get("drive_file_id") for j in jobs],
                         ["f0", "f1", "f2", "f3", "f4", "f5"])

    def test_drop_queued_while_its_backup_is_uploading_still_removes_the_file(self):
        started, release = threading.Event(), threading.Event()

        def slow_put(pdf, meta):
            started.set()
            release.wait(2)
            return "f9"

        self.fake.queue_put = slow_put
        job = phone_job("9")
        docmode._backup_to_drive(job, "doc.pdf")
        self.assertTrue(started.wait(2))
        # registered before the backup finished: there is no file id yet at this point
        job["status"] = "done"
        docmode._drop_backup(job)
        release.set()
        self.finish()
        self.assertEqual(self.fake.dropped, ["f9"])
        self.assertEqual(job["drive_file_id"], "")

    def test_job_finished_before_its_turn_is_not_uploaded(self):
        docmode._backup_to_drive(phone_job("7", status="done"), "doc.pdf")
        self.finish()
        self.assertEqual(self.fake.put, [])

    def test_backup_remembers_the_job_id(self):
        metas = []
        self.fake.queue_put = lambda pdf, meta: metas.append(meta) or "f3"
        job = phone_job("3")
        docmode._backup_to_drive(job, "doc.pdf")
        self.finish()
        self.assertEqual(metas[0]["job_id"], job["id"])


class RestoreKeepsJobIdTests(unittest.TestCase):
    """a page left open on /doc?job=... before a restart must still find its document"""

    def setUp(self):
        self.items = []
        fake = FakeDrive()
        fake.enabled = lambda: True
        fake.queue_list = lambda: self.items
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        for p in (patch.dict(sys.modules, {"drive": fake}),
                  patch.object(docmode, "_durable_phone_record", return_value=None),
                  patch.object(core, "WORK_DIR", work)):
            p.start()
            self.addCleanup(p.stop)
        before = set(docmode._jobs)
        self.addCleanup(lambda: [docmode._jobs.pop(j) for j in set(docmode._jobs) - before])

    def backed_up(self, book_id, job_id=None):
        meta = {"book_id": book_id, "doc_no": "ศธ 1/" + book_id, "created": now_th().isoformat()}
        if job_id is not None:
            meta["job_id"] = job_id
        self.items.append({"id": "f" + book_id, "meta": meta})

    def job_of(self, book_id):
        return next(j for j in docmode._jobs.values() if j.get("book_id") == book_id)

    def test_comes_back_under_the_same_id(self):
        self.backed_up("501", "ab" * 16)
        self.assertEqual(docmode._restore_from_drive(), 1)
        job = docmode._jobs["ab" * 16]
        self.assertEqual((job["book_id"], job["status"], job["id"]), ("501", "stored", "ab" * 16))

    def test_odd_taken_or_missing_ids_get_a_fresh_one(self):
        with docmode._lock:
            docmode._jobs["cd" * 16] = {"id": "cd" * 16, "book_id": "x", "created": now_th()}
        self.backed_up("502", "../../etc")
        self.backed_up("503", "cd" * 16)
        self.backed_up("504")                     # backed up before ids were kept
        self.assertEqual(docmode._restore_from_drive(), 3)
        for bid in ("502", "503", "504"):
            jid = self.job_of(bid)["id"]
            self.assertRegex(jid, r"^[0-9a-f]{32}$")
            self.assertNotEqual(jid, "cd" * 16)


class DriveRequestLockTests(unittest.TestCase):
    def test_requests_from_many_threads_never_overlap(self):
        state = {"active": 0, "max": 0}
        guard = threading.Lock()

        class Req:
            def execute(self):
                with guard:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.02)
                with guard:
                    state["active"] -= 1
                return {"id": "x"}

        threads = [threading.Thread(target=drive._run, args=(Req(),)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(state["max"], 1)


if __name__ == "__main__":
    unittest.main()
