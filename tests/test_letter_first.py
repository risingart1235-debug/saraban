"""Documents the phone queued before the update: the file named หนังสือราชการ was not on page 1.

Some news items list the letter third - 1.-2. สิ่งที่ส่งมาด้วย, 3. หนังสือราชการ+สิ่งที่ส่งมาด้วย.
Phone scripts from before took the first PDF as the letter, so page 1 was an enclosure and the
AI read the wrong paper.  The updated scripts send the letter first and say so (letter_first).
Documents already waiting in the queue cannot be fixed on the server - it only has the joined
file - and the phone never fetches a document twice, so the server spots them by the file
names in the attachment list, an updated phone fetches them again on its next run, and the new
file goes into the same queue item, as with 🔄 ดึงเรื่องนี้ใหม่.  A phone still on the old
scripts is not sent for them (it would bring the same order back, run after run).
Offline: the AI, the store, Drive and LINE are fakes.
"""
from __future__ import annotations

import os
import shutil
import unittest
from unittest.mock import patch

import sppweb
from tests.test_redo import text_pdf
from tests.test_refetch import RefetchBase

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

# the news item from the user's screen: the letter is the third file of four
FILES = [("สิ่งที่ส่งมาด้วย", "https://spp/f/a1.pdf"), ("สิ่งที่ส่งมาด้วย", "https://spp/f/a2.pdf"),
         ("หนังสือราชการ+สิ่งที่ส่งมาด้วย", "https://spp/f/letter.pdf"),
         ("สิ่งที่ส่งมาด้วย", "https://spp/f/a4.pdf")]


def attach(files=FILES):
    return sppweb.attach_text([{"name": n, "url": u} for n, u in files])


