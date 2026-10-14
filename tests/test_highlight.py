"""Highlight mode: the school's name is marked in yellow, like a highlighter pen.

Clerks mark the school's row in the list of schools attached to a letter so the director
sees at once that the letter concerns us.  The mode is switched on in the account
settings; the server finds the name in the PDF's text (tables, a name broken over two
lines, the shifted Thai tone marks Word puts in PDFs, rotated pages), the editor shows
the marks, and saving paints them under the stamp in the PDF and the LINE pictures.
Scanned pages have no text to search - they are reported, never guessed.
Offline: PDFs are drawn on the spot; the AI, Drive, LINE and Google Sheets are fakes.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw, ImageFont, ImageOps

import core
import store

try:
    import pymupdf
except ImportError:         # phone-only setup
    pymupdf = None

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    webmain = None

FONT = core._p("THSarabunNew.ttf")
ZOOM = 200 / 72                       # PDF points -> pixels of a page rendered at 200 DPI
ROW = 24                              # table row height in points
WORD_LEAD = "ขอแสดงความยินดีกับ "      # text before the name on the Word-style line
# Word keeps the shifted tone mark of บ้ and the lowered sara u as private-use characters
WORD_NAME = "รร.บ\uf714านโพนทอง ประชาอ\uf718ทิศ"
OURS_ROW, OTHER_ROW = 4, 5            # our school and "บ้านโพนทอง" (another school) in the table
SCHOOLS = ["บ้านนาดี", "บ้านโคกสูง", "บ้านหนองแวง", "อนุบาลสกลนคร",
           "บ้านโพนทองประชาอุทิศ", "บ้านโพนทอง", "บ้านท่าแร่"]
LETTER = "สำนักงานเขตพื้นที่การศึกษาประถมศึกษาสกลนคร เขต ๑ ขอเชิญประชุมผู้บริหารสถานศึกษา"


def row_y(i):
    """baseline of table row i (points)"""
    return 130 + i * ROW


def make_pdf(path):
    """1 letter without the name, 2 list of schools, 3 name broken over two lines and a
    Word-style line (shifted tone mark, spaces), 4 scanned page, 5 page turned 90 degrees"""
    doc = pymupdf.open()

    def page():
        p = doc.new_page(width=595, height=842)
        p.insert_font(fontname="sarabun", fontfile=FONT)
        return p

    def text(p, x, y, s):
        p.insert_text((x, y), s, fontname="sarabun", fontsize=16)

    p = page()
    text(p, 72, 120, "เรียน ผู้อำนวยการโรงเรียนตามบัญชีรายชื่อแนบท้าย")
    for i in range(6):
        text(p, 90, 160 + i * 22, LETTER)

    p = page()
    for i, name in enumerate(SCHOOLS):
        p.draw_line((72, row_y(i) - 18), (520, row_y(i) - 18))
        text(p, 80, row_y(i), str(i + 1))
        text(p, 120, row_y(i), name)
        text(p, 400, row_y(i), "๒ คน")

    p = page()
    text(p, 72, 100, "ผู้ประสานงาน")
    text(p, 300, 140, "โรงเรียนบ้านโพนทอง")
    text(p, 300, 160, "ประชาอุทิศ")
    text(p, 72, 260, WORD_LEAD + WORD_NAME + " ที่ได้รับรางวัล")

    img = Image.new("RGB", (1240, 1754), "white")
    ImageDraw.Draw(img).text((150, 300), core.SCHOOL, font=ImageFont.truetype(FONT, 48), fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    p = doc.new_page(width=595, height=842)
    p.insert_image(p.rect, stream=buf.getvalue())

    p = page()
    text(p, 72, 300, "โรงเรียนบ้านโพนทองประชาอุทิศ ได้รับจัดสรรงบประมาณ")
    p.set_rotation(90)
    doc.save(path)
    doc.close()


def ink_box(img, box=None):
    """bounding box of the dark pixels (text) in img, optionally only inside box"""
    part = img.crop(box) if box else img
    bb = ImageOps.invert(part.convert("L")).point(lambda v: 255 if v > 127 else 0).getbbox()
    if bb and box:
        bb = (bb[0] + box[0], bb[1] + box[1], bb[2] + box[0], bb[3] + box[1])
    return bb


def yellow_share(img, box):
    """share of the pixels in box painted highlighter yellow (white paper under the mark)"""
    px = img.convert("RGB").crop(box).get_flattened_data()
    return sum(1 for r, g, b in px if r > 230 and g > 200 and b < 120) / len(px)


@unittest.skipIf(pymupdf is None, "ต้องลง pymupdf ก่อน (pip install -r requirements.txt)")
class FindNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp()
        cls.pdf = os.path.join(cls.work, "doc.pdf")
        make_pdf(cls.pdf)
        cls.marks, cls.scans = core.find_name_marks(cls.pdf, range(1, 6))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, True)

    def test_our_row_in_the_list_is_marked_and_the_school_with_a_shorter_name_is_not(self):
        (x0, y0, x1, y1), = self.marks[2]
        # the band sits on our row's text and stays out of the next row ("บ้านโพนทอง")
        self.assertLess((row_y(OURS_ROW) - 16) * ZOOM, y0)
        self.assertLess(y1, (row_y(OTHER_ROW) - 16) * ZOOM)
        page = core.render_pdf_page(self.pdf, 2, 200)
        name = ink_box(page, (int(110 * ZOOM), int((row_y(OURS_ROW) - 17) * ZOOM),
                              int(390 * ZOOM), int((row_y(OURS_ROW) + 5) * ZOOM)))
        self.assertLessEqual(x0, name[0])                  # covers the whole name ...
        self.assertGreaterEqual(x1, name[2])
        self.assertLess(x1 - x0, (name[2] - name[0]) * 1.15)   # ... and not the column next to it

    def test_name_broken_over_two_lines_gets_a_band_on_each_line(self):
        bands = sorted(b for b in self.marks[3] if b[0] > 290 * ZOOM)
        self.assertEqual(len(bands), 2)
        self.assertLess(bands[0][1], bands[1][1])          # one per line

    def test_word_style_text_with_shifted_tone_marks_and_spaces_is_found(self):
        word_line = [b for b in self.marks[3] if b[1] > 240 * ZOOM]
        self.assertEqual(len(word_line), 1)
        x0, _, x1, _ = word_line[0]
        font = pymupdf.Font(fontfile=FONT)
        start = 72 + font.text_length(WORD_LEAD, fontsize=16)          # where "รร." begins
        end = start + font.text_length(WORD_NAME, fontsize=16)
        pad = 3                                            # the band reaches a little past the text
        self.assertLess(abs(x0 / ZOOM - start), pad)       # from "รร." (not from "โพน") ...
        self.assertLess(abs(x1 / ZOOM - end), pad)         # ... to the end of "อุทิศ"

    def test_letter_without_the_name_has_no_marks_and_is_not_called_a_scan(self):
        self.assertNotIn(1, self.marks)
        self.assertNotIn(1, self.scans)

    def test_scanned_page_is_reported_never_guessed(self):
        self.assertNotIn(4, self.marks)
        self.assertEqual(self.scans, {4})

    def test_mark_lands_on_the_text_of_a_turned_page(self):
        (x0, y0, x1, y1), = self.marks[5]
        page = core.render_pdf_page(self.pdf, 5, 200)
        self.assertGreater(page.width, page.height)        # rendered the way it is shown
        ink = ink_box(page, (int(x0) - 40, int(y0) - 40, int(x1) + 40, int(y1) + 40))
        self.assertIsNotNone(ink, "no text under the mark")
        self.assertLess(abs((ink[0] + ink[2]) / 2 - (x0 + x1) / 2), 15)
        self.assertGreater(y1 - y0, x1 - x0)               # runs down the page like the text
        self.assertLess(abs(y0 - ink[1]), 0.6 * ZOOM * 16)

    def test_name_pattern_needs_the_part_after_baan(self):
        pat = core._name_pattern()
        key = lambda s: "".join(map(core._name_key, s))
        self.assertTrue(pat.search(key("โพนทองประชาอุทิศ")))
        self.assertTrue(pat.search(key("บ.โพนทองประชาอุทิศ")))
        self.assertFalse(pat.search(key("โรงเรียนบ้านโพนทอง")))


class PaintTests(unittest.TestCase):
    def test_paper_turns_yellow_and_the_text_stays_dark(self):
        img = Image.new("RGB", (100, 40), "white")
        ImageDraw.Draw(img).rectangle([40, 10, 60, 30], fill="black")
        core.paint_marks(img, [(10, 5, 90, 35), (95, 30, 150, 80)])      # second one runs off the page
        self.assertEqual(img.getpixel((20, 20)), core.MARK_RGB)
        self.assertEqual(img.getpixel((50, 20)), (0, 0, 0))
        self.assertEqual(img.getpixel((5, 20)), (255, 255, 255))         # outside the mark
        self.assertEqual(img.getpixel((97, 35)), core.MARK_RGB)

    def test_works_on_the_see_through_page_layer_used_when_saving(self):
        img = Image.new("RGBA", (50, 20), (255, 255, 255, 255))
        core.paint_marks(img, [(0, 0, 50, 20)])
        self.assertEqual(img.getpixel((25, 10)), core.MARK_RGB + (255,))


class FakeSheetsUsers:
    """values() of the users tab: get / clear / update from a row"""

    def __init__(self, rows):
        self.rows = rows

    def values(self):
        return self

    def _later(self, fn):
        return type("Req", (), {"execute": staticmethod(fn)})()

    def get(self, spreadsheetId, range):
        return self._later(lambda: {"values": [list(r) for r in self.rows]})

    def clear(self, spreadsheetId, range, body):
        def run():
            del self.rows[1:]
            return {}
        return self._later(run)

    def update(self, spreadsheetId, range, valueInputOption, body):
        def run():
            self.rows[:] = [list(r) for r in body["values"]]
            return {}
        return self._later(run)


class StoredSettingTests(unittest.TestCase):
    def sheets(self, rows):
        st = store.SheetsStore.__new__(store.SheetsStore)
        st._api, st.sheet_id = FakeSheetsUsers(rows), "sheet"
        st._cache, st._hist_cache, st._users_cache = {}, None, None
        return st

    def test_users_from_a_sheet_made_before_the_setting_start_with_it_off(self):
        old_header = store.USER_COLS[:8]
        rows = [old_header, ["ครูเอ", "s", "h", "ครู เอ", "user", "approved", "2569-01-01", "TRUE"]]
        st = self.sheets(rows)
        u = st.load_users()["ครูเอ"]
        self.assertFalse(u["highlight"])
        self.assertTrue(u["must_change"])
        u["highlight"] = True
        st.save_users({"ครูเอ": u})
        self.assertEqual(rows[0], store.USER_COLS)           # the new column gets its heading
        self.assertEqual(rows[1][store.USER_COLS.index("highlight")], "TRUE")
        self.assertTrue(st.load_users()["ครูเอ"]["highlight"])

    def test_local_store_keeps_it_in_the_users_file(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "_p", lambda name: os.path.join(tmp, name)):
            st = store.LocalStore.__new__(store.LocalStore)
            st._lock = MagicMock()
            st.save_users({"ครูเอ": {"display": "ครู เอ", "highlight": True}})
            self.assertTrue(st.load_users()["ครูเอ"]["highlight"])


@unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class SettingEndpointTests(unittest.TestCase):
    def setUp(self):
        self.users = {"ครูเอ": {"status": "approved", "salt": "a"},
                      "bee": {"status": "approved", "salt": "b"}}
        saved = lambda users: self.users.update(users)
        for p in (patch.object(webmain, "load_users", side_effect=lambda **kw: self.users),
                  patch.object(webmain, "save_users", side_effect=saved)):
            p.start()
            self.addCleanup(p.stop)
        self.a = TestClient(webmain.app,
                            cookies={"session": webmain.make_session("ครูเอ", self.users["ครูเอ"])})

    def test_switch_on_and_off_for_your_own_account_only(self):
        self.assertFalse(self.a.get("/api/me").json()["highlight"])
        r = self.a.post("/api/me/settings", json={"highlight": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.users["ครูเอ"]["highlight"])
        self.assertNotIn("highlight", self.users["bee"])
        self.assertTrue(self.a.get("/api/me").json()["highlight"])
        self.a.post("/api/me/settings", json={"highlight": False})
        self.assertFalse(self.a.get("/api/me").json()["highlight"])

    def test_nonsense_is_refused(self):
        for bad in ({"highlight": "yes"}, {}, {"highlight": 1}):
            self.assertEqual(self.a.post("/api/me/settings", json=bad).status_code, 400, bad)

    def test_needs_login(self):
        self.assertEqual(TestClient(webmain.app).post("/api/me/settings",
                                                      json={"highlight": True}).status_code, 401)

    def test_the_editor_learns_the_setting_of_whoever_opens_the_document(self):
        job = {"id": "j", "user": "ครูเอ", "source": "upload", "status": "ready", "pages": [],
               "marks": [{"page": 0, "x": 1, "y": 2, "w": 3, "h": 4}], "scan_pages": [2]}
        with patch.object(webmain.docmode, "_jobs", {"j": job}):
            d = self.a.get("/api/doc/j").json()
            self.assertEqual((d["highlight"], d["marks"], d["scan_pages"]), (False, job["marks"], [2]))
            self.users["ครูเอ"]["highlight"] = True
            self.assertTrue(self.a.get("/api/doc/j").json()["highlight"])


@unittest.skipIf(pymupdf is None, "ต้องลง pymupdf ก่อน (pip install -r requirements.txt)")
class PrepareAndSaveTests(unittest.TestCase):
    """the web app end to end: found when the document is read, painted when saved"""

    def setUp(self):
        from web import docmode
        self.docmode = docmode
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        ai = ("เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ\nขอเชิญประชุม\nเพื่อโปรดทราบ", 1,
              "ศธ 04142/ว1234", "ประชุม", "15 มกราคม 2570", "สพป.สกลนคร เขต 1", "-")
        for p in (patch.object(docmode, "generate_kasien_text", return_value=ai),
                  patch.object(docmode, "get_next_receipt_no", return_value="๕๗"),
                  patch.object(core, "OUTPUT_ROOT", os.path.join(self.work, "out"))):
            p.start()
            self.addCleanup(p.stop)
        self.pdf = os.path.join(self.work, "doc.pdf")
        make_pdf(self.pdf)
        self.job = {"id": "j", "user": "ครูเอ", "source": "upload", "status": "analyzing",
                    "dir": self.work}
        docmode._prepare(self.job, self.pdf, doc_no="-", doc_title="-", doc_date="-", sender="-",
                         emoji="📄", attach="", book_id=None)
        self.assertEqual(self.job["status"], "ready")

    def save(self, **extra):
        sent = MagicMock(return_value=True)
        drive = MagicMock()
        drive.upload.return_value = {}
        payload = dict({"stamp": self.job["stamp"], "boxes": self.job["boxes"], "date": "", "time": ""},
                       **extra)
        with patch.dict(sys.modules, {"drive": drive}), \
                patch("store.get_store", return_value=MagicMock()), \
                patch.object(self.docmode, "publish_line_image",
                             side_effect=lambda p, day=None: "https://x/" + os.path.basename(p)), \
                patch.object(self.docmode, "send_line_with_image", sent):
            result = self.docmode._finalize_claimed(self.job, payload, lambda **kw: "๕๗")
        out = os.path.join(core.OUTPUT_ROOT, core.day_folder(), result["filename"])
        pictures = {u.rsplit("_", 1)[1] for u in sent.call_args.args[1]}
        return out, pictures

    def list_mark(self):
        m = next(m for m in self.job["marks"] if self.job["pages"][m["page"]]["index"] == 2)
        return (m["x"] + 4, m["y"] + 2, m["x"] + m["w"] - 4, m["y"] + m["h"] - 2)

    def test_found_while_reading_on_the_pages_shown_in_the_editor(self):
        pages = sorted({self.job["pages"][m["page"]]["index"] for m in self.job["marks"]})
        self.assertEqual(pages, [2, 3, 5])
        self.assertEqual(self.job["scan_pages"], [4])

    def test_saved_with_highlight_paints_the_pdf_but_line_still_stops_at_the_signature_page(self):
        out, pictures = self.save(highlight=True)
        page2 = core.render_pdf_page(out, 2, 200)
        self.assertGreater(yellow_share(page2, self.list_mark()), 0.4)
        self.assertEqual(yellow_share(core.render_pdf_page(out, 1, 200), (0, 0, 1654, 2339)), 0)
        # pages after the signature page go to LINE only when ticked on screen
        self.assertEqual(pictures, {"หน้า๑.jpg"})

    def test_ticked_list_page_goes_to_line_with_the_mark(self):
        out, pictures = self.save(highlight=True, line_pages=[1, 2])
        self.assertEqual(pictures, {"หน้า๑.jpg", "หน้า๒.jpg"})
        jpg = out[:-len(".pdf")] + "_หน้า๒.jpg"            # the picture kept next to the PDF
        with Image.open(jpg) as picture:
            self.assertGreater(yellow_share(picture, self.list_mark()), 0.4)

    def test_switched_off_for_this_document_leaves_the_pages_as_they_came(self):
        out, pictures = self.save(highlight=False)
        page2 = core.render_pdf_page(out, 2, 200)
        self.assertEqual(yellow_share(page2, self.list_mark()), 0)
        self.assertEqual(pictures, {"หน้า๑.jpg"})
        with pymupdf.open(out) as doc:                     # untouched pages keep their text
            self.assertIn("ประชาอุทิศ", doc[1].get_text())

    def test_pages_ticked_by_hand_win(self):
        _, pictures = self.save(highlight=True, line_pages=[1])
        self.assertEqual(pictures, {"หน้า๑.jpg"})

    def test_only_pages_left_highlighted_on_screen_are_painted(self):
        out, pictures = self.save(highlight=[2, "x", -1, True, "9"])      # page 3 and 5 switched off
        self.assertGreater(yellow_share(core.render_pdf_page(out, 2, 200), self.list_mark()), 0.4)
        self.assertEqual(yellow_share(core.render_pdf_page(out, 3, 200), (0, 0, 1654, 2339)), 0)
        self.assertEqual(yellow_share(core.render_pdf_page(out, 5, 200), (0, 0, 2339, 1654)), 0)
        self.assertEqual(pictures, {"หน้า๑.jpg"})


if __name__ == "__main__":
    unittest.main()
