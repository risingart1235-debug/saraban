"""Where the endorsement (คำเกษียณ) goes: next to the sender's signature, never at the end.

The endorsement used to be searched for only in a fixed left strip of the signature
page.  When the strip had no tall gap, the web app threw it onto a blank page appended
after the last page - behind every attachment, pages away from the signature it
belongs to - and when it did "fit" it could sit half on top of "ขอแสดงความนับถือ".
Now the whole signature page is searched for real white space nearest the signature
block, the text is shrunk (down to 70%) before giving up, and the blank page, when it
is still needed, sits right after the signature page in the saved PDF.
Offline: letters are drawn on the spot, the AI / Drive / LINE are fakes.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

import core

try:
    import pymupdf          # noqa: F401 - the web app renders PDF pages with it
except ImportError:         # phone-only setup
    pymupdf = None

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    webmain = None

W, H = 1654, 2339                     # A4 at 200 DPI
CM = 200 / 2.54
FONT = core.get_font(44)              # 16 pt, like the letters
TEXT = ("เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ\n"
        "สพป.สกลนคร เขต ๑ แจ้งแนวทางการดำเนินงานโครงการอาหารกลางวัน ปีงบประมาณ ๒๕๗๐\n"
        "เพื่อโปรดทราบ")
WORDS = core.cached_tokenize(
    "ด้วยสำนักงานเขตพื้นที่การศึกษาประถมศึกษาสกลนคร เขต ๑ ได้รับแจ้งจากสำนักงานคณะกรรมการ"
    "การศึกษาขั้นพื้นฐานว่า ได้กำหนดแนวทางการดำเนินงานโครงการอาหารกลางวันในโรงเรียน "
    "ประจำปีงบประมาณ พ.ศ. ๒๕๗๐ เพื่อให้สถานศึกษาในสังกัดถือปฏิบัติได้อย่างถูกต้อง ")


def blank_page():
    im = Image.new("RGB", (W, H), "white")
    return im, ImageDraw.Draw(im)


def write(d, x, y, s):
    d.text((x, y), s, font=FONT, fill="black")


def body(d, y, n_lines):
    """full-width lines between the 3 cm left and 2 cm right margins"""
    k = 0
    for _ in range(n_lines):
        line = ""
        while core.get_text_w(line + WORDS[k % len(WORDS)], FONT) < W - 5 * CM:
            line += WORDS[k % len(WORDS)]
            k += 1
        write(d, 3 * CM, y, line)
        y += 60
    return y


def closing(d, y):
    """ขอแสดงความนับถือ at 10 cm; signature, name and title centred at 12 cm, as the
    official format has it.  Returns (left, top, right, bottom) of the signature block."""
    top = y
    write(d, 10 * CM, y, "ขอแสดงความนับถือ")
    y += 60
    cx = 12 * CM
    d.line([(cx - 2 * CM + i * 9, y + 45 + (28 if (i // 2) % 2 else -28)) for i in range(36)],
           fill=(25, 35, 120), width=6)
    y += int(1.4 * CM)
    left = 10 * CM
    for s in ("(นายสมชาย ใจดี)", "ผู้อำนวยการสำนักงานเขตพื้นที่การศึกษา", "ประถมศึกษาสกลนคร เขต ๑"):
        w = core.get_text_w(s, FONT)
        write(d, cx - w / 2, y, s)
        left = min(left, cx - w / 2)
        y += 60
    return (left, top, cx + 2.5 * CM, y)


def signature_page(body_lines):
    """page 2 of a letter: the end of the text, the signature block, the contact lines
    bottom left.  Returns (page, signature block)."""
    im, d = blank_page()
    y = body(d, 230, body_lines)
    write(d, 5.5 * CM, y + 20, "จึงเรียนมาเพื่อโปรดทราบ")
    block = closing(d, y + 110)
    y = max(block[3] + 40, H - 4.6 * CM)
    for s in ("กลุ่มส่งเสริมการจัดการศึกษา", "โทร. ๐ ๔๒๗๑ ๑๒๓๔ ต่อ ๑๒"):
        write(d, 3 * CM, y, s)
        y += 60
    return im, block


def full_page():
    im, d = blank_page()
    for y in range(100, H - 60, 50):
        write(d, 60, y, "ข้อความเต็มหน้า " * 12)
    return im


def marked_page(colour):
    """an attachment page, told apart in the saved PDF by a coloured square"""
    im, d = blank_page()
    body(d, 300, 25)
    d.rectangle([60, 60, 260, 260], fill=colour)
    return im


def kasien_box(spot, page_w=W, text=TEXT):
    x, y, size, wrap = spot
    img = core.render_transparent_kasien(text, int(page_w * 0.42 * wrap / 100), size)
    x0, y0, x1, y1 = img.getchannel("A").getbbox()
    return img, (x + x0, y + y0, x + x1, y + y1)


def ink_in(page, rect, margin=0.2 * CM):
    """dark pixels of the page inside rect (+ a 2 mm margin)"""
    x0, y0, x1, y1 = rect
    crop = page.convert("L").crop((int(x0 - margin), int(y0 - margin),
                                   int(x1 + margin), int(y1 + margin)))
    return sum(crop.histogram()[:180])


def gap_cm(a, b):
    dx = max(0, a[0] - b[2], b[0] - a[2])
    dy = max(0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy) / CM


class PlaceKasienTests(unittest.TestCase):
    def test_lands_next_to_the_signature_on_the_same_page_and_covers_no_text(self):
        # 14 lines: room under the signature / 19: the old search put it across
        # "ขอแสดงความนับถือ" and the signature
        for lines in (14, 19):
            with self.subTest(body_lines=lines):
                page, block = signature_page(lines)
                spot = core.place_kasien(page, TEXT)
                self.assertIsNotNone(spot)
                self.assertEqual(spot[2:], (100, 100))            # no need to shrink
                _, ink = kasien_box(spot)
                self.assertEqual(ink_in(page, ink), 0)
                self.assertLess(gap_cm(ink, block), 1.5)

    def test_tucks_in_beside_the_signature_with_a_narrower_box(self):
        im, d = blank_page()
        y = body(d, 230, 27)                     # the signature block reaches the bottom
        block = closing(d, y + 30)
        spot = core.place_kasien(im, TEXT)
        self.assertIsNotNone(spot)
        self.assertEqual(spot[2], 100)
        self.assertLess(spot[3], 100)            # narrower, not smaller
        _, ink = kasien_box(spot)
        self.assertEqual(ink_in(im, ink), 0)
        self.assertLess(ink[2], block[0])        # left of ขอแสดงความนับถือ ...
        self.assertLess(ink[1], block[3])        # ... at the same height

    def test_shrinks_rather_than_leave_the_page(self):
        im = full_page()
        # a white hole just too narrow for the endorsement at 100% however it is wrapped
        narrowest = min(r[2] - r[0] for r in (kasien_box((0, 0, 100, w))[1] for w in (70, 100, 150)))
        hole = (3 * CM, H - 9 * CM, 3 * CM + narrowest + 0.6 * CM - 4, H - 4 * CM)
        ImageDraw.Draw(im).rectangle([int(v) for v in hole], fill="white")
        spot = core.place_kasien(im, TEXT)
        self.assertIsNotNone(spot)
        self.assertLess(spot[2], 100)
        self.assertGreaterEqual(spot[2], core.KASIEN_MIN_SIZE)
        self.assertEqual(ink_in(im, kasien_box(spot)[1]), 0)

    def test_no_white_space_anywhere_means_none(self):
        self.assertIsNone(core.place_kasien(full_page(), TEXT))

    def test_first_page_keeps_clear_of_the_letterhead_and_the_stamp(self):
        im, _ = blank_page()
        x, y, _, _ = core.place_kasien(im, TEXT, first_page=True)
        self.assertGreaterEqual(y, H * 0.40)

    def test_leaves_room_for_the_clerks_signature_under_it(self):
        page, block = signature_page(14)
        below = (315, 190)                       # 4 cm signature + the date under it
        spot = core.place_kasien(page, TEXT, below=below)
        img, ink = kasien_box(spot)
        x, y = spot[:2]
        sx, sy = x + (img.width - below[0]) // 2, y + img.height + round(0.2 * CM)
        self.assertEqual(ink_in(page, ink), 0)
        self.assertEqual(ink_in(page, (sx, sy, sx + below[0], sy + below[1])), 0)

    def test_keeps_off_what_is_already_placed_on_the_page(self):
        page, _ = signature_page(14)
        first = kasien_box(core.place_kasien(page, TEXT))[1]
        spot = core.place_kasien(page, TEXT, avoid=[first])
        self.assertIsNotNone(spot)
        self.assertGreater(gap_cm(kasien_box(spot)[1], first), 0)

    def test_clear_at_tells_free_spots_from_text_and_the_page_edge(self):
        page, block = signature_page(14)
        x, y, size, wrap = core.place_kasien(page, TEXT)
        self.assertTrue(core.kasien_clear_at(page, TEXT, x, y))
        self.assertFalse(core.kasien_clear_at(page, TEXT, 3 * CM, 5 * CM))          # on the body
        self.assertFalse(core.kasien_clear_at(page, TEXT, block[0], block[1]))     # on the signature
        self.assertFalse(core.kasien_clear_at(page, TEXT, W - 100, y))              # off the page
        # the clerk's signature under it lands on the contact lines
        self.assertFalse(core.kasien_clear_at(page, TEXT, x, y, below=(315, 400)))

    def test_keeps_the_size_the_user_chose_and_never_enlarges_it(self):
        page, _ = signature_page(4)              # plenty of room
        self.assertEqual(core.place_kasien(page, TEXT, size_pct=120, wrap_pct=110)[2:], (120, 110))
        self.assertEqual(core.place_kasien(page, TEXT, size_pct=80)[2], 80)
        self.assertEqual(core.place_kasien(page, TEXT, size_pct=50)[2], 50)


class PageOrderTests(unittest.TestCase):
    """the blank page (total+1) is filed right after the signature page"""

    def test_blank_page_follows_the_signature_page(self):
        self.assertEqual(core.page_order(4, 2), [1, 2, 5, 3, 4])
        self.assertEqual(core.page_order(4, 4), [1, 2, 3, 4, 5])
        self.assertEqual(core.page_order(4, 9), [1, 2, 3, 4, 5])
        self.assertEqual(core.page_order(4, None), [1, 2, 3, 4])

    def test_line_pages_count_in_file_order(self):
        # endorsement on the blank page after page 2: send 1, 2 and the blank page,
        # not the attachments that come before it in page numbers
        self.assertEqual(core.auto_line_pages({1, 5}, core.page_order(4, 2)), [1, 2, 5])
        self.assertEqual(core.auto_line_pages({1, 2}, core.page_order(4, None)), [1, 2])
        self.assertEqual(core.auto_line_pages({1, 7}, core.page_order(6, 5)), [1, 4, 5, 7])
        self.assertEqual(core.auto_line_pages(set(), core.page_order(3, None)), [1])

    def test_ticked_pages_come_out_in_file_order(self):
        self.assertEqual(core.chosen_line_pages([5, 3, 1], 4, True, core.page_order(4, 2)), [1, 5, 3])
        self.assertEqual(core.chosen_line_pages([5, 3, 1], 4, True), [1, 3, 5])   # no order: by number


TINT = {3: (220, 30, 30), 4: (30, 160, 30)}


def make_pdf(path, sig_page):
    pages = [signature_page(30)[0], sig_page, marked_page(TINT[3]), marked_page(TINT[4])]
    pages[0].save(path, "PDF", resolution=200.0, save_all=True, append_images=pages[1:])


def tint_of(img):
    r, g, b = img.convert("RGB").crop((20, 20, 60, 60)).resize((1, 1)).getpixel((0, 0))
    return next((n for n, c in TINT.items() if abs(r - c[0]) + abs(g - c[1]) + abs(b - c[2]) < 90), None)


@unittest.skipIf(pymupdf is None, "ต้องลง pymupdf ก่อน (pip install -r requirements.txt)")
class WebPrepareAndSaveTests(unittest.TestCase):
    """the web app: where the box is put, and the page order of the saved PDF"""

    def setUp(self):
        from web import docmode
        self.docmode = docmode
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        ai = (TEXT, 2, "ศธ 04137/ว1234", "อาหารกลางวัน", "15 มกราคม 2570", "สพป.สกลนคร เขต 1", "-")
        for p in (patch.object(docmode, "generate_kasien_text", return_value=ai),
                  patch.object(docmode, "get_next_receipt_no", return_value="๕๗"),
                  patch.object(core, "OUTPUT_ROOT", os.path.join(self.work, "out"))):
            p.start()
            self.addCleanup(p.stop)

    def prepared(self, sig_page):
        pdf = os.path.join(self.work, "doc.pdf")
        make_pdf(pdf, sig_page)
        job = {"id": "j", "user": "ครูเอ", "source": "upload", "status": "analyzing", "dir": self.work}
        self.docmode._prepare(job, pdf, doc_no="-", doc_title="-", doc_date="-", sender="-",
                              emoji="📄", attach="", book_id=None)
        self.assertEqual(job["status"], "ready")
        return job

    def saved(self, job, line_pages=None):
        sent = MagicMock(return_value=True)
        drive = MagicMock()
        drive.upload.return_value = {}
        payload = {"stamp": job["stamp"], "boxes": job["boxes"], "date": "", "time": ""}
        if line_pages is not None:
            payload["line_pages"] = line_pages
        with patch.dict(sys.modules, {"drive": drive}), \
                patch("store.get_store", return_value=MagicMock()), \
                patch.object(self.docmode, "publish_line_image",
                             side_effect=lambda p, day=None: "https://x/" + os.path.basename(p)), \
                patch.object(self.docmode, "send_line_with_image", sent):
            result = self.docmode._finalize_claimed(job, payload, lambda **kw: "๕๗")
        out = os.path.join(core.OUTPUT_ROOT, core.day_folder(), result["filename"])
        return out, [u.rsplit("/", 1)[1] for u in sent.call_args.args[1]]

    def pages_of(self, pdf):
        n = len(core.PdfReader(pdf).pages)
        return [core.render_pdf_page(pdf, i + 1, 50) for i in range(n)]

    def test_endorsement_goes_on_the_signature_page_when_there_is_room(self):
        job = self.prepared(signature_page(14)[0])
        box = job["boxes"][0]
        self.assertEqual(job["pages"][box["page"]]["index"], 2)
        self.assertTrue(box["fitted"])
        out, sent = self.saved(job)
        self.assertEqual(len(core.PdfReader(out).pages), 4)             # no blank page added
        self.assertEqual([s.rsplit("_", 1)[1] for s in sent], ["หน้า๑.jpg", "หน้า๒.jpg"])

    def test_full_signature_page_puts_the_blank_page_right_after_it(self):
        job = self.prepared(full_page())
        box = job["boxes"][0]
        self.assertEqual(box["page"], len(job["pages"]))                 # the blank page
        self.assertFalse(box["fitted"])
        out, sent = self.saved(job)
        pages = self.pages_of(out)
        self.assertEqual(len(pages), 5)
        self.assertEqual([tint_of(p) for p in pages], [None, None, None, 3, 4])
        colours = [c for _, c in pages[2].convert("RGB").getcolors(1 << 16)]
        self.assertTrue(any(b > r + 60 for r, g, b in colours))           # the blue endorsement
        # LINE: pages 1, 2 and the blank page, named by where they are in the file
        self.assertEqual([s.rsplit("_", 1)[1] for s in sent], ["หน้า๑.jpg", "หน้า๒.jpg", "หน้า๓.jpg"])

    def test_pages_ticked_on_screen_are_sent_in_file_order(self):
        job = self.prepared(full_page())
        _, sent = self.saved(job, line_pages=[5, 3, 1])
        self.assertEqual([s.rsplit("_", 1)[1] for s in sent], ["หน้า๑.jpg", "หน้า๓.jpg", "หน้า๔.jpg"])

    def test_pin_button_brings_a_moved_box_back_next_to_the_signature(self):
        sig, block = signature_page(14)
        job = self.prepared(sig)
        box = dict(job["boxes"][0], page=len(job["pages"]), left_cm=2, top_cm=3)   # on the blank page
        r = self.docmode.fit_box(job, {"page": box["page"], "box": box, "sig": {"w": 315, "h": 190}})
        self.assertTrue(r["fitted"])
        self.assertEqual(job["pages"][r["page"]]["index"], 2)
        spot = (r["left_cm"] * CM, r["top_cm"] * CM, r["size_pct"], r["wrap_pct"])
        _, ink = kasien_box(spot)
        self.assertEqual(ink_in(sig, ink, margin=0.1 * CM), 0)
        self.assertLess(gap_cm(ink, block), 2.5)

    def test_pin_button_with_no_room_says_so_and_uses_the_blank_page(self):
        job = self.prepared(full_page())
        r = self.docmode.fit_box(job, {"page": 1, "box": job["boxes"][0]})
        self.assertFalse(r["fitted"])
        self.assertEqual(r["page"], len(job["pages"]))

    def test_inserting_the_signature_moves_nothing_when_there_is_room_under_the_endorsement(self):
        job = self.prepared(signature_page(4)[0])            # lots of room
        box = job["boxes"][0]
        r = self.docmode.fit_box(job, {"page": box["page"], "box": box, "keep": True,
                                       "sig": {"w": 315, "h": 190}})
        self.assertEqual((r["page"], r["left_cm"], r["top_cm"]), (box["page"], box["left_cm"], box["top_cm"]))

    def test_inserting_the_signature_over_text_moves_both_to_free_space(self):
        sig, _ = signature_page(14)
        job = self.prepared(sig)
        box = job["boxes"][0]
        tall = {"w": 315, "h": 400}                         # would cover the contact lines
        r = self.docmode.fit_box(job, {"page": box["page"], "box": box, "keep": True, "sig": tall})
        self.assertNotEqual((r["left_cm"], r["top_cm"]), (box["left_cm"], box["top_cm"]))
        self.assertEqual(r["page"], box["page"])            # still on the signature page
        self.assertTrue(core.kasien_clear_at(sig, TEXT, r["left_cm"] * CM, r["top_cm"] * CM,
                                             size_pct=r["size_pct"], wrap_pct=r["wrap_pct"],
                                             below=(315, 400)))

    def test_other_boxes_are_only_avoided_on_their_own_page(self):
        sig, _ = signature_page(14)
        job = self.prepared(sig)
        box = job["boxes"][0]
        x, y = box["left_cm"], box["top_cm"]
        here = {"left_cm": x, "top_cm": y, "w": box["w"], "h": box["h"]}
        r = self.docmode.fit_box(job, {"page": box["page"], "box": box,
                                       "avoid": [dict(here, page=0)]})    # on page 1: no effect
        self.assertEqual((r["left_cm"], r["top_cm"]), (x, y))
        r = self.docmode.fit_box(job, {"page": box["page"], "box": box,
                                       "avoid": [dict(here, page=box["page"])]})
        self.assertNotEqual((r["left_cm"], r["top_cm"]), (x, y))


@unittest.skipIf(webmain is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class FitEndpointTests(unittest.TestCase):
    def test_only_the_owner_of_the_job_can_use_it(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        pdf = os.path.join(work, "doc.pdf")
        make_pdf(pdf, signature_page(14)[0])
        users = {"ครูเอ": {"status": "approved", "salt": "a"}, "bee": {"status": "approved", "salt": "b"}}
        job = {"id": "j", "user": "ครูเอ", "source": "upload", "status": "ready", "pdf_path": pdf,
               "sig_page": 2, "pages": [{"index": n, "w": W, "h": H} for n in (1, 2, 3, 4)]}
        with patch.object(webmain, "load_users", return_value=users), \
                patch.object(webmain.docmode, "_jobs", {"j": job}):
            a = TestClient(webmain.app, cookies={"session": webmain.make_session("ครูเอ", users["ครูเอ"])})
            b = TestClient(webmain.app, cookies={"session": webmain.make_session("bee", users["bee"])})
            body_ = {"page": 4, "box": {"text": TEXT, "size_pct": 100, "wrap_pct": 100, "indent_pct": 100}}
            r = a.post("/api/doc/j/fit", json=body_)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["page"], r.json()["fitted"]), (1, True))
            self.assertTrue(r.json()["png"].startswith("data:image/png;base64,"))
            self.assertEqual(b.post("/api/doc/j/fit", json=body_).status_code, 404)


if __name__ == "__main__":
    unittest.main()
