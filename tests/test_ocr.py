"""The 🔎 scan button: Tesseract reads the scanned pages the user picks and finds the school's name.

Scanned pages carry no text to search, so the school's name in a scanned list of schools was
never highlighted.  Tesseract (free, runs on our own server, no AI) reads the pages the user
picks - usually the few pages of the list - in the background, one page at a time; a name
it misreads by a letter or two is still found, a different school with a shorter name is not.
Offline: Tesseract is faked except in the one test that needs the real program.
"""
from __future__ import annotations

import io
import os
import shutil
import stat
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import core

try:
    import pymupdf
except ImportError:         # phone-only setup
    pymupdf = None

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
    from web import docmode
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    webmain = docmode = None

FONT = core._p("THSarabunNew.ttf")
CORE = core._name_core()[0]           # "โพนทองประชาอทศ" - the part that must be there


def pieces_of(text, x=100, y=500, line=("1", "1", "1")):
    """what Tesseract gives for one printed line: (almost) one piece per letter, with boxes"""
    out, cx = [], x
    for ch in text:
        if ch == " ":
            cx += 40
            continue
        mark = ch in "่้๊๋์ัิีึืุู็ํ"
        w = 8 if mark else 14
        box = (cx - (8 if mark else 0), y - (14 if mark else 0), cx - (8 if mark else 0) + w, y + 30)
        out.append((line, ch, box))
        if not mark:
            cx += 16
    return out


class FuzzyFindTests(unittest.TestCase):
    def test_exact_name_inside_a_line(self):
        line = "15" + core._key("บ้านโพนทองประชาอุทิศอำเภอเมือง")
        s, e = core._fuzzy_find(line, CORE, 2)
        self.assertEqual(line[s:e], CORE)

    def test_two_misread_letters_are_forgiven_three_are_not(self):
        self.assertIsNotNone(core._fuzzy_find("โพนทองประซาอทศ", CORE, 2))       # ช read as ซ
        self.assertIsNotNone(core._fuzzy_find("โพนทองประชาอทค", CORE, 2))       # ศ as ค
        self.assertIsNotNone(core._fuzzy_find("โพนทอบประซาอทศ", CORE, 2))
        self.assertIsNone(core._fuzzy_find("โพนทอบประซาอทค", CORE, 2))

    def test_other_schools_do_not_pass(self):
        for other in ("บ้านโพนทอง", "บ้านโพนทองอำเภอเมืองสกลนคร", "บ้านโพนสวางประชาสรรค์",
                      "บ้านโพนงามประชาอุทิศ"):
            self.assertIsNone(core._fuzzy_find(core._key(other), CORE, 2), other)


