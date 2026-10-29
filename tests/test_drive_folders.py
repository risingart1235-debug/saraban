"""One folder of each name on Drive, whoever makes it.

The Drive ended up with two "๒๕๖๙" and four "คิวรอลงรับ".  The app's key (drive.file) sees
only the folders the app made itself, so a folder the school computer made (Drive for
Desktop syncs it up) is invisible to the server, which then makes a second one with the
same name.  Two threads that both found nothing made two queue folders in the same minute,
and with twins Drive handed back whichever it liked, so queue files were spread over both.
Offline: an in-memory Drive and a temporary output folder.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import core
from tests.test_line_images import FakeDriveFiles, googleapiclient

DAY = "๒๕๖๙/๑๐ ตุลาคม/๒๙"


@unittest.skipIf(googleapiclient is None, "ต้องลง google-api-python-client ก่อน (pip install -r requirements.txt)")
class DriveFolderTests(unittest.TestCase):
    def setUp(self):
        import drive
        self.drive = drive
        self.files = FakeDriveFiles()
        svc = type("Svc", (), {"files": lambda _self: self.files})()
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, True)
        os.makedirs(os.path.join(self.out, drive.QUEUE_FOLDER_NAME))    # synced down: this is Drive's copy
        # the school computer: has the key, does not upload (Drive for Desktop syncs the folder)
        for p in (patch.dict(os.environ, {"SARABAN_DRIVE_FOLDER": "root"}),
                  patch.object(drive, "enabled", return_value=False),
                  patch.object(drive, "_service", return_value=svc),
                  patch.object(drive, "oauth_info", return_value={"refresh_token": "r"}),
                  patch.object(core, "OUTPUT_ROOT", self.out)):
            p.start()
            self.addCleanup(p.stop)

    def folders(self):
        """every folder on Drive as a path, e.g. "๒๕๖๙/๑๐ ตุลาคม" """
        def path(it):
            return it["name"] if it["parent"] == "root" else path(self.files.items[it["parent"]]) + "/" + it["name"]
        return sorted(path(it) for it in self.files.items.values() if it["folder"])

    def local(self, rel):
        return os.path.join(self.out, *rel.split("/"))

    # --- the server finding its own folders ---

    def test_of_two_folders_with_one_name_the_first_made_is_always_used(self):
        first = self.files.add("คิวรอลงรับ", "root", folder=True)
        self.files.add("คิวรอลงรับ", "root", folder=True)
        self.assertEqual(self.drive.ensure_subfolder("คิวรอลงรับ"), first)
        self.assertEqual(self.drive.ensure_subfolder("คิวรอลงรับ"), first)

    def test_two_threads_asking_at_once_make_one_folder(self):
        both_looked = threading.Barrier(2)
        listing = self.files.list

        def slow_list(*a, **kw):
            req = listing(*a, **kw)
            run = req.fn

            def look_then_wait():
                found = run()
                try:
                    both_looked.wait(timeout=0.5)   # the old code let both look before either made it
                except threading.BrokenBarrierError:
                    pass
                return found
            req.fn = look_then_wait
            return req

        made = []
        # straight to the fake, without the one-request-at-a-time lock, so the two lookups can overlap
        with patch.object(self.files, "list", side_effect=slow_list), \
                patch.object(self.drive, "_run", side_effect=lambda req, tries=3: req.execute()):
            threads = [threading.Thread(target=lambda: made.append(self.drive.ensure_subfolder("คิวรอลงรับ")))
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(self.folders(), ["คิวรอลงรับ"])
        self.assertEqual(made[0], made[1])

    # --- the school computer letting Drive make the day's folder first ---

    def test_school_computer_has_drive_make_the_folder_then_waits_for_the_sync(self):
        sync = threading.Timer(0.3, os.makedirs, (self.local(DAY),))     # Drive for Desktop brings it down
        sync.start()
        self.addCleanup(sync.join)
        self.assertTrue(self.drive.premake(DAY, wait=5))
        self.assertEqual(self.folders(), ["๒๕๖๙", "๒๕๖๙/๑๐ ตุลาคม", "๒๕๖๙/๑๐ ตุลาคม/๒๙"])

    def test_the_server_uses_the_folders_the_computer_had_drive_make(self):
        self.drive.premake(DAY)
        before = self.folders()
        self.drive.ensure_path(DAY + "/" + core.LINE_IMG_SUBDIR)            # what saving on the server does
        self.assertEqual(self.folders(), before + [DAY + "/" + core.LINE_IMG_SUBDIR])

    def test_a_folder_the_computer_made_itself_is_not_made_again_on_drive(self):
        os.makedirs(self.local("๒๕๖๙/๑๐ ตุลาคม"))     # made before this change: Drive cannot see it
        self.assertFalse(self.drive.premake(DAY))
        self.assertEqual(self.folders(), [])            # a second "๒๕๖๙" is exactly what this prevents

    def test_folder_already_on_the_computer_needs_nothing(self):
        os.makedirs(self.local(DAY))
        self.assertTrue(self.drive.premake(DAY))
        self.assertEqual(self.files.queries, [])

    def test_a_folder_drive_does_not_sync_gets_nothing_made_on_drive(self):
        # e.g. a test's temporary folder on a computer whose config.json holds the real key
        shutil.rmtree(os.path.join(self.out, self.drive.QUEUE_FOLDER_NAME))
        self.assertFalse(self.drive.premake(DAY, wait=5))
        self.assertEqual(self.files.queries, [])

    def test_the_server_uploads_itself_so_it_makes_nothing_ahead(self):
        with patch.object(self.drive, "enabled", return_value=True):
            self.assertFalse(self.drive.premake(DAY, wait=5))
        self.assertEqual(self.files.queries, [])

    def test_saving_still_works_when_drive_cannot_be_reached(self):
        with patch.object(self.drive, "_service", side_effect=RuntimeError("offline")):
            path = core.make_output_dir(DAY, wait=5)
        self.assertEqual(path, self.local(DAY))
        self.assertTrue(os.path.isdir(path))


if __name__ == "__main__":
    unittest.main()
