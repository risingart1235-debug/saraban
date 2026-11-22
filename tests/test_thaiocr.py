"""The "เรียน" line of a scanned letter is read on the server (OpenThai OCR), not taken from Gemini.

Most letters from สพป. are scans with no text in the file (11 of 12 sampled in Nov 2569), so the
recipient check used to depend on what Gemini said the "เรียน" line was.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import core
import thaiocr

try:
    import pymupdf
    from PIL import Image, ImageDraw
except ImportError:         # phone-only setup
    pymupdf = None


def line(row, x, text):
    return {"row": row, "text": text, "box": [[x, 0], [x + 100, 0], [x + 100, 30], [x, 30]], "confidence": 0.9}


class RecipientFromLinesTests(unittest.TestCase):
    def test_joins_the_pieces_of_its_row_left_to_right(self):
        lines = [line(5, 180, "เรื่อง"), line(5, 290, "ประชุม"),
                 line(6, 290, "ผู้อำนวยการสถานศึกษาในสังกัด"), line(6, 178, "เรียน"),
                 line(7, 290, "ด้วยสำนักงาน")]
        self.assertEqual(thaiocr.recipient_from_lines(lines), "เรียน ผู้อำนวยการสถานศึกษาในสังกัด")

    def test_a_stray_tone_mark_on_the_word_is_tolerated(self):
        lines = [line(6, 178, "เรี๊ยน"), line(6, 290, "ผู้อำนวยการโรงเรียนในสังกัด")]
        self.assertEqual(thaiocr.recipient_from_lines(lines), "เรียน ผู้อำนวยการโรงเรียนในสังกัด")
        self.assertEqual(core.classify_recipient(thaiocr.recipient_from_lines(lines)), "auto")

    def test_no_recipient_line(self):
        self.assertIsNone(thaiocr.recipient_from_lines([line(0, 10, "ที่ ศธ ๐๔๑๔๒/ว ๑")]))

    def test_no_model_means_no_answer_not_an_error(self):
        with patch.object(thaiocr, "_engine", None), patch.object(thaiocr, "_failed", False), \
                patch.object(thaiocr, "MODEL_DIR", "/nonexistent"):
            self.assertIsNone(thaiocr.read_recipient("whatever.pdf"))


@unittest.skipIf(pymupdf is None or not os.path.exists(os.path.join(thaiocr.MODEL_DIR, "rec.onnx")),
                 "OCR model not fetched (python thaiocr.py fetch)")
class ScannedLetterTests(unittest.TestCase):
    """a letter drawn as a picture (no text layer), like a scan from สพป."""

    def scanned(self, path, recipient):
        img = Image.new("RGB", (1654, 2339), "white")
        draw = ImageDraw.Draw(img)
        font = core.get_font(44)
        draw.text((180, 290), "ที่ ศธ ๐๔๑๔๒/ว ๔๔๙๙", font=font, fill="black")
        draw.text((180, 590), "เรื่อง   ขอความร่วมมือประชาสัมพันธ์", font=font, fill="black")
        draw.text((180, 660), "เรียน", font=font, fill="black")
        draw.text((290, 660), recipient, font=font, fill="black")
        draw.text((290, 740), "ด้วยสำนักงานเขตพื้นที่การศึกษาประถมศึกษาสกลนคร เขต ๑", font=font, fill="black")
        jpg = path + ".jpg"
        img.save(jpg, quality=85)
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, filename=jpg)
        doc.save(path)
        doc.close()
        return path

    def test_reads_the_recipient_of_a_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = self.scanned(os.path.join(tmp, "scan.pdf"), "ผู้อำนวยการโรงเรียนในสังกัดทุกโรงเรียน")
            self.assertIsNone(core.extract_recipient_line(pdf))           # nothing to read without OCR
            got = thaiocr.read_recipient(pdf)
            self.assertEqual(got.replace(" ", ""), "เรียนผู้อำนวยการโรงเรียนในสังกัดทุกโรงเรียน")
            self.assertEqual(core.classify_recipient(got), "auto")



try:
    from tests.test_redo import RedoBase, settle, docmode as _docmode
except ImportError:
    _docmode = None


@unittest.skipIf(_docmode is None or pymupdf is None, "server packages not installed")
class PrepareUsesOcrTests(RedoBase):
    """opening a scanned letter: the recipient check comes from the OCR, Gemini's reading is only the fallback"""

    def setUp(self):
        super().setUp()
        doc = pymupdf.open()                       # a page with no text layer at all
        doc.new_page(width=595, height=842)
        self.letter = os.path.join(self.work, "scan.pdf")
        doc.save(self.letter)
        doc.close()

    def opened(self):
        job = self.from_phone("77")
        self.assertTrue(_docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        return job

    def test_recipient_comes_from_the_ocr(self):
        with patch.object(thaiocr, "read_recipient", return_value="เรียน ผู้อำนวยการโรงเรียนในสังกัด") as ocr:
            job = self.opened()
        ocr.assert_called_once()
        self.assertEqual((job["recipient"], job["category"]), ("เรียน ผู้อำนวยการโรงเรียนในสังกัด", "auto"))

    def ai_calls(self):
        return _docmode.generate_kasien_text.call_count

    def test_addressed_to_all_schools_goes_to_the_ai(self):
        with patch.object(thaiocr, "read_recipient", return_value="เรียน ผู้อำนวยการโรงเรียนในสังกัดทุกโรงเรียน"):
            job = self.opened()
        self.assertEqual(self.ai_calls(), 1)
        self.assertFalse(job["ai_skipped"])

    def test_not_ours_is_not_sent_to_the_ai_until_asked(self):
        """addressed to one other school, our name nowhere in the file: no Gemini call, a person decides"""
        with patch.object(thaiocr, "read_recipient",
                          return_value="เรียน ผู้อำนวยการโรงเรียนบ้านหนองกอมป่าขาว"):
            job = self.opened()
            self.assertEqual(self.ai_calls(), 0)
            self.assertTrue(job["ai_skipped"])
            self.assertEqual(job["category"], "check")
            self.assertEqual(job["status"], "ready")          # still opens: pages, skip, or ask the AI
            _docmode.reread(job)                               # 🤖 ให้ AI อ่าน แล้วลงรับ
            self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(self.ai_calls(), 1)
        self.assertFalse(job["ai_skipped"])

    def test_our_name_in_the_attached_list_goes_to_the_ai(self):
        from tests.test_redo import text_pdf
        self.letter = text_pdf(os.path.join(self.work, "list.pdf"),
                               [["เรียน ผู้อำนวยการโรงเรียนตามบัญชีรายชื่อแนบท้าย"],
                                ["บัญชีรายชื่อโรงเรียน", "๑. " + core.SCHOOL]])
        job = self.opened()
        self.assertEqual(job["category"], "check")
        self.assertEqual(self.ai_calls(), 1)
        self.assertFalse(job["ai_skipped"])

    def test_ocr_finds_nothing_so_gemini_reading_is_used(self):
        with patch.object(thaiocr, "read_recipient", return_value=None):
            job = self.opened()
        self.assertEqual(job["category"], "unknown")    # the fake AI answers "-" for the recipient


if __name__ == "__main__":
    unittest.main()
