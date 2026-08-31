import os
import queue
import tempfile
import threading
import unittest
from unittest import mock

import store
from web import docmode


class FakeStore:
    def __init__(self, records=None, history=None):
        self.records = dict(records or {})
        self.history = set(history or ())
        self.marked = []
        self.updated = True

    def doc_records(self):
        return dict(self.records)

    def history_ids(self):
        return set(self.history)

    def mark_registered(self, book_id, receipt_no):
        self.marked.append((str(book_id), str(receipt_no)))
        self.records[str(book_id)] = {
            "status": "registered", "receipt_no": str(receipt_no)}
        self.history.add(str(book_id))

    def mark_skipped(self, book_id):
        self.records[str(book_id)] = {"status": "skipped", "receipt_no": ""}
        self.history.add(str(book_id))

    def update_registry_row(self, *args, **kwargs):
        return self.updated


class WebHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work_patch = mock.patch.object(
            docmode.core, "_w", side_effect=lambda name: os.path.join(self.tmp.name, name))
        self.work_patch.start()
        self.addCleanup(self.work_patch.stop)
        with docmode._lock:
            docmode._jobs.clear()

    def _meta(self, **updates):
        meta = {"book_id": "123", "doc_no": " ศธ/1\n",
                "doc_title": "เรื่องทดสอบ", "doc_date": "1 ม.ค. 2569",
                "sender": "สพป.", "emoji": "🔵", "attach": "ไฟล์แนบ"}
        meta.update(updates)
        return meta

    def test_duplicate_phone_submit_returns_same_active_job(self):
        fake = FakeStore()
        with mock.patch.object(store, "get_store", return_value=fake):
            first, created_first = docmode.claim_phone_job("phone", self._meta())
            second, created_second = docmode.claim_phone_job("phone", self._meta())
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertIs(first, second)
        self.assertEqual("ศธ/1", first["doc_no"])
        self.assertEqual("123", first["book_id"])

    def test_failed_phone_job_requires_explicit_retry_and_reuses_job(self):
        fake = FakeStore()
        with mock.patch.object(store, "get_store", return_value=fake):
            job, _ = docmode.claim_phone_job("phone", self._meta())
            job["status"] = "error"
            same, created = docmode.claim_phone_job("phone", self._meta(), retry_failed=False)
            retried, retry_started = docmode.claim_phone_job(
                "phone", self._meta(doc_title="แก้แล้ว"), retry_failed=True)
        self.assertIs(job, same)
        self.assertFalse(created)
        self.assertIs(job, retried)
        self.assertTrue(retry_started)
        self.assertEqual("uploading", job["status"])
        self.assertEqual("แก้แล้ว", job["doc_title"])
        self.assertEqual(1, len(docmode._jobs))

    def test_durable_done_or_skipped_is_rejected(self):
        for status in ("registered", "skipped"):
            with self.subTest(status=status), docmode._lock:
                docmode._jobs.clear()
            fake = FakeStore(records={"123": {"status": status, "receipt_no": "๙"}})
            with mock.patch.object(store, "get_store", return_value=fake):
                with self.assertRaises(docmode.AlreadyHandledError) as caught:
                    docmode.claim_phone_job("phone", self._meta())
            self.assertEqual(status, caught.exception.status)

    def test_metadata_survives_worker_error(self):
        fake = FakeStore()
        with mock.patch.object(store, "get_store", return_value=fake):
            job, _ = docmode.claim_phone_job("phone", self._meta())
        try:
            raise ValueError("AI failed")
        except ValueError as error:
            docmode._fail(job, error)
        self.assertEqual("error", job["status"])
        self.assertEqual("เรื่องทดสอบ", job["doc_title"])
        self.assertEqual("123", job["book_id"])
        self.assertIn("AI failed", job["error"])

    def test_only_one_concurrent_save_claim_wins(self):
        job = {"status": "ready"}
        gate = threading.Barrier(3)
        results = []

        def claim():
            gate.wait()
            try:
                docmode._claim_save(job)
                results.append("won")
            except docmode.JobStateError:
                results.append("conflict")

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join()
        self.assertCountEqual(["won", "conflict"], results)
        self.assertEqual("saving", job["status"])

    def test_receipt_is_reused_after_durable_mark_failure(self):
        class FlakyStore(FakeStore):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def mark_registered(self, book_id, receipt_no):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("store temporarily down")
                super().mark_registered(book_id, receipt_no)

        fake = FlakyStore()
        reserve_calls = []
        job = {"book_id": "123", "doc_no": "1", "doc_date": "-",
               "sender": "-", "doc_title": "-"}

        def reserve(**fields):
            reserve_calls.append(fields)
            return "๑๐"

        with self.assertRaises(RuntimeError):
            docmode._reserve_for_job(job, reserve, fake)
        receipt = docmode._reserve_for_job(job, reserve, fake)
        self.assertEqual("๑๐", receipt)
        self.assertEqual(1, len(reserve_calls))
        self.assertEqual("๑๐", job["reserved_receipt"])
        self.assertTrue(job["durable_claimed"])

    def test_redo_fails_when_registry_row_is_missing(self):
        fake = FakeStore()
        fake.updated = False
        job = {"redo_no": "๗", "doc_no": "1", "doc_date": "-",
               "sender": "-", "doc_title": "-"}
        with self.assertRaisesRegex(RuntimeError, "ไม่พบเลขรับ"):
            docmode._reserve_for_job(job, lambda **kw: self.fail("must not reserve"), fake)
        self.assertNotIn("reserved_receipt", job)

    def test_skip_conflicts_with_save(self):
        with self.assertRaises(docmode.JobStateError):
            docmode.skip({"status": "saving", "book_id": "123"})

    def test_done_save_returns_existing_result(self):
        result = {"ok": True, "receipt_no": "๓"}
        self.assertIs(result, docmode._claim_save({"status": "done", "result": result}))

    def test_safe_filename_contains_receipt_and_no_path_characters(self):
        name = docmode.safe_output_filename(' ศธ/1:*?"<>|\\ ', "๑๒")
        self.assertTrue(name.startswith("เลขรับ_๑๒_"))
        self.assertTrue(name.endswith(".pdf"))
        self.assertFalse(any(ch in name for ch in '<>:"/\\|?*'))

    def test_containment_rejects_parent_and_prefix_sibling(self):
        root = os.path.join(self.tmp.name, "out")
        inside = docmode.contained_path(root, "2026-01-01", "doc.pdf")
        self.assertEqual(os.path.abspath(os.path.join(root, "2026-01-01", "doc.pdf")), inside)
        self.assertIsNone(docmode.contained_path(root, "..", "outside.pdf"))
        sibling = root + "-other"
        self.assertIsNone(docmode.contained_path(root, sibling, "doc.pdf"))

    def test_queue_full_marks_job_error_and_raises(self):
        full = mock.Mock()
        full.put_nowait.side_effect = queue.Full
        job = {"status": "analyzing"}
        with mock.patch.object(docmode, "_work_queue", full):
            with self.assertRaises(docmode.QueueFullError):
                docmode._enqueue(job, lambda: None)
        self.assertEqual("error", job["status"])
        self.assertIn("คิว", job["error"])


if __name__ == "__main__":
    unittest.main()
