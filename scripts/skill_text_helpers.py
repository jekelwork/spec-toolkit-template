"""Generic helpers for the tests that lint a skill's prose (`.agents/skills/*/SKILL.md`).

Shared by `test_bootstrap_guide_skill.py`, `test_bootstrap_guide_safety.py` and
`test_roadmap_write_skill.py`: the heading, code-span, code-block and link patterns, GitHub's
heading anchors, section lookup, relative-link checking, whitespace-insensitive comparison and
`remove_phrase` for the guard tests. Nothing here depends on `bootstrap.py` or the docs. The name
does not start with `test_`, so test discovery does not collect it.
"""

import re

CODE_SPAN = re.compile(r"`([^`\n]+)`")
FENCE = re.compile(r"^```[^\n]*\n.*?^```[ \t]*$", re.S | re.M)
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6}) (.*)$", re.M)


def slug(heading):
    """GitHub's heading anchor: lowercase, drop punctuation except `-` and `_`, spaces to `-`."""
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def doc_anchors(text):
    headings = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"#{1,6} ", line):
            headings.append(slug(line.lstrip("#")))
    return set(headings)


def section_of(text, pattern):
    """Text of the first heading matching `pattern`, up to the next heading of the same or a
    higher level; None when there is none."""
    for match in HEADING.finditer(text):
        if re.search(pattern, match.group(2), re.I):
            level = len(match.group(1))
            rest = text[match.end():]
            end = re.search(rf"^#{{1,{level}}} ", rest, re.M)
            return rest[:end.start()] if end else rest
    return None


def check_links(text, skill_path):
    """Every relative link must resolve from the skill's own directory: the file exists, and an
    `#anchor` matches a heading of the target file (of the skill itself for `#anchor` alone)."""
    errors = []
    prose = FENCE.sub("", text)
    for link in LINK.findall(prose):
        if re.match(r"[a-z][a-z0-9+.-]*:", link):  # https:, mailto:, ...
            continue
        target, _, anchor = link.partition("#")
        if not target:
            path = skill_path
        else:
            path = (skill_path.parent / target).resolve()
            if not path.is_file():
                errors.append(f"link target does not exist from the skill: {link}")
                continue
        if anchor and path.suffix == ".md":
            headings = doc_anchors(prose if path == skill_path else path.read_text())
            if anchor not in headings:
                errors.append(f"no heading for the anchor in link: {link}")
    return errors


def squash(text):
    return " ".join(text.split())


def remove_phrase(text, phrase):
    """`text` without the phrase (whitespace-insensitive, case-sensitive); the text must change."""
    pattern = r"\s+".join(map(re.escape, phrase.split()))
    broken = re.sub(pattern, "x", text)
    assert broken != text, f"phrase not found: {phrase!r}"
    return broken
