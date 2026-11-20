import unittest

import display_text


class PrintableTests(unittest.TestCase):
    def test_escapes_output_unsafe_characters(self):
        cases = {
            "\x1b[2K": "\\x1b[2K",
            "\x85": "\\x85",
            chr(0x2028): "\\u2028",
            chr(0x2029): "\\u2029",
            chr(0x202E): "\\u202e",
            chr(0x2066): "\\u2066",
            chr(0x200E): "\\u200e",
            chr(0x200B): "\\u200b",
            chr(0xFEFF): "\\ufeff",
            "a\nb": "a\\nb",
            "a\tb": "a\\tb",
        }
        for raw, shown in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(display_text.printable(raw), shown)

    def test_backslash_is_escaped_so_an_escape_cannot_be_faked(self):
        self.assertEqual(display_text.printable("\\u202e"), "\\\\u202e")
        self.assertNotEqual(display_text.printable("\\x1b"), display_text.printable("\x1b"))

    def test_plain_text_and_emoji_stay(self):
        self.assertEqual(display_text.printable("Ångström 😀 ok"), "Ångström 😀 ok")

    def test_a_zero_width_joiner_is_escaped_in_output_though_accepted_as_input(self):
        self.assertEqual(display_text.printable("a\u200db"), "a\\u200db")

    def test_output_unsafe_predicate(self):
        for code in (0, 0x1B, 0x7F, 0x85, 0x2028, 0x202E, 0x200B, 0xFEFF, 0x200E):
            self.assertTrue(display_text.is_output_unsafe(chr(code)), hex(code))
        for char in ("a", " ", "é", "\U0001f600"):
            self.assertFalse(display_text.is_output_unsafe(char), repr(char))

    def test_input_rejection_set(self):
        for code in (0, 0x7F, 0x85, 0x2028, 0x2029, *range(0x202A, 0x202F),
                     *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C,
                     0x200B, 0x2060, 0xFEFF, 0xE0041, 0xE0001, 0x180E, 0x2061, 0x110BD):
            self.assertTrue(display_text.is_input_forbidden(chr(code)), hex(code))
        for code in (0x200D, 0x200C, 0x00AD, 0x61, 0x1F600):
            self.assertFalse(display_text.is_input_forbidden(chr(code)), hex(code))

    def test_a_zwj_emoji_sequence_and_a_soft_hyphen_are_accepted(self):
        sequence = "\U0001f468" + chr(0x200D) + "\U0001f469" + chr(0x200D) + "\U0001f467"
        for text in (sequence, "co" + chr(0x00AD) + "operate", "a" + chr(0x200C) + "b"):
            self.assertFalse(any(map(display_text.is_input_forbidden, text)), repr(text))

    def test_printable_lines_keeps_the_line_breaks_only(self):
        self.assertEqual(display_text.printable_lines("a" + chr(0x202E) + "\nb\\\r\nc"),
                         "a\\u202e\nb\\\\\\r\nc")

    def test_a_lone_surrogate_is_escaped_so_it_can_be_printed(self):
        self.assertEqual(display_text.printable("a" + chr(0xDC80)), "a\\udc80")

    def test_error_text_escapes_the_message_line_by_line(self):
        raw = f"x{chr(0x202E)}\ny"
        self.assertEqual(display_text.error_text(ValueError(raw)), "x\\u202e\ny")
        self.assertEqual(display_text.error_text(ValueError(raw), single_line=True),
                         "x\\u202e\\ny")

    def test_quoted_keeps_the_raw_value_and_cuts_it_to_80_characters(self):
        self.assertEqual(display_text.quoted("a\\b"), "'a\\b'")
        self.assertEqual(display_text.quoted(3), "3")
        long = display_text.quoted("x" * 200)
        self.assertEqual(len(long), 80)
        self.assertTrue(long.endswith("..."))

    def test_inline_turns_crlf_and_lf_into_one_marker_each_and_leaves_a_lone_cr(self):
        mark = chr(0x23CE)
        self.assertEqual(display_text.inline("a\r\nb"), f"a{mark}b")
        self.assertEqual(display_text.inline("a\nb\n"), f"a{mark}b{mark}")
        self.assertEqual(display_text.inline("a\rb"), "a\rb")
        self.assertEqual(display_text.printable(display_text.inline("a\rb")), "a\\rb")
        self.assertEqual(display_text.inline(42), "42")
        self.assertEqual(display_text.inline(ValueError("x\ny")), f"x{mark}y")

    def test_quoted_line_quotes_the_inlined_value(self):
        self.assertEqual(display_text.quoted_line("a\nb"), f"'a{chr(0x23CE)}b'")
        self.assertEqual(display_text.quoted_line(7), "'7'")

    def test_int_argument_converts_or_raises_with_the_raw_value_quoted(self):
        import argparse

        self.assertEqual(display_text.int_argument("12"), 12)
        with self.assertRaises(argparse.ArgumentTypeError) as raised:
            display_text.int_argument("a\\" + chr(0x202E))
        self.assertEqual(str(raised.exception), "invalid int value: 'a\\" + chr(0x202E) + "'")

    def test_contains_unsafe(self):
        self.assertTrue(display_text.has_unsafe("a" + chr(0x202E)))
        self.assertFalse(display_text.has_unsafe("a\\b"))


if __name__ == "__main__":
    unittest.main()
