import os
import tempfile
import unittest

import pymupdf
from PIL import Image

import core


class ReceiptReservationTests(unittest.TestCase):
    def test_retry_reuses_reserved_number(self):
        state = {}
        calls = []

        def register(**fields):
            calls.append(fields)
            return "๑๒๓"

        first = core.reserve_receipt_once(
            state, register, doc_no="ศธ 1", receive_date="๑/๑/๒๕๖๙")
        second = core.reserve_receipt_once(
            state, register, doc_no="ข้อมูลที่ต้องไม่ถูกเขียนซ้ำ")

        self.assertEqual(first, "๑๒๓")
        self.assertEqual(second, "๑๒๓")
        self.assertEqual(state["reserved_receipt_no"], "๑๒๓")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["doc_no"], "ศธ 1")

    def test_failed_reservation_does_not_create_checkpoint(self):
        state = {}

        def register(**_fields):
            raise RuntimeError("store unavailable")

        with self.assertRaisesRegex(RuntimeError, "store unavailable"):
            core.reserve_receipt_once(state, register)

        self.assertNotIn("reserved_receipt_no", state)


class PdfRenderingTests(unittest.TestCase):
    def test_a4_page_renders_at_explicit_200_dpi(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "a4.pdf")
            document = pymupdf.open()
            document.new_page(width=595.2756, height=841.8898)
            document.save(path)
            document.close()

            image = core.render_pdf_page(path, page_number=1, dpi=200)

        self.assertEqual(image.size, (1654, 2339))
        self.assertEqual(image.mode, "RGB")

    def test_200_dpi_image_saves_as_a4_sized_pdf(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "saved-a4.pdf")
            image = Image.new("RGB", (1654, 2339), "white")
            core.save_image_as_pdf(image, path, dpi=200)

            document = pymupdf.open(path)
            try:
                page_size = document[0].rect
                self.assertAlmostEqual(page_size.width, 595.44, places=1)
                self.assertAlmostEqual(page_size.height, 842.04, places=1)
            finally:
                document.close()

    def test_page_number_is_one_based_and_checked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "one-page.pdf")
            document = pymupdf.open()
            document.new_page()
            document.save(path)
            document.close()

            with self.assertRaisesRegex(ValueError, "start at 1"):
                core.render_pdf_page(path, page_number=0)
            with self.assertRaisesRegex(IndexError, "1 page"):
                core.render_pdf_page(path, page_number=2)


if __name__ == "__main__":
    unittest.main()
