"""A helper for the tests that check output for unescaped characters (not a test module)."""

import display_text


def raw_unsafe(text):
    """True when `text` still holds a character `display_text.printable` would escape. A line feed
    is allowed: output has several lines."""
    return display_text.has_unsafe(text.replace("\n", ""))
