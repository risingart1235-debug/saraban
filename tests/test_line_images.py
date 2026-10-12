"""Pictures sent to the LINE group must still open after the server restarts.

LINE fetches the picture again when someone opens it later.  The pictures used to live
only on the server's temporary disk plus a list in memory; Render's free instance sleeps
after 15 idle minutes and restarts on every deploy, so within the hour LINE got "not
found" and showed the picture as expired.  Now each picture is also kept on Drive behind
a signed link.  Offline: the drive module is a fake.
"""
from __future__ import annotations

import os
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


class FakeDrive:
    def __init__(self):
        self.on = True
        self.files = {}
        self.lookups = 0
        self.pruned = []

    def enabled(self):
        return self.on

    def line_image_put(self, path, name):
        with open(path, "rb") as f:
            self.files[name] = f.read()
        return "id-" + name

    def line_image_get(self, name, dest):
        self.lookups += 1
        if name not in self.files:
            return False
        with open(dest, "wb") as f:
            f.write(self.files[name])
        return True

    def line_images_prune(self, days):
        self.pruned.append(days)
        return 0


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
                  patch.object(docmode, "_line_key", None),
                  patch.object(docmode, "_line_pruned_at", 0.0)):
            p.start()
            self.addCleanup(p.stop)
        self.src = os.path.join(self.work, "line1.jpg")
        with open(self.src, "wb") as f:
            f.write(b"\xff\xd8 page 1 with the stamp")

    def publish(self):
        url = docmode.publish_line_image(self.src)
        docmode._drive_tasks.join()            # the Drive copy is made in the background
        self.assertTrue(url.startswith("https://saraban.example/api/line-image/"), url)
        return url.rsplit("/", 1)[1][:-len(".jpg")]

    def read(self, token):
        path = docmode.line_image_path(token)
        self.assertTrue(path, "picture not found")
        with open(path, "rb") as f:
            return f.read()

    def test_opens_right_after_sending(self):
        token = self.publish()
        self.assertEqual(self.read(token), b"\xff\xd8 page 1 with the stamp")
        self.assertEqual(list(self.drive.files), [token + ".jpg"])

    def test_still_opens_after_the_server_restarts(self):
        token = self.publish()
        shutil.rmtree(docmode._LINE_IMG_DIR)          # Render wiped the disk
        with patch.object(docmode, "_line_key", None):  # and the key is worked out again
            self.assertEqual(self.read(token), b"\xff\xd8 page 1 with the stamp")
            asked = self.drive.lookups
            self.read(token)                           # now served from the local copy again
        self.assertEqual(self.drive.lookups, asked)

    def test_made_up_or_altered_links_never_reach_drive(self):
        token = self.publish()
        flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
        for bad in ("", "abc", flipped, token[:24] + "A" * 16, "x" * 40, token + "x",
                    "../" * 13 + "a", token[:39] + "/"):
            self.assertEqual(docmode.line_image_path(bad), "", bad)
        self.assertEqual(self.drive.lookups, 0)

    def test_without_drive_it_works_until_the_next_restart_like_before(self):
        self.drive.on = False
        token = self.publish()
        self.assertTrue(docmode.line_image_path(token))
        shutil.rmtree(docmode._LINE_IMG_DIR)
        self.assertEqual(docmode.line_image_path(token), "")
        self.assertEqual(self.drive.files, {})

    def test_server_keeps_few_copies_and_older_ones_come_back_from_drive(self):
        with patch.object(docmode, "_LINE_IMG_KEEP", 3):
            tokens = [self.publish() for _ in range(5)]
            self.assertEqual(len(os.listdir(docmode._LINE_IMG_DIR)), 3)
            for t in tokens:
                self.assertEqual(self.read(t), b"\xff\xd8 page 1 with the stamp")

    def test_old_pictures_are_cleared_from_drive_at_most_twice_a_day(self):
        self.publish()
        self.publish()
        self.assertEqual(self.drive.pruned, [docmode.LINE_IMG_DAYS])

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
        self.items = {}          # id -> {name, parent, created, data, folder}
        self.queries = []
        self.deleted = []

    def add(self, name, parent, created, data=b"", folder=False):
        fid = "f%d" % (len(self.items) + 1)
        self.items[fid] = {"name": name, "parent": parent, "created": created,
                           "data": data, "folder": folder}
        return fid

    def list(self, q, fields=None, pageSize=100, **kw):
        import re as _re
        self.queries.append(q)
        parent = _re.search(r"'([^']+)' in parents", q).group(1)
        name = _re.search(r"name = '([^']*)'", q)
        before = _re.search(r"createdTime < '([^']+)'", q)
        want_folder = "google-apps.folder" in q

        def run():
            hits = [{"id": fid, "name": it["name"]} for fid, it in self.items.items()
                    if it["parent"] == parent and it["folder"] == want_folder
                    and (not name or it["name"] == name.group(1))
                    and (not before or it["created"] < before.group(1))]
            return {"files": hits[:pageSize]}
        return _Req(run)

    def create(self, body, media_body=None, fields=None, **kw):
        def run():
            data = media_body.getbytes(0, media_body.size()) if media_body else b""
            folder = body.get("mimeType") == "application/vnd.google-apps.folder"
            return {"id": self.add(body["name"], body["parents"][0], "2026-10-12T00:00:00",
                                   data, folder)}
        return _Req(run)

    def delete(self, fileId, **kw):
        def run():
            self.deleted.append(fileId)
            self.items.pop(fileId)
            return {}
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

    def test_put_then_get_back_after_a_restart(self):
        src = os.path.join(self.work, "a.jpg")
        with open(src, "wb") as f:
            f.write(b"jpeg bytes")
        self.assertTrue(self.drive.line_image_put(src, "tok.jpg"))
        folders = [it for it in self.files.items.values() if it["folder"]]
        self.assertEqual([(f["name"], f["parent"]) for f in folders], [("รูปที่ส่ง LINE", "root")])
        dest = os.path.join(self.work, "back.jpg")
        self.assertTrue(self.drive.line_image_get("tok.jpg", dest))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), b"jpeg bytes")
        self.assertFalse(os.path.exists(dest + ".part"))
        self.assertFalse(self.drive.line_image_get("other.jpg", os.path.join(self.work, "x.jpg")))
        self.drive.line_image_put(src, "tok2.jpg")      # same folder, not a second one
        self.assertEqual(len([it for it in self.files.items.values() if it["folder"]]), 1)

    def test_prune_deletes_only_old_pictures_page_after_page(self):
        folder = self.files.add("รูปที่ส่ง LINE", "root", "2026-01-01T00:00:00", folder=True)
        for i in range(150):
            self.files.add(f"old{i}.jpg", folder, "2026-01-01T00:00:00")
        self.files.add("new.jpg", folder, "2099-01-01T00:00:00")
        self.files.add("elsewhere.jpg", "root", "2026-01-01T00:00:00")
        self.assertEqual(self.drive.line_images_prune(90), 150)
        left = sorted(it["name"] for it in self.files.items.values() if not it["folder"])
        self.assertEqual(left, ["elsewhere.jpg", "new.jpg"])
        self.assertEqual(self.drive.line_images_prune(0), 0)     # 0 = keep forever


if __name__ == "__main__":
    unittest.main()
