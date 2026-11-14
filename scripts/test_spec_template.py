import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".specify" / "templates" / "overrides" / "spec-template.md"
SCRIPT = ROOT / "scripts" / "check_roadmap_spec_ids.py"


def input_line():
    return next(
        line for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
        if line.startswith("**Input**:")
    )


class SpecTemplateInputLineTest(unittest.TestCase):
    def check(self, line, folder="012-example", entry="012"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ROADMAP.md").write_text(f"### {entry} — Example  [status: planned]\n")
            spec = root / "specs" / folder / "spec.md"
            spec.parent.mkdir(parents=True)
            spec.write_text(line + "\n")
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True
            )

    def test_filled_in_template_line_is_accepted(self):
        result = self.check(input_line().replace("[NNN]", "012"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_filled_in_line_for_another_entry_is_rejected(self):
        result = self.check(input_line().replace("[NNN]", "013"))
        self.assertNotEqual(result.returncode, 0)

    def test_unfilled_template_line_is_rejected(self):
        self.assertIn("[NNN]", input_line())
        result = self.check(input_line())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing numbered folder or roadmap entry", result.stderr)

    def test_template_keeps_the_arguments_placeholder(self):
        self.assertIn('"$ARGUMENTS"', input_line())

    @unittest.skipUnless(shutil.which("bash"), "bash is not available")
    def test_resolved_template_is_the_override_with_the_roadmap_reference(self):
        # speckit-specify reads the template through resolve-template.sh, not the file directly.
        result = subprocess.run(
            ["bash", ".specify/scripts/bash/resolve-template.sh", "spec-template", "--json"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        content = json.loads(result.stdout)["TEMPLATE_CONTENT"]
        self.assertIn('**Input**: `ROADMAP.md` entry [NNN]; user description: "$ARGUMENTS"', content)


if __name__ == "__main__":
    unittest.main()
