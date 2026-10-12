"""Pictures sent to the LINE group are kept next to the PDF and still open after a restart.

LINE fetches the picture again when someone opens it later.  The pictures used to live
only on the server's temporary disk plus a list in memory; Render's free instance sleeps
after 15 idle minutes and restarts on every deploy, so within the hour LINE got "not
found" and showed the picture as expired.  Now every picture is saved with the PDF (same
Drive folder, named after the PDF) and the signed link finds it there again.
Offline: the drive module / the Drive API are fakes.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import core
from web import docmode

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):   # server packages not installed (phone-only setup)
    webmain = None

try:
    import googleapiclient.http        # noqa: F401 - drive.py uploads through it
except ImportError:
    googleapiclient = None

DAY = "๒๕๖๙/๑๐ ตุลาคม/๑๒"


class FakeDrive:
    def __init__(self):
        self.on = True
        self.files = []          # {"name", "day", "token", "data"}
        self.lookups = 0

    def enabled(self):
        return self.on

    def line_image_put(self, path, name, day=None, token=""):
        with open(path, "rb") as f:
            self.files.append({"name": name, "day": day, "token": token, "data": f.read()})
        return "id%d" % len(self.files)

    def line_image_get(self, token, dest):
        self.lookups += 1
        hit = next((f for f in self.files if token and f["token"] == token), None)
        if not hit:
            return False
        with open(dest, "wb") as f:
            f.write(hit["data"])
        return True


class LineImageTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.drive = FakeDrive()
        for p in (patch.dict(sys.modules, {"drive": self.drive}),
                  patch.dict(os.environ, {"SARABAN_PUBLIC_URL": "https://saraban.example",
                                          "SARABAN_SESSION_SECRET": "test-secret"}),
                  patch.object(core, "WORK_DIR", self.work),
                  patch.object(docmode, "_LINE_IMG_DIR", os.path.join(self.work, "_lineimg")),
                  patch.object(docmode, "_line_key", None)):
            p.start()
            self.addCleanup(p.stop)
        # รูปหน้าที่ส่ง LINE บันทึกไว้ข้างไฟล์ PDF แล้ว (ชื่อตามไฟล์ PDF + เลขหน้า)
        folder = os.path.join(self.work, "out", DAY)
        os.makedirs(folder)
        self.src = os.path.join(folder, "เลขรับ_๕๗_ศธ 1_หน้า๑.jpg")
        with open(self.src, "wb") as f:
            f.write(b"\xff\xd8 page 1 with the stamp")

    def publish(self):
        url = docmode.publish_line_image(self.src, day=DAY)
        docmode._drive_tasks.join()            # the Drive copy is made in the background
        self.assertTrue(url.startswith("https://saraban.example/api/line-image/"), url)
        return url.rsplit("/", 1)[1][:-len(".jpg")]

    def read(self, token):
        path = docmode.line_image_path(token)
        self.assertTrue(path, "picture not found")
        with open(path, "rb") as f:
            return f.read()

    def test_saved_on_drive_next_to_the_pdf_with_the_pdf_name(self):
        token = self.publish()
        self.assertEqual([(f["name"], f["day"], f["token"]) for f in self.drive.files],
                         [("เลขรับ_๕๗_ศธ 1_หน้า๑.jpg", DAY, token)])
        self.assertEqual(self.read(token), b"\xff\xd8 page 1 with the stamp")

    def test_still_opens_after_the_server_restarts(self):
        token = self.publish()
        shutil.rmtree(self.work)                          # Render wiped the disk ...
        with patch.object(docmode, "_line_key", None):    # ... and works the key out again
            self.assertEqual(self.read(token), b"\xff\xd8 page 1 with the stamp")
            asked = self.drive.lookups
            self.read(token)                              # now served from the local copy again
        self.assertEqual(self.drive.lookups, asked)

    def test_made_up_or_altered_links_never_reach_drive(self):
        token = self.publish()
        flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
        for bad in ("", "abc", flipped, token[:24] + "A" * 16, "x" * 40, token + "x",
                    "../" * 13 + "a", token[:39] + "/"):
            self.assertEqual(docmode.line_image_path(bad), "", bad)
        self.assertEqual(self.drive.lookups, 0)

    def test_running_on_the_school_computer_still_saves_the_picture_with_the_pdf(self):
        with patch.dict(os.environ, {"SARABAN_PUBLIC_URL": "", "RENDER_EXTERNAL_URL": ""}):
            self.assertEqual(docmode.publish_line_image(self.src, day=DAY), "")   # -> imgbb
        docmode._drive_tasks.join()
        self.assertEqual([(f["name"], f["day"], f["token"]) for f in self.drive.files],
                         [("เลขรับ_๕๗_ศธ 1_หน้า๑.jpg", DAY, "")])

    def test_without_drive_it_works_until_the_next_restart_like_before(self):
        self.drive.on = False
        token = self.publish()
        self.assertTrue(docmode.line_image_path(token))
        shutil.rmtree(docmode._LINE_IMG_DIR)
        self.assertEqual(docmode.line_image_path(token), "")
        self.assertEqual(self.drive.files, [])

    def test_server_keeps_few_copies_and_older_ones_come_back_from_drive(self):
        with patch.object(docmode, "_LINE_IMG_KEEP", 3):
            tokens = [self.publish() for _ in range(5)]
            self.assertEqual(len(os.listdir(docmode._LINE_IMG_DIR)), 3)
            for t in tokens:
                self.assertEqual(self.read(t), b"\xff\xd8 page 1 with the stamp")

    @unittest.skipIf(webmain is None, "ต้องลงแพ็กเกจฝั่งเซิร์ฟเวอร์ก่อน (pip install -r requirements.txt)")
    def test_line_can_fetch_it_without_logging_in(self):
        token = self.publish()
        shutil.rmtree(docmode._LINE_IMG_DIR)
        c = TestClient(webmain.app)
        r = c.get(f"/api/line-image/{token}.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertIn("max-age", r.headers["cache-control"])
        self.assertEqual(c.get("/api/line-image/" + "x" * 40 + ".jpg").status_code, 404)


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeDriveFiles:
    """files() ของ Drive API แบบย่อ — เข้าใจคำค้นที่ drive.py ใช้จริงเท่านั้น"""

    def __init__(self):
        self.items = {}          # id -> {name, parent, data, folder, props}
        self.queries = []

    def add(self, name, parent, data=b"", folder=False, props=None):
        fid = "f%d" % (len(self.items) + 1)
        self.items[fid] = {"name": name, "parent": parent, "data": data,
                           "folder": folder, "props": props or {}}
        return fid

    def list(self, q, fields=None, pageSize=100, **kw):
        self.queries.append(q)
        parent = re.search(r"'([^']+)' in parents", q)
        name = re.search(r"name = '([^']*)'", q)
        prop = re.search(r"appProperties has \{ key='([^']+)' and value='([^']*)' \}", q)
        want_folder = "google-apps.folder" in q

        def run():
            hits = [{"id": fid, "name": it["name"]} for fid, it in self.items.items()
                    if it["folder"] == want_folder
                    and (not parent or it["parent"] == parent.group(1))
                    and (not name or it["name"] == name.group(1))
                    and (not prop or it["props"].get(prop.group(1)) == prop.group(2))]
            return {"files": hits[:pageSize]}
        return _Req(run)

    def create(self, body, media_body=None, fields=None, **kw):
        def run():
            data = media_body.getbytes(0, media_body.size()) if media_body else b""
            folder = body.get("mimeType") == "application/vnd.google-apps.folder"
            return {"id": self.add(body["name"], body["parents"][0], data, folder,
                                   body.get("appProperties"))}
        return _Req(run)


@unittest.skipIf(googleapiclient is None, "ต้องลง google-api-python-client ก่อน (pip install -r requirements.txt)")
class DriveStoreTests(unittest.TestCase):
    """the real drive.py functions, against an in-memory Drive"""

    def setUp(self):
        import drive
        self.drive = drive
        self.files = FakeDriveFiles()
        svc = type("Svc", (), {"files": lambda _self: self.files})()

        def fetch(file_id, dest):
            with open(dest, "wb") as f:
                f.write(self.files.items[file_id]["data"])
            return dest

        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        for p in (patch.dict(os.environ, {"SARABAN_DRIVE_UPLOAD": "on", "SARABAN_DRIVE_FOLDER": "root"}),
                  patch.object(drive, "_service", return_value=svc),
                  patch.object(drive, "queue_fetch", side_effect=fetch)):
            p.start()
            self.addCleanup(p.stop)
        self.src = os.path.join(self.work, "a.jpg")
        with open(self.src, "wb") as f:
            f.write(b"jpeg bytes")

    def path_of(self, fid):
        parts, it = [], self.files.items[fid]
        while it["parent"] != "root":
            parts.append(it["name"])
            it = self.files.items[it["parent"]]
        return "/".join([it["name"]] + parts[::-1])

    def test_put_into_the_day_folder_then_find_it_by_the_link_token(self):
        fid = self.drive.line_image_put(self.src, "เลขรับ_๕๗_หน้า๑.jpg", day=DAY, token="T" * 40)
        self.assertEqual(self.path_of(fid), DAY + "/เลขรับ_๕๗_หน้า๑.jpg")   # same folder as the PDF
        dest = os.path.join(self.work, "back.jpg")
        self.assertTrue(self.drive.line_image_get("T" * 40, dest))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"jpeg bytes")
        self.assertFalse(os.path.exists(dest + ".part"))
        self.assertFalse(self.drive.line_image_get("U" * 40, os.path.join(self.work, "x.jpg")))

    def test_pictures_from_before_still_found_by_their_old_name(self):
        old_folder = self.files.add("รูปที่ส่ง LINE", "root", folder=True)
        self.files.add("L" * 40 + ".jpg", old_folder, b"older picture")
        dest = os.path.join(self.work, "old.jpg")
        self.assertTrue(self.drive.line_image_get("L" * 40, dest))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"older picture")

    def test_pictures_saved_from_the_school_computer_carry_no_token(self):
        fid = self.drive.line_image_put(self.src, "เลขรับ_๕๘_หน้า๒.jpg", day=DAY)
        self.assertEqual(self.files.items[fid]["props"], {})


if __name__ == "__main__":
    unittest.main()
