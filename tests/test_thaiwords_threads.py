"""The word list loads once, safely, even when two documents open at the same moment.

Right after a restart two workers laid out kasien text at once; the second one saw the word set
already filled and used the per-letter index while the first was still building it, and the whole
document failed ("'list' object has no attribute 'add'").
"""
import threading
import unittest
from unittest.mock import patch

import thaiwords


class LoadTests(unittest.TestCase):
    def test_many_threads_tokenize_on_a_cold_start(self):
        with patch.object(thaiwords, "_words", None), patch.object(thaiwords, "_by_first", None):
            errors, out = [], []
            go = threading.Event()

            def work():
                go.wait()
                try:
                    out.append(thaiwords.tokenize("ทราบ แจ้งครูทุกคนเพื่อโปรดทราบและดำเนินการ"))
                except Exception as e:          # the old loader failed here
                    errors.append(e)
            threads = [threading.Thread(target=work) for _ in range(8)]
            for t in threads:
                t.start()
            go.set()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len({tuple(x) for x in out}), 1)


if __name__ == "__main__":
    unittest.main()
