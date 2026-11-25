"""ตรวจบรรทัดเรียนผ่านแล้ว ส่ง AI เกษียณรอไว้เลย หลายเรื่องพร้อมกัน — กดเปิดลงรับไม่ต้องรอ AI"""
import os
import threading
import time
import unittest
from unittest.mock import patch

try:
    import pymupdf
except ImportError:
    pymupdf = None

try:
    import thaiocr
    from tests.test_redo import RedoBase, settle, docmode as _docmode
except ImportError:
    _docmode = None


@unittest.skipIf(_docmode is None or pymupdf is None, "server packages not installed")
class AiAheadTests(RedoBase):
    def setUp(self):
        super().setUp()
        doc = pymupdf.open()
        doc.new_page(width=595, height=842)
        self.letter = os.path.join(self.work, "scan.pdf")
        doc.save(self.letter)
        doc.close()
        for p in (patch.object(_docmode.core, "GEMINI_API_KEY", "key"),
                  patch.object(_docmode, "_ai_pause_until", [0.0])):
            p.start()
            self.addCleanup(p.stop)

    def calls(self):
        return _docmode.generate_kasien_text.call_count

    def sent(self, book_id, recipient="เรียน ผู้อำนวยการโรงเรียนในสังกัดทุกแห่ง"):
        with patch.object(thaiocr, "read_recipient", return_value=recipient):
            job = self.from_phone(book_id)
            _docmode._screen_tasks.join()
        _docmode._ai_tasks.join()
        _docmode._drive_tasks.join()
        return job

    def test_passing_letter_is_read_by_the_ai_before_anyone_opens_it(self):
        job = self.sent("71")
        self.assertEqual(self.calls(), 1)
        self.assertEqual(job["status"], "stored")
        self.assertEqual(_docmode.ai_state(job), "done")
        self.assertEqual(job["ai"][0], self.kasien[0])
        self.assertEqual(self.drive.queue_update_meta.call_args_list[-1].args[1]["ai"][0], self.kasien[0])
        self.assertTrue(_docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(self.calls(), 1)                 # opening used what was read ahead
        self.assertEqual(job["boxes"][0]["text"], self.kasien[0])

    def test_not_ours_is_not_sent_ahead(self):
        job = self.sent("72", "เรียน ผู้อำนวยการโรงเรียนบ้านหนองกอมป่าขาว")
        self.assertEqual(self.calls(), 0)
        self.assertEqual(_docmode.ai_state(job), "")

    def test_reread_asks_the_ai_again(self):
        job = self.sent("73")
        self.assertTrue(_docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        _docmode.reread(job)
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(self.calls(), 2)

    def test_failed_reading_is_not_kept_and_opening_reads_again(self):
        fail = ("เรียน ผอ.\n(" + _docmode.core.AI_FAILED + " โควตาหมด)\nเพื่อโปรดทราบ", 1, "-", "-", "-", "-", "-")
        good = _docmode.generate_kasien_text.side_effect
        _docmode.generate_kasien_text.side_effect = [fail, good(None)]
        job = self.sent("74")
        self.assertNotIn("ai", [k for k, v in job.items() if v])
        self.assertTrue(_docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(self.calls(), 2)
        self.assertEqual(job["boxes"][0]["text"], self.kasien[0])

    def test_several_letters_are_read_at_the_same_time(self):
        live, peak, lock = [0], [0], threading.Lock()
        answer = _docmode.generate_kasien_text.side_effect

        def slow(pdf):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.3)
            with lock:
                live[0] -= 1
            return answer(pdf)

        _docmode.generate_kasien_text.side_effect = slow
        with patch.object(thaiocr, "read_recipient", return_value="เรียน ผู้อำนวยการโรงเรียนในสังกัด"):
            jobs = [self.from_phone(str(80 + i)) for i in range(4)]
            _docmode._screen_tasks.join()
        _docmode._ai_tasks.join()
        self.assertEqual(self.calls(), 4)
        self.assertGreater(peak[0], 1)
        self.assertTrue(all(_docmode.ai_state(j) == "done" for j in jobs))

    def test_opening_while_the_ai_is_reading_waits_instead_of_asking_twice(self):
        started, release = threading.Event(), threading.Event()
        answer = _docmode.generate_kasien_text.side_effect

        def held(pdf):
            started.set()
            release.wait(10)
            return answer(pdf)

        _docmode.generate_kasien_text.side_effect = held
        with patch.object(thaiocr, "read_recipient", return_value="เรียน ผู้อำนวยการโรงเรียนในสังกัด"):
            job = self.from_phone("90")
            _docmode._screen_tasks.join()
        self.assertTrue(started.wait(10))
        self.assertEqual(_docmode.ai_state(job), "reading")
        self.assertTrue(_docmode.prepare_stored(job))
        time.sleep(0.3)
        release.set()
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(self.calls(), 1)
        self.assertEqual(job["boxes"][0]["text"], self.kasien[0])

    def test_kept_reading_comes_back_after_a_restart(self):
        job = self.sent("75")
        fid = job["drive_file_id"]
        meta = dict(self.metas[fid], ai=job["ai"])
        _docmode._jobs.pop(job["id"])
        self.drive.queue_list.return_value = [{"id": fid, "meta": meta}]
        with patch.object(_docmode, "_durable_phone_record", return_value=None):
            _docmode._restore_from_drive()
        back = _docmode.phone_job("75")
        self.assertEqual(_docmode.ai_state(back), "done")
        self.assertTrue(_docmode.prepare_stored(back))
        self.assertEqual(settle(back), "ready", back.get("error"))
        self.assertEqual(self.calls(), 1)


if __name__ == "__main__":
    unittest.main()
