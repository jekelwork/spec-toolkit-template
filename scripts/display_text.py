"""One definition of the characters that are unsafe in single-line output.

Standard library only, and no import from another repository script, so every script can use it.
Display deception (a bidi override that makes a preview read differently from what runs, a
terminal escape that rewrites the screen) is the risk; the bootstrap prints text it does not
fully control (GitHub, git and the project's own settings), so it escapes that text with
`printable` and refuses the same characters in values it writes with `is_input_forbidden`.
"""

import argparse
import unicodedata

# Everything that cannot be shown as itself on one line: control characters (C0, DEL, C1), format
# characters (bidi controls, zero-width characters, byte order mark), line/paragraph separators
# and lone surrogates (which cannot even be encoded for output).
OUTPUT_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
# Refused in values the bootstrap writes (project.name, project.description): control characters,
# line and paragraph separators and every format character (bidi controls, zero-width characters,
# byte order mark, tag characters), except ZWJ (U+200D, emoji sequences), ZWNJ (U+200C) and soft
# hyphen (U+00AD), which stay valid.
INPUT_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
INPUT_ALLOWED_FORMAT_CHARACTERS = frozenset({chr(0x200D), chr(0x200C), chr(0x00AD)})


def is_output_unsafe(char):
    """True when `char` must be escaped before it is shown."""
    return unicodedata.category(char) in OUTPUT_UNSAFE_CATEGORIES


def is_input_forbidden(char):
    """True when `char` may not appear in a value the bootstrap writes."""
    return (unicodedata.category(char) in INPUT_FORBIDDEN_CATEGORIES
            and char not in INPUT_ALLOWED_FORMAT_CHARACTERS)


def has_unsafe(text):
    """True when `text` holds a character `printable` would escape (a backslash does not count)."""
    return any(is_output_unsafe(char) for char in text)


def printable(text):
    """`text` with every output-unsafe character shown as a Python escape (`\\x1b`, `\\u202e`)
    and every backslash doubled, so an escape in the output is always one this function made."""
    return "".join(
        "\\\\" if char == "\\"
        else char.encode("unicode_escape").decode() if is_output_unsafe(char)
        else char
        for char in text
    )


def printable_lines(text):
    """`printable` for a message of several lines: the line feeds stay, every other unsafe
    character (a carriage return included) is escaped."""
    return "\n".join(printable(line) for line in text.split("\n"))


def quoted(value, limit=80):
    """`value` as it is in a message, in single quotes when it is a string, cut to `limit`
    characters. Not escaped: the message is escaped where it is printed."""
    text = f"'{value}'" if isinstance(value, str) else str(value)
    return text[:limit - 3] + "..." if len(text) > limit else text


# Stands for a line feed inside a value quoted in a message (`inline`): the value must not start
# a new output line, and an escape such as `\n` would be doubled by the escaping at the print.
LINE_BREAK_MARK = chr(0x23CE)


def inline(value):
    """`value` as text with every line feed replaced by `LINE_BREAK_MARK`, for a GitHub-sourced
    value inside a message that keeps its own line breaks. The mark is an ordinary symbol, so a
    value may contain it literally. Not escaped: that happens at the print."""
    return str(value).replace("\r\n", "\n").replace("\n", LINE_BREAK_MARK)


def quoted_line(value):
    """`value` in single quotes, on one line (`inline`)."""
    return f"'{inline(value)}'"


def int_argument(value):
    """An argparse `type=int` whose error quotes the raw value, so the one escaping happens where
    the message is printed (argparse's own message quotes it with `repr`, an escape already)."""
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: '{value}'") from None


def error_text(error, single_line=False):
    """The message of `error`, safe to print: escaped line by line (all on one line with
    `single_line`, for a message that quotes a value and must not break the output). Messages are
    built from raw values, so this is the one escaping, applied where the message is printed."""
    return printable(str(error)) if single_line else printable_lines(str(error))
