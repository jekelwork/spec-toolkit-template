"""Reference test for the "Where to find what" index in AGENTS.md.

The index is prose that agents follow, so its references must not rot. It is also published to
adopter projects, so it must never point at a file the export manifest excludes.

How cells are read:
- Markdown links, `[text](target)` with an optional title, must be relative; a bare `#anchor`
  points into AGENTS.md itself. Any `](`, `][` or `href=` left after the links are consumed
  (reference-style links, HTML) fails, so no link form is skipped silently.
- Inline code spans are checked for path-like tokens: starting with `scripts/`, `docs/`,
  `.specify/`, `.agents/` or `agent-definitions/`, or ending in `.toml` or `.md`. For a command
  such as `python3 scripts/x.py sub` the script path is the token. Each must exist and not be
  export-excluded.
- Governance files created at bootstrap (`project-context.toml`,
  `.specify/memory/constitution.md`) are excluded from the export but exist in every
  bootstrapped project. AGENTS.md names them as code, and they sit in an explicit allowlist
  (CODE_ALLOWLIST) that skips the existence and exclusion checks. Do not link them.

The export manifest (scripts/toolkit-export.toml) exists only in the source repository. In an
exported copy it is absent and the exclusion check is skipped, which is safe there: an excluded
file does not exist in the copy, so the existence check already fails for a reference to one.
"""

import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
AGENTS = ROOT / "AGENTS.md"
MANIFEST = ROOT / "scripts" / "toolkit-export.toml"
SECTION = "Where to find what"
LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
CODE_SPAN = re.compile(r"`([^`]+)`")
PATH_PREFIXES = ("scripts/", "docs/", ".specify/", ".agents/", "agent-definitions/")
CODE_ALLOWLIST = {"project-context.toml", ".specify/memory/constitution.md"}
FENCE = re.compile(r"^ {0,3}(```|~~~)")


def section_rows():
    text = AGENTS.read_text(encoding="utf-8")
    match = re.search(rf"^## {SECTION}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise AssertionError(f'AGENTS.md has no "## {SECTION}" section')
    rows = [line for line in match.group(1).splitlines() if line.startswith("|")]
    header, _separator, *body = rows
    cells = [c.strip() for c in header.strip("|").split("|")]
    if cells != ["Question", "Where to look"]:
        raise AssertionError(
            f"Unexpected table header {cells}; the index table header must be exactly "
            "'| Question | Where to look |' (see docs/bootstrap.md#troubleshooting)"
        )
    return body


def slug(heading):
    """GitHub's anchor rules: lowercase, drop punctuation, spaces become hyphens.

    Not handled: the "-1" suffixes GitHub adds to duplicate headings, and headings that contain
    links or HTML. Anchors to such headings need a manual check.
    """
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path):
    found, fence = set(), None
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := FENCE.match(line):
            fence = None if fence == m.group(1) else (fence or m.group(1))
        elif not fence and (m := re.match(r"#{1,6}\s+(.*?)\s*#*$", line)):
            found.add(slug(m.group(1)))
    return found


def manifest_excluded(rel):
    """True when the export manifest excludes rel (or, for a directory, anything below it)."""
    if not MANIFEST.exists():
        return False
    import export_toolkit

    patterns = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["exclude"]
    candidates = [rel] + ([rel.rstrip("/") + "/x"] if (ROOT / rel).is_dir() else [])
    return any(export_toolkit.any_match(patterns, c) for c in candidates)


def targets():
    for row in section_rows():
        for target in LINK.findall(row):
            yield row, target


def code_paths():
    """(row, path) for path-like tokens in code spans, including code inside link text."""
    for row in section_rows():
        for span in CODE_SPAN.findall(row):
            for token in span.split():
                token = token.rstrip(",;.")
                if token.startswith(PATH_PREFIXES) or token.endswith((".toml", ".md")):
                    yield row, token


def rel_path(target):
    return (ROOT / (target.split("#")[0] or "AGENTS.md")).resolve().relative_to(ROOT).as_posix()


class AgentsIndexTest(unittest.TestCase):
    def test_section_has_a_compact_table(self):
        # The index stays compact; a longer list is no longer a quick pointer.
        count = len(section_rows())
        self.assertTrue(
            10 <= count <= 16,
            f"the index has {count} rows; keep 10 to 16 (see docs/bootstrap.md#troubleshooting)",
        )

    def test_every_row_links_somewhere_or_names_a_command(self):
        for row in section_rows():
            self.assertTrue(LINK.search(row) or "`" in row, row)

    def test_no_link_form_is_skipped(self):
        for row in section_rows():
            leftover = LINK.sub("", row)
            for form in ("](", "][", "href="):
                self.assertNotIn(form, leftover, f"unparsed link form in {row}")
            # Autolinks such as <docs/x.md> would also be links the checks above never see.
            self.assertNotRegex(leftover, r"<[^>\s]*(/|\.md)[^>\s]*>", f"unparsed autolink in {row}")

    def test_links_are_relative_and_exist(self):
        for row, target in targets():
            self.assertNotRegex(target, r"^[a-z]+:|^/", f"not relative: {target}")
            path = ROOT / target.split("#")[0]  # a bare "#anchor" resolves to AGENTS.md itself
            self.assertTrue(path.exists() or not target.split("#")[0], f"missing {target} in {row}")

    def test_anchors_match_headings(self):
        for row, target in targets():
            path_part, _, anchor = target.partition("#")
            if not anchor:
                continue
            path = ROOT / path_part if path_part else AGENTS
            self.assertEqual(path.suffix, ".md", f"anchor on non-Markdown {target}")
            self.assertIn(
                anchor,
                anchors(path),
                f"no heading for {target}; fix the anchor, or the heading it points to "
                "(see docs/bootstrap.md#troubleshooting)",
            )

    def test_no_link_to_an_export_excluded_file(self):
        for row, target in targets():
            rel = rel_path(target)
            self.assertFalse(manifest_excluded(rel), f"{rel} is excluded from the export")

    def test_code_span_paths_exist_and_are_not_excluded(self):
        for row, token in code_paths():
            if token in CODE_ALLOWLIST:
                continue
            self.assertTrue((ROOT / token).exists(), f"missing code path {token} in {row}")
            self.assertFalse(manifest_excluded(token), f"{token} is excluded from the export")


if __name__ == "__main__":
    unittest.main()