class OcrMarksTests(unittest.TestCase):
    def test_band_covers_the_name_with_baan_and_not_the_rest_of_the_row(self):
        row = pieces_of("15 บ้านโพนทองประชาอุทิศ อำเภอเมือง")
        (x0, y0, x1, y1), = core._ocr_marks(row)
        letters = [b for _, ch, b in row if ch not in "1"]
        first = next(b for _, ch, b in row if ch == "บ")
        last = next(b for _, ch, b in reversed(row) if ch == "ศ")
        self.assertLess(x0, first[0])                     # starts at บ้าน ...
        self.assertGreater(x1, last[2])                   # ... ends after อุทิศ
        amphoe = next(b for _, ch, b in row if ch == "เ")
        self.assertLess(x1, amphoe[0])                    # but not into "อำเภอ"
        self.assertLess(y0, min(b[1] for b in letters))   # tone marks inside the band

    def test_a_misread_letter_still_marks_the_name(self):
        self.assertEqual(len(core._ocr_marks(pieces_of("บ้านโพนทองประซาอุทิศ"))), 1)

    def test_lines_are_matched_one_by_one(self):
        rows = (pieces_of("บ้านโพนทอง", y=300, line=("1", "1", "1")) +
                pieces_of("บ้านโพนทองประชาอุทิศ", y=400, line=("1", "1", "2")) +
                pieces_of("โรงเรียนบ้านโพนทองประชาอุทิศ", y=500, line=("1", "1", "3")))
        marks = core._ocr_marks(rows)
        # one band each for lines 2 and 3, starting just above their tone marks (y 386 / 486)
        self.assertEqual(len(marks), 2)
        for band, mark_top in zip(marks, (386, 486)):
            self.assertTrue(mark_top - 6 < band[1] < mark_top, band)
        self.assertLess(marks[1][0], 100)                 # "โรงเรียน" is part of the band

    def test_reading_tesseract_output(self):
        tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
               "4\t1\t1\t1\t1\t0\t10\t20\t300\t40\t-1\t\n"
               "5\t1\t1\t1\t1\t1\t10\t20\t12\t30\t91\tบ\n"
               "5\t1\t1\t1\t1\t2\t25\t12\t7\t8\t90\t้\n")
        done = types.SimpleNamespace(returncode=0, stdout=tsv.encode(), stderr=b"")
        with patch("subprocess.run", return_value=done) as run, \
                patch.object(core, "tesseract_cmd", return_value="tesseract"):
            got = core._ocr_page(Image.new("RGB", (50, 50), "white"), 200)
        self.assertEqual(got, [(("1", "1", "1"), "บ", (10, 20, 22, 50)),
                               (("1", "1", "1"), "้", (25, 12, 32, 20))])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-l") + 1], "tha")
        self.assertEqual(args[args.index("--psm") + 1], "4")
        # Windows: no console window flashing up for every page the desktop program scans
        self.assertEqual(run.call_args.kwargs["creationflags"],
                         getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0))


