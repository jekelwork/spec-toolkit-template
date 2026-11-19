"""Reference and rules test for the `roadmap-write` skill (.agents/skills/roadmap-write/SKILL.md).

The skill is prose that an agent follows, so this test checks:

- what it names exists: every relative link resolves from the skill's directory, with its anchor;
  every `python3 scripts/...` command names an existing script; and the plan statuses it names in
  code spans are the validator's (`roadmap_issues.PLAN_STATUSES`);
- what it must say (`SKILL_RULES`, by section heading): the roadmap is a living document that a
  first version is enough for, a draft is offered when the user asks for one or hands the content
  over (for a new roadmap or an amendment), inferred content is marked as a proposal, the whole
  file is shown, nothing is written without an explicit yes, and "never invent" applies only when
  no draft was asked for (#50);
- the matching statements in `docs/bootstrap.md` (Roadmap) and `docs/roadmap-format.md`
  (`DOC_RULES`).

The guard tests run each check on a deliberately broken copy and assert the specific error.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import roadmap_issues  # noqa: E402
from skill_text_helpers import (  # noqa: E402
    CODE_SPAN, check_links, remove_phrase, section_of, squash,
)

SKILL = ROOT / ".agents" / "skills" / "roadmap-write" / "SKILL.md"
BOOTSTRAP_DOC = ROOT / "docs" / "bootstrap.md"
FORMAT_DOC = ROOT / "docs" / "roadmap-format.md"
SCRIPT = re.compile(r"python3 (scripts/[\w.-]+\.py)")
# A list of three or more code spans of lowercase or hyphenated words (`planned`, `needs-info`
# or `deferred`). A two-item list is not checked; a future three-item list of other plain words
# (for example `git`, `gh` or `none`) would be reported as unknown statuses, which fails loudly.
STATUS_LIST = re.compile(r"((?:`[a-z-]+`(?:,\s*|\s+or\s+|,\s+or\s+)){2,}`[a-z-]+`)")
PROGRESS_WORDS = {"specced", "in-progress", "implemented", "verified"}

# What the skill must state, by section heading (an anchored, case-insensitive pattern on the
# heading text) and then needle (a case-sensitive, whitespace-insensitive phrase of that section).
SKILL_RULES = {
    r"^the roadmap is a living document$": (
        "When you create the roadmap",
        "seems unsure what to write",
        "before your first question",
        "For a routine amendment of an existing roadmap, skip this",
        "A first version is enough", "one entry is fine",
        "can amend the roadmap at any time",
        "add entries", "change scope or dependencies",
        "pause an entry with `needs-info` or `deferred`",
        "drop it with `abandoned`",
        "not reopened automatically",
        "for `planned` and `needs-info` entries",
        "change its status instead of deleting or renumbering it",
        "deleting such an entry abandons it",
        "ask for a first draft",
    ),
    r"^drafting on request$": (
        "asks for a draft",
        "hands the content over",
        "Do not ask the gap questions again",
        "to a new roadmap and to an amendment",
        "propose a complete first version",
        "the project brief (if any) and what the user has already answered",
        "the user's answers, or your own assumption",
        "which entries will get GitHub issues",
        "every required field",
        "Mark what you inferred",
        "as proposals",
        "Show the whole file",
        "Write only after an explicit yes",
        "A change request is not a yes",
    ),
    r"^gather and confirm context$": (
        "follow [Drafting on request](#drafting-on-request) instead of asking",
        # step 2 (no roadmap yet, the #50 case) and step 3 (amending) each need their own needle
        "When the user asks for a draft or hands the content over, follow",
        "When the user asks for a draft of the change or hands it over",
        "Unless the user has asked for a draft or handed the content over",
        "never invent decisions, scope, outcomes, or dependencies",
        "stop and ask rather than fabricate",
    ),
}

# The same statements in the docs: (file, `## section`) -> needles.
DOC_RULES = {
    (BOOTSTRAP_DOC, "Roadmap"): (
        "ask the skill for a first draft",
        "marks what it inferred",
        "writes only after your yes",
        "A first version is enough",
        "can amend it at any time",
    ),
    (FORMAT_DOC, None): (
        "can draft a first version on request",
        "can be amended at any time",
    ),
}


def check_skill_rules(text):
    errors = []
    for pattern, needles in SKILL_RULES.items():
        section = section_of(text, pattern)
        if section is None:
            errors.append(f"no section matching {pattern!r}")
            continue
        flat = squash(section)
        errors += [f"section {pattern!r} lacks {n!r}" for n in needles if squash(n) not in flat]
    return errors


def doc_scope(text, section):
    if section is None:
        return text
    match = re.search(rf"^## {re.escape(section)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1) if match else None


def check_doc_rules(texts):
    """`texts` maps each doc path to its text."""
    errors = []
    for (path, section), needles in DOC_RULES.items():
        scope = doc_scope(texts[path], section)
        if scope is None:
            errors.append(f"{path.name} has no section {section!r}")
            continue
        flat = squash(scope)
        errors += [f"{path.name} section {section!r} lacks {n!r}"
                   for n in needles if squash(n) not in flat]
    return errors


def check_scripts(text):
    return [f"script does not exist: {path}"
            for path in SCRIPT.findall(text) if not (ROOT / path).is_file()]


def check_statuses(text):
    """Every status list in the skill names only plan statuses, or only the progress words it
    forbids. Unknown names are reported; that a list names every plan status is checked in
    `test_the_skill_lists_every_plan_status`."""
    errors = []
    for match in STATUS_LIST.finditer(squash(text)):
        names = set(CODE_SPAN.findall(match.group(1)))
        if names <= PROGRESS_WORDS:
            continue
        unknown = names - set(roadmap_issues.PLAN_STATUSES)
        if unknown:
            errors.append(f"not a plan status: {', '.join(sorted(unknown))}")
    return errors


def check_all(text):
    return check_links(text, SKILL) + check_scripts(text) + check_statuses(text)


class RoadmapWriteSkillReferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")

    def test_what_the_skill_names_exists(self):
        self.assertEqual([], check_all(self.text))

    def test_the_skill_lists_every_plan_status(self):
        lists = [set(CODE_SPAN.findall(m.group(1)))
                 for m in STATUS_LIST.finditer(squash(self.text))]
        self.assertIn(set(roadmap_issues.PLAN_STATUSES), lists)

    def test_a_broken_link_is_reported(self):
        broken = self.text.replace("docs/roadmap-format.md)", "docs/no-such-file.md)", 1)
        self.assertIn("link target does not exist from the skill: ../../../docs/no-such-file.md",
                      check_all(broken))

    def test_a_missing_script_is_reported(self):
        broken = self.text.replace("scripts/check_roadmap.py", "scripts/no_such_check.py", 1)
        self.assertIn("script does not exist: scripts/no_such_check.py", check_all(broken))

    def test_an_unknown_status_is_reported(self):
        broken = self.text.replace("`undecided`, `needs-info`", "`undecided`, `paused`", 1)
        self.assertIn("not a plan status: paused", check_all(broken))


class RoadmapWriteSkillRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")
        cls.docs = {path: path.read_text(encoding="utf-8") for path in (BOOTSTRAP_DOC, FORMAT_DOC)}

    def test_every_skill_rule_is_guarded(self):
        self.assertEqual([], check_skill_rules(self.text))
        self.assertIn("no section matching '^drafting on request$'",
                      check_skill_rules("## Other\n\nx\n"))
        for pattern, needles in SKILL_RULES.items():
            for needle in needles:
                broken = remove_phrase(self.text, needle)
                self.assertIn(f"section {pattern!r} lacks {needle!r}", check_skill_rules(broken))

    def test_skill_rule_patterns_match_one_heading(self):
        headings = re.findall(r"(?m)^#{1,6} (.*)$", self.text)
        for pattern in SKILL_RULES:
            self.assertTrue(pattern.startswith("^") and pattern.endswith("$"), pattern)
            self.assertEqual(1, sum(bool(re.search(pattern, h, re.I)) for h in headings), pattern)

    def test_every_doc_rule_is_guarded(self):
        self.assertEqual([], check_doc_rules(self.docs))
        for (path, section), needles in DOC_RULES.items():
            for needle in needles:
                broken = dict(self.docs)
                broken[path] = remove_phrase(self.docs[path], needle)
                self.assertIn(f"{path.name} section {section!r} lacks {needle!r}",
                              check_doc_rules(broken))
        renamed = dict(self.docs)
        renamed[BOOTSTRAP_DOC] = self.docs[BOOTSTRAP_DOC].replace("## Roadmap\n", "## Other\n")
        self.assertIn("bootstrap.md has no section 'Roadmap'", check_doc_rules(renamed))


if __name__ == "__main__":
    unittest.main()
