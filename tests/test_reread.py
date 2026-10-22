"""The editor's receipt number keeps up, and the AI can be asked to read a document again.

1. The number on the editor is the next number when the document was opened.  Leave it
   open, register another document, come back: that number is gone - saving takes the real
   next number anyway, but the screen still showed the old one and looked like a duplicate.
2. AI could not read (Google busy, model retired) - the kasien said so and there was no way
   to try again short of opening the document from scratch.  🤖 ให้ AI อ่านใหม่ reads the same
   file again: new kasien, signature page, recipient check and number.
Offline: the AI, the Sheet, Drive and LINE are fakes (see tests/test_redo.py).
"""
from __future__ import annotations

import os
import queue
import shutil
import unittest
from unittest.mock import patch

from tests.test_redo import RedoBase, settle, docmode, pymupdf, webmain

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

FAILED = ("เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ\n"
          "(AI ผิดพลาด: AI ของ Google ไม่ว่าง (คนใช้เยอะ) — รอสักครู่แล้วให้ AI อ่านใหม่)\nเพื่อโปรดทราบ")
GOOD = "เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ\nเชิญประชุมสวนพฤกษศาสตร์โรงเรียน\nเพื่อโปรดทราบ"


@unittest.skipIf(webmain is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class EditorTests(RedoBase):
    USERS = {"ครูเอ": {"status": "approved", "salt": "a"}}

    def setUp(self):
        super().setUp()
        self.answers = []

        def ai(pdf):
            text, fields = self.answers.pop(0) if self.answers else (GOOD, ("-",) * 4)
            return (text, 2) + tuple(fields) + ("-",)

        for p in (patch.object(docmode, "generate_kasien_text", side_effect=ai),
                  # the real next number from the Sheet, not a fixed one
                  patch.object(docmode, "get_next_receipt_no",
                               side_effect=lambda: self.store.peek_receipt_no()),
                  patch.object(webmain, "load_users", return_value=self.USERS)):
            p.start()
            self.addCleanup(p.stop)
        self.web = TestClient(webmain.app, cookies={
            "session": webmain.make_session("ครูเอ", self.USERS["ครูเอ"])})

    def opened(self, book_id="88"):
        job = self.from_phone(book_id)
        self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        return job

    # ---- ๑. เลขรับที่จะลง ----
    def test_number_moves_on_when_another_document_takes_it(self):
        job = self.opened("88")
        self.assertEqual(job["receipt_no"], "๕๗")
        before = job["stamp"]["png"]
        self.open_and_save(self.opened("89"))                     # registered meanwhile: takes ๕๗
        self.assertEqual(self.store.rows[0][0], "๕๗")
        r = self.web.get(f"/api/doc/{job['id']}/receipt")          # the editor comes back into view
        self.assertEqual(r.json(), {"receipt_no": "๕๘", "changed": True})
        self.assertNotEqual(job["stamp"]["png"], before)           # the stamp preview too
        self.assertEqual(self.web.get(f"/api/doc/{job['id']}/receipt").json()["changed"], False)
        self.assertEqual(self.open_and_save(job)["receipt_no"], "๕๘")

    def test_reopening_the_editor_shows_the_current_number(self):
        job = self.opened("88")
        self.open_and_save(self.opened("89"))
        d = self.web.get(f"/api/doc/{job['id']}").json()
        self.assertEqual(d["receipt_no"], "๕๘")

    def test_a_number_of_its_own_stays(self):
        job = self.registered("88")
        docmode.reopen_phone("88", "๕๗")                          # ♻️ ลงรับใหม่ (เลขเดิม)
        self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.store.register(doc_no="อื่น")                          # ๕๘ taken by something else
        self.assertFalse(docmode.refresh_receipt_no(job))
        self.assertEqual(job["receipt_no"], "๕๗")

    # ---- ๒. 🤖 ให้ AI อ่านใหม่ ----
    def test_read_again_after_the_ai_failed(self):
        self.answers = [(FAILED, ("-",) * 4)]
        job = self.opened("88")
        self.assertIn("AI ผิดพลาด:", job["boxes"][0]["text"])
        r = self.web.post(f"/api/doc/{job['id']}/reread")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(job["boxes"][0]["text"], GOOD)
        self.assertEqual(job["doc_no"], "ศธ 04142/ว1234")         # from the สพป. site, kept
        self.assertEqual(job["sig_page"], 2)

    def test_read_again_takes_a_number_that_moved_on_too(self):
        job = self.opened("88")
        self.open_and_save(self.opened("89"))
        self.web.post(f"/api/doc/{job['id']}/reread")
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(job["receipt_no"], "๕๘")

    def test_an_upload_gets_its_details_read_again(self):
        self.answers = [(FAILED, ("-",) * 4),
                        (GOOD, ("ศธ 04142/ว4028", "ประชุม", "21 ต.ค. 69", "สพป.สกลนคร เขต 1"))]
        src = shutil.copy(self.letter, os.path.join(self.work, "upload.pdf"))
        job = docmode.start_from_upload_path("ครูเอ", "upload.pdf", src)
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(job["doc_no"], "-")
        self.assertEqual(self.web.post(f"/api/doc/{job['id']}/reread").status_code, 200)
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual((job["doc_no"], job["sender"]), ("ศธ 04142/ว4028", "สพป.สกลนคร เขต 1"))

    def test_only_a_document_being_arranged(self):
        job = self.from_phone("88")                                  # not opened yet
        self.assertEqual(self.web.post(f"/api/doc/{job['id']}/reread").status_code, 409)
        done = self.registered("89")
        self.assertEqual(self.web.post(f"/api/doc/{done['id']}/reread").status_code, 409)

    def test_a_full_queue_leaves_it_as_it_was(self):
        job = self.opened("88")
        full = queue.Queue(maxsize=1)
        full.put(lambda: None)
        with patch.object(docmode, "_work_queue", full):
            r = self.web.post(f"/api/doc/{job['id']}/reread")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(job["status"], "ready")                    # still arranged and savable


if __name__ == "__main__":
    unittest.main()
