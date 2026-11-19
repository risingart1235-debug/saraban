"""Mode 3 - registering a teacher's leave form (ลงรับใบลา).

Nothing is printed: the page gives the receipt number, date and time to write by hand into the
box on the leave form.  The number comes from the same register as incoming letters, the row
has ที่ "-", the chosen teacher as sender, the chosen ลงวันที่ and "ขออนุญาตลา<type>".
Offline: the store is a fake.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import core

try:
    from fastapi.testclient import TestClient
    from web import main as webmain
except (ImportError, RuntimeError):     # server packages not installed (phone-only setup)
    webmain = None


class FakeStore:
    def __init__(self):
        self.rows = []

    def peek_receipt_no(self):
        return core.to_thai_digits(str(120 + len(self.rows)))

    def register(self, doc_no="", doc_date="", sender="", doc_title="", receive_date=""):
        no = self.peek_receipt_no()
        self.rows.append((no, doc_no, doc_date, sender, doc_title, receive_date))
        return no


@unittest.skipIf(webmain is None, "server packages not installed")
class LeaveTests(unittest.TestCase):
    USERS = {"ครูเอ": {"status": "approved", "salt": "a"}}

    def setUp(self):
        self.store = FakeStore()
        for p in (patch.object(webmain, "load_users", return_value=self.USERS),
                  patch("store.get_store", return_value=self.store),
                  patch.object(core, "_store", return_value=self.store)):
            p.start()
            self.addCleanup(p.stop)
        self.web = TestClient(webmain.app, cookies={
            "session": webmain.make_session("ครูเอ", self.USERS["ครูเอ"])})

    def test_lists_the_ten_teachers_and_three_types(self):
        r = self.web.get("/api/leave/new")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(len(d["teachers"]), 10)
        self.assertIn("นายอิทธิพล ยาทองไชย", d["teachers"])
        self.assertEqual(d["types"], ["ป่วย", "กิจส่วนตัว", "คลอดบุตร"])
        self.assertEqual(d["receipt_no"], "๑๒๐")
        self.assertEqual(self.store.rows, [])            # only a peek, nothing reserved

    def test_save_registers_one_row_in_the_shared_register(self):
        r = self.web.post("/api/leave/save", json={
            "teacher": "นางนิตยา ยางธิสาร", "leave_type": "กิจส่วนตัว", "doc_date": "2026-11-18"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["receipt_no"], "๑๒๐")
        self.assertTrue(d["date"] and d["time"])
        self.assertEqual(self.store.rows, [(
            "๑๒๐", "-", "๑๘ พ.ย. ๒๕๖๙", "นางนิตยา ยางธิสาร", "ขออนุญาตลากิจส่วนตัว", d["date"])])
        # the next one gets the next number
        r = self.web.post("/api/leave/save", json={
            "teacher": "นายอนุชา มีภูคำ", "leave_type": "ป่วย", "doc_date": "2026-11-19"})
        self.assertEqual(r.json()["receipt_no"], "๑๒๑")

    def test_unknown_teacher_or_type_uses_no_number(self):
        for body in ({"teacher": "ใครก็ไม่รู้", "leave_type": "ป่วย"},
                     {"teacher": "นายอนุชา มีภูคำ", "leave_type": "พักร้อน"}):
            r = self.web.post("/api/leave/save", json=body)
            self.assertEqual(r.status_code, 400)
        self.assertEqual(self.store.rows, [])

    def test_page_needs_login(self):
        r = TestClient(webmain.app).get("/leave", follow_redirects=False)
        self.assertEqual(r.status_code, 302)


if __name__ == "__main__":
    unittest.main()