@unittest.skipIf(docmode is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class SpottingTests(unittest.TestCase):
    """which queued phone documents have an enclosure on page 1"""

    def job(self, files=FILES, **kw):
        # as the server keeps it: the phone's fields cleaned (line breaks become spaces)
        meta = docmode.sanitize_phone_meta({"book_id": "88", "attach": attach(files), **kw})
        return dict(meta, source="phone", status="stored")

    def test_sent_by_an_old_phone_with_the_letter_listed_third(self):
        self.assertIn("\n", attach())
        self.assertNotIn("\n", self.job()["attach"])
        self.assertTrue(docmode.letter_misplaced(self.job()))

    def test_an_updated_phone_already_sent_the_letter_first(self):
        job = self.job(letter_first="1")
        self.assertIs(job["letter_first"], True)
        self.assertFalse(docmode.letter_misplaced(job))

    def test_nothing_to_move(self):
        self.assertFalse(docmode.letter_misplaced(self.job([FILES[2], FILES[0]])))   # listed first
        self.assertFalse(docmode.letter_misplaced(self.job(FILES[:2])))             # no file named so
        self.assertFalse(docmode.letter_misplaced(self.job([])))                    # no attachments
        self.assertFalse(docmode.letter_misplaced(
            self.job([FILES[0], ("หนังสือราชการ", "https://spp/f/letter.docx")])))   # cannot lead
        self.assertFalse(docmode.letter_misplaced(dict(self.job(), source="web")))


class LetterBase(RefetchBase):
    """the news item above, sent by a phone from before the update or by an updated one"""

    def setUp(self):
        super().setUp()
        self.enclosure = text_pdf(os.path.join(self.work, "enclosure.pdf"),
                                  [["สิ่งที่ส่งมาด้วย รายละเอียดงบประมาณ"]])

    def send(self, first, extras=(), *, updated=False, book_id="88"):
        """what /api/phone/submit does with what the phone sent"""
        n = len(os.listdir(self.work))
        copies = [shutil.copy(p, os.path.join(self.work, f"sent{n}-{i}.pdf"))
                  for i, p in enumerate([first, *extras])]
        meta = {"book_id": book_id, "doc_no": "ศธ 04142/ว1234", "doc_title": "จัดสรรงบประมาณ",
                "doc_date": "30 ต.ค. 69", "sender": "สพป.", "emoji": "🔴", "attach": attach()}
        if updated:
            meta["letter_first"] = "1"
        job, created = docmode.start_from_phone_path("phone", copies[0], meta, extras=copies[1:])
        docmode._drive_tasks.join()
        return job, created

    def old_phone(self):
        return self.send(self.enclosure, [self.enclosure, self.letter, self.enclosure])

    def new_phone(self):
        return self.send(self.letter, [self.enclosure] * 3, updated=True)

    def first_page_text(self, job):
        with pymupdf.open(os.path.join(job["dir"], "doc.pdf")) as doc:
            return doc[0].get_text()


@unittest.skipIf(docmode is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class QueuedBeforeTheUpdateTests(LetterBase):
    def test_an_updated_phone_brings_it_again_into_the_same_queue_item(self):
        job = self.opened(self.old_phone()[0])
        self.assertIn("สิ่งที่ส่งมาด้วย", self.first_page_text(job))
        self.assertEqual(docmode.misplaced_ids(), {"88"})
        self.assertTrue(docmode._phone_row(job)["misplaced"])
        again, created = self.new_phone()
        self.assertTrue(created)
        self.assertIs(again, job)                             # the page left open still finds it
        self.assertEqual((job["status"], job["refetched"], job["letter_first"]), ("stored", True, True))
        self.assertEqual(docmode.misplaced_ids(), set())
        self.opened(job)
        self.assertIn("เรียน", self.first_page_text(job))
        self.assertEqual((job["main_pages"], job["total_pages"]), (2, 5))
        self.assertEqual(self.metas[job["drive_file_id"]]["letter_first"], True)   # survives a restart

    def test_an_old_phone_sending_it_again_changes_nothing(self):
        job = self.old_phone()[0]
        again, created = self.old_phone()
        self.assertFalse(created)                              # "อยู่ในคิวเดิม" as before
        self.assertIs(again, job)
        self.assertEqual(docmode.misplaced_ids(), {"88"})

    def test_documents_sent_by_an_updated_phone_are_left_alone(self):
        job = self.new_phone()[0]
        self.assertEqual(docmode.misplaced_ids(), set())
        again, created = self.new_phone()
        self.assertFalse(created)
        self.assertIs(again, job)

    def test_a_registered_document_being_redone_keeps_its_number(self):
        job = self.opened(self.old_phone()[0])
        self.save(job)
        self.assertEqual(docmode.misplaced_ids(), set())      # registered: left alone
        self.assertIs(docmode.reopen_phone("88", "๕๗"), job)  # ♻️ ลงรับใหม่
        self.assertEqual(docmode.misplaced_ids(), {"88"})
        self.new_phone()
        self.opened(job)
        self.assertIn("เรียน", self.first_page_text(job))

        def no_new_number(**kw):
            raise AssertionError("ลงรับใหม่ต้องไม่กินเลขใหม่")

        self.assertEqual(self.save(job, no_new_number)["receipt_no"], "๕๗")
        self.assertEqual(len(self.store.rows), 1)

    def test_restored_after_a_restart_with_the_mark(self):
        job = self.new_phone()[0]
        docmode._jobs.pop(job["id"])
        self.drive.queue_list.return_value = [{"id": fid, "meta": meta}
                                              for fid, meta in self.metas.items()]
        docmode._restore_from_drive()
        restored = docmode.phone_job("88")
        self.assertIsNot(restored, job)
        self.assertEqual((restored["status"], restored["letter_first"]), ("stored", True))
        self.assertEqual(docmode.misplaced_ids(), set())


@unittest.skipIf(webmain is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class WhatThePhoneIsToldTests(LetterBase):
    """/api/phone/history and the news list"""

    USERS = {"ครูเอ": {"status": "approved", "salt": "a"}}

    def setUp(self):
        super().setUp()
        p = patch.object(webmain, "load_users", return_value=self.USERS)
        p.start()
        self.addCleanup(p.stop)
        docmode._queue_restored.set()
        self.web = TestClient(webmain.app, cookies={
            "session": webmain.make_session("ครูเอ", self.USERS["ครูเอ"])})

    def history(self, updated):
        r = TestClient(webmain.app).get("/api/phone/history", headers={"X-Phone-Token": "tok"},
                                        params={"letter_first": "1"} if updated else None)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_only_an_updated_phone_is_sent_for_it(self):
        self.old_phone()
        old = self.history(updated=False)
        self.assertEqual((old["queued"], old["reorder"]), (["88"], []))
        new = self.history(updated=True)
        self.assertEqual((new["queued"], new["reorder"]), ([], ["88"]))
        self.assertNotIn("88", new["done"])
        doc = self.web.get("/api/spp/list").json()["days"][0]["docs"][0]
        self.assertTrue(doc["misplaced"])                      # the list says what to do
        self.new_phone()
        self.assertEqual(self.history(updated=True)["queued"], ["88"])
        self.assertFalse(self.web.get("/api/spp/list").json()["days"][0]["docs"][0]["misplaced"])

    def test_the_submit_form_carries_the_mark(self):
        files = [("file", ("doc.pdf", open(self.letter, "rb"), "application/pdf"))]
        self.addCleanup(files[0][1][1].close)
        r = TestClient(webmain.app).post("/api/phone/submit", headers={"X-Phone-Token": "tok"},
                                         files=files, data={"book_id": "90", "attach": attach(),
                                                            "letter_first": "1"})
        self.assertEqual(r.status_code, 200, r.text)
        docmode._drive_tasks.join()
        self.assertIs(docmode.phone_job("90")["letter_first"], True)
        self.assertEqual(docmode.misplaced_ids(), set())


if __name__ == "__main__":
    unittest.main()
