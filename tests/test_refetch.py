"""🔄 Fetch a phone document again, with the attachments that did not come along.

A phone still running the scripts from before attachments sends the covering letter alone, and
the phone never fetches a document that is already in the queue - so a document queued before
the scripts were updated could never get its attachments, and on Render the server cannot
reach the สพป. site itself.  Now the arranging page asks for the document again: the phone's
next run fetches it (the history stops counting it as queued or done), the server puts the
new file into the same queue item - the page left open still finds it, the old Drive copy
goes - and the page offers to load it again.  Offline: the AI, the store, Drive and LINE are fakes.
"""
from __future__ import annotations

import os
import shutil
import unittest
from unittest.mock import patch

from tests.test_redo import RedoBase, settle, text_pdf

try:
    import pymupdf
except ImportError:         # phone-only setup
    pymupdf = None

try:
    from web import docmode
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    docmode = None

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):
    webmain = None

ATTACH = "📥 ๑. หนังสือนำ\n👉 https://x/a.pdf\n📥 ๒. บัญชีรายชื่อ\n👉 https://x/b.pdf"


class RefetchBase(RedoBase):
    def setUp(self):
        super().setUp()
        self.list_pdf = text_pdf(os.path.join(self.work, "list.pdf"),
                                 [["บัญชีรายชื่อแนบท้าย", "โรงเรียนบ้านโพนทองประชาอุทิศ"], ["บัญชี หน้า ๒"]])

    def phone_sends(self, book_id="88", with_list=False):
        """what /api/phone/submit does with what the phone sent (the letter, and the list if it has it)"""
        n = len(os.listdir(self.work))
        src = shutil.copy(self.letter, os.path.join(self.work, f"sent{n}.pdf"))
        extras = [shutil.copy(self.list_pdf, os.path.join(self.work, f"extra{n}.pdf"))] if with_list else []
        job, created = docmode.start_from_phone_path("phone", src, {
            "book_id": book_id, "doc_no": "ศธ 04142/ว1234", "doc_title": "ประชุม",
            "doc_date": "15 ม.ค. 70", "sender": "สพป.", "emoji": "🔵", "attach": ATTACH}, extras=extras)
        docmode._drive_tasks.join()
        return job, created

    def opened(self, job):
        self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        return job

    def save(self, job, reserve=None):
        payload = {"stamp": job["stamp"], "boxes": job["boxes"], "date": "", "time": ""}
        result = docmode.finalize(job, payload, reserve or (lambda **kw: self.store.register(**kw)))
        docmode._drive_tasks.join()
        return result


