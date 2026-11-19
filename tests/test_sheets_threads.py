"""Every thread talks to the Google Sheet over its own connection.

All threads used to share one httplib2.Http, which is not thread-safe.  The web reads the Sheet
from many threads at once (next receipt number, users, the news list) while a save writes to it;
on Render the server died with Segmentation fault (exit 139) / abort (exit 134) seconds after
saving one document and opening the next.
"""
from __future__ import annotations

import threading
import unittest

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    import store
except ImportError:     # phone-only setup
    store = None


@unittest.skipIf(store is None, "google client not installed")
class ThreadConnectionTests(unittest.TestCase):
    def setUp(self):
        creds = Credentials(token="t")
        self.api = build("sheets", "v4", credentials=creds, cache_discovery=False,
                         static_discovery=True,
                         requestBuilder=store.thread_request_builder(creds)).spreadsheets()

    def http_of(self):
        return self.api.values().get(spreadsheetId="s", range="A:A").http

    def test_each_thread_has_its_own_connection_and_keeps_it(self):
        mine = self.http_of()
        self.assertIs(self.http_of(), mine)                 # reused, not a new TLS handshake every call
        others = []
        threads = [threading.Thread(target=lambda: others.append(self.http_of())) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = {id(h) for h in others} | {id(mine)}
        self.assertEqual(len(ids), 5)


if __name__ == "__main__":
    unittest.main()
