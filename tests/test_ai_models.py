"""The AI keeps reading when Google retires or overloads a model.

gemini-2.5-flash was written into the code; when Google started shutting it down every
document came back with "AI ผิดพลาด: ไม่พบโมเดล", and the old check called *any* error that
mentions "model" a missing model - "The model is overloaded" (503, gone in a minute) too.
Now: SARABAN_GEMINI_MODEL, then gemini-flash-latest, then gemini-2.5-flash; a retired one
moves on to the next; all retired asks Google which flash models it serves; a busy one is
retried, then the next is tried; a wrong API key stops at once.  Offline: a fake client.
"""
from __future__ import annotations

import os
import types
import unittest
from unittest.mock import patch

import core

try:
    from google.genai import errors as genai_errors
except ImportError:          # phone-only setup
    genai_errors = None

ANSWER = "\n".join([
    "เรียน ผอ.โรงเรียนบ้านโพนทองประชาอุทิศ",
    "สพป. เชิญประชุมกลุ่มสมาชิกสวนพฤกษศาสตร์โรงเรียน วันที่ 30 ตุลาคม",
    "เพื่อโปรดทราบ",
    "2",
    "ศธ 04142/ว4028",
    "ประชาสัมพันธ์การประชุมกลุ่มสมาชิกสวนพฤกษศาสตร์โรงเรียน",
    "21 ตุลาคม 2569",
    "สพป.สกลนคร เขต 1",
    "ผู้อำนวยการโรงเรียนในสังกัด",
])


def gone(name="gemini-2.5-flash"):
    return genai_errors.ClientError(404, {"error": {
        "code": 404, "status": "NOT_FOUND",
        "message": f"models/{name} is not found for API version v1beta, "
                   "or is not supported for generateContent."}})


def busy():
    return genai_errors.ServerError(503, {"error": {
        "code": 503, "status": "UNAVAILABLE",
        "message": "The model is overloaded. Please try again later."}})


def quota():
    return genai_errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota."}})


def bad_key():
    return genai_errors.ClientError(400, {"error": {
        "code": 400, "status": "INVALID_ARGUMENT",
        "message": "API key not valid. Please pass a valid API key."}})


class FakeClient:
    """models: name -> list of things to answer in turn (an exception, or the text)"""

    def __init__(self, models, listed=()):
        self.script = {k: list(v) for k, v in models.items()}
        self.calls = []
        self.listed = list(listed)
        self.files = types.SimpleNamespace(upload=lambda file: "uploaded:" + os.path.basename(file))
        self.models = types.SimpleNamespace(generate_content=self.generate, list=self.list)

    def generate(self, model, contents):
        self.calls.append(model)
        todo = self.script.get(model) or [gone(model)]
        step = todo.pop(0) if len(todo) > 1 else todo[0]
        if isinstance(step, Exception):
            raise step
        return types.SimpleNamespace(text=step)

    def list(self):
        return [types.SimpleNamespace(name="models/" + n, supported_actions=a) for n, a in self.listed]


