"""Which page images go to the LINE group, and how they are packed into one push.

If the endorsement sits on page 2, the group gets pages 1-2 (3 -> 1-3, 4 -> 1-4), so
people see the endorsement and not just the stamped first page.  LINE takes at most
5 bubbles per push: the text plus 4 pictures.  Offline: requests.post is faked.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import core


class LinePagesTests(unittest.TestCase):
    """one rule for the web app and the desktop program (both use core.line_pages)"""

    def test_send_every_page_up_to_the_endorsement(self):
        self.assertEqual(core.line_pages(1), [1])
        self.assertEqual(core.line_pages(2), [1, 2])
        self.assertEqual(core.line_pages(3), [1, 2, 3])
        self.assertEqual(core.line_pages(4), [1, 2, 3, 4])

    def test_further_than_four_pages_keeps_the_stamp_page_and_the_endorsement_page(self):
        self.assertEqual(core.line_pages(5), [1, 3, 4, 5])
        self.assertEqual(core.line_pages(7), [1, 5, 6, 7])


class DesktopPagesWithItemsTests(unittest.TestCase):
    """the desktop program stacks page 1, the sender's signature page and a blank page
    on one canvas; where the stamp/endorsement boxes sit says how far to send"""

    # page 1 at y=70 (h 2339), gap 70, signature page 3 at y=2479, blank page 6 at y=4888
    PAGES = [(70, 2339, 1), (2479, 2339, 3), (4888, 3307, 6)]

    def test_endorsement_on_the_signature_page(self):
        items = [(120, 300), (2900, 250)]                     # stamp on 1, kasien on page 3
        self.assertEqual(core.pages_with_items(items, self.PAGES), {1, 3})
        self.assertEqual(core.line_pages(max(core.pages_with_items(items, self.PAGES))), [1, 2, 3])

    def test_box_straddling_two_pages_counts_both(self):
        self.assertEqual(core.pages_with_items([(2300, 300)], self.PAGES), {1, 3})

    def test_box_left_in_the_gap_between_pages_counts_nowhere(self):
        self.assertEqual(core.pages_with_items([(2415, 50)], self.PAGES), set())

    def test_blank_page_only_counts_when_it_is_part_of_the_layout(self):
        self.assertEqual(core.pages_with_items([(5000, 200)], self.PAGES), {6})
        self.assertEqual(core.pages_with_items([(5000, 200)], self.PAGES[:2]), set())


class ChosenPagesTests(unittest.TestCase):
    """pages ticked on screen: only real pages, once each, in page order, at most 4"""

    def test_keeps_real_pages_in_order_once(self):
        from web import docmode
        self.assertEqual(docmode.chosen_line_pages([3, 1, 1, "2", "x", -1, 9], 5, False), [1, 2, 3])

    def test_blank_page_only_when_something_was_placed_on_it(self):
        from web import docmode
        self.assertEqual(docmode.chosen_line_pages([6], 5, True), [6])
        self.assertEqual(docmode.chosen_line_pages([6], 5, False), [])

    def test_nothing_ticked_means_text_only_and_never_more_than_four(self):
        from web import docmode
        self.assertEqual(docmode.chosen_line_pages([], 5, False), [])
        self.assertEqual(docmode.chosen_line_pages([5, 4, 3, 2, 1], 5, False), [1, 2, 3, 4])


class SendLineTests(unittest.TestCase):
    def sent(self, image_url):
        with patch.object(core.requests, "post") as post:
            post.return_value.status_code = 200
            self.assertTrue(core.send_line_with_image("ข้อความ", image_url))
        return post.call_args.kwargs["json"]["messages"]

    def test_text_then_one_picture_per_page_in_one_push(self):
        msgs = self.sent(["https://x/1.jpg", "https://x/2.jpg"])
        self.assertEqual([m["type"] for m in msgs], ["text", "image", "image"])
        self.assertEqual(msgs[2]["originalContentUrl"], "https://x/2.jpg")

    def test_never_more_than_lines_five_bubbles(self):
        msgs = self.sent([f"https://x/{i}.jpg" for i in range(6)])
        self.assertEqual(len(msgs), 5)

    def test_single_url_and_missing_pictures_still_work_like_before(self):
        self.assertEqual(len(self.sent("https://x/1.jpg")), 2)   # the desktop app passes one URL
        self.assertEqual(len(self.sent(None)), 1)
        self.assertEqual(len(self.sent(["", None, "https://x/1.jpg"])), 2)


if __name__ == "__main__":
    unittest.main()