@unittest.skipIf(docmode is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class RefetchTests(RefetchBase):
    def test_the_letter_alone_is_fetched_again_into_the_same_queue_item(self):
        job = self.opened(self.phone_sends()[0])
        self.assertEqual((job["total_pages"], docmode.missing_extras(job)), (2, 1))
        self.assertNotIn("88", docmode.refetch_ids())
        docmode.request_refetch(job)
        self.assertEqual(docmode.refetch_ids(), {"88"})
        self.assertEqual(job["status"], "ready")              # can still be registered as it is
        again, created = self.phone_sends(with_list=True)
        self.assertTrue(created)                              # used to be "อยู่ในคิวเดิม" and thrown away
        self.assertIs(again, job)                             # the page left open still finds it
        self.assertEqual((job["status"], job["refetched"]), ("stored", True))
        self.assertNotIn("refetch", job)
        self.assertNotIn("pages", job)                        # the AI's work on the old file is gone
        self.assertEqual(docmode.refetch_ids(), set())
        self.opened(job)
        self.assertEqual((job["total_pages"], job["main_pages"], job["extras_got"]), (4, 2, 1))
        self.assertEqual(docmode.missing_extras(job), 0)
        self.assertEqual([pg["index"] for pg in job["pages"]], [1, 2, 3, 4])

    def test_without_asking_the_queue_item_is_kept_as_before(self):
        job = self.opened(self.phone_sends()[0])
        again, created = self.phone_sends(with_list=True)
        self.assertFalse(created)
        self.assertIs(again, job)
        self.assertEqual((job["status"], job["total_pages"]), ("ready", 2))

    def test_the_new_file_replaces_the_old_copy_on_drive(self):
        job = self.opened(self.phone_sends()[0])
        self.assertEqual(job["drive_file_id"], "f88")
        docmode.request_refetch(job)
        self.phone_sends(with_list=True)
        self.assertEqual(job["drive_file_id"], "f88-1")
        self.drive.queue_drop.assert_called_once_with("f88")
        self.assertEqual(self.metas["f88-1"]["extras_got"], 1)   # restored after a restart: with the list

    def test_old_page_pictures_are_not_shown_for_the_new_file(self):
        job = self.opened(self.phone_sends(with_list=True)[0])
        old = job["pages"][2]["png"]
        self.assertRegex(old, rf"^/api/doc/{job['id']}/page/3\?v=[\w-]+$")
        self.assertTrue(docmode.preview_page(job, 3))          # drawn once, kept in the job's folder
        docmode.request_refetch(job)
        self.phone_sends(with_list=True)
        self.assertEqual(os.listdir(job["dir"]), ["doc.pdf"])   # nothing drawn from the old file is left
        self.opened(job)
        self.assertNotEqual(job["pages"][2]["png"], old)       # nor the browser's copy of the old page

    def test_a_redo_keeps_its_number(self):
        job = self.opened(self.phone_sends()[0])
        self.save(job)
        self.assertIs(docmode.reopen_phone("88", "๕๗"), job)
        self.opened(job)
        docmode.request_refetch(job)
        self.assertIn("88", docmode.refetch_ids())             # registered, but being redone: fetch it anyway
        self.phone_sends(with_list=True)
        self.assertEqual(job["redo_no"], "๕๗")
        self.opened(job)
        self.assertEqual((job["receipt_no"], job["total_pages"]), ("๕๗", 4))

        def no_new_number(**kw):
            raise AssertionError("ลงรับใหม่ต้องไม่กินเลขใหม่")

        self.assertEqual(self.save(job, no_new_number)["receipt_no"], "๕๗")
        self.assertEqual(len(self.store.rows), 1)

    def test_only_while_it_waits_to_be_registered(self):
        job = self.opened(self.phone_sends()[0])
        docmode.request_refetch(job)
        self.save(job)                                         # did not wait for the phone
        self.assertEqual(docmode.refetch_ids(), set())         # the phone is not sent for it any more
        with self.assertRaises(docmode.AlreadyHandledError):   # and a late delivery changes nothing
            self.phone_sends(with_list=True)
        self.assertEqual(job["status"], "done")
        with self.assertRaises(docmode.JobStateError):
            docmode.request_refetch(job)
        web_job = docmode.create_job("ครูเอ")
        self.addCleanup(lambda: docmode._jobs.pop(web_job["id"], None))
        web_job.update(status="ready", book_id="77")
        with self.assertRaises(docmode.JobStateError):          # opened from the สพป. site: opened there again
            docmode.request_refetch(web_job)


@unittest.skipIf(webmain is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class RefetchOnTheWebTests(RefetchBase):
    """the button on the arranging page, what the phone is told, the page waiting for the new file"""

    USERS = {"ครูเอ": {"status": "approved", "salt": "a"}}

    def setUp(self):
        super().setUp()
        p = patch.object(webmain, "load_users", return_value=self.USERS)
        p.start()
        self.addCleanup(p.stop)
        docmode._queue_restored.set()
        self.web = TestClient(webmain.app, cookies={
            "session": webmain.make_session("ครูเอ", self.USERS["ครูเอ"])})

    def history(self):
        r = TestClient(webmain.app).get("/api/phone/history", headers={"X-Phone-Token": "tok"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_the_phone_is_sent_for_it_and_the_page_learns_when_it_came(self):
        job = self.opened(self.phone_sends()[0])
        self.assertIn("88", self.history()["queued"])
        d = self.web.get(f"/api/doc/{job['id']}").json()
        self.assertEqual((d["missing_extras"], d["refetch"]), (1, False))
        r = self.web.post(f"/api/doc/{job['id']}/refetch")
        self.assertEqual(r.status_code, 200, r.text)
        h = self.history()
        self.assertNotIn("88", h["queued"])                    # the phone fetches it on its next run
        self.assertNotIn("88", h["done"])
        self.assertTrue(self.web.get(f"/api/doc/{job['id']}").json()["refetch"])
        self.assertEqual(self.web.get(f"/api/doc/{job['id']}?brief=1").json(),
                         {"status": "ready", "refetch": True, "refetched": False})
        self.assertTrue(self.web.get("/api/spp/list").json()["days"][0]["docs"][0]["refetch"])
        self.phone_sends(with_list=True)
        self.assertEqual(self.web.get(f"/api/doc/{job['id']}?brief=1").json(),
                         {"status": "stored", "step": "รอเปิดอ่าน", "refetched": True})
        self.assertIn("88", self.history()["queued"])          # in the queue again, with its list
        self.assertFalse(self.web.get("/api/spp/list").json()["days"][0]["docs"][0]["refetch"])
        self.assertEqual(self.web.post(f"/api/doc/{job['id']}/prepare").status_code, 200)
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(self.web.get(f"/api/doc/{job['id']}").json()["missing_extras"], 0)

    def test_a_registered_document_being_redone_is_fetched_too(self):
        job = self.opened(self.phone_sends()[0])
        self.save(job)
        self.assertIn("88", self.history()["done"])
        r = self.web.post("/api/doc/open", json={"book_id": "88", "redo_no": "๕๗"})
        self.assertEqual(r.json()["job_id"], job["id"])
        self.opened(job)
        self.assertEqual(self.web.post(f"/api/doc/{job['id']}/refetch").status_code, 200)
        self.assertNotIn("88", self.history()["done"])

    def test_asking_after_it_was_registered_is_refused(self):
        job = self.opened(self.phone_sends()[0])
        self.save(job)
        self.assertEqual(self.web.post(f"/api/doc/{job['id']}/refetch").status_code, 409)
        self.assertEqual(TestClient(webmain.app).post(f"/api/doc/{job['id']}/refetch").status_code, 401)


if __name__ == "__main__":
    unittest.main()
