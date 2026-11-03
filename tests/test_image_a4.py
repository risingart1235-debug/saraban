"""A picture imported in mode ๒ becomes an A4 page, so the stamp and the kasien keep their size.

The stamp and the kasien are drawn at their real size on A4 paper (46-pixel letters at 200 DPI).
A JPG/PNG used to become a page as big as its pixels: a phone photo 3024 pixels wide made a page
38 cm wide on the web (saved at 200 DPI) and over a metre on the desktop (72 DPI) - the stamp
and the kasien came out tiny, and a small picture made them overflow the page.  Now every
imported picture is put on an A4 page (landscape for a landscape picture), scaled to fit without
stretching, turned upright by its EXIF orientation first.  PDF files keep their own pages.
Offline: pictures are drawn on the spot; the AI, the Sheet, Drive and LINE are fakes.
"""
from __future__ import annotations

import glob
import os
import shutil
import tempfile
import unittest

from PIL import Image, ImageDraw

import core
from tests.test_redo import RedoBase, settle, docmode, pymupdf

A4_MM = (210, 297)
A4_PX = (1654, 2339)            # A4 at 200 DPI, as the stamp mode (โหมด ๓) draws it


def page_mm(pdf):
    with pymupdf.open(pdf) as doc:
        r = doc[0].rect
        return doc.page_count, r.width / 72 * 25.4, r.height / 72 * 25.4


def dark_box(img):
    """where the dark marks are on a rendered page"""
    return img.convert("L").point(lambda v: 255 if v < 128 else 0).getbbox()


def document_photo(size=(3024, 4032)):
    """a white sheet with a black block near its top left corner"""
    img = Image.new("RGB", size, "white")
    w, h = size
    ImageDraw.Draw(img).rectangle([w // 10, h // 20, w // 3, h // 10], fill="black")
    return img


@unittest.skipIf(pymupdf is None, "ต้องลง pymupdf ก่อน (pip install -r requirements.txt)")
class ImageToA4Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def convert(self, img, name="photo.jpg", **save):
        src = os.path.join(self.dir, name)
        img.save(src, **save)
        return core.image_to_a4_pdf(src, os.path.join(self.dir, name + ".pdf"))

    def assertA4(self, pdf, landscape=False):
        pages, w, h = page_mm(pdf)
        self.assertEqual(pages, 1)
        want = A4_MM[::-1] if landscape else A4_MM
        self.assertAlmostEqual(w, want[0], delta=0.5)
        self.assertAlmostEqual(h, want[1], delta=0.5)
        self.assertEqual(core.render_pdf_page(pdf, 1, 200).size, A4_PX[::-1] if landscape else A4_PX)

    def test_a_phone_photo_becomes_an_a4_page(self):
        self.assertA4(self.convert(document_photo((3024, 4032))))       # used to be 38 x 51 cm

    def test_a_small_picture_is_enlarged_to_a4_too(self):
        self.assertA4(self.convert(document_photo((600, 850)), "screenshot.png"))

    def test_a_landscape_picture_gets_a_landscape_page(self):
        self.assertA4(self.convert(document_photo((4000, 2800))), landscape=True)

    def test_not_stretched_the_rest_is_white_paper(self):
        pdf = self.convert(Image.new("RGB", (1200, 1200), (90, 90, 90)), "square.png")
        page = core.render_pdf_page(pdf, 1, 200)
        top = (A4_PX[1] - A4_PX[0]) // 2             # a square fills the width, centred top to bottom
        self.assertEqual(dark_box(page), (0, top, A4_PX[0], top + A4_PX[0]))
        self.assertEqual(page.getpixel((800, top // 2)), (255, 255, 255))

    def test_a_photo_stored_on_its_side_is_turned_upright(self):
        upright = self.convert(document_photo(), "upright.jpg", quality=90)
        exif = Image.Exif()
        exif[0x0112] = 6                                   # "turn 90° clockwise to show"
        sideways = self.convert(document_photo().transpose(Image.ROTATE_90), "sideways.jpg",
                                quality=90, exif=exif.tobytes())
        self.assertA4(sideways)                            # portrait, not a landscape page lying down
        want = dark_box(core.render_pdf_page(upright, 1, 200))
        got = dark_box(core.render_pdf_page(sideways, 1, 200))
        self.assertLess(want[2], A4_PX[0] / 2)             # the block is at the top left...
        for a, b in zip(got, want):                        # ...on both
            self.assertAlmostEqual(a, b, delta=3)

    def test_see_through_parts_become_white_paper(self):
        img = Image.new("RGBA", (1000, 1400), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle([100, 100, 400, 200], fill=(0, 0, 0, 255))
        page = core.render_pdf_page(self.convert(img, "cutout.png"), 1, 200)
        self.assertEqual(page.getpixel((20, 20)), (255, 255, 255))     # used to turn black
        self.assertIsNotNone(dark_box(page))


@unittest.skipIf(docmode is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class UploadTests(RedoBase):
    def uploaded(self, path, name):
        src = shutil.copy(path, os.path.join(self.work, "incoming"))
        job = docmode.start_from_upload_path("ครูเอ", name, src)
        self.assertEqual(settle(job), "ready", job.get("error"))
        return job

    def test_a_photo_of_a_letter_is_arranged_and_saved_on_a4(self):
        zoom = 3024 / 595                                  # page 1 of the letter, photographed
        with pymupdf.open(self.letter) as doc:
            pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        photo = os.path.join(self.work, "image.jpg")
        Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(photo, quality=90)

        job = self.uploaded(photo, "image.jpg")
        letter = self.uploaded(self.letter, "letter.pdf")
        page = job["pages"][0]
        self.assertEqual((page["w"], page["h"]), A4_PX)    # used to be 3024 x 4280
        # the stamp takes the same share of the page as on a PDF letter
        self.assertAlmostEqual(job["stamp"]["w"] / page["w"],
                               letter["stamp"]["w"] / letter["pages"][0]["w"], places=2)

        self.open_and_save(job)
        saved = glob.glob(os.path.join(self.work, "out", "**", "*.pdf"), recursive=True)
        self.assertEqual(len(saved), 1)
        pages, w, h = page_mm(saved[0])
        self.assertAlmostEqual(w, A4_MM[0], delta=0.5)
        self.assertAlmostEqual(h, A4_MM[1], delta=0.5)

    def test_a_pdf_keeps_its_own_page_size(self):
        letter_size = os.path.join(self.work, "us_letter.pdf")
        with pymupdf.open() as doc:
            doc.new_page(width=612, height=792)
            doc.save(letter_size)
        page = self.uploaded(letter_size, "us_letter.pdf")["pages"][0]
        self.assertEqual((page["w"], page["h"]), (1700, 2200))


if __name__ == "__main__":
    unittest.main()
