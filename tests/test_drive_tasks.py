"""Background Drive work must go out one request at a time.

The shared Drive client sits on httplib2, which is not thread-safe, and every
phone submission used to start its own upload thread.  Several uploads at once
on Render's 512 MB free instance can take the whole web service down.  These
tests use a fake drive module and fake requests; they never touch Google.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from unittest.mock import patch

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