@unittest.skipIf(os.name == "nt", "ใช้สคริปต์ sh ปลอมตัวเป็น tesseract")
class FindTesseractTests(unittest.TestCase):
    def fake(self, langs):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "tesseract")
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho 'List of available languages (%d):'\n" % len(langs))
            for lang in langs:
                f.write(f"echo {lang}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def found(self, path):
        with patch.object(core, "_TESSERACT", None), \
                patch.dict(os.environ, {"SARABAN_TESSERACT": path}), \
                patch("shutil.which", return_value=None):
            return core.tesseract_cmd()

    def test_needs_the_thai_language(self):
        thai = self.fake(["eng", "osd", "tha"])
        self.assertEqual(self.found(thai), thai)
        self.assertEqual(self.found(self.fake(["eng", "osd"])), "")

    def test_no_program_means_no_scanning(self):
        self.assertEqual(self.found("/nowhere/tesseract"), "")

    def test_installed_while_the_program_runs_is_found_within_a_minute(self):
        later = os.path.join(tempfile.mkdtemp(), "tesseract")
        self.addCleanup(shutil.rmtree, os.path.dirname(later), True)
        with patch.object(core, "_TESSERACT", None), patch.object(core, "_TESSERACT_AT", 0.0), \
                patch.dict(os.environ, {"SARABAN_TESSERACT": later}), \
                patch("shutil.which", return_value=None):
            self.assertEqual(core.tesseract_cmd(), "")
            shutil.copy(self.fake(["tha"]), later)
            self.assertEqual(core.tesseract_cmd(), "")          # not asked again straight away
            core._TESSERACT_AT -= 61
            self.assertEqual(core.tesseract_cmd(), later)


class ScanWholeDocumentTests(unittest.TestCase):
    """the program on the school computer reads every scanned page itself, several at a time"""

    def scan(self, pages, ocr, **kw):
        got = []
        with patch.object(core, "ocr_name_marks", side_effect=ocr):
            core.scan_name_pages("doc.pdf", pages, 200, on_page=lambda n, b, e: got.append((n, b, e)), **kw)
        return got

    def test_every_page_is_read_and_what_is_found_is_reported(self):
        got = self.scan([3, 4, 5, 6], lambda pdf, n, dpi, name: [(1, 2, 3, 4)] if n == 5 else [], workers=3)
        self.assertEqual(sorted(n for n, _, _ in got), [3, 4, 5, 6])
        self.assertEqual([(n, b) for n, b, _ in got if b], [(5, [(1, 2, 3, 4)])])

    def test_pages_are_read_side_by_side(self):
        lock, now, most = __import__("threading").Lock(), [0], [0]

        def ocr(pdf, n, dpi, name):
            with lock:
                now[0] += 1
                most[0] = max(most[0], now[0])
            time.sleep(0.15)
            with lock:
                now[0] -= 1
            return []
        self.scan([1, 2, 3, 4], ocr, workers=2)
        self.assertEqual(most[0], 2)

    def test_a_page_that_cannot_be_read_does_not_stop_the_rest(self):
        def ocr(pdf, n, dpi, name):
            if n == 2:
                raise RuntimeError("อ่านไม่ได้")
            return []
        got = self.scan([1, 2, 3], ocr, workers=1)
        self.assertEqual([n for n, _, _ in got], [1, 2, 3])
        self.assertIsInstance(got[1][2], RuntimeError)

    def test_closing_the_window_stops_the_scan(self):
        closed, read = __import__("threading").Event(), []

        def ocr(pdf, n, dpi, name):
            read.append(n)
            closed.set()                                      # closed while page 1 was being read
            return [(1, 2, 3, 4)]
        self.assertEqual(self.scan([1, 2, 3], ocr, workers=1, stop=closed.is_set), [])
        self.assertEqual(read, [1])


def scanned_list_pdf(path, rows=30, ours=15):
    """an image-only page: a scanned list of schools, our school in row `ours`, a little tilted"""
    dpi = 200
    img = Image.new("L", (1654, 2339), 255)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, int(14 / 72 * dpi))
    names = ["บ้านนาดี", "บ้านโคกสูง", "บ้านโพนทอง", "บ้านท่าแร่", "อนุบาลสกลนคร"]
    for i in range(rows):
        y = 240 + i * 56
        d.line([(160, y - 6), (1500, y - 6)], fill=0, width=2)
        d.text((180, y), str(i + 1), font=font, fill=0)
        d.text((300, y), "บ้านโพนทองประชาอุทิศ" if i + 1 == ours else names[i % len(names)], font=font, fill=0)
        d.text((1000, y), "อำเภอเมืองสกลนคร", font=font, fill=0)
    img = img.rotate(0.6, fillcolor=255).filter(ImageFilter.GaussianBlur(0.7))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=70)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(path)
    doc.close()
    return 240 + (ours - 1) * 56                      # top of our row, in 200 DPI pixels


@unittest.skipIf(pymupdf is None or not core.tesseract_cmd(),
                 "ต้องมี Tesseract ภาษาไทยในเครื่อง (apt install tesseract-ocr-tha)")
class RealTesseractTests(unittest.TestCase):
    def test_finds_our_row_in_a_scanned_list(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        top = scanned_list_pdf(os.path.join(work, "scan.pdf"))
        (x0, y0, x1, y1), = core.ocr_name_marks(os.path.join(work, "scan.pdf"), 1)
        self.assertLess(abs((y0 + y1) / 2 - (top + 28)), 28)        # on our row, not the next
        self.assertLess(x0, 310)
        self.assertLess(x1, 1000)                                  # not into the district column


def ready_job(test, marks=()):
    work = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, work, True)
    job = {"id": "ocr-%d" % id(test), "user": "ครูเอ", "source": "upload", "status": "ready",
           "dir": work, "pdf_path": os.path.join(work, "doc.pdf"),
           "pages": [{"index": n, "w": 1654, "h": 2339} for n in range(1, 6)],
           "marks": list(marks)}
    with docmode._lock:
        docmode._jobs[job["id"]] = job
    test.addCleanup(lambda: docmode._jobs.pop(job["id"], None))
    return job


