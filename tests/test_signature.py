"""Signature under the endorsement: upload cleanup, rendering with the date, storage, endpoints.

Offline: images are synthesised, Google Sheets is an in-memory fake.
"""
from __future__ import annotations

import io
import math
import os
import random
import re
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from PIL import Image, ImageDraw

import core
import store
from web import signature as sig

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    webmain = None


def phone_photo(w=1200, h=900, seed=1):
    """signature in blue pen on grey paper, lit unevenly (shadow on the right), with noise"""
    rnd = random.Random(seed)
    paper = Image.new("L", (w, h))
    paper.putdata([max(0, min(255, int(235 - 70 * (i % w) / w) + rnd.randint(-8, 8)))
                   for i in range(w * h)])
    img = paper.convert("RGB")
    d = ImageDraw.Draw(img)
    d.line([(300 + i * 12, 450 + int(80 * math.sin(i / 4))) for i in range(50)],
           fill=(25, 40, 140), width=9, joint="curve")
    d.line([(350, 520), (820, 380)], fill=(25, 40, 140), width=7)
    return img


def jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def rgba(png):
    return Image.open(io.BytesIO(png)).convert("RGBA")


def visible_colours(img):
    return {p[:3] for p in img.get_flattened_data() if p[3] > 0}


def opaque_share(img):
    px = img.get_flattened_data()
    return sum(1 for p in px if p[3] > 200) / len(px)


def white_card_png():
    """a PNG people often have: opaque white background, transparent rounded corners, black ink"""
    im = Image.new("RGBA", (600, 220), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, 599, 219], radius=30, fill=(255, 255, 255, 255))
    d.line([(60, 160), (200, 50), (330, 170), (540, 60)], fill=(10, 10, 10, 255), width=8)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class ShortDateTests(unittest.TestCase):
    def test_day_short_month_and_two_digit_buddhist_year_in_thai_digits(self):
        self.assertEqual(sig.thai_short_date(datetime(2026, 10, 12)), "๑๒ ต.ค. ๖๙")
        self.assertEqual(sig.thai_short_date(datetime(2027, 1, 5)), "๕ ม.ค. ๗๐")


class CleanUploadTests(unittest.TestCase):
    def test_phone_photo_keeps_the_ink_and_drops_the_paper(self):
        png = sig.clean_upload(jpeg(phone_photo()))
        self.assertLessEqual(len(png), sig.MAX_STORE_BYTES)
        im = rgba(png)
        alpha = im.getchannel("A")
        # cropped to the strokes: the corners are paper, so they must be see-through
        for corner in ((0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1)):
            self.assertEqual(alpha.getpixel(corner), 0)
        self.assertEqual(alpha.getextrema()[1], 255)          # ink is solid
        # whatever the pen, the strokes take the endorsement text colour
        self.assertEqual(visible_colours(im), {sig.INK})

    def test_photo_taken_sideways_is_turned_upright(self):
        exif = Image.Exif()
        exif[0x0112] = 6                                      # "rotate 90° to view"
        buf = io.BytesIO()
        phone_photo().save(buf, "JPEG", exif=exif)
        im = rgba(sig.clean_upload(buf.getvalue()))
        self.assertGreater(im.height, im.width)

    def test_png_already_without_background_is_trimmed_and_recoloured(self):
        src = Image.new("RGBA", (400, 150), (0, 0, 0, 0))
        ImageDraw.Draw(src).line([(20, 100), (380, 40)], fill=(0, 0, 0, 255), width=6)
        buf = io.BytesIO()
        src.save(buf, "PNG")
        im = rgba(sig.clean_upload(buf.getvalue()))
        self.assertEqual(im.getpixel((0, 0))[3], 0)
        self.assertLess(im.width, 400)                        # trimmed to the stroke
        self.assertEqual(visible_colours(im), {sig.INK})

    def test_white_background_png_with_see_through_corners_loses_its_background(self):
        # used to be kept as-is because some pixels were already transparent -> a white box
        im = rgba(sig.clean_upload(white_card_png()))
        self.assertLess(opaque_share(im), 0.2)
        self.assertEqual(visible_colours(im), {sig.INK})

    def test_huge_picture_is_refused_before_it_is_decoded(self):
        buf = io.BytesIO()
        Image.new("1", (5000, 4000), 1).save(buf, "PNG")      # 20 MP — too much RAM to decode
        with self.assertRaises(sig.SignatureError) as ctx:
            sig.clean_upload(buf.getvalue())
        self.assertIn("ใหญ่เกินไป", str(ctx.exception))

    def test_blank_paper_and_non_images_are_refused_with_a_reason(self):
        with self.assertRaises(sig.SignatureError):
            sig.clean_upload(jpeg(Image.new("RGB", (800, 600), (225, 225, 220))))
        with self.assertRaises(sig.SignatureError):
            sig.clean_upload(b"%PDF-1.4 not a picture")
        with self.assertRaises(sig.SignatureError):
            sig.clean_upload(b"x" * (sig.MAX_UPLOAD_BYTES + 1))


