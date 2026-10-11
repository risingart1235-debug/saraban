"""Endorsement (คำเกษียณ) line indentation: ย่อหน้า vs ชิดขอบ, and mixing both in one box.

ย่อหน้า (indent_pct > 0): every paragraph is indented except the "เรียน ผอ." line.
ชิดขอบ (indent_pct = 0): every line is flush, except lines the user typed with a
leading space — that is how one box mixes indented and flush lines.
"""
from __future__ import annotations

import unittest

import core

FLUSH, INDENT = 0, 100


def render(text, indent_pct):
    return core.render_transparent_kasien(text, 600, 100, indent_pct)


def left_edges(img, n_lines):
    """x of the leftmost ink on each drawn line (middle of the line, clear of tone marks)"""
    font_size = 46                       # size 100%
    step = int(font_size * 1.2)
    top = int(15 / 2)
    alpha = img.getchannel("A")
    edges = []
    for i in range(n_lines):
        y0 = top + i * step + int(step * 0.3)
        y1 = top + i * step + int(step * 0.8)
        box = alpha.crop((0, y0, img.width, y1)).getbbox()
        edges.append(box[0] if box else None)
    return edges


class KasienIndentTests(unittest.TestCase):
    def test_flush_box_indents_only_lines_typed_with_a_leading_space(self):
        first_indented, second_flush = left_edges(
            render(" เพื่อโปรดทราบ\nเห็นควรมอบงานวิชาการ", FLUSH), 2)
        self.assertGreater(first_indented, second_flush + 50)

        first_flush, second_indented = left_edges(
            render("เพื่อโปรดทราบ\n เห็นควรมอบงานวิชาการ", FLUSH), 2)
        self.assertGreater(second_indented, first_flush + 50)

    def test_flush_box_without_typed_spaces_is_all_flush(self):
        self.assertLess(max(left_edges(render("ทราบ\nมอบงานวิชาการ", FLUSH), 2)), 20)

    def test_typed_indent_is_the_same_as_the_normal_paragraph_indent(self):
        typed = render("เรียน ผอ.\n เพื่อโปรดทราบ", FLUSH)
        auto = render("เรียน ผอ.\nเพื่อโปรดทราบ", INDENT)
        self.assertEqual(typed.size, auto.size)
        self.assertEqual(typed.tobytes(), auto.tobytes())

    def test_indent_box_looks_the_same_with_or_without_typed_spaces(self):
        typed = render("เรียน ผอ.\n   สรุปเรื่อง\nเพื่อโปรดทราบ", INDENT)
        plain = render("เรียน ผอ.\nสรุปเรื่อง\nเพื่อโปรดทราบ", INDENT)
        self.assertEqual(typed.tobytes(), plain.tobytes())


if __name__ == "__main__":
    unittest.main()