@unittest.skipIf(docmode is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class BackgroundScanTests(unittest.TestCase):
    def setUp(self):
        self.read = []

        def ocr(pdf, n, dpi):
            self.read.append(n)
            return [(300, 1000, 700, 1040)] if n == 4 else []

        for p in (patch.object(core, "tesseract_cmd", return_value="tesseract"),
                  patch.object(core, "ocr_name_marks", side_effect=ocr)):
            p.start()
            self.addCleanup(p.stop)

    def test_reads_only_the_picked_pages_not_already_found_and_marks_what_it_finds(self):
        job = ready_job(self, marks=[{"page": 1, "x": 1, "y": 2, "w": 3, "h": 4}])   # page 2 by text
        state = docmode.start_ocr(job, 5, 2)                    # either order
        self.assertEqual(state["pages"], [3, 4, 5])
        docmode._ocr_tasks.join()
        self.assertEqual(self.read, [3, 4, 5])
        st = docmode.ocr_state(job)
        self.assertEqual((st["status"], st["done"], st["found"]), ("done", 3, [4]))
        self.assertEqual(job["marks"][-1], {"page": 3, "x": 300, "y": 1000, "w": 400, "h": 40, "ocr": True})

    def test_stop_and_leaving_the_document_end_the_scan(self):
        job = ready_job(self)
        gate = __import__("threading").Event()
        core.ocr_name_marks.side_effect = lambda pdf, n, dpi: (gate.wait(5), [])[1]
        docmode.start_ocr(job, 1, 5)
        while docmode.ocr_state(job)["status"] != "running":
            time.sleep(0.01)
        self.assertEqual(docmode.start_ocr(job, 1, 2)["pages"], [1, 2, 3, 4, 5])  # pressed twice: same scan
        docmode.stop_ocr(job)
        gate.set()
        docmode._ocr_tasks.join()
        self.assertEqual(docmode.ocr_state(job)["status"], "stopped")
        self.assertEqual(docmode.ocr_state(job)["done"], 1)

    def test_saved_meanwhile_stops_too(self):
        job = ready_job(self)

        def ocr(pdf, n, dpi):
            job["status"] = "done"                              # saved while page 1 was read
            return []
        core.ocr_name_marks.side_effect = ocr
        docmode.start_ocr(job, 1, 5)
        docmode._ocr_tasks.join()
        self.assertEqual(docmode.ocr_state(job)["status"], "stopped")

    def test_refused_without_tesseract_or_with_pages_not_in_the_document(self):
        job = ready_job(self)
        with self.assertRaises(ValueError):
            docmode.start_ocr(job, 7, 9)
        core.tesseract_cmd.return_value = ""
        with self.assertRaises(docmode.OcrUnavailable):
            docmode.start_ocr(job, 1, 2)

    def test_at_most_a_set_number_of_pages_a_time(self):
        job = ready_job(self)
        with patch.object(docmode, "OCR_MAX_PAGES", 2):
            state = docmode.start_ocr(job, 1, 5)
        docmode._ocr_tasks.join()
        self.assertEqual((state["pages"], state["cut"]), ([1, 2], True))

    def test_only_the_given_pages_and_no_cap_when_the_system_scans_by_itself(self):
        job = ready_job(self)
        with patch.object(docmode, "OCR_MAX_PAGES", 2):
            state = docmode.start_ocr(job, 2, 5, only={2, 4, 5}, limit=docmode.MAX_REVIEW_PAGES, auto=True)
        docmode._ocr_tasks.join()
        self.assertEqual((state["pages"], state["cut"], state["auto"]), ([2, 4, 5], False, True))
        self.assertEqual(self.read, [2, 4, 5])                  # page 3 (text) is not read


@unittest.skipIf(docmode is None or pymupdf is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class AutoScanOnTheSchoolComputerTests(unittest.TestCase):
    """the web app run on the school computer (เปิดเซิร์ฟเวอร์.bat sets SARABAN_OCR_AUTO): every
    scanned page is read as soon as the document is ready - on the free cloud server only on request"""

    def prepared(self, auto):
        from tests.test_highlight import make_pdf
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        pdf = os.path.join(work, "doc.pdf")
        make_pdf(pdf)                                           # page 4 is a scan, the rest is text
        ai = ("เรียน ผอ.\nเพื่อโปรดทราบ", 1, "ศธ 1", "ประชุม", "-", "-", "-")
        job = {"id": "auto-%d" % id(self), "user": "ครูเอ", "source": "upload", "status": "analyzing",
               "dir": work}
        with patch.object(docmode, "generate_kasien_text", return_value=ai), \
                patch.object(docmode, "get_next_receipt_no", return_value="๕๗"), \
                patch.object(docmode, "OCR_AUTO", auto), \
                patch.object(core, "tesseract_cmd", return_value="tesseract"), \
                patch.object(core, "ocr_name_marks", side_effect=lambda pdf, n, dpi: [(10, 20, 110, 50)]):
            docmode._prepare(job, pdf, doc_no="-", doc_title="-", doc_date="-", sender="-",
                             emoji="📄", attach="", book_id=None)
            docmode._ocr_tasks.join()
        self.assertEqual(job["status"], "ready")
        return job

    def test_scanned_pages_are_read_as_soon_as_the_document_is_ready(self):
        st = docmode.ocr_state(self.prepared(True))
        self.assertEqual((st["status"], st["pages"], st["found"], st["auto"]), ("done", [4], [4], True))

    def test_not_on_the_free_cloud_server(self):
        self.assertNotIn("status", docmode.ocr_state(self.prepared(False)))


@unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class ScanEndpointTests(unittest.TestCase):
    def setUp(self):
        users = {"ครูเอ": {"status": "approved", "salt": "a"}, "bee": {"status": "approved", "salt": "b"}}
        for p in (patch.object(webmain, "load_users", return_value=users),
                  patch.object(core, "tesseract_cmd", return_value="tesseract"),
                  patch.object(core, "ocr_name_marks",
                               side_effect=lambda pdf, n, dpi: [(10, 20, 110, 50)] if n == 3 else [])):
            p.start()
            self.addCleanup(p.stop)
        self.a = TestClient(webmain.app, cookies={"session": webmain.make_session("ครูเอ", users["ครูเอ"])})
        self.b = TestClient(webmain.app, cookies={"session": webmain.make_session("bee", users["bee"])})
        self.job = ready_job(self)

    def test_start_follow_and_get_the_new_marks(self):
        url = f"/api/doc/{self.job['id']}/ocr"
        r = self.a.post(url, json={"first": 2, "last": 4})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ready"])
        docmode._ocr_tasks.join()
        d = self.a.get(url).json()
        self.assertEqual((d["status"], d["found"]), ("done", [3]))
        self.assertEqual(d["marks"], [{"page": 2, "x": 10, "y": 20, "w": 100, "h": 30, "ocr": True}])
        self.assertIn("ocr", self.a.get(f"/api/doc/{self.job['id']}").json())

    def test_only_the_owner_and_only_sensible_input(self):
        url = f"/api/doc/{self.job['id']}/ocr"
        self.assertEqual(self.b.post(url, json={"first": 1, "last": 2}).status_code, 404)
        self.assertEqual(TestClient(webmain.app).post(url, json={"first": 1, "last": 2}).status_code, 401)
        self.assertEqual(self.a.post(url, json={"first": "x"}).status_code, 400)
        self.assertEqual(self.a.post(url, json={"first": 8, "last": 9}).status_code, 400)

    def test_server_without_tesseract_says_so(self):
        core.tesseract_cmd.return_value = ""
        r = self.a.post(f"/api/doc/{self.job['id']}/ocr", json={"first": 1, "last": 2})
        self.assertEqual(r.status_code, 503)
        self.assertIn("Tesseract", r.json()["detail"])
        self.assertFalse(self.a.get(f"/api/doc/{self.job['id']}/ocr").json()["ready"])

    def test_stop_button(self):
        r = self.a.post(f"/api/doc/{self.job['id']}/ocr/stop")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