class RenderBlockTests(unittest.TestCase):
    CM = 200 / 2.54

    def setUp(self):
        self.png = sig.clean_upload(jpeg(phone_photo()))

    def test_date_sits_centred_under_the_signature(self):
        block = sig.render_block(self.png, "๑๒ ต.ค. ๖๙", 100, self.CM)
        sig_h = round(rgba(self.png).height * int(sig.SIG_WIDTH_CM * self.CM) / rgba(self.png).width)
        below = block.getchannel("A").crop((0, sig_h, block.width, block.height)).getbbox()
        self.assertIsNotNone(below)                           # something is drawn under it
        left, right = below[0], block.width - below[2]
        self.assertLess(abs(left - right), 12)                # roughly centred

    def test_signature_is_drawn_in_the_text_colour_on_a_clear_background(self):
        block = sig.render_block(self.png, "๑๒ ต.ค. ๖๙", 100, self.CM)
        sig_h = round(rgba(self.png).height * int(sig.SIG_WIDTH_CM * self.CM) / rgba(self.png).width)
        self.assertEqual(visible_colours(block.crop((0, 0, block.width, sig_h))), {sig.INK})

    def test_signature_saved_before_the_recolouring_is_drawn_the_same_way(self):
        # stored as uploaded earlier: black ink on an opaque white card
        block = sig.render_block(white_card_png(), "", 100, self.CM)
        self.assertEqual(visible_colours(block), {sig.INK})
        self.assertLess(opaque_share(block), 0.2)

    def test_size_scales_signature_and_no_date_means_no_date_line(self):
        small = sig.render_block(self.png, "", 100, self.CM)
        big = sig.render_block(self.png, "", 200, self.CM)
        self.assertAlmostEqual(big.width / small.width, 2, delta=0.05)
        self.assertEqual(small.width, int(sig.SIG_WIDTH_CM * self.CM))
        with_date = sig.render_block(self.png, "๑๒ ต.ค. ๖๙", 100, self.CM)
        self.assertGreater(with_date.height, small.height)
        # a bad size never breaks a save (the receipt number is already taken by then)
        self.assertEqual(sig.render_block(self.png, "", "abc", self.CM).width, small.width)


class FakeSheetsAPI:
    """just enough of spreadsheets().values() for one tab, applied when execute() runs"""

    def __init__(self, rows):
        self.rows = rows

    def values(self):
        return self

    @staticmethod
    def _row(rng):
        return int(re.search(r"!A(\d+)", rng).group(1))

    def _later(self, fn):
        return type("Req", (), {"execute": staticmethod(fn)})()

    def get(self, spreadsheetId, range):
        return self._later(lambda: {"values": [list(r) for r in self.rows]})

    def update(self, spreadsheetId, range, valueInputOption, body):
        def run():
            i = self._row(range)
            while len(self.rows) < i:
                self.rows.append([])
            self.rows[i - 1] = list(body["values"][0])
            return {}
        return self._later(run)

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        return self._later(lambda: self.rows.extend(list(r) for r in body["values"]) or {})

    def clear(self, spreadsheetId, range, body):
        def run():
            self.rows[self._row(range) - 1] = []
            return {}
        return self._later(run)


