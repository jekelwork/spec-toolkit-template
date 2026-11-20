import re
import unittest
from pathlib import Path

GITHUB = Path(__file__).resolve().parent.parent / ".github"
WORKFLOWS = GITHUB / "workflows"
ACTIONS = GITHUB / "actions"
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)([^\n]*)", re.MULTILINE)
VERSION_COMMENT = re.compile(r"^\s+# v\d+(\.\d+)*\s*$")
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")


def unpinned(text):
    """Return the `uses:` references that are not pinned to a commit SHA."""
    bad = []
    for ref, rest in USES.findall(text):
        ref = ref.strip("'\"")
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        if not PINNED.match(ref) or not VERSION_COMMENT.match(rest):
            bad.append(ref)
    return bad


class ActionPinTest(unittest.TestCase):
    def test_every_workflow_action_is_pinned_to_a_commit_sha(self):
        files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml"), *ACTIONS.glob("*/action.yml")])
        self.assertTrue(files)
        self.assertTrue(list(ACTIONS.glob("*/action.yml")))
        for path in files:
            with self.subTest(workflow=path.name):
                self.assertEqual(unpinned(path.read_text(encoding="utf-8")), [])

    def test_app_token_steps_use_client_id_not_the_deprecated_app_id(self):
        # The secret names keep "APP_ID"; their value may be the Client ID or the numeric App ID.
        for path in sorted([*WORKFLOWS.glob("*.yml"), *ACTIONS.glob("*/action.yml")]):
            text = path.read_text(encoding="utf-8")
            if "create-github-app-token" not in text:
                continue
            with self.subTest(workflow=path.name):
                self.assertNotRegex(text, r"(?m)^\s+app-id:")
                self.assertRegex(text, r"(?m)^\s+client-id: \$\{\{ (secrets\.\w+_APP_ID|inputs\.client-id) \}\}")

    def test_detects_tag_and_branch_references(self):
        self.assertEqual(unpinned("- uses: actions/checkout@v4\n"), ["actions/checkout@v4"])
        self.assertEqual(unpinned("  uses: a/b@main\n"), ["a/b@main"])
        self.assertEqual(unpinned("- uses: a/b@" + "a" * 39 + "\n"), ["a/b@" + "a" * 39])

    def test_rejects_sha_without_version_comment(self):
        ref = "a/b@" + "a" * 40
        self.assertEqual(unpinned(f"- uses: {ref}\n"), [ref])
        self.assertEqual(unpinned(f"- uses: {ref} # main\n"), [ref])

    def test_accepts_sha_local_and_docker(self):
        text = f"- uses: a/b@{'a' * 40} # v1.0.0\n- uses: ./local\n- uses: docker://x:1\n"
        self.assertEqual(unpinned(text), [])


if __name__ == "__main__":
    unittest.main()
