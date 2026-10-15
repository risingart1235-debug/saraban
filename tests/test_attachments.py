"""News items that come as several files: every page shows and is kept, LINE stops at the signature.

A letter from สพป. often comes as separate attachments - 1. the covering letter, which ends
at the sender's signature, 2. the list of schools, a form, a picture...  Only the first PDF
used to be downloaded, so the screen showed pages up to the signature page and the rest never
reached the saved file.  Now every PDF attachment is put behind the covering letter in one
file (by the server for the phone, which has no PDF tools) - pictures, Word and Excel files
stay links in the LINE message, as the user chose - the AI still reads only the covering
letter, and LINE still sends page 1 to the signature page unless more are ticked.  A PDF that
did not come along (a phone script from before sends the letter alone) is pointed out.
Offline: files are drawn on the spot; the สพป. site, the AI, Drive and LINE are fakes.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

import core
import sppweb
from tests.test_sppweb import FakeResponse, FakeSession

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


def text_pdf(path, lines_per_page):
    """a PDF whose pages carry the given lines of Thai text"""
    doc = pymupdf.open()
    for lines in lines_per_page:
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname="sarabun", fontfile=FONT)
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + 24 * i), line, fontname="sarabun", fontsize=16)
    doc.save(path)
    doc.close()
    return path


def picture(path, size=(1200, 800)):
    Image.new("RGB", size, (200, 220, 240)).save(path)
    return path


def pages_of(path):
    with pymupdf.open(path) as doc:
        return [page.get_text() for page in doc]


LETTER = [["ที่ ศธ ๐๔๑๔๒/ว ๑๒๓๔", "เรียน ผู้อำนวยการโรงเรียนตามบัญชีรายชื่อแนบท้าย"],
          ["ขอแสดงความนับถือ", "(นายผู้อำนวยการ สพป.)"]]
LIST = [["บัญชีรายชื่อแนบท้าย", "๑ บ้านนาดี", "๒ บ้านโพนทองประชาอุทิศ"], ["๓ บ้านท่าแร่"]]


class PageAttachmentTests(unittest.TestCase):
    """which attachments of a news item become pages, in which order"""

    DETAIL = {"main_pdf": "u/letter.pdf", "attachments": [
        {"name": "หนังสือนำ", "url": "u/letter.pdf", "ext": ".pdf"},
        {"name": "บัญชีรายชื่อ", "url": "u/list.pdf", "ext": ".pdf"},
        {"name": "แบบฟอร์ม", "url": "u/form.docx", "ext": ".docx"},
        {"name": "รูปประกอบ", "url": "u/pic.jpg", "ext": ".jpg"},
        {"name": "ภาคผนวก", "url": "u/annex.pdf", "ext": ".pdf"},
        {"name": "ไฟล์บีบอัด", "url": "u/all.zip", "ext": ".zip"},
        {"name": "สแกน", "url": "u/scan.png", "ext": ".png"},
        {"name": "ปฏิทิน", "url": "u/annex2.pdf", "ext": ".pdf"}]}

    def test_only_pdf_files_after_the_letter_in_site_order(self):
        """the user chose PDF only - pictures stay links in the LINE message like Word files"""
        self.assertEqual([a["url"] for a in sppweb.page_attachments(self.DETAIL)],
                         ["u/list.pdf", "u/annex.pdf", "u/annex2.pdf"])

    def test_every_pdf_comes_along_even_a_dozen_of_them(self):
        dozen = {"main_pdf": "0.pdf", "attachments": [
            {"name": str(i), "url": f"{i}.pdf", "ext": ".pdf"} for i in range(13)]}
        self.assertEqual(len(sppweb.page_attachments(dozen)), 12)        # used to stop at ten

    def test_only_an_absurd_number_of_files_is_cut(self):
        many = {"main_pdf": "m.pdf", "attachments": [
            {"name": str(i), "url": f"{i}.pdf", "ext": ".pdf"} for i in range(sppweb.MAX_EXTRA_FILES + 5)]}
        self.assertEqual(len(sppweb.page_attachments(many)), sppweb.MAX_EXTRA_FILES)

    def test_detail_page_records_each_files_type(self):
        detail_url = sppweb.DETAIL_URL.format("77")
        html = """<html><body><h2>รายละเอียดหนังสือ</h2>
          <a href="/files/letter.pdf">1. หนังสือนำ</a>
          <a href="/files/list.PDF?x=1">2. บัญชีรายชื่อ</a>
          <a href="/files/form.docx">3. แบบฟอร์ม</a></body></html>"""
        sess = FakeSession().add("GET", detail_url, FakeResponse(text=html))
        for name in ("letter.pdf", "list.PDF?x=1"):
            sess.add("GET", sppweb.BASE + "files/" + name, FakeResponse(
                content=b"%PDF-1.7\n%%EOF\n", text=None, headers={"Content-Type": "application/pdf"}))
        sess.add("GET", sppweb.BASE + "files/form.docx", FakeResponse(
            content=b"PK word", text=None, headers={"Content-Type": "application/octet-stream"}))
        detail = sppweb.fetch_detail(sess, "77")
        self.assertEqual([a["ext"] for a in detail["attachments"]], [".pdf", ".pdf", ".docx"])
        self.assertEqual([a["name"] for a in sppweb.page_attachments(detail)], ["บัญชีรายชื่อ"])

    def fetch(self, fail=(), sizes=None, budget=sppweb.MAX_DOWNLOAD_BYTES):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        skipped = []

        def download(sess, url, dest, expected_type=None):
            if url in fail:
                raise fail[url]
            with open(dest, "wb") as f:
                f.write(b"x" * (sizes or {}).get(url, 10))

        with patch.object(sppweb, "download", side_effect=download):
            paths = sppweb.download_extras(None, self.DETAIL, folder, budget=budget,
                                           log=skipped.append)
        return [os.path.basename(p) for p in paths], skipped

    def test_a_file_that_will_not_download_is_skipped_not_fatal(self):
        paths, skipped = self.fetch(fail={"u/annex.pdf": sppweb.DownloadError("หน้า error")})
        self.assertEqual(paths, ["extra1.pdf", "extra3.pdf"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("ภาคผนวก", skipped[0])

    def test_files_past_the_size_budget_are_left_as_links(self):
        paths, skipped = self.fetch(sizes={"u/list.pdf": 60, "u/annex.pdf": 50, "u/annex2.pdf": 30},
                                    budget=100)
        self.assertEqual(paths, ["extra1.pdf", "extra3.pdf"])
        self.assertIn("ใหญ่เกิน", skipped[0])

    def test_an_expired_login_stops_like_for_the_letter_itself(self):
        with self.assertRaises(sppweb.SessionExpiredError):
            self.fetch(fail={"u/list.pdf": sppweb.SessionExpiredError("หมดอายุ")})


@unittest.skipIf(pymupdf is None, "ต้องลง pymupdf ก่อน (pip install -r requirements.txt)")
class CombineTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.letter = text_pdf(os.path.join(self.work, "letter.pdf"), LETTER)
        self.list = text_pdf(os.path.join(self.work, "list.pdf"), LIST)
        self.pic = picture(os.path.join(self.work, "pic.png"))
        self.broken = os.path.join(self.work, "broken.pdf")
        with open(self.broken, "wb") as f:
            f.write(b"%PDF-1.4 cut off half way")

    def test_letter_first_then_every_attachment_in_order(self):
        whole = os.path.join(self.work, "whole.pdf")
        main = core.combine_attachments(self.letter, [self.list, self.broken, self.pic], whole)
        self.assertEqual(main, 2)
        text = pages_of(whole)
        self.assertEqual(len(text), 5)                       # 2 + 2 + picture, broken file skipped
        self.assertIn("เรียน", text[0])
        self.assertIn("บัญชีรายชื่อแนบท้าย", text[2])
        with pymupdf.open(whole) as doc:
            self.assertGreater(doc[4].rect.width, doc[4].rect.height)   # wide picture, wide page
            self.assertTrue(doc[4].get_images())
        # the list keeps its text, so the school's name can still be highlighted there
        marks, scans = core.find_name_marks(whole, range(1, 6))
        self.assertEqual(sorted(marks), [3])
        # a signature page or the tail of a list has little text but is not a scan; the picture is
        self.assertEqual(scans, {5})

    def test_ai_copy_is_only_the_covering_letter(self):
        whole = os.path.join(self.work, "whole.pdf")
        core.combine_attachments(self.letter, [self.list], whole)
        head = core.pdf_head(whole, 2, os.path.join(self.work, "ai.pdf"))
        self.assertEqual(len(pages_of(head)), 2)
        self.assertEqual(core.pdf_head(self.letter, 2, os.path.join(self.work, "x.pdf")), self.letter)
        self.assertFalse(os.path.exists(os.path.join(self.work, "x.pdf")))

    def test_page_sizes_without_rendering(self):
        whole = os.path.join(self.work, "whole.pdf")
        core.combine_attachments(self.letter, [self.pic], whole)
        sizes = core.page_sizes(whole, 200)
        self.assertEqual(sizes[1], (1653, 2339))
        self.assertEqual(sizes[3], (2339, 1653))
        rendered = core.render_pdf_page(whole, 3, 200)
        self.assertLessEqual(abs(rendered.width - sizes[3][0]), 1)


def wait_ready(job, timeout=60):
    end = time.time() + timeout
    while job.get("status") not in ("ready", "error", "stored") and time.time() < end:
        time.sleep(0.05)
    return job.get("status")


@unittest.skipIf(docmode is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class ServerFlowTests(unittest.TestCase):
    """the web app, opened from the news list and from the phone queue"""

    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.files = {"u/letter.pdf": text_pdf(os.path.join(self.work, "letter.pdf"), LETTER),
                      "u/list.pdf": text_pdf(os.path.join(self.work, "list.pdf"), LIST),
                      "u/pic.jpg": picture(os.path.join(self.work, "pic.jpg"), (800, 1200))}
        self.ai_pages = []

        def ai(pdf):
            self.ai_pages.append(len(pages_of(pdf)))
            return ("เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ\nขอเชิญประชุม\nเพื่อโปรดทราบ", 4,
                    "ศธ 04142/ว1234", "ประชุม", "15 มกราคม 2570", "สพป.สกลนคร เขต 1", "-")

        def download(sess, url, dest, expected_type=None):
            shutil.copy(self.files[url], dest)

        self.store = MagicMock()
        self.store.doc_records.return_value = {}
        self.store.history_ids.return_value = set()
        self.drive = MagicMock()
        self.drive.upload.return_value = {}
        self.drive.queue_put.return_value = "fid"
        for p in (patch.object(core, "WORK_DIR", self.work),
                  patch.object(core, "OUTPUT_ROOT", os.path.join(self.work, "out")),
                  patch.object(docmode, "generate_kasien_text", side_effect=ai),
                  patch.object(docmode, "get_next_receipt_no", return_value="๕๗"),
                  patch.object(sppweb, "login", return_value=object()),
                  patch.object(sppweb, "list_documents", return_value=[{
                      "book_id": "77", "doc_no": "ศธ 04142/ว1234", "doc_title": "ประชุม",
                      "doc_date": "15 ม.ค. 70", "sender": "สพป."}]),
                  patch.object(sppweb, "fetch_detail", return_value={
                      "book_id": "77", "emoji": "🔵", "main_pdf": "u/letter.pdf",
                      "attachments": [{"name": "หนังสือนำ", "url": "u/letter.pdf", "ext": ".pdf"},
                                      {"name": "บัญชีรายชื่อ", "url": "u/list.pdf", "ext": ".pdf"},
                                      {"name": "แบบฟอร์ม", "url": "u/form.docx", "ext": ".docx"},
                                      {"name": "รูป", "url": "u/pic.jpg", "ext": ".jpg"}]}),
                  patch.object(sppweb, "download", side_effect=download),
                  patch("store.get_store", return_value=self.store),
                  patch.dict(sys.modules, {"drive": self.drive}),
                  patch.dict(os.environ, {"SARABAN_PHONE_TOKEN": "tok"})):
            p.start()
            self.addCleanup(p.stop)

    def opened_from_the_news_list(self):
        job = docmode.start_from_spp("ครูเอ", "77")
        self.addCleanup(lambda: docmode._jobs.pop(job["id"], None))
        self.assertEqual(wait_ready(job), "ready", job.get("error"))
        return job

    def test_every_page_of_every_file_shows_and_the_ai_reads_only_the_letter(self):
        job = self.opened_from_the_news_list()
        self.assertEqual((job["total_pages"], job["main_pages"]), (4, 2))     # the picture stays a link
        self.assertEqual([pg["index"] for pg in job["pages"]], [1, 2, 3, 4])
        self.assertEqual(self.ai_pages, [2])
        self.assertEqual(job["sig_page"], 2)                 # the AI said 4: no such page in the letter
        # pages after the first and the signature page load when scrolled to
        self.assertEqual(job["pages"][3]["png"], f"/api/doc/{job['id']}/page/4")
        self.assertEqual(job["pages"][3]["w"], 1653)
        self.assertEqual(sorted(m["page"] for m in job["marks"]), [2])       # the list, page 3

    def test_saved_file_keeps_every_page_and_line_stops_at_the_signature_page(self):
        job = self.opened_from_the_news_list()
        sent = MagicMock(return_value=True)
        payload = {"stamp": job["stamp"], "boxes": job["boxes"], "date": "", "time": ""}
        with patch.object(docmode, "publish_line_image",
                          side_effect=lambda p, day=None: "https://x/" + os.path.basename(p)), \
                patch.object(docmode, "send_line_with_image", sent):
            result = docmode._finalize_claimed(job, payload, lambda **kw: "๕๗")
        out = os.path.join(core.OUTPUT_ROOT, core.day_folder(), result["filename"])
        self.assertEqual(len(pages_of(out)), 4)
        sent_pages = sorted(u.rsplit("_", 1)[1] for u in sent.call_args.args[1])
        self.assertEqual(sent_pages, ["หน้า๑.jpg", "หน้า๒.jpg"])

    @unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน")
    def test_page_pictures_load_on_demand_for_the_owner_only(self):
        job = self.opened_from_the_news_list()
        users = {"ครูเอ": {"status": "approved", "salt": "a"}, "bee": {"status": "approved", "salt": "b"}}
        with patch.object(webmain, "load_users", return_value=users):
            a = TestClient(webmain.app, cookies={"session": webmain.make_session("ครูเอ", users["ครูเอ"])})
            b = TestClient(webmain.app, cookies={"session": webmain.make_session("bee", users["bee"])})
            r = a.get(f"/api/doc/{job['id']}/page/4")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "image/jpeg")
            self.assertEqual(Image.open(io.BytesIO(r.content)).size, (827, 1170))
            self.assertTrue(os.path.exists(os.path.join(job["dir"], "preview4.jpg")))   # kept
            self.assertEqual(a.get(f"/api/doc/{job['id']}/page/9").status_code, 404)
            self.assertEqual(b.get(f"/api/doc/{job['id']}/page/4").status_code, 404)
            self.assertEqual(TestClient(webmain.app).get(f"/api/doc/{job['id']}/page/4").status_code, 401)

    def phone_submit(self, extras, attach=""):
        client = TestClient(webmain.app)
        files = [("file", ("doc.pdf", open(self.files["u/letter.pdf"], "rb"), "application/pdf"))]
        for name, data, kind in extras:
            files.append(("extra", (name, data, kind)))
        meta = {"book_id": "88", "doc_no": "ศธ 1", "doc_title": "ประชุม", "doc_date": "-",
                "sender": "-", "emoji": "🔵", "attach": attach}
        try:
            r = client.post("/api/phone/submit", headers={"X-Phone-Token": "tok"},
                            files=files, data=meta)
        finally:
            files[0][1][1].close()
        self.assertEqual(r.status_code, 200, r.text)
        job = docmode._jobs[r.json()["job_id"]]
        self.addCleanup(lambda: docmode._jobs.pop(job["id"], None))
        return job

    @unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน")
    def test_phone_sends_the_other_files_and_the_server_puts_them_together(self):
        with open(self.files["u/list.pdf"], "rb") as f:
            listing = f.read()
        with open(self.files["u/pic.jpg"], "rb") as f:
            photo = f.read()
        job = self.phone_submit([("extra1.pdf", listing, "application/pdf"),
                                 ("junk.txt", b"not a document", "text/plain"),
                                 ("extra3.jpg", photo, "image/jpeg")])
        self.assertEqual(job["main_pages"], 2)
        # the junk and the picture (from a phone script of before) are skipped: PDF only
        self.assertEqual(len(pages_of(os.path.join(job["dir"], "doc.pdf"))), 4)
        docmode._drive_tasks.join()
        meta = self.drive.queue_put.call_args.args[1]
        self.assertEqual(meta["main_pages"], 2)             # kept for the restore after a restart
        self.assertEqual(os.listdir(core._w("_incoming")), [])   # nothing left behind
        self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(wait_ready(job), "ready", job.get("error"))
        self.assertEqual(self.ai_pages, [2])

    @unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน")
    def test_phone_script_from_before_still_works_with_one_file(self):
        job = self.phone_submit([])
        self.assertIsNone(job["main_pages"])
        self.assertEqual(len(pages_of(os.path.join(job["dir"], "doc.pdf"))), 2)

    def test_restored_queue_still_knows_where_the_letter_ends(self):
        self.drive.enabled.return_value = True
        self.drive.queue_list.return_value = [{"id": "f1", "meta": {
            "book_id": "99", "doc_no": "ศธ 2", "user": "phone", "main_pages": 3,
            "created": "2026-10-14T09:00:00+07:00"}}]
        self.assertEqual(docmode._restore_from_drive(), 1)
        job = next(j for j in docmode._jobs.values() if j.get("book_id") == "99")
        self.addCleanup(lambda: docmode._jobs.pop(job["id"], None))
        self.assertEqual(job["main_pages"], 3)

    # ---- ไฟล์แนบที่ไม่ได้มาด้วย: หน้าจอเตือน (เดิมหายเงียบ เช่นมือถือยังใช้สคริปต์รุ่นก่อน) ----
    ATTACH = sppweb.attach_text([{"name": "หนังสือนำ", "url": "https://spp/f/letter.pdf"},
                                 {"name": "บัญชีรายชื่อ", "url": "https://spp/f/list.pdf"},
                                 {"name": "บัญชีรายชื่อ ๒", "url": "https://spp/f/list2.pdf"},
                                 {"name": "แบบฟอร์ม", "url": "https://spp/f/form.docx"},
                                 {"name": "รูป", "url": "https://spp/f/pic.jpg"}])

    @unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน")
    def test_phone_script_from_before_sends_no_attachments_and_the_editor_says_so(self):
        job = self.phone_submit([], attach=self.ATTACH)
        self.assertEqual(docmode.missing_extras(job), 2)      # both lists - not the Word file or the picture
        self.assertTrue(docmode.prepare_stored(job))          # opened in the editor
        self.assertEqual(wait_ready(job), "ready", job.get("error"))
        users = {"ครูเอ": {"status": "approved", "salt": "a"}}
        with patch.object(webmain, "load_users", return_value=users):
            a = TestClient(webmain.app, cookies={"session": webmain.make_session("ครูเอ", users["ครูเอ"])})
            self.assertEqual(a.get(f"/api/doc/{job['id']}").json()["missing_extras"], 2)

    @unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน")
    def test_nothing_to_say_when_every_attachment_came(self):
        with open(self.files["u/list.pdf"], "rb") as f:
            listing = f.read()
        with open(self.files["u/pic.jpg"], "rb") as f:
            photo = f.read()
        job = self.phone_submit([("extra1.pdf", listing, "application/pdf"),
                                 ("extra2.pdf", listing, "application/pdf"),
                                 ("extra3.jpg", photo, "image/jpeg")], attach=self.ATTACH)
        self.assertEqual(docmode.missing_extras(job), 0)
        docmode._drive_tasks.join()
        self.assertEqual(self.drive.queue_put.call_args.args[1]["extras_got"], 2)   # kept for a restore

    def test_one_left_out_is_counted(self):
        self.assertEqual(docmode.missing_extras({"attach": self.ATTACH, "main_pages": 2, "extras_got": 1}), 1)

    def test_queue_saved_before_the_count_existed(self):
        old = {"attach": self.ATTACH}                            # restored, no extras_got
        self.assertEqual(docmode.missing_extras(dict(old, main_pages=None)), 2)
        self.assertEqual(docmode.missing_extras(dict(old, main_pages=2)), 0)   # had attachments: trust it

    def test_uploads_and_letters_without_attachments_say_nothing(self):
        self.assertEqual(docmode.missing_extras({"attach": "📥 นำเข้าไฟล์โดยผู้ใช้งาน (Manual Import)"}), 0)
        self.assertEqual(docmode.missing_extras({"attach": sppweb.attach_text(
            [{"name": "หนังสือนำ", "url": "https://spp/f/letter.pdf"},
             {"name": "แบบฟอร์ม", "url": "https://spp/f/form.xlsx"},
             {"name": "รูป", "url": "https://spp/f/pic.png"}])}), 0)       # links on purpose

    def test_opened_from_the_news_list_counts_what_was_downloaded(self):
        job = self.opened_from_the_news_list()
        self.assertEqual(job["extras_got"], 1)                # the list; the picture stays a link
        self.assertEqual(docmode.missing_extras(job), 0)


if __name__ == "__main__":
    unittest.main()