class StorageTests(unittest.TestCase):
    PNG_A, PNG_B = b"\x89PNG first", b"\x89PNG second"

    def roundtrip(self, st):
        self.assertIsNone(st.load_signature("ครูเอ"))
        st.save_signature("ครูเอ", self.PNG_A)
        st.save_signature("bee", self.PNG_B)
        self.assertEqual(st.load_signature("ครูเอ"), self.PNG_A)
        st.save_signature("ครูเอ", self.PNG_B)                # replace, not a second copy
        self.assertEqual(st.load_signature("ครูเอ"), self.PNG_B)
        st.delete_signature("ครูเอ")
        self.assertIsNone(st.load_signature("ครูเอ"))
        self.assertEqual(st.load_signature("bee"), self.PNG_B)  # other users untouched
        st.delete_signature("nobody")                         # deleting nothing is fine

    def test_local_store_keeps_one_file_per_user(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(core, "_p", lambda name: os.path.join(tmp, name)):
            self.roundtrip(store.LocalStore.__new__(store.LocalStore))
            self.assertEqual(len(os.listdir(os.path.join(tmp, "signatures"))), 1)

    def test_sheets_store_keeps_one_row_per_user_in_its_own_tab(self):
        st = store.SheetsStore.__new__(store.SheetsStore)
        rows = [["username", "รูปลายเซ็น (PNG base64)", "อัปเดตเมื่อ"]]
        st._api, st.sheet_id = FakeSheetsAPI(rows), "sheet"
        st._cache, st._hist_cache, st._users_cache = {}, None, None
        self.roundtrip(st)
        self.assertEqual([r[0] for r in rows[1:] if r], ["bee"])


class FakeUserStore:
    def __init__(self):
        self.sigs = {}

    def load_signature(self, user):
        return self.sigs.get(user)

    def save_signature(self, user, png):
        self.sigs[user] = png

    def delete_signature(self, user):
        self.sigs.pop(user, None)


@unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.st = FakeUserStore()
        users = {"ครูเอ": {"status": "approved"}, "bee": {"status": "approved"}}
        for p in (patch("store.get_store", return_value=self.st),
                  patch.object(webmain, "load_users", return_value=users),
                  patch.dict(webmain._sessions, {"tokA": "ครูเอ", "tokB": "bee"})):
            p.start()
            self.addCleanup(p.stop)
        self.a = TestClient(webmain.app, cookies={"session": "tokA"})
        self.b = TestClient(webmain.app, cookies={"session": "tokB"})

    def upload(self, client, data):
        return client.post("/api/me/signature", files={"file": ("sig.jpg", data, "image/jpeg")})

    def test_upload_view_and_delete_only_your_own_signature(self):
        r = self.upload(self.a, jpeg(phone_photo()))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["png"].startswith("data:image/png;base64,"))
        self.assertTrue(self.a.get("/api/me/signature").json()["has"])
        self.assertFalse(self.b.get("/api/me/signature").json()["has"])   # not shared
        self.assertEqual(self.b.delete("/api/me/signature").status_code, 200)
        self.assertTrue(self.a.get("/api/me/signature").json()["has"])    # b can't delete a's
        self.a.delete("/api/me/signature")
        self.assertFalse(self.a.get("/api/me/signature").json()["has"])

    def test_menu_shows_an_older_signature_the_way_documents_will(self):
        self.st.sigs["ครูเอ"] = white_card_png()              # uploaded before recolouring
        uri = self.a.get("/api/me/signature").json()["png"]
        import base64
        shown = rgba(base64.b64decode(uri.split(",", 1)[1]))
        self.assertEqual(visible_colours(shown), {sig.INK})
        self.assertLess(opaque_share(shown), 0.2)

    def test_bad_picture_gets_a_readable_400(self):
        r = self.upload(self.a, jpeg(Image.new("RGB", (600, 400), (230, 230, 230))))
        self.assertEqual(r.status_code, 400)
        self.assertIn("ไม่พบเส้นลายเซ็น", r.json()["detail"])

    def test_saving_with_a_signature_but_none_stored_stops_before_taking_a_number(self):
        job = {"id": "j", "user": "ครูเอ", "source": "upload", "status": "ready"}
        with patch.object(webmain.docmode, "_jobs", {"j": job}), \
                patch.object(webmain.docmode, "finalize") as finalize:
            r = self.a.post("/api/doc/j/save", json={"signature": {"page": 0}, "boxes": []})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ไม่พบรูปลายเซ็น", r.json()["detail"])
        finalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
