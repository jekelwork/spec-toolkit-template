import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
def read_hook_entries(path):
    events = {}
    event = None
    entry = None
    for line in path.read_text(encoding="utf-8").splitlines():
        event_match = re.fullmatch(r"  (before_\w+|after_\w+):", line)
        if event_match:
            event = event_match.group(1)
            if event in events:
                raise AssertionError(f"Duplicate hook event: {event}")
            events[event] = []
            entry = None
            continue
        entry_match = re.fullmatch(r" {2,4}- extension: (\S+)", line)
        if entry_match and event:
            entry = {"extension": entry_match.group(1)}
            events[event].append(entry)
            continue
        field_match = re.fullmatch(
            r" {4,6}(command|condition|enabled|optional|priority): (.*)", line
        )
        if field_match and entry is not None:
            entry[field_match.group(1)] = field_match.group(2)
    return events


class RoadmapDependencyWorkflowTest(unittest.TestCase):
    def test_third_party_roadmap_extension_is_removed(self):
        registry = json.loads(
            (ROOT / ".specify/extensions/.registry").read_text(encoding="utf-8")
        )
        self.assertNotIn("roadmap", registry["extensions"])
        self.assertFalse((ROOT / ".specify/extensions/roadmap").exists())
        for skill in (
            "speckit-roadmap-write",
            "speckit-roadmap-brief",
            "speckit-roadmap-debrief",
            "speckit-roadmap-sync",
        ):
            with self.subTest(skill=skill):
                self.assertFalse((ROOT / ".agents/skills" / skill).exists())

    def test_agent_derives_the_feature_directory(self):
        # Issue #61: the agent derives specs/<id>-<slug> from the roadmap
        # entry and passes it explicitly; the user is never asked to set it.
        def flat(path):
            return " ".join((ROOT / path).read_text(encoding="utf-8").split())

        entry_skill = flat(".agents/skills/roadmap-entry-check/SKILL.md")
        self.assertIn(
            "Derive an explicit `SPECIFY_FEATURE_DIRECTORY=specs/NNN-<slug>` from the "
            "roadmap entry the user or issue names",
            entry_skill,
        )
        self.assertIn("do not ask the user to set it", entry_skill)
        self.assertNotIn("from the user or issue instructions", entry_skill)
        # The slug rule and its example must match roadmap_issues.slugify,
        # which the sync uses.
        self.assertIn(
            "every run of characters other than `a-z` and `0-9` replaced by one hyphen",
            entry_skill,
        )
        self.assertIn('"Don\'t panic" becomes `don-t-panic`', entry_skill)
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import roadmap_issues
        finally:
            sys.path.pop(0)
        self.assertEqual(roadmap_issues.slugify("Don't panic"), "don-t-panic")

        agents = flat("AGENTS.md")
        self.assertIn(
            "derive `SPECIFY_FEATURE_DIRECTORY=specs/<roadmap-id>-<slug>` from the roadmap entry",
            agents,
        )
        self.assertIn("Do not ask the user to set it", agents)

        # docs/bootstrap.md is pinned by test_bootstrap_guide_safety.py.
        self.assertIn("derived by the agent from the entry", flat("docs/spec-workflow.md"))

    def test_exactly_three_roadmap_hooks_are_registered(self):
        skill = ROOT / ".agents/skills/roadmap-dependency-check/SKILL.md"
        self.assertTrue(skill.is_file(), f"Missing guard skill: {skill}")
        self.assertIn("name: roadmap-dependency-check", skill.read_text(encoding="utf-8"))

        entry_skill = ROOT / ".agents/skills/roadmap-entry-check/SKILL.md"
        self.assertTrue(entry_skill.is_file(), f"Missing entry check skill: {entry_skill}")
        self.assertIn("name: roadmap-entry-check", entry_skill.read_text(encoding="utf-8"))

        writer_skill = ROOT / ".agents/skills/roadmap-write/SKILL.md"
        self.assertTrue(writer_skill.is_file(), f"Missing roadmap writer skill: {writer_skill}")
        self.assertIn("name: roadmap-write", writer_skill.read_text(encoding="utf-8"))

        events = read_hook_entries(ROOT / ".specify/extensions.yml")
        roadmap_hooks = [
            (event, hook)
            for event, hooks in events.items()
            for hook in hooks
            if hook.get("command", "").startswith(("roadmap.", "roadmap-"))
        ]
        self.assertEqual(
            {(event, hook["command"]) for event, hook in roadmap_hooks},
            {
                ("before_specify", "roadmap-entry.check"),
                ("before_implement", "roadmap-dependency.check"),
                ("after_constitution", "roadmap.write"),
            },
        )

        entry = events["before_specify"][0]
        self.assertEqual(entry.get("enabled"), "true")
        self.assertEqual(entry.get("optional"), "false")
        self.assertEqual(entry.get("priority"), "5")
        dependency = events["before_implement"][0]
        self.assertEqual(dependency.get("enabled"), "true")
        self.assertEqual(dependency.get("optional"), "false")
        writer = events["after_constitution"][0]
        self.assertEqual(writer.get("enabled"), "true")
        self.assertEqual(writer.get("optional"), "true")
        self.assertEqual(
            [hook["command"] for hook in events["before_tasks"]],
            ["plan-merged.check"],
        )
        self.assertEqual(
            [hook["command"] for hook in events["after_specify"]],
            ["speckit.clarify"],
        )
        self.assertEqual(
            [hook["command"] for hook in events["after_tasks"]],
            ["speckit.analyze"],
        )
        self.assertEqual(
            [hook["command"] for hook in events["after_implement"]],
            ["code-review-and-quality"],
        )

        for event in ("before_clarify", "before_plan", "before_tasks",
                      "before_analyze", "before_converge"):
            self.assertFalse(
                any(hook.get("command") == "roadmap-dependency.check"
                    for hook in events.get(event, [])),
                event,
            )
        self.assertFalse(
            any(hook.get("command", "").startswith("speckit.roadmap.")
                for hooks in events.values() for hook in hooks)
        )

    def test_direct_work_and_delegation_require_the_guard(self):
        instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        guard = next(
            line for line in instructions.splitlines()
            if line.startswith("- **Hard rule: check roadmap dependencies")
        )
        self.assertIn("roadmap-dependency-check", guard)
        for action in ("branch", "In Progress", "launching agents"):
            self.assertIn(action, guard)
        self.assertIn("Phase", guard)
        self.assertIn("roadmap-entry-check", guard)
        self.assertIn("warn", guard)
        skill = (ROOT / ".agents/skills/roadmap-dependency-check/SKILL.md").read_text()
        self.assertNotIn("proceed with planning 005", skill)
        self.assertIn("Phase/implementation", skill)
        scenarios = (ROOT / "scripts/README.md").read_text()
        self.assertNotIn("**Blocked specification:**", scenarios)
        self.assertIn("**Blocked implementation:**", scenarios)

        implementer = (ROOT / "agent-definitions/tdd-implementer.md").read_text(encoding="utf-8")
        self.assertIn("roadmap-dependency.check", implementer)
        self.assertIn("require the caller's", implementer)
        self.assertIn("explicit", implementer)


if __name__ == "__main__":
    unittest.main()
