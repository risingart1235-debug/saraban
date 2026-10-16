"""Registered by mistake: ♻️ register a phone document again with the same receipt number.

On Render the สพป. site is blocked, so the server cannot download a document a second time.
The original the phone sent used to be deleted from Drive the moment it was registered, and
the document vanished from the news list - a mistake could not be fixed from the phone at
all.  Now the original stays on Drive for SARABAN_REDO_DAYS days after registering, the list
keeps the document with its receipt number, ♻️ opens it again from that original, saving
overwrites the same register row (no new number) and LINE gets a new message marked as the
correction.  "Cancel" on such a document leaves it registered as it was - it must never be
recorded as skipped.  Offline: the AI, the store, Drive and LINE are fakes.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

import core
import sppweb
import store
from core import now_th

try:
    import pymupdf
except ImportError:         # phone-only setup
    pymupdf = None

try:
    from web import docmode
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    docmode = None

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):
    webmain = None

FONT = core._p("THSarabunNew.ttf")
LETTER = [["ที่ ศธ ๐๔๑๔๒/ว ๑๒๓๔", "เรียน ผู้อำนวยการโรงเรียนบ้านโพนทองประชาอุทิศ"],
          ["ขอแสดงความนับถือ", "(นายผู้อำนวยการ สพป.)"]]


def text_pdf(path, lines_per_page):
    doc = pymupdf.open()
    for lines in lines_per_page:
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname="sarabun", fontfile=FONT)
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + 24 * i), line, fontname="sarabun", fontsize=16)
    doc.save(path)
    doc.close()
    return path


class FakeStore(store.StatusMixin):
    """the Google Sheet: register rows, one record per document, the history"""
    kind = "sheets"

    def __init__(self):
        self.rows, self.records, self.history = [], {}, set()
        self.updated, self.skipped = [], []

    def peek_receipt_no(self):
        return core.to_thai_digits(str(57 + len(self.rows)))

    def register(self, doc_no="", doc_date="", sender="", doc_title="", receive_date=""):
        no = self.peek_receipt_no()
        self.rows.append([no, doc_no, doc_date, sender, "", doc_title, "", "", ""])
        return no

    def registry_rows(self, limit=None):
        return [list(r) for r in self.rows]

    def update_registry_row(self, receipt_no, doc_no="", doc_date="", sender="",
                            doc_title="", receive_date="") -> bool:
        for r in self.rows:
            if r[0] == receipt_no:
                r[1], r[2], r[3], r[5] = doc_no, doc_date, sender, doc_title
                self.updated.append(receipt_no)
                return True
        return False

    def doc_records(self):
        return {k: dict(v) for k, v in self.records.items()}

    def set_record(self, book_id, rec):
        self.records[str(book_id)] = dict(rec)

    def history_ids(self):
        return set(self.history)

    def add_history(self, book_id):
        self.history.add(str(book_id))

    def mark_skipped(self, book_id):
        self.skipped.append(str(book_id))
        super().mark_skipped(book_id)

    def delete_receipt(self, receipt_no):
        for i, r in enumerate(self.rows):
            if r[0] == receipt_no:
                del self.rows[i]
                return {"row": r, "reusable": i == len(self.rows)}
        return {}

    def remove_history(self, book_id):
        had = book_id in self.history or book_id in self.records
        self.history.discard(book_id)
        self.records.pop(book_id, None)
        return had


class LocalStoreWithdrawTests(unittest.TestCase):
    """the file store keeps the history and the records apart: taking a document out of the
    history must take its registered/skipped record too, as the Sheet does (one row)"""

    def test_withdrawn_document_is_new_again(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core, "OUTPUT_ROOT", tmp):
            st = store.LocalStore.__new__(store.LocalStore)
            st._lock = MagicMock()
            st._hist = os.path.join(tmp, "history_ids.txt")
            st.mark_registered("88", "๕๗")
            st.mark_skipped("90")
            self.assertTrue(st.remove_history("88"))
            self.assertNotIn("88", st.history_ids())
            self.assertNotIn("88", st.doc_records())
            self.assertEqual(st.status_of([{"book_id": "88"}])[0]["status"], store.NEW)
            self.assertEqual(st.doc_records()["90"]["status"], "skipped")   # the others stay
            self.assertFalse(st.remove_history("404"))


def settle(job, timeout=60):
    end = time.time() + timeout
    while job.get("status") in ("queued", "analyzing") and time.time() < end:
        time.sleep(0.05)
    return job.get("status")


class RedoBase(unittest.TestCase):
    """a phone document, the AI, the Sheet, Drive and LINE - and the steps a user takes"""

    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.letter = text_pdf(os.path.join(self.work, "letter.pdf"), LETTER)
        self.kasien = ["ทราบ แจ้งครูทุกคน", "มอบครูวิชาการ"]     # what the AI writes, read in turn

        def ai(pdf):
            return (self.kasien[0], 2, "ศธ 04142/ว1234", "ประชุม", "15 มกราคม 2570",
                    "สพป.สกลนคร เขต 1", "-")

        self.store = FakeStore()
        self.drive = MagicMock()
        self.drive.enabled.return_value = True
        self.drive.ready.return_value = {"ok": True}
        self.drive.upload.return_value = {}
        self.drive.queue_put.side_effect = self.backed_up
        self.drive.queue_drop.return_value = True
        self.drive.queue_fetch.side_effect = lambda fid, dest: shutil.copy(self.on_drive[fid], dest)
        self.on_drive, self.metas = {}, {}
        self.line = MagicMock(return_value=True)
        for p in (patch.object(core, "WORK_DIR", self.work),
                  patch.object(core, "OUTPUT_ROOT", os.path.join(self.work, "out")),
                  patch.object(docmode, "generate_kasien_text", side_effect=ai),
                  patch.object(docmode, "get_next_receipt_no", return_value="๕๗"),
                  patch.object(docmode, "publish_line_image", return_value="https://x/p.jpg"),
                  patch.object(docmode, "send_line_with_image", self.line),
                  patch("store.get_store", return_value=self.store),
                  patch.dict(sys.modules, {"drive": self.drive}),
                  patch.dict(os.environ, {"SARABAN_PHONE_TOKEN": "tok"})):
            p.start()
            self.addCleanup(p.stop)
        before = set(docmode._jobs)
        self.addCleanup(lambda: [docmode._jobs.pop(j, None) for j in set(docmode._jobs) - before])

    def backed_up(self, pdf, meta):
        fid = "f" + meta["book_id"]
        if fid in self.on_drive:                    # sent again: Drive hands out a new id
            fid += "-%d" % len(self.on_drive)
        self.on_drive[fid] = shutil.copy(pdf, os.path.join(self.work, fid + ".pdf"))
        self.metas[fid] = dict(meta)
        return fid

    # ---- ขั้นตอนเหมือนผู้ใช้จริง ----
    def from_phone(self, book_id="88"):
        src = shutil.copy(self.letter, os.path.join(self.work, f"sent{book_id}.pdf"))
        job, created = docmode.start_from_phone_path("phone", src, {
            "book_id": book_id, "doc_no": "ศธ 04142/ว1234", "doc_title": "ประชุม",
            "doc_date": "15 ม.ค. 70", "sender": "สพป.", "emoji": "🔵", "attach": ""})
        self.assertTrue(created)
        docmode._drive_tasks.join()
        return job

    def open_and_save(self, job):
        if job["status"] == "stored":
            self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        payload = {"stamp": job["stamp"], "boxes": job["boxes"], "date": "", "time": ""}
        result = docmode.finalize(job, payload, lambda **kw: self.store.register(**kw))
        docmode._drive_tasks.join()
        return result

    def registered(self, book_id="88"):
        job = self.from_phone(book_id)
        self.open_and_save(job)
        self.kasien.pop(0)
        return job


@unittest.skipIf(docmode is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class RedoTests(RedoBase):
    # ---- ลงรับแล้วยังเก็บต้นฉบับไว้ ----
    def test_registering_keeps_the_original_and_the_document_stays_listed(self):
        job = self.registered()
        self.assertEqual((job["status"], job["receipt_no"]), ("done", "๕๗"))
        self.drive.queue_drop.assert_not_called()          # used to be deleted right here
        self.assertEqual(job["drive_file_id"], "f88")
        self.assertEqual([r["book_id"] for r in docmode.phone_kept()], ["88"])
        self.assertNotIn("88", [r["book_id"] for r in docmode.phone_queue()])

    def test_redo_uses_the_same_number_and_overwrites_the_row(self):
        job = self.registered()
        again = docmode.reopen_phone("88", "๕๗")
        self.assertIs(again, job)
        self.assertEqual((job["status"], job["redo_no"]), ("stored", "๕๗"))
        self.assertNotIn("reserved_receipt", job)          # left over, the row would not be rewritten
        self.assertNotIn("pages", job)

        def no_new_number(**kw):
            raise AssertionError("ลงรับใหม่ต้องไม่กินเลขใหม่")

        self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        self.assertEqual(job["receipt_no"], "๕๗")
        self.assertEqual(job["boxes"][0]["text"], "มอบครูวิชาการ")   # the AI read it afresh
        payload = {"stamp": job["stamp"], "boxes": job["boxes"], "date": "", "time": ""}
        result = docmode.finalize(job, payload, no_new_number)
        self.assertEqual(result["receipt_no"], "๕๗")
        self.assertEqual(self.store.updated, ["๕๗"])
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.records["88"]["status"], "registered")
        message = self.line.call_args.args[0]
        self.assertTrue(message.startswith("♻️ ลงรับใหม่ (แก้ไข)"), message)
        self.assertIn("📌 เลขที่รับ ๕๗", message)
        self.assertNotIn("♻️", self.line.call_args_list[0].args[0])   # the first message was plain
        docmode._drive_tasks.join()
        self.drive.queue_drop.assert_not_called()          # kept: can be redone again
        self.assertEqual(job["status"], "done")

    def test_reopening_twice_opens_the_same_work(self):
        job = self.registered()
        docmode.reopen_phone("88", "๕๗")
        self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        job["boxes"][0]["text"] = "แก้ไว้บนจอแล้ว"
        self.assertIs(docmode.reopen_phone("88", "๕๗"), job)   # another phone pressed ♻️ too
        self.assertEqual(job["boxes"][0]["text"], "แก้ไว้บนจอแล้ว")

    def test_cancel_leaves_it_registered_never_skipped(self):
        job = self.registered()
        docmode.reopen_phone("88", "๕๗")
        self.assertTrue(docmode.prepare_stored(job))
        self.assertEqual(settle(job), "ready", job.get("error"))
        result = docmode.skip(job)
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.store.skipped, [])
        self.assertEqual(self.store.records["88"]["status"], "registered")
        self.assertEqual((job["status"], job["receipt_no"]), ("done", "๕๗"))
        self.assertEqual([r["book_id"] for r in docmode.phone_kept()], ["88"])
        self.drive.queue_drop.assert_not_called()

    def test_cancelling_a_redo_from_the_spp_site_does_not_record_a_skip_either(self):
        job = {"id": "spp-redo", "source": "spp", "book_id": "70", "redo_no": "๑๒",
               "status": "ready", "created": now_th()}
        self.assertTrue(docmode.skip(job)["cancelled"])
        self.assertEqual(job["status"], "skipped")
        self.assertEqual(self.store.skipped, [])

    # ---- ข้ามแล้วก็เก็บต้นฉบับไว้ ๗ วัน ข้ามผิดลงรับได้อีก ----
    def test_skipping_keeps_the_original_too(self):
        """it used to be deleted on the spot: skipped by mistake = ask the phone to fetch it again"""
        job = self.from_phone("90")
        docmode.skip(job)
        docmode._drive_tasks.join()
        self.assertEqual(self.store.skipped, ["90"])
        self.drive.queue_drop.assert_not_called()
        self.assertEqual(job["status"], "skipped")
        self.assertEqual([(r["book_id"], r["status"]) for r in docmode.phone_kept()],
                         [("90", "skipped")])

    def test_skipped_by_mistake_is_registered_after_all(self):
        job = self.from_phone("90")
        docmode.skip(job)
        self.assertIs(docmode.reopen_phone("90"), job)        # 📥 ลงรับเรื่องนี้ in the list
        self.assertEqual((job["status"], job["redo_no"]), ("stored", None))
        self.open_and_save(job)                                  # the skip used to block the save
        self.assertEqual(self.store.records["90"]["status"], "registered")
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(job["status"], "done")

    def test_a_skip_made_elsewhere_does_not_stop_opening_it_from_the_queue(self):
        job = self.from_phone("90")
        self.store.mark_skipped("90")                            # ข้าม on another computer
        self.assertTrue(docmode.prepare_stored(job))             # used to end in "ข้ามแล้ว"
        self.assertEqual(settle(job), "ready", job.get("error"))

    def test_registered_elsewhere_still_stops_a_second_number(self):
        job = self.from_phone("90")
        self.store.mark_registered("90", "๙๙")
        self.assertFalse(docmode.prepare_stored(job))
        self.assertIn("ลงรับแล้ว (เลขรับ ๙๙)", job["error"])
        self.assertIn("♻️", job["error"])                         # kept: ♻️ in the list works
        self.drive.queue_drop.assert_not_called()
        self.assertIsNone(docmode.reopen_phone("90"))             # not a second registration…
        self.assertIs(docmode.reopen_phone("90", "๙๙"), job)      # …only ♻️ with its number

    def test_nothing_to_reopen_without_an_original(self):
        self.assertIsNone(docmode.reopen_phone("404", "๕๗"))       # never came from the phone
        job = self.registered()
        job["drive_file_id"] = ""
        os.remove(os.path.join(job["dir"], "doc.pdf"))
        self.assertIsNone(docmode.reopen_phone("88", "๕๗"))
        self.assertEqual(docmode.phone_kept(), [])

    def test_fresh_registration_only_once_the_record_is_withdrawn(self):
        job = self.registered()
        self.assertIsNone(docmode.reopen_phone("88"))               # still registered: ♻️ only
        del self.store.records["88"]                               # 🔧 ถอยเลข + ถอน ID
        self.store.history.discard("88")
        self.assertIs(docmode.reopen_phone("88"), job)
        self.assertIsNone(job["redo_no"])
        self.assertEqual(job["status"], "stored")

    # ---- ถอนออกจากประวัติที่หน้า 🔧 ถอยเลข แล้วให้มือถือส่งกลับมา ----
    def withdraw(self, book_id="88"):
        """🔧 ถอยเลข + ถอน ID — on the Sheet the record and the history are one row"""
        self.store.records.pop(book_id, None)
        self.store.history.discard(book_id)
        self.store.rows.clear()

    def test_phone_can_send_a_withdrawn_document_again(self):
        """it used to answer "จัดการไปแล้วโดยอีกเครื่อง" until the server happened to restart"""
        old = self.registered()
        self.withdraw()
        self.assertNotIn("88", docmode.phone_held_ids())
        new = self.from_phone()
        self.assertIsNot(new, old)
        self.assertNotIn(old["id"], docmode._jobs)
        self.assertEqual(new["status"], "stored")
        self.assertEqual([r["book_id"] for r in docmode.phone_queue()], ["88"])
        self.assertEqual(docmode.phone_kept(), [])
        self.assertFalse(os.path.exists(old["dir"]))
        self.drive.queue_drop.assert_called_once_with("f88")     # the copy kept for ♻️
        self.assertEqual(new["drive_file_id"], "f88-1")
        self.open_and_save(new)                                   # registered afresh
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.store.records["88"]["status"], "registered")

    def test_a_document_still_on_record_is_still_refused(self):
        self.registered()
        with self.assertRaises(docmode.AlreadyHandledError):
            self.from_phone()
        self.store.records.pop("88")                  # history alone still counts (older data)
        with self.assertRaises(docmode.AlreadyHandledError):
            self.from_phone()

    def test_a_withdrawn_skip_comes_back_too(self):
        docmode.skip(self.from_phone("90"))
        self.withdraw("90")
        self.assertEqual(self.from_phone("90")["status"], "stored")

    # ---- เซิร์ฟเวอร์เกิดใหม่ / พ้นกำหนดเก็บ ----
    def restart(self):
        with docmode._lock:
            gone = [j for j in docmode._jobs.values() if j.get("source") == "phone"]
            for j in gone:
                docmode._jobs.pop(j["id"])
        for j in gone:
            shutil.rmtree(j["dir"], ignore_errors=True)
        self.drive.queue_list.return_value = [{"id": fid, "meta": meta}
                                              for fid, meta in self.metas.items()
                                              if fid not in self.dropped()]
        return docmode._restore_from_drive()

    def dropped(self):
        return {c.args[0] for c in self.drive.queue_drop.call_args_list}

    def test_after_a_restart_it_comes_back_registered_and_can_be_redone(self):
        job = self.registered()
        self.assertEqual(self.restart(), 1)
        back = docmode._jobs[job["id"]]
        self.assertEqual((back["status"], back["receipt_no"]), ("done", "๕๗"))
        self.assertEqual([r["book_id"] for r in docmode.phone_kept()], ["88"])
        self.assertNotIn("88", [r["book_id"] for r in docmode.phone_queue()])
        self.assertIs(docmode.reopen_phone("88", "๕๗"), back)
        self.open_and_save(back)                               # the file comes back from Drive
        self.drive.queue_fetch.assert_called_once()
        self.assertEqual(self.store.updated, ["๕๗"])

    def test_restore_tidies_up_what_cannot_or_need_not_be_redone(self):
        old = (now_th() - timedelta(days=docmode.REDO_DAYS + 1)).strftime("%Y-%m-%d %H:%M")
        recent = (now_th() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        self.store.records = {
            "61": {"status": "registered", "receipt_no": "๑", "when": old},
            "62": {"status": "skipped", "receipt_no": "", "when": recent},
            "63": {"status": "registered", "receipt_no": "๓", "when": recent},
            "65": {"status": "registered", "receipt_no": "", "when": recent}}   # no number known
        self.store.history = {"61", "62", "63", "65", "66"}                    # 66: history only
        created = now_th().isoformat()
        self.drive.queue_list.return_value = [
            {"id": "f" + b, "meta": {"book_id": b, "doc_no": "ศธ " + b, "created": created}}
            for b in ("61", "62", "63", "64", "65", "66")]
        self.assertEqual(docmode._restore_from_drive(), 3)
        self.assertEqual(self.dropped(), {"f61", "f65", "f66"})
        status = {j["book_id"]: j["status"] for j in docmode._jobs.values()
                  if j.get("book_id") in ("62", "63", "64")}
        self.assertEqual(status, {"62": "skipped", "63": "done", "64": "stored"})

    def test_kept_originals_are_deleted_once_the_days_are_up(self):
        job = self.registered()
        job["finished_at"] = now_th() - timedelta(days=docmode.REDO_DAYS, minutes=1)
        self.assertEqual(docmode.phone_kept(), [])
        docmode._drive_tasks.join()
        self.drive.queue_drop.assert_called_once_with("f88")
        self.assertNotIn(job["id"], docmode._jobs)
        self.assertFalse(os.path.exists(job["dir"]))

    def test_zero_days_deletes_on_registering_like_before(self):
        with patch.object(docmode, "REDO_DAYS", 0):
            job = self.registered()
        self.drive.queue_drop.assert_called_once_with("f88")
        self.assertEqual(job["status"], "done")

    def test_finished_before_its_backup_went_up_is_still_kept(self):
        job = {"id": "early", "book_id": "91", "status": "done", "source": "phone",
               "created": now_th(), "dir": self.work}
        skipped = dict(job, id="gone", book_id="92", status="skipped")
        docmode._backup_to_drive(job, self.letter)
        docmode._backup_to_drive(skipped, self.letter)
        docmode._drive_tasks.join()
        self.assertEqual(job.get("drive_file_id"), "f91")
        self.assertEqual(skipped.get("drive_file_id"), "f92")


@unittest.skipIf(webmain is None or pymupdf is None,
                 "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
class RedoOnTheWebTests(RedoBase):
    """the same, through the pages: the news list on Render, ♻️, cancel, the phone's history"""

    USERS = {"ครูเอ": {"status": "approved", "salt": "a"}}

    def setUp(self):
        super().setUp()
        for p in (patch.object(webmain, "load_users", return_value=self.USERS),
                  patch.object(webmain, "_with_spp_session",
                               side_effect=sppweb.LoginError("Cloudflare 403 — บล็อก IP ศูนย์ข้อมูล"))):
            p.start()
            self.addCleanup(p.stop)
        docmode._queue_restored.set()
        self.web = TestClient(webmain.app, cookies={
            "session": webmain.make_session("ครูเอ", self.USERS["ครูเอ"])})

    def listed(self):
        r = self.web.get("/api/spp/list")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json(), {d["book_id"]: d for g in r.json()["days"] for d in g["docs"]}

    def test_render_list_keeps_registered_phone_documents_with_their_number(self):
        self.registered()
        data, docs = self.listed()
        self.assertEqual((docs["88"]["status"], docs["88"]["receipt_no"]), ("registered", "๕๗"))
        self.assertNotIn("job_id", docs["88"])        # always opened through /api/doc/open
        self.assertEqual(data["redo_days"], docmode.REDO_DAYS)
        self.assertEqual(data["total"]["registered"], 1)

    def test_redo_button_reopens_the_kept_original(self):
        job = self.registered()
        with patch.object(docmode, "start_from_spp") as spp:
            r = self.web.post("/api/doc/open", json={"book_id": "88", "redo_no": "๕๗"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["job_id"], job["id"])
        spp.assert_not_called()                       # blocked on Render anyway
        d = self.web.get(f"/api/doc/{job['id']}").json()
        self.assertEqual(d["status"], "stored")
        self.assertEqual(self.web.post(f"/api/doc/{job['id']}/prepare").status_code, 200)
        self.assertEqual(settle(job), "ready", job.get("error"))
        d = self.web.get(f"/api/doc/{job['id']}").json()
        self.assertEqual((d["redo_no"], d["receipt_no"]), ("๕๗", "๕๗"))
        r = self.web.post(f"/api/doc/{job['id']}/skip")
        self.assertTrue(r.json()["cancelled"])
        self.assertEqual(self.listed()[1]["88"]["status"], "registered")

    def test_redo_with_a_number_no_longer_in_the_register_is_refused_up_front(self):
        self.registered()
        self.store.rows.clear()                       # 🔧 ถอยเลข took the row out
        with patch.object(docmode, "start_from_spp") as spp:
            r = self.web.post("/api/doc/open", json={"book_id": "88", "redo_no": "๕๗"})
        self.assertEqual(r.status_code, 404)
        self.assertIn("ไม่พบเลขรับ ๕๗", r.json()["detail"])
        spp.assert_not_called()

    def test_a_document_the_server_does_not_hold_still_goes_to_the_spp_site(self):
        with patch.object(docmode, "start_from_spp", return_value={"id": "x"}) as spp:
            r = self.web.post("/api/doc/open", json={"book_id": "123", "redo_no": None})
        self.assertEqual(r.json(), {"job_id": "x"})
        spp.assert_called_once()

    def withdraw(self, book_id="88"):
        self.store.records.pop(book_id, None)
        self.store.history.discard(book_id)
        self.store.rows.clear()

    def phone_sends(self, book_id="88"):
        with open(self.letter, "rb") as f:
            return TestClient(webmain.app).post(
                "/api/phone/submit", headers={"X-Phone-Token": "tok"},
                files=[("file", ("doc.pdf", f, "application/pdf"))],
                data={"book_id": book_id, "doc_no": "ศธ 04142/ว1234", "doc_title": "ประชุม"})

    def test_phone_sends_a_withdrawn_document_back_into_the_queue(self):
        """what the user saw: "ยังไม่ลงรับ 1 เรื่อง … จัดการไปแล้วโดยอีกเครื่อง" (HTTP 409)"""
        self.registered()
        self.withdraw()
        d = TestClient(webmain.app).get("/api/phone/history", headers={"X-Phone-Token": "tok"}).json()
        self.assertNotIn("88", d["done"])
        self.assertNotIn("88", d["queued"])                    # the new phone script fetches it too
        r = self.phone_sends()
        self.assertEqual(r.status_code, 200, r.text)          # used to be 409
        self.assertTrue(r.json()["created"])
        doc = self.listed()[1]["88"]
        self.assertEqual(doc["status"], "new")
        self.assertEqual(doc["job_id"], r.json()["job_id"])
        self.assertEqual(self.phone_sends().status_code, 200)  # sent twice: the same queue item

    def test_withdrawn_document_opens_from_the_list_without_the_phone(self):
        job = self.registered()
        self.withdraw()
        docs = self.listed()[1]
        self.assertEqual(docs["88"]["status"], "new")          # still listed, from the kept copy
        r = self.web.post("/api/doc/open", json={"book_id": "88", "redo_no": None})
        self.assertEqual(r.json()["job_id"], job["id"])
        self.assertEqual((job["status"], job["redo_no"]), ("stored", None))   # a new number

    # ---- ถอยเลขที่หน้า 🔧 = ลบเรื่อง แล้วลงรับใหม่ได้เลย ----
    def test_undoing_the_number_puts_the_document_back_to_register_again(self):
        """it used to need the book ID typed into the box below as well - nobody knows that number"""
        job = self.registered()
        r = self.web.post("/api/fix/undo", json={"receipt_no": "๕๗"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["deleted"]["reusable"])
        self.assertEqual((d["book_id"], d["unmarked"], d["kept"]), ("88", True, True))
        self.assertNotIn("88", self.store.records)
        self.assertEqual(self.listed()[1]["88"]["status"], "new")
        r = self.web.post("/api/doc/open", json={"book_id": "88", "redo_no": None})
        self.assertEqual(r.json()["job_id"], job["id"])
        self.open_and_save(job)
        self.assertEqual([row[0] for row in self.store.rows], ["๕๗"])   # the number again

    def test_undoing_a_duplicate_row_keeps_the_letter_registered(self):
        self.registered()
        first = list(self.store.rows[0])
        self.store.rows.append(["๕๘"] + first[1:])               # the same letter twice
        self.store.records["88"]["receipt_no"] = "๕๘"
        d = self.web.post("/api/fix/undo", json={"receipt_no": "๕๘"}).json()
        self.assertNotIn("book_id", d)                            # still registered, row ๕๗
        rec = self.store.records["88"]
        self.assertEqual((rec["status"], rec["receipt_no"]), ("registered", "๕๗"))
        self.assertEqual(self.listed()[1]["88"]["receipt_no"], "๕๗")

    def test_undo_by_book_id_still_works_on_its_own(self):
        self.registered()
        d = self.web.post("/api/fix/undo", json={"book_id": "88"}).json()
        self.assertTrue(d["unmarked"])
        self.assertEqual(len(self.store.rows), 1)                 # the register is left alone

    # ---- ข้ามจากหน้ารายการ แล้วลงรับได้อีก ----
    def test_skip_button_in_the_list_keeps_it_and_it_can_be_registered_later(self):
        job = self.from_phone("90")
        r = self.web.post("/api/spp/skip", json={"book_id": "90"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(job["status"], "skipped")               # the queue item itself, not only the Sheet
        doc = self.listed()[1]["90"]
        self.assertEqual(doc["status"], "skipped")
        self.assertNotIn("job_id", doc)
        r = self.web.post("/api/doc/open", json={"book_id": "90", "redo_no": None})
        self.assertEqual(r.json()["job_id"], job["id"])
        self.open_and_save(job)
        self.assertEqual(self.store.records["90"]["status"], "registered")

    def test_phone_does_not_count_registered_documents_as_waiting(self):
        self.registered()
        waiting = self.from_phone("89")
        r = TestClient(webmain.app).get("/api/phone/history", headers={"X-Phone-Token": "tok"})
        d = r.json()
        self.assertIn("88", d["done"])
        self.assertEqual(d["queued"], [waiting["book_id"]])


if __name__ == "__main__":
    unittest.main()