@unittest.skipIf(genai_errors is None, "ต้องลง google-genai ก่อน (pip install -r requirements.txt)")
class ModelChoiceTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(core, "_ai_model", None),
                  patch.object(core, "AI_RETRY_WAIT", (0, 0)),
                  patch.dict(os.environ, {"SARABAN_GEMINI_MODEL": ""}),
                  patch.object(core, "_w", lambda name: os.devnull)):
            p.start()
            self.addCleanup(p.stop)

    def read(self, client):
        with patch.object(core, "get_ai_client", return_value=client):
            return core.generate_kasien_text("doc.pdf")

    def test_reads_with_the_flash_alias_first(self):
        ai = FakeClient({"gemini-flash-latest": [ANSWER]})
        text, sig, no, title, date, sender, recipient = self.read(ai)
        self.assertEqual(ai.calls, ["gemini-flash-latest"])
        self.assertIn("เพื่อโปรดทราบ", text)
        self.assertEqual((sig, no, sender), (2, "ศธ 04142/ว4028", "สพป.สกลนคร เขต 1"))

    def test_a_retired_model_moves_on_to_the_next(self):
        ai = FakeClient({"gemini-flash-latest": [gone("gemini-flash-latest")],
                         "gemini-2.5-flash": [ANSWER]})
        self.assertNotIn(core.AI_FAILED, self.read(ai)[0])
        self.assertEqual(ai.calls, ["gemini-flash-latest", "gemini-2.5-flash"])

    def test_all_retired_asks_google_which_flash_models_are_left(self):
        listed = [("gemini-2.5-flash", ["generateContent"]),
                  ("gemini-3.8-flash-lite", ["generateContent"]),
                  ("gemini-3.8-flash-image", ["generateContent"]),
                  ("gemini-3.9-flash-preview-10-2026", ["generateContent"]),
                  ("gemini-3.8-flash", ["generateContent", "countTokens"]),
                  ("gemini-3.1-pro", ["generateContent"]),
                  ("text-embedding-004", ["embedContent"])]
        ai = FakeClient({"gemini-3.8-flash": [busy()],
                         "gemini-3.9-flash-preview-10-2026": [ANSWER]}, listed)
        self.assertNotIn(core.AI_FAILED, self.read(ai)[0])
        # released models newest first, then previews (pulled sooner); never lite/image/pro
        self.assertEqual(core._discover_models(ai),
                         ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-3.9-flash-preview-10-2026"])
        self.assertEqual(ai.calls[:2], ["gemini-flash-latest", "gemini-2.5-flash"])
        self.assertEqual(ai.calls[2:5], ["gemini-3.8-flash"] * 3)        # busy: tried 3 times
        self.assertEqual(ai.calls[5:], ["gemini-3.9-flash-preview-10-2026"])
        # the next document goes straight to the model that worked
        ai.calls.clear()
        self.read(ai)
        self.assertEqual(ai.calls, ["gemini-3.9-flash-preview-10-2026"])

    def test_busy_is_tried_again_not_called_a_missing_model(self):
        ai = FakeClient({"gemini-flash-latest": [busy(), ANSWER]})
        self.assertNotIn(core.AI_FAILED, self.read(ai)[0])
        self.assertEqual(ai.calls, ["gemini-flash-latest", "gemini-flash-latest"])
        self.assertEqual(core.ai_error_kind(busy()), "busy")          # the old check said "model"

    def test_still_busy_after_retries_says_so(self):
        ai = FakeClient({"gemini-flash-latest": [busy()], "gemini-2.5-flash": [busy()]})
        text = self.read(ai)[0]
        self.assertIn("ไม่ว่าง", text)
        self.assertNotIn("ไม่พบ", text)

    def test_a_used_up_quota_tries_another_model(self):
        ai = FakeClient({"gemini-flash-latest": [quota()], "gemini-2.5-flash": [ANSWER]})
        self.assertNotIn(core.AI_FAILED, self.read(ai)[0])

    def test_a_wrong_api_key_stops_at_once(self):
        ai = FakeClient({"gemini-flash-latest": [bad_key()], "gemini-2.5-flash": [ANSWER]})
        text = self.read(ai)[0]
        self.assertEqual(ai.calls, ["gemini-flash-latest"])
        self.assertIn("API Key", text)

    def test_nothing_left_anywhere_says_how_to_fix_it(self):
        ai = FakeClient({}, listed=[("gemini-2.5-flash", ["generateContent"])])
        text, sig, *_ = self.read(ai)
        self.assertIn(core.AI_FAILED, text)
        self.assertIn("SARABAN_GEMINI_MODEL", text)
        self.assertEqual(sig, 1)

    def test_the_setting_is_tried_first(self):
        with patch.dict(os.environ, {"SARABAN_GEMINI_MODEL": "gemini-3.8-flash, gemini-3.6-flash"}):
            ai = FakeClient({"gemini-3.8-flash": [gone("gemini-3.8-flash")], "gemini-3.6-flash": [ANSWER]})
            self.read(ai)
        self.assertEqual(ai.calls, ["gemini-3.8-flash", "gemini-3.6-flash"])

    def test_no_api_key_set(self):
        text = self.read(None)[0]
        self.assertIn("API Key", text)

    def test_error_kinds(self):
        self.assertEqual(core.ai_error_kind(gone()), "gone")
        self.assertEqual(core.ai_error_kind(quota()), "quota")
        self.assertEqual(core.ai_error_kind(bad_key()), "auth")
        self.assertEqual(core.ai_error_kind(RuntimeError(
            "This model is no longer available to new users.")), "gone")
        self.assertEqual(core.ai_error_kind(RuntimeError("Read timed out")), "busy")
        self.assertEqual(core.ai_error_kind(ValueError("bad pdf")), "other")


if __name__ == "__main__":
    unittest.main()
